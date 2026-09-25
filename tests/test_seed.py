"""The first-run seed, persisted (final review F01; ledger 141, 142, 114/144;
F28's watch side).

A channel's first read starts just below everything posted since `watch`
first tried to read it. That start -- the seed cutoff -- is written to the
store at the FIRST attempt, before any seed request, and every later
attempt (the poll's, a restarted watch's) uses the stored one. So a watch
whose seed fails for its whole run no longer hands the next run a cutoff
at *its* start, which silently skipped every post the first run missed.

Real listener, real poll, real store; Telegram is the fake only. The run-1
client fails every seed read in one of the three ways the verifier found
(a long flood wait, every read hanging, only the anchor query hanging);
run 2 is a fresh process against the same channel history.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from telethon import errors

from fakes.telegram import FakeMessage, FakeTelegramClient
from test_poll import (
    A,
    ACCOUNTS,
    B,
    CHANNEL_A,
    CHANNEL_B,
    CHATTER_TEXT,
    NOW,
    SIGNAL_TEXT,
    SteppedTime,
    _missed,
    _pointer,
    _rows,
    _run_rounds,
    _stored,
    _until_asleep,
)
from xaucopier.ingest import listener as listener_module
from xaucopier.ingest.listener import FIRST_RUN_SEED_MARGIN, Listener
from xaucopier.ingest.poll import HistoryPoll
from xaucopier.store import channel_state, db

#: Every history request's bound in these tests: small, so a hung read
#: fails fast.
RPC_TIMEOUT = 0.05
THREE_DAYS_AGO = NOW - timedelta(days=3)
OLD_HISTORY = [
    FakeMessage(id=i, message=SIGNAL_TEXT if i == 3 else CHATTER_TEXT, date=THREE_DAYS_AGO)
    for i in range(1, 51)
]


def _at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


def _post(message_id: int, seconds: float, text: str = CHATTER_TEXT) -> FakeMessage:
    return FakeMessage(id=message_id, message=text, date=_at(seconds))


@dataclass
class _SeedFailsClient(FakeTelegramClient):
    """Run 1's Telegram, on which no seed ever succeeds.

    - "flood": every history read is told to wait an hour (the poll then
      defers the channel for that hour);
    - "hang": every history read hangs (and times out);
    - "anchor_hang": only the seed's anchor query (`offset_date`) hangs.
    By-id reads pass through."""

    mode: str = "flood"
    #: The offset_date of every anchor query tried, answered or not.
    tried_anchors: list = field(default_factory=list)

    async def get_messages(self, *args, **kwargs):
        if kwargs.get("offset_date") is not None:
            self.tried_anchors.append(kwargs["offset_date"])
        if kwargs.get("ids") is None:
            if self.mode == "flood":
                self.calls.append("get_messages")
                raise errors.FloodWaitError(request=None, capture=3600)
            if self.mode == "hang" or (self.mode == "anchor_hang" and "offset_date" in kwargs):
                self.calls.append("get_messages")
                await asyncio.Event().wait()
        return await super().get_messages(*args, **kwargs)


def _build(
    db_path: Path,
    client: FakeTelegramClient,
    time: SteppedTime,
    clock: dict,
    *,
    channels=(CHANNEL_A,),
) -> Listener:
    return Listener(
        client=client,
        channels=channels,
        accounts=ACCOUNTS,
        writer=db.Writer(db_path),
        db_path=db_path,
        now=lambda: clock["now"],
        poll_hook=HistoryPoll(sleep=time.sleep, clock=time.clock, rpc_timeout=RPC_TIMEOUT),
        backfill_rpc_timeout=RPC_TIMEOUT,
    )


@contextlib.asynccontextmanager
async def _watching(db_path: Path, client: FakeTelegramClient, clock: dict, **kwargs):
    """One `watch` process: started, its poll asleep before round 1."""
    time = SteppedTime()
    listener = _build(db_path, client, time, clock, **kwargs)
    await listener.start()
    try:
        await _until_asleep(listener, time)
        yield listener, time
    finally:
        await listener.aclose()


def _seed_row(db_path: Path, channel_id: int = A) -> tuple | None:
    rows = _rows(
        db_path,
        "SELECT last_processed_message_id, seeded_at_utc IS NOT NULL, seed_cutoff_utc "
        "FROM channel_state WHERE channel_id = ?",
        (channel_id,),
    )
    return rows[0] if rows else None


def _anchor_queries(client: FakeTelegramClient) -> list[datetime]:
    return [c.offset_date for c in client.get_messages_calls if c.offset_date is not None]


# -- F01: a restart before the first seed succeeds -------------------------------------


@pytest.mark.parametrize("mode", ["flood", "hang", "anchor_hang"])
def test_a_restart_after_a_run_whose_seed_never_succeeded_loses_nothing(
    tmp_path: Path, mode: str
) -> None:
    """F01's P1/S1 (flood), S3 (hang) and P1b (only the anchor query hangs),
    with S2's downtime post added. Run 1: the seed fails all run; 51 is
    delivered live, SIGNAL 52 is blacked out at +90 s, 53 arrives live, and
    run 1 stops at +150 s. 54 is posted at +200 s while watch is stopped.
    Run 2 starts at +300 s and every read works.

    Before: run 2 anchored at ITS start minus 60 s (+240 s), i.e. at 54 --
    52 and 54 were lost, with every counter at 0. Now it anchors at run 1's
    stored cutoff, the newest message before (run-1 start - 60 s), and
    reads everything after it."""
    db_path = tmp_path / "store.db"
    clock = {"now": NOW}

    async def run_1():
        client = _SeedFailsClient(history={A: list(OLD_HISTORY)}, mode=mode)
        async with _watching(db_path, client, clock) as (listener, time):
            clock["now"] = _at(30)
            assert await client.emit(A, _post(51, 30))
            await _run_rounds(listener, time)
            clock["now"] = _at(90)
            with client.blackout():
                assert not await client.emit(A, _post(52, 90, SIGNAL_TEXT))
            await _run_rounds(listener, time)
            clock["now"] = _at(120)
            assert await client.emit(A, _post(53, 120))
            await _run_rounds(listener, time)
            clock["now"] = _at(150)
            after = (_pointer(db_path), _stored(db_path), _seed_row(db_path))
        return client.history[A], after

    history, after_run_1 = asyncio.run(run_1())
    assert after_run_1[0] == 0 and after_run_1[1] == [(51, "live"), (53, "live")]
    # The first attempt's cutoff is on disk: run 1's start, whole seconds.
    assert after_run_1[2] == (0, 0, "2026-09-05T11:00:00+00:00")

    history = [*history, _post(54, 200)]  # posted while watch is stopped
    clock["now"] = _at(300)

    async def run_2():
        client = FakeTelegramClient(history={A: history})
        async with _watching(db_path, client, clock):
            return _anchor_queries(client)

    anchors = asyncio.run(run_2())

    assert anchors == [NOW - FIRST_RUN_SEED_MARGIN], "run 2 must use run 1's stored cutoff"
    assert _stored(db_path) == [(51, "live"), (52, "backfill"), (53, "live"), (54, "backfill")]
    assert _rows(db_path, "SELECT message_id FROM signals") == [(52,)]  # never old signal 3
    assert _rows(db_path, "SELECT COUNT(*) FROM outbox")[0][0] == len(ACCOUNTS)
    assert _pointer(db_path) == 54
    assert _seed_row(db_path) == (54, 1, None), "a successful seed marks the channel, clears the cutoff"


def test_a_seed_that_succeeds_later_in_the_same_run_still_uses_the_stored_cutoff(
    tmp_path: Path,
) -> None:
    """MF2's other half: the seed re-runs every round while the cutoff is
    set, and when it finally succeeds it anchors where a prompt one would
    have -- the blacked-out signal is recovered by the poll and counted as
    missed."""
    db_path = tmp_path / "store.db"
    clock = {"now": NOW}

    async def scenario():
        client = _SeedFailsClient(history={A: list(OLD_HISTORY)}, mode="hang")
        async with _watching(db_path, client, clock) as (listener, time):
            clock["now"] = _at(90)
            with client.blackout():
                await client.emit(A, _post(51, 90, SIGNAL_TEXT))
            await _run_rounds(listener, time, rounds=3)
            client.mode = "works"
            clock["now"] = _at(600)
            await _run_rounds(listener, time)
            return client.tried_anchors

    anchors = asyncio.run(scenario())

    # Every attempt -- startup, three failed rounds, the one that worked --
    # asked for the same anchor.
    assert anchors == [NOW - FIRST_RUN_SEED_MARGIN] * 5
    assert _stored(db_path) == [(51, "poll")]
    assert _missed(db_path) == 1
    assert _seed_row(db_path) == (51, 1, None)


def test_the_cutoff_is_stored_before_any_seed_request_for_every_channel(tmp_path: Path) -> None:
    """One writer job at start writes every enabled, unseeded channel's
    cutoff before the first seed request goes out."""
    db_path = tmp_path / "store.db"
    seen_at_first_request: list = []

    @dataclass
    class _Spy(FakeTelegramClient):
        async def get_messages(self, *args, **kwargs):
            if not seen_at_first_request:
                seen_at_first_request.append(
                    sorted(
                        _rows(db_path, "SELECT channel_id, seed_cutoff_utc FROM channel_state")
                    )
                )
            return await super().get_messages(*args, **kwargs)

    async def scenario():
        client = _Spy(history={A: list(OLD_HISTORY), B: list(OLD_HISTORY)})
        async with _watching(db_path, client, {"now": NOW}, channels=(CHANNEL_A, CHANNEL_B)):
            pass

    asyncio.run(scenario())

    assert seen_at_first_request == [
        [(B, "2026-09-05T11:00:00+00:00"), (A, "2026-09-05T11:00:00+00:00")]
    ]


def test_no_seed_request_goes_out_until_its_cutoff_is_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the cutoff cannot be written (another program holds the database),
    the seed waits for it -- a seed with no stored cutoff is exactly the
    per-process start F01 was about."""
    db_path = tmp_path / "store.db"
    clock = {"now": NOW}
    real_claim = channel_state.claim_seed_cutoff
    refusals = {"left": 3}

    def locked_claim(conn, **kwargs):
        if refusals["left"]:
            refusals["left"] -= 1
            import sqlite3

            raise sqlite3.OperationalError("database is locked")
        return real_claim(conn, **kwargs)

    monkeypatch.setattr(channel_state, "claim_seed_cutoff", locked_claim)

    async def scenario():
        client = FakeTelegramClient(history={A: list(OLD_HISTORY)})
        async with _watching(db_path, client, clock) as (listener, time):
            requests_while_locked = list(client.get_messages_calls)
            clock["now"] = _at(90)
            await _run_rounds(listener, time, rounds=2)  # 3rd refusal, then it works
            return requests_while_locked, _anchor_queries(client)

    requests_while_locked, anchors = asyncio.run(scenario())

    assert requests_while_locked == []  # the start job and the backfill's own were refused
    assert anchors == [NOW - FIRST_RUN_SEED_MARGIN]  # the cutoff is still this run's start
    assert _seed_row(db_path) == (50, 1, None)


