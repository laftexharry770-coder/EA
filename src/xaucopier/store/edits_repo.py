"""What an edit or a deletion of an already-stored message does (spec 8.3).

Signal posters edit messages -- fix a stop, move a target, occasionally
flip BUY to SELL -- and delete them, sometimes to retract a bad call. An
edited stop we never see is a live position with the wrong stop; a
deletion we never see is a trade the poster retracted, still held. This
module is the one policy for both, whichever path found the change: the
`MessageEdited`/`MessageDeleted` events, the poll's window re-read
(`ingest/edits.py`), or `messages_repo.ingest()` meeting a changed
version of a message it already stored (the poll's forward read, or
backfill). Every function runs inside one writer job -- one transaction,
decided against the stored row -- so a change seen by two paths is
processed exactly once: the second finds it already recorded.

**Which version is newer.** A version is acted on only when its
`edit_date` is set and not earlier than the stored one. No `edit_date`
and a different parse is Task 5's "unverified" case (ignored); an older
`edit_date` is a late event (ignored, "stale"). An *equal* `edit_date`
with different content counts as newer: Telegram's `edit_date` has
one-second resolution, so two edits inside one second share it, and
dropping the second would lose it for good. The price is that a late
event carrying the first of two same-second versions is applied too --
and the window re-read, which fetches the current version, re-applies
the second at its next round. A newer version is always
recorded verbatim on `messages` -- text, hash, `edit_date`, and `kind`,
which now follows the latest classification -- and then:

- **Same parse** (whitespace, emoji: `content_hash` unchanged) -- cosmetic.
  Logged once and dropped: no version bump, no state change, no alert.
- **Signal -> signal, side / symbol / entry zone changed** -- the signal is
  `INVALID` and a CRITICAL alert is raised, executed or not. Never
  silently re-aim a trade. The stored parse is kept: it is what any live
  leg was opened on.
- **Signal -> signal, only SL / TPs changed** -- the parse is replaced and
  `signals.version` bumped. If the signal is executed or in flight, one
  `AMEND_SL`, and one `AMEND_TP` per changed target, is also enqueued and
  fanned out (Plan 3 applies them). A ladder that changed length after
  entry cannot be amended and raises a WARNING.
- **Signal -> unparseable** (`parse_failed`) -- `INVALID` + CRITICAL alert.
- **Signal -> chatter or a follow-up** -- the call is withdrawn: handled
  exactly like a deletion (below). The new follow-up is not dispatched.
- **Follow-up -> follow-up** -- updated in place (a worker reads it when it
  claims); WARNING if it had already been dispatched.
- **Follow-up -> anything else** -- withdrawn: its queued rows are skipped.
- **A relay copy of a follow-up** (no followups row of its own; see
  `relay_repo`) edited or deleted -- a WARNING naming both posts; the
  follow-up acted on is the other post's and is not changed by guess.
  Nothing is said when that post already reads the same, or was withdrawn.
- **Anything -> signal or follow-up, from something else** -- recorded and
  alerted (WARNING), never traded. An edit never fans out an ENTRY, nor
  an instruction: one that creates a trade is too easy to trigger by
  accident. No `signals` row is created either -- it would make a genuine
  repost of the same call a relay duplicate, and so never traded.
- A signal already `INVALID` or `VOIDED` stays so; an edit of it is
  recorded with a WARNING.

**Deletions.** An unexecuted signal is `VOIDED`. An executed or in-flight
one is *flagged* -- `messages.deleted_at_utc` plus a CRITICAL alert naming
the signal -- and **not** closed: a deletion can be cosmetic (a repost, a
tidy-up). Its `state` stays `NEW` so Plan 3 keeps managing the live legs.
Either way the signal is marked withdrawn (`signals.withdrawn_at_utc`, so
`outbox.claim()` never hands out one of its entries again, even one whose
worker died holding it), and entries not yet taken (`QUEUED`) are
skipped. A deleted follow-up's queued rows are skipped. Everything else
is only marked. A signal edited into chatter is withdrawn the same way.

**A relayed call: its posts, whichever was stored first** (final review
F02, rulings C2 and R3-P1/P2). A call often reaches us as several posts
-- the poster's, a relay's copy, the poster's own repost -- and whichever
was stored first holds its `signals` row, an accident of delivery. So the
policy speaks of the call and its posts (`relay_repo`): every post acts
on the signal that carries the call now (`relay_repo.signal_for_post`:
the newest of a chain of signals that replaced each other), and a post
that is not that signal's own message is handled exactly like it --
amend, invalidate, void, flag -- with every alert naming both messages.
The post that closed one of the chain's signals stays with that signal.

- **A post that stops showing the call** (R3-P1) -- deleted, edited into
  something that is no signal, re-aimed (side, symbol, entry zone) or
  broken (unparseable): a channel withdraws the call only when its *last*
  standing post of it goes. A post *stands* while it is not deleted and,
  re-read with its channel's parser, still shows the call: a signal on
  the same side, symbol and entry zone. One of several standing posts on
  a channel going is a repeat removal: a WARNING `SIGNAL_REPEAT_REMOVED`
  names the post still showing it, and the call stands. When the post
  that goes is the signal's own message and its channel still shows the
  call in another post: if nothing was executed, the signal is VOIDED
  (INVALID for a re-aim or a break, with its CRITICAL alert) and the
  earliest such post is promoted in its place (below); if it was, only
  the WARNING -- the post was replaced, the trade continues. When a
  channel's last post goes, the call is withdrawn entirely, as a change
  of the signal's own message would: deleted or edited away, VOIDED (or
  a CRITICAL flag if executed); re-aimed or broken, `INVALID` with a
  CRITICAL alert -- one source withdrew the call -- and nothing is
  promoted on any channel. A post that has left the call says nothing
  more: a later version that still does not show it, or its deletion, is
  only recorded.
- **A promotion** (final review F04) happens only on the signal's own
  channel: the earliest standing post of the call there (not deleted,
  still showing the call, not the post that withdrew it, not deleted in
  the same report) is re-read with the channel's parser, given its
  `signals` row (`promoted_from_signal_id` naming the signal it
  replaces) and its ENTRY rows in the same writer job, unlinked, and
  announced with a WARNING `SIGNAL_REPOST_PROMOTED` naming both messages;
  every other copy of the call then repeats it, and every follow-up that
  targeted the withdrawn signal targets it (`relay_repo.record_replacement`;
  not one replying to the post that withdrew it, ruling R4-4).
  Never promoted: a repost of an executed
  signal (trading it would open the trade twice), and a post on any other
  channel. (A repost posted *after* the withdrawal is not a copy at all:
  see `relay_repo`.)
- **An older post of a call withdrawn and posted again** (ruling R4-1,
  `relay_repo.old_post`) -- a post stored with a signal that another post
  withdrew entirely, before the call was posted again. Its edits reach
  the newest signal like any post's; its *removal* (deleted, edited away,
  re-aimed, broken) depends on what it is. A forward of a post of the
  call that no longer stands is an echo of that post's removal: only
  recorded, with an INFO line naming both -- whichever post was stored
  first, and also when nothing was posted again. The post a withdrawing
  forward copied is the original poster retracting: handled as above. Any
  other (a hand copy) may be a retraction or only the other channel
  removing its old copy: it still reaches the newest signal -- the safe
  side -- but loudly: VOIDED with CRITICAL `OLD_POST_WITHDREW_CALL`, or the
  usual CRITICAL flag or invalidation, each naming the post that withdrew
  the earlier signal and the post that carries the call now, and saying
  to trade the call by hand if only an old copy went.

**A post's edit never puts back a level another post moved** (ruling
R3-P4). A post of a relayed call is often a hand copy that mirrors some
edits and not others. Its edit is applied like the signal's own -- but
when its version still shows a level (SL, a TP, the ladder's length)
that its edit did not change and the signal has since moved, applying
it would silently put the old level back on a live position. Then
nothing is applied and a WARNING `SIGNAL_COPY_OUT_OF_DATE` names what
the post changed and what it still shows; if what it changed is on the
signal already (a mirrored edit), nothing is said. This holds for the
signal's own message as for any other post of a relayed call, so it
does not matter which post was stored first; a call nobody copied is
never affected (its message always shows the stored parse). A post that
had left the call (re-aimed) and is edited back into it corrects
nothing: levels it shows that the signal does not have are not applied,
and the same WARNING says so.

**The same change on every post of a relayed call is applied once.** A
relay that mirrors edits and deletions delivers each change twice. For a
relayed call, a version that would only close it the way it is closed
already -- withdraw a withdrawn signal, invalidate an INVALID one by a
re-aim or an unparseable text -- is logged, not alerted, and a deletion
of a post of an already-withdrawn relayed signal (or of a post edited
away before) is only marked; an SL/TP correction the signal already
carries changes nothing anyway. A call nobody copied keeps the policy
above unchanged.

**A withdrawn call posted again** (final review F04). Every post that
withdraws or invalidates a signal is recorded on it
(`signals.closed_by_channel_id` / `closed_by_message_id`, the first one
kept): the same call posted again on that post's channel is a new signal
replacing it (`relay_repo`).

**Several posts deleted at once.** One `MessageDeleted` event, or one
window re-read, can name a call and its repost together; each id is
still applied in its own writer job, but each job is told the others
(`also_deleted`). A post deleted in the same report counts as gone
already: it is never promoted (its entries would be claimable until the
next job voided them), and a repeat deleted together with its signal's
own message says nothing of its own -- the signal's own deletion speaks
for both.

**A deletion inferred from absence can be reversed.** The window re-read
(`ingest/edits.py`) infers a deletion when a message stops coming back --
guarded there by a circuit breaker and a second read, but still an
inference. So a mark it made (`deleted_detected_by = 'window'`) is never
final: its row stays in the window, and any later version of the message
-- the window reading it back, or an edit event -- clears the mark, raises
a CRITICAL `DELETION_REVERSED`, and is then handled like any version,
so an SL edit of an executed signal still gets its AMEND. A post whose
inferred deletion withdrew a signal, read back still showing that call
while a newer signal carries it, stands again: a post of the call like
any other read back (ruling R4-5, `_stands_again`) -- and so does such a
post edited away and then edited back into the call, and such a post read
or edited back before its channel posted the call again, once it does
(`relay_repo.record_replacement`). What the
deletion did (a void, skipped entries, the withdrawal) is not undone
automatically; the alert says what to review. A `MessageDeleted` event
names its ids explicitly and is final.

**"Executed" is the outbox, for now.** Plan 3's legs table does not exist
yet, so `trade_status()` reads the signal's ENTRY outbox rows: one `DONE`
is executed; one `CLAIMED` (a worker holds it, possibly mid order-send)
is *in flight*, and is treated like executed wherever that protects a
live position. Replace it with the legs table when Plan 3 lands.

**Durable counters.** A newer version or a deletion found by a history
read (`window`, `poll`, `backfill`) rather than the event stream bumps
`edit_missed_by_eventstream` / `delete_missed_by_eventstream`, the edit
and deletion counterparts of the poll's `missed_by_eventstream`.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from collections.abc import Collection
from datetime import datetime
from typing import Literal, Protocol

from xaucopier.domain.signal import FollowUp, FollowUpOp, Signal
from xaucopier.parsing.normalize import normalize
from xaucopier.parsing.router import RouteResult, route
from xaucopier.store import alerts, metrics, outbox, relay_repo, signals_repo

logger = logging.getLogger(__name__)

# -- alert codes (alerts.code) ------------------------------------------------
#: CRITICAL: an edit changed a signal's side, symbol or entry zone.
SIGNAL_INVALIDATED_BY_EDIT = "SIGNAL_INVALIDATED_BY_EDIT"
#: CRITICAL: an edit left a signal unparseable.
SIGNAL_UNPARSEABLE_AFTER_EDIT = "SIGNAL_UNPARSEABLE_AFTER_EDIT"
#: WARNING: an unexecuted signal edited into chatter or a follow-up; VOIDED.
SIGNAL_RETRACTED_BY_EDIT = "SIGNAL_RETRACTED_BY_EDIT"
#: CRITICAL: the same, on a signal that is executed or in flight.
LIVE_SIGNAL_RETRACTED_BY_EDIT = "LIVE_SIGNAL_RETRACTED_BY_EDIT"
#: WARNING: an unexecuted signal was deleted; VOIDED.
SIGNAL_VOIDED_BY_DELETION = "SIGNAL_VOIDED_BY_DELETION"
#: CRITICAL: a signal that is executed or in flight was deleted.
LIVE_SIGNAL_DELETED = "LIVE_SIGNAL_DELETED"
#: WARNING: an edit turned a non-signal into a signal. Not traded.
EDIT_CREATED_SIGNAL = "EDIT_CREATED_SIGNAL"
#: WARNING: an edit turned chatter (or a broken signal) into a follow-up. Not dispatched.
EDIT_CREATED_FOLLOWUP = "EDIT_CREATED_FOLLOWUP"
#: WARNING: a material edit of a signal that is already INVALID or VOIDED.
EDIT_OF_CLOSED_SIGNAL = "EDIT_OF_CLOSED_SIGNAL"
#: WARNING: one of several standing posts of a call on one channel was
#: deleted, or edited into something that is no signal; another post there
#: still shows the call, which stands (final review F02, ruling R3-P1).
SIGNAL_REPEAT_REMOVED = "SIGNAL_REPEAT_REMOVED"
#: WARNING: an unexecuted signal's own message was withdrawn while its
#: channel still showed the same call in another post: that post is now the
#: signal, queued for trading (final review F04).
SIGNAL_REPOST_PROMOTED = "SIGNAL_REPOST_PROMOTED"
#: CRITICAL: an older post of a call -- stored with a signal of it that
#: another post had withdrawn, before the call was posted again -- was
#: deleted or edited away, and that voided the call's newest signal. It may
#: be a retraction, or only the other channel removing its old copy (ruling
#: R4-1).
OLD_POST_WITHDREW_CALL = "OLD_POST_WITHDREW_CALL"
#: WARNING: an edit of one post of a relayed call was not applied: its
#: version still shows a level another post has moved since (review round
#: 1, minor 4; ruling R3-P4).
SIGNAL_COPY_OUT_OF_DATE = "SIGNAL_COPY_OUT_OF_DATE"
#: WARNING: an executed signal's TP ladder changed length; legs cannot follow.
TP_LADDER_CHANGED_AFTER_ENTRY = "TP_LADDER_CHANGED_AFTER_ENTRY"
#: WARNING: a follow-up already dispatched to a worker was edited.
FOLLOWUP_EDITED_AFTER_DISPATCH = "FOLLOWUP_EDITED_AFTER_DISPATCH"
#: WARNING: a follow-up edited into something that is no instruction.
FOLLOWUP_RETRACTED_BY_EDIT = "FOLLOWUP_RETRACTED_BY_EDIT"
#: WARNING: a follow-up message was deleted.
FOLLOWUP_DELETED = "FOLLOWUP_DELETED"
#: WARNING: a relay copy of a follow-up was edited or deleted; the
#: follow-up acted on is the other post's, and it was not changed (F08).
FOLLOWUP_COPY_CHANGED = "FOLLOWUP_COPY_CHANGED"
#: CRITICAL: a message the window re-read had inferred deleted is back.
DELETION_REVERSED = "DELETION_REVERSED"
#: CRITICAL: a window re-read's answer looks wrong (too many ids missing);
#: no deletion was inferred from it. Raised by ingest/edits.py.
WINDOW_READ_SUSPECT = "WINDOW_READ_SUSPECT"
#: CRITICAL: one window re-read voided signals; the count and the ids.
#: Raised by ingest/edits.py.
WINDOW_VOIDED_SIGNALS = "WINDOW_VOIDED_SIGNALS"

# -- metrics ------------------------------------------------------------------
EDIT_MISSED_BY_EVENTSTREAM_METRIC = "edit_missed_by_eventstream"
DELETE_MISSED_BY_EVENTSTREAM_METRIC = "delete_missed_by_eventstream"

# -- who found the change ------------------------------------------------------
#: The `MessageEdited` / `MessageDeleted` update handlers.
EVENT_DETECTOR = "event"
#: The poll's by-id re-read of the recent window (ingest/edits.py).
WINDOW_DETECTOR = "window"
#: History reads: a change they find is one the event stream did not
#: deliver first. `poll`/`backfill` reach here through `ingest()`.
HISTORY_DETECTORS = frozenset({WINDOW_DETECTOR, "poll", "backfill"})
#: `live` is `ingest()` called from the `NewMessage` handler for an id
#: already stored (a re-delivered update).
DETECTORS = HISTORY_DETECTORS | {EVENT_DETECTOR, "live"}

EditOutcome = Literal[
    "unknown",  # never stored: an edit path creates nothing (the forward read owns new messages)
    "deleted",  # already processed as deleted: ignored
    "duplicate",  # nothing newer than what is stored
    "stale",  # an older version than the stored one (a late event): ignored
    "unverified",  # content changed with no edit_date: ignored (Task 5's rule)
    "cosmetic",  # newer, same parse: verbatim text recorded, logged, dropped
    "reparsed",  # SL/TP change: parse replaced, version bumped (+ AMEND_* if executed)
    "invalidated",  # side/symbol/entry changed, or now unparseable: INVALID + alert
    "voided",  # an unexecuted signal withdrawn by the edit: VOIDED
    "flagged",  # an executed signal withdrawn by the edit: alert only, nothing closed
    "followup_edited",  # a follow-up's instruction changed: updated in place
    "withdrawn",  # a follow-up edited into no instruction: its queued rows skipped
    "reclassified",  # became a signal or follow-up by an edit: alerted, never traded
    "recorded",  # any other newer version: recorded, nothing to do
]
#: Outcomes that recorded a newer, materially different version.
CHANGED_OUTCOMES = frozenset(
    {
        "reparsed",
        "invalidated",
        "voided",
        "flagged",
        "followup_edited",
        "withdrawn",
        "reclassified",
        "recorded",
    }
)

DeletionOutcome = Literal[
    "unknown",  # never stored: nothing to do
    "already_deleted",  # processed before (by the other path): nothing to do
    "voided",  # an unexecuted signal: VOIDED
    "flagged",  # an executed / in-flight signal: alerted, NOT closed
    "withdrawn",  # a follow-up: its queued rows skipped
    "deleted",  # anything else: marked only
]

#: `signals_repo.trade_status`'s answer (defined there, used here).
TradeStatus = signals_repo.TradeStatus


class _ChannelCfgLike(Protocol):
    channel_id: int
    parser: str


class _AccountCfgLike(Protocol):
    account_id: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class EditResult:
    outcome: EditOutcome
    #: `messages.kind` after this call (None for "unknown").
    kind: str | None
    signal_id: int | None
    #: The message's own follow-up (`edit_seq` 0), if it has one.
    followup_id: int | None
    #: AMEND_* follow-ups this edit enqueued.
    amendment_ids: tuple[int, ...]
    alert_ids: tuple[int, ...]
    detail: str
    #: QUEUED outbox rows this call skipped (a withdrawn signal's entries,
    #: a withdrawn follow-up's instructions).
    skipped: int = 0
    #: This version proved a window-inferred deletion wrong, and cleared it.
    reversed_deletion: bool = False
    #: The withdrawn signal's standing repost this change promoted, if any.
    promoted_signal_id: int | None = None

    @property
    def changed(self) -> bool:
        return self.outcome in CHANGED_OUTCOMES or self.reversed_deletion


@dataclass(frozen=True, slots=True)
class DeletionResult:
    outcome: DeletionOutcome
    message_id: int
    kind: str | None
    signal_id: int | None
    followup_id: int | None
    #: QUEUED outbox rows this deletion skipped.
    skipped: int
    alert_ids: tuple[int, ...]
    #: The withdrawn signal's standing repost this deletion promoted, if any.
    promoted_signal_id: int | None = None


@dataclass(frozen=True, slots=True)
class StoredVersion:
    """What the window re-read compares a fetched message against."""

    message_id: int
    edit_date_utc: datetime | None
    #: The verbatim text stored for it (two edits in one second share an
    #: `edit_date`; only the text can tell them apart).
    raw_text: str
    #: Marked deleted by an earlier window re-read -- kept in the window
    #: so the mark can be reversed if the message is seen again.
    deleted_by_window: bool


def content_hash_for(route_result: RouteResult, text: str) -> str:
    """A message's identity for "did it change": a routed signal's trade
    hash (side / zone / SL / TPs -- so decoration never counts), and for
    anything else the sha1 of the normalised text -- normalised, so an
    added emoji or reflowed whitespace is not mistaken for a correction
    either. (Moved here from messages_repo with Task 10.)"""
    if route_result.kind == "signal":
        return route_result.signal.content_hash
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()


#: Whether a signal has been traded -- `signals_repo.trade_status`, the one
#: definition (see the module docstring's "Executed is the outbox").
trade_status = signals_repo.trade_status


def window_versions(
    conn: sqlite3.Connection, *, channel_id: int, at_or_below: int, limit: int
) -> tuple[StoredVersion, ...]:
    """The `limit` newest stored messages of a channel with ids at or
    below `at_or_below` (the poll pointer), oldest first. A message a
    `MessageDeleted` event deleted is not asked for again -- Telegram named
    it. One the window itself inferred deleted *is*: that inference may be
    wrong, and reading the message back is how it is reversed. Read-only:
    the listener calls this on its reader connection."""
    rows = conn.execute(
        "SELECT message_id, edit_date_utc, raw_text, deleted_detected_by FROM messages "
        "WHERE channel_id = ? AND message_id <= ? "
        "  AND (deleted_at_utc IS NULL OR deleted_detected_by = ?) "
        "ORDER BY message_id DESC LIMIT ?",
        (channel_id, at_or_below, WINDOW_DETECTOR, limit),
    ).fetchall()
    return tuple(
        StoredVersion(
            message_id=row[0],
            edit_date_utc=_parse_utc(row[1]),
            raw_text=row[2],
            deleted_by_window=row[3] == WINDOW_DETECTOR,
        )
        for row in reversed(rows)
    )


# -- edits -------------------------------------------------------------------


def apply_edit(
    conn: sqlite3.Connection,
    *,
    channel_cfg: _ChannelCfgLike,
    message_id: int,
    text: str,
    posted_at_utc: datetime,
    edit_date_utc: datetime | None,
    reply_to_message_id: int | None,
    now_utc: datetime,
    accounts: tuple[_AccountCfgLike, ...],
    detected_by: str,
) -> EditResult:
    """The current version of a message, from an edit path (the event, or
    the window re-read). A message never stored is left alone
    ("unknown"): an edit path never creates one, and so never an ENTRY --
    the poll's forward read stores new messages."""
    _check_detector(detected_by)
    channel_id = channel_cfg.channel_id
    existing = _get_message(conn, channel_id=channel_id, message_id=message_id)
    if existing is None:
        logger.debug(
            "edit of channel %s message %s, which is not stored; the forward read owns it",
            channel_id,
            message_id,
        )
        return EditResult(
            outcome="unknown",
            kind=None,
            signal_id=None,
            followup_id=None,
            amendment_ids=(),
            alert_ids=(),
            detail="not stored; left to the poll",
        )
    route_result = route(
        text,
        channel_cfg=channel_cfg,
        message_id=message_id,
        posted_at_utc=posted_at_utc,
        reply_to_message_id=reply_to_message_id,
    )
    return reconcile(
        conn,
        existing=existing,
        channel_cfg=channel_cfg,
        message_id=message_id,
        text=text,
        route_result=route_result,
        new_hash=content_hash_for(route_result, text),
        edit_date_utc=edit_date_utc,
        now_utc=now_utc,
        accounts=accounts,
        detected_by=detected_by,
    )


