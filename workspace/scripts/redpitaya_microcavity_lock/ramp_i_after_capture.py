"""Short capture, then ramp integral gain gradually while monitoring."""

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

from data_paths import RESULTS_DIR


def bridge_get(base: str, path: str, params: dict[str, object] | None = None) -> dict:
    url = base.rstrip("/") + path
    if params:
        url += "?" + urlencode(params)
    with urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


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
        "i_gain": float(get_value(base, "pid0.i")),
        "ival": float(get_value(base, "pid0.ival")),
    }


def monitor(base: str, seconds: float, interval: float) -> list[dict[str, float]]:
    start = time.time()
    rows = []
    while time.time() - start < seconds:
        row = {"t_s": time.time() - start}
        row.update(sample(base))
        rows.append(row)
        time.sleep(interval)
    return rows


def summarize(rows: list[dict[str, float]], target: float) -> dict[str, float | bool | int]:
    ch1 = [row["ch1"] for row in rows]
    ch2 = [row["ch2"] for row in rows]
    if not ch1:
        return {"ok": False}
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
        "ival_start": rows[0]["ival"],
        "ival_end": rows[-1]["ival"],
        "ival_drift": rows[-1]["ival"] - rows[0]["ival"],
        "hit_platform": statistics.fmean(ch1) > target + 0.10,
        "too_deep": statistics.fmean(ch1) < target - 0.04,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7870")
    parser.add_argument("--tag", default="ramp_i_after_capture")
    parser.add_argument("--target", type=float, required=True)
    parser.add_argument("--capture-start", type=float, default=-0.03)
    parser.add_argument("--capture-stop", type=float, default=-0.16)
    parser.add_argument("--fast-step", type=float, default=-0.02)
    parser.add_argument("--slow-step", type=float, default=-0.005)
    parser.add_argument("--drop-threshold", type=float, default=0.01)
    parser.add_argument("--target-margin", type=float, default=0.006)
    parser.add_argument("--p", type=float, default=-0.002)
    parser.add_argument("--i-list", default="-0.004632,-0.01,-0.03,-0.1,-0.3,-1")
    parser.add_argument("--monitor-seconds", type=float, default=4.0)
    parser.add_argument("--interval", type=float, default=0.25)
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"{args.tag}_{stamp}"
    csv_path = RESULTS_DIR / f"{tag}.csv"
    png_path = RESULTS_DIR / f"{tag}.png"
    summary_path = RESULTS_DIR / f"{tag}_summary.json"

    all_rows: list[dict[str, object]] = []

    set_param(args.base, "pid0.i", 0)
    set_param(args.base, "pid0.p", 0)
    set_param(args.base, "pid0.output_direct", "off")
    set_param(args.base, "asg0.output_direct", "off")
    set_param(args.base, "pid0.ival", args.capture_start)
    set_param(args.base, "pid0.output_direct", "out2")

    baseline = None
    current = args.capture_start
    stage = "fast"
    capture_summary = {}
    while current >= args.capture_stop:
        time.sleep(0.25 if stage == "fast" else 0.6)
        vals = [sample(args.base) for _ in range(3)]
        ch1 = statistics.fmean(row["ch1"] for row in vals)
        ch2 = statistics.fmean(row["ch2"] for row in vals)
        readback = float(get_value(args.base, "pid0.ival"))
        if baseline is None:
            baseline = ch1
        action = "measure"
        row = {
            "phase": "capture",
            "stage": stage,
            "requested_ival": current,
            "readback_ival": readback,
            "ch1_mean": ch1,
            "ch2_mean": ch2,
            "drop_from_baseline": baseline - ch1,
            "error_to_target": ch1 - args.target,
            "action": action,
        }
        all_rows.append(row)
        print(
            f"{stage} ival={readback:+.5f} ch1={ch1:.6f} err={ch1 - args.target:+.6f}",
            flush=True,
        )
        if stage == "fast" and baseline - ch1 >= args.drop_threshold:
            stage = "slow"
            row["action"] = "switch_to_slow"
        if stage == "slow" and len([r for r in all_rows if r["phase"] == "capture"]) >= 2:
            previous_capture = [r for r in all_rows if r["phase"] == "capture"][-2]
            dv = readback - float(previous_capture["readback_ival"])
            dy = ch1 - float(previous_capture["ch1_mean"])
            if abs(dv) > 1e-12:
                predicted = ch1 + dy / dv * args.slow_step
                row["predicted_next_ch1"] = predicted
                if ch1 <= args.target + args.target_margin or (
                    ch1 > args.target and predicted <= args.target + args.target_margin
                ):
                    row["action"] = "capture_stop"
                    capture_summary = {
                        "final_ival": readback,
                        "final_ch1": ch1,
                        "final_ch2": ch2,
                    }
                    break
        step = args.fast_step if stage == "fast" else args.slow_step
        current = readback + step
        set_param(args.base, "pid0.ival", current)

    set_param(args.base, "pid0.setpoint", args.target)
    set_param(args.base, "pid0.p", args.p)
    set_param(args.base, "pid0.i", 0)
    set_param(args.base, "pid0.output_direct", "out2")

    p_rows = monitor(args.base, args.monitor_seconds, args.interval)
    p_summary = summarize(p_rows, args.target)
    for row in p_rows:
        merged = {"phase": "p_only", "setting": args.p}
        merged.update(row)
        all_rows.append(merged)
    print(
        f"P {args.p:+.6g} mean={p_summary['ch1_mean']:.6f} "
        f"ptp={p_summary['ch1_ptp']:.6f}",
        flush=True,
    )

    i_summaries = []
    selected_i = None
    for requested_i in [float(item) for item in args.i_list.split(",") if item.strip()]:
        readback_i = float(set_param(args.base, "pid0.i", requested_i))
        rows = monitor(args.base, args.monitor_seconds, args.interval)
        summary = summarize(rows, args.target)
        summary["requested_i"] = requested_i
        summary["readback_i"] = readback_i
        i_summaries.append(summary)
        for row in rows:
            merged = {"phase": "i_ramp", "setting": requested_i}
            merged.update(row)
            all_rows.append(merged)
        print(
            f"I req={requested_i:+.6g} read={readback_i:+.6g} "
            f"mean={summary['ch1_mean']:.6f} ptp={summary['ch1_ptp']:.6f} "
            f"err={summary['target_error_mean']:+.6f}",
            flush=True,
        )
        selected_i = readback_i
        if abs(float(summary["target_error_mean"])) <= 0.012 and float(summary["ch1_ptp"]) <= 0.08:
            break
        if summary["hit_platform"] or summary["too_deep"]:
            break

    fieldnames = sorted({key for row in all_rows for key in row.keys()})
    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    fig, ax = plt.subplots(figsize=(10, 5))
    for phase, marker in [("p_only", "o"), ("i_ramp", "s")]:
        rows = [row for row in all_rows if row.get("phase") == phase]
        if rows:
            ax.plot(
                [float(row["t_s"]) for row in rows],
                [float(row["ch1"]) for row in rows],
                marker,
                linestyle="None",
                markersize=3,
                label=phase,
            )
    ax.axhline(args.target, color="tab:orange", linestyle="--", label="target")
    ax.set_xlabel("Time within each monitor segment (s)")
    ax.set_ylabel("CH1 (V)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(png_path, dpi=160)
    plt.close(fig)

    summary = {
        "target": args.target,
        "capture": capture_summary,
        "p_summary": p_summary,
        "i_summaries": i_summaries,
        "selected_i": selected_i,
        "csv_path": str(csv_path),
        "png_path": str(png_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
