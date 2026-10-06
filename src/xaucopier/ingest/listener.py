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
   Then, before Telegram is contacted, one writer job cancels (SKIPPED,
   'channel switched off') every trade entry still waiting for a signal
   of a channel that is no longer switched on, with one WARNING alert
   naming how many (final review F15): nothing reads such a channel, so
   nothing would see its poster withdraw the signal.
1. **connect**, within a deadline (`client.STARTUP_TIMEOUT_SECONDS`,
   60 s). Telethon's own connect never gives up, so with no network --
   or a malformed reply making it reconnect in a tight loop -- `start()`
   used to hang for ever (ledger 145). Past the deadline it raises
   `ListenerStartupError` with a plain "Could not reach Telegram within
   60 seconds", and `watch` exits 4.
   Then **Telegram's clock**: one `updates.GetStateRequest()`, bounded
   (`TELEGRAM_CLOCK_TIMEOUT_SECONDS`, 10 s). The start time the first-run
   seed keeps everything after is min(this computer's clock, Telegram's)
   at the moment `start()` began, in whole seconds (ledger 143): a
   computer clock running fast used to push that start -- and with it a
   late seed's anchor -- past posts the update stream dropped. A
   difference over a minute is a plain WARNING, never a refusal; no
   answer means this computer's clock, with a WARNING.
2. **`await client.get_dialogs()`**, within the same deadline. This
   primes the per-channel `pts`
   state Telethon's update engine needs. It is not cosmetic -- skipping
   it degrades update delivery -- and it must happen before handlers
   exist, so the first update the engine dispatches is already against
   primed state.
   From that same chat list, each switched-on source is **named** as a
   channel, a group or unknown (`client.source_kind`; final review F05):
   a group -- anyone in it can post, and nothing filters who posted -- is
   a plain WARNING, and so is a source this account's chat list does not
   hold. Never a refusal: the user may mean it.
   Then one writer job: each enabled channel that needs a seed gets its
   **seed cutoff stored** -- this start, unless an earlier attempt's is
   already there (`store/channel_state.py`; F01).
3. **Backfill** each enabled channel from
   `channel_state.last_processed_message_id`, so a restart does not lose
   whatever was posted while the process was down. A channel never read
   (pointer 0 and no seeded marker), or one switched back on (a cutoff
   set on it), is *seeded* first: rather than ingest the channel's
   *oldest* messages (which is what `get_messages(min_id=0,
   reverse=True)` returns -- possibly years-old signals, each queuing
   ENTRY outbox rows), its pointer is set at the newest message dated
   before its **stored** seed cutoff (less a 60 s clock-skew margin) and
   only what came after it is ingested -- see `Listener._seed_pointer`
   for why the cutoff is the first attempt's start and not the moment
   the seed happens to succeed. Every history RPC here
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

from telethon import events, functions
from telethon import utils as tl_utils
from telethon.tl import types as tl_types

from xaucopier.config.accounts import AccountCfg
from xaucopier.config.channels import ChannelCfg
from xaucopier.store import (
    alerts,
    channel_state,
    db,
    edits_repo,
    heartbeats,
    messages_repo,
    metrics,
    outbox,
)
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
#: How far before a channel's stored seed cutoff its first read still
#: ingests from. Absorbs what skew is left between the start time and
#: Telegram's message dates; see `Listener._seed_pointer`.
FIRST_RUN_SEED_MARGIN = timedelta(seconds=60)
#: How long `start()` waits for Telegram's clock (`updates.GetState`)
#: before it uses this computer's own (ledger 143).
TELEGRAM_CLOCK_TIMEOUT_SECONDS = 10.0
#: A difference between this computer's clock and Telegram's above this
#: is logged as a WARNING (never a refusal).
CLOCK_DIFFERENCE_WARNING = timedelta(seconds=60)
#: A stored seed cutoff older than this, when used, is named in a WARNING.
OLD_SEED_CUTOFF_WARNING = timedelta(minutes=10)
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

#: The most ids `DeletedBeforeStored` keeps per channel: the lowest go
#: first. Only ids above the pointer are kept at all, and each goes once
#: the pointer passes it, so this bound is only ever reached by a channel
#: whose poster deletes hundreds of posts the forward read has not
#: reached yet.
MAX_DELETED_BEFORE_STORED = 500

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


