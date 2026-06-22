#!/usr/bin/env python3
"""Write the fixed-format per-cavity HTML card."""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, unquote, urlparse

from chip7_design import C_M_PER_S, chip7_design_for_die
from data_paths import CAMPAIGN_ENV, CHIP_ENV, DATA_ROOT_ENV, default_campaign, default_chip, default_results_dir


CARD_CSS = """  body { margin: 26px; font-family: Arial, "Microsoft YaHei", sans-serif; color: #111; background: #fff; }
  h1 { margin: 0 0 16px; padding-bottom: 14px; border-bottom: 1px solid #ddd; font-size: 28px; }
  .card { display: grid; grid-template-columns: minmax(380px, 0.9fr) minmax(560px, 1.25fr) minmax(500px, 1fr); gap: 24px; align-items: stretch; max-width: 1760px; min-height: 632px; }
  .panel { border: 1px solid #d8d8d8; border-radius: 4px; padding: 22px; min-height: 632px; box-sizing: border-box; background: #fff; }
  .cavity-title { font-size: 20px; font-weight: 700; margin: 0 0 14px; }
  .film { width: 100%; height: 214px; object-fit: cover; display: block; margin-bottom: 16px; background: #fafafa; border: 1px solid #e3e3e3; box-sizing: border-box; }
  table { border-collapse: collapse; width: 100%; font-size: 14px; }
  th, td { border-bottom: 1px solid #d8d8d8; padding: 7px 0; text-align: left; vertical-align: top; }
  th { font-weight: 700; }
  .info { margin-bottom: 14px; }
  .kv td:first-child { font-weight: 700; width: 36%; }
  .note { font-size: 14px; line-height: 1.5; margin-top: 12px; }
  .plot-title { font-size: 16px; font-weight: 700; margin: 0 0 16px; }
  .plot { width: 100%; height: 548px; object-fit: contain; display: block; margin: 0 auto; }
  .placeholder { height: 530px; border: 1px solid #d8d8d8; color: #777; display: flex; align-items: center; justify-content: center; text-align: center; line-height: 1.5; background: #fafafa; }
  .review-links { max-width: 1760px; margin-top: 14px; padding: 12px 14px; border: 1px solid #d8d8d8; border-radius: 4px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; box-sizing: border-box; }
  .review-links .label { font-weight: 700; margin-right: 4px; }
  .review-links a, .review-links span { display: inline-flex; align-items: center; min-height: 30px; padding: 0 12px; border: 1px solid #111; border-radius: 4px; font-size: 14px; text-decoration: none; color: #111; background: #fff; }
  .review-links span { border-color: #cfcfcf; color: #777; background: #fafafa; }
"""


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
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
        help=f"Root containing <die>/<cavity>. Defaults to ${DATA_ROOT_ENV}/experiments/<campaign>/results/<chip>.",
    )
    parser.add_argument("--radius-um", type=float, default=None, help="Cavity radius for cards without a built-in design helper.")
    parser.add_argument("--gap-um", type=float, default=None, help="Coupling gap for cards without a built-in design helper.")
    parser.add_argument("--throughput", default=None, help="Displayed throughput text. Defaults to the existing card value.")
    parser.add_argument("--output-power-uw", type=float, default=None, help="Measured out-coupled power in uW.")
    parser.add_argument("--input-monitor-power-uw", type=float, default=1.0, help="Input-side monitor power in uW.")
    parser.add_argument("--input-monitor-fraction", type=float, default=0.01, help="Fractional input monitor tap, e.g. 0.01 for 1%%.")
    parser.add_argument(
        "--loss-mode",
        choices=("single-ended", "total"),
        default="single-ended",
        help="Insertion-loss display convention when --output-power-uw is provided.",
    )
    parser.add_argument("--sensitivity", default=None, help="Displayed sensitivity text. Defaults to existing card value or pending.")
    parser.add_argument("--note", default=None, help="Short analysis note. Auto-generated when omitted.")
    parser.add_argument("--skip-reason", default=None, help="Write a skipped/not-measured card with this reason.")
    parser.add_argument("--photo", type=Path, default=None, help="Override cavity photo path.")
    return parser.parse_args(list(argv))


def text_or_pending(value: str | None) -> str:
    if value is None or not value.strip():
        return "pending"
    return value.strip()


