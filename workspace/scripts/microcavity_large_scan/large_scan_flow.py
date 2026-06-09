"""Run the onsite large-scan flow and standardize the Q folder."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from data_paths import CAMPAIGN_ENV, CHIP_ENV, DATA_ROOT_ENV, default_campaign, default_chip, default_results_dir


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]


def run_command(args: list[str]) -> float:
    print("RUN", " ".join(args), flush=True)
    started_at = time.perf_counter()
    subprocess.run(args, cwd=REPO_ROOT, check=True)
    return time.perf_counter() - started_at


def start_command(args: list[str]) -> subprocess.Popen:
    print("RUN", " ".join(args), flush=True)
    return subprocess.Popen(args, cwd=REPO_ROOT)


def latest_new_scan(q_dir: Path, started_at: float) -> tuple[Path, Path, str]:
    candidates = sorted(
        q_dir.glob("large_scan_*_1530-1570nm.npz"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for npz_path in candidates:
        if npz_path.stat().st_mtime + 1 < started_at:
            continue
        meta_path = npz_path.with_suffix(".json")
        if meta_path.exists():
            return npz_path, meta_path, npz_path.stem
    raise RuntimeError(f"No new timestamped large-scan npz/json found in {q_dir}")


def latest_existing_scan(q_dir: Path) -> tuple[Path, Path, str]:
    candidates = sorted(
        q_dir.glob("large_scan_*_1530-1570nm.npz"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for npz_path in candidates:
        meta_path = npz_path.with_suffix(".json")
        if meta_path.exists():
            return npz_path, meta_path, npz_path.stem

    stable_npz = q_dir / "raw.npz"
    stable_meta = q_dir / "acquisition.json"
    if stable_npz.exists() and stable_meta.exists():
        return stable_npz, stable_meta, stable_npz.stem
    raise RuntimeError(f"No existing large-scan raw npz/json found in {q_dir}")


def wait_for_new_scan_ready(
    q_dir: Path,
    started_at: float,
    acquire_proc: subprocess.Popen,
    *,
    timeout_s: float = 140.0,
) -> tuple[Path, Path, str, float]:
    wait_started_at = time.perf_counter()
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            npz_path, meta_path, stem = latest_new_scan(q_dir, started_at)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if int(meta.get("rows", 0)) > 0 and npz_path.exists():
                raw_ready_seconds = time.perf_counter() - wait_started_at
                print(f"RAW_READY {npz_path} after {raw_ready_seconds:.3f} s", flush=True)
                return npz_path, meta_path, stem, raw_ready_seconds
        except (RuntimeError, json.JSONDecodeError, OSError) as exc:
            last_error = exc

        return_code = acquire_proc.poll()
        if return_code is not None:
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, acquire_proc.args)
            if last_error is not None:
                raise RuntimeError(f"Acquisition ended before raw data became ready: {last_error}") from last_error
            raise RuntimeError("Acquisition ended before raw data became ready.")
        time.sleep(0.25)

    raise TimeoutError(f"Timed out waiting for new raw npz/json in {q_dir}")


def assert_acquisition_gates(npz_path: Path, meta_path: Path) -> dict[str, object]:
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if not npz_path.exists():
        raise RuntimeError(f"Missing new raw npz: {npz_path}")
    if int(meta.get("rows", 0)) <= 0:
        raise RuntimeError("Acquisition metadata reports zero rows.")
    if not meta.get("trigger_window_ok"):
        raise RuntimeError("Trigger window gate failed.")
    if not meta.get("pc_voltage_ok", False):
        raise RuntimeError("PC piezo gate failed.")
    if meta.get("emission_cycle_off_seconds") is None or meta.get("emission_post_cycle_off_seconds") is None:
        raise RuntimeError("Emission cycle gate failed.")
    if meta.get("emission_post_cycle_order") != "after_fine_scan_restore":
        raise RuntimeError("Post-scan emission cycle did not run after fine-scan state restoration.")
    if not meta.get("fine_scan_arc_factor_restored") or not meta.get("scope_idle_restored"):
        raise RuntimeError("Fine-scan restore gate failed before post-scan emission cycle.")

    target = meta.get("laser_restore_target_nm")
    readback = meta.get("laser_restore_readback_nm")
    if target is None or readback is None or abs(float(readback) - float(target)) > 0.01:
        raise RuntimeError(f"Laser restore gate failed: target={target}, readback={readback}")

    with np.load(npz_path) as arrays:
        for name in arrays.files:
            if not (name.endswith("_trans_v") or name.endswith("_mzi_v")):
                continue
            values = np.asarray(arrays[name], dtype=float)
            mn = float(np.nanmin(values))
            mx = float(np.nanmax(values))
            tol = max(1e-9, (mx - mn) * 1e-4)
            low_frac = float(np.mean(values <= mn + tol))
            high_frac = float(np.mean(values >= mx - tol))
            if low_frac > 0.01 or high_frac > 0.01:
                raise RuntimeError(
                    f"Saturation gate failed for {name}: low_frac={low_frac:.4g}, high_frac={high_frac:.4g}"
                )
    return meta


def move_file(src: Path, dst: Path) -> None:
    if not src.exists():
        raise RuntimeError(f"Missing expected file: {src}")
    if src.resolve() == dst.resolve():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    shutil.move(str(src), str(dst))


def move_file_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    if src.resolve() == dst.resolve():
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    shutil.move(str(src), str(dst))
    return True


def latest_evidence_dir(q_dir: Path) -> Path | None:
    evidence_root = q_dir / "evidence"
    if not evidence_root.exists():
        return None
    dirs = [path for path in evidence_root.glob("processing_*") if path.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda path: path.stat().st_mtime)


def update_summary_paths(q_dir: Path, evidence_dir: Path) -> None:
    replacements = {
        "process_summary.json": {
            "dip_table": str(evidence_dir / "dip_table.csv"),
            "raw_ch2_ch3_figure": str(evidence_dir / "raw_health.png"),
        },
        "dispersion_summary.json": {
            "family_points_csv": str(q_dir / "family_points.csv"),
            "auto_centered_family_points_csv": str(q_dir / "family_points.csv"),
            "common_coordinate_fit_figure": str(q_dir / "dispersion.png"),
            "auto_centered_fit_figure": str(q_dir / "d2_fit.png"),
        },
        "q_summary.json": {
            "q_table": str(q_dir / "q_by_mode.csv"),
            "trend_figure": str(q_dir / "q_trend.png"),
            "fit_examples_figure": str(evidence_dir / "q_fit_examples.png"),
            "local_dip_mosaic_figure": str(q_dir / "mode_spectra.png"),
        },
    }
    for name, patch in replacements.items():
        path = evidence_dir / name
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(patch)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def family_labels(q_dir: Path) -> dict[str, str]:
    path = q_dir / "family_points.csv"
    if not path.exists():
        return {}
    labels: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            family = row.get("family", "")
            label = row.get("family_label", "")
            if family and label:
                labels[family] = label
    return labels


def build_onsite_verdict(
    q_dir: Path,
    evidence_dir: Path,
    *,
    phase_seconds: dict[str, float],
) -> dict[str, object]:
    process_summary = read_json(evidence_dir / "process_summary.json")
    dispersion_summary = read_json(evidence_dir / "dispersion_summary.json")
    q_summary = read_json(evidence_dir / "q_summary.json")
    labels = family_labels(q_dir)

    family_rows: list[dict[str, object]] = []
    for fit in dispersion_summary.get("auto_centered_fits", []):
        if not isinstance(fit, dict) or "quadratic" not in fit:
            continue
        family = str(fit["name"])
        quadratic = fit["quadratic"]
        if not isinstance(quadratic, dict):
            continue
        spacing = fit.get("spacing_quality", {})
        if not isinstance(spacing, dict):
            spacing = {}
        q_family = {}
        family_summary = q_summary.get("family_summary", {})
        if isinstance(family_summary, dict):
            q_family = family_summary.get(family, {}) or {}
        family_rows.append(
            {
                "family": family,
                "label": labels.get(family, family),
                "count": int(fit.get("count", 0)),
                "fsr_ghz": round(float(quadratic["effective_d1_mhz"]) / 1000.0, 6),
                "d2_mhz": round(float(quadratic["d2_mhz_per_mode2"]), 3),
                "rms_mhz": round(float(quadratic["rms_residual_mhz"]), 3),
                "spacing_ok": bool(spacing.get("spacing_ok", False)),
                "spacing_rms_mhz": round(float(spacing.get("rms_spacing_error_mhz", float("nan"))), 3),
                "spacing_max_error_mhz": round(float(spacing.get("max_abs_spacing_error_mhz", float("nan"))), 3),
                "max_mode_gap": int(spacing.get("max_mode_gap", 0)),
                "q0_median_M": round(float(q_family.get("Q0_median_M", float("nan"))), 3),
                "q1_median_M": round(float(q_family.get("Q1_median_M", float("nan"))), 3),
            }
        )

    prune_log = dispersion_summary.get("outlier_prune_log", {})
    prune_count = 0
    if isinstance(prune_log, dict):
        prune_count = sum(len(items) for items in prune_log.values() if isinstance(items, list))
    mode_count = int(q_summary.get("mode_count", 0))
    ok_count = int(q_summary.get("ok_count", 0))
    fit_ratio = ok_count / mode_count if mode_count else 0.0
    depth_filtered = int(dispersion_summary.get("depth_filtered_dip_count", 0))
    family_counts = [int(row["count"]) for row in family_rows]
    rms_values = [float(row["rms_mhz"]) for row in family_rows]
    spacing_bad = [row for row in family_rows if not bool(row["spacing_ok"])]
    spacing_error_values = [
        float(row["spacing_max_error_mhz"]) for row in family_rows if np.isfinite(float(row["spacing_max_error_mhz"]))
    ]
    q0_values = [float(row["q0_median_M"]) for row in family_rows if np.isfinite(float(row["q0_median_M"]))]

    flags: list[str] = []
    if spacing_bad:
        labels = ",".join(str(row["label"]) for row in spacing_bad)
        flags.append(f"family_spacing_bad:{labels}")
    config = dispersion_summary.get("config", {})
    reference_fsr_mhz = float(config.get("reference_fsr_mhz", 0.0)) if isinstance(config, dict) else 0.0
    if reference_fsr_mhz > 0 and spacing_error_values and max(spacing_error_values) > 0.08 * reference_fsr_mhz:
        flags.append(f"family_spacing_error_high:max={max(spacing_error_values):.1f}MHz")
    if fit_ratio < 0.9:
        flags.append(f"q_fit_ratio_low:{ok_count}/{mode_count}")
    if len(family_rows) < 3:
        flags.append(f"family_count_low:{len(family_rows)}")
    if depth_filtered < 60:
        flags.append(f"depth_filtered_sparse:{depth_filtered}")
    if family_counts and min(family_counts) < 12:
        flags.append(f"family_points_sparse:min={min(family_counts)}")
    if rms_values and max(rms_values) > 80.0:
        flags.append(f"d2_rms_high:max={max(rms_values):.1f}MHz")
    if prune_count:
        flags.append(f"residual_pruned:{prune_count}")
    if q0_values and max(q0_values) < 0.8:
        flags.append(f"q0_low:max={max(q0_values):.3f}M")

    if any(
        flag.startswith(("family_spacing_bad", "family_spacing_error_high", "d2_rms_high", "family_count_low"))
        for flag in flags
    ):
        verdict = "escalate"
    elif flags:
        verdict = "limited"
    else:
        verdict = "accepted"

    analysis_seconds = sum(float(phase_seconds.get(key, 0.0)) for key in ("process", "dispersion", "q_fit"))
    raw_ready_seconds = float(phase_seconds.get("acquire_to_raw_ready", phase_seconds.get("acquire", 0.0)))
    return {
        "verdict": verdict,
        "flags": flags,
        "dip_count": process_summary.get("dip_count"),
        "depth_filtered_dip_count": depth_filtered,
        "q_fit": {"ok": ok_count, "total": mode_count, "ratio": round(fit_ratio, 3)},
        "families": sorted(family_rows, key=lambda row: str(row["label"])),
        "phase_seconds": {key: round(value, 3) for key, value in phase_seconds.items()},
        "analysis_seconds": round(analysis_seconds, 3),
        "time_to_analysis_ready_seconds": round(raw_ready_seconds + analysis_seconds, 3),
    }


def threshold_suffix(value: float) -> str:
    return f"{value:g}".replace(".", "p")


def required_stable_outputs(q_dir: Path) -> list[Path]:
    return [
        q_dir / "raw.npz",
        q_dir / "acquisition.json",
        q_dir / "dispersion.png",
        q_dir / "d2_fit.png",
        q_dir / "family_points.csv",
        q_dir / "q_by_mode.csv",
        q_dir / "q_trend.png",
    ]


def required_evidence_outputs(evidence_dir: Path) -> list[Path]:
    return [
        evidence_dir / "dip_table.csv",
        evidence_dir / "process_summary.json",
        evidence_dir / "dispersion_summary.json",
        evidence_dir / "q_summary.json",
        evidence_dir / "raw_health.png",
    ]


def standardized_outputs_ready(q_dir: Path, evidence_dir: Path | None) -> bool:
    return (
        evidence_dir is not None
        and all(path.exists() for path in required_stable_outputs(q_dir))
        and all(path.exists() for path in required_evidence_outputs(evidence_dir))
    )


def evidence_dir_for_stem(q_dir: Path, stem: str) -> Path:
    parts = stem.split("_")
    if len(parts) >= 4 and parts[0] == "large" and parts[1] == "scan":
        return q_dir / "evidence" / f"processing_{parts[2]}_{parts[3]}"
    return q_dir / "evidence" / f"processing_resume_{time.strftime('%Y%m%d_%H%M%S')}"


def standardize_outputs(q_dir: Path, stem: str, *, depth_threshold: float) -> Path:
    existing_evidence = latest_evidence_dir(q_dir)
    unstandardized_outputs_exist = any(path.is_file() for path in q_dir.glob(f"{stem}_*"))
    if (q_dir / f"{stem}.npz").exists() and stem != "raw":
        unstandardized_outputs_exist = True
    if standardized_outputs_ready(q_dir, existing_evidence) and not unstandardized_outputs_exist:
        update_summary_paths(q_dir, existing_evidence)
        return existing_evidence

    evidence_dir = evidence_dir_for_stem(q_dir, stem)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    depth_suffix = threshold_suffix(depth_threshold)
    stable = {
        f"{stem}.npz": "raw.npz",
        f"{stem}.json": "acquisition.json",
        f"{stem}_dispersion_common_with_mu0_panel_depth_gt_{depth_suffix}.png": "dispersion.png",
        f"{stem}_dispersion_auto_centered_depth_gt_{depth_suffix}.png": "d2_fit.png",
        f"{stem}_dispersion_auto_centered_family_points.csv": "family_points.csv",
        f"{stem}_large_scan_q_by_family.csv": "q_by_mode.csv",
        f"{stem}_large_scan_q_trends.png": "q_trend.png",
    }
    optional_stable = {
        f"{stem}_local_dip_mosaic.png": "mode_spectra.png",
    }
    evidence = {
        f"{stem}_dip_table.csv": "dip_table.csv",
        f"{stem}_process_summary.json": "process_summary.json",
        f"{stem}_dispersion_fit_summary.json": "dispersion_summary.json",
        f"{stem}_large_scan_q_summary.json": "q_summary.json",
        f"{stem}_ch2_ch3_raw.png": "raw_health.png",
    }
    optional_evidence = {
        f"{stem}_large_scan_q_fit_examples.png": "q_fit_examples.png",
    }
    for src_name, dst_name in stable.items():
        src = q_dir / src_name
        dst = q_dir / dst_name
        if src.exists():
            move_file(src, dst)
        elif not dst.exists():
            raise RuntimeError(f"Missing expected file: {src}")
    for src_name, dst_name in optional_stable.items():
        move_file_if_exists(q_dir / src_name, q_dir / dst_name)
    for src_name, dst_name in evidence.items():
        src = q_dir / src_name
        dst = evidence_dir / dst_name
        if src.exists():
            move_file(src, dst)
        elif not dst.exists():
            raise RuntimeError(f"Missing expected file: {src}")
    for src_name, dst_name in optional_evidence.items():
        move_file_if_exists(q_dir / src_name, evidence_dir / dst_name)

    for path in q_dir.glob(f"{stem}*"):
        if path.is_file():
            path.unlink()
    update_summary_paths(q_dir, evidence_dir)
    return evidence_dir


def nominal_width_samples_for_raw(npz_path: Path, meta_path: Path, override: int | None) -> int:
    if override is not None:
        return override
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    sample_rate = float(meta.get("actual_sample_rate_hz", meta.get("sample_rate_hz", 200_000.0)))
    return max(20, round(500 * sample_rate / 500_000.0))


def run_analysis_chain(
    args: argparse.Namespace,
    q_dir: Path,
    npz_path: Path,
    meta_path: Path,
    stem: str,
    *,
    process_extra_args: list[str],
    dispersion_extra_args: list[str],
) -> dict[str, float]:
    phase_seconds: dict[str, float] = {}
    nominal_width = nominal_width_samples_for_raw(npz_path, meta_path, args.nominal_width_samples)
    phase_seconds["process"] = run_command(
        [
            sys.executable,
            str(SCRIPT_DIR / "process_large_scan.py"),
            str(npz_path),
            "--campaign",
            args.campaign,
            "--chip",
            args.chip,
            "--die",
            args.die,
            "--cavity",
            args.cavity,
            "--nominal-width-samples",
            str(nominal_width),
            *process_extra_args,
        ]
    )
    dip_table = q_dir / f"{stem}_dip_table.csv"
    phase_seconds["dispersion"] = run_command(
        [
            sys.executable,
            str(SCRIPT_DIR / "fit_large_scan_dispersion.py"),
            str(dip_table),
            "--campaign",
            args.campaign,
            "--chip",
            args.chip,
            "--die",
            args.die,
            "--cavity",
            args.cavity,
            "--depth-threshold",
            f"{args.depth_threshold:g}",
            *dispersion_extra_args,
        ]
    )
    family_points = q_dir / f"{stem}_dispersion_auto_centered_family_points.csv"
    phase_seconds["q_fit"] = run_command(
        [
            sys.executable,
            str(SCRIPT_DIR / "fit_large_scan_q.py"),
            "--data-path",
            str(npz_path),
            "--family-points-csv",
            str(family_points),
            "--campaign",
            args.campaign,
            "--chip",
            args.chip,
            "--die",
            args.die,
            "--cavity",
            args.cavity,
            "--depth-threshold",
            f"{args.depth_threshold:g}",
        ]
    )
    return phase_seconds


def refresh_cavity_card(
    args: argparse.Namespace,
    results_root: Path,
    *,
    card_extra_args: list[str],
) -> float:
    return run_command(
        [
            sys.executable,
            str(SCRIPT_DIR / "write_cavity_card.py"),
            "--chip",
            args.chip,
            "--die",
            args.die,
            "--cavity",
            args.cavity,
            "--results-root",
            str(results_root),
            *card_extra_args,
        ]
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run acquisition, analysis, Q fitting, and card generation for one cavity.")
    parser.add_argument("--chip", default=default_chip(), help=f"Chip/sample id. Defaults to ${CHIP_ENV} or chip7.")
    parser.add_argument(
        "--campaign",
        default=default_campaign(),
        help=f"Campaign path under ${DATA_ROOT_ENV}/experiments. Defaults to ${CAMPAIGN_ENV} or wafer_measuement/Batch_260515.",
    )
    parser.add_argument("--die", required=True)
    parser.add_argument("--cavity", required=True)
    parser.add_argument(
        "--results-root",
        type=Path,
        default=None,
        help=f"Root containing <die>/<cavity>/Q. Defaults to ${DATA_ROOT_ENV}/experiments/<campaign>/results/<chip>.",
    )
    parser.add_argument(
        "--disk-fsr-mhz",
        type=float,
        default=None,
        help="Required for non-chip7 data unless process_large_scan.py can infer this chip's design FSR.",
    )
    parser.add_argument("--radius-um", type=float, default=None, help="Optional cavity radius for non-chip7 cards.")
    parser.add_argument("--gap-um", type=float, default=None, help="Optional coupling gap for non-chip7 cards.")
    parser.add_argument("--sample-rate-hz", type=float, default=200_000.0)
    parser.add_argument("--depth-threshold", type=float, default=0.4)
    parser.add_argument("--storage-format", choices=["npz", "npz-compressed"], default="npz")
    parser.add_argument("--nominal-width-samples", type=int, default=None)
    parser.add_argument(
        "--resume-existing-raw",
        action="store_true",
        help="Skip acquisition and rerun process/dispersion/Q fit from the latest raw npz/json in this cavity's Q folder.",
    )
    parser.add_argument(
        "--standardize-only",
        action="store_true",
        help="Skip acquisition and analysis; only standardize existing outputs, update summary paths, and refresh the card.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned paths and commands without connecting to instruments.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.resume_existing_raw and args.standardize_only:
        raise SystemExit("Use only one of --resume-existing-raw or --standardize-only.")
    try:
        results_root = args.results_root if args.results_root is not None else default_results_dir(args.chip, campaign=args.campaign)
    except RuntimeError as exc:
        if not args.dry_run:
            raise SystemExit(f"{exc} For this wrapper, pass --results-root to override.") from exc
        results_root = Path(f"<set {DATA_ROOT_ENV} or pass --results-root>") / "experiments" / args.campaign / "results" / args.chip
    if args.chip.lower() != "chip7" and args.disk_fsr_mhz is None:
        raise SystemExit("Pass --disk-fsr-mhz for non-chip7 data; this wrapper has no safe design-FSR default.")
    q_dir = results_root / args.die / args.cavity / "Q"

    phase_seconds: dict[str, float] = {}
    started_at = time.time()
    acquire_args = [
        sys.executable,
        str(SCRIPT_DIR / "acquire_large_scan.py"),
        "--campaign",
        args.campaign,
        "--chip",
        args.chip,
        "--die",
        args.die,
        "--cavity",
        args.cavity,
        "--start-nm",
        "1530",
        "--stop-nm",
        "1570",
        "--speed-nm-s",
        "2",
        "--sample-rate-hz",
        f"{args.sample_rate_hz:g}",
        "--record-seconds",
        "20",
        "--storage-format",
        args.storage_format,
        "--restore-wavelength-mode",
        "initial",
        "--cycle-emission-before-scan",
        "--cycle-emission-after-scan",
        "--emission-off-seconds",
        "2",
        "--emission-on-settle-seconds",
        "2",
        "--output-dir",
        str(q_dir),
    ]
    process_extra_args = ["--disk-fsr-mhz", f"{args.disk_fsr_mhz:g}"] if args.disk_fsr_mhz is not None else []
    dispersion_extra_args = (
        ["--reference-fsr-mhz", f"{args.disk_fsr_mhz:g}"] if args.disk_fsr_mhz is not None else []
    )
    card_extra_args = []
    if args.radius_um is not None:
        card_extra_args.extend(["--radius-um", f"{args.radius_um:g}"])
    if args.gap_um is not None:
        card_extra_args.extend(["--gap-um", f"{args.gap_um:g}"])

    if args.dry_run:
        if args.standardize_only:
            planned_commands = {
                "standardize": "standardize existing outputs and refresh cavity card only",
                "card_extra_args": card_extra_args,
            }
        elif args.resume_existing_raw:
            planned_commands = {
                "resume": "reuse latest existing raw npz/json; run process, dispersion, Q fit, standardization, and card refresh",
                "process_extra_args": process_extra_args,
                "dispersion_extra_args": dispersion_extra_args,
                "card_extra_args": card_extra_args,
            }
        else:
            planned_commands = {
                "acquire": acquire_args,
                "process_extra_args": process_extra_args,
                "dispersion_extra_args": dispersion_extra_args,
                "card_extra_args": card_extra_args,
            }
        plan = {
            "dry_run": True,
            "mode": "standardize-only"
            if args.standardize_only
            else "resume-existing-raw"
            if args.resume_existing_raw
            else "acquire-and-analyze",
            "campaign": args.campaign,
            "chip": args.chip,
            "die": args.die,
            "cavity": args.cavity,
            "results_root": str(results_root),
            "q_dir": str(q_dir),
            "requires_instrument_connection": False,
            "disk_fsr_mhz": args.disk_fsr_mhz,
            "commands": planned_commands,
        }
        print(json.dumps(plan, indent=2, ensure_ascii=False), flush=True)
        return 0

    q_dir.mkdir(parents=True, exist_ok=True)

    if args.standardize_only:
        npz_path, meta_path, stem = latest_existing_scan(q_dir)
        meta = assert_acquisition_gates(npz_path, meta_path)
        evidence_dir = standardize_outputs(q_dir, stem, depth_threshold=args.depth_threshold)
        verdict = build_onsite_verdict(q_dir, evidence_dir, phase_seconds={"standardize_only": 0.0})
        refresh_cavity_card(args, results_root, card_extra_args=card_extra_args)
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "standardize-only",
                    "onsite_verdict": verdict,
                    "q_dir": str(q_dir),
                    "evidence_dir": str(evidence_dir),
                    "stem": stem,
                    "rows": meta.get("rows"),
                    "sample_rate_hz": meta.get("actual_sample_rate_hz"),
                },
                indent=2,
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0

    if args.resume_existing_raw:
        npz_path, meta_path, stem = latest_existing_scan(q_dir)
        meta = assert_acquisition_gates(npz_path, meta_path)
        phase_seconds = run_analysis_chain(
            args,
            q_dir,
            npz_path,
            meta_path,
            stem,
            process_extra_args=process_extra_args,
            dispersion_extra_args=dispersion_extra_args,
        )
        evidence_dir = standardize_outputs(q_dir, stem, depth_threshold=args.depth_threshold)
        verdict = build_onsite_verdict(q_dir, evidence_dir, phase_seconds=phase_seconds)
        phase_seconds["cavity_card"] = refresh_cavity_card(args, results_root, card_extra_args=card_extra_args)
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "resume-existing-raw",
                    "onsite_verdict": verdict,
                    "q_dir": str(q_dir),
                    "evidence_dir": str(evidence_dir),
                    "stem": stem,
                    "rows": meta.get("rows"),
                    "sample_rate_hz": meta.get("actual_sample_rate_hz"),
                },
                indent=2,
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0

    acquire_started_at = time.perf_counter()
    acquire_proc = start_command(acquire_args)
    npz_path: Path | None = None
    meta_path: Path | None = None
    stem: str | None = None
    try:
        npz_path, meta_path, stem, phase_seconds["acquire_to_raw_ready"] = wait_for_new_scan_ready(
            q_dir,
            started_at,
            acquire_proc,
        )
        phase_seconds.update(
            run_analysis_chain(
                args,
                q_dir,
                npz_path,
                meta_path,
                stem,
                process_extra_args=process_extra_args,
                dispersion_extra_args=dispersion_extra_args,
            )
        )
    finally:
        return_code = acquire_proc.wait()
        phase_seconds["acquire_total"] = time.perf_counter() - acquire_started_at
        phase_seconds["post_raw_restore_overlap"] = max(
            0.0,
            phase_seconds["acquire_total"] - phase_seconds.get("acquire_to_raw_ready", 0.0),
        )
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, acquire_args)

    if npz_path is None or meta_path is None or stem is None:
        raise RuntimeError("Acquisition did not produce a scan for analysis.")

    meta = assert_acquisition_gates(npz_path, meta_path)
    evidence_dir = standardize_outputs(q_dir, stem, depth_threshold=args.depth_threshold)
    verdict = build_onsite_verdict(q_dir, evidence_dir, phase_seconds=phase_seconds)
    phase_seconds["cavity_card"] = refresh_cavity_card(args, results_root, card_extra_args=card_extra_args)
    print(
        json.dumps(
            {
                "ok": True,
                "onsite_verdict": verdict,
                "q_dir": str(q_dir),
                "evidence_dir": str(evidence_dir),
                "stem": stem,
                "rows": meta.get("rows"),
                "sample_rate_hz": meta.get("actual_sample_rate_hz"),
            },
            indent=2,
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
