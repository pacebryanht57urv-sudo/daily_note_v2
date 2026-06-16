#!/usr/bin/env python3
"""Acquire a TOPTICA DLC PRO large scan with an R&S RTE oscilloscope.

Default channel map for a microcavity large-scan measurement:
CH1 = large-scan trigger, CH2 = chip transmission PD, CH3 = MZI PD,
CH4 = fine-scan trigger (not used here).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np

from data_paths import CAMPAIGN_ENV, CHIP_ENV, DATA_ROOT_ENV, default_campaign, default_chip, default_cavity_dir


@dataclass
class ScanConfig:
    campaign: str
    chip: str
    die: str
    cavity: str
    start_nm: float
    stop_nm: float
    speed_nm_s: float
    laser_port: str
    scope_resource: str
    visa_backend: str
    scope_timeout_ms: int
    sample_rate_hz: float
    record_seconds: float
    pre_trigger_seconds: float
    post_trigger_seconds: float
    trigger_channel: int
    trans_channel: int
    mzi_channel: int
    save_trigger: bool
    trigger_level_v: float
    trigger_slope: str
    disable_arc_factor_for_large_scan: bool
    restore_fine_scope_after_scan: bool
    fine_arc_factor_v_per_v: float
    fine_center_nm: float
    arm_delay_s: float
    post_scan_wait_s: float
    laser_settle_timeout_s: float
    cycle_emission_before_scan: bool
    cycle_emission_after_scan: bool
    emission_off_seconds: float
    emission_on_settle_seconds: float
    pc_voltage_v: float | None
    pc_voltage_tolerance_v: float
    restore_wavelength_mode: str
    storage_format: str
    output_dir: str


class TopticaDlcPro:
    def __init__(self, port: str, timeout_s: float = 2.0) -> None:
        try:
            import serial  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Missing dependency pyserial. Install with: python -m pip install pyserial"
            ) from exc

        self._serial = serial.Serial(port, baudrate=115200, timeout=timeout_s)

    def close(self) -> None:
        if self._serial and self._serial.is_open:
            self._serial.close()

    def query(self, command: str) -> str:
        command_clean = command.strip()
        if not command.endswith("\r\n"):
            command += "\r\n"
        self._serial.reset_input_buffer()
        self._serial.write(command.encode("ascii"))
        deadline = time.monotonic() + float(self._serial.timeout or 2.0)
        responses: list[str] = []
        while time.monotonic() < deadline:
            line = self._serial.readline().decode("ascii", errors="replace").strip()
            if not line:
                continue
            if line == ">" or line.startswith(">"):
                if responses:
                    break
                continue
            if line == command_clean or command_clean in line:
                continue
            responses.append(line)
        if not responses:
            raise TimeoutError(f"No TOPTICA response for command: {command_clean}")
        return responses[-1]

    def get(self, name: str) -> str:
        return self.query(f"(param-ref '{name})")

    def set(self, expression: str) -> str:
        response = self.query(f"(param-set! '{expression})")
        if response.startswith("Error:"):
            raise RuntimeError(f"TOPTICA param-set failed for {expression!r}: {response}")
        return response

    def command(self, expression: str) -> str:
        return self.query(f"(exec '{expression})")

    def wavelength_nm(self) -> float:
        return float(self.get("laser1:ctl:wavelength-act"))

    def pc_voltage_set_v(self) -> float:
        return float(self.get("laser1:dl:pc:voltage-set"))

    def pc_voltage_act_v(self) -> float:
        return float(self.get("laser1:dl:pc:voltage-act"))

    def set_pc_voltage_v(self, voltage_v: float) -> None:
        self.set(f"laser1:dl:pc:voltage-set {voltage_v:.9g}")

    def set_wavelength_nm(self, wavelength_nm: float) -> None:
        self.set(f"laser1:ctl:wavelength-set {wavelength_nm:.9f}")

    def move_to_wavelength(self, wavelength_nm: float, timeout_s: float, tolerance_nm: float = 0.5) -> None:
        self.set_wavelength_nm(wavelength_nm)
        deadline = time.monotonic() + timeout_s
        last = math.nan
        while time.monotonic() < deadline:
            last = self.wavelength_nm()
            if abs(last - wavelength_nm) <= tolerance_nm:
                return
            time.sleep(1.0)
        raise TimeoutError(
            f"Laser did not reach {wavelength_nm:.3f} nm within {timeout_s:.1f} s; "
            f"last readback was {last:.6g} nm."
        )

    def configure_wide_scan(self, start_nm: float, stop_nm: float, speed_nm_s: float) -> None:
        self.set("laser1:wide-scan:output-channel 79")
        self.set(f"laser1:wide-scan:scan-begin {start_nm:.9f}")
        self.set(f"laser1:wide-scan:scan-end {stop_nm:.9f}")
        self.set(f"laser1:wide-scan:speed {speed_nm_s:.9f}")

    def start_wide_scan(self) -> None:
        self.command("laser1:wide-scan:start")

    def set_arc_factor_enabled(self, enabled: bool) -> None:
        self.set(f"laser1:dl:pc:external-input:enabled {'#t' if enabled else '#f'}")

    def set_arc_factor(self, factor_v_per_v: float) -> None:
        self.set(f"laser1:dl:pc:external-input:factor {factor_v_per_v:.9g}")

    def configure_fine_scan_arc_factor(self, factor_v_per_v: float = 25.0) -> None:
        self.set_arc_factor(factor_v_per_v)
        self.set_arc_factor_enabled(True)

    def set_emission_enabled(self, enabled: bool) -> None:
        self.set(f"laser1:dl:cc:enabled {'#t' if enabled else '#f'}")


class RohdeSchwarzRte:
    def __init__(self, resource: str, visa_backend: str = "@py", timeout_ms: int = 120000) -> None:
        try:
            import pyvisa  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Missing dependency pyvisa. Install with: python -m pip install pyvisa"
            ) from exc

        self._rm = pyvisa.ResourceManager(visa_backend)
        self._inst = self._rm.open_resource(resource)
        self._inst.timeout = timeout_ms
        self._inst.chunk_size = 1024 * 1024
        if "SOCKET" in resource.upper():
            self._inst.read_termination = "\n"
            self._inst.write_termination = "\n"

    def close(self) -> None:
        try:
            self._inst.close()
        finally:
            self._rm.close()

    def write(self, command: str) -> None:
        self._inst.write(command)

    def query(self, command: str) -> str:
        return str(self._inst.query(command)).strip()

    def idn(self) -> str:
        return self.query("*IDN?")

    def opc(self) -> None:
        self.query("*OPC?")

    def stop(self) -> None:
        self.write("STOP")

    def run(self) -> None:
        self.write("RUN")

    def single(self) -> None:
        self.write("SING")

    def configure_large_scan(
        self,
        *,
        record_seconds: float,
        sample_rate_hz: float,
        trigger_channel: int,
        trigger_level_v: float,
        trigger_slope: str,
        trans_channel: int,
        mzi_channel: int,
        save_trigger: bool,
    ) -> None:
        channels_to_enable = {trans_channel, mzi_channel}
        if save_trigger:
            channels_to_enable.add(trigger_channel)
        for channel in range(1, 5):
            state = "ON" if channel in channels_to_enable else "OFF"
            self.write(f"CHANnel{channel}:STATe {state}")

        self.write(f"TRIGger:SOURce:SELect CHAN{trigger_channel}")
        self.write("TRIGger:MODE NORMal")
        self.write(f"TRIGger:LEVel{trigger_channel}:VALue {trigger_level_v:.9g}")
        self.write(f"TRIGger:EDGE:SLOPe {trigger_slope.upper()}")
        self.write(f"TIMebase:SCALe {record_seconds / 10.0:.9g}")
        self.write(f"ACQuire:SRReal {sample_rate_hz:.9g}")
        self.write(f"ACQuire:POINts:VALue {int(round(record_seconds * sample_rate_hz))}")
        self.write("TIMebase:REFerence 50")
        self.write("TIMebase:HORizontal:POSition 0")
        self.write("FORMat:DATA REAL,32")
        self.write("FORMat:BORDer LSBF")
        self.opc()

    def configure_fine_scan_idle(
        self,
        *,
        trigger_channel: int = 4,
        trigger_level_v: float = 0.0,
        trigger_slope: str = "negative",
        trigger_mode: str = "auto",
        time_scale_s_per_div: float = 1e-3,
    ) -> None:
        self.stop()
        self.write("CHANnel1:STATe OFF")
        self.write(f"CHANnel{trigger_channel}:STATe ON")
        self.write(f"TRIGger:SOURce:SELect CHAN{trigger_channel}")
        self.write(f"TRIGger:MODE {trigger_mode.upper()}")
        self.write(f"TRIGger:LEVel{trigger_channel}:VALue {trigger_level_v:.9g}")
        self.write(f"TRIGger:EDGE:SLOPe {trigger_slope.upper()}")
        self.write("TIMebase:HORizontal:POSition 0")
        self.write(f"TIMebase:SCALe {time_scale_s_per_div:.9g}")
        self.run()
        self.opc()

    def read_channel(self, channel: int) -> tuple[list[float], list[float], dict[str, float]]:
        chan = f"CHAN{channel}"
        self.write("FORMat:DATA REAL,32")
        self.write("FORMat:BORDer LSBF")
        self.write(f"{chan}:DATA:POINts MAX")
        header_raw = self.query(f"{chan}:DATA:HEAD?")
        values = self._inst.query_binary_values(
            f"{chan}:DATA?",
            datatype="f",
            is_big_endian=False,
            expect_termination=False,
            container=list,
        )
        sample_rate = float(self.query("ACQuire:SRATe?"))
        values = [float(v) for v in values]
        header_parts = [float(part) for part in header_raw.replace(";", ",").split(",") if part.strip()]
        if len(header_parts) >= 3:
            x_start, x_stop, header_points = header_parts[:3]
            if len(values) > 1:
                step = (x_stop - x_start) / (len(values) - 1)
                time_axis = [x_start + i * step for i in range(len(values))]
            else:
                time_axis = [x_start]
        else:
            x_start = 0.0
            x_stop = (len(values) - 1) / sample_rate if values else 0.0
            header_points = float(len(values))
            time_axis = build_time_axis(len(values), sample_rate)
        header = {
            "x_start_s": x_start,
            "x_stop_s": x_stop,
            "header_points": header_points,
            "sample_rate_hz": sample_rate,
            "raw": header_raw,
        }
        return time_axis, values, header


def write_csv(path: Path, columns: dict[str, list[float]]) -> int:
    lengths = [len(values) for values in columns.values()]
    if not lengths:
        raise ValueError("No columns to save.")
    n = min(lengths)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns.keys())
        for i in range(n):
            writer.writerow([values[i] for values in columns.values()])
    return n


def write_npz(path: Path, columns: dict[str, list[float]], *, compressed: bool) -> int:
    lengths = [len(values) for values in columns.values()]
    if not lengths:
        raise ValueError("No columns to save.")
    n = min(lengths)
    if "time_s" not in columns:
        raise ValueError("NPZ storage requires a time_s column for metadata.")
    time_s = columns["time_s"][:n]
    arrays: dict[str, np.ndarray] = {}
    for name, values in columns.items():
        if name == "time_s":
            continue
        arrays[name] = np.asarray(values[:n], dtype=np.float32)
    t0 = float(time_s[0]) if n else 0.0
    t1 = float(time_s[-1]) if n else 0.0
    sample_rate = float((n - 1) / (t1 - t0)) if n > 1 and t1 != t0 else math.nan
    arrays["time_start_s"] = np.asarray(t0, dtype=np.float64)
    arrays["time_stop_s"] = np.asarray(t1, dtype=np.float64)
    arrays["sample_rate_hz"] = np.asarray(sample_rate, dtype=np.float64)
    arrays["rows"] = np.asarray(n, dtype=np.int64)
    if compressed:
        np.savez_compressed(path, **arrays)
    else:
        np.savez(path, **arrays)
    return n


def build_time_axis(n: int, sample_rate_hz: float) -> list[float]:
    dt = 1.0 / sample_rate_hz
    return [i * dt for i in range(n)]


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--campaign",
        default=default_campaign(),
        help=f"Campaign path under ${DATA_ROOT_ENV}/experiments. Defaults to ${CAMPAIGN_ENV} or wafer_measuement/Batch_260515.",
    )
    parser.add_argument("--chip", default=default_chip(), help=f"Chip/sample id. Defaults to ${CHIP_ENV} or chip7.")
    parser.add_argument("--die", default="die1-1")
    parser.add_argument("--cavity", default="c1")
    parser.add_argument("--start-nm", type=float, default=1530.0)
    parser.add_argument("--stop-nm", type=float, default=1570.0)
    parser.add_argument("--speed-nm-s", type=float, default=2.0)
    parser.add_argument("--laser-port", default="COM3")
    parser.add_argument("--scope-resource", default="TCPIP::192.168.1.8::INSTR")
    parser.add_argument("--visa-backend", default="@py")
    parser.add_argument(
        "--scope-timeout-ms",
        type=int,
        default=120000,
        help="VISA timeout for large binary waveform reads from the oscilloscope.",
    )
    parser.add_argument("--sample-rate-hz", type=float, default=200_000.0)
    parser.add_argument(
        "--record-seconds",
        type=float,
        default=20.0,
        help="Total oscilloscope window centered on CH1 trigger; default 20 s means 10 s before and 10 s after trigger.",
    )
    parser.add_argument("--trigger-channel", type=int, default=1)
    parser.add_argument("--trans-channel", type=int, default=2)
    parser.add_argument("--mzi-channel", type=int, default=3)
    parser.add_argument("--no-save-trigger", action="store_true")
    parser.add_argument("--trigger-level-v", type=float, default=1.0)
    parser.add_argument("--trigger-slope", choices=["positive", "negative"], default="positive")
    parser.add_argument("--no-restore-fine-scope", action="store_true")
    parser.add_argument("--fine-trigger-level-v", type=float, default=0.0)
    parser.add_argument("--fine-arc-factor-v-per-v", type=float, default=25.0)
    parser.add_argument("--fine-center-nm", type=float, default=1550.0)
    parser.add_argument("--keep-arc-factor-during-large-scan", action="store_true")
    parser.add_argument("--arm-delay-s", type=float, default=0.5)
    parser.add_argument("--post-scan-wait-s", type=float, default=2.0)
    parser.add_argument("--laser-settle-timeout-s", type=float, default=90.0)
    parser.add_argument(
        "--cycle-emission-before-scan",
        action="store_true",
        help="At the scan start wavelength, turn TOPTICA emission off, wait, turn it on, wait, then start the large scan.",
    )
    parser.add_argument(
        "--cycle-emission-after-scan",
        action="store_true",
        help="After waveform readout, turn TOPTICA emission off, wait, turn it on, then restore the requested wavelength.",
    )
    parser.add_argument("--emission-off-seconds", type=float, default=2.0)
    parser.add_argument("--emission-on-settle-seconds", type=float, default=2.0)
    parser.add_argument(
        "--pc-voltage-v",
        type=float,
        default=75.0,
        help="Set TOPTICA PC piezo voltage before acquisition; pass --skip-pc-voltage-set to leave it unchanged.",
    )
    parser.add_argument("--pc-voltage-tolerance-v", type=float, default=1.0)
    parser.add_argument("--skip-pc-voltage-set", action="store_true")
    parser.add_argument(
        "--restore-wavelength-mode",
        choices=["fine-center", "initial", "none"],
        default="initial",
        help=(
            "Wavelength restore target after the large scan. 'fine-center' uses --fine-center-nm; "
            "'initial' returns to the laser wavelength read before moving to the large-scan start; "
            "'none' leaves the laser at the post-scan state."
        ),
    )
    parser.add_argument(
        "--storage-format",
        choices=["csv", "npz", "npz-compressed", "both"],
        default="npz",
        help="Raw data storage. npz stores float32 channel arrays and reconstructs time from metadata.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help=f"Output directory. Defaults to ${DATA_ROOT_ENV}/experiments/.../results/<chip>/<die>/<cavity>.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned settings without connecting.")
    parser.add_argument(
        "--laser-prepare-only",
        action="store_true",
        help="Configure TOPTICA wide scan and move to start wavelength, then exit before touching the scope.",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    scan_time = abs(args.stop_nm - args.start_nm) / args.speed_nm_s
    record_seconds = args.record_seconds
    if record_seconds is None:
        record_seconds = scan_time
    pre_trigger_seconds = record_seconds / 2.0
    post_trigger_seconds = record_seconds / 2.0

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        try:
            output_dir = default_cavity_dir(args.chip, args.die, args.cavity, campaign=args.campaign)
        except RuntimeError as exc:
            if not args.dry_run:
                raise SystemExit(str(exc)) from exc
            output_dir = Path(f"<set {DATA_ROOT_ENV} or pass --output-dir>") / args.chip / args.die / args.cavity
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    basename = f"large_scan_{timestamp}_{args.start_nm:g}-{args.stop_nm:g}nm"
    csv_path = output_dir / f"{basename}.csv"
    npz_path = output_dir / f"{basename}.npz"
    meta_path = output_dir / f"{basename}.json"

    config = ScanConfig(
        campaign=args.campaign,
        chip=args.chip,
        die=args.die,
        cavity=args.cavity,
        start_nm=args.start_nm,
        stop_nm=args.stop_nm,
        speed_nm_s=args.speed_nm_s,
        laser_port=args.laser_port,
        scope_resource=args.scope_resource,
        visa_backend=args.visa_backend,
        scope_timeout_ms=args.scope_timeout_ms,
        sample_rate_hz=args.sample_rate_hz,
        record_seconds=record_seconds,
        pre_trigger_seconds=pre_trigger_seconds,
        post_trigger_seconds=post_trigger_seconds,
        trigger_channel=args.trigger_channel,
        trans_channel=args.trans_channel,
        mzi_channel=args.mzi_channel,
        save_trigger=not args.no_save_trigger,
        trigger_level_v=args.trigger_level_v,
        trigger_slope=args.trigger_slope,
        disable_arc_factor_for_large_scan=not args.keep_arc_factor_during_large_scan,
        restore_fine_scope_after_scan=not args.no_restore_fine_scope,
        fine_arc_factor_v_per_v=args.fine_arc_factor_v_per_v,
        fine_center_nm=args.fine_center_nm,
        arm_delay_s=args.arm_delay_s,
        post_scan_wait_s=args.post_scan_wait_s,
        laser_settle_timeout_s=args.laser_settle_timeout_s,
        cycle_emission_before_scan=args.cycle_emission_before_scan,
        cycle_emission_after_scan=args.cycle_emission_after_scan,
        emission_off_seconds=args.emission_off_seconds,
        emission_on_settle_seconds=args.emission_on_settle_seconds,
        pc_voltage_v=None if args.skip_pc_voltage_set else args.pc_voltage_v,
        pc_voltage_tolerance_v=args.pc_voltage_tolerance_v,
        restore_wavelength_mode=args.restore_wavelength_mode,
        storage_format=args.storage_format,
        output_dir=str(output_dir),
    )

    print(json.dumps(asdict(config), indent=2, ensure_ascii=False))
    if args.dry_run:
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "config": asdict(config),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "csv_path": str(csv_path) if args.storage_format in {"csv", "both"} else None,
        "npz_path": str(npz_path) if args.storage_format in {"npz", "npz-compressed", "both"} else None,
        "phase_seconds": {},
    }

    def mark_phase(name: str, started_at: float) -> None:
        seconds = time.perf_counter() - started_at
        meta["phase_seconds"][name] = round(seconds, 3)
        print(f"Phase {name}: {seconds:.3f} s")

    laser: TopticaDlcPro | None = None
    scope: RohdeSchwarzRte | None = None
    try:
        phase_t = time.perf_counter()
        print("Connecting to TOPTICA DLC PRO...")
        laser = TopticaDlcPro(args.laser_port)
        initial_wavelength_nm = laser.wavelength_nm()
        meta["laser_initial_readback_nm"] = initial_wavelength_nm
        print(f"Initial laser readback before large-scan setup: {initial_wavelength_nm:.9f} nm")
        meta["pc_voltage_before_set_v"] = laser.pc_voltage_set_v()
        meta["pc_voltage_before_act_v"] = laser.pc_voltage_act_v()
        if config.pc_voltage_v is not None:
            print(f"Setting TOPTICA PC piezo voltage to {config.pc_voltage_v:.6g} V...")
            laser.set_pc_voltage_v(config.pc_voltage_v)
            meta["pc_voltage_target_v"] = config.pc_voltage_v
            meta["pc_voltage_after_set_v"] = laser.pc_voltage_set_v()
            meta["pc_voltage_after_act_v"] = laser.pc_voltage_act_v()
            pc_error_v = abs(float(meta["pc_voltage_after_act_v"]) - config.pc_voltage_v)
            meta["pc_voltage_error_v"] = pc_error_v
            meta["pc_voltage_ok"] = pc_error_v <= config.pc_voltage_tolerance_v
            if not meta["pc_voltage_ok"]:
                raise RuntimeError(
                    "TOPTICA PC piezo voltage readback is outside tolerance: "
                    f"target={config.pc_voltage_v:.6g} V, "
                    f"act={float(meta['pc_voltage_after_act_v']):.6g} V, "
                    f"tolerance={config.pc_voltage_tolerance_v:.6g} V."
                )
        else:
            meta["pc_voltage_target_v"] = None
            meta["pc_voltage_ok"] = True
        if not args.keep_arc_factor_during_large_scan:
            print("Disabling TOPTICA external input arc factor for large scan...")
            laser.set_arc_factor_enabled(False)
        mark_phase("laser_connect_pc_arc_setup", phase_t)
        if args.laser_prepare_only:
            phase_t = time.perf_counter()
            print("Configuring laser wide scan...")
            laser.configure_wide_scan(args.start_nm, args.stop_nm, args.speed_nm_s)
            print(f"Moving laser to start wavelength {args.start_nm:g} nm...")
            laser.move_to_wavelength(args.start_nm, args.laser_settle_timeout_s)
            readback = laser.wavelength_nm()
            print(f"Laser readback: {readback:.9f} nm")
            mark_phase("configure_and_move_to_start", phase_t)
            return 0

        phase_t = time.perf_counter()
        print("Connecting to R&S oscilloscope...")
        scope = RohdeSchwarzRte(
            args.scope_resource,
            visa_backend=args.visa_backend,
            timeout_ms=args.scope_timeout_ms,
        )
        meta["scope_idn"] = scope.idn()
        print(f"Scope: {meta['scope_idn']}")
        mark_phase("scope_connect", phase_t)

        phase_t = time.perf_counter()
        print("Configuring laser wide scan...")
        laser.configure_wide_scan(args.start_nm, args.stop_nm, args.speed_nm_s)
        print(f"Moving laser to start wavelength {args.start_nm:g} nm...")
        laser.move_to_wavelength(args.start_nm, args.laser_settle_timeout_s)
        meta["laser_start_readback_nm"] = laser.wavelength_nm()
        mark_phase("configure_and_move_to_start", phase_t)

        if args.cycle_emission_before_scan:
            phase_t = time.perf_counter()
            print("Cycling TOPTICA emission before scan: OFF...")
            laser.set_emission_enabled(False)
            time.sleep(args.emission_off_seconds)
            meta["emission_cycle_off_seconds"] = args.emission_off_seconds
            print("Cycling TOPTICA emission before scan: ON...")
            laser.set_emission_enabled(True)
            time.sleep(args.emission_on_settle_seconds)
            meta["emission_cycle_on_settle_seconds"] = args.emission_on_settle_seconds
            meta["laser_start_readback_after_emission_cycle_nm"] = laser.wavelength_nm()
            mark_phase("pre_emission_cycle", phase_t)

        phase_t = time.perf_counter()
        print("Configuring oscilloscope acquisition and CH1 trigger...")
        scope.stop()
        scope.configure_large_scan(
            record_seconds=record_seconds,
            sample_rate_hz=args.sample_rate_hz,
            trigger_channel=args.trigger_channel,
            trigger_level_v=args.trigger_level_v,
            trigger_slope=args.trigger_slope,
            trans_channel=args.trans_channel,
            mzi_channel=args.mzi_channel,
            save_trigger=not args.no_save_trigger,
        )

        print("Arming oscilloscope single acquisition...")
        scope.single()
        time.sleep(args.arm_delay_s)
        mark_phase("scope_config_arm", phase_t)
        print("Starting laser wide scan...")
        laser.start_wide_scan()
        wait_s = max(scan_time, record_seconds) + args.post_scan_wait_s
        print(f"Waiting {wait_s:.1f} s for scan/acquisition...")
        phase_t = time.perf_counter()
        time.sleep(wait_s)
        mark_phase("scan_wait", phase_t)

        columns: dict[str, list[float]] = {}
        channel_data: dict[str, list[float]] = {}
        first_sample_rate = args.sample_rate_hz
        channel_headers: dict[str, dict[str, float]] = {}
        time_axes: dict[str, list[float]] = {}
        phase_t = time.perf_counter()
        if not args.no_save_trigger:
            print(f"Reading trigger CH{args.trigger_channel}...")
            trigger_time, trigger, trigger_header = scope.read_channel(args.trigger_channel)
            first_sample_rate = trigger_header["sample_rate_hz"]
            time_axes[f"ch{args.trigger_channel}_trigger_v"] = trigger_time
            channel_data[f"ch{args.trigger_channel}_trigger_v"] = trigger
            channel_headers[f"ch{args.trigger_channel}_trigger_v"] = trigger_header

        print(f"Reading transmission CH{args.trans_channel}...")
        trans_time, trans, trans_header = scope.read_channel(args.trans_channel)
        first_sample_rate = trans_header["sample_rate_hz"]
        time_axes[f"ch{args.trans_channel}_trans_v"] = trans_time
        channel_data[f"ch{args.trans_channel}_trans_v"] = trans
        channel_headers[f"ch{args.trans_channel}_trans_v"] = trans_header
        print(f"Reading MZI CH{args.mzi_channel}...")
        mzi_time, mzi, mzi_header = scope.read_channel(args.mzi_channel)
        first_sample_rate = mzi_header["sample_rate_hz"]
        time_axes[f"ch{args.mzi_channel}_mzi_v"] = mzi_time
        channel_data[f"ch{args.mzi_channel}_mzi_v"] = mzi
        channel_headers[f"ch{args.mzi_channel}_mzi_v"] = mzi_header
        mark_phase("scope_readout", phase_t)

        n = min(len(values) for values in channel_data.values())
        first_name = next(iter(channel_data))
        columns["time_s"] = time_axes[first_name][:n]
        for name, values in channel_data.items():
            columns[name] = values[:n]

        rows = 0
        phase_t = time.perf_counter()
        if args.storage_format in {"csv", "both"}:
            rows = write_csv(csv_path, columns)
            print(f"Saved {rows} rows to {csv_path}")
        if args.storage_format in {"npz", "npz-compressed", "both"}:
            rows = write_npz(npz_path, columns, compressed=args.storage_format == "npz-compressed")
            print(f"Saved {rows} rows to {npz_path}")
        mark_phase("save_waveform", phase_t)
        meta["rows"] = rows
        meta["actual_sample_rate_hz"] = first_sample_rate
        meta["channel_lengths"] = {name: len(values) for name, values in channel_data.items()}
        meta["channel_headers"] = channel_headers
        expected_x_start = -pre_trigger_seconds
        expected_x_stop = post_trigger_seconds
        trigger_window_tolerance_s = 0.05
        trigger_window_check = {}
        trigger_window_ok = True
        for name, header in channel_headers.items():
            x_start = float(header["x_start_s"])
            x_stop = float(header["x_stop_s"])
            start_error = x_start - expected_x_start
            stop_error = x_stop - expected_x_stop
            ok = (
                abs(start_error) <= trigger_window_tolerance_s
                and abs(stop_error) <= trigger_window_tolerance_s
            )
            trigger_window_ok = trigger_window_ok and ok
            trigger_window_check[name] = {
                "expected_x_start_s": expected_x_start,
                "expected_x_stop_s": expected_x_stop,
                "actual_x_start_s": x_start,
                "actual_x_stop_s": x_stop,
                "start_error_s": start_error,
                "stop_error_s": stop_error,
                "tolerance_s": trigger_window_tolerance_s,
                "ok": ok,
            }
        meta["trigger_window_check"] = trigger_window_check
        meta["trigger_window_ok"] = trigger_window_ok
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Saved metadata to {meta_path}")
        if not args.no_restore_fine_scope:
            phase_t = time.perf_counter()
            if args.restore_wavelength_mode == "initial":
                restore_nm = initial_wavelength_nm
                print(f"Moving TOPTICA back to initial wavelength {restore_nm:.9f} nm...")
                laser.move_to_wavelength(restore_nm, args.laser_settle_timeout_s)
                meta["laser_restore_target_nm"] = restore_nm
                meta["laser_restore_readback_nm"] = laser.wavelength_nm()
            elif args.restore_wavelength_mode == "fine-center":
                restore_nm = args.fine_center_nm
                print(f"Moving TOPTICA back to fine-scan center wavelength {restore_nm:g} nm...")
                laser.move_to_wavelength(restore_nm, args.laser_settle_timeout_s)
                meta["laser_restore_target_nm"] = restore_nm
                meta["laser_restore_readback_nm"] = laser.wavelength_nm()
                meta["laser_fine_center_readback_nm"] = meta["laser_restore_readback_nm"]
            else:
                print("Leaving TOPTICA wavelength at post-scan state (--restore-wavelength-mode none).")
                meta["laser_restore_target_nm"] = None
                meta["laser_restore_readback_nm"] = laser.wavelength_nm()

            meta["laser_restore_mode"] = args.restore_wavelength_mode
            mark_phase("restore_wavelength", phase_t)
            phase_t = time.perf_counter()
            print("Restoring TOPTICA external input arc factor for fine scan...")
            laser.configure_fine_scan_arc_factor(args.fine_arc_factor_v_per_v)
            meta["fine_scan_arc_factor_restored"] = True
            print("Restoring oscilloscope to fine-scan idle state: 1 ms/div, CH1 off, CH4 falling-edge AUTO trigger...")
            scope.configure_fine_scan_idle(
                trigger_level_v=args.fine_trigger_level_v,
                trigger_slope="negative",
                trigger_mode="auto",
            )
            meta["scope_idle_restored"] = True
            mark_phase("restore_fine_scan_state", phase_t)

            if args.cycle_emission_after_scan:
                phase_t = time.perf_counter()
                meta["emission_post_cycle_order"] = "after_fine_scan_restore"
                print("Fine-scan state restored; cycling TOPTICA emission after scan: OFF...")
                laser.set_emission_enabled(False)
                time.sleep(args.emission_off_seconds)
                meta["emission_post_cycle_off_seconds"] = args.emission_off_seconds
                print("Fine-scan state restored; cycling TOPTICA emission after scan: ON...")
                laser.set_emission_enabled(True)
                time.sleep(args.emission_on_settle_seconds)
                meta["emission_post_cycle_on_settle_seconds"] = args.emission_on_settle_seconds
                meta["laser_readback_after_post_emission_cycle_nm"] = laser.wavelength_nm()
                mark_phase("post_emission_cycle", phase_t)

            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        if not trigger_window_ok:
            raise RuntimeError(
                "Scope trigger window check failed: expected "
                f"{expected_x_start:.3f} s to {expected_x_stop:.3f} s around trigger. "
                "Raw data was saved, but this acquisition should be treated as invalid."
            )
        return 0
    finally:
        if scope is not None:
            scope.close()
        if laser is not None:
            laser.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
