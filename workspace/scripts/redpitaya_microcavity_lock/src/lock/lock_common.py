"""Shared helpers for the streamlined Red Pitaya microcavity lock workflow."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import numpy as np

LOCK_DIR = Path(__file__).resolve().parent
SRC_DIR = LOCK_DIR.parent
COMMON_DIR = SRC_DIR / "common"
DRIVERS_DIR = SRC_DIR / "drivers"
for module_dir in (COMMON_DIR, DRIVERS_DIR):
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))

from data_paths import RESULTS_DIR


def bridge_get(base: str, path: str, params: dict[str, object] | None = None) -> dict:
    url = base.rstrip("/") + path
    if params:
        url += "?" + urlencode(params)
    try:
        with urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
        raise RuntimeError(
            {
                "url": url,
                "status": exc.code,
                "reason": exc.reason,
                "body": body,
                "json": parsed,
            }
        ) from exc


def set_param(base: str, param: str, value: object) -> object:
    result = bridge_get(base, "/set", {"param": param, "value": str(value)})
    if not result.get("ok"):
        raise RuntimeError(result)
    return result.get("after")


def reset_pid_input_filter(base: str) -> object:
    """Use raw in1 for DC transmission locking."""
    result = bridge_get(base, "/get", {"param": "pid0.inputfilter"})
    if result.get("ok"):
        value = result.get("value")
        try:
            values = list(value)
            if values and all(abs(float(v)) < 1e-12 for v in values):
                return value
        except (TypeError, ValueError):
            try:
                if abs(float(value)) < 1e-12:
                    return value
            except (TypeError, ValueError):
                pass
    return set_param(base, "pid0.inputfilter", 0)


def stop_bridge_acquisitions(base: str) -> None:
    try:
        result = bridge_get(base, "/acquisition/stop")
        if not result.get("ok"):
            print(f"WARN bridge acquisition stop returned: {result}", flush=True)
    except Exception as exc:
        print(f"WARN could not stop spectrum/network acquisitions before scope setup: {exc}", flush=True)


def read_arc_factor(host: str) -> dict[str, object]:
    from toptica.lasersdk.dlcpro.v2_5_3 import DLCpro, NetworkConnection

    with DLCpro(NetworkConnection(host)) as dlc:
        return {
            "system_type": dlc.system_type.get(),
            "serial_number": dlc.serial_number.get(),
            "fw_ver": dlc.fw_ver.get(),
            "system_health_txt": dlc.system_health_txt.get(),
            "emission": bool(dlc.emission.get()),
            "arc_factor": float(dlc.laser1.dl.pc.external_input.factor.get()),
            "arc_enabled": bool(dlc.laser1.dl.pc.external_input.enabled.get()),
            "arc_signal": int(dlc.laser1.dl.pc.external_input.signal.get()),
        }


def read_pc(host: str) -> dict[str, float | bool]:
    from toptica.lasersdk.dlcpro.v2_5_3 import DLCpro, NetworkConnection

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
    from toptica.lasersdk.dlcpro.v2_5_3 import DLCpro, NetworkConnection

    with DLCpro(NetworkConnection(host)) as dlc:
        pc = dlc.laser1.dl.pc
        vmin = float(pc.voltage_min.get())
        vmax = float(pc.voltage_max.get())
        clipped = max(vmin, min(vmax, float(value)))
        pc.voltage_set.set(clipped)
        time.sleep(0.5)
    return read_pc(host)


def write_arc_factor(host: str, value: float) -> dict[str, float | bool]:
    from toptica.lasersdk.dlcpro.v2_5_3 import DLCpro, NetworkConnection

    with DLCpro(NetworkConnection(host)) as dlc:
        pc = dlc.laser1.dl.pc
        pc.external_input.factor.set(float(value))
        time.sleep(0.4)
    return read_pc(host)


def configure_prelock_sweep(base: str, duration: float, frequency: float, amplitude: float) -> None:
    for param, value in (
        ("pid0.output_direct", "off"),
        ("pid0.p", 0),
        ("pid0.i", 0),
        ("asg0.waveform", "ramp"),
        ("asg0.frequency", frequency),
        ("asg0.amplitude", amplitude),
        ("asg0.offset", 0),
        ("asg0.output_direct", "out2"),
        ("asg1.waveform", "square"),
        ("asg1.frequency", frequency),
        ("asg1.amplitude", 0.5),
        ("asg1.offset", 0),
        ("scope.input1", "in1"),
        ("scope.input2", "out2"),
        ("scope.trigger_source", "asg1"),
        ("scope.trigger_delay", 0),
        ("scope.duration", duration),
        ("scope.rolling_mode", "false"),
    ):
        set_param(base, param, value)
    try:
        set_param(base, "scope.run_continuous", "true")
    except Exception as exc:
        print(f"WARN could not force scope run_continuous before lock capture: {exc}", flush=True)


def crossing_x(y0: float, y1: float, x0: float, x1: float, y_cross: float) -> float | None:
    denom = y1 - y0
    if abs(denom) < 1e-15:
        return None
    frac = (y_cross - y0) / denom
    if frac < -1e-9 or frac > 1 + 1e-9:
        return None
    return float(x0 + frac * (x1 - x0))


def contiguous_regions(mask: np.ndarray) -> list[tuple[int, int]]:
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.r_[idx[0], idx[breaks + 1]]
    ends = np.r_[idx[breaks], idx[-1]]
    return [(int(start), int(end)) for start, end in zip(starts, ends)]


def downsweep_segment(t: np.ndarray, sweep: np.ndarray) -> dict[str, object]:
    x = np.asarray(sweep, dtype=float)
    if x.size < 16:
        return {"ok": False, "error": "too few samples for downsweep selection"}
    full_ptp = float(np.nanmax(x) - np.nanmin(x))
    if not np.isfinite(full_ptp) or full_ptp <= 0:
        return {"ok": False, "error": "invalid sweep range"}

    dx = np.diff(x)
    neg = dx < -max(full_ptp * 1e-5, 1e-7)
    candidates = []
    for start_diff, end_diff in contiguous_regions(neg):
        start = start_diff
        end = end_diff + 1
        if end - start < 16:
            continue
        drop = float(x[start] - x[end])
        if drop <= 0:
            continue
        candidates.append(
            {
                "start_index": int(start),
                "end_index": int(end),
                "start_time": float(t[start]),
                "end_time": float(t[end]),
                "start_sweep_voltage": float(x[start]),
                "end_sweep_voltage": float(x[end]),
                "drop_out2_v": drop,
                "samples": int(end - start + 1),
            }
        )
    if not candidates:
        return {"ok": False, "error": "no decreasing sweep segment found"}

    best = max(candidates, key=lambda item: (item["drop_out2_v"], item["samples"]))
    best["ok"] = True
    best["num_candidates"] = len(candidates)
    return best


def analyze_apparent_width(
    t: np.ndarray,
    transmission: np.ndarray,
    sweep: np.ndarray,
    depth_fraction: float,
    use_downsweep: bool = True,
) -> dict[str, object]:
    y = np.asarray(transmission, dtype=float)
    x = np.asarray(sweep, dtype=float)
    tt = np.asarray(t, dtype=float)
    finite = np.isfinite(y) & np.isfinite(x) & np.isfinite(tt)
    y = y[finite]
    x = x[finite]
    tt = tt[finite]
    if y.size < 16:
        return {"ok": False, "error": "too few finite samples"}

    segment: dict[str, object] | None = None
    if use_downsweep:
        segment = downsweep_segment(tt, x)
        if not segment.get("ok"):
            return segment
        start = int(segment["start_index"])
        end = int(segment["end_index"])
        y = y[start : end + 1]
        x = x[start : end + 1]
        tt = tt[start : end + 1]

    t_min = float(np.nanmin(y))
    min_index = int(np.nanargmin(y))
    min_sweep_voltage = float(x[min_index])
    min_time = float(tt[min_index])
    t_max = float(np.nanpercentile(y, 95))
    if t_max <= t_min:
        return {"ok": False, "error": "invalid transmission range"}
    t_lock = float(t_min + depth_fraction * (t_max - t_min))
    platform_drop_fraction = 1.0 - depth_fraction

    candidates = []
    for start, end in contiguous_regions(y <= t_lock):
        if start == 0 or end >= y.size - 1:
            continue
        left_x = crossing_x(y[start - 1], y[start], x[start - 1], x[start], t_lock)
        right_x = crossing_x(y[end], y[end + 1], x[end], x[end + 1], t_lock)
        if left_x is None or right_x is None:
            continue
        width = abs(right_x - left_x)
        if not np.isfinite(width) or width <= 0:
            continue
        local_min_index = int(start + np.argmin(y[start : end + 1]))
        dip_x = float(x[local_min_index])
        candidates.append(
            {
                "start_index": start,
                "end_index": end,
                "min_index": local_min_index,
                "min_transmission": float(y[local_min_index]),
                "min_sweep_voltage": dip_x,
                "min_time": float(tt[local_min_index]),
                "left_sweep_voltage": float(left_x),
                "right_sweep_voltage": float(right_x),
                "quarter_full_width_out2_v": float(width),
                "platform_drop_fraction": float(platform_drop_fraction),
                "capture_side_sweep_voltage": float(max(left_x, right_x)),
                "capture_direction": "from_higher_out2_to_dip",
                "capture_width_out2_v": float(abs(max(left_x, right_x) - dip_x)),
                "lower_side_sweep_voltage": float(min(left_x, right_x)),
                "higher_side_sweep_voltage": float(max(left_x, right_x)),
                "samples_below": int(end - start + 1),
            }
        )

    if not candidates:
        return {
            "ok": False,
            "error": "no complete crossings at chosen depth",
            "transmission_min": t_min,
            "min_sweep_voltage": min_sweep_voltage,
            "min_time": min_time,
            "transmission_max_p95": t_max,
            "transmission_lock": t_lock,
            "sweep_window": "downsweep" if use_downsweep else "full_trace",
            "downsweep_segment": segment,
        }

    best = min(candidates, key=lambda item: item["min_transmission"])
    return {
        "ok": True,
        "depth_fraction": depth_fraction,
        "platform_drop_fraction": platform_drop_fraction,
        "transmission_min": t_min,
        "transmission_max_p95": t_max,
        "transmission_lock": t_lock,
        "sweep_window": "downsweep" if use_downsweep else "full_trace",
        "downsweep_segment": segment,
        "num_complete_dips": len(candidates),
        "chosen": best,
        "candidates": candidates,
    }
