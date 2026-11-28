# spec: specs/proc_lib/claude_hook.spec.md
"""A Claude Code PreToolUse hook that shows account and project Bash checks the command behind
`proc start`.

A project's Bash checks judge the command text of a tool call. `proc start --name J -- CMD`
detaches CMD, so the text a check sees starts with `proc`, not with CMD, and a check written
for CMD (a database-write gate, an approval gate) passes it. This module is the hook that
closes that gap for every project at once: registered once in the user's Claude settings, it
removes each `proc start [options] --` prefix, runs account and project PreToolUse Bash
hooks on the remaining command and returns the strictest verdict.

It knows nothing of any project: hooks come from the active account's `settings.json`
and `$CLAUDE_PROJECT_DIR/.claude/settings.json` and `settings.local.json`.
Run as `python -m proc_lib.claude_hook` (what the registered hook command does);
`install`/`uninstall`/`ensure` edit the user's settings.

stdlib-only and light to import: it runs before every Bash call, so a command that does not
start a job costs one interpreter start, the read of stdin and one test of the command. This
module therefore holds only the shell scanner behind that test (`unwrap_command`) and `main`,
and imports nothing but `os` and `sys`; everything that judges a call (project hooks, verdicts,
settings editing) lives in `proc_lib.hook_judge`, imported only when the test matches, and is
reachable as an attribute of this module (`claude_hook.install`, `claude_hook.judge`, ...).
"""

from __future__ import annotations

import os
import sys

__all__ = [
    "START_FLAG_OPTIONS",
    "START_VALUE_OPTIONS",
    "is_assignment",
    "main",
    "shell_words",
    "skip_substitution",
    "unwrap_command",
]

# Every option of `proc start`; tests/test_claude_hook.py compares these two sets with the
# real parser, so a new option fails a test instead of mis-reading a call.
START_VALUE_OPTIONS = frozenset({"--name", "--timeout", "--cwd", "--env", "--at"})
START_FLAG_OPTIONS = frozenset({"--heavy", "--force-concurrent", "-h", "--help"})


# Words that leave the next word in command position.
_TRANSPARENT = frozenset(
    {
        "exec",
        "command",
        "time",
        "nohup",
        "nice",
        "env",
        "!",
        "if",
        "then",
        "else",
        "elif",
        "while",
        "until",
        "do",
        "{",
    }
)
_OPERATOR_CHARS = ";&|()\n"
_REDIRECT_CHARS = "<>"


# --------------------------------------------------------------------------- unwrapping


def is_assignment(value: str) -> bool:
    """`NAME=...` with NAME a shell identifier (what a regex `[A-Za-z_][A-Za-z0-9_]*=` matches
    at the start; written by hand because this module must not import `re`)."""
    name, equals, _ = value.partition("=")
    return (
        bool(equals)
        and name.isascii()
        and (name[:1].isalpha() or name[:1] == "_")
        and all(c.isalnum() or c == "_" for c in name)
    )


class _Word:
    """One shell word of a command (a plain class: `dataclasses` is not imported here)."""

    __slots__ = ("command", "end", "literal", "redirect_target", "segment", "start", "value")

    def __init__(
        self,
        start: int,
        end: int,
        value: str,  # quotes and backslashes removed
        literal: bool,  # no expansion inside
        command: bool,  # in command position
        segment: int,  # index of the simple command it belongs to
        redirect_target: bool = False,
    ) -> None:
        self.start = start
        self.end = end
        self.value = value
        self.literal = literal
        self.command = command
        self.segment = segment
        self.redirect_target = redirect_target