# -- ledger 141 (MF1): the cutoff in whole seconds -----------------------------------------


def test_a_cutoff_with_a_fraction_of_a_second_cannot_block_a_channel_for_ever(
    tmp_path: Path,
) -> None:
    """Telethon sends offset_date in whole seconds. With a start at
    11:00:00.5 the old cutoff was 10:59:00.5: the anchor query went out as
    10:59:00 and so did not return a message dated 10:59:00, while the
    empty-anchor guard compared that message with 10:59:00.5 and refused
    to read the channel -- on every round, for ever. The signal posted
    after it was never stored."""
    db_path = tmp_path / "store.db"
    start = NOW + timedelta(milliseconds=500)
    clock = {"now": start}
    history = [FakeMessage(id=1, message=CHATTER_TEXT, date=NOW - FIRST_RUN_SEED_MARGIN)]

    async def scenario():
        client = FakeTelegramClient(history={A: history})
        async with _watching(db_path, client, clock) as (listener, time):
            clock["now"] = _at(30)
            with client.blackout():
                await client.emit(A, _post(2, 30, SIGNAL_TEXT))
            await _run_rounds(listener, time, rounds=3)
            return _anchor_queries(client), listener.handler_error_count

    anchors, _ = asyncio.run(scenario())

    assert all(a.microsecond == 0 for a in anchors)
    assert anchors[0] == NOW - FIRST_RUN_SEED_MARGIN
    # Posted exactly at the cutoff: inside the margin, so it is read.
    assert [m for m, _ in _stored(db_path)] == [1, 2]
    assert _rows(db_path, "SELECT message_id FROM signals") == [(2,)]


