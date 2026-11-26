"""Command line entry point: `job-search-robot preview|run|sweep|refresh|account`."""
from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .collector import publish, run_search
from .common import ROOT, VERSION, ConfigError, ProviderError, utc_now
from .config import DEFAULT_POSITIONS, DEFAULT_SEARCH, FRESHNESS, Settings, load_settings, positive_int
from .localenv import LOCAL_ENV, load_local_env
from .queries import SearchQuery, build_queries, monthly_demand, plan_run
from .query_report import platform_name, write_query_report
from .reports import write_preview
from .serpapi import Account, SerpApiClient
from .serper import FREE_CREDITS, SerperClient
from .store import ResultStore
from .usage import other_services
from .verify import fetch_page, resolve_redirect
from .workbook import ensure_writable, read_notes

DEFAULT_HISTORY = ROOT / "data" / "history.sqlite3"
DEFAULT_RESULTS = ROOT / "results"
COMMANDS = (
    ("preview", "Show the planned searches; no API key, no network, no credits"),
    ("run", "Search (SerpApi, or Serper with --source serper) and add new leads to results/job_leads.xlsx"),
    ("sweep", "Search every enabled query once (page 1) and write a per-query outcome report"),
    ("refresh", "Free: re-read your Status/Note, check new job pages, re-apply the filters, rewrite the workbook"),
    ("account", "Free: show SerpApi's plan, price, searches used and left, plus Tavily and Serper credits; sends no search"),
)
MORE_SEARCHES = ('To search more now: use another key, or a bigger SerpApi plan with allow_paid_plan and a higher '
                 'monthly_limit (README: "Need more searches?").')
SOURCE_NAMES = {"serpapi": "SerpApi", "serper": "Serper"}
# SerpApi's count trails a search: still old 1 s after it, updated within 25 s (measured 2026-11-22).
COUNT_SETTLE_SECONDS = 60
COUNT_WAITS = (5, 10, 15, 30)


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
        sub.add_argument("--track", action="append", help="Only this track id from positions.toml, e.g. management (repeatable)")
        sub.add_argument("--max-requests", type=int, help="Lower the search budget for this run")
        sub.add_argument("--freshness", choices=list(FRESHNESS),
                         help="Override google.freshness ('any' = no date filter; sweep default: any)")
        sub.add_argument("--env-file", type=Path, default=LOCAL_ENV, help="Local values file (default local.env.yaml)")
        sub.set_defaults(fetch=fetch_page, resolve=resolve_redirect, source="serpapi")  # tests replace fetch, resolve
        if command in ("preview", "run", "sweep"):
            sub.add_argument("--source", choices=list(SOURCE_NAMES), default="serpapi",
                             help="Search service for this command (default serpapi)")
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


def read_history(path: Path, account: Account | None = None, source: str = "serpapi") -> History:
    """Counters for this search service and last search time per query (shared by the services, so a
    query isn't repeated right after another service searched it); a preview never creates the database.
    With a SerpApi `account`, the monthly count is SerpApi's own, so cached searches don't use up the budget.
    Serper reports only a balance, so its monthly count is the tool's."""
    if not path.exists():
        return History(account.month_used if account else 0, last_run={}, last_ok={})
    store = ResultStore(path)
    try:
        hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        if source == "serper":
            month_used = store.searches_this_month(provider="serper")
        else:
            month_used = account.month_used if account else store.estimated_month_used()
        hour_used = store.searches_since(hour_ago, account.account_id if account else None, source)
        return History(month_used, hour_used, store.last_searched(), store.last_searched(ok_only=True))
    finally:
        store.close()


def read_account(client, history_path: Path) -> Account:
    """Ask the search service for its own numbers (free) and remember them."""
    account = client.account()
    remember_account(account, history_path)
    return account


def remember_account(account: Account, history_path: Path) -> None:
    store = ResultStore(history_path)
    try:
        store.save_account(account)
    finally:
        store.close()


