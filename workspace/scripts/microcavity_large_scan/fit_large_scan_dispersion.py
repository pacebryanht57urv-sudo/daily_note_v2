#!/usr/bin/env python3
"""Fit preliminary dispersion families from a processed large-scan dip table."""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.optimize import curve_fit

from chip7_design import expected_chip7_fsr_mhz
from data_paths import DATA_ROOT_ENV, default_cavity_dir
DISPLAY_COLORS = {
    "mode1": "#1f77b4",
    "mode2": "#d62728",
    "mode3": "#2ca02c",
    "mode4": "#9467bd",
}
FAMILY_KEYS = ("lower_branch", "upper_branch", "middle_branch")
MIN_TRACK_SPACING_FRACTION = 0.65
MAX_TRACK_SPACING_FRACTION = 1.35
MAX_TRACK_MODE_SKIP = 3
MIN_DEPTH_CLUSTER_GAP = 0.12
MIN_DEPTH_CLUSTER_SIZE = 4


@dataclass
class FitConfig:
    dip_table: str
    depth_threshold: float
    reference_fsr_mhz: float
    reference_fsr_source: str
    auto_center_tolerance_mhz: float
    auto_center_iterations: int
    output_dir: str


def poly2(mode: np.ndarray, c0: float, d1_corr: float, d2: float) -> np.ndarray:
    return c0 + d1_corr * mode + 0.5 * d2 * mode**2


def poly3(mode: np.ndarray, c0: float, d1_corr: float, d2: float, d3: float) -> np.ndarray:
    return c0 + d1_corr * mode + 0.5 * d2 * mode**2 + d3 * mode**3 / 6.0


def wrap_frequency(freq_mhz: np.ndarray | float, fsr_mhz: float) -> np.ndarray | float:
    return (freq_mhz + fsr_mhz / 2.0) % fsr_mhz - fsr_mhz / 2.0


def load_dips(path: Path, depth_threshold: float, reference_fsr_mhz: float) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            parsed = {key: float(value) for key, value in row.items()}
            if parsed["depth_1_minus_norm"] < depth_threshold:
                continue
            freq = parsed["relative_freq_mhz"]
            mode = int(round(freq / reference_fsr_mhz))
            folded = (freq - mode * reference_fsr_mhz + reference_fsr_mhz / 2.0) % reference_fsr_mhz
            folded -= reference_fsr_mhz / 2.0
            parsed["mode_number_ref"] = mode
            parsed["folded_freq_ref_mhz"] = folded
            rows.append(parsed)
    return rows


def add_centered_coordinates(row: dict[str, float], reference_fsr_mhz: float, offset_mhz: float) -> dict[str, float]:
    freq = row["relative_freq_mhz"] + offset_mhz
    mode = int(round(freq / reference_fsr_mhz))
    folded = float(wrap_frequency(freq - mode * reference_fsr_mhz, reference_fsr_mhz))
    item = dict(row)
    item["auto_offset_mhz"] = offset_mhz
    item["mode_number_centered"] = mode
    item["folded_freq_centered_mhz"] = folded
    return item


