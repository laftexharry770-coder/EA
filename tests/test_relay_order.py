"""The relay rules do not depend on which post was stored first
(review round 3 of the relay fixes, rulings R3-P1 to R3-P5).

The code cannot tell the VIP's channel from a relay's: whichever post of
a call is stored first holds the signal, and that is only an accident of
delivery. So every scenario here runs in both storage orders -- the VIP's
post first ("vip-first"), or the relay's copy first ("relay-first", the
relay's copy then holds the signal and the VIP's post is its copy) --
and must give the same outcome for the trade:

- **R3-P1, last standing post**: a channel withdraws a call only when
  its last standing post of it is deleted or edited away; removing one
  of several is a repeat removal (a WARNING; the call stands). Promotion
  only ever happens on the holder's own channel.
- **R3-P2, one live signal per call**: a signal that replaces a
  withdrawn one is linked to it, and every standing post of the call --
  whichever signal it was stored against -- reaches the newest one
  (`relay_repo.live_signal_for`): its edits, deletions and reply
  follow-ups.
- **R3-P3, closing channel only**: a withdrawn call posted again is a
  new signal only on the channel whose last post withdrew it; anywhere
  else it is held back loudly, until that channel posts it again.
- **R3-P4**: a stale post's edit never puts back a level another post of
  the call has moved, whichever post holds the signal.

Unit-level tests drive the store directly; the arrival-path tests at the
bottom run the real listener and poll against the fake Telegram client.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from test_edits import DIRECTION_EDIT, EDIT_1, EDIT_2, SL_EDIT, UNPARSEABLE_EDIT, _round
from test_relay import (
    ACCOUNTS,
    CALL,
    CHATTER_TEXT,
    CLOSE_HALF,
    COPY,
    POSTED,
    RELAY,
    SIGNAL_TEXT,
    TP1_EDIT,
    VIP,
    VIP_CHATTER,
    R,
    REPOST,
    REPOST_2,
    V,
    _alerts,
    _db_rows,
    _edit,
    _ingest,
    _levels,
    _link,
    _mark_executed,
    _post,
    _q,
    _queued_entries,
    _run,
    conn,  # noqa: F401 -- the fixture
)
from xaucopier.store import edits_repo, outbox, relay_repo

ORDERS = ["vip-first", "relay-first"]
SL_4480 = SIGNAL_TEXT.replace("4470", "4480")


def _store_call(conn, order: str, *, relay_posts=(7001,), forwards: bool = False) -> None:  # noqa: F811
    """The VIP's call V/4001 and the relay's copies of it, 30 s apart:
    VIP-first, or with the relay's first copy stored before the VIP's
    post (it then holds the signal)."""

    def relay(index: int) -> None:
        _ingest(
            conn, RELAY, relay_posts[index], posted=POSTED + timedelta(seconds=30 * (index + 1)),
            fwd=(V, CALL) if forwards else None,
        )

    if order == "vip-first":
        _ingest(conn, VIP, CALL)
        for index in range(len(relay_posts)):
            relay(index)
    else:
        relay(0)
        _ingest(conn, VIP, CALL)
        for index in range(1, len(relay_posts)):
            relay(index)
    holder = (V, CALL) if order == "vip-first" else (R, relay_posts[0])
    assert _q(conn, "SELECT channel_id, message_id FROM signals") == [holder]


def _live(conn) -> list[int]:
    """The signals still carrying a call: NEW and not withdrawn."""
    return [
        row[0]
        for row in _q(
            conn,
            "SELECT signal_id FROM signals WHERE state = 'NEW' AND withdrawn_at_utc IS NULL "
            "ORDER BY signal_id",
        )
    ]


def _delete(conn, channel, message_id: int, at, *, by: str = "event", also=()):
    result = edits_repo.apply_deletion(
        conn, channel_cfg=channel, message_id=message_id, now_utc=at, accounts=ACCOUNTS,
        detected_by=by, also_deleted=also,
    )
    conn.commit()
    return result


def _take_entry(conn, signal_id: int) -> None:
    """Plan 3's worker, simulated: acct-1's ENTRY of `signal_id` is DONE."""
    conn.execute(
        "UPDATE outbox SET state = 'DONE' WHERE kind = 'ENTRY' AND account_id = 'acct-1' "
        "AND signal_id = ?",
        (signal_id,),
    )
    conn.commit()


def _entries_claimable(conn, account_id: str = "acct-2") -> list[int]:
    """Every ENTRY `account_id` can still claim (its signal ids); claims
    everything claimable on the way."""
    entries = []
    while (row := outbox.claim(
        conn, account_id=account_id, pid=1, now_utc=POSTED + timedelta(hours=1)
    )) is not None:
        if row.kind == "ENTRY":
            entries.append(row.signal_id)
    conn.commit()
    return entries


def _codes(conn) -> list[tuple[str, str]]:
    return [(level, code) for level, code, *_ in _q(
        conn, "SELECT level, code FROM alerts ORDER BY alert_id"
    )]


def _detail(conn, code: str) -> str:
    (row,) = _q(conn, "SELECT detail FROM alerts WHERE code = ?", code)
    return row[0]


def _the_vip_manages_the_trade(conn, live: int) -> None:
    """After acct-1 took `live`'s entry, the VIP's own post V/4001 still
    manages it: its "close half", its stop move and its deletion all reach
    `live`, whichever signal the post was stored against."""
    _take_entry(conn, live)
    close = _ingest(
        conn, VIP, 4005, CLOSE_HALF, posted=POSTED + timedelta(minutes=5), reply_to=CALL
    )
    assert close.outcome == "new"
    assert _q(conn, "SELECT target_signal_id FROM followups WHERE message_id = 4005") == [(live,)]

    moved = _edit(conn, VIP, CALL, SL_EDIT, EDIT_1)
    conn.commit()
    assert (moved.outcome, moved.signal_id) == ("reparsed", live)
    assert _q(
        conn, "SELECT op, target_signal_id FROM followups WHERE edit_seq > 0"
    ) == [("amend_sl", live)]
    assert _q(
        conn, "SELECT json_extract(parsed_json, '$.sl') FROM signals WHERE signal_id = ?", live
    ) == [(4460.0,)]

    gone = _delete(conn, VIP, CALL, POSTED + timedelta(minutes=7))
    assert (gone.outcome, gone.signal_id) == ("flagged", live)
    assert ("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED, V, CALL, live) in _alerts(conn)
    assert _entries_claimable(conn) == []  # acct-2's entry is skipped, the call withdrawn


# -- R3-P1: a relay tidying up one of two copies (review round 2, H3) -----------------


@pytest.mark.parametrize("copies", ["hand", "forward"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_relay_removing_one_of_its_two_copies_leaves_the_vip_managing_the_trade(
    conn, order, copies  # noqa: F811
):
    """H3 (a): the relay double-posts the VIP's call and deletes one copy,
    a tidy-up. The call still stands on the relay's channel: it is traded
    once, and the VIP's own post still reaches the trade."""
    _store_call(conn, order, relay_posts=(COPY, COPY + 1), forwards=copies == "forward")
    tidy = _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=2))

    (live,) = _live(conn)
    assert _queued_entries(conn) == [(live, "acct-1"), (live, "acct-2")]
    assert relay_repo.signal_for_post(conn, channel_id=V, message_id=CALL) == live
    assert "CRITICAL" not in [level for level, _ in _codes(conn)]
    if order == "vip-first":  # the relay's repeat went: nothing else changes
        assert tidy.outcome == "deleted"
        assert _codes(conn) == [("WARNING", edits_repo.SIGNAL_REPEAT_REMOVED)]
        detail = _detail(conn, edits_repo.SIGNAL_REPEAT_REMOVED)
        assert f"message {COPY + 1}" in detail
    else:  # the holder went, its channel still shows the call: promoted there
        assert (tidy.outcome, tidy.promoted_signal_id) == ("voided", live)
        assert ("WARNING", edits_repo.SIGNAL_REPOST_PROMOTED) in _codes(conn)
    _the_vip_manages_the_trade(conn, live)


