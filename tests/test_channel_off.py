"""A channel switched off: its waiting trade entries are cancelled at the
next `watch` start (final review F15, watch side).

OFF means the channel is not read at all -- neither new posts nor the
edits, deletions and follow-ups of signals it already posted. An ENTRY
still waiting for one of its signals could therefore be traded after the
poster withdrew it, with nobody watching. So `watch`'s start SKIPs every
QUEUED ENTRY row whose signal's channel is not switched on, with reason
'channel switched off', in one writer job, and records one WARNING alert,
CHANNEL_OFF_ENTRIES_SKIPPED, naming how many -- only when there were any.
A CLAIMED row is a worker's (possibly mid-send) and is left alone, as are
follow-ups.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fakes.telegram import FakeTelegramClient
from test_listener import ACCOUNTS, CHANNEL, NOW, POSTED, SIGNAL_TEXT, _build_listener, _rows
from xaucopier.config.channels import ChannelCfg
from xaucopier.store import alerts, db, messages_repo, outbox

A = CHANNEL
B_OFF = ChannelCfg(
    channel_id=-1002222222222, name="Channel B", parser="mmh", ordinal=2, enabled=False
)
#: A channel that was removed from channels.toml altogether.
C_GONE = ChannelCfg(
    channel_id=-1003333333333, name="Channel C", parser="mmh", ordinal=3, enabled=True
)
REASON = "channel switched off"


def _store_signal(db_path: Path, channel: ChannelCfg, message_id: int) -> int:
    conn = db.connect(db_path, role="writer")
    try:
        result = messages_repo.ingest(
            conn,
            channel_cfg=channel,
            message_id=message_id,
            text=SIGNAL_TEXT.replace("SL: 4470", f"SL: {4460 + message_id // 100}"),
            posted_at_utc=POSTED,
            edit_date_utc=None,
            reply_to_message_id=None,
            source="live",
            now_utc=POSTED,
            accounts=ACCOUNTS,
        )
        conn.commit()
    finally:
        conn.close()
    assert result.outcome == "new" and result.signal_id is not None, result
    return result.signal_id


def _claim_one(db_path: Path, account_id: str) -> None:
    conn = db.connect(db_path, role="writer")
    try:
        claimed = outbox.claim(conn, account_id=account_id, pid=4242, now_utc=NOW)
        conn.commit()
    finally:
        conn.close()
    assert claimed is not None


def _start_once(db_path: Path, channels: tuple[ChannelCfg, ...]) -> None:
    async def scenario():
        listener = _build_listener(db_path, FakeTelegramClient(), channels=channels)
        await listener.start()
        await listener.aclose()

    asyncio.run(scenario())


def _entries(db_path: Path) -> list[tuple]:
    return _rows(
        db_path,
        "SELECT s.channel_id, o.account_id, o.state, o.reason FROM outbox o "
        "JOIN signals s ON s.signal_id = o.signal_id WHERE o.kind = 'ENTRY' "
        "ORDER BY o.outbox_id",
    )


def _channel_off_alerts(db_path: Path) -> list[tuple]:
    return _rows(
        db_path,
        "SELECT level, detail FROM alerts WHERE code = ?",
        (alerts.CHANNEL_OFF_ENTRIES_SKIPPED,),
    )


def test_queued_entries_of_channels_not_switched_on_are_skipped_at_start(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "store.db"
    _store_signal(db_path, A, 101)
    _store_signal(db_path, B_OFF, 201)
    _store_signal(db_path, C_GONE, 301)
    _claim_one(db_path, "acct-a")  # the oldest: channel A's signal, left alone either way

    _start_once(db_path, (A, B_OFF))

    a, b, c = A.channel_id, B_OFF.channel_id, C_GONE.channel_id
    assert _entries(db_path) == [
        (a, "acct-a", "CLAIMED", None),
        (a, "acct-b", "QUEUED", None),
        (b, "acct-a", "SKIPPED", REASON),
        (b, "acct-b", "SKIPPED", REASON),
        (c, "acct-a", "SKIPPED", REASON),
        (c, "acct-b", "SKIPPED", REASON),
    ]
    raised = _channel_off_alerts(db_path)
    assert len(raised) == 1
    level, detail = raised[0]
    assert level == "WARNING"
    assert "4 waiting trade entries" in detail
    for jargon in ("RPC", "client", "listener", "flood", "outbox"):
        assert jargon not in detail, (jargon, detail)


def test_a_claimed_entry_of_a_channel_switched_off_is_left_to_its_worker(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    _store_signal(db_path, B_OFF, 201)
    _claim_one(db_path, "acct-a")

    _start_once(db_path, (A, B_OFF))

    assert [state for _, _, state, _ in _entries(db_path)] == ["CLAIMED", "SKIPPED"]


def test_no_alert_when_nothing_was_waiting(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    _store_signal(db_path, A, 101)

    _start_once(db_path, (A, B_OFF))
    _start_once(db_path, (A, B_OFF))

    assert [state for _, _, state, _ in _entries(db_path)] == ["QUEUED", "QUEUED"]
    assert _channel_off_alerts(db_path) == []


def test_the_skip_happens_once_and_a_second_start_raises_nothing_new(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    _store_signal(db_path, B_OFF, 201)

    _start_once(db_path, (A, B_OFF))
    _start_once(db_path, (A, B_OFF))

    assert len(_channel_off_alerts(db_path)) == 1
    assert "2 waiting trade entries" in _channel_off_alerts(db_path)[0][1]


def test_one_waiting_entry_is_named_in_the_singular(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    _store_signal(db_path, B_OFF, 201)
    _claim_one(db_path, "acct-a")

    _start_once(db_path, (A, B_OFF))

    assert "1 waiting trade entry " in _channel_off_alerts(db_path)[0][1]


def test_follow_ups_are_not_touched(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    conn = db.connect(db_path, role="writer")
    try:
        messages_repo.ingest(
            conn,
            channel_cfg=B_OFF,
            message_id=202,
            text="close all now",
            posted_at_utc=POSTED,
            edit_date_utc=None,
            reply_to_message_id=None,
            source="live",
            now_utc=POSTED,
            accounts=ACCOUNTS,
        )
        conn.commit()
    finally:
        conn.close()

    _start_once(db_path, (A, B_OFF))

    assert _rows(db_path, "SELECT kind, state FROM outbox") == [
        ("FOLLOWUP", "QUEUED"),
        ("FOLLOWUP", "QUEUED"),
    ]
    assert _channel_off_alerts(db_path) == []


def test_a_follow_up_row_that_names_its_signal_is_not_touched_either(tmp_path: Path) -> None:
    """Follow-up rows are queued without a signal today, but one that names
    the signal it acts on (as a follow-up resolved to its target may) is
    still a follow-up: only ENTRY rows are cancelled."""
    db_path = tmp_path / "store.db"
    signal_id = _store_signal(db_path, B_OFF, 201)
    conn = db.connect(db_path, role="writer")
    try:
        conn.execute(
            "INSERT INTO outbox (account_id, kind, signal_id, followup_id, state) "
            "VALUES ('acct-a', 'FOLLOWUP', ?, NULL, 'QUEUED')",
            (signal_id,),
        )
        conn.commit()
    finally:
        conn.close()

    _start_once(db_path, (A, B_OFF))

    assert _rows(db_path, "SELECT state FROM outbox WHERE kind = 'FOLLOWUP'") == [("QUEUED",)]
    assert {state for _, _, state, _ in _entries(db_path)} == {"SKIPPED"}


def test_the_alert_names_each_channel(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    _store_signal(db_path, B_OFF, 201)
    _store_signal(db_path, C_GONE, 301)

    _start_once(db_path, (A, B_OFF))

    detail = _channel_off_alerts(db_path)[0][1]
    assert f"Channel B ({B_OFF.channel_id}): 2" in detail
    assert f"channel {C_GONE.channel_id}, no longer listed: 2" in detail


def test_a_skip_that_fails_does_not_keep_watch_from_starting(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    import logging
    import sqlite3

    db_path = tmp_path / "store.db"
    _store_signal(db_path, B_OFF, 201)

    def locked(conn, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(outbox, "skip_entries_of_channels_off", locked)
    client = FakeTelegramClient()

    async def scenario():
        listener = _build_listener(db_path, client, channels=(A, B_OFF))
        await listener.start()
        await listener.aclose()

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())

    assert "add_event_handler" in client.calls
    assert [state for _, _, state, _ in _entries(db_path)] == ["QUEUED", "QUEUED"]
    assert any("tries again when it next starts" in r.getMessage() for r in caplog.records)
