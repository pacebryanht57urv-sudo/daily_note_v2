"""Local dashboard for stable microcavity Q/lock operations.

This dashboard intentionally stays thin: it does not own PyRPL or TOPTICA
state. It calls the existing bridge and locking scripts so the operational
logic stays in one place.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
DEFAULT_LOCK_SCRIPT = SCRIPT_DIR / "current_mode_fast_lock.py"
DEFAULT_WEIYUAN_LOCK_SCRIPT = SCRIPT_DIR / "weiyuan_current_mode_lock.py"
DEFAULT_BEST_Q_SCRIPT = SCRIPT_DIR / "lock_best_q_mode.py"
LARGE_SCAN_DIR = REPO_ROOT / "workspace" / "scripts" / "microcavity_large_scan"
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

DEFAULT_BRIDGE_CONFIG = "try_bridge_safe"
DEFAULT_BRIDGE_LISTEN_HOST = "127.0.0.1"
DEFAULT_BRIDGE_LISTEN_PORT = 7870
DEFAULT_RP_F0CB0D_EXTERNAL_GAIN_DB = 23.0
DEFAULT_CONFIG_FILE = SCRIPT_DIR / "config.local.json"

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

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def load_local_module(module_name: str) -> Any:
    module_path = SCRIPT_DIR / f"{module_name}.py"
    if not module_path.exists():
        raise FileNotFoundError(
            f"Missing {module_path.name} in {SCRIPT_DIR}. "
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
    parser.set_defaults(device_profile=defaults["device_profile"])
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


def bridge_set(base: str, param: str, value: str | int | float) -> dict[str, Any]:
    from urllib.parse import quote

    return request_json(f"{base}/set?param={quote(param)}&value={quote(str(value))}", timeout=5.0)


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
    return {
        "ok": ok,
        "message": "lock sweep restored; PID disabled and ASG ramp sent to out2",
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
    local_paths = [str(SCRIPT_DIR)]
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


def bridge_runtime_status(bridge_base: str) -> dict[str, Any]:
    with ACTIVE_BRIDGE_LOCK:
        proc = ACTIVE_BRIDGE.get("proc")
        started_at = ACTIVE_BRIDGE.get("started_at")
        command = ACTIVE_BRIDGE.get("command")
        host = ACTIVE_BRIDGE.get("rp_host")
        external_gain_db = ACTIVE_BRIDGE.get("external_gain_db")
        device_profile = ACTIVE_BRIDGE.get("device_profile")
        headless = ACTIVE_BRIDGE.get("headless")
    process = {
        "managed": proc is not None,
        "running": bool(proc is not None and proc.poll() is None),
        "pid": proc.pid if proc is not None else None,
        "returncode": proc.poll() if proc is not None else None,
        "rp_host": host,
        "external_gain_db": external_gain_db,
        "device_profile": device_profile,
        "headless": headless,
        "elapsed_s": time.time() - float(started_at) if started_at else None,
        "command": command,
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


def bridge_command(config: dict[str, str], args: argparse.Namespace, external_gain_db: float, *, headless: bool) -> list[str]:
    listen_host, listen_port = bridge_listen_address(config["bridge_base"])
    device_profile = getattr(args, "device_profile", {}) or {}
    command = [
        sys.executable,
        str(SCRIPT_DIR / "pyrpl_live_bridge.py"),
        "--config",
        DEFAULT_BRIDGE_CONFIG,
        "--hostname",
        config["rp_host"],
        "--listen-host",
        listen_host,
        "--listen-port",
        str(listen_port),
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
    command = bridge_command(config, args, external_gain_db, headless=headless)
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    with ACTIVE_BRIDGE_LOCK:
        ACTIVE_BRIDGE.update(
            {
                "proc": proc,
                "command": command,
                "started_at": time.time(),
                "rp_host": config["rp_host"],
                "external_gain_db": external_gain_db,
                "device_profile": device_profile,
                "headless": headless,
            }
        )

    health = {"ok": False, "error": "not checked yet"}
    for _ in range(20):
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
        "external_gain_db": external_gain_db,
        "device_profile": device_profile,
        "headless": headless,
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
        sys.executable,
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
        sys.executable,
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
    .preview-head {{ display: flex; justify-content: space-between; gap: 10px; align-items: center; margin-bottom: 6px; flex-wrap: wrap; }}
    .preview-actions {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
    .preview-actions button {{ margin: 0; }}
    .live-label {{ display: inline-flex; gap: 4px; align-items: center; margin: 0; font-weight: 400; }}
    .live-label input {{ width: auto; }}
    .rp-preview canvas {{ display: block; width: 100%; height: 240px; background: #fff; border: 1px solid #aaa; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th, td {{ border-bottom: 1px solid #ddd; padding: 5px 6px; text-align: left; }}
    th {{ background: #f1f3f5; position: sticky; top: 0; }}
    tr:hover {{ background: #fff7db; }}
    .table-wrap {{ max-height: 380px; overflow: auto; border: 1px solid #ddd; }}
    .muted {{ color: #555; font-size: 12px; }}
    .hidden {{ display: none !important; }}
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
        <label>Cavity directory</label>
        <div class="path-row">
          <input id="cavityDir" value="{default_cavity_text}" placeholder="Choose a cavity directory when needed" />
          <button onclick="browseCavity()">Browse...</button>
        </div>
        <button onclick="loadCavity()">Load cavity</button>
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
      <div class="button-row">
        <button class="target-controls" onclick="dryRunTarget()">Dry-run target</button>
        <button class="primary target-controls" onclick="moveTarget()">Move to target wavelength</button>
        <button onclick="lockCurrent()">Lock current mode</button>
        <button onclick="restoreLockSweep('targetResult')">Restore sweep / PID off</button>
      </div>
      <div id="targetResult" class="status"></div>
      <div class="preview-stack">
        <div class="rp-preview">
          <div class="preview-head">
            <b>Bridge scope preview</b>
            <div class="preview-actions">
              <button onclick="refreshScopePreview()">Single</button>
              <label class="live-label"><input id="scopeLive" type="checkbox" onchange="togglePreviewLive('scope')" /> Live 1 s</label>
              <span id="scopePreviewMeta" class="muted">not captured</span>
            </div>
          </div>
          <canvas id="scopePreviewCanvas" width="900" height="240"></canvas>
        </div>
      </div>
    </section>

    <section class="large-scan-panel">
      <h2>Large-Scan Q</h2>
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
<script>
let cavity = null;
let manualSelectedRow = null;
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
const api = async (path, body=null) => {{
  const opt = body ? {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(body)}} : {{}};
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

  const hostCheck = j && j.host_check;
  if (hostCheck) {{
    addLine(lines, 'RP host', hostCheck.host);
    if (hostCheck.addresses && hostCheck.addresses.length) {{
      lines.push(`resolved: ${{hostCheck.addresses.map(item => `${{item.family}} ${{item.address}}`).join(', ')}}`);
    }}
    if (hostCheck.warnings && hostCheck.warnings.length) lines.push(`warning: ${{hostCheck.warnings.join('; ')}}`);
    addLine(lines, 'external_gain if started', hostCheck.external_gain_db_if_started !== undefined ? `${{hostCheck.external_gain_db_if_started}} dB` : null);
  }}

  const bridgeStatus = j && j.status;
  if (bridgeStatus) {{
    const process = bridgeStatus.process || {{}};
    const health = bridgeStatus.health || {{}};
    const bridge = health.bridge || {{}};
    const correction = health.spectrum_power_correction || {{}};
    lines.push(`bridge http: ${{bridgeStatus.ok ? 'ok' : 'not responding'}}`);
    lines.push(`bridge process: ${{process.managed ? 'dashboard-managed' : 'external/none'}}, ${{process.running ? 'running' : 'not running'}}${{process.pid ? ', pid=' + process.pid : ''}}`);
    addLine(lines, 'bridge RP host', process.rp_host);
    addLine(lines, 'bridge external_gain', process.external_gain_db !== null && process.external_gain_db !== undefined ? `${{process.external_gain_db}} dB` : null);
    addLine(lines, 'live bridge pid', bridge.pid);
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
  el.textContent = 'moving wavelength...';
  try {{ renderCompactResult(el, await api('/api/target/move', targetBody())); }}
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
                elif parsed.path == "/api/scope/single":
                    payload = capture_scope_preview(body, args)
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
                            sys.executable,
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
