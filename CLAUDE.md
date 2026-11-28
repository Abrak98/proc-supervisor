# proc-lib — Operating Principles

`proc` is a process supervisor for autonomous agents. Its correctness is
safety-critical: it sends signals. Treat changes here with the care that
implies.

## Hard invariants — never regress these

1. **Never signal a process `proc` did not launch.** No `--pid` flag, ever.
   Signaling is registry-only, PID-reuse-guarded (`/proc` starttime), and
   session-scoped. Any change to `_alive`, `_session_members`, `_signal_session`,
   or `cmd_kill` must preserve this and ship with a test proving a
   reused/mismatched/zombie PID is not signaled.
2. **Never hang.** `start` returns immediately; `wait` and `logs --follow` have
   hard caps. Do not add a code path that can block unbounded.
3. **stdlib-only at runtime.** Zero runtime dependencies. A supervisor must not
   depend on anything that could break the runs it manages. `dev` deps (pytest,
   ruff) are fine; runtime deps are not.

## Engineering rules

- The engine lives in `src/proc_lib/` (`core.py` re-exports it; `registry.py`, `procfs.py`,
  `spawn.py`, `render.py`, `commands.py`); the CLI front end in `src/proc_lib/cli.py`. Keep
  `cli.py` thin.
- Every behavioural change ships with a test. The suite uses real short-lived
  processes against an isolated tmp `PROC_HOME` and runs in seconds — keep it
  that way (no multi-second sleeps; use `KILL_GRACE_SECONDS` monkeypatch for the
  escalation test).
- A change to a subcommand, an option, an exit code or a file under `$PROC_HOME` updates
  README.md and CLI_REFERENCE.md in the same commit; `tests/test_docs.py` fails when a command,
  option or exit code the code defines is missing from them.
- Gate before commit: `uv run ruff check src/ tests/`, `uv run ruff format src/ tests/`,
  `uv run pytest -q`. All green.
- State dir is `$PROC_HOME` (default `~/.local/state/proc/`). Never change the
  default without a migration — existing registries and other tools depend on it.

## Public repository

A change reaches the public repository only through `scripts/export_public.sh` and a pull
request that passes the required checks: [docs/public-release.md](docs/public-release.md). Never
push the private history or a private branch to the public remote.

## Language

Code, comments, docstrings, commits, and this repo's docs are in English.
