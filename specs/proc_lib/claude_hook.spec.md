# claude_hook — spec

> Permanent contract and source of truth. Edit THIS file directly on any change.
> Every source `.py` implementing it carries `# spec: <path-to-this-file>`.

## Intent
Make account and project Claude Code PreToolUse Bash checks judge the command behind
`proc start [options] -- CMD` as if `CMD` had been the tool call. A check written for `CMD`
(a database-write gate, an approval gate) reads the call's command text; `proc start ...`
hides `CMD` behind a launcher word, so without this hook each project has to learn `proc`'s
grammar. With it, a project knows nothing of `proc`: the hook ships with `proc`, is registered
once in the user's Claude settings, and where `proc` is absent (a colleague's machine) there
is nothing to unwrap.

## Rationale / Key decisions
- **Run the project's hooks directly instead of rewriting the call.** Measured on Claude Code
  2.1.319: with two PreToolUse Bash hooks on one call, the first returning
  `updatedInput.command = "echo REWRITTEN"` and the second logging the command it received,
  the tool ran the rewritten command while the second hook was handed the ORIGINAL command.
  Hooks of one event run in parallel on the original input, so a rewrite cannot make the
  project's hooks see the inner command. The hook reads account and project PreToolUse
  groups itself and feeds each command hook the payload with `tool_input.command` replaced.
- **Remove the prefix, keep the rest of the command.** `cd x && proc start --name J -- make`
  is judged as `cd x && make`: the chain, the `cd` and any redirections stay, so a check that
  reads the working directory or the whole line sees what will run.
- **Option tables, not guessing.** Where `--` ends `proc start`'s own words is decided with
  the option tables of `proc start` (`START_VALUE_OPTIONS`, `START_FLAG_OPTIONS`); a test
  compares them with the real parser, so a new option fails a test instead of mis-reading a
  call. The command scanner is shell-aware (quotes, comments, heredoc bodies, chain
  operators): `proc start` inside a quoted string, a commit message or a heredoc is not a
  call.
- **Fail closed.** A hook that times out, crashes, exits with a code other than 0 or 2, cannot
  be started, or an internal error in this module, is a refusal that says why: what the
  project's check guards against cannot be ruled out. Claude Code itself reads such an exit as
  a non-blocking error, so the stricter reading is this hook's own choice, as a project
  dispatcher's is.
- **Light on the common path.** It runs before every Bash call, so a call that starts no job
  must cost the interpreter start, the read of stdin and one test of the command (measured
  2026-11-12 on the live install: 0.07-0.09 CPU s, 16.6 MB max RSS and 1.8 MB read before the
  change, about 0.02 s, 12.9 MB and 0.3 MB after, against 0.015 s, 11.7 MB and 0.25 MB for a
  bare `python -I -c pass`). The registered command stays `<python> -I -m proc_lib.claude_hook`,
  so the module has two halves: `proc_lib.claude_hook` holds only the shell scanner behind
  `unwrap_command` and `main`, and imports nothing but `os` and `sys` (no `dataclasses`,
  `concurrent.futures`, `logging`, `subprocess`, `re`, `json`, `shlex`, `datetime`, `signal`,
  `pathlib`; a test fails if one comes back); `proc_lib.hook_judge` holds everything that judges
  a call and edits settings, and is imported only once the test matches (or when a caller asks
  `claude_hook` for one of its names, which `claude_hook.__getattr__` resolves there).
- **The judge-or-return test is the unwrap scan, on the command.** `main` pre-filters the raw
  stdin text with a substring test that can only skip (it never decides: a JSON `\u` escape
  sends the payload on), parses it with the C scanner of the standard library (no `json`, which
  imports `re`), and takes `tool_input.command` of a `Bash` call. The call goes to the judge
  exactly when `unwrap_command(command)` finds a `proc start` prefix, so the test admits every
  form the judge would unwrap by construction, and a path under `/proc` or the word "restart"
  in another field never sends an ordinary command to it. A payload that cannot be read is a
  refusal, as before.
