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
    _stored_ids,
    _until_asleep,
)
from xaucopier.ingest import listener as listener_module
from xaucopier.ingest.listener import FIRST_RUN_SEED_MARGIN, Listener
from xaucopier.ingest.poll import HistoryPoll
from xaucopier.store import channel_state, db
from xaucopier.store.messages_repo import ingest

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
        client = FakeTelegramClient(history={A: history}, server_time=NOW)
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
        client = FakeTelegramClient(history={A: history}, server_time=NOW)
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
        client = FakeTelegramClient(history={A: history}, server_time=NOW)
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
        client = FakeTelegramClient(history={A: history}, server_time=NOW)
        async with _watching(db_path, client, {"now": NOW}):
            return [c.min_id for c in client.get_messages_calls if c.reverse and c.min_id]

    forward_from = asyncio.run(scenario())

    assert forward_from == [100]
    assert _stored(db_path) == [(101, "backfill"), (102, "backfill")]
    assert _seed_row(db_path) == (102, 1, None)


# -- fix round 2 of F28: OFF periods are skipped exactly ------------------------------------


def _hours_ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


def _read_before(db_path: Path, *periods: tuple[float, float], pointer: int = 100,
                 seeded: bool = True, cutoff: datetime | None = None) -> None:
    """Channel A as `channels off`/`on` left it: pointer `pointer` (with the
    seeded marker, or a first-read cutoff), and each (off, on) period, in
    hours before NOW."""
    conn = db.connect(db_path, role="writer")
    try:
        conn.execute(
            "INSERT INTO channel_state (channel_id, last_processed_message_id, seeded_at_utc, "
            "seed_cutoff_utc) VALUES (?, ?, ?, ?)",
            (A, pointer, "2026-09-01T00:00:00+00:00" if seeded else None,
             channel_state.format_utc(cutoff) if cutoff is not None else None),
        )
        for off, on in periods:
            channel_state.record_switch_off(conn, channel_id=A, off_at_utc=_hours_ago(off))
            channel_state.record_switch_on(
                conn, channel_id=A, on_at_utc=_hours_ago(on), may_replace_first_cutoff=True
            )
        conn.commit()
    finally:
        conn.close()


def _history(*dated: tuple[int, float]) -> list[FakeMessage]:
    return [FakeMessage(id=i, message=CHATTER_TEXT, date=_hours_ago(h)) for i, h in dated]


def _periods_left(db_path: Path) -> list:
    return _rows(db_path, "SELECT off_at_utc, on_at_utc FROM channel_off_periods")


def test_two_off_periods_between_two_runs_are_each_skipped_exactly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """OFF 5 h ago, ON 4 h ago, OFF 3 h ago, ON 2 h ago, all while watch
    was stopped: what was posted while ON is read, what was posted while
    OFF is not, in order."""
    db_path = tmp_path / "store.db"
    _read_before(db_path, (5, 4), (3, 2))
    history = _history((101, 6), (102, 4.5), (103, 3.5), (104, 2.5), (105, 1))

    async def scenario():
        client = FakeTelegramClient(history={A: history}, server_time=NOW)
        async with _watching(db_path, client, {"now": NOW}):
            return _anchor_queries(client)

    with caplog.at_level(logging.WARNING, logger="xaucopier.ingest.listener"):
        anchors = asyncio.run(scenario())

    assert _stored(db_path) == [(101, "backfill"), (103, "backfill"), (105, "backfill")]
    assert anchors == [_hours_ago(4) - FIRST_RUN_SEED_MARGIN, _hours_ago(2) - FIRST_RUN_SEED_MARGIN]
    assert _pointer(db_path) == 105
    assert _periods_left(db_path) == []
    assert _seed_row(db_path) == (105, 1, None)  # never re-seeded
    skipped = [r.getMessage() for r in caplog.records if "was switched off from" in r.getMessage()]
    either = (
        "Everything it posted before and since is read, and so is what it posted within a "
        "minute of either switch, in case this computer's clock was off by up to that much "
        "when you switched."
    )
    assert skipped == [
        f"Channel {A} ({CHANNEL_A.name}) was switched off from 2026-09-05 06:00:00 to "
        f"2026-09-05 07:00:00 UTC: what it posted in between was not read. {either}",
        f"Channel {A} ({CHANNEL_A.name}) was switched off from 2026-09-05 08:00:00 to "
        f"2026-09-05 09:00:00 UTC: what it posted in between was not read. {either}",
    ]
    for text in skipped:
        _assert_plain(text)


def test_posts_within_a_minute_of_either_switch_are_read(tmp_path: Path) -> None:
    """This computer's clock stamps the switches, Telegram's the posts: a
    post up to a minute after the switch-off, or from a minute before the
    switch-on, is read rather than lost to a clock a little off."""
    db_path = tmp_path / "store.db"
    _read_before(db_path, (3, 2))
    minute = 1 / 60
    history = _history(
        (101, 3 - 0.99 * minute),   # 59 s after the switch-off: read
        (102, 3 - 1.02 * minute),   # 61 s after: skipped
        (103, 2 + 1.02 * minute),   # 61 s before the switch-on: skipped
        (104, 2 + 0.99 * minute),   # 59 s before: read
    )

    async def scenario():
        async with _watching(db_path, FakeTelegramClient(history={A: history}, server_time=NOW), {"now": NOW}):
            pass

    asyncio.run(scenario())

    assert _stored(db_path) == [(101, "backfill"), (104, "backfill")]


