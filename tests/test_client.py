"""Tests for the Telethon client factory and the cross-process guards.

No network: `build_client()` is only ever inspected as a constructed
object, never connected. Uses obviously-fake credentials only
(`api_id=12345`, `api_hash="0"*32`), per the Task 7 brief.
"""
from __future__ import annotations

import pytest
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    SessionRevokedError,
)
from telethon.sessions import StringSession

from xaucopier.ingest.client import (
    EXIT_TERMINAL_AUTH,
    TERMINAL_AUTH_ERRORS,
    AlreadyRunningError,
    acquire_single_instance_lock,
    build_client,
    classify_auth_error,
    session_in_use,
)
from xaucopier.ingest.secrets import SecretsBlob

_FAKE_BLOB = SecretsBlob(api_id=12345, api_hash="0" * 32, session_string="")


# ---------------------------------------------------------------------
# build_client
# ---------------------------------------------------------------------

def test_build_client_uses_string_session() -> None:
    client = build_client(_FAKE_BLOB)
    assert isinstance(client.session, StringSession)


def test_build_client_sets_recognisable_device_model() -> None:
    client = build_client(_FAKE_BLOB)
    assert client._init_request.device_model == "XAUCOPIER-TRADEBOX"


def test_build_client_sets_infinite_connection_retries() -> None:
    client = build_client(_FAKE_BLOB)
    assert client._connection_retries is None


def test_build_client_sets_retry_delay_and_flood_threshold() -> None:
    client = build_client(_FAKE_BLOB)
    assert client._retry_delay == 5
    assert client.flood_sleep_threshold == 120


def test_build_client_enables_catch_up() -> None:
    client = build_client(_FAKE_BLOB)
    assert client._catch_up is True


# ---------------------------------------------------------------------
# single-instance mutex
# ---------------------------------------------------------------------

def test_second_lock_in_same_process_raises_already_running() -> None:
    lock = acquire_single_instance_lock()
    try:
        with pytest.raises(AlreadyRunningError):
            acquire_single_instance_lock()
    finally:
        lock.release()


def test_releasing_first_lock_lets_a_new_one_succeed() -> None:
    lock = acquire_single_instance_lock()
    lock.release()
    lock2 = acquire_single_instance_lock()
    lock2.release()


def test_session_in_use_sees_the_lock_without_taking_it() -> None:
    """`channels` asks whether watch is running; asking must never
    create or hold the mutex, or a watch starting then would be refused."""
    assert session_in_use() is False
    # The probe left nothing behind: the lock can still be taken.
    lock = acquire_single_instance_lock()
    try:
        assert session_in_use() is True
        assert session_in_use() is True  # and asking twice does not change it
    finally:
        lock.release()
    assert session_in_use() is False
    acquire_single_instance_lock().release()


# ---------------------------------------------------------------------
# terminal vs retryable auth errors
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "exc",
    [
        AuthKeyUnregisteredError(request=None),
        SessionRevokedError(request=None),
        AuthKeyDuplicatedError(request=None),
    ],
)
def test_terminal_auth_errors_classify_as_terminal(exc: Exception) -> None:
    assert classify_auth_error(exc) == "terminal"
    assert isinstance(exc, TERMINAL_AUTH_ERRORS)


def test_flood_wait_is_retryable_not_terminal() -> None:
    exc = FloodWaitError(request=None, capture=5)
    assert classify_auth_error(exc) == "retryable"
    assert not isinstance(exc, TERMINAL_AUTH_ERRORS)


def test_exit_terminal_auth_is_3() -> None:
    assert EXIT_TERMINAL_AUTH == 3


# ---------------------------------------------------------------------
# Final review F36: a mutex this process may not open is still "running"
# ---------------------------------------------------------------------

def test_a_mutex_that_cannot_be_opened_means_already_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `watch` started in an administrator window leaves a mutex a
    normal window may not open: CreateMutex fails with access denied.
    That is "already running" (exit 6), with a hint -- not a traceback."""
    import pywintypes
    import winerror

    import xaucopier.ingest.client as client_module

    def denied(*args: object) -> object:
        raise pywintypes.error(winerror.ERROR_ACCESS_DENIED, "CreateMutex", "Access is denied.")

    monkeypatch.setattr(client_module.win32event, "CreateMutex", denied)
    with pytest.raises(AlreadyRunningError, match="administrator") as caught:
        acquire_single_instance_lock()
    assert isinstance(caught.value.__cause__, pywintypes.error)


def test_any_other_mutex_failure_is_not_mistaken_for_already_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pywintypes
    import winerror

    import xaucopier.ingest.client as client_module

    def broken(*args: object) -> object:
        raise pywintypes.error(winerror.ERROR_INVALID_HANDLE, "CreateMutex", "The handle is invalid.")

    monkeypatch.setattr(client_module.win32event, "CreateMutex", broken)
    with pytest.raises(pywintypes.error):
        acquire_single_instance_lock()


# ---------------------------------------------------------------------
# Ledger 145 / final review F06 side note: a bounded connect
# ---------------------------------------------------------------------

class _Connecting:
    """Just enough of a client for `connect_within`."""

    def __init__(self, behaviour: str) -> None:
        self.behaviour = behaviour
        self.cancelled = False

    async def connect(self) -> None:
        import asyncio

        if self.behaviour == "hang":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.behaviour == "refused":
            raise ConnectionRefusedError("refused")


def test_connect_within_gives_up_after_its_deadline_in_plain_words() -> None:
    """With no network (or Telethon reconnecting in a tight loop on a
    malformed reply) connect() never returns. connect_within stops
    waiting, cancels it, and says so plainly."""
    import asyncio

    from xaucopier.ingest.client import TelegramUnreachableError, connect_within

    client = _Connecting("hang")
    with pytest.raises(TelegramUnreachableError) as caught:
        # The outer bound only keeps a broken deadline from hanging the suite.
        asyncio.run(asyncio.wait_for(connect_within(client, 0.05), 5))
    assert str(caught.value).startswith("Could not reach Telegram within 0.05 seconds.")
    assert caught.value.seconds == 0.05
    assert client.cancelled
    assert "RPC" not in str(caught.value) and "client" not in str(caught.value)


def test_connect_within_passes_through_success_and_any_other_error() -> None:
    import asyncio

    from xaucopier.ingest.client import connect_within

    asyncio.run(connect_within(_Connecting("ok"), 5))
    with pytest.raises(ConnectionRefusedError):
        asyncio.run(connect_within(_Connecting("refused"), 5))


def test_the_same_deadline_bounds_any_startup_call() -> None:
    import asyncio

    from xaucopier.ingest.client import TelegramUnreachableError, telegram_within

    async def slow() -> str:
        await asyncio.sleep(10)
        return "late"

    async def quick() -> str:
        return "answered"

    assert asyncio.run(telegram_within(quick(), 5)) == "answered"
    with pytest.raises(TelegramUnreachableError, match="within 0.05 seconds"):
        asyncio.run(telegram_within(slow(), 0.05))