def settle_count(history_path: Path, source: str = "serpapi") -> None:
    """Before reading the budget: give the service time to count the last search, so a quick rerun can't overspend."""
    if not history_path.exists():
        return
    store = ResultStore(history_path)
    try:
        last = store.last_search_at(source)
    finally:
        store.close()
    wait = COUNT_SETTLE_SECONDS - (datetime.now(timezone.utc) - last).total_seconds() if last else 0
    if wait > 0:
        print(f"Waiting {wait:.0f} s so {SOURCE_NAMES[source]}'s count includes the last search...", flush=True)
        time.sleep(wait)


def read_account_after(client, history_path: Path, before: Account, searches: int, waits) -> Account | None:
    """Read the service's count until it shows this run's searches. Cached ones never show, so the waits are capped."""
    after = None
    for wait in (0, *waits):
        if wait:
            time.sleep(wait)
        try:
            after = client.account()
        except ProviderError:
            continue
        if before.plan_left - after.plan_left >= searches:
            break
    if after is not None:
        remember_account(after, history_path)
    return after


def paid_plan_refusal(account: Account, settings: Settings) -> str:
    """Why no search may be sent on this key; "" when it is free or a paid plan was allowed."""
    if not account.paid or settings.allow_paid_plan:
        return ""
    what = (f"this Serper key has more than the {FREE_CREDITS:,} free credits, so some were bought"
            if account.provider == "serper" else
            f"this key is on a paid SerpApi plan ({account.plan}, ${account.monthly_price:g} a month)")
    return (f"STOPPED, NO SEARCH SENT: {what}.\n"
            "  To search on it anyway, set allow_paid_plan = true under [budget] in sources/search.toml.")


def show_account(args, settings: Settings, history_path: Path) -> int:
    """Free: the key's plan and SerpApi's count, then what the tool sent per key this month."""
    client = api_client(args, settings)
    if client is None:
        return 1
    account = read_account(client, history_path)
    print("SerpApi account (this check is free and uses no search):")
    print(f"  Plan: {account.plan}, ${account.monthly_price:g} a month")
    print(f"  SerpApi's count: {account.month_used} used, {account.plan_left} left on the plan, "
          f"{account.extra_credits} extra credits")
    if account.renewal:
        print(f"  Plan renews on: {account.renewal}")
    store = ResultStore(history_path)
    try:
        sent = store.searches_by_account()
        serper_sent = store.searches_this_month(provider="serper")
    finally:
        store.close()
    print("Searches the tool sent this month (cached and failed ones included):")
    print(f"  This key: {sent.pop(account.account_id, 0)}")
    if "" in sent:
        print(f"  Before keys were recorded: {sent.pop('')}")
    if sent:
        print(f"  Other keys: {sum(sent.values())}")
    print("Other search services (this check sends no search):")
    for line in other_services(settings.timeout_seconds):
        print(f"  {line}")
    print(f"  The tool sent {serper_sent} Serper searches this month (run or sweep with --source serper).")
    refusal = paid_plan_refusal(account, settings)
    if refusal:
        print(f"\n{refusal}", file=sys.stderr)
        return 1
    if account.monthly_price > 0:
        print("\nPAID PLAN: searches are allowed because allow_paid_plan = true.")
    else:
        print(f"\nFree plan: run and sweep stop at the {account.plan_left} searches left and never use extra credits.")
    return 0


def print_charge(before: Account, after: Account | None, sent: int) -> None:
    name = SOURCE_NAMES[before.provider]
    if after is None:
        print(f"Could not read {name}'s count after this run. Check it with the `account` command.")
        return
    counted = max(0, before.plan_left - after.plan_left)
    rest = " (the rest were cached or failed, which is free, or its count is still catching up)" if counted < sent else ""
    left = "Credits left" if before.provider == "serper" else "Left on the plan"
    print(f"{name} counted {counted} of the {sent} searches sent{rest}. {left}: {after.plan_left}.")


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