def test_the_read_up_to_a_switch_off_can_take_several_reads(tmp_path: Path) -> None:
    """Two posts per read: the period is skipped only once everything
    before the switch-off has been read -- never earlier, or posts before
    it would be jumped over."""
    db_path = tmp_path / "store.db"
    _read_before(db_path, (3, 2))
    history = _history((101, 4), (102, 4), (103, 4), (104, 4), (105, 4), (106, 2.5), (107, 1))

    async def scenario():
        client = FakeTelegramClient(history={A: history}, server_time=NOW)
        listener = Listener(
            client=client,
            channels=(CHANNEL_A,),
            accounts=ACCOUNTS,
            writer=db.Writer(db_path),
            db_path=db_path,
            now=lambda: NOW,
            backfill_limit=2,
            backfill_rpc_timeout=RPC_TIMEOUT,
        )
        await listener.start()  # the backfill: one read of two posts
        steps = [(_stored_ids(db_path), len(_periods_left(db_path)))]
        try:
            for _ in range(3):
                await listener.read_history(CHANNEL_A, source="poll", limit=2,
                                            rpc_timeout=RPC_TIMEOUT)
                steps.append((_stored_ids(db_path), len(_periods_left(db_path))))
        finally:
            await listener.aclose()
        return steps

    steps = asyncio.run(scenario())

    assert steps == [
        ([101, 102], 1),
        ([101, 102, 103, 104], 1),
        # 105 read, 106 met after the switch-off: skipped to the switch-on,
        # then the ordinary read takes 107.
        ([101, 102, 103, 104, 105, 107], 0),
        ([101, 102, 103, 104, 105, 107], 0),
    ]


def test_a_post_before_the_switch_off_that_fails_holds_the_skip_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post before the switch-off that cannot be stored stops the read
    there, as any read; the period must wait for it, or the skip would
    jump the pointer over it and it would be lost for good."""
    from xaucopier.store import messages_repo

    db_path = tmp_path / "store.db"
    _read_before(db_path, (3, 2))
    history = _history((101, 4), (102, 4), (103, 2.5), (104, 1))
    real_ingest = messages_repo.ingest
    fail = {102}

    def flaky_ingest(conn, **kwargs):
        if kwargs["message_id"] in fail:
            raise RuntimeError("simulated write failure")
        return real_ingest(conn, **kwargs)

    monkeypatch.setattr(messages_repo, "ingest", flaky_ingest)

    async def scenario():
        client = FakeTelegramClient(history={A: history}, server_time=NOW)
        async with _watching(db_path, client, {"now": NOW}) as (listener, _time):
            first = (_stored_ids(db_path), _pointer(db_path), len(_periods_left(db_path)))
            fail.clear()
            await listener.read_history(CHANNEL_A, source="poll", limit=100,
                                        rpc_timeout=RPC_TIMEOUT)
            return first

    first = asyncio.run(scenario())

    assert first == ([101], 101, 1)
    assert _stored_ids(db_path) == [101, 102, 104]
    assert _periods_left(db_path) == []


def test_a_first_read_pending_is_seeded_at_its_own_cutoff_before_the_period(
    tmp_path: Path,
) -> None:
    """The reviewer's open (b), the exact way: a channel whose first read
    never finished, switched off and on while watch was stopped, is read
    from its first cutoff up to the switch-off, then from the switch-on."""
    db_path = tmp_path / "store.db"
    _read_before(db_path, (3, 2), pointer=0, seeded=False, cutoff=_hours_ago(5))
    history = _history((100, 30), (101, 4), (102, 2.5), (103, 1))

    async def scenario():
        async with _watching(db_path, FakeTelegramClient(history={A: history}, server_time=NOW), {"now": NOW}):
            pass

    asyncio.run(scenario())

    assert _stored_ids(db_path) == [101, 103]
    assert _periods_left(db_path) == []


def test_a_channel_that_posted_nothing_since_its_switch_off_is_done_with_it(
    tmp_path: Path,
) -> None:
    """The last batch there is ends before the switch-off: caught up, so
    the period is skipped now and does not hold up the reads that follow."""
    db_path = tmp_path / "store.db"
    _read_before(db_path, (3, 2))

    async def scenario():
        history = _history((101, 4))
        async with _watching(db_path, FakeTelegramClient(history={A: history}, server_time=NOW), {"now": NOW}):
            pass

    asyncio.run(scenario())

    assert _stored_ids(db_path) == [101]
    assert _periods_left(db_path) == []


def test_an_open_period_is_never_skipped(tmp_path: Path) -> None:
    """Switched off while this watch was reading it (watch reads the list
    only at start), or a `channels off` whose save failed: the channel is
    still read, and nothing is skipped."""
    db_path = tmp_path / "store.db"
    _read_before(db_path)
    conn = db.connect(db_path, role="writer")
    channel_state.record_switch_off(conn, channel_id=A, off_at_utc=_hours_ago(3))
    conn.commit()
    conn.close()
    history = _history((101, 4), (102, 2.5), (103, 1))

    async def scenario():
        async with _watching(db_path, FakeTelegramClient(history={A: history}, server_time=NOW), {"now": NOW}):
            pass

    asyncio.run(scenario())

    assert _stored_ids(db_path) == [101, 102, 103]
    assert len(_periods_left(db_path)) == 1


# -- final fix FF8/FF17: OFF times are this computer's; posts are Telegram's ---------------


def _stamp_period(db_path: Path, off_at: datetime, on_at: datetime) -> None:
    """`channels off` then `channels on`, stamped at `off_at` and `on_at`
    by this computer's clock, for channel A read before (pointer 100)."""
    conn = db.connect(db_path, role="writer")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO channel_state (channel_id, last_processed_message_id, "
            "seeded_at_utc) VALUES (?, 100, '2026-09-01T00:00:00+00:00')",
            (A,),
        )
        channel_state.record_switch_off(conn, channel_id=A, off_at_utc=off_at)
        channel_state.record_switch_on(
            conn, channel_id=A, on_at_utc=on_at, may_replace_first_cutoff=True
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("skew", [-180, 180], ids=["clock-3-min-slow", "clock-3-min-fast"])
def test_off_times_allow_for_how_far_this_computers_clock_is_off(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, skew: int
) -> None:
    """Final fix FF8/FF17. `channels off`/`on` stamp this computer's clock;
    the posts carry Telegram's. With the clock 3 minutes slow, a post made
    90 s before the real switch-off (the channel still ON) was never read;
    with it 3 minutes fast, a signal posted 90 s after the real switch-on
    was never read -- the only trace a WARNING saying nothing is skipped.
    watch measures the difference at start (Telegram's clock) and reads
    that much more around each switch (round 2: it never shifts them):
    the post before the switch-off and the post after the switch-on are
    read, what was posted while OFF is not, and the line says how far
    around each switch was read."""
    db_path = tmp_path / "store.db"
    real_off, real_on = _hours_ago(3), _hours_ago(2)  # Telegram's time
    clock_off = timedelta(seconds=skew)
    _stamp_period(db_path, real_off + clock_off, real_on + clock_off)
    history = [
        FakeMessage(id=101, message=CHATTER_TEXT, date=real_off - timedelta(seconds=90)),
        FakeMessage(id=102, message=CHATTER_TEXT, date=real_off + timedelta(minutes=30)),
        FakeMessage(id=103, message=CHATTER_TEXT, date=real_on + timedelta(seconds=90)),
    ]

    async def scenario():
        client = FakeTelegramClient(history={A: history}, server_time=NOW)
        async with _watching(db_path, client, {"now": NOW + clock_off}):
            pass

    with caplog.at_level(logging.WARNING, logger="xaucopier.ingest.listener"):
        asyncio.run(scenario())

    assert _stored_ids(db_path) == [101, 103]
    assert _periods_left(db_path) == []
    (skipped,) = [r.getMessage() for r in caplog.records if "was switched off from" in r.getMessage()]
    assert "within 240 seconds of either switch" in skipped
    _assert_plain(skipped)
    clock = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("This computer's clock is")]
    assert len(clock) == 1 and "so nothing is skipped" not in clock[0]


