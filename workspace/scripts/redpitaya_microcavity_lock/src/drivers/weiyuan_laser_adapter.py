"""Serial adapter for the Weiyuan Photonics laser controller.

The controller uses a small Modbus-like protocol documented in
``稳频通信协议V1.2.pdf``: 9600 8N1, function 0x03/0x10 for data registers,
function 0x01/0x05 for state registers, IEEE754 big-endian floats, and
CRC16 with low byte first.
"""

from __future__ import annotations

import argparse
import json
import struct
from dataclasses import dataclass
from typing import Any

import serial


FUNC_READ_DATA = 0x03
FUNC_WRITE_DATA = 0x10
FUNC_READ_STATE = 0x01
FUNC_WRITE_STATE = 0x05

REG_SN = 0x0018
REG_TEC_SET_RUN = 0x004A
REG_LD_SET_RUN = 0x006A
REG_TEC_SET_DEBUG = 0x0072
REG_LD_SET_DEBUG = 0x0076
REG_MODULE_TEMP = 0x007A
REG_TEC_TEMP = 0x007E
REG_LD_CURRENT = 0x0082
REG_MODE = 0x008E

STATE_TEC = 0x0001
STATE_LD = 0x0002


def crc16_modbus(payload: bytes) -> int:
    crc = 0xFFFF
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def append_crc(payload: bytes) -> bytes:
    crc = crc16_modbus(payload)
    return payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def validate_crc(frame: bytes) -> None:
    if len(frame) < 4:
        raise RuntimeError(f"Response too short: {frame.hex(' ')}")
    expected = crc16_modbus(frame[:-2])
    got = frame[-2] | (frame[-1] << 8)
    if got != expected:
        raise RuntimeError(f"CRC mismatch: got 0x{got:04x}, expected 0x{expected:04x}, frame={frame.hex(' ')}")


def float_to_payload(value: float) -> bytes:
    return struct.pack(">f", float(value))


def payload_to_float(payload: bytes) -> float:
    if len(payload) != 4:
        raise RuntimeError(f"Expected 4-byte float payload, got {len(payload)} bytes")
    return float(struct.unpack(">f", payload)[0])


@dataclass
class WeiyuanLaser:
    port: str = "COM5"
    slave: int = 1
    baudrate: int = 9600
    timeout_s: float = 0.8

    def __post_init__(self) -> None:
        self._serial: serial.Serial | None = None

    def __enter__(self) -> "WeiyuanLaser":
        self.open()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def open(self) -> None:
        if self._serial is not None and self._serial.is_open:
            return
        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=8,
            parity=serial.PARITY_NONE,
            stopbits=1,
            timeout=self.timeout_s,
            write_timeout=self.timeout_s,
        )

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    @property
    def serial(self) -> serial.Serial:
        if self._serial is None or not self._serial.is_open:
            self.open()
        assert self._serial is not None
        return self._serial

    def exchange(self, frame: bytes, expected_min_len: int) -> bytes:
        ser = self.serial
        ser.reset_input_buffer()
        ser.write(frame)
        ser.flush()
        response = ser.read(expected_min_len)
        if len(response) < expected_min_len:
            extra = ser.read(64)
            response += extra
        if len(response) < expected_min_len:
            raise TimeoutError(
                f"No complete response from {self.port}; expected at least {expected_min_len} bytes, "
                f"got {len(response)} bytes: {response.hex(' ')}"
            )
        validate_crc(response)
        if self.slave != 255 and response[0] != self.slave:
            raise RuntimeError(f"Unexpected slave id {response[0]}, expected {self.slave}")
        return response

    def read_data(self, register: int, length: int) -> bytes:
        payload = bytes([self.slave, FUNC_READ_DATA]) + register.to_bytes(2, "big") + length.to_bytes(2, "big")
        response = self.exchange(append_crc(payload), expected_min_len=6 + length + 2)
        if response[1] != FUNC_READ_DATA or int.from_bytes(response[2:4], "big") != register:
            raise RuntimeError(f"Unexpected read response: {response.hex(' ')}")
        returned_length = int.from_bytes(response[4:6], "big")
        if returned_length != length:
            raise RuntimeError(f"Unexpected read length {returned_length}, expected {length}: {response.hex(' ')}")
        return response[6 : 6 + length]

    def write_data(self, register: int, data: bytes) -> bytes:
        payload = (
            bytes([self.slave, FUNC_WRITE_DATA])
            + register.to_bytes(2, "big")
            + len(data).to_bytes(2, "big")
            + data
        )
        response = self.exchange(append_crc(payload), expected_min_len=6 + len(data) + 2)
        if response[:-2] != payload:
            raise RuntimeError(f"Unexpected write echo: sent={payload.hex(' ')}, response={response.hex(' ')}")
        return response

    def read_state_value(self, state_register: int) -> int:
        payload = bytes([self.slave, FUNC_READ_STATE]) + state_register.to_bytes(2, "big") + (1).to_bytes(2, "big")
        response = self.exchange(append_crc(payload), expected_min_len=8)
        if response[1] != FUNC_READ_STATE:
            raise RuntimeError(f"Unexpected state response: {response.hex(' ')}")
        return int.from_bytes(response[4:6], "big")

    def write_state_value(self, state_register: int, enabled: bool) -> bytes:
        value = 1 if enabled else 0
        payload = bytes([self.slave, FUNC_WRITE_STATE]) + state_register.to_bytes(2, "big") + value.to_bytes(2, "big")
        response = self.exchange(append_crc(payload), expected_min_len=8)
        if response[:-2] != payload:
            raise RuntimeError(f"Unexpected state write echo: sent={payload.hex(' ')}, response={response.hex(' ')}")
        return response

    def read_float(self, register: int) -> float:
        return payload_to_float(self.read_data(register, 4))

    def write_float(self, register: int, value: float) -> None:
        self.write_data(register, float_to_payload(value))

    def read_mode(self) -> int:
        data = self.read_data(REG_MODE, 1)
        return int(data[0])

    def write_mode(self, mode: int) -> None:
        if mode not in {0, 1}:
            raise ValueError("mode must be 0 (run) or 1 (debug)")
        self.write_data(REG_MODE, bytes([mode]))

    def read_status(self) -> dict[str, Any]:
        tec_state_value = self.read_state_value(STATE_TEC)
        ld_state_value = self.read_state_value(STATE_LD)
        mode = self.read_mode()
        tec_set_run = self.read_float(REG_TEC_SET_RUN)
        ld_set_run = self.read_float(REG_LD_SET_RUN)
        tec_set_debug = self.read_float(REG_TEC_SET_DEBUG)
        ld_set_debug = self.read_float(REG_LD_SET_DEBUG)
        return {
            "port": self.port,
            "slave": self.slave,
            "mode": mode,
            "mode_label": "debug" if mode == 1 else "run",
            "module_temp_c": self.read_float(REG_MODULE_TEMP),
            "tec_temp_c": self.read_float(REG_TEC_TEMP),
            "ld_current_actual_ma": self.read_float(REG_LD_CURRENT),
            "tec_set_temp_c": tec_set_debug if mode == 1 else tec_set_run,
            "ld_set_current_ma": ld_set_debug if mode == 1 else ld_set_run,
            "tec_set_temp_run_c": tec_set_run,
            "ld_set_current_run_ma": ld_set_run,
            "tec_set_temp_debug_c": tec_set_debug,
            "ld_set_current_debug_ma": ld_set_debug,
            "tec_enabled": bool(tec_state_value & 0x0002),
            "ld_enabled": bool(ld_state_value & 0x0004),
            "raw_state": {
                "tec": tec_state_value,
                "ld": ld_state_value,
            },
        }

    def set_temperature_c(self, value: float, *, debug: bool | None = None) -> None:
        use_debug = self.read_mode() == 1 if debug is None else debug
        self.write_float(REG_TEC_SET_DEBUG if use_debug else REG_TEC_SET_RUN, value)

    def set_current_ma(self, value: float, *, debug: bool | None = None) -> None:
        use_debug = self.read_mode() == 1 if debug is None else debug
        self.write_float(REG_LD_SET_DEBUG if use_debug else REG_LD_SET_RUN, value)

    def set_tec_enabled(self, enabled: bool) -> None:
        self.write_state_value(STATE_TEC, enabled)

    def set_ld_enabled(self, enabled: bool) -> None:
        self.write_state_value(STATE_LD, enabled)


