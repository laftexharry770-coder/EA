"""The window re-read's guards and reach (spec 8.3; Plan 2 Task 10, final
review F03, F18, ledger 164/165, group-3 leftover G3L-2).

- **The circuit breaker** (`edits.is_suspect`) at its boundaries: a read
  missing every message it expected, or at least `SUSPECT_MIN_ABSENT` of
  them and more than half, is not believed (ledger 164).
- **`AbsenceTracker.seen`**: a message a suspect read returned is no
  longer pending, so the next read that misses it starts the two-read
  count again (ledger 165).

Test helpers come from tests/test_edits.py, whose harness runs the real
listener, poll and store against the fake Telegram client.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

import pytest

import test_relay as relay

from fakes.telegram import FakeMessage, FakeTelegramClient
from test_edits import (
    ACCOUNTS,
    CHANNEL,
    CHATTER_TEXT,
    EDIT_1,
    POSTED,
    SL_EDIT,
    SIGNAL_ID_MSG,
    SIGNAL_TEXT,
    A,
    ScriptedClient,
    _alerts,
    _message,
    _metric,
    _outbox,
    _round,
    _rows,
    _run,
    _signal,
    _withdrawn,
)
from xaucopier.ingest.edits import SUSPECT_MIN_ABSENT, AbsenceTracker, is_suspect
from xaucopier.ingest.listener import DeletedBeforeStored
from xaucopier.store import db, edits_repo
from xaucopier.store.messages_repo import ingest

# -- the circuit breaker (ledger 164) --------------------------------------------


@pytest.mark.parametrize(
    ("absent", "expected", "suspect"),
    [
        # Nothing expected, or nothing missing: nothing to doubt.
        (0, 0, False),
        (0, 1, False),
        (0, 50, False),
        # Every expected message missing: never believed -- in a window of
        # one or two messages too, where the count rule alone would let an
        # all-None answer void them.
        (1, 1, True),
        (2, 2, True),
        (3, 3, True),
        (50, 50, True),
        # One or two missing in a small window, not all of it: believed.
        (1, 2, False),
        (2, 3, False),
        (2, 4, False),
        # Exactly SUSPECT_MIN_ABSENT (3) missing: suspect only above half.
        (3, 5, True),
        (3, 6, False),
        (3, 7, False),
        # Half is not "more than half".
        (4, 8, False),
        (5, 8, True),
        (25, 50, False),
        (26, 50, True),
    ],
)
def test_the_circuit_breaker_boundaries(absent: int, expected: int, suspect: bool) -> None:
    assert is_suspect(absent, expected) is suspect


def test_the_breaker_minimum_is_three() -> None:
    assert SUSPECT_MIN_ABSENT == 3


# -- AbsenceTracker.seen (ledger 165) ----------------------------------------------


def test_seen_forgets_a_pending_id_and_only_that_one() -> None:
    tracker = AbsenceTracker()
    assert tracker.settle(A, {1, 2, 3}) == frozenset()
    tracker.seen(A, [2, 99])
    assert tracker.pending(A) == frozenset({1, 3})
    # Another channel is not touched.
    tracker.settle(A + 1, {2})
    tracker.seen(A, [1])
    assert tracker.pending(A) == frozenset({3})
    assert tracker.pending(A + 1) == frozenset({2})


def _four_signals_and_chatter() -> list[FakeMessage]:
    """A window of eight: the signal first, then seven chatter posts."""
    messages = [FakeMessage(id=SIGNAL_ID_MSG, message=SIGNAL_TEXT, date=POSTED)]
    messages += [
        FakeMessage(id=SIGNAL_ID_MSG + n, message=f"{CHATTER_TEXT} {n}", date=POSTED)
        for n in range(1, 8)
    ]
    return messages


def _only(*message_ids: int):
    """A by-id answer that returns only these ids (the rest None)."""

    def answer(result: list) -> list:
        return [m if m is not None and m.id in message_ids else None for m in result]

    return answer


def _without_signal(result: list) -> list:
    return [None if m is not None and m.id == SIGNAL_ID_MSG else m for m in result]


def test_a_message_a_suspect_read_returned_is_no_longer_pending(tmp_path) -> None:
    """Read 1 (believed) misses the signal: pending. Read 2 is suspect --
    most of the window missing -- but it DID return the signal. Read 3
    misses the signal once more. Without `seen()`, read 3 would confirm a
    deletion from two reads that were not in a row (the signal came back in
    between) and void a signal that exists; with it, the signal is only
    pending again."""
    db_path = tmp_path / "store.db"
    client = ScriptedClient(history={A: _four_signals_and_chatter()})

    async def body(client, listener, time):
        await _round(listener, time)  # stored, pointer on the newest; one clean read
        client.ids_answers = [
            _without_signal,
            _only(SIGNAL_ID_MSG, SIGNAL_ID_MSG + 1, SIGNAL_ID_MSG + 2),  # 5 of 8 missing
            _without_signal,
        ]
        for _ in range(3):
            await _round(listener, time)

    _run(db_path, body, client=client)

    assert _signal(db_path)["state"] == "NEW"
    assert not _withdrawn(db_path)
    assert _message(db_path)["deleted_at_utc"] is None
    assert _outbox(db_path, "ENTRY") == [("acct-1", "QUEUED"), ("acct-2", "QUEUED")]
    assert [code for _, code in _alerts(db_path)] == [edits_repo.WINDOW_READ_SUSPECT]


# -- the window covers the newest stored messages, pointer or not (F03) ------------

CHATTER_BEFORE = (SIGNAL_ID_MSG, SIGNAL_ID_MSG + 1, SIGNAL_ID_MSG + 2)
NEWEST_SIGNAL = SIGNAL_ID_MSG + 3


def test_a_newest_signal_deleted_before_the_pointer_reaches_it_is_found_by_the_window(
    tmp_path,
) -> None:
    """Final review F03. Only history reads move the pointer, so a signal
    delivered live sits above it until the next forward read passes it --
    and a forward read never passes a message that is gone. Deleted within
    that gap, with the `MessageDeleted` update dropped, it used to stay
    claimable until the channel posted again (hours, maybe). The window
    now takes the newest stored messages whatever the pointer, so the
    deletion is found in the normal two rounds -- and the pointer is still
    never written by it."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        for message_id in CHATTER_BEFORE:
            assert await client.emit(A, FakeMessage(id=message_id, message=CHATTER_TEXT, date=POSTED))
        await _round(listener, time)
        assert listener.read_last_processed_message_id(A) == CHATTER_BEFORE[-1]
        assert await client.emit(A, FakeMessage(id=NEWEST_SIGNAL, message=SIGNAL_TEXT, date=POSTED))
        with client.blackout():
            assert not await client.emit_delete(A, NEWEST_SIGNAL)
        await _round(listener, time)  # missing once: pending
        pending = _signal(db_path, NEWEST_SIGNAL)["state"]
        await _round(listener, time)  # missing twice: deleted
        return pending, listener.read_last_processed_message_id(A)

    pending, pointer = _run(db_path, body)

    assert pending == "NEW"
    assert pointer == CHATTER_BEFORE[-1]  # the window never moves it
    assert _signal(db_path, NEWEST_SIGNAL)["state"] == "VOIDED"
    assert _withdrawn(db_path, NEWEST_SIGNAL)
    message = _message(db_path, NEWEST_SIGNAL)
    assert message["deleted_at_utc"] is not None and message["detected_by"] == "window"
    assert _outbox(db_path, "ENTRY") == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]
    assert _metric(db_path, edits_repo.DELETE_MISSED_BY_EVENTSTREAM_METRIC) == 1
    assert [code for _, code in _alerts(db_path)] == [
        edits_repo.SIGNAL_VOIDED_BY_DELETION,
        edits_repo.WINDOW_VOIDED_SIGNALS,
    ]


