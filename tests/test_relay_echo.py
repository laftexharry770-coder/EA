"""A call withdrawn and posted again, and the other channel's old posts of
it (review round 3 of the relay fixes, rulings R4-1 to R4-6).

Round 3 routed every post of a withdrawn signal to the signal posted in
its place (R3-P2). That made a relay's late echo of the poster's own
withdrawal -- its old copy removed after the poster's delete-and-repost --
void the poster's repost. Round 4 tells the cases apart, in both storage
orders:

- **R4-1, echo or original**: a forward of a post that no longer stands
  (deleted, edited away, or the post that withdrew the call) is an echo:
  its removal is only recorded. The post a withdrawing forward copied is
  the original: its removal withdraws the call posted in its place. Any
  other old post (a hand copy) still withdraws it -- doing nothing is the
  safe side -- with a CRITICAL alert that says it may only have been the
  other channel removing its old copy.
- **R4-2 / R4-3, every closer**: every channel whose last post of a call
  was withdrawn may post it again as a new signal -- by hand or by
  forwarding the same post again.
- **R4-4**: a reply to the post that withdrew a call is not moved to the
  call posted in its place, and a reply to such a post that no longer
  shows the call is about nothing.
- **R4-5**: a withdrawing post read back after a false window deletion
  stands again; a replaced call keeps its stale-post protection.
- **R4-6**: a forward showing another call than the live one is held back.
"""
from __future__ import annotations

import dataclasses
import json
import logging
from datetime import timedelta

import pytest
from telethon.tl import types

from fakes.telegram import FakeMessage
from test_edits import DIRECTION_EDIT, EDIT_1, EDIT_2, UNPARSEABLE_EDIT, _round
from test_relay import (
    ACCOUNTS,
    CALL,
    CHATTER_TEXT,
    CLOSE_HALF,
    COPY,
    OTHER,
    OTHER_CALL,
    POSTED,
    RELAY,
    REPOST,
    SIGNAL_TEXT,
    TP1_EDIT,
    VIP,
    VIP_CHATTER,
    R,
    V,
    _alerts,
    _claim,
    _db_rows,
    _db_write,
    _edit,
    _ingest,
    _link,
    _post,
    _q,
    _queued_entries,
    _run,
    conn,  # noqa: F401 -- the fixture
)
from test_relay_order import (
    ORDERS,
    SL_4480,
    _codes,
    _delete,
    _detail,
    _entries_claimable,
    _live,
    _live_rows,
    _store_call,
    _take_entry,
    _the_vip_manages_the_trade,
)
from xaucopier.parsing.router import route
from xaucopier.store import edits_repo, relay_repo

CANCELLED = "CANCELLED - do not take this one"
O = OTHER.channel_id


def _promoted_from(conn, signal_id: int) -> int | None:  # noqa: F811
    (row,) = _q(conn, "SELECT promoted_from_signal_id FROM signals WHERE signal_id = ?", signal_id)
    return row[0]


def _vip_deletes_and_posts_again(conn) -> int:  # noqa: F811
    """The VIP deletes its only post of the call (its channel withdrew it)
    and posts it again: a new signal replacing signal 1. Returns its id."""
    assert _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1)).outcome in ("voided", "deleted")
    again = _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=2))
    assert again.outcome == "new"
    assert _promoted_from(conn, again.signal_id) == 1
    assert _live(conn) == [again.signal_id]
    return again.signal_id


# -- R4-1 (a): an echo of the withdrawal is only recorded ------------------------------


@pytest.mark.parametrize("by", ["event", "window"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_relays_forward_removed_after_the_vip_posted_its_call_again_is_an_echo(
    conn, order, by, caplog  # noqa: F811
):
    """NEW-1 with a forwarding relay: the VIP deleted its call and posted it
    again; the relay's forward of the deleted post is removed afterwards --
    a late event, the window's inference, a relay mirroring deletions. That
    only echoes the VIP's deletion: the VIP's repost is still traded, and
    the relay forwarding the repost repeats it."""
    _store_call(conn, order, forwards=True)
    live = _vip_deletes_and_posts_again(conn)
    with caplog.at_level(logging.INFO, logger="xaucopier.store.edits_repo"):
        echo = _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=3), by=by)

    assert (echo.outcome, echo.promoted_signal_id) == ("deleted", None)
    assert _live(conn) == [live]
    assert _queued_entries(conn) == [(live, "acct-1"), (live, "acct-2")]
    assert _codes(conn) == [("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION)]
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        f"message {COPY}" in line and f"message {CALL}" in line and "echo" in line
        for line in lines
    ), lines

    forward = _ingest(
        conn, RELAY, COPY + 1, posted=POSTED + timedelta(minutes=3, seconds=5), fwd=(V, REPOST)
    )
    assert (forward.outcome, forward.relay_of_signal_id) == ("relay_duplicate", live)
    assert _entries_claimable(conn) == [live]


#: How the VIP takes its first post out of the call.
_REMOVALS = {"deleted": None, "chatter": CHATTER_TEXT, "re-aimed": DIRECTION_EDIT}


@pytest.mark.parametrize("how", list(_REMOVALS))
@pytest.mark.parametrize("executed", [False, True], ids=["pending", "executed"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_forward_of_a_post_its_channel_replaced_is_an_echo_when_removed(
    conn, order, executed, how  # noqa: F811
):
    """The VIP double-posted its call and took the first post out of it --
    deleted, edited into chatter or re-aimed; the relay's forward of that
    post is removed after it. The VIP's channel still shows the call, so
    the relay's removal only echoes the VIP's -- whichever post holds the
    signal (VIP-first the repost was promoted, or kept the trade;
    relay-first the relay's forward holds the call and the VIP's change was
    a repeat removal). The VIP deleting its repost, its channel's last
    post, then withdraws the call."""
    _store_call(conn, order, forwards=True)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=1))
    if executed:
        _take_entry(conn, 1)
    moment = POSTED + timedelta(minutes=2)
    if how == "deleted":
        _delete(conn, VIP, CALL, moment)
    else:
        _edit(conn, VIP, CALL, _REMOVALS[how], moment)
        conn.commit()
    (live,) = _live(conn)
    alerts = _alerts(conn)
    echo = _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=3))

    assert echo.outcome == "deleted"
    assert _live(conn) == [live]
    if executed:
        assert (live, _queued_entries(conn)) == (1, [(1, "acct-2")])
    else:
        assert _queued_entries(conn) == [(live, "acct-1"), (live, "acct-2")]
    assert _alerts(conn) == alerts  # the echo says nothing

    last = _delete(conn, VIP, REPOST, POSTED + timedelta(minutes=4))
    assert (last.outcome, last.signal_id) == ("flagged" if executed else "voided", live)


def test_a_forward_of_a_withdrawn_post_matched_to_another_call_is_an_echo_when_removed(
    conn,  # noqa: F811
):
    """The VIP withdrew its call; 16 minutes later a third channel posts the
    same trade (a call of its own, outside the window); the relay's late
    forward of the VIP's deleted post, posted close to the third channel's,
    repeats that live call. The relay removing its forward only echoes the
    VIP's deletion: the third channel's call is not withdrawn by it."""
    _ingest(conn, VIP, CALL)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1))
    other = _ingest(conn, OTHER, 8001, posted=POSTED + timedelta(minutes=16))
    assert other.outcome == "new"
    forward = _ingest(conn, RELAY, COPY, posted=POSTED + timedelta(minutes=20), fwd=(V, CALL))
    assert (forward.outcome, forward.relay_of_signal_id) == ("relay_duplicate", other.signal_id)

    echo = _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=21))
    assert echo.outcome == "deleted"
    assert _live(conn) == [other.signal_id]
    assert _queued_entries(conn) == [(other.signal_id, "acct-1"), (other.signal_id, "acct-2")]


#: How a post is edited out of the call: a "cancel" edit, re-aimed, broken.
_EDITS_AWAY = {"cancelled": CANCELLED, "re-aimed": DIRECTION_EDIT, "unparseable": UNPARSEABLE_EDIT}


def test_a_forward_of_a_post_of_no_call_is_never_an_echo(conn):  # noqa: F811
    """The rule is about forwards of the call's own posts. A third channel's
    copy stored before migration 4 linked copies is a post of no call; the
    relay forwarded it (a copy of the VIP's call, matched by the same trade
    close by) and both are removed. The relay's removal echoes nothing: it
    is the relay's last post of the call, which is withdrawn (C2)."""
    _ingest(conn, VIP, CALL)
    assert _ingest(conn, OTHER, 8001, posted=POSTED + timedelta(seconds=20)).outcome == (
        "relay_duplicate"
    )
    conn.execute(
        "UPDATE messages SET relay_of_signal_id = NULL WHERE channel_id = ? AND message_id = 8001",
        (O,),
    )
    conn.commit()
    forward = _ingest(conn, RELAY, COPY, posted=POSTED + timedelta(seconds=30), fwd=(O, 8001))
    assert (forward.outcome, forward.relay_of_signal_id) == ("relay_duplicate", 1)
    _delete(conn, OTHER, 8001, POSTED + timedelta(minutes=1))

    gone = _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=2))
    assert (gone.outcome, gone.signal_id) == ("voided", 1)
    assert _live(conn) == []


@pytest.mark.parametrize("change", list(_EDITS_AWAY))
@pytest.mark.parametrize("order", ORDERS)
def test_an_edit_taking_a_forward_out_of_the_call_is_an_echo_too(
    conn, order, change  # noqa: F811
):
    """A forward's text cannot be edited in Telegram, but the rule does
    not depend on it: a forward of a post no longer standing, edited out of
    the call -- a "cancel" edit, re-aimed or broken -- is recorded only,
    like its removal, whichever post holds the call's first signal."""
    _store_call(conn, order, forwards=True)
    live = _vip_deletes_and_posts_again(conn)
    edited = _edit(conn, RELAY, COPY, _EDITS_AWAY[change], POSTED + timedelta(minutes=3))
    conn.commit()
    assert edited.outcome == "recorded"
    assert _live(conn) == [live]
    assert _queued_entries(conn) == [(live, "acct-1"), (live, "acct-2")]
    assert _codes(conn) == [("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION)]


def test_a_held_back_forward_the_vip_posted_again_is_an_echo_when_removed(conn):  # noqa: F811
    """The relay's forward of a post the VIP had deleted was held back; the
    VIP then posted the call again, which the forward now repeats. The
    relay removing its forward of the deleted post only echoes the VIP."""
    _ingest(conn, VIP, CALL)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1))
    _ingest(conn, RELAY, COPY, posted=POSTED + timedelta(minutes=2), fwd=(V, CALL))
    again = _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=3))
    assert _link(conn, RELAY, COPY) == again.signal_id

    echo = _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=4))
    assert echo.outcome == "deleted"
    assert _live(conn) == [again.signal_id]
    assert _queued_entries(conn) == [(again.signal_id, "acct-1"), (again.signal_id, "acct-2")]


# -- R4-1 (b): the original the withdrawing forward copied ---------------------------------


@pytest.mark.parametrize("order", ORDERS)
def test_the_vip_withdrawing_the_post_a_relay_forwarded_again_withdraws_the_relays_repost(
    conn, order  # noqa: F811
):
    """The relay removed its only forward of the VIP's call (its channel's
    last post: the call is voided, the C2 cost) and forwarded the same post
    again -- a new signal (R4-3). The VIP then deletes that post: the
    original poster retracted the call, which is withdrawn, with the usual
    warning."""
    _store_call(conn, order, forwards=True)
    assert _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=1)).outcome == "voided"
    again = _ingest(conn, RELAY, COPY + 1, posted=POSTED + timedelta(minutes=2), fwd=(V, CALL))
    assert again.outcome == "new"

    gone = _delete(conn, VIP, CALL, POSTED + timedelta(minutes=3))
    assert (gone.outcome, gone.signal_id) == ("voided", again.signal_id)
    assert _live(conn) == []
    assert _queued_entries(conn) == []
    assert ("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION, V, CALL, again.signal_id) in (
        _alerts(conn)
    )
    assert "CRITICAL" not in [level for level, _ in _codes(conn)]


@pytest.mark.parametrize("change", ["cancelled", "re-aimed"])
@pytest.mark.parametrize("order", ORDERS)
def test_the_vip_editing_away_the_post_a_relay_forwarded_again_withdraws_the_relays_repost(
    conn, order, change  # noqa: F811
):
    """The same, the VIP editing its post into "cancelled" or re-aiming it:
    the original poster retracting. The call the relay's repost carries is
    withdrawn as by any channel's last post -- VOIDED with the usual
    WARNING, or INVALID with the usual CRITICAL -- never with the doubt
    of an older post (no "only an old copy")."""
    _store_call(conn, order, forwards=True)
    assert _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=1)).outcome == "voided"
    again = _ingest(conn, RELAY, COPY + 1, posted=POSTED + timedelta(minutes=2), fwd=(V, CALL))
    assert again.outcome == "new"

    text = CANCELLED if change == "cancelled" else DIRECTION_EDIT
    gone = _edit(conn, VIP, CALL, text, POSTED + timedelta(minutes=3))
    conn.commit()
    assert _live(conn) == []
    assert _queued_entries(conn) == []
    if change == "cancelled":
        assert (gone.outcome, gone.signal_id) == ("voided", again.signal_id)
        code = edits_repo.SIGNAL_RETRACTED_BY_EDIT
        assert ("WARNING", code, V, CALL, again.signal_id) in _alerts(conn)
    else:
        assert (gone.outcome, gone.signal_id) == ("invalidated", again.signal_id)
        code = edits_repo.SIGNAL_INVALIDATED_BY_EDIT
        assert ("CRITICAL", code, V, CALL, again.signal_id) in _alerts(conn)
    assert edits_repo.OLD_POST_WITHDREW_CALL not in [code for _, code in _codes(conn)]
    assert "old copy" not in _detail(conn, code)


