"""Shared fixtures. Every test runs against an isolated registry under a tmp
dir so no real ~/.local/state/proc/ job is ever touched."""

from __future__ import annotations

import argparse

import pytest

from proc_lib import core, hook_judge, launcher

REAL_ENSURE = launcher.ensure  # captured before any test stubs it
REAL_ENSURE_HOOK = hook_judge.ensure


@pytest.fixture(autouse=True)
def isolated_base(tmp_path, monkeypatch):
    """Point the engine's BASE at a fresh tmp dir for the duration of a test.

    All path helpers read core.BASE at call time, so this fully isolates the
    registry, logs, .rc sidecars and lockfile."""
    base = tmp_path / "proc"
    base.mkdir()
    monkeypatch.setattr(core, "BASE", base)
    return base


@pytest.fixture(autouse=True)
def stub_launcher(monkeypatch):
    """`start --at` installs the real launcher (systemd/cron of the machine running
    the tests); no test may touch that."""
    monkeypatch.setattr(launcher, "ensure", lambda base: launcher.Status("systemd", True, "stub"))


@pytest.fixture(autouse=True)
def stub_claude_hook(monkeypatch):
    """`start` registers the Claude Code hook in the user's settings; no test may touch
    the real ones."""
    monkeypatch.setattr(hook_judge, "ensure", lambda base, env=None: None)


@pytest.fixture(autouse=True)
def no_owner_env(monkeypatch):
    """The suite may itself run inside an agent session; a job's `owner` must come
    only from what a test sets."""
    monkeypatch.delenv(core.ENV_OWNER_SESSION, raising=False)
    monkeypatch.delenv(core.ENV_OWNER_PROJECT, raising=False)


def ns(**kw) -> argparse.Namespace:
    """Build an argparse.Namespace with the given attributes (engine inputs)."""
    return argparse.Namespace(**kw)


def start(name, *cmd, timeout=None, cwd="/tmp", env=(), at=None) -> int:
    """Convenience: launch (or, with `at`, schedule) a job via the real cmd_start."""
    return core.cmd_start(
        ns(name=name, timeout=timeout, cwd=cwd, env=list(env), at=at, cmd=["--", *cmd])
    )


def wait_terminal(name, deadline_s=10.0) -> dict:
    """Poll reconcile until the job is terminal or the deadline passes; return
    the final meta. Uses real wall-clock but with short commands this is fast."""
    import time

    end = time.time() + deadline_s
    while time.time() < end:
        m = core._with_lock(lambda: core._reconcile_and_save(name))
        if m is None or m.get("status") in core.TERMINAL:
            return m
        time.sleep(0.05)
    return core._load(name)


def reap_terminal(name) -> dict:
    """Wait for a job the test itself started by reaping its wrapper (the test process is
    its parent), then reconcile once. Unlike `wait_terminal` this has no deadline to miss
    under load: waitpid returns exactly when the wrapper has exited, and the `.rc` it wrote
    before exiting is then in place. Only for a job that is certain to end by itself."""
    import contextlib
    import os

    pid = core._load(name)["pid"]
    with contextlib.suppress(ChildProcessError):  # already reaped by subprocess' own cleanup
        os.waitpid(pid, 0)
    return core._with_lock(lambda: core._reconcile_and_save(name))
