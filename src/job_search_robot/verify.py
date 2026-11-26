"""Check a lead on its own job page: open or closed, real location, work mode, date.

Plain HTTP request to the public page (no SerpApi credits). Each platform states the job's
details differently:
- Comeet: a POSITION_DATA JSON object; a closed job redirects to the company board.
- Greenhouse: the `job__location` element (or `job_post_location`) and `published_at`; closed = 404
  or board redirect. Links that move to the company's own site are read from Greenhouse's embedded copy.
- Ashby, Lever, Workable and most company sites: a schema.org JobPosting block (JSON-LD).
- SmartRecruiters and some company sites: the same JobPosting as microdata (`itemprop` tags).
- Workday: never requested (it refused the tool with HTTP 429, and its page is built by JavaScript);
  the location comes from the job address. Open/closed and the posting date stay unknown.

Also resolves Google result redirect links (google.com/goto?url=...) to the page address.
"""
from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from .common import VERSION
from .config import Settings
from .urls import workday_place
from .workbook import mentions_israel

MAX_BYTES = 3 * 1024 * 1024
USER_AGENT = f"Mozilla/5.0 (compatible; JobSearchRobot/{VERSION}; personal job search)"
ISRAEL_CODES = {"il", "isr", "israel"}
PLATFORM_HOSTS = (".greenhouse.io", "comeet.com", "ashbyhq.com", "lever.co", "workable.com",
                  "smartrecruiters.com", "myworkdayjobs.com")
WORKDAY_NOTE = "Workday: location from the job address; page not read"
# "Hybrid" or "Remote / On-site" names no place, so it can't show that a job is abroad.
WORK_MODE_ONLY = re.compile(r"(?:[\s,;/|&()+–-]|remote|hybrid|on-?site|in[- ]?office|office|flexible|"
                            r"telecommute|anywhere)+", re.I)


@dataclass(frozen=True)
class PageCheck:
    status: str               # open | closed | unknown
    location: str = ""        # as stated on the page
    in_israel: bool | None = None   # None = the page names no place
    work_mode: str = ""       # remote | hybrid | onsite | ""
    posted: str = ""          # YYYY-MM-DD (Comeet: last update)
    final_url: str = ""
    note: str = ""

    @property
    def not_read(self) -> bool:
        """The page couldn't be read this time (network error, blocked, too many requests, server
        error), so this check says nothing new about the job."""
        return self.status == "unknown" and self.note.startswith(("HTTP ", "page could not"))


def fetch_page(url: str, timeout: int = 15) -> tuple[int, str, str]:
    """(HTTP status, final URL after redirects, page text). Status 0 = network failure."""
    try:
        with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout) as response:
            return response.status, response.geturl(), response.read(MAX_BYTES).decode("utf-8", "replace")
    except HTTPError as exc:
        return exc.code, url, ""
    except (URLError, TimeoutError, OSError, ValueError):
        return 0, url, ""


class _ReportRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # don't follow: the redirect surfaces as an HTTPError carrying its Location


open_no_redirect = build_opener(_ReportRedirect).open  # tests replace it, like urlopen


def resolve_redirect(url: str, timeout: int = 15) -> tuple[str | None, str]:
    """Where a Google result link (google.com/goto?url=... or /url?...) leads: (its Location header, "")
    or (None, why not). One plain HTTP request to Google (no SerpApi credits)."""
    try:
        with open_no_redirect(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout) as response:
            return None, f"HTTP {getattr(response, 'status', '?')}, no redirect"
    except HTTPError as exc:
        location = exc.headers.get("Location") if 300 <= exc.code < 400 and exc.headers else None
        return (urljoin(url, location), "") if location else (None, f"HTTP {exc.code}")
    except (URLError, TimeoutError, OSError, ValueError):
        return None, "network error"


