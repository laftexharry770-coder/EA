"""Tests for `store.alerts`: the durable operator-alert rows.

Plan 3 builds the real alerting (a notifier, acknowledgement from the
CLI). Until then every alert is a row, so nothing an operator must see
is lost to a scrolled-away log line. These tests pin the row, the log
line that accompanies it, and the transaction contract (the caller's
transaction decides whether the alert lands, as with `metrics.bump`).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from xaucopier.store import alerts, db

NOW = datetime(2026, 9, 5, 11, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "store.db", role="writer")
    try:
        yield connection
    finally:
        connection.close()


def test_record_writes_one_row_with_every_field(conn) -> None:
    alert_id = alerts.record(
        conn,
        now_utc=NOW,
        level="CRITICAL",
        code="SIGNAL_INVALIDATED_BY_EDIT",
        channel_id=-1001111111111,
        message_id=4001,
        signal_id=7,
        detail="side buy -> sell",
    )
    conn.commit()

    (alert,) = alerts.unacked(conn)
    assert alert == alerts.Alert(
        alert_id=alert_id,
        created_at_utc=NOW,
        level="CRITICAL",
        code="SIGNAL_INVALIDATED_BY_EDIT",
        channel_id=-1001111111111,
        message_id=4001,
        signal_id=7,
        detail="side buy -> sell",
        acked=False,
    )


def test_record_logs_at_the_alert_level(conn, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="xaucopier.store.alerts"):
        alerts.record(conn, now_utc=NOW, level="WARNING", code="A", detail="first")
        alerts.record(conn, now_utc=NOW, level="CRITICAL", code="B", detail="second")

    levels = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert [lvl for lvl, _ in levels] == [logging.WARNING, logging.CRITICAL]
    assert "A" in levels[0][1] and "first" in levels[0][1]
    assert "B" in levels[1][1] and "second" in levels[1][1]


def test_record_never_commits_so_the_caller_owns_the_transaction(conn) -> None:
    alerts.record(conn, now_utc=NOW, level="WARNING", code="A", detail="x")
    assert conn.in_transaction
    conn.rollback()
    assert alerts.unacked(conn) == ()


def test_record_converts_an_aware_non_utc_time_to_utc(conn) -> None:
    plus_three = timezone(timedelta(hours=3))
    alerts.record(
        conn, now_utc=NOW.astimezone(plus_three), level="WARNING", code="A", detail="x"
    )
    (alert,) = alerts.unacked(conn)
    assert alert.created_at_utc == NOW
    assert alert.created_at_utc.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"now_utc": datetime(2026, 9, 5, 11, 0)}, "naive"),
        ({"level": "INFO"}, "level"),
        ({"code": ""}, "code"),
        ({"detail": ""}, "detail"),
    ],
)
def test_record_refuses_malformed_input(conn, kwargs, match) -> None:
    arguments = {"now_utc": NOW, "level": "WARNING", "code": "A", "detail": "x"} | kwargs
    with pytest.raises(ValueError, match=match):
        alerts.record(conn, **arguments)
    assert alerts.unacked(conn) == ()


def test_unacked_is_oldest_first_and_ack_removes_one(conn) -> None:
    first = alerts.record(conn, now_utc=NOW, level="WARNING", code="A", detail="1")
    second = alerts.record(conn, now_utc=NOW, level="CRITICAL", code="B", detail="2")

    assert [a.alert_id for a in alerts.unacked(conn)] == [first, second]
    alerts.ack(conn, alert_id=first)
    assert [a.alert_id for a in alerts.unacked(conn)] == [second]


def test_ack_of_an_unknown_or_already_acked_alert_raises(conn) -> None:
    alert_id = alerts.record(conn, now_utc=NOW, level="WARNING", code="A", detail="1")
    alerts.ack(conn, alert_id=alert_id)
    with pytest.raises(ValueError, match="not an unacknowledged alert"):
        alerts.ack(conn, alert_id=alert_id)
    with pytest.raises(ValueError, match="not an unacknowledged alert"):
        alerts.ack(conn, alert_id=alert_id + 100)


def test_the_table_itself_refuses_an_unknown_level_or_acked_value(conn) -> None:
    """The CHECK constraints, not just `record()`'s validation: Plan 3
    will write this table from another process."""
    insert = (
        "INSERT INTO alerts (created_at_utc, level, code, detail, acked) "
        "VALUES ('2026-09-05T11:00:00+00:00', ?, 'A', 'x', ?)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("INFO", 0))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, ("WARNING", 2))
    conn.execute(insert, ("WARNING", 0))  # control: the constraint is what fired
