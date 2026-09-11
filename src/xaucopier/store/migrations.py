"""SQLite schema migration runner.

The schema's version lives in SQLite's built-in ``PRAGMA user_version`` --
an integer stored in the database file's own header, independent of any
table row, so it survives even a database that (through some future bug)
has no tables at all. ``migrate()`` reads it and brings the connection's
schema up to :data:`CURRENT_VERSION`.

``schema.sql`` is the verbatim DDL from the task brief: plain
``CREATE TABLE`` statements with no ``IF NOT EXISTS``, so running it twice
against the same database raises ``sqlite3.OperationalError`` (table
already exists). Idempotency is therefore this module's job, not the
DDL's: :func:`migrate` checks ``user_version`` first and only executes the
script when the database is still at version 0. Every later call --
same connection, a fresh connection to the same file, another process --
sees ``user_version >= CURRENT_VERSION`` and returns immediately.

Future schema changes add a branch below (``elif current == 1: ...``) and
bump ``CURRENT_VERSION``; existing databases upgrade in place the next
time ``migrate()`` runs against them.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

CURRENT_VERSION = 1

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _schema_sql() -> str:
    return _SCHEMA_PATH.read_text(encoding="utf-8")


def migrate(conn: sqlite3.Connection) -> None:
    """Bring `conn`'s schema up to CURRENT_VERSION. Safe to call repeatedly."""
    (current,) = conn.execute("PRAGMA user_version").fetchone()

    if current >= CURRENT_VERSION:
        return

    if current == 0:
        conn.executescript(_schema_sql())
        current = 1

    # Future migrations: `if current == 1: conn.executescript(...); current = 2`

    conn.execute(f"PRAGMA user_version = {CURRENT_VERSION}")
    conn.commit()
