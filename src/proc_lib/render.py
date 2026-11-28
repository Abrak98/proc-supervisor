# spec: specs/proc_lib/render.spec.md
"""proc engine — text and JSON views of a job record (verdict line, status, list, cost)."""

from __future__ import annotations

import shlex
import sys
from datetime import UTC, datetime

from proc_lib.corelink import core
from proc_lib.registry import (
    _RSS_NOTE_OLD,
    EXITED,
    KILLED,
    OVERDUE_WARN_SECONDS,
    RUNNING,
    SCHEDULED,
    TERMINAL,
    TIMEOUT,
    _iso,
)


def _elapsed(meta: dict) -> str:
    secs = _elapsed_seconds(meta)
    if secs is None:
        return "-"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def _cmd_str(meta: dict) -> str:
    return " ".join(meta.get("cmd") or [])


def _elapsed_seconds(meta: dict) -> int | None:
    start = meta.get("start_ts")
    if start is None:
        return None
    end = meta.get("end_ts") if meta.get("status") in TERMINAL else None
    return max(0, int((end if end else core._now()) - start))


def _human_duration(secs: int) -> str:
    """Two most significant units, unpadded: 12s, 41m5s, 2h13m, 3d4h."""
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


def _verdict(meta: dict) -> str:
    """One-line state summary, always the FIRST line of human status/wait output
    so a caller that truncates (`| head -1`) still gets the fact it needed."""
    name = meta.get("name", "?")
    status = meta.get("status", "?")
    if status == SCHEDULED:
        return f"{name}: scheduled for {_iso_short(meta['scheduled_ts'])} ({_until(meta)})"
    if status == KILLED and meta.get("start_ts") is None:
        return f"{name}: cancelled before start"
    secs = _elapsed_seconds(meta)
    dur = "?" if secs is None else _human_duration(secs)
    if status == RUNNING:
        return f"{name}: running pid={meta.get('pid')} for {dur}"
    rc = meta.get("exit_code")
    rc_s = "?" if rc is None else str(rc)
    if status == EXITED:
        return f"{name}: exited rc={rc_s} after {dur}"
    if status == TIMEOUT:
        return f"{name}: killed (timeout) after {dur}"
    if status == KILLED:
        return f"{name}: killed (proc kill) after {dur}"
    return f"{name}: {status} after {dur}"


def _iso_short(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _until(meta: dict) -> str:
    """`in 9h26m` before a scheduled start, `overdue 3m5s` after it."""
    delta = int(meta["scheduled_ts"] - core._now())
    return f"in {_human_duration(delta)}" if delta >= 0 else f"overdue {_human_duration(-delta)}"


def _job_succeeded(meta: dict) -> bool:
    return meta.get("status") == EXITED and meta.get("exit_code") == 0


def _status_json(meta: dict) -> dict:
    """Machine view of a reconciled job: state/rc/alive first, then every stored
    registry field unchanged. `alive` is derived — after _reconcile a job is
    RUNNING iff its PID-reuse-guarded process is still alive."""
    head = {
        "name": meta.get("name"),
        "status": meta.get("status"),
        "alive": meta.get("status") == RUNNING,
        "exit_code": meta.get("exit_code"),
        "pid": meta.get("pid"),
        "elapsed_s": _elapsed_seconds(meta),
        "log_path": meta.get("log_path"),
    }
    return head | {k: v for k, v in meta.items() if k not in head}


def _list_json(meta: dict) -> dict:
    """What `list --json` shows by default: what a caller filters on and displays, never
    the command texts, environment or paths (those are `--full`)."""
    row = {
        "name": meta.get("name"),
        "status": meta.get("status"),
        "alive": meta.get("status") == RUNNING,
        "exit_code": meta.get("exit_code"),
        "pid": meta.get("pid"),
        "pid_starttime": meta.get("pid_starttime"),
        "owner": meta.get("owner") or {},
        "start_ts": meta.get("start_ts"),
        "elapsed_s": _elapsed_seconds(meta),
    }
    if meta.get("status") == SCHEDULED:
        row["scheduled_ts"] = meta.get("scheduled_ts")
    return row


def _status_lines(meta: dict) -> list[str]:
    lines = [_verdict(meta), f"  log: {meta.get('log_path')}"]
    lines.append(f"  cmd: {shlex.join(meta.get('cmd') or [])}")
    lines.append(f"  cwd: {meta.get('cwd')}")
    if (
        meta.get("status") == SCHEDULED
        and meta["scheduled_ts"] < core._now() - OVERDUE_WARN_SECONDS
    ):
        lines.append("  overdue: the launcher has not started it; check `proc launcher status`")
    if meta.get("owner"):
        lines.append("  owner: " + " ".join(f"{k}={v}" for k, v in meta["owner"].items()))
    if meta.get("start_iso"):
        lines.append(f"  started: {meta['start_iso']}")
    if meta.get("next_run"):
        lines.append(f"  next run: {_iso_short(meta['next_run']['at_ts'])}")
    if meta.get("end_ts") is not None:
        lines.append(f"  ended: {_iso(meta['end_ts'])}")
    to = meta.get("timeout")
    lines.append(f"  timeout: {'none' if to is None else f'{to}s'}")
    job_env = meta.get("env") or {}
    if job_env:
        lines.append("  env: " + " ".join(f"{k}={v}" for k, v in job_env.items()))
    lines.append(f"  status: {meta.get('status')}  exit_code: {meta.get('exit_code')}")
    if meta.get("cost"):
        lines.append("  cost: " + _cost_text(meta["cost"]))
    return lines


def _cost_text(c: dict) -> str:
    parts = []
    if "cpu_user_s" in c:
        parts.append(f"cpu user {c['cpu_user_s']}s sys {c.get('cpu_sys_s', 0)}s")
    for label, key in (
        ("rchar", "rchar"),
        ("read from disk", "read_bytes"),
        ("wchar", "wchar"),
        ("written to disk", "write_bytes"),
    ):
        if key in c:
            parts.append(f"{label} {c[key] / 1048576:.1f} MB")
    if "maxrss_kb" in c:
        parts.append(f"peak RSS {c['maxrss_kb'] / 1024:.1f} MB")
    else:
        parts.append(f"peak RSS not recorded ({c.get('maxrss_note') or _RSS_NOTE_OLD})")
    return ", ".join(parts)


def _err(msg: str) -> None:
    print(f"proc: {msg}", file=sys.stderr)
