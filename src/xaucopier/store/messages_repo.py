"""Turns one routed Telegram message into `messages` / `signals` /
`followups` rows, with dedup.

This is the function the (not-yet-written) listener hands every incoming
update to, via `Writer.submit()` (store/db.py) so the whole call runs
inside one write transaction: a normal return commits, an exception rolls
back. `ingest()` itself must therefore never call `commit()`/`rollback()`.

Three outcomes for a *seen* (channel_id, message_id), not two -- this is
the load-bearing rule the brief calls out by name. A bare
`INSERT OR IGNORE` can only ever produce two outcomes (inserted / not),
because it never looks at content: it would silently swallow every
correction a poster makes, forever. So instead:

- unseen (channel_id, message_id)              -> route it, insert, "new"
- seen, content_hash unchanged                  -> "duplicate", no work
- seen, content_hash changed, edit_date_utc set -> "edited", record the
  correction in place (Task 10 decides what to *do* about it)

**Task 10: a seen message goes through the edit policy.** Every version
of an already-stored message -- whether the forward read, backfill or a
re-delivered update brought it -- is handed to `edits_repo.reconcile()`,
the same policy the `MessageEdited`/`MessageDeleted` paths use: a
direction change invalidates, an SL change on an executed signal queues
an `AMEND_SL`, a signal edited into chatter is a retraction, and so on
(see that module). Before Task 10 this branch re-parsed in place and
bumped the version whatever changed, so a BUY flipped to SELL during a
blackout and first seen by the poll's forward read was quietly re-aimed.
`ingest()` reports `"edited"` when the new version changed something
and `"duplicate"` otherwise (a cosmetic edit, an older version, or no
`edit_date`), so its three outcomes are the same ones as before.

`content_hash` for that comparison is computed from the *normalised
parse*, never the raw text: for a routed signal it is `Signal.content_hash`
(identity of the trade -- side/zone/sl/tps -- so decoration never counts
as a correction), and for anything else (followup, skip, parse_failed) it
is a hash of the normalised text, for the same reason.

Cross-channel relay dedup (spec G7): before a *signal* is inserted,
`relay_repo.find_signal_relay` asks whether it repeats a call we already
have -- a forward of a stored post (or of another stored copy of it),
however late (final review F09), or the same trade hash within
`relay_window_seconds` of this message's posted_at_utc, a withdrawn call
on the same channel excepted (F04). A match makes this one a
"relay_duplicate": the messages row is still inserted (every message is
a fixture; Plan 3's replay needs it), with `relay_of_signal_id` naming
the signal it repeats (F02: an edit or deletion of the copy reaches that
signal), logged and counted -- but no signals row is. A *follow-up*
records the signal it replies to (`followups.target_signal_id`, path
`'reply'`, or `'relay-reply'` through a copy) and is itself a
"relay_duplicate" -- stored, linked by `relay_of_followup_id`, logged and
counted, but no followups row and no outbox row -- when it repeats a
stored follow-up (F08). `UNIQUE(channel_id,
message_id)` cannot express "the other channel relayed the same call":
the user's own channel is itself a relay of a VIP channel, so the
identical trade legitimately shows up twice. See `relay_repo` for the
rules.

`now_utc` arrives as an explicit parameter rather than being read from
the wall clock, mirroring the pure core's rule that "now" always comes
from outside (controller ruling on this task's brief) -- it is what makes
the relay-dedup window testable at all.

Task 6 controller ruling: fan-out into the outbox rides in the *same*
transaction as the signal/followup insert, for outcome "new" only --
never for duplicate/edited/relay_duplicate, which touch no new trade
work at all. `outbox` is imported as a module (`from xaucopier.store
import outbox`, not `from ... import fan_out`) so a test can monkeypatch
`outbox.fan_out` to simulate a crash between the insert and the fan-out
and prove the whole thing rolls back atomically -- see
tests/test_outbox.py's atomicity tests, which own that guarantee.

`accounts` is a **required** keyword argument, deliberately with no
default. Task 6 review round 1 flagged the original `accounts=()`
default as a silent no-op: a caller that simply forgot to pass `accounts`
got a fully successful "new" outcome -- signal inserted, no exception, no
log, no metric, zero outbox rows -- which is exactly the
silently-dropped-signal failure mode the lease design exists to prevent,
just moved one layer up, out of the outbox and into ingest() itself.
Every call site (including tests/test_messages_repo.py, which does not
care about the outbox) now passes `accounts=` explicitly.

Round 2 found a second, more likely door to that same silent shape: an
`accounts` tuple that is non-empty but every account in it is disabled
-- a realistic operator misconfiguration (someone flips every account
off in accounts.toml), not just a forgotten argument. `outbox.fan_out_checked`
(until Task 10 `_fan_out_and_guard`, here) therefore checks `fan_out()`'s
actual *return value*
(`fanout_count == 0`) rather than guessing from the *input* (`not
accounts`), so the `ingest_new_no_accounts` metric fires for every route
to a zero-row fan-out, whichever one produced it -- and a `fanout_count`
that doesn't match the number of enabled accounts passed (0 when some
were expected, or any other mismatch) raises instead of silently
under- or over-fanning-out.

**Who posted it, and where a forward came from (migration 4).** ingest()
takes six optional keyword arguments, all stored on the message's row for
every kind and NULL when not given: ``sender_kind`` (``'user'`` /
``'channel'`` / ``'chat'``), ``sender_id``, ``is_post``, and for a
forwarded message ``fwd_from_channel_id`` (the -100... form, like
``channel_id``), ``fwd_from_post_id`` and ``fwd_from_date_utc`` (stored
in UTC). They are checked before anything is written: a
``fwd_from_channel_id`` not in the -100... form (every channel peer id is
below -10**12), a ``sender_id`` or ``fwd_from_post_id`` that is not an
integer (a bool is not one; a post id must be positive), a date that is
not a timezone-aware datetime, a non-bool ``is_post`` or an unknown
``sender_kind`` is a ``ValueError``. They are written once, when the
message is first stored; a later version of the same message does not
change them. (``messages.relay_of_signal_id`` is ingest's own finding,
not an argument.) The forward pair is also what recognises a relay copy
exactly (see above).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from xaucopier.parsing.router import RouteResult, route
from xaucopier.store import edits_repo, outbox, relay_repo, signals_repo

IngestOutcome = Literal["new", "duplicate", "edited", "relay_duplicate"]
SenderKind = Literal["user", "channel", "chat"]

_SENDER_KINDS = frozenset({"user", "channel", "chat"})


class _ChannelCfgLike(Protocol):
    """The two fields ingest() actually needs from a channel config --
    structural, not nominal, so store/ does not have to import the impure
    config.channels module. Mirrors parsing/router.py's own protocol.
    """

    channel_id: int
    parser: str


class _AccountCfgLike(Protocol):
    """The two fields fan_out() needs from an account config -- same
    structural-not-nominal rationale as _ChannelCfgLike above, and
    mirrors store/outbox.py's own identical protocol.
    """

    account_id: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class IngestResult:
    outcome: IngestOutcome
    signal_id: int | None
    followup_id: int | None
    kind: str
    detail: str
    #: For a "relay_duplicate" signal message: the signal it repeats
    #: (also stored as `messages.relay_of_signal_id`). None otherwise.
    relay_of_signal_id: int | None = None
    #: For a "relay_duplicate" follow-up: the follow-up it repeats (also
    #: stored as `messages.relay_of_followup_id`). None otherwise.
    relay_of_followup_id: int | None = None


def _get_message(conn: sqlite3.Connection, *, channel_id: int, message_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM messages WHERE channel_id = ? AND message_id = ?",
        (channel_id, message_id),
    ).fetchone()


#: Every channel peer id is below this: -(10**12 + the channel's own id).
_CHANNEL_PEER_ID_BASE = -(10**12)


def _is_int(value: object) -> bool:
    """An int that is not a bool (True is an int in Python)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _provenance_columns(
    *,
    sender_kind: str | None,
    sender_id: int | None,
    is_post: bool | None,
    fwd_from_channel_id: int | None,
    fwd_from_post_id: int | None,
    fwd_from_date_utc: datetime | None,
) -> dict[str, Any]:
    """The migration-4 provenance columns as stored, checked first -- see
    the module docstring. Raises ValueError, before anything is written."""
    if sender_kind is not None and not (
        isinstance(sender_kind, str) and sender_kind in _SENDER_KINDS
    ):
        raise ValueError(
            f"sender_kind must be one of {sorted(_SENDER_KINDS)} or None, got {sender_kind!r}"
        )
    if sender_id is not None and not _is_int(sender_id):
        raise ValueError(f"sender_id must be an integer or None, got {sender_id!r}")
    if is_post is not None and not isinstance(is_post, bool):
        raise ValueError(f"is_post must be True, False or None, got {is_post!r}")
    if fwd_from_channel_id is not None and not (
        _is_int(fwd_from_channel_id) and fwd_from_channel_id < _CHANNEL_PEER_ID_BASE
    ):
        # Every channel's peer id is -(10**12 + its Telegram id): the
        # -100... form of channel_id. A bare negated id would never match
        # a stored channel_id in the forward lookup (final review F09).
        raise ValueError(
            "fwd_from_channel_id must be in the same -100... form as channel_id, "
            f"got {fwd_from_channel_id!r}"
        )
    if fwd_from_post_id is not None and not (_is_int(fwd_from_post_id) and fwd_from_post_id > 0):
        raise ValueError(
            f"fwd_from_post_id must be a positive integer or None, got {fwd_from_post_id!r}"
        )
    if fwd_from_date_utc is not None and not (
        isinstance(fwd_from_date_utc, datetime) and fwd_from_date_utc.utcoffset() is not None
    ):
        raise ValueError(
            f"fwd_from_date_utc must be a timezone-aware datetime, got {fwd_from_date_utc!r}"
        )
    return {
        "sender_kind": sender_kind,
        "sender_id": sender_id,
        "is_post": is_post,  # sqlite3 stores True/False as 1/0
        "fwd_from_channel_id": fwd_from_channel_id,
        "fwd_from_post_id": fwd_from_post_id,
        "fwd_from_date_utc": (
            None if fwd_from_date_utc is None else fwd_from_date_utc.astimezone(UTC).isoformat()
        ),
    }