# -- R4-1 (c): an old hand copy removed -- ambiguous, so loud ------------------------------


@pytest.mark.parametrize("by", ["event", "window"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_hand_copy_removed_after_the_vip_posted_its_call_again_withdraws_it_loudly(
    conn, order, by  # noqa: F811
):
    """NEW-1 with a hand copy: nothing links the relay's post to the VIP's
    deleted one, so its removal may be a genuine retraction or only the
    relay tidying up its old copy. The call is withdrawn (the safe side)
    and a CRITICAL alert says which posts are involved, and to trade the
    call by hand if the relay only removed its old copy."""
    _store_call(conn, order)
    live = _vip_deletes_and_posts_again(conn)
    gone = _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=3), by=by)

    assert (gone.outcome, gone.signal_id) == ("voided", live)
    assert _live(conn) == []
    assert _queued_entries(conn) == []
    assert ("CRITICAL", edits_repo.OLD_POST_WITHDREW_CALL, R, COPY, live) in _alerts(conn)
    assert _codes(conn) == [
        ("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION),
        ("CRITICAL", edits_repo.OLD_POST_WITHDREW_CALL),
    ]
    detail = _detail(conn, edits_repo.OLD_POST_WITHDREW_CALL)
    assert f"message {COPY} on channel {R}" in detail
    assert f"message {CALL} on channel {V}" in detail  # the post that withdrew it first
    assert f"message {REPOST} on channel {V}" in detail and f"signal {live}" in detail
    assert "VOIDED" in detail and "trade it by hand" in detail


@pytest.mark.parametrize("order", ORDERS)
def test_a_hand_copy_removed_after_the_repost_was_taken_flags_it_loudly(
    conn, order  # noqa: F811
):
    """The same, after the repost's entry was taken: flagged, not closed,
    and the alert says the removal may only have been an old copy."""
    _store_call(conn, order)
    live = _vip_deletes_and_posts_again(conn)
    _take_entry(conn, live)
    gone = _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=3))

    assert (gone.outcome, gone.signal_id) == ("flagged", live)
    assert ("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED, R, COPY, live) in _alerts(conn)
    detail = _detail(conn, edits_repo.LIVE_SIGNAL_DELETED)
    assert f"message {CALL} on channel {V}" in detail and f"message {REPOST}" in detail
    assert "old copy" in detail and "keep the trade" in detail
    assert "trade it by hand" not in detail


@pytest.mark.parametrize("change", ["cancelled", "re-aimed"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_hand_copy_edited_away_after_the_vip_posted_its_call_again_withdraws_it_loudly(
    conn, order, change  # noqa: F811
):
    """NEW-1 (a): the relay edits its old copy into "cancelled" (or re-aims
    it) after the VIP's delete-and-repost. The same as a removal: the call
    is withdrawn, loudly, with the same explanation."""
    _store_call(conn, order)
    live = _vip_deletes_and_posts_again(conn)
    text = CANCELLED if change == "cancelled" else DIRECTION_EDIT
    result = _edit(conn, RELAY, COPY, text, POSTED + timedelta(minutes=3))
    conn.commit()

    assert _live(conn) == []
    assert _queued_entries(conn) == []
    if change == "cancelled":
        assert result.outcome == "voided"
        code = edits_repo.OLD_POST_WITHDREW_CALL
    else:
        assert result.outcome == "invalidated"
        code = edits_repo.SIGNAL_INVALIDATED_BY_EDIT
    assert ("CRITICAL", code, R, COPY, live) in _alerts(conn)
    detail = _detail(conn, code)
    assert f"message {CALL} on channel {V}" in detail and f"message {REPOST}" in detail
    assert "trade it by hand" in detail


# -- R4-2 / R4-3: every channel that withdrew the call may post it again -------------


@pytest.mark.parametrize("reposter", ["vip", "relay"], ids=["n4b", "H1b"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_channel_posting_again_a_call_it_withdrew_after_the_other_did_is_traded(
    conn, order, reposter  # noqa: F811
):
    """Both channels removed their only post of the call -- one withdrew it
    (the C2 cost), the other's removal came after -- and one posts it
    again: that channel withdrew the call too, so its repost is a new
    signal, whichever went first. n4 (b): the relay tidied up, the VIP
    deleted and posted again; H1b: the channels the other way round."""
    _store_call(conn, order)
    first, second = (RELAY, VIP) if reposter == "vip" else (VIP, RELAY)
    first_post, second_post = (COPY, CALL) if reposter == "vip" else (CALL, COPY)
    assert _delete(conn, first, first_post, POSTED + timedelta(minutes=1)).outcome == "voided"
    assert _delete(conn, second, second_post, POSTED + timedelta(minutes=2)).outcome == "deleted"
    again = _ingest(conn, second, second_post + 20, posted=POSTED + timedelta(minutes=3))

    assert again.outcome == "new"
    assert _promoted_from(conn, again.signal_id) == 1
    assert _live(conn) == [again.signal_id]
    assert _queued_entries(conn) == [(again.signal_id, "acct-1"), (again.signal_id, "acct-2")]
    assert relay_repo.COPY_OF_WITHDRAWN_SIGNAL not in [code for _, code in _codes(conn)]


@pytest.mark.parametrize("order", ORDERS)
def test_a_channel_still_showing_a_call_another_withdrew_is_held_back_posting_it_again(
    conn, order  # noqa: F811
):
    """Control: the relay tidied up (the call is voided) while the VIP's
    post still stands; the VIP posting the call again did not withdraw it,
    so the repost is held back loudly, as is a channel that never had it."""
    _store_call(conn, order)
    assert _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=1)).outcome == "voided"
    again = _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=2))
    other = _ingest(conn, OTHER, 8001, posted=POSTED + timedelta(minutes=3))

    assert (again.outcome, again.relay_of_signal_id) == ("relay_duplicate", 1)
    assert (other.outcome, other.relay_of_signal_id) == ("relay_duplicate", 1)
    assert _queued_entries(conn) == []
    alerts = _alerts(conn)
    assert ("CRITICAL", relay_repo.COPY_OF_WITHDRAWN_SIGNAL, V, REPOST, 1) in alerts
    assert ("CRITICAL", relay_repo.COPY_OF_WITHDRAWN_SIGNAL, O, 8001, 1) in alerts


@pytest.mark.parametrize("order", ORDERS)
def test_a_relay_forwarding_a_call_again_after_removing_its_only_forward_trades_it_once(
    conn, order  # noqa: F811
):
    """NEW-2: the relay removed its only forward of the VIP's call (the
    call is voided: the C2 cost) and forwarded the same post again -- the
    same as a hand repost: a new signal replacing the withdrawn one, which
    the VIP's own post then manages."""
    _store_call(conn, order, forwards=True)
    first = _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=1))
    assert first.outcome == "voided"
    again = _ingest(conn, RELAY, COPY + 1, posted=POSTED + timedelta(minutes=2), fwd=(V, CALL))

    assert again.outcome == "new"
    assert _promoted_from(conn, again.signal_id) == first.signal_id
    assert _live(conn) == [again.signal_id]
    assert _queued_entries(conn) == [(again.signal_id, "acct-1"), (again.signal_id, "acct-2")]
    assert relay_repo.COPY_OF_WITHDRAWN_SIGNAL not in [code for _, code in _codes(conn)]
    _the_vip_manages_the_trade(conn, again.signal_id)


@pytest.mark.parametrize("order", ORDERS)
def test_a_channel_whose_other_post_left_the_call_posting_it_again_is_traded(
    conn, order  # noqa: F811
):
    """The VIP re-aimed its repost (a repeat removal), then deleted its
    first post -- its channel's last post still showing the call, which is
    withdrawn. The re-aimed repost still reads as a signal, but not as this
    call: the VIP posting the call again withdrew it, and is traded."""
    _store_call(conn, order)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=1))
    assert _edit(conn, VIP, REPOST, DIRECTION_EDIT, POSTED + timedelta(minutes=2)).outcome == (
        "recorded"
    )
    conn.commit()
    assert _delete(conn, VIP, CALL, POSTED + timedelta(minutes=3)).outcome == "voided"
    again = _ingest(conn, VIP, 4020, posted=POSTED + timedelta(minutes=4))

    assert again.outcome == "new"
    assert _promoted_from(conn, again.signal_id) == 1
    assert _queued_entries(conn) == [(again.signal_id, "acct-1"), (again.signal_id, "acct-2")]


def test_a_forward_from_a_channel_that_never_had_the_call_is_held_back(conn):  # noqa: F811
    """Control: a forward of the withdrawn post by a channel with no post
    of the call is held back loudly."""
    _ingest(conn, VIP, CALL)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1))
    forward = _ingest(conn, RELAY, COPY, posted=POSTED + timedelta(minutes=2), fwd=(V, CALL))
    assert (forward.outcome, forward.relay_of_signal_id) == ("relay_duplicate", 1)
    assert ("CRITICAL", relay_repo.COPY_OF_WITHDRAWN_SIGNAL, R, COPY, 1) in _alerts(conn)


# -- R4-4: replies to the post that withdrew a call ------------------------------------


@pytest.mark.parametrize("when", ["before-the-repost", "after-the-repost"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_reply_to_the_post_that_re_aimed_a_call_targets_nothing_when_it_is_posted_again(
    conn, order, when  # noqa: F811
):
    """NEW-3: the VIP re-aimed its only post into a SELL (the call is
    INVALID) and posted the BUY again (a new signal replacing it). "Close
    half", replying to the SELL post, is about a call never traded: it
    targets nothing -- whether it was stored before the repost or after."""
    _store_call(conn, order)
    assert _edit(conn, VIP, CALL, DIRECTION_EDIT, EDIT_1).outcome == "invalidated"
    conn.commit()
    if when == "before-the-repost":
        _ingest(conn, VIP, 4005, CLOSE_HALF, posted=POSTED + timedelta(minutes=6), reply_to=CALL)
    again = _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=7))
    if when == "after-the-repost":
        _ingest(conn, VIP, 4005, CLOSE_HALF, posted=POSTED + timedelta(minutes=8), reply_to=CALL)

    assert again.outcome == "new"
    assert _live(conn) == [again.signal_id]
    assert _q(conn, "SELECT target_signal_id, resolution_path FROM followups") == [(None, None)]
    assert relay_repo.signal_for_post(conn, channel_id=V, message_id=CALL) == 1


@pytest.mark.parametrize("order", ORDERS)
def test_the_post_that_re_aimed_a_call_posted_again_is_never_an_older_post_of_it(
    conn, order  # noqa: F811
):
    """The VIP re-aimed its only post (the call is INVALID) and posted the
    BUY again. Edited again into chatter, the SELL post still speaks for the
    signal it closed, whichever post held it: a WARNING about that closed
    signal, naming the call posted in its place -- never the doubt of an
    older post of the call (it withdrew the signal itself), and nothing
    reaches the call posted again."""
    _store_call(conn, order)
    assert _edit(conn, VIP, CALL, DIRECTION_EDIT, EDIT_1).outcome == "invalidated"
    conn.commit()
    again = _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=6))
    assert again.outcome == "new"

    later = _edit(conn, VIP, CALL, CANCELLED, EDIT_2)
    conn.commit()
    assert (later.outcome, later.signal_id) == ("recorded", 1)
    assert _live(conn) == [again.signal_id]
    assert _queued_entries(conn) == [(again.signal_id, "acct-1"), (again.signal_id, "acct-2")]
    detail = _detail(conn, edits_repo.EDIT_OF_CLOSED_SIGNAL)
    assert "older post" not in detail and "old copy" not in detail
    assert "trade it by hand" not in detail
    assert f"signal {again.signal_id}" in detail


@pytest.mark.parametrize("order", ORDERS)
def test_a_reply_to_a_post_that_left_a_withdrawn_call_targets_nothing(
    conn, order  # noqa: F811
):
    """The VIP re-aimed its repost (a repeat removal: the call stood on its
    first post), then deleted the first post, withdrawing the call. A reply
    to the re-aimed post is about the SELL it shows, never traded: no
    target, like a reply to the post that withdrew the call."""
    _store_call(conn, order)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=1))
    _edit(conn, VIP, REPOST, DIRECTION_EDIT, POSTED + timedelta(minutes=2))
    conn.commit()
    assert _delete(conn, VIP, CALL, POSTED + timedelta(minutes=3)).outcome == "voided"
    _ingest(conn, VIP, 4005, CLOSE_HALF, posted=POSTED + timedelta(minutes=4), reply_to=REPOST)
    assert _q(conn, "SELECT target_signal_id FROM followups") == [(None,)]


