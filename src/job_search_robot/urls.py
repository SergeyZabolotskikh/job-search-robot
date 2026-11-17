"""URL normalisation and stable job identity (the basis of "seen before")."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def canonical_result(url: str) -> dict | None:
    """Keep URL identity distinct from job title; careers pages are retained separately."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
            return None
        port = parts.port
    except ValueError:
        return None
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
        host = "job-boards.greenhouse.io"
    if host in ("boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"):  # Greenhouse EU data centre
        host = "job-boards.eu.greenhouse.io"
    if host == "www.comeet.com":
        host = "comeet.com"
    path = parts.path.rstrip("/") or "/"
    if host in ("jobs.lever.co", "apply.workable.com", "jobs.ashbyhq.com"):
        path = re.sub(r"/apply$", "", path)
    known_host = host in {"comeet.com", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com", "apply.workable.com"}
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
              if not k.lower().startswith("utm_") and not (known_host and k.lower() in
              {"gh_src", "lever-source", "lever-via", "source", "coref", "referrer"})
              and not (host == "jobs.ashbyhq.com" and k == "action" and v == "apply")]
    netloc = host + (f":{port}" if port and port not in (80, 443) else "")
    canonical = urlunsplit(("https", netloc, path, urlencode(sorted(params)), ""))
    segments = [part for part in path.split("/") if part]
    identity, kind, platform, company = canonical, "other_page", "Other", ""
    if host == "comeet.com" and segments and segments[0] == "jobs":
        platform = "Comeet"
        company = segments[1] if len(segments) > 1 else ""
        kind = "job" if len(segments) >= 5 else "career_page"
        if kind == "job":
            identity = f"comeet:{company.lower()}:{segments[-1].lower()}"
    elif host in ("job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"):
        platform, company = "Greenhouse", segments[0] if segments else ""
        job = re.search(r"/jobs/(\d+)(?:/|$)", path)
        kind = "job" if job else "career_page"
        if job:
            identity = f"greenhouse:{company.lower()}:{job.group(1)}"
    elif host in ("jobs.lever.co", "jobs.ashbyhq.com"):
        platform = "Lever" if host == "jobs.lever.co" else "Ashby"
        company = segments[0] if segments else ""
        job_id = segments[1] if len(segments) >= 2 else ""
        kind = "job" if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", job_id) else "career_page"
        if kind == "job":
            identity = f"{platform.lower()}:{company.lower()}:{job_id.lower()}"
    elif host == "apply.workable.com":
        platform, company = "Workable", segments[0] if segments else ""
        job = re.search(r"/j/([A-Za-z0-9]+)(?:/|$)", path)
        kind = "job" if job else "career_page"
        if job:
            identity = f"workable:{company.lower()}:{job.group(1).lower()}"
    return {"identity": identity, "url": canonical, "result_type": kind,
            "platform": platform, "company_url_slug": company}


def in_site(url: str, site: str) -> bool:
    """Google sometimes broadens queries; keep only the configured domain/path."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        site_host, _, prefix = site.lower().partition("/")
        host, site_host = host.removeprefix("www."), site_host.removeprefix("www.")
        if site_host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
            valid_host = host in ("boards.greenhouse.io", "job-boards.greenhouse.io")
        else:
            valid_host = host == site_host or host.endswith("." + site_host)
        path = parts.path.lower()
        wanted = "/" + prefix.rstrip("/")
        return valid_host and (not prefix or path == wanted or path.startswith(wanted + "/"))
    except ValueError:
        return False
