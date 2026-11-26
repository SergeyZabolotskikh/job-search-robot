"""Offline behavioral tests ported from the prototype: no API key or live requests are used."""
import contextlib
import csv
import http.client
import io
import json
import os
import sqlite3
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from openpyxl import load_workbook

from job_search_robot import cli, collector, common, config, queries, reports, serpapi, serper, store, urls, usage, workbook, localenv, filters, verify

POSITIONS = Path(__file__).parent / "fixtures" / "positions.toml"
SEARCH = Path(__file__).parent / "fixtures" / "search.toml"
# One namespace with the prototype's public names, so the ported tests read unchanged.
app = types.SimpleNamespace(**{name: getattr(module, name)
                               for module in (common, config, queries, urls, store, reports, serpapi, workbook, localenv, filters, verify, collector, cli)
                               for name in dir(module) if not name.startswith("_")})


def setUpModule():
    """No test may touch the network: job page checks are replaced with fakes."""
    global _NO_NETWORK
    _NO_NETWORK = [patch.object(verify, name, side_effect=AssertionError("network access in tests"))
                   for name in ("urlopen", "open_no_redirect")]
    _NO_NETWORK += [patch.object(usage, "urlopen", side_effect=URLError("no network in tests")),
                    patch.object(serper, "urlopen", side_effect=AssertionError("network access in tests")),
                    patch.object(cli, "COUNT_WAITS", ()), patch.object(cli, "COUNT_SETTLE_SECONDS", 0)]  # no real waits
    for blocker in _NO_NETWORK:
        blocker.start()


def tearDownModule():
    for blocker in _NO_NETWORK:
        blocker.stop()


def item(job_id="100", title=None, **extra):
    # Distinct titles per job id: identical titles at one company count as duplicates (hidden).
    return {"title": title or f"Software Architect {job_id}", "link": f"https://job-boards.greenhouse.io/fixture/jobs/{job_id}",
            "snippet": "Synthetic test data; not a real vacancy.", **extra}


def page(items=None, next_start=None):
    payload = {"organic_results": [] if items is None else items}
    if next_start is not None:
        payload["serpapi_pagination"] = {"next": f"https://serpapi.com/search.json?start={next_start}"}
    return payload


class FakeClient:
    """`used`, `left`, `price`: what SerpApi reports. `cached=True`: searches are not charged."""

    def __init__(self, responses, used=0, left=1000, price=0.0, cached=False, account_id="fixture-account",
                 provider="serpapi", bought=False):
        self.responses = list(responses)
        self.calls = []
        self.used, self.left, self.price, self.cached, self.account_id = used, left, price, cached, account_id
        self.provider, self.bought = provider, bought

    def search(self, query, start=0, tbs=""):
        self.calls.append((query, start))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        if not self.cached:
            self.used, self.left = self.used + 1, self.left - 1
        return result

    def account(self):
        return serpapi.Account(self.account_id, "Fixture Plan", self.price, self.used, self.left, extra_credits=500,
                               provider=self.provider, bought_credits=self.bought)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.settings = app.load_settings(POSITIONS, SEARCH)

    def write(self, temp, name, text):
        path = Path(temp) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_one_platform_per_query_and_skip_disabled(self):
        queries = app.build_queries(self.settings)
        self.assertEqual([(q.family_id, q.sites) for q in queries],
                         [(f, (s,)) for f in ("architecture", "staff", "em") for s in ("greenhouse.io", "comeet.com/jobs")])
        self.assertTrue(queries[0].text.startswith('site:greenhouse.io ("Software Architect" OR'))
        self.assertNotIn(" OR site:", " ".join(q.text for q in queries))
        self.assertIn('("Israel" OR "Tel Aviv")', queries[0].text)
        self.assertNotIn("jobs.lever.co", " ".join(q.text for q in queries))
        self.assertEqual(parse_qs(urlsplit(queries[0].google_url()).query)["q"], [queries[0].text])

    def test_titles_are_split_to_fit_the_word_limit(self):
        small = app.Settings(**{**vars(self.settings), "max_query_words": 14})
        queries = [q for q in app.build_queries(small, families=["staff"]) if q.sites == ("greenhouse.io",)]
        self.assertGreater(len(queries), 1)
        self.assertTrue(all(q.words <= 14 for q in queries))
        self.assertEqual(sum(len(q.titles) for q in queries), 4)
        with self.assertRaises(app.ConfigError):
            app.build_queries(app.Settings(**{**vars(self.settings), "max_query_words": 6}))

    def test_selection_by_family_and_track(self):
        self.assertEqual({q.family_id for q in app.build_queries(self.settings, tracks=["management"])}, {"em"})
        self.assertEqual({q.family_id for q in app.build_queries(self.settings, families=["vp"])}, {"vp"})
        with self.assertRaises(app.ConfigError):
            app.build_queries(self.settings, families=["architecure"])

    def test_plan_searches_most_overdue_first(self):
        queries = app.build_queries(self.settings)
        now = datetime(2026, 11, 10, tzinfo=timezone.utc)
        self.assertEqual([q.family_id for q in app.plan_run(queries, 2, now=now)], ["architecture", "architecture"])
        last = {q.key: ("2026-11-09T00:00:00+00:00" if q.family_id == "architecture" else "2026-11-01T00:00:00+00:00")
                for q in queries if q.family_id != "em"}
        # em never searched -> first; staff 9 days / 4 = 2.25 overdue; architecture 1 day / 2 = 0.5
        self.assertEqual([q.family_id for q in app.plan_run(queries, 3, last, (2, 4, 8), now)], ["em", "em", "staff"])
        # priority weighting: architecture 3 days / 2 = 1.5 beats staff 4 days / 4 = 1.0 although staff is older
        last = {q.key: ("2026-11-07T00:00:00+00:00" if q.family_id == "architecture" else "2026-11-06T00:00:00+00:00")
                for q in queries}
        self.assertEqual([q.family_id for q in app.plan_run(queries, 2, last, (2, 4, 8), now)], ["architecture", "architecture"])
        self.assertEqual(app.monthly_demand(queries, (2, 4, 8)), 60)

    def test_invalid_config_fails_before_any_search(self):
        good = POSITIONS.read_text(encoding="utf-8")
        bad_inputs = [good.replace('track = "ic"', 'track = "unknown"', 1),
                      good.replace('id = "staff"', 'id = "architecture"'),
                      good.replace('"Software Architect"', '"Software \\"Architect\\""'),
                      good.replace("priority = 1", "priority = true"),
                      "not toml ["]
        with tempfile.TemporaryDirectory() as temp:
            for text in bad_inputs:
                with self.subTest(text=text[:40]), self.assertRaises(app.ConfigError):
                    app.load_settings(self.write(temp, "p.toml", text), SEARCH)
            bad_search = SEARCH.read_text(encoding="utf-8").replace('freshness = "week"', 'freshness = "fortnight"')
            with self.assertRaises(app.ConfigError):
                app.load_settings(POSITIONS, self.write(temp, "s.toml", bad_search))

    def test_preview_does_not_request_credentials_or_network(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(serpapi, "urlopen", side_effect=AssertionError("network")), patch.object(app.getpass, "getpass", side_effect=AssertionError("credential")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(["preview", "--positions", str(POSITIONS), "--search-config", str(SEARCH), "--output", temp]), 0)
            self.assertTrue((Path(temp) / "search_plan.html").is_file())
            self.assertFalse((Path(temp) / "history.sqlite3").exists())
            with (Path(temp) / "search_plan.csv").open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 6)

    def test_example_and_local_inputs_are_valid(self):
        sources = common.ROOT / "sources"
        pairs = [(sources / "positions.example.toml", sources / "search.example.toml")]
        if config.DEFAULT_POSITIONS.exists() and config.DEFAULT_SEARCH.exists():   # private copies, if present
            pairs.append((config.DEFAULT_POSITIONS, config.DEFAULT_SEARCH))
        for positions, search in pairs:
            with self.subTest(positions=positions.name):
                settings = app.load_settings(positions, search)
                queries = app.build_queries(settings)
                self.assertTrue(queries)
                self.assertTrue(all(q.words <= settings.max_query_words for q in queries))
                self.assertTrue(all(q.text.startswith("site:") and len(q.sites) == 1 for q in queries))