@pytest.mark.parametrize("how", ["deleted", "chatter"])
def test_a_reply_to_the_post_that_withdrew_an_untraded_call_targets_nothing(
    conn, how  # noqa: F811
):
    """The VIP withdrew its only post of the call -- deleted it, or edited
    it into chatter; a reply to that post (the poll bringing it late) is
    about no trade."""
    _ingest(conn, VIP, CALL)
    if how == "deleted":
        _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1))
    else:
        _edit(conn, VIP, CALL, CHATTER_TEXT, POSTED + timedelta(minutes=1))
        conn.commit()
    _ingest(conn, VIP, 4005, CLOSE_HALF, posted=POSTED + timedelta(minutes=2), reply_to=CALL)
    assert _q(conn, "SELECT target_signal_id FROM followups") == [(None,)]


@pytest.mark.parametrize("order", ORDERS)
def test_a_reply_to_a_post_still_showing_a_withdrawn_call_keeps_it(conn, order):  # noqa: F811
    """Control: the relay withdrew the call (its only copy deleted); the
    VIP's post still shows it, and a reply to that post keeps the call's
    signal -- withdrawn, which Plan 3 sees."""
    _store_call(conn, order)
    assert _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=1)).outcome == "voided"
    _ingest(conn, VIP, 4005, CLOSE_HALF, posted=POSTED + timedelta(minutes=2), reply_to=CALL)
    assert _q(conn, "SELECT target_signal_id FROM followups") == [(1,)]


def test_a_reply_to_the_post_that_re_aimed_an_executed_call_keeps_that_call(conn):  # noqa: F811
    """Control: an executed call is never posted again in its place; a
    reply to its re-aimed post may be about its live legs, and keeps it."""
    _ingest(conn, VIP, CALL)
    _take_entry(conn, 1)
    assert _edit(conn, VIP, CALL, DIRECTION_EDIT, EDIT_1).outcome == "invalidated"
    conn.commit()
    _ingest(conn, VIP, 4005, CLOSE_HALF, posted=POSTED + timedelta(minutes=6), reply_to=CALL)
    assert _q(conn, "SELECT target_signal_id, resolution_path FROM followups") == [(1, "reply")]


def test_a_reply_made_before_its_post_withdrew_the_call_stays_with_that_signal(
    conn,  # noqa: F811
):
    """R4-4: "close half" replied to the VIP's post while it was the call;
    the VIP then deleted that post and posted the call again. The reply is
    about the signal its post carried: it is not moved to the new one (and
    Plan 3 resolves no further, because it replies to that signal's
    withdrawing post)."""
    _ingest(conn, VIP, CALL)
    reply = _ingest(conn, VIP, 4005, CLOSE_HALF, posted=POSTED + timedelta(minutes=1), reply_to=CALL)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=2))
    again = _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=3))
    assert again.outcome == "new"
    assert _q(
        conn, "SELECT target_signal_id, resolution_path FROM followups WHERE followup_id = ?",
        reply.followup_id,
    ) == [(1, "reply")]


# -- R4-5: a read-back withdrawing post stands; a replaced call stays protected -------------


@pytest.mark.parametrize("edited", [False, True], ids=["as-posted", "edited"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_post_read_back_after_a_false_window_deletion_shows_the_call_again(
    conn, order, edited  # noqa: F811
):
    """The window inferred the VIP's first post deleted (VIP-first it
    withdrew signal 1 and the VIP's repost was promoted; relay-first it was
    a repeat removal), then read it back -- as posted (no edit date: the
    same version as stored), or edited since. The VIP deleting its repost
    now leaves the call standing -- the first post shows it -- and deleting
    the first post too, its channel's last, withdraws the call."""
    _store_call(conn, order)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=1))
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=2), by="window")
    back = edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=CALL, text=SIGNAL_TEXT + (" " if edited else ""),
        posted_at_utc=POSTED, edit_date_utc=POSTED + timedelta(minutes=3) if edited else None,
        reply_to_message_id=None, now_utc=POSTED + timedelta(minutes=3), accounts=ACCOUNTS,
        detected_by="window",
    )
    conn.commit()
    assert back.reversed_deletion
    (live,) = _live(conn)

    repost = _delete(conn, VIP, REPOST, POSTED + timedelta(minutes=4))
    assert (repost.outcome, _live(conn)) == ("deleted", [live])
    assert _queued_entries(conn) == [(live, "acct-1"), (live, "acct-2")]
    assert ("WARNING", edits_repo.SIGNAL_REPEAT_REMOVED, V, REPOST, live) in _alerts(conn)

    first = _delete(conn, VIP, CALL, POSTED + timedelta(minutes=5))
    assert (first.outcome, first.signal_id) == ("voided", live)
    assert _live(conn) == []


@pytest.mark.parametrize("change", ["chatter", "re-aimed"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_post_edited_back_into_the_call_shows_it_again_whichever_post_holds_it(
    conn, order, change  # noqa: F811
):
    """The VIP double-posted, took its first post out of the call (VIP-first
    it withdrew signal 1 and the repost was promoted; relay-first it was a
    repeat removal) and edited it back. It shows the call again, in either
    order: the VIP deleting its repost leaves the call standing, and
    deleting the first post too, its channel's last, withdraws it. Edits
    never open trades: nothing more is queued, and no alert says to trade
    the call by hand."""
    _store_call(conn, order)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=1))
    _edit(conn, VIP, CALL, _REMOVALS[change], EDIT_1)
    back = _edit(conn, VIP, CALL, SIGNAL_TEXT, EDIT_2)
    conn.commit()
    (live,) = _live(conn)
    assert back.outcome in ("reclassified", "cosmetic", "recorded")
    assert _queued_entries(conn) == [(live, "acct-1"), (live, "acct-2")]
    assert relay_repo.signal_for_post(conn, channel_id=V, message_id=CALL) == live

    repost = _delete(conn, VIP, REPOST, POSTED + timedelta(minutes=12))
    assert (repost.outcome, _live(conn)) == ("deleted", [live])
    first = _delete(conn, VIP, CALL, POSTED + timedelta(minutes=13))
    assert (first.outcome, _live(conn)) == ("voided", [])
    for (detail,) in _q(conn, "SELECT detail FROM alerts"):
        assert "by hand" not in detail, detail


def _read_back(conn, channel, message_id: int, at) -> None:  # noqa: F811
    """The window reads back a post it had inferred deleted, as posted."""
    back = edits_repo.apply_edit(
        conn, channel_cfg=channel, message_id=message_id, text=SIGNAL_TEXT,
        posted_at_utc=POSTED, edit_date_utc=None, reply_to_message_id=None, now_utc=at,
        accounts=ACCOUNTS, detected_by="window",
    )
    conn.commit()
    assert back.reversed_deletion


@pytest.mark.parametrize("timing", ["read-back-first", "repost-first"])
@pytest.mark.parametrize("relay", ["no-relay", *ORDERS])
def test_a_post_read_back_stands_again_whether_the_call_was_posted_again_before_or_after(
    conn, relay, timing  # noqa: F811
):
    """One channel is enough: the window inferred the VIP's only post of the
    call deleted (its last post: the call is voided) and the VIP posted the
    call again (a new signal, traded). The window also reads the first post
    back -- before the repost or after it. Either way the VIP's channel
    shows the call twice, so the first post stands again once a newer
    signal carries the call: the VIP deleting its repost leaves the call
    queued -- a repeat removal, or (relay-first, where the first post holds
    no signal of its own) that post promoted in the repost's place -- and
    deleting the first post too, its channel's last, withdraws it. (With the
    read-back first, the first post had to stay the post that withdrew the
    call until then, or the repost would have been held back against it.)"""
    if relay == "no-relay":
        _ingest(conn, VIP, CALL)
    else:
        _store_call(conn, relay)
    assert _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1), by="window").outcome in (
        "voided", "deleted"
    )
    if timing == "read-back-first":
        _read_back(conn, VIP, CALL, POSTED + timedelta(minutes=2))
    again = _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=3))
    if timing == "repost-first":
        _read_back(conn, VIP, CALL, POSTED + timedelta(minutes=4))
    assert again.outcome == "new"
    assert _live(conn) == [again.signal_id]
    assert relay_repo.signal_for_post(conn, channel_id=V, message_id=CALL) == again.signal_id

    critical = [code for level, code in _codes(conn) if level == "CRITICAL"]
    repost = _delete(conn, VIP, REPOST, POSTED + timedelta(minutes=5))
    (live,) = _live(conn)
    assert _queued_entries(conn) == [(live, "acct-1"), (live, "acct-2")]
    if relay == "relay-first":
        assert (repost.outcome, repost.promoted_signal_id) == ("voided", live)
        assert relay_repo.live_signal_for(conn, again.signal_id) == live
    else:
        assert (repost.outcome, live) == ("deleted", again.signal_id)
        assert ("WARNING", edits_repo.SIGNAL_REPEAT_REMOVED, V, REPOST, live) in _alerts(conn)
    assert [code for level, code in _codes(conn) if level == "CRITICAL"] == critical

    first = _delete(conn, VIP, CALL, POSTED + timedelta(minutes=6))
    assert (first.outcome, first.signal_id) == ("voided", live)
    assert _live(conn) == []


@pytest.mark.parametrize("order", ORDERS)
def test_another_channels_post_read_back_stays_with_the_signal_it_withdrew(
    conn, order  # noqa: F811
):
    """Only the channel posting the call again has its own posts re-read
    (with its own parser) when it does: the window inferred the relay's
    only copy deleted (the call is voided) and read it back; the VIP then
    deleted its post and posted the call again. The relay's post still
    speaks for the signal it withdrew: a third channel's forward of it,
    removed, only echoes it (R4-1 a), and when the relay really deletes it,
    the VIP's new call is left trading."""
    _store_call(conn, order)
    assert _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=1), by="window").outcome == (
        "voided"
    )
    _read_back(conn, RELAY, COPY, POSTED + timedelta(minutes=2))
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=3))
    again = _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=4))
    assert again.outcome == "new"
    assert relay_repo.signal_for_post(conn, channel_id=R, message_id=COPY) == 1

    forward = _ingest(conn, OTHER, 8001, posted=POSTED + timedelta(minutes=5), fwd=(R, COPY))
    assert forward.outcome == "relay_duplicate"
    assert _delete(conn, OTHER, 8001, POSTED + timedelta(minutes=6)).outcome == "deleted"
    assert _live(conn) == [again.signal_id]

    gone = _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=7))
    assert gone.signal_id == 1
    assert _live(conn) == [again.signal_id]
    assert _queued_entries(conn) == [(again.signal_id, "acct-1"), (again.signal_id, "acct-2")]


@pytest.mark.parametrize("order", ORDERS)
def test_a_call_voided_by_a_false_window_deletion_is_traded_when_posted_again(
    conn, order  # noqa: F811
):
    """Control: the window inferred the VIP's only post deleted -- its
    channel's last, so the call was voided -- and then read it back. Nothing
    carries the call in its place, so that post is still the one that
    withdrew it: the VIP posting the call again is a new signal, traded
    (never held back against a call its own channel withdrew)."""
    _store_call(conn, order)
    assert _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1), by="window").outcome == (
        "voided"
    )
    assert _edit(conn, VIP, CALL, SIGNAL_TEXT, POSTED + timedelta(minutes=2)).reversed_deletion
    conn.commit()
    again = _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=3))

    assert again.outcome == "new"
    assert _promoted_from(conn, again.signal_id) == 1
    assert _queued_entries(conn) == [(again.signal_id, "acct-1"), (again.signal_id, "acct-2")]


@pytest.mark.parametrize("how", ["promoted", "posted-again"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_replaced_call_keeps_its_stale_post_protection(conn, order, how):  # noqa: F811
    """NEW-4 (b): the VIP's repost carries the call -- promoted over its
    deleted first post, or posted again after it -- and the relay's copy
    moves the stop. The VIP's repost, which never did, then edits only its
    TP1: that must not put the old stop back, whichever post holds the
    call. Posted again, the relay's copy still points at the signal the
    repost replaced: the call is relayed through its chain of signals."""
    _store_call(conn, order)
    if how == "promoted":
        _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=1))
        _delete(conn, VIP, CALL, POSTED + timedelta(minutes=2))
    else:
        _vip_deletes_and_posts_again(conn)
    (live,) = _live(conn)
    _take_entry(conn, live)
    assert _edit(conn, RELAY, COPY, SL_4480, EDIT_1).outcome == "reparsed"
    stale = _edit(conn, VIP, REPOST, TP1_EDIT, EDIT_2)
    conn.commit()

    assert stale.outcome == "recorded"
    ((sl, tps),) = _q(
        conn, "SELECT json_extract(parsed_json, '$.sl'), json_extract(parsed_json, '$.tps') "
        "FROM signals WHERE signal_id = ?", live,
    )
    assert (sl, json.loads(tps)) == (4480.0, [4490.0, 4495.0, 4500.0, 4505.0])
    assert _q(
        conn, "SELECT op, json_extract(parsed_json, '$.price_hint') FROM followups "
        "WHERE edit_seq > 0"
    ) == [("amend_sl", 4480.0)]
    assert ("WARNING", edits_repo.SIGNAL_COPY_OUT_OF_DATE, V, REPOST, live) in _alerts(conn)


