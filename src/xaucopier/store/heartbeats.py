"""The one place the `heartbeats` table is written: "when did X last happen".

`metrics` counts how many times something happened on a UTC day; it
cannot say *when* the last one was. `xaucopier status` runs in a separate
process from `xaucopier watch` and can only report what `watch` wrote
down, so the few moments `status` needs are stamped here, one row per
name, overwritten each time (migration 3):

- `poll_round_completed` -- the history poll finished a round. `detail`
  says how many enabled channels that round read successfully. A recent
  stamp is the proof that `watch` is alive *and* that Telegram is still
  answering this session's requests.
- `channel_read:<channel_id>` -- the last round in which that channel's
  forward history read succeeded. A channel whose stamp goes stale while
  the round stamp stays fresh is one Telegram has stopped answering for.
- `watch_started` / `watch_stopped` -- `watch` itself, the stop carrying
  its exit code in `detail`, which is how `status` tells "stopped" from
  "stopped because Telegram logged this session out".

Like `metrics.bump` and `alerts.record`, `beat()` only executes: it runs
inside the caller's transaction and never commits.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

#: Stamped by the poll at the end of every round.
POLL_ROUND = "poll_round_completed"
#: Prefix of the per-channel stamp; see `channel_read_name()`.
CHANNEL_READ_PREFIX = "channel_read:"
#: Stamped by `xaucopier watch` when it starts and when it stops.
WATCH_STARTED = "watch_started"
WATCH_STOPPED = "watch_stopped"


@dataclass(frozen=True, slots=True)
class Heartbeat:
    name: str
    at_utc: datetime
    detail: str | None


def channel_read_name(channel_id: int) -> str:
    """The heartbeat name for "channel `channel_id` was read"."""
    if isinstance(channel_id, bool) or not isinstance(channel_id, int):
        raise TypeError(f"channel_id must be an int, got {channel_id!r}")
    return f"{CHANNEL_READ_PREFIX}{channel_id}"


def beat(
    conn: sqlite3.Connection,
    *,
    name: str,
    now_utc: datetime,
    detail: str | None = None,
) -> None:
    """Stamp `name` at `now_utc` (replacing its previous stamp). Runs in the
    caller's transaction and never commits. Refuses an empty name and a
    naive `now_utc` -- every "now" in this system is injected aware UTC,
    and a naive one is a bug at the call site."""
    if not name:
        raise ValueError("heartbeat name must be non-empty")
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError(f"heartbeats.beat() needs a timezone-aware now_utc, got {now_utc!r}")
    conn.execute(
        "INSERT INTO heartbeats (name, at_utc, detail) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET at_utc = excluded.at_utc, detail = excluded.detail",
        (name, now_utc.astimezone(UTC).isoformat(), detail),
    )


def read_all(conn: sqlite3.Connection) -> tuple[Heartbeat, ...]:
    """Every stamp, ordered by name."""
    rows = conn.execute("SELECT name, at_utc, detail FROM heartbeats ORDER BY name").fetchall()
    return tuple(
        Heartbeat(name=row[0], at_utc=datetime.fromisoformat(row[1]), detail=row[2])
        for row in rows
    )
