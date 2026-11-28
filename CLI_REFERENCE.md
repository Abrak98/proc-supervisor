# CLI Reference — `proc`

State dir: `$PROC_HOME` (default `~/.local/state/proc/`). All timestamps UTC.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | success / terminal reached |
| `2` | usage error or conflict (duplicate running name, `rm` on a running job, bad cwd, missing command) |
| `3` | `wait` / `logs --follow` timed out while the job was still running |
| `4` | named job not found in the registry |
| `5` | `wait`: the job ended other than `exited` with rc 0 (non-zero or unknown rc, `killed`, `timeout`) |
| `6` | no launcher backend can fire scheduled jobs (`start --at`, `reschedule`, `launcher`) |
| `7` | `hook status` / `hook install`: the Claude Code hook is not registered in a config dir |
| `8` | `kill` / `rm --force`: a member of the job's session is still alive after SIGKILL; the job is not recorded `killed` and the record's `kill_survivors` names the pids |

## `proc start --name N [--at TIME] [--timeout S] [--cwd D] [--env KEY=VAL]... [--heavy [--force-concurrent]] -- CMD...`

Launch `CMD` detached under name `N`; print the pid and log path, return
immediately. Everything after `--` is the command and its args.

- `--name N` — unique job name (required). A name with a `running` job is
  rejected; a name with a terminal job must be `rm`'d first (the log is never
  clobbered).
- `--timeout S` — OS-enforced cap in seconds via coreutils `timeout`
  (`--signal=TERM --kill-after=10`): SIGTERM at the deadline, SIGKILL 10s later.
  A fired timeout is reported as status `timeout` (exit_code 124).
- `--at TIME` — do not start now: register the job to start at `TIME`, an ISO 8601
  time that carries its zone (`2026-10-23T13:19:00Z`, `...+03:00`; a time without a zone
  is exit 2). Nothing runs until then: the entry has status `scheduled` and no process.
  A launcher (see `proc launcher`) starts it, within a minute of the time, exactly once;
  after that it is an ordinary running job. A time in the past starts at the next launcher
  pass. The job starts with the environment of the `proc start --at` call (kept in a
  private `0600` file next to the registry, never in the registry JSON), `--heavy` is
  checked at start time (a busy lock defers the start to the next pass), and a start that
  cannot spawn ends as `exited` with `launch_error` in the registry and a line in the log.
  Exit 6 and nothing registered if no launcher can be installed. Called from inside a
  running job with that job's own `--name` (the job sees it as `PROC_JOB_NAME`), it sets
  the job's next run instead: when the current run ends, the entry becomes `scheduled`
  with the new command, so a "check, then run or retry later" script exits instead of
  sleeping. The log keeps growing across runs.
- `--cwd D` — working directory (default: current dir).
- `--env KEY=VAL` — set an environment variable for the job, on top of the
  caller's environment; repeatable. `KEY` must match `[A-Za-z_][A-Za-z0-9_]*`
  and `VAL` may contain `=`; a malformed pair is exit 2 and nothing is launched.
  Recorded in the registry's `env` field; the value of any key containing
  `TOKEN`, `PASSWORD`, `SECRET` or `KEY` (case-insensitive) is stored and shown
  as `***` — the job itself receives the real value.
- `--heavy` — register as a RAM-heavy job; refuses to start (exit 2) while
  another heavy job is running. `--force-concurrent` bypasses that check.

## `proc reschedule N --at TIME`

On a `scheduled` job: move its start to `TIME`. Called from inside the running job `N`
itself: run the same command, cwd, timeout and environment again at `TIME`, once this run
ends (replaces an earlier next run). On another running job: exit 2. On a finished job:
exit 2 (`rm` it and `start --at` again). Exit 4 if unknown.

## `proc launch-due`

Start every `scheduled` job whose time has come, once each, oldest first; print one line per
job. Run every minute by the launcher; safe to run by hand and safe to run concurrently
(the registry lock makes each start happen once).

`launch-due` reads only the live index (a few KB), never a finished or not-yet-due record,
and takes the registry lock only around each start. It also finalises running jobs whose
process has gone. Each pass appends one line (CPU, bytes read, lock time, jobs started) to
`$PROC_HOME/cost/<date>.jsonl`; a pass that started nothing and exceeds its bound (0.5 s wall,
0.3 s CPU, 1 MB read, 40 MB RSS, 0.2 s lock) runs the command in `PROC_COST_ALERT_CMD` (else the
first line of `~/.config/proc/cost_alert_cmd`; for a `$PROC_HOME` other than the default, only
`$PROC_HOME/cost_alert_cmd`) once per day, with `COST_ALERT_KEY` and
`COST_ALERT_JSON` in its environment. Jobs it starts run in their own systemd scope.

