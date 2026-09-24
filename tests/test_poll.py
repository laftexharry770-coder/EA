"""Tests for the history poll (`ingest/poll.py`) -- the safety net that
recovers every message the Telegram update stream drops.

Everything runs the real listener and the real `store` stack against a
temporary database; only Telegram is faked. The poll's `sleep` and
`clock` are injected (`SteppedTime`), so a test runs one poll round at a
time and never waits five real seconds.

**The order these tests use is the point.** The earlier design's tests
polled *during* the blackout, before any live message had landed -- and
passed while a live message could carry the pointer past everything the
blackout hid (the reviewer's D1). So the scenarios here are: a blackout
hides more than one message, *then* a live message arrives, *then* the
poll runs.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import random
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from telethon import errors

from fakes.telegram import FakeMessage, FakeTelegramClient
from xaucopier.config.accounts import AccountCfg
from xaucopier.config.channels import ChannelCfg
from xaucopier.ingest.client import EXIT_TERMINAL_AUTH
from xaucopier.ingest.listener import (
    EXIT_LISTENER_STOPPED,
    FIRST_RUN_SEED_MARGIN,
    Listener,
    run_listener,
)
from xaucopier.ingest.poll import (
    DEFAULT_POLL_INTERVAL,
    DEFAULT_POLL_LIMIT,
    MISSED_BY_EVENTSTREAM_METRIC,
    HistoryPoll,
    WriterUnresponsiveError,
)
from xaucopier.store import db, heartbeats, messages_repo

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "messages" / "mmh_buy_4tp.json"
_FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
SIGNAL_TEXT: str = _FIXTURE["text"]
POSTED = datetime.fromisoformat(_FIXTURE["posted_at_utc"])
CHATTER_TEXT = "Good morning traders"
NOW = datetime(2026, 9, 5, 11, 0, tzinfo=UTC)

CHANNEL_A = ChannelCfg(
    channel_id=_FIXTURE["channel_id"], name="Channel A", parser="mmh", ordinal=1, enabled=True
)
CHANNEL_B = ChannelCfg(
    channel_id=-1002222222222, name="Channel B", parser="mmh", ordinal=2, enabled=True
)
A = CHANNEL_A.channel_id
B = CHANNEL_B.channel_id

ACCOUNTS = tuple(
    AccountCfg(
        account_id=f"acct-{ordinal}",
        broker="TestBroker",
        terminal_path=r"C:\terminal\terminal64.exe",
        login=123456,
        server="TestBroker-Demo",
        symbol="XAUUSD",
        fixed_lot=0.01,
        magic_base=1000 * ordinal,
        ordinal=ordinal,
        allow_live=False,
        enabled=True,
    )
    for ordinal in (1, 2)
)

#: `ScriptedClient.failures` entry: the read never answers (until cancelled).
HANG = "hang"


def _chatter(message_id: int) -> FakeMessage:
    return FakeMessage(id=message_id, message=CHATTER_TEXT, date=POSTED)


def _signal(message_id: int) -> FakeMessage:
    return FakeMessage(id=message_id, message=SIGNAL_TEXT, date=POSTED)


# -- harness -------------------------------------------------------------------


class SteppedTime:
    """The poll's injected `sleep` and `clock`.

    Every `sleep(seconds)` is recorded and then blocks until the test
    wakes it; waking it advances the clock by `seconds`. So one `wake()`
    is exactly one poll interval, and the poll's next `sleep()` call is
    proof that the round it started has finished."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.requests: list[float] = []
        self._gates: list[asyncio.Event] = []
        self._woken = 0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.requests.append(seconds)
        gate = asyncio.Event()
        self._gates.append(gate)
        await gate.wait()
        self.now += seconds

    @property
    def asleep(self) -> bool:
        return len(self._gates) > self._woken

    def wake(self) -> None:
        assert self.asleep, "the poll is not sleeping"
        self._gates[self._woken].set()
        self._woken += 1


@dataclass
class ScriptedClient(FakeTelegramClient):
    """The fake client, plus per-channel read failures a test scripts.

    `failures[channel_id]` is a queue: each read of that channel pops one
    entry and raises it (or, for `HANG`, never answers). `read_attempts`
    logs every read attempt's channel, including failed ones, which the
    fake's own `get_messages_calls` does not. `before_read` runs first.

    Only *history* reads are scripted and counted. Task 10's window
    re-read (`get_messages(ids=...)`) passes straight through to the fake:
    its own failures are scripted in tests/test_edits.py."""

    failures: dict[int, list[BaseException | str]] = field(default_factory=dict)
    read_attempts: list[int] = field(default_factory=list)
    hung_reads_cancelled: int = 0
    before_read: Callable[[int], Awaitable[None]] | None = None

    async def get_messages(self, *args, **kwargs):
        if kwargs.get("ids") is not None:
            return await super().get_messages(*args, **kwargs)
        channel_id = self._channel_id_of(args[0])
        self.read_attempts.append(channel_id)
        if self.before_read is not None:
            await self.before_read(channel_id)
        queue = self.failures.get(channel_id)
        if queue:
            failure = queue.pop(0)
            if failure == HANG:
                try:
                    await asyncio.Event().wait()
                finally:
                    self.hung_reads_cancelled += 1
            raise failure
        return await super().get_messages(*args, **kwargs)