@pytest.mark.parametrize("order", ORDERS)
def test_a_call_the_vip_withdraws_after_the_relay_tidied_up_is_traded_nowhere(
    conn, order  # noqa: F811
):
    """H3 (b): the relay's deletion of one of its two copies is applied
    first, then the VIP deletes its call: withdrawn, nothing queued."""
    _store_call(conn, order, relay_posts=(COPY, COPY + 1))
    _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=2))
    gone = _delete(conn, VIP, CALL, POSTED + timedelta(minutes=2, seconds=5))

    assert gone.outcome == "voided"
    assert _live(conn) == []
    assert _queued_entries(conn) == []
    assert ("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION, V, CALL, gone.signal_id) in (
        _alerts(conn)
    )
    assert "CRITICAL" not in [level for level, _ in _codes(conn)]


@pytest.mark.parametrize("order", ORDERS)
def test_a_relay_deleting_its_only_copy_and_posting_it_again_trades_it_once(
    conn, order  # noqa: F811
):
    """H3 (c): the relay deletes its only copy (its last post of the call
    withdraws it, loudly: the C2 cost) and posts it again, a formatting
    fix. The repost is a new signal replacing the withdrawn one, and the
    VIP's own post manages that."""
    _store_call(conn, order)
    first = _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=1))
    assert first.outcome == "voided"
    again = _ingest(conn, RELAY, COPY + 1, posted=POSTED + timedelta(minutes=2))

    assert again.outcome == "new"
    assert _live(conn) == [again.signal_id]
    assert _q(
        conn, "SELECT promoted_from_signal_id FROM signals WHERE signal_id = ?", again.signal_id
    ) == [(first.signal_id,)]
    assert relay_repo.live_signal_for(conn, first.signal_id) == again.signal_id
    assert _queued_entries(conn) == [(again.signal_id, "acct-1"), (again.signal_id, "acct-2")]
    assert relay_repo.COPY_OF_WITHDRAWN_SIGNAL not in [code for _, code in _codes(conn)]
    _the_vip_manages_the_trade(conn, again.signal_id)


# -- R3-P3: closing channel only (review round 2, H1 / H1b) ------------------------------


@pytest.mark.parametrize("order", ORDERS)
def test_the_relay_posting_a_call_again_after_the_vip_withdrew_it_is_held_back(
    conn, order  # noqa: F811
):
    """H1: the VIP withdrew the call; the relay, whose copy still shows it,
    then posts it again by hand. The VIP's channel withdrew the call, so the
    relay's post is held back loudly -- in either order.

    Round 4 (R4-2; round 3 also held back H1b, the relay deleting its own
    copy first): a relay that removed its only copy withdrew the call too,
    and its repost is a new signal -- see
    test_relay_echo.test_a_channel_posting_again_a_call_it_withdrew_after_the_other_did_is_traded."""
    _store_call(conn, order)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1))
    again = _ingest(conn, RELAY, COPY + 2, posted=POSTED + timedelta(minutes=3))

    assert (again.outcome, again.relay_of_signal_id) == ("relay_duplicate", 1)
    assert _queued_entries(conn) == []
    assert ("CRITICAL", relay_repo.COPY_OF_WITHDRAWN_SIGNAL, R, COPY + 2, 1) in _alerts(conn)
    detail = _detail(conn, relay_repo.COPY_OF_WITHDRAWN_SIGNAL)
    assert f"withdrawn by message {CALL} on channel {V}" in detail


