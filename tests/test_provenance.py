"""Who posted a message, and where a forward came from (final review F05
and F09: the metadata capture the relay and group fixes build on).

`normalise_message` reads Telethon's `post`, `from_id` and `fwd_from` --
attribute reads only, no request -- and every path that stores a message
(the update stream, the poll, backfill) hands them to
`messages_repo.ingest`, which stores them on the message's row. What cannot
be read cleanly is left empty: metadata must never stop a message from
being stored. `posted_at` stays the message's own date (spec G6).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest
from telethon.tl import types

from fakes.telegram import FakeMessage, FakeTelegramClient
from test_listener import (
    CHANNEL,
    CHANNEL_ID,
    CHATTER_TEXT,
    NOW,
    RPC_TIMEOUT,
    SIGNAL_TEXT,
    _rows,
    _seed_last_processed,
    _started,
)
from xaucopier.ingest.listener import normalise_message

VIP = 5555555555  # a channel's bare Telegram id
VIP_MARKED = -1005555555555  # the same, in channels.toml's -100... form
FORWARDED_AT = NOW - timedelta(minutes=20)


def _message(**kwargs) -> FakeMessage:
    base = dict(id=10, message=CHATTER_TEXT, date=NOW)
    base.update(kwargs)
    return FakeMessage(**base)


def _provenance(incoming) -> tuple:
    return (
        incoming.is_post,
        incoming.sender_kind,
        incoming.sender_id,
        incoming.fwd_from_channel_id,
        incoming.fwd_from_post_id,
        incoming.fwd_from_date_utc,
    )


# -- normalise_message ------------------------------------------------------------------


def test_a_channels_own_post_is_marked_as_one() -> None:
    incoming = normalise_message(_message(post=True), channel_id=CHANNEL_ID)
    assert _provenance(incoming) == (True, None, None, None, None, None)


@pytest.mark.parametrize(
    "peer, kind, sender_id",
    [
        (types.PeerUser(user_id=42), "user", 42),
        (types.PeerChannel(channel_id=VIP), "channel", VIP_MARKED),
        (types.PeerChat(chat_id=77), "chat", -77),
    ],
)
def test_the_sender_is_read_with_its_kind(peer, kind: str, sender_id: int) -> None:
    incoming = normalise_message(_message(post=False, from_id=peer), channel_id=CHANNEL_ID)
    assert (incoming.is_post, incoming.sender_kind, incoming.sender_id) == (False, kind, sender_id)


def test_a_forward_from_a_channel_records_the_channel_its_post_and_its_date() -> None:
    header = types.MessageFwdHeader(
        date=FORWARDED_AT, from_id=types.PeerChannel(channel_id=VIP), channel_post=4001
    )
    incoming = normalise_message(_message(fwd_from=header), channel_id=CHANNEL_ID)
    assert (
        incoming.fwd_from_channel_id,
        incoming.fwd_from_post_id,
        incoming.fwd_from_date_utc,
    ) == (VIP_MARKED, 4001, FORWARDED_AT)
    # The copy's own date stays its posted_at (spec G6).
    assert incoming.posted_at_utc == NOW


def test_a_forward_from_a_person_records_no_channel() -> None:
    header = types.MessageFwdHeader(date=FORWARDED_AT, from_id=types.PeerUser(user_id=42))
    incoming = normalise_message(_message(fwd_from=header), channel_id=CHANNEL_ID)
    assert (incoming.fwd_from_channel_id, incoming.fwd_from_post_id) == (None, None)
    assert incoming.fwd_from_date_utc == FORWARDED_AT


def test_a_forward_date_in_another_zone_is_stored_in_utc() -> None:
    plus_three = FORWARDED_AT.astimezone(timezone(timedelta(hours=3)))
    header = types.MessageFwdHeader(date=plus_three, from_id=types.PeerChannel(channel_id=VIP))
    incoming = normalise_message(_message(fwd_from=header), channel_id=CHANNEL_ID)
    assert incoming.fwd_from_date_utc == FORWARDED_AT
    assert incoming.fwd_from_date_utc.tzinfo == UTC


@pytest.mark.parametrize(
    "fields",
    [
        dict(post="yes"),
        dict(from_id="someone"),
        dict(from_id=types.PeerChannel(channel_id=0)),
        dict(fwd_from="a header"),
        dict(fwd_from=types.MessageFwdHeader(date=datetime(2026, 9, 5, 10, 40))),  # naive
        dict(fwd_from=types.MessageFwdHeader(date=FORWARDED_AT, channel_post=0)),
        dict(fwd_from=types.MessageFwdHeader(date=FORWARDED_AT, channel_post=True)),
    ],
)
def test_metadata_that_cannot_be_read_is_left_empty_never_an_error(fields: dict) -> None:
    incoming = normalise_message(_message(**fields), channel_id=CHANNEL_ID)
    assert incoming.text == CHATTER_TEXT
    provenance = _provenance(incoming)
    assert all(value is None for value in provenance[:5])
    assert provenance[5] is None or provenance[5].tzinfo is not None


def test_a_message_without_any_of_it_reads_as_unknown() -> None:
    bare = type("Bare", (), {"id": 3, "message": "hi", "date": NOW})()
    assert _provenance(normalise_message(bare, channel_id=CHANNEL_ID)) == (None,) * 6


# -- every path that stores a message stores it ------------------------------------------


def _stored_provenance(db_path) -> list[tuple]:
    return _rows(
        db_path,
        "SELECT message_id, source, is_post, sender_kind, sender_id, fwd_from_channel_id, "
        "fwd_from_post_id, fwd_from_date_utc, posted_at_utc FROM messages ORDER BY message_id",
    )


def _forwarded(message_id: int, text: str = SIGNAL_TEXT) -> FakeMessage:
    return FakeMessage(
        id=message_id,
        message=text,
        date=NOW + timedelta(seconds=message_id),
        post=True,
        fwd_from=types.MessageFwdHeader(
            date=FORWARDED_AT, from_id=types.PeerChannel(channel_id=VIP), channel_post=4001
        ),
    )


def _member(message_id: int) -> FakeMessage:
    return FakeMessage(
        id=message_id,
        message=CHATTER_TEXT,
        date=NOW + timedelta(seconds=message_id),
        post=False,
        from_id=types.PeerUser(user_id=42),
    )


def test_live_backfill_and_poll_all_store_who_posted_it_and_where_it_came_from(
    tmp_path,
) -> None:
    db_path = tmp_path / "store.db"
    _seed_last_processed(db_path, CHANNEL_ID, 9)

    async def scenario():
        client = FakeTelegramClient(history={CHANNEL_ID: [_forwarded(10)]})  # backfill
        async with _started(db_path, client) as listener:
            await client.emit(CHANNEL_ID, _member(11))  # live
            with client.blackout():
                await client.emit(CHANNEL_ID, _member(12))
            await listener.read_history(  # the poll
                CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT
            )

    asyncio.run(scenario())

    forwarded_at = FORWARDED_AT.isoformat()
    assert _stored_provenance(db_path) == [
        (10, "backfill", 1, None, None, VIP_MARKED, 4001, forwarded_at,
         (NOW + timedelta(seconds=10)).isoformat()),
        (11, "live", 0, "user", 42, None, None, None, (NOW + timedelta(seconds=11)).isoformat()),
        (12, "poll", 0, "user", 42, None, None, None, (NOW + timedelta(seconds=12)).isoformat()),
    ]
