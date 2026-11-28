# spec: specs/proc_lib/claude_hook.spec.md
"""What judges a call for `proc_lib.claude_hook`: project hook discovery and conditions, the
verdicts, the settings editing (`install`/`uninstall`/`status`/`ensure`).

Imported by `claude_hook` only once a Bash command holds a `proc start` prefix (or a caller
asks for one of these names), never on the hook's common path: this is where the heavy
standard-library imports live.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import datetime
import json
import os
import re
import shlex
import signal
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from proc_lib.claude_hook import is_assignment, shell_words, skip_substitution

__all__ = [
    "DEFAULT_HOOK_TIMEOUT",
    "HOOK_MARK",
    "Hook",
    "MAX_PARALLEL_HOOKS",
    "OPT_OUT_FILE",
    "REGISTERED_TIMEOUT",
    "Status",
    "Verdict",
    "combine",
    "default_config_dirs",
    "ensure",
    "find_project_dir",
    "hook_command",
    "install",
    "judge",
    "matching_hooks",
    "render",
    "run_hook",
    "settings_path",
    "status",
    "uninstall",
]

# The registered command contains this module name; install/uninstall/status find the
# hook's entry by it.
HOOK_MARK = "proc_lib.claude_hook"
# Written beside the registry by `uninstall`: `ensure` leaves a seat the user switched off.
OPT_OUT_FILE = ".no-claude-hook"
# Claude Code's own default for a command hook is 600 s; the registered entry gets more so
# this process answers before the harness would cut it.
DEFAULT_HOOK_TIMEOUT = 600
REGISTERED_TIMEOUT = 700
MAX_PARALLEL_HOOKS = 4


# --------------------------------------------------------------------------- project hooks


@dataclass(frozen=True)
class Hook:
    command: str
    timeout: int


def _matcher_matches(matcher: str, tool_name: str) -> bool:
    if matcher in ("", "*"):
        return True
    if re.fullmatch(r"[A-Za-z0-9_|]+", matcher):
        return tool_name in matcher.split("|")
    try:
        return re.search(matcher, tool_name) is not None
    except re.error:
        return False


def _permission_pattern_matches(pattern: str, command: str) -> bool:
    if pattern.endswith(":*"):
        prefix = pattern[:-2]
        return command == prefix or command.startswith(prefix + " ")
    pattern = re.sub(r"[ \t]+", " ", pattern.strip())
    pieces = re.findall(r"\\[*\\]|[^*\\]+|.", pattern, re.DOTALL)
    regex = "".join(
        ".*" if p == "*" else re.escape(p[1:] if p in (r"\*", r"\\") else p) for p in pieces
    )
    if pieces.count("*") == 1 and regex.endswith(r"\ .*"):
        regex = regex[:-4] + r"(?: .*)?"
    return re.fullmatch(regex, re.sub(r"[ \t]+", " ", command), re.DOTALL) is not None


def _condition_commands(command: str, depth: int = 0) -> tuple[list[str], bool, bool]:
    """Permission candidates, expansions, and uncertain syntax; never execute shell input."""
    if depth > 32:
        return [], True, True
    command = command.replace("\\\n", "")
    words = shell_words(command)
    candidates: list[str] = []
    segments: dict[int, list[str]] = {}
    expanded = uncertain = False
    for word in words:
        raw = command[word.start : word.end]
        quote = ""
        i = 0
        while i < len(raw):
            c = raw[i]
            if c == "\\" and quote != "'":
                i += 2
                continue
            if c in "\"'" and (not quote or quote == c):
                quote = "" if quote else c
            elif quote != "'" and c in "$`":
                expanded = True
                end = skip_substitution(raw, i)
                if c == "`" or raw[i : i + 2] == "$(":
                    body = raw[i + 1 : end - 1] if c == "`" else raw[i + 2 : end - 1]
                    nested, _, unknown = _condition_commands(body, depth + 1)
                    candidates.extend(nested)
                    uncertain |= unknown
                if word.command:
                    uncertain = True
                i = end
                continue
            i += 1
        uncertain |= bool(quote)
        if word.redirect_target or (
            word.value.isdecimal() and command[word.end : word.end + 1] in ("<", ">")
        ):
            continue
        segment = segments.setdefault(word.segment, [])
        if not segment and is_assignment(raw):
            continue
        if word.command and word.value in {
            "if",
            "then",
            "else",
            "elif",
            "while",
            "until",
            "for",
            "case",
            "do",
            "done",
            "function",
            "eval",
            "source",
            ".",
            "{",
            "}",
        }:
            uncertain = True
        segment.append(word.value)
    candidates.extend(" ".join(words) for words in segments.values() if words)
    return candidates, expanded, uncertain


def _condition_matches(condition: str | None, command: str) -> bool:
    """Claude Code's documented Bash `if` permission rule, including uncertain-input rules.

    https://code.claude.com/docs/en/hooks#bash-if-matching (Claude Code 2.1.319).
    Only command-name patterns can exclude a hook when expansions are present.
    """
    if not condition:
        return True
    match = re.fullmatch(r"([^()]+)(?:\((.*)\))?", condition, re.DOTALL)
    if not match:
        return False
    tool, pattern = match.groups()
    if tool != "Bash":
        return False
    if not pattern or pattern == "*":
        return True
    candidates, expanded, uncertain = _condition_commands(command)
    simple = (
        not re.search(r"\s", pattern[:-2])
        if pattern.endswith(":*")
        else re.fullmatch(r"[^\s*?\[]+\s?\*", pattern) is not None
    )
    if uncertain or (expanded and not simple):
        return True
    return any(
        _permission_pattern_matches(pattern, candidate)
        or _permission_pattern_matches("xargs " + pattern, candidate)
        for candidate in candidates
    )


def matching_hooks(paths: list[Path], tool_name: str, inner: str) -> list[Hook]:
    """Matching account and project command hooks, each command once, excluding ourselves."""
    hooks: list[Hook] = []
    seen: set[str] = set()
    for path in paths:
        try:
            data = _load(path)
        except (OSError, ValueError) as e:
            raise ValueError(f"cannot read hooks from {path}: {e}") from e
        groups = data.get("hooks", {}).get("PreToolUse", [])
        if not isinstance(groups, list):
            raise ValueError(f"{path}: hooks.PreToolUse must be a list")
        for group in groups:
            if not isinstance(group, dict) or not _matcher_matches(
                str(group.get("matcher") or ""), tool_name
            ):
                continue
            for entry in group.get("hooks") or []:
                if not isinstance(entry, dict) or entry.get("type", "command") != "command":
                    continue
                command = entry.get("command")
                if (
                    not isinstance(command, str)
                    or not command
                    or command in seen
                    or HOOK_MARK in command
                ):
                    continue
                if not _condition_matches(entry.get("if"), inner):
                    continue
                seen.add(command)
                timeout = entry.get("timeout")
                seconds = 0
                if isinstance(timeout, int | float) and not isinstance(timeout, bool):
                    seconds = int(timeout) if timeout > 0 else 0
                hooks.append(Hook(command, seconds))
    return hooks


def find_project_dir(env: dict[str, str], cwd: str) -> str | None:
    """`$CLAUDE_PROJECT_DIR`, else the nearest directory at or above `cwd` that holds a
    `.claude/settings.json` or `.claude/settings.local.json`."""
    given = env.get("CLAUDE_PROJECT_DIR")
    if given:
        return given
    if not cwd:
        return None
    for d in [Path(cwd), *Path(cwd).parents]:
        if (d / ".claude" / "settings.json").is_file() or (
            d / ".claude" / "settings.local.json"
        ).is_file():
            return str(d)
    return None


# --------------------------------------------------------------------------- verdicts

DENY, ASK, ALLOW = "deny", "ask", "allow"
_RANK = {DENY: 3, ASK: 2, ALLOW: 1}


@dataclass
class Verdict:
    decision: str | None = None
    reasons: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    # (hook command, command it proposed) for each hook that answered with `updatedInput`.
    rewrites: list[tuple[str, str]] = field(default_factory=list)

    @property
    def rewrote(self) -> bool:
        return bool(self.rewrites)


def _hook_label(command: str) -> str:
    """A short name for a hook command: its last script path, else its leading text."""
    scripts = re.findall(r"[^\s\"';&|]+\.(?:sh|py)\b", command)
    if scripts:
        return scripts[-1]
    one_line = " ".join(command.split())
    return one_line if len(one_line) <= 160 else one_line[:157] + "..."


def _proposed_command(updated: object) -> str:
    if isinstance(updated, dict) and isinstance(updated.get("command"), str):
        return updated["command"]
    return json.dumps(updated)


def _read_verdict(returncode: int, out: str, err: str) -> Verdict:
    """What one hook's exit code and output say, as Claude Code reads a PreToolUse hook."""
    if returncode == 2:
        return Verdict(DENY, [err.strip() or "the hook refused with exit status 2 and no message"])
    if returncode != 0:
        tail = err.strip()[-2000:]
        why = f"the hook exited with status {returncode} before reaching a verdict - refused"
        return Verdict(DENY, [why + (f"\n{tail}" if tail else "")])
    out = out.strip()
    if not out.startswith("{"):
        return Verdict()
    try:
        obj = json.loads(out)
    except ValueError:
        return Verdict()
    if not isinstance(obj, dict):
        return Verdict()
    special = obj.get("hookSpecificOutput")
    special = special if isinstance(special, dict) else {}
    verdict = Verdict()
    updated = special.get("updatedInput")
    if updated is not None:
        verdict.rewrites.append(("", _proposed_command(updated)))
    decision = special.get("permissionDecision")
    reason = special.get("permissionDecisionReason")
    if decision is None and obj.get("decision") in ("block", "deny"):
        decision, reason = DENY, obj.get("reason")
    if decision is None and obj.get("continue") is False:
        decision, reason = DENY, obj.get("stopReason")
    if decision == "defer":
        decision = ASK
    if decision in _RANK:
        verdict.decision = decision
        if isinstance(reason, str) and reason.strip():
            verdict.reasons.append(reason.strip())
    for text in (special.get("additionalContext"), obj.get("systemMessage")):
        if isinstance(text, str) and text.strip():
            verdict.context.append(text.strip())
    return verdict