def reconcile(
    conn: sqlite3.Connection,
    *,
    existing: sqlite3.Row,
    channel_cfg: _ChannelCfgLike,
    message_id: int,
    text: str,
    route_result: RouteResult,
    new_hash: str,
    edit_date_utc: datetime | None,
    now_utc: datetime,
    accounts: tuple[_AccountCfgLike, ...],
    detected_by: str,
) -> EditResult:
    """Decide what a version of an already-stored message means, and do
    it. `existing` is its `messages` row. See the module docstring for
    the whole policy; `messages_repo.ingest()` and `apply_edit()` both
    end here. `channel_cfg` is the message's channel (its parser re-reads
    a withdrawn call's standing repost, which may be promoted)."""
    _check_detector(detected_by)
    stored_kind = existing["kind"]
    channel_id = channel_cfg.channel_id
    ctx = _Ctx(
        conn=conn,
        channel_cfg=channel_cfg,
        message_id=message_id,
        now_utc=now_utc,
        edit_date_utc=edit_date_utc,
        accounts=accounts,
        detected_by=detected_by,
        signal_id=signals_repo.get_signal_id(conn, channel_id=channel_id, message_id=message_id),
        followup_id=signals_repo.get_followup_id(
            conn, channel_id=channel_id, message_id=message_id
        ),
        relay_of_signal_id=existing["relay_of_signal_id"],
        relay_of_followup_id=existing["relay_of_followup_id"],
    )

    reversal: tuple[int, ...] = ()
    if existing["deleted_at_utc"] is not None:
        if existing["deleted_detected_by"] != WINDOW_DETECTOR:
            # Telegram said so (a MessageDeleted event): final.
            return ctx.result("deleted", stored_kind, "already deleted; an edit of it is ignored")
        # Only inferred, from absence -- and here the message is.
        reversal = (_reverse_window_deletion(ctx, existing),)
        _stands_again(ctx, route_result)
    result = _reconcile_version(
        ctx, existing, text=text, route_result=route_result, new_hash=new_hash
    )
    if reversal:
        result = dataclasses.replace(
            result, alert_ids=(*reversal, *result.alert_ids), reversed_deletion=True
        )
    return result


