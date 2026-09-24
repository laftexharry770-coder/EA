"""The Telegram listener: the process that actually runs.

It connects, primes Telethon's update engine, backfills whatever was
posted while it was down, registers handlers, starts the periodic poll,
and then waits. Everything it learns it hands to the writer thread; it
never opens a write connection of its own, never sleeps on a lock, and
never makes an RPC on the update path.

**The startup order is fixed, and every step has a reason.**

0. *Start the writer and wait for it to have connected.* `db.connect()`
   runs `migrate()` on whichever thread calls it, so whoever opens a
   brand-new database file first is the thread that runs the schema DDL.
   If that were a reader opened from the event-loop thread, the loop
   would stall for the whole migration. The listener therefore starts
   the `Writer` and `await`s a no-op probe job before opening any reader:
   the probe can only resolve after `Writer._run` has already called
   `connect()` on the writer thread, which makes "writer first" a fact
   about causality rather than about the order two lines happen to be
   written in. The wait is bounded (`writer_ready_timeout`, 30 s): a
   writer that died (`WriterDeadError`) or never connected makes
   `start()` raise `ListenerStartupError` -- a `ListenerStopped`, so a
   non-zero exit -- instead of hanging with the process looking alive.
1. **connect.**
2. **`await client.get_dialogs()`.** This primes the per-channel `pts`
   state Telethon's update engine needs. It is not cosmetic -- skipping
   it degrades update delivery -- and it must happen before handlers
   exist, so the first update the engine dispatches is already against
   primed state.
3. **Backfill** each enabled channel from
   `channel_state.last_processed_message_id`, so a restart does not lose
   whatever was posted while the process was down. A pointer of 0 means
   this channel has never been read: rather than ingest the channel's
   *oldest* messages (which is what `get_messages(min_id=0,
   reverse=True)` returns -- possibly years-old signals, each queuing
   ENTRY outbox rows), the first run seeds the pointer at the newest
   message dated before the listener started (less a 60 s clock-skew
   margin) and ingests only what came after it -- see
   `Listener._seed_pointer` for why the cutoff is the *start* time and
   not the moment the seed happens to succeed. Every history RPC here
   is bounded (`rpc_timeout`), so a hung read cannot hold `start()`
   before handlers exist. Backfill runs
   *before* handlers are registered, so a message can never be ingested
   by backfill and the update stream at the same instant (and even if it
   were, `ingest()`'s dedup would make the second one a no-op).
4. **Register handlers.**
5. **Start the poll task** (`ingest/poll.py` is the poll itself;
   `poll_hook` is its slot). It starts last because a poll racing
   backfill over the same rows is pointless work.
6. **`run_until_disconnected()`, wrapped so any return is non-zero.** A
   listener that returns cleanly has still stopped listening: for the
   supervisor there is no such thing as a successful exit here, so the
   return is turned into `ListenerStopped` / `EXIT_LISTENER_STOPPED`.
   The poll task is watched alongside it: the poll is the safety net
   for everything the update stream drops, so a poll that raises (or
   returns) is a stopped listener too, not a background failure nobody
   sees. So is the writer: every write goes through `_run_on_writer()`,
   and the first `WriterDeadError` any path meets -- handler, backfill,
   poll -- stops the listener. Without a writer nothing can be stored,
   and a process that keeps running anyway looks healthy to its
   supervisor while recording nothing.

**Edits and deletions (Task 10).** `MessageEdited` and `MessageDeleted`
get handlers of their own, under the same four-step contract as
`NewMessage` below. They are hints, like `NewMessage`: the source of truth
is the poll's window re-read (`ingest/edits.py`), which fetches the recent
stored messages by id and finds the edits and deletions the stream
dropped. What an edit or deletion *does* is decided in one writer job by
`store/edits_repo.py`, so one seen by both paths is processed once.

**Each handler does exactly four things: route, normalise, submit,
return.** Routing comes first -- one dict lookup on `event.chat_id` --
so a message from a chat nobody watches is dropped before any other
work. No `get_entity`, no `get_reply_message`, no RPC of any kind --
each of those is a network round trip on the update path, and a slow
handler stalls the event loop, which is a documented cause of dropped
channel updates, which is a missed trade. Everything the handler needs
is already on the event object (`chat_id`, the message, its date, its
`reply_to_msg_id`); the channel lookup is a dict hit; the write is
handed to the writer thread and `await`ed via `asyncio.wrap_future`,
which suspends only that update's own task, never the loop.

For the same reason the handler is registered **without** Telethon's
`chats=` filter: resolving `chats=` makes Telethon call
`get_input_entity()` on the dispatch path, which can be an RPC. Filtering
on `event.chat_id` against a pre-built dict costs nothing and cannot
round-trip.

**Only a history read moves the per-channel pointer.**
`channel_state.last_processed_message_id` is where a history read
(backfill, Task 9's poll -- both are `read_history()`) resumes. A
history read walks ids in ascending order and so has seen everything
below the id it advances to; the update stream can silently drop ids,
so the handler ingests and never touches the pointer. The poll's re-read
of a message the handler already stored is a `"duplicate"` no-op. See
`_advance_last_processed` for the failure this prevents. For the same
reason a history read **stops at the first message that fails**: the
pointer rests at the last contiguous success and the next read retries.
Only a message that fails `MAX_HISTORY_ATTEMPTS` (3) reads in a row is
skipped -- pointer moved past that one id, CRITICAL log, and the durable
`history_skipped` metric -- so a poison message cannot wedge its channel.

**No `sqlite3` anywhere in this package.** Writes go through
`Writer.submit()`; the one read this module does (the per-channel
backfill pointer) goes through a `store.db.connect(role="reader")`
connection opened after the writer barrier above.
tests/test_listener.py asserts this with an AST walk over the whole
`ingest/` package.
"""
from __future__ import annotations

import asyncio
import collections
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

from telethon import events

from xaucopier.config.accounts import AccountCfg
from xaucopier.config.channels import ChannelCfg
from xaucopier.store import alerts, db, edits_repo, heartbeats, messages_repo, metrics
from xaucopier.store.edits_repo import DeletionResult, EditResult
from xaucopier.store.messages_repo import IngestResult

logger = logging.getLogger(__name__)

#: Exit code for "the client stopped listening". Distinct from
#: `client.EXIT_TERMINAL_AUTH` (3), which tells the supervisor *not* to
#: restart; this one is retryable, so the supervisor should restart.
EXIT_LISTENER_STOPPED = 4

DEFAULT_BACKFILL_LIMIT = 500
#: Hard bound on every history `get_messages()` RPC `read_history()`
#: makes -- backfill's, and the poll's by default.
DEFAULT_RPC_TIMEOUT = 20.0
#: How far before the listener's start a first-run seed still ingests
#: from. Absorbs skew between this machine's clock (the start time) and
#: Telegram's (message dates); see `Listener._seed_pointer`.
FIRST_RUN_SEED_MARGIN = timedelta(seconds=60)
DEFAULT_WRITER_STOP_TIMEOUT = 15.0
#: How long `start()` waits for the writer thread to have connected.
#: Connecting and migrating a local SQLite file takes milliseconds; 30 s
#: only ever elapses when something is wrong.
DEFAULT_WRITER_READY_TIMEOUT = 30.0

