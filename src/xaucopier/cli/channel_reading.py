"""Where the next `watch` starts reading a channel, as the channel commands
change it -- each in one short transaction on the database file.

- `channels off` records when it was switched off (`record_switch_off`);
- `channels on` records when it came back (`record_switch_on`), so the
  next `watch` skips exactly what the channel posted while it was off
  (fix round 2 of final review F28; `store/channel_state.py` says how);
- `channels add` and `resolve-channels` of a channel read before -- removed
  from channels.toml and added back -- ask for it to be read from now on
  (`reseed_from_now`, group-2 leftover G2L-3).

No database file yet: nothing is written and None is returned -- the
first `watch` seeds every channel at its own start anyway. A database
another program holds past SQLite's wait (10 s), one from a newer version,
or a file that is not a database raises `sqlite3.DatabaseError`, with
nothing written; each caller says so in plain words.

Here, not in `cli/channels.py`, because `cli/resolve.py` uses it too and
`cli/channels.py` imports `cli/resolve.py`.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from xaucopier.store import channel_state, db

T = TypeVar("T")


def _in_one_transaction(db_path: Path, job: Callable[[sqlite3.Connection], T]) -> T | None:
    """`job(conn)` in one `BEGIN IMMEDIATE` transaction on `db_path`,
    committed; None, with nothing created, if there is no such file."""
    if not db_path.is_file():
        return None
    conn = db.connect(db_path, role="writer")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = job(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return result
    finally:
        conn.close()


def record_switch_off(db_path: Path, channel_id: int, now: datetime) -> bool | None:
    """`channels off`: open the channel's OFF period at `now` if a `watch`
    has started reading it (`channel_state.record_switch_off`). Returns
    whether one was opened; None with no database."""
    return _in_one_transaction(
        db_path,
        lambda conn: channel_state.record_switch_off(conn, channel_id=channel_id, off_at_utc=now),
    )


def record_switch_on(
    db_path: Path, channel_id: int, now: datetime, *, may_replace_first_cutoff: bool
) -> channel_state.SwitchOn | None:
    """`channels on`: end the channel's OFF period at `now`
    (`channel_state.record_switch_on`). None with no database."""
    return _in_one_transaction(
        db_path,
        lambda conn: channel_state.record_switch_on(
            conn,
            channel_id=channel_id,
            on_at_utc=now,
            may_replace_first_cutoff=may_replace_first_cutoff,
        ),
    )


def reseed_from_now(
    db_path: Path, channel_id: int, now: datetime, *, may_replace_first_cutoff: bool
) -> channel_state.ReseedOutcome | None:
    """A channel read before, added back: the next `watch` reads it from
    `now` on (`channel_state.reseed`). None with no database."""
    return _in_one_transaction(
        db_path,
        lambda conn: channel_state.reseed(
            conn,
            channel_id=channel_id,
            cutoff_utc=now,
            may_replace_first_cutoff=may_replace_first_cutoff,
        ),
    )
