#!/usr/bin/env python3
"""Summarize standardized large-scan Q outputs for one chip/die."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
from pathlib import Path
from typing import Iterable

from chip7_design import C_M_PER_S, chip7_design_for_die
from data_paths import default_campaign, default_chip, default_results_dir


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", default=default_campaign())
    parser.add_argument("--chip", default=default_chip())
    parser.add_argument("--die", required=True)
    parser.add_argument(
        "--results-root",
        type=Path,
        default=None,
        help="Root containing <die>/<cavity>. Defaults to $DAILY_NOTE_DATA_ROOT/experiments/<campaign>/results/<chip>.",
    )
    parser.add_argument("--output-dir", type=Path, default=None, help="Defaults to the die result directory.")
    parser.add_argument(
        "--family-map-json",
        type=Path,
        default=None,
        help=(
            "Optional JSON mapping unified family names to cavity display labels, "
            'for example {"Family A": {"c2": "mode2", "c5": "mode1"}}.'
        ),
    )
    parser.add_argument("--alignment-target-nm", type=float, default=1550.0)
    return parser.parse_args(list(argv))


def latest_evidence_dir(q_dir: Path) -> Path | None:
    evidence_root = q_dir / "evidence"
    if not evidence_root.exists():
        return None
    candidates = [path for path in evidence_root.glob("processing_*") if path.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def q0_mle_million(values: list[float]) -> float:
    logs = [math.log10(value) for value in values if value > 0]
    if not logs:
        return float("nan")
    low = min(logs)
    bin_width = 0.08
    center = sum(logs) / len(logs)
    bins: dict[int, list[float]] = {}
    for value in logs:
        bins.setdefault(math.floor((value - low) / bin_width), []).append(value)
    best = max(bins.values(), key=lambda bucket: (len(bucket), -abs(sum(bucket) / len(bucket) - center)))
    return 10 ** (sum(best) / len(best)) / 1e6


def mean_million(rows: list[dict[str, str]], key: str) -> float:
    values = [float(row[key]) for row in rows if row.get(key) not in (None, "", "nan")]
    return sum(values) / len(values) / 1e6 if values else float("nan")


def nearest_q1_1550_million(rows: list[dict[str, str]]) -> float:
    if not rows:
        return float("nan")
    row = min(rows, key=lambda item: abs(float(item["wavelength_nm"]) - 1550.0))
    return float(row["Q1"]) / 1e6


def mode_wavelength(rows: list[dict[str, str]], mode_number: int) -> float:
    for row in rows:
        if int(float(row["mode_number"])) == mode_number:
            return float(row["wavelength_nm"])
    return float("nan")


def read_q_rows(q_dir: Path) -> list[dict[str, str]]:
    path = q_dir / "q_by_mode.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle) if row.get("fit_status") == "ok"]


def family_label_map(q_dir: Path, q_summary: dict[str, object]) -> dict[str, str]:
    summary = q_summary.get("family_summary", {})
    labels: dict[str, str] = {}
    if isinstance(summary, dict):
        for family, data in summary.items():
            if isinstance(data, dict):
                label = str(data.get("family_label", family))
                labels[str(family)] = label
    if labels:
        return labels

    path = q_dir / "family_points.csv"
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            family = row.get("family", "")
            label = row.get("family_label", "")
            if family and label:
                labels[family] = label
    return labels


def html_cell(card_text: str, label: str) -> str:
    match = re.search(rf"<tr><td>{re.escape(label)}</td><td>(.*?)</td></tr>", card_text, flags=re.I | re.S)
    if not match:
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<.*?>", "", match.group(1)))).strip()


def card_power_fields(cavity_dir: Path) -> tuple[str, str]:
    path = cavity_dir / "cavity_card.html"
    if not path.exists():
        return "", ""
    throughput = html_cell(path.read_text(encoding="utf-8"), "throughput")
    power_match = re.search(r"Pout\s+([0-9.]+)\s+uW", throughput)
    power = f"{float(power_match.group(1)):g}" if power_match else ""
    return power, throughput


def design_gap(chip: str, die: str, cavity: str) -> tuple[float | None, float | None]:
    if chip.lower() != "chip7":
        return None, None
    design = chip7_design_for_die(die)
    return design.radius_um, design.gap_for_cavity(cavity)


def collect_die(results_root: Path, chip: str, die: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    die_dir = results_root / die
    cavity_rows: list[dict[str, object]] = []
    family_rows: list[dict[str, object]] = []
    for index in range(1, 10):
        cavity = f"c{index}"
        cavity_dir = die_dir / cavity
        q_dir = cavity_dir / "Q"
        radius_um, gap_um = design_gap(chip, die, cavity)
        output_power_uw, throughput_text = card_power_fields(cavity_dir)
        row: dict[str, object] = {
            "chip": chip,
            "die": die,
            "cavity": cavity,
            "radius_um": radius_um,
            "gap_um": gap_um,
            "output_power_uw": output_power_uw,
            "throughput_text": throughput_text,
            "has_card": (cavity_dir / "cavity_card.html").exists(),
            "has_formal_q": False,
        }
        evidence_dir = latest_evidence_dir(q_dir)
        if evidence_dir is None or not (evidence_dir / "dispersion_summary.json").exists():
            cavity_rows.append(row)
            continue

        process_summary = read_json(evidence_dir / "process_summary.json")
        dispersion_summary = read_json(evidence_dir / "dispersion_summary.json")
        q_summary = read_json(evidence_dir / "q_summary.json") if (evidence_dir / "q_summary.json").exists() else {}
        q_rows = read_q_rows(q_dir)
        labels = family_label_map(q_dir, q_summary)
        row.update(
            {
                "has_formal_q": True,
                "evidence_dir": str(evidence_dir),
                "dip_count": process_summary.get("dip_count", ""),
                "depth_filtered_dip_count": dispersion_summary.get("depth_filtered_dip_count", ""),
                "q_ok_count": q_summary.get("ok_count", ""),
                "q_mode_count": q_summary.get("mode_count", ""),
            }
        )
        cavity_rows.append(row)

        for fit in dispersion_summary.get("auto_centered_fits", []):
            if not isinstance(fit, dict) or "quadratic" not in fit:
                continue
            family = str(fit["name"])
            label = labels.get(family, family)
            family_q_rows = [qrow for qrow in q_rows if qrow.get("family") == family]
            quadratic = fit["quadratic"]
            spacing = fit.get("spacing_quality", {})
            effective_d1_mhz = float(quadratic["effective_d1_mhz"])
            radius_m = radius_um * 1e-6 if radius_um is not None else float("nan")
            ng = C_M_PER_S / (effective_d1_mhz * 1e6 * 2.0 * math.pi * radius_m) if radius_um else float("nan")
            q0_values = [float(qrow["Q0"]) for qrow in family_q_rows if float(qrow.get("Q0", "nan")) > 0]
            family_rows.append(
                {
                    "chip": chip,
                    "die": die,
                    "cavity": cavity,
                    "gap_um": gap_um,
                    "family": family,
                    "family_label": label,
                    "count": int(fit.get("count", 0)),
                    "mode_min": fit.get("mode_min", ""),
                    "mode_max": fit.get("mode_max", ""),
                    "max_mode_gap": spacing.get("max_mode_gap", ""),
                    "spacing_ok": spacing.get("spacing_ok", ""),
                    "fsr_ghz": effective_d1_mhz / 1000.0,
                    "ng": ng,
                    "d2_mhz": float(quadratic["d2_mhz_per_mode2"]),
                    "rms_mhz": float(quadratic["rms_residual_mhz"]),
                    "q0_mean_M": mean_million(family_q_rows, "Q0"),
                    "q0_mle_M": q0_mle_million(q0_values),
                    "q1_mean_M": mean_million(family_q_rows, "Q1"),
                    "q1_at_1550_M": nearest_q1_1550_million(family_q_rows),
                    "local_mu0_wavelength_nm": mode_wavelength(family_q_rows, 0),
                }
            )
    return cavity_rows, family_rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def best_aligned_modes(rows: list[dict[str, str]], target_nm: float) -> dict[str, tuple[int, float]]:
    by_cavity: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_cavity.setdefault(row["cavity"], []).append(row)
    candidates = [row for family_rows in by_cavity.values() for row in family_rows]
    best: tuple[float, float, float, dict[str, tuple[int, float]]] | None = None
    for candidate in candidates:
        target = float(candidate["wavelength_nm"])
        picks: dict[str, tuple[int, float]] = {}
        wavelengths: list[float] = []
        for cavity, cavity_rows in by_cavity.items():
            pick = min(cavity_rows, key=lambda item: abs(float(item["wavelength_nm"]) - target))
            mode = int(float(pick["mode_number"]))
            wavelength = float(pick["wavelength_nm"])
            picks[cavity] = (mode, wavelength)
            wavelengths.append(wavelength)
        span = max(wavelengths) - min(wavelengths)
        mean_wavelength = sum(wavelengths) / len(wavelengths)
        variance = sum((value - mean_wavelength) ** 2 for value in wavelengths)
        score = (span + 0.05 * abs(mean_wavelength - target_nm), span, variance, picks)
        if best is None or score[:3] < best[:3]:
            best = score
    return best[3] if best else {}


def collect_unified_rows(
    family_map: dict[str, dict[str, str]],
    results_root: Path,
    chip: str,
    die: str,
    *,
    alignment_target_nm: float,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for unified_family, cavity_labels in family_map.items():
        q_rows_for_family: list[dict[str, str]] = []
        for cavity, label in cavity_labels.items():
            q_rows = read_q_rows(results_root / die / cavity / "Q")
            for row in q_rows:
                if row.get("family_label") == label:
                    item = dict(row)
                    item["cavity"] = cavity
                    q_rows_for_family.append(item)
        aligned = best_aligned_modes(q_rows_for_family, alignment_target_nm)
        for cavity, label in cavity_labels.items():
            match = aligned.get(cavity)
            rows.append(
                {
                    "unified_family": unified_family,
                    "cavity": cavity,
                    "family_label": label,
                    "aligned_local_mu": match[0] if match else "",
                    "global_mu0_wavelength_nm": match[1] if match else "",
                }
            )
    return rows


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    results_root = args.results_root if args.results_root is not None else default_results_dir(args.chip, campaign=args.campaign)
    output_dir = args.output_dir if args.output_dir is not None else results_root / args.die
    cavity_rows, family_rows = collect_die(results_root, args.chip, args.die)

    write_csv(output_dir / "die_cavity_summary.csv", cavity_rows)
    write_csv(output_dir / "die_family_summary.csv", family_rows)
    summary: dict[str, object] = {
        "campaign": args.campaign,
        "chip": args.chip,
        "die": args.die,
        "results_root": str(results_root),
        "cavity_summary_csv": str(output_dir / "die_cavity_summary.csv"),
        "family_summary_csv": str(output_dir / "die_family_summary.csv"),
        "cavities": cavity_rows,
        "families": family_rows,
    }
    if args.family_map_json is not None:
        family_map = json.loads(args.family_map_json.read_text(encoding="utf-8-sig"))
        unified_rows = collect_unified_rows(
            family_map,
            results_root,
            args.chip,
            args.die,
            alignment_target_nm=args.alignment_target_nm,
        )
        write_csv(output_dir / "die_unified_family_alignment.csv", unified_rows)
        summary["family_map_json"] = str(args.family_map_json)
        summary["unified_family_alignment_csv"] = str(output_dir / "die_unified_family_alignment.csv")
        summary["unified_family_alignment"] = unified_rows

    json_path = output_dir / "die_summary.json"
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"ok": True, "die_summary_json": str(json_path)}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
