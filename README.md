# proc-lib

`proc` — a managed background-process CLI for autonomous agents.

Launch long-running processes (`research run`, test suites, agent runs, smoke
tests, builds) **detached**, then monitor, tail, wait on, and kill them through
one standardized, logged, registry-backed interface — so an autonomous agent
(or a human) never hangs on a foreground command or an interactive `kill`, and
never loses track of a job across a crash of the agent or its session. The registry
survives a WSL restart; a process that was running when WSL stopped does not, and is
reported as finished with whatever exit code it left.

stdlib-only at runtime, by design: a process supervisor must not depend on
anything that could itself break the very runs it manages.

Linux (or another `/proc`-filesystem OS) only — the PID-reuse guard reads
`/proc/<pid>/stat` directly and has no fallback for platforms without it.

## Why

Running long jobs from an autonomous loop has three failure modes that `proc`
removes:

1. **Foreground hang.** A long or wedged command blocks the caller until a human
   intervenes. `proc start` returns immediately; the job runs detached.
2. **Interactive-kill hang.** Killing a wedged job used to need an interactive
   `kill` confirmation. `proc kill` is registry-scoped and safe to allow-list,
   so it never prompts.
3. **Lost jobs after a crash.** The built-in background-task mechanism doesn't
   survive a WSL crash. `proc` keeps a persistent on-disk registry, so status is
   recoverable afterwards.

It also gives every job an **OS-enforced timeout watchdog** (`--timeout`) so a
runaway job dies on its own even if nothing is watching.

## Install

```bash
uv tool install ~/Projects/proc-lib
# or, for development:
uv tool install --editable ~/Projects/proc-lib
# pipx works too:
pipx install ~/Projects/proc-lib
```

This installs the `proc` command. State lives under `$PROC_HOME`
(default `~/.local/state/proc/`).

`proc` needs Python >= 3.12. uv picks a matching interpreter by itself and
downloads one when none is installed; pass `--python 3.13` to choose it. With
pipx, an older default interpreter fails with `requires a different Python`;
pass `--python python3.12` (or newer). A non-editable install is a snapshot of
the checkout, so pick up a later change with
`uv tool install --reinstall ~/Projects/proc-lib` (pipx:
`pipx install --force [--python ...] ~/Projects/proc-lib`). Jobs already
running keep running across the reinstall, and the registry format is kept
backward compatible, so the new version lists, waits on and kills them.

## Commands

| Command | What it does |
| --- | --- |
| `proc start --name N [--timeout S] [--cwd D] [--env KEY=VAL]... -- CMD...` | Launch `CMD` detached, return immediately. With `--timeout` the OS hard-kills it after `S` seconds (coreutils `timeout`: SIGTERM, then SIGKILL after a 10s grace). `--env` (repeatable) sets a variable for the job; values of `*TOKEN*`/`*PASSWORD*`/`*SECRET*`/`*KEY*` keys are recorded as `***`. |
| `proc start ... --heavy [--force-concurrent] -- CMD...` | Mark the job RAM-heavy: it refuses to start (exit 2) while another heavy job runs, a lock between cooperating jobs, not a memory limit. `--force-concurrent` skips the check. |
| `proc start --at TIME --name N -- CMD...` | Schedule `CMD` for `TIME` (ISO 8601 with a zone) instead of running it: a registry record with status `scheduled`, no process. A per-minute launcher starts it once. Called from inside a running job with its own `--name`, it sets that job's next run, so a "run or retry later" script exits instead of sleeping. |
| `proc reschedule N --at TIME` | Move a scheduled job's start; from inside a running job, set its own next run. |
| `proc launcher status\|install\|uninstall` | The per-minute launcher (systemd user timer, cron fallback); `start --at` installs it when missing. `proc launch-due` is what it runs. |
| `proc hook status\|install\|uninstall` | Claude Code hook that lets a project's own Bash checks judge the command behind `proc start ... -- CMD` as if `CMD` were the call; `start` registers it in the user's Claude settings once when missing. |
| `proc list [--json]` (alias `ls`) | Table: `NAME PID STATUS ELAPSED TIMEOUT START CMD`, running jobs first, then scheduled jobs by start time. Reads only the live index, so it costs the same at any registry size, and reconciles liveness on read. `--json`: array of light per-job objects; `--json --full` the whole record of each. |
| `proc list --running\|--scheduled\|--session ID\|--all\|--exited --since H` | Narrow or widen the listing: one state, the jobs one Claude session started, every record including history (slow on a large registry), or the jobs that ended in the last `H` hours. |
| `proc status N [--json]` | First line is the verdict (`N: running pid=1234 for 2h13m`, `N: exited rc=0 after 41m5s`, `N: killed (timeout) after 1h0m`), second the log path, then details, including `cost:` for a finished job (CPU time, bytes read and written, peak RSS). `--json`: metadata with `status`/`alive`/`exit_code` first. |
| `proc logs N [--since TS] [--tail K] [--follow]` | Print the log, the lines after `TS`, last `K` lines, or follow. `--follow` auto-stops on terminal status or a 600s cap — never hangs forever. |
| `proc wait N [--timeout S]` | Block until terminal and print the verdict line. Exit 0 if it exited rc 0, 5 if it ended any other way, 3 if still running when the cap (default 3600s) hits. |
| `proc kill N [--force]` | SIGTERM the whole session, 10s grace, then SIGKILL (or immediate SIGKILL with `--force`). Registry-only, PID-reuse-guarded. Exit 8 and no `killed` status when a session member survives SIGKILL. |
| `proc rm N [--force]` | Remove a terminal job's metadata + log + rc sidecar. Refuses a running job unless `--force` (which kills it first). |
| `proc clean [--older-than H]` | Remove terminal jobs (default: older than 24h). |
| `proc archive` | Move every finished record still in the live folder into its history day folder at once (the daily sweep does it for jobs older than a day; this is the one-time move for an old flat registry). |
| `proc reindex` | Rebuild the live index and the per-session lists from every record; for use after records were edited or deleted by hand. |

