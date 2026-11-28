"""The Claude Code hook: unwrapping `proc start`, running a fake project's hooks on the
inner command, and the settings install/uninstall."""

from __future__ import annotations

import argparse
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from proc_lib import claude_hook, cli, core, hook_judge

from .conftest import REAL_ENSURE_HOOK, ns


@pytest.fixture(autouse=True)
def isolated_account(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))


# ------------------------------------------------------------------ unwrap_command


UNWRAP_FORMS = [
    ("proc start --name probe -- psql -c 'delete from t'", "psql -c 'delete from t'"),
    ("proc start --name p --timeout 60 --cwd /x -- echo hi", "echo hi"),
    ("proc start --name=p --env A=1 --env B=2 -- echo hi", "echo hi"),
    ("proc start --heavy --name p -- echo hi", "echo hi"),
    ("proc start --force-concurrent --name p -- echo hi", "echo hi"),
    ("proc start --force-concurrent make", "make"),
    ("proc start --help make", "make"),
    ("proc start -h make", "make"),
    ("proc start --name p -- bash -c 'a; b'", "bash -c 'a; b'"),
    ("cd /tmp/x && proc start --name p -- make", "cd /tmp/x && make"),
    ("cd x; proc start --name p -- make; echo done", "cd x; make; echo done"),
    ("/home/u/.local/bin/proc start --name p -- make", "make"),
    ("FOO=1 proc start --name p -- make", "FOO=1 make"),
    ("true || proc start --name p -- make | tee log", "true || make | tee log"),
    ("proc start --name a -- one && proc start --name b -- two", "one && two"),
    ("proc start --name a -- proc start --name b -- two", "two"),
    ("proc start --name p make", "make"),
    ("proc start --name p --at 2026-11-07T10:00:00+00:00 -- make", "make"),
    ("(proc start --name p -- make)", "(make)"),
]


@pytest.mark.parametrize(("command", "inner"), UNWRAP_FORMS)
def test_unwrap_removes_the_prefix(command, inner):
    assert claude_hook.unwrap_command(command) == inner


NOT_UNWRAPPED = [
    "ls -la",
    "proc wait job --timeout 30",
    "proc logs job --tail 5",
    "echo 'proc start --name x -- rm -rf /'",
    'echo "proc start --name x -- rm"',
    "git commit -m 'proc start --name x -- rm'",
    "ls /proc/1/start",
    "# proc start --name x -- rm",
    "cat <<'EOF'\nproc start --name x -- rm\nEOF",
    "startproc start",
    "proc",
    "proc start",
]


@pytest.mark.parametrize("command", NOT_UNWRAPPED)
def test_unwrap_leaves_other_commands_alone(command):
    assert claude_hook.unwrap_command(command) is None


def test_unwrap_start_option_tables_match_the_parser():
    """A new `proc start` option must be added to the hook's tables, or a call using it
    would be mis-read."""
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    values, flags = set(), set()
    for action in sub.choices["start"]._actions:
        if not action.option_strings:
            continue
        (flags if action.nargs == 0 else values).update(action.option_strings)
    assert values == claude_hook.START_VALUE_OPTIONS
    assert flags == claude_hook.START_FLAG_OPTIONS


# ------------------------------------------------------------------ the hook on a fake project


def _script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _project(tmp_path: Path, groups, local=None) -> Path:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    settings = {"hooks": {"PreToolUse": groups}}
    (project / ".claude" / "settings.json").write_text(json.dumps(settings))
    if local is not None:
        (project / ".claude" / "settings.local.json").write_text(
            json.dumps({"hooks": {"PreToolUse": local}})
        )
    return project


def _group(*commands, matcher="Bash", timeout=None):
    entries = []
    for c in commands:
        entry = {"type": "command", "command": c}
        if timeout is not None:
            entry["timeout"] = timeout
        entries.append(entry)
    return {"matcher": matcher, "hooks": entries}


def _call(project: Path | None, command: str, tool="Bash", extra_env=None):
    payload = {
        "session_id": "s",
        "cwd": str(project) if project else "/",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"command": command},
    }
    env = {"PATH": os.environ["PATH"], **(extra_env or {})}
    if project is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project)
    out, err = io.StringIO(), io.StringIO()
    code = claude_hook.main(io.StringIO(json.dumps(payload)), out, err, env)
    return code, out.getvalue(), err.getvalue()


# a project check that refuses a database write and prints its own message
DENY_WRITES = (
    "cmd=$(python3 -c \"import sys,json;print(json.load(sys.stdin)['tool_input']['command'])\"); "
    'case "$cmd" in *"delete from"*) echo "BLOCKED: db write -> $cmd" >&2; exit 2;; esac; exit 0'
)


