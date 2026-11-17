# Design

How the tool works and why. Update this file when a decision changes.

## Goal

Run one command every ~2 days on your own computer. It searches Google (through SerpApi)
for job postings at Israeli companies on hiring platforms, and adds only leads it has
**not seen before** to an Excel workbook. Each lead is tagged with the track (CV) that fits.

Out of scope: applying, contacting recruiters, CV rewriting, LLM ranking, web UI,
scheduling, and platform (ATS) APIs.

## Decisions

| Topic | Decision |
| --- | --- |
| Search source | Google only, through SerpApi |
| Budget | SerpApi free plan: 250 searches/month, at most 50/hour; cached (≤ 1 h) and failed searches are not counted. ~16 searches per run. The tool counts every search and stops at the limits |
| Inputs | `sources/positions.toml` and `sources/search.toml`, private to each user (git-ignored), copied from the tracked `*.example.toml` files |
| Tracks | Each role family belongs to a track; the workbook shows the track's label, so the user knows which CV to send. The example has `management` and `ic` |
| Memory | SQLite `data/history.sqlite3`: the source of truth for "seen before", the user's Status/Note, page checks and the search counter |
| Output | One workbook `results/job_leads.xlsx`, plus `results/last_run.json` |
| Lead identity | The platform's job ID (Comeet, Greenhouse incl. its EU host, Lever, Ashby, Workable), else the cleaned URL. Identities stored under older rules are re-computed and merged; Status/Note move with them |
| Page check | Each job page is opened once with a plain HTTP request (free; at most 80 per run, 1 s between pages of one site) for location, work mode, posting date, open/closed. Greenhouse links that move to a company site are read from Greenhouse's embedded copy of the job |
| Workbook filters | Hidden but kept in memory: titles with a `title_exclude` phrase, closed jobs, jobs whose page lists only places outside Israel, duplicates (same title at the same company; the same job at two levels with the same location and date), careers pages of companies that have a job shown (all careers pages when `show_career_pages = false`). A lead the user marked is never hidden; a `duplicate` mark never counts as the original |
| Secrets | `local.env.yaml` (git-ignored; template `local.env.example.yaml`) sets environment defaults; a real environment variable wins; otherwise a hidden prompt |
| Privacy | Settings, CVs, `data/`, `results/`, `local.env.yaml` and working notes (`docs/PROGRESS.md`, `docs/tasks/`) are git-ignored. The tool never reads CVs |
| Tests | Never reach the network. Added only where a bug would lose, repeat or wrongly hide leads, or spend credits |

## Query strategy

Measured with live searches:

- **One platform per query**: `site:comeet.com/jobs ("Software Architect" OR ...) ("Israel" OR "Tel Aviv")`.
  Google ignored a combined `(site:A OR site:B)` clause and returned general web pages.
- **Short queries**: at most 20 words. 14 words were honoured; 32 words made Google drop `site:`.
  Titles are packed into as few queries as fit.
- **Scheduling**: each family priority has a revisit interval (`budget.revisit_days`, default
  2/4/8 days). Each run searches the most overdue queries first, up to `per_run`. If the monthly
  demand exceeds the limit, everything is revisited proportionally less often; `preview` prints
  the demand.
- A "past week" date filter keeps results fresh; one page per query.
- SerpApi often returns Google redirect links (`google.com/goto?url=...`); the page address
  shows only in `displayed_link`. Many early "Google ignored `site:`" verdicts were probably
  these links (not checked: dropped links weren't saved then). When `displayed_link` is on the query's platform, the tool reads the
  redirect's `Location` with one plain HTTP request (no credits). Google once refused results
  7-10 of each fresh search but followed the same links minutes later, so failed links are tried
  once more after all searches, at least 60 s after their own. Results not on the platform
  are dropped. `sweep` measures this per query and lists the first dropped addresses, and
  every search's outcome is stored (`query_outcomes`).

## Workbook

One sheet, rewritten on every run. Columns:

`Found on · Status · Note · Track / CV · Role family · Title · Company · Location · Work mode · Posted · Region · Platform · Page type · Snippet · Link · Lead ID`

- **Status** (`new`, `interested`, `applied`, `not relevant`, `broken link`, `duplicate`) and
  **Note** belong to the user. They are read back into SQLite before every rewrite and never
  overwritten.
- If the workbook is open in Excel, a run stops before any search. Saves are atomic.
- Text from search results is always written as text, never as a formula.
- The user's marks are stored, so they can be used to tune the settings. Settings are never
  changed automatically.

## Folder layout

```
sources/positions.example.toml   tracked example; copy to sources/positions.toml (private)
sources/search.example.toml      tracked example; copy to sources/search.toml (private)
local.env.example.yaml           template; copy to local.env.yaml (private)
src/job_search_robot/            code (module map in CLAUDE.md)
tests/                           tests and fixtures (no network)
docs/design.md                   this file
docs/task-template.md            format of the private task notes
data/history.sqlite3             tool memory (private)
results/                         workbook and reports (private)
```