class DeletedBeforeStored:
    """Ids a `MessageDeleted` event named while they were not stored yet,
    per channel, with when the event said so (final review F18).

    The race: the poll's forward read has fetched a message, and the
    deletion event for it is handled before the read's ingest job runs.
    The deletion finds nothing stored ("unknown"); the read then stored
    the message as if it stood -- a signal with claimable entries. With
    company in the window re-read the absence healed it a round later; as
    a channel's only message, every window read missed all of it and
    tripped the circuit breaker, so it never healed. So the deletion job
    remembers the id here, with the report's time and the other ids it
    named, and the ingest job that stores it later applies that deletion
    in the same transaction (`messages_repo.ingest`'s
    `deleted_by_event_at_utc` / `deleted_by_event_with`): the race changes
    nothing -- the result is the event arriving a moment later, the signal
    VOIDED before any worker could claim it, and a relay copy of it seen
    as a copy of a deleted post whichever was stored first.

    **Touched only inside writer jobs**, which run one at a time on the
    writer thread: a deletion job adds, an ingest job reads. Each job
    first drops the ids at or below the channel's *stored* pointer, read
    inside that job -- the forward read never fetches those again -- so
    the pruning never depends on a job that later rolled back. An id at
    or below the pointer when the event names it is never kept (a message
    deleted from long ago that we never stored). Bounded per channel
    (`MAX_DELETED_BEFORE_STORED`, lowest ids dropped first). In memory
    only: a restart forgets it, and the window re-read then finds such a
    deletion by absence, as before."""

    __slots__ = ("_ids", "_limit")

    def __init__(self, *, limit: int = MAX_DELETED_BEFORE_STORED) -> None:
        #: channel -> message id -> (the report's time, the ids it named).
        self._ids: dict[int, dict[int, tuple[datetime, frozenset[int]]]] = {}
        self._limit = limit

    def ids(self, channel_id: int) -> frozenset[int]:
        return frozenset(self._ids.get(channel_id, {}))

    def pending(self, channel_id: int) -> bool:
        """Whether any id of the channel is remembered (a cheap check
        before the ingest job reads the pointer)."""
        return bool(self._ids.get(channel_id))

    def prune(self, channel_id: int, pointer: int) -> None:
        """Forget the ids the pointer has passed."""
        ids = self._ids.get(channel_id)
        if not ids:
            return
        for message_id in [m for m in ids if m <= pointer]:
            del ids[message_id]
        if not ids:
            del self._ids[channel_id]

    def record(
        self,
        channel_id: int,
        message_id: int,
        *,
        deleted_at_utc: datetime,
        pointer: int,
        also_deleted: Iterable[int] = (),
    ) -> bool:
        """Remember that the event deleted `message_id`, not stored yet, at
        `deleted_at_utc`, in a report that also named `also_deleted`. The
        first report is kept. False (nothing kept) when the pointer is at
        or past it."""
        self.prune(channel_id, pointer)
        if message_id <= pointer:
            return False
        ids = self._ids.setdefault(channel_id, {})
        ids.setdefault(message_id, (deleted_at_utc, frozenset(also_deleted)))
        while len(ids) > self._limit:
            del ids[min(ids)]
        return message_id in ids

    def deletion(
        self, channel_id: int, message_id: int, *, pointer: int
    ) -> tuple[datetime, frozenset[int]] | None:
        """The report that named `message_id` deleted before the message
        was stored -- its time and the ids it named; None if none did."""
        self.prune(channel_id, pointer)
        return self._ids.get(channel_id, {}).get(message_id)

    def deleted_at(self, channel_id: int, message_id: int, *, pointer: int) -> datetime | None:
        """When the event named `message_id` deleted, if it did before
        the message was stored; None otherwise."""
        deletion = self.deletion(channel_id, message_id, pointer=pointer)
        return None if deletion is None else deletion[0]