def check_page(url: str, platform: str, settings: Settings, fetch=fetch_page, context: str = "") -> PageCheck:
    """`context` is Google's title and snippet for the lead; only Workday uses it."""
    if platform == "Workday":
        return _workday(url, context, settings)
    status, final, page = fetch(url)
    start_host, final_host = urlsplit(url).hostname or "", urlsplit(final).hostname or ""
    note = "company site" if final_host and final_host != start_host and not final_host.endswith(
        PLATFORM_HOSTS) else ""
    if platform == "Greenhouse" and (note or status not in (200, 404, 410)):
        embedded = _greenhouse_embed(url, settings, fetch, final, note)
        if embedded:
            return embedded
    if status in (404, 410):
        return PageCheck("closed", final_url=final, note=f"HTTP {status}")
    if status != 200:
        return PageCheck("unknown", final_url=final, note=f"HTTP {status}" if status else "page could not be loaded")
    if platform == "Comeet":
        if len(_segments(final)) < 5 and len(_segments(url)) >= 5:
            return PageCheck("closed", final_url=final, note="redirected to the company board")
        found = _comeet(page, settings)
    elif platform == "Greenhouse" and final_host.endswith("greenhouse.io"):
        if "error=true" in final or "/jobs/" not in urlsplit(final).path:
            return PageCheck("closed", final_url=final, note="redirected to the company board")
        found = _greenhouse(page, settings)
    elif platform == "SmartRecruiters":
        # Assumed like Comeet (no closed SmartRecruiters job seen yet): a closed job lands on the company page.
        if len(_segments(final)) < 2 <= len(_segments(url)):
            return PageCheck("closed", final_url=final, note="redirected to the company board")
        found = None
    else:
        found = None
    found = found or _json_ld(page, settings) or _microdata(page, settings)
    if found is None:
        return PageCheck("unknown", final_url=final, note=note or "no job details on the page")
    location, in_israel, work_mode, posted = found
    return PageCheck("open", location, in_israel, work_mode, posted, final, note)


def _greenhouse_embed(url: str, settings: Settings, fetch, final: str, note: str) -> PageCheck | None:
    """Greenhouse's embedded copy of the job (checked 2026-11-17). Used when the job link moves to the
    company's own site, or the board answers with an error: the copy still states the location,
    and it moves to the company board (error=true) when the job is closed. None = no answer."""
    match = re.match(r"https://(job-boards(?:\.eu)?\.greenhouse\.io)/([^/?#]+)/jobs/(\d+)", url)
    if not match:
        return None
    host, company, job_id = match.groups()
    status, embed_final, page = fetch(f"https://{host}/embed/job_app?" + urlencode({"for": company, "token": job_id}))
    if status in (404, 410) or (status == 200 and ("error=true" in embed_final
                                                   or "/embed/job_app" not in embed_final)):
        return PageCheck("closed", final_url=final, note="removed from Greenhouse")
    found = _greenhouse(page, settings) if status == 200 else None
    if found is None:
        return None
    location, in_israel, work_mode, posted = found
    return PageCheck("open", location, in_israel, work_mode, posted, final, note)