def _kill_group(proc: subprocess.Popen) -> None:
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


def run_hook(hook: Hook, payload: str, env: dict[str, str]) -> Verdict:
    """Run one hook command with `payload` on stdin; a timeout, a crash or a hook that cannot
    start is a refusal, never a pass."""
    cap = min(hook.timeout or DEFAULT_HOOK_TIMEOUT, DEFAULT_HOOK_TIMEOUT)
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", hook.command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
    except OSError as e:
        return Verdict(DENY, [f"the hook could not be started ({e}) - refused"])
    try:
        out, err = proc.communicate(payload, timeout=cap)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        return Verdict(DENY, [f"the hook timed out after {cap}s - refused"])
    except BaseException:
        _kill_group(proc)
        raise
    verdict = _read_verdict(proc.returncode, out, err)
    verdict.rewrites = [(_hook_label(hook.command), new) for _, new in verdict.rewrites]
    return verdict


def combine(verdicts: list[Verdict]) -> Verdict:
    """The strictest decision, with the messages of every hook that gave it, and every
    hook's context."""
    top = max((v.decision for v in verdicts if v.decision), key=lambda d: _RANK[d], default=None)
    result = Verdict(top)
    for v in verdicts:
        if v.decision == top and top:
            result.reasons.extend(v.reasons)
        result.context.extend(v.context)
        result.rewrites.extend(v.rewrites)
    return result