class IdentityTests(unittest.TestCase):
    def test_greenhouse_host_and_tracking_duplicates(self):
        first = app.canonical_result("https://boards.greenhouse.io/fixture/jobs/123?gh_src=abc&utm_source=test#apply")
        second = app.canonical_result("https://job-boards.greenhouse.io/fixture/jobs/123")
        self.assertEqual(first["identity"], second["identity"])
        self.assertEqual(first["url"], second["url"])
        self.assertNotEqual(first["identity"], app.canonical_result("https://boards.greenhouse.io/fixture/jobs/124")["identity"])
        eu = app.canonical_result("https://job-boards.eu.greenhouse.io/fixture/jobs/555")
        self.assertEqual((eu["platform"], eu["result_type"], eu["identity"]), ("Greenhouse", "job", "greenhouse:fixture:555"))

    def test_application_variants_keep_same_job_identity(self):
        cases = [
            ("https://apply.workable.com/fixture/j/ABC123/", "https://apply.workable.com/fixture/j/ABC123/apply/"),
            ("https://jobs.lever.co/fixture/12345678-1234-1234-1234-123456789abc", "https://jobs.lever.co/fixture/12345678-1234-1234-1234-123456789abc/apply?lever-source=abc"),
            ("https://jobs.ashbyhq.com/fixture/12345678-1234-1234-1234-123456789abc", "https://jobs.ashbyhq.com/fixture/12345678-1234-1234-1234-123456789abc/?action=apply")]
        for first, second in cases:
            with self.subTest(first=first):
                self.assertEqual(app.canonical_result(first)["identity"], app.canonical_result(second)["identity"])
                self.assertEqual(app.canonical_result(first)["url"], app.canonical_result(second)["url"])

    def test_comeet_title_slug_changes_do_not_duplicate_job(self):
        first = app.canonical_result("https://www.comeet.com/jobs/fixture/81.009/old-title/98.20C?coref=123")
        second = app.canonical_result("https://comeet.com/jobs/fixture/81.009/new-title/98.20C")
        self.assertEqual(first["identity"], second["identity"])
        self.assertEqual(first["result_type"], "job")

    def test_career_pages_are_kept_and_labeled(self):
        urls = ["https://www.comeet.com/jobs/fixture/81.009", "https://job-boards.greenhouse.io/fixture",
                "https://jobs.lever.co/fixture", "https://jobs.ashbyhq.com/fixture", "https://apply.workable.com/fixture"]
        self.assertTrue(all(app.canonical_result(url)["result_type"] == "career_page" for url in urls))

    def test_workday_and_smartrecruiters_jobs_have_one_identity(self):
        host = "https://fixture.wd5.myworkdayjobs.com"
        same_job = [f"{host}/en-US/External/job/Israel-Haifa/Engineering-Manager_JR-123",
                    f"{host}/External/job/Engineering-Manager_JR-123/apply/applyManually?source=board",
                    f"{host}/he-IL/External/job/Israel-Haifa/Engineering-Manager_JR-123"]
        found = [app.canonical_result(url) for url in same_job]
        self.assertEqual({(r["identity"], r["platform"], r["result_type"], r["company_url_slug"]) for r in found},
                         {("workday:fixture:jr-123", "Workday", "job", "fixture")})
        self.assertEqual(found[1]["url"], f"{host}/External/job/Engineering-Manager_JR-123")
        searches = [f"{host}/us-en/External?Country=abc", f"{host}/External/13/refreshFacet/318c8b?source=x"]
        self.assertEqual({(app.canonical_result(url)["identity"], app.canonical_result(url)["result_type"])
                          for url in searches}, {(f"{host}/External", "career_page")})
        job = "https://jobs.smartrecruiters.com/FixtureCo/744000157809003-engineering-manager"
        found = app.canonical_result(job + "?oga=true")
        self.assertEqual((found["identity"], found["url"], found["platform"], found["result_type"]),
                         ("smartrecruiters:fixtureco:744000157809003", job, "SmartRecruiters", "job"))
        self.assertEqual(app.canonical_result("https://jobs.smartrecruiters.com/FixtureCo")["result_type"], "career_page")

    def test_query_based_identity_is_preserved_for_custom_platforms(self):
        first = app.canonical_result("https://careers.example.com/job?source=engineering&jobId=42&utm_campaign=test")
        second = app.canonical_result("https://careers.example.com/job?source=engineering&jobId=43")
        self.assertIn("jobId=42", first["url"])
        self.assertIn("source=engineering", first["url"])
        self.assertNotEqual(first["identity"], second["identity"])

    def test_unsafe_links_and_offsite_results_are_rejected(self):
        for url in ("javascript:alert(1)", "file:///tmp/a", "https://user:password@example.com/job", "https://example.com:bad/job"):
            self.assertIsNone(app.canonical_result(url))
        self.assertFalse(app.in_site("https://comeet.com.evil.example/jobs/a", "comeet.com/jobs"))
        self.assertFalse(app.in_site("https://comeet.com/jobs-old/a", "comeet.com/jobs"))
        self.assertTrue(app.in_site("https://www.comeet.com/jobs/a", "comeet.com/jobs"))
        self.assertTrue(app.in_site("https://boards.greenhouse.io/fixture/jobs/1", "greenhouse.io"))


class ProviderTests(unittest.TestCase):
    def call(self, payload):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        with patch.object(serpapi, "urlopen", return_value=io.BytesIO(raw)):
            return app.SerpApiClient("fixture-secret").search("site:comeet.com/jobs architect Israel")

    def test_google_api_request_parameters(self):
        with patch.object(serpapi, "urlopen", return_value=io.BytesIO(b'{"organic_results": []}')) as mocked:
            app.SerpApiClient("fixture-secret", "il", "en", 12).search("example query", 10)
            request = mocked.call_args.args[0]
            self.assertEqual(urlsplit(request.full_url).hostname, "serpapi.com")
            values = parse_qs(urlsplit(request.full_url).query)
            self.assertEqual(values["engine"], ["google"])
            self.assertEqual(values["start"], ["10"])
            self.assertEqual(values["q"], ["example query"])
            self.assertNotIn("num", values)

    def test_network_and_http_errors_do_not_leak_key(self):
        failures = [URLError("https://serpapi.com/?api_key=fixture-secret"),
                    HTTPError("https://serpapi.com/?api_key=fixture-secret", 429, "fixture-secret", {}, None), TimeoutError("fixture-secret")]
        for failure in failures:
            with self.subTest(failure=type(failure)), patch.object(serpapi, "urlopen", side_effect=failure):
                with self.assertRaises(app.ProviderError) as caught:
                    app.SerpApiClient("fixture-secret").search("test")
                self.assertNotIn("fixture-secret", str(caught.exception))
                if isinstance(failure, HTTPError):
                    self.assertTrue(caught.exception.fatal)
                    self.assertTrue(caught.exception.quota)

    def test_empty_google_response_is_not_a_provider_failure(self):
        self.assertEqual(self.call({"error": "Google hasn't returned any results for this query."}), {"organic_results": []})
        self.assertEqual(self.call({"search_metadata": {"status": "Success"}})["search_metadata"]["status"], "Success")

    def test_malformed_and_unfinished_responses_fail(self):
        for payload in (b"invalid", [], {}, {"organic_results": {}}, {"search_metadata": {"status": "Processing"}}):
            with self.subTest(payload=payload), self.assertRaises(app.ProviderError):
                self.call(payload)

    def test_error_body_never_echoes_credentials(self):
        for message in ("Invalid API key fixture-secret", "Search problem fixture-secret", "Account credit limit fixture-secret"):
            with self.subTest(message=message), self.assertRaises(app.ProviderError) as caught:
                self.call({"error": message})
            self.assertNotIn("fixture-secret", str(caught.exception))

    def test_pagination_supports_next_link_and_prevents_loops(self):
        self.assertEqual(app.next_offset({"serpapi_pagination": {"next_link": "https://serpapi.com/search.json?start=20"}}, 10), 20)
        self.assertIsNone(app.next_offset(page([], 10), 10))
        self.assertIsNone(app.next_offset({"pagination": {"next": "https://google.com/?start=bad"}}, 0))


class LocalEnvTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "local.env.yaml"

    def tearDown(self):
        self.temp.cleanup()

    def load(self, text, environ=None):
        self.path.write_text(text, encoding="utf-8")
        with patch.dict("os.environ", environ or {}, clear=True):
            applied = app.load_local_env(self.path)
            return applied, dict(os.environ)

    def test_values_become_environment_defaults(self):
        applied, env = self.load('SERPAPI_API_KEY: "file-secret"\n')
        self.assertEqual(applied, ["SERPAPI_API_KEY"])
        self.assertEqual(env["SERPAPI_API_KEY"], "file-secret")

    def test_real_environment_wins_and_placeholder_is_ignored(self):
        _, env = self.load('SERPAPI_API_KEY: "file-secret"\n', {"SERPAPI_API_KEY": "real"})
        self.assertEqual(env["SERPAPI_API_KEY"], "real")
        applied, env = self.load('SERPAPI_API_KEY: "paste-your-key-here"\n')
        self.assertEqual(applied, [])
        self.assertNotIn("SERPAPI_API_KEY", env)

    def test_missing_or_empty_file_is_fine(self):
        self.assertEqual(app.load_local_env(self.path), [])
        self.assertEqual(self.load("")[0], [])

    def test_errors_never_echo_the_file_contents(self):
        for text in ('SERPAPI_API_KEY: "file-secret\n', "- file-secret\n", "file-secret bad: [\n"):
            with self.subTest(text=text), self.assertRaises(app.ConfigError) as caught:
                self.load(text)
            self.assertNotIn("file-secret", str(caught.exception))

    def test_example_template_is_valid_and_has_no_real_key(self):
        example = common.ROOT / "local.env.example.yaml"
        self.assertTrue(example.exists(), "the template must be in the repository")
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(app.load_local_env(example), [])


class SearchFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name)
        self.history = self.output / "data" / "history.sqlite3"
        self.workbook = self.output / "results" / "job_leads.xlsx"
        settings = app.load_settings(POSITIONS, SEARCH)
        self.settings = app.Settings(**{**vars(settings), "platforms": settings.platforms[:1]})  # Greenhouse only
        self.queries = app.build_queries(self.settings, families=["architecture", "staff"])

    def tearDown(self):
        self.temp.cleanup()

    def run_search(self, responses, pages=2, budget=4, resolve=None):
        client = FakeClient(responses)
        with contextlib.redirect_stdout(io.StringIO()):
            result = app.run_search(self.queries, self.settings, client, history=self.history,
                                    workbook=self.workbook, pages=pages, budget=budget, resolve=resolve)
        return result, client

    def read_sheet(self):
        book = load_workbook(self.workbook)
        rows = list(book["Leads"].iter_rows(values_only=True))
        return [dict(zip(rows[0], row)) for row in rows[1:]]

    def edit_sheet(self, lead_suffix, status, note):
        book = load_workbook(self.workbook)
        sheet = book["Leads"]
        header = [c.value for c in sheet[1]]
        for row in sheet.iter_rows(min_row=2):
            if str(row[header.index("Link")].value).endswith(lead_suffix):
                row[header.index("Status")].value = status
                row[header.index("Note")].value = note
        book.save(self.workbook)

    def test_breadth_first_pagination_and_dedup_with_provenance(self):
        summary, client = self.run_search([page([item()], 10), page([item()], 10), page([item("101")]), page([item("102")])])
        self.assertEqual(client.calls, [(self.queries[0].text, 0), (self.queries[1].text, 0), (self.queries[0].text, 10), (self.queries[1].text, 10)])
        self.assertEqual(summary["unique_results"], 3)
        self.assertEqual(summary["new_results"], 3)
        repeated = next(r for r in self.read_sheet() if r["Link"].endswith("/100"))
        self.assertIn("Software architecture", repeated["Role family"])
        self.assertIn("Staff and Principal", repeated["Role family"])
        self.assertEqual(repeated["Track / CV"], "IC")
        self.assertEqual(summary["status"], "finished")

    def test_history_identifies_rediscovery_across_runs(self):
        first, _ = self.run_search([page([item()]), page([item("101")])])
        second, _ = self.run_search([page([item()]), page([item("102")])])
        self.assertEqual(first["new_results"], 2)
        self.assertEqual(second["new_results"], 1)
        self.assertEqual(len(self.read_sheet()), 3)  # the workbook keeps every lead once

    def test_hard_request_cap_saves_partial_results(self):
        summary, client = self.run_search([page([item()], 10), page([item("101")], 10)], budget=2)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(summary["requests_used"], 2)
        self.assertEqual(summary["status"], "partial")
        self.assertTrue(all(q["status"] == "request_budget" for q in summary["queries"]))
        self.assertEqual(len(self.read_sheet()), 2)

    def test_page_cap_is_explicit(self):
        summary, _ = self.run_search([page([item()], 10), page([item()], 10)], pages=1)
        self.assertEqual(summary["status"], "page_limited")
        self.assertTrue(all(q["status"] == "page_limit" for q in summary["queries"]))

    def test_auth_or_quota_failure_stops_without_retry(self):
        summary, client = self.run_search([page([item()], 10), app.ProviderError("quota reached", fatal=True)])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["queries"][0]["status"], "stopped_after_provider_error")
        self.assertEqual(len(self.read_sheet()), 1)

    def test_nonfatal_error_continues_other_queries(self):
        summary, client = self.run_search([app.ProviderError("temporary error"), page([item()])])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(len(self.read_sheet()), 1)

    def test_interrupt_retains_completed_pages(self):
        summary, _ = self.run_search([page([item()], 10), KeyboardInterrupt()])
        self.assertEqual(summary["status"], "interrupted")
        self.assertEqual(len(self.read_sheet()), 1)
        self.assertTrue((self.output / "results" / "last_run.json").exists())

    def test_empty_run_keeps_workbook_and_history(self):
        self.run_search([page([item()]), page([])])
        summary, _ = self.run_search([page([]), page([])])
        self.assertEqual(summary["unique_results"], 0)
        self.assertEqual(len(self.read_sheet()), 1)
        rediscovery, _ = self.run_search([page([item()]), page([])])
        self.assertEqual(rediscovery["new_results"], 0)

    def test_workbook_text_is_never_a_formula(self):
        self.run_search([page([item(title="=1+1", snippet="@SUM(A1)")]), page([])])
        cells = load_workbook(self.workbook)["Leads"][2]
        self.assertTrue(all(c.data_type != "f" for c in cells))
        self.assertEqual(self.read_sheet()[0]["Title"], "=1+1")

    def test_google_redirect_links_are_followed_to_the_job_page(self):
        # SerpApi may return google.com/goto?url=... links; the real address only shows in displayed_link.
        goto = "https://www.google.com/goto?url=CAESjob"
        lost = "https://www.google.com/goto?url=CAESlost"
        elsewhere = "https://www.google.com/goto?url=CAESother"
        results = [
            {"title": "Software Architect 555", "link": goto, "snippet": "Synthetic.",
             "displayed_link": "https://job-boards.greenhouse.io › fixture › jobs"},
            {"title": "Software Architect 556", "link": lost, "snippet": "Synthetic.",
             "displayed_link": "https://job-boards.greenhouse.io › fixture"},
            {"title": "Elsewhere", "link": elsewhere, "snippet": "Synthetic.",
             "displayed_link": "https://www.linkedin.com › jobs › view"},
        ]
        asked = []

        def resolve(url):
            asked.append(url)
            return ("https://job-boards.greenhouse.io/fixture/jobs/555", "") if url == goto else (None, "HTTP 400")

        summary, _ = self.run_search([page(results), page([])], resolve=resolve)
        self.assertEqual(asked, [goto, lost, lost, lost])  # only platform links; a failed one gets two more tries
        self.assertEqual(summary["unique_results"], 1)
        self.assertTrue(self.read_sheet()[0]["Link"].endswith("/jobs/555"))
        memory = app.ResultStore(self.history)
        try:
            examples = memory.latest_outcomes()[self.queries[0].key]["examples"]
        finally:
            memory.close()
        self.assertIn("not on platform: https://job-boards.greenhouse.io › fixture [HTTP 400] | https://www.linkedin.com", examples)
        self.assertEqual(summary["google_links"], {"retried": 1, "followed_on_retry": 0, "failed": {"HTTP 400": 1}})

    def test_google_links_refused_at_first_are_tried_again_after_the_searches(self):
        # Google refused results 7-10 of a fresh search, then followed the same links minutes later.
        late = "https://www.google.com/goto?url=CAESlate"
        results = [{"title": "Software Architect 557", "link": late, "snippet": "Synthetic.",
                    "displayed_link": "https://job-boards.greenhouse.io › fixture › jobs"}]
        tries = []

        def resolve(url):
            tries.append(url)
            return (None, "HTTP 400") if len(tries) == 1 else ("https://job-boards.greenhouse.io/fixture/jobs/557", "")

        summary, client = self.run_search([page(results), page([])], resolve=resolve)
        self.assertEqual(tries, [late, late])
        self.assertEqual(len(client.calls), 2)  # the second try costs no search
        self.assertEqual(summary["unique_results"], 1)
        self.assertEqual(summary["google_links"], {"retried": 1, "followed_on_retry": 1, "failed": {}})
        memory = app.ResultStore(self.history)
        try:
            self.assertEqual(memory.latest_outcomes()[self.queries[0].key]["on_platform"], 1)
        finally:
            memory.close()

    def test_google_links_refused_twice_get_a_last_try(self):
        # Google answered HTTP 200 instead of a redirect at the first two tries, then followed the link.
        late = "https://www.google.com/goto?url=CAESlater"
        results = [{"title": "Software Architect 558", "link": late, "snippet": "Synthetic.",
                    "displayed_link": "https://job-boards.greenhouse.io › fixture › jobs"}]
        tries = []

        def resolve(url):
            tries.append(url)
            return ("https://job-boards.greenhouse.io/fixture/jobs/558", "") if len(tries) == 3 \
                else (None, "HTTP 200, no redirect")

        summary, client = self.run_search([page(results), page([])], resolve=resolve)
        self.assertEqual(tries, [late, late, late])
        self.assertEqual(len(client.calls), 2)  # the extra tries cost no search
        self.assertEqual(summary["unique_results"], 1)
        self.assertEqual(summary["google_links"], {"retried": 1, "followed_on_retry": 1, "failed": {}})

    def test_resolve_redirect_reads_the_location_header(self):
        headers = http.client.HTTPMessage()
        headers["Location"] = "https://www.comeet.com/jobs/acme/AA.001/team-lead/BB.002"
        redirect = HTTPError("https://www.google.com/goto?url=x", 302, "Found", headers, None)
        with patch.object(verify, "open_no_redirect", side_effect=redirect):
            self.assertEqual(verify.resolve_redirect("https://www.google.com/goto?url=x"), (headers["Location"], ""))
        refused = HTTPError("https://www.google.com/goto?url=x", 400, "Bad Request", http.client.HTTPMessage(), None)
        with patch.object(verify, "open_no_redirect", side_effect=refused):
            self.assertEqual(verify.resolve_redirect("https://www.google.com/goto?url=x"), (None, "HTTP 400"))
        with patch.object(verify, "open_no_redirect", side_effect=URLError("offline")):
            self.assertEqual(verify.resolve_redirect("https://www.google.com/goto?url=x"), (None, "network error"))

    def test_out_of_scope_and_malformed_results_do_not_enter_history(self):
        summary, _ = self.run_search([page([item(), {"link": "https://evil.example/123", "title": "wrong"}, {"title": "no URL"}, "bad"]), page([])])
        self.assertEqual(summary["unique_results"], 1)

    # ---- agreed new tests: Status/Note, monthly counter, open workbook
    def test_status_and_note_survive_later_runs(self):
        self.run_search([page([item()]), page([item("101")])])
        self.edit_sheet("/100", "applied", "sent EM CV")
        self.edit_sheet("/101", "not relevant", "")
        summary, _ = self.run_search([page([item("102")]), page([item()])])
        self.assertEqual(summary["notes_imported"], 2)
        rows = {r["Link"].rsplit("/", 1)[-1]: r for r in self.read_sheet()}
        self.assertEqual((rows["100"]["Status"], rows["100"]["Note"]), ("applied", "sent EM CV"))
        self.assertEqual(rows["101"]["Status"], "not relevant")
        self.assertEqual(rows["102"]["Status"], "new")
        self.workbook.unlink()  # notes live in memory too, so a lost workbook is rebuilt with them
        self.run_search([page([]), page([])])
        self.assertEqual(next(r for r in self.read_sheet() if r["Link"].endswith("/100"))["Note"], "sent EM CV")

    def test_monthly_counter_counts_every_request_and_blocks_at_limit(self):
        self.run_search([page([item()], 10), app.ProviderError("temporary"), page([])], budget=3)
        store = app.ResultStore(self.history)
        try:
            self.assertEqual(store.searches_this_month(), 3)
            next_month = datetime.now(timezone.utc).replace(day=28) + timedelta(days=5)
            self.assertEqual(store.searches_this_month(next_month), 0)
            for _ in range(247):
                store.record_search("filler", self.queries[0], 0, "ok")
        finally:
            store.close()
        args = ["run", "--positions", str(POSITIONS), "--search-config", str(SEARCH), "--history", str(self.history),
                "--output", str(self.output / "results"), "--env-file", str(self.output / "missing.yaml")]
        fake = FakeClient([], used=250, left=0)                  # SerpApi's own count is what blocks
        with patch.dict("os.environ", {"SERPAPI_API_KEY": "fixture-secret"}), \
                patch.object(cli, "SerpApiClient", return_value=fake), \
                patch.object(serpapi, "urlopen", side_effect=AssertionError("network")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main(args), 1)
        self.assertEqual(fake.calls, [])

    def test_open_workbook_stops_before_any_search(self):
        self.run_search([page([item()]), page([])])
        lock = self.workbook.with_name("~$job_leads.xlsx")
        lock.write_text("", encoding="utf-8")
        client = FakeClient([page([item("101")])])
        with self.assertRaises(app.WorkbookLockedError):
            app.run_search(self.queries, self.settings, client, history=self.history,
                           workbook=self.workbook, pages=1, budget=4)
        self.assertEqual(client.calls, [])

    def test_region_tagging(self):
        self.assertEqual(app.region_of("Architect, Tel Aviv, Israel", self.settings), "Central")
        self.assertEqual(app.region_of("Architect - Jerusalem", self.settings), "outside area: Jerusalem")
        self.assertEqual(app.region_of("Architect, Israel", self.settings), "")