#: How many `HandlerError`s are kept for inspection. Older ones are
#: dropped; `handler_error_count` keeps the true total. Bounded so a
#: sustained failure cannot grow memory for the life of the process.
MAX_RECORDED_HANDLER_ERRORS = 256

#: How many history reads may fail on the same message before it is
#: skipped (see `Listener.ingest_history`). Only a failure that says
#: something about the message counts: a dead writer or another program
#: holding the database's write lock never does. Bounded for a message
#: that can never be stored. Counted in memory, so a restart allows at
#: most three more.
MAX_HISTORY_ATTEMPTS = 3

#: `metrics.name` bumped once per history message skipped after
#: `MAX_HISTORY_ATTEMPTS` failures. Every one is a message -- possibly a
#: trade -- that was never stored, so `status` must surface it.
HISTORY_SKIPPED_METRIC = "history_skipped"

#: `sqlite3.Connection`, unannotated on purpose: this package must not
#: import sqlite3 at all (see the module docstring), not even for a type.
Connection = Any

#: The poll slot: `async def poll(listener)`, which must never return
#: (see `wait_until_stopped`). `ingest.poll.HistoryPoll` fills it.
PollHook = Callable[["Listener"], Awaitable[None]]

#: `messages.source` for a message delivered by the update stream.
LIVE_SOURCE = "live"
#: `messages.source` values for a message found by reading channel
#: history. These -- and only these -- advance the per-channel pointer;
#: see `_advance_last_processed` for why.
HISTORY_SOURCES = frozenset({"backfill", "poll"})
ALL_SOURCES = HISTORY_SOURCES | {LIVE_SOURCE}


class ListenerStopped(RuntimeError):
    """The listener is not receiving Telegram updates: it stopped, or it
    never got going. Whether that happened cleanly or not, for a
    supervisor it is never a success -- `run_listener()` turns it into
    `EXIT_LISTENER_STOPPED` ("restart me")."""


class ListenerStartupError(ListenerStopped):
    """`start()` could not bring the listener up -- e.g. the writer
    thread died connecting to the database, or never connected within
    `writer_ready_timeout`. A subclass of `ListenerStopped` so it exits
    non-zero through the same door."""


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """One Telegram message, flattened to exactly what `ingest()` needs.

    Built from data already present on the event/message object -- the
    normalisation step never reaches back to Telegram for anything.
    """

    channel_id: int
    message_id: int
    text: str
    posted_at_utc: datetime
    edit_date_utc: datetime | None
    reply_to_message_id: int | None


@dataclass(frozen=True, slots=True)
class IncomingDeletion:
    """One `MessageDeleted` event, flattened: the channel and the ids."""

    channel_id: int
    message_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class HandlerError:
    """A per-message failure on any ingest path, kept so a test (and an
    operator) can see that one happened rather than inferring it from a
    missing row. `source` is the path it happened on: `"live"` (an
    update handler -- new message, edit or deletion), a history read
    (`"backfill"`, `"poll"`), or `"window"` (the poll's by-id re-read of
    the recent window, `ingest/edits.py`)."""

    channel_id: int | None
    message_id: int | None
    error: str
    source: str


@dataclass(frozen=True, slots=True)
class HistoryIngest:
    """What happened to one message a history read found.

    `result` is the `ingest()` outcome -- `"new"` means the update stream
    had not delivered it (yet), which is what Task 9's
    `missed_by_eventstream` counter counts.
    """

    message_id: int
    #: None when this message failed (the failure is in `handler_errors`):
    #: either the batch stopped here and the next read retries it, or it
    #: was skipped after `MAX_HISTORY_ATTEMPTS` failures.
    result: IngestResult | None


