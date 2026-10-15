"""Tests for `xaucopier resolve-channels`.

The property that matters most: **`channels.toml` is never left with a
channel switched on at `channel_id = 0`** -- the state the Task 1 loader
rejects, so `watch` would refuse to start -- not after success, not after
a failure injected at any point of the write (inside the write itself,
at the flush to disk, at the final rename). The file is always exactly
the old one or the fully checked new one, and everything else in it --
other channels, comments, layout -- survives byte for byte.

Every test works on a `tmp_path` copy of the config. The repository's
own `config/channels.toml` is checked, before and after every test in
this file, to be byte-identical. Telegram is the offline fake; the
session file, the mutex and the prompts are test doubles.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import errors
from telethon.tl import types

import xaucopier.cli.main as cli_main
import xaucopier.cli.resolve as resolve_module
from fakes.telegram import FakeTelegramClient
from xaucopier.cli import exit_codes
from xaucopier.config.channels import load_channels
from xaucopier.config.loader import ConfigError
from xaucopier.ingest.client import AlreadyRunningError
from xaucopier.ingest.secrets import SecretsBlob
from xaucopier.store import db

REPO = Path(__file__).resolve().parents[1]
REAL_CHANNELS = REPO / "config" / "channels.toml"
SHIPPED = REAL_CHANNELS.read_bytes().decode("utf-8")

MMH = types.Channel(
    id=1234567890,
    title="MARKET MASTERY HUB / COMMUNITY",
    photo=types.ChatPhotoEmpty(),
    date=None,
    broadcast=True,
)
MMH_ID = -1001234567890
OTHER = types.Channel(id=555, title="Some Other Channel", photo=types.ChatPhotoEmpty(), date=None)
A_USER = types.User(id=42, first_name="Not A Channel")

#: A second, already-resolved channel with its own comments, appended to
#: the shipped file: it must come through every edit untouched.
RESOLVED_BLOCK = """
# a second channel, resolved long ago -- keep this comment
[[channels]]
channel_id = -1009999999999   # do not touch
name       = "Already Resolved"
parser     = "mmh"
ordinal    = 2
enabled    = true
"""


@pytest.fixture(autouse=True)
def _the_real_config_is_never_touched():
    before = hashlib.sha256(REAL_CHANNELS.read_bytes()).hexdigest()
    yield
    assert hashlib.sha256(REAL_CHANNELS.read_bytes()).hexdigest() == before


@dataclass
class ResolveClient(FakeTelegramClient):
    entities: dict[str, Any] = field(default_factory=dict)
    dialogs: tuple[Any, ...] = ()
    authorized: bool = True
    asked: list[Any] = field(default_factory=list)

    async def get_me(self) -> Any:
        # Telethon's get_me(): None when Telegram says this login is not
        # authorised (`cli/resolve.logged_in`).
        return types.User(id=4242, is_self=True) if self.authorized else None

    async def get_entity(self, entity: Any) -> Any:
        self.calls.append("get_entity")
        self.asked.append(entity)
        if entity in self.entities:
            return self.entities[entity]
        raise ValueError(f'Cannot find any entity corresponding to "{entity}"')

    async def get_dialogs(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.calls.append("get_dialogs")
        return list(self.dialogs)


class Lock:
    def __init__(self) -> None:
        self.acquired = self.released = 0

    def __call__(self) -> "Lock":
        self.acquired += 1
        return self

    def release(self) -> None:
        self.released += 1


@dataclass
class Rig:
    config_dir: Path
    client: ResolveClient
    lock: Lock
    answers: list[str]

    @property
    def channels_path(self) -> Path:
        return self.config_dir / "channels.toml"

    @property
    def db_path(self) -> Path:
        """Never created by the rig: a test that needs a database makes it."""
        return self.config_dir.parent / "state.db"

    def argv(self, *extra: str) -> list[str]:
        return [
            "resolve-channels",
            "--config-dir", str(self.config_dir),
            "--secrets", str(self.config_dir / "no-session.dpapi"),
            "--db", str(self.db_path),
            *extra,
        ]


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    # newline="" / bytes: keep the shipped file's line endings exactly.
    (config_dir / "channels.toml").write_bytes((SHIPPED + RESOLVED_BLOCK).encode("utf-8"))
    (config_dir / "accounts.toml").write_bytes((REPO / "config" / "accounts.toml").read_bytes())
    client = ResolveClient(
        entities={"https://t.me/mmhcommunity": MMH, "@someuser": A_USER},
        dialogs=(SimpleNamespace(entity=OTHER), SimpleNamespace(entity=A_USER), SimpleNamespace(entity=MMH)),
    )
    lock = Lock()
    answers: list[str] = []
    monkeypatch.setattr(
        resolve_module, "load_session",
        lambda path: SecretsBlob(api_id=1, api_hash="0" * 32, session_string="NOT_REAL"),
    )
    monkeypatch.setattr(resolve_module, "build_one_shot_client", lambda blob: client)
    monkeypatch.setattr(resolve_module, "acquire_single_instance_lock", lock)
    monkeypatch.setattr("builtins.input", lambda prompt="": answers.pop(0))
    return Rig(config_dir=config_dir, client=client, lock=lock, answers=answers)


def _enabled_placeholders(path: Path) -> list[str]:
    data = tomllib.loads(path.read_bytes().decode("utf-8"))
    return [c["name"] for c in data["channels"] if c["enabled"] and c["channel_id"] == 0]


def _changed_lines(before: str, after: str) -> list[tuple[str, str]]:
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    assert len(old) == len(new), "lines were added or removed"
    return [(a, b) for a, b in zip(old, new) if a != b]


def _no_temp_files(config_dir: Path) -> bool:
    return sorted(p.name for p in config_dir.iterdir()) == ["accounts.toml", "channels.toml"]


# -- success ----------------------------------------------------------------------------------


def test_a_pasted_link_writes_the_id_and_switches_the_channel_on_touching_nothing_else(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    before = rig.channels_path.read_bytes().decode("utf-8")

    code = cli_main.main(rig.argv("--link", " https://t.me/mmhcommunity/55 ", "--yes"))

    assert code == exit_codes.EXIT_OK
    after = rig.channels_path.read_bytes().decode("utf-8")
    changed = _changed_lines(before, after)
    assert [old.split("=")[0].strip() for old, _ in changed] == ["channel_id", "enabled"]
    assert changed[0][1].startswith(f"channel_id = {MMH_ID}")
    assert "MARKET MASTERY HUB / COMMUNITY" in changed[0][1]
    assert changed[1][1].startswith("enabled    = true")
    # Trailing comments stay in their column where the new value fits.
    assert changed[1][1].index("#") == changed[1][0].index("#")
    assert changed[0][1].endswith(changed[0][0][len(changed[0][0].rstrip("\r\n")):])  # line ending kept
    assert "keep this comment" in after and "# do not touch" in after

    loaded = load_channels(rig.channels_path)
    assert [(c.name, c.channel_id, c.enabled) for c in loaded] == [
        ("MARKET MASTERY HUB / COMMUNITY", MMH_ID, True),
        ("Already Resolved", -1009999999999, True),
    ]
    # The one get_entity call, with the post number trimmed off the link.
    assert rig.client.asked == ["https://t.me/mmhcommunity"]
    assert rig.client.calls[0] == "connect" and rig.client.calls[-1] == "disconnect"
    assert rig.lock.acquired == rig.lock.released == 1
    assert _no_temp_files(rig.config_dir)
    assert "Next: run `.\\xaucopier watch`" in capsys.readouterr().out


def test_pressing_enter_picks_the_channel_from_the_joined_list(rig: Rig) -> None:
    rig.answers.extend(["", "2", "yes"])  # Enter, then "2" (MMH; the user is not listed), confirm

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_OK
    loaded = load_channels(rig.channels_path)
    assert (loaded[0].channel_id, loaded[0].enabled) == (MMH_ID, True)
    assert "get_entity" not in rig.client.calls
    assert rig.answers == []


def test_nothing_to_do_connects_to_nothing(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    rig.channels_path.write_text(RESOLVED_BLOCK, encoding="utf-8")

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_OK
    assert rig.client.calls == [] and rig.lock.acquired == 0
    assert "Nothing to do" in capsys.readouterr().out


# -- the file is never left enabled at id 0 -----------------------------------------------------


def _boom(*args: Any, **kwargs: Any) -> None:
    raise OSError("injected failure")


def _half_then_fail(path: Path, data: bytes) -> None:
    """The write is interrupted half-way: half the bytes reach `path`."""
    with open(path, "xb") as handle:
        handle.write(data[: len(data) // 2])
    raise OSError("injected: interrupted mid-write")


@pytest.mark.parametrize(
    "where",
    ["mid-write", "flush to disk", "final rename", "verification"],
)
def test_a_failure_at_any_point_of_the_write_leaves_the_file_exactly_as_it_was(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], where: str
) -> None:
    before = rig.channels_path.read_bytes()
    if where == "mid-write":
        monkeypatch.setattr(resolve_module, "_fsync_write", _half_then_fail)
    elif where == "flush to disk":
        monkeypatch.setattr(os, "fsync", _boom)
    elif where == "final rename":
        monkeypatch.setattr(os, "replace", _boom)
    else:
        def rejects(*args: Any, **kwargs: Any) -> None:
            raise ConfigError("injected: the loader rejects the edited file")

        monkeypatch.setattr(resolve_module, "load_channels", rejects)

    code = cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity", "--yes"))

    # F30: the disk refusing a write or the rename is worth running again
    # (exit 1); a file the loader rejects is a setup problem (exit 5).
    assert code == (exit_codes.EXIT_CONFIG_ERROR if where == "verification" else exit_codes.EXIT_FAILED)
    assert rig.channels_path.read_bytes() == before
    assert _enabled_placeholders(rig.channels_path) == []
    assert _no_temp_files(rig.config_dir)
    err = capsys.readouterr().err
    assert "was not changed" in err and f"channel_id = {MMH_ID}" in err
    assert rig.lock.released == 1 and "disconnect" in rig.client.calls


def test_an_edit_that_changes_anything_else_is_caught_before_the_file_is_replaced(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The last line of defence against a text edit gone wrong: the new
    file is loaded back and compared with the old one, and anything but
    the one channel's id and switch differing -- here, the edit also
    renamed the other channel, which the loader would happily accept --
    stops the write."""
    real = resolve_module.set_id_then_enable

    def sloppy(text: str, **kwargs: Any) -> str:
        return real(text, **kwargs).replace('"Already Resolved"', '"Renamed By Accident"')

    monkeypatch.setattr(resolve_module, "set_id_then_enable", sloppy)
    before = rig.channels_path.read_bytes()

    code = cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity", "--yes"))

    assert code == exit_codes.EXIT_CONFIG_ERROR
    assert rig.channels_path.read_bytes() == before
    assert _no_temp_files(rig.config_dir)
    assert "differs from the original" in capsys.readouterr().err


