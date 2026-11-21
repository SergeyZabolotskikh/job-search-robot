"""Google search through SerpApi. Errors never echo the API key."""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import Request, urlopen

from .common import VERSION, ProviderError

API_URL = "https://serpapi.com/search.json"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class SerpApiClient:
    def __init__(self, api_key: str, country: str = "il", language: str = "en", timeout: int = 30):
        self.api_key, self.country, self.language, self.timeout = api_key, country, language, timeout

    def search(self, query: str, start: int = 0, tbs: str = "") -> dict:
        params = {"engine": "google", "q": query, "start": start, "gl": self.country,
                  "hl": self.language, "api_key": self.api_key, "output": "json"}
        if tbs:
            params["tbs"] = tbs  # e.g. qdr:w = pages indexed in the past week
        request = Request(API_URL + "?" + urlencode(params), headers={"User-Agent": f"JobSearchRobot/{VERSION}"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise ProviderError("SerpApi rejected the API key or account permissions.", fatal=True) from None
            if exc.code in (402, 429):
                raise ProviderError("SerpApi quota, billing, or rate limit reached. Check your account before rerunning.",
                                    fatal=True, quota=True) from None
            raise ProviderError(f"SerpApi returned HTTP {exc.code}. This request was not retried.") from None
        except (URLError, TimeoutError, OSError):
            # urllib exceptions can contain the full request URL and API key.
            raise ProviderError("Network connection failed or timed out. This request was not retried.") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("SerpApi response exceeded the size limit.")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ProviderError("SerpApi returned invalid JSON.") from None
        if not isinstance(payload, dict):
            raise ProviderError("SerpApi returned an unexpected response format.")
        if payload.get("error"):
            message = str(payload["error"]).casefold()
            if "hasn't returned any results" in message or "has not returned any results" in message:
                return {"organic_results": []}
            if "api key" in message or "api_key" in message:
                raise ProviderError("SerpApi rejected the API key.", fatal=True)
            if any(term in message for term in ("quota", "credit", "limit", "out of searches", "run out")):
                raise ProviderError("SerpApi quota or account limit reached. Check your account.", fatal=True, quota=True)
            raise ProviderError("SerpApi reported a search error. See your SerpApi dashboard for details.")
        metadata = payload.get("search_metadata", {})
        if isinstance(metadata, dict) and metadata.get("status") in ("Processing", "Error"):
            raise ProviderError("SerpApi did not finish this search successfully.")
        if "organic_results" not in payload and not (isinstance(metadata, dict) and metadata.get("status") == "Success"):
            raise ProviderError("SerpApi response did not include search results or a successful completion status.")
        if not isinstance(payload.get("organic_results", []), list):
            raise ProviderError("SerpApi organic results had an unexpected format.")
        return payload


def next_offset(payload: dict, current: int) -> int | None:
    for key in ("serpapi_pagination", "pagination"):
        pagination = payload.get(key)
        if not isinstance(pagination, dict):
            continue
        link = pagination.get("next") or pagination.get("next_link")
        if not isinstance(link, str):
            continue
        try:
            value = int(dict(parse_qsl(urlsplit(link).query)).get("start", ""))
            if current < value <= 10000:
                return value
        except (ValueError, TypeError):
            continue
    return None
