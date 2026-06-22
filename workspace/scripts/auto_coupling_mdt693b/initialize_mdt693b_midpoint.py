"""Initialize MDT693B stages to a reproducible midpoint before manual coupling.

This script is dry-run by default.  Add --execute only after confirming both
fiber probes are safely away from the chip.  The goal is to make the manual
pre-coupling start state reproducible, not to optimize coupling.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import time

from mdt693b import MDT693B


DEFAULT_EXPERIMENT_DIR = (
    Path(__file__).resolve().parents[2] / "experiments" / "auto_coupling_mdt693b"
)


def parse_offset(text: str) -> tuple[str, str, float]:
    """Parse COM7:x=0.24 style readback-offset entries."""
    try:
        lhs, value_text = text.split("=", 1)
        port, axis = lhs.split(":", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "readback offset must look like COM7:x=0.24"
        ) from exc
    axis = axis.strip().lower()
    if axis not in {"x", "y", "z"}:
        raise argparse.ArgumentTypeError("axis must be x, y, or z")
    try:
        value = float(value_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("offset must be numeric") from exc
    return port.strip().upper(), axis, value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ports", nargs="+", default=["COM7", "COM6"])
    parser.add_argument("--axes", nargs="+", default=["x", "y", "z"], choices=["x", "y", "z"])
    parser.add_argument("--target-v", type=float, default=37.5)
    parser.add_argument("--min-v", type=float, default=0.0)
    parser.add_argument("--max-v", type=float, default=75.0)
    parser.add_argument(
        "--step-v",
        type=float,
        default=1.0,
        help="Maximum output/readback voltage change per write.",
    )
    parser.add_argument("--settle-s", type=float, default=0.05)
    parser.add_argument(
        "--readback-offset",
        action="append",
        type=parse_offset,
        default=[],
        help="Optional offset such as COM7:x=0.24; may be repeated.",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_EXPERIMENT_DIR / "raw")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually write voltages. Without this flag, only records the plan.",
    )
    return parser.parse_args()


def read_voltages(dev: MDT693B, axes: list[str]) -> dict[str, float]:
    return {axis: dev.read_axis_voltage(axis) for axis in axes}


def ramp_axis(
    dev: MDT693B,
    *,
    port: str,
    axis: str,
    target_v: float,
    step_v: float,
    min_v: float,
    max_v: float,
    settle_s: float,
    readback_offset_v: float,
    execute: bool,
) -> dict[str, object]:
    before_v = dev.read_axis_voltage(axis)
    delta_v = target_v - before_v
    n_steps = max(1, int(abs(delta_v) / step_v + 0.999999))
    planned = []
    current_v = before_v

    for idx in range(1, n_steps + 1):
        desired_v = before_v + delta_v * idx / n_steps
        command_v = desired_v - readback_offset_v
        row: dict[str, object] = {
            "idx": idx,
            "desired_readback_v": desired_v,
            "command_target_v": command_v,
            "readback_offset_v": readback_offset_v,
        }
        if execute:
            readback_v = dev.set_axis_voltage(
                axis,
                command_v,
                min_v=min_v,
                max_v=max_v,
                max_step_v=step_v * 1.05,
                readback_tolerance_v=max(0.08, step_v * 0.6),
                expected_readback_v=desired_v,
            )
            row["readback_v"] = readback_v
            current_v = readback_v
            time.sleep(settle_s)
        else:
            row["readback_v"] = current_v
        planned.append(row)

    after_v = dev.read_axis_voltage(axis)
    return {
        "port": port,
        "axis": axis,
        "before_v": before_v,
        "target_v": target_v,
        "after_v": after_v,
        "execute": execute,
        "n_steps": n_steps,
        "steps": planned,
    }


def main() -> int:
    args = parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    record_path = args.out_dir / f"mdt693b_midpoint_init_{timestamp}.json"
    offsets = {(port, axis): value for port, axis, value in args.readback_offset}

    payload: dict[str, object] = {
        "timestamp": timestamp,
        "purpose": "initialize MDT693B axes to midpoint before manual coupling",
        "execute": bool(args.execute),
        "ports": args.ports,
        "axes": args.axes,
        "target_v": args.target_v,
        "step_v": args.step_v,
        "safety_note": "Execute only when fiber probes are safely away from the chip.",
        "readback_offsets_v": {
            f"{port}:{axis}": value for (port, axis), value in offsets.items()
        },
        "controllers": [],
    }

    for port in args.ports:
        port_key = port.upper()
        with MDT693B(port) as dev:
            before = read_voltages(dev, args.axes)
            axis_results = []
            for axis in args.axes:
                axis_results.append(
                    ramp_axis(
                        dev,
                        port=port_key,
                        axis=axis,
                        target_v=args.target_v,
                        step_v=args.step_v,
                        min_v=args.min_v,
                        max_v=args.max_v,
                        settle_s=args.settle_s,
                        readback_offset_v=offsets.get((port_key, axis), 0.0),
                        execute=args.execute,
                    )
                )
            after = read_voltages(dev, args.axes)
        payload["controllers"].append(
            {
                "port": port_key,
                "before_v": before,
                "after_v": after,
                "axis_results": axis_results,
            }
        )

    record_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\nSaved: {record_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