@pytest.mark.parametrize("held", ["hand", "forward"])
@pytest.mark.parametrize("order", ORDERS)
def test_the_withdrawing_channel_posting_the_call_again_takes_over_the_copy_held_back(
    conn, order, held  # noqa: F811
):
    """R3-P3: the relay's copy was held back (the VIP had withdrawn the
    call) with a CRITICAL telling the user to trade it by hand. The VIP then
    posts the call again: a new signal, which the held-back copy now
    repeats -- and the CRITICAL is acknowledged, replaced by an alert that
    says the call is queued and must not be traded by hand as well."""
    _store_call(conn, order)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1))
    _ingest(
        conn, RELAY, COPY + 2, posted=POSTED + timedelta(minutes=3),
        fwd=(V, CALL) if held == "forward" else None,
    )
    (critical,) = _q(
        conn, "SELECT alert_id FROM alerts WHERE code = ?", relay_repo.COPY_OF_WITHDRAWN_SIGNAL
    )
    again = _ingest(conn, VIP, 4012, posted=POSTED + timedelta(minutes=4))

    assert again.outcome == "new"
    assert _q(
        conn, "SELECT promoted_from_signal_id FROM signals WHERE signal_id = ?", again.signal_id
    ) == [(1,)]
    assert _link(conn, RELAY, COPY + 2) == again.signal_id
    assert _q(conn, "SELECT acked FROM alerts WHERE alert_id = ?", critical[0]) == [(1,)]
    unacked = _q(conn, "SELECT level, code, channel_id, message_id, signal_id FROM alerts "
                       "WHERE acked = 0 ORDER BY alert_id")
    assert (
        "WARNING", relay_repo.COPY_OF_WITHDRAWN_SIGNAL_SUPERSEDED, R, COPY + 2, again.signal_id
    ) in unacked
    assert relay_repo.COPY_OF_WITHDRAWN_SIGNAL not in [row[1] for row in unacked]
    detail = _detail(conn, relay_repo.COPY_OF_WITHDRAWN_SIGNAL_SUPERSEDED)
    assert f"#{critical[0]}" in detail
    assert f"message {COPY + 2} on channel {R}" in detail
    assert f"message 4012 on channel {V}" in detail
    assert f"signal {again.signal_id}" in detail and "twice" in detail
    assert _queued_entries(conn) == [(again.signal_id, "acct-1"), (again.signal_id, "acct-2")]


def test_a_repost_on_the_withdrawing_channel_supersedes_nothing_when_nothing_was_held_back(
    conn,  # noqa: F811
):
    """Control: delete and repost with no copy held back -- no alert."""
    _ingest(conn, VIP, CALL)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1))
    again = _ingest(conn, VIP, 4012, posted=POSTED + timedelta(minutes=2))
    assert again.outcome == "new"
    assert _codes(conn) == [("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION)]


# -- R3-P4: a stale post never reverts a level (review round 2, H2) -----------------------


@pytest.mark.parametrize("order", ORDERS)
def test_a_stale_post_never_puts_back_a_stop_the_poster_moved_whichever_post_holds_the_call(
    conn, order  # noqa: F811
):
    """The relay's hand copy never mirrored the VIP's stop move and later
    edits only its TP1: applying its whole version would put the old stop
    back on a live position -- whether the relay's post holds the signal
    or is its copy."""
    _store_call(conn, order)
    _take_entry(conn, 1)
    assert _edit(conn, VIP, CALL, SL_4480, EDIT_1).outcome == "reparsed"
    stale = _edit(conn, RELAY, COPY, TP1_EDIT, EDIT_2)
    conn.commit()

    assert stale.outcome == "recorded"
    assert _levels(conn) == (2, 4480.0, [4490.0, 4495.0, 4500.0, 4505.0])
    assert _q(
        conn, "SELECT op, json_extract(parsed_json, '$.price_hint') FROM followups "
        "WHERE edit_seq > 0"
    ) == [("amend_sl", 4480.0)]
    assert _alerts(conn) == [("WARNING", edits_repo.SIGNAL_COPY_OUT_OF_DATE, R, COPY, 1)]
    detail = _detail(conn, edits_repo.SIGNAL_COPY_OUT_OF_DATE)
    assert "TP1 4490 -> 4491" in detail and "SL 4470" in detail and "4480" in detail


def test_the_holders_own_edit_of_a_call_nobody_copied_is_applied_whole(conn):  # noqa: F811
    """Control: the protection is for relayed calls only."""
    _ingest(conn, VIP, CALL)
    _take_entry(conn, 1)
    assert _edit(conn, VIP, CALL, SL_4480, EDIT_1).outcome == "reparsed"
    assert _edit(conn, VIP, CALL, SL_4480.replace("4490", "4491"), EDIT_2).outcome == "reparsed"
    assert _levels(conn) == (3, 4480.0, [4491.0, 4495.0, 4500.0, 4505.0])


# -- R3-P2: every alert names the call's newest signal (review round 2, H21) --------------


@pytest.mark.parametrize("order", ORDERS)
def test_a_reversed_deletion_names_the_signal_now_carrying_the_call(conn, order):  # noqa: F811
    """The VIP posted its call three times. The window inferred the first
    post deleted, then the second was deleted; then the first reads back.
    DELETION_REVERSED must name the signal that carries the call now --
    after two promotions, the third one -- never only a voided one."""
    if order == "relay-first":
        _ingest(conn, RELAY, COPY, posted=POSTED - timedelta(seconds=30))
    for index, message_id in enumerate((CALL, 4012, 4013)):
        _ingest(conn, VIP, message_id, posted=POSTED + timedelta(minutes=index))
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=3), by="window")
    _delete(conn, VIP, 4012, POSTED + timedelta(minutes=4))
    _edit(conn, VIP, CALL, SIGNAL_TEXT, POSTED + timedelta(minutes=5))
    conn.commit()

    (live,) = _live(conn)
    assert _queued_entries(conn) == [(live, "acct-1"), (live, "acct-2")]
    detail = _detail(conn, edits_repo.DELETION_REVERSED)
    assert f"signal {live}" in detail
    if order == "vip-first":
        assert live == 3
        assert "message 4013" in detail and "twice" in detail


