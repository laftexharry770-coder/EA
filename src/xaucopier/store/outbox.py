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
as a metric (``outbox_lease_reclaimed``) because a reclaim means a worker
died mid-claim -- that is an operational fact someone should see, not
just a queue mechanic that quietly heals itself. Both doors count it, in
the same transaction as the reclaim: :func:`sweep_expired` for each row
it re-queues, and :func:`claim` when the row it takes was CLAIMED, not
QUEUED (final review F13: claim() used to reclaim silently). A row the
sweep already re-queued is QUEUED again, so it is not counted twice.

**The lease clock is UTC, always.** ``claimed_at_utc`` is compared as an
ISO string, which only orders correctly when every value has the same
offset. :func:`claim` and :func:`sweep_expired` therefore refuse a naive
``now_utc`` and convert an aware one to UTC before building any string
(final review F14: a claim written at UTC-5 looked 5 hours old and was
reclaimed seconds later -- two workers holding one ENTRY -- and one
written at UTC+3 kept a dead worker's row for 3 hours).

**A result is only accepted from the claim it answers.** :func:`mark`
takes the fence :func:`claim` returned -- ``claimed_by_pid`` and
``attempts`` -- and matches both in its ``WHERE`` (final review F12).
A worker slower than the lease, whose row another worker has since
reclaimed (attempts moved on, even under the same pid), gets
:class:`StaleClaimError` and changes nothing; the live owner's mark
lands.

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

**An ENTRY is never handed out for a signal that is no longer
tradeable** (Task 10 review). A signal can be invalidated by an edit
(``state = 'INVALID'``) or withdrawn by its poster (``withdrawn_at_utc``
set -- a live one keeps ``state = 'NEW'`` so its legs are still managed)
while one of its ENTRY rows is CLAIMED by a worker. If that worker dies,
the lease expires and the row would come back: ``sweep_expired()`` would
re-queue it and ``claim()`` hand it to the next worker, which would open
the very trade the poster re-aimed or retracted. So both check the
signal, in their own ``BEGIN IMMEDIATE`` transaction:
``sweep_expired()`` SKIPs such an expired row instead of re-queueing it,
and ``claim()`` SKIPs every claimable ENTRY row of such a signal for the
account before it picks one -- so no path hands one out, including a row
re-queued before the signal died. Both count what they skipped in the
``outbox_dead_entry_skipped`` metric. FOLLOWUP rows are never refused
this way: a withdrawn or invalid signal that is live still needs its
legs managed. (A row already CLAIMED by a live worker cannot be stopped
here; ``store/edits_repo.py`` treats it as in flight, i.e. possibly
executed.)

**claim() and sweep_expired() are not Writer work -- call them on their
own connection, never the Writer's.** ``store/db.py``'s ``Writer`` exists
for exactly one reason: to keep SQLite's synchronous ``commit()`` off the
asyncio event loop in the *listener*, by handing every write to one
dedicated thread whose job runs inside a transaction the Writer opens
(``BEGIN IMMEDIATE``) and closes itself -- see ``Writer.submit``'s
docstring. :func:`claim` and :func:`sweep_expired` are the opposite shape
on purpose: they manage their *own* ``BEGIN IMMEDIATE`` transaction,
because that is what makes the claim atomic (see "Why BEGIN IMMEDIATE"
above). Routed through a Writer they now fail at once, on an empty queue
as on a full one, before anything is written: the Writer's transaction is
already open, so their no-pending-transaction precondition raises
``RuntimeError`` ("not Writer work"), and the stand-in connection a Writer
job gets would refuse their ``isolation_level`` switch anyway
(``WriterMisuseError``). Until final review F20 the Writer only noticed
*after* ``claim()`` had committed its own claim -- the future said
``WriterMisuseError`` while the row sat durably ``CLAIMED`` -- and on an
empty queue it looked healthy. Plan 3's execution worker is a separate
blocking process with its own ``db.connect(role="writer")`` connection and
has no need of ``Writer`` at all -- ``Writer`` belongs to the listener
alone. See ``test_claim_through_writer_fails_before_anything_lands`` and
``test_claim_on_its_own_connection_succeeds`` in tests/test_outbox.py.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from xaucopier.store import metrics

OutboxKind = Literal["ENTRY", "FOLLOWUP"]
OutboxState = Literal["QUEUED", "CLAIMED", "DONE", "SKIPPED"]

_DEFAULT_LEASE_SECONDS = 90

#: `metrics.name` for ENTRY rows claim()/sweep_expired() skipped because
#: their signal is no longer tradeable.
DEAD_ENTRY_SKIPPED_METRIC = "outbox_dead_entry_skipped"
#: `metrics.name` for rows sweep_expired() re-queued after a claimant's
#: lease expired -- a worker died mid-claim.
LEASE_RECLAIMED_METRIC = "outbox_lease_reclaimed"
#: `metrics.name` for a new signal or follow-up that fanned out to no
#: account at all (see fan_out_checked).
NO_ACCOUNTS_METRIC = "ingest_new_no_accounts"

#: An ENTRY row whose signal must never be traded again: invalidated (or
#: voided, or anything not NEW), or withdrawn by its poster. A signal_id
#: with no signals row at all is *not* dead -- nothing says so.
_DEAD_ENTRY = (
    "kind = 'ENTRY' AND signal_id IN ("
    "SELECT signal_id FROM signals WHERE state != 'NEW' OR withdrawn_at_utc IS NOT NULL)"
)


class StaleClaimError(ValueError):
    """:func:`mark` was given a claim that no longer holds the row: the
    lease was reclaimed by another worker (or by the same pid again --
    ``attempts`` moved on), the sweep re-queued it, it is already DONE or
    SKIPPED, or it does not exist. Nothing was changed. A ValueError, as
    mark()'s refusal always was."""


def _utc(now_utc: datetime, *, caller: str) -> datetime:
    """`now_utc` in UTC; a naive datetime is refused (see the module
    docstring: the lease clock is compared as ISO strings)."""
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError(f"{caller}() needs a timezone-aware now_utc, got naive {now_utc!r}")
    return now_utc.astimezone(UTC)


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
            f"before calling {caller}(). (A Writer job always has one open: "
            f"{caller}() is not Writer work -- call it on its own connection.)"
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
    it is what makes the lease-expiry rule testable at all. It must be
    timezone-aware (ValueError otherwise) and is converted to UTC before
    anything is compared or stored.

    Keep the returned row: its `claimed_by_pid` and `attempts` are the
    fence `mark()` requires.

    Taking a row that was CLAIMED (an expired lease: its worker died)
    rather than QUEUED bumps `outbox_lease_reclaimed` in the same
    transaction.

    An ENTRY row whose signal is no longer tradeable (INVALID, VOIDED,
    withdrawn) is never returned: in the same transaction, every such
    claimable row for the account is SKIPPED (see the module docstring)
    before the oldest remaining one is taken.

    Manages its own BEGIN IMMEDIATE / COMMIT / ROLLBACK -- see the module
    docstring for why a bare UPDATE ... RETURNING is not enough to avoid
    a "database is locked" race between two concurrent claimants.

    Raises RuntimeError if `conn` already has a transaction pending --
    see `_require_no_pending_transaction` and the module docstring.
    """
    now_utc = _utc(now_utc, caller="claim")
    _require_no_pending_transaction(conn, caller="claim")

    cutoff_iso = (now_utc - timedelta(seconds=lease_seconds)).isoformat()
    now_iso = now_utc.isoformat()

    previous_isolation_level = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            dead = conn.execute(
                "UPDATE outbox SET state = 'SKIPPED', "
                "reason = 'signal no longer tradeable (invalidated or withdrawn) at claim' "
                "WHERE account_id = ? "
                "  AND (state = 'QUEUED' OR (state = 'CLAIMED' AND claimed_at_utc < ?)) "
                f"  AND {_DEAD_ENTRY}",
                (account_id, cutoff_iso),
            ).rowcount
            if dead:
                metrics.bump(
                    conn,
                    day_key=metrics.day_key_for(now_utc),
                    name=DEAD_ENTRY_SKIPPED_METRIC,
                    value=dead,
                )
            # Pick first, under the write lock BEGIN IMMEDIATE holds, so
            # the state the row had -- QUEUED, or CLAIMED with an expired
            # lease -- is known (RETURNING only gives the new values).
            candidate = conn.execute(
                "SELECT outbox_id, state FROM outbox "
                "WHERE account_id = ? "
                "  AND (state = 'QUEUED' OR (state = 'CLAIMED' AND claimed_at_utc < ?)) "
                "ORDER BY outbox_id ASC "
                "LIMIT 1",
                (account_id, cutoff_iso),
            ).fetchone()
            row = None
            if candidate is not None:
                row = conn.execute(
                    "UPDATE outbox SET "
                    "  state = 'CLAIMED', "
                    "  claimed_by_pid = ?, "
                    "  claimed_at_utc = ?, "
                    "  attempts = attempts + 1 "
                    "WHERE outbox_id = ? "
                    "RETURNING *",
                    (pid, now_iso, candidate[0]),
                ).fetchone()
                if candidate[1] == "CLAIMED":
                    # A worker died holding this row (F13). Through the
                    # module, like sweep_expired(), so a test can prove the
                    # pair is atomic.
                    metrics.bump(
                        conn,
                        day_key=metrics.day_key_for(now_utc),
                        name=LEASE_RECLAIMED_METRIC,
                    )
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
    claimed_by_pid: int,
    attempts: int,
    reason: str | None = None,
) -> None:
    """Transition a CLAIMED row to a terminal state (DONE or SKIPPED),
    recording `reason` -- but only for the claim that holds it now.

    `claimed_by_pid` and `attempts` are the fence: pass them exactly as
    `claim()` returned them. Both are matched in the UPDATE itself, so a
    worker whose lease expired and was reclaimed by someone else (or by
    the same pid again: `attempts` moved on) cannot record its late
    result over the live owner's, and a row the sweep re-queued, a row
    already DONE/SKIPPED, or no row at all is refused too. Any refusal
    raises `StaleClaimError` (a ValueError) naming who holds the row
    now, and changes nothing (final review F12).

    Does not commit or roll back -- same contract as fan_out().
    """
    cursor = conn.execute(
        "UPDATE outbox SET state = ?, reason = ? "
        "WHERE outbox_id = ? AND state = 'CLAIMED' AND claimed_by_pid = ? AND attempts = ?",
        (state, reason, outbox_id, claimed_by_pid, attempts),
    )
    if cursor.rowcount == 1:
        return
    current = conn.execute(
        "SELECT state, claimed_by_pid, attempts FROM outbox WHERE outbox_id = ?",
        (outbox_id,),
    ).fetchone()
    yours = f"your claim (pid {claimed_by_pid}, attempt {attempts})"
    if current is None:
        now = "the row does not exist"
    elif current[0] == "CLAIMED":
        now = (
            f"it is now claimed by pid {current[1]} (attempt {current[2]}): the lease "
            "expired and was reclaimed"
        )
    else:
        now = f"it is now {current[0]}"
    raise StaleClaimError(
        f"refusing to mark outbox_id={outbox_id} {state!r} for {yours}: {now}"
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

    An expired ENTRY row whose signal is no longer tradeable (INVALID,
    VOIDED, withdrawn) is SKIPPED instead of re-queued, and counted in
    `outbox_dead_entry_skipped`, not in the return value.

    `now_utc` must be timezone-aware (ValueError otherwise) and is
    converted to UTC before anything is compared.

    Raises RuntimeError if `conn` already has a transaction pending --
    see `_require_no_pending_transaction` and the module docstring.
    """
    now_utc = _utc(now_utc, caller="sweep_expired")
    _require_no_pending_transaction(conn, caller="sweep_expired")

    cutoff_iso = (now_utc - timedelta(seconds=lease_seconds)).isoformat()
    day_key = metrics.day_key_for(now_utc)

    previous_isolation_level = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            dead = conn.execute(
                "UPDATE outbox SET state = 'SKIPPED', "
                "reason = 'lease expired; signal no longer tradeable (invalidated or withdrawn)' "
                f"WHERE state = 'CLAIMED' AND claimed_at_utc < ? AND {_DEAD_ENTRY}",
                (cutoff_iso,),
            ).rowcount
            if dead:
                metrics.bump(conn, day_key=day_key, name=DEAD_ENTRY_SKIPPED_METRIC, value=dead)
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
                    conn, day_key=day_key, name=LEASE_RECLAIMED_METRIC, value=reclaimed
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
        metrics.bump(conn, day_key=metrics.day_key_for(now_utc), name=NO_ACCOUNTS_METRIC)

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
    mid-send (``edits_repo`` treats it as in flight, i.e. possibly
    executed), and ``DONE``/``SKIPPED`` are final. If that worker dies,
    the row does not come back to life: once the signal is invalidated
    or withdrawn, ``sweep_expired()`` skips it rather than re-queue it,
    and ``claim()`` never hands out an ENTRY of such a signal (see the
    module docstring). Never commits."""
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


#: `outbox.reason` for an ENTRY skipped because its signal's channel is not
#: switched on (final review F15).
CHANNEL_OFF_REASON = "channel switched off"


def _entries_of_channels_off(enabled_channel_ids: tuple[int, ...]) -> tuple[str, tuple[int, ...]]:
    """The FROM/WHERE of every ``QUEUED`` ENTRY row whose signal's channel is
    not in `enabled_channel_ids` (`o`: the outbox row, `s`: its signal), and
    its parameters."""
    ids = tuple(int(channel_id) for channel_id in enabled_channel_ids)
    marks = ", ".join("?" for _ in ids)
    off = f"s.channel_id NOT IN ({marks})" if ids else "1 = 1"
    return (
        "FROM outbox o JOIN signals s ON s.signal_id = o.signal_id "
        f"WHERE o.kind = 'ENTRY' AND o.state = 'QUEUED' AND {off}",
        ids,
    )


def signals_with_entries_of_channels_off(
    conn: sqlite3.Connection, *, enabled_channel_ids: tuple[int, ...]
) -> tuple[int, ...]:
    """The signals `skip_entries_of_channels_off` would SKIP an entry of,
    oldest first -- asked just before it, in the same writer job, so that
    `relay_repo.admit_copies_of_switched_off` can trade from a channel
    switched on a call those signals can no longer trade (final fix round
    3, R3-1). Read-only."""
    where, ids = _entries_of_channels_off(enabled_channel_ids)
    rows = conn.execute(
        f"SELECT DISTINCT o.signal_id {where} ORDER BY o.signal_id", ids
    ).fetchall()
    return tuple(row[0] for row in rows)


def skip_entries_of_channels_off(
    conn: sqlite3.Connection, *, enabled_channel_ids: tuple[int, ...]
) -> dict[int, int]:
    """SKIP every ``QUEUED`` ENTRY row whose signal's channel is not in
    `enabled_channel_ids`, with reason `CHANNEL_OFF_REASON`; returns how
    many per channel (none: an empty dict).

    `watch` runs this at start (final review F15): a channel switched off
    is not read at all, so nobody would see its poster withdraw, re-aim or
    close a signal still waiting to be traded. A ``CLAIMED`` row is left
    to its worker, as in `skip_queued`; follow-ups are left alone. Never
    commits."""
    where, ids = _entries_of_channels_off(enabled_channel_ids)
    rows = conn.execute(f"SELECT o.outbox_id, s.channel_id {where}", ids).fetchall()
    skipped: dict[int, int] = {}
    for outbox_id, channel_id in rows:
        conn.execute(
            "UPDATE outbox SET state = 'SKIPPED', reason = ? "
            "WHERE outbox_id = ? AND state = 'QUEUED'",
            (CHANNEL_OFF_REASON, outbox_id),
        )
        skipped[channel_id] = skipped.get(channel_id, 0) + 1
    return skipped


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
