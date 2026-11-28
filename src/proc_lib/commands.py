# spec: specs/proc_lib/commands.spec.md
"""proc engine — the `cmd_*` subcommand implementations behind `proc`.

Exit codes are defined in :mod:`proc_lib.registry` and re-exported by :mod:`proc_lib.core`;
the table is in the `core` docstring.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import sys
import time
import types
from datetime import datetime
from pathlib import Path

from proc_lib import cost
from proc_lib.corelink import core
from proc_lib.procfs import (
    _alive,
    _session_alive,
    _session_members,
)
from proc_lib.registry import (
    _LOCK_SECONDS,
    ENV_JOB_NAME,
    EXIT_HOOK_MISSING,
    EXIT_JOB_FAILED,
    EXIT_KILL_SURVIVORS,
    EXIT_LAUNCHER,
    EXIT_NOTFOUND,
    EXIT_USAGE,
    EXIT_WAIT_TIMEOUT,
    EXITED,
    FOLLOW_HARD_CAP_SECONDS,
    KILLED,
    LIVE,
    RUNNING,
    SCHEDULED,
    TERMINAL,
    _all_metas,
    _archive,
    _current,
    _day,
    _ensure_base,
    _history_days,
    _history_record,
    _history_root,
    _index_locked,
    _iso,
    _live_entry,
    _live_index,
    _load,
    _log_path,
    _meta_path,
    _rc_path,
    _read_json,
    _read_rc,
    _read_rc_file,
    _rebuild_index,
    _reconcile,
    _reconcile_and_save,
    _save,
    _schedenv_path,
    _session_add,
    _session_names,
    _write_index,
)
from proc_lib.render import (
    _cmd_str,
    _elapsed,
    _err,
    _iso_short,
    _job_succeeded,
    _list_json,
    _status_json,
    _status_lines,
    _verdict,
)
from proc_lib.spawn import (
    _drop_schedenv,
    _LaunchError,
    _masked_env,
    _owner_from_env,
    _parse_env,
    _read_schedenv,
    _run_spec,
    _set_next_run,
    _spawn,
    _write_schedenv,
)


def _parse_zoned(text: str, flag: str = "--at") -> float:
    """Epoch seconds from an ISO 8601 time that carries its own zone. A naive time
    is refused: the host zone and the caller's zone can differ, and a job that
    starts hours off is worse than a refused command."""
    msg = (
        f"{flag} expects an ISO 8601 time with a zone (2026-10-23T13:19:00Z, ...+03:00), "
        f"got {text!r}"
    )
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(msg) from None
    if when.tzinfo is None:
        raise ValueError(msg)
    return when.timestamp()


def _parse_at(text: str) -> float:
    return _parse_zoned(text, "--at")


# A log line is timed only by a leading ISO 8601 time that carries a zone.
_LINE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})")


def _line_time(line: str) -> float | None:
    mt = _LINE_TIME.match(line)
    if mt is None:
        return None
    try:
        return _parse_zoned(mt.group(0))
    except ValueError:
        return None


def _since_filter(lines: list[str], since: float, mtime: float) -> list[str]:
    """Lines strictly after `since`. A line is timed by its own leading time, else by
    the nearest timed line above it; lines above the first timed line are dropped.
    A log with no timed line is judged whole by the file's mtime."""
    times = [_line_time(ln) for ln in lines]
    if all(t is None for t in times):
        return lines if mtime > since else []
    kept: list[str] = []
    cur: float | None = None
    for ln, t in zip(lines, times, strict=True):
        if t is not None:
            cur = t
        if cur is not None and cur > since:
            kept.append(ln)
    return kept


def _ensure_launcher() -> bool:
    from proc_lib import launcher

    try:
        launcher.ensure(core.BASE)
    except launcher.LauncherError as e:
        _err(f"scheduled start needs a launcher and none works: {e}")
        return False
    return True


def _ensure_claude_hook() -> None:
    from proc_lib import hook_judge

    note = hook_judge.ensure(core.BASE)
    if note:
        print(note, file=sys.stderr)


def _note_session(meta: dict) -> None:
    session = (meta.get("owner") or {}).get("session")
    if session:
        _session_add(session, meta["name"])


