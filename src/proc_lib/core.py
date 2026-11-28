# spec: specs/proc_lib/core.spec.md
"""proc engine — the public face of the registry, process/signal and command modules.

State lives under ``$PROC_HOME`` (default ``~/.local/state/proc``). The base dir is
read once at import; tests isolate it by setting ``PROC_HOME`` before import or
by monkeypatching :data:`BASE`. Every path helper reads :data:`BASE` at call
time, so a monkeypatch takes effect immediately.

Exit codes:
  0  success / terminal reached
  2  usage error or conflict (e.g. duplicate running name, rm on running job)
  3  wait/follow timed out while job still running
  4  named job not found in registry
  5  wait: job reached a terminal status other than `exited` with rc 0
  6  no launcher backend (systemd user timer / cron) can fire scheduled jobs

stdlib-only by design: a process supervisor must not depend on anything that
could itself break the very runs it manages. Modules only some commands need
(subprocess, shutil, launcher, claude_hook, hook_judge, heavy_job_lock) are imported inside the
functions that use them: the minute-by-minute `launch-due` pass reads their bytecode
only when it actually starts a job.

The engine is split by responsibility; this module re-exports every name of the pieces, so
``core.<name>`` keeps working for the CLI, the launcher and the tests:

  procfs     /proc reading, the PID-reuse guard, session scan and signaling
  registry   records, history, live index, registry lock, reconcile
  render     status/list text and JSON, durations, the error line
  spawn      wrapper script, start of a detached job, schedule sidecars, env masking
  commands   the ``cmd_*`` subcommand implementations

The pieces read the names a caller may replace (``BASE``, ``_now``, ``_with_lock``,
``_signal_session``, ``_session_scan``, ``_index_set``, ``_gnu_time``, ``_SCOPE_OK``,
``KILL_GRACE_SECONDS``, ``KILL_DRAIN_POLLS``) through this module at call time
(:mod:`proc_lib.corelink`), so a monkeypatch here takes effect in all of them.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# The pieces import this module's names back at call time (see the docstring), so these
# re-exports are the module's interface, private helpers included.
# ruff: noqa: F401, E402

KILL_GRACE_SECONDS = 10.0  # grace between SIGTERM and the escalation SIGKILL on `proc kill`
KILL_DRAIN_POLLS = 50  # SIGKILL re-send polls of 0.1s (~5s ceiling)
_SCOPE_OK: bool | None = None  # outcome of the first `systemd-run --scope` probe in this process


def _default_base() -> Path:
    return Path(os.environ.get("PROC_HOME") or os.path.expanduser("~/.local/state/proc"))


# Resolved once at import. Overridable via PROC_HOME (isolation/tests) or by
# monkeypatching this module attribute; all path helpers read it at call time.
BASE = _default_base()


def _now() -> float:
    return time.time()


from proc_lib.commands import (
    _LINE_TIME,
    _ensure_claude_hook,
    _ensure_launcher,
    _entry_meta,
    _exited_since,
    _job_files,
    _launch_one,
    _line_time,
    _list_exited,
    _list_source,
    _note_session,
    _parse_at,
    _parse_zoned,
    _rm_one,
    _settle,
    _since_filter,
    cmd_archive,
    cmd_clean,
    cmd_hook,
    cmd_kill,
    cmd_launch_due,
    cmd_launcher,
    cmd_list,
    cmd_logs,
    cmd_reindex,
    cmd_reschedule,
    cmd_rm,
    cmd_start,
    cmd_status,
    cmd_wait,
)
from proc_lib.procfs import (
    _DEAD_STATES,
    _alive,
    _capture_starttime,
    _read_member,
    _read_starttime,
    _read_stat,
    _session_alive,
    _session_members,
    _session_scan,
    _signal_member,
    _signal_session,
)
from proc_lib.registry import (
    _DAY_NAME,
    _IO_KEYS,
    _LOCK_SECONDS,
    _RSS_NOTE_NO_FIGURE,
    _RSS_NOTE_NO_TIME,
    _RSS_NOTE_OLD,
    _TIMES_LINE,
    ENV_JOB_NAME,
    ENV_OWNER_PROJECT,
    ENV_OWNER_SESSION,
    EXIT_HOOK_MISSING,
    EXIT_JOB_FAILED,
    EXIT_KILL_SURVIVORS,
    EXIT_LAUNCHER,
    EXIT_NOTFOUND,
    EXIT_USAGE,
    EXIT_WAIT_TIMEOUT,
    EXITED,
    FOLLOW_HARD_CAP_SECONDS,
    HISTORY_DAYS_CONFIG,
    HISTORY_DAYS_DEFAULT,
    HISTORY_DAYS_ENV,
    HISTORY_DIR,
    HISTORY_STAMP,
    INDEX_VERSION,
    KILLED,
    LIVE,
    LOST,
    OVERDUE_WARN_SECONDS,
    RUNNING,
    SCHEDULED,
    TERMINAL,
    TIMEOUT,
    _all_metas,
    _apply_next_run,
    _archive,
    _current,
    _day,
    _ensure_base,
    _history_days,
    _history_days_bound,
    _history_record,
    _history_root,
    _index_locked,
    _index_path,
    _index_set,
    _iso,
    _live_entry,
    _live_index,
    _load,
    _lockfile,
    _log_path,
    _meta_path,
    _move_ended_before,
    _parse_rc,
    _rc_path,
    _read_index,
    _read_json,
    _read_rc,
    _read_rc_file,
    _rebuild_index,
    _reconcile,
    _reconcile_and_save,
    _rss_path,
    _safe_session,
    _save,
    _schedenv_path,
    _seconds,
    _session_add,
    _session_names,
    _sessions_dir,
    _with_lock,
    _write_index,
    _write_session_file,
    housekeeping,
)
from proc_lib.render import (
    _cmd_str,
    _cost_text,
    _elapsed,
    _elapsed_seconds,
    _err,
    _human_duration,
    _iso_short,
    _job_succeeded,
    _list_json,
    _status_json,
    _status_lines,
    _until,
    _verdict,
)
from proc_lib.spawn import (
    MASKED_VALUE,
    SECRET_KEY_MARKERS,
    TIMEOUT_BIN,
    _drop_schedenv,
    _gnu_time,
    _is_secret_key,
    _LaunchError,
    _masked_env,
    _owner_from_env,
    _parse_env,
    _read_schedenv,
    _run_spec,
    _scope_prefix,
    _scope_works,
    _set_next_run,
    _spawn,
    _wrapper_script,
    _write_schedenv,
)

__all__ = [
    "BASE",
    "ENV_JOB_NAME",
    "ENV_OWNER_PROJECT",
    "ENV_OWNER_SESSION",
    "EXITED",
    "EXIT_HOOK_MISSING",
    "EXIT_JOB_FAILED",
    "EXIT_KILL_SURVIVORS",
    "EXIT_LAUNCHER",
    "EXIT_NOTFOUND",
    "EXIT_USAGE",
    "EXIT_WAIT_TIMEOUT",
    "FOLLOW_HARD_CAP_SECONDS",
    "HISTORY_DAYS_CONFIG",
    "HISTORY_DAYS_DEFAULT",
    "HISTORY_DAYS_ENV",
    "HISTORY_DIR",
    "HISTORY_STAMP",
    "INDEX_VERSION",
    "KILLED",
    "KILL_DRAIN_POLLS",
    "KILL_GRACE_SECONDS",
    "LIVE",
    "LOST",
    "MASKED_VALUE",
    "OVERDUE_WARN_SECONDS",
    "RUNNING",
    "SCHEDULED",
    "SECRET_KEY_MARKERS",
    "TERMINAL",
    "TIMEOUT",
    "TIMEOUT_BIN",
    "cmd_archive",
    "cmd_clean",
    "cmd_hook",
    "cmd_kill",
    "cmd_launch_due",
    "cmd_launcher",
    "cmd_list",
    "cmd_logs",
    "cmd_reindex",
    "cmd_reschedule",
    "cmd_rm",
    "cmd_start",
    "cmd_status",
    "cmd_wait",
    "housekeeping",
]
