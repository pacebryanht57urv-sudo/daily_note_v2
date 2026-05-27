"""Tune TOPTICA ARC factor while keeping the selected dip centered near Out2=0.

This uses the platform-drop 1/4 definition:

    T_level = T_platform - 0.25 * (T_platform - T_min)

and tunes against the full width between the two crossings at that level.
Only two TOPTICA parameters are written:

    laser1.dl.pc.external_input.factor
    laser1.dl.pc.voltage_set
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
)


def read_pc(host: str) -> dict[str, float | bool]:
    with DLCpro(NetworkConnection(host)) as dlc:
        pc = dlc.laser1.dl.pc
        return {
            "voltage_set": float(pc.voltage_set.get()),
            "voltage_act": float(pc.voltage_act.get()),
            "voltage_min": float(pc.voltage_min.get()),
            "voltage_max": float(pc.voltage_max.get()),
            "pc_enabled": bool(pc.enabled.get()),
            "arc_factor": float(pc.external_input.factor.get()),
        }


def write_pc_voltage(host: str, value: float) -> dict[str, float | bool]:
    with DLCpro(NetworkConnection(host)) as dlc:
        pc = dlc.laser1.dl.pc
        vmin = float(pc.voltage_min.get())
        vmax = float(pc.voltage_max.get())
        clipped = max(vmin, min(vmax, float(value)))
        pc.voltage_set.set(clipped)
        time.sleep(0.5)
        return read_pc(host)


def write_arc_factor(host: str, value: float) -> dict[str, float | bool]:
    with DLCpro(NetworkConnection(host)) as dlc:
        pc = dlc.laser1.dl.pc
        pc.external_input.factor.set(float(value))
        time.sleep(0.4)
        return read_pc(host)


def capture(args: argparse.Namespace, tag: str) -> dict[str, object]:
    arc_info = read_arc_factor(args.host)
    pc_info = read_pc(args.host)
    configure_prelock_sweep(args.base, args.duration, args.sweep_frequency, args.sweep_amplitude)
    time.sleep(args.settle_seconds)
    scope = bridge_get(args.base, "/scope/single", {"tag": tag, "timeout": 8})
    if not scope.get("ok"):
        raise RuntimeError(scope)
    data = np.load(Path(scope["path"]))
    t = np.asarray(data["t"], dtype=float)
    ch1 = np.asarray(data["ch1"], dtype=float)
    ch2 = np.asarray(data["ch2"], dtype=float)
    analysis = analyze_apparent_width(t, ch1, ch2, args.depth_fraction)
    suggestion = {
        "action": "full_width_centering",
        "suggested_arc_factor": arc_info["arc_factor"],
    }
    plot_path = RESULTS_DIR / f"{tag}_arc_width.png"
    make_plot(plot_path, t, ch1, ch2, analysis, arc_info, suggestion)
    return {
        "tag": tag,
        "arc_info": arc_info,
        "pc_info": pc_info,
        "capture": scope,
        "analysis": analysis,
        "plot_path": str(plot_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7870")
    parser.add_argument("--host", default="192.168.1.104")
    parser.add_argument("--tag", default="fullwidth_center_arc")
    parser.add_argument("--sweep-frequency", type=float, default=50.0)
    parser.add_argument("--sweep-amplitude", type=float, default=0.5)
    parser.add_argument("--duration", type=float, default=0.067108864)
    parser.add_argument("--depth-fraction", type=float, default=0.75)
    parser.add_argument("--min-full-width", type=float, default=0.10)
    parser.add_argument("--target-full-width", type=float, default=0.12)
    parser.add_argument("--max-full-width", type=float, default=0.20)
    parser.add_argument("--center-tolerance", type=float, default=0.02)
    parser.add_argument("--min-arc", type=float, default=0.2)
    parser.add_argument("--max-arc", type=float, default=60.0)
    parser.add_argument("--max-fractional-step", type=float, default=0.25)
    parser.add_argument("--pc-center-gain", type=float, default=0.8)
    parser.add_argument("--max-iterations", type=int, default=10)
    parser.add_argument("--settle-seconds", type=float, default=0.5)
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_tag = f"{args.tag}_{stamp}"
    iterations = []
    final_status = "not_started"

    for index in range(args.max_iterations):
        tag = f"{run_tag}_iter{index + 1:02d}"
        result = capture(args, tag)
        iterations.append(result)
        analysis = result["analysis"]
        if not analysis.get("ok"):
            final_status = "analysis_failed"
            break

        chosen = analysis["chosen"]
        dip = float(chosen["min_sweep_voltage"])
        full_width = float(chosen["quarter_full_width_out2_v"])
        capture_width = float(chosen["capture_width_out2_v"])
        pc = result["pc_info"]
        arc = float(pc["arc_factor"])
        print(
            f"iter={index + 1} arc={arc:.6g} pc={float(pc['voltage_set']):.6g} "
            f"dip={dip:+.6f} full={full_width:.6f} capture={capture_width:.6f}",
            flush=True,
        )

        centered = abs(dip) <= args.center_tolerance
        wide_enough = full_width >= args.min_full_width
        not_too_wide = full_width <= args.max_full_width
        if centered and wide_enough and not_too_wide:
            final_status = "target_reached"
            break

        if not centered:
            # Resonance condition is approximately V_pc + ARC * V_out2 = const.
            # To move the dip to Out2=0, change V_pc by ARC * V_dip.
            requested_pc = float(pc["voltage_set"]) + args.pc_center_gain * arc * dip
            write = write_pc_voltage(args.host, requested_pc)
            result["write"] = {
                "param": "laser1.dl.pc.voltage_set",
                "requested": requested_pc,
                "readback": write,
                "reason": "center dip near Out2=0 before/after ARC adjustment",
            }
            print(f"  wrote PC voltage {requested_pc:.6g}", flush=True)
            final_status = "max_iterations_reached"
            continue

        if full_width < args.min_full_width:
            raw = arc * full_width / args.target_full_width
            lower = arc * (1.0 - args.max_fractional_step)
            requested_arc = max(raw, lower, args.min_arc)
            action = "decrease_arc_factor"
        else:
            raw = arc * full_width / args.target_full_width
            upper = arc * (1.0 + args.max_fractional_step)
            requested_arc = min(raw, upper, args.max_arc)
            action = "increase_arc_factor"
        write = write_arc_factor(args.host, requested_arc)
        result["write"] = {
            "param": "laser1.dl.pc.external_input.factor",
            "requested": requested_arc,
            "readback": write,
            "reason": action,
        }
        print(f"  wrote ARC factor {requested_arc:.6g}", flush=True)
        final_status = "max_iterations_reached"

    summary = {
        "ok": final_status == "target_reached",
        "mode": "full_width_arc_tune_with_pc_centering",
        "final_status": final_status,
        "final_pc": read_pc(args.host),
        "target": {
            "depth_fraction": args.depth_fraction,
            "platform_drop_fraction": 1.0 - args.depth_fraction,
            "min_full_width_out2_v": args.min_full_width,
            "target_full_width_out2_v": args.target_full_width,
            "max_full_width_out2_v": args.max_full_width,
            "center_tolerance_out2_v": args.center_tolerance,
        },
        "iterations": iterations,
    }
    summary_path = RESULTS_DIR / f"{run_tag}_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
