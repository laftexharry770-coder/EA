"""`xaucopier ack` -- see the alerts `status` lists, and clear them.

An alert stays on `status`'s first screen until someone clears it: that
is what keeps it from being missed. But one that has been dealt with must
be clearable, or `ALERTS: none` -- what the runbook tells the user to
expect -- is never true again after the first deleted signal.

- `ack` lists every uncleared alert (CRITICAL first), read-only.
- `ack NUMBER` clears one; `ack --all` clears them all
  (`store/alerts.py`'s `ack()` / `ack_all()`, one short write that a
  running `watch` does not notice).

Two alerts clear themselves and need no `ack`: TELEGRAM_SESSION_DEAD and
TELEGRAM_API_ID_REJECTED are acknowledged by `watch`'s poll as soon as it
reads the channel again, because that proves the fix worked. Clearing one
by hand does not fix the login -- the command says so.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

from xaucopier.cli import exit_codes, paths
from xaucopier.cli.status import alert_lines, critical_first
from xaucopier.store import alerts, db

_CMD = paths.USER_COMMAND


def _reminder(cleared: list[alerts.Alert]) -> None:
    codes = {alert.code for alert in cleared} & set(alerts.SESSION_ALERT_CODES)
    if codes:
        print(
            f"Note: clearing {', '.join(sorted(codes))} does not fix the login. If you have "
            f"not yet done what it says, do that now (see `{_CMD} status`)."
        )


def _list(args: argparse.Namespace) -> int:
    try:
        conn = db.connect_readonly(args.db)
    except db.SchemaVersionError as exc:
        print(f"{exc}. Run `{_CMD} watch` once to bring it up to date.", file=sys.stderr)
        return exit_codes.EXIT_FAILED
    try:
        uncleared = critical_first(alerts.unacked(conn))
    finally:
        conn.close()
    if not uncleared:
        print("No alerts to clear.")
        return exit_codes.EXIT_OK
    print("\n".join(alert_lines(uncleared)))
    print(
        f"To clear one: `{_CMD} ack NUMBER` (the number after #). "
        f"To clear them all: `{_CMD} ack --all`."
    )
    return exit_codes.EXIT_OK


def cmd_ack(args: argparse.Namespace) -> int:
    if not args.db.is_file():
        print(f"No recordings yet: there is no database at {args.db}.", file=sys.stderr)
        return exit_codes.EXIT_FAILED
    if args.alert_id is None and not args.all:
        return _list(args)

    try:
        return _clear(args)
    except sqlite3.OperationalError as exc:
        # SQLite waited out its 10 s busy timeout (or could not write at
        # all): whatever this call did was rolled back with the connection.
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            print(
                "The database is busy -- watch is probably writing to it. Nothing was "
                "cleared; try again in a moment.",
                file=sys.stderr,
            )
        else:
            print(
                f"The database could not be changed ({exc}). Nothing was cleared.",
                file=sys.stderr,
            )
        return exit_codes.EXIT_FAILED


def _clear(args: argparse.Namespace) -> int:
    conn = db.connect(args.db, role="writer")
    try:
        uncleared = {alert.alert_id: alert for alert in alerts.unacked(conn)}
        if args.all:
            count = alerts.ack_all(conn)
            conn.commit()
            print(f"Cleared {count} alert(s).")
            _reminder(list(uncleared.values()))
            return exit_codes.EXIT_OK
        try:
            alerts.ack(conn, alert_id=args.alert_id)
        except (ValueError, OverflowError):
            # OverflowError: a number too big for SQLite -- so no such alert.
            conn.rollback()
            print(
                f"There is no uncleared alert #{args.alert_id}. Run `{_CMD} ack` to see them.",
                file=sys.stderr,
            )
            return exit_codes.EXIT_FAILED
        conn.commit()
        print(f"Cleared alert #{args.alert_id}.")
        _reminder([uncleared[args.alert_id]])
        return exit_codes.EXIT_OK
    finally:
        conn.close()
