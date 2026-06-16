#!/usr/bin/env python3
"""Fit preliminary Q0/Q1 trends for large-scan resonance families."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import curve_fit

from data_paths import CAMPAIGN_ENV, CHIP_ENV, DATA_ROOT_ENV, default_campaign, default_chip, default_cavity_dir
from process_large_scan import normalize_transmission_with_baseline, read_large_scan_data

C_M_PER_S = 299_792_458.0


@dataclass
class QFitConfig:
    data_path: str
    family_points_csv: str
    depth_threshold: float
    start_nm: float
    center_nm: float
    stop_nm: float
    min_half_window_samples: int
    max_half_window_samples: int
    neighbor_window_fraction: float
    workers: int
    output_dir: str


def lorentzian_notch(x_mhz: np.ndarray, a: float, b: float, eta: float, gamma_mhz: float, x0_mhz: float) -> np.ndarray:
    dx = x_mhz - x0_mhz
    baseline = a + b * dx
    return baseline * (1.0 - eta * gamma_mhz**2 / (gamma_mhz**2 + dx**2))


def time_to_wavelength_nm(time_s: np.ndarray, start_nm: float, center_nm: float, stop_nm: float) -> np.ndarray:
    # The current large-scan trigger is at center_nm and scan speed is 2 nm/s for 1530-1570 nm / 20 s.
    return center_nm + (stop_nm - start_nm) / 20.0 * time_s


def wavelength_nm_to_freq_mhz(wavelength_nm: np.ndarray) -> np.ndarray:
    return C_M_PER_S / (wavelength_nm * 1e-9) / 1e6


def load_family_points(path: Path, depth_threshold: float) -> list[dict[str, float | str]]:
    rows: list[dict[str, float | str]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            depth = float(row["depth_1_minus_norm"])
            if depth < depth_threshold:
                continue
            parsed: dict[str, float | str] = {"family": row["family"]}
            for key, value in row.items():
                if key == "family":
                    continue
                if value == "":
                    parsed[key] = math.nan
                else:
                    try:
                        parsed[key] = float(value)
                    except ValueError:
                        parsed[key] = value
            rows.append(parsed)
    return rows


def choose_half_window(center_idx: int, all_indices: np.ndarray, config: QFitConfig) -> int:
    distances = np.abs(all_indices - center_idx)
    distances = distances[distances > 0]
    nearest = int(np.min(distances)) if len(distances) else config.max_half_window_samples * 2
    half = int(config.neighbor_window_fraction * nearest)
    return max(config.min_half_window_samples, min(config.max_half_window_samples, half))


def estimate_initial_gamma(x_mhz: np.ndarray, y: np.ndarray, center_i: int, eta0: float) -> float:
    if eta0 <= 0:
        return max(50.0, 0.02 * np.ptp(x_mhz))
    ymin = y[center_i]
    ybase = np.nanmedian(np.r_[y[: max(1, len(y) // 5)], y[-max(1, len(y) // 5) :]])
    half_level = ymin + 0.5 * (ybase - ymin)
    left = center_i
    while left > 0 and y[left] < half_level:
        left -= 1
    right = center_i
    while right < len(y) - 1 and y[right] < half_level:
        right += 1
    if right > left:
        return max(10.0, abs(x_mhz[right] - x_mhz[left]) / 2.0)
    return max(50.0, 0.02 * np.ptp(x_mhz))


def fit_one_mode(
    *,
    row: dict[str, float | str],
    time_s: np.ndarray,
    trans_norm: np.ndarray,
    freq_mhz: np.ndarray,
    all_indices: np.ndarray,
    config: QFitConfig,
) -> dict[str, float | str]:
    center_idx = int(round(float(row["sample_index"])))
    half = choose_half_window(center_idx, all_indices, config)
    start = max(0, center_idx - half)
    end = min(len(time_s), center_idx + half + 1)
    x_abs = freq_mhz[start:end]
    y = trans_norm[start:end]
    if len(y) < 30:
        raise RuntimeError("too few samples")

    # Use increasing detuning for fitting; absolute frequency direction does not matter for linewidth.
    x0_abs = freq_mhz[center_idx]
    x = x_abs - x0_abs
    local_center = center_idx - start
    edge_count = max(5, len(y) // 5)
    baseline_guess = float(np.nanmedian(np.r_[y[:edge_count], y[-edge_count:]]))
    ymin = float(np.nanmin(y[max(0, local_center - 10) : min(len(y), local_center + 11)]))
    eta0 = min(0.98, max(0.02, 1.0 - ymin / max(baseline_guess, 1e-9)))
    gamma0 = estimate_initial_gamma(x, y, int(np.nanargmin(np.abs(np.arange(len(y)) - local_center))), eta0)
    x_span = float(np.ptp(x))
    lower = [0.2, -0.01, 0.0, 1.0, -0.25 * x_span]
    upper = [1.8, 0.01, 0.999, max(5.0, x_span), 0.25 * x_span]
    p0 = [max(0.2, min(1.8, baseline_guess)), 0.0, eta0, min(gamma0, max(5.0, x_span / 2.0)), 0.0]

    popt, _ = curve_fit(
        lorentzian_notch,
        x,
        y,
        p0=p0,
        bounds=(lower, upper),
        maxfev=30_000,
    )
    fit = lorentzian_notch(x, *popt)
    residual = y - fit
    a, b, eta, gamma_mhz, x0_fit_mhz = [float(v) for v in popt]
    transmission = max(0.0, min(1.0, 1.0 - eta))
    sqrt_t = math.sqrt(transmission)
    kappa_total_mhz = 2.0 * abs(gamma_mhz)
    if transmission <= 1e-6:
        kappa0_mhz = kappa_total_mhz / 2.0
        kappa1_mhz = kappa_total_mhz / 2.0
        coupling_note = "critical_assumed"
    else:
        kappa0_mhz = abs(gamma_mhz) * (1.0 + sqrt_t)
        kappa1_mhz = abs(gamma_mhz) * (1.0 - sqrt_t)
        coupling_note = "undercoupled_branch"
    lambda_fit_nm = C_M_PER_S / ((x0_abs + x0_fit_mhz) * 1e6) * 1e9
    f0_mhz = x0_abs + x0_fit_mhz
    q0 = f0_mhz / kappa0_mhz if kappa0_mhz > 0 else math.nan
    q1 = f0_mhz / kappa1_mhz if kappa1_mhz > 0 else math.nan
    ql = f0_mhz / kappa_total_mhz if kappa_total_mhz > 0 else math.nan
    return {
        "family": row["family"],
        "family_label": row.get("family_label", row["family"]),
        "mode_number": int(row.get("mode_number_centered", row.get("mode_number_ref", math.nan))),
        "sample_index": center_idx,
        "time_s": float(time_s[center_idx]),
        "wavelength_nm": float(lambda_fit_nm),
        "fit_center_offset_mhz": x0_fit_mhz,
        "half_window_samples": half,
        "fit_points": len(y),
        "transmission": transmission,
        "depth": 1.0 - transmission,
        "linewidth_loaded_mhz": kappa_total_mhz,
        "kappa0_mhz": kappa0_mhz,
        "kappa1_mhz": kappa1_mhz,
        "Q0": q0,
        "Q1": q1,
        "QL": ql,
        "baseline_a": a,
        "baseline_slope_per_mhz": b,
        "rms_residual": float(np.sqrt(np.mean(residual**2))),
        "max_abs_residual": float(np.max(np.abs(residual))),
        "fit_status": "ok",
        "coupling_note": coupling_note,
    }


def failed_fit_row(row: dict[str, float | str], time_s: np.ndarray, exc: Exception) -> dict[str, float | str]:
    center_idx = int(round(float(row["sample_index"])))
    return {
        "family": row["family"],
        "family_label": row.get("family_label", row["family"]),
        "mode_number": int(row.get("mode_number_centered", row.get("mode_number_ref", -999))),
        "sample_index": center_idx,
        "time_s": float(time_s[center_idx]),
        "wavelength_nm": float(row["wavelength_nm_linear"]),
        "fit_center_offset_mhz": math.nan,
        "half_window_samples": math.nan,
        "fit_points": 0,
        "transmission": math.nan,
        "depth": math.nan,
        "linewidth_loaded_mhz": math.nan,
        "kappa0_mhz": math.nan,
        "kappa1_mhz": math.nan,
        "Q0": math.nan,
        "Q1": math.nan,
        "QL": math.nan,
        "baseline_a": math.nan,
        "baseline_slope_per_mhz": math.nan,
        "rms_residual": math.nan,
        "max_abs_residual": math.nan,
        "fit_status": f"failed: {exc}",
        "coupling_note": "",
    }


def fit_modes(
    points: list[dict[str, float | str]],
    *,
    time_s: np.ndarray,
    trans_norm: np.ndarray,
    freq_mhz: np.ndarray,
    all_indices: np.ndarray,
    config: QFitConfig,
) -> list[dict[str, float | str]]:
    def run_one(row: dict[str, float | str]) -> dict[str, float | str]:
        try:
            return fit_one_mode(
                row=row,
                time_s=time_s,
                trans_norm=trans_norm,
                freq_mhz=freq_mhz,
                all_indices=all_indices,
                config=config,
            )
        except Exception as exc:
            return failed_fit_row(row, time_s, exc)

    if config.workers <= 1 or len(points) <= 1:
        return [run_one(row) for row in points]

    with concurrent.futures.ThreadPoolExecutor(max_workers=config.workers) as executor:
        return list(executor.map(run_one, points))


def write_q_table(path: Path, rows: list[dict[str, float | str]]) -> None:
    fields = [
        "family",
        "family_label",
        "mode_number",
        "sample_index",
        "time_s",
        "wavelength_nm",
        "fit_center_offset_mhz",
        "half_window_samples",
        "fit_points",
        "transmission",
        "depth",
        "linewidth_loaded_mhz",
        "kappa0_mhz",
        "kappa1_mhz",
        "Q0",
        "Q1",
        "QL",
        "baseline_a",
        "baseline_slope_per_mhz",
        "rms_residual",
        "max_abs_residual",
        "fit_status",
        "coupling_note",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def finite_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def candidate_from_q_row(
    row: dict[str, float | str],
    *,
    metric: str,
    selection_metric_value: float,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    candidate = {
        "selection_metric": metric,
        "selection_metric_value": selection_metric_value,
        "family": str(row.get("family", "")),
        "family_label": str(row.get("family_label", row.get("family", ""))),
        "mode_number": int(float(row.get("mode_number", 0))),
        "wavelength_nm": finite_float(row.get("wavelength_nm")),
        "Q0": finite_float(row.get("Q0")),
        "Q1": finite_float(row.get("Q1")),
        "QL": finite_float(row.get("QL")),
        "depth": finite_float(row.get("depth")),
        "transmission": finite_float(row.get("transmission")),
        "linewidth_loaded_mhz": finite_float(row.get("linewidth_loaded_mhz")),
        "sample_index": int(float(row.get("sample_index", 0))),
        "time_s": finite_float(row.get("time_s")),
        "fit_status": str(row.get("fit_status", "")),
        "coupling_note": str(row.get("coupling_note", "")),
    }
    if extra:
        candidate.update(extra)
    return candidate


def select_best_lock_candidate(
    rows: list[dict[str, float | str]],
    *,
    metric: str = "Q0",
) -> dict[str, object] | None:
    candidates: list[tuple[float, dict[str, float | str]]] = []
    for row in rows:
        if row.get("fit_status") != "ok":
            continue
        score = finite_float(row.get(metric))
        wavelength_nm = finite_float(row.get("wavelength_nm"))
        if score is None or wavelength_nm is None:
            continue
        candidates.append((score, row))
    if not candidates:
        return None

    score, row = max(candidates, key=lambda item: item[0])
    return candidate_from_q_row(row, metric=metric, selection_metric_value=score)


def select_nearest_wavelength_best_q_candidate(
    rows: list[dict[str, float | str]],
    *,
    target_wavelength_nm: float = 1550.0,
    metric: str = "Q0",
) -> dict[str, object] | None:
    candidates: list[tuple[float, float, dict[str, float | str]]] = []
    for row in rows:
        if row.get("fit_status") != "ok":
            continue
        wavelength_nm = finite_float(row.get("wavelength_nm"))
        score = finite_float(row.get(metric))
        if wavelength_nm is None or score is None:
            continue
        distance_nm = abs(wavelength_nm - target_wavelength_nm)
        candidates.append((distance_nm, score, row))
    if not candidates:
        return None

    distance_nm, score, row = min(candidates, key=lambda item: (item[0], -item[1]))
    return candidate_from_q_row(
        row,
        metric=f"nearest_{target_wavelength_nm:g}nm_then_{metric}",
        selection_metric_value=score,
        extra={
            "target_wavelength_nm": target_wavelength_nm,
            "distance_to_target_nm": distance_nm,
        },
    )


def write_best_lock_candidate(path: Path, rows: list[dict[str, float | str]], q_table: Path) -> dict[str, object] | None:
    candidate = select_best_lock_candidate(rows, metric="Q0")
    nearest_1550_candidate = select_nearest_wavelength_best_q_candidate(rows, target_wavelength_nm=1550.0, metric="Q0")
    payload: dict[str, object] = {
        "ok": candidate is not None,
        "purpose": "best current-mode lock candidate selected from fitted Q table",
        "q_table": str(q_table),
        "selection_metric": "Q0",
        "candidate": candidate,
        "nearest_1550_best_q_candidate": nearest_1550_candidate,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return candidate


def transmission_trend_metrics(rows: list[dict[str, float | str]]) -> dict[str, float | bool | str]:
    ok = [row for row in rows if row["fit_status"] == "ok"]
    if len(ok) < 3:
        return {
            "slope_per_nm": math.nan,
            "delta": math.nan,
            "increases_with_wavelength": False,
            "q0_q1_swapped": False,
            "note": "",
        }
    fam = sorted(ok, key=lambda item: float(item["wavelength_nm"]))
    wavelength_nm = np.array([float(row["wavelength_nm"]) for row in fam])
    transmission = np.array([float(row["transmission"]) for row in fam])
    finite = np.isfinite(wavelength_nm) & np.isfinite(transmission)
    if np.count_nonzero(finite) < 3:
        return {
            "slope_per_nm": math.nan,
            "delta": math.nan,
            "increases_with_wavelength": False,
            "q0_q1_swapped": False,
            "note": "",
        }
    wavelength_nm = wavelength_nm[finite]
    transmission = transmission[finite]
    slope = float(np.polyfit(wavelength_nm, transmission, 1)[0])
    delta = float(transmission[-1] - transmission[0])
    increases = slope > 1e-3 and delta > 0.03
    note = (
        "Tmin/platform rises vs wavelength; coupling branch ambiguous; Q0/Q1 not swapped."
        if increases
        else ""
    )
    return {
        "slope_per_nm": slope,
        "delta": delta,
        "increases_with_wavelength": increases,
        "q0_q1_swapped": False,
        "note": note,
    }


def annotate_family_coupling_notes(rows: list[dict[str, float | str]]) -> dict[str, dict[str, float | bool | str]]:
    trends: dict[str, dict[str, float | bool | str]] = {}
    for family in sorted({str(row["family"]) for row in rows}):
        fam = [row for row in rows if row["family"] == family]
        trend = transmission_trend_metrics(fam)
        trends[family] = trend
        if trend["increases_with_wavelength"]:
            for row in fam:
                if row["fit_status"] == "ok":
                    row["coupling_note"] = str(trend["note"])
    return trends


DISPLAY_COLORS = {
    "mode1": "#1f77b4",
    "mode2": "#d62728",
    "mode3": "#2ca02c",
    "mode4": "#9467bd",
}


def display_labels_by_depth(rows: list[dict[str, float | str]]) -> dict[str, str]:
    """Map internal family keys to depth-ordered mode labels for human-facing plots."""
    explicit = {
        str(row["family"]): str(row["family_label"])
        for row in rows
        if row.get("family") and row.get("family_label") and str(row.get("family_label")) != str(row.get("family"))
    }
    if explicit:
        return explicit
    sequence_transmission: dict[tuple[int, ...], float] = {}
    sequence_families: dict[tuple[int, ...], list[str]] = {}
    for family in sorted({str(row["family"]) for row in rows}):
        fam = [row for row in rows if row["family"] == family and row["fit_status"] == "ok"]
        if not fam:
            continue
        sample_indices = tuple(sorted(int(row["sample_index"]) for row in fam))
        median_transmission = float(np.nanmedian([float(row["transmission"]) for row in fam]))
        sequence_transmission[sample_indices] = min(
            sequence_transmission.get(sample_indices, math.inf),
            median_transmission,
        )
        sequence_families.setdefault(sample_indices, []).append(family)

    labels: dict[str, str] = {}
    for index, sample_indices in enumerate(
        sorted(sequence_families, key=lambda seq: (sequence_transmission[seq], min(sequence_families[seq]))),
        start=1,
    ):
        label = f"mode{index}"
        for family in sequence_families[sample_indices]:
            labels[family] = label
    return labels


def plot_q_trends(path: Path, rows: list[dict[str, float | str]]) -> None:
    families = sorted({str(row["family"]) for row in rows})
    family_trends = {family: transmission_trend_metrics([row for row in rows if row["family"] == family]) for family in families}
    display_labels = display_labels_by_depth(rows)
    fig, axes = plt.subplots(3, 1, figsize=(12, 13), sharex=True)
    fig.subplots_adjust(left=0.10, right=0.78, top=0.92, bottom=0.08, hspace=0.24)
    for family in families:
        fam = [row for row in rows if row["family"] == family and row["fit_status"] == "ok"]
        fam = sorted(fam, key=lambda item: float(item["wavelength_nm"]))
        if not fam:
            continue
        wavelength_nm = np.array([float(row["wavelength_nm"]) for row in fam])
        q0 = np.array([float(row["Q0"]) for row in fam]) / 1e6
        q1 = np.array([float(row["Q1"]) for row in fam]) / 1e6
        transmission = np.array([float(row["transmission"]) for row in fam])
        display_label = display_labels.get(family, family)
        color = DISPLAY_COLORS.get(display_label, None)
        axes[0].plot(wavelength_nm, q0, "o-", label=display_label, color=color, lw=2.3, ms=6.5)
        axes[1].plot(wavelength_nm, q1, "s--", label=display_label, color=color, lw=2.3, ms=6.5, alpha=0.75)
        axes[2].plot(wavelength_nm, transmission, "o-", label=display_label, color=color, lw=2.3, ms=6.5)
    fig.suptitle("Preliminary large-scan Q trends vs wavelength", fontsize=24)
    axes[0].set_title("Q0", fontsize=20)
    axes[1].set_title("Q1", fontsize=20)
    axes[2].set_title("Tmin / platform", fontsize=20)
    axes[0].set_ylabel("Q0 (million)", fontsize=18)
    axes[1].set_ylabel("Q1 (million)", fontsize=18)
    axes[2].set_ylabel("Tmin / platform", fontsize=18)
    axes[2].set_xlabel("Wavelength (nm)", fontsize=18)
    for ax in axes:
        ax.tick_params(axis="both", labelsize=15)
        ax.grid(True, alpha=0.25)
    handles, labels = axes[0].get_legend_handles_labels()
    if handles:
        unique: dict[str, object] = {}
        for handle, label in zip(handles, labels):
            unique.setdefault(label, handle)
        fig.legend(unique.values(), unique.keys(), loc="center left", bbox_to_anchor=(0.82, 0.58), fontsize=16, frameon=True)
    trend_notes = []
    for family, trend in family_trends.items():
        if trend["increases_with_wavelength"]:
            wrapped_note = str(trend["note"]).replace("; ", "\n  ")
            trend_notes.append(f"{display_labels.get(family, family)}:\n  {wrapped_note}")
    if trend_notes:
        fig.text(
            0.82,
            0.32,
            "Branch note\n" + "\n".join(trend_notes),
            ha="left",
            va="top",
            fontsize=14,
            bbox={"boxstyle": "round,pad=0.35", "facecolor": "white", "edgecolor": "#999999", "alpha": 0.95},
        )
    fig.savefig(path, dpi=220, bbox_inches="tight")
    plt.close(fig)


def plot_fit_examples(
    path: Path,
    q_rows: list[dict[str, float | str]],
    time_s: np.ndarray,
    trans_norm: np.ndarray,
    freq_mhz: np.ndarray,
    max_examples: int = 9,
) -> None:
    ok = [row for row in q_rows if row["fit_status"] == "ok"]
    if not ok:
        return
    display_labels = display_labels_by_depth(q_rows)
    # Pick a few across families and mode range.
    examples: list[dict[str, float | str]] = []
    for family in sorted({str(row["family"]) for row in ok}):
        fam = sorted([row for row in ok if row["family"] == family], key=lambda item: float(item["mode_number"]))
        if fam:
            examples.extend([fam[0], fam[len(fam) // 2], fam[-1]])
    examples = examples[:max_examples]
    ncols = 3
    nrows = math.ceil(len(examples) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(12, 3.4 * nrows), constrained_layout=True)
    axes = np.ravel(axes)
    for ax, row in zip(axes, examples):
        center = int(row["sample_index"])
        half = int(row["half_window_samples"])
        start = max(0, center - half)
        end = min(len(time_s), center + half + 1)
        x0_abs = freq_mhz[center]
        x = freq_mhz[start:end] - x0_abs
        y = trans_norm[start:end]
        gamma = float(row["linewidth_loaded_mhz"]) / 2.0
        eta = float(row["depth"])
        a = float(row["baseline_a"])
        b = float(row["baseline_slope_per_mhz"])
        x0 = float(row["fit_center_offset_mhz"])
        ax.plot(x, y, lw=0.7, color="#333333")
        ax.plot(x, lorentzian_notch(x, a, b, eta, gamma, x0), lw=1.5, color="#d62728")
        family = str(row["family"])
        display_label = display_labels.get(family, family)
        ax.set_title(f"{display_label} m={int(row['mode_number'])}, Q0={float(row['Q0'])/1e6:.2f}M, Q1={float(row['Q1'])/1e6:.2f}M")
        ax.set_xlabel("Detuning (MHz)")
        ax.set_ylabel("Norm. transmission")
    for ax in axes[len(examples) :]:
        ax.axis("off")
    fig.savefig(path, dpi=180)
    plt.close(fig)


def plot_local_dip_mosaic(
    path: Path,
    q_rows: list[dict[str, float | str]],
    time_s: np.ndarray,
    trans_norm: np.ndarray,
    freq_mhz: np.ndarray,
    *,
    ncols: int = 7,
) -> None:
    ok = [row for row in q_rows if row["fit_status"] == "ok"]
    if not ok:
        return
    display_labels = display_labels_by_depth(q_rows)
    grouped: dict[str, dict[int, dict[str, float | str]]] = {}
    for row in ok:
        label = display_labels.get(str(row["family"]), str(row["family"]))
        grouped.setdefault(label, {})
        grouped[label].setdefault(int(row["sample_index"]), row)
    family_rows = {
        label: sorted(rows_by_index.values(), key=lambda item: float(item["wavelength_nm"]))
        for label, rows_by_index in grouped.items()
    }
    families = sorted(family_rows)
    family_row_counts = {label: math.ceil(len(rows) / ncols) for label, rows in family_rows.items()}
    total_rows = sum(family_row_counts.values())
    fig, axes = plt.subplots(
        total_rows,
        ncols,
        figsize=(2.7 * ncols, 2.35 * total_rows),
        sharex=True,
        sharey=True,
        squeeze=False,
        constrained_layout=True,
    )
    fig.suptitle("Local dip mosaic from full-resolution normalized CH2", fontsize=22)
    row_offset = 0
    for label in families:
        rows = family_rows[label]
        color = DISPLAY_COLORS.get(label, "#333333")
        for i, qrow in enumerate(rows):
            r = row_offset + i // ncols
            c = i % ncols
            ax = axes[r, c]
            center = int(qrow["sample_index"])
            half = int(qrow["half_window_samples"])
            start = max(0, center - half)
            end = min(len(time_s), center + half + 1)
            x = freq_mhz[start:end] - freq_mhz[center]
            y = trans_norm[start:end]
            order = np.argsort(x)
            ax.plot(x[order], y[order], lw=0.7, color=color)
            ax.axvline(0.0, color="#999999", lw=0.6, alpha=0.7)
            ax.set_title(f"{label} m={int(qrow['mode_number'])}\n{float(qrow['wavelength_nm']):.2f} nm", fontsize=10)
            ax.tick_params(axis="both", labelsize=8)
            ax.grid(True, alpha=0.18)
            if c == 0:
                ax.set_ylabel(f"{label}\nNorm. T", fontsize=11)
            if r == total_rows - 1:
                ax.set_xlabel("MHz", fontsize=10)
        for i in range(len(rows), family_row_counts[label] * ncols):
            axes[row_offset + i // ncols, i % ncols].axis("off")
        row_offset += family_row_counts[label]
    fig.savefig(path, dpi=220)
    plt.close(fig)


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-path", default=None)
    parser.add_argument("--csv-path", default=None)
    parser.add_argument("--family-points-csv", default=None)
    parser.add_argument(
        "--campaign",
        default=default_campaign(),
        help=f"Campaign path under ${DATA_ROOT_ENV}/experiments. Defaults to ${CAMPAIGN_ENV} or wafer_measuement/Batch_260515.",
    )
    parser.add_argument("--chip", default=default_chip(), help=f"Chip/sample id. Defaults to ${CHIP_ENV} or chip7.")
    parser.add_argument("--die", default="die1-1")
    parser.add_argument("--cavity", default="c1")
    parser.add_argument("--depth-threshold", type=float, default=0.4)
    parser.add_argument("--start-nm", type=float, default=1530.0)
    parser.add_argument("--center-nm", type=float, default=1550.0)
    parser.add_argument("--stop-nm", type=float, default=1570.0)
    parser.add_argument("--min-half-window-samples", type=int, default=120)
    parser.add_argument("--max-half-window-samples", type=int, default=2500)
    parser.add_argument("--neighbor-window-fraction", type=float, default=0.40)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument(
        "--output-dir",
        default=None,
        help=f"Output directory. Defaults to the input data directory, or ${DATA_ROOT_ENV}/experiments/... when input paths are omitted.",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    result_dir: Path | None = None
    if args.data_path or args.csv_path:
        data_path = Path(args.data_path or args.csv_path)
    else:
        result_dir = default_cavity_dir(args.chip, args.die, args.cavity, campaign=args.campaign)
        candidates = list(result_dir.glob("large_scan_*_1530-1570nm.npz")) + list(result_dir.glob("large_scan_*_1530-1570nm.csv"))
        if not candidates:
            raise SystemExit(f"No large-scan data found in {result_dir}")
        data_path = max(candidates, key=lambda p: p.stat().st_mtime)
    family_points = (
        Path(args.family_points_csv)
        if args.family_points_csv
        else None
    )
    if family_points is None:
        if result_dir is None:
            result_dir = default_cavity_dir(args.chip, args.die, args.cavity, campaign=args.campaign)
        candidates = list(result_dir.glob("large_scan_*_dispersion_auto_centered_family_points.csv"))
        if not candidates:
            raise SystemExit(f"No auto-centered family points CSV found in {result_dir}")
        family_points = max(candidates, key=lambda p: p.stat().st_mtime)
    output_dir = Path(args.output_dir) if args.output_dir else data_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = data_path.stem
    config = QFitConfig(
        data_path=str(data_path),
        family_points_csv=str(family_points),
        depth_threshold=args.depth_threshold,
        start_nm=args.start_nm,
        center_nm=args.center_nm,
        stop_nm=args.stop_nm,
        min_half_window_samples=args.min_half_window_samples,
        max_half_window_samples=args.max_half_window_samples,
        neighbor_window_fraction=args.neighbor_window_fraction,
        workers=max(1, args.workers),
        output_dir=str(output_dir),
    )
    print(json.dumps(asdict(config), indent=2, ensure_ascii=False))

    time_s, _trigger, trans_raw, _mzi_raw = read_large_scan_data(data_path)
    trans_norm, _baseline = normalize_transmission_with_baseline(trans_raw)
    wavelength_nm = time_to_wavelength_nm(time_s, args.start_nm, args.center_nm, args.stop_nm)
    freq_mhz = wavelength_nm_to_freq_mhz(wavelength_nm)
    points = load_family_points(family_points, args.depth_threshold)
    all_indices = np.array([int(round(float(row["sample_index"]))) for row in points], dtype=int)

    q_rows = fit_modes(
        points,
        time_s=time_s,
        trans_norm=trans_norm,
        freq_mhz=freq_mhz,
        all_indices=all_indices,
        config=config,
    )

    family_trends = annotate_family_coupling_notes(q_rows)
    q_table = output_dir / f"{stem}_large_scan_q_by_family.csv"
    write_q_table(q_table, q_rows)
    best_lock_candidate_path = output_dir / f"{stem}_best_lock_candidate.json"
    best_lock_candidate = write_best_lock_candidate(best_lock_candidate_path, q_rows, q_table)
    trend_fig = output_dir / f"{stem}_large_scan_q_trends.png"
    plot_q_trends(trend_fig, q_rows)
    examples_fig = output_dir / f"{stem}_large_scan_q_fit_examples.png"
    plot_fit_examples(examples_fig, q_rows, time_s, trans_norm, freq_mhz)
    mosaic_fig = output_dir / f"{stem}_local_dip_mosaic.png"
    plot_local_dip_mosaic(mosaic_fig, q_rows, time_s, trans_norm, freq_mhz)
    summary = {
        "config": asdict(config),
        "mode_count": len(q_rows),
        "ok_count": sum(1 for row in q_rows if row["fit_status"] == "ok"),
        "q_table": str(q_table),
        "best_lock_candidate_json": str(best_lock_candidate_path),
        "best_lock_candidate": best_lock_candidate,
        "trend_figure": str(trend_fig),
        "fit_examples_figure": str(examples_fig),
        "local_dip_mosaic_figure": str(mosaic_fig),
        "family_summary": {},
    }
    for family in sorted({str(row["family"]) for row in q_rows}):
        fam = [row for row in q_rows if row["family"] == family and row["fit_status"] == "ok"]
        if not fam:
            continue
        summary["family_summary"][family] = {
            "family_label": str(fam[0].get("family_label", family)),
            "count": len(fam),
            "Q0_median_M": float(np.nanmedian([float(row["Q0"]) for row in fam]) / 1e6),
            "Q1_median_M": float(np.nanmedian([float(row["Q1"]) for row in fam]) / 1e6),
            "QL_median_M": float(np.nanmedian([float(row["QL"]) for row in fam]) / 1e6),
            "transmission_median": float(np.nanmedian([float(row["transmission"]) for row in fam])),
            "transmission_slope_per_nm": family_trends[family]["slope_per_nm"],
            "transmission_delta_first_to_last": family_trends[family]["delta"],
            "transmission_increases_with_wavelength": family_trends[family]["increases_with_wavelength"],
            "q0_q1_swapped": family_trends[family]["q0_q1_swapped"],
            "coupling_branch_note": family_trends[family]["note"],
            "linewidth_median_mhz": float(np.nanmedian([float(row["linewidth_loaded_mhz"]) for row in fam])),
        }
    summary_path = output_dir / f"{stem}_large_scan_q_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
