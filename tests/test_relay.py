"""Relay copies: the same call, or the same follow-up, reaching us twice
(spec G7; final review F02, F04, F08, F09).

The user follows a VIP channel and channels that copy it -- by forwarding
its posts, or by posting the same call again. A copy must never open a
second set of legs, and must never be silent either:

- **the link** (F02): a signal message stored as a copy records the
  signal it repeated (`messages.relay_of_signal_id`), so an edit or a
  deletion of the copy can reach that signal;
- **forwards** (F09): a forward of a stored post -- or of another stored
  forward of it -- is a copy however late it arrives; the 15-minute
  trade-hash window stays for calls posted again by hand;
- **withdrawn calls** (F04): a poster who deletes (or voids) a call nobody
  took yet and posts it again gets the new post traded; a copy of a
  withdrawn call from elsewhere is held back, loudly;
- **follow-ups** (F08): a follow-up replying to a stored signal records
  that signal; one repeated by another channel is stored, not dispatched
  twice;
- **visibility**: every copy is logged with both messages and counted.

Unit-level tests drive `messages_repo.ingest()` on a temporary database;
the arrival-path tests at the bottom run the real listener and poll
against the fake Telegram client.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fakes.telegram import FakeMessage, FakeTelegramClient
from test_edits import ACCOUNTS as LIVE_ACCOUNTS
from test_edits import CHATTER_TEXT as EDIT_CHATTER
from test_edits import (
    DIRECTION_EDIT,
    EDIT_1,
    EDIT_2,
    NOW,
    SL_EDIT,
    UNPARSEABLE_EDIT,
    ScriptedClient,
    SteppedTime,
    _round,
    _until_asleep,
    _without,
)
from xaucopier.config.channels import ChannelCfg
from xaucopier.domain.signal import FollowUp, FollowUpOp
from xaucopier.ingest.listener import HistoryIngest, Listener
from xaucopier.ingest.poll import HistoryPoll, split_new
from xaucopier.store import db, edits_repo, outbox, relay_repo, signals_repo
from xaucopier.store.messages_repo import IngestResult, ingest

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "messages" / "mmh_buy_4tp.json"
_FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
SIGNAL_TEXT: str = _FIXTURE["text"]
POSTED = datetime.fromisoformat(_FIXTURE["posted_at_utc"])
assert SIGNAL_TEXT.count("4470") == 1
OTHER_CALL = SIGNAL_TEXT.replace("4470", "4465")  # a different trade (another SL)
CHATTER_TEXT = "Good morning traders"

VIP = ChannelCfg(
    channel_id=_FIXTURE["channel_id"], name="VIP", parser="mmh", ordinal=1, enabled=True
)
RELAY = ChannelCfg(channel_id=-1002222222222, name="Relay", parser="mmh", ordinal=2, enabled=True)
OTHER = ChannelCfg(channel_id=-1003333333333, name="Other", parser="mmh", ordinal=3, enabled=True)
#: A channel nobody watches, whose posts reach us only as forwards.
UNWATCHED = -1004444444444


@dataclass(frozen=True)
class _Account:
    account_id: str
    enabled: bool


ACCOUNTS = (_Account("acct-1", True), _Account("acct-2", True))


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "store.db", role="writer")
    try:
        yield connection
    finally:
        connection.close()


def _ingest(
    conn,
    channel: ChannelCfg,
    message_id: int,
    text: str = SIGNAL_TEXT,
    *,
    posted: datetime = POSTED,
    reply_to: int | None = None,
    fwd: tuple[int, int] | None = None,
    window: int = 900,
    source: str = "live",
) -> IngestResult:
    result = ingest(
        conn,
        channel_cfg=channel,
        message_id=message_id,
        text=text,
        posted_at_utc=posted,
        edit_date_utc=None,
        reply_to_message_id=reply_to,
        source=source,
        now_utc=posted + timedelta(seconds=1),
        accounts=ACCOUNTS,
        relay_window_seconds=window,
        fwd_from_channel_id=None if fwd is None else fwd[0],
        fwd_from_post_id=None if fwd is None else fwd[1],
        fwd_from_date_utc=None if fwd is None else POSTED,
    )
    conn.commit()
    return result


def _q(conn, sql: str, *params) -> list[tuple]:
    return [tuple(row) for row in conn.execute(sql, params).fetchall()]


def _link(conn, channel: ChannelCfg, message_id: int) -> int | None:
    (row,) = _q(
        conn,
        "SELECT relay_of_signal_id FROM messages WHERE channel_id = ? AND message_id = ?",
        channel.channel_id,
        message_id,
    )
    return row[0]


def _signals(conn) -> list[tuple]:
    return _q(conn, "SELECT signal_id, channel_id, message_id, state FROM signals ORDER BY 1")


def _queued_entries(conn) -> list[tuple]:
    return _q(
        conn,
        "SELECT signal_id, account_id FROM outbox WHERE kind = 'ENTRY' AND state = 'QUEUED' "
        "ORDER BY outbox_id",
    )


def _metric(conn, name: str) -> int:
    return _q(conn, "SELECT COALESCE(SUM(value), 0) FROM metrics WHERE name = ?", name)[0][0]


def _alerts(conn) -> list[tuple]:
    return _q(conn, "SELECT level, code, channel_id, message_id, signal_id FROM alerts ORDER BY 1")


# -- the link (F02) ------------------------------------------------------------


def test_a_copy_records_the_signal_it_repeated(conn):
    first = _ingest(conn, VIP, 4001)
    copy = _ingest(conn, RELAY, 7001, posted=POSTED + timedelta(seconds=45))

    assert first.outcome == "new"
    assert (copy.outcome, copy.kind, copy.signal_id) == ("relay_duplicate", "signal", None)
    assert copy.relay_of_signal_id == first.signal_id
    assert _link(conn, RELAY, 7001) == first.signal_id
    assert _link(conn, VIP, 4001) is None  # the signal's own message is no copy
    assert _signals(conn) == [(first.signal_id, VIP.channel_id, 4001, "NEW")]


def test_a_new_signal_reports_no_copy(conn):
    assert _ingest(conn, VIP, 4001).relay_of_signal_id is None


# -- forwards (F09) --------------------------------------------------------------


@pytest.mark.parametrize("lag", [60, 900, 901, 1800, 7200])
def test_a_forward_of_a_stored_post_is_a_copy_however_late(conn, lag):
    """The copy's own date is when it was forwarded: a VIP call forwarded
    more than 15 minutes late used to become a second ENTRY."""
    first = _ingest(conn, VIP, 4001)
    copy = _ingest(
        conn,
        RELAY,
        7001,
        posted=POSTED + timedelta(seconds=lag),
        fwd=(VIP.channel_id, 4001),
    )

    assert copy.outcome == "relay_duplicate"
    assert copy.relay_of_signal_id == first.signal_id
    assert len(_signals(conn)) == 1
    assert len(_queued_entries(conn)) == len(ACCOUNTS)


@pytest.mark.parametrize(
    ("lag", "outcome"),
    [(60, "relay_duplicate"), (900, "relay_duplicate"), (901, "new"), (1800, "new"), (7200, "new")],
)
def test_the_same_call_posted_again_by_hand_is_a_copy_only_inside_the_window(conn, lag, outcome):
    """No forward header: the trade-hash window (15 minutes) decides."""
    _ingest(conn, VIP, 4001)
    copy = _ingest(conn, RELAY, 7001, posted=POSTED + timedelta(seconds=lag))

    assert copy.outcome == outcome
    assert len(_signals(conn)) == (1 if outcome == "relay_duplicate" else 2)


def test_a_forward_is_matched_through_another_stored_forward_of_the_same_post(conn):
    """The VIP channel is not watched; two channels forward its post two
    hours apart. The second is a copy of the first."""
    first = _ingest(conn, RELAY, 7001, fwd=(UNWATCHED, 900))
    copy = _ingest(
        conn, OTHER, 8001, posted=POSTED + timedelta(hours=2), fwd=(UNWATCHED, 900)
    )

    assert first.outcome == "new"
    assert copy.outcome == "relay_duplicate"
    assert copy.relay_of_signal_id == first.signal_id


def test_a_forward_of_a_copy_repeats_the_signal_the_copy_repeated(conn):
    holder = _ingest(conn, VIP, 4001)
    _ingest(conn, RELAY, 7001, posted=POSTED + timedelta(seconds=30))  # a copy by hand
    copy_of_copy = _ingest(
        conn, OTHER, 8001, posted=POSTED + timedelta(hours=3), fwd=(RELAY.channel_id, 7001)
    )

    assert copy_of_copy.outcome == "relay_duplicate"
    assert copy_of_copy.relay_of_signal_id == holder.signal_id


def test_the_original_stored_after_its_late_forward_is_a_copy_of_it(conn):
    """The forward was stored first (the VIP post was missed, or the VIP
    channel was switched on later): the original, two hours older than
    the forward, still repeats the forward's signal."""
    forward = _ingest(
        conn, RELAY, 7001, posted=POSTED + timedelta(hours=2), fwd=(VIP.channel_id, 4001)
    )
    original = _ingest(conn, VIP, 4001, source="poll")

    assert forward.outcome == "new"
    assert original.outcome == "relay_duplicate"
    assert original.relay_of_signal_id == forward.signal_id


def test_a_forward_of_a_stored_post_that_is_no_signal_falls_back_to_the_window(conn):
    """Channel VIP stored its post as chatter; the forward reads as a
    signal on its own. No signal to repeat, and nothing within the window:
    a new signal."""
    _ingest(conn, VIP, 4001, CHATTER_TEXT)
    forward = _ingest(
        conn, RELAY, 7001, posted=POSTED + timedelta(hours=2), fwd=(VIP.channel_id, 4001)
    )
    assert forward.outcome == "new"


def test_a_forward_matches_by_the_post_not_by_the_trade(conn):
    """A forward of 4001 is a copy of 4001's signal even if its text reads
    as another trade (4001 was edited after it was forwarded)."""
    holder = _ingest(conn, VIP, 4001)
    forward = _ingest(
        conn,
        RELAY,
        7001,
        OTHER_CALL,
        posted=POSTED + timedelta(hours=1),
        fwd=(VIP.channel_id, 4001),
    )
    assert forward.outcome == "relay_duplicate"
    assert forward.relay_of_signal_id == holder.signal_id


def test_a_forward_of_another_post_is_not_a_copy(conn):
    _ingest(conn, VIP, 4001)
    forward = _ingest(
        conn,
        RELAY,
        7001,
        OTHER_CALL,
        posted=POSTED + timedelta(hours=1),
        fwd=(VIP.channel_id, 4002),
    )
    assert forward.outcome == "new"


# -- the window boundary (L71) ---------------------------------------------------


