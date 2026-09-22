"""Tests for `xaucopier setup-login`.

No network, ever: `build_client` is monkeypatched with a fake client
whose `.start()` either "succeeds" or raises a terminal auth error, so
the whole login flow is exercised without touching Telethon or
Telegram. `input`/`getpass.getpass` are monkeypatched too -- the real
login is interactive and must be run by a human in a console; these
tests only prove the CLI wires prompts -> build_client -> secrets.save
together correctly. Every value below is an obviously-fake placeholder.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from telethon.errors import SessionRevokedError

import xaucopier.cli.main as cli_main
from xaucopier.ingest.secrets import load

_FAKE_API_ID = "12345"
_FAKE_API_HASH = "0" * 32
_FAKE_PHONE = "+10000000000"
_FAKE_SESSION_AFTER_LOGIN = "NOT_A_REAL_SESSION_AFTER_LOGIN_" + "y" * 40


class _FakeSession:
    def __init__(self, value: str) -> None:
        self._value = value

    def save(self) -> str:
        return self._value


class _FakeClientSuccess:
    """Stands in for telethon.TelegramClient: start() "succeeds" without
    any network, and session.save() returns a fixed fake string."""

    def __init__(self) -> None:
        self.session = _FakeSession(_FAKE_SESSION_AFTER_LOGIN)
        self.start_kwargs: dict[str, Any] = {}
        self.disconnected = False

    async def start(self, **kwargs: Any) -> "_FakeClientSuccess":
        self.start_kwargs = kwargs
        return self

    async def disconnect(self) -> None:
        self.disconnected = True


class _FakeClientTerminalFailure:
    """Stands in for a dead/revoked session: start() raises a terminal
    auth error, exactly as a real client would for a logged-out session."""

    def __init__(self) -> None:
        self.session = _FakeSession("")
        self.disconnected = False

    async def start(self, **kwargs: Any) -> None:
        raise SessionRevokedError(request=None)

    async def disconnect(self) -> None:
        self.disconnected = True


def _answers(monkeypatch: pytest.MonkeyPatch, target: str, values: list[str]) -> None:
    it = iter(values)
    monkeypatch.setattr(target, lambda *_args, **_kwargs: next(it))


def test_setup_login_success_writes_secrets_blob(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_client = _FakeClientSuccess()
    monkeypatch.setattr(cli_main, "build_client", lambda blob: fake_client)
    _answers(monkeypatch, "builtins.input", [_FAKE_API_ID, _FAKE_PHONE])
    _answers(monkeypatch, "getpass.getpass", [_FAKE_API_HASH])

    target = tmp_path / "session.dpapi"
    exit_code = cli_main.main(["setup-login", "--path", str(target)])

    assert exit_code == 0
    blob = load(target)
    assert blob.api_id == 12345
    assert blob.api_hash == _FAKE_API_HASH
    assert blob.session_string == _FAKE_SESSION_AFTER_LOGIN
    # phone/code_callback/password were wired into client.start(), never
    # skipped -- but the phone number itself is not asserted verbatim
    # anywhere in a way that could tempt copy-pasting a real one into a
    # future test.
    assert "phone" in fake_client.start_kwargs
    assert "code_callback" in fake_client.start_kwargs
    assert "password" in fake_client.start_kwargs
    assert fake_client.disconnected is True


def test_setup_login_terminal_auth_error_exits_3(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_client = _FakeClientTerminalFailure()
    monkeypatch.setattr(cli_main, "build_client", lambda blob: fake_client)
    _answers(monkeypatch, "builtins.input", [_FAKE_API_ID, _FAKE_PHONE])
    _answers(monkeypatch, "getpass.getpass", [_FAKE_API_HASH])

    target = tmp_path / "session.dpapi"
    exit_code = cli_main.main(["setup-login", "--path", str(target)])

    assert exit_code == 3
    assert not target.exists()
    # Security-review follow-up: the terminal-error branch used to return
    # without disconnecting, leaking a connection.
    assert fake_client.disconnected is True


def test_setup_login_save_failure_after_successful_login_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Telegram-side login "succeeds" (fake), but writing the encrypted
    # blob fails -- e.g. a full disk. Must not raise a raw traceback, must
    # not leak the session string into whatever it does print, and must
    # not leave a partial file behind.
    fake_client = _FakeClientSuccess()
    monkeypatch.setattr(cli_main, "build_client", lambda blob: fake_client)
    _answers(monkeypatch, "builtins.input", [_FAKE_API_ID, _FAKE_PHONE])
    _answers(monkeypatch, "getpass.getpass", [_FAKE_API_HASH])

    def _boom(blob: Any, path: Path) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(cli_main, "save", _boom)

    target = tmp_path / "session.dpapi"
    exit_code = cli_main.main(["setup-login", "--path", str(target)])

    assert exit_code == 1
    assert not target.exists()
    assert fake_client.disconnected is True

    captured = capsys.readouterr()
    assert _FAKE_SESSION_AFTER_LOGIN not in captured.out
    assert _FAKE_SESSION_AFTER_LOGIN not in captured.err
    assert _FAKE_API_HASH not in captured.out
    assert _FAKE_API_HASH not in captured.err
    assert "Devices" in captured.err


def test_setup_login_rejects_non_integer_api_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Never even builds a client -- a bad api_id should fail before any
    # login attempt, let alone a write.
    monkeypatch.setattr(
        cli_main, "build_client", lambda blob: (_ for _ in ()).throw(AssertionError("should not be called"))
    )
    _answers(monkeypatch, "builtins.input", ["not-a-number"])

    target = tmp_path / "session.dpapi"
    exit_code = cli_main.main(["setup-login", "--path", str(target)])

    assert exit_code == 1
    assert not target.exists()


def test_build_parser_has_setup_login_subcommand() -> None:
    parser = cli_main.build_parser()
    args = parser.parse_args(["setup-login"])
    assert args.command == "setup-login"
    assert args.path is None
