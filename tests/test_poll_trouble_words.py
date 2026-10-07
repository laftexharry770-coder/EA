"""What the watch window says when reading a channel goes wrong (final fix
FF20).

Every poll round that failed a channel printed a full Python traceback in
the watch window -- every 5 s, for ever -- and a flood wait read "must
wait 300s (FloodWaitError) ... Telethon applies a flood wait ... the
writer probe keeps running". When the Telegram account could no longer
read a channel (it left, or was removed from, a paid channel), nothing
was raised and `status` said "Telegram is not answering ... restart it",
which cannot help. Telethon's own lines reached the window too, and every
alert's line ended "channel=None message_id=None signal_id=None".

Now each trouble is one plain line when it starts (and one when it is
over); the technical detail goes to watch.log only. An account that can
no longer read a channel is a WARNING alert saying to join it again,
cleared by itself once the channel is read again, and `status` says so.

Real listener, poll and store; Telegram is the fake only.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from telethon import errors

import xaucopier.cli.watch as watch_module
from test_poll import (
    HANG,
    A,
    B,
    CHANNEL_A,
    CHANNEL_B,
    NOW,
    ScriptedClient,
    SteppedTime,
    _chatter,
    _polling,
    _rows,
    _run_rounds,
    _seed_pointer,
)
from xaucopier.cli import status as status_module
from xaucopier.store import alerts, db, heartbeats

#: Words that mean nothing to the user.
JARGON = ("Traceback", "Telethon", "FloodWait", "flood", "Error", "RPC", "poll", "re-read")


def _poll_with(
    tmp_path: Path, failures: list, rounds: int, *, then_ok: int = 0, **poll_kwargs
) -> tuple[str, str]:
    """Channel A fails each of `failures` in turn (one per round), then is
    read `then_ok` rounds; B is fine. The window and the log file, as watch
    shows and writes them."""
    db_path = tmp_path / "store.db"
    log_path = tmp_path / "watch.log"
    _seed_pointer(db_path, A, 9)
    _seed_pointer(db_path, B, 19)
    time = SteppedTime()

    async def scenario():
        client = ScriptedClient()
        async with _polling(
            db_path, client, time, channels=(CHANNEL_A, CHANNEL_B), **poll_kwargs
        ) as listener:
            client.failures[A] = list(failures)
            client.add_to_history(A, _chatter(10))
            await _run_rounds(listener, time, rounds)
            time.now += 3600  # past any wait Telegram asked for
            await _run_rounds(listener, time, then_ok)

    stop = watch_module.start_logging(log_path)
    try:
        asyncio.run(scenario())
    finally:
        stop()
    return log_path.read_text(encoding="utf-8"), str(db_path)


def _window_lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    window = capsys.readouterr().err
    assert "Traceback" not in window
    return [line for line in window.splitlines() if " UTC " in line]


#: What the window says about trouble reading a channel, and its end.
_TROUBLE = ("could not be read", "works again", "asked watch to wait", "can no longer read",
            "did not answer")


def _about_a(lines: list[str]) -> list[str]:
    return [line for line in lines if "Channel A" in line and any(t in line for t in _TROUBLE)]


def test_a_read_that_keeps_failing_is_one_plain_line_and_no_traceback_in_the_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log, _ = _poll_with(tmp_path, [ValueError("Request was unsuccessful 5 time(s)")] * 3, 3,
                        then_ok=1)

    window = _window_lines(capsys)
    about_a = _about_a(window)
    assert len(about_a) == 2, about_a  # it started failing; it works again
    assert "could not be read" in about_a[0] and "watch.log" in about_a[0]
    assert "works again" in about_a[1]
    for line in about_a:
        for word in JARGON:
            assert word not in line, (word, line)
    assert log.count("Traceback") == 1 and "Request was unsuccessful" in log


def test_a_wait_telegram_asks_for_is_said_in_minutes_in_plain_words(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log, _ = _poll_with(tmp_path, [errors.FloodWaitError(request=None, capture=300)], 1)

    (line,) = _about_a(_window_lines(capsys))
    assert "Telegram asked watch to wait about 5 minutes before reading Channel A again" in line
    for word in JARGON:
        assert word not in line, (word, line)
    assert "FloodWaitError" in log  # the detail, for whoever reads the log file


@pytest.mark.parametrize(
    "error", [errors.ChannelPrivateError(request=None), errors.ChannelInvalidError(request=None)]
)
def test_a_channel_this_account_can_no_longer_read_is_one_alert_cleared_once_read_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    _, db_path = _poll_with(tmp_path, [error] * 3, 3)

    (line,) = _about_a(_window_lines(capsys))
    assert "can no longer read" in line and "join it again in Telegram" in line
    raised = _rows(Path(db_path), "SELECT level, code, channel_id, detail, acked FROM alerts")
    assert [(r[0], r[1], r[2], r[4]) for r in raised] == [
        ("WARNING", alerts.TELEGRAM_CHANNEL_UNREADABLE, A, 0)
    ]
    assert "join it again in Telegram" in raised[0][3]
    (detail,) = _rows(
        Path(db_path), "SELECT detail FROM heartbeats WHERE name = ?", (heartbeats.POLL_ROUND,)
    )[0]
    assert detail.startswith("1 of 2 channel(s) read") and "cannot read: 1" in detail


def test_the_unreadable_channel_alert_clears_itself_once_the_channel_is_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, db_path = _poll_with(tmp_path, [errors.ChannelPrivateError(request=None)], 1, then_ok=1)

    assert _rows(Path(db_path), "SELECT code, acked FROM alerts") == [
        (alerts.TELEGRAM_CHANNEL_UNREADABLE, 1)
    ]


def test_status_says_the_account_cannot_read_a_channel_not_that_telegram_is_not_answering(
) -> None:
    lines = status_module.round_trouble("1 of 3 channel(s) read; waiting: 1; cannot read: 1")
    assert any("cannot read 1 of them" in line and "join" in line for line in lines), lines
    assert any("asked watch to wait" in line for line in lines), lines
    assert not any("not answering" in line for line in lines)
    assert not any("restart" in line.lower() and "does not help" not in line for line in lines)
    other = status_module.round_trouble("1 of 2 channel(s) read")
    assert any("not answering" in line for line in other)


def test_telethons_own_lines_go_to_the_log_file_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_path = tmp_path / "watch.log"
    stop = watch_module.start_logging(log_path)
    try:
        logging.getLogger("telethon.network.mtprotosender").warning(
            "Attempt 1 at connecting failed: ConnectionError"
        )
        logging.getLogger("telethon").info("Sleeping for 90s (0:01:30) on GetDialogsRequest flood wait")
    finally:
        stop()

    assert "Attempt 1" not in capsys.readouterr().err
    log = log_path.read_text(encoding="utf-8")
    assert "Attempt 1 at connecting failed" in log and "Sleeping for 90s" in log


def test_an_alert_line_names_only_what_it_is_about(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    conn = db.connect(tmp_path / "store.db", role="writer")
    try:
        with caplog.at_level(logging.WARNING, logger="xaucopier.store.alerts"):
            alerts.record(conn, now_utc=NOW, level="WARNING", code="SOMETHING",
                          detail="what it says", channel_id=A)
    finally:
        conn.close()

    (line,) = [r.getMessage() for r in caplog.records]
    assert "None" not in line
    assert f"channel={A}" in line and "what it says" in line


def test_a_first_read_at_start_that_fails_is_one_plain_line_in_the_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "store.db"
    log_path = tmp_path / "watch.log"
    _seed_pointer(db_path, A, 9)
    time = SteppedTime()

    async def scenario():
        client = ScriptedClient()
        client.failures[A] = [ValueError("Request was unsuccessful 5 time(s)")]
        async with _polling(db_path, client, time, channels=(CHANNEL_A,)):
            pass

    stop = watch_module.start_logging(log_path)
    try:
        asyncio.run(scenario())
    finally:
        stop()

    (line,) = _about_a(_window_lines(capsys))
    assert "could not be read" in line
    for word in JARGON + ("backfill", "pointer"):
        assert word not in line, (word, line)
    assert "Traceback" in log_path.read_text(encoding="utf-8")


def test_a_wait_is_said_in_seconds_when_it_is_short() -> None:
    from xaucopier.ingest import poll

    assert poll.wait_words(45) == "about 45 seconds"
    assert poll.wait_words(300) == "about 5 minutes"
    assert poll.wait_words(90) == "about 2 minutes"
    assert poll.wait_words(7200) == "about 2 hours"
    assert poll.wait_words(1) == "about 1 second"


def test_a_read_telegram_keeps_not_answering_is_said_once_in_plain_words(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _poll_with(tmp_path, [HANG] * 3, 3, then_ok=1, rpc_timeout=0.2)

    about_a = _about_a(_window_lines(capsys))
    assert len(about_a) == 2, about_a
    assert "Telegram did not answer within 0.2 seconds when reading Channel A" in about_a[0]
    assert "works again" in about_a[1]
    for line in about_a:
        for word in JARGON:
            assert word not in line, (word, line)


def test_an_error_while_disconnecting_at_the_end_is_for_the_log_file_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """watch is stopping anyway; nothing is lost by a disconnect that
    fails, so the window gets no traceback for it."""
    db_path = tmp_path / "store.db"
    log_path = tmp_path / "watch.log"
    _seed_pointer(db_path, A, 9)
    time = SteppedTime()

    class BadDisconnect(ScriptedClient):
        async def disconnect(self) -> None:
            await super().disconnect()
            raise ConnectionError("the socket is already closed")

    async def scenario():
        async with _polling(db_path, BadDisconnect(), time, channels=(CHANNEL_A,)):
            pass

    stop = watch_module.start_logging(log_path)
    try:
        asyncio.run(scenario())
    finally:
        stop()

    window = capsys.readouterr().err
    assert "Traceback" not in window and "disconnecting" not in window
    log = log_path.read_text(encoding="utf-8")
    assert "error disconnecting" in log and "the socket is already closed" in log
