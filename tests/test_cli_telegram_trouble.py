"""Final review F23, fix round 2: Telegram's own trouble, as the REAL
Telethon client meets it, in `channels add` and `resolve-channels`.

The other tests of these commands raise Telegram's errors from a fake's
`get_dialogs` or `get_entity`. Real Telethon never does that: a server
error (500, -500) or a timeout (-503) goes through its own retry loop
(`TelegramClient._call`), which retries 5 times and then -- unless the
client was built with `raise_last_call_error` -- raises a plain
`ValueError("Request was unsuccessful 6 time(s)")`. That escaped both
commands as a traceback from the channel list, and was reported as "could
not find the channel" for a pasted link. And Telethon's
`is_user_authorized()` reads ANY Telegram error -- a wait, a server error
-- as "not logged in", so both commands told the user to run setup-login
again, which makes a second XAUCOPIER-TRADEBOX session and cures nothing.

Here the real `TelegramClient`, built by the commands' own code, runs
with only the network removed: `connect()` does nothing, and every
request is answered at the MTProto sender -- below Telethon's retry loop,
exactly where a real server's answer arrives -- by
`errors.rpc_message_to_error`, the function Telethon itself uses to turn
Telegram's error into an exception. Telegram is never contacted.
"""
from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from telethon import TelegramClient, errors
from telethon.tl import functions, types

import xaucopier.cli.channels as channels_module
import xaucopier.cli.main as cli_main
import xaucopier.cli.resolve as resolve_module
import xaucopier.ingest.client as client_module
from xaucopier.cli import exit_codes
from xaucopier.ingest.secrets import SecretsBlob

REPO = Path(__file__).resolve().parents[1]
REAL_CHANNELS = REPO / "config" / "channels.toml"

#: This account, as Telegram answers "who am I".
ME = types.User(id=4242, is_self=True, first_name="Me", access_hash=1)

#: What Telegram answers, by request type: a result, or an error as
#: (code, message) -- turned into Telethon's exception the way it does.
Answers = dict[type, Any]

LOGGED_IN: Answers = {
    functions.users.GetUsersRequest: [ME],
    functions.updates.GetStateRequest: types.updates.State(
        pts=1, qts=0, date=None, seq=0, unread_count=0
    ),
}


@pytest.fixture(autouse=True)
def _the_real_config_is_never_touched():
    before = hashlib.sha256(REAL_CHANNELS.read_bytes()).hexdigest()
    yield
    assert hashlib.sha256(REAL_CHANNELS.read_bytes()).hexdigest() == before


