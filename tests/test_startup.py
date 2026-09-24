"""Tests for `watch`'s start-up with Telegram (final review fix wave, group 2).

- **Deadlines (ledger 145, F06 side note).** `connect()` and the start-up
  `get_dialogs()` each get a deadline: with no network -- or Telethon
  reconnecting in a tight loop on a malformed reply -- `start()` used to
  hang for ever, with only `watch_started` stamped. Now it stops with
  exit 4 and a plain "Could not reach Telegram within 60 seconds".
- **Telegram's clock (ledger 143, MF3).** `start()` asks Telegram for its
  time once (`updates.GetStateRequest`, bounded). The start time the
  first-run seed keeps everything after is min(this computer, Telegram),
  in whole seconds: a fast local clock can no longer make a late seed skip
  posts. A difference over 60 s is a plain WARNING, never a refusal; no
  answer means this computer's clock, with a WARNING.

Real listener, real store, fake Telegram only.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from fakes.telegram import FakeMessage, FakeTelegramClient
from test_listener import (
    ACCOUNTS,
    CHANNEL,
    CHANNEL_ID,
    CHATTER_TEXT,
    NOW,
    _build_listener,
    _rows,
)
from xaucopier.ingest import client as client_module
from xaucopier.ingest import listener as listener_module
from xaucopier.ingest.listener import (
    EXIT_LISTENER_STOPPED,
    Listener,
    ListenerStartupError,
    run_listener,
)
from xaucopier.store import db


@dataclass
class _HangingClient(FakeTelegramClient):
    """The fake, never answering at `hang_at` ("connect" or "get_dialogs")."""

    hang_at: str = ""
    cancelled: list[str] | None = None

    async def _hang(self, where: str) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if self.cancelled is not None:
                self.cancelled.append(where)
            raise

    async def connect(self) -> None:
        if self.hang_at == "connect":
            self.calls.append("connect")
            await self._hang("connect")
        await super().connect()

    async def get_dialogs(self, *args: Any, **kwargs: Any) -> Any:
        if self.hang_at == "get_dialogs":
            self.calls.append("get_dialogs")
            await self._hang("get_dialogs")
        return await super().get_dialogs(*args, **kwargs)


# -- deadlines ------------------------------------------------------------------------


@pytest.mark.parametrize("hang_at", ["connect", "get_dialogs"])
def test_a_start_up_that_cannot_reach_telegram_stops_with_exit_4_in_plain_words(
    tmp_path: Path, hang_at: str, caplog: pytest.LogCaptureFixture
) -> None:
    db_path = tmp_path / "store.db"

    async def scenario():
        client = _HangingClient(hang_at=hang_at, cancelled=[])
        listener = _build_listener(db_path, client, telegram_timeout=0.1)
        loop = asyncio.get_running_loop()
        started = loop.time()
        exit_code = await asyncio.wait_for(run_listener(listener), timeout=10.0)
        return exit_code, loop.time() - started, client

    with caplog.at_level(logging.ERROR, logger=listener_module.__name__):
        exit_code, elapsed, client = asyncio.run(scenario())

    assert exit_code == EXIT_LISTENER_STOPPED
    assert elapsed < 5.0
    assert client.cancelled == [hang_at], "the hung call was not cancelled"
    assert "add_event_handler" not in client.calls
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("Could not reach Telegram within 0.1 seconds.") for m in messages)
    stop = next(m for m in messages if m.startswith("Could not reach Telegram"))
    assert "exit code 4" in stop
    for jargon in ("RPC", "client", "listener", "flood"):
        assert jargon not in stop


def test_start_raises_a_listener_startup_error_carrying_the_timeout(tmp_path: Path) -> None:
    async def scenario():
        listener = _build_listener(
            tmp_path / "store.db", _HangingClient(hang_at="connect"), telegram_timeout=0.1
        )
        try:
            with pytest.raises(ListenerStartupError) as caught:
                await listener.start()
            return caught.value
        finally:
            await listener.aclose()

    error = asyncio.run(scenario())
    assert isinstance(error.__cause__, client_module.TelegramUnreachableError)


def test_the_start_up_deadline_is_sixty_seconds_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The design value, read when start() runs (so it is one constant)."""
    assert client_module.STARTUP_TIMEOUT_SECONDS == 60.0
    seen: list[float] = []
    real = client_module.telegram_within

    async def spy(awaitable, seconds):
        seen.append(seconds)
        return await real(awaitable, seconds)

    monkeypatch.setattr(client_module, "telegram_within", spy)

    async def scenario():
        listener = _build_listener(tmp_path / "store.db", FakeTelegramClient())
        await listener.start()
        await listener.aclose()

    asyncio.run(scenario())
    # connect and get_dialogs are each bounded by 60 s; between them, the
    # 10 s ask for Telegram's clock (ledger 143).
    assert seen == [60.0, 10.0, 60.0]


