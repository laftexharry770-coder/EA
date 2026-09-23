"""Tests for the fake Telegram client at `tests/fakes/telegram.py`.

The fake is not incidental scaffolding: Tasks 9 (history poll), 10
(edits/deletes) and 11 (the `watch` CLI) all test against it, so its own
behaviour needs pinning down before anything is built on top of it. In
particular the *blackout* window -- messages that never reach the update
handlers but do appear in `get_messages()` history -- is the documented
Telethon channel-blackout behaviour that Task 9's poll exists to catch;
if the fake got that wrong, Task 9's poll would be tested against a
fiction.

No network anywhere: `telethon` is imported only for the
`events.NewMessage` filter object, which is constructed offline.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime

import pytest
from telethon import events
from telethon.client.messages import MessageMethods

from fakes.telegram import FakeMessage, FakeTelegramClient

CHANNEL = -1001111111111
OTHER_CHANNEL = -1002222222222
POSTED = datetime(2026, 9, 5, 10, 45, tzinfo=UTC)


def _msg(message_id: int, text: str = "hello") -> FakeMessage:
    return FakeMessage(id=message_id, message=text, date=POSTED)


def _collector() -> tuple[list, object]:
    seen: list = []

    async def handler(event) -> None:
        seen.append(event)

    return seen, handler


# -- delivery ----------------------------------------------------------------


def test_emit_delivers_to_a_registered_new_message_handler() -> None:
    async def scenario():
        client = FakeTelegramClient()
        seen, handler = _collector()
        client.add_event_handler(handler, events.NewMessage())
        await client.connect()
        delivered = await client.emit(CHANNEL, _msg(1))
        return delivered, seen

    delivered, seen = asyncio.run(scenario())

    assert delivered is True
    assert [event.message.id for event in seen] == [1]
    assert seen[0].chat_id == CHANNEL
    # Telethon's event proxies unknown attributes to the message; the fake
    # mirrors that, because the listener reads `event.message.message`.
    assert seen[0].text == "hello"


def test_emit_before_connect_is_not_delivered() -> None:
    async def scenario():
        client = FakeTelegramClient()
        seen, handler = _collector()
        client.add_event_handler(handler, events.NewMessage())
        delivered = await client.emit(CHANNEL, _msg(1))
        return delivered, seen

    delivered, seen = asyncio.run(scenario())

    assert delivered is False
    assert seen == []


def test_handler_filter_chats_restricts_delivery() -> None:
    async def scenario():
        client = FakeTelegramClient()
        seen, handler = _collector()
        client.add_event_handler(handler, events.NewMessage(chats=[CHANNEL]))
        await client.connect()
        await client.emit(OTHER_CHANNEL, _msg(1))
        await client.emit(CHANNEL, _msg(2))
        return seen

    seen = asyncio.run(scenario())

    assert [event.message.id for event in seen] == [2]


# -- the blackout: the whole reason Task 9's poll exists ---------------------


def test_blackout_hides_messages_from_handlers_but_keeps_them_in_history() -> None:
    async def scenario():
        client = FakeTelegramClient()
        seen, handler = _collector()
        client.add_event_handler(handler, events.NewMessage())
        await client.connect()

        client.start_blackout()
        delivered = await client.emit(CHANNEL, _msg(7))
        client.end_blackout()

        history = await client.get_messages(CHANNEL, min_id=0, reverse=True, limit=None)
        return delivered, seen, history

    delivered, seen, history = asyncio.run(scenario())

    assert delivered is False
    assert seen == []
    assert [message.id for message in history] == [7]


def test_blackout_ends_and_delivery_resumes() -> None:
    async def scenario():
        client = FakeTelegramClient()
        seen, handler = _collector()
        client.add_event_handler(handler, events.NewMessage())
        await client.connect()

        with client.blackout():
            await client.emit(CHANNEL, _msg(7))
        await client.emit(CHANNEL, _msg(8))

        history = await client.get_messages(CHANNEL, min_id=0, reverse=True, limit=None)
        return seen, history

    seen, history = asyncio.run(scenario())

    assert [event.message.id for event in seen] == [8]
    assert [message.id for message in history] == [7, 8]


def test_deliveries_distinguish_event_stream_from_history() -> None:
    """A test must be able to tell "the event stream delivered it" from
    "history had it" -- otherwise Task 9 cannot prove its poll, rather
    than the update stream, is what recovered a blacked-out message."""

    async def scenario():
        client = FakeTelegramClient()
        seen, handler = _collector()
        client.add_event_handler(handler, events.NewMessage())
        await client.connect()

        await client.emit(CHANNEL, _msg(1))
        with client.blackout():
            await client.emit(CHANNEL, _msg(2))
        await client.get_messages(CHANNEL, min_id=1, reverse=True)
        return client.deliveries

    deliveries = asyncio.run(scenario())

    assert [(d.message_id, d.route) for d in deliveries] == [
        (1, "event"),
        (2, "history"),
    ]


# -- history ------------------------------------------------------------------


def test_get_messages_honours_min_id_reverse_and_limit() -> None:
    async def scenario():
        client = FakeTelegramClient(
            history={CHANNEL: [_msg(1), _msg(2), _msg(3), _msg(4)]}
        )
        await client.connect()
        ascending = await client.get_messages(CHANNEL, min_id=2, reverse=True, limit=None)
        descending = await client.get_messages(CHANNEL, min_id=0, limit=None)
        limited = await client.get_messages(CHANNEL, min_id=0, reverse=True, limit=2)
        return ascending, descending, limited

    ascending, descending, limited = asyncio.run(scenario())

    assert [m.id for m in ascending] == [3, 4]
    assert [m.id for m in descending] == [4, 3, 2, 1]
    assert [m.id for m in limited] == [1, 2]


def test_get_messages_without_limit_returns_one_message_as_telethon_does() -> None:
    """Telethon 1.44's `get_messages()` defaults to `limit=1` unless both
    `min_id` and `max_id` are passed by keyword. A poll written as
    `get_messages(channel, min_id=pointer, reverse=True)` therefore
    fetches ONE message per call in production; the fake must too, or
    every test of such a poll passes while production falls behind."""

    async def scenario():
        client = FakeTelegramClient(
            history={CHANNEL: [_msg(1), _msg(2), _msg(3), _msg(4)]}
        )
        await client.connect()
        return (
            await client.get_messages(CHANNEL, min_id=0, reverse=True),
            await client.get_messages(CHANNEL),
            await client.get_messages(CHANNEL, min_id=1, max_id=4),
        )

    no_limit_ascending, no_limit, full_range = asyncio.run(scenario())

    assert [m.id for m in no_limit_ascending] == [1]
    assert [m.id for m in no_limit] == [4]
    # Both bounds given by keyword: Telethon returns the whole range.
    assert [m.id for m in full_range] == [3, 2]


class _RealGetMessagesProbe:
    """Stands in for `self` so the body of Telethon's real
    `MessageMethods.get_messages` runs offline: that body only resolves
    the default `limit`, then calls `self.iter_messages(*args, **kwargs)`
    and `await it.collect()`. This captures what `limit` it resolved."""

    def __init__(self) -> None:
        self.limit: object = "never called"

    def iter_messages(self, *args, **kwargs):
        bound = inspect.signature(MessageMethods.iter_messages).bind(self, *args, **kwargs)
        bound.apply_defaults()
        self.limit = bound.arguments["limit"]
        return self

    async def collect(self) -> list:
        return []


_GET_MESSAGES_CALL_SHAPES = [
    pytest.param((CHANNEL,), {}, id="entity-only"),
    pytest.param((CHANNEL,), {"min_id": 2, "reverse": True}, id="min_id-reverse"),
    pytest.param((CHANNEL,), {"max_id": 4}, id="max_id-only"),
    pytest.param((CHANNEL,), {"min_id": 1, "max_id": 4}, id="min_id-and-max_id"),
    pytest.param((CHANNEL,), {"limit": None, "min_id": 2, "reverse": True}, id="limit-None"),
    pytest.param((CHANNEL,), {"limit": 3, "min_id": 0, "reverse": True}, id="limit-3"),
    pytest.param((CHANNEL, 2), {}, id="positional-limit"),
    pytest.param((CHANNEL, None), {"min_id": 2}, id="positional-None"),
    pytest.param((), {"entity": CHANNEL, "min_id": 2}, id="entity-by-keyword"),
]


@pytest.mark.parametrize(("args", "kwargs"), _GET_MESSAGES_CALL_SHAPES)
def test_fake_get_messages_resolves_limit_exactly_as_real_telethon(args, kwargs) -> None:
    """Pins the fake to the installed Telethon, not to a copy of it: if a
    Telethon upgrade changes the default, this fails instead of the fake
    silently drifting."""

    async def scenario():
        probe = _RealGetMessagesProbe()
        await MessageMethods.get_messages(probe, *args, **dict(kwargs))
        client = FakeTelegramClient(history={CHANNEL: [_msg(i) for i in range(1, 6)]})
        await client.get_messages(*args, **dict(kwargs))
        return probe.limit, client.get_messages_calls[-1].limit

    real_limit, fake_limit = asyncio.run(scenario())

    assert real_limit != "never called"
    assert fake_limit == real_limit


def test_get_messages_refuses_arguments_it_does_not_implement() -> None:
    """`offset_id`, `search`, ... all change what Telethon returns.
    Silently ignoring one would let a test pass against a fiction."""

    async def scenario():
        client = FakeTelegramClient(history={CHANNEL: [_msg(1)]})
        with pytest.raises(NotImplementedError, match="offset_id"):
            await client.get_messages(CHANNEL, offset_id=1, limit=None)

    asyncio.run(scenario())


def test_get_messages_offset_date_returns_the_newest_messages_dated_before_it() -> None:
    """Non-reversed `offset_date` is Telegram's `getHistory` "only return
    messages sent before the specified date": strictly before, newest
    first. The listener's first-run seed finds its anchor this way."""
    from datetime import timedelta

    history = [
        FakeMessage(id=i, message="hi", date=POSTED + timedelta(minutes=i)) for i in range(1, 6)
    ]
    cutoff = POSTED + timedelta(minutes=3)  # message 3 is dated exactly at it

    async def scenario():
        client = FakeTelegramClient(history={CHANNEL: history})
        before = await client.get_messages(CHANNEL, limit=2, offset_date=cutoff)
        anchor = await client.get_messages(CHANNEL, limit=1, offset_date=cutoff)
        none = await client.get_messages(CHANNEL, limit=1, offset_date=POSTED)
        return before, anchor, none, client.get_messages_calls[-1]

    before, anchor, none, last_call = asyncio.run(scenario())

    assert [m.id for m in before] == [2, 1]
    assert [m.id for m in anchor] == [2]
    assert none == []
    assert last_call.offset_date == POSTED


