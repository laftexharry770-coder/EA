"""Persistence for `signals`, `followups`, and the `channel_state`
parse-failure counter.

`messages_repo.ingest()` and `edits_repo` (Task 10) are the callers and
own the transaction (each runs inside a `Writer.submit()` job -- see
store/db.py); every function here just executes statements against the
`conn` it is given and never commits or rolls back.

`followups` holds two kinds of row since migration 2 (see migrations.py):
a message's *own* follow-up, `edit_seq = 0`, and the `AMEND_SL`/`AMEND_TP`
rows an edit of an executed signal derives, `edit_seq = 1, 2, ...`, keyed
by that signal's message. Everything that means "this message's
follow-up" filters on `edit_seq = 0`.

JSON serialisation lives here rather than on the domain types themselves,
because `domain/signal.py` must stay pure (no `json` import) -- see
tests/test_purity.py.

`signals.parsed_json.source` is the signal message's own
`messages.source` (live / poll / backfill), read from the stored message
row whenever the JSON is written -- never the parsed `Signal.source`,
which routing always leaves at 'live' (final review F39: every signal
the poll or backfill recovered said 'live', and Plan 3 is told to read
parsed_json at claim time). An edit keeps it: the message's provenance
is how it was first stored.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Literal

from xaucopier.domain.signal import FollowUp, Signal
from xaucopier.store import outbox

TradeStatus = Literal["pending", "in_flight", "executed"]


def signal_to_json(signal: Signal, *, source: str) -> str:
    """The persisted form of a parsed signal. Re-derived in full on every
    edit (see update_signal_on_edit) so `signals.parsed_json` always
    reflects the latest accepted parse, never a stale first cut.

    `source` is the message's real provenance (see the module docstring);
    `signal.source` is not used.
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
            "source": source,
        }
    )


def _message_source(conn: sqlite3.Connection, *, channel_id: int, message_id: int) -> str:
    """`messages.source` of a signal's own message. The message row is
    always stored first (signals has a foreign key to it)."""
    row = conn.execute(
        "SELECT source FROM messages WHERE channel_id = ? AND message_id = ?",
        (channel_id, message_id),
    ).fetchone()
    if row is None:
        raise LookupError(
            f"no stored message ({channel_id}, {message_id}) for this signal; the "
            "message row must be written before its signal"
        )
    return row[0]


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


def trade_status(conn: sqlite3.Connection, *, signal_id: int) -> TradeStatus:
    """Whether a signal has been traded -- by its ENTRY outbox rows, the
    proxy until Plan 3's legs table exists: one `DONE` is executed; one
    `CLAIMED` (a worker holds it, possibly mid order-send) is in flight.
    The one definition: `edits_repo` (what an edit or deletion does) and
    `relay_repo` (whether a withdrawn call was ever traded) both ask here.
    Replace it with the legs table when Plan 3 lands."""
    counts = outbox.state_counts(conn, kind="ENTRY", signal_id=signal_id)
    if counts.get("DONE"):
        return "executed"
    if counts.get("CLAIMED"):
        return "in_flight"
    return "pending"