def _stored_pointer(conn: Connection, channel_id: int) -> int:
    """The channel's pointer as committed, read inside a writer job."""
    row = conn.execute(
        "SELECT last_processed_message_id FROM channel_state WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


#: `extra=` for a log line meant for the log file only (G2L-2; defined in
#: `store.db`, which the store uses too).
LOG_FILE_ONLY = db.LOG_FILE_ONLY

#: What the window says for a stop no one gave plainer words to. Where the
#: details are is added by the window (`store.db.POINTS_TO_DETAILS`): the
#: log file, or right below when there is none.
UNEXPLAINED_STOP = "watch stopped on a problem this program did not expect."
#: What the window says when the 5-second check stopped.
_CHECK_STOPPED = (
    "The 5-second check of the channels stopped, so posts that Telegram's live feed "
    "drops would have been missed."
)


class ListenerStopped(RuntimeError):
    """The listener is not receiving Telegram updates: it stopped, or it
    never got going. Whether that happened cleanly or not, for a
    supervisor it is never a success -- `run_listener()` turns it into
    `EXIT_LISTENER_STOPPED` ("restart me").

    `plain` is what the watch window says: why watch stopped, in the
    user's words. The message itself is the technical detail, which goes
    to the log file only (G2L-2)."""

    def __init__(self, message: str, *, plain: str | None = None) -> None:
        super().__init__(message)
        self.plain = plain if plain is not None else UNEXPLAINED_STOP


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
    #: Who posted it (final review F05): `post` is a channel's own post;
    #: `sender_kind` "user" / "channel" / "chat" and `sender_id` (a user's
    #: id, or a channel's or group's in its -100... / negative form) come
    #: from `from_id`. None: not known.
    is_post: bool | None = None
    sender_kind: str | None = None
    sender_id: int | None = None
    #: Where a forward came from (F09): the channel (-100... form), its
    #: post id there, and when it was first posted. None: not a forward,
    #: or not from a channel. `posted_at_utc` stays this copy's own date.
    fwd_from_channel_id: int | None = None
    fwd_from_post_id: int | None = None
    fwd_from_date_utc: datetime | None = None


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
    `missed_by_eventstream` counter counts, for a message posted while
    this watch ran (`posted_at_utc`; final review F17).
    """

    message_id: int
    #: None when this message failed (the failure is in `handler_errors`):
    #: either the batch stopped here and the next read retries it, or it
    #: was skipped after `MAX_HISTORY_ATTEMPTS` failures.
    result: IngestResult | None
    #: The message's own date (UTC), when it was stored; None when it failed.
    posted_at_utc: datetime | None = None


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


#: Every channel's marked id is -(this + its bare Telegram id): the -100...
#: form channels.toml and `event.chat_id` use (Telethon's `get_peer_id`).
_CHANNEL_MARK = 10**12
#: The largest bare channel or chat id Telethon marks as such.
_MAX_BARE_ID = 9_999_999_999


def _positive_int(value: Any) -> int | None:
    """`value` if it is a positive int (never a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _marked_channel_id(peer: Any) -> int | None:
    """A `PeerChannel`'s id in the -100... form, or None."""
    bare = _positive_int(getattr(peer, "channel_id", None))
    if bare is None or bare > _MAX_BARE_ID:
        return None
    return -(_CHANNEL_MARK + bare)


def _sender_of(peer: Any) -> tuple[str | None, int | None]:
    """`message.from_id` as (kind, id): a user's own id, a channel's in the
    -100... form, a basic group's negated -- as Telethon marks them. None
    for anything else (an anonymous channel post has no `from_id`)."""
    if isinstance(peer, tl_types.PeerUser):
        user_id = _positive_int(peer.user_id)
        return ("user", user_id) if user_id is not None else (None, None)
    if isinstance(peer, tl_types.PeerChannel):
        marked = _marked_channel_id(peer)
        return ("channel", marked) if marked is not None else (None, None)
    if isinstance(peer, tl_types.PeerChat):
        chat_id = _positive_int(peer.chat_id)
        if chat_id is None or chat_id > _MAX_BARE_ID:
            return None, None
        return "chat", -chat_id
    return None, None


def _forward_of(header: Any) -> tuple[int | None, int | None, datetime | None]:
    """`message.fwd_from` as (channel in the -100... form, its post id
    there, the original's date in UTC). Each part that is missing or
    cannot be read is None."""
    if not isinstance(header, tl_types.MessageFwdHeader):
        return None, None, None
    from_id = getattr(header, "from_id", None)
    channel_id = (
        _marked_channel_id(from_id) if isinstance(from_id, tl_types.PeerChannel) else None
    )
    post_id = _positive_int(getattr(header, "channel_post", None))
    date = getattr(header, "date", None)
    if isinstance(date, datetime) and date.tzinfo is not None and date.utcoffset() is not None:
        date_utc: datetime | None = date.astimezone(UTC)
    else:
        date_utc = None
    return channel_id, post_id, date_utc


def normalise_message(message: Any, *, channel_id: int) -> IncomingMessage:
    """Flatten a Telethon `Message` (or the fake's stand-in). Pure
    attribute reads -- no RPC, no awaits, no clock.

    Who posted it (`post`, `from_id`) and where a forward came from
    (`fwd_from`) are read too, for the relay and group checks built on
    them (final review F05, F09). A part that is missing or cannot be read
    is None: that metadata must never keep a message from being stored."""
    text = getattr(message, "message", None)
    if text is None:
        text = getattr(message, "raw_text", None)

    reply_to = getattr(message, "reply_to_msg_id", None)
    if reply_to is None:
        reply_to = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)

    edit_date = getattr(message, "edit_date", None)

    post = getattr(message, "post", None)
    sender_kind, sender_id = _sender_of(getattr(message, "from_id", None))
    fwd_channel_id, fwd_post_id, fwd_date = _forward_of(getattr(message, "fwd_from", None))

    return IncomingMessage(
        channel_id=channel_id,
        message_id=message.id,
        text=text or "",
        posted_at_utc=_to_utc(message.date, field="date"),
        edit_date_utc=_to_utc(edit_date, field="edit_date") if edit_date is not None else None,
        reply_to_message_id=reply_to,
        is_post=post if isinstance(post, bool) else None,
        sender_kind=sender_kind,
        sender_id=sender_id,
        fwd_from_channel_id=fwd_channel_id,
        fwd_from_post_id=fwd_post_id,
        fwd_from_date_utc=fwd_date,
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
        telegram_timeout: float | None = None,
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
        # When start() began: min(the injected clock, Telegram's), whole
        # seconds. The seed cutoff start() stores for a channel that needs
        # one; see `started_at`.
        self._started_at: datetime | None = None
        # Channels already reported as having no messages at all, so the
        # report is logged once, not every round.
        self._reported_empty: set[int] = set()
        self._writer_stop_timeout = writer_stop_timeout
        self._writer_ready_timeout = writer_ready_timeout
        # The start-up deadline for connect() and get_dialogs(); None means
        # client.STARTUP_TIMEOUT_SECONDS (60 s), read when start() runs.
        self._telegram_timeout = telegram_timeout
        self._channels_by_id: dict[int, ChannelCfg] = {
            channel.channel_id: channel for channel in self.channels if channel.enabled
        }
        self._reader: Connection = None
        # What each enabled source is, from start()'s chat list (F05).
        self._source_kinds: dict[int, str] = {}
        self._handler_errors: collections.deque[HandlerError] = collections.deque(
            maxlen=MAX_RECORDED_HANDLER_ERRORS
        )
        self._handler_error_count = 0
        # (No in-memory "seeded" set: whether a channel still needs its
        # seed is the store's `channel_state`, read at every history read,
        # so a seed re-runs until it succeeds -- ledger 142.)
        # Failed history-read attempts per (channel_id, message_id); see
        # ingest_history(). Cleared on success or skip.
        self._history_attempts: dict[tuple[int, int], int] = {}
        # Ids a MessageDeleted event named before they were stored (F18);
        # touched only inside writer jobs.
        self._deleted_before_stored = DeletedBeforeStored()
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
    def started_at(self) -> datetime | None:
        """When this `watch` started, as `start()` decided it: the earlier
        of this computer's clock and Telegram's at the moment `start()`
        began, UTC, whole seconds. None before `start()`. A post dated
        after it (less `FIRST_RUN_SEED_MARGIN`) was made while this
        process ran."""
        return self._started_at

    @property
    def source_kinds(self) -> Mapping[int, str]:
        """What each switched-on source is, by channel id, as `start()` read
        it from Telegram's chat list: "channel", "group" or "unknown"
        (`client.source_kind`). Empty before `start()`."""
        return dict(self._source_kinds)

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
        tombstones = self._deleted_before_stored

        def job(conn: Connection) -> IngestResult:
            deletion = None
            if tombstones.pending(incoming.channel_id):
                # A deletion event named it before it was stored (F18):
                # stored, then that deletion applied in this same job.
                deletion = tombstones.deletion(
                    incoming.channel_id,
                    incoming.message_id,
                    pointer=_stored_pointer(conn, incoming.channel_id),
                )
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
                is_post=incoming.is_post,
                sender_kind=incoming.sender_kind,
                sender_id=incoming.sender_id,
                fwd_from_channel_id=incoming.fwd_from_channel_id,
                fwd_from_post_id=incoming.fwd_from_post_id,
                fwd_from_date_utc=incoming.fwd_from_date_utc,
                deleted_by_event_at_utc=None if deletion is None else deletion[0],
                deleted_by_event_with=() if deletion is None else deletion[1],
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
        also_deleted: Iterable[int] = (),
        reported_at_utc: datetime | None = None,
    ) -> DeletionResult:
        """Record that `message_id` is gone from its channel --
        `edits_repo.apply_deletion()`, on the writer thread. Idempotent:
        the second report of the same deletion finds it marked. Never
        closes anything, never touches the pointer. An id a
        `MessageDeleted` event names before it is stored is remembered
        (`DeletedBeforeStored`, F18): the job that stores it later applies
        this deletion at once, as this report. (A withdrawn call's
        standing repost may be promoted in the same job, which is why the
        accounts go along -- unless it is one of `also_deleted`, the
        other ids the same report named deleted.)

        `reported_at_utc`: the moment of the report that named
        `message_id` and `also_deleted` (`now_utc()` when the report was
        taken). Every id of one report is its own job, and each must be
        stamped with that one moment: posts deleted together carry the
        same deletion time, which is how `relay_repo` tells the posts of
        one report apart later (ruling R6-3). Without it, the clock is
        read now."""
        now_utc = self._now() if reported_at_utc is None else reported_at_utc
        accounts = self.accounts
        others = frozenset(also_deleted)
        tombstones = self._deleted_before_stored

        def job(conn: Connection) -> DeletionResult:
            result = edits_repo.apply_deletion(
                conn,
                channel_cfg=channel_cfg,
                message_id=message_id,
                now_utc=now_utc,
                accounts=accounts,
                detected_by=detected_by,
                also_deleted=others,
            )
            if result.outcome == "unknown" and detected_by == edits_repo.EVENT_DETECTOR:
                # Not stored yet -- perhaps a history read holds it right now
                # (F18): remembered, so the job that stores it applies this
                # deletion in the same transaction.
                channel_id = channel_cfg.channel_id
                if tombstones.record(
                    channel_id,
                    message_id,
                    deleted_at_utc=now_utc,
                    pointer=_stored_pointer(conn, channel_id),
                    also_deleted=others,
                ):
                    logger.info(
                        "channel %s message %s was deleted before it was stored; if a history "
                        "read brings it, the deletion is applied as it is stored",
                        channel_id,
                        message_id,
                    )
            return result

        return await self._run_on_writer(job)

    def now_utc(self) -> datetime:
        """The listener's clock: what every job it submits is stamped
        with -- for a caller that submits several jobs as one moment (a
        deletion report, `ingest_deletion`)."""
        return self._now()

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
        # Technical, so the log file only: the stop that follows says it
        # plainly in the window (G2L-2).
        logger.critical(
            "the writer thread is dead (%s); nothing can be stored, so the "
            "listener is stopping for its supervisor to restart it",
            exc,
            extra=LOG_FILE_ONLY,
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
            outcomes.append(
                HistoryIngest(
                    message_id=message.id,
                    result=result,
                    posted_at_utc=incoming.posted_at_utc,
                )
            )
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

        A channel that needs a seed (`channel_state.ChannelState.
        needs_seed`: never read, or switched back on) is seeded first:
        reading a never-read channel from 0 would return its *oldest*
        messages, so this call sets the pointer just below everything
        posted since its **stored** seed cutoff (`_seed_pointer`) and then
        reads on from there as usual. Whether a seed is needed is read
        from the store every time, never remembered: a seed that fails --
        at startup, in the poll, in an earlier process -- is simply run
        again by the next call, from the same stored cutoff, until it
        succeeds (ledger 142). It never reads from 0 unless the channel
        verifiably has nothing older, and a channel with no messages at
        all is not read -- nor marked seeded -- until its first post.

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
            state = channel_state.read(self.reader, channel_id)
            pointer = state.pointer
            if state.needs_seed:
                seeded = await self._seed_pointer(channel_cfg, state, rpc_timeout=rpc_timeout)
                if seeded is None:
                    return ()  # no messages at all yet: seeded by a later read
                pointer = seeded
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

    async def _seed_pointer(
        self,
        channel_cfg: ChannelCfg,
        state: channel_state.ChannelState,
        *,
        rpc_timeout: float,
    ) -> int | None:
        """Seed `channel_cfg`: set its pointer just below everything posted
        since its stored seed cutoff, and return it -- or None if the
        channel has no messages at all yet (nothing is marked; the next
        read tries again).

        The anchor is the newest message dated before `cutoff -
        FIRST_RUN_SEED_MARGIN` -- one RPC, `get_messages(limit=1,
        offset_date=...)` (Telegram's `getHistory` returns only messages
        sent before `offset_date`). One writer job then sets the pointer
        to it (never lower than it already is), marks the channel seeded
        and clears the cutoff it used (`channel_state.complete_seed`); the
        read that follows in the same `read_history()` call ingests
        everything after it.

        **Why the stored cutoff, and not "now" or this process's start.**
        Seeding at whatever is newest when the seed finally succeeds is
        only harmless at startup. If the startup seed fails (a connection
        blip, a long flood wait, a timeout), the poll seeds later -- and
        "newest" by then includes messages posted after `watch` started,
        which the update stream may well have dropped (fix round 1's
        Critical, probe 1f). The same holds across processes: a `watch`
        whose seed failed for its whole run, restarted, used to anchor at
        the NEW start and skip everything the first run missed or that
        was posted while it was stopped (final review F01). So the cutoff
        is the start of the FIRST attempt, stored before any seed request
        (`start()`; `channel_state.claim_seed_cutoff`) and used by every
        attempt after it. Messages at or after it are ingested by the
        read that follows (`source="poll"`, so they count as missed, or
        `"backfill"`). A channel switched back on (F28) carries the cutoff
        `channels on` wrote: posts made while it was off are not read.

        The cutoff is in whole seconds, and so is `offset_date` (ledger
        141): Telethon sends it as whole seconds, and a fraction here made
        the anchor query and the guard below disagree about a message
        dated in that last second -- the guard then refused the channel
        for ever. The margin absorbs what skew is left between the start
        time and Telegram's message dates (`start()` already takes the
        earlier of this computer's clock and Telegram's).

        No anchor -- everything in the channel is after the cutoff -- is a
        seed at 0, and the read goes from 0. That is safe only if the
        channel really has nothing older, so it is verified: the oldest
        message is fetched, and if *it* predates the cutoff the anchor
        query was wrong and this raises `RuntimeError` rather than ingest
        the channel's ancient history (the reviewer's D3); the next read
        retries. If the oldest query finds nothing either, the channel is
        empty -- or Telegram answered nothing both times -- and nothing is
        marked: a single empty answer used to mark the channel seeded at 0
        and let the next read ingest 400-day-old history (ledger 142). The
        cutoff stays stored, so the channel's first post is read whenever
        it comes, even across a restart (ledger 114/144).
        """
        channel_id = channel_cfg.channel_id
        if self._started_at is None:
            raise RuntimeError("a seed needs the start time Listener.start() records")
        cutoff, cutoff_raw = state.seed_cutoff_utc, state.seed_cutoff_raw
        if cutoff is None:
            # None stored yet (start()'s job could not write it) or one
            # that cannot be read: store it now, before any seed request.
            decision = await self._claim_seed_cutoff(channel_cfg)
            if decision is None:  # the channel needs no seed after all
                return channel_state.read(self.reader, channel_id).pointer
            cutoff, cutoff_raw = decision.cutoff_utc, decision.cutoff_raw
        offset = (cutoff - FIRST_RUN_SEED_MARGIN).replace(microsecond=0)
        before_cutoff = await asyncio.wait_for(
            self.client.get_messages(channel_id, limit=1, offset_date=offset), rpc_timeout
        )
        anchor_id = max((message.id for message in before_cutoff), default=0)
        if not anchor_id:
            oldest = await asyncio.wait_for(
                self.client.get_messages(channel_id, limit=1, reverse=True), rpc_timeout
            )
            if not oldest:
                if channel_id not in self._reported_empty:
                    self._reported_empty.add(channel_id)
                    logger.info(
                        "Channel %s (%s) has no messages yet. Its first message will be "
                        "read when it is posted.",
                        channel_id,
                        channel_cfg.name,
                    )
                return None
            if any(message.date < offset for message in oldest):
                raise RuntimeError(
                    f"channel {channel_id} ({channel_cfg.name}): Telegram reported no "
                    f"message before {offset.isoformat()}, yet its oldest message "
                    f"({oldest[0].id}) is older; refusing to read its history from 0"
                )
        seeded_at = self._now()

        def seed(conn: Connection) -> int:
            return channel_state.complete_seed(
                conn,
                channel_id=channel_id,
                anchor_id=anchor_id,
                cutoff_raw=cutoff_raw,
                seeded_at_utc=seeded_at,
            )

        pointer = await self._run_on_writer(seed)
        self._reported_empty.discard(channel_id)
        when = offset.strftime("%Y-%m-%d %H:%M:%S")
        if anchor_id:
            logger.warning(
                "Channel %s (%s): reading starts after message %s, the last one posted "
                "before %s UTC. Everything posted after it is read; nothing before it is.",
                channel_id,
                channel_cfg.name,
                anchor_id,
                when,
            )
        else:
            logger.warning(
                "Channel %s (%s): nothing in it was posted before %s UTC, so it is read "
                "from its first message.",
                channel_id,
                channel_cfg.name,
                when,
            )
        return pointer

    async def _claim_seed_cutoff(
        self, channel_cfg: ChannelCfg
    ) -> channel_state.CutoffDecision | None:
        """Store this start as `channel_cfg`'s seed cutoff unless one is
        already stored (`channel_state.claim_seed_cutoff`), in one writer
        job, and report what happened. Raises whatever the job raised: a
        seed must not run without a stored cutoff."""
        started_at = self._started_at
        channel_id = channel_cfg.channel_id

        def claim(conn: Connection) -> channel_state.CutoffDecision | None:
            return channel_state.claim_seed_cutoff(
                conn, channel_id=channel_id, started_at_utc=started_at
            )

        decision = await self._run_on_writer(claim)
        if decision is not None:
            self._report_seed_cutoff(channel_cfg, decision)
        return decision

    async def _store_seed_cutoffs(self) -> None:
        """Startup, before any seed request: one writer job stores this
        start as the seed cutoff of every enabled channel that needs a seed
        and has none yet (`channel_state.claim_seed_cutoff`: the first
        attempt's stays). If the job fails -- another program holding the
        database, say -- each channel's own read stores it before seeding
        (`_seed_pointer`), so no seed ever runs without one."""
        started_at = self._started_at
        channels = self.enabled_channels

        def claim_all(
            conn: Connection,
        ) -> tuple[tuple[ChannelCfg, channel_state.CutoffDecision | None], ...]:
            return tuple(
                (
                    channel,
                    channel_state.claim_seed_cutoff(
                        conn, channel_id=channel.channel_id, started_at_utc=started_at
                    ),
                )
                for channel in channels
            )

        try:
            decisions = await self._run_on_writer(claim_all)
        except Exception as exc:  # noqa: BLE001 -- each channel's read stores its own
            logger.warning(
                "Could not save where the reading of new channels starts (%s: %s). It is "
                "saved again before each such channel is first read, and nothing is read "
                "from it until then.",
                type(exc).__name__,
                exc,
            )
            return
        for channel, decision in decisions:
            if decision is not None:
                self._report_seed_cutoff(channel, decision)

    def _report_seed_cutoff(
        self, channel_cfg: ChannelCfg, decision: channel_state.CutoffDecision
    ) -> None:
        """Log what `claim_seed_cutoff` did, in plain words: a cutoff an
        earlier start stored more than `OLD_SEED_CUTOFF_WARNING` ago, or
        one replaced, is a WARNING naming its age (ruling C1)."""
        started_at = self._started_at
        channel = f"Channel {channel_cfg.channel_id} ({channel_cfg.name})"
        cutoff_text = decision.cutoff_utc.strftime("%Y-%m-%d %H:%M:%S")
        switched_on = decision.pointer > 0
        if decision.action == "set":
            logger.info(
                "%s is read for the first time: everything posted from about a minute "
                "before %s UTC on is read.",
                channel,
                cutoff_text,
            )
            return
        previous = decision.previous_utc
        if decision.action == "kept":
            age = started_at - decision.cutoff_utc if started_at is not None else timedelta(0)
            level = logging.WARNING if age > OLD_SEED_CUTOFF_WARNING else logging.INFO
            if switched_on:
                logger.log(
                    level,
                    "%s was switched back on at %s UTC, %s ago: it is read from about a "
                    "minute before then. Posts made while it was off are not read.",
                    channel,
                    cutoff_text,
                    _describe_age(age),
                )
            else:
                logger.log(
                    level,
                    "%s: an earlier start of watch did not finish its first read of this "
                    "channel, which begins at about a minute before %s UTC, %s ago. It is "
                    "read from there now, so nothing posted since then is skipped.",
                    channel,
                    cutoff_text,
                    _describe_age(age),
                )
            return
        # "replaced"
        if previous is None:
            logger.warning(
                "%s: the saved start of its reading could not be understood, so it is "
                "read from this start of watch (%s UTC) instead.",
                channel,
                cutoff_text,
            )
        elif previous > decision.cutoff_utc:
            logger.warning(
                "%s: the saved start of its reading, %s UTC, was later than this start of "
                "watch (a clock may have run fast), so it is read from %s UTC instead.",
                channel,
                previous.strftime("%Y-%m-%d %H:%M:%S"),
                cutoff_text,
            )
        else:
            logger.warning(
                "%s: its reading was due to start at %s UTC, %s ago -- too long ago for "
                "anything posted then to be traded -- so it is read from this start of "
                "watch (%s UTC) instead. Posts made in those %s are not read.",
                channel,
                previous.strftime("%Y-%m-%d %H:%M:%S"),
                _describe_age(decision.cutoff_utc - previous),
                cutoff_text,
                _describe_age(decision.cutoff_utc - previous),
            )

    # -- startup ------------------------------------------------------------

    async def start(self) -> None:
        """Steps 0-5 of the startup order. See the module docstring --
        the order is the design, not an implementation detail."""
        if self._started:
            raise RuntimeError("Listener.start() called twice")
        self._started = True
        # Before anything else, so before handlers are registered: the
        # first-run seed keeps everything posted after this instant (or
        # Telegram's clock's reading of it, if earlier -- see below).
        local_start = self._now()
        if local_start.tzinfo is None or local_start.utcoffset() is None:
            raise ValueError(f"the injected clock returned a naive datetime: {local_start!r}")
        local_start = local_start.astimezone(UTC)
        self._writer_death = asyncio.get_running_loop().create_future()

        await self._start_writer_and_wait_for_connection()  # 0
        self._reader = db.connect(self.db_path, role="reader")
        await self._skip_entries_of_channels_off()

        # Imported here for the same reason as in run_listener().
        from xaucopier.ingest import client as client_module

        timeout = (
            self._telegram_timeout
            if self._telegram_timeout is not None
            else client_module.STARTUP_TIMEOUT_SECONDS
        )
        try:
            await client_module.connect_within(self.client, timeout)  # 1
            telegram_clock = await self._read_telegram_clock()
            dialogs = await client_module.telegram_within(self.client.get_dialogs(), timeout)  # 2
        except client_module.TelegramUnreachableError as exc:
            message = (
                f"{exc} watch stops now (exit code 4); start it again once this "
                "computer is back online."
            )
            raise ListenerStartupError(message, plain=message) from exc
        self._name_sources(dialogs)
        self._started_at = self._decide_start(local_start, telegram_clock)
        await self._store_seed_cutoffs()

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
                f"{self._writer_ready_timeout:g}s; refusing to listen without it",
                plain=(
                    f"Could not open the database {self.db_path} within "
                    f"{self._writer_ready_timeout:g} seconds, so nothing could be recorded."
                ),
            ) from exc
        except Exception as exc:  # noqa: BLE001 -- any probe failure means no writer
            raise ListenerStartupError(
                f"the writer thread could not connect to {self.db_path}: "
                f"{type(exc).__name__}: {exc}",
                plain=(
                    f"Could not open the database {self.db_path} ({type(exc).__name__}), "
                    "so nothing could be recorded."
                ),
            ) from exc

    async def _skip_entries_of_channels_off(self) -> None:
        """Startup, before Telegram is contacted (final review F15): one
        writer job SKIPs every trade entry still QUEUED for a signal whose
        channel is not switched on (`outbox.skip_entries_of_channels_off`,
        reason 'channel switched off') and, if there were any, records one
        WARNING alert, `CHANNEL_OFF_ENTRIES_SKIPPED`, naming how many.

        OFF means the channel is not read at all -- neither its new posts
        nor the edits, deletions and follow-ups of the signals it already
        posted -- so nothing would see its poster withdraw or re-aim such a
        signal before it was traded. A job that fails is logged and left
        for the next start: it must not keep watch from starting."""
        enabled_ids = tuple(self._channels_by_id)
        names = {channel.channel_id: channel.name for channel in self.channels}
        now_utc = self._now()

        def job(conn: Connection) -> int:
            skipped = outbox.skip_entries_of_channels_off(conn, enabled_channel_ids=enabled_ids)
            total = sum(skipped.values())
            if total:
                where = "; ".join(
                    f"{names[channel_id]} ({channel_id}): {count}"
                    if channel_id in names
                    else f"channel {channel_id}, no longer listed: {count}"
                    for channel_id, count in sorted(skipped.items())
                )
                alerts.record(
                    conn,
                    now_utc=now_utc,
                    level="WARNING",
                    code=alerts.CHANNEL_OFF_ENTRIES_SKIPPED,
                    detail=(
                        f"{total} waiting trade {'entry' if total == 1 else 'entries'} "
                        f"from channels that are switched off "
                        f"{'was' if total == 1 else 'were'} cancelled (reason "
                        f"'{outbox.CHANNEL_OFF_REASON}') -- {where}. watch no longer "
                        "reads those channels, so it could not see their posters "
                        "withdraw, change or close those signals."
                    ),
                )
            return total

        try:
            await self._run_on_writer(job)
        except Exception as exc:  # noqa: BLE001 -- retried at the next start
            logger.warning(
                "Could not cancel the trade entries still waiting for channels that are "
                "switched off (%s: %s); watch tries again when it next starts.",
                type(exc).__name__,
                exc,
            )

    def _name_sources(self, dialogs: Any) -> None:
        """Say what each switched-on source is (final review F05), from the
        chat list `start()` already has -- no further call to Telegram.

        A supergroup passes every check `channels add` and
        `resolve-channels` make, and nothing filters who posted, so once
        one is switched on any member's "close all now" is read as an
        instruction. watch does not refuse it -- the user may mean it --
        but a group is a plain WARNING at every start, as is a source this
        account's chat list does not hold (not joined, or left: watch
        cannot tell what it is, and its posts are not sent to watch as
        they arrive). A chat-list entry that cannot be read is passed
        over: naming the sources must never stop watch."""
        # Imported here for the same reason as in run_listener().
        from xaucopier.ingest.client import source_kind

        listed: dict[int, Any] = {}
        for dialog in dialogs or ():
            entity = getattr(dialog, "entity", None)
            try:
                peer_id = tl_utils.get_peer_id(entity)
            except (TypeError, ValueError):
                continue
            listed.setdefault(peer_id, entity)

        for channel in self.enabled_channels:
            where = f"'{channel.name}' (number {channel.ordinal}, id {channel.channel_id})"
            if channel.channel_id not in listed:
                kind = "unknown"
                logger.warning(
                    "%s is not in this Telegram account's list of chats, so watch cannot "
                    "tell whether it is a channel or a group, and its new posts are only "
                    "found by watch's check every few seconds, not as they arrive. Join it "
                    "in Telegram with this account, or check its id in "
                    "`.\\xaucopier channels`.",
                    where,
                )
            else:
                kind = source_kind(listed[channel.channel_id])
                if kind == "channel":
                    logger.info("%s is a channel: only its admins can post.", where)
                elif kind == "group":
                    logger.warning(
                        "%s is a group, not a channel: anyone in it can post; their "
                        "messages are read as instructions. If you meant to follow a "
                        "channel, switch this one off with `.\\xaucopier channels off %d` "
                        "and add the channel instead.",
                        where,
                        channel.ordinal,
                    )
                else:
                    logger.warning(
                        "%s is neither a channel nor a group in Telegram's list of chats, "
                        "so watch cannot tell who can post in it. Check its id in "
                        "`.\\xaucopier channels`.",
                        where,
                    )
            self._source_kinds[channel.channel_id] = kind

    async def _read_telegram_clock(self) -> tuple[datetime, datetime] | None:
        """Ask Telegram for its time: one `updates.GetStateRequest()`,
        bounded by `TELEGRAM_CLOCK_TIMEOUT_SECONDS`. Returns Telegram's
        time (whole seconds, as it reports it) and this computer's clock
        when the answer came -- or None, with a plain WARNING, if there
        was no usable answer: watch then starts on this computer's clock,
        never refuses to start. A dead session or a rejected api_id is
        not "no answer", and is raised as itself (exit 3 or 5)."""
        # Imported here for the same reason as in run_listener().
        from xaucopier.ingest import client as client_module

        try:
            state = await client_module.telegram_within(
                self.client(functions.updates.GetStateRequest()),
                TELEGRAM_CLOCK_TIMEOUT_SECONDS,
            )
            telegram_now = state.date
            if (
                not isinstance(telegram_now, datetime)
                or telegram_now.tzinfo is None
                or telegram_now.utcoffset() is None
            ):
                raise ValueError(f"Telegram's answer carried no usable time: {telegram_now!r}")
        except client_module.PERMANENT_TELEGRAM_ERRORS:
            raise
        except Exception as exc:  # noqa: BLE001 -- no answer: this computer's clock, with a warning
            reason = (
                f"no answer within {TELEGRAM_CLOCK_TIMEOUT_SECONDS:g} seconds"
                if isinstance(exc, client_module.TelegramUnreachableError)
                else type(exc).__name__
            )
            logger.warning(
                "Could not read Telegram's clock (%s), so watch goes by this computer's "
                "clock. If this computer's clock runs fast, a message posted around this "
                "start could be missed: in Windows Settings > Time & language > Date & "
                "time, switch on 'Set time automatically'.",
                reason,
            )
            return None
        return telegram_now.astimezone(UTC), self._now().astimezone(UTC)

    def _decide_start(
        self, local_start: datetime, telegram_clock: tuple[datetime, datetime] | None
    ) -> datetime:
        """This watch's start: min(this computer's clock, Telegram's) at the
        moment `start()` began, in whole seconds (ledger 143).

        Telegram's answer came later than that moment, so its time is
        taken back by how long this computer's clock says passed since
        (all of it, up to the answer -- which errs early). The earlier of
        the two is the safe one: a start too early only reads a little
        more; a start too late -- a computer clock running fast -- made a
        late seed skip posts the update stream dropped. A difference over
        `CLOCK_DIFFERENCE_WARNING` is a WARNING in plain words, never a
        refusal: Telegram is the one that dates the posts."""
        start = local_start
        if telegram_clock is not None:
            telegram_now, answered_at = telegram_clock
            elapsed = max(answered_at - local_start, timedelta(0))
            telegram_start = telegram_now - elapsed
            difference = local_start - telegram_start
            if abs(difference) > CLOCK_DIFFERENCE_WARNING:
                logger.warning(
                    "This computer's clock is %d seconds %s Telegram's. watch goes by the "
                    "earlier of the two, so nothing is skipped, but please fix the clock: "
                    "in Windows Settings > Time & language > Date & time, switch on 'Set "
                    "time automatically'.",
                    int(abs(difference).total_seconds()),
                    "ahead of" if difference > timedelta(0) else "behind",
                )
            start = min(local_start, telegram_start)
        return start.replace(microsecond=0)

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
        reported_at = self._now()  # one report, one moment (ruling R6-3)
        for message_id in deletion.message_ids:
            try:
                await self.ingest_deletion(  # 3. submit
                    channel_cfg,
                    message_id,
                    detected_by=edits_repo.EVENT_DETECTOR,
                    also_deleted=deletion.message_ids,
                    reported_at_utc=reported_at,
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
                "listening is never a successful exit.",
                plain="The connection to Telegram ended, so new posts could no longer arrive.",
            )

        if self._writer_death is not None and self._writer_death in done:
            death = self._writer_death.result()
            raise ListenerStopped(
                f"the writer thread died ({death}): nothing can be stored, so the "
                "listener is stopping rather than running without a writer",
                plain="Saving to the database stopped working, so nothing more could be recorded.",
            ) from death

        # Neither of the others, so the poll task finished first.
        poll_task = self.poll_task
        if poll_task is None:  # pragma: no cover -- unreachable, see above
            raise ListenerStopped("the listener stopped for an unknown reason")
        if poll_task.cancelled():
            raise ListenerStopped(
                "the poll task was cancelled: nothing is recovering messages "
                "the update stream drops",
                plain=_CHECK_STOPPED,
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
                "nothing is recovering messages the update stream drops",
                # An error that says itself plainly (WriterUnresponsiveError)
                # says it; any other is named by its type.
                plain=getattr(poll_error, "plain", None)
                or (
                    f"The 5-second check of the channels failed ({type(poll_error).__name__}), "
                    "so posts that Telegram's live feed drops would have been missed."
                ),
            ) from poll_error
        raise ListenerStopped(
            "the poll task returned: nothing is recovering messages the update "
            "stream drops",
            plain=_CHECK_STOPPED,
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


def _describe_age(age: timedelta) -> str:
    """A duration in plain words for a log line: minutes below two hours,
    hours from there."""
    seconds = max(int(age.total_seconds()), 0)
    if seconds < 2 * 3600:
        return f"{seconds // 60} minutes"
    return f"{seconds // 3600} hours"


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
        # The window reads the plain sentence; the log file keeps the
        # technical detail too (G2L-2).
        logger.error(
            "%s", exc.plain, extra=db.POINTS_TO_DETAILS if exc.plain == UNEXPLAINED_STOP else None
        )
        if str(exc) != exc.plain:
            logger.error("%s", exc, extra=LOG_FILE_ONLY)
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
