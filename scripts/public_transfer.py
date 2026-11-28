#!/usr/bin/env python3
"""The weekly transfer of the maintained tree to the public repository, prepared whole.

    public_transfer.py run [--dry-run] [--allow-branch NAME]
    public_transfer.py merge <pull request number>

`run` is the one command the weekly schedule starts. In order:

1. Skip when the public main branch already holds the tree of the private tip, or when a
   transfer pull request is already open (one at a time).
2. Stage the transfer in a clone of the public repository that this script keeps itself
   (`scripts/export_public.sh --stage`): one commit holding the whole tree, on a new branch.
   Nothing is pushed yet.
3. Audit the staged diff against the public tip: gitleaks, the deny-list scan
   (`scripts/denylist_check.py`) and a model reading for personal traces the list does not know
   (names, employer terms, private paths, ticket numbers). Every finding has a file and a line.
4. A finding stops the run before any push: the findings are printed and sent to the project's
   inbox, and the exit status is 1.
5. A clean audit pushes the branch and opens the pull request (`scripts/export_public.sh
   --publish`), then files one ticket with the question whether to merge it.

`run` never merges. `merge` is the separate command that the answer "yes" in that ticket starts:
it refuses a pull request this script did not open and audit, one whose head changed since the
audit, and one whose required checks have not all passed, then squash-merges it.

Environment: PUBLIC_TRANSFER_HOME (state and clone, default ~/.local/state/proc-public-transfer),
PUBLIC_TRANSFER_DENYLIST (the deny-list file; required, the audit refuses to run without it),
PUBLIC_TRANSFER_MODEL (model alias for the reading, default opus),
PUBLIC_TRANSFER_TICKET_CMD (command that takes the ticket JSON file path as its last argument;
unset: the ticket is printed), PUBLIC_TRANSFER_INBOX_CMD (command that takes the stop report text
as its last argument; unset: the report is only printed).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PUBLIC_REPO = "Abrak98/proc-jobs"
PUBLIC_URL = f"git@github.com:{PUBLIC_REPO}.git"
PROJECT = "proc-lib"
REPO = Path(__file__).resolve().parent.parent
# One model call reads at most this many characters of diff; a bigger diff goes in several calls.
CHUNK_CHARS = 120_000
MODEL_TIMEOUT_SECONDS = 900


@dataclass(frozen=True)
class Finding:
    source: str  # "gitleaks", "deny list" or "model"
    file: str
    line: int
    what: str

    def render(self) -> str:
        return f"[{self.source}] {self.file}:{self.line}: {self.what}"


def state_dir() -> Path:
    default = Path.home() / ".local" / "state" / "proc-public-transfer"
    return Path(os.environ.get("PUBLIC_TRANSFER_HOME", default))


def denylist_file() -> Path:
    value = os.environ.get("PUBLIC_TRANSFER_DENYLIST")
    if not value:
        raise RuntimeError("PUBLIC_TRANSFER_DENYLIST is not set; the audit cannot run without it")
    return Path(value)


def env_command(name: str) -> list[str]:
    return shlex.split(os.environ.get(name, ""))


def run(
    cmd: list[str],
    cwd: Path | str | None = None,
    check: bool = True,
    input_text: str | None = None,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if env is None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    done = subprocess.run(
        cmd,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        check=False,
    )
    if check and done.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])} failed ({done.returncode}): {done.stderr.strip()}")
    return done


def git(repo: Path, *args: str, check: bool = True) -> str:
    return run(["git", "-C", str(repo), *args], check=check).stdout.strip()


# --- the clone and the skip decision ---------------------------------------------------------


def ensure_clone(home: Path) -> Path:
    """A clean clone of the public repository on its main branch, kept by this script alone."""
    clone = home / "proc-jobs"
    if not (clone / ".git").is_dir():
        home.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--quiet", PUBLIC_URL, str(clone)])
    git(clone, "fetch", "--quiet", "origin", "main")
    git(clone, "switch", "--quiet", "--force", "-C", "main", "origin/main")
    git(clone, "clean", "-fdq")
    for branch in git(clone, "branch", "--format=%(refname:short)").splitlines():
        if branch.startswith("sync-"):
            git(clone, "branch", "--quiet", "-D", branch)
    return clone


def tree_of(repo: Path, rev: str) -> str:
    return git(repo, "rev-parse", f"{rev}^{{tree}}")


def public_holds_tip(source: Path, clone: Path) -> bool:
    """True when the public main branch has exactly the tree of the private tip."""
    return tree_of(source, "HEAD") == tree_of(clone, "origin/main")


def open_transfer_prs() -> list[dict]:
    out = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            PUBLIC_REPO,
            "--state",
            "open",
            "--json",
            "number,headRefName,headRefOid,url",
        ]
    ).stdout
    return [p for p in json.loads(out or "[]") if str(p["headRefName"]).startswith("sync-")]


# --- the audit ---------------------------------------------------------------------------------


def audit_gitleaks(clone: Path, branch: str) -> list[Finding]:
    if shutil.which("gitleaks") is None:
        raise RuntimeError("gitleaks is not installed; refusing, an audit without it is no audit")
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "gitleaks.json"
        done = run(
            [
                "gitleaks",
                "detect",
                "--source",
                str(clone),
                "--log-opts",
                f"origin/main..{branch}",
                "--redact",
                "--no-banner",
                "--report-format",
                "json",
                "--report-path",
                str(report),
                "--exit-code",
                "1",
            ],
            check=False,
        )
        if done.returncode not in (0, 1):
            raise RuntimeError(f"gitleaks failed ({done.returncode}): {done.stderr.strip()}")
        rows = json.loads(report.read_text() or "[]") if report.exists() else []
    return [
        Finding("gitleaks", r.get("File", "?"), int(r.get("StartLine") or 0), str(r.get("RuleID")))
        for r in rows
    ]


_DENY_LINE = re.compile(r"^deny list: commit \S+ (?P<where>.+)$")
_FILE_LINE = re.compile(r"^(?P<file>.+):(?P<line>\d+)$")


def parse_denylist_output(text: str) -> list[Finding]:
    """The masked lines of denylist_check.py as findings (the matched word is never printed)."""
    found = []
    for raw in text.splitlines():
        m = _DENY_LINE.match(raw.strip())
        if not m:
            continue
        where = m.group("where")
        loc = _FILE_LINE.match(where)
        if loc:
            found.append(Finding("deny list", loc["file"], int(loc["line"]), "a deny-listed word"))
        else:
            found.append(Finding("deny list", where, 0, "a deny-listed word"))
    return found


def audit_denylist(clone: Path, branch: str) -> list[Finding]:
    done = run(
        [
            sys.executable,
            "-I",
            str(REPO / "scripts" / "denylist_check.py"),
            "--denylist-file",
            str(denylist_file()),
            "--mask",
            "--repo",
            str(clone),
            "--",
            f"origin/main..{branch}",
        ],
        check=False,
    )
    if done.returncode == 2:
        raise RuntimeError(f"the deny-list scan could not run: {done.stderr.strip()}")
    return parse_denylist_output(done.stdout)


MODEL_PROMPT = """You audit a change that is about to become public in an open source repository \
(a process supervisor for autonomous agents, written by one maintainer). Below is a unified \
diff against the current public tip. Look ONLY at lines the diff adds (lines starting with +) \
and at file names. Report every personal trace: a person's real name or a colleague's name, an \
employer or company term, a private filesystem path that contains a user name, a private \
ticket, issue or inbox number, an internal hostname, an email address other than a GitHub \
no-reply one. Ordinary project text, names of tools and environment variables, and time \
zones are not findings. Reply with ONLY a JSON array, no prose: \
each element {"file": "<path>", "line": <line number in the new file, 0 when unknown>, \
"what": "<one short sentence naming the trace>"}. Reply [] when there is none.

