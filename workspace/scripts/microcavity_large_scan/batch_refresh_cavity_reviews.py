#!/usr/bin/env python3
"""Batch-refresh cavity cards and lightweight interactive Q reviews.

Default mode is a dry run. Pass --apply to write files and remove extra PNGs.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
KEEP_Q_PNG = {"q_trend.png"}


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", type=Path, required=True, help="Root containing die/cavity folders, e.g. .../results/chip7.")
    parser.add_argument("--chip", default="chip7")
    parser.add_argument("--die", action="append", default=None, help="Limit to one or more dies, e.g. --die die3-1.")
    parser.add_argument("--cavity", action="append", default=None, help="Limit to one or more cavities, e.g. --cavity c2.")
    parser.add_argument("--apply", action="store_true", help="Actually write HTML/card files and delete extra PNGs.")
    parser.add_argument("--keep-extra-png", action="store_true", help="Refresh HTML/card but do not delete extra PNGs.")
    parser.add_argument("--max-local-points", type=int, default=400)
    parser.add_argument("--max-one-fsr-points", type=int, default=5000)
    return parser.parse_args(list(argv))


def is_cavity_dir(path: Path) -> bool:
    q_dir = path / "Q"
    return (
        q_dir.is_dir()
        and (q_dir / "raw.npz").exists()
        and (q_dir / "family_points.csv").exists()
        and (q_dir / "q_by_mode.csv").exists()
    )


def iter_cavities(results_root: Path, dies: set[str] | None, cavities: set[str] | None) -> list[Path]:
    out: list[Path] = []
    for die_dir in sorted(path for path in results_root.iterdir() if path.is_dir() and path.name.startswith("die")):
        if dies is not None and die_dir.name not in dies:
            continue
        for cavity_dir in sorted(path for path in die_dir.iterdir() if path.is_dir() and path.name.startswith("c")):
            if cavities is not None and cavity_dir.name not in cavities:
                continue
            if is_cavity_dir(cavity_dir):
                out.append(cavity_dir)
    return out


def run_command(args: list[str]) -> None:
    subprocess.run(args, check=True)


def extra_pngs(q_dir: Path) -> list[Path]:
    return sorted(path for path in q_dir.rglob("*.png") if path.name not in KEEP_Q_PNG)


def delete_extra_pngs(q_dir: Path) -> int:
    q_root = q_dir.resolve()
    count = 0
    for path in extra_pngs(q_dir):
        resolved = path.resolve()
        if q_root not in resolved.parents and resolved != q_root:
            raise RuntimeError(f"Refusing to delete outside Q directory: {resolved}")
        path.unlink()
        count += 1
    return count


def latest_evidence_dir(q_dir: Path) -> Path | None:
    evidence_root = q_dir / "evidence"
    if not evidence_root.exists():
        return None
    dirs = [path for path in evidence_root.glob("processing_*") if path.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda path: path.stat().st_mtime)


def patch_summary_paths(q_dir: Path) -> None:
    evidence_dir = latest_evidence_dir(q_dir)
    if evidence_dir is None:
        return
    patches: dict[str, dict[str, object]] = {
        "process_summary.json": {
            "raw_ch2_ch3_figure": None,
            "flattened_ch2_figure": None,
            "folded_dispersion_figure": None,
        },
        "dispersion_summary.json": {
            "family_points_csv": str(q_dir / "family_points.csv"),
            "auto_centered_family_points_csv": str(q_dir / "family_points.csv"),
            "common_coordinate_fit_figure": None,
            "auto_centered_fit_figure": None,
            "interactive_q_review": str(q_dir / "interactive_q.html"),
        },
        "q_summary.json": {
            "q_table": str(q_dir / "q_by_mode.csv"),
            "trend_figure": str(q_dir / "q_trend.png"),
            "fit_examples_figure": None,
            "local_dip_mosaic_figure": None,
            "interactive_q_review": str(q_dir / "interactive_q.html"),
        },
    }
    for name, patch in patches.items():
        path = evidence_dir / name
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        data.update(patch)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def refresh_cavity(cavity_dir: Path, args: argparse.Namespace) -> dict[str, object]:
    q_dir = cavity_dir / "Q"
    pngs = extra_pngs(q_dir)
    result: dict[str, object] = {
        "cavity": str(cavity_dir),
        "extra_png_count": len(pngs),
        "extra_pngs": [str(path.relative_to(cavity_dir)) for path in pngs],
    }
    if not args.apply:
        return result

    run_command(
        [
            sys.executable,
            str(SCRIPT_DIR / "write_interactive_q_review.py"),
            str(cavity_dir),
            "--max-local-points",
            str(args.max_local_points),
            "--max-one-fsr-points",
            str(args.max_one_fsr_points),
        ]
    )
    run_command(
        [
            sys.executable,
            str(SCRIPT_DIR / "write_cavity_card.py"),
            "--chip",
            args.chip,
            "--die",
            cavity_dir.parent.name,
            "--cavity",
            cavity_dir.name,
            "--results-root",
            str(args.results_root),
        ]
    )
    deleted = 0 if args.keep_extra_png else delete_extra_pngs(q_dir)
    patch_summary_paths(q_dir)
    result["deleted_png_count"] = deleted
    result["interactive_q"] = str(q_dir / "interactive_q.html")
    result["cavity_card"] = str(cavity_dir / "cavity_card.html")
    return result


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    results_root = args.results_root.resolve()
    if not results_root.is_dir():
        raise FileNotFoundError(results_root)
    args.results_root = results_root
    cavities = iter_cavities(
        results_root,
        set(args.die) if args.die else None,
        set(args.cavity) if args.cavity else None,
    )
    results = [refresh_cavity(cavity_dir, args) for cavity_dir in cavities]
    print(
        json.dumps(
            {
                "apply": args.apply,
                "results_root": str(results_root),
                "cavity_count": len(cavities),
                "total_extra_png": sum(int(item["extra_png_count"]) for item in results),
                "results": results,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
