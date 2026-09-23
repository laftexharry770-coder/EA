"""The one place the `metrics` table is written.

`metrics(day_key, name, value)` is a per-UTC-day counter: every row is
"how many times `name` happened on `day_key`". Each counter here is an
operational fact someone must be able to see later -- a signal that fanned
out to no account, a worker that died holding a lease, a history message
skipped after repeated failures, a message only the history poll found --
so each is written *in the same transaction* as the thing it counts.
`bump()` therefore only executes; it never commits, and the caller's
transaction (the `Writer`'s job, or `outbox.sweep_expired`'s explicit
`BEGIN IMMEDIATE`) decides whether the count lands.

Before Task 9 this upsert existed three times (`messages_repo.
_record_metric`, `outbox._bump_metric`, `listener._bump_metric`). They
now all call this module; a fourth copy is a review finding.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


def day_key_for(now_utc: datetime) -> str:
    """The UTC calendar day of `now_utc`, as `YYYY-MM-DD`.

    Converts an aware non-UTC datetime to UTC first, so a counter never
    lands on a local-time day. Refuses a naive datetime rather than
    guessing its timezone: every "now" in this system is injected as
    aware UTC, so a naive one is a bug at the call site."""
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError(
            f"day_key_for() needs a timezone-aware datetime, got naive {now_utc!r}"
        )
    return now_utc.astimezone(UTC).date().isoformat()


def bump(conn: sqlite3.Connection, *, day_key: str, name: str, value: int = 1) -> None:
    """`metrics(day_key, name) += value`, creating the row if needed.

    Runs inside the caller's transaction and never commits. `value` must
    be a positive int: a zero or negative bump is never what a caller
    means, and `True` would silently count as 1."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"metric bump value must be a positive int, got {value!r}")
    conn.execute(
        "INSERT INTO metrics (day_key, name, value) VALUES (?, ?, ?) "
        "ON CONFLICT(day_key, name) DO UPDATE SET value = value + excluded.value",
        (day_key, name, value),
    )
