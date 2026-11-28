# spec: specs/proc_lib/cost.spec.md
"""Cost record and cost alert of a `proc launch-due` pass.

The pass measures itself with two reads of counters the kernel already keeps
(`getrusage` and `/proc/self/io`), appends one JSON line to proc's own daily file
under ``$PROC_HOME/cost/`` and, when the line exceeds its bound, raises one alert per
key per day. No resident process, no scan: the cost of measuring is one append.

The alert command is configuration, never named here, and it belongs to a registry:
the default registry (``~/.local/state/proc``) reads the environment variable
:data:`ALERT_ENV`, else the first line of ``cost_alert_cmd`` in ``$XDG_CONFIG_HOME/proc``
(default ``~/.config/proc``); any other ``$PROC_HOME`` (a test, a scratch home) reads only
``<home>/cost_alert_cmd``. So a run on a scratch home records its line and posts nothing
unless that home carries its own file. With none configured nothing is posted and the
day's stamp is not taken.

stdlib-only, like the rest of the package.
"""

from __future__ import annotations

import contextlib
import json
import os
import resource
import shlex
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

__all__ = [
    "ALERT_CONFIG",
    "ALERT_ENV",
    "KEEP_DAYS",
    "LAUNCH_DUE_BOUNDS",
    "MB",
    "Probe",
    "TOOL",
    "alert_command",
    "append_line",
    "finish_launch_due",
    "is_default_home",
    "over_bounds",
    "raise_alert",
]

TOOL = "proc"
KEEP_DAYS = 14
ALERT_ENV = "PROC_COST_ALERT_CMD"
ALERT_CONFIG = "cost_alert_cmd"

# Bounds of a launch-due pass that started nothing (the steady state: it only reads
# the index). A pass that started or failed a job does real work and is only recorded.
LAUNCH_DUE_BOUNDS = {
    "wall_s": 0.5,
    "cpu_s": 0.3,
    "rchar_mb": 1.0,
    "rss_mb": 40.0,
    "lock_hold_s": 0.2,
}
MB = 1048576.0


def _proc_io() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        with open("/proc/self/io") as fh:
            for line in fh:
                key, _, val = line.partition(":")
                if val.strip().isdigit():
                    out[key] = int(val)
    except OSError:
        pass
    return out


class Probe:
    """Started at the top of a measured path; :meth:`stats` reads the process totals.

    CPU, rchar and RSS are the process's totals since it started, interpreter start-up
    and imports included: that is what the bound is set against."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()

    def stats(self) -> dict:
        own = resource.getrusage(resource.RUSAGE_SELF)
        kids = resource.getrusage(resource.RUSAGE_CHILDREN)
        io = _proc_io()
        return {
            "wall_s": round(time.monotonic() - self.t0, 3),
            "cpu_s": round(own.ru_utime + own.ru_stime + kids.ru_utime + kids.ru_stime, 3),
            "rss_mb": round(own.ru_maxrss / 1024, 1),
            "rchar_mb": round(io.get("rchar", 0) / MB, 3),
            "read_mb": round(io.get("read_bytes", 0) / MB, 3),
            "write_mb": round(io.get("write_bytes", 0) / MB, 3),
        }


def over_bounds(stats: dict, bounds: dict) -> list[str]:
    """Names of the figures in `stats` above their bound."""
    return [k for k, limit in bounds.items() if stats.get(k, 0) > limit]


def _day(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d")


def _rmtree(d: Path) -> None:
    import shutil

    shutil.rmtree(d, ignore_errors=True)


def _prune(base: Path, today: datetime) -> None:
    """Delete this tool's cost files and alert stamps older than KEEP_DAYS. Run when a
    new day's file is opened, so no job is needed to sweep."""
    oldest = _day(today - timedelta(days=KEEP_DAYS))
    for f in (base / "cost").glob("*.jsonl"):
        if f.stem < oldest:
            with contextlib.suppress(OSError):
                f.unlink()
    for d in (base / "cost-alert").glob("*"):
        if d.name < oldest:
            _rmtree(d)


