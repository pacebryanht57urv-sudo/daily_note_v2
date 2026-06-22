"""Continuous MDT693B coupling scan with asynchronous power readout.

This script is closer to the MDT69XB GUI function generator than the older
point-by-point optimizer.  One loop continuously writes a sine/triangle command
waveform at a fixed update rate, while another loop reads Red Pitaya optical
power as fast as the bridge allows.  After one scan pass, power timestamps are
matched to the commanded voltage trajectory, then the axis returns to the best
observed command voltage. By default, the restore approaches the best voltage
from the same scan direction where it was observed to reduce piezo hysteresis.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.parse import quote
from urllib.request import urlopen

from mdt693b import MDT693B


DEFAULT_EXPERIMENT_DIR = (
    Path(__file__).resolve().parents[2] / "experiments" / "auto_coupling_mdt693b"
)
DEFAULT_AXIS_ORDER = ["COM7:z", "COM7:y", "COM6:z", "COM6:x"]

POWER_RE = re.compile(
    r"P\s+(?P<kind>avg(?:\(\d+f\))?|inst)\s*:\s*(?P<body>[^\n\r]+)",
    re.IGNORECASE,
)
CHANNEL_POWER_RE = re.compile(
    r"(?P<channel>CH[12])\s+"
    r"(?P<value>[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*"
    r"(?P<unit>pW|nW|uW|mW|W)",
    re.IGNORECASE,
)
UNIT_TO_W = {"pw": 1e-12, "nw": 1e-9, "uw": 1e-6, "mw": 1e-3, "w": 1.0}


def parse_axis_token(text: str) -> tuple[str, str]:
    try:
        port, axis = text.split(":", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("axis token must look like COM7:z") from exc
    axis = axis.strip().lower()
    if axis not in {"x", "y", "z"}:
        raise argparse.ArgumentTypeError("axis must be x, y, or z")
    return port.strip().upper(), axis


def parse_ranges(text: str) -> list[float]:
    values = [float(item.strip()) for item in text.split(",") if item.strip()]
    if not values or any(value <= 0 for value in values):
        raise argparse.ArgumentTypeError("round ranges must be positive comma-separated values")
    return values


def parse_offset(text: str) -> tuple[str, str, float]:
    try:
        lhs, value_text = text.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("offset must look like COM7:z=0.24") from exc
    port, axis = parse_axis_token(lhs)
    return port, axis, float(value_text)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--axis-order",
        nargs="+",
        type=parse_axis_token,
        default=[parse_axis_token(item) for item in DEFAULT_AXIS_ORDER],
        help="Axes to scan. Default: COM7:z COM7:y COM6:z COM6:x.",
    )
    parser.add_argument("--round-ranges-v", type=parse_ranges, default=parse_ranges("8,2,0.6"))
    parser.add_argument("--scan-waveform", choices=["sine", "triangle"], default="sine")
    parser.add_argument(
        "--scan-clock",
        choices=["step", "time"],
        default="step",
        help=(
            "step advances waveform phase by one fixed sample per voltage write; "
            "time follows wall-clock phase and may skip points if serial writes lag."
        ),
    )
    parser.add_argument("--frequency-hz", type=float, default=0.5)
    parser.add_argument("--cycles", type=float, default=1.0)
    parser.add_argument("--update-rate-hz", type=float, default=50.0)
    parser.add_argument("--settle-before-read-s", type=float, default=0.1)
    parser.add_argument("--power-delay-s", type=float, default=0.0)
    parser.add_argument("--power-kind", choices=["inst", "avg"], default="inst")
    parser.add_argument("--channel", default="CH1", choices=["CH1", "CH2"])
    parser.add_argument("--bridge", default="http://127.0.0.1:7870")
    parser.add_argument("--min-v", type=float, default=0.0)
    parser.add_argument("--max-v", type=float, default=75.0)
    parser.add_argument("--readback-offset", action="append", type=parse_offset, default=[])
    parser.add_argument("--restore-mode", choices=["none", "direct", "coarse-fine"], default="coarse-fine")
    parser.add_argument(
        "--restore-approach",
        choices=["same-direction", "direct-path"],
        default="same-direction",
        help=(
            "same-direction first moves to the proper side of the best point, then "
            "approaches it from the scan direction where the best power was observed."
        ),
    )
    parser.add_argument("--restore-approach-margin-v", type=float, default=0.5)
    parser.add_argument("--restore-coarse-step-v", type=float, default=5.0)
    parser.add_argument("--restore-fine-step-v", type=float, default=0.5)
    parser.add_argument("--restore-fine-window-v", type=float, default=2.0)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_EXPERIMENT_DIR / "raw")
    parser.add_argument("--save-history", action="store_true")
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def parse_power_from_overlay(text: str, channel: str, power_kind: str) -> float:
    matches = list(POWER_RE.finditer(text))
    if not matches:
        raise ValueError(f"No power line found in overlay: {text!r}")
    preferred = [m for m in matches if m.group("kind").lower().startswith(power_kind)]
    ordered = preferred or matches
    for match in ordered:
        for item in CHANNEL_POWER_RE.finditer(match.group("body")):
            if item.group("channel").upper() == channel.upper():
                return float(item.group("value")) * UNIT_TO_W[item.group("unit").lower()]
    raise ValueError(f"No {channel} power found in overlay: {text!r}")


def read_overlay(bridge: str) -> str:
    url = f"{bridge.rstrip('/')}/get?param={quote('scope.active_means_v')}"
    with urlopen(url, timeout=3) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(payload)
    return str(payload["value"])


def waveform_value(kind: str, phase: float) -> float:
    if kind == "sine":
        return math.sin(2.0 * math.pi * phase)
    x = phase % 1.0
    if x < 0.25:
        return 4.0 * x
    if x < 0.75:
        return 2.0 - 4.0 * x
    return 4.0 * x - 4.0


def command_at_time(
    *,
    kind: str,
    center_v: float,
    amplitude_v: float,
    frequency_hz: float,
    elapsed_s: float,
) -> float:
    return center_v + amplitude_v * waveform_value(kind, elapsed_s * frequency_hz)


def nearest_command(commands: list[dict[str, float]], query_t: float) -> dict[str, float] | None:
    if not commands:
        return None
    lo = 0
    hi = len(commands) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if commands[mid]["t_s"] < query_t:
            lo = mid + 1
        else:
            hi = mid
    candidates = [commands[lo]]
    if lo > 0:
        candidates.append(commands[lo - 1])
    return min(candidates, key=lambda row: abs(row["t_s"] - query_t))


def command_direction_at(commands: list[dict[str, float]], query_t: float) -> int:
    """Return +1 for rising command voltage, -1 for falling, 0 if unknown."""
    if len(commands) < 2:
        return 0
    nearest_i = min(range(len(commands)), key=lambda idx: abs(commands[idx]["t_s"] - query_t))
    lo = max(0, nearest_i - 1)
    hi = min(len(commands) - 1, nearest_i + 1)
    delta = commands[hi]["commanded_readback_v"] - commands[lo]["commanded_readback_v"]
    if abs(delta) < 1e-9 and nearest_i > 0:
        delta = commands[nearest_i]["commanded_readback_v"] - commands[nearest_i - 1]["commanded_readback_v"]
    if abs(delta) < 1e-9 and nearest_i < len(commands) - 1:
        delta = commands[nearest_i + 1]["commanded_readback_v"] - commands[nearest_i]["commanded_readback_v"]
    if abs(delta) < 1e-9:
        return 0
    return 1 if delta > 0 else -1


def direction_label(direction: int) -> str:
    if direction > 0:
        return "rising"
    if direction < 0:
        return "falling"
    return "unknown"


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def coarse_fine_move(
    dev: MDT693B,
    *,
    axis: str,
    target_readback_v: float,
    readback_offset_v: float,
    args: argparse.Namespace,
    segment: str,
    force_fine: bool = False,
    approach_direction: int = 0,
    start_index: int = 0,
) -> list[dict[str, float | str | bool]]:
    rows: list[dict[str, float | str | bool]] = []
    coarse = max(0.05, args.restore_coarse_step_v)
    fine = max(0.02, args.restore_fine_step_v)
    fine_window = max(fine, args.restore_fine_window_v)
    tolerance = max(0.03, fine * 0.35)
    for idx in range(200):
        current = dev.read_axis_voltage(axis)
        remaining = target_readback_v - current
        if abs(remaining) <= tolerance:
            break
        if approach_direction and remaining * approach_direction <= 0:
            rows.append(
                {
                    "idx": start_index + idx + 1,
                    "restore_mode": "coarse-fine",
                    "restore_segment": segment,
                    "current_readback_v": current,
                    "target_readback_v": target_readback_v,
                    "readback_v": current,
                    "note": "already past target for requested approach direction",
                    "execute": bool(args.execute),
                }
            )
            break
        step_limit = fine if force_fine or abs(remaining) <= fine_window else coarse
        if approach_direction:
            step = approach_direction * min(abs(remaining), step_limit)
            desired = current + step
            if (target_readback_v - desired) * approach_direction < 0:
                desired = target_readback_v
        else:
            desired = current + math.copysign(min(abs(remaining), step_limit), remaining)
        desired = clamp(desired, args.min_v, args.max_v)
        command = desired - readback_offset_v
        if args.execute:
            dev.write_axis_voltage_blind(axis, command, min_v=args.min_v, max_v=args.max_v)
            time.sleep(0.03)
            readback = dev.read_axis_voltage(axis)
        else:
            readback = current
        rows.append(
            {
                "idx": start_index + idx + 1,
                "restore_mode": "coarse-fine",
                "restore_segment": segment,
                "current_readback_v": current,
                "desired_readback_v": desired,
                "target_readback_v": target_readback_v,
                "command_target_v": command,
                "readback_v": readback,
                "step_limit_v": step_limit,
                "approach_direction": direction_label(approach_direction),
                "execute": bool(args.execute),
            }
        )
        if not args.execute:
            break
    return rows


def restore_axis(
    dev: MDT693B,
    *,
    axis: str,
    target_readback_v: float,
    readback_offset_v: float,
    args: argparse.Namespace,
    approach_direction: int = 0,
) -> list[dict[str, float | str | bool]]:
    target_command_v = target_readback_v - readback_offset_v
    if args.restore_mode == "none":
        current = dev.read_axis_voltage(axis)
        return [
            {
                "restore_mode": "none",
                "restore_approach": "disabled",
                "current_readback_v": current,
                "target_readback_v": target_readback_v,
                "command_target_v": target_command_v,
                "readback_v": current,
                "note": "restore disabled; axis left at scan-end voltage",
                "execute": bool(args.execute),
            }
        ]
    if args.restore_mode == "direct":
        before = dev.read_axis_voltage(axis)
        if args.execute:
            dev.write_axis_voltage_blind(axis, target_command_v, min_v=args.min_v, max_v=args.max_v)
            time.sleep(0.05)
        after = dev.read_axis_voltage(axis)
        return [
            {
                "restore_mode": "direct",
                "restore_approach": "direct-path",
                "current_readback_v": before,
                "target_readback_v": target_readback_v,
                "command_target_v": target_command_v,
                "readback_v": after,
                "execute": bool(args.execute),
            }
        ]

    rows: list[dict[str, float | str | bool]] = []
    fine = max(0.02, args.restore_fine_step_v)
    tolerance = max(0.03, fine * 0.35)

    if args.restore_approach == "same-direction" and approach_direction:
        margin = max(fine, args.restore_approach_margin_v)
        pre_target = clamp(target_readback_v - approach_direction * margin, args.min_v, args.max_v)
        if abs(pre_target - target_readback_v) > tolerance:
            rows.extend(
                coarse_fine_move(
                    dev,
                    axis=axis,
                    target_readback_v=pre_target,
                    readback_offset_v=readback_offset_v,
                    args=args,
                    segment="preposition",
                    start_index=len(rows),
                )
            )
        current = dev.read_axis_voltage(axis)
        if (target_readback_v - current) * approach_direction <= tolerance:
            rows.append(
                {
                    "idx": len(rows) + 1,
                    "restore_mode": "coarse-fine",
                    "restore_segment": "same-direction-fallback",
                    "current_readback_v": current,
                    "target_readback_v": target_readback_v,
                    "approach_direction": direction_label(approach_direction),
                    "note": "preposition did not land on the requested approach side; falling back to direct-path restore",
                    "execute": bool(args.execute),
                }
            )
            rows.extend(
                coarse_fine_move(
                    dev,
                    axis=axis,
                    target_readback_v=target_readback_v,
                    readback_offset_v=readback_offset_v,
                    args=args,
                    segment="direct-path-fallback",
                    start_index=len(rows),
                )
            )
            return rows
        rows.extend(
            coarse_fine_move(
                dev,
                axis=axis,
                target_readback_v=target_readback_v,
                readback_offset_v=readback_offset_v,
                args=args,
                segment="same-direction-approach",
                force_fine=True,
                approach_direction=approach_direction,
                start_index=len(rows),
            )
        )
        return rows

    rows.extend(
        coarse_fine_move(
            dev,
            axis=axis,
            target_readback_v=target_readback_v,
            readback_offset_v=readback_offset_v,
            args=args,
            segment="direct-path",
        )
    )
    return rows


def scan_axis(
    args: argparse.Namespace,
    *,
    port: str,
    axis: str,
    range_v: float,
    readback_offset_v: float,
) -> dict[str, object]:
    with MDT693B(port) as dev:
        center_v = dev.read_axis_voltage(axis)
        amplitude_v = min(range_v, center_v - args.min_v, args.max_v - center_v)
        if amplitude_v <= 0:
            raise RuntimeError(f"{port}:{axis} has no voltage headroom around {center_v:g} V")

        stop_event = threading.Event()
        command_rows: list[dict[str, float]] = []
        power_rows: list[dict[str, object]] = []
        errors: list[str] = []
        t0 = time.perf_counter()
        duration_s = args.cycles / args.frequency_hz
        dt_s = 1.0 / args.update_rate_hz
        total_steps = max(2, int(round(duration_s * args.update_rate_hz)) + 1)

        def voltage_loop() -> None:
            next_t = t0
            try:
                if args.scan_clock == "step":
                    for step_i in range(total_steps):
                        elapsed = time.perf_counter() - t0
                        phase = args.cycles * step_i / (total_steps - 1)
                        command_readback_v = center_v + amplitude_v * waveform_value(
                            args.scan_waveform,
                            phase,
                        )
                        command_target_v = command_readback_v - readback_offset_v
                        if args.execute:
                            dev.write_axis_voltage_blind(
                                axis,
                                command_target_v,
                                min_v=args.min_v,
                                max_v=args.max_v,
                            )
                        command_rows.append(
                            {
                                "t_s": elapsed,
                                "commanded_readback_v": command_readback_v,
                                "command_target_v": command_target_v,
                                "command_phase": phase,
                                "command_step": float(step_i),
                            }
                        )
                        next_t = t0 + (step_i + 1) * dt_s
                        time.sleep(max(0.0, next_t - time.perf_counter()))
                    return

                while True:
                    now = time.perf_counter()
                    elapsed = now - t0
                    if elapsed > duration_s:
                        break
                    phase = elapsed * args.frequency_hz
                    command_readback_v = command_at_time(
                        kind=args.scan_waveform,
                        center_v=center_v,
                        amplitude_v=amplitude_v,
                        frequency_hz=args.frequency_hz,
                        elapsed_s=elapsed,
                    )
                    command_target_v = command_readback_v - readback_offset_v
                    if args.execute:
                        dev.write_axis_voltage_blind(
                            axis,
                            command_target_v,
                            min_v=args.min_v,
                            max_v=args.max_v,
                        )
                    command_rows.append(
                        {
                            "t_s": elapsed,
                            "commanded_readback_v": command_readback_v,
                            "command_target_v": command_target_v,
                            "command_phase": phase,
                        }
                    )
                    next_t += dt_s
                    time.sleep(max(0.0, next_t - time.perf_counter()))
            except Exception as exc:
                errors.append(f"voltage_loop: {type(exc).__name__}: {exc}")
            finally:
                stop_event.set()

        def power_loop() -> None:
            if args.settle_before_read_s > 0:
                time.sleep(args.settle_before_read_s)
            while not stop_event.is_set():
                try:
                    overlay = read_overlay(args.bridge)
                    power_w = parse_power_from_overlay(overlay, args.channel, args.power_kind)
                    power_rows.append(
                        {
                            "t_s": time.perf_counter() - t0,
                            "power_w": power_w,
                            "overlay": overlay,
                        }
                    )
                except Exception as exc:
                    errors.append(f"power_loop: {type(exc).__name__}: {exc}")
                    time.sleep(0.05)

        vt = threading.Thread(target=voltage_loop, name="mdt-voltage-loop", daemon=True)
        pt = threading.Thread(target=power_loop, name="rp-power-loop", daemon=True)
        vt.start()
        pt.start()
        vt.join()
        stop_event.set()
        pt.join(timeout=1.0)

        paired_rows: list[dict[str, object]] = []
        for prow in power_rows:
            command = nearest_command(command_rows, float(prow["t_s"]) - args.power_delay_s)
            if command is None:
                continue
            paired_rows.append({**prow, **{f"cmd_{k}": v for k, v in command.items()}})
        if not paired_rows:
            raise RuntimeError(f"No paired power rows for {port}:{axis}; errors={errors}")

        best = max(paired_rows, key=lambda row: float(row["power_w"]))
        best_direction = command_direction_at(command_rows, float(best["cmd_t_s"]))
        best["restore_approach_direction"] = direction_label(best_direction)
        print(
            f"{port}:{axis} {args.scan_waveform} range={range_v:.3f} V "
            f"best={float(best['cmd_commanded_readback_v']):.3f} V "
            f"power={float(best['power_w']) * 1e6:.3f} uW "
            f"direction={direction_label(best_direction)} "
            f"power_reads={len(power_rows)} command_updates={len(command_rows)}",
            flush=True,
        )
        restore_rows = restore_axis(
            dev,
            axis=axis,
            target_readback_v=float(best["cmd_commanded_readback_v"]),
            readback_offset_v=readback_offset_v,
            args=args,
            approach_direction=best_direction,
        )
        time.sleep(0.05)
        final_overlay = read_overlay(args.bridge)
        final_power_w = parse_power_from_overlay(final_overlay, args.channel, args.power_kind)
        final_readback_v = dev.read_axis_voltage(axis)

    return {
        "port": port,
        "axis": axis,
        "center_v": center_v,
        "range_v": range_v,
        "amplitude_v": amplitude_v,
        "waveform": args.scan_waveform,
        "scan_clock": args.scan_clock,
        "frequency_hz": args.frequency_hz,
        "cycles": args.cycles,
        "update_rate_hz": args.update_rate_hz,
        "planned_command_steps": total_steps,
        "duration_s": duration_s,
        "power_delay_s": args.power_delay_s,
        "power_kind": args.power_kind,
        "restore_approach": args.restore_approach,
        "restore_approach_direction": direction_label(best_direction),
        "command_rows": command_rows,
        "power_rows": power_rows,
        "paired_rows": paired_rows,
        "best": best,
        "restore_rows": restore_rows,
        "final_power_w": final_power_w,
        "final_readback_v": final_readback_v,
        "errors": errors,
    }


def read_all_voltages(axis_order: list[tuple[str, str]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for port in sorted({port for port, _axis in axis_order}):
        with MDT693B(port) as dev:
            port_values: dict[str, float] = {}
            for axis in ("x", "y", "z"):
                try:
                    port_values[axis] = dev.read_axis_voltage(axis)
                except Exception:
                    port_values[axis] = float("nan")
            out[port] = port_values
    return out


def main() -> int:
    args = parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    record_path = (
        args.out_dir / f"continuous_scan_opt_{timestamp}.json"
        if args.save_history
        else args.out_dir / "continuous_scan_opt_latest.json"
    )
    offsets = {(port, axis): value for port, axis, value in args.readback_offset}
    payload: dict[str, object] = {
        "timestamp": timestamp,
        "purpose": "continuous MDT693B scan coupling optimization",
        "execute": bool(args.execute),
        "axis_order": [f"{port}:{axis}" for port, axis in args.axis_order],
        "round_ranges_v": args.round_ranges_v,
        "scan_waveform": args.scan_waveform,
        "scan_clock": args.scan_clock,
        "frequency_hz": args.frequency_hz,
        "cycles": args.cycles,
        "update_rate_hz": args.update_rate_hz,
        "channel": args.channel,
        "bridge": args.bridge,
        "initial_voltages_v": read_all_voltages(args.axis_order),
        "rounds": [],
        "status": "started",
    }
    try:
        for round_index, range_v in enumerate(args.round_ranges_v, start=1):
            axes = []
            for port, axis in args.axis_order:
                axes.append(
                    scan_axis(
                        args,
                        port=port,
                        axis=axis,
                        range_v=range_v,
                        readback_offset_v=offsets.get((port, axis), 0.0),
                    )
                )
            payload["rounds"].append({"round_index": round_index, "range_v": range_v, "axes": axes})
        payload["status"] = "ok"
        payload["final_voltages_v"] = read_all_voltages(args.axis_order)
    except Exception as exc:
        payload["status"] = "error"
        payload["error_type"] = type(exc).__name__
        payload["error"] = str(exc)
        payload["error_state_voltages_v"] = read_all_voltages(args.axis_order)
        record_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"ERROR {type(exc).__name__}: {exc}\nSaved: {record_path}", flush=True)
        raise

    record_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Done. Saved: {record_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
