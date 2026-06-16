"""Current-mode lock workflow for the Weiyuan Photonics laser controller.

This reuses the Red Pitaya/PyRPL sweep, dip analysis, PID handoff, and final
monitor logic from the TOPTICA workflow, but replaces PC/ARC centering with LD
set-current centering.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from lock_common import RESULTS_DIR, analyze_apparent_width, bridge_get, configure_prelock_sweep, set_param
from weiyuan_laser_adapter import WeiyuanLaser


def get_value(base: str, param: str) -> Any:
    result = bridge_get(base, "/get", {"param": param})
    if not result.get("ok"):
        raise RuntimeError(result)
    return result["value"]


def safe_off(base: str) -> None:
    for param, value in (
        ("pid0.p", 0),
        ("pid0.i", 0),
        ("pid0.output_direct", "off"),
        ("asg0.output_direct", "off"),
    ):
        try:
            set_param(base, param, value)
        except Exception as exc:
            print(f"WARN safe_off {param}: {exc}", flush=True)


def remove_temp_capture(path_text: str | None, tag_prefix: str) -> None:
    if not path_text:
        return
    path = Path(path_text)
    try:
        if path.exists() and path.name.startswith(tag_prefix):
            path.unlink()
    except Exception as exc:
        print(f"WARN could not remove temporary capture {path}: {exc}", flush=True)


def capture_prelock(args: argparse.Namespace, tag: str) -> dict[str, Any]:
    configure_prelock_sweep(
        args.base,
        args.duration,
        args.sweep_frequency,
        args.sweep_amplitude,
    )
    time.sleep(args.settle_seconds)
    capture = bridge_get(args.base, "/scope/single", {"tag": tag, "timeout": 8, "plot": "false"})
    if not capture.get("ok"):
        raise RuntimeError(capture)

    with np.load(Path(capture["path"])) as data:
        t = np.asarray(data["t"], dtype=float)
        ch1 = np.asarray(data["ch1"], dtype=float)
        ch2 = np.asarray(data["ch2"], dtype=float)

    if not args.keep_captures:
        remove_temp_capture(capture.get("path"), args.tag)
        remove_temp_capture(capture.get("plot_path"), args.tag)

    width = analyze_apparent_width(t, ch1, ch2, args.width_depth_fraction)
    lock = analyze_apparent_width(t, ch1, ch2, args.lock_depth_fraction)
    return {
        "ok": bool(width.get("ok") and lock.get("ok")),
        "width_analysis": width,
        "lockpoint_analysis": lock,
        "capture": {
            "path": capture.get("path") if args.keep_captures else None,
            "n": capture.get("n"),
            "input1": capture.get("input1"),
            "input2": capture.get("input2"),
        },
    }


def sample(base: str) -> dict[str, float]:
    return {
        "ch1": float(get_value(base, "scope.voltage_in1")),
        "ch2": float(get_value(base, "scope.voltage_in2")),
        "ival": float(get_value(base, "pid0.ival")),
        "p": float(get_value(base, "pid0.p")),
        "i": float(get_value(base, "pid0.i")),
    }


def monitor_live(base: str, seconds: float, interval: float, target: float) -> dict[str, Any]:
    rows: list[dict[str, float]] = []
    start = time.time()
    while time.time() - start < seconds:
        row = sample(base)
        row["t_s"] = time.time() - start
        rows.append(row)
        time.sleep(interval)

    if not rows:
        return {"ok": False, "n": 0}

    ch1 = np.asarray([row["ch1"] for row in rows], dtype=float)
    ch2 = np.asarray([row["ch2"] for row in rows], dtype=float)
    ival = np.asarray([row["ival"] for row in rows], dtype=float)
    return {
        "ok": True,
        "n": int(len(rows)),
        "ch1_first": float(ch1[0]),
        "ch1_last": float(ch1[-1]),
        "ch1_mean": float(np.mean(ch1)),
        "ch1_min": float(np.min(ch1)),
        "ch1_max": float(np.max(ch1)),
        "ch1_ptp": float(np.ptp(ch1)),
        "ch2_mean": float(np.mean(ch2)),
        "ch2_min": float(np.min(ch2)),
        "ch2_max": float(np.max(ch2)),
        "ch2_ptp": float(np.ptp(ch2)),
        "ival_mean": float(np.mean(ival)),
        "ival_min": float(np.min(ival)),
        "ival_max": float(np.max(ival)),
        "ival_ptp": float(np.ptp(ival)),
        "target": float(target),
        "target_error_mean": float(np.mean(ch1) - target),
        "out2_saturated": bool(np.any(np.abs(ch2) > args_saturation.out2_limit_v)),
        "ival_saturated": bool(np.any(np.abs(ival) > args_saturation.ival_limit_v)),
    }


class args_saturation:
    out2_limit_v = 0.98
    ival_limit_v = 3.8


def centered_current_step(args: argparse.Namespace, current_ma: float, dip_out2_v: float) -> float:
    direction = -1.0 if args.invert_current_centering else 1.0
    raw_delta = direction * args.current_center_gain_ma_per_v * dip_out2_v
    clipped_delta = max(-args.max_current_step_ma, min(args.max_current_step_ma, raw_delta))
    requested = current_ma + clipped_delta
    return max(args.min_current_ma, min(args.max_current_ma, requested))


def center_and_width(
    args: argparse.Namespace, run_tag: str, laser: WeiyuanLaser
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    steps: list[dict[str, Any]] = []
    final: dict[str, Any] = {}

    if not args.no_initialize_current:
        laser.set_current_ma(args.initial_current_ma)
        time.sleep(args.current_settle_seconds)
        steps.append({"stage": "set_initial_current", "requested_current_ma": args.initial_current_ma})

    for center_index in range(1, args.max_center_iterations + 1):
        status = laser.read_status()
        current_ma = float(status["ld_set_current_ma"])
        capture = capture_prelock(args, f"{run_tag}_c{center_index:02d}")
        width = capture["width_analysis"]
        lock = capture["lockpoint_analysis"]
        if not width.get("ok") or not lock.get("ok"):
            fallback_dip = width.get("min_sweep_voltage")
            if fallback_dip is None:
                raise RuntimeError({"stage": "capture", "width": width, "lock": lock, "steps": steps})
            requested_current = centered_current_step(args, current_ma, float(fallback_dip))
            steps.append(
                {
                    "stage": "capture_incomplete_crossing",
                    "center_iteration": center_index,
                    "current_set_ma": current_ma,
                    "dip_out2_v": float(fallback_dip),
                    "width_error": width.get("error"),
                    "lock_error": lock.get("error"),
                }
            )
            laser.set_current_ma(requested_current)
            time.sleep(args.current_settle_seconds)
            steps.append(
                {
                    "stage": "write_current_center_from_incomplete",
                    "previous_current_ma": current_ma,
                    "requested_current_ma": requested_current,
                    "dip_out2_v": float(fallback_dip),
                    "gain_ma_per_v": args.current_center_gain_ma_per_v,
                    "inverted": args.invert_current_centering,
                }
            )
            continue

        chosen = width["chosen"]
        full_width = float(chosen["quarter_full_width_out2_v"])
        dip = float(chosen["min_sweep_voltage"])
        target = float(lock["transmission_lock"])
        steps.append(
            {
                "stage": "capture",
                "center_iteration": center_index,
                "current_set_ma": current_ma,
                "current_actual_ma": status.get("ld_current_actual_ma"),
                "dip_out2_v": dip,
                "full_width_out2_v": full_width,
                "target_ch1_v": target,
            }
        )
        final = {
            "laser_status": status,
            "width_analysis": width,
            "lockpoint_analysis": lock,
        }

        if abs(dip) <= args.center_tolerance:
            if full_width < args.min_full_width:
                raise RuntimeError(
                    {
                        "stage": "width",
                        "reason": "below_lower_limit",
                        "full_width_out2_v": full_width,
                        "min_full_width_out2_v": args.min_full_width,
                        "steps": steps,
                    }
                )
            return final, steps

        requested_current = centered_current_step(args, current_ma, dip)
        laser.set_current_ma(requested_current)
        time.sleep(args.current_settle_seconds)
        steps.append(
            {
                "stage": "write_current_center",
                "previous_current_ma": current_ma,
                "requested_current_ma": requested_current,
                "dip_out2_v": dip,
                "gain_ma_per_v": args.current_center_gain_ma_per_v,
                "inverted": args.invert_current_centering,
            }
        )

    raise RuntimeError(
        {
            "stage": "center",
            "reason": "max_center_iterations_reached",
            "last_dip_out2_v": float(
                final.get("width_analysis", {}).get("chosen", {}).get("min_sweep_voltage", float("nan"))
            ),
            "steps": steps,
        }
    )


def pid_handoff(args: argparse.Namespace, target: float) -> tuple[float, list[dict[str, Any]]]:
    steps: list[dict[str, Any]] = []
    selected_sign = -1.0
    set_param(args.base, "asg0.output_direct", "off")
    set_param(args.base, "pid0.p", 0)
    set_param(args.base, "pid0.i", 0)
    set_param(args.base, "pid0.output_direct", "out2")
    set_param(args.base, "pid0.setpoint", target)
    set_param(args.base, "pid0.ival", args.initial_ival)
    set_param(args.base, "pid0.p", args.p)
    set_param(args.base, "pid0.i", selected_sign * abs(args.low_i))
    steps.append({"stage": "low_i_fixed_handoff", "i": selected_sign * abs(args.low_i)})
    set_param(args.base, "pid0.i", selected_sign * args.final_i_magnitude)
    return selected_sign, steps


def save_summary_if_requested(args: argparse.Namespace, run_tag: str, summary: dict[str, Any]) -> None:
    if not args.save_summary:
        return
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{run_tag}_weiyuan_current_mode_lock_summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_path"] = str(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7870")
    parser.add_argument("--laser-port", default="COM5")
    parser.add_argument("--slave", type=int, default=255)
    parser.add_argument("--tag", default="weiyuan_current_mode_lock")
    parser.add_argument("--sweep-frequency", type=float, default=50.0)
    parser.add_argument("--sweep-amplitude", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=0.067108864)
    parser.add_argument("--settle-seconds", type=float, default=0.5)
    parser.add_argument("--width-depth-fraction", type=float, default=0.75)
    parser.add_argument("--lock-depth-fraction", type=float, default=0.25)
    parser.add_argument("--min-full-width", type=float, default=0.08)
    parser.add_argument("--center-tolerance", type=float, default=0.03)
    parser.add_argument("--max-center-iterations", type=int, default=6)
    parser.add_argument("--initial-current-ma", type=float, default=260.0)
    parser.add_argument("--no-initialize-current", action="store_true")
    parser.add_argument("--current-center-gain-ma-per-v", type=float, default=2.0)
    parser.add_argument("--max-current-step-ma", type=float, default=1.5)
    parser.add_argument("--current-settle-seconds", type=float, default=0.7)
    parser.add_argument("--min-current-ma", type=float, default=0.0)
    parser.add_argument("--max-current-ma", type=float, default=500.0)
    parser.add_argument("--invert-current-centering", dest="invert_current_centering", action="store_true", default=True)
    parser.add_argument("--non-invert-current-centering", dest="invert_current_centering", action="store_false")
    parser.add_argument("--initial-ival", type=float, default=1.0)
    parser.add_argument("--p", type=float, default=0.01)
    parser.add_argument("--low-i", type=float, default=1.0)
    parser.add_argument("--final-i-magnitude", type=float, default=100.0)
    parser.add_argument("--final-monitor-seconds", type=float, default=2.0)
    parser.add_argument("--monitor-interval", type=float, default=0.05)
    parser.add_argument("--max-final-target-error-v", type=float, default=0.02)
    parser.add_argument("--keep-captures", action="store_true")
    parser.add_argument("--save-summary", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    run_tag = f"{args.tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    summary: dict[str, Any] = {"ok": False, "run_tag": run_tag, "steps": []}

    try:
        safe_off(args.base)
        with WeiyuanLaser(port=args.laser_port, slave=args.slave) as laser:
            summary["initial_laser_status"] = laser.read_status()
            final, center_steps = center_and_width(args, run_tag, laser)
            summary["steps"].extend(center_steps)
            target = float(final["lockpoint_analysis"]["transmission_lock"])

            selected_sign, handoff_steps = pid_handoff(args, target)
            summary["steps"].extend(handoff_steps)
            final_monitor = monitor_live(args.base, args.final_monitor_seconds, args.monitor_interval, target)
            summary["steps"].append(
                {
                    "stage": "final_i_monitor",
                    "i": selected_sign * args.final_i_magnitude,
                    "summary": final_monitor,
                }
            )

            final_ok = (
                bool(final_monitor.get("ok"))
                and not final_monitor.get("out2_saturated")
                and not final_monitor.get("ival_saturated")
                and abs(float(final_monitor.get("target_error_mean", float("inf"))))
                <= args.max_final_target_error_v
            )
            summary.update(
                {
                    "ok": final_ok,
                    "target_ch1_v": target,
                    "selected_i_sign": selected_sign,
                    "final_pid": sample(args.base),
                    "final_laser_status": laser.read_status(),
                }
            )
            if not final_ok:
                summary["failure"] = {"stage": "final_monitor", "summary": final_monitor}
                safe_off(args.base)
    except Exception as exc:
        detail = exc.args[0] if getattr(exc, "args", None) and isinstance(exc.args[0], dict) else None
        if detail and "steps" in detail:
            summary["steps"] = detail.pop("steps")
        summary["failure"] = {"stage": "exception", "error": repr(exc), "detail": detail}
        safe_off(args.base)
        save_summary_if_requested(args, run_tag, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return 1

    save_summary_if_requested(args, run_tag, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