@pytest.mark.parametrize("copy_first", [False, True], ids=["copy-after", "copy-before"])
@pytest.mark.parametrize(
    ("window", "gap", "outcome"),
    [
        (900, 900, "relay_duplicate"),
        (900, 901, "new"),
        (60, 60, "relay_duplicate"),
        (60, 61, "new"),
    ],
)
def test_the_relay_window_includes_exactly_its_own_length(conn, window, gap, outcome, copy_first):
    """Telegram dates are whole seconds: a copy exactly `relay_window_seconds`
    away (either side) is still a copy; one second more is not."""
    copy_posted = POSTED + timedelta(seconds=-gap if copy_first else gap)
    _ingest(conn, VIP, 4001, window=window)
    copy = _ingest(conn, RELAY, 7001, posted=copy_posted, window=window)
    assert copy.outcome == outcome


# -- visibility ------------------------------------------------------------------


def test_every_copy_is_logged_with_both_messages_and_counted(conn, caplog):
    _ingest(conn, VIP, 4001)
    with caplog.at_level(logging.INFO, logger="xaucopier.store.relay_repo"):
        _ingest(conn, RELAY, 7001, posted=POSTED + timedelta(seconds=30))
        _ingest(
            conn, OTHER, 8001, posted=POSTED + timedelta(hours=2), fwd=(VIP.channel_id, 4001)
        )

    assert _metric(conn, relay_repo.RELAY_DUPLICATES_METRIC) == 2
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(lines) == 2
    for line, (channel, message_id) in zip(
        lines, ((RELAY.channel_id, 7001), (OTHER.channel_id, 8001)), strict=True
    ):
        assert f"channel {channel} message {message_id}" in line
        assert f"channel {VIP.channel_id} message 4001" in line
        assert "relayed copy" in line
    assert "forward" in lines[1]
    assert _alerts(conn) == []  # a copy of a live call is no alert


def test_a_new_signal_is_not_counted_as_a_copy(conn):
    _ingest(conn, VIP, 4001)
    _ingest(conn, RELAY, 7001, OTHER_CALL, posted=POSTED + timedelta(seconds=30))
    assert _metric(conn, relay_repo.RELAY_DUPLICATES_METRIC) == 0


def _history(outcome: str, message_id: int) -> HistoryIngest:
    return HistoryIngest(
        message_id=message_id,
        result=IngestResult(
            outcome=outcome, signal_id=None, followup_id=None, kind="signal", detail=""
        ),
        posted_at_utc=POSTED,
    )


def test_a_copy_only_a_history_read_found_counts_as_missed_by_the_update_stream():
    """A copy is stored for the first time, like a new message: when only
    the poll found it, the update stream missed it (F02 side note)."""
    results = (
        _history("new", 1),
        _history("relay_duplicate", 2),
        _history("duplicate", 3),
        _history("edited", 4),
    )
    assert split_new(results, live_from=POSTED - timedelta(minutes=1)) == ([1, 2], [])
    assert split_new(results, live_from=POSTED + timedelta(minutes=1)) == ([], [1, 2])


# == the arrival paths: the real listener and poll, the fake Telegram ===========

V = VIP.channel_id
R = RELAY.channel_id
#: The VIP's call, three chatter messages after it (a window re-read
#: needs company to believe an absence), then room for a repost.
CALL = 4001
VIP_CHATTER = (4002, 4003, 4004)
REPOST = 4012
REPOST_2 = 4013
COPY = 7001  # the relay channel's copy
N = len(LIVE_ACCOUNTS)


def _post(
    message_id: int,
    text: str = SIGNAL_TEXT,
    *,
    after: timedelta = timedelta(0),
    reply_to: int | None = None,
) -> FakeMessage:
    return FakeMessage(id=message_id, message=text, date=POSTED + after, reply_to_msg_id=reply_to)


def _db_rows(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = db.connect(db_path, role="reader")
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _db_write(db_path: Path, sql: str, params: tuple = ()) -> None:
    conn = db.connect(db_path, role="writer")
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _run(db_path: Path, body, *, channels=(VIP, RELAY), client=None):
    """Pointers just below each channel's first test id; start the real
    listener and poll; run `body(client, listener, time)`; no handler may
    fail."""
    for channel_id, pointer in ((V, CALL - 1), (R, COPY - 1)):
        _db_write(
            db_path,
            "INSERT INTO channel_state (channel_id, last_processed_message_id) VALUES (?, ?)",
            (channel_id, pointer),
        )
    time = SteppedTime()
    client = client if client is not None else FakeTelegramClient()

    async def scenario():
        listener = Listener(
            client=client,
            channels=channels,
            accounts=LIVE_ACCOUNTS,
            writer=db.Writer(db_path),
            db_path=db_path,
            now=lambda: NOW,
            poll_hook=HistoryPoll(sleep=time.sleep, clock=time.clock),
        )
        await listener.start()
        try:
            await _until_asleep(listener, time)
            result = await body(client, listener, time)
            assert listener.handler_error_count == 0, listener.handler_errors
            return result
        finally:
            await listener.aclose()

    return asyncio.run(scenario())


def _state(db_path: Path) -> dict:
    """What the path tests assert on: signals, QUEUED entries per signal,
    alerts, and every copy's link."""
    return {
        "signals": _db_rows(
            db_path,
            "SELECT signal_id, channel_id, message_id, state, withdrawn_at_utc IS NOT NULL "
            "FROM signals ORDER BY signal_id",
        ),
        "queued": _db_rows(
            db_path,
            "SELECT signal_id, COUNT(*) FROM outbox WHERE kind = 'ENTRY' AND state = 'QUEUED' "
            "GROUP BY signal_id ORDER BY signal_id",
        ),
        "alerts": _db_rows(
            db_path,
            "SELECT level, code, channel_id, message_id, signal_id FROM alerts ORDER BY alert_id",
        ),
        "links": _db_rows(
            db_path,
            "SELECT channel_id, message_id, relay_of_signal_id FROM messages "
            "WHERE relay_of_signal_id IS NOT NULL ORDER BY channel_id, message_id",
        ),
    }


def _codes(state: dict) -> list[tuple[str, str]]:
    return [(level, code) for level, code, *_ in state["alerts"]]


def _alert_detail(db_path: Path, code: str) -> str:
    (row,) = _db_rows(db_path, "SELECT detail FROM alerts WHERE code = ?", (code,))
    return row[0]


def _mark_executed(db_path: Path, signal_id: int = 1) -> None:
    """Plan 3's worker, simulated: acct-1's ENTRY of `signal_id` is DONE."""
    _db_write(
        db_path,
        "UPDATE outbox SET state = 'DONE' WHERE kind = 'ENTRY' AND account_id = 'acct-1' "
        "AND signal_id = ?",
        (signal_id,),
    )


async def _vip_call_stored(client, listener, time) -> None:
    """The VIP's call and three chatter posts, all stored and passed by
    the pointer (a window of four)."""
    assert await client.emit(V, _post(CALL))
    for message_id in VIP_CHATTER:
        assert await client.emit(V, _post(message_id, CHATTER_TEXT))
    await _round(listener, time)


VOIDED_BY_DELETION = ("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION, V, CALL, 1)


# -- F04: a withdrawn call posted again -------------------------------------------


def test_delete_then_repost_on_the_same_channel_is_a_new_signal(tmp_path):
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit_delete(V, CALL)
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=2)))
        await _round(listener, time)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, V, CALL, "VOIDED", 1), (2, V, REPOST, "NEW", 0)]
    assert state["queued"] == [(2, N)]
    assert state["alerts"] == [VOIDED_BY_DELETION]
    assert state["links"] == []


@pytest.mark.parametrize("how", ["unparseable", "chatter"])
def test_a_call_edited_away_then_posted_again_is_a_new_signal(tmp_path, how):
    """The original is INVALID (edited into something unparseable, which
    keeps its trade hash) or VOIDED (edited into chatter); the clean
    repost six minutes later trades."""
    db_path = tmp_path / "store.db"
    edit = UNPARSEABLE_EDIT if how == "unparseable" else EDIT_CHATTER

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit_edit(V, CALL, edit, edit_date=EDIT_1)
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=6)))
        await _round(listener, time)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [
        (1, V, CALL, "INVALID" if how == "unparseable" else "VOIDED", int(how == "chatter")),
        (2, V, REPOST, "NEW", 0),
    ]
    assert state["queued"] == [(2, N)]


