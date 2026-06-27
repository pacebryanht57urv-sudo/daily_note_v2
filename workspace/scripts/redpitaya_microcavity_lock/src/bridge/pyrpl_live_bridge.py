"""Run PyRPL GUI with a tiny local control bridge.

This process owns exactly one PyRPL instance. The GUI and HTTP commands operate
on that same instance, so parameter changes can be observed in the GUI without
editing YAML files behind PyRPL's back.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import traceback
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np

BRIDGE_DIR = Path(__file__).resolve().parent
SRC_DIR = BRIDGE_DIR.parent
COMMON_DIR = SRC_DIR / "common"
if str(COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(COMMON_DIR))

from data_paths import RESULTS_DIR

SAFE_PARAMS = {
    "scope.input1",
    "scope.input2",
    "scope.duration",
    "scope.trigger_delay",
    "scope.trigger_source",
    "scope.threshold",
    "scope.hysteresis",
    "scope.rolling_mode",
    "scope.run_continuous",
    "scope.average",
    "scope.scope_zero_enabled",
    "scope.ch1_zero_offset_v",
    "scope.ch2_zero_offset_v",
    "scope.ch1_power_response_v_per_w",
    "scope.ch2_power_response_v_per_w",
    "scope.scope_power_avg_frames",
    "pid0.inputfilter",
    "networkanalyzer.dbm_display_enabled",
    "networkanalyzer.dbm_load_ohm",
    "networkanalyzer.dbm_highz_correction_db",
    "networkanalyzer.external_gain_db",
    "networkanalyzer.amplitude",
    "networkanalyzer.start_freq",
    "networkanalyzer.stop_freq",
    "networkanalyzer.points",
    "networkanalyzer.rbw",
    "spectrumanalyzer.span",
    "spectrumanalyzer.trace_average",
}


READABLE_PREFIXES = (
    "scope.",
    "pid0.",
    "pid1.",
    "pid2.",
    "asg0.",
    "asg1.",
    "spectrumanalyzer.",
    "networkanalyzer.",
)


def parse_bool(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Cannot parse boolean value: {text!r}")


def json_ready(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer, np.bool_)):
        return value.item()
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_ready(v) for v in value]
    if hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
        try:
            return [json_ready(v) for v in value]
        except TypeError:
            pass
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start PyRPL and expose a localhost parameter bridge."
    )
    parser.add_argument("--config", default="try", help="PyRPL config name without .yml")
    parser.add_argument("--hostname", default="192.168.1.34", help="Red Pitaya IP")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=7870)
    parser.add_argument("--loglevel", default="info")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Do not open the PyRPL Qt GUI; keep only the localhost bridge running.",
    )
    parser.add_argument(
        "--allow-risky",
        action="store_true",
        help="Allow writes outside the initial safe parameter whitelist.",
    )
    parser.add_argument(
        "--spectrum-load-ohm",
        type=float,
        default=50.0,
        help="Equivalent load resistance for spectrum dBm/dBm/Hz display units.",
    )
    parser.add_argument(
        "--spectrum-power-correction-enabled",
        type=parse_bool,
        default=True,
        help=(
            "Apply display-only dB correction to spectrum dBm/dBm/Hz units. "
            "Use false to show raw RP 50-ohm-equivalent values."
        ),
    )
    parser.add_argument(
        "--spectrum-highz-correction-db",
        type=float,
        default=20.0 * math.log10(2.0),
        help=(
            "Display correction for RP 1 Mohm input reading a 50-ohm source. "
            "Default is 20log10(2)=6.0206 dB."
        ),
    )
    parser.add_argument(
        "--spectrum-external-gain-db",
        type=float,
        default=0.0,
        help="Display correction for external RF gain before the RP input.",
    )
    parser.add_argument(
        "--scope-ch1-response-v-per-w",
        type=float,
        default=3.22013e3,
        help="Initial CH1 optical-power response used by the patched scope GUI.",
    )
    parser.add_argument(
        "--scope-ch2-response-v-per-w",
        type=float,
        default=3.22013e3,
        help="Initial CH2 optical-power response used by the patched scope GUI.",
    )
    parser.add_argument(
        "--scope-zero-enabled",
        type=parse_bool,
        default=False,
        help="Apply configured CH1/CH2 display-zero offsets when opening the patched scope GUI.",
    )
    parser.add_argument(
        "--scope-ch1-zero-offset-v",
        type=float,
        default=0.0,
        help="Initial CH1 display-zero offset in volts, subtracted from the scope GUI trace.",
    )
    parser.add_argument(
        "--scope-ch2-zero-offset-v",
        type=float,
        default=0.0,
        help="Initial CH2 display-zero offset in volts, subtracted from the scope GUI trace.",
    )
    parser.add_argument(
        "--networkanalyzer-dbm-display-enabled",
        type=parse_bool,
        default=True,
        help="Display network-analyzer magnitude as coherent 50-ohm-equivalent dBm.",
    )
    parser.add_argument(
        "--networkanalyzer-load-ohm",
        type=float,
        default=50.0,
        help="Equivalent load resistance for network-analyzer dBm display.",
    )
    parser.add_argument(
        "--networkanalyzer-highz-correction-db",
        type=float,
        default=20.0 * math.log10(2.0),
        help="Display correction for RP 1 Mohm input in network-analyzer dBm mode.",
    )
    parser.add_argument(
        "--networkanalyzer-external-gain-db",
        type=float,
        default=None,
        help=(
            "External RF gain subtracted from network-analyzer dBm display. "
            "Defaults to --spectrum-external-gain-db."
        ),
    )
    return parser.parse_args()


def patch_spectrum_power_units(
    load_ohm: float,
    correction_enabled: bool = True,
    highz_correction_db: float = 20.0 * math.log10(2.0),
    external_gain_db: float = 0.0,
) -> dict[str, Any]:
    """Add 50-ohm-equivalent dBm display units to PyRPL's spectrum GUI."""
    from pyrpl.attributes import FloatProperty
    from pyrpl.software_modules.spectrum_analyzer import SpectrumAnalyzer

    if load_ohm <= 0:
        raise ValueError("--spectrum-load-ohm must be positive")
    if not all(
        np.isfinite(value)
        for value in (highz_correction_db, external_gain_db)
    ):
        raise ValueError("Spectrum display correction values must be finite")

    unit_property = SpectrumAnalyzer.display_unit
    options = list(unit_property.options(None).keys())
    for unit in ("dBm", "dBm/Hz"):
        if unit not in options:
            options.append(unit)
    unit_property.default_options = options

    if not hasattr(SpectrumAnalyzer, "external_gain_db"):

        class SpectrumExternalGainProperty(FloatProperty):
            def set_value(self, obj, value):
                super(SpectrumExternalGainProperty, self).set_value(obj, value)
                try:
                    obj._emit_signal_by_name("unit_changed")
                except Exception:
                    pass

        external_gain_property = SpectrumExternalGainProperty(
            default=external_gain_db,
            min=-200.0,
            max=200.0,
            increment=1.0,
            doc=(
                "External RF chain gain in dB. For dBm/dBm/Hz display, "
                "this value is subtracted after the fixed RP high-Z correction."
            ),
        )
        external_gain_property.name = "external_gain_db"
        SpectrumAnalyzer.external_gain_db = external_gain_property

    if "external_gain_db" not in SpectrumAnalyzer._gui_attributes:
        gui_attrs = list(SpectrumAnalyzer._gui_attributes)
        insert_after = "display_unit"
        try:
            index = gui_attrs.index(insert_after) + 1
        except ValueError:
            index = len(gui_attrs)
        gui_attrs.insert(index, "external_gain_db")
        SpectrumAnalyzer._gui_attributes = gui_attrs

    if getattr(SpectrumAnalyzer, "_daily_note_dbm_units_patched", False):
        SpectrumAnalyzer._daily_note_spectrum_load_ohm = load_ohm
        SpectrumAnalyzer._daily_note_power_correction_enabled = correction_enabled
        SpectrumAnalyzer._daily_note_highz_correction_db = highz_correction_db
        SpectrumAnalyzer._daily_note_external_gain_default_db = external_gain_db
        return spectrum_power_correction_state()

    original_data_to_unit = SpectrumAnalyzer.data_to_unit

    def data_to_unit_with_dbm(self, data, unit, rbw):
        if unit in {"dBm", "dBm/Hz"}:
            data = np.abs(data)
            divisor = 2.0 * self._daily_note_spectrum_load_ohm * 1e-3
            if unit == "dBm/Hz":
                divisor *= rbw
            values = 10.0 * np.log10(data / divisor + np.finfo(float).tiny)
            if self._daily_note_power_correction_enabled:
                values -= (
                    self._daily_note_highz_correction_db
                    + float(self.external_gain_db)
                )
            return values
        return original_data_to_unit(self, data, unit, rbw)

    SpectrumAnalyzer._daily_note_spectrum_load_ohm = load_ohm
    SpectrumAnalyzer._daily_note_power_correction_enabled = correction_enabled
    SpectrumAnalyzer._daily_note_highz_correction_db = highz_correction_db
    SpectrumAnalyzer._daily_note_external_gain_default_db = external_gain_db
    SpectrumAnalyzer.data_to_unit = data_to_unit_with_dbm
    SpectrumAnalyzer._daily_note_dbm_units_patched = True
    return spectrum_power_correction_state()