def test_a_stored_cutoff_carrying_a_fraction_is_still_asked_in_whole_seconds(
    tmp_path: Path,
) -> None:
    """Every cutoff this program writes is in whole seconds, but one already
    in the store with a fraction (written by hand, or by an earlier build)
    must not bring MF1 back: the anchor query and the empty-anchor guard
    both use the same whole-second instant, so the message posted exactly
    then is read, not refused for ever."""
    db_path = tmp_path / "store.db"
    conn = db.connect(db_path, role="writer")
    conn.execute(
        "INSERT INTO channel_state (channel_id, seed_cutoff_utc) VALUES (?, ?)",
        (A, (NOW - timedelta(minutes=5, milliseconds=-500)).isoformat()),
    )
    conn.commit()
    conn.close()
    offset = NOW - timedelta(minutes=5) - FIRST_RUN_SEED_MARGIN
    history = [FakeMessage(id=1, message=CHATTER_TEXT, date=offset)]

    async def scenario():
        client = FakeTelegramClient(history={A: history})
        async with _watching(db_path, client, {"now": NOW}) as (listener, time):
            await _run_rounds(listener, time, rounds=2)
            return _anchor_queries(client), listener.handler_error_count

    anchors, errors_seen = asyncio.run(scenario())

    assert anchors[0] == offset and all(a.microsecond == 0 for a in anchors)
    assert errors_seen == 0
    assert [m for m, _ in _stored(db_path)] == [1]
    assert _seed_row(db_path)[:2] == (1, True)


