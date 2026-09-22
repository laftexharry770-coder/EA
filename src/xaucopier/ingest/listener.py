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
   written in.
1. **connect.**
2. **`await client.get_dialogs()`.** This primes the per-channel `pts`
   state Telethon's update engine needs. It is not cosmetic -- skipping
   it degrades update delivery -- and it must happen before handlers
   exist, so the first update the engine dispatches is already against
   primed state.
3. **Backfill** each enabled channel from
   `channel_state.last_processed_message_id`, so a restart does not lose
   whatever was posted while the process was down. Backfill runs
   *before* handlers are registered, so a message can never be ingested
   by backfill and the update stream at the same instant (and even if it
   were, `ingest()`'s dedup would make the second one a no-op).
4. **Register handlers.**
5. **Start the poll task** (Task 9 owns the poll itself; `poll_hook` is
   its slot). It starts last because a poll racing backfill over the
   same rows is pointless work.
6. **`run_until_disconnected()`, wrapped so any return is non-zero.** A
   listener that returns cleanly has still stopped listening: for the
   supervisor there is no such thing as a successful exit here, so the
   return is turned into `ListenerStopped` / `EXIT_LISTENER_STOPPED`.

**The handler does exactly four things: normalise, route, submit,
return.** No `get_entity`, no `get_reply_message`, no RPC of any kind --
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
`_advance_last_processed` for the failure this prevents.

**No `sqlite3` anywhere in this package.** Writes go through
`Writer.submit()`; the one read this module does (the per-channel
backfill pointer) goes through a `store.db.connect(role="reader")`
connection opened after the writer barrier above.
tests/test_listener.py asserts this with an AST walk over the whole
`ingest/` package.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from telethon import events

from xaucopier.config.accounts import AccountCfg
from xaucopier.config.channels import ChannelCfg
from xaucopier.store import db, messages_repo
from xaucopier.store.messages_repo import IngestResult

logger = logging.getLogger(__name__)

#: Exit code for "the client stopped listening". Distinct from
#: `client.EXIT_TERMINAL_AUTH` (3), which tells the supervisor *not* to
#: restart; this one is retryable, so the supervisor should restart.
EXIT_LISTENER_STOPPED = 4

DEFAULT_BACKFILL_LIMIT = 500
DEFAULT_WRITER_STOP_TIMEOUT = 15.0

#: `sqlite3.Connection`, unannotated on purpose: this package must not
#: import sqlite3 at all (see the module docstring), not even for a type.
Connection = Any

#: The poll slot Task 9 fills: `async def poll(listener) -> None`.
PollHook = Callable[["Listener"], Awaitable[None]]

#: `messages.source` for a message delivered by the update stream.
LIVE_SOURCE = "live"
#: `messages.source` values for a message found by reading channel
#: history. These -- and only these -- advance the per-channel pointer;
#: see `_advance_last_processed` for why.
HISTORY_SOURCES = frozenset({"backfill", "poll"})
ALL_SOURCES = HISTORY_SOURCES | {LIVE_SOURCE}


class ListenerStopped(RuntimeError):
    """`run_until_disconnected()` returned. Whether it returned cleanly
    or not, the listener has stopped listening -- which for a supervisor
    is never a success."""


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
class HandlerError:
    """A handler exception, kept so a test (and an operator) can see that
    one happened rather than inferring it from a missing row."""

    channel_id: int | None
    message_id: int | None
    error: str


@dataclass(frozen=True, slots=True)
class HistoryIngest:
    """What happened to one message a history read found.

    `result` is the `ingest()` outcome -- `"new"` means the update stream
    had not delivered it (yet), which is what Task 9's
    `missed_by_eventstream` counter counts.
    """

    message_id: int
    result: IngestResult


def _to_utc(value: datetime) -> datetime:
    """Force a timezone-aware UTC datetime. Telethon already hands out
    aware UTC datetimes; a naive one would otherwise be written to the
    store as if it were local time."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
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
        posted_at_utc=_to_utc(message.date),
        edit_date_utc=_to_utc(edit_date) if edit_date is not None else None,
        reply_to_message_id=reply_to,
    )


def normalise_event(event: Any) -> IncomingMessage:
    """Flatten a `NewMessage` event. `event.chat_id` is the marked
    channel id (`-100...`) that `channels.toml` already uses, so no
    `get_entity()` round trip is needed to identify the channel."""
    return normalise_message(event.message, channel_id=event.chat_id)


def _advance_last_processed(
    conn: Connection, *, channel_id: int, message_id: int
) -> None:
    """Move `channel_state.last_processed_message_id` forward, never
    backwards.

    **Only a history read may call this** -- startup backfill and
    (Task 9) the poll, i.e. the sources in `HISTORY_SOURCES`. A history
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

    Task 9 and Task 11 build on the public surface here --
    `client`, `channels`, `enabled_channels`, `reader`,
    `channel_cfg_for()`, `read_last_processed_message_id()`,
    `read_history()`, `ingest_history()`, `ingest_message()` and
    `poll_hook` -- rather than reaching into the internals. The poll
    should be `read_history(channel, source="poll", limit=...)`: it is
    the one place the pointer is read, fetched from and advanced.
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
        writer_stop_timeout: float = DEFAULT_WRITER_STOP_TIMEOUT,
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
        self._writer_stop_timeout = writer_stop_timeout
        self._channels_by_id: dict[int, ChannelCfg] = {
            channel.channel_id: channel for channel in self.channels if channel.enabled
        }
        self._reader: Connection = None
        self._handler_errors: list[HandlerError] = []
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
        return tuple(self._handler_errors)

    @property
    def handler_error_count(self) -> int:
        return len(self._handler_errors)

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

        return await asyncio.wrap_future(self.writer.submit(job))

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
        """
        if source not in HISTORY_SOURCES:
            raise ValueError(
                f"ingest_history() is for history reads only; source must be one of "
                f"{sorted(HISTORY_SOURCES)}, got {source!r}"
            )
        outcomes: list[HistoryIngest] = []
        for message in sorted(messages, key=lambda m: m.id):
            incoming = normalise_message(message, channel_id=channel_cfg.channel_id)
            if incoming.message_id <= min_id:
                continue
            result = await self.ingest_message(
                incoming, channel_cfg=channel_cfg, source=source
            )
            outcomes.append(HistoryIngest(message_id=incoming.message_id, result=result))
        return tuple(outcomes)

    async def read_history(
        self, channel_cfg: ChannelCfg, *, source: str, limit: int
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

        RPC errors propagate; the caller decides whether to retry.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"limit must be a positive int, got {limit!r}")
        channel_id = channel_cfg.channel_id
        pointer = self.read_last_processed_message_id(channel_id)
        messages = await self.client.get_messages(
            channel_id, limit=limit, min_id=pointer, reverse=True
        )
        return await self.ingest_history(
            channel_cfg, messages, min_id=pointer, source=source
        )

    # -- startup ------------------------------------------------------------

    async def start(self) -> None:
        """Steps 0-5 of the startup order. See the module docstring --
        the order is the design, not an implementation detail."""
        if self._started:
            raise RuntimeError("Listener.start() called twice")
        self._started = True

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
        await asyncio.wrap_future(self.writer.submit(_writer_ready_probe))

    async def _backfill_channel(self, channel: ChannelCfg) -> None:
        try:
            await self.read_history(
                channel, source="backfill", limit=self._backfill_limit
            )
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
        # channel filter is the dict lookup in the handler instead.
        self.client.add_event_handler(self._on_new_message, events.NewMessage())

    def _start_poll_task(self) -> None:
        if self.poll_hook is None:
            return
        self.poll_task = asyncio.create_task(
            self.poll_hook(self), name="xaucopier-poll"
        )

    # -- the hot path -------------------------------------------------------

    async def _on_new_message(self, event: Any) -> None:
        """Normalise, route, submit, return. Nothing else belongs here.

        Every exception is caught and recorded: a handler that raises
        must never take down the client (and with it every *other*
        channel's updates) over one malformed message.
        """
        channel_id: int | None = None
        message_id: int | None = None
        try:
            incoming = normalise_event(event)  # 1. normalise
            channel_id = incoming.channel_id
            message_id = incoming.message_id

            channel_cfg = self.channel_cfg_for(channel_id)  # 2. route
            if channel_cfg is None:
                return

            await self.ingest_message(  # 3. submit
                incoming, channel_cfg=channel_cfg, source="live"
            )  # 4. return
        except Exception as exc:  # noqa: BLE001 -- recorded, never fatal
            self._record_handler_error(channel_id, message_id, exc)

    def _record_handler_error(
        self, channel_id: int | None, message_id: int | None, exc: Exception
    ) -> None:
        self._handler_errors.append(
            HandlerError(
                channel_id=channel_id,
                message_id=message_id,
                error=f"{type(exc).__name__}: {exc}",
            )
        )
        logger.exception(
            "listener handler failed for channel=%s message_id=%s: %s",
            channel_id,
            message_id,
            exc,
        )

    # -- running and stopping ------------------------------------------------

    async def wait_until_stopped(self) -> NoReturn:
        """Step 6. Any return from `run_until_disconnected()` -- clean or
        not -- means this process is no longer receiving updates, so it
        is turned into an exception the supervisor can act on."""
        await self.client.run_until_disconnected()
        raise ListenerStopped(
            "run_until_disconnected() returned: the listener is no longer "
            "receiving Telegram updates. A listener that has stopped "
            "listening is never a successful exit."
        )

    async def run(self) -> NoReturn:
        """`start()` then wait. Always raises: `ListenerStopped` when the
        client stops, or whatever startup raised."""
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


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def run_listener(listener: Listener) -> int:
    """Run `listener` to completion and return a process exit code.

    Never returns 0: reaching the end of this function at all means the
    listener stopped. `EXIT_TERMINAL_AUTH` (3) tells the supervisor the
    session is dead and a restart is pointless; `EXIT_LISTENER_STOPPED`
    (4) means "stopped, restart me".
    """
    # Imported here rather than at module import time so this module
    # stays importable (and testable) without pywin32 being loaded.
    from xaucopier.ingest.client import EXIT_TERMINAL_AUTH, TERMINAL_AUTH_ERRORS

    try:
        await listener.run()
    except ListenerStopped as exc:
        logger.error("%s", exc)
        return EXIT_LISTENER_STOPPED
    except TERMINAL_AUTH_ERRORS as exc:
        logger.error(
            "terminal Telegram auth error (%s): the session is dead and a "
            "restart cannot revive it. Run `xaucopier setup-login` again.",
            type(exc).__name__,
        )
        return EXIT_TERMINAL_AUTH
    finally:
        await listener.aclose()

    # Unreachable: Listener.run() is NoReturn. Kept so that a future
    # refactor which makes it returnable still cannot exit 0 here.
    return EXIT_LISTENER_STOPPED