@pytest.mark.parametrize("extra", [{"reverse": True}, {"min_id": 1}, {"max_id": 5}])
def test_get_messages_refuses_offset_date_combinations_it_does_not_model(extra) -> None:
    async def scenario():
        client = FakeTelegramClient(history={CHANNEL: [_msg(1)]})
        with pytest.raises(NotImplementedError, match="offset_date"):
            await client.get_messages(CHANNEL, limit=1, offset_date=POSTED, **extra)

    asyncio.run(scenario())


def test_get_messages_records_its_call_arguments() -> None:
    async def scenario():
        client = FakeTelegramClient(history={CHANNEL: [_msg(1)]})
        await client.connect()
        await client.get_messages(CHANNEL, min_id=0, reverse=True, limit=50)
        return client.get_messages_calls

    calls = asyncio.run(scenario())

    assert len(calls) == 1
    assert calls[0].entity == CHANNEL
    assert calls[0].min_id == 0
    assert calls[0].reverse is True
    assert calls[0].limit == 50


def test_get_messages_for_an_unknown_channel_is_empty() -> None:
    async def scenario():
        client = FakeTelegramClient()
        await client.connect()
        return await client.get_messages(CHANNEL, min_id=0, reverse=True)

    assert asyncio.run(scenario()) == []


