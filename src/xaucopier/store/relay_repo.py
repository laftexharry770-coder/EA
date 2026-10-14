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
replaces (`signals.promoted_from_signal_id`). So does a post of the call
on a channel switched on that watch's start admitted as a signal in place
of one whose channel was switched off (`signals.admitted_from_signal_id`,
final fix round 7, R7-1, below): once that channel is switched back on,
its posts of the call -- their edits, their deletion, the follow-ups
replying to them -- act on the signal traded in its place, as with both
channels read. The *newest* signal of the
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

**A signal whose channel was switched off carries nothing** (final fix
round 2, `switched_off`): `watch` cancels its waiting entries at start,
so nothing trades it any more. It is never the live call a new post
repeats (`relay_of_signal`), never the signal a promotion defers to
(`carrier_of_trade`), and never the carrier an alert names when it says
"do NOT trade it by hand as well" (`carrier_of`): the same call posted on
a channel switched on is traded instead. A signal whose entries were
refused for any other reason still carries its call. A post of the call
on a channel switched on that was stored as its copy *before* the switch
took effect -- watch reads a channel switched off until it restarts -- is
run through these rules again by the start-up job that cancels those
entries (`admit_copies_of_switched_off`, final fix round 3): the newest
one standing becomes a signal of its own -- if it was posted recently
enough to be traded (the policy's `max_signal_age_seconds`, final fix
round 4); one line says how many older ones are not. The same job
withdraws a signal of a channel switched on that only a post on a channel
switched off still showed (final fix round 5, R5-1): its own post went
while watch read both channels, so its removal was an echo or a repeat,
and nothing would see that other post go now. It looks only at signals
whose own post was deleted or edited, and says the calls too old to be
traded anyway in one line (final fix round 6, R6-2).

**An older post of a call** (ruling R4-1, `old_post`). When a call was
withdrawn and posted again, the other channel's posts of the withdrawn
signal are still posts of the call, and their edits reach its newest
signal. Their *removal* -- deleted, edited away, re-aimed or broken --
is told apart:

- a forward of *another channel's* post of the call that no longer
  stands (deleted, edited away, re-aimed, or the post that withdrew one of
  its signals -- or any post deleted in the same report as that one while
  it still showed the call, ruling R6-3) is an *echo*: its removal only
  repeats that post's, which
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
the call still stands on any channel this watch reads
(`_call_stands_elsewhere`; a post on a channel switched off counts for
nothing, final fix round 4), it is a
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
  standing while the call is queued -- nor when the same call is posted
  afresh, as a new signal within the relay window of a held-back copy
  that the relay rules now take for a copy of it (`admit_signal`, final
  fix round 5, R5-4). Nor is one ever raised while it is:
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

**A follow-up about a call whose channel is switched off** (final fix
round 6, R6-1; a WRONG action is worse than a missed one). Watch's start
admits a copy of such a call as a signal of its own and adopts the copies
of its follow-ups (`admit_copies_of_switched_off`), recording which
signal it admitted in place of which (`signals.admitted_from_signal_id`)
and which held follow-up each adopted one stands in for
(`followups.instead_of_followup_id`). A follow-up read after that start
which repeats a held one about the switched-off call
(`followup_after_channel_off`) is a copy of the one standing in for it,
if any; else, when it replies to a post of the signal admitted in its
place -- matched as the relay rules would have matched it while both
channels were read -- an instruction of its own about that signal,
standing in for the held one; else a copy, NOT applied, with a WARNING
(`FOLLOWUP_ABOUT_CHANNEL_OFF_SIGNAL`) that names the signal admitted in
its place, if one was, for the user to apply it by hand -- and none is
ever searched for by time. Once one copy of a held instruction was handed
over so, no later copy of it is applied either: never done twice. The
admission is a link of the call's chain (final fix round 7, R7-1), yet a
follow-up stored before it about the switched-off signal stays with that
signal (`signal_for_followup`): its copies act in its place, or it was
handed over -- carried over the admission it would act twice. One read
after the start that replies to a post of the call -- the channel
switched back on -- is about the signal traded instead, as any reply is,
and the relay rules match it against the instructions about that signal.

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
  So is another channel's held-back copy re-read when a new signal is
  stored (`_held_back_near`, `_ReadAs`): one that does not read as a
  signal that way stays held back, its alert standing -- the loud side.
- One channel suffices: of several posts of the call deleted in one
  report, only the lowest id is recorded as the post that withdrew it
  (`edits_repo._closing_post`), so a reply to another of them follows the
  call if it is posted again -- as a reply to any repeat removed before
  the withdrawal does. The same in either storage order (INV-2). A
  forward of any of them removed later is an echo all the same
  (`_deleted_with_closer`, ruling R6-3): only the replies differ.
- A call whose channel was switched off is never traded from a post that
  already holds an earlier signal of its chain (final fix round 3; one
  post, one signal). Example: the VIP's call was withdrawn when the relay
  deleted its copy, the relay posted the call again, and the relay is then
  switched off: the VIP's post holds the withdrawn signal, so it is not
  admitted again, and the call is not traded.
- A follow-up stored as a copy of one that was moved to another live
  signal -- not the one a start admitted -- or that has no target, is left
  a copy by that start (final fix round 4): it repeats an instruction
  about another call, or about none. Reaching it takes four channels, or a
  reply stored before the post it replies to.
- A copy held back that a new signal does not repeat by the relay window
  keeps its CRITICAL "trade it by hand": by the relay rules it is another
  call -- though the same call posted 900 s or more away may be queued.
- The follow-up copies of a copy re-linked at a start to a signal that was
  live already (not admitted at that start) are not looked at again: only
  the copies replying to, or forwarding the instruction about, a signal
  admitted then are (final fix rounds 4 and 5).
- A forward of a switched-off channel's follow-up whose own post was never
  stored -- the channel was switched off before watch read it -- says
  nothing of what it is about: a follow-up with no target, as any forward
  of a post never read (final fix round 5, R5-2).
- A follow-up read after a start that repeats an instruction about a call
  whose channel is switched off is applied only when it replies to the
  signal traded instead, and only while no copy of the same instruction
  was handed to the user by hand (final fix round 6, R6-1): which of the
  VIP's forward and its own reply comes first decides whether the
  instruction is applied once or handed over (never twice). And two posts
  of one channel are two instructions, as with both channels read --
  unless a post of another channel near one makes it a repeat: after the
  start, such a repeat is matched across the instructions about the
  switched-off call and the signal traded instead, so the VIP's forward
  stored before the relay's post and the VIP's own reply act once, not
  twice as while both channels were read.
- The posts of one report are told apart by their deletion time: the
  listener stamps every job of one report with the report's moment. A
  post of the report whose job failed and was marked deleted later, by
  another report, counts as deleted on its own -- a repeat removal: a
  forward of it removed once its channel shows the call nowhere
  withdraws the call posted again, loudly (the safe side).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Collection, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from xaucopier.domain.market import Policy
from xaucopier.domain.signal import FollowUp, Signal
from xaucopier.parsing.router import RouteResult, route
from xaucopier.store import alerts, metrics, outbox, signals_repo
from xaucopier.store import words as store_words

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
#: WARNING: a post on a channel switched on was stored as a copy of a signal
#: whose channel was then switched off; its waiting entries were cancelled,
#: so the post is now a signal of its own, queued (final fix round 3, R3-1;
#: `admit_copies_of_switched_off`).
COPY_OF_CHANNEL_OFF_SIGNAL_ADMITTED = "COPY_OF_CHANNEL_OFF_SIGNAL_ADMITTED"
#: WARNING, at most one per start: such posts that were posted longer ago
#: than the age gate allows are not admitted -- too old to be traded --
#: and this one line says how many, and of which signals (final fix round
#: 4, R4-2; `admit_copies_of_switched_off`).
OLD_COPIES_OF_CHANNEL_OFF_SIGNALS = "OLD_COPIES_OF_CHANNEL_OFF_SIGNALS"
#: WARNING -- CRITICAL when some of it was executed: at a start, a signal of
#: a channel switched on whose own post no longer stands, kept standing only
#: by a post on a channel switched off now, which watch no longer reads: it
#: is withdrawn (final fix round 5, R5-1; `admit_copies_of_switched_off`).
SIGNAL_SHOWN_ONLY_ON_CHANNEL_OFF = "SIGNAL_SHOWN_ONLY_ON_CHANNEL_OFF"
#: WARNING, one for the whole start: the signals withdrawn so
#: (`SIGNAL_SHOWN_ONLY_ON_CHANNEL_OFF`) that were posted too long ago to be
#: traded anyway, of which nothing was executed (final fix round 6, R6-2).
OLD_SIGNALS_SHOWN_ONLY_ON_CHANNEL_OFF = "OLD_SIGNALS_SHOWN_ONLY_ON_CHANNEL_OFF"
#: WARNING: a follow-up read after watch started repeats an instruction about
#: a signal whose channel is switched off: stored as a copy, NOT applied
#: (final fix round 6, R6-1; `followup_after_channel_off`).
FOLLOWUP_ABOUT_CHANNEL_OFF_SIGNAL = "FOLLOWUP_ABOUT_CHANNEL_OFF_SIGNAL"

#: The words of a `FOLLOWUP_ABOUT_CHANNEL_OFF_SIGNAL` WARNING that hand an
#: instruction to the user to carry out by hand (`handed_over` looks for them).
BY_HAND = "apply it by hand"

#: How old a signal may be and still be traded when the caller gives no
#: `max_signal_age_seconds` (`domain.market.Policy`'s default, 120 s).
DEFAULT_MAX_SIGNAL_AGE_SECONDS = Policy().max_signal_age_seconds

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
class Admission:
    """What `admit_signal` made of a message read as a signal."""

    #: The signal it repeats: it was stored as a copy of it
    #: (`record_signal_copy`), and nothing was queued. None otherwise.
    match: SignalMatch | None
    #: Else the new signal, fanned out.
    signal_id: int | None
    #: The withdrawn signal the new one replaces (the call posted again).
    replaces: int | None
    #: The ENTRY rows queued for the new signal.
    queued: int
    #: The alerts raised on the way (a held-back copy's, a replacement's).
    alert_ids: tuple[int, ...]


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
    channel that withdrew it (`signals.promoted_from_signal_id`), or a post
    of the call on a channel switched on that watch's start admitted in
    place of a signal whose channel was switched off
    (`signals.admitted_from_signal_id`; final fix round 7, R7-1) --
    followed to its end. Only that newest signal can be live: each one it
    replaced was withdrawn, or its channel switched off, before anything
    of it was executed. So a post of the call on a channel switched back
    on -- its edits, its deletion, the follow-ups replying to it -- acts on
    the signal traded in its place, as with both channels read.

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
    replaced, itself, and the ones that replaced it -- through a
    replacement or an admission at watch's start (`live_signal_for`). (A
    signal both replacing a withdrawn one of its own channel and admitted
    in place of another's -- two links back -- follows the replacement
    back: the switched-off call is not in its chain, only it is in the
    switched-off call's. `_segments` cuts before it all the same.)"""
    earlier: list[int] = []
    current = signal_id
    while True:
        row = conn.execute(
            "SELECT COALESCE(promoted_from_signal_id, admitted_from_signal_id) FROM signals "
            "WHERE signal_id = ?",
            (current,),
        ).fetchone()
        if row is None or row[0] is None or row[0] >= current:
            break
        current = row[0]
        earlier.append(current)
    return [*reversed(earlier), signal_id, *_replacements(conn, signal_id)]


def _admitted(conn: sqlite3.Connection, signal_id: int) -> int | None:
    """The switched-off signal watch's start admitted signal `signal_id` in
    place of (`signals.admitted_from_signal_id`; final fix round 7, R7-1),
    or None."""
    row = conn.execute(
        "SELECT admitted_from_signal_id FROM signals WHERE signal_id = ?", (signal_id,)
    ).fetchone()
    return None if row is None else row[0]


def _segments(conn: sqlite3.Connection, signal_id: int) -> list[list[int]]:
    """`signal_id`'s call (`chain_ids`) cut at each admission at watch's
    start (R7-1), oldest first: the signals of one switched-off call, then
    those of the one admitted in its place, and so on.

    The cut comes before EVERY signal admitted at a start -- also one whose
    link back in `chain_ids` is a replacement: a post of the VIP's posted
    again after it withdrew its call, stored as a copy of the relay's call
    and admitted in its place (final fix round 8, R8-1). So an admitted
    signal always begins a segment of its own, whichever signal of the call
    the chain is taken from, and the follow-ups kept with the withdrawn
    signal it replaced (`kept_closer`) are never taken for ones about the
    call it carries. (The follow-ups that replacement moves to it are moved
    as it is stored, before the admission is recorded: `record_replacement`.)
    The switched-off part is the one its `admitted_from_signal_id` names
    (`_switched_off_part`), not always the segment before it."""
    chain = chain_ids(conn, signal_id)
    segments = [[chain[0]]]
    for later in chain[1:]:
        if _admitted(conn, later) is not None:
            segments.append([later])
        else:
            segments[-1].append(later)
    return segments


def _segment_of(conn: sqlite3.Connection, signal_id: int) -> list[int]:
    """The signals of `signal_id`'s call between two admissions at watch's
    start (`_segments`) that `signal_id` is one of."""
    return next(segment for segment in _segments(conn, signal_id) if signal_id in segment)


