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

from xaucopier.store import db as store_db
from xaucopier.store.db import Writer, WriterMisuseError, connect
from xaucopier.store.migrations import CURRENT_VERSION

EXPECTED_TABLES = {
    "messages", "signals", "followups", "outbox",
    "channel_state", "metrics", "alerts", "heartbeats",
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
        assert version == CURRENT_VERSION
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


# -- a writer thread that dies -------------------------------------------------
#
# The reviewer's D6: with the database's parent directory missing,
# `connect()` raised on the writer thread, the thread died, and every
# future already queued -- including the listener's startup probe --
# stayed pending forever. The listener sat blocked in start(), never
# connected to Telegram, and looked alive to its supervisor. A dead
# writer must fail every job it will now never run, loudly and at once.


def test_writer_whose_connect_fails_fails_every_pending_future(tmp_path: Path):
    db_path = tmp_path / "no_such_dir" / "store.db"
    writer = Writer(db_path)
    ran: list[str] = []

    # Queued before the thread even exists, so all three are pending
    # when connect() fails.
    first = writer.submit(lambda conn: ran.append("first"))
    cancelled = writer.submit(lambda conn: ran.append("cancelled"))
    second = writer.submit(lambda conn: ran.append("second"))
    assert cancelled.cancel()

    writer.start()
    try:
        errors = [first.exception(timeout=5.0), second.exception(timeout=5.0)]
    finally:
        writer.stop(timeout=5.0)

    for error in errors:
        assert isinstance(error, store_db.WriterDeadError), error
        assert isinstance(error.__cause__, sqlite3.OperationalError), error.__cause__
        assert "unable to open database file" in str(error)
    assert errors[0] is not errors[1], "each future needs its own exception instance"
    assert cancelled.cancelled()
    assert ran == []


def test_submit_after_the_writer_died_fails_immediately(tmp_path: Path):
    db_path = tmp_path / "no_such_dir" / "store.db"
    writer = Writer(db_path)
    writer.start()
    ran: list[str] = []
    try:
        writer.submit(lambda conn: None).exception(timeout=5.0)  # wait for the death

        late = writer.submit(lambda conn: ran.append("late"))
        error = late.exception(timeout=0.5)
    finally:
        writer.stop(timeout=5.0)

    assert isinstance(error, store_db.WriterDeadError)
    assert isinstance(error.__cause__, sqlite3.OperationalError)
    assert ran == []


def test_writer_killed_mid_job_fails_the_in_flight_and_every_queued_job(tmp_path: Path):
    """Not only connect(): a BaseException escaping a job (which the
    per-job `except Exception` deliberately does not swallow) also ends
    the thread, and must not orphan the job it was running or the ones
    queued behind it."""
    db_path = tmp_path / "store.db"
    writer = Writer(db_path)
    writer.start()
    release = threading.Event()

    def blocker(conn: sqlite3.Connection) -> str:
        assert release.wait(timeout=5.0)
        return "blocker done"

    def killer(conn: sqlite3.Connection) -> None:
        raise SystemExit("writer job called sys.exit()")

    try:
        blocked = writer.submit(blocker)
        killed = writer.submit(killer)
        queued = writer.submit(
            lambda conn: _insert_channel_state(conn, channel_id=9, last_id=9)
        )
        release.set()

        assert blocked.result(timeout=5.0) == "blocker done"
        killed_error = killed.exception(timeout=5.0)
        queued_error = queued.exception(timeout=5.0)
    finally:
        writer.stop(timeout=5.0)

    assert isinstance(killed_error, store_db.WriterDeadError)
    assert isinstance(killed_error.__cause__, SystemExit)
    assert isinstance(queued_error, store_db.WriterDeadError)
    assert isinstance(queued_error.__cause__, SystemExit)


# -- final review F22: every role refuses a database from a newer version -----


def _newer_database(db_path: Path) -> None:
    conn = connect(db_path, role="writer")
    conn.execute(f"PRAGMA user_version = {CURRENT_VERSION + 1}")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("role", ["writer", "reader"])
def test_connect_refuses_a_database_from_a_newer_version(tmp_path: Path, role: str):
    """Rolling back from a later build must not let this one write into a
    schema it does not understand. Before F22, connect() accepted the file
    and left user_version as it was; only connect_readonly refused."""
    db_path = tmp_path / "store.db"
    _newer_database(db_path)

    with pytest.raises(store_db.SchemaVersionError, match=f"reads version {CURRENT_VERSION}"):
        connect(db_path, role=role)

    # The refused connection was closed, not leaked: on Windows an open
    # SQLite connection keeps the file from being deleted.
    raw = sqlite3.connect(db_path)
    (version,) = raw.execute("PRAGMA user_version").fetchone()
    raw.close()
    assert version == CURRENT_VERSION + 1
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"store.db{suffix}").unlink(missing_ok=True)
    assert not db_path.exists()


def test_connect_readonly_refuses_a_database_from_a_newer_version(tmp_path: Path):
    db_path = tmp_path / "store.db"
    _newer_database(db_path)
    with pytest.raises(store_db.SchemaVersionError) as caught:
        store_db.connect_readonly(db_path)
    assert caught.value.newer is True
    assert "newer version of xaucopier" in str(caught.value)


def test_a_writer_on_a_newer_database_dies_and_fails_its_jobs(tmp_path: Path):
    db_path = tmp_path / "store.db"
    _newer_database(db_path)
    writer = Writer(db_path)
    writer.start()
    try:
        error = writer.submit(lambda conn: None).exception(timeout=5.0)
    finally:
        writer.stop(timeout=5.0)
    assert isinstance(error, store_db.WriterDeadError)
    assert isinstance(error.__cause__, store_db.SchemaVersionError)


def test_schema_version_error_is_a_database_error():
    """So every caller that already reports "the database cannot be used"
    for an sqlite3.Error (watch's startup stamp) reports this too, instead
    of a traceback."""
    assert issubclass(store_db.SchemaVersionError, sqlite3.DatabaseError)
