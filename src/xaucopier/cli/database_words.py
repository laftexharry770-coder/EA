"""The plain words for a database file a command cannot use.

Shared by every command that opens the store -- `status`, `ack`,
`channels`, `export-fixtures`, `resolve-channels` and `watch` (final fix
FF19: watch printed the Python class name instead, and for a database
from a newer version said only "fix it"). It lives on its own so that
`watch` need not import `status`, which imports `watch`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from xaucopier.cli import paths
from xaucopier.store import db

_CMD = paths.USER_COMMAND
#: First words that only start a sentence: lower-cased after a colon.
_SENTENCE_STARTS = frozenset({"The", "This", "It", "Its", "No", "Nothing", "A", "An"})


def after_colon(text: str) -> str:
    """`text` -- one of these sentences, or any other a command prints --
    where it follows a colon mid-line ("watch cannot start: the database
    at ..."): its first word lower-cased when that word only starts a
    sentence ("The"), as everywhere else (final fix FF25 c, round 2). A
    path, a name or any other first word is left as it is."""
    first, space, rest = text.partition(" ")
    if first in _SENTENCE_STARTS:
        return first.lower() + space + rest
    return text


def database_problem(path: Path, exc: sqlite3.DatabaseError) -> str:
    """What is wrong with the database file at `path`, in plain words, for
    every command that reads it (`status`, `ack`, `channels`,
    `export-fixtures`): one written by a newer version of xaucopier
    (G1L-5: running `watch` cannot bring *that* up to date), one from an
    older version, one that is busy, or a file that is not xaucopier's
    database at all (final review F25). The caller says what was, or was
    not, done."""
    if isinstance(exc, db.SchemaVersionError):
        if exc.newer:
            return (
                f"The database at {path} was written by a newer version of xaucopier; "
                "use that version. This version does not read or change it."
            )
        return (
            f"The database at {path} is from an older version of xaucopier. Run "
            f"`{_CMD} watch` once to bring it up to date, then try again."
        )
    if db.is_lock_error(exc):
        return (
            f"The database is busy -- watch is probably writing to it ({path}). Try "
            "again in a moment."
        )
    if isinstance(exc, sqlite3.OperationalError):
        return f"The database at {path} could not be used ({exc})."
    return (
        f"The file at {path} is not xaucopier's database, or it is damaged ({exc}). "
        "Check that --db names the right file."
    )