def _scan_word(text: str, i: int) -> tuple[int, str, bool]:
    """(end, value, literal) of the shell word starting at text[i]."""
    n = len(text)
    out: list[str] = []
    literal = True
    while i < n:
        c = text[i]
        if c in " \t\n" or c in _OPERATOR_CHARS or c in _REDIRECT_CHARS:
            break
        if c == "\\":
            if i + 1 < n:
                out.append(text[i + 1])
            i += 2
        elif c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            out.append(text[i + 1 : j])
            i = j + 1
        elif c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
                if text[i] in "$`":
                    literal = False
                out.append(text[i])
                i += 1
            i += 1
        elif c == "$" or c == "`":
            literal = False
            end = skip_substitution(text, i)
            out.append(text[i:end])
            i = end
        else:
            if c in "*?[~":
                literal = False
            out.append(c)
            i += 1
    return min(i, n), "".join(out), literal


def skip_substitution(text: str, i: int) -> int:
    """End of the `$(...)`, `${...}`, `$name` or backtick span at text[i]."""
    n = len(text)
    if text[i] == "`":
        j = text.find("`", i + 1)
        return n if j < 0 else j + 1
    if i + 1 < n and text[i + 1] in "({":
        close = ")" if text[i + 1] == "(" else "}"
        depth = 0
        j = i + 1
        while j < n:
            if text[j] == text[i + 1]:
                depth += 1
            elif text[j] == close:
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        return n
    j = i + 1
    while j < n and (text[j].isalnum() or text[j] == "_"):
        j += 1
    return max(j, i + 1)


def _skip_heredoc_bodies(text: str, i: int, pending: list[tuple[str, bool]]) -> int:
    """i is just after a newline: skip one body per pending heredoc delimiter."""
    n = len(text)
    for delimiter, strip_tabs in pending:
        while i < n:
            j = text.find("\n", i)
            line = text[i:] if j < 0 else text[i:j]
            i = n if j < 0 else j + 1
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
    return i