def throughput_from_power_text(
    output_power_uw: float | None,
    input_monitor_power_uw: float,
    input_monitor_fraction: float,
    loss_mode: str,
) -> str | None:
    if output_power_uw is None:
        return None
    if input_monitor_power_uw <= 0:
        raise ValueError("--input-monitor-power-uw must be positive")
    if input_monitor_fraction <= 0:
        raise ValueError("--input-monitor-fraction must be positive")
    if output_power_uw < 0:
        raise ValueError("--output-power-uw cannot be negative")

    input_power_uw = input_monitor_power_uw / input_monitor_fraction
    throughput = output_power_uw / input_power_uw
    throughput_pct = throughput * 100.0
    if throughput > 0:
        if loss_mode == "single-ended":
            loss_db = -10.0 * math.log10(math.sqrt(throughput))
            loss_text = f"single-ended {loss_db:.2f} dB"
        else:
            loss_db = -10.0 * math.log10(throughput)
            loss_text = f"total {loss_db:.2f} dB"
    else:
        loss_text = "loss undefined at zero output"
    return (
        f"Pout {output_power_uw:g} uW; total throughput {throughput_pct:.3g}% "
        f"({input_power_uw:g} uW input); {loss_text}"
    )


def existing_cell(card_text: str, label: str) -> str | None:
    pattern = re.compile(rf"<tr><td>{re.escape(label)}</td><td>(.*?)</td></tr>", re.IGNORECASE | re.DOTALL)
    match = pattern.search(card_text)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip()


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def relative_asset_link(path: Path, base_dir: Path) -> str:
    relative = os.path.relpath(path.resolve(), base_dir.resolve())
    return quote(relative.replace("\\", "/"), safe="/:")


def existing_photo_link(existing_html: str | None, base_dir: Path) -> str | None:
    if not existing_html:
        return None
    match = re.search(r'<img class="film" src="([^"]+)"', existing_html)
    if not match:
        return None
    src = html.unescape(match.group(1))
    parsed = urlparse(src)
    if parsed.scheme == "file":
        local_path = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:/", local_path):
            local_path = local_path[1:]
        return relative_asset_link(Path(local_path), base_dir)
    if re.match(r"^[A-Za-z]:[\\/]", src):
        return relative_asset_link(Path(src), base_dir)
    return src


def find_photo(
    results_root: Path,
    chip: str,
    die: str,
    cavity: str,
    existing_html: str | None,
    override: Path | None,
    *,
    base_dir: Path,
) -> str:
    if override is not None:
        return relative_asset_link(override, base_dir)

    existing = existing_photo_link(existing_html, base_dir)
    if existing:
        return existing

    figure_dirs = [
        results_root / "figures" / "measurement" / chip / die,
        results_root.parent / "figures" / "measurement" / chip / die,
    ]
    if results_root.parent.name.lower() == "results":
        figure_dirs.append(results_root.parent.parent / "figures" / "measurement" / chip / die)

    for figure_dir in figure_dirs:
        if not figure_dir.exists():
            continue
        candidates = sorted(
            [
                path
                for path in figure_dir.iterdir()
                if path.is_file()
                and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
                and path.name.lower().startswith(cavity.lower())
            ],
            key=lambda path: (len(path.name), path.name),
        )
        if candidates:
            return relative_asset_link(candidates[0], base_dir)
    return ""


def latest_evidence_dir(q_dir: Path) -> Path | None:
    evidence_root = q_dir / "evidence"
    if not evidence_root.exists():
        return None
    dirs = [path for path in evidence_root.glob("processing_*") if path.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda path: path.stat().st_mtime)


def family_labels(family_points: Path) -> dict[str, str]:
    labels: dict[str, str] = {}
    if not family_points.exists():
        return labels
    with family_points.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            family = row.get("family", "")
            label = row.get("family_label", "")
            if family and label:
                labels[family] = label
    return labels


def q0_mle_million(rows: list[dict[str, str]]) -> float:
    values = [math.log10(float(row["Q0"])) for row in rows if float(row.get("Q0", "nan")) > 0]
    if not values:
        return float("nan")
    low = min(values)
    bin_width = 0.08
    bins: dict[int, list[float]] = {}
    for value in values:
        bins.setdefault(math.floor((value - low) / bin_width), []).append(value)
    center = sum(values) / len(values)
    best = max(bins.values(), key=lambda bucket: (len(bucket), -abs(sum(bucket) / len(bucket) - center)))
    return 10 ** (sum(best) / len(best)) / 1e6


def load_q_rows(path: Path) -> dict[str, list[dict[str, str]]]:
    rows_by_family: dict[str, list[dict[str, str]]] = {}
    if not path.exists():
        return rows_by_family
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("fit_status") != "ok":
                continue
            rows_by_family.setdefault(row["family"], []).append(row)
    return rows_by_family