def judge(payload: dict, env: dict[str, str], inner: str | None = None) -> Verdict | None:
    """The combined verdict of account and project PreToolUse hooks on the command behind
    `proc start`, or None when the call has nothing to unwrap or no hooks match. `inner` is
    the already unwrapped command (what `claude_hook.main` found); without it the command is
    unwrapped here."""
    tool_input = payload.get("tool_input")
    if payload.get("tool_name") != "Bash" or not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    if inner is None:
        from proc_lib.claude_hook import unwrap_command

        inner = unwrap_command(command)
    if inner is None:
        return None
    project_dir = find_project_dir(env, str(payload.get("cwd") or ""))
    paths = [settings_path(config_dir) for config_dir in default_config_dirs(env)]
    if project_dir is not None:
        paths.extend(
            Path(project_dir) / ".claude" / name
            for name in ("settings.json", "settings.local.json")
        )
    hooks = matching_hooks(paths, "Bash", inner)
    if not hooks:
        return None
    inner_payload = dict(payload)
    inner_payload["tool_input"] = {**tool_input, "command": inner}
    text = json.dumps(inner_payload)
    hook_env = dict(env)
    if project_dir is not None:
        hook_env["CLAUDE_PROJECT_DIR"] = project_dir
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL_HOOKS) as pool:
        verdicts = list(pool.map(lambda h: run_hook(h, text, hook_env), hooks))
    return combine(verdicts)