def test_delete_then_repost_outside_the_window_is_a_new_signal(tmp_path):
    """Control: 16 minutes later, as before the fix."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit_delete(V, CALL)
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=16)))
        await _round(listener, time)

    _run(db_path, body)
    assert _state(db_path)["queued"] == [(2, N)]


def test_an_executed_call_deleted_and_posted_again_is_not_traded_twice(tmp_path):
    """Control: the original was executed before it was deleted. The
    repost is still the same call -- trading it would open the trade
    twice -- and no copy alert fires."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        _mark_executed(db_path)
        assert await client.emit_delete(V, CALL)
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=2)))
        await _round(listener, time)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, V, CALL, "NEW", 1)]
    assert state["queued"] == []
    assert state["alerts"] == [("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED, V, CALL, 1)]
    assert state["links"] == [(V, REPOST, 1)]


@pytest.mark.parametrize("found_by", ["event", "window"])
def test_repost_then_delete_promotes_the_standing_repost(tmp_path, found_by):
    """The poster posts the call again (a copy while the original stands),
    then deletes the original, which nobody had taken yet: the repost
    becomes the signal, in the same writer job as the deletion."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=2)))
        await _round(listener, time)
        assert _state(db_path)["links"] == [(V, REPOST, 1)]
        if found_by == "event":
            assert await client.emit_delete(V, CALL)
        else:
            with client.blackout():
                assert not await client.emit_delete(V, CALL)
            for _ in range(3):
                await _round(listener, time)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, V, CALL, "VOIDED", 1), (2, V, REPOST, "NEW", 0)]
    assert state["queued"] == [(2, N)]
    assert state["links"] == []  # the promoted repost is no copy any more
    assert ("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION) in _codes(state)
    assert ("WARNING", edits_repo.SIGNAL_REPOST_PROMOTED, V, REPOST, 2) in state["alerts"]
    if found_by == "window":
        assert ("CRITICAL", edits_repo.WINDOW_VOIDED_SIGNALS) in _codes(state)
    detail = _alert_detail(db_path, edits_repo.SIGNAL_REPOST_PROMOTED)
    assert f"message {REPOST}" in detail and f"message {CALL}" in detail


@pytest.mark.parametrize("edit", ["reaim", "unparseable", "chatter"])
def test_a_standing_repost_is_promoted_when_the_original_is_edited_away(tmp_path, edit):
    db_path = tmp_path / "store.db"
    text = {"reaim": DIRECTION_EDIT, "unparseable": UNPARSEABLE_EDIT, "chatter": EDIT_CHATTER}[edit]

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=2)))
        assert await client.emit_edit(V, CALL, text, edit_date=EDIT_1)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"][1] == (2, V, REPOST, "NEW", 0)
    assert state["queued"] == [(2, N)]
    assert ("WARNING", edits_repo.SIGNAL_REPOST_PROMOTED, V, REPOST, 2) in state["alerts"]


def test_an_executed_original_is_never_replaced_by_its_repost(tmp_path):
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=2)))
        _mark_executed(db_path)
        assert await client.emit_delete(V, CALL)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, V, CALL, "NEW", 1)]
    assert state["queued"] == []
    assert state["links"] == [(V, REPOST, 1)]


def test_the_earliest_standing_repost_is_promoted_and_the_rest_follow_it(tmp_path):
    """Posted three times: deleting the first promotes the second, and the
    third becomes a copy of the second -- so deleting the second in turn
    promotes the third."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=2)))
        assert await client.emit(V, _post(REPOST_2, after=timedelta(minutes=3)))
        assert await client.emit_delete(V, CALL)
        after_first = _state(db_path)
        assert await client.emit_delete(V, REPOST)
        return after_first

    after_first = _run(db_path, body)

    assert after_first["links"] == [(V, REPOST_2, 2)]
    state = _state(db_path)
    assert state["signals"] == [
        (1, V, CALL, "VOIDED", 1),
        (2, V, REPOST, "VOIDED", 1),
        (3, V, REPOST_2, "NEW", 0),
    ]
    assert state["queued"] == [(3, N)]
    assert state["links"] == []


def test_a_repost_stored_before_the_original_is_promoted_too(tmp_path):
    """The original's update was dropped, so the repost was stored first
    and holds the signal; the original, recovered by the poll, became its
    copy. Deleting the repost leaves the original standing: it is promoted
    (both are the poster's own posts of the same call)."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        with client.blackout():
            await client.emit(V, _post(CALL))
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=2)))
        await _round(listener, time)
        assert _state(db_path)["links"] == [(V, CALL, 1)]
        assert await client.emit_delete(V, REPOST)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, V, REPOST, "VOIDED", 1), (2, V, CALL, "NEW", 0)]
    assert state["queued"] == [(2, N)]


def test_a_deleted_repost_is_not_promoted(tmp_path):
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=2)))
        assert await client.emit_delete(V, REPOST)
        assert await client.emit_delete(V, CALL)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, V, CALL, "VOIDED", 1)]
    assert state["queued"] == []


def test_a_copy_on_another_channel_is_never_promoted(tmp_path):
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit(R, _post(COPY, after=timedelta(minutes=1)))
        assert await client.emit_delete(V, CALL)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, V, CALL, "VOIDED", 1)]
    assert state["queued"] == []
    assert state["links"] == [(R, COPY, 1)]


def test_a_withdrawn_call_copied_from_another_channel_is_held_back_loudly(tmp_path):
    """The VIP deleted the call before anyone took it; the relay's copy
    arrives a minute later. Not traded -- one source withdrew it -- but a
    CRITICAL alert names both messages."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit_delete(V, CALL)
        assert await client.emit(R, _post(COPY, after=timedelta(minutes=1)))

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, V, CALL, "VOIDED", 1)]
    assert state["queued"] == []
    assert state["links"] == [(R, COPY, 1)]
    assert state["alerts"] == [
        VOIDED_BY_DELETION,
        ("CRITICAL", relay_repo.COPY_OF_WITHDRAWN_SIGNAL, R, COPY, 1),
    ]
    detail = _alert_detail(db_path, relay_repo.COPY_OF_WITHDRAWN_SIGNAL)
    assert f"message {COPY} on channel {R}" in detail
    assert f"message {CALL} on channel {V}" in detail


def test_a_live_repost_is_preferred_to_the_withdrawn_original(tmp_path):
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit_delete(V, CALL)
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=2)))
        assert await client.emit(R, _post(COPY, after=timedelta(minutes=3)))

    _run(db_path, body)

    state = _state(db_path)
    assert state["links"] == [(R, COPY, 2)]
    assert _codes(state) == [("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION)]


def test_a_forward_of_a_withdrawn_call_is_held_back_loudly_however_late(conn):
    first = _ingest(conn, VIP, 4001)
    conn.execute(
        "UPDATE signals SET state = 'VOIDED', withdrawn_at_utc = ? WHERE signal_id = ?",
        (POSTED.isoformat(), first.signal_id),
    )
    conn.execute("UPDATE outbox SET state = 'SKIPPED'")
    forward = _ingest(
        conn, RELAY, 7001, posted=POSTED + timedelta(hours=2), fwd=(VIP.channel_id, 4001)
    )
    assert forward.outcome == "relay_duplicate"
    assert _alerts(conn) == [
        ("CRITICAL", relay_repo.COPY_OF_WITHDRAWN_SIGNAL, RELAY.channel_id, 7001, first.signal_id)
    ]
    assert _queued_entries(conn) == []


@pytest.mark.parametrize(
    ("state", "withdrawn", "executed", "outcome"),
    [
        ("VOIDED", True, False, "new"),
        ("INVALID", False, False, "new"),
        ("NEW", True, False, "new"),  # withdrawn, state kept NEW (was in flight, then released)
        ("NEW", True, True, "relay_duplicate"),  # withdrawn while live
        ("INVALID", False, True, "relay_duplicate"),  # re-aimed after it was executed
        ("NEW", False, False, "relay_duplicate"),  # a live call
    ],
)
def test_which_signals_a_repost_on_the_same_channel_repeats(
    conn, state, withdrawn, executed, outcome
):
    first = _ingest(conn, VIP, 4001)
    conn.execute(
        "UPDATE signals SET state = ?, withdrawn_at_utc = ? WHERE signal_id = ?",
        (state, POSTED.isoformat() if withdrawn else None, first.signal_id),
    )
    if executed:
        conn.execute("UPDATE outbox SET state = 'DONE' WHERE account_id = 'acct-1'")
    conn.commit()
    repost = _ingest(conn, VIP, 4012, posted=POSTED + timedelta(minutes=2))
    assert repost.outcome == outcome
    assert _alerts(conn) == []


def test_a_promotion_is_part_of_the_deletion_that_caused_it(conn, monkeypatch):
    """One writer job: a crash while the repost's entries are fanned out
    rolls the deletion back too, so it is retried whole -- never a VOIDED
    original with an unpromoted repost."""
    holder = _ingest(conn, VIP, 4001)
    _ingest(conn, VIP, 4012, posted=POSTED + timedelta(minutes=2))

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash while the repost is fanned out")

    monkeypatch.setattr(outbox, "fan_out", boom)
    with pytest.raises(RuntimeError, match="simulated crash"):
        edits_repo.apply_deletion(
            conn, channel_cfg=VIP, message_id=4001, now_utc=POSTED, accounts=ACCOUNTS,
            detected_by="event",
        )
    conn.rollback()  # as the Writer does for a job that raised
    assert _signals(conn) == [(holder.signal_id, VIP.channel_id, 4001, "NEW")]
    assert _q(conn, "SELECT deleted_at_utc FROM messages WHERE message_id = 4001") == [(None,)]

    monkeypatch.undo()
    result = edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4001, now_utc=POSTED, accounts=ACCOUNTS,
        detected_by="event",
    )
    assert (result.outcome, result.promoted_signal_id) == ("voided", 2)
    assert _signals(conn)[1] == (2, VIP.channel_id, 4012, "NEW")


def test_an_executed_original_edited_away_leaves_its_repost_a_copy(tmp_path):
    """Invalidation promotes only while nothing was executed."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=2)))
        _mark_executed(db_path)
        assert await client.emit_edit(V, CALL, DIRECTION_EDIT, edit_date=EDIT_1)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, V, CALL, "INVALID", 0)]
    assert state["links"] == [(V, REPOST, 1)]
    assert _codes(state) == [("CRITICAL", edits_repo.SIGNAL_INVALIDATED_BY_EDIT)]


def test_a_repost_that_no_longer_reads_as_a_signal_is_passed_over(conn, caplog):
    """A stored repost whose text no longer parses (the channel's parser
    changed since) is not promoted; the next standing one is."""
    holder = _ingest(conn, VIP, 4001)
    _ingest(conn, VIP, 4012, posted=POSTED + timedelta(minutes=2))
    _ingest(conn, VIP, 4013, posted=POSTED + timedelta(minutes=3))
    conn.execute("UPDATE messages SET raw_text = ? WHERE message_id = 4012", (CHATTER_TEXT,))
    with caplog.at_level(logging.WARNING, logger="xaucopier.store.edits_repo"):
        result = edits_repo.apply_deletion(
            conn, channel_cfg=VIP, message_id=4001, now_utc=POSTED, accounts=ACCOUNTS,
            detected_by="event",
        )
    assert result.promoted_signal_id == holder.signal_id + 1
    assert _signals(conn)[1] == (holder.signal_id + 1, VIP.channel_id, 4013, "NEW")
    assert any("4012" in r.getMessage() and "not promoted" in r.getMessage()
               for r in caplog.records)


# -- F02: an edit or a deletion of a copy reaches the signal it repeats ----------
#
# The VIP's post (V/4001) and the relay's copy of it (R/7001, 2 s later).
# Whichever was stored first holds the signal; the other is its copy. In
# the finding's scenarios the VIP's post is the copy: its update was
# dropped, the relay's arrived, and the poll recovered the VIP's later.

RELAY_POST = _post(COPY, after=timedelta(seconds=2))


def _claim(db_path: Path, account_id: str = "acct-2"):
    conn = db.connect(db_path, role="writer")
    try:
        row = outbox.claim(conn, account_id=account_id, pid=4242, now_utc=NOW)
        conn.commit()
        return None if row is None else (row.kind, row.signal_id)
    finally:
        conn.close()


async def _relay_holds(client, listener, time, db_path, *, how: str = "blackout") -> None:
    """The relay's copy holds signal 1; the VIP's post is its copy."""
    if how == "blackout":  # F1a: the VIP's update dropped, the relay's delivered
        with client.blackout():
            await client.emit(V, _post(CALL))
        assert await client.emit(R, RELAY_POST)
    elif how == "relay-first":  # F1e: both delivered, the relay's first
        assert await client.emit(R, RELAY_POST)
        assert await client.emit(V, _post(CALL))
    elif how == "both-dropped":  # F1h: both dropped; the poll reads them in config order
        with client.blackout():
            await client.emit(V, _post(CALL))
            await client.emit(R, RELAY_POST)
    elif how != "posted-while-down":  # F1g: history before start, backfilled
        raise AssertionError(how)
    for _ in range(2):
        await _round(listener, time)
    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "NEW", 0)], state
    assert state["links"] == [(V, CALL, 1)]


def _posted_while_down() -> FakeTelegramClient:
    client = FakeTelegramClient()
    client.add_to_history(V, _post(CALL))
    client.add_to_history(R, RELAY_POST)
    return client


@pytest.mark.parametrize(
    ("how", "channels"),
    [
        ("blackout", (VIP, RELAY)),  # F1a
        ("relay-first", (VIP, RELAY)),  # F1e
        ("posted-while-down", (RELAY, VIP)),  # F1g
        ("both-dropped", (RELAY, VIP)),  # F1h
    ],
)
def test_a_copy_re_aimed_invalidates_the_signal_it_repeats(tmp_path, how, channels):
    db_path = tmp_path / "store.db"
    client = _posted_while_down() if how == "posted-while-down" else None

    async def body(client, listener, time):
        await _relay_holds(client, listener, time, db_path, how=how)
        assert await client.emit_edit(V, CALL, DIRECTION_EDIT, edit_date=EDIT_1)

    _run(db_path, body, channels=channels, client=client)

    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "INVALID", 0)]
    assert state["queued"] == []
    assert state["alerts"] == [("CRITICAL", edits_repo.SIGNAL_INVALIDATED_BY_EDIT, V, CALL, 1)]
    detail = _alert_detail(db_path, edits_repo.SIGNAL_INVALIDATED_BY_EDIT)
    assert f"message {CALL} on channel {V}" in detail  # the copy
    assert f"message {COPY} on channel {R}" in detail  # the signal's own message
    assert _claim(db_path) is None


def test_a_copy_deleted_voids_the_unexecuted_signal_it_repeats(tmp_path):
    """F1b: another channel's copy deleted -- one source withdrew the call."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _relay_holds(client, listener, time, db_path)
        assert await client.emit_delete(V, CALL)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "VOIDED", 1)]
    assert state["queued"] == []
    assert state["alerts"] == [("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION, V, CALL, 1)]
    detail = _alert_detail(db_path, edits_repo.SIGNAL_VOIDED_BY_DELETION)
    assert f"message {COPY} on channel {R}" in detail
    assert _claim(db_path) is None


def test_a_copy_deleted_flags_the_executed_signal_it_repeats(tmp_path):
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _relay_holds(client, listener, time, db_path)
        _mark_executed(db_path)
        assert await client.emit_delete(V, CALL)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "NEW", 1)]
    assert state["alerts"] == [("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED, V, CALL, 1)]
    assert _claim(db_path) is None  # acct-2's entry is skipped, and the signal withdrawn


def test_a_copys_sl_edit_amends_the_executed_signal_it_repeats(tmp_path):
    """F1c."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _relay_holds(client, listener, time, db_path)
        _mark_executed(db_path)
        assert await client.emit_edit(V, CALL, SL_EDIT, edit_date=EDIT_1)

    _run(db_path, body)

    (version, sl, posted) = _db_rows(
        db_path,
        "SELECT version, json_extract(parsed_json, '$.sl'), "
        "json_extract(parsed_json, '$.posted_at_utc') FROM signals",
    )[0]
    assert (version, sl) == (2, 4460.0)
    # Still the signal's own post date, not the copy's.
    assert posted == (POSTED + timedelta(seconds=2)).isoformat()
    amendments = _db_rows(
        db_path,
        "SELECT channel_id, message_id, op, target_signal_id, resolution_path FROM followups",
    )
    assert amendments == [(R, COPY, "amend_sl", 1, "edit")]
    assert _db_rows(db_path, "SELECT COUNT(*) FROM outbox WHERE kind = 'FOLLOWUP'") == [(N,)]


def test_a_copy_re_aimed_after_execution_is_loud_and_skips_the_rest(tmp_path):
    """F1d: the live BUY is not re-aimed; acct-2's queued BUY is skipped."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _relay_holds(client, listener, time, db_path)
        _mark_executed(db_path)
        assert await client.emit_edit(V, CALL, DIRECTION_EDIT, edit_date=EDIT_1)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "INVALID", 0)]
    assert state["alerts"] == [("CRITICAL", edits_repo.SIGNAL_INVALIDATED_BY_EDIT, V, CALL, 1)]
    assert "NOT closed or re-aimed" in _alert_detail(db_path, edits_repo.SIGNAL_INVALIDATED_BY_EDIT)
    assert _claim(db_path) is None


