# cli — spec

> Permanent contract and source of truth. Edit THIS file directly on any change.
> Every source `.py` implementing it carries `# spec: <path-to-this-file>`.

## Intent
The `proc` command-line front end. Defines the subcommand grammar (`start`, `list`/`ls`,
`status`, `logs`, `wait`, `kill`, `rm`, `clean`, `reschedule`, `launch-due`, `launcher`, `hook`,
`reindex`, `archive`) and dispatches each straight to its
`core.cmd_*` implementation. Exists as the single entry point (`proc = proc_lib.cli:main`)
so `core.py`'s functions stay callable in-process without dragging argparse along.

## Rationale / Key decisions
- **Thin dispatch, no logic duplication.** Every subparser's `set_defaults(func=...)` points
  directly at a `core.cmd_*` function; `cli.py` never re-implements a decision `core.py`
  already makes. Keeps the engine testable without spawning a subprocess and keeps the CLI
  grammar reviewable in one place.
- **`argparse.REMAINDER` for the launched command.** `start`'s positional `cmd` captures
  everything after `--` verbatim (including the user's own `--flags`), rather than trying to
  reparse it — the launched command's argument shape is opaque to `proc` by design.
- **One fast path, `launch-due`.** `main(["launch-due"])` calls the engine without building the
  parser: the minute timer runs it 1,440 times a day and argparse's bytecode is a tenth of what
  a pass otherwise reads. Any other argv, `launch-due --help` included, goes through the full
  parser, which still defines the command.
- **Top-level exception handling in `main()`, not per-subcommand.** `KeyboardInterrupt` (exit
  130) and `BrokenPipeError` (silent, e.g. `proc logs x | head`) are caught once around
  `args.func(args)` rather than in every `cmd_*`, since both are properties of *running the
  CLI*, not of any one subcommand's logic.

## Rejected alternatives
- **A single flat argument list with a `--action` flag** instead of subparsers. Rejected:
  subparsers give each subcommand its own `--help`, its own required/optional flags, and let
  `argparse.REMAINDER` scope cleanly to `start` alone.
- **Re-parsing/validating the launched command's own flags.** Rejected: `proc` is a generic
  supervisor for arbitrary commands; imposing a flag grammar on the supervised command would
  make it not-generic.

## Acceptance criteria
- GIVEN any subcommand WHEN invoked with `--help` THEN argparse prints that subcommand's own
  help text (not the top-level one).
- GIVEN `proc start --name x -- some-cmd --flag value` WHEN parsed THEN `cmd == ["some-cmd",
  "--flag", "value"]`, unaffected by argparse's own flag syntax.
- GIVEN a downstream pipe closes early (e.g. `proc logs x --follow | head -1`) WHEN `main()`
  is writing to stdout THEN the process exits 0 without a traceback.
- GIVEN Ctrl-C during any subcommand WHEN `main()` is running THEN it exits 130, not a raw
  traceback.
- GIVEN `proc start --env A=1 --env B=2 --name x -- cmd` WHEN parsed THEN `env == ["A=1",
  "B=2"]` (`action="append"`, validated in `core.cmd_start`, not here).
- GIVEN `proc status x --json` / `proc list --json` WHEN parsed THEN `json` is `True` and
  the engine switches to its machine view; without the flag the human view is the default.
- GIVEN `proc list --running --scheduled --session ID --all` WHEN parsed THEN `running`,
  `scheduled`, `session` and `all` hold the flags; `json` honours the same filters.
- GIVEN `proc launch-due` WHEN `main` runs THEN the parser is not built (`test_live_index.py`).
- GIVEN `proc list` / `ls` with only `--running`, `--scheduled`, `--all`, `--json`, `--full`,
  `--session ID` or `--session=ID` WHEN `main` runs THEN the parser is not built (argparse is
  about 0.3 MB of bytecode read); anything else (`--help`, an abbreviation, a missing value)
  goes to the full parser (`test_live_index.py`).
- GIVEN `proc list --json` WHEN run THEN the objects carry name, status, alive, exit_code, pid,
  pid_starttime, owner, start_ts, elapsed_s; `--full` adds the whole record. Measured on the
  live registry (27,636 records): `list --running [--session ID] --json` reads 0.79 MB
  and 0.05 CPU s at 12 MB peak RSS, inside the 1 MB bound of `cost.spec.md`.

- GIVEN `proc start --at <ISO time> ...` WHEN parsed THEN `at` holds the string; validation
  (zone required) is in `core.cmd_start`. `reschedule NAME --at T`, `launch-due` and
  `launcher {status,install,uninstall}` dispatch to `core.cmd_reschedule`,
  `core.cmd_launch_due`, `core.cmd_launcher`; `hook {status,install,uninstall,unwrap}
  [--config-dir DIR]...` dispatches to `core.cmd_hook`.

## State
n/a — `cli.py` holds no state of its own; all state lives in `core.py`'s registry (see
`core.spec.md` State).

## Methods
- `build_parser() -> argparse.ArgumentParser` — (imports argparse on call) construct the full subcommand grammar; pure,
  no side effects, safe to call repeatedly (used by tests to introspect help text).
- `main(argv=None) -> int` — parse `argv` (or `sys.argv` if `None`), dispatch to the matched
  `core.cmd_*`, translate `KeyboardInterrupt`/`BrokenPipeError` to clean exit codes.

## Invariants
- Every subcommand's `func` default is a `core.cmd_*` function — `cli.py` never inlines
  subcommand behaviour in a `set_defaults` lambda or elsewhere.
- `main()` never lets `BrokenPipeError` or `KeyboardInterrupt` propagate as an unhandled
  traceback to the caller.

## Formulas
n/a

## I/O contracts
- Input: `argv: list[str] | None` (defaults to `sys.argv[1:]` via argparse).
- Output: process exit code (`int`), plus whatever the dispatched `core.cmd_*` writes to
  stdout/stderr (see `core.spec.md` I/O contracts for the per-subcommand payloads).

## Names
| canonical | kind | forbidden-aliases |
|-----------|------|-------------------|
| build_parser | function | |
| main | function | |

## Dependencies
- `proc_lib.core` (`specs/proc_lib/core.spec.md`) — every subcommand's actual implementation.

## Test cases
- GIVEN any subcommand, option or exit code the code defines WHEN `CLI_REFERENCE.md` and
  `README.md` are read THEN each is written down: a section per subcommand naming all its
  options, every exit code in the table, the README listing every command and the registry's
  files (`test_docs.py`).
- GIVEN `proc list --exited --since 12 --json` WHEN parsed (by the fast parser or argparse)
  THEN `exited` is `True` and `since` is `12.0`; `--since` with a non-number goes to argparse
  (`test_history.py`).
- GIVEN any command but `hook` WHEN `main` runs THEN the day's sweep (move of finished jobs older than a day, retention) runs first
  (`core.housekeeping`; `test_history.py`).
- GIVEN `proc --help` WHEN run THEN it lists all twelve subcommands (`test_cli.py`).
- GIVEN `proc wait --help` WHEN run THEN the text documents exit code 5 (job ended unsuccessfully).
- GIVEN `proc start --name x -- echo hi --loud` WHEN parsed THEN the remainder after `--` is
  preserved intact as the launched command (`test_cli.py`).
- GIVEN a `BrokenPipeError` raised while writing output WHEN `main()` catches it THEN the
  process exits 0 with stdout closed cleanly, no traceback (`test_cli.py`).