DIFF:
"""


def diff_chunks(diff: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Split a unified diff at file boundaries into pieces of at most `limit` characters."""
    pieces = re.split(r"(?m)^(?=diff --git )", diff)
    chunks: list[str] = []
    cur = ""
    for piece in pieces:
        if not piece:
            continue
        if len(piece) > limit:  # one huge file: cut it at line boundaries
            if cur:
                chunks.append(cur)
                cur = ""
            lines = piece.splitlines(keepends=True)
            part = ""
            for line in lines:
                if len(part) + len(line) > limit and part:
                    chunks.append(part)
                    part = ""
                part += line
            if part:
                chunks.append(part)
            continue
        if len(cur) + len(piece) > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += piece
    if cur:
        chunks.append(cur)
    return chunks


def parse_model_findings(text: str) -> list[Finding]:
    """The JSON array in a model reply as findings; a reply with no array is an error."""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        raise RuntimeError(f"the model reply holds no JSON array: {text[:200]!r}")
    try:
        rows = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"the model reply is not valid JSON: {e}") from e
    found = []
    for r in rows:
        if not isinstance(r, dict):
            raise RuntimeError(f"unexpected element in the model reply: {r!r}")
        found.append(
            Finding(
                "model", str(r.get("file", "?")), int(r.get("line") or 0), str(r.get("what", ""))
            )
        )
    return found


