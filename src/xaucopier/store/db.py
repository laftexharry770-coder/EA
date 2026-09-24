"""SQLite connection factory and the dedicated writer thread.

Python's ``sqlite3`` module is synchronous: a ``commit()`` blocks the
calling thread for the entire duration of any lock wait, up to
``busy_timeout``. If that call happens on the asyncio event-loop thread,
the loop itself stalls for that whole window -- and a stalled loop is a
documented cause of *dropped Telegram channel updates*, which is a
missed trade. So every write from the listener is handed to exactly one
dedicated thread that owns the only write connection; the listener
``await``s the result via ``asyncio.wrap_future(writer.submit(fn))``. A
lock wait then blocks the writer thread, never update reception.

Design decisions (the brief specifies the interface, not the body):

- One ``queue.Queue``, one thread. No pool -- a single writer connection
  cannot usefully be shared by more than one writer thread anyway
  (SQLite serialises writers at the file level regardless), so a pool
  would add complexity for zero throughput gain.
- Each submitted ``fn`` runs inside its own transaction, opened with
  ``BEGIN IMMEDIATE`` *before* ``fn`` runs: a normal return commits it,
  any exception rolls it back. Every statement ``fn`` runs, reads
  included, is therefore under the write lock, so no other program can
  change what ``fn`` read before ``fn`` decides and writes (final review
  F21). ``submit()`` is "one job, one all-or-nothing write", which is the
  unit the listener actually wants.
- ``fn`` never gets the connection itself, only a thin stand-in for it
  (``_JobConnection``) that refuses, *before anything runs*, every way of
  ending or reshaping that transaction: ``commit()``, ``rollback()``,
  ``close()``, ``cursor()``, ``executescript()`` (which commits first),
  assigning ``isolation_level``, ``autocommit`` or any other attribute,
  and ``execute()``/``executemany()`` of ``BEGIN``, ``COMMIT``, ``END``,
  ``ROLLBACK``, ``SAVEPOINT`` or ``RELEASE``. The cursor ``execute()``
  returns is a guarded stand-in too (``_JobCursor``): its own
  ``execute()``/``executemany()`` are checked the same way, its
  ``executescript()`` is refused, and it does not hand out the real
  connection (``.connection``). Each refusal raises ``WriterMisuseError``,
  and the job is rolled back and fails with it even if ``fn`` caught it
  (final review F20). See ``WriterMisuseError``.
- ``submit(fn, read_only=True)`` is for a job that only reads -- the
  listener's writer probes, which prove the writer thread still answers.
  No transaction is opened for it, so it never waits for the write lock:
  another program holding that lock past the busy timeout must not make
  a healthy writer look dead and stop ``watch`` (group-1 review of the
  final fix wave). Such a job may run ``SELECT`` statements only; any
  other statement is refused before it runs, with ``WriterMisuseError``.
- ``stop()`` pushes a sentinel onto the queue rather than signalling the
  thread out-of-band. Because ``queue.Queue`` is FIFO, the sentinel is
  only ever processed after every job submitted before ``stop()`` was
  called returns -- but ``submit()``'s check-and-enqueue and ``stop()``'s
  set-and-sentinel share a lock, because FIFO ordering alone does not
  make "check `_stopped`, then enqueue" atomic with respect to a
  concurrent ``stop()`` landing in between those two steps.
- A future is only allowed to start running ``fn`` if
  ``set_running_or_notify_cancel()`` says so. A bare
  ``concurrent.futures.Future`` has no built-in link between "the caller
  cancelled it" and "the thread should not run the job" -- without this
  call, a cancelled future can still have its write silently land, while
  the caller believes ``CancelledError`` means nothing happened.
- A writer thread that dies -- ``connect()`` failing (a missing parent
  directory, a locked or corrupt file), or a ``BaseException`` escaping a
  job -- fails the job it was running, every job still queued, and every
  job submitted afterwards with ``WriterDeadError``. Before this, a
  ``connect()`` failure killed the thread with its queue unserviced: every
  pending future stayed pending forever, and a caller awaiting one (the
  listener's startup probe) hung while the process looked alive.
"""
from __future__ import annotations

import concurrent.futures
import logging
import queue
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Literal, NoReturn, TypeVar

from xaucopier.store.migrations import (
    CURRENT_VERSION,
    SchemaVersionError,
    migrate,
    newer_version_error,
)

