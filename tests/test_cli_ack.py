"""Tests for `xaucopier ack`: list the uncleared alerts, clear one, clear all.

Without it an alert could never be cleared, so after the first deleted
signal `status` could never say `ALERTS: none` again -- which is what the
runbook tells the user to expect.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import xaucopier.cli.main as cli_main
import xaucopier.cli.status as status_module
from xaucopier.cli import exit_codes
from xaucopier.store import alerts, db

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    db_path = tmp_path / "state.db"
    conn = db.connect(db_path, role="writer")
    try:
        for minutes, level, code in [
            (40, "WARNING", "SIGNAL_VOIDED_BY_DELETION"),  # 1
            (30, "CRITICAL", "LIVE_SIGNAL_DELETED"),  # 2
            (20, "CRITICAL", alerts.TELEGRAM_SESSION_DEAD),  # 3
            (10, "WARNING", "ALREADY_DEALT_WITH"),  # 4, acked below
        ]:
            alerts.record(
                conn, now_utc=NOW - timedelta(minutes=minutes), level=level, code=code,
                detail=f"what {code} means",
            )
        alerts.ack(conn, alert_id=4)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _uncleared(db_path: Path) -> list[int]:
    conn = db.connect_readonly(db_path)
    try:
        return [alert.alert_id for alert in alerts.unacked(conn)]
    finally:
        conn.close()


def _ack(db_path: Path, *args: str) -> int:
    return cli_main.main(["ack", "--db", str(db_path), *args])


def test_with_no_arguments_it_lists_uncleared_alerts_critical_first_and_changes_nothing(
    store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = hashlib.sha256(store.read_bytes()).hexdigest()

    assert _ack(store) == exit_codes.EXIT_OK

    out = capsys.readouterr().out
    listed = [line.split()[1] for line in out.splitlines() if line.startswith("  [")]
    assert listed == ["#2", "#3", "#1"]  # CRITICAL (oldest first), then WARNING
    assert "ALREADY_DEALT_WITH" not in out
    assert "ack NUMBER" in out and "ack --all" in out
    assert hashlib.sha256(store.read_bytes()).hexdigest() == before
    assert _uncleared(store) == [1, 2, 3]


def test_ack_a_number_clears_that_alert_only(
    store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _ack(store, "2") == exit_codes.EXIT_OK

    assert _uncleared(store) == [1, 3]
    assert "Cleared alert #2." in capsys.readouterr().out


@pytest.mark.parametrize(
    "number", ["4", "99", str(2**63), str(10**30)]
)  # already cleared; never existed; too big for SQLite
def test_ack_of_a_number_that_is_not_uncleared_changes_nothing(
    store: Path, capsys: pytest.CaptureFixture[str], number: str
) -> None:
    assert _ack(store, number) == exit_codes.EXIT_FAILED

    assert _uncleared(store) == [1, 2, 3]
    assert f"There is no uncleared alert #{number}" in capsys.readouterr().err


def test_ack_all_clears_everything_and_status_then_says_none(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _ack(store, "--all") == exit_codes.EXIT_OK

    out = capsys.readouterr().out
    assert "Cleared 3 alert(s)." in out
    # Clearing a login alert by hand does not fix the login: it says so.
    assert "does not fix the login" in out
    assert _uncleared(store) == []

    monkeypatch.setattr(status_module, "_utcnow", lambda: NOW)
    assert cli_main.main(["status", "--db", str(store), "--config-dir", str(store.parent)]) == 0
    assert "ALERTS: none" in capsys.readouterr().out


def test_status_tells_the_user_how_to_clear_alerts(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(status_module, "_utcnow", lambda: NOW)
    assert cli_main.main(["status", "--db", str(store), "--config-dir", str(store.parent)]) == 0
    assert "ack --all" in capsys.readouterr().out


def test_without_a_database_it_says_so_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "none.db"
    for args in ((), ("--all",), ("1",)):
        assert _ack(missing, *args) == exit_codes.EXIT_FAILED
        assert not missing.exists()
    assert "No recordings yet" in capsys.readouterr().err


def test_a_number_and_all_together_is_a_usage_error(store: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        _ack(store, "1", "--all")
    assert raised.value.code == exit_codes.EXIT_USAGE
    assert _uncleared(store) == [1, 2, 3]


def test_alerts_ack_all_by_code_touches_only_those_codes(store: Path) -> None:
    conn = db.connect(store, role="writer")
    try:
        assert alerts.ack_all(conn, codes=(alerts.TELEGRAM_SESSION_DEAD,)) == 1
        assert alerts.ack_all(conn, codes=()) == 0
        conn.commit()
    finally:
        conn.close()
    assert _uncleared(store) == [1, 2]


def test_a_database_locked_past_the_timeout_is_explained_and_changes_nothing(
    store: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Another writer holds the lock longer than SQLite's busy timeout
    (10 s; shortened here). One sentence, exit 1 -- not a traceback."""
    monkeypatch.setattr(db, "_BUSY_TIMEOUT_MS", 50)
    holder = sqlite3.connect(store)
    holder.execute("BEGIN IMMEDIATE")
    try:
        for args in (("--all",), ("2",)):
            assert _ack(store, *args) == exit_codes.EXIT_FAILED
            assert "The database is busy" in capsys.readouterr().err
    finally:
        holder.rollback()
        holder.close()
    assert _uncleared(store) == [1, 2, 3]
