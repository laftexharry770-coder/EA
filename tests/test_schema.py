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
    "channel_state", "metrics",
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
    """Plan 3 adds legs, fills, account_state, alerts -- not this task."""
    conn = _connect(tmp_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert tables == EXPECTED_TABLES
    for future_table in ("legs", "fills", "account_state", "alerts"):
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