def cmd_start(args) -> int:
    name = args.name
    cmd = args.cmd
    if not cmd:
        _err("no command given after '--'")
        return EXIT_USAGE
    # argparse.REMAINDER keeps a leading '--' if present; strip one.
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        _err("no command given after '--'")
        return EXIT_USAGE

    cwd = args.cwd or os.getcwd()
    if not os.path.isdir(cwd):
        _err(f"cwd does not exist: {cwd}")
        return EXIT_USAGE
    _ensure_claude_hook()

    try:
        job_env = _parse_env(getattr(args, "env", None) or [])
    except ValueError as e:
        _err(str(e))
        return EXIT_USAGE

    at = getattr(args, "at", None)
    at_ts = None
    if at is not None:
        try:
            at_ts = _parse_at(at)
        except ValueError as e:
            _err(str(e))
            return EXIT_USAGE
        if not _ensure_launcher():
            return EXIT_LAUNCHER
    heavy = bool(getattr(args, "heavy", False))
    force_concurrent = bool(getattr(args, "force_concurrent", False))
    owner = _owner_from_env()

    def op():
        existing = _load(name)
        if existing is not None:
            existing = _reconcile(existing)
            _save(existing)
            status = existing.get("status")
            if at_ts is not None and status == RUNNING and os.environ.get(ENV_JOB_NAME) == name:
                spec = _run_spec(
                    cmd, cwd, args.timeout, _masked_env(job_env), heavy, force_concurrent, at_ts
                )
                _set_next_run(existing, spec, dict(os.environ) | job_env)
                _save(existing)
                print(f"'{name}' will run again at {_iso_short(at_ts)} once this run ends")
                return 0
            if status == RUNNING:
                _err(
                    f"job '{name}' is already running (pid {existing.get('pid')}); "
                    f"kill it or choose another name"
                )
                return EXIT_USAGE
            if status == SCHEDULED:
                _err(
                    f"job '{name}' is already scheduled for "
                    f"{_iso_short(existing['scheduled_ts'])}; `proc reschedule {name} --at ...` "
                    f"moves it, `proc kill {name}` cancels it"
                )
                return EXIT_USAGE
            _err(
                f"job '{name}' already exists with status "
                f"'{status}'; run `proc rm {name}` first "
                f"(won't overwrite its log)"
            )
            return EXIT_USAGE

        if at_ts is not None:
            logp = _log_path(name)
            meta = _run_spec(
                cmd, cwd, args.timeout, _masked_env(job_env), heavy, force_concurrent, at_ts
            ) | {
                "name": name,
                "pid": None,
                "pgid": None,
                "start_ts": None,
                "start_iso": None,
                "pid_starttime": None,
                "status": SCHEDULED,
                "exit_code": None,
                "end_ts": None,
                "log_path": str(logp),
                "rc_path": str(_rc_path(name)),
            }
            if owner:
                meta["owner"] = owner
            _write_schedenv(name, dict(os.environ) | job_env)
            _save(meta)
            _note_session(meta)
            print(f"scheduled '{name}' for {_iso_short(at_ts)}")
            print(f"log: {logp}")
            return 0

        if heavy:
            from proc_lib import heavy_job_lock

            try:
                heavy_job_lock.check_free(name, force_concurrent)
            except heavy_job_lock.HeavyJobBusy as e:
                _err(str(e))
                return EXIT_USAGE

        try:
            meta = _spawn(
                name,
                cmd,
                cwd,
                args.timeout,
                heavy,
                os.environ | job_env,
                _masked_env(job_env),
            )
        except _LaunchError as e:
            _err(str(e))
            return EXIT_USAGE
        if owner:
            meta["owner"] = owner
        _save(meta)
        _note_session(meta)
        print(f"started '{name}' pid={meta['pid']} pgid={meta['pgid']}")
        print(f"log: {meta['log_path']}")
        return 0

    return core._with_lock(op)


