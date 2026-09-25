"""Relay copies: the same call, or the same follow-up, reaching us twice
(spec G7; final review F02, F04, F08, F09).

The user's channels copy each other: a free channel forwards the VIP's
posts, the user's own channel re-posts them, a poster deletes a call and
posts it again. `UNIQUE(channel_id, message_id)` cannot say "this is the
call we already have", so this module does, for `messages_repo.ingest()`
(a message stored for the first time) and `edits_repo` (a copy edited or
deleted, a withdrawn call's repost promoted). Everything here runs inside
the caller's writer job and never commits.

**What is a copy of a signal** (`find_signal_relay`), in this order:

1. *The same post, however late* (F09). A forward carries the post it
   copies (`fwd_from_channel_id`, `fwd_from_post_id`). If that post is
   stored, or another stored forward carries the same post, or a stored
   forward carries *this* message, the new message is a copy of that
   message's signal -- its own `signals` row, or the signal it was itself
   a copy of (`messages.relay_of_signal_id`) -- however long after the
   original it arrives. A forward keeps the original's words, so this is
   exact; its own date is when it was forwarded, which is why the window
   below cannot be trusted for it.
2. *The same trade, close together*: a stored signal whose trade hash --
   current, or the one it was first accepted with -- equals this one's,
   posted within `relay_window_seconds` (both ends included) either side.
   This is how a call re-posted by hand is recognised.

**A withdrawn call** (F04). A signal that is VOIDED, INVALID or withdrawn
and of which nothing was executed (`signals_repo.trade_status` is
"pending") is no longer a call anyone acts on:

- a same-trade post *on that signal's own channel* is not a copy of it:
  the poster deleted (or broke) the call and posted it again, and the new
  post is a new signal;
- a copy of it from another channel, or a forward of it, is still held
  back -- one source withdrew the call -- but raises a CRITICAL alert
  naming both messages (`COPY_OF_WITHDRAWN_SIGNAL`), because the call may
  be meant after all;
- a live match is always preferred to a withdrawn one.

A signal that *was* executed and then withdrawn still matches: trading
its repost would open the same trade twice.

**What is a copy of a follow-up** (`find_followup_relay`, F08): the same
post (forwards, as above), or -- for a follow-up whose target signal is
known because it replies to a stored signal message -- a stored
follow-up with the same instruction (`op`) and the same normalised text
targeting the same signal within the window. A follow-up that replies to
nothing is never matched by its text: two channels each saying "close
half" may be two genuine instructions. A stored follow-up whose message
was deleted, or edited into something else, no longer counts on its own
channel (the poster withdrew it and may post it again).

**Every copy is visible.** `record_signal_copy` / `record_followup_copy`
store the link on the copy's `messages` row, log it at INFO with both
messages, and bump the durable counters `relay_duplicates` /
`relay_duplicate_followups`.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from xaucopier.store import alerts, metrics, signals_repo

logger = logging.getLogger(__name__)

#: Counts every signal message stored as a copy of a signal we already have.
RELAY_DUPLICATES_METRIC = "relay_duplicates"
#: Counts every follow-up stored as a copy of one we already have.
RELAY_DUPLICATE_FOLLOWUPS_METRIC = "relay_duplicate_followups"

#: CRITICAL: a copy (from another channel, or a forward) of a signal that
#: was withdrawn before anything was executed; the copy is not traded.
COPY_OF_WITHDRAWN_SIGNAL = "COPY_OF_WITHDRAWN_SIGNAL"

#: `followups.resolution_path` of a follow-up replying to a signal's own message.
REPLY_PATH = "reply"
#: ... and of one replying to a stored copy of a signal.
RELAY_REPLY_PATH = "relay-reply"

MatchVia = Literal["forward", "hash"]

#: Signal states no one acts on (a withdrawn signal may still be NEW: live).
_CLOSED_STATES = frozenset({"VOIDED", "INVALID"})


@dataclass(frozen=True, slots=True)
class SignalMatch:
    """The signal a new message repeats."""

    signal_id: int
    #: The signal's own message.
    channel_id: int
    message_id: int
    #: How it was recognised: the same post ("forward"), or the same trade
    #: close together ("hash").
    via: MatchVia
    #: The signal is VOIDED, INVALID or withdrawn and none of it was executed.
    withdrawn_unexecuted: bool
    #: Its state, for the log and the alert.
    state: str


@dataclass(frozen=True, slots=True)
class FollowupMatch:
    """The follow-up a new follow-up message repeats."""

    followup_id: int
    #: The follow-up's own message.
    channel_id: int
    message_id: int
    via: MatchVia


# -- signals -------------------------------------------------------------------


def is_withdrawn_unexecuted(conn: sqlite3.Connection, signal: sqlite3.Row) -> bool:
    """`signal` (a `signals` row) is out of play -- VOIDED, INVALID or
    withdrawn -- and nothing of it was executed or is being executed."""
    closed = signal["state"] in _CLOSED_STATES or signal["withdrawn_at_utc"] is not None
    return closed and signals_repo.trade_status(conn, signal_id=signal["signal_id"]) == "pending"


def find_signal_relay(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    message_id: int,
    trade_hash: str,
    posted_at_utc: datetime,
    relay_window_seconds: int,
    fwd_from_channel_id: int | None,
    fwd_from_post_id: int | None,
) -> SignalMatch | None:
    """The signal the new signal message (`channel_id`, `message_id`)
    repeats, or None -- see the module docstring for the rules and their
    order. The new message's own row may already be stored; it is never
    matched against itself."""
    for stored in _same_post(
        conn,
        channel_id=channel_id,
        message_id=message_id,
        fwd_from_channel_id=fwd_from_channel_id,
        fwd_from_post_id=fwd_from_post_id,
    ):
        signal = _signal_of(conn, stored)
        if signal is not None:
            return _signal_match(conn, signal, via="forward")

    withdrawn_elsewhere: sqlite3.Row | None = None
    for signal in _same_trade(
        conn, trade_hash=trade_hash, posted_at_utc=posted_at_utc, window=relay_window_seconds
    ):
        if (signal["channel_id"], signal["message_id"]) == (channel_id, message_id):
            continue
        if not is_withdrawn_unexecuted(conn, signal):
            return _signal_match(conn, signal, via="hash")  # a live call wins
        if signal["channel_id"] != channel_id and withdrawn_elsewhere is None:
            withdrawn_elsewhere = signal
        # Withdrawn on this channel: the poster posted the call again.
    if withdrawn_elsewhere is not None:
        return _signal_match(conn, withdrawn_elsewhere, via="hash")
    return None


def record_signal_copy(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    message_id: int,
    match: SignalMatch,
    now_utc: datetime,
) -> tuple[int, ...]:
    """Store that (`channel_id`, `message_id`) -- already on `messages` --
    repeats `match`'s signal: the link, the INFO line, the counter, and
    for a copy of a withdrawn call the CRITICAL alert. Returns the ids of
    the alerts raised."""
    conn.execute(
        "UPDATE messages SET relay_of_signal_id = ? WHERE channel_id = ? AND message_id = ?",
        (match.signal_id, channel_id, message_id),
    )
    metrics.bump(conn, day_key=metrics.day_key_for(now_utc), name=RELAY_DUPLICATES_METRIC)
    logger.info(
        "channel %s message %s is a relayed copy of signal %s (channel %s message %s, %s; "
        "matched by %s): stored, not traded again",
        channel_id,
        message_id,
        match.signal_id,
        match.channel_id,
        match.message_id,
        match.state,
        "the forwarded post" if match.via == "forward" else "the same trade posted close by",
    )
    if not match.withdrawn_unexecuted:
        return ()
    alert_id = alerts.record(
        conn,
        now_utc=now_utc,
        level="CRITICAL",
        code=COPY_OF_WITHDRAWN_SIGNAL,
        channel_id=channel_id,
        message_id=message_id,
        signal_id=match.signal_id,
        detail=(
            f"message {message_id} on channel {channel_id} repeats signal {match.signal_id} "
            f"(message {match.message_id} on channel {match.channel_id}), which was "
            f"withdrawn before anything was executed (it is {match.state}). This copy was "
            "not traded either: one source withdrew the call. If the call is still meant, "
            "trade it by hand."
        ),
    )
    return (alert_id,)


def copies_of(conn: sqlite3.Connection, *, signal_id: int) -> list[sqlite3.Row]:
    """Every stored message recorded as a copy of `signal_id`, oldest id
    first (deleted ones and ones since edited into something else too)."""
    return conn.execute(
        "SELECT * FROM messages WHERE relay_of_signal_id = ? ORDER BY channel_id, message_id",
        (signal_id,),
    ).fetchall()


def has_copies(conn: sqlite3.Connection, *, signal_id: int) -> bool:
    """Whether any stored message is recorded as a copy of `signal_id`."""
    row = conn.execute(
        "SELECT 1 FROM messages WHERE relay_of_signal_id = ? LIMIT 1", (signal_id,)
    ).fetchone()
    return row is not None


def standing_copies_on(
    conn: sqlite3.Connection, *, signal_id: int, channel_id: int
) -> list[sqlite3.Row]:
    """`signal_id`'s copies on `channel_id` that still stand: not deleted,
    still read as a signal. Lowest message id (the earliest post) first."""
    return conn.execute(
        "SELECT * FROM messages WHERE relay_of_signal_id = ? AND channel_id = ? "
        "  AND deleted_at_utc IS NULL AND kind = 'signal' ORDER BY message_id",
        (signal_id, channel_id),
    ).fetchall()


# -- follow-ups ----------------------------------------------------------------


def resolve_reply_target(
    conn: sqlite3.Connection, *, channel_id: int, reply_to_message_id: int | None
) -> tuple[int, str] | None:
    """The signal a follow-up replying to (`channel_id`,
    `reply_to_message_id`) is about, and how it was found: the replied-to
    message's own signal (`"reply"`), or the signal it is a stored copy of
    (`"relay-reply"`). None when it replies to nothing stored as either."""
    if reply_to_message_id is None:
        return None
    own = signals_repo.get_signal_id(
        conn, channel_id=channel_id, message_id=reply_to_message_id
    )
    if own is not None:
        return own, REPLY_PATH
    row = conn.execute(
        "SELECT relay_of_signal_id FROM messages WHERE channel_id = ? AND message_id = ?",
        (channel_id, reply_to_message_id),
    ).fetchone()
    if row is not None and row[0] is not None:
        return row[0], RELAY_REPLY_PATH
    return None


def find_followup_relay(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    message_id: int,
    op: str,
    content_hash: str,
    target_signal_id: int | None,
    posted_at_utc: datetime,
    relay_window_seconds: int,
    fwd_from_channel_id: int | None,
    fwd_from_post_id: int | None,
) -> FollowupMatch | None:
    """The follow-up the new follow-up message repeats, or None -- see
    the module docstring."""
    for stored in _same_post(
        conn,
        channel_id=channel_id,
        message_id=message_id,
        fwd_from_channel_id=fwd_from_channel_id,
        fwd_from_post_id=fwd_from_post_id,
    ):
        followup = _followup_of(conn, stored)
        if followup is not None:
            return FollowupMatch(followup["followup_id"], followup["channel_id"],
                                 followup["message_id"], "forward")

    if target_signal_id is None:
        return None  # never matched by its text alone
    rows = conn.execute(
        "SELECT f.followup_id, f.channel_id, f.message_id, m.posted_at_utc, "
        "       m.deleted_at_utc, m.kind "
        "FROM followups f "
        "JOIN messages m ON m.channel_id = f.channel_id AND m.message_id = f.message_id "
        "WHERE f.edit_seq = 0 AND f.target_signal_id IS ? AND f.op = ? AND m.content_hash = ? "
        "ORDER BY f.followup_id",
        (target_signal_id, op, content_hash),
    ).fetchall()
    window = timedelta(seconds=relay_window_seconds)
    for row in rows:
        if (row["channel_id"], row["message_id"]) == (channel_id, message_id):
            continue
        withdrawn = row["deleted_at_utc"] is not None or row["kind"] != "followup"
        if withdrawn and row["channel_id"] == channel_id:
            continue  # the poster withdrew it here and said it again
        if abs(datetime.fromisoformat(row["posted_at_utc"]) - posted_at_utc) <= window:
            return FollowupMatch(row["followup_id"], row["channel_id"], row["message_id"], "hash")
    return None


def record_followup_copy(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    message_id: int,
    match: FollowupMatch,
    now_utc: datetime,
) -> None:
    """Store that (`channel_id`, `message_id`) repeats `match`'s follow-up:
    the link, the INFO line, the counter."""
    conn.execute(
        "UPDATE messages SET relay_of_followup_id = ? WHERE channel_id = ? AND message_id = ?",
        (match.followup_id, channel_id, message_id),
    )
    metrics.bump(
        conn, day_key=metrics.day_key_for(now_utc), name=RELAY_DUPLICATE_FOLLOWUPS_METRIC
    )
    logger.info(
        "channel %s message %s is a relayed copy of follow-up %s (channel %s message %s; "
        "matched by %s): stored, not dispatched again",
        channel_id,
        message_id,
        match.followup_id,
        match.channel_id,
        match.message_id,
        "the forwarded post" if match.via == "forward" else "the same instruction on the same signal",
    )


# -- helpers -------------------------------------------------------------------


def _same_post(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    message_id: int,
    fwd_from_channel_id: int | None,
    fwd_from_post_id: int | None,
) -> list[sqlite3.Row]:
    """Stored messages that are the same Telegram post as the new one:
    the post it forwards, other stored forwards of that post, and stored
    forwards of the new message itself. Never the new message's own row."""
    clauses = ["(fwd_from_channel_id = ? AND fwd_from_post_id = ?)"]
    params: list[int] = [channel_id, message_id]
    if fwd_from_channel_id is not None and fwd_from_post_id is not None:
        clauses.append("(channel_id = ? AND message_id = ?)")
        clauses.append("(fwd_from_channel_id = ? AND fwd_from_post_id = ?)")
        params += [fwd_from_channel_id, fwd_from_post_id] * 2
    return conn.execute(
        f"SELECT * FROM messages WHERE ({' OR '.join(clauses)}) "
        "AND NOT (channel_id = ? AND message_id = ?) ORDER BY received_at_utc, channel_id, message_id",
        (*params, channel_id, message_id),
    ).fetchall()