def test_an_edit_of_a_message_above_the_pointer_is_applied_once(tmp_path) -> None:
    """The window now also re-reads a live-stored message above the
    pointer; the forward read then meets the same version when it passes
    it. The edit is applied once, and counted once."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        for message_id in CHATTER_BEFORE:
            assert await client.emit(A, FakeMessage(id=message_id, message=CHATTER_TEXT, date=POSTED))
        await _round(listener, time)
        assert await client.emit(A, FakeMessage(id=NEWEST_SIGNAL, message=SIGNAL_TEXT, date=POSTED))
        with client.blackout():
            await client.emit_edit(A, NEWEST_SIGNAL, SL_EDIT, edit_date=EDIT_1)
        await _round(listener, time)
        await _round(listener, time)

    _run(db_path, body)

    signal = _signal(db_path, NEWEST_SIGNAL)
    assert (signal["state"], signal["version"], signal["sl"]) == ("NEW", 2, 4460.0)
    assert _metric(db_path, edits_repo.EDIT_MISSED_BY_EVENTSTREAM_METRIC) == 1
    assert _alerts(db_path) == []


# -- a window deletion that promoted a repost is no mass void (G3L-2) --------------


@pytest.mark.parametrize("order", ["vip-first", "relay-first"])
def test_a_window_deletion_of_a_double_posted_copy_raises_no_void_summary(
    tmp_path, order: str
) -> None:
    """Group-3 leftover G3L-2 (review round 3, NEW-6). The VIP posts a
    call; the relay posts it twice and removes one of its two posts, the
    deletion found only by the window. VIP-first, the relay's first post is
    a copy: removing it is a repeat removal (a WARNING). Relay-first, it
    holds the signal: the signal is voided and the relay's standing repost
    promoted in its place -- the call never stopped. The CRITICAL
    `WINDOW_VOIDED_SIGNALS` ("check they really were deleted") used to
    fire only in the second order; a void whose call is still queued as
    the promoted signal is left out of it, so neither order raises it,
    and the call stays claimable exactly once per account."""
    db_path = tmp_path / "store.db"
    second = relay.COPY + 1

    async def body(client, listener, time):
        vip = relay._post(relay.CALL)
        first = relay._post(relay.COPY, after=timedelta(seconds=30))
        if order == "vip-first":
            assert await client.emit(relay.V, vip)
            assert await client.emit(relay.R, first)
        else:
            assert await client.emit(relay.R, first)
            assert await client.emit(relay.V, vip)
        assert await client.emit(relay.R, relay._post(second, after=timedelta(seconds=60)))
        await _round(listener, time)
        with client.blackout():
            assert not await client.emit_delete(relay.R, relay.COPY)
        await _round(listener, time)
        await _round(listener, time)

    relay._run(db_path, body)

    state = relay._state(db_path)
    codes = relay._codes(state)
    assert ("CRITICAL", edits_repo.WINDOW_VOIDED_SIGNALS) not in codes
    assert [level for level, _ in codes] == ["WARNING"] * len(codes)
    live = [s for s in state["signals"] if s[3] == "NEW" and not s[4]]
    assert len(live) == 1
    assert state["queued"] == [(live[0][0], len(relay.LIVE_ACCOUNTS))]
    if order == "vip-first":
        assert codes == [("WARNING", edits_repo.SIGNAL_REPEAT_REMOVED)]
    else:
        assert codes == [
            ("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION),
            ("WARNING", edits_repo.SIGNAL_REPOST_PROMOTED),
        ]


def test_a_window_void_with_nothing_promoted_is_still_summarised(tmp_path) -> None:
    """The other side of G3L-2: a window deletion that voids a call and
    promotes nothing still raises the CRITICAL summary -- and in a report
    that did both, only the call no post carries any more is named."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        # Two different calls (another SL), each posted once, and chatter.
        assert await client.emit(A, FakeMessage(id=SIGNAL_ID_MSG, message=SIGNAL_TEXT, date=POSTED))
        other = SIGNAL_TEXT.replace("4470", "4465")
        assert await client.emit(A, FakeMessage(id=SIGNAL_ID_MSG + 1, message=other, date=POSTED))
        # ...the first posted again 1 minute later (a double post).
        assert await client.emit(
            A,
            FakeMessage(id=SIGNAL_ID_MSG + 2, message=SIGNAL_TEXT, date=POSTED + timedelta(minutes=1)),
        )
        for n in range(3, 7):
            assert await client.emit(
                A, FakeMessage(id=SIGNAL_ID_MSG + n, message=f"{CHATTER_TEXT} {n}", date=POSTED)
            )
        await _round(listener, time)
        with client.blackout():
            assert not await client.emit_delete(A, SIGNAL_ID_MSG, SIGNAL_ID_MSG + 1)
        await _round(listener, time)
        await _round(listener, time)

    _run(db_path, body)

    (detail,) = _rows(
        db_path, "SELECT detail FROM alerts WHERE code = ?", (edits_repo.WINDOW_VOIDED_SIGNALS,)
    )[0]
    other_signal = _signal(db_path, SIGNAL_ID_MSG + 1)["signal_id"]
    first_signal = _signal(db_path, SIGNAL_ID_MSG)["signal_id"]
    assert "1 signal(s)" in detail and f"[{other_signal}]" in detail
    assert str(first_signal) not in detail.split("signal(s)")[1].split(".")[0]
    assert _signal(db_path, SIGNAL_ID_MSG + 2)["state"] == "NEW"  # promoted, queued