# -- lifecycle ----------------------------------------------------------------


def test_get_dialogs_is_recorded_in_call_order() -> None:
    async def scenario():
        client = FakeTelegramClient()
        seen, handler = _collector()
        await client.connect()
        await client.get_dialogs()
        client.add_event_handler(handler, events.NewMessage())
        return client.calls, client.get_dialogs_call_count

    calls, count = asyncio.run(scenario())

    assert calls == ["connect", "get_dialogs", "add_event_handler"]
    assert count == 1


def test_run_until_disconnected_blocks_until_disconnect() -> None:
    async def scenario():
        client = FakeTelegramClient()
        await client.connect()
        task = asyncio.create_task(client.run_until_disconnected())
        await asyncio.sleep(0)
        still_running = not task.done()

        await client.disconnect()
        await asyncio.wait_for(task, timeout=1.0)
        return still_running, task.done(), client.is_connected()

    still_running, done, connected = asyncio.run(scenario())

    assert still_running is True
    assert done is True
    assert connected is False


def test_drop_connection_pauses_delivery_and_reconnect_resumes_it() -> None:
    """A transient network drop, not a terminal disconnect: Telethon
    reconnects on its own and `run_until_disconnected()` never returns."""

    async def scenario():
        client = FakeTelegramClient()
        seen, handler = _collector()
        client.add_event_handler(handler, events.NewMessage())
        await client.connect()
        task = asyncio.create_task(client.run_until_disconnected())
        await asyncio.sleep(0)

        await client.drop_connection()
        connected_while_dropped = client.is_connected()
        delivered_while_dropped = await client.emit(CHANNEL, _msg(1))

        await client.reconnect()
        delivered_after_reconnect = await client.emit(CHANNEL, _msg(2))
        run_returned = task.done()

        history = await client.get_messages(CHANNEL, min_id=0, reverse=True, limit=None)

        await client.disconnect()
        await asyncio.wait_for(task, timeout=1.0)
        return (
            connected_while_dropped,
            delivered_while_dropped,
            delivered_after_reconnect,
            run_returned,
            seen,
            history,
        )

    (
        connected_while_dropped,
        delivered_while_dropped,
        delivered_after_reconnect,
        run_returned,
        seen,
        history,
    ) = asyncio.run(scenario())

    assert connected_while_dropped is False
    assert delivered_while_dropped is False
    assert delivered_after_reconnect is True
    assert run_returned is False
    assert [event.message.id for event in seen] == [2]
    # The message sent while the socket was down still exists in the channel.
    assert [message.id for message in history] == [1, 2]


