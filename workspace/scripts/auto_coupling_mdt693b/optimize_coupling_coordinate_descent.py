"""Conservative MDT693B coordinate-descent coupling optimization.

The score is read from the PyRPL bridge scope overlay, e.g.
"P avg(30f): CH1 1.437 uW".  The script is dry-run by default; add --execute
to actually write MDT693B voltages.  The default controller is COM7 because it
is the right-side stage that couples light into the chip waveguide.
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
POWER_RE = re.compile(
    r"P\s+(?P<kind>avg(?:\(\d+f\))?|inst)\s*:\s*"
    r"(?P<body>[^\n\r]+)",
    re.IGNORECASE,
)
CHANNEL_POWER_RE = re.compile(
    r"(?P<channel>CH[12])\s+"
    r"(?P<value>[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*"
    r"(?P<unit>pW|nW|uW|mW|W)",
    re.IGNORECASE,
)
UNIT_TO_W = {"pw": 1e-12, "nw": 1e-9, "uw": 1e-6, "mw": 1e-3, "w": 1.0}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM7", help="MDT693B port. Default: COM7.")
    parser.add_argument(
        "--axes",
        nargs="+",
        default=["x"],
        choices=["x", "y", "z"],
        help="Axes to optimize. Default: x only for first safety test.",
    )
    parser.add_argument("--channel", default="CH1", choices=["CH1", "CH2"])
    parser.add_argument("--bridge", default="http://127.0.0.1:7870")
    parser.add_argument("--step-v", type=float, default=0.02)
    parser.add_argument("--range-v", type=float, default=0.10)
    parser.add_argument("--max-step-v", type=float, default=0.02)
    parser.add_argument(
        "--readback-offset-v",
        type=float,
        default=0.0,
        help=(
            "Constant output readback offset added to the USB setpoint, e.g. "
            "from EXT input bias. Optimization targets readback/output voltage."
        ),
    )
    parser.add_argument("--min-v", type=float, default=0.0)
    parser.add_argument("--max-v", type=float, default=75.0)
    parser.add_argument("--settle-s", type=float, default=0.8)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--sample-interval-s", type=float, default=0.25)
    parser.add_argument(
        "--min-improvement-frac",
        type=float,
        default=0.002,
        help="Minimum fractional improvement needed to keep walking.",
    )
    parser.add_argument(
        "--max-drop-frac",
        type=float,
        default=0.30,
        help="Abort if a trial point drops below this fraction of the starting power.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually write voltages. Without this flag, only dry-runs readouts/plans.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_EXPERIMENT_DIR / "raw",
    )
    return parser.parse_args()


def parse_power_from_overlay(text: str, channel: str) -> float:
    """Return selected channel optical power in W, preferring P avg."""
    matches = list(POWER_RE.finditer(text))
    if not matches:
        raise ValueError(f"No P inst/P avg line found in overlay: {text!r}")
    matches.sort(key=lambda m: 0 if m.group("kind").lower().startswith("avg") else 1)
    for match in matches:
        body = match.group("body")
        for item in CHANNEL_POWER_RE.finditer(body):
            if item.group("channel").upper() == channel.upper():
                value = float(item.group("value"))
                unit = item.group("unit").lower()
                return value * UNIT_TO_W[unit]
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
    return float(sum(finite) / len(finite)), values, last_overlay


def ramp_to(
    dev: MDT693B,
    axis: str,
    target_v: float,
    *,
    execute: bool,
    min_v: float,
    max_v: float,
    max_step_v: float,
    readback_offset_v: float,
) -> list[dict[str, float | str | bool]]:
    current = dev.read_axis_voltage(axis)
    target_v = float(target_v)
    if not (min_v <= target_v <= max_v):
        raise ValueError(f"Target {target_v:g} V outside [{min_v:g}, {max_v:g}] V")
    if max_step_v <= 0:
        raise ValueError("max_step_v must be positive")
    n_steps = max(1, int(math.ceil(abs(target_v - current) / max_step_v)))
    rows: list[dict[str, float | str | bool]] = []
    for step_idx in range(1, n_steps + 1):
        intermediate = current + (target_v - current) * step_idx / n_steps
        command_target = intermediate - readback_offset_v
        row: dict[str, float | str | bool] = {
            "axis": axis,
            "from_v": current,
            "target_v": intermediate,
            "command_target_v": command_target,
            "readback_offset_v": readback_offset_v,
            "execute": execute,
        }
        if execute:
            readback = dev.set_axis_voltage(
                axis,
                command_target,
                min_v=min_v,
                max_v=max_v,
                max_step_v=max_step_v * 1.05,
                expected_readback_v=intermediate,
            )
            row["readback_v"] = readback
        else:
            row["readback_v"] = current
        rows.append(row)
    return rows


def evaluate_point(
    args: argparse.Namespace,
    dev: MDT693B,
    axis: str,
    voltage_v: float,
    label: str,
) -> dict[str, object]:
    moves = ramp_to(
        dev,
        axis,
        voltage_v,
        execute=args.execute,
        min_v=args.min_v,
        max_v=args.max_v,
        max_step_v=args.max_step_v,
        readback_offset_v=args.readback_offset_v,
    )
    time.sleep(args.settle_s)
    power_w, samples_w, overlay = measure_power(args)
    actual_v = dev.read_axis_voltage(axis)
    return {
        "label": label,
        "axis": axis,
        "requested_v": voltage_v,
        "actual_v": actual_v,
        "power_w": power_w,
        "samples_w": samples_w,
        "overlay": overlay,
        "moves": moves,
    }


def evaluate_current(
    args: argparse.Namespace,
    dev: MDT693B,
    axis: str,
    label: str,
) -> dict[str, object]:
    """Measure power at the current voltage without issuing a voltage command."""
    actual_v = dev.read_axis_voltage(axis)
    time.sleep(args.settle_s)
    power_w, samples_w, overlay = measure_power(args)
    return {
        "label": label,
        "axis": axis,
        "requested_v": actual_v,
        "actual_v": actual_v,
        "power_w": power_w,
        "samples_w": samples_w,
        "overlay": overlay,
        "moves": [],
    }


def optimize_axis(
    args: argparse.Namespace,
    dev: MDT693B,
    axis: str,
    start_power_w: float,
) -> dict[str, object]:
    start_v = dev.read_axis_voltage(axis)
    lower = max(args.min_v, start_v - args.range_v)
    upper = min(args.max_v, start_v + args.range_v)
    axis_rows: list[dict[str, object]] = []

    center = evaluate_current(args, dev, axis, "center")
    axis_rows.append(center)
    best = center
    if center["power_w"] < start_power_w * (1.0 - args.max_drop_frac):
        return {"axis": axis, "start_v": start_v, "best": best, "rows": axis_rows, "aborted": True}

    candidates = []
    for direction in (1.0, -1.0):
        target = start_v + direction * args.step_v
        if lower <= target <= upper:
            trial = evaluate_point(args, dev, axis, target, f"trial_{direction:+.0f}")
            axis_rows.append(trial)
            candidates.append((direction, trial))
            if trial["power_w"] < start_power_w * (1.0 - args.max_drop_frac):
                break

    if axis_rows:
        best = max(axis_rows, key=lambda row: float(row["power_w"]))
    direction = 0.0
    for cand_direction, trial in candidates:
        if trial is best:
            direction = cand_direction
            break

    if direction != 0.0 and float(best["power_w"]) > float(center["power_w"]) * (
        1.0 + args.min_improvement_frac
    ):
        current_v = float(best["actual_v"])
        while True:
            next_v = current_v + direction * args.step_v
            if not (lower <= next_v <= upper):
                break
            trial = evaluate_point(args, dev, axis, next_v, f"walk_{direction:+.0f}")
            axis_rows.append(trial)
            if trial["power_w"] < start_power_w * (1.0 - args.max_drop_frac):
                break
            if float(trial["power_w"]) > float(best["power_w"]) * (
                1.0 + args.min_improvement_frac
            ):
                best = trial
                current_v = float(trial["actual_v"])
            else:
                break

    ramp_to(
        dev,
        axis,
        float(best["actual_v"]),
        execute=args.execute,
        min_v=args.min_v,
        max_v=args.max_v,
        max_step_v=args.max_step_v,
        readback_offset_v=args.readback_offset_v,
    )
    return {
        "axis": axis,
        "start_v": start_v,
        "lower_v": lower,
        "upper_v": upper,
        "best": best,
        "rows": axis_rows,
        "aborted": False,
    }


def main() -> int:
    args = parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    record_path = args.out_dir / f"coupling_opt_{args.port}_{timestamp}.json"

    payload: dict[str, object] = {
        "timestamp": timestamp,
        "execute": bool(args.execute),
        "port": args.port,
        "axes": args.axes,
        "channel": args.channel,
        "step_v": args.step_v,
        "range_v": args.range_v,
        "readback_offset_v": args.readback_offset_v,
        "settle_s": args.settle_s,
        "samples": args.samples,
        "status": "started",
    }
    axes_results = []
    try:
        with MDT693B(args.port) as dev:
            initial_power_w, initial_samples_w, initial_overlay = measure_power(args)
            initial_voltages = {axis: dev.read_axis_voltage(axis) for axis in ("x", "y", "z")}
            payload.update(
                {
                    "initial_power_w": initial_power_w,
                    "initial_samples_w": initial_samples_w,
                    "initial_overlay": initial_overlay,
                    "initial_voltages_v": initial_voltages,
                }
            )
            for axis in args.axes:
                result = optimize_axis(args, dev, axis, initial_power_w)
                axes_results.append(result)
                if result.get("aborted"):
                    break
            final_power_w, final_samples_w, final_overlay = measure_power(args)
            final_voltages = {axis: dev.read_axis_voltage(axis) for axis in ("x", "y", "z")}
            payload.update(
                {
                    "status": "ok",
                    "axes_results": axes_results,
                    "final_power_w": final_power_w,
                    "final_samples_w": final_samples_w,
                    "final_overlay": final_overlay,
                    "final_voltages_v": final_voltages,
                }
            )
    except Exception as exc:
        payload.update(
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "axes_results": axes_results,
            }
        )
        try:
            with MDT693B(args.port) as dev:
                payload["error_state_voltages_v"] = {
                    axis: dev.read_axis_voltage(axis) for axis in ("x", "y", "z")
                }
        except Exception as state_exc:
            payload["error_state_readback_error"] = repr(state_exc)
        record_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        print(f"\nSaved: {record_path}")
        raise
    record_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\nSaved: {record_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
