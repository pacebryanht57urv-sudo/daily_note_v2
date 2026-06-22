"""Local dashboard for stable microcavity Q/lock operations.

This dashboard intentionally stays thin: it does not own PyRPL or TOPTICA
state. It calls the existing bridge and locking scripts so the operational
logic stays in one place.
"""

from __future__ import annotations

import argparse
import csv
import html
import importlib.util
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen

import numpy as np


SCRIPT_PATH = Path(__file__).resolve()
DASHBOARD_DIR = SCRIPT_PATH.parent
SRC_DIR = DASHBOARD_DIR.parent
PACKAGE_DIR = SRC_DIR.parent
REPO_ROOT = PACKAGE_DIR.parents[2]
LOCK_DIR = SRC_DIR / "lock"
BRIDGE_DIR = SRC_DIR / "bridge"
DRIVERS_DIR = SRC_DIR / "drivers"
COMMON_DIR = SRC_DIR / "common"
CONFIG_DIR = PACKAGE_DIR / "config"
DEFAULT_LOCK_SCRIPT = LOCK_DIR / "current_mode_fast_lock.py"
DEFAULT_WEIYUAN_LOCK_SCRIPT = LOCK_DIR / "weiyuan_current_mode_lock.py"
DEFAULT_BEST_Q_SCRIPT = LOCK_DIR / "lock_best_q_mode.py"
LARGE_SCAN_DIR = REPO_ROOT / "workspace" / "scripts" / "microcavity_large_scan"
AUTO_COUPLING_DIR = REPO_ROOT / "workspace" / "scripts" / "auto_coupling_mdt693b"
DEFAULT_AUTO_COUPLING_SCRIPT = AUTO_COUPLING_DIR / "monotonic_step_optimize_coupling.py"
DEFAULT_LARGE_SCAN_SCRIPT = REPO_ROOT / "workspace" / "scripts" / "microcavity_large_scan" / "large_scan_flow.py"
DEFAULT_CARD_SCRIPT = REPO_ROOT / "workspace" / "scripts" / "microcavity_large_scan" / "write_cavity_card.py"
DEFAULT_TOPTICA_PYTHON = Path(r"C:\Users\win10\toptica_lasersdk_venv\Scripts\python.exe")
DEFAULT_RP_HOST = "RP-f0cb0d"
DEFAULT_LASER_TYPE = "toptica_serial"
DEFAULT_LARGE_SCAN_LASER_PORT = "COM3"
DEFAULT_SCOPE_RESOURCE = "TCPIP::192.168.1.8::INSTR"
DEFAULT_LOCK_SWEEP_FREQUENCY_HZ = 50.0
DEFAULT_LOCK_SWEEP_AMPLITUDE_V = 1.0
DEFAULT_LOCK_SCOPE_DURATION_S = 0.067108864
DEFAULT_CAVITY: Path | None = None
ACTIVE_LARGE_SCAN: dict[str, Any] = {"proc": None}
ACTIVE_LARGE_SCAN_LOCK = threading.Lock()
ACTIVE_BRIDGE: dict[str, Any] = {"proc": None}
ACTIVE_BRIDGE_LOCK = threading.Lock()
ACTIVE_SENSITIVITY: dict[str, Any] = {"running": False, "cancel_requested": False}
ACTIVE_SENSITIVITY_LOCK = threading.Lock()

DEFAULT_BRIDGE_CONFIG = "try_bridge_safe"
DEFAULT_PYRPL_CONFIG_TEMPLATE = CONFIG_DIR / "pyrpl_configs" / f"{DEFAULT_BRIDGE_CONFIG}.yml"
DEFAULT_BRIDGE_LISTEN_HOST = "127.0.0.1"
DEFAULT_BRIDGE_LISTEN_PORT = 7870
DEFAULT_RP_F0CB0D_EXTERNAL_GAIN_DB = 23.0
DEFAULT_CONFIG_FILE = PACKAGE_DIR / "config.local.json"
RUNTIME_CONFIG_FILE = PACKAGE_DIR / "runtime.local.json"
DEFAULT_PRESSURE_CALIBRATION_ROOT = (
    REPO_ROOT
    / "workspace"
    / "experiments"
    / "calibrations"
    / "ultrasound_sources"
)
DEFAULT_PRESSURE_SOURCE = "OLYMPUS_V103_RB"
DEFAULT_PRESSURE_CALIBRATION_FILE = "1MHz_pressure_10Vpp_10k-1M_100Hzbest.npz"
DEFAULT_PRESSURE_CALIBRATION = (
    DEFAULT_PRESSURE_CALIBRATION_ROOT
    / DEFAULT_PRESSURE_SOURCE
    / "processed"
    / DEFAULT_PRESSURE_CALIBRATION_FILE
)


def configured_runtime_python() -> str | None:
    env_python = os.environ.get("MICROCAVITY_RUNTIME_PYTHON")
    if env_python:
        return env_python
    if not RUNTIME_CONFIG_FILE.exists():
        return None
    try:
        config = json.loads(RUNTIME_CONFIG_FILE.read_text(encoding="utf-8-sig"))
        value = config.get("runtime_python")
        return str(value) if value else None
    except Exception:
        return None


def package_python_executable() -> str:
    """Use the installed runtime for child scripts unless explicitly overridden."""
    if os.environ.get("MICROCAVITY_USE_EXTERNAL_PYTHON") == "1":
        external = os.environ.get("PYTHON_EXE")
        if external:
            return external
    configured = configured_runtime_python()
    if configured and Path(configured).exists():
        return configured
    return sys.executable


def bridge_log_dir() -> Path:
    return Path(os.environ.get("MICROCAVITY_BRIDGE_LOG_DIR") or Path(tempfile.gettempdir()) / "redpitaya_microcavity_lock")


def bridge_ready_timeout_s() -> float:
    raw = os.environ.get("MICROCAVITY_BRIDGE_READY_TIMEOUT_S")
    try:
        timeout = float(raw) if raw else 60.0
    except ValueError:
        timeout = 60.0
    return max(10.0, timeout)


def read_text_tail(path: str | Path | None, limit_bytes: int = 4000) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        with p.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit_bytes), os.SEEK_SET)
            return handle.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return f"<failed to read log tail: {exc!r}>"


def pyrpl_user_config_dir() -> Path:
    try:
        from pyrpl import memory

        value = getattr(memory, "user_config_dir", None)
        if value:
            return Path(value)
    except Exception:
        pass
    return Path.home() / "pyrpl_user_dir" / "config"


def ensure_pyrpl_bridge_config(rp_host: str, timeout_s: float = 5.0) -> dict[str, Any]:
    config_dir = pyrpl_user_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / f"{DEFAULT_BRIDGE_CONFIG}.yml"
    actions: list[str] = []
    if not config_path.exists():
        if not DEFAULT_PYRPL_CONFIG_TEMPLATE.exists():
            return {
                "ok": False,
                "config_path": str(config_path),
                "error": f"missing template {DEFAULT_PYRPL_CONFIG_TEMPLATE}",
            }
        config_path.write_text(
            DEFAULT_PYRPL_CONFIG_TEMPLATE.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        actions.append("created_from_template")

    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    out: list[str] = []
    in_redpitaya = False
    in_spectrumanalyzer = False
    hostname_seen = False
    reloadserver_seen = False
    timeout_seen = False
    spectrum_window_seen = False
    changed = False
    for line in lines:
        stripped = line.strip()
        if line and not line.startswith(" ") and stripped.endswith(":"):
            if in_redpitaya and not hostname_seen:
                out.append(f"  hostname: {rp_host}")
                hostname_seen = True
                changed = True
            if in_redpitaya and not reloadserver_seen:
                out.append("  reloadserver: true")
                reloadserver_seen = True
                changed = True
            if in_redpitaya and not timeout_seen:
                out.append(f"  timeout: {timeout_s:g}")
                timeout_seen = True
                changed = True
            if in_spectrumanalyzer and not spectrum_window_seen:
                out.append("  window: blackman")
                spectrum_window_seen = True
                changed = True
            in_redpitaya = stripped == "redpitaya:"
            in_spectrumanalyzer = stripped == "spectrumanalyzer:"
        if in_redpitaya and stripped.startswith("hostname:"):
            new_line = f"  hostname: {rp_host}"
            out.append(new_line)
            hostname_seen = True
            if line != new_line:
                changed = True
            continue
        if in_redpitaya and stripped.startswith("reloadserver:"):
            new_line = "  reloadserver: true"
            out.append(new_line)
            reloadserver_seen = True
            if line != new_line:
                changed = True
            continue
        if in_redpitaya and stripped.startswith("timeout:"):
            new_line = f"  timeout: {timeout_s:g}"
            out.append(new_line)
            timeout_seen = True
            if line != new_line:
                changed = True
            continue
        if in_spectrumanalyzer and stripped.startswith("window:"):
            value = stripped.split(":", 1)[1].strip().strip("'\"").lower()
            new_line = line
            if value not in {"blackman", "flattop", "boxcar", "hamming"}:
                new_line = "  window: blackman"
            out.append(new_line)
            spectrum_window_seen = True
            if line != new_line:
                changed = True
            continue
        out.append(line)
    if in_redpitaya and not hostname_seen:
        out.append(f"  hostname: {rp_host}")
        hostname_seen = True
        changed = True
    if in_redpitaya and not reloadserver_seen:
        out.append("  reloadserver: true")
        reloadserver_seen = True
        changed = True
    if in_redpitaya and not timeout_seen:
        out.append(f"  timeout: {timeout_s:g}")
        timeout_seen = True
        changed = True
    if in_spectrumanalyzer and not spectrum_window_seen:
        out.append("  window: blackman")
        spectrum_window_seen = True
        changed = True
    if changed:
        config_path.write_text("\n".join(out) + "\n", encoding="utf-8")
        actions.append("updated_pyrpl_bridge_config")
    return {
        "ok": True,
        "config_path": str(config_path),
        "hostname": rp_host,
        "reloadserver": True,
        "timeout_s": timeout_s,
        "spectrumanalyzer_window": "blackman",
        "actions": actions,
    }

PD_MODELS: dict[str, dict[str, float]] = {
    "KY-PRM-10M-I-FA": {
        "dc_response_v_per_w_1mohm": 2.0e5,
        "rf_response_v_per_w_50ohm": 1.0e5,
        "saturation_power_uw": 25.0,
    },
    "KY-PRM-10M-I-FC": {
        "dc_response_v_per_w_1mohm": 4.0e4,
        "rf_response_v_per_w_50ohm": 2.0e4,
        "saturation_power_uw": 125.0,
    },
}

BIAS_TEE_MODELS: dict[str, dict[str, float]] = {
    "ZFBT-4R2GW+": {"rf_loss_db": 0.6},
}

AMPLIFIER_MODELS: dict[str, dict[str, float]] = {
    # Use the measured in-chain value as the default gain for this lab setup.
    "ZX60-43-S+": {"gain_db": 23.0, "noise_figure_db": 5.8},
}

LOCAL_MODULE_DIRS = (DASHBOARD_DIR, LOCK_DIR, DRIVERS_DIR, COMMON_DIR, BRIDGE_DIR)
for module_dir in LOCAL_MODULE_DIRS:
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))


def load_local_module(module_name: str) -> Any:
    module_path = None
    for module_dir in LOCAL_MODULE_DIRS:
        candidate = module_dir / f"{module_name}.py"
        if candidate.exists():
            module_path = candidate
            break
    if module_path is None:
        searched = ", ".join(str(path) for path in LOCAL_MODULE_DIRS)
        raise FileNotFoundError(
            f"Missing {module_name}.py. Searched: {searched}. "
            "Copy the complete redpitaya_microcavity_lock folder."
        )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load local module {module_name!r} from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_config_file(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Dashboard config must be a JSON object: {path}")
    return data


def bool_from_config(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def config_path(value: Any) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(str(value))))


def finite_config_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def read_pressure_source_meta(source_dir: Path) -> dict[str, Any] | None:
    meta_path = source_dir / "calibration.meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if not isinstance(meta, dict):
        return None
    default_file = str(meta.get("default_processed_file") or "").strip()
    pressure_path = (source_dir / default_file).resolve() if default_file else None
    return {
        "id": str(meta.get("id") or source_dir.name),
        "label": str(meta.get("label") or meta.get("model") or source_dir.name),
        "model": str(meta.get("model") or meta.get("label") or source_dir.name),
        "path": str(pressure_path) if pressure_path else "",
        "exists": bool(pressure_path and pressure_path.exists()),
        "center_frequency_hz": finite_config_float(meta.get("center_frequency_hz"), None),
        "frequency_min_hz": finite_config_float(meta.get("frequency_min_hz"), None),
        "frequency_max_hz": finite_config_float(meta.get("frequency_max_hz"), None),
        "calibration_drive_vpp": finite_config_float(meta.get("calibration_drive_vpp"), None),
        "pressure_quantity": str(meta.get("pressure_quantity") or "pk"),
        "meta_path": str(meta_path),
        "note": str(meta.get("note") or ""),
    }


def list_pressure_sources(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    sources: list[dict[str, Any]] = []
    for source_dir in sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name.lower()):
        meta = read_pressure_source_meta(source_dir)
        if meta:
            sources.append(meta)
    return sources


def default_pressure_calibration_from_source(root: Path, source_model: str | None) -> Path:
    source_id = source_model or DEFAULT_PRESSURE_SOURCE
    meta = read_pressure_source_meta(root / source_id)
    if meta and meta.get("path"):
        return Path(str(meta["path"]))
    return DEFAULT_PRESSURE_CALIBRATION


def voltage_ratio_from_db(db_value: float | None) -> float:
    if db_value is None:
        return 1.0
    return 10.0 ** (float(db_value) / 20.0)


def model_record(table: dict[str, dict[str, float]], model: str | None) -> dict[str, float]:
    if not model:
        return {}
    return table.get(str(model).strip(), {})


def resolve_device_profile(config: dict[str, Any]) -> dict[str, Any]:
    pd_cfg = config.get("photodetector") or config.get("pd") or {}
    if not isinstance(pd_cfg, dict):
        pd_cfg = {}
    rp_frontend = config.get("rp_frontend") or {}
    if not isinstance(rp_frontend, dict):
        rp_frontend = {}
    dc_path = rp_frontend.get("dc_path") or {}
    if not isinstance(dc_path, dict):
        dc_path = {}
    rf_path = rp_frontend.get("rf_path") or {}
    if not isinstance(rf_path, dict):
        rf_path = {}

    pd_model = str(pd_cfg.get("model") or "").strip()
    pd_model_info = model_record(PD_MODELS, pd_model)
    dc_response = finite_config_float(
        pd_cfg.get("scope_response_v_per_w")
        or pd_cfg.get("rp_scope_response_v_per_w")
        or config.get("scope_response_v_per_w")
    )
    if dc_response is None:
        base_response = finite_config_float(
            pd_cfg.get("dc_response_v_per_w_1mohm"),
            pd_model_info.get("dc_response_v_per_w_1mohm"),
        )
        dc_attenuator_db = finite_config_float(dc_path.get("attenuator_db"), 0.0)
        dc_gain_db = finite_config_float(dc_path.get("gain_db"), 0.0)
        dc_response = (
            base_response
            * voltage_ratio_from_db(-(dc_attenuator_db or 0.0))
            * voltage_ratio_from_db(dc_gain_db or 0.0)
            if base_response is not None
            else 3.22013e3
        )

    ch1_response = finite_config_float(
        config.get("scope_ch1_response_v_per_w")
        or pd_cfg.get("scope_ch1_response_v_per_w"),
        dc_response,
    )
    ch2_response = finite_config_float(
        config.get("scope_ch2_response_v_per_w")
        or pd_cfg.get("scope_ch2_response_v_per_w"),
        dc_response,
    )
    scope_zero_cfg = (
        config.get("scope_zero")
        or dc_path.get("scope_zero")
        or rp_frontend.get("scope_zero")
        or {}
    )
    if not isinstance(scope_zero_cfg, dict):
        scope_zero_cfg = {}
    scope_zero_enabled = bool_from_config(scope_zero_cfg.get("enabled"), False)
    ch1_zero_offset = finite_config_float(
        scope_zero_cfg.get("ch1_offset_v")
        or scope_zero_cfg.get("input1_offset_v"),
        0.0,
    )
    ch2_zero_offset = finite_config_float(
        scope_zero_cfg.get("ch2_offset_v")
        or scope_zero_cfg.get("input2_offset_v"),
        0.0,
    )

    bias_enabled = bool_from_config(rf_path.get("bias_tee_enabled"), False)
    amp_enabled = bool_from_config(rf_path.get("amplifier_enabled"), False)
    bias_model = str(rf_path.get("bias_tee_model") or "").strip()
    amp_model = str(rf_path.get("amplifier_model") or "").strip()
    bias_info = model_record(BIAS_TEE_MODELS, bias_model)
    amp_info = model_record(AMPLIFIER_MODELS, amp_model)

    explicit_external_gain = finite_config_float(
        rf_path.get("external_gain_db")
        or rf_path.get("rf_external_gain_db")
        or config.get("spectrum_external_gain_db")
    )
    if explicit_external_gain is not None:
        external_gain_db = explicit_external_gain
        external_gain_source = "config rf_path.external_gain_db"
    elif "rp_frontend" in config:
        external_gain_db = finite_config_float(rf_path.get("extra_gain_db"), 0.0) or 0.0
        if bias_enabled:
            external_gain_db -= finite_config_float(
                rf_path.get("bias_tee_loss_db"),
                bias_info.get("rf_loss_db", 0.0),
            ) or 0.0
        if amp_enabled:
            external_gain_db += finite_config_float(
                rf_path.get("amplifier_gain_db"),
                amp_info.get("gain_db", 0.0),
            ) or 0.0
        external_gain_source = "computed from rp_frontend.rf_path"
    else:
        external_gain_db = None
        external_gain_source = "legacy RP-host fallback"

    return {
        "photodetector_model": pd_model or None,
        "pd_model_known": bool(pd_model_info),
        "scope_ch1_response_v_per_w": ch1_response,
        "scope_ch2_response_v_per_w": ch2_response,
        "scope_zero_enabled": scope_zero_enabled,
        "scope_ch1_zero_offset_v": ch1_zero_offset,
        "scope_ch2_zero_offset_v": ch2_zero_offset,
        "saturation_power_uw": finite_config_float(
            pd_cfg.get("saturation_power_uw"),
            pd_model_info.get("saturation_power_uw"),
        ),
        "rf_response_v_per_w_50ohm": finite_config_float(
            pd_cfg.get("rf_response_v_per_w_50ohm"),
            pd_model_info.get("rf_response_v_per_w_50ohm"),
        ),
        "bias_tee_enabled": bias_enabled,
        "bias_tee_model": bias_model or None,
        "amplifier_enabled": amp_enabled,
        "amplifier_model": amp_model or None,
        "spectrum_external_gain_db": external_gain_db,
        "spectrum_external_gain_source": external_gain_source,
    }


def dashboard_defaults_from_config(config: dict[str, Any]) -> dict[str, Any]:
    laser_type = str(
        config.get("laser_type")
        or config.get("default_laser_type")
        or DEFAULT_LASER_TYPE
    )
    laser_port = config.get("laser_port")
    if laser_port is None:
        if laser_type == "weiyuan":
            laser_port = config.get("weiyuan_port", "COM5")
        else:
            laser_port = config.get("toptica_port", DEFAULT_LARGE_SCAN_LASER_PORT)
    return {
        "listen_host": str(config.get("listen_host", "127.0.0.1")),
        "listen_port": int(config.get("listen_port", 7880)),
        "bridge_base": str(config.get("bridge_base", "http://127.0.0.1:7870")),
        "rp_host": str(config.get("rp_host") or config.get("rp_hostname") or DEFAULT_RP_HOST),
        "laser_type": laser_type,
        "toptica_host": str(config.get("toptica_host", "192.168.1.104")),
        "toptica_python": config_path(config.get("toptica_python") or DEFAULT_TOPTICA_PYTHON),
        "large_scan_laser_port": str(laser_port),
        "scope_type": str(config.get("scope_type", "rs_rte")),
        "scope_resource": str(config.get("scope_resource", DEFAULT_SCOPE_RESOURCE)),
        "default_cavity": (
            config_path(config["default_cavity"])
            if config.get("default_cavity")
            else DEFAULT_CAVITY
        ),
        "auto_start_bridge": bool_from_config(config.get("auto_start_bridge"), True),
        "auto_start_bridge_gui": bool_from_config(
            config.get("open_pyrpl_gui", config.get("auto_start_bridge_gui")),
            False,
        ),
        "device_profile": resolve_device_profile(config),
        "sensitivity_defaults": resolve_sensitivity_defaults(config),
    }


def resolve_sensitivity_defaults(config: dict[str, Any]) -> dict[str, Any]:
    sensitivity = config.get("sensitivity") if isinstance(config.get("sensitivity"), dict) else {}
    root_value = sensitivity.get("pressure_calibration_root") if sensitivity else None
    calibration_root = config_path(root_value) if root_value else DEFAULT_PRESSURE_CALIBRATION_ROOT
    source_model = str(sensitivity.get("pressure_source_model") or DEFAULT_PRESSURE_SOURCE)
    path_value = sensitivity.get("pressure_calibration_path") if sensitivity else None
    calibration_path = (
        config_path(path_value)
        if path_value
        else default_pressure_calibration_from_source(calibration_root, source_model)
    )
    return {
        "pressure_calibration_root": str(calibration_root),
        "pressure_source_model": source_model,
        "pressure_calibration_path": str(calibration_path),
        "pressure_quantity": str(sensitivity.get("pressure_quantity") or "pk"),
        "pressure_calibration_drive_vpp": finite_config_float(
            sensitivity.get("pressure_calibration_drive_vpp"),
            None,
        ),
    }


def parse_args() -> argparse.Namespace:
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_FILE)
    pre_args, _ = pre_parser.parse_known_args()
    config = load_config_file(pre_args.config_file)
    defaults = dashboard_defaults_from_config(config)

    parser = argparse.ArgumentParser(description=__doc__, parents=[pre_parser])
    parser.add_argument("--listen-host", default=defaults["listen_host"])
    parser.add_argument("--listen-port", type=int, default=defaults["listen_port"])
    parser.add_argument("--bridge-base", default=defaults["bridge_base"])
    parser.add_argument("--rp-host", default=defaults["rp_host"])
    parser.add_argument("--laser-type", choices=["none", "toptica_tcp", "toptica_serial", "weiyuan"], default=defaults["laser_type"])
    parser.add_argument("--toptica-host", default=defaults["toptica_host"])
    parser.add_argument("--toptica-python", type=Path, default=defaults["toptica_python"])
    parser.add_argument("--large-scan-laser-port", default=defaults["large_scan_laser_port"])
    parser.add_argument("--scope-type", choices=["none", "rs_rte"], default=defaults["scope_type"])
    parser.add_argument("--scope-resource", default=defaults["scope_resource"])
    parser.add_argument("--default-cavity", type=Path, default=defaults["default_cavity"])
    parser.add_argument(
        "--auto-start-bridge",
        action=argparse.BooleanOptionalAction,
        default=defaults["auto_start_bridge"],
        help="Start a dashboard-managed PyRPL bridge when the dashboard starts.",
    )
    parser.add_argument(
        "--auto-start-bridge-gui",
        action=argparse.BooleanOptionalAction,
        default=defaults["auto_start_bridge_gui"],
        help="With --auto-start-bridge, open the PyRPL Qt GUI instead of using headless bridge mode.",
    )
    parser.set_defaults(
        device_profile=defaults["device_profile"],
        sensitivity_defaults=defaults["sensitivity_defaults"],
    )
    return parser.parse_args()


def finite_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def html_response(handler: BaseHTTPRequestHandler, body: str) -> None:
    data = body.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    body = handler.rfile.read(length).decode("utf-8")
    return json.loads(body) if body.strip() else {}


