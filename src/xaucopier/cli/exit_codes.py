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

from xaucopier.ingest.client import EXIT_TERMINAL_AUTH
from xaucopier.ingest.listener import EXIT_LISTENER_STOPPED

#: A one-shot command finished and did what it was asked.
EXIT_OK = 0
#: A one-shot command could not do what it was asked (the message says why).
EXIT_FAILED = 1
#: The command line itself was wrong (argparse's own code).
EXIT_USAGE = 2
#: Telegram has logged this session out, or it is otherwise dead
#: (`client.TERMINAL_AUTH_ERRORS`). Do NOT restart: run `setup-login`.
assert EXIT_TERMINAL_AUTH == 3
#: `watch` stopped listening -- a network drop it could not ride out, a
#: database write that failed, anything unexpected. Restart it.
assert EXIT_LISTENER_STOPPED == 4
#: A setup problem a restart cannot fix: a missing or malformed config
#: file, a channel switched on with `channel_id = 0`, no channel switched
#: on at all, a missing database folder or an unusable database file, a
#: missing or unreadable Telegram session file. Do NOT restart: fix it.
EXIT_CONFIG_ERROR = 5
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
        "Telegram logged this session out -- do not restart; run setup-login again",
    ),
    (EXIT_LISTENER_STOPPED, "watch stopped unexpectedly -- safe to start it again"),
    (
        EXIT_CONFIG_ERROR,
        "a setup problem (config file, channel id, database folder, session "
        "file) -- do not restart until it is fixed",
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


def help_epilog() -> str:
    """The exit-code table printed at the bottom of `--help`."""
    lines = ["exit codes:"]
    lines.extend(f"  {code:>3}  {text}" for code, text in MEANINGS)
    return "\n".join(lines)