def _insert_message(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    message_id: int,
    kind: str,
    content_hash: str,
    raw_text: str,
    edit_date_utc: datetime | None,
    received_at_utc: datetime,
    posted_at_utc: datetime,
    source: str,
    provenance: dict[str, Any],
) -> None:
    extra_columns = "".join(f", {name}" for name in provenance)
    conn.execute(
        "INSERT INTO messages "
        "(channel_id, message_id, kind, content_hash, raw_text, edit_date_utc, "
        f" received_at_utc, posted_at_utc, source{extra_columns}) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?{', ?' * len(provenance)})",
        (
            channel_id,
            message_id,
            kind,
            content_hash,
            raw_text,
            edit_date_utc.isoformat() if edit_date_utc is not None else None,
            received_at_utc.isoformat(),
            posted_at_utc.isoformat(),
            source,
            *provenance.values(),
        ),
    )


def ingest(
    conn: sqlite3.Connection,
    *,
    channel_cfg: _ChannelCfgLike,
    message_id: int,
    text: str,
    posted_at_utc: datetime,
    edit_date_utc: datetime | None,
    reply_to_message_id: int | None,
    source: str,
    now_utc: datetime,
    accounts: tuple[_AccountCfgLike, ...],
    relay_window_seconds: int = 900,
    sender_kind: SenderKind | None = None,
    sender_id: int | None = None,
    is_post: bool | None = None,
    fwd_from_channel_id: int | None = None,
    fwd_from_post_id: int | None = None,
    fwd_from_date_utc: datetime | None = None,
) -> IngestResult:
    channel_id = channel_cfg.channel_id
    provenance = _provenance_columns(
        sender_kind=sender_kind,
        sender_id=sender_id,
        is_post=is_post,
        fwd_from_channel_id=fwd_from_channel_id,
        fwd_from_post_id=fwd_from_post_id,
        fwd_from_date_utc=fwd_from_date_utc,
    )

    route_result = route(
        text,
        channel_cfg=channel_cfg,
        message_id=message_id,
        posted_at_utc=posted_at_utc,
        reply_to_message_id=reply_to_message_id,
    )
    new_hash = edits_repo.content_hash_for(route_result, text)

    existing = _get_message(conn, channel_id=channel_id, message_id=message_id)

    if existing is None:
        return _ingest_new(
            conn,
            channel_id=channel_id,
            message_id=message_id,
            text=text,
            route_result=route_result,
            new_hash=new_hash,
            posted_at_utc=posted_at_utc,
            edit_date_utc=edit_date_utc,
            source=source,
            now_utc=now_utc,
            accounts=accounts,
            relay_window_seconds=relay_window_seconds,
            provenance=provenance,
        )

    edit = edits_repo.reconcile(
        conn,
        existing=existing,
        channel_cfg=channel_cfg,
        message_id=message_id,
        text=text,
        route_result=route_result,
        new_hash=new_hash,
        edit_date_utc=edit_date_utc,
        now_utc=now_utc,
        accounts=accounts,
        detected_by=source,
    )
    return IngestResult(
        outcome="edited" if edit.changed else "duplicate",
        signal_id=edit.signal_id,
        followup_id=edit.followup_id,
        kind=route_result.kind,
        detail=edit.detail,
    )