@pytest.mark.parametrize("order", ORDERS)
def test_a_reversed_deletion_of_a_repeat_says_nothing_was_withdrawn(conn, order):  # noqa: F811
    """The window inferred one of the VIP's two posts deleted -- a repeat
    removal, which withdrew nothing -- and then it reads back. The alert
    must not claim the call's entries were withdrawn."""
    _store_call(conn, order)
    _ingest(conn, VIP, 4012, posted=POSTED + timedelta(minutes=1))
    removed = CALL if order == "relay-first" else 4012  # never the signal's own message
    assert _delete(conn, VIP, removed, POSTED + timedelta(minutes=3), by="window").outcome == (
        "deleted"
    )
    text = SIGNAL_TEXT if removed == CALL else SIGNAL_TEXT + " "
    _edit(conn, VIP, removed, text, POSTED + timedelta(minutes=5))
    conn.commit()

    assert _live(conn) == [1]
    detail = _detail(conn, edits_repo.DELETION_REVERSED)
    assert "signal 1 (this message is a copy of it) still stands" in detail
    assert "nothing was withdrawn" in detail and "withdrawn stay skipped" not in detail


# -- R3-P1: the holder removed while its channel still shows the call ---------------------


def test_an_executed_holder_removed_while_its_repost_stands_is_only_a_warning(conn):  # noqa: F811
    """The post was replaced; the trade continues; the repost stays linked."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, VIP, 4012, posted=POSTED + timedelta(minutes=2))
    _take_entry(conn, 1)
    gone = _delete(conn, VIP, CALL, POSTED + timedelta(minutes=3))

    assert gone.outcome == "deleted"
    assert _live(conn) == [1]
    assert _queued_entries(conn) == [(1, "acct-2")]
    assert _link(conn, VIP, 4012) == 1
    assert _alerts(conn) == [("WARNING", edits_repo.SIGNAL_REPEAT_REMOVED, V, CALL, 1)]
    assert "message 4012" in _detail(conn, edits_repo.SIGNAL_REPEAT_REMOVED)


@pytest.mark.parametrize("order", ORDERS)
def test_the_last_post_on_a_channel_withdraws_the_call_and_promotes_nothing(
    conn, order  # noqa: F811
):
    """The relay double-posted; the VIP deletes its only post: the VIP's
    channel withdrew the call, so it is voided -- the relay's standing
    posts are never promoted over that."""
    _store_call(conn, order, relay_posts=(COPY, COPY + 1))
    gone = _delete(conn, VIP, CALL, POSTED + timedelta(minutes=2))
    assert (gone.outcome, gone.promoted_signal_id) == ("voided", None)
    assert _live(conn) == []
    assert _queued_entries(conn) == []


@pytest.mark.parametrize("order", ORDERS)
def test_a_vip_post_edited_away_while_the_vip_reposted_the_call_leaves_it_standing(
    conn, order  # noqa: F811
):
    """The VIP posted the call again, then edited its first post into
    chatter: its channel still shows the call, which stands (and is traded
    once)."""
    _store_call(conn, order)
    _ingest(conn, VIP, 4012, posted=POSTED + timedelta(minutes=2))
    _edit(conn, VIP, CALL, CHATTER_TEXT, EDIT_1)
    conn.commit()
    (live,) = _live(conn)
    assert _queued_entries(conn) == [(live, "acct-1"), (live, "acct-2")]
    assert relay_repo.signal_for_post(conn, channel_id=R, message_id=COPY) == live
    assert relay_repo.signal_for_post(conn, channel_id=V, message_id=4012) == live


# -- R3-P1: a post re-aimed or broken is edited away too ----------------------------------
#
# A re-aimed post (another side, symbol or entry) or a broken one (no longer
# parses) no longer shows the call, like a deleted one. Round 1 invalidated
# the call for any post re-aimed -- except the signal's own message with a
# repost standing, which promoted the repost -- so whether the call was
# traded depended on which post was stored first.

_BREAKS = {"reaim": DIRECTION_EDIT, "unparseable": UNPARSEABLE_EDIT}
#: A later version of each: still not the call.
_BREAKS_AGAIN = {
    "reaim": DIRECTION_EDIT.replace("4490", "4491"),
    "unparseable": UNPARSEABLE_EDIT.replace("4490", "4491"),
}


@pytest.mark.parametrize("change", list(_BREAKS))
@pytest.mark.parametrize("order", ORDERS)
def test_a_post_re_aimed_while_its_channel_reposted_the_call_leaves_the_call_standing(
    conn, order, change  # noqa: F811
):
    """The VIP posted its call again, then re-aimed (or broke) its first
    post. Its channel still shows the call: it is traded once, whichever
    post holds it -- VIP-first the repost is promoted over the invalidated
    signal; relay-first the VIP's first post is only one of its channel's
    two posts, and its re-aim a repeat removal. Either way the re-aimed
    post has left the call: the VIP then deleting its repost is its
    channel's last post, and withdraws the call."""
    _store_call(conn, order)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=2))
    result = _edit(conn, VIP, CALL, _BREAKS[change], EDIT_1)
    conn.commit()

    (live,) = _live(conn)
    assert _queued_entries(conn) == [(live, "acct-1"), (live, "acct-2")]
    for channel_id, message_id in ((V, REPOST), (R, COPY)):
        assert relay_repo.signal_for_post(conn, channel_id=channel_id, message_id=message_id) == (
            live
        )
    if order == "vip-first":
        assert (result.outcome, result.promoted_signal_id) == ("invalidated", live)
    else:
        assert (result.outcome, live) == ("recorded", 1)
        assert _alerts(conn) == [("WARNING", edits_repo.SIGNAL_REPEAT_REMOVED, V, CALL, 1)]
        detail = _detail(conn, edits_repo.SIGNAL_REPEAT_REMOVED)
        assert f"message {REPOST}" in detail
        assert ("re-aimed" if change == "reaim" else "no longer reads as a signal") in detail
        assert "no longer read as part of it" in detail and "tidied up" not in detail

    gone = _delete(conn, VIP, REPOST, POSTED + timedelta(minutes=8))
    assert (gone.outcome, gone.promoted_signal_id) == ("voided", None)
    assert _live(conn) == []
    assert _queued_entries(conn) == []