def ask_model(prompt: str) -> str:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    with tempfile.TemporaryDirectory() as tmp:  # a neutral folder: no project rules are loaded
        done = run(
            [
                "claude",
                "-p",
                "--model",
                os.environ.get("PUBLIC_TRANSFER_MODEL", "opus"),
                "--tools",
                "",
                "--no-session-persistence",
                "--output-format",
                "text",
            ],
            cwd=tmp,
            input_text=prompt,
            timeout=MODEL_TIMEOUT_SECONDS,
            env=env,
        )
    return done.stdout


def audit_model(clone: Path, branch: str) -> list[Finding]:
    diff = git(clone, "diff", "--no-color", "--no-ext-diff", f"origin/main..{branch}")
    found: list[Finding] = []
    for chunk in diff_chunks(diff):
        found += parse_model_findings(ask_model(MODEL_PROMPT + chunk))
    return found


def summarize_changes(clone: Path, branch: str) -> str:
    """Plain-Russian bullets for the weekly ticket: what the transfer changes in the public tree."""
    stat = git(clone, "diff", "--stat=120", f"origin/main..{branch}")
    since = git(clone, "log", "-1", "--format=%cI", "origin/main")
    log = git(REPO, "log", f"--since={since}", "--format=- %s")
    prompt = (
        "Опиши по-русски простыми словами, что изменится в публичном репозитории программы "
        "proc (супервизор фоновых процессов), если слить этот перенос. Только маркированный "
        "список, не больше восьми пунктов, каждый пункт - одно изменение для пользователя "
        "программы, без номеров коммитов и без слов на английском, кроме имён команд. Без "
        "вступления и заключения.\n\nСтатистика переноса:\n"
        f"{stat}\n\nПоследние коммиты закрытого репозитория:\n{log}\n"
    )
    return ask_model(prompt).strip()


def audit(clone: Path, branch: str) -> list[Finding]:
    return (
        audit_gitleaks(clone, branch) + audit_denylist(clone, branch) + audit_model(clone, branch)
    )


# --- reporting and the ticket ------------------------------------------------------------------


def report_stop(findings: list[Finding], tip: str) -> None:
    lines = [f.render() for f in findings]
    print(f"public_transfer: audit of {tip} found {len(lines)} problem(s); nothing pushed")
    for line in lines:
        print(line)
    text = (
        f"The weekly transfer of the proc tree {tip} to the public repository stopped before any "
        f"push: the audit found {len(lines)} personal or secret trace(s). Fix each in the private "
        "repository, then the next weekly run retries. Findings: " + " | ".join(lines[:20])
    )
    inbox = env_command("PUBLIC_TRANSFER_INBOX_CMD")
    if inbox:
        run([*inbox, text], check=False)


def wrap_lines(text: str, width: int = 150) -> str:
    """Rewrap each line to the ticket's line limit; list items keep a hanging indent."""
    out = []
    for line in text.splitlines():
        indent = "  " if re.match(r"\s*(-|\d+\.)\s", line) else ""
        out.append(textwrap.fill(line, width, subsequent_indent=indent) if line.strip() else "")
    return "\n".join(out)


def build_ticket(pr_url: str, short: str, changes: str) -> dict:
    """The sections of the weekly ticket (type research: a question the operator answers)."""
    ticket: dict[str, Any] = {
        "type": "research",
        "project": PROJECT,
        "title": f"Слить перенос proc в публичный репозиторий ({short})",
        "sections": {
            "Ответ": (
                f"Еженедельный перенос закрытого репозитория proc в публичный `{PUBLIC_REPO}` "
                f"подготовлен и проверен: открыт pull request {pr_url}. Он ничего не меняет, "
                "пока его не слили."
            ),
            "Текущее состояние": (
                "Проверка перед открытием pull request: поиск секретов (`gitleaks`), сверка со "
                "списком запрещённых слов и чтение разницы моделью на личные следы. Замечаний "
                "нет.\n\nЧто меняется в публичном дереве:\n\n" + changes
            ),
            "Что значит каждое число и термин": (
                "- pull request - предложение изменений на GitHub: публичная ветка `main` "
                "получает один коммит с целым деревом, только после слияния.\n"
                "- слить - принять это предложение в `main` публичного репозитория (squash: "
                "один коммит)."
            ),
            "Вопросы": (
                f"1. Слить pull request {pr_url} в публичный репозиторий? Ответ «да» "
                "запускает отдельную команду слияния, которая сверяет, что проверенный коммит "
                "не изменился. Рекомендация: слить, если список изменений выше ожидаем."
            ),
        },
        "affects": ["file:scripts/public_transfer.py"],
        "links": [{"type": "url", "url": pr_url, "label": "Открыть pull request"}],
    }
    ticket["sections"] = {k: wrap_lines(v) for k, v in ticket["sections"].items()}
    return ticket