def test_deny_from_a_project_hook_passes_through_with_its_message(tmp_path):
    project = _project(tmp_path, [_group(DENY_WRITES)])
    code, out, err = _call(project, 'proc start --name probe -- psql -c "delete from t"')
    assert code == 2
    assert 'BLOCKED: db write -> psql -c "delete from t"' in err
    assert out == ""


def test_the_hook_sees_the_inner_command_and_keeps_the_rest_of_the_payload(tmp_path):
    dump = tmp_path / "seen.json"
    project = _project(tmp_path, [_group(f"cat > {dump}")])
    _call(project, "cd /tmp && proc start --name p -- make all")
    seen = json.loads(dump.read_text())
    assert seen["tool_input"]["command"] == "cd /tmp && make all"
    assert seen["cwd"] == str(project)
    assert seen["tool_name"] == "Bash"
    assert seen["hook_event_name"] == "PreToolUse"


def test_allowed_inner_command_is_silent(tmp_path):
    project = _project(tmp_path, [_group(DENY_WRITES)])
    assert _call(project, "proc start --name p -- ls") == (0, "", "")


def test_ask_passes_through(tmp_path):
    ask = (
        "cat >/dev/null; printf '%s' "
        '\'{"hookSpecificOutput":{"hookEventName":"PreToolUse",'
        '"permissionDecision":"ask","permissionDecisionReason":"confirm the rebuild"}}\''
    )
    project = _project(tmp_path, [_group(ask)])
    code, out, _ = _call(project, "proc start --name p -- rebuild")
    assert code == 0
    special = json.loads(out)["hookSpecificOutput"]
    assert special["permissionDecision"] == "ask"
    assert "confirm the rebuild" in special["permissionDecisionReason"]


def test_explicit_allow_passes_through(tmp_path):
    allow = (
        "cat >/dev/null; printf '%s' "
        '\'{"hookSpecificOutput":{"permissionDecision":"allow",'
        '"permissionDecisionReason":"known good"}}\''
    )
    project = _project(tmp_path, [_group(allow)])
    code, out, _ = _call(project, "proc start --name p -- ls")
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_the_strictest_verdict_wins_and_every_refusing_hook_speaks(tmp_path):
    ask = (
        "cat >/dev/null; printf '%s' "
        '\'{"hookSpecificOutput":{"permissionDecision":"ask",'
        '"permissionDecisionReason":"asking"}}\''
    )
    allow = (
        'cat >/dev/null; printf \'%s\' \'{"hookSpecificOutput":{"permissionDecision":"allow"}}\''
    )
    deny_a = "cat >/dev/null; echo first refusal >&2; exit 2"
    deny_b = "cat >/dev/null; echo second refusal >&2; exit 2"
    project = _project(tmp_path, [_group(allow, ask, deny_a, deny_b)])
    code, _, err = _call(project, "proc start --name p -- ls")
    assert code == 2
    assert "first refusal" in err
    assert "second refusal" in err
    assert "asking" not in err


def test_json_deny_is_read_like_the_harness_reads_it(tmp_path):
    deny = (
        "cat >/dev/null; printf '%s' "
        '\'{"hookSpecificOutput":{"permissionDecision":"deny",'
        '"permissionDecisionReason":"policy says no"}}\''
    )
    project = _project(tmp_path, [_group(deny)])
    code, _, err = _call(project, "proc start --name p -- ls")
    assert code == 2
    assert "policy says no" in err


def test_only_matching_groups_run(tmp_path):
    marker = tmp_path / "ran"
    project = _project(
        tmp_path,
        [
            _group(f"touch {marker}", matcher="Edit|Write"),
            _group(DENY_WRITES, matcher="Bash|Edit"),
        ],
    )
    code, _, _ = _call(project, "proc start --name p -- psql -c 'delete from t'")
    assert code == 2
    assert not marker.exists()


def test_settings_local_json_hooks_run_too(tmp_path):
    project = _project(
        tmp_path, [], local=[_group("cat >/dev/null; echo local says no >&2; exit 2")]
    )
    code, _, err = _call(project, "proc start --name p -- ls")
    assert code == 2
    assert "local says no" in err


@pytest.mark.parametrize("decision", ["deny", "ask"])
def test_account_verdict_passes_through_without_project_settings(tmp_path, decision):
    response = json.dumps(
        {
            "hookSpecificOutput": {
                "permissionDecision": decision,
                "permissionDecisionReason": "account policy",
            }
        }
    )
    seat = _seat(
        tmp_path,
        {"hooks": {"PreToolUse": [_group("cat >/dev/null; printf '%s' " + shlex.quote(response))]}},
    )
    code, out, err = _call(
        None, "proc start --name p -- ls", extra_env={"CLAUDE_CONFIG_DIR": str(seat)}
    )
    assert code == (2 if decision == "deny" else 0)
    assert "account policy" in (err if decision == "deny" else out)