def _ten_posts_around(real_off: datetime, real_on: datetime) -> list[FakeMessage]:
    """101 posted 90 s before the switch-off (the channel ON), 102 90 s
    after it, 103 half an hour into the OFF time, 104 90 s before the
    switch-on, 105 90 s after it (ON again) -- all by Telegram's clock."""
    return [
        FakeMessage(id=101, message=SIGNAL_TEXT, date=real_off - timedelta(seconds=90)),
        FakeMessage(id=102, message=CHATTER_TEXT, date=real_off + timedelta(seconds=90)),
        FakeMessage(id=103, message=CHATTER_TEXT, date=real_off + timedelta(minutes=30)),
        FakeMessage(id=104, message=CHATTER_TEXT, date=real_on - timedelta(seconds=90)),
        FakeMessage(id=105, message=SIGNAL_TEXT, date=real_on + timedelta(seconds=90)),
    ]


@pytest.mark.parametrize("start_skew", [180, -180], ids=["fast-at-start", "slow-at-start"])
def test_a_clock_put_wrong_after_the_switches_never_skips_a_post_made_while_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, start_skew: int
) -> None:
    """Final fix round 2 (FF8/FF17 regression). The clock was right at
    `channels off`/`on` and 3 minutes off when watch starts. Every stamp
    was shifted by the difference measured at the start -- one never
    measured when the stamps were taken: with the clock fast, BUY 101,
    posted 90 s before the switch-off while the channel was ON, was lost;
    with it slow, BUY 105, posted 90 s after the switch-on. Nothing is
    shifted now: the range skipped shrinks by the difference at either
    end, so what was posted while ON is read, and the cost is reading a
    little of what was posted while OFF (102 and 104)."""
    db_path = tmp_path / "store.db"
    real_off, real_on = _hours_ago(3), _hours_ago(2)
    _stamp_period(db_path, real_off, real_on)

    async def scenario():
        client = FakeTelegramClient(
            history={A: _ten_posts_around(real_off, real_on)}, server_time=NOW
        )
        async with _watching(db_path, client, {"now": NOW + timedelta(seconds=start_skew)}):
            pass

    with caplog.at_level(logging.WARNING, logger="xaucopier.ingest.listener"):
        asyncio.run(scenario())

    assert _stored_ids(db_path) == [101, 102, 104, 105]
    assert _periods_left(db_path) == []
    (skipped,) = [r.getMessage() for r in caplog.records if "was switched off from" in r.getMessage()]
    assert "within 240 seconds of either switch" in skipped
    _assert_plain(skipped)


