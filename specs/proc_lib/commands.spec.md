# commands — spec

> Permanent contract and source of truth. Edit THIS file directly on any change.
> Every source `.py` implementing it carries `# spec: <path-to-this-file>`.

## Intent
The `cmd_*` subcommand implementations of `proc` (`start`, `reschedule`, `launch-due`, `launcher`, `hook`, `list`, `reindex`, `status`, `logs`, `wait`, `kill`, `rm`, `clean`, `archive`), exposed by `proc_lib.core` and wired by `proc_lib.cli`. Their behaviour is the Methods and Acceptance criteria of `specs/proc_lib/core.spec.md`.

## Rationale / Key decisions
- **Commands compose the other modules; they hold no registry or signal logic of their own**, apart from `kill`'s drain loop.
- **`KILL_GRACE_SECONDS` and `KILL_DRAIN_POLLS` are read through `core`** so a replacement set on `proc_lib.core` (the escalation test) shortens the waits; `_signal_session` is read the same way.

## Rejected alternatives
- **Putting the commands back with the registry.** Rejected: one 2,300-line module mixed signal safety, storage and presentation.

## Acceptance criteria
- GIVEN any subcommand WHEN run THEN it behaves as `core.spec.md` states, with the same exit codes (`test_cli.py`, `test_kill.py`, `test_lifecycle.py`, `test_scheduled.py`, `test_logs_since.py`, `test_history.py`).

## State
Reads and writes the registry through `proc_lib.registry`; no state of its own.

## Methods
- `cmd_start`, `cmd_reschedule`, `cmd_launch_due`, `cmd_launcher`, `cmd_hook` — start, schedule, launcher and hook.
- `cmd_list`, `cmd_status`, `cmd_logs`, `cmd_wait`, `cmd_reindex` — reading.
- `cmd_kill`, `cmd_rm`, `cmd_clean`, `cmd_archive` — ending and removing.
- `_parse_zoned`, `_parse_at`, `_since_filter` — time arguments.

## Invariants
- `cmd_start` returns at once; `cmd_wait` and `cmd_logs --follow` have hard ceilings; `cmd_kill` signals only through `_signal_session`.
- Standard library at runtime; `launcher`, `claude_hook`, `hook_judge`, `heavy_job_lock` are imported where used.

## Formulas
n/a

## I/O contracts
Those of `specs/proc_lib/core.spec.md`; nothing is added or changed by the split.

## Names
| canonical | kind | forbidden-aliases |
|-----------|------|-------------------|
| cmd_start | function | |
| cmd_kill | function | |

## Dependencies
- `proc_lib.registry` (`specs/proc_lib/registry.spec.md`), `proc_lib.procfs` (`specs/proc_lib/procfs.spec.md`), `proc_lib.render` (`specs/proc_lib/render.spec.md`), `proc_lib.spawn` (`specs/proc_lib/spawn.spec.md`), `proc_lib.cost` (`specs/proc_lib/cost.spec.md`), `proc_lib.corelink` — the replaceable names.

## Test cases
- GIVEN the subcommands THEN their tests in `test_cli.py`, `test_kill.py`, `test_lifecycle.py`, `test_scheduled.py`, `test_logs_since.py`, `test_history.py`, `test_safety.py`.
