"""Final review F26: the runbook's exit-code table only helps if the user
can see the code. Before, only a `watch` that had started (and its setup
refusal) printed one: `channels add` (6), `resolve-channels` (6), watch's
own "already running" refusal (6) and every other command's failure
printed none -- and PowerShell does not show one either.

Now every command that does not finish normally ends with "(exit code
N)" (main() adds it), and `watch` says its code on every way it stops,
exactly once. A command that finished normally prints no code.

Nothing here contacts Telegram: every case stops before a session is
loaded, or uses the fake watch rig.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

import xaucopier.cli.main as cli_main
import xaucopier.cli.watch as watch_module
from xaucopier.cli import exit_codes
from xaucopier.ingest.client import AlreadyRunningError

from watch_rig import Rig, rig  # noqa: F401 -- `rig` is a fixture

REPO = Path(__file__).resolve().parents[1]

Argv = Callable[[Path, Path], list[str]]


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A copy of the shipped config (read, never written)."""
    folder = tmp_path / "config"
    folder.mkdir()
    for name in ("channels.toml", "accounts.toml"):
        (folder / name).write_bytes((REPO / "config" / name).read_bytes())
    return folder


CASES: list[tuple[str, Argv, int]] = [
    ("status, no database", lambda t, c: ["status", "--db", str(t / "no.db"), "--config-dir", str(c)], 1),
    ("ack, no database", lambda t, c: ["ack", "--db", str(t / "no.db")], 1),
    ("ack N, no database", lambda t, c: ["ack", "--db", str(t / "no.db"), "3"], 1),
    (
        "export-fixtures, no folder",
        lambda t, c: ["export-fixtures", "--db", str(t / "no.db"), "--out", str(t / "nowhere")],
        1,
    ),
    (
        "channels on, no such number",
        lambda t, c: ["channels", "on", "99", "--config-dir", str(c), "--db", str(t / "no.db")],
        1,
    ),
    ("channels on, no number", lambda t, c: ["channels", "on", "--config-dir", str(c)], 2),
    (
        "channels, no config",
        lambda t, c: ["channels", "--config-dir", str(t / "nowhere"), "--db", str(t / "no.db")],
        5,
    ),
    (
        "channels add, no session",
        lambda t, c: [
            "channels", "add", "--config-dir", str(c), "--secrets", str(t / "none.dpapi"),
            "--db", str(t / "no.db"),
        ],
        5,
    ),
    ("resolve-channels, no config", lambda t, c: ["resolve-channels", "--config-dir", str(t / "nowhere")], 5),
]


@pytest.mark.parametrize(("argv", "expected"), [(argv, code) for _, argv, code in CASES], ids=[c[0] for c in CASES])
def test_a_command_that_fails_ends_with_its_exit_code(
    tmp_path: Path,
    config_dir: Path,
    capsys: pytest.CaptureFixture[str],
    argv: Argv,
    expected: int,
) -> None:
    assert cli_main.main(argv(tmp_path, config_dir)) == expected

    captured = capsys.readouterr()
    assert captured.err.rstrip().splitlines()[-1] == f"(exit code {expected})"
    assert (captured.out + captured.err).count("exit code") == 1


def test_setup_login_that_fails_ends_with_its_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def closed(*_args: object, **_kwargs: object) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)

    assert cli_main.main(["setup-login", "--path", str(tmp_path / "s.dpapi")]) == 1
    assert capsys.readouterr().err.rstrip().splitlines()[-1] == "(exit code 1)"


def test_a_command_that_finished_normally_prints_no_code(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli_main.main(["channels", "--config-dir", str(config_dir), "--db", str(tmp_path / "no.db")])

    assert code == exit_codes.EXIT_OK
    captured = capsys.readouterr()
    assert "exit code" not in captured.out + captured.err


# -- watch: its code, once, on every way it stops ----------------------------------------------


def _said_once(capsys: pytest.CaptureFixture[str], code: int) -> str:
    captured = capsys.readouterr()
    said = captured.out + captured.err
    assert said.count("exit code") == 1, said
    assert f"exit code {code}" in said
    return said


def test_watch_refused_because_one_is_running_says_exit_code_6(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def held() -> None:
        raise AlreadyRunningError("held elsewhere")

    monkeypatch.setattr(watch_module, "acquire_single_instance_lock", held)

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_ALREADY_RUNNING
    assert "(exit code 6)" in _said_once(capsys, 6)


def test_watch_with_the_database_busy_at_start_says_exit_code_4(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def busy(db_path: Path) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(watch_module, "stamp_start", busy)

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_LISTENER_STOPPED
    assert "(exit code 4)" in _said_once(capsys, 4)


def test_watch_with_a_setup_problem_says_exit_code_5_once(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    (rig.config_dir / "channels.toml").unlink()

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_CONFIG_ERROR
    _said_once(capsys, 5)


def test_watch_that_ran_and_stopped_says_its_code_once(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(watch_module, "_run", lambda setup, blob: exit_codes.EXIT_LISTENER_STOPPED)

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_LISTENER_STOPPED
    assert "watch stopped (exit code 4" in _said_once(capsys, 4)
