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
from telethon import errors
from telethon.tl import types
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

    def __init__(self, *, authorized: bool = True) -> None:
        self.session = _FakeSession(_FAKE_SESSION_AFTER_LOGIN)
        self.start_kwargs: dict[str, Any] = {}
        self.disconnected = False
        self.authorized = authorized

    async def start(self, **kwargs: Any) -> "_FakeClientSuccess":
        self.start_kwargs = kwargs
        return self

    async def is_user_authorized(self) -> bool:
        return self.authorized

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


# -- every failure is one plain sentence and the right code: never a traceback --------------
#
# The user is not a developer. The likeliest mistakes -- a tutorial's
# api_id, a mistyped phone number, a wrong code -- used to end in a raw
# Python traceback. Each case below must return an exit code (an escaping
# exception fails the test), say what happened in words, save nothing,
# disconnect, and never print a secret.


class _FakeClientRaising:
    """start() raises `error` -- as Telethon would, from inside its loop."""

    def __init__(self, error: BaseException) -> None:
        self.session = _FakeSession("")
        self.error = error
        self.disconnected = False

    async def start(self, **kwargs: Any) -> None:
        raise self.error

    async def disconnect(self) -> None:
        self.disconnected = True


_FAILURES = [
    # (error start() raises, exit code, words the message must contain)
    (errors.ApiIdInvalidError(request=None), 5, "my.telegram.org"),
    (errors.ApiIdPublishedFloodError(request=None), 5, "copied from a tutorial"),
    (errors.ConnectionApiIdInvalidError(request=None), 5, "Get your OWN pair"),
    (errors.PhoneNumberBannedError(request=None), 3, "banned this phone number"),
    (errors.UserDeactivatedBanError(request=None), 3, "deactivated or banned"),
    (errors.UserDeactivatedError(request=None), 3, "deactivated or banned"),
    (errors.FrozenMethodInvalidError(request=None), 3, "frozen"),
    (errors.AuthKeyDuplicatedError(request=None), 3, "refused the login"),
    (errors.SessionRevokedError(request=None), 3, "refused the login"),
    (errors.PhoneNumberInvalidError(request=None), 1, "phone number is not valid"),
    (errors.PhoneNumberUnoccupiedError(request=None), 1, "No Telegram account uses"),
    (RuntimeError("3 consecutive sign-in attempts failed. Aborting"), 1, "wrong 3 times"),
    (errors.PasswordHashInvalidError(request=None), 1, "2FA password was wrong"),
    (errors.FloodWaitError(request=None, capture=600), 1, "Wait about 10 minute(s)"),
    (errors.PhoneNumberFloodError(request=None), 1, "too many login attempts"),
    (ConnectionError("network unreachable"), 1, "internet connection"),
    (ValueError("something nobody anticipated"), 1, "The login failed (ValueError)"),
]


@pytest.mark.parametrize(
    ("error", "code", "words"), _FAILURES, ids=[type(e).__name__ for e, _, _ in _FAILURES]
)
def test_every_login_failure_is_explained_in_words_with_its_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
    code: int,
    words: str,
) -> None:
    fake_client = _FakeClientRaising(error)
    monkeypatch.setattr(cli_main, "build_client", lambda blob: fake_client)
    _answers(monkeypatch, "builtins.input", [_FAKE_API_ID, _FAKE_PHONE])
    _answers(monkeypatch, "getpass.getpass", [_FAKE_API_HASH])
    target = tmp_path / "session.dpapi"

    exit_code = cli_main.main(["setup-login", "--path", str(target)])

    assert exit_code == code
    captured = capsys.readouterr()
    assert words in captured.err
    assert "Nothing was saved" in captured.err
    assert "Traceback" not in captured.err + captured.out
    assert not target.exists()
    assert fake_client.disconnected is True
    for secret in (_FAKE_API_HASH, _FAKE_PHONE):
        assert secret not in captured.out and secret not in captured.err


@pytest.mark.parametrize("at", ["a prompt", "the login"])
def test_ctrl_c_cancels_cleanly_with_exit_130(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], at: str
) -> None:
    fake_client = _FakeClientRaising(KeyboardInterrupt())
    monkeypatch.setattr(cli_main, "build_client", lambda blob: fake_client)
    if at == "a prompt":
        answers = iter([_FAKE_API_ID])

        def typed(*_args: Any, **_kwargs: Any) -> str:
            try:
                return next(answers)
            except StopIteration:
                raise KeyboardInterrupt from None  # Ctrl+C at the phone prompt

        monkeypatch.setattr("builtins.input", typed)
    else:
        _answers(monkeypatch, "builtins.input", [_FAKE_API_ID, _FAKE_PHONE])
    _answers(monkeypatch, "getpass.getpass", [_FAKE_API_HASH])
    target = tmp_path / "session.dpapi"

    assert cli_main.main(["setup-login", "--path", str(target)]) == 130
    err = capsys.readouterr().err
    assert "Cancelled with Ctrl+C. Nothing was saved." in err
    assert "Traceback" not in err
    assert not target.exists()


