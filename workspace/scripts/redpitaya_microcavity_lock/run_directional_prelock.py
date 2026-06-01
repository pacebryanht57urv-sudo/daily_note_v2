"""Directional pre-lock approach for a narrow optical mode.

The routine intentionally keeps one voltage history: start from one side,
walk toward the mode, slow down when transmission drops, and stop before a
full next step would overshoot the intended 1/4-depth point.
"""

from __future__ import annotations

import argparse
import csv
import json
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


def get_value(base: str, param: str) -> object:
    result = bridge_get(base, "/get", {"param": param})
    if not result.get("ok"):
        raise RuntimeError(result)
    return result["value"]


def read_ch1_ch2(base: str, samples: int, delay: float) -> tuple[float, float]:
    ch1_values = []
    ch2_values = []
    for _ in range(samples):
        ch1_values.append(float(get_value(base, "scope.voltage_in1")))
        ch2_values.append(float(get_value(base, "scope.voltage_in2")))
        time.sleep(delay)
    return sum(ch1_values) / len(ch1_values), sum(ch2_values) / len(ch2_values)


def same_direction_candidate(current: float, candidate: float, direction: float) -> bool:
    return (candidate - current) * direction > 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7870")
    parser.add_argument("--tag", default="directional_prelock")
    parser.add_argument("--target", type=float, required=True)
    parser.add_argument("--start", type=float, default=0.5)
    parser.add_argument("--stop-limit", type=float, default=-0.25)
    parser.add_argument("--fast-step", type=float, default=-0.05)
    parser.add_argument("--slow-step", type=float, default=-0.01)
    parser.add_argument("--min-final-step", type=float, default=0.001)
    parser.add_argument("--drop-threshold", type=float, default=0.04)
    parser.add_argument("--target-margin", type=float, default=0.02)
    parser.add_argument(
        "--interpolate-final",
        action="store_true",
        help="Take one smaller same-direction step to the interpolated target before stopping.",
    )
    parser.add_argument("--fast-dwell", type=float, default=1.0)
    parser.add_argument("--slow-dwell", type=float, default=2.5)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--sample-delay", type=float, default=0.2)
    parser.add_argument("--prepare-p", type=float, default=0.0)
    parser.add_argument("--prepare-i", type=float, default=0.0)
    parser.add_argument("--safe-off", action="store_true")
    args = parser.parse_args()

    direction = 1.0 if args.fast_step > 0 else -1.0
    if args.slow_step * direction <= 0:
        raise ValueError("slow-step must point in the same direction as fast-step")
    if (args.stop_limit - args.start) * direction <= 0:
        raise ValueError("stop-limit must be ahead of start in the scan direction")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"{args.tag}_{stamp}"
    csv_path = RESULTS_DIR / f"{tag}.csv"
    png_path = RESULTS_DIR / f"{tag}.png"
    summary_path = RESULTS_DIR / f"{tag}_summary.json"

    rows: list[dict[str, object]] = []
    stop_reason = "stop limit reached"
    stage = "fast"
    baseline: float | None = None
    current = args.start
    final_row: dict[str, object] | None = None

    try:
        set_param(args.base, "asg0.output_direct", "off")
        set_param(args.base, "pid0.p", 0)
        set_param(args.base, "pid0.i", 0)
        set_param(args.base, "pid0.ival", current)
        set_param(args.base, "pid0.output_direct", "out2")

        while (args.stop_limit - current) * direction >= 0:
            dwell = args.fast_dwell if stage == "fast" else args.slow_dwell
            time.sleep(dwell)
            ch1, ch2 = read_ch1_ch2(args.base, args.samples, args.sample_delay)
            readback = float(get_value(args.base, "pid0.ival"))
            if baseline is None:
                baseline = ch1
            drop = baseline - ch1
            row: dict[str, object] = {
                "stage": stage,
                "requested_ival": current,
                "readback_ival": readback,
                "ch1_mean": ch1,
                "ch2_mean": ch2,
                "baseline": baseline,
                "drop_from_baseline": drop,
                "error_to_target": ch1 - args.target,
                "action": "measure",
            }
            rows.append(row)
            final_row = row
            print(
                f"{stage:>4} ival={current:+.5f} read={readback:+.5f} "
                f"ch1={ch1:.6f} drop={drop:.6f} err={ch1 - args.target:+.6f}",
                flush=True,
            )

            if stage == "fast" and drop >= args.drop_threshold:
                stage = "slow"
                row["action"] = "switch_to_slow"
                print("switch_to_slow", flush=True)

            if stage == "slow":
                if ch1 <= args.target + args.target_margin:
                    stop_reason = "current point is already within target margin"
                    row["action"] = "stop_near_target"
                    break
                if len(rows) >= 2:
                    prev = rows[-2]
                    prev_v = float(prev["readback_ival"])
                    prev_y = float(prev["ch1_mean"])
                    dv = readback - prev_v
                    dy = ch1 - prev_y
                    if abs(dv) > 1e-12:
                        slope = dy / dv
                        next_v = readback + args.slow_step
                        predicted_next_y = ch1 + slope * args.slow_step
                        row["slope_ch1_per_v"] = slope
                        row["predicted_next_ival"] = next_v
                        row["predicted_next_ch1"] = predicted_next_y
                        would_cross = (
                            ch1 > args.target
                            and predicted_next_y <= args.target + args.target_margin
                        )
                        if would_cross and slope != 0:
                            estimate = readback + (args.target - ch1) / slope
                            max_step = abs(args.slow_step)
                            step = estimate - readback
                            if (
                                args.interpolate_final
                                and
                                same_direction_candidate(readback, estimate, direction)
                                and abs(step) >= args.min_final_step
                                and abs(step) <= max_step
                            ):
                                set_param(args.base, "pid0.ival", estimate)
                                time.sleep(args.slow_dwell)
                                ch1_final, ch2_final = read_ch1_ch2(
                                    args.base, args.samples, args.sample_delay
                                )
                                final_readback = float(get_value(args.base, "pid0.ival"))
                                final_row = {
                                    "stage": "final_interp",
                                    "requested_ival": estimate,
                                    "readback_ival": final_readback,
                                    "ch1_mean": ch1_final,
                                    "ch2_mean": ch2_final,
                                    "baseline": baseline,
                                    "drop_from_baseline": baseline - ch1_final,
                                    "error_to_target": ch1_final - args.target,
                                    "action": "final_interpolated_step",
                                }
                                rows.append(final_row)
                                print(
                                    f"final_interp ival={estimate:+.5f} "
                                    f"read={final_readback:+.5f} "
                                    f"ch1={ch1_final:.6f} "
                                    f"err={ch1_final - args.target:+.6f}",
                                    flush=True,
                                )
                                stop_reason = "predicted next slow step would cross target"
                                break
                            stop_reason = "predicted next slow step would cross target"
                            row["action"] = "stop_before_crossing"
                            break

            step = args.fast_step if stage == "fast" else args.slow_step
            current = readback + step
            set_param(args.base, "pid0.ival", current)

        set_param(args.base, "pid0.setpoint", args.target)
        set_param(args.base, "pid0.p", args.prepare_p)
        set_param(args.base, "pid0.i", args.prepare_i)
        set_param(args.base, "pid0.output_direct", "out2")
    finally:
        if args.safe_off:
            set_param(args.base, "pid0.p", 0)
            set_param(args.base, "pid0.i", 0)
            set_param(args.base, "pid0.output_direct", "off")
            set_param(args.base, "asg0.output_direct", "off")

    with csv_path.open("w", newline="", encoding="utf-8") as file:
        fieldnames = sorted({key for row in rows for key in row.keys()})
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    fig, ax = plt.subplots(figsize=(9, 5))
    x = [float(row["readback_ival"]) for row in rows]
    y = [float(row["ch1_mean"]) for row in rows]
    ax.plot(x, y, "o-", label="CH1 mean")
    ax.axhline(args.target, color="tab:orange", linestyle="--", label="T_lock")
    ax.set_xlabel("pid0.ival readback / Out2 proxy (V)")
    ax.set_ylabel("CH1 mean (V)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best")
    fig.suptitle(f"Directional pre-lock approach: {stop_reason}")
    fig.tight_layout()
    fig.savefig(png_path, dpi=160)
    plt.close(fig)

    summary = {
        "target": args.target,
        "stop_reason": stop_reason,
        "final_row": final_row,
        "prepare_p": args.prepare_p,
        "prepare_i": args.prepare_i,
        "csv_path": str(csv_path),
        "png_path": str(png_path),
        "safe_off": args.safe_off,
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