def test_the_id_is_written_before_the_channel_is_switched_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each step of the edit is checked against the loader's rule; the id
    step's output still has the channel off, so no step -- not even in
    memory -- ever has it on at id 0."""
    steps: list[str] = []
    real = resolve_module._check_no_enabled_placeholder

    def spy(text: str):
        steps.append(text)
        return real(text)

    monkeypatch.setattr(resolve_module, "_check_no_enabled_placeholder", spy)
    final = resolve_module.set_id_then_enable(
        SHIPPED, index=0, channel_id=MMH_ID, title="T", today="2026-09-23"
    )

    first, last = (tomllib.loads(text)["channels"][0] for text in steps)
    assert (first["channel_id"], first["enabled"]) == (MMH_ID, False)
    assert (last["channel_id"], last["enabled"]) == (MMH_ID, True)
    assert steps[-1] == final


def test_an_edit_that_would_enable_a_placeholder_is_refused() -> None:
    with pytest.raises(resolve_module.ResolveError, match="channel_id = 0"):
        resolve_module._check_no_enabled_placeholder(SHIPPED.replace("enabled    = false", "enabled = true"))


def test_field_order_within_the_section_does_not_matter() -> None:
    text = (
        "[[channels]]\nenabled = false  # off\nname = \"X\"\nparser = \"mmh\"\n"
        "ordinal = 1\nchannel_id = 0\n"
    )
    result = resolve_module.set_id_then_enable(text, index=0, channel_id=MMH_ID, title="X", today="d")
    (channel,) = tomllib.loads(result)["channels"]
    assert (channel["channel_id"], channel["enabled"]) == (MMH_ID, True)


@pytest.mark.parametrize(
    "text",
    [
        # the id written as an inline table: not a line this can edit
        'channels = [{channel_id = 0, name = "X", parser = "mmh", ordinal = 1, enabled = false}]\n',
        # the key twice in one section (invalid TOML anyway)
        "[[channels]]\nchannel_id = 0\nchannel_id = 0\nname = \"X\"\nenabled = false\n",
        # already switched off with a real id: no placeholder line to edit
        "[[channels]]\nchannel_id = 5\nname = \"X\"\nenabled = false\n",
    ],
)
def test_a_layout_it_cannot_edit_safely_is_refused_not_guessed(text: str) -> None:
    with pytest.raises(resolve_module.ResolveError):
        resolve_module.set_id_then_enable(text, index=0, channel_id=MMH_ID, title="X", today="d")


# -- refusals -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("link", "message"),
    [
        ("1234567890", "that is a number"),
        ("@someuser", "is not a channel"),
        ("https://t.me/nosuchchannel", "could not find"),
        ("https://t.me/c/1234567890/77", "private channel"),
    ],
)
def test_a_link_that_is_not_the_channel_changes_nothing(
    rig: Rig, capsys: pytest.CaptureFixture[str], link: str, message: str
) -> None:
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv("--link", link, "--yes")) == exit_codes.EXIT_FAILED
    assert rig.channels_path.read_bytes() == before
    assert message in capsys.readouterr().err


def test_declining_the_confirmation_changes_nothing(rig: Rig) -> None:
    before = rig.channels_path.read_bytes()
    rig.answers.extend(["https://t.me/mmhcommunity", "no"])

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_FAILED
    assert rig.channels_path.read_bytes() == before


def test_an_id_already_in_the_file_is_refused(rig: Rig, capsys: pytest.CaptureFixture[str]) -> None:
    rig.channels_path.write_bytes(
        (SHIPPED + RESOLVED_BLOCK.replace("-1009999999999", str(MMH_ID))).encode("utf-8")
    )
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity", "--yes")) == 1
    assert rig.channels_path.read_bytes() == before
    assert "already in channels.toml" in capsys.readouterr().err


def test_a_logged_out_session_exits_3(rig: Rig) -> None:
    rig.client.authorized = False
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity", "--yes")) == 3
    assert rig.channels_path.read_bytes() == before


def test_a_revoked_session_met_mid_lookup_exits_3(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    async def revoked(entity: Any) -> Any:
        raise errors.SessionRevokedError(request=None)

    monkeypatch.setattr(rig.client, "get_entity", revoked)

    assert cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity", "--yes")) == 3
    assert "disconnect" in rig.client.calls


def test_while_watch_runs_it_exits_6_without_connecting(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def held() -> None:
        raise AlreadyRunningError("held")

    monkeypatch.setattr(resolve_module, "acquire_single_instance_lock", held)

    assert cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity")) == 6
    assert rig.client.calls == []


def test_a_malformed_channels_file_exits_5(rig: Rig) -> None:
    rig.channels_path.write_text("[[channels]\n", encoding="utf-8")
    assert cli_main.main(rig.argv()) == exit_codes.EXIT_CONFIG_ERROR
    assert rig.client.calls == []


@pytest.mark.parametrize(
    ("error", "code", "words"),
    [
        (errors.ApiIdPublishedFloodError(request=None), 5, "my.telegram.org"),
        (errors.UserDeactivatedBanError(request=None), 3, "setup-login"),
    ],
    ids=["api_id rejected", "account banned"],
)
def test_a_permanent_telegram_error_mid_lookup_exits_with_its_code(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    code: int,
    words: str,
) -> None:
    async def refused(entity: Any) -> Any:
        raise error

    monkeypatch.setattr(rig.client, "get_entity", refused)
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity", "--yes")) == code
    assert rig.channels_path.read_bytes() == before
    assert words in capsys.readouterr().err
    assert "disconnect" in rig.client.calls


# -- F37 / F24 / F23 / G2L-1: connecting, waiting, and the session held ----------------------


@pytest.mark.parametrize(
    ("error", "code", "words"),
    [
        (errors.ConnectionApiIdInvalidError(request=None), 5, "my.telegram.org"),
        (errors.AuthKeyUnregisteredError(request=None), 3, "setup-login"),
    ],
    ids=["api_id rejected at connect", "session unknown at connect"],
)
def test_a_permanent_error_from_the_connect_itself_exits_with_its_code(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    error: Exception, code: int, words: str,
) -> None:
    """Final review F37: connect() was outside the try, so a bad api_id --
    which Telegram reports at the connect -- was a raw traceback, exit 1."""
    async def refused(*args: Any, **kwargs: Any) -> None:
        rig.client.calls.append("connect")
        raise error

    monkeypatch.setattr(rig.client, "connect", refused)
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity", "--yes")) == code

    assert rig.channels_path.read_bytes() == before
    err = capsys.readouterr().err
    assert words in err and "Traceback" not in err
    assert "disconnect" in rig.client.calls and rig.lock.released == 1


def test_with_no_network_it_gives_up_after_its_wait_and_says_so(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    monkeypatch.setattr(resolve_module, "CONNECT_TIMEOUT_SECONDS", 0.1)

    async def never(*args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(rig.client, "connect", never)

    assert cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity", "--yes")) == 1

    err = capsys.readouterr().err
    assert "Could not reach Telegram within 0.1 seconds" in err
    assert "`.\\xaucopier resolve-channels` again" in err and "Nothing was changed." in err
    assert rig.lock.released == 1


def test_told_to_wait_by_telegram_it_says_how_long(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def wait_please(*args: Any, **kwargs: Any) -> Any:
        raise errors.FloodWaitError(request=None, capture=61)

    monkeypatch.setattr(rig.client, "get_dialogs", wait_please)
    rig.answers.append("")
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_FAILED

    assert rig.channels_path.read_bytes() == before
    err = capsys.readouterr().err
    assert "Telegram asks to wait about 1 minute, then run it again" in err
    assert "Nothing was changed." in err and "Traceback" not in err


def test_any_other_telegram_error_is_one_plain_sentence(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def trouble(*args: Any, **kwargs: Any) -> Any:
        raise errors.RpcCallFailError(request=None)

    monkeypatch.setattr(rig.client, "get_me", trouble)

    assert cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity", "--yes")) == 1

    err = capsys.readouterr().err
    assert "Telegram could not do this just now (Telegram error 500)" in err
    assert "Nothing was changed." in err and "Traceback" not in err


def test_closed_input_is_explained_not_a_traceback(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def closed(*args: Any, **kwargs: Any) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_FAILED

    assert rig.channels_path.read_bytes() == before
    err = capsys.readouterr().err
    assert "input was closed" in err and "was not saved" in err
    assert rig.lock.released == 1


def test_while_an_administrator_window_holds_the_session_it_says_so(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import pywintypes
    import winerror

    import xaucopier.ingest.client as client_module

    def denied(*args: object) -> object:
        raise pywintypes.error(winerror.ERROR_ACCESS_DENIED, "CreateMutex", "Access is denied.")

    monkeypatch.setattr(client_module.win32event, "CreateMutex", denied)
    monkeypatch.setattr(resolve_module, "acquire_single_instance_lock",
                        client_module.acquire_single_instance_lock)

    assert cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity")) == 6

    err = capsys.readouterr().err
    assert "Stop it first" in err and "administrator" in err
    assert rig.client.calls == []


def test_a_failure_after_one_channel_was_saved_keeps_it_and_says_so(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two channels waiting for their id: the first is saved, then Telegram
    asks to wait. "Nothing was changed" would be false; the message says the
    first is kept."""
    second = RESOLVED_BLOCK.replace("channel_id = -1009999999999   # do not touch", "channel_id = 0") \
        .replace('"Already Resolved"', '"Second Placeholder"').replace("enabled    = true", "enabled    = false") \
        .replace("ordinal    = 2", "ordinal    = 3")
    rig.channels_path.write_bytes((SHIPPED + RESOLVED_BLOCK + second).encode("utf-8"))
    real_get_entity = rig.client.get_entity
    asked = {"n": 0}

    async def get_entity(entity: Any) -> Any:
        asked["n"] += 1
        if asked["n"] == 2:
            raise errors.FloodWaitError(request=None, capture=600)
        return await real_get_entity(entity)

    monkeypatch.setattr(rig.client, "get_entity", get_entity)
    rig.answers.extend(["https://t.me/mmhcommunity", "yes", "https://t.me/second"])

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_FAILED

    loaded = load_channels(rig.channels_path)
    assert [(c.name, c.channel_id) for c in loaded if c.enabled] == [
        ("MARKET MASTERY HUB / COMMUNITY", MMH_ID), ("Already Resolved", -1009999999999)
    ]
    err = capsys.readouterr().err
    assert "Telegram asks to wait about 10 minutes" in err
    assert "The 1 channel(s) saved above are kept; nothing else was changed." in err
    assert "Nothing was changed." not in err