def cmd_reschedule(args) -> int:
    """Move a scheduled job's start, or let a running job set its own next run
    (same command, cwd, timeout and environment) from inside itself."""
    try:
        at_ts = _parse_at(args.at)
    except ValueError as e:
        _err(str(e))
        return EXIT_USAGE
    if not _ensure_launcher():
        return EXIT_LAUNCHER
    name = args.name

    def op():
        m = _load(name)
        if m is None:
            _err(f"no such job: {name}")
            return EXIT_NOTFOUND
        _reconcile(m)
        status = m.get("status")
        if status == SCHEDULED:
            m["scheduled_ts"] = at_ts
            m["scheduled_iso"] = _iso(at_ts)
        elif status == RUNNING:
            if os.environ.get(ENV_JOB_NAME) != name:
                _err(f"job '{name}' is running; only the job itself may set its next run")
                return EXIT_USAGE
            spec = _run_spec(
                m["cmd"],
                m["cwd"],
                m.get("timeout"),
                m.get("env") or {},
                bool(m.get("heavy")),
                False,
                at_ts,
            )
            _set_next_run(m, spec, dict(os.environ))
        else:
            _err(f"job '{name}' is {status}; `proc rm {name}` and start it again with --at")
            return EXIT_USAGE
        _save(m)
        print(f"'{name}' will run at {_iso_short(at_ts)}")
        return 0

    return core._with_lock(op)


def _launch_one(name: str, now: float) -> str | None:
    """Flip one scheduled record to running (start its process), under the lock the
    caller holds. Returns "launched" or "failed" when it acted, None when the entry is
    no longer due (another pass started it, it was cancelled or moved)."""
    m = _load(name)
    if m is None:
        core._index_set(name, None)  # the record is gone: the index entry is stale
        return None
    if m.get("status") != SCHEDULED or m["scheduled_ts"] > now:
        core._index_set(name, _live_entry(m))  # the index entry is stale: bring it in line
        return None
    if m.get("heavy"):
        from proc_lib import heavy_job_lock

        try:
            heavy_job_lock.check_free(name, bool(m.get("force_concurrent")))
        except heavy_job_lock.HeavyJobBusy as e:
            print(f"deferred '{name}': {e}")
            return None
    try:
        new = _spawn(
            name,
            m["cmd"],
            m["cwd"],
            m.get("timeout"),
            bool(m.get("heavy")),
            _read_schedenv(name),
            m.get("env") or {},
            scope=True,
        )
    except _LaunchError as e:
        m.update(status=EXITED, exit_code=None, end_ts=core._now(), launch_error=str(e))
        with open(m["log_path"], "a") as fh:
            fh.write(f"proc: scheduled start failed: {e}\n")
        _save(m)
        _drop_schedenv(name)
        print(f"failed '{name}': {e}")
        return "failed"
    new["scheduled_for"] = m["scheduled_iso"]
    if m.get("owner"):  # the scheduling session owns it, not the launcher's environment
        new["owner"] = m["owner"]
    _save(new)
    _drop_schedenv(name)
    print(f"launched '{name}' pid={new['pid']}")
    return "launched"


def cmd_launch_due(args) -> int:
    """Start every scheduled job whose time has come, exactly once.

    Run once a minute by the launcher backend (see launcher.py). It finds the work in
    the live index (a few KB), never opening a record that is not due or dead, and
    takes the registry lock only around each state flip. Each flip re-reads its record
    under the lock and acts only if it is still scheduled and due, so two overlapping
    passes can never start the same entry twice. Running jobs whose process has gone
    are finalised here too, which is what removes them from the index when nobody
    else asks about them. The pass ends by writing its cost line (see cost.py)."""
    probe = cost.Probe()
    jobs = _live_index()
    entries = len(jobs)
    finalised = 0
    dead = [
        n
        for n, e in jobs.items()
        if e.get("status") == RUNNING and not _alive(e.get("pid"), e.get("st"))
    ]
    for name in dead:
        core._with_lock(lambda name=name: _reconcile_and_save(name))
        finalised += 1
    if dead:
        jobs = _live_index()
    now = core._now()
    due = sorted(
        (e.get("scheduled_ts") or 0, n)
        for n, e in jobs.items()
        if e.get("status") == SCHEDULED and (e.get("scheduled_ts") or 0) <= now
    )
    launched = failed = 0
    for _, name in due:
        outcome = core._with_lock(lambda name=name: _launch_one(name, now))
        launched += outcome == "launched"
        failed += outcome == "failed"
    cost.finish_launch_due(
        core.BASE,
        probe,
        entries=entries,
        launched=launched,
        failed=failed,
        finalised=finalised,
        lock_wait_s=_LOCK_SECONDS["wait"],
        lock_hold_s=_LOCK_SECONDS["hold"],
    )
    return 0