def test_closed_input_is_explained_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def closed(*_args: Any, **_kwargs: Any) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    assert cli_main.main(["setup-login", "--path", str(tmp_path / "s.dpapi")]) == 1
    assert "input was closed" in capsys.readouterr().err


# -- the same, through Telethon's real login loop ------------------------------------------------


def _real_client_offline(monkeypatch: pytest.MonkeyPatch, *, send_code=None, sign_in=None):
    """The real `build_client()` TelegramClient, with every method that would
    touch the network replaced -- so Telethon's own `start()` loop (its
    retry counting, its give-up errors) runs for real, offline."""
    from xaucopier.ingest.client import build_client

    made: dict[str, Any] = {
        "sign_ins": [], "disconnected": False, "code_requests": 0, "signed_in": False
    }

    def build(blob):
        client = build_client(blob)

        async def no_network(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("tried to contact Telegram")

        async def get_me(*args: Any, **kwargs: Any) -> None:
            return None  # not logged in yet

        async def send_code_request(*args: Any, **kwargs: Any) -> None:
            made["code_requests"] += 1
            if send_code is not None:
                raise send_code

        async def sign_in_(*args: Any, **kwargs: Any) -> Any:
            made["sign_ins"].append(sorted(k for k, v in kwargs.items() if v))
            if not kwargs.get("code") and not kwargs.get("password"):
                # Telethon's sign_in with neither (auth.py:331): it asks for
                # a new code and returns the SentCode -- no login.
                made["code_requests"] += 1
                return object()
            outcome = sign_in(kwargs)
            if isinstance(outcome, BaseException):
                raise outcome
            made["signed_in"] = True  # as Telethon's _on_login() would record
            return outcome

        async def is_user_authorized() -> bool:
            return made["signed_in"]

        async def disconnect() -> None:
            made["disconnected"] = True

        monkeypatch.setattr(client, "connect", no_network)
        monkeypatch.setattr(client, "is_connected", lambda: True)
        monkeypatch.setattr(client, "get_me", get_me)
        monkeypatch.setattr(client, "send_code_request", send_code_request)
        monkeypatch.setattr(client, "sign_in", sign_in_)
        monkeypatch.setattr(client, "is_user_authorized", is_user_authorized)
        monkeypatch.setattr(client, "disconnect", disconnect)
        monkeypatch.setattr(client.session, "save", lambda: _FAKE_SESSION_AFTER_LOGIN)
        return client

    monkeypatch.setattr(cli_main, "build_client", build)
    return made


def test_three_wrong_codes_through_telethons_own_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    made = _real_client_offline(
        monkeypatch, sign_in=lambda kwargs: errors.PhoneCodeInvalidError(request=None)
    )
    _answers(monkeypatch, "builtins.input", [_FAKE_API_ID, _FAKE_PHONE, "11111", "22222", "33333"])
    _answers(monkeypatch, "getpass.getpass", [_FAKE_API_HASH])

    assert cli_main.main(["setup-login", "--path", str(tmp_path / "s.dpapi")]) == 1
    assert len(made["sign_ins"]) == 3 and made["disconnected"]
    assert "The login code was wrong 3 times" in capsys.readouterr().err


def test_three_wrong_2fa_passwords_through_telethons_own_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def sign_in(kwargs):
        if kwargs.get("password"):
            return errors.PasswordHashInvalidError(request=None)
        return errors.SessionPasswordNeededError(request=None)  # the code was right

    made = _real_client_offline(monkeypatch, sign_in=sign_in)
    _answers(monkeypatch, "builtins.input", [_FAKE_API_ID, _FAKE_PHONE, "12345"])
    _answers(monkeypatch, "getpass.getpass", [_FAKE_API_HASH, "wrong1", "wrong2", "wrong3"])

    assert cli_main.main(["setup-login", "--path", str(tmp_path / "s.dpapi")]) == 1
    # One sign-in with the code (the phone goes positionally), then three
    # with the password: Telethon's own give-up count.
    assert made["sign_ins"] == [["code"]] + [["password", "phone"]] * 3
    assert "2FA password was wrong 3 times" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("error", "code"),
    [(errors.ApiIdPublishedFloodError(request=None), 5), (errors.PhoneNumberBannedError(request=None), 3)],
    ids=["ApiIdPublishedFlood", "PhoneNumberBanned"],
)
def test_a_refusal_at_send_code_through_telethons_own_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    code: int,
) -> None:
    made = _real_client_offline(monkeypatch, send_code=error)
    _answers(monkeypatch, "builtins.input", [_FAKE_API_ID, _FAKE_PHONE])
    _answers(monkeypatch, "getpass.getpass", [_FAKE_API_HASH])

    assert cli_main.main(["setup-login", "--path", str(tmp_path / "s.dpapi")]) == code
    assert made["sign_ins"] == [] and made["disconnected"]
    assert "Traceback" not in capsys.readouterr().err


