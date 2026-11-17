"""Local, git-ignored values (local.env.yaml) used as defaults for environment variables."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from .common import ROOT, ConfigError

LOCAL_ENV = ROOT / "local.env.yaml"
PLACEHOLDER = "paste-your-key-here"


def load_local_env(path: Path = LOCAL_ENV) -> list[str]:
    """Set os.environ defaults from a flat YAML mapping. Real environment variables win.

    Returns the names that were set. Error messages never include the file's contents,
    because the file holds secrets.
    """
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError):
        raise ConfigError(f"Cannot read {path.name}.") from None
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" near line {mark.line + 1}" if mark is not None else ""
        raise ConfigError(f"{path.name} is not valid YAML{where}. Use lines like  NAME: \"value\"") from None
    if data is None:
        return []
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain NAME: value lines.")
    applied = []
    for name, value in data.items():
        if not isinstance(name, str) or not name.isidentifier():
            raise ConfigError(f"{path.name}: names must be like SERPAPI_API_KEY.")
        if value is None or isinstance(value, (dict, list)):
            continue
        text = str(value).strip()
        if not text or text == PLACEHOLDER or os.environ.get(name, "").strip():
            continue
        os.environ[name] = text
        applied.append(name)
    return applied