# -- R4-6: a forward showing another call ---------------------------------------------------


def test_a_forward_of_a_post_re_aimed_away_from_a_reposted_call_is_held_back(
    conn,  # noqa: F811
):
    """NEW-5: the VIP re-aimed its first post into a SELL while its repost
    kept the BUY (promoted). A forward of the SELL post shows another call
    than the live BUY: it is held back against the signal that post
    closed, loudly -- never linked silently to the BUY."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=1))
    assert _edit(conn, VIP, CALL, DIRECTION_EDIT, EDIT_1).promoted_signal_id == 2
    conn.commit()
    forward = _ingest(
        conn, RELAY, COPY, DIRECTION_EDIT, posted=POSTED + timedelta(minutes=6), fwd=(V, CALL)
    )

    assert (forward.outcome, forward.relay_of_signal_id) == ("relay_duplicate", 1)
    assert ("CRITICAL", relay_repo.COPY_OF_WITHDRAWN_SIGNAL, R, COPY, 1) in _alerts(conn)
    assert _queued_entries(conn) == [(2, "acct-1"), (2, "acct-2")]


@pytest.mark.parametrize("reaimed", [CALL, REPOST], ids=["first-post", "repost"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_forward_of_a_post_re_aimed_away_is_never_traded_whichever_post_holds_the_call(
    conn, order, reaimed  # noqa: F811
):
    """The VIP double-posted the call (the relay copied it too) and
    re-aimed one of its posts into a SELL; a third channel forwards the
    SELL post. Whichever post holds the call and whichever was re-aimed,
    the call is traded once, the forward is never traded, and its removal
    and the replies to it change nothing.

    The alert is not order-independent (a known limitation in relay_repo):
    when the re-aim closed the call's signal (the VIP's first post held it)
    the forward is held back against that signal with CRITICAL
    COPY_OF_WITHDRAWN_SIGNAL (R4-6); when it was a repeat removal nothing
    was closed, and the forward is linked to the live call with an INFO
    line only -- as the VIP's own re-aim got only a WARNING then."""
    _store_call(conn, order)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=1))
    _edit(conn, VIP, reaimed, DIRECTION_EDIT, EDIT_1)
    conn.commit()
    (live,) = _live(conn)
    forward = _ingest(
        conn, OTHER, 8001, DIRECTION_EDIT, posted=POSTED + timedelta(minutes=6), fwd=(V, reaimed)
    )

    assert forward.outcome == "relay_duplicate"
    assert _queued_entries(conn) == [(live, "acct-1"), (live, "acct-2")]
    closed = order == "vip-first" and reaimed == CALL
    held_back = ("CRITICAL", relay_repo.COPY_OF_WITHDRAWN_SIGNAL, O, 8001, 1)
    assert (held_back in _alerts(conn)) == closed
    alerts = _alerts(conn)

    reply = _ingest(
        conn, OTHER, 8002, CLOSE_HALF, posted=POSTED + timedelta(minutes=7), reply_to=8001
    )
    assert _q(
        conn, "SELECT target_signal_id FROM followups WHERE followup_id = ?", reply.followup_id
    ) == [(None,)]
    assert _delete(conn, OTHER, 8001, POSTED + timedelta(minutes=8)).outcome == "deleted"
    assert _live(conn) == [live]
    assert _queued_entries(conn) == [(live, "acct-1"), (live, "acct-2")]
    assert _alerts(conn) == alerts


# == the arrival paths: the real listener and poll, the fake Telegram ===========

#: The VIP channel's bare id, as a forward header names it.
_V_BARE = -V - 10**12


def _forward_of_call(message_id: int, *, after: timedelta) -> FakeMessage:
    """The relay's forward of the VIP's call V/4001."""
    return FakeMessage(
        id=message_id,
        message=SIGNAL_TEXT,
        date=POSTED + after,
        fwd_from=types.MessageFwdHeader(
            date=POSTED, from_id=types.PeerChannel(channel_id=_V_BARE), channel_post=CALL
        ),
    )


async def _vip_and_relay(client, order: str, *, forwards: bool) -> None:
    """The VIP's call and chatter, the relay's copy (a forward, or by hand)
    and chatter: VIP-first, or the relay's copy delivered first."""
    copy = _forward_of_call(COPY, after=timedelta(seconds=30)) if forwards else _post(
        COPY, after=timedelta(seconds=30)
    )
    if order == "vip-first":
        assert await client.emit(V, _post(CALL))
        assert await client.emit(R, copy)
    else:
        assert await client.emit(R, copy)
        assert await client.emit(V, _post(CALL))
    for message_id in VIP_CHATTER:
        assert await client.emit(V, _post(message_id, CHATTER_TEXT))
    for message_id in (COPY + 1, COPY + 2, COPY + 3):
        assert await client.emit(R, _post(message_id, CHATTER_TEXT, after=timedelta(minutes=1)))


@pytest.mark.parametrize("copies", ["forward", "hand"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_relays_copy_removed_unseen_after_the_vip_posted_again_through_the_listener(
    tmp_path, order, copies
):
    """NEW-1 end to end: the VIP deletes its call and posts it again; the
    relay's removal of its copy is dropped and found later by the window
    re-read. A forward's removal is an echo -- the repost still trades; a
    hand copy's withdraws the repost, loudly."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_and_relay(client, order, forwards=copies == "forward")
        await _round(listener, time)
        assert await client.emit_delete(V, CALL)
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=2)))
        await _round(listener, time)
        (live,) = _live_rows(db_path)
        with client.blackout():
            assert not await client.emit_delete(R, COPY)
        for _ in range(3):
            await _round(listener, time)
        return live

    live = _run(db_path, body)

    codes = _db_rows(db_path, "SELECT level, code, message_id FROM alerts ORDER BY alert_id")
    queued = _db_rows(
        db_path, "SELECT signal_id, COUNT(*) FROM outbox WHERE kind = 'ENTRY' AND state = 'QUEUED' "
        "GROUP BY signal_id"
    )
    if copies == "forward":
        assert _live_rows(db_path) == [live]
        assert queued == [(live, 2)]
        assert [code for _, code, _ in codes] == [edits_repo.SIGNAL_VOIDED_BY_DELETION]
    else:
        assert _live_rows(db_path) == []
        assert queued == []
        assert ("CRITICAL", edits_repo.OLD_POST_WITHDREW_CALL, COPY) in codes


# == Round 5: a channel's forward of its own post, and mutual forwards =========
#
# Ruling R5-1 (X1): a forward of a post of the SAME channel is that
# channel's own post -- a "bump" of its call -- never an echo of another
# channel's removal. Ruling R5-2 (X3): a forward of a post removed as a
# repeat (it closed nothing) echoes that removal only while the forwarded
# post's channel still shows the call; once that channel's last post is
# gone too, removing the forward is a removal like any other.

#: The VIP's forward of its own call V/4001 (a "bump").
BUMP = 4002


def _vip_bumps_its_call(conn, *, executed: bool) -> None:  # noqa: F811
    _ingest(conn, VIP, CALL)
    bump = _ingest(conn, VIP, BUMP, posted=POSTED + timedelta(minutes=1), fwd=(V, CALL))
    assert (bump.outcome, bump.relay_of_signal_id) == ("relay_duplicate", 1)
    if executed:
        _take_entry(conn, 1)


def _withdrawn_everywhere(conn, *, executed: bool, signal_id: int) -> None:  # noqa: F811
    """Nothing of the call is claimable any more; an executed call carries
    a CRITICAL flag."""
    assert _live(conn) == []
    assert _queued_entries(conn) == []
    assert _entries_claimable(conn) == []
    if executed:
        assert ("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED) in _codes(conn)
        assert _q(
            conn, "SELECT state, withdrawn_at_utc IS NOT NULL FROM signals WHERE signal_id = ?",
            signal_id,
        ) == [("NEW", 1)]


@pytest.mark.parametrize("by", ["event", "window"])
@pytest.mark.parametrize("executed", [False, True], ids=["pending", "executed"])
def test_a_channels_forward_of_its_own_call_removed_last_withdraws_the_call(
    conn, executed, by  # noqa: F811
):
    """X1, one channel: the VIP forwards its own call (a bump), deletes the
    original, then deletes the bump -- its last post of the call. The bump
    is the VIP's own post, not an echo of another channel: the call is
    withdrawn (VOIDED, or a CRITICAL flag once executed), nothing left to
    claim."""
    _vip_bumps_its_call(conn, executed=executed)
    first = _delete(conn, VIP, CALL, POSTED + timedelta(minutes=2), by=by)
    if executed:
        assert (first.outcome, first.promoted_signal_id) == ("deleted", None)  # a repeat
    else:
        assert (first.outcome, first.promoted_signal_id) == ("voided", 2)

    last = _delete(conn, VIP, BUMP, POSTED + timedelta(minutes=3), by=by)

    assert (last.outcome, last.signal_id) == (
        ("flagged", 1) if executed else ("voided", 2)
    )
    _withdrawn_everywhere(conn, executed=executed, signal_id=1)


@pytest.mark.parametrize("how", ["re-aimed", "chatter"])
@pytest.mark.parametrize("executed", [False, True], ids=["pending", "executed"])
def test_a_channels_bump_removed_after_its_original_left_the_call_withdraws_it(
    conn, executed, how  # noqa: F811
):
    """X1 variants: the VIP takes its original out of the call (re-aimed, or
    edited into chatter) while its bump stands -- the bump is promoted, or
    keeps the executed trade -- and then deletes the bump, its last post of
    the call: the call is withdrawn."""
    _vip_bumps_its_call(conn, executed=executed)
    _edit(conn, VIP, CALL, _REMOVALS[how], POSTED + timedelta(minutes=2))
    conn.commit()
    live = 1 if executed else 2
    assert _live(conn) == [live]

    last = _delete(conn, VIP, BUMP, POSTED + timedelta(minutes=3))

    assert (last.outcome, last.signal_id) == ("flagged" if executed else "voided", live)
    _withdrawn_everywhere(conn, executed=executed, signal_id=1)


@pytest.mark.parametrize("executed", [False, True], ids=["pending", "executed"])
def test_a_channels_bump_deleted_with_its_original_withdraws_the_call(conn, executed):  # noqa: F811
    """X1 control: the original and its bump deleted in one report."""
    _vip_bumps_its_call(conn, executed=executed)
    both = (CALL, BUMP)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=2), also=both)
    _delete(conn, VIP, BUMP, POSTED + timedelta(minutes=2), also=both)
    _withdrawn_everywhere(conn, executed=executed, signal_id=1)


@pytest.mark.parametrize("executed", [False, True], ids=["pending", "executed"])
@pytest.mark.parametrize("order", ORDERS)
def test_two_channels_forwarding_each_others_posts_withdraw_the_call_when_all_go(
    conn, order, executed  # noqa: F811
):
    """X3: the VIP double-posts (V/4001, V/4002); the relay forwards V/4002
    (R/7001) and copies the call by hand (R/7002); the VIP forwards R/7002
    (V/4003). Every post is deleted, one at a time: V/4002 and R/7002 are
    repeats, V/4001 goes while V/4003 stands, and then each channel's last
    post. R/7001 is removed while the VIP still shows the call -- an echo
    of V/4002's removal -- but V/4003, a forward of R/7002, goes after the
    relay's last post: the call is withdrawn, nothing left to claim."""
    vip_first = order == "vip-first"
    posts = [
        (VIP, CALL, 0, None),
        (VIP, 4002, 20, None),
        (RELAY, 7001, 40, (V, 4002)),
        (RELAY, 7002, 60, None),
        (VIP, 4003, 80, (R, 7002)),
    ]
    stored = posts if vip_first else [posts[3], *posts[:3], posts[4]]
    for channel, message_id, after, fwd in stored:
        _ingest(conn, channel, message_id, posted=POSTED + timedelta(seconds=after), fwd=fwd)
    assert len(_live(conn)) == 1
    (holder,) = _live(conn)
    if executed:
        _take_entry(conn, holder)

    for step, (channel, message_id) in enumerate(
        [(VIP, 4002), (RELAY, 7002), (VIP, CALL), (RELAY, 7001), (VIP, 4003)]
    ):
        _delete(conn, channel, message_id, POSTED + timedelta(minutes=2 + step))

    _withdrawn_everywhere(conn, executed=executed, signal_id=holder)


@pytest.mark.parametrize("via", ["event", "window"])
@pytest.mark.parametrize("executed", [False, True], ids=["pending", "executed"])
def test_a_channels_bump_removed_last_through_the_listener_withdraws_the_call(
    tmp_path, executed, via
):
    """X1 end to end: the VIP forwards its own call through Telegram (a
    forward header naming its own channel), then deletes the original and
    the bump, one report each -- seen by the event, or inferred by the
    window re-read. Nothing is left to claim."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        assert await client.emit(V, _post(CALL))
        assert await client.emit(V, _forward_of_call(BUMP, after=timedelta(minutes=1)))
        for message_id in (BUMP + 1, BUMP + 2, BUMP + 3):  # chatter: a window re-read to believe
            assert await client.emit(
                V, _post(message_id, CHATTER_TEXT, after=timedelta(minutes=1))
            )
        await _round(listener, time)
        if executed:
            _db_write(
                db_path,
                "UPDATE outbox SET state = 'DONE' WHERE kind = 'ENTRY' "
                "AND account_id = 'acct-1' AND signal_id = 1",
            )
        for message_id in (CALL, BUMP):
            if via == "event":
                assert await client.emit_delete(V, message_id)
                await _round(listener, time)
            else:
                with client.blackout():
                    assert not await client.emit_delete(V, message_id)
                for _ in range(3):
                    await _round(listener, time)

    _run(db_path, body, channels=(VIP,))

    assert _db_rows(
        db_path, "SELECT signal_id FROM outbox WHERE kind = 'ENTRY' AND state = 'QUEUED'"
    ) == []
    assert _live_rows(db_path) == []
    assert _claim(db_path) is None
    if executed:
        assert _db_rows(
            db_path, "SELECT code FROM alerts WHERE level = 'CRITICAL' AND code = ?",
            (edits_repo.LIVE_SIGNAL_DELETED,),
        ) == [(edits_repo.LIVE_SIGNAL_DELETED,)]


# == Round 5 (ruling R5-3): a promotion is no withdrawal ========================
#
# The R4-4 carve-out -- a reply to the post that closed a signal stays with
# that signal -- holds for the closer of a WITHDRAWN call (its channel's
# last post) and for a post re-aimed to another call. A holder removed
# while its own channel still showed the call was replaced by a promotion:
# the call never stopped, so a reply to that post reaches the promoted
# signal, whether it was stored before the removal or after.

MOVE_SL_TO_BE = "Move SL to BE"
#: How the VIP takes its first post out of the call while its double stands.
_PROMOTIONS = {"deleted": None, "chatter": CHATTER_TEXT, "re-aimed": DIRECTION_EDIT}


def _followup_target(conn, message_id: int) -> tuple:  # noqa: F811
    (row,) = _q(
        conn, "SELECT target_signal_id, resolution_path FROM followups WHERE message_id = ?",
        message_id,
    )
    return row


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("how", list(_PROMOTIONS))
def test_a_reply_to_a_post_replaced_by_a_promotion_reaches_the_promoted_call(
    conn, how, when  # noqa: F811
):
    """X4, one channel: the VIP double-posts its call, a "move SL to break
    even" replies to the first post, and the first post goes -- the double
    is promoted in its place. The call never stopped: an instruction given
    while the post showed it targets the promoted signal. Deleted or
    edited into chatter, so does one stored after. Re-aimed, the first
    post shows another call now: a reply to it stored after the re-aim is
    about no trade and gets no target -- as when the re-aimed post held no
    signal and the call simply stood (INV-2)."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(seconds=30))
    reply_at = POSTED + timedelta(minutes=1 if when == "before" else 3)
    if when == "before":
        _ingest(conn, VIP, 4005, MOVE_SL_TO_BE, posted=reply_at, reply_to=CALL)
    moment = POSTED + timedelta(minutes=2)
    if how == "deleted":
        gone = _delete(conn, VIP, CALL, moment)
    else:
        gone = _edit(conn, VIP, CALL, _PROMOTIONS[how], moment)
        conn.commit()
    assert gone.promoted_signal_id == 2
    if when == "after":
        _ingest(conn, VIP, 4005, MOVE_SL_TO_BE, posted=reply_at, reply_to=CALL)

    if how == "re-aimed" and when == "after":
        assert _followup_target(conn, 4005) == (None, None)
    else:
        assert _followup_target(conn, 4005) == (2, relay_repo.RELAY_REPLY_PATH)
    assert _live(conn) == [2]


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_reply_to_a_relays_tidied_up_forward_reaches_the_live_call_in_either_order(
    conn, order, when  # noqa: F811
):
    """X5, two channels: the relay forwards the VIP's call (R/7001) and
    also posts it by hand (R/7002); "close half" replies to R/7001, and the
    relay deletes R/7001 -- a tidy-up, its channel still shows the call.
    VIP-first that is a repeat removal; relay-first R/7001 held the signal
    and R/7002 is promoted. Either way the instruction reaches the live
    call, stored before the deletion or after."""
    _store_call(conn, order, forwards=True)
    _ingest(conn, RELAY, 7002, posted=POSTED + timedelta(minutes=1))
    reply_at = POSTED + timedelta(minutes=2 if when == "before" else 4)
    if when == "before":
        _ingest(conn, RELAY, 7010, CLOSE_HALF, posted=reply_at, reply_to=COPY)
    _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=3))
    if when == "after":
        _ingest(conn, RELAY, 7010, CLOSE_HALF, posted=reply_at, reply_to=COPY)

    (live,) = _live(conn)
    assert live == (1 if order == "vip-first" else 2)
    target, path = _followup_target(conn, 7010)
    assert (target, path) == (live, relay_repo.RELAY_REPLY_PATH)
    assert relay_repo.live_signal_for(conn, target) == live


