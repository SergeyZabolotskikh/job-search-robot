"""Load and validate sources/positions.toml and sources/search.toml.

Both are private to each user (git-ignored). They start as copies of the tracked
sources/*.example.toml files.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .common import ROOT, ConfigError

DEFAULT_POSITIONS = ROOT / "sources" / "positions.toml"
DEFAULT_SEARCH = ROOT / "sources" / "search.toml"
FRESHNESS = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "any": ""}


@dataclass(frozen=True)
class Track:
    id: str
    label: str
    cv: str


@dataclass(frozen=True)
class Family:
    id: str
    name: str
    track: str
    priority: int
    enabled: bool
    titles: tuple[str, ...]
    notes: str = ""
    evidence: str = ""


@dataclass(frozen=True)
class Platform:
    name: str
    site: str
    enabled: bool


@dataclass(frozen=True)
class Region:
    name: str
    enabled: bool
    cities: tuple[str, ...]


@dataclass(frozen=True)
class Settings:
    tracks: dict[str, Track]
    families: tuple[Family, ...]
    platforms: tuple[Platform, ...]
    regions: tuple[Region, ...]
    query_terms: tuple[str, ...]
    exclude_terms: tuple[str, ...]
    title_exclude: tuple[str, ...]
    monthly_limit: int
    per_run: int
    pages_per_query: int
    max_query_words: int
    revisit_days: tuple[int, ...]
    per_hour: int
    country: str
    language: str
    freshness: str
    timeout_seconds: int
    show_career_pages: bool = False   # company careers pages (boards) are hidden unless true
    old_posting_days: int = 60        # Posted cell shaded when the job is older than this
    allow_paid_plan: bool = False     # False: no search is sent on a SerpApi plan with a price

    @property
    def tbs(self) -> str:
        return FRESHNESS[self.freshness]


def positive_int(value: object, label: str, maximum: int = 10000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ConfigError(f"{label} must be an integer between 1 and {maximum}.")
    return value


def phrases(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if value is None and allow_empty:
        return ()
    if (not isinstance(value, list) or (not value and not allow_empty)
            or any(not isinstance(s, str) or not s.strip() or '"' in s
                   or any(ord(c) < 32 for c in s) for s in value)):
        raise ConfigError(f"{label} must be a list of nonempty phrases without quotation marks.")
    return tuple(dict.fromkeys(" ".join(s.split()) for s in value))


def _read(path: Path) -> dict:
    example = path.with_name(path.stem + ".example.toml")
    if not path.exists() and example.exists():
        raise ConfigError(f"{path} not found. Copy {example.name} to {path.name} in the same folder and edit it "
                          "(your copy is private: git ignores it).")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}.") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: {exc}") from exc


def _table(data: dict, key: str, label: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{label}: [{key}] must be a table.")
    return value


def _list(data: dict, key: str, label: str) -> list[dict]:
    value = data.get(key)
    if not isinstance(value, list) or not value or any(not isinstance(v, dict) for v in value):
        raise ConfigError(f"{label}: add at least one [[{key}]] entry.")
    return value


def _text(item: dict, key: str, label: str, default: str | None = None) -> str:
    value = item.get(key, default)
    if not isinstance(value, str) or (default is None and not value.strip()):
        raise ConfigError(f"{label}: '{key}' must be text.")
    return value.strip()


def _flag(item: dict, label: str) -> bool:
    value = item.get("enabled", True)
    if not isinstance(value, bool):
        raise ConfigError(f"{label}: enabled must be true or false.")
    return value


def _unique(names: list[str], label: str) -> None:
    seen = set()
    for name in names:
        if name.casefold() in seen:
            raise ConfigError(f"Duplicate {label}: {name}")
        seen.add(name.casefold())


def load_positions(path: Path) -> tuple[dict[str, Track], tuple[Family, ...]]:
    data = _read(path)
    tracks = {}
    for key, item in _table(data, "tracks", path.name).items():
        if not isinstance(item, dict):
            raise ConfigError(f"{path.name}: [tracks.{key}] must be a table.")
        tracks[key] = Track(key, _text(item, "label", f"tracks.{key}"), _text(item, "cv", f"tracks.{key}", ""))
    if not tracks:
        raise ConfigError(f"{path.name}: define at least one [tracks.<id>] table.")
    families = []
    for item in _list(data, "family", path.name):
        fid = _text(item, "id", "family")
        label = f"family {fid}"
        if not re.fullmatch(r"[A-Za-z0-9_]+", fid):
            raise ConfigError(f"{label}: id may contain only letters, digits and _.")
        track = _text(item, "track", label)
        if track not in tracks:
            raise ConfigError(f"{label}: unknown track '{track}'. Known: {', '.join(tracks)}.")
        families.append(Family(
            id=fid, name=_text(item, "name", label, fid) or fid, track=track,
            priority=positive_int(item.get("priority", 2), f"{label}.priority", 9),
            enabled=_flag(item, label), titles=phrases(item.get("titles"), f"{label}.titles"),
            notes=_text(item, "notes", label, ""), evidence=_text(item, "evidence", label, "")))
    _unique([f.id for f in families], "family id")
    return tracks, tuple(families)


def _days(value: object) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("budget.revisit_days must be a list like [2, 4, 8] (days for priority 1, 2, 3...).")
    return tuple(positive_int(v, "budget.revisit_days entry", 365) for v in value)


def load_settings(positions: Path = DEFAULT_POSITIONS, search: Path = DEFAULT_SEARCH) -> Settings:
    tracks, families = load_positions(positions)
    data = _read(search)
    label = search.name
    platforms = []
    for item in _list(data, "platform", label):
        name = _text(item, "name", "platform")
        site = _text(item, "site", f"platform {name}")
        if (not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?(?:/[a-zA-Z0-9_./-]*)?", site)
                or "." not in site.split("/")[0]):
            raise ConfigError(f"platform {name}: site must look like comeet.com/jobs, without https://.")
        platforms.append(Platform(name, site, _flag(item, f"platform {name}")))
    _unique([p.name for p in platforms], "platform name")
    regions = []
    for item in data.get("region", []) or []:
        name = _text(item, "name", "region")
        regions.append(Region(name, _flag(item, f"region {name}"), phrases(item.get("cities"), f"region {name}.cities")))
    _unique([r.name for r in regions], "region name")
    budget = _table(data, "budget", label)
    google = _table(data, "google", label)
    freshness = google.get("freshness", "week")
    if freshness not in FRESHNESS:
        raise ConfigError(f"google.freshness must be one of: {', '.join(FRESHNESS)}.")
    codes = {}
    for key, default in (("country", "il"), ("language", "en")):
        value = google.get(key, default)
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z-]{2,12}", value):
            raise ConfigError(f"google.{key} must be a language/country code.")
        codes[key] = value
    filters = _table(data, "filters", label)
    show_boards = filters.get("show_career_pages", False)
    if not isinstance(show_boards, bool):
        raise ConfigError("filters.show_career_pages must be true or false.")
    allow_paid = budget.get("allow_paid_plan", False)
    if not isinstance(allow_paid, bool):
        raise ConfigError("budget.allow_paid_plan must be true or false.")
    return Settings(
        tracks=tracks, families=families, platforms=tuple(platforms), regions=tuple(regions),
        query_terms=phrases(_table(data, "location", label).get("query_terms"), "location.query_terms"),
        exclude_terms=phrases(filters.get("exclude_terms"), "filters.exclude_terms", allow_empty=True),
        title_exclude=phrases(filters.get("title_exclude"), "filters.title_exclude", allow_empty=True),
        monthly_limit=positive_int(budget.get("monthly_limit", 250), "budget.monthly_limit"),
        per_run=positive_int(budget.get("per_run", 16), "budget.per_run"),
        pages_per_query=positive_int(budget.get("pages_per_query", 1), "budget.pages_per_query", 10),
        max_query_words=positive_int(budget.get("max_query_words", 20), "budget.max_query_words", 200),
        revisit_days=_days(budget.get("revisit_days", [2, 4, 8])),
        per_hour=positive_int(budget.get("per_hour", 50), "budget.per_hour"),
        country=codes["country"], language=codes["language"], freshness=freshness,
        timeout_seconds=positive_int(google.get("timeout_seconds", 30), "google.timeout_seconds", 120),
        show_career_pages=show_boards,
        old_posting_days=positive_int(filters.get("old_posting_days", 60), "filters.old_posting_days", 3650),
        allow_paid_plan=allow_paid)
