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


def _restart(conn, *, on=(VIP, OTHER), seconds: float = 120):  # noqa: F811
    """`watch` starts with only `on` switched on: the start-up job, as the
    listener runs it."""
    enabled = tuple(channel.channel_id for channel in on)
    signal_ids = outbox.signals_with_entries_of_channels_off(conn, enabled_channel_ids=enabled)
    skipped = outbox.skip_entries_of_channels_off(conn, enabled_channel_ids=enabled)
    raised = relay_repo.admit_copies_of_switched_off(
        conn, signal_ids=signal_ids, channels=on, accounts=ACCOUNTS, now_utc=_at(seconds)
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

    store(RELAY, RELAY_CALL, 0)
    if restart == "between-the-posts":
        start()
    store(VIP, VIP_COPY, 30)
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
    """The VIP's copy (+800 s) is also within the window of the other
    channel's own signal of the same call (+1000 s; 1000 s after signal 1,
    so a signal of its own), which that channel then withdrew. Run through
    the relay rules again, the VIP's copy repeats that withdrawn call:
    held back, CRITICAL, as when it is first read -- never linked quietly,
    and never a signal."""
    assert _ingest(conn, RELAY, RELAY_CALL).signal_id == 1
    assert _ingest(conn, VIP, VIP_COPY, posted=_at(800)).relay_of_signal_id == 1
    assert _ingest(conn, OTHER, OTHER_COPY, posted=_at(1000)).signal_id == 2
    edits_repo.apply_deletion(
        conn, channel_cfg=OTHER, message_id=OTHER_COPY, now_utc=_at(1010), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()

    _restart(conn, seconds=1100)

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
