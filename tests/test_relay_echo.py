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
    _db_rows,
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
    speaks for the signal it withdrew: when the relay really deletes it,
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

    gone = _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=5))
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