def acquisition_run_text(q_dir: Path) -> str:
    path = q_dir / "acquisition.json"
    if not path.exists():
        return "not measured"
    data = read_json(path)
    created = str(data.get("created_at", ""))[:16].replace("T", " ")
    sample_rate = float(data.get("actual_sample_rate_hz", data.get("config", {}).get("sample_rate_hz", 0.0)))
    if sample_rate:
        return f"{created}; {sample_rate/1000:.0f} kSa/s large scan"
    return created or "measured"


def mode_rows_html(q_dir: Path, radius_um: float | None) -> str:
    evidence_dir = latest_evidence_dir(q_dir)
    if evidence_dir is None:
        return '<tr><td colspan="5">not measured</td></tr>'
    dispersion_path = evidence_dir / "dispersion_summary.json"
    q_summary_path = evidence_dir / "q_summary.json"
    if not dispersion_path.exists() or not q_summary_path.exists():
        return '<tr><td colspan="5">analysis pending</td></tr>'

    dispersion = read_json(dispersion_path)
    q_summary = read_json(q_summary_path)
    labels = family_labels(q_dir / "family_points.csv")
    q_rows = load_q_rows(q_dir / "q_by_mode.csv")
    radius_m = radius_um * 1e-6 if radius_um is not None else None

    rows: list[dict[str, object]] = []
    for fit in dispersion.get("auto_centered_fits", []):
        family = str(fit.get("name", ""))
        if "quadratic" not in fit or int(fit.get("count", 0)) <= 0:
            continue
        quadratic = fit["quadratic"]
        d1_mhz = float(quadratic["effective_d1_mhz"])
        ng = C_M_PER_S / (d1_mhz * 1e6 * 2.0 * math.pi * radius_m) if radius_m is not None else float("nan")
        fam_q_rows = q_rows.get(family, [])
        q0 = q0_mle_million(fam_q_rows) if fam_q_rows else float("nan")
        rows.append(
            {
                "label": labels.get(family, family),
                "fsr": d1_mhz / 1000.0,
                "ng": ng,
                "d2": float(quadratic["d2_mhz_per_mode2"]),
                "q0": q0 if math.isfinite(q0) else float(q_summary.get("family_summary", {}).get(family, {}).get("Q0_median_M", float("nan"))),
            }
        )

    if not rows:
        return '<tr><td colspan="5">analysis pending</td></tr>'
    lines = []
    for row in sorted(rows, key=lambda item: str(item["label"])):
        ng_text = f"{float(row['ng']):.4f}" if math.isfinite(float(row["ng"])) else "pending"
        lines.append(
            "<tr>"
            f"<td>{html.escape(str(row['label']))}</td>"
            f"<td>{float(row['fsr']):.3f}</td>"
            f"<td>{ng_text}</td>"
            f"<td>{float(row['d2']):.1f}</td>"
            f"<td>{float(row['q0']):.3f}</td>"
            "</tr>"
        )
    return "\n      ".join(lines)


def auto_note(q_dir: Path) -> str:
    evidence_dir = latest_evidence_dir(q_dir)
    if evidence_dir is None:
        return "Not measured in this run."
    dispersion_path = evidence_dir / "dispersion_summary.json"
    q_summary_path = evidence_dir / "q_summary.json"
    if not dispersion_path.exists() or not q_summary_path.exists():
        return "Analysis pending."
    dispersion = read_json(dispersion_path)
    q_summary = read_json(q_summary_path)
    q_fit = f"Q fit {q_summary.get('ok_count', 0)}/{q_summary.get('mode_count', 0)}"
    branch_log = dispersion.get("branch_extension_log", [])
    branch_text = ""
    if branch_log:
        entries = []
        labels = family_labels(q_dir / "family_points.csv")
        for entry in branch_log:
            label = labels.get(str(entry.get("family", "")), str(entry.get("family", "")))
            modes = entry.get("added_modes", [])
            if modes:
                entries.append(f"{label} branch extension added m={min(modes)}..{max(modes)}")
        if entries:
            branch_text = "; " + "; ".join(entries)
    fits = [fit for fit in dispersion.get("auto_centered_fits", []) if "quadratic" in fit and int(fit.get("count", 0)) > 0]
    high_rms = [
        f"{family_labels(q_dir / 'family_points.csv').get(str(fit['name']), str(fit['name']))} rms {float(fit['quadratic']['rms_residual_mhz']):.0f} MHz"
        for fit in fits
        if float(fit["quadratic"]["rms_residual_mhz"]) > 100.0
    ]
    limit = f"; D2 residual-limited: {', '.join(high_rms)}" if high_rms else ""
    return f"{q_fit}{branch_text}{limit}."


