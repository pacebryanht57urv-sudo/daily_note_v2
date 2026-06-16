"""One-command current-mode lock for the Red Pitaya microcavity setup.

Default flow:
1. turn off PID/sweep outputs,
2. set TOPTICA PC piezo to 75 V,
3. sweep the current optical mode,
4. center the dip near Out2 = 0 using PC only,
5. if the apparent width is below the lower limit, apply a bounded ARC exception,
6. hand off to PID with ival=+1 V, test I sign, then raise |I| to 100,
7. print a 2 s live monitor summary without saving monitor files.
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

from lock_common import (
    RESULTS_DIR,
    analyze_apparent_width,
    bridge_get,
    configure_prelock_sweep,
    read_arc_factor as read_arc_factor_tcp,
    set_param,
    read_pc as read_pc_tcp,
    write_arc_factor as write_arc_factor_tcp,
    write_pc_voltage as write_pc_voltage_tcp,
)
from toptica_laser_adapter import (
    SerialTopticaSession,
    read_pc as read_pc_adapter,
    write_arc_factor as write_arc_factor_adapter,
    write_pc_voltage as write_pc_voltage_adapter,
)


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


def serial_session(args: argparse.Namespace) -> SerialTopticaSession | None:
    return getattr(args, "_serial_toptica_session", None)


def laser_read_pc(args: argparse.Namespace, *, full: bool = True) -> dict[str, Any]:
    session = serial_session(args)
    if session is not None:
        return dict(session.read_pc(full=full))
    if args.laser_connection == "serial":
        return dict(read_pc_adapter(connection="serial", host=args.host, port=args.laser_port))
    return dict(read_pc_tcp(args.host))


def laser_write_pc_voltage(
    args: argparse.Namespace, value: float, *, readback: str = "minimal"
) -> dict[str, Any]:
    session = serial_session(args)
    if session is not None:
        return dict(session.write_pc_voltage(value, readback=readback))
    if args.laser_connection == "serial":
        return dict(
            write_pc_voltage_adapter(
                connection="serial",
                host=args.host,
                port=args.laser_port,
                value=value,
            )
        )
    return dict(write_pc_voltage_tcp(args.host, value))


def laser_write_arc_factor(args: argparse.Namespace, value: float) -> dict[str, Any]:
    session = serial_session(args)
    if session is not None:
        return dict(session.write_arc_factor(value, readback="minimal"))
    if args.laser_connection == "serial":
        return dict(
            write_arc_factor_adapter(
                connection="serial",
                host=args.host,
                port=args.laser_port,
                value=value,
            )
        )
    return dict(write_arc_factor_tcp(args.host, value))


def laser_read_arc_factor(args: argparse.Namespace) -> dict[str, Any]:
    session = serial_session(args)
    if session is not None:
        return dict(session.read_arc_factor())
    if args.laser_connection == "serial":
        with SerialTopticaSession(port=args.laser_port, host=args.host) as serial:
            return dict(serial.read_arc_factor())
    return dict(read_arc_factor_tcp(args.host))


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


def monitor_live(
    base: str, seconds: float, interval: float, target: float
) -> dict[str, Any]:
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


def center_and_width(args: argparse.Namespace, run_tag: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    steps: list[dict[str, Any]] = []
    final: dict[str, Any] = {}

    for width_index in range(1, args.max_width_iterations + 1):
        centered = False
        for center_index in range(1, args.max_center_iterations + 1):
            pc_info = laser_read_pc(args, full=False)
            capture = capture_prelock(args, f"{run_tag}_w{width_index:02d}_c{center_index:02d}")
            width = capture["width_analysis"]
            lock = capture["lockpoint_analysis"]
            if not width.get("ok") or not lock.get("ok"):
                raise RuntimeError({"stage": "capture", "width": width, "lock": lock})

            chosen = width["chosen"]
            full_width = float(chosen["quarter_full_width_out2_v"])
            dip = float(chosen["min_sweep_voltage"])
            arc = float(pc_info["arc_factor"])
            target = float(lock["transmission_lock"])
            steps.append(
                {
                    "stage": "capture",
                    "width_iteration": width_index,
                    "center_iteration": center_index,
                    "pc_voltage_set": float(pc_info["voltage_set"]),
                    "arc_factor": arc,
                    "dip_out2_v": dip,
                    "full_width_out2_v": full_width,
                    "target_ch1_v": target,
                }
            )
            final = {"pc_info": pc_info, "width_analysis": width, "lockpoint_analysis": lock}

            if abs(dip) > args.center_tolerance:
                requested_pc = float(pc_info["voltage_set"]) + args.pc_center_gain * arc * dip
                readback = laser_write_pc_voltage(args, requested_pc, readback="none")
                steps.append(
                    {
                        "stage": "write_pc_center",
                        "requested_pc_v": requested_pc,
                        "readback": readback,
                    }
                )
                continue
            centered = True
            break

        if not centered:
            raise RuntimeError({"stage": "center", "reason": "max_center_iterations_reached"})

        full_width = float(final["width_analysis"]["chosen"]["quarter_full_width_out2_v"])
        if full_width >= args.min_full_width:
            return final, steps

        if not args.allow_arc_low_width:
            raise RuntimeError(
                {
                    "stage": "width",
                    "reason": "below_lower_limit",
                    "full_width_out2_v": full_width,
                    "min_full_width_out2_v": args.min_full_width,
                }
            )

        current_arc = float(final["pc_info"]["arc_factor"])
        raw_arc = current_arc * full_width / args.target_full_width
        bounded_arc = max(raw_arc, current_arc * (1.0 - args.max_arc_fractional_step), args.min_arc)
        readback = laser_write_arc_factor(args, bounded_arc)
        steps.append(
            {
                "stage": "write_arc_low_width_exception",
                "full_width_out2_v": full_width,
                "requested_arc_factor": bounded_arc,
                "readback": readback,
            }
        )

    raise RuntimeError(
        {
            "stage": "width",
            "reason": "still_below_lower_limit",
            "last_full_width_out2_v": float(
                final.get("width_analysis", {}).get("chosen", {}).get("quarter_full_width_out2_v", float("nan"))
            ),
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
    path = RESULTS_DIR / f"{run_tag}_current_mode_fast_lock_summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_path"] = str(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7870")
    parser.add_argument("--host", default="192.168.1.104")
    parser.add_argument("--laser-connection", choices=["tcp", "serial"], default="tcp")
    parser.add_argument("--laser-port", default="COM3")
    parser.add_argument("--tag", default="current_mode_fast_lock")
    parser.add_argument("--pc-start-v", type=float, default=75.0)
    parser.add_argument("--sweep-frequency", type=float, default=50.0)
    parser.add_argument("--sweep-amplitude", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=0.067108864)
    parser.add_argument("--settle-seconds", type=float, default=0.5)
    parser.add_argument("--width-depth-fraction", type=float, default=0.75)
    parser.add_argument("--lock-depth-fraction", type=float, default=0.25)
    parser.add_argument("--min-full-width", type=float, default=0.08)
    parser.add_argument("--target-full-width", type=float, default=0.10)
    parser.add_argument("--center-tolerance", type=float, default=0.03)
    parser.add_argument("--max-center-iterations", type=int, default=5)
    parser.add_argument("--max-width-iterations", type=int, default=3)
    parser.add_argument("--pc-center-gain", type=float, default=0.8)
    parser.add_argument("--allow-arc-low-width", dest="allow_arc_low_width", action="store_true", default=True)
    parser.add_argument("--no-arc-low-width", dest="allow_arc_low_width", action="store_false")
    parser.add_argument("--max-arc-fractional-step", type=float, default=0.5)
    parser.add_argument("--min-arc", type=float, default=0.2)
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

    session_cm = (
        SerialTopticaSession(port=args.laser_port, host=args.host)
        if args.laser_connection == "serial"
        else None
    )
    try:
        if session_cm is not None:
            args._serial_toptica_session = session_cm.__enter__()
            summary["serial_session"] = {"connection": "serial", "port": args.laser_port, "reused": True}

        try:
            safe_off(args.base)
            pc_start = laser_write_pc_voltage(args, args.pc_start_v)
            summary["steps"].append({"stage": "set_pc_start", "pc": pc_start})

            final, center_steps = center_and_width(args, run_tag)
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
                    "final_pc": laser_read_pc(args, full=True),
                    "final_arc": laser_read_arc_factor(args),
                }
            )
            if not final_ok:
                summary["failure"] = {"stage": "final_monitor", "summary": final_monitor}
                safe_off(args.base)
        finally:
            if session_cm is not None:
                session_cm.__exit__(None, None, None)
                if hasattr(args, "_serial_toptica_session"):
                    delattr(args, "_serial_toptica_session")
    except Exception as exc:
        summary["failure"] = {"stage": "exception", "error": repr(exc)}
        safe_off(args.base)
        save_summary_if_requested(args, run_tag, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return 1

    save_summary_if_requested(args, run_tag, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