@pytest.mark.parametrize("found_by", ["window", "forward-read"])
def test_a_copys_edit_the_update_stream_dropped_reaches_the_signal(tmp_path, found_by):
    """Every arrival path: the window re-read (the copy is at or below the
    pointer) and the poll's forward read (the copy was stored live, above
    it)."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        if found_by == "window":
            await _relay_holds(client, listener, time, db_path)
        else:
            assert await client.emit(R, RELAY_POST)
            assert await client.emit(V, _post(CALL))  # stored live: a copy, pointer below it
        with client.blackout():
            assert not await client.emit_edit(V, CALL, DIRECTION_EDIT, edit_date=EDIT_1)
        await _round(listener, time)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "INVALID", 0)]
    assert state["alerts"] == [("CRITICAL", edits_repo.SIGNAL_INVALIDATED_BY_EDIT, V, CALL, 1)]


def test_a_copys_deletion_the_update_stream_dropped_reaches_the_signal(tmp_path):
    """The window re-read infers it (two reads); it voids the signal the
    copy repeats and the window's summary names that signal."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        with client.blackout():
            await client.emit(V, _post(CALL))
            for message_id in VIP_CHATTER:
                await client.emit(V, _post(message_id, CHATTER_TEXT))
        assert await client.emit(R, RELAY_POST)
        await _round(listener, time)
        assert _state(db_path)["links"] == [(V, CALL, 1)]
        with client.blackout():
            assert not await client.emit_delete(V, CALL)
        for _ in range(3):
            await _round(listener, time)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "VOIDED", 1)]
    assert ("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION, V, CALL, 1) in state["alerts"]
    assert ("CRITICAL", edits_repo.WINDOW_VOIDED_SIGNALS) in _codes(state)
    assert "[1]" in _alert_detail(db_path, edits_repo.WINDOW_VOIDED_SIGNALS)


# -- F02: mirrored edits and deletions are applied once ------------------------------


@pytest.mark.parametrize("first", ["copy", "holder"])
def test_a_re_aim_mirrored_on_both_posts_is_one_alert(tmp_path, first):
    """F1f and its reverse: the relay mirrors the edit. Whichever arrives
    first invalidates the signal; the other is a no-op."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _relay_holds(client, listener, time, db_path)
        order = [(V, CALL), (R, COPY)] if first == "copy" else [(R, COPY), (V, CALL)]
        for index, (channel_id, message_id) in enumerate(order):
            relay_text = DIRECTION_EDIT + ("\nvia VIP" if channel_id == R else "")
            assert await client.emit_edit(
                channel_id, message_id, relay_text, edit_date=EDIT_1 + timedelta(seconds=index)
            )

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "INVALID", 0)]
    assert _codes(state) == [("CRITICAL", edits_repo.SIGNAL_INVALIDATED_BY_EDIT)]


@pytest.mark.parametrize("executed", [False, True], ids=["unexecuted", "executed"])
@pytest.mark.parametrize("first", ["copy", "holder"])
def test_a_deletion_mirrored_on_both_posts_is_one_alert(tmp_path, first, executed):
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _relay_holds(client, listener, time, db_path)
        if executed:
            _mark_executed(db_path)
        order = [(V, CALL), (R, COPY)] if first == "copy" else [(R, COPY), (V, CALL)]
        for channel_id, message_id in order:
            assert await client.emit_delete(channel_id, message_id)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "NEW" if executed else "VOIDED", 1)]
    assert _codes(state) == [
        ("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED)
        if executed
        else ("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION)
    ]


@pytest.mark.parametrize("first", ["copy", "holder"])
def test_an_sl_edit_mirrored_on_both_posts_is_amended_once(tmp_path, first):
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _relay_holds(client, listener, time, db_path)
        _mark_executed(db_path)
        order = [(V, CALL), (R, COPY)] if first == "copy" else [(R, COPY), (V, CALL)]
        for index, (channel_id, message_id) in enumerate(order):
            assert await client.emit_edit(
                channel_id, message_id, SL_EDIT, edit_date=EDIT_1 + timedelta(seconds=index)
            )

    _run(db_path, body)

    assert _db_rows(db_path, "SELECT op FROM followups") == [("amend_sl",)]
    assert _db_rows(db_path, "SELECT version FROM signals") == [(2,)]
    assert _state(db_path)["alerts"] == []


def test_a_copy_edited_back_after_its_signal_was_closed_is_a_warning(tmp_path):
    """Not a mirror: the copy now reads as a live call again, while the
    signal stays INVALID -- said once, as for the signal's own message."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _relay_holds(client, listener, time, db_path)
        assert await client.emit_edit(V, CALL, DIRECTION_EDIT, edit_date=EDIT_1)
        assert await client.emit_edit(V, CALL, SIGNAL_TEXT, edit_date=EDIT_2)

    _run(db_path, body)

    assert _codes(_state(db_path)) == [
        ("CRITICAL", edits_repo.SIGNAL_INVALIDATED_BY_EDIT),
        ("WARNING", edits_repo.EDIT_OF_CLOSED_SIGNAL),
    ]


# -- F02: a copy on the same channel (a double post) ---------------------------------


@pytest.mark.parametrize("change", ["deleted", "edited-away"])
def test_a_double_post_tidied_up_only_warns(tmp_path, change):
    """The poster posts the call twice and removes the second: the call
    stands, still queued; a WARNING says so."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit(V, _post(REPOST, after=timedelta(seconds=30)))
        if change == "deleted":
            assert await client.emit_delete(V, REPOST)
        else:
            assert await client.emit_edit(V, REPOST, ".", edit_date=EDIT_1)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, V, CALL, "NEW", 0)]
    assert state["queued"] == [(1, N)]
    assert state["alerts"] == [("WARNING", edits_repo.SIGNAL_REPEAT_REMOVED, V, REPOST, 1)]
    detail = _alert_detail(db_path, edits_repo.SIGNAL_REPEAT_REMOVED)
    assert f"message {CALL}" in detail


def test_a_double_post_re_aimed_invalidates_the_signal(tmp_path):
    """A re-aim is not a tidy-up: whichever post says it, the call is in
    doubt."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit(V, _post(REPOST, after=timedelta(seconds=30)))
        assert await client.emit_edit(V, REPOST, DIRECTION_EDIT, edit_date=EDIT_1)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, V, CALL, "INVALID", 0)]
    assert state["alerts"] == [("CRITICAL", edits_repo.SIGNAL_INVALIDATED_BY_EDIT, V, REPOST, 1)]


def test_a_copy_edited_into_chatter_on_another_channel_withdraws_the_signal(tmp_path):
    """As its deletion would: another source withdrew the call."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _relay_holds(client, listener, time, db_path)
        assert await client.emit_edit(V, CALL, EDIT_CHATTER, edit_date=EDIT_1)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "VOIDED", 1)]
    assert state["alerts"] == [("WARNING", edits_repo.SIGNAL_RETRACTED_BY_EDIT, V, CALL, 1)]


def test_a_copys_withdrawal_never_promotes_a_repost(tmp_path):
    """The VIP double-posted; the relay deleted its copy. One source
    withdrew the call: the VIP's repost is not promoted over that."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit(V, _post(REPOST, after=timedelta(seconds=30)))
        assert await client.emit(R, _post(COPY, after=timedelta(seconds=40)))
        assert await client.emit_delete(R, COPY)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, V, CALL, "VOIDED", 1)]
    assert state["queued"] == []
    assert state["links"] == [(R, COPY, 1), (V, REPOST, 1)]