# == Round 5 (ruling R5-4): a held-back copy is no post of the call ============


@pytest.mark.parametrize("copies", ["hand", "forward"])
def test_a_channel_whose_only_post_was_held_back_cannot_repost_a_withdrawn_call(
    conn, copies  # noqa: F811
):
    """X2: the VIP posts a call and deletes it (VOIDED). Another channel,
    which never carried the call, posts it a minute later -- held back
    with a CRITICAL alert -- deletes that copy and posts it again. That
    channel never withdrew the call: the VIP did. Its repost is held back
    too, loudly; nothing is traded, and the first CRITICAL stays standing."""
    _ingest(conn, VIP, CALL)
    assert _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1)).outcome == "voided"
    fwd = (V, CALL) if copies == "forward" else None
    held = _ingest(conn, OTHER, 8001, posted=POSTED + timedelta(minutes=2), fwd=fwd)
    assert (held.outcome, held.relay_of_signal_id) == ("relay_duplicate", 1)
    assert _delete(conn, OTHER, 8001, POSTED + timedelta(minutes=3)).outcome == "deleted"

    again = _ingest(conn, OTHER, 8002, posted=POSTED + timedelta(minutes=4), fwd=fwd)

    assert (again.outcome, again.relay_of_signal_id) == ("relay_duplicate", 1)
    assert _q(conn, "SELECT signal_id, state FROM signals") == [(1, "VOIDED")]
    assert _queued_entries(conn) == []
    assert _entries_claimable(conn) == []
    assert _q(
        conn, "SELECT message_id, acked FROM alerts WHERE code = ? ORDER BY alert_id",
        relay_repo.COPY_OF_WITHDRAWN_SIGNAL,
    ) == [(8001, 0), (8002, 0)]
    assert relay_repo.COPY_OF_WITHDRAWN_SIGNAL_SUPERSEDED not in [c for _, c in _codes(conn)]


def test_a_held_back_copy_that_came_to_repeat_the_call_is_a_post_of_it(conn):  # noqa: F811
    """R5-4's limit: a copy held back against a withdrawn call is no post
    of the call its channel had -- until the call is posted again, when it
    repeats the new signal (COPY_OF_WITHDRAWN_SIGNAL_SUPERSEDED) and its
    channel shows the call like any other. The VIP deletes its call
    (VOIDED); channel C copies it (held back); the VIP posts it again
    (signal 2: C's copy now repeats it) and deletes that too, its last
    post (VOIDED). C then deletes its copy -- its last post of the call --
    and posts the call again: like every channel whose last post of the
    call was withdrawn (ruling R4-2), C posting it again is a new signal,
    traded."""
    _ingest(conn, VIP, CALL)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1))
    held = _ingest(conn, OTHER, 8001, posted=POSTED + timedelta(minutes=2))
    assert (held.outcome, held.relay_of_signal_id) == ("relay_duplicate", 1)
    again = _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=3))
    assert again.outcome == "new" and _link(conn, OTHER, 8001) == again.signal_id
    assert _delete(conn, VIP, REPOST, POSTED + timedelta(minutes=4)).outcome == "voided"
    _delete(conn, OTHER, 8001, POSTED + timedelta(minutes=5))

    third = _ingest(conn, OTHER, 8002, posted=POSTED + timedelta(minutes=6))

    assert third.outcome == "new", third
    assert _promoted_from(conn, third.signal_id) == again.signal_id
    assert _live(conn) == [third.signal_id]
    assert _queued_entries(conn) == [(third.signal_id, "acct-1"), (third.signal_id, "acct-2")]
    assert _q(
        conn, "SELECT message_id FROM alerts WHERE code = ? AND acked = 0",
        relay_repo.COPY_OF_WITHDRAWN_SIGNAL,
    ) == []


# == Round 5 (ruling R5-6): what the invariant tests found =====================
#
# tests/test_relay_invariants.py checks, on generated scenarios, that a call
# every post of which is withdrawn is never traded (INV-1), that storage
# order changes nothing (INV-2) and that no account can trade a call twice
# (INV-3). These pin the scenarios its first runs found.


@pytest.mark.parametrize("first", [CALL, REPOST], ids=["holder-first", "double-first"])
@pytest.mark.parametrize("how", ["re-aimed", "chatter"])
def test_a_double_post_deleted_with_its_holder_that_had_left_the_call_withdraws_it(
    conn, how, first  # noqa: F811
):
    """Retraction seed 1549, one channel: the VIP double-posts, acct-1 takes
    the entry, and the VIP takes the first post out of the call (re-aimed or
    edited into chatter: a repeat removal, the trade continues). Then both
    posts are deleted in one report. The first post's deletion says nothing
    -- it had left the call -- so the double's deletion, the channel's last
    post of the call, must say it, whichever job runs first: a CRITICAL
    flag, and acct-2's entry skipped."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(seconds=30))
    _take_entry(conn, 1)
    _edit(conn, VIP, CALL, _REMOVALS[how], POSTED + timedelta(minutes=1))
    conn.commit()
    assert _live(conn) == [1]

    both = (CALL, REPOST)
    for message_id in (first, REPOST if first == CALL else CALL):
        _delete(conn, VIP, message_id, POSTED + timedelta(minutes=2), also=both)

    _withdrawn_everywhere(conn, executed=True, signal_id=1)


@pytest.mark.parametrize("order", ORDERS)
def test_a_call_posted_again_by_forwarding_a_post_the_other_channel_broke_is_withdrawn_last(
    conn, order  # noqa: F811
):
    """Order seed 6299: the relay's copy is edited into broken text (its
    last post: signal 1 INVALID), the VIP deletes its post, the relay
    edits its copy back, and the VIP forwards that copy twice -- the VIP
    posting the call again (signal 2). The VIP deletes both forwards and
    the relay re-aims its copy. Each forward's removal looked like an echo
    of the relay's post that broke signal 1; the VIP's last one is not:
    nothing of the call is left to claim."""
    _store_call(conn, order)
    _edit(conn, RELAY, COPY, UNPARSEABLE_EDIT, POSTED + timedelta(minutes=1))
    conn.commit()
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=2))
    _edit(conn, RELAY, COPY, SIGNAL_TEXT, POSTED + timedelta(minutes=3))
    conn.commit()
    first = _ingest(conn, VIP, 4002, posted=POSTED + timedelta(minutes=4), fwd=(R, COPY))
    assert first.outcome == "new"
    _ingest(conn, VIP, 4003, posted=POSTED + timedelta(minutes=5), fwd=(R, COPY))
    assert _live(conn) == [first.signal_id]

    _delete(conn, VIP, 4002, POSTED + timedelta(minutes=6), by="window")
    _delete(conn, VIP, 4003, POSTED + timedelta(minutes=7))
    _edit(conn, RELAY, COPY, DIRECTION_EDIT, POSTED + timedelta(minutes=8))
    conn.commit()

    _withdrawn_everywhere(conn, executed=False, signal_id=first.signal_id)


@pytest.mark.parametrize("order", ORDERS)
def test_two_channels_echoing_each_other_through_a_call_posted_again_withdraw_it_last(
    conn, order  # noqa: F811
):
    """Order seed 5447: the relay forwards the VIP's call; the VIP deletes
    it (signal 1 VOIDED); the relay forwards the deleted post again and
    posts the call by hand (both held back); the VIP forwards the relay's
    hand post -- the VIP posting the call again (signal 2). Then every post
    is deleted. The VIP's forward goes while the relay's first forward
    still shows the call (an echo of the relay's removal); the relay's
    first forward, an echo of the VIP's withdrawal of signal 1, is by then
    the call's last post: its deletion withdraws signal 2."""
    _store_call(conn, order, forwards=True)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1), by="window")
    _ingest(conn, RELAY, 7002, posted=POSTED + timedelta(minutes=2), fwd=(V, CALL))
    _ingest(conn, RELAY, 7003, posted=POSTED + timedelta(minutes=3))
    again = _ingest(conn, VIP, 4002, posted=POSTED + timedelta(minutes=4), fwd=(R, 7003))
    assert again.outcome == "new"
    assert _live(conn) == [again.signal_id]

    for step, (channel, message_id) in enumerate(
        [(RELAY, 7003), (RELAY, 7002), (VIP, 4002), (RELAY, COPY)]
    ):
        _delete(conn, channel, message_id, POSTED + timedelta(minutes=5 + step))

    _withdrawn_everywhere(conn, executed=False, signal_id=again.signal_id)


