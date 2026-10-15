"""Every exit code a xaucopier command can return, and what it means.

This is the contract with whatever runs `xaucopier watch` -- the user
today, Plan 3's supervisor tomorrow. The one rule a supervisor needs is
which codes it may restart on: **only 4**. Every other non-zero code is
a condition a restart cannot fix, so restarting on it would loop forever
(the reason 5 exists: before it, a missing database folder exited 4 and
a supervisor would have restarted it hot, for ever).

`--help` prints `MEANINGS`; docs/RUNBOOK.md has the same table in the
user's words, and tests/test_runbook.py fails if its codes and these
ever differ.
"""
from __future__ import annotations

from datetime import UTC, datetime

from xaucopier.cli.paths import USER_COMMAND
from xaucopier.ingest.client import EXIT_CONFIG_ERROR, EXIT_TERMINAL_AUTH
from xaucopier.ingest.listener import EXIT_LISTENER_STOPPED, start_after_words

#: A one-shot command finished and did what it was asked.
EXIT_OK = 0
#: A one-shot command could not do what it was asked (the message says why).
EXIT_FAILED = 1
#: The command line itself was wrong (argparse's own code).
EXIT_USAGE = 2
#: Telegram refuses this session or account (`client.TERMINAL_AUTH_ERRORS`:
#: logged out, revoked, used from two places at once, deactivated, banned,
#: frozen). Do NOT restart: run `setup-login`.
assert EXIT_TERMINAL_AUTH == 3
#: `watch` stopped listening -- a network drop it could not ride out, a
#: database write that failed, anything unexpected. Restart it.
assert EXIT_LISTENER_STOPPED == 4
#: A setup problem a restart cannot fix: a missing or malformed config
#: file, a channel switched on with `channel_id = 0`, no channel switched
#: on at all, a missing database folder or an unusable database file, a
#: missing or unreadable Telegram session file -- or an api_id/api_hash
#: Telegram rejects (`client.API_CREDENTIAL_ERRORS`). Do NOT restart: fix it.
assert EXIT_CONFIG_ERROR == 5
#: Another `watch` (or `resolve-channels`) already holds this machine's
#: Telegram session. Do NOT start a second one: two processes on one
#: session can get it permanently revoked by Telegram.
EXIT_ALREADY_RUNNING = 6
#: The user pressed Ctrl+C. Everything recorded so far is saved.
EXIT_INTERRUPTED = 130

#: Plain-language meaning of each code, for `--help`, `status` and the
#: runbook.
MEANINGS: tuple[tuple[int, str], ...] = (
    (EXIT_OK, "finished normally"),
    (EXIT_FAILED, "the command could not do what was asked; the message says why"),
    (EXIT_USAGE, "the command line was wrong; see --help"),
    (
        EXIT_TERMINAL_AUTH,
        "Telegram refuses this login (logged out, used twice at once, or the account "
        f"is banned) -- do not restart; run `{USER_COMMAND} setup-login` again",
    ),
    (EXIT_LISTENER_STOPPED, "watch stopped unexpectedly -- safe to start it again"),
    (
        EXIT_CONFIG_ERROR,
        "a setup problem (config file, channel id, database folder, session file, "
        "or an api_id/api_hash Telegram rejects) -- do not restart until it is fixed",
    ),
    (
        EXIT_ALREADY_RUNNING,
        "watch is already running on this computer -- do not start a second one",
    ),
    (EXIT_INTERRUPTED, "stopped with Ctrl+C -- everything recorded so far is saved"),
)

#: Codes after which restarting `watch` is the right thing to do.
RESTARTABLE: frozenset[int] = frozenset({EXIT_LISTENER_STOPPED})


def meaning(code: int) -> str:
    """The plain-language meaning of `code`, or "unknown exit code"."""
    for known, text in MEANINGS:
        if known == code:
            return text
    return "unknown exit code"


def stopped_with(code: int, start_after_utc: datetime | None, *, now: datetime) -> str:
    """How watch's last line and `status` name a stop: "exit code 4: watch
    stopped unexpectedly -- safe to start it again" -- or, when Telegram
    asked watch to wait at start (`start_after_utc`), when to start it
    again instead (final fix FF21, round 2): starting it sooner only
    earns a longer wait. Said as it stands at `now` (round 3, R3-2): a
    time not today carries its date, and one that has passed is said to
    have passed -- safe to start it again."""
    if start_after_utc is None:
        return f"exit code {code}: {meaning(code)}"
    if now >= start_after_utc:
        return (
            f"exit code {code}: Telegram had asked watch to wait until "
            f"{start_after_utc.astimezone(UTC):%H:%M} UTC; that time has passed -- safe to "
            "start it again"
        )
    return (
        f"exit code {code}: Telegram asked watch to wait -- start it again after "
        f"{start_after_words(start_after_utc, now)}"
    )


def help_epilog() -> str:
    """The exit-code table printed at the bottom of `--help`."""
    lines = ["exit codes:"]
    lines.extend(f"  {code:>3}  {text}" for code, text in MEANINGS)
    return "\n".join(lines)