@pytest.mark.parametrize(
    ("stamp_skew", "start_skew"),
    [(-45, 45), (45, -45)],
    ids=["slow-at-switches-fast-at-start", "fast-at-switches-slow-at-start"],
)
def test_a_clock_never_a_minute_off_still_reads_every_post_made_while_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stamp_skew: int, start_skew: int
) -> None:
    """Final fix round 2 (the re-check's p_skew_swing). The clock is never
    more than 45 s off -- no clock warning -- but it moved by 90 s between
    the switches and this start. Slow then fast: BUY 101, posted 20 s
    before the switch-off while the channel was ON and watch stopped, was
    lost to the shift. Fast then slow: the period, closed a minute before
    this start, looked closed AFTER it by this computer's clock, so watch
    left it to the next start and read 102, posted while OFF. Both are
    decided without comparing clocks now."""
    db_path = tmp_path / "store.db"
    real_off, real_on = NOW - timedelta(minutes=30), NOW - timedelta(seconds=60)
    _stamp_period(
        db_path,
        real_off + timedelta(seconds=stamp_skew),
        real_on + timedelta(seconds=stamp_skew),
    )
    history = [
        FakeMessage(id=101, message=SIGNAL_TEXT, date=real_off - timedelta(seconds=20)),
        FakeMessage(id=102, message=CHATTER_TEXT, date=real_off + timedelta(minutes=10)),
        FakeMessage(id=105, message=CHATTER_TEXT, date=real_on + timedelta(seconds=20)),
    ]

    async def scenario():
        client = FakeTelegramClient(history={A: history}, server_time=NOW)
        async with _watching(db_path, client, {"now": NOW + timedelta(seconds=start_skew)}):
            pass

    with caplog.at_level(logging.WARNING, logger="xaucopier.ingest.listener"):
        asyncio.run(scenario())

    assert _stored_ids(db_path) == [101, 105]
    assert _periods_left(db_path) == []
    assert not [r for r in caplog.records if r.getMessage().startswith("This computer's clock is")]
    (skipped,) = [r.getMessage() for r in caplog.records if "was switched off from" in r.getMessage()]
    assert "within 105 seconds of either switch" in skipped


def test_without_telegrams_clock_the_off_times_are_taken_as_they_are(tmp_path: Path) -> None:
    """No answer from Telegram's clock: nothing to correct by -- the times
    as stamped, give or take the minute's margin."""
    db_path = tmp_path / "store.db"
    _stamp_period(db_path, _hours_ago(3), _hours_ago(2))
    history = _history((101, 3 + 30 / 3600), (102, 2.5), (103, 2 - 30 / 3600))

    async def scenario():
        client = FakeTelegramClient(
            history={A: history}, get_state_failure=RuntimeError("no clock")
        )
        async with _watching(db_path, client, {"now": NOW}):
            pass

    asyncio.run(scenario())

    assert _stored_ids(db_path) == [101, 103]


# -- final fix FF24: an OFF period closed while this watch runs is not this watch's -------


def test_a_period_closed_while_this_watch_runs_is_left_to_the_next_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Final fix FF24. watch reads channels.toml only at start, so it keeps
    reading a channel switched off and on while it runs. Its next read
    applied that period anyway: a post the update stream dropped while the
    channel was 'off' was skipped, and the window said "what it posted in
    between was not read" -- untrue. A period closed after this watch
    started is left to the next start; there everything is already read,
    so nothing is skipped and no such line is shown."""
    db_path = tmp_path / "store.db"
    _read_before(db_path)
    history = _history((101, 4))
    clock = {"now": NOW}

    async def first_run():
        client = FakeTelegramClient(history={A: history}, server_time=NOW)
        async with _watching(db_path, client, clock) as (listener, _time):
            # `channels off` at +5 s and `on` at +10 min, while this runs; a
            # post at +3 min whose live update was dropped.
            _stamp_period(db_path, NOW + timedelta(seconds=5), NOW + timedelta(minutes=10))
            client.history[A].append(
                FakeMessage(id=102, message=SIGNAL_TEXT, date=NOW + timedelta(minutes=3))
            )
            clock["now"] = NOW + timedelta(minutes=11)
            client.server_time = clock["now"]
            await listener.read_history(CHANNEL_A, source="poll", limit=100,
                                        rpc_timeout=RPC_TIMEOUT)

    with caplog.at_level(logging.INFO, logger="xaucopier.ingest.listener"):
        asyncio.run(first_run())

    assert _stored_ids(db_path) == [101, 102]
    assert len(_periods_left(db_path)) == 1  # the next start's
    assert not [r for r in caplog.records if "was switched off from" in r.getMessage()]

    caplog.clear()

    async def second_run():
        client = FakeTelegramClient(history={A: history}, server_time=clock["now"])
        async with _watching(db_path, client, clock):
            pass

    with caplog.at_level(logging.WARNING, logger="xaucopier.ingest.listener"):
        asyncio.run(second_run())

    assert _stored_ids(db_path) == [101, 102]
    assert _periods_left(db_path) == []
    assert not [r for r in caplog.records if "was not read" in r.getMessage()]


def test_a_period_extended_while_this_watch_runs_is_skipped_as_it_was_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final fix FF24, round 2. A period closed before this start waits for
    a post before its switch-off that failed. While watch runs, a
    `channels off` whose time could not be saved and a `channels on`
    extend that period to now. This watch skips only what the period
    covered when it started: never what was posted while it ran (104,
    whose live update was dropped), and never what was posted while the
    channel was OFF before it started (102)."""
    from xaucopier.store import messages_repo

    db_path = tmp_path / "store.db"
    _read_before(db_path, (3, 2))
    history = _history((101, 4), (102, 2.5), (103, 1))
    real_ingest = messages_repo.ingest
    fail = {101}

    def flaky_ingest(conn, **kwargs):
        if kwargs["message_id"] in fail:
            raise RuntimeError("simulated write failure")
        return real_ingest(conn, **kwargs)

    monkeypatch.setattr(messages_repo, "ingest", flaky_ingest)
    clock = {"now": NOW}

    async def scenario():
        client = FakeTelegramClient(history={A: history}, server_time=NOW)
        async with _watching(db_path, client, clock) as (listener, _time):
            assert (_stored_ids(db_path), len(_periods_left(db_path))) == ([], 1)
            conn = db.connect(db_path, role="writer")
            try:
                extended = channel_state.record_switch_on(
                    conn, channel_id=A, on_at_utc=NOW + timedelta(minutes=10),
                    may_replace_first_cutoff=False,
                )
                conn.commit()
            finally:
                conn.close()
            assert extended.outcome == "period_extended"
            client.history[A].append(
                FakeMessage(id=104, message=SIGNAL_TEXT, date=NOW + timedelta(minutes=3))
            )
            clock["now"] = NOW + timedelta(minutes=11)
            fail.clear()
            await listener.read_history(CHANNEL_A, source="poll", limit=100,
                                        rpc_timeout=RPC_TIMEOUT)

    asyncio.run(scenario())

    assert _stored_ids(db_path) == [101, 103, 104]
    assert _periods_left(db_path) == []


