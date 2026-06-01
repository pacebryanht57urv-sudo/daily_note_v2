"""Shared data-root helpers for Red Pitaya microcavity lock scripts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


DATA_ROOT_ENV = "DAILY_NOTE_DATA_ROOT"
RESULTS_RELATIVE_DIR = (
    "experiments",
    "2026-05-22",
    "auto_lock_redpitaya_microcavity",
    "results",
    "pyrpl_live_bridge",
)


def require_data_root() -> Path:
    value = os.environ.get(DATA_ROOT_ENV)
    if not value:
        raise RuntimeError(
            f"Set {DATA_ROOT_ENV} before saving Red Pitaya lock data; "
            "scripts no longer default to writing results inside this Git repository."
        )
    return Path(value).expanduser()


def default_results_dir() -> Path:
    return require_data_root().joinpath(*RESULTS_RELATIVE_DIR)


class DeferredResultsDir:
    """Path-like proxy that resolves the external data root only when used."""

    def resolve(self) -> Path:
        return default_results_dir()

    def __truediv__(self, key: str) -> Path:
        return self.resolve() / key

    def mkdir(self, *args: Any, **kwargs: Any) -> None:
        self.resolve().mkdir(*args, **kwargs)

    def glob(self, pattern: str):
        return self.resolve().glob(pattern)

    def __fspath__(self) -> str:
        return os.fspath(self.resolve())

    def __str__(self) -> str:
        return str(self.resolve())

    def __repr__(self) -> str:
        return f"DeferredResultsDir(env={DATA_ROOT_ENV!r})"


RESULTS_DIR = DeferredResultsDir()