def _switched_off_part(conn: sqlite3.Connection, segment: list[int]) -> list[int] | None:
    """For a segment of a call (`_segments`): the switched-off call's own
    part that its admission at watch's start was in place of -- the segment
    of its first signal's `admitted_from_signal_id` -- or None when it was
    no admission (final fix round 8, R8-1). Only a segment's first signal
    can be admitted at a start: `_segments` cuts before each one, and the
    chain's first signal never is (`chain_ids` walks back over every
    link)."""
    switched = _admitted(conn, segment[0])
    return None if switched is None else _segment_of(conn, switched)


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
    that signal itself. None when it has no target.

    A follow-up targeting a signal whose channel was switched off, stored
    before watch's start admitted another in its place, stays with it (its
    copies on a channel switched on act in its place, or the user was told
    to apply it by hand: final fix rounds 4-6): it is never carried over
    the admission (final fix round 7, R7-1). One stored after it -- a reply
    to a post of the call read once that channel is back on -- targets the
    admitted signal already (`resolve_reply_target`)."""
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
    segment = _segment_of(conn, target)
    return segment[-1]


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
    read_channels: Collection[int] | None = None,
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
      nothing is the safe side -- but loudly (`edits_repo`).

    `read_channels`: the channels this watch reads, for the check that the
    post is not the call's last standing one (`_call_stands_elsewhere`;
    final fix round 4, R4-5). None: every channel counts."""
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
        conn,
        signal_id=start,
        post=(channel_id, message_id),
        channel_cfg=channel_cfg,
        read_channels=read_channels,
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
    queued, and trading it by hand as well would open it twice (R3-P3) --
    unless the copy repeats another call by now (linked to a call traded
    instead at a start, `_relink_held_back`): it is left as it was, and no
    WARNING says it repeats this one (final fix round 5, R5-3).
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
    # Never a follow-up about a call before an admission at watch's start:
    # it stays with the switched-off signal (`signal_for_followup`, R7-1).
    _move_followups(
        conn,
        earlier=[sid for sid in _segment_of(conn, new_signal_id) if sid != new_signal_id],
        new=new,
        kept=kept,
    )
    held_back = conn.execute(
        "SELECT alert_id, channel_id, message_id, signal_id, acked FROM alerts "
        f"WHERE code = ? AND signal_id IN ({marks}) ORDER BY alert_id",
        (COPY_OF_WITHDRAWN_SIGNAL, *earlier),
    ).fetchall()
    if not held_back:
        return ()
    queued = _queued_entries(conn, new_signal_id)
    raised: list[int] = []
    for alert in held_back:
        conn.execute(
            "UPDATE messages SET relay_of_signal_id = ? WHERE channel_id = ? AND message_id = ? "
            f"  AND relay_of_signal_id IN ({marks})",
            (new_signal_id, alert["channel_id"], alert["message_id"], *earlier),
        )
        if _link_of(conn, alert["channel_id"], alert["message_id"]) != new_signal_id:
            # Not re-linked: the copy repeats another call already -- one
            # traded instead at a start, say (`_relink_held_back`). It does
            # not repeat this one, and no WARNING may say it does (final fix
            # round 5, R5-3).
            continue
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
            _supersede(
                conn,
                alert=alert,
                new=new,
                stands=(
                    "The channel that withdrew the call has now posted it again, as message "
                    f"{new['message_id']} on channel {new['channel_id']}: it is signal "
                    f"{new_signal_id}, with {queued} trade entr{'y' if queued == 1 else 'ies'} "
                    "queued, and the copy now repeats it."
                ),
                now_utc=now_utc,
            )
        )
    return tuple(raised)


def _move_followups(
    conn: sqlite3.Connection,
    *,
    earlier: list[int],
    new: sqlite3.Row,
    kept: set[tuple[int, int]],
) -> None:
    """Every follow-up that targeted one of the `earlier` signals of a call
    targets signal `new` now (path `'reply'` when it replies to `new`'s own
    message, `'relay-reply'` for any other post) -- except one replying to
    a post in `kept`, which stays with the signal that post closed (rulings
    R4-4, R5-3)."""
    marks = ", ".join("?" for _ in earlier)
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
            (new["signal_id"], path, followup["followup_id"]),
        )


def carrier_of_trade(
    conn: sqlite3.Connection,
    *,
    trade_hashes: Iterable[str],
    posted_at_utc: datetime,
    window: int = RELAY_WINDOW_SECONDS,
) -> sqlite3.Row | None:
    """For a promotion (`edits_repo._promote_repost`, final fix FF12): the
    signal that already carries the trade a standing repost of a withdrawn
    signal shows -- the newest signal (`live_signal_for`) of a call
    sharing one of `trade_hashes` (current or first accepted) whose own
    message was posted within `window` seconds of `posted_at_utc`, the
    repost's own time, and which is not withdrawn-unexecuted: live,
    queued, in flight or traded -- and can still trade (`switched_off`:
    never one whose entries were cancelled because its channel was
    switched off, final fix round 2). Exactly what that repost would
    repeat if it were stored now (`relay_of_signal`: "a live call wins"). The window
    is measured from each signal's own message, so a call posted 890 s and
    910 s after its first post has a copy of the first and a signal of its
    own, and promoting the copy would open the same trade twice (ruling
    P2). The withdrawn signal's own chain is never named: by then every
    signal of it is withdrawn and unexecuted. None when no signal carries
    the trade."""
    for trade_hash in dict.fromkeys(trade_hashes):
        for candidate in _same_trade(
            conn, trade_hash=trade_hash, posted_at_utc=posted_at_utc, window=window
        ):
            newest = _signal_row(conn, live_signal_for(conn, candidate["signal_id"]))
            if carries(conn, newest):
                return newest
    return None


def record_carried(
    conn: sqlite3.Connection,
    *,
    carrier_signal_id: int,
    withdrawn_signal_id: int,
) -> None:
    """Signal `withdrawn_signal_id` was withdrawn before anything of it was
    executed while its channel still shows its call in a standing repost,
    and that call is already carried by signal `carrier_signal_id`
    (`carrier_of_trade`): nothing is promoted (final fix FF12). Every
    copy of the withdrawn signal -- that repost included, on any channel,
    and one deleted before, which a window read may still read back --
    repeats the carrier now, as every copy of a promoted repost's call
    reaches it through `live_signal_for` (the post that withdrew it is
    the signal's own message, no copy), and every follow-up that targeted one of the withdrawn
    call's signals targets the carrier, as `record_replacement` moves them
    to a promoted repost: the call never stopped (ruling R5-3), so a reply
    to the post that withdrew it follows the call too."""
    carrier = _signal_row(conn, carrier_signal_id)
    earlier = chain_ids(conn, withdrawn_signal_id)
    conn.execute(
        "UPDATE messages SET relay_of_signal_id = ? WHERE relay_of_signal_id = ?",
        (carrier_signal_id, withdrawn_signal_id),
    )
    kept = {
        post
        for sid in earlier
        if sid != withdrawn_signal_id and (post := kept_closer(conn, sid)) is not None
    }
    # Never over an admission at watch's start (R7-1, `signal_for_followup`).
    _move_followups(
        conn, earlier=_segment_of(conn, withdrawn_signal_id), new=carrier, kept=kept
    )
    logger.info(
        "withdrawn signal %s: its call is carried by signal %s (channel %s message %s); its "
        "copies and follow-ups repeat that signal now (the call's earlier signals: %s)",
        withdrawn_signal_id,
        carrier_signal_id,
        carrier["channel_id"],
        carrier["message_id"],
        earlier,
    )


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
    one that is live or of which something was traded (`live_signal_for`),
    and not one whose channel was switched off (`switched_off`): the
    `signals` row, or None."""
    newest = live_signal_for(conn, signal_id)
    if newest == signal_id:
        return None
    row = _signal_row(conn, newest)
    live = row["state"] == "NEW" and row["withdrawn_at_utc"] is None
    if (
        live or signals_repo.trade_status(conn, signal_id=newest) != "pending"
    ) and not switched_off(conn, row):
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
    one of the moments `around`, other than the signals `besides`, and not
    one whose channel was switched off (`switched_off`); or None. Signals
    stay NEW for ever: a call with the same zone posted days ago is
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
        if any(abs(posted - moment) <= limit for moment in moments) and not switched_off(
            conn, row
        ):
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


def describe_stored_call(parsed_json: str) -> str:
    """A `signals` row's call (its `parsed_json`), as `describe_call`
    says a parse's: side and entry zone."""
    parsed = json.loads(parsed_json)
    zone = parsed["zone"]
    return f"{parsed['side'].upper()} {zone['lo']:g}-{zone['hi']:g}"


def _describe_both(signal: Signal, row: sqlite3.Row) -> tuple[str, str]:
    """A parse's call and a `signals` row's, for an alert that sets them
    side by side: side and entry zone (`describe_call`), and the symbol
    too when the two differ."""
    parsed = json.loads(row["parsed_json"])
    stored = describe_stored_call(row["parsed_json"])
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


def switched_off(conn: sqlite3.Connection, signal: sqlite3.Row) -> bool:
    """`signal` (a `signals` row) can no longer trade because its channel
    was switched off (final fix round 2): nothing of it was executed or is
    being executed, no trade entry of it is waiting, and its waiting ones
    were cancelled as `outbox.CHANNEL_OFF_REASON` -- which `watch` writes
    at start for every channel not switched on, so this is also how the
    store knows a signal's channel is not enabled. Such a signal is never
    the live carrier of a call (`carries`): a post of the same call on a
    channel switched on is traded instead of repeating it.

    Entries refused for any other reason -- a worker's age gate, say --
    leave a signal the carrier: a later copy must never re-open a call a
    gate refused (the age-gate basis for forwards is a Plan 3 decision)."""
    counts = outbox.state_counts(conn, kind="ENTRY", signal_id=signal["signal_id"])
    if counts.get("QUEUED") or counts.get("CLAIMED") or counts.get("DONE"):
        return False
    return (
        conn.execute(
            "SELECT 1 FROM outbox WHERE kind = 'ENTRY' AND signal_id = ? AND state = 'SKIPPED' "
            "AND reason = ? LIMIT 1",
            (signal["signal_id"], outbox.CHANNEL_OFF_REASON),
        ).fetchone()
        is not None
    )


def carries(conn: sqlite3.Connection, signal: sqlite3.Row) -> bool:
    """`signal` (a `signals` row) can be the live carrier of its call: it
    is not withdrawn-unexecuted (`is_withdrawn_unexecuted`) and its channel
    was not switched off (`switched_off`)."""
    return not is_withdrawn_unexecuted(conn, signal) and not switched_off(conn, signal)


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
            if not switched_off(conn, found):
                return SignalRelay(_signal_match(conn, found, via="forward"))
            # The post's signal can no longer trade -- its channel was
            # switched off (final fix round 2): a live call on a channel
            # switched on, or this post as a new signal.
            live = _live_same_trade(
                conn,
                trade_hash=trade_hash,
                around=(posted_at_utc, _posted_at(conn, found)),
                window=relay_window_seconds,
                exclude=own,
            )
            if live is not None:
                return SignalRelay(_signal_match(conn, live, via="hash"))
            return SignalRelay(None)
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
        if not is_withdrawn_unexecuted(conn, newest) and switched_off(conn, newest):
            # The call lives on as a signal whose channel was switched off
            # (final fix round 2): nothing trades it there.
            return SignalRelay(None)
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
            if switched_off(conn, newest):
                # It can no longer trade: its channel was switched off
                # (final fix round 2). The call is not carried there.
                continue
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