def shell_words(text: str) -> list[_Word]:
    words: list[_Word] = []
    n = len(text)
    i = 0
    command_position = True
    redirect_next = False
    segment = 0
    pending: list[tuple[str, bool]] = []
    while i < n:
        c = text[i]
        if c in " \t":
            i += 1
        elif c == "\\" and text[i + 1 : i + 2] == "\n":
            i += 2
        elif c == "\n":
            i += 1
            if pending:
                i = _skip_heredoc_bodies(text, i, pending)
                pending = []
            command_position, redirect_next = True, False
            segment += 1
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n;&|("):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in _OPERATOR_CHARS:
            i += 1
            while i < n and text[i] in "&|;":
                i += 1
            command_position, redirect_next = True, False
            segment += 1
        elif c in _REDIRECT_CHARS:
            j = i
            while j < n and text[j] in _REDIRECT_CHARS + "&":
                j += 1
            op = text[i:j]
            i = j
            if op.startswith("<<") and not op.startswith("<<<"):
                while i < n and text[i] in " \t":
                    i += 1
                end, value, _ = _scan_word(text, i)
                if value:
                    pending.append((value, op == "<<-"))
                i = end
            else:
                redirect_next = True
        else:
            end, value, literal = _scan_word(text, i)
            if end == i:  # a lone quote or similar: never loop
                end = i + 1
            is_command = command_position and not redirect_next
            words.append(
                _Word(i, end, value, literal, is_command, segment, redirect_target=redirect_next)
            )
            if redirect_next:
                redirect_next = False
            elif command_position and (is_assignment(value) or value in _TRANSPARENT):
                pass
            else:
                command_position = False
            i = end
    return words


def _is_proc(word: _Word) -> bool:
    return word.command and word.literal and os.path.basename(word.value) == "proc"


def _prefix_span(words: list[_Word], k: int) -> tuple[int, int] | None:
    """(start, end) in the text of the `proc start [options] --` prefix whose `proc` word is
    words[k], or None when words[k] is not `proc start`."""
    if k + 1 >= len(words):
        return None
    start_word = words[k + 1]
    if start_word.segment != words[k].segment or not start_word.literal:
        return None
    if start_word.value != "start":
        return None
    j = k + 2
    while j < len(words) and words[j].segment == words[k].segment:
        w = words[j]
        if w.redirect_target:
            j += 1
            continue
        if w.value == "--":
            nxt = words[j + 1] if j + 1 < len(words) else None
            if nxt is not None and nxt.segment == w.segment:
                return words[k].start, nxt.start
            return words[k].start, w.end
        if not w.value.startswith("-"):
            return words[k].start, w.start
        name, equals, _ = w.value.partition("=")
        if equals or name in START_FLAG_OPTIONS or name not in START_VALUE_OPTIONS:
            j += 1
        else:
            j += 2
    return None


def unwrap_command(command: str) -> str | None:
    """`command` with every `proc start [options] --` prefix removed, or None when it has no
    `proc start`. `cd x && proc start --name J -- psql ...` becomes `cd x && psql ...`."""
    if "proc" not in command or "start" not in command:
        return None
    changed = False
    for _ in range(8):  # `proc start -- proc start -- cmd` nests
        words = shell_words(command)
        spans = [
            span
            for k, w in enumerate(words)
            if _is_proc(w) and (span := _prefix_span(words, k)) is not None
        ]
        if not spans:
            break
        for start, end in reversed(spans):
            command = command[:start] + command[end:]
        changed = True
    return command if changed else None


# --------------------------------------------------------------------------- the hook


def _parse_json(raw: str) -> object:
    """`json.loads(raw)` without importing `json` (which imports `re`): the C scanner of the
    standard library, with the same strictness. Falls back to `json` where it is absent."""
    try:
        import _json
    except ImportError:  # pragma: no cover - a Python without the C accelerator
        import json

        return json.loads(raw)

    class _Context:
        strict = True
        object_hook = None
        object_pairs_hook = None
        parse_float = float
        parse_int = int
        memo: dict = {}

        @staticmethod
        def parse_constant(name: str) -> float:
            return float(name.replace("Infinity", "inf"))  # NaN, Infinity, -Infinity

    text = raw.lstrip(" \t\n\r")
    try:
        scan_once = _json.make_scanner(_Context())  # type: ignore[arg-type]  # typeshed wants the class
        value, end = scan_once(text, 0)
    except StopIteration as e:
        raise ValueError("payload is not JSON") from e
    if text[end:].strip(" \t\n\r"):
        raise ValueError("data after the JSON payload")
    return value


def _command_to_judge(raw: str) -> tuple[dict, str] | None:
    """(payload, inner command) when `raw` is a Bash call whose command has a `proc start`
    prefix, else None. Raises on a payload that cannot be read, so `main` refuses it."""
    # A cheap pre-filter, never the decision: the words can only be written differently in
    # JSON through an escape.
    if ("proc" not in raw or "start" not in raw) and "\\u" not in raw:
        return None
    payload = _parse_json(raw)
    if not isinstance(payload, dict):
        return None
    tool_input = payload.get("tool_input")
    if payload.get("tool_name") != "Bash" or not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    inner = unwrap_command(command)
    if inner is None:
        return None
    return payload, inner


def main(stdin=None, stdout=None, stderr=None, env=None) -> int:
    """The registered hook. Anything unexpected is a refusal with the reason, because a
    check that crashed must not read as a pass."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    env = dict(os.environ if env is None else env)
    try:
        raw = stdin.read()
        found = _command_to_judge(raw)
        if found is None:
            return 0
        from proc_lib import hook_judge

        verdict = hook_judge.judge(found[0], env, found[1])
        if verdict is None:
            return 0
        code, out, err = hook_judge.render(verdict)
    except Exception as e:  # noqa: BLE001 - fail closed on any internal error
        stderr.write(f"proc hook unwrap failed ({type(e).__name__}: {e}) - refused\n")
        return 2
    if out:
        stdout.write(out)
    if err:
        stderr.write(err)
    return code


def __getattr__(name: str):
    """Everything that judges a call or edits settings lives in `proc_lib.hook_judge`; it is
    imported on first use, never on the hook's common path."""
    if name.startswith("__"):
        raise AttributeError(name)
    from proc_lib import hook_judge

    try:
        return getattr(hook_judge, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


if __name__ == "__main__":
    sys.exit(main())