def test_a_repeat_edited_back_into_the_call_says_it_is_a_copy(tmp_path):
    """Edits never open trades -- and the warning must not invite the user
    to trade by hand a call that is already queued."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit(V, _post(REPOST, after=timedelta(seconds=30)))
        assert await client.emit_edit(V, REPOST, ".", edit_date=EDIT_1)
        assert await client.emit_edit(V, REPOST, SIGNAL_TEXT, edit_date=EDIT_2)

    _run(db_path, body)

    state = _state(db_path)
    assert state["queued"] == [(1, N)]
    assert _codes(state) == [
        ("WARNING", edits_repo.SIGNAL_REPEAT_REMOVED),
        ("WARNING", edits_repo.EDIT_CREATED_SIGNAL),
    ]
    detail = _alert_detail(db_path, edits_repo.EDIT_CREATED_SIGNAL)
    assert "copy of signal 1" in detail and "by hand" not in detail


def test_a_copys_window_deletion_reversed_names_the_signal_it_voided(tmp_path):
    db_path = tmp_path / "store.db"
    client = ScriptedClient()

    async def body(client, listener, time):
        with client.blackout():
            await client.emit(V, _post(CALL))
            for message_id in VIP_CHATTER:
                await client.emit(V, _post(message_id, CHATTER_TEXT))
        assert await client.emit(R, RELAY_POST)
        await _round(listener, time)
        # Each round reads the VIP's window, then the relay's: two misses
        # of the VIP's post in a row, then it is back.
        client.ids_answers = [_without(CALL)] * 4
        for _ in range(3):
            await _round(listener, time)

    _run(db_path, body, client=client)

    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "VOIDED", 1)]
    assert ("CRITICAL", edits_repo.DELETION_REVERSED) in _codes(state)
    detail = _alert_detail(db_path, edits_repo.DELETION_REVERSED)
    assert "signal 1 (this message is a copy of it) is VOIDED" in detail


@pytest.mark.parametrize("executed", [False, True], ids=["unexecuted", "executed"])
@pytest.mark.parametrize("first", ["copy", "holder"])
def test_a_retraction_mirrored_on_both_posts_is_one_alert(tmp_path, first, executed):
    """Both posts edited into chatter: the call is withdrawn once."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _relay_holds(client, listener, time, db_path)
        if executed:
            _mark_executed(db_path)
        order = [(V, CALL), (R, COPY)] if first == "copy" else [(R, COPY), (V, CALL)]
        for index, (channel_id, message_id) in enumerate(order):
            assert await client.emit_edit(
                channel_id, message_id, f"{EDIT_CHATTER} {index}",
                edit_date=EDIT_1 + timedelta(seconds=index),
            )

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "NEW" if executed else "VOIDED", 1)]
    assert _codes(state) == [
        ("CRITICAL", edits_repo.LIVE_SIGNAL_RETRACTED_BY_EDIT)
        if executed
        else ("WARNING", edits_repo.SIGNAL_RETRACTED_BY_EDIT)
    ]


def test_a_copy_re_aimed_then_deleted_is_one_alert(tmp_path):
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _relay_holds(client, listener, time, db_path)
        assert await client.emit_edit(V, CALL, DIRECTION_EDIT, edit_date=EDIT_1)
        assert await client.emit_delete(V, CALL)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "INVALID", 0)]
    assert _codes(state) == [("CRITICAL", edits_repo.SIGNAL_INVALIDATED_BY_EDIT)]


def test_a_repeat_edited_away_then_deleted_is_one_warning(tmp_path):
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit(V, _post(REPOST, after=timedelta(seconds=30)))
        assert await client.emit_edit(V, REPOST, ".", edit_date=EDIT_1)
        assert await client.emit_delete(V, REPOST)

    _run(db_path, body)

    state = _state(db_path)
    assert state["queued"] == [(1, N)]
    assert _codes(state) == [("WARNING", edits_repo.SIGNAL_REPEAT_REMOVED)]


def test_a_call_nobody_copied_keeps_its_own_policy_for_a_second_closing_edit(tmp_path):
    """The mirror rule is for relayed calls only: a signal re-aimed, then
    edited into something unparseable, still warns the second time."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit_edit(V, CALL, DIRECTION_EDIT, edit_date=EDIT_1)
        assert await client.emit_edit(V, CALL, UNPARSEABLE_EDIT, edit_date=EDIT_2)

    _run(db_path, body)

    assert _codes(_state(db_path)) == [
        ("CRITICAL", edits_repo.SIGNAL_INVALIDATED_BY_EDIT),
        ("WARNING", edits_repo.EDIT_OF_CLOSED_SIGNAL),
    ]


def test_a_call_nobody_copied_keeps_its_own_policy_for_a_deletion_after_a_retraction(tmp_path):
    """The mirror rule for deletions is for relayed calls only: an executed
    signal edited into chatter and then deleted says both, as before."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        _mark_executed(db_path)
        assert await client.emit_edit(V, CALL, EDIT_CHATTER, edit_date=EDIT_1)
        assert await client.emit_delete(V, CALL)

    _run(db_path, body)

    assert _codes(_state(db_path)) == [
        ("CRITICAL", edits_repo.LIVE_SIGNAL_RETRACTED_BY_EDIT),
        ("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED),
    ]


# -- F08: follow-ups ---------------------------------------------------------------

CLOSE_HALF = "Close half now"
SL_TO_BE = "Move SL to BE"


def _followups(conn) -> list[tuple]:
    return _q(
        conn,
        "SELECT followup_id, channel_id, message_id, op, reply_to_message_id, "
        "target_signal_id, resolution_path FROM followups ORDER BY followup_id",
    )


def _followup_link(conn, channel: ChannelCfg, message_id: int) -> int | None:
    (row,) = _q(
        conn,
        "SELECT relay_of_followup_id FROM messages WHERE channel_id = ? AND message_id = ?",
        channel.channel_id,
        message_id,
    )
    return row[0]


def _claims(conn) -> list[tuple]:
    claimed = []
    while (row := outbox.claim(
        conn, account_id="acct-1", pid=1, now_utc=POSTED + timedelta(hours=1)
    )) is not None:
        claimed.append((row.kind, row.signal_id, row.followup_id))
    return claimed


@pytest.mark.parametrize("text", [CLOSE_HALF, SL_TO_BE])
def test_a_follow_up_repeated_on_the_relay_is_dispatched_once(conn, text):
    """F2a: the VIP's call and the relay's copy 60 s later; the VIP's
    follow-up replies to its call, the relay's to its copy. One signal,
    one follow-up targeting it, the copy stored and linked."""
    holder = _ingest(conn, VIP, 4001)
    _ingest(conn, RELAY, 5001, posted=POSTED + timedelta(seconds=60))
    first = _ingest(conn, VIP, 4002, text, posted=POSTED + timedelta(minutes=5), reply_to=4001)
    copy = _ingest(
        conn, RELAY, 5002, text, posted=POSTED + timedelta(minutes=5, seconds=40), reply_to=5001
    )

    assert (first.outcome, first.kind) == ("new", "followup")
    assert (copy.outcome, copy.kind, copy.followup_id) == ("relay_duplicate", "followup", None)
    assert copy.relay_of_followup_id == first.followup_id
    op = "close_half" if text == CLOSE_HALF else "sl_to_be"
    assert _followups(conn) == [
        (first.followup_id, VIP.channel_id, 4002, op, 4001, holder.signal_id, "reply")
    ]
    assert _followup_link(conn, RELAY, 5002) == first.followup_id
    assert _claims(conn) == [
        ("ENTRY", holder.signal_id, None),
        ("FOLLOWUP", None, first.followup_id),
    ]


def test_a_follow_up_replying_to_a_copy_targets_the_signal_the_copy_repeats(conn):
    """F2b: the relay's copy was stored first and holds the signal; the
    VIP's call is its copy, and the VIP's follow-up replies to it."""
    holder = _ingest(conn, RELAY, 5001, posted=POSTED + timedelta(seconds=60))
    _ingest(conn, VIP, 4001, source="poll")
    followup = _ingest(
        conn, VIP, 4002, CLOSE_HALF, posted=POSTED + timedelta(minutes=5), reply_to=4001
    )
    assert followup.outcome == "new"
    assert _followups(conn) == [
        (followup.followup_id, VIP.channel_id, 4002, "close_half", 4001, holder.signal_id,
         "relay-reply")
    ]


def test_a_follow_up_replying_to_nothing_stored_has_no_target(conn):
    followup = _ingest(conn, VIP, 4002, CLOSE_HALF, reply_to=3999)
    assert _followups(conn)[0][5:] == (None, None)
    assert followup.outcome == "new"


def test_follow_ups_replying_to_nothing_are_never_matched_by_their_text(conn):
    """Two channels each saying "close half": two instructions."""
    _ingest(conn, VIP, 4001)
    _ingest(conn, RELAY, 5001, posted=POSTED + timedelta(seconds=60))
    first = _ingest(conn, VIP, 4002, CLOSE_HALF, posted=POSTED + timedelta(minutes=5))
    second = _ingest(conn, RELAY, 5002, CLOSE_HALF, posted=POSTED + timedelta(minutes=5))
    assert (first.outcome, second.outcome) == ("new", "new")
    assert len(_followups(conn)) == 2


def test_a_forwarded_follow_up_is_a_copy_however_late(conn):
    """A forward keeps no reply link, so it is matched by the post."""
    _ingest(conn, VIP, 4001)
    first = _ingest(conn, VIP, 4002, CLOSE_HALF, posted=POSTED + timedelta(minutes=5), reply_to=4001)
    copy = _ingest(
        conn, RELAY, 5002, CLOSE_HALF, posted=POSTED + timedelta(hours=2),
        fwd=(VIP.channel_id, 4002),
    )
    assert copy.outcome == "relay_duplicate"
    assert copy.relay_of_followup_id == first.followup_id


@pytest.mark.parametrize(
    ("text", "gap", "outcome"),
    [
        (CLOSE_HALF, 900, "relay_duplicate"),
        (CLOSE_HALF, 901, "new"),  # outside the window
        (SL_TO_BE, 60, "new"),  # another instruction
        ("close half now", 60, "new"),  # the same instruction in other words
    ],
)
def test_which_follow_ups_on_the_same_signal_are_copies(conn, text, gap, outcome):
    _ingest(conn, VIP, 4001)
    _ingest(conn, RELAY, 5001, posted=POSTED + timedelta(seconds=60))
    at = POSTED + timedelta(minutes=5)
    _ingest(conn, VIP, 4002, CLOSE_HALF, posted=at, reply_to=4001)
    copy = _ingest(conn, RELAY, 5002, text, posted=at + timedelta(seconds=gap), reply_to=5001)
    assert copy.outcome == outcome


def test_a_follow_up_withdrawn_and_said_again_on_its_channel_is_new(conn):
    _ingest(conn, VIP, 4001)
    at = POSTED + timedelta(minutes=5)
    _ingest(conn, VIP, 4002, CLOSE_HALF, posted=at, reply_to=4001)
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4002, now_utc=at, accounts=ACCOUNTS,
        detected_by="event",
    )
    again = _ingest(conn, VIP, 4003, CLOSE_HALF, posted=at + timedelta(minutes=1), reply_to=4001)
    assert again.outcome == "new"


