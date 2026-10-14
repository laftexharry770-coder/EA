"""A call on a channel switched ON, linked to a signal of a channel switched
OFF while watch ran (final fix round 3, R3-1).

While watch runs, `channels off` changes nothing until watch restarts: it
reads the channel all along. So the relay posts a call (signal 1, entries
QUEUED), and 30 s later the VIP posts the same call by hand -- stored as a
copy of signal 1, as "a live call wins" and signal 1 can still trade. At the
restart signal 1's entries are cancelled ('channel switched off'), and the
VIP's call, which still stands on a channel switched on, was never traded:
the only alert named the relay's entries.

Now the start-up job that cancels them looks, for each signal whose
entries it cancels, for standing copies on channels switched on, and runs
the newest through the relay rules again -- signal 1 can no longer trade,
so it is a new signal dated by its own post, queued, with a WARNING naming
both messages. Restarting between the two posts already made the VIP's
post a signal of its own; both orders end the same.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fakes.telegram import FakeTelegramClient
from test_relay import (
    ACCOUNTS,
    CLOSE_HALF,
    OTHER,
    OTHER_CALL,
    POSTED,
    RELAY,
    SIGNAL_TEXT,
    SL_TO_BE,
    VIP,
    R,
    V,
    _ingest,
    _link,
    _q,
    conn,  # noqa: F401 -- the fixture
)
from xaucopier.config.channels import ChannelCfg
from xaucopier.store import db, edits_repo, messages_repo, outbox, relay_repo

RELAY_CALL = 7001
VIP_COPY = 4001
VIP_REPOST = 4012
OTHER_COPY = 9001
CODE = relay_repo.COPY_OF_CHANNEL_OFF_SIGNAL_ADMITTED
#: The call re-aimed: another entry zone.
REAIMED = SIGNAL_TEXT.replace("4480-4485", "4476-4479")


def _at(seconds: float) -> datetime:
    return POSTED + timedelta(seconds=seconds)


def _restart(conn, *, on=(VIP, OTHER), seconds: float = 120, **kwargs):  # noqa: F811
    """`watch` starts with only `on` switched on: the start-up job, as the
    listener runs it."""
    enabled = tuple(channel.channel_id for channel in on)
    signal_ids = outbox.signals_with_entries_of_channels_off(conn, enabled_channel_ids=enabled)
    skipped = outbox.skip_entries_of_channels_off(conn, enabled_channel_ids=enabled)
    raised = relay_repo.admit_copies_of_switched_off(
        conn, signal_ids=signal_ids, channels=on, accounts=ACCOUNTS, now_utc=_at(seconds),
        **kwargs,
    )
    conn.commit()
    return skipped, raised


def _queued(conn) -> list[tuple]:  # noqa: F811
    return _q(
        conn,
        "SELECT signal_id, account_id FROM outbox WHERE kind = 'ENTRY' AND state = 'QUEUED' "
        "ORDER BY outbox_id",
    )


def _signals(conn) -> list[tuple]:  # noqa: F811
    return _q(conn, "SELECT signal_id, channel_id, message_id, state FROM signals ORDER BY 1")


def _alerts(conn) -> list[tuple]:  # noqa: F811
    return _q(conn, "SELECT level, code, channel_id, message_id, signal_id, detail FROM alerts")


def _relay_call_then_vip_copy(conn) -> None:  # noqa: F811
    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    copy = _ingest(conn, VIP, VIP_COPY, posted=_at(30))
    assert (copy.outcome, copy.relay_of_signal_id) == ("relay_duplicate", 1), copy


def test_a_copy_on_a_channel_on_is_traded_when_its_signals_channel_is_switched_off(
    conn,  # noqa: F811
):
    """The re-check's p_off_while_running, restart after both posts."""
    _relay_call_then_vip_copy(conn)

    skipped, raised = _restart(conn)

    assert skipped == {R: len(ACCOUNTS)}
    assert _signals(conn) == [(1, R, RELAY_CALL, "NEW"), (2, V, VIP_COPY, "NEW")]
    assert _link(conn, VIP, VIP_COPY) is None  # its own signal now
    assert _queued(conn) == [(2, "acct-1"), (2, "acct-2")]
    (alert,) = _alerts(conn)
    assert alert[:5] == ("WARNING", CODE, V, VIP_COPY, 2)
    assert raised == (1,)
    detail = alert[5]
    for named in (f"message {VIP_COPY}", f"message {RELAY_CALL}", "signal 1", "signal 2"):
        assert named in detail, (named, detail)
    assert "switched off" in detail and "2 trade entries are queued" in detail
    for jargon in ("client", "listener", "rpc", "flood", "outbox"):
        assert jargon not in detail.lower(), (jargon, detail)


def test_a_restart_between_the_two_posts_ends_the_same(conn):  # noqa: F811
    """The control: the VIP's post came after the restart, so it was a
    signal of its own at once. A second restart changes nothing."""
    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    _restart(conn)
    assert _ingest(conn, VIP, VIP_COPY, posted=_at(30)).signal_id == 2

    assert _restart(conn, seconds=200) == ({}, ())

    assert _signals(conn) == [(1, R, RELAY_CALL, "NEW"), (2, V, VIP_COPY, "NEW")]
    assert _queued(conn) == [(2, "acct-1"), (2, "acct-2")]
    assert _alerts(conn) == []


def test_a_second_restart_admits_nothing_more(conn):  # noqa: F811
    _relay_call_then_vip_copy(conn)
    _restart(conn)

    assert _restart(conn, seconds=200) == ({}, ())

    assert len(_signals(conn)) == 2
    assert len(_alerts(conn)) == 1


def test_only_the_newest_standing_copy_is_traded_and_the_other_repeats_it(
    conn,  # noqa: F811
):
    """Two channels switched on copied the call: one call, one signal. The
    newest copy is the signal; the other repeats it."""
    _relay_call_then_vip_copy(conn)
    assert _ingest(conn, OTHER, OTHER_COPY, posted=_at(60)).relay_of_signal_id == 1

    _restart(conn)

    assert _signals(conn)[1:] == [(2, OTHER.channel_id, OTHER_COPY, "NEW")]
    assert _link(conn, VIP, VIP_COPY) == 2
    assert _queued(conn) == [(2, "acct-1"), (2, "acct-2")]
    assert [a[:5] for a in _alerts(conn)] == [("WARNING", CODE, OTHER.channel_id, OTHER_COPY, 2)]


def test_an_older_copy_the_newest_is_too_far_from_is_never_a_second_signal(
    conn,  # noqa: F811
):
    """The VIP copied the relay's call 800 s before it was posted, the other
    channel 800 s after: both copies of signal 1, but 1600 s apart. The
    newest is the signal -- never a second signal of the same call. The
    VIP's copy, outside its window, still repeats signal 1's call, which
    signal 2 carries now (final fix round 7, R7-1: the admission is a link
    of the call's chain): it repeats signal 2."""
    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    assert _ingest(conn, VIP, VIP_COPY, posted=_at(-800)).relay_of_signal_id == 1
    assert _ingest(conn, OTHER, OTHER_COPY, posted=_at(800)).relay_of_signal_id == 1

    _restart(conn)

    assert _signals(conn)[1:] == [(2, OTHER.channel_id, OTHER_COPY, "NEW")]
    assert _link(conn, VIP, VIP_COPY) == 2
    assert relay_repo.signal_for_post(conn, channel_id=V, message_id=VIP_COPY) == 2
    assert _queued(conn) == [(2, "acct-1"), (2, "acct-2")]


@pytest.mark.parametrize("how", ["deleted", "re-aimed"])
def test_a_newer_copy_deleted_or_re_aimed_is_passed_over(conn, how):  # noqa: F811
    """The VIP posted the call twice; the newer post was then deleted, or
    edited to another entry zone (a repeat removal: the older one still
    showed the call, so signal 1 stood). Only a post still showing the call
    is traded: the older one."""
    _relay_call_then_vip_copy(conn)
    assert _ingest(conn, VIP, VIP_REPOST, posted=_at(60)).relay_of_signal_id == 1
    if how == "deleted":
        edits_repo.apply_deletion(
            conn, channel_cfg=VIP, message_id=VIP_REPOST, now_utc=_at(70), accounts=ACCOUNTS,
            detected_by="event",
        )
    else:
        edits_repo.apply_edit(
            conn, channel_cfg=VIP, message_id=VIP_REPOST, text=REAIMED, posted_at_utc=_at(60),
            edit_date_utc=_at(70), reply_to_message_id=None, now_utc=_at(70),
            accounts=ACCOUNTS, detected_by="event",
        )
    conn.commit()
    assert _signals(conn) == [(1, R, RELAY_CALL, "NEW")]

    _restart(conn)

    assert _signals(conn)[1:] == [(2, V, VIP_COPY, "NEW")]
    assert _link(conn, VIP, VIP_REPOST) == 1
    assert _queued(conn) == [(2, "acct-1"), (2, "acct-2")]


def test_a_copy_on_a_channel_switched_off_too_is_not_traded(conn):  # noqa: F811
    _relay_call_then_vip_copy(conn)

    _restart(conn, on=(OTHER,))

    assert _signals(conn) == [(1, R, RELAY_CALL, "NEW")]
    assert _link(conn, VIP, VIP_COPY) == 1
    assert _queued(conn) == []


def test_a_signal_still_being_traded_keeps_its_copy(conn, caplog):  # noqa: F811
    """One account's entry is being traded: the signal still carries the
    call, so the VIP's copy repeats it as before -- trading it as well
    would open the trade twice. Its copies are not even looked at."""
    _relay_call_then_vip_copy(conn)
    assert outbox.claim(conn, account_id="acct-1", pid=7, now_utc=_at(40)) is not None
    conn.commit()

    with caplog.at_level(logging.INFO, logger="xaucopier.store.relay_repo"):
        _restart(conn)

    assert not [r for r in caplog.records if "now repeats signal" in r.getMessage()]

    assert _signals(conn) == [(1, R, RELAY_CALL, "NEW")]
    assert _link(conn, VIP, VIP_COPY) == 1
    assert _queued(conn) == []
    assert _alerts(conn) == []


def test_a_follow_up_replying_to_the_copy_acts_on_the_signal_it_became(
    conn,  # noqa: F811
):
    """The VIP replied "close half" to its post before the restart: that
    instruction is about the VIP's call, which is signal 2 now."""
    _relay_call_then_vip_copy(conn)
    _ingest(conn, VIP, 4005, CLOSE_HALF, posted=_at(40), reply_to=VIP_COPY)
    _ingest(conn, RELAY, 7005, SL_TO_BE, posted=_at(45), reply_to=RELAY_CALL)
    assert _q(conn, "SELECT target_signal_id, resolution_path FROM followups ORDER BY 1") == [
        (1, relay_repo.RELAY_REPLY_PATH), (1, relay_repo.REPLY_PATH)
    ]

    _restart(conn)

    rows = _q(
        conn,
        "SELECT followup_id, channel_id, target_signal_id, resolution_path FROM followups "
        "ORDER BY followup_id",
    )
    assert [row[1:] for row in rows] == [
        (V, 2, relay_repo.REPLY_PATH),
        (R, 1, relay_repo.REPLY_PATH),  # the relay's own instruction stays with its signal
    ]
    assert relay_repo.signal_for_followup(conn, followup_id=rows[0][0]) == 2


def test_a_follow_up_replying_to_a_copy_that_repeats_the_new_signal_acts_on_it(
    conn,  # noqa: F811
):
    """The VIP's copy repeats the other channel's, which became signal 2:
    the VIP's reply to its own copy is about that call too."""
    _relay_call_then_vip_copy(conn)
    _ingest(conn, OTHER, OTHER_COPY, posted=_at(60))
    _ingest(conn, VIP, 4005, CLOSE_HALF, posted=_at(70), reply_to=VIP_COPY)

    _restart(conn)

    assert _link(conn, VIP, VIP_COPY) == 2
    assert _q(conn, "SELECT target_signal_id, resolution_path FROM followups") == [
        (2, relay_repo.RELAY_REPLY_PATH)
    ]


# -- through the listener's start-up job -------------------------------------


@pytest.mark.parametrize("restart", ["after-both-posts", "between-the-posts"])
def test_watch_starting_with_the_relay_off_trades_the_vips_call_in_either_order(
    tmp_path: Path, restart: str
) -> None:
    """The re-check's p_off_while_running, through `Listener.start()` (fake
    Telegram, nothing read). Restarted after both posts: the job that
    cancels the relay's entries makes the VIP's copy the signal, in the
    same writer job. Restarted between them: the VIP's post was a signal
    of its own when stored. Both end the same: signal 2 queued."""
    from test_listener import ACCOUNTS as LISTENER_ACCOUNTS
    from test_listener import _build_listener

    db_path = tmp_path / "store.db"
    relay_off = ChannelCfg(
        channel_id=R, name=RELAY.name, parser="mmh", ordinal=RELAY.ordinal, enabled=False
    )

    def store(channel, message_id, seconds):
        writer = db.connect(db_path, role="writer")
        try:
            messages_repo.ingest(
                writer, channel_cfg=channel, message_id=message_id, text=SIGNAL_TEXT,
                posted_at_utc=_at(seconds), edit_date_utc=None, reply_to_message_id=None,
                source="live", now_utc=_at(seconds + 1), accounts=LISTENER_ACCOUNTS,
            )
            writer.commit()
        finally:
            writer.close()

    def start():
        async def scenario():
            listener = _build_listener(
                db_path, FakeTelegramClient(), channels=(VIP, relay_off),
                accounts=LISTENER_ACCOUNTS,
            )
            await listener.start()
            await listener.aclose()

        asyncio.run(scenario())

    # test_listener's clock stands at POSTED + 900 s: the VIP's copy is 70 s
    # old when watch starts, young enough to be traded (R4-2).
    store(RELAY, RELAY_CALL, 800)
    if restart == "between-the-posts":
        start()
    store(VIP, VIP_COPY, 830)
    start()

    check = db.connect(db_path, role="reader")
    try:
        assert _signals(check) == [(1, R, RELAY_CALL, "NEW"), (2, V, VIP_COPY, "NEW")]
        assert _queued(check) == [(2, "acct-a"), (2, "acct-b")]
        codes = [row[:2] for row in _alerts(check)]
    finally:
        check.close()
    expected = [("WARNING", "CHANNEL_OFF_ENTRIES_SKIPPED")]
    if restart == "after-both-posts":
        expected.append(("WARNING", CODE))
    assert codes == expected


