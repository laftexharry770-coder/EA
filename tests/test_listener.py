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
from datetime import UTC, datetime
from pathlib import Path

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
from xaucopier.store import db, messages_repo

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


ACCOUNTS = (_account("acct-a", ordinal=1), _account("acct-off", enabled=False, ordinal=2))
ENABLED_ACCOUNT_COUNT = sum(1 for account in ACCOUNTS if account.enabled)


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
    outbox = _rows(db_path, "SELECT account_id, kind, state FROM outbox")
    assert outbox == [("acct-a", "ENTRY", "QUEUED")]
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
    """Every client method the fake exposes appends to `calls`. If the
    handler made *any* round trip -- get_entity, get_reply_message,
    get_messages -- the log would grow while the handler ran."""
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
            results = await listener.read_history(CHANNEL, source="poll", limit=100)
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
            await listener.read_history(CHANNEL, source="poll", limit=100)
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
            await listener.read_history(CHANNEL, source="poll", limit=100)
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


def test_poll_hook_starts_after_handlers_are_registered(tmp_path: Path) -> None:
    """Task 9 plugs its poll in here; it must not run before the update
    stream is live, or it would race backfill over the same rows."""
    db_path = tmp_path / "store.db"
    started: list[str] = []

    async def poll(listener: Listener) -> None:
        started.append("poll")
        await asyncio.sleep(3600)

    async def scenario() -> tuple[list[str], bool]:
        client = FakeTelegramClient()
        async with _started(db_path, client, poll_hook=poll) as listener:
            calls_at_poll_start = list(client.calls)
            await asyncio.sleep(0)
            assert listener.poll_task is not None
            return calls_at_poll_start, listener.poll_task.cancelled()

    calls, _cancelled = asyncio.run(scenario())

    assert started == ["poll"]
    assert "add_event_handler" in calls


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


def test_normalise_event_forces_utc_on_a_naive_datetime() -> None:
    from fakes.telegram import FakeNewMessageEvent

    event = FakeNewMessageEvent(
        chat_id=CHANNEL_ID,
        message=FakeMessage(id=7, message="hi", date=datetime(2026, 9, 5, 10, 45)),
    )

    incoming = normalise_event(event)

    assert incoming.posted_at_utc.tzinfo is not None
    assert incoming.posted_at_utc == datetime(2026, 9, 5, 10, 45, tzinfo=UTC)
