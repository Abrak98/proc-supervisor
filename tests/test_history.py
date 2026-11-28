"""A finished job stays at its flat path for at least a day; the first proc call of a UTC day
moves the jobs that ended before the previous day began into `history/<UTC day of end>/`.
Lookups find a job in both places; whole day folders go past the retention; `proc archive`
moves an old flat registry once; `list --exited --since` reads the live folder and the day
folders its window covers."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from proc_lib import cli, core

from .conftest import ns, reap_terminal, start

T0 = float(int(time.time()))  # "now" of the tests; the clock fixture pins the engine to it
DAY = 86400.0


@pytest.fixture
def clock(monkeypatch):
    now = [T0]
    monkeypatch.setattr(core, "_now", lambda: now[0])
    return now


def hist() -> Path:
    return core.BASE / "history"


def seed(name, end_ts, status="exited", *, home=None, log="line\n", live=False):
    """A finished record (+ log, .rc) as the engine writes it, in a history day folder or,
    with `live`, flat in the live folder as a pre-history registry has them."""
    folder = core.BASE if live else (hist() / core._day(end_ts))
    folder.mkdir(parents=True, exist_ok=True)
    meta = {
        "name": name,
        "cmd": ["true"],
        "cwd": "/tmp",
        "pid": 1,
        "start_ts": end_ts - 5,
        "end_ts": end_ts,
        "status": status,
        "exit_code": 0,
        "log_path": str(folder / f"{name}.log"),
        "rc_path": str(folder / f"{name}.rc"),
    }
    (folder / f"{name}.json").write_text(json.dumps(meta))
    (folder / f"{name}.log").write_text(log)
    (folder / f"{name}.rc").write_text("0\n")
    os.utime(folder / f"{name}.json", (end_ts, end_ts))  # the record is written at the end
    return meta


def live_names() -> set[str]:
    return {p.name for p in core.BASE.iterdir() if p.suffix in (".json", ".log", ".rc", ".rss")}


# --- a job's end leaves it in place; the day's sweep moves it ---------------- #


def test_a_job_that_ends_stays_at_its_path_after_wait(capsys):
    start("job1", "bash", "-c", "echo hello")
    assert core.cmd_wait(ns(name="job1", timeout=30)) == 0
    m = core._load("job1")
    for ext in ("json", "log", "rc"):
        assert (core.BASE / f"job1.{ext}").is_file()
    assert m["log_path"] == str(core.BASE / "job1.log") and not hist().exists()
    assert "hello" in (core.BASE / "job1.log").read_text()


def test_status_logs_and_list_all_find_a_finished_job_before_and_after_the_sweep(clock, capsys):
    start("job2", "bash", "-c", "echo found-me")
    reap_terminal("job2")  # no wall-clock deadline: it ends when the wrapper is reaped
    for sweep in (False, True):
        if sweep:
            clock[0] = T0 + 2 * DAY  # the first call of a day two days on
            core.housekeeping()
            assert list(hist().glob("*/job2.json")) and not (core.BASE / "job2.json").exists()
        capsys.readouterr()
        assert core.cmd_status(ns(name="job2", json=False)) == 0
        assert "job2: exited rc=0" in capsys.readouterr().out
        assert core.cmd_logs(ns(name="job2", tail=None, since=None, follow=False)) == 0
        assert "found-me" in capsys.readouterr().out
        assert core.cmd_list(ns(all=True, json=True)) == 0
        assert [r["name"] for r in json.loads(capsys.readouterr().out)] == ["job2"]


def test_a_killed_job_stays_at_its_path_too():
    start("victim", "bash", "-c", "sleep 30")
    core.cmd_kill(ns(name="victim", force=True))
    assert (core.BASE / "victim.json").is_file() and not hist().exists()
    assert core._load("victim")["status"] == core.KILLED


def test_a_running_job_never_moves_and_a_finished_name_is_not_reused(clock):
    start("alive", "bash", "-c", "sleep 30")
    clock[0] = T0 + 5 * DAY
    core.housekeeping()
    assert (core.BASE / "alive.json").is_file() and not list(hist().glob("*/alive.*"))
    core.cmd_kill(ns(name="alive", force=True))
    assert start("alive", "true") == core.EXIT_USAGE  # still refused: `proc rm` first
    assert core.cmd_rm(ns(name=["alive"], force=False)) == 0
    assert not (core.BASE / "alive.json").exists()
    assert start("alive", "true") == 0


def test_the_sweep_moves_only_jobs_that_ended_before_the_previous_day_began(clock):
    yesterday = core._day(T0 - DAY)
    stamp = datetime.fromisoformat(yesterday + "T00:00:00+00:00").timestamp()
    seed("just-in", stamp, live=True)  # ended at the start of yesterday: kept flat
    seed("too-old", stamp - 1, live=True)  # a second earlier: moves
    start("runner", "bash", "-c", "sleep 30")
    later = datetime.fromtimestamp(T0 + 600, tz=UTC).isoformat()
    start("sched", "true", at=later)
    core.housekeeping()
    assert (core.BASE / "just-in.json").is_file() and (core.BASE / "just-in.log").is_file()
    day = hist() / core._day(stamp - 1)
    assert (day / "too-old.json").is_file() and (day / "too-old.log").is_file()
    assert not (core.BASE / "too-old.json").exists() and not (core.BASE / "too-old.rc").exists()
    assert core._load("too-old")["log_path"] == str(day / "too-old.log")
    assert (core.BASE / "runner.json").is_file() and (core.BASE / "sched.json").is_file()
    core.cmd_kill(ns(name="runner", force=True))


def test_rm_and_clean_remove_a_history_job_with_its_log():
    seed("old-a", T0 - 3 * 3600)
    seed("old-b", T0 - 2 * 3600)  # in a day folder here, as after a sweep
    assert core.cmd_rm(ns(name=["old-a"], force=False)) == 0
    assert not list(hist().glob("*/old-a.*"))
    assert core.cmd_clean(ns(older_than=1)) == 0
    assert core._load("old-b") is None and not list(hist().glob("*/old-b.*"))


def test_a_crash_that_left_the_record_in_both_places_is_finished_by_the_next_save(clock):
    meta = seed("dup", T0 - 100)
    (core.BASE / "dup.json").write_text(json.dumps(meta))  # the live copy a crash left
    assert core._load("dup")["name"] == "dup"
    core._with_lock(lambda: core._archive(core._load("dup")))
    assert not (core.BASE / "dup.json").exists()
    assert (hist() / core._day(T0 - 100) / "dup.json").is_file()


# --- retention: whole day folders, first call of a new day ------------------ #


def test_the_first_call_of_a_new_day_deletes_whole_folders_past_the_bound(clock):
    seed("keep", T0 - 14 * DAY)  # exactly 14 days back: kept
    seed("gone", T0 - 15 * DAY)  # 15 days back: its whole folder goes
    core.housekeeping()
    assert (hist() / core._day(T0 - 14 * DAY)).is_dir()
    assert not (hist() / core._day(T0 - 15 * DAY)).exists()
    assert (hist() / core.HISTORY_STAMP).read_text() == core._day(T0)


def test_the_stamp_makes_the_second_call_of_a_day_do_nothing_and_a_new_day_sweeps_again(clock):
    seed("today", T0)
    core.housekeeping()
    seed("late", T0 - 20 * DAY)  # appears after today's sweep: today's stamp holds
    core.housekeeping()
    assert (hist() / core._day(T0 - 20 * DAY)).is_dir()
    clock[0] = T0 + DAY
    core.housekeeping()
    assert not (hist() / core._day(T0 - 20 * DAY)).exists()


def test_the_bound_is_read_from_the_registry_own_config(clock):
    (core.BASE / core.HISTORY_DAYS_CONFIG).write_text("# days\n3\n")
    seed("a", T0 - 3 * DAY)
    seed("b", T0 - 4 * DAY)
    core.housekeeping()
    assert (hist() / core._day(T0 - 3 * DAY)).is_dir()
    assert not (hist() / core._day(T0 - 4 * DAY)).exists()
    (core.BASE / core.HISTORY_DAYS_CONFIG).write_text("nonsense")
    assert core._history_days_bound() == core.HISTORY_DAYS_DEFAULT == 14


def test_an_ordinary_command_sweeps_but_the_hook_path_does_not(monkeypatch, clock):
    seed("old", T0 - 30 * DAY)
    cli.main(["hook", "status", "--config-dir", str(core.BASE / "cfg")])
    assert (hist() / core._day(T0 - 30 * DAY)).is_dir()
    cli.main(["list"])
    assert not (hist() / core._day(T0 - 30 * DAY)).exists()


# --- the one-time move of a flat registry ----------------------------------- #


def test_archive_moves_old_flat_records_and_leaves_live_ones(clock, capsys):
    seed("fin", T0 - 3600, live=True)
    seed("stale", T0 - 40 * DAY, live=True)  # past the bound: deleted, not moved
    start("runner", "bash", "-c", "sleep 30")
    assert core.cmd_archive(ns()) == 0
    assert "archived 2 finished job(s)" in capsys.readouterr().out
    assert {n for n in live_names() if not n.endswith(".rss")} == {"runner.json", "runner.log"}
    assert (hist() / core._day(T0 - 3600) / "fin.log").read_text() == "line\n"
    assert core._load("fin")["log_path"] == str(hist() / core._day(T0 - 3600) / "fin.log")
    assert core._load("stale") is None and not list(hist().glob(f"{core._day(T0 - 40 * DAY)}/*"))
    assert core.cmd_archive(ns()) == 0  # rerun: nothing left to move
    assert "archived 0 finished job(s)" in capsys.readouterr().out
    core.cmd_kill(ns(name="runner", force=True))


def test_archive_does_not_touch_a_scheduled_record(clock):
    start("later", "true", at=datetime.fromtimestamp(T0 + 600, tz=UTC).isoformat())
    core.cmd_archive(ns())
    assert (core.BASE / "later.json").is_file() and not hist().exists()


# --- list --exited --since -------------------------------------------------- #


def listed(capsys, *argv) -> list[dict]:
    capsys.readouterr()
    assert cli.main(["list", *argv, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_exited_since_answers_the_window_from_the_live_folder_and_the_days_it_covers(
    clock, capsys, monkeypatch
):
    seed("recent", T0 - 3600)
    seed("older", T0 - 20 * 3600, status="killed")
    seed("ancient", T0 - 9 * DAY)
    seed("flat-recent", T0 - 1800, live=True)  # ended, not yet swept: in the window
    seed("old-live", T0 - 6 * DAY, live=True)  # outside the window: skipped by file age
    # The seeded records have no index yet; building it is the one full scan (Path.glob goes
    # through os.scandir on Python 3.12), so it runs before the spy and only the listing is watched.
    core._live_index()
    scanned: list[str] = []
    real = os.scandir

    def spy(path="."):
        scanned.append(str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", spy)
    rows = listed(capsys, "--exited", "--since", "12")
    assert {r["name"] for r in rows} == {"recent", "flat-recent"}
    day_dirs = {s for s in scanned if s.startswith(str(hist()) + os.sep)}
    assert str(hist() / core._day(T0 - 9 * DAY)) not in day_dirs
    assert {r["name"] for r in listed(capsys, "--exited", "--since", "24")} == {
        "recent",
        "older",
        "flat-recent",
    }
    assert rows[0]["status"] == "exited" and rows[0]["exit_code"] == 0


def test_exited_since_skips_by_file_age_and_by_end_time(clock, capsys):
    seed("in", T0 - 1800)
    meta = seed("stale-end", T0 - 7200)
    # written recently (a rewrite), but it ended before the window: judged by end_ts
    os.utime(hist() / core._day(meta["end_ts"]) / "stale-end.json", (T0 - 60, T0 - 60))
    assert [r["name"] for r in listed(capsys, "--exited", "--since", "1")] == ["in"]


def test_exited_since_includes_a_job_that_has_just_died_unreconciled(capsys):
    start("fresh", "bash", "-c", "exit 3")
    deadline = time.monotonic() + 10
    while core._alive(core._load("fresh")["pid"], core._load("fresh")["pid_starttime"]):
        assert time.monotonic() < deadline
    rows = listed(capsys, "--exited", "--since", "1")
    assert [(r["name"], r["exit_code"]) for r in rows] == [("fresh", 3)]


def test_exited_since_table_and_flag_errors(clock, capsys):
    seed("t", T0 - 600)
    capsys.readouterr()
    assert cli.main(["list", "--exited", "--since", "1"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("NAME") and "t" in out.splitlines()[1]
    for bad in (
        ["--exited"],
        ["--since", "2"],
        ["--exited", "--since", "0"],
        ["--exited", "--since", "1", "--all"],
        ["--exited", "--since", "1", "--running"],
    ):
        assert cli.main(["list", *bad]) == core.EXIT_USAGE
    capsys.readouterr()


def test_the_fast_list_parser_takes_exited_and_since():
    a = cli._list_args(["--exited", "--since", "12", "--json"])
    assert a.exited is True and a.since == 12.0 and a.json is True
    assert cli._list_args(["--since=0.5", "--exited"]).since == 0.5
    assert cli._list_args(["--since", "x"]) is None
    assert cli._list_args([]).exited is False


def test_the_sweep_judges_retention_by_the_clock_it_was_given():
    """housekeeping(now=...) carries its clock into the archive step: a job that ended
    just now is past retention for a sweep run 30 days later, with the engine clock real."""
    seed("old1", T0, live=True)
    core.housekeeping(now=T0 + 30 * DAY)
    assert not (core.BASE / "old1.json").exists()
    assert not list(hist().glob("*/old1.json"))
