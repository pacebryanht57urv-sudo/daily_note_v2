#!/usr/bin/env python3
"""Fit preliminary dispersion families from a processed large-scan dip table."""

from __future__ import annotations

import argparse
import csv
import itertools
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
from process_large_scan import normalize_transmission_with_baseline, read_large_scan_data

DISPLAY_COLORS = {
    "mode1": "#1f77b4",
    "mode2": "#d62728",
    "mode3": "#2ca02c",
    "mode4": "#9467bd",
}
FAMILY_KEYS = ("lower_branch", "upper_branch", "middle_branch", "extra_branch")
MIN_TRACK_SPACING_FRACTION = 0.65
MAX_TRACK_SPACING_FRACTION = 1.35
MAX_TRACK_MODE_SKIP = 3
MIN_DEPTH_CLUSTER_GAP = 0.12
MIN_DEPTH_CLUSTER_SIZE = 4
MIN_PARALLEL_BIN_COUNT = 4
MAX_SEED_TRACK_RMS_FRACTION = 0.02


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


def common_mode_and_folded(
    relative_freq_mhz: float,
    *,
    origin_mhz: float,
    common_d1_mhz: float,
) -> tuple[int, float]:
    mode = int(round((relative_freq_mhz - origin_mhz) / common_d1_mhz))
    folded = relative_freq_mhz - origin_mhz - mode * common_d1_mhz
    if folded > common_d1_mhz / 2.0:
        folded -= common_d1_mhz
        mode += 1
    elif folded < -common_d1_mhz / 2.0:
        folded += common_d1_mhz
        mode -= 1
    return mode, folded


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

    parallel_tracks = split_parallel_folded_rank_tracks(rows, reference_fsr_mhz)
    if parallel_tracks:
        assigned_tracks = filter_usable_seed_tracks(
            [assign_sequence_modes(track, reference_fsr_mhz) for track in parallel_tracks],
            reference_fsr_mhz,
        )
        for key, track in zip(FAMILY_KEYS, sorted(assigned_tracks, key=track_sort_key)):
            families[key] = track
        return families

    depth_bands = split_depth_bands(rows)
    if len(depth_bands) >= 2:
        tracks: list[list[dict[str, float]]] = []
        for band in sorted(depth_bands, key=median_depth, reverse=True):
            tracks.extend(split_band_by_folded_rank(band, reference_fsr_mhz))
        assigned_tracks = filter_usable_seed_tracks(
            [assign_sequence_modes(track, reference_fsr_mhz) for track in tracks],
            reference_fsr_mhz,
        )
        for key, track in zip(FAMILY_KEYS, sorted(assigned_tracks, key=track_sort_key)):
            families[key] = track
        return families

    components = connected_components_by_fsr(rows, reference_fsr_mhz)
    assigned_components = filter_usable_seed_tracks(
        [assign_sequence_modes(component, reference_fsr_mhz) for component in components if len(component) >= 4],
        reference_fsr_mhz,
    )
    if not assigned_components:
        return families

    for key, component in zip(FAMILY_KEYS, sorted(assigned_components, key=track_sort_key)):
        families[key] = component
    return families


def split_parallel_folded_rank_tracks(
    rows: list[dict[str, float]],
    reference_fsr_mhz: float,
) -> list[list[dict[str, float]]]:
    """Preserve multiple same-FSR-bin candidates before graph connectivity.

    Large-radius cavities can contain several deep resonances in every FSR. If
    we connect rows only by near-FSR spacing, those parallel tracks become one
    connected component, and the later one-point-per-mode step stitches the
    deepest point from alternating branches. Ranking by folded coordinate
    inside each reference-FSR bin keeps those branches separate first.
    """
    by_mode: dict[int, list[dict[str, float]]] = {}
    for row in rows:
        by_mode.setdefault(int(row["mode_number_ref"]), []).append(row)

    parallel_bins = sum(1 for mode_rows in by_mode.values() if len(mode_rows) >= 2)
    if parallel_bins < MIN_PARALLEL_BIN_COUNT:
        return []

    tracks = split_parallel_tracks_by_iterative_assignment(rows, reference_fsr_mhz)
    return tracks if len(tracks) >= 2 else []