def sensitivity_panel(cavity_dir: Path) -> str:
    sensitivity_dir = cavity_dir / "sensitivity"
    latest = latest_sensitivity(cavity_dir)
    latest_png = sensitivity_path_from_latest(cavity_dir, latest, "figure_png")
    if latest_png and latest_png.exists():
        return f'<img class="plot" src="{html.escape(rel_href(latest_png, cavity_dir))}" alt="sensitivity">'
    candidates: list[Path] = []
    if sensitivity_dir.exists():
        candidates = sorted(
            [path for path in sensitivity_dir.rglob("*") if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg"}],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    if candidates:
        return f'<img class="plot" src="{html.escape(rel_href(candidates[0], cavity_dir))}" alt="sensitivity">'
    return '<div class="placeholder">pending<br>sensitivity figure</div>'


def rel_href(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve())).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


def latest_sensitivity(cavity_dir: Path) -> dict:
    latest_path = cavity_dir / "sensitivity" / "latest.json"
    if not latest_path.exists():
        return {}
    try:
        return json.loads(latest_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def sensitivity_path_from_latest(cavity_dir: Path, latest: dict, key: str) -> Path | None:
    value = latest.get(key)
    if not value:
        return None
    path = Path(str(value))
    if path.is_absolute():
        return path
    return cavity_dir / path


def sensitivity_run_dir_from_latest(cavity_dir: Path, latest: dict) -> Path | None:
    value = latest.get("run_dir")
    if not value:
        return None
    path = Path(str(value))
    if path.is_absolute():
        return path
    return cavity_dir / "sensitivity" / path


def sensitivity_review_link(
    cavity_dir: Path,
    latest: dict,
    *,
    latest_key: str,
    fallback_filename: str,
    label: str,
) -> str:
    latest_path = sensitivity_path_from_latest(cavity_dir, latest, latest_key)
    if latest_path and latest_path.exists():
        return f'<a href="{html.escape(rel_href(latest_path, cavity_dir))}">{html.escape(label)}</a>'

    plots = latest.get("plots") if isinstance(latest.get("plots"), dict) else {}
    plot_value = plots.get(latest_key)
    if plot_value:
        plot_path = Path(str(plot_value))
        if not plot_path.is_absolute():
            plot_path = cavity_dir / plot_path
        if plot_path.exists():
            return f'<a href="{html.escape(rel_href(plot_path, cavity_dir))}">{html.escape(label)}</a>'

    run_dir = sensitivity_run_dir_from_latest(cavity_dir, latest)
    fallback_path = run_dir / "figures" / fallback_filename if run_dir else None
    if fallback_path and fallback_path.exists():
        return f'<a href="{html.escape(rel_href(fallback_path, cavity_dir))}">{html.escape(label)}</a>'
    return f"<span>{html.escape(label)} pending</span>"


def q_trend_panel(q_dir: Path, force_pending: bool = False) -> str:
    if force_pending:
        return '<div class="placeholder">pending<br>Q trend</div>'
    if (q_dir / "q_trend.png").exists():
        return '<img class="plot" src="Q/q_trend.png" alt="Q trend">'
    return '<div class="placeholder">pending<br>Q trend</div>'


def review_links(cavity_dir: Path) -> str:
    q_review = cavity_dir / "Q" / "interactive_q.html"
    if not q_review.exists():
        q_review = cavity_dir / "Q" / "interactive_q_demo.html"
    q_html = (
        '<a href="Q/interactive_q.html">Q / dispersion review</a>'
        if q_review.name == "interactive_q.html" and q_review.exists()
        else '<a href="Q/interactive_q_demo.html">Q / dispersion review</a>'
        if q_review.exists()
        else "<span>Q / dispersion review pending</span>"
    )
    latest = latest_sensitivity(cavity_dir)
    latest_review = sensitivity_path_from_latest(cavity_dir, latest, "interactive_html")
    if latest_review and latest_review.exists():
        sensitivity_html = f'<a href="{html.escape(rel_href(latest_review, cavity_dir))}">Sensitivity review</a>'
    else:
        sensitivity_review = cavity_dir / "sensitivity" / "interactive_sensitivity.html"
        sensitivity_html = (
            '<a href="sensitivity/interactive_sensitivity.html">Sensitivity review</a>'
            if sensitivity_review.exists()
            else "<span>Sensitivity review pending</span>"
        )
    noise_html = sensitivity_review_link(
        cavity_dir,
        latest,
        latest_key="noise_psd_html",
        fallback_filename="noise_psd.html",
        label="Noise PSD review",
    )
    network_html = sensitivity_review_link(
        cavity_dir,
        latest,
        latest_key="network_response_html",
        fallback_filename="network_response.html",
        label="Network response review",
    )
    return (
        '<section class="review-links">\n'
        '  <div class="label">Interactive reviews</div>\n'
        f"  {q_html}\n"
        f"  {sensitivity_html}\n"
        f"  {noise_html}\n"
        f"  {network_html}\n"
        "</section>"
    )


def design_values(chip: str, die: str, cavity: str, radius_um: float | None, gap_um: float | None) -> tuple[float | None, float | None]:
    if chip.lower() == "chip7":
        design = chip7_design_for_die(die)
        return design.radius_um, design.gap_for_cavity(cavity)
    return radius_um, gap_um


def radius_gap_text(radius_um: float | None, gap_um: float | None) -> str:
    radius = f"{radius_um:.1f} um" if radius_um is not None else "pending"
    gap = f"{gap_um:g} um" if gap_um is not None else "pending"
    return f"{radius} / {gap}"


def write_card(args: argparse.Namespace) -> Path:
    results_root = args.results_root if args.results_root is not None else default_results_dir(args.chip, campaign=args.campaign)
    cavity_dir = results_root / args.die / args.cavity
    q_dir = cavity_dir / "Q"
    card_path = cavity_dir / "cavity_card.html"
    existing = card_path.read_text(encoding="utf-8") if card_path.exists() else ""
    power_throughput = throughput_from_power_text(
        args.output_power_uw,
        args.input_monitor_power_uw,
        args.input_monitor_fraction,
        args.loss_mode,
    )
    throughput = text_or_pending(args.throughput or power_throughput or existing_cell(existing, "throughput"))
    sensitivity = text_or_pending(args.sensitivity or existing_cell(existing, "sensitivity"))
    radius_um, gap_um = design_values(args.chip, args.die, args.cavity, args.radius_um, args.gap_um)
    photo = find_photo(results_root, args.chip, args.die, args.cavity, existing, args.photo, base_dir=cavity_dir)

    if args.skip_reason:
        mode_rows = '<tr><td colspan="5">not measured</td></tr>'
        run_text = "not measured"
        note = args.skip_reason
        q_panel = q_trend_panel(q_dir, force_pending=True)
    else:
        mode_rows = mode_rows_html(q_dir, radius_um)
        run_text = acquisition_run_text(q_dir)
        note = args.note or auto_note(q_dir)
        q_panel = q_trend_panel(q_dir)

    photo_html = (
        f'<img class="film" src="{html.escape(photo)}" alt="{html.escape(args.chip)} {html.escape(args.die)} {html.escape(args.cavity)} film">'
        if photo
        else '<div class="film">cavity photo missing</div>'
    )
    sensitivity_html = sensitivity_panel(cavity_dir)
    content = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{html.escape(args.chip)} {html.escape(args.die)} {html.escape(args.cavity)} cavity card</title>
<style>
{CARD_CSS}</style>
</head>
<body>
<h1>Cavity ID Card</h1>
<div class="card">
  <section class="panel">
    <h2 class="cavity-title">{html.escape(args.chip)} / {html.escape(args.die)} / {html.escape(args.cavity)}</h2>
    {photo_html}
    <table class="kv info">
      <tr><td>R / gap</td><td>{html.escape(radius_gap_text(radius_um, gap_um))}</td></tr>
      <tr><td>throughput</td><td>{html.escape(throughput)}</td></tr>
      <tr><td>sensitivity</td><td>{html.escape(sensitivity)}</td></tr>
      <tr><td>run</td><td>{html.escape(run_text)}</td></tr>
    </table>
    <table>
      <tr><th>mode</th><th>FSR (GHz)</th><th>n_g</th><th>D2 (MHz)</th><th>Q0 (M)</th></tr>
      {mode_rows}
    </table>
    <div class="note">
      <strong>Run:</strong> {html.escape(run_text)}.<br>
      <strong>Note:</strong> {html.escape(note)}
    </div>
  </section>
  <section class="panel">
    <div class="plot-title">Q trend snapshot</div>
    {q_panel}
  </section>
  <section class="panel">
    <div class="plot-title">sensitivity</div>
    {sensitivity_html}
  </section>
</div>
{review_links(cavity_dir)}
</body>
</html>
"""
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_text(content, encoding="utf-8")
    return card_path


def main(argv: Iterable[str]) -> int:
    path = write_card(parse_args(argv))
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