def append_line(base: Path, record: dict, today: datetime | None = None) -> None:
    """Append one JSON line (one O_APPEND write, atomic without a lock) to today's file."""
    today = today or datetime.now(UTC)
    d = base / "cost"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_day(today)}.jsonl"
    fresh = not path.exists()
    line = json.dumps(record, separators=(",", ":")) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)
    if fresh:
        _prune(base, today)


def _default_home() -> Path:
    return Path(os.path.expanduser("~/.local/state/proc"))


def is_default_home(base: Path) -> bool:
    try:
        return base.resolve() == _default_home().resolve()
    except OSError:
        return False


def alert_command(base: Path | None = None) -> list[str] | None:
    """The alert command configured for the registry `base`, as argv, or None.

    The user's configuration (environment variable, ``~/.config/proc``) is for the
    default registry only; every other home reads ``<base>/cost_alert_cmd`` alone.
    `base` None means the default registry."""
    if base is None or is_default_home(base):
        text = os.environ.get(ALERT_ENV, "").strip()
        cfg_file = Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"))
        cfg_file = cfg_file / "proc" / ALERT_CONFIG
    else:
        text = ""
        cfg_file = base / ALERT_CONFIG
    if not text:
        try:
            lines = cfg_file.read_text().splitlines()
        except OSError:
            return None
        text = next((ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")), "")
    try:
        argv = shlex.split(text)
    except ValueError:
        return None
    return argv or None


def raise_alert(
    base: Path, path: str, record: dict, bound: dict, today: datetime | None = None
) -> bool:
    """Post the alert for `path` once per day. The stamp `cost-alert/<date>/<tool>.<path>`
    is created with O_EXCL; only the pass that creates it runs the command, detached
    and never waited for. Returns True when this call posted."""
    argv = alert_command(base)
    if argv is None:
        return False
    today = today or datetime.now(UTC)
    stamp_dir = base / "cost-alert" / _day(today)
    stamp_dir.mkdir(parents=True, exist_ok=True)
    key = f"cost:{TOOL}:{path}"
    try:
        fd = os.open(str(stamp_dir / f"{TOOL}.{path}"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    payload = json.dumps({"key": key, "record": record, "bound": bound}, separators=(",", ":"))
    try:
        os.write(fd, payload.encode())
    finally:
        os.close(fd)
    import subprocess

    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ | {"COST_ALERT_KEY": key, "COST_ALERT_JSON": payload},
        )
    except (OSError, ValueError):
        # the command could not start: free the day's stamp so a fixed command still posts
        with contextlib.suppress(OSError):
            (stamp_dir / f"{TOOL}.{path}").unlink()
        return False
    return True


def finish_launch_due(
    base: Path,
    probe: Probe,
    *,
    entries: int,
    launched: int,
    failed: int,
    finalised: int,
    lock_wait_s: float,
    lock_hold_s: float,
) -> dict | None:
    """Write the pass's cost line and raise the alert when it is over its bound.

    Never raises: measuring must not break the supervisor. Returns the record, or None
    when it could not be written."""
    path = "launch-due"
    try:
        stats = probe.stats() | {
            "lock_wait_s": round(lock_wait_s, 3),
            "lock_hold_s": round(lock_hold_s, 3),
        }
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "tool": TOOL,
            "path": f"{TOOL} {path}",
            **stats,
            "n": entries,
            "launched": launched,
            "failed": failed,
            "finalised": finalised,
        }
        over = [] if launched or failed else over_bounds(stats, LAUNCH_DUE_BOUNDS)
        if over:
            record["over"] = over
        append_line(base, record)
        if over:
            raise_alert(base, path, record, LAUNCH_DUE_BOUNDS)
        return record
    except (OSError, ValueError) as e:
        print(f"proc: cost record failed: {e}", file=sys.stderr)
        return None