def patch_scope_mean_display() -> None:
    """Add a live active-channel mean readout to PyRPL's scope GUI."""
    from pyrpl.attributes import BoolProperty, FloatProperty, IntProperty, StringProperty
    from pyrpl.hardware_modules.scope import Scope
    from pyrpl.widgets.module_widgets.scope_widget import ScopeWidget
    from qtpy import QtWidgets
    import pyqtgraph as pg

    if not hasattr(Scope, "active_means_v"):
        active_means_property = StringProperty(
            default="mean: n/a",
            doc="Mean value of active scope traces currently displayed, in volts.",
        )
        active_means_property.name = "active_means_v"
        Scope.active_means_v = active_means_property

    if not hasattr(Scope, "scope_zero_enabled"):
        zero_enabled_property = BoolProperty(
            default=False,
            doc="Apply stored CH1/CH2 voltage offsets to the displayed scope traces.",
        )
        zero_enabled_property.name = "scope_zero_enabled"
        Scope.scope_zero_enabled = zero_enabled_property

    if not hasattr(Scope, "ch1_zero_offset_v"):
        ch1_zero_property = FloatProperty(
            default=0.0,
            min=-100.0,
            max=100.0,
            increment=1e-3,
            doc="CH1 voltage offset subtracted from displayed scope traces.",
        )
        ch1_zero_property.name = "ch1_zero_offset_v"
        Scope.ch1_zero_offset_v = ch1_zero_property

    if not hasattr(Scope, "ch2_zero_offset_v"):
        ch2_zero_property = FloatProperty(
            default=0.0,
            min=-100.0,
            max=100.0,
            increment=1e-3,
            doc="CH2 voltage offset subtracted from displayed scope traces.",
        )
        ch2_zero_property.name = "ch2_zero_offset_v"
        Scope.ch2_zero_offset_v = ch2_zero_property

    if not hasattr(Scope, "ch1_power_response_v_per_w"):
        ch1_response_property = FloatProperty(
            default=3.22013e3,
            min=1e-12,
            max=1e12,
            increment=100.0,
            doc="CH1 optical-power calibration, voltage response in V/W.",
        )
        ch1_response_property.name = "ch1_power_response_v_per_w"
        Scope.ch1_power_response_v_per_w = ch1_response_property

    if not hasattr(Scope, "ch2_power_response_v_per_w"):
        ch2_response_property = FloatProperty(
            default=3.22013e3,
            min=1e-12,
            max=1e12,
            increment=100.0,
            doc="CH2 optical-power calibration, voltage response in V/W.",
        )
        ch2_response_property.name = "ch2_power_response_v_per_w"
        Scope.ch2_power_response_v_per_w = ch2_response_property

    if not hasattr(Scope, "scope_power_avg_frames"):
        power_avg_property = IntProperty(
            default=30,
            min=1,
            max=10000,
            increment=1,
            doc="Number of displayed scope frames used for slow optical-power averaging.",
        )
        power_avg_property.name = "scope_power_avg_frames"
        Scope.scope_power_avg_frames = power_avg_property

    if "active_means_v" in Scope._gui_attributes:
        gui_attrs = list(Scope._gui_attributes)
        gui_attrs = [attr for attr in gui_attrs if attr != "active_means_v"]
        Scope._gui_attributes = gui_attrs

    if getattr(ScopeWidget, "_daily_note_scope_mean_display_patched", False):
        return

    original_init_gui = ScopeWidget.init_gui
    original_display_curve = ScopeWidget.display_curve

    def format_mean(value: float) -> str:
        if not np.isfinite(value):
            return "nan"
        abs_value = abs(value)
        if abs_value < 1e-3:
            return f"{value * 1e6:.2f} uV"
        if abs_value < 1.0:
            return f"{value * 1e3:.3f} mV"
        return f"{value:.5g} V"

    def format_response(value: float) -> str:
        if not np.isfinite(value) or value <= 0:
            return "n/a"
        if value >= 1e6:
            return f"{value / 1e6:.3g} MV/W"
        if value >= 1e3:
            return f"{value / 1e3:.3g} kV/W"
        return f"{value:.3g} V/W"

    def format_power_w(value: float) -> str:
        if not np.isfinite(value):
            return "nan"
        abs_value = abs(value)
        if abs_value < 1e-9:
            return f"{value * 1e12:.2f} pW"
        if abs_value < 1e-6:
            return f"{value * 1e9:.2f} nW"
        if abs_value < 1e-3:
            return f"{value * 1e6:.3f} uW"
        return f"{value * 1e3:.3f} mW"

    def set_active_means(widget: Any, text: str) -> None:
        try:
            widget.module.active_means_v = text
        except Exception:
            pass
        try:
            aw = widget.attribute_widgets.get("active_means_v")
            if aw is not None:
                aw.widget_value = text
        except Exception:
            pass

    def raw_scope_arrays(widget: Any) -> tuple[np.ndarray, np.ndarray] | None:
        cached = getattr(widget, "_daily_note_last_raw_scope_curves", None)
        if cached is not None:
            return cached
        data_avg = getattr(widget.module, "data_avg", None)
        if data_avg is None:
            return None
        try:
            data_avg = np.asarray(data_avg, dtype=float)
            return np.asarray(data_avg[0], dtype=float), np.asarray(data_avg[1], dtype=float)
        except Exception:
            return None

    def clear_power_history(widget: Any) -> None:
        widget._daily_note_power_history = [[], []]

    def update_power_history(
        widget: Any, powers: tuple[float, float], update_history: bool
    ) -> tuple[float, float]:
        history = getattr(widget, "_daily_note_power_history", None)
        if history is None:
            history = [[], []]
            widget._daily_note_power_history = history
        try:
            avg_frames = max(1, int(getattr(widget.module, "scope_power_avg_frames", 30)))
        except Exception:
            avg_frames = 30
        if update_history:
            for idx, power in enumerate(powers):
                if np.isfinite(power):
                    history[idx].append(float(power))
                if len(history[idx]) > avg_frames:
                    del history[idx][:-avg_frames]
        averages = []
        for idx, power in enumerate(powers):
            values = history[idx][-avg_frames:]
            if values:
                averages.append(float(np.nanmean(values)))
            else:
                averages.append(float(power))
        return averages[0], averages[1]

    def set_zero_from_current(widget: Any, mode: str) -> None:
        arrays = raw_scope_arrays(widget)
        if arrays is None:
            return
        ch1, ch2 = arrays
        if mode in {"active", "both"} and (mode == "both" or widget.module.ch1_active):
            widget.module.ch1_zero_offset_v = float(np.nanmean(ch1))
        if mode in {"active", "both"} and (mode == "both" or widget.module.ch2_active):
            widget.module.ch2_zero_offset_v = float(np.nanmean(ch2))
        widget.module.scope_zero_enabled = True
        clear_power_history(widget)
        refresh_zero_overlay(widget)

    def clear_zero(widget: Any) -> None:
        widget.module.ch1_zero_offset_v = 0.0
        widget.module.ch2_zero_offset_v = 0.0
        widget.module.scope_zero_enabled = False
        clear_power_history(widget)
        refresh_zero_overlay(widget)

    def make_response_spinbox(widget: Any, attr: str) -> Any:
        spin = QtWidgets.QDoubleSpinBox()
        spin.setDecimals(2)
        spin.setRange(1e-12, 1e12)
        spin.setSingleStep(100.0)
        spin.setValue(float(getattr(widget.module, attr)))
        spin.setSuffix(" V/W")
        spin.setMaximumWidth(150)

        def update_response(value: float) -> None:
            setattr(widget.module, attr, float(value))
            clear_power_history(widget)
            refresh_zero_overlay(widget)

        spin.valueChanged.connect(update_response)
        return spin

    def make_avg_frames_spinbox(widget: Any) -> Any:
        spin = QtWidgets.QSpinBox()
        spin.setRange(1, 10000)
        spin.setSingleStep(1)
        spin.setValue(int(getattr(widget.module, "scope_power_avg_frames", 30)))
        spin.setSuffix(" frames")
        spin.setMaximumWidth(130)

        def update_avg_frames(value: int) -> None:
            widget.module.scope_power_avg_frames = int(value)
            clear_power_history(widget)
            refresh_zero_overlay(widget)

        spin.valueChanged.connect(update_avg_frames)
        return spin

    def init_gui_with_active_means(self):
        original_init_gui(self)
        try:
            self.mean_text_item = pg.TextItem(
                text="mean: n/a",
                color=(255, 255, 255),
                anchor=(1, 0),
                fill=(0, 0, 0, 170),
                border=(255, 255, 255, 120),
            )
            self.plot_item.addItem(self.mean_text_item, ignoreBounds=True)
        except Exception:
            self.mean_text_item = None
        update_mean_text_position(self)

        try:
            self.zero_group = QtWidgets.QGroupBox("Display zero")
            zero_layout = QtWidgets.QHBoxLayout()
            zero_layout.setContentsMargins(4, 4, 4, 4)
            self.zero_group.setLayout(zero_layout)

            self.zero_active_button = QtWidgets.QPushButton("Set active")
            self.zero_both_button = QtWidgets.QPushButton("Set both")
            self.zero_clear_button = QtWidgets.QPushButton("Clear")
            for button in (
                self.zero_active_button,
                self.zero_both_button,
                self.zero_clear_button,
            ):
                button.setMaximumWidth(85)
                zero_layout.addWidget(button)
            self.zero_active_button.clicked.connect(
                lambda: set_zero_from_current(self, "active")
            )
            self.zero_both_button.clicked.connect(
                lambda: set_zero_from_current(self, "both")
            )
            self.zero_clear_button.clicked.connect(lambda: clear_zero(self))
            self.layout_misc.addWidget(self.zero_group)
        except Exception:
            pass

        try:
            self.power_group = QtWidgets.QGroupBox("Power cal")
            power_layout = QtWidgets.QGridLayout()
            power_layout.setContentsMargins(4, 4, 4, 4)
            self.power_group.setLayout(power_layout)

            power_layout.addWidget(QtWidgets.QLabel("CH1"), 0, 0)
            self.ch1_response_spin = make_response_spinbox(
                self, "ch1_power_response_v_per_w"
            )
            power_layout.addWidget(self.ch1_response_spin, 0, 1)

            power_layout.addWidget(QtWidgets.QLabel("CH2"), 1, 0)
            self.ch2_response_spin = make_response_spinbox(
                self, "ch2_power_response_v_per_w"
            )
            power_layout.addWidget(self.ch2_response_spin, 1, 1)

            power_layout.addWidget(QtWidgets.QLabel("avg"), 2, 0)
            self.power_avg_frames_spin = make_avg_frames_spinbox(self)
            power_layout.addWidget(self.power_avg_frames_spin, 2, 1)

            self.power_avg_clear_button = QtWidgets.QPushButton("Clear avg")
            self.power_avg_clear_button.setMaximumWidth(100)
            self.power_avg_clear_button.clicked.connect(
                lambda: (clear_power_history(self), refresh_zero_overlay(self))
            )
            power_layout.addWidget(self.power_avg_clear_button, 3, 1)

            self.layout_misc.addWidget(self.power_group)
        except Exception:
            pass

        try:
            self.plot_item.vb.sigRangeChanged.connect(
                lambda *args: update_mean_text_position(self)
            )
        except Exception:
            pass

    def update_mean_text_position(widget: Any) -> None:
        item = getattr(widget, "mean_text_item", None)
        if item is None:
            return
        try:
            (x_min, x_max), (y_min, y_max) = widget.plot_item.viewRange()
            x = x_min + 0.985 * (x_max - x_min)
            y = y_max - 0.055 * (y_max - y_min)
            item.setPos(x, y)
        except Exception:
            pass

    def refresh_zero_overlay(widget: Any, update_history: bool = False) -> None:
        arrays = raw_scope_arrays(widget)
        if arrays is None:
            text = "mean: waiting for scope data"
            set_active_means(widget, text)
            try:
                if getattr(widget, "mean_text_item", None) is not None:
                    widget.mean_text_item.setText(text)
            except Exception:
                pass
            return

        ch1, ch2 = arrays
        zero_enabled = bool(getattr(widget.module, "scope_zero_enabled", False))
        offsets = (
            float(getattr(widget.module, "ch1_zero_offset_v", 0.0)),
            float(getattr(widget.module, "ch2_zero_offset_v", 0.0)),
        )
        raw_means = (float(np.nanmean(ch1)), float(np.nanmean(ch2)))
        shown_means = (
            raw_means[0] - offsets[0] if zero_enabled else raw_means[0],
            raw_means[1] - offsets[1] if zero_enabled else raw_means[1],
        )
        responses = (
            float(getattr(widget.module, "ch1_power_response_v_per_w", np.nan)),
            float(getattr(widget.module, "ch2_power_response_v_per_w", np.nan)),
        )
        powers = (
            shown_means[0] / responses[0]
            if np.isfinite(responses[0]) and responses[0] > 0
            else np.nan,
            shown_means[1] / responses[1]
            if np.isfinite(responses[1]) and responses[1] > 0
            else np.nan,
        )
        avg_powers = update_power_history(widget, powers, update_history)
        try:
            avg_frames = max(1, int(getattr(widget.module, "scope_power_avg_frames", 30)))
        except Exception:
            avg_frames = 30

        raw_entries: list[str] = []
        offset_entries: list[str] = []
        shown_entries: list[str] = []
        response_entries: list[str] = []
        power_entries: list[str] = []
        avg_power_entries: list[str] = []
        active = (bool(widget.module.ch1_active), bool(widget.module.ch2_active))
        for idx, is_active in enumerate(active):
            if not is_active:
                continue
            label = f"CH{idx + 1}"
            raw_entries.append(f"{label} {format_mean(raw_means[idx])}")
            offset_entries.append(f"{label} {format_mean(offsets[idx])}")
            shown_entries.append(f"{label} {format_mean(shown_means[idx])}")
            response_entries.append(f"{label} {format_response(responses[idx])}")
            power_entries.append(f"{label} {format_power_w(powers[idx])}")
            avg_power_entries.append(f"{label} {format_power_w(avg_powers[idx])}")
        if widget.module.ch_math_active:
            shown_entries.append("MATH shown")

        if raw_entries:
            lines = [f"raw mean: {' | '.join(raw_entries)}"]
            if zero_enabled:
                lines.append(f"offset: {' | '.join(offset_entries)}")
                lines.append(f"shown mean: {' | '.join(shown_entries)}")
            else:
                lines.append("zero: off")
            lines.append(f"response: {' | '.join(response_entries)}")
            lines.append(f"P inst: {' | '.join(power_entries)}")
            lines.append(f"P avg({avg_frames}f): {' | '.join(avg_power_entries)}")
        else:
            lines = ["mean: no active channel"]
        text = "\n".join(lines)
        set_active_means(widget, text)
        try:
            if getattr(widget, "mean_text_item", None) is not None:
                widget.mean_text_item.setText(text)
                update_mean_text_position(widget)
        except Exception:
            pass

    def display_curve_with_active_means(self, list_of_arrays):
        times, curves = list_of_arrays
        ch1 = np.asarray(curves[0], dtype=float)
        ch2 = np.asarray(curves[1], dtype=float)
        self._daily_note_last_raw_scope_curves = (ch1, ch2)
        if bool(getattr(self.module, "scope_zero_enabled", False)):
            ch1_display = ch1 - float(getattr(self.module, "ch1_zero_offset_v", 0.0))
            ch2_display = ch2 - float(getattr(self.module, "ch2_zero_offset_v", 0.0))
            result = original_display_curve(
                self,
                [times, np.asarray((ch1_display, ch2_display), dtype=float)],
            )
        else:
            result = original_display_curve(self, list_of_arrays)
        refresh_zero_overlay(self, update_history=True)
        return result

    ScopeWidget.init_gui = init_gui_with_active_means
    ScopeWidget.display_curve = display_curve_with_active_means
    ScopeWidget._daily_note_scope_mean_display_patched = True


