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
    """Reproduces the Task 4 reviewer's finding: a `fn` that calls
    `commit()` itself and then raises used to have the exception surface
    normally while the row it wrote stayed durably committed. Since final
    review F20 the commit() is refused before it runs, so the caller is
    told WriterMisuseError *and* nothing the job wrote was kept.
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
        row = reader.execute(
            "SELECT last_processed_message_id FROM channel_state WHERE channel_id = 4"
        ).fetchone()
        assert row is None
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


# -- final review F20 / F21 / F31: the Writer owns the whole job ---------------
#
# Every job runs inside BEGIN IMMEDIATE, issued before fn: its reads and
# writes happen under the write lock (F21). fn gets a thin stand-in for
# the connection that refuses, before anything runs, every way of ending
# or reshaping that transaction (F20). A rollback SQLite makes itself --
# a full disk, INSERT OR ROLLBACK, RAISE(ROLLBACK) -- is reported as the
# real sqlite3 error, not as fn's misuse (F31).


def _run_jobs(db_path: Path, *jobs):
    """Run `jobs` in order on a fresh Writer; return each outcome: the
    result, or the exception instance."""
    writer = Writer(db_path)
    writer.start()
    outcomes = []
    try:
        for job in jobs:
            future = writer.submit(job)
            error = future.exception(timeout=10.0)
            outcomes.append(error if error is not None else future.result())
    finally:
        writer.stop(timeout=5.0)
    return outcomes


def _channel_ids(db_path: Path) -> list[int]:
    reader = connect(db_path, role="reader")
    try:
        return [
            row[0]
            for row in reader.execute("SELECT channel_id FROM channel_state ORDER BY 1")
        ]
    finally:
        reader.close()


def test_every_job_runs_inside_the_writers_write_transaction(tmp_path: Path):
    """F21: the job's first statement already runs in the transaction --
    before, a read before the first write ran outside it."""
    (seen,) = _run_jobs(tmp_path / "store.db", lambda conn: conn.in_transaction)
    assert seen is True


def test_a_jobs_reads_are_protected_by_the_write_lock(tmp_path: Path):
    """F21's scenario: a job reads, another connection tries to change
    what it read, then the job decides. The other connection must wait
    for the job (here: give up after 0.2 s) instead of committing in
    between."""
    db_path = tmp_path / "store.db"
    connect(db_path, role="writer").close()
    job_has_read = threading.Event()
    other_done = threading.Event()
    other_outcome: list[object] = []

    def other_writer():
        assert job_has_read.wait(timeout=5.0)
        other = sqlite3.connect(db_path, timeout=0.2)
        try:
            other.execute(
                "INSERT INTO channel_state (channel_id, last_processed_message_id) VALUES (8, 8)"
            )
            other.commit()
            other_outcome.append("committed")
        except sqlite3.OperationalError as exc:
            other_outcome.append(str(exc))
        finally:
            other.close()
            other_done.set()

    def job(conn: sqlite3.Connection) -> int:
        (count,) = conn.execute("SELECT COUNT(*) FROM channel_state").fetchone()
        job_has_read.set()
        assert other_done.wait(timeout=5.0)
        (count_again,) = conn.execute("SELECT COUNT(*) FROM channel_state").fetchone()
        assert count_again == count
        _insert_channel_state(conn, channel_id=7, last_id=count)
        return count

    thread = threading.Thread(target=other_writer)
    thread.start()
    (result,) = _run_jobs(db_path, job)
    thread.join(timeout=5.0)

    assert result == 0
    assert other_outcome == ["database is locked"]
    assert _channel_ids(db_path) == [7]


def _write_then(action):
    """A job that writes channel 1, then does `action(conn)`, then writes
    channel 2."""

    def job(conn: sqlite3.Connection) -> str:
        _insert_channel_state(conn, channel_id=1, last_id=1)
        action(conn)
        _insert_channel_state(conn, channel_id=2, last_id=2)
        return "returned"

    return job


def _set(name, value):
    return lambda conn: setattr(conn, name, value)


@pytest.mark.parametrize(
    "action",
    [
        lambda conn: conn.commit(),
        lambda conn: conn.rollback(),
        _set("isolation_level", None),
        _set("isolation_level", "IMMEDIATE"),
        _set("autocommit", True),
        _set("row_factory", None),
        lambda conn: conn.execute("COMMIT"),
        lambda conn: conn.execute("  commit transaction"),
        lambda conn: conn.execute("END"),
        lambda conn: conn.execute("-- done\nCOMMIT"),
        lambda conn: conn.execute("/* done */ COMMIT"),
        lambda conn: conn.execute("; COMMIT"),
        lambda conn: conn.execute("ROLLBACK"),
        lambda conn: conn.execute("BEGIN"),
        lambda conn: conn.execute("SAVEPOINT s"),
        lambda conn: conn.execute("RELEASE s"),
        lambda conn: conn.executemany("COMMIT", []),
        lambda conn: conn.executescript("SELECT 1;"),
        lambda conn: conn.cursor(),
        lambda conn: conn.close(),
        lambda conn: conn.execute("SELECT 1").execute("COMMIT"),
        lambda conn: conn.execute("SELECT 1").execute("/* x */ rollback"),
        lambda conn: conn.execute("SELECT 1").executemany("COMMIT", []),
        lambda conn: conn.execute("SELECT 1").executescript("SELECT 1;"),
        lambda conn: conn.execute("SELECT 1").connection,
        lambda conn: conn.executemany(
            "INSERT INTO heartbeats (name, at_utc) VALUES (?, ?)", []
        ).execute("SAVEPOINT s"),
    ],
    ids=[
        "commit()", "rollback()", "isolation_level=None", "isolation_level=IMMEDIATE",
        "autocommit=True", "row_factory", "COMMIT", "commit-transaction", "END",
        "comment-COMMIT", "block-comment-COMMIT", "semicolon-COMMIT", "ROLLBACK", "BEGIN", "SAVEPOINT",
        "RELEASE", "executemany-COMMIT", "executescript", "cursor()", "close()",
        "cursor-COMMIT", "cursor-comment-rollback", "cursor-executemany-COMMIT",
        "cursor-executescript", "cursor.connection", "executemany-cursor-SAVEPOINT",
    ],
)
def test_a_job_that_tries_to_manage_the_transaction_is_refused_and_writes_nothing(
    tmp_path: Path, action
):
    """F20. Before: only a *trailing* commit was noticed, and a job that
    wrote, committed and wrote again resolved normally with its first
    write already durable. Now every such call raises WriterMisuseError
    before it runs, the whole job is rolled back, and the future says so
    -- whatever fn did with the exception."""
    db_path = tmp_path / "store.db"
    raising, swallowing, after = _run_jobs(
        db_path,
        _write_then(action),
        _write_then(lambda conn: _swallow(action, conn)),
        lambda conn: _insert_channel_state(conn, channel_id=3, last_id=3),
    )
    assert isinstance(raising, WriterMisuseError), raising
    assert isinstance(swallowing, WriterMisuseError), swallowing
    assert "Nothing this job wrote was kept" in str(raising)
    assert after is None  # the thread survives, the next job commits
    assert _channel_ids(db_path) == [3]


def _swallow(action, conn):
    try:
        action(conn)
    except WriterMisuseError:
        pass


def test_ordinary_statements_still_run(tmp_path: Path):
    """The refusal is about transaction control, not keywords inside
    ordinary statements."""

    def job(conn: sqlite3.Connection) -> list:
        conn.execute("CREATE TABLE notes (body TEXT)")
        conn.executemany("INSERT INTO notes VALUES (?)", [("COMMIT",), ("end",)])
        conn.execute("-- a comment\nINSERT INTO notes VALUES ('ROLLBACK')")
        return [row[0] for row in conn.execute("SELECT body FROM notes ORDER BY rowid")]

    (result,) = _run_jobs(tmp_path / "store.db", job)
    assert result == ["COMMIT", "end", "ROLLBACK"]


def _full_disk_job(conn: sqlite3.Connection) -> None:
    """A nearly full disk: the file may grow by 3 pages, and one message
    needs many more."""
    (pages,) = conn.execute("PRAGMA page_count").fetchone()
    conn.execute(f"PRAGMA max_page_count = {pages + 3}")
    _insert_channel_state(conn, channel_id=1, last_id=1)
    conn.execute(
        "INSERT INTO messages (channel_id, message_id, kind, content_hash, raw_text, "
        "received_at_utc, posted_at_utc, source) VALUES (1, 1, 'skip', 'h', ?, 't', 't', 'live')",
        ("x" * 200_000,),
    )


def _or_rollback_job(conn: sqlite3.Connection) -> None:
    _insert_channel_state(conn, channel_id=1, last_id=1)
    conn.execute("INSERT OR ROLLBACK INTO channel_state (channel_id) VALUES (1)")


def _swallowing_or_rollback_job(conn: sqlite3.Connection) -> str:
    """Catches SQLite's rollback and carries on writing: the later write
    must not be committed as if it were the whole job."""
    _insert_channel_state(conn, channel_id=1, last_id=1)
    try:
        conn.execute("INSERT OR ROLLBACK INTO channel_state (channel_id) VALUES (1)")
    except sqlite3.IntegrityError:
        pass
    _insert_channel_state(conn, channel_id=2, last_id=2)
    return "carried on"


def _trigger_rollback_job(conn: sqlite3.Connection) -> None:
    _insert_channel_state(conn, channel_id=1, last_id=1)
    conn.execute("INSERT INTO heartbeats (name, at_utc) VALUES ('forbidden', 'x')")


@pytest.mark.parametrize(
    ("job", "error_type", "text"),
    [
        (_full_disk_job, sqlite3.OperationalError, "full"),
        (_or_rollback_job, sqlite3.IntegrityError, "UNIQUE"),
        (_swallowing_or_rollback_job, sqlite3.IntegrityError, "UNIQUE"),
        (_trigger_rollback_job, sqlite3.IntegrityError, "no forbidden heartbeat"),
    ],
    ids=["disk-full", "insert-or-rollback", "or-rollback-swallowed", "raise-rollback"],
)
def test_a_rollback_sqlite_made_itself_is_reported_as_the_real_error(
    tmp_path: Path, job, error_type, text
):
    """F31. Before: WriterMisuseError('fn called commit()/rollback()
    itself'), blaming a code bug that does not exist, for a full disk."""
    db_path = tmp_path / "store.db"
    conn = connect(db_path, role="writer")
    conn.execute(
        "CREATE TRIGGER no_forbidden BEFORE INSERT ON heartbeats "
        "WHEN NEW.name = 'forbidden' BEGIN SELECT RAISE(ROLLBACK, 'no forbidden heartbeat'); END"
    )
    conn.commit()
    conn.close()

    def after_job(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA max_page_count = 1073741823")  # undo the full-disk cap
        _insert_channel_state(conn, channel_id=3, last_id=3)

    outcome, after = _run_jobs(db_path, job, after_job)

    assert type(outcome) is error_type, repr(outcome)
    assert text in str(outcome)
    assert after is None
    assert _channel_ids(db_path) == [3]  # nothing of the failed job was kept


def test_claim_and_sweep_through_the_writer_fail_even_on_an_empty_queue(tmp_path: Path):
    """F20's other half (Plan 3 obligation 15's trap): claim() and
    sweep_expired() drive their own transaction, so routed through the
    Writer they used to 'work' on an empty queue and only misbehave once
    there was a row. Now they fail every time, before anything runs."""
    from datetime import UTC, datetime

    from xaucopier.store import outbox

    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    claimed, swept = _run_jobs(
        tmp_path / "store.db",
        lambda conn: outbox.claim(conn, account_id="acct-1", pid=1, now_utc=now),
        lambda conn: outbox.sweep_expired(conn, now_utc=now),
    )
    for outcome in (claimed, swept):
        assert isinstance(outcome, RuntimeError), repr(outcome)
        assert "not Writer work" in str(outcome)


def test_a_rollback_sqlite_made_through_a_cursor_is_still_the_real_error(tmp_path: Path):
    """The same F31 rule when the failing statement ran on a cursor fn
    got back from execute() -- not through the stand-in itself."""

    def job(conn: sqlite3.Connection) -> None:
        _insert_channel_state(conn, channel_id=1, last_id=1)
        cursor = conn.execute("SELECT 1")
        cursor.execute("INSERT OR ROLLBACK INTO channel_state (channel_id) VALUES (1)")

    db_path = tmp_path / "store.db"
    (outcome,) = _run_jobs(db_path, job)
    assert type(outcome) is sqlite3.IntegrityError, repr(outcome)
    assert _channel_ids(db_path) == []


def test_a_transaction_ended_by_an_unguarded_path_is_still_misuse(tmp_path: Path):
    """The backstop: reaching the real connection behind the stand-in's
    back (its private attribute) is not refused in advance, but a job that
    leaves the Writer's transaction closed fails with WriterMisuseError --
    which says its earlier writes may be kept."""

    def job(conn: sqlite3.Connection) -> str:
        _insert_channel_state(conn, channel_id=1, last_id=1)
        conn._conn.execute("COMMIT")
        return "looked fine"

    (outcome,) = _run_jobs(tmp_path / "store.db", job)
    assert isinstance(outcome, WriterMisuseError), repr(outcome)
    assert "may already be committed" in str(outcome)


def _misuse_swallowed(conn: sqlite3.Connection) -> None:
    _insert_channel_state(conn, channel_id=1, last_id=1)
    try:
        conn.commit()
    except WriterMisuseError:
        pass


def _plain_failure(conn: sqlite3.Connection) -> None:
    _insert_channel_state(conn, channel_id=1, last_id=1)
    raise ValueError("the job gave up")


@pytest.mark.parametrize(
    "job",
    [_swallowing_or_rollback_job, _misuse_swallowed, _plain_failure],
    ids=["sqlite-rollback-swallowed", "misuse-swallowed", "plain-failure"],
)
def test_a_failed_job_does_not_keep_the_write_lock(tmp_path: Path, job):
    """A failed job is rolled back at once, so it does not hold the write
    lock until the next job comes along (`ack`, or Plan 3's worker, would
    wait for it)."""
    db_path = tmp_path / "store.db"
    writer = Writer(db_path)
    writer.start()
    try:
        assert writer.submit(job).exception(timeout=10.0) is not None
        other = sqlite3.connect(db_path, timeout=0.2)
        try:
            _insert_channel_state(other, channel_id=9, last_id=9)
            other.commit()
        finally:
            other.close()
    finally:
        writer.stop(timeout=5.0)
    assert _channel_ids(db_path) == [9]


def test_a_job_never_starts_inside_a_transaction_a_failed_job_left_open(
    tmp_path: Path, monkeypatch
):
    """If an earlier job's rollback itself failed, the next job must not
    inherit its writes -- nor fail because a transaction is already open."""
    monkeypatch.setattr(Writer, "_rollback_quietly", staticmethod(lambda conn: None))
    db_path = tmp_path / "store.db"
    failed, good = _run_jobs(
        db_path,
        _plain_failure,
        lambda conn: _insert_channel_state(conn, channel_id=2, last_id=2),
    )
    assert isinstance(failed, ValueError)
    assert good is None
    assert _channel_ids(db_path) == [2]


def test_a_busy_database_fails_the_job_not_the_writer(tmp_path: Path, monkeypatch):
    """BEGIN IMMEDIATE waits for another program's write lock; if that
    outlasts the busy timeout, only this job fails ("database is locked")
    -- fn never ran -- and the next job runs normally."""
    monkeypatch.setattr(store_db, "_BUSY_TIMEOUT_MS", 100)
    db_path = tmp_path / "store.db"
    connect(db_path, role="writer").close()
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    ran: list[str] = []
    writer = Writer(db_path)
    writer.start()
    try:
        error = writer.submit(lambda conn: ran.append("first")).exception(timeout=10.0)
        holder.execute("ROLLBACK")
        after = writer.submit(
            lambda conn: _insert_channel_state(conn, channel_id=6, last_id=6)
        ).exception(timeout=10.0)
    finally:
        holder.close()
        writer.stop(timeout=5.0)
    assert isinstance(error, sqlite3.OperationalError) and "locked" in str(error)
    assert ran == []
    assert after is None
    assert _channel_ids(db_path) == [6]


# -- the cursor a job gets back is guarded too -----------------------------------
#
# Group-1 review of the final fix wave: execute() handed fn the real
# sqlite3.Cursor, and a cursor's own execute("COMMIT") was not refused.
# A job that wrote, committed through the cursor and wrote again resolved
# normally with its two writes landed in two separate commits -- the
# backstop only sees a transaction left closed, and the next write had
# opened a new one.


def _commit_through_the_cursor_then_write(conn: sqlite3.Connection) -> str:
    """The reviewer's shape: the cursor comes from the first INSERT."""
    cursor = conn.execute(
        "INSERT INTO channel_state (channel_id, last_processed_message_id) VALUES (1, 1)"
    )
    cursor.execute("COMMIT")
    _insert_channel_state(conn, channel_id=2, last_id=2)
    return "looks atomic"


def _commit_through_the_cursor_swallowed_then_raise(conn: sqlite3.Connection) -> None:
    cursor = conn.execute(
        "INSERT INTO channel_state (channel_id, last_processed_message_id) VALUES (1, 1)"
    )
    try:
        cursor.execute("COMMIT")
    except WriterMisuseError:
        pass
    _insert_channel_state(conn, channel_id=2, last_id=2)
    raise ValueError("the job gave up")


@pytest.mark.parametrize(
    "job",
    [_commit_through_the_cursor_then_write, _commit_through_the_cursor_swallowed_then_raise],
    ids=["returns", "swallows-then-raises"],
)
def test_a_commit_through_the_cursor_is_refused_and_writes_nothing(tmp_path: Path, job):
    db_path = tmp_path / "store.db"
    (outcome,) = _run_jobs(db_path, job)
    assert isinstance(outcome, WriterMisuseError), repr(outcome)
    assert "Nothing this job wrote was kept" in str(outcome)
    assert _channel_ids(db_path) == []


def test_the_cursor_a_job_gets_back_still_reads_and_reports(tmp_path: Path):
    """The guard is about transaction control only: everything a job does
    with a cursor today still works through the guarded one."""

    def job(conn: sqlite3.Connection) -> dict:
        inserted = conn.execute(
            "INSERT INTO channel_state (channel_id, last_processed_message_id) VALUES (1, 10)"
        )
        conn.executemany(
            "INSERT INTO channel_state (channel_id, last_processed_message_id) VALUES (?, ?)",
            [(2, 20), (3, 30)],
        )
        updated = conn.execute("UPDATE channel_state SET last_processed_message_id = 0 "
                               "WHERE channel_id > 1")
        cursor = conn.execute("SELECT channel_id FROM channel_state ORDER BY 1")
        again = cursor.execute("SELECT channel_id, last_processed_message_id "
                               "FROM channel_state ORDER BY 1")
        return {
            "lastrowid": inserted.lastrowid,
            "rowcount": updated.rowcount,
            "same cursor": again is cursor,
            "description": [column[0] for column in again.description],
            "first": tuple(again.fetchone()),
            "rest": [tuple(row) for row in again],
            "fetchall": [row["channel_id"] for row in conn.execute(
                "SELECT channel_id FROM channel_state ORDER BY 1").fetchall()],
        }

    (result,) = _run_jobs(tmp_path / "store.db", job)
    assert result == {
        "lastrowid": 1,
        "rowcount": 2,
        "same cursor": True,
        "description": ["channel_id", "last_processed_message_id"],
        "first": (1, 10),
        "rest": [(2, 0), (3, 0)],
        "fetchall": [1, 2, 3],
    }


# -- read-only jobs: they never wait for the write lock ------------------------------
#
# The same review's Important finding: the listener's writer probes only
# prove that the writer thread answers. Under F21's BEGIN IMMEDIATE they
# waited for the write lock like any job, so another program holding it
# past the busy timeout failed the poll's probe and stopped
# `.\xaucopier watch`. submit(fn, read_only=True) runs fn without opening
# a transaction -- and fn may then only read.


def _read_only(db_path: Path, *jobs):
    """Like _run_jobs, with every job submitted read_only=True."""
    writer = Writer(db_path)
    writer.start()
    outcomes = []
    try:
        for job in jobs:
            future = writer.submit(job, read_only=True)
            error = future.exception(timeout=10.0)
            outcomes.append(error if error is not None else future.result())
    finally:
        writer.stop(timeout=5.0)
    return outcomes


def test_a_read_only_job_runs_while_another_program_holds_the_write_lock(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(store_db, "_BUSY_TIMEOUT_MS", 100)
    db_path = tmp_path / "store.db"
    seed = connect(db_path, role="writer")
    _insert_channel_state(seed, channel_id=1, last_id=1)
    seed.commit()
    seed.close()
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    writer = Writer(db_path)
    writer.start()
    try:
        read = writer.submit(
            lambda conn: (
                conn.in_transaction,
                conn.execute("SELECT COUNT(*) FROM channel_state").fetchone()[0],
            ),
            read_only=True,
        ).result(timeout=10.0)
        write = writer.submit(lambda conn: None).exception(timeout=10.0)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        writer.stop(timeout=5.0)
    assert read == (False, 1)
    # An ordinary job still takes the write lock first, as F21 requires.
    assert isinstance(write, sqlite3.OperationalError) and "locked" in str(write)


_WRITES = [
    "INSERT INTO channel_state (channel_id, last_processed_message_id) VALUES (5, 5)",
    "UPDATE channel_state SET last_processed_message_id = 6",
    "DELETE FROM channel_state",
    "REPLACE INTO channel_state (channel_id, last_processed_message_id) VALUES (1, 9)",
    "WITH x(id) AS (SELECT 5) INSERT INTO channel_state (channel_id) SELECT id FROM x",
    "/* sneaky */ insert into channel_state (channel_id) values (5)",
    "CREATE TABLE notes (body TEXT)",
    "DROP TABLE heartbeats",
    "PRAGMA user_version = 99",
    "BEGIN IMMEDIATE",
]


def _via_execute(sql):
    return lambda conn: conn.execute(sql)


def _via_cursor(sql):
    return lambda conn: conn.execute("SELECT 1").execute(sql)


def _via_executemany(sql):
    return lambda conn: conn.executemany(sql, [()])


@pytest.mark.parametrize("sql", _WRITES, ids=[s.split()[0] if s[0] != "/" else "comment"
                                              for s in _WRITES])
@pytest.mark.parametrize("path", [_via_execute, _via_cursor, _via_executemany],
                         ids=["execute", "cursor", "executemany"])
def test_a_read_only_job_can_only_read(tmp_path: Path, sql, path):
    db_path = tmp_path / "store.db"
    seed = connect(db_path, role="writer")
    _insert_channel_state(seed, channel_id=1, last_id=1)
    seed.commit()
    seed.close()
    action = path(sql)

    def swallowing(conn: sqlite3.Connection) -> str:
        try:
            action(conn)
        except WriterMisuseError:
            pass
        return "carried on"

    raising, swallowed = _read_only(db_path, action, swallowing)

    for outcome in (raising, swallowed):
        assert isinstance(outcome, WriterMisuseError), repr(outcome)
        assert "read-only" in str(outcome)
    reader = connect(db_path, role="reader")
    try:
        rows = [tuple(r) for r in reader.execute("SELECT * FROM channel_state")]
        tables = {r[0] for r in reader.execute("SELECT name FROM sqlite_master")}
        (version,) = reader.execute("PRAGMA user_version").fetchone()
    finally:
        reader.close()
    assert [row[:2] for row in rows] == [(1, 1)]
    assert "notes" not in tables and "heartbeats" in tables
    assert version == CURRENT_VERSION


@pytest.mark.parametrize(
    "action",
    [lambda conn: conn.commit(), lambda conn: conn.rollback(),
     lambda conn: conn.executescript("SELECT 1;"), _set("isolation_level", None)],
    ids=["commit()", "rollback()", "executescript", "isolation_level=None"],
)
def test_a_read_only_job_is_refused_transaction_control_like_any_job(tmp_path: Path, action):
    (outcome,) = _read_only(tmp_path / "store.db", action)
    assert isinstance(outcome, WriterMisuseError), repr(outcome)


def test_a_read_only_job_returns_its_result_or_its_own_error(tmp_path: Path):
    def fails(conn: sqlite3.Connection) -> None:
        conn.execute("SELECT 1").fetchone()
        raise ValueError("the probe gave up")

    selected, failed, after = _read_only(
        tmp_path / "store.db",
        lambda conn: conn.execute("SELECT 41 + 1").fetchone()[0],
        fails,
        lambda conn: "the thread survived",
    )
    assert selected == 42
    assert isinstance(failed, ValueError) and str(failed) == "the probe gave up"
    assert after == "the thread survived"


def test_a_read_only_job_that_writes_behind_the_stand_ins_back_is_rolled_back(
    tmp_path: Path,
):
    """The backstop: a write that reached the real connection another way
    (its private attribute) opened a transaction; it is rolled back, and
    the job fails as misuse instead of resolving as if it had only read."""
    db_path = tmp_path / "store.db"

    def sneaky(conn: sqlite3.Connection) -> str:
        _insert_channel_state(conn._conn, channel_id=7, last_id=7)
        return "only read, honest"

    (outcome,) = _read_only(db_path, sneaky)
    assert isinstance(outcome, WriterMisuseError), repr(outcome)
    assert "read-only" in str(outcome) and "rolled back" in str(outcome)
    assert _channel_ids(db_path) == []


def test_a_read_only_job_never_runs_inside_a_transaction_a_failed_job_left_open(
    tmp_path: Path, monkeypatch
):
    """Like an ordinary job: an earlier job's write lock and writes are
    never inherited -- a read-only job rolls them back before it runs."""
    monkeypatch.setattr(Writer, "_rollback_quietly", staticmethod(lambda conn: None))
    db_path = tmp_path / "store.db"
    writer = Writer(db_path)
    writer.start()
    try:
        failed = writer.submit(_plain_failure).exception(timeout=10.0)
        seen = writer.submit(
            lambda conn: (conn.in_transaction,
                          conn.execute("SELECT COUNT(*) FROM channel_state").fetchone()[0]),
            read_only=True,
        ).result(timeout=10.0)
        other = sqlite3.connect(db_path, timeout=0.2)
        try:
            _insert_channel_state(other, channel_id=9, last_id=9)
            other.commit()
        finally:
            other.close()
    finally:
        writer.stop(timeout=5.0)
    assert isinstance(failed, ValueError)
    assert seen == (False, 0)
    assert _channel_ids(db_path) == [9]