def request_json(url: str, timeout: float = 3.0) -> dict[str, Any]:
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def bridge_query(base: str, path: str, params: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    clean_params = {key: value for key, value in params.items() if value is not None}
    return request_json(f"{base}{path}?{urlencode(clean_params)}", timeout=timeout)


class SensitivityCancelled(RuntimeError):
    pass


def bridge_get(base: str, param: str) -> dict[str, Any]:
    from urllib.parse import quote

    return request_json(f"{base}/get?param={quote(param)}", timeout=5.0)


def bridge_set(base: str, param: str, value: str | int | float) -> dict[str, Any]:
    from urllib.parse import quote

    return request_json(f"{base}/set?param={quote(param)}&value={quote(str(value))}", timeout=5.0)


def bridge_stop_acquisitions(base: str) -> dict[str, Any]:
    return request_json(f"{base.rstrip('/')}/acquisition/stop", timeout=5.0)


def safe_off(base: str) -> dict[str, Any]:
    results = []
    ok = True
    for param, value in (
        ("pid0.p", 0),
        ("pid0.i", 0),
        ("pid0.output_direct", "off"),
        ("asg0.output_direct", "off"),
    ):
        try:
            result = bridge_set(base, param, value)
            results.append({"param": param, "value": value, "result": result})
            ok = ok and bool(result.get("ok"))
        except Exception as exc:
            ok = False
            results.append({"param": param, "value": value, "error": repr(exc)})
    return {"ok": ok, "steps": results}


def restore_lock_sweep(base: str) -> dict[str, Any]:
    results = []
    ok = True
    try:
        result = bridge_stop_acquisitions(base)
        results.append({"action": "stop_acquisitions", "result": result})
    except Exception as exc:
        results.append({"action": "stop_acquisitions", "warning": repr(exc)})
    for param, value in (
        ("pid0.p", 0),
        ("pid0.i", 0),
        ("pid0.output_direct", "off"),
        ("asg0.waveform", "ramp"),
        ("asg0.frequency", DEFAULT_LOCK_SWEEP_FREQUENCY_HZ),
        ("asg0.amplitude", DEFAULT_LOCK_SWEEP_AMPLITUDE_V),
        ("asg0.offset", 0),
        ("asg0.output_direct", "out2"),
        ("asg1.waveform", "square"),
        ("asg1.frequency", DEFAULT_LOCK_SWEEP_FREQUENCY_HZ),
        ("asg1.amplitude", 0.5),
        ("asg1.offset", 0),
        ("scope.input1", "in1"),
        ("scope.input2", "out2"),
        ("scope.trigger_source", "asg1"),
        ("scope.trigger_delay", 0),
        ("scope.duration", DEFAULT_LOCK_SCOPE_DURATION_S),
        ("scope.rolling_mode", "false"),
    ):
        try:
            result = bridge_set(base, param, value)
            results.append({"param": param, "value": value, "result": result})
            ok = ok and bool(result.get("ok"))
        except Exception as exc:
            ok = False
            results.append({"param": param, "value": value, "error": repr(exc)})
    try:
        result = bridge_set(base, "scope.run_continuous", "true")
        results.append({"param": "scope.run_continuous", "value": "true", "result": result})
    except Exception as exc:
        results.append({"param": "scope.run_continuous", "value": "true", "warning": repr(exc)})
    return {
        "ok": ok,
        "message": "lock sweep restored; spectrum/network acquisitions stopped, PID disabled, ASG ramp sent to out2, and scope continuous run requested",
        "sweep_frequency_hz": DEFAULT_LOCK_SWEEP_FREQUENCY_HZ,
        "sweep_amplitude_v": DEFAULT_LOCK_SWEEP_AMPLITUDE_V,
        "scope_duration_s": DEFAULT_LOCK_SCOPE_DURATION_S,
        "steps": results,
    }


def instrument_config_from_mapping(source: dict[str, Any], args: argparse.Namespace) -> dict[str, str]:
    return {
        "rp_type": str(source.get("rp_type") or "pyrpl_bridge"),
        "bridge_base": str(source.get("bridge_base") or args.bridge_base),
        "rp_host": str(source.get("rp_host") or args.rp_host),
        "laser_type": str(source.get("laser_type") or args.laser_type),
        "toptica_host": str(source.get("toptica_host") or args.toptica_host),
        "laser_port": str(source.get("laser_port") or args.large_scan_laser_port),
        "weiyuan_slave": str(source.get("weiyuan_slave") or "255"),
        "scope_type": str(source.get("scope_type") or args.scope_type),
        "scope_resource": str(source.get("scope_resource") or args.scope_resource),
    }


def check_instruments(
    config: dict[str, str],
    toptica_python: Path,
    device_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    instruments: dict[str, Any] = {}

    if config["rp_type"] == "none":
        instruments["red_pitaya"] = {"type": "none", "ok": None, "message": "not selected"}
    else:
        try:
            bridge = request_json(f"{config['bridge_base']}/health", timeout=2.0)
        except Exception as exc:
            bridge = {"ok": False, "error": repr(exc)}
        instruments["red_pitaya"] = {
            "type": "PyRPL bridge",
            "bridge_base": config["bridge_base"],
            "rp_host": config["rp_host"] or None,
            "ok": bool(bridge.get("ok")),
            "bridge": bridge,
        }

    laser_type = config["laser_type"]
    if laser_type == "none":
        instruments["laser"] = {"type": "none", "ok": None, "message": "not selected"}
    elif laser_type == "toptica_tcp":
        try:
            adapter_read_pc = load_local_module("toptica_laser_adapter").read_pc

            info = adapter_read_pc(connection="tcp", host=config["toptica_host"], port=config["laser_port"])
            instruments["laser"] = {
                "type": "TOPTICA DLC PRO TCP/IP",
                "ok": True,
                "host": config["toptica_host"],
                "pc": info,
                "python": str(toptica_python),
            }
        except Exception as exc:
            instruments["laser"] = {
                "type": "TOPTICA DLC PRO TCP/IP",
                "ok": False,
                "host": config["toptica_host"],
                "error": repr(exc),
                "python": str(toptica_python),
            }
    elif laser_type == "toptica_serial":
        try:
            adapter_read_pc = load_local_module("toptica_laser_adapter").read_pc

            info = adapter_read_pc(connection="serial", host=config["toptica_host"], port=config["laser_port"])
            instruments["laser"] = {
                "type": "TOPTICA DLC PRO serial",
                "ok": True,
                "port": config["laser_port"],
                "pc": info,
            }
        except Exception as exc:
            instruments["laser"] = {
                "type": "TOPTICA DLC PRO serial",
                "ok": False,
                "port": config["laser_port"],
                "error": repr(exc),
            }
    elif laser_type == "weiyuan":
        try:
            read_status = load_local_module("weiyuan_laser_adapter").read_status

            info = read_status(config["laser_port"], slave=int(config["weiyuan_slave"]))
            instruments["laser"] = {
                "type": "微源光子",
                "ok": True,
                "port": config["laser_port"],
                "weiyuan": info,
            }
        except Exception as exc:
            instruments["laser"] = {
                "type": "微源光子",
                "ok": False,
                "port": config["laser_port"],
                "error": repr(exc),
            }
    else:
        instruments["laser"] = {"type": laser_type, "ok": False, "error": f"unknown laser type: {laser_type}"}

    if config["scope_type"] == "none":
        instruments["oscilloscope"] = {"type": "none", "ok": None, "message": "not selected"}
    else:
        try:
            _TopticaDlcPro, RohdeSchwarzRte = load_acquire_helpers()
            scope = RohdeSchwarzRte(config["scope_resource"], timeout_ms=2500)
            close_warning = None
            try:
                idn = scope.idn()
            finally:
                try:
                    scope.close()
                except Exception as exc:
                    close_warning = repr(exc)
            instruments["oscilloscope"] = {
                "type": "R&S RTE",
                "ok": True,
                "scope_resource": config["scope_resource"],
                "idn": idn,
                "close_warning": close_warning,
            }
        except Exception as exc:
            instruments["oscilloscope"] = {
                "type": "R&S RTE",
                "ok": False,
                "scope_resource": config["scope_resource"],
                "error": repr(exc),
            }

    selected = [item for item in instruments.values() if item.get("ok") is not None]
    return {
        "ok": all(bool(item.get("ok")) for item in selected) if selected else True,
        "config": config,
        "device_profile": device_profile or {},
        "instruments": instruments,
    }


def parse_script_json(stdout: str) -> dict[str, Any] | None:
    start = stdout.find("{")
    if start < 0:
        return None
    try:
        return json.loads(stdout[start:])
    except json.JSONDecodeError:
        return None


def run_script(command: list[str], timeout_s: float) -> dict[str, Any]:
    started = time.time()
    env = os.environ.copy()
    local_paths = [str(path) for path in (DASHBOARD_DIR, LOCK_DIR, DRIVERS_DIR, COMMON_DIR, BRIDGE_DIR, SRC_DIR)]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        local_paths.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(local_paths)
    proc = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    parsed = parse_script_json(proc.stdout)
    return {
        "ok": proc.returncode == 0 and bool(parsed is None or parsed.get("ok", True)),
        "returncode": proc.returncode,
        "elapsed_s": time.time() - started,
        "command": command,
        "json": parsed,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
    }


def run_standard_auto_coupling(body: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    config = instrument_config_from_mapping(body, args)
    if config["laser_type"] not in {"toptica_tcp", "toptica_serial"}:
        raise ValueError("Auto coupling is only enabled in TOPTICA Q / Lock mode")
    if config["rp_type"] == "none":
        raise ValueError("Auto coupling requires RP / PyRPL bridge power readout")
    if not DEFAULT_AUTO_COUPLING_SCRIPT.exists():
        raise FileNotFoundError(f"missing auto-coupling script: {DEFAULT_AUTO_COUPLING_SCRIPT}")

    common = [
        package_python_executable(),
        str(DEFAULT_AUTO_COUPLING_SCRIPT),
        "--execute",
        "--power-kind",
        "inst",
        "--channel",
        "CH1",
        "--bridge",
        config["bridge_base"],
        "--min-v",
        "0",
        "--max-v",
        "75",
    ]
    first_pass = common + [
        "--axis-order",
        "COM7:z",
        "COM7:y",
        "COM6:z",
        "COM6:x",
        "--round-steps-v",
        "1,0.3,0.1",
        "--round-max-travel-v",
        "20,20,20",
        "--distance-axis-order",
        "COM7:x",
        "COM6:y",
        "--distance-round-steps-v",
        "2,0.6,0.2",
        "--distance-round-max-travel-v",
        "20,20,20",
    ]
    second_pass = common + [
        "--axis-order",
        "COM7:z",
        "COM7:y",
        "COM6:z",
        "COM6:x",
        "COM7:x",
        "COM6:y",
        "--round-steps-v",
        "0.5,0.3,0.1",
        "--round-max-travel-v",
        "10,10,10",
    ]
    steps = [
        {
            "name": "coarse alignment + distance",
            "parameters": {
                "primary_axes": "COM7:z COM7:y COM6:z COM6:x",
                "primary_steps_v": "1,0.3,0.1",
                "primary_max_travel_v": "20,20,20",
                "distance_axes": "COM7:x COM6:y",
                "distance_steps_v": "2,0.6,0.2",
                "distance_max_travel_v": "20,20,20",
            },
            "result": run_script(first_pass, timeout_s=900.0),
        }
    ]
    if not steps[-1]["result"].get("ok"):
        return {
            "ok": False,
            "message": "auto coupling stopped during first pass",
            "steps": steps,
        }
    steps.append(
        {
            "name": "fine confirmation",
            "parameters": {
                "axes": "COM7:z COM7:y COM6:z COM6:x COM7:x COM6:y",
                "steps_v": "0.5,0.3,0.1",
                "max_travel_v": "10,10,10",
            },
            "result": run_script(second_pass, timeout_s=900.0),
        }
    )
    return {
        "ok": all(bool(step["result"].get("ok")) for step in steps),
        "message": "standard auto coupling finished",
        "workflow": "coarse monotonic alignment, distance tuning, then fine confirmation; instantaneous RP power is used",
        "steps": steps,
    }


def run_weiyuan_action(body: dict[str, Any]) -> dict[str, Any]:
    port = str(body.get("laser_port") or DEFAULT_LARGE_SCAN_LASER_PORT)
    slave = int(body.get("weiyuan_slave") or 255)
    action = str(body.get("weiyuan_action") or "status")
    try:
        WeiyuanLaser = load_local_module("weiyuan_laser_adapter").WeiyuanLaser

        with WeiyuanLaser(port=port, slave=slave) as laser:
            if action == "status":
                pass
            elif action == "set_temperature":
                value = finite_float(body.get("temperature_c"))
                if value is None:
                    raise ValueError("temperature_c is required")
                laser.set_temperature_c(value)
            elif action == "set_current":
                value = finite_float(body.get("current_ma"))
                if value is None:
                    raise ValueError("current_ma is required")
                laser.set_current_ma(value)
            elif action == "set_initial_current":
                laser.set_current_ma(260.0)
            elif action == "tec_on":
                laser.set_tec_enabled(True)
            elif action == "tec_off":
                laser.set_tec_enabled(False)
            elif action == "ld_on":
                laser.set_ld_enabled(True)
            elif action == "ld_off":
                laser.set_ld_enabled(False)
            else:
                raise ValueError(f"Unknown Weiyuan action: {action}")
            status = laser.read_status()
        return {"ok": True, "json": {"message": "微源光子通信成功", "weiyuan": status, "action": action}}
    except Exception as exc:
        return {"ok": False, "json": {"message": "微源光子通信失败", "port": port, "action": action, "error": repr(exc)}}


def capture_scope_preview(body: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    config = instrument_config_from_mapping(body, args)
    if config["rp_type"] == "none":
        raise ValueError("Bridge scope preview requires RP / PyRPL bridge")
    timeout = finite_float(body.get("timeout_s")) or 5.0
    max_points = int(finite_float(body.get("max_points")) or 1500)
    query = urlencode(
        {
            "tag": "dashboard_scope_preview",
            "timeout": timeout,
            "plot": "false",
            "save": "false",
            "inline": "true",
            "max_points": max_points,
        }
    )
    return request_json(f"{config['bridge_base']}/scope/single?{query}", timeout=max(10.0, timeout + 5.0))


def safe_tag_text(value: object, default: str = "sensitivity") -> str:
    text = str(value or default).strip() or default
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text)


def unique_run_dir(base_dir: Path) -> Path:
    if not base_dir.exists():
        return base_dir
    for idx in range(2, 1000):
        candidate = base_dir.with_name(f"{base_dir.name}_{idx:02d}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find an unused run directory near {base_dir}")


def default_sensitivity_root() -> Path:
    data_paths = load_local_module("data_paths")
    return data_paths.default_results_dir() / "sensitivity"


def resolve_sensitivity_run_dir(body: dict[str, Any], tag_prefix: str, timestamp: str) -> tuple[Path, bool, str | None]:
    cavity_text = str(body.get("cavity_dir") or "").strip()
    run_name = f"{timestamp}_{tag_prefix}"
    if cavity_text:
        cavity_dir = Path(cavity_text).expanduser()
        return unique_run_dir(cavity_dir / "sensitivity" / run_name), False, str(cavity_dir)
    return unique_run_dir(default_sensitivity_root() / run_name), True, None


def latest_sensitivity_run_dir(cavity_dir: Path) -> Path | None:
    latest_path = cavity_dir / "sensitivity" / "latest.json"
    if latest_path.exists():
        try:
            latest = json.loads(latest_path.read_text(encoding="utf-8-sig"))
            run_dir = latest.get("run_dir")
            if run_dir:
                path = Path(run_dir)
                if not path.is_absolute():
                    path = cavity_dir / "sensitivity" / str(run_dir)
                if path.exists():
                    return path
        except Exception:
            pass
    sensitivity_dir = cavity_dir / "sensitivity"
    if not sensitivity_dir.exists():
        return None
    candidates = sorted(
        [path for path in sensitivity_dir.iterdir() if path.is_dir() and (path / "raw").exists()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def pressure_npz_value(data: np.lib.npyio.NpzFile, key: str, default: Any = None) -> Any:
    if key not in data.files:
        return default
    value = data[key]
    if getattr(value, "shape", ()) == ():
        item = value.item()
        if isinstance(item, bytes):
            return item.decode("utf-8", errors="replace")
        return item
    return value


def load_pressure_calibration(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Pressure calibration file not found: {path}")
    data = np.load(str(path), allow_pickle=False)
    if "frequency_hz" not in data.files:
        raise ValueError(f"Pressure calibration lacks frequency_hz: {path}")
    pressure_key = None
    for candidate in ("pressure_pa_at_10vpp", "pressure_pk_pa_at_10vpp", "pressure_pa"):
        if candidate in data.files:
            pressure_key = candidate
            break
    if pressure_key is None:
        raise ValueError(f"Pressure calibration lacks pressure array: {path}")
    freq = np.asarray(data["frequency_hz"], dtype=float)
    pressure = np.asarray(data[pressure_key], dtype=float)
    n = min(len(freq), len(pressure))
    if n < 2:
        raise ValueError(f"Pressure calibration needs at least two points: {path}")
    freq = freq[:n]
    pressure = pressure[:n]
    order = np.argsort(freq)
    freq = freq[order]
    pressure = pressure[order]
    calibration_vpp = finite_config_float(pressure_npz_value(data, "calibration_drive_vpp"), None)
    if calibration_vpp is None:
        text = path.name.lower()
        match = re.search(r"(\d+(?:\.\d+)?)\s*vpp", text)
        calibration_vpp = float(match.group(1)) if match else 10.0
    quantity = str(pressure_npz_value(data, "pressure_quantity", "pk") or "pk")
    if quantity == "unknown_confirm_rms_or_peak":
        quantity = "pk"
    return {
        "path": str(path),
        "frequency_hz": freq,
        "pressure_pa": pressure,
        "pressure_array_key": pressure_key,
        "pressure_quantity": quantity,
        "calibration_drive_vpp": float(calibration_vpp),
        "start_hz": float(np.nanmin(freq)),
        "stop_hz": float(np.nanmax(freq)),
        "points": int(n),
    }


def pressure_calibration_from_body(body: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    defaults = getattr(args, "sensitivity_defaults", {}) or {}
    path_text = str(body.get("pressure_calibration_path") or "").strip()
    if not path_text and body.get("pressure_source_model"):
        root = Path(str(defaults.get("pressure_calibration_root") or DEFAULT_PRESSURE_CALIBRATION_ROOT))
        path_text = str(default_pressure_calibration_from_source(root, str(body.get("pressure_source_model"))))
    if not path_text:
        path_text = str(defaults.get("pressure_calibration_path") or DEFAULT_PRESSURE_CALIBRATION)
    calibration = load_pressure_calibration(Path(path_text).expanduser())
    quantity = str(body.get("pressure_quantity") or defaults.get("pressure_quantity") or calibration["pressure_quantity"] or "pk")
    calibration_drive_vpp = finite_config_float(
        body.get("pressure_calibration_drive_vpp"),
        finite_config_float(defaults.get("pressure_calibration_drive_vpp"), calibration["calibration_drive_vpp"]),
    )
    use_start_hz = finite_config_float(body.get("pressure_use_start_hz"), calibration["start_hz"])
    use_stop_hz = finite_config_float(body.get("pressure_use_stop_hz"), calibration["stop_hz"])
    if use_start_hz is not None and use_stop_hz is not None and use_start_hz > use_stop_hz:
        use_start_hz, use_stop_hz = use_stop_hz, use_start_hz
    calibration.update(
        {
            "pressure_quantity": quantity,
            "calibration_drive_vpp": float(calibration_drive_vpp or calibration["calibration_drive_vpp"]),
            "use_start_hz": float(use_start_hz or calibration["start_hz"]),
            "use_stop_hz": float(use_stop_hz or calibration["stop_hz"]),
        }
    )
    return calibration


def read_bridge_params(base: str, params: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    values: dict[str, Any] = {}
    steps: list[dict[str, Any]] = []
    for param in params:
        try:
            result = bridge_get(base, param)
            values[param] = result.get("value")
            steps.append({"param": param, "ok": bool(result.get("ok")), "result": result})
        except Exception as exc:
            steps.append({"param": param, "ok": False, "error": repr(exc)})
    return values, steps


def sensitivity_settings(body: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    config = instrument_config_from_mapping(body, args)
    if config["rp_type"] == "none":
        raise ValueError("Sensitivity settings require RP / PyRPL bridge")
    base = config["bridge_base"].rstrip("/")
    mode = str(body.get("acquisition_mode") or "both").strip().lower()
    settings = {} if mode == "compute" else bridge_query(base, "/acquisition/settings", {}, timeout=8.0)
    settings["bridge_base"] = base
    sensitivity_defaults = getattr(args, "sensitivity_defaults", {}) or {}
    pressure_root = Path(str(sensitivity_defaults.get("pressure_calibration_root") or DEFAULT_PRESSURE_CALIBRATION_ROOT))
    settings["pressure_sources"] = list_pressure_sources(pressure_root)
    try:
        pressure = pressure_calibration_from_body(body, args)
        settings["pressure_calibration"] = {
            "path": pressure["path"],
            "start_hz": pressure["start_hz"],
            "stop_hz": pressure["stop_hz"],
            "points": pressure["points"],
            "pressure_quantity": pressure["pressure_quantity"],
            "calibration_drive_vpp": pressure["calibration_drive_vpp"],
            "use_start_hz": pressure["use_start_hz"],
            "use_stop_hz": pressure["use_stop_hz"],
            "pressure_array_key": pressure["pressure_array_key"],
            "source_model": str(sensitivity_defaults.get("pressure_source_model") or DEFAULT_PRESSURE_SOURCE),
        }
    except Exception as exc:
        settings["pressure_calibration"] = {
            "ok": False,
            "error": repr(exc),
            "path": str((getattr(args, "sensitivity_defaults", {}) or {}).get("pressure_calibration_path") or DEFAULT_PRESSURE_CALIBRATION),
        }
    return settings


def sensitivity_requested_settings(body: dict[str, Any]) -> dict[str, Any]:
    optical_mode = body.get("optical_mode")
    if isinstance(optical_mode, dict):
        optical_mode = {str(k): json_ready(v) for k, v in optical_mode.items()}
    else:
        optical_mode = None
    return {
        "spectrum": {
            "span_hz": finite_config_float(body.get("spectrum_span_hz"), None),
            "rbw_hz": finite_config_float(body.get("spectrum_rbw_hz"), None),
            "trace_average": int(finite_config_float(body.get("spectrum_trace_average"), 0.0) or 0) or None,
        },
        "network": {
            "amplitude_vpk": finite_config_float(body.get("network_amplitude_vpk"), None),
            "start_freq_hz": finite_config_float(body.get("network_start_freq_hz"), None),
            "stop_freq_hz": finite_config_float(body.get("network_stop_freq_hz"), None),
            "points": int(finite_config_float(body.get("network_points"), 0.0) or 0) or None,
            "rbw_hz": finite_config_float(body.get("network_rbw_hz"), None),
        },
        "pressure": {
            "source_model": str(body.get("pressure_source_model") or "").strip() or None,
            "calibration_path": str(body.get("pressure_calibration_path") or "").strip() or None,
            "calibration_drive_vpp": finite_config_float(body.get("pressure_calibration_drive_vpp"), None),
            "quantity": str(body.get("pressure_quantity") or "").strip() or None,
            "use_start_hz": finite_config_float(body.get("pressure_use_start_hz"), None),
            "use_stop_hz": finite_config_float(body.get("pressure_use_stop_hz"), None),
        },
        "optical_mode": optical_mode,
    }


def apply_sensitivity_settings(base: str, mode: str, settings: dict[str, Any]) -> dict[str, Any]:
    requested: list[tuple[str, str | int | float | None]] = []
    if mode in {"psd", "both"}:
        spectrum = settings.get("spectrum") or {}
        requested.extend(
            [
                ("spectrumanalyzer.span", spectrum.get("span_hz")),
                ("spectrumanalyzer.trace_average", spectrum.get("trace_average")),
            ]
        )
    if mode in {"network", "both"}:
        network = settings.get("network") or {}
        requested.extend(
            [
                ("networkanalyzer.amplitude", network.get("amplitude_vpk")),
                ("networkanalyzer.start_freq", network.get("start_freq_hz")),
                ("networkanalyzer.stop_freq", network.get("stop_freq_hz")),
                ("networkanalyzer.points", network.get("points")),
                ("networkanalyzer.rbw", network.get("rbw_hz")),
            ]
        )

    steps: list[dict[str, Any]] = []
    ok = True
    for param, value in requested:
        if value is None:
            steps.append({"param": param, "ok": True, "skipped": True, "reason": "empty value"})
            continue
        try:
            result = bridge_set(base, param, value)
            steps.append({"param": param, "value": value, "ok": bool(result.get("ok")), "result": result})
            ok = ok and bool(result.get("ok"))
        except Exception as exc:
            ok = False
            steps.append({"param": param, "value": value, "ok": False, "error": repr(exc)})
    return {"ok": ok, "steps": steps}


def downsample_points(x: np.ndarray, y: np.ndarray, max_points: int) -> list[list[float]]:
    n = min(len(x), len(y))
    if n <= 0:
        return []
    step = max(1, int(math.ceil(n / max(100, int(max_points)))))
    points: list[list[float]] = []
    for xi, yi in zip(np.asarray(x[:n])[::step], np.asarray(y[:n])[::step]):
        if np.isfinite(xi) and np.isfinite(yi):
            points.append([float(xi), float(yi)])
    return points


def json_ready(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(v) for v in value]
    return value


def write_interactive_series_html(
    output: Path,
    *,
    title: str,
    subtitle: str,
    panels: list[dict[str, Any]],
) -> None:
    payload = json_ready({"title": title, "subtitle": subtitle, "panels": panels})
    payload_js = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    content = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body{{margin:0;background:#fff;color:#000;font-family:Arial,sans-serif}}
#wrap{{padding:16px 20px 28px;max-width:1500px;margin:auto}}
h1{{font-size:26px;margin:0 0 4px;color:#000}}
#sub{{font-size:15px;margin:0 0 14px;color:#000}}
.panel{{border:1px solid #000;border-radius:6px;padding:10px;margin-bottom:14px;background:#fff}}
.panel h2{{font-size:19px;margin:0 0 8px;color:#000}}
.fitbar{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 8px;font-size:14px;color:#000}}
.fitbar button{{border:1px solid #000;border-radius:4px;background:#fff;color:#000;padding:5px 10px;font:14px Arial,sans-serif;cursor:pointer}}
.fitbar button:hover{{background:#f2f2f2}}
.fitResult{{font-weight:700}}
.fitLegend{{display:inline-flex;gap:10px;align-items:center;flex-wrap:wrap}}
.swatch{{display:inline-block;width:22px;height:0;border-top:3px solid #000;vertical-align:middle;margin-right:4px}}
.box{{position:relative;height:420px}}
canvas{{display:block;width:100%;height:100%;background:#fff}}
.selectBox{{position:absolute;display:none;border:1.5px dashed #000;background:rgba(0,0,0,.08);pointer-events:none}}
#tooltip{{position:fixed;display:none;pointer-events:none;background:#fff;border:1px solid #000;border-radius:4px;padding:7px 9px;font-size:14px;box-shadow:0 2px 7px rgba(0,0,0,.25);z-index:10;white-space:nowrap;color:#000}}
.hint{{font-size:13px;margin-top:8px;color:#000}}
</style>
</head>
<body>
<div id="wrap">
  <h1 id="title"></h1>
  <div id="sub"></div>
  <div id="panels"></div>
  <div class="hint">Left-drag box zooms, Shift+drag pans, wheel zooms, double click resets. Hover reads data values.</div>
</div>
<div id="tooltip"></div>
<script>
const payload={payload_js};
const tooltip=document.getElementById('tooltip');
document.getElementById('title').textContent=payload.title;
document.getElementById('sub').textContent=payload.subtitle;
function range(vals,p=.05){{let fs=vals.filter(Number.isFinite);if(!fs.length)return[0,1];let mn=Math.min(...fs),mx=Math.max(...fs);if(mn===mx){{mn-=1;mx+=1}}let q=(mx-mn)*p;return[mn-q,mx+q]}}
function niceTicks(a,b,n=6){{const span=Math.abs(b-a)||1,raw=span/n,pow=Math.pow(10,Math.floor(Math.log10(raw))),m=raw/pow,step=(m<1.5?1:m<3?2:m<7?5:10)*pow,start=Math.ceil(a/step)*step,out=[];for(let v=start;v<=b+step*.3;v+=step)out.push(v);return out}}
function tickText(v,span){{const a=Math.abs(span);const d=a<.01?5:a<.1?4:a<1?3:a<10?2:1;return v.toFixed(d)}}
function finite(v){{return Number.isFinite(v)}}
function dbmToLinear(v){{return Math.pow(10,v/10)}}
function linearToDbm(v){{return 10*Math.log10(Math.max(v,1e-300))}}
function median(arr){{if(!arr.length)return NaN;const a=[...arr].sort((x,y)=>x-y),m=Math.floor(a.length/2);return a.length%2?a[m]:(a[m-1]+a[m])/2}}
function percentile(arr,p){{if(!arr.length)return NaN;const a=[...arr].sort((x,y)=>x-y),i=Math.min(a.length-1,Math.max(0,Math.round((a.length-1)*p)));return a[i]}}
function mechComponentLinear(x,p){{const f0=Math.max(p[0],1e-12),gamma=Math.max(p[1],1e-12),amp=Math.max(p[2],1e-300),den=(f0*f0-x*x)*(f0*f0-x*x)+(x*gamma)*(x*gamma),peak=(f0*gamma)*(f0*gamma);return amp*peak/Math.max(den,1e-300)}}
function mechSusceptibilityLinear(x,p){{const bg=Math.max(p[3],1e-300);return bg+mechComponentLinear(x,p)}}
function fitMechanicalSusceptibilityVisible(chart){{
  const source=(chart.opt.series[0]&&chart.opt.series[0].data)||[];
  let pts=source.filter(p=>finite(p[0])&&finite(p[1])&&p[0]>=chart.view.x0&&p[0]<=chart.view.x1).map(p=>[p[0],dbmToLinear(p[1]),p[1]]);
  if(pts.length<12)throw new Error('visible range has too few points');
  const stride=Math.max(1,Math.ceil(pts.length/1600));
  pts=pts.filter((_,i)=>i%stride===0);
  const xs=pts.map(p=>p[0]), ys=pts.map(p=>p[1]), ydb=pts.map(p=>p[2]);
  const xMin=Math.min(...xs), xMax=Math.max(...xs), span=Math.max(xMax-xMin,1e-9);
  const edgeN=Math.max(3,Math.floor(pts.length*0.16));
  const bg0=Math.max(1e-300,median([...ys.slice(0,edgeN),...ys.slice(-edgeN)]));
  let imax=0; for(let i=1;i<ys.length;i++) if(ys[i]>ys[imax]) imax=i;
  const f00=xs[imax], amp0=Math.max(ys[imax]-bg0, percentile(ys,.9)-bg0, bg0*0.1, 1e-300);
  const half=bg0+amp0/2;
  let left=xMin,right=xMax;
  for(let i=imax;i>0;i--){{if(ys[i]<=half){{left=xs[i];break}}}}
  for(let i=imax;i<ys.length;i++){{if(ys[i]<=half){{right=xs[i];break}}}}
  const g0=Math.max(Math.min(right-left,span),span/200);
  let q=[f00,Math.log(g0),Math.log(amp0),Math.log(bg0)];
  const score=(qq)=>{{
    const p=[Math.min(xMax,Math.max(xMin,qq[0])),Math.exp(qq[1]),Math.exp(qq[2]),Math.exp(qq[3])];
    let s=0,n=0;
    for(let i=0;i<pts.length;i++){{const m=linearToDbm(mechSusceptibilityLinear(xs[i],p)); if(!finite(m))continue; const r=m-ydb[i]; s+=r*r; n++}}
    const g=p[1];
    if(g<span/3000||g>span*2)s+=1e4;
    return s/Math.max(n,1);
  }};
  let best=score(q), steps=[span/12,Math.log(1.8),Math.log(1.8),Math.log(1.25)];
  for(let iter=0;iter<75;iter++){{
    let improved=false;
    for(let k=0;k<4;k++){{
      let localBest=best, localQ=q;
      for(const dir of [-1,1]){{const cand=q.slice();cand[k]+=dir*steps[k];cand[0]=Math.min(xMax,Math.max(xMin,cand[0]));const sc=score(cand);if(sc<localBest){{localBest=sc;localQ=cand}}}}
      if(localBest<best){{q=localQ;best=localBest;improved=true}}
    }}
    if(!improved)steps=steps.map(v=>v*0.72);
    if(Math.max(...steps)<1e-6)break;
  }}
  const p=[Math.min(xMax,Math.max(xMin,q[0])),Math.exp(q[1]),Math.exp(q[2]),Math.exp(q[3])];
  const fitData=[], mechData=[]; const nfit=360;
  for(let i=0;i<nfit;i++){{const x=xMin+span*i/(nfit-1),mech=mechComponentLinear(x,p);fitData.push([x,linearToDbm(p[3]+mech)]);mechData.push([x,linearToDbm(mech)])}}
  const bgDb=linearToDbm(p[3]), f0=p[0], fwhm=p[1], qm=f0/fwhm;
  return {{data:fitData,mechData,bgDb,f0,fwhm,qm,rmseDb:Math.sqrt(best),x0:xMin,x1:xMax}};
}}
class Chart{{
  constructor(canvas,opt){{this.canvas=canvas;this.box=canvas.parentElement;this.sel=this.box.querySelector('.selectBox');this.ctx=canvas.getContext('2d');this.opt=opt;this.pad={{l:92,r:28,t:18,b:62}};const xs=opt.series.flatMap(s=>s.data.map(p=>p[0])),ys=opt.series.flatMap(s=>s.data.map(p=>p[1]));const xr=range(xs,.02),yr=range(ys,.06);this.init={{x0:xr[0],x1:xr[1],y0:yr[0],y1:yr[1]}};this.view={{...this.init}};this.drag=false;this.pan=false;this.start=null;this.last=null;this.hoverState=null;this.fit=null;this.bind();this.resize()}}
  W(){{return this.box.clientWidth}} H(){{return this.box.clientHeight}}
  sx(x){{return this.pad.l+(x-this.view.x0)/(this.view.x1-this.view.x0)*(this.W()-this.pad.l-this.pad.r)}} sy(y){{return this.H()-this.pad.b-(y-this.view.y0)/(this.view.y1-this.view.y0)*(this.H()-this.pad.t-this.pad.b)}}
  ix(px){{return this.view.x0+(px-this.pad.l)/(this.W()-this.pad.l-this.pad.r)*(this.view.x1-this.view.x0)}} iy(py){{return this.view.y0+(this.H()-this.pad.b-py)/(this.H()-this.pad.t-this.pad.b)*(this.view.y1-this.view.y0)}}
  inPlot(x,y){{return x>=this.pad.l&&x<=this.W()-this.pad.r&&y>=this.pad.t&&y<=this.H()-this.pad.b}}
  resize(){{const dpr=window.devicePixelRatio||1;this.canvas.width=Math.round(this.W()*dpr);this.canvas.height=Math.round(this.H()*dpr);this.ctx.setTransform(dpr,0,0,dpr,0,0);this.draw()}}
  bind(){{window.addEventListener('resize',()=>this.resize());this.canvas.addEventListener('dblclick',()=>{{this.view={{...this.init}};this.draw()}});this.canvas.addEventListener('wheel',e=>{{e.preventDefault();const r=this.canvas.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top,cx=this.ix(mx),cy=this.iy(my),f=e.deltaY<0?.82:1.22;this.view.x0=cx-(cx-this.view.x0)*f;this.view.x1=cx+(this.view.x1-cx)*f;this.view.y0=cy-(cy-this.view.y0)*f;this.view.y1=cy+(this.view.y1-cy)*f;this.draw()}});this.canvas.addEventListener('mousedown',e=>{{const r=this.canvas.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;if(!this.inPlot(mx,my))return;this.drag=true;this.pan=!!e.shiftKey;this.start={{x:mx,y:my}};this.last={{x:e.clientX,y:e.clientY}};if(!this.pan)this.updateSel(mx,my)}});window.addEventListener('mouseup',e=>{{if(!this.drag)return;if(!this.pan&&this.start){{const r=this.canvas.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top,xA=Math.max(this.pad.l,Math.min(this.W()-this.pad.r,this.start.x)),xB=Math.max(this.pad.l,Math.min(this.W()-this.pad.r,mx)),yA=Math.max(this.pad.t,Math.min(this.H()-this.pad.b,this.start.y)),yB=Math.max(this.pad.t,Math.min(this.H()-this.pad.b,my));if(Math.abs(xB-xA)>8&&Math.abs(yB-yA)>8){{this.view={{x0:Math.min(this.ix(xA),this.ix(xB)),x1:Math.max(this.ix(xA),this.ix(xB)),y0:Math.min(this.iy(yA),this.iy(yB)),y1:Math.max(this.iy(yA),this.iy(yB))}};this.draw()}}}}this.drag=false;this.pan=false;this.start=null;this.sel.style.display='none'}});this.canvas.addEventListener('mouseleave',()=>{{if(!this.drag){{tooltip.style.display='none';if(this.hoverState){{this.hoverState=null;this.draw()}}}}}});this.canvas.addEventListener('mousemove',e=>{{const r=this.canvas.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;if(this.drag&&this.pan){{const dx=e.clientX-this.last.x,dy=e.clientY-this.last.y;this.last={{x:e.clientX,y:e.clientY}};const xs=this.view.x1-this.view.x0,ys=this.view.y1-this.view.y0;this.view.x0-=dx/(this.W()-this.pad.l-this.pad.r)*xs;this.view.x1-=dx/(this.W()-this.pad.l-this.pad.r)*xs;this.view.y0+=dy/(this.H()-this.pad.t-this.pad.b)*ys;this.view.y1+=dy/(this.H()-this.pad.t-this.pad.b)*ys;this.draw();return}}if(this.drag&&!this.pan){{this.updateSel(mx,my);return}}this.hover(e,mx,my)}})}}
  updateSel(mx,my){{const x1=Math.max(this.pad.l,Math.min(this.W()-this.pad.r,this.start.x)),y1=Math.max(this.pad.t,Math.min(this.H()-this.pad.b,this.start.y)),x2=Math.max(this.pad.l,Math.min(this.W()-this.pad.r,mx)),y2=Math.max(this.pad.t,Math.min(this.H()-this.pad.b,my));this.sel.style.left=Math.min(x1,x2)+'px';this.sel.style.top=Math.min(y1,y2)+'px';this.sel.style.width=Math.abs(x2-x1)+'px';this.sel.style.height=Math.abs(y2-y1)+'px';this.sel.style.display='block'}}
  interp(s,xq){{const d=s.data;if(!d||!d.length)return null;let prev=d[0];if(xq<=prev[0])return {{x:prev[0],y:prev[1]}};for(let i=1;i<d.length;i++){{const p=d[i];if(xq<=p[0]){{const x0=prev[0],y0=prev[1],x1=p[0],y1=p[1],t=(xq-x0)/((x1-x0)||1);return {{x:xq,y:y0+(y1-y0)*t}}}}prev=p}}const last=d[d.length-1];return {{x:last[0],y:last[1]}}}}
  hover(e,mx,my){{if(!this.inPlot(mx,my)){{tooltip.style.display='none';if(this.hoverState){{this.hoverState=null;this.draw()}}return}}const xq=this.ix(mx),rows=[];for(const s of this.opt.series){{const p=this.interp(s,xq);if(!p||!Number.isFinite(p.y))continue;if(p.y<this.view.y0||p.y>this.view.y1)continue;rows.push({{s,x:p.x,y:p.y}})}}if(!rows.length){{tooltip.style.display='none';if(this.hoverState){{this.hoverState=null;this.draw()}}return}}this.hoverState={{x:xq,rows}};this.draw();tooltip.style.display='block';tooltip.style.left=e.clientX+14+'px';tooltip.style.top=e.clientY+12+'px';const body=rows.map(r=>`<div><b style="color:${{r.s.color||'#000'}}">${{r.s.name}}</b>: ${{r.y.toFixed(3)}}</div>`).join('');tooltip.innerHTML=`<b>${{this.opt.xLabel}}: ${{xq.toFixed(4)}}</b><br>${{body}}`}}
  drawHover(){{const h=this.hoverState;if(!h)return;const c=this.ctx,x=this.sx(h.x);c.save();c.strokeStyle='#000';c.lineWidth=1;c.setLineDash([4,4]);c.beginPath();c.moveTo(x,this.pad.t);c.lineTo(x,this.H()-this.pad.b);c.stroke();c.setLineDash([]);for(const r of h.rows){{const y=this.sy(r.y);if(!this.inPlot(x,y))continue;c.beginPath();c.arc(x,y,4.5,0,Math.PI*2);c.fillStyle=r.s.color||'#000';c.fill();c.strokeStyle='#000';c.lineWidth=1;c.stroke()}}c.restore()}}
  setFit(fit){{this.fit=fit;this.draw()}}
  clearFit(){{this.fit=null;this.draw()}}
  drawFit(){{const f=this.fit;if(!f||!f.data||!f.data.length)return;const c=this.ctx;c.save();c.beginPath();let started=false;for(const p of f.data){{const x=p[0],y=p[1];if(!finite(x)||!finite(y)||x<this.view.x0||x>this.view.x1)continue;const px=this.sx(x),py=this.sy(y);if(!started){{c.moveTo(px,this.sy(f.bgDb));c.lineTo(px,py);started=true}}else c.lineTo(px,py)}}if(started){{for(let i=f.data.length-1;i>=0;i--){{const p=f.data[i],x=p[0];if(!finite(x)||x<this.view.x0||x>this.view.x1)continue;c.lineTo(this.sx(x),this.sy(f.bgDb))}}c.closePath();c.fillStyle='rgba(214,39,40,.12)';c.fill()}}const drawLine=(data,color,width,dash=[])=>{{c.save();c.strokeStyle=color;c.lineWidth=width;c.setLineDash(dash);c.beginPath();let st=false;for(const p of data){{const x=p[0],y=p[1];if(!finite(x)||!finite(y)||x<this.view.x0||x>this.view.x1)continue;const px=this.sx(x),py=this.sy(y);if(!st){{c.moveTo(px,py);st=true}}else c.lineTo(px,py)}}if(st)c.stroke();c.restore()}};drawLine(f.data.map(p=>[p[0],f.bgDb]),'#2ca02c',1.8,[7,5]);drawLine(f.mechData||[],'#1f77b4',1.9,[3,3]);drawLine(f.data,'#d62728',2.5,[]);const x0=this.sx(f.f0);if(finite(x0)){{c.setLineDash([5,4]);c.strokeStyle='#d62728';c.lineWidth=1;c.beginPath();c.moveTo(x0,this.pad.t);c.lineTo(x0,this.H()-this.pad.b);c.stroke()}}c.restore()}}
  draw(){{const c=this.ctx;c.clearRect(0,0,this.W(),this.H());c.fillStyle='#fff';c.fillRect(0,0,this.W(),this.H());c.strokeStyle='#000';c.lineWidth=1.2;c.strokeRect(this.pad.l,this.pad.t,this.W()-this.pad.l-this.pad.r,this.H()-this.pad.t-this.pad.b);const xt=niceTicks(this.view.x0,this.view.x1,6),yt=niceTicks(this.view.y0,this.view.y1,6);c.strokeStyle='#000';c.lineWidth=.45;c.font='14px Arial';c.textAlign='center';c.textBaseline='top';for(const v of xt){{const x=this.sx(v);c.beginPath();c.moveTo(x,this.pad.t);c.lineTo(x,this.H()-this.pad.b);c.stroke();c.fillStyle='#000';c.fillText(tickText(v,this.view.x1-this.view.x0),x,this.H()-this.pad.b+9)}}c.textAlign='right';c.textBaseline='middle';for(const v of yt){{const y=this.sy(v);c.beginPath();c.moveTo(this.pad.l,y);c.lineTo(this.W()-this.pad.r,y);c.stroke();c.fillStyle='#000';c.fillText(tickText(v,this.view.y1-this.view.y0),this.pad.l-9,y)}}c.font='18px Arial';c.textAlign='center';c.textBaseline='bottom';c.fillText(this.opt.xLabel,(this.pad.l+this.W()-this.pad.r)/2,this.H()-10);c.save();c.translate(24,(this.pad.t+this.H()-this.pad.b)/2);c.rotate(-Math.PI/2);c.fillText(this.opt.yLabel,0,0);c.restore();for(const s of this.opt.series){{c.strokeStyle=s.color||'#000';c.lineWidth=s.width||2;c.beginPath();let st=false;for(const p of s.data){{const x=p[0],y=p[1];if(x<this.view.x0||x>this.view.x1||y<this.view.y0-10||y>this.view.y1+10)continue;if(!st){{c.moveTo(this.sx(x),this.sy(y));st=true}}else c.lineTo(this.sx(x),this.sy(y))}}c.stroke()}}this.drawFit();this.drawHover()}}
}}
const root=document.getElementById('panels');
function installMechanicalFit(panel,chart){{
  const bar=document.createElement('div');bar.className='fitbar';
  const btn=document.createElement('button');btn.textContent='Fit visible mechanical peak';
  const res=document.createElement('span');res.className='fitResult';res.textContent='zoom to one peak, then fit';
  btn.onclick=()=>{{if(chart.fit){{chart.clearFit();btn.textContent='Fit visible mechanical peak';res.textContent='fit cleared';return}}try{{const fit=fitMechanicalSusceptibilityVisible(chart);chart.setFit(fit);btn.textContent='Clear fit';res.textContent=`f0=${{fit.f0.toFixed(6)}} MHz, Gamma/FWHM~${{(fit.fwhm*1e3).toFixed(2)}} kHz, Q~${{fit.qm.toFixed(0)}}, rms=${{fit.rmseDb.toFixed(2)}} dB`}}catch(err){{res.textContent='fit failed: '+err.message}}}};
  const legend=document.createElement('span');legend.className='fitLegend';legend.innerHTML='<span><i class="swatch" style="border-color:#d62728"></i>total</span><span><i class="swatch" style="border-color:#1f77b4;border-top-style:dashed"></i>mechanical</span><span><i class="swatch" style="border-color:#2ca02c;border-top-style:dashed"></i>background</span>';
  bar.appendChild(btn);bar.appendChild(res);bar.appendChild(legend);panel.insertBefore(bar,panel.querySelector('.box'));
}}
payload.panels.forEach((p,i)=>{{const panel=document.createElement('div');panel.className='panel';panel.innerHTML=`<h2>${{p.title}}</h2><div class="box"><canvas></canvas><div class="selectBox"></div></div>`;root.appendChild(panel);const chart=new Chart(panel.querySelector('canvas'),p);if(p.fitMechanical)installMechanicalFit(panel,chart)}});
</script>
</body>
</html>
"""
    output.write_text(content, encoding="utf-8")


def write_sensitivity_preview_plots(
    *,
    run_dir: Path,
    tag_prefix: str,
    timestamp: str,
    spectrum: dict[str, Any] | None,
    network: dict[str, Any] | None,
    max_points: int,
) -> dict[str, str]:
    plots: dict[str, str] = {}
    if spectrum and spectrum.get("ok") and spectrum.get("path"):
        data = np.load(str(spectrum["path"]))
        freq_mhz = np.asarray(data["frequency_hz"], dtype=float) / 1e6
        psd = np.asarray(data["input1_dbm_per_hz"], dtype=float)
        output = run_dir / "noise_psd.html"
        write_interactive_series_html(
            output,
            title=f"{tag_prefix} noise PSD",
            subtitle=f"{timestamp}; saved data: {spectrum.get('path')}",
            panels=[
                {
                    "title": "Spectrum analyzer PSD",
                    "xLabel": "Frequency (MHz)",
                    "yLabel": "PSD (dBm/Hz)",
                    "fitMechanical": True,
                    "series": [{"name": "PSD", "color": "#000", "width": 1.8, "data": downsample_points(freq_mhz, psd, max_points)}],
                }
            ],
        )
        plots["noise_psd_html"] = str(output)
    if network and network.get("ok") and network.get("path"):
        data = np.load(str(network["path"]))
        freq_mhz = np.asarray(data["frequency_hz"], dtype=float) / 1e6
        mag = np.asarray(data["magnitude_dbm"], dtype=float)
        phase = np.asarray(data["phase_deg"], dtype=float)
        output = run_dir / "network_response.html"
        write_interactive_series_html(
            output,
            title=f"{tag_prefix} network response",
            subtitle=f"{timestamp}; saved data: {network.get('path')}",
            panels=[
                {
                    "title": "Network analyzer coherent response",
                    "xLabel": "Frequency (MHz)",
                    "yLabel": "Magnitude (dBm)",
                    "series": [{"name": "Magnitude", "color": "#000", "width": 1.8, "data": downsample_points(freq_mhz, mag, max_points)}],
                },
                {
                    "title": "Network analyzer phase",
                    "xLabel": "Frequency (MHz)",
                    "yLabel": "Phase (deg)",
                    "series": [{"name": "Phase", "color": "#000", "width": 1.8, "data": downsample_points(freq_mhz, phase, max_points)}],
                },
            ],
        )
        plots["network_response_html"] = str(output)
    return plots


def copy_to_subdir_if_needed(path_text: str | None, target_dir: Path, target_name: str | None = None) -> str | None:
    if not path_text:
        return None
    source = Path(path_text)
    if not source.exists():
        return path_text
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / (target_name or source.name)
    if source.resolve() != target.resolve() and source.parent.resolve() == target_dir.resolve():
        source.replace(target)
    elif source.resolve() != target.resolve():
        target.write_bytes(source.read_bytes())
    return str(target)


def finite_interp(x_new: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 2:
        raise ValueError("Interpolation source has fewer than two finite points")
    x_src = np.asarray(x[mask], dtype=float)
    y_src = np.asarray(y[mask], dtype=float)
    order = np.argsort(x_src)
    return np.interp(x_new, x_src[order], y_src[order])


def previous_network_amplitude_vpk(run_dir: Path, raw_dir: Path) -> float | None:
    candidates = [run_dir / "run.json", raw_dir / "network_response.json", run_dir / "network_response.json"]
    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        values = [
            payload.get("network", {}).get("amplitude_v") if isinstance(payload.get("network"), dict) else None,
            payload.get("amplitude_v"),
            payload.get("amplitude_vpk"),
        ]
        for value in values:
            parsed = finite_config_float(value, None)
            if parsed is not None:
                return parsed
    return None


def compact_optical_mode_text(mode: dict[str, Any] | None) -> str:
    if not mode:
        return "mode not recorded"
    family = mode.get("family_label") or mode.get("family") or "mode"
    mu = mode.get("mode_number", mode.get("mu", "-"))
    wavelength = finite_config_float(mode.get("wavelength_nm"), None)
    q0 = finite_config_float(mode.get("Q0"), None)
    parts = [str(family), f"mu={mu}"]
    if wavelength is not None:
        parts.append(f"{wavelength:.6f} nm")
    if q0 is not None:
        parts.append(f"Q0={q0/1e6:.3f} M")
    return ", ".join(parts)


def write_sensitivity_result_html(output: Path, result: dict[str, np.ndarray], meta: dict[str, Any]) -> None:
    freq_mhz = result["frequency_hz"] / 1e6
    sens = result["sensitivity_pa_per_sqrt_hz"]
    panels = [
        {
            "title": "Noise PSD",
            "xLabel": "Frequency (MHz)",
            "yLabel": "PSD (dBm/Hz)",
            "fitMechanical": True,
            "series": [{"name": "noise PSD", "color": "#000", "width": 1.7, "data": np.column_stack([freq_mhz, result["noise_dbm_per_hz"]]).tolist()}],
        },
        {
            "title": "Acoustic response",
            "xLabel": "Frequency (MHz)",
            "yLabel": "Response (dBm/Pa²)",
            "series": [{"name": "response", "color": "#000", "width": 1.7, "data": np.column_stack([freq_mhz, result["response_dbm_per_pa2"]]).tolist()}],
        },
        {
            "title": "Sensitivity",
            "xLabel": "Frequency (MHz)",
            "yLabel": "NEP (Pa/√Hz)",
            "yScale": "log",
            "series": [{"name": "sensitivity", "color": "#000", "width": 1.7, "data": np.column_stack([freq_mhz, sens]).tolist()}],
        },
    ]
    payload = json.dumps(json_ready({"title": "Ultrasound sensitivity", "subtitle": meta, "panels": panels}), ensure_ascii=False, separators=(",", ":"))
    content = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Ultrasound sensitivity</title>
<style>
body{{margin:0;background:#fff;color:#000;font-family:Arial,sans-serif}}
#wrap{{padding:16px 20px 28px;max-width:1500px;margin:auto}}
h1{{font-size:26px;margin:0 0 4px;color:#000}}
#sub{{font-size:15px;margin:0 0 14px;color:#000}}
.panel{{border:1px solid #000;border-radius:6px;padding:10px;margin-bottom:14px;background:#fff}}
.panel h2{{font-size:19px;margin:0 0 8px;color:#000}}
.fitbar{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 8px;font-size:14px;color:#000}}
.fitbar button{{border:1px solid #000;border-radius:4px;background:#fff;color:#000;padding:5px 10px;font:14px Arial,sans-serif;cursor:pointer}}
.fitbar button:hover{{background:#f2f2f2}}
.fitResult{{font-weight:700}}
.fitLegend{{display:inline-flex;gap:10px;align-items:center;flex-wrap:wrap}}
.swatch{{display:inline-block;width:22px;height:0;border-top:3px solid #000;vertical-align:middle;margin-right:4px}}
.box{{position:relative;height:360px}}
canvas{{display:block;width:100%;height:100%;background:#fff}}
.selectBox{{position:absolute;display:none;border:1.5px dashed #000;background:rgba(0,0,0,.08);pointer-events:none}}
#tooltip{{position:fixed;display:none;pointer-events:none;background:#fff;border:1px solid #000;border-radius:4px;padding:7px 9px;font-size:14px;box-shadow:0 2px 7px rgba(0,0,0,.25);z-index:10;white-space:nowrap;color:#000}}
.hint{{font-size:13px;margin-top:8px;color:#000}}
</style>
</head>
<body>
<div id="wrap"><h1>Ultrasound sensitivity</h1><div id="sub"></div><div id="panels"></div><div class="hint">Left-drag box zooms, Shift+drag pans, wheel zooms, double click resets. Hover reads data values.</div></div>
<div id="tooltip"></div>
<script>
const payload={payload};
const tooltip=document.getElementById('tooltip');
document.getElementById('sub').textContent = `mode: ${{payload.subtitle.optical_mode_text || 'not recorded'}}; pressure: ${{payload.subtitle.pressure_file || '-'}}; drive: ${{payload.subtitle.actual_drive_vpp || '-'}} Vpp`;
function finite(v){{return Number.isFinite(v)}}
function log10(v){{return Math.log(v)/Math.LN10}}
function formatLog(v){{if(!finite(v)||v<=0)return ''; const e=Math.round(log10(v)); return `10^${{e}}`}}
function range(vals,p=.05,log=false){{let fs=vals.filter(v=>finite(v)&&(!log||v>0));if(!fs.length)return log?[1e-9,1e-3]:[0,1];let arr=log?fs.map(log10):fs;let mn=Math.min(...arr),mx=Math.max(...arr);if(mn===mx){{mn-=1;mx+=1}}let q=(mx-mn)*p;return log?[Math.pow(10,mn-q),Math.pow(10,mx+q)]:[mn-q,mx+q]}}
function niceTicks(a,b,n=6){{const span=Math.abs(b-a)||1,raw=span/n,pow=Math.pow(10,Math.floor(Math.log10(raw))),m=raw/pow,step=(m<1.5?1:m<3?2:m<7?5:10)*pow,start=Math.ceil(a/step)*step,out=[];for(let v=start;v<=b+step*.3;v+=step)out.push(v);return out}}
function logMajorTicks(a,b){{const ea=Math.ceil(log10(a)), eb=Math.floor(log10(b)), out=[];for(let e=ea;e<=eb;e++){{const v=Math.pow(10,e); if(v>=a&&v<=b)out.push(v)}}return out}}
function logMinorTicks(a,b){{const ea=Math.floor(log10(a)), eb=Math.ceil(log10(b)), out=[];for(let e=ea;e<=eb;e++){{for(const m of [2,3,4,5,6,7,8,9]){{const v=m*Math.pow(10,e); if(v>=a&&v<=b)out.push(v)}}}}return out}}
function tickText(v,span){{const a=Math.abs(span);const d=a<.01?5:a<.1?4:a<1?3:a<10?2:1;return v.toFixed(d)}}
function dbmToLinear(v){{return Math.pow(10,v/10)}}
function linearToDbm(v){{return 10*Math.log10(Math.max(v,1e-300))}}
function median(arr){{if(!arr.length)return NaN;const a=[...arr].sort((x,y)=>x-y),m=Math.floor(a.length/2);return a.length%2?a[m]:(a[m-1]+a[m])/2}}
function percentile(arr,p){{if(!arr.length)return NaN;const a=[...arr].sort((x,y)=>x-y),i=Math.min(a.length-1,Math.max(0,Math.round((a.length-1)*p)));return a[i]}}
function mechComponentLinear(x,p){{const f0=Math.max(p[0],1e-12),gamma=Math.max(p[1],1e-12),amp=Math.max(p[2],1e-300),den=(f0*f0-x*x)*(f0*f0-x*x)+(x*gamma)*(x*gamma),peak=(f0*gamma)*(f0*gamma);return amp*peak/Math.max(den,1e-300)}}
function mechSusceptibilityLinear(x,p){{const bg=Math.max(p[3],1e-300);return bg+mechComponentLinear(x,p)}}
function fitMechanicalSusceptibilityVisible(chart){{
  const source=(chart.opt.series[0]&&chart.opt.series[0].data)||[];
  let pts=source.filter(p=>finite(p[0])&&finite(p[1])&&p[0]>=chart.view.x0&&p[0]<=chart.view.x1).map(p=>[p[0],dbmToLinear(p[1]),p[1]]);
  if(pts.length<12)throw new Error('visible range has too few points');
  const stride=Math.max(1,Math.ceil(pts.length/1600));
  pts=pts.filter((_,i)=>i%stride===0);
  const xs=pts.map(p=>p[0]), ys=pts.map(p=>p[1]), ydb=pts.map(p=>p[2]);
  const xMin=Math.min(...xs), xMax=Math.max(...xs), span=Math.max(xMax-xMin,1e-9);
  const edgeN=Math.max(3,Math.floor(pts.length*0.16));
  const bg0=Math.max(1e-300,median([...ys.slice(0,edgeN),...ys.slice(-edgeN)]));
  let imax=0; for(let i=1;i<ys.length;i++) if(ys[i]>ys[imax]) imax=i;
  const f00=xs[imax], amp0=Math.max(ys[imax]-bg0, percentile(ys,.9)-bg0, bg0*0.1, 1e-300);
  const half=bg0+amp0/2;
  let left=xMin,right=xMax;
  for(let i=imax;i>0;i--){{if(ys[i]<=half){{left=xs[i];break}}}}
  for(let i=imax;i<ys.length;i++){{if(ys[i]<=half){{right=xs[i];break}}}}
  const g0=Math.max(Math.min(right-left,span),span/200);
  let q=[f00,Math.log(g0),Math.log(amp0),Math.log(bg0)];
  const score=(qq)=>{{
    const p=[Math.min(xMax,Math.max(xMin,qq[0])),Math.exp(qq[1]),Math.exp(qq[2]),Math.exp(qq[3])];
    let s=0,n=0;
    for(let i=0;i<pts.length;i++){{const m=linearToDbm(mechSusceptibilityLinear(xs[i],p)); if(!finite(m))continue; const r=m-ydb[i]; s+=r*r; n++}}
    const g=p[1];
    if(g<span/3000||g>span*2)s+=1e4;
    return s/Math.max(n,1);
  }};
  let best=score(q), steps=[span/12,Math.log(1.8),Math.log(1.8),Math.log(1.25)];
  for(let iter=0;iter<75;iter++){{
    let improved=false;
    for(let k=0;k<4;k++){{
      let localBest=best, localQ=q;
      for(const dir of [-1,1]){{const cand=q.slice();cand[k]+=dir*steps[k];cand[0]=Math.min(xMax,Math.max(xMin,cand[0]));const sc=score(cand);if(sc<localBest){{localBest=sc;localQ=cand}}}}
      if(localBest<best){{q=localQ;best=localBest;improved=true}}
    }}
    if(!improved)steps=steps.map(v=>v*0.72);
    if(Math.max(...steps)<1e-6)break;
  }}
  const p=[Math.min(xMax,Math.max(xMin,q[0])),Math.exp(q[1]),Math.exp(q[2]),Math.exp(q[3])];
  const fitData=[], mechData=[]; const nfit=360;
  for(let i=0;i<nfit;i++){{const x=xMin+span*i/(nfit-1),mech=mechComponentLinear(x,p);fitData.push([x,linearToDbm(p[3]+mech)]);mechData.push([x,linearToDbm(mech)])}}
  const bgDb=linearToDbm(p[3]), f0=p[0], fwhm=p[1], qm=f0/fwhm;
  return {{data:fitData,mechData,bgDb,f0,fwhm,qm,rmseDb:Math.sqrt(best),x0:xMin,x1:xMax}};
}}
class Chart{{
  constructor(canvas,opt){{this.canvas=canvas;this.box=canvas.parentElement;this.sel=this.box.querySelector('.selectBox');this.ctx=canvas.getContext('2d');this.opt=opt;this.logY=opt.yScale==='log';this.pad={{l:92,r:28,t:18,b:62}};const xs=opt.series.flatMap(s=>s.data.map(p=>p[0])),ys=opt.series.flatMap(s=>s.data.map(p=>p[1]));const xr=range(xs,.02,false),yr=range(ys,.08,this.logY);this.init={{x0:xr[0],x1:xr[1],y0:yr[0],y1:yr[1]}};this.view={{...this.init}};this.drag=false;this.pan=false;this.start=null;this.last=null;this.hoverState=null;this.fit=null;this.bind();this.resize()}}
  W(){{return this.box.clientWidth}} H(){{return this.box.clientHeight}}
  yv(y){{return this.logY?log10(Math.max(y,1e-300)):y}} iyv(v){{return this.logY?Math.pow(10,v):v}}
  sx(x){{return this.pad.l+(x-this.view.x0)/(this.view.x1-this.view.x0)*(this.W()-this.pad.l-this.pad.r)}} sy(y){{const y0=this.yv(this.view.y0),y1=this.yv(this.view.y1);return this.H()-this.pad.b-(this.yv(y)-y0)/(y1-y0)*(this.H()-this.pad.t-this.pad.b)}}
  ix(px){{return this.view.x0+(px-this.pad.l)/(this.W()-this.pad.l-this.pad.r)*(this.view.x1-this.view.x0)}} iy(py){{const y0=this.yv(this.view.y0),y1=this.yv(this.view.y1);return this.iyv(y0+(this.H()-this.pad.b-py)/(this.H()-this.pad.t-this.pad.b)*(y1-y0))}}
  inPlot(x,y){{return x>=this.pad.l&&x<=this.W()-this.pad.r&&y>=this.pad.t&&y<=this.H()-this.pad.b}}
  resize(){{const dpr=window.devicePixelRatio||1;this.canvas.width=Math.round(this.W()*dpr);this.canvas.height=Math.round(this.H()*dpr);this.ctx.setTransform(dpr,0,0,dpr,0,0);this.draw()}}
  bind(){{window.addEventListener('resize',()=>this.resize());this.canvas.addEventListener('dblclick',()=>{{this.view={{...this.init}};this.draw()}});this.canvas.addEventListener('wheel',e=>{{e.preventDefault();const r=this.canvas.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top,cx=this.ix(mx),cy=this.iy(my),f=e.deltaY<0?.82:1.22;this.view.x0=cx-(cx-this.view.x0)*f;this.view.x1=cx+(this.view.x1-cx)*f;const y0=this.yv(this.view.y0),y1=this.yv(this.view.y1),yc=this.yv(cy);this.view.y0=this.iyv(yc-(yc-y0)*f);this.view.y1=this.iyv(yc+(y1-yc)*f);this.draw()}});this.canvas.addEventListener('mousedown',e=>{{const r=this.canvas.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;if(!this.inPlot(mx,my))return;this.drag=true;this.pan=!!e.shiftKey;this.start={{x:mx,y:my}};this.last={{x:e.clientX,y:e.clientY}};if(!this.pan)this.updateSel(mx,my)}});window.addEventListener('mouseup',e=>{{if(!this.drag)return;if(!this.pan&&this.start){{const r=this.canvas.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top,xA=Math.max(this.pad.l,Math.min(this.W()-this.pad.r,this.start.x)),xB=Math.max(this.pad.l,Math.min(this.W()-this.pad.r,mx)),yA=Math.max(this.pad.t,Math.min(this.H()-this.pad.b,this.start.y)),yB=Math.max(this.pad.t,Math.min(this.H()-this.pad.b,my));if(Math.abs(xB-xA)>8&&Math.abs(yB-yA)>8){{this.view={{x0:Math.min(this.ix(xA),this.ix(xB)),x1:Math.max(this.ix(xA),this.ix(xB)),y0:Math.min(this.iy(yA),this.iy(yB)),y1:Math.max(this.iy(yA),this.iy(yB))}};this.draw()}}}}this.drag=false;this.pan=false;this.start=null;this.sel.style.display='none'}});this.canvas.addEventListener('mouseleave',()=>{{tooltip.style.display='none';if(this.hoverState){{this.hoverState=null;this.draw()}}}});this.canvas.addEventListener('mousemove',e=>{{const r=this.canvas.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;if(this.drag&&this.pan){{const dx=e.clientX-this.last.x,dy=e.clientY-this.last.y;this.last={{x:e.clientX,y:e.clientY}};const xs=this.view.x1-this.view.x0,ys=this.yv(this.view.y1)-this.yv(this.view.y0);this.view.x0-=dx/(this.W()-this.pad.l-this.pad.r)*xs;this.view.x1-=dx/(this.W()-this.pad.l-this.pad.r)*xs;const y0=this.yv(this.view.y0)+dy/(this.H()-this.pad.t-this.pad.b)*ys,y1=this.yv(this.view.y1)+dy/(this.H()-this.pad.t-this.pad.b)*ys;this.view.y0=this.iyv(y0);this.view.y1=this.iyv(y1);this.draw();return}}if(this.drag&&!this.pan){{this.updateSel(mx,my);return}}this.hover(e,mx,my)}})}}
  updateSel(mx,my){{const x1=Math.max(this.pad.l,Math.min(this.W()-this.pad.r,this.start.x)),y1=Math.max(this.pad.t,Math.min(this.H()-this.pad.b,this.start.y)),x2=Math.max(this.pad.l,Math.min(this.W()-this.pad.r,mx)),y2=Math.max(this.pad.t,Math.min(this.H()-this.pad.b,my));this.sel.style.left=Math.min(x1,x2)+'px';this.sel.style.top=Math.min(y1,y2)+'px';this.sel.style.width=Math.abs(x2-x1)+'px';this.sel.style.height=Math.abs(y2-y1)+'px';this.sel.style.display='block'}}
  interp(s,xq){{const d=s.data;if(!d||!d.length)return null;let prev=d[0];if(xq<=prev[0])return {{x:prev[0],y:prev[1]}};for(let i=1;i<d.length;i++){{const p=d[i];if(xq<=p[0]){{const t=(xq-prev[0])/((p[0]-prev[0])||1);return {{x:xq,y:prev[1]+(p[1]-prev[1])*t}}}}prev=p}}const last=d[d.length-1];return {{x:last[0],y:last[1]}}}}
  hover(e,mx,my){{if(!this.inPlot(mx,my)){{tooltip.style.display='none';if(this.hoverState){{this.hoverState=null;this.draw()}}return}}const xq=this.ix(mx),rows=[];for(const s of this.opt.series){{const p=this.interp(s,xq);if(!p||!finite(p.y))continue;if(p.y<this.view.y0||p.y>this.view.y1)continue;rows.push({{s,x:p.x,y:p.y}})}}if(!rows.length){{tooltip.style.display='none';if(this.hoverState){{this.hoverState=null;this.draw()}}return}}this.hoverState={{x:xq,rows}};this.draw();tooltip.style.display='block';tooltip.style.left=e.clientX+14+'px';tooltip.style.top=e.clientY+12+'px';const body=rows.map(r=>`<div><b>${{r.s.name}}</b>: ${{this.logY?r.y.toExponential(3):r.y.toFixed(3)}}</div>`).join('');tooltip.innerHTML=`<b>${{this.opt.xLabel}}: ${{xq.toFixed(4)}}</b><br>${{body}}`}}
  drawHover(){{const h=this.hoverState;if(!h)return;const c=this.ctx,x=this.sx(h.x);c.save();c.strokeStyle='#000';c.lineWidth=1;c.setLineDash([4,4]);c.beginPath();c.moveTo(x,this.pad.t);c.lineTo(x,this.H()-this.pad.b);c.stroke();c.setLineDash([]);for(const r of h.rows){{const y=this.sy(r.y);if(!this.inPlot(x,y))continue;c.beginPath();c.arc(x,y,4.5,0,Math.PI*2);c.fillStyle=r.s.color||'#000';c.fill();c.strokeStyle='#000';c.stroke()}}c.restore()}}
  setFit(fit){{this.fit=fit;this.draw()}}
  clearFit(){{this.fit=null;this.draw()}}
  drawFit(){{const f=this.fit;if(!f||!f.data||!f.data.length)return;const c=this.ctx;c.save();c.beginPath();let started=false;for(const p of f.data){{const x=p[0],y=p[1];if(!finite(x)||!finite(y)||x<this.view.x0||x>this.view.x1)continue;const px=this.sx(x),py=this.sy(y);if(!started){{c.moveTo(px,this.sy(f.bgDb));c.lineTo(px,py);started=true}}else c.lineTo(px,py)}}if(started){{for(let i=f.data.length-1;i>=0;i--){{const p=f.data[i],x=p[0];if(!finite(x)||x<this.view.x0||x>this.view.x1)continue;c.lineTo(this.sx(x),this.sy(f.bgDb))}}c.closePath();c.fillStyle='rgba(214,39,40,.12)';c.fill()}}const drawLine=(data,color,width,dash=[])=>{{c.save();c.strokeStyle=color;c.lineWidth=width;c.setLineDash(dash);c.beginPath();let st=false;for(const p of data){{const x=p[0],y=p[1];if(!finite(x)||!finite(y)||x<this.view.x0||x>this.view.x1)continue;const px=this.sx(x),py=this.sy(y);if(!st){{c.moveTo(px,py);st=true}}else c.lineTo(px,py)}}if(st)c.stroke();c.restore()}};drawLine(f.data.map(p=>[p[0],f.bgDb]),'#2ca02c',1.8,[7,5]);drawLine(f.mechData||[],'#1f77b4',1.9,[3,3]);drawLine(f.data,'#d62728',2.5,[]);const x0=this.sx(f.f0);if(finite(x0)){{c.setLineDash([5,4]);c.strokeStyle='#d62728';c.lineWidth=1;c.beginPath();c.moveTo(x0,this.pad.t);c.lineTo(x0,this.H()-this.pad.b);c.stroke()}}c.restore()}}
  draw(){{const c=this.ctx;c.clearRect(0,0,this.W(),this.H());c.fillStyle='#fff';c.fillRect(0,0,this.W(),this.H());c.strokeStyle='#000';c.lineWidth=1.2;c.strokeRect(this.pad.l,this.pad.t,this.W()-this.pad.l-this.pad.r,this.H()-this.pad.t-this.pad.b);const xt=niceTicks(this.view.x0,this.view.x1,6),yt=this.logY?logMajorTicks(this.view.y0,this.view.y1):niceTicks(this.view.y0,this.view.y1,6),ym=this.logY?logMinorTicks(this.view.y0,this.view.y1):[];c.font='14px Arial';if(this.logY){{c.strokeStyle='rgba(0,0,0,.22)';c.lineWidth=.35;for(const v of ym){{const y=this.sy(v);c.beginPath();c.moveTo(this.pad.l,y);c.lineTo(this.W()-this.pad.r,y);c.stroke()}}}}c.strokeStyle='#000';c.lineWidth=.45;c.textAlign='center';c.textBaseline='top';for(const v of xt){{const x=this.sx(v);c.beginPath();c.moveTo(x,this.pad.t);c.lineTo(x,this.H()-this.pad.b);c.stroke();c.fillStyle='#000';c.fillText(tickText(v,this.view.x1-this.view.x0),x,this.H()-this.pad.b+9)}}c.textAlign='right';c.textBaseline='middle';for(const v of yt){{const y=this.sy(v);c.beginPath();c.moveTo(this.pad.l,y);c.lineTo(this.W()-this.pad.r,y);c.stroke();c.fillStyle='#000';c.fillText(this.logY?formatLog(v):tickText(v,this.view.y1-this.view.y0),this.pad.l-9,y)}}c.font='18px Arial';c.textAlign='center';c.textBaseline='bottom';c.fillText(this.opt.xLabel,(this.pad.l+this.W()-this.pad.r)/2,this.H()-10);c.save();c.translate(24,(this.pad.t+this.H()-this.pad.b)/2);c.rotate(-Math.PI/2);c.fillText(this.opt.yLabel,0,0);c.restore();for(const s of this.opt.series){{c.strokeStyle=s.color||'#000';c.lineWidth=s.width||2;c.beginPath();let st=false;for(const p of s.data){{const x=p[0],y=p[1];if(!finite(x)||!finite(y)||(this.logY&&y<=0)||x<this.view.x0||x>this.view.x1)continue;const py=this.sy(y);if(!st){{c.moveTo(this.sx(x),py);st=true}}else c.lineTo(this.sx(x),py)}}c.stroke()}}this.drawFit();this.drawHover()}}
}}
const root=document.getElementById('panels');
function installMechanicalFit(panel,chart){{
  const bar=document.createElement('div');bar.className='fitbar';
  const btn=document.createElement('button');btn.textContent='Fit visible mechanical peak';
  const res=document.createElement('span');res.className='fitResult';res.textContent='zoom to one peak, then fit';
  btn.onclick=()=>{{if(chart.fit){{chart.clearFit();btn.textContent='Fit visible mechanical peak';res.textContent='fit cleared';return}}try{{const fit=fitMechanicalSusceptibilityVisible(chart);chart.setFit(fit);btn.textContent='Clear fit';res.textContent=`f0=${{fit.f0.toFixed(6)}} MHz, Gamma/FWHM~${{(fit.fwhm*1e3).toFixed(2)}} kHz, Q~${{fit.qm.toFixed(0)}}, rms=${{fit.rmseDb.toFixed(2)}} dB`}}catch(err){{res.textContent='fit failed: '+err.message}}}};
  const legend=document.createElement('span');legend.className='fitLegend';legend.innerHTML='<span><i class="swatch" style="border-color:#d62728"></i>total</span><span><i class="swatch" style="border-color:#1f77b4;border-top-style:dashed"></i>mechanical</span><span><i class="swatch" style="border-color:#2ca02c;border-top-style:dashed"></i>background</span>';
  bar.appendChild(btn);bar.appendChild(res);bar.appendChild(legend);panel.insertBefore(bar,panel.querySelector('.box'));
}}
payload.panels.forEach(p=>{{const panel=document.createElement('div');panel.className='panel';panel.innerHTML=`<h2>${{p.title}}</h2><div class="box"><canvas></canvas><div class="selectBox"></div></div>`;root.appendChild(panel);const chart=new Chart(panel.querySelector('canvas'),p);if(p.fitMechanical)installMechanicalFit(panel,chart)}});
</script>
</body>
</html>
"""
    output.write_text(content, encoding="utf-8")


def write_sensitivity_png(output: Path, result: dict[str, np.ndarray], summary: dict[str, Any]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    freq_mhz = result["frequency_hz"] / 1e6
    plt.rcParams.update(
        {
            "font.family": "Arial",
            "font.size": 12,
            "axes.labelsize": 13,
            "axes.titlesize": 14,
            "xtick.labelsize": 11,
            "ytick.labelsize": 11,
        }
    )
    fig, axes = plt.subplots(3, 1, figsize=(8.0, 8.5), sharex=True, constrained_layout=True)
    mode_text = summary.get("optical_mode_text") or "mode not recorded"
    fig.suptitle(f"Optical mode: {mode_text}", fontsize=15)
    axes[0].plot(freq_mhz, result["noise_dbm_per_hz"], color="black", lw=0.9)
    axes[0].set_ylabel("PSD (dBm/Hz)")
    axes[0].set_title("Noise PSD")
    axes[1].plot(freq_mhz, result["response_dbm_per_pa2"], color="black", lw=0.9)
    axes[1].set_ylabel("Response (dBm/Pa$^2$)")
    axes[1].set_title("Acoustic response")
    axes[2].semilogy(freq_mhz, result["sensitivity_pa_per_sqrt_hz"], color="black", lw=0.9)
    axes[2].set_ylabel("NEP (Pa/$\\sqrt{Hz}$)")
    axes[2].set_xlabel("Frequency (MHz)")
    axes[2].set_title(
        f"Sensitivity: median {summary['median_pa_per_sqrt_hz']:.2e}, best {summary['best_pa_per_sqrt_hz']:.2e} Pa/√Hz"
    )
    for ax in axes:
        ax.grid(True, which="both", alpha=0.35)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=180)
    plt.close(fig)


def compute_sensitivity_result(
    *,
    run_dir: Path,
    raw_dir: Path,
    processed_dir: Path,
    figures_dir: Path,
    pressure: dict[str, Any],
    requested_settings: dict[str, Any],
    spectrum: dict[str, Any] | None,
    network: dict[str, Any] | None,
) -> dict[str, Any]:
    noise_path = raw_dir / "noise.npz"
    network_path = raw_dir / "network_response.npz"
    if not noise_path.exists():
        spectrum_path = (spectrum or {}).get("path")
        noise_path = Path(str(spectrum_path)) if spectrum_path else run_dir / "noise.npz"
    if not network_path.exists():
        network_path_text = (network or {}).get("path")
        network_path = Path(str(network_path_text)) if network_path_text else run_dir / "network_response.npz"
    if not noise_path.exists() or not network_path.exists():
        raise FileNotFoundError("Sensitivity calculation requires both raw/noise.npz and raw/network_response.npz")

    noise_data = np.load(str(noise_path), allow_pickle=False)
    network_data = np.load(str(network_path), allow_pickle=False)
    noise_freq = np.asarray(noise_data["frequency_hz"], dtype=float)
    noise_psd = np.asarray(noise_data["input1_dbm_per_hz"], dtype=float)
    net_freq = np.asarray(network_data["frequency_hz"], dtype=float)
    net_mag = np.asarray(network_data["magnitude_dbm"], dtype=float)
    net_phase = np.asarray(network_data["phase_deg"], dtype=float)

    requested_pressure = requested_settings.get("pressure") or {}
    use_start = finite_config_float(requested_pressure.get("use_start_hz"), pressure["use_start_hz"]) or pressure["use_start_hz"]
    use_stop = finite_config_float(requested_pressure.get("use_stop_hz"), pressure["use_stop_hz"]) or pressure["use_stop_hz"]
    lo = max(float(use_start), float(np.nanmin(noise_freq)), float(np.nanmin(net_freq)), pressure["start_hz"])
    hi = min(float(use_stop), float(np.nanmax(noise_freq)), float(np.nanmax(net_freq)), pressure["stop_hz"])
    if not hi > lo:
        raise ValueError(f"No overlapping frequency range for sensitivity calculation: {lo:g}..{hi:g} Hz")
    mask = np.isfinite(net_freq) & (net_freq >= lo) & (net_freq <= hi)
    freq = net_freq[mask]
    if len(freq) < 2:
        raise ValueError("Network response has fewer than two points inside pressure calibration range")
    mag = net_mag[mask]
    phase = net_phase[mask]
    noise_interp = finite_interp(freq, noise_freq, noise_psd)
    pressure_interp = finite_interp(freq, pressure["frequency_hz"], pressure["pressure_pa"])
    network_settings = requested_settings.get("network") or {}
    actual_drive_vpk = finite_config_float(network_settings.get("amplitude_vpk"), None)
    if actual_drive_vpk is None:
        actual_drive_vpk = (
            finite_config_float((network or {}).get("amplitude_v"), None)
            or previous_network_amplitude_vpk(run_dir, raw_dir)
            or 1.0
        )
    actual_drive_vpp = actual_drive_vpk * 2.0
    calibration_drive_vpp = float(pressure["calibration_drive_vpp"])
    pressure_scaled = pressure_interp * (actual_drive_vpp / calibration_drive_vpp)
    pressure_quantity = str(pressure["pressure_quantity"]).lower()
    pressure_rms = pressure_scaled if "rms" in pressure_quantity else pressure_scaled / math.sqrt(2.0)
    response_dbm_per_pa2 = mag - 20.0 * np.log10(np.maximum(pressure_rms, 1e-300))
    sensitivity_db = noise_interp - response_dbm_per_pa2
    sensitivity = 10.0 ** (sensitivity_db / 20.0)
    optical_mode = requested_settings.get("optical_mode") if isinstance(requested_settings.get("optical_mode"), dict) else None
    optical_mode_text = compact_optical_mode_text(optical_mode)
    result = {
        "frequency_hz": freq,
        "noise_dbm_per_hz": noise_interp,
        "network_magnitude_dbm": mag,
        "network_phase_deg": phase,
        "pressure_pa_at_calibration_drive": pressure_interp,
        "pressure_pk_pa": pressure_scaled if "rms" not in pressure_quantity else pressure_scaled * math.sqrt(2.0),
        "pressure_rms_pa": pressure_rms,
        "response_dbm_per_pa2": response_dbm_per_pa2,
        "sensitivity_db_pa_per_sqrt_hz": sensitivity_db,
        "sensitivity_pa_per_sqrt_hz": sensitivity,
    }
    processed_dir.mkdir(parents=True, exist_ok=True)
    figures_dir.mkdir(parents=True, exist_ok=True)
    result_npz = processed_dir / "sensitivity_result.npz"
    np.savez(result_npz, **result)

    best_idx = int(np.nanargmin(sensitivity))
    summary = {
        "frequency_start_hz": float(freq[0]),
        "frequency_stop_hz": float(freq[-1]),
        "points": int(len(freq)),
        "median_pa_per_sqrt_hz": float(np.nanmedian(sensitivity)),
        "best_pa_per_sqrt_hz": float(sensitivity[best_idx]),
        "best_frequency_hz": float(freq[best_idx]),
        "median_db_pa_per_sqrt_hz": float(20.0 * np.log10(np.nanmedian(sensitivity))),
        "best_db_pa_per_sqrt_hz": float(20.0 * np.log10(sensitivity[best_idx])),
        "actual_drive_vpp": float(actual_drive_vpp),
        "pressure_calibration_drive_vpp": float(calibration_drive_vpp),
        "pressure_quantity": pressure_quantity,
        "pressure_file": pressure["path"],
        "optical_mode": optical_mode,
        "optical_mode_text": optical_mode_text,
    }
    meta = {
        "run_dir": str(run_dir),
        "raw_noise_npz": str(noise_path),
        "raw_network_npz": str(network_path),
        "result_npz": str(result_npz),
        "pressure": {
            "path": pressure["path"],
            "array_key": pressure["pressure_array_key"],
            "quantity": pressure_quantity,
            "calibration_drive_vpp": calibration_drive_vpp,
            "requested_start_hz": float(use_start),
            "requested_stop_hz": float(use_stop),
        },
        "optical_mode": optical_mode,
        "optical_mode_text": optical_mode_text,
        "summary": summary,
        "formula": {
            "pressure_rms": "pressure_calibrated * actual_drive_vpp / calibration_drive_vpp / sqrt(2) when pressure calibration is peak",
            "response_dbm_per_pa2": "network_magnitude_dbm - 20*log10(pressure_rms_pa)",
            "sensitivity_pa_per_sqrt_hz": "10**((noise_dbm_per_hz - response_dbm_per_pa2)/20)",
        },
    }
    result_json = processed_dir / "sensitivity_result.json"
    result_json.write_text(json.dumps(json_ready(meta), ensure_ascii=False, indent=2), encoding="utf-8")
    result_html = figures_dir / "sensitivity_result.html"
    write_sensitivity_result_html(result_html, result, {**summary, "pressure_file": Path(pressure["path"]).name, "optical_mode_text": optical_mode_text})
    png_path = figures_dir / "sensitivity_summary.png"
    write_sensitivity_png(png_path, result, summary)
    return {
        "ok": True,
        "result_npz": str(result_npz),
        "result_json": str(result_json),
        "result_html": str(result_html),
        "summary_png": str(png_path),
        "summary": summary,
    }


def rel_to(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve())).replace("\\", "/")
    except Exception:
        return str(path)


def update_sensitivity_latest(
    cavity_dir: Path,
    run_dir: Path,
    sensitivity: dict[str, Any],
    plots: dict[str, str] | None = None,
) -> Path:
    latest_path = cavity_dir / "sensitivity" / "latest.json"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    plots = plots or {}
    payload = {
        "run_dir": rel_to(run_dir, latest_path.parent),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "figure_png": rel_to(Path(sensitivity["summary_png"]), cavity_dir),
        "interactive_html": rel_to(Path(sensitivity["result_html"]), cavity_dir),
        "noise_psd_html": rel_to(Path(plots["noise_psd_html"]), cavity_dir) if plots.get("noise_psd_html") else None,
        "network_response_html": rel_to(Path(plots["network_response_html"]), cavity_dir) if plots.get("network_response_html") else None,
        "result_json": rel_to(Path(sensitivity["result_json"]), cavity_dir),
        "result_npz": rel_to(Path(sensitivity["result_npz"]), cavity_dir),
        "summary": sensitivity.get("summary", {}),
    }
    latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return latest_path


def sensitivity_summary_text(summary: dict[str, Any]) -> str:
    if not summary:
        return "measured"
    median = summary.get("median_pa_per_sqrt_hz")
    best = summary.get("best_pa_per_sqrt_hz")
    best_freq = summary.get("best_frequency_hz")
    mode_text = summary.get("optical_mode_text")
    if median is None or best is None or best_freq is None:
        return "measured"
    prefix = f"{mode_text}; " if mode_text and mode_text != "mode not recorded" else ""
    return f"{prefix}median {median:.2e} Pa/sqrtHz; best {best:.2e} @ {best_freq/1e3:.1f} kHz"


def refresh_card_for_sensitivity(cavity_dir_text: str | None, summary: dict[str, Any]) -> dict[str, Any] | None:
    if not cavity_dir_text:
        return None
    context = infer_cavity_context(cavity_dir_text)
    command = [
        package_python_executable(),
        str(DEFAULT_CARD_SCRIPT),
        "--chip",
        context["chip"],
        "--die",
        context["die"],
        "--cavity",
        context["cavity"],
        "--results-root",
        str(context["results_root"]),
        "--sensitivity",
        sensitivity_summary_text(summary),
    ]
    return run_script(command, timeout_s=60.0)


def start_sensitivity_job(mode: str, tag_prefix: str, base: str) -> bool:
    with ACTIVE_SENSITIVITY_LOCK:
        if ACTIVE_SENSITIVITY.get("running"):
            return False
        ACTIVE_SENSITIVITY.update(
            {
                "running": True,
                "cancel_requested": False,
                "started_at": time.time(),
                "mode": mode,
                "tag_prefix": tag_prefix,
                "bridge_base": base,
            }
        )
        return True


def finish_sensitivity_job() -> None:
    with ACTIVE_SENSITIVITY_LOCK:
        ACTIVE_SENSITIVITY.update({"running": False})


def sensitivity_cancel_requested() -> bool:
    with ACTIVE_SENSITIVITY_LOCK:
        return bool(ACTIVE_SENSITIVITY.get("cancel_requested"))


def check_sensitivity_cancelled() -> None:
    if sensitivity_cancel_requested():
        raise SensitivityCancelled("sensitivity acquisition cancelled by user")


def sensitivity_status() -> dict[str, Any]:
    with ACTIVE_SENSITIVITY_LOCK:
        status = dict(ACTIVE_SENSITIVITY)
    started = status.get("started_at")
    status["elapsed_s"] = time.time() - started if status.get("running") and started else None
    return {"ok": True, "sensitivity_acquisition": status}


def cancel_sensitivity_acquisition(body: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    config = instrument_config_from_mapping(body, args)
    with ACTIVE_SENSITIVITY_LOCK:
        was_running = bool(ACTIVE_SENSITIVITY.get("running"))
        ACTIVE_SENSITIVITY["cancel_requested"] = True
        base = str(ACTIVE_SENSITIVITY.get("bridge_base") or config["bridge_base"]).rstrip("/")
    stop_result = None
    if config["rp_type"] != "none":
        try:
            stop_result = bridge_query(base, "/acquisition/stop", {}, timeout=2.0)
        except Exception as exc:
            stop_result = {"ok": False, "error": repr(exc)}
    return {
        "ok": True,
        "message": "cancel requested" if was_running else "no active sensitivity acquisition",
        "was_running": was_running,
        "bridge_stop": stop_result,
        **sensitivity_status(),
    }


def run_sensitivity_acquisition(body: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    config = instrument_config_from_mapping(body, args)
    if config["rp_type"] == "none":
        raise ValueError("Sensitivity acquisition requires RP / PyRPL bridge")
    base = config["bridge_base"].rstrip("/")
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    tag_prefix = safe_tag_text(body.get("tag_prefix"), "sensitivity")
    mode = str(body.get("acquisition_mode") or "both").strip().lower()
    if mode not in {"psd", "network", "both", "compute"}:
        raise ValueError(f"Unknown sensitivity acquisition mode: {mode}")
    spectrum_timeout = finite_config_float(body.get("spectrum_timeout_s"), 30.0) or 30.0
    network_timeout = finite_config_float(body.get("network_timeout_s"), 600.0) or 600.0
    max_points = int(finite_config_float(body.get("max_points"), 1500.0) or 1500)
    acquire = bool_from_config(body.get("acquire"), True)
    requested_settings = sensitivity_requested_settings(body)
    pressure = pressure_calibration_from_body(body, args)
    if not start_sensitivity_job(mode, tag_prefix, base):
        return {
            "ok": False,
            "message": "another sensitivity acquisition is already active",
            **sensitivity_status(),
        }
    try:
        if mode == "compute":
            cavity_text = str(body.get("cavity_dir") or "").strip()
            if not cavity_text:
                raise ValueError("Compute-only sensitivity requires a selected cavity directory with previous raw data")
            cavity_dir = Path(cavity_text).expanduser()
            latest_run = latest_sensitivity_run_dir(cavity_dir)
            if latest_run is None:
                raise FileNotFoundError(f"No previous sensitivity raw data found under {cavity_dir / 'sensitivity'}")
            run_dir, no_cavity_dir, cavity_dir_text = latest_run, False, str(cavity_dir)
        else:
            run_dir, no_cavity_dir, cavity_dir_text = resolve_sensitivity_run_dir(body, tag_prefix, timestamp)
        raw_dir = run_dir / "raw"
        processed_dir = run_dir / "processed"
        figures_dir = run_dir / "figures"
        run_dir.mkdir(parents=True, exist_ok=True)
        raw_dir.mkdir(parents=True, exist_ok=True)
        figures_dir.mkdir(parents=True, exist_ok=True)

        started = time.time()
        steps: list[dict[str, Any]] = []
        spectrum: dict[str, Any] | None = None
        network: dict[str, Any] | None = None
        applied_settings = {"ok": True, "steps": []}
        if mode != "compute":
            check_sensitivity_cancelled()
            applied_settings = apply_sensitivity_settings(base, mode, requested_settings)
            check_sensitivity_cancelled()
            steps.append({"name": "apply_settings", "ok": bool(applied_settings.get("ok")), "result": applied_settings})
            if not applied_settings.get("ok"):
                run_payload = {
                    "ok": False,
                    "message": "instrument parameter update failed; acquisition was not run",
                    "elapsed_s": time.time() - started,
                    "acquisition_mode": mode,
                    "tag_prefix": tag_prefix,
                    "timestamp": timestamp,
                    "output_dir": str(run_dir),
                    "cavity_dir": cavity_dir_text,
                    "no_cavity_dir": no_cavity_dir,
                    "requested_settings": requested_settings,
                    "applied_settings": applied_settings,
                    "pressure_calibration": {k: v for k, v in pressure.items() if k not in {"frequency_hz", "pressure_pa"}},
                    "steps": steps,
                }
                run_json = run_dir / "run.json"
                run_payload["run_json_path"] = str(run_json)
                run_json.write_text(json.dumps(run_payload, ensure_ascii=False, indent=2), encoding="utf-8")
                return run_payload

            if mode in {"psd", "both"}:
                check_sensitivity_cancelled()
                spectrum = bridge_query(
                    base,
                    "/spectrum/single",
                    {
                        "tag": "noise",
                        "timeout": spectrum_timeout,
                        "save": "true",
                        "save_csv": "false",
                        "inline": "false",
                        "acquire": "true" if acquire else "false",
                        "max_points": max_points,
                        "output_dir": str(raw_dir),
                    },
                    timeout=max(20.0, spectrum_timeout + 15.0),
                )
                check_sensitivity_cancelled()
                spectrum["path"] = copy_to_subdir_if_needed(spectrum.get("path"), raw_dir, "noise.npz")
                spectrum["metadata_path"] = copy_to_subdir_if_needed(spectrum.get("metadata_path"), raw_dir, "noise.json")
                steps.append({"name": "spectrum_noise", "ok": bool(spectrum.get("ok")), "result": spectrum})
                if mode == "both" and not spectrum.get("ok"):
                    run_payload = {
                        "ok": False,
                        "message": "spectrum acquisition failed; network analyzer was not run",
                        "elapsed_s": time.time() - started,
                        "acquisition_mode": mode,
                        "tag_prefix": tag_prefix,
                        "timestamp": timestamp,
                        "output_dir": str(run_dir),
                        "raw_dir": str(raw_dir),
                        "cavity_dir": cavity_dir_text,
                        "no_cavity_dir": no_cavity_dir,
                        "requested_settings": requested_settings,
                        "applied_settings": applied_settings,
                        "pressure_calibration": {k: v for k, v in pressure.items() if k not in {"frequency_hz", "pressure_pa"}},
                        "steps": steps,
                    }
                    run_json = run_dir / "run.json"
                    run_json.write_text(json.dumps(run_payload, ensure_ascii=False, indent=2), encoding="utf-8")
                    run_payload["run_json_path"] = str(run_json)
                    return run_payload

            if mode in {"network", "both"}:
                check_sensitivity_cancelled()
                network = bridge_query(
                    base,
                    "/networkanalyzer/single",
                    {
                        "tag": "network_response",
                        "timeout": network_timeout,
                        "save": "true",
                        "inline": "false",
                        "acquire": "true" if acquire else "false",
                        "max_points": max_points,
                        "output_dir": str(raw_dir),
                    },
                    timeout=max(30.0, network_timeout + 15.0),
                )
                check_sensitivity_cancelled()
                network["path"] = copy_to_subdir_if_needed(network.get("path"), raw_dir, "network_response.npz")
                network["metadata_path"] = copy_to_subdir_if_needed(network.get("metadata_path"), raw_dir, "network_response.json")
                steps.append({"name": "network_response", "ok": bool(network.get("ok")), "result": network})

        check_sensitivity_cancelled()
        plots = write_sensitivity_preview_plots(
            run_dir=figures_dir,
            tag_prefix=tag_prefix,
            timestamp=timestamp,
            spectrum=spectrum,
            network=network,
            max_points=max_points,
        )
        sensitivity: dict[str, Any] | None = None
        sensitivity_error: str | None = None
        if mode == "both" or mode == "compute":
            try:
                check_sensitivity_cancelled()
                sensitivity = compute_sensitivity_result(
                    run_dir=run_dir,
                    raw_dir=raw_dir,
                    processed_dir=processed_dir,
                    figures_dir=figures_dir,
                    pressure=pressure,
                    requested_settings=requested_settings,
                    spectrum=spectrum,
                    network=network,
                )
                plots["sensitivity_html"] = sensitivity["result_html"]
                plots["sensitivity_png"] = sensitivity["summary_png"]
                steps.append({"name": "compute_sensitivity", "ok": True, "result": sensitivity})
                if cavity_dir_text:
                    latest_path = update_sensitivity_latest(Path(cavity_dir_text), run_dir, sensitivity, plots)
                    card_result = refresh_card_for_sensitivity(cavity_dir_text, sensitivity.get("summary", {}))
                    steps.append(
                        {
                            "name": "refresh_sensitivity_card",
                            "ok": bool(card_result.get("ok")) if card_result else False,
                            "latest_path": str(latest_path),
                            "result": card_result,
                        }
                    )
            except SensitivityCancelled:
                raise
            except Exception as exc:
                sensitivity_error = repr(exc)
                steps.append({"name": "compute_sensitivity", "ok": False, "error": sensitivity_error})
        ok = all(bool(step.get("ok")) for step in steps)
        message_by_mode = {
            "psd": "sensitivity acquisition finished: noise PSD",
            "network": "sensitivity acquisition finished: network response",
            "both": "sensitivity acquisition finished: noise PSD + network response + sensitivity",
            "compute": "sensitivity recomputed from existing raw data",
        }
        run_payload = {
            "ok": ok,
            "message": message_by_mode[mode],
            "elapsed_s": time.time() - started,
            "acquisition_mode": mode,
            "tag_prefix": tag_prefix,
            "timestamp": timestamp,
            "output_dir": str(run_dir),
            "raw_dir": str(raw_dir),
            "processed_dir": str(processed_dir),
            "figures_dir": str(figures_dir),
            "cavity_dir": cavity_dir_text,
            "no_cavity_dir": no_cavity_dir,
            "requested_settings": requested_settings,
            "applied_settings": applied_settings,
            "pressure_calibration": {k: v for k, v in pressure.items() if k not in {"frequency_hz", "pressure_pa"}},
            "spectrum": {
                "raw_path": spectrum.get("path"),
                "metadata_path": spectrum.get("metadata_path"),
                "rbw_hz": spectrum.get("rbw_hz"),
                "n": spectrum.get("n"),
                "display_power_correction": spectrum.get("display_power_correction"),
                "summary": spectrum.get("summary", {}),
                "plot_html": plots.get("noise_psd_html"),
            } if spectrum else None,
            "network": {
                "raw_path": network.get("path"),
                "metadata_path": network.get("metadata_path"),
                "n": network.get("n"),
                "input": network.get("input"),
                "output_direct": network.get("output_direct"),
                "amplitude_v": network.get("amplitude_v"),
                "amplitude_unit": network.get("amplitude_unit"),
                "power_display": network.get("power_display"),
                "summary": network.get("summary", {}),
                "plot_html": plots.get("network_response_html"),
            } if network else None,
            "sensitivity": {
                "error": sensitivity_error,
                **(sensitivity or {}),
            } if (sensitivity or sensitivity_error) else None,
            "plots": plots,
            "steps": steps,
        }
        run_json = run_dir / ("compute_latest.json" if mode == "compute" else "run.json")
        run_payload["run_json_path"] = str(run_json)
        run_json.write_text(json.dumps(run_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return run_payload
    except SensitivityCancelled as exc:
        return {
            "ok": False,
            "cancelled": True,
            "message": str(exc),
            "acquisition_mode": mode,
            "tag_prefix": tag_prefix,
            "timestamp": timestamp,
        }
    finally:
        finish_sensitivity_job()


def normalize_rp_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


def rp_host_default_external_gain_db(host: str) -> float:
    normalized = normalize_rp_host(host)
    if normalized in {"rp-f0cb0d", "rp-f0cb0d.local", "192.168.1.21"}:
        return DEFAULT_RP_F0CB0D_EXTERNAL_GAIN_DB
    return float(os.environ.get("SPECTRUM_EXTERNAL_GAIN_DB", "0") or 0)


def check_rp_host_resolution(host: str) -> dict[str, Any]:
    if not host.strip():
        return {"ok": False, "error": "RP host is empty"}
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as exc:
        return {
            "ok": False,
            "host": host,
            "error": repr(exc),
            "hint": "If .local resolves through a virtual adapter, try the bare hostname such as RP-f0cb0d.",
        }

    addresses: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for family, _type, _proto, _canon, sockaddr in infos:
        address = str(sockaddr[0])
        key = (family, address)
        if key in seen:
            continue
        seen.add(key)
        addresses.append(
            {
                "family": "IPv4" if family == socket.AF_INET else "IPv6" if family == socket.AF_INET6 else str(family),
                "address": address,
            }
        )
    warnings = []
    if host.lower().endswith(".local") and any(item["address"].startswith("198.18.") for item in addresses):
        warnings.append("This .local name resolved to a 198.18.x.x virtual-adapter address; use RP-f0cb0d instead.")
    return {
        "ok": bool(addresses),
        "host": host,
        "addresses": addresses,
        "warnings": warnings,
        "external_gain_db_if_started": rp_host_default_external_gain_db(host),
    }


def preferred_ipv4_address(host_check: dict[str, Any], fallback: str) -> str:
    for item in host_check.get("addresses") or []:
        if item.get("family") == "IPv4" and item.get("address"):
            return str(item["address"])
    return fallback


def bridge_runtime_status(bridge_base: str) -> dict[str, Any]:
    with ACTIVE_BRIDGE_LOCK:
        proc = ACTIVE_BRIDGE.get("proc")
        started_at = ACTIVE_BRIDGE.get("started_at")
        command = ACTIVE_BRIDGE.get("command")
        host = ACTIVE_BRIDGE.get("rp_host")
        pyrpl_host = ACTIVE_BRIDGE.get("pyrpl_host")
        external_gain_db = ACTIVE_BRIDGE.get("external_gain_db")
        device_profile = ACTIVE_BRIDGE.get("device_profile")
        headless = ACTIVE_BRIDGE.get("headless")
        log_path = ACTIVE_BRIDGE.get("log_path")
        python_executable = ACTIVE_BRIDGE.get("python_executable")
    process = {
        "managed": proc is not None,
        "running": bool(proc is not None and proc.poll() is None),
        "pid": proc.pid if proc is not None else None,
        "returncode": proc.poll() if proc is not None else None,
        "rp_host": host,
        "pyrpl_host": pyrpl_host,
        "external_gain_db": external_gain_db,
        "device_profile": device_profile,
        "headless": headless,
        "python_executable": python_executable,
        "elapsed_s": time.time() - float(started_at) if started_at else None,
        "command": command,
        "log_path": str(log_path) if log_path else None,
        "log_tail": read_text_tail(log_path),
    }
    try:
        health = request_json(f"{bridge_base.rstrip('/')}/health", timeout=1.5)
        http_ok = bool(health.get("ok"))
    except Exception as exc:
        health = {"ok": False, "error": repr(exc)}
        http_ok = False
    return {"ok": http_ok, "bridge_base": bridge_base, "process": process, "health": health}


def bridge_listen_address(bridge_base: str) -> tuple[str, int]:
    parsed = urlparse(bridge_base)
    host = parsed.hostname or DEFAULT_BRIDGE_LISTEN_HOST
    port = parsed.port or DEFAULT_BRIDGE_LISTEN_PORT
    if host in {"localhost", "::1"}:
        host = "127.0.0.1"
    return host, int(port)


def listening_pids_for_tcp_port(port: int) -> list[int]:
    if not sys.platform.startswith("win"):
        return []
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            text=True,
            capture_output=True,
            timeout=5.0,
            check=False,
        )
    except Exception:
        return []
    pids: set[int] = set()
    port_suffix = f":{port}"
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        local_address, state, pid_text = parts[1], parts[3].upper(), parts[4]
        if state != "LISTENING":
            continue
        if not (local_address.endswith(port_suffix) or local_address.endswith(f"]{port_suffix}")):
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid != os.getpid():
            pids.add(pid)
    return sorted(pids)


def kill_pids(pids: list[int]) -> dict[str, Any]:
    results = []
    ok = True
    for pid in pids:
        try:
            taskkill = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                text=True,
                capture_output=True,
                timeout=10.0,
                check=False,
            )
            item = {
                "pid": pid,
                "returncode": taskkill.returncode,
                "stdout_tail": taskkill.stdout[-1200:],
                "stderr_tail": taskkill.stderr[-1200:],
            }
            item["ok"] = taskkill.returncode == 0
            ok = ok and item["ok"]
        except Exception as exc:
            item = {"pid": pid, "ok": False, "error": repr(exc)}
            ok = False
        results.append(item)
    return {"ok": ok, "pids": pids, "results": results}


def bridge_command(
    config: dict[str, str],
    args: argparse.Namespace,
    external_gain_db: float,
    *,
    headless: bool,
    pyrpl_host: str | None = None,
) -> list[str]:
    listen_host, listen_port = bridge_listen_address(config["bridge_base"])
    device_profile = getattr(args, "device_profile", {}) or {}
    command = [
        package_python_executable(),
        str(BRIDGE_DIR / "pyrpl_live_bridge.py"),
        "--config",
        DEFAULT_BRIDGE_CONFIG,
        "--hostname",
        pyrpl_host or config["rp_host"],
        "--listen-host",
        listen_host,
        "--listen-port",
        str(listen_port),
        "--loglevel",
        os.environ.get("PYRPL_BRIDGE_LOGLEVEL", "debug"),
        "--allow-risky",
        "--spectrum-load-ohm",
        os.environ.get("SPECTRUM_LOAD_OHM", "50"),
        "--spectrum-power-correction-enabled",
        os.environ.get("SPECTRUM_POWER_CORRECTION_ENABLED", "true"),
        "--spectrum-highz-correction-db",
        os.environ.get("SPECTRUM_HIGHZ_CORRECTION_DB", "6.0206"),
        "--spectrum-external-gain-db",
        f"{external_gain_db:g}",
        "--scope-ch1-response-v-per-w",
        f"{float(device_profile.get('scope_ch1_response_v_per_w', 3.22013e3)):g}",
        "--scope-ch2-response-v-per-w",
        f"{float(device_profile.get('scope_ch2_response_v_per_w', 3.22013e3)):g}",
        "--scope-zero-enabled",
        "true" if device_profile.get("scope_zero_enabled") else "false",
        "--scope-ch1-zero-offset-v",
        f"{float(device_profile.get('scope_ch1_zero_offset_v') or 0.0):g}",
        "--scope-ch2-zero-offset-v",
        f"{float(device_profile.get('scope_ch2_zero_offset_v') or 0.0):g}",
    ]
    if headless:
        command.append("--headless")
    return command


def stop_managed_bridge(bridge_base: str) -> dict[str, Any]:
    with ACTIVE_BRIDGE_LOCK:
        proc = ACTIVE_BRIDGE.get("proc")
        command = ACTIVE_BRIDGE.get("command")
        started_at = ACTIVE_BRIDGE.get("started_at")
    if proc is None:
        status = bridge_runtime_status(bridge_base)
        return {
            "ok": not bool(status.get("ok")),
            "message": (
                "no dashboard-managed bridge process; bridge port is already free"
                if not status.get("ok")
                else "bridge is reachable but was not started by this dashboard; use Refresh bridge status to keep using it, or stop the background process manually if you need a clean restart"
            ),
            "status": status,
        }
    if proc.poll() is not None:
        with ACTIVE_BRIDGE_LOCK:
            if ACTIVE_BRIDGE.get("proc") is proc:
                ACTIVE_BRIDGE.update({"proc": None})
        return {"ok": True, "message": "managed bridge already exited", "returncode": proc.returncode, "command": command}
    stop = terminate_process_tree(proc)
    with ACTIVE_BRIDGE_LOCK:
        if ACTIVE_BRIDGE.get("proc") is proc:
            ACTIVE_BRIDGE.update({"proc": None})
    return {
        "ok": bool(stop.get("ok")),
        "message": "managed bridge stopped",
        "elapsed_s": time.time() - float(started_at or time.time()),
        "command": command,
        "stop": stop,
    }


def stop_any_bridge_on_url(bridge_base: str) -> dict[str, Any]:
    base = bridge_base.rstrip("/")
    before = bridge_runtime_status(bridge_base)
    if not before.get("ok"):
        return {"ok": True, "message": "bridge URL is already stopped", "before": before}

    shutdown: dict[str, Any]
    try:
        shutdown = request_json(f"{base}/shutdown", timeout=2.0)
    except Exception as exc:
        shutdown = {"ok": False, "error": repr(exc)}

    stopped_by_shutdown = False
    for _ in range(20):
        time.sleep(0.25)
        status = bridge_runtime_status(bridge_base)
        if not status.get("ok"):
            stopped_by_shutdown = True
            break

    if stopped_by_shutdown:
        with ACTIVE_BRIDGE_LOCK:
            ACTIVE_BRIDGE.update({"proc": None})
        return {
            "ok": True,
            "message": "bridge stopped through /shutdown",
            "before": before,
            "shutdown": shutdown,
        }

    _host, port = bridge_listen_address(bridge_base)
    pids = listening_pids_for_tcp_port(port)
    killed = kill_pids(pids) if pids else {"ok": False, "pids": [], "message": "no listening PID found"}

    final = bridge_runtime_status(bridge_base)
    if not final.get("ok"):
        with ACTIVE_BRIDGE_LOCK:
            ACTIVE_BRIDGE.update({"proc": None})
    return {
        "ok": not bool(final.get("ok")),
        "message": (
            "bridge stopped by killing the process listening on the bridge port"
            if not final.get("ok")
            else "bridge is still reachable after /shutdown and port-PID kill attempt"
        ),
        "before": before,
        "shutdown": shutdown,
        "killed": killed,
        "final": final,
    }


def start_or_restart_bridge(config: dict[str, str], args: argparse.Namespace, *, headless: bool = True) -> dict[str, Any]:
    if config["rp_type"] == "none":
        return {"ok": False, "error": "RP / PyRPL is set to none"}
    host_check = check_rp_host_resolution(config["rp_host"])
    if not host_check.get("ok"):
        return {"ok": False, "action": "start_restart_bridge", "host_check": host_check}
    pyrpl_host = preferred_ipv4_address(host_check, config["rp_host"])
    pyrpl_config = ensure_pyrpl_bridge_config(
        pyrpl_host,
        finite_config_float(os.environ.get("PYRPL_REDPITAYA_TIMEOUT_S"), 5.0) or 5.0,
    )

    status = bridge_runtime_status(config["bridge_base"])
    with ACTIVE_BRIDGE_LOCK:
        proc = ACTIVE_BRIDGE.get("proc")
        managed_running = bool(proc is not None and proc.poll() is None)
        managed_host = ACTIVE_BRIDGE.get("rp_host")

    if status.get("ok") and not managed_running:
        return {
            "ok": True,
            "action": "use_existing_bridge",
            "message": (
                "A bridge is already reachable on this URL. "
                "It may be a headless/background bridge, so no PyRPL window is expected. "
                "Using the existing bridge instead of starting another one."
            ),
            "headless": None,
            "status": status,
        }

    if not status.get("ok") and not managed_running:
        _listen_host, listen_port = bridge_listen_address(config["bridge_base"])
        stale_pids = listening_pids_for_tcp_port(listen_port)
        if stale_pids:
            killed = kill_pids(stale_pids)
            time.sleep(1.0)
            post_kill_status = bridge_runtime_status(config["bridge_base"])
            if post_kill_status.get("ok"):
                return {
                    "ok": True,
                    "action": "use_existing_bridge_after_stale_cleanup",
                    "message": "stale bridge listener was killed; a bridge became reachable afterward",
                    "stale_pids": stale_pids,
                    "killed": killed,
                    "status": post_kill_status,
                }
            remaining_pids = listening_pids_for_tcp_port(listen_port)
            if remaining_pids:
                return {
                    "ok": False,
                    "action": "start_restart_bridge",
                    "error": "bridge port is still occupied by a non-responsive process",
                    "stale_pids": stale_pids,
                    "killed": killed,
                    "remaining_pids": remaining_pids,
                    "status": post_kill_status,
                }

    if managed_running:
        stop = stop_managed_bridge(config["bridge_base"])
        if not stop.get("ok"):
            return {
                "ok": False,
                "action": "start_restart_bridge",
                "error": "failed to stop old managed bridge",
                "old_rp_host": managed_host,
                "stop": stop,
            }

    device_profile = getattr(args, "device_profile", {}) or {}
    configured_external_gain = device_profile.get("spectrum_external_gain_db")
    external_gain_db = (
        float(configured_external_gain)
        if configured_external_gain is not None
        else rp_host_default_external_gain_db(config["rp_host"])
    )
    command = bridge_command(config, args, external_gain_db, headless=headless, pyrpl_host=pyrpl_host)
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    logs_dir = bridge_log_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"pyrpl_bridge_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.log"
    with log_path.open("ab", buffering=0) as log:
        header = (
            f"\n=== pyrpl bridge start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n"
            f"cwd: {REPO_ROOT}\n"
            f"command: {' '.join(str(part) for part in command)}\n\n"
        )
        log.write(header.encode("utf-8", errors="replace"))
        proc = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    with ACTIVE_BRIDGE_LOCK:
        ACTIVE_BRIDGE.update(
            {
                "proc": proc,
                "command": command,
                "log_path": str(log_path),
                "started_at": time.time(),
                "rp_host": config["rp_host"],
                "pyrpl_host": pyrpl_host,
                "external_gain_db": external_gain_db,
                "device_profile": device_profile,
                "headless": headless,
                "python_executable": command[0],
            }
        )

    ready_timeout_s = bridge_ready_timeout_s()
    ready_deadline = time.time() + ready_timeout_s
    health = {"ok": False, "error": "not checked yet"}
    while time.time() < ready_deadline:
        time.sleep(0.5)
        if proc.poll() is not None:
            break
        health = bridge_runtime_status(config["bridge_base"])
        if health.get("ok"):
            break
    return {
        "ok": bool(health.get("ok")),
        "action": "start_restart_bridge",
        "message": "bridge started" if health.get("ok") else "bridge did not become ready",
        "rp_host": config["rp_host"],
        "pyrpl_host": pyrpl_host,
        "external_gain_db": external_gain_db,
        "device_profile": device_profile,
        "headless": headless,
        "python_executable": command[0],
        "log_path": str(log_path),
        "log_tail": read_text_tail(log_path),
        "ready_timeout_s": ready_timeout_s,
        "pyrpl_config": pyrpl_config,
        "host_check": host_check,
        "status": health,
    }


def run_bridge_action(body: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    config = instrument_config_from_mapping(body, args)
    action = str(body.get("bridge_action") or "refresh_bridge_status")
    if action == "check_rp_host":
        host_check = check_rp_host_resolution(config["rp_host"])
        device_profile = getattr(args, "device_profile", {})
        if device_profile.get("spectrum_external_gain_db") is not None:
            host_check["external_gain_db_if_started"] = device_profile["spectrum_external_gain_db"]
            host_check["external_gain_source"] = device_profile.get("spectrum_external_gain_source")
        return {
            "ok": bool(host_check.get("ok")),
            "action": action,
            "host_check": host_check,
            "device_profile": device_profile,
        }
    if action == "start_restart_bridge":
        return start_or_restart_bridge(config, args, headless=True)
    if action == "start_restart_bridge_gui":
        return start_or_restart_bridge(config, args, headless=False)
    if action == "stop_bridge":
        result = stop_managed_bridge(config["bridge_base"])
        return {"ok": bool(result.get("ok")), "action": action, **result}
    if action == "stop_any_bridge":
        result = stop_any_bridge_on_url(config["bridge_base"])
        return {"ok": bool(result.get("ok")), "action": action, **result}
    if action == "refresh_bridge_status":
        status = bridge_runtime_status(config["bridge_base"])
        return {
            "ok": bool(status.get("ok")),
            "action": action,
            "status": status,
            "device_profile": getattr(args, "device_profile", {}),
        }
    raise ValueError(f"Unknown bridge action: {action}")


def load_acquire_helpers() -> tuple[Any, Any]:
    import importlib.util

    path = LARGE_SCAN_DIR / "acquire_large_scan.py"
    spec = importlib.util.spec_from_file_location("dashboard_acquire_large_scan", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load acquire helpers from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    if str(LARGE_SCAN_DIR) not in sys.path:
        sys.path.insert(0, str(LARGE_SCAN_DIR))
    spec.loader.exec_module(module)
    return module.TopticaDlcPro, module.RohdeSchwarzRte


def read_large_scan_initial_wavelength(laser_port: str) -> dict[str, Any]:
    try:
        TopticaDlcPro, _RohdeSchwarzRte = load_acquire_helpers()
        laser = TopticaDlcPro(laser_port)
        try:
            return {"ok": True, "wavelength_nm": laser.wavelength_nm()}
        finally:
            laser.close()
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "wavelength_nm": None}


def terminate_process_tree(proc: subprocess.Popen) -> dict[str, Any]:
    if proc.poll() is not None:
        return {"ok": True, "message": "large-scan process already exited", "returncode": proc.returncode}
    try:
        taskkill = subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            text=True,
            capture_output=True,
            timeout=10.0,
            check=False,
        )
        return {
            "ok": taskkill.returncode == 0 or proc.poll() is not None,
            "pid": proc.pid,
            "taskkill_returncode": taskkill.returncode,
            "stdout_tail": taskkill.stdout[-1200:],
            "stderr_tail": taskkill.stderr[-1200:],
        }
    except Exception as exc:
        try:
            proc.terminate()
        except Exception:
            pass
        return {"ok": False, "pid": proc.pid, "error": repr(exc)}


def restore_large_scan_idle(
    base: str,
    restore_wavelength_nm: float | None,
    *,
    laser_port: str,
    scope_resource: str,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []

    steps.append({"name": "rp_safe_off", **safe_off(base)})

    try:
        TopticaDlcPro, RohdeSchwarzRte = load_acquire_helpers()
    except Exception as exc:
        steps.append({"name": "load_acquire_helpers", "ok": False, "error": repr(exc)})
        return {"ok": False, "steps": steps}

    try:
        laser = TopticaDlcPro(laser_port)
        try:
            try:
                laser.command("laser1:wide-scan:stop")
                steps.append({"name": "wide_scan_stop", "ok": True})
            except Exception as exc:
                steps.append({"name": "wide_scan_stop", "ok": False, "error": repr(exc)})

            if restore_wavelength_nm is not None:
                try:
                    laser.move_to_wavelength(float(restore_wavelength_nm), timeout_s=45.0)
                    steps.append(
                        {
                            "name": "restore_wavelength",
                            "ok": True,
                            "target_nm": restore_wavelength_nm,
                            "readback_nm": laser.wavelength_nm(),
                        }
                    )
                except Exception as exc:
                    steps.append(
                        {
                            "name": "restore_wavelength",
                            "ok": False,
                            "target_nm": restore_wavelength_nm,
                            "error": repr(exc),
                        }
                    )
            else:
                steps.append({"name": "restore_wavelength", "ok": False, "error": "initial wavelength unavailable"})

            try:
                laser.set_pc_voltage_v(75.0)
                steps.append({"name": "pc_voltage", "ok": True, "target_v": 75.0, "readback_v": laser.pc_voltage_act_v()})
            except Exception as exc:
                steps.append({"name": "pc_voltage", "ok": False, "target_v": 75.0, "error": repr(exc)})

            try:
                laser.configure_fine_scan_arc_factor(25.0)
                steps.append({"name": "fine_scan_arc", "ok": True, "factor_v_per_v": 25.0})
            except Exception as exc:
                steps.append({"name": "fine_scan_arc", "ok": False, "factor_v_per_v": 25.0, "error": repr(exc)})
        finally:
            laser.close()
    except Exception as exc:
        steps.append({"name": "toptica_restore", "ok": False, "error": repr(exc)})

    try:
        scope = RohdeSchwarzRte(scope_resource)
        try:
            scope.configure_fine_scan_idle(trigger_level_v=0.0, trigger_slope="negative", trigger_mode="auto")
            steps.append({"name": "scope_fine_scan_idle", "ok": True})
        finally:
            scope.close()
    except Exception as exc:
        steps.append({"name": "scope_fine_scan_idle", "ok": False, "error": repr(exc)})

    return {"ok": all(bool(step.get("ok")) for step in steps), "steps": steps}


def run_large_scan_script(
    command: list[str],
    timeout_s: float,
    bridge_base: str,
    *,
    laser_port: str,
    scope_resource: str,
) -> dict[str, Any]:
    started = time.time()
    with ACTIVE_LARGE_SCAN_LOCK:
        active = ACTIVE_LARGE_SCAN.get("proc")
        if active is not None and active.poll() is None:
            return {"ok": False, "error": "another large-scan process is already active"}
    initial = read_large_scan_initial_wavelength(laser_port)
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform.startswith("win") else 0
    proc = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    with ACTIVE_LARGE_SCAN_LOCK:
        ACTIVE_LARGE_SCAN.update(
            {
                "proc": proc,
                "command": command,
                "started_at": started,
                "initial_wavelength_nm": initial.get("wavelength_nm"),
                "initial_read": initial,
            }
        )
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            stop = terminate_process_tree(proc)
            stdout, stderr = proc.communicate(timeout=5.0)
            restore = restore_large_scan_idle(
                bridge_base,
                initial.get("wavelength_nm"),
                laser_port=laser_port,
                scope_resource=scope_resource,
            )
            return {
                "ok": False,
                "error": "large scan timed out; process was terminated and restore was attempted",
                "elapsed_s": time.time() - started,
                "command": command,
                "stop": stop,
                "restore": restore,
                "stdout_tail": stdout[-4000:],
                "stderr_tail": stderr[-4000:],
            }
        parsed = parse_script_json(stdout)
        return {
            "ok": proc.returncode == 0 and bool(parsed is None or parsed.get("ok", True)),
            "returncode": proc.returncode,
            "elapsed_s": time.time() - started,
            "command": command,
            "initial_read": initial,
            "json": parsed,
            "stdout_tail": stdout[-4000:],
            "stderr_tail": stderr[-4000:],
        }
    finally:
        with ACTIVE_LARGE_SCAN_LOCK:
            if ACTIVE_LARGE_SCAN.get("proc") is proc:
                ACTIVE_LARGE_SCAN.update({"proc": None})


def stop_large_scan_and_restore(base: str, *, laser_port: str, scope_resource: str) -> dict[str, Any]:
    with ACTIVE_LARGE_SCAN_LOCK:
        proc = ACTIVE_LARGE_SCAN.get("proc")
        restore_wavelength_nm = ACTIVE_LARGE_SCAN.get("initial_wavelength_nm")
        command = ACTIVE_LARGE_SCAN.get("command")
        started_at = ACTIVE_LARGE_SCAN.get("started_at")
    if proc is None or proc.poll() is not None:
        restore = restore_large_scan_idle(
            base,
            restore_wavelength_nm,
            laser_port=laser_port,
            scope_resource=scope_resource,
        )
        return {
            "ok": bool(restore.get("ok")),
            "json": {
                "message": "no active large-scan process; restore attempted",
                "restore": restore,
                "initial_wavelength_nm": restore_wavelength_nm,
            },
        }

    stop = terminate_process_tree(proc)
    time.sleep(0.5)
    restore = restore_large_scan_idle(
        base,
        restore_wavelength_nm,
        laser_port=laser_port,
        scope_resource=scope_resource,
    )
    with ACTIVE_LARGE_SCAN_LOCK:
        if ACTIVE_LARGE_SCAN.get("proc") is proc:
            ACTIVE_LARGE_SCAN.update({"proc": None})
    return {
        "ok": bool(stop.get("ok")) and bool(restore.get("ok")),
        "json": {
            "message": "large scan stopped; restore attempted",
            "command": command,
            "elapsed_s": time.time() - float(started_at or time.time()),
            "initial_wavelength_nm": restore_wavelength_nm,
            "stop": stop,
            "restore": restore,
        },
    }


def resolve_q_dir(cavity_dir: Path) -> Path:
    return cavity_dir / "Q" if (cavity_dir / "Q").exists() else cavity_dir


def load_cavity(cavity_dir_text: str) -> dict[str, Any]:
    cavity_dir = Path(cavity_dir_text).expanduser()
    q_dir = resolve_q_dir(cavity_dir)
    manifest_path = q_dir / "best_lock_candidate.json"
    q_table = q_dir / "q_by_mode.csv"
    manifest: dict[str, Any] | None = None
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    rows: list[dict[str, Any]] = []
    if q_table.exists():
        with q_table.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("fit_status") != "ok":
                    continue
                wavelength = finite_float(row.get("wavelength_nm"))
                q0 = finite_float(row.get("Q0"))
                if wavelength is None:
                    continue
                rows.append(
                    {
                        "family": row.get("family", ""),
                        "family_label": row.get("family_label", row.get("family", "")),
                        "mode_number": row.get("mode_number", ""),
                        "wavelength_nm": wavelength,
                        "Q0": q0,
                        "Q1": finite_float(row.get("Q1")),
                        "QL": finite_float(row.get("QL")),
                        "depth": finite_float(row.get("depth")),
                        "fit_status": row.get("fit_status", ""),
                        "coupling_note": row.get("coupling_note", ""),
                    }
                )
        rows.sort(key=lambda item: float(item["wavelength_nm"]))

    return {
        "ok": True,
        "cavity_dir": str(cavity_dir),
        "q_dir": str(q_dir),
        "manifest_path": str(manifest_path) if manifest_path.exists() else None,
        "q_table": str(q_table) if q_table.exists() else None,
        "manifest": manifest,
        "q_rows": rows,
    }


def infer_cavity_context(cavity_dir_text: str) -> dict[str, Any]:
    if not str(cavity_dir_text).strip():
        raise ValueError("Select a cavity directory first")
    cavity_dir = Path(cavity_dir_text).expanduser().resolve()
    cavity = cavity_dir.name
    die_dir = cavity_dir.parent
    die = die_dir.name
    results_root = die_dir.parent
    chip = results_root.name

    campaign = "wafer_measuement/Batch_260515"
    parts = list(cavity_dir.parts)
    lowered = [part.lower() for part in parts]
    if "experiments" in lowered and "results" in lowered:
        exp_idx = lowered.index("experiments")
        res_idx = lowered.index("results")
        if exp_idx < res_idx:
            campaign_parts = parts[exp_idx + 1 : res_idx]
            if campaign_parts:
                campaign = "/".join(campaign_parts)
        if res_idx + 1 < len(parts):
            chip = parts[res_idx + 1]
            results_root = Path(*parts[: res_idx + 2])

    return {
        "cavity_dir": cavity_dir,
        "die_dir": die_dir,
        "results_root": results_root,
        "campaign": campaign,
        "chip": chip,
        "die": die,
        "cavity": cavity,
    }


def compute_power_fields(
    input_monitor_power_uw: float | None,
    output_power_uw: float | None,
    *,
    input_monitor_fraction: float = 0.01,
) -> dict[str, Any]:
    result = {
        "input_monitor_power_uw": input_monitor_power_uw,
        "input_monitor_fraction": input_monitor_fraction,
        "input_power_uw": None,
        "output_power_uw": output_power_uw,
        "throughput": None,
        "throughput_pct": None,
        "single_ended_loss_db": None,
    }
    if input_monitor_power_uw is None or output_power_uw is None:
        return result
    if input_monitor_power_uw <= 0 or input_monitor_fraction <= 0 or output_power_uw < 0:
        return result
    input_power_uw = input_monitor_power_uw / input_monitor_fraction
    throughput = output_power_uw / input_power_uw
    result.update(
        {
            "input_power_uw": input_power_uw,
            "throughput": throughput,
            "throughput_pct": throughput * 100.0,
        }
    )
    if throughput > 0:
        result["single_ended_loss_db"] = -10.0 * math.log10(math.sqrt(throughput))
    return result


def finite_from_text(text: str | None) -> float | None:
    if text is None or text == "":
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def power_from_card(cavity_dir: Path) -> dict[str, Any] | None:
    card_path = cavity_dir / "cavity_card.html"
    if not card_path.exists():
        return None
    text = card_path.read_text(encoding="utf-8")
    pout_match = re.search(r"Pout\s+([0-9.eE+-]+)\s+uW", text)
    input_match = re.search(r"\(([0-9.eE+-]+)\s+uW input\)", text)
    input_monitor_fraction = 0.01
    if pout_match:
        output_power_uw = float(pout_match.group(1))
        input_power_uw = float(input_match.group(1)) if input_match else 100.0
    else:
        legacy_match = re.search(r"<tr><td>throughput</td><td>\s*([0-9.eE+-]+)\s*%\s*/\s*([0-9.eE+-]+)\s*dB", text)
        if not legacy_match:
            return None
        throughput_pct = float(legacy_match.group(1))
        input_power_uw = 100.0
        output_power_uw = input_power_uw * throughput_pct / 100.0
    input_monitor_power_uw = input_power_uw * input_monitor_fraction
    power = compute_power_fields(input_monitor_power_uw, output_power_uw, input_monitor_fraction=input_monitor_fraction)
    power["source"] = str(card_path)
    return power


def read_power_log_row(context: dict[str, Any]) -> dict[str, Any] | None:
    log_path = context["die_dir"] / "output_power_log.csv"
    if not log_path.exists():
        return None
    with log_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("cavity") != context["cavity"]:
                continue
            input_monitor_fraction = finite_from_text(row.get("input_monitor_fraction")) or 0.01
            input_monitor_power_uw = finite_from_text(row.get("input_monitor_power_uw"))
            output_power_uw = finite_from_text(row.get("output_power_uw"))
            if input_monitor_power_uw is None:
                input_monitor_power_uw = 1.0
            if output_power_uw is None:
                throughput_pct = finite_from_text(row.get("throughput_percent"))
                if throughput_pct is not None:
                    output_power_uw = (input_monitor_power_uw / input_monitor_fraction) * throughput_pct / 100.0
            power = compute_power_fields(
                input_monitor_power_uw,
                output_power_uw,
                input_monitor_fraction=input_monitor_fraction,
            )
            legacy_loss = finite_from_text(row.get("single_ended_insertion_loss_db"))
            if power["single_ended_loss_db"] is None and legacy_loss is not None:
                power["single_ended_loss_db"] = legacy_loss
            power["source"] = str(log_path)
            power["updated_at"] = row.get("updated_at")
            power["note"] = row.get("note")
            return power
    return None


def large_scan_status(cavity_dir_text: str) -> dict[str, Any]:
    context = infer_cavity_context(cavity_dir_text)
    cavity_dir = context["cavity_dir"]
    q_dir = cavity_dir / "Q"
    power = read_power_log_row(context) or power_from_card(cavity_dir)
    q_table = q_dir / "q_by_mode.csv"
    q_rows = 0
    if q_table.exists():
        with q_table.open(newline="", encoding="utf-8") as handle:
            q_rows = sum(1 for row in csv.DictReader(handle) if row.get("fit_status") == "ok")
    return {
        "ok": True,
        "context": {
            "campaign": context["campaign"],
            "chip": context["chip"],
            "die": context["die"],
            "cavity": context["cavity"],
            "results_root": str(context["results_root"]),
            "cavity_dir": str(cavity_dir),
        },
        "power": power,
        "q": {
            "q_dir": str(q_dir),
            "raw_npz": str(q_dir / "raw.npz") if (q_dir / "raw.npz").exists() else None,
            "q_table": str(q_table) if q_table.exists() else None,
            "q_rows": q_rows,
            "interactive_q": str(q_dir / "interactive_q.html") if (q_dir / "interactive_q.html").exists() else None,
            "q_trend": str(q_dir / "q_trend.png") if (q_dir / "q_trend.png").exists() else None,
        },
    }


def write_power_log(context: dict[str, Any], power: dict[str, Any], note: str = "") -> Path:
    log_path = context["die_dir"] / "output_power_log.csv"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "cavity",
        "input_monitor_power_uw",
        "input_monitor_fraction",
        "input_power_uw",
        "output_power_uw",
        "throughput",
        "throughput_pct",
        "single_ended_loss_db",
        "updated_at",
        "note",
    ]
    rows: list[dict[str, Any]] = []
    if log_path.exists():
        with log_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    rows = [row for row in rows if row.get("cavity") != context["cavity"]]
    rows.append(
        {
            "cavity": context["cavity"],
            "input_monitor_power_uw": power["input_monitor_power_uw"],
            "input_monitor_fraction": power["input_monitor_fraction"],
            "input_power_uw": power["input_power_uw"],
            "output_power_uw": power["output_power_uw"],
            "throughput": power["throughput"],
            "throughput_pct": power["throughput_pct"],
            "single_ended_loss_db": power["single_ended_loss_db"],
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": note,
        }
    )
    rows.sort(key=lambda row: row.get("cavity", ""))
    with log_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return log_path


def update_power_and_card(body: dict[str, Any]) -> dict[str, Any]:
    context = infer_cavity_context(str(body.get("cavity_dir") or ""))
    input_monitor_power_uw = finite_from_text(str(body.get("input_monitor_power_uw", "")))
    output_power_uw = finite_from_text(str(body.get("output_power_uw", "")))
    input_monitor_fraction = finite_from_text(str(body.get("input_monitor_fraction", "0.01"))) or 0.01
    if input_monitor_power_uw is None or output_power_uw is None:
        raise ValueError("input_monitor_power_uw and output_power_uw are required")
    power = compute_power_fields(
        input_monitor_power_uw,
        output_power_uw,
        input_monitor_fraction=input_monitor_fraction,
    )
    if input_monitor_power_uw <= 0 or output_power_uw < 0:
        raise ValueError("power values must be finite and non-negative; input monitor must be positive")
    log_path = write_power_log(context, power, note=str(body.get("note") or ""))
    command = [
        package_python_executable(),
        str(DEFAULT_CARD_SCRIPT),
        "--chip",
        context["chip"],
        "--die",
        context["die"],
        "--cavity",
        context["cavity"],
        "--results-root",
        str(context["results_root"]),
        "--output-power-uw",
        f"{output_power_uw:g}",
        "--input-monitor-power-uw",
        f"{input_monitor_power_uw:g}",
        "--input-monitor-fraction",
        f"{input_monitor_fraction:g}",
    ]
    card_result = run_script(command, timeout_s=60.0)
    return {
        "ok": bool(card_result.get("ok")),
        "json": {
            "message": "power saved and cavity card refreshed" if card_result.get("ok") else "card refresh failed",
            "power": {**power, "source": str(log_path)},
            "context": {
                "chip": context["chip"],
                "die": context["die"],
                "cavity": context["cavity"],
                "results_root": str(context["results_root"]),
            },
            "card_result": card_result,
        },
    }


def large_scan_command(body: dict[str, Any], args: argparse.Namespace) -> tuple[list[str], float]:
    context = infer_cavity_context(str(body.get("cavity_dir") or ""))
    config = instrument_config_from_mapping(body, args)
    if config["laser_type"] != "toptica_serial":
        raise ValueError("Large-scan Q currently requires TOPTICA serial connection; choose TOPTICA Serial.")
    if config["scope_type"] != "rs_rte":
        raise ValueError("Large-scan Q requires an R&S RTE oscilloscope resource.")
    mode = str(body.get("mode") or "run")
    command = [
        package_python_executable(),
        str(DEFAULT_LARGE_SCAN_SCRIPT),
        "--campaign",
        context["campaign"],
        "--chip",
        context["chip"],
        "--die",
        context["die"],
        "--cavity",
        context["cavity"],
        "--results-root",
        str(context["results_root"]),
        "--laser-port",
        config["laser_port"],
        "--scope-resource",
        config["scope_resource"],
    ]
    timeout_s = 900.0
    if mode == "resume":
        command.append("--resume-existing-raw")
        timeout_s = 600.0
    elif mode == "standardize":
        command.append("--standardize-only")
        timeout_s = 240.0
    elif mode != "run":
        raise ValueError(f"Unknown large-scan mode: {mode}")
    return command, timeout_s


def pick_folder(initial_dir_text: str | None, fallback_dir: Path) -> dict[str, Any]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        return {"ok": False, "error": f"Tk folder picker unavailable: {exc!r}"}

    initial_dir = Path(initial_dir_text).expanduser() if initial_dir_text else fallback_dir
    if initial_dir.is_file():
        initial_dir = initial_dir.parent
    if not initial_dir.exists():
        initial_dir = fallback_dir if fallback_dir.exists() else Path.cwd()

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            title="Select cavity directory",
            initialdir=str(initial_dir),
            mustexist=True,
        )
    finally:
        root.destroy()

    if not selected:
        return {"ok": True, "cancelled": True, "path": None}
    return {"ok": True, "cancelled": False, "path": selected}


def target_args(body: dict[str, Any], args: argparse.Namespace) -> list[str]:
    cavity_dir = str(body.get("cavity_dir") or "")
    if not cavity_dir:
        raise ValueError("Select a cavity directory first")
    target_kind = str(body.get("target_kind") or "candidate")
    config = instrument_config_from_mapping(body, args)
    if config["rp_type"] == "none":
        raise ValueError("Selected-mode lock/move requires RP / PyRPL bridge")
    if config["laser_type"] not in {"toptica_tcp", "toptica_serial"}:
        raise ValueError("Selected-mode lock/move currently requires TOPTICA laser")
    laser_connection = "serial" if config["laser_type"] == "toptica_serial" else "tcp"
    command = [
        str(args.toptica_python),
        str(DEFAULT_BEST_Q_SCRIPT),
        "--cavity-dir",
        cavity_dir,
        "--base",
        config["bridge_base"],
        "--host",
        config["toptica_host"],
        "--laser-connection",
        laser_connection,
        "--laser-port",
        config["laser_port"],
    ]
    if target_kind == "nearest_1550":
        command += ["--candidate-key", "nearest_1550_best_q_candidate"]
    elif target_kind == "manual":
        wavelength = body.get("wavelength_nm")
        if wavelength in (None, ""):
            raise ValueError("Manual target requires wavelength_nm")
        command += ["--wavelength-nm", str(wavelength)]
    elif target_kind == "candidate":
        command += ["--candidate-key", "candidate"]
    else:
        raise ValueError(f"Unknown target_kind: {target_kind}")
    return command


def page(
    default_cavity: Path | None,
    bridge_base: str,
    rp_host: str,
    laser_type: str,
    toptica_host: str,
    *,
    large_scan_laser_port: str,
    scope_type: str,
    scope_resource: str,
) -> str:
    default_cavity_text = str(default_cavity) if default_cavity is not None else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Microcavity Q/Lock Control</title>
  <style>
    :root {{ font-family: Arial, "Microsoft YaHei", sans-serif; color: #111; background: #f6f7f8; }}
    body {{ margin: 0; }}
    header {{ padding: 14px 18px; border-bottom: 1px solid #bbb; background: #fff; display: flex; justify-content: space-between; gap: 16px; align-items: center; }}
    h1 {{ margin: 0; font-size: 20px; }}
    .task-nav {{ position: sticky; top: 0; z-index: 10; display: flex; gap: 8px; align-items: center; padding: 10px 16px; background: #fff; border-bottom: 1px solid #d0d3d6; box-shadow: 0 1px 4px rgba(0,0,0,.04); }}
    .task-nav button {{ margin: 0; border-color: #aaa; background: #fff; font-weight: 700; }}
    .task-nav button.active {{ background: #0b65c2; color: #fff; border-color: #0b65c2; }}
    .task-nav .task-hint {{ margin-left: auto; font-size: 12px; color: #555; }}
    main {{
      padding: 16px;
      display: grid;
      grid-template-columns: minmax(520px, 1fr) minmax(520px, 1fr);
      grid-template-areas:
        "safety selected"
        "large selected"
        "qtable selected";
      gap: 14px;
      align-items: start;
    }}
    section {{ background: #fff; border: 1px solid #bbb; border-radius: 8px; padding: 14px; }}
    .safety-panel {{ grid-area: safety; }}
    .selected-panel {{ grid-area: selected; }}
    .large-scan-panel {{ grid-area: large; }}
    .q-table-panel {{ grid-area: qtable; }}
    h2 {{ margin: 0 0 10px; font-size: 17px; }}
    label {{ display: block; font-size: 13px; margin: 10px 0 4px; font-weight: 700; }}
    input, select {{ width: 100%; box-sizing: border-box; padding: 7px 8px; border: 1px solid #888; border-radius: 4px; font-size: 13px; }}
    button {{ padding: 7px 10px; margin: 6px 6px 0 0; border: 1px solid #555; border-radius: 4px; background: #f3f3f3; cursor: pointer; }}
    button.primary {{ background: #0b65c2; color: white; border-color: #0b65c2; }}
    button.danger {{ background: #fff1f1; border-color: #b00020; color: #8c001a; }}
    button:disabled {{ opacity: 0.55; cursor: wait; }}
    .row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
    .instrument-grid {{ display: grid; grid-template-columns: 130px minmax(150px, .8fr) minmax(180px, 1fr); gap: 8px; align-items: end; margin-bottom: 8px; }}
    .instrument-grid label {{ margin-top: 0; }}
    .instrument-grid .wide {{ grid-column: span 2; }}
    .mode-grid {{ display: grid; grid-template-columns: minmax(180px, .45fr) 1fr; gap: 10px; align-items: end; margin-bottom: 10px; }}
    .mode-note {{ border-left: 3px solid #0b65c2; padding: 7px 9px; background: #f4f8ff; font-size: 12px; line-height: 1.45; }}
    .path-row {{ display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: center; }}
    .path-row button {{ margin-top: 0; white-space: nowrap; }}
    .power-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }}
    .power-grid input[readonly] {{ background: #f6f7f8; }}
    .button-row {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 8px; }}
    .button-row button {{ margin: 0; }}
    .status {{ font-family: Consolas, monospace; font-size: 12px; white-space: normal; background: #f1f3f5; border: 1px solid #ccc; padding: 8px; min-height: 58px; }}
    .status .result-summary {{ white-space: pre-wrap; line-height: 1.45; }}
    .status .result-ok {{ color: #075b18; }}
    .status .result-bad {{ color: #8c001a; }}
    .status details {{ margin-top: 8px; color: #333; }}
    .status details pre {{ white-space: pre-wrap; overflow: auto; max-height: 260px; margin: 6px 0 0; }}
    .candidate {{ display: grid; grid-template-columns: 120px 1fr; gap: 4px 10px; font-size: 13px; border-top: 1px solid #ddd; padding-top: 8px; margin-top: 8px; }}
    .candidate b {{ font-family: Consolas, monospace; }}
    .preview-stack {{ display: grid; gap: 10px; margin-top: 10px; }}
    .rp-preview {{ border: 1px solid #ccc; background: #f8f9fa; padding: 8px; }}
    .selected-panel > .rp-preview {{ margin-top: 10px; }}
    .preview-head {{ display: flex; justify-content: space-between; gap: 10px; align-items: center; margin-bottom: 6px; flex-wrap: wrap; }}
    .preview-actions {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
    .preview-actions button {{ margin: 0; }}
    .live-label {{ display: inline-flex; gap: 4px; align-items: center; margin: 0; font-weight: 400; }}
    .live-label input {{ width: auto; }}
    .rp-preview canvas {{ display: block; width: 100%; height: 240px; background: #fff; border: 1px solid #aaa; }}
    .modal-backdrop {{ position: fixed; inset: 0; background: rgba(0, 0, 0, .28); display: flex; align-items: center; justify-content: center; z-index: 50; padding: 20px; }}
    .modal-card {{ width: min(1180px, 96vw); max-height: 92vh; overflow: auto; background: #fff; border: 1px solid #111; border-radius: 8px; box-shadow: 0 12px 36px rgba(0,0,0,.28); padding: 16px; }}
    .modal-card h2 {{ font-size: 19px; margin-bottom: 4px; }}
    .settings-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(270px, 1fr)); gap: 10px 14px; margin: 10px 0; }}
    .settings-group {{ border: 1px solid #bbb; border-radius: 6px; padding: 10px; }}
    .settings-group h3 {{ margin: 0 0 6px; font-size: 15px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th, td {{ border-bottom: 1px solid #ddd; padding: 5px 6px; text-align: left; }}
    th {{ background: #f1f3f5; position: sticky; top: 0; }}
    tr:hover {{ background: #fff7db; }}
    .table-wrap {{ max-height: 380px; overflow: auto; border: 1px solid #ddd; }}
    .muted {{ color: #555; font-size: 12px; }}
    .hidden {{ display: none !important; }}
    body[data-task="lock"] main,
    body[data-task="sensitivity"] main,
    body[data-task="qscan"] main,
    body[data-task="coupling"] main,
    body[data-task="qtable"] main,
    body[data-task="settings"] main {{ grid-template-columns: minmax(680px, 1280px); grid-template-areas: "active"; justify-content: center; }}
    body[data-task="lock"] main {{
      grid-template-areas:
        "active"
        "qtable";
    }}
    body[data-task="lock"] .safety-panel,
    body[data-task="lock"] .large-scan-panel,
    body[data-task="sensitivity"] .safety-panel,
    body[data-task="sensitivity"] .large-scan-panel,
    body[data-task="sensitivity"] .q-table-panel,
    body[data-task="qscan"] .safety-panel,
    body[data-task="qscan"] .selected-panel,
    body[data-task="qscan"] .q-table-panel,
    body[data-task="coupling"] .safety-panel,
    body[data-task="coupling"] .selected-panel,
    body[data-task="coupling"] .q-table-panel,
    body[data-task="qtable"] .safety-panel,
    body[data-task="qtable"] .selected-panel,
    body[data-task="qtable"] .large-scan-panel,
    body[data-task="settings"] .selected-panel,
    body[data-task="settings"] .large-scan-panel,
    body[data-task="settings"] .q-table-panel {{ display: none; }}
    body[data-task="lock"] .selected-panel,
    body[data-task="sensitivity"] .selected-panel,
    body[data-task="qscan"] .large-scan-panel,
    body[data-task="coupling"] .large-scan-panel,
    body[data-task="qtable"] .q-table-panel,
    body[data-task="settings"] .safety-panel {{ grid-area: active; }}
    body[data-task="lock"] .q-table-panel {{ grid-area: qtable; }}
    body[data-task="lock"] .sensitivity-card,
    body[data-task="sensitivity"] .target-controls,
    body[data-task="sensitivity"] .lock-actions,
    body[data-task="sensitivity"] #targetResult,
    body[data-task="sensitivity"] .scope-card,
    body[data-task="coupling"] .large-scan-controls,
    body[data-task="qscan"] .scope-card,
    body[data-task="coupling"] .cavity-controls {{ display: none !important; }}
    body[data-task="coupling"] .coupling-card {{ display: block; }}
    @media (max-width: 980px) {{
      main {{
        grid-template-columns: 1fr;
        grid-template-areas:
          "safety"
          "large"
          "selected"
          "qtable";
      }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Microcavity Q/Lock Control</h1>
    <div class="muted">
      RP bridge: <span id="bridgeBase">{bridge_base}</span> |
      mode: <span id="headerMode">-</span> |
      laser: <span id="headerLaser">{laser_type}</span> |
      large scan: <span id="laserPort">{large_scan_laser_port}</span> + <span id="scopeResource">{scope_resource}</span>
    </div>
  </header>
  <nav class="task-nav" aria-label="workflow tasks">
    <button type="button" data-task-button="settings" onclick="setTask('settings')">仪器设置</button>
    <button type="button" data-task-button="qscan" onclick="setTask('qscan')">Q 大扫 / 插损</button>
    <button type="button" data-task-button="lock" onclick="setTask('lock')">锁模</button>
    <button type="button" data-task-button="sensitivity" onclick="setTask('sensitivity')">灵敏度</button>
    <button type="button" data-task-button="coupling" onclick="setTask('coupling')">自动耦合</button>
    <button type="button" data-task-button="qtable" onclick="setTask('qtable')">Q 表</button>
    <span id="taskHint" class="task-hint"></span>
  </nav>
  <main>
    <section class="safety-panel">
      <h2>Experiment Mode & Safety</h2>
      <div class="mode-grid">
        <div>
          <label>Experiment mode</label>
          <select id="experimentMode">
            <option value="toptica_q_lock">TOPTICA Q / Lock</option>
            <option value="weiyuan_lock">微源光子 Lock</option>
            <option value="rp_debug">RP spectrum / debug</option>
          </select>
        </div>
        <div id="modeNote" class="mode-note"></div>
      </div>
      <div class="instrument-grid rp-connection">
        <div>
          <label>RP / PyRPL</label>
          <select id="rpType">
            <option value="pyrpl_bridge">PyRPL bridge</option>
            <option value="none">none</option>
          </select>
        </div>
        <div class="wide">
          <label>Bridge URL</label>
          <input id="bridgeBaseInput" value="{bridge_base}" />
        </div>
        <div></div>
        <div class="wide">
          <label>RP host / IP (optional)</label>
          <input id="rpHostInput" value="{rp_host}" placeholder="RP-f0cb0d" />
        </div>
        <div>
          <label>RP bridge action</label>
          <select id="bridgeAction">
            <option value="check_rp_host">Check RP host</option>
            <option value="start_restart_bridge">Start / restart headless bridge</option>
            <option value="start_restart_bridge_gui">Start / restart GUI bridge</option>
            <option value="refresh_bridge_status">Refresh bridge status</option>
            <option value="stop_bridge">Stop managed bridge</option>
            <option value="stop_any_bridge">Stop any bridge on this URL</option>
          </select>
        </div>
        <div class="wide">
          <button onclick="runBridgeAction()">Run bridge action</button>
          <span class="muted">Default bridge is headless; GUI bridge is only for PyRPL-native debugging. Spectrum gain and scope power response come from config.local.json.</span>
        </div>
      </div>
      <div class="instrument-grid laser-connection">
        <div>
          <label>Laser</label>
          <select id="laserType">
            <option value="toptica_serial">TOPTICA Serial</option>
            <option value="toptica_tcp">TOPTICA TCP/IP</option>
            <option value="weiyuan">微源光子</option>
            <option value="none">none</option>
          </select>
        </div>
        <div id="topticaHostField">
          <label>Laser host / IP</label>
          <input id="topticaHostInput" value="{toptica_host}" />
        </div>
        <div>
          <label id="laserPortLabel">Laser COM / port</label>
          <input id="laserPortInput" value="{large_scan_laser_port}" />
        </div>
      </div>
      <div id="weiyuanPanel">
        <h3 style="margin: 10px 0 4px; font-size: 15px;">微源光子控制</h3>
        <div class="row">
          <div>
            <label>Slave address</label>
            <input id="weiyuanSlaveInput" type="number" step="1" value="255" />
          </div>
          <div></div>
        </div>
        <div class="row">
          <div>
            <label>TEC set temperature (C)</label>
            <input id="weiyuanTempInput" type="number" step="0.01" placeholder="temperature" />
          </div>
          <div>
            <label>LD set current (mA)</label>
            <input id="weiyuanCurrentInput" type="number" step="0.01" value="260" />
          </div>
        </div>
        <div class="button-row">
          <button onclick="runWeiyuan('status')">Read micro-source</button>
          <button onclick="runWeiyuan('set_temperature')">Set temperature</button>
          <button onclick="runWeiyuan('set_current')">Set current</button>
          <button onclick="runWeiyuan('set_initial_current')">Set 260 mA</button>
          <button onclick="runWeiyuan('tec_on')">TEC on</button>
          <button onclick="runWeiyuan('tec_off')">TEC off</button>
          <button onclick="runWeiyuan('ld_on')">LD on</button>
          <button onclick="runWeiyuan('ld_off')">LD off</button>
        </div>
        <pre id="weiyuanResult" class="status">choose 微源光子 and read status</pre>
      </div>
      <div class="instrument-grid scope-connection">
        <div>
          <label>Oscilloscope</label>
          <select id="scopeType">
            <option value="rs_rte">R&S RTE</option>
            <option value="none">none</option>
          </select>
        </div>
        <div class="wide">
          <label>VISA resource</label>
          <input id="scopeResourceInput" value="{scope_resource}" />
        </div>
      </div>
      <button onclick="refreshHealth()">Refresh status</button>
      <button class="danger" onclick="safeOff()">Safe off PID/ASG</button>
      <pre id="health" class="status">not checked</pre>
    </section>

    <section class="selected-panel">
      <h2>Selected Mode</h2>
      <div class="target-controls">
        <label>Target source</label>
        <select id="targetKind" onchange="renderTarget()">
          <option value="candidate">highest Q0 candidate</option>
          <option value="nearest_1550">nearest 1550 nm candidate</option>
          <option value="manual">manual / selected row wavelength</option>
        </select>
        <label>Manual wavelength (nm)</label>
        <input id="manualWavelength" placeholder="1564.492182" oninput="manualSelectedRow=null; renderTarget()" />
        <div id="targetPreview" class="candidate"></div>
      </div>
      <div class="button-row lock-actions">
        <button class="target-controls" onclick="dryRunTarget()">Dry-run target</button>
        <button class="primary target-controls" onclick="moveTarget()">Move to target wavelength</button>
        <button onclick="lockCurrent()">Lock current mode</button>
        <button onclick="restoreLockSweep('targetResult')">Restore sweep / PID off</button>
      </div>
      <div id="targetResult" class="status"></div>
      <div class="rp-preview sensitivity-card">
        <div class="preview-head">
          <b>Sensitivity acquisition</b>
          <span class="muted">noise spectrum + network response</span>
        </div>
        <div class="row">
          <div>
            <label>Tag prefix</label>
            <input id="sensitivityTag" value="sensitivity" />
          </div>
          <div>
            <label>Acquisition mode</label>
            <select id="sensitivityMode">
              <option value="psd">PSD only</option>
              <option value="network">Network response only</option>
              <option value="both" selected>PSD then network response</option>
              <option value="compute">Compute sensitivity from existing raw</option>
            </select>
          </div>
        </div>
        <div class="row">
          <div>
            <label>Max inline points</label>
            <input id="sensitivityMaxPoints" type="number" value="1500" min="100" step="100" />
          </div>
          <div>
            <label>Spectrum timeout (s)</label>
            <input id="sensitivitySpectrumTimeout" type="number" value="30" min="1" step="1" />
          </div>
        </div>
        <div class="row">
          <div>
            <label>Network timeout (s)</label>
            <input id="sensitivityNetworkTimeout" type="number" value="600" min="1" step="1" />
          </div>
        </div>
        <div class="button-row">
          <button id="runSensitivityButton" class="primary" onclick="runSensitivityAcquisition()">Run selected acquisition</button>
          <button id="cancelSensitivityButton" class="danger hidden" onclick="cancelSensitivityAcquisition()">Cancel acquisition</button>
        </div>
        <div id="sensitivityResult" class="status"></div>
      </div>
    </section>

    <section class="large-scan-panel">
      <h2>Large-Scan Q</h2>
      <div class="cavity-controls">
        <label>Cavity directory</label>
        <div class="path-row">
          <input id="cavityDir" value="{default_cavity_text}" placeholder="Choose a cavity directory when needed" />
          <button onclick="browseCavity()">Browse...</button>
        </div>
        <button onclick="loadCavity()">Load cavity</button>
      </div>
      <div class="rp-preview coupling-controls coupling-card">
        <div class="preview-head">
          <b>Auto coupling</b>
          <span class="muted">monotonic step search with instantaneous RP power</span>
        </div>
        <p class="muted">
          Pass 1: all axes use 20 V range with 1/0.3/0.1 V steps.
          Pass 2: all axes use 10 V range with 0.5/0.3/0.1 V steps.
        </p>
        <div class="button-row">
          <button id="runCouplingButton" class="primary" onclick="runStandardCoupling()">Run standard auto coupling</button>
        </div>
        <div id="couplingResult" class="status"></div>
      </div>
      <div class="large-scan-controls">
      <p class="muted">Fixed large scan: 1530-1570 nm, 2 nm/s. Power fields are used for card throughput / single-ended insertion loss.</p>
      <div class="power-grid">
        <div>
          <label>1% monitor power (uW)</label>
          <input id="inputMonitorPower" placeholder="1.0" oninput="updateLossPreview()" />
        </div>
        <div>
          <label>Out-coupled power (uW)</label>
          <input id="outputPower" placeholder="5.0" oninput="updateLossPreview()" />
        </div>
        <div>
          <label>Single-ended loss (dB)</label>
          <input id="singleEndedLoss" readonly placeholder="pending" />
        </div>
      </div>
      <div class="muted" id="powerSource">power record not loaded</div>
      <div class="button-row">
        <button onclick="savePower()">Save power + refresh card</button>
        <button class="primary" onclick="runLargeScan('run')">Run large scan Q</button>
        <button onclick="runLargeScan('resume')">Resume existing raw</button>
        <button onclick="runLargeScan('standardize')">Standardize only</button>
        <button class="danger" onclick="stopLargeScan()">Stop scan + restore idle</button>
      </div>
      <div id="largeScanResult" class="status"></div>
      </div>
    </section>

    <section class="q-table-panel">
      <h2>Q Table</h2>
      <p class="muted">Click a row to copy its wavelength into the manual target field.</p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>family</th><th>mu</th><th>wavelength</th><th>Q0(M)</th><th>Q1(M)</th><th>QL(M)</th><th>depth</th></tr></thead>
          <tbody id="qRows"></tbody>
        </table>
      </div>
    </section>
  </main>
  <div id="sensitivitySettingsModal" class="modal-backdrop hidden">
    <div class="modal-card">
      <h2>Sensitivity acquisition settings</h2>
      <div id="sensitivitySettingsNote" class="muted">Review PyRPL settings before acquisition.</div>
      <div class="settings-grid">
        <div id="spectrumSettingsGroup" class="settings-group">
          <h3>Spectrum analyzer</h3>
          <label>Frequency span (Hz)</label>
          <select id="spectrumSpanHz" onchange="syncSpectrumRbwFromSpan()"></select>
          <label>RBW (Hz, derived)</label>
          <select id="spectrumRbwHz" disabled></select>
          <label>Average count</label>
          <input id="spectrumTraceAverage" type="number" min="1" step="1" />
          <div id="spectrumPointInfo" class="muted">Spectrum points are fixed by the PyRPL scope FFT path.</div>
        </div>
        <div id="networkSettingsGroup" class="settings-group">
          <h3>Network analyzer</h3>
          <label>Drive amplitude (Vpk)</label>
          <input id="networkAmplitudeVpk" type="number" step="0.001" />
          <label>Start frequency (Hz)</label>
          <input id="networkStartHz" type="number" step="1" />
          <label>Stop frequency (Hz)</label>
          <input id="networkStopHz" type="number" step="1" />
          <label>Points</label>
          <input id="networkPoints" type="number" step="1" min="2" />
          <label>RBW (Hz)</label>
          <select id="networkRbwHz"></select>
        </div>
        <div id="pressureSettingsGroup" class="settings-group">
          <h3>Pressure calibration</h3>
          <label>Pressure source</label>
          <select id="pressureSourceModel"></select>
          <label>Calibration file</label>
          <input id="pressureCalibrationPath" />
          <label>Calibration range</label>
          <input id="pressureCalibrationRange" readonly />
          <label>Calibration drive (Vpp)</label>
          <input id="pressureCalibrationDriveVpp" type="number" step="0.001" />
          <label>Pressure quantity</label>
          <select id="pressureQuantity">
            <option value="pk">peak pressure</option>
            <option value="rms">RMS pressure</option>
          </select>
          <label>Use start frequency (Hz)</label>
          <input id="pressureUseStartHz" type="number" step="1" />
          <label>Use stop frequency (Hz)</label>
          <input id="pressureUseStopHz" type="number" step="1" />
          <div id="pressureInfo" class="muted">Pressure calibration is used only when PSD and network response are both available.</div>
        </div>
      </div>
      <div class="button-row">
        <button class="primary" onclick="confirmSensitivityAcquisition()">Confirm and run</button>
        <button onclick="closeSensitivitySettings()">Cancel</button>
      </div>
    </div>
  </div>
<script>
let cavity = null;
let manualSelectedRow = null;
let pendingSensitivityBody = null;
let sensitivityAbortController = null;
const defaults = {{
  rpType: 'pyrpl_bridge',
  laserType: '{laser_type}',
  scopeType: '{scope_type}'
}};
const modeLabels = {{
  toptica_q_lock: 'TOPTICA Q / Lock',
  weiyuan_lock: '微源光子 Lock',
  rp_debug: 'RP spectrum / debug'
}};
const modeNotes = {{
  toptica_q_lock: '用于片上微腔大扫、Q 拟合、从 Q 表选模式并用 TOPTICA 锁模。',
  weiyuan_lock: '用于外场/微源光子实验：通过串口读写温度和 LD 电流，并对当前模式做电流居中锁定。',
  rp_debug: '只保留 Red Pitaya / PyRPL 桥、安全关闭和状态检查，用于频谱仪、scope 或临时调试。'
}};
const taskHints = {{
  lock: '手动选腔、移动到目标模式、锁定当前模式。',
  qscan: '先自动耦合或记录出腔功率，再运行大扫 / resume / standardize。',
  sensitivity: '锁好模式后采 PSD、网分响应，并计算灵敏度。',
  coupling: '只做 MDT693B 自动耦合优化；适合调光路时单独打开。',
  qtable: '查看当前腔的 Q 表，点行可把波长送到手动目标。',
  settings: '连接、安全关闭、仪器配置和诊断集中在这里。'
}};
function setTask(task) {{
  document.body.dataset.task = task;
  for (const button of document.querySelectorAll('[data-task-button]')) {{
    button.classList.toggle('active', button.dataset.taskButton === task);
  }}
  const hint = document.getElementById('taskHint');
  if (hint) hint.textContent = taskHints[task] || '';
}}
const api = async (path, body=null, options={{}}) => {{
  const opt = body ? {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}} : {{}};
  if (options.signal) opt.signal = options.signal;
  const r = await fetch(path, opt);
  const j = await r.json();
  if (!r.ok) throw j;
  return j;
}};
const fmt = (x, digits=6) => (x === null || x === undefined || x === '') ? '-' : Number(x).toFixed(digits);
const qfmt = x => (x === null || x === undefined || x === '') ? '-' : (Number(x)/1e6).toFixed(3);
function setSelectValue(id, value) {{
  const el = document.getElementById(id);
  if ([...el.options].some(option => option.value === value)) el.value = value;
}}
function instrumentBody() {{
  return {{
    rp_type: document.getElementById('rpType').value,
    bridge_base: document.getElementById('bridgeBaseInput').value.trim(),
    rp_host: document.getElementById('rpHostInput').value.trim(),
    laser_type: document.getElementById('laserType').value,
    toptica_host: document.getElementById('topticaHostInput').value.trim(),
    laser_port: document.getElementById('laserPortInput').value.trim(),
    weiyuan_slave: document.getElementById('weiyuanSlaveInput').value.trim(),
    scope_type: document.getElementById('scopeType').value,
    scope_resource: document.getElementById('scopeResourceInput').value.trim()
  }};
}}
function updateInstrumentHeader() {{
  const cfg = instrumentBody();
  const mode = document.getElementById('experimentMode').value;
  document.getElementById('bridgeBase').textContent = cfg.rp_type === 'none' ? 'none' : cfg.bridge_base;
  document.getElementById('headerMode').textContent = modeLabels[mode] || mode;
  document.getElementById('headerLaser').textContent = cfg.laser_type === 'none' ? 'none' : cfg.laser_type;
  document.getElementById('laserPort').textContent = cfg.laser_type === 'toptica_tcp' ? cfg.toptica_host : cfg.laser_port;
  document.getElementById('scopeResource').textContent = cfg.scope_type === 'none' ? 'none' : cfg.scope_resource;
}}
const bodyBase = () => ({{ cavity_dir: document.getElementById('cavityDir').value, ...instrumentBody() }});

function inferExperimentMode(laserType) {{
  if (laserType === 'weiyuan') return 'weiyuan_lock';
  if (laserType === 'none') return 'rp_debug';
  return 'toptica_q_lock';
}}
function setVisible(selector, visible) {{
  for (const el of document.querySelectorAll(selector)) {{
    el.classList.toggle('hidden', !visible);
  }}
}}
function applyExperimentMode(fromUser=false) {{
  const mode = document.getElementById('experimentMode').value;
  if (mode === 'toptica_q_lock') {{
    if (fromUser || document.getElementById('laserType').value === 'none' || document.getElementById('laserType').value === 'weiyuan') {{
      setSelectValue('laserType', 'toptica_serial');
      setSelectValue('scopeType', 'rs_rte');
      const port = document.getElementById('laserPortInput');
      if (!port.value.trim() || port.value.trim().toUpperCase() === 'COM5') port.value = 'COM3';
    }}
  }} else if (mode === 'weiyuan_lock') {{
    if (fromUser || document.getElementById('laserType').value !== 'weiyuan') {{
      setSelectValue('laserType', 'weiyuan');
      const port = document.getElementById('laserPortInput');
      if (!port.value.trim() || port.value.trim().toUpperCase() === 'COM3') port.value = 'COM5';
    }}
    setSelectValue('scopeType', 'none');
  }} else if (mode === 'rp_debug') {{
    if (fromUser || document.getElementById('laserType').value !== 'none') {{
      setSelectValue('laserType', 'none');
    }}
    setSelectValue('scopeType', 'none');
  }}
  const cfg = instrumentBody();
  const isTopticaMode = mode === 'toptica_q_lock';
  const isWeiyuanMode = mode === 'weiyuan_lock';
  const isDebugMode = mode === 'rp_debug';

  document.getElementById('modeNote').textContent = modeNotes[mode] || '';
  setVisible('.laser-connection', !isDebugMode);
  setVisible('.scope-connection', isTopticaMode);
  setVisible('.selected-panel', !isDebugMode);
  setVisible('.target-controls', isTopticaMode);
  setVisible('.large-scan-panel', isTopticaMode);
  setVisible('.q-table-panel', isTopticaMode);
  document.getElementById('weiyuanPanel').classList.toggle('hidden', !isWeiyuanMode);
  document.getElementById('topticaHostField').classList.toggle('hidden', cfg.laser_type !== 'toptica_tcp');
  document.getElementById('laserPortLabel').textContent = cfg.laser_type === 'weiyuan' ? 'Micro-source COM port' : 'Laser COM / port';
  updateInstrumentHeader();
  const currentTask = document.body.dataset.task;
  if (isDebugMode && currentTask !== 'settings') {{
    setTask('settings');
  }} else if (isWeiyuanMode && ['qscan', 'coupling', 'qtable'].includes(currentTask)) {{
    setTask('lock');
  }}
}}

function addLine(lines, label, value) {{
  if (value === null || value === undefined || value === '') return;
  lines.push(`${{label}}: ${{value}}`);
}}
function summarizePayload(payload) {{
  const lines = [];
  const ok = payload && payload.ok;
  lines.push(`ok: ${{ok ? 'true' : 'false'}}`);
  addLine(lines, 'elapsed', payload && payload.elapsed_s !== undefined ? `${{Number(payload.elapsed_s).toFixed(2)}} s` : null);
  addLine(lines, 'returncode', payload && payload.returncode);

  const j = payload && payload.json ? payload.json : payload;
  addLine(lines, 'message', j && j.message);
  addLine(lines, 'output dir', j && j.output_dir);
  addLine(lines, 'run json', j && j.run_json_path);
  addLine(lines, 'q rows', j && j.q_rows);
  addLine(lines, 'candidate source', j && j.candidate_source);
  const failure = j && j.failure;
  if (failure) {{
    addLine(lines, 'failure stage', failure.stage);
    addLine(lines, 'failure reason', failure.reason);
    addLine(lines, 'failure error', failure.error);
  }}
  if (j && j.error) addLine(lines, 'error', j.error);

  const device = j && j.device_profile;
  if (device) {{
    addLine(lines, 'PD model', device.photodetector_model);
    addLine(lines, 'scope CH1 response', device.scope_ch1_response_v_per_w !== null && device.scope_ch1_response_v_per_w !== undefined ? `${{Number(device.scope_ch1_response_v_per_w).toPrecision(5)}} V/W` : null);
    addLine(lines, 'scope CH2 response', device.scope_ch2_response_v_per_w !== null && device.scope_ch2_response_v_per_w !== undefined ? `${{Number(device.scope_ch2_response_v_per_w).toPrecision(5)}} V/W` : null);
    if (device.scope_zero_enabled) {{
      const z1 = device.scope_ch1_zero_offset_v !== null && device.scope_ch1_zero_offset_v !== undefined ? `${{(Number(device.scope_ch1_zero_offset_v) * 1e3).toFixed(3)}} mV` : '-';
      const z2 = device.scope_ch2_zero_offset_v !== null && device.scope_ch2_zero_offset_v !== undefined ? `${{(Number(device.scope_ch2_zero_offset_v) * 1e3).toFixed(3)}} mV` : '-';
      lines.push(`scope display zero: on, CH1 ${{z1}}, CH2 ${{z2}}`);
    }} else {{
      lines.push('scope display zero: off');
    }}
    addLine(lines, 'PD saturation', device.saturation_power_uw !== null && device.saturation_power_uw !== undefined ? `${{Number(device.saturation_power_uw).toPrecision(5)}} uW` : null);
    const rfParts = [];
    if (device.bias_tee_enabled) rfParts.push(`bias tee=${{device.bias_tee_model || 'on'}}`);
    if (device.amplifier_enabled) rfParts.push(`amp=${{device.amplifier_model || 'on'}}`);
    if (rfParts.length) lines.push(`RF path: ${{rfParts.join(', ')}}`);
    addLine(lines, 'spectrum external_gain', device.spectrum_external_gain_db !== null && device.spectrum_external_gain_db !== undefined ? `${{Number(device.spectrum_external_gain_db).toFixed(3)}} dB (${{device.spectrum_external_gain_source || 'config'}})` : null);
  }}

  const instruments = j && j.instruments;
  if (instruments) {{
    const rp = instruments.red_pitaya || {{}};
    const laser = instruments.laser || {{}};
    const scope = instruments.oscilloscope || {{}};
    lines.push(`RP / PyRPL: ${{rp.type || '-'}} ${{rp.bridge_base || ''}} (${{rp.ok === null || rp.ok === undefined ? 'skipped' : (rp.ok ? 'ok' : 'not responding')}})`);
    lines.push(`Laser: ${{laser.type || '-'}} ${{laser.host || laser.port || ''}} (${{laser.ok === null || laser.ok === undefined ? 'skipped' : (laser.ok ? 'ok' : 'not responding')}})`);
    lines.push(`Oscilloscope: ${{scope.type || '-'}} ${{scope.scope_resource || ''}} (${{scope.ok === null || scope.ok === undefined ? 'skipped' : (scope.ok ? 'ok' : 'not responding')}})`);
  }}

  const weiyuan = j && j.weiyuan;
  if (weiyuan) {{
    lines.push(`micro-source: ${{weiyuan.port || '-'}}, mode=${{weiyuan.mode_label || weiyuan.mode}}`);
    addLine(lines, 'TEC temp', weiyuan.tec_temp_c !== undefined ? `${{Number(weiyuan.tec_temp_c).toFixed(3)}} C` : null);
    addLine(lines, 'TEC set', weiyuan.tec_set_temp_c !== undefined ? `${{Number(weiyuan.tec_set_temp_c).toFixed(3)}} C` : null);
    addLine(lines, 'LD actual', weiyuan.ld_current_actual_ma !== undefined ? `${{Number(weiyuan.ld_current_actual_ma).toFixed(3)}} mA` : null);
    addLine(lines, 'LD set', weiyuan.ld_set_current_ma !== undefined ? `${{Number(weiyuan.ld_set_current_ma).toFixed(3)}} mA` : null);
    lines.push(`TEC/LD enabled: ${{weiyuan.tec_enabled ? 'on' : 'off'}} / ${{weiyuan.ld_enabled ? 'on' : 'off'}}`);
  }}

  const spectrum = j && j.spectrum;
  if (spectrum) {{
    addLine(lines, 'spectrum raw', spectrum.raw_path);
    addLine(lines, 'spectrum meta', spectrum.metadata_path);
    addLine(lines, 'spectrum html', spectrum.plot_html);
    addLine(lines, 'spectrum rbw', spectrum.rbw_hz !== undefined && spectrum.rbw_hz !== null ? `${{Number(spectrum.rbw_hz).toPrecision(6)}} Hz` : null);
    addLine(lines, 'spectrum n', spectrum.n);
    const specSummary = spectrum.summary || {{}};
    const specPower = specSummary.input1_dbm_per_hz || specSummary.dbm_per_hz || {{}};
    addLine(lines, 'spectrum median', specPower.median !== undefined ? `${{Number(specPower.median).toFixed(2)}} dBm/Hz` : null);
    const corr = spectrum.display_power_correction || {{}};
    addLine(lines, 'spectrum correction', corr.total_subtracted_db !== undefined ? `${{Number(corr.total_subtracted_db).toFixed(3)}} dB subtracted` : null);
  }}

  const network = j && j.network;
  if (network) {{
    addLine(lines, 'network raw', network.raw_path);
    addLine(lines, 'network meta', network.metadata_path);
    addLine(lines, 'network html', network.plot_html);
    addLine(lines, 'network input', network.input);
    addLine(lines, 'network output', network.output_direct);
    addLine(lines, 'network amplitude', network.amplitude_v !== undefined && network.amplitude_v !== null ? `${{Number(network.amplitude_v).toPrecision(5)}} ${{network.amplitude_unit || 'V'}}` : null);
    addLine(lines, 'network n', network.n);
    const display = network.power_display || {{}};
    addLine(lines, 'network display', display.display_unit ? `${{display.display_unit}}, correction=${{Number(display.total_subtracted_db || 0).toFixed(3)}} dB` : null);
    const netSummary = network.summary || {{}};
    const netDbm = netSummary.magnitude_dbm || {{}};
    const netDb = netSummary.magnitude_db || {{}};
    addLine(lines, 'network median', netDbm.median !== undefined ? `${{Number(netDbm.median).toFixed(2)}} dBm` : (netDb.median !== undefined ? `${{Number(netDb.median).toFixed(2)}} dB` : null));
  }}

  const hostCheck = j && j.host_check;
  if (hostCheck) {{
    addLine(lines, 'RP host', hostCheck.host);
    if (hostCheck.addresses && hostCheck.addresses.length) {{
      lines.push(`resolved: ${{hostCheck.addresses.map(item => `${{item.family}} ${{item.address}}`).join(', ')}}`);
    }}
    if (hostCheck.warnings && hostCheck.warnings.length) lines.push(`warning: ${{hostCheck.warnings.join('; ')}}`);
    addLine(lines, 'external_gain if started', hostCheck.external_gain_db_if_started !== undefined ? `${{hostCheck.external_gain_db_if_started}} dB` : null);
  }}

  const pyrplConfig = j && j.pyrpl_config;
  if (pyrplConfig) {{
    addLine(lines, 'PyRPL config', pyrplConfig.config_path);
    addLine(lines, 'PyRPL host/timeout', pyrplConfig.hostname ? `${{pyrplConfig.hostname}}, timeout=${{pyrplConfig.timeout_s}} s, reloadserver=${{pyrplConfig.reloadserver ? 'true' : 'false'}}` : null);
    addLine(lines, 'PyRPL spectrum window', pyrplConfig.spectrumanalyzer_window);
    if (pyrplConfig.actions && pyrplConfig.actions.length) lines.push(`PyRPL config actions: ${{pyrplConfig.actions.join(', ')}}`);
  }}

  const bridgeStatus = j && j.status;
  if (bridgeStatus) {{
    const process = bridgeStatus.process || {{}};
    const health = bridgeStatus.health || {{}};
    const bridge = health.bridge || {{}};
    const correction = health.spectrum_power_correction || {{}};
    lines.push(`bridge http: ${{bridgeStatus.ok ? 'ok' : 'not responding'}}`);
    lines.push(`bridge process: ${{process.managed ? 'dashboard-managed' : 'external/none'}}, ${{process.running ? 'running' : 'not running'}}${{process.pid ? ', pid=' + process.pid : ''}}`);
    addLine(lines, 'bridge python', process.python_executable);
    addLine(lines, 'bridge RP host', process.rp_host);
    addLine(lines, 'PyRPL connect host', process.pyrpl_host);
    addLine(lines, 'bridge external_gain', process.external_gain_db !== null && process.external_gain_db !== undefined ? `${{process.external_gain_db}} dB` : null);
    addLine(lines, 'bridge log', process.log_path);
    if (!bridgeStatus.ok && process.log_tail) {{
      lines.push(`bridge log tail:\\n${{String(process.log_tail).slice(-1600)}}`);
    }}
    addLine(lines, 'live bridge pid', bridge.pid);
    addLine(lines, 'live bridge python', bridge.python_executable);
    addLine(lines, 'live bridge PyRPL', bridge.pyrpl_version && bridge.pyrpl_file ? `${{bridge.pyrpl_version}} @ ${{bridge.pyrpl_file}}` : null);
    addLine(lines, 'live bridge config', bridge.config);
    addLine(lines, 'live bridge RP host', bridge.hostname);
    addLine(lines, 'live bridge mode', bridge.headless === true ? 'headless' : (bridge.headless === false ? 'GUI' : null));
    addLine(lines, 'live bridge uptime', bridge.uptime_s !== null && bridge.uptime_s !== undefined ? `${{Number(bridge.uptime_s).toFixed(1)}} s` : null);
    addLine(lines, 'live spectrum correction', correction.total_subtracted_db !== null && correction.total_subtracted_db !== undefined ? `${{Number(correction.total_subtracted_db).toFixed(3)}} dB subtracted` : null);
  }}

  const power = j && j.power;
  if (power) {{
    addLine(lines, '1% monitor', power.input_monitor_power_uw !== null && power.input_monitor_power_uw !== undefined ? `${{Number(power.input_monitor_power_uw).toFixed(4)}} uW` : null);
    addLine(lines, 'Pout', power.output_power_uw !== null && power.output_power_uw !== undefined ? `${{Number(power.output_power_uw).toFixed(4)}} uW` : null);
    addLine(lines, 'single-ended loss', power.single_ended_loss_db !== null && power.single_ended_loss_db !== undefined ? `${{Number(power.single_ended_loss_db).toFixed(2)}} dB` : null);
    addLine(lines, 'power source', power.source);
  }}

  const q = j && j.q;
  if (q) {{
    addLine(lines, 'Q rows', q.q_rows);
    addLine(lines, 'Q dir', q.q_dir);
  }}

  const c = j && j.candidate;
  if (c) {{
    const family = c.family_label || c.family || '-';
    lines.push(`target: ${{family}}, mu=${{c.mode_number ?? '-'}}, ${{fmt(c.wavelength_nm, 6)}} nm`);
    lines.push(`Q0 / Q1 / QL: ${{qfmt(c.Q0)}} / ${{qfmt(c.Q1)}} / ${{qfmt(c.QL)}} M`);
    addLine(lines, 'depth', fmt(c.depth, 3));
  }}

  const move = j && j.wavelength_move;
  if (move) {{
    const before = move.before_read_nm ?? move.before_set_nm;
    const after = move.after_read_nm ?? move.after_set_nm ?? move.target_nm;
    lines.push(`wavelength: ${{fmt(before, 6)}} -> ${{fmt(after, 6)}} nm`);
    addLine(lines, 'move ok', move.ok);
  }}

  const prelock = j && j.prelock;
  if (prelock) {{
    const pc = prelock.pc_start || {{}};
    const parts = [];
    if (pc.voltage_set !== undefined) parts.push(`PC=${{pc.voltage_set}} V`);
    if (prelock.arc_factor !== undefined) parts.push(`ARC=${{prelock.arc_factor}}`);
    if (prelock.pc_enabled !== undefined) parts.push(`PC enabled=${{prelock.pc_enabled}}`);
    if (parts.length) lines.push(`prelock: ${{parts.join(', ')}}`);
  }}

  if (payload && payload.stderr_tail && payload.stderr_tail.trim()) lines.push('stderr: see raw details');
  const restore = j && j.restore;
  if (restore) {{
    const good = (restore.steps || []).filter(step => step.ok).length;
    const total = (restore.steps || []).length;
    lines.push(`restore steps: ${{good}}/${{total}} ok`);
  }}
  return lines.join('\\n');
}}
function renderCompactResult(el, payload) {{
  el.innerHTML = '';
  const summary = document.createElement('div');
  summary.className = `result-summary ${{payload && payload.ok ? 'result-ok' : 'result-bad'}}`;
  summary.textContent = summarizePayload(payload);
  el.appendChild(summary);

  const details = document.createElement('details');
  const detailsTitle = document.createElement('summary');
  detailsTitle.textContent = 'raw details';
  const raw = document.createElement('pre');
  raw.textContent = JSON.stringify(payload, null, 2);
  details.appendChild(detailsTitle);
  details.appendChild(raw);
  el.appendChild(details);
}}
function renderErrorResult(el, error) {{
  if (error && typeof error === 'object' && error.ok !== undefined) {{
    renderCompactResult(el, error);
  }} else {{
    renderCompactResult(el, {{ok:false, error}});
  }}
}}
function renderStepResults(el, steps) {{
  const ok = steps.every(step => step.payload && step.payload.ok);
  renderCompactResult(el, {{
    ok,
    message: steps.map(step => `${{step.name}}: ${{step.payload && step.payload.ok ? 'ok' : 'failed'}}`).join('; '),
    steps
  }});
}}

async function refreshHealth() {{
  const el = document.getElementById('health');
  updateInstrumentHeader();
  el.textContent = 'checking...';
  try {{
    const params = new URLSearchParams(instrumentBody());
    renderCompactResult(el, await api('/api/health?' + params.toString()));
  }}
  catch(e) {{ el.textContent = JSON.stringify(e, null, 2); }}
}}
async function safeOff() {{
  const el = document.getElementById('health');
  el.textContent = 'safe-off running...';
  try {{ renderCompactResult(el, await api('/api/safe_off', instrumentBody())); }}
  catch(e) {{ el.textContent = JSON.stringify(e, null, 2); }}
}}
async function runBridgeAction() {{
  const el = document.getElementById('health');
  updateInstrumentHeader();
  const action = document.getElementById('bridgeAction').value;
  el.textContent = `bridge action: ${{action}}...`;
  try {{
    renderCompactResult(el, await api('/api/bridge/action', {{...instrumentBody(), bridge_action: action}}));
  }} catch(e) {{ renderErrorResult(el, e); }}
}}
async function runWeiyuan(action) {{
  const el = document.getElementById('weiyuanResult');
  updateInstrumentHeader();
  el.textContent = `micro-source action: ${{action}}...`;
  const body = {{
    ...instrumentBody(),
    weiyuan_action: action,
    temperature_c: document.getElementById('weiyuanTempInput').value,
    current_ma: document.getElementById('weiyuanCurrentInput').value,
    weiyuan_slave: document.getElementById('weiyuanSlaveInput').value
  }};
  try {{ renderCompactResult(el, await api('/api/weiyuan/action', body)); }}
  catch(e) {{ renderErrorResult(el, e); }}
}}
function setNumberInput(id, value, fallback='') {{
  const el = document.getElementById(id);
  if (!el) return;
  if (value === null || value === undefined || value === '' || Number.isNaN(Number(value))) {{
    el.value = fallback;
  }} else {{
    el.value = Number(value).toPrecision(8);
  }}
}}
function optionLabelHz(value) {{
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  if (Math.abs(n) >= 1e6) return `${{(n/1e6).toPrecision(7)}} MHz`;
  if (Math.abs(n) >= 1e3) return `${{(n/1e3).toPrecision(7)}} kHz`;
  return `${{n.toPrecision(7)}} Hz`;
}}
function setNumericOptions(id, options, current, fallbackOptions=[]) {{
  const el = document.getElementById(id);
  if (!el) return;
  const values = (Array.isArray(options) && options.length ? options : fallbackOptions)
    .map(v => Number(v))
    .filter(v => Number.isFinite(v));
  el.innerHTML = '';
  for (const v of values) {{
    const option = document.createElement('option');
    option.value = String(v);
    option.textContent = optionLabelHz(v);
    el.appendChild(option);
  }}
  if (!values.length) {{
    const option = document.createElement('option');
    option.value = current === null || current === undefined ? '' : String(current);
    option.textContent = current === null || current === undefined ? '-' : optionLabelHz(current);
    el.appendChild(option);
  }}
  setClosestNumericOption(id, current);
}}
function setClosestNumericOption(id, current) {{
  const el = document.getElementById(id);
  if (!el || !el.options.length) return;
  const target = Number(current);
  if (!Number.isFinite(target)) return;
  let best = el.options[0], bestErr = Math.abs(Number(best.value) - target);
  for (const option of el.options) {{
    const err = Math.abs(Number(option.value) - target);
    if (err < bestErr) {{ best = option; bestErr = err; }}
  }}
  el.value = best.value;
}}
function setSpectrumSpanOptions(pairs, currentSpan, currentRbw) {{
  const span = document.getElementById('spectrumSpanHz');
  span.innerHTML = '';
  const rows = Array.isArray(pairs) && pairs.length ? pairs : [
    {{span_hz:31250000, rbw_hz:5381}},
    {{span_hz:15625000, rbw_hz:2690}},
    {{span_hz:7812500, rbw_hz:1345}},
    {{span_hz:3906250, rbw_hz:672}},
  ];
  for (const row of rows) {{
    const option = document.createElement('option');
    option.value = String(Number(row.span_hz));
    option.dataset.rbw = String(Number(row.rbw_hz));
    option.textContent = `${{optionLabelHz(row.span_hz)}}  /  RBW ${{optionLabelHz(row.rbw_hz)}}`;
    span.appendChild(option);
  }}
  setClosestNumericOption('spectrumSpanHz', currentSpan);
  syncSpectrumRbwFromSpan(currentRbw);
}}
function syncSpectrumRbwFromSpan(preferredRbw=null) {{
  const span = document.getElementById('spectrumSpanHz');
  const rbw = document.getElementById('spectrumRbwHz');
  rbw.innerHTML = '';
  const selected = span.selectedOptions[0];
  const value = preferredRbw !== null && preferredRbw !== undefined ? preferredRbw : (selected ? selected.dataset.rbw : '');
  const option = document.createElement('option');
  option.value = String(value || '');
  option.textContent = value ? optionLabelHz(value) : '-';
  rbw.appendChild(option);
}}
function setPressureSourceOptions(sources, currentPath, currentSourceModel) {{
  const select = document.getElementById('pressureSourceModel');
  if (!select) return;
  const rows = Array.isArray(sources) ? sources : [];
  select.innerHTML = '';
  const manual = document.createElement('option');
  manual.value = '';
  manual.textContent = 'manual file path';
  select.appendChild(manual);
  const normalizedCurrent = String(currentPath || '').replaceAll('\\\\', '/').toLowerCase();
  let selectedValue = '';
  for (const source of rows) {{
    const option = document.createElement('option');
    option.value = source.id || source.model || source.label || '';
    option.textContent = source.label || source.model || source.id || option.value;
    option.dataset.path = source.path || '';
    option.dataset.drive = source.calibration_drive_vpp || '';
    option.dataset.quantity = source.pressure_quantity || 'pk';
    option.dataset.exists = source.exists ? '1' : '0';
    select.appendChild(option);
    const normalizedPath = String(source.path || '').replaceAll('\\\\', '/').toLowerCase();
    if (
      (currentSourceModel && option.value === currentSourceModel) ||
      (normalizedCurrent && normalizedPath && normalizedCurrent === normalizedPath)
    ) {{
      selectedValue = option.value;
    }}
  }}
  select.value = selectedValue;
  select.onchange = () => {{
    const option = select.selectedOptions[0];
    if (!option || !option.value) return;
    document.getElementById('pressureCalibrationPath').value = option.dataset.path || '';
    if (option.dataset.drive) setNumberInput('pressureCalibrationDriveVpp', option.dataset.drive, '10');
    document.getElementById('pressureQuantity').value = String(option.dataset.quantity || 'pk').toLowerCase().includes('rms') ? 'rms' : 'pk';
    document.getElementById('pressureInfo').textContent = option.dataset.exists === '1' ? 'Pressure source selected; calibration range will be read from the npz before acquisition.' : 'Selected pressure source has no readable default npz.';
  }};
}}
function openSensitivitySettings(mode, settings=null, note='Review PyRPL settings before acquisition.') {{
  document.getElementById('spectrumSettingsGroup').classList.toggle('hidden', !(mode === 'psd' || mode === 'both'));
  document.getElementById('networkSettingsGroup').classList.toggle('hidden', !(mode === 'network' || mode === 'both'));
  document.getElementById('pressureSettingsGroup').classList.toggle('hidden', !(mode === 'both' || mode === 'compute'));
  document.getElementById('sensitivitySettingsNote').textContent = note;
  const spectrum = (settings && settings.spectrum) || {{}};
  const network = (settings && settings.network) || {{}};
  const pressure = (settings && settings.pressure_calibration) || {{}};
  setSpectrumSpanOptions(spectrum.span_rbw_options, spectrum.span_hz, spectrum.rbw_hz);
  setNumberInput('spectrumTraceAverage', spectrum.trace_average, '1');
  const pointText = spectrum.display_points ? `Spectrum points: ${{spectrum.display_points}} shown by PyRPL before dashboard downsampling; data_length=${{spectrum.data_length || '-'}}, window=${{spectrum.window || '-'}}.` : 'Spectrum points are fixed by the PyRPL scope FFT path.';
  document.getElementById('spectrumPointInfo').textContent = pointText;
  setNumberInput('networkAmplitudeVpk', network.amplitude_vpk, '1');
  setNumberInput('networkStartHz', network.start_freq_hz, '1000');
  setNumberInput('networkStopHz', network.stop_freq_hz, '1000000');
  setNumberInput('networkPoints', network.points, '10001');
  setNumericOptions('networkRbwHz', network.rbw_options, network.rbw_hz, [151.78256, 303.56512, 607.13025, 1214.2605, 2428.521, 4857.042, 9714.084]);
  setPressureSourceOptions(settings && settings.pressure_sources, pressure.path, pressure.source_model);
  document.getElementById('pressureCalibrationPath').value = pressure.path || '';
  const pStart = Number(pressure.start_hz);
  const pStop = Number(pressure.stop_hz);
  const pPoints = pressure.points || '-';
  document.getElementById('pressureCalibrationRange').value = Number.isFinite(pStart) && Number.isFinite(pStop) ? `${{optionLabelHz(pStart)}} - ${{optionLabelHz(pStop)}} (${{pPoints}} pts)` : (pressure.error || 'not loaded');
  setNumberInput('pressureCalibrationDriveVpp', pressure.calibration_drive_vpp, '10');
  document.getElementById('pressureQuantity').value = (pressure.pressure_quantity || 'pk').toLowerCase().includes('rms') ? 'rms' : 'pk';
  setNumberInput('pressureUseStartHz', pressure.use_start_hz || pressure.start_hz, pressure.start_hz || '10000');
  setNumberInput('pressureUseStopHz', pressure.use_stop_hz || pressure.stop_hz, pressure.stop_hz || '1000000');
  document.getElementById('pressureInfo').textContent = pressure.error ? `Pressure calibration load failed: ${{pressure.error}}` : `Default use range follows calibration data; edit only when you want a narrower band.`;
  document.getElementById('sensitivitySettingsModal').classList.remove('hidden');
}}
function closeSensitivitySettings() {{
  document.getElementById('sensitivitySettingsModal').classList.add('hidden');
  pendingSensitivityBody = null;
}}
function sensitivitySettingBody() {{
  const mode = (pendingSensitivityBody && pendingSensitivityBody.acquisition_mode) || document.getElementById('sensitivityMode').value;
  const body = {{
    pressure_source_model: document.getElementById('pressureSourceModel').value,
    pressure_calibration_path: document.getElementById('pressureCalibrationPath').value,
    pressure_calibration_drive_vpp: document.getElementById('pressureCalibrationDriveVpp').value,
    pressure_quantity: document.getElementById('pressureQuantity').value,
    pressure_use_start_hz: document.getElementById('pressureUseStartHz').value,
    pressure_use_stop_hz: document.getElementById('pressureUseStopHz').value
  }};
  if (mode !== 'compute') {{
    Object.assign(body, {{
    spectrum_span_hz: document.getElementById('spectrumSpanHz').value,
    spectrum_rbw_hz: document.getElementById('spectrumRbwHz').value,
    spectrum_trace_average: document.getElementById('spectrumTraceAverage').value,
    network_amplitude_vpk: document.getElementById('networkAmplitudeVpk').value,
    network_start_freq_hz: document.getElementById('networkStartHz').value,
    network_stop_freq_hz: document.getElementById('networkStopHz').value,
    network_points: document.getElementById('networkPoints').value,
    network_rbw_hz: document.getElementById('networkRbwHz').value
    }});
  }}
  return body;
}}
async function runSensitivityAcquisition() {{
  const el = document.getElementById('sensitivityResult');
  updateInstrumentHeader();
  const mode = document.getElementById('sensitivityMode').value;
  pendingSensitivityBody = {{
    ...bodyBase(),
    tag_prefix: document.getElementById('sensitivityTag').value,
    acquisition_mode: mode,
    spectrum_timeout_s: document.getElementById('sensitivitySpectrumTimeout').value,
    network_timeout_s: document.getElementById('sensitivityNetworkTimeout').value,
    max_points: document.getElementById('sensitivityMaxPoints').value,
    optical_mode: chosenCandidate(),
    target_kind: document.getElementById('targetKind').value,
    acquire: true
  }};
  el.textContent = 'loading current PyRPL acquisition settings...';
  try {{
    const settings = await api('/api/sensitivity/settings', {{...bodyBase(), acquisition_mode: mode}});
    renderCompactResult(el, {{ok:true, json:{{message:'settings loaded; confirm to run'}}}});
    openSensitivitySettings(mode, settings, 'Current PyRPL settings loaded. Edit values, then confirm acquisition.');
  }} catch(e) {{
    renderCompactResult(el, {{ok:false, json:{{message:'could not read current settings; using editable defaults', error:e.error || JSON.stringify(e)}}}});
    openSensitivitySettings(mode, null, 'Could not read current PyRPL settings. Defaults are editable; confirm only if they are correct.');
  }}
}}
async function confirmSensitivityAcquisition() {{
  const el = document.getElementById('sensitivityResult');
  if (!pendingSensitivityBody) {{
    el.textContent = 'no pending sensitivity acquisition';
    return;
  }}
  const mode = pendingSensitivityBody.acquisition_mode;
  const modeText = mode === 'psd' ? 'noise PSD' : (mode === 'network' ? 'network response' : (mode === 'compute' ? 'sensitivity from existing raw data' : 'noise PSD, then network response'));
  const body = {{...pendingSensitivityBody, ...sensitivitySettingBody()}};
  closeSensitivitySettings();
  el.textContent = `applying settings and capturing ${{modeText}}...`;
  sensitivityAbortController = new AbortController();
  setSensitivityBusy(true);
  try {{
    const result = await api('/api/sensitivity/acquire', body, {{signal: sensitivityAbortController.signal}});
    renderCompactResult(el, result);
    if (result.no_cavity_dir && result.output_dir) {{
      alert(`No cavity directory was selected. Data saved to:\\n${{result.output_dir}}`);
    }}
  }} catch(e) {{
    if (e && e.name === 'AbortError') {{
      renderCompactResult(el, {{ok:false, json:{{message:'cancel requested; waiting for instrument call to unwind if PyRPL was already busy'}}}});
    }} else {{
      renderErrorResult(el, e);
    }}
  }} finally {{
    sensitivityAbortController = null;
    setSensitivityBusy(false);
  }}
}}
function setSensitivityBusy(isBusy) {{
  document.getElementById('runSensitivityButton').disabled = isBusy;
  document.getElementById('cancelSensitivityButton').classList.toggle('hidden', !isBusy);
}}
async function cancelSensitivityAcquisition() {{
  const el = document.getElementById('sensitivityResult');
  if (sensitivityAbortController) sensitivityAbortController.abort();
  renderCompactResult(el, {{ok:false, json:{{message:'cancel requested'}}}});
  try {{
    const result = await api('/api/sensitivity/cancel', bodyBase());
    renderCompactResult(el, result);
  }} catch(e) {{
    renderErrorResult(el, e);
  }}
}}
async function loadCavity() {{
  const el = document.getElementById('targetResult');
  const path = document.getElementById('cavityDir').value.trim();
  if (!path) {{
    cavity = null;
    manualSelectedRow = null;
    document.getElementById('qRows').innerHTML = '';
    document.getElementById('targetPreview').innerHTML = '<span class="muted">Choose a cavity directory when needed.</span>';
    renderCompactResult(el, {{ok:true, json:{{message:'no cavity selected'}}}});
    return;
  }}
  el.textContent = 'loading cavity...';
  try {{
    cavity = await api('/api/cavity?cavity_dir=' + encodeURIComponent(path));
    manualSelectedRow = null;
    renderRows();
    renderTarget();
    await loadLargeScanStatus();
    renderCompactResult(el, {{ok:true, json:{{message:'cavity loaded', q_rows:cavity.q_rows.length, manifest_path:cavity.manifest_path}}}});
  }} catch(e) {{ renderErrorResult(el, e); }}
}}
async function browseCavity() {{
  const el = document.getElementById('targetResult');
  el.textContent = 'opening folder picker...';
  try {{
    const result = await api('/api/pick_folder', {{initial_dir: document.getElementById('cavityDir').value}});
    if (result.cancelled) {{
      renderCompactResult(el, {{ok:true, json:{{message:'folder picker cancelled'}}}});
      return;
    }}
    if (result.path) {{
      document.getElementById('cavityDir').value = result.path;
      await loadCavity();
      return;
    }}
    renderCompactResult(el, result);
  }} catch(e) {{ renderErrorResult(el, e); }}
}}
function chosenCandidate() {{
  if (!cavity || !cavity.manifest) return null;
  const kind = document.getElementById('targetKind').value;
  if (kind === 'candidate') return cavity.manifest.candidate;
  if (kind === 'nearest_1550') return cavity.manifest.nearest_1550_best_q_candidate;
  const wl = Number(document.getElementById('manualWavelength').value);
  if (!Number.isFinite(wl)) return null;
  if (manualSelectedRow && Math.abs(Number(manualSelectedRow.wavelength_nm) - wl) < 1e-6) return manualSelectedRow;
  const matched = (cavity.q_rows || []).find(row => Math.abs(Number(row.wavelength_nm) - wl) < 1e-6);
  return matched || {{family:'manual', family_label:'manual', mode_number:'-', wavelength_nm:wl, Q0:null, Q1:null, QL:null, depth:null}};
}}
function renderTarget() {{
  const c = chosenCandidate();
  const el = document.getElementById('targetPreview');
  if (!c) {{ el.innerHTML = '<span class="muted">No target loaded.</span>'; return; }}
  el.innerHTML = `
    <span>family</span><b>${{c.family_label || c.family || '-'}}</b>
    <span>mu</span><b>${{c.mode_number ?? '-'}}</b>
    <span>wavelength</span><b>${{fmt(c.wavelength_nm, 6)}} nm</b>
    <span>Q0 / Q1 / QL</span><b>${{qfmt(c.Q0)}} / ${{qfmt(c.Q1)}} / ${{qfmt(c.QL)}} M</b>
    <span>depth</span><b>${{fmt(c.depth, 3)}}</b>`;
}}
function renderRows() {{
  const tbody = document.getElementById('qRows');
  tbody.innerHTML = '';
  if (!cavity) return;
  for (const row of cavity.q_rows) {{
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${{row.family_label || row.family}}</td><td>${{row.mode_number}}</td><td>${{fmt(row.wavelength_nm,6)}}</td><td>${{qfmt(row.Q0)}}</td><td>${{qfmt(row.Q1)}}</td><td>${{qfmt(row.QL)}}</td><td>${{fmt(row.depth,3)}}</td>`;
    tr.onclick = () => {{
      manualSelectedRow = row;
      document.getElementById('targetKind').value = 'manual';
      document.getElementById('manualWavelength').value = fmt(row.wavelength_nm, 9);
      renderTarget();
    }};
    tbody.appendChild(tr);
  }}
}}
function targetBody() {{
  const kind = document.getElementById('targetKind').value;
  const body = {{...bodyBase(), target_kind: kind}};
  if (kind === 'manual') body.wavelength_nm = document.getElementById('manualWavelength').value;
  return body;
}}
function requireCavityPath(el) {{
  const path = document.getElementById('cavityDir').value.trim();
  if (!path) {{
    renderCompactResult(el, {{ok:false, error:'Select a cavity directory first. Use Browse... or paste the cavity folder path.'}});
    return null;
  }}
  return path;
}}
function powerBody() {{
  return {{
    ...bodyBase(),
    input_monitor_power_uw: document.getElementById('inputMonitorPower').value,
    input_monitor_fraction: 0.01,
    output_power_uw: document.getElementById('outputPower').value
  }};
}}
function updateLossPreview() {{
  const monitor = Number(document.getElementById('inputMonitorPower').value);
  const pout = Number(document.getElementById('outputPower').value);
  const loss = document.getElementById('singleEndedLoss');
  if (!Number.isFinite(monitor) || monitor <= 0 || !Number.isFinite(pout) || pout < 0) {{
    loss.value = '';
    return;
  }}
  const inputPower = monitor / 0.01;
  const throughput = pout / inputPower;
  loss.value = throughput > 0 ? (-10 * Math.log10(Math.sqrt(throughput))).toFixed(2) : 'undefined';
}}
function fillPower(power) {{
  const source = document.getElementById('powerSource');
  if (!power) {{
    document.getElementById('inputMonitorPower').value = '';
    document.getElementById('outputPower').value = '';
    document.getElementById('singleEndedLoss').value = '';
    source.textContent = 'no previous power record';
    return;
  }}
  document.getElementById('inputMonitorPower').value = power.input_monitor_power_uw ?? '';
  document.getElementById('outputPower').value = power.output_power_uw ?? '';
  updateLossPreview();
  source.textContent = power.source ? `loaded from ${{power.source}}` : 'loaded previous power record';
}}
async function loadLargeScanStatus() {{
  const el = document.getElementById('largeScanResult');
  const path = document.getElementById('cavityDir').value.trim();
  if (!path) {{
    fillPower(null);
    renderCompactResult(el, {{ok:true, json:{{message:'no cavity selected'}}}});
    return;
  }}
  try {{
    const status = await api('/api/large_scan/status?cavity_dir=' + encodeURIComponent(path));
    fillPower(status.power);
    renderCompactResult(el, {{ok:true, json:{{message:'large-scan status loaded', power:status.power, q:status.q}}}});
  }} catch(e) {{
    fillPower(null);
    renderErrorResult(el, e);
  }}
}}
async function savePower() {{
  const el = document.getElementById('largeScanResult');
  if (!requireCavityPath(el)) return;
  el.textContent = 'saving power and refreshing card...';
  try {{
    const result = await api('/api/power/update', powerBody());
    if (result.json && result.json.power) fillPower(result.json.power);
    renderCompactResult(el, result);
  }} catch(e) {{ renderErrorResult(el, e); }}
}}
async function runStandardCoupling() {{
  const el = document.getElementById('couplingResult');
  const button = document.getElementById('runCouplingButton');
  el.textContent = 'running standard auto coupling...';
  button.disabled = true;
  try {{
    renderCompactResult(el, await api('/api/coupling/standard', bodyBase()));
    await loadLargeScanStatus();
  }} catch(e) {{
    renderErrorResult(el, e);
  }} finally {{
    button.disabled = false;
  }}
}}
async function runLargeScan(mode) {{
  const el = document.getElementById('largeScanResult');
  if (!requireCavityPath(el)) return;
  const labels = {{run:'running large scan Q...', resume:'resuming from existing raw...', standardize:'standardizing existing outputs...'}};
  el.textContent = labels[mode] || 'running...';
  try {{
    renderCompactResult(el, await api('/api/large_scan/run', {{...bodyBase(), mode}}));
    await loadCavity();
  }} catch(e) {{ renderErrorResult(el, e); }}
}}
async function stopLargeScan() {{
  const el = document.getElementById('largeScanResult');
  if (!requireCavityPath(el)) return;
  el.textContent = 'stopping large scan and restoring idle state...';
  try {{
    renderCompactResult(el, await api('/api/large_scan/stop', {{...bodyBase()}}));
    await loadLargeScanStatus();
  }} catch(e) {{ renderErrorResult(el, e); }}
}}
async function dryRunTarget() {{
  const el = document.getElementById('targetResult');
  if (!requireCavityPath(el)) return;
  el.textContent = 'dry-run...';
  try {{ renderCompactResult(el, await api('/api/target/dry_run', targetBody())); }}
  catch(e) {{ renderErrorResult(el, e); }}
}}
async function moveTarget() {{
  const el = document.getElementById('targetResult');
  if (!requireCavityPath(el)) return;
  el.textContent = 'moving wavelength, then restoring sweep / PID off...';
  try {{
    const moveResult = await api('/api/target/move', targetBody());
    let restoreResult;
    try {{
      restoreResult = await api('/api/lock/restore_sweep', bodyBase());
    }} catch (restoreError) {{
      restoreResult = (restoreError && typeof restoreError === 'object')
        ? restoreError
        : {{ok: false, error: restoreError}};
    }}
    renderStepResults(el, [
      {{name: 'move target wavelength', payload: moveResult}},
      {{name: 'restore sweep / PID off', payload: restoreResult}}
    ]);
  }}
  catch(e) {{ renderErrorResult(el, e); }}
}}
async function lockCurrent() {{
  const el = document.getElementById('targetResult');
  el.textContent = 'locking current mode...';
  try {{ renderCompactResult(el, await api('/api/lock/current', bodyBase())); }}
  catch(e) {{ renderErrorResult(el, e); }}
}}
async function restoreLockSweep(resultId='targetResult') {{
  const el = document.getElementById(resultId);
  el.textContent = 'restoring sweep and disabling PID...';
  try {{ renderCompactResult(el, await api('/api/lock/restore_sweep', bodyBase())); }}
  catch(e) {{ renderErrorResult(el, e); }}
}}
function finiteArray(arr) {{
  return (arr || []).map(Number).filter(Number.isFinite);
}}
function drawAxes(ctx, w, h, padL, padR, padT, padB) {{
  ctx.strokeStyle = '#d0d0d0';
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 0; i <= 4; i++) {{
    const y = padT + i * (h - padT - padB) / 4;
    ctx.moveTo(padL, y); ctx.lineTo(w - padR, y);
  }}
  for (let i = 0; i <= 5; i++) {{
    const x = padL + i * (w - padL - padR) / 5;
    ctx.moveTo(x, padT); ctx.lineTo(x, h - padB);
  }}
  ctx.stroke();
  ctx.strokeStyle = '#111';
  ctx.beginPath();
  ctx.rect(padL, padT, w - padL - padR, h - padT - padB);
  ctx.stroke();
}}
function drawXYChart(canvasId, series, options) {{
  const canvas = document.getElementById(canvasId);
  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, w, h);
  const allX = [];
  const allY = [];
  for (const item of series) {{
    for (let i = 0; i < item.x.length && i < item.y.length; i++) {{
      if (Number.isFinite(item.x[i]) && Number.isFinite(item.y[i])) {{
        allX.push(item.x[i]);
        allY.push(item.y[i]);
      }}
    }}
  }}
  if (!allX.length || !allY.length) {{
    ctx.fillStyle = '#333';
    ctx.font = '14px Arial';
    ctx.fillText(options.emptyText || 'no data returned', 20, 32);
    return;
  }}
  const padL = options.padL || 58, padR = options.padR || 18, padT = options.padT || 18, padB = options.padB || 34;
  let xMin = Math.min(...allX), xMax = Math.max(...allX);
  let yMin = Math.min(...allY), yMax = Math.max(...allY);
  if (!Number.isFinite(xMin) || !Number.isFinite(xMax) || xMin === xMax) {{ xMin -= 1; xMax += 1; }}
  if (!Number.isFinite(yMin) || !Number.isFinite(yMax) || yMin === yMax) {{ yMin -= 1; yMax += 1; }}
  const yPad = Math.max((yMax - yMin) * 0.08, 1e-9);
  yMin -= yPad;
  yMax += yPad;
  const sx = x => padL + (x - xMin) / (xMax - xMin || 1) * (w - padL - padR);
  const sy = y => padT + (yMax - y) / (yMax - yMin || 1) * (h - padT - padB);
  drawAxes(ctx, w, h, padL, padR, padT, padB);
  ctx.font = '12px Arial';
  let legendX = padL + 8;
  for (const item of series) {{
    ctx.strokeStyle = item.color || '#111';
    ctx.lineWidth = item.width || 1.5;
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < item.x.length && i < item.y.length; i++) {{
      const x = item.x[i], y = item.y[i];
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
      if (!started) {{ ctx.moveTo(sx(x), sy(y)); started = true; }}
      else ctx.lineTo(sx(x), sy(y));
    }}
    ctx.stroke();
    ctx.fillStyle = '#111';
    ctx.fillText(item.label || '', legendX, padT + 14);
    ctx.fillStyle = item.color || '#111';
    ctx.fillRect(legendX + ctx.measureText(item.label || '').width + 5, padT + 5, 18, 3);
    legendX += Math.max(90, ctx.measureText(item.label || '').width + 34);
  }}
  ctx.fillStyle = '#111';
  ctx.font = '12px Arial';
  ctx.fillText(options.xlabel || '', Math.round(w / 2) - 32, h - 8);
  ctx.save();
  ctx.translate(14, Math.round(h / 2) + 38);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText(options.ylabel || '', 0, 0);
  ctx.restore();
  ctx.fillText(`${{xMin.toPrecision(4)}}`, padL, h - 10);
  ctx.fillText(`${{xMax.toPrecision(4)}}`, w - padR - 60, h - 10);
  ctx.fillText(`${{yMax.toPrecision(4)}}`, 18, padT + 5);
  ctx.fillText(`${{yMin.toPrecision(4)}}`, 18, h - padB);
}}
function drawScopePreview(payload) {{
  const meta = document.getElementById('scopePreviewMeta');
  const trace = payload && payload.trace;
  if (!trace || !trace.t || !trace.t.length) {{
    drawXYChart('scopePreviewCanvas', [], {{emptyText: 'no scope trace returned'}});
    meta.textContent = 'no trace';
    return;
  }}
  const t = trace.t.map(Number);
  const ch1 = trace.ch1.map(Number);
  const ch2 = trace.ch2.map(Number);
  const tMs = t.map(x => x * 1e3);
  drawXYChart('scopePreviewCanvas', [
    {{x: tMs, y: ch1, label: payload.input1 || 'ch1', color: '#168a2f'}},
    {{x: tMs, y: ch2, label: payload.input2 || 'ch2', color: '#c62828'}},
  ], {{xlabel: 'time (ms)', ylabel: 'voltage (V)', emptyText: 'no scope trace returned'}});
  meta.textContent = `${{payload.input1}} / ${{payload.input2}}, n=${{payload.n}}, shown=${{t.length}}, duration=${{Number(payload.duration || 0).toFixed(4)}} s`;
}}
const previewBusy = {{scope: false, spectrum: false, network: false}};
async function refreshScopePreview() {{
  if (previewBusy.scope) return;
  previewBusy.scope = true;
  const el = document.getElementById('targetResult');
  const meta = document.getElementById('scopePreviewMeta');
  meta.textContent = 'capturing...';
  try {{
    const payload = await api('/api/scope/single', {{...instrumentBody(), timeout_s: 5, max_points: 1500}});
    drawScopePreview(payload);
    renderCompactResult(el, {{ok:true, json:{{message:'scope preview refreshed', input1:payload.input1, input2:payload.input2, n:payload.n}}}});
  }} catch(e) {{
    meta.textContent = 'capture failed';
    renderErrorResult(el, e);
  }} finally {{
    previewBusy.scope = false;
  }}
}}
const previewTimers = {{scope: null}};
const previewIntervals = {{scope: 1000}};
const previewRefreshers = {{scope: refreshScopePreview}};
function togglePreviewLive(kind) {{
  const checkbox = document.getElementById(`${{kind}}Live`);
  if (previewTimers[kind]) {{
    clearInterval(previewTimers[kind]);
    previewTimers[kind] = null;
  }}
  if (checkbox && checkbox.checked) {{
    previewRefreshers[kind]();
    previewTimers[kind] = setInterval(() => {{
      if (document.hidden) return;
      previewRefreshers[kind]();
    }}, previewIntervals[kind]);
  }}
}}
setSelectValue('rpType', defaults.rpType);
setSelectValue('laserType', defaults.laserType);
setSelectValue('scopeType', defaults.scopeType);
setSelectValue('experimentMode', inferExperimentMode(defaults.laserType));
document.getElementById('experimentMode').addEventListener('change', () => applyExperimentMode(true));
document.getElementById('laserType').addEventListener('change', () => {{
  const port = document.getElementById('laserPortInput');
  if (document.getElementById('laserType').value === 'weiyuan' && (!port.value.trim() || port.value.trim().toUpperCase() === 'COM3')) {{
    port.value = 'COM5';
  }}
  setSelectValue('experimentMode', inferExperimentMode(document.getElementById('laserType').value));
  applyExperimentMode(false);
}});
for (const id of ['rpType','bridgeBaseInput','rpHostInput','topticaHostInput','laserPortInput','weiyuanSlaveInput','scopeType','scopeResourceInput']) {{
  document.getElementById(id).addEventListener('change', updateInstrumentHeader);
  document.getElementById(id).addEventListener('input', updateInstrumentHeader);
}}
applyExperimentMode(false);
if (!document.body.dataset.task) {{
  setTask('settings');
}}
refreshHealth();
renderTarget();
</script>
</body>
</html>"""


def make_handler(args: argparse.Namespace) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *values: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            try:
                if parsed.path == "/":
                    html_response(
                        self,
                        page(
                            args.default_cavity,
                            args.bridge_base,
                            args.rp_host,
                            args.laser_type,
                            args.toptica_host,
                            large_scan_laser_port=args.large_scan_laser_port,
                            scope_type=args.scope_type,
                            scope_resource=args.scope_resource,
                        ),
                    )
                elif parsed.path == "/api/health":
                    flat_qs = {key: values[0] for key, values in qs.items()}
                    config = instrument_config_from_mapping(flat_qs, args)
                    health = check_instruments(
                        config,
                        args.toptica_python,
                        getattr(args, "device_profile", {}),
                    )
                    payload = {
                        "ok": bool(health.get("ok")),
                        "bridge": health["instruments"].get("red_pitaya", {}).get("bridge"),
                        "bridge_base": config["bridge_base"],
                        "rp_host": config["rp_host"],
                        "laser_type": config["laser_type"],
                        "toptica_host": config["toptica_host"],
                        "laser_port": config["laser_port"],
                        "weiyuan_slave": config["weiyuan_slave"],
                        "toptica_python": str(args.toptica_python),
                        "scope_type": config["scope_type"],
                        "scope_resource": config["scope_resource"],
                        "instruments": health["instruments"],
                    }
                    json_response(self, 200, payload)
                elif parsed.path == "/api/cavity":
                    cavity_dir = qs.get("cavity_dir", [""])[0]
                    if not cavity_dir:
                        raise ValueError("Select a cavity directory first")
                    payload = load_cavity(cavity_dir)
                    json_response(self, 200, payload)
                elif parsed.path == "/api/large_scan/status":
                    cavity_dir = qs.get("cavity_dir", [""])[0]
                    if not cavity_dir:
                        raise ValueError("Select a cavity directory first")
                    payload = large_scan_status(cavity_dir)
                    json_response(self, 200, payload)
                else:
                    json_response(self, 404, {"ok": False, "error": "unknown route"})
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": repr(exc)})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                body = read_json_body(self)
                if parsed.path == "/api/safe_off":
                    config = instrument_config_from_mapping(body, args)
                    if config["rp_type"] == "none":
                        payload = {"ok": True, "json": {"message": "RP / PyRPL not selected; safe-off skipped"}}
                    else:
                        payload = safe_off(config["bridge_base"])
                elif parsed.path == "/api/bridge/action":
                    payload = run_bridge_action(body, args)
                elif parsed.path == "/api/weiyuan/action":
                    payload = run_weiyuan_action(body)
                elif parsed.path == "/api/coupling/standard":
                    payload = run_standard_auto_coupling(body, args)
                elif parsed.path == "/api/scope/single":
                    payload = capture_scope_preview(body, args)
                elif parsed.path == "/api/sensitivity/settings":
                    payload = sensitivity_settings(body, args)
                elif parsed.path == "/api/sensitivity/acquire":
                    payload = run_sensitivity_acquisition(body, args)
                elif parsed.path == "/api/sensitivity/cancel":
                    payload = cancel_sensitivity_acquisition(body, args)
                elif parsed.path == "/api/pick_folder":
                    fallback_dir = args.default_cavity.parent if args.default_cavity is not None else Path.cwd()
                    payload = pick_folder(body.get("initial_dir"), fallback_dir)
                elif parsed.path == "/api/power/update":
                    payload = update_power_and_card(body)
                elif parsed.path == "/api/target/dry_run":
                    command = target_args(body, args) + ["--dry-run"]
                    payload = run_script(command, timeout_s=30.0)
                elif parsed.path == "/api/target/move":
                    command = target_args(body, args) + ["--move-only"]
                    payload = run_script(command, timeout_s=120.0)
                elif parsed.path == "/api/large_scan/run":
                    command, timeout_s = large_scan_command(body, args)
                    config = instrument_config_from_mapping(body, args)
                    payload = run_large_scan_script(
                        command,
                        timeout_s=timeout_s,
                        bridge_base=config["bridge_base"],
                        laser_port=config["laser_port"],
                        scope_resource=config["scope_resource"],
                    )
                elif parsed.path == "/api/large_scan/stop":
                    config = instrument_config_from_mapping(body, args)
                    payload = stop_large_scan_and_restore(
                        config["bridge_base"],
                        laser_port=config["laser_port"],
                        scope_resource=config["scope_resource"],
                    )
                elif parsed.path == "/api/lock/restore_sweep":
                    config = instrument_config_from_mapping(body, args)
                    if config["rp_type"] == "none":
                        payload = {"ok": True, "json": {"message": "RP / PyRPL not selected; restore skipped"}}
                    else:
                        restored = restore_lock_sweep(config["bridge_base"])
                        payload = {"ok": bool(restored.get("ok")), "json": restored}
                elif parsed.path == "/api/lock/current":
                    config = instrument_config_from_mapping(body, args)
                    if config["rp_type"] == "none":
                        raise ValueError("Current-mode lock requires RP / PyRPL bridge")
                    if config["laser_type"] in {"toptica_tcp", "toptica_serial"}:
                        laser_connection = "serial" if config["laser_type"] == "toptica_serial" else "tcp"
                        command = [
                            str(args.toptica_python),
                            str(DEFAULT_LOCK_SCRIPT),
                            "--base",
                            config["bridge_base"],
                            "--host",
                            config["toptica_host"],
                            "--laser-connection",
                            laser_connection,
                            "--laser-port",
                            config["laser_port"],
                        ]
                    elif config["laser_type"] == "weiyuan":
                        command = [
                            package_python_executable(),
                            str(DEFAULT_WEIYUAN_LOCK_SCRIPT),
                            "--base",
                            config["bridge_base"],
                            "--laser-port",
                            config["laser_port"],
                            "--slave",
                            config["weiyuan_slave"],
                        ]
                    else:
                        raise ValueError("Current-mode lock requires TOPTICA or 微源光子 laser")
                    payload = run_script(command, timeout_s=180.0)
                else:
                    payload = {"ok": False, "error": "unknown route"}
                json_response(self, 200 if payload.get("ok") else 400, payload)
            except Exception as exc:
                json_response(self, 400, {"ok": False, "error": repr(exc)})

    return Handler


def main() -> int:
    args = parse_args()
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), make_handler(args))
    print(f"Microcavity control panel listening on http://{args.listen_host}:{args.listen_port}", flush=True)
    print(f"Bridge base: {args.bridge_base}", flush=True)
    print(f"TOPTICA host: {args.toptica_host}", flush=True)
    if args.auto_start_bridge:
        headless = not args.auto_start_bridge_gui
        print(f"Auto-starting PyRPL bridge headless={headless}", flush=True)
        try:
            config = instrument_config_from_mapping({}, args)
            result = start_or_restart_bridge(config, args, headless=headless)
            print(
                "Auto-start bridge result: "
                f"ok={bool(result.get('ok'))} "
                f"message={result.get('message') or result.get('error')}",
                flush=True,
            )
        except Exception as exc:
            print(f"Auto-start bridge failed: {exc!r}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
