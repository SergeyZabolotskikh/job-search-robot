"""One run: import the user's notes, search breadth-first within budget, rewrite the workbook."""
from __future__ import annotations

import json
import re
import time
import uuid
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .common import ProviderError, atomic_text, utc_now
from .config import Settings
from .filters import split_visible
from .queries import SearchQuery
from .serpapi import next_offset
from .store import ResultStore
from .urls import in_site
from .verify import check_page, fetch_page, resolve_redirect
from .workbook import ensure_writable, mentions_israel, read_notes, write_workbook

PAGE_PAUSE_SECONDS = 1.0
GOOGLE_REDIRECT = re.compile(r"^https?://(www\.)?google\.[a-z.]+/(goto|url)\?", re.I)


def shown_address(item: dict) -> str:
    """Google's displayed link ("https://www.comeet.com › jobs › acme") as a URL-like string."""
    shown = item.get("displayed_link")
    if not isinstance(shown, str) or not shown.strip():
        return ""
    shown = re.sub(r"\s*›\s*", "/", shown.strip())
    return shown if "://" in shown else "https://" + shown


def unwrap_links(results: list, query: SearchQuery, resolve) -> list:
    """SerpApi often returns a Google redirect (google.com/goto?url=...) instead of the page address.
    For results that Google shows on the query's platform, ask Google once where the link leads
    (plain HTTP, no SerpApi credits). Others stay as they are and are dropped as off-platform."""
    if resolve is None:
        return results
    unwrapped, asked = [], 0
    for item in results:
        link = item.get("link") if isinstance(item, dict) else None
        if (isinstance(link, str) and GOOGLE_REDIRECT.match(link)
                and any(in_site(shown_address(item), site) for site in query.sites)):
            if resolve is resolve_redirect and asked:
                time.sleep(PAGE_PAUSE_SECONDS)  # be polite to Google
            asked += 1
            real = resolve(link)
            if real and not GOOGLE_REDIRECT.match(real):
                item = {**item, "link": real}
        unwrapped.append(item)
    return unwrapped


def describe(payload: dict, results: list, ingested: dict, settings: Settings, more_pages: bool) -> dict:
    """What one search returned, for the query outcome report."""
    kept = ingested["kept"]
    total = payload.get("search_information", {}).get("total_results") if isinstance(
        payload.get("search_information"), dict) else None
    examples = " | ".join(k["title"] for k in kept[:3])
    if ingested.get("dropped_examples"):
        examples += ("; " if examples else "") + "not on platform: " + " | ".join(ingested["dropped_examples"][:3])
    return {"total_results": total if isinstance(total, int) else None,
            "returned": len(results), "on_platform": len(kept), "off_platform": ingested["dropped"],
            "new_leads": sum(k["new"] for k in kept),
            "jobs": sum(k["result_type"] == "job" for k in kept),
            "career_pages": sum(k["result_type"] == "career_page" for k in kept),
            "israel": sum(mentions_israel(f'{k["title"]} {k["snippet"]}', settings) for k in kept),
            "more_pages": more_pages,
            "examples": examples}


def check_pages(store: ResultStore, settings: Settings, fetch, limit: int = 80) -> dict:
    """Open each unchecked job page once (plain HTTP, no SerpApi credits)."""
    todo = store.unchecked_jobs(limit)
    counts: Counter = Counter()
    last_host = ""
    for index, row in enumerate(todo, start=1):
        host = urlsplit(row["url"]).hostname or ""
        if fetch is fetch_page and host == last_host:
            time.sleep(PAGE_PAUSE_SECONDS)  # be polite: one page per second per site
        last_host = host
        check = check_page(row["url"], row["platform"], settings, fetch)
        store.save_check(row["identity"], check)
        counts[check.status] += 1
        where = check.location or check.note
        print(f"[page {index}/{len(todo)}] {check.status:<7} {where[:60]}", flush=True)
    return dict(counts)