def _to_utc(value: datetime, *, field: str) -> datetime:
    """Convert an aware datetime to UTC; refuse a naive one.

    Telethon 1.44 always hands out aware UTC datetimes, so a naive one
    can only come from test or fake code. Labelling it UTC would treat
    local wall-clock time as UTC -- shifting a signal's age by the local
    offset, hours, and quietly defeating the staleness gate -- so it is
    an error instead."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"message {field} {value!r} is naive; refusing to guess its timezone "
            "(Telethon always returns timezone-aware UTC)"
        )
    return value.astimezone(UTC)


def normalise_message(message: Any, *, channel_id: int) -> IncomingMessage:
    """Flatten a Telethon `Message` (or the fake's stand-in). Pure
    attribute reads -- no RPC, no awaits, no clock."""
    text = getattr(message, "message", None)
    if text is None:
        text = getattr(message, "raw_text", None)

    reply_to = getattr(message, "reply_to_msg_id", None)
    if reply_to is None:
        reply_to = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)

    edit_date = getattr(message, "edit_date", None)

    return IncomingMessage(
        channel_id=channel_id,
        message_id=message.id,
        text=text or "",
        posted_at_utc=_to_utc(message.date, field="date"),
        edit_date_utc=_to_utc(edit_date, field="edit_date") if edit_date is not None else None,
        reply_to_message_id=reply_to,
    )


def normalise_event(event: Any) -> IncomingMessage:
    """Flatten a `NewMessage` event -- or a `MessageEdited` one, which in
    Telethon is a `NewMessage.Event` carrying the edited message.
    `event.chat_id` is the marked channel id (`-100...`) that
    `channels.toml` already uses, so no `get_entity()` round trip is
    needed to identify the channel."""
    return normalise_message(event.message, channel_id=event.chat_id)


def normalise_deletion_event(event: Any) -> IncomingDeletion:
    """Flatten a `MessageDeleted` event: `chat_id` (the marked channel id
    -- Telethon builds a channel's event with `PeerChannel`) and every id
    in `deleted_ids`. Pure attribute reads."""
    return IncomingDeletion(
        channel_id=event.chat_id,
        message_ids=tuple(int(message_id) for message_id in (event.deleted_ids or ())),
    )


def _advance_last_processed(
    conn: Connection, *, channel_id: int, message_id: int
) -> None:
    """Move `channel_state.last_processed_message_id` forward, never
    backwards.

    **Only a history read may call this** -- startup backfill and
    (Task 9) the poll, i.e. the sources in `HISTORY_SOURCES`, plus the
    first-run seed, which reads the channel's newest message. A history
    read walks `get_messages(min_id=pointer, reverse=True)` in ascending
    id order, so when it advances the pointer to N it has already seen
    every id between the old pointer and N. The live update stream has
    no such property: it can silently skip ids (the Telethon channel
    blackout this whole design exists to survive), so a live message
    moving the pointer to N would carry it past every message the
    stream dropped -- and the poll, reading from the pointer, would
    never see them. That is the reviewer's D1: blackout hides 10 and
    11, 12 arrives live, and 10 and 11 are lost forever.

    `MAX()` is only a guard against a history read being handed its
    batch out of order; it is not what makes the pointer safe. What
    makes it safe is that the live path never writes it at all. The
    poll's re-read of a message the live path already stored is a
    `"duplicate"` no-op via `UNIQUE(channel_id, message_id)`.
    """
    conn.execute(
        "INSERT INTO channel_state (channel_id, last_processed_message_id) "
        "VALUES (?, ?) "
        "ON CONFLICT(channel_id) DO UPDATE SET last_processed_message_id = "
        "MAX(last_processed_message_id, excluded.last_processed_message_id)",
        (channel_id, message_id),
    )


class Listener:
    """Owns the Telegram client, the writer thread and the reader
    connection for one xaucopier process.

    The `Writer` handed in must **not** be started: `start()` starts it
    and, crucially, waits for it to have connected before any reader is
    opened (see the module docstring). `aclose()` owns the whole
    teardown, including stopping the writer.

    Tasks 9, 10 and 11 build on the public surface here --
    `client`, `channels`, `enabled_channels`, `reader`,
    `channel_cfg_for()`, `read_last_processed_message_id()`,
    `read_history()`, `ingest_history()`, `ingest_message()`,
    `ingest_edit()`, `ingest_deletion()`, `history_lock()`,
    `record_handler_error()`, `record_alert()`, `probe_writer()`,
    `record_metric()`, `record_heartbeats()` and `poll_hook` -- rather
    than reaching into the internals. The poll
    (`ingest/poll.py`) is `read_history(channel, source="poll",
    limit=..., rpc_timeout=...)`: it is the one place the pointer is
    read, fetched from and advanced. Its window re-read
    (`ingest/edits.py`) reads the pointer too, and never writes it.
    """

    def __init__(
        self,
        *,
        client: Any,
        channels: tuple[ChannelCfg, ...],
        accounts: tuple[AccountCfg, ...],
        writer: db.Writer,
        db_path: Path,
        now: Callable[[], datetime] | None = None,
        poll_hook: PollHook | None = None,
        backfill_limit: int = DEFAULT_BACKFILL_LIMIT,
        backfill_rpc_timeout: float = DEFAULT_RPC_TIMEOUT,
        writer_stop_timeout: float = DEFAULT_WRITER_STOP_TIMEOUT,
        writer_ready_timeout: float = DEFAULT_WRITER_READY_TIMEOUT,
    ) -> None:
        self.client = client
        self.channels = tuple(channels)
        self.accounts = tuple(accounts)
        self.writer = writer
        self.db_path = db_path
        self.poll_hook = poll_hook
        self.poll_task: asyncio.Task[None] | None = None

        self._now = now if now is not None else _utcnow
        self._backfill_limit = backfill_limit
        self._backfill_rpc_timeout = backfill_rpc_timeout
        # When start() began, from the injected clock. The first-run seed
        # keeps everything posted after it (less FIRST_RUN_SEED_MARGIN).
        self._started_at: datetime | None = None
        self._writer_stop_timeout = writer_stop_timeout
        self._writer_ready_timeout = writer_ready_timeout
        self._channels_by_id: dict[int, ChannelCfg] = {
            channel.channel_id: channel for channel in self.channels if channel.enabled
        }
        self._reader: Connection = None
        self._handler_errors: collections.deque[HandlerError] = collections.deque(
            maxlen=MAX_RECORDED_HANDLER_ERRORS
        )
        self._handler_error_count = 0
        # Channels whose pointer this process has established -- read as
        # non-zero, or seeded (even to 0, for an empty channel). Only a
        # channel *not* in here may be seeded: re-seeding an empty
        # channel on the next read would jump past its first post.
        self._pointer_seeded: set[int] = set()
        # Failed history-read attempts per (channel_id, message_id); see
        # ingest_history(). Cleared on success or skip.
        self._history_attempts: dict[tuple[int, int], int] = {}
        # One lock per channel, held for a whole read_history() call: at
        # most one history read per channel at a time (see read_history).
        self._history_locks: dict[int, asyncio.Lock] = {}
        # Resolved (with the WriterDeadError) the first time any write
        # finds the writer thread dead; watched by wait_until_stopped().
        # Created in start(), where a running loop is guaranteed.
        self._writer_death: asyncio.Future[db.WriterDeadError] | None = None
        self._started = False
        self._closed = False

    # -- public surface ---------------------------------------------------

    @property
    def enabled_channels(self) -> tuple[ChannelCfg, ...]:
        return tuple(self._channels_by_id.values())

    @property
    def reader(self) -> Connection:
        """The read-only connection, opened by `start()` *after* the
        writer has connected. Reading before `start()` would be the
        migrate-on-the-loop-thread hazard the ordering exists to
        prevent, so it raises instead."""
        if self._reader is None:
            raise RuntimeError(
                "reader connection is not open; Listener.start() opens it "
                "only after the Writer has connected (see module docstring)"
            )
        return self._reader

    @property
    def handler_errors(self) -> tuple[HandlerError, ...]:
        """The most recent `MAX_RECORDED_HANDLER_ERRORS` failures, oldest
        first."""
        return tuple(self._handler_errors)

    @property
    def handler_error_count(self) -> int:
        """Every failure since start, including those no longer kept in
        `handler_errors`."""
        return self._handler_error_count

    def channel_cfg_for(self, channel_id: int) -> ChannelCfg | None:
        """The enabled channel config for `channel_id`, or None if this
        channel is not watched. A dict lookup -- never an RPC."""
        return self._channels_by_id.get(channel_id)

    def read_last_processed_message_id(self, channel_id: int) -> int:
        """The backfill/poll pointer for `channel_id`, 0 if unseen.

        A single indexed lookup on the reader connection. Cheap enough
        for startup and for Task 9's poll interval; it is deliberately
        *not* called from the update handler.
        """
        row = self.reader.execute(
            "SELECT last_processed_message_id FROM channel_state WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    async def ingest_message(
        self,
        incoming: IncomingMessage,
        *,
        channel_cfg: ChannelCfg,
        source: str,
    ) -> IngestResult:
        """Hand one message to the writer thread and await the result.

        `source` is `"live"` for the update stream, or one of
        `HISTORY_SOURCES` (`"backfill"`, `"poll"`) for a history read.
        It decides whether the per-channel pointer moves: a history
        source advances `last_processed_message_id` to this message; a
        live one never touches it (see `_advance_last_processed` -- a
        live advance is what let one live message hide a blackout's
        worth of missed ones). Any other value raises `ValueError`
        rather than silently landing on one side of that rule.

        The whole job -- route, dedup, insert, outbox fan-out, and (for
        a history source) the pointer advance -- runs inside one
        transaction on the writer thread, so a crash mid-way leaves
        neither a signal with no outbox row nor a pointer that has moved
        past a message that was never stored.

        `await`ing here does not block the event loop: it suspends this
        one update's task while the writer thread does the blocking work.
        """
        if source not in ALL_SOURCES:
            raise ValueError(
                f"unknown ingest source {source!r}; expected one of {sorted(ALL_SOURCES)}"
            )
        advances_pointer = source in HISTORY_SOURCES
        now_utc = self._now()
        accounts = self.accounts

        def job(conn: Connection) -> IngestResult:
            result = messages_repo.ingest(
                conn,
                channel_cfg=channel_cfg,
                message_id=incoming.message_id,
                text=incoming.text,
                posted_at_utc=incoming.posted_at_utc,
                edit_date_utc=incoming.edit_date_utc,
                reply_to_message_id=incoming.reply_to_message_id,
                source=source,
                now_utc=now_utc,
                accounts=accounts,
            )
            if advances_pointer:
                _advance_last_processed(
                    conn,
                    channel_id=incoming.channel_id,
                    message_id=incoming.message_id,
                )
            return result

        return await self._run_on_writer(job)

    async def ingest_edit(
        self,
        incoming: IncomingMessage,
        *,
        channel_cfg: ChannelCfg,
        detected_by: str,
    ) -> EditResult:
        """Hand the current version of a message -- from a `MessageEdited`
        event (`detected_by="event"`) or the window re-read (`"window"`)
        -- to `edits_repo.apply_edit()` on the writer thread, and await
        the result. One job, one transaction, decided against the stored
        row, so an edit seen by both paths is applied once. A message
        never stored is left alone: this path never creates one. Never
        touches the pointer."""
        now_utc = self._now()
        accounts = self.accounts

        def job(conn: Connection) -> EditResult:
            return edits_repo.apply_edit(
                conn,
                channel_cfg=channel_cfg,
                message_id=incoming.message_id,
                text=incoming.text,
                posted_at_utc=incoming.posted_at_utc,
                edit_date_utc=incoming.edit_date_utc,
                reply_to_message_id=incoming.reply_to_message_id,
                now_utc=now_utc,
                accounts=accounts,
                detected_by=detected_by,
            )

        return await self._run_on_writer(job)

    async def ingest_deletion(
        self,
        channel_cfg: ChannelCfg,
        message_id: int,
        *,
        detected_by: str,
    ) -> DeletionResult:
        """Record that `message_id` is gone from its channel --
        `edits_repo.apply_deletion()`, on the writer thread. Idempotent:
        the second report of the same deletion finds it marked. Never
        closes anything, never touches the pointer."""
        now_utc = self._now()
        channel_id = channel_cfg.channel_id

        def job(conn: Connection) -> DeletionResult:
            return edits_repo.apply_deletion(
                conn,
                channel_id=channel_id,
                message_id=message_id,
                now_utc=now_utc,
                detected_by=detected_by,
            )

        return await self._run_on_writer(job)

    def history_lock(self, channel_id: int) -> asyncio.Lock:
        """The per-channel lock every history read holds for its whole
        length (see `read_history`). The window re-read takes it too, so
        it never interleaves with a read of the same channel."""
        return self._history_locks.setdefault(channel_id, asyncio.Lock())

    def record_handler_error(
        self,
        channel_id: int | None,
        message_id: int | None,
        exc: Exception,
        *,
        source: str,
    ) -> None:
        """Record (and log) a per-message failure in `handler_errors`, for
        paths outside this module -- the window re-read."""
        self._record_handler_error(channel_id, message_id, exc, source=source)

    async def _run_on_writer(
        self, job: Callable[[Connection], Any], *, read_only: bool = False
    ) -> Any:
        """Run `job` on the writer thread and await it -- the one door
        every listener write goes through (the startup probe aside, which
        has its own handling). A `WriterDeadError` is re-raised to the
        caller *and* reported to `wait_until_stopped()`, so a writer that
        dies after startup stops the listener instead of leaving it
        running with nothing able to record. `read_only` is passed to
        `Writer.submit`: such a job never waits for the write lock."""
        try:
            return await asyncio.wrap_future(self.writer.submit(job, read_only=read_only))
        except db.WriterDeadError as exc:
            self._writer_died(exc)
            raise

    def _writer_died(self, exc: db.WriterDeadError) -> None:
        if self._writer_death is None or self._writer_death.done():
            return
        logger.critical(
            "the writer thread is dead (%s); nothing can be stored, so the "
            "listener is stopping for its supervisor to restart it",
            exc,
        )
        # A result, not an exception: nothing is lost if no one ever
        # awaits it, and wait_until_stopped() chains it as the cause.
        self._writer_death.set_result(exc)

    async def ingest_history(
        self,
        channel_cfg: ChannelCfg,
        messages: Iterable[Any],
        *,
        min_id: int,
        source: str,
    ) -> tuple[HistoryIngest, ...]:
        """Ingest a batch that one `get_messages(min_id=..., reverse=True)`
        call returned, advancing the pointer message by message.

        Messages are taken in ascending id order, so the pointer only
        ever passes an id once everything below it in the batch has been
        handled. Anything at or below `min_id` is skipped (Telethon's
        `min_id` is already exclusive; this is a guard, not a filter).

        **A message that fails stops the batch.** It is recorded in
        `handler_errors` (with this `source`), logged, returned with
        `result=None`, and nothing after it in the batch is ingested. The
        pointer therefore rests at the last contiguous success -- never
        above an unstored message -- and the next read retries from
        there. Carrying on instead would let the next success move the
        pointer past the failed message, which no later read could then
        see: a transient `database is locked` would lose a message for
        good (fix round 1 did exactly that).

        **A message that keeps failing is skipped, loudly.** After
        `MAX_HISTORY_ATTEMPTS` failed reads of the same message, one
        writer job advances the pointer past that one id and bumps the
        durable `history_skipped` metric; the skip is logged at CRITICAL
        and the batch carries on. So a genuine poison message delays its
        channel by at most three reads per process lifetime and cannot
        crash-loop anything.

        A `WriterDeadError` stops the batch but is never counted: it means
        there is no writer, not that the message is bad. Nor is SQLite's
        "database is locked"/"busy" (`db.is_lock_error`): another program
        holding the write lock says nothing about the message either, and
        counting it let a lock held across three reads -- released just
        before the skip's own write -- skip a message for good (group-1
        review leftover G1L-2). Such a read is retried, uncounted, for as
        long as the lock lasts.
        """
        if source not in HISTORY_SOURCES:
            raise ValueError(
                f"ingest_history() is for history reads only; source must be one of "
                f"{sorted(HISTORY_SOURCES)}, got {source!r}"
            )
        channel_id = channel_cfg.channel_id
        outcomes: list[HistoryIngest] = []
        for message in sorted(messages, key=lambda m: m.id):
            if message.id <= min_id:
                continue
            key = (channel_id, message.id)
            try:
                incoming = normalise_message(message, channel_id=channel_id)
                result = await self.ingest_message(
                    incoming, channel_cfg=channel_cfg, source=source
                )
            except Exception as exc:  # noqa: BLE001 -- recorded; retried, then skipped
                self._record_handler_error(channel_id, message.id, exc, source=source)
                if isinstance(exc, db.WriterDeadError) or db.is_lock_error(exc):
                    # No writer, or another program holds the database's
                    # write lock: either says nothing about this message.
                    # Never counted -- a lock held across three reads must
                    # not skip a message for good (G1L-2). Next read retries.
                    outcomes.append(HistoryIngest(message_id=message.id, result=None))
                    break
                outcomes.append(HistoryIngest(message_id=message.id, result=None))
                attempts = self._history_attempts.get(key, 0) + 1
                self._history_attempts[key] = attempts
                if attempts < MAX_HISTORY_ATTEMPTS:
                    break  # the pointer stays below this id; the next read retries it
                if not await self._skip_history_message(
                    channel_cfg, message.id, attempts=attempts, last_error=exc
                ):
                    break  # the skip itself failed; the next read tries again
                # pop, not del: tolerant of a key already cleared, so even
                # a caller that breaks the one-read-per-channel rule by
                # calling ingest_history() concurrently gets no KeyError.
                self._history_attempts.pop(key, None)
                continue
            self._history_attempts.pop(key, None)
            outcomes.append(HistoryIngest(message_id=message.id, result=result))
        return tuple(outcomes)

    async def _skip_history_message(
        self,
        channel_cfg: ChannelCfg,
        message_id: int,
        *,
        attempts: int,
        last_error: Exception,
    ) -> bool:
        """Give up on one history message: advance the pointer past just
        this id and count it in `history_skipped`, in one transaction.
        Only called from `ingest_history`, where every earlier message in
        the batch has already succeeded -- so the pointer sits directly
        below this id and `MAX()` moves it past this one message only.
        Returns False (and skips nothing) if the job itself fails."""
        channel_id = channel_cfg.channel_id

        try:
            # Inside the try: a clock that fails here must make the skip
            # return False (retried next read), not escape ingest_history.
            day_key = metrics.day_key_for(self._now())

            def skip(conn: Connection) -> None:
                _advance_last_processed(conn, channel_id=channel_id, message_id=message_id)
                metrics.bump(conn, day_key=day_key, name=HISTORY_SKIPPED_METRIC)

            await self._run_on_writer(skip)
        except Exception as exc:  # noqa: BLE001 -- the next read retries the skip
            logger.error(
                "could not skip channel %s message_id %s (%s: %s); the next read "
                "tries again",
                channel_id,
                message_id,
                type(exc).__name__,
                exc,
            )
            return False
        logger.critical(
            "SKIPPED channel %s (%s) message_id %s after %s failed history reads "
            "(last: %s: %s). It is NOT stored and will not be retried; counted in "
            "metrics %r.",
            channel_id,
            channel_cfg.name,
            message_id,
            attempts,
            type(last_error).__name__,
            last_error,
            HISTORY_SKIPPED_METRIC,
        )
        return True

    async def read_history(
        self,
        channel_cfg: ChannelCfg,
        *,
        source: str,
        limit: int,
        rpc_timeout: float,
    ) -> tuple[HistoryIngest, ...]:
        """One history read for one channel: fetch everything after the
        pointer (oldest first, at most `limit`) and ingest it.

        This is the only operation that moves the pointer, and both
        startup backfill and Task 9's poll are this call. `limit` is
        required and always passed to `get_messages()` explicitly:
        Telethon 1.44 defaults `get_messages()` to **one** message unless
        both `min_id` and `max_id` are given (telethon/client/messages.py
        :590-594), so a call that leaves `limit` out quietly fetches one
        message per read. A batch smaller than the gap is fine -- the
        pointer stops at the last id read and the next call continues
        from there.

        A pointer of 0 on a channel this process has not yet seeded
        means the channel has never been read. Reading from 0 would
        return its *oldest* messages, so this call first seeds the
        pointer just below everything posted since the listener started
        (`_seed_pointer`) and then reads on from there as usual. If that
        seed fails at startup, the next call -- the poll's -- seeds
        instead, from the same start time, and never reads from 0 unless
        the channel verifiably has nothing older.

        `rpc_timeout` is required: it bounds every `get_messages()` RPC
        this call makes (`asyncio.wait_for`; `TimeoutError` propagates),
        so no history read -- and no holder of the per-channel lock
        below -- can wait on Telegram forever. Backfill passes the
        listener's `backfill_rpc_timeout`. It bounds the RPCs only, never
        the writes that follow: cancelling a batch half-way through its
        ingest would discard the outcomes of messages already stored (the
        poll counts `missed_by_eventstream` from them) and could land
        between a skip's write and its CRITICAL log.

        **At most one history read per channel runs at a time.** A read
        takes a per-channel lock for its whole length -- pointer read,
        fetch, ingest -- so a second call for the same channel waits for
        the first to finish and then reads from wherever it left the
        pointer. Two overlapping reads would each fetch the same batch
        from the same pointer: a message that fails would then count two
        attempts towards its skip in one round, and a skip raced by the
        other read would be taken twice. Reads of *different* channels
        do not wait for each other. (`ingest_history()` itself is not
        locked; call it only from within a read.)

        RPC errors propagate; the caller decides whether to retry.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"limit must be a positive int, got {limit!r}")
        if (
            isinstance(rpc_timeout, bool)
            or not isinstance(rpc_timeout, int | float)
            or rpc_timeout <= 0
        ):
            raise ValueError(f"rpc_timeout must be a positive number, got {rpc_timeout!r}")
        channel_id = channel_cfg.channel_id
        async with self.history_lock(channel_id):
            pointer = self.read_last_processed_message_id(channel_id)
            if pointer == 0 and channel_id not in self._pointer_seeded:
                pointer = await self._seed_pointer(channel_cfg, rpc_timeout=rpc_timeout)
            self._pointer_seeded.add(channel_id)
            messages = await asyncio.wait_for(
                self.client.get_messages(channel_id, limit=limit, min_id=pointer, reverse=True),
                rpc_timeout,
            )
            return await self.ingest_history(
                channel_cfg, messages, min_id=pointer, source=source
            )

    async def probe_writer(self) -> None:
        """Run a no-op (`SELECT 1`) on the writer thread and await it.

        A dead writer is otherwise noticed only by the next write, and a
        quiet channel makes none -- so a writer that died at night would
        go unnoticed until the morning's first signal, which it would then
        fail to store. The poll calls this once per interval. It goes
        through `_run_on_writer()`, so a `WriterDeadError` stops the
        listener exactly as a failed ingest does (and is re-raised).

        A read-only job: it asks whether the writer thread answers, not
        whether the write lock is free. Another program holding that lock
        past the busy timeout must not fail it and stop `watch`."""
        await self._run_on_writer(_writer_noop, read_only=True)

    def writer_progressed_since(self, moment: float) -> bool:
        """Whether the writer thread started or finished a job at or after
        `moment` (a `time.monotonic()` reading) -- the poll's test of a
        busy writer versus a hung one (see `db.Writer.last_progress`)."""
        progress = getattr(self.writer, "last_progress", None)
        return progress is not None and progress >= moment

    async def record_metric(self, name: str, value: int = 1) -> None:
        """`metrics(today, name) += value`, on the writer thread, where
        today is the UTC day of this listener's injected clock. Through
        `_run_on_writer()`, so a dead writer stops the listener here too.
        """
        day_key = metrics.day_key_for(self._now())

        def bump(conn: Connection) -> None:
            metrics.bump(conn, day_key=day_key, name=name, value=value)

        await self._run_on_writer(bump)

    async def record_heartbeats(
        self, beats: Mapping[str, str | None], *, resolve_alerts: tuple[str, ...] = ()
    ) -> int:
        """Stamp every `name -> detail` in `beats` at this listener's
        injected "now" (`store.heartbeats.beat`), in one writer job -- so a
        round's stamps land together or not at all -- and in the same job
        acknowledge every unacknowledged alert whose code is in
        `resolve_alerts` (the condition it reported is gone). Returns how
        many alerts that cleared. Through `_run_on_writer()`, so a dead
        writer stops the listener here too. `xaucopier status` reads the
        stamps from its own process."""
        now_utc = self._now()
        items = tuple(beats.items())
        codes = tuple(resolve_alerts)

        def job(conn: Connection) -> int:
            for name, detail in items:
                heartbeats.beat(conn, name=name, now_utc=now_utc, detail=detail)
            return alerts.ack_all(conn, codes=codes) if codes else 0

        cleared = await self._run_on_writer(job)
        if cleared:
            logger.warning(
                "cleared %d alert(s) %s: Telegram is answering this session again",
                cleared,
                list(codes),
            )
        return cleared

    async def record_alert(
        self,
        level: alerts.AlertLevel,
        code: str,
        detail: str,
        *,
        channel_id: int | None = None,
        message_id: int | None = None,
        signal_id: int | None = None,
    ) -> int:
        """Record one operator alert (`store.alerts.record`) on the writer
        thread, at this listener's injected "now"; returns its id. Through
        `_run_on_writer()`, so a dead writer stops the listener here too."""
        now_utc = self._now()

        def job(conn: Connection) -> int:
            return alerts.record(
                conn,
                now_utc=now_utc,
                level=level,
                code=code,
                detail=detail,
                channel_id=channel_id,
                message_id=message_id,
                signal_id=signal_id,
            )

        return await self._run_on_writer(job)

    async def _seed_pointer(self, channel_cfg: ChannelCfg, *, rpc_timeout: float) -> int:
        """First run for this channel: set the pointer just below
        everything posted since the listener started, and return it.

        The anchor is the newest message dated before `cutoff =
        started_at - FIRST_RUN_SEED_MARGIN` -- one RPC,
        `get_messages(limit=1, offset_date=cutoff)` (Telegram's
        `getHistory` returns only messages sent before `offset_date`).
        The pointer is set to it, and the read that follows in the same
        `read_history()` call ingests everything after it.

        **Why the start time, and not "now".** Seeding at whatever is
        newest when the seed finally succeeds is only harmless at
        startup. If the startup seed fails (a connection blip, a long
        flood wait, a timeout), the poll seeds later -- and "newest" by
        then includes messages posted after the listener started, which
        the update stream may well have dropped. Seeding above them would
        skip them for good: never stored, never counted, with a log line
        claiming nothing after the seed was lost (fix round 1's
        Critical, probe 1f). With the cutoff tied to the start, a late
        seed lands where a prompt one would have, and those messages are
        ingested by the poll (`source="poll"`, so they count as missed).
        At startup it finds only what was posted in the margin or during
        startup itself -- messages the update stream could not have
        delivered either, since handlers are not registered yet.

        The margin absorbs skew between this machine's clock (the start
        time) and Telegram's (the message dates).

        No anchor -- the channel is empty, or everything in it is after
        the cutoff -- leaves the pointer at 0 and the read goes from 0.
        That is safe only if the channel really has nothing older, so it
        is verified: the oldest message is fetched, and if *it* predates
        the cutoff the anchor query was wrong and this raises
        `RuntimeError` rather than ingest the channel's ancient history
        (the reviewer's D3). The next read retries the seed.
        """
        channel_id = channel_cfg.channel_id
        if self._started_at is None:
            raise RuntimeError("a first-run seed needs the start time Listener.start() records")
        cutoff = self._started_at - FIRST_RUN_SEED_MARGIN
        before_cutoff = await asyncio.wait_for(
            self.client.get_messages(channel_id, limit=1, offset_date=cutoff), rpc_timeout
        )
        anchor_id = max((message.id for message in before_cutoff), default=0)
        if anchor_id:

            def seed(conn: Connection) -> None:
                _advance_last_processed(conn, channel_id=channel_id, message_id=anchor_id)

            await self._run_on_writer(seed)
        else:
            oldest = await asyncio.wait_for(
                self.client.get_messages(channel_id, limit=1, reverse=True), rpc_timeout
            )
            if any(message.date < cutoff for message in oldest):
                raise RuntimeError(
                    f"channel {channel_id} ({channel_cfg.name}): Telegram reported no "
                    f"message before {cutoff.isoformat()}, yet its oldest message "
                    f"({oldest[0].id}) is older; refusing to read its history from 0"
                )
        self._pointer_seeded.add(channel_id)
        logger.warning(
            "first run for channel %s (%s): pointer seeded at message_id %s, the newest "
            "message dated before %s (listener start minus %ss). Everything after it is "
            "ingested; nothing before it is.",
            channel_id,
            channel_cfg.name,
            anchor_id,
            cutoff.isoformat(),
            int(FIRST_RUN_SEED_MARGIN.total_seconds()),
        )
        return anchor_id

    # -- startup ------------------------------------------------------------

    async def start(self) -> None:
        """Steps 0-5 of the startup order. See the module docstring --
        the order is the design, not an implementation detail."""
        if self._started:
            raise RuntimeError("Listener.start() called twice")
        self._started = True
        # Before anything else, so before handlers are registered: the
        # first-run seed keeps everything posted after this instant.
        started_at = self._now()
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError(f"the injected clock returned a naive datetime: {started_at!r}")
        self._started_at = started_at.astimezone(UTC)
        self._writer_death = asyncio.get_running_loop().create_future()

        await self._start_writer_and_wait_for_connection()  # 0
        self._reader = db.connect(self.db_path, role="reader")

        await self.client.connect()  # 1
        await self.client.get_dialogs()  # 2

        for channel in self.enabled_channels:  # 3
            await self._backfill_channel(channel)

        self._register_handlers()  # 4
        self._start_poll_task()  # 5

    async def _start_writer_and_wait_for_connection(self) -> None:
        self.writer.start()
        # The probe can only run after Writer._run has already called
        # db.connect() on the writer thread -- which is what makes
        # "the writer connected first" a guarantee rather than a hope.
        # Bounded, because "wait for the writer" must never become "wait
        # forever": a supervisor cannot tell a hung start() from a
        # healthy listener, and a hung listener misses every trade.
        # Read-only: another program holding the write lock at this moment
        # must not look like "the writer thread could not connect".
        probe = asyncio.wrap_future(self.writer.submit(_writer_ready_probe, read_only=True))
        try:
            await asyncio.wait_for(probe, timeout=self._writer_ready_timeout)
        except TimeoutError as exc:
            raise ListenerStartupError(
                f"the writer thread did not connect to {self.db_path} within "
                f"{self._writer_ready_timeout:g}s; refusing to listen without it"
            ) from exc
        except Exception as exc:  # noqa: BLE001 -- any probe failure means no writer
            raise ListenerStartupError(
                f"the writer thread could not connect to {self.db_path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    async def _backfill_channel(self, channel: ChannelCfg) -> None:
        # Imported here for the same reason as in run_listener().
        from xaucopier.ingest.client import PERMANENT_TELEGRAM_ERRORS

        try:
            await self.read_history(
                channel,
                source="backfill",
                limit=self._backfill_limit,
                rpc_timeout=self._backfill_rpc_timeout,
            )
        except PERMANENT_TELEGRAM_ERRORS:
            # A dead session or a rejected api_id is not "one bad channel":
            # every read will fail the same way, so stop at startup rather
            # than log it here and meet it again in the poll.
            raise
        except Exception as exc:  # noqa: BLE001 -- one bad channel must not block the rest
            # Failing the whole startup here would crash-loop the process
            # for every *other* channel too. Task 9's poll reads from the
            # same pointer, which only a successful history read moves,
            # so a channel skipped now is retried shortly -- loudly
            # logged, never silent.
            logger.exception(
                "backfill failed for channel %s (%s); continuing -- the poll "
                "will retry from the stored pointer: %s",
                channel.channel_id,
                channel.name,
                exc,
            )

    def _register_handlers(self) -> None:
        # No `chats=` filter on purpose: resolving it puts a potential
        # get_input_entity() RPC on Telethon's dispatch path. The
        # channel filter is the dict lookup in each handler instead.
        self.client.add_event_handler(self._on_new_message, events.NewMessage())
        self.client.add_event_handler(self._on_message_edited, events.MessageEdited())
        self.client.add_event_handler(self._on_message_deleted, events.MessageDeleted())

    def _start_poll_task(self) -> None:
        if self.poll_hook is None:
            return
        self.poll_task = asyncio.create_task(
            self.poll_hook(self), name="xaucopier-poll"
        )

    # -- the hot path -------------------------------------------------------

    async def _on_new_message(self, event: Any) -> None:
        """Route, normalise, submit, return. Nothing else belongs here.

        Routing is first: with no `chats=` filter the handler sees every
        chat the account is in, and a message from one nobody watches
        must cost one dict lookup, not a normalisation (or an error
        record for a chat we do not care about).

        Every exception is caught and recorded: a handler that raises
        must never take down the client (and with it every *other*
        channel's updates) over one malformed message.
        """
        channel_id: int | None = None
        try:
            channel_id = event.chat_id
            channel_cfg = self.channel_cfg_for(channel_id)  # 1. route
            if channel_cfg is None:
                return

            incoming = normalise_event(event)  # 2. normalise

            await self.ingest_message(  # 3. submit
                incoming, channel_cfg=channel_cfg, source=LIVE_SOURCE
            )  # 4. return
        except Exception as exc:  # noqa: BLE001 -- recorded, never fatal
            # Error path only: a plain attribute read for the record.
            message_id = getattr(getattr(event, "message", None), "id", None)
            self._record_handler_error(channel_id, message_id, exc, source=LIVE_SOURCE)

    async def _on_message_edited(self, event: Any) -> None:
        """Route, normalise, submit, return -- `_on_new_message`'s
        contract, for `MessageEdited`. A hint: the window re-read is the
        source of truth, and whichever of the two gets there first
        applies the edit, once. No RPC, no `sqlite3`; every failure is
        recorded, never raised into the client."""
        channel_id: int | None = None
        try:
            channel_id = event.chat_id
            channel_cfg = self.channel_cfg_for(channel_id)  # 1. route
            if channel_cfg is None:
                return

            incoming = normalise_event(event)  # 2. normalise

            await self.ingest_edit(  # 3. submit
                incoming, channel_cfg=channel_cfg, detected_by=edits_repo.EVENT_DETECTOR
            )  # 4. return
        except Exception as exc:  # noqa: BLE001 -- recorded, never fatal
            message_id = getattr(getattr(event, "message", None), "id", None)
            self._record_handler_error(channel_id, message_id, exc, source=LIVE_SOURCE)

    async def _on_message_deleted(self, event: Any) -> None:
        """Route, normalise, submit, return, for `MessageDeleted`.

        Outside a channel Telegram does not say where a message was
        deleted (Telethon builds the event with `peer=None`, so `chat_id`
        is None); that routes to nothing and is dropped, like any chat
        nobody watches. Each id is submitted as its own job, so one that
        fails does not hold back the rest -- and the window re-read would
        find it anyway."""
        channel_id: int | None = None
        try:
            channel_id = event.chat_id
            channel_cfg = self.channel_cfg_for(channel_id)  # 1. route
            if channel_cfg is None:
                return
            deletion = normalise_deletion_event(event)  # 2. normalise
        except Exception as exc:  # noqa: BLE001 -- recorded, never fatal
            self._record_handler_error(channel_id, None, exc, source=LIVE_SOURCE)
            return
        for message_id in deletion.message_ids:
            try:
                await self.ingest_deletion(  # 3. submit
                    channel_cfg, message_id, detected_by=edits_repo.EVENT_DETECTOR
                )
            except Exception as exc:  # noqa: BLE001 -- recorded, never fatal
                self._record_handler_error(channel_id, message_id, exc, source=LIVE_SOURCE)
        # 4. return

    def _record_handler_error(
        self,
        channel_id: int | None,
        message_id: int | None,
        exc: Exception,
        *,
        source: str,
    ) -> None:
        self._handler_error_count += 1
        self._handler_errors.append(
            HandlerError(
                channel_id=channel_id,
                message_id=message_id,
                error=f"{type(exc).__name__}: {exc}",
                source=source,
            )
        )
        logger.error(
            "ingest failed (%s) for channel=%s message_id=%s: %s",
            source,
            channel_id,
            message_id,
            exc,
            exc_info=exc,
        )

    # -- running and stopping ------------------------------------------------

    async def wait_until_stopped(self) -> NoReturn:
        """Step 6. Wait on `run_until_disconnected()`, the poll task *and*
        the writer's death; whichever comes first ends the listener.

        Any return from `run_until_disconnected()` -- clean or not --
        means this process is no longer receiving updates, so it is
        turned into `ListenerStopped` (an exception it raised, such as a
        terminal auth error, propagates as itself so `run_listener()` can
        classify it). The poll finishing -- raising, returning, or being
        cancelled -- is `ListenerStopped` too: without it, a blackout's
        worth of messages is silently lost while the process looks
        healthy, so the supervisor must restart it. The one exception is
        a terminal auth error from the poll, re-raised as itself (like
        one from `run_until_disconnected()`) so it exits 3, not 4 -- and
        likewise a rejected api_id/api_hash, which exits 5. And any write that
        found the writer thread dead is `ListenerStopped`, chained from
        the `WriterDeadError`: a listener that cannot store anything must
        not keep looking healthy either.
        """
        run_task: asyncio.Future[Any] = asyncio.ensure_future(
            self.client.run_until_disconnected()
        )
        watched: set[asyncio.Future[Any]] = {run_task}
        if self.poll_task is not None:
            watched.add(self.poll_task)
        if self._writer_death is not None:
            watched.add(self._writer_death)
        try:
            done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not run_task.done():
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)

        # Decided on what finished *first* (`done`), not on task state
        # after the cancel above, so the poll's own failure is what gets
        # reported when the poll is what stopped.
        if run_task in done:
            if not run_task.cancelled():
                run_task.result()  # re-raises whatever run_until_disconnected() raised
            raise ListenerStopped(
                "run_until_disconnected() returned: the listener is no longer "
                "receiving Telegram updates. A listener that has stopped "
                "listening is never a successful exit."
            )

        if self._writer_death is not None and self._writer_death in done:
            death = self._writer_death.result()
            raise ListenerStopped(
                f"the writer thread died ({death}): nothing can be stored, so the "
                "listener is stopping rather than running without a writer"
            ) from death

        # Neither of the others, so the poll task finished first.
        poll_task = self.poll_task
        if poll_task is None:  # pragma: no cover -- unreachable, see above
            raise ListenerStopped("the listener stopped for an unknown reason")
        if poll_task.cancelled():
            raise ListenerStopped(
                "the poll task was cancelled: nothing is recovering messages "
                "the update stream drops"
            )
        poll_error = poll_task.exception()
        if poll_error is not None:
            # Imported here for the same reason as in run_listener().
            from xaucopier.ingest.client import PERMANENT_TELEGRAM_ERRORS

            if isinstance(poll_error, PERMANENT_TELEGRAM_ERRORS):
                # As `run_task.result()` does above: a dead session (or a
                # rejected api_id) is not "restart me" but "stop trying",
                # and run_listener() can only say so (exit 3 or 5) if it
                # sees the error itself.
                raise poll_error
            raise ListenerStopped(
                f"the poll task died ({type(poll_error).__name__}: {poll_error}): "
                "nothing is recovering messages the update stream drops"
            ) from poll_error
        raise ListenerStopped(
            "the poll task returned: nothing is recovering messages the update "
            "stream drops"
        )

    async def run(self) -> NoReturn:
        """`start()` then wait. Always raises: `ListenerStopped` when the
        client stops or the poll task dies, `ListenerStartupError` (a
        `ListenerStopped`) when the writer never came up, or whatever
        else startup raised."""
        await self.start()
        await self.wait_until_stopped()

    async def aclose(self) -> None:
        """Tear everything down, in the reverse order of `start()`.
        Idempotent."""
        if self._closed:
            return
        self._closed = True

        if self.poll_task is not None:
            self.poll_task.cancel()
            try:
                await self.poll_task
            except (asyncio.CancelledError, Exception):  # noqa: B030 -- shutdown is best-effort
                pass

        try:
            await self.client.disconnect()
        except Exception:  # noqa: BLE001 -- shutdown is best-effort
            logger.exception("error disconnecting the Telegram client")

        # stop() drains every queued write, then joins the thread. Run it
        # off the loop thread so the join never blocks the loop.
        await asyncio.to_thread(self.writer.stop, self._writer_stop_timeout)

        if self._reader is not None:
            self._reader.close()
            self._reader = None