# -- strictness ---------------------------------------------------------------


def test_emit_reraises_a_handler_exception_and_records_it() -> None:
    """Deliberately *stricter* than Telethon, which swallows and logs
    callback exceptions. If the fake swallowed them too, the listener's
    own "an exception must not kill the client" guarantee would be
    untestable -- every test would pass whether the listener handled the
    exception or not."""

    async def scenario():
        client = FakeTelegramClient()

        async def boom(event) -> None:
            raise ValueError("handler blew up")

        client.add_event_handler(boom, events.NewMessage())
        await client.connect()
        with pytest.raises(ValueError, match="handler blew up"):
            await client.emit(CHANNEL, _msg(1))
        return client.handler_exceptions

    exceptions = asyncio.run(scenario())

    assert len(exceptions) == 1
    assert isinstance(exceptions[0], ValueError)


def test_dispatch_continues_past_a_failing_handler_then_reraises() -> None:
    """Telethon's dispatch catches a callback's exception and moves on to
    the next handler (telethon/client/updates.py `_dispatch_update`). The
    fake does the same, then re-raises the first exception so a test can
    still see it. The stream did deliver the event, so the delivery is
    recorded either way."""

    async def scenario():
        client = FakeTelegramClient()
        seen, second = _collector()

        async def first(event) -> None:
            raise ValueError("first handler blew up")

        client.add_event_handler(first, events.NewMessage())
        client.add_event_handler(second, events.NewMessage())
        await client.connect()
        with pytest.raises(ValueError, match="first handler blew up"):
            await client.emit(CHANNEL, _msg(1))
        return seen, client.deliveries, client.handler_exceptions

    seen, deliveries, exceptions = asyncio.run(scenario())

    assert [event.message.id for event in seen] == [1]
    assert [(d.message_id, d.route) for d in deliveries] == [(1, "event")]
    assert len(exceptions) == 1


