"""Suggest TOPTICA DLC pro ARC factor from a Red Pitaya pre-lock trace.

This script is intentionally read-only for TOPTICA by default. It only reads
the current ARC factor, captures a Red Pitaya sweep trace, estimates the
platform-drop 1/4 to dip capture distance on the Out2 control-voltage axis,
and suggests whether ARC factor should be increased, decreased, or kept.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

import matplotlib.pyplot as plt
import numpy as np
from toptica.lasersdk.dlcpro.v2_5_3 import DLCpro, NetworkConnection


SESSION_DIR = Path(__file__).resolve().parents[1]
RESULTS_DIR = SESSION_DIR / "results" / "pyrpl_live_bridge"


def bridge_get(base: str, path: str, params: dict[str, object] | None = None) -> dict:
    url = base.rstrip("/") + path
    if params:
        url += "?" + urlencode(params)
    with urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def set_param(base: str, param: str, value: object) -> object:
    result = bridge_get(base, "/set", {"param": param, "value": str(value)})
    if not result.get("ok"):
        raise RuntimeError(result)
    return result.get("after")


def read_arc_factor(host: str) -> dict[str, object]:
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


def configure_prelock_sweep(base: str, duration: float, frequency: float, amplitude: float) -> None:
    set_param(base, "pid0.output_direct", "off")
    set_param(base, "pid0.p", 0)
    set_param(base, "pid0.i", 0)
    set_param(base, "asg0.waveform", "ramp")
    set_param(base, "asg0.frequency", frequency)
    set_param(base, "asg0.amplitude", amplitude)
    set_param(base, "asg0.offset", 0)
    set_param(base, "asg0.output_direct", "out2")
    set_param(base, "asg1.waveform", "square")
    set_param(base, "asg1.frequency", frequency)
    set_param(base, "asg1.amplitude", 0.5)
    set_param(base, "asg1.offset", 0)
    set_param(base, "scope.input1", "in1")
    set_param(base, "scope.input2", "out2")
    set_param(base, "scope.trigger_source", "asg1")
    set_param(base, "scope.trigger_delay", 0)
    set_param(base, "scope.duration", duration)
    set_param(base, "scope.rolling_mode", "false")


def crossing_x(
    y0: float, y1: float, x0: float, x1: float, y_cross: float
) -> float | None:
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
    """Return the largest complete decreasing triangle-ramp segment."""
    x = np.asarray(sweep, dtype=float)
    if x.size < 16:
        return {"ok": False, "error": "too few samples for downsweep selection"}
    full_ptp = float(np.nanmax(x) - np.nanmin(x))
    if not np.isfinite(full_ptp) or full_ptp <= 0:
        return {"ok": False, "error": "invalid sweep range"}

    dx = np.diff(x)
    neg = dx < -max(full_ptp * 1e-5, 1e-7)
    regions = contiguous_regions(neg)
    candidates = []
    for start_diff, end_diff in regions:
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
    t_max = float(np.nanpercentile(y, 95))
    if t_max <= t_min:
        return {"ok": False, "error": "invalid transmission range"}
    t_lock = float(t_min + depth_fraction * (t_max - t_min))
    platform_drop_fraction = 1.0 - depth_fraction

    below = y <= t_lock
    regions = contiguous_regions(below)
    candidates = []
    for start, end in regions:
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
        higher_side_x = float(max(left_x, right_x))
        lower_side_x = float(min(left_x, right_x))
        capture_x = higher_side_x
        capture_direction = "from_higher_out2_to_dip"
        capture_width = abs(capture_x - dip_x)
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
                "capture_side_sweep_voltage": capture_x,
                "capture_direction": capture_direction,
                "capture_width_out2_v": float(capture_width),
                "lower_side_sweep_voltage": lower_side_x,
                "higher_side_sweep_voltage": higher_side_x,
                "samples_below": int(end - start + 1),
            }
        )

    if not candidates:
        return {
            "ok": False,
            "error": "no complete crossings at chosen depth",
            "transmission_min": t_min,
            "transmission_max_p95": t_max,
            "transmission_lock": t_lock,
            "sweep_window": "downsweep" if use_downsweep else "full_trace",
            "downsweep_segment": segment,
        }

    # Prefer the deepest complete dip. This avoids edge dips that lack both
    # crossings and ignores shallow threshold noise.
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


def suggest_arc_factor(
    current_arc: float,
    capture_width: float,
    min_width: float,
    max_width: float,
    min_arc: float,
    max_arc: float,
    max_fractional_step: float,
) -> dict[str, object]:
    target_width = 0.5 * (min_width + max_width)
    if capture_width < min_width:
        raw = current_arc * capture_width / target_width
        lower = current_arc * (1.0 - max_fractional_step)
        suggested = max(raw, lower, min_arc)
        action = "decrease_arc_factor"
        reason = "platform-drop 1/4 to dip capture distance is too short on the Out2 axis"
    elif capture_width > max_width:
        raw = current_arc * capture_width / target_width
        upper = current_arc * (1.0 + max_fractional_step)
        suggested = min(raw, upper, max_arc)
        action = "increase_arc_factor"
        reason = "platform-drop 1/4 to dip capture distance is too long on the Out2 axis"
    else:
        suggested = current_arc
        action = "keep_arc_factor"
        reason = "platform-drop 1/4 to dip capture distance is inside the provisional target range"
    return {
        "action": action,
        "reason": reason,
        "current_arc_factor": current_arc,
        "suggested_arc_factor": float(suggested),
        "target_capture_width_out2_v": target_width,
        "min_capture_width_out2_v": min_width,
        "max_capture_width_out2_v": max_width,
        "max_fractional_step": max_fractional_step,
    }


def make_plot(
    path: Path,
    t: np.ndarray,
    transmission: np.ndarray,
    sweep: np.ndarray,
    analysis: dict[str, object],
    arc_info: dict[str, object],
    suggestion: dict[str, object] | None,
    lock_analysis: dict[str, object] | None = None,
) -> None:
    plt.rcParams.update(
        {
            "font.size": 14,
            "axes.titlesize": 20,
            "axes.labelsize": 17,
            "xtick.labelsize": 14,
            "ytick.labelsize": 14,
            "legend.fontsize": 13,
            "lines.linewidth": 2.2,
            "figure.dpi": 150,
            "savefig.dpi": 300,
            "savefig.bbox": "tight",
        }
    )
    if lock_analysis is None:
        lock_analysis = analyze_apparent_width(t, transmission, sweep, 0.25)

    plot_t = np.asarray(t, dtype=float)
    plot_y = np.asarray(transmission, dtype=float)
    plot_x = np.asarray(sweep, dtype=float)
    seg = analysis.get("downsweep_segment") if isinstance(analysis, dict) else None
    if isinstance(seg, dict) and seg.get("ok"):
        start = int(seg["start_index"])
        end = int(seg["end_index"])
        plot_t = plot_t[start : end + 1]
        plot_y = plot_y[start : end + 1]
        plot_x = plot_x[start : end + 1]
    if plot_t.size:
        plot_t_ms = (plot_t - plot_t[0]) * 1e3
    else:
        plot_t_ms = plot_t * 1e3

    fig, axes = plt.subplots(1, 2, figsize=(15, 6), constrained_layout=True)
    axes[0].plot(plot_t_ms, plot_y, color="tab:green")
    axes[0].set_xlabel("Time in selected downsweep (ms)")
    axes[0].set_ylabel("CH1 / transmission (V)")
    axes[0].grid(True, alpha=0.3)

    axes[1].plot(plot_x, plot_y, color="tab:blue")
    axes[1].set_xlabel("CH2 / Out2 sweep voltage (V)")
    axes[1].set_ylabel("CH1 / transmission (V)")
    axes[1].grid(True, alpha=0.3)
    axes[1].invert_xaxis()

    lines = [
        f"ARC factor = {arc_info['arc_factor']:.6g}",
        f"ARC enabled = {arc_info['arc_enabled']}",
    ]
    if analysis.get("ok"):
        width_chosen = analysis["chosen"]
        y_width = float(analysis["transmission_lock"])
        axes[1].axhline(
            y_width,
            color="tab:orange",
            ls="--",
            label="platform-drop 1/4",
        )
        axes[1].scatter(
            [width_chosen["capture_side_sweep_voltage"], width_chosen["min_sweep_voltage"]],
            [y_width, width_chosen["min_transmission"]],
            color="tab:purple",
            s=55,
            zorder=5,
            label="width endpoints",
        )
        axes[1].annotate(
            "",
            xy=(width_chosen["min_sweep_voltage"], width_chosen["min_transmission"]),
            xytext=(width_chosen["capture_side_sweep_voltage"], y_width),
            arrowprops={"arrowstyle": "<->", "color": "tab:purple", "lw": 2.4},
        )
        lines.extend(
            [
                f"platform-drop 1/4 width = {width_chosen['capture_width_out2_v']:.6f} V",
                f"full width at this level = {width_chosen['quarter_full_width_out2_v']:.6f} V",
                f"dip Out2 = {width_chosen['min_sweep_voltage']:.6f} V",
                f"window = {analysis.get('sweep_window')}",
            ]
        )
    if lock_analysis and lock_analysis.get("ok"):
        lock_chosen = lock_analysis["chosen"]
        y_lock = float(lock_analysis["transmission_lock"])
        lock_fraction = float(lock_analysis.get("depth_fraction", 0.25))
        axes[1].axhline(
            y_lock,
            color="tab:red",
            ls=":",
            label=f"dip-rise {lock_fraction:.3g} lock",
        )
        axes[1].scatter(
            [lock_chosen["capture_side_sweep_voltage"]],
            [y_lock],
            color="tab:red",
            s=80,
            zorder=7,
            label="lock point",
        )
        lines.extend(
            [
                f"lock CH1 = {y_lock:.6f} V",
                f"lock Out2 = {lock_chosen['capture_side_sweep_voltage']:.6f} V",
            ]
        )
    else:
        if not analysis.get("ok"):
            lines.append(f"analysis error: {analysis.get('error')}")
        elif lock_analysis:
            lines.append(f"lock analysis error: {lock_analysis.get('error')}")
    if suggestion:
        lines.extend(
            [
                f"action = {suggestion['action']}",
                f"suggested ARC = {suggestion['suggested_arc_factor']:.6g}",
            ]
        )
    axes[0].set_title("Selected downsweep")
    axes[1].set_title("Downsweep in control-voltage axis")
    handles, labels = axes[1].get_legend_handles_labels()
    if handles:
        axes[1].legend(
            handles,
            labels,
            loc="center left",
            bbox_to_anchor=(1.02, 0.5),
            borderaxespad=0,
        )
    fig.text(0.5, 1.02, " | ".join(lines), ha="center", va="bottom", fontsize=15)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=300)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7870")
    parser.add_argument("--host", default="192.168.1.104")
    parser.add_argument("--tag", default="arc_factor_suggestion")
    parser.add_argument("--sweep-frequency", type=float, default=50.0)
    parser.add_argument("--sweep-amplitude", type=float, default=0.5)
    parser.add_argument("--duration", type=float, default=0.067108864)
    parser.add_argument("--depth-fraction", type=float, default=0.75)
    parser.add_argument("--min-width", type=float, default=0.02)
    parser.add_argument("--max-width", type=float, default=0.12)
    parser.add_argument("--min-arc", type=float, default=1.0)
    parser.add_argument("--max-arc", type=float, default=60.0)
    parser.add_argument("--max-fractional-step", type=float, default=0.25)
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = f"{args.tag}_{stamp}"

    arc_info = read_arc_factor(args.host)
    configure_prelock_sweep(args.base, args.duration, args.sweep_frequency, args.sweep_amplitude)
    time.sleep(0.2)
    capture = bridge_get(
        args.base, "/scope/single", {"tag": tag, "timeout": 8, "plot": "false"}
    )
    if not capture.get("ok"):
        raise RuntimeError(capture)

    npz_path = Path(capture["path"])
    data = np.load(npz_path)
    t = np.asarray(data["t"], dtype=float)
    ch1 = np.asarray(data["ch1"], dtype=float)
    ch2 = np.asarray(data["ch2"], dtype=float)

    analysis = analyze_apparent_width(t, ch1, ch2, args.depth_fraction)
    lock_analysis = analyze_apparent_width(t, ch1, ch2, 0.25)
    suggestion = None
    if analysis.get("ok"):
        suggestion = suggest_arc_factor(
            current_arc=float(arc_info["arc_factor"]),
            capture_width=float(analysis["chosen"]["capture_width_out2_v"]),
            min_width=args.min_width,
            max_width=args.max_width,
            min_arc=args.min_arc,
            max_arc=args.max_arc,
            max_fractional_step=args.max_fractional_step,
        )

    plot_path = RESULTS_DIR / f"{tag}_arc_width.png"
    make_plot(plot_path, t, ch1, ch2, analysis, arc_info, suggestion, lock_analysis)

    summary = {
        "ok": bool(analysis.get("ok")),
        "mode": "suggest_only_no_toptica_write",
        "arc_info": arc_info,
        "capture": capture,
        "analysis": analysis,
        "lockpoint_analysis": lock_analysis,
        "suggestion": suggestion,
        "plot_path": str(plot_path),
    }
    summary_path = RESULTS_DIR / f"{tag}_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
