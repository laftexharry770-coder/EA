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
import threading
from pathlib import Path

import asyncio
import pytest

from xaucopier.store.db import Writer, WriterMisuseError, connect

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


# -- Writer: cancellation, stop/submit races, transaction misuse -------------


def test_cancelling_before_pickup_prevents_the_write_from_landing(tmp_path: Path):
    """Reproduces the reviewer's finding: without
    `set_running_or_notify_cancel()`, a future cancelled while its job is
    still sitting in the queue used to have its write land anyway -- the
    caller sees CancelledError but the row was written regardless. `block`
    holds the writer thread on a `threading.Event` so the second job is
    guaranteed to still be queued (never picked up) when it is cancelled;
    only then is the blocking job released.
    """
    db_path = tmp_path / "store.db"
    writer = Writer(db_path)
    writer.start()

    release = threading.Event()

    def block(conn: sqlite3.Connection) -> None:
        assert release.wait(timeout=5.0), "release was never set"

    blocking_future = writer.submit(block)

    # The writer thread is now blocked inside `block`, waiting on
    # `release` -- it cannot possibly have reached this second job yet.
    second_future = writer.submit(
        lambda conn: _insert_channel_state(conn, channel_id=3, last_id=99)
    )
    cancelled = second_future.cancel()

    release.set()
    blocking_future.result(timeout=5.0)
    writer.stop(timeout=5.0)

    assert cancelled is True
    assert second_future.cancelled() is True

    reader = connect(db_path, role="reader")
    try:
        row = reader.execute(
            "SELECT last_processed_message_id FROM channel_state WHERE channel_id = 3"
        ).fetchone()
        assert row is None
    finally:
        reader.close()


def test_submit_after_stop_raises(tmp_path: Path):
    db_path = tmp_path / "store.db"
    writer = Writer(db_path)
    writer.start()
    writer.stop(timeout=5.0)

    with pytest.raises(RuntimeError):
        writer.submit(lambda conn: None)


def test_submit_racing_stop_never_hangs(tmp_path: Path):
    """Reproduces the reviewer's finding: submit()'s check-and-enqueue
    and stop()'s set-and-sentinel were not atomic with each other, so a
    submit() could pass the `_stopped` check, then have stop() run to
    completion, then enqueue its job behind a sentinel nothing will ever
    drain -- the future never resolves. Hammers submit() from many real
    OS threads concurrently with a single stop() and asserts every
    submit() either raised RuntimeError or its future completed --
    never silently hung.
    """
    db_path = tmp_path / "store.db"
    writer = Writer(db_path)
    writer.start()

    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def racer(i: int) -> None:
        try:
            future = writer.submit(
                lambda conn: _insert_channel_state(
                    conn, channel_id=100 + i, last_id=i
                )
            )
        except RuntimeError:
            with outcomes_lock:
                outcomes.append("raised")
            return
        try:
            future.result(timeout=5.0)
        except Exception as exc:  # anything other than a clean result is a bug
            with outcomes_lock:
                outcomes.append(f"error:{exc!r}")
            return
        with outcomes_lock:
            outcomes.append("completed")

    racers = [threading.Thread(target=racer, args=(i,)) for i in range(20)]
    stopper = threading.Thread(target=lambda: writer.stop(timeout=5.0))

    for t in racers:
        t.start()
    stopper.start()

    for t in racers:
        t.join(timeout=10.0)
    stopper.join(timeout=10.0)

    assert all(not t.is_alive() for t in racers), "a submit() call hung"
    assert not stopper.is_alive(), "stop() hung"
    assert len(outcomes) == 20
    assert set(outcomes) <= {"raised", "completed"}, outcomes


def test_fn_that_commits_then_raises_is_flagged_as_misuse(tmp_path: Path):
    """Reproduces the reviewer's finding: a `fn` that calls `commit()`
    itself and then raises used to have the exception surface normally
    while the row it wrote stayed durably committed -- `_rollback_quietly`
    has nothing left to undo by that point, so the caller is misled into
    thinking the write never happened. The Writer must instead detect
    that `fn` managed its own transaction and say so explicitly.
    """
    db_path = tmp_path / "store.db"
    writer = Writer(db_path)
    writer.start()

    def naughty(conn: sqlite3.Connection) -> None:
        _insert_channel_state(conn, channel_id=4, last_id=1)
        conn.commit()
        raise ValueError("boom after self-commit")

    async def go():
        future = writer.submit(naughty)
        with pytest.raises(WriterMisuseError):
            await asyncio.wrap_future(future)

    try:
        asyncio.run(go())
    finally:
        writer.stop(timeout=5.0)

    reader = connect(db_path, role="reader")
    try:
        # fn committed for real before raising -- the Writer's job is to
        # make the caller distrust its own result, not to un-commit a
        # write it never controlled in the first place.
        row = reader.execute(
            "SELECT last_processed_message_id FROM channel_state WHERE channel_id = 4"
        ).fetchone()
        assert row is not None
    finally:
        reader.close()


def test_well_behaved_fn_still_commits_normally(tmp_path: Path):
    """Companion to the misuse test above: a `fn` that leaves the
    transaction for the Writer to commit, as documented, must still work
    exactly as before the misuse check was added.
    """
    db_path = tmp_path / "store.db"
    writer = Writer(db_path)
    writer.start()

    async def go():
        future = writer.submit(
            lambda conn: _insert_channel_state(conn, channel_id=5, last_id=55)
        )
        await asyncio.wrap_future(future)

    try:
        asyncio.run(go())
    finally:
        writer.stop(timeout=5.0)

    reader = connect(db_path, role="reader")
    try:
        row = reader.execute(
            "SELECT last_processed_message_id FROM channel_state WHERE channel_id = 5"
        ).fetchone()
        assert row["last_processed_message_id"] == 55
    finally:
        reader.close()