@pytest.mark.parametrize("order", ORDERS)
def test_a_reply_given_before_its_post_was_re_aimed_follows_the_call_in_either_order(
    conn, order  # noqa: F811
):
    """Order seeds 5187 and 5469: "close half" replies to the relay's copy,
    the relay also forwards the VIP's post, and then re-aims its copy --
    VIP-first a repeat removal, relay-first the copy held the signal and
    the forward is promoted. The instruction was given while the post
    showed the call: it acts on the live call either way. A reply to the
    re-aimed post stored after the re-aim is about no trade, either way."""
    _store_call(conn, order)
    _ingest(conn, RELAY, 7002, CLOSE_HALF, posted=POSTED + timedelta(minutes=1), reply_to=COPY)
    _ingest(conn, RELAY, 7003, posted=POSTED + timedelta(minutes=2), fwd=(V, CALL))
    _edit(conn, RELAY, COPY, DIRECTION_EDIT, POSTED + timedelta(minutes=3))
    conn.commit()
    _ingest(conn, RELAY, 7004, CLOSE_HALF, posted=POSTED + timedelta(minutes=4), reply_to=COPY)

    (live,) = _live(conn)
    ((before,),) = _q(conn, "SELECT followup_id FROM followups WHERE message_id = 7002")
    ((after,),) = _q(conn, "SELECT followup_id FROM followups WHERE message_id = 7004")
    assert relay_repo.signal_for_followup(conn, followup_id=before) == live
    assert relay_repo.signal_for_followup(conn, followup_id=after) is None


@pytest.mark.parametrize("order", ORDERS)
def test_a_post_re_aimed_away_is_no_standing_post_for_the_last_ones_removal(
    conn, order  # noqa: F811
):
    """Order seed 8592 (found at 5000 seeds): the relay forwards the VIP's
    call; the VIP deletes it (signal 1 VOIDED); the relay posts the call
    by hand (R/7002) and the VIP forwards that -- the VIP posting the call
    again (signal 2). The relay re-aims R/7002 into a SELL; the VIP deletes
    its forward of it (an echo: the relay's forward R/7001 still shows the
    call). Then the relay deletes R/7001, which forwarded the post that
    withdrew signal 1. R/7002 still reads as a signal, but no longer as
    this call: R/7001 was the call's last standing post, and its removal
    withdraws signal 2."""
    _store_call(conn, order, forwards=True)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1))
    _ingest(conn, RELAY, 7002, posted=POSTED + timedelta(minutes=2))
    again = _ingest(conn, VIP, 4002, posted=POSTED + timedelta(minutes=3), fwd=(R, 7002))
    assert again.outcome == "new" and _live(conn) == [again.signal_id]
    _edit(conn, RELAY, 7002, DIRECTION_EDIT, POSTED + timedelta(minutes=4))
    conn.commit()
    _delete(conn, VIP, 4002, POSTED + timedelta(minutes=5))

    _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=6))

    _withdrawn_everywhere(conn, executed=False, signal_id=again.signal_id)


@pytest.mark.parametrize("by", ["event", "window"])
@pytest.mark.parametrize("first", [COPY, 7002], ids=["lower-first", "higher-first"])
@pytest.mark.parametrize("order", ORDERS)
def test_posts_deleted_together_withdraw_a_call_through_the_same_post_in_either_order(
    conn, order, first, by  # noqa: F811
):
    """Order seeds 6674 and 8947: the relay double-posts the call (R/7001,
    R/7002) and deletes both in one report -- its last posts: the call is
    withdrawn (VOIDED). Then it forwards the VIP's post again, posting the
    call again (signal 2). Which of the two deleted posts withdrew the call
    must not depend on which post held the signal (relay-first: R/7001,
    whose job speaks for both) or on the order the report named them
    (VIP-first: whichever job ran first): it is the lower id, R/7001. So a
    reply to R/7001 given before the deletion stays with signal 1 (it is
    about the call that post withdrew, ruling R4-4), one stored after gets
    no target, and a reply to R/7002 -- a repeat -- follows the call."""
    _store_call(conn, order)
    _ingest(conn, RELAY, 7002, posted=POSTED + timedelta(minutes=1))
    _ingest(conn, RELAY, 7010, CLOSE_HALF, posted=POSTED + timedelta(minutes=2), reply_to=COPY)
    both = (COPY, 7002)
    for message_id in (first, 7002 if first == COPY else COPY):
        _delete(conn, RELAY, message_id, POSTED + timedelta(minutes=3), by=by, also=both)
    assert _live(conn) == []
    assert _q(
        conn, "SELECT closed_by_channel_id, closed_by_message_id FROM signals WHERE signal_id = 1"
    ) == [(R, COPY)]

    again = _ingest(conn, RELAY, 7003, posted=POSTED + timedelta(minutes=4), fwd=(V, CALL))
    assert (again.outcome, _live(conn)) == ("new", [again.signal_id])
    _ingest(conn, RELAY, 7011, CLOSE_HALF, posted=POSTED + timedelta(minutes=5), reply_to=COPY)
    _ingest(conn, RELAY, 7012, CLOSE_HALF, posted=POSTED + timedelta(minutes=6), reply_to=7002)

    def acts_on(message_id: int) -> int | None:
        ((followup_id,),) = _q(
            conn, "SELECT followup_id FROM followups WHERE message_id = ?", message_id
        )
        return relay_repo.signal_for_followup(conn, followup_id=followup_id)

    assert acts_on(7010) == 1  # given before: about the call R/7001 withdrew
    assert acts_on(7011) is None  # stored after: R/7001 shows no call
    assert acts_on(7012) == again.signal_id  # R/7002 was a repeat: the call


@pytest.mark.parametrize("order", ORDERS)
def test_a_post_that_left_the_call_before_the_report_withdrew_nothing_in_it(
    conn, order  # noqa: F811
):
    """The same rule, with the lower id out of the call already: the relay
    re-aims R/7001 into a SELL while R/7002 keeps the BUY (VIP-first a
    repeat removal; relay-first R/7001 held the signal and R/7002 is
    promoted), a "close half" replies to R/7002, and one report deletes
    both. R/7001 showed no call any more: R/7002 withdrew it, in either
    order, so the reply stays with the signal R/7002 withdrew when the
    relay posts the call again."""
    _store_call(conn, order)
    _ingest(conn, RELAY, 7002, posted=POSTED + timedelta(minutes=1))
    _edit(conn, RELAY, COPY, DIRECTION_EDIT, POSTED + timedelta(minutes=2))
    conn.commit()
    _ingest(conn, RELAY, 7010, CLOSE_HALF, posted=POSTED + timedelta(minutes=3), reply_to=7002)
    for message_id in (COPY, 7002):
        _delete(conn, RELAY, message_id, POSTED + timedelta(minutes=4), also=(COPY, 7002))
    assert _live(conn) == []
    ((withdrawn,),) = _q(
        conn, "SELECT signal_id FROM signals WHERE closed_by_message_id = 7002"
    )

    again = _ingest(conn, RELAY, 7003, posted=POSTED + timedelta(minutes=5))

    assert (again.outcome, _live(conn)) == ("new", [again.signal_id])
    ((followup_id,),) = _q(conn, "SELECT followup_id FROM followups WHERE message_id = 7010")
    assert relay_repo.signal_for_followup(conn, followup_id=followup_id) == withdrawn


def test_a_post_of_another_signal_deleted_in_the_same_report_withdrew_nothing_of_this_one(
    conn,  # noqa: F811
):
    """One channel: the VIP posts a call and, 20 minutes later -- outside
    the relay window -- the same call again: two signals. One report
    deletes both posts; each withdrew its own signal, not the other's."""
    _ingest(conn, VIP, CALL)
    later = _ingest(conn, VIP, 4020, posted=POSTED + timedelta(minutes=20))
    assert later.outcome == "new" and _live(conn) == [1, later.signal_id]

    for message_id in (CALL, 4020):
        _delete(conn, VIP, message_id, POSTED + timedelta(minutes=21), also=(CALL, 4020))

    assert _live(conn) == []
    assert _q(conn, "SELECT signal_id, closed_by_message_id FROM signals ORDER BY 1") == [
        (1, CALL), (later.signal_id, 4020)
    ]


@pytest.mark.parametrize("executed", [False, True], ids=["pending", "executed"])
def test_a_channels_bump_is_its_own_post_while_its_double_still_shows_the_call(
    conn, executed, caplog  # noqa: F811
):
    """R5-1 while the call still stands on the channel: the VIP bumps its
    call (V/4002, a forward of its own V/4001) and posts it again by hand
    (V/4003). It deletes the original, then the bump. The bump is the
    VIP's own post, never an echo: pending, deleting it after it was
    promoted promotes the double in turn (the bump held the call);
    executed, it is a repeat removal like the original's -- a WARNING
    each time, and the call stands."""
    _vip_bumps_its_call(conn, executed=executed)
    _ingest(conn, VIP, 4003, posted=POSTED + timedelta(minutes=2))
    first = _delete(conn, VIP, CALL, POSTED + timedelta(minutes=3))
    with caplog.at_level(logging.INFO, logger="xaucopier.store.edits_repo"):
        bump = _delete(conn, VIP, BUMP, POSTED + timedelta(minutes=4))

    assert not [r for r in caplog.records if "echo" in r.getMessage()]
    if executed:
        assert (first.outcome, bump.outcome) == ("deleted", "deleted")
        assert _codes(conn) == [("WARNING", edits_repo.SIGNAL_REPEAT_REMOVED)] * 2
        assert _live(conn) == [1]
    else:
        assert first.promoted_signal_id == 2
        assert (bump.outcome, bump.promoted_signal_id) == ("voided", 3)
        assert _q(conn, "SELECT message_id FROM signals WHERE signal_id = 3") == [(4003,)]
        assert _live(conn) == [3]
        assert _queued_entries(conn) == [(3, "acct-1"), (3, "acct-2")]


@pytest.mark.parametrize("first", [COPY, 7002], ids=["forward-first", "double-first"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_relay_deleting_every_post_at_once_withdraws_the_call_though_one_is_an_echo(
    conn, order, first  # noqa: F811
):
    """The relay forwards the VIP's call (R/7001) and posts it again by hand
    (R/7002); the VIP double-posts (V/4002) and deletes V/4001 -- a repeat
    removal, or (VIP-first) its holder with V/4002 promoted. Then one report
    deletes both relay posts: its last. R/7001's removal only echoes the
    VIP's deletion of the post it forwarded; R/7002's is the relay's own,
    and withdraws the call (ruling C2) -- in either order: relay-first
    R/7001 held the signal, so R/7002 must not leave the saying to it."""
    _store_call(conn, order, forwards=True)
    _ingest(conn, VIP, 4002, posted=POSTED + timedelta(minutes=1))
    _ingest(conn, RELAY, 7002, posted=POSTED + timedelta(minutes=1, seconds=30))
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=2))
    assert len(_live(conn)) == 1

    both = (COPY, 7002)
    for message_id in (first, 7002 if first == COPY else COPY):
        _delete(conn, RELAY, message_id, POSTED + timedelta(minutes=3), also=both)

    assert _live(conn) == []
    assert _entries_claimable(conn) == []


# == Round 5 (ruling R5-5): "trade it by hand" never while the call is queued ==

_DO_NOT = "do NOT trade it by hand as well"
#: A held-back copy re-aimed into a SELL says so, and which call is meant
#: (ruling R6-4).
_READS_SELL = "This copy itself reads as SELL 4480-4485, not signal 1's BUY 4480-4485."
_TRADE_THE_BUY = "If signal 1's BUY 4480-4485 is still meant, trade it by hand."