# -- ledger 142 (MF2): one empty answer cannot mark a channel seeded ------------------------


@dataclass
class _EmptyThenRealClient(FakeTelegramClient):
    """Answers every history read with nothing while `empty` is set -- a
    server hiccup -- although the channel has 400 days of history."""

    empty: bool = True
    tried_anchors: list = field(default_factory=list)

    async def get_messages(self, *args, **kwargs):
        if kwargs.get("offset_date") is not None:
            self.tried_anchors.append(kwargs["offset_date"])
        if self.empty and kwargs.get("ids") is None:
            self.calls.append("get_messages")
            return []
        return await super().get_messages(*args, **kwargs)


def test_an_empty_answer_then_a_real_one_seeds_at_the_anchor_and_reads_no_old_history(
    tmp_path: Path,
) -> None:
    """Before: the startup seed saw no anchor and no oldest message, marked
    the channel seeded at 0 (in memory), and the next round read the
    channel from 0 -- its 400-day-old signal queued for trading."""
    db_path = tmp_path / "store.db"
    clock = {"now": NOW}
    ancient = [
        FakeMessage(id=i, message=SIGNAL_TEXT if i == 3 else CHATTER_TEXT,
                    date=NOW - timedelta(days=400))
        for i in range(1, 21)
    ]

    async def scenario():
        client = _EmptyThenRealClient(history={A: ancient})
        async with _watching(db_path, client, clock) as (listener, time):
            after_start = _seed_row(db_path)
            client.empty = False
            await _run_rounds(listener, time)
            return after_start, client.tried_anchors

    after_start, anchors = asyncio.run(scenario())

    assert after_start == (0, 0, "2026-09-05T11:00:00+00:00"), "still waiting to be seeded"
    assert anchors == [NOW - FIRST_RUN_SEED_MARGIN] * 2
    assert _stored(db_path) == []
    assert _rows(db_path, "SELECT COUNT(*) FROM outbox")[0][0] == 0
    assert _seed_row(db_path) == (20, 1, None)


