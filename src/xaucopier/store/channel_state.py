"""Where a channel's reading starts: the seed cutoff and the seeded marker
(migration 4; final review F01, ledger 141-144, F28).

A channel that has never been read -- no `channel_state` row, or a row at
pointer 0 with no `seeded_at_utc` (``signals_repo`` creates such rows for
its parse-failure counter) -- must be *seeded*: its pointer set just below
everything posted since `watch` first tried to read it, so its old history
is never ingested and nothing posted since is skipped. A channel that has a
pointer is seeded again when ``seed_cutoff_utc`` is set on it (`channels
on`, F28: posts made while it was off are not read).

**The cutoff is written at the first attempt and kept.** Before any seed
request, `watch` stores its start as ``seed_cutoff_utc`` for each channel
that needs a seed (`claim_seed_cutoff`), unless one is already stored:
the first attempt wins, across restarts. Every seed then anchors on the
*stored* cutoff, never on the current process's start -- otherwise a watch
whose seed failed for its whole run handed the next run a cutoff at the
next run's start, and every post the first run's update stream dropped (or
that was posted while it was stopped) fell below the new anchor, silently
(F01). A stored cutoff older than `MAX_CUTOFF_AGE` (24 h) is replaced by
this start: anything that old is untradeable under the 120 s age gate, and
reading it all would only flood the store. One later than this start (a
clock that ran fast) is replaced too -- the earlier cutoff is the safe one.

**A seed succeeds once, durably.** `complete_seed` sets the pointer to the
anchor (never below where it already is), stamps ``seeded_at_utc`` and
clears the cutoff it used, in one transaction. A seed at 0 -- every post in
the channel is newer than the cutoff -- is persisted through the marker, so
the next start reads from 0 instead of seeding again past a first post made
meanwhile (ledger 114/144).

Times are stored as ISO-8601 UTC in whole seconds
(``2026-09-05T11:00:00+00:00``). Like every store helper, nothing here
commits: each call runs inside the caller's (the Writer's) transaction.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from xaucopier.store import heartbeats

#: A stored cutoff older than this, relative to the start that finds it, is
#: replaced by that start (ruling C1 addendum).
MAX_CUTOFF_AGE = timedelta(hours=24)

CutoffAction = Literal["set", "kept", "replaced"]


@dataclass(frozen=True, slots=True)
class ChannelState:
    """One channel's reading state. A channel with no row reads as pointer
    0, never seeded, no cutoff."""

    channel_id: int
    pointer: int
    #: When it was seeded, parsed (None if never, or unreadable -- see
    #: `seeded`).
    seeded_at_utc: datetime | None
    #: The stored cutoff, parsed; None if there is none or it is unreadable.
    seed_cutoff_utc: datetime | None
    #: The stored cutoff exactly as stored (None: none) -- what a seed must
    #: match to clear it.
    seed_cutoff_raw: str | None
    #: Whether a seeded marker is stored at all (readable or not).
    seeded: bool = False

    @property
    def needs_seed(self) -> bool:
        """A cutoff is set (a first seed pending, or a re-seed asked for),
        or the channel has never been seeded: pointer 0 with no marker. A
        pointer above 0 was put there by a seed."""
        return self.seed_cutoff_raw is not None or (self.pointer == 0 and not self.seeded)


@dataclass(frozen=True, slots=True)
class CutoffDecision:
    """What `claim_seed_cutoff` did for one channel."""

    channel_id: int
    #: "set" (there was none), "kept" (the first attempt's stays) or
    #: "replaced" (too old, later than this start, or unreadable).
    action: CutoffAction
    #: The cutoff every seed of this channel now uses.
    cutoff_utc: datetime
    #: Exactly as stored now (see `ChannelState.seed_cutoff_raw`).
    cutoff_raw: str
    #: The cutoff that was there before: kept or replaced (None if there
    #: was none, or it was unreadable).
    previous_utc: datetime | None
    #: The channel's pointer: 0 for a first seed, above 0 for a re-seed.
    pointer: int


def format_utc(moment: datetime) -> str:
    """`moment` as stored: ISO-8601 UTC, whole seconds (the fraction is
    dropped, as Telethon drops it from `offset_date` -- ledger 141)."""
    _require_aware(moment, name="the time")
    return moment.astimezone(UTC).replace(microsecond=0).isoformat()


def _parse(text: str | None) -> datetime | None:
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _require_aware(moment: datetime, *, name: str) -> None:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime, got {moment!r}")


def read(conn: sqlite3.Connection, channel_id: int) -> ChannelState:
    """`channel_id`'s reading state. Read-only."""
    row = conn.execute(
        "SELECT last_processed_message_id, seeded_at_utc, seed_cutoff_utc "
        "FROM channel_state WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    if row is None:
        return ChannelState(
            channel_id=channel_id,
            pointer=0,
            seeded_at_utc=None,
            seed_cutoff_utc=None,
            seed_cutoff_raw=None,
        )
    return ChannelState(
        channel_id=channel_id,
        pointer=int(row[0]),
        seeded_at_utc=_parse(row[1]),
        seed_cutoff_utc=_parse(row[2]),
        seed_cutoff_raw=row[2],
        seeded=row[1] is not None,
    )


def claim_seed_cutoff(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    started_at_utc: datetime,
    max_age: timedelta = MAX_CUTOFF_AGE,
) -> CutoffDecision | None:
    """Store `started_at_utc` (whole seconds) as `channel_id`'s seed cutoff
    if it needs a seed and has none -- insert-if-absent, so the first
    attempt wins -- and return the cutoff its seeds must use. A stored one
    is kept, unless it is unreadable, older than `max_age` before
    `started_at_utc`, or later than it; then `started_at_utc` replaces it.
    Returns None, changing nothing, for a channel that needs no seed."""
    _require_aware(started_at_utc, name="started_at_utc")
    start = started_at_utc.astimezone(UTC).replace(microsecond=0)
    start_raw = format_utc(start)
    state = read(conn, channel_id)
    if not state.needs_seed:
        return None

    stored = state.seed_cutoff_utc
    if state.seed_cutoff_raw is not None and stored is not None:
        if start - max_age <= stored <= start:
            return CutoffDecision(
                channel_id=channel_id,
                action="kept",
                cutoff_utc=stored,
                cutoff_raw=state.seed_cutoff_raw,
                previous_utc=stored,
                pointer=state.pointer,
            )
    action: CutoffAction = "set" if state.seed_cutoff_raw is None else "replaced"
    conn.execute(
        "INSERT INTO channel_state (channel_id, seed_cutoff_utc) VALUES (?, ?) "
        "ON CONFLICT(channel_id) DO UPDATE SET seed_cutoff_utc = excluded.seed_cutoff_utc",
        (channel_id, start_raw),
    )
    return CutoffDecision(
        channel_id=channel_id,
        action=action,
        cutoff_utc=start,
        cutoff_raw=start_raw,
        previous_utc=stored,
        pointer=state.pointer,
    )


def complete_seed(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    anchor_id: int,
    cutoff_raw: str | None,
    seeded_at_utc: datetime,
) -> int:
    """A seed of `channel_id` succeeded at `anchor_id` (0: every post in the
    channel is newer than the cutoff). In one statement pair: the pointer
    becomes max(pointer, anchor_id) -- a re-seed never moves it back --,
    ``seeded_at_utc`` is stamped, and the cutoff is cleared if it is still
    `cutoff_raw`, the one this seed used (a cutoff written meanwhile asks
    for a re-seed of its own). Returns the pointer."""
    if isinstance(anchor_id, bool) or not isinstance(anchor_id, int) or anchor_id < 0:
        raise ValueError(f"anchor_id must be a non-negative integer, got {anchor_id!r}")
    seeded_raw = format_utc(seeded_at_utc)
    conn.execute(
        "INSERT INTO channel_state (channel_id, last_processed_message_id, seeded_at_utc) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(channel_id) DO UPDATE SET "
        "last_processed_message_id = MAX(last_processed_message_id, "
        "excluded.last_processed_message_id), "
        "seeded_at_utc = excluded.seeded_at_utc, "
        "seed_cutoff_utc = CASE WHEN seed_cutoff_utc IS ? THEN NULL ELSE seed_cutoff_utc END",
        (channel_id, anchor_id, seeded_raw, cutoff_raw),
    )
    row = conn.execute(
        "SELECT last_processed_message_id FROM channel_state WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    return int(row[0])


def request_reseed(conn: sqlite3.Connection, *, channel_id: int, cutoff_utc: datetime) -> None:
    """Ask the next `watch` to seed `channel_id` again at `cutoff_utc`
    (whole seconds): posts made before it (less the 60 s margin) are not
    read, posts since are. For a channel added back (G2L-3) and for
    `channels on` of a channel whose switch-off was not recorded (F28,
    ruling M12) -- see `reseed`. Overwrites a cutoff already set; keeps the
    pointer, which a re-seed never moves back. A `watch` already running
    re-seeds the channel at its next read."""
    conn.execute(
        "INSERT INTO channel_state (channel_id, seed_cutoff_utc) VALUES (?, ?) "
        "ON CONFLICT(channel_id) DO UPDATE SET seed_cutoff_utc = excluded.seed_cutoff_utc",
        (channel_id, format_utc(cutoff_utc)),
    )


# -- OFF periods: what a channel posted while it was switched off -------------------------
#
# Fix round 2 of F28 (the controller's ruling on the round-1 review): OFF
# skips EXACTLY what the channel posted while it was OFF. `channels off`
# records when (`record_switch_off`), `channels on` when it came back
# (`record_switch_on`), and the next `watch` reads the channel in two
# ranges per period: first everything after the pointer posted up to the
# switch-off -- a 'close all' posted while it was still ON and watch was
# stopped included -- and only then skips to the switch-on
# (`finish_off_period`). One row per period, so two OFF/ON switches
# between two runs of watch are each skipped exactly, and a first read
# still pending keeps its own cutoff.
#
# The stamps are this computer's clock, the posts Telegram's. Each switch
# also keeps how far off this computer's clock was, as the latest watch
# start measured it (`heartbeats.read_clock_difference`; final fix round 3,
# R3-4): a clock off at the switches and put right since -- what watch's
# own clock warning advises -- is still allowed for when the period is read.


@dataclass(frozen=True, slots=True)
class OffPeriod:
    """One OFF period the next read of its channel must skip."""

    period_id: int
    off_at_utc: datetime
    on_at_utc: datetime
    #: How far this computer's clock was off Telegram's when each switch was
    #: stamped, as the latest watch start before it measured (positive:
    #: ahead); None when none was measured (R3-4).
    off_clock_difference: timedelta | None = None
    on_clock_difference: timedelta | None = None


ReseedOutcome = Literal["reseeded", "never_read", "first_read_kept"]
SwitchOnOutcome = Literal[
    "period_closed", "period_extended", "reseeded", "never_read", "first_read_kept"
]


@dataclass(frozen=True, slots=True)
class SwitchOn:
    """What `record_switch_on` did."""

    #: "period_closed": the open OFF period now ends at the switch-on;
    #: "period_extended": no open one, so the latest closed one not yet
    #: skipped ends there instead (the previous `channels on` did not get
    #: saved, or the switch-off since was not recorded); "never_read":
    #: nothing to skip, the first read of the channel starts at that
    #: watch's start anyway; or `reseed`'s outcome, when no switch-off was
    #: recorded at all.
    outcome: SwitchOnOutcome
    #: The switch-off the skipped period starts at, for the two period
    #: outcomes; None otherwise.
    off_at_utc: datetime | None = None


def reading_started(state: ChannelState) -> bool:
    """Whether a `watch` has started reading the channel: it has a pointer,
    the seeded marker, or a seed cutoff stored (a first read pending, or a
    re-seed asked for). A channel never read has nothing to skip: its
    first read starts at that watch's start."""
    return state.pointer > 0 or state.seeded or state.seed_cutoff_raw is not None


def record_switch_off(conn: sqlite3.Connection, *, channel_id: int, off_at_utc: datetime) -> bool:
    """`channels off`: open an OFF period for `channel_id` at `off_at_utc`
    (whole seconds), if its reading has started, keeping with it the clock
    difference the latest watch start measured (`_clock_now`). An open
    period already there -- left by an earlier `channels off` whose save
    failed, so the channel stayed ON -- is replaced. Returns whether a
    period was opened.
    """
    off_raw = format_utc(off_at_utc)
    if not reading_started(read(conn, channel_id)):
        return False
    conn.execute(
        "DELETE FROM channel_off_periods WHERE channel_id = ? AND on_at_utc IS NULL",
        (channel_id,),
    )
    conn.execute(
        "INSERT INTO channel_off_periods (channel_id, off_at_utc, off_clock_difference_s) "
        "VALUES (?, ?, ?)",
        (channel_id, off_raw, _clock_now(conn)),
    )
    return True


def _clock_now(conn: sqlite3.Connection) -> int | None:
    """The clock difference to keep with a switch stamped now: the one the
    latest watch start measured, in signed whole seconds, or None (R3-4)."""
    difference = heartbeats.read_clock_difference(conn)
    return None if difference is None else int(difference.total_seconds())


def _seconds(value: object) -> timedelta | None:
    """A stored clock difference read back; anything unreadable is None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return timedelta(seconds=value)


def record_missing_switch_off(
    conn: sqlite3.Connection, *, channel_id: int, off_at_utc: datetime
) -> bool:
    """`channels off` for a channel already OFF (final fix FF11): when its
    reading has started and it has no OPEN OFF period -- the switch-off's
    time was never saved (the database was busy then, or the file was
    edited by hand) -- open one at `off_at_utc` (whole seconds), so the
    next `channels on` skips from here rather than re-seeding (everything
    since the last read) or extending an earlier period over the time the
    channel was ON (round 2: a closed period no watch has skipped yet
    does not count). Later than the real switch-off, so a little more is
    read, never less. An open period is kept as it is. Returns whether one
    was opened.

    The cost: a `channels on` whose save failed left the channel OFF with
    its period closed, and an `off` then opens a second one -- the time in
    between is read, not skipped. More is read, never less."""
    if not reading_started(read(conn, channel_id)):
        return False
    if conn.execute(
        "SELECT 1 FROM channel_off_periods WHERE channel_id = ? AND on_at_utc IS NULL LIMIT 1",
        (channel_id,),
    ).fetchone() is not None:
        return False
    conn.execute(
        "INSERT INTO channel_off_periods (channel_id, off_at_utc, off_clock_difference_s) "
        "VALUES (?, ?, ?)",
        (channel_id, format_utc(off_at_utc), _clock_now(conn)),
    )
    return True


def record_switch_on(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    on_at_utc: datetime,
    may_replace_first_cutoff: bool,
) -> SwitchOn:
    """`channels on`: end `channel_id`'s OFF period at `on_at_utc` (whole
    seconds; never before the period's own start, whatever the clock did),
    keeping with it the clock difference the latest watch start measured.

    - An open period is closed there.
    - None open, but one closed and not yet skipped: that one is extended
      to here. Either the previous `channels on` was not saved (the
      channel stayed OFF, so this is exact), or the switch-off since was
      not recorded -- and then this skips less than `reseed` would.
    - None at all, and a channel never read: nothing to do (any stray
      period is dropped), as if it had never been off.
    - None at all for a channel read before (its switch-off was not
      recorded: the database could not be written then, or the file was
      edited by hand): `reseed` -- everything since its last read is
      skipped, as ruling M12 first had it.
    """
    on_at = datetime.fromisoformat(format_utc(on_at_utc))
    row = conn.execute(
        "SELECT period_id, off_at_utc FROM channel_off_periods "
        "WHERE channel_id = ? AND on_at_utc IS NULL",
        (channel_id,),
    ).fetchone()
    outcome: SwitchOnOutcome = "period_closed"
    if row is None:
        row = conn.execute(
            "SELECT period_id, off_at_utc FROM channel_off_periods "
            "WHERE channel_id = ? AND on_at_utc IS NOT NULL "
            "ORDER BY off_at_utc DESC, period_id DESC LIMIT 1",
            (channel_id,),
        ).fetchone()
        outcome = "period_extended"
    if row is not None and reading_started(read(conn, channel_id)):
        period_id, off_raw = row
        off_at = _parse(off_raw)
        ends = max(on_at, off_at) if off_at is not None else on_at
        conn.execute(
            "UPDATE channel_off_periods SET on_at_utc = ?, on_clock_difference_s = ? "
            "WHERE period_id = ?",
            (format_utc(ends), _clock_now(conn), period_id),
        )
        return SwitchOn(outcome=outcome, off_at_utc=off_at)
    return SwitchOn(
        outcome=reseed(
            conn,
            channel_id=channel_id,
            cutoff_utc=on_at_utc,
            may_replace_first_cutoff=may_replace_first_cutoff,
        )
    )


def reseed(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    cutoff_utc: datetime,
    may_replace_first_cutoff: bool,
) -> ReseedOutcome:
    """Ask the next `watch` to read `channel_id` from `cutoff_utc` on,
    skipping everything posted since it last read it (`request_reseed`),
    and drop its OFF periods, which this supersedes. For `channels add` /
    `resolve-channels` of a channel read before (G2L-3) and `channels on`
    with no switch-off recorded.

    A channel whose first read has not finished (a cutoff stored, no
    pointer, no marker) gets the new cutoff only when
    `may_replace_first_cutoff` -- no `watch` holds the session -- since a
    running one may still be seeding it at the old one (fix round 2, the
    reviewer's open (b)); otherwise it is left as it is
    ("first_read_kept"). A channel never read is left alone
    ("never_read"): its first read starts at that watch's start anyway."""
    format_utc(cutoff_utc)  # an aware time, before anything is decided
    state = read(conn, channel_id)
    if not reading_started(state):
        conn.execute("DELETE FROM channel_off_periods WHERE channel_id = ?", (channel_id,))
        return "never_read"
    if not (state.pointer > 0 or state.seeded) and not may_replace_first_cutoff:
        return "first_read_kept"
    request_reseed(conn, channel_id=channel_id, cutoff_utc=cutoff_utc)
    conn.execute("DELETE FROM channel_off_periods WHERE channel_id = ?", (channel_id,))
    return "reseeded"


def off_periods(conn: sqlite3.Connection, channel_id: int) -> tuple[OffPeriod, ...]:
    """`channel_id`'s closed OFF periods not yet skipped, oldest first.
    Read-only. A period whose times cannot be read is left out (never
    skipped: what it covers is read, not lost)."""
    periods = []
    for period_id, off_raw, on_raw, off_clock, on_clock in conn.execute(
        "SELECT period_id, off_at_utc, on_at_utc, off_clock_difference_s, "
        "on_clock_difference_s FROM channel_off_periods "
        "WHERE channel_id = ? AND on_at_utc IS NOT NULL ORDER BY off_at_utc, period_id",
        (channel_id,),
    ).fetchall():
        off_at, on_at = _parse(off_raw), _parse(on_raw)
        if off_at is not None and on_at is not None:
            periods.append(
                OffPeriod(
                    period_id=period_id,
                    off_at_utc=off_at,
                    on_at_utc=on_at,
                    off_clock_difference=_seconds(off_clock),
                    on_clock_difference=_seconds(on_clock),
                )
            )
    return tuple(periods)


def finish_off_period(
    conn: sqlite3.Connection, *, channel_id: int, period_id: int, anchor_id: int
) -> int:
    """The read caught up to an OFF period's start; skip the period: the
    pointer becomes max(pointer, `anchor_id`) -- the newest post before
    the switch-on, less the margin -- and the period is deleted, in one
    transaction (the caller's). Returns the pointer. Only an existing row
    is moved: a channel with a period has one."""
    if isinstance(anchor_id, bool) or not isinstance(anchor_id, int) or anchor_id < 0:
        raise ValueError(f"anchor_id must be a non-negative integer, got {anchor_id!r}")
    conn.execute(
        "UPDATE channel_state SET last_processed_message_id = "
        "MAX(last_processed_message_id, ?) WHERE channel_id = ?",
        (anchor_id, channel_id),
    )
    conn.execute("DELETE FROM channel_off_periods WHERE period_id = ?", (period_id,))
    return read(conn, channel_id).pointer
