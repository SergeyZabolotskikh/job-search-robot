# Job Search Robot

A small command-line tool that finds job postings at Israeli companies on hiring
platforms (Comeet, Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday). It searches Google through
[SerpApi](https://serpapi.com), opens each job page to check it, and adds only the leads
it has **never shown you before** to one Excel workbook. Each lead is tagged with the
CV that fits it.

**By default it uses SerpApi's free plan: 250 searches a month.** The tool counts its
searches and stops at the limit. To search more, use another SerpApi key or pay for a bigger
plan: see [Search limits and getting more searches](#search-limits-and-getting-more-searches).

The example settings are sized for a run every ~2 days on the free plan. They search five
platforms for engineering-manager, team-lead, group-manager, head-of and software-architect
roles in Israel. [Setup](#setup-once) walks through everything you need to configure.

> **Fully vibe-coded.** The code, tests and documentation were written by AI from
> plain-language requests: a first prototype by ChatGPT, then everything since by
> [Claude Code](https://claude.com/claude-code). The author decided what to build, tested it
> on a real job search and reviewed the results, but didn't write the code by hand. See
> [Using it with Claude Code](#using-it-with-claude-code).

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

Steps 1-7 use no searches. Only step 8 does.

### What you need

- [Git](https://git-scm.com/downloads), to download the code.
- [uv](https://docs.astral.sh/uv/), which installs Python 3.14 and the dependencies for you:
  ```powershell
  # Windows (PowerShell)
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```
  ```bash
  # macOS/Linux
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
  Then open a new terminal and check that `uv --version` works.
- A free [SerpApi](https://serpapi.com) account, for your API key.
- Excel, LibreOffice or another spreadsheet app, to read the results.

### Step 1: Decide which roles to search

The tool searches only for the job titles you give it, so start with a short list. Each group
of titles costs one search per platform, so a focused list makes the free plan go further.
Choose one way:

**Option A: ask Claude to suggest roles from your CV.** Open [Claude](https://claude.ai) or
[Claude Code](https://claude.com/claude-code), attach your CV and paste this:

```text
Here is my CV. I am looking for jobs using exact job-title searches on hiring platforms.
Suggest which job titles I should search for.

- Group the titles by kind of role: at most 5 groups, up to 3 short titles each.
- Use the shortest common title: "Engineering Manager" also finds "Senior Engineering Manager".
- Mark each group as a strong fit or a realistic stretch, based only on my CV.
- List similar roles I should skip, and why.
- Do not review or rewrite my CV.

What I want and don't want: <for example: backend, people management, no sales>
```

**Option B: write the list yourself:**

```text
Want: Engineering Manager, Team Lead, Software Architect
Don't want: Sales, Hardware
```

Keep the list for later: the titles you want go into [step 5](#step-5-set-the-roles-you-want),
and the words you don't want go into `title_exclude` in
[step 6](#step-6-set-location-platforms-and-filters).

The tool itself never reads your CV. With option A, you share it with Anthropic through Claude,
so remove your phone number and address first if you prefer.

### Step 2: Download and install

```bash
git clone <this repository's URL>
cd job-search-robot
uv sync
```

### Step 3: Add your SerpApi key

1. Copy your key from your [SerpApi API key page](https://serpapi.com/manage-api-key).
2. Create your private key file, which git ignores:
   ```powershell
   Copy-Item local.env.example.yaml local.env.yaml     # macOS/Linux: cp local.env.example.yaml local.env.yaml
   ```
3. Open `local.env.yaml` and replace `paste-your-key-here` with your key:
   ```yaml
   SERPAPI_API_KEY: "your-key"
   ```

Alternatives: set a `SERPAPI_API_KEY` environment variable (it wins over the file), or leave
both empty and `run` asks for the key (hidden input, never saved). Never put the key in any
other file.

### Step 4: Create your settings files

```powershell
Copy-Item sources\positions.example.toml sources\positions.toml
Copy-Item sources\search.example.toml sources\search.toml
# macOS/Linux: cp sources/positions.example.toml sources/positions.toml
#              cp sources/search.example.toml sources/search.toml
```

Git ignores your copies, so they stay on your computer. Edit the copies, not the examples.

### Step 5: Set the roles you want

Open `sources/positions.toml` and enter the list from step 1.

**Tracks** are the kinds of job you apply for, usually one per CV. The label appears in the
workbook, so you know which CV to send:

```toml
[tracks.management]
label = "Management (EM CV)"
```

**Families** are groups of job titles to search:

```toml
[[family]]
id = "engineering_manager"
name = "Engineering manager"
track = "management"
priority = 1
enabled = true
titles = ["Engineering Manager", "Software Development Manager", "R&D Manager"]
```

1. Rename the track labels to match your CVs, and remove any track you don't need.
2. Edit the `titles` of each family, or copy a `[[family]]` block to add a new one.
3. Set `enabled = false` on every family you don't want.
4. Give your most important families `priority = 1`.

List the **shortest phrase** that still means the right job. Each title is an exact-phrase
search, so "Engineering Manager" also finds "Senior Engineering Manager". Every enabled family
costs at least one search per platform each time it is searched, so keep the list focused.

### Step 6: Set location, platforms and filters

Open `sources/search.toml` and check these sections:

- **`[location] query_terms`:** every search requires a page to mention one of these. The
  example, `["Israel", "Tel Aviv"]`, already covers the whole country. Keep it short: extra
  cities make every search longer and can split your titles into more searches.
- **`[[region]]`:** your city lists. **Add every city you would work in.** A job page that
  names only a city missing from all lists, with no country, counts as abroad and is hidden.
  Regions don't change the search. `enabled = false` only changes the Region label to
  `outside area`; those jobs are still shown.
- **`[[platform]]`:** which hiring platforms to search. Each one adds a search per title
  group. The example has the five that found Israeli jobs in testing.
- **`[filters] title_exclude`:** titles to hide, such as "Sales" or "Hardware".
- **`[filters] show_career_pages`:** whether to show company careers pages as well as jobs.
- **`[budget]`:** the example fits the free plan, so leave it unless you have more searches.
  See [Search limits and getting more searches](#search-limits-and-getting-more-searches).

The tool is built for jobs in Israel: its "abroad" check is fixed in the code, so searching
another country needs a code change.

### Step 7: Check the plan

This is free and uses no key or network:

```bash
uv run job-search-robot preview
```

It prints every query, which ones the next run will search, and whether the schedule fits the
monthly limit. With the unedited example: 25 queries, 16 in the next run, about 244 searches a
month. If it says **over the limit**, turn off some families or platforms. A mistake in a
settings file stops the tool here with a message, before any search.
`results/search_plan.html` has clickable Google links, so you can try any query by hand.

### Step 8: Run your first search

**This spends up to 16 of your 250 monthly searches** (the `per_run` setting):

```bash
uv run job-search-robot run
```

It takes a few minutes. If it asks for your key, `local.env.yaml` still has the placeholder.
When it finishes, open `results/job_leads.xlsx`; see [Routine](#routine-every-2-days) for how
to read it.

Some searches may bring nothing: Google sometimes shows other sites instead of the platform,
and the tool drops those. The next run picks up the queries this one didn't reach.

## Configuration reference

Every setting the tool reads. Run `preview` after each edit. If you delete a setting, the tool
uses the value below, except `freshness` (`week`), `show_career_pages` (`false`) and `title_exclude` (empty). Settings marked required, and each platform's `name` and `site`, can't be deleted.

### `local.env.yaml`

| Setting | What it does |
| --- | --- |
| `SERPAPI_API_KEY` | Your SerpApi key. A real environment variable with the same name wins. |
| `SERPER_API_KEY` | Optional. Needed for `--source serper`; `account` also shows its credits. |
| `TAVILY_API_KEY` | Optional. Not used for searching; `account` only shows its credits. |

### `sources/positions.toml`

| Setting | Value | What it does |
| --- | --- | --- |
| `[tracks.<id>]` `label` | required | Shown in the workbook's Track / CV column. |
| `[tracks.<id>]` `cv` | optional | A note for you, such as a CV file name. The tool doesn't read it. |
| `[[family]]` `id` | required | Stable key: letters, digits and `_`. Don't rename it casually. |
| `name` | defaults to `id` | Shown in the Role family column. |
| `track` | required | One of your track ids. |
| `priority` | 1-9, default 2 | Picks the matching `revisit_days` entry. |
| `enabled` | default `true` | `false` keeps the family in the file without searching it. |
| `titles` | required | Exact search phrases, without quotation marks. |
| `notes`, `evidence` | optional | Notes for you. The tool doesn't use them. |

### `sources/search.toml`

| Section | Setting | Example | What it does |
| --- | --- | --- | --- |
| `[budget]` | `monthly_limit` | `250` | The tool stops when SerpApi's own count for this key reaches this number. |
| | `per_run` | `16` | The most searches one run can spend. |
| | `per_hour` | `50` | The most searches in one hour. |
| | `allow_paid_plan` | `false` | `false` stops `run` and `sweep` before any search on a priced SerpApi plan, or on a Serper key with bought credits. |
| | `pages_per_query` | `1` | Google result pages per query (1-10). Each page is one search. |
| | `max_query_words` | `20` | Longer title lists split into more queries. Google ignored the platform at 32 words. |
| | `revisit_days` | `[2, 4, 8]` | Days until a query is due again, for priority 1, 2, 3. |
| `[google]` | `country` | `"il"` | Google country. It ranks local pages higher but doesn't exclude others. |
| | `language` | `"en"` | Google language. |
| | `freshness` | `"any"` | Date filter: `day`, `week`, `month` or `any`. Keep `any`: with a date filter, Google often ignored the platform. |
| | `timeout_seconds` | `30` | How long to wait for SerpApi (1-120). |
| `[location]` | `query_terms` | `["Israel", "Tel Aviv"]` | A page must mention one of these. Required. |
| `[[region]]` | `name` | `"Central"` | Shown in the Region column. |
| | `enabled` | `true` | `false` labels its cities `outside area`, without hiding them. |
| | `cities` | list | City names and spelling variants. They also tell the tool a job is in Israel. |
| `[[platform]]` | `name` | `"Comeet"` | Shown in the workbook and reports. |
| | `site` | `"comeet.com/jobs"` | The address searched, without `https://`. |
| | `enabled` | `true` | `false` keeps the platform in the file without searching it. |
| `[filters]` | `exclude_terms` | `[]` | Phrases excluded from every search. Each one makes the search longer. |
| | `title_exclude` | list | Hides leads whose title has one of these (whole words, any case). Free. |
| | `show_career_pages` | `true` | Shows company careers pages, unless that company already has a job shown. |
| | `old_posting_days` | `60` | The Posted cell turns orange after this many days. |

After editing `title_exclude`, `show_career_pages` or `old_posting_days`, run
`uv run job-search-robot refresh` to apply it to the existing leads. It's free.

## Routine (every ~2 days)

```bash
uv run job-search-robot run
```

**Close the workbook in Excel first.** If it's open, the run stops before spending any
searches.

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
- "New" means new to this tool, not newly posted. The example settings use no date filter,
  so the first runs also find older jobs.

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
| `run` | yes, up to `per_run` (16 in the example) | Searches the most overdue queries and updates the workbook. |
| `refresh` | no | Re-reads Status/Note, checks new job pages, re-applies the filters, rewrites the workbook. Use it after editing filters. |
| `refresh --recheck` | no | Also opens every job page again, to find jobs closed since. It opens them all in one go (a few minutes for a few hundred jobs). Until a page is read again, its earlier result stays, and a page that can't be read keeps it, so a job already known to be closed or abroad stays hidden. |
| `sweep` | yes, one per query | Searches every enabled query once and writes `results/query_report_<date>_<time>.xlsx`: what each query returned, with a Google link for a manual check. Use it to tune titles and platforms. |
| `account` | no | Shows SerpApi's plan, price, searches used and left, what the tool sent per key this month, and Tavily and Serper credits if their keys are set. |

Run them as `uv run job-search-robot <command>`. Useful options:

- `--family <id>` only searches that family (repeatable; also works for disabled ones).
- `--track <id>` only searches the families of that track: one of the `[tracks.<id>]` ids in
  your `positions.toml`, such as `management` (repeatable).
- `--max-requests N` lowers this run's budget.
- `--freshness day|week|month|any` overrides the date filter.
- `--source serper` makes `preview`, `run` or `sweep` use Serper instead of SerpApi.

`uv run job-search-robot <command> --help` lists them all.

## Search limits and getting more searches

**By default the tool is set up for SerpApi's free plan: 250 searches a month, 50 an hour.**

- One search is one Google results page (about 10 results).
- **Before every `run` or `sweep`, the tool asks SerpApi for this key's plan, price and
  searches left.** This check is free. If the plan has a price, the tool stops before searching
  unless you set `allow_paid_plan = true`. It also stops if SerpApi's answer is missing or unclear.
- A run never goes past the searches left on the plan, so it doesn't use extra credits. It
  also stops at `monthly_limit` or `per_hour`. When a limit stops or shortens a run, the tool
  says which limit, how many searches were made, and what to do next. `preview` warns you
  before a run would be cut short, using SerpApi's count from the last check.
- After each run, the tool reads SerpApi's count again, waiting up to a minute because that
  count updates a few seconds after a search. It prints how many of the searches SerpApi
  really counted, and how many reached a job platform. If few or none did, Google
  returned other sites; `sweep` shows which queries. `uv run job-search-robot account` shows
  the current numbers anytime.
- Each run searches the most overdue queries first and **spends up to `per_run`, even on
  queries that aren't due yet**. So the monthly cost is about `per_run` × the number of
  runs: 16 × 15 runs (every 2 days) = 240.
- `preview` also estimates what the `revisit_days` schedule needs and warns when that is
  over the monthly limit. To fit, turn off platforms or families, or raise `revisit_days`.
- Identical searches within an hour are served from SerpApi's cache and are free. Failed
  searches aren't charged either. Neither uses up the tool's budget, because the budget
  follows SerpApi's count.
- SerpApi's month runs to the plan's renewal date, which may not be the 1st. The `account`
  command shows that date.

### Need more searches?

There are two ways:

- **Pay for a bigger plan.** See [SerpApi pricing](https://serpapi.com/pricing); paid plans
  start at 1,000 searches a month.
- **Use another SerpApi key** that has searches left, for example from another account.
  [SerpApi's terms](https://serpapi.com/legal) let it limit or refuse service, so check that
  your use fits them.

Then update your local files. The limits in your settings don't change by themselves:

1. If the key changed, replace it in `local.env.yaml`. If you set a `SERPAPI_API_KEY`
   environment variable, change it there, because it wins over the file.
2. **Another free key needs no limit change:** the tool uses SerpApi's count for whichever
   key is set. **For a paid plan**, set `allow_paid_plan = true` and raise `monthly_limit` in
   `sources/search.toml`. Without `allow_paid_plan`, the tool refuses to search on that plan.
3. Set `per_hour` to your plan's hourly limit, shown on the pricing page.
4. Raise `per_run` so that `per_run` × runs a month stays within the limit. A run searches
   each enabled query at most once, so also turn on more families or platforms if you want
   it to use the extra searches.
5. Run `uv run job-search-robot preview` to check the new plan.

For example, on a 1,000-search plan with a run every 2 days:

```toml
[budget]
allow_paid_plan = true
monthly_limit = 1000
per_run = 60             # 60 x 15 runs = 900 a month
```

### Serper as a backup (optional)

SerpApi is the default. If it runs out or stops working, you can search through Serper, which
also returns Google results. The tool never switches by itself; you choose it per command:

```bash
uv run job-search-robot preview --source serper
uv run job-search-robot run --source serper
```

- Add `SERPER_API_KEY` to `local.env.yaml`. Serper offers 2,500 free credits, and one search
  uses one credit. They don't appear to renew monthly, so treat them as a reserve.
- Leads, page checks, hide rules and the workbook work the same. A query searched through one
  service counts as searched for the other, so it isn't repeated right away.
- The tool reads Serper's balance before and after each run and stops at zero. Serper's
  searches are counted apart from SerpApi's, with their own `monthly_limit`, `per_run` and
  `per_hour`.
- A balance above 2,500 means some credits were bought, so the tool stops unless
  `allow_paid_plan = true`. A lower balance can't show whether credits were bought.
- Serper's balance address isn't in the public docs we could read. If `account` says
  `unavailable`, `--source serper` won't search either; check Serper's dashboard.

### Tavily (not used for searching)

Tavily was tested and found too few of the known jobs (see the
[roadmap](docs/roadmap.md#4-other-search-services)). If you add `TAVILY_API_KEY`, `account`
shows its plan and credits without sending a search, and warns if any pay-as-you-go credits
were used, because those are charged.

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

- API keys live only in `local.env.yaml`, their environment variables, or (SerpApi only) the
  hidden prompt. Error messages never print them.
- Git ignores everything personal, so it stays on your computer: your settings
  (`sources/positions.toml`, `sources/search.toml`), `local.env.yaml`, `data/`,
  `results/`, any `.pdf`/`.doc`/`.docx` file and the `sources/cvs/` folder, and the
  working notes `docs/PROGRESS.md` and `docs/tasks/`.
- The tool doesn't need or read your CV. If you use Claude in
  [step 1](#step-1-decide-which-roles-to-search), you share the CV with Anthropic yourself.
- The tool sends only search queries and free account checks (to SerpApi, or to Serper with
  `--source serper`), one request per Google redirect link of a platform result (to Google, to
  learn the page address), and plain page requests to the public job pages it found (one per
  second per site, at most 80 per run; `refresh --recheck` opens every job page). If you set Tavily or Serper keys, `account` also asks
  those services for your credits.

## Known limitations

- SerpApi often returns Google redirect links (`google.com/goto?url=...`) instead of page
  addresses. For results that Google shows on the searched platform, the tool asks Google
  where the link leads (one free request each, no searches used). Google sometimes refuses
  links right after a search, so those get two more tries after all searches (about 1 and 5
  minutes after their search, so a run with refused links takes longer); the run prints how many, and the `sweep` report shows why any still failed.
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

## Roadmap

[docs/roadmap.md](docs/roadmap.md) lists suggested improvements that are not built yet, each
with why, what it would do, and what has to be decided first. Among them: a browser search
source next to SerpApi (no monthly search limit), an AI check of each job page against a
short profile of the roles you are looking for, occasional city searches, other search
services, reading company job boards directly, and a workbook sheet that lists every hidden
lead with the reason. Some of them would change what the tool
does (see the "Changes scope?" column there), so none starts without your decision.

## Using it with Claude Code

`CLAUDE.md` tells [Claude Code](https://claude.com/claude-code) how this project works
and what it must not do. In particular, it must never spend searches without your OK and
never read your key file. Just ask for what you want: the task stages and notes in `CLAUDE.md`
are only for changing the tool itself. Good requests:

- "Help me set up the tool: README steps 1-7. Here is my CV / my list of roles."
- "Run preview and tell me how many searches a month my configuration needs."
- "Read my Status and Note marks in results/job_leads.xlsx and propose title_exclude or
  role changes."
- "Add the family 'Data Engineering Manager' to positions.toml."

## Development

```bash
uv run pytest
```

The tests never touch the network or SerpApi. Code lives in `src/job_search_robot/`;
`CLAUDE.md` describes the modules, `docs/design.md` records the design decisions, and
`docs/roadmap.md` lists improvements not started yet. `docs/task-template.md` shows the
format of the private task notes.

## License

MIT. See [LICENSE](LICENSE).
