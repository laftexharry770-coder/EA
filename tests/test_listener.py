"""Tests for the Telegram listener -- the process that actually runs.

Everything here drives the real `store` stack (`Writer`, `messages_repo.
ingest`, `outbox.fan_out`) against a temporary database, with only the
Telegram client faked (`tests/fakes/telegram.py`). No network.

Four of these tests are not about the happy path at all, and are the
reason the listener is shaped the way it is:

- `test_get_dialogs_is_called_before_handlers_are_registered` -- the
  `get_dialogs()` priming call populates the per-channel `pts` state
  Telethon's update engine needs. Registering handlers first is the
  subtly-wrong ordering that silently degrades update delivery.
- `test_handler_makes_no_rpc_calls` / `test_ingest_package_never_calls_
  sqlite3_directly` -- an RPC or a blocking `sqlite3` call on the update
  path stalls the event loop, which is a documented cause of dropped
  channel updates, which is a missed trade.
- `test_writer_connects_before_any_reader_connection` -- `db.connect()`
  runs `migrate()` on whichever thread calls it, so a reader opening a
  brand-new file first would run schema DDL on the event-loop thread.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes.telegram import FakeMessage, FakeTelegramClient
from purity_helpers import sqlite3_offences
from xaucopier.config.accounts import AccountCfg
from xaucopier.config.channels import ChannelCfg
from xaucopier.ingest import listener as listener_module
from xaucopier.ingest.listener import (
    EXIT_LISTENER_STOPPED,
    IncomingMessage,
    Listener,
    ListenerStopped,
    normalise_event,
    run_listener,
)
from xaucopier.store import db, messages_repo, metrics

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "messages" / "mmh_buy_4tp.json"
_FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
SIGNAL_TEXT: str = _FIXTURE["text"]
SIGNAL_MESSAGE_ID: int = _FIXTURE["message_id"]
CHANNEL_ID: int = _FIXTURE["channel_id"]
POSTED = datetime.fromisoformat(_FIXTURE["posted_at_utc"])

CHATTER_TEXT = "Good morning traders"
NOW = datetime(2026, 9, 5, 11, 0, tzinfo=UTC)

CHANNEL = ChannelCfg(
    channel_id=CHANNEL_ID, name="Channel A", parser="mmh", ordinal=1, enabled=True
)
DISABLED_CHANNEL = ChannelCfg(
    channel_id=-1009999999999, name="Channel Off", parser="mmh", ordinal=2, enabled=False
)
UNKNOWN_CHANNEL_ID = -1003333333333
#: `read_history`'s required RPC bound, for tests that do not exercise it.
RPC_TIMEOUT = 5.0


def _account(account_id: str, *, enabled: bool = True, ordinal: int = 1) -> AccountCfg:
    return AccountCfg(
        account_id=account_id,
        broker="TestBroker",
        terminal_path=r"C:\terminal\terminal64.exe",
        login=123456,
        server="TestBroker-Demo",
        symbol="XAUUSD",
        fixed_lot=0.01,
        magic_base=1000 * ordinal,
        ordinal=ordinal,
        allow_live=False,
        enabled=enabled,
    )


ACCOUNTS = (
    _account("acct-a", ordinal=1),
    _account("acct-b", ordinal=2),
    _account("acct-off", enabled=False, ordinal=3),
)
ENABLED_ACCOUNT_COUNT = sum(1 for account in ACCOUNTS if account.enabled)
assert ENABLED_ACCOUNT_COUNT == 2, "the fan-out tests need more than one enabled account"


def _signal_message(message_id: int = SIGNAL_MESSAGE_ID) -> FakeMessage:
    return FakeMessage(id=message_id, message=SIGNAL_TEXT, date=POSTED)


def _chatter_message(message_id: int) -> FakeMessage:
    return FakeMessage(id=message_id, message=CHATTER_TEXT, date=POSTED)


def _build_listener(
    db_path: Path,
    client: FakeTelegramClient,
    *,
    channels: tuple[ChannelCfg, ...] = (CHANNEL,),
    accounts: tuple[AccountCfg, ...] = ACCOUNTS,
    writer: db.Writer | None = None,
    **kwargs,
) -> Listener:
    return Listener(
        client=client,
        channels=channels,
        accounts=accounts,
        writer=writer if writer is not None else db.Writer(db_path),
        db_path=db_path,
        now=lambda: NOW,
        **kwargs,
    )


@contextlib.asynccontextmanager
async def _started(db_path: Path, client: FakeTelegramClient, **kwargs):
    listener = _build_listener(db_path, client, **kwargs)
    await listener.start()
    try:
        yield listener
    finally:
        await listener.aclose()


def _rows(db_path: Path, sql: str, params: tuple = ()) -> list:
    conn = db.connect(db_path, role="reader")
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _count(db_path: Path, table: str) -> int:
    return _rows(db_path, f"SELECT COUNT(*) FROM {table}")[0][0]


def _seed_last_processed(db_path: Path, channel_id: int, value: int) -> None:
    conn = db.connect(db_path, role="writer")
    try:
        conn.execute(
            "INSERT INTO channel_state (channel_id, last_processed_message_id) "
            "VALUES (?, ?) ON CONFLICT(channel_id) DO UPDATE SET "
            "last_processed_message_id = excluded.last_processed_message_id",
            (channel_id, value),
        )
        conn.commit()
    finally:
        conn.close()


def _last_processed(db_path: Path, channel_id: int) -> int:
    rows = _rows(
        db_path,
        "SELECT last_processed_message_id FROM channel_state WHERE channel_id = ?",
        (channel_id,),
    )
    return rows[0][0] if rows else 0


# -- the hot path ------------------------------------------------------------


def test_emitted_message_writes_one_message_and_one_outbox_row_per_enabled_account(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "store.db"

    async def scenario() -> None:
        client = FakeTelegramClient()
        async with _started(db_path, client):
            await client.emit(CHANNEL_ID, _signal_message())

    asyncio.run(scenario())

    assert _count(db_path, "messages") == 1
    assert _count(db_path, "signals") == 1
    outbox = _rows(db_path, "SELECT account_id, kind, state FROM outbox ORDER BY account_id")
    assert outbox == [("acct-a", "ENTRY", "QUEUED"), ("acct-b", "ENTRY", "QUEUED")]
    assert len(outbox) == ENABLED_ACCOUNT_COUNT
    assert _rows(db_path, "SELECT source FROM messages") == [("live",)]
    # A live delivery never moves the history pointer -- see the
    # "history pointer" section below for why.
    assert _last_processed(db_path, CHANNEL_ID) == 0


def test_duplicate_emission_writes_no_second_row(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"

    async def scenario() -> None:
        client = FakeTelegramClient()
        async with _started(db_path, client):
            await client.emit(CHANNEL_ID, _signal_message())
            await client.emit(CHANNEL_ID, _signal_message())

    asyncio.run(scenario())

    assert _count(db_path, "messages") == 1
    assert _count(db_path, "signals") == 1
    assert _count(db_path, "outbox") == ENABLED_ACCOUNT_COUNT


def test_handler_makes_no_rpc_calls(tmp_path: Path) -> None:
    """Every RPC-shaped method the fake exposes -- on the client
    (`get_messages`, `get_entity`, `get_input_entity`, ...) and on the
    event (`get_reply_message`, `get_chat`, `get_sender`) -- appends to
    `calls`. The call log is the evidence: if the handler made any round
    trip, even one wrapped in `try/except: pass`, the log grows while
    the handler runs. (Before the fake had these stubs, such a
    suppressed call "failed" with AttributeError and this test passed --
    the reviewer's M2d.)"""
    db_path = tmp_path / "store.db"

    async def scenario() -> list[str]:
        client = FakeTelegramClient()
        async with _started(db_path, client) as listener:
            before = list(client.calls)
            await client.emit(CHANNEL_ID, _signal_message())
            after = list(client.calls)
            assert listener.handler_error_count == 0
        return [call for call in after[len(before) :]]

    new_calls = asyncio.run(scenario())

    assert new_calls == []


def test_ingest_package_never_calls_sqlite3_directly() -> None:
    """A blocking `sqlite3` call on the event-loop thread stalls the
    loop for the whole lock wait. Every write goes through
    `Writer.submit()`; every read through `store.db.connect()`."""
    ingest_dir = Path(listener_module.__file__).resolve().parent

    assert sqlite3_offences(ingest_dir) == set()


def test_handler_exception_does_not_kill_the_client_and_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "store.db"
    real_normalise = listener_module.normalise_event

    def boom(event):
        if event.message.id == 999:
            raise ValueError("normalisation blew up")
        return real_normalise(event)

    monkeypatch.setattr(listener_module, "normalise_event", boom)

    async def scenario() -> tuple[int, bool, bool, tuple]:
        client = FakeTelegramClient()
        async with _started(db_path, client) as listener:
            waiter = asyncio.create_task(client.run_until_disconnected())
            await asyncio.sleep(0)

            # Must not raise out of the handler, must not end the client.
            await client.emit(CHANNEL_ID, _chatter_message(999))

            still_running = not waiter.done()
            still_connected = client.is_connected()

            # ... and the listener keeps working afterwards.
            await client.emit(CHANNEL_ID, _signal_message())

            errors = listener.handler_errors
            count = listener.handler_error_count
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
        return count, still_running, still_connected, errors

    count, still_running, still_connected, errors = asyncio.run(scenario())

    assert count == 1
    assert still_running is True
    assert still_connected is True
    assert isinstance(errors, tuple)
    assert "normalisation blew up" in errors[0].error
    # The message that did parse still landed.
    assert _count(db_path, "messages") == 1
    assert _rows(db_path, "SELECT message_id FROM messages") == [(SIGNAL_MESSAGE_ID,)]


def test_message_from_an_unconfigured_channel_is_ignored(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"

    async def scenario() -> int:
        client = FakeTelegramClient()
        async with _started(db_path, client) as listener:
            await client.emit(UNKNOWN_CHANNEL_ID, _signal_message())
            return listener.handler_error_count

    errors = asyncio.run(scenario())

    assert errors == 0
    assert _count(db_path, "messages") == 0


def test_messages_from_unwatched_chats_are_dropped_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no `chats=` filter, so the handler sees every chat the
    account is in. Routing on `event.chat_id` comes first, so a message
    from a chat nobody watches costs one dict lookup -- no normalising,
    and no chance of recording an error for a chat we do not care about."""
    db_path = tmp_path / "store.db"
    normalised: list[int] = []
    real_normalise = listener_module.normalise_event

    def spy(event):
        normalised.append(event.chat_id)
        return real_normalise(event)

    monkeypatch.setattr(listener_module, "normalise_event", spy)

    async def scenario() -> int:
        client = FakeTelegramClient()
        async with _started(db_path, client, channels=(CHANNEL, DISABLED_CHANNEL)) as listener:
            await client.emit(UNKNOWN_CHANNEL_ID, _chatter_message(1))
            await client.emit(DISABLED_CHANNEL.channel_id, _chatter_message(2))
            await client.emit(CHANNEL_ID, _chatter_message(3))
            return listener.handler_error_count

    errors = asyncio.run(scenario())

    assert normalised == [CHANNEL_ID]
    assert errors == 0
    assert _rows(db_path, "SELECT channel_id, message_id FROM messages") == [(CHANNEL_ID, 3)]


def test_handler_error_log_is_bounded_but_the_count_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sustained failure must not grow memory for the life of the
    process; the count keeps the true total."""
    db_path = tmp_path / "store.db"
    kept = listener_module.MAX_RECORDED_HANDLER_ERRORS
    total = kept + 44

    def boom(event):
        raise ValueError(f"bad message {event.message.id}")

    monkeypatch.setattr(listener_module, "normalise_event", boom)

    async def scenario():
        client = FakeTelegramClient()
        async with _started(db_path, client) as listener:
            for message_id in range(1, total + 1):
                await client.emit(CHANNEL_ID, _chatter_message(message_id))
            return listener.handler_error_count, listener.handler_errors

    count, errors = asyncio.run(scenario())

    assert count == total
    assert len(errors) == kept
    assert isinstance(errors, tuple)
    # The most recent are the ones kept.
    assert errors[-1].error == f"ValueError: bad message {total}"
    assert errors[0].error == f"ValueError: bad message {total - kept + 1}"
    assert {error.source for error in errors} == {"live"}


def test_message_from_a_disabled_channel_is_ignored(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"

    async def scenario() -> None:
        client = FakeTelegramClient()
        async with _started(db_path, client, channels=(CHANNEL, DISABLED_CHANNEL)):
            await client.emit(DISABLED_CHANNEL.channel_id, _signal_message())

    asyncio.run(scenario())

    assert _count(db_path, "messages") == 0


def test_message_posted_during_a_blackout_never_reaches_the_store(
    tmp_path: Path,
) -> None:
    """The listener alone cannot see a blacked-out message: it is in
    history but was never delivered as an update. This is precisely the
    hole Task 9's poll fills."""
    db_path = tmp_path / "store.db"

    async def scenario() -> list:
        client = FakeTelegramClient()
        async with _started(db_path, client):
            with client.blackout():
                await client.emit(CHANNEL_ID, _signal_message())
            return await client.get_messages(CHANNEL_ID, min_id=0, reverse=True)

    history = asyncio.run(scenario())

    assert [message.id for message in history] == [SIGNAL_MESSAGE_ID]
    assert _count(db_path, "messages") == 0


# -- the history pointer -----------------------------------------------------
#
# `channel_state.last_processed_message_id` is where a *history read*
# (startup backfill, Task 9's poll) resumes. Only a history read sees a
# contiguous run of ids, so only a history read may move it. If the live
# path moved it too, one live message landing after a blackout would
# carry the pointer past everything the blackout hid, and the poll --
# which reads from the pointer -- would never see those messages at all.
# A missed message is a missed trade.


def test_live_message_after_a_blackout_does_not_carry_the_pointer_past_the_missed_ones(
    tmp_path: Path,
) -> None:
    """The reviewer's D1: blackout hides 10 and 11, then 12 arrives live."""
    db_path = tmp_path / "store.db"
    _seed_last_processed(db_path, CHANNEL_ID, 9)

    async def scenario():
        client = FakeTelegramClient()
        async with _started(db_path, client) as listener:
            with client.blackout():
                delivered_10 = await client.emit(CHANNEL_ID, _chatter_message(10))
                delivered_11 = await client.emit(CHANNEL_ID, _chatter_message(11))
            delivered_12 = await client.emit(CHANNEL_ID, _chatter_message(12))
            pointer_after_live = listener.read_last_processed_message_id(CHANNEL_ID)
            assert pointer_after_live == 9, "a live message moved the history pointer"

            # Exactly what a poll sees when it reads history from the pointer.
            visible = await client.get_messages(
                CHANNEL_ID, min_id=pointer_after_live, reverse=True, limit=None
            )
            results = await listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)
            pointer_after_read = listener.read_last_processed_message_id(CHANNEL_ID)
        return (
            (delivered_10, delivered_11, delivered_12),
            pointer_after_live,
            [message.id for message in visible],
            [(r.message_id, r.result.outcome) for r in results],
            pointer_after_read,
        )

    delivered, pointer_after_live, visible, outcomes, pointer_after_read = asyncio.run(
        scenario()
    )

    assert delivered == (False, False, True)
    assert pointer_after_live == 9, "a live message moved the history pointer"
    assert visible == [10, 11, 12]
    assert outcomes == [(10, "new"), (11, "new"), (12, "duplicate")]
    assert _rows(db_path, "SELECT message_id, source FROM messages ORDER BY message_id") == [
        (10, "poll"),
        (11, "poll"),
        (12, "live"),
    ]
    assert pointer_after_read == 12


def test_live_message_after_a_partial_backfill_does_not_skip_the_rest_of_the_gap(
    tmp_path: Path,
) -> None:
    """The reviewer's D2: a long outage, backfill takes only the first
    `backfill_limit` messages, then one live message arrives."""
    db_path = tmp_path / "store.db"
    _seed_last_processed(db_path, CHANNEL_ID, 1000)

    async def scenario() -> tuple[int, int, int]:
        client = FakeTelegramClient(
            history={CHANNEL_ID: [_chatter_message(i) for i in range(1001, 1013)]}
        )
        async with _started(db_path, client, backfill_limit=5) as listener:
            after_backfill = listener.read_last_processed_message_id(CHANNEL_ID)
            await client.emit(CHANNEL_ID, _chatter_message(1013))
            after_live = listener.read_last_processed_message_id(CHANNEL_ID)
            assert after_live == 1005, "a live message moved the history pointer"
            await listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)
            after_read = listener.read_last_processed_message_id(CHANNEL_ID)
        return after_backfill, after_live, after_read

    after_backfill, after_live, after_read = asyncio.run(scenario())

    assert after_backfill == 1005
    assert after_live == 1005, "a live message moved the history pointer"
    stored = [row[0] for row in _rows(db_path, "SELECT message_id FROM messages ORDER BY message_id")]
    assert stored == list(range(1001, 1014))
    assert after_read == 1013


def test_live_message_that_fails_is_recovered_by_the_next_history_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live N fails in the handler, live N+1 succeeds: the pointer must
    not have moved past N, or the poll can never recover it."""
    db_path = tmp_path / "store.db"
    _seed_last_processed(db_path, CHANNEL_ID, 9)
    real_ingest = messages_repo.ingest
    fail_once = {10}

    def flaky_ingest(conn, **kwargs):
        if kwargs["message_id"] in fail_once:
            fail_once.discard(kwargs["message_id"])
            raise RuntimeError("simulated write failure")
        return real_ingest(conn, **kwargs)

    monkeypatch.setattr(messages_repo, "ingest", flaky_ingest)

    async def scenario() -> tuple[int, int, int]:
        client = FakeTelegramClient()
        async with _started(db_path, client) as listener:
            await client.emit(CHANNEL_ID, _chatter_message(10))
            await client.emit(CHANNEL_ID, _chatter_message(11))
            errors = listener.handler_error_count
            pointer_after_live = listener.read_last_processed_message_id(CHANNEL_ID)
            assert pointer_after_live == 9, "a live message moved the history pointer"
            await listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)
            pointer_after_read = listener.read_last_processed_message_id(CHANNEL_ID)
        return errors, pointer_after_live, pointer_after_read

    errors, pointer_after_live, pointer_after_read = asyncio.run(scenario())

    assert errors == 1
    assert pointer_after_live == 9, "a live message moved the history pointer"
    assert _rows(db_path, "SELECT message_id, source FROM messages ORDER BY message_id") == [
        (10, "poll"),
        (11, "live"),
    ]
    assert pointer_after_read == 11


@pytest.mark.parametrize(
    ("source", "pointer_after"),
    [("live", 9), ("backfill", 10), ("poll", 10)],
)
def test_only_history_sources_move_the_pointer(
    tmp_path: Path, source: str, pointer_after: int
) -> None:
    db_path = tmp_path / "store.db"
    _seed_last_processed(db_path, CHANNEL_ID, 9)
    incoming = IncomingMessage(
        channel_id=CHANNEL_ID,
        message_id=10,
        text=CHATTER_TEXT,
        posted_at_utc=POSTED,
        edit_date_utc=None,
        reply_to_message_id=None,
    )

    async def scenario() -> int:
        client = FakeTelegramClient()
        async with _started(db_path, client) as listener:
            await listener.ingest_message(incoming, channel_cfg=CHANNEL, source=source)
            return listener.read_last_processed_message_id(CHANNEL_ID)

    assert asyncio.run(scenario()) == pointer_after


def test_ingest_message_rejects_an_unknown_source(tmp_path: Path) -> None:
    """The pointer rule is keyed on `source`, so a typo must not silently
    pick a side of it."""
    db_path = tmp_path / "store.db"
    incoming = IncomingMessage(
        channel_id=CHANNEL_ID,
        message_id=10,
        text=CHATTER_TEXT,
        posted_at_utc=POSTED,
        edit_date_utc=None,
        reply_to_message_id=None,
    )

    async def scenario() -> None:
        client = FakeTelegramClient()
        async with _started(db_path, client) as listener:
            with pytest.raises(ValueError, match="source"):
                await listener.ingest_message(incoming, channel_cfg=CHANNEL, source="Poll")

    asyncio.run(scenario())
    assert _count(db_path, "messages") == 0


# -- startup order -----------------------------------------------------------


def test_get_dialogs_is_called_before_handlers_are_registered(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"

    async def scenario() -> list[str]:
        client = FakeTelegramClient(history={CHANNEL_ID: [_signal_message()]})
        async with _started(db_path, client):
            return list(client.calls)

    calls = asyncio.run(scenario())

    assert "get_dialogs" in calls
    assert "add_event_handler" in calls
    assert calls.index("connect") < calls.index("get_dialogs")
    assert calls.index("get_dialogs") < calls.index("add_event_handler")
    # Backfill (get_messages) also runs before handlers are registered,
    # so a message cannot be ingested twice by both paths at once.
    assert calls.index("get_messages") < calls.index("add_event_handler")


def test_backfill_ingests_messages_newer_than_last_processed_and_advances_it(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "store.db"
    _seed_last_processed(db_path, CHANNEL_ID, 4000)

    async def scenario() -> None:
        client = FakeTelegramClient(
            history={
                CHANNEL_ID: [
                    _chatter_message(3999),  # older than last_processed
                    _signal_message(SIGNAL_MESSAGE_ID),  # 4001, newer
                ]
            }
        )
        async with _started(db_path, client):
            pass

    asyncio.run(scenario())

    assert _rows(db_path, "SELECT message_id, source FROM messages") == [
        (SIGNAL_MESSAGE_ID, "backfill")
    ]
    assert _last_processed(db_path, CHANNEL_ID) == SIGNAL_MESSAGE_ID


def test_first_run_seeds_the_pointer_from_the_newest_message_and_ingests_no_history(
    tmp_path: Path,
) -> None:
    """The reviewer's D3: with the pointer at 0, `get_messages(min_id=0,
    reverse=True, limit=N)` returns the first N messages the channel ever
    posted -- possibly years-old signals, each queuing ENTRY outbox rows.
    A first run must instead start from the listener's start: seed the
    pointer at the newest message dated before it (all of this history
    is 15 minutes old) and ingest nothing historical."""
    db_path = tmp_path / "store.db"
    old_history = [_chatter_message(i) for i in range(1, 21)]
    old_history[2] = _signal_message(3)  # an ancient signal, never to be traded

    async def scenario():
        client = FakeTelegramClient(history={CHANNEL_ID: old_history})
        async with _started(db_path, client, backfill_limit=5) as listener:
            rows_after_start = _count(db_path, "messages")
            pointer_after_start = listener.read_last_processed_message_id(CHANNEL_ID)
            seed_calls = list(client.get_messages_calls)

            # Going forward, everything after the first run is recorded --
            # by the update stream, and by a history read for what it drops.
            await client.emit(CHANNEL_ID, _chatter_message(21))
            with client.blackout():
                await client.emit(CHANNEL_ID, _chatter_message(22))
            await listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)
        return rows_after_start, pointer_after_start, seed_calls

    rows_after_start, pointer_after_start, seed_calls = asyncio.run(scenario())

    assert rows_after_start == 0, "first run ingested the channel's old history"
    assert pointer_after_start == 20
    anchor, forward = seed_calls
    assert (anchor.entity, anchor.limit, anchor.reverse, anchor.offset_date) == (
        CHANNEL_ID,
        1,
        False,
        NOW - listener_module.FIRST_RUN_SEED_MARGIN,
    ), "the seed must ask for exactly the newest message before the start cutoff"
    assert (forward.min_id, forward.reverse) == (20, True), "then read on from the anchor"
    assert _rows(db_path, "SELECT message_id, source FROM messages ORDER BY message_id") == [
        (21, "live"),
        (22, "poll"),
    ]
    assert _count(db_path, "outbox") == 0


def test_first_run_on_an_empty_channel_does_not_reseed_past_the_first_message(
    tmp_path: Path,
) -> None:
    """An empty channel seeds nothing (there is no newest message), so the
    pointer stays 0. The next history read must then read from 0 -- not
    seed again, which would jump past a first post the stream dropped."""
    db_path = tmp_path / "store.db"

    async def scenario() -> tuple[int, int]:
        client = FakeTelegramClient()
        async with _started(db_path, client) as listener:
            pointer_after_start = listener.read_last_processed_message_id(CHANNEL_ID)
            with client.blackout():
                await client.emit(CHANNEL_ID, _chatter_message(1))
            await listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)
            return pointer_after_start, listener.read_last_processed_message_id(CHANNEL_ID)

    pointer_after_start, pointer_after_read = asyncio.run(scenario())

    assert pointer_after_start == 0
    assert _rows(db_path, "SELECT message_id, source FROM messages") == [(1, "poll")]
    assert pointer_after_read == 1


def test_a_failed_first_run_seed_is_retried_as_a_seed_not_a_read_from_zero(
    tmp_path: Path,
) -> None:
    """If the seed RPC fails at startup, the pointer is still 0 when the
    poll first runs. Reading from 0 then would be D3 all over again."""
    db_path = tmp_path / "store.db"

    async def scenario() -> tuple[int, tuple, int]:
        client = FakeTelegramClient(
            history={CHANNEL_ID: [_chatter_message(i) for i in range(1, 21)]}
        )
        real_get_messages = client.get_messages
        failures = [ConnectionError("simulated RPC failure")]

        async def flaky_get_messages(*args, **kwargs):
            if failures:
                raise failures.pop()
            return await real_get_messages(*args, **kwargs)

        client.get_messages = flaky_get_messages
        async with _started(db_path, client) as listener:
            pointer_after_start = listener.read_last_processed_message_id(CHANNEL_ID)
            results = await listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)
            return (
                pointer_after_start,
                results,
                listener.read_last_processed_message_id(CHANNEL_ID),
            )

    pointer_after_start, results, pointer_after_read = asyncio.run(scenario())

    assert pointer_after_start == 0
    assert results == ()
    assert _count(db_path, "messages") == 0
    assert pointer_after_read == 20


def _dated(message_id: int, date: datetime, text: str = CHATTER_TEXT) -> FakeMessage:
    return FakeMessage(id=message_id, message=text, date=date)


def test_the_first_run_seed_keeps_everything_since_the_start_less_the_margin(
    tmp_path: Path,
) -> None:
    """The seed's cutoff is the listener's start minus the clock-skew
    margin: a message 1 s inside the margin is ingested, one 1 s outside
    it is not, and so is one posted while startup was still under way
    (handlers were not registered yet, so the stream could not have
    delivered it)."""
    db_path = tmp_path / "store.db"
    cutoff = NOW - listener_module.FIRST_RUN_SEED_MARGIN
    history = [
        _dated(1, NOW - timedelta(hours=2)),
        _dated(2, cutoff - timedelta(seconds=1)),
        _dated(3, cutoff + timedelta(seconds=1)),
        _dated(4, NOW + timedelta(seconds=1)),
    ]

    async def scenario() -> None:
        async with _started(db_path, FakeTelegramClient(history={CHANNEL_ID: history})):
            pass

    asyncio.run(scenario())

    assert _rows(db_path, "SELECT message_id, source FROM messages ORDER BY message_id") == [
        (3, "backfill"),
        (4, "backfill"),
    ]
    assert _last_processed(db_path, CHANNEL_ID) == 4


class _NoAnchorClient(FakeTelegramClient):
    """Answers every `offset_date` query with nothing -- as a server quirk
    or a hidden message might -- while its history is really there."""

    async def get_messages(self, *args, **kwargs):
        if "offset_date" in kwargs:
            self.calls.append("get_messages")
            return []
        return await super().get_messages(*args, **kwargs)


@pytest.mark.parametrize("history_age", ["ancient", "all_after_the_start"])
def test_a_seed_with_no_anchor_reads_from_zero_only_if_nothing_is_older(
    tmp_path: Path, history_age: str
) -> None:
    """No message before the cutoff means the read goes from 0. That is
    right for a channel whose every post is newer than the start, and the
    D3 disaster for one with years of history -- so it is verified
    against the channel's oldest message, never assumed."""
    db_path = tmp_path / "store.db"
    if history_age == "ancient":
        client = _NoAnchorClient(
            history={
                CHANNEL_ID: [
                    _dated(1, NOW - timedelta(days=400)),
                    _dated(2, NOW - timedelta(days=400), SIGNAL_TEXT),
                ]
            }
        )
    else:
        client = FakeTelegramClient(
            history={CHANNEL_ID: [_dated(1, NOW + timedelta(seconds=1)), _dated(2, NOW)]}
        )

    async def scenario():
        async with _started(db_path, client) as listener:  # the startup seed
            if history_age == "ancient":
                with pytest.raises(RuntimeError, match="refusing to read its history from 0"):
                    await listener.read_history(
                        CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT
                    )

    asyncio.run(scenario())

    stored = _rows(db_path, "SELECT message_id FROM messages ORDER BY message_id")
    if history_age == "ancient":
        assert stored == [], "a first run read the channel's ancient history"
        assert _count(db_path, "outbox") == 0
        assert _last_processed(db_path, CHANNEL_ID) == 0
    else:
        assert stored == [(1,), (2,)]
        assert _last_processed(db_path, CHANNEL_ID) == 2


@pytest.mark.parametrize("path", ["read", "seed"])
def test_a_hung_backfill_read_cannot_hold_start(tmp_path: Path, path: str) -> None:
    """Backfill runs before handlers are registered and before the poll
    starts: a read hung there would leave a live process that hears
    nothing, forever. `rpc_timeout` is required, so it is bounded; the
    channel is picked up by the next read once Telegram answers again."""
    db_path = tmp_path / "store.db"
    if path == "read":
        _seed_last_processed(db_path, CHANNEL_ID, 9)

    async def scenario():
        client = _ScriptedReadsClient()
        client.script = ["hang"]
        listener = _build_listener(db_path, client, backfill_rpc_timeout=0.1)
        try:
            await asyncio.wait_for(listener.start(), timeout=2.0)
            calls_after_start = list(client.calls)
            client.add_to_history(CHANNEL_ID, _dated(10, NOW + timedelta(seconds=10)))
            await listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)
        finally:
            await listener.aclose()
        return calls_after_start

    calls_after_start = asyncio.run(scenario())

    assert "add_event_handler" in calls_after_start
    assert _rows(db_path, "SELECT message_id, source FROM messages") == [(10, "poll")]
    assert _last_processed(db_path, CHANNEL_ID) == 10


def test_read_history_requires_an_rpc_timeout(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"

    async def scenario() -> None:
        async with _started(db_path, FakeTelegramClient()) as listener:
            with pytest.raises(TypeError, match="rpc_timeout"):
                await listener.read_history(CHANNEL, source="poll", limit=100)

    asyncio.run(scenario())


# -- a history message that fails -------------------------------------------
#
# A history read walks ids in ascending order and advances the pointer
# per message. If it stepped past a message that failed, the pointer
# would sit above an unstored message and no later read could ever see
# it -- the Critical's failure shape. So a failure stops the batch; the
# pointer rests at the last contiguous success and the next read retries.
# Only a message that fails MAX_HISTORY_ATTEMPTS times is skipped, loudly.


def _hold_a_real_write_lock_during_first_ingest_of(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, message_id: int
) -> list[int]:
    """The first ingest of `message_id` runs while a second connection
    holds `BEGIN IMMEDIATE`, so the writer's INSERT waits out a (shortened,
    300 ms) busy_timeout and fails with a real `database is locked` --
    the transient failure, not a mocked exception."""
    import sqlite3

    monkeypatch.setattr(db, "_BUSY_TIMEOUT_MS", 300)
    real_ingest = messages_repo.ingest
    locked_attempts: list[int] = []

    def ingest_under_a_foreign_lock(conn, **kwargs):
        if kwargs["message_id"] == message_id and not locked_attempts:
            locked_attempts.append(message_id)
            other = sqlite3.connect(db_path, timeout=0)
            other.execute("BEGIN IMMEDIATE")
            try:
                return real_ingest(conn, **kwargs)
            finally:
                other.rollback()
                other.close()
        return real_ingest(conn, **kwargs)

    monkeypatch.setattr(messages_repo, "ingest", ingest_under_a_foreign_lock)
    return locked_attempts


def _stored(db_path: Path) -> list[int]:
    return [row[0] for row in _rows(db_path, "SELECT message_id FROM messages ORDER BY message_id")]


def _history_skipped(db_path: Path) -> int:
    rows = _rows(db_path, "SELECT SUM(value) FROM metrics WHERE name = 'history_skipped'")
    return rows[0][0] or 0


def test_a_transient_history_failure_stops_the_batch_and_the_next_read_retries_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-reviewer's reproduction: a real SQLite lock on the first
    ingest of 11. Shipped round 1 stepped past it -- pointer 12, store
    [10, 12], and 11 was lost for good, even across a restart."""
    db_path = tmp_path / "store.db"
    _seed_last_processed(db_path, CHANNEL_ID, 9)
    locked = _hold_a_real_write_lock_during_first_ingest_of(db_path, monkeypatch, 11)

    async def scenario():
        client = FakeTelegramClient(
            history={CHANNEL_ID: [_chatter_message(i) for i in (10, 11, 12)]}
        )
        async with _started(db_path, client) as listener:  # backfill is read 1
            after_backfill = (listener.read_last_processed_message_id(CHANNEL_ID), _stored(db_path))
            second = await listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)
            after_second = (listener.read_last_processed_message_id(CHANNEL_ID), _stored(db_path))
            errors = listener.handler_errors
        return after_backfill, second, after_second, errors

    after_backfill, second, after_second, errors = asyncio.run(scenario())

    assert locked == [11], "the real lock never engaged"
    assert after_backfill == (10, [10]), "the pointer moved past a message that failed"
    assert [(r.message_id, r.result.outcome) for r in second] == [(11, "new"), (12, "new")]
    assert after_second == (12, [10, 11, 12])
    assert [(e.message_id, e.source) for e in errors] == [(11, "backfill")]
    assert "database is locked" in errors[0].error
    assert _history_skipped(db_path) == 0


@pytest.mark.parametrize("failing_step", ["normalise", "ingest"])
def test_a_message_that_always_fails_is_skipped_after_exactly_three_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing_step: str,
) -> None:
    """A genuine poison message must not wedge its channel forever, and
    must not be skipped on a single failure either: exactly
    MAX_HISTORY_ATTEMPTS reads, then skip it -- pointer moved past that one
    id, CRITICAL log, durable `history_skipped` metric -- and later
    messages flow again."""
    db_path = tmp_path / "store.db"
    _seed_last_processed(db_path, CHANNEL_ID, 9)
    poison_id = 11

    if failing_step == "normalise":
        real_normalise_message = listener_module.normalise_message

        def poisoned_normalise(message, *, channel_id):
            if message.id == poison_id:
                raise ValueError("cannot normalise this one")
            return real_normalise_message(message, channel_id=channel_id)

        monkeypatch.setattr(listener_module, "normalise_message", poisoned_normalise)
    else:
        real_ingest = messages_repo.ingest

        def poisoned_ingest(conn, **kwargs):
            if kwargs["message_id"] == poison_id:
                raise ValueError("cannot ingest this one")
            return real_ingest(conn, **kwargs)

        monkeypatch.setattr(messages_repo, "ingest", poisoned_ingest)

    async def scenario():
        client = FakeTelegramClient(
            history={CHANNEL_ID: [_chatter_message(i) for i in (10, 11, 12)]}
        )
        states = []
        async with _started(db_path, client) as listener:  # backfill is read 1
            states.append((listener.read_last_processed_message_id(CHANNEL_ID), _stored(db_path)))
            for _ in range(3):  # reads 2, 3 and a 4th that finds nothing new
                await listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)
                states.append(
                    (listener.read_last_processed_message_id(CHANNEL_ID), _stored(db_path))
                )
            errors = listener.handler_errors
        return states, errors

    with caplog.at_level("CRITICAL", logger=listener_module.__name__):
        states, errors = asyncio.run(scenario())

    # Reads 1 and 2: the pointer never passes the failed id.
    assert states[0] == (10, [10])
    assert states[1] == (10, [10])
    # Read 3: third failure -> skipped, and 12 flows in the same read.
    assert states[2] == (12, [10, 12])
    assert states[3] == (12, [10, 12])
    assert [(e.message_id, e.source) for e in errors] == [
        (poison_id, "backfill"),
        (poison_id, "poll"),
        (poison_id, "poll"),
    ]
    assert _history_skipped(db_path) == 1
    critical = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert len(critical) == 1 and f"message_id {poison_id}" in critical[0].getMessage()
    assert listener_module.MAX_HISTORY_ATTEMPTS == 3


def test_a_skip_that_cannot_read_the_clock_is_retried_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skip's day key is computed inside its `try`: if that fails, the
    skip returns False and the next read tries again, instead of the
    error escaping `ingest_history()` and the read."""
    db_path = tmp_path / "store.db"
    _seed_last_processed(db_path, CHANNEL_ID, 9)
    real_ingest = messages_repo.ingest
    real_day_key_for = metrics.day_key_for

    def poisoned_ingest(conn, **kwargs):
        if kwargs["message_id"] == 11:
            raise ValueError("cannot ingest this one")
        return real_ingest(conn, **kwargs)

    def broken_day_key_for(now_utc):
        raise RuntimeError("the clock broke")

    monkeypatch.setattr(messages_repo, "ingest", poisoned_ingest)

    async def scenario():
        client = FakeTelegramClient(history={CHANNEL_ID: [_chatter_message(11)]})
        async with _started(db_path, client) as listener:  # backfill: attempt 1
            await listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)
            monkeypatch.setattr(metrics, "day_key_for", broken_day_key_for)
            third = await listener.read_history(  # attempt 3: the skip itself fails
                CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT
            )
            pointer_after_third = listener.read_last_processed_message_id(CHANNEL_ID)
            monkeypatch.setattr(metrics, "day_key_for", real_day_key_for)
            await listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)
        return third, pointer_after_third

    third, pointer_after_third = asyncio.run(scenario())

    assert [(r.message_id, r.result) for r in third] == [(11, None)]
    assert pointer_after_third == 9, "a skip that failed still moved the pointer"
    assert _last_processed(db_path, CHANNEL_ID) == 11  # the next read's skip worked
    assert _history_skipped(db_path) == 1


class _SwitchablyDeadWriter(db.Writer):
    """A writer that can be made to answer every job with WriterDeadError
    -- and then brought back. Real writers do not come back; this exists
    only to observe that the dead-writer failures were never *counted*."""

    dead = False

    def submit(self, fn):
        if self.dead:
            import concurrent.futures

            future = concurrent.futures.Future()
            error = db.WriterDeadError("the writer thread died (simulated)")
            future.set_exception(error)
            return future
        return super().submit(fn)


def test_a_dead_writer_never_counts_toward_a_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WriterDeadError means "there is no writer", not "this message is
    bad". Two dead-writer failures, then one genuine failure once the
    writer is back: had the dead ones counted, that genuine failure would
    be the third strike and 11 would be skipped for good."""
    db_path = tmp_path / "store.db"
    _seed_last_processed(db_path, CHANNEL_ID, 9)
    writer = _SwitchablyDeadWriter(db_path)
    genuine_failures: list[int] = []
    real_ingest = messages_repo.ingest

    def fails_when_told(conn, **kwargs):
        if genuine_failures:
            genuine_failures.pop()
            raise ValueError("a genuine, transient failure")
        return real_ingest(conn, **kwargs)

    monkeypatch.setattr(messages_repo, "ingest", fails_when_told)

    async def scenario():
        client = FakeTelegramClient()
        async with _started(db_path, client, writer=writer) as listener:
            client.add_to_history(CHANNEL_ID, _chatter_message(11))  # after backfill
            writer.dead = True
            for _ in range(listener_module.MAX_HISTORY_ATTEMPTS - 1):
                results = await listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)
                assert [(r.message_id, r.result) for r in results] == [(11, None)]
            writer.dead = False
            genuine_failures.append(1)
            failed = await listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)
            assert [(r.message_id, r.result) for r in failed] == [(11, None)]
            assert listener.read_last_processed_message_id(CHANNEL_ID) == 9
            await listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)

    asyncio.run(scenario())

    assert _rows(db_path, "SELECT message_id, source FROM messages") == [(11, "poll")]
    assert _last_processed(db_path, CHANNEL_ID) == 11
    assert _history_skipped(db_path) == 0

class _GatedReadsClient(FakeTelegramClient):
    """Once `gate` is set, every `get_messages()` waits at it, and the
    number of reads in flight per channel is tracked."""

    gate: asyncio.Event | None = None

    async def get_messages(self, *args, **kwargs):
        if self.gate is None:
            return await super().get_messages(*args, **kwargs)
        channel_id = self._channel_id_of(args[0])
        self.in_flight[channel_id] = self.in_flight.get(channel_id, 0) + 1
        self.peak[channel_id] = max(self.peak.get(channel_id, 0), self.in_flight[channel_id])
        self.peak_total = max(self.peak_total, sum(self.in_flight.values()))
        try:
            await self.gate.wait()
            return await super().get_messages(*args, **kwargs)
        finally:
            self.in_flight[channel_id] -= 1


def test_history_reads_of_one_channel_never_overlap_but_other_channels_do(
    tmp_path: Path,
) -> None:
    """Two overlapping reads of one channel would fetch the same batch from
    the same pointer and double-count a failure towards its skip. The
    second must wait and then resume from where the first left the
    pointer; a read of another channel must not wait at all."""
    db_path = tmp_path / "store.db"
    other = ChannelCfg(
        channel_id=-1002222222222, name="Channel B", parser="mmh", ordinal=2, enabled=True
    )
    _seed_last_processed(db_path, CHANNEL_ID, 9)
    _seed_last_processed(db_path, other.channel_id, 19)

    async def scenario():
        client = _GatedReadsClient()
        async with _started(db_path, client, channels=(CHANNEL, other)) as listener:
            client.add_to_history(CHANNEL_ID, _chatter_message(10))
            client.add_to_history(CHANNEL_ID, _chatter_message(11))
            client.add_to_history(other.channel_id, _chatter_message(20))
            client.gate, client.in_flight, client.peak, client.peak_total = (
                asyncio.Event(), {}, {}, 0
            )
            reads = [
                asyncio.create_task(listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)),
                asyncio.create_task(listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)),
                asyncio.create_task(listener.read_history(other, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)),
            ]
            await _wait_for(lambda: sum(client.in_flight.values()) == 2)
            await asyncio.sleep(0.05)  # every chance for a second read of A to get in
            client.gate.set()
            results = await asyncio.wait_for(asyncio.gather(*reads), timeout=2.0)
        return client.peak, client.peak_total, results

    peak, peak_total, results = asyncio.run(scenario())

    assert peak[CHANNEL_ID] == 1, "two history reads of one channel overlapped"
    assert peak_total == 2, "a read of one channel waited for another channel's"
    first, second, of_other = (
        [(r.message_id, r.result.outcome) for r in result] for result in results
    )
    assert first == [(10, "new"), (11, "new")]
    assert second == []  # resumed from the pointer the first read left: nothing new
    assert of_other == [(20, "new")]


def test_a_skip_raced_by_an_overlapping_batch_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`read_history` never overlaps reads of one channel, but if a caller
    runs two `ingest_history` batches at once anyway, both can reach the
    skip of the same poison message. The second skip's bookkeeping must
    not raise `KeyError` for a key the first already cleared (the Task 8
    review's latent bug at `del self._history_attempts[key]`)."""
    db_path = tmp_path / "store.db"
    _seed_last_processed(db_path, CHANNEL_ID, 9)
    real_ingest = messages_repo.ingest

    def poisoned_ingest(conn, **kwargs):
        if kwargs["message_id"] == 11:
            raise ValueError("cannot ingest this one")
        return real_ingest(conn, **kwargs)

    monkeypatch.setattr(messages_repo, "ingest", poisoned_ingest)

    async def scenario():
        client = FakeTelegramClient(history={CHANNEL_ID: [_chatter_message(11)]})
        async with _started(db_path, client) as listener:  # backfill: attempt 1
            await listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)  # attempt 2
            batch = [_chatter_message(11)]
            return await asyncio.gather(
                listener.ingest_history(CHANNEL, batch, min_id=9, source="poll"),
                listener.ingest_history(CHANNEL, batch, min_id=9, source="poll"),
            )

    both = asyncio.run(scenario())

    assert [[(r.message_id, r.result) for r in batch] for batch in both] == [
        [(11, None)],
        [(11, None)],
    ]
    assert _last_processed(db_path, CHANNEL_ID) == 11
    assert _stored(db_path) == []


def test_backfill_asks_only_for_messages_newer_than_last_processed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "store.db"
    _seed_last_processed(db_path, CHANNEL_ID, 4000)

    async def scenario() -> list:
        client = FakeTelegramClient(history={CHANNEL_ID: [_signal_message()]})
        async with _started(db_path, client):
            return list(client.get_messages_calls)

    calls = asyncio.run(scenario())

    assert len(calls) == 1
    assert calls[0].entity == CHANNEL_ID
    assert calls[0].min_id == 4000
    assert calls[0].reverse is True
    # Always explicit: Telethon's own default is ONE message.
    assert calls[0].limit == listener_module.DEFAULT_BACKFILL_LIMIT


class _ScriptedReadsClient(FakeTelegramClient):
    """Each `get_messages()` pops one step: None reads normally, "hang"
    never answers (until cancelled), an exception is raised."""

    script: list = []

    async def get_messages(self, *args, **kwargs):
        step = self.script.pop(0) if self.script else None
        if step == "hang":
            await asyncio.Event().wait()
        if isinstance(step, BaseException):
            raise step
        return await super().get_messages(*args, **kwargs)


@pytest.mark.parametrize("path", ["read", "seed_anchor", "seed_oldest_check"])
def test_read_history_rpc_timeout_bounds_the_read_and_the_first_run_seed(
    tmp_path: Path, path: str
) -> None:
    """Every RPC `read_history` can make must honour `rpc_timeout`, or one
    hung RPC hangs the poll: the history read; the first-run seed's anchor
    query; and, when there is no anchor, the seed's check of the oldest
    message."""
    db_path = tmp_path / "store.db"
    if path == "read":
        _seed_last_processed(db_path, CHANNEL_ID, 9)

    async def scenario() -> float:
        client = _ScriptedReadsClient()
        # Startup backfill: a normal read, or a failed seed (so the
        # channel is still unseeded and the next call seeds). Then the
        # step that hangs; for the oldest check, the (empty) channel's
        # anchor query answers first.
        client.script = {
            "read": [None, "hang"],
            "seed_anchor": [ConnectionError("startup blip"), "hang"],
            "seed_oldest_check": [ConnectionError("startup blip"), None, "hang"],
        }[path]
        async with _started(db_path, client) as listener:
            started = asyncio.get_running_loop().time()
            with pytest.raises(TimeoutError):
                # The outer bound only keeps a broken read_history from
                # hanging the suite; it takes 2 s, which fails `elapsed`.
                await asyncio.wait_for(
                    listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=0.05),
                    timeout=2.0,
                )
            return asyncio.get_running_loop().time() - started

    elapsed = asyncio.run(scenario())

    assert elapsed < 1.0
    assert _last_processed(db_path, CHANNEL_ID) == (9 if path == "read" else 0)


@pytest.mark.parametrize("rpc_timeout", [0, -1, True, "20"])
def test_read_history_refuses_a_nonsense_rpc_timeout(tmp_path: Path, rpc_timeout) -> None:
    db_path = tmp_path / "store.db"

    async def scenario() -> None:
        async with _started(db_path, FakeTelegramClient()) as listener:
            with pytest.raises(ValueError, match="rpc_timeout"):
                await listener.read_history(
                    CHANNEL, source="poll", limit=100, rpc_timeout=rpc_timeout
                )

    asyncio.run(scenario())


def test_writer_connects_before_any_reader_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`db.connect()` runs `migrate()` on the calling thread. If a reader
    were the first connection against a brand-new file, schema DDL would
    run on the event-loop thread."""
    db_path = tmp_path / "store.db"
    assert not db_path.exists()

    opened: list[tuple[str, str]] = []
    real_connect = db.connect

    def recording_connect(path, *, role):
        opened.append((role, threading.current_thread().name))
        return real_connect(path, role=role)

    monkeypatch.setattr(db, "connect", recording_connect)

    async def scenario() -> None:
        client = FakeTelegramClient()
        async with _started(db_path, client):
            await client.emit(CHANNEL_ID, _signal_message())

    asyncio.run(scenario())

    roles = [role for role, _ in opened]
    assert roles[0] == "writer", f"first connection was not the writer: {opened}"
    assert "reader" in roles, "the listener never opened a reader at all"
    assert opened[0][1] == "xaucopier-writer"


def test_start_fails_promptly_and_non_zero_when_the_writer_cannot_connect(
    tmp_path: Path,
) -> None:
    """The reviewer's D6: the database's parent directory is missing, so
    the writer thread dies in connect(). start() used to wait forever on
    the probe, never reaching Telegram, while the process looked alive."""
    db_path = tmp_path / "no_such_dir" / "store.db"

    async def scenario():
        client = FakeTelegramClient()
        listener = _build_listener(db_path, client)
        loop = asyncio.get_running_loop()
        started = loop.time()
        # 10 s is a hang guard for the test itself; the default barrier
        # timeout is 30 s, and a dead writer must beat it by a mile.
        exit_code = await asyncio.wait_for(run_listener(listener), timeout=10.0)
        return exit_code, loop.time() - started, list(client.calls)

    exit_code, elapsed, calls = asyncio.run(scenario())

    assert exit_code == EXIT_LISTENER_STOPPED
    assert elapsed < 5.0, f"start() took {elapsed:.1f}s to notice a dead writer"
    assert "connect" not in calls, "the listener reached Telegram without a writer"


def test_start_raises_listener_startup_error_carrying_the_writer_death(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "no_such_dir" / "store.db"

    async def scenario() -> BaseException:
        client = FakeTelegramClient()
        listener = _build_listener(db_path, client)
        try:
            with pytest.raises(listener_module.ListenerStartupError) as excinfo:
                await asyncio.wait_for(listener.start(), timeout=10.0)
        finally:
            await listener.aclose()
        return excinfo.value

    error = asyncio.run(scenario())

    assert isinstance(error, ListenerStopped), "a failed start must map to a non-zero exit"
    assert isinstance(error.__cause__, db.WriterDeadError)
    assert "unable to open database file" in str(error)


class _NeverConnectingWriter(db.Writer):
    """A writer whose thread never starts, so the startup probe can
    never run: the stuck-but-not-dead case the barrier timeout exists
    for."""

    def start(self) -> None:
        pass


def test_start_times_out_when_the_writer_never_connects(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"

    async def scenario():
        client = FakeTelegramClient()
        listener = _build_listener(
            db_path,
            client,
            writer=_NeverConnectingWriter(db_path),
            writer_ready_timeout=0.2,
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        exit_code = await asyncio.wait_for(run_listener(listener), timeout=10.0)
        return exit_code, loop.time() - started, list(client.calls)

    exit_code, elapsed, calls = asyncio.run(scenario())

    assert exit_code == EXIT_LISTENER_STOPPED
    assert elapsed < 5.0
    assert "connect" not in calls


def test_poll_hook_starts_only_after_backfill_and_handler_registration(
    tmp_path: Path,
) -> None:
    """Task 9 plugs its poll in here; it must not run before the update
    stream is live, or it would race backfill over the same rows.

    The snapshot is taken from *inside* the hook, the moment it first
    runs -- the only place the ordering is observable. A snapshot taken
    after `start()` returns sees the same calls whatever order they
    happened in (the reviewer's M7 passed the old version of this test)."""
    db_path = tmp_path / "store.db"
    _seed_last_processed(db_path, CHANNEL_ID, 4000)
    seen_by_poll: dict[str, object] = {}

    async def poll(listener: Listener) -> None:
        seen_by_poll["calls"] = list(listener.client.calls)
        seen_by_poll["pointer"] = listener.read_last_processed_message_id(CHANNEL_ID)
        seen_by_poll["rows"] = _count(db_path, "messages")
        await asyncio.sleep(3600)

    async def scenario() -> None:
        client = FakeTelegramClient(history={CHANNEL_ID: [_signal_message()]})
        async with _started(db_path, client, poll_hook=poll):
            await _wait_for(lambda: "calls" in seen_by_poll)

    asyncio.run(scenario())

    assert seen_by_poll["calls"] == [
        "connect",
        "get_dialogs",
        "get_messages",
        "add_event_handler",
    ]
    # Backfill had *finished* -- its write landed -- not merely started.
    assert seen_by_poll["pointer"] == SIGNAL_MESSAGE_ID
    assert seen_by_poll["rows"] == 1


def test_poll_task_is_cancelled_on_close(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"

    async def poll(listener: Listener) -> None:
        await asyncio.sleep(3600)

    async def scenario():
        client = FakeTelegramClient()
        listener = _build_listener(db_path, client, poll_hook=poll)
        await listener.start()
        await asyncio.sleep(0)
        task = listener.poll_task
        await listener.aclose()
        return task

    task = asyncio.run(scenario())

    assert task is not None
    assert task.done()


# -- stopping ----------------------------------------------------------------


def test_run_raises_listener_stopped_when_run_until_disconnected_returns(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "store.db"

    async def scenario() -> None:
        client = FakeTelegramClient()
        listener = _build_listener(db_path, client)
        task = asyncio.create_task(listener.run())
        await _wait_for(lambda: "run_until_disconnected" in client.calls)
        await client.disconnect()
        try:
            with pytest.raises(ListenerStopped):
                await asyncio.wait_for(task, timeout=2.0)
        finally:
            await listener.aclose()

    asyncio.run(scenario())


def test_run_listener_returns_a_non_zero_exit_code_when_the_client_stops(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "store.db"

    async def scenario() -> int:
        client = FakeTelegramClient()
        listener = _build_listener(db_path, client)
        task = asyncio.create_task(run_listener(listener))
        await _wait_for(lambda: "run_until_disconnected" in client.calls)
        await client.disconnect()
        return await asyncio.wait_for(task, timeout=2.0)

    exit_code = asyncio.run(scenario())

    assert exit_code != 0
    assert exit_code == EXIT_LISTENER_STOPPED


async def _failing_poll(listener: Listener) -> None:
    await asyncio.sleep(0)
    raise RuntimeError("poll blew up")


def test_a_poll_that_raises_stops_the_listener_non_zero(tmp_path: Path) -> None:
    """The poll is the safety net for everything the update stream drops.
    If it dies while `run_until_disconnected()` carries on, the process
    looks healthy and silently misses every blacked-out message."""
    db_path = tmp_path / "store.db"

    async def scenario() -> tuple[int, list[str]]:
        client = FakeTelegramClient()
        listener = _build_listener(db_path, client, poll_hook=_failing_poll)
        exit_code = await asyncio.wait_for(run_listener(listener), timeout=2.0)
        return exit_code, list(client.calls)

    exit_code, calls = asyncio.run(scenario())

    assert exit_code == EXIT_LISTENER_STOPPED
    assert calls[-1] == "disconnect"


def test_run_raises_listener_stopped_carrying_the_poll_failure(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"

    async def scenario() -> BaseException:
        client = FakeTelegramClient()
        listener = _build_listener(db_path, client, poll_hook=_failing_poll)
        try:
            with pytest.raises(ListenerStopped) as excinfo:
                await asyncio.wait_for(listener.run(), timeout=2.0)
        finally:
            await listener.aclose()
        return excinfo.value

    error = asyncio.run(scenario())

    assert isinstance(error.__cause__, RuntimeError)
    assert "poll blew up" in str(error)


def test_a_poll_that_returns_stops_the_listener_non_zero(tmp_path: Path) -> None:
    """Returning is no better than raising: either way nothing is polling."""
    db_path = tmp_path / "store.db"

    async def returning_poll(listener: Listener) -> None:
        await asyncio.sleep(0)

    async def scenario() -> int:
        client = FakeTelegramClient()
        listener = _build_listener(db_path, client, poll_hook=returning_poll)
        return await asyncio.wait_for(run_listener(listener), timeout=2.0)

    assert asyncio.run(scenario()) == EXIT_LISTENER_STOPPED


# -- a writer that dies after startup ------------------------------------------
#
# WriterDeadError is permanent for the process. A listener that keeps
# running without a writer records nothing while its supervisor sees a
# healthy process (the re-reviewer's W4) -- it must stop, non-zero.


def _kill_the_writer(conn) -> None:
    raise SystemExit("simulated writer-thread death")


async def _kill_writer_of(listener: Listener) -> None:
    killed = listener.writer.submit(_kill_the_writer)
    with pytest.raises(db.WriterDeadError):
        await asyncio.wrap_future(killed)


def test_a_writer_that_dies_mid_run_stops_the_listener_non_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"

    async def scenario():
        client = FakeTelegramClient()
        listener = _build_listener(db_path, client)
        task = asyncio.create_task(run_listener(listener))
        await _wait_for(lambda: "run_until_disconnected" in client.calls)
        await _kill_writer_of(listener)

        await client.emit(CHANNEL_ID, _chatter_message(1))  # the first write that notices
        exit_code = await asyncio.wait_for(task, timeout=2.0)
        return exit_code, listener.handler_errors, list(client.calls)

    exit_code, errors, calls = asyncio.run(scenario())

    assert exit_code == EXIT_LISTENER_STOPPED
    assert [(e.message_id, e.source) for e in errors] == [(1, "live")]
    assert errors[0].error.startswith("WriterDeadError")
    assert calls[-1] == "disconnect"


def test_run_raises_listener_stopped_carrying_the_writer_death(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"

    async def scenario() -> BaseException:
        client = FakeTelegramClient()
        listener = _build_listener(db_path, client)
        run = asyncio.create_task(listener.run())
        try:
            await _wait_for(lambda: "run_until_disconnected" in client.calls)
            await _kill_writer_of(listener)
            await client.emit(CHANNEL_ID, _chatter_message(1))
            with pytest.raises(ListenerStopped) as excinfo:
                await asyncio.wait_for(run, timeout=2.0)
        finally:
            await listener.aclose()
        return excinfo.value

    error = asyncio.run(scenario())

    assert isinstance(error.__cause__, db.WriterDeadError)
    assert isinstance(error.__cause__.__cause__, SystemExit)
    assert "writer" in str(error)


def test_a_writer_death_seen_by_a_history_read_stops_the_listener(tmp_path: Path) -> None:
    """The poll path too: a message only history has (a blackout) meets
    the dead writer in read_history(), not in the handler."""
    db_path = tmp_path / "store.db"
    _seed_last_processed(db_path, CHANNEL_ID, 9)

    async def polling(listener: Listener) -> None:
        while True:
            await listener.read_history(CHANNEL, source="poll", limit=100, rpc_timeout=RPC_TIMEOUT)
            await asyncio.sleep(0.01)

    async def scenario():
        client = FakeTelegramClient()
        listener = _build_listener(db_path, client, poll_hook=polling)
        task = asyncio.create_task(run_listener(listener))
        await _wait_for(lambda: "run_until_disconnected" in client.calls)
        await _kill_writer_of(listener)

        with client.blackout():
            await client.emit(CHANNEL_ID, _chatter_message(10))
        exit_code = await asyncio.wait_for(task, timeout=2.0)
        return exit_code, listener.handler_errors

    exit_code, errors = asyncio.run(scenario())

    assert exit_code == EXIT_LISTENER_STOPPED
    assert (errors[0].message_id, errors[0].source) == (10, "poll")
    assert errors[0].error.startswith("WriterDeadError")


async def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.001)


# -- normalisation -----------------------------------------------------------


def test_normalise_event_reads_everything_off_the_event(tmp_path: Path) -> None:
    from fakes.telegram import FakeNewMessageEvent

    edited = datetime(2026, 9, 5, 10, 50, tzinfo=UTC)
    event = FakeNewMessageEvent(
        chat_id=CHANNEL_ID,
        message=FakeMessage(
            id=7,
            message=SIGNAL_TEXT,
            date=POSTED,
            edit_date=edited,
            reply_to_msg_id=3,
        ),
    )

    incoming = normalise_event(event)

    assert incoming == IncomingMessage(
        channel_id=CHANNEL_ID,
        message_id=7,
        text=SIGNAL_TEXT,
        posted_at_utc=POSTED,
        edit_date_utc=edited,
        reply_to_message_id=3,
    )


@pytest.mark.parametrize("field", ["date", "edit_date"])
def test_normalise_rejects_a_naive_datetime_instead_of_relabelling_it(field: str) -> None:
    """Telethon 1.44 always returns aware UTC datetimes, so a naive one
    can only come from test or fake code. Labelling it UTC would silently
    shift a signal's age by the local offset -- hours -- and defeat the
    staleness gate. Refuse it instead. (`FakeMessage` itself rejects naive
    dates, so this uses a bare stand-in.)"""
    naive = datetime(2026, 9, 5, 10, 45)
    message = SimpleNamespace(
        id=7,
        message="hi",
        date=naive if field == "date" else POSTED,
        edit_date=naive if field == "edit_date" else None,
        reply_to_msg_id=None,
    )
    event = SimpleNamespace(chat_id=CHANNEL_ID, message=message)

    with pytest.raises(ValueError, match="naive"):
        normalise_event(event)


def test_normalise_converts_an_aware_non_utc_datetime_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    message = SimpleNamespace(
        id=7,
        message="hi",
        date=datetime(2026, 9, 5, 12, 45, tzinfo=plus_two),
        edit_date=None,
        reply_to_msg_id=None,
    )

    incoming = normalise_event(SimpleNamespace(chat_id=CHANNEL_ID, message=message))

    assert incoming.posted_at_utc == datetime(2026, 9, 5, 10, 45, tzinfo=UTC)
    assert incoming.posted_at_utc.tzinfo is UTC