def admit_signal(
    conn: sqlite3.Connection,
    *,
    channel_cfg: _ChannelCfgLike,
    message_id: int,
    signal: Signal,
    posted_at_utc: datetime,
    relay_window_seconds: int,
    fwd_from_channel_id: int | None,
    fwd_from_post_id: int | None,
    accounts: tuple,
    now_utc: datetime,
    deleted_in_report: tuple[datetime, Collection[int]] | None = None,
    supersede_held_back: bool = True,
) -> Admission:
    """A message of `channel_cfg`'s channel -- already on `messages` --
    read as signal `signal` for the first time, admitted by the relay
    rules (`relay_of_signal`): a copy of a call we have is stored as such
    (`record_signal_copy`; a copy of a withdrawn call is held back
    loudly) and nothing is queued; else it is a new signal -- replacing a
    withdrawn call its channel posts again (`record_replacement`) -- with
    its ENTRY rows fanned out, in the caller's writer job. The one door
    for a new signal: `messages_repo.ingest` for a message stored for the
    first time, and `edits_repo` for a post an edit completed within the
    grace (final review F11), so both follow the same rules -- the same
    call already live or queued is never a second signal (G3L-1).

    `deleted_in_report` (the report's time and every id it named): a
    deletion report named this message before it was stored (final review
    F18). When the same report deleted the post that withdrew the call
    this one would replace -- a double post deleted together, one of them
    raced -- it is no call posted again but a copy of the call that report
    withdrew (`_withdrawn_in_report`, final fix FF5): linked quietly,
    exactly as the report handled after both were stored leaves it.

    A new signal -- the call posted afresh -- may be what a copy held back
    as a copy of a withdrawn call repeats now, by the relay rules: a post
    near both, still held back, its "trade it by hand" standing while the
    same call is queued (final fix round 5, R5-4). So the copies held back
    within the relay window of its post are run through the relay rules
    again (`_held_back_near`, `_live_carrier`); each that repeats it now is
    linked to it, and its CRITICAL is acknowledged and superseded by a
    WARNING (`_supersede`), as `record_replacement` does for the copies held
    back against the call it replaces -- and after it, for the copies held
    back against another withdrawn call (a post delivered late can replace
    one call and be near a copy held back against the other).
    `supersede_held_back` False leaves that to the caller
    (`admit_copies_of_switched_off`, which says it in its own words)."""
    channel_id = channel_cfg.channel_id
    relay = relay_of_signal(
        conn,
        channel_id=channel_id,
        message_id=message_id,
        trade_hash=signal.content_hash,
        posted_at_utc=posted_at_utc,
        relay_window_seconds=relay_window_seconds,
        fwd_from_channel_id=fwd_from_channel_id,
        fwd_from_post_id=fwd_from_post_id,
        channel_cfg=channel_cfg,
        signal=signal,
    )
    if relay.match is not None:
        raised = record_signal_copy(
            conn,
            channel_id=channel_id,
            message_id=message_id,
            match=relay.match,
            now_utc=now_utc,
            signal=signal,
            relay_window_seconds=relay_window_seconds,
        )
        return Admission(relay.match, None, None, 0, raised)
    if (
        relay.replaces is not None
        and deleted_in_report is not None
        and _withdrawn_in_report(conn, relay.replaces, channel_id, *deleted_in_report)
    ):
        withdrawn = _signal_match(conn, _signal_row(conn, relay.replaces), via="hash")
        _record_copy_withdrawn_with(
            conn, channel_id=channel_id, message_id=message_id, match=withdrawn, now_utc=now_utc
        )
        return Admission(withdrawn, None, None, 0, ())
    signal_id = signals_repo.insert_signal(
        conn,
        channel_id=channel_id,
        message_id=message_id,
        signal=signal,
        promoted_from_signal_id=relay.replaces,
        replacement_kind=None if relay.replaces is None else "posted_again",
    )
    # Same transaction as the signal insert above -- see messages_repo's
    # module docstring and tests/test_outbox.py's atomicity tests.
    queued = outbox.fan_out_checked(
        conn,
        kind="ENTRY",
        signal_id=signal_id,
        followup_id=None,
        accounts=accounts,
        now_utc=now_utc,
    )
    raised: tuple[int, ...] = ()
    if relay.replaces is not None:
        # The channel that withdrew the call posted it again: this signal
        # carries the call now.
        raised = record_replacement(
            conn,
            new_signal_id=signal_id,
            replaced_signal_id=relay.replaces,
            now_utc=now_utc,
            channel_cfg=channel_cfg,
        )
    if supersede_held_back:
        # Copies held back against another withdrawn call, too: the relay
        # rules may take them for copies of this one now (R5-4).
        raised += tuple(
            _supersede_held_back_by(
                conn,
                _signal_row(conn, signal_id),
                channel_cfg=channel_cfg,
                relay_window_seconds=relay_window_seconds,
                now_utc=now_utc,
            )
        )
    return Admission(None, signal_id, relay.replaces, queued, raised)


def _supersede_held_back_by(
    conn: sqlite3.Connection,
    new: sqlite3.Row,
    *,
    channel_cfg: _ChannelCfgLike,
    relay_window_seconds: int,
    now_utc: datetime,
) -> list[int]:
    """For `admit_signal` (final fix round 5, R5-4): every copy held back
    within the relay window of new signal `new`'s post (`_held_back_near`)
    that repeats `new` now by the relay rules (`_live_carrier`) is linked to
    it (`_relink`), and its `COPY_OF_WITHDRAWN_SIGNAL` alert acknowledged and
    superseded (`_supersede`). Returns the ids of the WARNINGs."""
    raised: list[int] = []
    queued = _queued_entries(conn, new["signal_id"])
    for copy, alert_ids in _held_back_near(
        conn,
        new,
        relay_window_seconds=relay_window_seconds,
        # Another channel's config is not at hand here: its posts are read
        # with this channel's parser (`_ReadAs`).
        read_as=lambda channel_id: channel_cfg if channel_id == channel_cfg.channel_id else None,
    ):
        carrier = _live_carrier(conn, copy, relay_window_seconds=relay_window_seconds)
        if carrier is None or carrier["signal_id"] != new["signal_id"]:
            continue
        _relink(conn, copy, carrier, why="held back as a copy of withdrawn signal %s")
        raised.extend(
            _supersede_all(
                conn,
                alert_ids,
                new=carrier,
                stands=(
                    "The same call stands as message "
                    f"{carrier['message_id']} on channel {carrier['channel_id']}: it is signal "
                    f"{carrier['signal_id']}, with {queued} trade "
                    f"entr{'y' if queued == 1 else 'ies'} queued, and the copy now repeats it."
                ),
                now_utc=now_utc,
            )
        )
    return raised


@dataclass(frozen=True, slots=True)
class _ReadAs:
    """A channel whose own config is not at hand, read with another
    channel's parser (see "Known limitations")."""

    channel_id: int
    parser: str


def _held_back_near(
    conn: sqlite3.Connection,
    new: sqlite3.Row,
    *,
    relay_window_seconds: int,
    read_as: Callable[[int], _ChannelCfgLike | None],
) -> list[tuple[_Copy, tuple[int, ...]]]:
    """The copies held back -- with an unacknowledged `COPY_OF_WITHDRAWN_SIGNAL`
    alert -- that were posted within `relay_window_seconds` of new signal
    `new`'s own post (both ends included) and still read as a signal (an
    edit away reads as none: `read_post`): each as a `_Copy` of the signal
    its alert names, read with `read_as(channel_id)` (another channel's
    parser when it gives None: `_ReadAs`), with its alerts' ids. Oldest
    alert first (final fix round 5, R5-4). For a NEW signal that replaces
    nothing, the window is the relay rules' own: no withdrawn call of the
    same trade is within the window of its post, or it would replace it or
    be held back itself (`relay_of_signal`), so a copy held back posted
    further from it -- a forward included -- repeats something else."""
    around = _posted_at(conn, new)
    limit = timedelta(seconds=relay_window_seconds)
    found: dict[tuple[int, int], tuple[_Copy, list[int]]] = {}
    for alert in conn.execute(
        "SELECT a.alert_id, a.signal_id, m.* FROM alerts a JOIN messages m "
        "  ON m.channel_id = a.channel_id AND m.message_id = a.message_id "
        "WHERE a.code = ? AND a.acked = 0 ORDER BY a.alert_id",
        (COPY_OF_WITHDRAWN_SIGNAL,),
    ).fetchall():
        key = (alert["channel_id"], alert["message_id"])
        if abs(datetime.fromisoformat(alert["posted_at_utc"]) - around) > limit:
            continue
        if key in found:
            found[key][1].append(alert["alert_id"])
            continue
        channel_cfg = read_as(alert["channel_id"])
        if channel_cfg is None:
            channel_cfg = _ReadAs(alert["channel_id"], read_as(new["channel_id"]).parser)
        routed = read_post(alert, channel_cfg=channel_cfg)
        if routed.kind != "signal":
            continue
        copy = _Copy(alert, routed, channel_cfg, _signal_row(conn, alert["signal_id"]))
        found[key] = (copy, [alert["alert_id"]])
    return [(copy, tuple(ids)) for copy, ids in found.values()]


def admit_copies_of_switched_off(
    conn: sqlite3.Connection,
    *,
    signal_ids: Iterable[int],
    channels: Iterable[_ChannelCfgLike],
    accounts: tuple,
    now_utc: datetime,
    relay_window_seconds: int = RELAY_WINDOW_SECONDS,
    max_age_seconds: int = DEFAULT_MAX_SIGNAL_AGE_SECONDS,
) -> tuple[int, ...]:
    """For `watch`'s start-up job, right after it cancelled the waiting
    entries of `signal_ids` because their channels are not switched on
    (`outbox.skip_entries_of_channels_off`; final fix round 3, R3-1).

    `channels off` takes effect only when watch restarts: until then watch
    reads the channel, so a post of the same call on a channel switched on
    -- `channels`, every one switched on -- was stored as a copy of such a
    signal ("a live call wins"), and nothing would ever trade it. So for
    each of those signals that can no longer trade (`switched_off`; one
    still being traded keeps its copies), its posts standing on a channel
    switched on (`_standing_copies`: not deleted, still showing the call
    read with that channel's parser, no signal of their own) are run
    through the relay rules again, newest first (`relay_of_signal`, which
    never matches such a signal):

    - one that another signal can trade repeats that one now (a quiet
      re-link; the follow-ups replying to it go with it);
    - else the first such post is admitted (`admit_signal`): a new signal
      dated by its own post, its entries fanned out, the follow-ups
      replying to it acting on it (`_move_replies_to`), and a WARNING
      naming both messages (`COPY_OF_CHANNEL_OFF_SIGNAL_ADMITTED`) -- or,
      by those same rules, held back loudly as a copy of a withdrawn call;
    - once one is a new signal, any other is only ever re-linked to a
      signal it repeats -- one call, one signal -- else left as it was;
    - a follow-up stored as a copy of another (`messages.relay_of_followup_id`)
      replying to a post that acts on a signal admitted here -- its own
      message, or a post re-linked to it -- while the follow-up it repeats
      targets one of the switched-off signals' chains, is an instruction
      of its own about the new signal (`_adopt_followup_copies`, final fix
      round 4, R4-4): the VIP's "close half", stored as a copy of the
      relay's, would otherwise act on the cancelled signal only -- and so
      is a forward of that follow-up's own post, when the follow-up is
      about the switched-off signal this new one was admitted in place of
      (final fix round 5, R5-2; a forward replies to nothing). A follow-up
      of its own on a channel switched on whose target came from its copy
      (`'copy-reply'`, a forward stored first) acts on the new signal now
      -- unless another follow-up about it gives the same instruction
      (`_same_instruction_about`, final fix round 6, R6-1): then that one
      acts in its place. Each adopted one records the held follow-up it
      stands in for (`followups.instead_of_followup_id`), so neither this
      job nor a later one, nor a copy read later, makes one held
      instruction two; the admission itself is recorded too
      (`signals.admitted_from_signal_id`). One read after this job is
      judged as it is stored (`followup_after_channel_off`);
    - and once one is a new signal, the copies this job held back, and
      those held back when they were read that were posted within the
      relay window of the new signal's post (`_with_held_back_near`, final
      fix round 5, R5-4), are run through the relay rules again
      (`_relink_held_back`, final fix round 4, R4-3): one that repeats it
      now -- going newest first, a copy near a withdrawn call was held back
      before an older one of the same call was admitted -- is linked to it,
      its CRITICAL "trade it by hand" is
      acknowledged and a WARNING (`COPY_OF_WITHDRAWN_SIGNAL_SUPERSEDED`)
      says not to, as `record_replacement` does: following it would open
      the trade twice;
    - last, a signal of a channel switched on whose own post no longer
      stands, kept tradable only because another post of the call stood --
      one on a channel switched off now, which watch no longer reads -- is
      withdrawn, loudly (`_withdraw_calls_shown_only_off`, final fix round
      5, R5-1): whether watch saw its post go before the restart or after it
      must not decide whether the call is traded. Those too old to be
      traded anyway are said in ONE line (`OLD_SIGNALS_SHOWN_ONLY_ON_CHANNEL_OFF`,
      final fix round 6, R6-2).

    Only a post still young enough to be traded is admitted (final fix
    round 4, R4-2): posted no longer than `max_age_seconds` (the policy's
    `max_signal_age_seconds`) before `now_utc` -- the bound `channels off`
    counts a waiting signal by (`report.signals_in_play`). An older one is
    left as it was, never run through the relay rules again (a "trade it by
    hand" for a call hours old helps no one), and ONE WARNING for the whole
    job says how many there were (`OLD_COPIES_OF_CHANNEL_OFF_SIGNALS`):
    switching off the faster channel of a pair after weeks must not raise a
    WARNING per old call saying its entries are queued.

    Runs in the caller's writer job; returns the ids of the alerts raised."""
    on = {channel.channel_id: channel for channel in channels}
    raised: list[int] = []
    # Posts too old to be traded, per signal they repeat (R4-2).
    too_old: dict[int, int] = {}
    # The copies this job held back, with the alerts it raised for them,
    # not yet linked to a call traded instead (R4-3).
    held: list[tuple[_Copy, tuple[int, ...]]] = []
    # Each signal admitted, with the switched-off signal it repeated (R4-4).
    admissions: list[tuple[int, int]] = []
    for signal_id in dict.fromkeys(signal_ids):
        signal = _signal_row(conn, signal_id)
        if signal is None or not switched_off(conn, signal):
            continue
        admitted = False
        for post, routed, channel_cfg in _standing_copies(conn, signal, on):
            copy = _Copy(post, routed, channel_cfg, signal)
            channel_id, message_id = copy.key
            posted = datetime.fromisoformat(post["posted_at_utc"])
            carrier = _live_carrier(conn, copy, relay_window_seconds=relay_window_seconds)
            if carrier is not None:
                _relink(conn, copy, carrier)
                continue
            if admitted:
                continue  # one call, one signal: it stays as it was
            if (now_utc - posted).total_seconds() > max_age_seconds:
                # Too old to be traded (R4-2): it stays as it was.
                too_old[signal_id] = too_old.get(signal_id, 0) + 1
                continue
            conn.execute(
                "UPDATE messages SET relay_of_signal_id = NULL "
                "WHERE channel_id = ? AND message_id = ?",
                (channel_id, message_id),
            )
            admission = admit_signal(
                conn,
                channel_cfg=channel_cfg,
                message_id=message_id,
                signal=routed.signal,
                posted_at_utc=posted,
                relay_window_seconds=relay_window_seconds,
                fwd_from_channel_id=post["fwd_from_channel_id"],
                fwd_from_post_id=post["fwd_from_post_id"],
                accounts=accounts,
                now_utc=now_utc,
                supersede_held_back=False,  # said below, in this job's words
            )
            raised.extend(admission.alert_ids)
            if admission.signal_id is None:
                # Held back loudly as a copy of a withdrawn call: tried again
                # once a call is traded instead (R4-3).
                held.append((copy, admission.alert_ids))
                continue
            admitted = True
            admissions.append((admission.signal_id, signal_id))
            # Kept (R6-1): a follow-up read later about the switched-off call
            # names the signal traded instead, and only that one.
            conn.execute(
                "UPDATE signals SET admitted_from_signal_id = ? WHERE signal_id = ?",
                (signal_id, admission.signal_id),
            )
            new = _signal_row(conn, admission.signal_id)
            _move_replies_to(conn, posts={(channel_id, message_id)}, signal=signal, new=new)
            raised.append(
                _say_admitted(
                    conn,
                    signal=signal,
                    new=new,
                    channel_cfg=channel_cfg,
                    queued=admission.queued,
                    now_utc=now_utc,
                )
            )
            # The copies held back -- by this job, or when they were read,
            # near the new signal (R5-4) -- tried again.
            held = _relink_held_back(
                conn,
                _with_held_back_near(
                    conn, held, new, on=on, relay_window_seconds=relay_window_seconds
                ),
                raised=raised,
                relay_window_seconds=relay_window_seconds,
                now_utc=now_utc,
            )
    if too_old:
        raised.append(_say_too_old(conn, too_old, max_age_seconds=max_age_seconds, now_utc=now_utc))
    # Once every post is where it stays: the follow-up copies (R4-4).
    # The switched-off calls' own signals: the admissions are links of their
    # chains now (R7-1), so never the signals admitted.
    old_chains = {sid for _, old in admissions for sid in _segment_of(conn, old)}
    for new_id, old in admissions:
        _adopt_followup_copies(
            conn,
            new=_signal_row(conn, new_id),
            admitted_from=_segment_of(conn, old),
            old_chains=old_chains,
            on=on,
            accounts=accounts,
            now_utc=now_utc,
            relay_window_seconds=relay_window_seconds,
        )
    # And a call no post on a channel switched on shows any more (R5-1).
    raised.extend(
        _withdraw_calls_shown_only_off(
            conn, on=on, now_utc=now_utc, max_age_seconds=max_age_seconds
        )
    )
    return tuple(raised)


