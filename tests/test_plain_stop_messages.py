"""Group-2 review leftover G2L-2: what the watch window says when watch
stops is plain; the technical detail goes to the log file only.

Every log line also shows in the watch window, so a stop used to read
"the poll task died (WriterUnresponsiveError: the writer thread did not
run a no-op within 60s ...; ... the poll is stopping the listener)" or
"run_until_disconnected() returned: the listener is no longer receiving
Telegram updates". Now the window says what happened in the user's words
("The database stopped answering for 60 seconds ...", "The connection to
Telegram ended ..."), and those technical lines are written to watch.log
only.

`watch` runs end to end here on the fake rig (tests/watch_rig.py):
Telegram is faked, never contacted.
"""
from __future__ import annotations

import concurrent.futures
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import xaucopier.cli.main as cli_main
import xaucopier.cli.watch as watch_module
from fakes.telegram import FakeMessage
from xaucopier.cli import exit_codes
from xaucopier.ingest.poll import HistoryPoll
from xaucopier.store import db
from xaucopier.store.db import LOG_FILE_ONLY

from watch_rig import (  # noqa: F401 -- `rig` is a fixture
    CHANNEL_ID,
    CHATTER,
    Rig,
    ScriptedWatchClient,
    _use_client,
    rig,
)

#: Words that mean nothing to the user, in anything the watch window says
#: about why watch stopped.
JARGON = ("listener", "poll task", "writer", "no-op", "thread", "run_until_disconnected")


def _history() -> dict[int, list[FakeMessage]]:
    old = datetime.now(UTC) - timedelta(hours=1)
    return {CHANNEL_ID: [FakeMessage(id=100, message=CHATTER, date=old)]}


def _window_and_log(rig: Rig, capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    captured = capsys.readouterr()
    log = (rig.db_path.parent / "watch.log").read_text(encoding="utf-8")
    return captured.out + captured.err, log


def _stop_lines(window: str) -> list[str]:
    """What the window says about the stop: the message of every ERROR or
    CRITICAL log line (after "<level> <logger name>: "), and watch's own
    lines."""
    said = []
    for line in window.splitlines():
        for level in (" ERROR ", " CRITICAL "):
            if level in line:
                said.append(line.split(level, 1)[1].strip().split(": ", 1)[1])
        if line.startswith("watch stopped"):
            said.append(line)
    return said


def _assert_plain(window: str) -> None:
    said = _stop_lines(window)
    assert said, window
    for line in said:
        for word in JARGON:
            assert word not in line.lower(), (word, line)


def test_a_connection_that_ended_is_said_plainly_and_the_detail_is_in_the_log_file(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def disconnect(client: ScriptedWatchClient) -> None:
        await client.disconnect()

    _use_client(rig, monkeypatch, ScriptedWatchClient(history=_history(), script=disconnect))

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_LISTENER_STOPPED

    window, log = _window_and_log(rig, capsys)
    assert "The connection to Telegram ended" in window
    _assert_plain(window)
    assert "run_until_disconnected() returned" in log


class _HangingWriter(db.Writer):
    """A writer whose thread stops serving jobs once `hang` is set."""

    made: list["_HangingWriter"] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.hang = False
        _HangingWriter.made.append(self)

    def submit(self, fn, **kwargs):  # type: ignore[no-untyped-def]
        if self.hang:
            return concurrent.futures.Future()
        return super().submit(fn, **kwargs)


def test_a_database_that_stopped_answering_is_said_plainly_and_the_detail_is_in_the_log_file(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The case G2L-2 names: WriterUnresponsiveError, extended in 516f5e0."""
    _HangingWriter.made.clear()
    monkeypatch.setattr(watch_module.db, "Writer", _HangingWriter)
    monkeypatch.setattr(
        watch_module,
        "build_poll",
        lambda: HistoryPoll(interval=0.01, rpc_timeout=2.0, writer_probe_timeout=0.2),
    )

    async def hang(client: ScriptedWatchClient) -> None:
        _HangingWriter.made[-1].hang = True

    _use_client(rig, monkeypatch, ScriptedWatchClient(history=_history(), script=hang))

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_LISTENER_STOPPED

    window, log = _window_and_log(rig, capsys)
    assert "The database stopped answering for 0.2 seconds" in window
    _assert_plain(window)
    assert "did not run a no-op" in log and "stopping the listener" in log


def test_an_error_nobody_expected_is_one_plain_line_in_the_window_and_a_traceback_in_the_log(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def broken(client: object, setup: object) -> int:
        raise RuntimeError("internal detail the user cannot use")

    monkeypatch.setattr(watch_module, "run_watch", broken)
    _use_client(rig, monkeypatch, ScriptedWatchClient(history=_history()))

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_LISTENER_STOPPED

    window, log = _window_and_log(rig, capsys)
    assert "did not expect" in window and "watch.log" in window
    assert "Traceback" not in window and "internal detail" not in window
    assert "Traceback" in log and "internal detail the user cannot use" in log


def test_a_line_for_the_log_file_only_never_shows_in_the_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_path = tmp_path / "watch.log"
    stop = watch_module.start_logging(log_path)
    try:
        logger = logging.getLogger("xaucopier.test")
        logger.error("for the user")
        logger.error("for whoever reads the log file", extra=LOG_FILE_ONLY)
    finally:
        stop()

    window = capsys.readouterr().err
    log = log_path.read_text(encoding="utf-8")
    assert "for the user" in window and "for the user" in log
    assert "for whoever reads the log file" not in window
    assert "for whoever reads the log file" in log


def test_with_no_log_file_the_detail_shows_in_the_window_rather_than_nowhere(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stop = watch_module.start_logging(tmp_path / "no-such-folder" / "watch.log")
    try:
        logging.getLogger("xaucopier.test").error("the detail", extra=LOG_FILE_ONLY)
    finally:
        stop()

    assert "the detail" in capsys.readouterr().err


def _kill_the_writer(conn: object) -> None:
    raise SystemExit("simulated writer-thread death")


def test_a_database_writer_that_died_is_said_plainly_and_the_detail_is_in_the_log_file(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _HangingWriter.made.clear()
    monkeypatch.setattr(watch_module.db, "Writer", _HangingWriter)

    async def kill(client: ScriptedWatchClient) -> None:
        _HangingWriter.made[-1].submit(_kill_the_writer)

    _use_client(rig, monkeypatch, ScriptedWatchClient(history=_history(), script=kill))

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_LISTENER_STOPPED

    window, log = _window_and_log(rig, capsys)
    assert "Saving to the database stopped working" in window
    _assert_plain(window)
    assert "the writer thread is dead" in log and "the writer thread died" in log


def test_a_stop_already_in_plain_words_is_written_once(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Could not reach Telegram at start: its message is plain already, so
    the log file gets it once, not once more as "detail"."""
    import asyncio

    from xaucopier.ingest import client as client_module

    class NoNetwork(ScriptedWatchClient):
        async def connect(self) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(client_module, "STARTUP_TIMEOUT_SECONDS", 0.1)
    _use_client(rig, monkeypatch, NoNetwork(history=_history()))

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_LISTENER_STOPPED

    window, log = _window_and_log(rig, capsys)
    assert window.count("Could not reach Telegram within 0.1 seconds") == 1
    assert log.count("Could not reach Telegram within 0.1 seconds") == 1