- **Registered once, current always.** The settings entry names the module, not a copy of
  the code, so a `proc` upgrade is live without re-registering. `ensure` (called by
  `proc start`) registers it for the seat the caller runs under when that seat has a settings
  file and no entry; `uninstall` records an opt-out beside the registry so `ensure` respects
  it, and `install` clears it. For seats managed by `seat_settings_sync.py`, edit its
  shared source instead of generated settings, then run the generator's `sync` with a
  30-second cap. Resolve the source from `SEAT_SETTINGS_SOURCE` (default
  `~/.claude/settings.shared.json`) and seat membership from `SEAT_SETTINGS_SEATS`
  (colon-separated config dirs; default `~/.claude` alone). Other seats retain direct settings edits.
  The shared entry uses `$HOME/.claude/hooks/_hook_wrap.sh`; uninstall removes it from
  the same source, retaining an empty shared hooks object when necessary. A missing or
  failing generator fails explicit installation; automatic ensure remains best effort.
  Status and missing-interpreter checks read the Python command after the wrapper,
  either directly invoked or launched by bash, accepting both `$HOME` and absolute paths.

- **Active account only.** Read `settings.json` in `$CLAUDE_CONFIG_DIR`, else `~/.claude`,
  followed by project `settings.json` and `settings.local.json`. Do not combine different
  accounts. Skip commands containing `proc_lib.claude_hook` to prevent recursion and run each
  matching command once across all files. Evaluate conditions before deduplicating.
