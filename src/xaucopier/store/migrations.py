"""SQLite schema migration runner.

The schema's version lives in SQLite's built-in ``PRAGMA user_version`` --
an integer stored in the database file's own header, independent of any
table row, so it survives even a database that (through some future bug)
has no tables at all. ``migrate()`` reads it and brings the connection's
schema up to :data:`CURRENT_VERSION`.

NOTE -- deviation from "DDL verbatim": ``schema.sql``'s ``CREATE TABLE``
/ ``CREATE INDEX`` statements now carry ``IF NOT EXISTS``. This is a
deliberate, controller-approved departure from the task brief's original
plain DDL (see the crash-safety discussion below for why it's needed in
addition to, not instead of, the transactional fix). It does not by
itself provide idempotency across already-fully-migrated databases --
:func:`migrate` still checks ``user_version`` first and only touches the
schema when the database is still below :data:`CURRENT_VERSION`, so a
normal repeat call (same connection, a fresh connection to the same
file, another process) short-circuits before running any DDL at all.
``IF NOT EXISTS`` only matters for the specific partial-application
scenario described next.

Applying the DDL and recording the new ``user_version`` is one atomic
transaction, not two independent steps. ``Connection.executescript()``
would ruin that: it first commits any open transaction and then runs
each statement in *autocommit* mode, so every ``CREATE TABLE`` becomes
durable the instant it runs -- there is no way to roll a partially
applied script back. If the process died between the script finishing
and the follow-up ``PRAGMA user_version = 1`` (a rate-limit kill, a
power cut, Ctrl-C), the tables would already be on disk while
``user_version`` stayed 0. The next ``migrate()`` call would see
``current == 0``, try to re-run the script from scratch, and raise
``table messages already exists`` -- forever, since nothing about that
failure changes state. That is the exact scenario
``test_migrate_recovers_from_partial_prior_application`` reproduces.

So instead of ``executescript``, the schema is split into individual
statements (comments stripped, split on ``;``) and each one is run with
plain ``execute()`` inside a single explicit ``BEGIN`` / ``COMMIT``
that also covers the ``PRAGMA user_version`` write -- SQLite DDL is
transactional, and ``PRAGMA user_version`` *is* allowed inside a
transaction and rolls back with it like any other statement. On any
exception the transaction is rolled back and the exception re-raised,
so a failed *future* ``migrate()`` call always leaves the file exactly
as it found it: either nothing changed, or fully upgraded. There is no
in-between state left on disk by ``migrate()`` itself for a later call
to trip over -- this is what
``test_migrate_rolls_back_cleanly_on_mid_ddl_failure`` pins down.

That guarantee only covers partial states ``migrate()`` itself could
create going forward -- it can't undo a partial state that already
exists on disk before ``migrate()`` is ever called (e.g. a database
bricked by the pre-fix version of this code, or any other process that
ran some of the DDL and died). For that case the transaction alone
isn't enough: `current` reads 0 (nothing recorded the partial
progress), so `migrate()` would re-run from the first statement and
immediately hit ``table messages already exists`` on the very table
that already exists -- an exception, triggering the rollback path
above, which would undo nothing (there was nothing pending to undo)
and simply re-raise. The database would stay bricked forever, which is
the original bug. This is why ``schema.sql``'s statements carry
``IF NOT EXISTS`` (see the NOTE above): it lets ``migrate()`` walk
through a schema that is already partially present, silently skipping
whatever's already there and creating whatever's still missing, all
inside the same atomic transaction as the ``user_version`` write. This
is exactly ``test_migrate_recovers_from_partial_prior_application``'s
scenario.

``sqlite3.Connection`` in Python 3.14's default (legacy "DEFERRED")
isolation mode auto-issues ``BEGIN`` before DML but *not* before DDL,
which would silently defeat the transaction above. ``migrate()``
therefore switches the connection to autocommit mode
(``conn.isolation_level = None``) for the duration of its own explicit
``BEGIN`` / ``COMMIT`` / ``ROLLBACK``, and restores whatever
``isolation_level`` the connection had on entry before returning --
callers should see no lasting change to the connection's transaction
mode. (The newer 3.12+ ``conn.autocommit`` attribute would work too;
``isolation_level`` is used here because it is restorable to an
arbitrary prior value with a plain assignment, which keeps the
save/restore symmetric without caring which mode the caller was in.)

Future schema changes add a branch below (``elif current == 1: ...``)
and bump ``CURRENT_VERSION``; existing databases upgrade in place the
next time ``migrate()`` runs against them.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

CURRENT_VERSION = 1

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _schema_sql() -> str:
    return _SCHEMA_PATH.read_text(encoding="utf-8")


def _schema_statements() -> list[str]:
    """Split `schema.sql` into individual statements for `execute()`.

    `executescript()` is what forces autocommit (see module docstring),
    so applying the DDL atomically means running each statement
    ourselves. `schema.sql` has no string literals containing `;` or
    `--`, so a straightforward strip-comments-then-split is exact here;
    it is not a general-purpose SQL statement splitter.
    """
    statements = []
    for raw_line in _schema_sql().splitlines():
        line = raw_line.split("--", 1)[0]
        if line.strip():
            statements.append(line)
    script = "\n".join(statements)
    return [stmt.strip() for stmt in script.split(";") if stmt.strip()]


def migrate(conn: sqlite3.Connection) -> None:
    """Bring `conn`'s schema up to CURRENT_VERSION. Safe to call repeatedly.

    Crash-safe: applying the schema and recording `user_version` happen
    in one transaction, so a call that is interrupted (exception, kill,
    power loss) never leaves the database in a half-migrated state --
    see the module docstring for why `executescript()` cannot offer
    that guarantee and what replaces it here.
    """
    (current,) = conn.execute("PRAGMA user_version").fetchone()

    if current >= CURRENT_VERSION:
        return

    previous_isolation_level = conn.isolation_level
    conn.isolation_level = None  # autocommit; we drive BEGIN/COMMIT ourselves
    try:
        conn.execute("BEGIN")
        try:
            if current == 0:
                for statement in _schema_statements():
                    conn.execute(statement)
                current = 1

            # Future migrations:
            # `if current == 1: for stmt in ...: conn.execute(stmt); current = 2`

            conn.execute(f"PRAGMA user_version = {CURRENT_VERSION}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.isolation_level = previous_isolation_level
