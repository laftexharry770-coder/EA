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

**Precondition, enforced, not assumed.** Because :func:`claim` and
:func:`sweep_expired` open their own transaction, they require the
connection to have no transaction already pending -- and this is checked
up front via ``conn.in_transaction`` and raised as ``RuntimeError``, not
merely documented. The first cut of this module got that precondition
wrong in a dangerous direction: it assumed a connection already
mid-transaction would make ``conn.execute("BEGIN IMMEDIATE")`` raise
``OperationalError: cannot start a transaction within a transaction``,
which reads as "loud failure, safe". Task 6 review round 1 found that is
not what actually happens: assigning ``conn.isolation_level = None`` --
which both functions did unconditionally, before ever touching
``BEGIN`` -- is itself a CPython ``sqlite3`` operation that implicitly
*commits* any pending transaction on that connection as a side effect of
switching transaction-control modes. A connection handed in with
unrelated, deliberately-uncommitted work already pending would have that
work silently committed with no exception and no signal -- the opposite
of loud failure. The explicit ``conn.in_transaction`` check below runs
*before* ``isolation_level`` is touched at all, so the precondition
violation is now a clean raise with nothing committed. See
``test_claim_raises_if_connection_has_pending_transaction`` and
``test_sweep_expired_raises_if_connection_has_pending_transaction`` in
tests/test_outbox.py, which assert both the raise and (from a second,
independent connection) that the pending write never became visible.

**claim() and sweep_expired() are not Writer work -- call them on their
own connection, never the Writer's.** ``store/db.py``'s ``Writer`` exists
for exactly one reason: to keep SQLite's synchronous ``commit()`` off the
asyncio event loop in the *listener*, by handing every write to one
dedicated thread whose ``fn`` never manages its own transaction (a normal
return commits, an exception rolls back -- see ``Writer.submit``'s
docstring). :func:`claim` and :func:`sweep_expired` are the opposite
shape on purpose: they manage their *own* ``BEGIN IMMEDIATE`` transaction
internally, because that is what makes the claim atomic (see "Why BEGIN
IMMEDIATE" above) -- a self-managed transaction is precisely what
``Writer`` detects and rejects as misuse. Submitting either function
through a real ``Writer`` (``writer.submit(lambda c: outbox.claim(c,
...))``) does not fail cleanly: the future resolves with
``WriterMisuseError``, but by the time ``Writer`` notices, ``claim()``
has already called ``COMMIT`` itself and the row is durably ``CLAIMED``
on disk -- a worker that took the ``WriterMisuseError`` at face value
("no work happened") would be wrong, and that claimed row would sit
invisible until the next ``sweep_expired()`` lease sweep. This is
deliberate and pinned by test, not an oversight to be "fixed" by removing
:func:`claim`'s internal transaction (which would break the atomic-claim
guarantee the whole lease design rests on): Plan 3's execution worker is
a separate blocking process with its own ``db.connect(role="writer")``
connection and has no need of ``Writer`` at all -- ``Writer`` belongs to
the listener alone. See ``test_claim_through_writer_raises_writer_misuse_error_but_lands_durably``
(pins the rejected pattern) and ``test_claim_on_its_own_connection_succeeds``
(pins the correct one) in tests/test_outbox.py.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from xaucopier.store import metrics

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