def _reconcile_version(
    ctx: _Ctx,
    existing: sqlite3.Row,
    *,
    text: str,
    route_result: RouteResult,
    new_hash: str,
) -> EditResult:
    """The version rules and the policy -- `reconcile()` minus the
    deletion mark."""
    conn = ctx.conn
    channel_id = ctx.channel_id
    message_id = ctx.message_id
    edit_date_utc = ctx.edit_date_utc
    detected_by = ctx.detected_by
    now_utc = ctx.now_utc
    stored_kind = existing["kind"]
    stored_hash = existing["content_hash"]
    if edit_date_utc is None:
        if new_hash == stored_hash:
            return ctx.result("duplicate", stored_kind, route_result.detail)
        return ctx.result(
            "unverified", stored_kind, "content_hash changed with no edit_date_utc; ignored"
        )
    stored_edit = _parse_utc(existing["edit_date_utc"])
    if stored_edit is not None and edit_date_utc == stored_edit:
        # One-second resolution: an equal edit_date with different content
        # is a second edit inside the same second (module docstring).
        if new_hash == stored_hash and text == existing["raw_text"]:
            return ctx.result("duplicate", stored_kind, route_result.detail)
    elif stored_edit is not None and edit_date_utc < stored_edit:
        if new_hash == stored_hash:
            return ctx.result("duplicate", stored_kind, route_result.detail)
        logger.info(
            "ignoring an older version of channel %s message %s (edited %s; %s is stored)",
            channel_id,
            message_id,
            edit_date_utc.isoformat(),
            stored_edit.isoformat(),
        )
        return ctx.result("stale", stored_kind, "older than the stored version; ignored")

    # A newer version: recorded verbatim, whatever it turns out to mean.
    new_kind = route_result.kind
    conn.execute(
        "UPDATE messages SET raw_text = ?, content_hash = ?, edit_date_utc = ?, kind = ? "
        "WHERE channel_id = ? AND message_id = ?",
        (text, new_hash, edit_date_utc.isoformat(), new_kind, channel_id, message_id),
    )
    if detected_by in HISTORY_DETECTORS:
        metrics.bump(
            conn, day_key=metrics.day_key_for(now_utc), name=EDIT_MISSED_BY_EVENTSTREAM_METRIC
        )
    # A post that withdrew a signal, edited back into its call while a newer
    # signal carries it, stands again (R4-5, as a post read back).
    _stands_again(ctx, route_result)

    if new_hash == stored_hash:
        logger.info(
            "cosmetic edit of channel %s message %s (%s, found by %s): the parse is "
            "unchanged, so it is recorded and dropped",
            channel_id,
            message_id,
            stored_kind,
            detected_by,
        )
        return ctx.result("cosmetic", new_kind, "cosmetic edit; parse unchanged")

    if stored_kind == "signal":
        return _edit_of_signal(
            dataclasses.replace(ctx, previous_text=existing["raw_text"]), route_result
        )
    if stored_kind == "followup":
        return _edit_of_followup(ctx, route_result, new_hash)
    return _edit_of_other(ctx, stored_kind, route_result)


def _reverse_window_deletion(ctx: _Ctx, existing: sqlite3.Row) -> int:
    """Clear a deletion the window inferred for a message that turns out
    to exist, and raise a CRITICAL alert saying what that deletion did --
    none of which is undone here."""
    ctx.conn.execute(
        "UPDATE messages SET deleted_at_utc = NULL, deleted_detected_by = NULL "
        "WHERE channel_id = ? AND message_id = ?",
        (ctx.channel_id, ctx.message_id),
    )
    row = _call_row(ctx)
    if row is not None and row["state"] == "NEW" and row["withdrawn_at_utc"] is None:
        # A repeat removal (ruling R3-P1): another post showed the call.
        consequence = (
            f"signal {row['signal_id']}{_relation(ctx, row)} still stands (it is NEW, trade "
            f"status {trade_status(ctx.conn, signal_id=row['signal_id'])}): another post of "
            "the call still showed it, so nothing was withdrawn"
        )
    elif row is not None:
        consequence = (
            f"signal {row['signal_id']}{_relation(ctx, row)} is {row['state']}"
            f"{' and marked withdrawn' if row['withdrawn_at_utc'] else ''}; entries skipped "
            "when it was withdrawn stay skipped, and claim() will not hand out its "
            "remaining ones. Trade status: "
            f"{trade_status(ctx.conn, signal_id=row['signal_id'])}"
            f"{_promoted_in_place_of(ctx.conn, row['signal_id'])}"
        )
    elif ctx.followup_id is not None:
        consequence = f"follow-up {ctx.followup_id}'s queued instructions stay skipped"
    else:
        consequence = f"nothing -- it is {existing['kind']}"
    return ctx.alert(
        "CRITICAL",
        DELETION_REVERSED,
        f"message {ctx.message_id} was marked deleted at {existing['deleted_at_utc']} "
        "because the window re-read stopped returning it, but it exists after all. "
        f"The mark is cleared; what the deletion did is NOT undone: {consequence}. "
        "Review by hand.",
    )


def _stands_again(ctx: _Ctx, route_result: RouteResult) -> None:
    """`ctx`'s post, which withdrew a signal, shows its call again: the
    window had inferred it deleted and it is back (`_reverse_window_deletion`),
    or it was edited away and is now edited back. If a newer signal carries
    that call (a promotion, or the call posted again), the post is no
    longer the one that withdrew it: it stands again, a post of the call
    like any other read or edited back (ruling R4-5) -- counted as showing
    the call (`relay_repo.standing_posts_on`), and its later edits,
    deletion and replies reach the call's newest signal. So it does not
    matter whether the post held the signal (VIP-first: it withdrew it) or
    was a copy (relay-first: a repeat removal). A signal nothing replaced
    keeps its closer: its channel withdrew the call, and may post it
    again; and an edit never opens a trade."""
    if route_result.kind != "signal":
        return
    for row in ctx.conn.execute(
        "SELECT * FROM signals WHERE closed_by_channel_id = ? AND closed_by_message_id = ?",
        (ctx.channel_id, ctx.message_id),
    ).fetchall():
        if relay_repo.live_signal_for(ctx.conn, row["signal_id"]) == row["signal_id"]:
            continue
        if not relay_repo.shows_call(route_result.signal, row):
            continue
        signals_repo.release_closed_by(
            ctx.conn,
            signal_id=row["signal_id"],
            channel_id=ctx.channel_id,
            message_id=ctx.message_id,
        )
        logger.info(
            "channel %s message %s, which withdrew signal %s, shows its call again: it "
            "stands again, a post of the call signal %s carries now",
            ctx.channel_id,
            ctx.message_id,
            row["signal_id"],
            relay_repo.live_signal_for(ctx.conn, row["signal_id"]),
        )


def _promoted_in_place_of(conn: sqlite3.Connection, signal_id: int) -> str:
    """For an alert about withdrawn signal `signal_id`: the signal that
    carries its call now (`relay_repo.live_signal_for` -- after a chain of
    replacements, the newest), if another one does -- the same call,
    which trading `signal_id` by hand as well would open twice. A
    sentence starting with ". ", or ""."""
    newest = relay_repo.live_signal_for(conn, signal_id)
    if newest == signal_id:
        return ""
    row = conn.execute(
        "SELECT signal_id, channel_id, message_id, state, withdrawn_at_utc FROM signals "
        "WHERE signal_id = ?",
        (newest,),
    ).fetchone()
    status = trade_status(conn, signal_id=newest)
    carried = (row["state"] == "NEW" and row["withdrawn_at_utc"] is None) or status != "pending"
    return (
        f". The same call posted again, message {row['message_id']} on channel "
        f"{row['channel_id']}, was made signal {newest} in its place (it is "
        f"{row['state']}{', withdrawn' if row['withdrawn_at_utc'] else ''}, trade status "
        f"{status})"
        + (
            f": trading signal {signal_id} by hand as well would open the trade twice"
            if carried
            else ", and was withdrawn too"
        )
    )


def _relation(ctx: _Ctx, row: sqlite3.Row) -> str:
    """How `ctx`'s message stands to signal `row`, the one it acts on
    (`_call_row`), for an alert: "" for the signal's own message."""
    if row["signal_id"] == ctx.signal_id:
        return ""
    if ctx.signal_id is not None:
        return f" (this message first carried the same call, as signal {ctx.signal_id})"
    return " (this message is a copy of it)"