# -- F05: a group is not a channel ------------------------------------------------------------

COMMUNITY_GROUP = types.Channel(
    id=7777777777, title="MARKET MASTERY HUB / COMMUNITY", photo=types.ChatPhotoEmpty(),
    date=None, megagroup=True,
)
COMMUNITY_GROUP_ID = -1007777777777


@pytest.fixture
def group_rig(rig: Rig) -> Rig:
    rig.client.dialogs = (SimpleNamespace(entity=MMH), SimpleNamespace(entity=COMMUNITY_GROUP))
    rig.client.entities["https://t.me/mmhchat"] = COMMUNITY_GROUP
    return rig


def test_the_picker_says_which_entry_is_a_channel_and_which_a_group(
    group_rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final review F05 (A3): the user's own first setup path offered a
    group titled like the channel with nothing to tell them apart."""
    group_rig.answers.extend(["", ""])

    assert cli_main.main(group_rig.argv()) == exit_codes.EXIT_FAILED

    out = capsys.readouterr().out
    assert "  1. MARKET MASTERY HUB / COMMUNITY (channel)" in out
    assert "  2. MARKET MASTERY HUB / COMMUNITY (group)" in out


def test_a_group_needs_its_own_yes_and_no_saves_nothing(
    group_rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    before = group_rig.channels_path.read_bytes()
    group_rig.answers.extend(["", "2", "no"])

    assert cli_main.main(group_rig.argv()) == exit_codes.EXIT_FAILED

    assert group_rig.channels_path.read_bytes() == before
    out = capsys.readouterr().out
    assert (
        "This is a group: anyone in it can post, and their messages will be read as trading "
        "instructions."
    ) in out
    assert "Not saved. Nothing was changed." in out
    assert group_rig.answers == []


def test_a_group_confirmed_twice_is_saved(group_rig: Rig) -> None:
    group_rig.answers.extend(["", "2", "yes", "yes"])

    assert cli_main.main(group_rig.argv()) == exit_codes.EXIT_OK

    loaded = load_channels(group_rig.channels_path)
    assert (loaded[0].channel_id, loaded[0].enabled) == (COMMUNITY_GROUP_ID, True)
    assert group_rig.answers == []  # both questions were asked


def test_yes_on_the_command_line_does_not_answer_the_group_question(
    group_rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    before = group_rig.channels_path.read_bytes()
    group_rig.answers.append("no")

    assert cli_main.main(group_rig.argv("--link", "https://t.me/mmhchat", "--yes")) == 1

    assert group_rig.channels_path.read_bytes() == before
    assert group_rig.answers == []


def test_allow_group_saves_a_group_without_asking(
    group_rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli_main.main(group_rig.argv("--link", "https://t.me/mmhchat", "--yes", "--allow-group"))

    assert code == exit_codes.EXIT_OK
    assert load_channels(group_rig.channels_path)[0].channel_id == COMMUNITY_GROUP_ID
    assert "anyone in it can post" in capsys.readouterr().out


def test_saved_names_the_telegram_id_and_the_channel_number_apart(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final review F27: "is now channel -1001234567890" used "channel
    number" for the Telegram id, while `channels on N` means the number the
    list shows."""
    assert cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity", "--yes")) == 0

    out = capsys.readouterr().out
    assert (
        "Saved: 'MARKET MASTERY HUB / COMMUNITY' -- its Telegram id -1001234567890 -- and "
        "switched it on. It is channel 1 in `.\\xaucopier channels`."
    ) in out
    assert "is now channel -100" not in out
    assert "does not have its Telegram id yet" in out


# -- fix round 2: a channel read before and resolved again is read from now on --------------

RESOLVED_AT = datetime(2026, 9, 23, 12, 0, 0, 654321, tzinfo=UTC)


def _make_db(rig: Rig, *, state: tuple | None) -> None:
    """The rig's database, with channel MMH's reading state (pointer,
    seeded marker, cutoff) when `state` is given."""
    conn = db.connect(rig.db_path, role="writer")
    try:
        if state is not None:
            conn.execute(
                "INSERT INTO channel_state (channel_id, last_processed_message_id, "
                "seeded_at_utc, seed_cutoff_utc) VALUES (?, ?, ?, ?)",
                (MMH_ID, *state),
            )
        conn.commit()
    finally:
        conn.close()


def _mmh_state(rig: Rig) -> tuple | None:
    conn = db.connect_readonly(rig.db_path)
    try:
        row = conn.execute(
            "SELECT last_processed_message_id, seeded_at_utc, seed_cutoff_utc FROM channel_state "
            "WHERE channel_id = ?",
            (MMH_ID,),
        ).fetchone()
        return None if row is None else tuple(row)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("state", "after"),
    [
        ((40, "2026-09-01T00:00:00+00:00", None),
         (40, "2026-09-01T00:00:00+00:00", "2026-09-23T12:00:00+00:00")),
        # A first read that never finished: this command holds the session,
        # so no watch is seeding it at the old cutoff (the reviewer's (b)).
        ((0, None, "2026-09-20T08:00:00+00:00"), (0, None, "2026-09-23T12:00:00+00:00")),
    ],
    ids=["read before", "first read pending"],
)
def test_resolving_to_a_channel_read_before_reads_it_from_now(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: tuple,
    after: tuple,
) -> None:
    """G2L-3's `channels add` rule, for `resolve-channels` too (fix round 2
    of the reviewer's open (a)): a channel removed and put back as a
    placeholder kept its old pointer, so the next watch read everything it
    posted meanwhile as new."""
    _make_db(rig, state=state)
    monkeypatch.setattr(resolve_module, "_utcnow", lambda: RESOLVED_AT)

    assert cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity", "--yes")) == 0

    assert load_channels(rig.channels_path)[0].channel_id == MMH_ID
    assert _mmh_state(rig) == after
    assert (
        "It was read before, so the next watch reads it from now on: what it posted "
        "since it was last read is skipped."
    ) in capsys.readouterr().out


