# core — spec

> Permanent contract and source of truth. Edit THIS file directly on any change.
> Every source `.py` implementing it carries `# spec: <path-to-this-file>`.

## Intent
The engine behind `proc`: a registry of detached background jobs plus the process/signal
logic to launch, list, inspect, tail, wait on, kill, and garbage-collect them. Exists so an
autonomous agent (or the operator) can start a long-running command, walk away, and later
recover its exit status and full log — without either process staying resident as a parent,
and without ever risking a signal reaching a PID that isn't actually the job it thinks it is.

## Rationale / Key decisions
- **JSON-per-job registry, not a database.** One `<name>.json` file under `$PROC_HOME` per
  job, guarded by a single `flock` on `.lock` for the whole registry. Zero runtime
  dependencies (stdlib-only, per `CLAUDE.md`'s hard invariant) and trivially inspectable by
  hand or by `cat`.
- **A finished job keeps its flat path for a day, then the daily sweep moves it to a history
  folder.** 99.6 % of a long-lived registry was finished jobs (70,837 files, +2,100 to 11,100
  a day), all in one flat directory, so every listing paid for the whole history. But callers
  read a job right after `proc wait` (`status`, `tail <job>.log`), so `_save` writes a finished
  job's record flat like any other and its log, `.rc` and `.rss` stay beside it. The first proc
  call of a UTC day (`housekeeping`, called by `cli.main` except for the Claude hook, which
  stays free of registry reads) moves every flat finished job whose end day is earlier than
  yesterday's date (it ended before the previous UTC day began) into `history/<UTC day of
  end_ts>/` through `_archive`: log, `.rc` and `.rss` first, the record written there with
  `log_path` and `rc_path` repointed, the flat record removed last (a crash between the last two
  leaves a flat terminal copy that wins on read and is moved by the next sweep). Each record is
  re-read under the lock and moved only if still finished and still that old; running and
  scheduled jobs never move. So a finished job keeps its path for at least 24 h and the flat
  folder holds about two days of finished jobs. `_load`, so `status`, `logs`, `wait`, `rm`,
  `clean` and `list --all`, finds a job in the flat folder first and then in the history folders
  (at most the retention bound of stat calls). A finished name is still refused by `start`
  until `rm`. A job that asked for a next run is re-armed as `scheduled` and never moved.
- **Retention deletes whole day folders, once a day, with no timer.** The same first call reads
  `history/.swept`; when it is not today's date it writes it first (creating `history/` when
  absent), then removes every day folder older than the bound with one `rmtree` each, never a
  single file, then moves the flat jobs above. The bound is 14 days; it is read only on that
  first call from `PROC_HISTORY_DAYS`, else `history_days` in `~/.config/proc` (default
  registry), or `<home>/history_days` for any other `$PROC_HOME`, like the cost alert's
  command. A day folder is deleted when its date is earlier than today minus the bound, so
  nothing is deleted before it is 14 days old. A registry whose folder does not exist is not
  touched.
- **`list --exited --since H` reads the live folder and only the day folders its window
  covers.** The live folder (jobs not yet swept) and the folders from the UTC day of `now - H`
  to today are scanned; a record file last written before the window is skipped unopened (a record is written at, or after, the end), the rest are opened and
  kept when `end_ts` is inside the window. Running jobs whose process has gone are finalised
  first so a job that just ended is not missed.
- **`proc archive` is the one-time move of a flat registry**, by the same `_archive`: every
  finished record still in the live folder, whatever its age, moves with its files, those past
  the bound are deleted,
  live and scheduled ones stay; a single pass, the lock held per record, rerunnable.
- **A live index finds the running and scheduled jobs without opening a finished record.**
  99.6 % of a long-lived registry is finished jobs, and a pass that read every record to
  find the few live ones cost seconds of CPU and tens of MB of reads every minute. `.live-index`
  (deliberately not `*.json`, so a reader globbing records never takes it for one) maps each
  live job to the fields the routine paths need: status, `scheduled_ts`, `pid` and starttime
  (liveness without opening the record), whether it asked for a next run, and its owner and
  start time (everything `list --json` shows, so a live listing opens no record).
  `_save` keeps it in step with the record under the same lock: a job that goes live or
  changes while live is indexed BEFORE its record is written, a job that stops being live
  leaves the index AFTER, so a crash between the two writes leaves at worst a finished job
  still listed (the next reconcile removes it), never a live job the index does not know. The
  index is replaced atomically, so it is read without the lock. A missing, unreadable or
  other-version index is rebuilt by one full scan under the lock (`proc reindex` does the same
  on demand). `sessions/<id>` is an append-only list of the job names a session started, any
  state, written at start; it answers `list --session` without a scan.
- **Single-record reads take no lock.** Records are replaced atomically (`os.replace`), so
  `wait`, `status` and `logs --follow` read one record lock-free; only a record still marked
  `running` whose process has gone is finalised, under the lock. A 0.5 s `wait` poll of a
  running job therefore never queues behind a launcher pass.
- **`launch-due` takes the lock only around a state flip.** It reads the index, finalises the
  running entries whose process is gone (this is what takes a finished job out of the index
  when nobody asks about it, and re-arms a job that asked for a next run), then flips each due
  entry under its own short lock section that re-reads the record and acts only while it is
  still scheduled and due. Two overlapping passes still start an entry exactly once. A pass
  with nothing to do reads under 1 MB (interpreter included) and takes no lock; the pass
  writes its cost line (`cost.spec.md`). The timer's `proc launch-due` skips argparse
  (`cli.main` fast path) and the engine imports `subprocess`, `shutil`, `launcher`,
  `claude_hook`, `hook_judge` and `heavy_job_lock` only where used, because bytecode read at start is most
  of such a pass's reads.
- **A launched job runs in its own systemd scope.** When the launcher's unit starts a job
  (`INVOCATION_ID` set, `systemd-run` present) the wrapper is exec'd by `systemd-run --user
  --scope`, which keeps pid, starttime and session unchanged, so nothing of the job stays in
  the unit's cgroup (systemd logged each as a left-over process on every unit end). Outside a
  unit (an immediate `start`, cron) nothing changes.
- **The wrapper records the job's cost in `.rc`.** After the job is reaped, the bash wrapper
  writes the exit code (line 1), `times` (CPU of its reaped children, i.e. the job) and its own
  `/proc/<pid>/io`, into which the kernel has folded the reaped job's counters. All builtins:
  no extra process. `_reconcile` stores them as the record's `cost`; `status` shows them.
- **The peak RSS comes from GNU time, the job's reaping parent.** Only the parent that reaps a
  child can read its maximum resident size (`wait4` rusage), and bash cannot. When
  `/usr/bin/time` is GNU time (probed once per process; 27 KB, about 1 MB resident per job,
  no interpreter) the wrapper runs the job under `time -f 'maxrss %M' -o <name>.rss` (outermost,
  so `timeout` and every descendant are covered), reads the figure with builtins, writes
  `maxrss_kb <n>` into `.rc` and removes the side file; the figure never touches the job's
  log, and the exit code is the job's own (128+n for a signal, as bash alone reports it).
  With no GNU time, or when `time` was killed before it reported, the wrapper writes
  `maxrss_note <why>` instead and `status` prints "peak RSS not recorded (<why>)"; a record
  from before this field says so too. Rejected: a Python wrapper (about 10 MB resident per
  job) and the systemd scope's `memory.peak` (a scope exists only for jobs the launcher
  starts, not for `proc start` from a shell or a chat).
- **PID-reuse guard via `/proc/<pid>/stat` starttime, not PID alone.** A bare `os.kill(pid, 0)`
  cannot distinguish "our job is still alive" from "the PID was recycled by an unrelated
  process." Every launch captures the kernel starttime (`_capture_starttime`) and every
  liveness check (`_alive`) demands it match — a mismatch, or a starttime we never captured,
  is treated as not-ours (fail closed, never signal a process we didn't launch).
- **Session-based signaling, not process-group.** The job is launched with
  `start_new_session=True` so the bash wrapper becomes the session leader. `kill`/escalation
  signal every live member of that *session* (`_session_members` / `_signal_session`), not
  `killpg` on the recorded pgid: coreutils `timeout` forks the real command into its own
  process group, so a pgid-only signal would never reach it. Session id is inherited by every
  descendant, survives reparenting to init, and is not touched by `timeout` — the one property
  a signal-delivery mechanism needs here.
- **Each session member is re-verified right before its signal.** The scan of `/proc`
  (`_session_scan`) records every member's starttime; `_signal_member` pins the process with a
  pidfd (`os.pidfd_open`), re-reads `/proc/<pid>/stat`, and signals through the pidfd
  (`signal.pidfd_send_signal`) only when starttime and session still match and the process is
  not dead, so a pid freed and reused between the scan and the signal is never hit. Without
  pidfd support (an old kernel) the same check runs and a plain `os.kill` follows, leaving a
  window of microseconds rather than the scan's duration. Known limit, by design: a
  descendant that calls `setsid()` leaves the session and is no longer reached by `kill`.
- **`.rc` sidecar for the real exit code.** `proc` is never the parent of the detached job
  across separate CLI invocations, so it cannot `waitpid` for an exit code. The launched
  command is wrapped in a small bash script that captures `$?` and writes it to a `.rc` file
  before exiting; `_reconcile` reads that file once liveness checks show the job is gone.
- **Hard caps on every blocking path.** `logs --follow` auto-stops after
  `FOLLOW_HARD_CAP_SECONDS` (600s); `wait` defaults to a 3600s deadline. Reflects the
  `CLAUDE.md` invariant "never hang" — a supervisor that can itself hang defeats its purpose.
- **Re-signal-until-drained on kill, not signal-once.** `cmd_kill` loops re-sending
  SIGTERM (grace period) then SIGKILL to every live session member, because a process tree
  still forking children when the first signal lands could otherwise let a late-forked
  descendant escape a one-shot kill.
- **`killed` is written only when the session is empty.** After the SIGKILL drain `cmd_kill`
  rescans the session under the lock; if a member is still alive (a process in
  uninterruptible sleep, say) it does not write `killed`: the status stays `running`, the
  record gets `kill_survivors` (the pids), stderr names them and the exit code is
  `EXIT_KILL_SURVIVORS` (8). A job recorded as killed with live children would hold locks and
  memory nobody accounts for. `rm --force` stops on that code and keeps the record.
- **Atomic writes via temp-file + `os.replace`.** `_save` never leaves a half-written
  `<name>.json` visible to a concurrent reader. `_read_rc` reads the first line only, so the
  cost lines after the exit code never change what an older reader sees.
- **Verdict line first in human output.** `status` and `wait` print one summary line
  (`_verdict`) before any detail, because the primary caller is an agent that truncates
  output (`| head -5`) and must not lose the job's state to the truncation. Machine callers
  get `--json`, whose leading keys are `name`, `status`, `alive`, `exit_code`.
- **`wait` exit code is a distinct `EXIT_JOB_FAILED` (5), not the job's own rc.** Passing
  the job's rc through would collide with `EXIT_USAGE`/`EXIT_WAIT_TIMEOUT`/`EXIT_NOTFOUND`
  (a job exiting 3 would read as "still running"); one fixed failure code keeps every
  outcome unambiguous while still letting `proc wait a && next` stop on failure. The exact
  rc is on the verdict line and in `status`.
- **`--env` values masked at write time, not at display time.** Values of keys containing
  `TOKEN`/`PASSWORD`/`SECRET`/`KEY` (case-insensitive) are replaced with `***` before the
  registry entry is written; the job receives the real value through `Popen(env=...)`. The
  registry is a plain JSON directory, and nothing reads the plaintext back, so it never
  reaches disk.

- **A scheduled job is a registry record, not a sleeping process.** `start --at` writes an
  entry in status `scheduled` and starts nothing. `launch-due`, run each minute by
  `proc_lib.launcher`, starts every due entry. Each flip holds the registry flock, re-reads the
  record and flips it to `running` before releasing, so two overlapping passes start an entry
  exactly once. After the start it is an ordinary job (`_spawn` is the one launch path, shared
  with an immediate `start`).
- **A scheduled run keeps the scheduling shell's full environment in a 0600 sidecar**
  (`<name>.schedenv`), not in the registry: a launcher started by systemd or cron has none of
  the caller's `PATH` or variables, and the registry masks secret values. The sidecar is
  deleted when the run starts or is cancelled.
- **A job records who started it (`owner`).** Anything that lists jobs per agent session needs
  the starter as a fact, not a guess from log or transcript text. `start` copies the session and
  project from the caller's environment; a scheduled job keeps the owner of the call that
  scheduled it (the launcher's own environment names nobody); absent variables mean no `owner`.
- **A running job sets its own next run (`next_run`).** `start --at` / `reschedule` from inside
  the job (recognised by `PROC_JOB_NAME`, set in every job's environment) records `next_run`
  on the running entry; `_reconcile` turns the entry into `scheduled` when the run ends.
  `kill` drops it. The log is kept across runs.
- **No retry of a failed scheduled start.** A start that cannot spawn (missing cwd, no
  `timeout` binary) becomes `exited` with `launch_error` and a line in the log; silently
  re-trying every minute would hide it. A `--heavy` entry whose lock is busy stays scheduled
  and is retried by the next pass.

## Rejected alternatives
- **`sleep` inside the job until the wanted time.** Rejected: the job holds a process and
  shows as live for hours or days; that is the defect scheduled start removes.
- **SQLite registry.** Rejected: adds a runtime dependency (or at minimum ships stdlib
  `sqlite3`'s locking semantics) for a workload (dozens of jobs, single host) where a
  flock-guarded JSON directory is simpler to reason about and to debug by hand.
- **`killpg` on the recorded process-group id.** Rejected: `timeout` puts the real worker
  into its own process group via `setpgid`, so a `killpg` on the wrapper's original pgid
  cannot reach it. Session-scoped signaling was adopted instead (see Rationale).
- **PPID-walk to find descendants.** Rejected: when the bash wrapper dies, its children are
  reparented to init and the parent-child tree breaks, making a PPID walk unreliable exactly
  when it matters (mid-kill).
- **`proc` staying resident as the job's reaper.** Rejected: `start` must return immediately
  (never-hang invariant); a resident reaper would mean either blocking `start` or running a
  second long-lived process, both of which the supervisor is explicitly meant to avoid needing.

## Acceptance criteria
- GIVEN a registry of any size WHEN `launch-due` runs with nothing due THEN it opens no job
  record, takes no lock, reads under 1 MB and uses under 0.3 CPU s.
- GIVEN a job started via `cmd_start` WHEN it exits (any cause) THEN a subsequent `cmd_status`
  or `cmd_list` reconciles its status to a TERMINAL value with the real exit code recovered
  from the `.rc` sidecar, without requiring `proc` to have stayed running in between.
- GIVEN a job's original PID has been reused by an unrelated process WHEN any liveness check
  runs THEN the job is reported as not-alive (never signaled, never reported RUNNING).
- GIVEN `cmd_kill` is called on a running job WHEN the job's process tree includes a
  `timeout`-wrapped worker in a different process group THEN every live member of the job's
  session receives the signal, not just the wrapper.
- GIVEN `logs --follow` is called WHEN the job never reaches a terminal status THEN the call
  returns (exit code `EXIT_WAIT_TIMEOUT`) after `FOLLOW_HARD_CAP_SECONDS`, never blocking
  forever.
- GIVEN two `proc` invocations race on the same registry WHEN both write THEN the flock
  serializes them and no `<name>.json` is left truncated or corrupted.

## State
Persistent state lives under `$PROC_HOME` (default `~/.local/state/proc`), resolved once at import
via `BASE` (overridable for tests by monkeypatching `core.BASE`):
- `<name>.json` — one job's metadata (see I/O contracts); once the daily sweep has moved a
  finished job, its record, log and sidecars below are in `history/<YYYY-MM-DD>/` (UTC day of
  its end) instead.
- `history/.swept` — the UTC day of the last retention sweep.
- `<name>.log` — the job's combined stdout+stderr.
- `<name>.rc` — sidecar the bash wrapper writes: the job's real exit code on line 1, then its
  cost (`times` lines, `io <key>: <value>` lines, then `maxrss_kb <n>` or `maxrss_note <why>`).
- `<name>.rss` — GNU time's side file for the peak RSS; the wrapper deletes it once folded into
  `.rc` (`rm`/`gc` also remove a leftover one).
- `.live-index` — JSON `{"version": 2, "jobs": {name: entry}}` of the running and scheduled
  jobs (see Rationale); written only under the lock, read lock-free.
- `sessions/<id>` — job names (one per line) a Claude session started, any state.
- `cost/<date>.jsonl`, `cost-alert/<date>/` — see `cost.spec.md`.
- `.lock` — registry-wide flock file, created on demand, never deleted.
- `heavy_job/<name>.lock` — owned by `proc_lib.heavy_job_lock`, referenced (not written) here
  for cleanup in `_rm_one`.

## Methods
- `cmd_start(args) -> int` — launch `args.cmd` detached under `args.name`; refuses a duplicate
  running name; wraps in `timeout` if `--timeout` given; merges `args.env` (`KEY=VAL` list,
  malformed pair -> `EXIT_USAGE`, nothing launched) over the caller's environment; writes the
  registry entry; returns 0 or an `EXIT_*` code.
- `cmd_list(args) -> int` — print the live jobs (running first, then scheduled by start time)
  read from the index. Cost by form:
  - no flag, `--running`, `--scheduled`, each with or without `--session ID` (then also
    filtered by the entry's owner session): the index alone. `--json` without `--full` opens no
    record (the index entry carries every field it shows); the table and `--json --full` also
    show command, timeout and paths, so they open the records of the listed live jobs only.
  - `--session ID` with neither `--running` nor `--scheduled`: the jobs that session ever
    started, in any state, from its session list; opens one record per job it started (a
    session with many finished jobs reads that many records).
  - `--all`: every record, live folder and history folders (a live copy wins), the only form
    that scans the registry.
  - `--exited --since H` (alone; with `--all`, `--running`, `--scheduled` or `--session` it is
    exit 2, as is `--since` without `--exited` or `H <= 0`): the jobs that ended in the last
    `H` hours, from the live folder and the history day folders the window covers.
  A running entry whose process has gone is finalised first (its record is read and saved under
  one lock section, none when nothing is stale). `--json` prints a compact array of
  `_list_json` objects (`name`, `status`, `alive`, `exit_code`, `pid`, `pid_starttime`,
  `owner`, `start_ts`, `elapsed_s`, plus `scheduled_ts` for a scheduled job); `--full` prints
  the whole `_status_json` record of each instead.
- `cmd_reindex(args) -> int` — rebuild the index and the session lists from every record.
- `cmd_status(args) -> int` — read (lock-free) and print one job: the verdict line, then
  `log`/`cmd`/`cwd`/timestamps/`timeout`/`env`/`status`+`exit_code` detail lines;
  `args.json` prints `_status_json` instead.
- `cmd_logs(args) -> int` — print or `--follow` a job's log; follow auto-stops on terminal
  status or the hard cap. `args.since` (`--since TS`) keeps only the lines after `TS`, applied
  before `--tail` and to the initial dump of `--follow` (bytes that arrive later are streamed
  whole). `TS` is ISO 8601 with a zone (`2026-10-31T12:00:00Z`, `...+03:00`); a naive or
  unparsable value is refused with `EXIT_USAGE`, nothing printed (same rule as `--at`). The
  log has no time of its own: `proc` writes the job's raw output, so `--since` decides from
  what the lines carry. A line that begins with an ISO 8601 time with a zone is timed by it;
  a line without one takes the time of the nearest timed line above it; a line above the first
  timed line is untimed and dropped. A line is kept when its time is strictly after `TS`. If no
  line is timed, the only time known is the log file's last-modified time: the whole log is
  printed when it is after `TS`, nothing otherwise.
- `cmd_wait(args) -> int` — block (polling without the registry lock, not a blocking syscall)
  until the job reaches a terminal status or `args.timeout`/3600s elapses; prints the verdict line; returns 0 only
  for `exited` with rc 0, `EXIT_JOB_FAILED` for any other terminal end.
- `_verdict(meta) -> str` — the one-line state summary: `NAME: running pid=N for 2h13m`,
  `NAME: exited rc=0 after 41m5s` (`rc=?` when unknown), `NAME: killed (timeout) after …`,
  `NAME: killed (proc kill) after …`.
- `_status_json(meta) -> dict` — `name`, `status`, `alive`, `exit_code`, `pid`, `elapsed_s`,
  `log_path` first, then every other stored field unchanged.
- `cmd_kill(args) -> int` — SIGTERM-then-grace-then-SIGKILL (or immediate SIGKILL with
  `--force`) every live member of the job's session; finalizes status to `KILLED` only once the session is
  empty, else exits `EXIT_KILL_SURVIVORS` naming the surviving pids (status unchanged).
- `cmd_rm(args) -> int` — remove one or more terminal jobs' metadata/log/rc (and kill first
  under `--force` if still running).
- `cmd_clean(args) -> int` — remove all terminal jobs older than `--older-than` hours
  (default 24). The scan is lock-free; each removal re-reads its record under the lock. A
  record still marked running is left to the reconcile.
- `_alive(pid, starttime) -> bool` — the PID-reuse-guarded liveness predicate every other
  method relies on.
- `_reconcile(meta) -> dict` — bring one job's recorded status in line with reality; the single
  code path that finalizes a no-longer-alive RUNNING job from its `.rc` sidecar.

## Invariants
- Never send a signal to a PID this process did not itself launch and whose `/proc` starttime
  it did not capture and verify (`_alive` fails closed when `starttime is None`).
- `cmd_start` returns to the caller immediately; nothing in the start path blocks on the
  launched job's completion.
- Every blocking loop (`wait`, `logs --follow`) has a hard, finite ceiling — no code path
  polls forever.
- No import outside the Python standard library at runtime (`heavy_job_lock` is the one
  in-package dependency; see Dependencies).
- The registry lockfile is created on demand and never unlinked, avoiding an unlink/recreate
  race between concurrent `proc` invocations.

## Formulas
n/a

## I/O contracts
Job metadata (`<name>.json`), the schema every method reads/writes:
```
{
  "name": str,
  "cmd": list[str],
  "cwd": str,
  "pid": int,
  "pgid": int,
  "start_ts": float,       # epoch seconds
  "start_iso": str,        # UTC ISO8601
  "pid_starttime": int | null,   # /proc starttime at capture, PID-reuse guard
  "timeout": int | null,   # seconds, if --timeout was given
  "env": dict[str, str],   # --env overrides, secret values masked "***"; ABSENT on
                           # entries written before this field existed (read as {})
  "status": "scheduled" | "running" | "exited" | "killed" | "timeout" | "lost",
  "exit_code": int | null,
  "end_ts": float | null,
  "log_path": str,
  "rc_path": str,
  "heavy": true,           # only on --heavy jobs
  "scheduled_ts": float,   # only while/after `scheduled`: epoch start time
  "scheduled_iso": str,
  "scheduled_for": str,    # on a run the launcher started: the time it was due
  "launch_error": str,     # on a scheduled start that failed to spawn
  "next_run": {"at_ts": float, "spec": {...}},  # running job asked to run again
  "cost": {"cpu_user_s": float, "cpu_sys_s": float, "rchar": int, "wchar": int, "syscr": int,
           "syscw": int, "read_bytes": int, "write_bytes": int,
           "maxrss_kb": int, "maxrss_note": str},  # of a finished job, from its .rc; each key
                           # only if the wrapper recorded it; maxrss_kb is the peak RSS, and
                           # maxrss_note says why it is absent
  "owner": {"session": str, "project": str}  # from CLAUDE_CODE_SESSION_ID / COORD_PROJECT
                           # at `start` (scheduling time for --at); each key only if that
                           # variable is set and non-empty; ABSENT if neither is. Never guessed.
}
```
A `scheduled` entry has `pid`, `pgid`, `pid_starttime`, `start_ts`, `start_iso`, `exit_code`
and `end_ts` all `null`, and carries `cmd`, `cwd`, `timeout`, `env` (masked), `heavy`,
`force_concurrent`. A `killed` entry with `start_ts` null was cancelled before it started.
Exit codes returned by every `cmd_*` function: `0` success/terminal reached, `2`
(`EXIT_USAGE`) usage error or conflict, `3` (`EXIT_WAIT_TIMEOUT`) wait/follow timed out while
still running, `4` (`EXIT_NOTFOUND`) named job not found, `5` (`EXIT_JOB_FAILED`) `wait`
reached a terminal status other than `exited` with rc 0, `6` (`EXIT_LAUNCHER`) no launcher
backend can fire scheduled jobs, `7` (`EXIT_HOOK_MISSING`) the Claude Code hook is not registered.

Backward compatibility: every reader tolerates a missing optional field, so an entry written
by an older version (no `env`) is listed, reconciled, waited on and killed unchanged.

## Names
| canonical | kind | forbidden-aliases |
|-----------|------|-------------------|
| SCHEDULED | const | |
| RUNNING | const | |
| EXITED | const | |
| KILLED | const | |
| TIMEOUT | const | |
| LOST | const | |
| EXIT_USAGE | const | |
| EXIT_WAIT_TIMEOUT | const | |
| EXIT_NOTFOUND | const | |
| EXIT_JOB_FAILED | const | |
| EXIT_LAUNCHER | const | |
| ENV_JOB_NAME | const | |
| next_run | field | |
| owner | field | |
| pid_starttime | field | |
| env | field | |
| alive | field | |

## Dependencies
- `proc_lib.launcher` (`specs/proc_lib/launcher.spec.md`) — install/check of the per-minute
  launcher, called before any scheduling.
- `proc_lib.claude_hook` (`specs/proc_lib/claude_hook.spec.md`) — the Claude Code hook itself
  (`main`), run by `cmd_hook unwrap`; `proc_lib.hook_judge` (same spec) — its settings editing
  (`install`, `uninstall`, `status`, `ensure`, `settings_path`, `default_config_dirs`), called
  by `cmd_hook` and `cmd_start` through its exported names.
- `proc_lib.heavy_job_lock` (`specs/proc_lib/heavy_job_lock.spec.md`) — optional RAM-exclusivity
  check/claim invoked from `cmd_start` when `args.heavy` is set.
- `proc_lib.cost` (`specs/proc_lib/cost.spec.md`) — the cost line and alert of a launch pass.
- The engine is split into `proc_lib.procfs` (`specs/proc_lib/procfs.spec.md`),
  `proc_lib.registry` (`specs/proc_lib/registry.spec.md`), `proc_lib.render`
  (`specs/proc_lib/render.spec.md`), `proc_lib.spawn` (`specs/proc_lib/spawn.spec.md`) and
  `proc_lib.commands` (`specs/proc_lib/commands.spec.md`), joined by `proc_lib.corelink`
  (`specs/proc_lib/corelink.spec.md`); `proc_lib.core` re-exports their names, so this contract
  holds for the whole and every name it states is reachable as `core.<name>`.

- `cmd_start` with `args.at` — parse an ISO 8601 time that carries a zone (naive or malformed
  -> `EXIT_USAGE`), ensure the launcher works (`EXIT_LAUNCHER` otherwise), write a `scheduled`
  entry plus the environment sidecar, spawn nothing. Called from inside a running job of the
  same name it records that job's `next_run` instead; a name that is already scheduled is
  refused.
- `cmd_reschedule(args) -> int` — move a `scheduled` entry's time, or (from inside the running
  job only) set its `next_run` reusing its command, cwd, timeout and environment.
- `cmd_launch_due(args) -> int` — finalise running entries whose process is gone, start every
  due `scheduled` entry once, oldest first (index-driven, lock only around each flip), then
  write the pass's cost line.
- `cmd_hook(args) -> int` — `status` / `install` / `uninstall` of the Claude Code hook in each
  `--config-dir` (default `$CLAUDE_CONFIG_DIR`, else `~/.claude`), `unwrap` runs the hook
  (`proc_lib.claude_hook`, `specs/proc_lib/claude_hook.spec.md`); `status`/`install` exit
  `EXIT_HOOK_MISSING` (7) when an entry is missing. `cmd_start` calls `hook_judge.ensure`
  before launching and prints its one-line note to stderr; it never fails a start.
- `cmd_launcher(args) -> int` — `status` / `install` / `uninstall` of the launcher backend;
  `status` exits `EXIT_LAUNCHER` when it cannot fire jobs.
- `cmd_kill` on a `scheduled` entry cancels it (status `killed`, nothing signaled); on a
  running job it also drops any `next_run`. `cmd_wait` on a `scheduled` job keeps waiting
  through the start to the end.

## Test cases
- GIVEN a job that exits before `proc` ever checks it again WHEN `cmd_status` runs THEN status
  reconciles to `exited` with the exit code recovered from `.rc` (`test_reconcile.py`).
- GIVEN a job's PID has been reused by a different live process WHEN `cmd_status`/`cmd_kill`
  runs THEN the job is treated as not-alive, not as the new process (`test_safety.py`).
- GIVEN a running job wrapped in `timeout` WHEN `cmd_kill` is called without `--force` THEN
  every session member (wrapper, `timeout`, worker) is signaled and the job finalizes to
  `killed` (`test_kill.py`).
- GIVEN a session member survives SIGTERM and SIGKILL WHEN `cmd_kill` finishes its drain THEN the
  status is not `killed`, the record's `kill_survivors` and stderr name the pids and the exit
  code is `EXIT_KILL_SURVIVORS` (`test_kill.py`).
- GIVEN a session member whose pid now has another starttime than the scan recorded (reuse), or
  that left the session, or is a zombie WHEN `_signal_member` / `_signal_session` run THEN it
  is not signaled and not counted; with matching identity it is signaled, with and without
  pidfd support (`test_safety.py`).
- GIVEN `proc start` immediately followed by `proc kill` (before grandchildren fork) WHEN the
  kill runs THEN no descendant escapes (`test_kill.py`).
- GIVEN a log whose lines carry times WHEN `cmd_logs --since TS` runs with `TS` between two of
  them THEN only the lines after `TS` print, untimed continuation lines following their timed
  line; with `TS` after the last line nothing prints and the exit code is 0; with a naive or
  unparsable `TS` the exit code is `EXIT_USAGE` and nothing prints (`test_logs_since.py`).
- GIVEN a log with no timed line WHEN `cmd_logs --since TS` runs THEN the whole log prints when
  its last-modified time is after `TS`, nothing otherwise (`test_logs_since.py`).
- GIVEN a full start -> list -> logs -> wait -> rm sequence WHEN run against a short-lived
  command THEN every stage reports consistent state (`test_lifecycle.py`).
- GIVEN any job WHEN `cmd_status` runs THEN line 1 is the verdict and line 2 the log path;
  with `--json` the first keys are `name`, `status`, `alive`, `exit_code`
  (`test_agent_surface.py`).
- GIVEN a job that exits non-zero, is killed, or times out WHEN `cmd_wait` runs THEN it
  returns `EXIT_JOB_FAILED`; a clean rc 0 returns 0 (`test_agent_surface.py`).
- GIVEN `--env API_TOKEN=x --env FOO=1` WHEN the job runs THEN it sees both real values while
  the registry and status output show `API_TOKEN=***` (`test_agent_surface.py`).
- GIVEN a running entry in the pre-`env` registry format WHEN `list`/`status`/`wait`/`kill`
  run THEN each works unchanged (`test_agent_surface.py`).

- GIVEN `start --at` WHEN the time has not come THEN no process exists; WHEN a launch pass
  runs at the time THEN the job runs once even under two concurrent passes, and a fresh
  process sees the entry (`test_scheduled.py`).
- GIVEN a running job that calls `start --at` with its own name WHEN the run ends THEN the entry
  is `scheduled` for the new time and the launcher runs the new command (`test_scheduled.py`).

- GIVEN a registry with finished, scheduled and running jobs WHEN `launch-due` runs with
  nothing due THEN no `*.json` record is opened and the lock is not taken; WHEN one entry is
  due THEN only its record is opened, in one lock section (`test_live_index.py`).
- GIVEN a running entry whose pid now belongs to another process WHEN `launch-due` runs THEN it
  is finalised as ended and no signal other than 0 is sent (`test_live_index.py`).
- GIVEN a job going live or leaving live WHEN `_save` runs THEN the index is written before the
  record in the first case and after it in the second (`test_live_index.py`).
- GIVEN a missing, corrupt or other-version index WHEN any live read runs THEN it is rebuilt
  from the records (`test_live_index.py`).
- GIVEN `list` with no flag, `--running`, `--scheduled`, `--session`, `--all` THEN each shows
  the set described under Methods and the default opens no finished record
  (`test_live_index.py`).
- GIVEN `list --json` with `--running` and/or `--scheduled`, with or without `--session` WHEN
  run THEN no record file is opened; with `--full` or the table only the listed live jobs'
  records are (`test_live_index.py`).
- GIVEN `list --json` THEN each object has only the fields named under Methods, no command text
  (`test_live_index.py`).
- GIVEN a running job WHEN `wait` or `status` runs THEN the registry lock is not taken
  (`test_live_index.py`).
- GIVEN a finished job THEN its record carries `cost` from the `.rc` and `status` prints it
  (`test_live_index.py`).
- GIVEN GNU time is installed WHEN a job ends (any exit code, signal or `--timeout`) THEN its
  `.rc` and `cost` carry `maxrss_kb`, the exit code and log are as without the measurement, and
  `status` and `--json` show it (`test_peak_rss.py`).
- GIVEN no GNU time or a `time` that reported nothing WHEN a job ends THEN `cost` has
  `maxrss_note` and `status` prints "peak RSS not recorded (<why>)" (`test_peak_rss.py`).
- GIVEN a unit environment WHEN a scheduled job starts THEN the wrapper is prefixed by
  `systemd-run --user --scope` (`test_live_index.py`).
- GIVEN a job ends (exit, kill, timeout, failed scheduled start) THEN its record, log and
  `.rc` are still at `<home>/<job>.*` after `proc wait` and `history/` is not created
  (`test_history.py`).
- GIVEN the first call of a new UTC day WHEN flat finished jobs exist THEN those that ended
  before the previous UTC day began (a job ended one second earlier moves, one ended at that
  instant stays) are in their day folder with `log_path` repointed, running and scheduled jobs
  stay, and `status`, `logs` and `list --all` find a job before and after the move
  (`test_history.py`).
- GIVEN the first call of a new UTC day WHEN day folders older than the bound exist THEN each
  is deleted whole, a folder exactly at the bound stays, a second call that day does nothing,
  and the bound comes from the registry's own `history_days` (`test_history.py`).
- GIVEN a flat registry WHEN `proc archive` runs THEN finished records and their files are in
  their day folders, records past the bound are deleted, running and scheduled ones stay, and a
  rerun moves nothing (`test_history.py`).
- GIVEN `list --exited --since 12 --json` THEN only jobs that ended in the window are listed
  (flat or moved), no day folder outside the window is scanned, and a job that just died
  unreconciled is included (`test_history.py`).
- GIVEN the Claude hook command WHEN it runs THEN no housekeeping runs (`test_history.py`).
- GIVEN `clean --older-than 168` THEN a 6-day-old job stays, an 8-day-old one goes, and the
  lock is held per removal (`test_live_index.py`).
