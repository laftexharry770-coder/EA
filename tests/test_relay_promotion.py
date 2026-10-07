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
        ("SKIPPED", "already signal 2 (none of its trade entries is waiting)"),
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
