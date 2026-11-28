# spec: specs/proc_lib/registry.spec.md
"""proc engine — the job registry: paths, records, history, live index, lock, reconcile.

One `<name>.json` per job under `$PROC_HOME`, the daily move of finished jobs into
`history/<UTC day>/`, the `.live-index` and per-session lists, the registry `flock`, and
the reconcile that brings a recorded status in line with the process. Paths read
`core.BASE` at call time.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from proc_lib import cost
from proc_lib.corelink import core
from proc_lib.procfs import _alive

# status values
SCHEDULED = "scheduled"  # registered with --at, no process yet; the launcher starts it
RUNNING = "running"
EXITED = "exited"
KILLED = "killed"
TIMEOUT = "timeout"
LOST = "lost"  # legacy-read only — no longer produced (see _reconcile / cmd_kill)
TERMINAL = {EXITED, KILLED, TIMEOUT, LOST}

EXIT_USAGE = 2
EXIT_WAIT_TIMEOUT = 3
EXIT_NOTFOUND = 4
EXIT_JOB_FAILED = 5
EXIT_LAUNCHER = 6
EXIT_HOOK_MISSING = 7
EXIT_KILL_SURVIVORS = 8  # kill: session members still alive after SIGKILL

# Set in every job's environment so a job can recognise itself when it asks to be
# re-scheduled (`proc start --at` / `proc reschedule` from inside the running job).
ENV_JOB_NAME = "PROC_JOB_NAME"
ENV_OWNER_SESSION = "CLAUDE_CODE_SESSION_ID"
ENV_OWNER_PROJECT = "COORD_PROJECT"


# A scheduled entry this far past its time means the launcher is not firing.
OVERDUE_WARN_SECONDS = 180.0
# Hard ceiling for `proc logs --follow` so it never tails forever unattended.
FOLLOW_HARD_CAP_SECONDS = 600.0


# A finished job's record and sidecars stay at `$PROC_HOME/<job>.*` until the first proc call
# of a UTC day after the day following its end moves them into `history/<UTC day of its end>/`;
# the whole folder of a day is deleted once the day is older than the retention bound.
HISTORY_DIR = "history"
HISTORY_STAMP = ".swept"  # holds the UTC day of the last sweep; one read per proc call
HISTORY_DAYS_DEFAULT = 14
HISTORY_DAYS_ENV = "PROC_HISTORY_DAYS"
HISTORY_DAYS_CONFIG = "history_days"
_DAY_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _iso(ts: float) -> str:
    """UTC ISO8601 from epoch seconds."""
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def _lockfile() -> Path:
    return core.BASE / ".lock"


def _ensure_base() -> None:
    core.BASE.mkdir(parents=True, exist_ok=True)


def _meta_path(name: str) -> Path:
    return core.BASE / f"{name}.json"


def _log_path(name: str) -> Path:
    return core.BASE / f"{name}.log"


def _rc_path(name: str) -> Path:
    """Sidecar where the wrapper records the job's real exit code."""
    return core.BASE / f"{name}.rc"


def _schedenv_path(name: str) -> Path:
    """Private (0600) sidecar holding the full environment a scheduled run starts
    with. It is never in the registry JSON: unlike `env` there is no masking, the
    launcher needs the real values."""
    return core.BASE / f"{name}.schedenv"


def _read_json(p: Path):
    try:
        with open(p) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def _load(name: str):
    """A job's record: the live folder first, else its daily history folder."""
    m = _read_json(_meta_path(name))
    if m is not None:
        return m
    hp = _history_record(name)
    return _read_json(hp) if hp is not None else None


# --------------------------------------------------------------------------- #
# history: finished jobs by UTC day of their end
# --------------------------------------------------------------------------- #
def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def _history_root() -> Path:
    return core.BASE / HISTORY_DIR


def _history_days() -> list[str]:
    """The day folders present, oldest first."""
    try:
        with os.scandir(_history_root()) as it:
            return sorted(e.name for e in it if _DAY_NAME.match(e.name) and e.is_dir())
    except OSError:
        return []


def _history_record(name: str) -> Path | None:
    """The newest history record of `name` (at most the retention bound of stats)."""
    for day in reversed(_history_days()):
        p = _history_root() / day / f"{name}.json"
        if p.exists():
            return p
    return None


