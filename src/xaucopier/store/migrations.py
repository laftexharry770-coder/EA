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

Future schema changes add a branch below (``if current == 3: ...``)
and bump ``CURRENT_VERSION``; existing databases upgrade in place the
next time ``migrate()`` runs against them. Every step a database needs
runs inside the *same* transaction as the one ``user_version`` write, so
a version-0 file goes to the current version in one atomic step, and a
version-1 file likewise.

**Migration 2 (Plan 2 Task 10: edits and deletions).** Four changes,
all in :data:`_MIGRATION_2`:

- ``alerts``: one durable row per thing an operator must look at. Plan 3
  builds the notifier; until then an alert that only went to a log
  would scroll away. ``store/alerts.py`` is its one writer.
- ``messages.deleted_at_utc`` / ``deleted_detected_by``: the durable mark
  that a message was deleted, and which mechanism saw it (the
  ``MessageDeleted`` event or the poll's window re-read). It is what
  makes a deletion seen by both mechanisms processed exactly once, and
  what keeps an executed signal's deletion (whose ``state`` deliberately
  does not change) from being re-alerted every poll round.
- ``followups.edit_seq`` and ``UNIQUE(channel_id, message_id, edit_seq)``
  in place of ``UNIQUE(channel_id, message_id)``. A material SL/TP edit
  of an *executed* signal enqueues ``AMEND_SL``/``AMEND_TP`` follow-ups,
  keyed by the signal's own message: one edit can need several (SL and
  a TP), and a signal can be edited more than once. Under the old key
  the second one could not be stored at all -- the writer job would fail
  every round, forever. ``edit_seq`` 0 is a message's own follow-up
  (everything migration 1 stored); 1, 2, ... are amendments derived from
  edits. SQLite cannot drop a table constraint in place, so this is the
  standard rebuild (create, copy, drop, rename), which keeps every
  ``followup_id`` -- ``outbox.followup_id`` still points at the same row.
  Nothing references ``followups`` by foreign key, so the rebuild needs
  no ``PRAGMA foreign_keys`` change (which SQLite would ignore inside a
  transaction anyway).
- ``signals.withdrawn_at_utc`` and ``signals.first_trade_hash``. The first
  records that the poster withdrew a signal (deleted it, or edited it into
  something that is no signal). A withdrawn signal that was already
  executed keeps ``state = 'NEW'`` so Plan 3 still manages its live legs,
  so ``state`` alone cannot tell ``outbox.claim()`` that its entries must
  never be handed out again -- this column does. The second keeps the
  trade hash a signal was first accepted with: an SL/TP edit rewrites
  ``trade_hash``, and relay dedup must still recognise another channel's
  copy of the *pre-edit* call as the same trade (``relay_repo.
  find_signal_relay`` matches either). Existing rows get
  ``first_trade_hash = trade_hash``.

**Migration 3 (Plan 2 Task 11: `status`).** One new table,
``heartbeats(name, at_utc, detail)``: "when did X last happen", one row
per name, overwritten each time. ``metrics`` counts events per day and
cannot say *when* the last one was; ``xaucopier status`` runs in a
different process from ``watch`` and can only learn what ``watch`` wrote
down. The poll stamps ``poll_round_completed`` (and each channel it read)
at the end of every round, and ``watch`` stamps its own start and stop
(with the exit code), which is how ``status`` tells "running" from
"stopped" and "stopped because the Telegram session was logged out".
``store/heartbeats.py`` is its one writer. Nothing else changes.

**Migration 4 (Plan 2 final review fix wave).** New NULL-able columns,
one index and two backfills, all in :data:`_MIGRATION_4`; apart from the
two backfills every existing row keeps every value it had.

- ``channel_state.seed_cutoff_utc``: when set, the channel must be
  (re)seeded at the newest message dated before this cutoff minus the
  60 s margin -- the first run's cutoff, persisted, so a restart before
  the first seed succeeds cannot re-anchor at the new process's start and
  skip what the first run missed (F01). Cleared in the same transaction
  that writes the seeded pointer.
- ``channel_state.seeded_at_utc``: an explicit "this channel has been
  seeded" marker. Pointer 0 with ``seeded_at_utc`` NULL means *never
  seeded* -- ``signals_repo`` creates such rows for the parse-failure
  counter -- while pointer 0 with a marker is an empty channel that was
  seeded at 0. Existing rows whose pointer is above 0 were seeded by an
  earlier run, so the migration marks them seeded as of the migration
  (ISO UTC, whole seconds).
