"""Run PyRPL GUI with a tiny local control bridge.

This process owns exactly one PyRPL instance. The GUI and HTTP commands operate
on that same instance, so parameter changes can be observed in the GUI without
editing YAML files behind PyRPL's back.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import queue
import threading
import traceback
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np


SAFE_PARAMS = {
    "scope.input1",
    "scope.input2",
    "scope.duration",
    "scope.trigger_delay",
    "scope.trigger_source",
    "scope.threshold",
    "scope.hysteresis",
    "scope.rolling_mode",
    "scope.average",
}


READABLE_PREFIXES = (
    "scope.",
    "pid0.",
    "pid1.",
    "pid2.",
    "asg0.",
    "asg1.",
)

SESSION_DIR = Path(__file__).resolve().parents[1]
RESULTS_DIR = SESSION_DIR / "results" / "pyrpl_live_bridge"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start PyRPL GUI and expose a localhost parameter bridge."
    )
    parser.add_argument("--config", default="try", help="PyRPL config name without .yml")
    parser.add_argument("--hostname", default="192.168.1.34", help="Red Pitaya IP")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=7870)
    parser.add_argument("--loglevel", default="info")
    parser.add_argument(
        "--allow-risky",
        action="store_true",
        help="Allow writes outside the initial safe parameter whitelist.",
    )
    return parser.parse_args()


def coerce_value(text: str, current: Any | None) -> Any:
    lowered = text.strip().lower()
    if isinstance(current, bool) or lowered in {"true", "false"}:
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        raise ValueError(f"Cannot parse boolean value: {text!r}")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(text)
    if isinstance(current, float):
        return float(text)
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


class Bridge:
    def __init__(self, pyrpl_instance: Any, allow_risky: bool = False):
        self.p = pyrpl_instance
        self.allow_risky = allow_risky
        self.tasks: "queue.Queue[tuple[callable, threading.Event, dict[str, Any]]]" = (
            queue.Queue()
        )

    def submit(self, fn: callable, wait_timeout: float = 5.0) -> dict[str, Any]:
        done = threading.Event()
        box: dict[str, Any] = {}
        self.tasks.put((fn, done, box))
        if not done.wait(timeout=wait_timeout):
            return {"ok": False, "error": "Timed out waiting for PyRPL GUI thread"}
        return box

    def drain_once(self) -> None:
        while True:
            try:
                fn, done, box = self.tasks.get_nowait()
            except queue.Empty:
                break
            try:
                box.update(fn())
            except Exception as exc:  # pragma: no cover - interactive diagnostics
                box.update(
                    {
                        "ok": False,
                        "error": repr(exc),
                        "traceback": traceback.format_exc(limit=4),
                    }
                )
            finally:
                done.set()

    def resolve(self, dotted: str) -> tuple[Any, str]:
        if "." not in dotted:
            raise ValueError("Parameter must look like module.attribute")
        module_name, attr = dotted.split(".", 1)
        module = getattr(self.p.rp, module_name)
        return module, attr

    def get_param(self, dotted: str) -> dict[str, Any]:
        if not dotted.startswith(READABLE_PREFIXES):
            raise ValueError(f"Reading {dotted!r} is not in the bridge allowlist")
        module, attr = self.resolve(dotted)
        value = getattr(module, attr)
        return {"ok": True, "param": dotted, "value": value, "type": type(value).__name__}

    def set_param(self, dotted: str, raw_value: str) -> dict[str, Any]:
        if dotted not in SAFE_PARAMS and not self.allow_risky:
            raise ValueError(
                f"Refusing to write {dotted!r}. Start with --allow-risky only after "
                "the safe scope-parameter test is working."
            )
        module, attr = self.resolve(dotted)
        before = getattr(module, attr)
        value = coerce_value(raw_value, before)
        setattr(module, attr, value)
        after = getattr(module, attr)
        return {
            "ok": True,
            "param": dotted,
            "before": before,
            "written": value,
            "after": after,
            "type": type(after).__name__,
        }

    def capture_scope(
        self, tag: str = "scope_capture", timeout: float = 5.0, make_plot: bool = True
    ) -> dict[str, Any]:
        scope = self.p.rp.scope
        curve = scope.single(timeout=timeout)
        ch1 = np.asarray(curve[0], dtype=float)
        ch2 = np.asarray(curve[1], dtype=float)
        times = np.asarray(scope.times, dtype=float)
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        safe_tag = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)
        path = RESULTS_DIR / f"{safe_tag}.npz"
        np.savez(
            path,
            t=times,
            ch1=ch1,
            ch2=ch2,
            input1=str(scope.input1),
            input2=str(scope.input2),
            duration=float(scope.duration),
            trigger_source=str(scope.trigger_source),
        )
        analysis = analyze_scope_trace(times, ch1, ch2, str(scope.input1), str(scope.input2))
        plot_path = RESULTS_DIR / f"{safe_tag}_lockpoint.png"
        if make_plot:
            plot_result = make_lockpoint_plot(
                times=times,
                ch1=ch1,
                ch2=ch2,
                input1=str(scope.input1),
                input2=str(scope.input2),
                analysis=analysis,
                path=plot_path,
                title=safe_tag,
            )
        else:
            plot_result = {"ok": False, "skipped": True, "reason": "plot=false"}
        return {
            "ok": True,
            "path": str(path),
            "plot_path": str(plot_path) if plot_result.get("ok") else None,
            "plot": plot_result,
            "n": int(len(times)),
            "input1": str(scope.input1),
            "input2": str(scope.input2),
            "duration": float(scope.duration),
            "trigger_source": str(scope.trigger_source),
            "analysis": analysis,
        }

    def catch_lock(
        self,
        tag: str = "catch_lock",
        branch: str = "right",
        p_gain: float = -0.002,
        i_gain: float = -0.004632,
        timeout: float = 5.0,
        monitor_seconds: float = 3.0,
        monitor_interval: float = 0.1,
        sweep_frequency: float = 50.0,
        sweep_amplitude: float = 0.5,
        handoff_offset: float = 0.0,
        timed: bool = False,
        phase_delay_offset: float = 0.0,
    ) -> dict[str, Any]:
        rp = self.p.rp
        scope = rp.scope
        asg0 = rp.asg0
        asg1 = rp.asg1
        pid0 = rp.pid0

        pid0.p = 0
        pid0.i = 0
        pid0.output_direct = "off"
        asg0.output_direct = "off"

        asg0.waveform = "ramp"
        asg0.amplitude = sweep_amplitude
        asg0.offset = 0
        asg0.frequency = sweep_frequency
        asg1.waveform = "square"
        asg1.amplitude = 0.5
        asg1.offset = 0
        asg1.frequency = sweep_frequency
        asg1.output_direct = "off"
        scope.input1 = "in1"
        scope.input2 = "out2"
        scope.trigger_source = "asg1"
        scope.trigger_delay = 0
        scope.duration = 0.067108864
        scope.rolling_mode = False

        asg0.output_direct = "out2"
        curve = scope.single(timeout=timeout)
        ch1 = np.asarray(curve[0], dtype=float)
        ch2 = np.asarray(curve[1], dtype=float)
        times = np.asarray(scope.times, dtype=float)
        analysis = analyze_scope_trace(times, ch1, ch2, str(scope.input1), str(scope.input2))
        lock = analysis.get("quarter_lockpoint", {})
        if not lock.get("ok"):
            asg0.output_direct = "off"
            return {"ok": False, "error": "Could not compute lock point", "analysis": analysis}

        point = lock.get(f"{branch}_lockpoint")
        if not point:
            asg0.output_direct = "off"
            return {
                "ok": False,
                "error": f"No {branch!r} lockpoint in sweep trace",
                "analysis": analysis,
            }

        target = float(lock["transmission_lock_quarter"])
        capture_voltage = float(point["sweep_voltage"])
        handoff_voltage = capture_voltage + handoff_offset
        phase_delay = 0.0
        if timed:
            period = 1.0 / sweep_frequency
            trace_end = float(np.nanmax(times))
            cross_time = float(point["time"])
            phase_delay = (cross_time - trace_end) % period
            phase_delay = max(0.0, phase_delay + phase_delay_offset)
        safe_tag = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        path = RESULTS_DIR / f"{safe_tag}.npz"
        plot_path = RESULTS_DIR / f"{safe_tag}_lockpoint.png"
        np.savez(
            path,
            t=times,
            ch1=ch1,
            ch2=ch2,
            input1=str(scope.input1),
            input2=str(scope.input2),
            duration=float(scope.duration),
            trigger_source=str(scope.trigger_source),
            branch=branch,
            capture_voltage=capture_voltage,
            handoff_offset=handoff_offset,
            handoff_voltage=handoff_voltage,
            timed=timed,
            phase_delay=phase_delay,
            phase_delay_offset=phase_delay_offset,
            target=target,
            p_gain=p_gain,
            i_gain=i_gain,
        )
        make_lockpoint_plot(
            times=times,
            ch1=ch1,
            ch2=ch2,
            input1=str(scope.input1),
            input2=str(scope.input2),
            analysis=analysis,
            path=plot_path,
            title=safe_tag,
        )

        if timed and phase_delay > 0:
            time.sleep(phase_delay)
        asg0.output_direct = "off"
        pid0.setpoint = target
        pid0.p = 0
        pid0.i = 0
        pid0.ival = handoff_voltage
        pid0.output_direct = "out2"
        pid0.p = p_gain
        pid0.i = i_gain

        monitor = []
        start = time.time()
        while time.time() - start < monitor_seconds:
            monitor.append(
                {
                    "t_s": float(time.time() - start),
                    "ch1": float(scope.voltage_in1),
                    "ch2": float(scope.voltage_in2),
                    "ival": float(pid0.ival),
                    "p": float(pid0.p),
                    "i": float(pid0.i),
                }
            )
            time.sleep(monitor_interval)

        ch1_values = np.asarray([row["ch1"] for row in monitor], dtype=float)
        ch2_values = np.asarray([row["ch2"] for row in monitor], dtype=float)
        summary = {
            "n": int(ch1_values.size),
            "ch1_mean": float(np.mean(ch1_values)) if ch1_values.size else None,
            "ch1_min": float(np.min(ch1_values)) if ch1_values.size else None,
            "ch1_max": float(np.max(ch1_values)) if ch1_values.size else None,
            "ch1_ptp": float(np.ptp(ch1_values)) if ch1_values.size else None,
            "ch2_mean": float(np.mean(ch2_values)) if ch2_values.size else None,
            "ch2_min": float(np.min(ch2_values)) if ch2_values.size else None,
            "ch2_max": float(np.max(ch2_values)) if ch2_values.size else None,
            "ch2_ptp": float(np.ptp(ch2_values)) if ch2_values.size else None,
            "target_error_mean": float(np.mean(ch1_values) - target) if ch1_values.size else None,
        }
        return {
            "ok": True,
            "path": str(path),
            "plot_path": str(plot_path),
            "branch": branch,
            "target": target,
            "capture_voltage": capture_voltage,
            "handoff_offset": handoff_offset,
            "handoff_voltage": handoff_voltage,
            "timed": timed,
            "phase_delay": phase_delay,
            "phase_delay_offset": phase_delay_offset,
            "p_gain_requested": p_gain,
            "i_gain_requested": i_gain,
            "p_gain_readback": float(pid0.p),
            "i_gain_readback": float(pid0.i),
            "ival_readback": float(pid0.ival),
            "monitor_summary": summary,
            "monitor": monitor,
            "analysis": analysis,
        }


def channel_stats(y: np.ndarray) -> dict[str, Any]:
    finite = y[np.isfinite(y)]
    if finite.size == 0:
        return {"ok": False}
    dy = np.diff(finite)
    yrange = float(np.max(finite) - np.min(finite))
    rms_diff = float(np.sqrt(np.mean(dy * dy))) if dy.size else 0.0
    return {
        "mean": float(np.mean(finite)),
        "min": float(np.min(finite)),
        "max": float(np.max(finite)),
        "ptp": yrange,
        "std": float(np.std(finite)),
        "rms_diff": rms_diff,
        "smoothness": float(yrange / (rms_diff + 1e-12)),
    }


def extrema_features(y: np.ndarray) -> dict[str, Any]:
    finite = np.asarray(y, dtype=float)
    if finite.size < 16 or not np.all(np.isfinite(finite)):
        return {"prominence_pos": 0.0, "prominence_neg": 0.0, "width_frac": None}
    baseline = float(np.median(finite))
    p5, p95 = np.percentile(finite, [5, 95])
    y_max = float(np.max(finite))
    y_min = float(np.min(finite))
    prom_pos = float((y_max - p95) / (np.ptp(finite) + 1e-12))
    prom_neg = float((p5 - y_min) / (np.ptp(finite) + 1e-12))
    if (y_max - baseline) >= (baseline - y_min):
        half = baseline + 0.5 * (y_max - baseline)
        width = int(np.count_nonzero(finite > half))
        polarity = "peak"
    else:
        half = baseline - 0.5 * (baseline - y_min)
        width = int(np.count_nonzero(finite < half))
        polarity = "dip"
    return {
        "polarity": polarity,
        "prominence_pos": prom_pos,
        "prominence_neg": prom_neg,
        "width_frac": float(width / finite.size),
    }


def quarter_lockpoint_features(t: np.ndarray, transmission: np.ndarray, sweep: np.ndarray) -> dict[str, Any]:
    y = np.asarray(transmission, dtype=float)
    x = np.asarray(sweep, dtype=float)
    tt = np.asarray(t, dtype=float)
    if y.size < 4 or y.size != x.size:
        return {"ok": False, "error": "invalid arrays"}
    idx_min = int(np.nanargmin(y))
    t_min = float(y[idx_min])
    t_max = float(np.nanmax(y))
    t_lock = float(t_min + (t_max - t_min) / 4.0)

    crossings = []
    shifted = y - t_lock
    for i in range(y.size - 1):
        if not np.isfinite(shifted[i]) or not np.isfinite(shifted[i + 1]):
            continue
        if shifted[i] == 0 or shifted[i] * shifted[i + 1] < 0:
            denom = abs(shifted[i]) + abs(shifted[i + 1])
            frac = abs(shifted[i]) / denom if denom else 0.0
            crossings.append(
                {
                    "index": int(i),
                    "time": float(tt[i] * (1 - frac) + tt[i + 1] * frac),
                    "sweep_voltage": float(x[i] * (1 - frac) + x[i + 1] * frac),
                    "transmission": t_lock,
                }
            )
    left = [c for c in crossings if c["index"] < idx_min]
    right = [c for c in crossings if c["index"] > idx_min]
    left_pick = left[-1] if left else None
    right_pick = right[0] if right else None
    return {
        "ok": True,
        "transmission_min": t_min,
        "transmission_max": t_max,
        "transmission_lock_quarter": t_lock,
        "dip_index": idx_min,
        "dip_time": float(tt[idx_min]),
        "dip_sweep_voltage": float(x[idx_min]),
        "left_lockpoint": left_pick,
        "right_lockpoint": right_pick,
        "num_crossings": len(crossings),
    }


def triangle_similarity(t: np.ndarray, y: np.ndarray, frequency: float = 50.0) -> float:
    if t.size != y.size or t.size < 16:
        return 0.0
    yy = y - np.mean(y)
    if float(np.std(yy)) <= 1e-12:
        return 0.0
    phase = ((t - t[0]) * frequency) % 1.0
    tri = 1.0 - 4.0 * np.abs(phase - 0.5)
    tri -= np.mean(tri)
    return float(abs(np.corrcoef(yy, tri)[0, 1]))


def analyze_scope_trace(
    t: np.ndarray, ch1: np.ndarray, ch2: np.ndarray, input1: str, input2: str
) -> dict[str, Any]:
    stats = {"ch1": channel_stats(ch1), "ch2": channel_stats(ch2)}
    extrema = {"ch1": extrema_features(ch1), "ch2": extrema_features(ch2)}
    tri = {
        "ch1": triangle_similarity(t, ch1),
        "ch2": triangle_similarity(t, ch2),
    }
    sweep_ch = "ch1" if tri["ch1"] >= tri["ch2"] else "ch2"
    other_ch = "ch2" if sweep_ch == "ch1" else "ch1"
    # A Lorentzian transmission channel should usually be less triangular than
    # the control signal and have a localized peak/dip or otherwise meaningful
    # non-triangular variation.
    lorentz_ch = other_ch
    if input1 in {"out1", "out2", "asg0", "asg1"} and input2 not in {"out1", "out2", "asg0", "asg1"}:
        sweep_ch, lorentz_ch = "ch1", "ch2"
    if input2 in {"out1", "out2", "asg0", "asg1"} and input1 not in {"out1", "out2", "asg0", "asg1"}:
        sweep_ch, lorentz_ch = "ch2", "ch1"
    transmission = ch1 if lorentz_ch == "ch1" else ch2
    sweep = ch1 if sweep_ch == "ch1" else ch2
    lockpoint = quarter_lockpoint_features(t, transmission, sweep)
    return {
        "stats": stats,
        "extrema": extrema,
        "triangle_similarity": tri,
        "likely_sweep_channel": sweep_ch,
        "likely_sweep_input": input1 if sweep_ch == "ch1" else input2,
        "likely_transmission_channel": lorentz_ch,
        "likely_transmission_input": input1 if lorentz_ch == "ch1" else input2,
        "quarter_lockpoint": lockpoint,
        "note": (
            "Classification combines scope input labels with 50 Hz triangle-wave "
            "similarity and localized peak/dip features."
        ),
    }


def make_lockpoint_plot(
    times: np.ndarray,
    ch1: np.ndarray,
    ch2: np.ndarray,
    input1: str,
    input2: str,
    analysis: dict[str, Any],
    path: Path,
    title: str,
) -> dict[str, Any]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:
        return {"ok": False, "error": f"matplotlib unavailable: {exc!r}"}

    sweep_ch = analysis["likely_sweep_channel"]
    trans_ch = analysis["likely_transmission_channel"]
    sweep = ch1 if sweep_ch == "ch1" else ch2
    transmission = ch1 if trans_ch == "ch1" else ch2
    sweep_label = input1 if sweep_ch == "ch1" else input2
    trans_label = input1 if trans_ch == "ch1" else input2
    lock = analysis.get("quarter_lockpoint", {})

    fig, axes = plt.subplots(2, 2, figsize=(12, 7))
    t_ms = times * 1e3

    axes[0, 0].plot(t_ms, ch1, color="tab:green", lw=1.0)
    axes[0, 0].set_title(f"CH1 / {input1}")
    axes[0, 0].set_xlabel("Time relative to trigger (ms)")
    axes[0, 0].set_ylabel("Voltage (V)")
    axes[0, 0].grid(True, alpha=0.25)

    axes[0, 1].plot(t_ms, ch2, color="tab:red", lw=1.0)
    axes[0, 1].set_title(f"CH2 / {input2}")
    axes[0, 1].set_xlabel("Time relative to trigger (ms)")
    axes[0, 1].set_ylabel("Voltage (V)")
    axes[0, 1].grid(True, alpha=0.25)

    axes[1, 0].plot(sweep, transmission, color="tab:blue", lw=1.0)
    axes[1, 0].set_title(f"Transmission vs sweep ({trans_ch}/{trans_label} vs {sweep_ch}/{sweep_label})")
    axes[1, 0].set_xlabel(f"Sweep voltage {sweep_ch}/{sweep_label} (V)")
    axes[1, 0].set_ylabel(f"Transmission {trans_ch}/{trans_label} (V)")
    axes[1, 0].grid(True, alpha=0.25)

    if lock.get("ok"):
        y_lock = lock["transmission_lock_quarter"]
        axes[1, 0].axhline(y_lock, color="tab:orange", ls="--", lw=1.2, label="1/4 lock level")
        for name, marker, color in (
            ("left_lockpoint", "o", "tab:purple"),
            ("right_lockpoint", "s", "tab:brown"),
        ):
            point = lock.get(name)
            if point:
                axes[1, 0].scatter(
                    [point["sweep_voltage"]],
                    [point["transmission"]],
                    marker=marker,
                    color=color,
                    s=45,
                    label=name.replace("_", " "),
                    zorder=5,
                )
        axes[1, 0].legend(loc="best", fontsize=8)

    axes[1, 1].axis("off")
    lines = [
        f"Capture: {title}",
        f"Likely transmission: {trans_ch}/{trans_label}",
        f"Likely sweep: {sweep_ch}/{sweep_label}",
        f"Triangle similarity: CH1={analysis['triangle_similarity']['ch1']:.3f}, CH2={analysis['triangle_similarity']['ch2']:.3f}",
    ]
    if lock.get("ok"):
        lines.extend(
            [
                f"T_min = {lock['transmission_min']:.6f} V",
                f"T_max = {lock['transmission_max']:.6f} V",
                f"T_lock = Tmin + (Tmax-Tmin)/4 = {lock['transmission_lock_quarter']:.6f} V",
                f"Dip sweep voltage = {lock['dip_sweep_voltage']:.6f} V",
            ]
        )
        for label, point in (
            ("Left 1/4", lock.get("left_lockpoint")),
            ("Right 1/4", lock.get("right_lockpoint")),
        ):
            if point:
                lines.append(
                    f"{label}: sweep={point['sweep_voltage']:.6f} V, time={point['time']*1e3:.3f} ms"
                )
    axes[1, 1].text(0.0, 1.0, "\n".join(lines), va="top", ha="left", family="monospace", fontsize=9)

    fig.suptitle("Pre-lock sweep capture and 1/4-depth lockpoint", fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return {"ok": True, "path": str(path)}


def make_handler(bridge: Bridge) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            try:
                if parsed.path == "/health":
                    payload = {
                        "ok": True,
                        "message": "pyrpl_live_bridge is running",
                        "safe_params": sorted(SAFE_PARAMS),
                    }
                elif parsed.path == "/get":
                    param = qs.get("param", [""])[0]
                    payload = bridge.submit(lambda: bridge.get_param(param))
                elif parsed.path == "/set":
                    param = qs.get("param", [""])[0]
                    value = qs.get("value", [""])[0]
                    payload = bridge.submit(lambda: bridge.set_param(param, value))
                elif parsed.path == "/scope/single":
                    tag = qs.get("tag", ["scope_capture"])[0]
                    timeout = float(qs.get("timeout", ["5"])[0])
                    make_plot = qs.get("plot", ["true"])[0].strip().lower() not in {
                        "0",
                        "false",
                        "no",
                        "off",
                    }
                    payload = bridge.submit(
                        lambda: bridge.capture_scope(tag, timeout, make_plot),
                        wait_timeout=max(10.0, timeout + 5.0),
                    )
                elif parsed.path == "/lock/catch":
                    tag = qs.get("tag", ["catch_lock"])[0]
                    branch = qs.get("branch", ["right"])[0]
                    p_gain = float(qs.get("p", ["-0.002"])[0])
                    i_gain = float(qs.get("i", ["-0.004632"])[0])
                    timeout = float(qs.get("timeout", ["5"])[0])
                    monitor_seconds = float(qs.get("monitor", ["3"])[0])
                    monitor_interval = float(qs.get("interval", ["0.1"])[0])
                    handoff_offset = float(qs.get("offset", ["0"])[0])
                    timed = qs.get("timed", ["false"])[0].strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "on",
                    }
                    phase_delay_offset = float(qs.get("phase_offset", ["0"])[0])
                    payload = bridge.submit(
                        lambda: bridge.catch_lock(
                            tag=tag,
                            branch=branch,
                            p_gain=p_gain,
                            i_gain=i_gain,
                            timeout=timeout,
                            monitor_seconds=monitor_seconds,
                            monitor_interval=monitor_interval,
                            handoff_offset=handoff_offset,
                            timed=timed,
                            phase_delay_offset=phase_delay_offset,
                        ),
                        wait_timeout=max(15.0, timeout + monitor_seconds + 8.0),
                    )
                else:
                    payload = {
                        "ok": True,
                        "usage": {
                            "health": "/health",
                            "get": "/get?param=scope.duration",
                            "set": "/set?param=scope.duration&value=0.1",
                            "scope_single": "/scope/single?tag=test",
                            "catch_lock": "/lock/catch?tag=test&branch=right&p=-0.002&i=-0.004632",
                        },
                    }
                status = 200 if payload.get("ok") else 400
                self.send_json(status, payload)
            except Exception as exc:
                self.send_json(400, {"ok": False, "error": repr(exc)})

    return Handler


def main() -> int:
    args = parse_args()

    # Import PyRPL only after argument parsing so --help never creates PyRPL
    # user config files or touches the Red Pitaya.
    import pyrpl
    from qtpy import QtCore

    print(f"Starting PyRPL {pyrpl.__version__} config={args.config!r}")
    print(f"Connecting to Red Pitaya at {args.hostname}")
    p = pyrpl.Pyrpl(
        config=args.config,
        hostname=args.hostname,
        gui=True,
        loglevel=args.loglevel,
    )

    bridge = Bridge(p, allow_risky=args.allow_risky)
    server = ThreadingHTTPServer(
        (args.listen_host, args.listen_port), make_handler(bridge)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Bridge listening on http://{args.listen_host}:{args.listen_port}")
    print("Initial safe write example: /set?param=scope.duration&value=0.1")

    timer = QtCore.QTimer()
    timer.timeout.connect(bridge.drain_once)
    timer.start(50)

    def shutdown() -> None:
        server.shutdown()
        server.server_close()
        try:
            p._clear()
        except Exception:
            pass

    pyrpl.APP.aboutToQuit.connect(shutdown)
    return pyrpl.APP.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