def _withdraw_calls_shown_only_off(
    conn: sqlite3.Connection,
    *,
    on: dict[int, _ChannelCfgLike],
    now_utc: datetime,
    max_age_seconds: int = DEFAULT_MAX_SIGNAL_AGE_SECONDS,
) -> list[int]:
    """For `admit_copies_of_switched_off`, at the end of its job (final fix
    round 5, R5-1): every signal of a channel switched on (`on`) that still
    has trade entries waiting while its own post no longer stands --
    deleted, or edited so that it no longer shows the call -- is looked at
    again. Its post's removal kept it standing because another post of the
    call still stood (an echo, or a repeat); if no such post stands on a
    channel switched on now (`_call_stands_elsewhere` with those channels),
    it stood only on a channel switched off since, which watch no longer
    reads: nothing would see that post go, and a call no post watch reads
    shows is never left to trade (INV-1). It is withdrawn as `edits_repo`
    withdraws a call at its last post's removal -- the same as when that
    removal is seen after the restart: VOIDED (flagged, when some of it was
    executed), its waiting entries cancelled, its own post the one that
    withdrew it -- with an alert naming the post on the channel switched off
    (`SIGNAL_SHOWN_ONLY_ON_CHANNEL_OFF`). Returns the ids of the alerts.

    It runs at every start, over every signal ever stored -- in watch-only
    Plan 2 their entries wait for ever -- so only a signal whose own post
    may no longer stand is looked at (final fix round 6, R6-2): deleted, no
    longer stored as a signal, or edited (every edit stamps
    `edit_date_utc`) since a start last re-read it and found it still
    showing the call (`signals.reread_edit_date_utc`, final fix round 7,
    R7-6: a channel that adds "TP1 hit" to its posts would otherwise have
    every one re-read at every start). A post never edited still shows the
    call it was read as, and so does one not edited since it was re-read
    so. And a signal posted more than `max_age_seconds` before `now_utc`,
    too old to be traded anyway, of which nothing was executed, is
    withdrawn without an alert of its own: ONE WARNING for the whole start
    says how many (`OLD_SIGNALS_SHOWN_ONLY_ON_CHANNEL_OFF`), as
    `_say_too_old` does. One partly traded keeps its own CRITICAL."""
    read = frozenset(on)
    raised: list[int] = []
    too_old: list[tuple[int, int]] = []  # (signal, entries cancelled)
    for signal in conn.execute(
        "SELECT s.*, m.posted_at_utc AS own_posted_at_utc, m.edit_date_utc AS own_edit_date_utc "
        "FROM signals s "
        "JOIN messages m ON m.channel_id = s.channel_id AND m.message_id = s.message_id "
        "WHERE m.deleted_at_utc IS NOT NULL OR m.kind <> 'signal' "
        "  OR (m.edit_date_utc IS NOT NULL AND m.edit_date_utc IS NOT s.reread_edit_date_utc) "
        "ORDER BY s.signal_id"
    ).fetchall():
        channel_cfg = on.get(signal["channel_id"])
        if channel_cfg is None:
            continue  # its channel is off: its entries were cancelled already
        if not outbox.state_counts(conn, kind="ENTRY", signal_id=signal["signal_id"]).get(
            "QUEUED"
        ):
            continue  # nothing of it waits to be traded
        own = (signal["channel_id"], signal["message_id"])
        how = _how_gone(conn, signal, channel_cfg)
        if how is None:
            # Its own post still stands: not re-read again until edited (R7-6).
            conn.execute(
                "UPDATE signals SET reread_edit_date_utc = ? WHERE signal_id = ?",
                (signal["own_edit_date_utc"], signal["signal_id"]),
            )
            continue
        if _call_stands_elsewhere(
            conn, signal_id=signal["signal_id"], post=own, channel_cfg=channel_cfg,
            read_channels=read,
        ):
            continue
        # Some post of the call still stands, then -- on a channel switched off.
        kept_by = next(
            _posts_showing_call(
                conn, signal_id=signal["signal_id"], post=own, channel_cfg=channel_cfg
            ),
            None,
        )
        posted = datetime.fromisoformat(signal["own_posted_at_utc"])
        old = (now_utc - posted).total_seconds() > max_age_seconds
        if old and signals_repo.trade_status(conn, signal_id=signal["signal_id"]) == "pending":
            # Too old to be traded anyway (R6-2): said in one line below.
            skipped = _withdraw_quietly(conn, signal, how=how, now_utc=now_utc)
            too_old.append((signal["signal_id"], skipped))
            continue
        raised.append(
            _withdraw_shown_only_off(
                conn, signal=signal, channel_cfg=channel_cfg, how=how, kept_by=kept_by,
                now_utc=now_utc,
            )
        )
    if too_old:
        raised.append(
            _say_old_shown_only_off(
                conn, too_old, max_age_seconds=max_age_seconds, now_utc=now_utc
            )
        )
    return raised


def _withdraw_quietly(
    conn: sqlite3.Connection, signal: sqlite3.Row, *, how: str, now_utc: datetime
) -> int:
    """`_withdraw_shown_only_off`'s withdrawal of `signal` -- a call too old
    to be traded, nothing of it executed -- with no alert of its own (R6-2):
    VOIDED and withdrawn, its own post the one that withdrew it, its waiting
    entries cancelled. Returns how many entries were cancelled."""
    signal_id = signal["signal_id"]
    signals_repo.mark_withdrawn(conn, signal_id=signal_id, now_utc=now_utc)
    signals_repo.record_closed_by(
        conn, signal_id=signal_id, channel_id=signal["channel_id"],
        message_id=signal["message_id"],
    )
    skipped = outbox.skip_queued(
        conn,
        kind="ENTRY",
        signal_id=signal_id,
        reason=f"signal withdrawn: its post {how}, and no post on a channel switched on shows it",
    )
    signals_repo.set_signal_state(conn, signal_id=signal_id, state="VOIDED")
    logger.info(
        "signal %s VOIDED: its own post, message %s on channel %s, %s, and no post on a "
        "channel switched on shows the call; it was posted too long ago to be traded anyway "
        "(%s queued trade entries cancelled)",
        signal_id,
        signal["message_id"],
        signal["channel_id"],
        how,
        skipped,
    )
    return skipped


def _say_old_shown_only_off(
    conn: sqlite3.Connection,
    too_old: list[tuple[int, int]],
    *,
    max_age_seconds: int,
    now_utc: datetime,
) -> int:
    """The one WARNING for `_withdraw_calls_shown_only_off` about the calls
    it withdrew that were too old to be traded anyway (R6-2): `too_old`
    holds each signal with the entries cancelled for it."""
    ids = [signal_id for signal_id, _ in too_old]
    entries = sum(skipped for _, skipped in too_old)
    shown = ", ".join(str(signal_id) for signal_id in ids[:10])
    if len(ids) > 10:
        shown += f" and {len(ids) - 10} more"
    one = len(ids) == 1
    detail = (
        (
            f"signal {shown} was waiting to be traded, but its own post"
            if one
            else f"{len(ids)} signals ({shown}) were waiting to be traded, but their own posts"
        )
        + " had been deleted, or edited so that they no longer show the call, and no post on a "
        "channel that is switched on shows the call any more (a post on a channel switched "
        "off now is no longer read). "
        + ("It was" if one else "They were")
        + f" posted more than {max_age_seconds} seconds ago, too old to be traded anyway. "
        + ("It is" if one else "They are")
        + f" VOIDED: {entries} queued trade entr{'y was' if entries == 1 else 'ies were'} "
        "cancelled. Nothing had been executed. Nothing to do."
    )
    logger.info("%s", detail)
    return alerts.record(
        conn,
        now_utc=now_utc,
        level="WARNING",
        code=OLD_SIGNALS_SHOWN_ONLY_ON_CHANNEL_OFF,
        signal_id=ids[0] if one else None,
        detail=detail,
    )


def _how_gone(
    conn: sqlite3.Connection, signal: sqlite3.Row, channel_cfg: _ChannelCfgLike
) -> str | None:
    """How `signal`'s own post no longer stands, in words -- or None while it
    stands: not deleted and, re-read with its channel's parser, still
    showing the call (`post_shows_call`). (A signal's own message is always
    stored: `signals` references it.)"""
    post = conn.execute(
        "SELECT * FROM messages WHERE channel_id = ? AND message_id = ?",
        (signal["channel_id"], signal["message_id"]),
    ).fetchone()
    if post["deleted_at_utc"] is not None:
        return "was deleted"
    if not post_shows_call(post, signal, channel_cfg=channel_cfg):
        return "was edited and no longer shows the call"
    return None


def _withdraw_shown_only_off(
    conn: sqlite3.Connection,
    *,
    signal: sqlite3.Row,
    channel_cfg: _ChannelCfgLike,
    how: str,
    kept_by: sqlite3.Row | None,
    now_utc: datetime,
) -> int:
    """`_withdraw_calls_shown_only_off`'s withdrawal of `signal`, whose own
    post `how`, kept standing only by post `kept_by` on a channel switched
    off (None: by none at all), as `edits_repo._withdraw_signal` withdraws a
    call; returns the alert's id."""
    signal_id = signal["signal_id"]
    status = signals_repo.trade_status(conn, signal_id=signal_id)
    signals_repo.mark_withdrawn(conn, signal_id=signal_id, now_utc=now_utc)
    signals_repo.record_closed_by(
        conn, signal_id=signal_id, channel_id=signal["channel_id"],
        message_id=signal["message_id"],
    )
    skipped = outbox.skip_queued(
        conn,
        kind="ENTRY",
        signal_id=signal_id,
        reason=f"signal withdrawn: its post {how}, and no post on a channel switched on shows it",
    )
    name = getattr(channel_cfg, "name", None)
    where = (
        f"its own post, message {signal['message_id']} on channel {signal['channel_id']}"
        + (f" ({name})" if name else "")
        + f", {how}, and "
    )
    if kept_by is not None:
        where += (
            f"the call stood only in message {kept_by['message_id']} on channel "
            f"{kept_by['channel_id']}, which is switched off now: this program no longer reads "
            "that channel, so it could not see that post withdrawn too. No post on a channel "
            "that is switched on shows the call any more"
        )
    else:
        where += "no other post on a channel that is switched on shows the call"
    entries = f"{skipped} queued trade entr{'y was' if skipped == 1 else 'ies were'} cancelled"
    if status == "pending":
        signals_repo.set_signal_state(conn, signal_id=signal_id, state="VOIDED")
        level: alerts.AlertLevel = "WARNING"
        detail = (
            f"signal {signal_id} VOIDED: {where}, so it is not traded: {entries}. Nothing had "
            "been executed. Nothing to do."
        )
    else:
        level = "CRITICAL"
        traded = "already traded" if status == "executed" else "being traded right now"
        detail = (
            f"signal {signal_id} is withdrawn: {where}. But it is {traded}: its open trades "
            f"were NOT closed -- review them by hand. {entries}."
        )
    logger.info("%s", detail)
    return alerts.record(
        conn,
        now_utc=now_utc,
        level=level,
        code=SIGNAL_SHOWN_ONLY_ON_CHANNEL_OFF,
        channel_id=signal["channel_id"],
        message_id=signal["message_id"],
        signal_id=signal_id,
        detail=detail,
    )


@dataclass(frozen=True, slots=True)
class _Copy:
    """For `admit_copies_of_switched_off`: a standing post of `signal`'s
    call on a channel switched on (`_standing_copies`), with its parse and
    its channel."""

    post: sqlite3.Row
    routed: RouteResult
    channel_cfg: _ChannelCfgLike
    #: The signal whose channel is switched off, that the post repeated.
    signal: sqlite3.Row

    @property
    def key(self) -> tuple[int, int]:
        return self.channel_cfg.channel_id, self.post["message_id"]