def _require_no_pending_transaction(conn: sqlite3.Connection, *, caller: str) -> None:
    """claim() and sweep_expired() both drive their own explicit
    transaction by setting `conn.isolation_level = None` before issuing
    `BEGIN IMMEDIATE`. That assignment is not inert: CPython's sqlite3
    module implicitly commits any transaction already pending on the
    connection as a side effect of switching isolation_level. Checking
    `in_transaction` up front -- before isolation_level is touched at all
    -- turns a precondition violation into a clean, loud RuntimeError
    with nothing committed, instead of a silent commit of whatever
    unrelated work the caller had pending. See the module docstring.
    """
    if conn.in_transaction:
        raise RuntimeError(
            f"{caller}() must be called on a connection with no pending "
            "transaction -- it drives its own BEGIN IMMEDIATE, and doing "
            "that via conn.isolation_level = None on a connection with "
            "uncommitted work would silently commit that work first "
            "rather than raise. Commit or roll back whatever is pending "
            f"before calling {caller}()."
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

    Raises RuntimeError if `conn` already has a transaction pending --
    see `_require_no_pending_transaction` and the module docstring.
    """
    _require_no_pending_transaction(conn, caller="claim")

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

    Raises RuntimeError if `conn` already has a transaction pending --
    see `_require_no_pending_transaction` and the module docstring.
    """
    _require_no_pending_transaction(conn, caller="sweep_expired")

    cutoff_iso = (now_utc - timedelta(seconds=lease_seconds)).isoformat()
    day_key = metrics.day_key_for(now_utc)

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
                # Called through the module (not imported by name) so a test
                # can monkeypatch `metrics.bump` to crash between the reset
                # UPDATE and the metric write, and prove the pair is atomic.
                metrics.bump(
                    conn, day_key=day_key, name="outbox_lease_reclaimed", value=reclaimed
                )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.isolation_level = previous_isolation_level

    return reclaimed


def fan_out_checked(
    conn: sqlite3.Connection,
    *,
    kind: OutboxKind,
    signal_id: int | None,
    followup_id: int | None,
    accounts: tuple[_AccountCfgLike, ...],
    now_utc: datetime,
) -> int:
    """:func:`fan_out`, guarded against the silent zero-row fan-out.
    Returns the number of rows written. Never commits.

    - ``fanout_count == 0`` -- for *any* reason -- records the
      ``ingest_new_no_accounts`` metric. Task 6 review round 1 only
      checked ``not accounts``, which missed the more likely door round 2
      found: an ``accounts`` tuple that is non-empty but every account in
      it is disabled. Checking the actual output catches every route to
      the same silent shape.
    - ``fanout_count != enabled_count`` -- any mismatch -- contradicts
      :func:`fan_out`'s one-row-per-enabled-account contract and raises,
      rather than let a signal or follow-up silently end up with the
      wrong number of workers assigned.

    This was ``messages_repo._fan_out_and_guard`` until Task 10, when the
    AMEND_* follow-ups an edit derives (``store/edits_repo.py``) had to
    fan out under the same guard. It calls :func:`fan_out` through this
    module's globals, so a test that monkeypatches ``outbox.fan_out``
    still reaches every caller.
    """
    fanout_count = fan_out(
        conn, kind=kind, signal_id=signal_id, followup_id=followup_id, accounts=accounts
    )
    enabled_count = sum(1 for account in accounts if account.enabled)

    if fanout_count == 0:
        metrics.bump(conn, day_key=metrics.day_key_for(now_utc), name="ingest_new_no_accounts")

    if fanout_count != enabled_count:
        raise RuntimeError(
            f"fan_out(kind={kind!r}) wrote {fanout_count} outbox row(s) but "
            f"{enabled_count} of the accounts passed were enabled -- fan_out() "
            "guarantees exactly one row per enabled account, so this mismatch "
            "should be impossible. Refusing to let this signal/follow-up silently "
            "end up with the wrong number of workers assigned."
        )
    return fanout_count


def skip_queued(
    conn: sqlite3.Connection,
    *,
    kind: OutboxKind,
    signal_id: int | None = None,
    followup_id: int | None = None,
    reason: str,
) -> int:
    """Move one signal's (or follow-up's) ``QUEUED`` rows of ``kind`` to
    ``SKIPPED`` with ``reason``; returns how many. Used when a signal is
    invalidated or retracted, or a follow-up withdrawn, before a worker
    has taken it.

    Only ``QUEUED``: a ``CLAIMED`` row belongs to a worker that may be
    mid-send, and ``DONE``/``SKIPPED`` are final. Plan 3's worker must
    therefore re-check the signal's state after it claims (see the Task
    10 report). Never commits."""
    if (signal_id is None) == (followup_id is None):
        raise ValueError("skip_queued requires exactly one of signal_id or followup_id")
    if not reason:
        raise ValueError("skip_queued requires a reason")
    column = "signal_id" if signal_id is not None else "followup_id"
    cursor = conn.execute(
        f"UPDATE outbox SET state = 'SKIPPED', reason = ? "
        f"WHERE kind = ? AND {column} = ? AND state = 'QUEUED'",
        (reason, kind, signal_id if signal_id is not None else followup_id),
    )
    return cursor.rowcount


def state_counts(
    conn: sqlite3.Connection,
    *,
    kind: OutboxKind,
    signal_id: int | None = None,
    followup_id: int | None = None,
) -> dict[str, int]:
    """``{state: count}`` over one signal's (or follow-up's) rows of
    ``kind``. States with no rows are absent."""
    if (signal_id is None) == (followup_id is None):
        raise ValueError("state_counts requires exactly one of signal_id or followup_id")
    column = "signal_id" if signal_id is not None else "followup_id"
    rows = conn.execute(
        f"SELECT state, COUNT(*) FROM outbox WHERE kind = ? AND {column} = ? GROUP BY state",
        (kind, signal_id if signal_id is not None else followup_id),
    ).fetchall()
    return {row[0]: row[1] for row in rows}
