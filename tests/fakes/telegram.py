"""A fake Telethon client: the substrate every ingest test runs on.

Tasks 8 (listener), 9 (history poll), 10 (edits/deletes) and 11 (the
`watch` CLI) all test against this object, and none of them may touch
the network. It reproduces the handful of `TelegramClient` behaviours
the listener actually depends on -- and, more importantly, the two
*failure* behaviours that the design exists to survive:

**The update blackout.** Telegram can stop delivering channel updates to
a connected client for a window of time (the documented Telethon channel
"blackout": the connection is healthy, the client believes it is caught
up, and new posts simply never arrive as updates). The posts are still
in the channel, so `get_messages()` history returns them. That gap --
delivered-as-update vs merely present-in-history -- is the entire reason
Task 9's periodic poll exists, so the fake models it directly:
`start_blackout()` / `end_blackout()` / `blackout()` suppress delivery to
handlers while leaving history untouched.

**A dropped socket.** `drop_connection()` / `reconnect()` model a
transient network drop that Telethon recovers from on its own: messages
sent meanwhile never reach handlers, `run_until_disconnected()` does
*not* return, and history still has everything. That is deliberately
distinct from `disconnect()`, which is terminal and *does* make
`run_until_disconnected()` return -- the case the listener must turn
into a non-zero exit for its supervisor.

**Honesty about provenance.** Every message the client hands out is
recorded in `deliveries` with the route that produced it -- `"event"`
for the update stream, `"history"` for `get_messages()`. A test can
therefore prove *which mechanism* recovered a message, not merely that
the row eventually appeared. (The listener's own `messages.source`
column -- `live` / `backfill` / `poll` -- is the durable counterpart of
the same distinction.)

**One deliberate departure from Telethon:** an exception raised by a
registered handler propagates out of `emit()` instead of being swallowed
and logged. Real Telethon catches callback exceptions; if the fake did
too, the listener's "a handler exception must not kill the client"
guarantee would be untestable, because every test would pass whether the
listener handled the exception or not. Everything else matches Telethon:
dispatch carries on to the remaining handlers first (Telethon's
`_dispatch_update` catches per callback and moves on), and only then is
the first exception re-raised. It is also appended to
`handler_exceptions`, and the `"event"` delivery is recorded either way
-- the stream did deliver it.

**RPCs are logged, not missing.** Every method that is a network round
trip in Telethon and that a handler might be tempted to call --
`get_messages`, `get_entity`, `get_input_entity` on the client;
`get_reply_message`, `get_chat`, `get_sender` on the event -- exists
here and appends to `calls`. A missing method would make such a call
fail with `AttributeError`, which a `try/except: pass` swallows without
a trace; a logged one cannot hide.

**`get_messages()` defaults to ONE message, as Telethon's does** --
see its docstring. Omitting `limit` is a production bug the fake must
reproduce, not paper over.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal

from telethon import events

DeliveryRoute = Literal["event", "history"]

#: `get_messages()` keyword arguments the fake implements. Every other
#: Telethon keyword (`offset_id`, `ids`, `search`, `filter`, ...) changes
#: what Telethon returns, so the fake refuses it rather than silently
#: returning something production never would.
_SUPPORTED_GET_MESSAGES_KWARGS = frozenset(
    {"entity", "limit", "min_id", "max_id", "reverse", "wait_time"}
)


@dataclass(frozen=True, slots=True)
class FakeMessage:
    """The subset of `telethon.tl.custom.Message` the listener reads.

    Field names match Telethon's exactly (`id`, `message`, `date`,
    `edit_date`, `reply_to_msg_id`) so a listener written against this
    object is written against the real one. `date`/`edit_date` must be
    timezone-aware, as Telethon's (UTC) always are; a naive one raises
    `ValueError`, because a test that builds one is testing a message
    production can never produce.
    """

    id: int
    message: str
    date: datetime
    edit_date: datetime | None = None
    reply_to_msg_id: int | None = None

    def __post_init__(self) -> None:
        for name in ("date", "edit_date"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(
                    f"FakeMessage.{name} must be timezone-aware, as Telethon's "
                    f"always is; got naive {value!r}"
                )

    @property
    def text(self) -> str:
        return self.message

    @property
    def raw_text(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class FakeNewMessageEvent:
    """Stand-in for `events.NewMessage.Event`.

    Telethon's event proxies unknown attribute reads to the underlying
    message; `__getattr__` below does the same, so `event.text`,
    `event.id` and `event.date` work exactly as they do in production.

    `client` is the client that delivered it, as on Telethon's events;
    the RPC-shaped methods below log to its `calls`.
    """

    chat_id: int
    message: FakeMessage
    client: FakeTelegramClient | None = field(default=None, repr=False, compare=False)

    async def get_reply_message(self) -> FakeMessage | None:
        """An RPC in Telethon (it fetches the replied-to message).
        Logged, then answered from the fake's history."""
        self._log_rpc("get_reply_message")
        reply_to = self.message.reply_to_msg_id
        if reply_to is None or self.client is None:
            return None
        return next(
            (m for m in self.client.history.get(self.chat_id, []) if m.id == reply_to),
            None,
        )

    async def get_chat(self) -> Any:
        """Can be an RPC in Telethon. Logged."""
        self._log_rpc("get_chat")
        return None

    async def get_sender(self) -> Any:
        """Can be an RPC in Telethon. Logged."""
        self._log_rpc("get_sender")
        return None

    def _log_rpc(self, name: str) -> None:
        if self.client is not None:
            self.client.calls.append(name)

    def __getattr__(self, name: str) -> Any:
        try:
            return getattr(self.message, name)
        except AttributeError:
            raise AttributeError(name) from None