def _live_carrier(
    conn: sqlite3.Connection, copy: _Copy, *, relay_window_seconds: int
) -> sqlite3.Row | None:
    """The signal `copy` repeats now by the relay rules (`relay_of_signal`)
    when that signal can still trade it -- not a withdrawn call; None
    otherwise."""
    post = copy.post
    relay = relay_of_signal(
        conn,
        channel_id=copy.channel_cfg.channel_id,
        message_id=post["message_id"],
        trade_hash=copy.routed.signal.content_hash,
        posted_at_utc=datetime.fromisoformat(post["posted_at_utc"]),
        relay_window_seconds=relay_window_seconds,
        fwd_from_channel_id=post["fwd_from_channel_id"],
        fwd_from_post_id=post["fwd_from_post_id"],
        channel_cfg=copy.channel_cfg,
        signal=copy.routed.signal,
    )
    if relay.match is None or relay.match.withdrawn_unexecuted:
        return None
    return _signal_row(conn, relay.match.signal_id)


def _relink(
    conn: sqlite3.Connection,
    copy: _Copy,
    carrier: sqlite3.Row,
    *,
    why: str = "a copy of signal %s whose channel is switched off",
) -> None:
    """`copy` repeats `carrier` now: the link, and the follow-ups replying
    to it go with it (`_move_replies_to`). `why`: what the copy was, for the
    log (the signal it repeated fills its `%s`)."""
    channel_id, message_id = copy.key
    conn.execute(
        "UPDATE messages SET relay_of_signal_id = ? WHERE channel_id = ? AND message_id = ?",
        (carrier["signal_id"], channel_id, message_id),
    )
    _move_replies_to(conn, posts={copy.key}, signal=copy.signal, new=carrier)
    logger.info(
        "channel %s message %s, " + why + ", now repeats signal %s (channel %s message %s)",
        channel_id,
        message_id,
        copy.signal["signal_id"],
        carrier["signal_id"],
        carrier["channel_id"],
        carrier["message_id"],
    )


def _relink_held_back(
    conn: sqlite3.Connection,
    held: list[tuple[_Copy, tuple[int, ...]]],
    *,
    raised: list[int],
    relay_window_seconds: int,
    now_utc: datetime,
) -> list[tuple[_Copy, tuple[int, ...]]]:
    """For `admit_copies_of_switched_off`, right after it admitted a copy
    (final fix round 4, R4-3): each copy it held back -- `held`, with the
    alerts it raised for it -- is run through the relay rules again. One
    that repeats a call traded now (`_live_carrier`) is linked to it
    (`_relink`), and its `COPY_OF_WITHDRAWN_SIGNAL` alert is acknowledged
    and superseded by a WARNING, as `record_replacement` does: no "trade it
    by hand" may stand while the call is queued. The ids of the WARNINGs go
    to `raised`; returns the copies still held back."""
    kept: list[tuple[_Copy, tuple[int, ...]]] = []
    for copy, alert_ids in held:
        carrier = _live_carrier(conn, copy, relay_window_seconds=relay_window_seconds)
        if carrier is None:
            kept.append((copy, alert_ids))
            continue
        _relink(conn, copy, carrier)
        queued = _queued_entries(conn, carrier["signal_id"])
        raised.extend(
            _supersede_all(
                conn,
                alert_ids,
                new=carrier,
                stands=(
                    "The same call stands on a channel that is switched on: message "
                    f"{carrier['message_id']} on channel {carrier['channel_id']} is signal "
                    f"{carrier['signal_id']}, with {queued} trade "
                    f"entr{'y' if queued == 1 else 'ies'} queued, and the copy now repeats it."
                ),
                now_utc=now_utc,
            )
        )
    return kept


def _supersede_all(
    conn: sqlite3.Connection,
    alert_ids: tuple[int, ...],
    *,
    new: sqlite3.Row,
    stands: str,
    now_utc: datetime,
) -> list[int]:
    """`_supersede` each `COPY_OF_WITHDRAWN_SIGNAL` alert of `alert_ids`,
    oldest first: its copy repeats signal `new` now. Returns the ids of the
    WARNINGs."""
    marks = ", ".join("?" for _ in alert_ids)
    return [
        _supersede(conn, alert=alert, new=new, stands=stands, now_utc=now_utc)
        for alert in conn.execute(
            "SELECT alert_id, channel_id, message_id, acked FROM alerts "
            f"WHERE code = ? AND alert_id IN ({marks}) ORDER BY alert_id",
            (COPY_OF_WITHDRAWN_SIGNAL, *alert_ids),
        ).fetchall()
    ]


def _with_held_back_near(
    conn: sqlite3.Connection,
    held: list[tuple[_Copy, tuple[int, ...]]],
    new: sqlite3.Row,
    *,
    on: dict[int, _ChannelCfgLike],
    relay_window_seconds: int,
) -> list[tuple[_Copy, tuple[int, ...]]]:
    """For `admit_copies_of_switched_off`, right after it admitted signal
    `new` (final fix round 5, R5-4): the copies it held back (`held`), and
    every other copy held back -- when it was read -- within the relay window
    of `new`'s post (`_held_back_near`), each once, with all its alerts."""
    merged = {copy.key: (copy, alert_ids) for copy, alert_ids in held}
    for copy, alert_ids in _held_back_near(
        conn, new, relay_window_seconds=relay_window_seconds, read_as=on.get
    ):
        if copy.key in merged:
            kept, ids = merged[copy.key]
            merged[copy.key] = (kept, tuple(dict.fromkeys((*ids, *alert_ids))))
        else:
            merged[copy.key] = (copy, alert_ids)
    return list(merged.values())


def _queued_entries(conn: sqlite3.Connection, signal_id: int) -> int:
    (queued,) = conn.execute(
        "SELECT COUNT(*) FROM outbox WHERE kind = 'ENTRY' AND signal_id = ? AND state = 'QUEUED'",
        (signal_id,),
    ).fetchone()
    return int(queued)


def _supersede(
    conn: sqlite3.Connection,
    *,
    alert: sqlite3.Row,
    new: sqlite3.Row,
    stands: str,
    now_utc: datetime,
) -> int:
    """A held-back copy's `COPY_OF_WITHDRAWN_SIGNAL` `alert` (its id,
    channel, message and whether it is acknowledged) no longer applies: the
    copy repeats signal `new` now, which `stands` says. Acknowledge it and
    raise the WARNING `COPY_OF_WITHDRAWN_SIGNAL_SUPERSEDED` naming both
    (`record_replacement`, `_relink_held_back`). Returns its id."""
    if not alert["acked"]:
        alerts.ack(conn, alert_id=alert["alert_id"])
    return alerts.record(
        conn,
        now_utc=now_utc,
        level="WARNING",
        code=COPY_OF_WITHDRAWN_SIGNAL_SUPERSEDED,
        channel_id=alert["channel_id"],
        message_id=alert["message_id"],
        signal_id=new["signal_id"],
        detail=(
            f"alert #{alert['alert_id']} no longer applies. It said that message "
            f"{alert['message_id']} on channel {alert['channel_id']} copies a call that "
            "had been withdrawn, was not traded, and that the call should be traded by "
            f"hand if it was still meant. {stands} Alert #{alert['alert_id']} is cleared. "
            "Do not trade this call by hand as well: that would open the trade twice. If "
            "you already did, check your open positions."
        ),
    )


def _adopt_followup_copies(
    conn: sqlite3.Connection,
    *,
    new: sqlite3.Row,
    admitted_from: Collection[int],
    old_chains: set[int],
    on: dict[int, _ChannelCfgLike],
    accounts: tuple,
    now_utc: datetime,
    relay_window_seconds: int,
) -> None:
    """For `admit_copies_of_switched_off`, at the end of its job (final fix
    round 4, R4-4): every follow-up stored as a copy of another
    (`messages.relay_of_followup_id`) on a channel switched on (`on`), not
    deleted, still reading as a follow-up, and

    - replying to a post that acts on signal `new` -- its own message, or a
      post stored as its copy -- while the follow-up it repeats targets a
      signal of `old_chains` (the switched-off signals' chains that the job
      admitted a copy of); or
    - a forward of the very post of the follow-up it repeats, while that
      follow-up targets a signal of `admitted_from` -- the chain of the
      switched-off signal `new` was admitted in place of (final fix round
      5, R5-2): the VIP forwarding the relay's "close half", which as a
      forward replies to nothing

    was stored as a copy of an instruction about the call that is cancelled
    now, and is about `new`. Repeating a follow-up about `new` already --
    another copy of the same instruction adopted first, or another
    channel's, matched by the follow-up relay rules (`find_followup_relay`)
    -- it is linked to that one: one instruction, never two. Else it is a
    follow-up of its own targeting `new` (path `'reply'` when it replies to
    `new`'s own message, `'relay-reply'` otherwise), its FOLLOWUP rows
    fanned out.

    And a follow-up of its own on a channel switched on whose target came
    from its copy (`'copy-reply'`: a forward, whose original -- stored
    second -- replied to the call) and is a signal of `admitted_from` acts
    on `new` now (R5-2): the VIP's forward stored before the relay's post it
    forwards. Every order of the posts ends the same: the instruction acts
    on the call it was given about."""
    posts = {(new["channel_id"], new["message_id"])} | {
        (row["channel_id"], row["message_id"])
        for row in conn.execute(
            "SELECT channel_id, message_id FROM messages WHERE relay_of_signal_id = ?",
            (new["signal_id"],),
        ).fetchall()
    }
    for copy in conn.execute(
        "SELECT m.*, f.followup_id AS held_id, f.target_signal_id AS held_target, "
        "  f.channel_id AS held_channel_id, f.message_id AS held_message_id "
        "FROM messages m JOIN followups f ON f.followup_id = m.relay_of_followup_id "
        "WHERE m.deleted_at_utc IS NULL "
        "ORDER BY m.posted_at_utc, m.channel_id, m.message_id"
    ).fetchall():
        channel_cfg = on.get(copy["channel_id"])
        if channel_cfg is None:
            continue
        replies = (copy["channel_id"], copy["reply_to_message_id"]) in posts
        forwards = (copy["fwd_from_channel_id"], copy["fwd_from_post_id"]) == (
            copy["held_channel_id"],
            copy["held_message_id"],
        )
        if not (
            (replies and copy["held_target"] in old_chains)
            or (forwards and copy["held_target"] in admitted_from)
        ):
            # The instruction it repeats is not about the call cancelled in
            # `new`'s place -- one about `new` already, moved with its post,
            # or about another call: it stays a copy of it.
            continue
        if _acts_no_more(conn, copy["held_id"]):
            # Its poster withdrew the instruction it repeats: with both
            # channels read nothing acts on it, so it stays a copy of it
            # (final fix round 8).
            continue
        posted = datetime.fromisoformat(copy["posted_at_utc"])
        routed = route(
            copy["raw_text"],
            channel_cfg=channel_cfg,
            message_id=copy["message_id"],
            posted_at_utc=posted,
            reply_to_message_id=copy["reply_to_message_id"],
        )
        if routed.kind != "followup":
            continue
        followup = routed.followup
        key = (copy["channel_id"], copy["message_id"])
        # The follow-up adopted already for the same held one -- by this
        # job or an earlier one (R6-1): one held instruction, one about `new`.
        match = _stand_in(conn, held_id=copy["held_id"], signal_id=new["signal_id"])
        if match is None:
            # A forward matches the post it forwards -- the follow-up held --
            # so only by its words is it found repeating one about `new`.
            match = find_followup_relay(
                conn,
                channel_id=copy["channel_id"],
                message_id=copy["message_id"],
                op=followup.op.value,
                content_hash=copy["content_hash"],
                target_signal_id=new["signal_id"],
                posted_at_utc=posted,
                relay_window_seconds=relay_window_seconds,
                fwd_from_channel_id=None if forwards else copy["fwd_from_channel_id"],
                fwd_from_post_id=None if forwards else copy["fwd_from_post_id"],
            )
        if match is not None and match.followup_id != copy["held_id"]:
            conn.execute(
                "UPDATE messages SET relay_of_followup_id = ? "
                "WHERE channel_id = ? AND message_id = ?",
                (match.followup_id, *key),
            )
            logger.info(
                "channel %s message %s, a copy of follow-up %s about a signal whose channel is "
                "switched off, now repeats follow-up %s (channel %s message %s) about signal %s",
                *key,
                copy["held_id"],
                match.followup_id,
                match.channel_id,
                match.message_id,
                new["signal_id"],
            )
            continue
        conn.execute(
            "UPDATE messages SET relay_of_followup_id = NULL WHERE channel_id = ? AND message_id = ?",
            key,
        )
        own = (copy["channel_id"], copy["reply_to_message_id"]) == (
            new["channel_id"],
            new["message_id"],
        )
        followup_id = signals_repo.insert_followup(
            conn,
            channel_id=copy["channel_id"],
            message_id=copy["message_id"],
            followup=followup,
            target_signal_id=new["signal_id"],
            resolution_path=REPLY_PATH if own else RELAY_REPLY_PATH,
            instead_of_followup_id=copy["held_id"],
        )
        outbox.fan_out_checked(
            conn,
            kind="FOLLOWUP",
            signal_id=None,
            followup_id=followup_id,
            accounts=accounts,
            now_utc=now_utc,
        )
        logger.info(
            "channel %s message %s, a copy of follow-up %s about a signal whose channel is "
            "switched off, is follow-up %s now, about signal %s",
            *key,
            copy["held_id"],
            followup_id,
            new["signal_id"],
        )
    marks = ", ".join("?" for _ in admitted_from)
    for held in conn.execute(
        "SELECT followup_id, channel_id, message_id FROM followups "
        f"WHERE edit_seq = 0 AND resolution_path = ? AND target_signal_id IN ({marks}) "
        "ORDER BY followup_id",
        (COPY_REPLY_PATH, *admitted_from),
    ).fetchall():
        if held["channel_id"] not in on:
            continue  # on a channel switched off too: about the cancelled call
        same = _same_instruction_about(
            conn, followup_id=held["followup_id"], signal_id=new["signal_id"],
            window=relay_window_seconds,
        )
        if same is not None:
            # Another follow-up about `new` gives the same instruction -- a
            # reply to its post, or a copy adopted above (R6-1): that one acts
            # on `new` in this one's place, and this one stays with the
            # cancelled call. Never two.
            conn.execute(
                "UPDATE followups SET instead_of_followup_id = ? "
                "WHERE followup_id = ? AND instead_of_followup_id IS NULL",
                (held["followup_id"], same),
            )
            logger.info(
                "follow-up %s (channel %s message %s), whose copy replied to a signal whose "
                "channel is switched off, stays with it: follow-up %s gives the same "
                "instruction about signal %s",
                held["followup_id"],
                held["channel_id"],
                held["message_id"],
                same,
                new["signal_id"],
            )
            continue
        conn.execute(
            "UPDATE followups SET target_signal_id = ? WHERE followup_id = ?",
            (new["signal_id"], held["followup_id"]),
        )
        logger.info(
            "follow-up %s (channel %s message %s), whose copy replied to a signal whose "
            "channel is switched off, is about signal %s now",
            held["followup_id"],
            held["channel_id"],
            held["message_id"],
            new["signal_id"],
        )


