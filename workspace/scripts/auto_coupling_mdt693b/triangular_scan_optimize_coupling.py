"""Coarse-to-fine voltage scans for MDT693B coupling optimization.

The workflow assumes the user has already manually found visible coupling.
It then scans one axis at a time with a fast stair-step sine or triangular sweep,
records Red Pitaya scope optical power during the sweep, moves to the best
observed command point, and shrinks the scan range on later rounds.  During the
sweep it does not read or judge each MDT voltage point; it behaves more like a
manual sweep where optical power is inspected after one continuous pass.

The script is dry-run by default.  Add --execute only when the probes are in a
safe local-optimization state.
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


DEFAULT_EXPERIMENT_DIR = (
    Path(__file__).resolve().parents[2] / "experiments" / "auto_coupling_mdt693b"
)
DEFAULT_AXIS_ORDER = ["COM7:z", "COM7:y", "COM7:x", "COM6:z", "COM6:x", "COM6:y"]

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
        raise argparse.ArgumentTypeError("axis token must look like COM7:x") from exc
    axis = axis.strip().lower()
    if axis not in {"x", "y", "z"}:
        raise argparse.ArgumentTypeError("axis must be x, y, or z")
    return port.strip().upper(), axis


def parse_offset(text: str) -> tuple[str, str, float]:
    try:
        lhs, value_text = text.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("offset must look like COM7:x=0.24") from exc
    port, axis = parse_axis_token(lhs)
    try:
        value = float(value_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("offset must be numeric") from exc
    return port, axis, value


def parse_ranges(text: str) -> list[float]:
    values = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        value = float(item)
        if value <= 0:
            raise argparse.ArgumentTypeError("round ranges must be positive")
        values.append(value)
    if not values:
        raise argparse.ArgumentTypeError("at least one range is required")
    return values


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--axis-order",
        nargs="+",
        type=parse_axis_token,
        default=[parse_axis_token(item) for item in DEFAULT_AXIS_ORDER],
        help=(
            "Axis order. Default: right/input COM7 z/y/x, then left/output "
            "COM6 z/x/y."
        ),
    )
    parser.add_argument(
        "--round-ranges-v",
        type=parse_ranges,
        default=parse_ranges("5.0,2.0,0.8,0.3"),
        help="Comma-separated half-ranges for successive rounds.",
    )
    parser.add_argument(
        "--scan-waveform",
        choices=["sine", "triangular"],
        default="sine",
        help="Command waveform for each axis scan. Sine is smoother at turning points.",
    )
    parser.add_argument("--points-per-half", type=int, default=12)
    parser.add_argument("--channel", default="CH1", choices=["CH1", "CH2"])
    parser.add_argument("--bridge", default="http://127.0.0.1:7870")
    parser.add_argument("--min-v", type=float, default=0.0)
    parser.add_argument("--max-v", type=float, default=75.0)
    parser.add_argument("--max-step-v", type=float, default=1.0)
    parser.add_argument(
        "--restore-mode",
        choices=["coarse-fine", "ramp", "direct"],
        default="coarse-fine",
        help=(
            "How to move back to the best point after a scan. coarse-fine uses "
            "large steps far away and small steps near the target; ramp is the "
            "old uniform safe ramp; direct jumps in one command."
        ),
    )
    parser.add_argument(
        "--restore-coarse-step-v",
        type=float,
        default=5.0,
        help="Large step size for coarse-fine restore.",
    )
    parser.add_argument(
        "--restore-fine-step-v",
        type=float,
        default=0.5,
        help="Fine step size for coarse-fine restore near the target.",
    )
    parser.add_argument(
        "--restore-fine-window-v",
        type=float,
        default=2.0,
        help="Use fine restore steps when this close to the target.",
    )
    parser.add_argument("--settle-s", type=float, default=0.03)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--sample-interval-s", type=float, default=0.05)
    parser.add_argument("--readback-offset", action="append", type=parse_offset, default=[])
    parser.add_argument(
        "--abort-below-frac",
        type=float,
        default=0.08,
        help="Abort if power falls below this fraction of the initial power.",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_EXPERIMENT_DIR / "raw")
    parser.add_argument(
        "--save-history",
        action="store_true",
        help="Save a timestamped JSON record. By default only latest.json is overwritten.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every scan point. By default only compact per-axis summaries are printed.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually write voltages. Without this flag, only records a dry-run plan.",
    )
    return parser.parse_args()


def parse_power_from_overlay(text: str, channel: str) -> float:
    matches = list(POWER_RE.finditer(text))
    if not matches:
        raise ValueError(f"No P inst/P avg line found in overlay: {text!r}")
    matches.sort(key=lambda m: 0 if m.group("kind").lower().startswith("avg") else 1)
    for match in matches:
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


def measure_power(args: argparse.Namespace) -> tuple[float, list[float], str]:
    values: list[float] = []
    last_overlay = ""
    for idx in range(max(1, args.samples)):
        last_overlay = read_overlay(args.bridge)
        values.append(parse_power_from_overlay(last_overlay, args.channel))
        if idx < args.samples - 1:
            time.sleep(args.sample_interval_s)
    finite = [value for value in values if math.isfinite(value)]
    if not finite:
        raise RuntimeError("No finite power readout")
    return sum(finite) / len(finite), values, last_overlay


def linspace(start: float, stop: float, n: int) -> list[float]:
    if n <= 1:
        return [float(stop)]
    return [start + (stop - start) * idx / (n - 1) for idx in range(n)]


def triangular_points(center: float, lower: float, upper: float, points_per_half: int) -> list[float]:
    """Return center -> lower -> upper -> lower, with duplicate neighbors removed."""
    n = max(2, int(points_per_half) + 1)
    segments = [
        linspace(center, lower, n),
        linspace(lower, upper, 2 * n - 1),
        linspace(upper, lower, 2 * n - 1),
    ]
    points: list[float] = []
    for segment in segments:
        for value in segment:
            clipped = float(value)
            if points and abs(points[-1] - clipped) < 1e-9:
                continue
            points.append(clipped)
    return points


def sine_points(center: float, lower: float, upper: float, points_per_half: int) -> list[float]:
    """Return one smooth center -> upper -> center -> lower -> center sine cycle."""
    amplitude = min(abs(center - lower), abs(upper - center))
    if amplitude <= 0:
        return [float(center)]
    n = max(9, int(points_per_half) * 4 + 1)
    points: list[float] = []
    for idx in range(n):
        theta = 2.0 * math.pi * idx / (n - 1)
        value = center + amplitude * math.sin(theta)
        clipped = max(lower, min(upper, value))
        if points and abs(points[-1] - clipped) < 1e-9:
            continue
        points.append(float(clipped))
    return points


def scan_points(
    center: float,
    lower: float,
    upper: float,
    points_per_half: int,
    waveform: str,
) -> list[float]:
    if waveform == "sine":
        return sine_points(center, lower, upper, points_per_half)
    return triangular_points(center, lower, upper, points_per_half)


def expand_points_for_max_step(points: list[float], max_step_v: float) -> list[float]:
    """Insert intermediate command points so adjacent commands stay bounded."""
    if not points:
        return []
    max_step_v = max(0.02, float(max_step_v))
    expanded = [float(points[0])]
    for point in points[1:]:
        start = expanded[-1]
        stop = float(point)
        steps = max(1, int(math.ceil(abs(stop - start) / max_step_v)))
        for idx in range(1, steps + 1):
            value = start + (stop - start) * idx / steps
            if abs(value - expanded[-1]) > 1e-9:
                expanded.append(value)
    return expanded


def move_to(
    dev: MDT693B,
    *,
    axis: str,
    target_readback_v: float,
    readback_offset_v: float,
    args: argparse.Namespace,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    safe_step_v = max(0.02, args.max_step_v * 0.75)
    idx = 0
    tolerance_v = max(0.05, args.max_step_v * 0.18)
    max_moves = max(2, int(math.ceil(abs(target_readback_v - dev.read_axis_voltage(axis)) / safe_step_v)) + 2)
    while True:
        current = dev.read_axis_voltage(axis)
        remaining = target_readback_v - current
        if abs(remaining) <= tolerance_v:
            break
        if idx >= max_moves:
            rows.append(
                {
                    "idx": idx + 1,
                    "current_readback_v": current,
                    "desired_readback_v": target_readback_v,
                    "command_target_v": target_readback_v - readback_offset_v,
                    "readback_offset_v": readback_offset_v,
                    "readback_v": current,
                    "execute": bool(args.execute),
                    "note": "target not reached within tolerance; using actual readback",
                }
            )
            break
        step = max(-safe_step_v, min(safe_step_v, remaining))
        desired = current + step
        idx += 1
        command = desired - readback_offset_v
        row: dict[str, object] = {
            "idx": idx,
            "current_readback_v": current,
            "desired_readback_v": desired,
            "command_target_v": command,
            "readback_offset_v": readback_offset_v,
            "execute": bool(args.execute),
        }
        if args.execute:
            row["readback_v"] = dev.set_axis_voltage_fast(
                axis,
                command,
                min_v=args.min_v,
                max_v=args.max_v,
                max_step_v=args.max_step_v * 1.05,
                expected_readback_v=desired,
                settle_s=min(args.settle_s, 0.03),
            )
            time.sleep(min(args.settle_s, 0.03))
        else:
            row["readback_v"] = current
        rows.append(row)
        if not args.execute:
            break
    return rows


def restore_to_best(
    dev: MDT693B,
    *,
    axis: str,
    target_readback_v: float,
    readback_offset_v: float,
    args: argparse.Namespace,
) -> list[dict[str, object]]:
    """Move back to the best scan point using the selected restore strategy."""
    if args.restore_mode == "ramp":
        rows = move_to(
            dev,
            axis=axis,
            target_readback_v=target_readback_v,
            readback_offset_v=readback_offset_v,
            args=args,
        )
        for row in rows:
            row["restore_mode"] = "ramp"
        return rows

    current = dev.read_axis_voltage(axis)
    target_command_v = target_readback_v - readback_offset_v
    if args.restore_mode == "direct":
        row: dict[str, object] = {
            "idx": 1,
            "restore_mode": "direct",
            "current_readback_v": current,
            "desired_readback_v": target_readback_v,
            "command_target_v": target_command_v,
            "readback_offset_v": readback_offset_v,
            "execute": bool(args.execute),
        }
        if args.execute:
            dev.write_axis_voltage_blind(axis, target_command_v, min_v=args.min_v, max_v=args.max_v)
            time.sleep(args.settle_s)
            row["readback_v"] = dev.read_axis_voltage(axis)
        else:
            row["readback_v"] = current
        return [row]

    rows: list[dict[str, object]] = []
    coarse_step_v = max(0.05, float(args.restore_coarse_step_v))
    fine_step_v = max(0.02, float(args.restore_fine_step_v))
    fine_window_v = max(fine_step_v, float(args.restore_fine_window_v))
    tolerance_v = max(0.03, fine_step_v * 0.35)
    idx = 0
    max_moves = max(4, int(math.ceil(abs(target_readback_v - current) / fine_step_v)) + 4)
    while True:
        current = dev.read_axis_voltage(axis)
        remaining = target_readback_v - current
        if abs(remaining) <= tolerance_v:
            break
        step_limit = fine_step_v if abs(remaining) <= fine_window_v else coarse_step_v
        step = math.copysign(min(abs(remaining), step_limit), remaining)
        desired = current + step
        command = desired - readback_offset_v
        idx += 1
        row = {
            "idx": idx,
            "restore_mode": "coarse-fine",
            "current_readback_v": current,
            "desired_readback_v": desired,
            "command_target_v": command,
            "readback_offset_v": readback_offset_v,
            "step_limit_v": step_limit,
            "execute": bool(args.execute),
        }
        if args.execute:
            row["readback_v"] = dev.set_axis_voltage_fast(
                axis,
                command,
                min_v=args.min_v,
                max_v=args.max_v,
                max_step_v=max(coarse_step_v, fine_step_v) * 1.1,
                expected_readback_v=desired,
                settle_s=min(args.settle_s, 0.03),
            )
            time.sleep(min(args.settle_s, 0.03))
        else:
            row["readback_v"] = current
        rows.append(row)
        if not args.execute or idx >= max_moves:
            break
    return rows


def evaluate_at(
    dev: MDT693B,
    *,
    port: str,
    axis: str,
    target_readback_v: float,
    readback_offset_v: float,
    args: argparse.Namespace,
) -> dict[str, object]:
    moves = move_to(
        dev,
        axis=axis,
        target_readback_v=target_readback_v,
        readback_offset_v=readback_offset_v,
        args=args,
    )
    time.sleep(args.settle_s)
    power_w, samples_w, overlay = measure_power(args)
    actual_v = dev.read_axis_voltage(axis)
    return {
        "port": port,
        "axis": axis,
        "target_readback_v": target_readback_v,
        "actual_readback_v": actual_v,
        "power_w": power_w,
        "samples_w": samples_w,
        "overlay": overlay,
        "moves": moves,
    }


def scan_one_axis(
    args: argparse.Namespace,
    dev: MDT693B,
    *,
    port: str,
    axis: str,
    range_v: float,
    initial_power_w: float,
    readback_offset_v: float,
) -> dict[str, object]:
    center = dev.read_axis_voltage(axis)
    lower = max(args.min_v, center - range_v)
    upper = min(args.max_v, center + range_v)
    coarse_points = scan_points(center, lower, upper, args.points_per_half, args.scan_waveform)
    points = expand_points_for_max_step(coarse_points, args.max_step_v)

    rows: list[dict[str, object]] = []
    aborted = False
    previous_command_readback_v = center
    for idx, point in enumerate(points):
        command = point - readback_offset_v
        if args.execute:
            if abs(point - previous_command_readback_v) > args.max_step_v * 1.05:
                raise ValueError(
                    f"Refusing command step {previous_command_readback_v:g} -> {point:g} V "
                    f"on {port}:{axis}; max_step_v={args.max_step_v:g}"
                )
            dev.write_axis_voltage_blind(axis, command, min_v=args.min_v, max_v=args.max_v)
            previous_command_readback_v = point
            time.sleep(args.settle_s)
        power_w, samples_w, overlay = measure_power(args)
        row = {
            "port": port,
            "axis": axis,
            "scan_index": idx,
            "commanded_readback_v": point,
            "command_target_v": command,
            "readback_offset_v": readback_offset_v,
            "power_w": power_w,
            "samples_w": samples_w,
            "overlay": overlay,
            "execute": bool(args.execute),
        }
        rows.append(row)
        if args.verbose:
            print(
                f"{port}:{axis} range={range_v:.3f} V "
                f"point {idx + 1}/{len(points)} "
                f"command={point:.3f} V "
                f"power={float(row['power_w']) * 1e6:.3f} uW",
                flush=True,
            )

    best = max(rows, key=lambda row: float(row["power_w"]))
    if float(best["power_w"]) < initial_power_w * args.abort_below_frac:
        aborted = True
    print(
        f"{port}:{axis} best command={float(best['commanded_readback_v']):.3f} V "
        f"power={float(best['power_w']) * 1e6:.3f} uW",
        flush=True,
    )
    restore_moves = restore_to_best(
        dev,
        axis=axis,
        target_readback_v=float(best["commanded_readback_v"]),
        readback_offset_v=readback_offset_v,
        args=args,
    )
    time.sleep(args.settle_s)
    final_power_w, final_samples_w, final_overlay = measure_power(args)
    final_v = dev.read_axis_voltage(axis)
    return {
        "port": port,
        "axis": axis,
        "center_v": center,
        "range_v": range_v,
        "scan_waveform": args.scan_waveform,
        "lower_v": lower,
        "upper_v": upper,
        "readback_offset_v": readback_offset_v,
        "aborted": aborted,
        "rows": rows,
        "best": best,
        "restore_moves": restore_moves,
        "final_readback_v": final_v,
        "final_power_w": final_power_w,
        "final_samples_w": final_samples_w,
        "final_overlay": final_overlay,
        "best_at_boundary": abs(float(best["commanded_readback_v"]) - lower) < 1e-6
        or abs(float(best["commanded_readback_v"]) - upper) < 1e-6,
    }


def read_all_voltages(axis_order: list[tuple[str, str]]) -> dict[str, dict[str, float]]:
    ports = sorted({port for port, _axis in axis_order})
    out: dict[str, dict[str, float]] = {}
    for port in ports:
        with MDT693B(port) as dev:
            out[port] = {axis: dev.read_axis_voltage(axis) for axis in ("x", "y", "z")}
    return out


def main() -> int:
    args = parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    record_path = (
        args.out_dir / f"triangular_scan_opt_{timestamp}.json"
        if args.save_history
        else args.out_dir / "triangular_scan_opt_latest.json"
    )
    offsets = {(port, axis): value for port, axis, value in args.readback_offset}

    initial_power_w, initial_samples_w, initial_overlay = measure_power(args)
    payload: dict[str, object] = {
        "timestamp": timestamp,
        "purpose": "coarse-to-fine triangular MDT693B scan coupling optimization",
        "execute": bool(args.execute),
        "axis_order": [f"{port}:{axis}" for port, axis in args.axis_order],
        "axis_order_note": (
            "COM7 is the right/input stage: z for height, y for lateral "
            "waveguide alignment, x for fiber-waveguide distance. COM6 is the "
            "left/output stage: z for height, x for lateral alignment, y for "
            "fiber-waveguide distance."
        ),
        "round_ranges_v": args.round_ranges_v,
        "scan_waveform": args.scan_waveform,
        "points_per_half": args.points_per_half,
        "channel": args.channel,
        "bridge": args.bridge,
        "max_step_v": args.max_step_v,
        "restore_mode": args.restore_mode,
        "restore_coarse_step_v": args.restore_coarse_step_v,
        "restore_fine_step_v": args.restore_fine_step_v,
        "restore_fine_window_v": args.restore_fine_window_v,
        "settle_s": args.settle_s,
        "samples": args.samples,
        "abort_below_frac": args.abort_below_frac,
        "readback_offsets_v": {f"{port}:{axis}": value for (port, axis), value in offsets.items()},
        "initial_power_w": initial_power_w,
        "initial_samples_w": initial_samples_w,
        "initial_overlay": initial_overlay,
        "initial_voltages_v": read_all_voltages(args.axis_order),
        "rounds": [],
        "status": "started",
    }

    try:
        for round_index, range_v in enumerate(args.round_ranges_v, start=1):
            round_rows = []
            for port, axis in args.axis_order:
                with MDT693B(port) as dev:
                    result = scan_one_axis(
                        args,
                        dev,
                        port=port,
                        axis=axis,
                        range_v=range_v,
                        initial_power_w=initial_power_w,
                        readback_offset_v=offsets.get((port, axis), 0.0),
                    )
                result["round_index"] = round_index
                round_rows.append(result)
                if result["aborted"]:
                    payload["status"] = "aborted"
                    payload["rounds"].append(
                        {"round_index": round_index, "range_v": range_v, "axes": round_rows}
                    )
                    raise RuntimeError(f"aborted after {port}:{axis} power drop")
            payload["rounds"].append(
                {"round_index": round_index, "range_v": range_v, "axes": round_rows}
            )

        final_power_w, final_samples_w, final_overlay = measure_power(args)
        payload.update(
            {
                "status": "ok",
                "final_power_w": final_power_w,
                "final_samples_w": final_samples_w,
                "final_overlay": final_overlay,
                "final_voltages_v": read_all_voltages(args.axis_order),
            }
        )
    except Exception as exc:
        payload.update(
            {
                "status": payload.get("status", "error") if payload.get("status") == "aborted" else "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "error_state_voltages_v": read_all_voltages(args.axis_order),
            }
        )
        record_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(
            f"ERROR {type(exc).__name__}: {exc}\n"
            f"Saved compact record: {record_path}",
            flush=True,
        )
        raise

    record_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"Done: initial={initial_power_w * 1e6:.3f} uW, "
        f"final={float(payload['final_power_w']) * 1e6:.3f} uW\n"
        f"Saved compact record: {record_path}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