def render(verdict: Verdict) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) the hook process ends with for `verdict`."""
    if verdict.decision == DENY:
        body = "\n\n".join(verdict.reasons) or "refused"
        return (
            2,
            "",
            f"Refused for the command behind `proc start` by account or project hooks:\n{body}\n",
        )
    special: dict = {"hookEventName": "PreToolUse"}
    if verdict.decision:
        special["permissionDecision"] = verdict.decision
        special["permissionDecisionReason"] = (
            "\n\n".join(verdict.reasons)
            or "decided by account or project hooks for the command behind `proc start`"
        )
    context = list(verdict.context)
    if verdict.rewrites:
        lines = [
            "Hooks asked to rewrite the command behind `proc start`; no rewrite was applied "
            "(each command below is the form the hook proposed for the command after `--`):"
        ]
        lines.extend(f"- hook {hook}: {new}" for hook, new in verdict.rewrites)
        context.append("\n".join(lines))
    if context:
        special["additionalContext"] = "\n\n".join(context)
    if len(special) == 1:
        return 0, "", ""
    return 0, json.dumps({"hookSpecificOutput": special}), ""


# --------------------------------------------------------------------------- settings editing


@dataclass
class Status:
    path: Path
    installed: bool
    note: str = ""

    def line(self) -> str:
        state = "installed" if self.installed else "not installed"
        return f"{state}: {self.path}" + (f" ({self.note})" if self.note else "")


def default_config_dirs(env: dict[str, str] | None = None) -> list[Path]:
    source: Mapping[str, str] = os.environ if env is None else env
    given = source.get("CLAUDE_CONFIG_DIR")
    return [Path(given) if given else Path.home() / ".claude"]


def settings_path(config_dir: Path) -> Path:
    return Path(config_dir) / "settings.json"


def _shared_source(config_dir: Path, env: Mapping[str, str]) -> Path | None:
    """Use the generator's source only for seats it manages."""
    home = Path.home()
    source = Path(env.get("SEAT_SETTINGS_SOURCE", str(home / ".claude/settings.shared.json")))
    seats = env.get("SEAT_SETTINGS_SEATS", str(home / ".claude"))
    if source.is_file() and any(
        config_dir.resolve() == Path(seat).resolve() for seat in seats.split(":") if seat
    ):
        return source
    return None


def _sync_shared(env: Mapping[str, str]) -> None:
    """Let the settings owner generate the seats; never edit its output directly."""
    script = Path.home() / ".claude/hooks/seat_settings_sync.py"
    try:
        subprocess.run(
            [sys.executable, str(script), "sync"],
            env=dict(env),
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise OSError(
            f"Shared settings sync failed: script={script}, python={sys.executable}: {e}"
        ) from e


def _hook_words(command: str) -> list[str]:
    """Strip the seat settings wrapper, with or without an explicit bash launcher."""
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    offset = 1 if words and words[0] in ("bash", "/bin/bash", "/usr/bin/bash") else 0
    wrappers = {
        "$HOME/.claude/hooks/_hook_wrap.sh",
        str(Path.home() / ".claude/hooks/_hook_wrap.sh"),
    }
    if len(words) > offset and words[offset] in wrappers:
        return words[offset + 1 :]
    return words


def hook_command(python: str | None = None) -> str:
    return f"{shlex.quote(python or sys.executable)} -I -m {HOOK_MARK}"


def _load(path: Path) -> dict:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return data


def _is_ours(entry: object) -> bool:
    return isinstance(entry, dict) and HOOK_MARK in str(entry.get("command", ""))


def _ours(data: dict) -> list[dict]:
    groups = (data.get("hooks") or {}).get("PreToolUse") or []
    return [e for g in groups if isinstance(g, dict) for e in g.get("hooks") or [] if _is_ours(e)]


def _write(path: Path, data: dict) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    if path.exists():
        stamp = datetime.date.today().strftime("%Y%m%d")
        backup = path.with_name(f"{path.name}.bak-{stamp}-proc-hook")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-proc-hook")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.chmod(mode)
    tmp.replace(path)


def status(config_dir: Path) -> Status:
    path = settings_path(config_dir)
    try:
        data = _load(path)
    except (OSError, ValueError) as e:
        return Status(path, False, f"unreadable: {e}")
    found = _ours(data)
    if not found:
        return Status(path, False)
    return Status(
        path,
        True,
        ""
        if any(_hook_words(str(e.get("command", ""))) == shlex.split(hook_command()) for e in found)
        else "pinned to another interpreter",
    )


def install(
    config_dir: Path, base: Path | None = None, env: dict[str, str] | None = None
) -> Status:
    """Register the hook in the settings of `config_dir`; an existing entry of ours has its
    command and timeout rewritten, nothing else is touched. Clears the opt-out of `base`."""
    env = dict(os.environ if env is None else env)
    shared = _shared_source(config_dir, env)
    path = shared or settings_path(config_dir)
    data = _load(path)
    command = hook_command()
    if shared is not None:
        command = f"$HOME/.claude/hooks/_hook_wrap.sh {command}"
    found = _ours(data)
    if found:
        for entry in found:
            entry["command"], entry["timeout"] = command, REGISTERED_TIMEOUT
    else:
        hooks = data.setdefault("hooks", {})
        hooks.setdefault("PreToolUse", []).append(
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": command, "timeout": REGISTERED_TIMEOUT}],
            }
        )
    _write(path, data)
    if shared is not None:
        _sync_shared(env)
    if base is not None:
        with contextlib.suppress(OSError):
            (Path(base) / OPT_OUT_FILE).unlink()
    return status(config_dir)