@pytest.mark.parametrize("change", list(_BREAKS))
@pytest.mark.parametrize("order", ORDERS)
def test_an_executed_call_whose_first_post_is_re_aimed_while_reposted_keeps_trading(
    conn, order, change  # noqa: F811
):
    """R3-P1: executed, the post was replaced and the trade continues -- a
    WARNING only, in both orders. The re-aimed post has left the call:
    later versions of it, and its deletion, say nothing more; the VIP
    deleting its repost, its channel's last post, flags the call."""
    _store_call(conn, order)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=2))
    _take_entry(conn, 1)
    first = _edit(conn, VIP, CALL, _BREAKS[change], EDIT_1)
    again = _edit(conn, VIP, CALL, _BREAKS_AGAIN[change], EDIT_2)
    conn.commit()

    assert (first.outcome, again.outcome) == ("recorded", "recorded")
    assert _live(conn) == [1]
    assert _levels(conn) == (1, 4470.0, [4490.0, 4495.0, 4500.0, 4505.0])
    assert _queued_entries(conn) == [(1, "acct-2")]
    assert _alerts(conn) == [("WARNING", edits_repo.SIGNAL_REPEAT_REMOVED, V, CALL, 1)]

    assert _delete(conn, VIP, CALL, POSTED + timedelta(minutes=12)).outcome == "deleted"
    assert len(_alerts(conn)) == 1
    last = _delete(conn, VIP, REPOST, POSTED + timedelta(minutes=13))
    assert (last.outcome, last.signal_id) == ("flagged", 1)
    assert ("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED, V, REPOST, 1) in _alerts(conn)


@pytest.mark.parametrize("order", ORDERS)
def test_a_repost_re_aimed_away_is_never_promoted(conn, order):  # noqa: F811
    """The VIP re-aimed its repost (a repeat removal: its first post still
    shows the call), then deleted its first post. Its channel no longer
    shows the call, which is withdrawn -- the re-aimed repost, which still
    reads as a signal, is never promoted into a trade."""
    _store_call(conn, order)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=2))
    assert _edit(conn, VIP, REPOST, DIRECTION_EDIT, EDIT_1).outcome == "recorded"
    conn.commit()
    assert _live(conn) == [1]

    gone = _delete(conn, VIP, CALL, POSTED + timedelta(minutes=8))
    assert (gone.outcome, gone.promoted_signal_id) == ("voided", None)
    assert _live(conn) == []
    assert _queued_entries(conn) == []


@pytest.mark.parametrize("order", ORDERS)
def test_a_post_that_left_the_call_never_puts_back_a_level_when_it_shows_it_again(
    conn, order  # noqa: F811
):
    """The VIP re-aimed its repost, moved the stop on its first post
    (4470 -> 4480, amended), then edited the repost back into the call with
    the old stop: its version is no correction of the call -- nothing is
    applied, and a WARNING says why."""
    _store_call(conn, order)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=2))
    _take_entry(conn, 1)
    _edit(conn, VIP, REPOST, DIRECTION_EDIT.replace("4470", "4520"), EDIT_1)
    assert _edit(conn, VIP, CALL, SL_4480, EDIT_1).outcome == "reparsed"
    back = _edit(conn, VIP, REPOST, SIGNAL_TEXT, EDIT_2)
    conn.commit()

    assert back.outcome == "recorded"
    assert _levels(conn) == (2, 4480.0, [4490.0, 4495.0, 4500.0, 4505.0])
    assert _q(
        conn, "SELECT op, json_extract(parsed_json, '$.price_hint') FROM followups "
        "WHERE edit_seq > 0"
    ) == [("amend_sl", 4480.0)]
    assert ("WARNING", edits_repo.SIGNAL_COPY_OUT_OF_DATE, V, REPOST, 1) in _alerts(conn)
    assert "SL 4470" in _detail(conn, edits_repo.SIGNAL_COPY_OUT_OF_DATE)


@pytest.mark.parametrize("change", ["chatter", *_BREAKS])
def test_an_executed_call_its_post_left_and_shows_again_is_never_to_be_traded_by_hand(
    conn, change  # noqa: F811
):
    """The call's own message was edited away while its repost stood (a
    WARNING; the trade continues), then edited back into the call. Edits
    never open trades -- and the warning must not invite the user to trade
    by hand a call that is still live."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=2))
    _take_entry(conn, 1)
    _edit(conn, VIP, CALL, {"chatter": CHATTER_TEXT, **_BREAKS}[change], EDIT_1)
    _edit(conn, VIP, CALL, SIGNAL_TEXT, EDIT_2)
    conn.commit()

    assert _live(conn) == [1]
    assert _queued_entries(conn) == [(1, "acct-2")]
    for (detail,) in _q(conn, "SELECT detail FROM alerts"):
        assert "by hand" not in detail, detail


def test_a_post_that_left_the_call_and_shows_another_is_a_new_call_never_traded(
    conn,  # noqa: F811
):
    """Control: the call's own message edited away while its repost stood,
    then edited into a different call (SELL). That is not the live call
    coming back: the usual warning for a signal an edit made -- not
    traded; trade it by hand if it is real."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=2))
    _take_entry(conn, 1)
    _edit(conn, VIP, CALL, CHATTER_TEXT, EDIT_1)
    _edit(conn, VIP, CALL, DIRECTION_EDIT, EDIT_2)
    conn.commit()

    assert _live(conn) == [1]
    detail = _detail(conn, edits_repo.EDIT_CREATED_SIGNAL)
    assert "trade it by hand" in detail and "still stands" not in detail


