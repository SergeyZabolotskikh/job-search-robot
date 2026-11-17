"""Shared constants, errors and small helpers."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# src/job_search_robot/common.py -> project root (the package is installed in editable mode by uv).
ROOT = Path(__file__).resolve().parents[2]
VERSION = "0.2.0"


class ConfigError(ValueError):
    """Invalid or missing user configuration."""


class ProviderError(Exception):
    """A search provider request failed. `fatal` stops the whole run (auth, quota)."""

    def __init__(self, message: str, *, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write a text file so that readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
