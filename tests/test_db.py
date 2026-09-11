"""Tests for the connection factory and writer thread (store/db.py).

The writer thread is the whole point: Python's sqlite3 is synchronous,
so a commit() on the asyncio event-loop thread blocks that loop for the
full lock-wait -- up to busy_timeout -- and a stalled loop is a
documented cause of dropped Telegram channel updates, which is a missed
trade. Every write from the (not-yet-written) listener goes through
`Writer.submit()` and is awaited as
`await asyncio.wrap_future(writer.submit(fn))`, exactly as these tests
drive it, so a lock wait blocks the writer thread and never the loop.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import asyncio
import pytest

from xaucopier.store.db import Writer, connect

EXPECTED_TABLES = {
    "messages", "signals", "followups", "outbox",
    "channel_state", "metrics",
}


def _insert_channel_state(conn: sqlite3.Connection, channel_id: int, last_id: int) -> None:
    conn.execute(
        "INSERT INTO channel_state (channel_id, last_processed_message_id) "
        "VALUES (?, ?)",
        (channel_id, last_id),
    )


# -- connect() ---------------------------------------------------------------


def test_connect_migrates_fresh_file(tmp_path: Path):
    conn = connect(tmp_path / "store.db", role="writer")
    try:
        (version,) = conn.execute("PRAGMA user_version").fetchone()
        assert version == 1
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert tables == EXPECTED_TABLES
    finally:
        conn.close()


def test_connect_enables_foreign_keys(tmp_path: Path):
    conn = connect(tmp_path / "store.db", role="reader")
    try:
        (fk,) = conn.execute("PRAGMA foreign_keys").fetchone()
        assert fk == 1
    finally:
        conn.close()


def test_connect_row_factory_is_sqlite3_row(tmp_path: Path):
    conn = connect(tmp_path / "store.db", role="reader")
    try:
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_connect_writer_is_fully_synchronous(tmp_path: Path):
    conn = connect(tmp_path / "store.db", role="writer")
    try:
        (synchronous,) = conn.execute("PRAGMA synchronous").fetchone()
        assert synchronous == 2  # FULL
    finally:
        conn.close()


def test_connect_reader_is_normal_synchronous(tmp_path: Path):
    conn = connect(tmp_path / "store.db", role="reader")
    try:
        (synchronous,) = conn.execute("PRAGMA synchronous").fetchone()
        assert synchronous == 1  # NORMAL
    finally:
        conn.close()


def test_connect_sets_wal_and_busy_timeout(tmp_path: Path):
    conn = connect(tmp_path / "store.db", role="writer")
    try:
        (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        assert mode.lower() == "wal"
        (timeout,) = conn.execute("PRAGMA busy_timeout").fetchone()
        assert timeout == 10000
    finally:
        conn.close()


# -- Writer -------------------------------------------------------------------


def test_write_from_coroutine_visible_to_reader(tmp_path: Path):
    db_path = tmp_path / "store.db"
    writer = Writer(db_path)
    writer.start()

    async def go():
        future = writer.submit(
            lambda conn: _insert_channel_state(conn, channel_id=1, last_id=42)
        )
        await asyncio.wrap_future(future)

    try:
        asyncio.run(go())
    finally:
        writer.stop(timeout=5.0)

    reader = connect(db_path, role="reader")
    try:
        row = reader.execute(
            "SELECT last_processed_message_id FROM channel_state WHERE channel_id = 1"
        ).fetchone()
        assert row["last_processed_message_id"] == 42
    finally:
        reader.close()


def test_exception_in_fn_surfaces_and_thread_survives(tmp_path: Path):
    db_path = tmp_path / "store.db"
    writer = Writer(db_path)
    writer.start()

    def bad(conn: sqlite3.Connection):
        raise ValueError("boom")

    async def go():
        bad_future = writer.submit(bad)
        with pytest.raises(ValueError, match="boom"):
            await asyncio.wrap_future(bad_future)

        # The thread must have survived the exception, and the writer
        # connection must be clean -- not stuck mid-transaction.
        good_future = writer.submit(
            lambda conn: _insert_channel_state(conn, channel_id=2, last_id=7)
        )
        await asyncio.wrap_future(good_future)

    try:
        asyncio.run(go())
    finally:
        writer.stop(timeout=5.0)

    reader = connect(db_path, role="reader")
    try:
        row = reader.execute(
            "SELECT last_processed_message_id FROM channel_state WHERE channel_id = 2"
        ).fetchone()
        assert row is not None
        assert row["last_processed_message_id"] == 7
    finally:
        reader.close()


def test_stop_drains_queued_writes(tmp_path: Path):
    db_path = tmp_path / "store.db"
    writer = Writer(db_path)
    writer.start()

    for channel_id in range(5):
        writer.submit(
            lambda conn, cid=channel_id: _insert_channel_state(
                conn, channel_id=cid, last_id=cid * 10
            )
        )

    # No awaiting here on purpose: stop() itself is the thing under test --
    # it must drain everything already queued before it returns.
    writer.stop(timeout=5.0)

    reader = connect(db_path, role="reader")
    try:
        rows = reader.execute(
            "SELECT channel_id, last_processed_message_id FROM channel_state "
            "ORDER BY channel_id"
        ).fetchall()
        assert [dict(r) for r in rows] == [
            {"channel_id": cid, "last_processed_message_id": cid * 10}
            for cid in range(5)
        ]
    finally:
        reader.close()


def test_concurrent_submits_serialise_without_database_locked(tmp_path: Path):
    db_path = tmp_path / "store.db"
    writer = Writer(db_path)
    writer.start()

    def insert_metric(conn: sqlite3.Connection, name: str, value: int) -> None:
        conn.execute(
            "INSERT INTO metrics (day_key, name, value) VALUES (?, ?, ?)",
            ("2026-09-11", name, value),
        )

    async def submit_many(prefix: str, count: int):
        # Submit the whole batch before awaiting any of it, so the two
        # coroutines' jobs genuinely interleave in the queue rather than
        # politely taking turns.
        futures = [
            writer.submit(lambda conn, i=i: insert_metric(conn, f"{prefix}-{i}", i))
            for i in range(count)
        ]
        for future in futures:
            await asyncio.wrap_future(future)

    async def go():
        await asyncio.gather(submit_many("a", 25), submit_many("b", 25))

    try:
        asyncio.run(go())
    finally:
        writer.stop(timeout=5.0)

    reader = connect(db_path, role="reader")
    try:
        (count,) = reader.execute("SELECT COUNT(*) FROM metrics").fetchone()
        assert count == 50
    finally:
        reader.close()