__all__ = [
    "SchemaVersionError",
    "Writer",
    "WriterDeadError",
    "WriterMisuseError",
    "connect",
    "connect_readonly",
]

Role = Literal["writer", "reader"]

_BUSY_TIMEOUT_MS = 10_000

T = TypeVar("T")

logger = logging.getLogger(__name__)


def connect(path: Path, *, role: Role) -> sqlite3.Connection:
    """Open a connection to the store at `path`, configured for `role`.

    Always sets WAL journaling, `foreign_keys=ON`, and a 10s
    `busy_timeout` so a lock wait retries instead of failing immediately
    with "database is locked". `synchronous` is FULL for the writer --
    the one connection whose durability actually matters, since it is
    the only connection that ever commits -- and NORMAL for readers,
    which is the standard, safe pairing with WAL (NORMAL only risks the
    last commit on a full OS crash, never on an application crash, and
    a read-only connection has nothing to lose from that anyway).

    Runs `migrations.migrate()` before returning, so a brand new file is
    immediately usable regardless of which role opens it first. A file
    written by a newer version of xaucopier is refused for every role
    with `SchemaVersionError` (final review F22), and the connection is
    closed before the error leaves.
    """
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute(f"PRAGMA synchronous = {'FULL' if role == 'writer' else 'NORMAL'}")
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def connect_readonly(path: Path) -> sqlite3.Connection:
    """A connection that **cannot** write to the store at `path` -- for
    `xaucopier status` and `export-fixtures`, which only report.

    `connect(role="reader")` is not read-only in that sense: it creates a
    missing file, sets `journal_mode` (a write on a new file), runs
    `migrate()` (a write on an older one), and as a read-write connection
    it checkpoints the WAL into the main file when it is the last one to
    close. This one opens the file with SQLite's `mode=ro`, so the library
    itself refuses every write ("attempt to write a readonly database"),
    and sets `query_only` on top. SQLite may still create an empty `-wal`
    and the `-shm` index beside a WAL database it reads; neither holds
    data, and the main file is never touched.

    Raises `FileNotFoundError` for a missing file (it is never created)
    and `SchemaVersionError` for a schema other than `CURRENT_VERSION`:
    an older one is never migrated here (that would be a write), and a
    newer one (`.newer`) is from a later build of xaucopier.
    """
    if not path.is_file():
        raise FileNotFoundError(f"no database at {path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA query_only = ON")
        (version,) = conn.execute("PRAGMA user_version").fetchone()
        if version > CURRENT_VERSION:
            raise newer_version_error(version, where=f"the database at {path}")
        if version != CURRENT_VERSION:
            raise SchemaVersionError(
                f"the database at {path} is at schema version {version}; this "
                f"version of xaucopier reads version {CURRENT_VERSION}",
                found=version,
                expected=CURRENT_VERSION,
            )
    except BaseException:
        conn.close()
        raise
    return conn


class WriterMisuseError(RuntimeError):
    """A submitted `fn` tried to manage the transaction the `Writer` owns
    for the whole job.

    Raised *inside* `fn`, before the call does anything, by the stand-in
    connection `fn` is given: `commit()`, `rollback()`, `close()`,
    `cursor()`, `executescript()`, assigning `isolation_level`,
    `autocommit` or any other attribute, or `execute()`/`executemany()`
    of a transaction-control statement (BEGIN, COMMIT, END, ROLLBACK,
    SAVEPOINT, RELEASE -- leading comments and case do not hide one).
    The same goes for the cursor `execute()` returns: its `execute()`/
    `executemany()` of such a statement, its `executescript()`, and
    asking it for `.connection`. In a read-only job (`submit(fn,
    read_only=True)`), any statement other than a `SELECT` is refused
    too. The job is then rolled back and its future fails with this
    error, even if `fn` caught it and carried on: nothing the job wrote
    is kept.

    One backstop remains for reaching the real connection behind the
    stand-in's back (its private attribute): if the Writer's transaction
    is found closed after `fn` returns, and SQLite did not close it
    itself, the future fails with this error too -- and in that case what
    `fn` wrote before closing it may already be committed. A read-only
    job found to have opened a transaction that way is rolled back and
    fails with this error.

    Not raised for a rollback SQLite makes on its own (a full disk,
    `INSERT OR ROLLBACK`, a trigger's `RAISE(ROLLBACK)`): that job's future
    fails with the real `sqlite3` error, and nothing of the job is kept
    (final review F31).
    """


