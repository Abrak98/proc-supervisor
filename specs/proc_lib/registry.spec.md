# registry — spec

> Permanent contract and source of truth. Edit THIS file directly on any change.
> Every source `.py` implementing it carries `# spec: <path-to-this-file>`.

## Intent
The job registry of the engine (`specs/proc_lib/core.spec.md`): the paths of a job's files, the record load/save, the daily history, the live index and per-session lists, the registry lock, the `.rc` parse and the reconcile that brings a recorded status in line with the process.

## Rationale / Key decisions
- **Behaviour is the engine's, documented in `core.spec.md`** (JSON-per-job registry, history by UTC day, live index, reconcile). This module is where that logic lives; nothing here differs from it.
- **Every path helper reads `core.BASE` at call time**, and the lock, the clock and the index writer are called as `core._with_lock`, `core._now`, `core._index_set`, so replacing them on `proc_lib.core` takes effect here.
- **Constants of the registry** (status values, exit codes, environment variable names, history and follow bounds) are defined here and re-exported by `core`.

## Rejected alternatives
- **A database or a per-module copy of `BASE`.** Rejected for the reasons in `core.spec.md`; a copy would defeat the call-time read.

## Acceptance criteria
- GIVEN a record, history and index operations WHEN run THEN the results are those of `core.spec.md` (`test_history.py`, `test_live_index.py`, `test_reconcile.py`, `test_peak_rss.py`).

## State
`$PROC_HOME/<job>.{json,log,rc,rss,schedenv}`, `history/<UTC day>/`, `.live-index`, `sessions/`, `.lock`.

## Methods
- `_meta_path`, `_log_path`, `_rc_path`, `_rss_path`, `_schedenv_path`, `_lockfile`, `_ensure_base` — paths under `core.BASE`.
- `_load(name)`, `_save(meta)`, `_all_metas(history)`, `_archive(meta, now)`, `housekeeping(now)` — records and history.
- `_read_index`, `_write_index`, `_rebuild_index`, `_index_locked`, `_index_set`, `_live_index`, `_session_add`, `_session_names` — the live index and session lists.
- `_with_lock(fn)` — the registry flock.
- `_parse_rc`, `_read_rc_file`, `_read_rc`, `_reconcile`, `_reconcile_and_save`, `_current`, `_apply_next_run` — finishing a job from its `.rc`.

## Invariants
- The lockfile is created on demand and never unlinked.
- The index is replaced atomically and written only under the lock.
- Standard library plus `proc_lib.cost` (home test) and `proc_lib.procfs` (`_alive`).

## Formulas
n/a

## I/O contracts
Those of `specs/proc_lib/core.spec.md`; nothing is added or changed by the split.

## Names
| canonical | kind | forbidden-aliases |
|-----------|------|-------------------|
| housekeeping | function | |
| INDEX_VERSION | const | |

## Dependencies
- `proc_lib.procfs` (`specs/proc_lib/procfs.spec.md`) — `_alive` for reconcile.
- `proc_lib.cost` (`specs/proc_lib/cost.spec.md`) — `is_default_home` for the history bound.
- `proc_lib.corelink` — the replaceable names.

## Test cases
- GIVEN a day boundary THEN finished jobs move to their history day and old days are deleted (`test_history.py`).
- GIVEN a deleted index THEN it is rebuilt from the records (`test_live_index.py`).
- GIVEN a job that ended with no one asking THEN reconcile finalises it from `.rc` (`test_reconcile.py`).
