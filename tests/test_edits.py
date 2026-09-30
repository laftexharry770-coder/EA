"""Tests for edited and deleted Telegram messages (spec 8.3; Plan 2 Task 10).

A poster edits a signal to fix a stop, move a target, or -- rarely --
flip BUY to SELL, and deletes one to retract a bad call. An edited stop
we never see is a live position with the wrong stop; a deletion we never
see is a trade the poster retracted, still held. So:

- `MessageEdited` / `MessageDeleted` are *hints*, handled like
  `NewMessage`: route, normalise, submit, return.
- The poll's **window re-read** is the source of truth: after each
  channel's forward read it fetches the newest stored messages -- above
  the pointer too (F03) -- by id (`get_messages(ids=[...])`), finds edits the stream
  dropped, and finds deletions by *absence* -- a `None` where a message
  used to be.
- Everything is decided inside one writer job against the stored row, so
  an edit or deletion seen by both paths is processed exactly once.

Everything runs the real listener, poll and store against a temporary
database; only Telegram is faked. "Executed" is simulated the way Plan 3
will record it: an ENTRY outbox row for the signal reaching DONE.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from telethon import errors, events

from fakes.telegram import (
    FakeMessage,
    FakeMessageDeletedEvent,
    FakeMessageEditedEvent,
    FakeTelegramClient,
)
from xaucopier.config.accounts import AccountCfg
from xaucopier.config.channels import ChannelCfg
from xaucopier.ingest import edits
from xaucopier.ingest.edits import DEFAULT_EDIT_WINDOW, reconcile_window
from xaucopier.ingest.listener import Listener
from xaucopier.ingest.poll import HistoryPoll
from xaucopier.store import db, edits_repo

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "messages" / "mmh_buy_4tp.json"
_FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
SIGNAL_TEXT: str = _FIXTURE["text"]
SIGNAL_ID_MSG: int = _FIXTURE["message_id"]  # 4001
POSTED = datetime.fromisoformat(_FIXTURE["posted_at_utc"])
NOW = datetime(2026, 9, 5, 11, 0, tzinfo=UTC)
EDIT_1 = POSTED + timedelta(minutes=5)
EDIT_2 = POSTED + timedelta(minutes=10)

assert SIGNAL_TEXT.count("4470") == 1 and SIGNAL_TEXT.count("BUY") == 1
assert SIGNAL_TEXT.count("4495") == 1 and SIGNAL_TEXT.count("4480-4485") == 1
SL_EDIT = SIGNAL_TEXT.replace("4470", "4460")
SL_AND_TP2_EDIT = SL_EDIT.replace("4495", "4497")
DIRECTION_EDIT = SIGNAL_TEXT.replace("BUY", "SELL")
ENTRY_EDIT = SIGNAL_TEXT.replace("4480-4485", "4475-4480")
UNPARSEABLE_EDIT = SIGNAL_TEXT.replace("SL: 4470", "")  # header intact, no stop
WHITESPACE_EDIT = SIGNAL_TEXT.replace("\n", "\n\n   ")
EMOJI_EDIT = SIGNAL_TEXT + " \U0001F525\U0001F680"
CHATTER_TEXT = "Good morning traders"
FOLLOWUP_TEXT = "Close half now"

CHANNEL = ChannelCfg(
    channel_id=_FIXTURE["channel_id"], name="Channel A", parser="mmh", ordinal=1, enabled=True
)
A = CHANNEL.channel_id
UNWATCHED = -1003333333333

ACCOUNTS = tuple(
    AccountCfg(
        account_id=f"acct-{ordinal}",
        broker="TestBroker",
        terminal_path=r"C:\terminal\terminal64.exe",
        login=123456,
        server="TestBroker-Demo",
        symbol="XAUUSD",
        fixed_lot=0.01,
        magic_base=1000 * ordinal,
        ordinal=ordinal,
        allow_live=False,
        enabled=True,
    )
    for ordinal in (1, 2)
)
N_ACCOUNTS = len(ACCOUNTS)


def _msg(message_id: int, text: str, *, date: datetime = POSTED) -> FakeMessage:
    return FakeMessage(id=message_id, message=text, date=date)


# -- harness ---------------------------------------------------------------------


class SteppedTime:
    """The poll's injected sleep/clock: each `wake()` is one poll round."""

    def __init__(self) -> None:
        self.now = 1000.0
        self._gates: list[asyncio.Event] = []
        self._woken = 0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        gate = asyncio.Event()
        self._gates.append(gate)
        await gate.wait()
        self.now += seconds

    @property
    def asleep(self) -> bool:
        return len(self._gates) > self._woken

    def wake(self) -> None:
        assert self.asleep
        self._gates[self._woken].set()
        self._woken += 1


@dataclass
class ScriptedClient(FakeTelegramClient):
    """The fake, plus scripted trouble for by-id reads only."""

    ids_failures: list[BaseException | str] = field(default_factory=list)
    #: Rewrites every by-id answer while set.
    ids_answer: Callable[[list], list] | None = None
    #: One-shot rewrites: each by-id read uses (and drops) the first.
    ids_answers: list[Callable[[list], list]] = field(default_factory=list)
    before_ids_read: Callable[[], Awaitable[None]] | None = None

    async def get_messages(self, *args, **kwargs):
        if kwargs.get("ids") is None:
            return await super().get_messages(*args, **kwargs)
        if self.before_ids_read is not None:
            await self.before_ids_read()
        if self.ids_failures:
            failure = self.ids_failures.pop(0)
            if failure == "hang":
                await asyncio.Event().wait()
            raise failure
        result = await super().get_messages(*args, **kwargs)
        if self.ids_answers:
            return self.ids_answers.pop(0)(result)
        return self.ids_answer(result) if self.ids_answer is not None else result


@contextlib.asynccontextmanager
async def _polling(db_path: Path, client: FakeTelegramClient, time: SteppedTime, **poll_kwargs):
    """A started listener whose poll is asleep before its first round.
    Events reach the listener at once; history is only read when a test
    runs a round."""
    listener = Listener(
        client=client,
        channels=(CHANNEL,),
        accounts=ACCOUNTS,
        writer=db.Writer(db_path),
        db_path=db_path,
        now=lambda: NOW,
        poll_hook=HistoryPoll(sleep=time.sleep, clock=time.clock, **poll_kwargs),
    )
    await listener.start()
    try:
        await _until_asleep(listener, time)
        yield listener
    finally:
        await listener.aclose()


async def _until_asleep(listener: Listener, time: SteppedTime) -> None:
    deadline = asyncio.get_running_loop().time() + 2.0
    while not time.asleep:
        task = listener.poll_task
        if task is not None and task.done():
            reason = "cancelled" if task.cancelled() else repr(task.exception())
            raise AssertionError(f"the poll stopped instead of finishing its round: {reason}")
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the poll never went back to sleep")
        await asyncio.sleep(0.001)


async def _round(listener: Listener, time: SteppedTime) -> None:
    await _until_asleep(listener, time)
    time.wake()
    await _until_asleep(listener, time)


