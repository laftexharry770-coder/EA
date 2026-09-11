"""The per-account work queue Plan 3's execution workers will drain: one
row per (signal-or-followup, account).

**The lease is the whole point.** A plain ``UPDATE ... SET
state='CLAIMED'`` is a one-way door: a worker killed between claiming a
row and its first durable write leaves that row ``CLAIMED`` forever --
invisible to the main loop (which only ever selects ``QUEUED``) and to
any recovery sweep that only looks for stuck ``INTENT`` legs that were
never written. That is a silently dropped trade signal, visible in no
report and no metric. Plan 1's design review found exactly this failure
mode in two of three candidate architectures.

So :func:`claim` matches ``state='QUEUED' OR (state='CLAIMED' AND
claimed_at_utc < now - lease)``: a dead claimant's row becomes claimable
again once its lease has expired. Every such reclaim is itself recorded
as a metric (``outbox_lease_reclaimed``, via :func:`sweep_expired`)
because a reclaim means a worker died mid-claim -- that is an operational
fact someone should see, not just a queue mechanic that quietly heals
itself.

**Why BEGIN IMMEDIATE, not a bare UPDATE ... RETURNING.** A single UPDATE
statement is already atomic with respect to *what it writes*, but Python's
``sqlite3`` module (legacy transaction-control mode) auto-opens a
*deferred* transaction before it: no lock is taken until the statement
actually needs one. Two connections evaluating the same claim query at
once can each acquire a SHARED (read) lock while planning/evaluating the
subquery, then both try to upgrade to a write lock at the same moment --
exactly the classic SQLite "database is locked" livelock, which
``busy_timeout`` does not reliably paper over because it is a lock
*upgrade* conflict, not a plain wait. ``BEGIN IMMEDIATE`` sidesteps this
by taking the write lock (RESERVED) up front, before any read happens: a
second connection's own ``BEGIN IMMEDIATE`` then simply blocks (retrying
under ``busy_timeout``) until the first commits, and by the time it gets
the lock the row is no longer claimable -- it gets ``None``, never an
error. :func:`claim` and :func:`sweep_expired` both therefore drive their
own explicit ``BEGIN``/``COMMIT``/``ROLLBACK`` (with ``conn.isolation_level``
set to ``None`` for the duration, exactly mirroring the established
pattern in ``migrations.py.migrate()`` -- see that module's docstring for
why the explicit dance is needed at all in legacy transaction-control
mode) rather than leaving Python's implicit per-statement transaction
handling to do it.

:func:`fan_out` and :func:`mark`, by contrast, do **not** manage their
own transaction: :func:`fan_out` is meant to run inside whatever
transaction its caller already has open. For the signal/followup insert
in ``messages_repo.ingest()`` this matters concretely -- it is called
*in that same transaction*, for outcome ``"new"`` only, so a crash (or
injected exception) between the insert and the fan-out leaves neither
row behind once the Writer (or a test) rolls back. See
``messages_repo.py``'s call site.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

OutboxKind = Literal["ENTRY", "FOLLOWUP"]
OutboxState = Literal["QUEUED", "CLAIMED", "DONE", "SKIPPED"]

_DEFAULT_LEASE_SECONDS = 90


class _AccountCfgLike(Protocol):
    """The two fields fan_out() actually needs from an account config --
    structural, not nominal, so store/ does not import the impure
    config.accounts module. Mirrors messages_repo.py's own
    _ChannelCfgLike protocol for the same reason.
    """

    account_id: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class OutboxRow:
    outbox_id: int
    account_id: str
    kind: OutboxKind
    signal_id: int | None
    followup_id: int | None
    state: OutboxState
    reason: str | None
    claimed_at_utc: datetime | None
    claimed_by_pid: int | None
    attempts: int


def _row_to_outbox(row: sqlite3.Row) -> OutboxRow:
    claimed_at = row["claimed_at_utc"]
    return OutboxRow(
        outbox_id=row["outbox_id"],
        account_id=row["account_id"],
        kind=row["kind"],
        signal_id=row["signal_id"],
        followup_id=row["followup_id"],
        state=row["state"],
        reason=row["reason"],
        claimed_at_utc=datetime.fromisoformat(claimed_at) if claimed_at is not None else None,
        claimed_by_pid=row["claimed_by_pid"],
        attempts=row["attempts"],
    )


def fan_out(
    conn: sqlite3.Connection,
    *,
    kind: OutboxKind,
    signal_id: int | None,
    followup_id: int | None,
    accounts: tuple[_AccountCfgLike, ...],
) -> int:
    """Write one QUEUED outbox row per **enabled** account. Returns the
    count written. A disabled account gets no row at all -- not a row
    that is immediately SKIPPED -- because it was never a candidate to
    trade this signal in the first place; there is nothing to explain.

    Does not commit or roll back: this runs inside whatever transaction
    the caller already has open (see the module docstring for why that
    matters to messages_repo.ingest()'s atomicity).
    """
    if (signal_id is None) == (followup_id is None):
        raise ValueError("fan_out requires exactly one of signal_id or followup_id")

    count = 0
    for account in accounts:
        if not account.enabled:
            continue
        conn.execute(
            "INSERT INTO outbox "
            "(account_id, kind, signal_id, followup_id, state, attempts) "
            "VALUES (?, ?, ?, ?, 'QUEUED', 0)",
            (account.account_id, kind, signal_id, followup_id),
        )
        count += 1
    return count


def claim(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    pid: int,
    now_utc: datetime,
    lease_seconds: int = _DEFAULT_LEASE_SECONDS,
) -> OutboxRow | None:
    """Atomically take the oldest claimable row for `account_id`: one
    that is QUEUED, or CLAIMED with an expired lease. Records
    `claimed_by_pid` / `claimed_at_utc` (= `now_utc`) and increments
    `attempts`. Returns None if nothing is claimable right now.

    `now_utc` is always a parameter, never read from the wall clock --
    it is what makes the lease-expiry rule testable at all.

    Manages its own BEGIN IMMEDIATE / COMMIT / ROLLBACK -- see the module
    docstring for why a bare UPDATE ... RETURNING is not enough to avoid
    a "database is locked" race between two concurrent claimants.
    """
    cutoff_iso = (now_utc - timedelta(seconds=lease_seconds)).isoformat()
    now_iso = now_utc.isoformat()

    previous_isolation_level = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "UPDATE outbox SET "
                "  state = 'CLAIMED', "
                "  claimed_by_pid = ?, "
                "  claimed_at_utc = ?, "
                "  attempts = attempts + 1 "
                "WHERE outbox_id = ("
                "  SELECT outbox_id FROM outbox "
                "  WHERE account_id = ? "
                "    AND (state = 'QUEUED' "
                "         OR (state = 'CLAIMED' AND claimed_at_utc < ?)) "
                "  ORDER BY outbox_id ASC "
                "  LIMIT 1"
                ") "
                "RETURNING *",
                (pid, now_iso, account_id, cutoff_iso),
            ).fetchone()
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.isolation_level = previous_isolation_level

    return _row_to_outbox(row) if row is not None else None


def mark(
    conn: sqlite3.Connection,
    *,
    outbox_id: int,
    state: Literal["DONE", "SKIPPED"],
    reason: str | None = None,
) -> None:
    """Transition a CLAIMED row to a terminal state (DONE or SKIPPED),
    recording `reason`. Raises if the row is not currently CLAIMED --
    marking a row a worker never actually held would silently paper over
    a lease that had already expired out from under it (reclaimed by
    someone else, or by sweep_expired()), which is exactly the kind of
    silent double-write the lease exists to prevent.

    Does not commit or roll back -- same contract as fan_out().
    """
    cursor = conn.execute(
        "UPDATE outbox SET state = ?, reason = ? WHERE outbox_id = ? AND state = 'CLAIMED'",
        (state, reason, outbox_id),
    )
    if cursor.rowcount == 0:
        raise ValueError(
            f"outbox_id={outbox_id} is not CLAIMED; refusing to mark it {state!r} "
            "(it may not exist, already be terminal, or have had its lease "
            "reclaimed by someone else)"
        )


def sweep_expired(
    conn: sqlite3.Connection,
    *,
    now_utc: datetime,
    lease_seconds: int = _DEFAULT_LEASE_SECONDS,
) -> int:
    """Reset every CLAIMED row whose lease has expired back to QUEUED,
    and record the count as the `outbox_lease_reclaimed` metric for
    `now_utc`'s UTC day -- in the *same* transaction as the reset, so the
    metric can never be written without the reset actually having landed
    (or vice versa). Returns the number of rows reset.

    A fresh (non-expired) claim is left alone: this only reclaims rows
    whose owner has had more than `lease_seconds` to make its first
    durable write and still hasn't.
    """
    cutoff_iso = (now_utc - timedelta(seconds=lease_seconds)).isoformat()
    day_key = now_utc.date().isoformat()

    previous_isolation_level = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                "UPDATE outbox SET state = 'QUEUED', claimed_by_pid = NULL, "
                "claimed_at_utc = NULL "
                "WHERE state = 'CLAIMED' AND claimed_at_utc < ?",
                (cutoff_iso,),
            )
            reclaimed = cursor.rowcount
            if reclaimed > 0:
                conn.execute(
                    "INSERT INTO metrics (day_key, name, value) "
                    "VALUES (?, 'outbox_lease_reclaimed', ?) "
                    "ON CONFLICT(day_key, name) DO UPDATE SET "
                    "value = value + excluded.value",
                    (day_key, reclaimed),
                )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.isolation_level = previous_isolation_level

    return reclaimed