- **Handler conditions follow Bash permission rules.** The Claude Code hooks reference
  (https://code.claude.com/docs/en/hooks#bash-if-matching, read 2026-11-07) specifies one
  permission rule per `if`, not a shell predicate. Match `Bash`, `Bash(*)`, exact commands,
  `*` wildcards and legacy `:*` prefixes against each normalized subcommand, including
  `xargs` forms, after removing leading assignments and shell quoting. Inspect `$()` and
  backtick bodies too, without executing them. Only `*` is a wildcard; `?` is literal.
  With expansions, argument-specific patterns run regardless of their apparent match;
  command-name patterns may still exclude a hook. Undetermined shell syntax runs the hook.
  A rule for another tool skips it; absent or empty `if` runs it. These rules reproduce the
  reference's Bash matching examples and the installed 2.1.319 permission matcher.

## Rejected alternatives
- **Each project strips `proc` itself.** The state before this module: the word `proc` in
  many hook files and their tests, per project.
- **A rewrite (`updatedInput`) hook ahead of the project's.** Measured above: not seen by the
  other hooks.
- **Copying the hook script into `~/.claude/hooks`.** A copy goes stale on every `proc`
  upgrade; the module path does not.
- **A `--pid`-style or registry lookup to find the command.** The command is in the tool call.

## Acceptance criteria
- GIVEN a project hook that refuses a text containing `delete from` WHEN the call is
  `proc start --name p -- psql -c "delete from t"` THEN the hook exits 2 and its stderr
  carries the project hook's own message.
- GIVEN a project hook that answers `ask` or `allow` through `hookSpecificOutput` WHEN the call
  starts a job THEN the same decision and reason come back.
- GIVEN several hooks WHEN they disagree THEN the strictest decision (deny, ask, allow) wins and
  carries the messages of every hook that gave it.
- GIVEN flags before `--`, `--name=J`, a path to `proc`, a leading `cd x &&`, an environment
  prefix, a chain, or nested `proc start` WHEN unwrapped THEN only the `proc start ... --`
  prefixes are removed.
- GIVEN a command without `proc start` at command position (an `echo`, a commit message, a
  heredoc body, `proc wait`) THEN it passes with no output and no project hook runs.
- GIVEN `ls -la`, `grep start /proc/cpuinfo` or `echo restart` WHEN the hook runs THEN none of
  `dataclasses`, `concurrent.futures`, `logging`, `subprocess`, `re`, `json`, `shlex`,
  `datetime`, `signal`, `pathlib` or `proc_lib.hook_judge` is imported.
- GIVEN each form in the unwrap examples above THEN the judge-or-return test admits it with the
  same inner command; GIVEN other fields of the payload holding `proc` and `start` THEN an
  ordinary command is not judged.
- GIVEN no project directory or no project settings THEN account hooks still run; with no
  matching account or project hooks the call passes.
- GIVEN account hooks answering deny or ask THEN their decisions and messages pass through.
- GIVEN hooks that answer with `updatedInput` WHEN the call starts a job THEN no rewrite is
  applied and the notice in `additionalContext` names each such hook (its script path) and the
  command it proposed for the command after `--`.
- GIVEN this module in account or project settings THEN it is skipped; a command present in
  account, project and local settings runs once if any matching entry selects it.
- GIVEN `if: "Bash(git *)"` THEN `ls` skips the hook, and an assignment-prefixed or chained
  `git push` selects it, evaluated on the unwrapped command.
- GIVEN `if: "Bash(rm *)"` THEN `echo $(rm -rf /)` selects it, `echo $(date)` skips it, and
  single-quoted command-looking text does not select it.
- GIVEN an argument-specific condition and shell expansions THEN the hook runs; GIVEN a
  condition for another tool THEN it is skipped.
- GIVEN a malformed existing settings file THEN the call is refused rather than skipping
  its checks; a missing file or absent hooks contributes no hooks.
- GIVEN a project hook that hangs past its `timeout`, exits 1, or does not exist THEN the call
  is refused with a message naming the cause.
- GIVEN `install` then `uninstall` THEN the user's other settings are unchanged and no empty
  `hooks` residue remains.

## State
- One `PreToolUse` group with `matcher: "Bash"` in `<config dir>/settings.json`
  (`$CLAUDE_CONFIG_DIR`, else `~/.claude`), whose command contains `proc_lib.claude_hook`.
  The previous file is kept once a day as `settings.json.bak-<date>-proc-hook`.
- For a managed seat the source above holds the entry; backups are made beside that
  source, and the generator owns writing each seat's settings file.
- `$PROC_HOME/.no-claude-hook`: the opt-out written by `uninstall`.

## Methods
- `unwrap_command(command) -> str | None` — the command without its `proc start ... --`
  prefixes, None when there is none.
- `shell_words(text)`, `skip_substitution(text, i) -> int`, `is_assignment(value) -> bool` — the
  scanner pieces `hook_judge` reuses to read a hook condition's command (exported in `__all__`).
- `matching_hooks(paths, tool_name, inner) -> list[Hook]` — matching command hooks from
  account then project settings, conditions evaluated on `inner`, each command once.
- `run_hook(hook, payload, env) -> Verdict`, `combine(verdicts) -> Verdict`.
- `judge(payload, env, inner=None) -> Verdict | None`, `render(verdict) -> (code, stdout,
  stderr)` (both in `proc_lib.hook_judge`, reachable from `claude_hook`),
  `main(...) -> int` (in `claude_hook`) — the hook.
- `install(config_dir, base)`, `uninstall(config_dir, base)`, `status(config_dir)`,
  `ensure(base, env) -> note | None`.

## Invariants
- No import from `proc_lib.core` or `proc_lib.cli` (the engine imports this module).
- `proc_lib/claude_hook.py` imports only `os` and `sys` at module level and `proc_lib.hook_judge`
  only inside `main`/`__getattr__`; a Bash call without a `proc start` prefix never imports
  `hook_judge`.
- No project, path or hook name is written in this module: the hooks come only from the
  active account's and project's settings files.
- A hook's `updatedInput` is never applied; the verdict says it was not.
- Every account or project hook runs under a hard cap (its own `timeout`, at most 600 s) in its own
  process group, killed on expiry.
- `ensure` never raises and never creates a settings file; `proc` never signals anything
  because of this module.
- A handler's `if` is evaluated on the inner command before deduplication; no shell
  command from the tool input or the condition is executed by the matcher.
- Only hooks of `type: "command"` run.

## Formulas
n/a