def test_a_channel_with_no_messages_at_all_keeps_its_cutoff_and_reads_nothing(
    tmp_path: Path,
) -> None:
    """An answer of nothing to both seed queries is not a successful seed:
    the channel keeps its stored cutoff, and its first post -- whenever it
    comes -- is read."""
    db_path = tmp_path / "store.db"
    clock = {"now": NOW}

    async def scenario():
        client = FakeTelegramClient()
        async with _watching(db_path, client, clock) as (listener, time):
            await _run_rounds(listener, time, rounds=2)
            waiting = _seed_row(db_path)
            forward_reads = [c for c in client.get_messages_calls if c.min_id]
            clock["now"] = _at(45)
            with client.blackout():
                await client.emit(A, _post(1, 45, SIGNAL_TEXT))
            await _run_rounds(listener, time)
            return waiting, forward_reads

    waiting, forward_reads = asyncio.run(scenario())

    assert waiting == (0, 0, "2026-09-05T11:00:00+00:00")
    assert forward_reads == []
    assert _stored(db_path) == [(1, "poll")]
    assert _seed_row(db_path) == (1, 1, None)


# -- ledger 114/144: an empty channel's first post, made while watch is stopped -----------


def test_an_empty_channels_first_post_made_while_watch_is_stopped_is_read_on_restart(
    tmp_path: Path,
) -> None:
    """Before: nothing about the empty channel was on disk, so run 2 seeded
    at ITS start minus 60 s -- above the post made during the downtime."""
    db_path = tmp_path / "store.db"
    clock = {"now": NOW}

    async def run_1():
        async with _watching(db_path, FakeTelegramClient(), clock):
            pass

    asyncio.run(run_1())
    clock["now"] = _at(600)

    async def run_2():
        client = FakeTelegramClient(history={A: [_post(1, 120, SIGNAL_TEXT)]})
        async with _watching(db_path, client, clock):
            pass

    asyncio.run(run_2())

    assert _stored(db_path) == [(1, "backfill")]
    assert _rows(db_path, "SELECT COUNT(*) FROM outbox")[0][0] == len(ACCOUNTS)
    assert _seed_row(db_path) == (1, 1, None)


def test_a_channel_seeded_at_0_is_read_from_0_after_a_restart(tmp_path: Path) -> None:
    """A channel whose every post is newer than the cutoff seeds at 0 --
    and that is persisted, so the next start reads from 0 rather than
    seeding again at its own start."""
    db_path = tmp_path / "store.db"
    clock = {"now": NOW}

    async def run_1():
        client = FakeTelegramClient(history={A: [_post(1, 10)]})
        async with _watching(db_path, client, clock):
            pass

    asyncio.run(run_1())
    assert _seed_row(db_path) == (1, 1, None)


# -- the age of a stored cutoff ---------------------------------------------------------


