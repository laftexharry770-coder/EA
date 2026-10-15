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


# -- OFF periods (fix round 2 of F28): OFF skips exactly what was posted while off ----------

SEEDED = "2026-09-01T00:00:00+00:00"
OFF = START + timedelta(microseconds=250)  # the fraction is dropped
ON = START + timedelta(hours=2)


def _periods(conn, channel_id: int = A) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT off_at_utc, on_at_utc FROM channel_off_periods WHERE channel_id = ? "
            "ORDER BY period_id",
            (channel_id,),
        ).fetchall()
    ]


@pytest.mark.parametrize(
    "state",
    [
        {"pointer": 100, "seeded_at": SEEDED},
        {"pointer": 0, "seeded_at": SEEDED},
        {"pointer": 100},
        {"cutoff": "2026-09-05T10:00:00+00:00"},
    ],
    ids=["seeded", "seeded at 0", "a pointer from before the marker", "a first read pending"],
)
def test_switching_off_a_channel_being_read_opens_a_period(conn, state) -> None:
    _insert(conn, **state)
    assert channel_state.record_switch_off(conn, channel_id=A, off_at_utc=OFF) is True
    assert _periods(conn) == [("2026-09-05T11:00:00+00:00", None)]


@pytest.mark.parametrize("row", [False, True], ids=["no row", "a counter's row at 0"])
def test_switching_off_a_channel_never_read_records_nothing(conn, row) -> None:
    """Its first read starts at that watch's start: nothing to skip."""
    if row:
        _insert(conn)
    assert channel_state.record_switch_off(conn, channel_id=A, off_at_utc=OFF) is False
    assert _periods(conn) == []


def test_a_second_switch_off_replaces_the_open_period(conn) -> None:
    """The first `channels off` was recorded but its save failed, so the
    channel stayed ON: only the second switch-off is real."""
    _insert(conn, pointer=100, seeded_at=SEEDED)
    channel_state.record_switch_off(conn, channel_id=A, off_at_utc=OFF)
    channel_state.record_switch_off(conn, channel_id=A, off_at_utc=OFF + timedelta(hours=1))
    assert _periods(conn) == [("2026-09-05T12:00:00+00:00", None)]


def test_switching_on_closes_the_open_period(conn) -> None:
    _insert(conn, pointer=100, seeded_at=SEEDED)
    channel_state.record_switch_off(conn, channel_id=A, off_at_utc=OFF)

    done = channel_state.record_switch_on(
        conn, channel_id=A, on_at_utc=ON + timedelta(microseconds=7), may_replace_first_cutoff=True
    )

    assert done == channel_state.SwitchOn("period_closed", START)
    assert _periods(conn) == [("2026-09-05T11:00:00+00:00", "2026-09-05T13:00:00+00:00")]
    assert _row(conn) == (100, SEEDED, None)  # no re-seed: the period says it all
    assert channel_state.off_periods(conn, A) == (
        channel_state.OffPeriod(
            period_id=1, off_at_utc=START, on_at_utc=START + timedelta(hours=2)
        ),
    )


def test_a_switch_on_never_ends_a_period_before_it_began(conn) -> None:
    """A clock set back between the two: the period is empty, not inverted."""
    _insert(conn, pointer=100, seeded_at=SEEDED)
    channel_state.record_switch_off(conn, channel_id=A, off_at_utc=OFF)
    channel_state.record_switch_on(
        conn, channel_id=A, on_at_utc=START - timedelta(hours=1), may_replace_first_cutoff=True
    )
    assert _periods(conn) == [("2026-09-05T11:00:00+00:00", "2026-09-05T11:00:00+00:00")]


def test_two_switches_between_two_watches_are_two_periods(conn) -> None:
    _insert(conn, pointer=100, seeded_at=SEEDED)
    for hours in (0, 4):
        channel_state.record_switch_off(conn, channel_id=A, off_at_utc=START + timedelta(hours=hours))
        channel_state.record_switch_on(
            conn,
            channel_id=A,
            on_at_utc=START + timedelta(hours=hours + 1),
            may_replace_first_cutoff=True,
        )
    assert [(p.off_at_utc.hour, p.on_at_utc.hour) for p in channel_state.off_periods(conn, A)] == [
        (11, 12),
        (15, 16),
    ]