def test_a_copy_the_relay_rules_hold_back_is_held_back_loudly(conn):  # noqa: F811
    """The VIP's copy (+890 s) is also within the window of the other
    channel's own signal of the same call (+950 s; 950 s after signal 1,
    so a signal of its own), which that channel then withdrew. Run through
    the relay rules again, the VIP's copy repeats that withdrawn call:
    held back, CRITICAL, as when it is first read -- never linked quietly,
    and never a signal. (Restarted at +970 s: the copy is 80 s old, still
    young enough to be traded, R4-2.)"""
    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    assert _ingest(conn, VIP, VIP_COPY, posted=_at(890)).relay_of_signal_id == 1
    assert _ingest(conn, OTHER, OTHER_COPY, posted=_at(950)).signal_id == 2
    edits_repo.apply_deletion(
        conn, channel_cfg=OTHER, message_id=OTHER_COPY, now_utc=_at(960), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()

    _restart(conn, seconds=970)

    assert [row[0] for row in _signals(conn)] == [1, 2]
    assert _link(conn, VIP, VIP_COPY) == 2
    assert _queued(conn) == []
    assert ("CRITICAL", relay_repo.COPY_OF_WITHDRAWN_SIGNAL, V, VIP_COPY, 2) in [
        a[:5] for a in _alerts(conn)
    ]


def test_a_post_holding_a_signal_of_its_own_is_never_admitted_again(conn):  # noqa: F811
    """One post, one signal. The VIP's call was signal 1; the relay's copy
    of it was deleted (withdrawing it) and the relay posted the call again:
    signal 2, replacing 1. The relay is switched off. The VIP's post still
    stands and shows the call, but it holds signal 1 already: it is passed
    over, and the start goes on."""
    assert _ingest(conn, VIP, VIP_COPY).signal_id == 1
    assert _ingest(conn, RELAY, RELAY_CALL, posted=_at(10)).relay_of_signal_id == 1
    edits_repo.apply_deletion(
        conn, channel_cfg=RELAY, message_id=RELAY_CALL, now_utc=_at(15), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()
    assert _ingest(conn, RELAY, 7002, posted=_at(20)).signal_id == 2

    assert _restart(conn) == ({R: len(ACCOUNTS)}, ())

    assert [row[0] for row in _signals(conn)] == [1, 2]
    assert _queued(conn) == []



def test_a_copy_on_a_channel_off_at_that_start_is_not_traded_when_switched_on_later(
    conn,  # noqa: F811
):
    """Both the relay and the VIP were off when watch restarted: signal 1's
    entries were cancelled, and the VIP's copy was not traded -- the VIP
    was off too, which cancels whatever it had waiting. Switched on again
    later, the VIP's old copy stays as it was: only the start that cancels
    a signal's entries looks at its copies."""
    _relay_call_then_vip_copy(conn)
    assert _restart(conn, on=(OTHER,)) == ({R: len(ACCOUNTS)}, ())

    assert _restart(conn, on=(VIP, OTHER), seconds=300) == ({}, ())

    assert _signals(conn) == [(1, R, RELAY_CALL, "NEW")]
    assert _link(conn, VIP, VIP_COPY) == 1
    assert _queued(conn) == []


# -- only a copy young enough to be traded (final fix round 4, R4-2) --------

OLD_CODE = relay_repo.OLD_COPIES_OF_CHANNEL_OFF_SIGNALS


def _ten_days_of_the_relay_and_the_vip(conn) -> None:  # noqa: F811
    """The re-check's p_r31_old: each day the relay posts a call (another SL
    each day, so another call) and the VIP repeats it 30 s later."""
    for day in range(10):
        text = SIGNAL_TEXT.replace("4470", str(4460 - day))
        assert _ingest(conn, RELAY, 7000 + day, text, posted=_at(day * 86400)).signal_id == day + 1
        copy = _ingest(conn, VIP, 4000 + day, text, posted=_at(day * 86400 + 30))
        assert copy.relay_of_signal_id == day + 1, copy


def test_old_copies_of_a_channel_switched_off_are_not_traded_and_said_once(conn):  # noqa: F811
    """p_r31_old: the relay is switched off after ten days, and watch
    restarts an hour after the last call. Every copy on the VIP's channel
    was posted far longer ago than the age gate allows: none is a signal,
    nothing is queued, and ONE line says why -- not ten WARNINGs saying
    "2 trade entries are queued for it"."""
    _ten_days_of_the_relay_and_the_vip(conn)

    skipped, raised = _restart(conn, seconds=9 * 86400 + 3600)

    assert skipped == {R: 10 * len(ACCOUNTS)}
    assert [row[0] for row in _signals(conn)] == list(range(1, 11))
    assert _queued(conn) == []
    assert [_link(conn, VIP, 4000 + day) for day in range(10)] == list(range(1, 11))
    (alert,) = _alerts(conn)
    assert alert[:5] == ("WARNING", OLD_CODE, None, None, None)
    assert len(raised) == 1
    detail = " ".join(alert[5].split())
    assert detail == (
        "10 posts on channels that are switched on repeat the calls of signals whose channel "
        "is now switched off (signals 1, 2, 3, 4, 5, 6, 7, 8, 9, 10). Those signals' waiting "
        "trade entries were cancelled, and these posts are not traded instead: each was "
        "posted more than 120 seconds ago, too old to be traded. Nothing to do."
    ), detail
    for jargon in ("client", "listener", "rpc", "flood", "outbox", "queued for it"):
        assert jargon not in detail.lower(), (jargon, detail)


def test_a_young_copy_is_traded_while_the_old_ones_are_said_in_one_line(conn):  # noqa: F811
    """Ten old calls, and an eleventh the VIP copied 30 s before the
    restart: that one is a signal, queued, with its own WARNING; the ten
    old ones get the one line."""
    _ten_days_of_the_relay_and_the_vip(conn)
    late = 9 * 86400 + 3600
    assert _ingest(conn, RELAY, 7100, posted=_at(late - 60)).signal_id == 11
    assert _ingest(conn, VIP, 4100, posted=_at(late - 30)).relay_of_signal_id == 11

    _restart(conn, seconds=late)

    assert _signals(conn)[-1] == (12, V, 4100, "NEW")
    assert _queued(conn) == [(12, "acct-1"), (12, "acct-2")]
    codes = [a[:2] for a in _alerts(conn)]
    assert sorted(codes) == [("WARNING", CODE), ("WARNING", OLD_CODE)]
    (old,) = [a[5] for a in _alerts(conn) if a[1] == OLD_CODE]
    assert old.startswith("10 posts on channels"), old


@pytest.mark.parametrize(
    ("age", "admitted"), [(120, True), (121, False)], ids=["exactly the gate", "a second older"]
)
def test_the_age_gate_is_the_one_channels_off_counts_by(conn, age, admitted):  # noqa: F811
    """A copy exactly `max_signal_age_seconds` old is still young enough --
    `channels off` counts it as waiting (`report.signals_in_play`); one a
    second older is too old."""
    _relay_call_then_vip_copy(conn)

    _restart(conn, seconds=30 + age)

    if admitted:
        assert _signals(conn)[1:] == [(2, V, VIP_COPY, "NEW")]
        assert [a[1] for a in _alerts(conn)] == [CODE]
    else:
        assert _signals(conn)[1:] == []
        assert [a[1] for a in _alerts(conn)] == [OLD_CODE]
        assert _alerts(conn)[0][5].startswith(
            "1 post on a channel that is switched on repeats the call of a signal whose "
            "channel is now switched off (signal 1). That signal's waiting trade entries were "
            "cancelled, and this post is not traded instead: it was posted more than 120 "
            "seconds ago, too old to be traded."
        ), _alerts(conn)[0][5]


def test_the_age_gate_is_the_policys(conn):  # noqa: F811
    """A longer `max_signal_age_seconds` (policy.toml) lets an older copy be
    traded: the gate given is the one used."""
    _relay_call_then_vip_copy(conn)

    _restart(conn, seconds=1000, max_age_seconds=1200)

    assert _signals(conn)[1:] == [(2, V, VIP_COPY, "NEW")]
    assert [a[1] for a in _alerts(conn)] == [CODE]


def test_an_old_copy_of_a_call_traded_from_a_young_one_is_not_said_to_be_too_old(
    conn,  # noqa: F811
):
    """The re-check's r42_order (final fix round 5, R5-6). The VIP copied the
    relay's call by hand 30 s after it, and forwarded the relay's post
    2000 s later -- a forward is the same post however late. Newest first,
    the young forward is traded (signal 2). The old hand copy is outside
    signal 2's window, yet repeats signal 1's call, which signal 2 carries
    now (R7-1): it repeats signal 2 -- the call IS traded, so no line may
    say that this post is not traded because it is too old."""
    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    assert _ingest(conn, VIP, 4002, posted=_at(30)).relay_of_signal_id == 1
    forward = _ingest(conn, VIP, VIP_COPY, posted=_at(2000), fwd=(R, RELAY_CALL))
    assert forward.relay_of_signal_id == 1, forward

    _restart(conn, on=(VIP,), seconds=2050)

    assert _signals(conn)[1:] == [(2, V, VIP_COPY, "NEW")]
    assert _link(conn, VIP, 4002) == 2
    assert [a[1] for a in _alerts(conn)] == [CODE]


def test_an_old_copy_the_relay_rules_would_hold_back_raises_no_trade_it_by_hand(
    conn,  # noqa: F811
):
    """The held-back case, restarted long after: the VIP's copy is too old
    to be traded, so it is never run through the relay rules again -- no
    CRITICAL telling the user to trade a call hours old by hand."""
    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    assert _ingest(conn, VIP, VIP_COPY, posted=_at(890)).relay_of_signal_id == 1
    assert _ingest(conn, OTHER, OTHER_COPY, posted=_at(950)).signal_id == 2
    edits_repo.apply_deletion(
        conn, channel_cfg=OTHER, message_id=OTHER_COPY, now_utc=_at(960), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()

    _restart(conn, seconds=4 * 3600)

    assert _link(conn, VIP, VIP_COPY) == 1
    assert relay_repo.COPY_OF_WITHDRAWN_SIGNAL not in [a[1] for a in _alerts(conn)]
    assert OLD_CODE in [a[1] for a in _alerts(conn)]


@pytest.mark.parametrize("gate", [None, 1200], ids=["the default gate", "policy.toml's gate"])
def test_watch_admits_only_a_copy_young_enough_for_its_age_gate(
    tmp_path: Path, gate: int | None
) -> None:
    """Through `Listener.start()`: test_listener's clock stands 870 s after
    the VIP's copy. With the default gate (120 s) it is too old -- one line,
    nothing queued; with `max_signal_age_seconds = 1200` given to watch, it
    is traded."""
    from test_listener import ACCOUNTS as LISTENER_ACCOUNTS
    from test_listener import _build_listener

    db_path = tmp_path / "store.db"
    relay_off = ChannelCfg(
        channel_id=R, name=RELAY.name, parser="mmh", ordinal=RELAY.ordinal, enabled=False
    )
    writer = db.connect(db_path, role="writer")
    try:
        for channel, message_id, seconds in ((RELAY, RELAY_CALL, 0), (VIP, VIP_COPY, 30)):
            messages_repo.ingest(
                writer, channel_cfg=channel, message_id=message_id, text=SIGNAL_TEXT,
                posted_at_utc=_at(seconds), edit_date_utc=None, reply_to_message_id=None,
                source="live", now_utc=_at(seconds + 1), accounts=LISTENER_ACCOUNTS,
            )
        writer.commit()
    finally:
        writer.close()

    async def scenario():
        extra = {} if gate is None else {"max_signal_age_seconds": gate}
        listener = _build_listener(
            db_path, FakeTelegramClient(), channels=(VIP, relay_off),
            accounts=LISTENER_ACCOUNTS, **extra,
        )
        await listener.start()
        await listener.aclose()

    asyncio.run(scenario())

    check = db.connect(db_path, role="reader")
    try:
        signals = _signals(check)
        queued = _queued(check)
        codes = [row[1] for row in _alerts(check)]
    finally:
        check.close()
    assert codes[0] == "CHANNEL_OFF_ENTRIES_SKIPPED"
    if gate is None:
        assert signals == [(1, R, RELAY_CALL, "NEW")]
        assert queued == []
        assert codes[1:] == [OLD_CODE]
    else:
        assert signals == [(1, R, RELAY_CALL, "NEW"), (2, V, VIP_COPY, "NEW")]
        assert queued == [(2, "acct-a"), (2, "acct-b")]
        assert codes[1:] == [CODE]


# -- a copy held back while an older one is traded (final fix round 4, R4-3) --

HELD = relay_repo.COPY_OF_WITHDRAWN_SIGNAL
SUPERSEDED = relay_repo.COPY_OF_WITHDRAWN_SIGNAL_SUPERSEDED
#: policy.toml's gate in these tests: long enough that the VIP's older copy,
#: 1070 s before the restart, may still be traded (R4-2).
LONG_GATE = 1200


def _two_vip_copies_and_a_withdrawn_repost(conn, *, reposted_by) -> None:  # noqa: F811
    """The re-check's p_r31_heldback2 (`reposted_by` RELAY) and
    p_r31_heldback (OTHER): the relay's call (signal 1); the VIP copies it
    at +30 s and again at +850 s; at +1000 s -- 1000 s after signal 1, so a
    signal of its own -- the same call is posted (signal 2) and deleted at
    +1010 s (VOIDED). The VIP's +850 s copy is within the window of both."""
    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    assert _ingest(conn, VIP, VIP_COPY, posted=_at(30)).relay_of_signal_id == 1
    assert _ingest(conn, VIP, VIP_REPOST, posted=_at(850)).relay_of_signal_id == 1
    repost = 7002 if reposted_by is RELAY else OTHER_COPY
    assert _ingest(conn, reposted_by, repost, posted=_at(1000)).signal_id == 2
    edits_repo.apply_deletion(
        conn, channel_cfg=reposted_by, message_id=repost, now_utc=_at(1010), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()


@pytest.mark.parametrize(
    "reposted_by", [RELAY, OTHER], ids=["p_r31_heldback2: the relay", "p_r31_heldback: another"]
)
def test_a_copy_held_back_then_repeats_the_older_copy_traded_instead(
    conn, reposted_by  # noqa: F811
):
    """Newest first, the VIP's +850 s copy is tried first: it is within the
    window of the withdrawn signal 2, so it is held back (CRITICAL, "trade
    it by hand"). Its +30 s copy is then traded, as signal 3. Run through
    the relay rules again, the held-back copy now repeats signal 3: it is
    linked to it, the CRITICAL is cleared, and a WARNING says not to trade
    the call by hand -- as when the call is posted again
    (`record_replacement`). No "trade it by hand" stands while it is queued:
    following it would open the trade twice."""
    _two_vip_copies_and_a_withdrawn_repost(conn, reposted_by=reposted_by)
    on = (VIP,) if reposted_by is RELAY else (VIP, OTHER)

    _, raised = _restart(conn, on=on, seconds=1100, max_age_seconds=LONG_GATE)

    assert _signals(conn)[2:] == [(3, V, VIP_COPY, "NEW")]
    assert _link(conn, VIP, VIP_REPOST) == 3
    assert _queued(conn) == [(3, "acct-1"), (3, "acct-2")]
    rows = _q(
        conn,
        "SELECT alert_id, level, code, message_id, signal_id, acked, detail FROM alerts "
        "WHERE code IN (?, ?, ?) ORDER BY alert_id",
        HELD, CODE, SUPERSEDED,
    )
    assert [row[1:6] for row in rows] == [
        ("CRITICAL", HELD, VIP_REPOST, 2, 1),  # cleared
        ("WARNING", CODE, VIP_COPY, 3, 0),
        ("WARNING", SUPERSEDED, VIP_REPOST, 3, 0),
    ]
    assert set(raised) == {row[0] for row in rows}
    held_id, detail = rows[0][0], " ".join(rows[2][6].split())
    assert detail == (
        f"alert #{held_id} no longer applies. It said that message {VIP_REPOST} on channel {V} "
        "copies a call that had been withdrawn, was not traded, and that the call should be "
        "traded by hand if it was still meant. The same call stands on a channel that is "
        f"switched on: message {VIP_COPY} on channel {V} is signal 3, with 2 trade entries "
        f"queued, and the copy now repeats it. Alert #{held_id} is cleared. Do not trade this "
        "call by hand as well: that would open the trade twice. If you already did, check "
        "your open positions."
    ), detail
    unacked_by_hand = _q(
        conn, "SELECT detail FROM alerts WHERE acked = 0 AND detail LIKE '%trade it by hand.%'"
    )
    assert unacked_by_hand == []


def test_a_held_back_copy_of_the_switched_off_call_repeats_the_call_traded_in_its_place(
    conn,  # noqa: F811
):
    """The VIP's copies are 950 s apart: the +900 s one, held back against
    the withdrawn signal 2, is outside the window of signal 3 (the -50 s
    copy). But it repeats signal 1's call -- it is within signal 1's window,
    and a live call wins over a withdrawn one -- and signal 3 carries that
    call now (final fix round 7, R7-1): it repeats signal 3, and no "trade
    it by hand" stands while the call is queued. (It stayed held back
    before, its CRITICAL standing.)"""
    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    assert _ingest(conn, VIP, VIP_COPY, posted=_at(-50)).relay_of_signal_id == 1
    assert _ingest(conn, VIP, VIP_REPOST, posted=_at(900)).relay_of_signal_id == 1
    assert _ingest(conn, RELAY, 7002, posted=_at(1000)).signal_id == 2
    edits_repo.apply_deletion(
        conn, channel_cfg=RELAY, message_id=7002, now_utc=_at(1010), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()

    _restart(conn, on=(VIP,), seconds=1100, max_age_seconds=LONG_GATE)

    assert _signals(conn)[2:] == [(3, V, VIP_COPY, "NEW")]
    assert _link(conn, VIP, VIP_REPOST) == 3
    assert _q(
        conn, "SELECT code, acked, signal_id FROM alerts WHERE code IN (?, ?) ORDER BY alert_id",
        HELD, SUPERSEDED,
    ) == [(HELD, 1, 2), (SUPERSEDED, 0, 3)]


def test_a_reply_to_a_held_back_copy_moves_with_it_to_the_traded_call(conn):  # noqa: F811
    """The VIP's "close half", replying to its +850 s copy, acts on the call
    that copy repeats once it is linked to signal 3."""
    _two_vip_copies_and_a_withdrawn_repost(conn, reposted_by=RELAY)
    _ingest(conn, VIP, 4020, CLOSE_HALF, posted=_at(1020), reply_to=VIP_REPOST)

    _restart(conn, on=(VIP,), seconds=1100, max_age_seconds=LONG_GATE)

    (followup_id,) = _q(conn, "SELECT followup_id FROM followups WHERE message_id = 4020")[0]
    assert relay_repo.signal_for_followup(conn, followup_id=followup_id) == 3


def test_a_copy_linked_to_the_call_traded_at_start_gets_no_second_warning_later(
    conn,  # noqa: F811
):
    """The re-check's r43_rerep (final fix round 5, R5-3). After the start of
    p_r31_heldback, the VIP's +850 s copy repeats signal 3 and its CRITICAL is
    cleared. At +1800 s the other channel posts the call again: outside
    signal 3's window, inside withdrawn signal 2's, so it is signal 4 in
    signal 2's place. The copy is NOT linked to signal 4 -- it still repeats
    signal 3 -- so no second WARNING may say that it does."""
    _two_vip_copies_and_a_withdrawn_repost(conn, reposted_by=OTHER)
    _restart(conn, on=(VIP, OTHER), seconds=1100, max_age_seconds=LONG_GATE)
    before = _q(conn, "SELECT alert_id FROM alerts WHERE code = ?", SUPERSEDED)
    assert len(before) == 1

    again = _ingest(conn, OTHER, 9002, posted=_at(1800))

    assert again.signal_id == 4
    assert _q(conn, "SELECT promoted_from_signal_id FROM signals WHERE signal_id = 4") == [(2,)]
    assert _link(conn, VIP, VIP_REPOST) == 3
    assert _q(conn, "SELECT alert_id FROM alerts WHERE code = ?", SUPERSEDED) == before
    assert _q(conn, "SELECT COUNT(*) FROM alerts WHERE signal_id = 4") == [(0,)]


def _held_back_then_the_call_posted_afresh(conn) -> None:  # noqa: F811
    """The re-check's r43_variants V2: the relay posts the call and deletes
    it (signal 1, VOIDED); 800 s later the VIP posts the call, held back as
    a copy of the withdrawn call (CRITICAL, "trade it by hand"); at +1000 s
    the relay posts the call again -- 1000 s after signal 1, so a fresh
    signal, not signal 1 posted again. The VIP's post is 200 s from it."""
    assert _ingest(conn, RELAY, 7002).signal_id == 1
    edits_repo.apply_deletion(
        conn, channel_cfg=RELAY, message_id=7002, now_utc=_at(10), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()
    assert _ingest(conn, VIP, 4002, posted=_at(800)).relay_of_signal_id == 1
    assert _q(conn, "SELECT acked FROM alerts WHERE code = ?", HELD) == [(0,)]
    assert _ingest(conn, RELAY, RELAY_CALL, posted=_at(1000)).signal_id == 2


def _unacked_by_hand(conn) -> list[tuple]:  # noqa: F811
    return _q(
        conn, "SELECT alert_id FROM alerts WHERE acked = 0 AND detail LIKE '%trade it by hand.%'"
    )


def test_a_held_back_copy_repeats_the_same_call_posted_afresh(conn, caplog):  # noqa: F811
    """Final fix round 5, R5-4, at ingest: by the relay rules the VIP's post
    repeats signal 2 now -- the live call within its window -- so it is
    linked to it, its CRITICAL is cleared, and a WARNING says not to trade
    the call by hand: following the CRITICAL would open signal 2's trade
    twice."""
    with caplog.at_level(logging.INFO, logger="xaucopier.store.relay_repo"):
        _held_back_then_the_call_posted_afresh(conn)

    assert _link(conn, VIP, 4002) == 2
    assert (
        f"channel {V} message 4002, held back as a copy of withdrawn signal 1, now repeats "
        f"signal 2 (channel {R} message {RELAY_CALL})"
    ) in [record.getMessage() for record in caplog.records]
    assert _queued(conn) == [(2, "acct-1"), (2, "acct-2")]
    assert _unacked_by_hand(conn) == []
    rows = _q(
        conn,
        "SELECT alert_id, level, code, message_id, signal_id, acked, detail FROM alerts "
        "WHERE code IN (?, ?) ORDER BY alert_id",
        HELD, SUPERSEDED,
    )
    assert [row[1:6] for row in rows] == [
        ("CRITICAL", HELD, 4002, 1, 1),
        ("WARNING", SUPERSEDED, 4002, 2, 0),
    ]
    held_id, detail = rows[0][0], " ".join(rows[1][6].split())
    assert detail == (
        f"alert #{held_id} no longer applies. It said that message 4002 on channel {V} copies "
        "a call that had been withdrawn, was not traded, and that the call should be traded by "
        f"hand if it was still meant. The same call stands as message "
        f"{RELAY_CALL} on channel {R}: it is signal 2, with 2 trade entries queued, and the "
        f"copy now repeats it. Alert #{held_id} is cleared. Do not trade this call by hand as "
        "well: that would open the trade twice. If you already did, check your open positions."
    ), detail


def test_a_held_back_copy_repeats_a_call_posted_again_by_another_channel(conn):  # noqa: F811
    """The same when the new signal replaces a withdrawn call: the relay's
    call at 0 s (signal 1) and the other channel's at +1800 s (signal 2) are
    both withdrawn; the VIP's post at +1700 s is held back against signal 2.
    The relay's call posted again at +850 s -- delivered late, after the
    VIP's -- is signal 3 in signal 1's place, and 850 s from the VIP's post:
    by the relay rules the VIP's post repeats it now. Clearing the copies
    held back against signal 1 only (`record_replacement`) left this one's
    "trade it by hand" standing."""
    for channel, message_id, posted in ((RELAY, 7002, 0), (OTHER, OTHER_COPY, 1800)):
        _ingest(conn, channel, message_id, posted=_at(posted))
        edits_repo.apply_deletion(
            conn, channel_cfg=channel, message_id=message_id, now_utc=_at(posted + 10),
            accounts=ACCOUNTS, detected_by="event",
        )
        conn.commit()
    assert _ingest(conn, VIP, 4002, posted=_at(1700)).relay_of_signal_id == 2

    again = _ingest(conn, RELAY, RELAY_CALL, posted=_at(850))

    assert again.signal_id == 3
    assert _q(conn, "SELECT promoted_from_signal_id FROM signals WHERE signal_id = 3") == [(1,)]
    assert _link(conn, VIP, 4002) == 3
    assert _unacked_by_hand(conn) == []
    assert _q(conn, "SELECT message_id, signal_id FROM alerts WHERE code = ?", SUPERSEDED) == [
        (4002, 3)
    ]

def test_a_held_back_copy_outside_the_new_signals_window_stays_held_back(conn):  # noqa: F811
    """The control: the VIP's post 1000 s before the fresh call is another
    call by the relay rules -- it stays held back, its alert standing."""
    assert _ingest(conn, RELAY, 7002).signal_id == 1
    edits_repo.apply_deletion(
        conn, channel_cfg=RELAY, message_id=7002, now_utc=_at(10), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()
    assert _ingest(conn, VIP, 4002, posted=_at(800)).relay_of_signal_id == 1
    assert _ingest(conn, RELAY, RELAY_CALL, posted=_at(1801)).signal_id == 2

    assert _link(conn, VIP, 4002) == 1
    assert _q(conn, "SELECT acked FROM alerts WHERE code = ?", HELD) == [(0,)]
    assert _q(conn, "SELECT COUNT(*) FROM alerts WHERE code = ?", SUPERSEDED) == [(0,)]


def test_a_held_back_copy_edited_away_since_is_left_as_it_was(conn):  # noqa: F811
    """The VIP edited its held-back post into chatter before the relay's
    fresh call: it shows no call, so it repeats none -- left as it was."""
    assert _ingest(conn, RELAY, 7002).signal_id == 1
    edits_repo.apply_deletion(
        conn, channel_cfg=RELAY, message_id=7002, now_utc=_at(10), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()
    assert _ingest(conn, VIP, 4002, posted=_at(800)).relay_of_signal_id == 1
    edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=4002, text="good morning traders",
        posted_at_utc=_at(800), edit_date_utc=_at(900), reply_to_message_id=None,
        now_utc=_at(900), accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()

    assert _ingest(conn, RELAY, RELAY_CALL, posted=_at(1000)).signal_id == 2

    assert _link(conn, VIP, 4002) == 1
    assert _q(conn, "SELECT COUNT(*) FROM alerts WHERE code = ?", SUPERSEDED) == [(0,)]

def test_a_held_back_copy_repeats_the_same_call_after_the_restart_too(conn):  # noqa: F811
    """V2 at the start: the VIP copies the relay's fresh call at +1030 s, and
    watch restarts with the relay switched off. The VIP's copy is traded
    (signal 3), and the post held back first repeats it: no "trade it by
    hand" stands while it is queued."""
    _held_back_then_the_call_posted_afresh(conn)
    assert _ingest(conn, VIP, VIP_COPY, posted=_at(1030)).relay_of_signal_id == 2

    _restart(conn, on=(VIP,), seconds=1100)

    assert _signals(conn)[2:] == [(3, V, VIP_COPY, "NEW")]
    assert _link(conn, VIP, 4002) == 3
    assert _queued(conn) == [(3, "acct-1"), (3, "acct-2")]
    assert _unacked_by_hand(conn) == []


def test_a_copy_held_back_when_read_repeats_the_call_traded_at_start(conn):  # noqa: F811
    """The start's own case: the VIP's post at +700 s was held back when read
    (signal 1, withdrawn, is within its window; the relay's fresh call at
    +1700 s, signal 2, is not). The VIP copied that call at +1000 s, and the
    relay is switched off: the copy is traded (signal 3), 300 s from the
    held-back post, which repeats it now -- its CRITICAL is cleared at the
    start that queues signal 3, with the start's words."""
    assert _ingest(conn, RELAY, 7002).signal_id == 1
    edits_repo.apply_deletion(
        conn, channel_cfg=RELAY, message_id=7002, now_utc=_at(10), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()
    assert _ingest(conn, VIP, 4002, posted=_at(700)).relay_of_signal_id == 1
    assert _ingest(conn, RELAY, RELAY_CALL, posted=_at(1700)).signal_id == 2
    assert _ingest(conn, VIP, VIP_COPY, posted=_at(1000)).relay_of_signal_id == 2
    assert _link(conn, VIP, 4002) == 1 and len(_unacked_by_hand(conn)) == 1

    _, raised = _restart(conn, on=(VIP,), seconds=1750, max_age_seconds=LONG_GATE)

    assert _signals(conn)[2:] == [(3, V, VIP_COPY, "NEW")]
    assert _link(conn, VIP, 4002) == 3
    assert _unacked_by_hand(conn) == []
    (superseded,) = _q(
        conn, "SELECT alert_id, signal_id, detail FROM alerts WHERE code = ?", SUPERSEDED
    )
    assert superseded[0] in raised and superseded[1] == 3
    assert (
        "The same call stands on a channel that is switched on: message "
        f"{VIP_COPY} on channel {V} is signal 3, with 2 trade entries queued"
    ) in " ".join(superseded[2].split())


# -- a follow-up copy of the switched-off channel's (final fix round 4, R4-4) -

VIP_CLOSE = 4005
RELAY_CLOSE = 7005


def _followups(conn) -> list[tuple]:  # noqa: F811
    return _q(
        conn,
        "SELECT followup_id, channel_id, message_id, target_signal_id, resolution_path "
        "FROM followups ORDER BY followup_id",
    )


def _followup_rows(conn, followup_id: int) -> list[tuple]:  # noqa: F811
    return _q(
        conn,
        "SELECT account_id, state FROM outbox WHERE kind = 'FOLLOWUP' AND followup_id = ? "
        "ORDER BY outbox_id",
        followup_id,
    )


def _acting_on(conn, signal_id: int) -> list[tuple]:  # noqa: F811
    """The follow-ups that act on `signal_id` (`signal_for_followup`), as
    (channel, message)."""
    return [
        (row[1], row[2])
        for row in _followups(conn)
        if relay_repo.signal_for_followup(conn, followup_id=row[0]) == signal_id
    ]


@pytest.mark.parametrize("order", ["the relay first", "the VIP first", "a restart between"])
def test_the_vips_close_half_acts_on_its_call_in_every_order(conn, order):  # noqa: F811
    """The re-check's p_r31_followups. The relay posts the call, the VIP
    copies it 30 s later; each then says "close half", replying to its own
    post, 10 s apart. The relay is switched off and watch restarts: the
    VIP's post is signal 2. Whichever said it first -- or with a restart
    between the calls and the instructions, when the relay's is never read
    -- the VIP's "close half" is an instruction of its own about signal 2,
    with its entries for both accounts: never lost as a copy of the relay's
    instruction, which acts on signal 1, cancelled."""
    _relay_call_then_vip_copy(conn)
    if order == "a restart between":
        _restart(conn, on=(VIP,), seconds=50)
        _ingest(conn, VIP, VIP_CLOSE, CLOSE_HALF, posted=_at(60), reply_to=VIP_COPY)
    else:
        first, second = (
            [(RELAY, RELAY_CLOSE, RELAY_CALL), (VIP, VIP_CLOSE, VIP_COPY)]
            if order == "the relay first"
            else [(VIP, VIP_CLOSE, VIP_COPY), (RELAY, RELAY_CLOSE, RELAY_CALL)]
        )
        assert _ingest(conn, *first[:2], CLOSE_HALF, posted=_at(60), reply_to=first[2]).followup_id
        copy = _ingest(conn, *second[:2], CLOSE_HALF, posted=_at(70), reply_to=second[2])
        assert copy.outcome == "relay_duplicate", copy
        _restart(conn, on=(VIP,), seconds=100)

    assert _signals(conn)[1:] == [(2, V, VIP_COPY, "NEW")]
    assert _acting_on(conn, 2) == [(V, VIP_CLOSE)]
    (vip,) = [row for row in _followups(conn) if row[1:3] == (V, VIP_CLOSE)]
    assert vip[3:] == (2, relay_repo.REPLY_PATH)
    assert _followup_rows(conn, vip[0]) == [("acct-1", "QUEUED"), ("acct-2", "QUEUED")]
    assert _q(conn, "SELECT relay_of_followup_id FROM messages WHERE message_id = ?", VIP_CLOSE) == [
        (None,)
    ]


def test_a_follow_up_copy_is_stored_with_the_message_it_replies_to(conn):  # noqa: F811
    """What R4-4 reads: the post a copy of a follow-up replied to."""
    _relay_call_then_vip_copy(conn)
    _ingest(conn, RELAY, RELAY_CLOSE, CLOSE_HALF, posted=_at(60), reply_to=RELAY_CALL)
    _ingest(conn, VIP, VIP_CLOSE, CLOSE_HALF, posted=_at(70), reply_to=VIP_COPY)

    assert _q(
        conn,
        "SELECT message_id, reply_to_message_id FROM messages WHERE message_id IN (?, ?, ?) "
        "ORDER BY message_id",
        VIP_COPY, VIP_CLOSE, RELAY_CLOSE,
    ) == [(VIP_COPY, None), (VIP_CLOSE, VIP_COPY), (RELAY_CLOSE, RELAY_CALL)]


def test_two_channels_repeating_the_relays_close_half_give_one_instruction(
    conn,  # noqa: F811
):
    """The VIP and another channel both copied the relay's call and its
    "close half". The other channel's copy is the newest: signal 2; the
    VIP's repeats it. Each one's "close half" was a copy of the relay's.
    Now one of them is the instruction about signal 2 and the other repeats
    it -- never two, which would close half twice."""
    _relay_call_then_vip_copy(conn)
    assert _ingest(conn, OTHER, OTHER_COPY, posted=_at(40)).relay_of_signal_id == 1
    _ingest(conn, RELAY, RELAY_CLOSE, CLOSE_HALF, posted=_at(60), reply_to=RELAY_CALL)
    _ingest(conn, VIP, VIP_CLOSE, CLOSE_HALF, posted=_at(70), reply_to=VIP_COPY)
    _ingest(conn, OTHER, 9005, CLOSE_HALF, posted=_at(75), reply_to=OTHER_COPY)

    _restart(conn, seconds=100)

    assert _signals(conn)[1:] == [(2, OTHER.channel_id, OTHER_COPY, "NEW")]
    acting = _acting_on(conn, 2)
    assert len(acting) == 1, acting
    (own,) = [row for row in _followups(conn) if (row[1], row[2]) == acting[0]]
    links = _q(
        conn,
        "SELECT channel_id, message_id, relay_of_followup_id FROM messages "
        "WHERE message_id IN (?, ?) ORDER BY message_id",
        VIP_CLOSE, 9005,
    )
    assert {link[2] for link in links} == {None, own[0]}, links
    assert acting[0] in {(V, VIP_CLOSE), (OTHER.channel_id, 9005)}
    # The VIP's replies to its copy, which repeats signal 2: 'relay-reply';
    # the other channel's to signal 2's own message: 'reply'.
    assert own[4] == (
        relay_repo.RELAY_REPLY_PATH if acting[0] == (V, VIP_CLOSE) else relay_repo.REPLY_PATH
    )


def test_a_deleted_or_edited_away_follow_up_copy_gives_no_instruction(conn):  # noqa: F811
    """The VIP deleted its "close half", or edited it into chatter, before
    the restart: nothing of it is an instruction about signal 2."""
    _relay_call_then_vip_copy(conn)
    _ingest(conn, RELAY, RELAY_CLOSE, CLOSE_HALF, posted=_at(60), reply_to=RELAY_CALL)
    _ingest(conn, VIP, VIP_CLOSE, CLOSE_HALF, posted=_at(70), reply_to=VIP_COPY)
    _ingest(conn, VIP, VIP_CLOSE + 1, CLOSE_HALF, posted=_at(72), reply_to=VIP_COPY)
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=VIP_CLOSE, now_utc=_at(80), accounts=ACCOUNTS,
        detected_by="event",
    )
    edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=VIP_CLOSE + 1, text="good morning traders",
        posted_at_utc=_at(72), edit_date_utc=_at(82), reply_to_message_id=VIP_COPY,
        now_utc=_at(82), accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()

    _restart(conn, on=(VIP,), seconds=100)

    assert _signals(conn)[1:] == [(2, V, VIP_COPY, "NEW")]
    assert _acting_on(conn, 2) == []


def test_a_follow_up_copy_replying_to_an_old_copy_acts_on_the_call_traded_in_its_place(
    conn,  # noqa: F811
):
    """The VIP copied the relay's call 800 s before it was posted, the other
    channel 800 s after: the other channel's post is signal 2. The VIP's,
    1600 s away from it, still repeats signal 1's call, which signal 2
    carries now (final fix round 7, R7-1): it repeats signal 2. So the
    VIP's "close half" replying to it, stored as a copy of the relay's, is
    the instruction about signal 2 now -- one, as with both channels read.
    (It was left a copy of the relay's before: nothing closed half.)"""
    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    assert _ingest(conn, VIP, VIP_COPY, posted=_at(-800)).relay_of_signal_id == 1
    assert _ingest(conn, OTHER, OTHER_COPY, posted=_at(800)).relay_of_signal_id == 1
    _ingest(conn, RELAY, RELAY_CLOSE, CLOSE_HALF, posted=_at(810), reply_to=RELAY_CALL)
    copy = _ingest(conn, VIP, VIP_CLOSE, CLOSE_HALF, posted=_at(820), reply_to=VIP_COPY)
    assert copy.relay_of_followup_id == 1, copy

    _restart(conn, seconds=900)

    assert _signals(conn)[1:] == [(2, OTHER.channel_id, OTHER_COPY, "NEW")]
    assert _link(conn, VIP, VIP_COPY) == 2
    assert _acting_on(conn, 2) == [(V, VIP_CLOSE)]
    assert _acting_on(conn, 1) == [(R, RELAY_CLOSE)]
    assert _q(conn, "SELECT relay_of_followup_id FROM messages WHERE message_id = ?", VIP_CLOSE) == [
        (None,)
    ]


def test_a_copy_of_a_close_half_that_moved_to_the_new_signal_stays_a_copy(
    conn,  # noqa: F811
):
    """The other channel said "close half" first, replying to its copy of
    the call; the VIP's, 10 s later, is stored as a copy of it. The other
    channel's post becomes signal 2, and its "close half" moves with it.
    The VIP's copy repeats an instruction about signal 2 already: it stays
    a copy -- two would close half twice."""
    _relay_call_then_vip_copy(conn)
    assert _ingest(conn, OTHER, OTHER_COPY, posted=_at(40)).relay_of_signal_id == 1
    other = _ingest(conn, OTHER, 9005, CLOSE_HALF, posted=_at(60), reply_to=OTHER_COPY)
    copy = _ingest(conn, VIP, VIP_CLOSE, CLOSE_HALF, posted=_at(70), reply_to=VIP_COPY)
    assert copy.relay_of_followup_id == other.followup_id, copy

    _restart(conn, seconds=100)

    assert _signals(conn)[1:] == [(2, OTHER.channel_id, OTHER_COPY, "NEW")]
    assert _acting_on(conn, 2) == [(OTHER.channel_id, 9005)]
    assert _q(conn, "SELECT relay_of_followup_id FROM messages WHERE message_id = ?", VIP_CLOSE) == [
        (other.followup_id,)
    ]


# -- a forward of the switched-off channel's follow-up (final fix round 5) --


def _the_relays_close_half(conn, *, seconds: float = 60) -> None:  # noqa: F811
    _ingest(conn, RELAY, RELAY_CLOSE, CLOSE_HALF, posted=_at(seconds), reply_to=RELAY_CALL)


def _the_vips_forward_of_it(conn, *, seconds: float = 70, message_id: int = VIP_CLOSE):  # noqa: F811
    return _ingest(
        conn, VIP, message_id, CLOSE_HALF, posted=_at(seconds), fwd=(R, RELAY_CLOSE)
    )


@pytest.mark.parametrize("order", ["the relay first", "the VIP first"])
def test_the_vips_forward_of_the_relays_close_half_acts_on_its_call_in_every_order(
    conn, order  # noqa: F811
):
    """The re-check's r44_variants F1 (final fix round 5, R5-2). The relay
    posts the call, the VIP copies it 30 s later; the relay says "close
    half" replying to its call, and the VIP FORWARDS that post -- a forward
    has no reply, so it was stored as a copy of the relay's instruction,
    which acts on signal 1, cancelled when the relay is switched off. The
    VIP's post is signal 2 after the restart, and the VIP's "close half" is
    an instruction about it, with its entries for both accounts: whichever
    was stored first. (Read only after the restart, the forward is NOT
    applied -- final fix round 6, R6-1:
    `test_a_forward_read_after_the_restart_is_not_applied_and_said_so`.)"""
    _relay_call_then_vip_copy(conn)
    if order == "the relay first":
        _the_relays_close_half(conn)
        assert _the_vips_forward_of_it(conn).relay_of_followup_id == 1
    else:
        assert _the_vips_forward_of_it(conn).followup_id == 1
        _the_relays_close_half(conn)
    _restart(conn, on=(VIP,), seconds=100)

    assert _signals(conn)[1:] == [(2, V, VIP_COPY, "NEW")]
    assert _acting_on(conn, 2) == [(V, VIP_CLOSE)]
    (vip,) = [row for row in _followups(conn) if row[1:3] == (V, VIP_CLOSE)]
    assert vip[3:] == (
        2,
        relay_repo.COPY_REPLY_PATH if order == "the VIP first" else relay_repo.RELAY_REPLY_PATH,
    )
    assert _followup_rows(conn, vip[0]) == [("acct-1", "QUEUED"), ("acct-2", "QUEUED")]
    assert _q(conn, "SELECT relay_of_followup_id FROM messages WHERE message_id = ?", VIP_CLOSE) == [
        (None,)
    ]


@pytest.mark.parametrize("restart_between", [False, True], ids=["one start", "a restart between"])
@pytest.mark.parametrize("other_replies", [False, True], ids=["both forward", "one replies"])
def test_two_posts_repeating_the_relays_close_half_give_one_instruction(
    conn, other_replies, restart_between  # noqa: F811
):
    """The VIP forwards the relay's "close half", and the other channel --
    which copied the relay's call too -- forwards it as well, or replies
    "close half" to its own copy. Each repeats the one instruction: one of
    them is the instruction about the signal traded instead, the other
    repeats it -- never two, which would close half twice. Read only after
    the restart (final fix round 6, R6-1), the VIP's forward is NOT applied
    -- nothing ties it to the call traded instead -- and a WARNING says so;
    the other channel's post repeats the instruction handed over by hand,
    so it is not applied either, and its WARNING says to apply it once."""
    _relay_call_then_vip_copy(conn)
    assert _ingest(conn, OTHER, OTHER_COPY, posted=_at(20)).relay_of_signal_id == 1
    _the_relays_close_half(conn)

    def the_others() -> None:
        if other_replies:
            _ingest(conn, OTHER, 9005, CLOSE_HALF, posted=_at(75), reply_to=OTHER_COPY)
        else:
            _ingest(conn, OTHER, 9005, CLOSE_HALF, posted=_at(75), fwd=(R, RELAY_CLOSE))

    if restart_between:
        _restart(conn, on=(VIP, OTHER), seconds=65)
        _the_vips_forward_of_it(conn)
        the_others()
    else:
        _the_vips_forward_of_it(conn)
        the_others()
        _restart(conn, on=(VIP, OTHER), seconds=100)

    (new,) = [row[0] for row in _signals(conn)[1:]]
    acting = _acting_on(conn, new)
    links = _q(
        conn,
        "SELECT relay_of_followup_id FROM messages WHERE message_id IN (?, ?) ORDER BY message_id",
        VIP_CLOSE, 9005,
    )
    if restart_between:
        assert acting == [], (acting, _followups(conn))
        assert links == [(1,), (1,)], links
        alerts = _about_off(conn)
        assert [(a[0], a[1]) for a in alerts] == [(VIP_CLOSE, new), (9005, new)], alerts
        assert " ".join(alerts[0][3].split()).endswith(
            f"if the instruction was meant for signal {new}, apply it by hand."
        )
        assert " ".join(alerts[1][3].split()).endswith(
            f"It is the same instruction as message {VIP_CLOSE} on channel {V} "
            f"(alert #{_q(conn, 'SELECT MIN(alert_id) FROM alerts WHERE code = ?', ABOUT_OFF)[0][0]}"
            f"). Signal {new} trades that call instead: if the instruction was meant for "
            f"signal {new}, apply it by hand -- once, not once for each message."
        ), alerts[1][3]
        return
    assert len(acting) == 1, (acting, _followups(conn))
    (own,) = [row for row in _followups(conn) if (row[1], row[2]) == acting[0]]
    assert _followup_rows(conn, own[0]) == [("acct-1", "QUEUED"), ("acct-2", "QUEUED")]
    assert sorted(links, key=str) == sorted([(None,), (own[0],)], key=str), links


def test_a_forward_of_a_close_half_about_a_call_not_traded_instead_stays_a_copy(
    conn,  # noqa: F811
):
    """The VIP's copy of the relay's call is too old to be traded at the
    restart: no signal trades the call instead, so the VIP's forward of the
    relay's "close half" is left a copy of it."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _the_vips_forward_of_it(conn)

    _restart(conn, on=(VIP,), seconds=1000)

    assert _signals(conn)[1:] == []
    assert _q(conn, "SELECT relay_of_followup_id FROM messages WHERE message_id = ?", VIP_CLOSE) == [
        (1,)
    ]
    assert [row[1:3] for row in _followups(conn)] == [(R, RELAY_CLOSE)]


def test_a_forward_of_a_close_half_acts_on_the_call_it_was_given_about(conn):  # noqa: F811
    """Two calls of the relay, each copied by the VIP, are traded instead at
    the restart: signal 3 (the first call) and signal 4 (the second). The
    relay's "close half" replied to the SECOND call; the VIP's forward of it
    acts on signal 4 only -- never on the first call traded instead."""
    assert _ingest(conn, RELAY, 7003, OTHER_CALL).signal_id == 1
    assert _ingest(conn, VIP, 4003, OTHER_CALL, posted=_at(10)).relay_of_signal_id == 1
    assert _ingest(conn, RELAY, RELAY_CALL, posted=_at(20)).signal_id == 2
    assert _ingest(conn, VIP, VIP_COPY, posted=_at(30)).relay_of_signal_id == 2
    _the_relays_close_half(conn)
    assert _the_vips_forward_of_it(conn).relay_of_followup_id == 1

    _restart(conn, on=(VIP,), seconds=100)

    assert _signals(conn)[2:] == [(3, V, 4003, "NEW"), (4, V, VIP_COPY, "NEW")]
    assert _acting_on(conn, 3) == []
    assert _acting_on(conn, 4) == [(V, VIP_CLOSE)]


def test_the_vip_forwarding_and_repeating_the_relays_close_half_gives_one_instruction(
    conn,  # noqa: F811
):
    """The VIP both forwards the relay's "close half" and says it again
    replying to its copy of the call: two copies of the relay's one
    instruction, on one channel. One of them is the instruction about
    signal 2; the other repeats it."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _the_vips_forward_of_it(conn)
    _ingest(conn, VIP, VIP_CLOSE + 1, CLOSE_HALF, posted=_at(72), reply_to=VIP_COPY)

    _restart(conn, on=(VIP,), seconds=100)

    acting = _acting_on(conn, 2)
    assert acting == [(V, VIP_CLOSE)], (acting, _followups(conn))
    (own,) = [row for row in _followups(conn) if row[1:3] == (V, VIP_CLOSE)]
    assert _q(
        conn, "SELECT relay_of_followup_id FROM messages WHERE message_id = ?", VIP_CLOSE + 1
    ) == [(own[0],)]


def test_a_follow_up_on_the_channel_switched_off_stays_with_the_cancelled_call(
    conn,  # noqa: F811
):
    """The relay forwarded the other channel's "close half" before that post
    was stored: the relay's forward holds the instruction, its target taken
    from the other channel's post ('copy-reply'). The relay is switched off;
    the other channel's post is adopted as the instruction about the call
    traded instead, and the relay's stays with the cancelled call -- never
    two instructions about signal 2."""
    _relay_call_then_vip_copy(conn)
    assert _ingest(conn, OTHER, OTHER_COPY, posted=_at(20)).relay_of_signal_id == 1
    held = _ingest(conn, RELAY, RELAY_CLOSE, CLOSE_HALF, posted=_at(60), fwd=(OTHER.channel_id, 9005))
    assert held.followup_id == 1
    _ingest(conn, OTHER, 9005, CLOSE_HALF, posted=_at(55), reply_to=OTHER_COPY)
    assert _followups(conn) == [(1, R, RELAY_CLOSE, 1, relay_repo.COPY_REPLY_PATH)]

    _restart(conn, on=(VIP, OTHER), seconds=100)

    assert _signals(conn)[1:] == [(2, V, VIP_COPY, "NEW")]
    assert _acting_on(conn, 2) == [(OTHER.channel_id, 9005)]
    assert relay_repo.signal_for_followup(conn, followup_id=1) == 1


def test_a_reply_after_the_restart_to_an_old_copy_acts_on_the_call_traded_in_its_place(
    conn,  # noqa: F811
):
    """As at the start: the other channel's copy, 800 s after the relay's
    call, is signal 2; the VIP's copy 800 s before it is 1600 s from signal
    2 but repeats signal 1's call, which signal 2 carries now (final fix
    round 7, R7-1). The VIP's "close half" replying to it, read after the
    restart, repeats the relay's instruction as the relay rules match it
    with both channels read (another channel, within the window): it is
    that instruction about signal 2, standing in for the relay's -- one,
    never two."""
    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    assert _ingest(conn, VIP, VIP_COPY, posted=_at(-800)).relay_of_signal_id == 1
    assert _ingest(conn, OTHER, OTHER_COPY, posted=_at(800)).relay_of_signal_id == 1
    _the_relays_close_half(conn, seconds=810)
    _restart(conn, seconds=900)
    assert _signals(conn)[1:] == [(2, OTHER.channel_id, OTHER_COPY, "NEW")]

    reply = _ingest(conn, VIP, VIP_CLOSE, CLOSE_HALF, posted=_at(820), reply_to=VIP_COPY)

    assert reply.outcome == "new", reply
    assert _acting_on(conn, 2) == [(V, VIP_CLOSE)]
    assert _acting_on(conn, 1) == [(R, RELAY_CLOSE)]
    assert _q(conn, "SELECT instead_of_followup_id FROM followups WHERE followup_id = ?",
              reply.followup_id) == [(1,)]
    assert _about_off(conn) == []


def test_a_forward_of_a_close_half_about_a_call_still_traded_stays_a_copy(
    conn,  # noqa: F811
):
    """Nothing is switched off: the relay's call (signal 1) and the same call
    the VIP posted 910 s later (signal 2, outside signal 1's window) both
    trade. The VIP's forward of the relay's "close half" about signal 1 is a
    copy of it -- never taken for an instruction about signal 2."""
    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    assert _ingest(conn, VIP, VIP_COPY, posted=_at(910)).signal_id == 2
    _the_relays_close_half(conn)

    forward = _the_vips_forward_of_it(conn, seconds=950)

    assert forward.relay_of_followup_id == 1, forward
    assert _acting_on(conn, 2) == []


# -- one held instruction, at most one instruction about the call traded
# -- instead (final fix round 6, R6-1) ---------------------------------------

ABOUT_OFF = relay_repo.FOLLOWUP_ABOUT_CHANNEL_OFF_SIGNAL
FWD, REP, FWD2, REP2 = 4005, 4006, 4007, 4007
#: The words that tell each kind of R6-1 WARNING apart.
ALREADY = "already acts on it as follow-up 2 "
BY_HAND = "if the instruction was meant for signal 2, apply it by hand."
ONCE = "apply it by hand -- once, not once for each message."
NO_OTHER = "No other signal is known to trade that call."


def _fwd(conn, message_id=FWD, seconds=70):  # noqa: F811
    return _ingest(conn, VIP, message_id, CLOSE_HALF, posted=_at(seconds), fwd=(R, RELAY_CLOSE))


def _rep(conn, message_id=REP, seconds=72):  # noqa: F811
    return _ingest(conn, VIP, message_id, CLOSE_HALF, posted=_at(seconds), reply_to=VIP_COPY)


def _start(seconds):
    return lambda conn: _restart(conn, on=(VIP,), seconds=seconds)  # noqa: F811


def _relay_close(conn):  # noqa: F811
    _the_relays_close_half(conn)


#: The re-check's p52 probes (final fix round 6, R6-1): the relay posts the
#: call (signal 1), the VIP copies it (4001); the relay says "close half"
#: replying to its call (7005); the VIP forwards that post and/or says
#: "close half" replying to its copy, and watch restarts with the relay
#: switched off -- in every order. Each case: the events, the one post whose
#: instruction acts on signal 2 (or None), and the R6-1 WARNINGs raised, as
#: (message, words that tell its kind apart).
P52 = {
    # p52_forward_and_reply
    "fwd, reply, then start": (
        [_relay_close, _fwd, _rep, _start(100)], FWD, []),
    "start, then fwd, reply": (
        [_relay_close, _start(65), _fwd, _rep], None, [(FWD, BY_HAND), (REP, ONCE)]),
    "start, then reply, fwd": (
        [_relay_close, _start(65), lambda c: _rep(c, seconds=70), lambda c: _fwd(c, seconds=72)],
        REP, [(FWD, ALREADY)]),
    "fwd, start, reply": (
        [_relay_close, _fwd, _start(71), _rep], FWD, [(REP, ALREADY)]),
    "reply, start, fwd": (
        [_relay_close, lambda c: _rep(c, seconds=70), _start(71), lambda c: _fwd(c, seconds=72)],
        REP, [(FWD, ALREADY)]),
    # p52_more_orders
    "VIP fwd first, relay, reply, start": (
        [_fwd, _relay_close, _rep, _start(100)], REP, []),
    "VIP fwd first, relay, start, reply": (
        [_fwd, _relay_close, _start(100), lambda c: _rep(c, seconds=101)], FWD, []),
    "relay, two fwds, start": (
        [_relay_close, _fwd, lambda c: _fwd(c, FWD2, 75), _start(100)], FWD, []),
    "relay, start, two fwds": (
        [_relay_close, _start(65), _fwd, lambda c: _fwd(c, FWD2, 75)], None,
        [(FWD, BY_HAND), (FWD2, ONCE)]),
    "relay, fwd, start, fwd": (
        [_relay_close, _fwd, _start(72), lambda c: _fwd(c, FWD2, 75)], FWD, [(FWD2, ALREADY)]),
    # p52_two_replies
    "reply, reply, start": (
        [_relay_close, lambda c: _rep(c, seconds=70), lambda c: _rep(c, REP2, 80), _start(100)],
        REP, []),
    "reply, start, reply": (
        [_relay_close, lambda c: _rep(c, seconds=70), _start(75), lambda c: _rep(c, REP2, 80)],
        REP, [(REP2, ALREADY)]),
    "start, reply, reply": (
        [_relay_close, _start(65), lambda c: _rep(c, seconds=70), lambda c: _rep(c, REP2, 80)],
        REP, [(REP2, ALREADY)]),
}


def _about_off(conn) -> list[tuple]:  # noqa: F811
    return _q(
        conn,
        "SELECT message_id, signal_id, level, detail FROM alerts WHERE code = ? ORDER BY alert_id",
        ABOUT_OFF,
    )


@pytest.mark.parametrize("case", list(P52))
def test_the_relays_one_close_half_acts_at_most_once_on_the_call_traded_instead(
    conn, case  # noqa: F811
):
    """Final fix round 6 (R6-1), the re-check's p52 probes. Whatever the
    order -- the VIP's forward and its own "close half" before or after the
    restart, its forward stored before the relay's post or after it -- the
    relay's ONE "close half" gives AT MOST ONE instruction about signal 2,
    the VIP's post traded instead: never two, which would close half twice.
    One read after the restart that cannot be tied safely to signal 2 -- a
    forward replies to nothing -- is stored as a copy and NOT applied, with
    a WARNING; and once one such WARNING told the user to apply it by hand,
    no later copy of the same instruction is applied either, so it is never
    done twice."""
    events, acting, warned = P52[case]
    _relay_call_then_vip_copy(conn)
    for event in events:
        event(conn)

    assert _signals(conn)[1:] == [(2, V, VIP_COPY, "NEW")]
    assert _acting_on(conn, 2) == ([] if acting is None else [(V, acting)]), _followups(conn)
    for followup_id, *_ in _followups(conn):
        if relay_repo.signal_for_followup(conn, followup_id=followup_id) == 2:
            assert _followup_rows(conn, followup_id) == [("acct-1", "QUEUED"), ("acct-2", "QUEUED")]
    alerts = _about_off(conn)
    assert [(a[0], a[1], a[2]) for a in alerts] == [(m, 2, "WARNING") for m, _ in warned], alerts
    for (message_id, ending), alert in zip(warned, alerts, strict=True):
        assert ending in " ".join(alert[3].split()), alert[3]
        # Linked to the follow-up that acts in its place, or to the relay's.
        (instead,) = [row[0] for row in _followups(conn) if row[2] == acting] or [1]
        assert _q(
            conn, "SELECT relay_of_followup_id FROM messages WHERE message_id = ?", message_id
        ) == [(instead if ending == ALREADY else 1,)]
        for jargon in ("client", "listener", "rpc", "flood", "outbox"):
            assert jargon not in alert[3].lower(), (jargon, alert[3])


def test_a_forward_read_after_the_restart_is_not_applied_and_said_so(conn):  # noqa: F811
    """The round-5 pins f1_orders / r44_variants F1, restart between: the
    relay's "close half" was read, then watch restarted with the relay
    switched off -- the VIP's post is signal 2 -- and only then the VIP's
    forward of it. A forward replies to nothing: nothing ties it to signal
    2 but a guess. It is stored as a copy of the relay's instruction, NOT
    applied, and ONE WARNING names the message, the instruction, the call
    it was given about and its channel, and the signal traded instead."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _restart(conn, on=(VIP,), seconds=65)

    forward = _the_vips_forward_of_it(conn)

    assert forward.outcome == "relay_duplicate" and forward.relay_of_followup_id == 1, forward
    assert _acting_on(conn, 2) == []
    assert [row[1:3] for row in _followups(conn)] == [(R, RELAY_CLOSE)]
    ((message_id, signal_id, level, detail),) = _about_off(conn)
    assert (message_id, signal_id, level) == (VIP_CLOSE, 2, "WARNING")
    assert " ".join(detail.split()) == (
        f"message {VIP_CLOSE} on channel {V} (VIP) repeats the instruction 'close half of the "
        f"trade' of message {RELAY_CLOSE} on channel {R}, about signal 1 (message {RELAY_CALL} "
        f"on channel {R}), whose channel is switched off: signal 1 is not traded, and this "
        "message was NOT applied. Signal 2 trades that call instead: if the instruction was "
        "meant for signal 2, apply it by hand."
    ), detail


def test_a_repeat_of_an_instruction_withdrawn_since_is_not_applied_and_said_so(
    conn,  # noqa: F811
):
    """The VIP's forward of the relay's "close half" was adopted at the
    restart as the instruction about signal 2, then the VIP deleted it: it
    is withdrawn. The VIP's "close half" read after that, repeating the
    relay's instruction, is not applied either -- and the WARNING says the
    one about signal 2 was withdrawn, never that it still acts."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    _restart(conn, on=(VIP,), seconds=71)
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=FWD, now_utc=_at(72), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()

    _rep(conn, seconds=73)

    assert _followup_rows(conn, 2) == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]
    assert [row[1:3] for row in _followups(conn)] == [(R, RELAY_CLOSE), (V, FWD)]
    ((message_id, signal_id, _level, detail),) = _about_off(conn)
    assert (message_id, signal_id) == (REP, 2)
    flat = " ".join(detail.split())
    assert ALREADY not in flat, flat
    assert flat.endswith(
        f"Signal 2 trades that call instead; the same instruction was given about it as "
        f"follow-up 2 (message {FWD} on channel {V}), which was withdrawn since. If the "
        "instruction is still meant for signal 2, apply it by hand."
    ), flat


@pytest.mark.parametrize("order", ["forward, reply", "reply, forward"])
def test_repeats_of_an_instruction_withdrawn_since_ask_for_it_by_hand_once(
    conn, order  # noqa: F811
):
    """The re-check's r61_withdrawn_standin (fuzz seed 42417; final fix
    round 7, R7-3). After the start the VIP's "close half" replying to its
    copy is the instruction about signal 2, in place of the relay's; then
    the VIP deletes it. Then it forwards the relay's post and says "close
    half" again, in either order: neither is applied. The first WARNING asks
    for it by hand if it is still meant; the second says it is the same
    instruction as the first message, names that alert, and asks for it
    once, not once for each message -- never two by-hand requests."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _restart(conn, on=(VIP,), seconds=65)
    assert _rep(conn, message_id=REP, seconds=70).followup_id == 2
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=REP, now_utc=_at(75), accounts=ACCOUNTS,
        detected_by="event", read_channels=(V,),
    )
    conn.commit()

    if order == "forward, reply":
        first, second = _fwd(conn, seconds=80), _rep(conn, message_id=4007, seconds=85)
        ids = (FWD, 4007)
    else:
        first, second = _rep(conn, message_id=4007, seconds=80), _fwd(conn, seconds=85)
        ids = (4007, FWD)

    assert (first.outcome, second.outcome) == ("relay_duplicate", "relay_duplicate")
    assert _followup_rows(conn, 2) == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]
    alerts = _q(
        conn, "SELECT alert_id, message_id, detail FROM alerts WHERE code = ? ORDER BY alert_id",
        ABOUT_OFF,
    )
    assert [a[1] for a in alerts] == list(ids)
    withdrawn = (
        f"Signal 2 trades that call instead; the same instruction was given about it as "
        f"follow-up 2 (message {REP} on channel {V}), which was withdrawn since. If the "
        "instruction is still meant for signal 2, apply it by hand"
    )
    one, two = (" ".join(a[2].split()) for a in alerts)
    assert one.endswith("this message was NOT applied. " + withdrawn + "."), one
    assert two.endswith(
        f"this message was NOT applied. It is the same instruction as message {ids[0]} on "
        f"channel {V} (alert #{alerts[0][0]}). {withdrawn} -- once, not once for each message."
    ), two


@pytest.mark.parametrize(
    "change", ["edited to close all", "deleted", "edited, no signal trades the call"]
)
def test_a_change_to_a_message_not_applied_says_so_and_what_to_review_by_hand(
    conn, change  # noqa: F811
):
    """The re-check's r61_byhand_copy_changed (final fix round 7, R7-4).
    After the start with the relay switched off, the VIP forwards the
    relay's "close half": a copy of follow-up 1, about the cancelled signal
    1, NOT applied, with the WARNING asking for it by hand on signal 2. Then
    the VIP edits that forward into "close all", or deletes it. The alert
    says this message was NOT applied (naming that WARNING), what it says
    now in plain words -- never "close_all" -- or that it was deleted, and
    that a by-hand "close half" on signal 2 needs a review; never that
    follow-up 1 "is the one acted on" with instructions "still queued".
    With no signal trading the call (the VIP's copy withdrawn first) no
    by-hand request was made: nothing to do."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _restart(conn, on=(VIP,), seconds=65)
    if change == "edited, no signal trades the call":
        edits_repo.apply_deletion(
            conn, channel_cfg=VIP, message_id=VIP_COPY, now_utc=_at(68), accounts=ACCOUNTS,
            detected_by="event", read_channels=(V,),
        )
        conn.commit()
    assert _fwd(conn).relay_of_followup_id == 1
    (asked,) = _q(conn, "SELECT alert_id FROM alerts WHERE code = ?", ABOUT_OFF)

    if change == "deleted":
        edits_repo.apply_deletion(
            conn, channel_cfg=VIP, message_id=FWD, now_utc=_at(80), accounts=ACCOUNTS,
            detected_by="event", read_channels=(V,),
        )
    else:
        edits_repo.apply_edit(
            conn, channel_cfg=VIP, message_id=FWD, text="Close all now", posted_at_utc=_at(70),
            edit_date_utc=_at(80), reply_to_message_id=None, now_utc=_at(80), accounts=ACCOUNTS,
            detected_by="event", read_channels=(V,),
        )
    conn.commit()

    (alert,) = _q(
        conn, "SELECT level, signal_id, detail FROM alerts WHERE code = ?",
        edits_repo.FOLLOWUP_COPY_CHANGED,
    )
    flat = " ".join(alert[2].split())
    what = (
        "was deleted, found by event." if change == "deleted"
        else "was edited; it now says 'close the whole trade'."
    )
    then = (
        "Nothing to do." if change == "edited, no signal trades the call"
        else "If you applied the instruction by hand on signal 2, review that."
    )
    assert flat == (
        f"message {FWD} on channel {V}, a copy of follow-up 1 (message {RELAY_CLOSE} on channel "
        f"{R}), {what} This message was NOT applied: follow-up 1 is about signal 1, whose "
        f"channel is switched off (alert #{asked[0]}). {then} [found by event]"
    ), flat
    assert alert[0] == "WARNING"
    assert "close_all" not in flat and "is the one acted on" not in flat
    assert _acting_on(conn, 2) == []



@pytest.mark.parametrize(
    ("text", "now"),
    [
        ("Close all now", "it now says 'close the whole trade'"),
        ("nice trade everyone", "it no longer reads as an instruction"),
        (
            SIGNAL_TEXT,
            "it now reads as a trade signal: BUY 4480-4485 SL 4470 TP 4490/4495/4500/4505",
        ),
        (
            SIGNAL_TEXT + "\nCANCELLED",
            "it now reads as a trade signal with CANCELLED added (a retraction)",
        ),
    ],
    ids=["an instruction", "chatter", "a call", "a call cancelled"],
)
def test_a_message_not_applied_edited_says_what_it_says_now_in_plain_words(
    conn, text, now  # noqa: F811
):
    """R7-4: whatever the VIP edits its forward of the relay's "close half"
    into -- after the start with the relay switched off, so it was NOT
    applied -- the WARNING says what it says now in plain words."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _restart(conn, on=(VIP,), seconds=65)
    _fwd(conn)

    edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=FWD, text=text, posted_at_utc=_at(70),
        edit_date_utc=_at(80), reply_to_message_id=None, now_utc=_at(80), accounts=ACCOUNTS,
        detected_by="event", read_channels=(V,),
    )
    conn.commit()

    (detail,) = _q(
        conn, "SELECT detail FROM alerts WHERE code = ?", edits_repo.FOLLOWUP_COPY_CHANGED
    )
    flat = " ".join(detail[0].split())
    assert f"), was edited; {now}. This message was NOT applied: " in flat, flat



@pytest.mark.parametrize("change", ["edited to close all", "deleted"])
@pytest.mark.parametrize("stand_in", ["withdrawn since", "acting"])
def test_a_change_to_a_repeat_of_the_instruction_about_the_call_traded_is_never_silent(
    conn, stand_in, change  # noqa: F811
):
    """R7-4, the same for a repeat of the instruction standing in for the
    relay's: after the start the VIP's "close half" replying to its copy is
    the instruction about signal 2 (follow-up 2), and the VIP forwards the
    relay's "close half" -- a copy of follow-up 2, NOT applied. When the
    VIP had deleted follow-up 2 first, that forward's WARNING asked for the
    instruction by hand; its edit or deletion then says this message was NOT
    applied and to review what was done by hand -- never nothing, as when
    the change "reached both posts". With follow-up 2 acting, it is the one
    acted on, and the WARNING says so, as before."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _restart(conn, on=(VIP,), seconds=65)
    assert _rep(conn, seconds=70).followup_id == 2
    if stand_in == "withdrawn since":
        edits_repo.apply_deletion(
            conn, channel_cfg=VIP, message_id=REP, now_utc=_at(72), accounts=ACCOUNTS,
            detected_by="event", read_channels=(V,),
        )
        conn.commit()
    assert _fwd(conn, seconds=74).relay_of_followup_id == 2
    (asked,) = _q(conn, "SELECT alert_id FROM alerts WHERE code = ?", ABOUT_OFF)

    if change == "deleted":
        edits_repo.apply_deletion(
            conn, channel_cfg=VIP, message_id=FWD, now_utc=_at(80), accounts=ACCOUNTS,
            detected_by="event", read_channels=(V,),
        )
    else:
        edits_repo.apply_edit(
            conn, channel_cfg=VIP, message_id=FWD, text="Close all now", posted_at_utc=_at(74),
            edit_date_utc=_at(80), reply_to_message_id=None, now_utc=_at(80), accounts=ACCOUNTS,
            detected_by="event", read_channels=(V,),
        )
    conn.commit()

    (alert,) = _q(
        conn, "SELECT level, detail FROM alerts WHERE code = ?", edits_repo.FOLLOWUP_COPY_CHANGED
    )
    flat = " ".join(alert[1].split())
    assert alert[0] == "WARNING"
    start = f"message {FWD} on channel {V}, a copy of follow-up 2 (message {REP} on channel {V}), "
    assert flat.startswith(start), flat
    if stand_in == "acting":
        assert "Follow-up 2 is the one acted on, and it was NOT changed" in flat, flat
        if change != "deleted":
            assert "was edited; it now says 'close the whole trade'." in flat, flat
            assert "close_all" not in flat, flat
        assert "2 instruction(s) still queued" in flat, flat
        return
    what = (
        "was deleted, found by event." if change == "deleted"
        else "was edited; it now says 'close the whole trade'."
    )
    assert flat == (
        f"{start}{what} This message was NOT applied: follow-up 2, the same instruction about "
        f"signal 2, was withdrawn since (alert #{asked[0]}). If you applied the instruction by "
        "hand on signal 2, review that. [found by event]"
    ), flat



def test_a_message_not_applied_reworded_into_the_instruction_it_repeats_is_nothing_new(
    conn,  # noqa: F811
):
    """R7-4 keeps the rule that a copy edited to read as the instruction it
    repeats changes nothing: the VIP's forward of the relay's "close half",
    NOT applied after the start with the relay switched off, worded
    differently, is edited to read exactly as the relay's post. No new
    WARNING: the by-hand request stands as it was."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _restart(conn, on=(VIP,), seconds=65)
    copy = _ingest(conn, VIP, FWD, "Close half of it now", posted=_at(70), fwd=(R, RELAY_CLOSE))
    assert copy.relay_of_followup_id == 1, copy
    assert _q(conn, "SELECT kind FROM messages WHERE message_id = ?", FWD) == [("followup",)]

    result = edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=FWD, text=CLOSE_HALF, posted_at_utc=_at(70),
        edit_date_utc=_at(80), reply_to_message_id=None, now_utc=_at(80), accounts=ACCOUNTS,
        detected_by="event", read_channels=(V,),
    )
    conn.commit()

    assert result.alert_ids == (), result
    assert [a[1] for a in _alerts(conn)] == [CODE, ABOUT_OFF]



def test_fuzz_seed_42417_repeats_after_the_instruction_was_withdrawn_ask_for_it_once(
    conn,  # noqa: F811
):
    """The re-check's fuzz seed 42417 (R7-3). After the start with the
    relay switched off, the VIP's "close half" replying to its copy is the
    instruction about signal 2, and its forward of the relay's "close half"
    (posted earlier, read after) repeats it: nothing to do. The VIP then
    deletes that "close half", and forwards the relay's post and says
    "close half" again. Neither is applied; each asks for it by hand ONCE,
    naming the first message of that instruction -- never two requests that
    each read as their own."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn, seconds=86)
    _restart(conn, on=(VIP,), seconds=147)
    assert _rep(conn, message_id=4005, seconds=179).followup_id == 2
    assert _fwd(conn, message_id=4006, seconds=149).relay_of_followup_id == 2
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4005, now_utc=_at(180), accounts=ACCOUNTS,
        detected_by="event", read_channels=(V,),
    )
    conn.commit()

    again = (_fwd(conn, message_id=4007, seconds=105), _rep(conn, message_id=4008, seconds=150))

    assert [a.outcome for a in again] == ["relay_duplicate", "relay_duplicate"]
    assert _followup_rows(conn, 2) == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]
    assert _q(conn, "SELECT COUNT(*) FROM followups") == [(2,)]
    alerts = _q(
        conn, "SELECT alert_id, message_id, detail FROM alerts WHERE code = ? ORDER BY alert_id",
        ABOUT_OFF,
    )
    assert [a[1] for a in alerts] == [4006, 4007, 4008]
    first, *repeats = (" ".join(a[2].split()) for a in alerts)
    assert first.endswith("Nothing to do."), first
    for flat in repeats:
        assert (
            f"It is the same instruction as message 4006 on channel {V} (alert #{alerts[0][0]})."
            in flat
        ), flat
        assert flat.endswith("apply it by hand -- once, not once for each message."), flat



@pytest.mark.parametrize("again", ["the relay's post", "its own forward"])
def test_a_repeat_read_after_the_start_of_an_instruction_one_acts_on_is_nothing_to_do(
    conn, again  # noqa: F811
):
    """The re-check's r61_m10 (final fix round 7, R7-5 a). The VIP forwarded
    the relay's "close half" before the relay's post was stored -- its
    forward holds the instruction -- then said "close half" itself,
    replying to its copy of the call; watch restarted with the relay
    switched off. The VIP's reply acts on signal 2 in the forward's place,
    and the start records it standing in for the forward. Then the VIP
    forwards the relay's post again, or its own forward: still ONE
    instruction on signal 2, and the WARNING says the same instruction
    already acts on it -- never "apply it by hand", which would close half
    twice."""
    _relay_call_then_vip_copy(conn)
    _fwd(conn)
    _the_relays_close_half(conn)
    _rep(conn)
    _restart(conn, on=(VIP,), seconds=100)
    assert _q(conn, "SELECT followup_id, message_id, instead_of_followup_id FROM followups "
                    "ORDER BY 1") == [(1, FWD, None), (2, REP, 1)]

    repeat_id = 4007 if again == "the relay's post" else 4008
    forwarded = (R, RELAY_CLOSE) if again == "the relay's post" else (V, FWD)
    repeat = _ingest(conn, VIP, repeat_id, CLOSE_HALF, posted=_at(110), fwd=forwarded)

    assert repeat.outcome == "relay_duplicate", repeat
    assert _acting_on(conn, 2) == [(V, REP)]
    assert _followup_rows(conn, 2) == [("acct-1", "QUEUED"), ("acct-2", "QUEUED")]
    ((message_id, signal_id, level, detail),) = _about_off(conn)
    assert (message_id, signal_id, level) == (repeat_id, 2, "WARNING")
    flat = " ".join(detail.split())
    assert flat.endswith(
        "Signal 2 trades that call instead, and the same instruction already acts on it as "
        f"follow-up 2 (message {REP} on channel {V}). Nothing to do."
    ), flat
    assert "by hand" not in flat, flat


def test_a_close_half_given_long_after_is_an_instruction_of_its_own(conn):  # noqa: F811
    """After the restart the VIP forwards the relay's "close half" -- NOT
    applied, handed over by hand -- and half an hour later says "close
    half" again, replying to its post traded instead. That is no repeat of
    the relay's instruction by the relay rules (posted 900 s and more after
    it): an instruction of its own about signal 2."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _restart(conn, on=(VIP,), seconds=65)
    _fwd(conn)

    later = _rep(conn, seconds=2000)

    assert later.outcome == "new", later
    assert _acting_on(conn, 2) == [(V, REP)]
    assert [a[0] for a in _about_off(conn)] == [FWD]


def test_two_close_halfs_of_the_vip_long_after_are_two_instructions(conn):  # noqa: F811
    """After the restart, half an hour after the relay's "close half", the
    VIP says "close half" twice, 10 s apart, replying to its post traded
    instead: two posts of one channel are two instructions, as with both
    channels read (an accepted ruling) -- only a post of ANOTHER channel
    near it makes one a repeat of the other."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _restart(conn, on=(VIP,), seconds=65)

    _rep(conn, seconds=2000)
    _rep(conn, REP2, seconds=2010)

    assert _acting_on(conn, 2) == [(V, REP), (V, REP2)]
    assert _about_off(conn) == []


def test_the_vips_forward_of_its_own_forward_stays_one_instruction(conn):  # noqa: F811
    """The VIP forwarded the relay's "close half" before that post was
    stored -- the VIP's forward holds the instruction, its target taken from
    the relay's post -- and 15 minutes later forwards its own forward again,
    just before watch restarts with the relay switched off; the VIP's copy
    of the call (a forward of it, late) is signal 2. The second forward is
    adopted as the instruction about signal 2, and the first stays with the
    cancelled call: never two, even though no post of another channel is
    near the second forward."""
    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    assert _ingest(conn, VIP, FWD, CLOSE_HALF, posted=_at(70), fwd=(R, RELAY_CLOSE)).followup_id == 1
    _the_relays_close_half(conn)
    assert _ingest(conn, VIP, VIP_COPY, posted=_at(1000), fwd=(R, RELAY_CALL)).relay_of_signal_id == 1
    bump = _ingest(conn, VIP, 4009, CLOSE_HALF, posted=_at(1005), fwd=(V, FWD))
    assert bump.relay_of_followup_id == 1, bump

    _restart(conn, on=(VIP,), seconds=1010)

    assert _signals(conn)[1:] == [(2, V, VIP_COPY, "NEW")]
    assert _acting_on(conn, 2) == [(V, 4009)], _followups(conn)
    assert relay_repo.signal_for_followup(conn, followup_id=1) == 1


def test_a_forward_names_no_signal_traded_instead_once_that_one_is_withdrawn(
    conn,  # noqa: F811
):
    """Signal 2 was admitted in place of the relay's call, then the VIP
    deleted its post: signal 2 is withdrawn, so nothing trades the call any
    more. The VIP's forward of the relay's "close half" read after that
    names no signal to apply it to."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _restart(conn, on=(VIP,), seconds=65)
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=VIP_COPY, now_utc=_at(68), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()
    assert _claimable(conn) == []

    _fwd(conn)

    ((message_id, signal_id, _level, detail),) = _about_off(conn)
    assert (message_id, signal_id) == (FWD, 1)
    flat = " ".join(detail.split())
    assert flat.endswith(NO_OTHER) and "signal 2" not in flat, flat


@pytest.mark.parametrize("later", [1500, 86400], ids=["25 minutes later", "a day later"])
@pytest.mark.parametrize("relay_on", [False, True], ids=["relay off", "relay on"])
def test_a_forward_of_an_old_close_half_never_acts_on_a_later_call(
    conn, later, relay_on  # noqa: F811
):
    """The re-check's p52_far_carrier. The relay's call (signal 1), never
    copied, and its "close half"; watch restarts with the relay switched
    off. Later -- outside the relay window, so another call by the relay
    rules -- the VIP posts the same side, symbol and zone (signal 2, its
    own) and forwards the relay's "close half". It is NOT an instruction
    about signal 2 -- that would close half of a fresh, unrelated trade --
    but a copy of the relay's, and the WARNING names no other signal. With
    the relay switched on it is a copy, quietly, as ever."""
    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    _the_relays_close_half(conn)
    _restart(conn, on=(VIP, RELAY) if relay_on else (VIP,), seconds=100)
    assert _ingest(conn, VIP, VIP_COPY, posted=_at(later)).signal_id == 2

    forward = _the_vips_forward_of_it(conn, seconds=later + 10)

    assert forward.relay_of_followup_id == 1, forward
    assert _acting_on(conn, 2) == []
    alerts = _about_off(conn)
    if relay_on:
        assert alerts == []
    else:
        ((message_id, signal_id, level, detail),) = alerts
        assert (message_id, signal_id, level) == (VIP_CLOSE, 1, "WARNING")
        flat = " ".join(detail.split())
        assert flat.endswith("this message was NOT applied. " + NO_OTHER), flat
        assert "signal 2" not in flat, flat


@pytest.mark.parametrize("with_reply", [False, True], ids=["forward only", "and a reply"])
def test_with_both_channels_read_the_vips_own_repeats_stay_separate(
    conn, with_reply  # noqa: F811
):
    """The re-check's p52_vipfirst_both_on, unchanged (an accepted ruling):
    with both channels read, the VIP's forward stored before the relay's
    post holds the instruction, and the VIP's own "close half" on its own
    channel is a second instruction, as two posts of one channel are."""
    _relay_call_then_vip_copy(conn)
    _fwd(conn)
    _the_relays_close_half(conn)
    if with_reply:
        _rep(conn)

    assert _acting_on(conn, 1) == ([(V, FWD), (V, REP)] if with_reply else [(V, FWD)])
    assert _about_off(conn) == []


def test_the_start_records_the_signal_it_admits_and_the_follow_up_each_stands_in_for(
    conn,  # noqa: F811
):
    """R6-1 (b) and (c): the start-up job keeps which switched-off signal
    each admitted one stands in for (`signals.admitted_from_signal_id`), and
    which held follow-up each adopted one stands in for
    (`followups.instead_of_followup_id`)."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)

    _restart(conn, on=(VIP,), seconds=100)

    assert _q(conn, "SELECT signal_id, admitted_from_signal_id FROM signals ORDER BY 1") == [
        (1, None), (2, 1)
    ]
    assert _q(
        conn, "SELECT followup_id, message_id, instead_of_followup_id FROM followups ORDER BY 1"
    ) == [(1, RELAY_CLOSE, None), (2, FWD, 1)]


def test_a_later_start_never_adopts_a_second_copy_of_one_held_instruction(
    conn,  # noqa: F811
):
    """R6-1 (c): the follow-up a copy was adopted as is kept, not only for
    one start-up job. Should a later one look at the copies of the same
    held instruction again for the same signal, a copy is linked to the
    follow-up adopted first -- never a second instruction."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    _restart(conn, on=(VIP,), seconds=100)
    _rep(conn, seconds=72)  # read later, stored as a copy of the one adopted
    conn.execute(
        "UPDATE messages SET relay_of_followup_id = 1 WHERE channel_id = ? AND message_id = ?",
        (V, REP),
    )

    relay_repo._adopt_followup_copies(
        conn,
        new=relay_repo._signal_row(conn, 2),
        admitted_from=relay_repo.chain_ids(conn, 1),
        old_chains=set(relay_repo.chain_ids(conn, 1)),
        on={V: VIP},
        accounts=ACCOUNTS,
        now_utc=_at(200),
        relay_window_seconds=900,
    )

    assert _acting_on(conn, 2) == [(V, FWD)]
    assert _q(conn, "SELECT relay_of_followup_id FROM messages WHERE message_id = ?", REP) == [(2,)]


# -- a channel switched back on (final fix round 7, R7-1, R7-2) ---------------

QUEUED_TWICE = [("acct-1", "QUEUED"), ("acct-2", "QUEUED")]
SWITCHED = pytest.mark.parametrize(
    "switched", [False, True], ids=["both channels read", "relay off, then back on"]
)


def _relay_off_then_back_on(conn, *, off_at: float = 40, on_at: float = 50):  # noqa: F811
    """`watch` restarts with the relay switched off -- the VIP's copy of its
    call is admitted as signal 2 -- and later restarts with it switched
    back on."""
    _restart(conn, on=(VIP,), seconds=off_at)
    _restart(conn, on=(VIP, RELAY), seconds=on_at)


@pytest.mark.parametrize(
    "posted", [60, 35], ids=["posted once back on", "posted before the switch-off, read once on"]
)
@SWITCHED
def test_the_relays_close_half_read_once_it_is_back_on_acts_on_the_call_traded(
    conn, switched, posted  # noqa: F811
):
    """The re-check's r61_back_on_relay_only (final fix round 7, R7-1). The
    relay's call is signal 1 and the VIP's copy of it signal 2 since the
    start with the relay switched off; the relay is switched back on. Its
    "close half" replying to its call -- posted after that, or posted
    before the switch-off and read only once it is back on -- acts on
    signal 2, the call traded, exactly as it acts on signal 1 with both
    channels read throughout. Before, it acted on the cancelled signal 1:
    nothing closed half, and nothing said so."""
    _relay_call_then_vip_copy(conn)
    if switched:
        _relay_off_then_back_on(conn)
    traded = 2 if switched else 1

    close = _ingest(conn, RELAY, RELAY_CLOSE, CLOSE_HALF, posted=_at(posted), reply_to=RELAY_CALL)

    assert close.outcome == "new", close
    assert _claimable(conn) == [traded]
    assert _acting_on(conn, traded) == [(R, RELAY_CLOSE)]
    assert _followup_rows(conn, close.followup_id) == QUEUED_TWICE
    assert [a[1] for a in _alerts(conn)] == ([CODE] if switched else [])


@pytest.mark.parametrize("executed", [False, True], ids=["nothing executed", "one account traded"])
@SWITCHED
def test_the_relay_deleting_its_call_once_back_on_withdraws_the_call_traded(
    conn, switched, executed  # noqa: F811
):
    """r61_back_on_edits, the deletion (R7-1 b). The relay deletes its call,
    its last post of it, once it is back on: the call is withdrawn (ruling
    C2) -- signal 2, the one traded, VOIDED when nothing of it was executed,
    flagged CRITICAL when it was -- as signal 1 is with both channels read.
    Never "Nothing had been executed" about the cancelled signal 1 while
    signal 2 stays claimable."""
    _relay_call_then_vip_copy(conn)
    if switched:
        _relay_off_then_back_on(conn)
    traded = 2 if switched else 1
    if executed:
        conn.execute(
            "UPDATE outbox SET state = 'DONE' WHERE outbox_id = (SELECT MIN(outbox_id) "
            "FROM outbox WHERE kind = 'ENTRY' AND signal_id = ?)",
            (traded,),
        )
        conn.commit()

    result = edits_repo.apply_deletion(
        conn, channel_cfg=RELAY, message_id=RELAY_CALL, now_utc=_at(60), accounts=ACCOUNTS,
        detected_by="event", read_channels=(V, R),
    )
    conn.commit()

    assert result.outcome == ("flagged" if executed else "voided"), result
    assert result.signal_id == traded, result
    assert _claimable(conn) == []
    assert _q(conn, "SELECT state, withdrawn_at_utc IS NOT NULL FROM signals WHERE signal_id = ?",
              traded) == [("NEW" if executed else "VOIDED", 1)]
    (alert,) = [a for a in _alerts(conn) if a[1] != CODE]
    flat = " ".join(alert[5].split())
    if executed:
        assert alert[:2] == ("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED), alert
        assert f"signal {traded} was withdrawn by the poster" in flat, flat
    else:
        assert alert[:2] == ("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION), alert
        assert f"signal {traded} VOIDED: the poster withdrew it" in flat, flat
        assert "Nothing had been executed; 2 queued entries skipped." in flat, flat
    assert alert[4] == traded
    if switched:
        assert "signal 1 VOIDED" not in flat, flat
        assert flat.startswith(
            f"message {RELAY_CALL} on channel {R} first carried this call as signal 1; signal 2 "
            f"(message {VIP_COPY} on channel {V}) carries it now: "
        ), flat


@pytest.mark.parametrize("how", ["stop moved", "re-aimed"])
@SWITCHED
def test_the_relays_edit_of_its_call_once_back_on_reaches_the_call_traded(
    conn, switched, how  # noqa: F811
):
    """r61_back_on_edits, the edits (R7-1). Once the relay is back on, its
    edit of its call reaches signal 2, the call traded, as it reaches signal
    1 with both channels read: a stop moved is the traded signal's stop now
    (signal 1, cancelled, keeps the one it had), and a call re-aimed to
    another zone -- the relay's last post of it -- invalidates it."""
    _relay_call_then_vip_copy(conn)
    if switched:
        _relay_off_then_back_on(conn)
    traded = 2 if switched else 1

    edits_repo.apply_edit(
        conn, channel_cfg=RELAY, message_id=RELAY_CALL,
        text=OTHER_CALL if how == "stop moved" else REAIMED, posted_at_utc=POSTED,
        edit_date_utc=_at(60), reply_to_message_id=None, now_utc=_at(60), accounts=ACCOUNTS,
        detected_by="event", read_channels=(V, R),
    )
    conn.commit()

    stops = dict(_q(conn, "SELECT signal_id, json_extract(parsed_json, '$.sl') FROM signals"))
    if how == "stop moved":
        assert _claimable(conn) == [traded]
        assert stops[traded] == 4465
        if switched:
            assert stops[1] == 4470
    else:
        assert _claimable(conn) == []
        assert _q(conn, "SELECT state FROM signals WHERE signal_id = ?", traded) == [("INVALID",)]
        (alert,) = [a for a in _alerts(conn) if a[1] != CODE]
        assert alert[:2] == ("CRITICAL", edits_repo.SIGNAL_INVALIDATED_BY_EDIT), alert
        assert alert[4] == traded


def test_an_instruction_read_before_the_switch_off_stays_one_once_back_on(conn):  # noqa: F811
    """R7-1 keeps every R6-1 outcome. The relay's "close half" was read
    before the start that switched it off, and the VIP's forward of it was
    adopted there as the instruction about signal 2. With the relay back on
    the relay's own follow-up still acts on the cancelled signal 1 -- the
    VIP's forward acts in its place -- so the one instruction stays one."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    _relay_off_then_back_on(conn, off_at=80, on_at=90)

    assert relay_repo.signal_for_followup(conn, followup_id=1) == 1
    assert _acting_on(conn, 2) == [(V, FWD)]
    assert relay_repo.live_signal_for(conn, 1) == 2


@SWITCHED
def test_the_relay_back_on_posting_its_withdrawn_call_again_is_a_new_signal(
    conn, switched  # noqa: F811
):
    """R7-1: the relay, back on, deletes its call -- the call traded is
    withdrawn -- and posts it again. The relay withdrew the call, so its new
    post is a new signal in its place (ruling R3-P3), queued, as with both
    channels read -- never held back as a copy of a call withdrawn by
    another channel, with a CRITICAL "trade it by hand"."""
    _relay_call_then_vip_copy(conn)
    if switched:
        _relay_off_then_back_on(conn)
    traded = 2 if switched else 1
    edits_repo.apply_deletion(
        conn, channel_cfg=RELAY, message_id=RELAY_CALL, now_utc=_at(60), accounts=ACCOUNTS,
        detected_by="event", read_channels=(V, R),
    )
    conn.commit()

    again = _ingest(conn, RELAY, 7002, posted=_at(70))

    assert (again.outcome, again.signal_id) == ("new", traded + 1), again
    assert _q(conn, "SELECT promoted_from_signal_id, replacement_kind FROM signals "
                    "WHERE signal_id = ?", again.signal_id) == [(traded, "posted_again")]
    assert _claimable(conn) == [again.signal_id]
    assert relay_repo.COPY_OF_WITHDRAWN_SIGNAL not in [a[1] for a in _alerts(conn)]


def test_a_follow_up_kept_with_the_switched_off_call_never_follows_it_posted_again(
    conn,  # noqa: F811
):
    """R7-1: the relay's "close half", read before the start, stays with the
    cancelled signal 1; the VIP's forward of it acts on signal 2 in its
    place. The VIP then deletes its post -- signal 2 withdrawn -- and posts
    the call again: signal 3 replaces signal 2, and the VIP's instruction
    moves to it. The relay's never does: one instruction, not two."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    _restart(conn, on=(VIP,), seconds=80)
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=VIP_COPY, now_utc=_at(85), accounts=ACCOUNTS,
        detected_by="event", read_channels=(V,),
    )
    conn.commit()

    again = _ingest(conn, VIP, 4030, posted=_at(90))

    assert again.signal_id == 3, again
    assert _q(conn, "SELECT promoted_from_signal_id FROM signals WHERE signal_id = 3") == [(2,)]
    assert _acting_on(conn, 3) == [(V, FWD)]
    assert relay_repo.signal_for_followup(conn, followup_id=1) == 1


def test_a_call_carried_elsewhere_takes_only_the_follow_ups_since_the_start(
    conn,  # noqa: F811
):
    """R7-1, `record_carried`: when signal 2 -- admitted in place of the
    relay's call -- is withdrawn and another signal carries the same trade,
    the follow-ups about signal 2 repeat that one; the relay's, kept with
    the cancelled signal 1, does not."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    _restart(conn, on=(VIP,), seconds=80)
    assert _ingest(conn, OTHER, OTHER_COPY, posted=_at(5000)).signal_id == 3

    relay_repo.record_carried(conn, carrier_signal_id=3, withdrawn_signal_id=2)

    assert _acting_on(conn, 3) == [(V, FWD)]
    assert relay_repo.signal_for_followup(conn, followup_id=1) == 1


def test_the_vips_bump_of_its_own_close_half_stays_one_instruction_at_the_start(
    conn,  # noqa: F811
):
    """R7-1: the VIP said "close half" replying to its copy of the call, and
    forwarded that post of its own (a bump) -- a copy of it. At the start
    the VIP's copy is signal 2 and its "close half" moves with it; the bump
    repeats an instruction about signal 2 already, and stays a copy: one
    instruction, not two."""
    _relay_call_then_vip_copy(conn)
    _rep(conn)
    bump = _ingest(conn, VIP, 4009, CLOSE_HALF, posted=_at(80), fwd=(V, REP))
    assert bump.relay_of_followup_id == 1, bump

    _restart(conn, on=(VIP,), seconds=100)

    assert _acting_on(conn, 2) == [(V, REP)]
    assert _q(conn, "SELECT relay_of_followup_id FROM messages WHERE message_id = 4009") == [(1,)]


def test_admitted_for_names_only_a_signal_admitted_in_its_place(conn):  # noqa: F811
    """R7-1 keeps `admitted_for`: the signal a start admitted in place of a
    switched-off one, and only that -- never a signal for itself."""
    _relay_call_then_vip_copy(conn)
    assert relay_repo.admitted_for(conn, 1) is None

    _restart(conn, on=(VIP,), seconds=40)

    assert relay_repo.admitted_for(conn, 1) == 2
    assert relay_repo.admitted_for(conn, 2) is None
    assert relay_repo.chain_ids(conn, 2) == relay_repo.chain_ids(conn, 1) == [1, 2]


@pytest.mark.parametrize("relay_first", [False, True], ids=["the VIP first", "the relay first"])
@SWITCHED
def test_the_vip_and_the_relay_back_on_saying_close_half_give_one_instruction(
    conn, switched, relay_first  # noqa: F811
):
    """The re-check's r61_relay_back_on (fuzz seed 42969; R7-2). With the
    relay back on, the VIP says "close half" replying to its copy, the relay
    says it replying to its call a few seconds apart, and the VIP forwards
    the relay's post. With both channels read that is ONE instruction; so it
    is once the relay is back on: the relay's post is about signal 2 too, so
    the relay rules take it for a copy of the VIP's, or the VIP's for a copy
    of it. No WARNING asks for it to be applied by hand."""
    _relay_call_then_vip_copy(conn)
    if switched:
        _relay_off_then_back_on(conn, off_at=31, on_at=33)
    traded = 2 if switched else 1
    vip = lambda c: _ingest(c, VIP, 4005, CLOSE_HALF, posted=_at(75), reply_to=VIP_COPY)  # noqa: E731
    relay = lambda c: _ingest(c, RELAY, RELAY_CLOSE, CLOSE_HALF, posted=_at(81), reply_to=RELAY_CALL)  # noqa: E731
    for say in (relay, vip) if relay_first else (vip, relay):
        say(conn)
    _ingest(conn, VIP, 4006, CLOSE_HALF, posted=_at(113), fwd=(R, RELAY_CLOSE))

    first = (R, RELAY_CLOSE) if relay_first else (V, 4005)
    assert _acting_on(conn, traded) == [first], _followups(conn)
    assert _followup_rows(conn, 1) == QUEUED_TWICE
    assert [row[1:3] for row in _followups(conn)] == [first]
    assert _about_off(conn) == []


# -- a post on a channel switched off keeps no call standing (R4-5) ---------


def _claimable(conn) -> list[int]:  # noqa: F811
    return [
        row[0]
        for row in _q(
            conn,
            "SELECT DISTINCT o.signal_id FROM outbox o JOIN signals s USING (signal_id) "
            "WHERE o.kind = 'ENTRY' AND o.state = 'QUEUED' AND s.state = 'NEW' "
            "AND s.withdrawn_at_utc IS NULL ORDER BY 1",
        )
    ]


def _the_relays_held_back_forward(conn) -> None:  # noqa: F811
    """Seed 98259 reduced (the re-check's p_off_support2): the VIP's call
    (signal 1), deleted; the relay posts it (held back, a copy of the
    withdrawn call) and forwards its own post; it deletes the post, keeping
    the forward. The relay is switched off and watch restarts."""
    assert _ingest(conn, VIP, VIP_COPY).signal_id == 1
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=VIP_COPY, now_utc=_at(10), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()
    assert _ingest(conn, RELAY, 7006, posted=_at(20)).relay_of_signal_id == 1
    assert _ingest(conn, RELAY, 7007, posted=_at(25), fwd=(R, 7006)).relay_of_signal_id == 1
    edits_repo.apply_deletion(
        conn, channel_cfg=RELAY, message_id=7006, now_utc=_at(30), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()
    _restart(conn, on=(VIP,), seconds=35)


@pytest.mark.parametrize("by", ["event", "window"])
@pytest.mark.parametrize(
    ("read", "withdrawn"),
    [((V,), True), ((V, R), False), (None, False)],
    ids=["the relay switched off", "the relay read", "no channel set given"],
)
def test_a_forward_on_a_channel_switched_off_keeps_no_call_standing(
    conn, by, read, withdrawn  # noqa: F811
):
    """After the restart the VIP forwards the relay's deleted post: the call
    posted again, signal 2, which the relay's forward repeats. The VIP then
    deletes its forward. It echoes the relay's deleted post -- but its
    removal withdraws the call unless another post of it still stands, and
    the relay's forward, on a channel watch no longer reads, counts for
    nothing: nothing would see it go (INV-1, "a channel this watch reads").
    Signal 2 is withdrawn, not left to trade. With the relay read (or no
    channel set given, as the store's own callers do) the relay's forward
    still shows the call, and signal 2 stands."""
    _the_relays_held_back_forward(conn)
    again = _ingest(conn, VIP, 4009, posted=_at(40), fwd=(R, 7006))
    assert again.signal_id == 2, again
    assert _link(conn, RELAY, 7007) == 2

    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4009, now_utc=_at(50), accounts=ACCOUNTS,
        detected_by=by, read_channels=read,
    )
    conn.commit()

    assert _claimable(conn) == ([] if withdrawn else [2])
    (state,) = _q(conn, "SELECT state, withdrawn_at_utc IS NOT NULL FROM signals WHERE signal_id = 2")
    assert (state[0] != "NEW" or bool(state[1])) is withdrawn, state


def test_an_edit_away_on_a_channel_read_counts_only_the_channels_read(conn):  # noqa: F811
    """The same through an edit: the VIP edits its forward into chatter."""
    _the_relays_held_back_forward(conn)
    assert _ingest(conn, VIP, 4009, posted=_at(40), fwd=(R, 7006)).signal_id == 2

    edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=4009, text="good morning traders",
        posted_at_utc=_at(40), edit_date_utc=_at(50), reply_to_message_id=None,
        now_utc=_at(50), accounts=ACCOUNTS, detected_by="event", read_channels=(V,),
    )
    conn.commit()

    assert _claimable(conn) == []


@pytest.mark.parametrize(
    ("read", "says"), [((V,), 4009), ((V, R), 4010)], ids=["the relay switched off", "the relay read"]
)
def test_a_repeat_deleted_with_the_signals_post_leaves_the_saying_to_it_when_it_withdraws(
    conn, read, says  # noqa: F811
):
    """The VIP's forward (signal 2) and its own repeat of the call, deleted
    in one report, the repeat's job first. The repeat leaves the saying to
    the signal's own post when that post's removal will withdraw the call
    (`edits_repo._holder_speaks`): with the relay switched off it will --
    the relay's forward keeps nothing standing -- so the WARNING names the
    signal's own message. With the relay read, the forward's removal is an
    echo of the relay's, and the repeat says it."""
    _the_relays_held_back_forward(conn)
    assert _ingest(conn, VIP, 4009, posted=_at(40), fwd=(R, 7006)).signal_id == 2
    assert _ingest(conn, VIP, 4010, posted=_at(45)).relay_of_signal_id == 2

    for message_id in (4010, 4009):
        edits_repo.apply_deletion(
            conn, channel_cfg=VIP, message_id=message_id, now_utc=_at(50), accounts=ACCOUNTS,
            detected_by="event", also_deleted={4009, 4010} - {message_id}, read_channels=read,
        )
        conn.commit()

    assert _claimable(conn) == []
    assert _q(
        conn, "SELECT message_id FROM alerts WHERE code = ?", edits_repo.SIGNAL_VOIDED_BY_DELETION
    )[-1] == (says,)


GONE_WAYS = ["deleted", "edited away", "edited, read again", "deleted before it was stored"]


@pytest.mark.parametrize("how", GONE_WAYS)
@pytest.mark.parametrize("relay_on", [False, True], ids=["relay off", "relay on"])
def test_watch_counts_only_the_channels_it_reads_when_a_post_goes(
    tmp_path: Path, relay_on: bool, how: str
) -> None:
    """Seed 98259 through the listener: watch reads only the VIP. The VIP's
    forward of the relay's deleted post is the call posted again (signal 2)
    and goes -- deleted, edited into chatter (the event, or a history read
    finding the new version), or deleted before watch stored it: signal 2
    is withdrawn. The relay's forward still shows the call, but watch does
    not read that channel. With the relay switched on, its forward still
    stands and signal 2 is kept."""
    from test_listener import ACCOUNTS as LISTENER_ACCOUNTS
    from test_listener import _build_listener
    from xaucopier.ingest.listener import IncomingMessage

    db_path = tmp_path / "store.db"
    relay = ChannelCfg(
        channel_id=R, name=RELAY.name, parser="mmh", ordinal=RELAY.ordinal, enabled=relay_on
    )
    writer = db.connect(db_path, role="writer")
    try:
        for channel, message_id, seconds, fwd in (
            (VIP, VIP_COPY, 0, None),
            (RELAY, 7006, 20, None),
            (RELAY, 7007, 25, (R, 7006)),
        ):
            messages_repo.ingest(
                writer, channel_cfg=channel, message_id=message_id, text=SIGNAL_TEXT,
                posted_at_utc=_at(seconds), edit_date_utc=None, reply_to_message_id=None,
                source="live", now_utc=_at(seconds + 1), accounts=LISTENER_ACCOUNTS,
                fwd_from_channel_id=None if fwd is None else fwd[0],
                fwd_from_post_id=None if fwd is None else fwd[1],
                fwd_from_date_utc=None if fwd is None else _at(20),
            )
            if message_id == VIP_COPY:
                edits_repo.apply_deletion(
                    writer, channel_cfg=VIP, message_id=VIP_COPY, now_utc=_at(10),
                    accounts=LISTENER_ACCOUNTS, detected_by="event",
                )
        edits_repo.apply_deletion(
            writer, channel_cfg=RELAY, message_id=7006, now_utc=_at(30),
            accounts=LISTENER_ACCOUNTS, detected_by="event",
        )
        writer.commit()
    finally:
        writer.close()

    def forward(text: str = SIGNAL_TEXT, edited: bool = False) -> IncomingMessage:
        return IncomingMessage(
            channel_id=V, message_id=4009, text=text, posted_at_utc=_at(40),
            edit_date_utc=_at(50) if edited else None, reply_to_message_id=None,
            fwd_from_channel_id=R, fwd_from_post_id=7006, fwd_from_date_utc=_at(20),
        )

    async def scenario():
        listener = _build_listener(
            db_path, FakeTelegramClient(), channels=(VIP, relay), accounts=LISTENER_ACCOUNTS,
        )
        await listener.start()
        try:
            if how == "deleted before it was stored":
                await listener.ingest_deletion(VIP, 4009, detected_by="event")
            again = await listener.ingest_message(forward(), channel_cfg=VIP, source="live")
            assert again.signal_id == 2, again
            if how == "deleted":
                await listener.ingest_deletion(VIP, 4009, detected_by="event")
            elif how == "edited away":
                await listener.ingest_edit(
                    forward("good morning traders", edited=True), channel_cfg=VIP,
                    detected_by="event",
                )
            elif how == "edited, read again":
                await listener.ingest_message(
                    forward("good morning traders", edited=True), channel_cfg=VIP, source="poll"
                )
        finally:
            await listener.aclose()

    asyncio.run(scenario())

    check = db.connect(db_path, role="reader")
    try:
        claimable = _claimable(check)
    finally:
        check.close()
    assert claimable == ([2] if relay_on else [])


# -- a call no post on a channel switched on shows (final fix round 5, R5-1) -

SHOWN_OFF = relay_repo.SIGNAL_SHOWN_ONLY_ON_CHANNEL_OFF
CANCELLED_TEXT = SIGNAL_TEXT + "\nCANCELLED"


def _the_relays_call_kept_only_by_the_vips_forward(  # noqa: F811
    conn, *, delete_before_restart: bool, on=(RELAY,), before_restart=None, gone="deleted",
    second_forward: bool | str = False, restart_at: float = 100, before_gone=None,
):
    """Seed 102253 reduced (the re-check's p_102253): the relay's call
    (signal 1) is CANCELLED; the VIP posts the call (held back, a copy of
    the withdrawn call); the relay forwards the VIP's post: the call posted
    again, signal 2; the VIP forwards its own post and edits that post into
    chatter -- its forward still shows the call. Then the relay deletes its
    forward, signal 2's own post (or edits it into chatter, `gone`): with
    both channels read that is an echo, the VIP's forward still standing --
    before the restart, or after it."""
    assert _ingest(conn, RELAY, 5002).signal_id == 1
    edits_repo.apply_edit(
        conn, channel_cfg=RELAY, message_id=5002, text=CANCELLED_TEXT, posted_at_utc=POSTED,
        edit_date_utc=_at(10), reply_to_message_id=None, now_utc=_at(10), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()
    assert _ingest(conn, VIP, 1002, posted=_at(20)).relay_of_signal_id == 1
    assert _ingest(conn, RELAY, 5005, posted=_at(30), fwd=(V, 1002)).signal_id == 2
    forwards = [(1005, 40), (1006, 45)] if second_forward else [(1005, 40)]
    if second_forward == "newer stored first":
        forwards.reverse()
    for message_id, seconds in forwards:
        assert _ingest(
            conn, VIP, message_id, posted=_at(seconds), fwd=(V, 1002)
        ).relay_of_signal_id == 2
    edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=1002, text="Good morning traders",
        posted_at_utc=_at(20), edit_date_utc=_at(50), reply_to_message_id=None,
        now_utc=_at(50), accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()

    def delete(seconds: float, read) -> None:
        if gone == "deleted":
            edits_repo.apply_deletion(
                conn, channel_cfg=RELAY, message_id=5005, now_utc=_at(seconds),
                accounts=ACCOUNTS, detected_by="window", read_channels=read,
            )
        else:
            edits_repo.apply_edit(
                conn, channel_cfg=RELAY, message_id=5005,
                text=REAIMED if gone == "re-aimed" else "Good morning traders",
                posted_at_utc=_at(30), edit_date_utc=_at(seconds), reply_to_message_id=None,
                now_utc=_at(seconds), accounts=ACCOUNTS, detected_by="event",
                read_channels=read,
            )
        conn.commit()

    if before_gone is not None:
        before_gone(conn)
    if delete_before_restart:
        delete(60, (V, R))
        assert _claimable(conn) == [2]  # an echo: the VIP's forward stands
    if before_restart is not None:
        before_restart(conn)
    _restart(conn, on=on, seconds=restart_at)
    if not delete_before_restart:
        delete(restart_at + 10, (R,))


@pytest.mark.parametrize("gone", ["deleted", "edited away"])
@pytest.mark.parametrize(
    "delete_before_restart", [True, False], ids=["p_102253", "p_102253_after"]
)
def test_a_call_only_a_switched_off_channels_post_shows_is_not_traded_in_either_order(
    conn, delete_before_restart, gone  # noqa: F811
):
    """Only the relay is switched on at the restart. The one post still
    showing signal 2's call is the VIP's forward, on a channel watch no
    longer reads: nothing would see it go. Signal 2 is withdrawn -- VOIDED,
    its entries cancelled, the relay's deletion the post that withdrew it --
    whether watch saw that deletion before the restart or after it."""
    _the_relays_call_kept_only_by_the_vips_forward(
        conn, delete_before_restart=delete_before_restart, gone=gone
    )

    assert _claimable(conn) == []
    assert _q(
        conn,
        "SELECT state, withdrawn_at_utc IS NOT NULL, closed_by_channel_id, closed_by_message_id "
        "FROM signals WHERE signal_id = 2",
    ) == [("VOIDED", 1, R, 5005)]
    assert _q(
        conn, "SELECT DISTINCT state FROM outbox WHERE kind = 'ENTRY' AND signal_id = 2"
    ) == [("SKIPPED",)]
    codes = [a[1] for a in _alerts(conn)]
    if delete_before_restart:
        (alert,) = [a for a in _alerts(conn) if a[1] == SHOWN_OFF]
        assert alert[:5] == ("WARNING", SHOWN_OFF, R, 5005, 2)
        detail = " ".join(alert[5].split())
        how = "was deleted" if gone == "deleted" else "was edited and no longer shows the call"
        assert detail == (
            f"signal 2 VOIDED: its own post, message 5005 on channel {R} (Relay), {how}, "
            f"and the call stood only in message 1005 on channel {V}, which is switched off "
            "now: this program no longer reads that channel, so it could not see that post "
            "withdrawn too. No post on a channel that is switched on shows the call any more, "
            "so it is not traded: 2 queued trade entries were cancelled. Nothing had been "
            "executed. Nothing to do."
        ), detail
        for jargon in ("client", "listener", "rpc", "flood", "outbox"):
            assert jargon not in detail.lower(), (jargon, detail)
    else:
        assert SHOWN_OFF not in codes
        assert (
            edits_repo.SIGNAL_VOIDED_BY_DELETION
            if gone == "deleted"
            else edits_repo.SIGNAL_RETRACTED_BY_EDIT
        ) in codes


@pytest.mark.parametrize("stored", ["older stored first", "newer stored first"])
def test_the_withdrawal_at_start_names_the_oldest_post_on_the_channel_switched_off(
    conn, stored  # noqa: F811
):
    """Final fix round 6 (R6-3): with two posts of the call still standing
    on the channel switched off -- the VIP forwarded its post twice -- the
    alert names the older one, whichever was stored first (final fix round
    7, R7-5 e: the newer one stored first too)."""
    _the_relays_call_kept_only_by_the_vips_forward(
        conn, delete_before_restart=True, second_forward=stored
    )

    assert _claimable(conn) == []
    (alert,) = [a for a in _alerts(conn) if a[1] == SHOWN_OFF]
    detail = " ".join(alert[5].split())
    assert f"the call stood only in message 1005 on channel {V}, which is switched off" in detail
    assert "1006" not in detail, detail


def test_a_call_partly_traded_is_withdrawn_loudly_and_left_open(conn):  # noqa: F811
    """The same, but one account had already traded signal 2: it is
    withdrawn -- the other account's entry cancelled -- yet left NEW, its
    open trade not closed, and the alert is CRITICAL, as when its last post
    is removed while watch reads it (`edits_repo`)."""
    def done_first(conn):  # noqa: F811
        conn.execute(
            "UPDATE outbox SET state = 'DONE' WHERE outbox_id = (SELECT MIN(outbox_id) "
            "FROM outbox WHERE kind = 'ENTRY' AND signal_id = 2)"
        )
        conn.commit()

    _the_relays_call_kept_only_by_the_vips_forward(
        conn, delete_before_restart=True, before_restart=done_first
    )

    assert _claimable(conn) == []
    assert _q(conn, "SELECT state, withdrawn_at_utc IS NOT NULL FROM signals WHERE signal_id = 2") == [
        ("NEW", 1)
    ]
    assert _q(
        conn, "SELECT state FROM outbox WHERE kind = 'ENTRY' AND signal_id = 2 ORDER BY outbox_id"
    ) == [("DONE",), ("SKIPPED",)]
    (alert,) = [a for a in _alerts(conn) if a[1] == SHOWN_OFF]
    assert alert[0] == "CRITICAL"
    detail = " ".join(alert[5].split())
    assert "But it is already traded: its open trades were NOT closed -- review them by hand. "         "1 queued trade entry was cancelled." in detail, detail

def test_a_waiting_signal_no_post_shows_is_withdrawn_at_start_too(conn):  # noqa: F811
    """Should a signal still wait to be traded with its own post gone and
    no other post of its call standing anywhere -- marked deleted with
    nothing else done, say -- the start withdraws it as well, and says no
    post shows the call (INV-1: a call no post shows is never traded)."""
    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    conn.execute(
        "UPDATE messages SET deleted_at_utc = ? WHERE channel_id = ? AND message_id = ?",
        (_at(20).isoformat(), R, RELAY_CALL),
    )
    conn.commit()

    _restart(conn, on=(RELAY,), seconds=30)

    assert _claimable(conn) == []
    (alert,) = [a for a in _alerts(conn) if a[1] == SHOWN_OFF]
    assert (
        f"its own post, message {RELAY_CALL} on channel {R} (Relay), was deleted, and no other "
        "post on a channel that is switched on shows the call, so it is not traded"
    ) in " ".join(alert[5].split())


OLD_SHOWN_OFF = relay_repo.OLD_SIGNALS_SHOWN_ONLY_ON_CHANNEL_OFF
TWO_DAYS = 2 * 86400


def test_the_withdrawal_at_start_re_reads_a_signals_post_re_aimed_since(conn):  # noqa: F811
    """Final fix round 6 (R6-2): only a signal whose own post may no longer
    stand is re-read -- deleted, no longer stored as a signal, or edited. A
    post re-aimed to another call is still stored as a signal: its edit is
    what has it re-read, and signal 2 is withdrawn all the same."""
    assert _ingest(conn, RELAY, 5002).signal_id == 1
    edits_repo.apply_edit(
        conn, channel_cfg=RELAY, message_id=5002, text=CANCELLED_TEXT, posted_at_utc=POSTED,
        edit_date_utc=_at(10), reply_to_message_id=None, now_utc=_at(10), accounts=ACCOUNTS,
        detected_by="event",
    )
    assert _ingest(conn, VIP, 1002, posted=_at(20)).relay_of_signal_id == 1
    assert _ingest(conn, RELAY, 5005, posted=_at(30), fwd=(V, 1002)).signal_id == 2
    assert _ingest(conn, VIP, 1005, posted=_at(40), fwd=(V, 1002)).relay_of_signal_id == 2
    edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=1002, text="Good morning traders",
        posted_at_utc=_at(20), edit_date_utc=_at(50), reply_to_message_id=None,
        now_utc=_at(50), accounts=ACCOUNTS, detected_by="event",
    )
    edits_repo.apply_edit(
        conn, channel_cfg=RELAY, message_id=5005, text=REAIMED, posted_at_utc=_at(30),
        edit_date_utc=_at(60), reply_to_message_id=None, now_utc=_at(60), accounts=ACCOUNTS,
        detected_by="event", read_channels=(V, R),
    )
    conn.commit()
    assert _claimable(conn) == [2]  # an echo: the VIP's forward stands
    assert _q(conn, "SELECT kind FROM messages WHERE channel_id = ? AND message_id = 5005", R) == [
        ("signal",)
    ]

    _restart(conn, on=(RELAY,), seconds=100)

    assert _claimable(conn) == []
    (alert,) = [a for a in _alerts(conn) if a[1] == SHOWN_OFF]
    assert "was edited and no longer shows the call" in " ".join(alert[5].split())


def _three_waiting_calls_whose_posts_are_gone(conn) -> None:  # noqa: F811
    """Three of the relay's calls (three trades) wait to be traded with
    their posts gone and no other post of them standing."""
    for message_id, stop in ((7001, "4470"), (7002, "4469"), (7003, "4468")):
        text = SIGNAL_TEXT.replace("4470", stop)
        assert _ingest(conn, RELAY, message_id, text).outcome == "new"
    conn.execute(
        "UPDATE messages SET deleted_at_utc = ? WHERE channel_id = ?", (_at(20).isoformat(), R)
    )
    conn.commit()


def test_old_calls_no_post_shows_are_withdrawn_at_start_in_one_line(conn):  # noqa: F811
    """Final fix round 6 (R6-2): calls posted longer ago than a signal may
    be and still be traded are withdrawn all the same, but ONE WARNING for
    the whole start says so -- not one per old call, as `_say_too_old` says
    the old copies it leaves."""
    _three_waiting_calls_whose_posts_are_gone(conn)

    _restart(conn, on=(RELAY,), seconds=TWO_DAYS)

    assert _claimable(conn) == []
    assert _q(conn, "SELECT signal_id, state, withdrawn_at_utc IS NOT NULL, closed_by_message_id "
                    "FROM signals ORDER BY 1") == [
        (1, "VOIDED", 1, 7001), (2, "VOIDED", 1, 7002), (3, "VOIDED", 1, 7003)
    ]
    assert SHOWN_OFF not in [a[1] for a in _alerts(conn)]
    (alert,) = [a for a in _alerts(conn) if a[1] == OLD_SHOWN_OFF]
    assert alert[:5] == ("WARNING", OLD_SHOWN_OFF, None, None, None)
    assert " ".join(alert[5].split()) == (
        "3 signals (1, 2, 3) were waiting to be traded, but their own posts had been deleted, "
        "or edited so that they no longer show the call, and no post on a channel that is "
        "switched on shows the call any more (a post on a channel switched off now is no "
        "longer read). They were posted more than 120 seconds ago, too old to be traded "
        "anyway. They are VOIDED: 6 queued trade entries were cancelled. Nothing had been "
        "executed. Nothing to do."
    )


def test_an_old_call_only_a_switched_off_channels_post_shows_is_said_in_one_line(
    conn,  # noqa: F811
):
    """The re-check's p51_states, "restart two days later": signal 2 is
    still withdrawn, and said in the one line for old calls."""
    _the_relays_call_kept_only_by_the_vips_forward(
        conn, delete_before_restart=True, restart_at=TWO_DAYS
    )

    assert _claimable(conn) == []
    assert _q(conn, "SELECT state, withdrawn_at_utc IS NOT NULL FROM signals WHERE signal_id = 2") == [
        ("VOIDED", 1)
    ]
    codes = [a[1] for a in _alerts(conn)]
    assert SHOWN_OFF not in codes
    (alert,) = [a for a in _alerts(conn) if a[1] == OLD_SHOWN_OFF]
    assert alert[4] == 2
    assert " ".join(alert[5].split()).startswith(
        "signal 2 was waiting to be traded, but its own post had been deleted"
    )


def test_an_old_call_partly_traded_keeps_its_own_critical(conn):  # noqa: F811
    """An old call one account already traded is no call to sum up: its
    open trade was not closed, and its CRITICAL says so on its own."""
    _three_waiting_calls_whose_posts_are_gone(conn)
    conn.execute(
        "UPDATE outbox SET state = 'DONE' WHERE outbox_id = (SELECT MIN(outbox_id) "
        "FROM outbox WHERE kind = 'ENTRY' AND signal_id = 2)"
    )
    conn.commit()

    _restart(conn, on=(RELAY,), seconds=TWO_DAYS)

    (critical,) = [a for a in _alerts(conn) if a[1] == SHOWN_OFF]
    assert critical[0] == "CRITICAL" and critical[4] == 2
    (summary,) = [a for a in _alerts(conn) if a[1] == OLD_SHOWN_OFF]
    assert "2 signals (1, 3) were waiting" in " ".join(summary[5].split())


def _waiting_calls_whose_posts_are_gone(conn, count: int) -> None:  # noqa: F811
    """`count` of the relay's calls (each another trade: another stop) wait
    to be traded with their posts gone and no other post of them standing."""
    for n in range(count):
        text = SIGNAL_TEXT.replace("4470", str(4470 - n))
        assert _ingest(conn, RELAY, 7001 + n, text).outcome == "new"
    conn.execute(
        "UPDATE messages SET deleted_at_utc = ? WHERE channel_id = ?", (_at(20).isoformat(), R)
    )
    conn.commit()


def test_a_call_too_old_to_trade_but_not_ten_times_so_is_summed_up(conn):  # noqa: F811
    """Final fix round 7 (R7-5 b): "too old to be traded" is the policy's
    age itself -- a call posted 500 s before the start, past the 120 s a
    signal may be and still be traded, is summed up in the one line, not
    given a WARNING of its own."""
    _waiting_calls_whose_posts_are_gone(conn, 1)

    _restart(conn, on=(RELAY,), seconds=500)

    assert _claimable(conn) == []
    assert SHOWN_OFF not in [a[1] for a in _alerts(conn)]
    (alert,) = [a for a in _alerts(conn) if a[1] == OLD_SHOWN_OFF]
    assert " ".join(alert[5].split()).startswith(
        "signal 1 was waiting to be traded, but its own post had been deleted"
    ), alert


def test_the_policys_age_decides_what_is_too_old_at_start(conn):  # noqa: F811
    """R7-5 c: with policy.toml allowing a signal 300 s, a call posted 200 s
    before the start could still be traded: it gets its own WARNING, not the
    line for calls too old to be traded anyway."""
    _waiting_calls_whose_posts_are_gone(conn, 1)

    _restart(conn, on=(RELAY,), seconds=200, max_age_seconds=300)

    assert _claimable(conn) == []
    assert OLD_SHOWN_OFF not in [a[1] for a in _alerts(conn)]
    (alert,) = [a for a in _alerts(conn) if a[1] == SHOWN_OFF]
    assert alert[:5] == ("WARNING", SHOWN_OFF, R, 7001, 1)


def test_the_line_for_old_calls_names_ten_and_counts_the_rest(conn):  # noqa: F811
    """R7-5 d: twelve old calls -- the one line names the first ten and says
    how many more, as `_say_too_old` does."""
    _waiting_calls_whose_posts_are_gone(conn, 12)

    _restart(conn, on=(RELAY,), seconds=TWO_DAYS)

    (alert,) = [a for a in _alerts(conn) if a[1] == OLD_SHOWN_OFF]
    assert " ".join(alert[5].split()).startswith(
        "12 signals (1, 2, 3, 4, 5, 6, 7, 8, 9, 10 and 2 more) were waiting to be traded"
    ), alert
    assert "24 queued trade entries were cancelled" in alert[5], alert


def test_the_withdrawal_at_start_re_reads_no_post_left_as_it_was(conn):  # noqa: F811
    """R6-2, the cost: 2000 signals of one channel stored over the weeks,
    their entries waiting for ever in watch-only Plan 2, none of their posts
    deleted or edited. The start-up job's look at them stays well under
    0.2 s: none is re-read."""
    import time

    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    for n in range(2, 2001):
        conn.execute(
            "INSERT INTO messages (channel_id, message_id, kind, content_hash, raw_text, "
            "  edit_date_utc, received_at_utc, posted_at_utc, source) "
            "SELECT channel_id, ?, kind, content_hash, raw_text, edit_date_utc, "
            "  received_at_utc, ?, source FROM messages WHERE message_id = ?",
            (RELAY_CALL + n, _at(n * 1000).isoformat(), RELAY_CALL),
        )
        conn.execute(
            "INSERT INTO signals (channel_id, message_id, parsed_json, trade_hash, state) "
            "SELECT channel_id, ?, parsed_json, trade_hash, state FROM signals WHERE signal_id = 1",
            (RELAY_CALL + n,),
        )
        conn.execute(
            "INSERT INTO outbox (account_id, kind, signal_id, state) "
            "SELECT account_id, kind, ?, state FROM outbox WHERE signal_id = 1",
            (n,),
        )
    conn.commit()
    assert _q(conn, "SELECT COUNT(*) FROM signals") == [(2000,)]

    started = time.perf_counter()
    raised = relay_repo._withdraw_calls_shown_only_off(
        conn, on={R: RELAY}, now_utc=_at(2001 * 1000)
    )
    took = time.perf_counter() - started

    assert raised == []
    assert took < 0.2, took


def test_the_withdrawal_at_start_re_reads_an_edited_post_once_per_edit(conn):  # noqa: F811
    """Final fix round 7 (R7-6), the re-check's r62_perf_edited: 2000 calls
    of one channel whose posts were edited -- "TP1 hit" added -- but still
    show the call. The first start re-reads each one; a later start skips
    every post not edited since, so its look stays well under 0.2 s."""
    import time

    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    for n in range(2, 2001):
        conn.execute(
            "INSERT INTO messages (channel_id, message_id, kind, content_hash, raw_text, "
            "  edit_date_utc, received_at_utc, posted_at_utc, source) "
            "SELECT channel_id, ?, kind, content_hash, raw_text, edit_date_utc, "
            "  received_at_utc, ?, source FROM messages WHERE message_id = ?",
            (RELAY_CALL + n, _at(n * 1000).isoformat(), RELAY_CALL),
        )
        conn.execute(
            "INSERT INTO signals (channel_id, message_id, parsed_json, trade_hash, state) "
            "SELECT channel_id, ?, parsed_json, trade_hash, state FROM signals WHERE signal_id = 1",
            (RELAY_CALL + n,),
        )
        conn.execute(
            "INSERT INTO outbox (account_id, kind, signal_id, state) "
            "SELECT account_id, kind, ?, state FROM outbox WHERE signal_id = 1",
            (n,),
        )
    conn.execute(
        "UPDATE messages SET raw_text = raw_text || ?, edit_date_utc = ?",
        ("\nTP1 hit", _at(3000 * 1000).isoformat()),
    )
    conn.commit()
    now = _at(2001 * 1000)
    assert relay_repo._withdraw_calls_shown_only_off(conn, on={R: RELAY}, now_utc=now) == []
    conn.commit()

    started = time.perf_counter()
    raised = relay_repo._withdraw_calls_shown_only_off(conn, on={R: RELAY}, now_utc=now)
    took = time.perf_counter() - started

    assert raised == []
    assert took < 0.2, took
    assert _claimable(conn) == list(range(1, 2001))


@pytest.mark.parametrize("gone", ["edited away", "re-aimed"])
def test_a_post_re_read_at_one_start_is_re_read_again_once_edited_since(
    conn, gone  # noqa: F811
):
    """R7-6: what a start remembers is the edit it re-read. Signal 2's own
    post was edited -- still showing the call -- and a start (both channels
    read) re-read it; then it was edited away, or re-aimed to another call
    (still stored as a signal), while the VIP's forward still showed the
    call (an echo, so signal 2 stood). At the next start, with the VIP
    switched off, that newer edit is re-read and signal 2 withdrawn -- as
    with no start in between."""
    def tp1_hit_and_a_start(conn):  # noqa: F811
        edits_repo.apply_edit(
            conn, channel_cfg=RELAY, message_id=5005, text=SIGNAL_TEXT + "\nTP1 hit",
            posted_at_utc=_at(30), edit_date_utc=_at(55), reply_to_message_id=None,
            now_utc=_at(55), accounts=ACCOUNTS, detected_by="event", read_channels=(V, R),
        )
        conn.commit()
        _restart(conn, on=(RELAY, VIP), seconds=56)
        assert _claimable(conn) == [2]

    _the_relays_call_kept_only_by_the_vips_forward(
        conn, delete_before_restart=True, gone=gone, before_gone=tp1_hit_and_a_start
    )
    assert _q(conn, "SELECT kind FROM messages WHERE channel_id = ? AND message_id = 5005", R) == [
        ("signal" if gone == "re-aimed" else "skip",)
    ]

    assert _claimable(conn) == []
    (alert,) = [a for a in _alerts(conn) if a[1] == SHOWN_OFF]
    assert alert[:5] == ("WARNING", SHOWN_OFF, R, 5005, 2)


def test_the_withdrawal_at_start_looks_only_at_signals_of_channels_switched_on(
    conn,  # noqa: F811
):
    """Called before the cancelling (as no start does), a signal of a channel
    switched off whose post is gone is passed over, not withdrawn as if its
    channel were read: cancelling its entries is the other half of the job's
    work, with its own alert."""
    assert _ingest(conn, VIP, VIP_COPY).signal_id == 1
    conn.execute(
        "UPDATE messages SET deleted_at_utc = ? WHERE channel_id = ? AND message_id = ?",
        (_at(20).isoformat(), V, VIP_COPY),
    )
    conn.commit()

    raised = relay_repo.admit_copies_of_switched_off(
        conn, signal_ids=(), channels=(RELAY,), accounts=ACCOUNTS, now_utc=_at(30)
    )

    assert raised == ()
    assert _claimable(conn) == [1]

def test_a_call_still_shown_on_a_channel_switched_on_is_kept(conn):  # noqa: F811
    """The control: the VIP is switched on at the restart too. Its forward
    is read, still shows the call, and signal 2 stands."""
    _the_relays_call_kept_only_by_the_vips_forward(
        conn, delete_before_restart=True, on=(RELAY, VIP)
    )

    assert _claimable(conn) == [2]
    assert SHOWN_OFF not in [a[1] for a in _alerts(conn)]


@pytest.mark.parametrize("seen", ["before the restart", "after the restart"])
def test_watch_trades_no_call_only_a_switched_off_channels_post_shows(
    tmp_path: Path, seen: str
) -> None:
    """Seed 102253 through the listener: the first watch reads both
    channels, the second only the relay. Whether the relay's deletion of
    signal 2's own post was seen before the restart or after it, signal 2
    is withdrawn: the one post still showing its call is on the VIP's
    channel, which the second watch does not read."""
    from test_listener import ACCOUNTS as LISTENER_ACCOUNTS
    from test_listener import _build_listener
    from xaucopier.ingest.listener import IncomingMessage

    db_path = tmp_path / "store.db"
    vip_off = ChannelCfg(
        channel_id=V, name=VIP.name, parser="mmh", ordinal=VIP.ordinal, enabled=False
    )

    def post(channel, message_id, seconds, text=SIGNAL_TEXT, fwd=None, edited=None):
        return IncomingMessage(
            channel_id=channel.channel_id, message_id=message_id, text=text,
            posted_at_utc=_at(seconds), edit_date_utc=None if edited is None else _at(edited),
            reply_to_message_id=None,
            fwd_from_channel_id=None if fwd is None else fwd[0],
            fwd_from_post_id=None if fwd is None else fwd[1],
            fwd_from_date_utc=None if fwd is None else _at(20),
        )

    async def first_watch():
        listener = _build_listener(
            db_path, FakeTelegramClient(), channels=(VIP, RELAY), accounts=LISTENER_ACCOUNTS
        )
        await listener.start()
        try:
            await listener.ingest_message(post(RELAY, 5002, 0), channel_cfg=RELAY, source="live")
            await listener.ingest_edit(
                post(RELAY, 5002, 0, CANCELLED_TEXT, edited=10), channel_cfg=RELAY,
                detected_by="event",
            )
            await listener.ingest_message(post(VIP, 1002, 20), channel_cfg=VIP, source="live")
            again = await listener.ingest_message(
                post(RELAY, 5005, 30, fwd=(V, 1002)), channel_cfg=RELAY, source="live"
            )
            assert again.signal_id == 2, again
            await listener.ingest_message(
                post(VIP, 1005, 40, fwd=(V, 1002)), channel_cfg=VIP, source="live"
            )
            await listener.ingest_edit(
                post(VIP, 1002, 20, "Good morning traders", edited=50), channel_cfg=VIP,
                detected_by="event",
            )
            if seen == "before the restart":
                await listener.ingest_deletion(RELAY, 5005, detected_by="event")
        finally:
            await listener.aclose()

    async def second_watch():
        listener = _build_listener(
            db_path, FakeTelegramClient(), channels=(vip_off, RELAY), accounts=LISTENER_ACCOUNTS
        )
        await listener.start()
        try:
            if seen == "after the restart":
                await listener.ingest_deletion(RELAY, 5005, detected_by="event")
        finally:
            await listener.aclose()

    asyncio.run(first_watch())
    check = db.connect(db_path, role="reader")
    try:
        assert _claimable(check) == [2]  # an echo while the VIP's forward is read
    finally:
        check.close()
    asyncio.run(second_watch())

    check = db.connect(db_path, role="reader")
    try:
        claimable = _claimable(check)
        (state,) = _q(check, "SELECT state FROM signals WHERE signal_id = 2")
    finally:
        check.close()
    assert claimable == []
    assert state == ("VOIDED",)


def test_watch_trades_no_copy_on_a_channel_switched_off_too(tmp_path: Path) -> None:
    """Final fix round 4 (R4-6; the re-check's p_r31_listener_channels):
    through `Listener.start()`, the relay AND the VIP switched off, another
    channel on. The VIP's copy of the relay's call is young enough to be
    traded, but its channel is off as well: nothing is a signal, nothing is
    queued -- the start-up job looks only at the channels switched on."""
    from test_listener import ACCOUNTS as LISTENER_ACCOUNTS
    from test_listener import _build_listener

    db_path = tmp_path / "store.db"

    def off(channel: ChannelCfg) -> ChannelCfg:
        return ChannelCfg(
            channel_id=channel.channel_id, name=channel.name, parser="mmh",
            ordinal=channel.ordinal, enabled=False,
        )

    writer = db.connect(db_path, role="writer")
    try:
        # test_listener's clock stands at POSTED + 900 s: the copy is 70 s old.
        for channel, message_id, seconds in ((RELAY, RELAY_CALL, 800), (VIP, VIP_COPY, 830)):
            messages_repo.ingest(
                writer, channel_cfg=channel, message_id=message_id, text=SIGNAL_TEXT,
                posted_at_utc=_at(seconds), edit_date_utc=None, reply_to_message_id=None,
                source="live", now_utc=_at(seconds + 1), accounts=LISTENER_ACCOUNTS,
            )
        writer.commit()
    finally:
        writer.close()

    async def scenario():
        listener = _build_listener(
            db_path, FakeTelegramClient(), channels=(off(VIP), off(RELAY), OTHER),
            accounts=LISTENER_ACCOUNTS,
        )
        await listener.start()
        await listener.aclose()

    asyncio.run(scenario())

    check = db.connect(db_path, role="reader")
    try:
        signals, queued = _signals(check), _queued(check)
        codes = [row[1] for row in _alerts(check)]
    finally:
        check.close()
    assert signals == [(1, R, RELAY_CALL, "NEW")]
    assert queued == []
    assert codes == ["CHANNEL_OFF_ENTRIES_SKIPPED"]


@SWITCHED
def test_fuzz_seed_42969_the_vip_read_while_the_relay_is_off_then_the_relay_is_one_instruction(
    conn, switched  # noqa: F811
):
    """The re-check's fuzz seed 42969 (R7-2). With the relay switched off
    at a restart, the VIP says "close half" replying to its copy -- the
    instruction about signal 2. The relay is switched back on at the next
    restart, and its "close half" replying to its call is read then; later
    the VIP forwards the relay's post. With both channels read that is ONE
    instruction; so it is with the relay off and back on -- the relay's
    post is about signal 2, a copy of the VIP's -- and nothing asks for an
    instruction by hand."""
    _relay_call_then_vip_copy(conn)
    if switched:
        _restart(conn, on=(VIP,), seconds=91)
    _ingest(conn, VIP, 4005, CLOSE_HALF, posted=_at(95), reply_to=VIP_COPY)
    if switched:
        _restart(conn, on=(VIP, RELAY), seconds=106)
    relay = _ingest(conn, RELAY, RELAY_CLOSE, CLOSE_HALF, posted=_at(95), reply_to=RELAY_CALL)
    forward = _ingest(conn, VIP, 4006, CLOSE_HALF, posted=_at(192), fwd=(R, RELAY_CLOSE))

    traded = 2 if switched else 1
    assert _acting_on(conn, traded) == [(V, 4005)]
    assert _followup_rows(conn, 1) == QUEUED_TWICE
    assert [row[:3] for row in _followups(conn)] == [(1, V, 4005)]
    assert (relay.relay_of_followup_id, forward.relay_of_followup_id) == (1, 1)
    assert ABOUT_OFF not in [a[1] for a in _alerts(conn)]


def test_a_call_admitted_twice_matches_an_instruction_to_the_one_switched_off_last(
    conn,  # noqa: F811
):
    """R7-1 with two admissions in one call. The relay's call, the VIP's and
    the other channel's copies; a start with the relay switched off admits
    the other channel's copy (signal 2), whose "close half" replying to it
    is the instruction about signal 2; a later start with that channel
    switched off (the relay back on) admits the VIP's copy (signal 3). The
    VIP's "close half" replying to its copy then gives the instruction of
    signal 2's -- the call switched off last, not the relay's -- so it
    stands in for it: when the relay forwards the other channel's post, that
    is the same instruction acting on signal 3 already. Nothing to do --
    never "apply it by hand" while one acts."""
    _relay_call_then_vip_copy(conn)
    assert _ingest(conn, OTHER, OTHER_COPY, posted=_at(35)).relay_of_signal_id == 1
    _restart(conn, on=(VIP, OTHER), seconds=40)
    other = _ingest(conn, OTHER, 9005, CLOSE_HALF, posted=_at(60), reply_to=OTHER_COPY)
    assert other.followup_id == 1, other
    _restart(conn, on=(RELAY, VIP), seconds=65)
    assert relay_repo.live_signal_for(conn, 1) == 3
    assert relay_repo.admitted_for(conn, 2) == 3

    vip = _ingest(conn, VIP, 4005, CLOSE_HALF, posted=_at(70), reply_to=VIP_COPY)
    relay = _ingest(conn, RELAY, 7009, CLOSE_HALF, posted=_at(80), fwd=(OTHER.channel_id, 9005))

    assert vip.followup_id == 2, vip
    assert _q(conn, "SELECT instead_of_followup_id FROM followups WHERE followup_id = 2") == [(1,)]
    assert _followup_rows(conn, 2) == QUEUED_TWICE
    assert relay.relay_of_followup_id == 2, relay
    (alert,) = [a for a in _alerts(conn) if a[1] == ABOUT_OFF]
    flat = " ".join(alert[5].split())
    assert flat.endswith(
        f"Signal 3 trades that call instead, and the same instruction already acts on it as "
        f"follow-up 2 (message 4005 on channel {V}). Nothing to do."
    ), flat


# -- final fix round 8 ---------------------------------------------------------

SL4460 = SIGNAL_TEXT.replace("4470", "4460")


def _vip_call_withdrawn_then_posted_again_as_a_copy(conn) -> None:  # noqa: F811
    """The re-check's p2b_in_order (R8-1). The VIP posts the call with
    another stop (signal 1), the relay posts it (signal 2), the VIP moves
    its stop to the relay's and deletes its post (signal 1 VOIDED), then
    posts it again (4002, a copy of signal 2: a live call wins). The relay
    says "close half" (7005) and the VIP forwards it (4003)."""
    assert _ingest(conn, VIP, 4001, SL4460, posted=_at(0)).signal_id == 1
    assert _ingest(conn, RELAY, RELAY_CALL, posted=_at(20)).signal_id == 2
    edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=4001, text=SIGNAL_TEXT, posted_at_utc=_at(0),
        edit_date_utc=_at(30), reply_to_message_id=None, now_utc=_at(30), accounts=ACCOUNTS,
        detected_by="event", read_channels=(V, R),
    )
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4001, now_utc=_at(40), accounts=ACCOUNTS,
        detected_by="event", read_channels=(V, R),
    )
    conn.commit()
    assert _ingest(conn, VIP, 4002, posted=_at(50)).relay_of_signal_id == 2
    assert _ingest(conn, RELAY, RELAY_CLOSE, CLOSE_HALF, posted=_at(55), reply_to=RELAY_CALL).followup_id == 1
    assert _ingest(conn, VIP, 4003, CLOSE_HALF, posted=_at(57), fwd=(R, RELAY_CLOSE)).relay_of_followup_id == 1


@pytest.mark.parametrize(
    "switched", [False, True], ids=["both channels read", "relay switched off"]
)
def test_a_signal_admitted_and_replacing_its_own_call_keeps_one_instruction(
    conn, switched  # noqa: F811
):
    """The re-check's p2b_in_order (final fix round 8, R8-1). The start
    with the relay switched off admits the VIP's 4002 as signal 3 -- in
    place of the relay's signal 2 AND replacing the VIP's own withdrawn
    signal 1 (both links recorded). The VIP's forward 4003 stands in for the
    relay's "close half" about signal 3. Then the VIP says "close half"
    replying to 4002, near the relay's post: with both channels read that
    is a copy of the relay's instruction -- ONE instruction. So it is with
    the relay switched off: a copy of the one acting on signal 3, never a
    second "close half" on it."""
    _vip_call_withdrawn_then_posted_again_as_a_copy(conn)
    if switched:
        _restart(conn, on=(VIP,), seconds=60)
        assert _q(conn, "SELECT promoted_from_signal_id, admitted_from_signal_id FROM signals "
                        "WHERE signal_id = 3") == [(1, 2)]
    traded = 3 if switched else 2

    again = _ingest(conn, VIP, 4004, CLOSE_HALF, posted=_at(65), reply_to=4002)

    assert again.followup_id is None, again
    assert again.relay_of_followup_id == (2 if switched else 1), again
    assert _claimable(conn) == [traded]
    acting = [(1, 2)] if not switched else [(2, 2)]
    assert [
        (fid, sum(1 for _, state in _followup_rows(conn, fid) if state == "QUEUED"))
        for fid, *_ in _followups(conn)
        if relay_repo.signal_for_followup(conn, followup_id=fid) == traded
    ] == acting
    if switched:
        (alert,) = [a for a in _alerts(conn) if a[1] == ABOUT_OFF]
        assert alert[3] == 4004
        assert " ".join(alert[5].split()).endswith(
            f"Signal 3 trades that call instead, and the same instruction already acts on it as "
            f"follow-up 2 (message 4003 on channel {V}). Nothing to do."
        ), alert


@pytest.mark.parametrize(
    "switched", [False, True], ids=["both channels read", "relay switched off"]
)
def test_a_follow_up_kept_with_the_withdrawn_call_an_admission_replaced_is_no_match(
    conn, switched  # noqa: F811
):
    """R8-1, the cut before every admission (`relay_repo._segments`). The
    VIP's call with another stop (signal 1), the relay's (signal 2); the VIP
    says "close half" replying to its call and the relay forwards that
    post. The VIP moves its stop to the relay's, deletes its call (signal
    1 VOIDED, its "close half" kept with it) and posts it again (4002, a
    copy of signal 2). With the relay switched off at a start, 4002 is
    signal 3 -- replacing signal 1 and admitted in place of signal 2. The
    VIP's "close half" replying to 4002 is then an instruction of its own
    about signal 3, as it is about signal 2 with both channels read -- never
    a copy of the one kept with the withdrawn signal 1, which acts on
    nothing (nothing would close half)."""
    assert _ingest(conn, VIP, 4001, SL4460, posted=_at(0)).signal_id == 1
    assert _ingest(conn, RELAY, RELAY_CALL, posted=_at(20)).signal_id == 2
    assert _ingest(conn, VIP, 4010, CLOSE_HALF, posted=_at(25), reply_to=4001).followup_id == 1
    assert _ingest(conn, RELAY, 7010, CLOSE_HALF, posted=_at(27), fwd=(V, 4010)).relay_of_followup_id == 1
    edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=4001, text=SIGNAL_TEXT, posted_at_utc=_at(0),
        edit_date_utc=_at(30), reply_to_message_id=None, now_utc=_at(30), accounts=ACCOUNTS,
        detected_by="event", read_channels=(V, R),
    )
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4001, now_utc=_at(40), accounts=ACCOUNTS,
        detected_by="event", read_channels=(V, R),
    )
    conn.commit()
    assert _ingest(conn, VIP, 4002, posted=_at(50)).relay_of_signal_id == 2
    if switched:
        _restart(conn, on=(VIP,), seconds=60)
        assert relay_repo._segment_of(conn, 3) == [3]
    traded = 3 if switched else 2

    again = _ingest(conn, VIP, 4004, CLOSE_HALF, posted=_at(65), reply_to=4002)

    assert (again.outcome, again.followup_id, again.relay_of_followup_id) == ("new", 2, None)
    assert _claimable(conn) == [traded]
    assert _acting_on(conn, traded) == [(V, 4004)]
    assert _followup_rows(conn, 2) == QUEUED_TWICE
    assert relay_repo.signal_for_followup(conn, followup_id=1) == 1


def test_a_call_kept_at_one_start_by_another_channel_is_withdrawn_at_the_next_without_it(
    conn,  # noqa: F811
):
    """The re-check's p7_gone_kept_then_off (final fix round 8, R8-5; a test
    gap of R7-6). The relay re-aims its forward 5005 -- signal 2's own post,
    still stored as a signal -- while the VIP's forward 1005 still shows the
    call (an echo: signal 2 stands). A start with both channels on keeps
    signal 2, as the call stands on a channel read -- but its own post no
    longer stands, so that start must not mark the post re-read. The next
    start, with only the relay on, withdraws signal 2: no post on a channel
    switched on shows the call, as when no start came between."""
    _the_relays_call_kept_only_by_the_vips_forward(
        conn, delete_before_restart=True, gone="re-aimed", on=(VIP, RELAY)
    )
    assert _claimable(conn) == [2]
    assert _q(conn, "SELECT reread_edit_date_utc FROM signals WHERE signal_id = 2") == [(None,)]

    _restart(conn, on=(RELAY,), seconds=202)

    assert _claimable(conn) == []
    assert _q(
        conn, "SELECT state, withdrawn_at_utc IS NOT NULL FROM signals WHERE signal_id = 2"
    ) == [("VOIDED", 1)]


@pytest.mark.parametrize(
    "switched", [False, True], ids=["both channels read", "relay switched off"]
)
def test_a_reply_to_the_call_posted_again_after_the_admitted_one_was_withdrawn_is_one(
    conn, switched  # noqa: F811
):
    """R8-1, `relay_repo._switched_off_part`: the admission that names the
    switched-off call is the FIRST signal of its part of the call. The
    relay's "close half" and the VIP's forward of it; with the relay
    switched off at a start, the VIP's copy is signal 2 and the forward
    acts on it. The VIP deletes its copy (signal 2 withdrawn) and posts the
    call again: signal 3 replaces signal 2, the forward's instruction moves
    to it. The VIP's "close half" replying to its new post, near the
    relay's, repeats that instruction: one, as with both channels read
    (where the deletion withdraws signal 1, and the relay's instruction
    moves to signal 2, the call posted again)."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    on = (VIP,) if switched else (VIP, RELAY)
    if switched:
        _restart(conn, on=on, seconds=80)
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=VIP_COPY, now_utc=_at(85), accounts=ACCOUNTS,
        detected_by="event", read_channels=tuple(c.channel_id for c in on),
    )
    conn.commit()
    _ingest(conn, VIP, 4030, posted=_at(90))
    traded = 3 if switched else 2  # the VIP's deletion withdrew the call, either way
    assert _claimable(conn) == [traded]

    again = _ingest(conn, VIP, 4031, CLOSE_HALF, posted=_at(95), reply_to=4030)

    assert again.followup_id is None, again
    assert _acting_on(conn, traded) == [(V, FWD) if switched else (R, RELAY_CLOSE)]
    if switched:
        assert again.relay_of_followup_id == 2, again
        (alert,) = [a for a in _alerts(conn) if a[1] == ABOUT_OFF]
        assert " ".join(alert[5].split()).endswith(
            f"Signal 3 trades that call instead, and the same instruction already acts on it as "
            f"follow-up 2 (message {FWD} on channel {V}). Nothing to do."
        ), alert


def _two_admissions_with_instructions(conn) -> None:  # noqa: F811
    """The relay's call, the VIP's and the other channel's copies, and the
    relay's "close half" (follow-up 1). A start with the relay switched off
    admits the other channel's copy (signal 2); the other channel's "close
    half" replying to it, near the relay's, stands in for follow-up 1 about
    signal 2 (follow-up 2). A later start with the other channel switched
    off and the relay back on admits the VIP's copy (signal 3)."""
    _relay_call_then_vip_copy(conn)
    assert _ingest(conn, OTHER, OTHER_COPY, posted=_at(35)).relay_of_signal_id == 1
    _the_relays_close_half(conn, seconds=38)
    _restart(conn, on=(VIP, OTHER), seconds=40)
    other = _ingest(conn, OTHER, 9005, CLOSE_HALF, posted=_at(60), reply_to=OTHER_COPY)
    assert other.followup_id == 2, other
    assert _q(conn, "SELECT instead_of_followup_id FROM followups WHERE followup_id = 2") == [(1,)]
    _restart(conn, on=(RELAY, VIP), seconds=65)
    assert relay_repo.live_signal_for(conn, 1) == 3


def test_a_reply_after_two_admissions_stands_in_for_the_one_switched_off_last(
    conn,  # noqa: F811
):
    """R8-1: the part of the call switched off that an admission names is
    that signal's own (`_segment_of`), never the whole call. After the two
    admissions, the VIP's "close half" replying to its copy -- near both
    instructions -- stands in for the other channel's follow-up 2 about
    signal 2, the call switched off last, not for the relay's about signal 1."""
    _two_admissions_with_instructions(conn)

    vip = _ingest(conn, VIP, 4005, CLOSE_HALF, posted=_at(70), reply_to=VIP_COPY)

    assert vip.followup_id == 3, vip
    assert _q(conn, "SELECT instead_of_followup_id FROM followups WHERE followup_id = 3") == [(2,)]
    assert _acting_on(conn, 3) == [(V, 4005)]

def _instructions(conn, signal_id: int) -> list[tuple]:  # noqa: F811
    """The instructions waiting on `signal_id`: (follow-up, op, QUEUED rows)
    of every follow-up acting on it with one queued."""
    found = []
    for followup_id, op in _q(conn, "SELECT followup_id, op FROM followups ORDER BY 1"):
        queued = sum(1 for _, state in _followup_rows(conn, followup_id) if state == "QUEUED")
        if queued and relay_repo.signal_for_followup(conn, followup_id=followup_id) == signal_id:
            found.append((followup_id, op, queued))
    return found


def _the_relay_changes_its_close_half(conn, how: str, *, seconds: float = 100) -> None:  # noqa: F811
    if how == "deleted":
        edits_repo.apply_deletion(
            conn, channel_cfg=RELAY, message_id=RELAY_CLOSE, now_utc=_at(seconds),
            accounts=ACCOUNTS, detected_by="event", read_channels=(V, R),
        )
    else:
        edits_repo.apply_edit(
            conn, channel_cfg=RELAY, message_id=RELAY_CLOSE,
            text="Close all now" if how == "close all" else "good morning traders",
            posted_at_utc=_at(60), edit_date_utc=_at(seconds), reply_to_message_id=RELAY_CALL,
            now_utc=_at(seconds), accounts=ACCOUNTS, detected_by="event", read_channels=(V, R),
        )
    conn.commit()


@SWITCHED
def test_the_relays_edit_of_an_instruction_from_before_the_switch_off_reaches_its_stand_in(
    conn, switched  # noqa: F811
):
    """The re-check's p3, edit (final fix round 8, R8-2). The relay's "close
    half" (follow-up 1) and the VIP's forward of it; the start with the
    relay switched off adopts the forward as follow-up 2, the instruction
    about signal 2 in follow-up 1's place. The relay, back on, edits its
    post into "close all": with both channels read that is ONE instruction,
    "close all", on the call -- and so it is on signal 2, the call traded:
    follow-up 2 takes the corrected instruction, never two."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    if switched:
        _relay_off_then_back_on(conn, off_at=80, on_at=90)
    traded = 2 if switched else 1

    _the_relay_changes_its_close_half(conn, "close all")

    assert _claimable(conn) == [traded]
    assert _instructions(conn, traded) == [(2 if switched else 1, "close_all", 2)]
    if switched:
        (json_,) = _q(conn, "SELECT parsed_json FROM followups WHERE followup_id = 2")
        assert '"op": "close_all"' in json_[0] and '"reply_to_message_id": null' in json_[0]


@SWITCHED
@pytest.mark.parametrize("how", ["deleted", "edited away"])
def test_the_relays_withdrawal_of_an_instruction_from_before_the_switch_off_withdraws_its_stand_in(
    conn, switched, how  # noqa: F811
):
    """The re-check's p3, delete (R8-2). As above, but the relay, back on,
    deletes its "close half" or edits it into chatter: with both channels
    read the instruction is withdrawn -- and so is follow-up 2, the one
    acting on signal 2 in its place. The alert says so, naming signal 2."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    if switched:
        _relay_off_then_back_on(conn, off_at=80, on_at=90)
    traded = 2 if switched else 1

    _the_relay_changes_its_close_half(conn, how)

    assert _claimable(conn) == [traded]
    assert _instructions(conn, traded) == []
    code = "FOLLOWUP_DELETED" if how == "deleted" else "FOLLOWUP_RETRACTED_BY_EDIT"
    (alert,) = [a for a in _alerts(conn) if a[1] == code]
    flat = " ".join(alert[5].split())
    assert flat.startswith("follow-up 1 withdrawn"), flat
    if switched:
        assert alert[4] == 2, alert
        assert (
            f"Follow-up 2 (message {FWD} on channel {V}) gives the same instruction about "
            f"signal 2, which trades that call instead: it is withdrawn with it, 2 queued "
            f"instruction(s) skipped; 0 already taken by a worker, which is not undone."
        ) in flat, flat
        assert _followup_rows(conn, 2) == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]


def test_the_relays_withdrawal_reaches_the_stand_in_of_its_stand_in(conn):  # noqa: F811
    """R8-2 after two admissions: the other channel's "close half" stands in
    for the relay's about signal 2, and the VIP's for the other channel's
    about signal 3, the call traded. The relay, back on, deletes its post:
    with every channel read the one instruction is withdrawn, so both
    stand-ins are, and nothing closes half on signal 3."""
    _two_admissions_with_instructions(conn)
    vip = _ingest(conn, VIP, 4005, CLOSE_HALF, posted=_at(70), reply_to=VIP_COPY)
    assert vip.followup_id == 3, vip

    _the_relay_changes_its_close_half(conn, "deleted")

    assert _claimable(conn) == [3]
    assert _instructions(conn, 3) == []
    (alert,) = [a for a in _alerts(conn) if a[1] == "FOLLOWUP_DELETED"]
    assert alert[4] == 3, alert
    assert "Follow-up 3 (message 4005 on channel" in alert[5], alert


def test_the_relays_edit_reaches_the_stand_in_of_its_stand_in(conn):  # noqa: F811
    """R8-2 after two admissions, the edit: the VIP's stand-in acting on
    signal 3 takes the relay's corrected instruction."""
    _two_admissions_with_instructions(conn)
    assert _ingest(conn, VIP, 4005, CLOSE_HALF, posted=_at(70), reply_to=VIP_COPY).followup_id == 3

    _the_relay_changes_its_close_half(conn, "close all")

    assert _instructions(conn, 3) == [(3, "close_all", 2)]


def _the_vip_changes_its_forward(conn, how: str, *, read, seconds: float = 100) -> None:  # noqa: F811
    if how == "deleted":
        edits_repo.apply_deletion(
            conn, channel_cfg=VIP, message_id=FWD, now_utc=_at(seconds), accounts=ACCOUNTS,
            detected_by="event", read_channels=read,
        )
    else:
        edits_repo.apply_edit(
            conn, channel_cfg=VIP, message_id=FWD,
            text="Close all now" if how == "close all" else "good morning traders",
            posted_at_utc=_at(70), edit_date_utc=_at(seconds), reply_to_message_id=None,
            now_utc=_at(seconds), accounts=ACCOUNTS, detected_by="event", read_channels=read,
        )
    conn.commit()


@SWITCHED
@pytest.mark.parametrize("how", ["deleted", "edited away", "close all"])
def test_the_vips_stand_in_withdrawn_hands_the_instruction_back_to_the_relays_post(
    conn, switched, how  # noqa: F811
):
    """The re-check's p3b (final fix round 8, R8-3). The VIP's forward of
    the relay's "close half" stands in for it about signal 2; the relay is
    switched back on. The VIP then deletes its forward, edits it into
    chatter or into "close all", while the relay's post still says "close
    half": with both channels read the forward was only a copy of it, and
    "close half" still acts on the call. So it does on signal 2 -- the
    relay's own follow-up 1 acts on it now -- and the alert says so."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    if switched:
        _relay_off_then_back_on(conn, off_at=80, on_at=90)
    traded = 2 if switched else 1

    _the_vip_changes_its_forward(conn, how, read=(V, R))

    assert _claimable(conn) == [traded]
    assert _instructions(conn, traded) == [(1, "close_half", 2)]
    if switched:
        assert relay_repo.signal_for_followup(conn, followup_id=1) == 2
        assert _followup_rows(conn, 2) == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]
        (alert,) = [
            a for a in _alerts(conn)
            if a[1] in ("FOLLOWUP_DELETED", "FOLLOWUP_RETRACTED_BY_EDIT", "FOLLOWUP_COPY_CHANGED")
        ]
        assert alert[4] == 2, alert
        flat = " ".join(alert[5].split())
        assert (
            f"The same instruction still stands in message {RELAY_CLOSE} on channel {R} "
            f"(follow-up 1), on a channel this program reads: it acts on signal 2 in this one's "
            f"place now, 2 instruction(s) queued."
        ) in flat, flat


@pytest.mark.parametrize("how", ["deleted", "edited away"])
def test_the_vips_stand_in_withdrawn_while_the_relay_is_off_is_withdrawn(
    conn, how  # noqa: F811
):
    """R8-3's other side: while the relay is still switched off its post
    counts for nothing, so the VIP withdrawing its forward withdraws the
    instruction about signal 2 -- nothing is handed back."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    _restart(conn, on=(VIP,), seconds=80)

    _the_vip_changes_its_forward(conn, how, read=(V,))

    assert _claimable(conn) == [2]
    assert _instructions(conn, 2) == []
    assert relay_repo.signal_for_followup(conn, followup_id=1) == 1
    assert not any("The same instruction still stands" in a[5] for a in _alerts(conn))


@pytest.mark.parametrize("how", ["deleted", "edited away"])
def test_a_forward_of_an_instruction_its_poster_withdrew_is_nothing_to_do(
    conn, how  # noqa: F811
):
    """The re-check's p5 and fuzz seed 70665 (final fix round 8, R8-4). The
    relay says "close half" and withdraws it -- deletes it, or edits it into
    chatter -- while both channels are read. A start with the relay switched
    off admits the VIP's copy as signal 2; the VIP then forwards the relay's
    post. With both channels read that is a copy of an instruction
    withdrawn: the WARNING says so and ends "Nothing to do" -- never "apply
    it by hand"."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _the_relay_changes_its_close_half(
        conn, "deleted" if how == "deleted" else "chatter", seconds=62
    )
    (withdrawal,) = _q(
        conn, "SELECT alert_id FROM alerts WHERE code IN "
        "('FOLLOWUP_DELETED', 'FOLLOWUP_RETRACTED_BY_EDIT')",
    )
    _restart(conn, on=(VIP,), seconds=65)

    copy = _fwd(conn)

    assert copy.relay_of_followup_id == 1, copy
    assert _instructions(conn, 2) == []
    (alert,) = [a for a in _alerts(conn) if a[1] == ABOUT_OFF]
    flat = " ".join(alert[5].split())
    assert flat.endswith(
        f"Message {RELAY_CLOSE} was withdrawn by its poster (alert #{withdrawal[0]}). "
        "Nothing to do."
    ), flat
    assert relay_repo.BY_HAND not in flat
    assert alert[4] == 1, alert


def test_a_forward_of_an_instruction_withdrawn_once_back_on_is_nothing_to_do(
    conn,  # noqa: F811
):
    """R8-4 with R8-2: the VIP's forward stood in for the relay's "close
    half" about signal 2; the relay, back on, deletes its post -- the
    forward is withdrawn with it. The VIP forwarding the relay's post again
    is a copy of an instruction its poster withdrew: nothing to do."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    _relay_off_then_back_on(conn, off_at=80, on_at=90)
    _the_relay_changes_its_close_half(conn, "deleted")

    copy = _fwd(conn, message_id=FWD2, seconds=110)

    assert copy.followup_id is None, copy
    assert _instructions(conn, 2) == []
    (alert,) = [a for a in _alerts(conn) if a[1] == ABOUT_OFF]
    assert " ".join(alert[5].split()).endswith("was withdrawn by its poster (alert #2). Nothing to do.")


@pytest.mark.parametrize("how", ["deleted", "close all"])
def test_a_change_to_an_instruction_handed_over_by_hand_says_to_review_it(
    conn, how  # noqa: F811
):
    """The by-hand side of R8-2 (round 7's open item, fuzz seed 70802). The
    relay's "close half" was read before the start that switched it off;
    the VIP's forward of it, read after that start, was not applied and the
    user was told to apply it by hand on signal 2. The relay, back on, then
    deletes its post or edits it into "close all": the alert names that
    request and says to review what was done by hand on signal 2."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _restart(conn, on=(VIP,), seconds=65)
    assert _fwd(conn).relay_of_followup_id == 1
    (asked,) = [a for a in _alerts(conn) if a[1] == ABOUT_OFF]
    assert relay_repo.BY_HAND in asked[5]
    (asked_id,) = _q(conn, "SELECT alert_id FROM alerts WHERE code = ?", ABOUT_OFF)[0]
    _restart(conn, on=(VIP, RELAY), seconds=80)

    _the_relay_changes_its_close_half(conn, how)

    code = "FOLLOWUP_DELETED" if how == "deleted" else "FOLLOWUP_EDITED_AFTER_DISPATCH"
    (alert,) = [a for a in _alerts(conn) if a[1] == code]
    flat = " ".join(alert[5].split())
    assert alert[4] == 2, alert
    assert (
        f"It was handed to you to apply by hand on signal 2 (alert #{asked_id})"
    ) in flat, flat
    assert "review that" in flat, flat
    if how == "close all":
        assert "it now says 'close the whole trade'" in flat, flat


def test_a_stand_in_handed_back_keeps_the_relays_follow_up_as_a_reply_to_another_post(
    conn,  # noqa: F811
):
    """R8-3: handed back, the relay's follow-up replies to its own post of
    the call -- not signal 2's own message -- so its path is 'relay-reply',
    and its instructions are the ones it had, still queued."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    _relay_off_then_back_on(conn, off_at=80, on_at=90)

    _the_vip_changes_its_forward(conn, "deleted", read=(V, R))

    assert _followups(conn)[0] == (1, R, RELAY_CLOSE, 2, relay_repo.RELAY_REPLY_PATH)
    assert _followup_rows(conn, 1) == QUEUED_TWICE


def test_a_stand_in_withdrawn_after_the_call_traded_was_withdrawn_hands_nothing_back(
    conn,  # noqa: F811
):
    """R8-3 needs a signal still traded: the VIP deleted its copy of the call
    (signal 2 withdrawn) before deleting its forward -- nothing is handed
    back to a call no one trades, and no alert says the instruction acts."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    _relay_off_then_back_on(conn, off_at=80, on_at=90)
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=VIP_COPY, now_utc=_at(95), accounts=ACCOUNTS,
        detected_by="event", read_channels=(V, R),
    )
    conn.commit()

    _the_vip_changes_its_forward(conn, "deleted", read=(V, R))

    assert relay_repo.signal_for_followup(conn, followup_id=1) == 1
    assert not any("The same instruction still stands" in a[5] for a in _alerts(conn))


def test_the_relays_withdrawal_names_no_stand_in_withdrawn_before_nor_a_request_not_made(
    conn,  # noqa: F811
):
    """R8-2: the VIP's forward stood in for the relay's "close half" and the
    VIP's second forward, read once the relay is back on, was "Nothing to
    do" (it acts already). The relay then deletes its post: the stand-in is
    withdrawn with it, and the alert says nothing of a by-hand request --
    none was made."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    _relay_off_then_back_on(conn, off_at=80, on_at=90)
    _fwd(conn, message_id=FWD2, seconds=95)
    (said,) = [a for a in _alerts(conn) if a[1] == ABOUT_OFF]
    assert "Nothing to do" in said[5]

    _the_relay_changes_its_close_half(conn, "deleted")

    (alert,) = [a for a in _alerts(conn) if a[1] == "FOLLOWUP_DELETED"]
    assert "Follow-up 2 (message" in alert[5]
    assert "handed to you" not in alert[5], alert


def test_a_request_by_hand_after_the_stand_in_was_withdrawn_is_named_when_the_relay_withdraws(
    conn,  # noqa: F811
):
    """R8-2, by hand: with the relay switched off the VIP's forward stood in
    for the relay's "close half", and the VIP deleted it; its second forward
    asked for the instruction by hand (a copy of the one withdrawn). With the
    relay back on, its deletion of the post names that request -- and not
    the stand-in, withdrawn before."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    _restart(conn, on=(VIP,), seconds=80)
    _the_vip_changes_its_forward(conn, "deleted", read=(V,), seconds=82)
    _fwd(conn, message_id=FWD2, seconds=85)
    (asked,) = _q(conn, "SELECT alert_id, detail FROM alerts WHERE code = ?", ABOUT_OFF)
    assert relay_repo.BY_HAND in asked[1]
    _restart(conn, on=(VIP, RELAY), seconds=90)

    _the_relay_changes_its_close_half(conn, "deleted")

    (alert,) = [a for a in _alerts(conn) if a[1] == "FOLLOWUP_DELETED" and a[3] == RELAY_CLOSE]
    flat = " ".join(alert[5].split())
    assert f"It was handed to you to apply by hand on signal 2 (alert #{asked[0]})" in flat, flat
    assert "Follow-up 2 (message" not in flat, flat


def test_a_stand_in_taken_by_a_worker_is_said_when_the_relay_changes_the_instruction(
    conn,  # noqa: F811
):
    """R8-2: a worker had taken one of the stand-in's instructions when the
    relay, back on, edits its post into "close all": the stand-in says the
    new instruction, and the WARNING says what was taken is not undone."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    _relay_off_then_back_on(conn, off_at=80, on_at=90)
    conn.execute(
        "UPDATE outbox SET state = 'CLAIMED' WHERE kind = 'FOLLOWUP' AND followup_id = 2 "
        "AND account_id = 'acct-1'"
    )

    _the_relay_changes_its_close_half(conn, "close all")

    assert _q(conn, "SELECT op FROM followups WHERE followup_id = 2") == [("close_all",)]
    (json_,) = _q(conn, "SELECT parsed_json FROM followups WHERE followup_id = 2")[0]
    assert f'"posted_at_utc": "{_at(70).isoformat()}"' in json_, json_
    (alert,) = [a for a in _alerts(conn) if a[1] == "FOLLOWUP_EDITED_AFTER_DISPATCH"]
    assert alert[4] == 2, alert
    assert (
        "Follow-up 2, the same instruction about signal 2, which trades that call instead, was "
        "changed with it after 1 worker(s) had taken it; what they did is not undone."
    ) in " ".join(alert[5].split()), alert


def test_a_stand_in_withdrawn_after_two_admissions_hands_back_to_the_post_still_read(
    conn,  # noqa: F811
):
    """R8-3 after two admissions: the VIP's "close half" stands in for the
    other channel's, which stands in for the relay's. The other channel is
    switched off now, the relay back on; the VIP deletes its post. The
    relay's "close half" still stands on a channel read: with every channel
    read it is the one instruction, so it acts on signal 3, the call traded."""
    _two_admissions_with_instructions(conn)
    assert _ingest(conn, VIP, 4005, CLOSE_HALF, posted=_at(70), reply_to=VIP_COPY).followup_id == 3
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4005, now_utc=_at(100), accounts=ACCOUNTS,
        detected_by="event", read_channels=(V, R),
    )
    conn.commit()

    assert _instructions(conn, 3) == [(1, "close_half", 2)]


def test_a_withdrawn_stand_in_of_a_stand_in_is_never_handed_back(conn):  # noqa: F811
    """R8-3 hands back only an instruction still waiting: after two
    admissions the relay withdrew its "close half" -- both stand-ins with
    it. Then every channel is switched on, and the VIP deletes its post:
    the other channel's post still stands, but its instruction was
    withdrawn with the relay's, so nothing is handed back to it."""
    _two_admissions_with_instructions(conn)
    assert _ingest(conn, VIP, 4005, CLOSE_HALF, posted=_at(70), reply_to=VIP_COPY).followup_id == 3
    _the_relay_changes_its_close_half(conn, "deleted", seconds=90)
    _restart(conn, on=(VIP, RELAY, OTHER), seconds=95)
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4005, now_utc=_at(100), accounts=ACCOUNTS,
        detected_by="event", read_channels=(V, R, OTHER.channel_id),
    )
    conn.commit()

    assert _instructions(conn, 3) == []
    assert relay_repo.signal_for_followup(conn, followup_id=2) == 2
    assert not any("The same instruction still stands" in a[5] for a in _alerts(conn))


