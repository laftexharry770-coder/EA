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