def spectrum_power_correction_state(spectrum: Any | None = None) -> dict[str, Any]:
    from pyrpl.software_modules.spectrum_analyzer import SpectrumAnalyzer

    enabled = bool(
        getattr(SpectrumAnalyzer, "_daily_note_power_correction_enabled", True)
    )
    highz = float(getattr(SpectrumAnalyzer, "_daily_note_highz_correction_db", 0.0))
    if spectrum is not None:
        gain = float(getattr(spectrum, "external_gain_db", 0.0))
    else:
        gain = float(
            getattr(SpectrumAnalyzer, "_daily_note_external_gain_default_db", 0.0)
        )
    load = float(getattr(SpectrumAnalyzer, "_daily_note_spectrum_load_ohm", 50.0))
    total = highz + gain if enabled else 0.0
    return {
        "ok": True,
        "load_ohm": load,
        "enabled": enabled,
        "highz_correction_db": highz,
        "external_gain_db": gain,
        "total_subtracted_db": total,
        "applies_to_units": ["dBm", "dBm/Hz"],
    }


def spectrum_vpk2_to_display_dbm_per_hz(
    vpk2: np.ndarray,
    rbw_hz: float,
    correction_state: dict[str, Any],
) -> np.ndarray:
    """Convert PyRPL Vpk^2 spectrum data to corrected display dBm/Hz."""
    if rbw_hz <= 0:
        raise ValueError("rbw_hz must be positive")
    load_ohm = float(correction_state["load_ohm"])
    if load_ohm <= 0:
        raise ValueError("load_ohm must be positive")
    values = 10.0 * np.log10(
        np.abs(vpk2) / (2.0 * load_ohm * 1e-3 * rbw_hz) + np.finfo(float).tiny
    )
    values -= float(correction_state.get("total_subtracted_db", 0.0))
    return values


