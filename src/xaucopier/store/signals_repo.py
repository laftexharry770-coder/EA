"""Persistence for `signals`, `followups`, and the `channel_state`
parse-failure counter.

`messages_repo.ingest()` is the only caller and owns the transaction (it
runs inside a `Writer.submit()` job -- see store/db.py); every function
here just executes statements against the `conn` it is given and never
commits or rolls back.

JSON serialisation lives here rather than on the domain types themselves,
because `domain/signal.py` must stay pure (no `json` import) -- see
tests/test_purity.py.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from xaucopier.domain.signal import FollowUp, Signal


def signal_to_json(signal: Signal) -> str:
    """The persisted form of a parsed signal. Re-derived in full on every
    edit (see update_signal_on_edit) so `signals.parsed_json` always
    reflects the latest accepted parse, never a stale first cut.
    """
    return json.dumps(
        {
            "side": signal.side.value,
            "symbol_alias": signal.symbol_alias,
            "zone": {"lo": signal.zone.lo, "hi": signal.zone.hi},
            "sl": signal.sl,
            "tps": list(signal.tps),
            "posted_at_utc": signal.posted_at_utc.isoformat(),
            "content_hash": signal.content_hash,
            "source": signal.source,
        }
    )


def followup_to_json(followup: FollowUp) -> str:
    return json.dumps(
        {
            "op": followup.op.value,
            "posted_at_utc": followup.posted_at_utc.isoformat(),
            "reply_to_message_id": followup.reply_to_message_id,
            "price_hint": followup.price_hint,
            "tp_index_hint": followup.tp_index_hint,
            "raw": followup.raw,
        }
    )


def find_relay_duplicate(
    conn: sqlite3.Connection,
    *,
    trade_hash: str,
    posted_at_utc: datetime,
    relay_window_seconds: int,
) -> sqlite3.Row | None:
    """Look for an already-accepted signal -- from *any* channel -- that
    shares `trade_hash` and whose own message was posted within
    `relay_window_seconds` of `posted_at_utc` (spec G7).

    `UNIQUE(channel_id, message_id)` on `signals` cannot express this: the
    user's channel is itself a relay of a VIP channel, so the same call
    legitimately arrives twice under two different (channel_id,
    message_id) pairs. `idx_signals_trade_hash` exists in schema.sql
    specifically to make this lookup cheap.

    Returns the first matching row (signal_id, channel_id, message_id,
    posted_at_utc) or None. Deliberately not restricted to a different
    channel than the caller's: two messages of the identical trade inside
    one channel, close together, are just as much a relay-shaped
    duplicate as a cross-channel one.
    """
    rows = conn.execute(
        "SELECT s.signal_id, s.channel_id, s.message_id, m.posted_at_utc "
        "FROM signals s "
        "JOIN messages m ON m.channel_id = s.channel_id AND m.message_id = s.message_id "
        "WHERE s.trade_hash = ?",
        (trade_hash,),
    ).fetchall()

    window = timedelta(seconds=relay_window_seconds)
    for row in rows:
        other_posted = datetime.fromisoformat(row["posted_at_utc"])
        if abs(other_posted - posted_at_utc) <= window:
            return row
    return None


def insert_signal(
    conn: sqlite3.Connection, *, channel_id: int, message_id: int, signal: Signal
) -> int:
    cursor = conn.execute(
        "INSERT INTO signals (channel_id, message_id, parsed_json, trade_hash, state, version) "
        "VALUES (?, ?, ?, ?, 'NEW', 1)",
        (channel_id, message_id, signal_to_json(signal), signal.content_hash),
    )
    return cursor.lastrowid


def update_signal_on_edit(
    conn: sqlite3.Connection, *, channel_id: int, message_id: int, signal: Signal
) -> None:
    """Re-record a corrected parse in place and bump `version`. Task 10
    decides what -- if anything -- to *do* about a live position when this
    happens; this just makes the correction faithfully visible.

    A no-op (matches zero rows) if the original message never produced a
    `signals` row in the first place -- e.g. it was a relay_duplicate.
    There is nothing to correct in that case; nothing to fabricate either.
    """
    conn.execute(
        "UPDATE signals SET parsed_json = ?, trade_hash = ?, version = version + 1 "
        "WHERE channel_id = ? AND message_id = ?",
        (signal_to_json(signal), signal.content_hash, channel_id, message_id),
    )


def get_signal_id(conn: sqlite3.Connection, *, channel_id: int, message_id: int) -> int | None:
    row = conn.execute(
        "SELECT signal_id FROM signals WHERE channel_id = ? AND message_id = ?",
        (channel_id, message_id),
    ).fetchone()
    return row["signal_id"] if row is not None else None


def insert_followup(
    conn: sqlite3.Connection, *, channel_id: int, message_id: int, followup: FollowUp
) -> int:
    cursor = conn.execute(
        "INSERT INTO followups (channel_id, message_id, op, parsed_json, reply_to_message_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            channel_id,
            message_id,
            followup.op.value,
            followup_to_json(followup),
            followup.reply_to_message_id,
        ),
    )
    return cursor.lastrowid


def update_followup_on_edit(
    conn: sqlite3.Connection, *, channel_id: int, message_id: int, followup: FollowUp
) -> None:
    conn.execute(
        "UPDATE followups SET op = ?, parsed_json = ?, reply_to_message_id = ? "
        "WHERE channel_id = ? AND message_id = ?",
        (
            followup.op.value,
            followup_to_json(followup),
            followup.reply_to_message_id,
            channel_id,
            message_id,
        ),
    )


def get_followup_id(conn: sqlite3.Connection, *, channel_id: int, message_id: int) -> int | None:
    row = conn.execute(
        "SELECT followup_id FROM followups WHERE channel_id = ? AND message_id = ?",
        (channel_id, message_id),
    ).fetchone()
    return row["followup_id"] if row is not None else None


def increment_parse_failures(conn: sqlite3.Connection, *, channel_id: int) -> None:
    """A parse_failed message: the channel's template may have changed.
    Three in a row trips PAUSE_NEW (spec 6.2) -- Plan 3 reads this
    counter and acts on it; this only records it.
    """
    conn.execute(
        "INSERT INTO channel_state (channel_id, consecutive_parse_failures) VALUES (?, 1) "
        "ON CONFLICT(channel_id) DO UPDATE SET "
        "consecutive_parse_failures = consecutive_parse_failures + 1",
        (channel_id,),
    )


def reset_parse_failures(conn: sqlite3.Connection, *, channel_id: int) -> None:
    """A clean signal parse: the template is fine again."""
    conn.execute(
        "INSERT INTO channel_state (channel_id, consecutive_parse_failures) VALUES (?, 0) "
        "ON CONFLICT(channel_id) DO UPDATE SET consecutive_parse_failures = 0",
        (channel_id,),
    )