def test_a_stand_in_withdrawn_before_keeps_what_it_said_when_the_relay_edits(
    conn,  # noqa: F811
):
    """R8-2 restates only a stand-in still giving its instruction: the
    VIP's forward was withdrawn while the relay was off, and stays the
    "close half" it was when the relay, back on, edits its post."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _fwd(conn)
    _restart(conn, on=(VIP,), seconds=80)
    _the_vip_changes_its_forward(conn, "deleted", read=(V,), seconds=82)
    _restart(conn, on=(VIP, RELAY), seconds=90)

    _the_relay_changes_its_close_half(conn, "close all")

    assert _q(conn, "SELECT op FROM followups ORDER BY 1") == [("close_all",), ("close_half",)]


@pytest.mark.parametrize("switched", [False, True], ids=["both channels read", "relay switched off"])
@pytest.mark.parametrize(
    "how, copy",
    [
        ("deleted", "reply"),
        ("deleted", "forward"),
        ("edited away", "forward"),
        ("edited away and back", "forward"),
    ],
)
def test_a_copy_of_an_instruction_withdrawn_before_the_start_is_never_adopted(
    conn, switched, how, copy  # noqa: F811
):
    """INV-3h seeds 130154, 130190 and 130152 (final fix round 8). The relay
    says "close half" and withdraws it -- deletes it, edits it into chatter,
    or into chatter and back into an instruction (which an edit never
    queues) -- and the VIP's "close half" replying to its copy of the call
    (read after the deletion: a post edited away no longer says it), or its
    forward of the relay's post, was stored as a copy of it. With
    both channels read nothing closes half. So it is after a start with the
    relay switched off: the start adopts no copy of an instruction its
    poster withdrew."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    if how == "deleted":
        _the_relay_changes_its_close_half(conn, "deleted", seconds=62)
    else:
        _the_relay_changes_its_close_half(conn, "chatter", seconds=62)
        if how == "edited away and back":
            _the_relay_changes_its_close_half(conn, "close all", seconds=63)
    vip = _fwd(conn) if copy == "forward" else _rep(conn, seconds=70)
    assert vip.relay_of_followup_id == 1, vip
    if switched:
        _restart(conn, on=(VIP,), seconds=75)
    traded = 2 if switched else 1

    assert _claimable(conn) == [traded]
    assert _instructions(conn, traded) == []