@dataclass(frozen=True, slots=True)
class Delivery:
    """One message handed to the caller, and by which route."""

    channel_id: int
    message_id: int
    route: DeliveryRoute


@dataclass(frozen=True, slots=True)
class GetMessagesCall:
    entity: Any
    min_id: int
    max_id: int
    limit: int | None
    reverse: bool


@dataclass(frozen=True, slots=True)
class RegisteredHandler:
    callback: Callable[[Any], Any]
    event_filter: Any


@dataclass
class FakeTelegramClient:
    """A scriptable, offline stand-in for `telethon.TelegramClient`."""

    history: dict[int, list[FakeMessage]] = field(default_factory=dict)

    #: Ordered log of lifecycle calls -- `connect`, `get_dialogs`,
    #: `add_event_handler`, ... Tests assert on the *order* of these
    #: (get_dialogs must precede handler registration).
    calls: list[str] = field(default_factory=list)
    handlers: list[RegisteredHandler] = field(default_factory=list)
    get_messages_calls: list[GetMessagesCall] = field(default_factory=list)
    deliveries: list[Delivery] = field(default_factory=list)
    handler_exceptions: list[BaseException] = field(default_factory=list)
    get_dialogs_call_count: int = 0

    _connected: bool = False
    _blackout: bool = False
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        # Normalise whatever was passed in: sorted by id, and owned by
        # this instance rather than aliasing the caller's list.
        self.history = {
            channel_id: sorted(messages, key=lambda m: m.id)
            for channel_id, messages in self.history.items()
        }

    # -- TelegramClient surface ------------------------------------------

    async def connect(self) -> None:
        self.calls.append("connect")
        self._connected = True
        self._stop_event.clear()

    def is_connected(self) -> bool:
        return self._connected

    async def get_dialogs(self, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
        """Priming call: in production this is what populates the
        per-channel `pts` state Telethon's update engine needs. Here it
        only records that it happened, and when relative to everything
        else."""
        self.calls.append("get_dialogs")
        self.get_dialogs_call_count += 1
        return ()

    def add_event_handler(
        self, callback: Callable[[Any], Any], event: Any = None
    ) -> None:
        self.calls.append("add_event_handler")
        self.handlers.append(RegisteredHandler(callback=callback, event_filter=event))

    def remove_event_handler(
        self, callback: Callable[[Any], Any], event: Any = None
    ) -> None:
        self.calls.append("remove_event_handler")
        self.handlers = [
            handler
            for handler in self.handlers
            if not (
                handler.callback is callback
                and (event is None or handler.event_filter is event)
            )
        ]

    async def get_messages(self, *args: Any, **kwargs: Any) -> list[FakeMessage]:
        """Scripted history. `min_id` is exclusive and `max_id` is
        exclusive, matching Telethon; `reverse=True` returns oldest
        first, otherwise newest first.

        **The default `limit` is Telethon's, exactly: ONE message.**
        Telethon 1.44.0 `get_messages()` (telethon/client/messages.py
        :590-594) sets `limit=1` unless `limit` is given (by keyword or
        positionally), or both `min_id` and `max_id` are given by
        keyword, in which case it returns the whole range. The block
        below is that code, verbatim. So
        `get_messages(channel, min_id=pointer, reverse=True)` returns one
        message here exactly as it does in production -- a poll written
        that way is caught by its tests instead of fetching one message
        per interval in production while every test passes.
        `tests/test_fakes_telegram.py` pins this against the installed
        Telethon's own `get_messages` so the two cannot drift.

        History is *not* affected by a blackout or a dropped socket --
        that asymmetry is the whole point of the fake.
        """
        # telethon/client/messages.py:590-594 (Telethon 1.44.0), verbatim:
        if len(args) == 1 and 'limit' not in kwargs:
            if 'min_id' in kwargs and 'max_id' in kwargs:
                kwargs['limit'] = None
            else:
                kwargs['limit'] = 1

        unsupported = set(kwargs) - _SUPPORTED_GET_MESSAGES_KWARGS
        if unsupported:
            raise NotImplementedError(
                f"FakeTelegramClient.get_messages() does not implement "
                f"{sorted(unsupported)}; add it to the fake before relying on it"
            )
        return self._read_history(*args, **kwargs)

    def _read_history(
        self,
        entity: Any,
        limit: int | None = None,
        *,
        min_id: int = 0,
        max_id: int = 0,
        reverse: bool = False,
        wait_time: float | None = None,
    ) -> list[FakeMessage]:
        """`iter_messages()`'s signature and defaults (`limit=None` means
        everything), applied to the scripted history."""
        self.calls.append("get_messages")
        self.get_messages_calls.append(
            GetMessagesCall(
                entity=entity,
                min_id=min_id,
                max_id=max_id,
                limit=limit,
                reverse=reverse,
            )
        )

        channel_id = self._channel_id_of(entity)
        messages = [
            message
            for message in self.history.get(channel_id, [])
            if message.id > min_id and (max_id == 0 or message.id < max_id)
        ]
        messages.sort(key=lambda m: m.id, reverse=not reverse)
        if limit is not None:
            messages = messages[:limit]

        for message in messages:
            self.deliveries.append(
                Delivery(channel_id=channel_id, message_id=message.id, route="history")
            )
        return messages

    # -- RPC-shaped methods ------------------------------------------------
    # Network round trips in Telethon. They exist so that calling one is
    # *logged* rather than an AttributeError a `try/except` could hide.

    async def get_entity(self, entity: Any) -> Any:
        self.calls.append("get_entity")
        return entity

    async def get_input_entity(self, peer: Any) -> Any:
        self.calls.append("get_input_entity")
        return peer

    async def run_until_disconnected(self) -> None:
        """Blocks until `disconnect()` (terminal). A `drop_connection()`
        deliberately does not end it: Telethon reconnects on its own."""
        self.calls.append("run_until_disconnected")
        await self._stop_event.wait()

    async def disconnect(self) -> None:
        """Terminal: `run_until_disconnected()` returns."""
        self.calls.append("disconnect")
        self._connected = False
        self._stop_event.set()

    # -- test drivers ------------------------------------------------------

    async def emit(
        self,
        channel_id: int,
        message: FakeMessage,
        *,
        add_to_history: bool = True,
    ) -> bool:
        """Post `message` to `channel_id`.

        The message always lands in history (it really was posted to the
        channel). Whether it reaches the registered update handlers
        depends on the client's state: not while disconnected, and not
        during a blackout. Returns True iff it was delivered as an
        update.
        """
        if add_to_history:
            self.add_to_history(channel_id, message)

        if not self._connected or self._blackout:
            return False

        event = FakeNewMessageEvent(chat_id=channel_id, message=message, client=self)
        delivered, errors = await self._dispatch_collecting(
            event, filter_type=events.NewMessage
        )
        if delivered:
            # Recorded even if a handler raised: the stream did deliver it.
            self.deliveries.append(
                Delivery(channel_id=channel_id, message_id=message.id, route="event")
            )
        if errors:
            raise errors[0]
        return delivered

    async def dispatch(self, event: Any, *, filter_type: type) -> bool:
        """Hand `event` to every handler registered with a filter of
        `filter_type` (or with no filter at all). Returns True if at
        least one handler was invoked.

        A handler that raises does not stop dispatch -- the remaining
        handlers still run, as in Telethon -- and the first exception is
        then re-raised (see the module docstring).

        Generic on purpose: Task 10 emits `MessageEdited` /
        `MessageDeleted` events through this same door without the fake
        needing to know anything about them.
        """
        invoked, errors = await self._dispatch_collecting(event, filter_type=filter_type)
        if errors:
            raise errors[0]
        return invoked

    async def _dispatch_collecting(
        self, event: Any, *, filter_type: type
    ) -> tuple[bool, list[Exception]]:
        channel_id = getattr(event, "chat_id", None)
        invoked = False
        errors: list[Exception] = []
        for handler in list(self.handlers):
            if not self._filter_matches(handler.event_filter, filter_type, channel_id):
                continue
            invoked = True
            try:
                await handler.callback(event)
            except Exception as exc:  # noqa: BLE001 -- recorded, re-raised after dispatch
                self.handler_exceptions.append(exc)
                errors.append(exc)
        return invoked, errors

    def add_to_history(self, channel_id: int, message: FakeMessage) -> None:
        """Put `message` in the channel's history without any update
        delivery at all -- a message that only ever existed as history,
        e.g. one posted while the listener was down."""
        messages = self.history.setdefault(channel_id, [])
        messages = [existing for existing in messages if existing.id != message.id]
        messages.append(message)
        messages.sort(key=lambda m: m.id)
        self.history[channel_id] = messages

    def start_blackout(self) -> None:
        """Begin an update blackout: emitted messages stop reaching
        handlers but still appear in `get_messages()`."""
        self._blackout = True

    def end_blackout(self) -> None:
        self._blackout = False

    @property
    def in_blackout(self) -> bool:
        return self._blackout

    @contextlib.contextmanager
    def blackout(self) -> Iterator[None]:
        self.start_blackout()
        try:
            yield
        finally:
            self.end_blackout()

    async def drop_connection(self) -> None:
        """A transient socket drop. Delivery stops, history does not
        change, and `run_until_disconnected()` keeps waiting."""
        self.calls.append("drop_connection")
        self._connected = False

    async def reconnect(self) -> None:
        self.calls.append("reconnect")
        self._connected = True

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _channel_id_of(entity: Any) -> int:
        if isinstance(entity, int):
            return entity
        channel_id = getattr(entity, "channel_id", None)
        if isinstance(channel_id, int):
            return channel_id
        raise TypeError(
            f"FakeTelegramClient only understands integer channel ids, got {entity!r}"
        )

    @staticmethod
    def _filter_matches(event_filter: Any, filter_type: type, channel_id: int | None) -> bool:
        if event_filter is not None and not isinstance(event_filter, filter_type):
            return False

        # Simplified `chats=` matching: integer ids only, no username
        # resolution (which in production would be an RPC). The listener
        # registers without `chats=` precisely to avoid that round trip.
        chats = getattr(event_filter, "chats", None)
        if chats is None or channel_id is None:
            return True
        return channel_id in set(chats)
