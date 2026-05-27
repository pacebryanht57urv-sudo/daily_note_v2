"""Slow dynamic search of PID ival through the local PyRPL bridge.

This script uses pid0 with p=i=0 as a DC output source on Out2, steps ival,
waits at each point, and samples scope.voltage_in1/scope.voltage_in2 through
the live bridge. It is meant for onsite pre-lock exploration, not final lock.
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


SESSION_DIR = Path(__file__).resolve().parents[1]
RESULTS_DIR = SESSION_DIR / "results" / "pyrpl_live_bridge"


def bridge_get(base: str, path: str, params: dict[str, object] | None = None) -> dict:
    url = base.rstrip("/") + path
    if params:
        url += "?" + urlencode(params)
    with urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def set_param(base: str, param: str, value: object) -> dict:
    return bridge_get(base, "/set", {"param": param, "value": str(value)})


def get_value(base: str, param: str) -> object:
    result = bridge_get(base, "/get", {"param": param})
    if not result.get("ok"):
        raise RuntimeError(result)
    return result["value"]


def frange(start: float, stop: float, step: float) -> list[float]:
    if step == 0:
        raise ValueError("step must be non-zero")
    if (stop - start) * step < 0:
        step = -step
    values = []
    value = start
    if step > 0:
        while value <= stop + abs(step) * 0.5:
            values.append(value)
            value += step
    else:
        while value >= stop - abs(step) * 0.5:
            values.append(value)
            value += step
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7870")
    parser.add_argument("--tag", default="dynamic_ival_search")
    parser.add_argument("--target", type=float, required=True)
    parser.add_argument("--start", type=float, default=0.5)
    parser.add_argument("--stop", type=float, default=-0.2)
    parser.add_argument("--step", type=float, default=-0.01)
    parser.add_argument("--dwell", type=float, default=1.0)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--sample-delay", type=float, default=0.2)
    parser.add_argument("--park-best", action="store_true")
    parser.add_argument("--safe-off", action="store_true")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"{args.tag}_{stamp}"
    csv_path = RESULTS_DIR / f"{tag}.csv"
    png_path = RESULTS_DIR / f"{tag}.png"
    summary_path = RESULTS_DIR / f"{tag}_summary.json"

    rows: list[dict[str, float]] = []
    best: dict[str, float] | None = None

    try:
        set_param(args.base, "asg0.output_direct", "off")
        set_param(args.base, "pid0.p", 0)
        set_param(args.base, "pid0.i", 0)
        set_param(args.base, "pid0.output_direct", "out2")

        for requested in frange(args.start, args.stop, args.step):
            set_param(args.base, "pid0.ival", requested)
            time.sleep(args.dwell)
            ch1_samples = []
            ch2_samples = []
            for _ in range(args.samples):
                ch1_samples.append(float(get_value(args.base, "scope.voltage_in1")))
                ch2_samples.append(float(get_value(args.base, "scope.voltage_in2")))
                time.sleep(args.sample_delay)
            ch1 = sum(ch1_samples) / len(ch1_samples)
            ch2 = sum(ch2_samples) / len(ch2_samples)
            err = ch1 - args.target
            row = {
                "requested_ival": requested,
                "readback_ival": float(get_value(args.base, "pid0.ival")),
                "ch1_mean": ch1,
                "ch2_mean": ch2,
                "error_to_target": err,
            }
            rows.append(row)
            if best is None or abs(err) < abs(best["error_to_target"]):
                best = row
            print(
                f"ival={requested:+.4f} read={row['readback_ival']:+.4f} "
                f"ch1={ch1:.6f} ch2={ch2:+.6f} err={err:+.6f}",
                flush=True,
            )
    finally:
        if best and args.park_best:
            set_param(args.base, "pid0.ival", best["readback_ival"])
            time.sleep(args.dwell)
        if args.safe_off:
            set_param(args.base, "pid0.output_direct", "off")
            set_param(args.base, "asg0.output_direct", "off")

    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "requested_ival",
                "readback_ival",
                "ch1_mean",
                "ch2_mean",
                "error_to_target",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    fig, ax1 = plt.subplots(figsize=(9, 5))
    x = [row["readback_ival"] for row in rows]
    y = [row["ch1_mean"] for row in rows]
    ax1.plot(x, y, "o-", label="CH1 mean")
    ax1.axhline(args.target, color="tab:orange", linestyle="--", label="target T_lock")
    ax1.set_xlabel("pid0.ival readback / Out2 proxy (V)")
    ax1.set_ylabel("CH1 mean (V)")
    ax1.grid(True, alpha=0.3)
    ax1.legend(loc="best")
    fig.suptitle("Slow dynamic pid0.ival pre-lock search")
    fig.tight_layout()
    fig.savefig(png_path, dpi=160)
    plt.close(fig)

    summary = {
        "target": args.target,
        "start": args.start,
        "stop": args.stop,
        "step": args.step,
        "dwell": args.dwell,
        "samples": args.samples,
        "sample_delay": args.sample_delay,
        "csv_path": str(csv_path),
        "png_path": str(png_path),
        "best": best,
        "min_ch1": min((row["ch1_mean"] for row in rows), default=None),
        "max_ch1": max((row["ch1_mean"] for row in rows), default=None),
        "crossed_target": any(row["ch1_mean"] <= args.target for row in rows),
        "safe_off": args.safe_off,
        "park_best": args.park_best,
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
