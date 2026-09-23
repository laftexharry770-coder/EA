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
import tomllib
from dataclasses import dataclass, field
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
from xaucopier.ingest.client import AlreadyRunningError
from xaucopier.ingest.secrets import SecretsBlob

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

    async def is_user_authorized(self) -> bool:
        return self.authorized

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

    def argv(self, *extra: str) -> list[str]:
        return [
            "resolve-channels",
            "--config-dir", str(self.config_dir),
            "--secrets", str(self.config_dir / "no-session.dpapi"),
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
    monkeypatch.setattr(resolve_module, "build_client", lambda blob: client)
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
    assert "Next: run `xaucopier watch`" in capsys.readouterr().out


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
        monkeypatch.setattr(resolve_module, "load_channels", _boom)

    code = cli_main.main(rig.argv("--link", "https://t.me/mmhcommunity", "--yes"))

    assert code == exit_codes.EXIT_CONFIG_ERROR
    assert rig.channels_path.read_bytes() == before
    assert _enabled_placeholders(rig.channels_path) == []
    assert _no_temp_files(rig.config_dir)
    err = capsys.readouterr().err
    assert "was not changed" in err and f"channel_id = {MMH_ID}" in err
    assert rig.lock.released == 1 and "disconnect" in rig.client.calls


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
