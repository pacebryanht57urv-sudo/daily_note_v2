"""Small TOPTICA connection adapter shared by dashboard lock helpers."""

from __future__ import annotations

import math
import time
from typing import Any


class SerialTopticaDlcPro:
    """Minimal TOPTICA DLC PRO serial adapter used by the lock workflow."""

    def __init__(self, port: str, timeout_s: float = 2.0) -> None:
        try:
            import serial  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Missing dependency pyserial. Install with: python -m pip install pyserial") from exc

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

    def set_arc_factor_enabled(self, enabled: bool) -> None:
        self.set(f"laser1:dl:pc:external-input:enabled {'#t' if enabled else '#f'}")

    def set_arc_factor(self, factor_v_per_v: float) -> None:
        self.set(f"laser1:dl:pc:external-input:factor {factor_v_per_v:.9g}")

    def configure_fine_scan_arc_factor(self, factor_v_per_v: float = 25.0) -> None:
        self.set_arc_factor(factor_v_per_v)
        self.set_arc_factor_enabled(True)


class SerialTopticaSession:
    """Reuse one TOPTICA serial connection for a short control run."""

    def __init__(self, port: str, host: str = "", timeout_s: float = 0.25) -> None:
        self.port = port
        self.host = host
        self.timeout_s = timeout_s
        self._laser: Any | None = None

    def __enter__(self) -> "SerialTopticaSession":
        self._laser = SerialTopticaDlcPro(self.port, timeout_s=self.timeout_s)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    @property
    def laser(self) -> Any:
        if self._laser is None:
            raise RuntimeError("Serial TOPTICA session is not open")
        return self._laser

    def close(self) -> None:
        if self._laser is not None:
            self._laser.close()
            self._laser = None

    def read_pc(self, *, full: bool = True) -> dict[str, float | bool | str]:
        result: dict[str, float | bool | str] = {
            "connection": "serial",
            "port": self.port,
            "voltage_set": self.laser.pc_voltage_set_v(),
            "arc_factor": float(self.laser.get("laser1:dl:pc:external-input:factor")),
        }
        if full:
            result.update(
                {
                    "voltage_act": self.laser.pc_voltage_act_v(),
                    "pc_enabled": True,
                }
            )
        return result

    def write_pc_voltage(self, value: float, *, readback: str = "minimal") -> dict[str, float | bool | str]:
        self.laser.set_pc_voltage_v(float(value))
        time.sleep(0.5)
        if readback == "none":
            return {"connection": "serial", "port": self.port, "voltage_set": float(value)}
        return self.read_pc(full=(readback == "full"))

    def write_arc_factor(self, value: float, *, readback: str = "minimal") -> dict[str, float | bool | str]:
        self.laser.configure_fine_scan_arc_factor(float(value))
        time.sleep(0.4)
        if readback == "none":
            return {"connection": "serial", "port": self.port, "arc_factor": float(value)}
        return self.read_pc(full=(readback == "full"))

    def read_arc_factor(self) -> dict[str, float | bool | str]:
        return {
            "connection": "serial",
            "port": self.port,
            "arc_factor": float(self.laser.get("laser1:dl:pc:external-input:factor")),
            "arc_enabled": self.laser.get("laser1:dl:pc:external-input:enabled"),
        }


def read_pc(*, connection: str, host: str, port: str) -> dict[str, float | bool | str]:
    if connection == "serial":
        with SerialTopticaSession(port=port, host=host) as session:
            return session.read_pc(full=True)

    from toptica.lasersdk.dlcpro.v2_5_3 import DLCpro, NetworkConnection

    with DLCpro(NetworkConnection(host)) as dlc:
        pc = dlc.laser1.dl.pc
        return {
            "connection": "tcp",
            "host": host,
            "voltage_set": float(pc.voltage_set.get()),
            "voltage_act": float(pc.voltage_act.get()),
            "voltage_min": float(pc.voltage_min.get()),
            "voltage_max": float(pc.voltage_max.get()),
            "pc_enabled": bool(pc.enabled.get()),
            "arc_factor": float(pc.external_input.factor.get()),
        }


def write_pc_voltage(
    *, connection: str, host: str, port: str, value: float
) -> dict[str, float | bool | str]:
    if connection == "serial":
        with SerialTopticaSession(port=port, host=host) as session:
            return session.write_pc_voltage(float(value), readback="full")

    from toptica.lasersdk.dlcpro.v2_5_3 import DLCpro, NetworkConnection

    with DLCpro(NetworkConnection(host)) as dlc:
        pc = dlc.laser1.dl.pc
        vmin = float(pc.voltage_min.get())
        vmax = float(pc.voltage_max.get())
        clipped = max(vmin, min(vmax, float(value)))
        pc.voltage_set.set(clipped)
        time.sleep(0.5)
    return read_pc(connection=connection, host=host, port=port)


def write_arc_factor(
    *, connection: str, host: str, port: str, value: float
) -> dict[str, float | bool | str]:
    if connection == "serial":
        with SerialTopticaSession(port=port, host=host) as session:
            return session.write_arc_factor(float(value), readback="full")

    from toptica.lasersdk.dlcpro.v2_5_3 import DLCpro, NetworkConnection

    with DLCpro(NetworkConnection(host)) as dlc:
        pc = dlc.laser1.dl.pc
        pc.external_input.factor.set(float(value))
        time.sleep(0.4)
    return read_pc(connection=connection, host=host, port=port)


def move_to_wavelength(
    *,
    connection: str,
    host: str,
    port: str,
    target_nm: float,
    timeout_s: float,
    tolerance_nm: float,
) -> dict[str, object]:
    if connection == "serial":
        laser = SerialTopticaDlcPro(port)
        try:
            before = laser.wavelength_nm()
            laser.move_to_wavelength(float(target_nm), timeout_s, tolerance_nm)
            readback = laser.wavelength_nm()
            after_set = float(target_nm)
        finally:
            laser.close()
        ok = abs(readback - target_nm) <= tolerance_nm
        return {
            "ok": ok,
            "connection": "serial",
            "port": port,
            "target_nm": target_nm,
            "tolerance_nm": tolerance_nm,
            "before_nm": before,
            "after_set_nm": after_set,
            "after_read_nm": readback,
        }

    from toptica.lasersdk.dlcpro.v2_5_3 import DLCpro, NetworkConnection

    with DLCpro(NetworkConnection(host)) as dlc:
        before = float(dlc.laser1.ctl.wavelength_act.get())
        dlc.laser1.ctl.wavelength_set.set(float(target_nm))
        deadline = time.monotonic() + timeout_s
        readback = before
        samples = 0
        while time.monotonic() < deadline:
            readback = float(dlc.laser1.ctl.wavelength_act.get())
            samples += 1
            if abs(readback - target_nm) <= tolerance_nm:
                break
            time.sleep(1.0)
        after_set = float(dlc.laser1.ctl.wavelength_set.get())
    ok = abs(readback - target_nm) <= tolerance_nm
    return {
        "ok": ok,
        "connection": "tcp",
        "host": host,
        "target_nm": target_nm,
        "tolerance_nm": tolerance_nm,
        "before_nm": before,
        "after_set_nm": after_set,
        "after_read_nm": readback,
        "samples": samples,
    }
