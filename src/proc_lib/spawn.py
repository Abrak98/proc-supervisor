# spec: specs/proc_lib/spawn.spec.md
"""proc engine — starting a detached job: wrapper script, spawn, schedule sidecars, env masking.

Shared by `start` (now) and the launcher (at the scheduled time), so a job behaves
identically however it was started.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import shlex
from pathlib import Path

from proc_lib.corelink import core
from proc_lib.procfs import _capture_starttime
from proc_lib.registry import (
    _RSS_NOTE_NO_FIGURE,
    _RSS_NOTE_NO_TIME,
    ENV_JOB_NAME,
    ENV_OWNER_PROJECT,
    ENV_OWNER_SESSION,
    RUNNING,
    _iso,
    _log_path,
    _rc_path,
    _rss_path,
    _schedenv_path,
)

TIMEOUT_BIN = "/usr/bin/timeout"


# `--env` keys whose value is masked before it is written to the registry. The
# registry is a plain world-readable JSON dir; the job itself gets the real value
# through Popen's env, so nothing downstream needs the plaintext on disk.
SECRET_KEY_MARKERS = ("TOKEN", "PASSWORD", "SECRET", "KEY")
MASKED_VALUE = "***"


@functools.lru_cache(maxsize=1)
def _gnu_time() -> str | None:
    """Path of GNU time (`/usr/bin/time`, 27 KB, about 1 MB resident per job) or None.
    Probed once per process: a non-GNU `time` (busybox) has no `-f`/`-o` and would put
    its usage message into the job's log."""
    import shutil
    import subprocess

    path = "/usr/bin/time" if os.access("/usr/bin/time", os.X_OK) else shutil.which("time")
    if not path:
        return None
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return path if "GNU" in (out.stdout + out.stderr) else None


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in SECRET_KEY_MARKERS)


def _parse_env(pairs: list[str]) -> dict[str, str]:
    """`KEY=VAL` strings -> ordered dict; raises ValueError naming the bad pair."""
    parsed: dict[str, str] = {}
    for pair in pairs:
        key, sep, val = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"--env expects KEY=VAL, got {pair!r}")
        if not (key[0].isalpha() or key[0] == "_") or not all(c.isalnum() or c == "_" for c in key):
            raise ValueError(f"--env key must match [A-Za-z_][A-Za-z0-9_]*, got {key!r}")
        parsed[key] = val
    return parsed


def _masked_env(job_env: dict[str, str]) -> dict[str, str]:
    return {k: (MASKED_VALUE if _is_secret_key(k) else v) for k, v in job_env.items()}


def _owner_from_env() -> dict[str, str]:
    """Who is starting a job, read from the caller's environment: the agent session
    and the project. Only variables that are set and non-empty are recorded; an
    empty dict means the caller declared no owner, and none is guessed."""
    owner = {}
    for key, var in (("session", ENV_OWNER_SESSION), ("project", ENV_OWNER_PROJECT)):
        if os.environ.get(var):
            owner[key] = os.environ[var]
    return owner


class _LaunchError(Exception):
    """A job could not be spawned; the message is operator-facing."""


