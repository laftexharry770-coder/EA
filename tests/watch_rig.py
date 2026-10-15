"""The `xaucopier watch` test harness, shared by tests/test_cli_watch.py and
tests/test_cli_permanent_errors.py.

`watch` runs for real -- the CLI entry point, the config loaders on a
temporary copy of the config, the listener, the poll, the store -- with
only Telegram faked (`tests/fakes/telegram.py`, never the network) and the
three machine-touching pieces swapped for doubles: the DPAPI session file,
the system-wide mutex, and the poll's 5-second interval.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import xaucopier.cli.watch as watch_module
from fakes.telegram import FakeMessage, FakeTelegramClient
from xaucopier.ingest.poll import HistoryPoll
from xaucopier.ingest.secrets import SecretsBlob
from xaucopier.store import db, heartbeats

REPO = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (REPO / "tests" / "fixtures" / "messages" / "mmh_buy_4tp.json").read_text(encoding="utf-8")
)
SIGNAL_TEXT: str = FIXTURE["text"]
CHANNEL_ID = -1001111111111
CHATTER = "Good morning traders"

ENABLED_CHANNEL = f"""
[[channels]]
channel_id = {CHANNEL_ID}
name       = "Test Channel"
parser     = "mmh"
ordinal    = 1
enabled    = true
"""

#: Obviously fake; never a real credential.
FAKE_BLOB = SecretsBlob(api_id=12345, api_hash="0" * 32, session_string="NOT_A_REAL_SESSION")


# -- the rig -----------------------------------------------------------------------


@dataclass
class ScriptedWatchClient(FakeTelegramClient):
    """The fake client, plus a scenario that starts once `watch` is fully
    up -- i.e. when the listener calls `run_until_disconnected()`, after
    backfill, handler registration and the poll task."""

    script: Callable[["ScriptedWatchClient"], Awaitable[None]] | None = None
    script_task: asyncio.Task | None = None

    async def run_until_disconnected(self) -> None:
        if self.script is not None and self.script_task is None:
            self.script_task = asyncio.create_task(self.script(self))
        await super().run_until_disconnected()


class FakeLock:
    def __init__(self) -> None:
        self.acquired = 0
        self.released = 0

    def __call__(self) -> "FakeLock":
        self.acquired += 1
        return self

    def release(self) -> None:
        self.released += 1


@dataclass
class Rig:
    config_dir: Path
    db_path: Path
    secrets_path: Path
    lock: FakeLock
    built: list[SecretsBlob]

    def argv(self, command: str = "watch") -> list[str]:
        return [
            command,
            "--config-dir", str(self.config_dir),
            "--db", str(self.db_path),
            "--secrets", str(self.secrets_path),
        ]


def _write_config(config_dir: Path, channels: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "channels.toml").write_text(channels, encoding="utf-8")
    # The real accounts file, copied (never modified).
    (config_dir / "accounts.toml").write_text(
        (REPO / "config" / "accounts.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    config_dir = tmp_path / "config"
    _write_config(config_dir, ENABLED_CHANNEL)
    (tmp_path / "data").mkdir()
    lock = FakeLock()
    built: list[SecretsBlob] = []
    monkeypatch.setattr(watch_module, "load_secrets", lambda path: FAKE_BLOB)
    monkeypatch.setattr(watch_module, "acquire_single_instance_lock", lock)
    monkeypatch.setattr(
        watch_module,
        "build_client",
        lambda blob: (_ for _ in ()).throw(AssertionError("no client was scripted")),
    )
    # A 5 s poll is the design; a test runs rounds 10 ms apart instead.
    monkeypatch.setattr(watch_module, "build_poll", lambda: HistoryPoll(interval=0.01, rpc_timeout=2.0))
    return Rig(
        config_dir=config_dir,
        db_path=tmp_path / "data" / "state.db",
        secrets_path=tmp_path / "no-session-here.dpapi",
        lock=lock,
        built=built,
    )


def _use_client(rig: Rig, monkeypatch: pytest.MonkeyPatch, client: FakeTelegramClient) -> None:
    def build(blob: SecretsBlob) -> FakeTelegramClient:
        rig.built.append(blob)
        return client

    monkeypatch.setattr(watch_module, "build_client", build)


def _read(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    conn = db.connect_readonly(db_path)
    try:
        return [tuple(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _stamps(db_path: Path) -> dict[str, str | None]:
    conn = db.connect_readonly(db_path)
    try:
        return {beat.name: beat.detail for beat in heartbeats.read_all(conn)}
    finally:
        conn.close()


async def _until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            if predicate():
                return True
        except (sqlite3.Error, FileNotFoundError):
            pass
        await asyncio.sleep(0.01)
    return False
