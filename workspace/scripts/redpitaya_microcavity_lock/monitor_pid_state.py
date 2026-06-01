"""Monitor the current PyRPL PID state through the live bridge."""

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


def set_param(base: str, param: str, value: object) -> None:
    result = bridge_get(base, "/set", {"param": param, "value": str(value)})
    if not result.get("ok"):
        raise RuntimeError(result)


def get_value(base: str, param: str) -> object:
    result = bridge_get(base, "/get", {"param": param})
    if not result.get("ok"):
        raise RuntimeError(result)
    return result["value"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7870")
    parser.add_argument("--tag", default="pid_monitor")
    parser.add_argument("--target", type=float, required=True)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--p", type=float)
    parser.add_argument("--i", type=float)
    parser.add_argument("--output-direct", default="out2")
    parser.add_argument("--safe-off", action="store_true")
    args = parser.parse_args()

    if args.p is not None:
        set_param(args.base, "pid0.p", args.p)
    if args.i is not None:
        set_param(args.base, "pid0.i", args.i)
    if args.output_direct:
        set_param(args.base, "pid0.output_direct", args.output_direct)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"{args.tag}_{stamp}"
    csv_path = RESULTS_DIR / f"{tag}.csv"
    png_path = RESULTS_DIR / f"{tag}.png"
    summary_path = RESULTS_DIR / f"{tag}_summary.json"

    rows = []
    start = time.time()
    while time.time() - start < args.seconds:
        row = {
            "t_s": time.time() - start,
            "ch1": float(get_value(args.base, "scope.voltage_in1")),
            "ch2": float(get_value(args.base, "scope.voltage_in2")),
            "p": float(get_value(args.base, "pid0.p")),
            "i_gain": float(get_value(args.base, "pid0.i")),
            "ival": float(get_value(args.base, "pid0.ival")),
            "output_direct": str(get_value(args.base, "pid0.output_direct")),
        }
        rows.append(row)
        print(
            f"t={row['t_s']:.1f}s ch1={row['ch1']:.6f} ch2={row['ch2']:+.6f} "
            f"ival={row['ival']:+.6f} p={row['p']:+.6g} i={row['i_gain']:+.6g}",
            flush=True,
        )
        time.sleep(args.interval)

    ch1 = [row["ch1"] for row in rows]
    ch2 = [row["ch2"] for row in rows]
    ival = [row["ival"] for row in rows]
    first_n = max(1, len(ch1) // 5)
    last_n = max(1, len(ch1) // 5)
    first_mean = statistics.fmean(ch1[:first_n])
    last_mean = statistics.fmean(ch1[-last_n:])
    summary = {
        "target": args.target,
        "seconds": args.seconds,
        "interval": args.interval,
        "n": len(rows),
        "ch1_mean": statistics.fmean(ch1),
        "ch1_min": min(ch1),
        "ch1_max": max(ch1),
        "ch1_ptp": max(ch1) - min(ch1),
        "ch1_std": statistics.pstdev(ch1) if len(ch1) > 1 else 0.0,
        "ch1_first_20pct_mean": first_mean,
        "ch1_last_20pct_mean": last_mean,
        "ch1_mean_drift_last_minus_first": last_mean - first_mean,
        "target_error_mean": statistics.fmean(ch1) - args.target,
        "target_error_last_20pct": last_mean - args.target,
        "ch2_mean": statistics.fmean(ch2),
        "ch2_ptp": max(ch2) - min(ch2),
        "ival_start": ival[0],
        "ival_end": ival[-1],
        "ival_drift": ival[-1] - ival[0],
        "p_readback": rows[-1]["p"],
        "i_readback": rows[-1]["i_gain"],
        "output_direct": rows[-1]["output_direct"],
        "csv_path": str(csv_path),
        "png_path": str(png_path),
    }

    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    axes[0].plot([row["t_s"] for row in rows], ch1, marker=".", lw=0.8)
    axes[0].axhline(args.target, color="tab:orange", ls="--", label="target")
    axes[0].set_ylabel("CH1 (V)")
    axes[0].grid(True, alpha=0.3)
    axes[0].legend(loc="best")
    axes[1].plot([row["t_s"] for row in rows], ch2, marker=".", lw=0.8, label="CH2")
    axes[1].plot([row["t_s"] for row in rows], ival, marker=".", lw=0.8, label="ival")
    axes[1].set_xlabel("Time (s)")
    axes[1].set_ylabel("Control (V)")
    axes[1].grid(True, alpha=0.3)
    axes[1].legend(loc="best")
    fig.suptitle(tag)
    fig.tight_layout()
    fig.savefig(png_path, dpi=160)
    plt.close(fig)

    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)

    if args.safe_off:
        set_param(args.base, "pid0.i", 0)
        set_param(args.base, "pid0.p", 0)
        set_param(args.base, "pid0.output_direct", "off")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
