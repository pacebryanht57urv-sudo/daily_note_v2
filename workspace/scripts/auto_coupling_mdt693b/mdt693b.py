"""Minimal Thorlabs MDT693B serial helper.

The MDT693B USB interface appears as an RS232/VCP serial port.  This helper is
intentionally conservative: high-level write helpers clamp voltage and read
back after setting, while discovery can be run read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Iterable

import serial


DEFAULT_BAUDRATE = 115200
DEFAULT_TIMEOUT_S = 0.8
TERMINATOR = "\r\n"


class MDT693BError(RuntimeError):
    """Raised when the controller returns an error-like or empty response."""


@dataclass
class AxisLimits:
    minimum_v: float | None
    maximum_v: float | None


class MDT693B:
    """Small serial wrapper for MDT693B command-line interface."""

    def __init__(
        self,
        port: str,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.port = port
        self.serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout_s,
            write_timeout=timeout_s,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        time.sleep(0.1)
        self.serial.reset_input_buffer()
        self.serial.reset_output_buffer()

    def close(self) -> None:
        self.serial.close()

    def __enter__(self) -> "MDT693B":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def query(self, command: str, *, allow_empty: bool = False) -> str:
        """Send one command/query and return the response text.

        Commands are terminated with CRLF. The controller can emit prompt-like
        characters such as "*" or "!" around responses; these are preserved in
        the raw response so callers can inspect them.
        """
        self.serial.reset_input_buffer()
        payload = (command.strip() + TERMINATOR).encode("ascii")
        self.serial.write(payload)
        self.serial.flush()
        time.sleep(0.06)
        chunks: list[bytes] = []
        deadline = time.monotonic() + float(self.serial.timeout or DEFAULT_TIMEOUT_S)
        while time.monotonic() < deadline:
            waiting = self.serial.in_waiting
            if waiting:
                chunks.append(self.serial.read(waiting))
                deadline = time.monotonic() + 0.08
            else:
                time.sleep(0.02)
        text = b"".join(chunks).decode("ascii", errors="replace").strip()
        if not text and not allow_empty:
            raise MDT693BError(f"{self.port} returned no response to {command!r}")
        return text

    def cleaned_query(self, command: str, *, allow_empty: bool = False) -> str:
        raw = self.query(command, allow_empty=allow_empty)
        lines = [line.strip() for line in raw.replace("\r", "\n").split("\n")]
        lines = [
            line
            for line in lines
            if line and line not in {"*", "!", ">"} and line.lower() != command.lower()
        ]
        return "\n".join(lines)

    def read_float(self, command: str, *, attempts: int = 4, retry_delay_s: float = 0.08) -> float:
        last_error: Exception | None = None
        last_response = ""
        for attempt in range(max(1, attempts)):
            try:
                response = self.cleaned_query(command)
            except MDT693BError as exc:
                last_error = exc
                if attempt < attempts - 1:
                    time.sleep(retry_delay_s)
                    continue
                raise
            last_response = response
            cleaned = (
                response.replace(",", " ")
                .replace("[", " ")
                .replace("]", " ")
                .replace(">", " ")
            )
            for token in cleaned.split():
                try:
                    return float(token)
                except ValueError:
                    continue
            last_error = MDT693BError(f"Could not parse float from {command!r}: {response!r}")
            if attempt < attempts - 1:
                time.sleep(retry_delay_s)
        if last_error is not None:
            raise MDT693BError(f"Could not parse float from {command!r}: {last_response!r}") from last_error
        raise MDT693BError(f"Could not parse float from {command!r}: {last_response!r}")

    def identify(self) -> dict[str, str]:
        return {
            "id": self.cleaned_query("id?", allow_empty=True),
            "serial": self.cleaned_query("serial?", allow_empty=True),
            "friendly": self.cleaned_query("friendly?", allow_empty=True),
        }

    def read_axis_voltage(self, axis: str) -> float:
        axis = self._axis(axis)
        return self.read_float(f"{axis}voltage?")

    def read_axis_limits(self, axis: str) -> AxisLimits:
        axis = self._axis(axis)
        return AxisLimits(
            minimum_v=self._read_optional_float(f"{axis}min?"),
            maximum_v=self._read_optional_float(f"{axis}max?"),
        )

    def write_axis_voltage_blind(
        self,
        axis: str,
        voltage_v: float,
        *,
        min_v: float = 0.0,
        max_v: float = 150.0,
    ) -> None:
        """Write an axis voltage command without waiting for controller readback."""
        axis = self._axis(axis)
        target = float(voltage_v)
        if not (min_v <= target <= max_v):
            raise ValueError(f"Command target {target:g} V outside [{min_v:g}, {max_v:g}] V")
        payload = (f"{axis}voltage={target:.6f}" + TERMINATOR).encode("ascii")
        self.serial.write(payload)
        self.serial.flush()

    def set_axis_voltage(
        self,
        axis: str,
        voltage_v: float,
        *,
        min_v: float = 0.0,
        max_v: float = 150.0,
        max_step_v: float = 1.0,
        readback_tolerance_v: float = 0.05,
        expected_readback_v: float | None = None,
    ) -> float:
        """Set one axis voltage with conservative bounds and readback."""
        axis = self._axis(axis)
        current = self.read_axis_voltage(axis)
        target = float(voltage_v)
        expected = target if expected_readback_v is None else float(expected_readback_v)
        if not (min_v <= expected <= max_v):
            raise ValueError(f"Expected output {expected:g} V outside [{min_v:g}, {max_v:g}] V")
        if not (min_v <= target <= max_v):
            raise ValueError(f"Command target {target:g} V outside [{min_v:g}, {max_v:g}] V")
        if abs(expected - current) > max_step_v:
            raise ValueError(
                f"Refusing {current:g} -> {expected:g} V output step on {axis}; "
                f"max_step_v={max_step_v:g}"
            )
        if abs(expected - current) < 1e-9:
            return current
        self.cleaned_query(f"{axis}voltage={target:.6f}", allow_empty=True)
        time.sleep(0.08)
        readback = self.read_axis_voltage(axis)
        if abs(readback - expected) > readback_tolerance_v:
            raise ValueError(
                f"{axis} readback {readback:g} V differs from expected {expected:g} V "
                f"by more than {readback_tolerance_v:g} V"
            )
        return readback

    def set_axis_voltage_fast(
        self,
        axis: str,
        voltage_v: float,
        *,
        min_v: float = 0.0,
        max_v: float = 150.0,
        max_step_v: float = 1.0,
        expected_readback_v: float | None = None,
        settle_s: float = 0.02,
    ) -> float:
        """Set one axis voltage and return actual readback without convergence enforcement.

        This is useful for coupling scans where optical power is the real feedback
        signal. It still protects the requested range and maximum step, but it does
        not fail merely because the controller readback lags or quantizes the target.
        """
        axis = self._axis(axis)
        current = self.read_axis_voltage(axis)
        target = float(voltage_v)
        expected = target if expected_readback_v is None else float(expected_readback_v)
        if not (min_v <= expected <= max_v):
            raise ValueError(f"Expected output {expected:g} V outside [{min_v:g}, {max_v:g}] V")
        if not (min_v <= target <= max_v):
            raise ValueError(f"Command target {target:g} V outside [{min_v:g}, {max_v:g}] V")
        if abs(expected - current) > max_step_v:
            raise ValueError(
                f"Refusing {current:g} -> {expected:g} V output step on {axis}; "
                f"max_step_v={max_step_v:g}"
            )
        if abs(expected - current) >= 1e-9:
            self.cleaned_query(f"{axis}voltage={target:.6f}", allow_empty=True)
            time.sleep(max(0.0, float(settle_s)))
        return self.read_axis_voltage(axis)

    def _read_optional_float(self, command: str) -> float | None:
        try:
            return self.read_float(command)
        except Exception:
            return None

    @staticmethod
    def _axis(axis: str) -> str:
        axis = axis.strip().lower()
        if axis not in {"x", "y", "z"}:
            raise ValueError(f"axis must be x, y, or z; got {axis!r}")
        return axis


def discover_ports(ports: Iterable[str]) -> list[dict[str, object]]:
    """Read identity and voltage state from MDT693B ports."""
    rows: list[dict[str, object]] = []
    for port in ports:
        row: dict[str, object] = {"port": port, "ok": False}
        try:
            with MDT693B(port) as dev:
                row.update(dev.identify())
                row["vlimit"] = dev.cleaned_query("vlimit?", allow_empty=True)
                row["msenable"] = dev.cleaned_query("msenable?", allow_empty=True)
                row["msvoltage_v"] = dev._read_optional_float("msvoltage?")
                for axis in ("x", "y", "z"):
                    row[f"{axis}_voltage_v"] = dev.read_axis_voltage(axis)
                    limits = dev.read_axis_limits(axis)
                    row[f"{axis}_min_v"] = limits.minimum_v
                    row[f"{axis}_max_v"] = limits.maximum_v
                row["ok"] = True
        except Exception as exc:
            row["error"] = repr(exc)
        rows.append(row)
    return rows