def test_telethon_still_gives_up_on_codes_with_the_words_we_match() -> None:
    """`login_failure` recognises three wrong codes by Telethon's own
    message; if Telethon ever rewords it, this fails first."""
    import inspect

    from telethon.client import auth

    assert cli_main._THREE_WRONG_CODES in inspect.getsource(auth)


# -- two-step verification: a blank password is never taken as "no password" ----------------
#
# Telethon only asks for the password when 2FA is on. Given a blank one,
# its sign_in() asks Telegram to resend the code instead of checking a
# password and returns without logging in -- and start() returned as if it
# had: "Login succeeded", a saved session that is not logged in, and exit
# 3 at the next command. Measured by the reviewer.

_USER = types.User(id=777, first_name="Signed In")


def _two_step(kwargs: dict[str, Any]) -> Any:
    """The code is right; the account has 2FA; "right" is its password."""
    if kwargs.get("code"):
        return errors.SessionPasswordNeededError(request=None)
    if kwargs.get("password") == "right":
        return _USER
    return errors.PasswordHashInvalidError(request=None)


def test_a_blank_2fa_answer_is_asked_again_and_the_right_password_logs_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    made = _real_client_offline(monkeypatch, sign_in=_two_step)
    _answers(monkeypatch, "builtins.input", [_FAKE_API_ID, _FAKE_PHONE, "12345"])
    prompts: list[str] = []
    answers = iter([_FAKE_API_HASH, "", "", "right"])

    def getpass_(prompt: str = "") -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("getpass.getpass", getpass_)
    target = tmp_path / "session.dpapi"

    assert cli_main.main(["setup-login", "--path", str(target)]) == 0

    assert load(target).session_string == _FAKE_SESSION_AFTER_LOGIN
    # The blanks never reached Telegram: one sign-in with the code, one with
    # the password -- and only the one code request, never a resend.
    assert made["sign_ins"] == [["code"], ["password", "phone"]]
    assert made["code_requests"] == 1
    password_prompts = prompts[1:]
    assert len(password_prompts) == 3
    for prompt in password_prompts:
        assert "two-step verification" in prompt and "blank" not in prompt.lower()
    assert "cannot be blank" in capsys.readouterr().err


def test_three_blank_2fa_answers_give_up_and_save_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    made = _real_client_offline(monkeypatch, sign_in=_two_step)
    _answers(monkeypatch, "builtins.input", [_FAKE_API_ID, _FAKE_PHONE, "12345"])
    _answers(monkeypatch, "getpass.getpass", [_FAKE_API_HASH, "", "", ""])
    target = tmp_path / "session.dpapi"

    assert cli_main.main(["setup-login", "--path", str(target)]) == 1

    assert not target.exists()
    assert made["sign_ins"] == [["code"]] and made["code_requests"] == 1
    assert made["disconnected"]
    err = capsys.readouterr().err
    assert "two-step verification turned on" in err and "left blank 3 times" in err
    assert "Two-Step Verification" in err and "Nothing was saved" in err


def test_a_start_that_returns_without_a_login_saves_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Defence in depth: whatever start() returned, a session Telegram does
    not recognise as logged in is never saved."""
    fake_client = _FakeClientSuccess(authorized=False)
    monkeypatch.setattr(cli_main, "build_client", lambda blob: fake_client)
    _answers(monkeypatch, "builtins.input", [_FAKE_API_ID, _FAKE_PHONE])
    _answers(monkeypatch, "getpass.getpass", [_FAKE_API_HASH])
    target = tmp_path / "session.dpapi"

    assert cli_main.main(["setup-login", "--path", str(target)]) == 1

    assert not target.exists()
    assert fake_client.disconnected is True
    captured = capsys.readouterr()
    assert "did not log this program in" in captured.err
    assert "Login succeeded" not in captured.out


@pytest.mark.parametrize(
    ("answers", "words"),
    [
        ([_FAKE_API_ID, ""], "The phone number was empty"),
        ([_FAKE_API_ID], "The api_hash was empty"),
    ],
    ids=["empty phone", "empty api_hash"],
)
def test_an_empty_answer_is_explained_before_anything_is_sent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    answers: list[str],
    words: str,
) -> None:
    monkeypatch.setattr(
        cli_main, "build_client",
        lambda blob: (_ for _ in ()).throw(AssertionError("a client was built")),
    )
    _answers(monkeypatch, "builtins.input", answers)
    _answers(monkeypatch, "getpass.getpass", ["" if "api_hash" in words else _FAKE_API_HASH])

    assert cli_main.main(["setup-login", "--path", str(tmp_path / "s.dpapi")]) == 1
    err = capsys.readouterr().err
    assert words in err and "Traceback" not in err
