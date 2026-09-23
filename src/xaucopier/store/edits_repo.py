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

**A deletion inferred from absence can be reversed.** The window re-read
(`ingest/edits.py`) infers a deletion when a message stops coming back --
guarded there by a circuit breaker and a second read, but still an
inference. So a mark it made (`deleted_detected_by = 'window'`) is never
final: its row stays in the window, and any later version of the message
-- the window reading it back, or an edit event -- clears the mark, raises
a CRITICAL `DELETION_REVERSED`, and is then handled like any version,
so an SL edit of an executed signal still gets its AMEND. What the
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
from datetime import datetime
from typing import Literal, Protocol

from xaucopier.domain.signal import FollowUp, FollowUpOp, Signal
from xaucopier.parsing.normalize import normalize
from xaucopier.parsing.router import RouteResult, route
from xaucopier.store import alerts, metrics, outbox, signals_repo

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
#: WARNING: an executed signal's TP ladder changed length; legs cannot follow.
TP_LADDER_CHANGED_AFTER_ENTRY = "TP_LADDER_CHANGED_AFTER_ENTRY"
#: WARNING: a follow-up already dispatched to a worker was edited.
FOLLOWUP_EDITED_AFTER_DISPATCH = "FOLLOWUP_EDITED_AFTER_DISPATCH"
#: WARNING: a follow-up edited into something that is no instruction.
FOLLOWUP_RETRACTED_BY_EDIT = "FOLLOWUP_RETRACTED_BY_EDIT"
#: WARNING: a follow-up message was deleted.
FOLLOWUP_DELETED = "FOLLOWUP_DELETED"
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

TradeStatus = Literal["pending", "in_flight", "executed"]


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


