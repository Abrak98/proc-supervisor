# spec: specs/proc_lib/procfs.spec.md
"""proc engine — /proc reading, the PID-reuse guard, session scan and signaling.

Every signal `proc` sends goes through this module: only to a process the registry
recorded, only after its `/proc` starttime (and, for session members, session id) is
re-checked just before the signal. See specs/proc_lib/core.spec.md for the contract.
"""

from __future__ import annotations

import errno
import os
import signal
import time

from proc_lib.corelink import core

# Process states (/proc/<pid>/stat field 3) that mean the process has ended but
# may not be reaped yet. A zombie still has a /proc entry and answers
# os.kill(pid, 0), so we must treat it as NOT alive — otherwise an exited job
# whose parent hasn't reaped it (e.g. proc running in-process) reads as running
# forever. In production `proc start` exits immediately, so the wrapper is
# reparented to init and reaped; this only matters in the brief pre-reap window
# and for in-process use, but skipping Z/X is strictly more correct.
_DEAD_STATES = {"Z", "X", "x"}


def _read_stat(pid: int | None) -> tuple[int | None, str | None]:
    """Return (starttime, state) from /proc/<pid>/stat, or (None, None) if gone.

    starttime is field 22 (clock ticks since boot); state is field 3. The comm
    field (field 2) is wrapped in parentheses and may itself contain spaces or
    ')', so we split on the LAST ')' and index the remaining space-separated
    fields: state is index 0 of the tail, starttime is field 22 -> tail index 19.
    The starttime is the PID-reuse guard: a recycled PID gets a different
    boot-relative starttime, so a mismatch means our process is gone even though
    some process now owns the PID."""
    if pid is None:
        return (None, None)
    try:
        with open(f"/proc/{pid}/stat") as fh:
            data = fh.read()
    except (FileNotFoundError, ProcessLookupError, OSError):
        return (None, None)
    rparen = data.rfind(")")
    if rparen == -1:
        return (None, None)
    tail = data[rparen + 2 :].split()
    state = tail[0] if tail else None
    try:
        return (int(tail[19]), state)
    except (IndexError, ValueError):
        return (None, state)


def _read_starttime(pid: int | None) -> int | None:
    """starttime field of /proc/<pid>/stat, or None if the process is gone."""
    return _read_stat(pid)[0]


def _capture_starttime(pid: int, attempts: int = 6, delay: float = 0.01):
    """Best-effort read of a freshly launched pid's starttime.

    A job that lives more than a few ms always yields a value; we retry briefly
    so a transient /proc read race at launch doesn't leave starttime=None on a
    process that is actually alive (which would defeat the PID-reuse guard —
    see _alive). A job that exits faster than this window legitimately returns
    None and is finalized via its .rc sidecar on the next reconcile."""
    for _ in range(attempts):
        st = _read_starttime(pid)
        if st is not None:
            return st
        time.sleep(delay)
    return None


def _alive(pid: int | None, starttime: int | None) -> bool:
    """True iff `pid` is alive AND its /proc starttime matches the stored one.

    PID-reuse guard. If we never captured a starttime (`starttime is None`) we
    deliberately return False: we cannot prove the live PID is *our* process, so
    for signaling safety we treat it as not-ours and let reconcile finalize the
    job from its .rc sidecar. In practice starttime is None only for a job that
    exited within milliseconds of launch (see _capture_starttime), which is
    genuinely terminal, so this is also correct for liveness."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # exists but owned by someone else — confirm identity via starttime below
        pass
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
    cur, state = _read_stat(pid)
    if cur is None:
        return False
    if state in _DEAD_STATES:
        # zombie/dead: the process has exited, it just hasn't been reaped yet
        return False
    if starttime is None:
        # can't confirm this live PID is ours -> not-our-process (safe default)
        return False
    return cur == starttime


def _read_member(pid: int) -> tuple[int, int, str] | None:
    """(starttime, session id, state) of `pid` from /proc/<pid>/stat, or None when it is
    gone or unreadable. Split on the LAST ')' as `_read_stat` does."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            data = fh.read()
        # fields after the "(comm)" token: state ppid pgrp session ... starttime (index 19)
        after = data.rsplit(")", 1)[1].split()
        return (int(after[19]), int(after[3]), after[0])
    except (OSError, ValueError, IndexError):
        return None


def _session_scan(sid: int | None) -> list[tuple[int, int]]:
    """(pid, starttime) of every live member of the session led by `sid`, by a /proc scan.
    The starttime lets `core._signal_session` prove, just before it signals, that the pid still
    names the process the scan saw.

    Why session, not process-group: we launch the job with
    start_new_session=True, so the bash wrapper becomes the session leader and
    its pid == sid. coreutils `timeout` then puts the real command into its OWN
    process group (setpgid) — so the recorded pgid (the wrapper's) goes empty
    the instant the wrapper dies, while timeout+worker live on in a different
    group that killpg(recorded_pgid) never reaches. A descendant PPID-walk
    doesn't help either: when the wrapper dies the worker is reparented to init
    and the tree breaks. The session id, by contrast, is inherited by every
    descendant, is NOT changed by reparenting, and is NOT forked off by
    `timeout`. So scanning /proc for session==sid reliably finds the whole job
    (wrapper + timeout + worker), which is what kill/escalation must signal."""
    if not sid or sid <= 0:
        return []
    members = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        m = _read_member(int(entry))
        if m is None or m[2] in _DEAD_STATES:
            continue  # gone, or zombie/dead: exited, don't count as a live member
        if m[1] == sid:
            members.append((int(entry), m[0]))
    return members


def _session_members(sid: int | None) -> list[int]:
    """All live PIDs belonging to the session led by `sid` (see `core._session_scan`)."""
    return [pid for pid, _ in core._session_scan(sid)]


def _session_alive(sid: int | None) -> bool:
    """True if any process still belongs to the session led by `sid`."""
    return bool(_session_members(sid))


def _signal_member(pid: int, starttime: int, sid: int, sig: int) -> bool:
    """Send `sig` to `pid` only if it is still the member the scan saw: same starttime,
    still in session `sid`, not dead. A pid freed and reused between the scan and this
    call is therefore never signaled. Where the kernel has pidfds the process is pinned
    first (`pidfd_open`), identity is checked on the pinned process and the signal goes
    through the pidfd, so no reuse can slip in between the check and the signal; without
    pidfd support the check is followed by a plain `os.kill`, a window of microseconds
    instead of the scan's duration. Returns whether a signal was sent."""
    fd = None
    try:
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            return False
        except (AttributeError, OSError):
            fd = None  # no pidfd support here: verified os.kill below
        cur = _read_member(pid)
        if cur is None or cur[0] != starttime or cur[1] != sid or cur[2] in _DEAD_STATES:
            return False
        if fd is not None:
            signal.pidfd_send_signal(fd, sig)
        else:
            os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    finally:
        if fd is not None:
            os.close(fd)


def _signal_session(sid: int | None, sig: int) -> int:
    """Send `sig` to every live member of the session led by `sid`.

    Signals each pid individually rather than killpg, because `timeout` forks
    the worker into a separate process group — one killpg can't reach every
    member. Each member is re-verified right before its signal (`_signal_member`), so a
    process that exited after the scan and whose pid was reused is left alone. Returns
    the number of processes signaled."""
    if not sid or sid <= 0:
        return 0
    n = 0
    for pid, starttime in core._session_scan(sid):
        if _signal_member(pid, starttime, sid, sig):
            n += 1
    return n
