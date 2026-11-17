# Task notes: format

Bigger changes are tracked in private working notes, so any session (yours or Claude Code's)
can resume where the last one stopped. Both are git-ignored:

- `docs/PROGRESS.md`: where work stands.
- `docs/tasks/<ID>_<short-name>.md`: one file per task, e.g. `docs/tasks/T1-1_new-platform.md`.

## docs/PROGRESS.md

```markdown
# Progress

## Now
Task: [T1-2](tasks/T1-2_title-filter.md) title filter: design proposed, waiting for review.
Next: ...

## Tasks
| ID | Task | Status |
| --- | --- | --- |
| [T1-1](tasks/T1-1_new-platform.md) | Add a hiring platform | done |
| [T1-2](tasks/T1-2_title-filter.md) | Title filter | waiting for review |

## Session log
- YYYY-MM-DD: what was done, what was decided.
```

Statuses: `proposed`, `in progress`, `waiting for review`, `done`.

## docs/tasks/&lt;ID&gt;_&lt;short-name&gt;.md

```markdown
# T1-2 - Title filter
Status: in progress
Last updated: YYYY-MM-DD

## 1. Why & what
The problem, and what the task changes.

## 2. Inputs / outputs
What it reads (files, settings, pages) and what it produces.

## 3. Design
How it works. Stop here for the user's review.

## 4. Code
What changed, test results, results of a free `refresh` if relevant. Stop here for review.

## 5. Progress & next step
- [ ] Why & what agreed
- [ ] Design agreed
- [ ] Code done
- [ ] Tests pass
- [ ] Reviewed by the user

Next: ...
Open questions: ...
```