def trade_status(conn: sqlite3.Connection, *, signal_id: int) -> TradeStatus:
    """Whether a signal has been traded -- by its ENTRY outbox rows, the
    proxy until Plan 3's legs table exists (see the module docstring)."""
    counts = outbox.state_counts(conn, kind="ENTRY", signal_id=signal_id)
    if counts.get("DONE"):
        return "executed"
    if counts.get("CLAIMED"):
        return "in_flight"
    return "pending"


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
        channel_id=channel_id,
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
    channel_id: int,
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
    end here."""
    _check_detector(detected_by)
    stored_kind = existing["kind"]
    stored_hash = existing["content_hash"]
    ctx = _Ctx(
        conn=conn,
        channel_id=channel_id,
        message_id=message_id,
        now_utc=now_utc,
        edit_date_utc=edit_date_utc,
        accounts=accounts,
        detected_by=detected_by,
        signal_id=signals_repo.get_signal_id(conn, channel_id=channel_id, message_id=message_id),
        followup_id=signals_repo.get_followup_id(
            conn, channel_id=channel_id, message_id=message_id
        ),
    )

    reversal: tuple[int, ...] = ()
    if existing["deleted_at_utc"] is not None:
        if existing["deleted_detected_by"] != WINDOW_DETECTOR:
            # Telegram said so (a MessageDeleted event): final.
            return ctx.result("deleted", stored_kind, "already deleted; an edit of it is ignored")
        # Only inferred, from absence -- and here the message is.
        reversal = (_reverse_window_deletion(ctx, existing),)
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
        return _edit_of_signal(ctx, route_result)
    if stored_kind == "followup":
        return _edit_of_followup(ctx, route_result)
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
    row = (
        signals_repo.get_signal(ctx.conn, channel_id=ctx.channel_id, message_id=ctx.message_id)
        if ctx.signal_id is not None
        else None
    )
    if row is not None:
        consequence = (
            f"signal {row['signal_id']} is {row['state']}"
            f"{' and marked withdrawn' if row['withdrawn_at_utc'] else ''}; entries skipped "
            "when it was withdrawn stay skipped, and claim() will not hand out its "
            "remaining ones. Trade status: "
            f"{trade_status(ctx.conn, signal_id=row['signal_id'])}"
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


def _edit_of_signal(ctx: _Ctx, route_result: RouteResult) -> EditResult:
    new_kind = route_result.kind
    row = (
        signals_repo.get_signal(ctx.conn, channel_id=ctx.channel_id, message_id=ctx.message_id)
        if ctx.signal_id is not None
        else None
    )
    if row is None:
        # Never traded: a relay duplicate, or a signal only an edit made.
        # Nothing to correct, and still nothing to trade.
        logger.info(
            "edit of signal message %s/%s, which has no trade on file (now %s); recorded",
            ctx.channel_id,
            ctx.message_id,
            new_kind,
        )
        return ctx.result("recorded", new_kind, "no trade on file for this signal")

    status = trade_status(ctx.conn, signal_id=row["signal_id"])
    if row["state"] != "NEW":
        alert = ctx.alert(
            "WARNING",
            EDIT_OF_CLOSED_SIGNAL,
            f"signal {row['signal_id']} is {row['state']} and stays so; the poster edited "
            f"it again (it now reads as {new_kind}: {_describe(route_result)}). Trade "
            f"status: {status}. Nothing was changed or queued.",
        )
        return ctx.result("recorded", new_kind, "edit of a closed signal", alert_ids=(alert,))

    if new_kind == "signal":
        return _signal_to_signal(ctx, row, status, route_result.signal)
    if new_kind == "parse_failed":
        return _invalidate(
            ctx,
            row,
            status,
            code=SIGNAL_UNPARSEABLE_AFTER_EDIT,
            reason=f"the edited text no longer parses ({route_result.detail})",
            new_kind=new_kind,
        )
    # skip or followup: the call is withdrawn -- a deletion in all but name.
    return _withdraw_signal(
        ctx,
        row,
        status,
        via=f"edited into {new_kind} ({_describe(route_result)})",
        pending_code=SIGNAL_RETRACTED_BY_EDIT,
        live_code=LIVE_SIGNAL_RETRACTED_BY_EDIT,
        new_kind=new_kind,
    )


def _signal_to_signal(
    ctx: _Ctx, row: sqlite3.Row, status: TradeStatus, new: Signal
) -> EditResult:
    old = json.loads(row["parsed_json"])
    reaimed = []
    if old["side"] != new.side.value:
        reaimed.append(f"side {old['side']} -> {new.side.value}")
    if old["symbol_alias"] != new.symbol_alias:
        reaimed.append(f"symbol {old['symbol_alias']} -> {new.symbol_alias}")
    if not (_same(old["zone"]["lo"], new.zone.lo) and _same(old["zone"]["hi"], new.zone.hi)):
        reaimed.append(
            f"entry {old['zone']['lo']}-{old['zone']['hi']} -> {new.zone.lo}-{new.zone.hi}"
        )
    if reaimed:
        return _invalidate(
            ctx,
            row,
            status,
            code=SIGNAL_INVALIDATED_BY_EDIT,
            reason="the edit re-aims the trade: " + "; ".join(reaimed),
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
        # The hash is over these same fields, so this is unreachable in
        # practice; a hash change with equal levels is still nothing to do.
        return ctx.result("cosmetic", "signal", "levels unchanged")

    signal_id = row["signal_id"]
    signals_repo.update_signal_on_edit(
        ctx.conn, channel_id=ctx.channel_id, message_id=ctx.message_id, signal=new
    )
    change = _describe_levels(old, new)
    amendments: list[int] = []
    raised: list[int] = []
    if status != "pending":
        raw = f"edit of signal {signal_id} ({ctx.channel_id}/{ctx.message_id}): {change}"
        if sl_changed:
            amendments.append(_amend(ctx, signal_id, FollowUpOp.AMEND_SL, new.sl, None, raw))
        for index, price in tp_changes:
            amendments.append(_amend(ctx, signal_id, FollowUpOp.AMEND_TP, price, index, raw))
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


def _amend(
    ctx: _Ctx,
    signal_id: int,
    op: FollowUpOp,
    price: float,
    tp_index: int | None,
    raw: str,
) -> int:
    followup = FollowUp(
        channel_id=ctx.channel_id,
        message_id=ctx.message_id,
        op=op,
        posted_at_utc=ctx.edit_date_utc,
        reply_to_message_id=ctx.message_id,
        price_hint=price,
        tp_index_hint=tp_index,
        raw=raw,
    )
    followup_id = signals_repo.insert_edit_amendment(
        ctx.conn,
        channel_id=ctx.channel_id,
        message_id=ctx.message_id,
        followup=followup,
        target_signal_id=signal_id,
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
    skipped = outbox.skip_queued(
        ctx.conn, kind="ENTRY", signal_id=signal_id, reason=f"signal invalidated: {reason}"
    )
    alert = ctx.alert(
        "CRITICAL",
        code,
        f"signal {signal_id} is now INVALID: {reason}. {skipped} queued entr"
        f"{'y' if skipped == 1 else 'ies'} skipped. {_live_clause(status)}",
        signal_id=signal_id,
    )
    return ctx.result("invalidated", new_kind, reason, alert_ids=(alert,))


def _withdraw_signal(
    ctx: _Ctx,
    row: sqlite3.Row,
    status: TradeStatus,
    *,
    via: str,
    pending_code: str,
    live_code: str,
    new_kind: str | None,
) -> EditResult:
    """A signal the poster withdrew -- by deleting it, or by editing it
    into something that is no signal. Entries not yet taken are skipped
    either way. Unexecuted: VOIDED. Executed or in flight: flagged with a
    CRITICAL alert and *not* closed; `state` stays NEW so its live legs
    are still managed."""
    signal_id = row["signal_id"]
    signals_repo.mark_withdrawn(ctx.conn, signal_id=signal_id, now_utc=ctx.now_utc)
    skipped = outbox.skip_queued(
        ctx.conn, kind="ENTRY", signal_id=signal_id, reason=f"signal withdrawn: {via}"
    )
    entries = f"{skipped} queued entr{'y' if skipped == 1 else 'ies'} skipped"
    if status == "pending":
        signals_repo.set_signal_state(ctx.conn, signal_id=signal_id, state="VOIDED")
        alert = ctx.alert(
            "WARNING",
            pending_code,
            f"signal {signal_id} VOIDED: the poster withdrew it ({via}). Nothing had been "
            f"executed; {entries}.",
            signal_id=signal_id,
        )
        return ctx.result("voided", new_kind, via, alert_ids=(alert,), skipped=skipped)
    alert = ctx.alert(
        "CRITICAL",
        live_code,
        f"signal {signal_id} was withdrawn by the poster ({via}) but is {status}. Its live "
        f"legs were NOT closed -- a deletion or rewrite can be cosmetic -- review them by "
        f"hand. {entries}.",
        signal_id=signal_id,
    )
    return ctx.result("flagged", new_kind, via, alert_ids=(alert,), skipped=skipped)


def _edit_of_followup(ctx: _Ctx, route_result: RouteResult) -> EditResult:
    new_kind = route_result.kind
    followup_id = ctx.followup_id
    if followup_id is None:
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
    channel_id: int,
    message_id: int,
    now_utc: datetime,
    detected_by: str,
) -> DeletionResult:
    """A message is gone from its channel (the event said so, or the
    window re-read found `None` where it used to be). Marks it deleted --
    exactly once: a second report finds the mark -- and applies the
    deletion policy in the module docstring. Never closes anything."""
    _check_detector(detected_by)
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
        channel_id=channel_id,
        message_id=message_id,
        now_utc=now_utc,
        edit_date_utc=None,
        accounts=(),
        detected_by=detected_by,
        signal_id=signal_id,
        followup_id=followup_id,
    )

    # Decided on the signals row, not on `kind`: a signal an earlier edit
    # reclassified (INVALID, or withdrawn while live) can still have live
    # legs, and its message going away is still worth saying.
    row = (
        signals_repo.get_signal(conn, channel_id=channel_id, message_id=message_id)
        if signal_id is not None
        else None
    )
    if row is not None:
        status = trade_status(conn, signal_id=signal_id)
        if row["state"] == "NEW" or status != "pending":
            result = _withdraw_signal(
                ctx,
                row,
                status,
                via=f"message deleted, found by {detected_by}",
                pending_code=SIGNAL_VOIDED_BY_DELETION,
                live_code=LIVE_SIGNAL_DELETED,
                new_kind=kind,
            )
            return DeletionResult(
                result.outcome,  # "voided" or "flagged", both DeletionOutcomes
                message_id,
                kind,
                signal_id,
                followup_id,
                result.skipped,
                result.alert_ids,
            )
    elif kind == "followup" and followup_id is not None:
        partial = _withdraw_followup(
            ctx, followup_id, code=FOLLOWUP_DELETED, via=f"message deleted, found by {detected_by}"
        )
        return DeletionResult(
            "withdrawn", message_id, kind, signal_id, followup_id, partial.skipped, partial.alert_ids
        )

    logger.info(
        "channel %s message %s (%s) deleted, found by %s; marked",
        channel_id,
        message_id,
        kind,
        detected_by,
    )
    return DeletionResult("deleted", message_id, kind, signal_id, followup_id, 0, ())


# -- helpers -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Partial:
    alert_ids: tuple[int, ...]
    skipped: int


@dataclass(frozen=True, slots=True)
class _Ctx:
    """Everything one call's helpers share."""

    conn: sqlite3.Connection
    channel_id: int
    message_id: int
    now_utc: datetime
    edit_date_utc: datetime | None
    accounts: tuple[_AccountCfgLike, ...]
    detected_by: str
    signal_id: int | None
    followup_id: int | None

    def alert(
        self, level: alerts.AlertLevel, code: str, detail: str, *, signal_id: int | None = None
    ) -> int:
        return alerts.record(
            self.conn,
            now_utc=self.now_utc,
            level=level,
            code=code,
            channel_id=self.channel_id,
            message_id=self.message_id,
            signal_id=signal_id if signal_id is not None else self.signal_id,
            detail=f"{detail} [found by {self.detected_by}]",
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


def _same(a: float, b: float) -> bool:
    """Price equality at the resolution the trade hash uses (3 dp), so a
    diff never disagrees with the hash about whether anything changed."""
    return f"{a:.3f}" == f"{b:.3f}"


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