def _history_days_bound() -> int:
    """The retention bound in days. The user's setting (`PROC_HISTORY_DAYS`, else
    `history_days` in `~/.config/proc`) applies to the default registry only; any other
    home reads `<home>/history_days` alone, like the cost alert's command."""
    if cost.is_default_home(core.BASE):
        text = os.environ.get(HISTORY_DAYS_ENV, "").strip()
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"))
        cfg = cfg / "proc" / HISTORY_DAYS_CONFIG
    else:
        text = ""
        cfg = core.BASE / HISTORY_DAYS_CONFIG
    if not text:
        try:
            text = next(
                (
                    ln.strip()
                    for ln in cfg.read_text().splitlines()
                    if ln.strip()[:1] not in ("", "#")
                ),
                "",
            )
        except OSError:
            text = ""
    try:
        days = int(text)
    except ValueError:
        return HISTORY_DAYS_DEFAULT
    return days if days >= 1 else HISTORY_DAYS_DEFAULT


def housekeeping(now: float | None = None) -> None:
    """The first proc call of a UTC day moves the finished jobs that ended before the
    previous UTC day began into their history day folders (`_archive`, the lock held per
    record) and deletes the day folders older than the retention bound, whole (no per-file
    sweep, no timer). So a finished job keeps its flat path for at least 24 h and the live
    folder holds about two days of finished jobs. The stamp is taken before the sweep, so a
    concurrent call skips it; a sweep that dies is finished by the next day's. A registry
    that does not exist yet is left untouched. Never raises: housekeeping is not a reason
    for a command to fail."""
    root = _history_root()
    try:
        if not core.BASE.is_dir():
            return
        t = now if now is not None else core._now()
        today = _day(t)
        stamp = root / HISTORY_STAMP
        try:
            if stamp.read_text() == today:
                return
        except OSError:
            pass
        root.mkdir(exist_ok=True)
        tmp = stamp.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(today)
        os.replace(tmp, stamp)
        cutoff = _day(t - _history_days_bound() * 86400)
        import shutil

        for day in _history_days():
            if day < cutoff:
                shutil.rmtree(root / day, ignore_errors=True)
        _move_ended_before(_day(t - 86400), t)
    except OSError:
        return


def _move_ended_before(first_kept_day: str, now: float | None = None) -> None:
    """Move every finished flat job whose end day is earlier than `first_kept_day` into
    its history folder. Each record is re-read under the lock and moved only if it is
    still finished and still that old."""
    with os.scandir(core.BASE) as it:
        names = sorted(
            e.name[:-5]
            for e in it
            if e.name.endswith(".json") and not e.name.startswith(".") and e.is_file()
        )

    def one(name: str) -> None:
        m = _read_json(_meta_path(name))
        if not m or m.get("name") != name or m.get("status") not in TERMINAL:
            return
        if _day(m.get("end_ts") or m.get("start_ts") or core._now()) < first_kept_day:
            _archive(m, now)

    for name in names:
        core._with_lock(lambda name=name: one(name))


def _archive(meta: dict, now: float | None = None) -> None:
    """Move a finished job into its history day folder (the daily sweep and `proc archive`
    call this, never a job's end): its log, `.rc` and `.rss` first,
    then the record (written there with the new paths), then the live record is removed.
    A crash between the last two leaves the record in both places; the live copy wins and
    the next archive of the job (a save, or `proc archive`) finishes the move. A job whose
    end already lies beyond the retention bound is deleted instead, so the one-time
    migration of an old registry also prunes it. `now` is the clock of the caller's sweep
    (default: the current time). Caller holds the lock."""
    name = meta["name"]
    end = meta.get("end_ts") or meta.get("start_ts") or core._now()
    day = _day(end)
    live = {"log_path": _log_path(name), "rc_path": _rc_path(name)}
    sidecars = [*live.values(), _rss_path(name)]
    if day < _day((now if now is not None else core._now()) - _history_days_bound() * 86400):
        for f in (*sidecars, _meta_path(name), _schedenv_path(name)):
            with contextlib.suppress(FileNotFoundError):
                f.unlink()
        return
    dest = _history_root() / day
    dest.mkdir(parents=True, exist_ok=True)
    # a copy already in another day folder (the end time of a finished record was changed)
    # comes along: its files move here and the copy goes, so one job has one record
    prev = _history_record(name)
    prev_home = prev.parent if prev is not None and prev.parent != dest else None
    for home in ([prev_home] if prev_home else []) + [core.BASE]:
        for ext in ("log", "rc", "rss"):
            with contextlib.suppress(FileNotFoundError):
                os.replace(home / f"{name}.{ext}", dest / f"{name}.{ext}")
    for key, f in live.items():
        cur = meta.get(key)
        if cur is None or Path(cur) in (f, *([prev_home / f.name] if prev_home else [])):
            meta[key] = str(dest / f.name)  # a path set elsewhere is not ours to repoint
    p = dest / f"{name}.json"
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w") as fh:
        json.dump(meta, fh, indent=2)
    os.replace(tmp, p)
    with contextlib.suppress(FileNotFoundError):
        _meta_path(name).unlink()
    if prev_home:
        with contextlib.suppress(FileNotFoundError):
            (prev_home / f"{name}.json").unlink()