def _build(
    db_path: Path,
    client: FakeTelegramClient,
    time: SteppedTime,
    *,
    channels: tuple[ChannelCfg, ...] = (CHANNEL_A,),
    writer: db.Writer | None = None,
    now: Callable[[], datetime] = lambda: NOW,
    **poll_kwargs,
) -> Listener:
    poll = HistoryPoll(sleep=time.sleep, clock=time.clock, **poll_kwargs)
    return Listener(
        client=client,
        channels=channels,
        accounts=ACCOUNTS,
        writer=writer if writer is not None else db.Writer(db_path),
        db_path=db_path,
        now=now,
        poll_hook=poll,
    )


@contextlib.asynccontextmanager
async def _polling(db_path: Path, client: FakeTelegramClient, time: SteppedTime, **kwargs):
    """A started listener whose poll is asleep before its first round.
    Nothing here supervises the listener (no `wait_until_stopped`), so
    the poll task's own fate is observable directly."""
    listener = _build(db_path, client, time, **kwargs)
    await listener.start()
    try:
        await _until_asleep(listener, time)
        yield listener
    finally:
        await listener.aclose()


async def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.001)


async def _until_asleep(listener: Listener, time: SteppedTime, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not time.asleep:
        task = listener.poll_task
        if task is not None and task.done():
            reason = "cancelled" if task.cancelled() else repr(task.exception())
            raise AssertionError(f"the poll stopped instead of finishing its round: {reason}")
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the poll never went back to sleep")
        await asyncio.sleep(0.001)


async def _run_rounds(listener: Listener, time: SteppedTime, rounds: int = 1) -> None:
    """Wake the poll and wait until it is asleep again: `rounds` whole rounds."""
    for _ in range(rounds):
        await _until_asleep(listener, time)
        time.wake()
        await _until_asleep(listener, time)


def _rows(db_path: Path, sql: str, params: tuple = ()) -> list:
    conn = db.connect(db_path, role="reader")
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _stored(db_path: Path, channel_id: int = A) -> list[tuple[int, str]]:
    return _rows(
        db_path,
        "SELECT message_id, source FROM messages WHERE channel_id = ? ORDER BY message_id",
        (channel_id,),
    )


def _stored_ids(db_path: Path, channel_id: int = A) -> list[int]:
    return [message_id for message_id, _ in _stored(db_path, channel_id)]


def _pointer(db_path: Path, channel_id: int = A) -> int:
    rows = _rows(
        db_path,
        "SELECT last_processed_message_id FROM channel_state WHERE channel_id = ?",
        (channel_id,),
    )
    return rows[0][0] if rows else 0


def _seed_pointer(db_path: Path, channel_id: int, value: int) -> None:
    conn = db.connect(db_path, role="writer")
    try:
        conn.execute(
            "INSERT INTO channel_state (channel_id, last_processed_message_id) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET "
            "last_processed_message_id = excluded.last_processed_message_id",
            (channel_id, value),
        )
        conn.commit()
    finally:
        conn.close()


def _missed(db_path: Path) -> int:
    rows = _rows(
        db_path, "SELECT SUM(value) FROM metrics WHERE name = ?", (MISSED_BY_EVENTSTREAM_METRIC,)
    )
    return rows[0][0] or 0


def _kill_the_writer(conn) -> None:
    raise SystemExit("simulated writer-thread death")


async def _kill_writer_of(listener: Listener) -> None:
    with pytest.raises(db.WriterDeadError):
        await asyncio.wrap_future(listener.writer.submit(_kill_the_writer))


# -- the reviewer's D1, through the real poll ------------------------------------


def test_d1_blackout_then_live_then_one_round_recovers_the_whole_gap(tmp_path: Path) -> None:
    """10, 11 and 12 black out; 13 arrives live; only then does the poll
    run. One round must store all three, leave 13 as the live stream's,
    and count three messages the stream missed."""
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    time = SteppedTime()

    async def scenario():
        client = FakeTelegramClient()
        async with _polling(db_path, client, time) as listener:
            with client.blackout():
                hidden = [
                    await client.emit(A, message)
                    for message in (_chatter(10), _signal(11), _chatter(12))
                ]
            live = await client.emit(A, _chatter(13))
            pointer_before_poll = listener.read_last_processed_message_id(A)
            calls_before = len(client.get_messages_calls)
            await _run_rounds(listener, time)
            poll_calls = client.get_messages_calls[calls_before:]
        return hidden, live, pointer_before_poll, poll_calls

    hidden, live, pointer_before_poll, poll_calls = asyncio.run(scenario())

    assert hidden == [False, False, False] and live is True
    assert pointer_before_poll == 9, "a live message moved the history pointer"
    # One history read, from the pointer, asking for a bounded batch --
    # not Telethon's default of ONE message...
    history_calls = [c for c in poll_calls if c.ids is None]
    assert [(c.min_id, c.limit, c.reverse) for c in history_calls] == [(9, DEFAULT_POLL_LIMIT, True)]
    # ...then (Task 10) one by-id re-read of what is now at or below the
    # pointer, in which edits and deletions would show.
    assert [c.ids for c in poll_calls if c.ids is not None] == [(10, 11, 12, 13)]
    assert _stored(db_path) == [(10, "poll"), (11, "poll"), (12, "poll"), (13, "live")]
    assert _pointer(db_path) == 13
    assert _missed(db_path) == 3
    assert _rows(db_path, "SELECT day_key, name, value FROM metrics") == [
        ("2026-09-05", MISSED_BY_EVENTSTREAM_METRIC, 3)
    ]
    # The recovered signal is queued for trading like any live one.
    assert _rows(db_path, "SELECT COUNT(*) FROM outbox")[0][0] == len(ACCOUNTS)


@pytest.mark.parametrize("first", ["live", "poll"])
def test_a_message_delivered_by_both_paths_is_ingested_exactly_once(
    tmp_path: Path, first: str
) -> None:
    """Whichever path stores it first, the other is a no-op: one message
    row, one signal, one outbox row per account -- one trade, not two."""
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    time = SteppedTime()

    async def scenario() -> int:
        client = FakeTelegramClient()
        async with _polling(db_path, client, time) as listener:
            if first == "live":
                assert await client.emit(A, _signal(10))
                await _run_rounds(listener, time)
            else:
                client.add_to_history(A, _signal(10))
                await _run_rounds(listener, time)
                assert await client.emit(A, _signal(10))
            return listener.handler_error_count

    handler_errors = asyncio.run(scenario())

    assert handler_errors == 0
    assert _stored(db_path) == [(10, first)]
    assert _rows(db_path, "SELECT COUNT(*) FROM signals")[0][0] == 1
    assert _rows(db_path, "SELECT COUNT(*) FROM outbox")[0][0] == len(ACCOUNTS)
    assert _pointer(db_path) == 10
    # The poll got there first only in the "poll" order -- which is what
    # the counter means (see the poll's module docstring).
    assert _missed(db_path) == (1 if first == "poll" else 0)


def test_two_channels_one_blacked_out_one_live_recover_independently(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    _seed_pointer(db_path, B, 19)
    time = SteppedTime()

    async def scenario() -> None:
        client = FakeTelegramClient()
        async with _polling(db_path, client, time, channels=(CHANNEL_A, CHANNEL_B)) as listener:
            # A is blacked out throughout (history only, never an update);
            # B is live apart from one dropped message in the middle.
            for message_id in (10, 11, 12):
                client.add_to_history(A, _chatter(message_id))
            assert await client.emit(B, _chatter(20))
            client.add_to_history(B, _chatter(21))
            assert await client.emit(B, _signal(22))
            assert (_pointer(db_path, A), _pointer(db_path, B)) == (9, 19)
            await _run_rounds(listener, time)

    asyncio.run(scenario())

    assert _stored(db_path, A) == [(10, "poll"), (11, "poll"), (12, "poll")]
    assert _stored(db_path, B) == [(20, "live"), (21, "poll"), (22, "live")]
    assert (_pointer(db_path, A), _pointer(db_path, B)) == (12, 22)
    assert _missed(db_path) == 4


def test_a_late_first_run_seed_recovers_what_was_posted_after_the_listener_started(
    tmp_path: Path,
) -> None:
    """Fix round 1's Critical (probe 1f), in the D1 order. A first run
    whose startup seed fails; then a signal is posted during a blackout;
    then a live message arrives; only minutes later does the poll get to
    seed. Seeding at "whatever is newest now" would jump over the
    blacked-out signal for good. The seed is anchored at the listener's
    start instead, so the signal is stored and counted as missed -- and
    the channel's old history, including an old signal, is not."""
    db_path = tmp_path / "store.db"  # no pointer: a first run
    time = SteppedTime()
    clock = {"now": NOW}
    an_hour_ago = NOW - timedelta(hours=1)
    old_history = [
        FakeMessage(id=i, message=SIGNAL_TEXT if i == 3 else CHATTER_TEXT, date=an_hour_ago)
        for i in range(1, 6)
    ]

    async def scenario():
        client = ScriptedClient(history={A: old_history})
        client.failures[A] = [ConnectionError("blip during the startup seed")]
        async with _polling(db_path, client, time, now=lambda: clock["now"]) as listener:
            after_start = (_pointer(db_path), _stored(db_path))
            clock["now"] = NOW + timedelta(seconds=30)
            with client.blackout():
                hidden = await client.emit(
                    A, FakeMessage(id=6, message=SIGNAL_TEXT, date=clock["now"])
                )
            clock["now"] = NOW + timedelta(seconds=45)
            live = await client.emit(A, FakeMessage(id=7, message=CHATTER_TEXT, date=clock["now"]))
            clock["now"] = NOW + timedelta(minutes=5)  # the seed is late
            calls_before = len(client.get_messages_calls)
            await _run_rounds(listener, time)
            poll_calls = client.get_messages_calls[calls_before:]
        return after_start, hidden, live, poll_calls

    after_start, hidden, live, poll_calls = asyncio.run(scenario())

    assert after_start == (0, []), "the startup seed was supposed to fail"
    assert (hidden, live) == (False, True)
    assert _stored(db_path) == [(6, "poll"), (7, "live")]
    assert _pointer(db_path) == 7
    assert _missed(db_path) == 1
    assert _rows(db_path, "SELECT message_id FROM signals") == [(6,)]  # never old signal 3
    assert _rows(db_path, "SELECT COUNT(*) FROM outbox")[0][0] == len(ACCOUNTS)
    # Anchored at the listener's start, not at the moment the seed ran.
    assert poll_calls[0].offset_date == NOW - FIRST_RUN_SEED_MARGIN


# -- one channel's trouble stays that channel's ----------------------------------


def test_a_flood_wait_defers_only_that_channel_for_the_time_telegram_asked(
    tmp_path: Path,
) -> None:
    """A's read is told to wait 12 s. B is still polled in that same round
    and every round after; A is not read again until 12 s have passed;
    and the poll itself keeps its 5 s beat throughout."""
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    _seed_pointer(db_path, B, 19)
    time = SteppedTime(start=1000.0)

    async def scenario():
        client = ScriptedClient()
        rounds = []
        # A first, so a flood wait escaping A's read would cost B its round too.
        async with _polling(db_path, client, time, channels=(CHANNEL_A, CHANNEL_B)) as listener:
            client.failures[A] = [errors.FloodWaitError(request=None, capture=12)]
            client.add_to_history(A, _chatter(10))
            for round_number in range(4):
                client.add_to_history(B, _chatter(20 + round_number))
                attempts_before = len(client.read_attempts)
                await _run_rounds(listener, time)
                rounds.append(
                    (
                        time.now,
                        client.read_attempts[attempts_before:],
                        _stored_ids(db_path, A),
                        _stored_ids(db_path, B),
                    )
                )
        return rounds

    rounds = asyncio.run(scenario())

    assert rounds == [
        (1005.0, [A, B], [], [20]),  # A told to wait until 1017; B polled anyway
        (1010.0, [B], [], [20, 21]),  # A not asked again yet
        (1015.0, [B], [], [20, 21, 22]),
        (1020.0, [A, B], [10], [20, 21, 22, 23]),  # 12 s have passed: A is read again
    ]
    assert time.requests == [DEFAULT_POLL_INTERVAL] * 5  # never slept for the flood itself


@pytest.mark.parametrize("failure", ["timeout", "connection", "rpc", "unexpected"])
def test_one_channels_failed_read_does_not_stop_the_poll_or_the_other_channel(
    tmp_path: Path, failure: str
) -> None:
    """A read that hangs past `rpc_timeout`, a dropped connection, an RPC
    error, or anything else: logged, B is still polled that round, and A
    is simply read again next round."""
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    _seed_pointer(db_path, B, 19)
    time = SteppedTime()
    injected: BaseException | str = {
        "timeout": HANG,
        "connection": ConnectionError("Cannot send requests while disconnected"),
        "rpc": errors.ChannelPrivateError(request=None),
        "unexpected": ValueError("something nobody anticipated"),
    }[failure]

    async def scenario():
        client = ScriptedClient()
        async with _polling(
            db_path, client, time, channels=(CHANNEL_A, CHANNEL_B), rpc_timeout=0.2
        ) as listener:
            client.failures[A] = [injected]
            client.add_to_history(A, _chatter(10))
            client.add_to_history(B, _chatter(20))
            await _run_rounds(listener, time)
            after_first = (_stored_ids(db_path, A), _stored_ids(db_path, B))
            await _run_rounds(listener, time)
            after_second = (_stored_ids(db_path, A), _stored_ids(db_path, B))
        return after_first, after_second, client.hung_reads_cancelled

    after_first, after_second, hung_reads_cancelled = asyncio.run(scenario())

    assert after_first == ([], [20])
    assert after_second == ([10], [20])
    assert _pointer(db_path, A) == 10
    # The hung RPC was cancelled by the timeout, not left running.
    assert hung_reads_cancelled == (1 if failure == "timeout" else 0)


def test_a_terminal_auth_error_is_not_swallowed_and_exits_3(tmp_path: Path) -> None:
    """A dead session never recovers. Retrying it every 5 s forever would
    leave a process that looks alive and can never read anything again,
    so the poll stops at once -- and the listener exits 3 ("do not
    restart"), not 4 ("restart me"), exactly as it does when
    `run_until_disconnected()` raises the same error."""
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    _seed_pointer(db_path, B, 19)
    time = SteppedTime()

    async def scenario():
        client = ScriptedClient()
        listener = _build(db_path, client, time, channels=(CHANNEL_A, CHANNEL_B))
        task = asyncio.create_task(run_listener(listener))
        await _wait_for(lambda: "run_until_disconnected" in client.calls)
        await _until_asleep(listener, time)
        client.failures[A] = [errors.AuthKeyUnregisteredError(request=None)]
        attempts_before = len(client.read_attempts)
        time.wake()
        exit_code = await asyncio.wait_for(task, timeout=2.0)
        return exit_code, listener.poll_task.exception(), client.read_attempts[attempts_before:]

    exit_code, error, attempts = asyncio.run(scenario())

    assert exit_code == EXIT_TERMINAL_AUTH
    assert isinstance(error, errors.AuthKeyUnregisteredError)
    assert attempts == [A], "the poll went on to the next channel with a dead session"


# -- a dead (or hung) writer stops everything --------------------------------------


def test_a_writer_death_met_by_a_poll_read_stops_the_listener_with_exit_4(
    tmp_path: Path,
) -> None:
    """The writer dies after this round's probe, so the first write to meet
    it is the poll's ingest of a blacked-out message."""
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    time = SteppedTime()

    async def scenario():
        client = ScriptedClient()
        listener = _build(db_path, client, time)
        task = asyncio.create_task(run_listener(listener))
        await _wait_for(lambda: "run_until_disconnected" in client.calls)
        await _until_asleep(listener, time)
        with client.blackout():
            await client.emit(A, _chatter(10))

        async def kill_once(channel_id: int) -> None:
            client.before_read = None
            await _kill_writer_of(listener)

        client.before_read = kill_once
        time.wake()
        exit_code = await asyncio.wait_for(task, timeout=2.0)
        return exit_code, listener.handler_errors

    exit_code, handler_errors = asyncio.run(scenario())

    assert exit_code == EXIT_LISTENER_STOPPED
    assert [(e.message_id, e.source) for e in handler_errors] == [(10, "poll")]
    assert handler_errors[0].error.startswith("WriterDeadError")
    assert _stored(db_path) == []


def test_the_poll_never_swallows_a_writer_death(tmp_path: Path) -> None:
    """A `WriterDeadError` that reaches the poll ends it at once -- the
    rest of the round is not attempted -- rather than being logged like a
    channel's trouble and polled past.

    The death reaches the poll through a seed write: A's startup backfill
    failed, so A is still unseeded and the poll's first read of it seeds
    the pointer. The writer dies during that read."""
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, B, 19)  # A deliberately has no pointer yet
    time = SteppedTime()

    async def scenario():
        client = ScriptedClient(history={A: [_chatter(50)]})
        client.failures[A] = [ConnectionError("blip during startup backfill")]
        listener = _build(db_path, client, time, channels=(CHANNEL_A, CHANNEL_B))
        task = asyncio.create_task(run_listener(listener))
        await _wait_for(lambda: "run_until_disconnected" in client.calls)
        await _until_asleep(listener, time)

        async def kill_once(channel_id: int) -> None:
            client.before_read = None
            await _kill_writer_of(listener)

        client.before_read = kill_once
        attempts_before = len(client.read_attempts)
        time.wake()
        exit_code = await asyncio.wait_for(task, timeout=2.0)
        return exit_code, listener.poll_task, client.read_attempts[attempts_before:]

    exit_code, poll_task, attempts = asyncio.run(scenario())

    assert exit_code == EXIT_LISTENER_STOPPED
    assert not poll_task.cancelled(), "the poll carried on after the writer died"
    assert isinstance(poll_task.exception(), db.WriterDeadError)
    assert attempts == [A], "the poll went on to the next channel with no writer"
    assert _pointer(db_path, A) == 0


def test_a_writer_that_dies_in_a_quiet_period_stops_the_listener_within_one_interval(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No messages at all, so nothing writes -- except the poll's per-round
    writer no-op, which is what notices the death. It goes through the
    listener's writer door, so the listener stops *because the writer
    died* (and says so), not merely because its poll did."""
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    time = SteppedTime()

    async def scenario():
        client = FakeTelegramClient()
        listener = _build(db_path, client, time)
        task = asyncio.create_task(run_listener(listener))
        await _wait_for(lambda: "run_until_disconnected" in client.calls)
        await _until_asleep(listener, time)
        await _kill_writer_of(listener)
        time.wake()  # one interval passes
        exit_code = await asyncio.wait_for(task, timeout=2.0)
        return exit_code, listener.poll_task

    with caplog.at_level("ERROR", logger="xaucopier.ingest.listener"):
        exit_code, poll_task = asyncio.run(scenario())

    assert exit_code == EXIT_LISTENER_STOPPED
    assert time.requests == [DEFAULT_POLL_INTERVAL], "the poll slept again with a dead writer"
    assert isinstance(poll_task.exception(), db.WriterDeadError)
    stop_reasons = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(("the writer thread died", "the poll task"))
    ]
    assert len(stop_reasons) == 1 and "nothing can be stored" in stop_reasons[0], stop_reasons


class _HangingWriter(db.Writer):
    """A writer whose thread stops serving jobs once `hang` is set: every
    later job's future simply never resolves."""

    hang = False

    def submit(self, fn, **kwargs):
        if self.hang:
            import concurrent.futures

            return concurrent.futures.Future()
        return super().submit(fn, **kwargs)


def test_a_hung_writer_stops_the_poll_instead_of_hanging_it(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    time = SteppedTime()
    writer = _HangingWriter(db_path)

    async def scenario():
        client = FakeTelegramClient()
        listener = _build(db_path, client, time, writer=writer, writer_probe_timeout=0.2)
        task = asyncio.create_task(run_listener(listener))
        await _wait_for(lambda: "run_until_disconnected" in client.calls)
        await _until_asleep(listener, time)
        writer.hang = True
        time.wake()
        exit_code = await asyncio.wait_for(task, timeout=2.0)
        return exit_code, listener.poll_task

    exit_code, poll_task = asyncio.run(scenario())

    assert exit_code == EXIT_LISTENER_STOPPED
    assert isinstance(poll_task.exception(), WriterUnresponsiveError)



# -- another program holding the write lock ---------------------------------------
#
# Group-1 review of the final fix wave: since F21 every Writer job opens
# with BEGIN IMMEDIATE, so the writer probes -- which only check that the
# writer thread answers -- waited for the write lock too. Another program
# holding it longer than the busy timeout (a database browser with an
# unsaved edit, later Plan 3's worker) then failed the per-round probe with
# "database is locked", the poll task died, and `.\xaucopier watch`
# stopped with exit 4: every later message missed until someone noticed.
# The probes are read-only jobs now, which never wait for that lock.


@contextlib.contextmanager
def _write_lock_held_by_another_program(db_path: Path):
    holder = sqlite3.connect(db_path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        yield
    finally:
        if holder.in_transaction:
            holder.execute("ROLLBACK")
        holder.close()


def test_another_program_holding_the_write_lock_does_not_stop_the_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The round that meets the lock cannot store anything (its writes
    wait out the busy timeout and fail) -- but the poll survives it, and
    the next round, after the lock is released, recovers the message."""
    monkeypatch.setattr(db, "_BUSY_TIMEOUT_MS", 100)
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    time = SteppedTime()

    async def scenario():
        client = FakeTelegramClient()
        async with _polling(db_path, client, time) as listener:
            with _write_lock_held_by_another_program(db_path):
                client.add_to_history(A, _chatter(10))
                await _run_rounds(listener, time)
                stored_while_locked = _stored_ids(db_path)
            await _run_rounds(listener, time)
            return stored_while_locked, listener.poll_task.done()

    stored_while_locked, poll_stopped = asyncio.run(scenario())

    assert stored_while_locked == []
    assert not poll_stopped
    assert _stored_ids(db_path) == [10]
    assert _pointer(db_path) == 10


def test_watch_starts_while_another_program_holds_the_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The startup probe too: it proves the writer thread connected, and
    must not turn someone else's write lock into "the writer thread could
    not connect". The start-up read of the channel cannot be stored while
    the lock is held; the first poll round after the release stores it."""
    monkeypatch.setattr(db, "_BUSY_TIMEOUT_MS", 100)
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    time = SteppedTime()

    async def scenario():
        client = FakeTelegramClient()
        client.add_to_history(A, _chatter(10))
        with _write_lock_held_by_another_program(db_path):
            listener = _build(db_path, client, time)
            await listener.start()
        try:
            stored_before_the_poll = _stored_ids(db_path)
            await _run_rounds(listener, time)
            return stored_before_the_poll, listener.poll_task.done()
        finally:
            await listener.aclose()

    stored_before_the_poll, poll_stopped = asyncio.run(scenario())

    assert stored_before_the_poll == []
    assert not poll_stopped
    assert _stored_ids(db_path) == [10]


# -- the pointer and the beat -------------------------------------------------------


def test_the_pointer_only_rises_and_never_passes_an_unstored_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thirty rounds of live messages, dropped ones and transient write
    failures (on both the live path and the poll path). At every
    observation the pointer is no lower than before, and every id at or
    below it is stored -- the property that makes it safe to resume from."""
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    time = SteppedTime()
    fails_once = {14, 23, 31, 40}
    real_ingest = messages_repo.ingest

    def flaky_ingest(conn, **kwargs):
        if kwargs["message_id"] in fails_once:
            fails_once.discard(kwargs["message_id"])
            raise RuntimeError("transient write failure")
        return real_ingest(conn, **kwargs)

    monkeypatch.setattr(messages_repo, "ingest", flaky_ingest)
    rng = random.Random(20260923)
    live_ids: list[int] = []
    dropped_ids: list[int] = []

    async def scenario():
        client = FakeTelegramClient()
        observations = []
        next_id = 10
        async with _polling(db_path, client, time) as listener:
            for _ in range(30):
                for _ in range(rng.randint(0, 3)):
                    if rng.random() < 0.5:
                        live_ids.append(next_id)
                        await client.emit(A, _chatter(next_id))
                    else:
                        dropped_ids.append(next_id)
                        client.add_to_history(A, _chatter(next_id))
                    next_id += 1
                    observations.append((_pointer(db_path), set(_stored_ids(db_path))))
                await _run_rounds(listener, time)
                observations.append((_pointer(db_path), set(_stored_ids(db_path))))
            await _run_rounds(listener, time, 2)  # drain the retries
            observations.append((_pointer(db_path), set(_stored_ids(db_path))))
        return observations, next_id - 1

    observations, last_id = asyncio.run(scenario())

    pointers = [pointer for pointer, _ in observations]
    assert pointers == sorted(pointers), "the pointer went backwards"
    for pointer, stored in observations:
        assert set(range(10, pointer + 1)) <= stored, (
            f"pointer {pointer} is above unstored ids {set(range(10, pointer + 1)) - stored}"
        )
    assert pointers[-1] == last_id
    assert _stored_ids(db_path) == list(range(10, last_id + 1))
    assert not fails_once, "a scripted failure never fired; the scenario lost its teeth"
    # Every message the stream did not deliver *and store* was counted once.
    live_but_failed = {14, 23, 31, 40} & set(live_ids)
    assert _missed(db_path) == len(dropped_ids) + len(live_but_failed)


def test_the_poll_sleeps_one_interval_per_round_and_never_spins(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    time = SteppedTime()

    async def scenario():
        client = FakeTelegramClient()
        reads_per_round = []
        async with _polling(db_path, client, time) as listener:
            calls_at_start = len(client.get_messages_calls)
            for _ in range(4):
                await _run_rounds(listener, time)
                # Real time: a poll that skipped its sleep would read many
                # more times in 20 ms than once per round.
                await asyncio.sleep(0.02)
                reads_per_round.append(len(client.get_messages_calls) - calls_at_start)
        return reads_per_round

    reads_per_round = asyncio.run(scenario())

    assert reads_per_round == [1, 2, 3, 4]
    assert time.requests == [DEFAULT_POLL_INTERVAL] * 5


def test_the_default_poll_is_every_five_seconds_on_the_real_clock() -> None:
    poll = HistoryPoll()

    assert poll.interval == DEFAULT_POLL_INTERVAL == 5.0
    assert poll.sleep is asyncio.sleep
    assert poll.limit == DEFAULT_POLL_LIMIT
    assert poll.rpc_timeout == 20.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"interval": 0},
        {"interval": -5},
        {"interval": True},
        {"rpc_timeout": 0},
        {"writer_probe_timeout": -1},
        {"limit": 0},
        {"limit": 2.5},
        {"limit": True},
    ],
)
def test_a_config_that_could_spin_or_read_nothing_is_refused(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        HistoryPoll(**kwargs)


# -- the round stamp `status` reads (Task 11) ----------------------------------------


def _stamps(db_path: Path) -> dict[str, tuple[datetime, str | None]]:
    conn = db.connect(db_path, role="reader")
    try:
        return {beat.name: (beat.at_utc, beat.detail) for beat in heartbeats.read_all(conn)}
    finally:
        conn.close()


def test_each_round_stamps_its_completion_and_only_the_channels_it_read(
    tmp_path: Path,
) -> None:
    """`status` runs in another process: the poll's own stamps are the only
    way it can tell that `watch` is alive and that Telegram still answers.
    A channel whose read failed must not be stamped as read."""
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    _seed_pointer(db_path, B, 19)
    time = SteppedTime()
    clock = {"now": NOW}

    async def scenario():
        client = ScriptedClient()
        async with _polling(
            db_path, client, time, channels=(CHANNEL_A, CHANNEL_B), now=lambda: clock["now"]
        ) as listener:
            client.failures[A] = [ConnectionError("blip")]
            clock["now"] = NOW + timedelta(seconds=5)
            await _run_rounds(listener, time)
            first = _stamps(db_path)
            clock["now"] = NOW + timedelta(seconds=10)
            await _run_rounds(listener, time)
            second = _stamps(db_path)
        return first, second

    first, second = asyncio.run(scenario())

    round_1 = NOW + timedelta(seconds=5)
    round_2 = NOW + timedelta(seconds=10)
    assert first == {
        heartbeats.POLL_ROUND: (round_1, "1 of 2 channel(s) read"),
        heartbeats.channel_read_name(B): (round_1, None),
    }
    assert second == {
        heartbeats.POLL_ROUND: (round_2, "2 of 2 channel(s) read"),
        heartbeats.channel_read_name(A): (round_2, None),
        heartbeats.channel_read_name(B): (round_2, None),
    }


def test_a_stamp_that_cannot_be_written_does_not_stop_the_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stamp is a report: failing to write one makes `status` look stale
    for a round. It must never stop the poll that recovers trades."""
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    time = SteppedTime()

    async def scenario():
        client = FakeTelegramClient()
        async with _polling(db_path, client, time) as listener:
            real = listener.record_heartbeats
            failures = [sqlite3.OperationalError("database is locked")]

            async def flaky(beats, **kwargs):
                if failures:
                    raise failures.pop()
                await real(beats, **kwargs)

            monkeypatch.setattr(listener, "record_heartbeats", flaky)
            client.add_to_history(A, _chatter(10))
            await _run_rounds(listener, time)
            after_failed_stamp = (_stored_ids(db_path), _stamps(db_path))
            client.add_to_history(A, _chatter(11))
            await _run_rounds(listener, time)
            poll_alive = not listener.poll_task.done()
        return after_failed_stamp, _stored_ids(db_path), _stamps(db_path), poll_alive

    after_failed_stamp, stored, stamps, poll_alive = asyncio.run(scenario())

    assert after_failed_stamp == ([10], {})
    assert stored == [10, 11] and poll_alive
    assert stamps[heartbeats.POLL_ROUND] == (NOW, "1 of 1 channel(s) read")


def test_a_writer_death_met_by_the_stamp_stops_the_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    time = SteppedTime()

    async def scenario():
        client = FakeTelegramClient()
        async with _polling(db_path, client, time) as listener:

            async def dead(beats, **kwargs):
                raise db.WriterDeadError("simulated")

            monkeypatch.setattr(listener, "record_heartbeats", dead)
            time.wake()
            await _wait_for(lambda: listener.poll_task.done())
            return listener.poll_task.exception()

    assert isinstance(asyncio.run(scenario()), db.WriterDeadError)


# -- group-1 review leftover G1L-2: a lock never skips a message ---------------------


class _LockReleasingWriter(db.Writer):
    """A real Writer that lets go of another program's lock (`holder`) the
    moment the `release_after`-th ingest job has failed with "database is
    locked" -- i.e. just before whatever the listener does next."""

    def __init__(self, path: Path, holder: sqlite3.Connection, release_after: int) -> None:
        super().__init__(path)
        self.holder = holder
        self.release_after = release_after
        self.locked_ingests = 0

    def submit(self, fn, **kwargs):
        future = super().submit(fn, **kwargs)
        if "ingest_message" in getattr(fn, "__qualname__", ""):
            future.add_done_callback(self._count)
        return future

    def _count(self, future) -> None:  # runs on the writer thread, as the job fails
        if future.cancelled() or future.exception() is None:
            return
        if "locked" in str(future.exception()):
            self.locked_ingests += 1
            if self.locked_ingests == self.release_after and self.holder.in_transaction:
                self.holder.execute("ROLLBACK")


def test_another_programs_lock_across_three_rounds_never_skips_the_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G1L-2. Another program holds the write lock for three rounds, and
    lets go just after the third failed attempt. "database is locked"
    says nothing about the message, so it must not count towards
    MAX_HISTORY_ATTEMPTS: before, the third failure triggered the skip,
    whose own write then found the lock free -- the message was skipped
    for good (history_skipped 1, never stored)."""
    monkeypatch.setattr(db, "_BUSY_TIMEOUT_MS", 100)
    db_path = tmp_path / "store.db"
    _seed_pointer(db_path, A, 9)
    holder = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    writer = _LockReleasingWriter(db_path, holder, release_after=3)
    time = SteppedTime()

    async def scenario():
        client = FakeTelegramClient()
        async with _polling(db_path, client, time, writer=writer) as listener:
            holder.execute("BEGIN IMMEDIATE")
            client.add_to_history(A, _signal(10))
            await _run_rounds(listener, time, rounds=3)
            stored_after_the_lock = _stored_ids(db_path)
            await _run_rounds(listener, time)
            return stored_after_the_lock, listener.poll_task.done(), listener.handler_errors

    try:
        stored_after_the_lock, poll_stopped, errors = asyncio.run(scenario())
    finally:
        if holder.in_transaction:
            holder.execute("ROLLBACK")
        holder.close()

    assert writer.locked_ingests == 3, "the lock did not hold for three rounds"
    assert stored_after_the_lock == []
    assert not poll_stopped
    assert _stored(db_path) == [(10, "poll")]
    assert _pointer(db_path) == 10
    assert _rows(db_path, "SELECT COUNT(*) FROM metrics WHERE name = 'history_skipped'") == [(0,)]
    assert _rows(db_path, "SELECT COUNT(*) FROM outbox")[0][0] == len(ACCOUNTS)
    assert [e.message_id for e in errors] == [10, 10, 10]
    assert all("locked" in e.error for e in errors)