def test_the_standing_posts_are_the_ones_still_showing_the_call(conn):  # noqa: F811
    """`relay_repo.standing_posts_on`: a channel's posts of a call, minus
    the ones deleted or edited into something that is no signal."""
    for index, message_id in enumerate((CALL, 4012, 4013, 4014)):
        _ingest(conn, VIP, message_id, posted=POSTED + timedelta(minutes=index))
    _edit(conn, VIP, 4013, CHATTER_TEXT, EDIT_1)
    _delete(conn, VIP, 4014, POSTED + timedelta(minutes=6))
    conn.commit()
    assert [
        row["message_id"]
        for row in relay_repo.standing_posts_on(conn, signal_id=1, channel_id=V)
    ] == [CALL, 4012]


@pytest.mark.parametrize("order", ORDERS)
def test_a_channel_still_showing_a_replaced_call_keeps_it_standing(conn, order):  # noqa: F811
    """The relay deleted its only copy (the call is voided: the C2 cost) and
    posted it again (signal 2 replaces signal 1). The VIP double-posted the
    call: deleting one of its two posts -- both stored against signal 1 --
    is a repeat removal of the call signal 2 carries now; deleting the
    other, its channel's last, withdraws it."""
    _store_call(conn, order)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=1))
    assert _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=2)).outcome == "voided"
    again = _ingest(conn, RELAY, COPY + 1, posted=POSTED + timedelta(minutes=3))
    assert (again.outcome, relay_repo.live_signal_for(conn, 1)) == ("new", again.signal_id)

    first = _delete(conn, VIP, CALL, POSTED + timedelta(minutes=4))
    assert (first.outcome, first.signal_id) == ("deleted", again.signal_id)
    assert _live(conn) == [again.signal_id]
    assert _queued_entries(conn) == [(again.signal_id, "acct-1"), (again.signal_id, "acct-2")]
    assert [
        row["message_id"]
        for row in relay_repo.standing_posts_on(conn, signal_id=again.signal_id, channel_id=V)
    ] == [REPOST]

    last = _delete(conn, VIP, REPOST, POSTED + timedelta(minutes=5))
    assert (last.outcome, last.signal_id) == ("voided", again.signal_id)
    assert _live(conn) == []