def test_a_held_back_forward_of_a_re_aimed_post_names_the_call_queued_in_its_place(
    conn,  # noqa: F811
):
    """n4 (c): the VIP re-aimed its first post into a SELL while its repost
    kept the BUY (promoted, signal 2, queued); a relay forwards the SELL
    post, held back against signal 1 with a CRITICAL. That alert must not
    tell the user to trade signal 1's call by hand: signal 2 carries it and
    is queued. It names signal 2, says not to trade it by hand as well, and
    says the copy itself shows the re-aimed SELL."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=1))
    assert _edit(conn, VIP, CALL, DIRECTION_EDIT, EDIT_1).promoted_signal_id == 2
    conn.commit()
    _ingest(conn, RELAY, COPY, DIRECTION_EDIT, posted=POSTED + timedelta(minutes=6), fwd=(V, CALL))

    detail = _detail(conn, relay_repo.COPY_OF_WITHDRAWN_SIGNAL)
    assert "signal 2" in detail and f"message {REPOST}" in detail, detail
    assert _DO_NOT in detail, detail
    assert "trade it by hand." not in detail.replace(_DO_NOT, ""), detail
    assert _READS_SELL in detail, detail
    assert "The same call, BUY 4480-4485, is signal 2" in detail, detail


def test_a_held_back_copy_of_a_call_nobody_posted_again_still_says_trade_it_by_hand(
    conn,  # noqa: F811
):
    """The control: the VIP withdrew its call and nothing carries it; a copy
    from another channel is held back and says to trade the call by hand if
    it is still meant."""
    _ingest(conn, VIP, CALL)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1))
    _ingest(conn, OTHER, 8001, posted=POSTED + timedelta(minutes=2))

    detail = _detail(conn, relay_repo.COPY_OF_WITHDRAWN_SIGNAL)
    assert detail.endswith("If the call is still meant, trade it by hand."), detail
    assert _DO_NOT not in detail


@pytest.mark.parametrize("live", [True, False], ids=["call-live", "call-withdrawn"])
def test_an_edit_that_makes_a_post_the_live_call_says_not_to_trade_it_by_hand(
    conn, live  # noqa: F811
):
    """One channel: the VIP's call is queued (signal 1), and the VIP edits
    an old chatter post into the same call. Edits never open trades; the
    alert used to say "if it is a real call, trade it by hand" -- which
    would open the queued call twice. With the call live it names signal 1
    and says not to trade it by hand as well; with the call withdrawn it
    still says to trade it by hand if it is real."""
    _ingest(conn, VIP, 4000, CHATTER_TEXT, posted=POSTED - timedelta(minutes=1))
    _ingest(conn, VIP, CALL)
    if not live:
        _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1))
    _edit(conn, VIP, 4000, SIGNAL_TEXT, POSTED + timedelta(minutes=2))
    conn.commit()

    detail = _detail(conn, edits_repo.EDIT_CREATED_SIGNAL)
    if live:
        assert "signal 1" in detail and f"message {CALL}" in detail, detail
        assert _DO_NOT in detail, detail
        assert "If it is a real call, trade it by hand." not in detail
    else:
        assert detail.split(" [found by")[0].endswith("If it is a real call, trade it by hand.")
    assert _queued_entries(conn) == ([(1, "acct-1"), (1, "acct-2")] if live else [])


def test_a_held_back_copy_names_the_same_call_queued_as_another_signal(conn):  # noqa: F811
    """The VIP withdrew its call (signal 1, VOIDED) and posted the same call
    again two minutes later with a moved stop -- another trade: a new
    signal 2, queued, not linked to signal 1. A relay's forward of the
    withdrawn post, read late (after a reconnect), is held back against
    signal 1 with a CRITICAL. It must not say to trade the call by hand:
    signal 2 shows the same call, posted within the relay window, and is
    queued (rulings R5-5, R6-1)."""
    _ingest(conn, VIP, CALL)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1))
    again = _ingest(conn, VIP, 4020, OTHER_CALL, posted=POSTED + timedelta(minutes=2))
    assert (again.outcome, _live(conn)) == ("new", [2])
    assert _promoted_from(conn, 2) is None

    held = _ingest(conn, RELAY, COPY, posted=POSTED + timedelta(seconds=30), fwd=(V, CALL))

    assert (held.outcome, held.relay_of_signal_id) == ("relay_duplicate", 1)
    detail = _detail(conn, relay_repo.COPY_OF_WITHDRAWN_SIGNAL)
    assert "signal 2" in detail and "message 4020" in detail, detail
    assert _DO_NOT in detail, detail
    assert "trade it by hand." not in detail.replace(_DO_NOT, ""), detail
    assert "reads as" not in detail, detail  # the copy shows that very call


@pytest.mark.parametrize(
    ("minutes", "named"),
    [(10, True), (16, True), (-14, True), (20, False)],
    ids=["near-both", "near-the-signal", "near-the-removed-post", "outside-the-window"],
)
@pytest.mark.parametrize("order", ORDERS)
def test_an_older_posts_doubt_names_the_same_call_queued_as_another_signal(
    conn, order, minutes, named  # noqa: F811
):
    """R4-1 (c) with the same call queued elsewhere: the VIP deleted its
    call and posted it again (signal 2, V/4012 at +2 min), and also posted
    it with a moved stop -- another trade, a new signal 3, at `minutes`.
    The relay's old hand copy (R/7001 at +30 s) is removed: an older post,
    which voids signal 2 with the CRITICAL doubt. When signal 3 was posted
    within the relay window of the removed post or of signal 2's own post,
    the doubt must not end "trade it by hand": signal 3 shows the call and
    is queued (rulings R5-5, R6-1). Posted further off, it is another call,
    and the doubt says to trade the call by hand."""
    _store_call(conn, order)
    live = _vip_deletes_and_posts_again(conn)
    later = _ingest(conn, VIP, 4020, OTHER_CALL, posted=POSTED + timedelta(minutes=minutes))
    assert later.outcome == "new" and _live(conn) == [live, later.signal_id]

    _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=21))

    assert _live(conn) == [later.signal_id]
    detail = _detail(conn, edits_repo.OLD_POST_WITHDREW_CALL)
    assert "old copy" in detail, detail
    if named:
        assert f"signal {later.signal_id}" in detail and "message 4020" in detail, detail
        assert _DO_NOT in detail, detail
        assert "trade it by hand." not in detail.replace(_DO_NOT, ""), detail
    else:
        assert detail.split(" [found by")[0].endswith("-- trade it by hand."), detail
        assert _DO_NOT not in detail and "message 4020" not in detail, detail


def test_a_held_back_copy_of_a_call_withdrawn_twice_still_says_trade_it_by_hand(
    conn,  # noqa: F811
):
    """The VIP deleted its call, posted it again (signal 2) and deleted that
    too: nothing carries the call. A relay's forward of the first post,
    read late, is held back with a CRITICAL that says to trade the call by
    hand if it is still meant -- never "do not trade it by hand" in the
    name of the withdrawn signal 2."""
    _ingest(conn, VIP, CALL)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1))
    again = _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=2))
    assert _promoted_from(conn, again.signal_id) == 1
    _delete(conn, VIP, REPOST, POSTED + timedelta(minutes=3))
    assert _live(conn) == []

    held = _ingest(conn, RELAY, COPY, posted=POSTED + timedelta(seconds=30), fwd=(V, CALL))

    assert held.outcome == "relay_duplicate"
    detail = _detail(conn, relay_repo.COPY_OF_WITHDRAWN_SIGNAL)
    assert detail.endswith("If the call is still meant, trade it by hand."), detail
    assert _DO_NOT not in detail


def test_a_held_back_forward_of_a_re_aimed_post_names_no_withdrawn_signal(
    conn,  # noqa: F811
):
    """n4 (c) once the promoted repost is withdrawn too: the VIP re-aimed
    V/4001 (signal 1 INVALID, V/4012 promoted as signal 2) and then deleted
    V/4012 (signal 2 VOIDED). A relay's forward of the SELL post is held
    back against signal 1. Nothing carries the call now: the alert says to
    trade it by hand if it is still meant, and names no withdrawn signal
    as carrying it."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=1))
    assert _edit(conn, VIP, CALL, DIRECTION_EDIT, EDIT_1).promoted_signal_id == 2
    conn.commit()
    _delete(conn, VIP, REPOST, POSTED + timedelta(minutes=5))
    assert _live(conn) == []

    _ingest(conn, RELAY, COPY, DIRECTION_EDIT, posted=POSTED + timedelta(minutes=6), fwd=(V, CALL))

    assert _link(conn, RELAY, COPY) == 1
    detail = _detail(conn, relay_repo.COPY_OF_WITHDRAWN_SIGNAL)
    assert _READS_SELL in detail, detail
    assert detail.endswith(_TRADE_THE_BUY), detail
    assert _DO_NOT not in detail and "signal 2" not in detail, detail


# == Round 6 (ruling R6-2): R5-2's clauses with three channels =================
#
# With two channels, the last-standing-post rule alone withdraws a call whose
# every post is gone. With a third channel forwarding the relay's posts, the
# VIP's call posted again stands on the VIP's channel while the forward is
# removed: then only R5-2's clauses in relay_repo._gone_post tell an echo
# (nothing to do) from a removal that must withdraw the call posted again.


def _removed_forward_withdraws_the_call_posted_again(  # noqa: F811
    conn, *, forward: int, again: int
) -> None:
    """The third channel removes its forward O/`forward` of a post whose
    channel no longer shows the call: no echo. The removal of an older post
    of the call reaches signal `again` -- VOIDED with the CRITICAL doubt --
    and nothing of the call is claimable."""
    removed = _delete(conn, OTHER, forward, POSTED + timedelta(minutes=10))

    assert (removed.outcome, removed.signal_id) == ("voided", again)
    assert _live(conn) == []
    assert ("CRITICAL", edits_repo.OLD_POST_WITHDREW_CALL) in _codes(conn)
    assert _entries_claimable(conn) == []


def test_a_forward_of_a_repeat_removed_is_no_echo_once_its_channel_withdrew_the_call(
    conn,  # noqa: F811
):
    """r5_three_repeat: a third channel forwards the relay's R/7001. The
    relay deletes R/7001 while R/7002 still shows the call (a repeat
    removal), then R/7002, its last post: the relay withdrew the call. The
    VIP deletes its post and posts the call again (signal 2). The forward's
    removal cannot echo R/7001's repeat removal: the relay shows the call
    nowhere any more, so it withdraws signal 2 like any older post."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, RELAY, COPY, posted=POSTED + timedelta(seconds=30))
    _ingest(conn, RELAY, 7002, posted=POSTED + timedelta(seconds=60))
    forward = _ingest(conn, OTHER, 9001, posted=POSTED + timedelta(seconds=90), fwd=(R, COPY))
    assert (forward.outcome, forward.relay_of_signal_id) == ("relay_duplicate", 1)
    assert _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=2)).outcome == "deleted"
    assert _delete(conn, RELAY, 7002, POSTED + timedelta(minutes=3)).outcome == "voided"
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=4))
    again = _ingest(conn, VIP, 4002, posted=POSTED + timedelta(minutes=5))
    assert (again.outcome, _live(conn)) == ("new", [again.signal_id])

    _removed_forward_withdraws_the_call_posted_again(conn, forward=9001, again=again.signal_id)


def test_a_forward_of_a_post_a_promotion_replaced_is_no_echo_once_its_channel_withdrew_the_call(
    conn,  # noqa: F811
):
    """r5_three_promo: a third channel forwards the VIP's V/4001. The VIP
    deletes V/4001 while its double V/4002 stands (promoted: signal 2 --
    the call never stopped, so V/4001 withdrew nothing). The relay deletes
    its only copy (the call is withdrawn), the VIP deletes V/4002, and the
    relay posts the call again (signal 3). The forward's removal cannot
    echo V/4001's: the VIP shows the call nowhere, so it withdraws
    signal 3."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, VIP, 4002, posted=POSTED + timedelta(seconds=20))
    _ingest(conn, RELAY, COPY, posted=POSTED + timedelta(seconds=30))
    forward = _ingest(conn, OTHER, 9001, posted=POSTED + timedelta(seconds=40), fwd=(V, CALL))
    assert (forward.outcome, forward.relay_of_signal_id) == ("relay_duplicate", 1)
    assert _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1)).promoted_signal_id == 2
    assert _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=2)).outcome == "voided"
    _delete(conn, VIP, 4002, POSTED + timedelta(minutes=3))
    again = _ingest(conn, RELAY, 7002, posted=POSTED + timedelta(minutes=4))
    assert (again.outcome, _live(conn)) == ("new", [again.signal_id])
    assert _promoted_from(conn, again.signal_id) == 2

    _removed_forward_withdraws_the_call_posted_again(conn, forward=9001, again=again.signal_id)


def test_a_forward_of_a_repeat_removed_is_no_echo_once_its_channel_re_aimed_its_last_post(
    conn,  # noqa: F811
):
    """r5_three_reread: a third channel forwards the relay's R/7001. The
    relay deletes R/7001 (a repeat: R/7002 and R/7003 stand), re-aims
    R/7002 into a SELL (a repeat: R/7003 stands) and deletes R/7003, its
    last post of the call. The VIP deletes its post and posts the call again
    (signal 2). R/7002 is still stored as a signal, but re-read it shows
    another call: the relay shows this one nowhere, so the forward's removal
    withdraws signal 2."""
    _ingest(conn, VIP, CALL)
    for index, message_id in enumerate((COPY, 7002, 7003)):
        _ingest(conn, RELAY, message_id, posted=POSTED + timedelta(seconds=30 + 10 * index))
    forward = _ingest(conn, OTHER, 9001, posted=POSTED + timedelta(seconds=70), fwd=(R, COPY))
    assert (forward.outcome, forward.relay_of_signal_id) == ("relay_duplicate", 1)
    assert _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=2)).outcome == "deleted"
    assert _edit(conn, RELAY, 7002, DIRECTION_EDIT, EDIT_1).outcome == "recorded"
    conn.commit()
    assert _delete(conn, RELAY, 7003, POSTED + timedelta(minutes=6)).outcome == "voided"
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=7))
    again = _ingest(conn, VIP, 4002, posted=POSTED + timedelta(minutes=8))
    assert (again.outcome, _live(conn)) == ("new", [again.signal_id])

    _removed_forward_withdraws_the_call_posted_again(conn, forward=9001, again=again.signal_id)


