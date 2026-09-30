"""Relay copies: the same call, or the same follow-up, reaching us twice
(spec G7; final review F02, F04, F08, F09; relay review rounds 3-4).

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
a channel that withdrew it), the new signal records the one it
replaces (`signals.promoted_from_signal_id`). The *newest* signal of the
chain carries the call: `live_signal_for` -- the one helper every
consumer resolves through, Plan 3 included. The call's *posts* are the
chain's own messages and every message stored as a copy of one of them
(`messages.relay_of_signal_id`); a post *stands* while it is not deleted,
still shows the call -- a signal on the same side, symbol and entry zone
(`standing_posts_on` keeps the posts still stored as signals;
`edits_repo` re-reads them with their channel's parser, as a re-aimed
post still reads as a signal) -- and is not the post that closed one of
the chain's signals.
`signal_for_post` says which signal a stored post acts on: the newest
of its call -- or, for the post that closed one of the chain's signals,
that signal (its later edits are about the call it broke;
`kept_closer`). A *promotion* is no such closing (ruling R5-3): a
signal's own message deleted, edited away or broken while its channel
still showed the call in another post, promoted in its place
(`signals.replacement_kind` 'promoted', `closed_by_promotion`) -- the
call never stopped, so that post acts on the newest signal like any
other; only one re-aimed to another call stays with the signal. Such a post
that shows the call again -- read back after a false window deletion, or
edited back -- while a newer signal carries it is that signal's closer no
more: it stands again (ruling R4-5, `edits_repo._stands_again`) -- and so
does one read or edited back first, once its channel posts the call again
(`record_replacement`): which of the two came first does not matter.

**An older post of a call** (ruling R4-1, `old_post`). When a call was
withdrawn and posted again, the other channel's posts of the withdrawn
signal are still posts of the call, and their edits reach its newest
signal. Their *removal* -- deleted, edited away, re-aimed or broken --
is told apart:

- a forward of *another channel's* post of the call that no longer
  stands (deleted, edited away, re-aimed, or the post that withdrew one of
  its signals) is an *echo*: its removal only repeats that post's, which
  was handled when it happened, and is recorded (INFO) without reaching
  any signal -- also when nothing was posted again, so that which post
  was stored first never matters. A forward of the channel's *own* post
  (a "bump") is that channel's own post, never an echo (ruling R5-1). A
  forwarded post removed as a repeat -- it withdrew nothing, as its
  channel still showed the call -- is echoed only while that channel
  still shows the call; once its last post is gone too, the forward's
  removal is a removal like any other (R5-2). With two channels the
  last-standing-post rule below already decides what is traded; these
  clauses decide it when a third channel forwards another's posts: the
  call posted again still stands on its channel while the forward goes
  (ruling R6-2);
- the post a withdrawing forward copied is the *original*: its removal is
  the original poster retracting, and reaches the newest signal as any
  removal does;
- any other post stored with an earlier signal that another post
  withdrew (a hand copy, nothing linking the two) is ambiguous: a
  retraction, or only the other channel removing its old copy. Its
  removal still reaches the newest signal -- doing nothing is the safe
  side -- with a CRITICAL alert saying so (`edits_repo`).

A promotion re-links every copy of the call, on any channel, to the
promoted signal: the call never stopped, so none of them is an older post.

**The call's last standing post is never an echo, nor an older post**
(ruling R5-6, INV-1). Whatever a removal echoes, when no other post of
the call still stands on any channel (`_call_stands_elsewhere`), it is a
removal like any other and withdraws the call: a call no post shows is
never left to trade. Echoes can otherwise chain -- each channel's last
post judged an echo of the other's -- and no rule about one pair of
posts can rule that out.

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

**A withdrawn call** (F04, rulings R3-P3, R4-2, R4-3, R4-6). A call whose
newest signal is VOIDED, INVALID or withdrawn and of which nothing was
executed (`signals_repo.trade_status` is "pending") is no longer a call
anyone acts on:

- a same-trade post, or a forward of the same post, *on a channel that
  withdrew the call* -- every channel whose last post of it was
  withdrawn, not only the one that withdrew it first (`withdrew_call`;
  for a row closed before migration 4, the signal's own channel) -- is
  not a copy of it: that channel withdrew the call and posted it again,
  and the new post is a new signal *replacing* the withdrawn one
  (`SignalRelay.replaces`; `record_replacement`). A copy held back (below)
  is no post of the call its channel had while it is held back: a channel
  that only copied a call after it was withdrawn never withdrew it
  (ruling R5-4);
- a copy of it from any other channel, or a forward of it, is held back
  -- one source withdrew the call -- with a CRITICAL alert naming both
  messages and the post that withdrew it (`COPY_OF_WITHDRAWN_SIGNAL`),
  because the call may be meant after all. When a withdrawing channel
  later posts the call again, those held-back copies repeat the new
  signal, their CRITICAL alerts are acknowledged and a WARNING says so
  (`COPY_OF_WITHDRAWN_SIGNAL_SUPERSEDED`): no "trade it by hand" is left
  standing while the call is queued. Nor is one ever raised while it is:
  every alert that would tell the user to trade a call by hand first
  looks for another signal carrying it -- through `live_signal_for`, else
  a live signal showing the same call posted within the relay window of
  the alert's post (signals stay NEW for ever: a call with the same zone
  posted days before is another call; ruling R6-1) -- and then names that
  signal and says NOT to trade the call by hand as well (`carrier_of`,
  `by_hand`; ruling R5-5);
- a live match is always preferred to a withdrawn one -- also for a
  forward: a forward of a withdrawn post repeats the live signal of its
  call, or the live signal the poster posted again in its place (the
  same trade, posted within the window of the withdrawn post or of the
  forward), if there is one -- but only when the forward's own text
  shows that call: a forward of a post re-aimed away (a SELL where the
  live call is a BUY) is held back against the signal its post belongs
  to, loudly, never linked silently to the live call.

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
to the new one (`record_replacement`) -- except one replying to the post
that withdrew the call (its channel's last post), which stays with the
signal that post closed (rulings R4-4, R5-3). A promotion moves every
reply, even one to a post re-aimed to another call: it was given while
the post showed the call, which never stopped -- as when the re-aimed
post held no signal and the call simply stood (INV-2). A reply to any post of a
call withdrawn before anything was executed, when the post no longer
shows the call -- the post that withdrew it above all -- gets no target
at all (`edits_repo.post_left_withdrawn_call`). `signal_for_followup` is
the one place that says which signal a follow-up acts on now.

**Every copy is visible.** `record_signal_copy` / `record_followup_copy`
store the link on the copy's `messages` row, log it at INFO with both
messages, and bump the durable counters `relay_duplicates` /
`relay_duplicate_followups`.

**Known limitations** (accepted, rulings G3 rounds 4 and 5; each needs
two watched channels carrying the same calls, unless it says otherwise):

- A copy held back and then re-linked to a call posted again is re-linked
  again, with a second `COPY_OF_WITHDRAWN_SIGNAL_SUPERSEDED` warning, if
  the call is withdrawn and posted again a second time.
- When two different withdrawn calls with the same trade hash are in the
  window -- one withdrawn by this channel, one by another -- a new post
  is held back against the other channel's rather than treated as this
  channel's call posted again.
- A forward held back because it shows another call than the live one
  (a forward of a re-aimed post) is re-linked like any held-back copy if
  that call is withdrawn and posted again.
- A forward of a post re-aimed away while the call stood on that
  channel's other post (so nothing was closed) is linked to the live
  call without an alert; it is never traded, and its edits and removal
  change nothing, as it does not show the call.
- A post read back after a false window deletion that had withdrawn the
  call stands again when the call is posted again on its own channel
  (`record_replacement` re-reads that channel's posts). Posted again by
  another channel -- whose ingest does not have the first channel's
  parser -- it stays the post that withdrew its signal: its later removal
  changes nothing, and a forward of it removed is an echo.
- Whether another channel's posts still show the call (`_gone_post`,
  `_call_stands_elsewhere`) is read with the changed post's own channel
  config, as the other channel's parser is not at hand. With channels on
  different parsers, a post that does not read as the call that way
  counts as not standing -- the safe side: the removal withdraws the call.
- One channel suffices: of several posts of the call deleted in one
  report, only the lowest id is recorded as the post that withdrew it
  (`edits_repo._closing_post`), so a reply to another of them follows the
  call if it is posted again -- as a reply to any repeat removed before
  the withdrawal does. The same in either storage order (INV-2).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from xaucopier.domain.signal import Signal
from xaucopier.parsing.router import route
from xaucopier.store import alerts, metrics, signals_repo

logger = logging.getLogger(__name__)


class _ChannelCfgLike(Protocol):
    channel_id: int
    parser: str

#: The relay window's length when the caller gives none (`ingest()`'s
#: default): same-trade posts this close together are copies of one call,
#: and only a signal posted this close to an alert's post is named as
#: carrying the same call (`carrier_of`).
RELAY_WINDOW_SECONDS = 900

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
    #: one -- a channel that withdrew the call posted it again. Insert it
    #: with `promoted_from_signal_id` set to this, then `record_replacement`.
    replaces: int | None = None


@dataclass(frozen=True, slots=True)
class OldPost:
    """A stored post whose removal would reach its call's newest signal,
    though the post echoes or predates that signal (ruling R4-1;
    `old_post`)."""

    #: "echo": a forward of a post that no longer stands -- its removal
    #: only echoes that post's. "original": the post that a forward which
    #: withdrew an earlier signal of the call copied -- the original poster
    #: retracting. "earlier": any other post stored against an earlier,
    #: replaced signal of the call, which another post withdrew -- it may
    #: be a retraction, or only the other channel removing its old copy.
    kind: Literal["echo", "original", "earlier"]
    #: "echo": the signal the forwarded post belongs to; else the earlier
    #: signal of the call that `post` withdrew.
    signal_id: int
    #: "echo": the forwarded post; else the post that withdrew `signal_id`
    #: (channel id, message id).
    post: tuple[int, int]


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
    (PLAN3-OBLIGATIONS #45), UNLESS the follow-up replies to that
    signal's closer and that closer stays with it (`kept_closer`: the
    post that withdrew the call, or one re-aimed to another call -- not
    a post a promotion replaced, ruling R5-3): such a reply is about the
    signal that post closed, never about the call posted in its place
    (ruling R4-4). `signal_for_followup` does exactly that."""
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
        if kept_closer(conn, signal_id) == (channel_id, message_id):
            return signal_id
    return live_signal_for(conn, start)