def _edit_of_signal(ctx: _Ctx, route_result: RouteResult) -> EditResult:
    new_kind = route_result.kind
    row = _call_row(ctx)
    if row is not None:
        ctx = dataclasses.replace(
            ctx,
            old_post=relay_repo.old_post(
                ctx.conn,
                channel_id=ctx.channel_id,
                message_id=ctx.message_id,
                channel_cfg=ctx.channel_cfg,
            ),
        )
        if row["signal_id"] == ctx.signal_id:
            return _edit_of_signal_row(ctx, row, route_result)
        # Another post of the call -- a relay copy (final review F02), or
        # the post that carried the call before it was replaced (R3-P2):
        # the edit is reconciled against the signal carrying the call,
        # exactly like an edit of that signal's own message, and every
        # alert names both messages.
        return _edit_of_signal_row(_as_copy(ctx, row), row, route_result)
    # Never traded and a copy of nothing: a signal only an edit made (or a
    # copy stored before copies were linked). Nothing to correct, and still
    # nothing to trade.
    logger.info(
        "edit of signal message %s/%s, which has no trade on file (now %s); recorded",
        ctx.channel_id,
        ctx.message_id,
        new_kind,
    )
    return ctx.result("recorded", new_kind, "no trade on file for this signal")


def _edit_of_signal_row(ctx: _Ctx, row: sqlite3.Row, route_result: RouteResult) -> EditResult:
    """The edit policy for signal `row`, reached through its own message
    or (`ctx.via_copy`) through a copy of it."""
    new_kind = route_result.kind
    status = trade_status(ctx.conn, signal_id=row["signal_id"])
    again = _closes_it_again(ctx, row, route_result)
    if again is not None:
        logger.info(
            "edit of channel %s message %s (now %s): signal %s is %s already; the same "
            "change reaching another copy of the call, so nothing more is done",
            ctx.channel_id,
            ctx.message_id,
            new_kind,
            row["signal_id"],
            again,
        )
        return ctx.result("recorded", new_kind, f"signal {row['signal_id']} is {again} already")
    if row["state"] != "NEW":
        alert = ctx.alert(
            "WARNING",
            EDIT_OF_CLOSED_SIGNAL,
            f"signal {row['signal_id']} is {row['state']} and stays so; the poster edited "
            f"it again (it now reads as {new_kind}: {_describe(route_result)}). Trade "
            f"status: {status}. Nothing was changed or queued"
            f"{_promoted_in_place_of(ctx.conn, row['signal_id'])}.",
        )
        return ctx.result("recorded", new_kind, "edit of a closed signal", alert_ids=(alert,))

    shows_now = new_kind == "signal" and not _reaims(
        json.loads(row["parsed_json"]), route_result.signal
    )
    if not shows_now and _left_call_before(ctx, row):
        # This post had left the call already (re-aimed while another post
        # of it stood); a version that still does not show it changes nothing.
        logger.info(
            "edit of channel %s message %s (now %s), which no longer showed the call of "
            "signal %s; recorded",
            ctx.channel_id,
            ctx.message_id,
            new_kind,
            row["signal_id"],
        )
        return ctx.result("recorded", new_kind, "a post that had left the call")
    if not shows_now and _is_echo(ctx):
        # A forward of a post of the call that no longer stands (R4-1 a).
        _log_echo(ctx, row, how=f"edited into {new_kind}")
        return ctx.result("recorded", new_kind, "an echo of another post's removal")
    if new_kind == "signal":
        return _signal_to_signal(ctx, row, status, route_result.signal)
    if new_kind == "parse_failed":
        return _break_post(
            ctx,
            row,
            status,
            code=SIGNAL_UNPARSEABLE_AFTER_EDIT,
            reason=f"the edited text no longer parses ({route_result.detail})",
            how="edited so it no longer reads as a signal",
            new_kind=new_kind,
        )
    # skip or followup: the post no longer shows the call -- a deletion in
    # all but name (R3-P1: the call is withdrawn only if it was the
    # channel's last standing post of it).
    return _withdraw_post(
        ctx,
        row,
        status,
        via=f"edited into {new_kind} ({_describe(route_result)})",
        how=f"edited into {new_kind}",
        pending_code=SIGNAL_RETRACTED_BY_EDIT,
        live_code=LIVE_SIGNAL_RETRACTED_BY_EDIT,
        new_kind=new_kind,
    )


def _signal_to_signal(
    ctx: _Ctx, row: sqlite3.Row, status: TradeStatus, new: Signal
) -> EditResult:
    old = json.loads(row["parsed_json"])
    reaimed = _reaims(old, new)
    if reaimed:
        return _break_post(
            ctx,
            row,
            status,
            code=SIGNAL_INVALIDATED_BY_EDIT,
            reason="the edit re-aims the trade: " + "; ".join(reaimed),
            how=f"re-aimed ({'; '.join(reaimed)})",
            new_kind="signal",
        )

    old_tps = tuple(old["tps"])
    sl_changed = not _same(old["sl"], new.sl)
    tp_changes = tuple(
        (index, new.tps[index - 1])
        for index in range(1, min(len(old_tps), len(new.tps)) + 1)
        if not _same(old_tps[index - 1], new.tps[index - 1])
    )
    ladder_changed = len(old_tps) != len(new.tps)
    if not (sl_changed or tp_changes or ladder_changed):
        # The signal already says this: the same correction reaching another
        # copy of the call (the edited message's own hash did change).
        return ctx.result("cosmetic", "signal", "levels unchanged")
    if ctx.via_copy is not None or relay_repo.is_relayed(ctx.conn, signal_id=row["signal_id"]):
        # Any post of a relayed call, its signal's own message included
        # (R3-P4): which post holds the signal is an accident of delivery.
        out_of_date = _copy_out_of_date(ctx, row, old, new)
        if out_of_date is not None:
            return out_of_date

    signal_id = row["signal_id"]
    if ctx.via_copy is not None:
        # The copy's correction, recorded as the signal's own: its message,
        # and the date the call was first posted.
        new = dataclasses.replace(
            new,
            channel_id=row["channel_id"],
            message_id=row["message_id"],
            posted_at_utc=datetime.fromisoformat(old["posted_at_utc"]),
        )
    signals_repo.update_signal_on_edit(
        ctx.conn, channel_id=row["channel_id"], message_id=row["message_id"], signal=new
    )
    change = _describe_levels(old, new)
    amendments: list[int] = []
    raised: list[int] = []
    if status != "pending":
        raw = f"edit of signal {signal_id} ({ctx.channel_id}/{ctx.message_id}): {change}"
        if sl_changed:
            amendments.append(_amend(ctx, row, FollowUpOp.AMEND_SL, new.sl, None, raw))
        for index, price in tp_changes:
            amendments.append(_amend(ctx, row, FollowUpOp.AMEND_TP, price, index, raw))
        if ladder_changed:
            raised.append(
                ctx.alert(
                    "WARNING",
                    TP_LADDER_CHANGED_AFTER_ENTRY,
                    f"signal {signal_id} is {status}; the poster changed its take-profit "
                    f"ladder from {len(old_tps)} to {len(new.tps)} targets ({change}). "
                    "Legs cannot be added or removed after entry: review them by hand.",
                    signal_id=signal_id,
                )
            )
    logger.info(
        "signal %s (%s/%s) re-parsed after an edit found by %s: %s; trade %s; %d "
        "amendment(s) queued",
        signal_id,
        ctx.channel_id,
        ctx.message_id,
        ctx.detected_by,
        change,
        status,
        len(amendments),
    )
    return ctx.result(
        "reparsed", "signal", change, amendments=tuple(amendments), alert_ids=tuple(raised)
    )


def _copy_out_of_date(
    ctx: _Ctx, row: sqlite3.Row, old: dict, new: Signal
) -> EditResult | None:
    """An edit (`new`) of one post of relayed signal `row` (stored parse
    `old`), compared with that post's own previous version
    (`ctx.previous_text`): None when it may be applied like any edit.
    Otherwise its version still shows a level its edit did not change but
    the signal has moved since -- see the module docstring -- and nothing
    is applied: an INFO line when what the post changed is on the signal
    already, else a WARNING naming it."""
    if ctx.previous_text is None:
        return None
    before = route(
        ctx.previous_text,
        channel_cfg=ctx.channel_cfg,
        message_id=ctx.message_id,
        posted_at_utc=new.posted_at_utc,
    )
    if before.kind != "signal":
        return None
    prev = before.signal
    if _reaims(old, prev):
        # The post had left the call (re-aimed while another post of it
        # stood) and shows it again: its version corrects nothing, so no
        # level it shows is applied (the "levels unchanged" case returned
        # before this).
        alert = ctx.alert(
            "WARNING",
            SIGNAL_COPY_OUT_OF_DATE,
            f"{_subject(ctx)} was edited back into the call of signal {row['signal_id']} "
            f"after it had been re-aimed away from it, but it shows "
            f"{', '.join(_levels_not_on(old, new, row['signal_id']))}. This post had left "
            f"the call, so its version is no correction of it: signal {row['signal_id']} was "
            "NOT changed and nothing was queued. If the poster meant the new level, review "
            "it by hand.",
            signal_id=row["signal_id"],
        )
        return ctx.result(
            "recorded", "signal", "a post back in the call; not applied", alert_ids=(alert,)
        )
    old_tps = tuple(old["tps"])
    changed: list[str] = []  # what the copy's edit changed
    pending: list[str] = []  # ... and is not on the signal yet
    stale: list[str] = []  # what it did not change, which the signal has moved since
    if not _same(prev.sl, new.sl):
        changed.append(f"SL {prev.sl:g} -> {new.sl:g}")
        if not _same(new.sl, old["sl"]):
            pending.append("SL")
    elif not _same(new.sl, old["sl"]):
        stale.append(f"SL {new.sl:g} (signal {row['signal_id']} has {old['sl']:g})")
    for index in range(min(len(prev.tps), len(new.tps))):
        label = f"TP{index + 1}"
        on_signal = old_tps[index] if index < len(old_tps) else None
        if not _same(prev.tps[index], new.tps[index]):
            changed.append(f"{label} {prev.tps[index]:g} -> {new.tps[index]:g}")
            if on_signal is None or not _same(new.tps[index], on_signal):
                pending.append(label)
        elif on_signal is not None and not _same(new.tps[index], on_signal):
            stale.append(
                f"{label} {new.tps[index]:g} (signal {row['signal_id']} has {on_signal:g})"
            )
    if len(prev.tps) != len(new.tps):
        changed.append(f"its take-profit count {len(prev.tps)} -> {len(new.tps)}")
        if len(new.tps) != len(old_tps):
            pending.append("the take-profit count")
    elif len(new.tps) != len(old_tps):
        stale.append(
            f"{len(new.tps)} take-profits (signal {row['signal_id']} has {len(old_tps)})"
        )
    if not stale:
        return None
    if not pending:
        logger.info(
            "edit of channel %s message %s, a post of signal %s (%s): the signal carries "
            "that already; nothing more is done",
            ctx.channel_id,
            ctx.message_id,
            row["signal_id"],
            ", ".join(changed),
        )
        return ctx.result("recorded", "signal", f"signal {row['signal_id']} carries it already")
    alert = ctx.alert(
        "WARNING",
        SIGNAL_COPY_OUT_OF_DATE,
        f"{_subject(ctx)} was edited ({', '.join(changed)}), but it still shows "
        f"{', '.join(stale)}: another post of this call has moved that since, and this "
        f"post never followed. Signal {row['signal_id']} was NOT changed and nothing was "
        "queued -- applying this version would put the old level back. If the poster "
        "meant the new level, review it by hand.",
        signal_id=row["signal_id"],
    )
    return ctx.result(
        "recorded", "signal", "a post's out-of-date version; not applied", alert_ids=(alert,)
    )


