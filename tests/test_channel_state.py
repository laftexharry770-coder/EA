"""Tests for `store/channel_state.py`: where a channel's first read starts,
persisted (final review F01; ledger 141-144; F28's watch side).

A channel's seed cutoff is written at the FIRST attempt and kept across
restarts, so a restart before the first seed succeeds cannot re-anchor at
the new start and skip what the earlier run missed. A successful seed
persists the pointer -- 0 included, through the seeded marker -- and
clears the cutoff. Real SQLite, real migrations.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from xaucopier.store import channel_state, db

A = -1001111111111
B = -1002222222222
START = datetime(2026, 9, 5, 11, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "store.db", role="writer")
    try:
        yield connection
    finally:
        connection.close()


def _row(conn, channel_id: int = A) -> tuple | None:
    row = conn.execute(
        "SELECT last_processed_message_id, seeded_at_utc, seed_cutoff_utc "
        "FROM channel_state WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    return None if row is None else tuple(row)


def _insert(conn, *, pointer: int = 0, seeded_at: str | None = None, cutoff: str | None = None,
            channel_id: int = A) -> None:
    conn.execute(
        "INSERT INTO channel_state (channel_id, last_processed_message_id, seeded_at_utc, "
        "seed_cutoff_utc) VALUES (?, ?, ?, ?)",
        (channel_id, pointer, seeded_at, cutoff),
    )


# -- reading the state ------------------------------------------------------------


def test_a_channel_with_no_row_needs_a_seed(conn) -> None:
    state = channel_state.read(conn, A)
    assert (state.pointer, state.seeded_at_utc, state.seed_cutoff_utc) == (0, None, None)
    assert state.needs_seed


def test_a_pointer_0_row_without_the_marker_still_needs_a_seed(conn) -> None:
    """signals_repo creates such rows for the parse-failure counter: a row
    is not proof that the channel was ever seeded (ledger 114)."""
    _insert(conn, pointer=0)
    assert channel_state.read(conn, A).needs_seed


def test_a_seeded_channel_needs_no_seed(conn) -> None:
    _insert(conn, pointer=0, seeded_at="2026-09-05T10:00:00+00:00", channel_id=A)
    _insert(conn, pointer=5, channel_id=B)  # a pointer above 0 was put there by a seed
    assert not channel_state.read(conn, A).needs_seed
    assert not channel_state.read(conn, B).needs_seed


def test_a_marker_that_cannot_be_read_still_marks_the_channel_seeded(conn) -> None:
    """Only this module writes the marker; whatever it holds, the channel
    was seeded -- seeding it again at a new cutoff could skip posts."""
    _insert(conn, pointer=0, seeded_at="garbled")
    state = channel_state.read(conn, A)
    assert state.seeded_at_utc is None and not state.needs_seed


def test_a_seeded_channel_with_a_cutoff_needs_a_seed_again(conn) -> None:
    """F28's watch side: `channels on` writes a cutoff for a channel that
    already has a pointer, and the next watch re-seeds it there."""
    _insert(conn, pointer=5, seeded_at="2026-09-01T00:00:00+00:00",
            cutoff="2026-09-05T10:59:00+00:00")
    state = channel_state.read(conn, A)
    assert state.needs_seed
    assert state.seed_cutoff_utc == datetime(2026, 9, 5, 10, 59, tzinfo=UTC)


def test_an_unreadable_cutoff_still_needs_a_seed_and_reads_as_none(conn) -> None:
    _insert(conn, pointer=5, cutoff="not a time")
    state = channel_state.read(conn, A)
    assert state.needs_seed and state.seed_cutoff_utc is None
    assert state.seed_cutoff_raw == "not a time"


# -- the cutoff: written once, at the first attempt ----------------------------------


def test_the_first_attempt_writes_the_cutoff_in_whole_seconds(conn) -> None:
    decision = channel_state.claim_seed_cutoff(
        conn, channel_id=A, started_at_utc=START + timedelta(microseconds=700_000)
    )
    assert decision is not None
    assert (decision.action, decision.cutoff_utc, decision.previous_utc) == ("set", START, None)
    assert _row(conn) == (0, None, "2026-09-05T11:00:00+00:00")


def test_a_later_attempt_keeps_the_first_cutoff(conn) -> None:
    """The heart of F01: a restart must not move the cutoff to its own start."""
    channel_state.claim_seed_cutoff(conn, channel_id=A, started_at_utc=START)
    later = channel_state.claim_seed_cutoff(
        conn, channel_id=A, started_at_utc=START + timedelta(minutes=5)
    )
    assert later is not None
    assert (later.action, later.cutoff_utc, later.previous_utc) == ("kept", START, START)
    assert _row(conn) == (0, None, "2026-09-05T11:00:00+00:00")


def test_a_pointer_0_row_without_the_marker_gets_a_cutoff(conn) -> None:
    _insert(conn, pointer=0)
    decision = channel_state.claim_seed_cutoff(conn, channel_id=A, started_at_utc=START)
    assert decision is not None and decision.action == "set"
    assert _row(conn) == (0, None, "2026-09-05T11:00:00+00:00")


@pytest.mark.parametrize(
    "pointer, seeded_at", [(0, "2026-09-05T10:00:00+00:00"), (7, None), (7, "2026-09-01T00:00:00+00:00")]
)
def test_a_seeded_channel_gets_no_cutoff(conn, pointer: int, seeded_at: str | None) -> None:
    _insert(conn, pointer=pointer, seeded_at=seeded_at)
    assert channel_state.claim_seed_cutoff(conn, channel_id=A, started_at_utc=START) is None
    assert _row(conn) == (pointer, seeded_at, None)


@pytest.mark.parametrize(
    "age, replaced",
    [
        (timedelta(hours=24), False),
        (timedelta(hours=24, seconds=1), True),
        (timedelta(days=3), True),
    ],
)
def test_a_cutoff_older_than_24_hours_is_replaced_by_this_start(
    conn, age: timedelta, replaced: bool
) -> None:
    """Ruling (C1 addendum): anything that old is untradeable under the
    120 s age gate, so reading it all would only flood the store."""
    stored = START - age
    _insert(conn, cutoff=stored.isoformat())
    decision = channel_state.claim_seed_cutoff(conn, channel_id=A, started_at_utc=START)
    assert decision is not None
    assert decision.previous_utc == stored
    if replaced:
        assert (decision.action, decision.cutoff_utc) == ("replaced", START)
        assert _row(conn)[2] == "2026-09-05T11:00:00+00:00"
    else:
        assert (decision.action, decision.cutoff_utc) == ("kept", stored)
        assert _row(conn)[2] == stored.isoformat()


def test_a_cutoff_later_than_this_start_is_replaced_by_it(conn) -> None:
    """A cutoff in the future (written by a clock that ran fast) would skip
    posts; the earlier of the two is the safe one."""
    _insert(conn, pointer=5, cutoff=(START + timedelta(seconds=90)).isoformat())
    decision = channel_state.claim_seed_cutoff(conn, channel_id=A, started_at_utc=START)
    assert decision is not None
    assert (decision.action, decision.cutoff_utc) == ("replaced", START)
    assert _row(conn) == (5, None, "2026-09-05T11:00:00+00:00")


def test_an_unreadable_cutoff_is_replaced_by_this_start(conn) -> None:
    _insert(conn, cutoff="yesterday-ish")
    decision = channel_state.claim_seed_cutoff(conn, channel_id=A, started_at_utc=START)
    assert decision is not None
    assert (decision.action, decision.cutoff_utc, decision.previous_utc) == (
        "replaced", START, None
    )
    assert _row(conn)[2] == "2026-09-05T11:00:00+00:00"


def test_a_cutoff_needs_an_aware_start(conn) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        channel_state.claim_seed_cutoff(conn, channel_id=A, started_at_utc=START.replace(tzinfo=None))


# -- a successful seed --------------------------------------------------------------


def test_a_seed_persists_the_pointer_marks_the_channel_and_clears_the_cutoff(conn) -> None:
    channel_state.claim_seed_cutoff(conn, channel_id=A, started_at_utc=START)
    pointer = channel_state.complete_seed(
        conn, channel_id=A, anchor_id=40, cutoff_raw="2026-09-05T11:00:00+00:00",
        seeded_at_utc=START + timedelta(seconds=3, microseconds=5),
    )
    assert pointer == 40
    assert _row(conn) == (40, "2026-09-05T11:00:03+00:00", None)
    assert not channel_state.read(conn, A).needs_seed


def test_an_empty_channel_seeded_at_0_stays_seeded(conn) -> None:
    """Ledger 114/144: a seed at 0 is persisted, so the next start reads
    from 0 instead of seeding again past a first post made meanwhile."""
    channel_state.claim_seed_cutoff(conn, channel_id=A, started_at_utc=START)
    assert channel_state.complete_seed(
        conn, channel_id=A, anchor_id=0, cutoff_raw="2026-09-05T11:00:00+00:00",
        seeded_at_utc=START,
    ) == 0
    state = channel_state.read(conn, A)
    assert (state.pointer, state.needs_seed) == (0, False)
    assert state.seeded_at_utc == START


def test_a_re_seed_never_moves_the_pointer_backward(conn) -> None:
    _insert(conn, pointer=100, seeded_at="2026-09-01T00:00:00+00:00",
            cutoff="2026-09-05T11:00:00+00:00")
    pointer = channel_state.complete_seed(
        conn, channel_id=A, anchor_id=95, cutoff_raw="2026-09-05T11:00:00+00:00",
        seeded_at_utc=START,
    )
    assert pointer == 100
    assert _row(conn) == (100, "2026-09-05T11:00:00+00:00", None)


def test_a_seed_leaves_a_cutoff_written_after_it_started(conn) -> None:
    """Only the cutoff the seed used is cleared: one written meanwhile (a
    `channels on` while watch runs) still asks for its own re-seed."""
    _insert(conn, pointer=100, cutoff="2026-09-05T12:00:00+00:00")
    channel_state.complete_seed(
        conn, channel_id=A, anchor_id=120, cutoff_raw="2026-09-05T11:00:00+00:00",
        seeded_at_utc=START,
    )
    assert _row(conn) == (120, "2026-09-05T11:00:00+00:00", "2026-09-05T12:00:00+00:00")


def test_a_seed_of_a_channel_with_no_row_creates_it(conn) -> None:
    assert channel_state.complete_seed(
        conn, channel_id=A, anchor_id=7, cutoff_raw=None, seeded_at_utc=START
    ) == 7
    assert _row(conn) == (7, "2026-09-05T11:00:00+00:00", None)


def test_complete_seed_refuses_a_negative_anchor_and_a_naive_time(conn) -> None:
    with pytest.raises(ValueError, match="anchor_id"):
        channel_state.complete_seed(conn, channel_id=A, anchor_id=-1, cutoff_raw=None,
                                    seeded_at_utc=START)
    with pytest.raises(ValueError, match="timezone-aware"):
        channel_state.complete_seed(conn, channel_id=A, anchor_id=1, cutoff_raw=None,
                                    seeded_at_utc=START.replace(tzinfo=None))


# -- `channels on` (group 5): ask the next watch to re-seed ------------------------------


def test_request_reseed_writes_a_whole_second_cutoff_and_keeps_the_pointer(conn) -> None:
    _insert(conn, pointer=100, seeded_at="2026-09-01T00:00:00+00:00")
    channel_state.request_reseed(conn, channel_id=A, cutoff_utc=START + timedelta(microseconds=1))
    assert _row(conn) == (100, "2026-09-01T00:00:00+00:00", "2026-09-05T11:00:00+00:00")
    assert channel_state.read(conn, A).needs_seed


def test_request_reseed_creates_the_row_for_a_channel_never_read(conn) -> None:
    channel_state.request_reseed(conn, channel_id=A, cutoff_utc=START)
    assert _row(conn) == (0, None, "2026-09-05T11:00:00+00:00")


def test_request_reseed_needs_an_aware_time(conn) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        channel_state.request_reseed(conn, channel_id=A, cutoff_utc=START.replace(tzinfo=None))