def test_a_follow_up_copy_is_logged_with_both_messages_and_counted(conn, caplog):
    _ingest(conn, VIP, 4001)
    _ingest(conn, RELAY, 5001, posted=POSTED + timedelta(seconds=60))
    at = POSTED + timedelta(minutes=5)
    _ingest(conn, VIP, 4002, CLOSE_HALF, posted=at, reply_to=4001)
    with caplog.at_level(logging.INFO, logger="xaucopier.store.relay_repo"):
        _ingest(conn, RELAY, 5002, CLOSE_HALF, posted=at, reply_to=5001)
    assert _metric(conn, relay_repo.RELAY_DUPLICATE_FOLLOWUPS_METRIC) == 1
    assert _metric(conn, relay_repo.RELAY_DUPLICATES_METRIC) == 1  # the signal's copy
    (line,) = [r.getMessage() for r in caplog.records]
    assert f"channel {RELAY.channel_id} message 5002" in line
    assert f"channel {VIP.channel_id} message 4002" in line


def test_a_promoted_repost_takes_over_the_follow_ups_that_replied_to_it(conn):
    holder = _ingest(conn, VIP, 4001)
    _ingest(conn, VIP, 4012, posted=POSTED + timedelta(minutes=2))
    _ingest(conn, VIP, 4013, CLOSE_HALF, posted=POSTED + timedelta(minutes=3), reply_to=4012)
    _ingest(conn, VIP, 4014, SL_TO_BE, posted=POSTED + timedelta(minutes=4), reply_to=4001)
    assert [row[5:] for row in _followups(conn)] == [
        (holder.signal_id, "relay-reply"),
        (holder.signal_id, "reply"),
    ]
    result = edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4001, now_utc=POSTED, accounts=ACCOUNTS,
        detected_by="event",
    )
    assert [row[5:] for row in _followups(conn)] == [
        (result.promoted_signal_id, "reply"),
        (holder.signal_id, "reply"),  # it replied to the deleted post
    ]


def test_a_relayed_follow_up_is_dispatched_once_through_the_listener(tmp_path):
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        assert await client.emit(V, _post(CALL))
        assert await client.emit(R, _post(COPY, after=timedelta(seconds=60)))
        assert await client.emit(V, _post(CALL + 1, CLOSE_HALF, after=timedelta(minutes=5),
                                          reply_to=CALL))
        with client.blackout():  # the relay's copy is found by the poll
            await client.emit(R, _post(COPY + 1, CLOSE_HALF, after=timedelta(minutes=5),
                                       reply_to=COPY))
        await _round(listener, time)

    _run(db_path, body)

    assert _db_rows(db_path, "SELECT channel_id, message_id, target_signal_id FROM followups") == [
        (V, CALL + 1, 1)
    ]
    assert _db_rows(db_path, "SELECT COUNT(*) FROM outbox WHERE kind = 'FOLLOWUP'") == [(N,)]
    assert _db_rows(
        db_path,
        "SELECT relay_of_followup_id FROM messages WHERE channel_id = ? AND message_id = ?",
        (R, COPY + 1),
    ) == [(1,)]


# -- F08: a follow-up copy edited or deleted -------------------------------------------


@pytest.mark.parametrize("change", ["edited", "deleted"])
def test_a_follow_up_copy_changed_is_a_warning_naming_both(conn, change):
    """The follow-up acted on is the other post's; a change of its copy is
    never applied silently, and never applied by guess either."""
    _ingest(conn, VIP, 4001)
    _ingest(conn, RELAY, 5001, posted=POSTED + timedelta(seconds=60))
    at = POSTED + timedelta(minutes=5)
    first = _ingest(conn, VIP, 4002, CLOSE_HALF, posted=at, reply_to=4001)
    _ingest(conn, RELAY, 5002, CLOSE_HALF, posted=at, reply_to=5001)
    if change == "edited":
        edits_repo.apply_edit(
            conn, channel_cfg=RELAY, message_id=5002, text="Close all now", posted_at_utc=at,
            edit_date_utc=at + timedelta(minutes=1), reply_to_message_id=5001, now_utc=at,
            accounts=ACCOUNTS, detected_by="event",
        )
    else:
        edits_repo.apply_deletion(
            conn, channel_cfg=RELAY, message_id=5002, now_utc=at, accounts=ACCOUNTS,
            detected_by="event",
        )
    assert _alerts(conn) == [
        ("WARNING", edits_repo.FOLLOWUP_COPY_CHANGED, RELAY.channel_id, 5002, None)
    ]
    (detail,) = _q(conn, "SELECT detail FROM alerts")[0]
    assert f"follow-up {first.followup_id}" in detail and "message 4002" in detail
    assert _followups(conn)[0][3] == "close_half"  # unchanged
    queued = _q(conn, "SELECT COUNT(*) FROM outbox WHERE kind = 'FOLLOWUP' AND state = 'QUEUED'")
    assert queued == [(len(ACCOUNTS),)]


@pytest.mark.parametrize("change", ["edited", "deleted"])
def test_a_follow_up_change_mirrored_on_its_copy_is_said_once(conn, change):
    _ingest(conn, VIP, 4001)
    _ingest(conn, RELAY, 5001, posted=POSTED + timedelta(seconds=60))
    at = POSTED + timedelta(minutes=5)
    _ingest(conn, VIP, 4002, CLOSE_HALF, posted=at, reply_to=4001)
    _ingest(conn, RELAY, 5002, CLOSE_HALF, posted=at, reply_to=5001)
    for channel, message_id, reply_to, index in ((VIP, 4002, 4001, 0), (RELAY, 5002, 5001, 1)):
        if change == "edited":
            edits_repo.apply_edit(
                conn, channel_cfg=channel, message_id=message_id, text="Close all now",
                posted_at_utc=at, edit_date_utc=at + timedelta(minutes=1, seconds=index),
                reply_to_message_id=reply_to, now_utc=at, accounts=ACCOUNTS,
                detected_by="event",
            )
        else:
            edits_repo.apply_deletion(
                conn, channel_cfg=channel, message_id=message_id, now_utc=at,
                accounts=ACCOUNTS, detected_by="event",
            )
    codes = [code for _, code, *_ in _alerts(conn)]
    assert edits_repo.FOLLOWUP_COPY_CHANGED not in codes
    assert len(codes) == (0 if change == "edited" else 1)  # edited: updated in place, silently


def test_a_forward_of_a_follow_up_copy_repeats_the_follow_up_it_copied(conn):
    _ingest(conn, VIP, 4001)
    _ingest(conn, RELAY, 5001, posted=POSTED + timedelta(seconds=60))
    at = POSTED + timedelta(minutes=5)
    first = _ingest(conn, VIP, 4002, CLOSE_HALF, posted=at, reply_to=4001)
    _ingest(conn, RELAY, 5002, CLOSE_HALF, posted=at, reply_to=5001)
    forward = _ingest(
        conn, OTHER, 8002, CLOSE_HALF, posted=at + timedelta(hours=2),
        fwd=(RELAY.channel_id, 5002),
    )
    assert forward.outcome == "relay_duplicate"
    assert forward.relay_of_followup_id == first.followup_id


def test_a_follow_ups_target_and_its_path_go_together(conn):
    followup = FollowUp(
        channel_id=VIP.channel_id, message_id=1, op=FollowUpOp.CLOSE_ALL, posted_at_utc=POSTED
    )
    for target, path in ((1, None), (None, "reply")):
        with pytest.raises(ValueError, match="go together"):
            signals_repo.insert_followup(
                conn, channel_id=VIP.channel_id, message_id=1, followup=followup,
                target_signal_id=target, resolution_path=path,
            )


def test_a_repeat_deleted_after_its_withdrawn_signal_says_nothing_more(tmp_path):
    """The poster deleted both posts of an executed call: the first
    deletion flags it; the second has nothing to add."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit(V, _post(REPOST, after=timedelta(seconds=30)))
        _mark_executed(db_path)
        assert await client.emit_delete(V, CALL)
        assert await client.emit_delete(V, REPOST)

    _run(db_path, body)

    assert _codes(_state(db_path)) == [("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED)]


# -- F04 when the relay's copy holds the call ---------------------------------------
#
# The relay's copy was stored first (delivered first, or the VIP's update
# was dropped), so it holds signal 1 and the VIP's own post is its copy.
# The VIP deleting or breaking its post and posting the call again must be
# traded exactly as when the VIP's post holds the signal: what counts is
# the channel whose post withdrew the call, not which post was stored
# first.


async def _relay_holds_with_chatter(client, listener, time, db_path, *, how: str) -> None:
    """`_relay_holds`, with the VIP's chatter stored too, so the window
    re-read (four ids) covers the VIP's post."""
    if how == "relay-first":
        assert await client.emit(R, RELAY_POST)
        assert await client.emit(V, _post(CALL))
        for message_id in VIP_CHATTER:
            assert await client.emit(V, _post(message_id, CHATTER_TEXT))
    else:
        assert how == "blackout"
        with client.blackout():
            await client.emit(V, _post(CALL))
            for message_id in VIP_CHATTER:
                await client.emit(V, _post(message_id, CHATTER_TEXT))
        assert await client.emit(R, RELAY_POST)
    for _ in range(2):
        await _round(listener, time)
    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "NEW", 0)], state
    assert state["links"] == [(V, CALL, 1)]


@pytest.mark.parametrize("found_by", ["event", "window"])
@pytest.mark.parametrize("how", ["relay-first", "blackout"])
def test_repost_then_delete_promotes_the_repost_when_the_relays_copy_holds_the_call(
    tmp_path, how, found_by
):
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _relay_holds_with_chatter(client, listener, time, db_path, how=how)
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=2)))
        await _round(listener, time)
        assert _state(db_path)["links"] == [(V, CALL, 1), (V, REPOST, 1)]
        if found_by == "event":
            assert await client.emit_delete(V, CALL)
        else:
            with client.blackout():
                assert not await client.emit_delete(V, CALL)
            for _ in range(3):
                await _round(listener, time)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "VOIDED", 1), (2, V, REPOST, "NEW", 0)]
    assert state["queued"] == [(2, N)]
    assert state["links"] == [(V, CALL, 1)]  # the deleted post still names what it voided
    assert ("WARNING", edits_repo.SIGNAL_REPOST_PROMOTED, V, REPOST, 2) in state["alerts"]
    assert relay_repo.COPY_OF_WITHDRAWN_SIGNAL not in [code for _, code in _codes(state)]
    assert _claim(db_path) == ("ENTRY", 2)
    assert _db_rows(
        db_path, "SELECT signal_id, promoted_from_signal_id FROM signals ORDER BY 1"
    ) == [(1, None), (2, 1)]
    assert _db_rows(
        db_path, "SELECT closed_by_channel_id, closed_by_message_id FROM signals ORDER BY signal_id"
    ) == [(V, CALL), (None, None)]


