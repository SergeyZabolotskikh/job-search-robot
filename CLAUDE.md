# Job Search Robot

Local CLI that searches Google (through SerpApi, free plan) for jobs on hiring platforms
(Comeet, Greenhouse, Lever, Ashby, Workable), checks each job page, and writes only
never-seen leads to an Excel workbook, tagged with the CV ("track") that fits. The user runs
it from their own computer every ~2 days. README.md is the user guide.

## Start of every session
1. If docs/PROGRESS.md exists, read it: where work stopped and the next action.
2. Open the current task file in docs/tasks/ (if any).
3. Agreed design decisions: docs/design.md. If a decision changes, update it there.

## Private vs shared
The repository is shared; each user's personal files stay on their machine (git-ignored):

| Private (git-ignored) | Shared (in git) |
| --- | --- |
| sources/positions.toml, sources/search.toml | sources/positions.example.toml, sources/search.example.toml |
| local.env.yaml | local.env.example.yaml |
| docs/PROGRESS.md, docs/tasks/ (working notes) | docs/task-template.md (their format), docs/design.md |
| data/, results/, CVs (sources/cvs/, *.pdf, *.doc, *.docx) | code, tests, README.md, CLAUDE.md |

- The examples must stay generic and valid: no personal details, and a new setting gets
  added to the example (with a comment) as well as to the code. The tests load both the
  examples and, if present, the private copies.
- Never `git add -f` an ignored file. Before any commit, check that no staged file holds a
  name, email, CV detail, company applied to, or key.

## Hard rules
- **No paid searches without approval.** `run` and `sweep` spend SerpApi credits. Before
  running either, tell the user how many searches it will make (`preview` shows the plan)
  and wait for explicit approval of that number. Free: `preview`, `refresh`,
  `refresh --recheck`, `uv run pytest`.
- **The API key is secret.** It lives only in git-ignored local.env.yaml, the
  SERPAPI_API_KEY environment variable, or the hidden prompt. Never read, print or copy
  local.env.yaml; never put the key in a command, file or output. To check that a key is
  configured, check only that it is present.
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
- `uv sync` installs; `uv run job-search-robot <preview|run|refresh|sweep>`; `uv run pytest`.
- `refresh --recheck` opens every job page again (free). `--family`, `--track`,
  `--max-requests`, `--freshness` work on every command.
- Windows console: set `PYTHONIOENCODING=utf-8` when output may contain non-Latin text.
- After any change to hide rules, identity rules or page parsing, run `refresh` (free) and
  report the shown/hidden counts.

## Code map (src/job_search_robot/)
| Module | Role |
| --- | --- |
| cli.py | Commands, budget checks (monthly, hourly, per run), key loading |
| config.py | Loads and validates sources/positions.toml and sources/search.toml into `Settings` |
| queries.py | Builds queries (one platform per query, at most `max_query_words`); schedules by `revisit_days` |
| serpapi.py | SerpApi client; errors never echo the key |
| urls.py | Canonical URLs and the stable lead identity per platform (the basis of "seen before") |
| store.py | SQLite memory: results, searches (counter), leads (Status/Note), page_checks, identity_aliases, query_outcomes |
| collector.py | One run: import Status/Note, search breadth-first, record outcomes, check pages, publish |
| verify.py | Job page check: Comeet `POSITION_DATA`, Greenhouse `job__location` / embedded copy, schema.org JobPosting |
| filters.py | Hide rules (excluded titles, careers pages, closed, abroad) and duplicate detection |
| workbook.py | The Excel workbook; text is never written as a formula; atomic save |
| query_report.py, reports.py | Sweep report; preview HTML/CSV |
| localenv.py, common.py | local.env.yaml loading; paths, errors, atomic writes |

## Conventions
- Python 3.14 with uv. Dependencies: stdlib, openpyxl, pyyaml. Ask before adding any.
- Out of scope: applying or messaging, CV rewriting, LLM ranking, web UI, scheduler,
  platform (ATS) APIs.
- Tests must never reach the network: `verify.urlopen` is blocked for the whole suite;
  pass fake `fetch` functions and fake clients. Add tests only where a bug would lose,
  repeat or wrongly hide leads, or spend credits: dedupe ("seen before"), hide rules, page
  parsers, the search counter, Status/Note preservation.
- Keep README.md in step with commands, columns and settings.

## Working process
- Discuss before creating: propose, get agreement, then write files.
- A bigger task has 5 stages: Why & what, Inputs/outputs, Design, Code, Progress & next
  step. Stop for the user's review after Design and after Code; small tasks may go straight
  through. A task is done only after the user's review.
- Track tasks in the private notes docs/tasks/<id>_<name>.md and docs/PROGRESS.md (Now, task
  table, session log), updated after every stage, so any session can resume. Format:
  docs/task-template.md. Create them if missing.