def test_rpc_shaped_methods_are_logged_to_calls() -> None:
    """Every method that is a network round trip in Telethon exists on
    the fake and logs to `calls`. Without these, a handler calling one
    inside `try/except: pass` "failed" with AttributeError and left the
    call log untouched -- the no-RPC test passed (reviewer's M2d)."""

    async def scenario():
        client = FakeTelegramClient(history={CHANNEL: [_msg(1)]})
        seen, handler = _collector()
        client.add_event_handler(handler, events.NewMessage())
        await client.connect()
        await client.emit(
            CHANNEL, FakeMessage(id=2, message="re", date=POSTED, reply_to_msg_id=1)
        )
        event = seen[0]
        before = len(client.calls)
        await client.get_entity(CHANNEL)
        await client.get_input_entity(CHANNEL)
        replied = await event.get_reply_message()
        await event.get_chat()
        await event.get_sender()
        return client.calls[before:], replied

    new_calls, replied = asyncio.run(scenario())

    assert new_calls == [
        "get_entity",
        "get_input_entity",
        "get_reply_message",
        "get_chat",
        "get_sender",
    ]
    assert replied is not None and replied.id == 1


def test_fake_message_rejects_naive_datetimes() -> None:
    """Telethon 1.44 always hands out aware UTC datetimes. A naive one in
    the fake could only ever come from test code -- and silently reading
    it as UTC could shift a signal's age by hours."""
    naive = datetime(2026, 9, 5, 10, 45)

    with pytest.raises(ValueError, match="date"):
        FakeMessage(id=1, message="hi", date=naive)
    with pytest.raises(ValueError, match="edit_date"):
        FakeMessage(id=1, message="hi", date=POSTED, edit_date=naive)


def test_remove_event_handler_stops_delivery() -> None:
    async def scenario():
        client = FakeTelegramClient()
        seen, handler = _collector()
        client.add_event_handler(handler, events.NewMessage())
        await client.connect()
        await client.emit(CHANNEL, _msg(1))
        client.remove_event_handler(handler)
        await client.emit(CHANNEL, _msg(2))
        return seen

    seen = asyncio.run(scenario())

    assert [event.message.id for event in seen] == [1]


def test_message_fields_are_tz_aware_utc() -> None:
    message = _msg(1)
    assert message.date.tzinfo is not None
    assert message.date.utcoffset() == POSTED.utcoffset()


# -- get_messages(ids=...): Task 10's window re-read ---------------------------


class _RealIdsStub:
    """Stands in for `self` so Telethon 1.44's real `get_messages` /
    `iter_messages` / `_IDsIter` run offline against the fake's history.

    `_IDsIter` resolves the entity (`get_input_entity`), sends one
    `channels.GetMessagesRequest` per 100 ids, and turns each
    `MessageEmpty` Telegram answers for a deleted id into `None`
    (telethon/client/messages.py:285-338). This stub answers that request
    the way Telegram does -- a `Message` per existing id, a
    `MessageEmpty` per missing one, in the requested order -- so the
    *real* library code decides the shape of the result, and the fake is
    compared against it rather than against a copy of it."""

    _self_id = 1

    class _Cache:
        def get(self, *args, **kwargs):
            return None

    _mb_entity_cache = _Cache()

    def __init__(self, fake: FakeTelegramClient, channel_id: int) -> None:
        self._by_id = {m.id: m for m in fake.history.get(channel_id, [])}
        self.requests = 0

    async def get_input_entity(self, entity):
        from telethon.tl import types

        return types.InputPeerChannel(channel_id=1111111111, access_hash=0)

    async def __call__(self, request):
        from telethon.tl import types

        self.requests += 1
        peer = types.PeerChannel(1111111111)
        answered = []
        for wanted in request.id:
            wanted = getattr(wanted, "id", wanted)
            message = self._by_id.get(wanted)
            if message is None:
                answered.append(types.MessageEmpty(id=wanted, peer_id=peer))
            else:
                answered.append(
                    types.Message(
                        id=wanted, peer_id=peer, date=message.date, message=message.message
                    )
                )
        return types.messages.ChannelMessages(
            pts=1, count=len(answered), messages=answered, chats=[], users=[], topics=[]
        )

    def iter_messages(self, *args, **kwargs):
        return MessageMethods.iter_messages(self, *args, **kwargs)


