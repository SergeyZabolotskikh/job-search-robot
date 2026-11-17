"""Turn the role list and search settings into Google queries that fit the word limit."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

from .common import ConfigError
from .config import Family, Settings


@dataclass(frozen=True)
class SearchQuery:
    family_id: str
    family: str
    track: str
    priority: int
    order: int                 # position in positions.toml, for stable ordering
    sites: tuple[str, ...]
    titles: tuple[str, ...]
    text: str

    @property
    def key(self) -> str:
        """Stable identity used for rotation; changes when the query text changes."""
        return self.text

    @property
    def words(self) -> int:
        return count_words(self.text)

    @property
    def platform(self) -> str:
        return ", ".join(self.sites)

    def google_url(self, country: str = "", language: str = "", tbs: str = "") -> str:
        """Link for a manual check in Google, with the same country/language/date filter as SerpApi."""
        params = {"q": self.text, "hl": language, "gl": country, "tbs": tbs}
        return "https://www.google.com/search?" + urlencode({k: v for k, v in params.items() if v})


def count_words(text: str) -> int:
    """Count words as Google's query limit does (conservatively): each word, operator and site: term."""
    return sum(1 for token in re.findall(r'[^\s()"]+', text) if token != "-")


def or_group(values: list[str] | tuple[str, ...], quote: bool = True) -> str:
    items = [f'"{v}"' if quote else v for v in values]
    return items[0] if len(items) == 1 else "(" + " OR ".join(items) + ")"


def site_clause(sites: tuple[str, ...]) -> str:
    return or_group([f"site:{site}" for site in sites], quote=False)


def _chunks(values: tuple[str, ...], fits, limit: int) -> list[tuple[str, ...]]:
    """Greedily pack values into the fewest consecutive groups that satisfy fits(group)."""
    groups: list[tuple[str, ...]] = []
    current: tuple[str, ...] = ()
    for value in values:
        if current and fits(current + (value,)):
            current += (value,)
        else:
            if current:
                groups.append(current)
            if not fits((value,)):
                raise ConfigError(f'"{value}" does not fit into a query of {limit} words. '
                                  "Shorten it, reduce location.query_terms, or raise budget.max_query_words.")
            current = (value,)
    if current:
        groups.append(current)
    return groups


def build_queries(settings: Settings, *, families: list[str] | None = None,
                  tracks: list[str] | None = None) -> list[SearchQuery]:
    """One query per (title group, platform).

    Measured 2026-11-09 (task M4-4): Google ignores a combined (site:A OR site:B) clause and
    also drops site: from long queries (32 words), returning general web pages instead.
    A single site: with a short query (~14 words) is honoured. So: one platform per query,
    and titles packed only up to max_query_words.
    """
    selected = _select(settings, families, tracks)
    sites = tuple(p.site for p in settings.platforms if p.enabled)
    if not sites:
        raise ConfigError("Enable at least one platform in search.toml.")
    tail = [or_group(settings.query_terms)] + [f'-"{t}"' for t in settings.exclude_terms]
    tail_words = count_words(" ".join(tail))
    limit = settings.max_query_words
    queries, seen = [], set()
    for order, family in enumerate(settings.families):
        if family not in selected:
            continue
        # The site: term is one word for every platform, so the title groups are the same for all.
        def title_fits(group):
            return 1 + tail_words + count_words(or_group(group)) <= limit
        for titles in _chunks(family.titles, title_fits, limit):
            for site in sites:
                text = " ".join([f"site:{site}", or_group(titles), *tail])
                if text not in seen:
                    seen.add(text)
                    queries.append(SearchQuery(family.id, family.name, family.track, family.priority,
                                               order, (site,), titles, text))
    if not queries:
        raise ConfigError("No searches selected. Enable at least one family in positions.toml.")
    return queries


def _select(settings: Settings, families: list[str] | None, tracks: list[str] | None) -> list[Family]:
    if families:
        wanted = {f.casefold() for f in families}
        unknown = wanted - {f.id.casefold() for f in settings.families}
        if unknown:
            raise ConfigError(f"Unknown family id: {', '.join(sorted(unknown))}")
        chosen = [f for f in settings.families if f.id.casefold() in wanted]
    else:
        chosen = [f for f in settings.families if f.enabled]
    if tracks:
        unknown = set(tracks) - set(settings.tracks)
        if unknown:
            raise ConfigError(f"Unknown track: {', '.join(sorted(unknown))}")
        chosen = [f for f in chosen if f.track in tracks]
    return chosen


def revisit_days(priority: int, intervals: tuple[int, ...]) -> int:
    return intervals[min(priority, len(intervals)) - 1]


def plan_run(queries: list[SearchQuery], budget: int, last_run: dict[str, str] | None = None,
             intervals: tuple[int, ...] = (2, 4, 8), now: datetime | None = None) -> list[SearchQuery]:
    """Pick the queries for one run: most overdue first, at most `budget`.

    Each priority has a revisit interval in days (budget.revisit_days). A query's urgency is
    days since its last search divided by its interval; never-searched queries come first,
    by priority. When the budget is too small, every query is revisited less often in
    proportion, instead of low priorities never running.
    """
    last_run = last_run or {}
    now = now or datetime.now(timezone.utc)

    def urgency(q: SearchQuery):
        last = last_run.get(q.key)
        if not last:
            return (0, 0.0, q.priority, q.order)
        days = (now - datetime.fromisoformat(last)).total_seconds() / 86400
        return (1, -days / revisit_days(q.priority, intervals), q.priority, q.order)

    return sorted(queries, key=urgency)[:budget]


def monthly_demand(queries: list[SearchQuery], intervals: tuple[int, ...], pages: int = 1) -> int:
    """Searches per month needed to revisit every query on its interval."""
    return round(sum(30 / revisit_days(q.priority, intervals) for q in queries) * pages)