def cmd_launcher(args) -> int:
    """install / status / uninstall the per-minute launcher backend."""
    from proc_lib import launcher

    try:
        if args.action == "install":
            st = launcher.install(core.BASE)
        elif args.action == "uninstall":
            st = launcher.uninstall()
        else:
            st = launcher.status()
    except launcher.LauncherError as e:
        _err(str(e))
        return EXIT_LAUNCHER
    print(st.line())
    return 0 if st.ok or args.action == "uninstall" else EXIT_LAUNCHER


def cmd_hook(args) -> int:
    """install / uninstall / status of the Claude Code hook, or `unwrap` (the hook itself)."""
    from proc_lib import claude_hook, hook_judge

    if args.action == "unwrap":
        return claude_hook.main()
    dirs = [Path(d) for d in args.config_dir] or hook_judge.default_config_dirs()
    code = 0
    for d in dirs:
        try:
            if args.action == "install":
                st = hook_judge.install(d, core.BASE)
            elif args.action == "uninstall":
                st = hook_judge.uninstall(d, core.BASE)
            else:
                st = hook_judge.status(d)
        except (OSError, ValueError) as e:
            _err(f"{hook_judge.settings_path(d)}: {e}")
            return EXIT_USAGE
        print(st.line())
        if args.action != "uninstall" and not st.installed:
            code = EXIT_HOOK_MISSING
    return code


def _entry_meta(name: str, e: dict) -> dict:
    """A record-shaped view of an index entry: every field `list --json` shows."""
    meta = {
        "name": name,
        "status": e.get("status"),
        "pid": e.get("pid"),
        "pid_starttime": e.get("st"),
        "owner": e.get("owner") or {},
        "start_ts": e.get("start"),
    }
    if e.get("status") == SCHEDULED:
        meta["scheduled_ts"] = e.get("scheduled_ts")
    return meta


def _list_source(args, from_records: bool) -> tuple[list[dict], set[str]]:
    """The jobs `list` considers and the statuses to show (empty set: any).

    Default: the live index (running and scheduled). `--running` / `--scheduled`
    narrow it, with `--session ID` also by owner: that is the live index alone, and
    a record is opened only when `from_records` (the table view and `--full`, which
    show command, timeout or paths) asks for the listed jobs' own records. `--session
    ID` without `--running`/`--scheduled` is that session's own list of every job it
    started, in any state, and opens a record per job. `--all` is the whole registry,
    the only form that reads every record."""
    session = getattr(args, "session", None)
    want: set[str] = set()
    if getattr(args, "running", False):
        want.add(RUNNING)
    if getattr(args, "scheduled", False):
        want.add(SCHEDULED)
    if session and not want:
        names = _session_names(session)
        return [m for n in names if (m := _load(n)) is not None], want
    if getattr(args, "all", False) and not want:
        return _all_metas(history=True), want
    if not want:
        want = set(LIVE)
    entries = {
        n: e
        for n, e in _live_index().items()
        if e.get("status") in want
        and (not session or (e.get("owner") or {}).get("session") == session)
    }
    if from_records:
        return [m for n in entries if (m := _load(n)) is not None], want
    return [_entry_meta(n, e) for n, e in entries.items()], want


def _settle(metas: list[dict]) -> list[dict]:
    """Return the records with each still-`running`-but-dead one finalised, the writes
    done under one lock section (no lock when nothing needs finalising)."""
    stale = [
        m["name"]
        for m in metas
        if m.get("status") == RUNNING and not _alive(m.get("pid"), m.get("pid_starttime"))
    ]
    if not stale:
        return metas

    def op():
        return {n: _reconcile_and_save(n) for n in stale}

    fresh = core._with_lock(op)
    return [fresh.get(m["name"]) or m for m in metas]


def cmd_reindex(args) -> int:
    """Rebuild the live index and the per-session lists from every record."""
    jobs = core._with_lock(_rebuild_index)
    print(f"reindexed: {len(jobs)} live job(s)")
    return 0