def _ids_of(result):
    if isinstance(result, list):
        return [None if m is None else m.id for m in result]
    return None if result is None else result.id


_IDS_CALL_SHAPES = [
    pytest.param({"ids": [5, 4, 3, 1]}, id="list-with-deleted"),
    pytest.param({"ids": (2, 9, 3)}, id="tuple-with-never-existed"),
    pytest.param({"ids": [5, 4, 3], "limit": 1}, id="limit-is-ignored"),
    pytest.param({"ids": [5, 4, 3], "min_id": 4}, id="min_id-is-ignored"),
    pytest.param({"ids": [1, 2, 3], "reverse": True}, id="reverse-reverses-the-ids"),
    pytest.param({"ids": 3}, id="scalar-present"),
    pytest.param({"ids": 4}, id="scalar-deleted"),
    pytest.param({"ids": []}, id="empty"),
]


@pytest.mark.parametrize("kwargs", _IDS_CALL_SHAPES)
def test_fake_get_messages_ids_matches_real_telethon(kwargs) -> None:
    """A list in, a list out, the same order, `None` where a message is
    missing or deleted; a scalar in, one message (or `None`) out; every
    other argument ignored -- exactly what the installed Telethon does."""
    history = [_msg(1), _msg(2), _msg(3), _msg(5)]  # 4 was deleted

    async def scenario():
        fake = FakeTelegramClient(history={CHANNEL: history})
        real = await MessageMethods.get_messages(
            _RealIdsStub(fake, CHANNEL), CHANNEL, **dict(kwargs)
        )
        mine = await fake.get_messages(CHANNEL, **dict(kwargs))
        return real, mine

    real, mine = asyncio.run(scenario())

    assert _ids_of(mine) == _ids_of(real)
    assert isinstance(mine, list) == isinstance(real, list)


def test_get_messages_ids_records_the_call_and_the_history_deliveries() -> None:
    async def scenario():
        client = FakeTelegramClient(history={CHANNEL: [_msg(1), _msg(3)]})
        result = await client.get_messages(CHANNEL, ids=[3, 2, 1])
        return result, client.get_messages_calls, client.deliveries, client.calls

    result, calls, deliveries, call_log = asyncio.run(scenario())

    assert _ids_of(result) == [3, None, 1]
    assert [(c.entity, c.ids, c.limit) for c in calls] == [(CHANNEL, (3, 2, 1), 3)]
    assert [(d.message_id, d.route) for d in deliveries] == [(3, "history"), (1, "history")]
    assert call_log == ["get_messages"]


def test_get_messages_ids_still_refuses_arguments_it_does_not_implement() -> None:
    async def scenario():
        client = FakeTelegramClient(history={CHANNEL: [_msg(1)]})
        with pytest.raises(NotImplementedError, match="search"):
            await client.get_messages(CHANNEL, ids=[1], search="x")

    asyncio.run(scenario())


# -- edits and deletions --------------------------------------------------------


EDITED_AT = datetime(2026, 9, 5, 10, 50, tzinfo=UTC)


