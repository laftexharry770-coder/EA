"""Tests for store/outbox.py: fan_out(), claim()'s leased-claim rule,
mark(), sweep_expired(), and the fan-out wiring into messages_repo.ingest().

The lease is the whole point (see outbox.py's module docstring and the
task-6 brief): a plain `UPDATE ... SET state='CLAIMED'` is a one-way door
that can strand a row forever if the claimant dies before its first
durable write. These tests pin down that a dead claimant's row becomes
claimable again after the lease expires, that two live claimants can
never both take the same row, and that fan-out is atomic with the
signal/followup insert it rides alongside inside ingest().
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from xaucopier.config.channels import ChannelCfg
from xaucopier.store import db, metrics, outbox
from xaucopier.store.messages_repo import ingest

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "messages" / "mmh_buy_4tp.json"
_FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
SIGNAL_TEXT = _FIXTURE["text"]
SIGNAL_MESSAGE_ID = _FIXTURE["message_id"]
POSTED = datetime.fromisoformat(_FIXTURE["posted_at_utc"])

CHANNEL_A = ChannelCfg(
    channel_id=_FIXTURE["channel_id"],
    name="Channel A (source)",
    parser="mmh",
    ordinal=1,
    enabled=True,
)

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


def _ingest(
    conn: sqlite3.Connection,
    *,
    channel_cfg: ChannelCfg = CHANNEL_A,
    message_id: int = SIGNAL_MESSAGE_ID,
    text: str = SIGNAL_TEXT,
    posted_at_utc: datetime = POSTED,
    edit_date_utc: datetime | None = None,
    reply_to_message_id: int | None = None,
    source: str = "live",
    now_utc: datetime = NOW,
    accounts: tuple = (ACCOUNT_1, ACCOUNT_2),
):
    return ingest(
        conn,
        channel_cfg=channel_cfg,
        message_id=message_id,
        text=text,
        posted_at_utc=posted_at_utc,
        edit_date_utc=edit_date_utc,
        reply_to_message_id=reply_to_message_id,
        source=source,
        now_utc=now_utc,
        accounts=accounts,
    )


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


def test_claim_exact_lease_boundary_is_not_reclaimable(conn):
    """`claimed_at_utc < now - lease` is strict: a claim exactly
    `lease_seconds` old must NOT be reclaimable -- `<` (not `<=`) is what
    protects a worker that is still legitimately inside its lease window
    down to the last second.
    """
    boundary_time = (NOW - timedelta(seconds=90)).isoformat()
    _insert_outbox_row(
        conn, account_id="acct-1", state="CLAIMED", claimed_at_utc=boundary_time, attempts=1
    )
    result = outbox.claim(conn, account_id="acct-1", pid=1, now_utc=NOW, lease_seconds=90)
    assert result is None


def test_claim_one_second_past_lease_boundary_is_reclaimable(conn):
    past_boundary_time = (NOW - timedelta(seconds=91)).isoformat()
    outbox_id = _insert_outbox_row(
        conn, account_id="acct-1", state="CLAIMED", claimed_at_utc=past_boundary_time, attempts=1
    )
    result = outbox.claim(conn, account_id="acct-1", pid=1, now_utc=NOW, lease_seconds=90)
    assert result is not None
    assert result.outbox_id == outbox_id


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


def test_claim_raises_if_connection_has_pending_transaction(tmp_path: Path):
    """Task 6 review round 1: the first cut of claim() wrongly predicted
    that calling it on a connection already mid-transaction would make its
    own `BEGIN IMMEDIATE` raise a loud `OperationalError`. It doesn't --
    `conn.isolation_level = None` (which claim() sets unconditionally,
    before ever issuing BEGIN) is itself a CPython sqlite3 operation that
    implicitly *commits* whatever was pending, silently. claim() must
    instead check `conn.in_transaction` up front, before isolation_level
    is touched at all, and refuse -- so the caller's pending work is
    neither committed nor lost, just left exactly as it was.
    """
    db_path = tmp_path / "store.db"
    conn = db.connect(db_path, role="writer")
    try:
        conn.execute(
            "INSERT INTO channel_state (channel_id, last_processed_message_id) "
            "VALUES (?, ?)",
            (999, 1),
        )
        assert conn.in_transaction

        with pytest.raises(RuntimeError):
            outbox.claim(conn, account_id="acct-1", pid=1, now_utc=NOW)

        # claim() must not have touched the caller's pending transaction
        # at all -- still open, on this same connection.
        assert conn.in_transaction

        # And, decisively: a second, independent connection must not see
        # the pending write as committed.
        other = db.connect(db_path, role="reader")
        try:
            row = other.execute(
                "SELECT 1 FROM channel_state WHERE channel_id = 999"
            ).fetchone()
            assert row is None
        finally:
            other.close()
    finally:
        conn.rollback()
        conn.close()


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


def test_sweep_expired_exact_lease_boundary_resets_nothing(conn):
    boundary_time = (NOW - timedelta(seconds=90)).isoformat()
    _insert_outbox_row(
        conn, account_id="acct-1", state="CLAIMED", claimed_at_utc=boundary_time, attempts=1
    )
    assert outbox.sweep_expired(conn, now_utc=NOW, lease_seconds=90) == 0


def test_sweep_expired_one_second_past_lease_boundary_resets_one(conn):
    past_boundary_time = (NOW - timedelta(seconds=91)).isoformat()
    _insert_outbox_row(
        conn, account_id="acct-1", state="CLAIMED", claimed_at_utc=past_boundary_time, attempts=1
    )
    assert outbox.sweep_expired(conn, now_utc=NOW, lease_seconds=90) == 1


def test_sweep_expired_rolls_back_reset_if_metric_write_fails(conn, monkeypatch):
    """Companion to claim()'s thread-contention test and ingest()'s
    monkeypatch atomicity tests: sweep_expired()'s reset+metric pairing
    had only been verified by reading the code, never by injecting a
    crash between the two statements. Monkeypatching `metrics.bump`
    specifically (not the whole function) lets the reset UPDATE genuinely
    run first and then fail, proving the UPDATE itself rolls back rather
    than landing alone while the metric is missed.
    """
    stale_time = (NOW - timedelta(seconds=200)).isoformat()
    stale_id = _insert_outbox_row(
        conn, account_id="acct-1", state="CLAIMED", claimed_at_utc=stale_time, attempts=1
    )

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash between reset UPDATE and metric write")

    monkeypatch.setattr(metrics, "bump", boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        outbox.sweep_expired(conn, now_utc=NOW, lease_seconds=90)

    row = conn.execute(
        "SELECT state, claimed_at_utc FROM outbox WHERE outbox_id = ?", (stale_id,)
    ).fetchone()
    assert row["state"] == "CLAIMED"  # the reset itself rolled back
    assert row["claimed_at_utc"] == stale_time

    metric_row = conn.execute(
        "SELECT value FROM metrics WHERE name = 'outbox_lease_reclaimed'"
    ).fetchone()
    assert metric_row is None


def test_sweep_expired_raises_if_connection_has_pending_transaction(tmp_path: Path):
    db_path = tmp_path / "store.db"
    conn = db.connect(db_path, role="writer")
    try:
        conn.execute(
            "INSERT INTO channel_state (channel_id, last_processed_message_id) "
            "VALUES (?, ?)",
            (998, 1),
        )
        assert conn.in_transaction

        with pytest.raises(RuntimeError):
            outbox.sweep_expired(conn, now_utc=NOW)

        assert conn.in_transaction

        other = db.connect(db_path, role="reader")
        try:
            row = other.execute(
                "SELECT 1 FROM channel_state WHERE channel_id = 998"
            ).fetchone()
            assert row is None
        finally:
            other.close()
    finally:
        conn.rollback()
        conn.close()


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


# -- ingest() wiring: fan-out fires only for "new", atomically -----------


def test_ingest_new_signal_fans_out_entry_rows_for_enabled_accounts(conn):
    result = _ingest(conn, accounts=(ACCOUNT_1, ACCOUNT_2, ACCOUNT_DISABLED))
    assert result.outcome == "new"
    assert result.kind == "signal"

    assert _count(conn, "signals") == 1
    rows = conn.execute(
        "SELECT account_id, kind, signal_id, followup_id FROM outbox ORDER BY account_id"
    ).fetchall()
    assert [dict(r) for r in rows] == [
        {"account_id": "acct-1", "kind": "ENTRY", "signal_id": result.signal_id, "followup_id": None},
        {"account_id": "acct-2", "kind": "ENTRY", "signal_id": result.signal_id, "followup_id": None},
    ]


def test_ingest_followup_fans_out_followup_rows(conn):
    result = _ingest(
        conn,
        message_id=9201,
        text="close all",
        reply_to_message_id=4001,
        accounts=(ACCOUNT_1,),
    )
    assert result.outcome == "new"
    assert result.kind == "followup"
    assert result.followup_id is not None

    row = conn.execute(
        "SELECT account_id, kind, signal_id, followup_id FROM outbox"
    ).fetchone()
    assert row["account_id"] == "acct-1"
    assert row["kind"] == "FOLLOWUP"
    assert row["signal_id"] is None
    assert row["followup_id"] == result.followup_id


def test_ingest_duplicate_edited_relay_duplicate_produce_no_outbox_rows(conn):
    first = _ingest(conn)
    assert first.outcome == "new"
    assert _count(conn, "outbox") == 2  # two accounts

    duplicate = _ingest(conn)
    assert duplicate.outcome == "duplicate"
    assert _count(conn, "outbox") == 2  # unchanged

    assert SIGNAL_TEXT.count("4470") == 1
    edited_text = SIGNAL_TEXT.replace("4470", "4460")
    edited = _ingest(conn, text=edited_text, edit_date_utc=POSTED + timedelta(minutes=2))
    assert edited.outcome == "edited"
    assert _count(conn, "outbox") == 2  # unchanged

    # Relay against the *current* trade identity -- the edit above already
    # moved the signal's trade_hash on to edited_text's, so a relay of the
    # original (pre-edit) text would no longer match anything. The window
    # compares against the *original* message's posted_at_utc (edits never
    # touch that column -- see _update_message_edit), so POSTED + 30s is
    # still well inside relay_window_seconds=900.
    relay = _ingest(
        conn,
        message_id=6001,
        text=edited_text,
        posted_at_utc=POSTED + timedelta(seconds=30),
    )
    assert relay.outcome == "relay_duplicate"
    assert _count(conn, "outbox") == 2  # unchanged


def test_ingest_new_signal_atomic_with_fan_out(conn, monkeypatch):
    """The load-bearing atomicity test the controller ruling calls out by
    name: kill the transaction between the signal insert and fan_out(),
    and assert neither the signals row nor any outbox rows survive.
    """

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash between signal insert and fan-out")

    monkeypatch.setattr(outbox, "fan_out", boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        _ingest(conn)

    # Nothing committed yet on this connection -- roll back exactly as the
    # Writer would on an exception from a submitted job, then verify on
    # the same connection that neither row is left behind.
    conn.rollback()

    assert _count(conn, "signals") == 0
    assert _count(conn, "outbox") == 0
    assert _count(conn, "messages") == 0


def test_ingest_followup_atomic_with_fan_out(conn, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash between followup insert and fan-out")

    monkeypatch.setattr(outbox, "fan_out", boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        _ingest(conn, message_id=9201, text="close all", reply_to_message_id=4001)

    conn.rollback()

    assert _count(conn, "followups") == 0
    assert _count(conn, "outbox") == 0
    assert _count(conn, "messages") == 0


# -- ingest(): accounts is required, and a zero-account fan-out is visible --


def test_ingest_new_signal_with_zero_accounts_records_visibility_metric(conn):
    """`accounts=()` is a legitimate, explicit choice (this whole test
    file makes it via `_ingest`'s default) -- but Task 6 review round 1
    flagged that a *forgotten* `accounts` argument used to produce the
    exact same shape silently. Now that `accounts` is required, `()` is
    always a deliberate choice, and _ingest_new records it as a metric
    either way so the condition is visible regardless of which reason
    produced it.
    """
    result = _ingest(conn, accounts=())
    assert result.outcome == "new"
    assert _count(conn, "outbox") == 0

    metric_row = conn.execute(
        "SELECT value FROM metrics WHERE name = 'ingest_new_no_accounts'"
    ).fetchone()
    assert metric_row["value"] == 1


def test_ingest_new_followup_with_zero_accounts_records_visibility_metric(conn):
    result = _ingest(
        conn, message_id=9201, text="close all", reply_to_message_id=4001, accounts=()
    )
    assert result.outcome == "new"
    assert _count(conn, "outbox") == 0

    metric_row = conn.execute(
        "SELECT value FROM metrics WHERE name = 'ingest_new_no_accounts'"
    ).fetchone()
    assert metric_row["value"] == 1


def test_ingest_new_signal_raises_if_fan_out_contract_is_violated(conn, monkeypatch):
    """Defense in depth, not a reachable case in normal operation:
    fan_out() guarantees one QUEUED row per enabled account, so it
    returning 0 while an enabled account was passed is a contradiction.
    If that contract were ever violated (a bug in fan_out(), a future
    refactor), ingest() must not let a signal it just durably inserted
    silently end up with zero workers ever assigned to it.
    """
    monkeypatch.setattr(outbox, "fan_out", lambda *args, **kwargs: 0)

    with pytest.raises(RuntimeError, match="should be impossible"):
        _ingest(conn, accounts=(ACCOUNT_1,))


def test_ingest_new_signal_with_all_accounts_disabled_records_visibility_metric(conn):
    """Task 6 review round 2, finding 1: an `accounts` tuple that is
    non-empty but every account in it is disabled used to defeat both
    guards -- `if not accounts` was False (the tuple is non-empty) and
    `enabled_count > 0` was False (nothing enabled), so control fell off
    the end silently: a fully successful "new" outcome, zero outbox
    rows, no metric, no raise. This is a *more* realistic
    misconfiguration than a forgotten `accounts` argument (which is now
    a TypeError at the call site), so it must produce the same
    visibility as accounts=() -- checking `fanout_count == 0` (the
    actual output) rather than `not accounts` (a guess from the input)
    catches this route too.
    """
    result = _ingest(conn, accounts=(ACCOUNT_DISABLED,))
    assert result.outcome == "new"
    assert _count(conn, "outbox") == 0

    metric_row = conn.execute(
        "SELECT value FROM metrics WHERE name = 'ingest_new_no_accounts'"
    ).fetchone()
    assert metric_row["value"] == 1


def test_ingest_new_signal_raises_if_fan_out_count_mismatches_enabled_count(conn, monkeypatch):
    """Round 2 strengthened the guard from `enabled_count > 0 and
    fanout_count == 0` to a direct count comparison: the narrower check
    only caught "0 when some expected", not the more plausible shape of
    a real fan_out() bug -- "2 written when 1 expected". Comparing the
    counts directly catches any mismatch, not just zero.
    """
    monkeypatch.setattr(outbox, "fan_out", lambda *args, **kwargs: 2)

    with pytest.raises(RuntimeError, match="should be impossible"):
        _ingest(conn, accounts=(ACCOUNT_1,))  # 1 enabled; fan_out lies and says 2


# -- claim() is not Writer work: pin the rejected pattern and the correct one --


def test_claim_through_writer_raises_writer_misuse_error_but_lands_durably(tmp_path: Path):
    """Task 6 review round 2, finding 3 (controller ruling): claim() and
    sweep_expired() manage their own BEGIN IMMEDIATE transaction --
    that's what makes the claim atomic -- so they are deliberately not
    Writer work. Submitting claim() through a real Writer does not fail
    cleanly: claim() calls COMMIT itself, so by the time Writer's own
    self-managed-fn misuse detector notices (total_changes moved while
    in_transaction is False) and resolves the future with
    WriterMisuseError, the claim has already landed durably on disk. A
    worker that took the WriterMisuseError at face value ("no work
    happened") would be wrong -- the row is CLAIMED and invisible to
    everyone until the next lease sweep. This pins that failure mode as
    known-and-rejected, not an untested surprise: Plan 3 must never
    route claim()/sweep_expired() through a Writer.
    """
    db_path = tmp_path / "store.db"
    setup_conn = db.connect(db_path, role="writer")
    outbox_id = _insert_outbox_row(setup_conn, account_id="acct-1", state="QUEUED")
    setup_conn.close()

    writer = db.Writer(db_path)
    writer.start()

    async def go():
        future = writer.submit(
            lambda c: outbox.claim(c, account_id="acct-1", pid=1, now_utc=NOW)
        )
        with pytest.raises(db.WriterMisuseError):
            await asyncio.wrap_future(future)

    try:
        asyncio.run(go())
    finally:
        writer.stop(timeout=5.0)

    # The claim landed durably anyway -- exactly the hazard this test
    # pins down.
    reader = db.connect(db_path, role="reader")
    try:
        row = reader.execute(
            "SELECT state, attempts, claimed_by_pid FROM outbox WHERE outbox_id = ?",
            (outbox_id,),
        ).fetchone()
        assert row["state"] == "CLAIMED"
        assert row["attempts"] == 1
        assert row["claimed_by_pid"] == 1
    finally:
        reader.close()


def test_claim_on_its_own_connection_succeeds(tmp_path: Path):
    """The correct pattern, as a positive control for the test above:
    claim() on its own dedicated db.connect(role="writer") connection --
    never routed through a Writer -- succeeds normally and returns the
    claimed row.
    """
    db_path = tmp_path / "store.db"
    setup_conn = db.connect(db_path, role="writer")
    outbox_id = _insert_outbox_row(setup_conn, account_id="acct-1", state="QUEUED")
    setup_conn.close()

    worker_conn = db.connect(db_path, role="writer")
    try:
        result = outbox.claim(worker_conn, account_id="acct-1", pid=1, now_utc=NOW)
        assert result is not None
        assert result.outbox_id == outbox_id
        assert result.state == "CLAIMED"
        assert result.claimed_by_pid == 1
        assert result.attempts == 1
    finally:
        worker_conn.close()