- ``signals.parsed_json``'s ``source`` key: rows stored before final
  review F39 said ``'live'`` whatever recovered them; the migration
  writes the message's own ``messages.source`` into each row whose key
  holds a different value (a row that is not JSON, or has no such key,
  is left alone). ``json_set`` re-serialises only those rows, compactly.
- ``messages.relay_of_signal_id``: the signal a message was classified
  as a relay duplicate of, at ingest (F02), with the index
  ``idx_messages_relay_of_signal`` -- a signal's copies are looked up by
  it when one of them is edited or deleted, and when a withdrawn signal's
  standing repost is promoted (F04). ``messages.relay_of_followup_id``
  is the same for a follow-up (F08): the follow-up a relayed copy
  repeated. See ``store/relay_repo.py``.
- ``signals.closed_by_channel_id`` / ``closed_by_message_id``: the post
  whose deletion or edit first withdrew or invalidated the signal -- its
  own message, or another post of its call. A withdrawn call posted
  again *on that post's channel* is a new signal, whichever post was
  stored first (F04, ruling R3-P3). ``signals.promoted_from_signal_id``:
  for a signal that replaces a withdrawn one -- a standing repost
  promoted, or the call posted again by the channel that withdrew it --
  the signal it replaces; ``relay_repo.live_signal_for`` follows these
  links to the signal carrying a call now (R3-P2). All three are NULL on
  rows stored before migration 4 (then only a signal's own message could
  close it, and nothing replaced a signal).
- ``messages.sender_kind`` (``'user'`` / ``'channel'`` / ``'chat'``),
  ``sender_id`` and ``is_post`` (0/1): who posted a message, so a group
  member's chatter can be told from the channel's own posts (F05).
- ``messages.fwd_from_channel_id`` (in the same -100... form as
  ``channel_id``), ``fwd_from_post_id``, ``fwd_from_date_utc``: where a
  forwarded message came from, so a relay of an already-stored post is
  recognised exactly, however late it arrives (F09), with the index
  ``idx_messages_fwd_from`` for that lookup.

``sender_kind`` and ``is_post`` carry CHECK constraints; NULL (not
known, or stored before migration 4) always passes.

**Two programs opening one file at once (final review F19).** ``watch``
and, from Plan 3, the execution worker may both open the file for the
first time after an upgrade. ``migrate()`` used to read ``user_version``
*before* its (deferred) ``BEGIN``: the second program read the old
version while the first was mid-upgrade, waited for the first's lock,
then re-ran the ``ALTER TABLE``s -- "duplicate column name". Now the
transaction is ``BEGIN IMMEDIATE`` (the write lock is taken before
anything is decided) and ``user_version`` is read again *inside* it: the
second program waits for the first, then finds nothing left to do. The
first read, outside the transaction, is only a shortcut so an up-to-date
file (every normal start) never takes the write lock at all.

**A file from a newer version is refused (final review F22).** A
``user_version`` above :data:`CURRENT_VERSION` means a later build of
xaucopier wrote this file -- the user rolled back. This build does not
know that schema, so writing into it could corrupt what the newer build
relies on. ``migrate()`` raises :class:`SchemaVersionError` (checked
both before and inside the transaction) and changes nothing; since
``store.db.connect()`` runs ``migrate()`` for every role, every
connection refuses such a file, as ``connect_readonly()`` already did.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

CURRENT_VERSION = 4


class SchemaVersionError(sqlite3.DatabaseError):
    """The database file's schema is not one this code can use: older
    than :data:`CURRENT_VERSION` where the caller may not migrate
    (``store.db.connect_readonly``), or newer, written by a later
    build of xaucopier (every caller -- see the module docstring).

    A ``sqlite3.DatabaseError`` so a caller that already reports "the
    database cannot be used" for any ``sqlite3.Error`` reports this too.
    ``found`` / ``expected`` are the two versions; ``newer`` says which
    way round, because the fix differs: an older file is brought up to
    date by one run of ``watch``, a newer one needs the newer build.
    """

    def __init__(self, message: str, *, found: int, expected: int) -> None:
        super().__init__(message)
        self.found = found
        self.expected = expected

    @property
    def newer(self) -> bool:
        return self.found > self.expected