@pytest.mark.parametrize("switched", [False, True], ids=["both channels read", "relay switched off"])
def test_a_reply_after_the_start_near_an_instruction_withdrawn_is_nothing_to_do(
    conn, switched  # noqa: F811
):
    """INV-3h seed 130086 (final fix round 8, with R8-4). The relay says
    "close half" and deletes it. After a start with the relay switched off,
    the VIP says "close half" replying to its copy, near the relay's post:
    with both channels read that is a copy of an instruction withdrawn --
    nothing closes half. So it is with the relay switched off: never an
    instruction of its own standing in for the withdrawn one; the WARNING
    says it was withdrawn by its poster. Nothing to do."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _the_relay_changes_its_close_half(conn, "deleted", seconds=62)
    if switched:
        _restart(conn, on=(VIP,), seconds=65)
    traded = 2 if switched else 1

    again = _rep(conn, seconds=80)

    assert again.followup_id is None, again
    assert _instructions(conn, traded) == []
    if switched:
        (alert,) = [a for a in _alerts(conn) if a[1] == ABOUT_OFF]
        assert " ".join(alert[5].split()).endswith(
            f"Message {RELAY_CLOSE} was withdrawn by its poster (alert #1). Nothing to do."
        ), alert

@SWITCHED
@pytest.mark.parametrize("vip", ["nothing", "its forward, deleted while the relay was off"])
def test_the_relays_instruction_read_before_the_switch_off_acts_once_it_is_back_on(
    conn, switched, vip  # noqa: F811
):
    """INV-3h seed 130184 (final fix round 8, R8-3 at a start). The relay's
    "close half" was read before the start that switched it off, and no
    post of the VIP's stands in for it about signal 2 -- the VIP never
    repeated it, or deleted its forward while the relay was off. With the
    relay back on its post is read again and still says "close half": with
    both channels read that instruction acts on the call, so it acts on
    signal 2, the call traded, from the start that switches the relay back
    on."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    if vip != "nothing":
        _fwd(conn)  # read before the start: it stands in for the relay's there
    if switched:
        _restart(conn, on=(VIP,), seconds=80)
        if vip != "nothing":
            assert _instructions(conn, 2) == [(2, "close_half", 2)]
            _the_vip_changes_its_forward(conn, "deleted", read=(V,), seconds=82)
            assert _instructions(conn, 2) == []
        _restart(conn, on=(VIP, RELAY), seconds=90)
    elif vip != "nothing":
        _the_vip_changes_its_forward(conn, "deleted", read=(V, R), seconds=82)
    traded = 2 if switched else 1

    assert _claimable(conn) == [traded]
    assert _instructions(conn, traded) == [(1, "close_half", 2)]