def fake_pages(pages):
    """fetch() stand-in: {url: (status, final_url, html)}; unknown URLs fail like a network error."""
    return lambda url: pages.get(url, (0, url, ""))


COMEET_JOB = "https://comeet.com/jobs/acme/A1.001/backend-team-lead/B2.002"
COMEET_PAGE = ('<script>POSITION_DATA = {"name": "Backend Team Lead", "workplace_type": "Hybrid", '
               '"time_updated": "2026-10-28T13:57:16Z", "location": {"name": "Tel Aviv", "country": "IL", '
               '"city": "Ramat Gan"}};\n</script>')
GREENHOUSE_JOB = "https://job-boards.greenhouse.io/acme/jobs/555"
GREENHOUSE_PAGE = ('<h1>Staff Engineer</h1><div class="job__location"><svg></svg><div>Remote, Canada; '
                   'Remote, Israel</div></div><script>{"published_at":"2026-09-17T04:06:14-04:00"}</script>')
ASHBY_JOB = "https://jobs.ashbyhq.com/acme/12345678-1234-1234-1234-123456789abc"
ASHBY_PAGE = ('<script type="application/ld+json">{"@type": "JobPosting", "title": "EM", "jobLocationType": '
              '"TELECOMMUTE", "datePosted": "2026-09-03", "jobLocation": {"address": {"addressCountry": "USA"}}}'
              '</script>')


class PageCheckTests(unittest.TestCase):
    def setUp(self):
        self.settings = app.load_settings(POSITIONS, SEARCH)

    def check(self, url, platform, pages):
        return app.check_page(url, platform, self.settings, fake_pages(pages))

    def test_platform_pages_give_location_mode_and_date(self):
        comeet = self.check(COMEET_JOB, "Comeet", {COMEET_JOB: (200, COMEET_JOB, COMEET_PAGE)})
        self.assertEqual((comeet.status, comeet.in_israel, comeet.work_mode, comeet.posted),
                         ("open", True, "hybrid", "2026-10-28"))
        self.assertIn("Ramat Gan", comeet.location)
        greenhouse = self.check(GREENHOUSE_JOB, "Greenhouse", {GREENHOUSE_JOB: (200, GREENHOUSE_JOB, GREENHOUSE_PAGE)})
        self.assertEqual((greenhouse.in_israel, greenhouse.work_mode, greenhouse.posted), (True, "remote", "2026-09-17"))
        ashby = self.check(ASHBY_JOB, "Ashby", {ASHBY_JOB: (200, ASHBY_JOB, ASHBY_PAGE)})
        self.assertEqual((ashby.status, ashby.in_israel, ashby.work_mode), ("open", False, "remote"))

    def test_closed_and_unknown_pages(self):
        board = "https://www.comeet.com/jobs/acme/A1.001"
        self.assertEqual(self.check(COMEET_JOB, "Comeet", {COMEET_JOB: (200, board, "<html>")}).status, "closed")
        self.assertEqual(self.check(GREENHOUSE_JOB, "Greenhouse", {GREENHOUSE_JOB: (404, GREENHOUSE_JOB, "")}).status, "closed")
        company = "https://www.acme.com/careers/staff-engineer?gh_jid=555"
        unknown = self.check(GREENHOUSE_JOB, "Greenhouse", {GREENHOUSE_JOB: (200, company, "<html>no data</html>")})
        self.assertEqual((unknown.status, unknown.in_israel, unknown.note), ("unknown", None, "company site"))
        self.assertEqual(self.check(ASHBY_JOB, "Ashby", {}).status, "unknown")   # network failure

    def test_company_site_links_are_read_from_greenhouse_embedded_copy(self):
        company = "https://www.acme.com/careers/staff-engineer?gh_jid=555"
        embed = "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=555"
        copy = ('<script>{"job_post_location":"Tel Aviv-Yafo, Tel Aviv District, Israel",'
                '"published_at":"2026-11-04T07:12:46-05:00"}</script>')
        found = self.check(GREENHOUSE_JOB, "Greenhouse", {GREENHOUSE_JOB: (200, company, "<html>"),
                                                          embed: (200, embed, copy)})
        self.assertEqual((found.status, found.in_israel, found.posted, found.note),
                         ("open", True, "2026-11-04", "company site"))
        board = "https://job-boards.greenhouse.io/embed/job_board?for=acme&error=true"
        closed = self.check(GREENHOUSE_JOB, "Greenhouse", {GREENHOUSE_JOB: (429, GREENHOUSE_JOB, ""),
                                                           embed: (200, board, "<html>")})
        self.assertEqual(closed.status, "closed")

    def test_smartrecruiters_page_microdata(self):
        url = "https://jobs.smartrecruiters.com/FixtureCo/744000157809003-engineering-manager"
        html = ('<main itemscope itemtype="http://schema.org/JobPosting"><h1 itemprop="title">Engineering Manager</h1>'
                '<li itemprop="jobLocation" itemscope itemtype="http://schema.org/Place"><span itemprop="address">'
                '<spl-job-location formattedAddress="Tel Aviv-Yafo, Israel" workplaceType="hybrid"></spl-job-location>'
                '<meta itemprop="addressCountry" content="Israel"><meta itemprop="addressLocality" content="Tel Aviv-Yafo">'
                '<meta itemprop="addressRegion" content="Tel Aviv District"></span></li>'
                '<meta itemprop="datePosted" content="2026-10-26T13:07:39.752Z"></main>')
        found = self.check(url, "SmartRecruiters", {url: (200, url, html)})
        self.assertEqual((found.status, found.location, found.in_israel, found.work_mode, found.posted),
                         ("open", "Tel Aviv-Yafo, Tel Aviv District, Israel", True, "hybrid", "2026-10-26"))
        board = "https://jobs.smartrecruiters.com/FixtureCo"
        self.assertEqual(self.check(url, "SmartRecruiters", {url: (200, board, "<html>")}).status, "closed")

    def test_workday_location_comes_from_the_address_without_a_request(self):
        def check(place, context=""):
            url = "https://fixture.wd5.myworkdayjobs.com/en-US/External/job/" + (f"{place}/" if place else "") + "EM_JR1"
            return app.check_page(url, "Workday", self.settings, fetch=None, context=context)  # a request would fail
        self.assertEqual((check("Israel-Haifa").in_israel, check("Israel-Haifa").location),
                         (True, "Israel Haifa (from the address)"))
        self.assertTrue(check("RehovotISR").in_israel)
        self.assertIs(check("US-CA-Santa-Clara").in_israel, False)                       # abroad: hidden
        self.assertIs(check("Chicago-IL").in_israel, False)                              # IL is Illinois here
        self.assertIsNone(check("US-CA-Santa-Clara", "EM - Santa Clara or Israel").in_israel)  # may be in Israel too
        self.assertEqual((check("").status, check("").location, check("").in_israel), ("unknown", "", None))

    def test_a_work_mode_alone_does_not_mean_abroad(self):
        def greenhouse(location):
            page = f'<div class="job__location"><svg></svg><div>{location}</div></div>'
            return self.check(GREENHOUSE_JOB, "Greenhouse", {GREENHOUSE_JOB: (200, GREENHOUSE_JOB, page)}).in_israel
        for location in ("Hybrid", "Remote", "On-site", "Remote / Hybrid"):
            self.assertIsNone(greenhouse(location), location)      # no place named: the job stays visible
        self.assertIs(greenhouse("Hybrid, Berlin"), False)         # a foreign place is still abroad
        self.assertIs(greenhouse("Remote - United States"), False)
        workday = "https://fixture.wd5.myworkdayjobs.com/en-US/External/job/Remote/EM_JR1"
        self.assertIsNone(app.check_page(workday, "Workday", self.settings, fetch=None).in_israel)


