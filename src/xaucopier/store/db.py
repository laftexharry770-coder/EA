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
- Each submitted ``fn`` runs inside its own transaction: a normal return
  commits it, any exception rolls it back. ``fn`` must never call
  ``commit()``/``rollback()`` itself -- see ``Writer.submit``'s
  docstring and ``WriterMisuseError``. ``submit()`` is therefore "one
  job, one all-or-nothing write", which is the unit the listener
  actually wants.
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
from typing import Literal, TypeVar

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
    """Raised (via the future, not as a live exception) when a submitted
    `fn` calls `commit()` or `rollback()` on the connection itself,
    instead of letting the `Writer` own the transaction boundary for the
    whole job.

    Detected by comparing `conn.total_changes` and `conn.in_transaction`
    from just before `fn` runs to just after: if `fn` made at least one
    write (`total_changes` moved) but left no open transaction behind,
    the only way that can happen is `fn` called `commit()` or
    `rollback()` on its own -- the Writer never calls either until after
    this check. Whatever `fn` already did to the database at that point
    is final; there is nothing left to roll back, and the caller must
    not be left trusting `fn`'s own result or exception as the whole
    story.
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
        self, fn: Callable[[sqlite3.Connection], T]
    ) -> concurrent.futures.Future[T]:
        """Queue `fn` to run on the writer thread. Returns a
        `concurrent.futures.Future` that resolves with `fn`'s return
        value (or its exception). The listener awaits this with
        `await asyncio.wrap_future(writer.submit(fn))`.

        `fn` MUST NOT call `commit()` or `rollback()` on the connection
        it is given -- the Writer owns the transaction boundary for the
        whole job: a normal return commits, an exception rolls back. A
        self-managed `fn` is detected and resolves the future with
        `WriterMisuseError` instead of `fn`'s own result/exception (see
        that class's docstring for why).

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
            self._queue.put((fn, future))
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
                fn, in_flight = job
                self._run_job(conn, fn, in_flight)
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
    ) -> None:
        if not future.set_running_or_notify_cancel():
            # The caller cancelled this job before the writer thread
            # reached it. fn was never invoked, so no statement ever ran
            # against conn -- nothing to roll back, just move on.
            return

        before_changes = conn.total_changes
        fn_exc: Exception | None = None
        result: object = None
        try:
            result = fn(conn)
        except Exception as exc:  # noqa: BLE001 -- must reach the future, never kill the thread
            fn_exc = exc

        if conn.total_changes != before_changes and not conn.in_transaction:
            # fn wrote something (total_changes moved) yet left no open
            # transaction behind, even though the Writer has not called
            # commit()/rollback() yet at this point. The only way that
            # combination happens is fn called one of them itself.
            misuse = WriterMisuseError(
                "fn called commit()/rollback() itself; the Writer owns "
                "the transaction for the whole submitted job -- fn must "
                "only execute statements and let the Writer commit or "
                "roll back."
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
    def _rollback_quietly(conn: sqlite3.Connection) -> None:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
