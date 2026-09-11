"""The Telethon client factory and the cross-process guards around it.

Two hazards this module exists to prevent:

1. **A second process opening the same session.** The same Telegram auth
   key used from two IPs at once is *permanently* invalidated
   (`AuthKeyDuplicatedError`) -- there is no recovery, only a fresh login.
   An ad-hoc debug script opening `config/secrets/session.dpapi` while the
   real listener is running is exactly this hazard, plus a mundane
   `sqlite3` "database is locked" on top. `acquire_single_instance_lock()`
   takes a named Windows mutex (`Global\\xaucopier_listener`) before
   anything touches the session, so a second process fails fast and
   loudly instead of racing the first one for the socket.
2. **Retrying an error that can never succeed.** `AuthKeyUnregisteredError`,
   `SessionRevokedError`, and `AuthKeyDuplicatedError` all mean the
   session is dead -- logged out remotely, revoked, or duplicated. A
   supervisor that blindly restarts on any non-zero exit would spin
   forever on these. `classify_auth_error` and `EXIT_TERMINAL_AUTH` give
   the supervisor an unambiguous signal to stop trying.
"""
from __future__ import annotations

from typing import Literal

import win32api
import win32event
import winerror
from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    SessionRevokedError,
)
from telethon.sessions import StringSession

from xaucopier.ingest.secrets import SecretsBlob

_MUTEX_NAME = r"Global\xaucopier_listener"

#: Terminal auth errors: the session is dead and no retry can revive it.
TERMINAL_AUTH_ERRORS: tuple[type[Exception], ...] = (
    AuthKeyUnregisteredError,
    SessionRevokedError,
    AuthKeyDuplicatedError,
)

#: Exit code the supervisor must treat as "do not restart".
EXIT_TERMINAL_AUTH = 3


class AlreadyRunningError(Exception):
    """Raised when another process already holds the xaucopier listener
    lock (`Global\\xaucopier_listener`)."""


class SingleInstanceLock:
    """Owns the handle returned by `win32event.CreateMutex`.

    `release()` closes the handle, which drops this process's hold on the
    named mutex -- letting a subsequent `acquire_single_instance_lock()`
    (in this process or another) succeed again. Not a context manager on
    its own terms because the production lifetime is "held for the whole
    process", released implicitly by OS handle cleanup on exit; tests
    that need a deterministic release call `release()` explicitly.
    """

    __slots__ = ("_handle",)

    def __init__(self, handle: object) -> None:
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            win32api.CloseHandle(self._handle)
            self._handle = None


def acquire_single_instance_lock() -> SingleInstanceLock:
    """Take the named `Global\\xaucopier_listener` mutex.

    Raises `AlreadyRunningError` if another handle to it is already open
    anywhere on the system (including this process). Must be called
    before `build_client()` opens the session.
    """
    handle = win32event.CreateMutex(None, False, _MUTEX_NAME)
    already_exists = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    if already_exists:
        win32api.CloseHandle(handle)
        raise AlreadyRunningError(
            f"xaucopier listener is already running (mutex {_MUTEX_NAME!r} "
            "is held). A second process opening the same Telegram session "
            "risks a locked database, or worse, a duplicated auth key that "
            "permanently invalidates the session."
        )
    return SingleInstanceLock(handle)


def build_client(blob: SecretsBlob) -> TelegramClient:
    """Construct a `TelegramClient` from `blob`. Does not connect.

    `device_model="XAUCOPIER-TRADEBOX"` makes the session identifiable in
    the user's Telegram -> Settings -> Devices, so it is not mistaken for
    an unknown session and logged out by accident. `connection_retries`
    is infinite and `flood_sleep_threshold` generous (120s) because a
    dropped connection or a flood wait must be waited out, never treated
    as fatal by this layer -- only the terminal auth errors are.
    """
    return TelegramClient(
        StringSession(blob.session_string),
        blob.api_id,
        blob.api_hash,
        connection_retries=None,
        retry_delay=5,
        flood_sleep_threshold=120,
        device_model="XAUCOPIER-TRADEBOX",
        system_version="Windows",
        app_version="0.1",
        catch_up=True,
    )


def classify_auth_error(exc: BaseException) -> Literal["terminal", "retryable"]:
    """`"terminal"` for the three dead-session errors, `"retryable"` for
    everything else (e.g. `FloodWaitError`, network errors)."""
    return "terminal" if isinstance(exc, TERMINAL_AUTH_ERRORS) else "retryable"