def _offline_client_class(answers: Answers) -> type[TelegramClient]:
    class OfflineTelegramClient(TelegramClient):
        """Telethon's own client with no network: requests are answered
        at the sender, below Telethon's retry loop."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._sender.send = self._answer  # type: ignore[method-assign]

        def _answer(self, request: Any, ordered: bool = False) -> asyncio.Future[Any]:
            future = asyncio.get_running_loop().create_future()
            answer = answers.get(type(request))
            if answer is None:
                answer = (400, "NOT_SCRIPTED_IN_THIS_TEST")
            if isinstance(answer, tuple):
                code, message = answer
                future.set_exception(
                    errors.rpc_message_to_error(types.RpcError(code, message), request)
                )
            else:
                future.set_result(answer)
            return future

        async def connect(self) -> None:  # type: ignore[override]
            return None

        def is_connected(self) -> bool:
            return True

        async def disconnect(self) -> None:  # type: ignore[override]
            return None

    return OfflineTelegramClient


class _Lock:
    def __call__(self) -> "_Lock":
        return self

    def release(self) -> None:
        pass


@dataclass
class Command:
    """One of the two commands, run against a temp copy of the config."""

    name: str
    argv: Callable[..., list[str]]
    nothing: str
    channels_path: Path


@pytest.fixture(params=["channels add", "resolve-channels"])
def command(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Command:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ("channels.toml", "accounts.toml"):
        (config_dir / name).write_bytes((REPO / "config" / name).read_bytes())
    blob = SecretsBlob(api_id=1, api_hash="0" * 32, session_string="")
    for module in (channels_module, resolve_module):
        monkeypatch.setattr(module, "load_session", lambda path: blob)
        monkeypatch.setattr(module, "acquire_single_instance_lock", _Lock())
    answers: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": answers.pop(0) if answers else "")
    # Telethon waits 2 s between its retries; the retries themselves are kept.
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda delay, result=None: real_sleep(0, result))
    secrets = str(tmp_path / "session.dpapi")
    if request.param == "channels add":
        def argv(*extra: str) -> list[str]:
            return [
                "channels", "add", "--config-dir", str(config_dir),
                "--db", str(tmp_path / "state.db"), "--secrets", secrets, *extra,
            ]

        return Command("channels add", argv, "Nothing was added.", config_dir / "channels.toml")

    def argv(*extra: str) -> list[str]:
        return ["resolve-channels", "--config-dir", str(config_dir), "--secrets", secrets, *extra]

    return Command("resolve-channels", argv, "Nothing was changed.", config_dir / "channels.toml")


def _use(monkeypatch: pytest.MonkeyPatch, answers: Answers) -> None:
    monkeypatch.setattr(client_module, "TelegramClient", _offline_client_class(answers))


TROUBLE = [
    pytest.param((500, "RPC_CALL_FAIL"), "500", id="500 internal error"),
    pytest.param((-500, "No workers running"), "500", id="-500 no workers"),
    pytest.param((-503, "Timeout"), "503", id="-503 timeout"),
]


@pytest.mark.parametrize(("error", "number"), TROUBLE)
def test_telegrams_own_trouble_while_listing_the_channels_is_one_plain_sentence(
    command: Command,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: tuple[int, str],
    number: str,
) -> None:
    """F23's own scenario: a 500 from the channel list, the user's first
    setup path (press Enter to pick from the list)."""
    _use(monkeypatch, {**LOGGED_IN, functions.messages.GetDialogsRequest: error})
    before = command.channels_path.read_bytes()

    assert cli_main.main(command.argv()) == exit_codes.EXIT_FAILED

    assert command.channels_path.read_bytes() == before
    err = capsys.readouterr().err
    assert f"Telegram could not do this just now (Telegram error {number})" in err
    assert command.nothing in err and "Traceback" not in err


@pytest.mark.parametrize(("error", "number"), TROUBLE)
def test_telegrams_own_trouble_looking_up_a_link_is_never_called_not_found(
    command: Command,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: tuple[int, str],
    number: str,
) -> None:
    _use(monkeypatch, {**LOGGED_IN, functions.contacts.ResolveUsernameRequest: error})
    before = command.channels_path.read_bytes()

    code = cli_main.main(command.argv("--link", "https://t.me/somegold", "--yes"))

    assert code == exit_codes.EXIT_FAILED
    assert command.channels_path.read_bytes() == before
    err = capsys.readouterr().err
    assert f"Telegram could not do this just now (Telegram error {number})" in err
    assert "could not find" not in err.lower() and "could not resolve" not in err.lower()


def test_a_name_telegram_does_not_know_is_still_not_found(
    command: Command, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _use(monkeypatch, {
        **LOGGED_IN,
        functions.contacts.ResolveUsernameRequest: (400, "USERNAME_NOT_OCCUPIED"),
    })

    assert cli_main.main(command.argv("--link", "https://t.me/somegold", "--yes")) == 1

    err = capsys.readouterr().err
    assert "somegold" in err and "Telegram could not do this just now" not in err


LOGIN_CHECK = (functions.users.GetUsersRequest, functions.updates.GetStateRequest)


def test_a_wait_telegram_asks_for_at_the_login_check_is_a_wait_not_a_dead_login(
    command: Command, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """p_auth_flood.py: FLOOD_WAIT_300 answering the login check was "This
    Telegram session is not logged in ... run setup-login again" (exit 3)."""
    _use(monkeypatch, {request: (420, "FLOOD_WAIT_300") for request in LOGIN_CHECK})

    assert cli_main.main(command.argv("--link", "https://t.me/somegold", "--yes")) == 1

    err = capsys.readouterr().err
    assert "Telegram asks to wait about 5 minutes, then run it again" in err
    assert "setup-login" not in err and command.nothing in err


def test_telegrams_own_trouble_at_the_login_check_is_not_a_dead_login(
    command: Command, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _use(monkeypatch, {request: (500, "RPC_CALL_FAIL") for request in LOGIN_CHECK})

    assert cli_main.main(command.argv("--link", "https://t.me/somegold", "--yes")) == 1

    err = capsys.readouterr().err
    assert "Telegram could not do this just now (Telegram error 500)" in err
    assert "setup-login" not in err and "Traceback" not in err


def test_a_login_telegram_no_longer_knows_is_still_exit_3(
    command: Command, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _use(monkeypatch, {request: (401, "AUTH_KEY_UNREGISTERED") for request in LOGIN_CHECK})

    code = cli_main.main(command.argv("--link", "https://t.me/somegold", "--yes"))

    assert code == exit_codes.EXIT_TERMINAL_AUTH
    assert "setup-login" in capsys.readouterr().err