# -- a deletion that arrives before the message is stored (F18) --------------------


@dataclass
class _DeletedDuringForwardRead(FakeTelegramClient):
    """The race of final review F18: the poll's forward read has fetched
    `doomed`, and the `MessageDeleted` update for it is handled before the
    read's ingest job runs -- so when the deletion is applied, the message
    is not stored yet."""

    doomed: int | None = None
    #: The by-id reads the window made, for the single-message case.
    by_id: list[tuple[int, ...]] = field(default_factory=list)

    async def get_messages(self, *args, **kwargs):
        result = await super().get_messages(*args, **kwargs)
        if kwargs.get("ids") is not None:
            self.by_id.append(tuple(kwargs["ids"]))
            return result
        doomed = self.doomed
        if doomed is not None and any(m.id == doomed for m in result):
            self.doomed = None
            assert await self.emit_delete(A, doomed)  # delivered, handled now
        return result


@pytest.mark.parametrize("n_chatter", [3, 0], ids=["window-of-four", "only-message"])
def test_a_deletion_handled_before_the_forward_read_stores_the_message_is_applied(
    tmp_path, n_chatter: int
) -> None:
    """Final review F18. The event found nothing stored ("unknown") and
    was forgotten; the read then stored the message as a new signal with
    claimable entries. With company in the window it healed a round later;
    as a channel's only message (first run, a new channel) every window
    read missed all of it, tripped the breaker, and the signal stayed
    claimable for good under a WINDOW_READ_SUSPECT alert saying deletion
    events still act. Now the event's id is remembered, and the job that
    stores the message applies the deletion in the same transaction --
    exactly as if the event had come a moment later: the signal VOIDED,
    its entries skipped before anyone could claim them, a WARNING, and the
    window never asks for it."""
    db_path = tmp_path / "store.db"
    client = _DeletedDuringForwardRead()

    async def body(client, listener, time):
        with client.blackout():  # history only: the poll will fetch them
            await client.emit(A, FakeMessage(id=SIGNAL_ID_MSG, message=SIGNAL_TEXT, date=POSTED))
            for n in range(1, n_chatter + 1):
                await client.emit(
                    A, FakeMessage(id=SIGNAL_ID_MSG + n, message=f"{CHATTER_TEXT} {n}", date=POSTED)
                )
        client.doomed = SIGNAL_ID_MSG
        client.by_id.clear()
        for _ in range(3):
            await _round(listener, time)
        assert client.doomed is None, "the race never happened"
        return listener.read_last_processed_message_id(A)

    pointer = _run(db_path, body, client=client)

    assert pointer == SIGNAL_ID_MSG + n_chatter
    message = _message(db_path)
    assert message["deleted_at_utc"] is not None and message["detected_by"] == "event"
    assert message["raw_text"] == SIGNAL_TEXT  # still a fixture
    assert _signal(db_path)["state"] == "VOIDED" and _withdrawn(db_path)
    assert _outbox(db_path, "ENTRY") == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]
    assert _alerts(db_path) == [("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION)]
    assert _metric(db_path, edits_repo.DELETE_MISSED_BY_EVENTSTREAM_METRIC) == 0
    assert all(SIGNAL_ID_MSG not in ids for ids in client.by_id)
    if n_chatter == 0:
        assert client.by_id == []  # nothing left to re-read


def test_deleted_before_stored_keeps_only_ids_above_the_pointer_and_forgets_them_once_passed():
    tombstones = DeletedBeforeStored(limit=3)
    at = POSTED
    assert not tombstones.record(A, 4000, deleted_at_utc=at, pointer=4000)  # passed already
    assert not tombstones.pending(A)
    assert tombstones.record(
        A, 4002, deleted_at_utc=at, pointer=4000, also_deleted=frozenset({4002, 4003})
    )
    assert tombstones.deleted_at(A, 4002, pointer=4000) == at
    # The report's other ids go along: the deletion is applied as that report.
    assert tombstones.deletion(A, 4002, pointer=4000) == (at, frozenset({4002, 4003}))
    assert tombstones.deleted_at(A, 4003, pointer=4000) is None
    assert tombstones.deletion(A, 4003, pointer=4000) is None
    assert tombstones.deleted_at(A + 1, 4002, pointer=0) is None  # per channel
    # The pointer passes it: forgotten.
    assert tombstones.deleted_at(A, 4002, pointer=4002) is None
    assert not tombstones.pending(A)
    # Bounded: the lowest ids go first.
    for message_id in (4010, 4011, 4012, 4013):
        tombstones.record(A, message_id, deleted_at_utc=at, pointer=4000)
    assert tombstones.ids(A) == frozenset({4011, 4012, 4013})
    # The first report's time is kept.
    tombstones.record(A, 4013, deleted_at_utc=at + timedelta(minutes=1), pointer=4000)
    assert tombstones.deleted_at(A, 4013, pointer=4000) == at


def _without_times(conn) -> dict[str, list[tuple]]:
    """Every row of every table, minus the columns that hold a time."""
    state = {}
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "AND name NOT IN ('heartbeats', 'schema_version') ORDER BY 1"
    ).fetchall():
        columns = [
            row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            if not row[1].endswith("_utc")
        ]
        state[table] = [
            tuple(row) for row in conn.execute(
                f"SELECT {', '.join(columns)} FROM {table} ORDER BY {', '.join(columns)}"
            ).fetchall()
        ]
    return state


#: The live signal on the channel when the race is run: another call than
#: SIGNAL_TEXT (its own stop).
OTHER_CALL = SIGNAL_TEXT.replace("4470", "4465")

#: What the race is run on: a signal, a repeat of the live one, a
#: follow-up replying to it, an unreadable signal, and chatter -- with the
#: states the raced message's own signals rows end in.
RACED = [
    (SIGNAL_TEXT, "signal", None, ["VOIDED"]),
    (OTHER_CALL, "signal", None, []),  # a copy: linked, no row of its own
    ("Close half now", "followup", 3001, []),
    (SIGNAL_TEXT.replace("SL: 4470", ""), "parse_failed", None, []),
    (CHATTER_TEXT, "skip", None, []),
]


@pytest.mark.parametrize(
    ("text", "kind", "reply_to", "own_states"),
    RACED,
    ids=["signal", "repeat", "followup", "parse_failed", "skip"],
)
def test_a_message_stored_after_its_deletion_event_ends_as_if_the_event_came_after(
    tmp_path, text, kind, reply_to, own_states
) -> None:
    """`ingest(deleted_by_event_at_utc=...)` -- the deletion event handled
    before the message was stored (F18) -- ends exactly as the same event
    handled a moment after it was stored: every row the same but for the
    times. So the race changes nothing: a signal is VOIDED with its
    entries skipped in the same transaction (never claimable), a relay
    rule still sees the post it matched, and the event, not the window,
    is what found it. A second delivery of the message is a duplicate."""
    stored_at = POSTED + timedelta(seconds=5)
    event_at = POSTED + timedelta(seconds=2)

    def store(conn, message_id, text, reply_to=None, **extra):
        result = ingest(
            conn, channel_cfg=CHANNEL, message_id=message_id, text=text,
            posted_at_utc=POSTED, edit_date_utc=None, reply_to_message_id=reply_to,
            source="poll", now_utc=stored_at, accounts=ACCOUNTS, **extra,
        )
        conn.commit()
        return result

    states, results = [], []
    for raced in (True, False):
        conn = db.connect(tmp_path / f"{raced}.db", role="writer")
        try:
            store(conn, 3001, OTHER_CALL)  # a live signal on the channel
            if raced:
                result = store(
                    conn, SIGNAL_ID_MSG, text, reply_to,
                    deleted_by_event_at_utc=event_at,
                    deleted_by_event_with=frozenset({SIGNAL_ID_MSG}),
                )
            else:
                result = store(conn, SIGNAL_ID_MSG, text, reply_to)
                edits_repo.apply_deletion(
                    conn, channel_cfg=CHANNEL, message_id=SIGNAL_ID_MSG,
                    now_utc=stored_at + timedelta(seconds=1), accounts=ACCOUNTS,
                    detected_by="event", also_deleted=frozenset({SIGNAL_ID_MSG}),
                )
                conn.commit()
            again = store(conn, SIGNAL_ID_MSG, text, reply_to)
            assert again.outcome == "duplicate"
            states.append(_without_times(conn))
            results.append((result.outcome, result.kind))
            if raced:
                row = conn.execute(
                    "SELECT deleted_at_utc, deleted_detected_by FROM messages "
                    "WHERE message_id = ?", (SIGNAL_ID_MSG,),
                ).fetchone()
                assert tuple(row) == (event_at.isoformat(), "event")  # the event's own time
                claimable = conn.execute(
                    "SELECT COUNT(*) FROM outbox o JOIN signals s ON s.signal_id = o.signal_id "
                    "WHERE s.message_id = ? AND o.state = 'QUEUED'", (SIGNAL_ID_MSG,),
                ).fetchone()[0]
                assert claimable == 0
        finally:
            conn.close()
    raced_state, reference = states
    assert raced_state == reference
    assert results[0] == results[1] and results[0][1] == kind
    assert [row[5] for row in raced_state["signals"] if row[2] == SIGNAL_ID_MSG] == own_states


@pytest.mark.parametrize("order", ["relay-first", "vip-first"])
def test_the_race_on_a_relayed_call_withdraws_it_whichever_post_was_stored_first(
    tmp_path, order: str
) -> None:
    """Group 4 review, round 1. The relay forwards the VIP's call; the
    VIP deletes it, and the event is handled before the forward read
    stores the VIP's post. Stored with no signals row and no relay link,
    the deleted post was invisible to the relay rules: the relay's copy
    stayed queued, silently, in both storage orders. Applied as the event
    it is, the deletion withdraws the call (ruling C2) -- nothing is
    claimable, and it is said."""
    conn = db.connect(tmp_path / "store.db", role="writer")
    try:
        def store(channel_cfg, message_id, at, **extra):
            result = ingest(
                conn, channel_cfg=channel_cfg, message_id=message_id, text=SIGNAL_TEXT,
                posted_at_utc=at, edit_date_utc=None, reply_to_message_id=None,
                source="poll", now_utc=at + timedelta(seconds=5), accounts=ACCOUNTS, **extra,
            )
            conn.commit()
            return result

        def relay_copy():
            store(
                relay.RELAY, 7001, POSTED + timedelta(seconds=30),
                fwd_from_channel_id=relay.VIP.channel_id, fwd_from_post_id=SIGNAL_ID_MSG,
                fwd_from_date_utc=POSTED,
            )

        def vip_post_raced():
            deleted_at = POSTED + timedelta(seconds=40)
            unknown = edits_repo.apply_deletion(
                conn, channel_cfg=relay.VIP, message_id=SIGNAL_ID_MSG, now_utc=deleted_at,
                accounts=ACCOUNTS, detected_by="event",
            )
            assert unknown.outcome == "unknown"
            store(relay.VIP, SIGNAL_ID_MSG, POSTED, deleted_by_event_at_utc=deleted_at)

        for step in ((relay_copy, vip_post_raced) if order == "relay-first"
                     else (vip_post_raced, relay_copy)):
            step()
        claimable = conn.execute(
            "SELECT COUNT(*) FROM outbox o JOIN signals s ON s.signal_id = o.signal_id "
            "WHERE o.kind = 'ENTRY' AND o.state = 'QUEUED' AND s.state = 'NEW' "
            "AND s.withdrawn_at_utc IS NULL"
        ).fetchone()[0]
        assert claimable == 0
        assert [tuple(r) for r in conn.execute(
            "SELECT state, withdrawn_at_utc IS NOT NULL FROM signals"
        ).fetchall()] == [("VOIDED", 1)]
        codes = [c for (c,) in conn.execute("SELECT code FROM alerts").fetchall()]
        assert edits_repo.SIGNAL_VOIDED_BY_DELETION in codes
    finally:
        conn.close()


def test_a_report_naming_a_post_not_stored_yet_is_applied_as_that_report(tmp_path) -> None:
    """The VIP double-posts the call (4001, 4002) and deletes both in one
    report. 4002 is stored; the report's deletion of 4001 is handled
    before the forward read stores it, and that read's ingest job lands
    before the report's job for 4002. Remembered with the report's other
    ids, 4001's deletion is applied as part of that report -- 4002 already
    counts as gone -- so everything ends exactly as the same report
    handled after both were stored: the call withdrawn once, nothing
    claimable."""
    from test_listener import CHANNEL as L_CHANNEL
    from test_listener import POSTED as L_POSTED
    from test_listener import SIGNAL_TEXT as L_SIGNAL_TEXT
    from test_listener import _started

    from xaucopier.ingest.listener import IncomingMessage

    report = (4001, 4002)
    reported_at = L_POSTED + timedelta(seconds=40)

    def incoming(message_id: int) -> IncomingMessage:
        return IncomingMessage(
            channel_id=L_CHANNEL.channel_id, message_id=message_id, text=L_SIGNAL_TEXT,
            posted_at_utc=L_POSTED + timedelta(seconds=message_id - 4000),
            edit_date_utc=None, reply_to_message_id=None,
        )

    async def delete(listener, message_id: int):
        return await listener.ingest_deletion(
            L_CHANNEL, message_id, detected_by=edits_repo.EVENT_DETECTOR,
            also_deleted=report, reported_at_utc=reported_at,
        )

    async def raced(listener) -> None:
        await listener.ingest_message(incoming(4002), channel_cfg=L_CHANNEL, source="live")
        assert (await delete(listener, 4001)).outcome == "unknown"
        await listener.ingest_message(incoming(4001), channel_cfg=L_CHANNEL, source="poll")
        await delete(listener, 4002)

    async def reference(listener) -> None:
        await listener.ingest_message(incoming(4002), channel_cfg=L_CHANNEL, source="live")
        await listener.ingest_message(incoming(4001), channel_cfg=L_CHANNEL, source="poll")
        await delete(listener, 4001)
        await delete(listener, 4002)

    states = []
    for name, steps in (("raced", raced), ("reference", reference)):
        db_path = tmp_path / f"{name}.db"

        async def scenario(steps=steps, db_path=db_path) -> None:
            async with _started(db_path, FakeTelegramClient()) as listener:
                await steps(listener)

        asyncio.run(scenario())
        conn = db.connect(db_path, role="reader")
        try:
            states.append(_without_times(conn))
            claimable = conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE kind = 'ENTRY' AND state = 'QUEUED'"
            ).fetchone()[0]
            assert claimable == 0
        finally:
            conn.close()
    raced_state, reference_state = states
    assert raced_state == reference_state
    assert [(row[2], row[5]) for row in raced_state["signals"]] == [(4002, "VOIDED")]