def uninstall(config_dir: Path, base: Path | None = None) -> Status:
    """Remove the hook from the settings of `config_dir` and record the opt-out in `base`
    so `ensure` does not put it back."""
    env = dict(os.environ)
    shared = _shared_source(config_dir, env)
    path = shared or settings_path(config_dir)
    if base is not None:
        Path(base).mkdir(parents=True, exist_ok=True)
        (Path(base) / OPT_OUT_FILE).write_text("proc hook uninstall\n")
    data = _load(path)
    if _ours(data):
        hooks = data["hooks"]
        groups = []
        for group in hooks["PreToolUse"]:
            if isinstance(group, dict) and "hooks" in group:
                group["hooks"] = [e for e in group["hooks"] if not _is_ours(e)]
                if not group["hooks"]:
                    continue
            groups.append(group)
        if groups:
            hooks["PreToolUse"] = groups
        else:
            del hooks["PreToolUse"]
        if not hooks and shared is None:
            del data["hooks"]
        _write(path, data)
        if shared is not None:
            _sync_shared(env)
    return status(config_dir)


def _interpreter_missing(entries: list[dict]) -> bool:
    """True when no entry of ours names an interpreter that still exists (a tool environment
    removed or rebuilt elsewhere): the hook would fail on every Bash call."""
    for entry in entries:
        try:
            first = _hook_words(str(entry.get("command", "")))[0]
        except (ValueError, IndexError):
            continue
        if Path(first).exists():
            return False
    return True


def ensure(base: Path, env: dict[str, str] | None = None) -> str | None:
    """Register the hook for the Claude seat the caller runs under when its settings file
    exists and holds no working entry of ours (none, or one whose interpreter is gone) and the
    user did not uninstall. The one-line note
    when it installed, else None. Never raises: this runs inside `proc start`."""
    try:
        env = dict(os.environ if env is None else env)
        if (Path(base) / OPT_OUT_FILE).exists():
            return None
        for config_dir in default_config_dirs(env):
            path = settings_path(config_dir)
            if not path.is_file():
                continue
            shared = _shared_source(config_dir, env)
            found = _ours(_load(shared or path))
            if found and not _interpreter_missing(found):
                if shared is not None:
                    generated = _ours(_load(path))
                    expected = [
                        {**e, "command": _hook_words(str(e.get("command", "")))} for e in found
                    ]
                    actual = [
                        {**e, "command": _hook_words(str(e.get("command", "")))} for e in generated
                    ]
                    if actual != expected:
                        _sync_shared(env)
                continue
            st = install(config_dir, env=env)
            return (
                "proc: installed its Claude Code hook "
                f"(shows project hooks the command behind `proc start`): {st.path}"
            )
    except Exception:  # noqa: BLE001 - best effort by contract
        return None
    return None