def newer_version_error(found: int, *, where: str = "this database") -> SchemaVersionError:
    """The error for a file written by a later build (F22)."""
    return SchemaVersionError(
        f"{where} was written by a newer version of xaucopier (schema version "
        f"{found}); this version of xaucopier reads version {CURRENT_VERSION} "
        "and will not change it",
        found=found,
        expected=CURRENT_VERSION,
    )

#: Migration 2 -- see the module docstring. Plain statements, run in
#: order inside `migrate()`'s transaction. `CREATE ... IF NOT EXISTS`
#: where SQLite allows it, like schema.sql; the `ALTER`s and the rebuild
#: rely on `user_version` gating plus that one transaction instead.
_MIGRATION_2: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS alerts (
      alert_id       INTEGER PRIMARY KEY,
      created_at_utc TEXT    NOT NULL,
      level          TEXT    NOT NULL CHECK (level IN ('WARNING', 'CRITICAL')),
      code           TEXT    NOT NULL,
      channel_id     INTEGER,
      message_id     INTEGER,
      signal_id      INTEGER,
      detail         TEXT    NOT NULL,
      acked          INTEGER NOT NULL DEFAULT 0 CHECK (acked IN (0, 1))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_alerts_unacked ON alerts(acked, alert_id)",
    "ALTER TABLE messages ADD COLUMN deleted_at_utc TEXT",
    "ALTER TABLE messages ADD COLUMN deleted_detected_by TEXT",
    """CREATE TABLE followups_v2 (
      followup_id         INTEGER PRIMARY KEY,
      channel_id          INTEGER NOT NULL,
      message_id          INTEGER NOT NULL,
      op                  TEXT    NOT NULL,
      parsed_json         TEXT    NOT NULL,
      reply_to_message_id INTEGER,
      target_signal_id    INTEGER,
      resolution_path     TEXT,
      edit_seq            INTEGER NOT NULL DEFAULT 0,
      UNIQUE (channel_id, message_id, edit_seq)
    )""",
    "INSERT INTO followups_v2 (followup_id, channel_id, message_id, op, parsed_json, "
    "reply_to_message_id, target_signal_id, resolution_path, edit_seq) "
    "SELECT followup_id, channel_id, message_id, op, parsed_json, "
    "reply_to_message_id, target_signal_id, resolution_path, 0 FROM followups",
    "DROP TABLE followups",
    "ALTER TABLE followups_v2 RENAME TO followups",
    "ALTER TABLE signals ADD COLUMN withdrawn_at_utc TEXT",
    "ALTER TABLE signals ADD COLUMN first_trade_hash TEXT",
    "UPDATE signals SET first_trade_hash = trade_hash",
    "CREATE INDEX IF NOT EXISTS idx_signals_first_trade_hash ON signals(first_trade_hash)",
)

