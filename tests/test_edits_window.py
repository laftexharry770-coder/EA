"""The window re-read's guards and reach (spec 8.3; Plan 2 Task 10, final
review F03, F18, ledger 164/165, group-3 leftover G3L-2).

- **The circuit breaker** (`edits.is_suspect`) at its boundaries: a read
  missing every message it expected, or at least `SUSPECT_MIN_ABSENT` of
  them and more than half, is not believed (ledger 164).
- **`AbsenceTracker.seen`**: a message a suspect read returned is no
  longer pending, so the next read that misses it starts the two-read
  count again (ledger 165).

Test helpers come from tests/test_edits.py, whose harness runs the real
listener, poll and store against the fake Telegram client.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import test_relay as relay

from fakes.telegram import FakeMessage
from test_edits import (
    CHATTER_TEXT,
    EDIT_1,
    POSTED,
    SL_EDIT,
    SIGNAL_ID_MSG,
    SIGNAL_TEXT,
    A,
    ScriptedClient,
    _alerts,
    _message,
    _metric,
    _outbox,
    _round,
    _rows,
    _run,
    _signal,
    _withdrawn,
)
from xaucopier.ingest.edits import SUSPECT_MIN_ABSENT, AbsenceTracker, is_suspect
from xaucopier.store import edits_repo

# -- the circuit breaker (ledger 164) --------------------------------------------


@pytest.mark.parametrize(
    ("absent", "expected", "suspect"),
    [
        # Nothing expected, or nothing missing: nothing to doubt.
        (0, 0, False),
        (0, 1, False),
        (0, 50, False),
        # Every expected message missing: never believed -- in a window of
        # one or two messages too, where the count rule alone would let an
        # all-None answer void them.
        (1, 1, True),
        (2, 2, True),
        (3, 3, True),
        (50, 50, True),
        # One or two missing in a small window, not all of it: believed.
        (1, 2, False),
        (2, 3, False),
        (2, 4, False),
        # Exactly SUSPECT_MIN_ABSENT (3) missing: suspect only above half.
        (3, 5, True),
        (3, 6, False),
        (3, 7, False),
        # Half is not "more than half".
        (4, 8, False),
        (5, 8, True),
        (25, 50, False),
        (26, 50, True),
    ],
)
def test_the_circuit_breaker_boundaries(absent: int, expected: int, suspect: bool) -> None:
    assert is_suspect(absent, expected) is suspect


def test_the_breaker_minimum_is_three() -> None:
    assert SUSPECT_MIN_ABSENT == 3


# -- AbsenceTracker.seen (ledger 165) ----------------------------------------------


def test_seen_forgets_a_pending_id_and_only_that_one() -> None:
    tracker = AbsenceTracker()
    assert tracker.settle(A, {1, 2, 3}) == frozenset()
    tracker.seen(A, [2, 99])
    assert tracker.pending(A) == frozenset({1, 3})
    # Another channel is not touched.
    tracker.settle(A + 1, {2})
    tracker.seen(A, [1])
    assert tracker.pending(A) == frozenset({3})
    assert tracker.pending(A + 1) == frozenset({2})


def _four_signals_and_chatter() -> list[FakeMessage]:
    """A window of eight: the signal first, then seven chatter posts."""
    messages = [FakeMessage(id=SIGNAL_ID_MSG, message=SIGNAL_TEXT, date=POSTED)]
    messages += [
        FakeMessage(id=SIGNAL_ID_MSG + n, message=f"{CHATTER_TEXT} {n}", date=POSTED)
        for n in range(1, 8)
    ]
    return messages


def _only(*message_ids: int):
    """A by-id answer that returns only these ids (the rest None)."""

    def answer(result: list) -> list:
        return [m if m is not None and m.id in message_ids else None for m in result]

    return answer


def _without_signal(result: list) -> list:
    return [None if m is not None and m.id == SIGNAL_ID_MSG else m for m in result]


def test_a_message_a_suspect_read_returned_is_no_longer_pending(tmp_path) -> None:
    """Read 1 (believed) misses the signal: pending. Read 2 is suspect --
    most of the window missing -- but it DID return the signal. Read 3
    misses the signal once more. Without `seen()`, read 3 would confirm a
    deletion from two reads that were not in a row (the signal came back in
    between) and void a signal that exists; with it, the signal is only
    pending again."""
    db_path = tmp_path / "store.db"
    client = ScriptedClient(history={A: _four_signals_and_chatter()})

    async def body(client, listener, time):
        await _round(listener, time)  # stored, pointer on the newest; one clean read
        client.ids_answers = [
            _without_signal,
            _only(SIGNAL_ID_MSG, SIGNAL_ID_MSG + 1, SIGNAL_ID_MSG + 2),  # 5 of 8 missing
            _without_signal,
        ]
        for _ in range(3):
            await _round(listener, time)

    _run(db_path, body, client=client)

    assert _signal(db_path)["state"] == "NEW"
    assert not _withdrawn(db_path)
    assert _message(db_path)["deleted_at_utc"] is None
    assert _outbox(db_path, "ENTRY") == [("acct-1", "QUEUED"), ("acct-2", "QUEUED")]
    assert [code for _, code in _alerts(db_path)] == [edits_repo.WINDOW_READ_SUSPECT]


# -- the window covers the newest stored messages, pointer or not (F03) ------------

CHATTER_BEFORE = (SIGNAL_ID_MSG, SIGNAL_ID_MSG + 1, SIGNAL_ID_MSG + 2)
NEWEST_SIGNAL = SIGNAL_ID_MSG + 3


def test_a_newest_signal_deleted_before_the_pointer_reaches_it_is_found_by_the_window(
    tmp_path,
) -> None:
    """Final review F03. Only history reads move the pointer, so a signal
    delivered live sits above it until the next forward read passes it --
    and a forward read never passes a message that is gone. Deleted within
    that gap, with the `MessageDeleted` update dropped, it used to stay
    claimable until the channel posted again (hours, maybe). The window
    now takes the newest stored messages whatever the pointer, so the
    deletion is found in the normal two rounds -- and the pointer is still
    never written by it."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        for message_id in CHATTER_BEFORE:
            assert await client.emit(A, FakeMessage(id=message_id, message=CHATTER_TEXT, date=POSTED))
        await _round(listener, time)
        assert listener.read_last_processed_message_id(A) == CHATTER_BEFORE[-1]
        assert await client.emit(A, FakeMessage(id=NEWEST_SIGNAL, message=SIGNAL_TEXT, date=POSTED))
        with client.blackout():
            assert not await client.emit_delete(A, NEWEST_SIGNAL)
        await _round(listener, time)  # missing once: pending
        pending = _signal(db_path, NEWEST_SIGNAL)["state"]
        await _round(listener, time)  # missing twice: deleted
        return pending, listener.read_last_processed_message_id(A)

    pending, pointer = _run(db_path, body)

    assert pending == "NEW"
    assert pointer == CHATTER_BEFORE[-1]  # the window never moves it
    assert _signal(db_path, NEWEST_SIGNAL)["state"] == "VOIDED"
    assert _withdrawn(db_path, NEWEST_SIGNAL)
    message = _message(db_path, NEWEST_SIGNAL)
    assert message["deleted_at_utc"] is not None and message["detected_by"] == "window"
    assert _outbox(db_path, "ENTRY") == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]
    assert _metric(db_path, edits_repo.DELETE_MISSED_BY_EVENTSTREAM_METRIC) == 1
    assert [code for _, code in _alerts(db_path)] == [
        edits_repo.SIGNAL_VOIDED_BY_DELETION,
        edits_repo.WINDOW_VOIDED_SIGNALS,
    ]


