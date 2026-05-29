#!/usr/bin/env python3
"""Process a large-scan CSV/NPZ using the reusable logic from the old MATLAB GUI.

This ports the useful parts of FittingUtil/dispersion_analyzer.m:
- baseline-normalize CH2 transmission and find resonance dips;
- find alternating CH3 MZI extrema and map each dip to an MZI mu coordinate;
- convert MZI mu to relative frequency and fold by an estimated resonator FSR.
"""

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
from scipy.signal import savgol_filter


SESSION_DIR = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_DIR = SESSION_DIR / "results"


@dataclass
class ProcessConfig:
    data_path: str
    start_nm: float
    center_nm: float
    stop_nm: float
    mzi_d1_mhz: float
    mzi_d2_mhz: float
    mzi_d3_mhz: float
    disk_fsr_mhz: float
    offset_mhz: float
    peak_sensitivity_db: float
    mzi_sensitivity_db: float
    nominal_width_samples: int
    fsr_scan_min_mhz: float
    fsr_scan_max_mhz: float
    fsr_scan_step_mhz: float
    output_dir: str


def odd_window(value: int, n: int) -> int:
    value = max(5, min(value, n - 1 if n % 2 == 0 else n))
    if value % 2 == 0:
        value -= 1
    return max(5, value)


