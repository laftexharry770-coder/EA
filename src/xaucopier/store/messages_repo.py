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

`content_hash` for that comparison is computed from the *normalised
parse*, never the raw text: for a routed signal it is `Signal.content_hash`
(identity of the trade -- side/zone/sl/tps -- so decoration never counts
as a correction), and for anything else (followup, skip, parse_failed) it
is a hash of the normalised text, for the same reason.

Cross-channel relay dedup (spec G7): before a *signal* is inserted, any
already-accepted signal from *any* channel sharing the same trade_hash
within `relay_window_seconds` of this message's posted_at_utc makes this
one a "relay_duplicate" -- the messages row is still inserted (every
message is a fixture; Plan 3's replay needs it), but no signals row is.
`UNIQUE(channel_id, message_id)` cannot express "the other channel
relayed the same call": the user's own channel is itself a relay of a
VIP channel, so the identical trade legitimately shows up twice.

`now_utc` arrives as an explicit parameter rather than being read from
the wall clock, mirroring the pure core's rule that "now" always comes
from outside (controller ruling on this task's brief) -- it is what makes
the relay-dedup window testable at all.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from xaucopier.parsing.normalize import normalize
from xaucopier.parsing.router import RouteResult, route
from xaucopier.store import signals_repo

IngestOutcome = Literal["new", "duplicate", "edited", "relay_duplicate"]


class _ChannelCfgLike(Protocol):
    """The two fields ingest() actually needs from a channel config --
    structural, not nominal, so store/ does not have to import the impure
    config.channels module. Mirrors parsing/router.py's own protocol.
    """

    channel_id: int
    parser: str


@dataclass(frozen=True, slots=True)
class IngestResult:
    outcome: IngestOutcome
    signal_id: int | None
    followup_id: int | None
    kind: str
    detail: str


def _text_content_hash(text: str) -> str:
    """Identity for anything that is not a routed signal: sha1 of the
    normalised text. Normalised, not raw, so a cosmetic edit (an added
    emoji, reflowed whitespace) is never mistaken for a real correction --
    normalize() is exactly the fold every parser already applies before
    matching.
    """
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()


def _get_message(conn: sqlite3.Connection, *, channel_id: int, message_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM messages WHERE channel_id = ? AND message_id = ?",
        (channel_id, message_id),
    ).fetchone()


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
) -> None:
    conn.execute(
        "INSERT INTO messages "
        "(channel_id, message_id, kind, content_hash, raw_text, edit_date_utc, "
        " received_at_utc, posted_at_utc, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        ),
    )


def _update_message_edit(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    message_id: int,
    raw_text: str,
    content_hash: str,
    edit_date_utc: datetime,
) -> None:
    conn.execute(
        "UPDATE messages SET raw_text = ?, content_hash = ?, edit_date_utc = ? "
        "WHERE channel_id = ? AND message_id = ?",
        (raw_text, content_hash, edit_date_utc.isoformat(), channel_id, message_id),
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
    relay_window_seconds: int = 900,
) -> IngestResult:
    channel_id = channel_cfg.channel_id

    route_result = route(
        text,
        channel_cfg=channel_cfg,
        message_id=message_id,
        posted_at_utc=posted_at_utc,
        reply_to_message_id=reply_to_message_id,
    )
    kind = route_result.kind
    new_hash = (
        route_result.signal.content_hash if kind == "signal" else _text_content_hash(text)
    )

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
            relay_window_seconds=relay_window_seconds,
        )

    if existing["content_hash"] == new_hash:
        return _duplicate_result(conn, channel_id=channel_id, message_id=message_id, route_result=route_result)

    if edit_date_utc is None:
        # content_hash changed for a (channel_id, message_id) Telegram
        # never marked as edited. Under normal operation this cannot
        # happen -- message_id is immutable and content only changes via
        # a real Telegram edit, which always carries edit_date. Rather
        # than silently overwrite an audit-trail row on an unverified
        # change, treat it as a no-op: no write, flagged in `detail`.
        result = _duplicate_result(conn, channel_id=channel_id, message_id=message_id, route_result=route_result)
        return IngestResult(
            outcome=result.outcome,
            signal_id=result.signal_id,
            followup_id=result.followup_id,
            kind=result.kind,
            detail="content_hash changed with no edit_date_utc; ignored",
        )

    return _ingest_edit(
        conn,
        channel_id=channel_id,
        message_id=message_id,
        text=text,
        route_result=route_result,
        new_hash=new_hash,
        edit_date_utc=edit_date_utc,
    )


def _duplicate_result(
    conn: sqlite3.Connection, *, channel_id: int, message_id: int, route_result: RouteResult
) -> IngestResult:
    return IngestResult(
        outcome="duplicate",
        signal_id=signals_repo.get_signal_id(conn, channel_id=channel_id, message_id=message_id),
        followup_id=signals_repo.get_followup_id(conn, channel_id=channel_id, message_id=message_id),
        kind=route_result.kind,
        detail=route_result.detail,
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
    relay_window_seconds: int,
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
        followup_id = signals_repo.insert_followup(
            conn, channel_id=channel_id, message_id=message_id, followup=route_result.followup
        )
        return IngestResult(
            outcome="new", signal_id=None, followup_id=followup_id, kind=kind, detail=route_result.detail
        )

    # kind == "signal"
    signals_repo.reset_parse_failures(conn, channel_id=channel_id)
    signal = route_result.signal

    relay = signals_repo.find_relay_duplicate(
        conn,
        trade_hash=signal.content_hash,
        posted_at_utc=posted_at_utc,
        relay_window_seconds=relay_window_seconds,
    )
    if relay is not None:
        return IngestResult(
            outcome="relay_duplicate",
            signal_id=None,
            followup_id=None,
            kind=kind,
            detail=route_result.detail,
        )

    signal_id = signals_repo.insert_signal(
        conn, channel_id=channel_id, message_id=message_id, signal=signal
    )
    return IngestResult(
        outcome="new", signal_id=signal_id, followup_id=None, kind=kind, detail=route_result.detail
    )


def _ingest_edit(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    message_id: int,
    text: str,
    route_result: RouteResult,
    new_hash: str,
    edit_date_utc: datetime,
) -> IngestResult:
    _update_message_edit(
        conn,
        channel_id=channel_id,
        message_id=message_id,
        raw_text=text,
        content_hash=new_hash,
        edit_date_utc=edit_date_utc,
    )

    kind = route_result.kind
    signal_id: int | None = None
    followup_id: int | None = None

    if kind == "signal":
        signals_repo.update_signal_on_edit(
            conn, channel_id=channel_id, message_id=message_id, signal=route_result.signal
        )
        signal_id = signals_repo.get_signal_id(conn, channel_id=channel_id, message_id=message_id)
    elif kind == "followup":
        signals_repo.update_followup_on_edit(
            conn, channel_id=channel_id, message_id=message_id, followup=route_result.followup
        )
        followup_id = signals_repo.get_followup_id(conn, channel_id=channel_id, message_id=message_id)

    return IngestResult(
        outcome="edited", signal_id=signal_id, followup_id=followup_id, kind=kind, detail=route_result.detail
    )
