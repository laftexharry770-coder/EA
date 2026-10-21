"""docs/RUNBOOK.md is for the user, but it states facts about the code:
the exit codes, and the commands to type. These tests fail if the two
drift apart."""
from __future__ import annotations

import dataclasses
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import xaucopier.cli.main as cli_main
from xaucopier.cli import exit_codes
from xaucopier.domain.errors import RejectReason
from xaucopier.domain.followup_planner import LegView, plan_followup
from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.planner import distribute, plan_entry
from xaucopier.domain.signal import FollowUp, FollowUpOp, Side
from xaucopier.domain.trailing import OpenLeg, target_stops
from xaucopier.parsing.channel_mmh import parse

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
        "What it posts while it is off is **not read**",
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
        relay_repo.OLD_COPIES_OF_CHANNEL_OFF_SIGNALS,
        relay_repo.SIGNAL_SHOWN_ONLY_ON_CHANNEL_OFF,
        relay_repo.OLD_SIGNALS_SHOWN_ONLY_ON_CHANNEL_OFF,
        relay_repo.FOLLOWUP_ABOUT_CHANNEL_OFF_SIGNAL,
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
    # Final fix round 4 (R4-1): the promise that it is not read is
    # qualified where it is made -- and round 5 (R5-5): the read around each
    # switch grows with how far the PC's clock is off, and no bold headline
    # says it is ignored for good.
    flat = " ".join(RUNBOOK.split())
    assert "ignored for good" not in flat
    assert (
        "not even after you switch it back on — except what it posts close to either switch: "
        "within about a minute of each, and more if your PC's clock is off, by as much again "
        "as it is off" in flat
    )
    assert (
        "So if you switch it back on within about 2 minutes — plus twice as much as your PC's "
        "clock is off — everything it posted while it was off is read" in flat
    )
    assert "If your PC's clock is off when `watch` starts, `watch` reads that much more" in flat
    # Final fix round 6 (R6-4): it reads more for any difference, but warns
    # only past a minute.
    assert (
        "`watch` reads that much more around each switch — and, when it is more than a minute "
        "off, warns you to fix the clock." in flat
    )


def test_the_runbook_says_a_channel_back_on_acts_on_the_trade_made_instead() -> None:
    """Final fix round 7 (R7-1): a channel switched back on speaks about its
    old call to the trade made in its place."""
    flat = " ".join(RUNBOOK.split())
    assert (
        "A call the channel posted before you switched it off that another channel's post was "
        "traded in place of (**COPY_OF_CHANNEL_OFF_SIGNAL_ADMITTED**) stays that one trade: "
        "what the channel says about it once it is back on — a \"close half\" replying to it, "
        "an edit, deleting it — acts on the trade made instead." in flat
    )


def test_the_runbook_says_a_call_on_a_channel_on_is_still_traded_after_a_switch_off() -> None:
    """Final fix round 3 (R3-1): switching a channel off cancels its waiting
    signals, never the same call standing on a channel switched on."""
    assert "the same call posted by a channel that is\n  switched on is still traded" in RUNBOOK


def test_the_runbook_says_a_change_to_an_instruction_not_carried_out_is_reported() -> None:
    """Final fix round 7 (R7-4): an edit or deletion of a message that was not
    carried out, and perhaps applied by hand, is reported with what to do."""
    assert (
        "If that message is edited or deleted later, a\n  **FOLLOWUP_COPY_CHANGED** alert says "
        "it was not carried out, and what it\n  says now: if you applied it by hand, review "
        "what you did." in RUNBOOK
    )


# ---------------------------------------------------------------------------
# Plan 1b Task 5: "Choosing how many positions and what lot size". Its two
# tables are worked on the real signal (BUY 4480-4485, SL 4470, TPs 4490 /
# 4495 / 4500 / 4505), and are checked here against the code that will
# trade it.
# ---------------------------------------------------------------------------

POSITIONS_SECTION = RUNBOOK.split("## Choosing how many positions and what lot size", 1)[-1] \
    .split("\n## ", 1)[0]
REAL_TPS = (4490.0, 4495.0, 4500.0, 4505.0)
GOLD = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)


def _real_signal():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "messages" / "mmh_buy_4tp.json")
                         .read_text(encoding="utf-8"))
    result = parse(fixture["text"], channel_id=fixture["channel_id"],
                   message_id=fixture["message_id"],
                   posted_at_utc=datetime.fromisoformat(fixture["posted_at_utc"]))
    assert result.ok and result.signal.tps == REAL_TPS, result
    return result.signal


def test_the_runbook_explains_positions_lot_layered_entry_and_the_trail() -> None:
    assert "## Choosing how many positions and what lot size" in RUNBOOK
    flat = " ".join(POSITIONS_SECTION.split())
    for phrase in (
        "This version does not place trades yet.",
        "**`fixed_lot`** is the lot size of **each** position",
        "**What you risk is positions × lot.** 4 positions of 0.01 lot are 0.04 lot in total",
        "4 × 12.55 = $50.20",
        "the extra ones go to the last",
        "the first two",
        "It is on unless you switch it off, also while `positions` is left out.",
        "two wait at 4482.70 and 4480",
        "On this signal that is a price above 4486.25: at 4486 two still buy at once, "
        "at 4487.50 none do.",
        "cancelled when the price reaches the first take profit, or 4 hours after the signal",
        "is **not placed** at all",
        '"not placed: price already past TP1"',
        "delete the `# ` in front of `layered_entry = false`",
        "It **only ever moves in your favour**.",
        'A channel message like "move SL to …" is followed only when it moves the stop '
        "loss in your favour. One that would move it back is not followed.",
        "There is no `lot` setting; the lot is `fixed_lot`.",
    ):
        assert phrase in flat, phrase