def closed_by_promotion(conn: sqlite3.Connection, signal_id: int) -> bool:
    """Whether signal `signal_id` lost its own message -- deleted, edited
    away, re-aimed or broken -- while its channel still showed the call in
    another post, which was promoted in its place in the same writer job
    (`signals.replacement_kind` 'promoted' on the signal replacing it). The
    call never stopped: that post's closing withdrew nothing (ruling
    R5-3)."""
    row = conn.execute(
        "SELECT 1 FROM signals WHERE promoted_from_signal_id = ? AND signal_id > ? "
        "  AND replacement_kind = 'promoted' LIMIT 1",
        (signal_id, signal_id),
    ).fetchone()
    return row is not None


def kept_closer(conn: sqlite3.Connection, signal_id: int) -> tuple[int, int] | None:
    """The post (channel id, message id) that closed signal `signal_id`,
    when that post stays with it -- what the post says now, and the
    follow-ups replying to it, are about `signal_id`, never about the call
    posted in its place (ruling R4-4, as limited by R5-3): the post that
    withdrew the call (its channel's last post of it), or a post that now
    shows another call (re-aimed, still a signal and not deleted). None
    when nothing closed the signal, or when a promotion replaced it and
    its closer was deleted, edited into something that is no signal, or
    broken: the call never stopped, so that post acts on the call's newest
    signal like any other post of it."""
    row = conn.execute(
        "SELECT closed_by_channel_id, closed_by_message_id FROM signals WHERE signal_id = ?",
        (signal_id,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    closer = (row[0], row[1])
    if not closed_by_promotion(conn, signal_id):
        return closer
    post = conn.execute(
        "SELECT kind, deleted_at_utc FROM messages WHERE channel_id = ? AND message_id = ?",
        closer,
    ).fetchone()
    if post is not None and post["kind"] == "signal" and post["deleted_at_utc"] is None:
        return closer  # re-aimed: it shows another call now
    return None


def signal_for_followup(conn: sqlite3.Connection, *, followup_id: int) -> int | None:
    """**The signal follow-up `followup_id` acts on now** -- what Plan 3
    must act on (PLAN3-OBLIGATIONS #45): its `target_signal_id` resolved
    through `live_signal_for`, unless it replies to the post that closed
    that signal and that post stays with it (`kept_closer`), when it is
    that signal itself. None when it has no target."""
    row = conn.execute(
        "SELECT channel_id, reply_to_message_id, target_signal_id FROM followups "
        "WHERE followup_id = ?",
        (followup_id,),
    ).fetchone()
    if row is None or row["target_signal_id"] is None:
        return None
    target = row["target_signal_id"]
    if kept_closer(conn, target) == (row["channel_id"], row["reply_to_message_id"]):
        return target
    return live_signal_for(conn, target)


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
    """The posts of `signal_id`'s call on `channel_id` that may still
    stand (ruling R3-P1): the chain's own messages and the copies of any of
    its signals there, not deleted, still stored as a signal, and not the
    post that closed one of the chain's signals. Lowest message id (the
    earliest post) first. A post re-aimed away from the call is still
    stored as a signal: the caller re-reads each one
    (`edits_repo._standing_others`)."""
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


def withdrew_call(
    conn: sqlite3.Connection,
    *,
    signal_id: int,
    channel_id: int,
    channel_cfg: _ChannelCfgLike | None = None,
) -> bool:
    """Whether channel `channel_id` withdrew `signal_id`'s call (ruling
    R4-2): it had a post of the call -- a message of one of the chain's
    signals, or a copy of one -- and none of them stands any more: each
    was deleted, edited into something that is no signal, or closed one of
    the chain's signals (`standing_posts_on`). With `channel_cfg`, the
    channel's own config, each post still stored as a signal is re-read
    with its parser too: one re-aimed away from the call does not stand
    either. Every such channel, not only the one whose post withdrew the
    call first, may post the call again as a new signal. A copy *held
    back* -- stored after the call was withdrawn, with its own
    `COPY_OF_WITHDRAWN_SIGNAL` alert naming the signal it is still linked
    to -- is no post of the call it had: a channel that only ever copied a
    withdrawn call never withdrew it, so deleting that copy and posting the
    call again is held back again (ruling R5-4). Once the call was posted
    again, that copy repeats the new signal (`record_replacement`): a post
    of the call like any other. (A signal closed before migration 4 recorded no
    closer: then its own channel withdrew it, as only a signal's own
    message could close it.)"""
    signal = _signal_row(conn, signal_id)
    if signal["closed_by_channel_id"] is None and channel_id == signal["channel_id"]:
        return True
    chain = chain_ids(conn, signal_id)
    marks = ", ".join("?" for _ in chain)
    had = conn.execute(
        "SELECT 1 FROM messages m WHERE m.channel_id = ? "
        f"  AND (m.relay_of_signal_id IN ({marks}) OR EXISTS ("
        "    SELECT 1 FROM signals s WHERE s.channel_id = m.channel_id "
        f"     AND s.message_id = m.message_id AND s.signal_id IN ({marks}))) "
        "  AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.code = ? "
        "    AND a.channel_id = m.channel_id AND a.message_id = m.message_id "
        "    AND a.signal_id = m.relay_of_signal_id) LIMIT 1",
        (channel_id, *chain, *chain, COPY_OF_WITHDRAWN_SIGNAL),
    ).fetchone()
    if had is None:
        return False
    for post in standing_posts_on(conn, signal_id=signal_id, channel_id=channel_id):
        if channel_cfg is None or post_shows_call(post, signal, channel_cfg=channel_cfg):
            return False
    return True


def old_post(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    message_id: int,
    channel_cfg: _ChannelCfgLike | None = None,
) -> OldPost | None:
    """How stored post (`channel_id`, `message_id`) stands to the newest
    signal of its call, for a removal of it -- deleted, edited away,
    re-aimed or broken (ruling R4-1). None when its removal is simply a
    removal of a post of that signal's call; else:

    - "echo": the post is a forward (`fwd_from_*`) of a stored post of a
      call that no longer stands -- deleted, edited into something that is
      no signal, or the post that withdrew one of its call's signals. The removal only echoes that post's, which was said when
      it happened: it is recorded, and reaches no signal. (Whichever post
      of the call was stored first, and whether or not the call was
      posted again since.) A forwarded post still stored as a signal is
      re-read with `channel_cfg`, the forward's own channel's config -- it
      read the forwarded post's words as this call -- so one re-aimed away
      no longer stands either.
    - "original": the post was stored against an earlier signal of the
      call, since replaced, and the post that withdrew that signal was a
      forward of *this* post: this is the original poster retracting, and
      the removal reaches the newest signal like any.
    - "earlier": stored against an earlier signal of the call, since
      replaced, that another post withdrew, with no forward linking the
      two: it may be a retraction or only the other channel removing its
      old copy. The removal still reaches the newest signal -- doing
      nothing is the safe side -- but loudly (`edits_repo`)."""
    message = conn.execute(
        "SELECT * FROM messages WHERE channel_id = ? AND message_id = ?", (channel_id, message_id)
    ).fetchone()
    if message is None:
        return None
    start = signals_repo.get_signal_id(conn, channel_id=channel_id, message_id=message_id)
    if start is None:
        start = message["relay_of_signal_id"]
    if start is None:
        return None
    found = _old_post_kind(conn, message, start, channel_cfg)
    if found is not None and not _call_stands_elsewhere(
        conn, signal_id=start, post=(channel_id, message_id), channel_cfg=channel_cfg
    ):
        # The call's last standing post, on every channel: its removal is a
        # removal like any other, whatever it echoes -- a call no post shows
        # is never left to trade (ruling R5-6, INV-1).
        return None
    return found


def _old_post_kind(
    conn: sqlite3.Connection,
    message: sqlite3.Row,
    start: int,
    channel_cfg: _ChannelCfgLike | None,
) -> OldPost | None:
    """`old_post`'s classification of stored post `message`, stored with
    signal `start`, before the last-standing-post check."""
    channel_id, message_id = message["channel_id"], message["message_id"]
    chain = chain_ids(conn, start)
    forwarded = (message["fwd_from_channel_id"], message["fwd_from_post_id"])
    if None not in forwarded and forwarded[0] != channel_id:
        # A forward of the channel's OWN post is that channel's own post of
        # the call (a "bump"), never an echo of another channel (R5-1).
        echoed = _gone_post(conn, forwarded, channel_cfg)
        if echoed is not None:
            return OldPost("echo", echoed, forwarded)
    newest = chain[-1]
    closed = []
    for signal_id in chain[chain.index(start) : chain.index(newest)]:
        row = _signal_row(conn, signal_id)
        closer = (row["closed_by_channel_id"], row["closed_by_message_id"])
        if None in closer or closer == (channel_id, message_id):
            continue
        closer_row = conn.execute(
            "SELECT fwd_from_channel_id, fwd_from_post_id FROM messages "
            "WHERE channel_id = ? AND message_id = ?",
            closer,
        ).fetchone()
        if closer_row is not None and tuple(closer_row) == (channel_id, message_id):
            return OldPost("original", signal_id, closer)
        closed.append((signal_id, closer))
    if closed:
        return OldPost("earlier", *closed[0])
    return None


def record_replacement(
    conn: sqlite3.Connection,
    *,
    new_signal_id: int,
    replaced_signal_id: int,
    now_utc: datetime,
    channel_cfg: _ChannelCfgLike | None = None,
) -> tuple[int, ...]:
    """Signal `new_signal_id` -- already stored with
    `promoted_from_signal_id = replaced_signal_id`, and fanned out -- now
    carries the call `replaced_signal_id` was withdrawn from (ruling
    R3-P2). With `channel_cfg`, the channel that posted the call again: a
    post of that channel which closed one of the call's earlier signals
    but shows the call again -- read back after a false window deletion,
    or edited back, while nothing carried the call in its place -- stands
    again now, a post of the call like any other (ruling R4-5, as
    `edits_repo._stands_again` does when the call was posted again first).
    Every follow-up that targeted one of the call's earlier
    signals targets it now (path `'reply'` when it replies to its own
    message, `'relay-reply'` for any other post) -- except one replying to
    the post that withdrew one of those signals (a closing no promotion
    followed), which stays with the signal that post closed (rulings
    R4-4, R5-3). Every copy held back
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
    if channel_cfg is not None:
        _release_closers_showing_call(conn, earlier=earlier, new=new, channel_cfg=channel_cfg)
    marks = ", ".join("?" for _ in earlier)
    kept = {
        closer
        for sid in earlier
        if not closed_by_promotion(conn, sid) and (closer := kept_closer(conn, sid)) is not None
    }
    for followup in conn.execute(
        "SELECT followup_id, channel_id, reply_to_message_id, resolution_path FROM followups "
        f"WHERE edit_seq = 0 AND target_signal_id IN ({marks}) ORDER BY followup_id",
        earlier,
    ).fetchall():
        if (followup["channel_id"], followup["reply_to_message_id"]) in kept:
            continue  # about the signal that post closed (R4-4, R5-3)
        path = followup["resolution_path"]
        if path in (REPLY_PATH, RELAY_REPLY_PATH):
            own = (followup["channel_id"], followup["reply_to_message_id"]) == (
                new["channel_id"],
                new["message_id"],
            )
            path = REPLY_PATH if own else RELAY_REPLY_PATH
        conn.execute(
            "UPDATE followups SET target_signal_id = ?, resolution_path = ? WHERE followup_id = ?",
            (new_signal_id, path, followup["followup_id"]),
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


def _release_closers_showing_call(
    conn: sqlite3.Connection,
    *,
    earlier: list[int],
    new: sqlite3.Row,
    channel_cfg: _ChannelCfgLike,
) -> None:
    """For `record_replacement`: every post of `channel_cfg`'s channel that
    closed one of the `earlier` signals of `new`'s call, is not deleted, and
    re-read with the channel's parser shows the call, is that signal's
    closer no more (`signals_repo.release_closed_by`): it stands again."""
    marks = ", ".join("?" for _ in earlier)
    closers = conn.execute(
        "SELECT s.signal_id AS closed_signal_id, m.* FROM signals s "
        "JOIN messages m ON m.channel_id = s.closed_by_channel_id "
        "  AND m.message_id = s.closed_by_message_id "
        f"WHERE s.signal_id IN ({marks}) AND m.channel_id = ? "
        "  AND m.deleted_at_utc IS NULL AND m.kind = 'signal'",
        (*earlier, channel_cfg.channel_id),
    ).fetchall()
    for post in closers:
        if not post_shows_call(post, new, channel_cfg=channel_cfg):
            continue
        signals_repo.release_closed_by(
            conn,
            signal_id=post["closed_signal_id"],
            channel_id=post["channel_id"],
            message_id=post["message_id"],
        )
        logger.info(
            "channel %s message %s, which withdrew signal %s, shows its call again: it stands "
            "again, a post of the call signal %s carries now",
            post["channel_id"],
            post["message_id"],
            post["closed_signal_id"],
            new["signal_id"],
        )


# -- signals -------------------------------------------------------------------


def carried_by(conn: sqlite3.Connection, signal_id: int) -> sqlite3.Row | None:
    """The signal that carries `signal_id`'s call now, when it is another
    one that is live or of which something was traded (`live_signal_for`):
    the `signals` row, or None."""
    newest = live_signal_for(conn, signal_id)
    if newest == signal_id:
        return None
    row = _signal_row(conn, newest)
    live = row["state"] == "NEW" and row["withdrawn_at_utc"] is None
    if live or signals_repo.trade_status(conn, signal_id=newest) != "pending":
        return row
    return None


def _call_key(call: Signal | sqlite3.Row) -> tuple[str, str, str, str]:
    """A call's side, symbol and entry zone (at the trade hash's price
    resolution, `same_price`), from a parse or a `signals` row."""
    if isinstance(call, Signal):
        side, symbol, lo, hi = call.side.value, call.symbol_alias, call.zone.lo, call.zone.hi
    else:
        parsed = json.loads(call["parsed_json"])
        side, symbol = parsed["side"], parsed["symbol_alias"]
        lo, hi = parsed["zone"]["lo"], parsed["zone"]["hi"]
    return side, symbol, f"{lo:.3f}", f"{hi:.3f}"


def live_same_call(
    conn: sqlite3.Connection,
    call: Signal | sqlite3.Row,
    *,
    around: Iterable[datetime],
    window: int,
    besides: Iterable[int] = (),
) -> sqlite3.Row | None:
    """The newest live signal (NEW, not withdrawn) showing `call` -- a
    parse or a `signals` row: the same side, symbol and entry zone -- whose
    own message was posted within `window` seconds (both ends included) of
    one of the moments `around`, other than the signals `besides`; or None.
    Signals stay NEW for ever: a call with the same zone posted days ago is
    another call, so it is never named (ruling R6-1)."""
    key, skip = _call_key(call), set(besides)
    moments, limit = tuple(around), timedelta(seconds=window)
    for row in conn.execute(
        "SELECT s.*, m.posted_at_utc AS message_posted_at_utc FROM signals s "
        "JOIN messages m ON m.channel_id = s.channel_id AND m.message_id = s.message_id "
        "WHERE s.state = 'NEW' AND s.withdrawn_at_utc IS NULL ORDER BY s.signal_id DESC"
    ).fetchall():
        if row["signal_id"] in skip or _call_key(row) != key:
            continue
        posted = datetime.fromisoformat(row["message_posted_at_utc"])
        if any(abs(posted - moment) <= limit for moment in moments):
            return row
    return None


def carrier_of(
    conn: sqlite3.Connection,
    *,
    around: Iterable[datetime],
    window: int = RELAY_WINDOW_SECONDS,
    signal_id: int | None = None,
    call: Signal | sqlite3.Row | None = None,
) -> sqlite3.Row | None:
    """For an alert that would tell the user to trade a call by hand
    (ruling R5-5): the signal that carries that call now, live, queued or
    traded, when it is not signal `signal_id` itself -- first through
    `live_signal_for` (`carried_by`: the call posted again or promoted),
    else the newest live signal showing the same call outside that chain,
    posted as a signal of its own within the relay window (`window`) of
    one of the moments `around` -- when the post the alert is about was
    posted, and when the post of the call it names was (`live_same_call`,
    ruling R6-1): the same horizon that decides duplicates. `call` is the
    call meant; by default signal `signal_id`'s. None when nothing else
    carries it."""
    if signal_id is not None:
        carrier = carried_by(conn, signal_id)
        if carrier is not None:
            return carrier
        if call is None:
            call = _signal_row(conn, signal_id)
    if call is None:
        return None
    besides = chain_ids(conn, signal_id) if signal_id is not None else ()
    return live_same_call(conn, call, around=around, window=window, besides=besides)


def by_hand(
    conn: sqlite3.Connection,
    *,
    advice: str,
    carrier: sqlite3.Row | None,
    described: str | None = None,
) -> str:
    """The closing advice of an alert that would tell the user to trade a
    call by hand (ruling R5-5): `advice` -- unless the same call is live,
    queued or traded as another signal, `carrier` (`carrier_of`), when
    trading it by hand as well would open it twice: the advice then names
    that signal and says NOT to trade the call by hand as well. Every
    "trade it by hand" goes through here. `described`: the call meant, as
    `describe_call` says it, when the alert also names another call."""
    if carrier is None:
        return advice
    status = signals_repo.trade_status(conn, signal_id=carrier["signal_id"])
    state = carrier["state"] + (", withdrawn" if carrier["withdrawn_at_utc"] else "")
    same = "The same call" if described is None else f"The same call, {described},"
    return (
        f"{same} is signal {carrier['signal_id']} (message {carrier['message_id']} on "
        f"channel {carrier['channel_id']}; it is {state}, trade status {status}): "
        "do NOT trade it by hand as well -- that would open the trade twice."
    )


def describe_call(signal: Signal) -> str:
    """A signal's call, for an alert: side and entry zone."""
    return f"{signal.side.value.upper()} {signal.zone.lo:g}-{signal.zone.hi:g}"


def _describe_both(signal: Signal, row: sqlite3.Row) -> tuple[str, str]:
    """A parse's call and a `signals` row's, for an alert that sets them
    side by side: side and entry zone (`describe_call`), and the symbol
    too when the two differ."""
    parsed = json.loads(row["parsed_json"])
    zone = parsed["zone"]
    stored = f"{parsed['side'].upper()} {zone['lo']:g}-{zone['hi']:g}"
    if parsed["symbol_alias"] == signal.symbol_alias:
        return describe_call(signal), stored
    return (
        f"{signal.symbol_alias} {describe_call(signal)}",
        f"{parsed['symbol_alias']} {stored}",
    )


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
    channel_cfg: _ChannelCfgLike | None = None,
    signal: Signal | None = None,
) -> SignalRelay:
    """What the new signal message (`channel_id`, `message_id`) is: a copy
    of a call we have (`match`), a new signal replacing a withdrawn call
    (`replaces`), or neither -- see the module docstring for the rules and
    their order. The new message's own row may already be stored; it is
    never matched against itself, nor against a signal it holds.
    `channel_cfg` (the new message's channel) re-reads that channel's own
    posts to decide whether it withdrew a call (`withdrew_call`); `signal`
    (the new message's parse) tells whether a forward still shows the
    call its post belongs to."""
    own = (channel_id, message_id)
    for stored in _same_post(
        conn,
        channel_id=channel_id,
        message_id=message_id,
        fwd_from_channel_id=fwd_from_channel_id,
        fwd_from_post_id=fwd_from_post_id,
    ):
        found = _signal_of(conn, stored)
        if found is None or (found["channel_id"], found["message_id"]) == own:
            continue
        if not is_withdrawn_unexecuted(conn, found):
            return SignalRelay(_signal_match(conn, found, via="forward"))
        # The post is withdrawn; the call may be live again as a repost.
        live = _live_same_trade(
            conn,
            trade_hash=trade_hash,
            around=(posted_at_utc, _posted_at(conn, found)),
            window=relay_window_seconds,
            exclude=own,
        )
        if live is not None:
            return SignalRelay(_signal_match(conn, live, via="hash"))
        # The newest signal of the call (the post may have closed an
        # earlier one of the chain): R3-P2.
        newest = _signal_row(conn, live_signal_for(conn, found["signal_id"]))
        if signal is not None and not shows_call(signal, newest):
            # A forward of a post that shows another call now (re-aimed):
            # held back against the signal that post belongs to, loudly --
            # never linked silently to the live call (ruling R4-6).
            return SignalRelay(_signal_match(conn, found, via="forward"))
        if is_withdrawn_unexecuted(conn, newest) and withdrew_call(
            conn, signal_id=newest["signal_id"], channel_id=channel_id, channel_cfg=channel_cfg
        ):
            # This channel withdrew the call and forwards the same post
            # again: a new signal in its place, as a hand repost (R4-3).
            return SignalRelay(None, replaces=newest["signal_id"])
        return SignalRelay(_signal_match(conn, newest, via="forward"))

    withdrawn_elsewhere: sqlite3.Row | None = None
    replaces: int | None = None
    for candidate in _same_trade(
        conn, trade_hash=trade_hash, posted_at_utc=posted_at_utc, window=relay_window_seconds
    ):
        if (candidate["channel_id"], candidate["message_id"]) == own:
            continue
        newest = _signal_row(conn, live_signal_for(conn, candidate["signal_id"]))
        if (newest["channel_id"], newest["message_id"]) == own:
            continue
        if not is_withdrawn_unexecuted(conn, newest):
            return SignalRelay(_signal_match(conn, newest, via="hash"))  # a live call wins
        if withdrew_call(
            conn, signal_id=newest["signal_id"], channel_id=channel_id, channel_cfg=channel_cfg
        ):
            # This channel's last post of the call was withdrawn: posted
            # again here, it is a new signal in its place (R3-P3, R4-2).
            if replaces is None:
                replaces = newest["signal_id"]
        elif withdrawn_elsewhere is None:
            withdrawn_elsewhere = newest
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
    channel_cfg: _ChannelCfgLike | None = None,
    signal: Signal | None = None,
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
        channel_cfg=channel_cfg,
        signal=signal,
    ).match