def test_resolving_a_channel_never_read_writes_nothing(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_db(rig, state=None)

    assert cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity", "--yes")) == 0

    assert _mmh_state(rig) is None
    assert "It was read before" not in capsys.readouterr().out


def test_resolving_with_no_database_creates_none(rig: Rig) -> None:
    assert cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity", "--yes")) == 0
    assert not rig.db_path.exists()


def test_resolving_while_the_database_is_held_saves_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The database first, then channels.toml, as `channels add`."""
    _make_db(rig, state=(40, "2026-09-01T00:00:00+00:00", None))
    monkeypatch.setattr(db, "_BUSY_TIMEOUT_MS", 50)
    before = rig.channels_path.read_bytes()
    holder = sqlite3.connect(rig.db_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        code = cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity", "--yes"))
    finally:
        holder.rollback()
        holder.close()

    assert code == exit_codes.EXIT_FAILED
    assert rig.channels_path.read_bytes() == before
    err = capsys.readouterr().err
    assert "The database is busy" in err and "Nothing was changed." in err
    assert "Traceback" not in err


def _refuse_channels_toml(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows refusing channels.toml for good (FF9), waited out briefly."""
    import xaucopier.config.loader as loader_module

    monkeypatch.setattr(loader_module, "REPLACE_WAIT_SECONDS", 0.2)
    real_open = Path.open

    def opened(path, *args, **kwargs):
        if path.name == "channels.toml":
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)


def test_a_channels_file_another_command_keeps_open_asks_to_run_it_again(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final fix FF9, for resolve-channels: exit 1 and "run the command
    again", never the setup-problem exit 5."""
    _refuse_channels_toml(monkeypatch)

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_FAILED

    err = capsys.readouterr().err
    assert "Run the command again" in err and "PermissionError" not in err
    assert "If this keeps happening, check that the file can be opened." in err
