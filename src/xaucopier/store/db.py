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
  commits it, any exception rolls it back. This is what makes the
  brief's rollback constraint watertight -- there is no path where a
  *successful* ``fn`` that forgot to call ``commit()`` itself could leave
  the connection sitting in an open transaction for the next caller
  either. ``submit()`` is therefore "one job, one all-or-nothing write",
  which is the unit the listener actually wants.
- ``stop()`` pushes a sentinel onto the queue rather than signalling the
  thread out-of-band. Because ``queue.Queue`` is FIFO, the sentinel is
  only ever processed after every job submitted before ``stop()`` was
  called, so drains happen for free from the queue's own ordering
  guarantee -- there is no separate "flush" step to get wrong.
"""
from __future__ import annotations

import concurrent.futures
import queue
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypeVar

from xaucopier.store.migrations import migrate

Role = Literal["writer", "reader"]

_BUSY_TIMEOUT_MS = 10_000

T = TypeVar("T")


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
    immediately usable regardless of which role opens it first.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    conn.execute(f"PRAGMA synchronous = {'FULL' if role == 'writer' else 'NORMAL'}")
    migrate(conn)
    return conn


class Writer:
    """Owns the only write connection to the store.

    `submit(fn)` hands `fn` to a single dedicated thread, which runs it
    against the writer's own `sqlite3.Connection` and resolves the
    returned future with `fn`'s result -- or, if `fn` raises, with that
    exception. The thread itself never dies from a bad `fn`: the
    exception is caught, the transaction is rolled back, and the thread
    loops back to `queue.get()` for the next job.

    Not thread-safe to call `start()` / `stop()` concurrently from
    multiple threads; `submit()` is safe to call from anywhere
    (`queue.Queue` is the synchronisation point), which is the only
    concurrency this class actually needs to support -- the listener's
    asyncio coroutines all run on one event-loop thread, but a coroutine
    that has handed a submit to `run_in_executor` or similar could in
    principle call from elsewhere too.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()

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
        """
        if self._stopped.is_set():
            raise RuntimeError("Writer is stopped; cannot submit new work")
        future: concurrent.futures.Future[T] = concurrent.futures.Future()
        self._queue.put((fn, future))
        return future

    def stop(self, timeout: float) -> None:
        """Stop accepting new work, drain every job already queued, then
        wait up to `timeout` seconds for the thread to exit. Safe to
        call even if `start()` was never called.
        """
        if self._thread is None:
            return
        self._stopped.set()
        self._queue.put(None)
        self._thread.join(timeout)

    def _run(self) -> None:
        conn = connect(self._path, role="writer")
        try:
            while True:
                job = self._queue.get()
                if job is None:
                    break
                fn, future = job
                self._run_job(conn, fn, future)
        finally:
            conn.close()

    @staticmethod
    def _run_job(
        conn: sqlite3.Connection,
        fn: Callable[[sqlite3.Connection], object],
        future: concurrent.futures.Future,
    ) -> None:
        try:
            result = fn(conn)
        except Exception as exc:  # noqa: BLE001 -- must reach the future, never kill the thread
            Writer._rollback_quietly(conn)
            if not future.cancelled():
                future.set_exception(exc)
            return

        try:
            conn.commit()
        except Exception as exc:  # noqa: BLE001 -- same contract as above
            Writer._rollback_quietly(conn)
            if not future.cancelled():
                future.set_exception(exc)
            return

        if not future.cancelled():
            future.set_result(result)

    @staticmethod
    def _rollback_quietly(conn: sqlite3.Connection) -> None:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
