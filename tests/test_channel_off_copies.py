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
    newest is the signal; the VIP's copy is outside its window and stays a
    copy of signal 1 -- never a second signal of the same call."""
    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    assert _ingest(conn, VIP, VIP_COPY, posted=_at(-800)).relay_of_signal_id == 1
    assert _ingest(conn, OTHER, OTHER_COPY, posted=_at(800)).relay_of_signal_id == 1

    _restart(conn)

    assert _signals(conn)[1:] == [(2, OTHER.channel_id, OTHER_COPY, "NEW")]
    assert _link(conn, VIP, VIP_COPY) == 1
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


def test_a_held_back_copy_the_traded_call_does_not_repeat_stays_held_back(
    conn,  # noqa: F811
):
    """The VIP's copies are 950 s apart: the +900 s one, held back against
    the withdrawn signal 2, is outside the window of signal 3 (the -50 s
    copy), so by the relay rules it is another call -- it stays held back,
    its alert standing."""
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
    assert _link(conn, VIP, VIP_REPOST) == 2
    assert _q(conn, "SELECT code, acked FROM alerts WHERE code IN (?, ?)", HELD, SUPERSEDED) == [
        (HELD, 0)
    ]


def test_a_reply_to_a_held_back_copy_moves_with_it_to_the_traded_call(conn):  # noqa: F811
    """The VIP's "close half", replying to its +850 s copy, acts on the call
    that copy repeats once it is linked to signal 3."""
    _two_vip_copies_and_a_withdrawn_repost(conn, reposted_by=RELAY)
    _ingest(conn, VIP, 4020, CLOSE_HALF, posted=_at(1020), reply_to=VIP_REPOST)

    _restart(conn, on=(VIP,), seconds=1100, max_age_seconds=LONG_GATE)

    (followup_id,) = _q(conn, "SELECT followup_id FROM followups WHERE message_id = 4020")[0]
    assert relay_repo.signal_for_followup(conn, followup_id=followup_id) == 3