## `proc launcher status|install|uninstall`

The launcher is what runs `launch-due` each minute: a systemd user timer
(`proc-launch.timer`), or a crontab line when user systemd is unavailable. `start --at`
and `reschedule` install it when missing or broken; `install` rewrites it explicitly
(idempotent); `status` prints one line and exits 6 when scheduled jobs would not fire.

## `proc hook status|install|uninstall|unwrap [--config-dir DIR]...`

The Claude Code PreToolUse hook that makes a project's own Bash checks judge `CMD` of
`proc start ... -- CMD` as if it had been the call. `install` writes one `Bash` entry into
`DIR/settings.json` (default `$CLAUDE_CONFIG_DIR`, else `~/.claude`; repeat the flag for more
seats; the previous file is kept as `settings.json.bak-<date>-proc-hook`); it is idempotent and
touches nothing else. `uninstall` removes it and records an opt-out beside the registry, so
`proc start` does not put it back; `install` clears the opt-out. `status` prints one line per
dir and exits 7 when an entry is missing. `unwrap` is the hook itself: it reads the tool call
JSON on stdin and answers as a PreToolUse hook (exit 2 with the refusing hook's message, or a
`permissionDecision` of `ask`/`allow`).

## `proc list` / `proc ls [--running] [--scheduled] [--session ID] [--all] [--exited --since HOURS] [--json [--full]]`

Aligned table `NAME PID STATUS ELAPSED TIMEOUT START CMD`; running jobs first, then
scheduled jobs by start time (`START` holds the scheduled time, `-` otherwise).
Prints `no jobs` when nothing matches. `--json` prints a JSON array (same order) of the
objects `status --json` prints, filtered the same way; `[]` when empty.

Without a flag it lists the live jobs (running and scheduled), read from a small index that
every state change keeps current, so it never opens a finished job's record and costs the same
at any registry size. A job still marked running whose process has gone is finalised as it is
listed (and, being finished, is not shown).

- `--running` / `--scheduled` — only that state (both: the union).
- `--session ID` — the jobs the Claude session `ID` started, in any state (add `--running` /
  `--scheduled` to narrow).
- `--all` — every job in the registry (live folder and history), finished ones included. This
  reads every record, so it is slow on a large registry.
- `--json --full` — the whole record of each listed job (command, cwd, env, log path; the
  shape of `status --json`) instead of the light objects; reads each listed job's record.
- `--exited --since HOURS` — the jobs that ended in the last `HOURS` hours (a positive number),
  read from the live folder and the history day folders that window covers; not combinable with the other
  filters. With `--json` the same light objects as above.

A finished job's record, log, `.rc` and `.rss` stay in `$PROC_HOME` when it ends. The first
`proc` call of a UTC day moves the jobs that ended before the previous UTC day began into
`$PROC_HOME/history/<YYYY-MM-DD>/` (UTC day of the end), so a finished job keeps its path for at
least 24 h; its `log_path` says where it is. `status`, `logs`, `wait`, `rm` and `list --all`
find it in either place. That same call deletes whole day folders older than 14 days
(`PROC_HISTORY_DAYS`, else `history_days` in `~/.config/proc`, or `$PROC_HOME/history_days` for
a home other than the default).

## `proc archive`

Move every finished record still in `$PROC_HOME`, with its log and sidecars, into its day
folder at once, whatever its age; those past the retention bound are deleted. The daily sweep
does this for jobs older than a day; `archive` is the one-time move of a registry that grew
flat before history folders existed. Safe to rerun.

## `proc reindex`

Rebuild the live index and the per-session lists from every record (one full scan). The index
is kept by every state change and rebuilt automatically when missing; run this after records
were edited or deleted by hand, or once after upgrading from a version without the index.

## `proc status N [--json]`

Job `N`, reconciled. Exit 4 if unknown. The first line is always the verdict:

```
N: running pid=1234 for 2h13m
N: exited rc=0 after 41m5s        # rc=? when the exit code is unknown
N: killed (timeout) after 1h0m
N: killed (proc kill) after 5m2s
N: scheduled for 2026-10-23T13:19:00Z (in 9h26m)   # or (overdue 3m5s)
N: cancelled before start         # `proc kill` on a scheduled job
```

