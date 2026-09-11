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