def test_a_switch_on_with_no_open_period_extends_the_last_one_not_yet_skipped(conn) -> None:
    """The previous `channels on` closed the period but its save failed, so
    the channel stayed OFF until now: the period runs to now."""
    _insert(conn, pointer=100, seeded_at=SEEDED)
    channel_state.record_switch_off(conn, channel_id=A, off_at_utc=OFF)
    channel_state.record_switch_on(conn, channel_id=A, on_at_utc=ON, may_replace_first_cutoff=True)

    done = channel_state.record_switch_on(
        conn, channel_id=A, on_at_utc=ON + timedelta(hours=1), may_replace_first_cutoff=True
    )

    assert done == channel_state.SwitchOn("period_extended", START)
    assert _periods(conn) == [("2026-09-05T11:00:00+00:00", "2026-09-05T14:00:00+00:00")]
    assert _row(conn) == (100, SEEDED, None)


def test_a_switch_on_with_no_switch_off_recorded_re_seeds_from_now(conn) -> None:
    """The switch-off could not be recorded (the database was busy then):
    everything since its last read is skipped, as ruling M12 first had it."""
    _insert(conn, pointer=100, seeded_at=SEEDED)

    done = channel_state.record_switch_on(
        conn, channel_id=A, on_at_utc=ON, may_replace_first_cutoff=False
    )

    # No watch start has measured this computer's clock (R6-5).
    assert done == channel_state.SwitchOn("reseeded", clock_measured=False)
    assert _row(conn) == (100, SEEDED, "2026-09-05T13:00:00+00:00")


@pytest.mark.parametrize(
    ("seconds", "cutoff"),
    [(45, "2026-09-05T12:59:15+00:00"), (0, "2026-09-05T13:00:00+00:00"),
     (-30, "2026-09-05T13:00:00+00:00")],
    ids=["clock 45 s ahead", "clock right", "clock 30 s behind"],
)
def test_a_re_seed_from_now_allows_for_this_computers_clock_being_ahead(
    conn, seconds, cutoff
) -> None:
    """Final fix round 6 (R6-5): the cutoff is this computer's clock, and the
    re-read starts a minute before it by Telegram's. With the clock 45 s
    ahead (as the latest watch start measured it) that would be only 15 s
    before the switch-on, so the cutoff moves back by as much: about a
    minute before now is read, as `channels on` says. A clock behind
    already reads more, by as much as it is behind, and is left as it is."""
    _insert(conn, pointer=100, seeded_at=SEEDED)
    _clock(conn, seconds)

    done = channel_state.record_switch_on(
        conn, channel_id=A, on_at_utc=ON, may_replace_first_cutoff=False
    )

    assert done == channel_state.SwitchOn("reseeded", clock_measured=True)
    assert _row(conn) == (100, SEEDED, cutoff)


@pytest.mark.parametrize("row", [False, True], ids=["no row", "a counter's row at 0"])
def test_a_switch_on_of_a_channel_never_read_changes_nothing(conn, row) -> None:
    if row:
        _insert(conn)
    before = _row(conn)
    done = channel_state.record_switch_on(conn, channel_id=A, on_at_utc=ON, may_replace_first_cutoff=True)
    assert done == channel_state.SwitchOn("never_read")
    assert _row(conn) == before
    assert _periods(conn) == []


def test_a_stray_period_of_a_channel_never_read_is_dropped(conn) -> None:
    conn.execute("INSERT INTO channel_off_periods (channel_id, off_at_utc) VALUES (?, ?)", (A, SEEDED))
    done = channel_state.record_switch_on(conn, channel_id=A, on_at_utc=ON, may_replace_first_cutoff=True)
    assert done.outcome == "never_read"
    assert _periods(conn) == []


