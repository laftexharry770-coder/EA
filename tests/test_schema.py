"""Schema + migration runner tests.

Every constraint the DDL claims to hold gets a test that inserts data
proving it actually fires -- a UNIQUE or FOREIGN KEY in the .sql text is
not evidence by itself; SQLite silently ignores foreign keys unless the
connection has turned the pragma on, and a typo in a constraint clause
would otherwise pass a schema-only smoke test.
"""
import sqlite3
from pathlib import Path

import pytest

from xaucopier.store.migrations import CURRENT_VERSION, migrate

EXPECTED_TABLES = {
    "messages", "signals", "followups", "outbox",
    "channel_state", "metrics", "alerts", "heartbeats",
}


def _connect(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "store.db")
    migrate(conn)
    return conn


def _insert_message(conn, channel_id=1, message_id=100):
    conn.execute(
        """
        INSERT INTO messages
            (channel_id, message_id, kind, content_hash, raw_text,
             edit_date_utc, received_at_utc, posted_at_utc, source)
        VALUES (?, ?, 'signal', 'hash1', 'BUY GOLD 3350',
                NULL, '2026-09-10T00:00:00+00:00',
                '2026-09-10T00:00:00+00:00', 'live')
        """,
        (channel_id, message_id),
    )


def _insert_signal(conn, channel_id=1, message_id=100):
    conn.execute(
        """
        INSERT INTO signals
            (channel_id, message_id, parsed_json, trade_hash, state, version)
        VALUES (?, ?, '{}', 'hash1', 'NEW', 1)
        """,
        (channel_id, message_id),
    )


def _insert_followup(conn, channel_id=1, message_id=200):
    conn.execute(
        """
        INSERT INTO followups
            (channel_id, message_id, op, parsed_json,
             reply_to_message_id, target_signal_id, resolution_path)
        VALUES (?, ?, 'BE', '{}', NULL, NULL, NULL)
        """,
        (channel_id, message_id),
    )


# -- schema application ------------------------------------------------

