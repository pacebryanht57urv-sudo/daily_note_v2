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
from data_paths import CAMPAIGN_ENV, CHIP_ENV, DATA_ROOT_ENV, default_campaign, default_chip, default_cavity_dir
from process_large_scan import normalize_transmission_with_baseline, read_large_scan_data

DISPLAY_COLORS = {
    "mode1": "#1f77b4",
    "mode2": "#d62728",
    "mode3": "#2ca02c",
    "mode4": "#9467bd",
}
FAMILY_KEYS = ("lower_branch", "upper_branch", "middle_branch", "extra_branch")
MIN_TRACK_SPACING_FRACTION = 0.88
MAX_TRACK_SPACING_FRACTION = 1.12
MAX_TRACK_MODE_SKIP = 3
MIN_DEPTH_CLUSTER_GAP = 0.12
MIN_DEPTH_CLUSTER_SIZE = 4
MIN_PARALLEL_BIN_COUNT = 4
MAX_SEED_TRACK_RMS_FRACTION = 0.008
MAX_TRACK_SPACING_ERROR_FRACTION = 0.08
MAX_TRACK_SPACING_RMS_FRACTION = 0.04
MIN_RECOVERED_FAMILY_POINTS = 8
MIN_RECOVERED_MODE_SPAN = 6
MAX_RECOVERED_RMS_MHZ = 120.0
MAX_RECOVERED_ABS_RESIDUAL_MHZ = 500.0
RECOVERY_INITIAL_WINDOW_FRACTION = 0.18
RECOVERY_DUPLICATE_OVERLAP_FRACTION = 0.55
BRANCH_EXTENSION_RESIDUAL_FRACTION = 0.025
BRANCH_EXTENSION_MIN_TOLERANCE_MHZ = 2_500.0
BRANCH_EXTENSION_RMS_GROWTH_LIMIT = 1.25
BRANCH_EXTENSION_ABS_RESIDUAL_FRACTION = 0.004
BRANCH_EXTENSION_MIN_DEPTH = 0.35


@dataclass
class FitConfig:
    dip_table: str
    depth_threshold: float
    reference_fsr_mhz: float
    reference_fsr_source: str
    auto_center_tolerance_mhz: float
    auto_center_iterations: int
    output_dir: str