def _save(meta: dict) -> None:
    """Write a job's record and keep the live index in step with it. Caller holds the
    registry lock.

    Order is what makes the pair crash-safe: a job that becomes live (or changes
    while live) is put in the index BEFORE its record is written, a job that stops
    being live leaves the index AFTER its record is written. A crash between the two
    writes therefore leaves at worst a finished job still listed as live (the next
    reconcile removes it), never a live job the index does not know. A finished job's
    record stays in the live folder; the daily sweep moves it later (`housekeeping`)."""
    name = meta["name"]
    entry = _live_entry(meta)
    if entry is not None:
        core._index_set(name, entry)
    p = _meta_path(name)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w") as fh:
        json.dump(meta, fh, indent=2)
    os.replace(tmp, p)
    if entry is None:
        core._index_set(name, None)


def _all_metas(history: bool = False):
    """Every record in the live folder, plus with `history` every record of the history
    folders (a live copy wins over a history copy of the same job). Reads all of them:
    only `list --all`, `clean` and the index rebuild call it; no routine path does."""
    metas: list[dict] = []
    if not core.BASE.exists():
        return metas
    seen: set[str] = set()
    sources = [core.BASE]
    if history:
        sources += [_history_root() / d for d in reversed(_history_days())]
    for src in sources:
        for p in sorted(src.glob("*.json")):
            m = _read_json(p)
            if m is None or m.get("name") in seen:
                continue
            seen.add(m.get("name"))
            metas.append(m)
    return metas


# --------------------------------------------------------------------------- #
# live index: running and scheduled jobs (+ jobs per starting session)
# --------------------------------------------------------------------------- #
# `.live-index` (not *.json, so a reader globbing the registry for records never
# takes it for one) maps each running or scheduled job name to the few fields the
# routine paths need: status, scheduled_ts, pid + starttime (liveness without
# opening the record), whether it asked for a next run, start time and owner (what
# `list --json` shows, so a live listing never opens a record). It is
# replaced atomically (os.replace) and written only under the registry lock by
# `_save`, so it can be read without the lock. `sessions/<id>` is an append-only
# list of the job names a Claude session started, in any state.
INDEX_VERSION = 2
LIVE = (RUNNING, SCHEDULED)


def _index_path() -> Path:
    return core.BASE / ".live-index"


def _sessions_dir() -> Path:
    return core.BASE / "sessions"


def _live_entry(meta: dict) -> dict | None:
    """The index entry of a record, or None when the job is not live."""
    status = meta.get("status")
    if status not in LIVE:
        return None
    entry: dict = {"status": status}
    if status == SCHEDULED:
        entry["scheduled_ts"] = meta.get("scheduled_ts")
    else:
        entry["pid"] = meta.get("pid")
        entry["st"] = meta.get("pid_starttime")
        entry["start"] = meta.get("start_ts")
        if meta.get("next_run"):
            entry["nxt"] = True
    if meta.get("owner"):
        entry["owner"] = meta["owner"]
    return entry


def _read_index() -> dict | None:
    """The index's job map, or None when it is missing, unreadable or of another
    version (the caller then rebuilds it under the lock). Lock-free."""
    try:
        with open(_index_path()) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
        return None
    jobs = data.get("jobs")
    return jobs if isinstance(jobs, dict) else None


def _write_index(jobs: dict) -> None:
    p = _index_path()
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w") as fh:
        json.dump({"version": INDEX_VERSION, "jobs": jobs}, fh, separators=(",", ":"))
    os.replace(tmp, p)


def _safe_session(session: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session)[:200]
    return "_" + safe if safe.startswith(".") else safe


def _write_session_file(session: str, names: list[str]) -> None:
    d = _sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / _safe_session(session)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text("".join(f"{n}\n" for n in names))
    os.replace(tmp, p)