def split_families(rows: list[dict[str, float]], reference_fsr_mhz: float) -> dict[str, list[dict[str, float]]]:
    continuous = split_families_by_continuous_fsr(rows, reference_fsr_mhz)
    if any(continuous.values()):
        return continuous

    candidates = [row for row in rows if row["depth_1_minus_norm"] > 0.35]
    by_mode: dict[int, list[dict[str, float]]] = {}
    for row in candidates:
        by_mode.setdefault(int(row["mode_number_ref"]), []).append(row)

    families: dict[str, list[dict[str, float]]] = {key: [] for key in FAMILY_KEYS}
    incomplete_bins: list[tuple[int, list[dict[str, float]]]] = []
    for mode, mode_rows in by_mode.items():
        mode_rows = sorted(mode_rows, key=lambda item: item["folded_freq_ref_mhz"])
        if len(mode_rows) >= 2:
            families["lower_branch"].append(mode_rows[0])
            families["upper_branch"].append(mode_rows[-1])
            if len(mode_rows) >= 3:
                middle = mode_rows[len(mode_rows) // 2]
                if middle is not mode_rows[0] and middle is not mode_rows[-1]:
                    families["middle_branch"].append(middle)
        elif len(mode_rows) == 1:
            incomplete_bins.append((mode, mode_rows))

    branch_models: dict[str, tuple[float, float]] = {}
    for family, family_rows in families.items():
        if len(family_rows) >= 2:
            mode = np.array([row["mode_number_ref"] for row in family_rows], dtype=float)
            folded = np.array([row["folded_freq_ref_mhz"] for row in family_rows], dtype=float)
            slope, intercept = np.polyfit(mode, folded, deg=1)
        elif family_rows:
            slope = 0.0
            intercept = float(family_rows[0]["folded_freq_ref_mhz"])
        else:
            slope = 0.0
            intercept = 0.0
        branch_models[family] = (float(slope), float(intercept))

    for mode, mode_rows in incomplete_bins:
        remaining = set(families)
        candidates_for_bin: list[tuple[float, str, dict[str, float]]] = []
        for row in mode_rows:
            folded = row["folded_freq_ref_mhz"]
            for family, (slope, intercept) in branch_models.items():
                if len(families[family]) < 2:
                    continue
                predicted = slope * mode + intercept
                candidates_for_bin.append((abs(folded - predicted), family, row))
        if not candidates_for_bin:
            families["lower_branch"].append(mode_rows[0])
            continue
        for _distance, family, row in sorted(candidates_for_bin, key=lambda item: item[0]):
            if family not in remaining:
                continue
            if row in families[family]:
                continue
            if any(row in families[other] for other in families):
                continue
            families[family].append(row)
            remaining.remove(family)

    return {family: sorted(family_rows, key=lambda item: item["mode_number_ref"]) for family, family_rows in families.items()}


def rows_are_connected_by_fsr(left: dict[str, float], right: dict[str, float], reference_fsr_mhz: float) -> bool:
    spacing = abs(float(right["relative_freq_mhz"]) - float(left["relative_freq_mhz"]))
    if spacing <= 0:
        return False
    mode_gap = int(round(spacing / reference_fsr_mhz))
    if mode_gap < 1 or mode_gap > MAX_TRACK_MODE_SKIP:
        return False
    spacing_per_mode = spacing / mode_gap
    return (
        MIN_TRACK_SPACING_FRACTION * reference_fsr_mhz
        <= spacing_per_mode
        <= MAX_TRACK_SPACING_FRACTION * reference_fsr_mhz
    )


def connected_components_by_fsr(rows: list[dict[str, float]], reference_fsr_mhz: float) -> list[list[dict[str, float]]]:
    if not rows:
        return []
    ordered = sorted(rows, key=lambda item: float(item["relative_freq_mhz"]))
    n = len(ordered)
    adjacency: list[set[int]] = [set() for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            spacing = float(ordered[j]["relative_freq_mhz"]) - float(ordered[i]["relative_freq_mhz"])
            if spacing > MAX_TRACK_MODE_SKIP * MAX_TRACK_SPACING_FRACTION * reference_fsr_mhz:
                break
            if rows_are_connected_by_fsr(ordered[i], ordered[j], reference_fsr_mhz):
                adjacency[i].add(j)
                adjacency[j].add(i)

    seen = [False] * n
    components: list[list[dict[str, float]]] = []
    for start in range(n):
        if seen[start]:
            continue
        stack = [start]
        seen[start] = True
        component: list[dict[str, float]] = []
        while stack:
            node = stack.pop()
            component.append(ordered[node])
            for neighbor in adjacency[node]:
                if not seen[neighbor]:
                    seen[neighbor] = True
                    stack.append(neighbor)
        components.append(component)
    return components


def assign_sequence_modes(rows: list[dict[str, float]], reference_fsr_mhz: float) -> list[dict[str, float]]:
    center = min(rows, key=lambda item: abs(float(item["relative_freq_mhz"])))
    center_freq = float(center["relative_freq_mhz"])
    assigned: list[dict[str, float]] = []
    used_modes: dict[int, dict[str, float]] = {}
    for row in rows:
        mode = int(round((float(row["relative_freq_mhz"]) - center_freq) / reference_fsr_mhz))
        item = dict(row)
        item["mode_number_ref"] = mode
        item["folded_freq_ref_mhz"] = float(row["relative_freq_mhz"]) - center_freq - mode * reference_fsr_mhz
        item["_sequence_mode_assigned"] = 1.0
        previous = used_modes.get(mode)
        if previous is None or float(item["depth_1_minus_norm"]) > float(previous["depth_1_minus_norm"]):
            used_modes[mode] = item
    assigned = sorted(used_modes.values(), key=lambda item: int(item["mode_number_ref"]))
    return assigned


def split_families_by_continuous_fsr(rows: list[dict[str, float]], reference_fsr_mhz: float) -> dict[str, list[dict[str, float]]]:
    families: dict[str, list[dict[str, float]]] = {key: [] for key in FAMILY_KEYS}
    if len(rows) < 4:
        return families

    depth_bands = split_depth_bands(rows)
    if len(depth_bands) >= 2:
        for key, band in zip(FAMILY_KEYS, sorted(depth_bands, key=median_depth, reverse=True)):
            families[key] = assign_sequence_modes(band, reference_fsr_mhz)
        return families

    components = connected_components_by_fsr(rows, reference_fsr_mhz)
    usable = [component for component in components if len(component) >= 4]
    if not usable:
        return families

    def component_sort_key(component: list[dict[str, float]]) -> tuple[float, int]:
        depth = -float(np.nanmedian([row["depth_1_minus_norm"] for row in component]))
        count = -len(component)
        return depth, count

    for key, component in zip(FAMILY_KEYS, sorted(usable, key=component_sort_key)):
        families[key] = assign_sequence_modes(component, reference_fsr_mhz)
    return families


def median_depth(rows: list[dict[str, float]]) -> float:
    return float(np.nanmedian([float(row["depth_1_minus_norm"]) for row in rows]))


def split_depth_bands(rows: list[dict[str, float]]) -> list[list[dict[str, float]]]:
    groups = [sorted(rows, key=lambda item: float(item["depth_1_minus_norm"]))]
    while len(groups) < len(FAMILY_KEYS):
        best: tuple[float, int, int] | None = None
        for group_index, group in enumerate(groups):
            if len(group) < 2 * MIN_DEPTH_CLUSTER_SIZE:
                continue
            depths = [float(row["depth_1_minus_norm"]) for row in group]
            for split_index in range(MIN_DEPTH_CLUSTER_SIZE, len(group) - MIN_DEPTH_CLUSTER_SIZE + 1):
                gap = depths[split_index] - depths[split_index - 1]
                if gap < MIN_DEPTH_CLUSTER_GAP:
                    continue
                if best is None or gap > best[0]:
                    best = (gap, group_index, split_index)
        if best is None:
            break
        _gap, group_index, split_index = best
        group = groups.pop(group_index)
        groups.append(group[:split_index])
        groups.append(group[split_index:])
    return [group for group in groups if len(group) >= MIN_DEPTH_CLUSTER_SIZE]


def display_labels_by_depth(families: dict[str, list[dict[str, float]]]) -> dict[str, str]:
    """Map internal family keys to depth-ordered mode labels for human-facing output."""
    sequence_depth: dict[tuple[int, ...], float] = {}
    sequence_families: dict[tuple[int, ...], list[str]] = {}
    for family, rows in families.items():
        if not rows:
            continue
        sample_indices = tuple(sorted(int(round(row["sample_index"])) for row in rows))
        median_depth = float(np.nanmedian([float(row["depth_1_minus_norm"]) for row in rows]))
        sequence_depth[sample_indices] = max(sequence_depth.get(sample_indices, -math.inf), median_depth)
        sequence_families.setdefault(sample_indices, []).append(family)

    labels: dict[str, str] = {}
    for index, sample_indices in enumerate(
        sorted(sequence_families, key=lambda seq: (-sequence_depth[seq], min(sequence_families[seq]))),
        start=1,
    ):
        label = f"mode{index}"
        for family in sequence_families[sample_indices]:
            labels[family] = label
    return labels


def fit_family(name: str, rows: list[dict[str, float]], reference_fsr_mhz: float) -> dict[str, object]:
    if len(rows) < 4:
        return {"name": name, "count": len(rows), "status": "too_few_points"}
    mode = np.array([row["mode_number_ref"] for row in rows], dtype=float)
    folded = np.array([row["folded_freq_ref_mhz"] for row in rows], dtype=float)
    depth = np.array([row["depth_1_minus_norm"] for row in rows], dtype=float)
    sigma = 1.0 / np.maximum(depth, 0.05)

    p2, _ = curve_fit(poly2, mode, folded, sigma=sigma, absolute_sigma=False, maxfev=20_000)
    r2 = folded - poly2(mode, *p2)
    p3, _ = curve_fit(poly3, mode, folded, sigma=sigma, absolute_sigma=False, maxfev=20_000)
    r3 = folded - poly3(mode, *p3)
    return {
        "name": name,
        "count": len(rows),
        "mode_min": int(np.min(mode)),
        "mode_max": int(np.max(mode)),
        "quadratic": {
            "offset_mhz": float(p2[0]),
            "d1_correction_mhz": float(p2[1]),
            "effective_d1_mhz": float(reference_fsr_mhz + p2[1]),
            "d2_mhz_per_mode2": float(p2[2]),
            "rms_residual_mhz": float(np.sqrt(np.mean(r2**2))),
            "max_abs_residual_mhz": float(np.max(np.abs(r2))),
        },
        "cubic": {
            "offset_mhz": float(p3[0]),
            "d1_correction_mhz": float(p3[1]),
            "effective_d1_mhz": float(reference_fsr_mhz + p3[1]),
            "d2_mhz_per_mode2": float(p3[2]),
            "d3_mhz_per_mode3": float(p3[3]),
            "rms_residual_mhz": float(np.sqrt(np.mean(r3**2))),
            "max_abs_residual_mhz": float(np.max(np.abs(r3))),
        },
        "points": [
            {
                "mode_number": int(row["mode_number_ref"]),
                "folded_freq_mhz": float(row["folded_freq_ref_mhz"]),
                "depth": float(row["depth_1_minus_norm"]),
                "wavelength_nm": float(row["wavelength_nm_linear"]),
                "sample_index": int(row["sample_index"]),
            }
            for row in rows
        ],
    }


def fit_family_centered(name: str, rows: list[dict[str, float]], reference_fsr_mhz: float) -> dict[str, object]:
    if len(rows) < 4:
        return {"name": name, "count": len(rows), "status": "too_few_points"}
    mode = np.array([row["mode_number_centered"] for row in rows], dtype=float)
    folded = np.array([row["folded_freq_centered_mhz"] for row in rows], dtype=float)
    depth = np.array([row["depth_1_minus_norm"] for row in rows], dtype=float)
    sigma = 1.0 / np.maximum(depth, 0.05)

    p2, _ = curve_fit(poly2, mode, folded, sigma=sigma, absolute_sigma=False, maxfev=20_000)
    r2 = np.asarray(wrap_frequency(folded - poly2(mode, *p2), reference_fsr_mhz), dtype=float)
    p3, _ = curve_fit(poly3, mode, folded, sigma=sigma, absolute_sigma=False, maxfev=20_000)
    r3 = np.asarray(wrap_frequency(folded - poly3(mode, *p3), reference_fsr_mhz), dtype=float)
    return {
        "name": name,
        "count": len(rows),
        "auto_offset_mhz": float(rows[0]["auto_offset_mhz"]),
        "mode_min": int(np.min(mode)),
        "mode_max": int(np.max(mode)),
        "quadratic": {
            "offset_mhz": float(p2[0]),
            "d1_correction_mhz": float(p2[1]),
            "effective_d1_mhz": float(reference_fsr_mhz + p2[1]),
            "d2_mhz_per_mode2": float(p2[2]),
            "rms_residual_mhz": float(np.sqrt(np.mean(r2**2))),
            "max_abs_residual_mhz": float(np.max(np.abs(r2))),
        },
        "cubic": {
            "offset_mhz": float(p3[0]),
            "d1_correction_mhz": float(p3[1]),
            "effective_d1_mhz": float(reference_fsr_mhz + p3[1]),
            "d2_mhz_per_mode2": float(p3[2]),
            "d3_mhz_per_mode3": float(p3[3]),
            "rms_residual_mhz": float(np.sqrt(np.mean(r3**2))),
            "max_abs_residual_mhz": float(np.max(np.abs(r3))),
        },
        "points": [
            {
                "mode_number": int(row["mode_number_centered"]),
                "folded_freq_mhz": float(row["folded_freq_centered_mhz"]),
                "residual_mhz": float(r2[index]),
                "depth": float(row["depth_1_minus_norm"]),
                "wavelength_nm": float(row["wavelength_nm_linear"]),
                "sample_index": int(row["sample_index"]),
            }
            for index, row in enumerate(rows)
        ],
    }


def select_closest_per_mode(
    rows: list[dict[str, float]],
    *,
    reference_fsr_mhz: float,
    fit_params: np.ndarray,
    tolerance_mhz: float,
) -> list[dict[str, float]]:
    by_mode: dict[int, tuple[float, dict[str, float]]] = {}
    for row in rows:
        mode = int(row["mode_number_centered"])
        predicted = poly2(np.array([mode], dtype=float), *fit_params)[0]
        residual = float(wrap_frequency(row["folded_freq_centered_mhz"] - predicted, reference_fsr_mhz))
        if abs(residual) > tolerance_mhz:
            continue
        score = abs(residual) / max(row["depth_1_minus_norm"], 0.05)
        if mode not in by_mode or score < by_mode[mode][0]:
            picked = dict(row)
            picked["auto_residual_mhz"] = residual
            by_mode[mode] = (score, picked)
    return [item for _score, item in sorted(by_mode.values(), key=lambda pair: pair[1]["mode_number_centered"])]


def auto_center_family(
    name: str,
    all_rows: list[dict[str, float]],
    seed_rows: list[dict[str, float]],
    *,
    reference_fsr_mhz: float,
    tolerance_mhz: float,
    iterations: int,
) -> tuple[list[dict[str, float]], dict[str, object]]:
    if len(seed_rows) < 4:
        return [], {"name": name, "status": "too_few_seed_points", "count": len(seed_rows)}

    if all("_sequence_mode_assigned" in row for row in seed_rows):
        selected = []
        for row in seed_rows:
            item = dict(row)
            item["auto_offset_mhz"] = 0.0
            item["mode_number_centered"] = int(row["mode_number_ref"])
            item["folded_freq_centered_mhz"] = float(row["folded_freq_ref_mhz"])
            selected.append(item)
        fit = fit_family_centered(name, selected, reference_fsr_mhz)
        fit["seed_count"] = len(seed_rows)
        fit["auto_center_tolerance_mhz"] = tolerance_mhz
        fit["auto_center_mode"] = "continuous_fsr_sequence"
        return selected, fit

    seed_folded = np.array([row["folded_freq_ref_mhz"] for row in seed_rows], dtype=float)
    offset_mhz = -float(np.median(seed_folded))
    centered_seed = [add_centered_coordinates(row, reference_fsr_mhz, offset_mhz) for row in seed_rows]
    centered_all = [add_centered_coordinates(row, reference_fsr_mhz, offset_mhz) for row in all_rows]

    fit = fit_family_centered(name, centered_seed, reference_fsr_mhz)
    selected = centered_seed
    for _ in range(max(1, iterations)):
        if "quadratic" not in fit:
            break
        p = fit["quadratic"]
        params = np.array(
            [
                float(p["offset_mhz"]),
                float(p["d1_correction_mhz"]),
                float(p["d2_mhz_per_mode2"]),
            ]
        )
        selected = select_closest_per_mode(
            centered_all,
            reference_fsr_mhz=reference_fsr_mhz,
            fit_params=params,
            tolerance_mhz=tolerance_mhz,
        )
        fit = fit_family_centered(name, selected, reference_fsr_mhz)
    fit["seed_count"] = len(seed_rows)
    fit["auto_center_tolerance_mhz"] = tolerance_mhz
    return selected, fit


def auto_center_families(
    all_rows: list[dict[str, float]],
    seed_families: dict[str, list[dict[str, float]]],
    *,
    reference_fsr_mhz: float,
    tolerance_mhz: float,
    iterations: int,
) -> tuple[dict[str, list[dict[str, float]]], list[dict[str, object]]]:
    auto_families: dict[str, list[dict[str, float]]] = {}
    auto_fits: list[dict[str, object]] = []
    for name, seed_rows in seed_families.items():
        rows, fit = auto_center_family(
            name,
            all_rows,
            seed_rows,
            reference_fsr_mhz=reference_fsr_mhz,
            tolerance_mhz=tolerance_mhz,
            iterations=iterations,
        )
        auto_families[name] = rows
        auto_fits.append(fit)
    return auto_families, auto_fits


def write_family_points(path: Path, families: dict[str, list[dict[str, float]]]) -> None:
    display_labels = display_labels_by_depth(families)
    fields = [
        "family",
        "family_label",
        "mode_number_ref",
        "folded_freq_ref_mhz",
        "relative_freq_mhz",
        "wavelength_nm_linear",
        "depth_1_minus_norm",
        "norm_transmission",
        "sample_index",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for family, rows in families.items():
            for row in rows:
                output = {}
                for field in fields:
                    if field == "family":
                        output[field] = family
                    elif field == "family_label":
                        output[field] = display_labels.get(family, family)
                    else:
                        output[field] = row[field]
                writer.writerow(output)


def write_auto_family_points(path: Path, families: dict[str, list[dict[str, float]]]) -> None:
    display_labels = display_labels_by_depth(families)
    fields = [
        "family",
        "family_label",
        "auto_offset_mhz",
        "mode_number_centered",
        "folded_freq_centered_mhz",
        "auto_residual_mhz",
        "mode_number_ref",
        "folded_freq_ref_mhz",
        "relative_freq_mhz",
        "wavelength_nm_linear",
        "depth_1_minus_norm",
        "norm_transmission",
        "sample_index",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for family, rows in families.items():
            for row in rows:
                output = {}
                for field in fields:
                    if field == "family":
                        output[field] = family
                    elif field == "family_label":
                        output[field] = display_labels.get(family, family)
                    elif field == "auto_residual_mhz":
                        output[field] = row.get(field, "")
                    else:
                        output[field] = row[field]
                writer.writerow(output)


def plot_fits(
    path: Path,
    rows: list[dict[str, float]],
    families: dict[str, list[dict[str, float]]],
    fits: list[dict[str, object]],
    reference_fsr_mhz: float,
) -> None:
    fig, ax = plt.subplots(figsize=(15, 8), constrained_layout=True)
    all_mode = np.array([row["mode_number_ref"] for row in rows], dtype=float)
    all_folded = np.array([row["folded_freq_ref_mhz"] for row in rows], dtype=float)
    all_depth = np.array([row["depth_1_minus_norm"] for row in rows], dtype=float)
    sc = ax.scatter(all_mode, all_folded / 1000.0, c=all_depth, s=28, cmap="Greys", alpha=0.35, linewidths=0)

    display_labels = display_labels_by_depth(families)
    for fit in fits:
        name = str(fit["name"])
        points = families.get(name, [])
        if not points or "quadratic" not in fit:
            continue
        mode = np.array([row["mode_number_ref"] for row in points], dtype=float)
        folded = np.array([row["folded_freq_ref_mhz"] for row in points], dtype=float)
        label = display_labels.get(name, name)
        color = DISPLAY_COLORS.get(label, "#333333")
        ax.scatter(mode, folded / 1000.0, s=48, color=color, label=label)
        p = fit["quadratic"]
        x = np.linspace(mode.min(), mode.max(), 400)
        y = poly2(
            x,
            float(p["offset_mhz"]),
            float(p["d1_correction_mhz"]),
            float(p["d2_mhz_per_mode2"]),
        )
        ax.plot(x, y / 1000.0, color=color, lw=2.8)

    ax.axhline(0.0, color="black", lw=1.2)
    ax.axhline(reference_fsr_mhz / 2000.0, color="#888888", lw=1.0, ls="--")
    ax.axhline(-reference_fsr_mhz / 2000.0, color="#888888", lw=1.0, ls="--")
    ax.set_xlabel("Mode number, folded by reference FSR", fontsize=18)
    ax.set_ylabel("Folded frequency (GHz)", fontsize=18)
    ax.set_title(f"Preliminary large-scan dispersion families, depth filtered, ref FSR={reference_fsr_mhz/1000:.4g} GHz", fontsize=20)
    ax.tick_params(axis="both", labelsize=15)
    ax.legend(loc="upper right", fontsize=16)
    ax.text(
        0.02,
        0.03,
        "Seed families follow continuous lower/upper folded-frequency tracks; display labels are depth-ordered.",
        transform=ax.transAxes,
        fontsize=15,
        ha="left",
        va="bottom",
        bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "edgecolor": "#999999", "alpha": 0.9},
    )
    cb = fig.colorbar(sc, ax=ax)
    cb.set_label("Depth", fontsize=17)
    cb.ax.tick_params(labelsize=14)
    fig.savefig(path, dpi=240)
    plt.close(fig)


def plot_auto_centered_fits(
    path: Path,
    families: dict[str, list[dict[str, float]]],
    fits: list[dict[str, object]],
) -> None:
    visible_fits = [fit for fit in fits if "quadratic" in fit]
    if not visible_fits:
        fig, ax = plt.subplots(figsize=(8, 4), constrained_layout=True)
        ax.text(0.5, 0.5, "No auto-centered families", ha="center", va="center")
        ax.axis("off")
        fig.savefig(path, dpi=180)
        plt.close(fig)
        return

    fig, axes = plt.subplots(len(visible_fits), 1, figsize=(11, 3.5 * len(visible_fits)), sharex=False, constrained_layout=True)
    if len(visible_fits) == 1:
        axes = [axes]
    display_labels = display_labels_by_depth(families)
    for ax, fit in zip(axes, visible_fits):
        name = str(fit["name"])
        rows = families[name]
        mode = np.array([row["mode_number_centered"] for row in rows], dtype=float)
        folded = np.array([row["folded_freq_centered_mhz"] for row in rows], dtype=float)
        depth = np.array([row["depth_1_minus_norm"] for row in rows], dtype=float)
        display_label = display_labels.get(name, name)
        color = DISPLAY_COLORS.get(display_label, "#333333")
        p = fit["quadratic"]
        offset_mhz = float(p["offset_mhz"])
        d1_corr_mhz = float(p["d1_correction_mhz"])
        d2_mhz = float(p["d2_mhz_per_mode2"])
        dint = folded - offset_mhz - d1_corr_mhz * mode
        ax.scatter(mode, dint / 1000.0, c=depth, s=36, cmap="viridis", vmin=0.2, vmax=1.0, edgecolors="none")
        x = np.linspace(mode.min(), mode.max(), 400)
        y = 0.5 * d2_mhz * x**2
        ax.plot(x, y / 1000.0, color=color, lw=1.8)
        ax.axhline(0.0, color="black", lw=0.7)
        ax.set_ylabel("Dint after fitted D1 (GHz)")
        ax.set_title(
            f"{display_label}: offset={float(fit['auto_offset_mhz'])/1000:.3f} GHz, "
            f"D1={float(p['effective_d1_mhz'])/1000:.6g} GHz, "
            f"D2={float(p['d2_mhz_per_mode2']):.3g} MHz, "
            f"rms={float(p['rms_residual_mhz']):.0f} MHz"
        )
    axes[-1].set_xlabel("Mode number after family-specific offset")
    fig.savefig(path, dpi=180)
    plt.close(fig)


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dip_table", nargs="?", default=None)
    parser.add_argument("--chip", default="chip7")
    parser.add_argument("--die", default="die1-1")
    parser.add_argument("--cavity", default="c1")
    parser.add_argument("--depth-threshold", type=float, default=0.2)
    parser.add_argument(
        "--reference-fsr-mhz",
        type=float,
        default=None,
        help="Reference FSR for family assignment. Defaults to chip/die design estimate.",
    )
    parser.add_argument("--auto-center-tolerance-mhz", type=float, default=8_000.0)
    parser.add_argument("--auto-center-iterations", type=int, default=3)
    parser.add_argument(
        "--output-dir",
        default=None,
        help=f"Output directory. Defaults to the dip-table directory, or ${DATA_ROOT_ENV}/experiments/... when dip_table is omitted.",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    if args.dip_table:
        dip_table = Path(args.dip_table)
    else:
        result_dir = default_cavity_dir(args.chip, args.die, args.cavity)
        candidates = list(result_dir.glob("large_scan_*_dip_table.csv"))
        if not candidates:
            raise SystemExit(f"No dip table found in {result_dir}")
        dip_table = max(candidates, key=lambda item: item.stat().st_mtime)
    output_dir = Path(args.output_dir) if args.output_dir else dip_table.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = dip_table.name.replace("_dip_table.csv", "")
    if args.reference_fsr_mhz is not None:
        reference_fsr_mhz = float(args.reference_fsr_mhz)
        reference_fsr_source = "cli"
    elif args.chip.lower() == "chip7":
        reference_fsr_mhz = expected_chip7_fsr_mhz(args.die)
        reference_fsr_source = f"chip7_design_ng2_radius_for_{args.die}"
    else:
        raise SystemExit("Pass --reference-fsr-mhz for non-chip7 data; no safe default is available.")
    config = FitConfig(
        dip_table=str(dip_table),
        depth_threshold=args.depth_threshold,
        reference_fsr_mhz=reference_fsr_mhz,
        reference_fsr_source=reference_fsr_source,
        auto_center_tolerance_mhz=args.auto_center_tolerance_mhz,
        auto_center_iterations=args.auto_center_iterations,
        output_dir=str(output_dir),
    )

    rows = load_dips(dip_table, args.depth_threshold, reference_fsr_mhz)
    families = split_families(rows, reference_fsr_mhz)
    fits = [fit_family(name, family_rows, reference_fsr_mhz) for name, family_rows in families.items()]
    auto_families, auto_fits = auto_center_families(
        rows,
        families,
        reference_fsr_mhz=reference_fsr_mhz,
        tolerance_mhz=args.auto_center_tolerance_mhz,
        iterations=args.auto_center_iterations,
    )

    family_points_path = output_dir / f"{stem}_dispersion_family_points.csv"
    write_family_points(family_points_path, families)
    auto_family_points_path = output_dir / f"{stem}_dispersion_auto_centered_family_points.csv"
    write_auto_family_points(auto_family_points_path, auto_families)
    fit_plot_path = output_dir / f"{stem}_dispersion_families_depth_gt_{args.depth_threshold:g}.png".replace(".", "p")
    # Keep the extension readable after decimal replacement.
    fit_plot_path = fit_plot_path.with_name(fit_plot_path.name.replace("ppng", ".png"))
    plot_fits(fit_plot_path, rows, families, fits, reference_fsr_mhz)
    auto_fit_plot_path = output_dir / f"{stem}_dispersion_auto_centered_depth_gt_{args.depth_threshold:g}.png".replace(".", "p")
    auto_fit_plot_path = auto_fit_plot_path.with_name(auto_fit_plot_path.name.replace("ppng", ".png"))
    plot_auto_centered_fits(auto_fit_plot_path, auto_families, auto_fits)

    summary = {
        "config": asdict(config),
        "depth_filtered_dip_count": len(rows),
        "family_points_csv": str(family_points_path),
        "auto_centered_family_points_csv": str(auto_family_points_path),
        "fit_figure": str(fit_plot_path),
        "auto_centered_fit_figure": str(auto_fit_plot_path),
        "fits": fits,
        "auto_centered_fits": auto_fits,
    }
    summary_path = output_dir / f"{stem}_dispersion_fit_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
