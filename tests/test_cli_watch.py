"""Tests for `xaucopier watch` -- the deliverable -- and its exit codes.

`watch` runs end to end here: the real CLI entry point, the real config
loaders on a temporary copy of the config, the real listener, the real
history poll, the real store. Only Telegram is faked
(`tests/fakes/telegram.py`) -- never the network -- and the three things
that touch the machine are swapped for test doubles: the DPAPI session
file, the system-wide single-instance mutex, and the poll's 5-second
interval (shortened, so a test does not wait five real seconds).

Two scenarios carry the weight:

- **starts and stops cleanly** -- a live signal arrives through the update
  stream, then a post is hidden by a blackout and must be recovered by
  the poll (proving `watch` wired the poll in, not just the listener),
  then the client disconnects: exit 4, and everything is torn down --
  writer thread, Telegram client, lock, log handlers;
- **Ctrl+C** -- a real SIGINT, through `asyncio.run`'s own handler: exit
  130, the same clean teardown.

And every permanent setup problem must exit 5 -- not 4, which a
supervisor restarts -- without connecting to anything.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from telethon import errors

import xaucopier.cli.main as cli_main
import xaucopier.cli.watch as watch_module
from fakes.telegram import FakeMessage, FakeTelegramClient
from xaucopier.cli import exit_codes
from xaucopier.ingest.client import AlreadyRunningError, acquire_single_instance_lock
from xaucopier.ingest.poll import (
    DEFAULT_POLL_INTERVAL,
    MISSED_BY_EVENTSTREAM_METRIC,
    HistoryPoll,
)
from xaucopier.ingest.secrets import SecretsBlob
from xaucopier.store import alerts, db, heartbeats

from watch_rig import (  # noqa: F401 -- `rig` is a fixture
    REPO,
    FIXTURE,
    SIGNAL_TEXT,
    CHANNEL_ID,
    CHATTER,
    ENABLED_CHANNEL,
    FAKE_BLOB,
    ScriptedWatchClient,
    FakeLock,
    Rig,
    _write_config,
    rig,
    _use_client,
    _read,
    _stamps,
    _until,
)

def _recovered_by_the_poll(db_path: Path) -> bool:
    return (102, "poll") in _read(
        db_path, "SELECT message_id, source FROM messages WHERE channel_id = ?", (CHANNEL_ID,)
    ) and heartbeats.POLL_ROUND in _stamps(db_path)


def _scenario(db_path: Path, stop: Callable[[ScriptedWatchClient], Awaitable[None]]):
    """A live signal (101), then a post the update stream never delivers
    (102, a blackout), then `stop` -- once the poll has recovered 102, or
    after 5 s if it never does (so a watch wired without the poll fails
    its assertions instead of hanging)."""

    async def script(client: ScriptedWatchClient) -> None:
        now = datetime.now(UTC)
        await client.emit(CHANNEL_ID, FakeMessage(id=101, message=SIGNAL_TEXT, date=now))
        with client.blackout():
            await client.emit(CHANNEL_ID, FakeMessage(id=102, message=CHATTER, date=now))
        await _until(lambda: _recovered_by_the_poll(db_path))
        await stop(client)

    return script


def _history() -> dict[int, list[FakeMessage]]:
    # An hour-old post: the first-run seed anchors on it, so nothing
    # before the listener started is ingested.
    old = datetime.now(UTC) - timedelta(hours=1)
    return {CHANNEL_ID: [FakeMessage(id=100, message=CHATTER, date=old)]}


def _writer_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "xaucopier-writer" and t.is_alive()]


def _assert_recorded_and_torn_down(rig: Rig, client: ScriptedWatchClient, stopped_with: int) -> None:
    stored = _read(
        rig.db_path,
        "SELECT message_id, source, kind FROM messages WHERE channel_id = ? ORDER BY message_id",
        (CHANNEL_ID,),
    )
    assert stored == [(101, "live", "signal"), (102, "poll", "skip")]
    assert _read(rig.db_path, "SELECT SUM(value) FROM metrics WHERE name = ?",
                 (MISSED_BY_EVENTSTREAM_METRIC,)) == [(1,)]
    # The signal is recorded and queued for the (future) trading step --
    # one row per enabled account in the real accounts.toml -- and nothing
    # has claimed it: watch places no orders.
    assert _read(rig.db_path, "SELECT account_id, kind, state FROM outbox") == [
        ("ic_demo", "ENTRY", "QUEUED")
    ]
    stamps = _stamps(rig.db_path)
    assert stamps[heartbeats.POLL_ROUND] == "1 of 1 channel(s) read"
    assert heartbeats.channel_read_name(CHANNEL_ID) in stamps
    assert heartbeats.WATCH_STARTED in stamps
    assert stamps[heartbeats.WATCH_STOPPED] == str(stopped_with)

    # Clean teardown: client disconnected, writer thread gone, lock released.
    assert "disconnect" in client.calls
    assert _writer_threads() == []
    assert rig.lock.acquired == 1 and rig.lock.released == 1
    assert rig.built == [FAKE_BLOB]


# -- starts and stops cleanly ----------------------------------------------------------


def test_watch_records_live_and_poll_recovered_messages_and_stops_cleanly_on_disconnect(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def disconnect(client: ScriptedWatchClient) -> None:
        await client.disconnect()

    client = ScriptedWatchClient(history=_history(), script=_scenario(rig.db_path, disconnect))
    _use_client(rig, monkeypatch, client)
    handlers_before = list(logging.getLogger().handlers)

    exit_code = cli_main.main(rig.argv())

    assert exit_code == exit_codes.EXIT_LISTENER_STOPPED == 4
    _assert_recorded_and_torn_down(rig, client, stopped_with=4)
    assert logging.getLogger().handlers == handlers_before
    out = capsys.readouterr().out
    assert "WATCH-ONLY" in out and "places NO orders" in out
    assert f"id {CHANNEL_ID}" in out
    assert "exit code 4" in out
    log = (rig.db_path.parent / "watch.log").read_text(encoding="utf-8")
    assert "poll recovered 1 message(s)" in log


def test_ctrl_c_stops_watch_cleanly_with_exit_130(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real SIGINT, handled by `asyncio.run` exactly as a Ctrl+C in the
    console is: the listener is cancelled and torn down, then
    KeyboardInterrupt, which `watch` turns into exit 130."""

    async def press_ctrl_c(client: ScriptedWatchClient) -> None:
        signal.raise_signal(signal.SIGINT)

    client = ScriptedWatchClient(history=_history(), script=_scenario(rig.db_path, press_ctrl_c))
    _use_client(rig, monkeypatch, client)

    exit_code = cli_main.main(rig.argv())

    assert exit_code == exit_codes.EXIT_INTERRUPTED == 130
    _assert_recorded_and_torn_down(rig, client, stopped_with=130)
    assert "exit code 130" in capsys.readouterr().out


