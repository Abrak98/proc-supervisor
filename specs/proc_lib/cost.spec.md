# cost — spec

> Permanent contract and source of truth. Edit THIS file directly on any change.
> Every source `.py` implementing it carries `# spec: <path-to-this-file>`.

## Intent
Make the cost of the minute-by-minute `proc launch-due` pass visible and bounded without any
resident process: the pass measures itself with counters the kernel already keeps, appends one
line to proc's own daily file, and raises one alert per day when it exceeds its bound.

## Rationale / Key decisions
- **Measure at the end of the work, not by a watcher.** Two reads (`getrusage` of self and
  children, `/proc/self/io`) plus wall time cost nothing next to the pass, and a watcher would
  be a process to supervise.
- **Process totals, not deltas.** CPU, rchar and peak RSS are the process's totals since it
  started, interpreter start-up and imports included: that is what a bound on "a pass reads
  under 1 MB" means. Wall time is from the start of the pass.
- **One O_APPEND write per line** (under 512 bytes), so concurrent passes never interleave a
  line and no lock is taken. The file is `$PROC_HOME/cost/<UTC date>.jsonl`; when a new day's
  file is created the module deletes its own cost files and alert stamps older than 14 days (no
  sweep job).
- **The same 1 MB read bound holds for a routine `proc list --running [--session ID] --json`
  call** (a hook or board check runs it many times a day): it reads the live index only, skips
  argparse and prints no command texts. `list --session ID` without `--running` reads one record
  per job that session started and is not a routine call.
- **Bounds apply to a pass that started nothing.** A pass that started or failed a job does real
  work (spawn, log, record) and is only recorded. Bounds of the steady state: wall 0.5 s, CPU
  0.3 s, rchar 1 MB, RSS 40 MB, lock held 0.2 s.
- **One alert per key per day, taken with O_EXCL.** The stamp `cost-alert/<date>/proc.<path>`
  is created exclusively; only the pass that creates it runs the alert command, detached and
  never waited for. Every later excess that day only appends its line.
- **The alert command is configuration, never named here, and it belongs to a registry.** For
  the default registry (`~/.local/state/proc`): the environment variable `PROC_COST_ALERT_CMD`,
  else the first non-comment line of `$XDG_CONFIG_HOME/proc/cost_alert_cmd` (default
  `~/.config/proc/`). For any other `$PROC_HOME` (a test, a scratch home): only the first
  non-comment line of `<home>/cost_alert_cmd`; the user's variable and file are not read, so a
  run there records its line and posts nothing, with no test having to unset anything
  (2026-11-12: a scratch registry's pass, 3 s CPU because it ran inside pytest, posted a false
  alert through the user's configuration). With none configured
  nothing is posted and no stamp is taken, so configuring it later the same day still posts.
  The command gets `COST_ALERT_KEY` (`cost:proc:launch-due`) and `COST_ALERT_JSON` (key, the
  record, the bounds) in its environment.
- **Measuring never breaks the supervisor.** A write failure prints one stderr line and the
  pass returns 0.

## Rejected alternatives
- **A resident collector or a scan of job records for cost.** Rejected: the audit that
  motivated this found exactly such periodic scans to be the cost.
- **Posting on every excess.** Rejected: a path over its bound on every run would post 1,440
  times a day.

## Acceptance criteria
- GIVEN a launch pass WHEN it ends THEN exactly one line is appended to today's file with `ts`,
  `tool`, `path`, `wall_s`, `cpu_s`, `rss_mb`, `rchar_mb`, `read_mb`, `write_mb`,
  `lock_wait_s`, `lock_hold_s`, `n`, `launched`, `failed`, `finalised`.
- GIVEN a pass over a bound that started nothing WHEN it ends THEN its line has `over` and the
  configured command runs once that day; a second such pass appends its line and posts nothing.
- GIVEN no alert command is configured WHEN a pass is over a bound THEN no stamp exists.
- GIVEN a `$PROC_HOME` other than the default WHEN a pass is over a bound and the user's
  variable and file name a command THEN the line is recorded, no stamp exists and nothing posts.

## State
`$PROC_HOME/cost/<date>.jsonl`, `$PROC_HOME/cost-alert/<date>/<tool>.<path>`.

## Methods
- `Probe()` / `Probe.stats()` — start and read the process totals.
- `over_bounds(stats, bounds) -> list[str]`.
- `append_line(base, record, today=None)` — the O_APPEND write plus the daily prune.
- `alert_command(base=None) -> list[str] | None`, `raise_alert(base, path, record, bound) -> bool`.
- `is_default_home(base) -> bool` — whether `base` is the default registry (`~/.local/state/proc`); the
  user's alert and history settings apply to that registry only.
- `finish_launch_due(base, probe, *, entries, launched, failed, finalised, lock_wait_s,
  lock_hold_s)` — build the line, apply the bounds, append, alert; never raises.

## Invariants
- No import from `proc_lib.core`. Standard library only; `subprocess` and `shutil` are imported
  where used.
- The alert is never waited for and never blocks the pass.

## Formulas
n/a

## I/O contracts
A line is one JSON object, compact separators, newline-terminated.

## Names
| canonical | kind | forbidden-aliases |
|-----------|------|-------------------|
| Probe | class | |
| LAUNCH_DUE_BOUNDS | const | |
| ALERT_ENV | const | |

## Dependencies
- Python standard library only.

## Test cases
- GIVEN two passes THEN two lines with the fields above (`test_live_index.py`).
- GIVEN bounds forced below the measured figures THEN every pass is marked `over` and exactly
  one alert posts (`test_live_index.py`).
- GIVEN no configuration THEN no stamp and no post (`test_live_index.py`).
- GIVEN a command in the config file THEN `alert_command` returns its argv (`test_live_index.py`).
- GIVEN a scratch home over its bound and a user configuration THEN nothing posts
  (`test_live_index.py`).
- GIVEN a pass that started a job THEN it is recorded without `over` (`test_live_index.py`).
- GIVEN cost files and stamps older than 14 days WHEN a new day's file is created THEN they are
  deleted (`test_live_index.py`).
