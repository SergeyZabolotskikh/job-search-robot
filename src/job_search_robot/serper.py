"""Google search through Serper, answered in the shape the collector reads. Errors never echo the API key."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .collector import GOOGLE_REDIRECT
from .common import VERSION, ProviderError
from .serpapi import Account
from .usage import SERPER_URL as ACCOUNT_URL

SEARCH_URL = "https://google.serper.dev/search"
RESULTS_PER_PAGE = 10        # 10 results = 1 credit
FREE_CREDITS = 2500          # a higher balance means some credits were bought
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _error_text(exc: HTTPError) -> str:
    try:
        return exc.read(2000).decode("utf-8", "replace").casefold()
    except (OSError, ValueError, AttributeError):
        return ""


class SerperClient:
    def __init__(self, api_key: str, country: str = "il", language: str = "en", timeout: int = 30):
        self.api_key, self.country, self.language, self.timeout = api_key, country, language, timeout

    def search(self, query: str, start: int = 0, tbs: str = "") -> dict:
        body = {"q": query, "gl": self.country, "hl": self.language, "num": RESULTS_PER_PAGE,
                "page": start // RESULTS_PER_PAGE + 1}
        if tbs:
            body["tbs"] = tbs
        payload = self._send(SEARCH_URL, json.dumps(body).encode("utf-8"))
        organic = payload.get("organic")
        if not isinstance(organic, list):
            if "credit" in str(payload.get("message", "")).casefold():
                raise ProviderError("Serper has no credits left. Check your account.", fatal=True, quota=True)
            raise ProviderError("Serper's answer had no results list.")
        site = re.search(r"\bsite:(\S+)", query)
        results = []
        for item in organic:  # the collector follows a Google redirect link only if it is shown on the platform
            link = item.get("link") if isinstance(item, dict) else None
            redirect = isinstance(link, str) and GOOGLE_REDIRECT.match(link)
            results.append({**item, "displayed_link": site.group(1)} if redirect and site else item)
        answer = {"organic_results": results}
        if len(organic) >= RESULTS_PER_PAGE:
            answer["serpapi_pagination"] = {"next": f"{SEARCH_URL}?start={start + RESULTS_PER_PAGE}"}
        return answer

    def account(self) -> Account:
        """Credits left. Reading them sends no search. The id is a short hash, so the key itself is never stored."""
        balance = self._send(ACCOUNT_URL).get("balance")
        if isinstance(balance, bool) or not isinstance(balance, int) or balance < 0:
            raise ProviderError("Serper's account answer was incomplete, so no search was sent.", fatal=True)
        identity = "serper:" + hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()[:12]
        return Account(identity, "Serper credits", 0.0, 0, balance, provider="serper",
                       bought_credits=balance > FREE_CREDITS)

    def _send(self, url: str, data: bytes | None = None) -> dict:
        headers = {"X-API-KEY": self.api_key, "User-Agent": f"JobSearchRobot/{VERSION}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            detail = _error_text(exc)
            if exc.code in (402, 429) or "credit" in detail:
                raise ProviderError("Serper has no credits left or hit its rate limit. Check your account.",
                                    fatal=True, quota=True) from None
            if exc.code in (401, 403):
                raise ProviderError("Serper rejected the API key.", fatal=True) from None
            raise ProviderError(f"Serper returned HTTP {exc.code}. This request was not retried.") from None
        except (URLError, TimeoutError, OSError):
            raise ProviderError("Network connection failed or timed out. This request was not retried.") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("Serper's response exceeded the size limit.")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ProviderError("Serper returned invalid JSON.") from None
        if not isinstance(payload, dict):
            raise ProviderError("Serper returned an unexpected response format.")
        return payload