class HideRuleTests(unittest.TestCase):
    def setUp(self):
        settings = app.load_settings(POSITIONS, SEARCH)
        self.settings = app.Settings(**{**vars(settings), "title_exclude": ("Sales", "Hardware")})

    def row(self, ident, title, company="acme", kind="job", status="new", check=None, seen="2026-11-10T10:00:00+00:00"):
        return {"identity": ident, "title": title, "company_url_slug": company, "result_type": kind,
                "status": status, "check": check, "first_seen_utc": seen}

    def test_hide_rules(self):
        abroad = {"status": "open", "in_israel": 0}
        closed = {"status": "closed", "in_israel": None}
        rows = [
            self.row("1", "Engineering Manager, App Primitives @ Acme", seen="2026-11-09T10:00:00+00:00"),
            self.row("2", "Engineering Manager, App Primitives - CAN @ Acme"),          # location variant
            self.row("3", "Sales Engineering Manager"),                                  # excluded title
            self.row("4", "Hardware Team Lead", status="interested"),                    # marked: never hidden
            self.row("5", "Staff Engineer", check=abroad),                               # abroad
            self.row("6", "Platform Team Lead", check=closed),                           # closed
            self.row("7", "Jobs at Acme", kind="career_page"),                           # board of a shown company
            self.row("8", "Jobs at Beta", company="beta", kind="career_page"),           # only lead of Beta: shown
            self.row("9", "Job Application for Software Architect at Acme"),
        ]
        shown, hidden = app.split_visible(rows, self.settings)                  # careers pages hidden (default)
        self.assertEqual([r["identity"] for r in shown], ["1", "4", "9"])
        self.assertEqual(dict(hidden), {"duplicate": 1, "career page": 2, "excluded title": 1, "abroad": 1, "closed": 1})
        rows[7]["status"] = "not relevant"                                      # a marked careers page stays
        self.assertIn("8", [r["identity"] for r in app.split_visible(rows, self.settings)[0]])
        rows[7]["status"] = "new"
        boards = app.Settings(**{**vars(self.settings), "show_career_pages": True})
        shown, hidden = app.split_visible(rows, boards)
        self.assertEqual([r["identity"] for r in shown], ["1", "4", "8", "9"])
        self.assertEqual(dict(hidden), {"duplicate": 2, "excluded title": 1, "abroad": 1, "closed": 1})

    def test_parentheses_that_name_the_job_keep_jobs_apart(self):
        rows = [self.row("a", "Engineering Manager (Platform)", seen="2026-11-09T10:00:00+00:00"),
                self.row("b", "Engineering Manager (Data)", seen="2026-11-10T10:00:00+00:00"),         # another job
                self.row("c", "Engineering Manager (Data) (Tel Aviv / Hybrid)", seen="2026-11-11T10:00:00+00:00")]  # b again
        shown, hidden = app.split_visible(rows, self.settings)
        self.assertEqual([r["identity"] for r in shown], ["a", "b"])
        self.assertEqual(dict(hidden), {"duplicate": 1})

    def test_same_job_at_two_levels_and_duplicate_mark(self):
        def page(posted):
            return {"status": "open", "in_israel": 1, "location": "Tel Aviv-Yafo, Israel", "posted": posted}
        rows = [
            self.row("a", "Job Application for Staff Software Engineer at Acme", "acme", status="duplicate",
                     check=page("2026-06-01"), seen="2026-11-09T10:00:00+00:00"),       # marked duplicate
            self.row("b", "Job Application for Principal Software Engineer at Acme", "acme",
                     check=page("2026-06-01"), seen="2026-11-10T10:00:00+00:00"),       # so this one stays
            self.row("c", "Senior Software Engineer @ Acme", "acme", check=page("2026-06-01"),
                     seen="2026-11-11T10:00:00+00:00"),                                 # same job again: hidden
            self.row("d", "Staff Software Engineer @ Acme", "acme", check=page("2026-07-01"),
                     seen="2026-11-12T10:00:00+00:00"),                                 # other date: another job
            self.row("e", "Lead Software Engineer @ Acme", "acme", seen="2026-11-13T10:00:00+00:00"),  # unchecked
        ]
        shown, hidden = app.split_visible(rows, self.settings)
        self.assertEqual([r["identity"] for r in shown], ["a", "b", "d", "e"])
        self.assertEqual(dict(hidden), {"duplicate": 1})

    def test_title_variants_of_one_job_are_duplicates(self):
        early = "2026-11-09T10:00:00+00:00"
        rows = [
            self.row("a", "ServiceNow Full Stack Team Lead (Armis/Veza)", "ServiceNow", status="not relevant"),
            self.row("b", "Easy apply - Full Stack Team Lead (Veza/Armis)", "ServiceNow"),   # a again: hidden
            self.row("c", "Sr Staff Software Engineer - ServiceNow", "ServiceNow", seen=early),
            self.row("d", "Senior Staff Software Engineer", "ServiceNow"),                   # c again
            self.row("e", "Software Architect, GPU Networking - this page", "nvidia", seen=early),
            self.row("f", "Software Architect, GPU Networking", "nvidia"),                    # e again
            self.row("g", "Kaltura, Inc. Team Leader", "KalturaInc", seen=early),
            self.row("h", "Team Leader", "KalturaInc"),                                      # g again
            self.row("i", "Software Architect, AI Networking", "nvidia"),                     # another job
        ]
        shown, hidden = app.split_visible(rows, self.settings)
        self.assertEqual([r["identity"] for r in shown], ["a", "c", "e", "g", "i"])
        self.assertEqual(dict(hidden), {"duplicate": 4})

    def test_old_postings_are_hidden_only_when_set(self):
        def posted(days):
            return {"status": "open", "in_israel": 1, "location": "Tel Aviv",
                    "posted": (date.today() - timedelta(days=days)).isoformat()}
        rows = [self.row("recent", "Engineering Manager", check=posted(10)),
                self.row("old", "Team Lead", check=posted(400)),
                self.row("marked", "Software Architect", status="applied", check=posted(400)),   # marked: shown
                self.row("undated", "Tech Lead", check={"status": "unknown", "in_israel": None, "posted": ""}),
                self.row("unchecked", "Group Manager")]
        self.assertEqual(len(app.split_visible(rows, self.settings)[0]), 5)                   # 0 = never hidden
        shown, hidden = app.split_visible(rows, replace(self.settings, hide_older_than_days=30))
        self.assertEqual([r["identity"] for r in shown], ["recent", "marked", "undated", "unchecked"])
        self.assertEqual(dict(hidden), {"old posting": 1})


class DedupeMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.history = Path(self.temp.name) / "history.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_old_identities_are_merged_and_keep_status(self):
        old_id = "https://job-boards.eu.greenhouse.io/acme/jobs/777"   # how the EU job was stored before the fix
        connection = sqlite3.connect(self.history)
        store = app.ResultStore(self.history)
        store.close()
        with connection:
            connection.execute("INSERT INTO results VALUES (?, ?, 'EM', '', 'Other', '', 'other_page', "
                               "'2026-11-09T10:00:00+00:00', '2026-11-09T10:00:00+00:00', 'run1')", (old_id, old_id))
            connection.execute("INSERT INTO leads VALUES (?, 'applied', 'sent EM CV', '2026-11-09T11:00:00+00:00')", (old_id,))
            connection.execute("INSERT INTO matches VALUES ('run1', ?, 'q', 'em', 'management')", (old_id,))
        connection.close()
        store = app.ResultStore(self.history)                          # re-opening migrates
        try:
            self.assertEqual(store.migrated, 1)
            rows = store.rows()
            self.assertEqual([(r["identity"], r["platform"], r["status"], r["note"]) for r in rows],
                             [("greenhouse:acme:777", "Greenhouse", "applied", "sent EM CV")])
            self.assertEqual(store.resolve(old_id), "greenhouse:acme:777")   # old workbook Lead IDs still match
            store.save_notes({old_id: ("interested", "call back")})
            self.assertEqual(store.rows()[0]["status"], "interested")
            self.assertEqual(store.ingest("run2", app.SearchQuery("em", "EM", "management", 1, 0, ("greenhouse.io",), (), "q2"),
                                          [{"link": "https://boards.eu.greenhouse.io/acme/jobs/777", "title": "EM"}])["kept"][0]["new"],
                             False)                                    # found again: not new
        finally:
            store.close()

    def test_workday_rows_stored_before_support_merge_and_are_never_requested(self):
        placed = "https://fixture.wd5.myworkdayjobs.com/External/job/US-CA-Santa-Clara/Engineering-Manager_JR9"
        urls = ["https://fixture.wd5.myworkdayjobs.com/en-US/External/job/Engineering-Manager_JR9",   # no location
                placed + "/apply"]
        app.ResultStore(self.history).close()
        connection = sqlite3.connect(self.history)
        with connection:
            for n, url in enumerate(urls):                             # stored as "Other" before the fix
                seen = f"2026-11-1{n}T10:00:00+00:00"
                connection.execute("INSERT INTO results VALUES (?, ?, 'Engineering Manager', 'Santa Clara', 'Other', "
                                   "'', 'other_page', ?, ?, 'run1')", (url, url, seen, seen))
            connection.execute("INSERT INTO leads VALUES (?, 'not relevant', 'US only', '2026-11-18T11:00:00+00:00')",
                               (urls[1],))
        connection.close()
        store = app.ResultStore(self.history)                          # re-opening migrates
        try:
            self.assertEqual(store.migrated, 2)
            self.assertEqual([(r["identity"], r["platform"], r["result_type"], r["status"], r["note"]) for r in store.rows()],
                             [("workday:fixture:jr9", "Workday", "job", "not relevant", "US only")])
            with contextlib.redirect_stdout(io.StringIO()):
                counts = app.check_pages(store, app.load_settings(POSITIONS, SEARCH), fetch=None)  # a request would fail
            self.assertEqual(counts, {"unknown": 1})
            self.assertEqual(store.unchecked_jobs(10), [])             # never asked again
            row = store.rows()[0]                                      # the merged address that names the place
            self.assertEqual((row["url"], row["check"]["location"]), (placed, "US CA Santa Clara (from the address)"))
        finally:
            store.close()

    def test_workday_keeps_the_address_that_names_the_location(self):
        bare = "https://fixture.wd5.myworkdayjobs.com/External/job/Engineering-Manager_JR7"
        placed = "https://fixture.wd5.myworkdayjobs.com/External/job/Israel-Haifa/Engineering-Manager_JR7"
        query = app.SearchQuery("em", "EM", "management", 1, 0, ("myworkdayjobs.com",), (), "q")
        settings = app.load_settings(POSITIONS, SEARCH)
        store = app.ResultStore(self.history)
        try:
            def found(link):
                store.ingest("run1", query, [{"link": link, "title": "Engineering Manager", "snippet": ""}])
                with contextlib.redirect_stdout(io.StringIO()):
                    app.check_pages(store, settings, fetch=None)
                return store.rows()[0]
            self.assertEqual(found(bare)["check"]["location"], "")
            row = found(placed)                                        # found again with the location: read again
            self.assertEqual((row["url"], row["check"]["location"], row["check"]["in_israel"]),
                             (placed, "Israel Haifa (from the address)", 1))
            self.assertEqual(found(bare)["url"], placed)               # a later address without it doesn't replace it
        finally:
            store.close()

    def test_recheck_opens_every_page_and_never_unhides_a_known_job(self):
        settings = app.load_settings(POSITIONS, SEARCH)
        query = app.SearchQuery("em", "EM", "management", 1, 0, ("greenhouse.io",), (), "q")
        closed, abroad, israel = (f"https://job-boards.greenhouse.io/acme/jobs/{n}" for n in (1, 2, 3))
        here = '<div class="job__location"><svg></svg>Tel Aviv, Israel</div>'
        first = {closed: (404, closed, ""),
                 abroad: (200, abroad, '<div class="job__location"><svg></svg>Remote, USA</div>'),
                 israel: (200, israel, here)}
        later = {abroad: (200, abroad, here), israel: (404, israel, "")}     # `closed` can't be read now
        store = app.ResultStore(self.history)
        try:
            store.ingest("run1", query, [{"link": url, "title": title, "snippet": ""} for url, title in
                                         ((closed, "Engineering Manager"), (abroad, "Team Lead"), (israel, "Architect"))])
            with contextlib.redirect_stdout(io.StringIO()):
                app.check_pages(store, settings, fake_pages(first))
                self.assertEqual(dict(app.split_visible(store.rows(), settings)[1]), {"closed": 1, "abroad": 1})
                counts = app.check_pages(store, settings, fake_pages(later), limit=1, recheck=True)
            self.assertEqual(counts, {"not read (earlier result kept)": 1, "open": 1, "closed": 1})  # all 3, past the limit
            shown, hidden = app.split_visible(store.rows(), settings)
            self.assertEqual(dict(hidden), {"closed": 2})       # the unread page keeps "closed"; the other closed since
            self.assertEqual([r["identity"] for r in shown], ["greenhouse:acme:2"])   # no longer abroad
        finally:
            store.close()

    def test_hidden_leads_never_return_as_new(self):
        settings = app.load_settings(POSITIONS, SEARCH)
        settings = app.Settings(**{**vars(settings), "platforms": settings.platforms[:1]})
        queries = app.build_queries(settings, families=["architecture"])
        workbook = Path(self.temp.name) / "job_leads.xlsx"
        abroad_url = "https://job-boards.greenhouse.io/fixture/jobs/100"
        pages = {abroad_url: (200, abroad_url, '<div class="job__location"><svg></svg>Remote, USA</div>')}
        for run in range(2):
            with contextlib.redirect_stdout(io.StringIO()):
                summary = app.run_search(queries, settings, FakeClient([page([item()])]), history=self.history,
                                         workbook=workbook, pages=1, budget=1, fetch=fake_pages(pages))
            self.assertEqual(summary["new_results"], 1 if run == 0 else 0)
            self.assertEqual(summary["hidden"], {"abroad": 1})
            rows = list(load_workbook(workbook)["Leads"].iter_rows(values_only=True))
            self.assertEqual(len(rows), 1)                                 # header only


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.history, self.output = root / "data" / "history.sqlite3", root / "results"
        self.search = root / "search.toml"
        self.search.write_text(SEARCH.read_text(encoding="utf-8").replace("[budget]\n", "[budget]\nper_hour = 4\n", 1),
                               encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def sweep(self, responses):
        fake = FakeClient(responses)
        args = ["sweep", "--positions", str(POSITIONS), "--search-config", str(self.search), "--history", str(self.history),
                "--output", str(self.output), "--env-file", str(Path(self.temp.name) / "missing.yaml")]
        with patch.object(cli, "SerpApiClient", return_value=fake), patch.object(cli, "fetch_page", fake_pages({})), \
                patch.object(serpapi, "urlopen", side_effect=AssertionError("network")), \
                patch.dict("os.environ", {"SERPAPI_API_KEY": "fixture-secret"}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = app.main(args)
        return code, fake

    def test_sweep_searches_each_query_once_and_resumes_after_hourly_limit(self):
        all_queries = {q.text for q in app.build_queries(app.load_settings(POSITIONS, self.search))}
        code, first = self.sweep([page([item(str(i))]) for i in range(10)])
        self.assertEqual(code, 0)
        self.assertEqual(len(first.calls), 4)                     # stopped at the hourly limit
        self.assertEqual(len({query for query, _ in first.calls}), 4)
        connection = sqlite3.connect(self.history)               # pretend two hours have passed
        two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        with connection:
            connection.execute("UPDATE searches SET at_utc = ?", (two_hours_ago,))
        connection.close()
        code, second = self.sweep([page([item(str(i))]) for i in range(10, 20)])
        self.assertEqual(code, 0)
        self.assertEqual(len(second.calls), 2)                    # only the queries not searched yet
        self.assertEqual({q for q, _ in first.calls} | {q for q, _ in second.calls}, all_queries)
        report = sorted(self.output.glob("query_report_*.xlsx"), key=lambda p: p.stat().st_mtime)[-1]
        rows = list(load_workbook(report)["Queries"].iter_rows(values_only=True))
        verdicts = [dict(zip(rows[0], r))["Verdict"] for r in rows[1:]]
        self.assertEqual(len(verdicts), len(all_queries))
        self.assertNotIn("not searched yet", verdicts)
        self.assertIn("no platform links (see Examples)", verdicts)   # comeet queries got greenhouse links


class LimitNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.history, self.output = root / "data" / "history.sqlite3", root / "results"
        self.search = root / "search.toml"
        self.search.write_text(SEARCH.read_text(encoding="utf-8").replace("monthly_limit = 250", "monthly_limit = 2"),
                               encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def main(self, command, responses=(), fake=None, extra=(), **account):
        fake, out, err = fake or FakeClient(responses, **account), io.StringIO(), io.StringIO()
        args = [command, "--positions", str(POSITIONS), "--search-config", str(self.search), "--history", str(self.history),
                "--output", str(self.output), "--env-file", str(Path(self.temp.name) / "missing.yaml"), *extra]
        with patch.object(cli, "SerpApiClient", return_value=fake), patch.object(cli, "SerperClient", return_value=fake), \
                patch.object(cli, "fetch_page", fake_pages({})), \
                patch.object(serpapi, "urlopen", side_effect=AssertionError("network")), \
                patch.dict("os.environ", {"SERPAPI_API_KEY": "fixture-secret", "SERPER_API_KEY": "fixture-secret"}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = app.main(args)
        return code, fake, out.getvalue(), err.getvalue()

    def test_user_is_told_when_the_monthly_limit_is_reached(self):
        code, _, out, _ = self.main("preview")
        self.assertEqual(code, 0)
        self.assertIn("MONTHLY SEARCH LIMIT NEAR: only 2 of 2", out)        # warned before spending
        code, fake, out, _ = self.main("run", [page([]), page([])])
        self.assertEqual((code, len(fake.calls)), (0, 2))
        self.assertIn("MONTHLY SEARCH LIMIT REACHED: 2 of 2", out)
        self.assertIn("restarts on", out)
        self.assertIn("Need more searches?", out)
        code, fake, _, err = self.main("run", used=2)                       # next run: no search, same message
        self.assertEqual((code, fake.calls), (1, []))
        self.assertIn("MONTHLY SEARCH LIMIT REACHED", err)

    def use_limit(self, monthly):
        self.search.write_text(SEARCH.read_text(encoding="utf-8").replace("monthly_limit = 250",
                                                                          f"monthly_limit = {monthly}"), encoding="utf-8")

    def test_a_paid_plan_stops_before_any_search(self):
        self.use_limit(250)
        code, fake, _, err = self.main("run", [page([])] * 6, price=75.0)
        self.assertEqual((code, fake.calls), (1, []))
        self.assertIn("NO SEARCH SENT", err)
        self.assertEqual(self.main("account", price=75.0)[0], 1)
        self.search.write_text(self.search.read_text(encoding="utf-8").replace(
            "[budget]\n", "[budget]\nallow_paid_plan = true\n", 1), encoding="utf-8")
        code, fake, _, _ = self.main("run", [page([])] * 6, price=75.0)     # only after the user allows it
        self.assertEqual((code, len(fake.calls)), (0, 6))

    def test_an_unclear_account_answer_stops_before_any_search(self):
        answer = {"account_id": "a", "plan_monthly_price": 0, "this_month_usage": 1, "plan_searches_left": 9,
                  "api_key": "fixture-secret", "account_email": "someone@example.com"}
        kept = app.parse_account(answer)
        self.assertEqual((kept.month_used, kept.plan_left), (1, 9))
        self.assertNotIn("fixture-secret", repr(kept))                      # the key and email are not kept
        self.assertNotIn("example.com", repr(kept))
        for missing in ("account_id", "plan_monthly_price", "this_month_usage", "plan_searches_left"):
            broken = {k: v for k, v in answer.items() if k != missing}
            with self.assertRaises(app.ProviderError):
                app.parse_account(broken)
        self.use_limit(250)
        fake = FakeClient([page([])])
        fake.account = lambda: app.parse_account({"account_id": "a"})
        with patch.object(cli, "SerpApiClient", return_value=fake), \
                patch.dict("os.environ", {"SERPAPI_API_KEY": "fixture-secret"}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = app.main(["run", "--positions", str(POSITIONS), "--search-config", str(self.search),
                             "--history", str(self.history), "--output", str(self.output),
                             "--env-file", str(Path(self.temp.name) / "missing.yaml")])
        self.assertEqual((code, fake.calls), (1, []))

    def test_a_run_never_goes_past_the_searches_left_on_the_plan(self):
        self.use_limit(250)                                                 # extra credits exist but are not used
        code, fake, out, _ = self.main("run", [page([])] * 6, used=248, left=2)
        self.assertEqual((code, len(fake.calls)), (0, 2))
        self.assertIn("MONTHLY SEARCH LIMIT REACHED: 250 of 250", out)

    def test_cached_searches_do_not_use_up_the_budget(self):
        self.use_limit(250)
        code, fake, out, _ = self.main("run", [page([])] * 6, used=100, left=150, cached=True)
        self.assertEqual((code, len(fake.calls)), (0, 6))
        self.assertIn("SerpApi counted 0 of the 6 searches sent", out)
        self.assertIn("Searches used this month: 100 of 250", out)

    def test_a_quick_rerun_waits_for_serpapis_count_before_spending(self):
        self.use_limit(250)
        self.assertEqual(len(self.main("run", [page([])] * 2, left=2)[1].calls), 2)
        stale = FakeClient([page([])] * 2, used=0, left=2)                  # the last 2 searches aren't counted yet

        def count_arrives(_seconds):
            stale.used, stale.left = 2, 0
        with patch.object(cli, "COUNT_SETTLE_SECONDS", 60), patch.object(cli.time, "sleep", side_effect=count_arrives):
            code, _, _, err = self.main("run", fake=stale)
        self.assertEqual((code, stale.calls), (1, []))
        self.assertIn("MONTHLY SEARCH LIMIT REACHED", err)

    def test_the_count_is_read_again_until_it_shows_the_searches(self):
        self.use_limit(250)
        fake, reads = FakeClient([page([])] * 6, used=10, left=100), []
        real = fake.account

        def lagging():
            reads.append(1)
            return replace(real(), month_used=10, plan_left=100) if len(reads) == 2 else real()
        fake.account = lagging
        with patch.object(cli, "COUNT_WAITS", (0,)):
            _, _, out, _ = self.main("run", fake=fake)
        self.assertIn("SerpApi counted 6 of the 6 searches sent.", out)

    def test_serper_searches_are_counted_apart_from_serpapi(self):
        self.use_limit(250)
        serper_run = {"extra": ("--source", "serper"), "provider": "serper", "account_id": "serper:fixture"}
        code, fake, out, _ = self.main("run", [page([])] * 6, left=2495, **serper_run)
        self.assertEqual((code, len(fake.calls)), (0, 6))
        self.assertIn("Serper counted 6 of the 6 searches sent. Credits left: 2489.", out)
        memory = app.ResultStore(self.history)
        try:
            self.assertEqual((memory.searches_this_month(), memory.searches_this_month(provider="serper")), (0, 6))
        finally:
            memory.close()

    def test_the_source_setting_picks_the_service_and_the_flag_wins(self):
        self.use_limit(250)
        self.search.write_text(self.search.read_text(encoding="utf-8").replace(
            "[google]\n", '[google]\nsource = "serper"\n', 1), encoding="utf-8")
        code, fake, out, _ = self.main("run", [page([])] * 6, left=2495, provider="serper", account_id="serper:fixture")
        self.assertEqual((code, len(fake.calls)), (0, 6))
        self.assertIn("Serper counted 6 of the 6 searches sent.", out)            # no --source needed
        code, fake, out, _ = self.main("run", [page([])] * 6, extra=("--source", "serpapi"))
        self.assertEqual((code, len(fake.calls)), (0, 6))
        self.assertIn("SerpApi counted 6 of the 6 searches sent.", out)
        self.search.write_text(self.search.read_text(encoding="utf-8").replace(
            'source = "serper"', 'source = "bing"'), encoding="utf-8")
        with self.assertRaises(app.ConfigError):
            app.load_settings(POSITIONS, self.search)

    def test_serper_never_spends_past_its_credits_or_on_bought_credits(self):
        self.use_limit(250)
        serper_run = {"extra": ("--source", "serper"), "provider": "serper", "account_id": "serper:fixture"}
        code, fake, _, err = self.main("run", [page([])] * 6, left=0, **serper_run)
        self.assertEqual((code, fake.calls), (1, []))
        self.assertIn("no credits left", err)
        code, fake, _, err = self.main("run", [page([])] * 6, left=3000, bought=True, **serper_run)
        self.assertEqual((code, fake.calls), (1, []))
        self.assertIn("NO SEARCH SENT", err)
        code, fake, _, _ = self.main("run", [page([])] * 6, left=1, **serper_run)
        self.assertEqual((code, len(fake.calls)), (0, 1))

    def test_searches_are_counted_per_key_and_old_rows_are_kept(self):
        self.use_limit(250)
        self.history.parent.mkdir(parents=True)
        old = sqlite3.connect(self.history)                                 # a memory file from before this change
        with old:
            old.execute("CREATE TABLE searches (run_id TEXT NOT NULL, query TEXT NOT NULL, start INTEGER NOT NULL, "
                        "at_utc TEXT NOT NULL, outcome TEXT NOT NULL)")
            old.execute("INSERT INTO searches VALUES ('old', 'q', 0, ?, 'ok')", (common.utc_now(),))
        old.close()
        self.main("run", [page([])] * 6, account_id="key-a")
        code, _, out, _ = self.main("account", account_id="key-b")
        self.assertEqual(code, 0)
        self.assertIn("This key: 0", out)
        self.assertIn("Before keys were recorded: 1", out)
        self.assertIn("Other keys: 6", out)
        self.assertNotIn("key-a", out)                                      # account ids are not printed

    def test_user_is_told_when_serpapi_refuses_a_search(self):
        code, fake, out, _ = self.main("run", [app.ProviderError("out of searches", fatal=True, quota=True)])
        self.assertEqual((code, len(fake.calls)), (2, 1))
        self.assertIn("SERPAPI SEARCH LIMIT", out)
        self.assertIn("made 0 of", out)

    def test_notice_names_the_limit_that_applies(self):
        settings = app.load_settings(POSITIONS, SEARCH)                     # 250 a month, 50 an hour
        self.assertEqual(app.limit_notice(10, 10, settings, 16, 16, after=True), "")
        self.assertIn("HOURLY SEARCH LIMIT", app.limit_notice(10, 50, settings, 16, 4, after=True))
        self.assertIn("HOURLY SEARCH LIMIT", app.limit_notice(10, 45, settings, 16, 5))
        self.assertIn("only 10 of 250", app.limit_notice(240, 16, settings, 16, 16, after=True))
        december = datetime(2026, 12, 31, tzinfo=timezone.utc)
        self.assertIn("restarts on 2027-01-01", app.limit_notice(250, 0, settings, 16, 0, after=True, now=december))


class SerperClientTests(unittest.TestCase):
    def client(self):
        return serper.SerperClient("fixture-secret", "il", "en", 12)

    def answer(self, payload):
        return patch.object(serper, "urlopen", return_value=io.BytesIO(json.dumps(payload).encode()))

    def test_search_request_and_answer_shape(self):
        found = [item(str(i)) for i in range(10)]
        with self.answer({"organic": found, "credits": 1}) as mocked:
            payload = self.client().search("site:greenhouse.io architect", 10)
        request = mocked.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual((request.get_method(), request.full_url), ("POST", serper.SEARCH_URL))
        self.assertEqual((body["page"], body["num"], body["gl"], body["hl"]), (2, 10, "il", "en"))
        self.assertNotIn("fixture-secret", request.full_url + request.data.decode())   # the key goes in a header only
        self.assertEqual(payload["organic_results"], found)
        self.assertEqual(app.next_offset(payload, 10), 20)
        with self.answer({"organic": found[:3]}):
            self.assertIsNone(app.next_offset(self.client().search("q"), 0))

    def test_google_redirect_links_are_marked_for_following(self):
        link = "https://www.google.com/goto?url=CAES"
        with self.answer({"organic": [{"title": "t", "link": link}, item("7")]}):
            results = self.client().search("site:comeet.com/jobs manager")["organic_results"]
        self.assertEqual(results[0]["displayed_link"], "comeet.com/jobs")
        self.assertNotIn("displayed_link", results[1])

    def test_errors_stop_the_run_and_never_show_the_key(self):
        url = "https://google.serper.dev/search"
        cases = ((400, b'{"message": "Not enough credits"}', True), (429, b"{}", True), (403, b"{}", False))
        for code, body, quota in cases:
            failure = HTTPError(url, code, "fixture-secret", {}, io.BytesIO(body))
            with self.subTest(code=code), patch.object(serper, "urlopen", side_effect=failure):
                with self.assertRaises(app.ProviderError) as caught:
                    self.client().search("q")
                self.assertTrue(caught.exception.fatal)
                self.assertEqual(caught.exception.quota, quota)
                self.assertNotIn("fixture-secret", str(caught.exception))

    def test_account_reads_the_balance_and_refuses_an_unclear_answer(self):
        with self.answer({"balance": 2495, "rateLimit": 5}):
            account = self.client().account()
        self.assertEqual((account.plan_left, account.provider, account.paid), (2495, "serper", False))
        self.assertNotIn("fixture-secret", repr(account))
        with self.answer({"balance": 2501}):
            self.assertTrue(self.client().account().paid)                   # more than the free credits: some were bought
        for broken in ({}, {"balance": "many"}, {"balance": -1}, {"balance": True}):
            with self.subTest(broken=broken), self.answer(broken), self.assertRaises(app.ProviderError):
                self.client().account()


class OtherServicesTests(unittest.TestCase):
    TAVILY = {"account": {"current_plan": "Researcher", "plan_usage": 15, "plan_limit": 1000, "paygo_usage": 0}}

    def lines(self, tavily, serper, keys=("TAVILY_API_KEY", "SERPER_API_KEY")):
        self.requests = []

        def answer(request, timeout=0):
            self.requests.append(request)
            found = tavily if "tavily" in request.full_url else serper
            if isinstance(found, BaseException):
                raise found
            return io.BytesIO(json.dumps(found).encode())
        values = {name: "fixture-secret" if name in keys else "" for name in ("TAVILY_API_KEY", "SERPER_API_KEY")}
        with patch.object(usage, "urlopen", side_effect=answer), patch.dict("os.environ", values):
            return usage.other_services(5)

    def test_usage_is_read_without_sending_a_search(self):
        self.assertEqual(self.lines(self.TAVILY, {"balance": 2495}),
                         ["Tavily: Researcher plan, 15 of 1000 credits used, 985 left, pay-as-you-go used: 0",
                          "Serper: 2495 credits left"])
        self.assertEqual([(r.get_method(), r.full_url) for r in self.requests],
                         [("GET", usage.TAVILY_URL), ("GET", usage.SERPER_URL)])

    def test_pay_as_you_go_use_is_a_warning(self):
        charged = {"account": {**self.TAVILY["account"], "paygo_usage": 3}}
        self.assertIn("WARNING: 3 pay-as-you-go credits", self.lines(charged, {"balance": 1})[0])
        unclear = {"account": {k: v for k, v in self.TAVILY["account"].items() if k != "paygo_usage"}}
        self.assertIn("pay-as-you-go use unknown", self.lines(unclear, {"balance": 1})[0])

    def test_a_failing_service_stops_nothing_and_never_shows_the_key(self):
        refused = HTTPError("https://api.tavily.com/usage", 401, "fixture-secret", {}, None)
        lines = self.lines(refused, {"balance": 7})
        self.assertEqual(lines, ["Tavily: unavailable (HTTP 401)", "Serper: 7 credits left"])
        self.assertNotIn("fixture-secret", " ".join(lines))
        self.assertEqual(self.lines({}, {"other": 1})[1], "Serper: unavailable (unexpected answer)")
        self.assertEqual(self.lines(self.TAVILY, {"balance": 7}, keys=("SERPER_API_KEY",))[0], "Tavily: no key set")
        self.assertEqual(len(self.requests), 1)                             # no request without a key


if __name__ == "__main__":
    unittest.main()