# -- ledger 143 (MF3): Telegram's clock ---------------------------------------------------


def _clock_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if r.name == listener_module.__name__ and "clock" in r.getMessage().lower()
        and r.levelno >= logging.WARNING
    ]


def _assert_plain(text: str) -> None:
    for jargon in ("RPC", "client", "listener", "flood"):
        assert jargon not in text, (jargon, text)


def _start_and_report(db_path: Path, client: FakeTelegramClient, local_now: datetime):
    async def scenario():
        listener = Listener(
            client=client,
            channels=(CHANNEL,),
            accounts=ACCOUNTS,
            writer=db.Writer(db_path),
            db_path=db_path,
            now=lambda: local_now,
        )
        await listener.start()
        try:
            return listener.started_at, [
                c.offset_date for c in client.get_messages_calls if c.offset_date is not None
            ]
        finally:
            await listener.aclose()

    return asyncio.run(scenario())


def _old_history() -> list[FakeMessage]:
    return [FakeMessage(id=i, message=CHATTER_TEXT, date=NOW - timedelta(hours=1)) for i in (1, 2)]


def test_a_fast_computer_clock_is_overruled_by_telegrams(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """This computer's clock runs 5 minutes fast. The start is Telegram's
    time, so the first read starts a minute before the real start -- not 4
    minutes after it, which skipped every post of those minutes the update
    stream dropped -- and the user is told, in plain words."""
    client = FakeTelegramClient(history={CHANNEL_ID: _old_history()}, server_time=NOW)
    with caplog.at_level(logging.WARNING):
        started_at, anchors = _start_and_report(
            tmp_path / "store.db", client, NOW + timedelta(minutes=5)
        )

    assert started_at == NOW
    assert anchors == [NOW - listener_module.FIRST_RUN_SEED_MARGIN]
    assert _rows(tmp_path / "store.db", "SELECT seed_cutoff_utc IS NULL FROM channel_state") == [
        (1,)
    ]
    warnings = _clock_warnings(caplog)
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    text = warnings[0].getMessage()
    assert "300 seconds ahead" in text
    _assert_plain(text)


def test_a_slow_computer_clock_is_kept_and_named(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Behind Telegram: the earlier of the two is this computer's own, which
    only makes the first read start earlier. Still worth fixing; said once."""
    client = FakeTelegramClient(history={CHANNEL_ID: _old_history()}, server_time=NOW)
    with caplog.at_level(logging.WARNING):
        started_at, anchors = _start_and_report(
            tmp_path / "store.db", client, NOW - timedelta(minutes=2)
        )

    assert started_at == NOW - timedelta(minutes=2)
    assert anchors == [NOW - timedelta(minutes=2) - listener_module.FIRST_RUN_SEED_MARGIN]
    warnings = _clock_warnings(caplog)
    assert len(warnings) == 1 and "120 seconds behind" in warnings[0].getMessage()
    _assert_plain(warnings[0].getMessage())


@pytest.mark.parametrize("skew_seconds, warned", [(60, False), (-60, False), (61, True), (-61, True)])
def test_clocks_within_a_minute_of_each_other_are_not_worth_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, skew_seconds: int, warned: bool
) -> None:
    client = FakeTelegramClient(server_time=NOW)
    local = NOW + timedelta(seconds=skew_seconds)
    with caplog.at_level(logging.WARNING):
        started_at, _ = _start_and_report(tmp_path / "store.db", client, local)

    assert started_at == min(NOW, local)
    assert bool(_clock_warnings(caplog)) is warned


def test_the_start_is_in_whole_seconds(tmp_path: Path) -> None:
    client = FakeTelegramClient(server_time=NOW + timedelta(hours=1))
    started_at, _ = _start_and_report(
        tmp_path / "store.db", client, NOW + timedelta(milliseconds=999)
    )
    assert started_at == NOW


@pytest.mark.parametrize("failure", ["error", "hang"])
def test_no_answer_from_telegrams_clock_means_this_computers_clock_with_a_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Never a refusal: watch starts on this computer's clock and says so."""
    monkeypatch.setattr(listener_module, "TELEGRAM_CLOCK_TIMEOUT_SECONDS", 0.1)
    client = FakeTelegramClient(
        history={CHANNEL_ID: _old_history()},
        get_state_failure=ConnectionError("no answer") if failure == "error" else "hang",
    )
    local = NOW + timedelta(minutes=5)
    with caplog.at_level(logging.WARNING):
        started_at, anchors = _start_and_report(tmp_path / "store.db", client, local)

    assert started_at == local
    assert anchors == [local - listener_module.FIRST_RUN_SEED_MARGIN]
    assert client.calls.count("GetStateRequest") == 1
    warnings = _clock_warnings(caplog)
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    assert "this computer's clock" in warnings[0].getMessage()
    _assert_plain(warnings[0].getMessage())


def test_telegrams_clock_is_asked_once_within_ten_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert listener_module.TELEGRAM_CLOCK_TIMEOUT_SECONDS == 10.0
    seen: list[float] = []
    real = client_module.telegram_within

    async def spy(awaitable, seconds):
        seen.append(seconds)
        return await real(awaitable, seconds)

    monkeypatch.setattr(client_module, "telegram_within", spy)
    client = FakeTelegramClient(server_time=NOW)
    _start_and_report(tmp_path / "store.db", client, NOW)
    assert seen == [60.0, 10.0, 60.0], "connect, Telegram's clock, then the chat list"
    assert client.calls.count("GetStateRequest") == 1


def test_a_dead_session_met_asking_the_time_still_exits_3(tmp_path: Path) -> None:
    from telethon import errors

    client = FakeTelegramClient(
        get_state_failure=errors.AuthKeyUnregisteredError(request=None)
    )

    async def scenario():
        listener = _build_listener(tmp_path / "store.db", client)
        return await asyncio.wait_for(run_listener(listener), timeout=10.0)

    assert asyncio.run(scenario()) == client_module.EXIT_TERMINAL_AUTH


def test_the_time_telegram_took_to_answer_is_taken_off_its_clock(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """start() began at 11:01:30 on a clock 90 s fast; Telegram's answer came
    30 s later (11:02:00 here) and said 11:00:30. Telegram's clock at the
    moment start() began was therefore 11:00:00 -- that is the start."""
    readings = iter([NOW + timedelta(seconds=90)])

    def clock() -> datetime:
        return next(readings, NOW + timedelta(seconds=120))

    client = FakeTelegramClient(server_time=NOW + timedelta(seconds=30))

    async def scenario():
        listener = Listener(
            client=client, channels=(CHANNEL,), accounts=ACCOUNTS,
            writer=db.Writer(tmp_path / "store.db"), db_path=tmp_path / "store.db", now=clock,
        )
        await listener.start()
        try:
            return listener.started_at
        finally:
            await listener.aclose()

    with caplog.at_level(logging.WARNING):
        started_at = asyncio.run(scenario())

    assert started_at == NOW
    warnings = _clock_warnings(caplog)
    assert len(warnings) == 1 and "90 seconds ahead" in warnings[0].getMessage()


def test_a_computer_clock_that_steps_back_while_waiting_is_not_read_as_negative_time(
    tmp_path: Path,
) -> None:
    """The clock read 11:01:40 when start() began and 11:01:30 when the
    answer came (Windows corrected it meanwhile). No time can have passed
    backwards: Telegram's 11:00:00 is taken as it is."""
    readings = iter([NOW + timedelta(seconds=100)])

    def clock() -> datetime:
        return next(readings, NOW + timedelta(seconds=90))

    client = FakeTelegramClient(server_time=NOW)

    async def scenario():
        listener = Listener(
            client=client, channels=(CHANNEL,), accounts=ACCOUNTS,
            writer=db.Writer(tmp_path / "store.db"), db_path=tmp_path / "store.db", now=clock,
        )
        await listener.start()
        try:
            return listener.started_at
        finally:
            await listener.aclose()

    assert asyncio.run(scenario()) == NOW


def test_an_answer_without_a_usable_time_means_this_computers_clock(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = FakeTelegramClient(server_time=NOW.replace(tzinfo=None))
    local = NOW + timedelta(minutes=5)
    with caplog.at_level(logging.WARNING):
        started_at, _ = _start_and_report(tmp_path / "store.db", client, local)

    assert started_at == local
    warnings = _clock_warnings(caplog)
    assert len(warnings) == 1 and "this computer's clock" in warnings[0].getMessage()