def _writer_ready_probe(conn: Connection) -> bool:
    """A job that writes nothing. Its only purpose is to have *run* on
    the writer thread, which proves `Writer._run` already connected."""
    return True


def _writer_noop(conn: Connection) -> None:
    """`probe_writer()`'s job: touches the connection, writes nothing."""
    conn.execute("SELECT 1").fetchone()



def _utcnow() -> datetime:
    return datetime.now(UTC)


async def run_listener(listener: Listener) -> int:
    """Run `listener` to completion and return a process exit code.

    Never returns 0: reaching the end of this function at all means the
    listener stopped. `EXIT_TERMINAL_AUTH` (3) tells the supervisor the
    session (or the account) is dead and a restart is pointless;
    `EXIT_CONFIG_ERROR` (5) that Telegram rejects the api_id/api_hash,
    which only a new pair can fix; `EXIT_LISTENER_STOPPED` (4) means
    "stopped, restart me".
    """
    # Imported here rather than at module import time so this module
    # stays importable (and testable) without pywin32 being loaded.
    from xaucopier.ingest.client import (
        API_CREDENTIAL_ERRORS,
        EXIT_CONFIG_ERROR,
        EXIT_TERMINAL_AUTH,
        TERMINAL_AUTH_ERRORS,
    )

    try:
        await listener.run()
    except ListenerStopped as exc:
        logger.error("%s", exc)
        return EXIT_LISTENER_STOPPED
    except TERMINAL_AUTH_ERRORS as exc:
        logger.error(
            "Telegram refuses this session (%s): it was logged out, revoked, used "
            "from two places at once, or the account is deactivated, banned or "
            "frozen. A restart cannot fix it. Run `.\\xaucopier setup-login` again.",
            type(exc).__name__,
        )
        return EXIT_TERMINAL_AUTH
    except API_CREDENTIAL_ERRORS as exc:
        logger.error(
            "Telegram rejects this program's api_id/api_hash (%s). Neither a restart "
            "nor logging in again with the same pair can fix it: get a new "
            "api_id/api_hash at https://my.telegram.org, then run "
            "`.\\xaucopier setup-login` again.",
            type(exc).__name__,
        )
        return EXIT_CONFIG_ERROR
    finally:
        await listener.aclose()

    # Unreachable: Listener.run() is NoReturn. Kept so that a future
    # refactor which makes it returnable still cannot exit 0 here.
    return EXIT_LISTENER_STOPPED