def _say_too_old(
    conn: sqlite3.Connection, too_old: dict[int, int], *, max_age_seconds: int, now_utc: datetime
) -> int:
    """The one WARNING for `admit_copies_of_switched_off` about the posts it
    left as they were because they are too old to be traded (R4-2):
    `too_old` counts them per signal they repeat."""
    posts = sum(too_old.values())
    ids = sorted(too_old)
    shown = ", ".join(str(signal_id) for signal_id in ids[:10])
    if len(ids) > 10:
        shown += f" and {len(ids) - 10} more"
    one_post, one_signal = posts == 1, len(ids) == 1
    detail = (
        (
            "1 post on a channel that is switched on repeats"
            if one_post
            else f"{posts} posts on channels that are switched on repeat"
        )
        + (
            f" the call of a signal whose channel is now switched off (signal {shown}). That "
            "signal's"
            if one_signal
            else f" the calls of signals whose channel is now switched off (signals {shown}). "
            "Those signals'"
        )
        + " waiting trade entries were cancelled, and "
        + ("this post is not traded instead: it was" if one_post else
           "these posts are not traded instead: each was")
        + f" posted more than {max_age_seconds} seconds ago, too old to be traded. "
        "Nothing to do."
    )
    logger.info("%s", detail)
    return alerts.record(
        conn,
        now_utc=now_utc,
        level="WARNING",
        code=OLD_COPIES_OF_CHANNEL_OFF_SIGNALS,
        detail=detail,
    )


def _standing_copies(
    conn: sqlite3.Connection, signal: sqlite3.Row, on: dict[int, _ChannelCfgLike]
) -> list[tuple[sqlite3.Row, RouteResult, _ChannelCfgLike]]:
    """For `admit_copies_of_switched_off`: the posts of `signal`'s call on
    the channels switched on (`on`) that still stand -- not deleted, not
    the post that closed one of its signals (`standing_posts_on`), still
    showing the call read with their own channel's parser (`read_post`),
    and holding no signal of their own (one post, one signal) -- each with
    its parse and its channel. Newest post first."""
    found = []
    for channel_id, channel_cfg in on.items():
        for post in standing_posts_on(conn, signal_id=signal["signal_id"], channel_id=channel_id):
            own = signals_repo.get_signal_id(
                conn, channel_id=channel_id, message_id=post["message_id"]
            )
            if own is not None:
                continue
            routed = read_post(post, channel_cfg=channel_cfg)
            if routed.kind == "signal" and shows_call(routed.signal, signal):
                found.append((post, routed, channel_cfg))
    found.sort(
        key=lambda item: (
            datetime.fromisoformat(item[0]["posted_at_utc"]),
            item[2].channel_id,
            item[0]["message_id"],
        ),
        reverse=True,
    )
    return found


def _move_replies_to(
    conn: sqlite3.Connection,
    *,
    posts: set[tuple[int, int]],
    signal: sqlite3.Row,
    new: sqlite3.Row,
) -> None:
    """Every follow-up replying to one of `posts` -- posts of `signal`'s
    call that hold signal `new` now, or repeat it -- and targeting one of
    the call's signals targets `new` (path `'reply'` when it replies to
    `new`'s own message, `'relay-reply'` for any other post), as
    `_move_followups` moves them: the instruction is about the call that
    post shows, which `new` carries now."""
    chain = chain_ids(conn, signal["signal_id"])
    marks = ", ".join("?" for _ in chain)
    for followup in conn.execute(
        "SELECT followup_id, channel_id, reply_to_message_id, resolution_path FROM followups "
        f"WHERE edit_seq = 0 AND target_signal_id IN ({marks}) ORDER BY followup_id",
        chain,
    ).fetchall():
        replied = (followup["channel_id"], followup["reply_to_message_id"])
        if replied not in posts:
            continue
        path = followup["resolution_path"]
        if path in (REPLY_PATH, RELAY_REPLY_PATH):
            own = replied == (new["channel_id"], new["message_id"])
            path = REPLY_PATH if own else RELAY_REPLY_PATH
        conn.execute(
            "UPDATE followups SET target_signal_id = ?, resolution_path = ? WHERE followup_id = ?",
            (new["signal_id"], path, followup["followup_id"]),
        )


def _say_admitted(
    conn: sqlite3.Connection,
    *,
    signal: sqlite3.Row,
    new: sqlite3.Row,
    channel_cfg: _ChannelCfgLike,
    queued: int,
    now_utc: datetime,
) -> int:
    """The WARNING for `admit_copies_of_switched_off`: post `new` was a copy
    of `signal`, whose channel is switched off; it is signal `new` now."""
    return alerts.record(
        conn,
        now_utc=now_utc,
        level="WARNING",
        code=COPY_OF_CHANNEL_OFF_SIGNAL_ADMITTED,
        channel_id=new["channel_id"],
        message_id=new["message_id"],
        signal_id=new["signal_id"],
        detail=(
            f"message {new['message_id']} on channel {new['channel_id']} "
            f"({channel_cfg.name}) repeats the call of signal {signal['signal_id']} "
            f"(message {signal['message_id']} on channel {signal['channel_id']}). That "
            f"channel is switched off, so signal {signal['signal_id']}'s waiting trade "
            "entries were cancelled and it is not traded. The same call still stands here: "
            f"message {new['message_id']} is now signal {new['signal_id']}, and {queued} "
            f"trade entr{'y is' if queued == 1 else 'ies are'} queued for it. Nothing to do."
        ),
    )


def _withdrawn_in_report(
    conn: sqlite3.Connection,
    signal_id: int,
    channel_id: int,
    reported_at_utc: datetime,
    report_ids: Collection[int],
) -> bool:
    """For `admit_signal` (final fix FF5): whether signal `signal_id` was
    withdrawn by the deletion of a post of `channel_id` that the report
    made at `reported_at_utc` naming `report_ids` deleted -- every job of
    one report is stamped with the report's moment
    (`Listener.ingest_deletion`)."""
    row = _signal_row(conn, signal_id)
    if row["closed_by_channel_id"] != channel_id or row["closed_by_message_id"] not in set(
        report_ids
    ):
        return False
    closer = conn.execute(
        "SELECT deleted_at_utc FROM messages WHERE channel_id = ? AND message_id = ?",
        (channel_id, row["closed_by_message_id"]),
    ).fetchone()
    return closer is not None and closer["deleted_at_utc"] == reported_at_utc.isoformat()


def _record_copy_withdrawn_with(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    message_id: int,
    match: SignalMatch,
    now_utc: datetime,
) -> None:
    """Store that (`channel_id`, `message_id`) -- already on `messages`,
    deleted in the report that withdrew `match`'s signal -- is a copy of
    it: the link, the INFO line and the counter, and no alert. The report
    withdrew the call once, and said so; this post went with it."""
    conn.execute(
        "UPDATE messages SET relay_of_signal_id = ? WHERE channel_id = ? AND message_id = ?",
        (match.signal_id, channel_id, message_id),
    )
    metrics.bump(conn, day_key=metrics.day_key_for(now_utc), name=RELAY_DUPLICATES_METRIC)
    logger.info(
        "channel %s message %s repeats signal %s (message %s), and was deleted in the same "
        "report as the post that withdrew it: stored as its copy, not traded",
        channel_id,
        message_id,
        match.signal_id,
        match.message_id,
    )


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
    (copy_text,) = conn.execute(
        "SELECT raw_text FROM messages WHERE channel_id = ? AND message_id = ?",
        (channel_id, message_id),
    ).fetchone()
    cancelled = store_words.retraction_words(copy_text)
    if cancelled:
        # The copy carries the withdrawal itself -- a forward or a copy of
        # the post after "CANCELLED" was added to it: there is nothing to
        # trade by hand (final fix FF7).
        advice = (
            f"This copy itself says {store_words.said(cancelled)}: nothing to do, unless the "
            "poster still means the call."
        )
    else:
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


@dataclass(frozen=True, slots=True)
class ChannelOffFollowup:
    """`followup_after_channel_off`'s decision for a follow-up read after
    watch started (final fix round 6, R6-1)."""

    #: Store it as a copy of this follow-up -- the held one it repeats, or
    #: the one standing in for it about the call traded instead -- or None:
    #: it is an instruction of its own about the signal it replies to.
    copy_of: FollowupMatch | None
    #: With `copy_of` None: the held follow-up it stands in for
    #: (`followups.instead_of_followup_id`).
    instead_of: int | None = None


def followup_after_channel_off(
    conn: sqlite3.Connection,
    *,
    channel_cfg: _ChannelCfgLike,
    message_id: int,
    followup: FollowUp,
    content_hash: str,
    target_signal_id: int | None,
    posted_at_utc: datetime,
    relay_window_seconds: int,
    match: FollowupMatch | None,
    now_utc: datetime,
) -> ChannelOffFollowup | None:
    """For `messages_repo.ingest()`: a follow-up read after watch started
    that repeats an instruction about a signal whose channel is switched off
    (final fix round 6, R6-1; the rule: a WRONG action is worse than a
    missed one). None when it does not -- it is stored as ever.

    It repeats one when the follow-up relay rules matched it to a held
    follow-up (`match`: the post it forwards, or the same words about the
    same signal) that acts on a signal whose channel is switched off
    (`switched_off`) -- or, replying to a post of a signal the start-up job
    admitted in place of such a one (`signals.admitted_from_signal_id`),
    when it gives the same instruction (op and words) as a follow-up about
    either, posted on another channel within the relay window
    (`_repeated_about_admitted`): what the relay rules would have matched
    while both channels were read. Then:

    - another follow-up stands in for that one about the signal traded
      instead (`followups.instead_of_followup_id`, `_stand_in`): it is a
      copy of it -- one held instruction, one instruction about that call;
      and one acting on the signal it replies to already is repeated the
      same way, quietly;
    - else, replying to a post of the signal traded instead, and no copy of
      the same held instruction was handed to the user already
      (`_reported`): it is an instruction of its own about the signal it
      replies to, standing in for the held one;
    - else -- a forward replies to nothing, so only a guess would tie it to
      the call traded instead -- it is stored as a copy of the held one and
      NOT applied, with ONE WARNING (`FOLLOWUP_ABOUT_CHANNEL_OFF_SIGNAL`)
      naming the message, the instruction and the switched-off call: if the
      start-up job admitted a signal in place of that one, "if the
      instruction was meant for signal N, apply it by hand" (once, when
      another copy was handed over already); else it names no other signal.
      No carrier is ever searched for by time: the same call posted on a
      channel switched on later is another call.

    Every copy of a held instruction about a switched-off call raises that
    WARNING -- with "Nothing to do" when another one acts in its place, or
    saying that one was withdrawn since, when it was."""
    if match is not None:
        held = match
    elif target_signal_id is not None:
        found = _repeated_about_admitted(
            conn,
            signal_id=target_signal_id,
            op=followup.op.value,
            content_hash=content_hash,
            channel_id=channel_cfg.channel_id,
            posted_at_utc=posted_at_utc,
            window=relay_window_seconds,
        )
        if found is None:
            return None
        held, about_target = found
        if about_target:
            # The same instruction acts on the signal it replies to already.
            return ChannelOffFollowup(copy_of=held)
    else:
        return None
    about = signal_for_followup(conn, followup_id=held.followup_id)
    if about is None:
        return None
    about_row = _signal_row(conn, about)
    if not switched_off(conn, about_row):
        return None
    admitted = admitted_for(conn, about)
    stand_in = None if admitted is None else _stand_in(
        conn, held_id=held.followup_id, signal_id=admitted
    )
    reported = _reported(conn, held_id=held.followup_id)
    # A reply matched only by `_repeated_about_admitted` replies to a post of
    # the call `admitted` carries (one admission per switched-off call).
    if (
        stand_in is None
        and match is None
        and admitted is not None
        and reported is None
        and not _acts_no_more(conn, held.followup_id)
    ):
        logger.info(
            "channel %s message %s replies to signal %s, admitted in place of signal %s whose "
            "channel is switched off, with the instruction of follow-up %s: it is that "
            "instruction about signal %s",
            channel_cfg.channel_id,
            message_id,
            admitted,
            about,
            held.followup_id,
            admitted,
        )
        return ChannelOffFollowup(copy_of=None, instead_of=held.followup_id)
    _say_about_channel_off(
        conn,
        channel_cfg=channel_cfg,
        message_id=message_id,
        followup=followup,
        held=held,
        about=about_row,
        admitted=admitted,
        stand_in=stand_in,
        reported=reported,
        now_utc=now_utc,
    )
    return ChannelOffFollowup(copy_of=stand_in if stand_in is not None else held)