def insert_signal(
    conn: sqlite3.Connection, *, channel_id: int, message_id: int, signal: Signal
) -> int:
    """A new NEW signal for an already-stored message; parsed_json's
    `source` is that message's `messages.source` (F39)."""
    source = _message_source(conn, channel_id=channel_id, message_id=message_id)
    cursor = conn.execute(
        "INSERT INTO signals (channel_id, message_id, parsed_json, trade_hash, "
        "first_trade_hash, state, version) VALUES (?, ?, ?, ?, ?, 'NEW', 1)",
        (
            channel_id,
            message_id,
            signal_to_json(signal, source=source),
            signal.content_hash,
            signal.content_hash,
        ),
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

    `first_trade_hash` is left alone: relay dedup keeps matching the call
    as it was first accepted (see `relay_repo.find_signal_relay`). So is
    parsed_json's `source`: it stays the message's `messages.source`.
    """
    source = _message_source(conn, channel_id=channel_id, message_id=message_id)
    conn.execute(
        "UPDATE signals SET parsed_json = ?, trade_hash = ?, version = version + 1 "
        "WHERE channel_id = ? AND message_id = ?",
        (signal_to_json(signal, source=source), signal.content_hash, channel_id, message_id),
    )


def get_signal(conn: sqlite3.Connection, *, channel_id: int, message_id: int) -> sqlite3.Row | None:
    """The whole `signals` row for a message, or None."""
    return conn.execute(
        "SELECT * FROM signals WHERE channel_id = ? AND message_id = ?",
        (channel_id, message_id),
    ).fetchone()


def set_signal_state(
    conn: sqlite3.Connection, *, signal_id: int, state: str, from_state: str = "NEW"
) -> bool:
    """Move a signal from `from_state` to `state`; False if it was not in
    `from_state` (so a terminal state is never overwritten)."""
    cursor = conn.execute(
        "UPDATE signals SET state = ? WHERE signal_id = ? AND state = ?",
        (state, signal_id, from_state),
    )
    return cursor.rowcount == 1


def mark_withdrawn(conn: sqlite3.Connection, *, signal_id: int, now_utc: datetime) -> None:
    """Record that the poster withdrew this signal (deleted it, or edited
    it into something that is no signal). Kept once set. `outbox.claim()`
    never hands out an ENTRY of a withdrawn signal, even one whose
    `state` stays NEW because it is live."""
    conn.execute(
        "UPDATE signals SET withdrawn_at_utc = ? WHERE signal_id = ? AND withdrawn_at_utc IS NULL",
        (now_utc.isoformat(), signal_id),
    )


def get_signal_id(conn: sqlite3.Connection, *, channel_id: int, message_id: int) -> int | None:
    row = conn.execute(
        "SELECT signal_id FROM signals WHERE channel_id = ? AND message_id = ?",
        (channel_id, message_id),
    ).fetchone()
    return row["signal_id"] if row is not None else None


def insert_followup(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    message_id: int,
    followup: FollowUp,
    target_signal_id: int | None = None,
    resolution_path: str | None = None,
) -> int:
    """A message's own follow-up (`edit_seq` 0). `target_signal_id` /
    `resolution_path` are the signal it replies to, when ingest could
    resolve it (`relay_repo.resolve_reply_target`: `'reply'` or
    `'relay-reply'`); NULL when it replies to nothing stored."""
    if (target_signal_id is None) != (resolution_path is None):
        raise ValueError("target_signal_id and resolution_path go together")
    cursor = conn.execute(
        "INSERT INTO followups (channel_id, message_id, op, parsed_json, reply_to_message_id, "
        "target_signal_id, resolution_path, edit_seq) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (
            channel_id,
            message_id,
            followup.op.value,
            followup_to_json(followup),
            followup.reply_to_message_id,
            target_signal_id,
            resolution_path,
        ),
    )
    return cursor.lastrowid


def insert_edit_amendment(
    conn: sqlite3.Connection,
    *,
    channel_id: int,
    message_id: int,
    followup: FollowUp,
    target_signal_id: int,
) -> int:
    """Store an AMEND_* follow-up derived from an edit of signal message
    `message_id`, as the next `edit_seq` under that message. Its target is
    known exactly -- the edited signal -- so `target_signal_id` is set and
    `resolution_path` is `'edit'`; Plan 3 need not resolve it by reply."""
    (next_seq,) = conn.execute(
        "SELECT COALESCE(MAX(edit_seq), 0) + 1 FROM followups "
        "WHERE channel_id = ? AND message_id = ?",
        (channel_id, message_id),
    ).fetchone()
    cursor = conn.execute(
        "INSERT INTO followups (channel_id, message_id, op, parsed_json, reply_to_message_id, "
        "target_signal_id, resolution_path, edit_seq) VALUES (?, ?, ?, ?, ?, ?, 'edit', ?)",
        (
            channel_id,
            message_id,
            followup.op.value,
            followup_to_json(followup),
            followup.reply_to_message_id,
            target_signal_id,
            next_seq,
        ),
    )
    return cursor.lastrowid


def update_followup_on_edit(
    conn: sqlite3.Connection, *, channel_id: int, message_id: int, followup: FollowUp
) -> None:
    conn.execute(
        "UPDATE followups SET op = ?, parsed_json = ?, reply_to_message_id = ? "
        "WHERE channel_id = ? AND message_id = ? AND edit_seq = 0",
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
        "SELECT followup_id FROM followups "
        "WHERE channel_id = ? AND message_id = ? AND edit_seq = 0",
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
