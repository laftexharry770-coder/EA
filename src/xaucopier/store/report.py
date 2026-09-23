"""Everything `xaucopier status` reports, read from the store in one place.

Read-only by construction: every function here only runs `SELECT`s, and
`status` hands them a `db.connect_readonly()` connection, on which SQLite
itself refuses any write. `collect()` returns plain frozen data;
`cli/status.py` turns it into words.

What it gathers, and why each is here:

- **what was recorded** -- messages by kind (signal / followup / skip /
  parse_failed) and by how they arrived (live / backfill / poll);
- **the reliability counters** (`metrics`), today and all time --
  `missed_by_eventstream` above all, the count of posts the Telegram
  update stream dropped and the poll recovered, plus its edit and deletion
  counterparts and the counters that should stay at zero
  (`history_skipped`, `ingest_new_no_accounts`, `outbox_lease_reclaimed`);
- **every unacknowledged alert**, CRITICAL first;
- **signal states** -- INVALID and VOIDED counts in particular;
- **live signals whose message was deleted** -- state still `NEW` (Plan 3
  keeps managing their legs) but the post is gone;
- **SKIPPED outbox rows and their reasons**;
- **per channel**, the newest stored message, the history pointer and the
  parse-failure streak;
- **the heartbeats** `watch` and its poll stamp (migration 3), from which
  `status` tells whether `watch` is alive.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from xaucopier.store import alerts, heartbeats


@dataclass(frozen=True, slots=True)
class MetricTotal:
    name: str
    today: int
    all_time: int


@dataclass(frozen=True, slots=True)
class ChannelActivity:
    channel_id: int
    messages: int
    last_message_id: int | None
    last_posted_at_utc: datetime | None
    last_kind: str | None
    last_source: str | None
    #: `channel_state.last_processed_message_id`: where the next history read starts.
    pointer: int
    consecutive_parse_failures: int


@dataclass(frozen=True, slots=True)
class DeletedLiveSignal:
    signal_id: int
    channel_id: int
    message_id: int
    deleted_at_utc: datetime
    detected_by: str | None


@dataclass(frozen=True, slots=True)
class SkippedOutboxRow:
    outbox_id: int
    account_id: str
    kind: str
    signal_id: int | None
    followup_id: int | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class StatusReport:
    messages_by_kind: tuple[tuple[str, int], ...]
    messages_by_source: tuple[tuple[str, int], ...]
    signals_by_state: tuple[tuple[str, int], ...]
    withdrawn_signals: int
    followups: int
    outbox_by_state: tuple[tuple[str, int], ...]
    metrics: tuple[MetricTotal, ...]
    #: Unacknowledged, CRITICAL first, each level oldest first.
    alerts: tuple[alerts.Alert, ...]
    deleted_live_signals: tuple[DeletedLiveSignal, ...]
    skipped_outbox: tuple[SkippedOutboxRow, ...]
    channels: tuple[ChannelActivity, ...]
    heartbeats: tuple[heartbeats.Heartbeat, ...]

    @property
    def messages_total(self) -> int:
        return sum(count for _, count in self.messages_by_kind)

    def metric(self, name: str) -> MetricTotal:
        """The totals for `name`; zeros if it has never been counted."""
        for total in self.metrics:
            if total.name == name:
                return total
        return MetricTotal(name=name, today=0, all_time=0)

    def heartbeat(self, name: str) -> heartbeats.Heartbeat | None:
        for beat in self.heartbeats:
            if beat.name == name:
                return beat
        return None


def count(pairs: tuple[tuple[str, int], ...], key: str) -> int:
    """The count for `key` in one of the report's `(key, count)` tuples."""
    for name, value in pairs:
        if name == key:
            return value
    return 0