def limit_notice(month_used: int, hour_used: int, settings: Settings, wanted: int, done: int, *,
                 after: bool = False, now: datetime | None = None, restart: str = "") -> str:
    """What to tell the user when a search limit stops or shortens a run; "" when none applies.
    `wanted` searches were asked for and `done` were made (after=True) or can be made (after=False).
    `restart` is the plan's renewal date, when SerpApi gave one."""
    now = now or datetime.now(timezone.utc)
    limit = settings.monthly_limit
    month_left, hour_left = limit - month_used, settings.per_hour - hour_used
    what = f"This run searched {done} of {wanted}." if after else f"The next run can search {done} of {wanted}."
    if month_left <= 0:
        restart = restart or f"{(now.replace(day=1) + timedelta(days=32)).replace(day=1):%Y-%m-%d}"
        return (f"MONTHLY SEARCH LIMIT REACHED: {month_used} of {limit} searches used. {what}\n"
                f"  The count restarts on {restart}. {MORE_SEARCHES}")
    if done < wanted and not after and month_left <= done:
        return f"MONTHLY SEARCH LIMIT NEAR: only {month_left} of {limit} searches left. {what}\n  {MORE_SEARCHES}"
    if done < wanted and hour_left <= (0 if after else done):
        return (f"HOURLY SEARCH LIMIT: {hour_used} of {settings.per_hour} used in the last hour. {what}\n"
                "  Run again in up to an hour for the rest.")
    if after and month_left < wanted:
        return (f"MONTHLY SEARCH LIMIT NEAR: only {month_left} of {limit} searches left, "
                f"so the next run will be shorter.\n  {MORE_SEARCHES}")
    return ""


def print_plan(queries, planned, settings) -> None:
    position = {q.key: i + 1 for i, q in enumerate(planned)}
    print(f"{'run':>4}  {'pri':>3}  {'words':>5}  {'platform':<11} family / titles")
    for q in queries:
        slot = position.get(q.key, "-")
        flag = "  <-- over word limit" if q.words > settings.max_query_words else ""
        print(f"{slot:>4}  {q.priority:>3}  {q.words:>5}  {platform_name(q.sites[0], settings):<11} "
              f"{q.family}: {', '.join(q.titles)}{flag}")