def _exited_since(hours: float) -> list[dict]:
    """The jobs that ended in the last `hours`, from the live folder (jobs not yet swept)
    and the history day folders the window covers, nothing else. A running job whose process
    has gone is finalised first, so a job that just ended is not missed. A record whose file
    was last written before the window cannot have ended inside it (the record is written
    at, or after, the end), so it is skipped without being opened."""
    dead = [
        n
        for n, e in _live_index().items()
        if e.get("status") == RUNNING and not _alive(e.get("pid"), e.get("st"))
    ]
    if dead:
        core._with_lock(lambda: [_reconcile_and_save(n) for n in dead])
    cutoff = core._now() - hours * 3600
    first = _day(cutoff)
    metas = []
    seen: set[str] = set()
    folders = [core.BASE] + [_history_root() / d for d in reversed(_history_days()) if d >= first]
    for folder in folders:
        with os.scandir(folder) as it:
            for e in it:
                if (
                    not e.name.endswith(".json")
                    or e.name.startswith(".")
                    or e.stat().st_mtime < cutoff
                ):
                    continue
                m = _read_json(Path(e.path))
                if not m or m.get("name") in seen:
                    continue
                if m.get("status") in TERMINAL and (m.get("end_ts") or 0) >= cutoff:
                    seen.add(m.get("name"))
                    metas.append(m)
    return metas


def _list_exited(args) -> list[dict] | None:
    """The `--exited --since` listing, or None (error printed) when the flags are wrong."""
    since = getattr(args, "since", None)
    clash = [
        f
        for f in ("all", "running", "scheduled", "session")
        if getattr(args, f, None) not in (None, False)
    ]
    if getattr(args, "exited", False):
        if since is None or since <= 0:
            _err("--exited needs --since HOURS (a positive number)")
            return None
        if clash:
            _err(f"--exited cannot be combined with --{clash[0]}")
            return None
        return _exited_since(since)
    if since is not None:
        _err("--since belongs to --exited")
        return None
    return []


def cmd_list(args) -> int:
    as_json = getattr(args, "json", False)
    full = getattr(args, "full", False)
    exited = getattr(args, "exited", False)
    if exited or getattr(args, "since", None) is not None:
        metas = _list_exited(args)
        if metas is None:
            return EXIT_USAGE
        want: set[str] = set()
    else:
        metas, want = _list_source(args, from_records=full or not as_json)
        metas = _settle(metas)
    if want:
        metas = [m for m in metas if m.get("status") in want]

    # running first, then scheduled by start time, then the rest by start_ts desc
    def sort_key(m):
        status = m.get("status")
        if status == RUNNING:
            return (0, -(m.get("start_ts") or 0))
        if status == SCHEDULED:
            return (1, m["scheduled_ts"])
        return (2, -(m.get("start_ts") or 0))

    metas.sort(key=sort_key)
    if as_json:
        render = _status_json if full else _list_json
        print(json.dumps([render(m) for m in metas], indent=2 if full else None))
        return 0
    if not metas:
        print("no jobs")
        return 0

    rows = []
    for m in metas:
        cmd = _cmd_str(m)
        if len(cmd) > 50:
            cmd = cmd[:47] + "..."
        to = m.get("timeout")
        rows.append(
            [
                m.get("name", "?"),
                str(m.get("pid") or "-"),
                m.get("status", "?"),
                _elapsed(m),
                ("-" if to is None else f"{to}s"),
                _iso_short(m["scheduled_ts"]) if m.get("status") == SCHEDULED else "-",
                cmd,
            ]
        )
    headers = ["NAME", "PID", "STATUS", "ELAPSED", "TIMEOUT", "START", "CMD"]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    print(fmt.format(*headers))
    for r in rows:
        print(fmt.format(*r))
    return 0


def cmd_status(args) -> int:
    m = _current(args.name)
    if m is None:
        _err(f"no such job: {args.name}")
        return EXIT_NOTFOUND
    if getattr(args, "json", False):
        print(json.dumps(_status_json(m), indent=2))
    else:
        print("\n".join(_status_lines(m)))
    return 0


