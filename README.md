# Job Search Robot

A small command-line tool that finds job postings at Israeli companies on hiring
platforms (Comeet, Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday). It searches Google through
[SerpApi](https://serpapi.com), opens each job page to check it, and adds only the leads
it has **never shown you before** to one Excel workbook. Each lead is tagged with the
CV that fits it.

It is built for a run every ~2 days on SerpApi's **free plan** (250 searches a month).
The included configuration looks for engineering-manager and architect / tech-lead
roles in central and northern Israel. Edit two TOML files to make it yours.

## How it works

1. **Search.** One Google query per group of job titles and platform, for example
   `site:comeet.com/jobs ("Engineering Manager" OR "R&D Manager") ("Israel" OR "Tel Aviv")`.
   Results that are not on the platform are dropped.
2. **Remember.** Each lead gets a stable ID (the platform's job ID), stored in
   `data/history.sqlite3`. A lead found again is never "new" again.
3. **Check the job page** (free, plain HTTP): location, work mode, posting date, open or
   closed.
4. **Filter.** Jobs abroad, closed jobs, duplicates and titles you exclude are hidden.
5. **Write the workbook** `results/job_leads.xlsx`. Your Status and Note columns are kept.

## Setup (once)

You need [uv](https://docs.astral.sh/uv/) (it installs Python 3.14 for you) and a free
[SerpApi](https://serpapi.com) account.

1. Clone the repository and install the dependencies:
   ```bash
   git clone <this repository's URL>
   cd job-search-robot
   uv sync
   ```
2. Put your SerpApi key in a local file that git ignores:
   ```powershell
   Copy-Item local.env.example.yaml local.env.yaml     # macOS/Linux: cp local.env.example.yaml local.env.yaml
   ```
   Open `local.env.yaml` and replace `paste-your-key-here` with your key (SerpApi
   dashboard, "API key"). Alternatives: set a `SERPAPI_API_KEY` environment variable
   (it wins over the file), or leave both empty and `run` asks for the key (hidden
   input, never saved). Never put the key in any other file.
3. Create your own settings from the examples, then make them yours (see
   [Configure your search](#configure-your-search)):
   ```powershell
   Copy-Item sources\positions.example.toml sources\positions.toml
   Copy-Item sources\search.example.toml sources\search.toml
   # macOS/Linux: cp sources/positions.example.toml sources/positions.toml
   #              cp sources/search.example.toml sources/search.toml
   ```
   Your copies are private: git ignores them, so they stay on your computer.
4. Check the plan. It's free and uses no key or network:
   ```bash
   uv run job-search-robot preview
   ```
   It prints every query, which ones the next run will search, and whether the full
   schedule fits the monthly limit. `results/search_plan.html` has clickable Google
   links, so you can try any query by hand.

## Configure your search

| Your file (private) | Started from (in git) | What you set |
| --- | --- | --- |
| `sources/positions.toml` | `sources/positions.example.toml` | **Tracks**: one per kind of job you apply for, e.g. one per CV; the workbook shows the track's label. **Role families**: the job titles to search, their track, priority, enabled. |
| `sources/search.toml` | `sources/search.example.toml` | Budget, freshness, location words, regions and cities, platforms, title filters. |

Both files explain every field in comments. If the examples change later, compare them
with your copies. Tips:

- List the **shortest title phrase** that still means the right job. Each phrase is an
  exact-phrase search, so "Engineering Manager" also finds "Senior Engineering Manager".
- **Priority** sets how often a family is searched: `revisit_days = [2, 4, 8]` means every
  2 days for priority 1, every 4 for priority 2 and every 8 for priority 3.
- `enabled = false` keeps a family or platform in the file without searching it.
- **Regions** don't change the search. They label each lead (e.g. `Central`) and mark
  cities in disabled regions as `outside area`.
- `title_exclude` hides leads whose title contains a phrase (whole words, any case), for
  example "Sales" or "Hardware". It's free, because the search itself doesn't change.

Run `preview` after every edit. A configuration error stops the tool before any search.

## Routine (every ~2 days)

```bash
uv run job-search-robot run
```

**Close the workbook in Excel first.** If it's open, the run stops before spending any
searches. The first time, `uv run job-search-robot run --freshness any` also finds older
postings; by default only pages Google indexed in the past week count.

Then open `results/job_leads.xlsx` (Excel, LibreOffice or any spreadsheet app):

- This run's new leads have a **green "Found on"** cell.
- **Status** (dropdown) and **Note** are yours. The tool reads them back before every run
  and never overwrites them. Use `interested`, `applied`, `not relevant`, `broken link` or
  `duplicate`. Don't delete rows, because they come back; set a Status instead.
- **Track / CV** tells you which CV to send.
- **Location, Work mode, Posted** come from the job page itself. `JOB CLOSED` means the
  job was removed. `not verified` means the page gave no details, so open the link.
  The **Posted** cell turns orange when a job is older than 60 days.
- **Region** comes from the page location, or else from the title and snippet.
- **Page type** `career page` is a company's job list, not a single job.
- "New" means new to this tool, not newly posted.

### What the workbook hides

Hidden leads stay in the tool's memory, so they never come back as "new":

- jobs whose page lists only locations outside Israel,
- closed jobs,
- duplicates: the same title at the same company in another location, the same job
  posted at two levels (Staff / Principal) with the same location and date, and a
  company's careers page when one of its jobs is already shown,
- titles that contain a `title_exclude` phrase,
- all careers pages, if you set `show_career_pages = false`.

A lead you have marked (any Status other than `new`) is never hidden. Each run prints
how many leads were hidden and why.

## Commands

| Command | Costs searches? | What it does |
| --- | --- | --- |
| `preview` | no | Shows the planned queries and the monthly schedule; writes `results/search_plan.html`/`.csv`. |
| `run` | yes, up to `per_run` (16) | Searches the most overdue queries and updates the workbook. |
| `refresh` | no | Re-reads Status/Note, checks new job pages, re-applies the filters, rewrites the workbook. Use it after editing filters. |
| `refresh --recheck` | no | Also opens every job page again, to find jobs closed since. |
| `sweep` | yes, one per query | Searches every enabled query once and writes `results/query_report_<date>_<time>.xlsx`: what each query returned, with a Google link for a manual check. Use it to tune titles and platforms. |

Run them as `uv run job-search-robot <command>`. Useful options:

- `--family <id>` only searches that family (repeatable; also works for disabled ones).
- `--track management|ic` only searches one track.
- `--max-requests N` lowers this run's budget.
- `--freshness day|week|month|any` overrides the date filter.

`uv run job-search-robot <command> --help` lists them all.

## The search budget

- One search is one Google results page (about 10 results) and costs one SerpApi credit.
  The free plan gives 250 searches a month and at most 50 an hour.
- The tool counts every search in `data/history.sqlite3`. It stops at `monthly_limit`
  and `per_hour`, and each run prints "used X of 250 this month".
- Each run searches the most overdue queries first, up to `per_run`. If the full
  schedule needs more than the monthly limit, `preview` warns you; turn off platforms or
  families, or raise `revisit_days`.
- Identical searches within an hour are served from SerpApi's cache and are free.
  Failed searches aren't charged.

## Files the tool writes

All of these are private and git-ignored.

| Path | What |
| --- | --- |
| `results/job_leads.xlsx` | The workbook. |
| `results/last_run.json` | Summary of the last run: searches, errors, hidden counts. |
| `results/search_plan.html` / `.csv` | Output of `preview`. |
| `results/query_report_*.xlsx` | Output of `sweep`. |
| `data/history.sqlite3` | The tool's memory: every lead seen, your Status/Note, page checks, the search counter. **Back it up.** Deleting it makes every lead "new" again and resets the counter. |

## Privacy

- The API key lives only in `local.env.yaml`, the environment variable, or the hidden
  prompt. Error messages never print it.
- Git ignores everything personal, so it stays on your computer: your settings
  (`sources/positions.toml`, `sources/search.toml`), `local.env.yaml`, `data/`,
  `results/`, any `.pdf`/`.doc`/`.docx` file and the `sources/cvs/` folder, and the
  working notes `docs/PROGRESS.md` and `docs/tasks/`.
- The tool doesn't need or read your CV.
- The tool sends only search queries (to SerpApi), one request per Google redirect link
  of a platform result (to Google, to learn the page address), and plain page requests to
  the public job pages it found (one per second per site, at most 80 per run).

## Known limitations

- SerpApi often returns Google redirect links (`google.com/goto?url=...`) instead of page
  addresses. For results that Google shows on the searched platform, the tool asks Google
  where the link leads (one free request each, no searches used). Google sometimes refuses
  links right after a search, so those are tried once more after all searches (at least a
  minute later); the run prints how many, and the `sweep` report shows why any still failed.
- Results on other sites are dropped. The `sweep` report shows, per query, how many were
  dropped and the first of them under Examples.
- Google picks up new postings with a delay, and the search sees only what Google has indexed.
- Comeet pages state only when a job was last updated, not when it was posted.
- Ashby marks many Israeli jobs as remote-eligible. Read "remote" there as "remote allowed".
- Workday refuses the tool's page requests, so Workday jobs are never opened: the location
  comes from the job address (e.g. `.../job/Israel-Yokneam/...`, shown with "(from the
  address)"), and open/closed and the posting date stay unknown. A job whose address names a
  place abroad is hidden only when Google's title and snippet don't mention Israel either.
- SmartRecruiters keeps very old jobs online; check the Posted date.
- Only these platforms are known: Comeet, Greenhouse, Lever, Ashby, Workable,
  SmartRecruiters and Workday. Jobs that appear only on LinkedIn or on a company's own site
  aren't found.

## Using it with Claude Code

`CLAUDE.md` tells [Claude Code](https://claude.com/claude-code) how this project works
and what it must not do. In particular, it must never spend searches without your OK and
never read your key file. Good requests:

- "Run preview and tell me how many searches a month my configuration needs."
- "Read my Status and Note marks in results/job_leads.xlsx and propose title_exclude or
  role changes."
- "Add the family 'Data Engineering Manager' to positions.toml."

## Development

```bash
uv run pytest
```

The tests never touch the network or SerpApi. Code lives in `src/job_search_robot/`;
`CLAUDE.md` describes the modules, and `docs/design.md` records the design decisions.
`docs/task-template.md` shows the format of the private task notes.