def _grouped(conn: sqlite3.Connection, sql: str) -> tuple[tuple[str, int], ...]:
    return tuple((row[0], int(row[1])) for row in conn.execute(sql).fetchall())


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def collect(conn: sqlite3.Connection, *, today: str) -> StatusReport:
    """Read the whole report. `today` is the UTC day key (`YYYY-MM-DD`,
    `metrics.day_key_for(now)`) the "today" column is counted for."""
    metrics_rows = conn.execute(
        "SELECT name, SUM(CASE WHEN day_key = ? THEN value ELSE 0 END), SUM(value) "
        "FROM metrics GROUP BY name ORDER BY name",
        (today,),
    ).fetchall()

    unacked = sorted(
        alerts.unacked(conn), key=lambda alert: (alert.level != "CRITICAL", alert.alert_id)
    )

    deleted_live = tuple(
        DeletedLiveSignal(
            signal_id=row[0],
            channel_id=row[1],
            message_id=row[2],
            deleted_at_utc=datetime.fromisoformat(row[3]),
            detected_by=row[4],
        )
        for row in conn.execute(
            "SELECT s.signal_id, s.channel_id, s.message_id, m.deleted_at_utc, "
            "m.deleted_detected_by FROM signals s JOIN messages m "
            "ON m.channel_id = s.channel_id AND m.message_id = s.message_id "
            "WHERE s.state = 'NEW' AND m.deleted_at_utc IS NOT NULL "
            "ORDER BY s.signal_id"
        ).fetchall()
    )

    skipped = tuple(
        SkippedOutboxRow(
            outbox_id=row[0],
            account_id=row[1],
            kind=row[2],
            signal_id=row[3],
            followup_id=row[4],
            reason=row[5],
        )
        for row in conn.execute(
            "SELECT outbox_id, account_id, kind, signal_id, followup_id, reason "
            "FROM outbox WHERE state = 'SKIPPED' ORDER BY outbox_id"
        ).fetchall()
    )

    return StatusReport(
        messages_by_kind=_grouped(
            conn, "SELECT kind, COUNT(*) FROM messages GROUP BY kind ORDER BY kind"
        ),
        messages_by_source=_grouped(
            conn, "SELECT source, COUNT(*) FROM messages GROUP BY source ORDER BY source"
        ),
        signals_by_state=_grouped(
            conn, "SELECT state, COUNT(*) FROM signals GROUP BY state ORDER BY state"
        ),
        withdrawn_signals=conn.execute(
            "SELECT COUNT(*) FROM signals WHERE withdrawn_at_utc IS NOT NULL"
        ).fetchone()[0],
        followups=conn.execute("SELECT COUNT(*) FROM followups").fetchone()[0],
        outbox_by_state=_grouped(
            conn, "SELECT state, COUNT(*) FROM outbox GROUP BY state ORDER BY state"
        ),
        metrics=tuple(
            MetricTotal(name=row[0], today=int(row[1]), all_time=int(row[2]))
            for row in metrics_rows
        ),
        alerts=tuple(unacked),
        deleted_live_signals=deleted_live,
        skipped_outbox=skipped,
        channels=_channels(conn),
        heartbeats=heartbeats.read_all(conn),
    )


def _channels(conn: sqlite3.Connection) -> tuple[ChannelActivity, ...]:
    """One entry per channel the store knows of -- from its messages or its
    pointer row -- ordered by id."""
    ids = sorted(
        {row[0] for row in conn.execute("SELECT DISTINCT channel_id FROM messages")}
        | {row[0] for row in conn.execute("SELECT channel_id FROM channel_state")}
    )
    activity: list[ChannelActivity] = []
    for channel_id in ids:
        total = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE channel_id = ?", (channel_id,)
        ).fetchone()[0]
        last = conn.execute(
            "SELECT message_id, posted_at_utc, kind, source FROM messages "
            "WHERE channel_id = ? ORDER BY message_id DESC LIMIT 1",
            (channel_id,),
        ).fetchone()
        state = conn.execute(
            "SELECT last_processed_message_id, consecutive_parse_failures "
            "FROM channel_state WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        activity.append(
            ChannelActivity(
                channel_id=channel_id,
                messages=total,
                last_message_id=last[0] if last is not None else None,
                last_posted_at_utc=_dt(last[1]) if last is not None else None,
                last_kind=last[2] if last is not None else None,
                last_source=last[3] if last is not None else None,
                pointer=state[0] if state is not None else 0,
                consecutive_parse_failures=state[1] if state is not None else 0,
            )
        )
    return tuple(activity)