def networkanalyzer_response_to_dbm(
    response: np.ndarray | complex | float,
    *,
    drive_vpk: float,
    load_ohm: float,
    correction_enabled: bool,
    highz_correction_db: float,
    external_gain_db: float,
) -> np.ndarray:
    """Convert NA coherent voltage transfer response to corrected dBm.

    PyRPL's network analyzer stores a complex voltage ratio:
    V_read / V_drive. Its GUI normally plots 20log10(abs(response)).
    This helper multiplies by the configured drive Vpk, converts to Vrms,
    then reports coherent power in dBm for the chosen equivalent load.
    """
    if load_ohm <= 0:
        raise ValueError("load_ohm must be positive")
    drive_vpk = max(float(abs(drive_vpk)), np.finfo(float).tiny)
    values = (
        20.0 * np.log10(np.abs(response) + np.finfo(float).tiny)
        + 20.0 * np.log10(drive_vpk / math.sqrt(2.0))
        - 10.0 * math.log10(load_ohm)
        + 30.0
    )
    if correction_enabled:
        values -= highz_correction_db + external_gain_db
    return values


def patch_networkanalyzer_dbm_display(
    *,
    enabled: bool = True,
    load_ohm: float = 50.0,
    highz_correction_db: float = 20.0 * math.log10(2.0),
    external_gain_db: float = 0.0,
) -> dict[str, Any]:
    """Display PyRPL network-analyzer magnitude as coherent dBm."""
    from pyrpl.attributes import BoolProperty, FloatProperty
    from pyrpl.software_modules.network_analyzer import NetworkAnalyzer

    if load_ohm <= 0:
        raise ValueError("--networkanalyzer-load-ohm must be positive")
    if not all(np.isfinite(value) for value in (highz_correction_db, external_gain_db)):
        raise ValueError("Network-analyzer display correction values must be finite")

    if not hasattr(NetworkAnalyzer, "dbm_display_enabled"):
        enabled_property = BoolProperty(
            default=enabled,
            doc="Display network-analyzer magnitude as coherent dBm instead of transfer dB.",
        )
        enabled_property.name = "dbm_display_enabled"
        NetworkAnalyzer.dbm_display_enabled = enabled_property

    if not hasattr(NetworkAnalyzer, "dbm_load_ohm"):
        load_property = FloatProperty(
            default=load_ohm,
            min=1e-9,
            max=1e12,
            increment=1.0,
            doc="Equivalent load resistance for coherent network-analyzer dBm display.",
        )
        load_property.name = "dbm_load_ohm"
        NetworkAnalyzer.dbm_load_ohm = load_property

    if not hasattr(NetworkAnalyzer, "dbm_highz_correction_db"):
        highz_property = FloatProperty(
            default=highz_correction_db,
            min=-200.0,
            max=200.0,
            increment=1.0,
            doc="RP high-impedance input correction subtracted in network-analyzer dBm mode.",
        )
        highz_property.name = "dbm_highz_correction_db"
        NetworkAnalyzer.dbm_highz_correction_db = highz_property

    if not hasattr(NetworkAnalyzer, "external_gain_db"):
        gain_property = FloatProperty(
            default=external_gain_db,
            min=-200.0,
            max=200.0,
            increment=1.0,
            doc="External RF-chain gain subtracted in network-analyzer dBm mode.",
        )
        gain_property.name = "external_gain_db"
        NetworkAnalyzer.external_gain_db = gain_property

    desired_attrs = [
        "dbm_display_enabled",
        "dbm_load_ohm",
        "dbm_highz_correction_db",
        "external_gain_db",
    ]
    gui_attrs = list(NetworkAnalyzer._gui_attributes)
    try:
        index = gui_attrs.index("amplitude") + 1
    except ValueError:
        index = len(gui_attrs)
    for attr in desired_attrs:
        if attr not in gui_attrs:
            gui_attrs.insert(index, attr)
            index += 1
    NetworkAnalyzer._gui_attributes = gui_attrs

    if not getattr(NetworkAnalyzer, "_daily_note_dbm_display_patched", False):
        from pyrpl.widgets.module_widgets.na_widget import NaWidget

        original_init_gui = NaWidget.init_gui
        original_update_attribute_by_name = NaWidget.update_attribute_by_name

        def update_na_magnitude_title(widget):
            if bool(getattr(widget.module, "dbm_display_enabled", False)):
                widget.plot_item.setTitle("Magnitude (dBm)")
            else:
                widget.plot_item.setTitle("Magnitude (dB)")

        def init_gui_with_dbm_display(self):
            original_init_gui(self)
            update_na_magnitude_title(self)

        def magnitude_with_dbm_display(self, data):
            if bool(getattr(self.module, "dbm_display_enabled", False)):
                return networkanalyzer_response_to_dbm(
                    data,
                    drive_vpk=float(getattr(self.module, "amplitude", 0.0)),
                    load_ohm=float(getattr(self.module, "dbm_load_ohm", 50.0)),
                    correction_enabled=True,
                    highz_correction_db=float(
                        getattr(self.module, "dbm_highz_correction_db", 0.0)
                    ),
                    external_gain_db=float(getattr(self.module, "external_gain_db", 0.0)),
                )
            return 20.0 * np.log10(np.abs(data) + np.finfo(float).tiny)

        def update_attribute_by_name_with_dbm(self, name, new_value_list):
            original_update_attribute_by_name(self, name, new_value_list)
            if name in {
                "dbm_display_enabled",
                "dbm_load_ohm",
                "dbm_highz_correction_db",
                "external_gain_db",
                "amplitude",
            }:
                update_na_magnitude_title(self)
                try:
                    for chunk_index in range(len(self.chunks)):
                        self.update_chunk(chunk_index)
                except Exception:
                    pass

        NaWidget.init_gui = init_gui_with_dbm_display
        NaWidget._magnitude = magnitude_with_dbm_display
        NaWidget.update_attribute_by_name = update_attribute_by_name_with_dbm
        NetworkAnalyzer._daily_note_dbm_display_patched = True

    NetworkAnalyzer._daily_note_dbm_display_default_enabled = enabled
    NetworkAnalyzer._daily_note_dbm_load_ohm = load_ohm
    NetworkAnalyzer._daily_note_dbm_highz_correction_db = highz_correction_db
    NetworkAnalyzer._daily_note_external_gain_default_db = external_gain_db
    return networkanalyzer_power_state()