def _amend(
    ctx: _Ctx,
    row: sqlite3.Row,
    op: FollowUpOp,
    price: float,
    tp_index: int | None,
    raw: str,
) -> int:
    """One AMEND_* follow-up for signal `row`, keyed by the signal's own
    message -- also when the edit came through a copy of it (`raw` names
    the message that was edited)."""
    followup = FollowUp(
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        op=op,
        posted_at_utc=ctx.edit_date_utc,
        reply_to_message_id=row["message_id"],
        price_hint=price,
        tp_index_hint=tp_index,
        raw=raw,
    )
    followup_id = signals_repo.insert_edit_amendment(
        ctx.conn,
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        followup=followup,
        target_signal_id=row["signal_id"],
    )
    outbox.fan_out_checked(
        ctx.conn,
        kind="FOLLOWUP",
        signal_id=None,
        followup_id=followup_id,
        accounts=ctx.accounts,
        now_utc=ctx.now_utc,
    )
    return followup_id


def _invalidate(
    ctx: _Ctx,
    row: sqlite3.Row,
    status: TradeStatus,
    *,
    code: str,
    reason: str,
    new_kind: str,
) -> EditResult:
    signal_id = row["signal_id"]
    signals_repo.set_signal_state(ctx.conn, signal_id=signal_id, state="INVALID")
    signals_repo.record_closed_by(
        ctx.conn, signal_id=signal_id, channel_id=ctx.channel_id, message_id=ctx.message_id
    )
    skipped = outbox.skip_queued(
        ctx.conn, kind="ENTRY", signal_id=signal_id, reason=f"signal invalidated: {reason}"
    )
    alert = ctx.alert(
        "CRITICAL",
        code,
        f"signal {signal_id} is now INVALID: {reason}. {skipped} queued entr"
        f"{'y' if skipped == 1 else 'ies'} skipped. {_live_clause(status)}"
        f"{_old_post_doubt(ctx, status, changed=True)}",
        signal_id=signal_id,
    )
    # The signal's own message re-aimed or broken while nothing was
    # executed, and its channel still shows the call in another post: that
    # post is promoted. Never through any other post (R3-P1).
    promoted = _promote_repost(ctx, row) if status == "pending" and _is_holder(ctx, row) else None
    return ctx.result(
        "invalidated",
        new_kind,
        reason,
        alert_ids=(alert, *_alerts_of(promoted)),
        promoted_signal_id=_signal_of(promoted),
    )


def _withdraw_post(
    ctx: _Ctx,
    row: sqlite3.Row,
    status: TradeStatus,
    *,
    via: str,
    how: str,
    pending_code: str,
    live_code: str,
    new_kind: str | None,
) -> EditResult:
    """`ctx`'s post of the call signal `row` carries no longer shows it:
    deleted, or edited into something that is no signal (ruling R3-P1). A
    channel withdraws the call only with its *last* standing post of it:

    - another post of the call still stands on this channel: the signal's
      own message going while nothing was executed voids it and promotes
      the earliest such post (`_withdraw_signal`); anything else is a
      repeat removal -- a WARNING naming the post still showing the call,
      which stands (an executed call's own message included: the post was
      replaced, the trade continues);
    - it was the channel's last: the call is withdrawn entirely
      (`_withdraw_signal`: VOIDED, or a CRITICAL flag if executed), and
      nothing is promoted on any channel.

    A post deleted in the same report counts as gone already; a repeat
    deleted together with the signal's own message leaves the saying to
    that message's job."""
    if row["withdrawn_at_utc"] is None:
        if (
            not _is_holder(ctx, row)
            and row["channel_id"] == ctx.channel_id
            and row["message_id"] in ctx.also_deleted
        ):
            logger.info(
                "channel %s message %s, a repeat of signal %s, %s together with the signal's "
                "own message; that one's deletion says it",
                ctx.channel_id,
                ctx.message_id,
                row["signal_id"],
                how,
            )
            return ctx.result("recorded", new_kind, "deleted with its signal's own message")
        others = _standing_others(ctx, row)
        if others:
            if (
                _is_holder(ctx, row)
                and row["state"] == "NEW"
                and status == "pending"
                and _promotable(ctx, others)
            ):
                return _withdraw_signal(
                    ctx, row, status, via=via, pending_code=pending_code, live_code=live_code,
                    new_kind=new_kind, promote=True,
                )
            alert = _repeat_removed(ctx, row, status, how=how, standing=others[0][0])
            return ctx.result(
                "recorded", new_kind, f"a repeat {how}; the call stands", alert_ids=(alert,)
            )
    return _withdraw_signal(
        ctx, row, status, via=via, pending_code=pending_code, live_code=live_code,
        new_kind=new_kind, promote=False,
    )


def _break_post(
    ctx: _Ctx,
    row: sqlite3.Row,
    status: TradeStatus,
    *,
    code: str,
    reason: str,
    how: str,
    new_kind: str,
) -> EditResult:
    """`ctx`'s post of the call signal `row` carries was re-aimed or broken:
    it no longer shows the call (ruling R3-P1, as `_withdraw_post`). Another
    post of the call still standing on this channel: a repeat removal -- a
    WARNING, the call stands -- unless the post is the signal's own message
    and nothing was executed, when the signal is invalidated and that post
    promoted (`_invalidate`). The channel's last post: the call is
    invalidated, loudly, and nothing is promoted."""
    others = _standing_others(ctx, row) if row["withdrawn_at_utc"] is None else []
    if others and not (_is_holder(ctx, row) and status == "pending" and _promotable(ctx, others)):
        alert = _repeat_removed(
            ctx, row, status, how=how, standing=others[0][0], changed=True
        )
        return ctx.result(
            "recorded", new_kind, f"a repeat {how}; the call stands", alert_ids=(alert,)
        )
    return _invalidate(ctx, row, status, code=code, reason=reason, new_kind=new_kind)


def _reread(ctx: _Ctx, post: sqlite3.Row) -> RouteResult:
    """A stored post of `ctx`'s channel, re-read with the channel's parser."""
    return route(
        post["raw_text"],
        channel_cfg=ctx.channel_cfg,
        message_id=post["message_id"],
        posted_at_utc=datetime.fromisoformat(post["posted_at_utc"]),
    )


def _shows(route_result: RouteResult, row: sqlite3.Row) -> bool:
    """Whether a post reading `route_result` shows the call of signal
    `row`: a signal on its side, symbol and entry zone (its stop and
    targets may lag behind -- a hand copy that never followed an edit)."""
    return route_result.kind == "signal" and relay_repo.shows_call(route_result.signal, row)


def _standing_others(ctx: _Ctx, row: sqlite3.Row) -> list[tuple[sqlite3.Row, RouteResult]]:
    """The other posts of signal `row`'s call on `ctx`'s channel that still
    stand (ruling R3-P1), earliest first, each with its re-read:
    `relay_repo.standing_posts_on`, minus `ctx`'s own post, posts deleted in
    the same report, and posts that no longer show the call -- a post
    re-aimed while another stood still reads as a signal, but not as this
    one."""
    others = []
    for post in relay_repo.standing_posts_on(
        ctx.conn, signal_id=row["signal_id"], channel_id=ctx.channel_id
    ):
        if post["message_id"] == ctx.message_id or post["message_id"] in ctx.also_deleted:
            continue
        routed = _reread(ctx, post)
        if _shows(routed, row):
            others.append((post, routed))
        elif routed.kind != "signal":
            # Stored as a signal, no longer read as one: the parser changed.
            logger.warning(
                "channel %s message %s, a post of signal %s, no longer reads as a signal "
                "(%s); not promoted or counted as showing the call",
                ctx.channel_id,
                post["message_id"],
                row["signal_id"],
                routed.detail,
            )
    return others


def post_left_live_call(
    conn: sqlite3.Connection, *, channel_cfg: _ChannelCfgLike, message_id: int, signal_id: int
) -> bool:
    """Whether stored post `message_id` of `channel_cfg`'s channel, re-read
    with the channel's parser, shows another call than live signal
    `signal_id` (another side, symbol or entry zone): it was re-aimed away
    while another post kept the call standing (ruling R3-P1), so it is no
    post of that call any more. A follow-up replying to it is not about
    the live trade (`messages_repo.ingest`). False when the signal is
    closed -- a reply to the post that closed it is about that signal --
    and for a post that no longer reads as a signal at all."""
    post = _get_message(conn, channel_id=channel_cfg.channel_id, message_id=message_id)
    signal = conn.execute("SELECT * FROM signals WHERE signal_id = ?", (signal_id,)).fetchone()
    if post is None or signal is None or post["kind"] != "signal":
        return False
    if signal["state"] != "NEW" or signal["withdrawn_at_utc"] is not None:
        return False
    routed = route(
        post["raw_text"],
        channel_cfg=channel_cfg,
        message_id=message_id,
        posted_at_utc=datetime.fromisoformat(post["posted_at_utc"]),
    )
    return routed.kind == "signal" and not _shows(routed, signal)


def post_left_withdrawn_call(
    conn: sqlite3.Connection, *, channel_cfg: _ChannelCfgLike, message_id: int, signal_id: int
) -> bool:
    """Whether stored post `message_id` of `channel_cfg`'s channel, a post
    of signal `signal_id` -- withdrawn before anything of it was executed --
    no longer shows its call: deleted, edited into something that is no
    signal, re-aimed or broken. The post that withdrew the signal, above
    all (ruling R4-4), or one that had left the call while another post
    kept it standing. A follow-up replying to it is about no trade, so it
    gets no target (`messages_repo.ingest`) -- whether or not the call was
    posted again since, which it must never reach. A signal that was
    executed keeps such a reply: it may be about its live legs, and such
    a signal is never replaced."""
    signal = conn.execute("SELECT * FROM signals WHERE signal_id = ?", (signal_id,)).fetchone()
    if signal is None or not relay_repo.is_withdrawn_unexecuted(conn, signal):
        return False
    post = _get_message(conn, channel_id=channel_cfg.channel_id, message_id=message_id)
    if post is None or post["deleted_at_utc"] is not None or post["kind"] != "signal":
        return True
    return not relay_repo.post_shows_call(post, signal, channel_cfg=channel_cfg)


def _promotable(
    ctx: _Ctx, others: list[tuple[sqlite3.Row, RouteResult]]
) -> list[tuple[sqlite3.Row, RouteResult]]:
    """The standing posts among `others` that can be promoted: every one but
    a post that already carries a signals row of its own -- an earlier
    signal of the call, its own message standing again (a window deletion
    read back). One post, one signal: while only such posts stand, the
    call is not withdrawn but stands, a repeat removal."""
    return [
        (post, routed)
        for post, routed in others
        if signals_repo.get_signal_id(
            ctx.conn, channel_id=ctx.channel_id, message_id=post["message_id"]
        )
        is None
    ]


def _left_call_before(ctx: _Ctx, row: sqlite3.Row) -> bool:
    """Whether `ctx`'s post, in the version this edit replaces
    (`ctx.previous_text`), no longer showed the call of signal `row`: it
    was re-aimed away while another post of the call stood (a repeat
    removal, so the signal stayed as it was)."""
    if ctx.previous_text is None:
        return False
    before = route(
        ctx.previous_text,
        channel_cfg=ctx.channel_cfg,
        message_id=ctx.message_id,
        posted_at_utc=ctx.now_utc,
    )
    return not _shows(before, row)


