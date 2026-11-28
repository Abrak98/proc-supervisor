"""Agent-facing output surface: the verdict-first `status`, `--json` views,
`wait` exit codes, `start --env`, and compatibility with registry entries
written before the `env` field existed."""

from __future__ import annotations

import json
import re

from proc_lib import cli, core

from .conftest import ns, start, wait_terminal

LEGACY_KEYS = {
    "name",
    "cmd",
    "cwd",
    "pid",
    "pgid",
    "start_ts",
    "start_iso",
    "pid_starttime",
    "timeout",
    "status",
    "exit_code",
    "end_ts",
    "log_path",
    "rc_path",
}


def _record(name):
    """The job's registry record as stored on disk, or None when there is none."""
    path = core.BASE / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def _status_out(capsys, name, **kw) -> str:
    capsys.readouterr()
    assert core.cmd_status(ns(name=name, **kw)) == 0
    return capsys.readouterr().out


def test_status_verdict_shows_two_most_significant_units(capsys):
    start("hd", "bash", "-c", "exit 0")
    wait_terminal("hd")
    path = core.BASE / "hd.json"
    meta = json.loads(path.read_text())
    for secs, shown in [
        (12, "12s"),
        (41 * 60 + 5, "41m5s"),
        (2 * 3600 + 13 * 60 + 59, "2h13m"),
        (3 * 86400 + 4 * 3600, "3d4h"),
    ]:
        meta["end_ts"] = meta["start_ts"] + secs
        path.write_text(json.dumps(meta))
        first = _status_out(capsys, "hd").splitlines()[0]
        assert first == f"hd: exited rc=0 after {shown}"


def test_status_first_line_is_running_verdict(capsys):
    start("r", "bash", "-c", "sleep 5")
    lines = _status_out(capsys, "r").splitlines()
    pid = _record("r")["pid"]
    assert re.fullmatch(rf"r: running pid={pid} for \d+s", lines[0])
    assert lines[1] == f"  log: {core.BASE / 'r.log'}"
    core.cmd_kill(ns(name="r", force=True))


def test_timeout_and_proc_kill_verdicts_and_wait_job_failed(capsys):
    start("to", "bash", "-c", "sleep 5", timeout=1)
    wait_terminal("to", deadline_s=8)
    assert _status_out(capsys, "to").splitlines()[0].startswith("to: killed (timeout) after ")
    assert core.cmd_wait(ns(name="to", timeout=1)) == core.EXIT_JOB_FAILED

    start("k", "bash", "-c", "sleep 5")
    core.cmd_kill(ns(name="k", force=True))
    assert _status_out(capsys, "k").splitlines()[0].startswith("k: killed (proc kill) after ")
    assert core.cmd_wait(ns(name="k", timeout=1)) == core.EXIT_JOB_FAILED


def test_status_json_puts_state_first(capsys):
    start("j", "bash", "-c", "exit 3")
    wait_terminal("j")
    data = json.loads(_status_out(capsys, "j", json=True))
    assert list(data)[:4] == ["name", "status", "alive", "exit_code"]
    assert data["status"] == "exited"
    assert data["alive"] is False
    assert data["exit_code"] == 3
    assert data["log_path"] == str(core.BASE / "j.log")
    assert set(data) >= LEGACY_KEYS


def test_status_json_alive_true_for_running_job(capsys):
    start("ja", "bash", "-c", "sleep 5")
    data = json.loads(_status_out(capsys, "ja", json=True))
    assert data["status"] == "running"
    assert data["alive"] is True
    core.cmd_kill(ns(name="ja", force=True))