def test_an_edit_of_a_message_above_the_pointer_is_applied_once(tmp_path) -> None:
    """The window now also re-reads a live-stored message above the
    pointer; the forward read then meets the same version when it passes
    it. The edit is applied once, and counted once."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        for message_id in CHATTER_BEFORE:
            assert await client.emit(A, FakeMessage(id=message_id, message=CHATTER_TEXT, date=POSTED))
        await _round(listener, time)
        assert await client.emit(A, FakeMessage(id=NEWEST_SIGNAL, message=SIGNAL_TEXT, date=POSTED))
        with client.blackout():
            await client.emit_edit(A, NEWEST_SIGNAL, SL_EDIT, edit_date=EDIT_1)
        await _round(listener, time)
        await _round(listener, time)

    _run(db_path, body)

    signal = _signal(db_path, NEWEST_SIGNAL)
    assert (signal["state"], signal["version"], signal["sl"]) == ("NEW", 2, 4460.0)
    assert _metric(db_path, edits_repo.EDIT_MISSED_BY_EVENTSTREAM_METRIC) == 1
    assert _alerts(db_path) == []


# -- a window deletion that promoted a repost is no mass void (G3L-2) --------------


@pytest.mark.parametrize("order", ["vip-first", "relay-first"])
def test_a_window_deletion_of_a_double_posted_copy_raises_no_void_summary(
    tmp_path, order: str
) -> None:
    """Group-3 leftover G3L-2 (review round 3, NEW-6). The VIP posts a
    call; the relay posts it twice and removes one of its two posts, the
    deletion found only by the window. VIP-first, the relay's first post is
    a copy: removing it is a repeat removal (a WARNING). Relay-first, it
    holds the signal: the signal is voided and the relay's standing repost
    promoted in its place -- the call never stopped. The CRITICAL
    `WINDOW_VOIDED_SIGNALS` ("check they really were deleted") used to
    fire only in the second order; a void whose call is still queued as
    the promoted signal is left out of it, so neither order raises it,
    and the call stays claimable exactly once per account."""
    db_path = tmp_path / "store.db"
    second = relay.COPY + 1

    async def body(client, listener, time):
        vip = relay._post(relay.CALL)
        first = relay._post(relay.COPY, after=timedelta(seconds=30))
        if order == "vip-first":
            assert await client.emit(relay.V, vip)
            assert await client.emit(relay.R, first)
        else:
            assert await client.emit(relay.R, first)
            assert await client.emit(relay.V, vip)
        assert await client.emit(relay.R, relay._post(second, after=timedelta(seconds=60)))
        await _round(listener, time)
        with client.blackout():
            assert not await client.emit_delete(relay.R, relay.COPY)
        await _round(listener, time)
        await _round(listener, time)

    relay._run(db_path, body)

    state = relay._state(db_path)
    codes = relay._codes(state)
    assert ("CRITICAL", edits_repo.WINDOW_VOIDED_SIGNALS) not in codes
    assert [level for level, _ in codes] == ["WARNING"] * len(codes)
    live = [s for s in state["signals"] if s[3] == "NEW" and not s[4]]
    assert len(live) == 1
    assert state["queued"] == [(live[0][0], len(relay.LIVE_ACCOUNTS))]
    if order == "vip-first":
        assert codes == [("WARNING", edits_repo.SIGNAL_REPEAT_REMOVED)]
    else:
        assert codes == [
            ("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION),
            ("WARNING", edits_repo.SIGNAL_REPOST_PROMOTED),
        ]


def test_a_window_void_with_nothing_promoted_is_still_summarised(tmp_path) -> None:
    """The other side of G3L-2: a window deletion that voids a call and
    promotes nothing still raises the CRITICAL summary -- and in a report
    that did both, only the call no post carries any more is named."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        # Two different calls (another SL), each posted once, and chatter.
        assert await client.emit(A, FakeMessage(id=SIGNAL_ID_MSG, message=SIGNAL_TEXT, date=POSTED))
        other = SIGNAL_TEXT.replace("4470", "4465")
        assert await client.emit(A, FakeMessage(id=SIGNAL_ID_MSG + 1, message=other, date=POSTED))
        # ...the first posted again 1 minute later (a double post).
        assert await client.emit(
            A,
            FakeMessage(id=SIGNAL_ID_MSG + 2, message=SIGNAL_TEXT, date=POSTED + timedelta(minutes=1)),
        )
        for n in range(3, 7):
            assert await client.emit(
                A, FakeMessage(id=SIGNAL_ID_MSG + n, message=f"{CHATTER_TEXT} {n}", date=POSTED)
            )
        await _round(listener, time)
        with client.blackout():
            assert not await client.emit_delete(A, SIGNAL_ID_MSG, SIGNAL_ID_MSG + 1)
        await _round(listener, time)
        await _round(listener, time)

    _run(db_path, body)

    (detail,) = _rows(
        db_path, "SELECT detail FROM alerts WHERE code = ?", (edits_repo.WINDOW_VOIDED_SIGNALS,)
    )[0]
    other_signal = _signal(db_path, SIGNAL_ID_MSG + 1)["signal_id"]
    first_signal = _signal(db_path, SIGNAL_ID_MSG)["signal_id"]
    assert "1 signal(s)" in detail and f"[{other_signal}]" in detail
    assert str(first_signal) not in detail.split("signal(s)")[1].split(".")[0]
    assert _signal(db_path, SIGNAL_ID_MSG + 2)["state"] == "NEW"  # promoted, queued
