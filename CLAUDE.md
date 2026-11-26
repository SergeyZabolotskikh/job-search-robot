# Job Search Robot

Local CLI that searches Google (through SerpApi, free plan; Serper as a backup chosen with
`--source serper`) for jobs on hiring platforms
(Comeet, Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday), checks each job page, and writes only
never-seen leads to an Excel workbook, tagged with the CV ("track") that fits. The user runs
it from their own computer every ~2 days. README.md is the user guide.

## Start of every session
1. If docs/PROGRESS.md exists, read it: where work stopped and the next action.
2. Open the current task file in docs/tasks/ (if any).
3. Agreed design decisions: docs/design.md. If a decision changes, update it there.
4. Ideas not started yet: docs/roadmap.md. When one starts, it becomes a task; update its status there.

## Private vs shared
The repository is shared; each user's personal files stay on their machine (git-ignored):

| Private (git-ignored) | Shared (in git) |
| --- | --- |
| sources/positions.toml, sources/search.toml | sources/positions.example.toml, sources/search.example.toml |
| local.env.yaml | local.env.example.yaml |
| docs/PROGRESS.md, docs/tasks/ (working notes) | docs/task-template.md (their format), docs/design.md, docs/roadmap.md, LICENSE (MIT) |
| data/, results/, CVs (sources/cvs/, *.pdf, *.doc, *.docx) | code, tests, README.md, CLAUDE.md |

- The examples must stay generic and valid: no personal details, and a new setting gets
  added to the example (with a comment) as well as to the code. The tests load both the
  examples and, if present, the private copies.
- Never `git add -f` an ignored file. Before any commit, check that no staged file holds a
  name, email, CV detail, company applied to, or key.

## Hard rules
- **No paid searches without approval.** `run` and `sweep` spend SerpApi credits, or Serper
  credits when `[google] source = "serper"` in search.toml or `--source serper`. Before running
  either, tell the user which service (the setting, unless `--source` is given) and how
  many searches it will make (`preview` shows the plan) and wait for explicit approval of
  that number. The same applies to any search sent to Tavily (the tool never searches it).
  Free: `preview`, `refresh`,
  `refresh --recheck`, `account` (reads usage, sends no search), `uv run pytest`.
- **API keys are secret.** The SerpApi, Tavily and Serper keys live only in git-ignored
  local.env.yaml or their environment variables (SERPAPI_API_KEY, TAVILY_API_KEY,
  SERPER_API_KEY); the SerpApi key may also come from the hidden prompt. Never read, print
  or copy local.env.yaml; never put a key in a command, file or output. To check that a key
  is configured, check only that it is present.
- **Private data stays private.** The private files above are personal: never commit them
  or send their contents anywhere. The tool doesn't use CVs; read one only when the user
  asks, and never invent experience.
- **Status and Note belong to the user.** Never overwrite them in results/job_leads.xlsx.
  The tool imports them before every rewrite; don't edit the workbook by hand or by script.
- **Config belongs to the user.** Don't change sources/positions.toml or sources/search.toml
  automatically from results; propose the change and edit after agreement.
- **Git:** commit only when the user asks. Never create, add or push to a remote unless
  the user asks.
- **No personal details in tracked files.** Write "the user", never a personal name, and
  keep job-search specifics (companies applied to, CV content, the user's marks) out of
  code, config comments and docs.

## Commands
- `uv sync` installs; `uv run job-search-robot <preview|run|refresh|sweep|account>`; `uv run pytest`.
- `[google] source` in search.toml picks SerpApi or Serper; `--source` on `preview`, `run` or
  `sweep` overrides it for one command. The tool never
  switches by itself.
- `refresh --recheck` opens every job page again (free). `--family`, `--track`,
  `--max-requests`, `--freshness` work on every command.
- Windows console: set `PYTHONIOENCODING=utf-8` when output may contain non-Latin text.
- After any change to hide rules, identity rules or page parsing, run `refresh` (free) and
  report the shown/hidden counts.

## Code map (src/job_search_robot/)
| Module | Role |
| --- | --- |
| cli.py | Commands, budget checks (paid-plan stop, searches left, monthly, hourly, per run), key loading |
| config.py | Loads and validates sources/positions.toml and sources/search.toml into `Settings` |
| queries.py | Builds queries (one platform per query, at most `max_query_words`); schedules by `revisit_days` |
| serpapi.py | SerpApi client and free account check; errors never echo the key |
| serper.py | Serper client (same answer shape as SerpApi) and balance check; errors never echo the key |
| usage.py | Read-only Tavily and Serper credits for the `account` command; sends no search |
| urls.py | Canonical URLs and the stable lead identity per platform (the basis of "seen before") |
| store.py | SQLite memory: results, searches (per account), account_checks, leads (Status/Note), page_checks, identity_aliases, query_outcomes |
| collector.py | One run: import Status/Note, search breadth-first, follow Google redirect links (two more tries after the searches), record outcomes, check pages, publish |
| verify.py | Job page check: Comeet `POSITION_DATA`, Greenhouse `job__location` / embedded copy, schema.org JobPosting (JSON-LD or microdata: SmartRecruiters); Workday from the address, never requested; Google redirect links → page address |
| filters.py | Hide rules (excluded titles, careers pages, closed, abroad, old posting) and duplicate detection |
| workbook.py | The Excel workbook; text is never written as a formula; atomic save |
| query_report.py, reports.py | Sweep report; preview HTML/CSV |
| localenv.py, common.py | local.env.yaml loading; paths, errors, atomic writes |

## Conventions
- Python 3.14 with uv. Dependencies: stdlib, openpyxl, pyyaml. Ask before adding any.
- Out of scope: applying or messaging, CV rewriting, LLM ranking, web UI, scheduler,
  platform (ATS) APIs.
- Tests must never reach the network: `verify.urlopen`, `verify.open_no_redirect`, `usage.urlopen` and `serper.urlopen` are
  blocked for the whole suite; pass fake `fetch`/`resolve` functions and fake clients. Add tests only where a bug would lose,
  repeat or wrongly hide leads, or spend credits: dedupe ("seen before"), hide rules, page
  parsers, the search counter, Status/Note preservation.
- Keep README.md in step with commands, columns and settings.

## Working process
- **Using the tool is not a task.** If the user only wants to search (set it up with the
  README steps, choose roles, edit their own settings, run `preview`/`run`/`refresh`, read the
  workbook), help directly: no stages, no task notes, no docs/PROGRESS.md needed. The hard
  rules above still apply. Everything below is for changes to the tool itself (code, tests,
  shared docs).
- Discuss before creating: propose, get agreement, then write files.
- A bigger task has 5 stages: Why & what, Inputs/outputs, Design, Code, Progress & next
  step. Stop for the user's review after Design and after Code; small tasks may go straight
  through. A task is done only after the user's review.
- Track tasks in the private notes docs/tasks/<id>_<name>.md and docs/PROGRESS.md (Now, task
  table, session log), updated after every stage, so any session can resume. Format:
  docs/task-template.md. Create them if missing.