class WriterDeadError(RuntimeError):
    """The writer thread has died, so this job did not run to completion
    and never will, and no later job will run either.

    Set on the future of the job the thread was running when it died,
    of every job still queued, and of every job submitted afterwards --
    a fresh instance each time, with `__cause__` set to whatever killed
    the thread (e.g. `sqlite3.OperationalError: unable to open database
    file` from `connect()`). A distinct type rather than that exception
    itself, so a caller can tell "your job failed" (maybe transient) from
    "there is no writer any more" (permanent for this process).
    """


class Writer:
    """Owns the only write connection to the store.

    `submit(fn)` hands `fn` to a single dedicated thread, which runs it
    against the writer's own `sqlite3.Connection` and resolves the
    returned future with `fn`'s result -- or, if `fn` raises, with that
    exception. The thread itself never dies from a bad `fn`: the
    exception is caught, the transaction is rolled back, and the thread
    loops back to `queue.get()` for the next job. If the thread dies
    anyway -- `connect()` fails, or a `BaseException` escapes `fn` --
    every job it will now never run is failed with `WriterDeadError`
    rather than left pending forever.

    A future cancelled before the writer thread reaches it prevents `fn`
    from running at all (see `submit`); once `fn` has started, it always
    runs to completion and cancellation requests are refused, matching
    the standard `concurrent.futures.Future` contract.

    `start()` / `stop()` are not meant to be called concurrently with
    each other from multiple threads. `submit()` is safe to call from
    anywhere, including concurrently with `stop()`: both share a lock
    around their queue-mutating critical sections, so a `submit()`
    racing a `stop()` either lands ahead of the stop sentinel and runs
    normally, or observes the writer already stopped and raises
    `RuntimeError` -- it can never be silently enqueued behind a
    sentinel nothing will ever drain.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()
        # What killed the writer thread, once it has died. Read and
        # written only under _lifecycle_lock, so a submit() either lands
        # in the queue before _die() drains it, or sees this set.
        self._death: BaseException | None = None
        # Guards the two critical sections that must not interleave:
        # submit()'s "check _stopped, then enqueue" and stop()'s "set
        # _stopped, then enqueue the sentinel". Without this lock, a
        # submit() that checks _stopped just before stop() sets it can
        # still enqueue its job just after stop() has already pushed the
        # sentinel -- orphaning that job behind a queue nothing will ever
        # drain again, and its future never resolves.
        self._lifecycle_lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Writer already started")
        self._thread = threading.Thread(
            target=self._run, name="xaucopier-writer", daemon=True
        )
        self._thread.start()

    def submit(
        self, fn: Callable[[sqlite3.Connection], T], *, read_only: bool = False
    ) -> concurrent.futures.Future[T]:
        """Queue `fn` to run on the writer thread. Returns a
        `concurrent.futures.Future` that resolves with `fn`'s return
        value (or its exception). The listener awaits this with
        `await asyncio.wrap_future(writer.submit(fn))`.

        `fn` runs inside a transaction the Writer opens with BEGIN
        IMMEDIATE before calling it, so all of `fn` -- reads included --
        runs under the write lock. A normal return commits; an exception
        rolls back and fails the future with that exception. `fn` is
        given a stand-in for the connection that only runs statements:
        any attempt to commit, roll back or otherwise end or reshape the
        transaction -- through the stand-in or through a cursor it
        returned -- raises `WriterMisuseError` before it runs, and the
        whole job is rolled back and fails with it (see that class). A
        rollback SQLite makes itself (a full disk, `INSERT OR ROLLBACK`,
        `RAISE(ROLLBACK)`) fails the future with the real `sqlite3`
        error, with nothing of the job kept. If the write lock cannot be
        had within the busy timeout, the future fails with SQLite's
        "database is locked" and `fn` never runs.

        With `read_only=True`, no transaction is opened, so `fn` never
        waits for the write lock -- for a job that only has to prove the
        writer thread answers. `fn` may then run `SELECT` statements
        only: any other statement raises `WriterMisuseError` before it
        runs, and the job fails with it. The same refusals of
        transaction control apply.

        If the returned future is cancelled before the writer thread
        reaches it, `fn` is never invoked -- no statement it would have
        run ever touches the connection. Cancelling after the writer
        thread has already started `fn` has no effect; the job always
        runs to completion in that case.

        Raises `RuntimeError` if the writer has already been `stop()`ped.
        If the writer thread has died, returns a future that has already
        failed with `WriterDeadError`; `fn` never runs.
        """
        with self._lifecycle_lock:
            if self._stopped.is_set():
                raise RuntimeError("Writer is stopped; cannot submit new work")
            future: concurrent.futures.Future[T] = concurrent.futures.Future()
            if self._death is not None:
                future.set_exception(self._dead_error(self._death))
                return future
            self._queue.put((fn, future, read_only))
        return future

    def stop(self, timeout: float) -> None:
        """Stop accepting new work, drain every job already queued, then
        wait up to `timeout` seconds for the thread to exit. Safe to
        call even if `start()` was never called.
        """
        if self._thread is None:
            return
        with self._lifecycle_lock:
            self._stopped.set()
            self._queue.put(None)
        self._thread.join(timeout)

    def _run(self) -> None:
        try:
            conn = connect(self._path, role="writer")
        except BaseException as exc:  # noqa: BLE001 -- must reach every queued future
            self._die(exc, in_flight=None)
            return

        in_flight: concurrent.futures.Future | None = None
        try:
            while True:
                job = self._queue.get()
                if job is None:
                    break
                fn, in_flight, read_only = job
                self._run_job(conn, fn, in_flight, read_only=read_only)
                in_flight = None
        except BaseException as exc:  # noqa: BLE001 -- same: never orphan a future
            self._die(exc, in_flight=in_flight)
        finally:
            conn.close()

    def _die(
        self, exc: BaseException, *, in_flight: concurrent.futures.Future | None
    ) -> None:
        """The thread is ending abnormally. Fail the job it was running,
        every job still queued, and (via `_death`) every later submit()."""
        logger.critical(
            "xaucopier writer thread for %s died (%s: %s); every pending and "
            "future write will fail with WriterDeadError",
            self._path,
            type(exc).__name__,
            exc,
            exc_info=exc,
        )
        orphans: list[concurrent.futures.Future] = []
        with self._lifecycle_lock:
            self._death = exc
            while True:
                try:
                    job = self._queue.get_nowait()
                except queue.Empty:
                    break
                if job is not None:  # a stop() sentinel has nothing to fail
                    orphans.append(job[1])

        if in_flight is not None:
            # Already RUNNING (set_running_or_notify_cancel said so), and
            # _run_job never resolved it because fn never returned.
            try:
                in_flight.set_exception(self._dead_error(exc))
            except concurrent.futures.InvalidStateError:
                pass
        for future in orphans:
            if future.set_running_or_notify_cancel():
                future.set_exception(self._dead_error(exc))

    @staticmethod
    def _dead_error(exc: BaseException) -> WriterDeadError:
        error = WriterDeadError(
            f"the writer thread died ({type(exc).__name__}: {exc}); this write "
            "did not run and no further write will"
        )
        error.__cause__ = exc
        return error

    @staticmethod
    def _run_job(
        conn: sqlite3.Connection,
        fn: Callable[[sqlite3.Connection], object],
        future: concurrent.futures.Future,
        *,
        read_only: bool = False,
    ) -> None:
        if not future.set_running_or_notify_cancel():
            # The caller cancelled this job before the writer thread
            # reached it. fn was never invoked, so no statement ever ran
            # against conn -- nothing to roll back, just move on.
            return

        try:
            if conn.in_transaction:
                # Only if an earlier job's commit and rollback both
                # failed: never start a job inside someone else's work.
                conn.rollback()
            if not read_only:
                # Before fn, so fn's reads are under the write lock too (F21).
                conn.execute("BEGIN IMMEDIATE")
        except Exception as exc:  # noqa: BLE001 -- e.g. "database is locked"
            Writer._rollback_quietly(conn)
            future.set_exception(exc)
            return

        job_conn = _JobConnection(conn, read_only=read_only)
        fn_exc: Exception | None = None
        result: object = None
        try:
            result = fn(job_conn)  # type: ignore[arg-type] -- a stand-in, see _JobConnection
        except Exception as exc:  # noqa: BLE001 -- must reach the future, never kill the thread
            fn_exc = exc

        if read_only:
            Writer._finish_read_only(conn, job_conn, future, fn_exc, result)
            return

        sqlite_rollback = job_conn.sqlite_rollback
        # Reachable only through the stand-in's private `_conn`: SQLite ended
        # the transaction on a statement the stand-in never saw (F31, G1L-4).
        if sqlite_rollback is None and isinstance(fn_exc, sqlite3.Error) and not conn.in_transaction:
            sqlite_rollback = fn_exc
        if sqlite_rollback is not None:
            # SQLite ended the transaction itself (F31): nothing of the
            # job before that point was kept, and anything fn wrote after
            # it (a new implicit transaction) is rolled back here, so the
            # job stays all-or-nothing. Report the real error.
            Writer._rollback_quietly(conn)
            future.set_exception(sqlite_rollback)
            return

        if job_conn.misuse is not None:
            # fn tried to manage the transaction; the stand-in refused
            # before anything ran, whatever fn then did with the error.
            Writer._rollback_quietly(conn)
            future.set_exception(job_conn.misuse)
            return

        if not conn.in_transaction:
            # The Writer's transaction is gone, SQLite did not end it, and
            # no guarded call did. The stand-in and its cursors guard every
            # public path, so fn went behind them: the stand-in's private
            # `_conn` attribute, the one way left to the real connection.
            misuse = WriterMisuseError(
                "fn ended the Writer's transaction through the real connection "
                "behind the stand-in (its private _conn attribute), which the "
                "Writer cannot guard; what fn wrote before that may already be "
                "committed. fn must only execute statements on the connection it "
                "is given and let the Writer commit or roll back."
            )
            if fn_exc is not None:
                misuse.__cause__ = fn_exc
            future.set_exception(misuse)
            return

        if fn_exc is not None:
            Writer._rollback_quietly(conn)
            future.set_exception(fn_exc)
            return

        try:
            conn.commit()
        except Exception as exc:  # noqa: BLE001 -- same contract as above
            Writer._rollback_quietly(conn)
            future.set_exception(exc)
            return

        future.set_result(result)

    @staticmethod
    def _finish_read_only(
        conn: sqlite3.Connection,
        job_conn: _JobConnection,
        future: concurrent.futures.Future,
        fn_exc: Exception | None,
        result: object,
    ) -> None:
        """A read-only job has returned or raised. It opened no
        transaction, so there is nothing to commit -- unless fn wrote
        behind the stand-in's back, which opened one: that is rolled back
        at once (it would hold the write lock) and the job is misuse."""
        if conn.in_transaction:
            Writer._rollback_quietly(conn)
            misuse = WriterMisuseError(
                "fn wrote in a read-only job, by a path the Writer does not guard; "
                "what it wrote was rolled back. A read-only job may only run SELECT."
            )
            if fn_exc is not None:
                misuse.__cause__ = fn_exc
            future.set_exception(misuse)
            return
        if job_conn.misuse is not None:
            future.set_exception(job_conn.misuse)
            return
        if fn_exc is not None:
            future.set_exception(fn_exc)
            return
        future.set_result(result)

    @staticmethod
    def _rollback_quietly(conn: sqlite3.Connection) -> None:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass


#: Statements that begin, end or nest a transaction -- the Writer's job.
_TRANSACTION_CONTROL = frozenset({"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"})


def _leading_keyword(sql: str) -> str:
    """The first word of `sql`, upper-cased, after any whitespace, `;`,
    `-- line` and `/* block */` comments -- enough to see a
    transaction-control statement however it is dressed."""
    i, n = 0, len(sql)
    while i < n:
        if sql[i].isspace() or sql[i] == ";":
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end < 0 else end + 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            break
    j = i
    while j < n and (sql[j].isalnum() or sql[j] == "_"):
        j += 1
    return sql[i:j].upper()


class _JobConnection:
    """What a Writer job's `fn` gets instead of the connection: every
    read and every ordinary statement passes straight through, and every
    way of ending or reshaping the Writer's transaction raises
    `WriterMisuseError` *before it runs* (see that class). In a read-only
    job, so does every statement that is not a `SELECT`. The cursors
    `execute()`/`executemany()` return are guarded the same way
    (`_JobCursor`).

    It also notes, for `Writer._run_job`, the first misuse (so a job that
    catches the error still fails) and the first `sqlite3` error after
    which SQLite had ended the transaction itself (so a full disk is
    reported as a full disk, even if `fn` caught it)."""

    __slots__ = ("_conn", "misuse", "read_only", "sqlite_rollback")

    def __init__(self, conn: sqlite3.Connection, *, read_only: bool = False) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "misuse", None)
        object.__setattr__(self, "read_only", read_only)
        object.__setattr__(self, "sqlite_rollback", None)

    def _refuse(self, what: str) -> NoReturn:
        if self.read_only:
            tail = (
                "a read-only job may only run SELECT statements and must leave the "
                "transaction to the Writer. Nothing was written."
            )
        else:
            tail = (
                "the Writer owns the transaction for the whole submitted job -- fn "
                "must only execute statements and let the Writer commit or roll "
                "back. Nothing this job wrote was kept."
            )
        error = WriterMisuseError(f"fn {what}; {tail}")
        if self.misuse is None:
            object.__setattr__(self, "misuse", error)
        raise error

    def _check(self, sql: object, method: str) -> None:
        if isinstance(sql, str):
            keyword = _leading_keyword(sql)
            if self.read_only and keyword != "SELECT":
                self._refuse(f"ran {keyword or 'a statement'} through {method}() in a read-only job")
            if keyword in _TRANSACTION_CONTROL:
                self._refuse(f"ran {keyword} through {method}()")

    def _run(self, method: Callable[..., T], *args: object, **kwargs: object) -> T:
        was_in_transaction = self._conn.in_transaction
        try:
            return method(*args, **kwargs)
        except sqlite3.Error as exc:
            if (
                was_in_transaction
                and not self._conn.in_transaction
                and self.sqlite_rollback is None
            ):
                object.__setattr__(self, "sqlite_rollback", exc)
            raise

    def execute(self, sql: str, *args: object, **kwargs: object) -> _JobCursor:
        self._check(sql, "execute")
        return _JobCursor(self._run(self._conn.execute, sql, *args, **kwargs), self)

    def executemany(self, sql: str, *args: object, **kwargs: object) -> _JobCursor:
        self._check(sql, "executemany")
        return _JobCursor(self._run(self._conn.executemany, sql, *args, **kwargs), self)

    def executescript(self, *args: object, **kwargs: object) -> NoReturn:
        self._refuse("called executescript(), which commits the open transaction first")

    def commit(self) -> NoReturn:
        self._refuse("called commit()")

    def rollback(self) -> NoReturn:
        self._refuse("called rollback()")

    def close(self) -> NoReturn:
        self._refuse("called close() on the Writer's connection")

    def cursor(self, *args: object, **kwargs: object) -> NoReturn:
        self._refuse("asked for cursor() (use execute(), which returns one)")

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        if name in ("isolation_level", "autocommit"):
            self._refuse(f"set {name}, which can commit or end the open transaction")
        self._refuse(f"set {name} on the Writer's connection, which every job shares")


class _JobCursor:
    """The cursor a Writer job gets back from `execute()`/`executemany()`:
    fetching, iterating, `lastrowid`, `rowcount`, `description` and the
    rest pass straight through. Its own `execute()`/`executemany()` are
    checked exactly like the job connection's (they would otherwise run a
    `COMMIT` unseen: final fix wave, group-1 review), its
    `executescript()` is refused (it commits first), and it does not hand
    out the real connection (`.connection`)."""

    __slots__ = ("_cursor", "_job")

    def __init__(self, cursor: sqlite3.Cursor, job: _JobConnection) -> None:
        object.__setattr__(self, "_cursor", cursor)
        object.__setattr__(self, "_job", job)

    def execute(self, sql: str, *args: object, **kwargs: object) -> _JobCursor:
        self._job._check(sql, "a cursor's execute")
        self._job._run(self._cursor.execute, sql, *args, **kwargs)
        return self

    def executemany(self, sql: str, *args: object, **kwargs: object) -> _JobCursor:
        self._job._check(sql, "a cursor's executemany")
        self._job._run(self._cursor.executemany, sql, *args, **kwargs)
        return self

    def executescript(self, *args: object, **kwargs: object) -> NoReturn:
        self._job._refuse(
            "called a cursor's executescript(), which commits the open transaction first"
        )

    @property
    def connection(self) -> NoReturn:
        self._job._refuse("asked a cursor for the Writer's connection")

    def __iter__(self) -> _JobCursor:
        return self

    def __next__(self) -> object:
        return next(self._cursor)

    def __getattr__(self, name: str) -> object:
        return getattr(self._cursor, name)

    def __setattr__(self, name: str, value: object) -> None:
        # arraysize, row_factory: they only change this one cursor.
        setattr(self._cursor, name, value)