def _withdraw_signal(
    ctx: _Ctx,
    row: sqlite3.Row,
    status: TradeStatus,
    *,
    via: str,
    pending_code: str,
    live_code: str,
    new_kind: str | None,
    promote: bool,
) -> EditResult:
    """A signal the poster withdrew -- by deleting it, or by editing it
    into something that is no signal. Entries not yet taken are skipped
    either way. Unexecuted: VOIDED, and with `promote` the earliest
    standing post of the call on this channel promoted in its place
    (`_promote_repost`). Executed or in flight: flagged with a CRITICAL
    alert and *not* closed; `state` stays NEW so its live legs are still
    managed."""
    signal_id = row["signal_id"]
    signals_repo.mark_withdrawn(ctx.conn, signal_id=signal_id, now_utc=ctx.now_utc)
    signals_repo.record_closed_by(
        ctx.conn, signal_id=signal_id, channel_id=ctx.channel_id, message_id=ctx.message_id
    )
    skipped = outbox.skip_queued(
        ctx.conn, kind="ENTRY", signal_id=signal_id, reason=f"signal withdrawn: {via}"
    )
    entries = f"{skipped} queued entr{'y' if skipped == 1 else 'ies'} skipped"
    if status == "pending":
        signals_repo.set_signal_state(ctx.conn, signal_id=signal_id, state="VOIDED")
        if _is_older_post(ctx):
            # R4-1 (c): a retraction, or only an old copy removed -- loudly.
            alert = ctx.alert(
                "CRITICAL",
                OLD_POST_WITHDREW_CALL,
                f"signal {signal_id} is now VOIDED ({via}). Nothing had been executed; "
                f"{entries}.{_old_post_doubt(ctx, status, changed=False)}",
                signal_id=signal_id,
            )
        else:
            alert = ctx.alert(
                "WARNING",
                pending_code,
                f"signal {signal_id} VOIDED: the poster withdrew it ({via}). Nothing had been "
                f"executed; {entries}.",
                signal_id=signal_id,
            )
        promoted = _promote_repost(ctx, row) if promote else None
        return ctx.result(
            "voided",
            new_kind,
            via,
            alert_ids=(alert, *_alerts_of(promoted)),
            skipped=skipped,
            promoted_signal_id=_signal_of(promoted),
        )
    alert = ctx.alert(
        "CRITICAL",
        live_code,
        f"signal {signal_id} was withdrawn by the poster ({via}) but is {status}. Its live "
        f"legs were NOT closed -- a deletion or rewrite can be cosmetic -- review them by "
        f"hand. {entries}.{_old_post_doubt(ctx, status, changed=False)}",
        signal_id=signal_id,
    )
    return ctx.result("flagged", new_kind, via, alert_ids=(alert,), skipped=skipped)


def _promote_repost(
    ctx: _Ctx, withdrawn: sqlite3.Row
) -> tuple[int, tuple[int, ...]] | None:
    """`withdrawn`'s own message -- `ctx`'s post -- was withdrawn (deleted,
    edited into something else, re-aimed or broken) before anything was
    executed, while its channel still shows the same call in another
    standing post (`_standing_others`: not deleted, still showing the
    call, not the post that withdrew it, not deleted in the same report;
    and not carrying a signal of its own). The earliest such
    post becomes the signal: re-read with the channel's parser, given its
    `signals` row (recording the one it replaces) and its ENTRY rows, in
    this same writer job, unlinked, and announced with a WARNING naming
    both messages. Every other copy of the call now repeats it,
    and `relay_repo.record_replacement` moves the call's follow-ups to
    it. Only ever on the signal's own channel (ruling R3-P1). Returns
    (new signal id, alert ids) or None."""
    conn = ctx.conn
    old_id = withdrawn["signal_id"]
    # Never the post that withdrew the call, a post deleted in the same
    # report, one that no longer shows the call, or one with a signals row
    # of its own (`_standing_others`, `_promotable`).
    for repost, routed in _promotable(ctx, _standing_others(ctx, withdrawn)):
        new_id = signals_repo.insert_signal(
            conn,
            channel_id=ctx.channel_id,
            message_id=repost["message_id"],
            signal=routed.signal,
            promoted_from_signal_id=old_id,
        )
        queued = outbox.fan_out_checked(
            conn,
            kind="ENTRY",
            signal_id=new_id,
            followup_id=None,
            accounts=ctx.accounts,
            now_utc=ctx.now_utc,
        )
        conn.execute(
            "UPDATE messages SET relay_of_signal_id = NULL WHERE channel_id = ? AND message_id = ?",
            (ctx.channel_id, repost["message_id"]),
        )
        # Every other copy of the call, on any channel, now repeats the
        # promoted signal -- not the post that withdrew it, which keeps
        # naming the signal it closed. The call never stopped: none of them
        # is an older post of a withdrawn call (ruling R4-1).
        conn.execute(
            "UPDATE messages SET relay_of_signal_id = ? "
            "WHERE relay_of_signal_id = ? AND deleted_at_utc IS NULL "
            "  AND NOT (channel_id = ? AND message_id = ?)",
            (new_id, old_id, ctx.channel_id, ctx.message_id),
        )
        replaced = relay_repo.record_replacement(
            conn, new_signal_id=new_id, replaced_signal_id=old_id, now_utc=ctx.now_utc
        )
        alert = ctx.alert(
            "WARNING",
            SIGNAL_REPOST_PROMOTED,
            f"message {ctx.message_id} withdrew signal {old_id} before anything was "
            f"executed, but the same call posted again as message {repost['message_id']} on "
            f"this channel still stands: it is now signal {new_id}, and {queued} trade "
            f"entr{'y is' if queued == 1 else 'ies are'} queued for it. If the poster meant to "
            "cancel the call altogether, check the channel.",
            signal_id=new_id,
            message_id=repost["message_id"],
        )
        logger.warning(
            "channel %s: repost %s promoted to signal %s after signal %s (message %s) was "
            "withdrawn",
            ctx.channel_id,
            repost["message_id"],
            new_id,
            old_id,
            ctx.message_id,
        )
        return new_id, (alert, *replaced)
    return None


def _call_row(ctx: _Ctx) -> sqlite3.Row | None:
    """The `signals` row `ctx`'s message acts on
    (`relay_repo.signal_for_post`: its own signal, or the one it was
    stored as a copy of, taken to the newest signal of the call -- unless
    this message closed one of them), or None."""
    signal_id = relay_repo.signal_for_post(
        ctx.conn, channel_id=ctx.channel_id, message_id=ctx.message_id
    )
    if signal_id is None:
        return None
    return ctx.conn.execute("SELECT * FROM signals WHERE signal_id = ?", (signal_id,)).fetchone()


def _is_holder(ctx: _Ctx, row: sqlite3.Row) -> bool:
    """Whether `ctx`'s message is signal `row`'s own message."""
    return (row["channel_id"], row["message_id"]) == (ctx.channel_id, ctx.message_id)


def _subject(ctx: _Ctx) -> str:
    """`ctx`'s message, as an alert's subject: "it" after `_as_copy`'s
    prefix (which names it), else its ids."""
    if ctx.via_copy is not None:
        return "it"
    return f"message {ctx.message_id} on channel {ctx.channel_id}"


def _as_copy(ctx: _Ctx, holder: sqlite3.Row) -> _Ctx:
    """`ctx` for a change that reaches signal `holder` through another post
    of its call: results and alerts carry the signal, and every alert
    first names this post and the signal's own message -- for an older
    post of the call (`_is_older_post`), also the post that withdrew the
    signal it was stored with."""
    stored_with = ctx.signal_id if ctx.signal_id is not None else ctx.relay_of_signal_id
    old = ctx.old_post
    if old is not None and old.kind == "earlier":
        via = (
            f"message {ctx.message_id} on channel {ctx.channel_id} is an older post of this "
            f"call: it was first recorded with signal {stored_with}, and message "
            f"{old.post[1]} on channel {old.post[0]} had already withdrawn signal "
            f"{old.signal_id}; the call was posted again as message {holder['message_id']} "
            f"on channel {holder['channel_id']}, which is signal {holder['signal_id']}"
        )
    elif ctx.signal_id is not None:
        via = (
            f"message {ctx.message_id} on channel {ctx.channel_id} first carried this call as "
            f"signal {ctx.signal_id}; signal {holder['signal_id']} (message "
            f"{holder['message_id']} on channel {holder['channel_id']}) carries it now"
        )
    elif stored_with is not None and stored_with != holder["signal_id"]:
        via = (
            f"message {ctx.message_id} on channel {ctx.channel_id} is a copy of signal "
            f"{stored_with}, whose call signal {holder['signal_id']} (message "
            f"{holder['message_id']} on channel {holder['channel_id']}) carries now"
        )
    else:
        via = (
            f"message {ctx.message_id} on channel {ctx.channel_id} is a copy of signal "
            f"{holder['signal_id']} (message {holder['message_id']} on channel "
            f"{holder['channel_id']})"
        )
    return dataclasses.replace(ctx, signal_id=holder["signal_id"], via_copy=via)


def _is_echo(ctx: _Ctx) -> bool:
    """Whether `ctx`'s post is an echo (`relay_repo.old_post`): a forward of
    a post of the call that no longer stands. Its removal was said when
    that post went; this one is only recorded (ruling R4-1 a)."""
    return ctx.old_post is not None and ctx.old_post.kind == "echo"


def _is_older_post(ctx: _Ctx) -> bool:
    """Whether `ctx`'s post is an older post of its call
    (`relay_repo.old_post` "earlier"): stored with a signal another post
    withdrew before the call was posted again, and no forward tells whether
    its removal is a retraction or only an old copy going (R4-1 c)."""
    return ctx.old_post is not None and ctx.old_post.kind == "earlier"


def _old_post_doubt(ctx: _Ctx, status: TradeStatus, *, changed: bool) -> str:
    """For an alert about an older post's removal or change (`_is_older_post`):
    the doubt, and what to do if it was only the other channel tidying up.
    "" for any other post."""
    if not _is_older_post(ctx):
        return ""
    what = "changing" if changed else "removing"
    doubt = (
        f" This post may only have been channel {ctx.channel_id} {what} its old copy of the "
        "call: if so, the call is still meant --"
    )
    if status == "pending":
        return f"{doubt} trade it by hand."
    return f"{doubt} keep the trade, and trade by hand the entries that were skipped."


def _log_echo(ctx: _Ctx, row: sqlite3.Row, *, how: str) -> None:
    """INFO: `ctx`'s post, an echo (`_is_echo`), was `how`: recorded only."""
    old = ctx.old_post
    logger.info(
        "channel %s message %s, a forward of message %s on channel %s -- a post of the call "
        "that no longer stands -- was %s: only an echo of that post's removal, which was "
        "handled when it happened; signal %s is left as it is",
        ctx.channel_id,
        ctx.message_id,
        old.post[1],
        old.post[0],
        how,
        row["signal_id"],
    )


#: How a parse re-aims a stored one: side, symbol, entry zone (the one
#: definition is `relay_repo.reaims`, which the relay rules share).
_reaims = relay_repo.reaims


