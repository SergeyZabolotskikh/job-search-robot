"""Preview outputs: search_plan.csv and search_plan.html."""
from __future__ import annotations

import csv
import html
import io
from pathlib import Path

from .common import atomic_text
from .queries import SearchQuery

def csv_text(fields: list[str], rows: list[dict]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        safe = {}
        for field in fields:
            value = str(row.get(field, ""))
            # Search titles/snippets may be interpreted as formulas by spreadsheet apps.
            safe[field] = "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value
        writer.writerow(safe)
    return stream.getvalue()


STYLE = """body{font:16px/1.5 system-ui,sans-serif;max-width:1200px;margin:36px auto;padding:0 22px;color:#202937;background:#f7f9fc}h1{font-size:28px}table{width:100%;border-collapse:collapse;background:white}th,td{text-align:left;vertical-align:top;padding:12px;border-bottom:1px solid #dce3ed}th{background:#eaf0f8}a{color:#0756af}small,.muted{color:#596779}code{overflow-wrap:anywhere}details{margin-top:8px}.badge{background:#dcefe4;color:#18592f;padding:2px 6px;border-radius:4px}td:first-child{min-width:240px}.note{background:white;border-left:4px solid #2371bc;padding:12px 16px}.summary{margin:18px 0}ul{padding-left:22px}"""


def html_page(title: str, body: str) -> str:
    return f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title><style>{STYLE}</style><body>{body}</body></html>'


def write_preview(queries: list[SearchQuery], planned: list[SearchQuery], settings, output: Path) -> None:
    """Write the search plan (CSV + HTML). `planned` is the subset/order the next run would use."""
    position = {q.key: i + 1 for i, q in enumerate(planned)}
    rows = [{"next_run": position.get(q.key, "rotates"), "family": q.family, "track": q.track,
             "priority": q.priority, "words": q.words, "query": q.text, "google_url": q.google_url(settings.country, settings.language, settings.tbs)}
            for q in queries]
    fields = ["next_run", "family", "track", "priority", "words", "query", "google_url"]
    atomic_text(output / "search_plan.csv", csv_text(fields, rows), "utf-8-sig")
    page2 = max(0, min(len(planned) * (settings.pages_per_query - 1), settings.per_run - len(planned)))
    body = (f'<h1>Job search plan</h1><p>{len(queries)} queries. Next run: {len(planned)} first pages'
            f' + up to {page2} second pages = at most {len(planned) + page2} of {settings.per_run} searches per run'
            f' (monthly limit {settings.monthly_limit}). Word limit per query: {settings.max_query_words}.'
            f' Freshness: {html.escape(settings.freshness)}.</p>'
            '<p class="note">Preview only. No API requests have been made. Each link opens the same search in Google'
            ' (same country, language and date filter), free of charge.</p>'
            '<table><tr><th>Next run</th><th>Family</th><th>Google query</th></tr>')
    for row in rows:
        warn = ' <strong>over limit</strong>' if row["words"] > settings.max_query_words else ""
        body += (f'<tr><td>{html.escape(str(row["next_run"]))}</td><td>{html.escape(row["family"])}<br>'
                 f'<small>{html.escape(row["track"])} · priority {row["priority"]} · {row["words"]} words{warn}</small></td>'
                 f'<td><a href="{html.escape(row["google_url"], quote=True)}" target="_blank" rel="noopener noreferrer">Open in Google</a>'
                 f'<br><code>{html.escape(row["query"])}</code></td></tr>')
    atomic_text(output / "search_plan.html", html_page("Job search plan", body + "</table>"))
