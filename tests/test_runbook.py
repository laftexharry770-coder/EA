"""docs/RUNBOOK.md is for the user, but it states facts about the code:
the exit codes, and the commands to type. These tests fail if the two
drift apart."""
from __future__ import annotations

import re
from pathlib import Path

import xaucopier.cli.main as cli_main
from xaucopier.cli import exit_codes

RUNBOOK = (Path(__file__).resolve().parents[1] / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")


def test_the_exit_code_table_lists_exactly_the_codes_the_cli_returns() -> None:
    documented = {int(code) for code in re.findall(r"^\| (\d+) \|", RUNBOOK, flags=re.MULTILINE)}
    assert documented == {code for code, _ in exit_codes.MEANINGS}


def test_every_command_the_runbook_tells_the_user_to_type_exists() -> None:
    parser = cli_main.build_parser()
    commands = set(parser._subparsers._group_actions[0].choices)  # noqa: SLF001 -- argparse has no public accessor
    typed = set(re.findall(r"\.\\xaucopier ([a-z][a-z-]*)", RUNBOOK))  # a command, not --help
    assert typed, "the runbook names no command"
    assert typed <= commands, typed - commands
    assert {"setup-login", "resolve-channels", "watch", "status", "ack"} <= typed


def test_the_runbook_says_what_the_user_must_know() -> None:
    for phrase in (
        "never stores the login code, your 2FA password",
        "XAUCOPIER-TRADEBOX",
        "Do not terminate it",
        "Set time automatically",
        "session.dpapi",
        "Never use an api_id or api_hash you found in a tutorial",
        # Fix round 1: a .cmd asks this on Ctrl+C; answering Y is the way out.
        "Terminate batch job (Y/N)?",
        # A stuck watch still holds the login: stop it before starting another.
        "press **Ctrl+C** and answer **Y** first",
        # Exit 3's likeliest silent cause.
        "a second copy of the session was used somewhere else",
        # How to clear alerts, and which need action first.
        "ack --all",
        "TELEGRAM_SESSION_DEAD",
        "TELEGRAM_API_ID_REJECTED",
    ):
        assert phrase in RUNBOOK, phrase