def _signal_of(conn: sqlite3.Connection, message: sqlite3.Row) -> sqlite3.Row | None:
    """The signal a stored message carries: its own, or the one it is a copy of."""
    own = signals_repo.get_signal(
        conn, channel_id=message["channel_id"], message_id=message["message_id"]
    )
    if own is not None:
        return own
    if message["relay_of_signal_id"] is None:
        return None
    return conn.execute(
        "SELECT * FROM signals WHERE signal_id = ?", (message["relay_of_signal_id"],)
    ).fetchone()


def _followup_of(conn: sqlite3.Connection, message: sqlite3.Row) -> sqlite3.Row | None:
    """The follow-up a stored message carries: its own (`edit_seq` 0), or
    the one it is a copy of."""
    followup_id = signals_repo.get_followup_id(
        conn, channel_id=message["channel_id"], message_id=message["message_id"]
    )
    if followup_id is None:
        followup_id = message["relay_of_followup_id"]
    if followup_id is None:
        return None
    return conn.execute(
        "SELECT followup_id, channel_id, message_id FROM followups WHERE followup_id = ?",
        (followup_id,),
    ).fetchone()


def _same_trade(
    conn: sqlite3.Connection, *, trade_hash: str, posted_at_utc: datetime, window: int
) -> list[sqlite3.Row]:
    """Signals sharing `trade_hash` (current or first accepted: an SL/TP
    edit moves the current one, and a copy of the pre-edit call is still
    the same trade) whose own message was posted within `window` seconds
    of `posted_at_utc`, either side. Oldest signal first."""
    rows = conn.execute(
        "SELECT s.*, m.posted_at_utc AS message_posted_at_utc FROM signals s "
        "JOIN messages m ON m.channel_id = s.channel_id AND m.message_id = s.message_id "
        "WHERE s.trade_hash = ? OR s.first_trade_hash = ? ORDER BY s.signal_id",
        (trade_hash, trade_hash),
    ).fetchall()
    limit = timedelta(seconds=window)
    return [
        row
        for row in rows
        if abs(datetime.fromisoformat(row["message_posted_at_utc"]) - posted_at_utc) <= limit
    ]


def _signal_match(conn: sqlite3.Connection, signal: sqlite3.Row, *, via: MatchVia) -> SignalMatch:
    return SignalMatch(
        signal_id=signal["signal_id"],
        channel_id=signal["channel_id"],
        message_id=signal["message_id"],
        via=via,
        withdrawn_unexecuted=is_withdrawn_unexecuted(conn, signal),
        state=signal["state"] if signal["withdrawn_at_utc"] is None
        else f"{signal['state']}, withdrawn",
    )
