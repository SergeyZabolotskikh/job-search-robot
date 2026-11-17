"""Command line entry point: `job-search-robot preview|run|sweep|refresh`."""
from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .collector import publish, run_search
from .common import ROOT, VERSION, ConfigError, utc_now
from .config import DEFAULT_POSITIONS, DEFAULT_SEARCH, FRESHNESS, Settings, load_settings, positive_int
from .localenv import LOCAL_ENV, load_local_env
from .queries import SearchQuery, build_queries, monthly_demand, plan_run
from .query_report import platform_name, write_query_report
from .reports import write_preview
from .serpapi import SerpApiClient
from .store import ResultStore
from .verify import fetch_page, resolve_redirect
from .workbook import ensure_writable, read_notes

DEFAULT_HISTORY = ROOT / "data" / "history.sqlite3"
DEFAULT_RESULTS = ROOT / "results"
COMMANDS = (
    ("preview", "Show the planned searches; no API key, no network, no credits"),
    ("run", "Search with your SerpApi key and add new leads to results/job_leads.xlsx"),
    ("sweep", "Search every enabled query once (page 1) and write a per-query outcome report"),
    ("refresh", "Free: re-read your Status/Note, check new job pages, re-apply the filters, rewrite the workbook"),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Find jobs on hiring platforms using Google results through SerpApi.")
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    for command, text in COMMANDS:
        sub = commands.add_parser(command, help=text)
        sub.add_argument("--positions", type=Path, default=DEFAULT_POSITIONS, help="Role list (TOML)")
        sub.add_argument("--search-config", type=Path, default=DEFAULT_SEARCH, help="Search settings (TOML)")
        sub.add_argument("--output", type=Path, default=DEFAULT_RESULTS, help="Folder for the workbook and reports")
        sub.add_argument("--history", type=Path, default=DEFAULT_HISTORY, help="SQLite memory file")
        sub.add_argument("--family", action="append", help="Only this family id (repeatable; also runs disabled ones)")
        sub.add_argument("--track", action="append", help="Only this track: management or ic (repeatable)")
        sub.add_argument("--max-requests", type=int, help="Lower the search budget for this run")
        sub.add_argument("--freshness", choices=list(FRESHNESS),
                         help="Override google.freshness ('any' = no date filter; sweep default: any)")
        sub.add_argument("--env-file", type=Path, default=LOCAL_ENV, help="Local values file (default local.env.yaml)")
        sub.set_defaults(fetch=fetch_page, resolve=resolve_redirect)  # page checks, Google redirects; tests replace them
        if command == "refresh":
            sub.add_argument("--recheck", action="store_true",
                             help="Open every job page again (finds jobs closed since the last check)")
        if command == "sweep":
            sub.add_argument("--since-hours", type=int, default=12,
                             help="Skip queries searched within this many hours (resume after the hourly limit)")
    return parser


@dataclass
class History:
    month_used: int = 0
    hour_used: int = 0
    last_run: dict[str, str] | None = None
    last_ok: dict[str, str] | None = None


def read_history(path: Path) -> History:
    """Counters and last search time per query; a preview never creates the database."""
    if not path.exists():
        return History(last_run={}, last_ok={})
    store = ResultStore(path)
    try:
        hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        return History(store.searches_this_month(), store.searches_since(hour_ago), store.last_searched(),
                       store.last_searched(ok_only=True))
    finally:
        store.close()


def sweep_plan(queries: list[SearchQuery], history: History, settings: Settings, since_hours: int,
               cap: int | None = None, now: datetime | None = None) -> tuple[list[SearchQuery], int]:
    """Queries without a successful search within `since_hours`, never-searched first; cut to
    the budget. Failed searches don't count, so a rerun retries them. Returns (planned, eligible)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=since_hours)
    last_run = history.last_ok or {}
    eligible = [q for q in queries
                if not last_run.get(q.key) or datetime.fromisoformat(last_run[q.key]) < cutoff]
    budget = min(len(eligible), budget_left(history, settings), cap if cap is not None else len(eligible))
    return plan_run(eligible, budget, last_run, settings.revisit_days, now), len(eligible)


def budget_left(history: History, settings: Settings) -> int:
    return max(0, min(settings.monthly_limit - history.month_used, settings.per_hour - history.hour_used))


def print_plan(queries, planned, settings) -> None:
    position = {q.key: i + 1 for i, q in enumerate(planned)}
    print(f"{'run':>4}  {'pri':>3}  {'words':>5}  {'platform':<11} family / titles")
    for q in queries:
        slot = position.get(q.key, "-")
        flag = "  <-- over word limit" if q.words > settings.max_query_words else ""
        print(f"{slot:>4}  {q.priority:>3}  {q.words:>5}  {platform_name(q.sites[0], settings):<11} "
              f"{q.family}: {', '.join(q.titles)}{flag}")


def api_client(args, settings: Settings) -> SerpApiClient | None:
    load_local_env(args.env_file)  # local.env.yaml; real environment variables win
    key = os.environ.get("SERPAPI_API_KEY", "").strip()
    if not key and sys.stdin.isatty():
        key = getpass.getpass("SerpApi API key (hidden; not saved): ").strip()
    if not key:
        print("A SerpApi API key is required. Put it in local.env.yaml (see local.env.example.yaml), "
              "set SERPAPI_API_KEY, or run interactively to enter it. Preview works without a key.", file=sys.stderr)
        return None
    return SerpApiClient(key, settings.country, settings.language, settings.timeout_seconds)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.positions, args.search_config)
        history_path = args.history.expanduser().resolve()
        output = args.output.expanduser().resolve()
        if args.command == "refresh":
            return refresh(settings, history_path, output / "job_leads.xlsx", args.fetch, args.recheck)
        history = read_history(history_path)
        queries = build_queries(settings, families=args.family, tracks=args.track)
        cap = None if args.max_requests is None else positive_int(args.max_requests, "--max-requests")
        limits = (f"used {history.month_used} of {settings.monthly_limit} this month, "
                  f"{history.hour_used} of {settings.per_hour} in the last hour")
        if args.command == "sweep":
            planned, eligible = sweep_plan(queries, history, settings, args.since_hours, cap)
            pages, freshness = 1, args.freshness or "any"
            print_plan(queries, planned, settings)
            print(f"\nSweep: {eligible} of {len(queries)} queries not searched in the last {args.since_hours} h; "
                  f"this sweep searches {len(planned)} ({limits}). Freshness: {freshness}.")
        else:
            per_run = settings.per_run if cap is None else cap
            budget = min(per_run, budget_left(history, settings))
            planned = plan_run(queries, budget, history.last_run, settings.revisit_days)
            pages, freshness = settings.pages_per_query, args.freshness or settings.freshness
            print_plan(queries, planned, settings)
            demand = monthly_demand(queries, settings.revisit_days, settings.pages_per_query)
            days = "/".join(str(d) for d in settings.revisit_days)
            print(f"\n{len(queries)} queries; this run searches {len(planned)} of them. Per run {per_run}; {limits}. "
                  f"{pages} page(s) per query, freshness: {freshness}.")
            print(f"Full schedule (revisit every {days} days by priority) needs ~{demand} searches/month; "
                  f"limit {settings.monthly_limit}.")
            if demand > settings.monthly_limit:
                print(f"  -> over the limit: queries will be revisited ~{demand / settings.monthly_limit:.1f}x less "
                      "often. Disable platforms/families or merge titles to fit.")
            if len(planned) < len(queries):
                print("Queries marked '-' wait for later runs (most overdue first).")
        if args.command == "preview":
            write_preview(queries, planned, settings, output)
            print(f"Preview saved: {output / 'search_plan.html'} (no API requests made)")
            return 0
        if not planned:
            if args.command == "sweep" and eligible == 0:
                path = write_report(output, queries, history_path, settings, set())
                print(f"Nothing left to sweep. Report: {path}")
                return 0
            print("No searches left in the monthly or hourly limit. Try again later "
                  "(SerpApi free plan: 50 searches per hour).", file=sys.stderr)
            return 1
        workbook = output / "job_leads.xlsx"
        ensure_writable(workbook)  # before asking for the key or spending credits
        client = api_client(args, settings)
        if client is None:
            return 1
        summary = run_search(planned, settings, client, history=history_path, workbook=workbook,
                             pages=pages, budget=len(planned) * pages if args.command == "sweep" else budget,
                             tbs=FRESHNESS[freshness], fetch=args.fetch, resolve=args.resolve)
        print(f'\n{summary["new_results"]} new results this run, {summary.get("new_shown", 0)} of them shown '
              f'in the workbook. Searches used this month: {summary["searches_this_month"]} of '
              f'{settings.monthly_limit}. Status: {summary["status"]}.')
        print_workbook_summary(summary, workbook)
        if args.command == "sweep":
            path = write_report(output, queries, history_path, settings, {q.key for q in planned})
            print(f"Query report: {path}")
            left = eligible - summary["requests_used"]
            if left > 0:
                resume = datetime.now() + timedelta(hours=1)
                print(f"{left} queries not searched yet (hourly/monthly limit). "
                      f"Run `sweep` again after {resume:%H:%M} to finish.")
        if summary["errors"]:
            print(f'{len(summary["errors"])} search error(s). See {workbook.with_name("last_run.json")}.', file=sys.stderr)
        return 130 if summary["status"] == "interrupted" else 2 if summary["status"] == "partial" else 0
    except (ConfigError, OSError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Cancelled.", file=sys.stderr)
        return 130


def print_workbook_summary(summary: dict, workbook: Path) -> None:
    if summary.get("notes_imported"):
        print(f'Saved {summary["notes_imported"]} Status/Note change(s) from the workbook.')
    if summary.get("identities_migrated"):
        print(f'Merged {summary["identities_migrated"]} lead(s) recorded under old link rules.')
    if summary.get("page_checks"):
        print("Job pages checked: " + ", ".join(f"{n} {k}" for k, n in sorted(summary["page_checks"].items())) + ".")
    hidden = summary.get("hidden") or {}
    text = ", ".join(f"{n} {reason}" for reason, n in sorted(hidden.items())) or "none"
    print(f'Workbook shows {summary.get("shown_in_workbook", 0)} leads; hidden: {text}.')
    print(f"Workbook: {workbook}")


def refresh(settings: Settings, history_path: Path, workbook: Path, fetch, recheck: bool = False) -> int:
    """Free: import Status/Note, check unchecked job pages, re-apply hide rules, rewrite the workbook."""
    ensure_writable(workbook)
    store = ResultStore(history_path)
    try:
        if recheck:
            store.forget_checks()
        summary = {"command": "refresh", "started_utc": utc_now(), "identities_migrated": store.migrated,
                   "notes_imported": store.save_notes(read_notes(workbook))}
        publish(store, settings, workbook, summary, set(), fetch)
    finally:
        store.close()
    print_workbook_summary(summary, workbook)
    return 0


def write_report(output: Path, queries: list[SearchQuery], history_path: Path, settings: Settings,
                 swept: set[str]) -> Path:
    store = ResultStore(history_path)
    try:
        outcomes = store.latest_outcomes()
    finally:
        store.close()
    return write_query_report(output, queries, outcomes, settings, swept)