def test_default_account_settings_are_read(tmp_path):
    seat = tmp_path / ".claude"
    seat.mkdir()
    (seat / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [_group("echo default account >&2; exit 2")]}})
    )
    code, _, err = _call(None, "proc start --name p -- ls")
    assert code == 2
    assert "default account" in err


def test_account_and_project_hooks_run_once_and_skip_ours(tmp_path):
    marker = tmp_path / "ran"
    hook = f"cat >/dev/null; echo ran >> {marker}"
    own = f"echo {hook_judge.HOOK_MARK} >> {marker}; exit 2"
    project = _project(tmp_path, [_group(hook, own)], local=[_group(hook)])
    seat = _seat(tmp_path, {"hooks": {"PreToolUse": [_group(hook, own)]}})
    assert _call(
        project, "proc start --name p -- ls", extra_env={"CLAUDE_CONFIG_DIR": str(seat)}
    ) == (0, "", "")
    assert marker.read_text() == "ran\n"


@pytest.mark.parametrize(
    ("condition", "inner", "matches"),
    [
        ("Bash(git *)", "FOO=bar git push", True),
        ("Bash(git *)", "npm test && git push", True),
        ("Bash(rm *)", "echo $(rm -rf /)", True),
        ("Bash(rm *)", "echo `rm -rf /`", True),
        ("Bash(rm *)", "echo $(date)", False),
        ("Bash(git push *)", "echo $(date)", True),
        ("Bash(git push *)", "echo $VALUE", True),
        ("Bash(rm *)", "echo '$(rm -rf /)'", False),
        ("Bash(rm *)", "echo 'rm -rf /'", False),
        ("Bash(git *)", "git", True),
        ("Bash(git:*)", "git push", True),
        ("Bash(git:*)", "gitify push", False),
        ("Bash(git *)", "xargs git push", True),
        ("Bash(git *)", "ls", False),
        ("Bash(git push)", "git push 2>/dev/null", True),
        ("Bash(git *)", "git\\\n push", True),
        ("Bash(git *)", "ls | git status", True),
        ("Bash(git *)", 'echo "$(git status)"', True),
        ("Bash(git *)", "$COMMAND", True),
        ("Read", "git push", False),
        ("Bash", "ls", True),
        ("Bash(*)", "ls", True),
        ("Bash(echo a?b)", "echo acb", False),
        ("Bash(echo a?b)", "echo a?b", True),
    ],
)
@pytest.mark.parametrize("source", ["account", "project"])
def test_handler_if_uses_unwrapped_bash_permission_rule(
    tmp_path, condition, inner, matches, source
):
    group = _group("echo condition matched >&2; exit 2")
    group["hooks"][0]["if"] = condition
    project = _project(tmp_path, [group] if source == "project" else [])
    seat = _seat(tmp_path, {"hooks": {"PreToolUse": [group] if source == "account" else []}})
    code, _, err = _call(
        project, "proc start --name p -- " + inner, extra_env={"CLAUDE_CONFIG_DIR": str(seat)}
    )
    assert code == (2 if matches else 0)
    assert ("condition matched" in err) == matches


def test_nonmatching_duplicate_does_not_hide_matching_entry(tmp_path):
    group = _group("echo matching duplicate >&2; exit 2")
    group["hooks"][0]["if"] = "Bash(git *)"
    project = _project(tmp_path, [_group(group["hooks"][0]["command"])])
    seat = _seat(tmp_path, {"hooks": {"PreToolUse": [group]}})
    code, _, err = _call(
        project, "proc start --name p -- ls", extra_env={"CLAUDE_CONFIG_DIR": str(seat)}
    )
    assert code == 2
    assert "matching duplicate" in err


def test_account_and_project_use_the_strictest_verdict(tmp_path):
    project = _project(tmp_path, [_group("echo project denies >&2; exit 2")])
    seat = _seat(
        tmp_path,
        {
            "hooks": {
                "PreToolUse": [
                    _group(
                        'printf \'%s\' \'{"hookSpecificOutput":{"permissionDecision":"allow"}}\''
                    )
                ]
            }
        },
    )
    code, _, err = _call(
        project, "proc start --name p -- ls", extra_env={"CLAUDE_CONFIG_DIR": str(seat)}
    )
    assert code == 2
    assert "project denies" in err


def test_account_settings_without_hooks_pass(tmp_path):
    seat = _seat(tmp_path, {"model": "x"})
    assert _call(None, "proc start --name p -- ls", extra_env={"CLAUDE_CONFIG_DIR": str(seat)}) == (
        0,
        "",
        "",
    )