def _workday(url: str, context: str, settings: Settings) -> PageCheck:
    """.../job/<location>/<title>_<id>: the location segment names the job's first location only, so a
    foreign one counts as abroad only when Google's title/snippet doesn't mention Israel either.
    The note is not one that page checks retry, so Workday is never asked."""
    place = workday_place(url)
    if not place:
        return PageCheck("unknown", final_url=url, note=WORKDAY_NOTE)
    words = " ".join(re.split(r"[-_+]+", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", place))).strip()  # RehovotISR
    # "ISR" but not "IL": in US addresses IL is Illinois.
    in_israel = mentions_israel(words, settings) or "isr" in words.lower().split()
    if not in_israel and (mentions_israel(context, settings) or WORK_MODE_ONLY.fullmatch(words)):
        in_israel = None
    return PageCheck("unknown", f"{words} (from the address)", in_israel, _mode(words), "", url, WORKDAY_NOTE)


def _segments(url: str) -> list[str]:
    return [s for s in urlsplit(url).path.split("/") if s]


def _israel(location: str, countries: list[str], settings: Settings) -> bool | None:
    if not countries and (not location.strip() or WORK_MODE_ONLY.fullmatch(location)):
        return None
    return any(c.strip().lower() in ISRAEL_CODES for c in countries) or mentions_israel(location, settings)


def _mode(text: str) -> str:
    text = text.lower()
    if "hybrid" in text:
        return "hybrid"
    if "remote" in text or "telecommute" in text:
        return "remote"
    if "on-site" in text or "onsite" in text or "office" in text:
        return "onsite"
    return ""


def _comeet(page: str, settings: Settings):
    match = re.search(r"POSITION_DATA\s*=\s*(\{.*?\});\s*\n", page, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    loc = data.get("location") if isinstance(data.get("location"), dict) else {}
    parts = [str(loc.get(k)) for k in ("city", "name", "country") if loc.get(k)]
    location = ", ".join(dict.fromkeys(parts))
    country = [str(loc.get("country"))] if loc.get("country") else []
    mode = _mode(str(data.get("workplace_type") or ""))
    return location, _israel(location, country, settings), mode, str(data.get("time_updated") or "")[:10]


def _greenhouse(page: str, settings: Settings):
    match = re.search(r'class="job__location".*?</svg>(.*?)</div>', page, re.S)
    if match:
        location = html_lib.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))
    else:  # some pages state it only in the page data
        match = re.search(r'"job_post_location"\s*:\s*("(?:[^"\\]|\\.)*")', page)
        if not match:
            return None
        try:
            location = json.loads(match.group(1))
        except ValueError:
            return None
    location = " ".join(location.split())
    if not location:
        return None
    posted = re.search(r'"published_at"\s*:\s*"(\d{4}-\d{2}-\d{2})', page)
    return location, _israel(location, [], settings), _mode(location), posted.group(1) if posted else ""


def _json_ld(page: str, settings: Settings):
    for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return _job_posting(item, settings)
    return None


def _microdata(page: str, settings: Settings):
    """A JobPosting as microdata (SmartRecruiters, checked 2026-11-18): <meta itemprop="addressLocality"
    content="...">, addressRegion, addressCountry inside each jobLocation, and datePosted."""
    if not re.search(r'itemtype="https?://schema\.org/JobPosting"', page, re.I):
        return None
    starts = [m.end() for m in re.finditer(r'itemprop="jobLocation"', page)]
    locations = []
    for start, end in zip(starts, starts[1:] + [len(page)]):
        address: dict = {}
        for prop, value in _meta_props(page[start:min(end, start + 3000)]):
            if prop.startswith("address"):
                address.setdefault(prop, value)
        if address:
            locations.append({"address": address})
    posted = next((value for prop, value in _meta_props(page) if prop == "datePosted"), "")
    found = _job_posting({"jobLocation": locations, "datePosted": posted}, settings)
    workplace = re.search(r'workplaceType="(remote|hybrid|onsite)"', page, re.I)  # SmartRecruiters
    if found and workplace:
        found = (*found[:2], workplace.group(1).lower(), found[3])
    return found


def _meta_props(text: str):
    for tag in re.findall(r"<meta\b[^>]*>", text, re.I):
        prop, content = re.search(r'itemprop="([^"]+)"', tag), re.search(r'content="([^"]*)"', tag)
        if prop and content:
            yield prop.group(1), html_lib.unescape(content.group(1))


def _job_posting(item: dict, settings: Settings):
    places, countries = [], []
    raw = item.get("jobLocation")
    for loc in raw if isinstance(raw, list) else [raw]:
        address = loc.get("address") if isinstance(loc, dict) else None
        if isinstance(address, dict):
            country = address.get("addressCountry")
            country = country.get("name") if isinstance(country, dict) else country
            parts = [str(address.get(k)) for k in ("addressLocality", "addressRegion") if address.get(k)]
            if country:
                parts.append(str(country))
                countries.append(str(country))
            if parts:
                places.append(", ".join(dict.fromkeys(parts)))
    requirements = item.get("applicantLocationRequirements")
    for req in requirements if isinstance(requirements, list) else [requirements]:
        if isinstance(req, dict) and req.get("name"):
            countries.append(str(req["name"]))
            if not any(str(req["name"]) in place for place in places):
                places.append(f'remote from {req["name"]}')
    location = "; ".join(dict.fromkeys(places))
    if not location and not countries:
        return None
    mode = "remote" if str(item.get("jobLocationType", "")).upper() == "TELECOMMUTE" else _mode(location)
    return location, _israel(location, countries, settings), mode, str(item.get("datePosted") or "")[:10]
