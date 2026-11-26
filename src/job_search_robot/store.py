"""SQLite memory: every lead ever seen, which search found it, paid searches, and the user's Status/Note."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .common import utc_now
from .queries import SearchQuery
from .urls import canonical_result, in_site, workday_place

DEFAULT_STATUS = "new"


class ResultStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS results (
                identity TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
                snippet TEXT NOT NULL, platform TEXT NOT NULL, company_url_slug TEXT NOT NULL,
                result_type TEXT NOT NULL, first_seen_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL, first_run_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, started_utc TEXT NOT NULL, summary_json TEXT);
            CREATE TABLE IF NOT EXISTS matches (
                run_id TEXT NOT NULL, identity TEXT NOT NULL, query TEXT NOT NULL,
                family_id TEXT NOT NULL, track TEXT NOT NULL,
                PRIMARY KEY (run_id, identity, query));
            -- One row per request sent to a search service: the monthly counter and query rotation.
            CREATE TABLE IF NOT EXISTS searches (
                run_id TEXT NOT NULL, query TEXT NOT NULL, start INTEGER NOT NULL,
                at_utc TEXT NOT NULL, outcome TEXT NOT NULL);
            -- The user's own columns from the workbook; never written by a search.
            CREATE TABLE IF NOT EXISTS leads (
                identity TEXT PRIMARY KEY, status TEXT NOT NULL, note TEXT NOT NULL,
                updated_utc TEXT NOT NULL);
            -- What each successful search returned: the data for learning which queries work.
            CREATE TABLE IF NOT EXISTS query_outcomes (
                run_id TEXT NOT NULL, query TEXT NOT NULL, family_id TEXT NOT NULL, site TEXT NOT NULL,
                start INTEGER NOT NULL, at_utc TEXT NOT NULL, freshness TEXT NOT NULL,
                total_results INTEGER, returned INTEGER NOT NULL, on_platform INTEGER NOT NULL,
                off_platform INTEGER NOT NULL, new_leads INTEGER NOT NULL, jobs INTEGER NOT NULL,
                career_pages INTEGER NOT NULL, israel INTEGER NOT NULL, more_pages INTEGER NOT NULL,
                examples TEXT NOT NULL);
            -- What the job page itself says (free HTTP check, no SerpApi credits).
            CREATE TABLE IF NOT EXISTS page_checks (
                identity TEXT PRIMARY KEY, checked_utc TEXT NOT NULL, status TEXT NOT NULL,
                location TEXT NOT NULL, in_israel INTEGER, work_mode TEXT NOT NULL, posted TEXT NOT NULL,
                final_url TEXT NOT NULL, note TEXT NOT NULL);
            -- Old lead ids replaced when URL rules improved (so old workbook Lead IDs still match).
            CREATE TABLE IF NOT EXISTS identity_aliases (old TEXT PRIMARY KEY, new TEXT NOT NULL);
            -- SerpApi's own numbers for a key, read before and after each run (free request).
            CREATE TABLE IF NOT EXISTS account_checks (
                at_utc TEXT NOT NULL, provider TEXT NOT NULL, account TEXT NOT NULL, plan TEXT NOT NULL,
                monthly_price REAL NOT NULL, month_used INTEGER NOT NULL, plan_left INTEGER NOT NULL,
                extra_credits INTEGER NOT NULL, renewal TEXT NOT NULL);
        """)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(searches)")}
        with self.connection:  # older memory files: their searches stay, with no account ("")
            if "provider" not in columns:
                self.connection.execute("ALTER TABLE searches ADD COLUMN provider TEXT NOT NULL DEFAULT 'serpapi'")
            if "account" not in columns:
                self.connection.execute("ALTER TABLE searches ADD COLUMN account TEXT NOT NULL DEFAULT ''")
        self.migrated = self.migrate_identities()
        self.restore_workday_places()

    # ----------------------------------------------------------------- runs and searches
    def start_run(self, run_id: str) -> None:
        with self.connection:
            self.connection.execute("INSERT INTO runs VALUES (?, ?, NULL)", (run_id, utc_now()))

    def record_search(self, run_id: str, query: SearchQuery, start: int, outcome: str, account: str = "",
                      provider: str = "serpapi") -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO searches (run_id, query, start, at_utc, outcome, provider, account) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", (run_id, query.key, start, utc_now(), outcome, provider, account))

    def save_account(self, account) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO account_checks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (utc_now(), account.provider, account.account_id, account.plan, account.monthly_price,
                 account.month_used, account.plan_left, account.extra_credits, account.renewal))

    @staticmethod
    def _month_start(now: datetime | None) -> str:
        now = now or datetime.now(timezone.utc)
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")

    def searches_this_month(self, now: datetime | None = None, provider: str = "serpapi") -> int:
        """One service's requests since the 1st of the current month (UTC), on any key. Counted
        conservatively: failed or cached searches are not charged, but this counts every attempt."""
        return self.connection.execute("SELECT COUNT(*) FROM searches WHERE at_utc >= ? AND provider = ?",
                                       (self._month_start(now), provider)).fetchone()[0]

    def searches_by_account(self, now: datetime | None = None) -> dict[str, int]:
        """SerpApi requests sent this month per account; "" = sent before accounts were recorded."""
        return {row[0]: row[1] for row in self.connection.execute(
            "SELECT account, COUNT(*) FROM searches WHERE at_utc >= ? AND provider = 'serpapi' GROUP BY account",
            (self._month_start(now),))}

    def estimated_month_used(self, now: datetime | None = None) -> int:
        """For preview (no network): SerpApi's count at its last check this month plus what was sent
        since. Without a check this month, the tool's own count."""
        month_start = self._month_start(now)
        last = self.connection.execute("SELECT at_utc, month_used FROM account_checks WHERE at_utc >= ? "
                                       "AND provider = 'serpapi' ORDER BY at_utc DESC, rowid DESC LIMIT 1",
                                       (month_start,)).fetchone()
        if last is None:
            return self.searches_this_month(now)
        return last["month_used"] + self.connection.execute(
            "SELECT COUNT(*) FROM searches WHERE at_utc > ? AND provider = 'serpapi'", (last["at_utc"],)).fetchone()[0]

    def last_search_at(self, provider: str = "serpapi") -> datetime | None:
        value = self.connection.execute("SELECT MAX(at_utc) FROM searches WHERE provider = ?", (provider,)).fetchone()[0]
        return datetime.fromisoformat(value) if value else None

    def searches_since(self, since: datetime, account: str | None = None, provider: str = "serpapi") -> int:
        """One service's searches. With `account`: that key's, plus those from before accounts were recorded."""
        where, values = "at_utc >= ? AND provider = ?", [since.isoformat(timespec="seconds"), provider]
        if account is not None:
            where, values = where + " AND account IN (?, '')", values + [account]
        return self.connection.execute(f"SELECT COUNT(*) FROM searches WHERE {where}", values).fetchone()[0]

    def record_outcome(self, run_id: str, query: SearchQuery, start: int, freshness: str, outcome: dict) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO query_outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, query.key, query.family_id, ",".join(query.sites), start, utc_now(), freshness,
                 outcome["total_results"], outcome["returned"], outcome["on_platform"], outcome["off_platform"],
                 outcome["new_leads"], outcome["jobs"], outcome["career_pages"], outcome["israel"],
                 int(outcome["more_pages"]), outcome["examples"]))

    def latest_outcomes(self) -> dict[str, dict]:
        """Latest page-1 outcome per query text."""
        rows = self.connection.execute("""SELECT * FROM query_outcomes o WHERE start = 0 AND at_utc =
            (SELECT MAX(at_utc) FROM query_outcomes i WHERE i.query = o.query AND i.start = 0)""").fetchall()
        return {row["query"]: dict(row) for row in rows}

    def last_searched(self, ok_only: bool = False) -> dict[str, str]:
        where = "WHERE outcome = 'ok'" if ok_only else ""
        return {row[0]: row[1] for row in self.connection.execute(
            f"SELECT query, MAX(at_utc) FROM searches {where} GROUP BY query")}

    def finish(self, run_id: str, summary: dict) -> None:
        with self.connection:
            self.connection.execute("UPDATE runs SET summary_json=? WHERE run_id=?", (json.dumps(summary), run_id))

    # ----------------------------------------------------------------- identity upgrades
    def migrate_identities(self) -> int:
        """Re-compute each lead's identity with the current URL rules and merge duplicates.
        The user's Status/Note and the search history move to the new identity."""
        changed = 0
        for record in self.connection.execute("SELECT * FROM results").fetchall():
            result = canonical_result(record["url"])
            if result is None or result["identity"] == record["identity"]:
                continue
            old, new = record["identity"], result["identity"]
            with self.connection:
                target = self.connection.execute("SELECT * FROM results WHERE identity=?", (new,)).fetchone()
                if target:
                    first = min(record, target, key=lambda r: r["first_seen_utc"])
                    self.connection.execute(
                        "UPDATE results SET first_seen_utc=?, first_run_id=?, last_seen_utc=? WHERE identity=?",
                        (first["first_seen_utc"], first["first_run_id"],
                         max(record["last_seen_utc"], target["last_seen_utc"]), new))
                    self.connection.execute("DELETE FROM results WHERE identity=?", (old,))
                else:
                    self.connection.execute(
                        "UPDATE results SET identity=?, url=?, platform=?, company_url_slug=?, result_type=? "
                        "WHERE identity=?", (new, result["url"], result["platform"], result["company_url_slug"],
                                             result["result_type"], old))
                self.connection.execute("UPDATE OR IGNORE matches SET identity=? WHERE identity=?", (new, old))
                self.connection.execute("DELETE FROM matches WHERE identity=?", (old,))
                mine = self.connection.execute("SELECT * FROM leads WHERE identity=?", (old,)).fetchone()
                theirs = self.connection.execute("SELECT * FROM leads WHERE identity=?", (new,)).fetchone()
                if mine and (not theirs or theirs["status"] == DEFAULT_STATUS and not theirs["note"]):
                    self.connection.execute("INSERT OR REPLACE INTO leads VALUES (?, ?, ?, ?)",
                                            (new, mine["status"], mine["note"], mine["updated_utc"]))
                self.connection.execute("DELETE FROM leads WHERE identity=?", (old,))
                self.connection.execute("DELETE FROM page_checks WHERE identity IN (?, ?)", (old, new))
                self.connection.execute("UPDATE identity_aliases SET new=? WHERE new=?", (new, old))
                self.connection.execute("INSERT OR REPLACE INTO identity_aliases VALUES (?, ?)", (old, new))
            changed += 1
        return changed

    def restore_workday_places(self) -> int:
        """A merged Workday job keeps the address stored first. When that one names no location but
        an address merged into it did (kept as an old identity), use that one and read the job again."""
        fixed = 0
        for row in self.connection.execute(
                "SELECT identity, url FROM results WHERE platform='Workday' AND result_type='job'").fetchall():
            if workday_place(row["url"]):
                continue
            for (old,) in self.connection.execute("SELECT old FROM identity_aliases WHERE new=?",
                                                  (row["identity"],)).fetchall():
                result = canonical_result(old)
                if result and result["identity"] == row["identity"] and workday_place(result["url"]):
                    with self.connection:
                        self.connection.execute("UPDATE results SET url=? WHERE identity=?",
                                                (result["url"], row["identity"]))
                        self.connection.execute("DELETE FROM page_checks WHERE identity=?", (row["identity"],))
                    fixed += 1
                    break
        return fixed

    def resolve(self, identity: str) -> str:
        row = self.connection.execute("SELECT new FROM identity_aliases WHERE old=?", (identity,)).fetchone()
        return row["new"] if row else identity

    # ----------------------------------------------------------------- page checks
    def unchecked_jobs(self, limit: int) -> list[sqlite3.Row]:
        """Job leads whose page was never checked, or whose last check failed temporarily (network,
        too many requests, server error); newest first."""
        return self.connection.execute("""SELECT r.identity, r.url, r.platform, r.title, r.snippet FROM results r
            LEFT JOIN page_checks c ON c.identity = r.identity
            WHERE r.result_type = 'job' AND (c.identity IS NULL OR c.status = 'unknown' AND (
                c.note LIKE 'page could not%' OR c.note = 'HTTP 429' OR c.note LIKE 'HTTP 5%'))
            ORDER BY r.first_seen_utc DESC LIMIT ?""", (limit,)).fetchall()

    def jobs_to_recheck(self) -> list[sqlite3.Row]:
        """Every job lead for `refresh --recheck`, never-checked and oldest check first, with the
        status of its earlier check (old_status, None if never checked). Earlier checks are kept
        until replaced, so a job known to be closed or abroad stays hidden during a recheck."""
        return self.connection.execute("""SELECT r.identity, r.url, r.platform, r.title, r.snippet,
                c.status AS old_status FROM results r
            LEFT JOIN page_checks c ON c.identity = r.identity
            WHERE r.result_type = 'job'
            ORDER BY c.checked_utc IS NOT NULL, c.checked_utc, r.first_seen_utc DESC""").fetchall()

    def save_check(self, identity: str, check) -> None:
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO page_checks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    (identity, utc_now(), check.status, check.location,
                                     None if check.in_israel is None else int(check.in_israel),
                                     check.work_mode, check.posted, check.final_url, check.note))

    # ----------------------------------------------------------------- results
    def ingest(self, run_id: str, query: SearchQuery, items: list) -> dict:
        """Store on-platform results. Returns {"kept": [...], "dropped": n, "dropped_examples": [...]};
        each kept item has identity, title, snippet, result_type and new (first time this tool sees it).
        Dropped examples are the first addresses not on the platform (Google's displayed link for an
        unresolved Google redirect), so the sweep report shows what was dropped."""
        kept, dropped, dropped_examples = [], 0, []
        with self.connection:
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("link"), str):
                    continue
                url = item["link"]
                result = canonical_result(url)
                if result is None or not any(in_site(url, site) for site in query.sites):
                    dropped += 1
                    shown = item.get("displayed_link") if "://www.google." in url[:20] else url
                    if len(dropped_examples) < 3:
                        why = f' [{item["google_link_failed"]}]' if item.get("google_link_failed") else ""
                        dropped_examples.append(str(shown or url)[:90] + why)
                    continue
                stored = self.connection.execute("SELECT url FROM results WHERE identity=?",
                                                 (result["identity"],)).fetchone()
                is_new = stored is None
                url = result["url"]
                if stored and result["platform"] == "Workday" and workday_place(stored["url"]):
                    url = stored["url"]   # the job's only location source: keep an address that names one
                elif stored and result["platform"] == "Workday" and workday_place(url):
                    self.connection.execute("DELETE FROM page_checks WHERE identity=?", (result["identity"],))
                now = utc_now()
                title = str(item.get("title") or result["url"])
                snippet = str(item.get("snippet") or "")
                self.connection.execute("""
                    INSERT INTO results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(identity) DO UPDATE SET url=excluded.url, title=excluded.title,
                    snippet=CASE WHEN excluded.snippet != '' THEN excluded.snippet ELSE results.snippet END,
                    last_seen_utc=excluded.last_seen_utc
                """, (result["identity"], url, title, snippet, result["platform"],
                      result["company_url_slug"], result["result_type"], now, now, run_id))
                self.connection.execute("INSERT OR IGNORE INTO matches VALUES (?, ?, ?, ?, ?)",
                                        (run_id, result["identity"], query.text, query.family_id, query.track))
                kept.append({"identity": result["identity"], "title": title, "snippet": snippet,
                             "result_type": result["result_type"], "new": is_new})
        return {"kept": kept, "dropped": dropped, "dropped_examples": dropped_examples}

    def rows(self, run_id: str | None = None) -> list[dict]:
        """Leads found in one run, or every lead ever seen (run_id=None), newest first."""
        if run_id is None:
            raw = self.connection.execute("SELECT * FROM results").fetchall()
        else:
            raw = self.connection.execute("""SELECT * FROM results WHERE identity IN
                (SELECT identity FROM matches WHERE run_id=?)""", (run_id,)).fetchall()
        notes = {r["identity"]: r for r in self.connection.execute("SELECT * FROM leads")}
        checks = {r["identity"]: dict(r) for r in self.connection.execute("SELECT * FROM page_checks")}
        rows = []
        for record in raw:
            matches = self.connection.execute("SELECT DISTINCT family_id, track FROM matches WHERE identity=?",
                                              (record["identity"],)).fetchall()
            row = dict(record)
            first_run = row.pop("first_run_id")
            row["first_run_id"] = first_run
            row["new_to_tool"] = "yes" if run_id is not None and first_run == run_id else "no"
            row["family_ids"] = sorted({m["family_id"] for m in matches})
            row["tracks"] = sorted({m["track"] for m in matches})
            row["role_groups"] = " | ".join(row["family_ids"])
            mine = notes.get(record["identity"])
            row["status"] = mine["status"] if mine else DEFAULT_STATUS
            row["note"] = mine["note"] if mine else ""
            row["check"] = checks.get(record["identity"])
            row["vacancy_status"] = row["check"]["status"] if row["check"] else "not_verified"
            rows.append(row)
        return sorted(rows, key=lambda r: (r["first_seen_utc"], r["result_type"] == "job"), reverse=True)

    # ----------------------------------------------------------------- the user's columns
    def save_notes(self, notes: dict[str, tuple[str, str]]) -> int:
        """Store Status/Note read from the workbook. Returns how many changed."""
        changed = 0
        with self.connection:
            for identity, (status, note) in notes.items():
                identity = self.resolve(identity)
                status = status.strip() or DEFAULT_STATUS
                current = self.connection.execute("SELECT status, note FROM leads WHERE identity=?", (identity,)).fetchone()
                if current and (current["status"], current["note"]) == (status, note):
                    continue
                if not current and status == DEFAULT_STATUS and not note:
                    continue
                self.connection.execute("""INSERT INTO leads VALUES (?, ?, ?, ?) ON CONFLICT(identity)
                    DO UPDATE SET status=excluded.status, note=excluded.note, updated_utc=excluded.updated_utc""",
                                        (identity, status, note, utc_now()))
                changed += 1
        return changed

    def close(self) -> None:
        self.connection.close()
