"""Fast lock attempt with ARC/PC pre-tuning.

This mode is for testing the workflow:
1. sweep with ASG amplitude = 1,
2. tune ARC factor toward a platform-drop 1/4 full width near 0.1 V,
3. center the dip near Out2 = 0 with TOPTICA PC voltage,
4. turn off ASG and try a guarded PID handoff from the platform side.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

import matplotlib.pyplot as plt
import numpy as np

from suggest_arc_factor import (
    RESULTS_DIR,
    analyze_apparent_width,
    bridge_get,
    configure_prelock_sweep,
    make_plot,
    read_arc_factor,
)
from test_arc_factor_lock_series import compact_capture, remove_file
from tune_arc_fullwidth_center import read_pc, write_arc_factor, write_pc_voltage


def set_param(base: str, param: str, value: object) -> object:
    result = bridge_get(base, "/set", {"param": param, "value": str(value)})
    if not result.get("ok"):
        raise RuntimeError(result)
    return result.get("after")


def get_value(base: str, param: str) -> object:
    result = bridge_get(base, "/get", {"param": param})
    if not result.get("ok"):
        raise RuntimeError(result)
    return result["value"]


def sample(base: str) -> dict[str, float]:
    return {
        "ch1": float(get_value(base, "scope.voltage_in1")),
        "ch2": float(get_value(base, "scope.voltage_in2")),
        "p": float(get_value(base, "pid0.p")),
        "i": float(get_value(base, "pid0.i")),
        "ival": float(get_value(base, "pid0.ival")),
    }


def capture_prelock(args: argparse.Namespace, tag: str) -> dict[str, object]:
    arc_info = read_arc_factor(args.host)
    pc_info = read_pc(args.host)
    configure_prelock_sweep(
        args.base,
        args.duration,
        args.sweep_frequency,
        args.sweep_amplitude,
    )
    time.sleep(args.settle_seconds)
    capture = bridge_get(args.base, "/scope/single", {"tag": tag, "timeout": 8, "plot": "false"})
    if not capture.get("ok"):
        return {"ok": False, "capture": capture, "arc_info": arc_info, "pc_info": pc_info}
    with np.load(Path(capture["path"])) as data:
        t = np.asarray(data["t"], dtype=float)
        ch1 = np.asarray(data["ch1"], dtype=float)
        ch2 = np.asarray(data["ch2"], dtype=float)
    remove_file(capture.get("path"))
    remove_file(capture.get("plot_path"))
    width = analyze_apparent_width(t, ch1, ch2, 0.75)
    lock = analyze_apparent_width(t, ch1, ch2, args.lock_depth_fraction)
    return {
        "ok": bool(width.get("ok") and lock.get("ok")),
        "capture": compact_capture(capture),
        "arc_info": arc_info,
        "pc_info": pc_info,
        "width_analysis": width,
        "lockpoint_analysis": lock,
        "trace": {"t": t, "ch1": ch1, "ch2": ch2},
    }


def tune_arc_pc(args: argparse.Namespace, run_tag: str) -> tuple[list[dict[str, object]], dict[str, object]]:
    iterations: list[dict[str, object]] = []
    last: dict[str, object] = {}
    for index in range(args.max_pretune_iterations):
        result = capture_prelock(args, f"{run_tag}_pretune{index + 1:02d}")
        result_for_summary = {k: v for k, v in result.items() if k != "trace"}
        iterations.append(result_for_summary)
        last = result
        if not result.get("ok"):
            break
        width = result["width_analysis"]
        chosen = width["chosen"]
        full_width = float(chosen["quarter_full_width_out2_v"])
        dip = float(chosen["min_sweep_voltage"])
        arc = float(result["arc_info"]["arc_factor"])
        pc_set = float(result["pc_info"]["voltage_set"])
        centered = abs(dip) <= args.center_tolerance
        width_ok = args.min_full_width <= full_width <= args.max_full_width
        print(
            f"pretune {index + 1}: arc={arc:.6g} pc={pc_set:.6g} "
            f"dip={dip:+.5f} full_width={full_width:.5f}",
            flush=True,
        )
        if centered and width_ok:
            iterations[-1]["decision"] = "target_reached"
            break
        if not centered:
            requested_pc = pc_set + args.pc_center_gain * arc * dip
            write = write_pc_voltage(args.host, requested_pc)
            iterations[-1]["write"] = {
                "param": "laser1.dl.pc.voltage_set",
                "requested": requested_pc,
                "readback": write,
                "reason": "center dip near Out2=0",
            }
            continue
        requested_arc = arc * full_width / args.target_full_width
        if full_width < args.min_full_width:
            requested_arc = max(
                requested_arc, arc * (1 - args.max_fractional_step), args.min_arc
            )
            reason = "increase apparent width by decreasing ARC factor"
        else:
            requested_arc = min(
                requested_arc, arc * (1 + args.max_fractional_step), args.max_arc
            )
            reason = "decrease apparent width by increasing ARC factor"
        write = write_arc_factor(args.host, requested_arc)
        iterations[-1]["write"] = {
            "param": "laser1.dl.pc.external_input.factor",
            "requested": requested_arc,
            "readback": write,
            "reason": reason,
        }
    return iterations, last


def monitor(base: str, seconds: float, interval: float, run_start: float) -> list[dict[str, object]]:
    rows = []
    start = time.time()
    while time.time() - start < seconds:
        row: dict[str, object] = {"elapsed_s": time.time() - run_start, "phase": "monitor"}
        row.update(sample(base))
        rows.append(row)
        if abs(float(row["ch2"])) > 0.98 or abs(float(row["ival"])) > 3.8:
            break
        time.sleep(interval)
    return rows


def summarize(rows: list[dict[str, object]], target: float) -> dict[str, object]:
    if not rows:
        return {"ok": False}
    ch1 = [float(row["ch1"]) for row in rows]
    ch2 = [float(row["ch2"]) for row in rows]
    ival = [float(row["ival"]) for row in rows]
    return {
        "ok": True,
        "n": len(rows),
        "ch1_mean": statistics.fmean(ch1),
        "ch1_min": min(ch1),
        "ch1_max": max(ch1),
        "ch1_ptp": max(ch1) - min(ch1),
        "ch1_std": statistics.pstdev(ch1) if len(ch1) > 1 else 0.0,
        "target_error_mean": statistics.fmean(ch1) - target,
        "ch2_mean": statistics.fmean(ch2),
        "ch2_min": min(ch2),
        "ch2_max": max(ch2),
        "ch2_ptp": max(ch2) - min(ch2),
        "ival_start": ival[0],
        "ival_end": ival[-1],
        "ival_drift": ival[-1] - ival[0],
        "out2_saturated": max(abs(v) for v in ch2) > 0.98,
        "ival_saturated": max(abs(v) for v in ival) > 3.8,
    }


def save_timeline(path: Path, rows: list[dict[str, object]], target: float) -> None:
    if not rows:
        return
    csv_path = path.with_suffix(".csv")
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    plt.rcParams.update(
        {
            "font.size": 14,
            "axes.titlesize": 20,
            "axes.labelsize": 17,
            "xtick.labelsize": 14,
            "ytick.labelsize": 14,
            "legend.fontsize": 13,
            "lines.linewidth": 2.2,
            "savefig.dpi": 300,
            "savefig.bbox": "tight",
        }
    )
    fig, axes = plt.subplots(2, 1, figsize=(13, 7), sharex=True)
    axes[0].plot([float(r["elapsed_s"]) for r in rows], [float(r["ch1"]) for r in rows], "o-", label="CH1")
    axes[0].axhline(target, color="tab:orange", linestyle="--", label="target")
    axes[1].plot([float(r["elapsed_s"]) for r in rows], [float(r["ch2"]) for r in rows], "o-", label="CH2 / Out2")
    axes[0].set_ylabel("CH1 / transmission (V)")
    axes[1].set_ylabel("CH2 / Out2 (V)")
    axes[1].set_xlabel("Elapsed time (s)")
    for axis in axes:
        axis.grid(True, alpha=0.3)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="center left", bbox_to_anchor=(1.01, 0.5))
    fig.suptitle("Guarded PID handoff monitor")
    fig.subplots_adjust(right=0.82, top=0.90)
    fig.savefig(path.with_suffix(".png"), dpi=300, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7870")
    parser.add_argument("--host", default="192.168.1.104")
    parser.add_argument("--tag", default="fast_pretune_lock")
    parser.add_argument("--sweep-frequency", type=float, default=50.0)
    parser.add_argument("--sweep-amplitude", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=0.067108864)
    parser.add_argument("--settle-seconds", type=float, default=0.5)
    parser.add_argument("--target-full-width", type=float, default=0.10)
    parser.add_argument("--min-full-width", type=float, default=0.08)
    parser.add_argument("--max-full-width", type=float, default=0.24)
    parser.add_argument("--center-tolerance", type=float, default=0.03)
    parser.add_argument("--max-pretune-iterations", type=int, default=6)
    parser.add_argument("--max-fractional-step", type=float, default=0.35)
    parser.add_argument("--min-arc", type=float, default=0.2)
    parser.add_argument("--max-arc", type=float, default=60.0)
    parser.add_argument("--pc-center-gain", type=float, default=0.8)
    parser.add_argument("--platform-ival", type=float, default=0.95)
    parser.add_argument("--initial-ival", type=float)
    parser.add_argument("--lock-depth-fraction", type=float, default=0.25)
    parser.add_argument("--p", type=float, default=0.01)
    parser.add_argument("--i", type=float, default=10.0)
    parser.add_argument("--monitor-seconds", type=float, default=2.0)
    parser.add_argument("--monitor-interval", type=float, default=0.1)
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_tag = f"{args.tag}_{stamp}"
    run_dir = RESULTS_DIR / run_tag
    run_dir.mkdir(parents=True, exist_ok=True)
    run_start = time.time()

    iterations, final = tune_arc_pc(args, run_tag)
    failure = None
    monitor_rows: list[dict[str, object]] = []
    monitor_summary: dict[str, object] = {"ok": False}
    target = None
    lock_center = None
    final_plot_path = None

    if not final.get("ok"):
        failure = {"stage": "pretune", "reason": "final prelock analysis failed"}
    else:
        width = final["width_analysis"]
        lock = final["lockpoint_analysis"]
        full_width = float(width["chosen"]["quarter_full_width_out2_v"])
        dip = float(width["chosen"]["min_sweep_voltage"])
        pretune_ok = args.min_full_width <= full_width <= args.max_full_width and abs(dip) <= args.center_tolerance
        trace = final["trace"]
        final_plot_path = run_dir / "final_prelock_downsweep_width_lock.png"
        make_plot(
            final_plot_path,
            np.asarray(trace["t"], dtype=float),
            np.asarray(trace["ch1"], dtype=float),
            np.asarray(trace["ch2"], dtype=float),
            width,
            final["arc_info"],
            None,
            lock,
        )
        if not pretune_ok:
            failure = {
                "stage": "pretune",
                "reason": "width or center target not reached",
                "full_width": full_width,
                "dip_out2": dip,
            }
        else:
            target = float(lock["transmission_lock"])
            lock_center = float(lock["chosen"]["capture_side_sweep_voltage"])
            set_param(args.base, "asg0.output_direct", "off")
            set_param(args.base, "pid0.p", 0)
            set_param(args.base, "pid0.i", 0)
            set_param(args.base, "pid0.output_direct", "out2")
            initial_ival = (
                args.initial_ival
                if args.initial_ival is not None
                else args.platform_ival if lock_center <= 0 else -args.platform_ival
            )
            set_param(args.base, "pid0.ival", initial_ival)
            set_param(args.base, "pid0.setpoint", target)
            set_param(args.base, "pid0.p", args.p)
            set_param(args.base, "pid0.i", args.i)
            monitor_rows = monitor(args.base, args.monitor_seconds, args.monitor_interval, run_start)
            monitor_summary = summarize(monitor_rows, target)
            save_timeline(run_dir / "handoff_monitor", monitor_rows, target)
            if monitor_summary.get("out2_saturated") or monitor_summary.get("ival_saturated"):
                failure = {"stage": "handoff", "reason": "saturation guard triggered"}
                set_param(args.base, "pid0.i", 0)
                set_param(args.base, "pid0.p", 0)
                set_param(args.base, "pid0.output_direct", "off")

    summary = {
        "ok": failure is None,
        "run_dir": str(run_dir),
        "elapsed_s": time.time() - run_start,
        "settings": vars(args),
        "iterations": iterations,
        "final_arc": read_arc_factor(args.host),
        "final_pc": read_pc(args.host),
        "target_ch1": target,
        "lock_center_out2": lock_center,
        "monitor": monitor_summary,
        "failure": failure,
        "final_prelock_plot_path": str(final_plot_path) if final_plot_path else None,
    }
    (run_dir / "fast_pretune_lock_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2), flush=True)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