def cmd_logs(args) -> int:
    m = _load(args.name)
    if m is None:
        _err(f"no such job: {args.name}")
        return EXIT_NOTFOUND
    logp = Path(m.get("log_path") or _log_path(args.name))
    if not logp.exists():
        _err(f"no log file for job: {args.name}")
        return EXIT_NOTFOUND

    since_ts = None
    if getattr(args, "since", None) is not None:
        try:
            since_ts = _parse_zoned(args.since, "--since")
        except ValueError as e:
            _err(str(e))
            return EXIT_USAGE

    def _initial(lines: list[str]) -> list[str]:
        if since_ts is not None:
            lines = _since_filter(lines, since_ts, logp.stat().st_mtime)
        if args.tail is not None and args.tail >= 0:
            lines = lines[-args.tail :] if args.tail else []
        return lines

    if not args.follow:
        with open(logp, errors="replace") as fh:
            lines = fh.readlines()
        lines = _initial(lines)
        sys.stdout.write("".join(lines))
        if lines and not lines[-1].endswith("\n"):
            sys.stdout.write("\n")
        return 0

    # follow: poll for new bytes; auto-stop on terminal status or the hard cap.
    started = core._now()
    with open(logp, errors="replace") as fh:
        if args.tail is not None or since_ts is not None:
            # show only the filtered / last N lines first
            sys.stdout.write("".join(_initial(fh.readlines())))
            sys.stdout.flush()
        else:
            sys.stdout.write(fh.read())
            sys.stdout.flush()
        while True:
            chunk = fh.read()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            cur = _current(args.name)
            status = cur.get("status") if cur else None
            if status in TERMINAL:
                # drain any final bytes
                tail = fh.read()
                if tail:
                    sys.stdout.write(tail)
                    sys.stdout.flush()
                print(f"\n-- job {status} --", file=sys.stderr)
                return 0
            if core._now() - started > FOLLOW_HARD_CAP_SECONDS:
                print("\n-- follow auto-stopped after 600s cap --", file=sys.stderr)
                return EXIT_WAIT_TIMEOUT
            time.sleep(0.5)


def cmd_wait(args) -> int:
    m = _load(args.name)
    if m is None:
        _err(f"no such job: {args.name}")
        return EXIT_NOTFOUND
    deadline = core._now() + (args.timeout if args.timeout is not None else 3600)
    while True:
        cur = _current(args.name)  # no registry lock while the job is simply still running
        if cur is None:
            _err(f"no such job: {args.name}")
            return EXIT_NOTFOUND
        status = cur.get("status")
        if status in TERMINAL:
            # Non-zero on any unsuccessful end, so `proc wait a && next` chains
            # stop on failure; a distinct code keeps it apart from 3 (still
            # running) and 4 (unknown job) instead of passing the job's own rc
            # through, which would collide with them.
            print(_verdict(cur))
            return 0 if _job_succeeded(cur) else EXIT_JOB_FAILED
        if core._now() >= deadline:
            print(f"{_verdict(cur)} (wait timed out)")
            return EXIT_WAIT_TIMEOUT
        time.sleep(0.5)


