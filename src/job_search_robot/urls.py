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
    elif host.endswith(".myworkdayjobs.com"):
        platform, company, kind, identity, canonical = _workday(host, segments)
    elif host == "jobs.smartrecruiters.com" and segments:
        # /<Company>/<digits>-<title>: the digits are the job ID; /<Company> alone is the company board.
        platform, company = "SmartRecruiters", segments[0]
        job = re.fullmatch(r"(\d{6,})(?:-.*)?", segments[1]) if len(segments) > 1 else None
        kind = "job" if job else "career_page"
        canonical = f"https://{host}/{company}" + (f"/{segments[1]}" if job else "")
        identity = f"smartrecruiters:{company.lower()}:{job.group(1)}" if job else canonical
    return {"identity": identity, "url": canonical, "result_type": kind,
            "platform": platform, "company_url_slug": company}


WORKDAY_LOCALE = re.compile(r"[a-z]{2}-[a-z]{2}", re.I)   # en-US, he-IL, us-en


def _workday(host: str, segments: list[str]) -> tuple[str, str, str, str, str]:
    """<company>.wd<N>.myworkdayjobs.com/[<locale>/]<site>/job/[<location>/]<title>_<jobId>[/apply/...].
    The same job appears with other locales, with or without the location, and with /apply or
    ?source=..., so its identity is the company and job ID only. Other pages (job searches) are the
    company's board: https://<host>/<site>. Returns (platform, company, kind, identity, url)."""
    company = host.split(".")[0]
    path = [s for s in segments if not WORKDAY_LOCALE.fullmatch(s)] if segments else []
    if "job" in path:
        start = path.index("job")
        title = next((i for i in range(start + 1, len(path)) if "_" in path[i]), None)
        if title is not None:
            job_id = path[title].rsplit("_", 1)[1]
            return ("Workday", company, "job", f"workday:{company.lower()}:{job_id.lower()}",
                    f"https://{host}/" + "/".join(segments[:segments.index(path[title]) + 1]))
    board = f"https://{host}/" + (path[0] if path and path[0] != "job" else "")
    return "Workday", company, "career_page", board, board


def workday_place(url: str) -> str:
    """The location segment of a Workday job address (.../job/<location>/<title>_<id>), or "" when the
    address has none. It is the only location the tool gets for a Workday job (verify._workday)."""
    try:
        path = [s for s in urlsplit(url).path.split("/") if s]
    except ValueError:
        return ""
    rest = path[path.index("job") + 1:] if "job" in path else []
    return rest[0] if len(rest) > 1 and "_" not in rest[0] else ""


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
