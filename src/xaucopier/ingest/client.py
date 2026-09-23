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
2. **Retrying an error that can never succeed.** Two families of
   Telegram error mean a restart cannot help, and a supervisor that
   restarted on them would loop for ever:

   - `TERMINAL_AUTH_ERRORS` -> `EXIT_TERMINAL_AUTH` (3): the session or
     the account is dead. Every 401 (`UnauthorizedError`: logged out,
     revoked, expired, a deactivated or banned account, ...) -- the same
     rule Telethon 1.44's own update loop stops on
     (telethon/client/updates.py:328, :373) -- plus
     `AuthKeyDuplicatedError` (406: the key was used from two places at
     once) and `FrozenMethodInvalidError` (420: a frozen account). *Not*
     the `AuthKeyError` base class: that is every 406, including harmless
     ones such as `FilerefUpgradeNeededError`.
   - `API_CREDENTIAL_ERRORS` -> `EXIT_CONFIG_ERROR` (5): Telegram rejects
     this program's api_id/api_hash (invalid, or published somewhere --
     the tutorial-id ban). Logging in again with the same pair cannot fix
     it; the user needs a new pair from my.telegram.org, so this is a
     setup problem, not a dead session. Telethon's update loop treats
     these 400s as transient (updates.py:344), so only this program's own
     reads can notice them.

   `PERMANENT_TELEGRAM_ERRORS` is both; `exit_code_for()` maps one to its
   code.
"""
from __future__ import annotations

from typing import Literal

import pywintypes
import win32api
import win32con
import win32event
import winerror
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from xaucopier.ingest.secrets import SecretsBlob

_MUTEX_NAME = r"Global\xaucopier_listener"

#: The session or the account is dead and no retry can revive it (see
#: the module docstring for why these three and not `AuthKeyError`).
TERMINAL_AUTH_ERRORS: tuple[type[Exception], ...] = (
    errors.UnauthorizedError,  # every 401
    errors.AuthKeyDuplicatedError,  # 406 -- not the AuthKeyError base
    errors.FrozenMethodInvalidError,  # 420, a frozen account
)

#: Telegram rejects this program's api_id/api_hash: a new pair is needed.
API_CREDENTIAL_ERRORS: tuple[type[Exception], ...] = (
    errors.ApiIdInvalidError,
    errors.ApiIdPublishedFloodError,
    errors.ConnectionApiIdInvalidError,
)

#: Every Telegram error after which retrying cannot help.
PERMANENT_TELEGRAM_ERRORS: tuple[type[Exception], ...] = (
    TERMINAL_AUTH_ERRORS + API_CREDENTIAL_ERRORS
)

#: Exit code the supervisor must treat as "do not restart": the session is
#: dead; run setup-login.
EXIT_TERMINAL_AUTH = 3
#: Exit code for a permanent setup problem -- here, an api_id/api_hash
#: Telegram rejects. The CLI uses the same code for every other permanent
#: setup problem (cli/exit_codes.py). Do not restart.
EXIT_CONFIG_ERROR = 5


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


def session_in_use() -> bool:
    """True if some process holds the `Global\\xaucopier_listener` mutex
    right now -- `watch`, `resolve-channels` or `channels add`.

    For commands that only *report* whether `watch` is running (`channels`):
    it never creates the mutex and never takes it. `OpenMutex` finds it
    only if another process has it open, and the handle is closed at once.
    With no mutex there is no handle at all, so a `watch` starting at that
    moment is unaffected; with one, a second `watch` would be refused
    anyway."""
    try:
        handle = win32event.OpenMutex(win32con.SYNCHRONIZE, False, _MUTEX_NAME)
    except pywintypes.error as exc:
        if exc.winerror == winerror.ERROR_FILE_NOT_FOUND:
            return False
        if exc.winerror == winerror.ERROR_ACCESS_DENIED:
            return True  # it exists; a process of another user holds it
        raise
    win32api.CloseHandle(handle)
    return True


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
    """`"terminal"` for every `PERMANENT_TELEGRAM_ERRORS` error,
    `"retryable"` for everything else (e.g. `FloodWaitError`, a harmless
    406, network errors)."""
    return "terminal" if isinstance(exc, PERMANENT_TELEGRAM_ERRORS) else "retryable"


def exit_code_for(exc: BaseException) -> int | None:
    """The exit code a permanent Telegram error means -- 3 for a dead
    session or account, 5 for a rejected api_id/api_hash -- or None if a
    retry could still succeed."""
    if isinstance(exc, TERMINAL_AUTH_ERRORS):
        return EXIT_TERMINAL_AUTH
    if isinstance(exc, API_CREDENTIAL_ERRORS):
        return EXIT_CONFIG_ERROR
    return None