def cmd_kill(args) -> int:
    def op():
        m = _load(args.name)
        if m is None:
            return ("notfound", None)
        # Reconcile FIRST: a job that already exited cleanly but whose registry
        # entry is still 'running' (no list/status/wait ran since) must be
        # finalized from its .rc sidecar -> 'exited' with the real exit code,
        # NOT mislabeled and signaled. This is the tech-debt fix that retires
        # the old `lost` status (which discarded the .rc code).
        before = m.get("status")
        _reconcile(m)
        if m.get("status") != before:
            _save(m)
        if m.get("status") in TERMINAL:
            return ("already", m)
        if m.get("status") == SCHEDULED:
            m.update(status=KILLED, end_ts=core._now())
            _save(m)
            _drop_schedenv(args.name)
            return ("cancelled", m)
        if m.pop("next_run", None):
            # killing a job also cancels the run it had asked for
            _drop_schedenv(args.name)
            _save(m)

        pid = m.get("pid")
        # Launched with start_new_session=True, so the wrapper is the session
        # leader and sid == its pid. We signal by SESSION, not pgid (see
        # _session_members for why timeout's nested process group defeats killpg).
        sid = pid
        # After reconcile a still-RUNNING job is genuinely alive (reconcile would
        # have finalized a dead one). Re-check defensively before signaling.
        if not _alive(pid, m.get("pid_starttime")):
            _reconcile(m)
            _save(m)
            return ("already", m)

        sig = signal.SIGKILL if args.force else signal.SIGTERM
        signaled = core._signal_session(sid, sig)
        if signaled == 0:
            # session already empty — nothing left to kill
            m["status"] = KILLED
            m["end_ts"] = core._now()
            ec = _read_rc(m)
            if ec is not None:
                m["exit_code"] = ec
            _save(m)
            return ("gone", m)
        return ("signaled", m, sig)

    res = core._with_lock(op)
    tag = res[0]
    if tag == "notfound":
        _err(f"no such job: {args.name}")
        return EXIT_NOTFOUND
    if tag == "already":
        m = res[1]
        ec = m.get("exit_code")
        print(f"{args.name}: already {m.get('status')} (exit_code={ec})")
        return 0
    if tag == "gone":
        print(f"{args.name}: process already gone -> marked killed")
        return 0
    if tag == "cancelled":
        print(f"{args.name}: scheduled start cancelled")
        return 0

    # signaled — wait outside the lock, then finalize.
    m = res[1]
    sig = res[2]
    sid = m.get("pid")  # session leader id == wrapper pid

    # Drain the WHOLE SESSION, not just the leader pid. The bash wrapper (leader)
    # dies on SIGTERM instantly; `timeout` and a worker with its own SIGTERM
    # handler live on in a different process group but the SAME session. We
    # RE-signal each poll iteration rather than signal once: a process tree that
    # is still forking children when the first signal lands (e.g. `proc kill`
    # immediately after `start`, before bash has spawned its grandchildren) would
    # otherwise let a late-forked descendant escape a one-shot kill.
    if sig == signal.SIGTERM:
        # grace period: let the session exit on its own, re-TERMing stragglers.
        deadline = core._now() + core.KILL_GRACE_SECONDS
        while core._now() < deadline:
            if not _session_alive(sid):
                break
            core._signal_session(sid, signal.SIGTERM)
            time.sleep(0.2)
    # Hard-kill drain (also the entire --force path): re-SIGKILL any member that
    # is still or newly alive until the session is empty or the budget is spent.
    for _ in range(core.KILL_DRAIN_POLLS):  # ~5s ceiling, exits early the moment the session drains
        if not _session_alive(sid):
            break
        core._signal_session(sid, signal.SIGKILL)
        time.sleep(0.1)

    def finalize():
        cur = _load(args.name) or m
        if cur.pop("next_run", None):  # asked for during the kill grace
            _drop_schedenv(args.name)
        # Decided under the lock, on a fresh scan: `killed` is written only when
        # no member of the session is alive.
        survivors = _session_members(sid)
        if survivors:
            # Not dead: keep the status as it was and name the pids in the record.
            cur["kill_survivors"] = survivors
            _save(cur)
            return survivors
        cur.pop("kill_survivors", None)
        cur["status"] = KILLED
        cur["end_ts"] = core._now()
        # If the job recorded an exit code before dying (e.g. it caught SIGTERM
        # and exited cleanly), keep it for information — status stays `killed`.
        ec, job_cost = _read_rc_file(cur)
        if ec is not None:
            cur["exit_code"] = ec
        if job_cost:
            cur["cost"] = job_cost
        _save(cur)
        return []

    survivors = core._with_lock(finalize)
    if survivors:
        _err(
            f"{args.name}: NOT killed - session {sid} still has live members "
            f"after SIGKILL: pids {', '.join(map(str, survivors))}"
        )
        return EXIT_KILL_SURVIVORS
    print(f"{args.name}: killed (sid {sid})")
    return 0


def _job_files(name: str, m: dict | None) -> list[Path]:
    """Every file of a job, wherever it is now: the record in the live folder and in a
    history folder, the log and `.rc` its record names (else the live defaults), the
    `.rss`, each also in the history folder the record sits in."""
    hp = _history_record(name)
    homes = [core.BASE] + ([hp.parent] if hp else [])
    files = [_meta_path(name)] + ([hp] if hp else [])
    files += [Path(m[k]) for k in ("log_path", "rc_path") if m and m.get(k)]
    for h in homes:
        files += [h / f"{name}.log", h / f"{name}.rc", h / f"{name}.rss"]
    return list(dict.fromkeys(files))