def test_list_json_is_array_of_status_objects(capsys):
    assert core.cmd_list(ns(json=True)) == 0
    assert json.loads(capsys.readouterr().out) == []

    start("l1", "bash", "-c", "exit 0")
    start("l2", "bash", "-c", "sleep 5")
    wait_terminal("l1")
    capsys.readouterr()
    assert cli.main(["list", "--all", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows] == ["l2", "l1"]  # running first
    assert rows[0]["alive"] is True and rows[1]["exit_code"] == 0
    core.cmd_kill(ns(name="l2", force=True))


def test_wait_exit_zero_only_on_clean_exit(capsys):
    start("ok", "bash", "-c", "exit 0")
    wait_terminal("ok")
    assert core.cmd_wait(ns(name="ok", timeout=10)) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("ok: exited rc=0 after ")


def test_wait_nonzero_rc_is_job_failed(capsys):
    start("bad", "bash", "-c", "exit 3")
    wait_terminal("bad")
    capsys.readouterr()
    assert cli.main(["wait", "bad", "--timeout", "10"]) == core.EXIT_JOB_FAILED
    assert capsys.readouterr().out.startswith("bad: exited rc=3 after ")


def test_wait_cap_prints_running_verdict(capsys):
    start("slow", "bash", "-c", "sleep 5")
    capsys.readouterr()
    assert core.cmd_wait(ns(name="slow", timeout=0)) == core.EXIT_WAIT_TIMEOUT
    out = capsys.readouterr().out
    assert re.fullmatch(r"slow: running pid=\d+ for \d+s \(wait timed out\)\n", out)
    core.cmd_kill(ns(name="slow", force=True))


def test_start_env_reaches_job_and_is_recorded(capsys):
    start("e", "bash", "-c", 'echo "foo=$FOO bar=$BAR"', env=["FOO=1", "BAR=a=b"])
    wait_terminal("e")
    assert "foo=1 bar=a=b" in (core.BASE / "e.log").read_text()
    assert _record("e")["env"] == {"FOO": "1", "BAR": "a=b"}
    assert "  env: FOO=1 BAR=a=b" in _status_out(capsys, "e").splitlines()


def test_start_env_secret_values_masked_in_registry_but_real_for_job(capsys):
    secret_env = ["API_TOKEN=t0k", "DB_PASSWORD=pw", "my_secret=s", "AWS_KEY=k", "PLAIN=v"]
    start("sec", "bash", "-c", 'echo "$API_TOKEN $DB_PASSWORD $my_secret $AWS_KEY"', env=secret_env)
    wait_terminal("sec")
    assert "t0k pw s k" in (core.BASE / "sec.log").read_text()
    raw = (core.BASE / "sec.json").read_text()
    for value in ("t0k", '"pw"', '"s"', '"k"'):
        assert value not in raw
    recorded = _record("sec")["env"]
    assert recorded == {
        "API_TOKEN": "***",
        "DB_PASSWORD": "***",
        "my_secret": "***",
        "AWS_KEY": "***",
        "PLAIN": "v",
    }
    human = _status_out(capsys, "sec")
    assert "t0k" not in human and "API_TOKEN=***" in human
    assert json.loads(_status_out(capsys, "sec", json=True))["env"] == recorded


def test_start_env_malformed_pair_is_usage_error():
    for bad in ("NOEQUALS", "=v", "1X=v", "A-B=v"):
        assert start("bad", "true", env=[bad]) == core.EXIT_USAGE
        assert _record("bad") is None


def test_start_env_flag_is_repeatable_via_cli():
    argv = ["start", "--name", "ce", "--cwd", "/tmp", "--env", "A=1", "--env", "B=2"]
    assert cli.main([*argv, "--", "bash", "-c", "exit 0"]) == 0
    wait_terminal("ce")
    assert _record("ce")["env"] == {"A": "1", "B": "2"}


def _make_legacy(name: str) -> None:
    """Rewrite a job's registry entry into the pre-`env` format the previously
    installed version wrote, byte-for-byte in key set."""
    path = core.BASE / f"{name}.json"
    meta = json.loads(path.read_text())
    del meta["env"]
    assert set(meta) == LEGACY_KEYS
    path.write_text(json.dumps(meta, indent=2))


def test_legacy_running_entry_is_listed_waited_and_killed(capsys):
    start("old", "bash", "-c", "sleep 5")
    _make_legacy("old")

    capsys.readouterr()
    assert core.cmd_list(ns()) == 0
    assert "old" in capsys.readouterr().out
    assert core.cmd_list(ns(json=True)) == 0
    assert json.loads(capsys.readouterr().out)[0]["alive"] is True

    lines = _status_out(capsys, "old").splitlines()
    assert lines[0].startswith("old: running pid=")
    assert not any(line.startswith("  env:") for line in lines)

    assert core.cmd_wait(ns(name="old", timeout=0)) == core.EXIT_WAIT_TIMEOUT
    assert core.cmd_kill(ns(name="old", force=True)) == 0
    assert _record("old")["status"] == core.KILLED
    assert core.cmd_wait(ns(name="old", timeout=1)) == core.EXIT_JOB_FAILED


def test_legacy_exited_entry_waits_clean():
    start("oldok", "bash", "-c", "exit 0")
    _make_legacy("oldok")
    wait_terminal("oldok")
    assert core.cmd_wait(ns(name="oldok", timeout=10)) == 0