def test_broken_account_settings_fail_closed(tmp_path):
    seat = _seat(tmp_path)
    (seat / "settings.json").write_text("{broken")
    code, _, err = _call(
        None, "proc start --name p -- ls", extra_env={"CLAUDE_CONFIG_DIR": str(seat)}
    )
    assert code == 2
    assert "refused" in err


def test_a_hook_that_crashes_fails_closed_with_a_message(tmp_path):
    project = _project(tmp_path, [_group("cat >/dev/null; echo traceback here >&2; exit 1")])
    code, _, err = _call(project, "proc start --name p -- ls")
    assert code == 2
    assert "exited with status 1" in err
    assert "refused" in err
    assert "traceback here" in err


def test_a_missing_hook_program_fails_closed(tmp_path):
    project = _project(tmp_path, [_group("/nonexistent/hook-program")])
    code, _, err = _call(project, "proc start --name p -- ls")
    assert code == 2
    assert "refused" in err


def test_a_hook_that_times_out_fails_closed_with_a_message(tmp_path):
    project = _project(tmp_path, [_group("sleep 30", timeout=1)])
    code, _, err = _call(project, "proc start --name p -- ls")
    assert code == 2
    assert "timed out after 1s" in err
    assert "refused" in err


def test_a_project_without_settings_passes(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    assert _call(bare, "proc start --name p -- ls") == (0, "", "")
    assert _call(None, "proc start --name p -- ls") == (0, "", "")


def test_a_command_without_proc_start_never_reads_the_project(tmp_path):
    project = _project(tmp_path, [_group("exit 2")])
    assert _call(project, "ls -la") == (0, "", "")
    assert _call(project, "proc wait job") == (0, "", "")
    assert _call(project, "echo 'proc start --name x -- ls'") == (0, "", "")


def test_other_tools_are_not_unwrapped(tmp_path):
    project = _project(tmp_path, [_group("exit 2", matcher="*")])
    assert _call(project, "proc start --name p -- ls", tool="Write") == (0, "", "")


def test_project_dir_is_found_from_cwd_when_the_env_lacks_it(tmp_path):
    project = _project(tmp_path, [_group("cat >/dev/null; echo found >&2; exit 2")])
    payload = {
        "cwd": str(project / ".claude"),
        "tool_name": "Bash",
        "tool_input": {"command": "proc start --name p -- ls"},
    }
    err = io.StringIO()
    code = claude_hook.main(io.StringIO(json.dumps(payload)), io.StringIO(), err, {"PATH": "/bin"})
    assert code == 2
    assert "found" in err.getvalue()


def test_a_hook_rewrite_is_not_applied_and_says_so(tmp_path):
    rewrite = (
        "cat >/dev/null; printf '%s' "
        '\'{"hookSpecificOutput":{"updatedInput":{"command":"echo safe"}}}\''
    )
    project = _project(tmp_path, [_group(rewrite)])
    code, out, _ = _call(project, "proc start --name p -- ls")
    assert code == 0
    special = json.loads(out)["hookSpecificOutput"]
    assert "permissionDecision" not in special
    assert "no rewrite was applied" in special["additionalContext"]
    assert "echo safe" in special["additionalContext"]


def test_the_rewrite_notice_names_each_hook_and_its_proposed_command(tmp_path):
    def rewriting(name: str, new: str) -> str:
        script = tmp_path / name
        script.write_text(
            "#!/bin/sh\ncat >/dev/null\n"
            f'printf \'%s\' \'{{"hookSpecificOutput":{{"updatedInput":{{"command":"{new}"}}}}}}\'\n'
        )
        script.chmod(0o755)
        return f'"{script}"'

    project = _project(
        tmp_path,
        [_group(rewriting("strip_cd.sh", "ls -l")), _group(rewriting("add_prefix.sh", "nice ls"))],
    )
    code, out, _ = _call(project, "proc start --name p -- cd /x; ls")
    assert code == 0
    notice = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert f"- hook {tmp_path}/strip_cd.sh: ls -l" in notice
    assert f"- hook {tmp_path}/add_prefix.sh: nice ls" in notice


def _rewrite_notice(tmp_path: Path, hook_command: str) -> str:
    project = _project(tmp_path, [_group(hook_command)])
    code, out, _ = _call(project, "proc start --name p -- ls")
    assert code == 0
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


_REWRITE_OUTPUT = (
    'printf \'%s\' \'{"hookSpecificOutput":{"updatedInput":{"command":"echo safe"}}}\''
)


def test_a_rewriting_hook_is_named_by_its_script_when_the_command_runs_one(tmp_path):
    hooks = tmp_path / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    script = hooks / "rewrite_it.sh"
    script.write_text("#!/bin/sh\ncat >/dev/null\n" + _REWRITE_OUTPUT + "\n")
    script.chmod(0o755)
    notice = _rewrite_notice(
        tmp_path, f'd="$X"; h="{tmp_path}"; exec "$h/.claude/hooks/rewrite_it.sh"'
    )
    assert "- hook $h/.claude/hooks/rewrite_it.sh: echo safe" in notice


def test_a_rewriting_hook_without_a_script_is_named_by_its_one_line_command(tmp_path):
    notice = _rewrite_notice(tmp_path, f"cat >/dev/null  &&\n  {_REWRITE_OUTPUT}")
    assert f"- hook cat >/dev/null && {_REWRITE_OUTPUT}: echo safe" in notice


def test_a_long_hook_command_is_shortened_in_the_rewrite_notice(tmp_path):
    notice = _rewrite_notice(tmp_path, f"cat >/dev/null; {_REWRITE_OUTPUT}; : {'x ' * 200}")
    line = next(ln for ln in notice.splitlines() if ln.startswith("- hook "))
    name = line.removeprefix("- hook ").removesuffix(": echo safe")
    assert name.endswith("...")
    assert len(name) <= 160


def test_unparseable_input_is_a_refusal_with_a_reason():
    err = io.StringIO()
    code = claude_hook.main(io.StringIO("proc start {not json"), io.StringIO(), err, {})
    assert code == 2
    assert "refused" in err.getvalue()


def test_runs_as_a_module_end_to_end(tmp_path):
    import subprocess

    project = _project(tmp_path, [_group(DENY_WRITES)])
    payload = json.dumps(
        {
            "cwd": str(project),
            "tool_name": "Bash",
            "tool_input": {"command": "proc start --name p -- psql -c 'delete from t'"},
        }
    )
    src = str(Path(__file__).resolve().parents[1] / "src")
    r = subprocess.run(
        [sys.executable, "-I", "-m", "proc_lib.claude_hook"],
        input=payload,
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": src, "CLAUDE_PROJECT_DIR": str(project)},
        timeout=30,
    )
    # -I ignores PYTHONPATH: the package resolves only when installed, so fall back to cwd
    if r.returncode != 2 and "No module named" in r.stderr:
        r = subprocess.run(
            [sys.executable, "-m", "proc_lib.claude_hook"],
            input=payload,
            capture_output=True,
            text=True,
            env={"PATH": os.environ["PATH"], "PYTHONPATH": src, "CLAUDE_PROJECT_DIR": str(project)},
            timeout=30,
        )
    assert r.returncode == 2
    assert "BLOCKED: db write" in r.stderr


# ------------------------------------------------------------------ install / uninstall / ensure


def _seat(tmp_path: Path, settings=None) -> Path:
    seat = tmp_path / "seat"
    seat.mkdir()
    if settings is not None:
        (seat / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")
    return seat


def test_install_adds_one_entry_and_keeps_the_rest(tmp_path):
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "other.sh"}]}
    seat = _seat(tmp_path, {"model": "x", "hooks": {"PreToolUse": [other], "Stop": []}})
    st = hook_judge.install(seat)
    assert st.installed
    data = json.loads((seat / "settings.json").read_text())
    assert data["model"] == "x"
    assert data["hooks"]["Stop"] == []
    assert data["hooks"]["PreToolUse"][0] == other
    ours = data["hooks"]["PreToolUse"][1]
    assert ours["matcher"] == "Bash"
    assert ours["hooks"][0]["command"] == hook_judge.hook_command()
    assert list(seat.glob("settings.json.bak-*-proc-hook"))
    hook_judge.install(seat)  # idempotent
    again = json.loads((seat / "settings.json").read_text())
    assert again == data