def record_signal_copy(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    message_id: int,
    match: SignalMatch,
    now_utc: datetime,
    signal: Signal | None = None,
    relay_window_seconds: int = RELAY_WINDOW_SECONDS,
) -> tuple[int, ...]:
    """Store that (`channel_id`, `message_id`) -- already on `messages` --
    repeats `match`'s signal: the link, the INFO line, the counter, and
    for a copy of a withdrawn call the CRITICAL alert. `signal` is the
    copy's own parse. When another signal carries the withdrawn call now
    (`carrier_of`: through the call's chain, or posted within
    `relay_window_seconds` of the copy or of the post it repeats), the
    alert says so (`by_hand`). When the copy reads as another call than
    the withdrawn signal's -- a forward of a re-aimed post -- the alert
    says what it reads as and names the call it means, carrier or not
    (ruling R6-4). Returns the ids of the alerts raised."""
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
    # The call meant is the withdrawn signal's: this copy may show another.
    carrier = carrier_of(
        conn,
        signal_id=match.signal_id,
        around=(
            posted_at(conn, channel_id=channel_id, message_id=message_id),
            posted_at(conn, channel_id=match.channel_id, message_id=match.message_id),
        ),
        window=relay_window_seconds,
    )
    withdrawn = _signal_row(conn, match.signal_id)
    reads, meant = "", None
    if signal is not None and not shows_call(signal, withdrawn):
        # The copy shows another call (a forward of a re-aimed post): say so,
        # and which call "the call" is -- signal N's (ruling R6-4).
        shown, meant = _describe_both(signal, withdrawn)
        reads = f" This copy itself reads as {shown}, not signal {match.signal_id}'s {meant}."
    advice = by_hand(
        conn,
        advice=(
            "If the call is still meant, trade it by hand."
            if meant is None
            else f"If signal {match.signal_id}'s {meant} is still meant, trade it by hand."
        ),
        carrier=carrier,
        described=meant,
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
            f"was not traded either: one source withdrew the call.{reads} {advice}"
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


def same_price(a: float, b: float) -> bool:
    """Price equality at the resolution the trade hash uses (3 dp), so a
    diff never disagrees with the hash about whether anything changed."""
    return f"{a:.3f}" == f"{b:.3f}"


def reaims(parsed: dict, signal: Signal) -> list[str]:
    """How `signal` re-aims the stored parse `parsed` (a `signals` row's
    `parsed_json`): its side, symbol and entry zone, each described. Empty
    when `signal` shows the same call (its stop and targets may differ)."""
    reaimed = []
    if parsed["side"] != signal.side.value:
        reaimed.append(f"side {parsed['side']} -> {signal.side.value}")
    if parsed["symbol_alias"] != signal.symbol_alias:
        reaimed.append(f"symbol {parsed['symbol_alias']} -> {signal.symbol_alias}")
    zone = parsed["zone"]
    if not (same_price(zone["lo"], signal.zone.lo) and same_price(zone["hi"], signal.zone.hi)):
        reaimed.append(f"entry {zone['lo']}-{zone['hi']} -> {signal.zone.lo}-{signal.zone.hi}")
    return reaimed


def shows_call(signal: Signal, row: sqlite3.Row) -> bool:
    """Whether a post parsed as `signal` shows the call of signals row
    `row`: the same side, symbol and entry zone."""
    return not reaims(json.loads(row["parsed_json"]), signal)


def post_shows_call(
    post: sqlite3.Row, row: sqlite3.Row, *, channel_cfg: _ChannelCfgLike
) -> bool:
    """Whether stored post `post` (a `messages` row of `channel_cfg`'s
    channel), re-read with the channel's parser, shows the call of signals
    row `row`."""
    routed = route(
        post["raw_text"],
        channel_cfg=channel_cfg,
        message_id=post["message_id"],
        posted_at_utc=datetime.fromisoformat(post["posted_at_utc"]),
    )
    return routed.kind == "signal" and shows_call(routed.signal, row)


def _gone_post(
    conn: sqlite3.Connection,
    post: tuple[int, int],
    channel_cfg: _ChannelCfgLike | None = None,
) -> int | None:
    """For `old_post`: the signal stored post `post` was stored with (its
    own, or the one it copies), when a forward of it is an echo of its
    removal. None otherwise, or when it is no post of a call:

    - the post closed one of its call's signals (it withdrew, broke or
      re-aimed it): its removal was said when it happened -- an echo;
    - it no longer stands -- deleted, edited into something that is no
      signal, or (re-read with `channel_cfg`) re-aimed away -- without
      closing anything, or closing a signal only for a promotion
      (`closed_by_promotion`: its channel still showed the call): a
      repeat removal on its channel. The forward echoes
      it only while that channel still shows the call in a standing post
      (re-read now). Once that channel's last post is gone too, nothing
      said the call was withdrawn, so the forward's removal is a removal
      like any other (ruling R5-2) -- two channels forwarding each other's
      posts can never echo each other's repeats into keeping a call nobody
      shows;
    - it still stands: None.

    These clauses decide what is traded when three or more channels carry
    the call (ruling R6-2): a third channel's forward goes while the call,
    posted again, still stands on another channel, so the last-standing-
    post rule (`old_post`) does not apply, and only these tell an echo
    from a removal that must withdraw the call. With two channels that
    rule already withdraws any call no post shows.

    The call it belongs to need not be the one its forward was matched to:
    a late forward of a withdrawn post may repeat another call of the same
    trade, and removing it still only echoes the withdrawn post."""
    row = conn.execute(
        "SELECT * FROM messages WHERE channel_id = ? AND message_id = ?", post
    ).fetchone()
    if row is None:
        return None
    linked = signals_repo.get_signal_id(conn, channel_id=post[0], message_id=post[1])
    if linked is None:
        linked = row["relay_of_signal_id"]
    if linked is None:
        return None
    chain = chain_ids(conn, linked)
    marks = ", ".join("?" for _ in chain)
    closer = conn.execute(
        f"SELECT signal_id FROM signals WHERE signal_id IN ({marks}) "
        "  AND closed_by_channel_id = ? AND closed_by_message_id = ?",
        (*chain, *post),
    ).fetchone()
    if closer is not None and not closed_by_promotion(conn, closer["signal_id"]):
        return linked  # its removal withdrew one of the call's signals: said then
    gone = (
        row["deleted_at_utc"] is not None
        or row["kind"] != "signal"
        or (
            channel_cfg is not None
            and not post_shows_call(row, _signal_row(conn, linked), channel_cfg=channel_cfg)
        )
    )
    if not gone:
        return None
    # A repeat removal: echoed only while its channel still shows the call.
    newest = _signal_row(conn, live_signal_for(conn, linked))
    for other in standing_posts_on(conn, signal_id=linked, channel_id=post[0]):
        if channel_cfg is None or post_shows_call(other, newest, channel_cfg=channel_cfg):
            return linked
    return None


def _call_stands_elsewhere(
    conn: sqlite3.Connection,
    *,
    signal_id: int,
    post: tuple[int, int],
    channel_cfg: _ChannelCfgLike | None,
) -> bool:
    """Whether a post of `signal_id`'s call other than `post` still stands,
    on any channel: not deleted, still stored as a signal, not the post
    that closed one of the call's signals (`standing_posts_on`) and -- with
    `channel_cfg`, the changed post's channel config -- re-read still
    showing the call of its newest signal. A post of another channel is
    re-read with that config too (its own is not at hand here): one that
    does not read as the call with it counts as not standing, which only
    makes a removal withdraw the call -- the safe side."""
    newest = _signal_row(conn, live_signal_for(conn, signal_id))
    chain = chain_ids(conn, signal_id)
    marks = ", ".join("?" for _ in chain)
    for other in conn.execute(
        "SELECT m.* FROM messages m "
        "WHERE m.deleted_at_utc IS NULL AND m.kind = 'signal' "
        "  AND NOT (m.channel_id = ? AND m.message_id = ?) "
        f"  AND (m.relay_of_signal_id IN ({marks}) OR EXISTS ("
        "    SELECT 1 FROM signals s WHERE s.channel_id = m.channel_id "
        f"     AND s.message_id = m.message_id AND s.signal_id IN ({marks}))) "
        "  AND NOT EXISTS (SELECT 1 FROM signals s "
        f"    WHERE s.signal_id IN ({marks}) AND s.closed_by_channel_id = m.channel_id "
        "     AND s.closed_by_message_id = m.message_id)",
        (*post, *chain, *chain, *chain),
    ).fetchall():
        if channel_cfg is None or post_shows_call(other, newest, channel_cfg=channel_cfg):
            return True
    return False


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
    return posted_at(conn, channel_id=signal["channel_id"], message_id=signal["message_id"])


def posted_at(conn: sqlite3.Connection, *, channel_id: int, message_id: int) -> datetime:
    """When stored post (`channel_id`, `message_id`) was posted -- for the
    alerts that name a carrier only within the relay window of their post
    (`carrier_of`)."""
    (posted,) = conn.execute(
        "SELECT posted_at_utc FROM messages WHERE channel_id = ? AND message_id = ?",
        (channel_id, message_id),
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
