"""Closed-loop ARC factor pre-tuning for TOPTICA DLC pro.

Only the DLC pro ARC factor is writable here:

    laser1.dl.pc.external_input.factor

Each iteration captures a Red Pitaya pre-lock sweep, estimates the
platform-drop 1/4 to dip capture distance on the Out2 axis, and changes ARC
factor by a bounded fractional step until the distance enters the target range.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from toptica.lasersdk.dlcpro.v2_5_3 import DLCpro, NetworkConnection

from suggest_arc_factor import (
    RESULTS_DIR,
    analyze_apparent_width,
    bridge_get,
    configure_prelock_sweep,
    make_plot,
    read_arc_factor,
    suggest_arc_factor,
)


def write_arc_factor(host: str, value: float) -> float:
    with DLCpro(NetworkConnection(host)) as dlc:
        dlc.laser1.dl.pc.external_input.factor.set(float(value))
        time.sleep(0.25)
        return float(dlc.laser1.dl.pc.external_input.factor.get())


def capture_and_analyze(args: argparse.Namespace, tag: str) -> dict[str, object]:
    arc_info = read_arc_factor(args.host)
    configure_prelock_sweep(args.base, args.duration, args.sweep_frequency, args.sweep_amplitude)
    time.sleep(args.settle_seconds)
    capture = bridge_get(args.base, "/scope/single", {"tag": tag, "timeout": 8})
    if not capture.get("ok"):
        raise RuntimeError(capture)
    data = np.load(Path(capture["path"]))
    t = np.asarray(data["t"], dtype=float)
    ch1 = np.asarray(data["ch1"], dtype=float)
    ch2 = np.asarray(data["ch2"], dtype=float)
    analysis = analyze_apparent_width(t, ch1, ch2, args.depth_fraction)
    suggestion = None
    if analysis.get("ok"):
        suggestion = suggest_arc_factor(
            current_arc=float(arc_info["arc_factor"]),
            capture_width=float(analysis["chosen"]["capture_width_out2_v"]),
            min_width=args.min_width,
            max_width=args.max_width,
            min_arc=args.min_arc,
            max_arc=args.max_arc,
            max_fractional_step=args.max_fractional_step,
        )
    plot_path = RESULTS_DIR / f"{tag}_arc_width.png"
    make_plot(plot_path, t, ch1, ch2, analysis, arc_info, suggestion)
    return {
        "arc_info": arc_info,
        "capture": capture,
        "analysis": analysis,
        "suggestion": suggestion,
        "plot_path": str(plot_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7870")
    parser.add_argument("--host", default="192.168.1.104")
    parser.add_argument("--tag", default="arc_factor_autotune")
    parser.add_argument("--sweep-frequency", type=float, default=50.0)
    parser.add_argument("--sweep-amplitude", type=float, default=0.5)
    parser.add_argument("--duration", type=float, default=0.067108864)
    parser.add_argument("--depth-fraction", type=float, default=0.75)
    parser.add_argument("--min-width", type=float, default=0.02)
    parser.add_argument("--max-width", type=float, default=0.12)
    parser.add_argument("--min-arc", type=float, default=1.0)
    parser.add_argument("--max-arc", type=float, default=60.0)
    parser.add_argument("--max-fractional-step", type=float, default=0.25)
    parser.add_argument("--max-iterations", type=int, default=6)
    parser.add_argument("--settle-seconds", type=float, default=0.4)
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_tag = f"{args.tag}_{stamp}"

    iterations = []
    final_status = "not_started"
    for index in range(args.max_iterations):
        tag = f"{run_tag}_iter{index + 1:02d}"
        result = capture_and_analyze(args, tag)
        analysis = result["analysis"]
        suggestion = result["suggestion"]
        iteration = {
            "index": index + 1,
            "tag": tag,
            **result,
        }
        iterations.append(iteration)

        if not analysis.get("ok"):
            final_status = "analysis_failed"
            break
        width = float(analysis["chosen"]["capture_width_out2_v"])
        current_arc = float(result["arc_info"]["arc_factor"])
        action = suggestion["action"] if suggestion else "no_suggestion"
        print(
            f"iter={index + 1} arc={current_arc:.6g} capture_width={width:.6g} action={action}",
            flush=True,
        )
        if action == "keep_arc_factor":
            final_status = "target_width_reached"
            break
        suggested = float(suggestion["suggested_arc_factor"])
        if abs(suggested - current_arc) < 1e-9:
            final_status = "suggestion_clipped_no_change"
            break
        readback = write_arc_factor(args.host, suggested)
        iteration["write"] = {
            "param": "laser1.dl.pc.external_input.factor",
            "requested": suggested,
            "readback": readback,
        }
        print(f"  wrote ARC factor {suggested:.6g}, readback={readback:.6g}", flush=True)
        final_status = "max_iterations_reached"

    final_arc_info = read_arc_factor(args.host)
    summary = {
        "ok": final_status == "target_width_reached",
        "mode": "arc_factor_autotune",
        "final_status": final_status,
        "final_arc_info": final_arc_info,
        "target_range": {
            "min_capture_width_out2_v": args.min_width,
            "max_capture_width_out2_v": args.max_width,
            "depth_fraction": args.depth_fraction,
        },
        "iterations": iterations,
    }
    summary_path = RESULTS_DIR / f"{run_tag}_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
