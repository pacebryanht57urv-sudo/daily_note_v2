"""Safely nudge one MDT693B axis by a small voltage step.

This script is dry-run by default. Add --execute to actually write voltage.
"""

from __future__ import annotations

import argparse
import json

from mdt693b import MDT693B


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="COM port, e.g. COM6.")
    parser.add_argument("--axis", required=True, choices=["x", "y", "z"])
    parser.add_argument("--delta-v", type=float, required=True, help="Relative voltage step.")
    parser.add_argument("--min-v", type=float, default=0.0)
    parser.add_argument("--max-v", type=float, default=75.0)
    parser.add_argument("--max-step-v", type=float, default=0.2)
    parser.add_argument(
        "--readback-offset-v",
        type=float,
        default=0.0,
        help=(
            "Constant output readback offset added to the USB setpoint, e.g. "
            "from EXT input bias. The requested delta is applied to the "
            "readback/output voltage while the USB command is offset-corrected."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually write the new voltage. Without this flag, only prints the plan.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with MDT693B(args.port) as dev:
        before = dev.read_axis_voltage(args.axis)
        desired_after = before + args.delta_v
        command_target = desired_after - args.readback_offset_v
        plan = {
            "port": args.port,
            "axis": args.axis,
            "before_v": before,
            "delta_v": args.delta_v,
            "desired_after_v": desired_after,
            "readback_offset_v": args.readback_offset_v,
            "command_target_v": command_target,
            "limits_v": [args.min_v, args.max_v],
            "max_step_v": args.max_step_v,
            "execute": bool(args.execute),
        }
        if not args.execute:
            plan["status"] = "dry_run_no_voltage_written"
            print(json.dumps(plan, indent=2, ensure_ascii=False))
            return 0
        after = dev.set_axis_voltage(
            args.axis,
            command_target,
            min_v=args.min_v,
            max_v=args.max_v,
            max_step_v=args.max_step_v,
            expected_readback_v=desired_after,
        )
        plan["after_v"] = after
        plan["status"] = "written_and_read_back"
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
