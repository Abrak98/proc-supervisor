# render — spec

> Permanent contract and source of truth. Edit THIS file directly on any change.
> Every source `.py` implementing it carries `# spec: <path-to-this-file>`.

## Intent
Text and JSON views of a job record for `status`, `wait` and `list`: the first-line verdict, durations, the status and list JSON, the cost line, and the one-line error printer. Part of the engine (`specs/proc_lib/core.spec.md`).

## Rationale / Key decisions
- **The verdict is always the first line** of human `status`/`wait` output, so a caller that truncates still gets the fact.
- **Pure over a record**, apart from the clock read as `core._now()`; no registry access.

## Rejected alternatives
- **Formatting inside each command.** Rejected: one place keeps the verdict and JSON shapes identical across commands.

## Acceptance criteria
- GIVEN a record in any status WHEN `_verdict` runs THEN the line is the one `core.spec.md` states (`test_agent_surface.py`, `test_scheduled.py`).
- GIVEN a record WHEN `_status_json` / `_list_json` run THEN the field set is that of the I/O contract (`test_agent_surface.py`, `test_live_index.py`).

## State
None.

## Methods
- `_verdict`, `_until`, `_elapsed`, `_elapsed_seconds`, `_human_duration`, `_iso_short`, `_cmd_str`, `_job_succeeded`.
- `_status_json`, `_list_json`, `_status_lines`, `_cost_text`.
- `_err(msg)` — `proc: <msg>` on stderr.

## Invariants
- Reads no file and takes no lock.

## Formulas
n/a

## I/O contracts
Those of `specs/proc_lib/core.spec.md`; nothing is added or changed by the split.

## Names
| canonical | kind | forbidden-aliases |
|-----------|------|-------------------|
| _verdict | function | |

## Dependencies
- `proc_lib.registry` (`specs/proc_lib/registry.spec.md`) — status constants; `proc_lib.corelink` — the clock.

## Test cases
- GIVEN each status THEN its verdict text (`test_agent_surface.py`, `test_scheduled.py`).
- GIVEN a cost dict THEN its text (`test_peak_rss.py`).
