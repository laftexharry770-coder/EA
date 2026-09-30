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

import pytest


from fakes.telegram import FakeMessage
from test_edits import (
    CHATTER_TEXT,
    POSTED,
    SIGNAL_ID_MSG,
    SIGNAL_TEXT,
    A,
    ScriptedClient,
    _alerts,
    _message,
    _outbox,
    _round,
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