def _closes_it_again(ctx: _Ctx, row: sqlite3.Row, route_result: RouteResult) -> str | None:
    """For a relayed call only -- this is a copy, or the signal has copies:
    whether this version would only close the signal the way it is closed
    already (withdraw a withdrawn one; invalidate an INVALID one by a
    re-aim or an unparseable text). The same edit typically reaches every
    copy of a relayed call, and the first one already did all there is to
    do and said so. Returns how it is closed, or None. A call nobody
    copied keeps its own policy unchanged."""
    if ctx.via_copy is None and not relay_repo.is_relayed(ctx.conn, signal_id=row["signal_id"]):
        return None
    new_kind = route_result.kind
    if new_kind in ("skip", "followup") and row["withdrawn_at_utc"] is not None:
        return "withdrawn"
    if row["state"] == "INVALID" and (
        new_kind == "parse_failed"
        or (new_kind == "signal" and _reaims(json.loads(row["parsed_json"]), route_result.signal))
    ):
        return "INVALID"
    return None


def _repeat_removed(
    ctx: _Ctx,
    row: sqlite3.Row,
    status: TradeStatus,
    *,
    how: str,
    standing: sqlite3.Row,
    changed: bool = False,
) -> int:
    """WARNING: `ctx`'s post of signal `row`'s call went while `standing`,
    another post of it on the same channel, still shows it (a double post
    tidied up, or the signal's own message replaced after execution); the
    signal stands (ruling R3-P1). `changed`: the post was re-aimed or
    broken rather than removed."""
    reading = (
        "so the call stands and this post is no longer read as part of it"
        if changed
        else "so this looks like a double post tidied up"
    )
    return ctx.alert(
        "WARNING",
        SIGNAL_REPEAT_REMOVED,
        f"{_subject(ctx)} was {how}. Signal {row['signal_id']} stands (it is {row['state']}, "
        f"trade status {status}): the same call is still posted as message "
        f"{standing['message_id']} on this channel, {reading}"
        f"{'' if status == 'pending' else ', and the trade continues'}. If the poster meant "
        f"to {'change' if changed else 'withdraw'} the call, check the channel.",
    )


def _alerts_of(promoted: tuple[int, tuple[int, ...]] | None) -> tuple[int, ...]:
    return () if promoted is None else promoted[1]


def _signal_of(promoted: tuple[int, tuple[int, ...]] | None) -> int | None:
    return None if promoted is None else promoted[0]


def _edit_of_followup(ctx: _Ctx, route_result: RouteResult, new_hash: str) -> EditResult:
    new_kind = route_result.kind
    followup_id = ctx.followup_id
    if followup_id is None:
        if ctx.relay_of_followup_id is not None:
            raised = _followup_copy_changed(
                ctx, how=f"edited to {_describe(route_result)}", new_hash=new_hash
            )
            return ctx.result(
                "recorded", new_kind, "a copy of a follow-up edited", alert_ids=raised
            )
        return ctx.result("recorded", new_kind, "no follow-up on file for this message")
    counts = outbox.state_counts(ctx.conn, kind="FOLLOWUP", followup_id=followup_id)
    dispatched = counts.get("CLAIMED", 0) + counts.get("DONE", 0)
    if new_kind == "followup":
        signals_repo.update_followup_on_edit(
            ctx.conn,
            channel_id=ctx.channel_id,
            message_id=ctx.message_id,
            followup=route_result.followup,
        )
        raised: tuple[int, ...] = ()
        if dispatched:
            raised = (
                ctx.alert(
                    "WARNING",
                    FOLLOWUP_EDITED_AFTER_DISPATCH,
                    f"follow-up {followup_id} was edited to {_describe(route_result)} after "
                    f"{dispatched} worker(s) had taken it; what they did is not undone.",
                ),
            )
        return ctx.result("followup_edited", new_kind, _describe(route_result), alert_ids=raised)

    partial = _withdraw_followup(
        ctx, followup_id, code=FOLLOWUP_RETRACTED_BY_EDIT, via=f"edited into {new_kind}"
    )
    if new_kind != "signal":
        return ctx.result(
            "withdrawn",
            new_kind,
            f"follow-up edited into {new_kind}",
            alert_ids=partial.alert_ids,
            skipped=partial.skipped,
        )
    created = _created_alert(ctx, route_result)
    return ctx.result(
        "reclassified",
        new_kind,
        "follow-up edited into a signal; not traded",
        alert_ids=(*partial.alert_ids, created),
        skipped=partial.skipped,
    )


def _followup_copy_changed(ctx: _Ctx, *, how: str, new_hash: str | None) -> tuple[int, ...]:
    """A relay copy of a follow-up (`ctx.relay_of_followup_id`) was edited
    or deleted. The follow-up acted on is the other post's, and it is not
    changed by guess: a WARNING names both, unless the change is already
    there -- the follow-up's own message reads the same, or was withdrawn
    (the same change reaching both posts)."""
    row = ctx.conn.execute(
        "SELECT f.followup_id, f.channel_id, f.message_id, m.content_hash, m.kind, "
        "       m.deleted_at_utc "
        "FROM followups f "
        "JOIN messages m ON m.channel_id = f.channel_id AND m.message_id = f.message_id "
        "WHERE f.followup_id = ?",
        (ctx.relay_of_followup_id,),
    ).fetchone()
    if row is None:
        return ()
    if row["content_hash"] == new_hash or row["deleted_at_utc"] is not None or (
        row["kind"] != "followup"
    ):
        logger.info(
            "channel %s message %s, a copy of follow-up %s, was %s; the follow-up's own "
            "message already says so",
            ctx.channel_id,
            ctx.message_id,
            row["followup_id"],
            how,
        )
        return ()
    counts = outbox.state_counts(ctx.conn, kind="FOLLOWUP", followup_id=row["followup_id"])
    taken = counts.get("CLAIMED", 0) + counts.get("DONE", 0)
    alert = ctx.alert(
        "WARNING",
        FOLLOWUP_COPY_CHANGED,
        f"message {ctx.message_id} on channel {ctx.channel_id}, a copy of follow-up "
        f"{row['followup_id']} (message {row['message_id']} on channel {row['channel_id']}), "
        f"was {how}. Follow-up {row['followup_id']} is the one acted on, and it was NOT "
        "changed: the instruction stands as first recorded, "
        f"{counts.get('QUEUED', 0)} instruction(s) still queued, {taken} already taken by a "
        "worker. If the poster changed or withdrew the instruction, review it by hand.",
    )
    return (alert,)


def _withdraw_followup(ctx: _Ctx, followup_id: int, *, code: str, via: str) -> _Partial:
    skipped = outbox.skip_queued(
        ctx.conn, kind="FOLLOWUP", followup_id=followup_id, reason=f"follow-up withdrawn: {via}"
    )
    counts = outbox.state_counts(ctx.conn, kind="FOLLOWUP", followup_id=followup_id)
    dispatched = counts.get("CLAIMED", 0) + counts.get("DONE", 0)
    alert = ctx.alert(
        "WARNING",
        code,
        f"follow-up {followup_id} withdrawn ({via}): {skipped} queued instruction(s) "
        f"skipped; {dispatched} already taken by a worker, which is not undone.",
    )
    return _Partial(alert_ids=(alert,), skipped=skipped)


def _edit_of_other(ctx: _Ctx, stored_kind: str, route_result: RouteResult) -> EditResult:
    new_kind = route_result.kind
    if new_kind in ("signal", "followup"):
        alert = _created_alert(ctx, route_result)
        return ctx.result(
            "reclassified", new_kind, f"{stored_kind} edited into {new_kind}", alert_ids=(alert,)
        )
    logger.info(
        "edit of channel %s message %s (%s -> %s): recorded; nothing to do",
        ctx.channel_id,
        ctx.message_id,
        stored_kind,
        new_kind,
    )
    return ctx.result("recorded", new_kind, f"{stored_kind} edited into {new_kind}")


def _created_alert(ctx: _Ctx, route_result: RouteResult) -> int:
    holder = (
        _call_row(ctx)
        if route_result.kind == "signal" and ctx.relay_of_signal_id is not None
        else None
    )
    if holder is not None:
        return ctx.alert(
            "WARNING",
            EDIT_CREATED_SIGNAL,
            f"an edit turned message {ctx.message_id} back into a signal "
            f"({_describe(route_result)}). It is a copy of signal {holder['signal_id']} "
            f"(message {holder['message_id']} on channel {holder['channel_id']}), which is "
            f"{holder['state']}{', withdrawn' if holder['withdrawn_at_utc'] else ''}"
            f"{_promoted_in_place_of(ctx.conn, holder['signal_id'])}. Edits never open "
            "trades: nothing was queued.",
            signal_id=holder["signal_id"],
        )
    if route_result.kind == "signal" and ctx.signal_id is not None:
        row = _call_row(ctx)
        if (
            row is not None
            and row["state"] == "NEW"
            and row["withdrawn_at_utc"] is None
            and _shows(route_result, row)
        ):
            # This post left its call while another post of it stood (a
            # repeat removal, ruling R3-P1): the call never stopped.
            return ctx.alert(
                "WARNING",
                EDIT_CREATED_SIGNAL,
                f"an edit turned message {ctx.message_id} back into a signal "
                f"({_describe(route_result)}). It is the call of signal {row['signal_id']}"
                f"{_relation(ctx, row)}, which still stands (it is NEW, trade status "
                f"{trade_status(ctx.conn, signal_id=row['signal_id'])}): another post of it "
                "showed the call all along. Edits never open trades: nothing more was queued.",
                signal_id=row["signal_id"],
            )
    if route_result.kind == "signal":
        return ctx.alert(
            "WARNING",
            EDIT_CREATED_SIGNAL,
            f"an edit turned message {ctx.message_id} into a signal "
            f"({_describe(route_result)}). Edits never open trades: nothing was queued. "
            "If it is a real call, trade it by hand.",
        )
    return ctx.alert(
        "WARNING",
        EDIT_CREATED_FOLLOWUP,
        f"an edit turned message {ctx.message_id} into a follow-up "
        f"({_describe(route_result)}). Edits never dispatch instructions: nothing was "
        "queued. If it is real, apply it by hand.",
    )


# -- deletions ---------------------------------------------------------------


