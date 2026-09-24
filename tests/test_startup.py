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
    assert seen == [60.0, 60.0], "connect and get_dialogs are each bounded by 60 s"