#: Migration 3 -- the `heartbeats` table; see the module docstring.
_MIGRATION_3: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS heartbeats (
      name   TEXT PRIMARY KEY,
      at_utc TEXT NOT NULL,
      detail TEXT
    )""",
)

#: Migration 4 -- seed state, sender and forward provenance, relay link;
#: see the module docstring. The ALTERs rely on `user_version` gating and
#: the one transaction, like migration 2's.
_MIGRATION_4: tuple[str, ...] = (
    "ALTER TABLE channel_state ADD COLUMN seed_cutoff_utc TEXT",
    "ALTER TABLE channel_state ADD COLUMN seeded_at_utc TEXT",
    "ALTER TABLE messages ADD COLUMN relay_of_signal_id INTEGER",
    "ALTER TABLE messages ADD COLUMN relay_of_followup_id INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_messages_relay_of_signal ON messages(relay_of_signal_id)",
    "ALTER TABLE messages ADD COLUMN sender_kind TEXT "
    "CHECK (sender_kind IN ('user', 'channel', 'chat'))",
    "ALTER TABLE messages ADD COLUMN sender_id INTEGER",
    "ALTER TABLE messages ADD COLUMN is_post INTEGER CHECK (is_post IN (0, 1))",
    "ALTER TABLE messages ADD COLUMN fwd_from_channel_id INTEGER",
    "ALTER TABLE messages ADD COLUMN fwd_from_post_id INTEGER",
    "ALTER TABLE messages ADD COLUMN fwd_from_date_utc TEXT",
    "ALTER TABLE signals ADD COLUMN closed_by_channel_id INTEGER",
    "ALTER TABLE signals ADD COLUMN closed_by_message_id INTEGER",
    "ALTER TABLE signals ADD COLUMN promoted_from_signal_id INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_messages_fwd_from "
    "ON messages(fwd_from_channel_id, fwd_from_post_id)",
    # A pointer above 0 was written by a seed (or by ingest after one):
    # that channel has been seeded. Pointer-0 rows stay unmarked.
    "UPDATE channel_state SET seeded_at_utc = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now') "
    "WHERE last_processed_message_id > 0 AND seeded_at_utc IS NULL",
    # F39: parsed_json.source said 'live' for every signal, whatever
    # recovered it. Write the message's own source into rows that carry
    # the key with another value. CASE (evaluated lazily, unlike AND)
    # keeps a row that is not valid JSON from failing the whole upgrade;
    # such a row, and one without the key, is left as it is.
    "UPDATE signals SET parsed_json = json_set(parsed_json, '$.source', "
    "  (SELECT m.source FROM messages m "
    "   WHERE m.channel_id = signals.channel_id AND m.message_id = signals.message_id)) "
    "WHERE CASE WHEN json_valid(parsed_json) "
    "  THEN json_type(parsed_json, '$.source') = 'text' "
    "   AND json_extract(parsed_json, '$.source') IS NOT "
    "    (SELECT m.source FROM messages m "
    "     WHERE m.channel_id = signals.channel_id AND m.message_id = signals.message_id) "
    "   AND EXISTS (SELECT 1 FROM messages m "
    "     WHERE m.channel_id = signals.channel_id AND m.message_id = signals.message_id) "
    "  ELSE 0 END",
)

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


def _migration_2_statements() -> tuple[str, ...]:
    """Migration 2's statements. A function, like `_schema_sql`, so a
    test can inject a failure part-way through and prove the whole step
    rolls back."""
    return _MIGRATION_2


def _migration_3_statements() -> tuple[str, ...]:
    """Migration 3's statements; a function for the same reason as
    `_migration_2_statements`."""
    return _MIGRATION_3


def _migration_4_statements() -> tuple[str, ...]:
    """Migration 4's statements; a function for the same reason as
    `_migration_2_statements`."""
    return _MIGRATION_4


def migrate(conn: sqlite3.Connection) -> None:
    """Bring `conn`'s schema up to CURRENT_VERSION. Safe to call repeatedly.

    Crash-safe: applying the schema and recording `user_version` happen
    in one transaction, so a call that is interrupted (exception, kill,
    power loss) never leaves the database in a half-migrated state --
    see the module docstring for why `executescript()` cannot offer
    that guarantee and what replaces it here.

    Safe against a second program upgrading the same file at the same
    moment: the version is re-read under the write lock (F19). Raises
    `SchemaVersionError`, changing nothing, for a file from a newer
    version of xaucopier (F22).
    """
    (current,) = conn.execute("PRAGMA user_version").fetchone()
    if current > CURRENT_VERSION:
        raise newer_version_error(current)
    if current == CURRENT_VERSION:
        return  # the normal start: no write lock taken at all

    previous_isolation_level = conn.isolation_level
    conn.isolation_level = None  # autocommit; we drive BEGIN/COMMIT ourselves
    try:
        # IMMEDIATE: take the write lock *before* deciding what to run,
        # then decide from the version as it is now -- another program
        # may have upgraded the file while this one waited (F19).
        conn.execute("BEGIN IMMEDIATE")
        try:
            (current,) = conn.execute("PRAGMA user_version").fetchone()
            if current > CURRENT_VERSION:
                raise newer_version_error(current)

            if current == 0:
                for statement in _schema_statements():
                    conn.execute(statement)
                current = 1

            if current == 1:
                for statement in _migration_2_statements():
                    conn.execute(statement)
                current = 2

            if current == 2:
                for statement in _migration_3_statements():
                    conn.execute(statement)
                current = 3

            if current == 3:
                for statement in _migration_4_statements():
                    conn.execute(statement)
                current = 4

            # Future migrations:
            # `if current == 4: for stmt in ...: conn.execute(stmt); current = 5`

            if current != CURRENT_VERSION:
                raise RuntimeError(
                    f"migrate() stopped at schema version {current}, not "
                    f"{CURRENT_VERSION}: a migration step is missing"
                )
            conn.execute(f"PRAGMA user_version = {CURRENT_VERSION}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.isolation_level = previous_isolation_level
