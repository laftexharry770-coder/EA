"""Every Telegram error a restart cannot fix stops `watch` with the right
code -- wherever it is met -- and never loops.

Two families (ingest/client.py):

- `TERMINAL_AUTH_ERRORS` -> exit 3: every 401 (`UnauthorizedError` --
  logged out, revoked, expired, deactivated, banned, ...),
  `AuthKeyDuplicatedError` and `FrozenMethodInvalidError`. Fix: log in
  again (`setup-login`).
- `API_CREDENTIAL_ERRORS` -> exit 5: the api_id/api_hash is rejected.
  Fix: a new pair from my.telegram.org -- logging in again with the same
  pair cannot help, so this is a setup problem, not a dead session.

Each class is driven through the real path -- `cli_main.main(["watch"])`
-> `Listener` -> `HistoryPoll` -> the store -- at each place Telegram can
say it: at startup (priming `get_dialogs`, or the first backfill read),
from the update loop (`run_until_disconnected()` raising, as Telethon's
does on a 401), and mid-run in the poll after a good round. For each: the
exit code, the stop stamp `status` reads, the CRITICAL alert naming the
fix, and -- mid-run -- that the poll made exactly one failed read and
stopped, rather than logging the same failure every 5 seconds while
`status` said RUNNING (the reviewer measured ~37 failed reads in 0.6 s).

And the other side: errors that look alike but are harmless (a 406 that
is not `AuthKeyDuplicated`, an unrelated 400) must NOT stop the poll.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from telethon import errors

import xaucopier.cli.main as cli_main
from fakes.telegram import FakeTelegramClient
from watch_rig import (  # noqa: F401 -- `rig` is a fixture
    Rig,
    ScriptedWatchClient,
    _stamps,
    _until,
    _use_client,
    rig,
)
from xaucopier.cli import exit_codes
from xaucopier.ingest.client import (
    API_CREDENTIAL_ERRORS,
    PERMANENT_TELEGRAM_ERRORS,
    TERMINAL_AUTH_ERRORS,
    classify_auth_error,
    exit_code_for,
)
from xaucopier.store import alerts, db, heartbeats

#: The reviewer's table: every permanent error `watch` can meet, and its code.
PERMANENT: tuple[tuple[type[Exception], int], ...] = (
    (errors.AuthKeyUnregisteredError, 3),
    (errors.SessionRevokedError, 3),
    (errors.AuthKeyDuplicatedError, 3),
    (errors.AuthKeyInvalidError, 3),
    (errors.SessionExpiredError, 3),
    (errors.UserDeactivatedError, 3),
    (errors.UserDeactivatedBanError, 3),
    (errors.ActiveUserRequiredError, 3),
    (errors.AuthKeyPermEmptyError, 3),
    (errors.SessionPasswordNeededError, 3),
    (errors.FrozenMethodInvalidError, 3),
    (errors.ApiIdInvalidError, 5),
    (errors.ApiIdPublishedFloodError, 5),
    (errors.ConnectionApiIdInvalidError, 5),
)
#: Where Telegram can say it.
WHERE = ("get_dialogs", "backfill", "update loop", "poll")
#: Look alike, but a retry can succeed: never a reason to stop.
HARMLESS: tuple[type[Exception], ...] = (
    errors.FilerefUpgradeNeededError,  # a 406 (AuthKeyError), not AuthKeyDuplicated
    errors.PremiumCurrentlyUnavailableError,  # another 406
    errors.PhoneNumberInvalidError,  # an unrelated 400
)

ALERT_FOR = {3: alerts.TELEGRAM_SESSION_DEAD, 5: alerts.TELEGRAM_API_ID_REJECTED}
FIX_FOR = {3: "setup-login", 5: "my.telegram.org"}
#: How long a poll that swallows the error is given to prove it: at a
#: 10 ms interval that is dozens of rounds.
WATCHDOG_SECONDS = 1.0


@dataclass
class TroubleClient(ScriptedWatchClient):
    """The fake client, failing with `error` at `where`."""

    error: BaseException | None = None
    where: str = ""
    db_path: Path | None = None
    #: Mid-run: reads fail once armed (after one good poll round).
    armed: bool = False
    failed_reads: int = 0
    #: Set if `watch` was still running WATCHDOG_SECONDS after the error.
    still_running_after_error: bool = False

    async def get_dialogs(self, *args: Any, **kwargs: Any) -> Any:
        if self.where == "get_dialogs":
            raise self.error
        return await super().get_dialogs(*args, **kwargs)

    async def get_messages(self, *args: Any, **kwargs: Any) -> Any:
        if self.where == "backfill" or self.armed:
            self.failed_reads += 1
            raise self.error
        return await super().get_messages(*args, **kwargs)

    async def run_until_disconnected(self) -> None:
        if self.where == "update loop":
            self.calls.append("run_until_disconnected")
            raise self.error  # as Telethon's does, from its `_updates_error`
        if self.where == "poll" and self.script_task is None:
            self.script_task = asyncio.create_task(self._fail_mid_run())
        await super().run_until_disconnected()

    async def _fail_mid_run(self) -> None:
        assert self.db_path is not None
        # One good round first: the error meets a watch that was working.
        await _until(lambda: heartbeats.POLL_ROUND in _stamps(self.db_path))
        self.armed = True
        await asyncio.sleep(WATCHDOG_SECONDS)
        # Still here: nothing stopped watch. Stop it, so the test fails
        # on its assertions instead of hanging.
        self.still_running_after_error = True
        await self.disconnect()


def _unacked(db_path: Path) -> list[tuple[str, str]]:
    conn = db.connect_readonly(db_path)
    try:
        return [(a.level, a.code) for a in alerts.unacked(conn)]
    finally:
        conn.close()


@pytest.mark.parametrize("where", WHERE)
@pytest.mark.parametrize(
    ("error_class", "expected"), PERMANENT, ids=[cls.__name__ for cls, _ in PERMANENT]
)
def test_a_permanent_telegram_error_stops_watch_with_its_code_wherever_it_is_met(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_class: type[Exception],
    expected: int,
    where: str,
) -> None:
    client = TroubleClient(error=error_class(request=None), where=where, db_path=rig.db_path)
    _use_client(rig, monkeypatch, client)

    exit_code = cli_main.main(rig.argv())

    assert exit_code == expected
    assert _stamps(rig.db_path)[heartbeats.WATCH_STOPPED] == str(expected)
    assert _unacked(rig.db_path) == [("CRITICAL", ALERT_FOR[expected])]
    assert FIX_FOR[expected] in capsys.readouterr().err
    assert rig.lock.released == 1
    if where == "poll":
        assert not client.still_running_after_error, "the poll kept going after the error"
        assert client.failed_reads == 1, f"{client.failed_reads} failed reads: the poll looped"
    if where == "backfill":
        assert client.failed_reads == 1
        assert "add_event_handler" not in client.calls, "startup went on past the error"


@pytest.mark.parametrize("error_class", HARMLESS, ids=[cls.__name__ for cls in HARMLESS])
def test_a_harmless_lookalike_error_mid_run_does_not_stop_watch(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, error_class: type[Exception]
) -> None:
    client = TroubleClient(error=error_class(request=None), where="poll", db_path=rig.db_path)
    _use_client(rig, monkeypatch, client)

    exit_code = cli_main.main(rig.argv())

    # Only the watchdog's disconnect stopped it: exit 4, restartable.
    assert client.still_running_after_error
    assert client.failed_reads >= 2, "the poll did not retry a retryable error"
    assert exit_code == exit_codes.EXIT_LISTENER_STOPPED
    assert _unacked(rig.db_path) == []


# -- the classification itself --------------------------------------------------------------


@pytest.mark.parametrize(
    ("error_class", "expected"), PERMANENT, ids=[cls.__name__ for cls, _ in PERMANENT]
)
def test_every_error_in_the_table_is_permanent_with_its_code(
    error_class: type[Exception], expected: int
) -> None:
    exc = error_class(request=None)
    assert classify_auth_error(exc) == "terminal"
    assert isinstance(exc, PERMANENT_TELEGRAM_ERRORS)
    assert exit_code_for(exc) == expected


@pytest.mark.parametrize(
    "exc",
    [
        errors.FilerefUpgradeNeededError(request=None),
        errors.PremiumCurrentlyUnavailableError(request=None),
        errors.PhoneNumberInvalidError(request=None),
        errors.FloodWaitError(request=None, capture=30),
        errors.ChannelPrivateError(request=None),
        ConnectionError("dropped"),
        TimeoutError(),
    ],
    ids=lambda exc: type(exc).__name__,
)
def test_retryable_errors_are_not_permanent(exc: Exception) -> None:
    assert classify_auth_error(exc) == "retryable"
    assert exit_code_for(exc) is None


def test_the_two_families_do_not_overlap_and_the_406_base_is_not_used() -> None:
    assert not set(TERMINAL_AUTH_ERRORS) & set(API_CREDENTIAL_ERRORS)
    assert errors.AuthKeyError not in PERMANENT_TELEGRAM_ERRORS
    assert errors.UnauthorizedError in TERMINAL_AUTH_ERRORS


# -- the session alerts clear themselves once Telegram answers again ------------------------


@dataclass
class HealthyThenStop(ScriptedWatchClient):
    """A working client: one good poll round, then it disconnects."""

    db_path: Path | None = None

    async def run_until_disconnected(self) -> None:
        if self.script_task is None:
            self.script_task = asyncio.create_task(self._one_round_then_stop())
        await FakeTelegramClient.run_until_disconnected(self)

    async def _one_round_then_stop(self) -> None:
        assert self.db_path is not None
        await _until(lambda: heartbeats.POLL_ROUND in _stamps(self.db_path))
        await self.disconnect()


@pytest.mark.parametrize(
    "error_class", [errors.SessionRevokedError, errors.ApiIdPublishedFloodError],
    ids=lambda cls: cls.__name__,
)
def test_a_good_read_after_the_fix_clears_the_session_alert(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, error_class: type[Exception]
) -> None:
    """Exit 3 (or 5) leaves a CRITICAL alert. Once the user has fixed it
    (setup-login, or a new api_id) the next watch reads the channel -- and
    that proves the condition is gone, so the alert clears itself instead
    of staying CRITICAL for ever. Other alerts are left alone."""
    _use_client(rig, monkeypatch, TroubleClient(error=error_class(request=None), where="get_dialogs"))
    assert cli_main.main(rig.argv()) in (3, 5)
    conn = db.connect(rig.db_path, role="writer")
    unrelated = alerts.record(
        conn, now_utc=datetime.now(UTC), level="WARNING", code="SOMETHING_ELSE", detail="keep me"
    )
    conn.commit()
    conn.close()
    assert len(_unacked(rig.db_path)) == 2

    _use_client(rig, monkeypatch, HealthyThenStop(db_path=rig.db_path))
    assert cli_main.main(rig.argv()) == exit_codes.EXIT_LISTENER_STOPPED

    assert _unacked(rig.db_path) == [("WARNING", "SOMETHING_ELSE")]
    assert unrelated


def test_a_round_that_reads_nothing_does_not_clear_the_session_alert(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_client(rig, monkeypatch, TroubleClient(error=errors.SessionRevokedError(request=None), where="get_dialogs"))
    assert cli_main.main(rig.argv()) == 3

    # Every read now fails with something retryable: rounds complete (and
    # are stamped) but read 0 channels, which proves nothing.
    client = TroubleClient(
        error=ConnectionError("still no network"), where="poll", db_path=rig.db_path
    )
    _use_client(rig, monkeypatch, client)
    monkeypatch.setattr(client, "armed", True)  # fail from the very first read
    assert cli_main.main(rig.argv()) == exit_codes.EXIT_LISTENER_STOPPED

    assert _unacked(rig.db_path) == [("CRITICAL", alerts.TELEGRAM_SESSION_DEAD)]