def read_status(port: str, *, slave: int = 1) -> dict[str, Any]:
    with WeiyuanLaser(port=port, slave=slave) as laser:
        return laser.read_status()


def write_temperature(port: str, value_c: float, *, slave: int = 1) -> dict[str, Any]:
    with WeiyuanLaser(port=port, slave=slave) as laser:
        laser.set_temperature_c(value_c)
        return laser.read_status()


def write_current(port: str, value_ma: float, *, slave: int = 1) -> dict[str, Any]:
    with WeiyuanLaser(port=port, slave=slave) as laser:
        laser.set_current_ma(value_ma)
        return laser.read_status()


def write_initial_current(port: str, *, value_ma: float = 260.0, slave: int = 1) -> dict[str, Any]:
    return write_current(port, value_ma, slave=slave)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["status", "set-temp", "set-current", "set-initial-current", "tec-on", "tec-off", "ld-on", "ld-off"])
    parser.add_argument("--port", default="COM5")
    parser.add_argument("--slave", type=int, default=1)
    parser.add_argument("--value", type=float)
    args = parser.parse_args()

    with WeiyuanLaser(port=args.port, slave=args.slave) as laser:
        if args.action == "status":
            result = laser.read_status()
        elif args.action == "set-temp":
            if args.value is None:
                raise SystemExit("--value is required")
            laser.set_temperature_c(args.value)
            result = laser.read_status()
        elif args.action == "set-current":
            if args.value is None:
                raise SystemExit("--value is required")
            laser.set_current_ma(args.value)
            result = laser.read_status()
        elif args.action == "set-initial-current":
            laser.set_current_ma(260.0 if args.value is None else args.value)
            result = laser.read_status()
        elif args.action == "tec-on":
            laser.set_tec_enabled(True)
            result = laser.read_status()
        elif args.action == "tec-off":
            laser.set_tec_enabled(False)
            result = laser.read_status()
        elif args.action == "ld-on":
            laser.set_ld_enabled(True)
            result = laser.read_status()
        elif args.action == "ld-off":
            laser.set_ld_enabled(False)
            result = laser.read_status()
        else:
            raise AssertionError(args.action)

    print(json.dumps({"ok": True, "weiyuan": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