def networkanalyzer_power_state(networkanalyzer: Any | None = None) -> dict[str, Any]:
    from pyrpl.software_modules.network_analyzer import NetworkAnalyzer

    if networkanalyzer is not None:
        enabled = bool(getattr(networkanalyzer, "dbm_display_enabled", True))
        load = float(getattr(networkanalyzer, "dbm_load_ohm", 50.0))
        highz = float(getattr(networkanalyzer, "dbm_highz_correction_db", 0.0))
        gain = float(getattr(networkanalyzer, "external_gain_db", 0.0))
    else:
        enabled = bool(
            getattr(NetworkAnalyzer, "_daily_note_dbm_display_default_enabled", True)
        )
        load = float(getattr(NetworkAnalyzer, "_daily_note_dbm_load_ohm", 50.0))
        highz = float(
            getattr(NetworkAnalyzer, "_daily_note_dbm_highz_correction_db", 0.0)
        )
        gain = float(
            getattr(NetworkAnalyzer, "_daily_note_external_gain_default_db", 0.0)
        )
    return {
        "ok": True,
        "display_unit": "dBm" if enabled else "dB",
        "enabled": enabled,
        "load_ohm": load,
        "highz_correction_db": highz,
        "external_gain_db": gain,
        "total_subtracted_db": highz + gain if enabled else 0.0,
        "drive_amplitude": "networkanalyzer.amplitude, interpreted as Vpk",
    }


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
    def __init__(
        self,
        pyrpl_instance: Any,
        allow_risky: bool = False,
        metadata: dict[str, Any] | None = None,
    ):
        self.p = pyrpl_instance
        self.allow_risky = allow_risky
        self.metadata = metadata or {}
        self.started_at = time.time()
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
        if hasattr(self.p.rp, module_name):
            module = getattr(self.p.rp, module_name)
        else:
            module = getattr(self.p, module_name)
        return module, attr

    def get_param(self, dotted: str) -> dict[str, Any]:
        if not dotted.startswith(READABLE_PREFIXES):
            raise ValueError(f"Reading {dotted!r} is not in the bridge allowlist")
        module, attr = self.resolve(dotted)
        value = json_ready(getattr(module, attr))
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

    def get_spectrum_power_correction(self) -> dict[str, Any]:
        return spectrum_power_correction_state(self.p.spectrumanalyzer)

    def get_networkanalyzer_power_display(self) -> dict[str, Any]:
        return networkanalyzer_power_state(self.p.networkanalyzer)

    def acquisition_settings(self) -> dict[str, Any]:
        spectrum = self.p.spectrumanalyzer
        network = self.p.networkanalyzer
        span_options = [float(v) for v in spectrum.spans]
        spectrum_rbw_options = [
            float(v) for v in spectrum.__class__.rbw.valid_frequencies(spectrum)
        ]
        network_rbw_options = [
            float(v) for v in network.__class__.rbw.valid_frequencies(network)
        ]
        spectrum_pairs = [
            {"span_hz": span, "rbw_hz": spectrum_rbw_options[idx]}
            for idx, span in enumerate(span_options)
            if idx < len(spectrum_rbw_options)
        ]
        return {
            "ok": True,
            "spectrum": {
                "span_hz": float(spectrum.span),
                "rbw_hz": float(spectrum.rbw),
                "trace_average": int(spectrum.trace_average),
                "window": str(spectrum.window),
                "baseband": bool(spectrum.baseband),
                "data_length": int(spectrum.data_length),
                "display_points": int(spectrum._real_points),
                "span_rbw_options": spectrum_pairs,
            },
            "network": {
                "amplitude_vpk": float(network.amplitude),
                "start_freq_hz": float(network.start_freq),
                "stop_freq_hz": float(network.stop_freq),
                "points": int(network.points),
                "rbw_hz": float(network.rbw),
                "average_per_point": int(network.average_per_point),
                "trace_average": int(network.trace_average),
                "rbw_options": network_rbw_options,
            },
        }

    def stop_acquisitions(self) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        for name in ("spectrumanalyzer", "networkanalyzer", "scope"):
            module = getattr(self.p, name, None)
            if module is None:
                continue
            module_steps: list[dict[str, Any]] = []
            for method_name in ("stop", "pause"):
                method = getattr(module, method_name, None)
                if callable(method):
                    try:
                        method()
                        module_steps.append({"action": method_name, "ok": True})
                        break
                    except Exception as exc:
                        module_steps.append({"action": method_name, "ok": False, "error": repr(exc)})
            for attr_name, value in (
                ("running", False),
                ("run_continuous", False),
                ("continuous", False),
            ):
                if hasattr(module, attr_name):
                    try:
                        setattr(module, attr_name, value)
                        module_steps.append({"action": f"set {attr_name}", "ok": True, "value": value})
                    except Exception as exc:
                        module_steps.append({"action": f"set {attr_name}", "ok": False, "error": repr(exc)})
            steps.append({"module": name, "steps": module_steps})
        return {"ok": True, "message": "best-effort acquisition stop requested", "steps": steps}

    def health(self) -> dict[str, Any]:
        pyrpl_module = sys.modules.get("pyrpl")
        return {
            "ok": True,
            "message": "pyrpl_live_bridge is running",
            "bridge": {
                "pid": os.getpid(),
                "python_executable": sys.executable,
                "pyrpl_version": getattr(pyrpl_module, "__version__", None),
                "pyrpl_file": getattr(pyrpl_module, "__file__", None),
                "started_at": self.started_at,
                "uptime_s": time.time() - self.started_at,
                "allow_risky": self.allow_risky,
                **self.metadata,
            },
            "spectrum_power_correction": self.get_spectrum_power_correction(),
            "networkanalyzer_power_display": self.get_networkanalyzer_power_display(),
            "safe_params": sorted(SAFE_PARAMS),
            "supports_shutdown": True,
        }

    def set_spectrum_power_correction(
        self,
        enabled: bool | None = None,
        highz_correction_db: float | None = None,
        external_gain_db: float | None = None,
        load_ohm: float | None = None,
    ) -> dict[str, Any]:
        spectrum = self.p.spectrumanalyzer
        state = spectrum_power_correction_state(spectrum)
        patch_spectrum_power_units(
            load_ohm=state["load_ohm"] if load_ohm is None else load_ohm,
            correction_enabled=state["enabled"] if enabled is None else enabled,
            highz_correction_db=(
                state["highz_correction_db"]
                if highz_correction_db is None
                else highz_correction_db
            ),
            external_gain_db=(
                state["external_gain_db"]
                if external_gain_db is None
                else external_gain_db
            ),
        )
        if external_gain_db is not None:
            spectrum.external_gain_db = external_gain_db
        return spectrum_power_correction_state(spectrum)

    def capture_scope(
        self,
        tag: str = "scope_capture",
        timeout: float = 5.0,
        make_plot: bool = True,
        save: bool = True,
        inline: bool = False,
        max_points: int = 1500,
    ) -> dict[str, Any]:
        scope = self.p.rp.scope
        curve = scope.single(timeout=timeout)
        ch1 = np.asarray(curve[0], dtype=float)
        ch2 = np.asarray(curve[1], dtype=float)
        times = np.asarray(scope.times, dtype=float)
        safe_tag = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)
        path = None
        if save:
            path = RESULTS_DIR / f"{safe_tag}.npz"
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
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
        plot_path = None
        if make_plot and save:
            plot_path = RESULTS_DIR / f"{safe_tag}_lockpoint.png"
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
            reason = "plot=false" if not make_plot else "save=false"
            plot_result = {"ok": False, "skipped": True, "reason": reason}
        payload = {
            "ok": True,
            "path": str(path) if save else None,
            "plot_path": str(plot_path) if plot_result.get("ok") else None,
            "plot": plot_result,
            "n": int(len(times)),
            "input1": str(scope.input1),
            "input2": str(scope.input2),
            "duration": float(scope.duration),
            "trigger_source": str(scope.trigger_source),
            "analysis": analysis,
        }
        if inline:
            limit = max(100, int(max_points))
            step = max(1, int(math.ceil(len(times) / limit)))
            payload["trace"] = {
                "t": times[::step].astype(float).tolist(),
                "ch1": ch1[::step].astype(float).tolist(),
                "ch2": ch2[::step].astype(float).tolist(),
                "decimation": step,
            }
        return payload

    def capture_spectrum(
        self,
        tag: str = "spectrum_capture",
        timeout: float = 15.0,
        load_ohm: float = 50.0,
        save_csv: bool = False,
        save_npz: bool = True,
        inline: bool = False,
        max_points: int = 1500,
        acquire: bool = True,
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        if load_ohm <= 0:
            raise ValueError("load_ohm must be positive")
        spectrum = self.p.spectrumanalyzer
        data = None
        freqs = None
        source = "single"
        if not acquire:
            cached = getattr(spectrum, "data_avg", None)
            cached_x = getattr(spectrum, "data_x", None)
            if cached is not None and cached_x is not None:
                try:
                    cached_arr = np.asarray(cached, dtype=float)
                    cached_freqs = np.asarray(cached_x, dtype=float)
                    if cached_arr.size and cached_freqs.size:
                        data = cached_arr
                        freqs = cached_freqs
                        source = "cache"
                except Exception:
                    data = None
                    freqs = None
        if data is None or freqs is None:
            data = np.asarray(spectrum.single(timeout=timeout), dtype=float)
            freqs = np.asarray(spectrum.data_x, dtype=float)
        rbw = float(spectrum.rbw)
        if data.ndim == 1:
            input1_vpk2 = data
        else:
            input1_vpk2 = np.asarray(data[0], dtype=float)
        display_power_correction = spectrum_power_correction_state(spectrum)
        input1_dbm_per_hz = spectrum_vpk2_to_display_dbm_per_hz(
            input1_vpk2,
            rbw,
            display_power_correction,
        )

        output_root = Path(output_dir).expanduser() if output_dir else RESULTS_DIR.resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        safe_tag = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)
        npz_path = output_root / f"{safe_tag}.npz"
        csv_path = output_root / f"{safe_tag}.csv" if save_csv else None
        meta_path = output_root / f"{safe_tag}.json"

        arrays = {
            "frequency_hz": freqs,
            "input1_dbm_per_hz": input1_dbm_per_hz,
        }
        if save_npz:
            np.savez(npz_path, **arrays)

        if csv_path is not None:
            with csv_path.open("w", encoding="utf-8", newline="") as fh:
                fh.write("frequency_hz,input1_dbm_per_hz\n")
                for idx, freq in enumerate(freqs):
                    fh.write(
                        f"{float(freq):.12g},"
                        f"{float(input1_dbm_per_hz[idx]):.12g}\n"
                    )

        finite = (
            np.isfinite(input1_vpk2)
            & np.isfinite(input1_dbm_per_hz)
            & np.isfinite(freqs)
        )
        band = finite & (freqs > 0)
        summary = {}
        if np.any(band):
            summary["input1_dbm_per_hz"] = {
                "median": float(np.nanmedian(input1_dbm_per_hz[band])),
                "min": float(np.nanmin(input1_dbm_per_hz[band])),
                "max": float(np.nanmax(input1_dbm_per_hz[band])),
            }

        meta = {
            "ok": True,
            "tag": safe_tag,
            "path": str(npz_path) if save_npz else None,
            "output_dir": str(output_root),
            "csv_path": str(csv_path) if csv_path is not None else None,
            "save_csv": save_csv,
            "save_npz": save_npz,
            "metadata_path": str(meta_path) if save_npz else None,
            "saved_arrays": ["frequency_hz", "input1_dbm_per_hz"],
            "raw_channel": "input1",
            "raw_unit_not_saved": "Vpk^2, from PyRPL spectrumanalyzer.single() before display-unit conversion",
            "display_unit_saved": "dBm/Hz",
            "conversion_note": {
                "vpk_to_vrms": "Vrms = Vpk / sqrt(2)",
                "spectrum_vpk2_to_dbm_per_hz": (
                    "uncorrected_dbm_per_hz = 10*log10((Vpk^2 / (2*R_ohm)) / RBW_Hz / 1e-3); "
                    "saved input1_dbm_per_hz = uncorrected_dbm_per_hz - total_subtracted_db when correction is enabled"
                ),
                "inverse_dbm_per_hz_to_vpk2": (
                    "Vpk^2 = 2*R_ohm*RBW_Hz*1e-3*10^((saved_dbm_per_hz + total_subtracted_db)/10) "
                    "when correction is enabled"
                ),
            },
            "display_power_correction": display_power_correction,
            "display_load_ohm": display_power_correction["load_ohm"],
            "source": source,
            "rbw_hz": rbw,
            "baseband": bool(spectrum.baseband),
            "span_hz": float(spectrum.span),
            "center_hz": float(spectrum.center),
            "window": str(spectrum.window),
            "trace_average": int(spectrum.trace_average),
            "current_avg": int(spectrum.current_avg),
            "input": str(spectrum.input),
            "input1_baseband": str(spectrum.input1_baseband),
            "input2_baseband": str(spectrum.input2_baseband),
            "display_input1_baseband": bool(spectrum.display_input1_baseband),
            "display_input2_baseband": bool(spectrum.display_input2_baseband),
            "n": int(len(freqs)),
            "labels": ["frequency_hz", "input1_dbm_per_hz"],
            "summary": summary,
        }
        if inline:
            limit = max(100, int(max_points))
            step = max(1, int(math.ceil(len(freqs) / limit)))
            meta["trace"] = {
                "frequency_hz": freqs[::step].astype(float).tolist(),
                "input1_dbm_per_hz": input1_dbm_per_hz[::step].astype(float).tolist(),
                "decimation": step,
            }
        if save_npz:
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta

    def capture_network_analyzer(
        self,
        tag: str = "network_analyzer_preview",
        timeout: float = 20.0,
        save_npz: bool = True,
        inline: bool = True,
        max_points: int = 1200,
        acquire: bool = True,
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        na = self.p.networkanalyzer
        data = None
        freqs = None
        source = "single"
        if not acquire:
            cached = getattr(na, "data_avg", None)
            cached_x = getattr(na, "data_x", None)
            if cached is not None and cached_x is not None:
                try:
                    cached_arr = np.asarray(cached)
                    cached_freqs = np.asarray(cached_x, dtype=float)
                    if cached_arr.size and cached_freqs.size:
                        data = cached_arr
                        freqs = cached_freqs
                        source = "cache"
                except Exception:
                    data = None
                    freqs = None
        if data is None or freqs is None:
            data = np.asarray(na.single(timeout=timeout))
            freqs = np.asarray(na.frequencies, dtype=float)
        if data.ndim > 1:
            response = np.asarray(data[0], dtype=complex)
        else:
            response = np.asarray(data, dtype=complex)
        n = min(len(freqs), len(response))
        freqs = freqs[:n]
        response = response[:n]
        magnitude = np.abs(response)
        magnitude_db = 20.0 * np.log10(np.maximum(magnitude, 1e-300))
        na_power = networkanalyzer_power_state(na)
        magnitude_dbm = networkanalyzer_response_to_dbm(
            response,
            drive_vpk=float(na.amplitude),
            load_ohm=float(na_power["load_ohm"]),
            correction_enabled=bool(na_power["enabled"]),
            highz_correction_db=float(na_power["highz_correction_db"]),
            external_gain_db=float(na_power["external_gain_db"]),
        )
        phase_deg = np.angle(response) * 180.0 / np.pi
        finite = (
            np.isfinite(freqs)
            & np.isfinite(magnitude_db)
            & np.isfinite(magnitude_dbm)
            & np.isfinite(phase_deg)
        )
        summary = {}
        if np.any(finite):
            summary = {
                "magnitude_dbm": {
                    "median": float(np.nanmedian(magnitude_dbm[finite])),
                    "min": float(np.nanmin(magnitude_dbm[finite])),
                    "max": float(np.nanmax(magnitude_dbm[finite])),
                },
                "phase_deg": {
                    "median": float(np.nanmedian(phase_deg[finite])),
                    "min": float(np.nanmin(phase_deg[finite])),
                    "max": float(np.nanmax(phase_deg[finite])),
                },
            }
        safe_tag = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)
        output_root = Path(output_dir).expanduser() if output_dir else RESULTS_DIR.resolve()
        npz_path = output_root / f"{safe_tag}.npz"
        meta_path = output_root / f"{safe_tag}.json"
        if save_npz:
            output_root.mkdir(parents=True, exist_ok=True)
            np.savez(
                npz_path,
                frequency_hz=freqs,
                magnitude_dbm=magnitude_dbm,
                phase_deg=phase_deg,
            )
        payload = {
            "ok": True,
            "tag": safe_tag,
            "path": str(npz_path) if save_npz else None,
            "output_dir": str(output_root),
            "metadata_path": str(meta_path) if save_npz else None,
            "save_npz": save_npz,
            "saved_arrays": [
                "frequency_hz",
                "magnitude_dbm",
                "phase_deg",
            ],
            "raw_unit_not_saved": "complex voltage response ratio from PyRPL networkanalyzer.single()",
            "display_unit_saved": "dBm when networkanalyzer dBm display is enabled, otherwise computed alongside dB",
            "conversion_note": {
                "response_to_magnitude_db": "magnitude_db = 20*log10(abs(response_complex))",
                "response_to_received_vpk": "received_Vpk = abs(response_complex) * networkanalyzer.amplitude_Vpk",
                "vpk_to_vrms": "Vrms = Vpk / sqrt(2)",
                "received_vpk_to_dbm": (
                    "uncorrected_dBm = 10*log10((received_Vpk^2 / (2*R_ohm)) / 1e-3); "
                    "saved magnitude_dbm = uncorrected_dBm - total_subtracted_db when correction is enabled"
                ),
                "phase_deg": "phase_deg = angle(response_complex)*180/pi, wrapped to -180..+180 deg",
            },
            "n": int(n),
            "input": str(na.input),
            "output_direct": str(na.output_direct),
            "source": source,
            "start_freq_hz": float(na.start_freq),
            "stop_freq_hz": float(na.stop_freq),
            "rbw_hz": float(na.rbw),
            "points": int(na.points),
            "trace_average": int(na.trace_average),
            "average_per_point": int(na.average_per_point),
            "amplitude_v": float(na.amplitude),
            "amplitude_unit": "Vpk",
            "logscale": bool(na.logscale),
            "power_display": na_power,
            "summary": summary,
        }
        if inline:
            limit = max(100, int(max_points))
            step = max(1, int(math.ceil(n / limit)))
            payload["trace"] = {
                "frequency_hz": freqs[::step].astype(float).tolist(),
                "magnitude_dbm": magnitude_dbm[::step].astype(float).tolist(),
                "phase_deg": phase_deg[::step].astype(float).tolist(),
                "decimation": step,
            }
        if save_npz:
            meta_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload

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
                    payload = bridge.health()
                elif parsed.path == "/shutdown":
                    payload = {
                        "ok": True,
                        "message": "bridge shutdown requested",
                        "bridge": bridge.health().get("bridge"),
                    }
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
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
                    save = qs.get("save", ["true"])[0].strip().lower() not in {
                        "0",
                        "false",
                        "no",
                        "off",
                    }
                    inline = qs.get("inline", ["false"])[0].strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "on",
                    }
                    max_points = int(qs.get("max_points", ["1500"])[0])
                    payload = bridge.submit(
                        lambda: bridge.capture_scope(
                            tag,
                            timeout,
                            make_plot,
                            save=save,
                            inline=inline,
                            max_points=max_points,
                        ),
                        wait_timeout=max(10.0, timeout + 5.0),
                    )
                elif parsed.path == "/acquisition/settings":
                    payload = bridge.submit(lambda: bridge.acquisition_settings())
                elif parsed.path == "/acquisition/stop":
                    payload = bridge.submit(lambda: bridge.stop_acquisitions())
                elif parsed.path == "/spectrum/single":
                    tag = qs.get("tag", ["spectrum_capture"])[0]
                    timeout = float(qs.get("timeout", ["15"])[0])
                    load_ohm = float(qs.get("load_ohm", ["50"])[0])
                    save_csv = qs.get("save_csv", ["false"])[0].strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "on",
                    }
                    save_npz = qs.get("save", ["true"])[0].strip().lower() not in {
                        "0",
                        "false",
                        "no",
                        "off",
                    }
                    inline = qs.get("inline", ["false"])[0].strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "on",
                    }
                    acquire = qs.get("acquire", ["true"])[0].strip().lower() not in {
                        "0",
                        "false",
                        "no",
                        "off",
                    }
                    max_points = int(qs.get("max_points", ["1500"])[0])
                    output_dir = qs.get("output_dir", [None])[0]
                    payload = bridge.submit(
                        lambda: bridge.capture_spectrum(
                            tag,
                            timeout,
                            load_ohm,
                            save_csv,
                            save_npz=save_npz,
                            inline=inline,
                            max_points=max_points,
                            acquire=acquire,
                            output_dir=output_dir,
                        ),
                        wait_timeout=max(20.0, timeout + 10.0),
                    )
                elif parsed.path == "/networkanalyzer/single":
                    tag = qs.get("tag", ["network_analyzer_preview"])[0]
                    timeout = float(qs.get("timeout", ["20"])[0])
                    save_npz = qs.get("save", ["true"])[0].strip().lower() not in {
                        "0",
                        "false",
                        "no",
                        "off",
                    }
                    inline = qs.get("inline", ["true"])[0].strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "on",
                    }
                    acquire = qs.get("acquire", ["true"])[0].strip().lower() not in {
                        "0",
                        "false",
                        "no",
                        "off",
                    }
                    max_points = int(qs.get("max_points", ["1200"])[0])
                    output_dir = qs.get("output_dir", [None])[0]
                    payload = bridge.submit(
                        lambda: bridge.capture_network_analyzer(
                            tag,
                            timeout,
                            save_npz=save_npz,
                            inline=inline,
                            max_points=max_points,
                            acquire=acquire,
                            output_dir=output_dir,
                        ),
                        wait_timeout=max(30.0, timeout + 10.0),
                    )
                elif parsed.path == "/spectrum/power_correction":
                    enabled = (
                        parse_bool(qs["enabled"][0])
                        if "enabled" in qs
                        else None
                    )
                    highz_correction_db = (
                        float(qs["highz_correction_db"][0])
                        if "highz_correction_db" in qs
                        else None
                    )
                    external_gain_db = (
                        float(qs["external_gain_db"][0])
                        if "external_gain_db" in qs
                        else None
                    )
                    load_ohm = (
                        float(qs["load_ohm"][0])
                        if "load_ohm" in qs
                        else None
                    )
                    if any(
                        value is not None
                        for value in (
                            enabled,
                            highz_correction_db,
                            external_gain_db,
                            load_ohm,
                        )
                    ):
                        payload = bridge.submit(
                            lambda: bridge.set_spectrum_power_correction(
                                enabled=enabled,
                                highz_correction_db=highz_correction_db,
                                external_gain_db=external_gain_db,
                                load_ohm=load_ohm,
                            )
                        )
                    else:
                        payload = bridge.submit(bridge.get_spectrum_power_correction)
                elif parsed.path == "/networkanalyzer/power_display":
                    if "enabled" in qs:
                        payload = bridge.submit(
                            lambda: bridge.set_param(
                                "networkanalyzer.dbm_display_enabled", qs["enabled"][0]
                            )
                        )
                    elif "external_gain_db" in qs:
                        payload = bridge.submit(
                            lambda: bridge.set_param(
                                "networkanalyzer.external_gain_db",
                                qs["external_gain_db"][0],
                            )
                        )
                    elif "highz_correction_db" in qs:
                        payload = bridge.submit(
                            lambda: bridge.set_param(
                                "networkanalyzer.dbm_highz_correction_db",
                                qs["highz_correction_db"][0],
                            )
                        )
                    elif "load_ohm" in qs:
                        payload = bridge.submit(
                            lambda: bridge.set_param(
                                "networkanalyzer.dbm_load_ohm", qs["load_ohm"][0]
                            )
                        )
                    else:
                        payload = bridge.submit(bridge.get_networkanalyzer_power_display)
                else:
                    payload = {
                        "ok": True,
                        "usage": {
                            "health": "/health",
                            "shutdown": "/shutdown",
                            "get": "/get?param=scope.duration",
                            "set": "/set?param=scope.duration&value=0.1",
                            "scope_single": "/scope/single?tag=test",
                            "spectrum_single": "/spectrum/single?tag=test",
                            "networkanalyzer_single": "/networkanalyzer/single?tag=test",
                            "spectrum_power_correction": "/spectrum/power_correction?external_gain_db=23",
                            "networkanalyzer_power_display": "/networkanalyzer/power_display",
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

    networkanalyzer_external_gain_db = (
        args.spectrum_external_gain_db
        if args.networkanalyzer_external_gain_db is None
        else args.networkanalyzer_external_gain_db
    )
    patch_spectrum_power_units(
        load_ohm=args.spectrum_load_ohm,
        correction_enabled=args.spectrum_power_correction_enabled,
        highz_correction_db=args.spectrum_highz_correction_db,
        external_gain_db=args.spectrum_external_gain_db,
    )
    patch_networkanalyzer_dbm_display(
        enabled=args.networkanalyzer_dbm_display_enabled,
        load_ohm=args.networkanalyzer_load_ohm,
        highz_correction_db=args.networkanalyzer_highz_correction_db,
        external_gain_db=networkanalyzer_external_gain_db,
    )
    if not args.headless:
        patch_scope_mean_display()
    print(
        f"Starting PyRPL {pyrpl.__version__} config={args.config!r} "
        f"headless={args.headless} loglevel={args.loglevel}",
        flush=True,
    )
    print(f"Connecting to Red Pitaya at {args.hostname}", flush=True)
    p = pyrpl.Pyrpl(
        config=args.config,
        hostname=args.hostname,
        gui=not args.headless,
        loglevel=args.loglevel,
    )
    print("PyRPL connection returned; applying bridge display settings", flush=True)
    p.spectrumanalyzer.external_gain_db = args.spectrum_external_gain_db
    p.networkanalyzer.dbm_display_enabled = args.networkanalyzer_dbm_display_enabled
    p.networkanalyzer.dbm_load_ohm = args.networkanalyzer_load_ohm
    p.networkanalyzer.dbm_highz_correction_db = args.networkanalyzer_highz_correction_db
    p.networkanalyzer.external_gain_db = networkanalyzer_external_gain_db
    if not args.headless:
        try:
            p.rp.scope.ch1_power_response_v_per_w = args.scope_ch1_response_v_per_w
            p.rp.scope.ch2_power_response_v_per_w = args.scope_ch2_response_v_per_w
            p.rp.scope.ch1_zero_offset_v = args.scope_ch1_zero_offset_v
            p.rp.scope.ch2_zero_offset_v = args.scope_ch2_zero_offset_v
            p.rp.scope.scope_zero_enabled = args.scope_zero_enabled
        except Exception as exc:
            print(f"WARN could not apply scope display settings: {exc}", flush=True)
    power_correction = spectrum_power_correction_state(p.spectrumanalyzer)
    print(f"Spectrum dBm units use {args.spectrum_load_ohm:g} ohm equivalent load", flush=True)
    print(
        "Spectrum dBm correction: "
        f"enabled={power_correction['enabled']} "
        f"high-Z={power_correction['highz_correction_db']:.3f} dB "
        f"external_gain={power_correction['external_gain_db']:.3f} dB "
        f"total_subtracted={power_correction['total_subtracted_db']:.3f} dB",
        flush=True,
    )
    networkanalyzer_display = networkanalyzer_power_state(p.networkanalyzer)
    print(
        "Network analyzer magnitude display: "
        f"unit={networkanalyzer_display['display_unit']} "
        f"load={networkanalyzer_display['load_ohm']:.3g} ohm "
        f"high-Z={networkanalyzer_display['highz_correction_db']:.3f} dB "
        f"external_gain={networkanalyzer_display['external_gain_db']:.3f} dB "
        f"total_subtracted={networkanalyzer_display['total_subtracted_db']:.3f} dB",
        flush=True,
    )

    bridge = Bridge(
        p,
        allow_risky=args.allow_risky,
        metadata={
            "config": args.config,
            "hostname": args.hostname,
            "headless": args.headless,
            "listen_host": args.listen_host,
            "listen_port": args.listen_port,
            "loglevel": args.loglevel,
            "scope_ch1_response_v_per_w": args.scope_ch1_response_v_per_w,
            "scope_ch2_response_v_per_w": args.scope_ch2_response_v_per_w,
            "scope_zero_enabled": args.scope_zero_enabled,
            "scope_ch1_zero_offset_v": args.scope_ch1_zero_offset_v,
            "scope_ch2_zero_offset_v": args.scope_ch2_zero_offset_v,
            "networkanalyzer_dbm_display": networkanalyzer_display,
        },
    )
    server = ThreadingHTTPServer(
        (args.listen_host, args.listen_port), make_handler(bridge)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Bridge listening on http://{args.listen_host}:{args.listen_port}", flush=True)
    print("Initial safe write example: /set?param=scope.duration&value=0.1", flush=True)

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