def test_install_creates_settings_when_missing(tmp_path):
    seat = tmp_path / "newseat"
    assert hook_judge.install(seat).installed
    assert (seat / "settings.json").is_file()


def test_uninstall_removes_only_ours_and_prunes_empties(tmp_path):
    seat = _seat(tmp_path, {"model": "x"})
    hook_judge.install(seat)
    st = hook_judge.uninstall(seat)
    assert not st.installed
    assert json.loads((seat / "settings.json").read_text()) == {"model": "x"}
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "other.sh"}]}
    seat2 = tmp_path / "seat2"
    seat2.mkdir()
    (seat2 / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [other]}}))
    hook_judge.install(seat2)
    hook_judge.uninstall(seat2)
    assert json.loads((seat2 / "settings.json").read_text()) == {"hooks": {"PreToolUse": [other]}}


def test_ensure_installs_once_and_respects_an_uninstall(tmp_path):
    seat = _seat(tmp_path, {})
    base = tmp_path / "base"
    base.mkdir()
    env = {"CLAUDE_CONFIG_DIR": str(seat)}
    note = REAL_ENSURE_HOOK(base, env)
    assert note and str(seat / "settings.json") in note
    assert hook_judge.status(seat).installed
    assert REAL_ENSURE_HOOK(base, env) is None  # already there
    hook_judge.uninstall(seat, base)
    assert REAL_ENSURE_HOOK(base, env) is None  # switched off by the user
    assert not hook_judge.status(seat).installed
    hook_judge.install(seat, base)  # an explicit install clears the opt-out
    hook_judge.uninstall(seat)
    assert REAL_ENSURE_HOOK(base, env) is not None