# -- final fix round 3 (R3-4): the clock difference at the switches is kept ----------------


def _measure_clock(db_path: Path, skew: int) -> None:
    """A watch started (and stopped) with this computer's clock `skew`
    seconds off Telegram's: it measures the difference and keeps it. Only
    channel B is read, so channel A is left as `channels off`/`on` will
    leave it."""

    async def scenario():
        client = FakeTelegramClient(history={B: []}, server_time=NOW - timedelta(hours=4))
        clock = {"now": NOW - timedelta(hours=4) + timedelta(seconds=skew)}
        async with _watching(db_path, client, clock, channels=(CHANNEL_B,)):
            pass

    asyncio.run(scenario())


@pytest.mark.parametrize("skew", [180, -180], ids=["fast-at-switches", "slow-at-switches"])
def test_a_clock_off_at_the_switches_and_put_right_since_never_skips_a_post_made_while_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, skew: int
) -> None:
    """Final fix round 3 (R3-4; the re-check's p_skew_changed +180/0 and
    -180/0). watch warned the clock was 3 minutes off; the user stopped it,
    ran `channels off` and `channels on` with the clock still off, then
    put the clock right, as advised, and started watch. With the clock
    fast, BUY 105, posted 90 s after the switch-on, was skipped; with it
    slow, BUY 101, posted 90 s before the switch-off -- silently: this
    start measured no difference. Each switch now keeps the difference
    the latest watch start measured, and the read around it is widened by
    that too."""
    db_path = tmp_path / "store.db"
    _measure_clock(db_path, skew)
    real_off, real_on = _hours_ago(3), _hours_ago(2)
    clock_off = timedelta(seconds=skew)
    _stamp_period(db_path, real_off + clock_off, real_on + clock_off)

    async def scenario():
        client = FakeTelegramClient(
            history={A: _ten_posts_around(real_off, real_on)}, server_time=NOW
        )
        async with _watching(db_path, client, {"now": NOW}):
            pass

    with caplog.at_level(logging.WARNING, logger="xaucopier.ingest.listener"):
        asyncio.run(scenario())

    stored = _stored_ids(db_path)
    assert 101 in stored and 105 in stored, stored  # posted while ON
    assert 103 not in stored  # half an hour into the OFF time
    assert _periods_left(db_path) == []
    (skipped,) = [r.getMessage() for r in caplog.records if "was switched off from" in r.getMessage()]
    assert "within 240 seconds of either switch" in skipped, skipped
    _assert_plain(skipped)


