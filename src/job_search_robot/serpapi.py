"""Google search through SerpApi. Errors never echo the API key."""
from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import Request, urlopen

from .common import VERSION, ProviderError

API_URL = "https://serpapi.com/search.json"
ACCOUNT_URL = "https://serpapi.com/account.json"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Account:
    """A search service's own numbers for this key. The key and email in its answer are never kept."""
    account_id: str
    plan: str
    monthly_price: float
    month_used: int
    plan_left: int
    extra_credits: int = 0
    renewal: str = ""
    provider: str = "serpapi"
    bought_credits: bool = False

    @property
    def paid(self) -> bool:
        return self.monthly_price > 0 or self.bought_credits


def parse_account(payload: dict) -> Account:
    """An answer without a clear price or count is refused, so no search is sent on a guess."""
    identity, price, used, left = (payload.get(name) for name in (
        "account_id", "plan_monthly_price", "this_month_usage", "plan_searches_left"))
    if (not isinstance(identity, str) or not identity
            or isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0
            or any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in (used, left))):
        raise ProviderError("SerpApi's account answer was incomplete, so no search was sent.", fatal=True)
    extra = payload.get("extra_credits")
    renewal = payload.get("plan_renewal_date")
    return Account(identity, str(payload.get("plan_name") or "unknown plan"), float(price), used, left,
                   extra if isinstance(extra, int) and not isinstance(extra, bool) else 0,
                   renewal if isinstance(renewal, str) else "")


class SerpApiClient:
    def __init__(self, api_key: str, country: str = "il", language: str = "en", timeout: int = 30):
        self.api_key, self.country, self.language, self.timeout = api_key, country, language, timeout

    def search(self, query: str, start: int = 0, tbs: str = "") -> dict:
        params = {"engine": "google", "q": query, "start": start, "gl": self.country,
                  "hl": self.language, "api_key": self.api_key, "output": "json"}
        if tbs:
            params["tbs"] = tbs  # e.g. qdr:w = pages indexed in the past week
        payload = self._get(API_URL + "?" + urlencode(params))
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

    def account(self) -> Account:
        """Plan and searches left. Free: SerpApi doesn't count this request as a search."""
        payload = self._get(ACCOUNT_URL + "?" + urlencode({"api_key": self.api_key}))
        if payload.get("error"):
            raise ProviderError("SerpApi refused the account check. Check your API key.", fatal=True)
        return parse_account(payload)

    def _get(self, url: str) -> dict:
        request = Request(url, headers={"User-Agent": f"JobSearchRobot/{VERSION}"})
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
