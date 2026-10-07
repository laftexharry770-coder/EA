"""A promotion never makes a second live signal for a call another live
signal already carries (final fix FF12, ruling P2 "one live signal per
call").

The scenario, on one channel, every update delivered in order:

- t=0: the call is posted (V/4001) and becomes signal 1. Nobody executes
  it -- it arrived stale after downtime, or trading was paused.
- t=+890 s: the same call is posted again (V/4012, by hand or as a
  forward of 4001). It is a copy of signal 1.
- t=+910 s: the same call a third time (V/4013), or a relay copies it
  (R/7001). It is NEW signal 2: the 900 s window is measured from each
  signal's own message, and 4001 is 910 s older.
- t=+915 s: 4001 is deleted or edited away. Signal 1 is VOIDED.

Before the fix the standing repost 4012 was promoted to signal 3 with
fresh entries -- the same trade as signal 2, posted 20 s apart, both
inside the age gate: the trade opened twice per account, even when
signal 2 had already been executed. Now the repost repeats signal 2,
which already carries the call, and a WARNING names it.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from telethon.tl import types

from fakes.telegram import FakeMessage
from test_edits import SL_EDIT
from test_relay import (
    ACCOUNTS,
    CALL,
    CHATTER_TEXT,
    CLOSE_HALF,
    OTHER,
    COPY,
    POSTED,
    RELAY,
    REPOST,
    REPOST_2,
    SIGNAL_TEXT,
    VIP,
    R,
    V,
    _db_rows,
    _ingest,
    _link,
    _post,
    _q,
    _round,
    _run,
    _state,
    _vip_call_stored,
    conn,  # noqa: F401 -- the fixture
)
from xaucopier.domain import gates
from xaucopier.domain.market import MarketSnapshot, Policy
from xaucopier.parsing.router import route
from xaucopier.store import db, edits_repo, outbox, relay_repo

FIRST, SECOND, THIRD, WITHDRAWN = 0, 890, 910, 915
HOWS = ("event", "window", "edit")
THIRDS = ("vip", "relay")


def _at(seconds: float) -> datetime:
    return POSTED + timedelta(seconds=seconds)


def _third(third_on: str):
    return (VIP, REPOST_2) if third_on == "vip" else (RELAY, COPY)


def _withdraw(conn, how: str, seconds: float = WITHDRAWN):  # noqa: F811
    now = _at(seconds)
    if how == "edit":
        result = edits_repo.apply_edit(
            conn, channel_cfg=VIP, message_id=CALL, text=CHATTER_TEXT, posted_at_utc=POSTED,
            edit_date_utc=now, reply_to_message_id=None, now_utc=now, accounts=ACCOUNTS,
            detected_by="event",
        )
    else:
        result = edits_repo.apply_deletion(
            conn, channel_cfg=VIP, message_id=CALL, now_utc=now, accounts=ACCOUNTS,
            detected_by=how,
        )
    conn.commit()
    return result


def _four_posts(conn, *, third_on: str, order: str, bump: bool, third_at=THIRD):  # noqa: F811
    """Signal 1 (V/4001), its repost V/4012 at +890 and the third post at
    `third_at`, the last two stored in `order`."""
    first = _ingest(conn, VIP, CALL)
    assert first.signal_id == 1
    channel, message_id = _third(third_on)

    def second():
        _ingest(conn, VIP, REPOST, posted=_at(SECOND), fwd=(V, CALL) if bump else None)

    def third():
        _ingest(conn, channel, message_id, posted=_at(third_at))

    for step in (second, third) if order == "second-first" else (third, second):
        step()
    assert _link(conn, VIP, REPOST) == 1


def _signals(conn) -> list[tuple]:  # noqa: F811
    return _q(
        conn,
        "SELECT signal_id, channel_id, message_id, state, withdrawn_at_utc IS NOT NULL "
        "FROM signals ORDER BY signal_id",
    )


def _claim_all(conn, seconds: float) -> list[tuple]:  # noqa: F811
    got = []
    for account in ACCOUNTS:
        while (row := outbox.claim(
            conn, account_id=account.account_id, pid=7, now_utc=_at(seconds)
        )) is not None:
            got.append((account.account_id, row.kind, row.signal_id))
    conn.commit()
    return got


def _codes(conn) -> list[tuple]:  # noqa: F811
    return _q(conn, "SELECT level, code, signal_id FROM alerts ORDER BY alert_id")


def _detail(conn, code: str) -> str:  # noqa: F811
    (row,) = _q(conn, "SELECT detail FROM alerts WHERE code = ?", code)
    return row[0]


WITHDRAWAL_CODE = {
    "event": edits_repo.SIGNAL_VOIDED_BY_DELETION,
    "window": edits_repo.SIGNAL_VOIDED_BY_DELETION,
    "edit": edits_repo.SIGNAL_RETRACTED_BY_EDIT,
}


@pytest.mark.parametrize("bump", [False, True], ids=["by-hand", "forward"])
@pytest.mark.parametrize("order", ["second-first", "third-first"])
@pytest.mark.parametrize("third_on", THIRDS)
@pytest.mark.parametrize("how", HOWS)
def test_a_repost_whose_call_is_already_live_as_another_signal_is_not_promoted(
    conn, how, third_on, order, bump  # noqa: F811
):
    _four_posts(conn, third_on=third_on, order=order, bump=bump)
    channel, message_id = _third(third_on)

    result = _withdraw(conn, how)

    assert result.outcome == "voided"
    assert result.promoted_signal_id is None
    assert result.carried_by_signal_id == 2
    assert _signals(conn) == [(1, V, CALL, "VOIDED", 1), (2, channel.channel_id, message_id, "NEW", 0)]
    assert _link(conn, VIP, REPOST) == 2
    assert _claim_all(conn, WITHDRAWN + 10) == [
        ("acct-1", "ENTRY", 2),
        ("acct-2", "ENTRY", 2),
    ]
    assert _codes(conn) == [
        ("WARNING", WITHDRAWAL_CODE[how], 1),
        ("WARNING", edits_repo.SIGNAL_REPOST_ALREADY_LIVE, 2),
    ]
    detail = _detail(conn, edits_repo.SIGNAL_REPOST_ALREADY_LIVE)
    assert "already queued as signal 2" in detail
    assert f"message {message_id} on channel {channel.channel_id}" in detail
    assert f"message {REPOST}" in detail and f"message {CALL}" in detail
    assert "Do NOT trade it by hand" in detail


@pytest.mark.parametrize("third_on", THIRDS)
def test_a_repost_inside_the_first_posts_window_is_still_promoted(conn, third_on):  # noqa: F811
    """Control: the third post at +899 is inside 4001's window, so it is a
    copy of signal 1 too; deleting 4001 promotes the earliest standing
    repost and the third post follows it -- one trade."""
    _four_posts(conn, third_on=third_on, order="second-first", bump=False, third_at=899)

    result = _withdraw(conn, "event")

    assert (result.promoted_signal_id, result.carried_by_signal_id) == (2, None)
    assert _signals(conn) == [(1, V, CALL, "VOIDED", 1), (2, V, REPOST, "NEW", 0)]
    assert _claim_all(conn, WITHDRAWN + 10) == [("acct-1", "ENTRY", 2), ("acct-2", "ENTRY", 2)]


def test_every_copy_and_follow_up_of_the_withdrawn_call_moves_to_the_signal_carrying_it(
    conn,  # noqa: F811
):
    """The relay's copy of signal 1, and the replies to its posts, follow
    the call to signal 2 -- as they would follow a promoted repost."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, RELAY, 7005, posted=_at(30))  # the relay copies signal 1
    reply_to_call = _ingest(conn, VIP, 4005, CLOSE_HALF, posted=_at(60), reply_to=CALL)
    _ingest(conn, VIP, REPOST, posted=_at(SECOND))
    reply_to_repost = _ingest(conn, VIP, 4020, CLOSE_HALF, posted=_at(895), reply_to=REPOST)
    _ingest(conn, VIP, REPOST_2, posted=_at(THIRD))
    assert _link(conn, RELAY, 7005) == 1

    assert _withdraw(conn, "event").carried_by_signal_id == 2

    assert _link(conn, RELAY, 7005) == 2
    assert _link(conn, VIP, REPOST) == 2
    for followup in (reply_to_call, reply_to_repost):
        assert relay_repo.signal_for_followup(conn, followup_id=followup.followup_id) == 2