def split_parallel_tracks_by_iterative_assignment(
    rows: list[dict[str, float]],
    reference_fsr_mhz: float,
) -> list[list[dict[str, float]]]:
    by_mode: dict[int, list[dict[str, float]]] = {}
    for row in rows:
        by_mode.setdefault(int(row["mode_number_ref"]), []).append(row)
    if not by_mode:
        return []

    track_count = min(len(FAMILY_KEYS), max(len(mode_rows) for mode_rows in by_mode.values()))
    if track_count < 2:
        return []

    seed_mode = sorted(by_mode, key=lambda mode: (-len(by_mode[mode]), abs(mode)))[0]
    seed_rows = sorted(by_mode[seed_mode], key=lambda item: float(item["folded_freq_ref_mhz"]))
    seed_centers = [float(row["folded_freq_ref_mhz"]) for row in seed_rows[:track_count]]
    tracks = assign_rows_to_track_models(
        by_mode,
        [(0, np.array([center], dtype=float)) for center in seed_centers],
        reference_fsr_mhz,
    )
    for _iteration in range(4):
        models = [fit_track_model(track) for track in tracks]
        tracks = assign_rows_to_track_models(by_mode, models, reference_fsr_mhz)

    return [track for track in tracks if len(track) >= MIN_DEPTH_CLUSTER_SIZE]


def filter_usable_seed_tracks(
    tracks: list[list[dict[str, float]]],
    reference_fsr_mhz: float,
) -> list[list[dict[str, float]]]:
    max_rms_mhz = MAX_SEED_TRACK_RMS_FRACTION * reference_fsr_mhz
    return [track for track in tracks if track_quadratic_rms_mhz(track, reference_fsr_mhz) <= max_rms_mhz]


def track_quadratic_rms_mhz(track: list[dict[str, float]], reference_fsr_mhz: float) -> float:
    if len(track) < MIN_DEPTH_CLUSTER_SIZE:
        return math.inf
    mode = np.array([row["mode_number_ref"] for row in track], dtype=float)
    folded = np.array([row["folded_freq_ref_mhz"] for row in track], dtype=float)
    degree = min(2, len(track) - 1)
    coeff = np.polyfit(mode, folded, deg=degree)
    residual = np.asarray(wrap_frequency(folded - np.polyval(coeff, mode), reference_fsr_mhz), dtype=float)
    return float(np.sqrt(np.mean(residual**2)))


def fit_track_model(track: list[dict[str, float]]) -> tuple[int, np.ndarray]:
    if not track:
        return 0, np.array([0.0], dtype=float)
    mode = np.array([row["mode_number_ref"] for row in track], dtype=float)
    folded = np.array([row["folded_freq_ref_mhz"] for row in track], dtype=float)
    degree = min(2, len(track) - 1)
    coeff = np.polyfit(mode, folded, deg=degree)
    return degree, coeff


def predict_track_model(model: tuple[int, np.ndarray], mode: int) -> float:
    _degree, coeff = model
    return float(np.polyval(coeff, mode))


def assign_rows_to_track_models(
    by_mode: dict[int, list[dict[str, float]]],
    models: list[tuple[int, np.ndarray]],
    reference_fsr_mhz: float,
) -> list[list[dict[str, float]]]:
    tracks: list[list[dict[str, float]]] = [[] for _model in models]
    for mode, mode_rows in sorted(by_mode.items()):
        ordered_rows = sorted(mode_rows, key=lambda item: float(item["folded_freq_ref_mhz"]))
        assignment = best_unique_assignment(ordered_rows, mode, models, reference_fsr_mhz)
        for row_index, track_index in assignment:
            tracks[track_index].append(ordered_rows[row_index])
    return tracks