def test_ensure_repoints_an_entry_whose_interpreter_is_gone(tmp_path):
    seat = _seat(tmp_path, {})
    base = tmp_path / "base"
    base.mkdir()
    env = {"CLAUDE_CONFIG_DIR": str(seat)}
    hook_judge.install(seat)
    data = json.loads((seat / "settings.json").read_text())
    data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = (
        f"{tmp_path}/gone/bin/python -I -m {hook_judge.HOOK_MARK}"
    )
    (seat / "settings.json").write_text(json.dumps(data))
    assert REAL_ENSURE_HOOK(base, env) is not None
    assert hook_judge.status(seat).note == ""
    assert REAL_ENSURE_HOOK(base, env) is None


def test_ensure_does_not_create_settings_for_a_seat_without_any(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    assert REAL_ENSURE_HOOK(base, {"CLAUDE_CONFIG_DIR": str(tmp_path / "none")}) is None
    assert not (tmp_path / "none").exists()


def test_ensure_never_raises_on_a_broken_settings_file(tmp_path):
    seat = tmp_path / "seat"
    seat.mkdir()
    (seat / "settings.json").write_text("{broken")
    assert REAL_ENSURE_HOOK(tmp_path, {"CLAUDE_CONFIG_DIR": str(seat)}) is None


def test_start_runs_ensure_and_reports_an_install(monkeypatch, capsys):
    monkeypatch.setattr(hook_judge, "ensure", lambda base, env=None: "proc: installed its hook")
    assert (
        core.cmd_start(ns(name="j", timeout=None, cwd="/tmp", env=[], at=None, cmd=["--", "true"]))
        == 0
    )
    assert "proc: installed its hook" in capsys.readouterr().err


def test_cli_hook_install_status_uninstall(tmp_path, capsys):
    seat = tmp_path / "seat"
    assert cli.main(["hook", "status", "--config-dir", str(seat)]) == core.EXIT_HOOK_MISSING
    assert cli.main(["hook", "install", "--config-dir", str(seat)]) == 0
    assert cli.main(["hook", "status", "--config-dir", str(seat)]) == 0
    assert "installed:" in capsys.readouterr().out
    assert cli.main(["hook", "uninstall", "--config-dir", str(seat)]) == 0
    assert cli.main(["hook", "status", "--config-dir", str(seat)]) == core.EXIT_HOOK_MISSING


def test_cli_hook_unwrap_reads_stdin(tmp_path, monkeypatch, capsys):
    project = _project(tmp_path, [_group("cat >/dev/null; echo nope >&2; exit 2")])
    payload = {
        "cwd": str(project),
        "tool_name": "Bash",
        "tool_input": {"command": "proc start --name p -- ls"},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    assert cli.main(["hook", "unwrap"]) == 2
    assert "nope" in capsys.readouterr().err


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "$HOME/.claude/hooks/_hook_wrap.sh ",
        "bash $HOME/.claude/hooks/_hook_wrap.sh ",
        "absolute",
        "bash-absolute",
    ],
)
def test_wrapped_hook_status_and_repoint(tmp_path, prefix):
    seat = _seat(tmp_path, {})
    wrapper = shlex.quote(str(tmp_path / ".claude/hooks/_hook_wrap.sh"))
    if prefix == "absolute":
        prefix = wrapper + " "
    elif prefix == "bash-absolute":
        prefix = "bash " + wrapper + " "
    hook_judge.install(seat)
    data = json.loads((seat / "settings.json").read_text())
    entry = data["hooks"]["PreToolUse"][0]["hooks"][0]
    entry["command"] = prefix + hook_judge.hook_command()
    (seat / "settings.json").write_text(json.dumps(data))
    assert hook_judge.status(seat).note == ""
    env = {"CLAUDE_CONFIG_DIR": str(seat)}
    assert REAL_ENSURE_HOOK(tmp_path / "registry", env) is None
    entry["command"] = prefix + hook_judge.hook_command(str(tmp_path / "gone/bin/python"))
    (seat / "settings.json").write_text(json.dumps(data))
    assert hook_judge.status(seat).note == "pinned to another interpreter"
    assert REAL_ENSURE_HOOK(tmp_path / "registry", env) is not None
    assert hook_judge.status(seat).note == ""


