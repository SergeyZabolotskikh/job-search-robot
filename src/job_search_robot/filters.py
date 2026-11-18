"""Which leads the workbook shows. Hidden leads stay in memory, so they never return as "new".

Rules, in order (decided with the user 2026-11-10, extended 2026-11-17):
1. A lead the user has marked (Status other than "new") is always shown.
2. Title contains a phrase from `[filters] title_exclude` (e.g. hardware, sales, ML): hidden.
3. A company careers page (a board, not one job): hidden unless `show_career_pages = true`.
4. The job page says the job is closed: hidden.
5. The job page states a location and none of it is in Israel: hidden.
6. Duplicates: same company and same title once location tags are removed (text in parentheses
   counts unless it only names a place or work mode), or titles that differ
   only by a leading level word (Staff / Principal ...) with the same page location and posting
   date -> only the earliest is shown. A lead the user marked "duplicate" never counts as the
   original. A company board page is hidden when another lead of that company is shown.
"""
from __future__ import annotations

import re
from collections import Counter

from .config import Settings
from .store import DEFAULT_STATUS

LOCATION_TAG = re.compile(
    r"\b(remote|hybrid|on-?site|can|canada|usa?|united states|uk|united kingdom|emea|apac|latam|americas|"
    r"europe|israel|tel aviv|london|new york|nyc|berlin|paris|amsterdam|dublin|singapore|sydney|india|"
    r"bangalore|bengaluru|poland|warsaw|germany|france|spain|portugal|netherlands|ireland|australia|"
    r"brazil|mexico|japan|tokyo)\b", re.I)
LEVEL_WORD = re.compile(r"^(staff|principal|senior|sr|lead)\s+")


def excluded_title(title: str, settings: Settings) -> str | None:
    for phrase in settings.title_exclude:
        if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", title, re.I):
            return phrase
    return None


def is_board(row: dict) -> bool:
    return row["result_type"] == "career_page" or bool(re.match(r"\s*jobs at\b", row["title"], re.I))


def _place_only(match: re.Match) -> str:
    """'(Remote)', '(Tel Aviv / Hybrid)' only name a place or work mode: dropped. '(Platform)' or
    '(Data team)' tell two jobs apart: kept (dropping them hid separate jobs as duplicates)."""
    parts = [p.strip() for p in re.split(r"[,/;|&]|\s-\s", match.group(1)) if p.strip()]
    return " " if parts and all(LOCATION_TAG.fullmatch(p) for p in parts) else f" {match.group(1)} "


def title_key(row: dict) -> str:
    """Comparable job title: no 'Job Application for', no '@ Company', no location tags."""
    title = row["title"].lower()
    title = re.sub(r"^\s*(job application for|jobs at)\s+", "", title)
    title = re.sub(r"\s+(@|at)\s+[^|,(-]+(\s*-\s*comeet)?\s*$", "", title)
    title = re.sub(r"\s*-\s*comeet\s*$", "", title)
    title = re.sub(r"\(([^)]*)\)", _place_only, title)
    parts = [p for p in re.split(r"\s+[-–|]\s+|,\s*", title) if p and not LOCATION_TAG.fullmatch(p.strip())]
    kept = [p for p in parts if not (LOCATION_TAG.search(p) and len(p.split()) <= 3)] or parts
    return re.sub(r"[^a-z0-9&]+", " ", " ".join(kept)).strip()


def level_key(row: dict) -> tuple[str, str, str] | None:
    """One job posted at two levels (Staff / Principal ...): same title rest, page location and date."""
    check = row.get("check") or {}
    if not check.get("location") or not check.get("posted"):
        return None
    return LEVEL_WORD.sub("", title_key(row)), check["location"].lower(), check["posted"]


def hide_reason(row: dict, settings: Settings) -> str | None:
    if row["status"] != DEFAULT_STATUS:
        return None
    if excluded_title(row["title"], settings):
        return "excluded title"
    if row["result_type"] == "career_page" and not settings.show_career_pages:
        return "career page"
    check = row.get("check")
    if check and check["status"] == "closed":
        return "closed"
    if check and check["in_israel"] == 0:
        return "abroad"
    return None


def split_visible(rows: list[dict], settings: Settings) -> tuple[list[dict], Counter]:
    """Return (rows to show, hidden counts by reason). Input order is kept for shown rows."""
    hidden: Counter = Counter()
    candidates = []
    for row in rows:
        reason = hide_reason(row, settings)
        if reason:
            hidden[reason] += 1
        else:
            candidates.append(row)
    # Duplicates: decide oldest first, so the first-seen copy wins; marked leads always win.
    keep: set[str] = set()
    seen: set[tuple] = set()
    by_company: Counter = Counter()
    ordered = sorted(candidates, key=lambda r: (r["status"] == DEFAULT_STATUS, r["first_seen_utc"]))
    for row in ordered:
        company = row["company_url_slug"].lower()
        if is_board(row):
            continue
        keys = {(company, "title", title_key(row))}
        level = level_key(row)
        if level:
            keys.add((company, "level") + level)
        if row["status"] == DEFAULT_STATUS and company and keys & seen:
            hidden["duplicate"] += 1
            continue
        if row["status"] != "duplicate":   # the user's duplicate mark: shown, but never the original
            seen |= keys
        by_company[company] += 1
        keep.add(row["identity"])
    boards_seen: set[str] = set()
    for row in ordered:
        if not is_board(row):
            continue
        company = row["company_url_slug"].lower()
        if row["status"] == DEFAULT_STATUS and company and (by_company[company] or company in boards_seen):
            hidden["duplicate"] += 1
            continue
        boards_seen.add(company)
        keep.add(row["identity"])
    return [r for r in rows if r["identity"] in keep], hidden