followed by indented detail lines — `log:` (always line 2), `cmd:`, `cwd:`,
`started:`, `ended:` (terminal only), `timeout:`, `env:` (only if set),
`status:` + `exit_code:`; `cost:` (a finished job's CPU time, bytes read and written and peak RSS, as its
wrapper recorded them; "peak RSS not recorded (<why>)" when GNU time was unavailable); `next run:` while a running job has asked to run again, and
`overdue:` when a scheduled job is more than 3 minutes late (the launcher is not firing).

`--json` prints the metadata object with `name`, `status`, `alive`,
`exit_code`, `pid`, `elapsed_s`, `log_path` first, then every other stored
field. `alive` is `true` iff the job is `running` after reconcile.

Registry fields (a scheduled entry also has `scheduled_ts`, `scheduled_iso`, `heavy`,
`force_concurrent`; a started one `scheduled_for`, `launch_error` on a failed start,
`next_run` on a running job that asked to run again; `owner` on a job started by a caller
whose environment named one): `name`, `cmd` (list), `cwd`, `pid`, `pgid`, `start_ts`
(epoch float), `start_iso` (UTC ISO8601), `pid_starttime`, `timeout`, `env`
(object; absent on entries written by versions before it existed), `status`,
`exit_code`, `end_ts`, `log_path`, `rc_path`.

`owner` is `{"session": ..., "project": ...}` read at `start` (or at scheduling time for `--at`)
from `CLAUDE_CODE_SESSION_ID` and `COORD_PROJECT`; a variable that is unset or empty leaves its key
out, and with neither set the entry has no `owner` at all (never guessed). `status` prints it as
an `owner:` line; `list --json` and `status --json` carry the field. A scheduled run keeps the
owner of the session that scheduled it. Entries written before this field existed have none.

## `proc logs N [--since TS] [--tail K] [--follow]`

Print job `N`'s combined stdout+stderr log.

- `--since TS` — only the lines after `TS`, an ISO 8601 time with a zone
  (`2026-10-31T12:00:00Z`, `...+03:00`); a naive or unparsable value is refused (exit 2).
  The log holds the job's raw output and has no time of its own, so a line is timed by a
  leading ISO 8601 time with a zone if it has one, else by the nearest timed line above it;
  lines above the first timed line are dropped. A log with no timed line is judged whole by
  its last-modified time. Applied before `--tail`, and to the first dump of `--follow`.
- `--tail K` — only the last `K` lines.
- `--follow` — stream new output; auto-stops on terminal status or a 600s hard
  cap (exit 3 if the cap hits), never hangs forever.

## `proc wait N [--timeout S]`

Block until job `N` is terminal (a `scheduled` job: through its start to its end), then print its verdict line (same format as
`status`). Polls every 0.5 s without taking the registry lock while the job is simply running. Default cap 3600s; `--timeout S` overrides. Exit 0 if the job exited
with rc 0, 5 if it ended any other way, 3 if still running when the cap hits
(the verdict line then ends with `(wait timed out)`), 4 if unknown.

## `proc kill N [--force]`

Cancel a `scheduled` job (status `killed`, nothing signaled) or terminate job `N`; killing
a running job also drops its next run. SIGTERM the whole session, wait a 10s grace, then SIGKILL
whatever remains; `--force` sends SIGKILL immediately. Each member is re-checked (starttime
and session) right before it is signaled, through a pidfd where the kernel has one, so a pid
reused after the scan is never hit. A descendant that left the session with `setsid()` is not
reached. If a member is still alive after SIGKILL the job keeps its status, `kill_survivors`
names the pids and the exit is 8. Registry-only and
PID-reuse-guarded — if the recorded process is gone/reused, the job is
reconciled to terminal and nothing is signaled. A cleanly-exited-but-stale job
is reported as `already exited` with its real exit code.

## `proc rm N [--force]`

Delete job `N`'s metadata, log, and rc sidecar. Refuses a `running` job; with
`--force` it kills the job first, then removes it. Exit 4 if unknown.

## `proc clean [--older-than HOURS]`

Remove all terminal jobs (running jobs are never touched). With `--older-than H`
only those whose `end_ts` is older than `H` hours (default 24). The scan takes no lock; the
registry lock is held only per removal. A daily run that keeps a week of history is
`proc clean --older-than 168`.

## Status values

`scheduled`, `running`, `exited`, `killed`, `timeout`. (`lost` retired; read-only for legacy
entries.)