def _write_schedenv(name: str, env: dict[str, str]) -> None:
    path = _schedenv_path(name)
    tmp = path.with_suffix(".schedenv.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(env, fh)
    os.replace(tmp, path)


def _read_schedenv(name: str) -> dict[str, str]:
    try:
        with open(_schedenv_path(name)) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise _LaunchError(f"scheduled environment of '{name}' is unreadable: {e}") from e


def _drop_schedenv(name: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        _schedenv_path(name).unlink()


def _run_spec(cmd, cwd, timeout, env, heavy, force_concurrent, at_ts) -> dict:
    """The part of a registry entry that describes one future run."""
    return {
        "cmd": list(cmd),
        "cwd": cwd,
        "timeout": timeout,
        "env": env,
        "heavy": heavy,
        "force_concurrent": force_concurrent,
        "scheduled_ts": at_ts,
        "scheduled_iso": _iso(at_ts),
    }


def _set_next_run(meta: dict, spec: dict, full_env: dict[str, str]) -> None:
    """Record, on a RUNNING job's own entry, the run that follows it. Caller holds
    the lock and saves."""
    meta["next_run"] = {"at_ts": spec["scheduled_ts"], "spec": spec}
    _write_schedenv(meta["name"], full_env)


def _scope_works() -> bool:
    """True when `systemd-run --user --scope` can start a command here. Probed once per
    process, only when a job is about to start, with a hard timeout: a launcher running
    where the user manager or bus is unreachable then starts jobs unscoped instead of
    losing them to a systemd-run that exits before running anything."""
    if core._SCOPE_OK is None:
        import subprocess

        try:
            r = subprocess.run(
                ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", "true"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            core._SCOPE_OK = r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            core._SCOPE_OK = False
    return core._SCOPE_OK


def _scope_prefix(name: str) -> list[str]:
    """`systemd-run` words that move the job into its own transient scope, or [] when
    the caller is not a systemd unit (or has no systemd-run). A job the launcher's
    unit starts would otherwise stay in that unit's cgroup, and systemd would log it
    as a left-over process every time the unit ends. systemd-run --scope execs the
    command in the same process, so the pid, starttime and session the registry
    records are the job's own."""
    import shutil

    if not os.environ.get("INVOCATION_ID") or not shutil.which("systemd-run"):
        return []
    if not _scope_works():
        return []
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:80]
    unit = f"proc-job-{safe}-{int(core._now() * 1000)}"
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}", "--"]


def _wrapper_script(
    quoted: str, rcp: Path, time_bin: str | None = None, rssp: Path | None = None
) -> str:
    """The bash payload around a job. After the job is reaped it writes the job's exit
    code (first line) and its cost into the `.rc` sidecar: `times` (the CPU of its
    reaped children), this shell's own /proc/<pid>/io, which the kernel has folded
    the reaped job's counters into, and the peak RSS. Bash cannot read the latter, so
    with `time_bin` (GNU time) the job runs under it, its figure goes to the side file
    `rssp` (never the job's log) and is folded in as `maxrss_kb`; the exit code is
    unchanged (`time` exits with the job's code, 128+n for a signal, as bash does).
    Builtins only, apart from `time` itself and one `rm` of the side file."""
    rc_file = shlex.quote(str(rcp))
    run = quoted
    fold = f"  echo {shlex.quote('maxrss_note ' + _RSS_NOTE_NO_TIME)}\n"
    if time_bin and rssp is not None:
        rss_file = shlex.quote(str(rssp))
        run = f"{shlex.quote(time_bin)} -f 'maxrss %M' -o {rss_file} {quoted}"
        fold = (
            "  mr=\n"
            f"  if [ -r {rss_file} ]; then\n"
            "    while IFS=' ' read -r k v; do\n"
            '      [ "$k" = maxrss ] && mr=$v\n'
            f"    done < {rss_file}\n"
            f"    rm -f {rss_file}\n"
            "  fi\n"
            '  case "$mr" in\n'
            '    ""|*[!0-9]*) echo ' + shlex.quote("maxrss_note " + _RSS_NOTE_NO_FIGURE) + " ;;\n"
            '    *) echo "maxrss_kb $mr" ;;\n'
            "  esac\n"
        )
    return (
        f"{run}\n"
        "rc=$?\n"
        '{ echo "$rc"; times\n'
        "  if [ -r /proc/$$/io ]; then\n"
        '    while IFS= read -r l; do echo "io $l"; done < /proc/$$/io\n'
        "  fi\n"
        f"{fold}"
        f"}} > {rc_file}\n"
        'exit "$rc"'
    )


def _spawn(name, cmd, cwd, timeout, heavy, full_env, recorded_env, scope=False) -> dict:
    """Start `cmd` detached and return its RUNNING registry entry (unsaved).

    Shared by `start` (now) and the launcher (at the scheduled time), so a job
    behaves identically however it was started. `scope` puts it in its own systemd
    scope when the caller runs inside a unit (the launcher does). Raises
    _LaunchError. Caller holds the registry lock and saves the entry."""
    # Inner command, optionally wrapped in coreutils `timeout` so the OS
    # enforces the cap: SIGTERM at the deadline, SIGKILL 10s later.
    # `timeout` exits 124 when it fires — that code flows out as the
    # inner status and is what lets us label the job `timeout` vs `exited`.
    import shutil
    import subprocess

    from proc_lib import heavy_job_lock

    inner = list(cmd)
    if timeout is not None:
        if not shutil.which(TIMEOUT_BIN) and not shutil.which("timeout"):
            raise _LaunchError("coreutils `timeout` not found; cannot honor --timeout")
        to_bin = TIMEOUT_BIN if os.path.exists(TIMEOUT_BIN) else "timeout"
        inner = [to_bin, "--signal=TERM", "--kill-after=10", str(timeout)] + inner

    # We launch detached and never become this PID's reaper across separate
    # proc invocations, so we can't waitpid for the real exit code. Instead
    # we wrap in a bash group-leader that writes the inner command's $? to a
    # .rc sidecar. The wrapper is the process-group leader, so killpg still
    # signals the whole tree (timeout + the real command); the wrapper then
    # records the code. `exec` is intentionally NOT used so the rc line runs.
    rcp = _rc_path(name)
    with contextlib.suppress(FileNotFoundError):
        rcp.unlink()
    # Quote inner argv safely for the bash -c payload.
    quoted = " ".join(shlex.quote(a) for a in inner)
    rssp = _rss_path(name)
    with contextlib.suppress(FileNotFoundError):
        rssp.unlink()
    script = _wrapper_script(quoted, rcp, core._gnu_time(), rssp)
    argv = [*(_scope_prefix(name) if scope else []), "bash", "-c", script]

    logp = _log_path(name)
    # Opened here, handed to the child, and closed below once it has the fd;
    # a `with` block can't span the Popen handoff, hence the explicit close.
    logf = open(logp, "ab")  # noqa: SIM115
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=full_env | {ENV_JOB_NAME: name},
        )
    except FileNotFoundError:
        raise _LaunchError(f"command not found: {argv[0]} (or cwd missing: {cwd})") from None
    except OSError as e:
        raise _LaunchError(f"failed to launch: {e}") from e
    finally:
        # parent doesn't need the fd; child inherited it
        with contextlib.suppress(OSError):
            logf.close()

    pid = proc.pid
    if heavy:
        heavy_job_lock.claim(name, pid, " ".join(cmd))
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        pgid = pid
    # Best-effort: a job living >~ a few ms always yields a starttime; this
    # hardens the PID-reuse guard against a transient /proc race at launch.
    starttime = _capture_starttime(pid)
    ts = core._now()
    meta = {
        "name": name,
        "cmd": list(cmd),
        "cwd": cwd,
        "pid": pid,
        "pgid": pgid,
        "start_ts": ts,
        "start_iso": _iso(ts),
        "pid_starttime": starttime,
        "timeout": timeout,
        "env": recorded_env,
        "status": RUNNING,
        "exit_code": None,
        "end_ts": None,
        "log_path": str(logp),
        "rc_path": str(rcp),
    }
    if heavy:  # recorded only for heavy jobs so a plain job keeps the long-standing key set
        meta["heavy"] = True
    return meta
