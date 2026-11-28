"""The weekly public transfer (scripts/public_transfer.py) and where public-guard runs."""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "public_transfer", ROOT / "scripts" / "public_transfer.py"
)
assert _spec and _spec.loader
pt = importlib.util.module_from_spec(_spec)
sys.modules["public_transfer"] = pt
_spec.loader.exec_module(pt)


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()


def _repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    for name, text in files.items():
        (path / name).write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "c")
    return path


def test_workflow_jobs_run_only_in_the_public_repository():
    text = (ROOT / ".github" / "workflows" / "public-guard.yml").read_text()
    jobs = "\n" + text.split("\njobs:\n", 1)[1]
    for name in ("secrets", "denylist"):
        block = re.split(r"\n  (?=\S)", jobs.split(f"\n  {name}:\n", 1)[1])[0]
        assert "if: github.repository == 'Abrak98/proc-jobs'" in block
    assert pt.PUBLIC_REPO == "Abrak98/proc-jobs"


def test_public_holds_tip_compares_trees_not_histories(tmp_path):
    private = _repo(tmp_path / "private", {"a.txt": "one\n"})
    public = _repo(tmp_path / "public", {"a.txt": "one\n"})
    _git(public, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert pt.public_holds_tip(private, public)
    (private / "a.txt").write_text("two\n")
    _git(private, "commit", "-qam", "change")
    assert not pt.public_holds_tip(private, public)


def test_parse_denylist_output_gives_file_and_line_never_the_word():
    out = (
        "deny list: commit abcdef1234 docs/a.md:7\n"
        "deny list: commit abcdef1234 file name src/x.py\n"
        "deny list: commit abcdef1234 author email\n"
        "unrelated line\n"
    )
    found = pt.parse_denylist_output(out)
    assert [(f.file, f.line) for f in found] == [
        ("docs/a.md", 7),
        ("file name src/x.py", 0),
        ("author email", 0),
    ]
    assert all(f.source == "deny list" for f in found)


def test_parse_model_findings_reads_the_array_and_fails_closed_on_prose():
    reply = 'Here:\n[{"file": "README.md", "line": 3, "what": "a private path"}]'
    (f,) = pt.parse_model_findings(reply)
    assert (f.source, f.file, f.line) == ("model", "README.md", 3)
    assert pt.parse_model_findings("[]") == []
    with pytest.raises(RuntimeError):
        pt.parse_model_findings("looks fine to me")
    with pytest.raises(RuntimeError):
        pt.parse_model_findings("[not json]")


def test_diff_chunks_split_at_file_boundaries_and_keep_every_byte():
    diff = "".join(f"diff --git a/f{i} b/f{i}\n+" + "x" * 50 + "\n" for i in range(10))
    chunks = pt.diff_chunks(diff, limit=200)
    assert len(chunks) > 1
    assert "".join(chunks) == diff
    assert all(c.startswith("diff --git ") for c in chunks)
    big = "diff --git a/big b/big\n" + "".join(f"+line {i}\n" for i in range(100))
    assert "".join(pt.diff_chunks(big, limit=100)) == big


def test_ticket_has_the_question_and_no_table():
    ticket = pt.build_ticket(
        "https://github.com/Abrak98/proc-jobs/pull/9", "abc1234567", "- a change"
    )
    assert ticket["type"] == "research"
    assert "Вопросы" in ticket["sections"]
    assert ticket["links"][0]["url"].endswith("/pull/9")
    assert "|" not in "".join(ticket["sections"].values())


def test_ticket_lines_fit_the_limit_and_keep_list_items():
    long_item = "- " + "слово " * 60
    ticket = pt.build_ticket("https://github.com/Abrak98/proc-jobs/pull/9", "abc1234567", long_item)
    lines = "\n".join(ticket["sections"].values()).splitlines()
    assert max(len(line) for line in lines) <= 160
    assert any(line.startswith("  слово") for line in lines)