def _report_outcome(conn) -> tuple:
    """What one report deleting a double post left: the signals' states,
    the ENTRY rows' states, and the alerts -- with no ids or times, so a
    run in which the other post holds the signal compares equal."""
    return (
        [tuple(r) for r in conn.execute(
            "SELECT state, withdrawn_at_utc IS NOT NULL FROM signals ORDER BY signal_id"
        ).fetchall()],
        sorted(s for (s,) in conn.execute("SELECT state FROM outbox WHERE kind = 'ENTRY'")),
        [tuple(r) for r in conn.execute("SELECT level, code FROM alerts ORDER BY alert_id")],
    )


@pytest.mark.parametrize("raced_id", [4001, 4002])
@pytest.mark.parametrize("order", ["4001-first", "4002-first"])
def test_a_double_post_deleted_in_one_report_and_raced_is_one_signal_voided_once(
    tmp_path, raced_id: int, order: str
) -> None:
    """Final fix FF5. The VIP posts the same call twice (4001, 4002) and
    deletes both in one report; one of them is raced -- the whole report
    is handled before the read that stores it. The stored one is the
    signal, and the report withdrew it. The raced post, stored then, saw a
    withdrawn call its channel posted again and became a second signal,
    voided the same moment: two signals rows, two ENTRY fan-outs and a
    WARNING about a signal the user never saw. It is a copy of the call
    the same report withdrew: one signal, voided once -- exactly as the
    report handled after both were stored."""
    report = frozenset({4001, 4002})
    reported_at = POSTED + timedelta(seconds=20)
    deletions = (4001, 4002) if order == "4001-first" else (4002, 4001)

    def store(conn, message_id, **extra):
        result = ingest(
            conn, channel_cfg=CHANNEL, message_id=message_id, text=SIGNAL_TEXT,
            posted_at_utc=POSTED + timedelta(seconds=message_id - 4000), edit_date_utc=None,
            reply_to_message_id=None, source="poll", now_utc=POSTED + timedelta(seconds=10),
            accounts=ACCOUNTS, **extra,
        )
        conn.commit()
        return result

    def delete(conn, message_id):
        result = edits_repo.apply_deletion(
            conn, channel_cfg=CHANNEL, message_id=message_id, now_utc=reported_at,
            accounts=ACCOUNTS, detected_by="event", also_deleted=report,
        )
        conn.commit()
        return result

    outcomes = []
    for raced in (True, False):
        conn = db.connect(tmp_path / f"{raced}.db", role="writer")
        try:
            if raced:
                store(conn, ({4001, 4002} - {raced_id}).pop())
                for message_id in deletions:
                    delete(conn, message_id)
                result = store(
                    conn, raced_id,
                    deleted_by_event_at_utc=reported_at, deleted_by_event_with=report,
                )
                assert result.outcome == "relay_duplicate" and result.relay_of_signal_id == 1
            else:
                for message_id in (4001, 4002):
                    store(conn, message_id)
                for message_id in deletions:
                    delete(conn, message_id)
            outcomes.append(_report_outcome(conn))
        finally:
            conn.close()
    raced_outcome, reference = outcomes
    assert raced_outcome == reference == (
        [("VOIDED", 1)],
        ["SKIPPED", "SKIPPED"],
        [("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION)],
    )


def test_a_raced_repost_of_a_call_withdrawn_in_an_earlier_report_is_still_the_call_again(
    tmp_path,
) -> None:
    """FF5's limit: only the report that withdrew the call makes the raced
    post a copy. The VIP deletes its call (one report), posts it again and
    deletes that too (a later report), the repost raced: the repost is the
    call posted again -- signal 2 -- and that later report withdrew it."""
    conn = db.connect(tmp_path / "store.db", role="writer")
    try:
        ingest(
            conn, channel_cfg=CHANNEL, message_id=4001, text=SIGNAL_TEXT,
            posted_at_utc=POSTED, edit_date_utc=None, reply_to_message_id=None,
            source="poll", now_utc=POSTED + timedelta(seconds=1), accounts=ACCOUNTS,
        )
        edits_repo.apply_deletion(
            conn, channel_cfg=CHANNEL, message_id=4001, now_utc=POSTED + timedelta(seconds=10),
            accounts=ACCOUNTS, detected_by="event", also_deleted=frozenset({4001}),
        )
        result = ingest(
            conn, channel_cfg=CHANNEL, message_id=4002, text=SIGNAL_TEXT,
            posted_at_utc=POSTED + timedelta(seconds=15), edit_date_utc=None,
            reply_to_message_id=None, source="poll", now_utc=POSTED + timedelta(seconds=30),
            accounts=ACCOUNTS, deleted_by_event_at_utc=POSTED + timedelta(seconds=20),
            deleted_by_event_with=frozenset({4001, 4002}),
        )
        conn.commit()
        assert (result.outcome, result.signal_id) == ("new", 2)
        assert [tuple(r) for r in conn.execute(
            "SELECT signal_id, state FROM signals ORDER BY signal_id"
        ).fetchall()] == [(1, "VOIDED"), (2, "VOIDED")]
    finally:
        conn.close()


def test_only_the_report_that_withdrew_the_call_makes_a_raced_post_its_copy(tmp_path) -> None:
    """FF5's test, `relay_repo._withdrawn_in_report`: the call counts as
    withdrawn in a report only when the post that withdrew it is on the
    same channel, is one of the ids that report named, and was deleted at
    the report's moment."""
    from xaucopier.store import relay_repo

    reported_at = POSTED + timedelta(seconds=20)
    conn = db.connect(tmp_path / "store.db", role="writer")
    try:
        for channel_cfg in (CHANNEL, relay.RELAY):  # the relay's 4001 is chatter
            ingest(
                conn, channel_cfg=channel_cfg, message_id=4001,
                text=SIGNAL_TEXT if channel_cfg is CHANNEL else CHATTER_TEXT,
                posted_at_utc=POSTED, edit_date_utc=None, reply_to_message_id=None,
                source="poll", now_utc=POSTED + timedelta(seconds=1), accounts=ACCOUNTS,
            )
            edits_repo.apply_deletion(
                conn, channel_cfg=channel_cfg, message_id=4001, now_utc=reported_at,
                accounts=ACCOUNTS, detected_by="event", also_deleted=frozenset({4001, 4002}),
            )
        conn.commit()
        withdrawn = relay_repo._withdrawn_in_report
        channel = CHANNEL.channel_id
        assert withdrawn(conn, 1, channel, reported_at, {4001, 4002}) is True
        assert withdrawn(conn, 1, channel, reported_at, {4002, 4003}) is False  # another report
        assert withdrawn(conn, 1, channel, reported_at + timedelta(seconds=1), {4001}) is False
        assert withdrawn(conn, 1, relay.RELAY.channel_id, reported_at, {4001}) is False
    finally:
        conn.close()


def test_a_channels_only_signal_deleted_unseen_is_named_with_its_waiting_trade(
    tmp_path,
) -> None:
    """Final fix FF13. A channel's only stored message is a signal (a
    channel first read today). It is deleted while the deletion update is
    lost: every re-read misses 1 of the 1 message it expects, which the
    breaker cannot tell from a bad answer -- so nothing is applied, and
    the signal's trade stays queued. The CRITICAL said only 'did not
    return 1 of the 1 messages', in this program's own terms. It now says
    which signal that message is, that its trade is still waiting, and
    that it should not be placed if the post is gone -- in plain words."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        assert await client.emit(A, FakeMessage(id=NEWEST_SIGNAL, message=SIGNAL_TEXT, date=POSTED))
        with client.blackout():
            assert not await client.emit_delete(A, NEWEST_SIGNAL)
        for _ in range(3):
            await _round(listener, time)

    _run(db_path, body)

    assert _signal(db_path, NEWEST_SIGNAL)["state"] == "NEW"  # the breaker held
    assert _alerts(db_path) == [("CRITICAL", edits_repo.WINDOW_READ_SUSPECT)]
    (detail,) = _rows(db_path, "SELECT detail FROM alerts")[0]
    assert (
        f"Message {NEWEST_SIGNAL} is signal 1, BUY 4480-4485, and 2 trade entries are still "
        "waiting for it. If the post is gone in Telegram, the trade should not be placed."
    ) in detail
    for jargon in ("window re-read", "MessageDeleted", "event"):
        assert jargon not in detail


def test_a_suspect_read_names_no_trade_when_none_is_waiting(tmp_path) -> None:
    """The same breaker trip over chatter alone: no trade is named."""
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        assert await client.emit(A, FakeMessage(id=NEWEST_SIGNAL, message=CHATTER_TEXT, date=POSTED))
        with client.blackout():
            assert not await client.emit_delete(A, NEWEST_SIGNAL)
        for _ in range(3):
            await _round(listener, time)

    _run(db_path, body)

    (detail,) = _rows(db_path, "SELECT detail FROM alerts WHERE code = ?",
                      (edits_repo.WINDOW_READ_SUSPECT,))[0]
    assert "signal" not in detail and "trade" not in detail


def test_waiting_trades_names_only_entries_still_queued(tmp_path) -> None:
    """FF13's helper: a signal whose entries are all taken names no trade;
    one left waiting is said in the singular; chatter is never named."""
    from xaucopier.store import outbox

    conn = db.connect(tmp_path / "store.db", role="writer")
    try:
        for message_id, text in ((4001, SIGNAL_TEXT), (4002, CHATTER_TEXT)):
            ingest(
                conn, channel_cfg=CHANNEL, message_id=message_id, text=text,
                posted_at_utc=POSTED, edit_date_utc=None, reply_to_message_id=None,
                source="poll", now_utc=POSTED, accounts=ACCOUNTS,
            )
        conn.commit()

        def said():
            return edits_repo.waiting_trades(
                conn, channel_id=CHANNEL.channel_id, message_ids=(4001, 4002, 4003)
            )

        assert said() == (
            "Message 4001 is signal 1, BUY 4480-4485, and 2 trade entries are still waiting "
            "for it. If the post is gone in Telegram, the trade should not be placed.",
        )
        for n, account in enumerate(ACCOUNTS):
            assert outbox.claim(conn, account_id=account.account_id, pid=1, now_utc=POSTED)
            conn.commit()
            if n == 0:
                assert said() == (
                    "Message 4001 is signal 1, BUY 4480-4485, and 1 trade entry is still "
                    "waiting for it. If the post is gone in Telegram, the trade should not be "
                    "placed.",
                )
        assert said() == ()
    finally:
        conn.close()
