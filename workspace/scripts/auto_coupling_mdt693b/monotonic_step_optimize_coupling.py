"""Monotonic step coupling optimizer for MDT693B stages.

This optimizer avoids sine/triangle waveform timing.  For each axis it probes
one direction with discrete voltage steps.  If the power does not improve over
a coarse span, it returns to the starting voltage and scans the opposite
direction.  Once a peak is passed, it stops after the instantaneous power falls
below the round's initial power for several consecutive points, then returns to
the best voltage from the same direction where the best point was observed.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import quote
from urllib.request import urlopen

from mdt693b import MDT693B


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


def parse_float_list(text: str) -> list[float]:
    values = [float(item.strip()) for item in text.split(",") if item.strip()]
    if not values or any(value <= 0 for value in values):
        raise argparse.ArgumentTypeError("expected positive comma-separated values")
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
        help="Primary alignment axes to optimize. Default: COM7:z COM7:y COM6:z COM6:x.",
    )
    parser.add_argument(
        "--distance-axis-order",
        nargs="+",
        type=parse_axis_token,
        default=[],
        help="Optional distance axes optimized after primary axes, for example COM7:x COM6:y.",
    )
    parser.add_argument(
        "--round-steps-v",
        type=parse_float_list,
        default=parse_float_list("1,0.3,0.1"),
        help="Voltage step for each refinement round.",
    )
    parser.add_argument(
        "--round-max-travel-v",
        type=parse_float_list,
        default=parse_float_list("10,3,1"),
        help="Maximum one-direction travel for each round.",
    )
    parser.add_argument(
        "--distance-round-steps-v",
        type=parse_float_list,
        default=parse_float_list("2,0.6,0.2"),
        help="Voltage step for distance-axis refinement rounds.",
    )
    parser.add_argument(
        "--distance-round-max-travel-v",
        type=parse_float_list,
        default=parse_float_list("20,6,2"),
        help="Maximum one-direction travel for distance-axis rounds.",
    )
    parser.add_argument(
        "--final-polish",
        action="store_true",
        help="After primary/distance axes, run one extra fine confirmation pass.",
    )
    parser.add_argument(
        "--final-axis-order",
        nargs="+",
        type=parse_axis_token,
        default=[],
        help=(
            "Axes for the final polish pass. Default when --final-polish is set: "
            "primary axes followed by distance axes."
        ),
    )
    parser.add_argument(
        "--final-round-steps-v",
        type=parse_float_list,
        default=parse_float_list("0.05"),
        help="Voltage step for the final polish pass.",
    )
    parser.add_argument(
        "--final-round-max-travel-v",
        type=parse_float_list,
        default=parse_float_list("0.5"),
        help="Maximum one-direction travel for the final polish pass.",
    )
    parser.add_argument("--stop-below-start-count", type=int, default=3)
    parser.add_argument("--min-improvement-frac", type=float, default=0.03)
    parser.add_argument("--settle-s", type=float, default=0.08)
    parser.add_argument("--restore-approach-margin-v", type=float, default=0.5)
    parser.add_argument("--restore-coarse-step-v", type=float, default=3.0)
    parser.add_argument("--restore-fine-step-v", type=float, default=0.2)
    parser.add_argument("--bridge", default="http://127.0.0.1:7870")
    parser.add_argument("--channel", default="CH1", choices=["CH1", "CH2"])
    parser.add_argument("--power-kind", default="inst", choices=["inst", "avg"])
    parser.add_argument("--min-v", type=float, default=0.0)
    parser.add_argument("--max-v", type=float, default=75.0)
    parser.add_argument("--readback-offset", action="append", type=parse_offset, default=[])
    parser.add_argument("--record-path", type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def direction_label(direction: int) -> str:
    if direction > 0:
        return "positive"
    if direction < 0:
        return "negative"
    return "none"


def read_overlay(bridge: str) -> str:
    url = f"{bridge.rstrip('/')}/get?param={quote('scope.active_means_v')}"
    with urlopen(url, timeout=3) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(payload)
    return str(payload["value"])


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


def read_power_w(args: argparse.Namespace) -> tuple[float, str]:
    overlay = read_overlay(args.bridge)
    return parse_power_from_overlay(overlay, args.channel, args.power_kind), overlay


def command_axis(
    dev: MDT693B,
    axis: str,
    readback_v: float,
    readback_offset_v: float,
    args: argparse.Namespace,
) -> None:
    command_v = readback_v - readback_offset_v
    if args.execute:
        dev.write_axis_voltage_blind(axis, command_v, min_v=args.min_v, max_v=args.max_v)


def measure_at(
    dev: MDT693B,
    *,
    port: str,
    axis: str,
    readback_v: float,
    readback_offset_v: float,
    args: argparse.Namespace,
    round_index: int,
    direction: int,
    point_index: int,
    rows: list[dict[str, object]],
) -> dict[str, object]:
    readback_v = clamp(readback_v, args.min_v, args.max_v)
    t0 = time.perf_counter()
    command_axis(dev, axis, readback_v, readback_offset_v, args)
    time.sleep(max(0.0, args.settle_s))
    power_w, overlay = read_power_w(args)
    row = {
        "t_s": time.perf_counter() - t0,
        "port": port,
        "axis": axis,
        "round": round_index,
        "direction": direction_label(direction),
        "point_index": point_index,
        "readback_target_v": readback_v,
        "command_target_v": readback_v - readback_offset_v,
        "power_w": power_w,
        "overlay": overlay,
    }
    rows.append(row)
    return row


def direct_move(
    dev: MDT693B,
    *,
    axis: str,
    target_readback_v: float,
    readback_offset_v: float,
    step_v: float,
    args: argparse.Namespace,
) -> None:
    current = dev.read_axis_voltage(axis)
    target = clamp(target_readback_v, args.min_v, args.max_v)
    step_v = max(0.05, step_v)
    while abs(target - current) > step_v:
        current += math.copysign(step_v, target - current)
        command_axis(dev, axis, current, readback_offset_v, args)
        time.sleep(0.03)
    command_axis(dev, axis, target, readback_offset_v, args)
    time.sleep(0.05)


def approach_best_same_direction(
    dev: MDT693B,
    *,
    axis: str,
    best_readback_v: float,
    best_direction: int,
    readback_offset_v: float,
    args: argparse.Namespace,
) -> None:
    if best_direction == 0:
        direct_move(
            dev,
            axis=axis,
            target_readback_v=best_readback_v,
            readback_offset_v=readback_offset_v,
            step_v=args.restore_coarse_step_v,
            args=args,
        )
        return
    margin = max(args.restore_fine_step_v, args.restore_approach_margin_v)
    pre_target = clamp(best_readback_v - best_direction * margin, args.min_v, args.max_v)
    direct_move(
        dev,
        axis=axis,
        target_readback_v=pre_target,
        readback_offset_v=readback_offset_v,
        step_v=args.restore_coarse_step_v,
        args=args,
    )
    current = dev.read_axis_voltage(axis)
    fine_step = max(0.02, args.restore_fine_step_v)
    for _ in range(100):
        remaining = best_readback_v - current
        if abs(remaining) <= max(0.02, fine_step * 0.35):
            break
        if remaining * best_direction < 0:
            break
        current += best_direction * min(abs(remaining), fine_step)
        command_axis(dev, axis, current, readback_offset_v, args)
        time.sleep(0.03)


def scan_direction(
    dev: MDT693B,
    *,
    port: str,
    axis: str,
    start_v: float,
    start_power_w: float,
    direction: int,
    step_v: float,
    max_travel_v: float,
    readback_offset_v: float,
    args: argparse.Namespace,
    round_index: int,
    rows: list[dict[str, object]],
) -> tuple[dict[str, object], bool]:
    best: dict[str, object] = {
        "readback_target_v": start_v,
        "power_w": start_power_w,
        "direction": direction_label(direction),
    }
    below_start_count = 0
    point_index = 0
    travelled = 0.0
    current = start_v
    while travelled + step_v <= max_travel_v + 1e-9:
        next_v = current + direction * step_v
        if next_v < args.min_v or next_v > args.max_v:
            break
        travelled += step_v
        current = next_v
        point_index += 1
        row = measure_at(
            dev,
            port=port,
            axis=axis,
            readback_v=current,
            readback_offset_v=readback_offset_v,
            args=args,
            round_index=round_index,
            direction=direction,
            point_index=point_index,
            rows=rows,
        )
        if float(row["power_w"]) > float(best["power_w"]):
            best = row
        if float(row["power_w"]) < start_power_w:
            below_start_count += 1
        else:
            below_start_count = 0
        if below_start_count >= max(1, args.stop_below_start_count):
            return best, True
    return best, False


def optimize_axis(
    args: argparse.Namespace,
    *,
    port: str,
    axis: str,
    readback_offset_v: float,
    round_steps_v: list[float],
    round_max_travel_v: list[float],
    phase_label: str,
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    with MDT693B(port) as dev:
        current_v = dev.read_axis_voltage(axis)
        start_power_w, start_overlay = read_power_w(args)
        best = {
            "readback_target_v": current_v,
            "power_w": start_power_w,
            "direction": "none",
            "overlay": start_overlay,
        }
        initial_v = current_v
        initial_power_w = start_power_w

        for round_index, step_v in enumerate(round_steps_v, start=1):
            max_travel_v = round_max_travel_v[min(round_index - 1, len(round_max_travel_v) - 1)]
            round_start_v = dev.read_axis_voltage(axis)
            round_start_power_w, _overlay = read_power_w(args)
            improvement_gate = round_start_power_w * (1.0 + max(0.0, args.min_improvement_frac))

            pos_best, pos_stopped = scan_direction(
                dev,
                port=port,
                axis=axis,
                start_v=round_start_v,
                start_power_w=round_start_power_w,
                direction=1,
                step_v=step_v,
                max_travel_v=max_travel_v,
                readback_offset_v=readback_offset_v,
                args=args,
                round_index=round_index,
                rows=rows,
            )
            if float(pos_best["power_w"]) >= improvement_gate:
                round_best = pos_best
                round_direction = 1
                stopped = pos_stopped
            else:
                direct_move(
                    dev,
                    axis=axis,
                    target_readback_v=round_start_v,
                    readback_offset_v=readback_offset_v,
                    step_v=args.restore_coarse_step_v,
                    args=args,
                )
                neg_best, neg_stopped = scan_direction(
                    dev,
                    port=port,
                    axis=axis,
                    start_v=round_start_v,
                    start_power_w=round_start_power_w,
                    direction=-1,
                    step_v=step_v,
                    max_travel_v=max_travel_v,
                    readback_offset_v=readback_offset_v,
                    args=args,
                    round_index=round_index,
                    rows=rows,
                )
                round_best = neg_best if float(neg_best["power_w"]) > float(pos_best["power_w"]) else pos_best
                round_direction = -1 if round_best is neg_best else 1
                stopped = neg_stopped if round_best is neg_best else pos_stopped

            if float(round_best["power_w"]) > float(best["power_w"]):
                best = round_best
            approach_best_same_direction(
                dev,
                axis=axis,
                best_readback_v=float(round_best["readback_target_v"]),
                best_direction=round_direction,
                readback_offset_v=readback_offset_v,
                args=args,
            )
            print(
                f"{port}:{axis} {phase_label} round {round_index} step={step_v:g} V "
                f"best={float(round_best['readback_target_v']):.3f} V "
                f"power={float(round_best['power_w']) * 1e6:.3f} uW "
                f"direction={direction_label(round_direction)} stopped={stopped}",
                flush=True,
            )

        final_v = dev.read_axis_voltage(axis)
        final_power_w, final_overlay = read_power_w(args)

    return {
        "port": port,
        "axis": axis,
        "phase": phase_label,
        "initial_readback_v": initial_v,
        "initial_power_w": initial_power_w,
        "best": best,
        "final_readback_v": final_v,
        "final_power_w": final_power_w,
        "final_overlay": final_overlay,
        "rows": rows,
    }


def main() -> int:
    args = parse_args()
    offsets = {(port, axis): offset for port, axis, offset in args.readback_offset}
    results = []
    schedule = [
        ("primary", args.axis_order, args.round_steps_v, args.round_max_travel_v),
        ("distance", args.distance_axis_order, args.distance_round_steps_v, args.distance_round_max_travel_v),
    ]
    if args.final_polish:
        final_axis_order = args.final_axis_order or (args.axis_order + args.distance_axis_order)
        schedule.append(
            (
                "final-polish",
                final_axis_order,
                args.final_round_steps_v,
                args.final_round_max_travel_v,
            )
        )
    for phase_label, axis_order, round_steps_v, round_max_travel_v in schedule:
        if not axis_order:
            continue
        print(
            f"=== {phase_label} axes: "
            + " ".join(f"{port}:{axis}" for port, axis in axis_order)
            + f" | steps={round_steps_v} max_travel={round_max_travel_v} ===",
            flush=True,
        )
        for port, axis in axis_order:
            offset = offsets.get((port, axis), 0.0)
            result = optimize_axis(
                args,
                port=port,
                axis=axis,
                readback_offset_v=offset,
                round_steps_v=round_steps_v,
                round_max_travel_v=round_max_travel_v,
                phase_label=phase_label,
            )
            results.append(result)
            print(
                f"{port}:{axis} final={result['final_readback_v']:.3f} V "
                f"power={result['final_power_w'] * 1e6:.3f} uW",
                flush=True,
            )
    if args.record_path:
        args.record_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "args": {
                key: str(value) if isinstance(value, Path) else value
                for key, value in vars(args).items()
                if key != "record_path"
            },
            "results": results,
        }
        args.record_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"record: {args.record_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
