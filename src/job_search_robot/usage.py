"""Read-only plan and credit numbers for the other search services. Sends no search; errors never echo a key."""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .common import VERSION

TAVILY_URL = "https://api.tavily.com/usage"
SERPER_URL = "https://google.serper.dev/account"  # not in the public docs we could read; answered on 2026-11-22
MAX_RESPONSE_BYTES = 1024 * 1024


class UsageUnavailable(Exception):
    """A service gave no usable answer. The message never contains the key."""


def _get(url: str, headers: dict, timeout: int) -> dict:
    request = Request(url, headers={"User-Agent": f"JobSearchRobot/{VERSION}", "Accept": "application/json", **headers})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(MAX_RESPONSE_BYTES))
    except HTTPError as exc:
        raise UsageUnavailable(f"HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise UsageUnavailable("network error") from None
    except (ValueError, UnicodeError):
        raise UsageUnavailable("unexpected answer") from None
    if not isinstance(payload, dict):
        raise UsageUnavailable("unexpected answer")
    return payload


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def tavily_line(key: str, timeout: int) -> str:
    account = _get(TAVILY_URL, {"Authorization": f"Bearer {key}"}, timeout).get("account")
    if not isinstance(account, dict) or not _is_number(account.get("plan_usage")):
        raise UsageUnavailable("unexpected answer")
    used, limit, paygo = account["plan_usage"], account.get("plan_limit"), account.get("paygo_usage")
    credits = (f"{_number(used)} of {_number(limit)} credits used, {_number(max(0, limit - used))} left"
               if _is_number(limit) else f"{_number(used)} credits used")
    if not _is_number(paygo):
        charged = "pay-as-you-go use unknown"
    elif paygo > 0:
        charged = f"WARNING: {_number(paygo)} pay-as-you-go credits used, and those are charged"
    else:
        charged = "pay-as-you-go used: 0"
    return f"{account.get('current_plan') or 'unknown'} plan, {credits}, {charged}"


def serper_line(key: str, timeout: int) -> str:
    balance = _get(SERPER_URL, {"X-API-KEY": key}, timeout).get("balance")
    if not _is_number(balance):
        raise UsageUnavailable("unexpected answer")
    return f"{_number(balance)} credits left"


SERVICES = (("Tavily", "TAVILY_API_KEY", tavily_line), ("Serper", "SERPER_API_KEY", serper_line))


def other_services(timeout: int) -> list[str]:
    """One line per service. A service that fails never stops the others."""
    lines = []
    for name, variable, read in SERVICES:
        key = os.environ.get(variable, "").strip()
        if not key:
            lines.append(f"{name}: no key set")
            continue
        try:
            lines.append(f"{name}: {read(key, timeout)}")
        except UsageUnavailable as exc:
            lines.append(f"{name}: unavailable ({exc})")
    return lines
