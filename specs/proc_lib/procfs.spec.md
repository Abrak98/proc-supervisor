# procfs — spec

> Permanent contract and source of truth. Edit THIS file directly on any change.
> Every source `.py` implementing it carries `# spec: <path-to-this-file>`.

## Intent
Read `/proc` and signal processes safely: the PID-reuse guard (`_alive`), the session scan, and the only code that sends a signal. Part of the engine whose contract is `specs/proc_lib/core.spec.md`; split out of `core` so the safety-critical signaling code is one small module.

## Rationale / Key decisions
- **Identity is `/proc` starttime, checked again right before every signal.** `_alive` fails closed when no starttime was captured; `_signal_member` pins the process with a pidfd where the kernel has one, re-reads starttime and session and signals through the pidfd, else through a verified `os.kill`. Unchanged from the engine's contract (`core.spec.md`, Invariants).
- **Session, not process group.** `timeout` moves the real command into its own group, so the session id (inherited by every descendant, not changed by reparenting) is what finds the whole job.
- **Replaceable names are read through `core`.** `_session_scan` is called as `core._session_scan` so a replacement set on `proc_lib.core` reaches `_session_members` and `_signal_session`.

## Rejected alternatives
- **Signaling by a bare pid or a `--pid` flag.** Rejected: `proc` signals only what it launched (`CLAUDE.md`, hard invariant 1).

## Acceptance criteria
- GIVEN a reused, mismatched or zombie pid WHEN `_alive` or `_signal_session` runs THEN nothing is signaled (`test_safety.py`, `test_kill.py`).
- GIVEN a session member that exited and whose pid was reused after the scan WHEN `_signal_session` runs THEN it is left alone (`test_kill.py`).

## State
None; reads `/proc/<pid>/stat` only.

## Methods
- `_read_stat(pid)`, `_read_starttime(pid)`, `_capture_starttime(pid)`, `_alive(pid, starttime) -> bool` — liveness with the PID-reuse guard.
- `_read_member(pid)`, `_session_scan(sid)`, `_session_members(sid)`, `_session_alive(sid)` — the live members of a session.
- `_signal_member(pid, starttime, sid, sig) -> bool`, `_signal_session(sid, sig) -> int` — the verified signal; the count of processes signaled.

## Invariants
- Never signals a process that is not a recorded, re-verified member of the session asked for.
- Standard library only; imports nothing from the package at load time.

## Formulas
n/a

## I/O contracts
Those of `specs/proc_lib/core.spec.md`; nothing is added or changed by the split.

## Names
| canonical | kind | forbidden-aliases |
|-----------|------|-------------------|
| _alive | function | |
| _signal_session | function | |

## Dependencies
- Python standard library; `proc_lib.corelink` for the replaceable `_session_scan`.

## Test cases
- GIVEN a pid whose starttime differs from the stored one THEN `_alive` is false (`test_safety.py`).
- GIVEN a zombie THEN it is not alive and not signaled (`test_safety.py`).
- GIVEN a member whose starttime changes after the scan THEN no signal is sent (`test_kill.py`).