## Examples

```bash
# launch a research run with a 1h OS-enforced cap, return immediately
proc start --name research_x --timeout 3600 -- research run --topic "..."

# launch a test suite in a specific project dir
proc start --name tests_proclib --cwd ~/Projects/proc-lib -- python -m pytest -q

# run something at 13:19 UTC without a process waiting for it
proc start --name nightly --at 2026-10-23T13:19:00Z -- ./nightly.sh
proc wait nightly --timeout 40000     # waits through the start to the end
proc kill nightly                     # before 13:19: cancels it

# a script that retries later by re-scheduling itself instead of sleeping
#   if load_is_high; then proc reschedule "$PROC_JOB_NAME" --at "$(date -u -d '+10 min' +%FT%TZ)"; exit 0; fi

# see what is running
proc list

# follow logs until the job finishes (auto-stops, never hangs)
proc logs research_x --follow

# block until done, then read the verdict and the tail (`;`, not `&&`: wait exits 5
# on a failed job, and a failed job's log is the one you most need)
proc wait research_x --timeout 7200; proc status research_x | head -2; proc logs research_x --tail 40

# pass environment variables without an `env` wrapper
proc start --name api --env PORT=8080 --env LOG_LEVEL=debug -- python -m api

# stop a wedged job and clean it up
proc kill tests_proclib && proc rm tests_proclib

# sweep finished jobs older than a day
proc clean --older-than 24
```

## Scheduled start

`--at` never leaves a sleeping process: the entry is a JSON record and nothing else. A
launcher runs `proc launch-due` every minute (a systemd user timer, or a crontab line when
user systemd is unreachable, as in some WSL setups); each due entry is started exactly once
(the registry lock serializes overlapping passes) and from then on is an ordinary job. The
job starts with the environment of the call that scheduled it, stored in a private `0600`
file beside the registry. Start times are accurate to about a minute. `proc launcher status`
says whether the launcher works; `start --at` refuses (exit 6) when none can be installed.

A job also records its `owner` (`session` and `project`) when the starting environment sets
`CLAUDE_CODE_SESSION_ID` / `COORD_PROJECT`, so tools that file jobs per agent session read the
starter instead of guessing it; with neither set there is no `owner`.

## Claude Code hook

`proc start ... -- CMD` hides `CMD` from a project's Bash checks (a database-write gate, an
approval gate), which read the call's text. `proc hook install` registers a PreToolUse hook in
the user's Claude settings (`$CLAUDE_CONFIG_DIR`, else `~/.claude`; `--config-dir DIR`,
repeatable, for another seat); `proc start` does it once when the settings file exists and has
no entry, unless `proc hook uninstall` was run. For a call containing `proc start` the hook
removes each `proc start [options] --` prefix, runs the current project's own PreToolUse Bash
hooks (from `.claude/settings.json` and `settings.local.json`) on the rest and returns the
strictest verdict with their messages; a hook that crashes or times out refuses. Any other
call passes untouched. The project needs no knowledge of `proc`.