def publish(store: ResultStore, settings: Settings, workbook: Path, summary: dict, new_ids: set[str],
            fetch=None) -> None:
    """Check new job pages (if `fetch`), apply the hide rules, rewrite the workbook and last_run.json."""
    if fetch is not None:
        summary["page_checks"] = check_pages(store, settings, fetch)
    shown, hidden = split_visible(store.rows(), settings)
    summary.update({"shown_in_workbook": len(shown), "hidden": dict(hidden),
                    "new_shown": sum(r["identity"] in new_ids for r in shown)})
    write_workbook(workbook, shown, settings, new_ids)
    atomic_text(workbook.with_name("last_run.json"), json.dumps(summary, indent=2, ensure_ascii=False) + "\n")


def run_search(queries: list[SearchQuery], settings: Settings, client, *, history: Path, workbook: Path,
               pages: int, budget: int, tbs: str = "", fetch=None, resolve=None) -> dict:
    """`fetch` opens job pages, `resolve` follows Google redirect links; None = skip (tests pass fakes)."""
    ensure_writable(workbook)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    store = ResultStore(history)
    summary = {"run_id": run_id, "started_utc": utc_now(), "status": "running",
               "requests_used": 0, "request_budget": budget, "pages_per_query": pages, "freshness": tbs,
               "errors": [], "queries": [{"query": q.text, "family": q.family, "status": "not_started",
                                          "pages_fetched": 0, "results": 0} for q in queries]}
    # Breadth first: all queries get page one before any query gets page two.
    pending = deque((index, 0) for index in range(len(queries)))
    interrupted, fatal = False, False
    try:
        summary["notes_imported"] = store.save_notes(read_notes(workbook))
        store.start_run(run_id)
        while pending and summary["requests_used"] < budget:
            index, start = pending.popleft()
            query, state = queries[index], summary["queries"][index]
            summary["requests_used"] += 1
            state["status"] = "in_progress"
            print(f'[{summary["requests_used"]}/{budget}] {query.family} / page {state["pages_fetched"] + 1}', flush=True)
            try:
                payload = client.search(query.text, start, tbs=tbs)
            except ProviderError as exc:
                store.record_search(run_id, query, start, "error")
                state["status"] = "error"
                summary["errors"].append({"query": query.text, "message": str(exc)})
                if exc.fatal:
                    fatal = True
                    break
                continue
            store.record_search(run_id, query, start, "ok")
            results = unwrap_links(payload.get("organic_results", []), query, resolve)
            ingested = store.ingest(run_id, query, results)
            state["results"] += len(ingested["kept"])
            state["pages_fetched"] += 1
            next_start = next_offset(payload, start) if results else None
            store.record_outcome(run_id, query, start, tbs or "any",
                                 describe(payload, results, ingested, settings, next_start is not None))
            if next_start is None:
                state["status"] = "no_more_pages_returned"
            elif state["pages_fetched"] >= pages:
                state["status"] = "page_limit"
            else:
                state["status"] = "pending_next_page"
                pending.append((index, next_start))
    except KeyboardInterrupt:
        interrupted = True
    finally:
        remaining_status = "interrupted" if interrupted else "stopped_after_provider_error" if fatal else "request_budget"
        for state in summary["queries"]:
            if state["status"] in ("not_started", "pending_next_page", "in_progress"):
                state["status"] = remaining_status
        summary["status"] = ("interrupted" if interrupted else "partial" if summary["errors"] or pending
                             else "page_limited" if any(q["status"] == "page_limit" for q in summary["queries"]) else "finished")
        try:
            run_rows = store.rows(run_id)
            new_ids = {r["identity"] for r in run_rows if r["new_to_tool"] == "yes"}
            summary.update({"finished_utc": utc_now(), "unique_results": len(run_rows),
                            "new_results": len(new_ids), "job_links": sum(r["result_type"] == "job" for r in run_rows),
                            "searches_this_month": store.searches_this_month(),
                            "monthly_limit": settings.monthly_limit})
            store.finish(run_id, summary)
            publish(store, settings, workbook, summary, new_ids, None if interrupted else fetch)
        finally:
            store.close()
    return summary
