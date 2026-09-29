"""Relay copies: the same call, or the same follow-up, reaching us twice
(spec G7; final review F02, F04, F08, F09; relay review round 3).

The user's channels copy each other: a free channel forwards the VIP's
posts, the user's own channel re-posts them, a poster deletes a call and
posts it again. `UNIQUE(channel_id, message_id)` cannot say "this is the
call we already have", so this module does, for `messages_repo.ingest()`
(a message stored for the first time) and `edits_repo` (a post of a call
edited or deleted, a withdrawn call's repost promoted). Everything here
runs inside the caller's writer job and never commits.

**Nothing here may depend on which post was stored first.** The code
cannot tell a VIP's channel from a relay's: whichever post of a call is
stored first holds its `signals` row, and that is an accident of
delivery. So the rules speak of *the call* and *its posts*, never of
"the original" and "the copy".

**The call and its posts.** A call is one signal, or a chain of them:
when a signal is withdrawn before anything was executed and the same
call is posted again (a repost promoted in its place, or posted again by
the channel that withdrew it), the new signal records the one it
replaces (`signals.promoted_from_signal_id`). The *newest* signal of the
chain carries the call: `live_signal_for` -- the one helper every
consumer resolves through, Plan 3 included. The call's *posts* are the
chain's own messages and every message stored as a copy of one of them
(`messages.relay_of_signal_id`); a post *stands* while it is not deleted
and still reads as a signal, and is not the post that closed one of the
chain's signals (a re-aimed post no longer shows the call).
`signal_for_post` says which signal a stored post acts on: the newest
of its call -- or, for the post that closed one of the chain's signals,
that signal (its later edits are about the call it broke).

**What is a copy of a signal** (`find_signal_relay`), in this order:

1. *The same post, however late* (F09). A forward carries the post it
   copies (`fwd_from_channel_id`, `fwd_from_post_id`). If that post is
   stored, or another stored forward carries the same post, or a stored
   forward carries *this* message, the new message is a copy of the
   call that post belongs to (`signal_for_post`), however long after the
   original it arrives. A forward keeps the original's words, so this
   is exact; its own date is when it was forwarded, which is why the
   window below cannot be trusted for it.
2. *The same trade, close together*: a stored signal whose trade hash --
   current, or the one it was first accepted with -- equals this one's,
   posted within `relay_window_seconds` (both ends included) either
   side, taken to the newest signal of its call. This is how a call
   re-posted by hand is recognised.

**A withdrawn call** (F04, ruling R3-P3). A call whose newest signal is
VOIDED, INVALID or withdrawn and of which nothing was executed
(`signals_repo.trade_status` is "pending") is no longer a call anyone
acts on:

- a same-trade post *on the channel whose last post withdrew it*
  (`signals.closed_by_channel_id`; the signal's own channel for a row
  closed before migration 4) is not a copy of it: that channel withdrew
  the call and posted it again, and the new post is a new signal
  *replacing* the withdrawn one (`SignalRelay.replaces`;
  `record_replacement`);
- a copy of it from any other channel, or a forward of it, is held back
  -- one source withdrew the call -- with a CRITICAL alert naming both
  messages and the post that withdrew it (`COPY_OF_WITHDRAWN_SIGNAL`),
  because the call may be meant after all. When the withdrawing channel
  later posts the call again, those held-back copies repeat the new
  signal, their CRITICAL alerts are acknowledged and a WARNING says so
  (`COPY_OF_WITHDRAWN_SIGNAL_SUPERSEDED`): no "trade it by hand" is left
  standing while the call is queued;
- a live match is always preferred to a withdrawn one -- also for a
  forward: a forward of a withdrawn post repeats the live signal of its
  call, or the live signal the poster posted again in its place (the
  same trade, posted within the window of the withdrawn post or of the
  forward), if there is one.

A signal that *was* executed and then withdrawn still matches: trading
its repost would open the same trade twice.

**What is a copy of a follow-up** (`find_followup_relay`, F08): the same
post (forwards, as above), or -- for a follow-up whose target signal is
known because it replies to a stored post of a call -- a follow-up *on
another channel* with the same instruction (`op`) and the same
normalised text targeting the same signal within the window. A relay is
another channel: a poster saying "close half" at TP1 and again at TP2 on
its own channel gives two instructions, and so does a poster who
withdrew one and said it again. A follow-up that replies to nothing is
never matched by its text: two channels each saying "close half" may be
two genuine instructions.

A follow-up replying to a stored post targets the newest signal of that
post's call (`resolve_reply_target`): path `'reply'` when it replies to
that signal's own message, `'relay-reply'` for any other post of the
call. When the follow-up held (the first one stored) is a forward, which
carries no reply, and its copy -- the original post, stored second --
replies to a stored post, the held follow-up is given that target
(`resolution_path` `'copy-reply'`): which of the two Telegram delivered
first must not decide whether the instruction knows its signal. A target
the held follow-up already has is never replaced by a copy's; when the
call is replaced, every follow-up targeting its earlier signals is moved
to the new one (`record_replacement`).

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
#: WARNING: a copy held back by `COPY_OF_WITHDRAWN_SIGNAL` now repeats the
#: new signal the withdrawing channel posted: that CRITICAL alert is
#: acknowledged, and this one says the call is queued (ruling R3-P3).
COPY_OF_WITHDRAWN_SIGNAL_SUPERSEDED = "COPY_OF_WITHDRAWN_SIGNAL_SUPERSEDED"

#: `followups.resolution_path` of a follow-up replying to a signal's own message.
REPLY_PATH = "reply"
#: ... and of one replying to any other stored post of the signal's call.
RELAY_REPLY_PATH = "relay-reply"
#: ... and of a follow-up with no reply of its own (a forward) whose copy
#: -- the post it forwards -- replies to a stored post of a call.
COPY_REPLY_PATH = "copy-reply"

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
    #: The post (channel id, message id) that withdrew or invalidated it,
    #: when known (`signals.closed_by_*`).
    closed_by: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class SignalRelay:
    """What a new signal message is, relay-wise (`relay_of_signal`)."""

    #: The signal it repeats: store it as a copy (`record_signal_copy`).
    match: SignalMatch | None
    #: Else, when not None: it is a new signal replacing this withdrawn
    #: one -- the channel that withdrew the call posted it again. Insert it
    #: with `promoted_from_signal_id` set to this, then `record_replacement`.
    replaces: int | None = None


@dataclass(frozen=True, slots=True)
class FollowupMatch:
    """The follow-up a new follow-up message repeats."""

    followup_id: int
    #: The follow-up's own message.
    channel_id: int
    message_id: int
    via: MatchVia


# -- the call: its signals and its posts ---------------------------------------


def live_signal_for(conn: sqlite3.Connection, signal_id: int) -> int:
    """**The signal that carries `signal_id`'s call now** (ruling R3-P2):
    `signal_id` itself, or the newest signal that replaced it -- a repost
    promoted when it was withdrawn, or the call posted again by the
    channel that withdrew it (`signals.promoted_from_signal_id`, followed
    to its end). Only that newest signal can be live: each one it
    replaced was withdrawn before anything of it was executed.

    Every consumer holding a signal id taken from a post or a follow-up
    resolves it through here: the edit and deletion policy, the reply
    targets of follow-ups -- and Plan 3, which must resolve every
    follow-up's `target_signal_id` through this before acting on it
    (PLAN3-OBLIGATIONS #45)."""
    replacements = _replacements(conn, signal_id)
    return replacements[-1] if replacements else signal_id


def chain_ids(conn: sqlite3.Connection, signal_id: int) -> list[int]:
    """Every signal of `signal_id`'s call, oldest first: the ones it
    replaced, itself, and the ones that replaced it."""
    earlier: list[int] = []
    current = signal_id
    while True:
        row = conn.execute(
            "SELECT promoted_from_signal_id FROM signals WHERE signal_id = ?", (current,)
        ).fetchone()
        if row is None or row[0] is None or row[0] >= current:
            break
        current = row[0]
        earlier.append(current)
    return [*reversed(earlier), signal_id, *_replacements(conn, signal_id)]


def signal_for_post(conn: sqlite3.Connection, *, channel_id: int, message_id: int) -> int | None:
    """The signal a stored post acts on -- its edits, its deletion, the
    follow-ups replying to it: the newest signal of its call
    (`live_signal_for`), whichever signal the post was stored against
    (its own `signals` row, or the one it was stored as a copy of). A post
    that closed one of the call's signals -- deleted it, broke it,
    re-aimed it -- stays with that signal: what it says now is about the
    call it closed, not about the one posted in its place. None when the
    post belongs to no call."""
    start = signals_repo.get_signal_id(conn, channel_id=channel_id, message_id=message_id)
    if start is None:
        row = conn.execute(
            "SELECT relay_of_signal_id FROM messages WHERE channel_id = ? AND message_id = ?",
            (channel_id, message_id),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        start = row[0]
    for signal_id in (start, *_replacements(conn, start)):
        closed = conn.execute(
            "SELECT closed_by_channel_id, closed_by_message_id FROM signals WHERE signal_id = ?",
            (signal_id,),
        ).fetchone()
        if closed is not None and (closed[0], closed[1]) == (channel_id, message_id):
            return signal_id
    return live_signal_for(conn, start)


def is_relayed(conn: sqlite3.Connection, *, signal_id: int) -> bool:
    """Whether `signal_id`'s call has more than one post: a stored copy of
    one of its signals, or a signal it replaced or that replaced it."""
    chain = chain_ids(conn, signal_id)
    if len(chain) > 1:
        return True
    return has_copies(conn, signal_id=signal_id)


def has_copies(conn: sqlite3.Connection, *, signal_id: int) -> bool:
    """Whether any stored message is recorded as a copy of `signal_id`."""
    row = conn.execute(
        "SELECT 1 FROM messages WHERE relay_of_signal_id = ? LIMIT 1", (signal_id,)
    ).fetchone()
    return row is not None


def standing_posts_on(
    conn: sqlite3.Connection, *, signal_id: int, channel_id: int
) -> list[sqlite3.Row]:
    """The posts of `signal_id`'s call on `channel_id` that still stand
    (ruling R3-P1): the chain's own messages and the copies of any of its
    signals there, not deleted, still read as a signal, and not the post
    that closed one of the chain's signals. Lowest message id (the
    earliest post) first."""
    chain = chain_ids(conn, signal_id)
    marks = ", ".join("?" for _ in chain)
    return conn.execute(
        "SELECT m.* FROM messages m "
        "WHERE m.channel_id = ? AND m.deleted_at_utc IS NULL AND m.kind = 'signal' "
        f"  AND (m.relay_of_signal_id IN ({marks}) OR EXISTS ("
        "    SELECT 1 FROM signals s WHERE s.channel_id = m.channel_id "
        f"     AND s.message_id = m.message_id AND s.signal_id IN ({marks}))) "
        "  AND NOT EXISTS (SELECT 1 FROM signals s "
        f"    WHERE s.signal_id IN ({marks}) AND s.closed_by_channel_id = m.channel_id "
        "     AND s.closed_by_message_id = m.message_id) "
        "ORDER BY m.message_id",
        (channel_id, *chain, *chain, *chain),
    ).fetchall()


def record_replacement(
    conn: sqlite3.Connection, *, new_signal_id: int, replaced_signal_id: int, now_utc: datetime
) -> tuple[int, ...]:
    """Signal `new_signal_id` -- already stored with
    `promoted_from_signal_id = replaced_signal_id`, and fanned out -- now
    carries the call `replaced_signal_id` was withdrawn from (ruling
    R3-P2). Every follow-up that targeted one of the call's earlier
    signals targets it now (path `'reply'` when it replies to its own
    message, `'relay-reply'` for any other post). Every copy held back
    against the withdrawn call (`COPY_OF_WITHDRAWN_SIGNAL`) repeats it
    now, and that CRITICAL alert -- which told the user to trade the call
    by hand -- is acknowledged and superseded by a WARNING
    (`COPY_OF_WITHDRAWN_SIGNAL_SUPERSEDED`) naming both posts: the call is
    queued, and trading it by hand as well would open it twice (R3-P3).
    Returns the ids of the alerts raised."""
    new = conn.execute("SELECT * FROM signals WHERE signal_id = ?", (new_signal_id,)).fetchone()
    if new["promoted_from_signal_id"] != replaced_signal_id:
        raise ValueError(
            f"signal {new_signal_id} does not record that it replaces signal {replaced_signal_id}"
        )
    earlier = [sid for sid in chain_ids(conn, new_signal_id) if sid != new_signal_id]
    logger.info(
        "signal %s (channel %s message %s) carries the call of withdrawn signal %s now "
        "(the call's earlier signals: %s)",
        new_signal_id,
        new["channel_id"],
        new["message_id"],
        replaced_signal_id,
        earlier,
    )
    marks = ", ".join("?" for _ in earlier)
    conn.execute(
        "UPDATE followups SET target_signal_id = ?, resolution_path = CASE "
        "  WHEN resolution_path NOT IN (?, ?) THEN resolution_path "
        "  WHEN channel_id = ? AND reply_to_message_id = ? THEN ? ELSE ? END "
        f"WHERE edit_seq = 0 AND target_signal_id IN ({marks})",
        (
            new_signal_id,
            REPLY_PATH,
            RELAY_REPLY_PATH,
            new["channel_id"],
            new["message_id"],
            REPLY_PATH,
            RELAY_REPLY_PATH,
            *earlier,
        ),
    )
    held_back = conn.execute(
        "SELECT alert_id, channel_id, message_id, signal_id, acked FROM alerts "
        f"WHERE code = ? AND signal_id IN ({marks}) ORDER BY alert_id",
        (COPY_OF_WITHDRAWN_SIGNAL, *earlier),
    ).fetchall()
    if not held_back:
        return ()
    (queued,) = conn.execute(
        "SELECT COUNT(*) FROM outbox WHERE kind = 'ENTRY' AND signal_id = ? AND state = 'QUEUED'",
        (new_signal_id,),
    ).fetchone()
    raised: list[int] = []
    for alert in held_back:
        conn.execute(
            "UPDATE messages SET relay_of_signal_id = ? WHERE channel_id = ? AND message_id = ? "
            f"  AND relay_of_signal_id IN ({marks})",
            (new_signal_id, alert["channel_id"], alert["message_id"], *earlier),
        )
        if not alert["acked"]:
            alerts.ack(conn, alert_id=alert["alert_id"])
        logger.info(
            "channel %s message %s, held back as a copy of withdrawn signal %s, now repeats "
            "signal %s (channel %s message %s)",
            alert["channel_id"],
            alert["message_id"],
            alert["signal_id"],
            new_signal_id,
            new["channel_id"],
            new["message_id"],
        )
        raised.append(
            alerts.record(
                conn,
                now_utc=now_utc,
                level="WARNING",
                code=COPY_OF_WITHDRAWN_SIGNAL_SUPERSEDED,
                channel_id=alert["channel_id"],
                message_id=alert["message_id"],
                signal_id=new_signal_id,
                detail=(
                    f"alert #{alert['alert_id']} no longer applies. It said that message "
                    f"{alert['message_id']} on channel {alert['channel_id']} copies a call that "
                    "had been withdrawn, was not traded, and that the call should be traded by "
                    "hand if it was still meant. The channel that withdrew the call has now "
                    f"posted it again, as message {new['message_id']} on channel "
                    f"{new['channel_id']}: it is signal {new_signal_id}, with {queued} trade "
                    f"entr{'y' if queued == 1 else 'ies'} queued, and the copy now repeats it. "
                    f"Alert #{alert['alert_id']} is cleared. Do not trade this call by hand as "
                    "well: that would open the trade twice. If you already did, check your "
                    "open positions."
                ),
            )
        )
    return tuple(raised)


# -- signals -------------------------------------------------------------------


def is_withdrawn_unexecuted(conn: sqlite3.Connection, signal: sqlite3.Row) -> bool:
    """`signal` (a `signals` row) is out of play -- VOIDED, INVALID or
    withdrawn -- and nothing of it was executed or is being executed."""
    closed = signal["state"] in _CLOSED_STATES or signal["withdrawn_at_utc"] is not None
    return closed and signals_repo.trade_status(conn, signal_id=signal["signal_id"]) == "pending"


def relay_of_signal(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    message_id: int,
    trade_hash: str,
    posted_at_utc: datetime,
    relay_window_seconds: int,
    fwd_from_channel_id: int | None,
    fwd_from_post_id: int | None,
) -> SignalRelay:
    """What the new signal message (`channel_id`, `message_id`) is: a copy
    of a call we have (`match`), a new signal replacing a withdrawn call
    (`replaces`), or neither -- see the module docstring for the rules and
    their order. The new message's own row may already be stored; it is
    never matched against itself, nor against a signal it holds."""
    own = (channel_id, message_id)
    for stored in _same_post(
        conn,
        channel_id=channel_id,
        message_id=message_id,
        fwd_from_channel_id=fwd_from_channel_id,
        fwd_from_post_id=fwd_from_post_id,
    ):
        signal = _signal_of(conn, stored)
        if signal is None or (signal["channel_id"], signal["message_id"]) == own:
            continue
        if is_withdrawn_unexecuted(conn, signal):
            # The post is withdrawn; the call may be live again as a repost.
            live = _live_same_trade(
                conn,
                trade_hash=trade_hash,
                around=(posted_at_utc, _posted_at(conn, signal)),
                window=relay_window_seconds,
                exclude=own,
            )
            if live is not None:
                return SignalRelay(_signal_match(conn, live, via="hash"))
            # Held back against the newest signal of the call (the post may
            # have closed an earlier one of the chain): R3-P2.
            signal = _signal_row(conn, live_signal_for(conn, signal["signal_id"]))
        return SignalRelay(_signal_match(conn, signal, via="forward"))

    withdrawn_elsewhere: sqlite3.Row | None = None
    replaces: int | None = None
    for candidate in _same_trade(
        conn, trade_hash=trade_hash, posted_at_utc=posted_at_utc, window=relay_window_seconds
    ):
        if (candidate["channel_id"], candidate["message_id"]) == own:
            continue
        signal = _signal_row(conn, live_signal_for(conn, candidate["signal_id"]))
        if (signal["channel_id"], signal["message_id"]) == own:
            continue
        if not is_withdrawn_unexecuted(conn, signal):
            return SignalRelay(_signal_match(conn, signal, via="hash"))  # a live call wins
        if channel_id == _closing_channel(signal):
            # This channel's last post withdrew the call: posted again here,
            # it is a new signal in its place.
            if replaces is None:
                replaces = signal["signal_id"]
        elif withdrawn_elsewhere is None:
            withdrawn_elsewhere = signal
    if withdrawn_elsewhere is not None:
        return SignalRelay(_signal_match(conn, withdrawn_elsewhere, via="hash"))
    return SignalRelay(None, replaces)


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
    repeats, or None: `relay_of_signal(...).match`. A caller about to
    store a new signal uses `relay_of_signal`, which also says whether it
    replaces a withdrawn call."""
    return relay_of_signal(
        conn,
        channel_id=channel_id,
        message_id=message_id,
        trade_hash=trade_hash,
        posted_at_utc=posted_at_utc,
        relay_window_seconds=relay_window_seconds,
        fwd_from_channel_id=fwd_from_channel_id,
        fwd_from_post_id=fwd_from_post_id,
    ).match


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
    by = (
        ""
        if match.closed_by is None
        else f" by message {match.closed_by[1]} on channel {match.closed_by[0]}"
    )
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
            f"withdrawn{by} before anything was executed (it is {match.state}). This copy "
            "was not traded either: one source withdrew the call. If the call is still "
            "meant, trade it by hand."
        ),
    )
    return (alert_id,)


# -- follow-ups ----------------------------------------------------------------


def resolve_reply_target(
    conn: sqlite3.Connection, *, channel_id: int, reply_to_message_id: int | None
) -> tuple[int, str] | None:
    """The signal a follow-up replying to (`channel_id`,
    `reply_to_message_id`) is about -- the one that post acts on
    (`signal_for_post`: the newest signal of its call) -- and how it was
    found: `"reply"` when the replied-to message is that signal's own,
    `"relay-reply"` for any other post of the call. None when it replies
    to nothing stored as a post of a call."""
    if reply_to_message_id is None:
        return None
    signal_id = signal_for_post(conn, channel_id=channel_id, message_id=reply_to_message_id)
    if signal_id is None:
        return None
    signal = _signal_row(conn, signal_id)
    own = (signal["channel_id"], signal["message_id"]) == (channel_id, reply_to_message_id)
    return signal_id, REPLY_PATH if own else RELAY_REPLY_PATH


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
        "SELECT f.followup_id, f.channel_id, f.message_id, m.posted_at_utc "
        "FROM followups f "
        "JOIN messages m ON m.channel_id = f.channel_id AND m.message_id = f.message_id "
        "WHERE f.edit_seq = 0 AND f.target_signal_id IS ? AND f.op = ? AND m.content_hash = ? "
        "ORDER BY f.followup_id",
        (target_signal_id, op, content_hash),
    ).fetchall()
    window = timedelta(seconds=relay_window_seconds)
    for row in rows:
        if row["channel_id"] == channel_id:
            continue  # said again on its own channel: another instruction, not a relay
        if abs(datetime.fromisoformat(row["posted_at_utc"]) - posted_at_utc) <= window:
            return FollowupMatch(row["followup_id"], row["channel_id"], row["message_id"], "hash")
    return None


def record_followup_copy(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    message_id: int,
    match: FollowupMatch,
    target_signal_id: int | None,
    now_utc: datetime,
) -> None:
    """Store that (`channel_id`, `message_id`) repeats `match`'s follow-up:
    the link, the INFO line, the counter. `target_signal_id` is the signal
    the copy itself replies to (`resolve_reply_target`), if any: the
    follow-up held is given it when it has none (see the module
    docstring)."""
    conn.execute(
        "UPDATE messages SET relay_of_followup_id = ? WHERE channel_id = ? AND message_id = ?",
        (match.followup_id, channel_id, message_id),
    )
    if target_signal_id is not None:
        filled = conn.execute(
            "UPDATE followups SET target_signal_id = ?, resolution_path = ? "
            "WHERE followup_id = ? AND target_signal_id IS NULL",
            (target_signal_id, COPY_REPLY_PATH, match.followup_id),
        ).rowcount
        if filled:
            logger.info(
                "follow-up %s (channel %s message %s) targets signal %s: its copy, channel %s "
                "message %s, replies to that signal",
                match.followup_id,
                match.channel_id,
                match.message_id,
                target_signal_id,
                channel_id,
                message_id,
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


def _replacements(conn: sqlite3.Connection, signal_id: int) -> list[int]:
    """The signals that replaced `signal_id`, one after the other, oldest
    first (each records the one before it in `promoted_from_signal_id`; a
    replacement is always stored after the signal it replaces)."""
    chain: list[int] = []
    current = signal_id
    while True:
        row = conn.execute(
            "SELECT signal_id FROM signals WHERE promoted_from_signal_id = ? AND signal_id > ? "
            "ORDER BY signal_id DESC LIMIT 1",
            (current, current),
        ).fetchone()
        if row is None:
            return chain
        current = row[0]
        chain.append(current)


def _closing_channel(signal: sqlite3.Row) -> int:
    """The channel whose post withdrew or invalidated `signal`: recorded
    since migration 4; before it, only a signal's own message closed it."""
    closed = signal["closed_by_channel_id"]
    return signal["channel_id"] if closed is None else closed


def _signal_row(conn: sqlite3.Connection, signal_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM signals WHERE signal_id = ?", (signal_id,)).fetchone()


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
    """The signal a stored message acts on (`signal_for_post`), or None."""
    signal_id = signal_for_post(
        conn, channel_id=message["channel_id"], message_id=message["message_id"]
    )
    return None if signal_id is None else _signal_row(conn, signal_id)


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


def _posted_at(conn: sqlite3.Connection, signal: sqlite3.Row) -> datetime:
    """When `signal`'s own message was posted."""
    (posted,) = conn.execute(
        "SELECT posted_at_utc FROM messages WHERE channel_id = ? AND message_id = ?",
        (signal["channel_id"], signal["message_id"]),
    ).fetchone()
    return datetime.fromisoformat(posted)


def _live_same_trade(
    conn: sqlite3.Connection,
    *,
    trade_hash: str,
    around: tuple[datetime, ...],
    window: int,
    exclude: tuple[int, int],
) -> sqlite3.Row | None:
    """The newest signal of the oldest call on `trade_hash` posted within
    `window` seconds of any of the `around` times that is not
    withdrawn-unexecuted, or None. Never a signal of `exclude`'s own."""
    for moment in around:
        for candidate in _same_trade(
            conn, trade_hash=trade_hash, posted_at_utc=moment, window=window
        ):
            if (candidate["channel_id"], candidate["message_id"]) == exclude:
                continue
            signal = _signal_row(conn, live_signal_for(conn, candidate["signal_id"]))
            if (signal["channel_id"], signal["message_id"]) == exclude:
                continue
            if not is_withdrawn_unexecuted(conn, signal):
                return signal
    return None


def _signal_match(conn: sqlite3.Connection, signal: sqlite3.Row, *, via: MatchVia) -> SignalMatch:
    return SignalMatch(
        signal_id=signal["signal_id"],
        channel_id=signal["channel_id"],
        message_id=signal["message_id"],
        via=via,
        withdrawn_unexecuted=is_withdrawn_unexecuted(conn, signal),
        state=signal["state"] if signal["withdrawn_at_utc"] is None
        else f"{signal['state']}, withdrawn",
        closed_by=None
        if signal["closed_by_channel_id"] is None
        else (signal["closed_by_channel_id"], signal["closed_by_message_id"]),
    )
