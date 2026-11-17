"""Per-query outcome report: results/query_report_<date>_<time>.xlsx (a new file each time).

One row per current query with what its latest page-1 search returned, a verdict, a Google
link for a manual check, and an empty "Your check" column. Two summary sheets: by platform
and by role family.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from .config import FRESHNESS, Settings
from .queries import SearchQuery
from .workbook import _save_atomic, set_text

QUERY_COLUMNS = [  # (header, width)
    ("Priority", 8), ("Track", 11), ("Family", 24), ("Platform", 18), ("Titles", 45), ("Words", 7),
    ("Google link", 12), ("Searched at", 17), ("Freshness", 9), ("Verdict", 26), ("Google total", 12),
    ("Returned", 9), ("On platform", 11), ("Off platform", 11), ("New leads", 10), ("Jobs", 6),
    ("Career pages", 12), ("Mentions Israel", 14), ("More pages", 10), ("Examples", 70), ("Your check", 40),
]
FRESHNESS_NAME = {code or "any": name for name, code in FRESHNESS.items()}
SUMMARY_COLUMNS = ["Queries", "Searched", "Returned", "On platform", "Off platform", "New leads",
                   "Jobs", "Career pages", "Mentions Israel", "Site ignored", "No results"]


def verdict(outcome: dict | None) -> str:
    if outcome is None:
        return "not searched yet"
    if outcome["returned"] == 0:
        return "no results"
    if outcome["on_platform"] == 0:
        return "site: ignored (all off-platform)"
    text = "mixed (some off-platform)" if outcome["off_platform"] else "ok"
    return text + (", more pages" if outcome["more_pages"] else "")


def platform_name(site: str, settings: Settings) -> str:
    return next((p.name for p in settings.platforms if p.site == site), site)


def _header(sheet, columns: list[tuple[str, int]]) -> None:
    sheet.append([name for name, _ in columns])
    for index, (_, width) in enumerate(columns, start=1):
        cell = sheet.cell(1, index)
        cell.font = Font(bold=True)
        sheet.column_dimensions[cell.column_letter].width = width
    sheet.freeze_panes = "A2"


def write_query_report(output: Path, queries: list[SearchQuery], outcomes: dict[str, dict],
                       settings: Settings, swept: set[str]) -> Path:
    book = Workbook()
    sheet = book.active
    sheet.title = "Queries"
    _header(sheet, QUERY_COLUMNS)
    totals: dict[tuple[str, str], dict] = {}
    for r, q in enumerate(queries, start=2):
        o = outcomes.get(q.key)
        site = q.sites[0]
        tbs = "" if o is None or o["freshness"] == "any" else o["freshness"]
        values = [q.priority, q.track, q.family, platform_name(site, settings), ", ".join(q.titles), q.words,
                  "open", (f'{datetime.fromisoformat(o["at_utc"]).astimezone():%Y-%m-%d %H:%M}' + (" *" if q.key in swept else "")) if o else "",
                  FRESHNESS_NAME.get(o["freshness"], o["freshness"]) if o else "", verdict(o)]
        values += [o[k] for k in ("total_results", "returned", "on_platform", "off_platform", "new_leads", "jobs",
                                  "career_pages", "israel")] if o else [None] * 8
        values += ["yes" if o and o["more_pages"] else "", o["examples"] if o else "", ""]
        for c, value in enumerate(values, start=1):
            set_text(sheet.cell(r, c), value)
        link = sheet.cell(r, 7)
        link.hyperlink = q.google_url(settings.country, settings.language, tbs)
        link.font = Font(color="0563C1", underline="single")
        for key in (("platform", platform_name(site, settings)), ("family", q.family)):
            bucket = totals.setdefault(key, {name: 0 for name in SUMMARY_COLUMNS})
            bucket["Queries"] += 1
            if o:
                bucket["Searched"] += 1
                for name, field in (("Returned", "returned"), ("On platform", "on_platform"),
                                    ("Off platform", "off_platform"), ("New leads", "new_leads"), ("Jobs", "jobs"),
                                    ("Career pages", "career_pages"), ("Mentions Israel", "israel")):
                    bucket[name] += o[field]
                bucket["Site ignored"] += o["returned"] > 0 and o["on_platform"] == 0
                bucket["No results"] += o["returned"] == 0
    sheet.auto_filter.ref = sheet.dimensions
    for kind, title in (("platform", "By platform"), ("family", "By family")):
        summary = book.create_sheet(title)
        _header(summary, [(kind.capitalize(), 30)] + [(name, 13) for name in SUMMARY_COLUMNS])
        for (k, name), bucket in totals.items():
            if k == kind:
                summary.append([name] + [bucket[c] for c in SUMMARY_COLUMNS])
    notes = book.create_sheet("How to read")
    for line in (
        "One row per query; values come from its latest page-1 search. '*' in 'Searched at' = this sweep.",
        "On platform = results on the query's hiring platform (kept). Off platform = Google ignored site: (dropped).",
        "New leads = first time this tool saw the result. Mentions Israel = title/snippet names Israel or an Israeli city.",
        "Google link = the same query in Google with the same country/language (and date filter). Results differ per",
        "person and location, so expect similar, not identical results. Write what you see in 'Your check'.",
        "This file is never overwritten: each sweep writes a new one.",
    ):
        notes.append([line])
    notes.column_dimensions["A"].width = 120
    path = output / f"query_report_{datetime.now():%Y-%m-%d_%H%M}.xlsx"
    if path.exists():
        path = output / f"query_report_{datetime.now():%Y-%m-%d_%H%M%S}.xlsx"
    _save_atomic(book, path)
    return path