@pytest.mark.parametrize("may", [True, False], ids=["no watch running", "watch running"])
def test_a_first_read_pending_is_re_seeded_only_when_no_watch_runs(conn, may) -> None:
    """The reviewer's open (b): a channel whose first read never finished
    keeps its first-attempt cutoff -- and reads everything since -- unless
    the cutoff may be replaced: no watch holds the session, so none is
    seeding the channel at the old one."""
    first = "2026-09-05T10:00:00+00:00"
    _insert(conn, cutoff=first)

    outcome = channel_state.reseed(conn, channel_id=A, cutoff_utc=ON, may_replace_first_cutoff=may)

    assert outcome == ("reseeded" if may else "first_read_kept")
    assert _row(conn) == (0, None, "2026-09-05T13:00:00+00:00" if may else first)


def test_a_re_seed_drops_every_off_period_it_supersedes(conn) -> None:
    """`channels add` of a channel read before: everything since its last
    read is skipped, so its OFF periods have nothing left to say."""
    _insert(conn, pointer=100, seeded_at=SEEDED)
    channel_state.record_switch_off(conn, channel_id=A, off_at_utc=OFF)
    channel_state.record_switch_on(conn, channel_id=A, on_at_utc=ON, may_replace_first_cutoff=True)
    channel_state.record_switch_off(conn, channel_id=A, off_at_utc=ON + timedelta(hours=1))
    _insert(conn, pointer=5, seeded_at=SEEDED, channel_id=B)
    channel_state.record_switch_off(conn, channel_id=B, off_at_utc=OFF)

    assert channel_state.reseed(conn, channel_id=A, cutoff_utc=ON, may_replace_first_cutoff=True) == (
        "reseeded"
    )

    assert _periods(conn) == []
    assert _periods(conn, B) == [("2026-09-05T11:00:00+00:00", None)]


def test_finishing_a_period_moves_the_pointer_up_only_and_deletes_it(conn) -> None:
    _insert(conn, pointer=100, seeded_at=SEEDED)
    channel_state.record_switch_off(conn, channel_id=A, off_at_utc=OFF)
    channel_state.record_switch_on(conn, channel_id=A, on_at_utc=ON, may_replace_first_cutoff=True)
    (period,) = channel_state.off_periods(conn, A)

    assert channel_state.finish_off_period(conn, channel_id=A, period_id=period.period_id,
                                           anchor_id=90) == 100
    assert _periods(conn) == []
    _insert(conn, pointer=100, seeded_at=SEEDED, channel_id=B)
    channel_state.record_switch_off(conn, channel_id=B, off_at_utc=OFF)
    channel_state.record_switch_on(conn, channel_id=B, on_at_utc=ON, may_replace_first_cutoff=True)
    (period,) = channel_state.off_periods(conn, B)
    assert channel_state.finish_off_period(conn, channel_id=B, period_id=period.period_id,
                                           anchor_id=140) == 140
    assert _row(conn, B) == (140, SEEDED, None)


def test_finishing_a_period_never_creates_a_row(conn) -> None:
    """A pointer-0 row without the marker would read as "never seeded"."""
    assert channel_state.finish_off_period(conn, channel_id=A, period_id=99, anchor_id=7) == 0
    assert _row(conn) is None


def test_open_and_unreadable_periods_are_never_skipped(conn) -> None:
    _insert(conn, pointer=100, seeded_at=SEEDED)
    channel_state.record_switch_off(conn, channel_id=A, off_at_utc=OFF)
    conn.execute(
        "INSERT INTO channel_off_periods (channel_id, off_at_utc, on_at_utc) "
        "VALUES (?, 'not a time', ?)",
        (A, SEEDED),
    )
    assert channel_state.off_periods(conn, A) == ()


# -- final fix round 3 (R3-4): each switch keeps the clock difference last measured ---------


def _clock(conn, seconds: int) -> None:
    """A watch start measured this computer's clock `seconds` off Telegram's."""
    from xaucopier.store import heartbeats

    heartbeats.record_clock_difference(
        conn, difference=timedelta(seconds=seconds), now_utc=START - timedelta(days=1)
    )


