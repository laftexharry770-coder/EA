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
from datetime import UTC, datetime

import pytest
from telethon import events

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

        history = await client.get_messages(CHANNEL, min_id=0, reverse=True)
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

        history = await client.get_messages(CHANNEL, min_id=0, reverse=True)
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
        ascending = await client.get_messages(CHANNEL, min_id=2, reverse=True)
        descending = await client.get_messages(CHANNEL, min_id=0)
        limited = await client.get_messages(CHANNEL, min_id=0, reverse=True, limit=2)
        return ascending, descending, limited

    ascending, descending, limited = asyncio.run(scenario())

    assert [m.id for m in ascending] == [3, 4]
    assert [m.id for m in descending] == [4, 3, 2, 1]
    assert [m.id for m in limited] == [1, 2]


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

        history = await client.get_messages(CHANNEL, min_id=0, reverse=True)

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
