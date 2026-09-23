"""Tests for `store.heartbeats` (migration 3's "when did X last happen")
and `store.db.connect_readonly()`, the connection `status` and
`export-fixtures` read through.

`status` runs in its own process, so it can only report what `watch`
wrote down -- and it must be unable to write anything itself. The second
half of this file proves the read-only connection is enforced by SQLite,
not by good intentions.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from xaucopier.store import db, heartbeats
from xaucopier.store.migrations import CURRENT_VERSION

NOW = datetime(2026, 9, 5, 11, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "store.db", role="writer")
    try:
        yield connection
    finally:
        connection.close()


def test_beat_creates_then_replaces_one_row_per_name(conn) -> None:
    heartbeats.beat(conn, name=heartbeats.POLL_ROUND, now_utc=NOW, detail="1 of 1")
    heartbeats.beat(conn, name=heartbeats.POLL_ROUND, now_utc=NOW + timedelta(seconds=5))
    heartbeats.beat(conn, name=heartbeats.WATCH_STARTED, now_utc=NOW)

    assert heartbeats.read_all(conn) == (
        heartbeats.Heartbeat(heartbeats.POLL_ROUND, NOW + timedelta(seconds=5), None),
        heartbeats.Heartbeat(heartbeats.WATCH_STARTED, NOW, None),
    )


def test_beat_never_commits_so_the_caller_owns_the_transaction(conn) -> None:
    heartbeats.beat(conn, name="x", now_utc=NOW)
    assert conn.in_transaction
    conn.rollback()
    assert heartbeats.read_all(conn) == ()


def test_beat_stores_utc_even_for_a_non_utc_aware_time(conn) -> None:
    nairobi = timezone(timedelta(hours=3))
    heartbeats.beat(conn, name="x", now_utc=NOW.astimezone(nairobi))
    (row,) = conn.execute("SELECT at_utc FROM heartbeats").fetchall()
    assert row[0] == NOW.isoformat()


@pytest.mark.parametrize(
    "kwargs",
    [{"name": "", "now_utc": NOW}, {"name": "x", "now_utc": NOW.replace(tzinfo=None)}],
)
def test_beat_refuses_an_empty_name_or_a_naive_time(conn, kwargs) -> None:
    with pytest.raises(ValueError):
        heartbeats.beat(conn, **kwargs)


def test_channel_read_name_is_per_channel_and_refuses_a_non_int() -> None:
    assert heartbeats.channel_read_name(-1001234567890) == "channel_read:-1001234567890"
    with pytest.raises(TypeError):
        heartbeats.channel_read_name(True)
    with pytest.raises(TypeError):
        heartbeats.channel_read_name("-100123")  # type: ignore[arg-type]


# -- connect_readonly ---------------------------------------------------------


def _seeded(db_path: Path) -> None:
    conn = db.connect(db_path, role="writer")
    heartbeats.beat(conn, name="x", now_utc=NOW)
    conn.commit()
    conn.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_connect_readonly_reads_but_sqlite_itself_refuses_every_write(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    _seeded(db_path)
    before = _digest(db_path)

    conn = db.connect_readonly(db_path)
    try:
        assert [b.name for b in heartbeats.read_all(conn)] == ["x"]
        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
            heartbeats.beat(conn, name="y", now_utc=NOW)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE sneaky (x)")
        assert conn.total_changes == 0
    finally:
        conn.close()

    assert _digest(db_path) == before


def test_connect_readonly_never_creates_a_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nothing-here.db"
    with pytest.raises(FileNotFoundError):
        db.connect_readonly(missing)
    assert not missing.exists()


def test_connect_readonly_never_migrates_an_older_database(tmp_path: Path) -> None:
    """Migrating is a write. An old file is refused and left as it was."""
    db_path = tmp_path / "old.db"
    raw = sqlite3.connect(db_path)
    raw.execute("CREATE TABLE t (x)")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()
    before = _digest(db_path)

    with pytest.raises(db.SchemaVersionError, match=f"version 1.*reads version {CURRENT_VERSION}"):
        db.connect_readonly(db_path)

    assert _digest(db_path) == before


def test_connect_readonly_sees_what_a_live_writer_has_committed(tmp_path: Path) -> None:
    """`status` runs while `watch` is running: WAL mode lets it read every
    committed row without waiting for the writer, and without a checkpoint."""
    db_path = tmp_path / "store.db"
    writer = db.connect(db_path, role="writer")
    try:
        heartbeats.beat(writer, name="live", now_utc=NOW)
        writer.commit()
        reader = db.connect_readonly(db_path)
        try:
            assert [b.name for b in heartbeats.read_all(reader)] == ["live"]
        finally:
            reader.close()
    finally:
        writer.close()
