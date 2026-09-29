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

from test_edits import DIRECTION_EDIT, EDIT_1, EDIT_2
from test_relay import (
    CALL,
    CLOSE_HALF,
    COPY,
    OTHER,
    POSTED,
    RELAY,
    REPOST,
    SIGNAL_TEXT,
    TP1_EDIT,
    VIP,
    R,
    V,
    _alerts,
    _edit,
    _ingest,
    _link,
    _q,
    _queued_entries,
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


@pytest.mark.parametrize("executed", [False, True], ids=["pending", "executed"])
@pytest.mark.parametrize("order", ORDERS)
def test_a_forward_of_a_post_its_channel_replaced_is_an_echo_when_removed(
    conn, order, executed  # noqa: F811
):
    """The VIP double-posted its call and deleted the first post; the
    relay's forward of that post is removed after it. The VIP's channel
    still shows the call, so the relay's removal only echoes that deletion
    -- whichever post holds the signal (VIP-first the repost was promoted,
    or kept the trade; relay-first the relay's forward holds the call and
    the VIP's deletion was a repeat removal). The VIP deleting its repost,
    its channel's last post, then withdraws the call."""
    _store_call(conn, order, forwards=True)
    _ingest(conn, VIP, REPOST, posted=POSTED + timedelta(minutes=1))
    if executed:
        _take_entry(conn, 1)
    assert _delete(conn, VIP, CALL, POSTED + timedelta(minutes=2)).outcome in ("voided", "deleted")
    echo = _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=3))

    assert echo.outcome == "deleted"
    (live,) = _live(conn)
    if executed:
        assert (live, _queued_entries(conn)) == (1, [(1, "acct-2")])
    else:
        assert _queued_entries(conn) == [(live, "acct-1"), (live, "acct-2")]
    assert "CRITICAL" not in [level for level, _ in _codes(conn)]

    last = _delete(conn, VIP, REPOST, POSTED + timedelta(minutes=4))
    assert (last.outcome, last.signal_id) == ("flagged" if executed else "voided", live)


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


def test_a_hand_copy_removed_after_the_repost_was_taken_flags_it_loudly(conn):  # noqa: F811
    """The same, after the repost's entry was taken: flagged, not closed,
    and the alert says the removal may only have been an old copy."""
    _store_call(conn, "vip-first")
    live = _vip_deletes_and_posts_again(conn)
    _take_entry(conn, live)
    gone = _delete(conn, RELAY, COPY, POSTED + timedelta(minutes=3))

    assert (gone.outcome, gone.signal_id) == ("flagged", live)
    assert ("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED, R, COPY, live) in _alerts(conn)
    detail = _detail(conn, edits_repo.LIVE_SIGNAL_DELETED)
    assert f"message {CALL} on channel {V}" in detail and f"message {REPOST}" in detail
    assert "old copy" in detail


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