def _rebuild_index() -> dict:
    """Rebuild the index (and the per-session lists) from every record: the one full
    scan, run when the index is missing or by `proc reindex`. Caller holds the lock."""
    jobs: dict = {}
    sessions: dict[str, list[str]] = {}
    for m in _all_metas(history=True):
        name = m.get("name")
        if not name:
            continue
        entry = _live_entry(m)
        if entry is not None:
            jobs[name] = entry
        session = (m.get("owner") or {}).get("session")
        if session:
            sessions.setdefault(session, []).append(name)
    _write_index(jobs)
    for session, names in sessions.items():
        _write_session_file(session, names)
    return jobs


def _index_locked() -> dict:
    """The index job map, rebuilt first when missing. Caller holds the lock."""
    jobs = _read_index()
    return jobs if jobs is not None else _rebuild_index()


def _index_set(name: str, entry: dict | None) -> None:
    """Put `name` in the index (or take it out when `entry` is None). Caller holds
    the lock."""
    jobs = _index_locked()
    if entry is None:
        if name not in jobs:
            return
        del jobs[name]
    else:
        if jobs.get(name) == entry:
            return
        jobs[name] = entry
    _write_index(jobs)


def _live_index() -> dict:
    """The live job map without taking the lock (rebuilt under it when missing)."""
    jobs = _read_index()
    return jobs if jobs is not None else core._with_lock(_index_locked)


def _session_add(session: str, name: str) -> None:
    """Record that `session` started job `name`. One O_APPEND write of a short line,
    atomic without the lock."""
    d = _sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(d / _safe_session(session)), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, f"{name}\n".encode())
    finally:
        os.close(fd)


def _session_names(session: str) -> list[str]:
    try:
        text = (_sessions_dir() / _safe_session(session)).read_text()
    except OSError:
        return []
    return list(dict.fromkeys(ln for ln in text.splitlines() if ln))


# Seconds this process spent waiting for, and holding, the registry lock; the
# launch-due cost line reports them.
_LOCK_SECONDS = {"wait": 0.0, "hold": 0.0}


def _with_lock(fn):
    """Run fn() while holding an exclusive flock on the registry lockfile, so
    parallel proc invocations don't corrupt JSON. The lockfile is created on
    demand and never deleted (cheap, avoids unlink/recreate races)."""
    _ensure_base()
    lock_fd = os.open(str(_lockfile()), os.O_CREAT | os.O_RDWR, 0o644)
    asked = time.monotonic()
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        got = time.monotonic()
        _LOCK_SECONDS["wait"] += got - asked
        try:
            return fn()
        finally:
            _LOCK_SECONDS["hold"] += time.monotonic() - got
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


# `times` (a bash builtin) prints two lines of "<m>m<s>s <m>m<s>s": the shell's own
# user/system CPU, then that of its reaped children, which is the job.
_TIMES_LINE = re.compile(r"^(\d+)m([\d.,]+)s (\d+)m([\d.,]+)s$")
# /proc/<wrapper>/io counters the wrapper copies into `.rc` (the kernel folds a reaped
# child's counters into its parent's).
_IO_KEYS = ("rchar", "wchar", "syscr", "syscw", "read_bytes", "write_bytes")


# Peak resident memory of the job, which only its reaping parent can read (wait4 rusage).
# The wrapper runs the job under GNU `time -f 'maxrss %M'` (kB) when it is installed and
# folds the figure into `.rc` as `maxrss_kb <n>`; otherwise, or when `time` was killed
# before it reported, it writes `maxrss_note <why>` so `status` can say why it is absent.
_RSS_NOTE_NO_TIME = "GNU time is not installed"
_RSS_NOTE_NO_FIGURE = "GNU time left no figure, killed before it could report"
_RSS_NOTE_OLD = "job started before peak RSS was recorded"


def _rss_path(name: str) -> Path:
    """Side file GNU time writes the job's peak RSS to; the wrapper folds it into `.rc`
    and removes it (never the job's log)."""
    return core.BASE / f"{name}.rss"


def _seconds(minutes: str, seconds: str) -> float:
    return round(int(minutes) * 60 + float(seconds.replace(",", ".")), 3)


