"""The one place the `alerts` table is written: operator alerts, durably.

An alert is something a human must look at -- a live trade whose signal
was deleted, a signal re-aimed by an edit and therefore invalidated. Plan
3 builds the real notifier and acknowledgement; until then every alert is
a row (created by migration 2), so nothing an operator must see is lost
to a log line that scrolled away. Each is also logged, at WARNING or
CRITICAL, when it is recorded.

Like `metrics.bump`, `record()` only executes: it runs inside the
caller's transaction and never commits, so an alert lands if and only if
the change it describes lands (the edit or deletion job that raised it).
The log line is emitted when the row is written; if that job then rolls
back, the job's own failure is logged as an error too and the job is
retried, raising the alert again.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

logger = logging.getLogger(__name__)

AlertLevel = Literal["WARNING", "CRITICAL"]

#: CRITICAL, recorded by `xaucopier watch` on exit 3: Telegram refuses this
#: session (logged out, revoked, duplicated; the account deactivated,
#: banned or frozen). Fix: run setup-login again.
TELEGRAM_SESSION_DEAD = "TELEGRAM_SESSION_DEAD"
#: CRITICAL, recorded by `xaucopier watch` when Telegram rejects the
#: api_id/api_hash (exit 5). Fix: a new pair from my.telegram.org.
TELEGRAM_API_ID_REJECTED = "TELEGRAM_API_ID_REJECTED"
#: WARNING, recorded by `xaucopier watch` at start when it cancelled trade
#: entries still waiting for signals of channels that are switched off
#: (final review F15): nothing reads those channels any more, so nothing
#: would see their posters withdraw or change those signals.
CHANNEL_OFF_ENTRIES_SKIPPED = "CHANNEL_OFF_ENTRIES_SKIPPED"
#: Alerts a successful read resolves: Telegram answering this session
#: proves the condition is gone. The poll acknowledges them (poll.py).
SESSION_ALERT_CODES: tuple[str, ...] = (TELEGRAM_SESSION_DEAD, TELEGRAM_API_ID_REJECTED)
#: WARNING, recorded by `xaucopier watch` when Telegram says this account
#: cannot read a channel (it left it, was removed, or the channel is gone;
#: final fix FF20). Fix: join it again. Acknowledged by the poll once that
#: channel is read again (`ack_for_channels`).
TELEGRAM_CHANNEL_UNREADABLE = "TELEGRAM_CHANNEL_UNREADABLE"

_LOG_LEVELS: dict[str, int] = {"WARNING": logging.WARNING, "CRITICAL": logging.CRITICAL}


@dataclass(frozen=True, slots=True)
class Alert:
    alert_id: int
    created_at_utc: datetime
    level: AlertLevel
    code: str
    channel_id: int | None
    message_id: int | None
    signal_id: int | None
    detail: str
    acked: bool


def record(
    conn: sqlite3.Connection,
    *,
    now_utc: datetime,
    level: AlertLevel,
    code: str,
    detail: str,
    channel_id: int | None = None,
    message_id: int | None = None,
    signal_id: int | None = None,
) -> int:
    """Insert one unacknowledged alert and log it at `level`. Returns its
    `alert_id`. Never commits (see the module docstring).

    Refuses a naive `now_utc` (every "now" here is injected aware UTC; a
    naive one is a bug at the call site), a level other than WARNING or
    CRITICAL, and an empty code or detail -- an alert nobody can read is
    not an alert."""
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError(f"alerts.record() needs a timezone-aware now_utc, got naive {now_utc!r}")
    if level not in _LOG_LEVELS:
        raise ValueError(f"alert level must be one of {sorted(_LOG_LEVELS)}, got {level!r}")
    if not code:
        raise ValueError("alert code must be non-empty")
    if not detail:
        raise ValueError("alert detail must be non-empty")
    cursor = conn.execute(
        "INSERT INTO alerts (created_at_utc, level, code, channel_id, message_id, "
        "signal_id, detail, acked) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (
            now_utc.astimezone(UTC).isoformat(),
            level,
            code,
            channel_id,
            message_id,
            signal_id,
            detail,
        ),
    )
    alert_id = int(cursor.lastrowid)
    # Only what it is about: "channel=None message_id=None ..." told the
    # window nothing (final fix FF20).
    about = "".join(
        f" {label}={value}"
        for label, value in (
            ("channel", channel_id),
            ("message_id", message_id),
            ("signal_id", signal_id),
        )
        if value is not None
    )
    logger.log(_LOG_LEVELS[level], "ALERT %s #%s%s: %s", code, alert_id, about, detail)
    return alert_id


def unacked(conn: sqlite3.Connection) -> tuple[Alert, ...]:
    """Every unacknowledged alert, oldest first."""
    rows = conn.execute(
        "SELECT alert_id, created_at_utc, level, code, channel_id, message_id, "
        "signal_id, detail, acked FROM alerts WHERE acked = 0 ORDER BY alert_id"
    ).fetchall()
    return tuple(
        Alert(
            alert_id=row[0],
            created_at_utc=datetime.fromisoformat(row[1]),
            level=row[2],
            code=row[3],
            channel_id=row[4],
            message_id=row[5],
            signal_id=row[6],
            detail=row[7],
            acked=bool(row[8]),
        )
        for row in rows
    )


def ack_for_channels(conn: sqlite3.Connection, *, code: str, channel_ids: tuple[int, ...]) -> int:
    """Acknowledge every unacknowledged `code` alert about one of
    `channel_ids` -- the condition it reported is gone for that channel.
    Returns how many. Never commits."""
    if not channel_ids:
        return 0
    marks = ", ".join("?" for _ in channel_ids)
    cursor = conn.execute(
        f"UPDATE alerts SET acked = 1 WHERE acked = 0 AND code = ? AND channel_id IN ({marks})",
        (code, *channel_ids),
    )
    return cursor.rowcount


def ack_all(conn: sqlite3.Connection, *, codes: tuple[str, ...] | None = None) -> int:
    """Acknowledge every unacknowledged alert -- or, with `codes`, every one
    whose code is in `codes`. Returns how many. Never commits."""
    if codes is None:
        cursor = conn.execute("UPDATE alerts SET acked = 1 WHERE acked = 0")
    else:
        if not codes:
            return 0
        marks = ", ".join("?" for _ in codes)
        cursor = conn.execute(
            f"UPDATE alerts SET acked = 1 WHERE acked = 0 AND code IN ({marks})", tuple(codes)
        )
    return cursor.rowcount


def ack(conn: sqlite3.Connection, *, alert_id: int) -> None:
    """Acknowledge one alert. Raises `ValueError` if it does not exist or
    was already acknowledged, rather than silently doing nothing. Never
    commits."""
    cursor = conn.execute(
        "UPDATE alerts SET acked = 1 WHERE alert_id = ? AND acked = 0", (alert_id,)
    )
    if cursor.rowcount == 0:
        raise ValueError(f"alert_id={alert_id} is not an unacknowledged alert")