def admitted_for(conn: sqlite3.Connection, signal_id: int) -> int | None:
    """The signal watch's start admitted in place of `signal_id`'s call
    (`signals.admitted_from_signal_id`, final fix round 6, R6-1) -- the
    newest signal of the call since (`live_signal_for`: the admission is a
    link of its chain, final fix round 7, R7-1) -- while it carries that
    call (`carries`); else None. Never a signal found by time."""
    segments = _segments(conn, signal_id)
    if signal_id in segments[-1]:
        return None  # no admission after it
    live = segments[-1][-1]
    return live if carries(conn, _signal_row(conn, live)) else None


def _stand_in(conn: sqlite3.Connection, *, held_id: int, signal_id: int) -> FollowupMatch | None:
    """The follow-up standing in for held follow-up `held_id` about signal
    `signal_id` (`followups.instead_of_followup_id`; R6-1), or None."""
    for row in conn.execute(
        "SELECT followup_id, channel_id, message_id FROM followups "
        "WHERE edit_seq = 0 AND instead_of_followup_id = ? ORDER BY followup_id",
        (held_id,),
    ).fetchall():
        if signal_for_followup(conn, followup_id=row["followup_id"]) == signal_id:
            return FollowupMatch(row["followup_id"], row["channel_id"], row["message_id"], "hash")
    return None


@dataclass(frozen=True, slots=True)
class _Reported:
    alert_id: int
    channel_id: int
    message_id: int
    #: The signal the alert names (`handed_over` only).
    signal_id: int | None = None


def _reported(
    conn: sqlite3.Connection, *, held_id: int, also: tuple[int, ...] = ()
) -> _Reported | None:
    """The first FOLLOWUP_ABOUT_CHANNEL_OFF_SIGNAL WARNING raised for a copy
    of held follow-up `held_id` -- the instruction handed to the user (R6-1)
    -- or of any follow-up of `also` (the one standing in for it, final fix
    round 7, R7-3); or None."""
    ids = (held_id, *also)
    marks = ", ".join("?" for _ in ids)
    row = conn.execute(
        "SELECT a.alert_id, a.channel_id, a.message_id FROM alerts a "
        "JOIN messages m ON m.channel_id = a.channel_id AND m.message_id = a.message_id "
        f"WHERE a.code = ? AND m.relay_of_followup_id IN ({marks}) ORDER BY a.alert_id LIMIT 1",
        (FOLLOWUP_ABOUT_CHANNEL_OFF_SIGNAL, *ids),
    ).fetchone()
    return None if row is None else _Reported(row[0], row[1], row[2])


def _carried_near(
    conn: sqlite3.Connection,
    followup_id: int,
    *,
    other_than: int,
    posted_at_utc: datetime,
    window: int,
) -> bool:
    """Whether a post carrying follow-up `followup_id` -- its own, or one
    stored as a copy of it -- on a channel other than `other_than` was
    posted within `window` seconds of `posted_at_utc` (R6-1)."""
    limit = timedelta(seconds=window)
    for row in conn.execute(
        "SELECT m.channel_id, m.posted_at_utc FROM messages m JOIN followups f "
        "ON f.followup_id = ? WHERE (m.channel_id = f.channel_id AND m.message_id = f.message_id) "
        "  OR m.relay_of_followup_id = f.followup_id",
        (followup_id,),
    ).fetchall():
        if row[0] != other_than and abs(datetime.fromisoformat(row[1]) - posted_at_utc) <= limit:
            return True
    return False


def _repeated_about_admitted(
    conn: sqlite3.Connection,
    *,
    signal_id: int,
    op: str,
    content_hash: str,
    channel_id: int,
    posted_at_utc: datetime,
    window: int,
) -> tuple[FollowupMatch, bool] | None:
    """For a follow-up on `channel_id` replying to a post of signal
    `signal_id` that the relay rules matched to nothing: when that signal --
    or one of its call -- was admitted at watch's start in place of a
    switched-off one (R6-1), the follow-up about either call it repeats, as
    the relay rules would have matched it while both channels were read: the
    same op and words, one of its posts (its own, or a copy of it) on
    another channel within `window` seconds. With whether it is about
    `signal_id`'s call already (True) or the switched-off one's (False).
    None otherwise."""
    # The signal's own part of the call, and the switched-off one's that
    # its admission named (R7-1, R8-1).
    chain = _segment_of(conn, signal_id)
    switched = _switched_off_part(conn, chain)
    if switched is None:
        return None  # no admission in its part of the call
    both = [*chain, *switched]
    marks = ", ".join("?" for _ in both)
    candidates = conn.execute(
        "SELECT f.followup_id, f.channel_id, f.message_id, f.target_signal_id FROM followups f "
        "JOIN messages m ON m.channel_id = f.channel_id AND m.message_id = f.message_id "
        f"WHERE f.edit_seq = 0 AND f.op = ? AND m.content_hash = ? AND f.target_signal_id IN ({marks}) "
        "ORDER BY f.followup_id",
        (op, content_hash, *both),
    ).fetchall()
    for candidate in candidates:
        if _carried_near(
            conn, candidate["followup_id"], other_than=channel_id, posted_at_utc=posted_at_utc,
            window=window,
        ):
            return (
                FollowupMatch(
                    candidate["followup_id"], candidate["channel_id"], candidate["message_id"],
                    "hash",
                ),
                candidate["target_signal_id"] in chain,
            )
    return None


def _same_instruction_about(
    conn: sqlite3.Connection, *, followup_id: int, signal_id: int, window: int
) -> int | None:
    """For the start-up job (R6-1): another follow-up about `signal_id` that
    gives follow-up `followup_id`'s instruction -- standing in for it
    already, or the same op and words with a post of `followup_id`'s (its
    own, or a copy) on another channel within `window` seconds of its own:
    the relay rules would have taken one for a copy of the other. None
    otherwise."""
    row = conn.execute(
        "SELECT f.op, m.content_hash FROM followups f "
        "JOIN messages m ON m.channel_id = f.channel_id AND m.message_id = f.message_id "
        "WHERE f.followup_id = ?",
        (followup_id,),
    ).fetchone()
    for other in conn.execute(
        "SELECT f.followup_id, f.channel_id, f.instead_of_followup_id, m.posted_at_utc "
        "FROM followups f "
        "JOIN messages m ON m.channel_id = f.channel_id AND m.message_id = f.message_id "
        "WHERE f.edit_seq = 0 AND f.target_signal_id = ? AND f.followup_id <> ? "
        "  AND f.op = ? AND m.content_hash = ? ORDER BY f.followup_id",
        (signal_id, followup_id, row["op"], row["content_hash"]),
    ).fetchall():
        if other["instead_of_followup_id"] == followup_id or _carried_near(
            conn,
            followup_id,
            other_than=other["channel_id"],
            posted_at_utc=datetime.fromisoformat(other["posted_at_utc"]),
            window=window,
        ):
            return other["followup_id"]
    return None


def _say_about_channel_off(
    conn: sqlite3.Connection,
    *,
    channel_cfg: _ChannelCfgLike,
    message_id: int,
    followup: FollowUp,
    held: FollowupMatch,
    about: sqlite3.Row,
    admitted: int | None,
    stand_in: FollowupMatch | None,
    reported: _Reported | None,
    now_utc: datetime,
) -> int:
    """The one WARNING for `followup_after_channel_off`: message
    `message_id` repeats `held`'s instruction about signal `about`, whose
    channel is switched off, and was not applied (R6-1)."""
    name = getattr(channel_cfg, "name", None)
    signal_id = about["signal_id"]
    detail = (
        f"message {message_id} on channel {channel_cfg.channel_id}"
        + (f" ({name})" if name else "")
        + f" repeats the instruction '{store_words.instruction_words(followup)}' of message "
        f"{held.message_id} on channel {held.channel_id}, about signal {signal_id} (message "
        f"{about['message_id']} on channel {about['channel_id']}), whose channel is switched "
        f"off: signal {signal_id} is not traded, and this message was NOT applied."
    )
    counts = (
        {} if stand_in is None
        else outbox.state_counts(conn, kind="FOLLOWUP", followup_id=stand_in.followup_id)
    )
    withdrawn = withdrawn_by_poster(conn, held.followup_id)
    if withdrawn is not None:
        # Its poster withdrew it -- before the switch-off, or since: with both
        # channels read the message is a copy of an instruction withdrawn
        # (final fix round 8, R8-4). Never "apply it by hand".
        detail += (
            f" Message {held.message_id} was withdrawn by its poster (alert #{withdrawn}). "
            "Nothing to do."
        )
    elif stand_in is not None and set(counts) == {"SKIPPED"}:
        # The one about the call traded instead was withdrawn since -- its
        # post deleted or edited away: nothing acts on it now. A copy of it,
        # or of the held one, reported already is the same instruction: by
        # hand once, never once for each message (R7-3).
        first = _reported(conn, held_id=held.followup_id, also=(stand_in.followup_id,))
        if first is not None:
            detail += (
                f" It is the same instruction as message {first.message_id} on channel "
                f"{first.channel_id} (alert #{first.alert_id})."
            )
        detail += (
            f" Signal {admitted} trades that call instead; the same instruction was given "
            f"about it as follow-up {stand_in.followup_id} (message {stand_in.message_id} on "
            f"channel {stand_in.channel_id}), which was withdrawn since. If the instruction is "
            f"still meant for signal {admitted}, {BY_HAND}"
            + (" -- once, not once for each message." if first is not None else ".")
        )
    elif stand_in is not None:
        detail += (
            f" Signal {admitted} trades that call instead, and the same instruction already "
            f"acts on it as follow-up {stand_in.followup_id} (message {stand_in.message_id} on "
            f"channel {stand_in.channel_id}). Nothing to do."
        )
    else:
        if reported is not None:
            detail += (
                f" It is the same instruction as message {reported.message_id} on channel "
                f"{reported.channel_id} (alert #{reported.alert_id})."
            )
        if admitted is not None:
            detail += (
                f" Signal {admitted} trades that call instead: if the instruction was meant "
                f"for signal {admitted}, {BY_HAND}"
                + (" -- once, not once for each message." if reported is not None else ".")
            )
        else:
            detail += " No other signal is known to trade that call."
    return alerts.record(
        conn,
        now_utc=now_utc,
        level="WARNING",
        code=FOLLOWUP_ABOUT_CHANNEL_OFF_SIGNAL,
        channel_id=channel_cfg.channel_id,
        message_id=message_id,
        signal_id=admitted if admitted is not None and withdrawn is None else signal_id,
        detail=detail,
    )


def withdrawn_by_poster(conn: sqlite3.Connection, followup_id: int) -> int | None:
    """The alert that said follow-up `followup_id` was withdrawn by its
    poster -- its own post deleted or edited into no instruction, every
    instruction of it skipped by that (`edits_repo`'s `FOLLOWUP_DELETED` or
    `FOLLOWUP_RETRACTED_BY_EDIT`), none waiting since (`_acts_no_more`: an
    edit back into an instruction queues nothing) -- or None (final fix
    round 8, R8-4)."""
    from xaucopier.store import edits_repo  # edits_repo imports this module

    post = conn.execute(
        "SELECT channel_id, message_id FROM followups WHERE followup_id = ?", (followup_id,)
    ).fetchone()
    if post is None or not _acts_no_more(conn, followup_id):
        return None
    row = conn.execute(
        "SELECT alert_id FROM alerts WHERE code IN (?, ?) AND channel_id = ? AND message_id = ? "
        "ORDER BY alert_id LIMIT 1",
        (
            edits_repo.FOLLOWUP_DELETED,
            edits_repo.FOLLOWUP_RETRACTED_BY_EDIT,
            post["channel_id"],
            post["message_id"],
        ),
    ).fetchone()
    return None if row is None else row[0]


def _acts_no_more(conn: sqlite3.Connection, followup_id: int) -> bool:
    """Whether follow-up `followup_id` gives no instruction any more: every
    one of its instructions skipped -- withdrawn by its poster, or with the
    one it stood in for -- none waiting or taken. Nothing stands in for
    such a one (final fix round 8): with both channels read a repeat of it
    is a copy of an instruction withdrawn, which acts on nothing."""
    return set(outbox.state_counts(conn, kind="FOLLOWUP", followup_id=followup_id)) == {"SKIPPED"}


def stand_ins_of(conn: sqlite3.Connection, followup_id: int) -> list[int]:
    """The follow-ups standing in for follow-up `followup_id`
    (`followups.instead_of_followup_id`, R6-1) -- each stored as the same
    instruction about the signal a start admitted in place of the call it
    is about -- and, after another admission, the ones standing in for
    those in turn; oldest first (final fix round 8, R8-2). With both
    channels read each of them is only a copy of `followup_id`'s
    instruction: when its poster changes or withdraws that, `edits_repo`
    changes or withdraws them with it."""
    found: list[int] = []
    todo = [followup_id]
    while todo:
        held = todo.pop(0)
        for (other,) in conn.execute(
            "SELECT followup_id FROM followups WHERE edit_seq = 0 AND instead_of_followup_id = ? "
            "ORDER BY followup_id",
            (held,),
        ).fetchall():
            if other != followup_id and other not in found:
                found.append(other)
                todo.append(other)
    return found