def _differences(conn, channel_id: int = A) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT off_clock_difference_s, on_clock_difference_s FROM channel_off_periods "
            "WHERE channel_id = ? ORDER BY period_id",
            (channel_id,),
        ).fetchall()
    ]


def test_each_switch_keeps_the_clock_difference_the_latest_watch_start_measured(conn) -> None:
    """The stamps are this computer's clock; a watch start measured how far
    off it was. `channels off` keeps the latest measurement with the
    switch-off, `channels on` with the switch-on: the read around each is
    widened by it, so a clock put right since never skips a post made
    while ON."""
    _insert(conn, pointer=100, seeded_at=SEEDED)
    _clock(conn, 180)
    channel_state.record_switch_off(conn, channel_id=A, off_at_utc=OFF)
    assert _differences(conn) == [(180, None)]
    _clock(conn, -45)
    channel_state.record_switch_on(conn, channel_id=A, on_at_utc=ON, may_replace_first_cutoff=True)

    assert _differences(conn) == [(180, -45)]
    (period,) = channel_state.off_periods(conn, A)
    assert period.off_clock_difference == timedelta(seconds=180)
    assert period.on_clock_difference == timedelta(seconds=-45)


def test_with_no_difference_measured_yet_the_switches_keep_none(conn) -> None:
    _insert(conn, pointer=100, seeded_at=SEEDED)
    channel_state.record_switch_off(conn, channel_id=A, off_at_utc=OFF)
    channel_state.record_switch_on(conn, channel_id=A, on_at_utc=ON, may_replace_first_cutoff=True)

    assert _differences(conn) == [(None, None)]
    (period,) = channel_state.off_periods(conn, A)
    assert (period.off_clock_difference, period.on_clock_difference) == (None, None)


def test_a_switch_off_saved_late_and_an_extended_period_keep_the_latest_difference(conn) -> None:
    """`channels off` saving a switch-off whose time was never saved, and a
    `channels on` extending a closed period, each keep the difference
    measured last."""
    _insert(conn, pointer=100, seeded_at=SEEDED)
    _clock(conn, 30)
    assert channel_state.record_missing_switch_off(conn, channel_id=A, off_at_utc=OFF) is True
    channel_state.record_switch_on(conn, channel_id=A, on_at_utc=ON, may_replace_first_cutoff=True)
    _clock(conn, -150)
    extended = channel_state.record_switch_on(
        conn, channel_id=A, on_at_utc=ON + timedelta(hours=1), may_replace_first_cutoff=True
    )

    assert extended.outcome == "period_extended"
    assert _differences(conn) == [(30, -150)]


def test_an_unreadable_difference_is_kept_as_none(conn) -> None:
    """Whatever is stored, a switch is never refused for it."""
    conn.execute(
        "INSERT INTO heartbeats (name, at_utc, detail) VALUES (?, ?, ?)",
        ("clock_difference", START.isoformat(), "not a number"),
    )
    _insert(conn, pointer=100, seeded_at=SEEDED)
    channel_state.record_switch_off(conn, channel_id=A, off_at_utc=OFF)
    assert _differences(conn) == [(None, None)]


@pytest.mark.parametrize("stored", ["garbled", 1.5])
def test_a_difference_stored_unreadable_with_a_period_reads_as_none(conn, stored) -> None:
    """Final fix round 3 (R3-4): a period whose kept difference cannot be
    read is still skipped, as one with none kept -- never a failed read."""
    _insert(conn, pointer=100, seeded_at=SEEDED)
    channel_state.record_switch_off(conn, channel_id=A, off_at_utc=OFF)
    channel_state.record_switch_on(
        conn, channel_id=A, on_at_utc=OFF + timedelta(hours=1), may_replace_first_cutoff=True
    )
    conn.execute(
        "UPDATE channel_off_periods SET off_clock_difference_s = ?, on_clock_difference_s = ?",
        (stored, stored),
    )

    (period,) = channel_state.off_periods(conn, A)

    assert (period.off_clock_difference, period.on_clock_difference) == (None, None)

