"""Shared data-root helpers for microcavity large-scan scripts."""

from __future__ import annotations

import os
import sys
from pathlib import Path


DATA_ROOT_ENV = "DAILY_NOTE_DATA_ROOT"
CAMPAIGN_ENV = "DAILY_NOTE_CAMPAIGN"
CHIP_ENV = "DAILY_NOTE_CHIP"
DEFAULT_CAMPAIGN = "wafer_measuement/Batch_260515"
DEFAULT_CHIP = "chip7"


def require_data_root() -> Path:
    value = os.environ.get(DATA_ROOT_ENV) or windows_user_environment_value(DATA_ROOT_ENV)
    if not value:
        raise RuntimeError(
            f"Set {DATA_ROOT_ENV} or pass --output-dir / explicit input paths; "
            "scripts no longer default to writing data inside this Git repository."
        )
    return Path(value).expanduser()


def windows_user_environment_value(name: str) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _value_type = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return str(value) if value else None


def env_or_default(name: str, default: str) -> str:
    return os.environ.get(name) or windows_user_environment_value(name) or default


def default_campaign() -> str:
    return env_or_default(CAMPAIGN_ENV, DEFAULT_CAMPAIGN)


def default_chip() -> str:
    return env_or_default(CHIP_ENV, DEFAULT_CHIP)


def campaign_parts(campaign: str | None = None) -> tuple[str, ...]:
    value = campaign or default_campaign()
    normalized = value.replace("\\", "/").strip("/")
    if not normalized:
        raise ValueError("Campaign path cannot be empty.")
    return tuple(part for part in normalized.split("/") if part)


def default_results_dir(*parts: str, campaign: str | None = None) -> Path:
    return require_data_root().joinpath("experiments", *campaign_parts(campaign), "results", *parts)


def default_cavity_dir(chip: str, die: str, cavity: str, *, campaign: str | None = None) -> Path:
    return default_results_dir(chip, die, cavity, campaign=campaign)