def test_watch_runs_the_real_poll_with_its_designed_defaults() -> None:
    """The tests above shorten the interval; this pins what `watch` really
    runs -- the 5-second poll, with Task 10's edit window."""
    poll = watch_module.build_poll()

    assert isinstance(poll, HistoryPoll)
    assert poll == HistoryPoll()
    assert poll.interval == DEFAULT_POLL_INTERVAL == 5.0


# -- exit 3: the session is dead ----------------------------------------------------------


def test_a_dead_session_exits_3_and_leaves_a_critical_alert_for_status(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    @dataclass
    class RevokedClient(FakeTelegramClient):
        async def get_dialogs(self, *args, **kwargs):
            raise errors.AuthKeyUnregisteredError(request=None)

    client = RevokedClient()
    _use_client(rig, monkeypatch, client)

    exit_code = cli_main.main(rig.argv())

    assert exit_code == 3
    assert _stamps(rig.db_path)[heartbeats.WATCH_STOPPED] == "3"
    conn = db.connect_readonly(rig.db_path)
    try:
        unacked = alerts.unacked(conn)
    finally:
        conn.close()
    assert [(a.level, a.code) for a in unacked] == [("CRITICAL", watch_module.TELEGRAM_SESSION_DEAD)]
    assert "setup-login" in unacked[0].detail
    assert rig.lock.released == 1


def test_an_unexpected_error_is_exit_4_restartable_not_a_traceback(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    @dataclass
    class BrokenClient(FakeTelegramClient):
        async def connect(self):
            raise RuntimeError("something nobody anticipated")

    _use_client(rig, monkeypatch, BrokenClient())

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_LISTENER_STOPPED
    assert _stamps(rig.db_path)[heartbeats.WATCH_STOPPED] == "4"
    assert _writer_threads() == []
    assert rig.lock.released == 1


# -- exit 5: permanent setup problems -----------------------------------------------------


def _missing_db_folder(rig: Rig) -> None:
    rig.db_path = rig.db_path.parent / "no-such-folder" / "state.db"


def _malformed_toml(rig: Rig) -> None:
    (rig.config_dir / "channels.toml").write_text("[[channels]\nname = ", encoding="utf-8")


def _missing_key(rig: Rig) -> None:
    (rig.config_dir / "channels.toml").write_text(
        ENABLED_CHANNEL.replace('parser     = "mmh"\n', ""), encoding="utf-8"
    )


def _enabled_with_id_0(rig: Rig) -> None:
    (rig.config_dir / "channels.toml").write_text(
        ENABLED_CHANNEL.replace(str(CHANNEL_ID), "0"), encoding="utf-8"
    )


def _shipped_placeholder_config(rig: Rig) -> None:
    # The repository's own channels.toml, as shipped: one channel, id 0,
    # switched off -- nothing to watch until resolve-channels has run.
    (rig.config_dir / "channels.toml").write_text(
        (REPO / "config" / "channels.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )


def _missing_accounts(rig: Rig) -> None:
    (rig.config_dir / "accounts.toml").unlink()


def _duplicate_channel(rig: Rig) -> None:
    (rig.config_dir / "channels.toml").write_text(
        ENABLED_CHANNEL + ENABLED_CHANNEL.replace("ordinal    = 1", "ordinal    = 2"),
        encoding="utf-8",
    )


def _db_path_is_a_folder(rig: Rig) -> None:
    rig.db_path.mkdir()


PRE_LOCK_PROBLEMS = {
    "missing database folder": _missing_db_folder,
    "malformed channels.toml": _malformed_toml,
    "channels.toml missing a required key": _missing_key,
    "a channel enabled with channel_id 0": _enabled_with_id_0,
    "no channel switched on (the shipped placeholder)": _shipped_placeholder_config,
    "missing accounts.toml": _missing_accounts,
    "one channel id switched on twice": _duplicate_channel,
    "database path is a folder": _db_path_is_a_folder,
}


@pytest.mark.parametrize("problem", sorted(PRE_LOCK_PROBLEMS))
def test_each_permanent_setup_problem_exits_5_before_touching_anything(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], problem: str
) -> None:
    PRE_LOCK_PROBLEMS[problem](rig)
    files_before = sorted(p.name for p in rig.db_path.parent.parent.rglob("*"))

    exit_code = cli_main.main(rig.argv())

    assert exit_code == exit_codes.EXIT_CONFIG_ERROR == 5
    assert rig.built == [], "a client was built despite a setup problem"
    assert rig.lock.acquired == 0
    assert sorted(p.name for p in rig.db_path.parent.parent.rglob("*")) == files_before
    err = capsys.readouterr().err
    assert "cannot start" in err and "exit code 5" in err


def test_a_missing_session_file_exits_5_and_says_to_run_setup_login(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from xaucopier.ingest.secrets import load

    monkeypatch.setattr(watch_module, "load_secrets", load)  # the real one

    assert cli_main.main(rig.argv()) == 5
    assert rig.built == [] and rig.lock.acquired == 0
    assert "setup-login" in capsys.readouterr().err


def test_a_database_file_that_is_not_a_database_exits_5_and_releases_the_lock(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.db_path.write_bytes(b"this is not an sqlite database" * 100)

    assert cli_main.main(rig.argv()) == 5
    assert rig.built == []
    assert rig.lock.acquired == 1 and rig.lock.released == 1
    assert "cannot be used" in capsys.readouterr().err


def test_exit_5_is_its_own_code_and_not_restartable() -> None:
    assert exit_codes.EXIT_CONFIG_ERROR not in (
        exit_codes.EXIT_TERMINAL_AUTH,
        exit_codes.EXIT_LISTENER_STOPPED,
        exit_codes.EXIT_ALREADY_RUNNING,
    )
    assert exit_codes.RESTARTABLE == frozenset({exit_codes.EXIT_LISTENER_STOPPED})
    assert len({code for code, _ in exit_codes.MEANINGS}) == len(exit_codes.MEANINGS)


def test_help_documents_every_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["--help"], ["watch", "--help"]):
        with pytest.raises(SystemExit):
            cli_main.main(argv)
        text = capsys.readouterr().out
        for code, meaning in exit_codes.MEANINGS:
            assert f"{code:>3}  {meaning}" in text, (argv, code)


# -- exit 6: already running ---------------------------------------------------------------


def test_a_second_watch_exits_6_without_building_a_client(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def held() -> None:
        raise AlreadyRunningError("held elsewhere")

    monkeypatch.setattr(watch_module, "acquire_single_instance_lock", held)

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_ALREADY_RUNNING == 6
    assert rig.built == []
    assert not rig.db_path.exists()


def test_the_real_single_instance_mutex_is_what_watch_takes(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not the fake: hold the real system-wide mutex, and watch -- using
    the real `acquire_single_instance_lock` -- must refuse to start."""
    monkeypatch.setattr(watch_module, "acquire_single_instance_lock", acquire_single_instance_lock)
    held = acquire_single_instance_lock()
    try:
        assert cli_main.main(rig.argv()) == 6
    finally:
        held.release()
    assert rig.built == []


# -- ledger 145: no network at start-up ------------------------------------------------


@dataclass
class _NoNetworkClient(ScriptedWatchClient):
    """connect() never returns -- no network, or Telethon reconnecting in a
    tight loop on a malformed reply (final review F06, side note)."""

    async def connect(self) -> None:
        self.calls.append("connect")
        await asyncio.Event().wait()


def test_watch_that_cannot_reach_telegram_stops_with_exit_4_and_says_so(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Before: start() waited on connect() for ever, `status` showing
    STARTING. Now the start-up deadline (60 s; 0.1 s here) stops watch
    with exit 4 -- restartable -- and a plain message."""
    from xaucopier.ingest import client as client_module

    monkeypatch.setattr(client_module, "STARTUP_TIMEOUT_SECONDS", 0.1)
    client = _NoNetworkClient()
    _use_client(rig, monkeypatch, client)

    exit_code = cli_main.main(rig.argv())

    assert exit_code == exit_codes.EXIT_LISTENER_STOPPED
    assert _stamps(rig.db_path)[heartbeats.WATCH_STOPPED] == "4"
    err = capsys.readouterr().err
    assert "Could not reach Telegram within 0.1 seconds" in err
    assert "add_event_handler" not in client.calls
    assert rig.lock.released == 1


# -- F32: a channel name the output cannot show -----------------------------------------------


def test_an_emoji_channel_name_never_crashes_the_banner_when_output_is_redirected(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirected to a file, Python writes cp1252 on Windows, which has no
    emoji; `channels add` copies Telegram titles verbatim. Before: the
    banner raised UnicodeEncodeError -- a traceback, exit 1, watch_stopped
    never stamped. Now every command's output replaces what it cannot show."""
    import io
    import sys

    name = "\U0001F947 GOLD VIP SIGNALS \U0001F4C8"
    _write_config(rig.config_dir, ENABLED_CHANNEL.replace('"Test Channel"', f'"{name}"'))
    out_bytes, err_bytes = io.BytesIO(), io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out_bytes, encoding="cp1252"))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(err_bytes, encoding="cp1252"))

    def run(setup, blob) -> int:
        # watch's log lines name channels too (a group source, a slow read),
        # and they go to stderr.
        logging.getLogger("xaucopier.ingest.listener").warning("%s is a group", name)
        return exit_codes.EXIT_LISTENER_STOPPED

    monkeypatch.setattr(watch_module, "_run", run)

    exit_code = cli_main.main(rig.argv())
    sys.stdout.flush()
    sys.stderr.flush()

    assert exit_code == exit_codes.EXIT_LISTENER_STOPPED
    out = out_bytes.getvalue().decode("cp1252")
    err = err_bytes.getvalue().decode("cp1252")
    assert "watching: ? GOLD VIP SIGNALS ?" in out
    assert "watch stopped (exit code 4" in out
    assert "? GOLD VIP SIGNALS ? is a group" in err
    assert "Traceback" not in err and "Logging error" not in err
    assert _stamps(rig.db_path)[heartbeats.WATCH_STOPPED] == "4"
    assert rig.lock.released == 1


def test_a_stream_that_cannot_be_reconfigured_is_left_as_it_is(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Some stand-ins for stdout have no reconfigure(); main() must still run."""
    import sys

    class Plain:
        def __init__(self) -> None:
            self.text: list[str] = []

        def write(self, text: str) -> int:
            self.text.append(text)
            return len(text)

        def flush(self) -> None:
            pass

    class Refusing(Plain):
        def reconfigure(self, **kwargs: object) -> None:
            raise ValueError("I/O operation on closed file")

    plain, refusing = Plain(), Refusing()
    monkeypatch.setattr(sys, "stdout", plain)
    monkeypatch.setattr(sys, "stderr", refusing)
    with pytest.raises(SystemExit):
        cli_main.main(["--help"])
    assert "exit codes:" in "".join(plain.text)
