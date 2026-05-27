"""Fast single-mode lock attempt from one downsweep pre-lock trace."""

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
from tune_arc_fullwidth_center import read_pc


SESSION_DIR = Path(__file__).resolve().parents[1]


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


def mean_at_ival(base: str, ival: float, dwell: float, samples: int) -> dict[str, float]:
    set_param(base, "pid0.ival", ival)
    time.sleep(dwell)
    rows = [sample(base) for _ in range(samples)]
    return {
        "ival": float(get_value(base, "pid0.ival")),
        "ch1": statistics.fmean(row["ch1"] for row in rows),
        "ch2": statistics.fmean(row["ch2"] for row in rows),
    }


def monitor(base: str, seconds: float, interval: float, run_start: float) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    start = time.time()
    while time.time() - start < seconds:
        row = {"elapsed_s": time.time() - run_start, "monitor_t_s": time.time() - start}
        row.update(sample(base))
        rows.append(row)
        time.sleep(interval)
    return rows


def summarize_monitor(rows: list[dict[str, float]], target: float) -> dict[str, object]:
    if not rows:
        return {"ok": False}
    ch1 = [row["ch1"] for row in rows]
    ch2 = [row["ch2"] for row in rows]
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
        "out2_saturated": max(abs(v) for v in ch2) > 0.98,
        "ival_saturated": max(abs(row["ival"]) for row in rows) > 3.8,
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def plot_timeline(path: Path, rows: list[dict[str, object]], target: float) -> None:
    plt.rcParams.update(
        {
            "font.size": 14,
            "axes.titlesize": 20,
            "axes.labelsize": 17,
            "xtick.labelsize": 14,
            "ytick.labelsize": 14,
            "legend.fontsize": 13,
            "lines.linewidth": 2.2,
            "figure.dpi": 150,
            "savefig.dpi": 300,
            "savefig.bbox": "tight",
        }
    )
    fig, axes = plt.subplots(2, 1, figsize=(14, 7), sharex=True)
    styles = {
        "probe": ("D", "tab:red"),
        "approach": ("^", "tab:purple"),
        "monitor": ("o", "tab:green"),
    }
    for phase, (marker, color) in styles.items():
        phase_rows = [row for row in rows if row.get("phase") == phase]
        if not phase_rows:
            continue
        axes[0].plot(
            [float(row["elapsed_s"]) for row in phase_rows],
            [float(row["ch1"]) for row in phase_rows],
            marker=marker,
            color=color,
            label=phase,
        )
        axes[1].plot(
            [float(row["elapsed_s"]) for row in phase_rows],
            [float(row["ch2"]) for row in phase_rows],
            marker=marker,
            color=color,
            label=phase,
        )
    axes[0].axhline(target, color="tab:orange", linestyle="--", label="target")
    axes[0].set_ylabel("CH1 / transmission (V)")
    axes[1].set_ylabel("CH2 / Out2 (V)")
    axes[1].set_xlabel("Elapsed time (s)")
    for axis in axes:
        axis.grid(True, alpha=0.3)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="center left", bbox_to_anchor=(1.01, 0.5))
    fig.suptitle("Fast lock timeline")
    fig.subplots_adjust(right=0.80, top=0.90, hspace=0.18)
    fig.savefig(path, dpi=300, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7870")
    parser.add_argument("--host", default="192.168.1.104")
    parser.add_argument("--tag", default="fast_lock")
    parser.add_argument("--p", type=float, default=-0.001)
    parser.add_argument("--i", type=float, default=-50.0)
    parser.add_argument("--confirm-seconds", type=float, default=2.0)
    parser.add_argument("--monitor-interval", type=float, default=0.2)
    parser.add_argument("--probe-step", type=float, default=0.02)
    parser.add_argument("--probe-dwell", type=float, default=0.12)
    parser.add_argument("--probe-samples", type=int, default=1)
    parser.add_argument("--approach-step", type=float, default=0.006)
    parser.add_argument("--approach-max-steps", type=int, default=8)
    parser.add_argument("--handoff-tolerance", type=float, default=0.012)
    parser.add_argument("--sweep-frequency", type=float, default=50.0)
    parser.add_argument("--sweep-amplitude", type=float, default=0.5)
    parser.add_argument("--duration", type=float, default=0.067108864)
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_tag = f"{args.tag}_{stamp}"
    run_dir = RESULTS_DIR / run_tag
    run_dir.mkdir(parents=True, exist_ok=True)
    run_start = time.time()
    events: list[dict[str, object]] = []
    failure: dict[str, object] | None = None

    arc_info = read_arc_factor(args.host)
    pc_info = read_pc(args.host)

    configure_prelock_sweep(args.base, args.duration, args.sweep_frequency, args.sweep_amplitude)
    capture = bridge_get(
        args.base, "/scope/single", {"tag": run_tag, "timeout": 8, "plot": "false"}
    )
    if not capture.get("ok"):
        failure = {"stage": "prelock_capture", "reason": capture}
        summary = {"ok": False, "failure": failure, "elapsed_s": time.time() - run_start}
        (run_dir / "fast_lock_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, indent=2), flush=True)
        return 1

    with np.load(Path(capture["path"])) as data:
        t = np.asarray(data["t"], dtype=float)
        ch1 = np.asarray(data["ch1"], dtype=float)
        ch2 = np.asarray(data["ch2"], dtype=float)
    remove_file(capture.get("path"))
    remove_file(capture.get("plot_path"))

    width_analysis = analyze_apparent_width(t, ch1, ch2, 0.75)
    lock_analysis = analyze_apparent_width(t, ch1, ch2, 0.25)
    prelock_plot = run_dir / "prelock_downsweep_width_lock.png"
    make_plot(prelock_plot, t, ch1, ch2, width_analysis, arc_info, None, lock_analysis)

    if not width_analysis.get("ok") or not lock_analysis.get("ok"):
        failure = {"stage": "prelock_analysis", "width": width_analysis, "lock": lock_analysis}
    else:
        lock_chosen = lock_analysis["chosen"]
        target = float(lock_analysis["transmission_lock"])
        center = float(lock_chosen["capture_side_sweep_voltage"])
        set_param(args.base, "asg0.output_direct", "off")
        set_param(args.base, "pid0.p", 0)
        set_param(args.base, "pid0.i", 0)
        set_param(args.base, "pid0.setpoint", target)
        set_param(args.base, "pid0.output_direct", "out2")

        probe_points = [
            ("center", center),
            ("plus", center + abs(args.probe_step)),
            ("minus", center - abs(args.probe_step)),
        ]
        probe = {}
        for label, ival in probe_points:
            row = mean_at_ival(args.base, ival, args.probe_dwell, args.probe_samples)
            row.update({"phase": "probe", "point": label, "elapsed_s": time.time() - run_start})
            events.append(row)
            probe[label] = row

        center_error = float(probe["center"]["ch1"]) - target
        plus_delta = float(probe["plus"]["ch1"]) - float(probe["center"]["ch1"])
        minus_delta = float(probe["minus"]["ch1"]) - float(probe["center"]["ch1"])
        if abs(center_error) <= args.handoff_tolerance:
            direction = 0.0
        elif center_error > 0:
            direction = 1.0 if plus_delta < minus_delta else -1.0
        else:
            direction = 1.0 if plus_delta > minus_delta else -1.0

        best_row = dict(probe["center"])
        best_abs_error = abs(float(best_row["ch1"]) - target)
        current = float(probe["center"]["ival"])
        if direction != 0.0:
            for step_index in range(args.approach_max_steps):
                current += direction * abs(args.approach_step)
                row = mean_at_ival(
                    args.base, current, args.probe_dwell, args.probe_samples
                )
                row.update(
                    {
                        "phase": "approach",
                        "step_index": step_index,
                        "elapsed_s": time.time() - run_start,
                    }
                )
                events.append(row)
                err = abs(float(row["ch1"]) - target)
                if err < best_abs_error:
                    best_abs_error = err
                    best_row = dict(row)
                if err <= args.handoff_tolerance:
                    break

        set_param(args.base, "pid0.ival", best_row["ival"])
        set_param(args.base, "pid0.p", args.p)
        set_param(args.base, "pid0.i", args.i)
        monitor_rows = monitor(args.base, args.confirm_seconds, args.monitor_interval, run_start)
        for row in monitor_rows:
            row["phase"] = "monitor"
        events.extend(monitor_rows)
        monitor_summary = summarize_monitor(monitor_rows, target)

        csv_path = run_dir / "fast_lock_timeline.csv"
        png_path = run_dir / "fast_lock_timeline.png"
        write_csv(csv_path, events)
        plot_timeline(png_path, events, target)
        summary = {
            "ok": True,
            "run_dir": str(run_dir),
            "elapsed_s": time.time() - run_start,
            "arc_info": arc_info,
            "pc_info": pc_info,
            "capture": compact_capture(capture),
            "width_analysis": width_analysis,
            "lockpoint_analysis": lock_analysis,
            "target_ch1": target,
            "probe_center_out2": center,
            "p": float(get_value(args.base, "pid0.p")),
            "i": float(get_value(args.base, "pid0.i")),
            "selected_ival": float(get_value(args.base, "pid0.ival")),
            "monitor": monitor_summary,
            "prelock_plot_path": str(prelock_plot),
            "csv_path": str(csv_path),
            "png_path": str(png_path),
        }
        (run_dir / "fast_lock_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, indent=2), flush=True)
        return 0 if not monitor_summary.get("out2_saturated") else 2

    set_param(args.base, "asg0.output_direct", "off")
    set_param(args.base, "pid0.p", 0)
    set_param(args.base, "pid0.i", 0)
    set_param(args.base, "pid0.output_direct", "off")
    summary = {
        "ok": False,
        "run_dir": str(run_dir),
        "elapsed_s": time.time() - run_start,
        "arc_info": arc_info,
        "pc_info": pc_info,
        "capture": compact_capture(capture),
        "failure": failure,
        "prelock_plot_path": str(prelock_plot),
    }
    (run_dir / "fast_lock_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2), flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