@pytest.fixture(params=[False, True], ids=["default-source", "custom-source"])
def shared_settings(tmp_path, monkeypatch, request):
    seats = [tmp_path / ".claude", tmp_path / ".second-seat"]
    for index, seat in enumerate(seats):
        seat.mkdir()
        (seat / "settings.json").write_text(json.dumps({"model": str(index), "hooks": {}}))
    source = seats[0] / "settings.shared.json"
    if request.param:
        custom_seat = tmp_path / "custom-seat"
        seats[1].rename(custom_seat)
        seats[1] = custom_seat
        source = tmp_path / "custom.shared.json"
        monkeypatch.setenv("SEAT_SETTINGS_SOURCE", str(source))
    monkeypatch.setenv("SEAT_SETTINGS_SEATS", ":".join(map(str, seats)))
    source.write_text(json.dumps({"hooks": {}}))
    calls = []

    def sync(argv, **kwargs):
        assert argv == [sys.executable, str(seats[0] / "hooks/seat_settings_sync.py"), "sync"]
        assert kwargs["check"] and kwargs["timeout"] == 30
        want = json.loads(source.read_text())
        # A generated file must still equal what the previous sync wrote.
        for index, seat in enumerate(seats):
            old = json.loads((seat / "settings.json").read_text())
            assert old == {"model": str(index), **(calls[-1] if calls else {"hooks": {}})}
            (seat / "settings.json").write_text(json.dumps({**old, **want}))
        calls.append(want)

    monkeypatch.setattr(claude_hook.subprocess, "run", sync)
    return seats, source, calls


def _registered_commands(source: Path) -> list[str]:
    """The hook commands the shared source registers, found by the exported mark."""
    groups = json.loads(source.read_text()).get("hooks", {}).get("PreToolUse", [])
    return [
        entry["command"]
        for group in groups
        for entry in group.get("hooks", [])
        if hook_judge.HOOK_MARK in entry.get("command", "")
    ]


def test_shared_install_syncs_both_seats_and_uninstall_stays_removed(shared_settings):
    seats, source, calls = shared_settings
    assert hook_judge.install(seats[1]).note == ""
    command = _registered_commands(source)[0]
    assert command == "$HOME/.claude/hooks/_hook_wrap.sh " + hook_judge.hook_command()
    assert all(hook_judge.status(seat).installed for seat in seats)
    assert len(calls) == 1
    hook_judge.uninstall(seats[0])
    assert not _registered_commands(source)
    assert all(not hook_judge.status(seat).installed for seat in seats)


def test_shared_ensure_repairs_source_and_does_not_resync_working_hook(shared_settings, tmp_path):
    seats, source, calls = shared_settings
    data = json.loads(source.read_text())
    data["hooks"] = {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "bash $HOME/.claude/hooks/_hook_wrap.sh "
                        + hook_judge.hook_command(str(tmp_path / "gone/python")),
                    }
                ],
            }
        ]
    }
    source.write_text(json.dumps(data))
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(seats[1])}
    assert REAL_ENSURE_HOOK(tmp_path / "registry", env) is not None
    assert all(hook_judge.status(seat).note == "" for seat in seats)
    assert REAL_ENSURE_HOOK(tmp_path / "registry", env) is None
    assert len(calls) == 1


def test_unmanaged_seat_ignores_existing_shared_source(shared_settings, tmp_path):
    _, source, calls = shared_settings
    before = source.read_bytes()
    seat = _seat(tmp_path, {})
    assert hook_judge.install(seat).installed
    assert source.read_bytes() == before
    assert calls == []


def test_default_seats_are_only_the_claude_dir(tmp_path, monkeypatch):
    from proc_lib import hook_judge

    monkeypatch.delenv("SEAT_SETTINGS_SEATS", raising=False)
    monkeypatch.delenv("SEAT_SETTINGS_SOURCE", raising=False)
    source = tmp_path / ".claude" / "settings.shared.json"
    source.parent.mkdir()
    source.write_text("{}")
    other = tmp_path / ".other-seat"
    other.mkdir()
    assert hook_judge._shared_source(tmp_path / ".claude", {}) == source
    assert hook_judge._shared_source(other, {}) is None