def test_an_instruction_handed_over_by_hand_is_not_carried_out_again_once_back_on(
    conn,  # noqa: F811
):
    """R8-3 at a start, the other side: the VIP's forward of the relay's
    "close half", read after the start that switched the relay off, was
    handed to the user to apply by hand on signal 2. With the relay back on
    the relay's own follow-up stays with the cancelled signal 1: done by
    hand, it is never carried out a second time."""
    _relay_call_then_vip_copy(conn)
    _the_relays_close_half(conn)
    _restart(conn, on=(VIP,), seconds=80)
    _fwd(conn, seconds=81)
    assert relay_repo.handed_over(conn, 1) is not None

    _restart(conn, on=(VIP, RELAY), seconds=90)

    assert relay_repo.signal_for_followup(conn, followup_id=1) == 1
    assert _instructions(conn, 2) == []


def _the_vips_forward_read_first_and_its_own_reply(conn) -> None:  # noqa: F811
    """INV-3h seed 132612, reduced: the VIP's forward of the relay's "close
    half" is read before the relay's post (follow-up 1, which the relay's
    post is a copy of), and the VIP also says "close half" replying to its
    copy of the call (follow-up 2) -- two posts of one channel, two
    instructions with both channels read."""
    _relay_call_then_vip_copy(conn)
    assert _ingest(conn, VIP, FWD, CLOSE_HALF, posted=_at(95), fwd=(R, RELAY_CLOSE)).followup_id == 1
    relay = _ingest(conn, RELAY, RELAY_CLOSE, CLOSE_HALF, posted=_at(60), reply_to=RELAY_CALL)
    assert relay.relay_of_followup_id == 1, relay
    assert _ingest(conn, VIP, REP, CLOSE_HALF, posted=_at(72), reply_to=VIP_COPY).followup_id == 2


