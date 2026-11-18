# Roadmap: future improvements

Ideas and pending work that are not started yet. Each item says why, what it would do, and what
has to be decided first. Agreed decisions live in [design.md](design.md); work in progress lives in
the private notes (`docs/PROGRESS.md`, `docs/tasks/`). When an item starts, it becomes a task there
and this file links to it.

Statuses: `idea` (not discussed in detail), `needs decision` (the user has to choose), `planned`
(agreed, not started).

| # | Item | Status | Changes scope? |
| --- | --- | --- | --- |
| 1 | [Browser search source next to SerpApi](#1-browser-search-source-next-to-serpapi) | needs decision | yes |
| 2 | [AI fit check of each job page](#2-ai-fit-check-of-each-job-page) | needs decision | yes |
| 3 | [City searches](#3-city-searches) | idea | no |
| 4 | [Other search services](#4-other-search-services) | idea | no |
| 5 | [Company job boards read directly](#5-company-job-boards-read-directly) | idea | yes |
| 6 | [Platform follow-ups](#6-platform-follow-ups) | idea | no |
| 7 | [Sharing the repository](#7-sharing-the-repository) | needs decision | no |

---

## 1. Browser search source next to SerpApi

**Why.** SerpApi's free plan allows 250 searches a month, which limits role groups, platforms,
cities and result pages. Google's own search API is closed to new customers and shuts down on
2027-01-01, so there is no official free route to Google results.

**Idea.** A second search source that drives a browser with a testing tool (e.g. Playwright,
which can run Chromium, Firefox or WebKit), opens Google, runs the same query and reads title,
link and snippet. The tool talks to SerpApi through one small interface ("search this, return
results"); the browser source would offer the same one, so everything after it (dedupe, page
checks, workbook) stays the same. The source is chosen per run (e.g. `run --source browser`), so
both can run on the same queries and be compared. Browser searches would not count against the
SerpApi budget.

Bonus: the same browser could open job pages that plain requests can't read (Workday refuses them;
Workable pages are built by JavaScript), giving their open/closed status and posting date.

**Before building.**
- A feasibility test: 3-6 slow searches from the user's connection, with and without a visible
  window. Google tends to block testing browsers (they announce themselves, and a fresh profile has
  no cookies), showing a consent page or an "unusual traffic" CAPTCHA.
- Risks to accept: automated searches are against Google's terms; a block shows up as CAPTCHAs on
  the user's own connection for a while; Google's result page changes and the reader needs upkeep.
- Scope: adds browser automation and a new dependency (Playwright plus a browser download).
- What it does not fix: Google's own behaviour (ignoring `site:` when there are few matches).

## 2. AI fit check of each job page

**Why.** Title phrases and `title_exclude` miss cases such as an "Engineering Manager" role in
sales engineering, hardware or mechanical work, or a "Group Manager" outside software. The user
marks those `not relevant` by hand.

**Idea.** The tool already opens each job page. It would pass the job's text to a language model
together with a description of what the user is looking for, and get back
`fit: yes / no / unsure` with a one-line reason. First as a new workbook column only; hiding `no`
comes later, once it has proven accurate.

**The comparison is the hard part.** The model needs something to compare each post with, and it
must reflect the roles the user is actually looking for, which come from the user's CVs:
1. **A target profile per track**, written once: the kinds of roles that fit (e.g. software
   engineering management, team or group size, domains, seniority) and what doesn't fit (e.g.
   sales, hardware). It would be drafted from the CVs only when the user asks, reviewed and
   corrected by the user, and stored in the private settings next to the track. It must not invent
   experience. Sending this short profile instead of the CVs keeps the CVs on the user's computer
   and keeps each request small.
2. **An evaluation set from the user's own marks**: leads marked `applied` or `interested` should
   come out as fit, most `not relevant` ones as not fit. Caveat: `not relevant` sometimes means
   location, company or seniority rather than the role, so the set needs a short review.
3. **An offline trial** on those stored leads: measure how often the model agrees with the marks,
   adjust the profile, and only then add the column. The tool doesn't keep job page text today, so
   the trial re-reads the pages (free) or the page check starts keeping the description.

**Provider (a setting).** A cloud API (e.g. OpenAI's API, a separate paid service from the ChatGPT
app, with its own secret key handled like the SerpApi key; small cost per job), or a free model on
the user's computer (e.g. through Ollama: private, free, needs a capable PC; quality depends on the
model). Both can be called with Python's standard library, so no new dependency is needed.

**Scope.** "LLM ranking" is out of scope in CLAUDE.md and design.md today; this item changes that.

## 3. City searches

**Why.** A test on 2026-11-18 searched cities while excluding "Israel" and "Tel Aviv", to find only
the pages the regular searches can't. Herzliya, Ramat Gan, Petah Tikva, Haifa and Yokneam found a
few jobs whose pages say "IL" instead of "Israel". Four other city groups found nothing.

**Idea.** An occasional (e.g. weekly) extra search per role group and platform with those cities and
the two exclusions, instead of adding cities to every query. Needs "location groups" in the query
builder. Before that, watch whether adding Haifa and Ramat Gan to the regular location terms
(2026-11-18) keeps Google honouring `site:` (in the first run, three searches lost all platform
results).

## 4. Other search services

- **A paid SerpApi plan**: the same approach with more searches; no code change.
- **Brave Search API**: its own index, not Google's; about $5 of monthly credit (roughly 1,000
  searches) with a card. Needs a test of `site:` support and coverage of the platforms.
- Google's Custom Search API and Bing's search API are not options (closed or retired).

## 5. Company job boards read directly

**Idea.** Greenhouse, Lever, Ashby and Comeet publish each company's open jobs in a readable form.
Reading those boards directly is the most complete and reliable source, and costs no searches.

**Before building.** It needs a list of companies to follow (which could grow from the companies
the searches find), and platform APIs are out of scope in CLAUDE.md today.

## 6. Platform follow-ups

- **SmartRecruiters**: the rule "a closed job lands on the company page" is assumed; check it on a
  real closed job. Very old jobs stay online; the Posted date shows it.
- **Lever and Workable**: off (no Israeli management jobs found in tests). Re-test occasionally.
- **Untested platforms** in the example settings (BambooHR, Breezy, Recruitee, Teamtailor): a free
  check of whether they carry Israeli jobs, before any paid test.
- **Search counter**: the tool counts searches answered from SerpApi's 1-hour cache too, so it can
  show more than SerpApi's own count (stricter, never looser). Could read SerpApi's account usage
  (free) instead.

## 7. Sharing the repository

- A license (e.g. MIT) for others to use the code.
- Review `sources/search.example.toml` defaults against what the tests showed (freshness "any",
  which platforms are on by default).