_WITHDRAWING_EDITS = {
    "chatter": (EDIT_CHATTER, "VOIDED", 1),
    "reaim": (DIRECTION_EDIT, "INVALID", 0),
    "unparseable": (UNPARSEABLE_EDIT, "INVALID", 0),
}


@pytest.mark.parametrize("change", ["deleted", *_WITHDRAWING_EDITS])
@pytest.mark.parametrize("how", ["relay-first", "blackout"])
def test_a_call_withdrawn_through_the_vips_copy_and_posted_again_is_a_new_signal(
    tmp_path, how, change
):
    """The VIP deletes (or breaks) its post -- which reaches signal 1
    through the VIP's copy -- and posts the call again: a new signal,
    traded, with no "copy of a withdrawn call" alert."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _relay_holds_with_chatter(client, listener, time, db_path, how=how)
        if change == "deleted":
            assert await client.emit_delete(V, CALL)
        else:
            assert await client.emit_edit(V, CALL, _WITHDRAWING_EDITS[change][0], edit_date=EDIT_1)
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=6)))

    _run(db_path, body)

    state = _state(db_path)
    closed, withdrawn = ("VOIDED", 1) if change == "deleted" else _WITHDRAWING_EDITS[change][1:]
    assert state["signals"] == [(1, R, COPY, closed, withdrawn), (2, V, REPOST, "NEW", 0)]
    assert state["queued"] == [(2, N)]
    assert relay_repo.COPY_OF_WITHDRAWN_SIGNAL not in [code for _, code in _codes(state)]
    assert _claim(db_path) == ("ENTRY", 2)


@pytest.mark.parametrize("change", list(_WITHDRAWING_EDITS))
def test_repost_then_edit_away_promotes_the_repost_when_the_relays_copy_holds_the_call(
    tmp_path, change
):
    """Promotion through the VIP's copy picks the repost, never the edited
    post itself (a re-aimed post still reads as a signal), and leaves the
    edited post naming the signal it closed."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _relay_holds_with_chatter(client, listener, time, db_path, how="relay-first")
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=2)))
        assert await client.emit_edit(V, CALL, _WITHDRAWING_EDITS[change][0], edit_date=EDIT_1)

    _run(db_path, body)

    state = _state(db_path)
    closed, withdrawn = _WITHDRAWING_EDITS[change][1:]
    assert state["signals"] == [(1, R, COPY, closed, withdrawn), (2, V, REPOST, "NEW", 0)]
    assert state["queued"] == [(2, N)]
    assert state["links"] == [(V, CALL, 1)]
    assert ("WARNING", edits_repo.SIGNAL_REPOST_PROMOTED, V, REPOST, 2) in state["alerts"]


def test_the_relays_own_repost_of_a_call_the_vip_withdrew_is_new_too(tmp_path):
    """The relay's copy holds signal 1 and the VIP deleted its post. The
    relay posting the call again on its own channel is a new signal, as
    for any channel reposting its own call."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _relay_holds_with_chatter(client, listener, time, db_path, how="relay-first")
        assert await client.emit_delete(V, CALL)
        assert await client.emit(R, _post(COPY + 1, after=timedelta(minutes=3)))

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "VOIDED", 1), (2, R, COPY + 1, "NEW", 0)]
    assert state["queued"] == [(2, N)]


def test_a_third_channels_copy_of_a_call_the_vip_withdrew_is_held_back_loudly(tmp_path):
    """Control: neither the channel holding the call nor the one whose post
    withdrew it -- still held back, with the CRITICAL alert naming who
    withdrew it."""
    db_path = tmp_path / "store.db"
    other = OTHER.channel_id

    async def body(client, listener, time):
        await _relay_holds_with_chatter(client, listener, time, db_path, how="relay-first")
        assert await client.emit_delete(V, CALL)
        assert await client.emit(other, _post(8001, after=timedelta(minutes=3)))

    _run(db_path, body, channels=(VIP, RELAY, OTHER))

    state = _state(db_path)
    assert state["signals"] == [(1, R, COPY, "VOIDED", 1)]
    assert state["queued"] == []
    assert ("CRITICAL", relay_repo.COPY_OF_WITHDRAWN_SIGNAL, other, 8001, 1) in state["alerts"]
    detail = _alert_detail(db_path, relay_repo.COPY_OF_WITHDRAWN_SIGNAL)
    assert f"withdrawn by message {CALL} on channel {V}" in detail


def test_a_double_post_re_aimed_promotes_nothing_while_the_call_stands_elsewhere(tmp_path):
    """Posted three times; the second post re-aimed: the call is in doubt
    (INVALID), and the first post still shows it -- the third is not
    promoted over that."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit(V, _post(REPOST, after=timedelta(seconds=30)))
        assert await client.emit(V, _post(REPOST_2, after=timedelta(seconds=40)))
        assert await client.emit_edit(V, REPOST, DIRECTION_EDIT, edit_date=EDIT_1)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, V, CALL, "INVALID", 0)]
    assert state["queued"] == []
    assert _codes(state) == [("CRITICAL", edits_repo.SIGNAL_INVALIDATED_BY_EDIT)]


def test_the_first_post_to_close_a_signal_is_the_one_recorded(conn):
    first = _ingest(conn, VIP, 4001)
    for channel_id, message_id in ((VIP.channel_id, 4001), (RELAY.channel_id, 7001)):
        signals_repo.record_closed_by(
            conn, signal_id=first.signal_id, channel_id=channel_id, message_id=message_id
        )
    assert _q(conn, "SELECT closed_by_channel_id, closed_by_message_id FROM signals") == [
        (VIP.channel_id, 4001)
    ]


def test_a_deletion_through_a_copy_reports_the_repost_it_promoted(conn):
    holder = _ingest(conn, RELAY, 7001, posted=POSTED + timedelta(seconds=2))
    _ingest(conn, VIP, 4001)  # the VIP's own post: a copy of the relay's
    _ingest(conn, VIP, 4012, posted=POSTED + timedelta(minutes=2))
    result = edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4001, now_utc=POSTED, accounts=ACCOUNTS,
        detected_by="event",
    )
    assert (result.outcome, result.signal_id) == ("voided", holder.signal_id)
    assert result.promoted_signal_id == holder.signal_id + 1
    assert _signals(conn)[1] == (holder.signal_id + 1, VIP.channel_id, 4012, "NEW")


# -- F08: the same instruction twice on one channel ---------------------------------


@pytest.mark.parametrize("second_reply_to", [4001, 4012], ids=["same-post", "its-repost"])
def test_the_same_instruction_said_twice_on_one_channel_is_two_instructions(
    conn, second_reply_to
):
    """A relay is another channel. A poster saying "close half" at TP1 and
    again at TP2, seven minutes apart, gives two instructions -- replying
    to the call, or to the poster's own repost of it."""
    holder = _ingest(conn, VIP, 4001)
    _ingest(conn, VIP, 4012, posted=POSTED + timedelta(minutes=1))  # a repeat: a copy of 4001
    first = _ingest(
        conn, VIP, 4002, CLOSE_HALF, posted=POSTED + timedelta(minutes=5), reply_to=4001
    )
    again = _ingest(
        conn, VIP, 4003, CLOSE_HALF, posted=POSTED + timedelta(minutes=12),
        reply_to=second_reply_to,
    )
    assert (first.outcome, again.outcome) == ("new", "new")
    assert again.relay_of_followup_id is None
    assert [row[2] for row in _followups(conn)] == [4002, 4003]
    assert {row[5] for row in _followups(conn)} == {holder.signal_id}
    queued = _q(conn, "SELECT COUNT(*) FROM outbox WHERE kind = 'FOLLOWUP' AND state = 'QUEUED'")
    assert queued == [(2 * len(ACCOUNTS),)]
    assert _metric(conn, relay_repo.RELAY_DUPLICATE_FOLLOWUPS_METRIC) == 0


# -- F08: a forwarded follow-up stored before the post it forwards ---------------------


@pytest.mark.parametrize("holder_first", [True, False], ids=["vip-holds", "relay-holds"])
@pytest.mark.parametrize("order", ["original-first", "forward-first"])
def test_a_forwarded_follow_up_knows_its_signal_whichever_post_is_stored_first(
    conn, order, holder_first
):
    """The relay forwards the VIP's call and the VIP's "close half" (a
    forward carries no reply). Whichever of the two follow-ups is stored
    first holds the instruction, and it targets the call either way."""
    if holder_first:
        holder = _ingest(conn, VIP, 4001)
        _ingest(conn, RELAY, 7001, posted=POSTED + timedelta(seconds=2), fwd=(VIP.channel_id, 4001))
        path = "reply"
    else:
        holder = _ingest(
            conn, RELAY, 7001, posted=POSTED + timedelta(seconds=2), fwd=(VIP.channel_id, 4001)
        )
        _ingest(conn, VIP, 4001)
        path = "relay-reply"
    at = POSTED + timedelta(minutes=5)

    def original():
        return _ingest(conn, VIP, 4002, CLOSE_HALF, posted=at, reply_to=4001)

    def forward():
        return _ingest(
            conn, RELAY, 7002, CLOSE_HALF, posted=at + timedelta(seconds=2),
            fwd=(VIP.channel_id, 4002),
        )

    if order == "original-first":
        first, second = original(), forward()
    else:
        first, second = forward(), original()

    assert (first.outcome, second.outcome) == ("new", "relay_duplicate")
    assert second.relay_of_followup_id == first.followup_id
    (held,) = _followups(conn)
    assert held[0] == first.followup_id
    assert held[5:] == (holder.signal_id, path if order == "original-first" else "copy-reply")
    queued = _q(conn, "SELECT COUNT(*) FROM outbox WHERE kind = 'FOLLOWUP'")
    assert queued == [(len(ACCOUNTS),)]


def test_a_follow_ups_target_is_never_replaced_by_its_copys(conn):
    """The follow-up held keeps the target it was stored with; a copy only
    fills in a target the held one lacks."""
    holder = _ingest(conn, VIP, 4001)
    _ingest(conn, RELAY, 5001, posted=POSTED + timedelta(seconds=60))
    at = POSTED + timedelta(minutes=5)
    _ingest(conn, VIP, 4002, CLOSE_HALF, posted=at, reply_to=4001)
    _ingest(conn, RELAY, 5002, CLOSE_HALF, posted=at, reply_to=5001)  # 'relay-reply' of its own
    assert _followups(conn)[0][5:] == (holder.signal_id, "reply")


# -- F09: a late forward of a withdrawn post ---------------------------------------------