@pytest.mark.parametrize(
    "error", [subprocess.CalledProcessError(1, ["sync"]), subprocess.TimeoutExpired(["sync"], 30)]
)
def test_shared_sync_failure_is_reported_without_editing_generated_files(
    shared_settings, monkeypatch, tmp_path, error
):
    seats, source, _ = shared_settings
    before = [(seat / "settings.json").read_bytes() for seat in seats]

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(claude_hook.subprocess, "run", fail)
    with pytest.raises(OSError, match="Shared settings sync failed") as raised:
        hook_judge.install(seats[0])
    assert raised.value.__cause__ is error
    assert [(seat / "settings.json").read_bytes() for seat in seats] == before
    # Automatic start remains best effort and retries syncing the unapplied source.
    assert (
        REAL_ENSURE_HOOK(tmp_path / "registry", {**os.environ, "CLAUDE_CONFIG_DIR": str(seats[0])})
        is None
    )
    assert _registered_commands(source)


# ------------------------------------------------------------------ the common path stays light


def _bash_payload(command: str, **extra) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, **extra})


def _judged_command(tmp_path: Path, raw: str) -> str | None:
    """Runs `main` on the raw payload with a project check that records the command it is
    shown; returns that command, or None when the check never ran (the call was not judged)."""
    tmp_path = Path(tempfile.mkdtemp(dir=tmp_path))
    dump = tmp_path / "judged.txt"
    project = _project(tmp_path, [_group(f"cat > {dump}")])
    env = {"PATH": os.environ["PATH"], "CLAUDE_PROJECT_DIR": str(project)}
    code = claude_hook.main(io.StringIO(raw), io.StringIO(), io.StringIO(), env)
    assert code == 0
    return json.loads(dump.read_text())["tool_input"]["command"] if dump.exists() else None


@pytest.mark.parametrize(("command", "inner"), UNWRAP_FORMS)
def test_the_fast_test_admits_every_form_the_judge_unwraps(tmp_path, command, inner):
    """Every form the unwrap tests use reaches the judge, with the same inner command."""
    assert _judged_command(tmp_path, _bash_payload(command)) == inner


@pytest.mark.parametrize("command", NOT_UNWRAPPED)
def test_the_fast_test_skips_a_command_the_judge_would_not_unwrap(tmp_path, command):
    assert _judged_command(tmp_path, _bash_payload(command)) is None


def test_the_fast_test_reads_the_command_not_the_payload(tmp_path):
    """`proc` and `start` elsewhere in the payload (a description, a cwd under /proc) do not
    send an ordinary command to the judge."""
    payload = _bash_payload("ls -la", cwd="/proc/self", description="restart the thing")
    assert _judged_command(tmp_path, payload) is None
    other_tool = json.dumps(
        {"tool_name": "Read", "tool_input": {"command": "proc start --name p -- make"}}
    )
    assert _judged_command(tmp_path, other_tool) is None


def test_the_fast_test_sees_a_command_written_with_json_escapes(tmp_path):
    """`\\u0070roc` is `proc`: the pre-filter on the raw text must not skip it."""
    raw = '{"tool_name": "Bash", "tool_input": {"command": "\\u0070roc start --name p -- make"}}'
    assert _judged_command(tmp_path, raw) == "make"


@pytest.mark.parametrize(
    "raw", ["proc start {not json", '{"a": 1} trailing proc start', '{"x": 1, "y": "proc start",}']
)
def test_the_fast_test_refuses_an_unreadable_payload(raw):
    err = io.StringIO()
    assert claude_hook.main(io.StringIO(raw), io.StringIO(), err, {}) == 2
    assert "refused" in err.getvalue()


HEAVY_MODULES = (
    "dataclasses",
    "concurrent.futures",
    "logging",
    "subprocess",
    "re",
    "shlex",
    "datetime",
    "signal",
    "inspect",
    "json",
    "pathlib",
    "proc_lib.hook_judge",
    "proc_lib.core",
)


@pytest.mark.parametrize(
    "command",
    ["ls -la", "grep start /proc/cpuinfo", "echo restart; cat /proc/self/status"],
)
def test_the_common_path_imports_no_heavy_module(command):
    """A call that starts no job costs the interpreter start and one scan: none of the heavy
    standard-library modules (or the judging module) is imported."""
    import subprocess

    src = str(Path(__file__).resolve().parents[1] / "src")
    program = (
        "import io, sys\n"
        f"sys.path.insert(0, {src!r})\n"
        "before = set(sys.modules)\n"
        "from proc_lib import claude_hook\n"
        "code = claude_hook.main(io.StringIO(sys.argv[1]), io.StringIO(), io.StringIO(), {})\n"
        "new = set(sys.modules) - before\n"
        f"print(code, sorted(m for m in {HEAVY_MODULES!r} if m in new))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", program, _bash_payload(command)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == "0 []", result.stderr


def test_an_unknown_name_on_the_hook_module_raises_attribute_error():
    """The lazy lookup that forwards names to the judging module still refuses a name neither
    module has."""
    with pytest.raises(AttributeError):
        claude_hook.no_such_name  # noqa: B018