@pytest.mark.parametrize("another", [False, True], ids=["alone", "with-another-repost"])
@pytest.mark.parametrize("how", ["deleted", "re-aimed"])
def test_a_holder_withdrawn_while_an_older_post_of_its_call_stands_leaves_the_call_standing(
    conn, how, another  # noqa: F811
):
    """A standing post that already carries a signal of the call (its own
    signals row) cannot be promoted -- one post, one signal. Reached here
    through a window deletion read back: the VIP's first post (signal 1)
    stood again after the call had moved to signal 3 on the VIP's own
    repost. The VIP deleting (or re-aiming) that repost leaves its channel
    still showing the call in the first post: the call stands (a repeat
    removal), rather than being voided with nothing in its place -- or,
    with another repost of the VIP's standing too, that one is promoted,
    never the first post."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, RELAY, COPY, posted=POSTED + timedelta(seconds=30))
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=1))
    _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=2))  # the relay's last post: voided
    assert _ingest(conn, RELAY, COPY + 1, posted=POSTED + timedelta(minutes=3)).signal_id == 2
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=4), by="window")  # a repeat removal
    _delete(conn, VIP, REPOST, POSTED + timedelta(minutes=5))  # the VIP's last post: voided
    third = _ingest(conn, VIP, REPOST_2, posted=POSTED + timedelta(minutes=6))
    assert (third.outcome, _live(conn)) == ("new", [3])
    if another:
        _ingest(conn, VIP, 4014, posted=POSTED + timedelta(minutes=6, seconds=30))
    _edit(conn, VIP, CALL, SIGNAL_TEXT, POSTED + timedelta(minutes=7))  # read back
    conn.commit()

    if how == "deleted":
        result = _delete(conn, VIP, REPOST_2, POSTED + timedelta(minutes=8))
    else:
        result = _edit(conn, VIP, REPOST_2, DIRECTION_EDIT, POSTED + timedelta(minutes=8))
        conn.commit()
    assert _q(conn, "SELECT COUNT(*) FROM signals WHERE message_id = ?", CALL) == [(1,)]
    if another:
        assert result.promoted_signal_id == 4
        assert _q(conn, "SELECT message_id FROM signals WHERE signal_id = 4") == [(4014,)]
        assert _live(conn) == [4]
        assert _queued_entries(conn) == [(4, "acct-1"), (4, "acct-2")]
    else:
        assert (result.outcome, result.promoted_signal_id) == (
            "deleted" if how == "deleted" else "recorded", None
        )
        assert _live(conn) == [3]
        assert _queued_entries(conn) == [(3, "acct-1"), (3, "acct-2")]
        assert ("WARNING", edits_repo.SIGNAL_REPEAT_REMOVED, V, REPOST_2, 3) in _alerts(conn)


def test_a_late_copy_of_a_call_posted_three_times_repeats_its_newest_signal(
    conn,  # noqa: F811
):
    """The VIP deleted and posted its call again twice, 10 minutes apart
    (signals 1 -> 2 -> 3). The relay's hand copy of the first post, stored
    late, is within the window of the first two signals only -- both
    withdrawn -- yet repeats the live one, the newest of the call."""
    for index, message_id in enumerate((CALL, 4012, 4013)):
        _ingest(conn, VIP, message_id, posted=POSTED + timedelta(minutes=10 * index))
        if index < 2:
            _delete(conn, VIP, message_id, POSTED + timedelta(minutes=10 * index + 1))
    assert _live(conn) == [3]
    assert relay_repo.chain_ids(conn, 1) == [1, 2, 3]

    late = _ingest(conn, RELAY, COPY, posted=POSTED + timedelta(minutes=1))
    assert (late.outcome, late.relay_of_signal_id) == ("relay_duplicate", 3)
    assert relay_repo.COPY_OF_WITHDRAWN_SIGNAL not in [code for _, code in _codes(conn)]
    assert _queued_entries(conn) == [(3, "acct-1"), (3, "acct-2")]


# -- R3-P2: the chain and the one helper ---------------------------------------------


def test_a_late_forward_of_a_call_withdrawn_twice_names_its_newest_signal(conn):  # noqa: F811
    """The VIP deleted its call, posted it again (signal 2 replaces signal
    1) and deleted that too. A forward of the first post arriving hours
    later is held back loudly -- against the newest signal of the call,
    and naming the post that withdrew that one."""
    _ingest(conn, VIP, CALL)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1))
    again = _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=2))
    assert _q(conn, "SELECT promoted_from_signal_id FROM signals WHERE signal_id = 2") == [(1,)]
    _delete(conn, VIP, REPOST, POSTED + timedelta(minutes=3))
    forward = _ingest(conn, RELAY, COPY, posted=POSTED + timedelta(hours=3), fwd=(V, CALL))

    assert (forward.outcome, forward.relay_of_signal_id) == ("relay_duplicate", again.signal_id)
    assert _queued_entries(conn) == []
    assert ("CRITICAL", relay_repo.COPY_OF_WITHDRAWN_SIGNAL, R, COPY, again.signal_id) in (
        _alerts(conn)
    )
    assert f"withdrawn by message {REPOST} on channel {V}" in _detail(
        conn, relay_repo.COPY_OF_WITHDRAWN_SIGNAL
    )


def test_live_signal_for_follows_the_call_to_its_newest_signal(conn):  # noqa: F811
    for index, message_id in enumerate((CALL, 4012, 4013)):
        _ingest(conn, VIP, message_id, posted=POSTED + timedelta(minutes=index))
    assert [relay_repo.live_signal_for(conn, 1)] == [1]
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=3))
    _delete(conn, VIP, 4012, POSTED + timedelta(minutes=4))
    assert [relay_repo.live_signal_for(conn, sid) for sid in (1, 2, 3)] == [3, 3, 3]
    assert relay_repo.signal_for_post(conn, channel_id=V, message_id=4013) == 3


@pytest.mark.parametrize("order", ORDERS)
def test_a_follow_up_replying_to_a_post_re_aimed_away_from_the_call_targets_nothing(
    conn, order  # noqa: F811
):
    """The VIP re-aimed its repost (BUY -> SELL: a repeat removal, the BUY
    call stands on its first post) and then says "close half" replying to
    the re-aimed post. That post shows another call now, which was never
    traded: the instruction is not given the live BUY signal. A reply to
    the first post still is."""
    _store_call(conn, order)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=2))
    assert _edit(conn, VIP, REPOST, DIRECTION_EDIT, EDIT_1).outcome == "recorded"
    conn.commit()
    (live,) = _live(conn)
    to_repost = _ingest(
        conn, VIP, 4020, CLOSE_HALF, posted=POSTED + timedelta(minutes=8), reply_to=REPOST
    )
    to_call = _ingest(
        conn, VIP, 4021, "Move SL to BE", posted=POSTED + timedelta(minutes=9), reply_to=CALL
    )
    assert (to_repost.outcome, to_call.outcome) == ("new", "new")
    assert _q(
        conn, "SELECT message_id, target_signal_id FROM followups ORDER BY followup_id"
    ) == [(4020, None), (4021, live)]


@pytest.mark.parametrize("order", ORDERS)
def test_a_follow_up_replying_to_the_post_that_re_aimed_the_call_targets_that_call(
    conn, order  # noqa: F811
):
    """Control: the VIP re-aimed its only post -- the call is INVALID (C2).
    A reply to that post is still about that call, which Plan 3 sees is
    closed."""
    _store_call(conn, order)
    assert _edit(conn, VIP, CALL, DIRECTION_EDIT, EDIT_1).outcome == "invalidated"
    conn.commit()
    _ingest(conn, VIP, 4020, CLOSE_HALF, posted=POSTED + timedelta(minutes=8), reply_to=CALL)
    assert _q(conn, "SELECT target_signal_id FROM followups") == [(1,)]
    assert _q(conn, "SELECT state FROM signals") == [("INVALID",)]


def test_a_follow_up_replying_to_a_replaced_signals_post_targets_the_new_one(conn):  # noqa: F811
    """A reply to any post of the call reaches its newest signal: stored
    ones are re-pointed when the call is replaced, later ones resolve to
    it at once -- except a reply to the post that withdrew the call.

    Round 4 (R4-4; round 3 moved `before` to the new signal too): `before`
    replied to the relay's copy, the post that then withdrew signal 1, so
    it stays with signal 1."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, RELAY, COPY, posted=POSTED + timedelta(seconds=30))
    before = _ingest(
        conn, RELAY, 7005, CLOSE_HALF, posted=POSTED + timedelta(minutes=1), reply_to=COPY
    )
    _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=2))  # the relay's last post
    again = _ingest(conn, RELAY, COPY + 1, posted=POSTED + timedelta(minutes=3))
    after = _ingest(
        conn, VIP, 4005, "Move SL to BE", posted=POSTED + timedelta(minutes=4), reply_to=CALL
    )
    rows = _q(
        conn, "SELECT followup_id, target_signal_id, resolution_path FROM followups "
        "ORDER BY followup_id"
    )
    assert rows == [
        (before.followup_id, 1, "relay-reply"),
        (after.followup_id, again.signal_id, "relay-reply"),
    ]