def file_ticket(ticket: dict) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ticket.json"
        path.write_text(json.dumps(ticket, ensure_ascii=False, indent=2))
        command = env_command("PUBLIC_TRANSFER_TICKET_CMD")
        if not command:
            return 0, json.dumps(ticket, ensure_ascii=False, indent=2)
        done = run([*command, str(path)], check=False)
    return done.returncode, (done.stdout + done.stderr).strip()


# --- commands ------------------------------------------------------------------------------------


def current_branch() -> str:
    return git(REPO, "rev-parse", "--abbrev-ref", "HEAD")


def cmd_run(args: argparse.Namespace) -> int:
    branch_now = current_branch()
    if branch_now != "main" and args.allow_branch != branch_now:
        print(
            f"public_transfer: the checkout is on {branch_now}, not main; give "
            f"--allow-branch {branch_now} to transfer that tip knowingly",
            file=sys.stderr,
        )
        return 2
    if git(REPO, "status", "--porcelain", "--untracked-files=no"):
        print("public_transfer: the private checkout has uncommitted changes", file=sys.stderr)
        return 2
    tip = git(REPO, "rev-parse", "--short=10", "HEAD")
    clone = ensure_clone(state_dir())
    if public_holds_tip(REPO, clone):
        print(f"public_transfer: the public main branch already holds {tip}; nothing to do")
        return 0
    open_prs = open_transfer_prs()
    if open_prs:
        urls = ", ".join(p["url"] for p in open_prs)
        print(f"public_transfer: a transfer pull request is already open ({urls}); nothing to do")
        return 0

    export = str(REPO / "scripts" / "export_public.sh")
    branch = run([export, "--stage", str(clone)], cwd=REPO).stdout.strip().splitlines()[-1]
    findings = audit(clone, branch)
    if findings:
        report_stop(findings, tip)
        return 1
    head_sha = git(clone, "rev-parse", branch)
    print(f"public_transfer: audit of {tip} clean (staged {branch} at {head_sha[:10]})")
    if args.dry_run:
        print("public_transfer: --dry-run, nothing pushed")
        return 0
    changes = summarize_changes(clone, branch)
    out = run([export, "--publish", str(clone)], cwd=REPO).stdout.strip()
    pr_url = out.splitlines()[-1]
    number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
    state_dir().joinpath(f"pending-{number}.json").write_text(
        json.dumps({"pr": number, "url": pr_url, "head": head_sha, "tip": tip})
    )
    print(f"public_transfer: opened {pr_url}")
    code, shown = file_ticket(build_ticket(pr_url, tip, changes))
    print(shown)
    return 0 if code == 0 else 1


def cmd_merge(args: argparse.Namespace) -> int:
    pending = state_dir() / f"pending-{args.number}.json"
    if not pending.is_file():
        print(
            f"public_transfer: pull request {args.number} was not opened and audited here",
            file=sys.stderr,
        )
        return 1
    info = json.loads(pending.read_text())
    view = json.loads(
        run(
            [
                "gh",
                "pr",
                "view",
                str(args.number),
                "--repo",
                PUBLIC_REPO,
                "--json",
                "state,headRefOid,baseRefName,statusCheckRollup",
            ]
        ).stdout
    )
    if view["state"] != "OPEN" or view["baseRefName"] != "main":
        print(
            f"public_transfer: pull request {args.number} is not open against main", file=sys.stderr
        )
        return 1
    if view["headRefOid"] != info["head"]:
        print(
            "public_transfer: the head of the pull request changed since the audit "
            f"({info['head'][:10]} -> {view['headRefOid'][:10]}); run the audit again",
            file=sys.stderr,
        )
        return 1
    bad = [c for c in view["statusCheckRollup"] if c.get("conclusion") != "SUCCESS"]
    if bad or not view["statusCheckRollup"]:
        names = ", ".join(str(c.get("name")) for c in bad) or "no checks yet"
        print(f"public_transfer: checks not green ({names}); not merging", file=sys.stderr)
        return 1
    run(
        [
            "gh",
            "pr",
            "merge",
            str(args.number),
            "--repo",
            PUBLIC_REPO,
            "--squash",
            "--delete-branch",
        ]
    )
    pending.unlink()
    print(f"public_transfer: merged pull request {args.number}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="prepare the week's transfer up to the pull request")
    p_run.add_argument("--dry-run", action="store_true", help="stage and audit, push nothing")
    p_run.add_argument("--allow-branch", help="transfer the tip of this non-main branch knowingly")
    p_run.set_defaults(func=cmd_run)
    p_merge = sub.add_parser("merge", help="squash-merge an audited transfer pull request")
    p_merge.add_argument("number", type=int)
    p_merge.set_defaults(func=cmd_merge)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        print(f"public_transfer: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