def api_client(args, settings: Settings) -> SerpApiClient | SerperClient | None:
    load_local_env(args.env_file)  # local.env.yaml; real environment variables win
    if args.source == "serper":
        key = os.environ.get("SERPER_API_KEY", "").strip()
        if not key:
            print("A Serper API key is required. Add SERPER_API_KEY to local.env.yaml "
                  "(see local.env.example.yaml) or set it as an environment variable.", file=sys.stderr)
            return None
        return SerperClient(key, settings.country, settings.language, settings.timeout_seconds)
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
        if args.command == "account":
            return show_account(args, settings, history_path)
        workbook = output / "job_leads.xlsx"
        source, client, account = args.source, None, None
        name = SOURCE_NAMES[source]
        if args.command != "preview":  # before planning: the service's own numbers set the budget
            ensure_writable(workbook)  # before asking for the key or spending credits
            client = api_client(args, settings)
            if client is None:
                return 1
            settle_count(history_path, source)
            account = read_account(client, history_path)
            refusal = paid_plan_refusal(account, settings)
            if refusal:
                print(refusal, file=sys.stderr)
                return 1
            if source == "serper":
                if account.plan_left <= 0:
                    print("STOPPED, NO SEARCH SENT: this Serper key has no credits left.", file=sys.stderr)
                    return 1
                print(f"Serper: {account.plan_left} credits left.")
            else:
                print(f"SerpApi: {account.plan}, ${account.monthly_price:g} a month, {account.month_used} used, "
                      f"{account.plan_left} left.")
        restart = account.renewal if account else ""
        history = read_history(history_path, account, source)
        if account:  # never past what the plan has left, so extra credits are not used
            settings = replace(settings, monthly_limit=min(settings.monthly_limit,
                                                           history.month_used + account.plan_left))
        queries = build_queries(settings, families=args.family, tracks=args.track)
        cap = None if args.max_requests is None else positive_int(args.max_requests, "--max-requests")
        limits = (f"used {history.month_used} of {settings.monthly_limit} this month, "
                  f"{history.hour_used} of {settings.per_hour} in the last hour")
        if args.command == "sweep":
            planned, eligible = sweep_plan(queries, history, settings, args.since_hours, cap)
            pages, freshness = 1, args.freshness or "any"
            wanted, can_search = (eligible if cap is None else min(eligible, cap)), len(planned)
            print_plan(queries, planned, settings)
            print(f"\nSweep: {eligible} of {len(queries)} queries not searched in the last {args.since_hours} h; "
                  f"this sweep searches {len(planned)} ({limits}). Freshness: {freshness}.")
        else:
            per_run = settings.per_run if cap is None else cap
            budget = min(per_run, budget_left(history, settings))
            planned = plan_run(queries, budget, history.last_run, settings.revisit_days)
            pages, freshness = settings.pages_per_query, args.freshness or settings.freshness
            wanted = min(per_run, len(queries) * pages)
            can_search = min(budget, wanted)
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
            notice = limit_notice(history.month_used, history.hour_used, settings, wanted, can_search)
            if notice:
                print(f"\n{notice}")
            return 0
        if not planned:
            if args.command == "sweep" and eligible == 0:
                path = write_report(output, queries, history_path, settings, set())
                print(f"Nothing left to sweep. Report: {path}")
                return 0
            print(limit_notice(history.month_used, history.hour_used, settings, wanted, 0, after=True,
                               restart=restart), file=sys.stderr)
            return 1
        summary = run_search(planned, settings, client, history=history_path, workbook=workbook,
                             pages=pages, budget=len(planned) * pages if args.command == "sweep" else budget,
                             tbs=FRESHNESS[freshness], fetch=args.fetch, resolve=args.resolve,
                             account_id=account.account_id, provider=source)
        made = summary["requests_used"] - len(summary["errors"])
        print(f"\nReading {name}'s count (this can take up to a minute)...", flush=True)
        after = read_account_after(client, history_path, account, made,
                                   () if summary["status"] == "interrupted" else COUNT_WAITS)
        used_now = history.month_used + (max(0, account.plan_left - after.plan_left) if after
                                         else summary["requests_used"])
        print(f'\n{summary["new_results"]} new results this run, {summary.get("new_shown", 0)} of them shown '
              f'in the workbook. Searches used this month: {used_now} of '
              f'{settings.monthly_limit}. Status: {summary["status"]}.')
        print_charge(account, after, summary["requests_used"])
        print(f'{summary["searches_on_platform"]} of {made} searches reached a job platform.')
        print_workbook_summary(summary, workbook)
        if args.command == "sweep":
            path = write_report(output, queries, history_path, settings, {q.key for q in planned})
            print(f"Query report: {path}")
            left = eligible - summary["requests_used"]
            if left > 0 and used_now < settings.monthly_limit and not summary.get("provider_limit"):
                resume = datetime.now() + timedelta(hours=1)
                print(f"{left} queries not searched yet (hourly limit). "
                      f"Run `sweep` again after {resume:%H:%M} to finish.")
        if summary["errors"]:
            print(f'{len(summary["errors"])} search error(s). See {workbook.with_name("last_run.json")}.', file=sys.stderr)
        if summary.get("provider_limit"):
            notice = (f"{name.upper()} SEARCH LIMIT: {name} refused the search: this account has no searches left, "
                      f"or hit a rate limit. This run made {made} of {wanted} searches.\n"
                      f"  Check your {name} dashboard. {MORE_SEARCHES}")
        else:
            notice = limit_notice(used_now, history.hour_used + summary["requests_used"],
                                  settings, wanted, summary["requests_used"], after=True, restart=restart)
        if notice:
            print(f"\n{notice}")
        return 130 if summary["status"] == "interrupted" else 2 if summary["status"] == "partial" else 0
    except (ConfigError, ProviderError, OSError, sqlite3.Error) as exc:
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
    links = summary.get("google_links") or {}
    if links.get("retried") or links.get("failed"):
        failed = "; ".join(f"{why}: {n}" for why, n in sorted(links.get("failed", {}).items()))
        print(f'Google redirect links: {links.get("retried", 0)} tried again, {links.get("followed_on_retry", 0)} '
              f'followed then' + (f'; not followed ({failed}).' if failed else '.'))
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
        summary = {"command": "refresh", "started_utc": utc_now(), "identities_migrated": store.migrated,
                   "notes_imported": store.save_notes(read_notes(workbook))}
        publish(store, settings, workbook, summary, set(), fetch, recheck=recheck)
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