def process_summary_fsr_mhz(dip_table: Path, output_dir: Path, stem: str) -> float | None:
    candidates = [
        output_dir / f"{stem}_process_summary.json",
        output_dir / "process_summary.json",
        dip_table.with_name(f"{stem}_process_summary.json"),
        dip_table.with_name("process_summary.json"),
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
            return float(summary["config"]["disk_fsr_mhz"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def poly2(mode: np.ndarray, c0: float, d1_corr: float, d2: float) -> np.ndarray:
    return c0 + d1_corr * mode + 0.5 * d2 * mode**2


def poly3(mode: np.ndarray, c0: float, d1_corr: float, d2: float, d3: float) -> np.ndarray:
    return c0 + d1_corr * mode + 0.5 * d2 * mode**2 + d3 * mode**3 / 6.0


def wrap_frequency(freq_mhz: np.ndarray | float, fsr_mhz: float) -> np.ndarray | float:
    return (freq_mhz + fsr_mhz / 2.0) % fsr_mhz - fsr_mhz / 2.0


def folded_key_for_mode_key(mode_key: str) -> str:
    return "folded_freq_centered_mhz" if mode_key.endswith("_centered") else "folded_freq_ref_mhz"


def unwrapped_track_arrays(
    track: list[dict[str, float]],
    reference_fsr_mhz: float,
    *,
    mode_key: str,
    folded_key: str | None = None,
) -> tuple[list[dict[str, float]], np.ndarray, np.ndarray]:
    ordered = sorted(track, key=lambda item: int(item[mode_key]))
    if not ordered:
        return [], np.array([], dtype=float), np.array([], dtype=float)
    folded_key = folded_key or folded_key_for_mode_key(mode_key)
    modes = np.array([int(row[mode_key]) for row in ordered], dtype=float)
    folded = [float(row[folded_key]) for row in ordered]
    center_index = min(range(len(ordered)), key=lambda index: abs(modes[index]))
    unwrapped: list[float | None] = [None] * len(ordered)
    unwrapped[center_index] = folded[center_index]

    for index in range(center_index + 1, len(ordered)):
        previous = float(unwrapped[index - 1])
        k = round((previous - folded[index]) / reference_fsr_mhz)
        candidates = [folded[index] + (k + delta) * reference_fsr_mhz for delta in range(-2, 3)]
        unwrapped[index] = min(candidates, key=lambda value: abs(value - previous))

    for index in range(center_index - 1, -1, -1):
        previous = float(unwrapped[index + 1])
        k = round((previous - folded[index]) / reference_fsr_mhz)
        candidates = [folded[index] + (k + delta) * reference_fsr_mhz for delta in range(-2, 3)]
        unwrapped[index] = min(candidates, key=lambda value: abs(value - previous))

    return ordered, modes, np.array([float(value) for value in unwrapped], dtype=float)


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


def split_families_by_parallel_bins(rows: list[dict[str, float]]) -> dict[str, list[dict[str, float]]]:
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


def assigned_row_count(families: dict[str, list[dict[str, float]]]) -> int:
    seen = {int(row["sample_index"]) for family_rows in families.values() for row in family_rows}
    return len(seen)


def populated_family_count(families: dict[str, list[dict[str, float]]], min_count: int = MIN_DEPTH_CLUSTER_SIZE) -> int:
    return sum(1 for family_rows in families.values() if len(family_rows) >= min_count)


def split_families(rows: list[dict[str, float]], reference_fsr_mhz: float) -> dict[str, list[dict[str, float]]]:
    continuous = split_families_by_continuous_fsr(rows, reference_fsr_mhz)
    binned = {
        family: family_rows if track_is_spacing_usable(family_rows, reference_fsr_mhz) else []
        for family, family_rows in split_families_by_parallel_bins(rows).items()
    }

    continuous_families = populated_family_count(continuous)
    binned_families = populated_family_count(binned)
    continuous_rows = assigned_row_count(continuous)
    binned_rows = assigned_row_count(binned)

    # The graph-based continuous-FSR splitter is precise when all visible branches
    # connect cleanly, but it can return after finding only one branch and leave
    # obvious one-FSR panel modes unassigned. In that case, prefer the parallel
    # per-mode-bin assignment used for multi-family chip7 scans.
    if binned_families > continuous_families and binned_rows >= max(continuous_rows, MIN_PARALLEL_BIN_COUNT):
        return binned
    if any(continuous.values()):
        return continuous
    return binned


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

    components = connected_components_by_fsr(rows, reference_fsr_mhz)
    assigned_components = filter_usable_seed_tracks(
        [assign_sequence_modes(component, reference_fsr_mhz) for component in components if len(component) >= 4],
        reference_fsr_mhz,
    )
    if assigned_components:
        for key, component in zip(FAMILY_KEYS, sorted(assigned_components, key=track_sort_key)):
            families[key] = component
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
        models = [fit_track_model(track, reference_fsr_mhz) for track in tracks]
        tracks = assign_rows_to_track_models(by_mode, models, reference_fsr_mhz)

    return [track for track in tracks if len(track) >= MIN_DEPTH_CLUSTER_SIZE]


def filter_usable_seed_tracks(
    tracks: list[list[dict[str, float]]],
    reference_fsr_mhz: float,
) -> list[list[dict[str, float]]]:
    max_rms_mhz = MAX_SEED_TRACK_RMS_FRACTION * reference_fsr_mhz
    return [
        track
        for track in tracks
        if track_quadratic_rms_mhz(track, reference_fsr_mhz) <= max_rms_mhz
        and track_is_spacing_usable(track, reference_fsr_mhz)
    ]


def track_quadratic_rms_mhz(track: list[dict[str, float]], reference_fsr_mhz: float) -> float:
    if len(track) < MIN_DEPTH_CLUSTER_SIZE:
        return math.inf
    _ordered, mode, folded = unwrapped_track_arrays(track, reference_fsr_mhz, mode_key="mode_number_ref")
    degree = min(2, len(track) - 1)
    coeff = np.polyfit(mode, folded, deg=degree)
    residual = folded - np.polyval(coeff, mode)
    return float(np.sqrt(np.mean(residual**2)))


def track_spacing_quality(
    track: list[dict[str, float]],
    reference_fsr_mhz: float,
    *,
    mode_key: str = "mode_number_ref",
) -> dict[str, float | int | bool]:
    if len(track) < 2:
        return {
            "adjacent_pair_count": 0,
            "max_mode_gap": 0,
            "median_spacing_per_mode_mhz": math.nan,
            "rms_spacing_error_mhz": math.inf,
            "max_abs_spacing_error_mhz": math.inf,
            "max_abs_spacing_error_fraction": math.inf,
            "spacing_ok": False,
        }

    ordered, modes, unwrapped = unwrapped_track_arrays(track, reference_fsr_mhz, mode_key=mode_key)
    errors: list[float] = []
    spacings: list[float] = []
    max_mode_gap = 0
    for index in range(len(ordered) - 1):
        mode_gap = int(modes[index + 1] - modes[index])
        if mode_gap <= 0:
            continue
        folded_step_per_mode = float(unwrapped[index + 1] - unwrapped[index]) / mode_gap
        spacing_per_mode = reference_fsr_mhz + folded_step_per_mode
        spacings.append(spacing_per_mode)
        errors.append(folded_step_per_mode)
        max_mode_gap = max(max_mode_gap, mode_gap)

    if not errors:
        rms_error = math.inf
        max_abs_error = math.inf
        max_abs_fraction = math.inf
        median_spacing = math.nan
    else:
        err = np.array(errors, dtype=float)
        rms_error = float(np.sqrt(np.mean(err**2)))
        max_abs_error = float(np.max(np.abs(err)))
        max_abs_fraction = max_abs_error / reference_fsr_mhz
        median_spacing = float(np.median(spacings))
    spacing_ok = (
        len(errors) >= max(1, min(len(track) - 1, MIN_DEPTH_CLUSTER_SIZE - 1))
        and max_mode_gap <= MAX_TRACK_MODE_SKIP
        and max_abs_fraction <= MAX_TRACK_SPACING_ERROR_FRACTION
        and rms_error / reference_fsr_mhz <= MAX_TRACK_SPACING_RMS_FRACTION
    )
    return {
        "adjacent_pair_count": len(errors),
        "max_mode_gap": int(max_mode_gap),
        "median_spacing_per_mode_mhz": median_spacing,
        "rms_spacing_error_mhz": rms_error,
        "max_abs_spacing_error_mhz": max_abs_error,
        "max_abs_spacing_error_fraction": max_abs_fraction,
        "spacing_ok": bool(spacing_ok),
    }


def track_is_spacing_usable(
    track: list[dict[str, float]],
    reference_fsr_mhz: float,
    *,
    mode_key: str = "mode_number_ref",
) -> bool:
    return bool(track_spacing_quality(track, reference_fsr_mhz, mode_key=mode_key)["spacing_ok"])


def fit_track_model(track: list[dict[str, float]], reference_fsr_mhz: float) -> tuple[int, np.ndarray]:
    if not track:
        return 0, np.array([0.0], dtype=float)
    _ordered, mode, folded = unwrapped_track_arrays(track, reference_fsr_mhz, mode_key="mode_number_ref")
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
    ordered_rows, mode, folded = unwrapped_track_arrays(rows, reference_fsr_mhz, mode_key="mode_number_ref")
    depth = np.array([row["depth_1_minus_norm"] for row in ordered_rows], dtype=float)
    sigma = 1.0 / np.maximum(depth, 0.05)

    p2, _ = curve_fit(poly2, mode, folded, sigma=sigma, absolute_sigma=False, maxfev=20_000)
    r2 = folded - poly2(mode, *p2)
    p3, _ = curve_fit(poly3, mode, folded, sigma=sigma, absolute_sigma=False, maxfev=20_000)
    r3 = folded - poly3(mode, *p3)
    spacing_quality = track_spacing_quality(rows, reference_fsr_mhz, mode_key="mode_number_ref")
    return {
        "name": name,
        "count": len(rows),
        "mode_min": int(np.min(mode)),
        "mode_max": int(np.max(mode)),
        "spacing_quality": spacing_quality,
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
            for row in ordered_rows
        ],
    }


def fit_family_centered(name: str, rows: list[dict[str, float]], reference_fsr_mhz: float) -> dict[str, object]:
    if len(rows) < 4:
        return {"name": name, "count": len(rows), "status": "too_few_points"}
    ordered_rows, mode, folded = unwrapped_track_arrays(rows, reference_fsr_mhz, mode_key="mode_number_centered")
    depth = np.array([row["depth_1_minus_norm"] for row in ordered_rows], dtype=float)
    sigma = 1.0 / np.maximum(depth, 0.05)

    p2, _ = curve_fit(poly2, mode, folded, sigma=sigma, absolute_sigma=False, maxfev=20_000)
    r2 = folded - poly2(mode, *p2)
    p3, _ = curve_fit(poly3, mode, folded, sigma=sigma, absolute_sigma=False, maxfev=20_000)
    r3 = folded - poly3(mode, *p3)
    spacing_quality = track_spacing_quality(ordered_rows, reference_fsr_mhz, mode_key="mode_number_centered")
    return {
        "name": name,
        "count": len(ordered_rows),
        "auto_offset_mhz": float(ordered_rows[0]["auto_offset_mhz"]),
        "mode_min": int(np.min(mode)),
        "mode_max": int(np.max(mode)),
        "spacing_quality": spacing_quality,
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
            for index, row in enumerate(ordered_rows)
        ],
    }


def select_closest_per_mode(
    rows: list[dict[str, float]],
    *,
    reference_fsr_mhz: float,
    fit_params: np.ndarray,
    tolerance_mhz: float,
) -> list[dict[str, float]]:
    by_mode: dict[int, tuple[tuple[float, float], dict[str, float]]] = {}
    for row in rows:
        mode = int(row["mode_number_centered"])
        predicted = poly2(np.array([mode], dtype=float), *fit_params)[0]
        residual = float(wrap_frequency(row["folded_freq_centered_mhz"] - predicted, reference_fsr_mhz))
        if abs(residual) > tolerance_mhz:
            continue
        score = (abs(residual), -float(row["depth_1_minus_norm"]))
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


def prune_auto_centered_family_outliers(
    families: dict[str, list[dict[str, float]]],
    *,
    reference_fsr_mhz: float,
    max_abs_residual_mhz: float = 500.0,
    rms_residual_mhz: float = 120.0,
    min_points: int = 10,
) -> tuple[dict[str, list[dict[str, float]]], dict[str, list[dict[str, float]]]]:
    pruned: dict[str, list[dict[str, float]]] = {}
    prune_log: dict[str, list[dict[str, float]]] = {}
    for family, rows in families.items():
        current = list(rows)
        removed: list[dict[str, float]] = []
        while len(current) > min_points:
            fit = fit_family_centered(family, current, reference_fsr_mhz)
            quad = fit.get("quadratic")
            points = fit.get("points", [])
            if not quad or not points:
                break
            if (
                float(quad["max_abs_residual_mhz"]) <= max_abs_residual_mhz
                and float(quad["rms_residual_mhz"]) <= rms_residual_mhz
            ):
                break
            worst = max(points, key=lambda item: abs(float(item["residual_mhz"])))
            worst_index = int(worst["sample_index"])
            removed.append(
                {
                    "sample_index": worst_index,
                    "mode_number": int(worst["mode_number"]),
                    "wavelength_nm": float(worst["wavelength_nm"]),
                    "residual_mhz": float(worst["residual_mhz"]),
                }
            )
            current = [row for row in current if int(round(float(row["sample_index"]))) != worst_index]
        pruned[family] = current
        prune_log[family] = removed
    return pruned, prune_log


def sample_index(row: dict[str, float]) -> int:
    return int(round(float(row["sample_index"])))


def family_sample_indices(rows: list[dict[str, float]]) -> set[int]:
    return {sample_index(row) for row in rows}


def candidate_balance_ok(rows: list[dict[str, float]]) -> bool:
    modes = [int(row["mode_number_centered"]) for row in rows]
    if not modes:
        return False
    if max(modes) - min(modes) < MIN_RECOVERED_MODE_SPAN:
        return False
    return sum(mode < 0 for mode in modes) >= 2 and sum(mode > 0 for mode in modes) >= 2


def fit_is_recovered_family_candidate(fit: dict[str, object], rows: list[dict[str, float]]) -> bool:
    if len(rows) < MIN_RECOVERED_FAMILY_POINTS:
        return False
    if not candidate_balance_ok(rows):
        return False
    quadratic = fit.get("quadratic")
    if not quadratic:
        return False
    if float(quadratic["rms_residual_mhz"]) > MAX_RECOVERED_RMS_MHZ:
        return False
    if float(quadratic["max_abs_residual_mhz"]) > MAX_RECOVERED_ABS_RESIDUAL_MHZ:
        return False
    spacing = fit.get("spacing_quality", {})
    return bool(spacing.get("spacing_ok", False))


def recover_candidate_from_seed(
    name: str,
    seed_row: dict[str, float],
    candidate_rows: list[dict[str, float]],
    *,
    reference_fsr_mhz: float,
    tolerance_mhz: float,
    iterations: int,
) -> tuple[list[dict[str, float]], dict[str, object]] | None:
    offset_mhz = -float(seed_row["folded_freq_ref_mhz"])
    centered_all = [add_centered_coordinates(row, reference_fsr_mhz, offset_mhz) for row in candidate_rows]
    initial_window_mhz = max(tolerance_mhz * 2.0, reference_fsr_mhz * RECOVERY_INITIAL_WINDOW_FRACTION)

    zero_params = np.array([0.0, 0.0, 0.0], dtype=float)
    selected = select_closest_per_mode(
        centered_all,
        reference_fsr_mhz=reference_fsr_mhz,
        fit_params=zero_params,
        tolerance_mhz=initial_window_mhz,
    )
    if len(selected) < MIN_RECOVERED_FAMILY_POINTS:
        return None

    try:
        fit = fit_family_centered(name, selected, reference_fsr_mhz)
        for _ in range(max(1, iterations)):
            quadratic = fit.get("quadratic")
            if not quadratic:
                return None
            params = np.array(
                [
                    float(quadratic["offset_mhz"]),
                    float(quadratic["d1_correction_mhz"]),
                    float(quadratic["d2_mhz_per_mode2"]),
                ],
                dtype=float,
            )
            selected = select_closest_per_mode(
                centered_all,
                reference_fsr_mhz=reference_fsr_mhz,
                fit_params=params,
                tolerance_mhz=tolerance_mhz,
            )
            if len(selected) < MIN_RECOVERED_FAMILY_POINTS:
                return None
            fit = fit_family_centered(name, selected, reference_fsr_mhz)
    except (RuntimeError, ValueError, TypeError):
        return None

    if not fit_is_recovered_family_candidate(fit, selected):
        return None
    fit["auto_center_mode"] = "residual_gui_style_offset_search"
    fit["seed_sample_index"] = sample_index(seed_row)
    fit["seed_wavelength_nm"] = float(seed_row["wavelength_nm_linear"])
    fit["auto_center_tolerance_mhz"] = tolerance_mhz
    return selected, fit


def candidate_sort_key(candidate: tuple[list[dict[str, float]], dict[str, object]]) -> tuple[float, float, int]:
    rows, fit = candidate
    quadratic = fit["quadratic"]
    spacing = fit["spacing_quality"]
    return (
        float(quadratic["rms_residual_mhz"]),
        float(spacing["rms_spacing_error_mhz"]),
        -len(rows),
    )


def candidate_overlap_fraction(rows_a: list[dict[str, float]], rows_b: list[dict[str, float]]) -> float:
    samples_a = family_sample_indices(rows_a)
    samples_b = family_sample_indices(rows_b)
    if not samples_a or not samples_b:
        return 0.0
    return len(samples_a & samples_b) / min(len(samples_a), len(samples_b))


def recover_residual_centered_families(
    all_rows: list[dict[str, float]],
    families: dict[str, list[dict[str, float]]],
    *,
    reference_fsr_mhz: float,
    tolerance_mhz: float,
    iterations: int,
) -> tuple[dict[str, list[dict[str, float]]], list[dict[str, object]]]:
    available_keys = [key for key in FAMILY_KEYS if len(families.get(key, [])) < MIN_RECOVERED_FAMILY_POINTS]
    if not available_keys:
        return families, []

    assigned = set().union(*(family_sample_indices(rows) for rows in families.values()))
    residual_rows = [row for row in all_rows if sample_index(row) not in assigned]
    if len(residual_rows) < MIN_RECOVERED_FAMILY_POINTS:
        return families, []

    candidates: list[tuple[list[dict[str, float]], dict[str, object]]] = []
    seed_rows = sorted(residual_rows, key=lambda row: -float(row["depth_1_minus_norm"]))
    for seed_index, seed_row in enumerate(seed_rows):
        candidate = recover_candidate_from_seed(
            f"recovered_{seed_index + 1}",
            seed_row,
            residual_rows,
            reference_fsr_mhz=reference_fsr_mhz,
            tolerance_mhz=tolerance_mhz,
            iterations=iterations,
        )
        if candidate is None:
            continue
        if any(
            candidate_overlap_fraction(candidate[0], existing_rows) >= RECOVERY_DUPLICATE_OVERLAP_FRACTION
            for existing_rows, _existing_fit in candidates
        ):
            continue
        candidates.append(candidate)

    if not candidates:
        return families, []

    updated = {key: list(rows) for key, rows in families.items()}
    recovery_log: list[dict[str, object]] = []
    used_samples = set(assigned)
    for key, (rows, fit) in zip(available_keys, sorted(candidates, key=candidate_sort_key)):
        row_samples = family_sample_indices(rows)
        if row_samples & used_samples:
            continue
        renamed = [dict(row) for row in rows]
        updated[key] = renamed
        used_samples |= row_samples
        recovery_log.append(
            {
                "family": key,
                "seed_sample_index": fit.get("seed_sample_index"),
                "seed_wavelength_nm": fit.get("seed_wavelength_nm"),
                "count": len(rows),
                "mode_min": fit.get("mode_min"),
                "mode_max": fit.get("mode_max"),
                "auto_offset_mhz": fit.get("auto_offset_mhz"),
                "quadratic": fit.get("quadratic"),
                "spacing_quality": fit.get("spacing_quality"),
            }
        )
        if len(recovery_log) >= len(available_keys):
            break
    return updated, recovery_log


def find_gui_offset_candidates(
    rows: list[dict[str, float]],
    *,
    reference_fsr_mhz: float,
    tolerance_mhz: float,
    iterations: int,
) -> list[tuple[list[dict[str, float]], dict[str, object]]]:
    candidates: list[tuple[list[dict[str, float]], dict[str, object]]] = []
    seed_rows = sorted(rows, key=lambda row: -float(row["depth_1_minus_norm"]))
    for seed_index, seed_row in enumerate(seed_rows):
        candidate = recover_candidate_from_seed(
            f"global_recovered_{seed_index + 1}",
            seed_row,
            rows,
            reference_fsr_mhz=reference_fsr_mhz,
            tolerance_mhz=tolerance_mhz,
            iterations=iterations,
        )
        if candidate is None:
            continue
        if any(
            candidate_overlap_fraction(candidate[0], existing_rows) >= RECOVERY_DUPLICATE_OVERLAP_FRACTION
            for existing_rows, _existing_fit in candidates
        ):
            continue
        candidates.append(candidate)
    return sorted(candidates, key=candidate_sort_key)


def select_nonoverlapping_gui_candidates(
    candidates: list[tuple[list[dict[str, float]], dict[str, object]]],
    *,
    max_family_count: int,
) -> list[tuple[list[dict[str, float]], dict[str, object]]]:
    selected: list[tuple[list[dict[str, float]], dict[str, object]]] = []
    used_samples: set[int] = set()
    for rows, fit in candidates:
        row_samples = family_sample_indices(rows)
        if row_samples & used_samples:
            continue
        selected.append((rows, fit))
        used_samples |= row_samples
        if len(selected) >= max_family_count:
            break
    return selected


def centered_family_quality_score(families: dict[str, list[dict[str, float]]], reference_fsr_mhz: float) -> tuple[int, int, int, float]:
    good_family_count = 0
    total_points = 0
    bad_gap_count = 0
    rms_sum = 0.0
    for family, rows in families.items():
        if len(rows) < MIN_RECOVERED_FAMILY_POINTS:
            continue
        fit = fit_family_centered(family, rows, reference_fsr_mhz)
        quadratic = fit.get("quadratic")
        spacing = fit.get("spacing_quality", {})
        if not quadratic:
            continue
        total_points += len(rows)
        rms_sum += float(quadratic["rms_residual_mhz"])
        if bool(spacing.get("spacing_ok", False)):
            good_family_count += 1
        else:
            bad_gap_count += 1
    return good_family_count, total_points, -bad_gap_count, -rms_sum


def recover_global_gui_offset_families(
    rows: list[dict[str, float]],
    families: dict[str, list[dict[str, float]]],
    *,
    reference_fsr_mhz: float,
    tolerance_mhz: float,
    iterations: int,
) -> tuple[dict[str, list[dict[str, float]]], list[dict[str, object]]]:
    candidates = find_gui_offset_candidates(
        rows,
        reference_fsr_mhz=reference_fsr_mhz,
        tolerance_mhz=tolerance_mhz,
        iterations=iterations,
    )
    selected = select_nonoverlapping_gui_candidates(candidates, max_family_count=len(FAMILY_KEYS))
    if len(selected) < 2:
        return families, []

    candidate_families: dict[str, list[dict[str, float]]] = {key: [] for key in FAMILY_KEYS}
    global_log: list[dict[str, object]] = []
    for key, (candidate_rows, fit) in zip(FAMILY_KEYS, selected):
        candidate_families[key] = [dict(row) for row in candidate_rows]
        global_log.append(
            {
                "family": key,
                "seed_sample_index": fit.get("seed_sample_index"),
                "seed_wavelength_nm": fit.get("seed_wavelength_nm"),
                "count": len(candidate_rows),
                "mode_min": fit.get("mode_min"),
                "mode_max": fit.get("mode_max"),
                "auto_offset_mhz": fit.get("auto_offset_mhz"),
                "quadratic": fit.get("quadratic"),
                "spacing_quality": fit.get("spacing_quality"),
            }
        )

    if centered_family_quality_score(candidate_families, reference_fsr_mhz) > centered_family_quality_score(
        families,
        reference_fsr_mhz,
    ):
        return candidate_families, global_log
    return families, []


def family_quadratic_params(fit: dict[str, object]) -> np.ndarray | None:
    quadratic = fit.get("quadratic")
    if not quadratic:
        return None
    return np.array(
        [
            float(quadratic["offset_mhz"]),
            float(quadratic["d1_correction_mhz"]),
            float(quadratic["d2_mhz_per_mode2"]),
        ],
        dtype=float,
    )


def edge_connected_candidate_blocks(
    existing_modes: set[int],
    candidate_modes: set[int],
) -> list[list[int]]:
    if not existing_modes or not candidate_modes:
        return []

    blocks: list[list[int]] = []
    min_mode = min(existing_modes)
    max_mode = max(existing_modes)

    left: list[int] = []
    mode = min_mode - 1
    while mode in candidate_modes:
        left.append(mode)
        mode -= 1
    if left:
        blocks.append(sorted(left))

    right: list[int] = []
    mode = max_mode + 1
    while mode in candidate_modes:
        right.append(mode)
        mode += 1
    if right:
        blocks.append(right)

    gap_block: list[int] = []
    for mode in range(min_mode + 1, max_mode):
        if mode in existing_modes:
            if gap_block:
                blocks.append(gap_block)
                gap_block = []
            continue
        if mode in candidate_modes:
            gap_block.append(mode)
        elif gap_block:
            gap_block = []
    if gap_block:
        blocks.append(gap_block)

    return blocks


def add_common_fit_coordinates(
    row: dict[str, float],
    *,
    origin_mhz: float,
    common_d1_mhz: float,
) -> dict[str, float]:
    mode, folded = common_mode_and_folded(
        float(row["relative_freq_mhz"]),
        origin_mhz=origin_mhz,
        common_d1_mhz=common_d1_mhz,
    )
    item = dict(row)
    item["auto_offset_mhz"] = 0.0
    item["mode_number_centered"] = mode
    item["folded_freq_centered_mhz"] = folded
    item["auto_coordinate_system"] = "common_coordinate_extension"
    return item


def extend_one_centered_family(
    family: str,
    family_rows: list[dict[str, float]],
    all_rows: list[dict[str, float]],
    assigned_elsewhere: set[int],
    *,
    reference_fsr_mhz: float,
    tolerance_mhz: float,
    common_origin_mhz: float,
    common_d1_mhz: float,
) -> tuple[list[dict[str, float]], dict[str, object] | None]:
    if len(family_rows) < 4:
        return family_rows, None

    common_family_rows = [
        add_common_fit_coordinates(row, origin_mhz=common_origin_mhz, common_d1_mhz=common_d1_mhz)
        for row in family_rows
    ]
    try:
        base_fit = fit_family_centered(family, common_family_rows, reference_fsr_mhz)
    except (RuntimeError, ValueError, TypeError):
        return family_rows, None

    params = family_quadratic_params(base_fit)
    if params is None:
        return family_rows, None

    extension_tolerance_mhz = min(
        tolerance_mhz,
        max(BRANCH_EXTENSION_MIN_TOLERANCE_MHZ, reference_fsr_mhz * BRANCH_EXTENSION_RESIDUAL_FRACTION),
    )
    existing_samples = family_sample_indices(family_rows)
    existing_modes = {int(row["mode_number_centered"]) for row in common_family_rows}
    by_mode: dict[int, tuple[tuple[float, float], dict[str, float]]] = {}

    for row in all_rows:
        row_sample = sample_index(row)
        if row_sample in existing_samples or row_sample in assigned_elsewhere:
            continue
        if float(row["depth_1_minus_norm"]) < BRANCH_EXTENSION_MIN_DEPTH:
            continue
        centered = add_common_fit_coordinates(row, origin_mhz=common_origin_mhz, common_d1_mhz=common_d1_mhz)
        mode = int(centered["mode_number_centered"])
        if mode in existing_modes:
            continue
        predicted = poly2(np.array([mode], dtype=float), *params)[0]
        residual = float(wrap_frequency(centered["folded_freq_centered_mhz"] - predicted, reference_fsr_mhz))
        if abs(residual) > extension_tolerance_mhz:
            continue
        candidate = dict(centered)
        candidate["auto_residual_mhz"] = residual
        score = (abs(residual), -float(candidate["depth_1_minus_norm"]))
        if mode not in by_mode or score < by_mode[mode][0]:
            by_mode[mode] = (score, candidate)

    candidate_blocks = edge_connected_candidate_blocks(existing_modes, set(by_mode))
    if not candidate_blocks:
        return family_rows, None

    base_quadratic = base_fit.get("quadratic", {})
    base_rms = float(base_quadratic.get("rms_residual_mhz", MAX_RECOVERED_RMS_MHZ))
    base_abs = float(base_quadratic.get("max_abs_residual_mhz", MAX_RECOVERED_ABS_RESIDUAL_MHZ))
    rms_limit = max(MAX_RECOVERED_RMS_MHZ, base_rms * BRANCH_EXTENSION_RMS_GROWTH_LIMIT)
    abs_limit = max(
        MAX_RECOVERED_ABS_RESIDUAL_MHZ,
        base_abs * BRANCH_EXTENSION_RMS_GROWTH_LIMIT,
        reference_fsr_mhz * BRANCH_EXTENSION_ABS_RESIDUAL_FRACTION,
    )

    accepted: list[tuple[tuple[int, float], list[dict[str, float]], dict[str, object], list[dict[str, float]]]] = []
    for block in candidate_blocks:
        additions = [by_mode[mode][1] for mode in block]
        trial_rows = sorted(
            [dict(row) for row in common_family_rows] + additions,
            key=lambda row: int(row["mode_number_centered"]),
        )
        try:
            trial_fit = fit_family_centered(family, trial_rows, reference_fsr_mhz)
        except (RuntimeError, ValueError, TypeError):
            continue

        quadratic = trial_fit.get("quadratic")
        spacing = trial_fit.get("spacing_quality", {})
        if not quadratic or not bool(spacing.get("spacing_ok", False)):
            continue
        if float(quadratic["rms_residual_mhz"]) > rms_limit:
            continue
        if float(quadratic["max_abs_residual_mhz"]) > abs_limit:
            continue
        accepted.append(((-len(additions), float(quadratic["rms_residual_mhz"])), trial_rows, trial_fit, additions))

    if not accepted:
        return family_rows, None

    _score, trial_rows, trial_fit, additions = sorted(accepted, key=lambda item: item[0])[0]
    quadratic = trial_fit["quadratic"]
    spacing = trial_fit["spacing_quality"]
    return trial_rows, {
        "family": family,
        "added_count": len(additions),
        "added_modes": [int(row["mode_number_centered"]) for row in additions],
        "added_wavelength_nm": [float(row["wavelength_nm_linear"]) for row in additions],
        "added_residual_mhz": [float(row.get("auto_residual_mhz", math.nan)) for row in additions],
        "tolerance_mhz": extension_tolerance_mhz,
        "before_count": len(family_rows),
        "after_count": len(trial_rows),
        "coordinate_system": "common_coordinate_extension",
        "common_d1_mhz": common_d1_mhz,
        "before_quadratic": base_fit.get("quadratic"),
        "after_quadratic": quadratic,
        "spacing_quality": spacing,
    }


def extend_centered_family_branches(
    all_rows: list[dict[str, float]],
    families: dict[str, list[dict[str, float]]],
    *,
    reference_fsr_mhz: float,
    tolerance_mhz: float,
    iterations: int,
) -> tuple[dict[str, list[dict[str, float]]], list[dict[str, object]]]:
    updated = {key: [dict(row) for row in rows] for key, rows in families.items()}
    extension_log: list[dict[str, object]] = []
    fit_candidates = [
        fit_family_centered(name, family_rows, reference_fsr_mhz)
        for name, family_rows in updated.items()
        if family_rows
    ]
    origin = choose_common_origin(updated, fit_candidates)
    if origin is None:
        return updated, extension_log
    _origin_name, origin_row, common_d1_mhz = origin
    common_origin_mhz = float(origin_row["relative_freq_mhz"])
    for _ in range(max(1, iterations)):
        changed = False
        for family in FAMILY_KEYS:
            family_rows = updated.get(family, [])
            if not family_rows:
                continue
            assigned_elsewhere = set().union(
                *(
                    family_sample_indices(rows)
                    for key, rows in updated.items()
                    if key != family
                )
            )
            extended_rows, log_entry = extend_one_centered_family(
                family,
                family_rows,
                all_rows,
                assigned_elsewhere,
                reference_fsr_mhz=reference_fsr_mhz,
                tolerance_mhz=tolerance_mhz,
                common_origin_mhz=common_origin_mhz,
                common_d1_mhz=common_d1_mhz,
            )
            if log_entry is None:
                continue
            updated[family] = extended_rows
            extension_log.append(log_entry)
            changed = True
        if not changed:
            break
    return updated, extension_log


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
    for name in ("raw.npz", "raw.csv"):
        candidate = dip_table.with_name(name)
        if candidate.exists():
            return candidate
    if dip_table.name == "dip_table.csv":
        evidence_dir = dip_table.parent
        if evidence_dir.parent.name == "evidence":
            q_dir = evidence_dir.parent.parent
            for name in ("raw.npz", "raw.csv"):
                candidate = q_dir / name
                if candidate.exists():
                    return candidate
        summary_path = evidence_dir / "process_summary.json"
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                configured_path = Path(str(summary.get("config", {}).get("data_path", "")))
                if configured_path.exists():
                    return configured_path
            except json.JSONDecodeError:
                pass
    return None


def load_unfiltered_dip_rows(path: Path) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            try:
                rows.append(
                    {
                        "sample_index": float(raw["sample_index"]),
                        "relative_freq_mhz": float(raw["relative_freq_mhz"]),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
    return rows


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


def representative_side_panel_rows(
    common_rows: list[dict[str, float]],
    assigned_by_sample: dict[int, tuple[str, dict[str, float]]],
) -> tuple[int, list[dict[str, float]], set[str], set[str]]:
    target_labels = {label for label, _row in assigned_by_sample.values()}
    if not common_rows:
        return 0, [], target_labels, set()
    available_modes = sorted({int(row["common_mode_number"]) for row in common_rows})
    mode_order = [0]
    max_abs_mode = max(abs(mode) for mode in available_modes)
    for index in range(1, max_abs_mode + 1):
        mode_order.extend([index, -index])
    mode_order.extend(mode for mode in available_modes if mode not in mode_order)

    best_mode = mode_order[0]
    best_rows: list[dict[str, float]] = []
    best_labels: set[str] = set()
    for mode in mode_order:
        side_rows = [row for row in common_rows if int(row["common_mode_number"]) == mode]
        labels = {
            assigned_by_sample[int(float(row["sample_index"]))][0]
            for row in side_rows
            if int(float(row["sample_index"])) in assigned_by_sample
        }
        if not best_rows or len(labels) > len(best_labels) or (
            len(labels) == len(best_labels) and abs(mode) < abs(best_mode)
        ):
            best_mode = mode
            best_rows = side_rows
            best_labels = labels
        if target_labels and target_labels.issubset(labels):
            break

    sorted_rows = sorted(best_rows, key=lambda item: float(item["common_folded_mhz"]))
    return best_mode, sorted_rows, target_labels, best_labels


def infer_common_origin_mhz(common_rows: list[dict[str, float]], common_d1_mhz: float) -> float | None:
    origins: list[float] = []
    for row in common_rows:
        try:
            common_unfolded = float(row["common_mode_number"]) * common_d1_mhz + float(row["common_folded_mhz"])
            origins.append(float(row["relative_freq_mhz"]) - common_unfolded)
        except (KeyError, TypeError, ValueError):
            continue
    if not origins:
        return None
    return float(np.median(origins))


def draw_one_fsr_transmission_trace(
    side: plt.Axes,
    *,
    data_path: Path,
    dip_table: Path,
    selected_mode: int,
    common_rows: list[dict[str, float]],
    common_d1_mhz: float,
) -> bool:
    origin_mhz = infer_common_origin_mhz(common_rows, common_d1_mhz)
    if origin_mhz is None:
        return False

    control_rows = load_unfiltered_dip_rows(dip_table)
    if len(control_rows) < 2:
        return False
    control_rows = sorted(control_rows, key=lambda row: row["sample_index"])
    control_samples = np.array([row["sample_index"] for row in control_rows], dtype=float)
    control_rel_freq = np.array([row["relative_freq_mhz"] for row in control_rows], dtype=float)
    unique_mask = np.concatenate([[True], np.diff(control_samples) > 0])
    control_samples = control_samples[unique_mask]
    control_rel_freq = control_rel_freq[unique_mask]
    if len(control_samples) < 2:
        return False

    _time_s, _trigger, trans_raw, _mzi_raw = read_large_scan_data(data_path)
    trans_norm, _baseline = normalize_transmission_with_baseline(trans_raw)
    lo = max(0, int(np.ceil(control_samples.min())))
    hi = min(len(trans_norm) - 1, int(np.floor(control_samples.max())))
    if hi <= lo:
        return False

    step = max(1, (hi - lo) // 80_000)
    sample_grid = np.arange(lo, hi + 1, step)
    rel_freq = np.interp(sample_grid.astype(float), control_samples, control_rel_freq)
    folded = rel_freq - origin_mhz - selected_mode * common_d1_mhz
    mask = np.abs(folded) <= common_d1_mhz / 2.0
    if int(np.count_nonzero(mask)) < 3:
        return False

    side.plot(trans_norm[sample_grid][mask], folded[mask] / 1000.0, color="#333333", lw=1.15, zorder=2)
    return True


def draw_mu0_side_panel(
    side: plt.Axes,
    *,
    dip_table: Path,
    stem: str,
    common_rows: list[dict[str, float]],
    assigned_by_sample: dict[int, tuple[str, dict[str, float]]],
    common_d1_mhz: float,
) -> None:
    selected_mode, side_rows, target_labels, plotted_labels = representative_side_panel_rows(
        common_rows,
        assigned_by_sample,
    )
    data_path = find_large_scan_data_path(dip_table, stem)
    plotted_trace = False
    if data_path is not None and common_rows:
        try:
            plotted_trace = draw_one_fsr_transmission_trace(
                side,
                data_path=data_path,
                dip_table=dip_table,
                selected_mode=selected_mode,
                common_rows=common_rows,
                common_d1_mhz=common_d1_mhz,
            )
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
    side.set_ylabel("Offset within selected FSR (GHz)" if plotted_trace else "")
    title = f"m={selected_mode:+d} one FSR"
    if target_labels and not target_labels.issubset(plotted_labels):
        missing = ",".join(sorted(target_labels - plotted_labels))
        title += f"\nmissing {missing}"
    side.set_title(title, fontsize=14)
    side.set_xlim(1.05, -0.05)
    side.set_ylim(-common_d1_mhz / 2000.0, common_d1_mhz / 2000.0)
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
        f"Common-coordinate dispersion map with representative one-FSR panel; D1={common_d1_mhz/1000:.6g} GHz",
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
    parser.add_argument(
        "--campaign",
        default=default_campaign(),
        help=f"Campaign path under ${DATA_ROOT_ENV}/experiments. Defaults to ${CAMPAIGN_ENV} or wafer_measuement/Batch_260515.",
    )
    parser.add_argument("--chip", default=default_chip(), help=f"Chip/sample id. Defaults to ${CHIP_ENV} or chip7.")
    parser.add_argument("--die", default="die1-1")
    parser.add_argument("--cavity", default="c1")
    parser.add_argument("--depth-threshold", type=float, default=0.4)
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
        result_dir = default_cavity_dir(args.chip, args.die, args.cavity, campaign=args.campaign)
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
    elif (process_fsr_mhz := process_summary_fsr_mhz(dip_table, output_dir, stem)) is not None:
        reference_fsr_mhz = process_fsr_mhz
        reference_fsr_source = "process_summary"
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
    auto_families, outlier_prune_log = prune_auto_centered_family_outliers(
        auto_families,
        reference_fsr_mhz=reference_fsr_mhz,
    )
    auto_families, residual_recovery_log = recover_residual_centered_families(
        rows,
        auto_families,
        reference_fsr_mhz=reference_fsr_mhz,
        tolerance_mhz=args.auto_center_tolerance_mhz,
        iterations=args.auto_center_iterations,
    )
    auto_families, global_gui_recovery_log = recover_global_gui_offset_families(
        rows,
        auto_families,
        reference_fsr_mhz=reference_fsr_mhz,
        tolerance_mhz=args.auto_center_tolerance_mhz,
        iterations=args.auto_center_iterations,
    )
    auto_families, branch_extension_log = extend_centered_family_branches(
        rows,
        auto_families,
        reference_fsr_mhz=reference_fsr_mhz,
        tolerance_mhz=args.auto_center_tolerance_mhz,
        iterations=args.auto_center_iterations,
    )
    auto_fits = [fit_family_centered(name, family_rows, reference_fsr_mhz) for name, family_rows in auto_families.items()]

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
        "outlier_prune_log": outlier_prune_log,
        "residual_recovery_log": residual_recovery_log,
        "global_gui_recovery_log": global_gui_recovery_log,
        "branch_extension_log": branch_extension_log,
    }
    summary_path = output_dir / f"{stem}_dispersion_fit_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
