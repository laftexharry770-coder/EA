"""A file at the database path that xaucopier cannot use -- one that is not
a database at all (final review F25), or one written by a newer version
of xaucopier (group-1 review leftover G1L-5) -- is explained in one plain
sentence by every command that reads the store, exit 1. Never a
traceback, never the advice to run `watch` "to bring it up to date",
which is wrong for a newer file, and the file is never changed.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

import xaucopier.cli.main as cli_main
from xaucopier.cli import exit_codes
from xaucopier.store import db
from xaucopier.store.migrations import CURRENT_VERSION

REPO = Path(__file__).resolve().parents[1]

ENABLED_CHANNEL = """
[[channels]]
channel_id = -1001111111111
name       = "Test Channel"
parser     = "mmh"
ordinal    = 1
enabled    = true
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "config"
    folder.mkdir()
    (folder / "channels.toml").write_text(ENABLED_CHANNEL, encoding="utf-8")
    (folder / "accounts.toml").write_bytes((REPO / "config" / "accounts.toml").read_bytes())
    return folder


def _not_a_database(path: Path) -> Path:
    path.write_bytes(b"this is not an sqlite database, it is a text file\n" * 60)
    return path


def _newer(path: Path) -> Path:
    db.connect(path, role="writer").close()
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {CURRENT_VERSION + 1}")
    raw.commit()
    raw.close()
    return path


def _commands(db_path: Path, config_dir: Path, out_dir: Path) -> dict[str, list[str]]:
    common = ["--db", str(db_path)]
    return {
        "status": ["status", *common, "--config-dir", str(config_dir)],
        "ack": ["ack", *common],
        "ack N": ["ack", *common, "1"],
        "ack --all": ["ack", *common, "--all"],
        "export-fixtures": ["export-fixtures", *common, "--out", str(out_dir)],
    }


COMMANDS = ("status", "ack", "ack N", "ack --all", "export-fixtures")


@pytest.mark.parametrize("command", COMMANDS)
def test_a_file_that_is_not_a_database_is_explained_in_one_sentence(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    db_path = _not_a_database(tmp_path / "state.db")
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    out_dir = tmp_path / "fixtures"
    out_dir.mkdir()

    code = cli_main.main(_commands(db_path, config_dir, out_dir)[command])

    assert code == exit_codes.EXIT_FAILED
    captured = capsys.readouterr()
    assert f"The file at {db_path} is not xaucopier's database" in captured.err
    assert "Traceback" not in captured.err + captured.out
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    assert list(out_dir.iterdir()) == []


@pytest.mark.parametrize("command", COMMANDS)
def test_a_database_from_a_newer_version_says_to_use_that_version(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    db_path = _newer(tmp_path / "state.db")
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    out_dir = tmp_path / "fixtures"
    out_dir.mkdir()

    code = cli_main.main(_commands(db_path, config_dir, out_dir)[command])

    assert code == exit_codes.EXIT_FAILED
    captured = capsys.readouterr()
    assert "written by a newer version of xaucopier; use that version" in captured.err
    assert "bring it up to date" not in captured.err + captured.out
    assert "Traceback" not in captured.err + captured.out
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    assert list(out_dir.iterdir()) == []


def test_the_channel_list_from_a_newer_database_says_to_use_that_version(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = _newer(tmp_path / "state.db")

    code = cli_main.main(["channels", "--config-dir", str(config_dir), "--db", str(db_path)])

    assert code == exit_codes.EXIT_OK  # the list itself comes from channels.toml
    out = capsys.readouterr().out
    assert "1. Test Channel -- ON" in out
    assert "written by a newer version of xaucopier; use that version" in out
    assert "bring it up to date" not in out


def test_the_channel_list_from_a_file_that_is_not_a_database_says_so(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = _not_a_database(tmp_path / "state.db")

    code = cli_main.main(["channels", "--config-dir", str(config_dir), "--db", str(db_path)])

    assert code == exit_codes.EXIT_OK
    out = capsys.readouterr().out
    assert "1. Test Channel -- ON" in out
    assert f"The file at {db_path} is not xaucopier's database" in out


def test_an_older_database_still_says_to_run_watch_once(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "state.db"
    raw = sqlite3.connect(db_path)
    raw.execute("PRAGMA user_version = 2")
    raw.commit()
    raw.close()

    for argv in (
        ["status", "--db", str(db_path), "--config-dir", str(config_dir)],
        ["ack", "--db", str(db_path)],
    ):
        assert cli_main.main(argv) == exit_codes.EXIT_FAILED
        err = capsys.readouterr().err
        assert "older version of xaucopier" in err
        assert "Run `.\\xaucopier watch` once" in err


@pytest.mark.parametrize(("command", "cleared_said"), [("ack", False), ("ack N", True), ("ack --all", True)])
def test_ack_says_nothing_was_cleared_only_when_asked_to_clear(
    tmp_path: Path, config_dir: Path, capsys: pytest.CaptureFixture[str], command: str,
    cleared_said: bool,
) -> None:
    db_path = _newer(tmp_path / "state.db")

    assert cli_main.main(_commands(db_path, config_dir, tmp_path)[command]) == 1
    assert ("Nothing was cleared." in capsys.readouterr().err) is cleared_said


def test_each_kind_of_database_problem_has_its_own_plain_sentence(tmp_path: Path) -> None:
    from xaucopier.cli.status import database_problem
    from xaucopier.store.migrations import SchemaVersionError

    path = tmp_path / "state.db"
    newer = SchemaVersionError("x", found=CURRENT_VERSION + 1, expected=CURRENT_VERSION)
    older = SchemaVersionError("x", found=2, expected=CURRENT_VERSION)
    assert "newer version of xaucopier; use that version" in database_problem(path, newer)
    assert "older version" in database_problem(path, older)
    assert "Run `.\\xaucopier watch` once" in database_problem(path, older)
    busy = database_problem(path, sqlite3.OperationalError("database is locked"))
    assert busy.startswith("The database is busy")
    io_error = database_problem(path, sqlite3.OperationalError("disk I/O error"))
    assert io_error == f"The database at {path} could not be used (disk I/O error)."
    not_db = database_problem(path, sqlite3.DatabaseError("file is not a database"))
    assert not_db.startswith(f"The file at {path} is not xaucopier's database")