def best_unique_assignment(
    rows: list[dict[str, float]],
    mode: int,
    models: list[tuple[int, np.ndarray]],
    reference_fsr_mhz: float,
) -> list[tuple[int, int]]:
    if not rows or not models:
        return []
    pair_count = min(len(rows), len(models))
    best_cost = math.inf
    best_pairs: list[tuple[int, int]] = []
    row_indices_options = itertools.combinations(range(len(rows)), pair_count)
    for row_indices in row_indices_options:
        for track_indices in itertools.permutations(range(len(models)), pair_count):
            cost = 0.0
            pairs: list[tuple[int, int]] = []
            for row_index, track_index in zip(row_indices, track_indices):
                folded = float(rows[row_index]["folded_freq_ref_mhz"])
                predicted = predict_track_model(models[track_index], mode)
                residual = float(wrap_frequency(folded - predicted, reference_fsr_mhz))
                cost += abs(residual) / max(float(rows[row_index]["depth_1_minus_norm"]), 0.05)
                pairs.append((row_index, track_index))
            if cost < best_cost:
                best_cost = cost
                best_pairs = pairs
    return best_pairs


def track_sort_key(track: list[dict[str, float]]) -> tuple[float, int]:
    depth = -float(np.nanmedian([row["depth_1_minus_norm"] for row in track]))
    count = -len(track)
    return depth, count