def test_the_post_that_re_aimed_a_call_never_reaches_the_repost_promoted_after_it(
    conn,  # noqa: F811
):
    """The VIP re-aimed its first post (the call is INVALID) while its
    repost stood (promoted). Another edit of the re-aimed post is about
    the call it broke, not about the repost: the promoted signal is left
    alone."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, VIP, 4012, posted=POSTED + timedelta(minutes=2))
    reaim = SIGNAL_TEXT.replace("BUY", "SELL")
    assert _edit(conn, VIP, CALL, reaim, EDIT_1).promoted_signal_id == 2
    again = _edit(conn, VIP, CALL, reaim.replace("4470", "4495"), EDIT_2)
    conn.commit()
    assert again.signal_id == 1
    assert _live(conn) == [2]
    assert _queued_entries(conn) == [(2, "acct-1"), (2, "acct-2")]
    assert _q(conn, "SELECT version FROM signals WHERE signal_id = 2") == [(1,)]


# == the arrival paths: the real listener and poll, the fake Telegram ===========


async def _post_call(
    client, order: str, *, relay_copies=(COPY, COPY + 1), relay_chatter=()
) -> None:
    """The VIP's call and its chatter, and the relay's copies (30 s apart)
    and chatter: VIP-first, or the relay's first copy delivered first."""
    relay = [
        _post(mid, after=timedelta(seconds=30 * (i + 1))) for i, mid in enumerate(relay_copies)
    ]
    if order == "vip-first":
        assert await client.emit(V, _post(CALL))
        for message in relay:
            assert await client.emit(R, message)
    else:
        assert await client.emit(R, relay[0])
        assert await client.emit(V, _post(CALL))
        for message in relay[1:]:
            assert await client.emit(R, message)
    for message_id in VIP_CHATTER:
        assert await client.emit(V, _post(message_id, CHATTER_TEXT))
    for message_id in relay_chatter:
        assert await client.emit(R, _post(message_id, CHATTER_TEXT, after=timedelta(minutes=1)))


def _live_rows(db_path) -> list[int]:
    return [
        row[0]
        for row in _db_rows(
            db_path,
            "SELECT signal_id FROM signals WHERE state = 'NEW' AND withdrawn_at_utc IS NULL",
        )
    ]


@pytest.mark.parametrize("order", ORDERS)
def test_a_relays_tidy_up_through_the_listener_leaves_the_vip_managing_the_trade(
    tmp_path, order
):
    """H3 end to end: the relay double-posts the VIP's call and deletes
    one copy; acct-1 takes the entry; the VIP says "close half", moves its
    stop and deletes its call. Every one of those reaches the one trade."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _post_call(client, order)
        await _round(listener, time)
        assert await client.emit_delete(R, COPY)
        (live,) = _live_rows(db_path)
        _mark_executed(db_path, live)
        assert await client.emit(
            V, _post(4005, CLOSE_HALF, after=timedelta(minutes=5), reply_to=CALL)
        )
        assert await client.emit_edit(V, CALL, SL_EDIT, edit_date=EDIT_1)
        assert await client.emit_delete(V, CALL)
        await _round(listener, time)
        return live

    live = _run(db_path, body)

    assert _db_rows(
        db_path, "SELECT op, target_signal_id FROM followups ORDER BY followup_id"
    ) == [("close_half", live), ("amend_sl", live)]
    assert ("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED, V, CALL, live) in _db_rows(
        db_path, "SELECT level, code, channel_id, message_id, signal_id FROM alerts"
    )
    assert _db_rows(
        db_path, "SELECT account_id, state FROM outbox WHERE kind = 'ENTRY' AND signal_id = ? "
        "ORDER BY account_id", (live,)
    ) == [("acct-1", "DONE"), ("acct-2", "SKIPPED")]
    assert _db_rows(
        db_path, "SELECT COUNT(*) FROM outbox WHERE kind = 'ENTRY' AND state = 'QUEUED'"
    ) == [(0,)]


@pytest.mark.parametrize("order", ORDERS)
def test_a_relays_only_copy_deleted_unseen_and_posted_again_through_the_listener(
    tmp_path, order
):
    """H3 (c) end to end, the relay's deletion found by the window re-read
    (the update was dropped): the call is withdrawn once, the relay's
    repost is the one live signal, and the VIP's post reaches it."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _post_call(
            client, order, relay_copies=(COPY,), relay_chatter=(COPY + 1, COPY + 2, COPY + 3)
        )
        await _round(listener, time)
        with client.blackout():
            assert not await client.emit_delete(R, COPY)
        for _ in range(3):
            await _round(listener, time)
        assert _live_rows(db_path) == []
        assert await client.emit(R, _post(COPY + 9, after=timedelta(minutes=3)))
        await _round(listener, time)
        (live,) = _live_rows(db_path)
        assert await client.emit(
            V, _post(4005, CLOSE_HALF, after=timedelta(minutes=5), reply_to=CALL)
        )
        return live

    live = _run(db_path, body)

    assert _db_rows(
        db_path, "SELECT channel_id, message_id, promoted_from_signal_id FROM signals "
        "WHERE signal_id = ?", (live,)
    ) == [(R, COPY + 9, 1)]
    assert _db_rows(db_path, "SELECT target_signal_id FROM followups") == [(live,)]
    assert _db_rows(
        db_path, "SELECT signal_id, COUNT(*) FROM outbox WHERE kind = 'ENTRY' AND state = 'QUEUED' "
        "GROUP BY signal_id"
    ) == [(live, 2)]
