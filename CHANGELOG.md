# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `proc list --exited --since HOURS` (with `--json`) lists the jobs that ended in that window,
  reading the live folder and only the history day folders it covers.
- `proc archive` moves every finished record still in the live folder into the history folders.

- A finished job's `cost` carries its peak RSS (`maxrss_kb`), read from GNU time as the job's
  reaping parent and folded into `.rc` by the wrapper (about 1 MB resident per job); without GNU
  time `status` says "peak RSS not recorded" and why. Exit code, signal and log are unchanged.

### Changed

- `kill` re-verifies each session member (starttime, session, not dead) right before signaling
  it, through a pidfd where the kernel supports it, so a pid reused after the `/proc` scan is
  never signaled.
- README and CLI_REFERENCE describe the whole current surface: `--heavy`, `list --json --full`
  and its filters, `archive`, `reindex`, the history and index files, exit code 8, per-job
  cost, and a section on what `proc` does not do. `tests/test_docs.py` fails when a command,
  option or exit code is added without them.

- The default state directory is `~/.local/state/proc` (an earlier release used a different directory). A registry that lives
  elsewhere keeps working by setting `PROC_HOME` to it; nothing is moved. The seats whose Claude Code
  settings are generated from a shared source default to `~/.claude` alone; list more through
  `SEAT_SETTINGS_SEATS`.
- The heavy-job lock module is a clean-room rewrite from its spec; its behaviour and lockfile
  format are unchanged.
- A finished job's record, log, `.rc` and `.rss` stay at `$PROC_HOME/<job>.*` when it ends, so
  a job read right after `proc wait` is where it always was. The first `proc` call of a UTC day
  moves the jobs that ended before the previous UTC day began into `$PROC_HOME/history/<UTC day
  of its end>/` (the flat folder holds about two days of finished jobs) and deletes whole day
  folders older than 14 days (`PROC_HISTORY_DAYS`, `history_days`). `status`, `logs`, `wait`,
  `rm`, `clean` and `list --all` find a job in both places; a moved job's `log_path` names the
  new place. `proc archive` makes the one-time move of a registry that grew flat.

- The cost-alert command belongs to a registry: the user's `PROC_COST_ALERT_CMD` and
  `~/.config/proc/cost_alert_cmd` apply to the default `~/.local/state/proc` only; any other
  `$PROC_HOME` (tests, scratch homes) records its cost line and posts nothing unless it holds
  its own `cost_alert_cmd`.
- `proc list` shows the live jobs (running and scheduled) by default, read from a live index
  that every state change keeps current; `--running`, `--scheduled`, `--session ID` and `--all`
  (history, the only form that reads every record) select otherwise, and `--json` honours them.
  Finished jobs are no longer listed without `--all` or `--session`.
- `proc launch-due` finds its work in the index instead of reading every record, takes the
  registry lock only around each start, finalises running jobs whose process has gone, and starts
  each job in its own systemd scope; with nothing due it reads under 1 MB and uses under 0.1 CPU
  seconds at any registry size. The service unit gains `IOAccounting=yes` (`proc launcher
  install`, or any `start --at`, upgrades an installed unit in place).
- `proc wait`, `proc status` and `proc logs` read the one record without the registry lock.
- `proc clean` scans without the registry lock and holds it only per removal.

### Added

- `proc reindex` rebuilds the live index and the per-session job lists; both are also rebuilt
  automatically when missing.
- A finished job's record carries `cost` (CPU seconds, bytes read and written), written by the
  job's own wrapper into `.rc`; `proc status` shows it.
- Each `launch-due` pass appends a cost line to `$PROC_HOME/cost/<date>.jsonl` and, over its
  bound, runs the user-configured alert command once a day (`PROC_COST_ALERT_CMD` or
  `~/.config/proc/cost_alert_cmd`).

- `proc hook status|install|uninstall`: a Claude Code PreToolUse hook, registered in the user's
  Claude settings, that runs the current project's own Bash hooks on the command behind
  `proc start ... -- CMD` and returns the strictest verdict (deny, ask, allow), failing closed
  when a hook crashes or times out. `proc start` registers it once when a seat's settings lack
  it (`uninstall` opts out); `status` exits 7 when it is not registered.
- `proc logs --since TS`: only the log lines after an ISO 8601 time with a zone. Lines are timed by
  their own leading time, else by the line above; an untimed log is judged by its mtime.
- Job owner: `proc start` records `owner: {session, project}` from `CLAUDE_CODE_SESSION_ID` and
  `COORD_PROJECT` (a scheduled job keeps the owner of the session that scheduled it); with neither
  variable set there is no `owner` key. Shown by `status` and carried by `--json`.
- Scheduled start: `proc start --at TIME` registers a job in the new `scheduled` status and
  starts no process; a per-minute launcher (systemd user timer, cron fallback; `proc launcher`,
  `proc launch-due`) starts each due entry exactly once. `proc kill` cancels one, `proc wait`
  waits through its start to its end, `proc list` has a `START` column. A running job sets its
  own next run with `proc start --at` (same `--name`) or `proc reschedule N --at TIME`; every
  job now sees `PROC_JOB_NAME`. New exit code 6 (no launcher backend works).
- `proc start --env KEY=VAL` (repeatable): set environment variables for the job
  without an `env` wrapper command. Recorded in the registry's new optional `env`
  field; the value of any key containing `TOKEN`, `PASSWORD`, `SECRET` or `KEY`
  is stored and displayed as `***` (the job receives the real value).
- `proc status --json` and `proc list --json`: machine output with `name`,
  `status`, `alive`, `exit_code`, `pid`, `elapsed_s`, `log_path` first.

### Changed

- `proc status` now prints a human verdict line first
  (`N: running pid=1234 for 2h13m` / `N: exited rc=0 after 41m5s` /
  `N: killed (timeout) after 1h0m`), then the log path and the other details. The
  previous JSON output is available as `proc status --json` (keys reordered,
  plus `alive` and `elapsed_s`).
- `proc wait` exits 5 when the job ends any way other than `exited` with rc 0
  (it used to exit 0 for every terminal status), and prints the same verdict
  line as `status`.
- Packaging moved to uv: the build backend is `uv_build` (was setuptools), the
  development tools are the PEP 735 `dev` dependency group (was the `[dev]`
  extra), and `uv.lock` pins them. Install with `uv tool install`, develop with
  `uv sync` / `uv run pytest -q`. The license is declared as the SPDX expression
  `MIT` with `LICENSE` as its license file.

### Compatibility

- The registry format is unchanged apart from the new optional `env` field;
  entries written by 0.1.0 (no `env`) are listed, waited on and killed as before.

### Fixed

- `gates` CI job was failing on every push/PR: `pre-commit run --all-files`
  tried to clone the private `Abrak98/headspec` hook repo, which a fresh GitHub
  Actions runner has no credentials for. CI now runs against a trimmed
  `.pre-commit-ci.yaml` that omits the headspec repo block until it is published.

## [0.1.0] - 2026-05-31

### Added

- `heavy_job_lock`: stdlib process-level RAM mutex, wired into `proc start --heavy`
  (free check before launch, claim after), with a `--force-concurrent` bypass.
  Global lock, atomic `O_CREAT|O_EXCL` claim, PID+mtime stale reclaim, self-only
  release, re-entrancy.
- GitHub repository published; pre-commit hooks installed (ruff, ruff-format,
  standard hygiene hooks, gitleaks, pytest).
- First test suite (43 tests, runs in ~5s) covering lifecycle, reconcile,
  kill/escalation, and PID-reuse guard.