def _rm_one(name: str, force: bool) -> int:
    def op():
        m = _load(name)
        if m is None:
            return ("notfound", None)
        before = m.get("status")
        _reconcile(m)
        if m.get("status") != before:
            _save(m)
        if m.get("status") == RUNNING and not force:
            return ("running", m)
        return ("ok", m)

    res = core._with_lock(op)
    tag, m = res
    if tag == "notfound":
        _err(f"no such job: {name}")
        return EXIT_NOTFOUND
    if tag == "running":
        _err(
            f"job '{name}' is still running; use `proc kill {name}` "
            f"first or `proc rm --force {name}`"
        )
        return EXIT_USAGE

    # if forcing removal of a running job, kill it first
    if m.get("status") == RUNNING and force:
        kargs = types.SimpleNamespace(name=name, force=True)
        krc = cmd_kill(kargs)
        if krc not in (0, EXIT_NOTFOUND):
            return krc  # members survive: keep the record that names them

    def do_remove():
        # Also drop this job's heavy-job lockfile, if any — otherwise an rm'd
        # heavy job leaves a stale lock that only ages out (or, under PID reuse,
        # could falsely block a future heavy job for up to HEAVY_JOB_STALE_SEC).
        hlock = core.BASE / "heavy_job" / f"{name}.lock"
        for f in (*_job_files(name, m), hlock, _schedenv_path(name)):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                _err(f"could not remove {f}: {e}")
        core._index_set(name, None)
        return True

    core._with_lock(do_remove)
    print(f"removed '{name}'")
    return 0


def cmd_rm(args) -> int:
    # `name` is nargs="+" (a list); tolerate a bare string for internal callers.
    names = args.name if isinstance(args.name, list) else [args.name]
    worst = 0
    for name in names:
        rc = _rm_one(name, args.force)
        if rc != 0 and worst == 0:
            worst = rc
    return worst


def cmd_clean(args) -> int:
    """Remove terminal jobs whose end is older than the cutoff (default 24 h).

    The scan reads records without the registry lock; each removal re-reads its record
    under the lock and removes it only if it is still terminal and old, so the lock is
    held per removal, not for the whole scan. Only records stored as terminal are
    considered: a job still marked running is the reconcile's business (launch-due
    finalises it within a minute)."""
    hours = args.older_than if args.older_than is not None else 24
    cutoff = core._now() - hours * 3600

    def expired(m) -> bool:
        if not m or m.get("status") not in TERMINAL:
            return False
        return (m.get("end_ts") or m.get("start_ts") or 0) <= cutoff

    def remove(name: str) -> bool:
        m = _load(name)
        if not expired(m):
            return False
        for f in _job_files(name, m):
            with contextlib.suppress(FileNotFoundError):
                f.unlink()
        core._index_set(name, None)
        return True

    removed = []
    for m in _all_metas(history=True):
        if expired(m) and core._with_lock(lambda n=m["name"]: remove(n)):
            removed.append(m["name"])
    print(f"removed {len(removed)} terminal job(s) older than {hours}h")
    for n in removed:
        print(f"  - {n}")
    return 0


def cmd_archive(args) -> int:
    """Move every finished record still in the live folder, with its log and sidecars,
    into its history day folder (records past the retention bound are deleted): the one-time
    move of a registry that grew before history existed, by the same `_archive` a job's end
    uses. A single pass over the live folder, the lock held per record; a record that
    changed meanwhile (no longer finished) is left."""
    moved: list[str] = []
    _ensure_base()

    def one(name: str) -> bool:
        m = _read_json(_meta_path(name))
        if not m or m.get("status") not in TERMINAL or m.get("name") != name:
            return False
        _archive(m)
        return True

    with os.scandir(core.BASE) as it:
        names = sorted(e.name[:-5] for e in it if e.name.endswith(".json") and e.is_file())
    for name in names:
        if core._with_lock(lambda name=name: one(name)):
            moved.append(name)

    def forget():  # a finished job a crash left in the index leaves it now
        jobs = _index_locked()
        gone = [n for n in moved if n in jobs]
        for n in gone:
            del jobs[n]
        if gone:
            _write_index(jobs)

    core._with_lock(forget)
    kept = len(names) - len(moved)
    print(f"archived {len(moved)} finished job(s); live folder keeps {kept} record(s)")
    return 0