def _rows(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = db.connect(db_path, role="reader")
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _write(db_path: Path, sql: str, params: tuple = ()) -> None:
    conn = db.connect(db_path, role="writer")
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _seed_pointer(db_path: Path, value: int) -> None:
    _write(
        db_path,
        "INSERT INTO channel_state (channel_id, last_processed_message_id) VALUES (?, ?)",
        (A, value),
    )


def _pointer(db_path: Path) -> int:
    rows = _rows(
        db_path, "SELECT last_processed_message_id FROM channel_state WHERE channel_id = ?", (A,)
    )
    return rows[0][0] if rows else 0


def _signal(db_path: Path, message_id: int = SIGNAL_ID_MSG) -> dict:
    rows = _rows(
        db_path,
        "SELECT signal_id, state, version, parsed_json FROM signals "
        "WHERE channel_id = ? AND message_id = ?",
        (A, message_id),
    )
    if not rows:
        return {}
    signal_id, state, version, parsed = rows[0]
    return {"signal_id": signal_id, "state": state, "version": version, **json.loads(parsed)}


def _message(db_path: Path, message_id: int = SIGNAL_ID_MSG) -> dict:
    (row,) = _rows(
        db_path,
        "SELECT kind, raw_text, edit_date_utc, deleted_at_utc, deleted_detected_by "
        "FROM messages WHERE channel_id = ? AND message_id = ?",
        (A, message_id),
    )
    return dict(zip(("kind", "raw_text", "edit_date_utc", "deleted_at_utc", "detected_by"), row))


def _alerts(db_path: Path) -> list[tuple[str, str]]:
    return _rows(db_path, "SELECT level, code FROM alerts ORDER BY alert_id")


def _outbox(db_path: Path, kind: str) -> list[tuple[str, str]]:
    return _rows(
        db_path,
        "SELECT account_id, state FROM outbox WHERE kind = ? ORDER BY outbox_id",
        (kind,),
    )


def _amendments(db_path: Path) -> list[tuple]:
    rows = _rows(
        db_path,
        "SELECT op, parsed_json, target_signal_id, resolution_path, edit_seq, "
        "reply_to_message_id FROM followups WHERE edit_seq > 0 ORDER BY edit_seq",
    )
    out = []
    for op, parsed, target, path, seq, reply_to in rows:
        parsed = json.loads(parsed)
        out.append((op, parsed["price_hint"], parsed["tp_index_hint"], target, path, seq, reply_to))
    return out


def _metric(db_path: Path, name: str) -> int:
    return _rows(db_path, "SELECT COALESCE(SUM(value), 0) FROM metrics WHERE name = ?", (name,))[0][0]


def _mark_entry(db_path: Path, account_id: str, state: str) -> None:
    """Plan 3's worker, simulated: an ENTRY row reaching `state`."""
    _write(
        db_path,
        "UPDATE outbox SET state = ? WHERE kind = 'ENTRY' AND account_id = ?",
        (state, account_id),
    )


async def _stored_signal(client: FakeTelegramClient, listener: Listener, time: SteppedTime) -> None:
    """The signal arrives live and one poll round passes it: stored, pointer on it."""
    assert await client.emit(A, _msg(SIGNAL_ID_MSG, SIGNAL_TEXT))
    await _round(listener, time)
    assert listener.read_last_processed_message_id(A) == SIGNAL_ID_MSG


CHATTER_IDS = (SIGNAL_ID_MSG + 1, SIGNAL_ID_MSG + 2, SIGNAL_ID_MSG + 3)


async def _stored_signal_among_chatter(
    client: FakeTelegramClient, listener: Listener, time: SteppedTime
) -> None:
    """The signal and three chatter messages after it, all stored and at
    or below the pointer: a window of four. A deletion found by absence
    needs company -- a read missing *every* message it expected trips the
    circuit breaker and is not believed."""
    assert await client.emit(A, _msg(SIGNAL_ID_MSG, SIGNAL_TEXT))
    for message_id in CHATTER_IDS:
        assert await client.emit(A, _msg(message_id, CHATTER_TEXT))
    await _round(listener, time)
    assert listener.read_last_processed_message_id(A) == CHATTER_IDS[-1]


def _without(*message_ids: int) -> Callable[[list], list]:
    """A scripted by-id fault: these ids come back None although the
    messages exist."""

    def answer(result: list) -> list:
        return [None if m is not None and m.id in message_ids else m for m in result]

    return answer


def _signals_and_chatter(n_signals: int, n_chatter: int) -> list[FakeMessage]:
    """Distinct signals (distinct SLs, so no relay dedup) then chatter."""
    messages = [
        _msg(SIGNAL_ID_MSG + i, SIGNAL_TEXT.replace("4470", str(4461 + i)))
        for i in range(n_signals)
    ]
    messages += [
        _msg(SIGNAL_ID_MSG + n_signals + j, f"chatter number {j}") for j in range(n_chatter)
    ]
    return messages


def _withdrawn(db_path: Path, message_id: int = SIGNAL_ID_MSG) -> bool:
    rows = _rows(
        db_path,
        "SELECT withdrawn_at_utc FROM signals WHERE channel_id = ? AND message_id = ?",
        (A, message_id),
    )
    return rows[0][0] is not None


def _run(db_path: Path, body, *, client: FakeTelegramClient | None = None, **poll_kwargs):
    """Seed the pointer below the signal, start everything, run `body`."""
    _seed_pointer(db_path, SIGNAL_ID_MSG - 1)
    time = SteppedTime()
    client = client if client is not None else FakeTelegramClient()

    async def scenario():
        async with _polling(db_path, client, time, **poll_kwargs) as listener:
            return await body(client, listener, time)

    return asyncio.run(scenario())


# -- edits: the policy ------------------------------------------------------------


def test_sl_edit_of_an_unexecuted_signal_replaces_the_parse_and_bumps_the_version(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        assert await client.emit_edit(A, SIGNAL_ID_MSG, SL_EDIT, edit_date=EDIT_1)
        return listener.handler_error_count

    assert _run(db_path, body) == 0

    signal = _signal(db_path)
    assert (signal["state"], signal["version"], signal["sl"]) == ("NEW", 2, 4460.0)
    assert signal["tps"] == [4490.0, 4495.0, 4500.0, 4505.0]
    assert _alerts(db_path) == []
    assert _amendments(db_path) == []
    assert _outbox(db_path, "ENTRY") == [("acct-1", "QUEUED"), ("acct-2", "QUEUED")]
    assert _outbox(db_path, "FOLLOWUP") == []
    message = _message(db_path)
    assert (message["raw_text"], message["edit_date_utc"]) == (SL_EDIT, EDIT_1.isoformat())


@pytest.mark.parametrize("cosmetic", [WHITESPACE_EDIT, EMOJI_EDIT], ids=["whitespace", "emoji"])
def test_a_cosmetic_edit_is_logged_once_and_dropped(
    tmp_path: Path, cosmetic: str, caplog: pytest.LogCaptureFixture
) -> None:
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        with caplog.at_level(logging.INFO, logger="xaucopier.store.edits_repo"):
            assert await client.emit_edit(A, SIGNAL_ID_MSG, cosmetic, edit_date=EDIT_1)
            await _round(listener, time)  # the window re-reads it: no second log
            await _round(listener, time)

    _run(db_path, body)

    signal = _signal(db_path)
    assert (signal["state"], signal["version"], signal["sl"]) == ("NEW", 1, 4470.0)
    assert _alerts(db_path) == []
    assert _outbox(db_path, "FOLLOWUP") == []
    cosmetic_logs = [r for r in caplog.records if "cosmetic" in r.getMessage()]
    assert len(cosmetic_logs) == 1
    # The verbatim text is still the latest (raw_text is "verbatim, always").
    assert _message(db_path)["raw_text"] == cosmetic


@pytest.mark.parametrize("entry_state", ["DONE", "CLAIMED"], ids=["executed", "in-flight"])
def test_sl_edit_of_an_executed_signal_enqueues_one_amend_sl_and_fans_it_out(
    tmp_path: Path, entry_state: str
) -> None:
    """In flight (an ENTRY claimed, none done) is treated like executed:
    the order may be at the broker already, carrying the old stop."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        _mark_entry(db_path, "acct-1", entry_state)
        assert await client.emit_edit(A, SIGNAL_ID_MSG, SL_EDIT, edit_date=EDIT_1)

    _run(db_path, body)

    signal = _signal(db_path)
    assert (signal["state"], signal["version"], signal["sl"]) == ("NEW", 2, 4460.0)
    assert _amendments(db_path) == [
        ("amend_sl", 4460.0, None, signal["signal_id"], "edit", 1, SIGNAL_ID_MSG)
    ]
    assert _outbox(db_path, "FOLLOWUP") == [("acct-1", "QUEUED"), ("acct-2", "QUEUED")]
    assert _alerts(db_path) == []


def test_every_changed_level_of_an_executed_signal_gets_its_own_amendment(
    tmp_path: Path,
) -> None:
    """SL and TP2 in one edit, then the SL again: three amendments, each
    fanned out. Before migration 2's edit_seq the second could not even
    be stored."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        _mark_entry(db_path, "acct-1", "DONE")
        assert await client.emit_edit(A, SIGNAL_ID_MSG, SL_AND_TP2_EDIT, edit_date=EDIT_1)
        second = SL_AND_TP2_EDIT.replace("4460", "4455")
        assert await client.emit_edit(A, SIGNAL_ID_MSG, second, edit_date=EDIT_2)
        return listener.handler_error_count

    assert _run(db_path, body) == 0

    signal_id = _signal(db_path)["signal_id"]
    assert _amendments(db_path) == [
        ("amend_sl", 4460.0, None, signal_id, "edit", 1, SIGNAL_ID_MSG),
        ("amend_tp", 4497.0, 2, signal_id, "edit", 2, SIGNAL_ID_MSG),
        ("amend_sl", 4455.0, None, signal_id, "edit", 3, SIGNAL_ID_MSG),
    ]
    assert len(_outbox(db_path, "FOLLOWUP")) == 3 * N_ACCOUNTS
    assert _signal(db_path)["version"] == 3


def test_a_changed_tp_ladder_on_an_executed_signal_is_alerted(tmp_path: Path) -> None:
    """Legs cannot be added or removed after entry, so a ladder that
    changes length is something only a human can reconcile."""
    db_path = tmp_path / "store.db"
    three_tps = SIGNAL_TEXT.replace("\n\U0001F3AF TP4 :4505", "")

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        _mark_entry(db_path, "acct-1", "DONE")
        assert await client.emit_edit(A, SIGNAL_ID_MSG, three_tps, edit_date=EDIT_1)

    _run(db_path, body)

    assert _alerts(db_path) == [("WARNING", edits_repo.TP_LADDER_CHANGED_AFTER_ENTRY)]
    assert _signal(db_path)["tps"] == [4490.0, 4495.0, 4500.0]
    assert _amendments(db_path) == []


@pytest.mark.parametrize("executed", [False, True], ids=["unexecuted", "executed"])
@pytest.mark.parametrize("edit", [DIRECTION_EDIT, ENTRY_EDIT], ids=["direction", "entry"])
def test_an_edit_that_re_aims_the_signal_invalidates_it_with_a_critical_alert(
    tmp_path: Path, executed: bool, edit: str
) -> None:
    """Never silently re-aim a trade: the stored parse is kept as it was
    (it is what any live leg was opened on), the signal goes INVALID, the
    entries not yet taken are skipped, and nothing live is touched."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        if executed:
            _mark_entry(db_path, "acct-1", "DONE")
        assert await client.emit_edit(A, SIGNAL_ID_MSG, edit, edit_date=EDIT_1)

    _run(db_path, body)

    signal = _signal(db_path)
    assert (signal["state"], signal["version"], signal["side"]) == ("INVALID", 1, "buy")
    assert signal["zone"] == {"lo": 4480.0, "hi": 4485.0}
    assert _alerts(db_path) == [("CRITICAL", edits_repo.SIGNAL_INVALIDATED_BY_EDIT)]
    assert _amendments(db_path) == []
    assert _outbox(db_path, "FOLLOWUP") == []
    expected_entries = [("acct-1", "DONE" if executed else "SKIPPED"), ("acct-2", "SKIPPED")]
    assert _outbox(db_path, "ENTRY") == expected_entries
    assert _message(db_path)["raw_text"] == edit


def test_an_edit_into_unparseable_text_invalidates_alerts_and_reclassifies(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        assert await client.emit_edit(A, SIGNAL_ID_MSG, UNPARSEABLE_EDIT, edit_date=EDIT_1)

    _run(db_path, body)

    assert _signal(db_path)["state"] == "INVALID"
    assert _alerts(db_path) == [("CRITICAL", edits_repo.SIGNAL_UNPARSEABLE_AFTER_EDIT)]
    assert _message(db_path)["kind"] == "parse_failed"
    assert _outbox(db_path, "ENTRY") == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]


@pytest.mark.parametrize("path", ["event", "window"])
@pytest.mark.parametrize("original", [CHATTER_TEXT, FOLLOWUP_TEXT], ids=["chatter", "followup"])
def test_an_edit_that_turns_a_message_into_a_signal_never_opens_a_trade(
    tmp_path: Path, path: str, original: str
) -> None:
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        assert await client.emit(A, _msg(SIGNAL_ID_MSG, original))
        await _round(listener, time)
        followup_rows = _outbox(db_path, "FOLLOWUP")
        if path == "event":
            assert await client.emit_edit(A, SIGNAL_ID_MSG, SIGNAL_TEXT, edit_date=EDIT_1)
        else:
            with client.blackout():
                assert not await client.emit_edit(A, SIGNAL_ID_MSG, SIGNAL_TEXT, edit_date=EDIT_1)
            await _round(listener, time)
        return followup_rows

    followup_rows_before = _run(db_path, body)

    assert _message(db_path)["kind"] == "signal"
    assert _message(db_path)["raw_text"] == SIGNAL_TEXT
    assert _signal(db_path) == {}  # no signals row: it would relay-dedup a genuine repost
    assert _outbox(db_path, "ENTRY") == []
    codes = [code for _, code in _alerts(db_path)]
    assert edits_repo.EDIT_CREATED_SIGNAL in codes
    if original == FOLLOWUP_TEXT:
        # The instruction it used to carry is withdrawn, not executed.
        assert followup_rows_before == [("acct-1", "QUEUED"), ("acct-2", "QUEUED")]
        assert _outbox(db_path, "FOLLOWUP") == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]


def test_chatter_edited_into_a_follow_up_dispatches_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        assert await client.emit(A, _msg(SIGNAL_ID_MSG, CHATTER_TEXT))
        assert await client.emit_edit(A, SIGNAL_ID_MSG, "Close all now", edit_date=EDIT_1)

    _run(db_path, body)

    assert _message(db_path)["kind"] == "followup"
    assert _outbox(db_path, "FOLLOWUP") == []
    assert _alerts(db_path) == [("WARNING", edits_repo.EDIT_CREATED_FOLLOWUP)]


@pytest.mark.parametrize("executed", [False, True], ids=["unexecuted", "executed"])
def test_a_signal_edited_into_chatter_is_treated_as_a_retraction(
    tmp_path: Path, executed: bool
) -> None:
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        if executed:
            _mark_entry(db_path, "acct-1", "DONE")
        assert await client.emit_edit(A, SIGNAL_ID_MSG, CHATTER_TEXT, edit_date=EDIT_1)

    _run(db_path, body)

    assert _message(db_path)["kind"] == "skip"
    assert _outbox(db_path, "FOLLOWUP") == []  # nothing closed
    if executed:
        assert _signal(db_path)["state"] == "NEW"
        assert _alerts(db_path) == [("CRITICAL", edits_repo.LIVE_SIGNAL_RETRACTED_BY_EDIT)]
        assert _outbox(db_path, "ENTRY") == [("acct-1", "DONE"), ("acct-2", "SKIPPED")]
    else:
        assert _signal(db_path)["state"] == "VOIDED"
        assert _alerts(db_path) == [("WARNING", edits_repo.SIGNAL_RETRACTED_BY_EDIT)]
        assert _outbox(db_path, "ENTRY") == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]


def test_an_invalid_signal_stays_invalid_whatever_the_next_edit_says(tmp_path: Path) -> None:
    """BUY -> SELL -> BUY: the second edit does not quietly revive it."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        assert await client.emit_edit(A, SIGNAL_ID_MSG, DIRECTION_EDIT, edit_date=EDIT_1)
        assert await client.emit_edit(A, SIGNAL_ID_MSG, SIGNAL_TEXT, edit_date=EDIT_2)

    _run(db_path, body)

    assert _signal(db_path)["state"] == "INVALID"
    assert _alerts(db_path) == [
        ("CRITICAL", edits_repo.SIGNAL_INVALIDATED_BY_EDIT),
        ("WARNING", edits_repo.EDIT_OF_CLOSED_SIGNAL),
    ]
    assert _outbox(db_path, "ENTRY") == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]


# -- edits: detection ------------------------------------------------------------


def test_an_edit_dropped_by_the_update_stream_is_found_by_the_window_re_read(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        with client.blackout():
            assert not await client.emit_edit(A, SIGNAL_ID_MSG, SL_EDIT, edit_date=EDIT_1)
        before = _signal(db_path)["version"]
        await _round(listener, time)
        return before

    assert _run(db_path, body) == 1

    assert (_signal(db_path)["version"], _signal(db_path)["sl"]) == (2, 4460.0)
    assert _metric(db_path, edits_repo.EDIT_MISSED_BY_EVENTSTREAM_METRIC) == 1


@pytest.mark.parametrize("first", ["event", "window"])
def test_an_edit_seen_by_both_the_event_and_the_window_is_applied_once(
    tmp_path: Path, first: str
) -> None:
    """Executed, so every application would leave a trace: an amendment,
    a fan-out, a version bump. Exactly one of each."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        _mark_entry(db_path, "acct-1", "DONE")
        if first == "event":
            assert await client.emit_edit(A, SIGNAL_ID_MSG, SL_EDIT, edit_date=EDIT_1)
            await _round(listener, time)
        else:
            with client.blackout():
                await client.emit_edit(A, SIGNAL_ID_MSG, SL_EDIT, edit_date=EDIT_1)
            await _round(listener, time)
            # ...and the same update, delivered late.
            (current,) = await client.get_messages(A, ids=[SIGNAL_ID_MSG])
            late = FakeMessageEditedEvent(chat_id=A, message=current, client=client)
            assert await client.dispatch(late, filter_type=events.MessageEdited)
        await _round(listener, time)
        return listener.handler_error_count

    assert _run(db_path, body) == 0

    assert _signal(db_path)["version"] == 2
    assert [a[0] for a in _amendments(db_path)] == ["amend_sl"]
    assert len(_outbox(db_path, "FOLLOWUP")) == N_ACCOUNTS
    assert _metric(db_path, edits_repo.EDIT_MISSED_BY_EVENTSTREAM_METRIC) == (
        1 if first == "window" else 0
    )


def test_a_late_event_for_an_older_edit_does_not_undo_a_newer_one(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        with client.blackout():
            await client.emit_edit(A, SIGNAL_ID_MSG, SL_EDIT, edit_date=EDIT_1)
            await client.emit_edit(A, SIGNAL_ID_MSG, SL_EDIT.replace("4460", "4455"), edit_date=EDIT_2)
        await _round(listener, time)
        older = FakeMessage(id=SIGNAL_ID_MSG, message=SL_EDIT, date=POSTED, edit_date=EDIT_1)
        stale = FakeMessageEditedEvent(chat_id=A, message=older, client=client)
        assert await client.dispatch(stale, filter_type=events.MessageEdited)

    _run(db_path, body)

    signal = _signal(db_path)
    assert (signal["sl"], signal["version"]) == (4455.0, 2)
    assert _message(db_path)["edit_date_utc"] == EDIT_2.isoformat()


def test_an_edit_found_by_the_forward_read_goes_through_the_same_policy(tmp_path: Path) -> None:
    """A signal stored live, above the pointer, then flipped during a
    blackout: the poll's forward read -- not the window -- meets the
    edited version first, and it must still invalidate, not quietly
    re-parse (the pre-Task-10 `ingest()` just bumped the version)."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        assert await client.emit(A, _msg(SIGNAL_ID_MSG, SIGNAL_TEXT))  # stored live, pointer below
        with client.blackout():
            await client.emit_edit(A, SIGNAL_ID_MSG, DIRECTION_EDIT, edit_date=EDIT_1)
        await _round(listener, time)

    _run(db_path, body)

    assert _signal(db_path)["state"] == "INVALID"
    assert _alerts(db_path) == [("CRITICAL", edits_repo.SIGNAL_INVALIDATED_BY_EDIT)]
    assert _pointer(db_path) == SIGNAL_ID_MSG


def test_an_edit_of_a_message_never_stored_creates_nothing(tmp_path: Path) -> None:
    """A NewMessage dropped in a blackout, then its edit delivered: the
    edit path never creates a message (and so never an ENTRY). The poll's
    forward read stores it, as it would any dropped message."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        with client.blackout():
            await client.emit(A, _msg(SIGNAL_ID_MSG, SIGNAL_TEXT))
        assert await client.emit_edit(A, SIGNAL_ID_MSG, SL_EDIT, edit_date=EDIT_1)
        after_edit = _rows(db_path, "SELECT COUNT(*) FROM messages")[0][0]
        await _round(listener, time)
        return after_edit, listener.handler_error_count

    assert _run(db_path, body) == (0, 0)

    assert _signal(db_path)["sl"] == 4460.0 and _signal(db_path)["version"] == 1
    assert len(_outbox(db_path, "ENTRY")) == N_ACCOUNTS


def test_the_edit_handler_makes_no_rpc_and_ignores_unwatched_chats(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        client.add_to_history(UNWATCHED, _msg(1, "x"))
        before = list(client.calls)
        assert await client.emit_edit(A, SIGNAL_ID_MSG, SL_EDIT, edit_date=EDIT_1)
        assert await client.emit_edit(UNWATCHED, 1, "y", edit_date=EDIT_1)
        return client.calls[len(before):], listener.handler_error_count

    new_calls, errors = _run(db_path, body)

    assert new_calls == []
    assert errors == 0
    assert _signal(db_path)["version"] == 2


def test_an_edit_handler_failure_is_recorded_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "store.db"

    def boom(*args, **kwargs):
        raise RuntimeError("edit policy blew up")

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        monkeypatch.setattr(edits_repo, "apply_edit", boom)
        assert await client.emit_edit(A, SIGNAL_ID_MSG, SL_EDIT, edit_date=EDIT_1)
        return [(e.message_id, e.source) for e in listener.handler_errors]

    assert _run(db_path, body) == [(SIGNAL_ID_MSG, "live")]


# -- deletions ---------------------------------------------------------------------


def test_deleting_an_unexecuted_signal_via_the_event_voids_it(tmp_path: Path) -> None:
    """The event names its ids explicitly, so it acts at once -- no
    second read, no circuit breaker (they guard inferences only)."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        assert await client.emit_delete(A, SIGNAL_ID_MSG)
        return listener.handler_error_count

    assert _run(db_path, body) == 0

    assert _signal(db_path)["state"] == "VOIDED"
    assert _withdrawn(db_path)
    assert _outbox(db_path, "ENTRY") == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]
    message = _message(db_path)
    assert (message["deleted_at_utc"], message["detected_by"]) == (NOW.isoformat(), "event")
    assert _alerts(db_path) == [("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION)]


def test_a_deletion_the_event_stream_dropped_is_found_by_absence(tmp_path: Path) -> None:
    """The case absence detection exists for: the MessageDeleted event
    never fires (a blackout), and only the window re-read -- a `None`
    where the message used to be -- can see the deletion. The first read
    that misses it only makes it pending; the second read confirms it,
    and only then is anything voided."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal_among_chatter(client, listener, time)
        with client.blackout():
            assert not await client.emit_delete(A, SIGNAL_ID_MSG)
            await _round(listener, time)
            after_one_read = (_signal(db_path)["state"], _message(db_path)["deleted_at_utc"])
            await _round(listener, time)
        return after_one_read

    assert _run(db_path, body) == ("NEW", None)

    assert _signal(db_path)["state"] == "VOIDED"
    assert _withdrawn(db_path)
    assert _message(db_path)["detected_by"] == "window"
    assert _outbox(db_path, "ENTRY") == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]
    assert _metric(db_path, edits_repo.DELETE_MISSED_BY_EVENTSTREAM_METRIC) == 1
    assert _alerts(db_path) == [
        ("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION),
        ("CRITICAL", edits_repo.WINDOW_VOIDED_SIGNALS),
    ]


@pytest.mark.parametrize("path", ["event", "window"])
@pytest.mark.parametrize("entry_state", ["DONE", "CLAIMED"], ids=["executed", "in-flight"])
def test_deleting_an_executed_signal_flags_and_alerts_and_closes_nothing(
    tmp_path: Path, path: str, entry_state: str
) -> None:
    """A deletion can be cosmetic (a repost, a tidy-up), so a live trade
    is never closed on one: the deletion is marked on the message, the
    signal is marked withdrawn, a CRITICAL alert names it, it stays NEW so
    its legs are still managed, and only entries not yet taken are
    skipped."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal_among_chatter(client, listener, time)
        _mark_entry(db_path, "acct-1", entry_state)
        if path == "event":
            assert await client.emit_delete(A, SIGNAL_ID_MSG)
        else:
            with client.blackout():
                await client.emit_delete(A, SIGNAL_ID_MSG)
            await _round(listener, time)
            await _round(listener, time)
        await _round(listener, time)  # and again: not re-alerted

    _run(db_path, body)

    signal = _signal(db_path)
    assert signal["state"] == "NEW"
    assert _withdrawn(db_path)
    assert _alerts(db_path) == [("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED)]
    assert _rows(db_path, "SELECT signal_id FROM alerts") == [(signal["signal_id"],)]
    assert _outbox(db_path, "ENTRY") == [("acct-1", entry_state), ("acct-2", "SKIPPED")]
    assert _outbox(db_path, "FOLLOWUP") == []  # no close, no cancel: nothing queued at all
    assert _rows(db_path, "SELECT COUNT(*) FROM followups")[0][0] == 0
    assert _message(db_path)["detected_by"] == path


@pytest.mark.parametrize("first", ["event", "window"])
def test_a_deletion_seen_by_both_the_event_and_the_window_is_processed_once(
    tmp_path: Path, first: str
) -> None:
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal_among_chatter(client, listener, time)
        _mark_entry(db_path, "acct-1", "DONE")
        if first == "event":
            assert await client.emit_delete(A, SIGNAL_ID_MSG)
            calls_before = len(client.get_messages_calls)
            await _round(listener, time)
            window_ids = [c.ids for c in client.get_messages_calls[calls_before:] if c.ids]
        else:
            with client.blackout():
                await client.emit_delete(A, SIGNAL_ID_MSG)
            await _round(listener, time)
            await _round(listener, time)
            window_ids = []
            late = FakeMessageDeletedEvent(chat_id=A, deleted_ids=(SIGNAL_ID_MSG,), client=client)
            assert await client.dispatch(late, filter_type=events.MessageDeleted)
            calls_before = len(client.get_messages_calls)
            await _round(listener, time)
            window_ids = [c.ids for c in client.get_messages_calls[calls_before:] if c.ids]
        return window_ids, listener.handler_error_count

    window_ids, errors = _run(db_path, body)

    assert errors == 0
    assert _alerts(db_path) == [("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED)]
    assert _rows(db_path, "SELECT detail LIKE ? FROM alerts", (f"%[found by {first}]",)) == [(1,)]
    # Telegram's explicit word is final: a window-inferred mark the event
    # then confirms becomes an event mark, and the message leaves the
    # window -- in both orders it is not asked for again.
    assert _message(db_path)["detected_by"] == "event"
    assert window_ids and all(SIGNAL_ID_MSG not in ids for ids in window_ids)


def test_deleting_a_follow_up_withdraws_its_queued_instruction(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        assert await client.emit(A, _msg(SIGNAL_ID_MSG + 1, FOLLOWUP_TEXT))
        assert _outbox(db_path, "FOLLOWUP") == [("acct-1", "QUEUED"), ("acct-2", "QUEUED")]
        assert await client.emit_delete(A, SIGNAL_ID_MSG + 1)

    _run(db_path, body)

    assert _outbox(db_path, "FOLLOWUP") == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]
    assert _alerts(db_path) == [("WARNING", edits_repo.FOLLOWUP_DELETED)]
    assert _signal(db_path)["state"] == "NEW"


def test_deleting_chatter_is_only_marked(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        assert await client.emit(A, _msg(SIGNAL_ID_MSG, CHATTER_TEXT))
        assert await client.emit_delete(A, SIGNAL_ID_MSG)

    _run(db_path, body)

    assert _message(db_path)["deleted_at_utc"] == NOW.isoformat()
    assert _alerts(db_path) == []


# -- absence is evidence, not proof (Task 10 review) --------------------------------


def _keep_only_last(result: list) -> list:
    out = [None] * len(result)
    out[-1] = result[-1]
    return out


@pytest.mark.parametrize(
    ("answer", "n_chatter"),
    [
        (lambda result: [None] * len(result), 3),
        (lambda result: [], 3),
        (_keep_only_last, 45),
    ],
    ids=["all-none", "empty-list", "49-of-50-none"],
)
def test_a_suspect_window_read_voids_nothing_and_raises_one_critical_alert(
    tmp_path: Path, answer, n_chatter: int
) -> None:
    """Telethon turns `MessageIdsEmptyError` into `None` for every id
    (messages.py:309-318); Telegram may omit ids altogether. Before the
    circuit breaker, one such answer voided every unexecuted signal in the
    window. Three bad reads in a row: nothing voided, nothing marked, no
    entry skipped, one CRITICAL alert -- and an edit of a message that did
    come back is still applied."""
    db_path = tmp_path / "store.db"
    client = ScriptedClient(history={A: _signals_and_chatter(5, n_chatter)})
    last = SIGNAL_ID_MSG + 5 + n_chatter - 1

    async def body(client, listener, time):
        await _round(listener, time)  # backfill stored all of it; one clean window read
        with client.blackout():
            await client.emit_edit(A, last, "chatter, edited", edit_date=EDIT_1)
        client.ids_answer = answer
        for _ in range(3):
            await _round(listener, time)
        during = _message(db_path, last)["raw_text"]
        client.ids_answer = None
        await _round(listener, time)  # a good read: all there
        return during

    during = _run(db_path, body, client=client)

    assert _rows(db_path, "SELECT DISTINCT state FROM signals") == [("NEW",)]
    assert _rows(db_path, "SELECT COUNT(*) FROM signals WHERE withdrawn_at_utc IS NOT NULL") == [(0,)]
    assert _rows(db_path, "SELECT COUNT(*) FROM messages WHERE deleted_at_utc IS NOT NULL") == [(0,)]
    assert _rows(db_path, "SELECT DISTINCT state FROM outbox WHERE kind = 'ENTRY'") == [("QUEUED",)]
    assert _alerts(db_path) == [("CRITICAL", edits_repo.WINDOW_READ_SUSPECT)]
    returned = n_chatter == 45
    assert during == ("chatter, edited" if returned else f"chatter number {n_chatter - 1}")
    assert _message(db_path, last)["raw_text"] == "chatter, edited"


def test_a_stray_none_is_only_pending_and_forgotten_once_the_message_is_back(
    tmp_path: Path,
) -> None:
    """One `None` for a message that exists: pending, not deleted; the
    next read returns it and it is forgotten. A second stray `None` later,
    not consecutive with the first, is pending again -- still no void."""
    db_path = tmp_path / "store.db"
    client = ScriptedClient()

    async def body(client, listener, time):
        await _stored_signal_among_chatter(client, listener, time)
        for _ in range(2):
            client.ids_answers = [_without(SIGNAL_ID_MSG)]
            await _round(listener, time)  # missing: pending
            await _round(listener, time)  # back: forgotten

    _run(db_path, body, client=client)

    assert _signal(db_path)["state"] == "NEW"
    assert not _withdrawn(db_path)
    assert _message(db_path)["deleted_at_utc"] is None
    assert _outbox(db_path, "ENTRY") == [("acct-1", "QUEUED"), ("acct-2", "QUEUED")]
    assert _alerts(db_path) == []


def test_a_window_read_that_voids_signals_raises_one_critical_summary(tmp_path: Path) -> None:
    """Each void has its own WARNING; a window read that voids anything
    also raises one CRITICAL naming how many and which, so a mass void
    cannot pass as routine noise."""
    db_path = tmp_path / "store.db"
    client = ScriptedClient(history={A: _signals_and_chatter(2, 4)})

    async def body(client, listener, time):
        await _round(listener, time)
        with client.blackout():
            await client.emit_delete(A, SIGNAL_ID_MSG, SIGNAL_ID_MSG + 1)
            await _round(listener, time)
            await _round(listener, time)

    _run(db_path, body, client=client)

    signal_ids = [row[0] for row in _rows(db_path, "SELECT signal_id FROM signals ORDER BY 1")]
    assert _rows(db_path, "SELECT DISTINCT state FROM signals") == [("VOIDED",)]
    assert _alerts(db_path) == [
        ("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION),
        ("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION),
        ("CRITICAL", edits_repo.WINDOW_VOIDED_SIGNALS),
    ]
    (detail,) = _rows(db_path, "SELECT detail FROM alerts WHERE level = 'CRITICAL'")[0]
    assert "2 signal(s)" in detail and str(signal_ids) in detail


def test_a_false_absence_void_is_reported_when_the_message_reads_back(tmp_path: Path) -> None:
    """Two faulty reads in a row get past both guards and void a signal
    that exists. Its mark stays in the window, so the next read that
    returns it clears the mark and raises CRITICAL DELETION_REVERSED. The
    void is not undone automatically (the alert says so), and it is
    reported once."""
    db_path = tmp_path / "store.db"
    client = ScriptedClient()

    async def body(client, listener, time):
        await _stored_signal_among_chatter(client, listener, time)
        client.ids_answers = [_without(SIGNAL_ID_MSG), _without(SIGNAL_ID_MSG)]
        await _round(listener, time)
        await _round(listener, time)
        voided = (_signal(db_path)["state"], _message(db_path)["detected_by"])
        await _round(listener, time)  # it reads back
        await _round(listener, time)  # and again: nothing more
        return voided

    assert _run(db_path, body, client=client) == ("VOIDED", "window")

    assert _message(db_path)["deleted_at_utc"] is None
    assert _signal(db_path)["state"] == "VOIDED"
    assert _alerts(db_path) == [
        ("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION),
        ("CRITICAL", edits_repo.WINDOW_VOIDED_SIGNALS),
        ("CRITICAL", edits_repo.DELETION_REVERSED),
    ]


@pytest.mark.parametrize("path", ["event", "window"])
def test_an_sl_edit_after_an_absence_mark_on_an_executed_signal_still_amends(
    tmp_path: Path, path: str
) -> None:
    """The reviewer's probe 7: a false absence mark on an executed signal
    used to make every later edit of it 'already deleted' -- the live
    position kept the wrong stop and nothing said so. Now the edit is the
    proof the message exists: the mark is reversed (CRITICAL) and the SL
    change gets its AMEND_SL, fanned out."""
    db_path = tmp_path / "store.db"
    client = ScriptedClient()

    async def body(client, listener, time):
        await _stored_signal_among_chatter(client, listener, time)
        _mark_entry(db_path, "acct-1", "DONE")
        client.ids_answers = [_without(SIGNAL_ID_MSG), _without(SIGNAL_ID_MSG)]
        await _round(listener, time)
        await _round(listener, time)
        marked = _message(db_path)["detected_by"]
        if path == "event":
            assert await client.emit_edit(A, SIGNAL_ID_MSG, SL_EDIT, edit_date=EDIT_1)
        else:
            with client.blackout():
                await client.emit_edit(A, SIGNAL_ID_MSG, SL_EDIT, edit_date=EDIT_1)
            await _round(listener, time)
        return marked

    assert _run(db_path, body, client=client) == "window"

    signal = _signal(db_path)
    assert (signal["state"], signal["sl"], signal["version"]) == ("NEW", 4460.0, 2)
    assert [a[:2] for a in _amendments(db_path)] == [("amend_sl", 4460.0)]
    assert _outbox(db_path, "FOLLOWUP") == [("acct-1", "QUEUED"), ("acct-2", "QUEUED")]
    assert _message(db_path)["deleted_at_utc"] is None
    assert _alerts(db_path) == [
        ("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED),
        ("CRITICAL", edits_repo.DELETION_REVERSED),
    ]


@pytest.mark.parametrize("dies_by", ["direction-edit", "deletion"])
def test_a_claimed_entry_of_a_dead_signal_is_never_handed_out_again(
    tmp_path: Path, dies_by: str
) -> None:
    """The reviewer's probe 3: acct-1's worker claims the ENTRY, the
    poster re-aims or deletes the signal while it holds it, the worker
    dies, the lease expires. The sweep must not re-queue it and the next
    claim must not hand it out -- for a deletion in flight the signal
    stays NEW (it may be live), so only the withdrawal mark can say so."""
    from xaucopier.store import outbox

    db_path = tmp_path / "store.db"

    def claim(now):
        conn = db.connect(db_path, role="writer")
        try:
            return outbox.claim(conn, account_id="acct-1", pid=4242, now_utc=now)
        finally:
            conn.close()

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        assert claim(NOW) is not None
        if dies_by == "direction-edit":
            assert await client.emit_edit(A, SIGNAL_ID_MSG, DIRECTION_EDIT, edit_date=EDIT_1)
        else:
            assert await client.emit_delete(A, SIGNAL_ID_MSG)

    _run(db_path, body)

    later = NOW + timedelta(days=1)
    conn = db.connect(db_path, role="writer")
    try:
        reclaimed = outbox.sweep_expired(conn, now_utc=later)
    finally:
        conn.close()
    assert reclaimed == 0
    assert claim(later) is None
    assert _outbox(db_path, "ENTRY") == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]
    if dies_by == "deletion":
        assert _signal(db_path)["state"] == "NEW" and _withdrawn(db_path)


@pytest.mark.parametrize("second_by", ["event", "window"])
def test_two_edits_inside_one_second_are_both_applied(tmp_path: Path, second_by: str) -> None:
    """Telegram's edit_date has one-second resolution. A second edit in
    the same second shares the first's edit_date; treating that as 'not
    newer' dropped it for good."""
    db_path = tmp_path / "store.db"
    second = SL_EDIT.replace("4460", "4455")

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        assert await client.emit_edit(A, SIGNAL_ID_MSG, SL_EDIT, edit_date=EDIT_1)
        if second_by == "event":
            assert await client.emit_edit(A, SIGNAL_ID_MSG, second, edit_date=EDIT_1)
        else:
            with client.blackout():
                await client.emit_edit(A, SIGNAL_ID_MSG, second, edit_date=EDIT_1)
        await _round(listener, time)
        await _round(listener, time)  # nothing more to do

    _run(db_path, body)

    signal = _signal(db_path)
    assert (signal["sl"], signal["version"]) == (4455.0, 3)
    assert _message(db_path)["raw_text"] == second


def test_the_deletion_handler_makes_no_rpc_and_ignores_what_it_cannot_route(
    tmp_path: Path,
) -> None:
    """`MessageDeleted` outside a channel carries no chat at all
    (Telethon: `UpdateDeleteMessages`, `peer=None`), so it is dropped, as
    is one from a chat nobody watches."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        before = list(client.calls)
        no_chat = FakeMessageDeletedEvent(chat_id=None, deleted_ids=(SIGNAL_ID_MSG,), client=client)
        await client.dispatch(no_chat, filter_type=events.MessageDeleted)
        other = FakeMessageDeletedEvent(chat_id=UNWATCHED, deleted_ids=(SIGNAL_ID_MSG,), client=client)
        await client.dispatch(other, filter_type=events.MessageDeleted)
        state_after_unroutable = _signal(db_path)["state"]
        assert await client.emit_delete(A, SIGNAL_ID_MSG)
        return client.calls[len(before):], state_after_unroutable, listener.handler_error_count

    new_calls, state_after_unroutable, errors = _run(db_path, body)

    assert new_calls == []
    assert state_after_unroutable == "NEW"
    assert errors == 0
    assert _signal(db_path)["state"] == "VOIDED"


# -- the window re-read ------------------------------------------------------------


def test_the_window_re_read_never_moves_the_pointer(tmp_path: Path) -> None:
    """The D1 shape again. 4002 is hidden by a blackout; 4003 arrives
    live and is stored *above* the pointer. A window re-read that
    advanced the pointer would carry it over 4002, which no later read
    could then see. The window re-reads 4003 too -- the newest stored
    messages, whatever the pointer (final review F03) -- but writes
    nothing to the pointer."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        with client.blackout():
            await client.emit(A, _msg(SIGNAL_ID_MSG + 1, CHATTER_TEXT))
        assert await client.emit(A, _msg(SIGNAL_ID_MSG + 2, CHATTER_TEXT))
        calls_before = len(client.get_messages_calls)
        result = await reconcile_window(
            listener, CHANNEL, window=10, rpc_timeout=5.0, absences=edits.AbsenceTracker()
        )
        pointer_after_window = _pointer(db_path)
        requested = [c.ids for c in client.get_messages_calls[calls_before:]]
        await _round(listener, time)
        return result, pointer_after_window, requested

    result, pointer_after_window, requested = _run(db_path, body)

    assert pointer_after_window == SIGNAL_ID_MSG
    assert requested == [(SIGNAL_ID_MSG, SIGNAL_ID_MSG + 2)]
    assert result.requested == (SIGNAL_ID_MSG, SIGNAL_ID_MSG + 2)
    assert result.pointer == SIGNAL_ID_MSG
    # ...so the next forward read still recovers the blacked-out message.
    assert _rows(db_path, "SELECT message_id, source FROM messages ORDER BY message_id") == [
        (SIGNAL_ID_MSG, "live"),
        (SIGNAL_ID_MSG + 1, "poll"),
        (SIGNAL_ID_MSG + 2, "live"),
    ]
    assert _pointer(db_path) == SIGNAL_ID_MSG + 2


def test_the_window_leaves_a_pointer_on_an_unstored_id_where_it_is(tmp_path: Path) -> None:
    """The pointer can rest on an id that was never stored -- the
    first-run seed's anchor, or a message skipped after three failed
    reads. The window then reads only older ids; it must not pull the
    pointer back to the newest of them (the next forward read would
    re-read everything in between) any more than push it forward."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        # Stored live below the pointer (4000), which itself is not stored.
        assert await client.emit(A, _msg(SIGNAL_ID_MSG - 2, CHATTER_TEXT))
        assert await client.emit(A, _msg(SIGNAL_ID_MSG - 3, CHATTER_TEXT))
        with client.blackout():
            await client.emit_edit(A, SIGNAL_ID_MSG - 2, "Good evening", edit_date=EDIT_1)
        result = await reconcile_window(
            listener, CHANNEL, window=10, rpc_timeout=5.0, absences=edits.AbsenceTracker()
        )
        pointer_after_window = _pointer(db_path)
        await _round(listener, time)
        return result, pointer_after_window

    result, pointer_after_window = _run(db_path, body)

    assert result.requested == (SIGNAL_ID_MSG - 3, SIGNAL_ID_MSG - 2)
    assert [e.outcome for e in result.edits] == ["recorded"]  # the window did its work...
    assert pointer_after_window == SIGNAL_ID_MSG - 1  # ...and left the pointer alone
    assert _pointer(db_path) == SIGNAL_ID_MSG - 1


def test_the_window_is_the_newest_n_stored_messages(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        for offset in range(6):  # 4001..4006
            assert await client.emit(A, _msg(SIGNAL_ID_MSG + offset, CHATTER_TEXT))
        calls_before = len(client.get_messages_calls)
        await _round(listener, time)  # forward read to 4006, then the window
        return [c.ids for c in client.get_messages_calls[calls_before:] if c.ids is not None]

    requested = _run(db_path, body, edit_window=3)

    assert requested == [(SIGNAL_ID_MSG + 3, SIGNAL_ID_MSG + 4, SIGNAL_ID_MSG + 5)]


def test_the_window_matches_what_it_read_by_id_not_by_position(tmp_path: Path) -> None:
    """Telegram may leave an invalid id out of the answer altogether
    rather than send `MessageEmpty` for it (telethon/client/messages.py:
    324-327), so the answer need not zip with the ids asked for. Matching
    by position would take 4003 for 4002 and 'find' 4003 deleted."""
    db_path = tmp_path / "store.db"
    client = ScriptedClient(ids_answer=lambda result: [m for m in result if m is not None])

    async def body(client, listener, time):
        for offset in range(3):
            assert await client.emit(A, _msg(SIGNAL_ID_MSG + offset, CHATTER_TEXT))
        await _round(listener, time)
        with client.blackout():
            await client.emit_delete(A, SIGNAL_ID_MSG + 1)
        await _round(listener, time)
        await _round(listener, time)  # absence confirmed by a second read

    _run(db_path, body, client=client)

    deleted = _rows(
        db_path, "SELECT message_id FROM messages WHERE deleted_at_utc IS NOT NULL"
    )
    assert deleted == [(SIGNAL_ID_MSG + 1,)]


@pytest.mark.parametrize("trouble", ["hang", ConnectionError("blip")], ids=["hang", "error"])
def test_a_failed_window_read_is_retried_next_round_and_never_stops_the_poll(
    tmp_path: Path, trouble
) -> None:
    db_path = tmp_path / "store.db"
    client = ScriptedClient(ids_failures=[trouble])

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)  # that round's window read fails
        with client.blackout():
            await client.emit_edit(A, SIGNAL_ID_MSG, SL_EDIT, edit_date=EDIT_1)
        await _round(listener, time)
        return listener.poll_task.done()

    poll_stopped = _run(db_path, body, client=client, rpc_timeout=0.05)

    assert poll_stopped is False
    assert (_signal(db_path)["sl"], _signal(db_path)["version"]) == (4460.0, 2)


def test_a_writer_death_met_by_the_window_stops_the_poll(tmp_path: Path) -> None:
    """The one exception the poll does not swallow: with no writer,
    nothing can be stored, and the listener must stop (exit 4)."""
    db_path = tmp_path / "store.db"
    client = ScriptedClient()
    armed = {"on": False}

    def kill(conn):
        raise SystemExit("simulated writer-thread death")

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)

        async def before_ids_read():
            if armed["on"]:
                with pytest.raises(db.WriterDeadError):
                    await asyncio.wrap_future(listener.writer.submit(kill))

        client.before_ids_read = before_ids_read
        with client.blackout():
            await client.emit_edit(A, SIGNAL_ID_MSG, SL_EDIT, edit_date=EDIT_1)
        armed["on"] = True
        time.wake()
        deadline = asyncio.get_running_loop().time() + 2.0
        while not listener.poll_task.done():
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.001)
        return listener.poll_task.exception()

    error = _run(db_path, body, client=client)

    assert isinstance(error, db.WriterDeadError)


def test_a_window_message_that_fails_is_recorded_and_the_rest_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "store.db"
    real_apply = edits_repo.apply_edit
    later = "Good evening traders"

    def flaky(conn, *, message_id, **kwargs):
        if message_id == SIGNAL_ID_MSG:
            raise RuntimeError("this one is poison")
        return real_apply(conn, message_id=message_id, **kwargs)

    async def body(client, listener, time):
        for offset in range(2):
            assert await client.emit(A, _msg(SIGNAL_ID_MSG + offset, CHATTER_TEXT))
        await _round(listener, time)
        monkeypatch.setattr(edits_repo, "apply_edit", flaky)
        with client.blackout():
            for offset in range(2):
                await client.emit_edit(A, SIGNAL_ID_MSG + offset, later, edit_date=EDIT_1)
        await _round(listener, time)
        return [(e.message_id, e.source) for e in listener.handler_errors]

    errors = _run(db_path, body)

    assert errors == [(SIGNAL_ID_MSG, "window")]
    assert _message(db_path, SIGNAL_ID_MSG + 1)["raw_text"] == later
    assert _message(db_path, SIGNAL_ID_MSG)["raw_text"] == CHATTER_TEXT


@pytest.mark.parametrize("window", [0, -1, True, 1.5, edits.MAX_EDIT_WINDOW + 1])
def test_a_nonsense_window_is_refused(window) -> None:
    with pytest.raises(ValueError, match="edit_window"):
        HistoryPoll(edit_window=window)


def test_the_default_window_is_fifty() -> None:
    assert DEFAULT_EDIT_WINDOW == 50
    assert HistoryPoll().edit_window == 50


def test_the_listener_registers_one_handler_per_event_type(tmp_path: Path) -> None:
    """By exact builder type, as Telethon dispatches: an edit reaches
    only the edit handler, a deletion only the deletion handler."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        return [(type(h.event_filter), h.callback.__name__) for h in client.handlers]

    assert _run(db_path, body) == [
        (events.NewMessage, "_on_new_message"),
        (events.MessageEdited, "_on_message_edited"),
        (events.MessageDeleted, "_on_message_deleted"),
    ]


def test_a_flood_wait_on_the_window_never_defers_the_forward_read(tmp_path: Path) -> None:
    """The window's `channels.GetMessages` is its own request type, and
    Telethon records flood waits per type. A flood wait on it defers only
    that channel's window; the forward read -- how a message the update
    stream dropped, a trade, is recovered -- still runs every round. The
    window resumes once the wait has passed."""
    db_path = tmp_path / "store.db"
    client = ScriptedClient(ids_failures=[errors.FloodWaitError(request=None, capture=12)])

    def ids_reads() -> int:
        return sum(1 for c in client.get_messages_calls if c.ids is not None)

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)  # t=1005: the window is told to wait 12 s
        with client.blackout():
            await client.emit(A, _msg(SIGNAL_ID_MSG + 1, CHATTER_TEXT))
        await _round(listener, time)  # t=1010
        recovered = _rows(
            db_path, "SELECT source FROM messages WHERE message_id = ?", (SIGNAL_ID_MSG + 1,)
        )
        reads_while_deferred = ids_reads()
        await _round(listener, time)  # t=1015: still deferred
        await _round(listener, time)  # t=1020: 12 s have passed
        return recovered, reads_while_deferred, ids_reads()

    recovered, reads_while_deferred, reads_after = _run(db_path, body, client=client)

    assert recovered == [("poll",)]
    assert reads_while_deferred == 0
    assert reads_after == 1