def read_large_scan_csv(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    data = np.genfromtxt(path, delimiter=",", names=True)
    names = data.dtype.names or ()
    ch1_name = next((name for name in names if "ch1" in name.lower()), None)
    ch2_name = next(name for name in names if "ch2" in name.lower())
    ch3_name = next(name for name in names if "ch3" in name.lower())
    ch1 = np.asarray(data[ch1_name], dtype=float) if ch1_name else np.full(len(data), np.nan)
    return (
        np.asarray(data["time_s"], dtype=float),
        ch1,
        np.asarray(data[ch2_name], dtype=float),
        np.asarray(data[ch3_name], dtype=float),
    )


def read_large_scan_npz(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    data = np.load(path)
    keys = set(data.files)
    ch1_name = next((name for name in keys if "ch1" in name.lower()), None)
    ch2_name = next(name for name in keys if "ch2" in name.lower())
    ch3_name = next(name for name in keys if "ch3" in name.lower())
    ch2 = np.asarray(data[ch2_name], dtype=float)
    n = len(ch2)
    if "time_s" in keys:
        time_s = np.asarray(data["time_s"], dtype=float)
    else:
        t0 = float(np.asarray(data["time_start_s"]))
        t1 = float(np.asarray(data["time_stop_s"]))
        time_s = np.linspace(t0, t1, n)
    ch1 = np.asarray(data[ch1_name], dtype=float) if ch1_name else np.full(n, np.nan)
    return (
        time_s,
        ch1,
        ch2,
        np.asarray(data[ch3_name], dtype=float),
    )


def read_large_scan_data(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    if path.suffix.lower() == ".npz":
        return read_large_scan_npz(path)
    return read_large_scan_csv(path)


def alternating_extrema(indices: np.ndarray, values: np.ndarray, signs: np.ndarray) -> np.ndarray:
    extrema = np.column_stack([indices.astype(float), values.astype(float), signs.astype(float)])
    extrema = extrema[np.argsort(extrema[:, 0])]
    if len(extrema) <= 1:
        return extrema
    keep = np.ones(len(extrema), dtype=bool)
    active = 0
    expected_sign = extrema[0, 2]
    for i in range(1, len(extrema)):
        if extrema[i, 2] != expected_sign:
            active = i
            expected_sign *= -1
            continue
        old_is_less_extreme = math.copysign(1.0, extrema[i, 1] - extrema[active, 1]) == expected_sign
        if old_is_less_extreme:
            keep[active] = False
            active = i
        else:
            keep[i] = False
    return extrema[keep]


def find_mzi_index(mzi_raw: np.ndarray, mzi_sensitivity_db: float) -> np.ndarray:
    n = len(mzi_raw)
    mean_mzi = savgol_filter(mzi_raw, odd_window(501, n), 1)
    mzi = savgol_filter(mzi_raw, odd_window(11, n), 2)
    diff = np.diff(mzi)
    dip_idx = np.where((diff[:-1] < 0) & (diff[1:] >= 0))[0] + 1
    peak_idx = np.where((diff[:-1] > 0) & (diff[1:] <= 0))[0] + 1
    # MATLAB uses the slow mean as the threshold for AC-coupled MZI traces.
    dip_idx = dip_idx[mzi[dip_idx] < mean_mzi[dip_idx]]
    peak_idx = peak_idx[mzi[peak_idx] > mean_mzi[peak_idx]]
    extrema = alternating_extrema(
        np.concatenate([dip_idx, peak_idx]),
        np.concatenate([mzi[dip_idx], mzi[peak_idx]]),
        np.concatenate([-np.ones(len(dip_idx)), np.ones(len(peak_idx))]),
    )
    return extrema[:, 0].astype(int)


def normalize_transmission(trans_raw: np.ndarray) -> np.ndarray:
    trans, _baseline = normalize_transmission_with_baseline(trans_raw)
    return trans


def normalize_transmission_with_baseline(trans_raw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n = len(trans_raw)
    baseline = savgol_filter(trans_raw, odd_window(1001, n), 1)
    slope_guard = 10.0 / n
    for i in range(1, n):
        baseline[i] = max(baseline[i], baseline[i - 1] * (1.0 - slope_guard))
    for i in range(n - 2, -1, -1):
        baseline[i] = max(baseline[i], baseline[i + 1] * (1.0 - slope_guard))
    baseline = np.where(np.abs(baseline) < 1e-12, np.nanmedian(trans_raw), baseline)
    trans = trans_raw / baseline
    return savgol_filter(trans, odd_window(51, n), 2), baseline


def find_transmission_dips(
    trans_raw: np.ndarray,
    *,
    peak_sensitivity_db: float,
    nominal_width_samples: int,
    mzi_index: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    trans = normalize_transmission(trans_raw)
    threshold = (np.quantile(trans, 0.99) - np.quantile(trans, 0.50)) / (10.0 ** (peak_sensitivity_db / 10.0))
    threshold = max(float(threshold), 1e-4)
    clipped = trans.copy()
    clipped[clipped > 1.0 - threshold] = 1.0
    diff = np.diff(clipped)
    dip_idx = np.where((diff[:-1] < 0) & (diff[1:] >= 0))[0] + 1
    dip_idx = dip_idx[clipped[dip_idx] < (1.0 - 2.0 * threshold)]
    peak_idx = np.where((diff[:-1] > 0) & (diff[1:] <= 0))[0] + 1
    peak_idx = peak_idx[clipped[peak_idx] < (1.0 - threshold)]
    extrema = np.column_stack(
        [
            np.concatenate([dip_idx, peak_idx]).astype(float),
            np.concatenate([clipped[dip_idx], clipped[peak_idx]]).astype(float),
            np.concatenate([-np.ones(len(dip_idx)), np.ones(len(peak_idx))]),
        ]
    )
    extrema = extrema[np.argsort(extrema[:, 0])]
    extrema = merge_narrow_extrema(clipped, extrema, max(1, int(nominal_width_samples)))
    if len(extrema) == 0:
        return np.array([], dtype=int), np.array([], dtype=float), trans

    good = np.ones(len(extrema), dtype=bool)
    for i in range(1, len(extrema) - 1):
        if extrema[i, 2] == -1:
            left_ok = extrema[i - 1, 2] == 1 and extrema[i - 1, 1] - extrema[i, 1] < threshold
            right_ok = extrema[i + 1, 2] == 1 and extrema[i + 1, 1] - extrema[i, 1] < threshold
            if left_ok or right_ok:
                good[i] = False
    extrema = extrema[good]
    if len(extrema) == 0:
        return np.array([], dtype=int), np.array([], dtype=float), trans

    if len(mzi_index) > 0:
        extrema = extrema[(extrema[:, 0] >= mzi_index[0]) & (extrema[:, 0] <= mzi_index[-1])]
    dips = extrema[extrema[:, 2] == -1]
    return dips[:, 0].astype(int), dips[:, 1].astype(float), trans


def merge_narrow_extrema(trace: np.ndarray, extrema: np.ndarray, width: int) -> np.ndarray:
    if len(extrema) <= 1:
        return extrema
    keep = np.ones(len(extrema), dtype=bool)
    n = len(trace)
    for i in range(len(extrema) - 1):
        if not keep[i]:
            continue
        for j in range(i, len(extrema)):
            if extrema[j, 0] > extrema[i, 0] + width:
                probe = min(n - 1, int(extrema[i, 0]) + width)
                if math.copysign(1.0, trace[probe] - extrema[i, 1]) == extrema[i, 2]:
                    keep[i] = False
                break
            if math.copysign(1.0, extrema[j, 1] - extrema[i, 1]) == extrema[i, 2] and j != i:
                keep[i] = False
                break
    extrema = extrema[keep]
    keep = np.ones(len(extrema), dtype=bool)
    for i in range(len(extrema) - 1, 0, -1):
        if not keep[i]:
            continue
        for j in range(i, -1, -1):
            if extrema[j, 0] < extrema[i, 0] - width:
                probe = max(0, int(extrema[i, 0]) - width)
                if math.copysign(1.0, trace[probe] - extrema[i, 1]) == extrema[i, 2]:
                    keep[i] = False
                break
            if math.copysign(1.0, extrema[j, 1] - extrema[i, 1]) == extrema[i, 2] and j != i:
                keep[i] = False
                break
    return extrema[keep]


def map_dips_to_mzi_mu(dip_idx: np.ndarray, mzi_index: np.ndarray) -> np.ndarray:
    if len(dip_idx) == 0 or len(mzi_index) < 2:
        return np.array([], dtype=float)
    idx = np.concatenate([[dip_idx[0] - 1], mzi_index, [dip_idx[-1] + 1]]).astype(float)
    mu_values = np.empty(len(dip_idx), dtype=float)
    mzi_mu = 1
    for i, dip in enumerate(dip_idx):
        while mzi_mu < len(idx) and dip > idx[mzi_mu]:
            mzi_mu += 1
        left = idx[mzi_mu - 1]
        right = idx[mzi_mu]
        frac = 0.0 if right == left else (right - dip) / (right - left)
        mu_values[i] = (mzi_mu - frac) / 2.0
    return mu_values


def relative_frequency_mhz(
    mzi_mu: np.ndarray,
    *,
    start_nm: float,
    center_nm: float,
    stop_nm: float,
    d1: float,
    d2: float,
    d3: float,
    offset_mhz: float,
) -> tuple[np.ndarray, float]:
    c = 299_792_458.0
    f_start = c / start_nm
    f_center = c / center_nm
    f_stop = c / stop_nm
    mapped = mzi_mu.copy()
    if f_start > f_stop:
        mapped = -mapped
    mu_center = mapped[0] + (f_center - f_start) / (f_stop - f_start) * (mapped[-1] - mapped[0])
    mu0 = mapped - mu_center
    freq = d1 * mu0 + 0.5 * d2 * mu0**2 + (d3 / 6.0) * mu0**3 + offset_mhz
    return freq, float(mu_center)


def find_fsr_candidates(
    rel_freq_mhz: np.ndarray,
    depth: np.ndarray,
    *,
    min_mhz: float,
    max_mhz: float,
    step_mhz: float,
    min_depth: float = 0.15,
    top_n: int = 12,
) -> list[dict[str, float]]:
    mask = depth >= min_depth
    freq = rel_freq_mhz[mask]
    weights = depth[mask]
    if len(freq) < 4:
        return []
    fsr_values = np.arange(min_mhz, max_mhz + step_mhz / 2.0, step_mhz)
    score = np.empty_like(fsr_values)
    weight_sum = np.sum(weights)
    for i, fsr_mhz in enumerate(fsr_values):
        score[i] = abs(np.sum(weights * np.exp(1j * 2.0 * np.pi * freq / fsr_mhz))) / weight_sum
    candidates = []
    order = np.argsort(score)[::-1]
    selected: list[int] = []
    min_separation = max(5, int(round(2_000.0 / step_mhz)))
    for idx in order:
        if all(abs(idx - prev) >= min_separation for prev in selected):
            selected.append(int(idx))
            candidates.append(
                {
                    "fsr_mhz": float(fsr_values[idx]),
                    "fsr_ghz": float(fsr_values[idx] / 1000.0),
                    "score": float(score[idx]),
                }
            )
        if len(candidates) >= top_n:
            break
    return candidates


def write_dip_table(path: Path, rows: list[dict[str, float | int]]) -> None:
    fieldnames = [
        "dip_id",
        "sample_index",
        "time_s",
        "wavelength_nm_linear",
        "norm_transmission",
        "depth_1_minus_norm",
        "mzi_mu",
        "relative_freq_mhz",
        "mode_number",
        "folded_freq_mhz",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def plot_results(
    *,
    stem: str,
    output_dir: Path,
    time_s: np.ndarray,
    trans_raw: np.ndarray,
    mzi_raw: np.ndarray,
    dip_idx: np.ndarray,
    rows: list[dict[str, float | int]],
    disk_fsr_mhz: float,
) -> tuple[Path, Path]:
    decim = max(1, math.ceil(len(time_s) / 20000))
    t_plot = time_s[::decim]
    trans_plot = trans_raw[::decim]
    mzi_plot = mzi_raw[::decim]

    fig, axes = plt.subplots(2, 1, figsize=(15, 8), sharex=True, constrained_layout=True)
    axes[0].plot(t_plot, trans_plot, lw=0.9, color="#2364aa")
    if len(dip_idx):
        pick = dip_idx[:: max(1, len(dip_idx) // 400)]
        axes[0].scatter(time_s[pick], trans_raw[pick], s=18, color="#c43b3b", zorder=3, label="detected dips")
        axes[0].legend(loc="best", fontsize=15)
    axes[0].set_ylabel("CH2 trans (V)", fontsize=18)
    axes[0].set_title(f"{stem}: raw CH2/CH3 decimated every {decim} samples", fontsize=20)
    axes[1].plot(t_plot, mzi_plot, lw=0.75, color="#55a868")
    axes[1].set_xlabel("Time relative to CH1 trigger (s)", fontsize=18)
    axes[1].set_ylabel("CH3 MZI (V)", fontsize=18)
    for ax in axes:
        ax.tick_params(axis="both", labelsize=15)
    raw_fig = output_dir / f"{stem}_ch2_ch3_raw.png"
    fig.savefig(raw_fig, dpi=240)
    plt.close(fig)

    folded_fig = output_dir / f"{stem}_folded_dispersion.png"
    if rows:
        mode_number = np.array([float(row["mode_number"]) for row in rows])
        folded = np.array([float(row["folded_freq_mhz"]) for row in rows])
        depth = np.array([float(row["depth_1_minus_norm"]) for row in rows])
        fig, ax = plt.subplots(figsize=(12, 5.6), constrained_layout=True)
        sc = ax.scatter(mode_number, folded, c=depth, s=12, cmap="viridis", linewidths=0)
        ax.axhline(0, color="black", lw=0.8)
        ax.axhline(disk_fsr_mhz / 2, color="#999999", lw=0.6, ls="--")
        ax.axhline(-disk_fsr_mhz / 2, color="#999999", lw=0.6, ls="--")
        ax.set_xlabel("Mode number from folded FSR")
        ax.set_ylabel("Folded frequency (MHz)")
        ax.set_title(f"Folded resonance map, disk FSR = {disk_fsr_mhz / 1000:.6g} GHz")
        cb = fig.colorbar(sc, ax=ax)
        cb.set_label("Depth, 1 - normalized transmission")
        fig.savefig(folded_fig, dpi=180)
        plt.close(fig)
    else:
        fig, ax = plt.subplots(figsize=(8, 4), constrained_layout=True)
        ax.text(0.5, 0.5, "No dips detected", ha="center", va="center")
        ax.axis("off")
        fig.savefig(folded_fig, dpi=180)
        plt.close(fig)
    return raw_fig, folded_fig


def plot_normalized_transmission(
    *,
    stem: str,
    output_dir: Path,
    time_s: np.ndarray,
    trans_raw: np.ndarray,
    dip_idx: np.ndarray,
) -> Path:
    trans_norm, baseline = normalize_transmission_with_baseline(trans_raw)
    decim = max(1, math.ceil(len(time_s) / 25000))
    t_plot = time_s[::decim]
    raw_plot = trans_raw[::decim]
    baseline_plot = baseline[::decim]
    norm_plot = trans_norm[::decim]

    fig, axes = plt.subplots(2, 1, figsize=(12, 6.6), sharex=True, constrained_layout=True)
    axes[0].plot(t_plot, raw_plot, color="#2364aa", lw=0.55, label="CH2 raw")
    axes[0].plot(t_plot, baseline_plot, color="#c43b3b", lw=1.0, alpha=0.9, label="estimated baseline")
    axes[0].set_ylabel("CH2 trans (V)")
    axes[0].legend(loc="best")
    axes[0].set_title("Baseline flattening used for dip detection")

    axes[1].plot(t_plot, norm_plot, color="#2b8c5a", lw=0.6, label="normalized + smoothed CH2")
    if len(dip_idx):
        pick = dip_idx[:: max(1, len(dip_idx) // 500)]
        axes[1].scatter(time_s[pick], trans_norm[pick], s=8, color="#c43b3b", zorder=3, label="detected dips")
    axes[1].axhline(1.0, color="#777777", lw=0.8, ls="--")
    axes[1].set_xlabel("Time relative to CH1 trigger (s)")
    axes[1].set_ylabel("Normalized CH2")
    axes[1].legend(loc="best")

    path = output_dir / f"{stem}_ch2_flattened_for_dips.png"
    fig.savefig(path, dpi=180)
    plt.close(fig)
    return path


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_path", nargs="?", default=None)
    parser.add_argument("--chip", default="chip7")
    parser.add_argument("--die", default="die1-1")
    parser.add_argument("--cavity", default="c1")
    parser.add_argument("--start-nm", type=float, default=1530.0)
    parser.add_argument("--center-nm", type=float, default=1550.0)
    parser.add_argument("--stop-nm", type=float, default=1570.0)
    parser.add_argument("--mzi-d1-mhz", type=float, default=40.1228)
    parser.add_argument("--mzi-d2-mhz", type=float, default=4e-8)
    parser.add_argument("--mzi-d3-mhz", type=float, default=-3e-14)
    parser.add_argument("--disk-fsr-mhz", type=float, default=204_900.0)
    parser.add_argument("--offset-mhz", type=float, default=0.0)
    parser.add_argument("--peak-sensitivity-db", type=float, default=0.0)
    parser.add_argument("--mzi-sensitivity-db", type=float, default=0.0)
    parser.add_argument("--nominal-width-samples", type=int, default=50)
    parser.add_argument("--fsr-scan-min-mhz", type=float, default=50_000.0)
    parser.add_argument("--fsr-scan-max-mhz", type=float, default=300_000.0)
    parser.add_argument("--fsr-scan-step-mhz", type=float, default=10.0)
    parser.add_argument("--output-dir", default=None)
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    if args.data_path:
        data_path = Path(args.data_path)
    else:
        result_dir = DEFAULT_RESULTS_DIR / args.chip / args.die / args.cavity
        candidates = list(result_dir.glob("large_scan_*_1530-1570nm.npz")) + list(result_dir.glob("large_scan_*_1530-1570nm.csv"))
        data_path = max(candidates, key=lambda p: p.stat().st_mtime)
    output_dir = Path(args.output_dir) if args.output_dir else data_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = data_path.stem

    config = ProcessConfig(
        data_path=str(data_path),
        start_nm=args.start_nm,
        center_nm=args.center_nm,
        stop_nm=args.stop_nm,
        mzi_d1_mhz=args.mzi_d1_mhz,
        mzi_d2_mhz=args.mzi_d2_mhz,
        mzi_d3_mhz=args.mzi_d3_mhz,
        disk_fsr_mhz=args.disk_fsr_mhz,
        offset_mhz=args.offset_mhz,
        peak_sensitivity_db=args.peak_sensitivity_db,
        mzi_sensitivity_db=args.mzi_sensitivity_db,
        nominal_width_samples=args.nominal_width_samples,
        fsr_scan_min_mhz=args.fsr_scan_min_mhz,
        fsr_scan_max_mhz=args.fsr_scan_max_mhz,
        fsr_scan_step_mhz=args.fsr_scan_step_mhz,
        output_dir=str(output_dir),
    )

    print(json.dumps(asdict(config), indent=2, ensure_ascii=False))
    time_s, _trigger, trans_raw, mzi_raw = read_large_scan_data(data_path)
    mzi_index = find_mzi_index(mzi_raw, args.mzi_sensitivity_db)
    dip_idx, dip_norm, _trans_norm = find_transmission_dips(
        trans_raw,
        peak_sensitivity_db=args.peak_sensitivity_db,
        nominal_width_samples=args.nominal_width_samples,
        mzi_index=mzi_index,
    )
    mzi_mu = map_dips_to_mzi_mu(dip_idx, mzi_index)
    rows: list[dict[str, float | int]] = []
    if len(dip_idx) and len(mzi_mu):
        rel_freq, mu_center = relative_frequency_mhz(
            mzi_mu,
            start_nm=args.start_nm,
            center_nm=args.center_nm,
            stop_nm=args.stop_nm,
            d1=args.mzi_d1_mhz,
            d2=args.mzi_d2_mhz,
            d3=args.mzi_d3_mhz,
            offset_mhz=args.offset_mhz,
        )
        mode_number = np.rint(rel_freq / args.disk_fsr_mhz).astype(int)
        folded = rel_freq - mode_number * args.disk_fsr_mhz
        depth = 1.0 - dip_norm
        wavelength = args.center_nm + (args.stop_nm - args.start_nm) / 20.0 * time_s[dip_idx]
        for i, idx in enumerate(dip_idx):
            rows.append(
                {
                    "dip_id": i + 1,
                    "sample_index": int(idx),
                    "time_s": float(time_s[idx]),
                    "wavelength_nm_linear": float(wavelength[i]),
                    "norm_transmission": float(dip_norm[i]),
                    "depth_1_minus_norm": float(depth[i]),
                    "mzi_mu": float(mzi_mu[i]),
                    "relative_freq_mhz": float(rel_freq[i]),
                    "mode_number": int(mode_number[i]),
                    "folded_freq_mhz": float(folded[i]),
                }
            )
        fsr_candidates = find_fsr_candidates(
            rel_freq,
            depth,
            min_mhz=args.fsr_scan_min_mhz,
            max_mhz=args.fsr_scan_max_mhz,
            step_mhz=args.fsr_scan_step_mhz,
        )
    else:
        mu_center = float("nan")
        fsr_candidates = []

    table_path = output_dir / f"{stem}_dip_table.csv"
    write_dip_table(table_path, rows)
    raw_fig, folded_fig = plot_results(
        stem=stem,
        output_dir=output_dir,
        time_s=time_s,
        trans_raw=trans_raw,
        mzi_raw=mzi_raw,
        dip_idx=dip_idx,
        rows=rows,
        disk_fsr_mhz=args.disk_fsr_mhz,
    )
    flattened_fig = plot_normalized_transmission(
        stem=stem,
        output_dir=output_dir,
        time_s=time_s,
        trans_raw=trans_raw,
        dip_idx=dip_idx,
    )
    summary = {
        "config": asdict(config),
        "rows": int(len(time_s)),
        "mzi_extrema_count": int(len(mzi_index)),
        "dip_count": int(len(rows)),
        "mu_center": mu_center,
        "fsr_candidates": fsr_candidates,
        "dip_table": str(table_path),
        "raw_ch2_ch3_figure": str(raw_fig),
        "flattened_ch2_figure": str(flattened_fig),
        "folded_dispersion_figure": str(folded_fig),
    }
    summary_path = output_dir / f"{stem}_process_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