@dataclass(frozen=True, slots=True)
class HandBack:
    """`hand_back`'s answer: the follow-up that acts on `signal_id` now."""

    followup_id: int
    channel_id: int
    message_id: int
    signal_id: int
    #: Its instructions waiting.
    queued: int


def hand_back(
    conn: sqlite3.Connection, *, followup_id: int, read_channels: Collection[int] | None
) -> HandBack | None:
    """Follow-up `followup_id`, standing in for a held one about a call whose
    channel was switched off (`followups.instead_of_followup_id`, R6-1), no
    longer gives its instruction -- its own post deleted, edited away, or
    edited into another instruction -- while the instruction it stood in
    for still stands in its own post, on a channel this watch reads (the
    channel switched back on: `read_channels`; None, not known: never), not
    withdrawn: its instructions still waiting (a deletion or an edit away
    skips them all; none of one about a call switched off was ever taken,
    as a call traded is never switched off). With both channels read
    `followup_id`'s post was only a copy of that one, which still acts: so
    that one acts on the signal `followup_id` acted on now -- its target
    moved there, as a start moves a forward's (`_adopt_followup_copies`)
    (final fix round 8, R8-3). After two admissions the one it stands in
    for may be on a channel switched off; then the one THAT stands in for,
    and so on. None, changing nothing, when there is none such or when
    `followup_id` acted on no signal still traded."""
    row = conn.execute(
        "SELECT instead_of_followup_id FROM followups WHERE followup_id = ?", (followup_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    signal_id = signal_for_followup(conn, followup_id=followup_id)
    if signal_id is None or not carries(conn, _signal_row(conn, signal_id)):
        return None
    signal = _signal_row(conn, signal_id)
    held_id, seen = row[0], {followup_id}
    while held_id is not None and held_id not in seen:
        seen.add(held_id)
        held = conn.execute(
            "SELECT * FROM followups WHERE followup_id = ?", (held_id,)
        ).fetchone()
        if held is None:
            return None
        counts = outbox.state_counts(conn, kind="FOLLOWUP", followup_id=held_id)
        about = signal_for_followup(conn, followup_id=held_id)
        if (
            read_channels is not None
            and held["channel_id"] in read_channels
            and counts.get("QUEUED")
            and about is not None
            and switched_off(conn, _signal_row(conn, about))
        ):
            own = (held["channel_id"], held["reply_to_message_id"]) == (
                signal["channel_id"],
                signal["message_id"],
            )
            path = (
                REPLY_PATH if own
                else COPY_REPLY_PATH if held["resolution_path"] == COPY_REPLY_PATH
                else RELAY_REPLY_PATH
            )
            conn.execute(
                "UPDATE followups SET target_signal_id = ?, resolution_path = ? "
                "WHERE followup_id = ?",
                (signal_id, path, held_id),
            )
            logger.info(
                "follow-up %s no longer gives the instruction it stood in for; follow-up %s "
                "(channel %s message %s) still does, on a channel read: it acts on signal %s now",
                followup_id,
                held_id,
                held["channel_id"],
                held["message_id"],
                signal_id,
            )
            return HandBack(
                held_id, held["channel_id"], held["message_id"], signal_id, counts["QUEUED"]
            )
        held_id = held["instead_of_followup_id"]
    return None


def handed_over(conn: sqlite3.Connection, followup_id: int) -> _Reported | None:
    """The first `FOLLOWUP_ABOUT_CHANNEL_OFF_SIGNAL` WARNING that handed
    follow-up `followup_id`'s instruction -- read in a copy of it, or of one
    standing in for it -- to the user to carry out by hand (`BY_HAND`), with
    the signal it named; or None. When its poster later changes or withdraws
    it, `edits_repo` says to review what was done by hand (final fix round
    8, R8-2)."""
    ids = (followup_id, *stand_ins_of(conn, followup_id))
    marks = ", ".join("?" for _ in ids)
    row = conn.execute(
        "SELECT a.alert_id, a.channel_id, a.message_id, a.signal_id FROM alerts a "
        "JOIN messages m ON m.channel_id = a.channel_id AND m.message_id = a.message_id "
        f"WHERE a.code = ? AND m.relay_of_followup_id IN ({marks}) AND a.detail LIKE ? "
        "ORDER BY a.alert_id LIMIT 1",
        (FOLLOWUP_ABOUT_CHANNEL_OFF_SIGNAL, *ids, f"%{BY_HAND}%"),
    ).fetchone()
    return None if row is None else _Reported(row[0], row[1], row[2], row[3])


# -- helpers -------------------------------------------------------------------


def _replacements(conn: sqlite3.Connection, signal_id: int) -> list[int]:
    """The signals that replaced `signal_id`, one after the other, oldest
    first (each records the one before it in `promoted_from_signal_id`, or
    -- admitted at watch's start in its place -- in `admitted_from_signal_id`
    (final fix round 7, R7-1); a replacement is always stored after the
    signal it replaces)."""
    chain: list[int] = []
    current = signal_id
    while True:
        row = conn.execute(
            "SELECT signal_id FROM signals "
            "WHERE (promoted_from_signal_id = ? OR admitted_from_signal_id = ?) AND signal_id > ? "
            "ORDER BY signal_id DESC LIMIT 1",
            (current, current, current),
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


def read_post(post: sqlite3.Row, *, channel_cfg: _ChannelCfgLike) -> RouteResult:
    """**How every relay rule reads a stored post** (group-3 leftover
    G3L-3): `post` (a `messages` row of `channel_cfg`'s channel) re-read
    with the channel's parser -- unless it is no longer stored as a signal
    (`messages.kind`, which follows the post's latest version). A post
    retracted by an edit that ADDED a retraction word ("CANCELLED", ledger
    161; `edits_repo`) still parses as the same call -- the word changes
    no level -- but is stored as 'skip': read here, it shows no call. So
    are a post edited into chatter, a follow-up or broken text, and --
    the safe side -- one whose stored kind another channel's parser would
    disagree with. Every "does this post still show the call" question,
    here and in `edits_repo`, goes through this (`post_shows_call`)."""
    routed = route(
        post["raw_text"],
        channel_cfg=channel_cfg,
        message_id=post["message_id"],
        posted_at_utc=datetime.fromisoformat(post["posted_at_utc"]),
    )
    if routed.kind == "signal" and post["kind"] != "signal":
        return RouteResult(
            kind=post["kind"], detail=f"stored as {post['kind']}; it shows no call"
        )
    return routed


def post_shows_call(
    post: sqlite3.Row, row: sqlite3.Row, *, channel_cfg: _ChannelCfgLike
) -> bool:
    """Whether stored post `post` (a `messages` row of `channel_cfg`'s
    channel), read as the relay rules read a post (`read_post`: a
    retracted post shows no call), shows the call of signals row `row`."""
    routed = read_post(post, channel_cfg=channel_cfg)
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
    - it was deleted in the same report as the post recorded as closing
      one of them, still showing the call until then
      (`_deleted_with_closer`, ruling R6-3): it withdrew the call together
      with that post, which alone is recorded -- an echo too;
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
    if _deleted_with_closer(conn, row, chain, channel_cfg):
        return linked  # deleted in the report that withdrew the call: said then
    # A repeat removal: echoed only while its channel still shows the call.
    newest = _signal_row(conn, live_signal_for(conn, linked))
    for other in standing_posts_on(conn, signal_id=linked, channel_id=post[0]):
        if channel_cfg is None or post_shows_call(other, newest, channel_cfg=channel_cfg):
            return linked
    return None


def _deleted_with_closer(
    conn: sqlite3.Connection,
    post: sqlite3.Row,
    chain: list[int],
    channel_cfg: _ChannelCfgLike | None,
) -> bool:
    """For `_gone_post` (ruling R6-3): whether stored post `post`, a post of
    the call whose signals are `chain`, was deleted in the same report as
    the post recorded as withdrawing one of them -- the same channel and
    the same deletion time (every job of one report is stamped with the
    report's moment, `Listener.ingest_deletion`) -- while it still showed
    that signal's call (still stored as a signal; re-read with
    `channel_cfg`, when given). Of several posts deleted together only the
    lowest id is recorded (`edits_repo._closing_post`), yet each withdrew
    the call as much: a forward of any of them echoes that withdrawal. A
    promotion's closer withdrew nothing (`closed_by_promotion`), and a post
    that had left the call before the report withdrew nothing in it -- the
    recorded closer included: one that had withdrawn the call earlier by an
    edit (into chatter, or re-aimed) and is only deleted in this report
    withdrew nothing in it either (final fix FF1), so the report counts
    only while that closer still showed the call too."""
    if post["deleted_at_utc"] is None or post["kind"] != "signal":
        return False
    marks = ", ".join("?" for _ in chain)
    for signal in conn.execute(
        "SELECT s.* FROM signals s JOIN messages m "
        "  ON m.channel_id = s.closed_by_channel_id AND m.message_id = s.closed_by_message_id "
        f"WHERE s.signal_id IN ({marks}) AND s.closed_by_channel_id = ? "
        "  AND m.deleted_at_utc = ? ORDER BY s.signal_id",
        (*chain, post["channel_id"], post["deleted_at_utc"]),
    ).fetchall():
        if closed_by_promotion(conn, signal["signal_id"]):
            continue
        closer = conn.execute(
            "SELECT * FROM messages WHERE channel_id = ? AND message_id = ?",
            (signal["closed_by_channel_id"], signal["closed_by_message_id"]),
        ).fetchone()
        # The closer still showing the call: not stored as something that is
        # no signal (edited into chatter, or CANCELLED), nor re-aimed.
        if closer["kind"] != "signal":
            continue
        if channel_cfg is None or (
            post_shows_call(closer, signal, channel_cfg=channel_cfg)
            and post_shows_call(post, signal, channel_cfg=channel_cfg)
        ):
            return True
    return False


def _call_stands_elsewhere(
    conn: sqlite3.Connection,
    *,
    signal_id: int,
    post: tuple[int, int],
    channel_cfg: _ChannelCfgLike | None,
    read_channels: Collection[int] | None = None,
) -> bool:
    """Whether a post of `signal_id`'s call other than `post` still stands,
    on any channel this watch reads (`read_channels`; None: on any
    channel): not deleted, still stored as a signal, not the post
    that closed one of the call's signals (`standing_posts_on`) and -- with
    `channel_cfg`, the changed post's channel config -- re-read still
    showing the call of its newest signal. A post of another channel is
    re-read with that config too (its own is not at hand here): one that
    does not read as the call with it counts as not standing, which only
    makes a removal withdraw the call -- the safe side.

    A post on a channel switched off counts for nothing (final fix round
    4, R4-5): watch does not read it, so nothing would see it deleted or
    edited away, and a call standing only there would be left to trade
    with no post this watch reads showing it (INV-1, "a channel this watch
    reads")."""
    for other in _posts_showing_call(
        conn, signal_id=signal_id, post=post, channel_cfg=channel_cfg
    ):
        if read_channels is not None and other["channel_id"] not in read_channels:
            continue  # on a channel switched off: not read (R4-5)
        return True
    return False


def _posts_showing_call(
    conn: sqlite3.Connection,
    *,
    signal_id: int,
    post: tuple[int, int],
    channel_cfg: _ChannelCfgLike | None,
) -> Iterator[sqlite3.Row]:
    """For `_call_stands_elsewhere`: the posts of `signal_id`'s call other
    than `post` that still stand, on any channel -- not deleted, still
    stored as a signal, not the post that closed one of the call's signals
    and, with `channel_cfg`, re-read showing the call of its newest
    signal. Oldest post first: the start's withdrawal names the oldest
    (`_withdraw_calls_shown_only_off`; final fix round 6, R6-3)."""
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
        "     AND s.closed_by_message_id = m.message_id) "
        "ORDER BY m.posted_at_utc, m.channel_id, m.message_id",
        (*post, *chain, *chain, *chain),
    ).fetchall():
        if channel_cfg is None or post_shows_call(other, newest, channel_cfg=channel_cfg):
            yield other


def _signal_row(conn: sqlite3.Connection, signal_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM signals WHERE signal_id = ?", (signal_id,)).fetchone()


def _link_of(conn: sqlite3.Connection, channel_id: int, message_id: int) -> int | None:
    """The signal stored post (`channel_id`, `message_id`) is a copy of
    (`messages.relay_of_signal_id`), or None."""
    row = conn.execute(
        "SELECT relay_of_signal_id FROM messages WHERE channel_id = ? AND message_id = ?",
        (channel_id, message_id),
    ).fetchone()
    return None if row is None else row[0]


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
    `window` seconds of any of the `around` times that can carry it
    (`carries`: not withdrawn-unexecuted, its channel not switched off), or
    None. Never a signal of `exclude`'s own."""
    for moment in around:
        for candidate in _same_trade(
            conn, trade_hash=trade_hash, posted_at_utc=moment, window=window
        ):
            if (candidate["channel_id"], candidate["message_id"]) == exclude:
                continue
            signal = _signal_row(conn, live_signal_for(conn, candidate["signal_id"]))
            if (signal["channel_id"], signal["message_id"]) == exclude:
                continue
            if carries(conn, signal):
                return signal
    return None


def match_of(conn: sqlite3.Connection, signal: sqlite3.Row) -> SignalMatch:
    """Signal `signal` (a `signals` row) as the call a post repeats -- the
    same trade -- for `record_signal_copy`."""
    return _signal_match(conn, signal, via="hash")


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
