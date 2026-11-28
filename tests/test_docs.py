"""The reference documents keep up with the program: every subcommand, every option and
every exit code the code defines is written down. A change that adds one without updating the
docs fails here, which is the step that keeps README and CLI_REFERENCE from falling behind."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from proc_lib import cli, core

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
REFERENCE = (ROOT / "CLI_REFERENCE.md").read_text()


def _public_commands() -> dict[str, argparse.ArgumentParser]:
    """Every subcommand under its first name (an alias such as `ls` is the same parser)."""
    parser = cli.build_parser()
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    seen: set[int] = set()
    commands = {}
    for name, sp in action.choices.items():
        if id(sp) not in seen:
            seen.add(id(sp))
            commands[name] = sp
    return commands


COMMANDS = _public_commands()


def _options(sp: argparse.ArgumentParser) -> list[str]:
    return [
        opt
        for a in sp._actions
        for opt in a.option_strings
        if opt.startswith("--") and opt != "--help"
    ]


def _section(command: str) -> str:
    """The CLI_REFERENCE section whose heading starts with `proc <command>`."""
    for part in re.split(r"(?m)^## ", REFERENCE):
        if re.match(rf"`proc {re.escape(command)}\b", part):
            return part
    return ""


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_reference_has_a_section_per_command(command):
    assert _section(command), f"CLI_REFERENCE.md has no section for `proc {command}`"


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_readme_lists_every_command(command):
    assert f"proc {command}" in README, f"README.md does not mention `proc {command}`"


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_reference_names_every_option_of_its_command(command):
    section = _section(command)
    missing = [o for o in _options(COMMANDS[command]) if o not in section]
    assert not missing, f"CLI_REFERENCE.md section of `proc {command}` lacks {missing}"


def test_reference_lists_every_exit_code():
    table = REFERENCE.split("## Exit codes", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"^\| `(\d+)` \|", table, re.M))
    defined = {str(v) for k, v in vars(core).items() if k.startswith("EXIT_")}
    assert defined - documented == set(), "exit codes missing from CLI_REFERENCE.md"


def test_readme_storage_layout_names_what_the_registry_holds():
    layout = README.split("## Storage layout", 1)[1].split("\n## ", 1)[0]
    for entry in (core.HISTORY_DIR, "sessions", "cost", ".live-index", ".rss", ".schedenv"):
        assert entry in layout, f"README storage layout lacks {entry}"