## Safety model

The guarantees that make `proc` safe to grant a blanket allow-list entry:

- **Registry-only signaling.** `kill`/`rm`/`clean` act only on jobs recorded in
  `$PROC_HOME`. There is no `--pid` flag; a raw PID can never be passed in.
- **PID-reuse guard.** Before signaling, the stored `pid_starttime` (field 22 of
  `/proc/<pid>/stat`) is compared with the live process's current starttime. On
  mismatch — or when the starttime was never captured, or the process is a
  zombie — the job is reconciled to terminal and **nothing is signaled**, so a
  recycled PID belonging to an unrelated process is never killed.
- **Session-scoped termination.** Jobs run as a session leader
  (`start_new_session`); `kill` signals every member of the session, repeatedly
  during the drain, so `timeout`'s nested process group and any late-forked
  descendant are all reached (a one-shot `killpg` misses them).
- **Each member re-checked before its signal.** `kill` scans `/proc` for the session's members,
  then pins each with a pidfd and re-reads its starttime and session just before signaling, so
  a pid that exited and was reused after the scan is never hit.
- **Concurrency-safe.** Registry writes are serialized with an `fcntl.flock`;
  parallel `proc` invocations can't corrupt the JSON.
- **No hangs.** `start` returns immediately; `logs --follow` and `wait` have hard
  caps. Designed for an unattended agent.

## What `proc` does not do

Small on purpose (see [VISION.md](VISION.md)):

- **It does not keep a process alive across a reboot or a WSL shutdown.** The registry is on
  disk, so the job's record and log are still there; a process that was running is gone, and
  `status` says `exited` with the exit code it left, if any.
- **It does not limit resources.** `--heavy` is a lock between jobs that opt in, `--timeout` a
  wall-clock cap. There is no memory, CPU or process-count limit; jobs started by the launcher
  run in their own systemd scope only to keep systemd's accounting tidy.
- **It does not model work.** No dependencies between jobs, no retry policy: a job retries by
  scheduling its own next run (`proc reschedule`). Workflow, attempts and results belong to the
  layer above, which uses `proc` as its execution backend.
- **It does not reach a process that left the session.** A descendant that calls `setsid()` is
  outside the job's session and `kill` does not signal it.
- **Scheduled starts are accurate to about a minute**, since the launcher runs once a minute.
- **It is Linux-only** (`/proc`), single host, single user, and meant for agents rather than
  for a person at a terminal.

## Status values

`scheduled`, `running`, `exited`, `killed`, `timeout`. (`lost` is retired — a vanished
process is now reconciled to `exited` with its real exit code recovered from the
`.rc` sidecar; `lost` is still read from any legacy registry entry.)

## Storage layout

```
$PROC_HOME/                # default ~/.local/state/proc/
  .lock                    # flock target for registry serialization
  .live-index              # running and scheduled jobs only; what `list` and `launch-due` read
  sessions/<session-id>    # names of the jobs one Claude session started (`list --session`)
  <name>.json              # job metadata
  <name>.log               # combined stdout + stderr
  <name>.rc                # exit code and cost the wrapper recorded
  <name>.rss               # peak RSS, transient: folded into .rc when the job ends
  <name>.schedenv          # 0600: environment a scheduled run starts with (until it starts)
  history/.swept           # UTC day of the last daily sweep
  history/<YYYY-MM-DD>/    # finished jobs (record, log, .rc), by UTC day of their end;
                           #   whole folders older than 14 days are deleted
  cost/<YYYY-MM-DD>.jsonl  # one line per `launch-due` pass: its own CPU, I/O and lock time
  history_days, cost_alert_cmd   # optional settings; for the default home they are read from
                           #   ~/.config/proc/, for another PROC_HOME from here
```

A finished job stays at its flat path for at least 24 hours (so `status` and its log are where
they were right after `proc wait`), then the first `proc` call of a UTC day moves it into
`history/`. `status`, `logs`, `wait`, `rm` and `list --all` find it in either place.

All timestamps are UTC.

## Development

```bash
cd ~/Projects/proc-lib
uv sync                    # .venv with the dev group (pytest, ruff), pinned by uv.lock
uv run pytest -q           # full suite, runs in seconds
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

Tests isolate the registry to a tmp dir (`PROC_HOME` / monkeypatched `BASE`) and
drive real short-lived processes, so they never touch a live `~/.local/state/proc/`
job.
