# spawn — spec

> Permanent contract and source of truth. Edit THIS file directly on any change.
> Every source `.py` implementing it carries `# spec: <path-to-this-file>`.

## Intent
Start a detached job for the engine (`specs/proc_lib/core.spec.md`): the bash wrapper that records the exit code and cost, the spawn itself, the schedule sidecars and next-run bookkeeping, and the masking of secret `--env` values. Shared by `start` (now) and the launcher (at the scheduled time).

## Rationale / Key decisions
- **The wrapper, not a parent, reports the exit code**: `proc` never stays the job's parent, so the wrapper writes `$?`, `times`, `/proc/<pid>/io` and peak RSS to the `.rc` sidecar (`core.spec.md`).
- **Replaceable names are read through `core`**: `_gnu_time`, `_SCOPE_OK` and the clock, so a replacement set on `proc_lib.core` reaches the spawn.
- **Secrets never reach the registry**: values of keys that look like secrets are stored masked; the job gets the real value through its environment.

## Rejected alternatives
- **`exec`-ing the job from the wrapper.** Rejected: the `.rc` line must run after the job.

## Acceptance criteria
- GIVEN `--timeout` WHEN the job is spawned THEN it is wrapped in coreutils `timeout` (`test_lifecycle.py`).
- GIVEN a secret-looking `--env` key WHEN recorded THEN the value is masked (`test_agent_surface.py`).
- GIVEN a scheduled job WHEN the launcher starts it THEN its saved environment is used and the sidecar removed (`test_scheduled.py`).

## State
`$PROC_HOME/<job>.schedenv` (0600), the `.rc` and `.rss` sidecars it makes the wrapper write.

## Methods
- `_wrapper_script(quoted, rcp, time_bin, rssp)`, `_gnu_time()`, `_scope_works()`, `_scope_prefix(name)`, `_spawn(...)` — the process start.
- `_write_schedenv`, `_read_schedenv`, `_drop_schedenv`, `_run_spec`, `_set_next_run` — schedule data.
- `_parse_env`, `_masked_env`, `_is_secret_key`, `_owner_from_env` — the environment of a start.
- `_LaunchError` — an operator-facing spawn failure.

## Invariants
- `_spawn` returns at once after the fork; it never waits for the job.
- Standard library only; `subprocess`, `shutil` and `heavy_job_lock` are imported where used.

## Formulas
n/a

## I/O contracts
Those of `specs/proc_lib/core.spec.md`; nothing is added or changed by the split.

## Names
| canonical | kind | forbidden-aliases |
|-----------|------|-------------------|
| _LaunchError | class | |

## Dependencies
- `proc_lib.registry` (`specs/proc_lib/registry.spec.md`) — paths and constants; `proc_lib.procfs` (`specs/proc_lib/procfs.spec.md`) — `_capture_starttime`; `proc_lib.heavy_job_lock` (`specs/proc_lib/heavy_job_lock.spec.md`) — claim for `--heavy`; `proc_lib.corelink` — the replaceable names.

## Test cases
- GIVEN a job THEN `.rc` holds its exit code and cost (`test_peak_rss.py`, `test_lifecycle.py`).
- GIVEN no systemd scope THEN the job starts unscoped (`test_scheduled.py`).