def test_schema_applies_to_fresh_database(tmp_path):
    conn = _connect(tmp_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert tables == EXPECTED_TABLES
    conn.close()


def test_no_extra_tables_beyond_the_brief(tmp_path):
    """Plan 3 adds legs, fills, account_state -- not Plan 2. (`alerts`
    moved into Plan 2 with Task 10's migration 2: edits and deletions
    must be able to record an operator alert durably before Plan 3's
    notifier exists. `heartbeats` is Task 11's migration 3: `status`
    runs in another process and can only report what `watch` stamped.)"""
    conn = _connect(tmp_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert tables == EXPECTED_TABLES
    for future_table in ("legs", "fills", "account_state"):
        assert future_table not in tables
    conn.close()


# -- migrations.py idempotency and user_version -------------------------

def test_migrate_is_idempotent(tmp_path):
    conn = sqlite3.connect(tmp_path / "store.db")
    migrate(conn)
    migrate(conn)  # must be a no-op, not an error (no IF NOT EXISTS in DDL)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert tables == EXPECTED_TABLES
    conn.close()


def test_migrate_idempotent_across_reconnect(tmp_path):
    """A second process opening the same file must not re-run the DDL."""
    db_path = tmp_path / "store.db"
    conn1 = sqlite3.connect(db_path)
    migrate(conn1)
    conn1.close()

    conn2 = sqlite3.connect(db_path)
    migrate(conn2)  # must not raise
    (version,) = conn2.execute("PRAGMA user_version").fetchone()
    assert version == CURRENT_VERSION
    conn2.close()


def test_user_version_is_set_and_readable(tmp_path):
    conn = _connect(tmp_path)
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == CURRENT_VERSION
    assert version > 0
    conn.close()


# -- messages: UNIQUE(channel_id, message_id) via PRIMARY KEY -----------

def test_messages_duplicate_channel_message_rejected(tmp_path):
    conn = _connect(tmp_path)
    _insert_message(conn)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_message(conn)
    conn.close()


# -- signals: UNIQUE(channel_id, message_id) -----------------------------

def test_signals_duplicate_channel_message_rejected(tmp_path):
    conn = _connect(tmp_path)
    _insert_message(conn)
    conn.commit()
    _insert_signal(conn)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_signal(conn)
    conn.close()


# -- signals: FOREIGN KEY(channel_id, message_id) -> messages -----------

def test_signals_foreign_key_rejects_orphan_when_enforced(tmp_path):
    conn = _connect(tmp_path)
    conn.execute("PRAGMA foreign_keys = ON")
    # No row in messages for (channel_id=1, message_id=999).
    with pytest.raises(sqlite3.IntegrityError):
        _insert_signal(conn, channel_id=1, message_id=999)
    conn.close()


def test_signals_foreign_key_allows_matching_message(tmp_path):
    """Control for the rejection test above: proves the failure there is
    the foreign key firing, not a syntax or type error along the way."""
    conn = _connect(tmp_path)
    conn.execute("PRAGMA foreign_keys = ON")
    _insert_message(conn)
    _insert_signal(conn)
    conn.commit()
    (count,) = conn.execute("SELECT COUNT(*) FROM signals").fetchone()
    assert count == 1
    conn.close()


def test_signals_foreign_key_not_enforced_when_pragma_off(tmp_path):
    """Documents SQLite's actual default: foreign_keys is OFF per
    connection unless a caller turns it on. This is why the enforcement
    test above sets the pragma explicitly rather than relying on it."""
    conn = _connect(tmp_path)
    # foreign_keys pragma left at its default (off).
    _insert_signal(conn, channel_id=1, message_id=999)  # no matching message
    conn.commit()
    (count,) = conn.execute("SELECT COUNT(*) FROM signals").fetchone()
    assert count == 1
    conn.close()


# -- followups: UNIQUE(channel_id, message_id) ---------------------------

def test_followups_duplicate_channel_message_rejected(tmp_path):
    conn = _connect(tmp_path)
    _insert_followup(conn)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_followup(conn)
    conn.close()


# -- migrate() must be crash-safe: DDL + user_version is one transaction -

def test_migrate_recovers_from_partial_prior_application(tmp_path):
    """Reproduces the reviewer's crash-window scenario: a process dies
    between `executescript()` finishing (already durable -- DDL commits
    itself statement-by-statement in autocommit mode) and the
    `PRAGMA user_version = 1` write that was meant to follow it. On disk
    that leaves some tables present but `user_version` still 0.

    Before the fix, `migrate()` trusted `user_version == 0` to mean "no
    tables exist" and re-ran the full script from scratch, raising
    `sqlite3.OperationalError: table messages already exists` -- and
    every subsequent call would fail the same way, permanently bricking
    the database. The fix must tolerate (and complete) this partial
    state instead.
    """
    db_path = tmp_path / "store.db"

    # Simulate a migrate() that got through the first couple of
    # statements and then died before writing user_version.
    partial_conn = sqlite3.connect(db_path)
    partial_conn.executescript(
        """
        CREATE TABLE messages (
          channel_id      INTEGER NOT NULL,
          message_id      INTEGER NOT NULL,
          kind            TEXT    NOT NULL,
          content_hash    TEXT,
          raw_text        TEXT    NOT NULL,
          edit_date_utc   TEXT,
          received_at_utc TEXT    NOT NULL,
          posted_at_utc   TEXT    NOT NULL,
          source          TEXT    NOT NULL,
          PRIMARY KEY (channel_id, message_id)
        );
        CREATE TABLE signals (
          signal_id   INTEGER PRIMARY KEY,
          channel_id  INTEGER NOT NULL,
          message_id  INTEGER NOT NULL,
          parsed_json TEXT    NOT NULL,
          trade_hash  TEXT    NOT NULL,
          state       TEXT    NOT NULL,
          version     INTEGER NOT NULL DEFAULT 1,
          UNIQUE (channel_id, message_id),
          FOREIGN KEY (channel_id, message_id) REFERENCES messages(channel_id, message_id)
        );
        CREATE INDEX idx_signals_trade_hash ON signals(trade_hash);
        """
    )
    # Deliberately no `PRAGMA user_version = 1`; close without setting it,
    # matching a process that died mid-migrate.
    partial_conn.close()

    # Confirm the simulated crash state: tables exist, version is still 0.
    check_conn = sqlite3.connect(db_path)
    (version,) = check_conn.execute("PRAGMA user_version").fetchone()
    assert version == 0
    existing = {
        row[0]
        for row in check_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "messages" in existing
    check_conn.close()

    # A fresh connection's migrate() call must recover, not brick.
    conn = sqlite3.connect(db_path)
    migrate(conn)  # must not raise sqlite3.OperationalError

    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == CURRENT_VERSION

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert tables == EXPECTED_TABLES
    conn.close()


def test_migrate_rolls_back_cleanly_on_mid_ddl_failure(tmp_path, monkeypatch):
    """If migrate() is interrupted partway through applying the DDL, the
    whole attempt must roll back: `user_version` stays 0 and no tables
    are left behind, so the next call starts from a clean slate rather
    than the crash-window brick this task fixes.

    `sqlite3.Connection` is an immutable C type -- its `execute` method
    cannot be monkeypatched directly -- so the failure is injected the
    way the task brief's fallback suggests: by appending a deliberately
    invalid statement onto the real schema text. Everything before it
    (all six real tables) executes fine inside the transaction; the
    broken statement then raises, and that must undo the valid work
    that came before it too, proving the whole DDL application plus the
    user_version write is one atomic unit.
    """
    import xaucopier.store.migrations as migrations_module

    db_path = tmp_path / "store.db"

    real_schema_sql = migrations_module._schema_sql

    def broken_schema_sql() -> str:
        return real_schema_sql() + "\nCREATE TABLE this is not valid sql;\n"

    monkeypatch.setattr(migrations_module, "_schema_sql", broken_schema_sql)

    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.OperationalError):
        migrations_module.migrate(conn)

    monkeypatch.undo()

    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == 0
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert tables == set()
    conn.close()

    # And a subsequent, unpatched migrate() call on the same file
    # succeeds cleanly.
    conn2 = sqlite3.connect(db_path)
    migrate(conn2)
    (version,) = conn2.execute("PRAGMA user_version").fetchone()
    assert version == CURRENT_VERSION
    conn2.close()


# -- migration 2 (Task 10): alerts, messages deletion marks, followups.edit_seq


def _v1_database(db_path):
    """A database exactly as migration 1 left it -- schema.sql, applied
    the way `migrate()` applied it, and `user_version = 1` -- holding one
    row in every table, so an upgrade has something to lose."""
    import xaucopier.store.migrations as migrations_module

    conn = sqlite3.connect(db_path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    for statement in migrations_module._schema_statements():
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("COMMIT")
    conn.isolation_level = ""
    _insert_message(conn, channel_id=1, message_id=100)
    _insert_signal(conn, channel_id=1, message_id=100)
    _insert_message(conn, channel_id=1, message_id=200)
    conn.execute(
        "INSERT INTO followups (followup_id, channel_id, message_id, op, parsed_json, "
        "reply_to_message_id, target_signal_id, resolution_path) "
        "VALUES (41, 1, 200, 'amend_sl', '{\"price_hint\": 4460.0}', 100, 5, 'reply')"
    )
    conn.execute(
        "INSERT INTO outbox (account_id, kind, signal_id, followup_id, state, attempts) "
        "VALUES ('acct-1', 'FOLLOWUP', NULL, 41, 'DONE', 1)"
    )
    conn.execute(
        "INSERT INTO channel_state (channel_id, last_processed_message_id, "
        "consecutive_parse_failures) VALUES (1, 200, 2)"
    )
    conn.execute("INSERT INTO metrics (day_key, name, value) VALUES ('2026-09-05', 'x', 3)")
    conn.commit()
    return conn


def _dump(conn):
    """Every row of every migration-1 table, in migration-1 columns."""
    return {
        "messages": conn.execute(
            "SELECT channel_id, message_id, kind, content_hash, raw_text, edit_date_utc, "
            "received_at_utc, posted_at_utc, source FROM messages ORDER BY 1, 2"
        ).fetchall(),
        "signals": conn.execute(
            "SELECT signal_id, channel_id, message_id, parsed_json, trade_hash, state, version "
            "FROM signals ORDER BY signal_id"
        ).fetchall(),
        "followups": conn.execute(
            "SELECT followup_id, channel_id, message_id, op, parsed_json, "
            "reply_to_message_id, target_signal_id, resolution_path FROM followups "
            "ORDER BY followup_id"
        ).fetchall(),
        "outbox": conn.execute("SELECT * FROM outbox ORDER BY outbox_id").fetchall(),
        "channel_state": conn.execute(
            "SELECT channel_id, last_processed_message_id, consecutive_parse_failures "
            "FROM channel_state ORDER BY channel_id"
        ).fetchall(),
        "metrics": conn.execute("SELECT * FROM metrics").fetchall(),
    }


def _columns(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def test_upgrading_a_version_1_database_to_2_preserves_every_row(tmp_path):
    conn = _v1_database(tmp_path / "store.db")
    before = _dump(conn)
    assert "edit_seq" not in _columns(conn, "followups")

    migrate(conn)

    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == CURRENT_VERSION
    assert _dump(conn) == before
    # followup_id survives the table rebuild, so outbox.followup_id still
    # points at the same follow-up.
    assert conn.execute(
        "SELECT f.op FROM outbox o JOIN followups f ON f.followup_id = o.followup_id"
    ).fetchall() == [("amend_sl",)]
    assert conn.execute("SELECT edit_seq FROM followups").fetchall() == [(0,)]
    assert conn.execute(
        "SELECT deleted_at_utc, deleted_detected_by FROM messages"
    ).fetchall() == [(None, None), (None, None)]
    # A signal accepted before migration 2 was first accepted with the
    # trade hash it has now, and has not been withdrawn.
    assert conn.execute(
        "SELECT first_trade_hash, trade_hash, withdrawn_at_utc FROM signals"
    ).fetchall() == [("hash1", "hash1", None)]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == EXPECTED_TABLES
    conn.close()


def test_migration_2_is_idempotent(tmp_path):
    db_path = tmp_path / "store.db"
    conn = _v1_database(db_path)
    migrate(conn)
    after_first = _dump(conn)
    migrate(conn)  # a no-op, not "table alerts already exists"
    conn.close()

    conn = sqlite3.connect(db_path)
    migrate(conn)  # a fresh connection, likewise
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == CURRENT_VERSION
    assert _dump(conn) == after_first
    conn.close()


def test_migration_2_rolls_back_whole_on_a_failure_after_the_rebuild(tmp_path, monkeypatch):
    """The failure is injected *after* `DROP TABLE followups` and the
    rename -- the worst place: were the statements not one transaction
    with the `user_version` write, the old followups table would be gone
    and the database stuck at version 1 with a half-applied migration 2."""
    import xaucopier.store.migrations as migrations_module

    db_path = tmp_path / "store.db"
    conn = _v1_database(db_path)
    before = _dump(conn)
    real = migrations_module._migration_2_statements

    def broken():
        statements = real()
        assert any(s.startswith("DROP TABLE followups") for s in statements)
        return (*statements, "CREATE TABLE this is not valid sql")

    monkeypatch.setattr(migrations_module, "_migration_2_statements", broken)
    with pytest.raises(sqlite3.OperationalError):
        migrations_module.migrate(conn)
    monkeypatch.undo()

    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == 1
    assert _dump(conn) == before
    assert "edit_seq" not in _columns(conn, "followups")
    assert "deleted_at_utc" not in _columns(conn, "messages")
    assert "withdrawn_at_utc" not in _columns(conn, "signals")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "alerts" not in tables and "followups_v2" not in tables

    migrate(conn)  # and the next, unpatched, attempt completes
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == CURRENT_VERSION
    assert _dump(conn) == before
    conn.close()


def test_a_fresh_database_goes_straight_to_the_current_version(tmp_path):
    conn = _connect(tmp_path)
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == CURRENT_VERSION
    assert {"name", "at_utc", "detail"} == set(_columns(conn, "heartbeats"))
    assert "edit_seq" in _columns(conn, "followups")
    assert {"deleted_at_utc", "deleted_detected_by"} <= set(_columns(conn, "messages"))
    assert {"withdrawn_at_utc", "first_trade_hash"} <= set(_columns(conn, "signals"))
    assert {
        "alert_id", "created_at_utc", "level", "code", "channel_id",
        "message_id", "signal_id", "detail", "acked",
    } <= set(_columns(conn, "alerts"))
    conn.close()


def test_followups_are_unique_per_message_and_edit_seq(tmp_path):
    """A message's own follow-up is edit_seq 0; the AMEND_* rows an edit
    of a *signal* derives are 1, 2, ... under that signal's message id.
    Before migration 2 the key was (channel_id, message_id) alone, and a
    second amendment of one signal could not be stored at all."""
    conn = _connect(tmp_path)
    _insert_followup(conn, message_id=200)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _insert_followup(conn, message_id=200)
    for seq in (1, 2):
        conn.execute(
            "INSERT INTO followups (channel_id, message_id, op, parsed_json, edit_seq) "
            "VALUES (1, 200, 'amend_sl', '{}', ?)",
            (seq,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO followups (channel_id, message_id, op, parsed_json, edit_seq) "
            "VALUES (1, 200, 'amend_tp', '{}', 2)"
        )
    conn.close()


# -- migration 3 (Task 11): heartbeats ------------------------------------------


def _v2_database(db_path):
    """A version-1 database upgraded to exactly version 2 (migration 3
    withheld), holding a row in every table and one alert."""
    import xaucopier.store.migrations as migrations_module

    conn = _v1_database(db_path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    for statement in migrations_module._migration_2_statements():
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 2")
    conn.execute("COMMIT")
    conn.isolation_level = ""
    conn.execute(
        "INSERT INTO alerts (created_at_utc, level, code, detail) "
        "VALUES ('2026-09-10T00:00:00+00:00', 'CRITICAL', 'X', 'an alert')"
    )
    conn.commit()
    return conn


def test_upgrading_a_version_2_database_to_3_preserves_every_row(tmp_path):
    conn = _v2_database(tmp_path / "store.db")
    before = _dump(conn)
    alerts_before = conn.execute("SELECT * FROM alerts").fetchall()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "heartbeats" not in tables

    migrate(conn)

    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == CURRENT_VERSION
    assert _dump(conn) == before
    assert conn.execute("SELECT * FROM alerts").fetchall() == alerts_before
    assert conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone() == (0,)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == EXPECTED_TABLES
    conn.close()


def test_migration_3_rolls_back_whole_on_a_failure(tmp_path, monkeypatch):
    import xaucopier.store.migrations as migrations_module

    conn = _v2_database(tmp_path / "store.db")
    before = _dump(conn)
    real = migrations_module._migration_3_statements
    monkeypatch.setattr(
        migrations_module,
        "_migration_3_statements",
        lambda: (*real(), "CREATE TABLE this is not valid sql"),
    )
    with pytest.raises(sqlite3.OperationalError):
        migrations_module.migrate(conn)
    monkeypatch.undo()

    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == 2
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "heartbeats" not in tables
    assert _dump(conn) == before

    migrate(conn)
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == CURRENT_VERSION
    conn.close()


def test_heartbeats_are_one_row_per_name(tmp_path):
    conn = _connect(tmp_path)
    conn.execute("INSERT INTO heartbeats (name, at_utc) VALUES ('a', 't1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO heartbeats (name, at_utc) VALUES ('a', 't2')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO heartbeats (name, at_utc) VALUES ('b', NULL)")
    conn.close()


# -- final review F19: two processes upgrading one file at once ----------------


def test_two_connections_upgrading_one_file_at_once_both_succeed(tmp_path):
    """F19. B read `user_version` before A committed its upgrade, then
    re-ran migration 2's ALTERs after A's commit: "duplicate column name".
    migrate() now takes the write lock (BEGIN IMMEDIATE) *before* it
    decides what to run, and re-reads the version inside that
    transaction, so B waits for A and then finds nothing left to do."""
    import threading
    import time

    import xaucopier.store.migrations as migrations_module

    db_path = tmp_path / "store.db"
    _v1_database(db_path).close()

    a_holds_the_lock = threading.Event()
    real = migrations_module._migration_2_statements

    def slowed():
        statements = real()
        first_alter = next(i for i, s in enumerate(statements) if s.startswith("ALTER"))
        # After A's first ALTER, A holds the write lock; `slow()` then
        # signals the test and keeps the transaction open for a while.
        return (*statements[: first_alter + 1], "SELECT slow()", *statements[first_alter + 1 :])

    def a_slow():
        a_holds_the_lock.set()
        time.sleep(1.0)
        return 0

    conn_a = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn_b = sqlite3.connect(db_path, timeout=10)
    conn_a.create_function("slow", 0, a_slow)
    conn_b.create_function("slow", 0, lambda: 0)
    outcome: dict[str, object] = {}

    def upgrade_a():
        try:
            migrations_module.migrate(conn_a)
            outcome["a"] = "ok"
        except Exception as exc:  # noqa: BLE001 -- reported by the assert below
            outcome["a"] = exc

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(migrations_module, "_migration_2_statements", slowed)
        thread = threading.Thread(target=upgrade_a)
        thread.start()
        assert a_holds_the_lock.wait(timeout=5)
        try:
            migrations_module.migrate(conn_b)  # starts while A is mid-upgrade
            outcome["b"] = "ok"
        except Exception as exc:  # noqa: BLE001
            outcome["b"] = exc
        thread.join(timeout=10)

    assert outcome == {"a": "ok", "b": "ok"}
    for conn in (conn_a, conn_b):
        (version,) = conn.execute("PRAGMA user_version").fetchone()
        assert version == CURRENT_VERSION
    assert "edit_seq" in _columns(conn_b, "followups")
    conn_a.close()
    conn_b.close()


# -- final review F22: a database from a newer version is refused --------------


def test_migrate_refuses_a_database_from_a_newer_version(tmp_path):
    from xaucopier.store.migrations import SchemaVersionError

    db_path = tmp_path / "store.db"
    conn = _connect(tmp_path)
    conn.execute(f"PRAGMA user_version = {CURRENT_VERSION + 1}")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db_path)
    with pytest.raises(SchemaVersionError) as caught:
        migrate(conn)
    assert caught.value.found == CURRENT_VERSION + 1
    assert caught.value.expected == CURRENT_VERSION
    assert caught.value.newer is True
    assert f"reads version {CURRENT_VERSION}" in str(caught.value)
    assert "newer version of xaucopier" in str(caught.value)
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == CURRENT_VERSION + 1  # left exactly as it was
    assert not conn.in_transaction
    conn.close()


def test_migrate_refuses_a_newer_version_written_while_it_waited(tmp_path):
    """The version is checked again inside migrate()'s own transaction:
    a newer program may upgrade the file between migrate()'s first look
    (which saw version 1) and the write lock."""
    import threading
    import time

    import xaucopier.store.migrations as migrations_module

    db_path = tmp_path / "store.db"
    _v1_database(db_path).close()
    newer = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    newer_holds_the_lock = threading.Event()

    def newer_program_upgrades():
        newer.execute("BEGIN IMMEDIATE")
        newer.execute(f"PRAGMA user_version = {CURRENT_VERSION + 1}")
        newer_holds_the_lock.set()
        time.sleep(1.0)
        newer.execute("COMMIT")

    thread = threading.Thread(target=newer_program_upgrades)
    thread.start()
    assert newer_holds_the_lock.wait(timeout=5)
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        with pytest.raises(migrations_module.SchemaVersionError):
            migrations_module.migrate(conn)
        thread.join(timeout=10)
        (version,) = conn.execute("PRAGMA user_version").fetchone()
        assert version == CURRENT_VERSION + 1
        assert "edit_seq" not in _columns(conn, "followups")  # nothing of ours ran
        assert not conn.in_transaction
    finally:
        conn.close()
        newer.close()


def test_a_missing_migration_step_is_an_error_not_a_version_stamp(tmp_path, monkeypatch):
    """Bumping CURRENT_VERSION without adding its step used to stamp the
    new version over a schema that never got the change."""
    import xaucopier.store.migrations as migrations_module

    monkeypatch.setattr(migrations_module, "CURRENT_VERSION", CURRENT_VERSION + 1)
    conn = sqlite3.connect(tmp_path / "store.db")
    with pytest.raises(RuntimeError, match="a migration step is missing"):
        migrations_module.migrate(conn)
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == 0
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone() == (0,)
    conn.close()


@pytest.mark.parametrize("newer", [False, True])
def test_an_up_to_date_or_newer_file_is_decided_without_the_write_lock(tmp_path, newer):
    """Every normal start finds the file already current: that must not
    wait for the write lock (a reader connecting while `watch` writes
    would stall), and a file from a newer version is refused at once."""
    import xaucopier.store.migrations as migrations_module

    db_path = tmp_path / "store.db"
    _connect(tmp_path).close()
    if newer:
        raw = sqlite3.connect(db_path)
        raw.execute(f"PRAGMA user_version = {CURRENT_VERSION + 1}")
        raw.commit()
        raw.close()
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # another program is writing
    conn = sqlite3.connect(db_path, timeout=0.1)
    try:
        if newer:
            with pytest.raises(migrations_module.SchemaVersionError):
                migrations_module.migrate(conn)
        else:
            migrations_module.migrate(conn)  # no "database is locked"
    finally:
        conn.close()
        holder.execute("ROLLBACK")
        holder.close()


# -- migration 4 (Plan 2 final review fix wave) ---------------------------------

#: Every column migration 4 adds, by table. All NULL-able, no default.
_V4_COLUMNS = {
    "channel_state": ("seed_cutoff_utc", "seeded_at_utc"),
    "messages": (
        "relay_of_signal_id",
        "sender_kind",
        "sender_id",
        "is_post",
        "fwd_from_channel_id",
        "fwd_from_post_id",
        "fwd_from_date_utc",
    ),
}


def _v3_database(db_path):
    """A database exactly as migration 3 left it (migration 4 withheld),
    holding a row in every table: a channel whose pointer was seeded, a
    channel with only a parse-failure counter (pointer 0, never seeded),
    an alert and a heartbeat."""
    import xaucopier.store.migrations as migrations_module

    conn = _v2_database(db_path)
    conn.isolation_level = None
    conn.execute("BEGIN")
    for statement in migrations_module._migration_3_statements():
        conn.execute(statement)
    conn.execute("PRAGMA user_version = 3")
    conn.execute("COMMIT")
    conn.isolation_level = ""
    conn.execute(
        "INSERT INTO channel_state (channel_id, last_processed_message_id, "
        "consecutive_parse_failures) VALUES (2, 0, 1)"
    )
    conn.execute(
        "INSERT INTO heartbeats (name, at_utc, detail) "
        "VALUES ('watch_started', '2026-09-10T00:00:00+00:00', NULL)"
    )
    conn.commit()
    return conn


def _schema_shape(conn):
    """Everything about the schema a program could notice: every table's
    and index's DDL as SQLite stored it, and every column's full
    definition (type, NOT NULL, default, key position, hidden)."""
    master = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    tables = [row[1] for row in master if row[0] == "table"]
    columns = {t: conn.execute(f"PRAGMA table_xinfo({t})").fetchall() for t in tables}
    indexes = {
        t: [
            (idx[1], idx[2], conn.execute(f"PRAGMA index_xinfo({idx[1]})").fetchall())
            for idx in conn.execute(f"PRAGMA index_list({t})").fetchall()
        ]
        for t in tables
    }
    return master, columns, indexes


def test_current_version_is_four():
    assert CURRENT_VERSION == 4


def test_upgrading_a_version_3_database_to_4_preserves_every_row(tmp_path):
    from datetime import UTC, datetime, timedelta

    conn = _v3_database(tmp_path / "store.db")
    before = _dump(conn)
    alerts_before = conn.execute("SELECT * FROM alerts").fetchall()
    heartbeats_before = conn.execute("SELECT * FROM heartbeats").fetchall()
    for table, added in _V4_COLUMNS.items():
        assert not set(added) & set(_columns(conn, table))

    started = datetime.now(UTC).replace(microsecond=0)
    migrate(conn)
    finished = datetime.now(UTC)

    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == CURRENT_VERSION == 4
    assert _dump(conn) == before
    assert conn.execute("SELECT * FROM alerts").fetchall() == alerts_before
    assert conn.execute("SELECT * FROM heartbeats").fetchall() == heartbeats_before
    for table, added in _V4_COLUMNS.items():
        assert set(added) <= set(_columns(conn, table))
    # Every new messages column starts NULL on existing rows.
    new_message_columns = ", ".join(_V4_COLUMNS["messages"])
    assert conn.execute(f"SELECT {new_message_columns} FROM messages").fetchall() == [
        (None,) * len(_V4_COLUMNS["messages"])
    ] * 2
    # A channel whose pointer is above 0 was seeded by an earlier run:
    # it is marked seeded as of the migration. A pointer-0 row (only the
    # parse-failure counter) was never seeded and stays unmarked. No
    # channel is waiting for a re-seed.
    rows = dict(
        conn.execute("SELECT channel_id, seeded_at_utc FROM channel_state").fetchall()
    )
    assert rows[2] is None
    seeded_at = datetime.fromisoformat(rows[1])
    assert seeded_at.utcoffset() == timedelta(0)
    assert seeded_at.microsecond == 0
    assert started - timedelta(seconds=1) <= seeded_at <= finished
    assert rows[1].endswith("+00:00")
    assert conn.execute("SELECT seed_cutoff_utc FROM channel_state").fetchall() == [
        (None,),
        (None,),
    ]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == EXPECTED_TABLES
    conn.close()


def test_migration_4_rolls_back_whole_on_a_failure(tmp_path, monkeypatch):
    """Injected after the last ALTER and the backfill: were migration 4
    not one transaction with the user_version write, the file would be
    left at version 3 with columns a rerun cannot add again."""
    import xaucopier.store.migrations as migrations_module

    conn = _v3_database(tmp_path / "store.db")
    before = _dump(conn)
    shape_before = _schema_shape(conn)
    real = migrations_module._migration_4_statements
    monkeypatch.setattr(
        migrations_module,
        "_migration_4_statements",
        lambda: (*real(), "CREATE TABLE this is not valid sql"),
    )
    with pytest.raises(sqlite3.OperationalError):
        migrations_module.migrate(conn)
    monkeypatch.undo()

    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == 3
    assert _schema_shape(conn) == shape_before
    assert _dump(conn) == before

    migrate(conn)
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    assert version == 4
    conn.close()


@pytest.mark.parametrize("start", [1, 2, 3])
def test_a_fresh_schema_equals_an_upgraded_one(tmp_path, start):
    """A new install and an upgraded install must end with the identical
    schema -- same DDL text, same columns in the same order, same
    indexes -- whichever version the upgrade started from."""
    fresh = _connect(tmp_path)
    builders = {1: _v1_database, 2: _v2_database, 3: _v3_database}
    upgraded = builders[start](tmp_path / f"v{start}.db")
    migrate(upgraded)
    assert _schema_shape(upgraded) == _schema_shape(fresh)
    fresh.close()
    upgraded.close()


def test_messages_forward_lookup_has_its_index(tmp_path):
    conn = _connect(tmp_path)
    indexes = {row[1]: row for row in conn.execute("PRAGMA index_list(messages)")}
    assert "idx_messages_fwd_from" in indexes
    columns = [row[2] for row in conn.execute("PRAGMA index_info(idx_messages_fwd_from)")]
    assert columns == ["fwd_from_channel_id", "fwd_from_post_id"]
    plan = " ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT channel_id, message_id FROM messages "
            "WHERE fwd_from_channel_id = ? AND fwd_from_post_id = ?",
            (-1001, 5),
        )
    )
    assert "idx_messages_fwd_from" in plan
    conn.close()


@pytest.mark.parametrize(
    ("column", "bad"),
    [("sender_kind", "bot"), ("sender_kind", "User"), ("is_post", 2), ("is_post", -1)],
)
def test_sender_columns_accept_only_their_values(tmp_path, column, bad):
    conn = _connect(tmp_path)
    _insert_message(conn, message_id=100)
    for good in {"sender_kind": ("user", "channel", "chat", None), "is_post": (0, 1, None)}[column]:
        conn.execute(f"UPDATE messages SET {column} = ?", (good,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE messages SET {column} = ?", (bad,))
    conn.close()


def test_migration_4_corrects_parsed_json_source_of_recovered_signals(tmp_path):
    """F39: signals stored before the fix say source 'live' whatever
    recovered them. The upgrade writes the message's own source into
    parsed_json; a row already right, a row without the key, and a row
    that is not JSON at all are left byte-for-byte alone (the upgrade
    must never fail on one odd row)."""
    import json

    conn = _v3_database(tmp_path / "store.db")
    rows = {
        300: ("poll", json.dumps({"side": "BUY", "source": "live"})),
        301: ("backfill", json.dumps({"side": "SELL", "source": "live"})),
        302: ("live", json.dumps({"side": "BUY", "source": "live"})),
        303: ("poll", "not json at all"),
    }
    for message_id, (source, parsed_json) in rows.items():
        conn.execute(
            "INSERT INTO messages (channel_id, message_id, kind, content_hash, raw_text, "
            "received_at_utc, posted_at_utc, source) VALUES (1, ?, 'signal', 'h', 't', "
            "'2026-09-10T00:00:00+00:00', '2026-09-10T00:00:00+00:00', ?)",
            (message_id, source),
        )
        conn.execute(
            "INSERT INTO signals (channel_id, message_id, parsed_json, trade_hash, state, "
            "version) VALUES (1, ?, ?, ?, 'NEW', 1)",
            (message_id, parsed_json, f"hash{message_id}"),
        )
    conn.commit()
    untouched = dict(
        conn.execute(
            "SELECT message_id, parsed_json FROM signals WHERE message_id IN (100, 302, 303)"
        ).fetchall()
    )

    migrate(conn)

    after = dict(conn.execute("SELECT message_id, parsed_json FROM signals").fetchall())
    assert json.loads(after[300]) == {"side": "BUY", "source": "poll"}
    assert json.loads(after[301]) == {"side": "SELL", "source": "backfill"}
    for message_id, parsed_json in untouched.items():
        assert after[message_id] == parsed_json
    conn.close()