def _ingest_new(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    message_id: int,
    text: str,
    route_result: RouteResult,
    new_hash: str,
    posted_at_utc: datetime,
    edit_date_utc: datetime | None,
    source: str,
    now_utc: datetime,
    accounts: tuple[_AccountCfgLike, ...],
    relay_window_seconds: int,
    provenance: dict[str, Any],
) -> IngestResult:
    kind = route_result.kind

    # raw_text is stored verbatim for every outcome, including skip and
    # parse_failed -- every message becomes a replay fixture.
    _insert_message(
        conn,
        channel_id=channel_id,
        message_id=message_id,
        kind=kind,
        content_hash=new_hash,
        raw_text=text,
        edit_date_utc=edit_date_utc,
        received_at_utc=now_utc,
        posted_at_utc=posted_at_utc,
        source=source,
        provenance=provenance,
    )

    if kind == "parse_failed":
        signals_repo.increment_parse_failures(conn, channel_id=channel_id)
        return IngestResult(
            outcome="new", signal_id=None, followup_id=None, kind=kind, detail=route_result.detail
        )

    if kind == "skip":
        # Ordinary chatter must never touch the parse-failure counter --
        # see router.py's module docstring for why that distinction
        # exists at all (it is what keeps PAUSE_NEW a template-change
        # detector rather than a chatter counter).
        return IngestResult(
            outcome="new", signal_id=None, followup_id=None, kind=kind, detail=route_result.detail
        )

    if kind == "followup":
        followup = route_result.followup
        # The signal it is about, when it replies to one we have: the
        # replied-to message's own, or the one that message is a copy of
        # (final review F08). Plan 3 resolves by this, not by the reply.
        target = relay_repo.resolve_reply_target(
            conn, channel_id=channel_id, reply_to_message_id=followup.reply_to_message_id
        )
        target_signal_id, resolution_path = target if target is not None else (None, None)
        relay = relay_repo.find_followup_relay(
            conn,
            channel_id=channel_id,
            message_id=message_id,
            op=followup.op.value,
            content_hash=new_hash,
            target_signal_id=target_signal_id,
            posted_at_utc=posted_at_utc,
            relay_window_seconds=relay_window_seconds,
            fwd_from_channel_id=provenance["fwd_from_channel_id"],
            fwd_from_post_id=provenance["fwd_from_post_id"],
        )
        if relay is not None:
            relay_repo.record_followup_copy(
                conn, channel_id=channel_id, message_id=message_id, match=relay, now_utc=now_utc
            )
            return IngestResult(
                outcome="relay_duplicate",
                signal_id=None,
                followup_id=None,
                kind=kind,
                detail=route_result.detail,
                relay_of_followup_id=relay.followup_id,
            )
        followup_id = signals_repo.insert_followup(
            conn,
            channel_id=channel_id,
            message_id=message_id,
            followup=followup,
            target_signal_id=target_signal_id,
            resolution_path=resolution_path,
        )
        # Same transaction as the followup insert above -- see the module
        # docstring and tests/test_outbox.py's atomicity tests.
        outbox.fan_out_checked(
            conn,
            kind="FOLLOWUP",
            signal_id=None,
            followup_id=followup_id,
            accounts=accounts,
            now_utc=now_utc,
        )
        return IngestResult(
            outcome="new", signal_id=None, followup_id=followup_id, kind=kind, detail=route_result.detail
        )

    # kind == "signal"
    signals_repo.reset_parse_failures(conn, channel_id=channel_id)
    signal = route_result.signal

    relay = relay_repo.find_signal_relay(
        conn,
        channel_id=channel_id,
        message_id=message_id,
        trade_hash=signal.content_hash,
        posted_at_utc=posted_at_utc,
        relay_window_seconds=relay_window_seconds,
        fwd_from_channel_id=provenance["fwd_from_channel_id"],
        fwd_from_post_id=provenance["fwd_from_post_id"],
    )
    if relay is not None:
        relay_repo.record_signal_copy(
            conn, channel_id=channel_id, message_id=message_id, match=relay, now_utc=now_utc
        )
        return IngestResult(
            outcome="relay_duplicate",
            signal_id=None,
            followup_id=None,
            kind=kind,
            detail=route_result.detail,
            relay_of_signal_id=relay.signal_id,
        )

    signal_id = signals_repo.insert_signal(
        conn, channel_id=channel_id, message_id=message_id, signal=signal
    )
    # Same transaction as the signal insert above -- see the module
    # docstring and tests/test_outbox.py's atomicity tests.
    outbox.fan_out_checked(
        conn,
        kind="ENTRY",
        signal_id=signal_id,
        followup_id=None,
        accounts=accounts,
        now_utc=now_utc,
    )
    return IngestResult(
        outcome="new", signal_id=signal_id, followup_id=None, kind=kind, detail=route_result.detail
    )
