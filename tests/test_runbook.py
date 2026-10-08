"""docs/RUNBOOK.md is for the user, but it states facts about the code:
the exit codes, and the commands to type. These tests fail if the two
drift apart."""
from __future__ import annotations

import re
from pathlib import Path

import xaucopier.cli.main as cli_main
from xaucopier.cli import exit_codes

RUNBOOK = (Path(__file__).resolve().parents[1] / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")


def test_the_exit_code_table_lists_exactly_the_codes_the_cli_returns() -> None:
    documented = {int(code) for code in re.findall(r"^\| (\d+) \|", RUNBOOK, flags=re.MULTILINE)}
    assert documented == {code for code, _ in exit_codes.MEANINGS}


def test_every_command_the_runbook_tells_the_user_to_type_exists() -> None:
    parser = cli_main.build_parser()
    commands = set(parser._subparsers._group_actions[0].choices)  # noqa: SLF001 -- argparse has no public accessor
    typed = set(re.findall(r"\.\\xaucopier ([a-z][a-z-]*)", RUNBOOK))  # a command, not --help
    assert typed, "the runbook names no command"
    assert typed <= commands, typed - commands
    assert {"setup-login", "resolve-channels", "watch", "status", "ack", "channels"} <= typed


def test_the_runbook_never_invites_a_blank_2fa_password() -> None:
    step_2 = RUNBOOK.split("## 2.", 1)[1].split("## 3.", 1)[0]
    password_step = step_2.split("5. **", 1)[1].split("\n\n", 1)[0]
    assert "two-step verification" in password_step
    assert "press Enter" not in password_step
    assert "leave blank" not in step_2.lower() and "just press Enter" not in step_2


def test_the_runbook_says_what_the_user_must_know() -> None:
    for phrase in (
        "never stores the login code, your 2FA password",
        "XAUCOPIER-TRADEBOX",
        "Do not terminate it",
        "Set time automatically",
        "session.dpapi",
        "Never use an api_id or api_hash you found in a tutorial",
        # Fix round 1: a .cmd asks this on Ctrl+C; answering Y is the way out.
        "Terminate batch job (Y/N)?",
        # A stuck watch still holds the login: stop it before starting another.
        "press **Ctrl+C** and answer **Y** first",
        # Exit 3's likeliest silent cause.
        "a second copy of the session was used somewhere else",
        # How to clear alerts, and which need action first.
        "ack --all",
        # Fix round 2: the password step only appears with 2FA on, and then
        # a blank answer is wrong -- never "just press Enter".
        "two-step verification is turned on** for your Telegram account",
        "it cannot be left blank",
        "TELEGRAM_SESSION_DEAD",
        "TELEGRAM_API_ID_REJECTED",
        # Task 12: choosing channels -- how, when it takes effect, and the
        # format caveat for a new channel.
        "## Choosing which channels to follow",
        ".\\xaucopier channels add",
        ".\\xaucopier channels off 2",
        "A change takes effect the next time `watch` starts",
        "If every channel is OFF, `watch` will not start",
        # Final review F07: a signal in another layout is recorded, not
        # traded; an instruction-looking message is recorded as one.
        "recorded but **not traded**",
        "is still recorded as an instruction, whatever its",
        "5–10 of its real messages",
        # The final fix wave. F15 / F28 (ruling M12): what OFF does.
        'follow-ups (like "close all") of the signals it already posted',
        "posts while it is off is ignored for good",
        # Fix round 2 of F28: OFF skips exactly the time it was off.
        "whatever it posted **before you\n  switched it off** that `watch` had not read yet",
        "it made while it was off are skipped, and everything from the moment you\n  switched it on is read",
        "each time it was off is\n  skipped, and only that",
        # Final fix round 2 (FF11 case B): the re-run saves it then too.
        "it then saves the time you run it (and says so) —\n  also when you had switched the "
        "channel off and on before",
        "switching it back on skips\n  more than what it posted while it was off",
        # F05: a group is not a channel.
        "anyone in it can post, and **every member's message would be",
        # F27: the Telegram id is not the channel number.
        "writes the channel's **Telegram id**",
        "not the long Telegram id",
        # FF25 (b): exit 5 is about a channel's Telegram id, not its number.
        "a config file, a channel's Telegram id, the data folder",
        # F26: the exit code is printed.
        "its last line is its exit code",
        # F06 / exit 3 and exit 4.
        '*AuthKeyNotFound* ("auth key not found")',
        "Could not reach Telegram within 60 seconds",
        # F17 / STATUS-NEW-METRICS: missed by the live feed vs caught up.
        "*caught up after a stop*",
        "FOR INFORMATION -- NOTHING TO DO",
        # F34: a login Telegram accepted before the failure.
        "Telegram had already accepted this login",
        # G1L-5: a database from a newer version.
        "use that newer version",
        # G4L-1: an unread instruction is applied by hand.
        "**apply it by hand**",
        # G2L-1: the administrator-window hint.
        "running in a window opened as administrator",
    ):
        assert phrase in RUNBOOK, phrase


def test_the_runbook_explains_every_alert_the_fix_wave_added() -> None:
    """Each by its code, in the list of what to do before clearing one."""
    from xaucopier.store import alerts, edits_repo, messages_repo, relay_repo

    clearing = RUNBOOK.split("### Clearing alerts", 1)[1].split("\n## ", 1)[0]
    for code in (
        messages_repo.TEMPLATE_CHANGED_SUSPECTED,
        messages_repo.ENTRY_SHAPED_INSTRUCTION_NOT_QUEUED,
        edits_repo.EDIT_COMPLETED_SIGNAL,
        edits_repo.SIGNAL_REPOST_PROMOTED,
        edits_repo.SIGNAL_REPOST_ALREADY_LIVE,
        edits_repo.CANCELLED_WHEN_FIRST_READ,
        alerts.CHANNEL_OFF_ENTRIES_SKIPPED,
        alerts.TELEGRAM_CHANNEL_UNREADABLE,
        relay_repo.COPY_OF_CHANNEL_OFF_SIGNAL_ADMITTED,
    ):
        assert f"- **{code}** — " in clearing, code


def test_the_runbook_uses_none_of_the_developer_words() -> None:
    """The user knows this program and `.\\xaucopier watch`, not a
    client, a listener, an RPC or a flood wait."""
    for word in ("client", "listener", "rpc", "flood"):
        assert word not in RUNBOOK.lower(), word


def test_the_exit_4_row_says_to_wait_when_telegram_asked_watch_to_wait() -> None:
    """Final fix FF21: watch stops with exit 4 when Telegram asks it to wait
    before starting, and every start sooner earns a longer wait -- so
    "safe to start it again" needs its exception, in the row the user
    reads."""
    (row,) = re.findall(r"^\| 4 \|.*$", RUNBOOK, flags=re.MULTILINE)
    assert "Telegram asked watch to wait" in row
    assert "after the time it names" in row
    # Final fix round 3 (R3-2): the date, and a time that has passed.
    assert "with its date, when that is not today" in row
    assert "Once that time has passed" in row


def test_the_runbook_says_a_clock_put_right_after_the_switches_costs_nothing() -> None:
    """Final fix round 3 (R3-4): each switch keeps the clock difference the
    last watch start measured, so the old warning that a clock put right
    before the next start can cost a post is gone; what is left is named."""
    assert "is put\n  right before `watch` starts again costs nothing" in RUNBOOK
    assert "Only a clock that went\n  wrong after `watch` last started" in RUNBOOK
    assert "right again when `watch`\n  starts, can still cost" not in RUNBOOK


def test_the_runbook_says_a_short_switch_off_may_read_everything_it_posted() -> None:
    """Final fix round 3 (R3-3): a channel off for only a few minutes may
    have posted nothing outside the margin read around the switches; then
    what it posted while off is read and handled, and watch says so."""
    assert "A channel switched off for only a few minutes" in RUNBOOK
    assert "a signal it posted then is handled like any other" in RUNBOOK
    # Final fix round 4 (R4-1): the promise that it is ignored for good is
    # qualified where it is made.
    assert (
        "not even after you switch it back on — except\n  what it posts within about a "
        "minute of either switch" in RUNBOOK
    )
    assert "So if you switch it\n  back on within about 2 minutes, everything it posted" in RUNBOOK


def test_the_runbook_says_a_call_on_a_channel_on_is_still_traded_after_a_switch_off() -> None:
    """Final fix round 3 (R3-1): switching a channel off cancels its waiting
    signals, never the same call standing on a channel switched on."""
    assert "the same call posted by a channel that is\n  switched on is still traded" in RUNBOOK