@pytest.mark.parametrize("third_on", THIRDS)
def test_deleting_the_carrier_afterwards_promotes_the_repost_once(conn, third_on):  # noqa: F811
    """Signal 2 carries the call; the VIP's repost 4012 repeats it. On the
    VIP, deleting signal 2's own post promotes 4012 -- still one live
    signal. A relay's own post going withdraws the call (a copy on
    another channel is never promoted, ruling R3-P1)."""
    _four_posts(conn, third_on=third_on, order="second-first", bump=False)
    _withdraw(conn, "event")
    channel, message_id = _third(third_on)

    result = edits_repo.apply_deletion(
        conn, channel_cfg=channel, message_id=message_id, now_utc=_at(950), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()

    live = _q(conn, "SELECT signal_id FROM signals WHERE state = 'NEW' AND withdrawn_at_utc IS NULL")
    if third_on == "vip":
        assert result.promoted_signal_id == 3
        assert _signals(conn)[2] == (3, V, REPOST, "NEW", 0)
        assert live == [(3,)]
    else:
        assert live == []
    assert len({s for _, _, s in _claim_all(conn, 960)}) <= 1


# -- probe 3: a worker that executes at once ----------------------------------


def _worker(conn, seconds: float) -> list[tuple]:  # noqa: F811
    """Plan 3's worker, simulated: every claimable ENTRY at once, through
    Plan 1's own age gate (120 s) -- stale: SKIPPED, fresh: DONE."""
    now = _at(seconds)
    snapshot = MarketSnapshot(
        bid=4482, ask=4482.2, tick_time_utc=now, spread_usd=0.2, median_spread_usd=0.2,
        now_utc=now,
    )
    acted = []
    for account in ACCOUNTS:
        while (row := outbox.claim(
            conn, account_id=account.account_id, pid=5, now_utc=now
        )) is not None:
            channel_id, message_id, text, posted = conn.execute(
                "SELECT m.channel_id, m.message_id, m.raw_text, m.posted_at_utc FROM signals s "
                "JOIN messages m USING (channel_id, message_id) WHERE s.signal_id = ?",
                (row.signal_id,),
            ).fetchone()
            signal = route(
                text, channel_cfg=VIP if channel_id == V else RELAY, message_id=message_id,
                posted_at_utc=datetime.fromisoformat(posted),
            ).signal
            state = "DONE" if gates.age_gate(signal, None, snapshot, Policy()).ok else "SKIPPED"
            outbox.mark(
                conn, outbox_id=row.outbox_id, state=state, claimed_by_pid=row.claimed_by_pid,
                attempts=row.attempts, reason="test worker",
            )
            conn.commit()
            acted.append((account.account_id, row.signal_id, state))
    return acted


@pytest.mark.parametrize("third_on", THIRDS)
@pytest.mark.parametrize("how", HOWS)
def test_a_call_already_executed_as_another_signal_is_not_opened_again(
    conn, how, third_on  # noqa: F811
):
    """Signal 1 is first seen stale (its entries are skipped, it stays
    unexecuted); signal 2 is executed at once. The withdrawal of 4001
    must not open the same trade a second time."""
    _ingest(conn, VIP, CALL)
    acted = _worker(conn, 300)
    _ingest(conn, VIP, REPOST, posted=_at(SECOND))
    acted += _worker(conn, SECOND + 1)
    channel, message_id = _third(third_on)
    _ingest(conn, channel, message_id, posted=_at(THIRD))
    acted += _worker(conn, THIRD + 1)

    result = _withdraw(conn, how)
    acted += _worker(conn, WITHDRAWN + 1)

    assert result.carried_by_signal_id == 2
    done = sorted((account, signal) for account, signal, state in acted if state == "DONE")
    assert done == [("acct-1", 2), ("acct-2", 2)]
    assert "already traded as signal 2" in _detail(conn, edits_repo.SIGNAL_REPOST_ALREADY_LIVE)


# -- end to end: the real listener, poll and writer -----------------------------


def _forward_of_call(message_id: int, after: timedelta) -> FakeMessage:
    header = types.MessageFwdHeader(
        date=POSTED, from_id=types.PeerChannel(channel_id=abs(V) - 1000000000000),
        channel_post=CALL,
    )
    return FakeMessage(id=message_id, message=SIGNAL_TEXT, date=POSTED + after, fwd_from=header)


@pytest.mark.parametrize("bump", [False, True], ids=["by-hand", "forward"])
@pytest.mark.parametrize("third_on", THIRDS)
@pytest.mark.parametrize("how", HOWS)
def test_end_to_end_the_same_call_is_never_queued_twice(tmp_path, how, third_on, bump):
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        second = (
            _forward_of_call(REPOST, timedelta(seconds=SECOND)) if bump
            else _post(REPOST, after=timedelta(seconds=SECOND))
        )
        assert await client.emit(V, second)
        await _round(listener, time)
        third = _post(REPOST_2 if third_on == "vip" else COPY, after=timedelta(seconds=THIRD))
        assert await client.emit(V if third_on == "vip" else R, third)
        await _round(listener, time)
        if how == "event":
            assert await client.emit_delete(V, CALL)
        elif how == "window":
            with client.blackout():
                assert not await client.emit_delete(V, CALL)
            for _ in range(3):
                await _round(listener, time)
        else:
            assert await client.emit_edit(V, CALL, CHATTER_TEXT, edit_date=_at(WITHDRAWN))
        await _round(listener, time)

    _run(db_path, body, now=lambda: _at(WITHDRAWN))

    state = _state(db_path)
    third_id = REPOST_2 if third_on == "vip" else COPY
    assert [row[:3] for row in state["signals"]] == [(1, V, CALL), (2, V if third_on == "vip" else R, third_id)]
    assert state["queued"] == [(2, len(ACCOUNTS))]
    assert (V, REPOST, 2) in state["links"]
    codes = [code for _, code, *_ in state["alerts"]]
    assert edits_repo.SIGNAL_REPOST_ALREADY_LIVE in codes
    assert edits_repo.SIGNAL_REPOST_PROMOTED not in codes
    # The call is still queued, as signal 2: no "check they really were
    # deleted" summary for a window deletion (as for a promotion, G3L-2).
    assert edits_repo.WINDOW_VOIDED_SIGNALS not in codes
    conn = db.connect(db_path, role="writer")
    try:
        assert {s for _, _, s in _claim_all(conn, WITHDRAWN + 10)} == {2}
    finally:
        conn.close()
    assert _db_rows(db_path, "SELECT COUNT(*) FROM signals WHERE promoted_from_signal_id = 1") == [(0,)]


# -- which signal carries the call ------------------------------------------------


def _amend_call(conn, text: str, seconds: float = 100) -> None:  # noqa: F811
    result = edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=CALL, text=text, posted_at_utc=POSTED,
        edit_date_utc=_at(seconds), reply_to_message_id=None, now_utc=_at(seconds),
        accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()
    assert result.outcome == "reparsed", result


@pytest.mark.parametrize(
    ("repost", "third"),
    [(SIGNAL_TEXT, SL_EDIT), (SL_EDIT, SIGNAL_TEXT)],
    ids=["repost-shows-the-first-stop", "repost-shows-the-moved-stop"],
)
def test_the_signal_carrying_the_call_is_found_whichever_stop_each_post_shows(
    conn, repost, third  # noqa: F811
):
    """The poster moved signal 1's stop (4470 -> 4460) by an edit. The
    repost and the third post each show one of the two stops: the third
    post is still the same trade as signal 1 -- its current levels or its
    first ones -- so the repost is not promoted beside it."""
    _ingest(conn, VIP, CALL)
    _amend_call(conn, SL_EDIT)
    _ingest(conn, VIP, REPOST, repost, posted=_at(SECOND))
    assert _link(conn, VIP, REPOST) == 1
    assert _ingest(conn, VIP, REPOST_2, third, posted=_at(THIRD)).signal_id == 2

    assert _withdraw(conn, "event").carried_by_signal_id == 2
    assert _q(conn, "SELECT COUNT(*) FROM signals") == [(2,)]


def test_a_withdrawn_signal_of_the_same_trade_does_not_carry_the_call(conn):  # noqa: F811
    """Signal 2 (the relay's post at +910) was itself withdrawn before
    anything of it was executed: it carries nothing, so the VIP's standing
    repost is promoted as before."""
    _four_posts(conn, third_on="relay", order="second-first", bump=False)
    edits_repo.apply_deletion(
        conn, channel_cfg=RELAY, message_id=COPY, now_utc=_at(912), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()

    result = _withdraw(conn, "event")

    assert (result.promoted_signal_id, result.carried_by_signal_id) == (3, None)
    assert _claim_all(conn, WITHDRAWN + 10) == [("acct-1", "ENTRY", 3), ("acct-2", "ENTRY", 3)]


@pytest.mark.parametrize(
    ("state", "words"),
    [
        ("CLAIMED", "already being traded as signal 2"),
        (
            "SKIPPED",
            "already signal 2, which is not being traded (none of its trade entries is waiting)",
        ),
    ],
)
def test_the_warning_says_what_became_of_the_signal_carrying_the_call(
    conn, state, words  # noqa: F811
):
    _four_posts(conn, third_on="vip", order="second-first", bump=False)
    conn.execute("UPDATE outbox SET state = ? WHERE signal_id = 2", (state,))
    conn.commit()

    _withdraw(conn, "event")

    assert words in _detail(conn, edits_repo.SIGNAL_REPOST_ALREADY_LIVE)


def test_a_repeat_deleted_before_reaches_the_signal_carrying_the_call(conn):  # noqa: F811
    """A double post of signal 1 removed earlier (the window inferred it
    deleted) still belongs to the call: read back, its edits and replies
    reach signal 2, which carries the call now -- as they would reach a
    promoted repost."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, VIP, 4011, posted=_at(5))
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4011, now_utc=_at(50), accounts=ACCOUNTS,
        detected_by="window",
    )
    _ingest(conn, VIP, REPOST, posted=_at(SECOND))
    _ingest(conn, VIP, REPOST_2, posted=_at(THIRD))

    assert _withdraw(conn, "event").carried_by_signal_id == 2

    assert relay_repo.signal_for_post(conn, channel_id=V, message_id=4011) == 2


# -- FF16: the promoted post keeps the stop the poster moved ----------------------


@pytest.mark.parametrize("bump", [False, True], ids=["by-hand", "forward"])
def test_the_promoted_repost_is_one_showing_the_calls_current_levels(conn, bump):  # noqa: F811
    """Final fix FF16: the poster moved signal 1's stop (4470 -> 4460) by
    an edit. An earlier repost (by hand, or the channel's own forward,
    which Telegram never updates) still shows 4470; a later one shows
    4460. Deleting the original promotes the one showing 4460 -- the
    trade the poster means -- not the earliest; the other repeats it."""
    _ingest(conn, VIP, CALL, posted=_at(10))
    _ingest(conn, VIP, REPOST, posted=_at(15), fwd=(V, CALL) if bump else None)
    _amend_call(conn, SL_EDIT, seconds=20)
    _ingest(conn, VIP, REPOST_2, SL_EDIT, posted=_at(25))
    assert (_link(conn, VIP, REPOST), _link(conn, VIP, REPOST_2)) == (1, 1)

    result = _withdraw(conn, "event", seconds=30)

    assert result.promoted_signal_id == 2
    (row,) = _q(conn, "SELECT message_id, parsed_json FROM signals WHERE signal_id = 2")
    assert row[0] == REPOST_2
    assert json.loads(row[1])["sl"] == 4460
    assert _link(conn, VIP, REPOST) == 2


def test_with_no_repost_at_the_current_levels_the_earliest_is_promoted(conn):  # noqa: F811
    _ingest(conn, VIP, CALL, posted=_at(10))
    _ingest(conn, VIP, REPOST, posted=_at(15))
    _ingest(conn, VIP, REPOST_2, posted=_at(25))
    _amend_call(conn, SL_EDIT, seconds=30)

    assert _withdraw(conn, "event", seconds=40).promoted_signal_id == 2
    assert _q(conn, "SELECT message_id FROM signals WHERE signal_id = 2") == [(REPOST,)]


# -- final fix round 2: a signal whose channel was switched off carries nothing -----


def _restart_with_relay_off(conn) -> dict[int, int]:  # noqa: F811
    """`watch` starts again with the relay switched off: its waiting trade
    entries are cancelled (`outbox.skip_entries_of_channels_off`)."""
    skipped = outbox.skip_entries_of_channels_off(conn, enabled_channel_ids=(V,))
    conn.commit()
    return skipped


def test_a_signal_whose_channel_was_switched_off_never_blocks_a_promotion(conn):  # noqa: F811
    """The re-check's p_carrier_off. The VIP's call (signal 1) has a repost
    at +890; the relay's own post at +910 is signal 2. The relay is
    switched off and watch restarts, so signal 2's entries are cancelled.
    Then the VIP deletes its call. Signal 2 can no longer trade, so it does
    not carry the call: the VIP's standing repost is promoted and queued
    -- before, nothing was queued and the WARNING said "Do NOT trade it by
    hand", so the call still standing on a channel switched ON was never
    traded."""
    _four_posts(conn, third_on="relay", order="second-first", bump=False)
    assert _restart_with_relay_off(conn) == {R: len(ACCOUNTS)}

    result = _withdraw(conn, "event")

    assert (result.promoted_signal_id, result.carried_by_signal_id) == (3, None)
    assert _signals(conn)[2] == (3, V, REPOST, "NEW", 0)
    assert _claim_all(conn, WITHDRAWN + 10) == [("acct-1", "ENTRY", 3), ("acct-2", "ENTRY", 3)]
    codes = [code for _, code, _ in _codes(conn)]
    assert edits_repo.SIGNAL_REPOST_PROMOTED in codes
    assert edits_repo.SIGNAL_REPOST_ALREADY_LIVE not in codes


@pytest.mark.parametrize("bump", [False, True], ids=["by-hand", "forward"])
def test_a_call_on_a_channel_on_is_traded_though_a_channel_switched_off_had_it(
    conn, bump  # noqa: F811
):
    """The silent case before FF12. The relay's post is signal 1; the relay
    is switched off (its entries cancelled at the restart), and 15 s later
    the VIP posts the same call -- or forwards the relay's post. It was
    stored as a copy of signal 1, which nothing trades, with no alert:
    the VIP's call was never traded. A live call wins only when it can
    still trade."""
    assert _ingest(conn, RELAY, COPY).signal_id == 1
    _restart_with_relay_off(conn)

    result = _ingest(conn, VIP, CALL, posted=_at(15), fwd=(R, COPY) if bump else None)

    assert (result.outcome, result.signal_id, result.relay_of_signal_id) == ("new", 2, None)
    assert _claim_all(conn, 20) == [("acct-1", "ENTRY", 2), ("acct-2", "ENTRY", 2)]


def test_a_held_back_copy_never_names_a_signal_whose_channel_was_switched_off(
    conn,  # noqa: F811
):
    """The relay's post is signal 1, and the relay is switched off. The VIP
    posts the call (signal 2) and deletes it; another channel then copies
    it: held back, CRITICAL. Its advice named signal 1 -- "do NOT trade it
    by hand as well, that would open the trade twice" -- though nothing
    trades signal 1. A signal that can no longer trade is no carrier."""
    assert _ingest(conn, RELAY, COPY).signal_id == 1
    _restart_with_relay_off(conn)
    assert _ingest(conn, VIP, CALL, posted=_at(15)).signal_id == 2
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=CALL, now_utc=_at(20), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()

    copy = _ingest(conn, OTHER, 9001, posted=_at(25))

    assert copy.relay_of_signal_id == 2
    detail = _detail(conn, relay_repo.COPY_OF_WITHDRAWN_SIGNAL)
    assert "If the call is still meant, trade it by hand." in detail
    assert "do NOT trade it by hand" not in detail
    assert _claim_all(conn, 30) == []


def test_a_carrier_a_worker_refused_still_carries_the_call(conn):  # noqa: F811
    """Only a channel switched off makes a signal no carrier: signal 2's
    entries refused by a worker (too old, say) still stand for the call,
    so the VIP's repost is not promoted -- a later copy must never re-open
    a call the age gate refused. Nothing is waiting for signal 2, so the
    WARNING never says "Do NOT trade it by hand" (it would not open the
    trade twice)."""
    _four_posts(conn, third_on="relay", order="second-first", bump=False)
    conn.execute(
        "UPDATE outbox SET state = 'SKIPPED', reason = 'too old' WHERE signal_id = 2"
    )
    conn.commit()

    result = _withdraw(conn, "event")

    assert (result.promoted_signal_id, result.carried_by_signal_id) == (None, 2)
    detail = _detail(conn, edits_repo.SIGNAL_REPOST_ALREADY_LIVE)
    assert "not being traded" in detail
    assert "Do NOT trade it by hand" not in detail
    assert _claim_all(conn, WITHDRAWN + 10) == []


def test_a_forward_of_a_post_from_a_channel_switched_off_repeats_the_call_live_elsewhere(
    conn,  # noqa: F811
):
    """The relay's post (signal 1) can no longer trade -- the relay is
    switched off -- and the VIP has posted the call as signal 2. Another
    channel then forwards the relay's post: it repeats signal 2, the call
    live on a channel switched on, never a third signal (that would open
    the trade twice)."""
    assert _ingest(conn, RELAY, COPY).signal_id == 1
    _restart_with_relay_off(conn)
    assert _ingest(conn, VIP, CALL, posted=_at(15)).signal_id == 2

    forward = _ingest(conn, OTHER, 9001, posted=_at(20), fwd=(R, COPY))

    assert (forward.outcome, forward.relay_of_signal_id) == ("relay_duplicate", 2)
    assert _claim_all(conn, 30) == [("acct-1", "ENTRY", 2), ("acct-2", "ENTRY", 2)]


def _relay_call_promoted_then_switched_off(conn) -> None:  # noqa: F811
    """The relay's call (signal 1) and its repost; the relay deletes the
    call, so the repost is promoted (signal 2); then the relay is switched
    off and watch restarts: signal 2's entries are cancelled."""
    assert _ingest(conn, RELAY, COPY).signal_id == 1
    _ingest(conn, RELAY, 7002, posted=_at(5))
    deleted = edits_repo.apply_deletion(
        conn, channel_cfg=RELAY, message_id=COPY, now_utc=_at(10), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()
    assert deleted.promoted_signal_id == 2
    _restart_with_relay_off(conn)


def test_a_forward_of_a_withdrawn_post_whose_call_lives_on_a_channel_switched_off_is_traded(
    conn,  # noqa: F811
):
    """The VIP forwards the relay's deleted post. Its call lives on as
    signal 2, the relay's promoted repost -- which nothing trades: the
    relay is switched off. The forward was stored as a copy of signal 2,
    silently; it is a new signal now."""
    _relay_call_promoted_then_switched_off(conn)

    forward = _ingest(conn, VIP, CALL, posted=_at(20), fwd=(R, COPY))

    assert (forward.outcome, forward.signal_id, forward.relay_of_signal_id) == ("new", 3, None)
    assert _claim_all(conn, 30) == [("acct-1", "ENTRY", 3), ("acct-2", "ENTRY", 3)]


def test_a_withdrawn_call_living_on_a_channel_switched_off_has_no_carrier(conn):  # noqa: F811
    """`carrier_of`, which decides whether an alert may say "do NOT trade
    it by hand as well": signal 1's call lives on as signal 2, which can no
    longer trade (its channel was switched off), so nothing carries it."""
    _relay_call_promoted_then_switched_off(conn)

    assert relay_repo.carrier_of(conn, signal_id=1, around=(_at(10),)) is None


def test_a_forward_of_a_withdrawn_post_whose_call_was_posted_again_on_a_channel_off(
    conn,  # noqa: F811
):
    """The relay deleted its call (signal 1) and posted it again (signal 2,
    in its place); then the relay is switched off. The VIP forwards the
    relay's deleted post: the call lives on only as signal 2, which
    nothing trades, so the forward is a new signal -- not a silent copy."""
    assert _ingest(conn, RELAY, COPY).signal_id == 1
    edits_repo.apply_deletion(
        conn, channel_cfg=RELAY, message_id=COPY, now_utc=_at(10), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()
    again = _ingest(conn, RELAY, 7003, posted=_at(20))
    assert (again.outcome, again.signal_id) == ("new", 2)
    _restart_with_relay_off(conn)

    forward = _ingest(conn, VIP, CALL, posted=_at(30), fwd=(R, COPY))

    assert (forward.outcome, forward.signal_id, forward.relay_of_signal_id) == ("new", 3, None)
    assert _claim_all(conn, 40) == [("acct-1", "ENTRY", 3), ("acct-2", "ENTRY", 3)]


@pytest.mark.parametrize(
    ("state", "words"),
    [("DONE", "already traded as signal 2"), ("CLAIMED", "already being traded as signal 2")],
)
def test_a_signal_traded_before_its_channel_was_switched_off_still_carries_the_call(
    conn, state, words  # noqa: F811
):
    """One account traded signal 2 (or is trading it) before the relay was
    switched off; the restart cancels only the other account's waiting
    entry. Signal 2 still carries the call: promoting the VIP's repost
    would open the trade a second time on the first account."""
    _four_posts(conn, third_on="relay", order="second-first", bump=False)
    conn.execute(
        "UPDATE outbox SET state = ? WHERE signal_id = 2 AND account_id = 'acct-1'", (state,)
    )
    conn.commit()
    assert _restart_with_relay_off(conn) == {R: 1}

    result = _withdraw(conn, "event")

    assert (result.promoted_signal_id, result.carried_by_signal_id) == (None, 2)
    assert words in _detail(conn, edits_repo.SIGNAL_REPOST_ALREADY_LIVE)
    assert _q(conn, "SELECT COUNT(*) FROM outbox WHERE state = 'QUEUED'") == [(0,)]


def test_a_signal_with_an_entry_still_waiting_is_not_switched_off(conn):  # noqa: F811
    """`relay_repo.switched_off` on its own: a cancelled entry says the
    channel was switched off, but one still waiting means the signal can
    still trade."""
    assert _ingest(conn, RELAY, COPY).signal_id == 1
    conn.execute(
        "UPDATE outbox SET state = 'SKIPPED', reason = ? WHERE signal_id = 1 "
        "AND account_id = 'acct-1'",
        (outbox.CHANNEL_OFF_REASON,),
    )
    row = conn.execute("SELECT * FROM signals WHERE signal_id = 1").fetchone()

    assert not relay_repo.switched_off(conn, row)
    conn.execute("UPDATE outbox SET state = 'SKIPPED', reason = 'too old' WHERE signal_id = 1 "
                 "AND account_id = 'acct-2'")
    assert relay_repo.switched_off(conn, row)


# -- final fix round 2: which signal carries the call, pinned ---------------------


def test_a_signal_of_the_same_trade_outside_the_window_carries_nothing(conn):  # noqa: F811
    """The re-check's p_carrier_window. Day 1: the call is posted and
    traded. Day 2: the same call is posted again (signal 2 -- a day is far
    outside the window), reposted a minute later, and its first post is
    deleted. Yesterday's traded signal is another call, not the carrier:
    the standing repost is promoted and queued."""
    _ingest(conn, VIP, CALL)
    for account in ACCOUNTS:
        row = outbox.claim(conn, account_id=account.account_id, pid=7, now_utc=_at(5))
        outbox.mark(
            conn, outbox_id=row.outbox_id, state="DONE", claimed_by_pid=7,
            attempts=row.attempts,
        )
        conn.commit()
    day2 = timedelta(days=1).total_seconds()
    assert _ingest(conn, VIP, 5001, posted=_at(day2)).signal_id == 2
    assert _ingest(conn, VIP, 5002, posted=_at(day2 + 60)).relay_of_signal_id == 2

    result = edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=5001, now_utc=_at(day2 + 90), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()

    assert (result.promoted_signal_id, result.carried_by_signal_id) == (3, None)
    assert _claim_all(conn, day2 + 100) == [("acct-1", "ENTRY", 3), ("acct-2", "ENTRY", 3)]


def test_the_carrier_is_the_newest_signal_of_the_call_it_finds(conn):  # noqa: F811
    """The re-check's p_carrier_newest. 4001 at 0 (signal 1), its repost
    at +890, the call again at +910 (signal 2, outside 4001's window), its
    repost at +1800. Deleting +910 promotes +1800 (signal 3). Deleting
    4001 then finds signal 2 near the +890 repost -- withdrawn, but its
    call lives on as signal 3, which carries it: one trade, not a fourth
    signal beside signal 3."""
    for message_id, seconds in ((CALL, 0), (REPOST, 890), (REPOST_2, 910), (4014, 1800)):
        _ingest(conn, VIP, message_id, posted=_at(seconds))
    promoted = edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=REPOST_2, now_utc=_at(1805), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()
    assert promoted.promoted_signal_id == 3

    result = _withdraw(conn, "event", seconds=1810)

    assert (result.promoted_signal_id, result.carried_by_signal_id) == (None, 3)
    assert _q(conn, "SELECT COUNT(*) FROM signals") == [(3,)]
    assert _claim_all(conn, 1820) == [("acct-1", "ENTRY", 3), ("acct-2", "ENTRY", 3)]


def test_a_reply_to_a_post_that_re_aimed_its_call_stays_with_that_signal(conn):  # noqa: F811
    """The re-check's p_carrier_kept. 4001 is re-aimed (BUY -> SELL) while
    its repost stands: the repost is promoted (signal 2), and 4001 stays
    signal 1's closer. A reply to 4001 is about signal 1's call as it
    stood, and stays with the signal the re-aim left it on (as
    `record_replacement` keeps it) when signal 2 is withdrawn in turn and
    its call is carried by signal 3 -- never moved to signal 3."""
    _ingest(conn, VIP, CALL)
    _ingest(conn, VIP, REPOST, posted=_at(5))
    reply = _ingest(conn, VIP, 4003, CLOSE_HALF, posted=_at(7), reply_to=CALL)
    edited = edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=CALL, text=SIGNAL_TEXT.replace("BUY", "SELL"),
        posted_at_utc=POSTED, edit_date_utc=_at(10), reply_to_message_id=None,
        now_utc=_at(10), accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()
    assert edited.promoted_signal_id == 2
    before = relay_repo.signal_for_followup(conn, followup_id=reply.followup_id)
    _ingest(conn, VIP, 4004, posted=_at(890))
    assert _ingest(conn, VIP, 4005, posted=_at(910)).signal_id == 3

    deleted = edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=REPOST, now_utc=_at(920), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()

    assert deleted.carried_by_signal_id == 3
    assert before == 2
    assert relay_repo.signal_for_followup(conn, followup_id=reply.followup_id) == 2
    assert _q(conn, "SELECT target_signal_id FROM followups WHERE followup_id = ?",
              reply.followup_id) == [(2,)]
