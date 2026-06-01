"""Directional pre-lock followed by conservative P/I tuning.

The script keeps the approach history intact: it first enters the mode from
one side, stops before the next step would cross the lock point, then tries
progressively smaller P gains until large CH1 oscillation disappears. After a
usable P is found, it tests small I gains with the same sign.
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

from data_paths import RESULTS_DIR


def bridge_get(base: str, path: str, params: dict[str, object] | None = None) -> dict:
    url = base.rstrip("/") + path
    if params:
        url += "?" + urlencode(params)
    with urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def set_param(base: str, param: str, value: object) -> dict:
    result = bridge_get(base, "/set", {"param": param, "value": str(value)})
    if not result.get("ok"):
        raise RuntimeError(result)
    return result


def get_value(base: str, param: str) -> float | str:
    result = bridge_get(base, "/get", {"param": param})
    if not result.get("ok"):
        raise RuntimeError(result)
    return result["value"]


def read_pair(base: str) -> tuple[float, float]:
    return float(get_value(base, "scope.voltage_in1")), float(
        get_value(base, "scope.voltage_in2")
    )


def monitor(base: str, seconds: float, interval: float) -> list[dict[str, float]]:
    rows = []
    start = time.time()
    index = 0
    while time.time() - start < seconds:
        ch1, ch2 = read_pair(base)
        rows.append(
            {
                "t_s": time.time() - start,
                "index": index,
                "ch1": ch1,
                "ch2": ch2,
                "ival": float(get_value(base, "pid0.ival")),
                "p": float(get_value(base, "pid0.p")),
                "i_gain": float(get_value(base, "pid0.i")),
            }
        )
        index += 1
        time.sleep(interval)
    return rows


def summarize_monitor(rows: list[dict[str, float]], target: float) -> dict[str, float | int | bool]:
    ch1 = [row["ch1"] for row in rows]
    ch2 = [row["ch2"] for row in rows]
    if not ch1:
        return {"ok": False}
    crossings = 0
    previous = ch1[0] - target
    for value in ch1[1:]:
        current = value - target
        if previous == 0 or current == 0 or previous * current < 0:
            crossings += 1
        previous = current
    mean = statistics.fmean(ch1)
    std = statistics.pstdev(ch1) if len(ch1) > 1 else 0.0
    ptp = max(ch1) - min(ch1)
    ch2_ptp = max(ch2) - min(ch2)
    platform_fraction = sum(value > target + 0.35 for value in ch1) / len(ch1)
    deep_fraction = sum(value < target - 0.12 for value in ch1) / len(ch1)
    oscillating = ptp > 0.28 or std > 0.12 or crossings >= 5
    mostly_platform = platform_fraction > 0.7
    return {
        "ok": True,
        "n": len(ch1),
        "ch1_mean": mean,
        "ch1_min": min(ch1),
        "ch1_max": max(ch1),
        "ch1_ptp": ptp,
        "ch1_std": std,
        "ch2_mean": statistics.fmean(ch2),
        "ch2_min": min(ch2),
        "ch2_max": max(ch2),
        "ch2_ptp": ch2_ptp,
        "target_error_mean": mean - target,
        "target_crossings": crossings,
        "platform_fraction": platform_fraction,
        "deep_fraction": deep_fraction,
        "oscillating": oscillating,
        "mostly_platform": mostly_platform,
    }


def approach_mode(args: argparse.Namespace) -> tuple[list[dict[str, object]], dict[str, object]]:
    direction = 1.0 if args.fast_step > 0 else -1.0
    rows: list[dict[str, object]] = []
    current = args.start
    stage = "fast"
    baseline: float | None = None

    set_param(args.base, "asg0.output_direct", "off")
    set_param(args.base, "pid0.p", 0)
    set_param(args.base, "pid0.i", 0)
    set_param(args.base, "pid0.ival", current)
    set_param(args.base, "pid0.output_direct", "out2")

    while (args.stop_limit - current) * direction >= 0:
        time.sleep(args.fast_dwell if stage == "fast" else args.slow_dwell)
        samples = []
        for _ in range(args.samples):
            samples.append(read_pair(args.base))
            time.sleep(args.sample_delay)
        ch1 = statistics.fmean(pair[0] for pair in samples)
        ch2 = statistics.fmean(pair[1] for pair in samples)
        readback = float(get_value(args.base, "pid0.ival"))
        if baseline is None:
            baseline = ch1
        row: dict[str, object] = {
            "phase": "approach",
            "stage": stage,
            "requested_ival": current,
            "readback_ival": readback,
            "ch1_mean": ch1,
            "ch2_mean": ch2,
            "baseline": baseline,
            "drop_from_baseline": baseline - ch1,
            "error_to_target": ch1 - args.target,
            "action": "measure",
        }
        rows.append(row)
        print(
            f"{stage:>4} ival={readback:+.5f} ch1={ch1:.6f} "
            f"err={ch1 - args.target:+.6f}",
            flush=True,
        )

        if stage == "fast" and baseline - ch1 >= args.drop_threshold:
            stage = "slow"
            row["action"] = "switch_to_slow"
            print("switch_to_slow", flush=True)

        if stage == "slow" and ch1 <= args.target + args.target_margin:
            row["action"] = "stop_near_or_below_target"
            return rows, {
                "stop_reason": "current slow point is near or below target",
                "final_ival": readback,
                "final_ch1": ch1,
                "final_ch2": ch2,
            }

        if stage == "slow" and len(rows) >= 2:
            prev = rows[-2]
            prev_v = float(prev["readback_ival"])
            prev_y = float(prev["ch1_mean"])
            dv = readback - prev_v
            dy = ch1 - prev_y
            if abs(dv) > 1e-12:
                slope = dy / dv
                predicted_next = ch1 + slope * args.slow_step
                row["slope_ch1_per_v"] = slope
                row["predicted_next_ch1"] = predicted_next
                if ch1 > args.target and predicted_next <= args.target + args.target_margin:
                    row["action"] = "stop_before_crossing"
                    return rows, {
                        "stop_reason": "predicted next slow step would cross target",
                        "final_ival": readback,
                        "final_ch1": ch1,
                        "final_ch2": ch2,
                    }

        step = args.fast_step if stage == "fast" else args.slow_step
        current = readback + step
        set_param(args.base, "pid0.ival", current)

    return rows, {
        "stop_reason": "stop limit reached",
        "final_ival": float(get_value(args.base, "pid0.ival")),
        "final_ch1": float(get_value(args.base, "scope.voltage_in1")),
        "final_ch2": float(get_value(args.base, "scope.voltage_in2")),
    }


def append_monitor_rows(
    all_rows: list[dict[str, object]],
    phase: str,
    setting: float,
    rows: list[dict[str, float]],
    summary: dict[str, object],
) -> None:
    for row in rows:
        merged: dict[str, object] = {"phase": phase, "setting": setting}
        merged.update(row)
        all_rows.append(merged)
    summary_row: dict[str, object] = {
        "phase": phase + "_summary",
        "setting": setting,
    }
    summary_row.update(summary)
    all_rows.append(summary_row)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7870")
    parser.add_argument("--tag", default="adaptive_pid_tune")
    parser.add_argument("--target", type=float, required=True)
    parser.add_argument("--start", type=float, default=0.5)
    parser.add_argument("--stop-limit", type=float, default=-0.22)
    parser.add_argument("--fast-step", type=float, default=-0.05)
    parser.add_argument("--slow-step", type=float, default=-0.01)
    parser.add_argument("--drop-threshold", type=float, default=0.03)
    parser.add_argument("--target-margin", type=float, default=0.02)
    parser.add_argument("--fast-dwell", type=float, default=1.0)
    parser.add_argument("--slow-dwell", type=float, default=2.5)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--sample-delay", type=float, default=0.2)
    parser.add_argument("--monitor-seconds", type=float, default=14.0)
    parser.add_argument("--monitor-interval", type=float, default=0.5)
    parser.add_argument("--p-list", default="-0.01,-0.005,-0.002,-0.001")
    parser.add_argument("--i-list", default="-0.0002,-0.0005,-0.001")
    parser.add_argument("--safe-off", action="store_true")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"{args.tag}_{stamp}"
    csv_path = RESULTS_DIR / f"{tag}.csv"
    png_path = RESULTS_DIR / f"{tag}.png"
    summary_path = RESULTS_DIR / f"{tag}_summary.json"

    all_rows: list[dict[str, object]] = []
    selected_p: float | None = None
    selected_i: float | None = None
    p_summaries = []
    i_summaries = []

    try:
        approach_rows, approach_summary = approach_mode(args)
        all_rows.extend(approach_rows)

        set_param(args.base, "pid0.setpoint", args.target)
        set_param(args.base, "pid0.i", 0)
        for p_gain in [float(item) for item in args.p_list.split(",") if item.strip()]:
            set_param(args.base, "pid0.p", p_gain)
            set_param(args.base, "pid0.i", 0)
            set_param(args.base, "pid0.output_direct", "out2")
            rows = monitor(args.base, args.monitor_seconds, args.monitor_interval)
            summary = summarize_monitor(rows, args.target)
            summary["p"] = p_gain
            p_summaries.append(summary)
            append_monitor_rows(all_rows, "p_test", p_gain, rows, summary)
            print(
                f"P={p_gain:+.6g} mean={summary['ch1_mean']:.4f} "
                f"ptp={summary['ch1_ptp']:.4f} std={summary['ch1_std']:.4f} "
                f"cross={summary['target_crossings']} "
                f"osc={summary['oscillating']} platform={summary['mostly_platform']}",
                flush=True,
            )
            if not summary["oscillating"]:
                selected_p = p_gain
                break

        if selected_p is not None:
            set_param(args.base, "pid0.p", selected_p)
            for i_gain in [float(item) for item in args.i_list.split(",") if item.strip()]:
                set_param(args.base, "pid0.i", i_gain)
                rows = monitor(args.base, args.monitor_seconds, args.monitor_interval)
                summary = summarize_monitor(rows, args.target)
                summary["p"] = selected_p
                summary["i"] = i_gain
                i_summaries.append(summary)
                append_monitor_rows(all_rows, "i_test", i_gain, rows, summary)
                print(
                    f"I={i_gain:+.6g} mean={summary['ch1_mean']:.4f} "
                    f"ptp={summary['ch1_ptp']:.4f} std={summary['ch1_std']:.4f} "
                    f"cross={summary['target_crossings']} "
                    f"osc={summary['oscillating']} platform={summary['mostly_platform']}",
                    flush=True,
                )
                if summary["oscillating"]:
                    set_param(args.base, "pid0.i", 0)
                    break
                selected_i = i_gain
    finally:
        if args.safe_off:
            set_param(args.base, "pid0.p", 0)
            set_param(args.base, "pid0.i", 0)
            set_param(args.base, "pid0.output_direct", "off")
            set_param(args.base, "asg0.output_direct", "off")

    fieldnames = sorted({key for row in all_rows for key in row.keys()})
    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    fig, ax = plt.subplots(figsize=(10, 5))
    for phase, marker in [("p_test", "o"), ("i_test", "s")]:
        rows = [row for row in all_rows if row.get("phase") == phase]
        if rows:
            ax.plot(
                [float(row["t_s"]) for row in rows],
                [float(row["ch1"]) for row in rows],
                marker,
                markersize=3,
                linestyle="None",
                label=phase,
            )
    ax.axhline(args.target, color="tab:orange", linestyle="--", label="T_lock")
    ax.set_xlabel("Monitor time within each gain test (s)")
    ax.set_ylabel("CH1 (V)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best")
    fig.suptitle("Adaptive P/I tuning after directional pre-lock")
    fig.tight_layout()
    fig.savefig(png_path, dpi=160)
    plt.close(fig)

    summary = {
        "target": args.target,
        "approach": approach_summary,
        "p_summaries": p_summaries,
        "i_summaries": i_summaries,
        "selected_p": selected_p,
        "selected_i": selected_i,
        "csv_path": str(csv_path),
        "png_path": str(png_path),
        "safe_off": args.safe_off,
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
