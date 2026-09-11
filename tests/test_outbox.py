"""Tests for store/outbox.py: fan_out(), claim()'s leased-claim rule,
mark(), and sweep_expired().

The lease is the whole point (see outbox.py's module docstring and the
task-6 brief): a plain `UPDATE ... SET state='CLAIMED'` is a one-way door
that can strand a row forever if the claimant dies before its first
durable write. These tests pin down that a dead claimant's row becomes
claimable again after the lease expires, and that two live claimants can
never both take the same row.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from xaucopier.store import db, outbox

NOW = datetime(2026, 9, 5, 11, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Account:
    """A minimal stand-in for config.accounts.AccountCfg -- only the two
    fields fan_out() actually reads (account_id, enabled). store/ tests
    don't need the impure config module for this.
    """

    account_id: str
    enabled: bool


ACCOUNT_1 = _Account(account_id="acct-1", enabled=True)
ACCOUNT_2 = _Account(account_id="acct-2", enabled=True)
ACCOUNT_DISABLED = _Account(account_id="acct-off", enabled=False)


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "store.db", role="writer")
    try:
        yield connection
    finally:
        connection.close()


def _count(conn: sqlite3.Connection, table: str) -> int:
    (n,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return n


def _insert_outbox_row(
    conn: sqlite3.Connection,
    *,
    account_id: str = "acct-1",
    kind: str = "ENTRY",
    signal_id: int | None = 1,
    followup_id: int | None = None,
    state: str = "QUEUED",
    claimed_at_utc: str | None = None,
    claimed_by_pid: int | None = None,
    attempts: int = 0,
) -> int:
    cursor = conn.execute(
        "INSERT INTO outbox "
        "(account_id, kind, signal_id, followup_id, state, claimed_at_utc, "
        " claimed_by_pid, attempts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            account_id,
            kind,
            signal_id,
            followup_id,
            state,
            claimed_at_utc,
            claimed_by_pid,
            attempts,
        ),
    )
    conn.commit()
    return cursor.lastrowid


# -- fan_out() -----------------------------------------------------------


def test_fan_out_writes_one_row_per_enabled_account_and_returns_count(conn):
    count = outbox.fan_out(
        conn,
        kind="ENTRY",
        signal_id=42,
        followup_id=None,
        accounts=(ACCOUNT_1, ACCOUNT_2, ACCOUNT_DISABLED),
    )
    assert count == 2

    rows = conn.execute(
        "SELECT account_id, kind, signal_id, followup_id, state FROM outbox "
        "ORDER BY account_id"
    ).fetchall()
    assert [dict(r) for r in rows] == [
        {
            "account_id": "acct-1",
            "kind": "ENTRY",
            "signal_id": 42,
            "followup_id": None,
            "state": "QUEUED",
        },
        {
            "account_id": "acct-2",
            "kind": "ENTRY",
            "signal_id": 42,
            "followup_id": None,
            "state": "QUEUED",
        },
    ]


def test_fan_out_no_rows_when_all_accounts_disabled(conn):
    count = outbox.fan_out(
        conn, kind="ENTRY", signal_id=1, followup_id=None, accounts=(ACCOUNT_DISABLED,)
    )
    assert count == 0
    assert _count(conn, "outbox") == 0


# -- claim(): the lease -----------------------------------------------


def test_two_concurrent_claims_only_one_wins(tmp_path: Path):
    """Real threads, each with its own writer-role connection opened
    *inside* the thread -- sqlite3 connections are not shareable across
    threads (`check_same_thread`), and this is also the realistic shape:
    Plan 3 workers are separate processes/connections, not a shared one.
    A `threading.Barrier` lines both threads up to call claim() as close
    to simultaneously as possible, to actually exercise the BEGIN
    IMMEDIATE lock contention rather than two calls that never overlap.
    """
    db_path = tmp_path / "store.db"
    setup_conn = db.connect(db_path, role="writer")
    outbox_id = _insert_outbox_row(setup_conn, account_id="acct-1", state="QUEUED")
    setup_conn.close()

    results: dict[str, outbox.OutboxRow | None] = {}
    errors: dict[str, BaseException] = {}
    barrier = threading.Barrier(2)

    def worker(name: str, pid: int) -> None:
        conn = db.connect(db_path, role="writer")
        try:
            barrier.wait(timeout=5.0)
            try:
                results[name] = outbox.claim(
                    conn, account_id="acct-1", pid=pid, now_utc=NOW
                )
            except BaseException as exc:  # noqa: BLE001 -- surfaced via errors dict
                errors[name] = exc
        finally:
            conn.close()

    t_a = threading.Thread(target=worker, args=("a", 111))
    t_b = threading.Thread(target=worker, args=("b", 222))
    t_a.start()
    t_b.start()
    t_a.join(timeout=5.0)
    t_b.join(timeout=5.0)

    assert not errors, errors
    winners = [r for r in results.values() if r is not None]
    losers = [r for r in results.values() if r is None]
    assert len(winners) == 1
    assert len(losers) == 1
    assert winners[0].outbox_id == outbox_id
    assert winners[0].claimed_by_pid in (111, 222)


def test_expired_claim_is_reclaimable_and_attempts_becomes_two(conn):
    stale_claim_time = (NOW - timedelta(seconds=200)).isoformat()
    outbox_id = _insert_outbox_row(
        conn,
        account_id="acct-1",
        state="CLAIMED",
        claimed_at_utc=stale_claim_time,
        claimed_by_pid=999,
        attempts=1,
    )

    result = outbox.claim(conn, account_id="acct-1", pid=555, now_utc=NOW, lease_seconds=90)

    assert result is not None
    assert result.outbox_id == outbox_id
    assert result.state == "CLAIMED"
    assert result.claimed_by_pid == 555
    assert result.attempts == 2


def test_fresh_claim_is_not_reclaimable(conn):
    fresh_claim_time = (NOW - timedelta(seconds=10)).isoformat()
    _insert_outbox_row(
        conn,
        account_id="acct-1",
        state="CLAIMED",
        claimed_at_utc=fresh_claim_time,
        claimed_by_pid=999,
        attempts=1,
    )

    result = outbox.claim(conn, account_id="acct-1", pid=555, now_utc=NOW, lease_seconds=90)
    assert result is None


def test_claim_takes_oldest_outbox_id_first(conn):
    first_id = _insert_outbox_row(conn, account_id="acct-1", signal_id=1)
    second_id = _insert_outbox_row(conn, account_id="acct-1", signal_id=2)
    assert first_id < second_id

    result = outbox.claim(conn, account_id="acct-1", pid=1, now_utc=NOW)
    assert result.outbox_id == first_id


def test_claim_ignores_other_accounts(conn):
    _insert_outbox_row(conn, account_id="acct-other", state="QUEUED")
    result = outbox.claim(conn, account_id="acct-1", pid=1, now_utc=NOW)
    assert result is None


# -- sweep_expired() ------------------------------------------------------


def test_sweep_expired_resets_stale_claim_and_counts_metric(conn):
    stale_time = (NOW - timedelta(seconds=200)).isoformat()
    stale_id = _insert_outbox_row(
        conn,
        account_id="acct-1",
        state="CLAIMED",
        claimed_at_utc=stale_time,
        claimed_by_pid=999,
        attempts=1,
    )
    fresh_time = (NOW - timedelta(seconds=5)).isoformat()
    fresh_id = _insert_outbox_row(
        conn,
        account_id="acct-1",
        state="CLAIMED",
        claimed_at_utc=fresh_time,
        claimed_by_pid=1000,
        attempts=1,
    )

    reset_count = outbox.sweep_expired(conn, now_utc=NOW, lease_seconds=90)
    assert reset_count == 1

    stale_row = conn.execute(
        "SELECT state, claimed_at_utc, claimed_by_pid FROM outbox WHERE outbox_id = ?",
        (stale_id,),
    ).fetchone()
    assert stale_row["state"] == "QUEUED"
    assert stale_row["claimed_at_utc"] is None
    assert stale_row["claimed_by_pid"] is None

    fresh_row = conn.execute(
        "SELECT state FROM outbox WHERE outbox_id = ?", (fresh_id,)
    ).fetchone()
    assert fresh_row["state"] == "CLAIMED"

    metric_row = conn.execute(
        "SELECT value FROM metrics WHERE name = 'outbox_lease_reclaimed'"
    ).fetchone()
    assert metric_row["value"] == 1


def test_sweep_expired_accumulates_metric_across_calls(conn):
    stale_time = (NOW - timedelta(seconds=200)).isoformat()
    _insert_outbox_row(
        conn, account_id="acct-1", state="CLAIMED", claimed_at_utc=stale_time, attempts=1
    )
    _insert_outbox_row(
        conn, account_id="acct-2", state="CLAIMED", claimed_at_utc=stale_time, attempts=1
    )

    first = outbox.sweep_expired(conn, now_utc=NOW, lease_seconds=90)
    assert first == 2

    # Nothing left to reclaim on a second sweep -- and the metric must not
    # double count or reset.
    second = outbox.sweep_expired(conn, now_utc=NOW, lease_seconds=90)
    assert second == 0

    metric_row = conn.execute(
        "SELECT value FROM metrics WHERE name = 'outbox_lease_reclaimed'"
    ).fetchone()
    assert metric_row["value"] == 2


# -- mark() ----------------------------------------------------------------


def test_mark_transitions_claimed_to_done_and_records_reason(conn):
    outbox_id = _insert_outbox_row(conn, account_id="acct-1", state="CLAIMED")

    outbox.mark(conn, outbox_id=outbox_id, state="DONE", reason="filled")

    row = conn.execute(
        "SELECT state, reason FROM outbox WHERE outbox_id = ?", (outbox_id,)
    ).fetchone()
    assert row["state"] == "DONE"
    assert row["reason"] == "filled"


def test_mark_raises_when_row_is_not_claimed(conn):
    outbox_id = _insert_outbox_row(conn, account_id="acct-1", state="QUEUED")

    with pytest.raises(Exception):
        outbox.mark(conn, outbox_id=outbox_id, state="DONE")

    row = conn.execute(
        "SELECT state FROM outbox WHERE outbox_id = ?", (outbox_id,)
    ).fetchone()
    assert row["state"] == "QUEUED"  # untouched