def test_emit_edit_updates_history_and_reaches_only_message_edited_handlers() -> None:
    """Telethon dispatches by the *builder's type*
    (telethon/client/updates.py:591, `built[type(builder)]`), and each
    builder's `build()` accepts only its own updates -- `MessageEdited`
    takes the edit updates, `NewMessage` the new-message ones.
    `MessageEdited` subclasses `NewMessage`, so an `isinstance` filter
    would wrongly hand every new message to an edit handler too."""

    async def scenario():
        client = FakeTelegramClient(history={CHANNEL: [_msg(1, "before")]})
        new_seen, on_new = _collector()
        edit_seen, on_edit = _collector()
        client.add_event_handler(on_new, events.NewMessage())
        client.add_event_handler(on_edit, events.MessageEdited())
        await client.connect()
        delivered = await client.emit_edit(CHANNEL, 1, "after", edit_date=EDITED_AT)
        await client.emit(CHANNEL, _msg(2))
        (current,) = await client.get_messages(CHANNEL, ids=[1])
        return delivered, new_seen, edit_seen, current

    delivered, new_seen, edit_seen, current = asyncio.run(scenario())

    assert delivered is True
    assert [e.message.id for e in new_seen] == [2]
    assert [
        (e.chat_id, e.message.id, e.message.message, e.message.edit_date) for e in edit_seen
    ] == [(CHANNEL, 1, "after", EDITED_AT)]
    assert (current.message, current.edit_date, current.date) == ("after", EDITED_AT, POSTED)


def test_emit_delete_removes_from_history_and_reaches_only_message_deleted_handlers() -> None:
    async def scenario():
        client = FakeTelegramClient(history={CHANNEL: [_msg(1), _msg(2), _msg(3)]})
        new_seen, on_new = _collector()
        deleted_seen, on_deleted = _collector()
        client.add_event_handler(on_new, events.NewMessage())
        client.add_event_handler(on_deleted, events.MessageDeleted())
        await client.connect()
        delivered = await client.emit_delete(CHANNEL, 1, 3)
        by_id = await client.get_messages(CHANNEL, ids=[1, 2, 3])
        return delivered, new_seen, deleted_seen, by_id

    delivered, new_seen, deleted_seen, by_id = asyncio.run(scenario())

    assert delivered is True
    assert new_seen == []
    (event,) = deleted_seen
    assert (event.chat_id, list(event.deleted_ids), event.deleted_id) == (CHANNEL, [1, 3], 1)
    assert _ids_of(by_id) == [None, 2, None]


@pytest.mark.parametrize("outage", ["blackout", "dropped"])
def test_edits_and_deletions_during_an_outage_change_history_but_are_not_delivered(
    outage: str,
) -> None:
    async def scenario():
        client = FakeTelegramClient(history={CHANNEL: [_msg(1), _msg(2)]})
        seen, handler = _collector()
        client.add_event_handler(handler, events.MessageEdited())
        client.add_event_handler(handler, events.MessageDeleted())
        await client.connect()
        if outage == "blackout":
            client.start_blackout()
        else:
            await client.drop_connection()
        edited = await client.emit_edit(CHANNEL, 1, "after", edit_date=EDITED_AT)
        deleted = await client.emit_delete(CHANNEL, 2)
        by_id = await client.get_messages(CHANNEL, ids=[1, 2])
        return edited, deleted, seen, by_id

    edited, deleted, seen, by_id = asyncio.run(scenario())

    assert (edited, deleted) == (False, False)
    assert seen == []
    assert by_id[0].message == "after" and by_id[1] is None


def test_emit_edit_and_delete_refuse_what_telegram_could_not_produce() -> None:
    async def scenario():
        client = FakeTelegramClient(history={CHANNEL: [_msg(1)]})
        await client.connect()
        with pytest.raises(KeyError):
            await client.emit_edit(CHANNEL, 99, "x", edit_date=EDITED_AT)
        with pytest.raises(KeyError):
            await client.emit_delete(CHANNEL, 99)
        with pytest.raises(ValueError, match="naive"):
            await client.emit_edit(CHANNEL, 1, "x", edit_date=datetime(2026, 9, 5, 11, 0))
        with pytest.raises(ValueError, match="before"):
            await client.emit_edit(
                CHANNEL, 1, "x", edit_date=datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
            )
        # A refused call changed nothing.
        (unchanged,) = await client.get_messages(CHANNEL, ids=[1])
        assert unchanged == _msg(1)

    asyncio.run(scenario())