@pytest.mark.parametrize("how", ["deleted", "edited away"])
@pytest.mark.parametrize("switched", [False, True], ids=["both channels read", "relay switched off"])
def test_a_channels_own_instruction_a_start_merged_acts_again_when_the_other_is_withdrawn(
    conn, switched, how  # noqa: F811
):
    """INV-3h seed 132612 (final fix round 8). The start that switches the
    relay off takes the VIP's reply for the instruction about signal 2 in
    place of its forward, read first (round 6's "never two": one channel's
    two posts near another channel's taken for one). The VIP then deletes
    its reply, or edits it into chatter: with both channels read its forward
    still says "close half", so it acts on the call traded -- handed back to
    it, as to a post of another channel (R8-3), and the alert says so. It is
    on the VIP's channel, read: whether the relay is read does not matter."""
    _the_vips_forward_read_first_and_its_own_reply(conn)
    if switched:
        _restart(conn, on=(VIP,), seconds=100)
        assert _instructions(conn, 2) == [(2, "close_half", 2)]
    traded = 2 if switched else 1
    read = (V,) if switched else (V, R)

    if how == "deleted":
        edits_repo.apply_deletion(
            conn, channel_cfg=VIP, message_id=REP, now_utc=_at(105), accounts=ACCOUNTS,
            detected_by="event", read_channels=read,
        )
    else:
        edits_repo.apply_edit(
            conn, channel_cfg=VIP, message_id=REP, text="good morning traders",
            posted_at_utc=_at(72), edit_date_utc=_at(105), reply_to_message_id=VIP_COPY,
            now_utc=_at(105), accounts=ACCOUNTS, detected_by="event", read_channels=read,
        )
    conn.commit()

    assert _claimable(conn) == [traded]
    assert _instructions(conn, traded) == [(1, "close_half", 2)]
    if switched:
        code = "FOLLOWUP_DELETED" if how == "deleted" else "FOLLOWUP_RETRACTED_BY_EDIT"
        (alert,) = [a for a in _alerts(conn) if a[1] == code]
        assert alert[4] == 2, alert
        assert (
            f"The same instruction still stands in message {FWD} on channel {V} (follow-up 1), "
            "on a channel this program reads: it acts on signal 2 in this one's place now, 2 "
            "instruction(s) queued."
        ) in " ".join(alert[5].split()), alert