def _parse_rc(text: str) -> tuple[int | None, dict | None]:
    """Exit code (first line) and cost figures from the text of a `.rc` sidecar. An old
    one holds the code alone."""
    lines = text.splitlines()
    try:
        rc = int(lines[0].strip()) if lines and lines[0].strip() else None
    except ValueError:
        rc = None
    cost: dict = {}
    times = [m for ln in lines[1:] if (m := _TIMES_LINE.match(ln.strip()))]
    if len(times) >= 2:
        child = times[1]
        cost["cpu_user_s"] = _seconds(child.group(1), child.group(2))
        cost["cpu_sys_s"] = _seconds(child.group(3), child.group(4))
    for ln in lines[1:]:
        if ln.startswith("io "):
            key, _, val = ln[3:].partition(":")
            if key in _IO_KEYS and val.strip().isdigit():
                cost[key] = int(val)
        elif ln.startswith("maxrss_kb ") and ln[10:].strip().isdigit():
            cost["maxrss_kb"] = int(ln[10:])
        elif ln.startswith("maxrss_note "):
            cost["maxrss_note"] = ln[12:].strip()
    return rc, cost or None


def _read_rc_file(meta: dict) -> tuple[int | None, dict | None]:
    """(exit code, cost) the wrapper recorded in the .rc sidecar; (None, None) if it
    isn't there yet / unreadable."""
    rp = meta.get("rc_path") or str(_rc_path(meta.get("name", "")))
    try:
        with open(rp) as fh:
            return _parse_rc(fh.read())
    except (FileNotFoundError, OSError):
        return (None, None)


def _read_rc(meta: dict):
    """Read the exit code the wrapper recorded in the .rc sidecar, or None if
    it isn't there yet / unreadable."""
    return _read_rc_file(meta)[0]


def _reconcile(meta: dict) -> dict:
    """Bring a job's recorded status in line with reality.

    For detached processes we are not the parent across separate proc
    invocations, so we cannot reap an exit code via waitpid. Liveness is
    probed via os.kill(pid,0) + the /proc starttime guard. When a RUNNING job
    is no longer alive we consult the .rc sidecar the wrapper wrote:
      - rc 124 on a job that had a --timeout  -> status `timeout`
      - any other rc                          -> status `exited`, exit_code=rc
      - no rc file (e.g. SIGKILL beat the wrapper) -> `exited`, exit_code unknown
    Already-terminal jobs are returned untouched. This single code path also
    covers PID reuse (starttime mismatch -> not alive -> finalized here),
    recovering the real exit code instead of discarding it: that retires the
    old `lost` status, which used to be set by cmd_kill and lost the .rc code.
    """
    if meta.get("status") != RUNNING:
        return meta
    if _alive(meta.get("pid"), meta.get("pid_starttime")):
        return meta
    rc, job_cost = _read_rc_file(meta)
    if job_cost:
        meta["cost"] = job_cost
    if rc == 124 and meta.get("timeout") is not None:
        meta["status"] = TIMEOUT
        meta["exit_code"] = rc
    else:
        meta["status"] = EXITED
        if rc is not None:
            meta["exit_code"] = rc
    if meta.get("end_ts") is None:
        meta["end_ts"] = core._now()
    if meta.get("next_run"):
        _apply_next_run(meta)
    return meta


def _reconcile_and_save(name: str):
    """Load + reconcile + persist if changed. Caller must hold the lock."""
    m = _load(name)
    if m is None:
        core._index_set(name, None)  # the record is gone: drop a stale index entry
        return None
    before = m.get("status")
    _reconcile(m)
    if m.get("status") != before:
        _save(m)
    else:
        core._index_set(name, _live_entry(m))  # also drops an entry a crash left behind
    return m


def _current(name: str):
    """A job's up-to-date record. The common read (job missing, scheduled, running and
    alive, or already finished) opens one record and takes no lock; only a record
    still marked running whose process has gone is finalised, under the lock."""
    m = _load(name)
    if (
        m is not None
        and m.get("status") == RUNNING
        and not _alive(m.get("pid"), m.get("pid_starttime"))
    ):
        return core._with_lock(lambda: _reconcile_and_save(name))
    return m


def _apply_next_run(meta: dict) -> None:
    """A running job asked to run again later and has now ended: turn its entry
    into a scheduled one. The log is kept and keeps growing across runs."""
    nxt = meta.pop("next_run")
    meta.update(nxt["spec"])
    meta.update(
        status=SCHEDULED,
        pid=None,
        pgid=None,
        pid_starttime=None,
        start_ts=None,
        start_iso=None,
        end_ts=None,
        exit_code=None,
    )