# == Round 6 (ruling R6-1): "the same call" only within the relay window =======
#
# Signals stay NEW for ever, so a call with the same side and entry zone
# traded days ago is not "the same call" as a new one: an alert names another
# signal as carrying the call (and says NOT to trade it by hand) only when
# that signal came through the call's own chain, or was posted within the
# relay window -- the horizon that decides duplicates -- of the post the alert
# is about.

LATER = POSTED + timedelta(days=2)
#: The fixture's BUY with another entry zone: another call.
OTHER_ZONE = SIGNAL_TEXT.replace("4480-4485", "4476-4479")
assert OTHER_ZONE != SIGNAL_TEXT


@pytest.mark.parametrize("executed", [False, True], ids=["pending", "executed"])
def test_an_edit_into_a_call_posted_days_ago_still_says_trade_it_by_hand(
    conn, executed  # noqa: F811
):
    """r5_stale_carrier (a), one channel: the VIP's BUY 4480-4485 two days
    ago is signal 1 (taken by acct-1, or not). Today the VIP edits a
    chatter post into the same BUY 4480-4485. Edits never open trades; the
    alert must not call two-day-old signal 1 "the same call" and forbid
    trading today's call by hand."""
    _ingest(conn, VIP, CALL)
    if executed:
        _take_entry(conn, 1)
    _ingest(conn, VIP, 5001, CHATTER_TEXT, posted=LATER)
    _edit(conn, VIP, 5001, SIGNAL_TEXT, LATER + timedelta(minutes=1))
    conn.commit()

    detail = _detail(conn, edits_repo.EDIT_CREATED_SIGNAL)
    assert detail.split(" [found by")[0].endswith("If it is a real call, trade it by hand."), detail
    assert _DO_NOT not in detail and "signal 1" not in detail, detail


@pytest.mark.parametrize("executed", [False, True], ids=["pending", "executed"])
def test_a_held_back_copy_of_a_call_posted_again_days_later_still_says_trade_it_by_hand(
    conn, executed  # noqa: F811
):
    """r5_stale_carrier (c), two channels: the VIP's BUY 4480-4485 two days
    ago is signal 1. Today the VIP posts the same call (signal 2) and
    deletes it; a relay's copy two minutes later is held back against
    signal 2. Nothing posted near it carries the call: the alert says to
    trade it by hand if it is still meant, never naming signal 1."""
    _ingest(conn, VIP, CALL)
    if executed:
        _take_entry(conn, 1)
    today = _ingest(conn, VIP, 5001, posted=LATER)
    assert today.outcome == "new" and _promoted_from(conn, today.signal_id) is None
    _delete(conn, VIP, 5001, LATER + timedelta(minutes=1))
    held = _ingest(conn, RELAY, COPY, posted=LATER + timedelta(minutes=2))

    assert (held.outcome, held.relay_of_signal_id) == ("relay_duplicate", today.signal_id)
    detail = _detail(conn, relay_repo.COPY_OF_WITHDRAWN_SIGNAL)
    assert detail.endswith("If the call is still meant, trade it by hand."), detail
    assert _DO_NOT not in detail and "signal 1 " not in detail, detail


@pytest.mark.parametrize(
    ("window", "gap", "named"),
    [(900, 900, True), (900, 901, False), (300, 300, True), (300, 301, False)],
)
def test_the_same_call_is_named_only_within_the_relay_window(
    conn, window, gap, named  # noqa: F811
):
    """The VIP withdrew its call (signal 1) and posts it again with a moved
    stop -- another trade, a signal of its own (signal 2, queued) -- `gap`
    seconds after a relay forwarded the first post (read late: held back
    against signal 1). Signal 2 shows the same call: within the relay
    window of the forward (`window`, both ends included) the alert names it
    and says not to trade the call by hand as well; one second later it is
    another call, and the alert says to trade it by hand if it is still
    meant."""
    forwarded = POSTED + timedelta(seconds=30)
    _ingest(conn, VIP, CALL, window=window)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1))
    moved = _ingest(
        conn, VIP, 4020, OTHER_CALL, posted=forwarded + timedelta(seconds=gap), window=window
    )
    assert moved.outcome == "new" and _promoted_from(conn, moved.signal_id) is None

    held = _ingest(conn, RELAY, COPY, posted=forwarded, fwd=(V, CALL), window=window)

    assert (held.outcome, held.relay_of_signal_id) == ("relay_duplicate", 1)
    detail = _detail(conn, relay_repo.COPY_OF_WITHDRAWN_SIGNAL)
    if named:
        assert f"The same call is signal {moved.signal_id}" in detail, detail
        assert _DO_NOT in detail, detail
    else:
        assert detail.endswith("If the call is still meant, trade it by hand."), detail
        assert _DO_NOT not in detail and "message 4020" not in detail, detail


def test_a_late_forward_names_the_same_call_posted_near_the_post_it_repeats(
    conn,  # noqa: F811
):
    """The VIP's call V/4001 (signal 1) and, two minutes later, the same
    call with a moved stop (another trade: signal 2, queued). A relay
    forwards V/4001 twenty minutes after it was posted, and the VIP deletes
    V/4001 (signal 1 VOIDED) before that forward is read: it is held back
    against signal 1. The forward was posted long after signal 2, but the
    post it repeats was posted two minutes before it: signal 2 shows the
    same call, so the alert names it and says not to trade it by hand as
    well."""
    _ingest(conn, VIP, CALL)
    moved = _ingest(conn, VIP, 4020, OTHER_CALL, posted=POSTED + timedelta(minutes=2))
    assert moved.outcome == "new" and _promoted_from(conn, moved.signal_id) is None
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=25))
    assert _live(conn) == [moved.signal_id]

    held = _ingest(conn, RELAY, COPY, posted=POSTED + timedelta(minutes=20), fwd=(V, CALL))

    assert (held.outcome, held.relay_of_signal_id) == ("relay_duplicate", 1)
    detail = _detail(conn, relay_repo.COPY_OF_WITHDRAWN_SIGNAL)
    assert f"The same call is signal {moved.signal_id}" in detail and _DO_NOT in detail, detail


def test_an_old_post_edited_into_the_live_call_names_it(conn):  # noqa: F811
    """One channel: a chatter post half an hour old, then the VIP's call
    (signal 1, queued), and a minute later the VIP edits the old chatter
    post into the same call. The post was posted long before the call, but
    it became this call a minute after it: the alert names signal 1 and
    says not to trade the call by hand as well."""
    _ingest(conn, VIP, 4000, CHATTER_TEXT, posted=POSTED - timedelta(minutes=30))
    _ingest(conn, VIP, CALL)
    _edit(conn, VIP, 4000, SIGNAL_TEXT, POSTED + timedelta(minutes=1))
    conn.commit()

    detail = _detail(conn, edits_repo.EDIT_CREATED_SIGNAL)
    assert "The same call is signal 1" in detail and _DO_NOT in detail, detail
    assert _queued_entries(conn) == [(1, "acct-1"), (1, "acct-2")]


@pytest.mark.parametrize(
    ("text", "shown"),
    [(OTHER_ZONE, "BUY 4476-4479"), (DIRECTION_EDIT, "SELL 4480-4485")],
    ids=["another-zone", "another-side"],
)
def test_a_live_call_in_another_entry_zone_is_not_the_same_call(conn, text, shown):  # noqa: F811
    """One channel: the VIP's BUY 4480-4485 is queued (signal 1); a minute
    later the VIP edits a chatter post into a BUY 4476-4479 (another entry
    zone) or a SELL 4480-4485 (another side) -- another call. Edits never
    open trades: the alert says to trade it by hand if it is real, and
    names no signal."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, VIP, 4002, CHATTER_TEXT, posted=POSTED + timedelta(seconds=30))
    _edit(conn, VIP, 4002, text, POSTED + timedelta(minutes=1))
    conn.commit()

    detail = _detail(conn, edits_repo.EDIT_CREATED_SIGNAL)
    assert shown in detail, detail
    assert detail.split(" [found by")[0].endswith("If it is a real call, trade it by hand."), detail
    assert _DO_NOT not in detail and "signal 1" not in detail, detail


def test_a_held_back_forward_of_a_re_aimed_post_names_the_call_traded_in_its_place(
    conn,  # noqa: F811
):
    """r5_carrier_exec: n4 (c) with the promoted repost traded. The VIP
    re-aims V/4001 into a SELL while its repost V/4012 keeps the BUY
    (promoted: signal 2); acct-1 takes signal 2's entry and the VIP then
    deletes V/4012 (signal 2 flagged: withdrawn, but traded). A relay's
    forward of the SELL post is held back against signal 1. Signal 2 is no
    longer live, but the call was traded as signal 2: trading it by hand
    as well would open it twice, so the alert names signal 2 and says so."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=1))
    assert _edit(conn, VIP, CALL, DIRECTION_EDIT, EDIT_1).promoted_signal_id == 2
    conn.commit()
    _take_entry(conn, 2)
    assert _delete(conn, VIP, REPOST, POSTED + timedelta(minutes=6)).outcome == "flagged"
    assert _live(conn) == []

    _ingest(conn, RELAY, COPY, DIRECTION_EDIT, posted=POSTED + timedelta(minutes=7), fwd=(V, CALL))

    assert _link(conn, RELAY, COPY) == 1
    detail = _detail(conn, relay_repo.COPY_OF_WITHDRAWN_SIGNAL)
    assert "The same call" in detail and "signal 2 (message 4012" in detail, detail
    assert "trade status executed" in detail and _DO_NOT in detail, detail
    assert "trade it by hand." not in detail.replace(_DO_NOT, ""), detail


# == Round 6 (ruling R6-4): a held-back copy that shows another call ===========


@pytest.mark.parametrize("executed", [False, True], ids=["pending", "executed"])
def test_a_channels_bump_of_its_re_aimed_post_says_which_call_it_withdrew(
    conn, executed  # noqa: F811
):
    """r5_bump_reaimed, one channel: the VIP re-aims its BUY 4480-4485 into
    a SELL (signal 1 INVALID, or flagged when it was traded), then forwards
    its own post -- now a SELL -- as V/4002. The bump repeats signal 1 and
    is held back with a CRITICAL. Its text reads as SELL: the alert must
    say so and say that the call it means is signal 1's BUY, so the user
    does not trade the retracted BUY by mistake for what the bump shows."""
    _ingest(conn, VIP, CALL)
    if executed:
        _take_entry(conn, 1)
    _edit(conn, VIP, CALL, DIRECTION_EDIT, EDIT_1)
    conn.commit()

    bump = _ingest(
        conn, VIP, BUMP, DIRECTION_EDIT, posted=POSTED + timedelta(minutes=6), fwd=(V, CALL)
    )

    assert (bump.outcome, bump.relay_of_signal_id) == ("relay_duplicate", 1)
    assert _entries_claimable(conn) == []
    if not executed:
        detail = _detail(conn, relay_repo.COPY_OF_WITHDRAWN_SIGNAL)
        assert _READS_SELL in detail, detail
        assert detail.endswith(_TRADE_THE_BUY), detail
    else:  # a traded call's copy is no copy of a withdrawn call: no alert
        assert relay_repo.COPY_OF_WITHDRAWN_SIGNAL not in [code for _, code in _codes(conn)]


def test_a_held_back_copy_showing_the_call_names_no_other_call(conn):  # noqa: F811
    """The control: a held-back copy that shows the withdrawn call itself
    says nothing about what it reads as, and the advice is unchanged."""
    _ingest(conn, VIP, CALL)
    _delete(conn, VIP, CALL, POSTED + timedelta(minutes=1))
    _ingest(conn, OTHER, 8001, posted=POSTED + timedelta(minutes=2))

    detail = _detail(conn, relay_repo.COPY_OF_WITHDRAWN_SIGNAL)
    assert "reads as" not in detail, detail
    assert detail.endswith("one source withdrew the call. If the call is still meant, trade it by hand.")


def test_a_copy_on_another_symbol_names_both_symbols(conn):  # noqa: F811
    """Side by side, a copy's call and the call meant carry their symbols
    when these differ (one parser reads XAUUSD only today; another may
    not): never "BUY 4480-4485, not signal 1's BUY 4480-4485"."""
    _ingest(conn, VIP, CALL)
    (row,) = conn.execute("SELECT * FROM signals WHERE signal_id = 1").fetchall()
    signal = route(SIGNAL_TEXT, channel_cfg=VIP, message_id=CALL, posted_at_utc=POSTED).signal

    assert relay_repo._describe_both(signal, row) == ("BUY 4480-4485", "BUY 4480-4485")
    silver = dataclasses.replace(signal, symbol_alias="XAGUSD")
    assert relay_repo._describe_both(silver, row) == (
        "XAGUSD BUY 4480-4485", "XAUUSD BUY 4480-4485"
    )