@pytest.mark.parametrize(
    ("reposted", "forwarded"),
    [
        (timedelta(minutes=2), timedelta(minutes=3)),
        (timedelta(minutes=2), timedelta(hours=2)),  # near the deleted post only
        (timedelta(minutes=30), timedelta(minutes=31)),  # near the forward only
    ],
)
def test_a_forward_of_a_withdrawn_post_repeats_the_live_repost_of_the_call(
    conn, reposted, forwarded
):
    """The VIP deleted its call and posted it again (a new signal, queued);
    the relay's forward of the deleted post arrives after both. It repeats
    the live repost -- no alert telling the user to trade the call by hand
    while it is queued already."""
    _ingest(conn, VIP, 4001)
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4001, now_utc=POSTED + timedelta(minutes=1),
        accounts=ACCOUNTS, detected_by="event",
    )
    repost = _ingest(conn, VIP, 4012, posted=POSTED + reposted)
    forward = _ingest(conn, RELAY, 7001, posted=POSTED + forwarded, fwd=(VIP.channel_id, 4001))
    assert repost.outcome == "new"
    assert (forward.outcome, forward.relay_of_signal_id) == ("relay_duplicate", repost.signal_id)
    assert relay_repo.COPY_OF_WITHDRAWN_SIGNAL not in [code for _, code, *_ in _alerts(conn)]
    assert _queued_entries(conn) == [(repost.signal_id, "acct-1"), (repost.signal_id, "acct-2")]


def test_a_message_that_holds_a_signal_is_never_matched_against_it(conn):
    """For an edit path asking about a message that already has its own
    signals row: find_signal_relay never answers with that signal, by the
    trade nor as the live repost of a withdrawn post it forwards."""
    _ingest(conn, VIP, 4001)
    (trade_hash,) = _q(conn, "SELECT trade_hash FROM signals")[0]
    ask = dict(trade_hash=trade_hash, relay_window_seconds=900)
    assert relay_repo.find_signal_relay(
        conn, channel_id=VIP.channel_id, message_id=4001, posted_at_utc=POSTED,
        fwd_from_channel_id=None, fwd_from_post_id=None, **ask,
    ) is None

    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4001, now_utc=POSTED, accounts=ACCOUNTS,
        detected_by="event",
    )
    later = POSTED + timedelta(hours=2)
    own = _ingest(conn, RELAY, 7001, posted=later)  # its own signal: outside the window
    assert own.outcome == "new"
    match = relay_repo.find_signal_relay(
        conn, channel_id=RELAY.channel_id, message_id=7001, posted_at_utc=later,
        fwd_from_channel_id=VIP.channel_id, fwd_from_post_id=4001, **ask,
    )
    assert match is not None and match.signal_id != own.signal_id


def test_a_promotion_re_points_follow_ups_that_replied_to_any_repost_it_takes_over(conn):
    """The call posted three times, "close half" replying to the third
    post; the first is deleted, the second promoted, the third now repeats
    the second's signal -- and so does the follow-up that replied to it."""
    holder = _ingest(conn, VIP, 4001)
    _ingest(conn, VIP, 4002, posted=POSTED + timedelta(minutes=1))
    _ingest(conn, VIP, 4003, posted=POSTED + timedelta(minutes=2))
    _ingest(conn, VIP, 4004, CLOSE_HALF, posted=POSTED + timedelta(minutes=3), reply_to=4003)
    assert [row[5:] for row in _followups(conn)] == [(holder.signal_id, "relay-reply")]
    result = edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4001, now_utc=POSTED, accounts=ACCOUNTS,
        detected_by="event",
    )
    promoted = result.promoted_signal_id
    assert _signals(conn)[1] == (promoted, VIP.channel_id, 4002, "NEW")
    assert _link(conn, VIP, 4003) == promoted
    assert [row[5:] for row in _followups(conn)] == [(promoted, "relay-reply")]


def test_a_window_deletion_reversed_after_a_promotion_names_the_promoted_repost(tmp_path):
    """The window inferred the VIP's call deleted, voided it and promoted
    its repost; then the call reads back. The alert must say the repost
    now carries the call, or the user trades it twice by hand."""
    db_path = tmp_path / "store.db"
    client = ScriptedClient()

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=2)))
        await _round(listener, time)
        # Only the VIP's window is read by id (the relay has no posts): two
        # misses of the VIP's call in a row, then it is back.
        client.ids_answers = [_without(CALL)] * 2
        for _ in range(3):
            await _round(listener, time)

    _run(db_path, body, client=client)

    state = _state(db_path)
    assert state["signals"] == [(1, V, CALL, "VOIDED", 1), (2, V, REPOST, "NEW", 0)]
    assert ("CRITICAL", edits_repo.DELETION_REVERSED) in _codes(state)
    detail = _alert_detail(db_path, edits_repo.DELETION_REVERSED)
    assert f"message {REPOST}" in detail and "signal 2" in detail
    assert "twice" in detail


# -- F04: the call and its repost deleted together -----------------------------------


@pytest.mark.parametrize(
    "ids", [(CALL, REPOST), (REPOST, CALL)], ids=["call-first", "repost-first"]
)
@pytest.mark.parametrize("found_by", ["event", "window"])
def test_a_call_deleted_with_its_repost_in_one_go_is_voided_once(tmp_path, ids, found_by):
    """One deletion naming both posts (or one window read missing both):
    the repost is not promoted in between -- its entries would be
    claimable until the next job voided them -- and no double-post warning
    claims the call still stands."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _vip_call_stored(client, listener, time)
        assert await client.emit(V, _post(REPOST, after=timedelta(minutes=2)))
        await _round(listener, time)
        if found_by == "event":
            assert await client.emit_delete(V, *ids)
        else:
            with client.blackout():
                assert not await client.emit_delete(V, *ids)
            for _ in range(3):
                await _round(listener, time)

    _run(db_path, body)

    state = _state(db_path)
    assert state["signals"] == [(1, V, CALL, "VOIDED", 1)]
    assert state["queued"] == []
    expected = [("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION)]
    if found_by == "window":
        expected.append(("CRITICAL", edits_repo.WINDOW_VOIDED_SIGNALS))
    assert _codes(state) == expected


# -- F02: a copy's edit never reverts what the poster moved since -----------------------

TP1_EDIT = SIGNAL_TEXT.replace("4490", "4491")  # TP1 4490 -> 4491, the stop left at 4470
assert SIGNAL_TEXT.count("4490") == 1


def _copied_and_executed(conn) -> int:
    """The VIP's call (signal 1, acct-1's entry DONE) and the relay's hand
    copy of it 30 s later."""
    holder = _ingest(conn, VIP, 4001)
    _ingest(conn, RELAY, 7001, posted=POSTED + timedelta(seconds=30))
    conn.execute("UPDATE outbox SET state = 'DONE' WHERE kind = 'ENTRY' AND account_id = 'acct-1'")
    conn.commit()
    return holder.signal_id


def _edit(conn, channel: ChannelCfg, message_id: int, text: str, edit_date: datetime):
    return edits_repo.apply_edit(
        conn, channel_cfg=channel, message_id=message_id, text=text, posted_at_utc=POSTED,
        edit_date_utc=edit_date, reply_to_message_id=None, now_utc=edit_date,
        accounts=ACCOUNTS, detected_by="event",
    )


def _levels(conn) -> tuple:
    (row,) = _q(
        conn,
        "SELECT version, json_extract(parsed_json, '$.sl'), json_extract(parsed_json, '$.tps') "
        "FROM signals",
    )
    return row[0], row[1], json.loads(row[2])


def _amendments(conn) -> list[tuple]:
    return _q(
        conn,
        "SELECT op, json_extract(parsed_json, '$.price_hint') FROM followups "
        "WHERE edit_seq > 0 ORDER BY followup_id",
    )


def test_a_copys_edit_never_puts_back_a_stop_the_poster_moved(conn):
    """The VIP moved the stop (amended); the relay never mirrored that and
    later edits only its TP1. Its whole version would put the old stop
    back on a live position: nothing is applied, a WARNING says why."""
    signal_id = _copied_and_executed(conn)
    assert _edit(conn, VIP, 4001, SL_EDIT, EDIT_1).outcome == "reparsed"
    result = _edit(conn, RELAY, 7001, TP1_EDIT, EDIT_2)

    assert result.outcome == "recorded"
    assert _levels(conn) == (2, 4460.0, [4490.0, 4495.0, 4500.0, 4505.0])
    assert _amendments(conn) == [("amend_sl", 4460.0)]
    assert _alerts(conn) == [
        ("WARNING", edits_repo.SIGNAL_COPY_OUT_OF_DATE, RELAY.channel_id, 7001, signal_id)
    ]
    (detail,) = _q(conn, "SELECT detail FROM alerts")[0]
    assert "TP1 4490 -> 4491" in detail and "SL 4470" in detail and "4460" in detail


def test_a_copy_mirroring_an_edit_the_poster_made_changes_nothing_more(conn):
    """The VIP moved its stop, then its TP1; the relay mirrors only the
    TP1 edit. The signal carries it already: nothing to do, nothing said."""
    _copied_and_executed(conn)
    _edit(conn, VIP, 4001, SL_EDIT, EDIT_1)
    _edit(conn, VIP, 4001, SL_EDIT.replace("4490", "4491"), EDIT_2)
    before = (_levels(conn), _amendments(conn))
    result = _edit(conn, RELAY, 7001, TP1_EDIT, EDIT_2 + timedelta(minutes=1))

    assert result.outcome == "recorded"
    assert (_levels(conn), _amendments(conn)) == before
    assert _alerts(conn) == []


def test_a_copys_edit_of_the_level_it_changes_still_wins(conn):
    """Control: the copy's own change is applied as the holder's would be,
    latest first -- only levels it did not touch are protected."""
    _copied_and_executed(conn)
    _edit(conn, VIP, 4001, SL_EDIT, EDIT_1)  # SL 4460
    result = _edit(conn, RELAY, 7001, SIGNAL_TEXT.replace("4470", "4465"), EDIT_2)
    assert result.outcome == "reparsed"
    assert _levels(conn)[:2] == (3, 4465.0)
    assert _amendments(conn) == [("amend_sl", 4460.0), ("amend_sl", 4465.0)]


_THREE_TPS = "\n".join(line for line in SIGNAL_TEXT.split("\n") if "TP4" not in line)


@pytest.mark.parametrize(
    ("vip_edit", "relay_edit", "still_shows"),
    [
        (SL_EDIT, TP1_EDIT, "SL 4470"),
        (SIGNAL_TEXT.replace("4495", "4497"), SIGNAL_TEXT.replace("4470", "4465"), "TP2 4495"),
        (_THREE_TPS, SIGNAL_TEXT.replace("4470", "4465"), "4 take-profits"),
    ],
    ids=["stop", "a-target", "the-ladder"],
)
def test_every_level_the_poster_moved_is_protected_from_a_copys_edit(
    conn, vip_edit, relay_edit, still_shows
):
    _copied_and_executed(conn)
    _edit(conn, VIP, 4001, vip_edit, EDIT_1)
    before = (_levels(conn), _amendments(conn))
    result = _edit(conn, RELAY, 7001, relay_edit, EDIT_2)
    assert result.outcome == "recorded"
    assert (_levels(conn), _amendments(conn)) == before
    out_of_date = _q(
        conn, "SELECT detail FROM alerts WHERE code = ?", edits_repo.SIGNAL_COPY_OUT_OF_DATE
    )
    assert len(out_of_date) == 1 and still_shows in out_of_date[0][0]