def apply_deletion(
    conn: sqlite3.Connection,
    *,
    channel_cfg: _ChannelCfgLike,
    message_id: int,
    now_utc: datetime,
    accounts: tuple[_AccountCfgLike, ...],
    detected_by: str,
    also_deleted: Collection[int] = (),
) -> DeletionResult:
    """A message is gone from its channel (the event said so, or the
    window re-read found `None` where it used to be). Marks it deleted --
    exactly once: a second report finds the mark -- and applies the
    deletion policy in the module docstring. Never closes anything.
    `also_deleted`: the other ids of the same channel that the same report
    named deleted (see "Several posts deleted at once").

    `channel_cfg` and `accounts` are what promoting a withdrawn call's
    standing repost needs (its parser; its ENTRY fan-out): required, like
    `ingest()`'s `accounts`, so a caller cannot silently promote a repost
    to no account."""
    _check_detector(detected_by)
    channel_id = channel_cfg.channel_id
    existing = _get_message(conn, channel_id=channel_id, message_id=message_id)
    if existing is None:
        return DeletionResult("unknown", message_id, None, None, None, 0, ())
    kind = existing["kind"]
    signal_id = signals_repo.get_signal_id(conn, channel_id=channel_id, message_id=message_id)
    followup_id = signals_repo.get_followup_id(conn, channel_id=channel_id, message_id=message_id)
    if existing["deleted_at_utc"] is not None:
        if detected_by == EVENT_DETECTOR and existing["deleted_detected_by"] == WINDOW_DETECTOR:
            # Telegram now confirms what the window inferred: make it final,
            # so the row leaves the window. Nothing else to do again.
            conn.execute(
                "UPDATE messages SET deleted_detected_by = ? WHERE channel_id = ? AND message_id = ?",
                (EVENT_DETECTOR, channel_id, message_id),
            )
        return DeletionResult(
            "already_deleted", message_id, kind, signal_id, followup_id, 0, ()
        )

    conn.execute(
        "UPDATE messages SET deleted_at_utc = ?, deleted_detected_by = ? "
        "WHERE channel_id = ? AND message_id = ?",
        (now_utc.isoformat(), detected_by, channel_id, message_id),
    )
    if detected_by in HISTORY_DETECTORS:
        metrics.bump(
            conn, day_key=metrics.day_key_for(now_utc), name=DELETE_MISSED_BY_EVENTSTREAM_METRIC
        )
    ctx = _Ctx(
        conn=conn,
        channel_cfg=channel_cfg,
        message_id=message_id,
        now_utc=now_utc,
        edit_date_utc=None,
        accounts=accounts,
        detected_by=detected_by,
        signal_id=signal_id,
        followup_id=followup_id,
        relay_of_signal_id=existing["relay_of_signal_id"],
        relay_of_followup_id=existing["relay_of_followup_id"],
        also_deleted=frozenset(also_deleted) - {message_id},
    )

    # Decided on the signals row, not on `kind`: a signal an earlier edit
    # reclassified (INVALID, or withdrawn while live) can still have live
    # legs, and its message going away is still worth saying. The row is
    # the one this post acts on (`_call_row`): its own signal or the one it
    # copies, taken to the newest signal of the call (R3-P2).
    row = _call_row(ctx)
    if row is not None:
        status = trade_status(conn, signal_id=row["signal_id"])
        own = row["signal_id"] == signal_id
        ctx = dataclasses.replace(
            ctx,
            old_post=relay_repo.old_post(
                conn, channel_id=channel_id, message_id=message_id, channel_cfg=channel_cfg
            ),
        )
        post_ctx = ctx if own else _as_copy(ctx, row)
        relayed = relay_repo.is_relayed(conn, signal_id=row["signal_id"])
        via = (
            f"message deleted, found by {detected_by}"
            if own
            else f"this {'copy' if signal_id is None else 'post'} was deleted, found by "
            f"{detected_by}"
        )
        if row["withdrawn_at_utc"] is None and (row["state"] == "NEW" or status != "pending"):
            if relayed and not _shows(_reread(ctx, existing), row):
                # A post of a relayed call edited into something else, or
                # re-aimed, before: it stopped showing the call then, and
                # that was said then.
                logger.info(
                    "channel %s message %s (%s), a post of signal %s edited away before, "
                    "deleted, found by %s; marked",
                    channel_id,
                    message_id,
                    kind,
                    row["signal_id"],
                    detected_by,
                )
                return DeletionResult(
                    "deleted", message_id, kind, row["signal_id"], followup_id, 0, ()
                )
            if _is_echo(ctx):
                # A forward of a post of the call that no longer stands: its
                # removal echoes that post's (ruling R4-1 a).
                _log_echo(ctx, row, how=f"deleted, found by {detected_by}")
                return DeletionResult(
                    "deleted", message_id, kind, row["signal_id"], followup_id, 0, ()
                )
            result = _withdraw_post(
                post_ctx,
                row,
                status,
                via=via,
                how=f"deleted, found by {detected_by}",
                pending_code=SIGNAL_VOIDED_BY_DELETION,
                live_code=LIVE_SIGNAL_DELETED,
                new_kind=kind,
            )
            return _deletion_result(result, message_id, kind, row["signal_id"], followup_id)
        if row["withdrawn_at_utc"] is not None and not relayed and (
            row["state"] == "NEW" or status != "pending"
        ):
            # A call nobody copied, withdrawn before (edited into chatter):
            # its deletion is said too, as it always was.
            result = _withdraw_signal(
                post_ctx,
                row,
                status,
                via=via,
                pending_code=SIGNAL_VOIDED_BY_DELETION,
                live_code=LIVE_SIGNAL_DELETED,
                new_kind=kind,
                promote=False,
            )
            return _deletion_result(result, message_id, kind, row["signal_id"], followup_id)
        # Closed already -- withdrawn by another post of a relayed call (the
        # same deletion reaching each post is said once), or INVALID/VOIDED
        # with nothing executed: only marked.
        logger.info(
            "channel %s message %s deleted, found by %s: signal %s is %s%s already; marked",
            channel_id,
            message_id,
            detected_by,
            row["signal_id"],
            row["state"],
            ", withdrawn" if row["withdrawn_at_utc"] else "",
        )
        return DeletionResult("deleted", message_id, kind, row["signal_id"], followup_id, 0, ())
    if kind == "followup" and followup_id is not None:
        partial = _withdraw_followup(
            ctx, followup_id, code=FOLLOWUP_DELETED, via=f"message deleted, found by {detected_by}"
        )
        return DeletionResult(
            "withdrawn", message_id, kind, signal_id, followup_id, partial.skipped, partial.alert_ids
        )
    if ctx.relay_of_followup_id is not None:
        raised = _followup_copy_changed(
            ctx, how=f"deleted, found by {detected_by}", new_hash=None
        )
        return DeletionResult("deleted", message_id, kind, None, None, 0, raised)

    logger.info(
        "channel %s message %s (%s) deleted, found by %s; marked",
        channel_id,
        message_id,
        kind,
        detected_by,
    )
    return DeletionResult("deleted", message_id, kind, signal_id, followup_id, 0, ())


def _deletion_result(
    result: EditResult, message_id: int, kind: str, signal_id: int, followup_id: int | None
) -> DeletionResult:
    """A deletion's `_withdraw_post` / `_withdraw_signal` outcome as a
    DeletionResult: "voided" or "flagged" as they are, a repeat removal
    (or nothing to say) "deleted"."""
    outcome: DeletionOutcome = (
        result.outcome if result.outcome in ("voided", "flagged") else "deleted"
    )
    return DeletionResult(
        outcome,
        message_id,
        kind,
        signal_id,
        followup_id,
        result.skipped,
        result.alert_ids,
        result.promoted_signal_id,
    )


# -- helpers -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Partial:
    alert_ids: tuple[int, ...]
    skipped: int


@dataclass(frozen=True, slots=True)
class _Ctx:
    """Everything one call's helpers share."""

    conn: sqlite3.Connection
    #: The changed message's channel.
    channel_cfg: _ChannelCfgLike
    message_id: int
    now_utc: datetime
    edit_date_utc: datetime | None
    accounts: tuple[_AccountCfgLike, ...]
    detected_by: str
    signal_id: int | None
    followup_id: int | None
    #: Set when the change reaches a signal through one of its relay copies
    #: (this message): what every alert then says first, naming both.
    via_copy: str | None = None
    #: The changed message's own `messages.relay_of_signal_id`.
    relay_of_signal_id: int | None = None
    #: ... and `messages.relay_of_followup_id`.
    relay_of_followup_id: int | None = None
    #: A deletion's: the channel's other ids the same report deleted.
    also_deleted: frozenset[int] = frozenset()
    #: An edit of a stored signal message: the text it replaces.
    previous_text: str | None = None
    #: How the changed post stands to the newest signal of its call, when
    #: its removal would reach that signal though the post echoes or
    #: predates it (`relay_repo.old_post`, ruling R4-1).
    old_post: relay_repo.OldPost | None = None

    @property
    def channel_id(self) -> int:
        return self.channel_cfg.channel_id

    def alert(
        self,
        level: alerts.AlertLevel,
        code: str,
        detail: str,
        *,
        signal_id: int | None = None,
        message_id: int | None = None,
    ) -> int:
        prefix = "" if self.via_copy is None else f"{self.via_copy}: "
        return alerts.record(
            self.conn,
            now_utc=self.now_utc,
            level=level,
            code=code,
            channel_id=self.channel_id,
            message_id=message_id if message_id is not None else self.message_id,
            signal_id=signal_id if signal_id is not None else self.signal_id,
            detail=f"{prefix}{detail} [found by {self.detected_by}]",
        )

    def result(
        self,
        outcome: EditOutcome,
        kind: str | None,
        detail: str,
        *,
        amendments: tuple[int, ...] = (),
        alert_ids: tuple[int, ...] = (),
        skipped: int = 0,
        promoted_signal_id: int | None = None,
    ) -> EditResult:
        return EditResult(
            outcome=outcome,
            kind=kind,
            signal_id=self.signal_id,
            followup_id=self.followup_id,
            amendment_ids=amendments,
            alert_ids=alert_ids,
            detail=detail,
            skipped=skipped,
            promoted_signal_id=promoted_signal_id,
        )


def _live_clause(status: TradeStatus) -> str:
    if status == "pending":
        return "Nothing had been executed."
    return (
        f"It is {status.replace('_', ' ')} (an entry was taken or is being taken): its legs "
        f"were NOT closed or re-aimed -- review them by hand."
    )


def _describe(route_result: RouteResult) -> str:
    if route_result.kind == "signal":
        s = route_result.signal
        tps = "/".join(f"{tp:g}" for tp in s.tps)
        return f"{s.side.value.upper()} {s.zone.lo:g}-{s.zone.hi:g} SL {s.sl:g} TP {tps}"
    if route_result.kind == "followup":
        f = route_result.followup
        extra = "" if f.price_hint is None else f" {f.price_hint:g}"
        return f"{f.op.value}{extra}"
    return route_result.kind


def _describe_levels(old: dict, new: Signal) -> str:
    parts = []
    if not _same(old["sl"], new.sl):
        parts.append(f"SL {old['sl']:g} -> {new.sl:g}")
    old_tps = old["tps"]
    for index in range(1, max(len(old_tps), len(new.tps)) + 1):
        before = old_tps[index - 1] if index <= len(old_tps) else None
        after = new.tps[index - 1] if index <= len(new.tps) else None
        if before is None or after is None or not _same(before, after):
            parts.append(
                f"TP{index} {'-' if before is None else f'{before:g}'} -> "
                f"{'-' if after is None else f'{after:g}'}"
            )
    return ", ".join(parts)


def _levels_not_on(old: dict, new: Signal, signal_id: int) -> list[str]:
    """The levels `new` shows that signal `signal_id` (stored parse `old`)
    does not have, each with the signal's own, for an alert."""
    parts = []
    if not _same(old["sl"], new.sl):
        parts.append(f"SL {new.sl:g} (signal {signal_id} has {old['sl']:g})")
    old_tps = old["tps"]
    for index in range(min(len(old_tps), len(new.tps))):
        if not _same(old_tps[index], new.tps[index]):
            parts.append(
                f"TP{index + 1} {new.tps[index]:g} (signal {signal_id} has {old_tps[index]:g})"
            )
    if len(old_tps) != len(new.tps):
        parts.append(f"{len(new.tps)} take-profits (signal {signal_id} has {len(old_tps)})")
    return parts


#: Price equality at the trade hash's resolution (`relay_repo.same_price`).
_same = relay_repo.same_price


def _parse_utc(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _get_message(conn: sqlite3.Connection, *, channel_id: int, message_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM messages WHERE channel_id = ? AND message_id = ?",
        (channel_id, message_id),
    ).fetchone()


def _check_detector(detected_by: str) -> None:
    if detected_by not in DETECTORS:
        raise ValueError(f"unknown detector {detected_by!r}; expected one of {sorted(DETECTORS)}")