def test_each_switch_is_widened_by_the_difference_kept_with_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The clock was 3 minutes slow at `channels off` and right at `channels
    on` (a watch start in between measured it right): the read is widened
    at the switch-off only. 104, posted 90 s before the switch-on while
    the channel was OFF, is not read."""
    db_path = tmp_path / "store.db"
    real_off, real_on = _hours_ago(3), _hours_ago(2)
    _measure_clock(db_path, -180)
    conn = db.connect(db_path, role="writer")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO channel_state (channel_id, last_processed_message_id, "
            "seeded_at_utc) VALUES (?, 100, '2026-09-01T00:00:00+00:00')",
            (A,),
        )
        channel_state.record_switch_off(conn, channel_id=A, off_at_utc=real_off - timedelta(seconds=180))
        conn.commit()
    finally:
        conn.close()
    _measure_clock(db_path, 0)
    conn = db.connect(db_path, role="writer")
    try:
        channel_state.record_switch_on(
            conn, channel_id=A, on_at_utc=real_on, may_replace_first_cutoff=True
        )
        conn.commit()
    finally:
        conn.close()

    async def scenario():
        client = FakeTelegramClient(
            history={A: _ten_posts_around(real_off, real_on)}, server_time=NOW
        )
        async with _watching(db_path, client, {"now": NOW}):
            pass

    with caplog.at_level(logging.WARNING, logger="xaucopier.ingest.listener"):
        asyncio.run(scenario())

    assert _stored_ids(db_path) == [101, 105]
    (skipped,) = [r.getMessage() for r in caplog.records if "was switched off from" in r.getMessage()]
    assert "within 240 seconds of the switch-off and a minute of the switch-on" in skipped, skipped
    _assert_plain(skipped)


def test_each_watch_start_keeps_the_clock_difference_it_measured(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """For `channels off`/`on` to keep with their stamps (R3-4): every start
    that reads Telegram's clock keeps the difference it measured; one that
    cannot read it leaves the last one as it was -- quietly: there was
    nothing to keep, so nothing failed."""
    from xaucopier.store import heartbeats

    db_path = tmp_path / "store.db"
    _measure_clock(db_path, 150)

    def kept():
        conn = db.connect(db_path, role="reader")
        try:
            return heartbeats.read_clock_difference(conn)
        finally:
            conn.close()

    assert kept() == timedelta(seconds=150)
    _measure_clock(db_path, -30)
    assert kept() == timedelta(seconds=-30)

    async def no_clock():
        client = FakeTelegramClient(history={B: []}, get_state_failure=RuntimeError("no clock"))
        async with _watching(db_path, client, {"now": NOW}, channels=(CHANNEL_B,)):
            pass

    with caplog.at_level(logging.INFO, logger="xaucopier.ingest.listener"):
        asyncio.run(no_clock())
    assert kept() == timedelta(seconds=-30)
    assert not [r for r in caplog.records if "keeping the clock difference" in r.getMessage()]


# -- final fix round 3 (R3-3): the OFF line says what happened -------------------------------


def _off_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "was switched off from" in r.getMessage()]


@pytest.mark.parametrize("start_skew", [0, 150], ids=["clock-right", "clock-150-s-fast"])
def test_a_short_off_period_says_what_it_posted_while_off_was_read_and_is_handled(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, start_skew: int
) -> None:
    """Final fix round 3 (R3-3; the re-check's p_short_off). The channel was
    OFF for 5 minutes; a BUY posted while OFF, 60 s before the switch-on,
    falls inside what watch reads around the switches. It was read now and
    queued, but the window said 'everything it posted in that time had
    been read already'. It now says it was read and is handled like any
    other post: as the period was no longer than what watch reads around
    its two switches (clock 150 s fast: 210 s each), or, clock right, as
    the post came within the minute read before the switch-on."""
    db_path = tmp_path / "store.db"
    real_on = NOW - timedelta(seconds=30)
    real_off = real_on - timedelta(minutes=5)
    _stamp_period(db_path, real_off, real_on)
    history = [FakeMessage(id=101, message=SIGNAL_TEXT, date=real_on - timedelta(seconds=60))]

    async def scenario():
        client = FakeTelegramClient(history={A: history}, server_time=NOW)
        async with _watching(db_path, client, {"now": NOW + timedelta(seconds=start_skew)}):
            pass

    with caplog.at_level(logging.INFO, logger="xaucopier.ingest.listener"):
        asyncio.run(scenario())

    assert _stored_ids(db_path) == [101]
    assert _rows(db_path, "SELECT state FROM outbox WHERE kind = 'ENTRY'") == [
        ("QUEUED",), ("QUEUED",)
    ]
    (line,) = _off_lines(caplog)
    assert "had been read already" not in line
    assert "so what it posted while off was read and is handled like any other post" in line, line
    if start_skew:
        assert (
            "It was off for no longer than the 420 seconds watch reads around the two "
            "switches (within 210 seconds of either switch, in case this computer's clock "
            "was off)" in line
        ), line
    else:
        assert (
            "It posted only within a minute of either switch, which watch reads in case this "
            "computer's clock was off" in line
        ), line
    _assert_plain(line)


@pytest.mark.parametrize("start_skew", [30, -30], ids=["30-s-fast", "30-s-slow"])
def test_a_channel_off_for_170_s_is_read_in_full_with_the_clock_30_s_off(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, start_skew: int
) -> None:
    """Final fix round 5 (R5-5; the re-check's r41_skew_off). The clock was
    right at `channels off` and `on`, 170 s apart, and 30 s off when watch
    starts -- under the minute that earns a clock warning. watch reads 90 s
    around each switch, so a BUY posted in the middle of those 170 s is read
    and traded: what `channels off`/`on` now say ("about 2 minutes, plus
    twice as much as this computer's clock is off"), not "about 2 minutes"
    flat."""
    db_path = tmp_path / "store.db"
    real_on = NOW - timedelta(seconds=30)
    real_off = real_on - timedelta(seconds=170)
    _stamp_period(db_path, real_off, real_on)
    history = [FakeMessage(id=101, message=SIGNAL_TEXT, date=real_off + timedelta(seconds=85))]

    async def scenario():
        client = FakeTelegramClient(history={A: history}, server_time=NOW)
        async with _watching(db_path, client, {"now": NOW + timedelta(seconds=start_skew)}):
            pass

    with caplog.at_level(logging.INFO, logger="xaucopier.ingest.listener"):
        asyncio.run(scenario())

    assert _stored_ids(db_path) == [101]
    assert _rows(db_path, "SELECT state FROM outbox WHERE kind = 'ENTRY'") == [
        ("QUEUED",), ("QUEUED",)
    ]
    (line,) = _off_lines(caplog)
    assert "within 90 seconds of either switch" in line, line

def test_a_post_read_just_after_the_switch_off_is_said_to_be_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An hour OFF; the channel posted only 30 s after the switch-off --
    within the minute read after it -- and nothing else until it was on
    again. Nothing is skipped, and the line says that post was read, not
    that it had been read already."""
    db_path = tmp_path / "store.db"
    _read_before(db_path, (3, 2))
    history = _history((101, 3 - 30 / 3600), (102, 1))

    async def scenario():
        async with _watching(db_path, FakeTelegramClient(history={A: history}, server_time=NOW), {"now": NOW}):
            pass

    with caplog.at_level(logging.INFO, logger="xaucopier.ingest.listener"):
        asyncio.run(scenario())

    assert _stored_ids(db_path) == [101, 102]
    (line,) = _off_lines(caplog)
    assert "had been read already" not in line
    assert (
        "It posted only within a minute of either switch, which watch reads in case this "
        "computer's clock was off, so what it posted while off was read and is handled like "
        "any other post." in line
    ), line


def test_a_period_whose_posts_a_running_watch_read_says_they_had_been_read_already(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The one case for 'had been read already': switched off and on while
    a watch ran (it kept reading the channel), so at the next start the
    pointer is already past the switch-on and nothing is read again."""
    db_path = tmp_path / "store.db"
    _read_before(db_path)
    history = _history((101, 4))
    clock = {"now": NOW}

    async def first_run():
        client = FakeTelegramClient(history={A: history}, server_time=NOW)
        async with _watching(db_path, client, clock) as (listener, _time):
            _stamp_period(db_path, NOW + timedelta(seconds=5), NOW + timedelta(minutes=10))
            client.history[A].append(
                FakeMessage(id=102, message=SIGNAL_TEXT, date=NOW + timedelta(minutes=3))
            )
            clock["now"] = NOW + timedelta(minutes=11)
            client.server_time = clock["now"]
            await listener.read_history(CHANNEL_A, source="poll", limit=100,
                                        rpc_timeout=RPC_TIMEOUT)

    asyncio.run(first_run())

    async def second_run():
        client = FakeTelegramClient(history={A: history}, server_time=clock["now"])
        async with _watching(db_path, client, clock):
            pass

    with caplog.at_level(logging.INFO, logger="xaucopier.ingest.listener"):
        asyncio.run(second_run())

    assert _stored_ids(db_path) == [101, 102]
    (line,) = _off_lines(caplog)
    assert line.endswith("everything it posted in that time had been read already."), line


def test_a_period_in_which_the_channel_posted_nothing_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R3-3: 'had been read already' only when posts made while OFF had
    been read before. OFF for an hour and nothing posted in it: nothing is
    skipped, nothing read, and the line says it posted nothing."""
    db_path = tmp_path / "store.db"
    _read_before(db_path, (3, 2))
    history = _history((101, 4), (102, 1))

    async def scenario():
        async with _watching(db_path, FakeTelegramClient(history={A: history}, server_time=NOW), {"now": NOW}):
            pass

    with caplog.at_level(logging.INFO, logger="xaucopier.ingest.listener"):
        asyncio.run(scenario())

    assert _stored_ids(db_path) == [101, 102]
    (line,) = _off_lines(caplog)
    assert line == (
        f"Channel {A} ({CHANNEL_A.name}) was switched off from 2026-09-05 08:00:00 to "
        "2026-09-05 09:00:00 UTC; it posted nothing in that time."
    ), line


def test_a_post_read_just_before_the_switch_off_is_no_post_made_while_off(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Final fix round 4 (R4-6; the re-check's p_r33_gap): the channel
    posted 30 s BEFORE it was switched off -- while it was on -- and then
    nothing until it was on again. That post is read like any other, and
    the line says the channel posted nothing while it was off: not that
    what it posted while off was read."""
    db_path = tmp_path / "store.db"
    _read_before(db_path, (3, 2))
    history = _history((101, 3 + 30 / 3600), (102, 1))

    async def scenario():
        async with _watching(db_path, FakeTelegramClient(history={A: history}, server_time=NOW), {"now": NOW}):
            pass

    with caplog.at_level(logging.INFO, logger="xaucopier.ingest.listener"):
        asyncio.run(scenario())

    assert _stored_ids(db_path) == [101, 102]
    (line,) = _off_lines(caplog)
    assert line == (
        f"Channel {A} ({CHANNEL_A.name}) was switched off from 2026-09-05 08:00:00 to "
        "2026-09-05 09:00:00 UTC; it posted nothing in that time."
    ), line


def test_another_channels_post_made_while_this_one_was_off_is_not_its_own(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R4-6: channel A was OFF for an hour and posted nothing in it; channel
    B, read all along, posted in that hour. B's post is not A's: A's line
    says it posted nothing, not that what it posted had been read
    already."""
    db_path = tmp_path / "store.db"
    _read_before(db_path, (3, 2))
    writer = db.connect(db_path, role="writer")
    try:
        ingest(
            writer, channel_cfg=CHANNEL_B, message_id=55, text=CHATTER_TEXT,
            posted_at_utc=_hours_ago(2.5), edit_date_utc=None, reply_to_message_id=None,
            source="live", now_utc=_hours_ago(2.5), accounts=ACCOUNTS,
        )
        writer.commit()
    finally:
        writer.close()
    history = _history((101, 4), (102, 1))

    async def scenario():
        async with _watching(db_path, FakeTelegramClient(history={A: history}, server_time=NOW), {"now": NOW}):
            pass

    with caplog.at_level(logging.INFO, logger="xaucopier.ingest.listener"):
        asyncio.run(scenario())

    assert _stored_ids(db_path) == [101, 102]
    (line,) = _off_lines(caplog)
    assert line.endswith("it posted nothing in that time."), line


@dataclass
class _FailsOnceAfterClient(FakeTelegramClient):
    """Fails the first ordinary read from `fail_min_id` (oldest first) once."""

    fail_min_id: int = 0
    failed: int = 0

    async def get_messages(self, *args, **kwargs):
        if kwargs.get("reverse") and kwargs.get("min_id") == self.fail_min_id and not self.failed:
            self.failed += 1
            raise ConnectionError("simulated dropped read")
        return await super().get_messages(*args, **kwargs)


def test_what_a_skip_did_not_read_is_said_at_once_though_the_read_after_it_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R3-3: the line waits for the read after a skip only when it has to.
    A skip that moved the pointer is said at once: a watch stopped before
    the next read must still say what was not read."""
    db_path = tmp_path / "store.db"
    _read_before(db_path, (3, 2))
    history = _history((101, 4), (102, 2.5), (103, 1))

    async def scenario():
        client = _FailsOnceAfterClient(history={A: history}, server_time=NOW, fail_min_id=102)
        async with _watching(db_path, client, {"now": NOW}):
            return client.failed

    with caplog.at_level(logging.INFO, logger="xaucopier.ingest.listener"):
        failed = asyncio.run(scenario())

    assert failed == 1
    assert _stored_ids(db_path) == [101]  # 103: the next read's
    (line,) = _off_lines(caplog)
    assert "what it posted in between was not read" in line, line


def test_a_period_that_skipped_nothing_is_said_after_the_next_clean_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R3-3: a skip that moved nothing cannot tell yet what was read: the
    read after it takes what was posted within the minute before the
    switch-on. That read failed, so the line waits -- never 'posted
    nothing' -- for the next read, which reads the post and says so."""
    db_path = tmp_path / "store.db"
    _read_before(db_path, (3, 2))
    history = _history((101, 4), (102, 2 + 0.5 / 60), (103, 1))

    async def scenario():
        client = _FailsOnceAfterClient(history={A: history}, server_time=NOW, fail_min_id=101)
        async with _watching(db_path, client, {"now": NOW}) as (listener, _time):
            before = (_off_lines(caplog), _stored_ids(db_path))
            await listener.read_history(CHANNEL_A, source="poll", limit=100,
                                        rpc_timeout=RPC_TIMEOUT)
            return client.failed, before

    with caplog.at_level(logging.INFO, logger="xaucopier.ingest.listener"):
        failed, before = asyncio.run(scenario())

    assert failed == 1
    assert before == ([], [101])
    assert _stored_ids(db_path) == [101, 102, 103]
    (line,) = _off_lines(caplog)
    assert (
        "It posted only within a minute of either switch, which watch reads in case this "
        "computer's clock was off, so what it posted while off was read and is handled like "
        "any other post." in line
    ), line


def test_a_post_read_while_off_before_the_skip_is_said_at_once_though_the_read_after_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R3-3: a post made 30 s after the switch-off was read before the skip,
    so what the line says is known then: it is said at once, not held for
    a read after the skip that fails."""
    db_path = tmp_path / "store.db"
    _read_before(db_path, (3, 2))
    history = _history((101, 3 - 30 / 3600), (102, 1))

    async def scenario():
        client = _FailsOnceAfterClient(history={A: history}, server_time=NOW, fail_min_id=101)
        async with _watching(db_path, client, {"now": NOW}):
            return client.failed

    with caplog.at_level(logging.INFO, logger="xaucopier.ingest.listener"):
        failed = asyncio.run(scenario())

    assert failed == 1
    assert _stored_ids(db_path) == [101]
    (line,) = _off_lines(caplog)
    assert "so what it posted while off was read and is handled like any other post." in line


def test_two_periods_are_said_in_order_each_by_what_was_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R3-3: the first period skipped nothing, and its post 30 s before its
    switch-on is read by the read up to the second period's switch-off --
    which must count it for the first period. The second skipped a post.
    The lines come oldest period first."""
    db_path = tmp_path / "store.db"
    _read_before(db_path, (5, 4), (3, 2))
    history = _history((101, 6), (102, 4 + 0.5 / 60), (103, 2.5), (104, 1))

    async def scenario():
        async with _watching(db_path, FakeTelegramClient(history={A: history}, server_time=NOW), {"now": NOW}):
            pass

    with caplog.at_level(logging.INFO, logger="xaucopier.ingest.listener"):
        asyncio.run(scenario())

    assert _stored_ids(db_path) == [101, 102, 104]
    first, second = _off_lines(caplog)
    assert first.startswith(
        f"Channel {A} ({CHANNEL_A.name}) was switched off from 2026-09-05 06:00:00 to "
        "2026-09-05 07:00:00 UTC. It posted only within a minute of either switch"
    ), first
    assert second.startswith(
        f"Channel {A} ({CHANNEL_A.name}) was switched off from 2026-09-05 08:00:00 to "
        "2026-09-05 09:00:00 UTC: what it posted in between was not read."
    ), second


def test_a_period_waits_for_a_read_after_its_skip_in_which_no_post_failed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3-3: the read after the skip got its posts, but the one made 30 s
    before the switch-on could not be stored: the batch stops there, so
    what was read while off is not known yet. The line waits for the read
    that stores it."""
    from xaucopier.store import messages_repo

    db_path = tmp_path / "store.db"
    _read_before(db_path, (3, 2))
    history = _history((101, 4), (102, 2 + 0.5 / 60), (103, 1))
    real_ingest = messages_repo.ingest
    fail = {102}

    def flaky_ingest(conn, **kwargs):
        if kwargs["message_id"] in fail:
            raise RuntimeError("simulated write failure")
        return real_ingest(conn, **kwargs)

    monkeypatch.setattr(messages_repo, "ingest", flaky_ingest)

    async def scenario():
        client = FakeTelegramClient(history={A: history}, server_time=NOW)
        async with _watching(db_path, client, {"now": NOW}) as (listener, _time):
            before = (_off_lines(caplog), _stored_ids(db_path))
            fail.clear()
            await listener.read_history(CHANNEL_A, source="poll", limit=100,
                                        rpc_timeout=RPC_TIMEOUT)
            return before

    with caplog.at_level(logging.INFO, logger="xaucopier.ingest.listener"):
        before = asyncio.run(scenario())

    assert before == ([], [101])
    assert _stored_ids(db_path) == [101, 102, 103]
    (line,) = _off_lines(caplog)
    assert "It posted only within a minute of either switch" in line, line
