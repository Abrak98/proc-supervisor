"""Safety invariants: PID-reuse guard, registry integrity under concurrency,
and clean's age filter."""

from __future__ import annotations

import os
import threading
import time

from proc_lib import core

from .conftest import ns, start, wait_terminal


def test_alive_true_for_self_with_matching_starttime():
    pid = os.getpid()
    st = core._read_starttime(pid)
    assert st is not None
    assert core._alive(pid, st) is True


def test_alive_false_on_starttime_mismatch():
    """PID-reuse guard: same live PID, wrong starttime -> not our process."""
    pid = os.getpid()
    assert core._alive(pid, 999_999_999) is False


def test_alive_false_when_starttime_unknown():
    """If we never captured a starttime we cannot confirm identity -> False.
    Closes the gap where a reused PID with stored None would read as alive."""
    pid = os.getpid()
    assert core._alive(pid, None) is False


def test_alive_false_for_dead_pid():
    # 2^31-ish pid that won't exist
    assert core._alive(2_000_000_000, 123) is False
    assert core._alive(None, None) is False


def test_kill_does_not_signal_reused_pid():
    """If the recorded process is gone (starttime won't match), kill must NOT
    signal — it reconciles to terminal instead."""
    start("ph", "bash", "-c", "exit 0")
    _ = wait_terminal("ph")
    m = core._load("ph")
    # Corrupt the registry so the PID looks 'running' but starttime is bogus,
    # mimicking a recycled PID now owned by something else.
    m["status"] = core.RUNNING
    m["pid_starttime"] = 1  # guaranteed mismatch
    m["end_ts"] = None
    core._save(m)
    rc = core.cmd_kill(ns(name="ph", force=False))
    assert rc == 0
    assert core._load("ph")["status"] in core.TERMINAL
    assert core._load("ph")["status"] != "killed"  # never signaled


def test_concurrent_starts_do_not_corrupt_registry():
    """20 parallel launches must each persist a readable JSON (flock works)."""
    names = [f"c{i}" for i in range(20)]

    def launch(n):
        start(n, "bash", "-c", "exit 0")

    threads = [threading.Thread(target=launch, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    metas = core._all_metas()
    got = {m["name"] for m in metas}
    assert got == set(names)  # all present, all valid JSON (none corrupted)
    for n in names:
        wait_terminal(n)


def test_clean_age_filter():
    start("old", "bash", "-c", "exit 0")
    start("recent", "bash", "-c", "exit 0")
    wait_terminal("old")
    wait_terminal("recent")
    # backdate 'old' two days
    m = core._load("old")
    m["end_ts"] = time.time() - 2 * 86400
    core._save(m)
    rc = core.cmd_clean(ns(older_than=24))
    assert rc == 0
    assert core._load("old") is None  # removed (older than 24h)
    assert core._load("recent") is not None  # kept (fresh)


def test_clean_keeps_running_jobs():
    start("alive", "bash", "-c", "sleep 30")
    core.cmd_clean(ns(older_than=0))  # 0h cutoff: everything terminal goes
    assert core._load("alive") is not None  # running -> never cleaned
    core.cmd_kill(ns(name="alive", force=True))


# --- per-member identity check just before a session signal ------------------- #


def _own_session_sleeper():
    """A real process that leads its own session (sid == pid), the shape of a job."""
    import subprocess

    p = subprocess.Popen(["sleep", "30"], start_new_session=True)
    for _ in range(200):
        if core._read_member(p.pid) is not None:
            break
        time.sleep(0.005)
    return p


def test_signal_member_skips_a_pid_whose_starttime_changed():
    """A pid freed and reused after the scan has another starttime: not signaled."""
    p = _own_session_sleeper()
    try:
        st, sid, _ = core._read_member(p.pid)
        assert core._signal_member(p.pid, st + 1, sid, 15) is False
        time.sleep(0.05)
        assert p.poll() is None  # untouched
        assert core._signal_member(p.pid, st, sid, 15) is True
        assert p.wait(timeout=10) == -15
    finally:
        p.kill()
        p.wait()


def test_signal_member_skips_a_pid_that_left_the_session():
    p = _own_session_sleeper()
    try:
        st, sid, _ = core._read_member(p.pid)
        assert core._signal_member(p.pid, st, sid + 1, 15) is False
        time.sleep(0.05)
        assert p.poll() is None
    finally:
        p.kill()
        p.wait()


def test_signal_member_skips_a_zombie():
    import subprocess

    p = subprocess.Popen(["true"], start_new_session=True)
    for _ in range(400):  # exited but not reaped: a zombie
        m = core._read_member(p.pid)
        if m and m[2] == "Z":
            break
        time.sleep(0.005)
    try:
        m = core._read_member(p.pid)
        assert m and m[2] == "Z"
        assert core._signal_member(p.pid, m[0], m[1], 15) is False
    finally:
        p.wait()


def test_signal_session_does_not_signal_a_member_that_the_scan_mislabels(monkeypatch):
    """The scan saw the pid with a starttime it no longer has (reuse after the scan):
    the session signal leaves it alone and counts nothing."""
    p = _own_session_sleeper()
    try:
        st, sid, _ = core._read_member(p.pid)
        monkeypatch.setattr(core, "_session_scan", lambda s: [(p.pid, st + 7)])
        assert core._signal_session(sid, 15) == 0
        time.sleep(0.05)
        assert p.poll() is None
        monkeypatch.undo()
        assert core._signal_session(sid, 15) == 1
        assert p.wait(timeout=10) == -15
    finally:
        p.kill()
        p.wait()


def test_signal_member_without_pidfd_support_still_verifies(monkeypatch):
    def no_pidfd(pid):
        raise OSError(38, "ENOSYS")

    monkeypatch.setattr(core.os, "pidfd_open", no_pidfd)
    p = _own_session_sleeper()
    try:
        st, sid, _ = core._read_member(p.pid)
        assert core._signal_member(p.pid, st + 1, sid, 15) is False
        assert p.poll() is None
        assert core._signal_member(p.pid, st, sid, 15) is True
        assert p.wait(timeout=10) == -15
    finally:
        p.kill()
        p.wait()