@pytest.mark.parametrize("switched", [False, True], ids=["both channels read", "relay switched off"])
@pytest.mark.parametrize(
    "text, says",
    [("Close all now", "close_all"), ("Move SL to entry", "sl_to_be"), ("CLOSE HALF NOW", "close_half")],
)
def test_a_channels_own_instruction_a_start_merged_acts_again_when_the_other_says_another(
    conn, switched, text, says  # noqa: F811
):
    """INV-3h seed 132612's edit (final fix round 8). As above, but the VIP
    edits its reply into another instruction. With both channels read the
    forward still says "close half" and the reply says the new one: two
    instructions. So it is after the start: the reply says the new one, and
    the forward acts on signal 2 again beside it -- never "close half" lost
    unseen -- with a WARNING naming both. Edited into the same instruction
    in other words, the two stay the one the start took them for."""
    _the_vips_forward_read_first_and_its_own_reply(conn)
    if switched:
        _restart(conn, on=(VIP,), seconds=100)
    traded = 2 if switched else 1

    edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=REP, text=text, posted_at_utc=_at(72),
        edit_date_utc=_at(105), reply_to_message_id=VIP_COPY, now_utc=_at(105),
        accounts=ACCOUNTS, detected_by="event", read_channels=(V,) if switched else (V, R),
    )
    conn.commit()

    merged = switched and says == "close_half"
    assert _instructions(conn, traded) == (
        [(2, "close_half", 2)] if merged else [(1, "close_half", 2), (2, says, 2)]
    )
    warned = [a for a in _alerts(conn) if a[1] == "FOLLOWUP_EDITED_AFTER_DISPATCH"]
    if not switched or merged:
        assert warned == []
        return
    (alert,) = warned
    assert alert[2:5] == (V, REP, 2), alert
    assert " ".join(alert[5].split()) == (
        f"Follow-up 2 (message {REP} on channel {V}) was edited; it now says "
        f"'{'close the whole trade' if says == 'close_all' else 'move the stop loss to the entry price'}'. "
        f"When this program started it took it for the same instruction as follow-up 1 (message "
        f"{FWD} on channel {V}), which still says 'close half of the trade': two posts of one "
        "channel are two instructions, so follow-up 1 acts on signal 2 too now, 2 "
        "instruction(s) queued. [found by event]"
    ), alert
