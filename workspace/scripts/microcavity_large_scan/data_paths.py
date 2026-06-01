"""Shared data-root helpers for microcavity large-scan scripts."""

from __future__ import annotations

import os
from pathlib import Path


DATA_ROOT_ENV = "DAILY_NOTE_DATA_ROOT"
SESSION_RELATIVE_RESULTS = (
    "experiments",
    "2026-05-28",
    "four_inch_sample_formal_measurement",
    "results",
)


def require_data_root() -> Path:
    value = os.environ.get(DATA_ROOT_ENV)
    if not value:
        raise RuntimeError(
            f"Set {DATA_ROOT_ENV} or pass --output-dir / explicit input paths; "
            "scripts no longer default to writing data inside this Git repository."
        )
    return Path(value).expanduser()


def default_results_dir(*parts: str) -> Path:
    return require_data_root().joinpath(*SESSION_RELATIVE_RESULTS, *parts)


def default_cavity_dir(chip: str, die: str, cavity: str) -> Path:
    return default_results_dir(chip, die, cavity)

