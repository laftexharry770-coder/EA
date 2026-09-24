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
    read, posts since are. For `channels on` of a channel that already has
    a pointer (F28, ruling M12). Overwrites a cutoff already set; keeps the
    pointer, which a re-seed never moves back. A `watch` already running
    re-seeds the channel at its next read."""
    conn.execute(
        "INSERT INTO channel_state (channel_id, seed_cutoff_utc) VALUES (?, ?) "
        "ON CONFLICT(channel_id) DO UPDATE SET seed_cutoff_utc = excluded.seed_cutoff_utc",
        (channel_id, format_utc(cutoff_utc)),
    )