def split_band_by_folded_rank(
    rows: list[dict[str, float]],
    reference_fsr_mhz: float,
) -> list[list[dict[str, float]]]:
    """Split one depth band into parallel tracks.

    When two same-depth families appear in the same folded-FSR bin, choosing
    only the deepest point per mode can stitch them into a zig-zag sequence.
    Ranking dips by folded frequency within each coarse mode bin preserves
    parallel tracks before each track is re-centered by `assign_sequence_modes`.
    """
    by_mode: dict[int, list[dict[str, float]]] = {}
    for row in rows:
        by_mode.setdefault(int(row["mode_number_ref"]), []).append(row)
    if not by_mode:
        return []

    max_rank = max(len(mode_rows) for mode_rows in by_mode.values())
    tracks: list[list[dict[str, float]]] = []
    for rank in range(max_rank):
        track: list[dict[str, float]] = []
        for _mode, mode_rows in sorted(by_mode.items()):
            ordered = sorted(mode_rows, key=lambda item: float(item["folded_freq_ref_mhz"]))
            if rank < len(ordered):
                track.append(ordered[rank])
        if len(track) >= MIN_DEPTH_CLUSTER_SIZE:
            tracks.append(track)

    if tracks:
        return tracks
    if len(rows) >= MIN_DEPTH_CLUSTER_SIZE:
        return [rows]
    return []


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
    ax.set_xlabel("Mode number within each seed family", fontsize=18)
    ax.set_ylabel("Family-centered folded frequency (GHz)", fontsize=18)
    ax.set_title(
        f"Seed family assignment, family-centered coordinates, ref FSR={reference_fsr_mhz/1000:.4g} GHz",
        fontsize=20,
    )
    ax.tick_params(axis="both", labelsize=15)
    ax.legend(loc="upper right", fontsize=16)
    ax.text(
        0.02,
        0.03,
        "Each seed family is centered separately; use the common-coordinate plot for inter-family frequency spacing.",
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


def find_large_scan_data_path(dip_table: Path, stem: str) -> Path | None:
    for suffix in (".npz", ".csv"):
        candidate = dip_table.with_name(f"{stem}{suffix}")
        if candidate.exists():
            return candidate
    return None


def choose_common_origin(
    families: dict[str, list[dict[str, float]]],
    fits: list[dict[str, object]],
) -> tuple[str, dict[str, float], float] | None:
    display_labels = display_labels_by_depth(families)
    fit_by_name = {str(fit["name"]): fit for fit in fits if "quadratic" in fit}
    candidates: list[tuple[int, float, str, dict[str, float], float]] = []
    for name, rows in families.items():
        fit = fit_by_name.get(name)
        if not rows or fit is None:
            continue
        label = display_labels.get(name, name)
        label_rank = 0 if label == "mode1" else 1
        center_row = min(rows, key=lambda row: abs(float(row["relative_freq_mhz"])))
        d1_mhz = float(fit["quadratic"]["effective_d1_mhz"])
        candidates.append((label_rank, abs(float(center_row["relative_freq_mhz"])), name, center_row, d1_mhz))
    if not candidates:
        return None
    _rank, _distance, name, row, d1_mhz = sorted(candidates, key=lambda item: (item[0], item[1]))[0]
    return name, row, d1_mhz


def build_common_rows(
    rows: list[dict[str, float]],
    *,
    origin_mhz: float,
    common_d1_mhz: float,
) -> list[dict[str, float]]:
    common_rows: list[dict[str, float]] = []
    for row in rows:
        mode, folded = common_mode_and_folded(
            float(row["relative_freq_mhz"]),
            origin_mhz=origin_mhz,
            common_d1_mhz=common_d1_mhz,
        )
        item = dict(row)
        item["common_mode_number"] = float(mode)
        item["common_folded_mhz"] = float(folded)
        common_rows.append(item)
    return common_rows


def assigned_labels_by_sample(families: dict[str, list[dict[str, float]]]) -> dict[int, tuple[str, dict[str, float]]]:
    display_labels = display_labels_by_depth(families)
    assigned: dict[int, tuple[str, dict[str, float]]] = {}
    for family, family_rows in families.items():
        label = display_labels.get(family, family)
        for row in family_rows:
            assigned[int(float(row["sample_index"]))] = (label, row)
    return assigned


def draw_mu0_side_panel(
    side: plt.Axes,
    *,
    dip_table: Path,
    stem: str,
    common_rows: list[dict[str, float]],
    assigned_by_sample: dict[int, tuple[str, dict[str, float]]],
    common_d1_mhz: float,
) -> None:
    side_rows = sorted(
        [row for row in common_rows if int(row["common_mode_number"]) == 0],
        key=lambda item: float(item["common_folded_mhz"]),
    )
    data_path = find_large_scan_data_path(dip_table, stem)
    plotted_trace = False
    if data_path is not None and len(side_rows) >= 2:
        try:
            _time_s, _trigger, trans_raw, _mzi_raw = read_large_scan_data(data_path)
            trans_norm, _baseline = normalize_transmission_with_baseline(trans_raw)
            control = sorted(
                (
                    int(float(row["sample_index"])),
                    float(row["common_mode_number"]) * common_d1_mhz + float(row["common_folded_mhz"]),
                )
                for row in common_rows
            )
            control_samples = np.array([item[0] for item in control], dtype=float)
            control_unfolded = np.array([item[1] for item in control], dtype=float)
            side_samples = np.array([int(float(row["sample_index"])) for row in side_rows], dtype=int)
            pad = max(10_000, int(0.15 * (side_samples.max() - side_samples.min())))
            lo = max(0, int(side_samples.min()) - pad)
            hi = min(len(trans_norm) - 1, int(side_samples.max()) + pad)
            step = max(1, (hi - lo) // 60_000)
            sample_grid = np.arange(lo, hi + 1, step)
            unfolded = np.interp(sample_grid, control_samples, control_unfolded)
            trace_mode = np.rint(unfolded / common_d1_mhz).astype(int)
            trace_folded = unfolded - trace_mode * common_d1_mhz
            mask = np.abs(trace_folded) <= common_d1_mhz / 2.0
            side.plot(trans_norm[sample_grid][mask], trace_folded[mask] / 1000.0, color="#333333", lw=0.75)
            plotted_trace = True
        except Exception as exc:  # pragma: no cover - diagnostic plot should not break fitting.
            side.text(
                0.5,
                0.05,
                f"raw trace unavailable:\n{exc}",
                transform=side.transAxes,
                ha="center",
                va="bottom",
                fontsize=8,
            )

    for row in side_rows:
        sample = int(float(row["sample_index"]))
        label_row = assigned_by_sample.get(sample)
        if label_row is None:
            label = "unassigned"
            color = "#ff7f0e" if float(row["depth_1_minus_norm"]) > 0.7 else "#777777"
        else:
            label = label_row[0]
            color = DISPLAY_COLORS.get(label, "#333333")
        x_value = float(row["norm_transmission"]) if plotted_trace else 1.0 - float(row["depth_1_minus_norm"])
        y_value = float(row["common_folded_mhz"]) / 1000.0
        side.scatter(x_value, y_value, s=70, color=color, edgecolor="white", linewidth=0.8, zorder=6)
        if label != "unassigned" or float(row["depth_1_minus_norm"]) > 0.7:
            side.annotate(
                f"{label}\n{float(row['wavelength_nm_linear']):.6f} nm",
                xy=(x_value, y_value),
                xytext=(min(1.03, x_value + 0.12), y_value + 4.0),
                arrowprops={"arrowstyle": "->", "color": color, "lw": 0.9},
                color=color,
                fontsize=8,
                bbox={"boxstyle": "round,pad=0.2", "facecolor": "white", "edgecolor": color, "alpha": 0.88},
            )

    side.axhline(0.0, color="black", lw=1.0)
    side.set_xlabel("Normalized CH2" if plotted_trace else "1 - depth", fontsize=13)
    side.set_title("mu=0 one FSR", fontsize=14)
    side.set_xlim(1.05, -0.05)
    side.grid(alpha=0.22)


def plot_common_fits_with_side_panel(
    path: Path,
    *,
    dip_table: Path,
    stem: str,
    rows: list[dict[str, float]],
    families: dict[str, list[dict[str, float]]],
    fits: list[dict[str, object]],
) -> None:
    origin = choose_common_origin(families, fits)
    if origin is None:
        fig, ax = plt.subplots(figsize=(8, 4), constrained_layout=True)
        ax.text(0.5, 0.5, "No fitted family available for common-coordinate plot", ha="center", va="center")
        ax.axis("off")
        fig.savefig(path, dpi=180)
        plt.close(fig)
        return

    origin_name, origin_row, common_d1_mhz = origin
    origin_mhz = float(origin_row["relative_freq_mhz"])
    display_labels = display_labels_by_depth(families)
    fit_by_name = {str(fit["name"]): fit for fit in fits if "quadratic" in fit}
    common_rows = build_common_rows(rows, origin_mhz=origin_mhz, common_d1_mhz=common_d1_mhz)

    fig = plt.figure(figsize=(15, 7.2), constrained_layout=True)
    gs = fig.add_gridspec(1, 2, width_ratios=[4.2, 1.25])
    ax = fig.add_subplot(gs[0, 0])
    side = fig.add_subplot(gs[0, 1], sharey=ax)

    if common_rows:
        sc = ax.scatter(
            [row["common_mode_number"] for row in common_rows],
            [row["common_folded_mhz"] / 1000.0 for row in common_rows],
            c=[row["depth_1_minus_norm"] for row in common_rows],
            s=28,
            cmap="Greys",
            vmin=0.2,
            vmax=1.0,
            alpha=0.45,
            linewidths=0,
            label="depth-filtered dips",
        )
    else:
        sc = None

    assigned_by_sample: dict[int, tuple[str, dict[str, float]]] = {}
    for family, family_rows in families.items():
        fit = fit_by_name.get(family)
        if fit is None or not family_rows:
            continue
        label = display_labels.get(family, family)
        color = DISPLAY_COLORS.get(label, "#333333")
        points: list[tuple[int, float, float]] = []
        for row in family_rows:
            mode, folded = common_mode_and_folded(
                float(row["relative_freq_mhz"]),
                origin_mhz=origin_mhz,
                common_d1_mhz=common_d1_mhz,
            )
            points.append((mode, folded, float(row["depth_1_minus_norm"])))
            assigned_by_sample[int(float(row["sample_index"]))] = (label, row)
        if not points:
            continue
        points = sorted(points, key=lambda item: item[0])
        segments: list[list[tuple[int, float, float]]] = [[]]
        for point in points:
            if segments[-1] and abs(point[1] - segments[-1][-1][1]) > common_d1_mhz / 2.0:
                segments.append([])
            segments[-1].append(point)
        label_used = False
        for segment in segments:
            if not segment:
                continue
            ax.plot(
                [item[0] for item in segment],
                [item[1] / 1000.0 for item in segment],
                marker="o",
                ms=5.5,
                lw=2.2,
                color=color,
                label=label if not label_used else None,
            )
            label_used = True

    ax.axhline(0.0, color="black", lw=1.0)
    ax.axhline(common_d1_mhz / 2000.0, color="#888888", lw=0.9, ls="--")
    ax.axhline(-common_d1_mhz / 2000.0, color="#888888", lw=0.9, ls="--")
    origin_label = display_labels.get(origin_name, origin_name)
    ax.set_xlabel(f"Mode number, common origin = {origin_label} M=0", fontsize=16)
    ax.set_ylabel("Folded frequency, common coordinates (GHz)", fontsize=16)
    ax.set_title(
        f"Common-coordinate dispersion map with mu=0 one-FSR panel; D1={common_d1_mhz/1000:.6g} GHz",
        fontsize=17,
    )
    ax.grid(alpha=0.22)
    ax.legend(loc="upper right", fontsize=12)
    ax.tick_params(axis="both", labelsize=13)
    if sc is not None:
        cb = fig.colorbar(sc, ax=ax, pad=0.01)
        cb.set_label("Depth", fontsize=14)

    draw_mu0_side_panel(
        side,
        dip_table=dip_table,
        stem=stem,
        common_rows=common_rows,
        assigned_by_sample=assigned_by_sample,
        common_d1_mhz=common_d1_mhz,
    )
    plt.setp(side.get_yticklabels(), visible=False)

    fig.savefig(path, dpi=220)
    plt.close(fig)


def plot_auto_centered_fits(
    path: Path,
    *,
    dip_table: Path,
    stem: str,
    all_rows: list[dict[str, float]],
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

    origin = choose_common_origin(families, fits)
    fig = plt.figure(figsize=(14, max(5.5, 3.5 * len(visible_fits))), constrained_layout=True)
    gs = fig.add_gridspec(len(visible_fits), 2, width_ratios=[3.4, 1.1])
    axes = [fig.add_subplot(gs[index, 0]) for index in range(len(visible_fits))]
    side = fig.add_subplot(gs[:, 1])
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
    if origin is not None:
        _origin_name, origin_row, common_d1_mhz = origin
        common_rows = build_common_rows(
            all_rows,
            origin_mhz=float(origin_row["relative_freq_mhz"]),
            common_d1_mhz=common_d1_mhz,
        )
        draw_mu0_side_panel(
            side,
            dip_table=dip_table,
            stem=stem,
            common_rows=common_rows,
            assigned_by_sample=assigned_labels_by_sample(families),
            common_d1_mhz=common_d1_mhz,
        )
    else:
        side.text(0.5, 0.5, "No fitted family\nfor one-FSR panel", ha="center", va="center")
        side.axis("off")
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
    common_fit_plot_path = output_dir / f"{stem}_dispersion_common_with_mu0_panel_depth_gt_{args.depth_threshold:g}.png".replace(".", "p")
    common_fit_plot_path = common_fit_plot_path.with_name(common_fit_plot_path.name.replace("ppng", ".png"))
    plot_common_fits_with_side_panel(
        common_fit_plot_path,
        dip_table=dip_table,
        stem=stem,
        rows=rows,
        families=auto_families,
        fits=auto_fits,
    )
    auto_fit_plot_path = output_dir / f"{stem}_dispersion_auto_centered_depth_gt_{args.depth_threshold:g}.png".replace(".", "p")
    auto_fit_plot_path = auto_fit_plot_path.with_name(auto_fit_plot_path.name.replace("ppng", ".png"))
    plot_auto_centered_fits(
        auto_fit_plot_path,
        dip_table=dip_table,
        stem=stem,
        all_rows=rows,
        families=auto_families,
        fits=auto_fits,
    )

    summary = {
        "config": asdict(config),
        "depth_filtered_dip_count": len(rows),
        "family_points_csv": str(family_points_path),
        "auto_centered_family_points_csv": str(auto_family_points_path),
        "common_coordinate_fit_figure": str(common_fit_plot_path),
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