def test_the_runbooks_take_profit_table_is_what_the_planner_does() -> None:
    rows = re.findall(r"^\| `positions = (\d+)` \| ([\d, ]+)", POSITIONS_SECTION,
                      flags=re.MULTILINE)
    assert [n for n, _ in rows] == ["4", "6", "2", "1"]
    for n, documented in rows:
        planned = tuple(tp for _, tp in distribute(int(n), tuple(enumerate(REAL_TPS))))
        assert tuple(float(tp) for tp in documented.split(",")) == planned, n


def test_the_runbooks_layered_entry_examples_are_what_the_planner_does() -> None:
    """Each position's (order kind, entry price) when the real signal is
    planned with 4 positions at the ask the runbook names."""
    signal = _real_signal()

    def entries(ask: float, layered: bool = True) -> list[tuple[str, float]]:
        account = AccountSpec("ic_demo", "XAUUSD", fixed_lot=0.01, magic_base=77001,
                              positions=4, layered_entry=layered)
        at = signal.posted_at_utc + timedelta(seconds=5)
        snap = MarketSnapshot(bid=round(ask - 0.10, 2), ask=ask, tick_time_utc=at,
                              spread_usd=0.10, median_spread_usd=0.10, now_utc=at)
        plan = plan_entry(signal, account, GOLD, snap, Policy())
        assert plan.accepted, plan
        return [(leg.kind.value, leg.price) for leg in plan.legs]

    # "with the price at 4483 ... two buy at once at 4483 (take profits 4490
    # and 4500) and two wait at 4482.70 and 4480 (take profits 4495 and 4505)"
    assert entries(4483.00) == [("market", 4483.0), ("limit", 4482.7),
                                ("market", 4483.0), ("limit", 4480.0)]
    # "below the whole zone ... all the positions open at once"
    assert entries(4479.00) == [("market", 4479.0)] * 4
    # "a price above 4486.25: at 4486 two still buy at once, at 4487.50 none
    # do", and the waiting half then spreads over the whole zone, 4485-4480.
    for ask in (4486.00, 4486.25):
        assert [kind for kind, _ in entries(ask)] == ["market", "limit", "market", "limit"], ask
    for ask in (4486.26, 4487.50):
        assert entries(ask) == [("limit", 4485.0), ("limit", 4485.0),
                                ("limit", 4485.0), ("limit", 4480.0)], ask
    # Switched off: "at once while the price is in the zone or better,
    # otherwise as limit orders at the zone's best edge (the bottom, for a buy)"
    assert entries(4483.00, layered=False) == [("market", 4483.0)] * 4
    assert entries(4479.00, layered=False) == [("market", 4479.0)] * 4
    assert entries(4487.50, layered=False) == [("limit", 4480.0)] * 4


def test_the_runbooks_trailing_table_is_what_the_trail_does() -> None:
    """Row by row, the stop of the last position (TP4, bought at 4482.55)
    as the trail moves it, and how many positions are still open."""
    rows = re.findall(r"^\| (\d{4}\.\d{2})[^|]*\| [^|\d]*(\d{4}\.\d{2})[^|]*\| (\d)\b",
                      POSITIONS_SECTION, flags=re.MULTILINE)
    assert [price for price, _, _ in rows] == [
        "4490.00", "4492.00", "4495.00", "4497.50", "4500.00", "4502.00"]
    now = datetime(2026, 9, 5, 10, 46, tzinfo=UTC)
    leg = OpenLeg(position_ticket=1, price_open=4482.55, current_sl=4470.0)
    for price, stop, still_open in rows:
        bid = float(price)
        snap = MarketSnapshot(bid=bid, ask=round(bid + 0.10, 2), tick_time_utc=now,
                              spread_usd=0.10, median_spread_usd=0.10, now_utc=now)
        for move in target_stops(Side.BUY, REAL_TPS, (leg,), snap, GOLD, Policy()):
            leg = dataclasses.replace(leg, current_sl=move.new_sl)
        assert leg.current_sl == float(stop), price
        assert int(still_open) == sum(tp > bid for tp in REAL_TPS), price


def test_the_runbooks_move_sl_rule_is_what_the_follow_up_planner_does() -> None:
    """At the trailing table's last row the bid is 4502 and the last
    position's stop is 4500. "SL to 4495" would move it back; "SL to 4501"
    moves it in the user's favour."""
    now = datetime(2026, 9, 5, 10, 50, tzinfo=UTC)
    snap = MarketSnapshot(bid=4502.0, ask=4502.10, tick_time_utc=now,
                          spread_usd=0.10, median_spread_usd=0.10, now_utc=now)
    last = LegView(position_ticket=1, leg_index=3, tp_index=3, side=Side.BUY, volume=0.01,
                   price_open=4482.55, sl=4500.0, tp=4505.0, is_pending=False)

    def move_sl_to(price: float):
        follow_up = FollowUp(channel_id=-1001111111111, message_id=4002,
                             op=FollowUpOp.AMEND_SL, posted_at_utc=now,
                             reply_to_message_id=4001, price_hint=price)
        return plan_followup(follow_up, (last,), GOLD, snap, Policy())

    back = move_sl_to(4495.0)
    assert back.intents == () and back.reason is RejectReason.WOULD_LOOSEN
    favour = move_sl_to(4501.0)
    assert favour.reason is None and [intent.sl for intent in favour.intents] == [4501.0]