def test_a_stored_cutoff_older_than_ten_minutes_is_used_and_named(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db_path = tmp_path / "store.db"
    conn = db.connect(db_path, role="writer")
    channel_state.claim_seed_cutoff(conn, channel_id=A, started_at_utc=NOW - timedelta(minutes=30))
    conn.commit()
    conn.close()

    async def scenario():
        client = FakeTelegramClient(history={A: list(OLD_HISTORY)})
        async with _watching(db_path, client, {"now": NOW}):
            return _anchor_queries(client)

    with caplog.at_level(logging.WARNING, logger=listener_module.__name__):
        anchors = asyncio.run(scenario())

    assert anchors == [NOW - timedelta(minutes=30) - FIRST_RUN_SEED_MARGIN]
    named = [r for r in caplog.records if "30 minutes" in r.getMessage()]
    assert len(named) == 1 and named[0].levelname == "WARNING", [r.getMessage() for r in caplog.records]
    _assert_plain(named[0].getMessage())


def test_a_stored_cutoff_older_than_a_day_is_replaced_and_the_gap_named(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db_path = tmp_path / "store.db"
    conn = db.connect(db_path, role="writer")
    channel_state.claim_seed_cutoff(conn, channel_id=A, started_at_utc=NOW - timedelta(days=3))
    conn.commit()
    conn.close()

    async def scenario():
        client = FakeTelegramClient(history={A: list(OLD_HISTORY)})
        async with _watching(db_path, client, {"now": NOW}):
            return _anchor_queries(client)

    with caplog.at_level(logging.WARNING, logger=listener_module.__name__):
        anchors = asyncio.run(scenario())

    assert anchors == [NOW - FIRST_RUN_SEED_MARGIN]
    named = [r for r in caplog.records if "72 hours" in r.getMessage()]
    assert len(named) == 1 and named[0].levelname == "WARNING", [r.getMessage() for r in caplog.records]
    _assert_plain(named[0].getMessage())


def _assert_plain(text: str) -> None:
    for jargon in ("RPC", "client", "listener", "flood"):
        assert jargon not in text, (jargon, text)


# -- F28's watch side: a channel switched back on ------------------------------------------


def test_a_channel_switched_back_on_is_re_seeded_at_its_cutoff(tmp_path: Path) -> None:
    """`channels on` (group 5) writes a cutoff for a channel that already has
    a pointer. The next watch re-seeds it there: posts made while it was
    off are not read, posts since are, and the pointer only moves up."""
    db_path = tmp_path / "store.db"
    switched_on = NOW - timedelta(minutes=5)
    conn = db.connect(db_path, role="writer")
    conn.execute(
        "INSERT INTO channel_state (channel_id, last_processed_message_id, seeded_at_utc) "
        "VALUES (?, 100, '2026-09-01T00:00:00+00:00')",
        (A,),
    )
    channel_state.request_reseed(conn, channel_id=A, cutoff_utc=switched_on)
    conn.commit()
    conn.close()
    while_off = [
        FakeMessage(id=i, message=SIGNAL_TEXT if i == 105 else CHATTER_TEXT,
                    date=switched_on - timedelta(hours=1))
        for i in range(101, 111)
    ]
    since = [FakeMessage(id=111, message=SIGNAL_TEXT, date=switched_on + timedelta(minutes=1))]

    async def scenario():
        client = FakeTelegramClient(history={A: while_off + since})
        async with _watching(db_path, client, {"now": NOW}):
            return _anchor_queries(client)

    anchors = asyncio.run(scenario())

    assert anchors == [switched_on - FIRST_RUN_SEED_MARGIN]
    assert _stored(db_path) == [(111, "backfill")]
    assert _rows(db_path, "SELECT message_id FROM signals") == [(111,)]
    assert _seed_row(db_path) == (111, 1, None)


def test_a_re_seed_never_moves_the_pointer_backward(tmp_path: Path) -> None:
    """The anchor below the cutoff can be older than the pointer (the cutoff
    was written before messages the pointer already passed). The pointer
    stays, and the read goes on from it."""
    db_path = tmp_path / "store.db"
    cutoff = NOW - timedelta(minutes=5)
    conn = db.connect(db_path, role="writer")
    conn.execute(
        "INSERT INTO channel_state (channel_id, last_processed_message_id) VALUES (?, 100)", (A,)
    )
    channel_state.request_reseed(conn, channel_id=A, cutoff_utc=cutoff)
    conn.commit()
    conn.close()
    history = [
        FakeMessage(id=i, message=CHATTER_TEXT,
                    date=cutoff - timedelta(hours=1) if i <= 95 else cutoff + timedelta(seconds=i))
        for i in range(90, 103)
    ]

    async def scenario():
        client = FakeTelegramClient(history={A: history})
        async with _watching(db_path, client, {"now": NOW}):
            return [c.min_id for c in client.get_messages_calls if c.reverse and c.min_id]

    forward_from = asyncio.run(scenario())

    assert forward_from == [100]
    assert _stored(db_path) == [(101, "backfill"), (102, "backfill")]
    assert _seed_row(db_path) == (102, 1, None)
