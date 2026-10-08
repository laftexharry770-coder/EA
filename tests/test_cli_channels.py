"""Tests for `xaucopier channels` -- choosing which channels signals come from.

What is pinned hardest:

- **The number is the ordinal.** The list is numbered by `ordinal`, not
  by position in the file, and `on N` / `off N` act on the channel whose
  ordinal is N -- the seeded file deliberately lists ordinal 7 before 3.
- **Listing writes nothing** -- not the database (file hash, a second
  connection's `data_version`), not `channels.toml` -- and never touches
  Telegram.
- **`channels.toml` is never half-written, never enabled at id 0, and
  keeps every other channel, comment and line ending byte for byte**,
  whatever step of the write fails.

Every test works on a `tmp_path` copy of the config; the repository's own
`config/channels.toml` is checked to be byte-identical before and after
every test. Telegram is the offline fake throughout.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import errors
from telethon.tl import types

import xaucopier.cli.channels as channels_module
import xaucopier.cli.main as cli_main
import xaucopier.cli.resolve as resolve_module
from fakes.telegram import FakeTelegramClient
from xaucopier.cli import exit_codes
from xaucopier.cli.watch import load_session as real_load_session
from xaucopier.config.accounts import load_accounts
from xaucopier.config.channels import ChannelCfg, load_channels
from xaucopier.config.loader import ConfigError
from xaucopier.ingest.client import AlreadyRunningError
from xaucopier.ingest.secrets import SecretsBlob
from xaucopier.store import db, messages_repo

REPO = Path(__file__).resolve().parents[1]
REAL_CHANNELS = REPO / "config" / "channels.toml"
SHIPPED = REAL_CHANNELS.read_bytes().decode("utf-8")
ACCOUNTS = load_accounts(REPO / "config" / "accounts.toml")  # read, never written
SIGNAL_TEXT: str = json.loads(
    (REPO / "tests" / "fixtures" / "messages" / "mmh_buy_4tp.json").read_text(encoding="utf-8")
)["text"]

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
GOLD = -1002222222222
QUIET = -1003333333333

#: Appended to the shipped file (channel 1, the placeholder). Ordinal 7
#: comes before ordinal 3 on purpose; the comments must survive every edit.
EXTRA = """
# Gold Room: resolved long ago -- keep this comment
[[channels]]
channel_id = -1002222222222   # do not touch
name       = "Gold Room"
parser     = "mmh"
ordinal    = 7
enabled    = true

[[channels]]
channel_id = -1003333333333
name       = "Quiet Signals"
parser     = "mmh"
ordinal    = 3        # numbered 3 although it comes last
enabled    = false    # off for now
"""
SEEDED_TOML = SHIPPED + EXTRA

GOLD_CFG = ChannelCfg(channel_id=GOLD, name="Gold Room", parser="mmh", ordinal=7, enabled=True)
QUIET_CFG = ChannelCfg(channel_id=QUIET, name="Quiet Signals", parser="mmh", ordinal=3, enabled=True)


@pytest.fixture(autouse=True)
def _the_real_config_is_never_touched():
    before = hashlib.sha256(REAL_CHANNELS.read_bytes()).hexdigest()
    yield
    assert hashlib.sha256(REAL_CHANNELS.read_bytes()).hexdigest() == before


def _ingest(conn: sqlite3.Connection, channel: ChannelCfg, message_id: int, text: str, posted: datetime):
    return messages_repo.ingest(
        conn,
        channel_cfg=channel,
        message_id=message_id,
        text=text,
        posted_at_utc=posted,
        edit_date_utc=None,
        reply_to_message_id=None,
        source="live",
        now_utc=NOW,
        accounts=ACCOUNTS,
    )


def _seed_db(path: Path) -> None:
    conn = db.connect(path, role="writer")
    try:
        t0 = NOW - timedelta(hours=6)
        # Three signals an hour apart (outside the 15-minute relay window).
        for offset, message_id in enumerate((10, 11, 12)):
            result = _ingest(conn, GOLD_CFG, message_id, SIGNAL_TEXT, t0 + timedelta(hours=offset))
            assert result.outcome == "new" and result.signal_id is not None
        _ingest(conn, GOLD_CFG, 13, "Good morning traders", t0 + timedelta(hours=4))
        _ingest(conn, QUIET_CFG, 5, "Good morning traders", NOW - timedelta(days=3))
        conn.commit()
    finally:
        conn.close()


class Forbidden:
    """Stands in for everything that reaches Telegram or its session:
    `channels`, `channels on` and `channels off` must never call it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("called")
        raise AssertionError("this command must not contact Telegram")


@dataclass
class Rig:
    config_dir: Path
    db_path: Path
    watch_running: bool
    telegram: Forbidden

    @property
    def channels_path(self) -> Path:
        return self.config_dir / "channels.toml"

    def argv(self, *extra: str) -> list[str]:
        return [
            "channels",
            *extra,
            "--config-dir", str(self.config_dir),
            "--db", str(self.db_path),
            "--secrets", str(self.config_dir.parent / "no-session.dpapi"),
        ]

    def files(self) -> list[str]:
        return sorted(p.name for p in self.config_dir.iterdir())


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    # Bytes, so the file's own line endings are kept exactly.
    (config_dir / "channels.toml").write_bytes(SEEDED_TOML.encode("utf-8"))
    (config_dir / "accounts.toml").write_bytes((REPO / "config" / "accounts.toml").read_bytes())
    db_path = tmp_path / "state.db"
    _seed_db(db_path)
    rig = Rig(config_dir=config_dir, db_path=db_path, watch_running=False, telegram=Forbidden())
    monkeypatch.setattr(channels_module, "session_in_use", lambda: rig.watch_running)
    for name in ("build_one_shot_client", "load_session", "acquire_single_instance_lock"):
        if hasattr(channels_module, name):
            monkeypatch.setattr(channels_module, name, rig.telegram)
    return rig


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- the list ---------------------------------------------------------------------------------


def test_the_list_is_numbered_by_ordinal_and_shows_everything_about_each_channel(
    rig: Rig, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(channels_module, "_utcnow", lambda: NOW)

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_OK

    out = capsys.readouterr().out
    # By number (ordinal), not by place in the file: 1, 3, 7.
    headers = [line.strip() for line in out.splitlines() if line.startswith("  ") and ". " in line[:6]]
    assert headers == [
        "1. MARKET MASTERY HUB / COMMUNITY -- OFF",
        "3. Quiet Signals -- OFF",
        "7. Gold Room -- ON",
    ]
    placeholder, quiet, gold = (out.split(header, 1)[1].split("\n\n", 1)[0] for header in headers)
    assert "NOT LOOKED UP YET" in placeholder and "`.\\xaucopier resolve-channels`" in placeholder
    assert f"id: {QUIET} (looked up)" in quiet
    assert f"id: {GOLD} (looked up)" in gold
    assert "parser: mmh (reads the MARKET MASTERY HUB message layout)" in gold
    # Gold Room: its newest message (#13, 2 h ago) and its three signals.
    assert "posted 2026-09-23 10:00:00 UTC (2 h 0 min ago)" in gold
    assert "messages: 4 | signals: 3" in gold
    assert "posted 2026-09-20 12:00:00 UTC (3 days ago)" in quiet
    assert "messages: 1 | signals: 0" in quiet
    assert "last message recorded: none | signals: 0" in placeholder
    assert "1 of 3 channel(s) ON." in out
    assert "`.\\xaucopier channels on NUMBER`" in out and "`.\\xaucopier channels add`" in out
    assert "watch is running" not in out


def test_listing_writes_nothing_and_never_contacts_telegram(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    db_before = _sha(rig.db_path)
    toml_before = rig.channels_path.read_bytes()
    files_before = rig.files()
    observer = sqlite3.connect(rig.db_path)
    try:
        (version_before,) = observer.execute("PRAGMA data_version").fetchone()
        (rows_before,) = observer.execute("SELECT COUNT(*) FROM metrics").fetchone()

        assert cli_main.main(rig.argv()) == exit_codes.EXIT_OK

        # data_version changes when ANOTHER connection commits a write.
        assert observer.execute("PRAGMA data_version").fetchone()[0] == version_before
        assert observer.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == rows_before
    finally:
        observer.close()
    assert _sha(rig.db_path) == db_before
    assert rig.channels_path.read_bytes() == toml_before
    assert rig.files() == files_before
    assert rig.telegram.calls == []
    assert "Gold Room" in capsys.readouterr().out


def test_with_no_database_yet_it_lists_the_channels_and_creates_nothing(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = rig.db_path.with_name("never-created.db")

    code = cli_main.main(["channels", "--config-dir", str(rig.config_dir), "--db", str(missing)])

    assert code == exit_codes.EXIT_OK
    assert not missing.exists()
    out = capsys.readouterr().out
    assert "Nothing has been recorded yet" in out and "7. Gold Room -- ON" in out


def test_while_watch_runs_the_list_says_changes_wait_for_its_next_start(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.watch_running = True

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_OK

    out = capsys.readouterr().out
    assert "watch is running now" in out
    assert "takes effect the next time watch starts" in out
    assert rig.telegram.calls == []


def test_with_no_channel_on_the_list_warns_that_watch_will_not_start(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.channels_path.write_bytes(
        SEEDED_TOML.replace("ordinal    = 7\nenabled    = true", "ordinal    = 7\nenabled    = false")
        .encode("utf-8")
    )

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_OK
    out = capsys.readouterr().out
    assert "No channel is ON, so `.\\xaucopier watch` will refuse to start" in out
    assert "`.\\xaucopier channels on NUMBER`" in out


def test_a_broken_channels_file_is_reported_not_listed(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.channels_path.write_bytes(SEEDED_TOML.replace("ordinal    = 3", "ordinal    = 7").encode("utf-8"))
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv()) == exit_codes.EXIT_CONFIG_ERROR
    err = capsys.readouterr().err
    assert "ordinal 7 is already used" in err and "refuse to start" in err
    assert rig.channels_path.read_bytes() == before


def test_the_seeded_config_is_valid(rig: Rig) -> None:
    """Guards the fixture itself: the loader accepts it as written."""
    assert [c.ordinal for c in load_channels(rig.channels_path)] == [1, 7, 3]


# -- on / off ---------------------------------------------------------------------------------


def _changed_lines(before: bytes, after: bytes) -> list[tuple[str, str]]:
    old = before.decode("utf-8").splitlines(keepends=True)
    new = after.decode("utf-8").splitlines(keepends=True)
    assert len(old) == len(new), "lines were added or removed"
    return [(a, b) for a, b in zip(old, new) if a != b]


def _switches(path: Path) -> dict[int, bool]:
    return {c.ordinal: c.enabled for c in load_channels(path)}


def _no_temp_files(rig: Rig) -> bool:
    return rig.files() == ["accounts.toml", "channels.toml"]


def test_on_switches_on_exactly_the_channel_with_that_number(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    before = rig.channels_path.read_bytes()

    # Channel 3 is the LAST block in the file: the number is the ordinal.
    assert cli_main.main(rig.argv("on", "3")) == exit_codes.EXIT_OK

    after = rig.channels_path.read_bytes()
    ((old, new),) = _changed_lines(before, after)
    assert old == "enabled    = false    # off for now\n"
    assert new.startswith("enabled    = true     # switched on on ")
    assert new.endswith("(channels on 3)\n")
    assert _switches(rig.channels_path) == {1: False, 7: True, 3: True}
    assert "keep this comment" in after.decode() and "# do not touch" in after.decode()
    assert _no_temp_files(rig)
    assert rig.telegram.calls == []
    out = capsys.readouterr().out
    assert "Channel 3 (Quiet Signals) is now ON." in out
    assert "takes effect the next time you run `.\\xaucopier watch`" in out
    # A channel never read: nothing more to say, and no blank line for it.
    assert "Channel 3 (Quiet Signals) is now ON.\nwatch reads this list" in out


def test_off_switches_off_exactly_that_channel_and_keeps_crlf_line_endings(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    crlf = SEEDED_TOML.replace("\n", "\r\n").replace(
        "ordinal    = 3        # numbered 3 although it comes last\r\nenabled    = false",
        "ordinal    = 3        # numbered 3 although it comes last\r\nenabled    = true ",
    )
    rig.channels_path.write_bytes(crlf.encode("utf-8"))
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK

    after = rig.channels_path.read_bytes()
    ((old, new),) = _changed_lines(before, after)
    assert old == "enabled    = true\r\n"
    assert new.startswith("enabled    = false # switched off on ") and new.endswith("(channels off 7)\r\n")
    assert after.count(b"\r\n") == after.count(b"\n")  # every line ending kept as CRLF
    assert _switches(rig.channels_path) == {1: False, 7: False, 3: True}
    out = capsys.readouterr().out
    assert "Channel 7 (Gold Room) is now OFF." in out
    assert "WARNING" not in out  # channel 3 is still on
    assert rig.telegram.calls == []


def test_off_of_the_last_channel_on_is_done_but_warns_that_watch_will_not_start(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK

    assert _switches(rig.channels_path) == {1: False, 7: False, 3: False}
    out = capsys.readouterr().out
    assert "WARNING: that was the last channel switched on" in out
    assert "`.\\xaucopier watch` will refuse to start (exit code 5)" in out
    assert "`.\\xaucopier channels on NUMBER`" in out


def test_on_refuses_a_channel_whose_id_is_still_0(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv("on", "1")) == exit_codes.EXIT_FAILED

    assert rig.channels_path.read_bytes() == before
    assert _no_temp_files(rig)
    err = capsys.readouterr().err
    assert "was NOT switched on" in err and "channel_id is still 0" in err
    assert "`.\\xaucopier channels add`" in err and "`.\\xaucopier resolve-channels`" in err


def test_on_refuses_a_channel_whose_parser_does_not_exist(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.channels_path.write_bytes(
        SEEDED_TOML.replace('name       = "Quiet Signals"\nparser     = "mmh"',
                            'name       = "Quiet Signals"\nparser     = "gold"').encode("utf-8")
    )
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv("on", "3")) == exit_codes.EXIT_FAILED

    assert rig.channels_path.read_bytes() == before
    assert _no_temp_files(rig)
    err = capsys.readouterr().err
    assert "Channel 3 (Quiet Signals) was NOT switched on" in err
    assert "parser 'gold', which xaucopier does not have" in err
    assert "The only parser built so far is mmh" in err


def test_on_refuses_a_second_copy_of_a_channel_already_on(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Since final review F40 a channels.toml that lists one Telegram
    channel twice does not load at all (on or off), so `channels on`
    stops at the file, names both blocks and changes nothing."""
    twin = EXTRA.split("[[channels]]")[1].replace("ordinal    = 7\nenabled    = true",
                                                 "ordinal    = 9\nenabled    = false")
    rig.channels_path.write_bytes((SEEDED_TOML + "\n[[channels]]" + twin).encode("utf-8"))
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv("on", "9")) == exit_codes.EXIT_CONFIG_ERROR

    assert rig.channels_path.read_bytes() == before
    err = capsys.readouterr().err
    assert "same Telegram channel" in err
    assert "(ordinal 9)" in err and "(ordinal 7)" in err
    assert "Nothing was changed" in err


@pytest.mark.parametrize(("argv", "code"), [(("on", "4"), 1), (("on",), 2), (("off",), 2)])
def test_a_missing_or_unknown_number_changes_nothing(
    rig: Rig, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...], code: int
) -> None:
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv(*argv)) == code

    assert rig.channels_path.read_bytes() == before
    assert "`.\\xaucopier channels" in capsys.readouterr().err


def test_switching_to_the_state_it_is_already_in_writes_nothing(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv("on", "7")) == exit_codes.EXIT_OK
    assert cli_main.main(rig.argv("off", "3")) == exit_codes.EXIT_OK

    assert rig.channels_path.read_bytes() == before
    out = capsys.readouterr().out
    assert "is already ON" in out and "is already OFF" in out


def test_while_watch_runs_a_switch_is_saved_and_says_to_restart_watch(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    rig.watch_running = True

    assert cli_main.main(rig.argv("on", "3")) == exit_codes.EXIT_OK

    assert _switches(rig.channels_path)[3] is True
    out = capsys.readouterr().out
    assert "watch is running now" in out and "takes effect the next time watch starts" in out
    assert rig.telegram.calls == []


# -- the write is all or nothing --------------------------------------------------------------


def _boom(*args: Any, **kwargs: Any) -> Any:
    raise OSError("injected failure")


def _half_then_fail(path: Path, data: bytes) -> None:
    """The write is interrupted half-way: half the bytes reach `path`."""
    with open(path, "xb") as handle:
        handle.write(data[: len(data) // 2])
    raise OSError("injected: interrupted mid-write")


def _inject(where: str, monkeypatch: pytest.MonkeyPatch, rig: Rig) -> None:
    """Make step `where` of the save fail."""
    import os

    if where == "text edit":
        def refuse(*args: Any, **kwargs: Any) -> str:
            raise resolve_module.ResolveError("injected: the edit could not be made")

        monkeypatch.setattr(resolve_module, "set_enabled", refuse)
        monkeypatch.setattr(resolve_module, "append_channel", refuse, raising=False)
    elif where == "temp write":
        monkeypatch.setattr(resolve_module, "_fsync_write", _half_then_fail)
    elif where == "fsync":
        monkeypatch.setattr(os, "fsync", _boom)
    elif where == "reload":
        # The real loader rejecting what was written.
        def rejects(*args: Any, **kwargs: Any) -> Any:
            raise ConfigError("injected: the loader rejects the edited file")

        monkeypatch.setattr(resolve_module, "load_channels", rejects)
    elif where == "compare":
        # A sloppy edit that also renames another channel -- which the
        # loader would accept -- must be caught by the comparison.
        for name in ("set_enabled", "append_channel"):
            real = getattr(resolve_module, name, None)
            if real is not None:
                monkeypatch.setattr(
                    resolve_module,
                    name,
                    lambda *a, _real=real, **k: _real(*a, **k).replace('"Gold Room"', '"Renamed"'),
                )
    elif where == "changed meanwhile":
        real_write = resolve_module._fsync_write

        def someone_else_edits(path: Path, data: bytes) -> None:
            real_write(path, data)
            rig.channels_path.write_bytes(rig.channels_path.read_bytes() + b"# edited by hand\n")

        monkeypatch.setattr(resolve_module, "_fsync_write", someone_else_edits)
    elif where == "replace":
        monkeypatch.setattr(os, "replace", _boom)
    else:
        raise AssertionError(where)


WRITE_STEPS = ("text edit", "temp write", "fsync", "reload", "compare", "changed meanwhile", "replace")
#: Final review F30: a failure that running the command again can fix -- the
#: file changed meanwhile, or the disk refused a write or the final rename
#: (another program had the file open) -- is exit 1. A file the edit or the
#: loader cannot accept is a setup problem: exit 5.
STEP_CODE = {
    "text edit": 5,
    "temp write": 1,
    "fsync": 1,
    "reload": 5,
    "compare": 5,
    "changed meanwhile": 1,
    "replace": 1,
}


@pytest.mark.parametrize("where", WRITE_STEPS)
@pytest.mark.parametrize("argv", [("on", "3"), ("off", "7")], ids=["on", "off"])
def test_a_failure_at_any_step_of_a_switch_leaves_the_file_exactly_as_it_was(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    where: str,
    argv: tuple[str, str],
) -> None:
    _inject(where, monkeypatch, rig)
    before = rig.channels_path.read_bytes()

    code = cli_main.main(rig.argv(*argv))

    assert code == STEP_CODE[where]
    if where == "changed meanwhile":
        assert rig.channels_path.read_bytes() == before + b"# edited by hand\n"
    else:
        assert rig.channels_path.read_bytes() == before
    assert _no_temp_files(rig)
    err = capsys.readouterr().err
    if where == "changed meanwhile":
        # Something else changed the file: nothing is claimed about it but
        # that this command wrote nothing.
        assert "was changed by something else" in err
        assert "Nothing was written by this command" in err
        assert "The file was not changed" not in err
    else:
        assert "The file was not changed" in err
    assert ("run the command again" in err.lower()) is (STEP_CODE[where] == 1)
    if where == "compare":
        assert "differs from the original" in err


def test_a_layout_it_cannot_edit_safely_is_refused_not_guessed(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Channels written as an inline array: valid, but not a layout the
    line edit can change safely -- refused, never guessed at."""
    rig.channels_path.write_bytes(
        b'channels = [\n  {channel_id = -1005, name = "X", parser = "mmh", ordinal = 2, '
        b"enabled = false},\n]\n"
    )
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv("on", "2")) == exit_codes.EXIT_CONFIG_ERROR
    assert rig.channels_path.read_bytes() == before
    assert _no_temp_files(rig)


# -- add --------------------------------------------------------------------------------------

SILVER = types.Channel(
    id=4444444444,
    title='Silver "Signals" \\ \U0001F680',
    photo=types.ChatPhotoEmpty(),
    date=None,
    broadcast=True,
)
SILVER_ID = -1004444444444
GOLD_ENTITY = types.Channel(id=2222222222, title="Gold Room", photo=types.ChatPhotoEmpty(), date=None)
QUIET_ENTITY = types.Channel(id=3333333333, title="Quiet Signals", photo=types.ChatPhotoEmpty(), date=None)
MMH_ENTITY = types.Channel(
    id=1234567890, title="MARKET MASTERY HUB / COMMUNITY", photo=types.ChatPhotoEmpty(), date=None
)
A_USER = types.User(id=42, first_name="Not A Channel")


@dataclass
class AddClient(FakeTelegramClient):
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
class AddRig:
    rig: Rig
    client: AddClient
    lock: Lock
    answers: list[str]

    def argv(self, *extra: str) -> list[str]:
        return self.rig.argv("add", *extra)


@pytest.fixture
def add_rig(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> AddRig:
    client = AddClient(
        entities={
            "https://t.me/silver": SILVER,
            "https://t.me/goldroom": GOLD_ENTITY,
            "https://t.me/quiet": QUIET_ENTITY,
            "https://t.me/mmhcommunity": MMH_ENTITY,
            "@someuser": A_USER,
        },
        dialogs=(SimpleNamespace(entity=A_USER), SimpleNamespace(entity=GOLD_ENTITY),
                 SimpleNamespace(entity=SILVER)),
    )
    lock = Lock()
    answers: list[str] = []
    monkeypatch.setattr(
        channels_module, "load_session",
        lambda path: SecretsBlob(api_id=1, api_hash="0" * 32, session_string="NOT_REAL"),
    )
    monkeypatch.setattr(channels_module, "build_one_shot_client", lambda blob: client)
    monkeypatch.setattr(channels_module, "acquire_single_instance_lock", lock)
    monkeypatch.setattr("builtins.input", lambda prompt="": answers.pop(0))
    return AddRig(rig=rig, client=client, lock=lock, answers=answers)


def test_add_appends_the_channel_as_the_next_number_keeping_every_byte(
    add_rig: AddRig, capsys: pytest.CaptureFixture[str]
) -> None:
    path = add_rig.rig.channels_path
    before = path.read_bytes()
    old = load_channels(path)

    assert cli_main.main(add_rig.argv("--link", "https://t.me/silver", "--yes")) == exit_codes.EXIT_OK

    after = path.read_bytes()
    assert after.startswith(before)  # every byte of the old file kept, comments included
    added = after[len(before):].decode("utf-8")
    assert added.startswith("\n# Silver") and added.count("[[channels]]") == 1
    loaded = load_channels(path)
    assert loaded[:-1] == old  # every other channel exactly as it was
    # One more than the highest number in use (1, 7, 3 -> 8) -- NOT how
    # many channels there are (3 -> 4).
    assert loaded[-1] == ChannelCfg(
        channel_id=SILVER_ID, name=SILVER.title, parser="mmh", ordinal=8, enabled=True
    )
    assert _no_temp_files(add_rig.rig)
    assert add_rig.client.asked == ["https://t.me/silver"]
    assert add_rig.client.calls[0] == "connect" and add_rig.client.calls[-1] == "disconnect"
    assert add_rig.lock.acquired == add_rig.lock.released == 1
    out = capsys.readouterr().out
    # Final review F07: what the caveat says must be true of what ingest does.
    caveat = channels_module.FORMAT_CAVEAT
    assert f"PLEASE NOTE: {caveat}" in out
    assert "only reads trade signals written in the MARKET MASTERY HUB layout" in caveat
    assert "written any other way is recorded but NOT traded" in caveat
    assert "5-10 of its real messages" in caveat
    # A message that reads as an instruction is recorded as one (and queued),
    # whatever the layout -- never "nothing" from this channel.
    assert "recorded as an instruction" in caveat and "close all" in caveat
    assert "nothing will be traded from it" not in caveat
    # A layout it cannot read is loud: the alert, by name, and what it means.
    assert "TEMPLATE_CHANGED_SUSPECTED" in caveat and "alert" in caveat
    assert "writes some calls in a layout this program does not read" in caveat
    assert "is now channel 8, switched ON" in out
    assert "`.\\xaucopier channels off 8`" in out


def test_add_by_picking_from_the_joined_channels(add_rig: AddRig) -> None:
    add_rig.answers.extend(["", "2", "yes"])  # Enter; "2" = Silver (the user is not listed); confirm

    assert cli_main.main(add_rig.argv()) == exit_codes.EXIT_OK

    assert load_channels(add_rig.rig.channels_path)[-1].channel_id == SILVER_ID
    assert "get_entity" not in add_rig.client.calls
    assert add_rig.answers == []


def test_add_keeps_crlf_line_endings_and_a_missing_final_newline(add_rig: AddRig) -> None:
    path = add_rig.rig.channels_path
    path.write_bytes(SEEDED_TOML.rstrip("\n").replace("\n", "\r\n").encode("utf-8"))
    before = path.read_bytes()
    assert not before.endswith(b"\n")

    assert cli_main.main(add_rig.argv("--link", "https://t.me/silver", "--yes")) == 0

    after = path.read_bytes()
    assert after.startswith(before + b"\r\n")
    assert after.count(b"\n") == after.count(b"\r\n")
    assert load_channels(path)[-1].ordinal == 8


def test_add_to_a_file_with_no_channel_gives_number_1(add_rig: AddRig) -> None:
    path = add_rig.rig.channels_path
    path.write_bytes(b"# no channels yet\n")

    assert cli_main.main(add_rig.argv("--link", "https://t.me/silver", "--yes")) == 0

    (only,) = load_channels(path)
    assert (only.channel_id, only.ordinal, only.enabled) == (SILVER_ID, 1, True)
    assert path.read_bytes().startswith(b"# no channels yet\n")


@pytest.mark.parametrize(
    ("link", "number", "state"),
    [("https://t.me/goldroom", 7, "ON"), ("https://t.me/quiet", 3, "OFF")],
)
def test_add_refuses_a_channel_already_in_the_file(
    add_rig: AddRig, capsys: pytest.CaptureFixture[str], link: str, number: int, state: str
) -> None:
    before = add_rig.rig.channels_path.read_bytes()

    assert cli_main.main(add_rig.argv("--link", link, "--yes")) == exit_codes.EXIT_FAILED

    assert add_rig.rig.channels_path.read_bytes() == before
    assert _no_temp_files(add_rig.rig)
    err = capsys.readouterr().err
    assert f"already in channels.toml as channel {number}" in err
    assert f"and it is {state}. Nothing was added." in err
    assert ("`.\\xaucopier channels on 3`" in err) is (state == "OFF")


def test_add_sends_the_channel_waiting_for_its_id_to_resolve_channels(
    add_rig: AddRig, capsys: pytest.CaptureFixture[str]
) -> None:
    before = add_rig.rig.channels_path.read_bytes()

    code = cli_main.main(add_rig.argv("--link", "https://t.me/mmhcommunity", "--yes"))

    assert code == exit_codes.EXIT_FAILED
    assert add_rig.rig.channels_path.read_bytes() == before
    err = capsys.readouterr().err
    assert "as channel 1, waiting for its id" in err and "`.\\xaucopier resolve-channels`" in err


def test_add_while_watch_holds_the_session_says_so_and_does_nothing(
    add_rig: AddRig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def held() -> None:
        raise AlreadyRunningError("held")

    monkeypatch.setattr(channels_module, "acquire_single_instance_lock", held)
    before = add_rig.rig.channels_path.read_bytes()
    add_rig.answers.extend(["https://t.me/silver", "yes"])

    code = cli_main.main(add_rig.argv())

    assert code == exit_codes.EXIT_ALREADY_RUNNING
    assert add_rig.client.calls == []  # never connected
    assert add_rig.answers == ["https://t.me/silver", "yes"]  # never asked anything
    assert add_rig.rig.channels_path.read_bytes() == before
    assert _no_temp_files(add_rig.rig)
    err = capsys.readouterr().err
    assert "watch is running" in err and "Only one program may use the Telegram session" in err
    assert "Stop watch first" in err and "`.\\xaucopier channels add` again" in err
    assert "Traceback" not in err


@pytest.mark.parametrize("where", ["get_entity", "get_dialogs"])
@pytest.mark.parametrize(
    ("error", "code", "words"),
    [
        (errors.ApiIdInvalidError(request=None), 5, "https://my.telegram.org"),
        (errors.ApiIdPublishedFloodError(request=None), 5, "https://my.telegram.org"),
        (errors.SessionRevokedError(request=None), 3, "`.\\xaucopier setup-login`"),
        (errors.UserDeactivatedBanError(request=None), 3, "`.\\xaucopier setup-login`"),
        (errors.AuthKeyDuplicatedError(request=None), 3, "`.\\xaucopier setup-login`"),
    ],
    ids=["api_id invalid", "api_id published", "session revoked", "account banned", "key duplicated"],
)
def test_add_stops_on_a_permanent_telegram_error_with_its_exit_code(
    add_rig: AddRig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    where: str,
    error: Exception,
    code: int,
    words: str,
) -> None:
    async def refused(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(add_rig.client, where, refused)
    before = add_rig.rig.channels_path.read_bytes()
    argv = add_rig.argv("--link", "https://t.me/silver") if where == "get_entity" else add_rig.argv()
    add_rig.answers.append("")  # get_dialogs: Enter, to pick from the list

    assert cli_main.main(argv) == code

    assert add_rig.rig.channels_path.read_bytes() == before
    err = capsys.readouterr().err
    assert words in err and "Traceback" not in err
    # One plain sentence, then the exit code (F26).
    assert [line for line in err.splitlines() if line.strip()][1:] == [f"(exit code {code})"]
    assert "disconnect" in add_rig.client.calls
    assert add_rig.lock.released == 1


def test_add_with_a_logged_out_session_exits_3(add_rig: AddRig) -> None:
    add_rig.client.authorized = False
    before = add_rig.rig.channels_path.read_bytes()

    assert cli_main.main(add_rig.argv("--link", "https://t.me/silver", "--yes")) == 3
    assert add_rig.rig.channels_path.read_bytes() == before


def test_add_without_a_session_file_exits_5_before_taking_the_lock(
    add_rig: AddRig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(channels_module, "load_session", real_load_session)

    assert cli_main.main(add_rig.argv("--link", "https://t.me/silver")) == 5
    assert add_rig.lock.acquired == 0 and add_rig.client.calls == []
    assert "channels add cannot start" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("answers", "link", "message"),
    [
        (["https://t.me/silver", "no"], None, "Not added"),
        ([], "https://t.me/nosuchchannel", "Could not find the channel"),
        ([], "@someuser", "is not a channel"),
        (["", ""], None, "Cancelled"),
    ],
    ids=["declined", "not found", "a user, not a channel", "cancelled at the list"],
)
def test_add_that_goes_no_further_changes_nothing(
    add_rig: AddRig,
    capsys: pytest.CaptureFixture[str],
    answers: list[str],
    link: str | None,
    message: str,
) -> None:
    add_rig.answers.extend(answers)
    before = add_rig.rig.channels_path.read_bytes()
    extra = ("--link", link) if link is not None else ()

    assert cli_main.main(add_rig.argv(*extra)) == exit_codes.EXIT_FAILED

    assert add_rig.rig.channels_path.read_bytes() == before
    captured = capsys.readouterr()
    assert message in captured.out + captured.err


def test_add_takes_no_number(add_rig: AddRig, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main.main(add_rig.argv("3")) == exit_codes.EXIT_USAGE
    assert add_rig.lock.acquired == 0 and add_rig.client.calls == []


@pytest.mark.parametrize("where", WRITE_STEPS)
def test_a_failure_at_any_step_of_an_add_leaves_the_file_exactly_as_it_was(
    add_rig: AddRig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    where: str,
) -> None:
    rig = add_rig.rig
    _inject(where, monkeypatch, rig)
    before = rig.channels_path.read_bytes()

    code = cli_main.main(add_rig.argv("--link", "https://t.me/silver", "--yes"))

    assert code == STEP_CODE[where]
    if where == "changed meanwhile":
        assert rig.channels_path.read_bytes() == before + b"# edited by hand\n"
    else:
        assert rig.channels_path.read_bytes() == before
    assert _no_temp_files(rig)
    err = capsys.readouterr().err
    if where == "changed meanwhile":
        assert "Nothing was added by this command" in err
    else:
        assert "The file was not changed: nothing was added." in err
    assert ("`.\\xaucopier channels add` again" in err) is (STEP_CODE[where] == 1)
    if where == "compare":
        assert "differs from the original" in err
    assert add_rig.lock.released == 1


# -- F33: two commands writing channels.toml at once ------------------------------------------


def _run_in_thread(name: str, argv: list[str], results: dict[str, int]):
    import threading

    def run() -> None:
        results[name] = cli_main.main(argv)

    thread = threading.Thread(target=run, name=name)
    thread.start()
    return thread


def test_two_switches_at_once_both_end_true_the_later_one_changes_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final review F33: A (`channels on 3`) passes every check and is about
    to replace the file; B (`channels off 7`) starts meanwhile. Before, B
    deleted A's temporary file (one fixed name), A's replace then moved
    B's text into place, and both reported the opposite of what happened.
    Now the check-and-replace is one step no other xaucopier command can
    enter: A's change lands, and B -- which read the file before A
    changed it -- says truthfully that it wrote nothing."""
    import os
    import threading
    import time

    real_replace = os.replace
    a_inside, b_started = threading.Event(), threading.Event()

    def replace(src: Any, dst: Any) -> None:
        if threading.current_thread().name == "A":
            a_inside.set()
            b_started.wait(5)
            time.sleep(0.5)  # B has read the file and is trying to write it
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    real_fsync_write = resolve_module._fsync_write

    def fsync_write(path: Path, data: bytes) -> None:
        # Each writer's own temporary file, never another's.
        assert path.parent == rig.channels_path.parent
        assert not path.exists()
        real_fsync_write(path, data)

    monkeypatch.setattr(resolve_module, "_fsync_write", fsync_write)
    results: dict[str, int] = {}

    a = _run_in_thread("A", rig.argv("on", "3"), results)
    assert a_inside.wait(10)  # A passed every check and is about to replace
    b = _run_in_thread("B", rig.argv("off", "7"), results)
    b_started.set()
    a.join(20)
    b.join(20)

    assert results == {"A": exit_codes.EXIT_OK, "B": exit_codes.EXIT_FAILED}
    assert _switches(rig.channels_path) == {1: False, 7: True, 3: True}
    assert _no_temp_files(rig)
    captured = capsys.readouterr()
    assert "Channel 3 (Quiet Signals) is now ON." in captured.out
    assert "Channel 7 (Gold Room) is now OFF." not in captured.out
    assert "was changed by something else" in captured.err
    assert "Nothing was written by this command" in captured.err


def test_each_write_uses_its_own_temporary_file_beside_channels_toml(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    names: list[str] = []
    real_fsync_write = resolve_module._fsync_write

    def fsync_write(path: Path, data: bytes) -> None:
        names.append(path.name)
        assert path.parent == rig.channels_path.parent
        real_fsync_write(path, data)

    monkeypatch.setattr(resolve_module, "_fsync_write", fsync_write)

    assert cli_main.main(rig.argv("on", "3")) == exit_codes.EXIT_OK
    assert cli_main.main(rig.argv("off", "3")) == exit_codes.EXIT_OK

    assert len(names) == 2 and names[0] != names[1]
    assert all(name.startswith(".channels.toml.") and name.endswith(".tmp") for name in names)
    assert _no_temp_files(rig)


def test_a_write_that_cannot_get_its_turn_changes_nothing_and_says_to_run_again(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Another xaucopier command holds the write for longer than the wait
    (10 s; 0.2 s here): nothing is written, exit 1, run it again."""
    import threading

    monkeypatch.setattr(resolve_module, "CONFIG_LOCK_TIMEOUT_SECONDS", 0.2)
    holding, release = threading.Event(), threading.Event()

    def hold() -> None:
        with resolve_module.config_write_lock(rig.channels_path):
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert holding.wait(5)
        before = rig.channels_path.read_bytes()

        assert cli_main.main(rig.argv("on", "3")) == exit_codes.EXIT_FAILED

        assert rig.channels_path.read_bytes() == before
    finally:
        release.set()
        holder.join(10)
    assert _no_temp_files(rig)
    err = capsys.readouterr().err
    assert "another xaucopier command is changing" in err
    assert "channel 3 is still OFF" in err and "run the command again" in err.lower()
    # Once it is free, the same command goes through.
    assert cli_main.main(rig.argv("on", "3")) == exit_codes.EXIT_OK


def test_a_lock_held_by_an_administrator_window_is_waited_for_like_any_other(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A command running as administrator holds a lock this window may not
    open (access denied): that is "busy", waited for, never a traceback."""
    import pywintypes
    import winerror

    real_create = resolve_module.win32event.CreateMutex
    denied = {"left": 3}

    def create(*args: Any) -> Any:
        if denied["left"]:
            denied["left"] -= 1
            raise pywintypes.error(winerror.ERROR_ACCESS_DENIED, "CreateMutex", "Access is denied.")
        return real_create(*args)

    monkeypatch.setattr(resolve_module.win32event, "CreateMutex", create)

    assert cli_main.main(rig.argv("on", "3")) == exit_codes.EXIT_OK
    assert denied["left"] == 0
    assert _switches(rig.channels_path)[3] is True

    denied["left"] = 10_000
    monkeypatch.setattr(resolve_module, "CONFIG_LOCK_TIMEOUT_SECONDS", 0.2)
    assert cli_main.main(rig.argv("off", "3")) == exit_codes.EXIT_FAILED
    assert _switches(rig.channels_path)[3] is True
    assert "another xaucopier command is changing" in capsys.readouterr().err


def test_a_writer_that_died_holding_the_write_does_not_block_the_next_one(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Windows hands a mutex whose holder ended without releasing it to the
    next waiter as "abandoned": the write is the next one's to make."""
    import threading

    import win32api
    import win32event

    handles: list[Any] = []

    def die_holding() -> None:
        handle = win32event.CreateMutex(None, False, resolve_module.config_lock_name(rig.channels_path))
        assert win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_OBJECT_0
        handles.append(handle)  # the thread ends owning it, never releasing it

    holder = threading.Thread(target=die_holding)
    holder.start()
    holder.join(10)
    try:
        assert cli_main.main(rig.argv("on", "3")) == exit_codes.EXIT_OK
    finally:
        win32api.CloseHandle(handles[0])
    assert _switches(rig.channels_path)[3] is True


def test_the_write_lock_is_one_per_file_however_its_path_is_spelled(tmp_path: Path) -> None:
    path = tmp_path / "config" / "channels.toml"
    same = tmp_path / "config" / ".." / "CONFIG" / "Channels.TOML"
    assert resolve_module.config_lock_name(path) == resolve_module.config_lock_name(same)
    assert resolve_module.config_lock_name(path) != resolve_module.config_lock_name(
        tmp_path / "other" / "channels.toml"
    )
    assert resolve_module.config_lock_name(path).startswith("Local\\xaucopier_config_")


# -- F24 / F23 / G2L-1: Telegram out of reach, busy, or the session held ---------------------


def test_add_with_no_network_gives_up_after_its_wait_and_says_so(
    add_rig: AddRig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final review F24: Telethon's connect never gives up, so with no
    network `channels add` ran for ever, holding the session. The connect
    now has 30 s (0.1 s here): exit 1, one plain sentence."""
    import asyncio

    monkeypatch.setattr(resolve_module, "CONNECT_TIMEOUT_SECONDS", 0.1)

    async def never(*args: Any, **kwargs: Any) -> None:
        add_rig.client.calls.append("connect")
        await asyncio.Event().wait()

    monkeypatch.setattr(add_rig.client, "connect", never)
    before = add_rig.rig.channels_path.read_bytes()

    assert cli_main.main(add_rig.argv("--link", "https://t.me/silver", "--yes")) == 1

    assert add_rig.rig.channels_path.read_bytes() == before
    err = capsys.readouterr().err
    assert "Could not reach Telegram within 0.1 seconds" in err
    assert "`.\\xaucopier channels add` again" in err and "Nothing was added." in err
    assert "Traceback" not in err
    assert "disconnect" in add_rig.client.calls and add_rig.lock.released == 1


@pytest.mark.parametrize("where", ["get_dialogs", "get_entity", "get_me"])
def test_add_told_to_wait_by_telegram_says_how_long_in_minutes(
    add_rig: AddRig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], where: str
) -> None:
    """Final review F23: a wait Telegram asks for (longer than the 120 s
    this program waits out by itself) was a raw traceback."""
    async def wait_please(*args: Any, **kwargs: Any) -> Any:
        raise errors.FloodWaitError(request=None, capture=300)

    monkeypatch.setattr(add_rig.client, where, wait_please)
    add_rig.answers.append("")  # Enter: pick from the list (get_dialogs)
    before = add_rig.rig.channels_path.read_bytes()
    argv = add_rig.argv("--link", "https://t.me/silver") if where == "get_entity" else add_rig.argv()

    assert cli_main.main(argv) == exit_codes.EXIT_FAILED

    assert add_rig.rig.channels_path.read_bytes() == before
    err = capsys.readouterr().err
    assert "Telegram asks to wait about 5 minutes, then run it again" in err
    assert "`.\\xaucopier channels add`" in err and "Nothing was added." in err
    assert "Traceback" not in err and "flood" not in err.lower()
    assert [line for line in err.splitlines() if line.strip()][1:] == ["(exit code 1)"]
    assert "disconnect" in add_rig.client.calls and add_rig.lock.released == 1


@pytest.mark.parametrize(
    "error",
    [errors.RpcCallFailError(request=None), errors.BadRequestError(request=None, message="SOMETHING")],
    ids=["telegram trouble (500)", "a request Telegram refuses (400)"],
)
def test_add_meeting_any_other_telegram_error_is_one_plain_sentence(
    add_rig: AddRig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    async def refused(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(add_rig.client, "get_dialogs", refused)
    add_rig.answers.append("")

    assert cli_main.main(add_rig.argv()) == exit_codes.EXIT_FAILED

    err = capsys.readouterr().err
    assert f"Telegram could not do this just now (Telegram error {error.code})" in err
    assert "Nothing was added." in err and "Traceback" not in err
    assert "RPC" not in err
    assert add_rig.lock.released == 1


def test_add_while_an_administrator_window_holds_the_session_says_so(
    add_rig: AddRig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Group-2 review leftover G2L-1: the real lock's hint -- probably an
    administrator window -- reaches the user, not only the fixed text."""
    import pywintypes
    import winerror

    import xaucopier.ingest.client as client_module

    def denied(*args: object) -> object:
        raise pywintypes.error(winerror.ERROR_ACCESS_DENIED, "CreateMutex", "Access is denied.")

    monkeypatch.setattr(client_module.win32event, "CreateMutex", denied)
    monkeypatch.setattr(channels_module, "acquire_single_instance_lock",
                        client_module.acquire_single_instance_lock)

    assert cli_main.main(add_rig.argv("--link", "https://t.me/silver", "--yes")) == 6

    err = capsys.readouterr().err
    assert "Stop watch first" in err
    assert "administrator" in err
    assert add_rig.client.calls == []


# -- F05: a group is not a channel ------------------------------------------------------------

VIP_CHANNEL = types.Channel(
    id=5555555555, title="Gold Signals VIP", photo=types.ChatPhotoEmpty(), date=None, broadcast=True
)
VIP_GROUP = types.Channel(
    id=6666666666, title="Gold Signals VIP", photo=types.ChatPhotoEmpty(), date=None, megagroup=True
)
VIP_GROUP_ID = -1006666666666
GROUP_WARNING = (
    "This is a group: anyone in it can post, and their messages will be read as trading "
    "instructions."
)


@pytest.fixture
def group_rig(add_rig: AddRig) -> AddRig:
    add_rig.client.dialogs = (SimpleNamespace(entity=VIP_CHANNEL), SimpleNamespace(entity=VIP_GROUP))
    add_rig.client.entities["https://t.me/goldchat"] = VIP_GROUP
    return add_rig


def test_the_picker_says_which_entry_is_a_channel_and_which_a_group(
    group_rig: AddRig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final review F05: a channel and its discussion group often share a
    title; the list showed '1. Gold Signals VIP' twice."""
    group_rig.answers.extend(["", ""])  # Enter for the list, then Enter to cancel

    assert cli_main.main(group_rig.argv()) == exit_codes.EXIT_FAILED

    out = capsys.readouterr().out
    assert "  1. Gold Signals VIP (channel)" in out
    assert "  2. Gold Signals VIP (group)" in out


def test_a_group_picked_from_the_list_needs_its_own_yes(
    group_rig: AddRig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts: list[str] = []
    answers = ["", "2", "yes", "yes"]

    def typed(prompt: str = "") -> str:
        prompts.append(prompt)
        return answers.pop(0)

    monkeypatch.setattr("builtins.input", typed)

    assert cli_main.main(group_rig.argv()) == exit_codes.EXIT_OK

    added = load_channels(group_rig.rig.channels_path)[-1]
    assert (added.channel_id, added.enabled) == (VIP_GROUP_ID, True)
    out = capsys.readouterr().out
    assert GROUP_WARNING in out
    # FF25 (a): a blank line, then the warning -- not ' WARNING:'.
    assert "\n\nWARNING: " + GROUP_WARNING in out
    assert " WARNING: " not in out
    assert "Found: Gold Signals VIP -- a group" in out
    # The group question comes before, and apart from, the ordinary one.
    assert prompts[2] == "Type yes to use it anyway: "
    assert prompts[3].startswith("Add Gold Signals VIP as channel 8")


@pytest.mark.parametrize("answer", ["no", "y", ""])
def test_a_group_not_confirmed_with_yes_is_not_added(
    group_rig: AddRig, capsys: pytest.CaptureFixture[str], answer: str
) -> None:
    before = group_rig.rig.channels_path.read_bytes()
    group_rig.answers.extend(["", "2", answer])

    assert cli_main.main(group_rig.argv()) == exit_codes.EXIT_FAILED

    assert group_rig.rig.channels_path.read_bytes() == before
    assert "Not added. Nothing was changed." in capsys.readouterr().out
    assert group_rig.answers == []  # the ordinary question was never asked


def test_yes_on_the_command_line_does_not_answer_the_group_question(
    group_rig: AddRig, capsys: pytest.CaptureFixture[str]
) -> None:
    before = group_rig.rig.channels_path.read_bytes()
    group_rig.answers.append("no")

    code = cli_main.main(group_rig.argv("--link", "https://t.me/goldchat", "--yes"))

    assert code == exit_codes.EXIT_FAILED
    assert group_rig.rig.channels_path.read_bytes() == before
    assert GROUP_WARNING in capsys.readouterr().out
    assert group_rig.answers == []


def test_with_no_one_to_answer_a_group_is_refused(
    group_rig: AddRig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def closed(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    before = group_rig.rig.channels_path.read_bytes()

    code = cli_main.main(group_rig.argv("--link", "https://t.me/goldchat", "--yes"))

    assert code == exit_codes.EXIT_FAILED
    assert group_rig.rig.channels_path.read_bytes() == before
    assert "No answer could be read" in capsys.readouterr().err


def test_allow_group_is_the_explicit_way_to_add_a_group_without_asking(
    group_rig: AddRig, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli_main.main(
        group_rig.argv("--link", "https://t.me/goldchat", "--yes", "--allow-group")
    )

    assert code == exit_codes.EXIT_OK
    assert load_channels(group_rig.rig.channels_path)[-1].channel_id == VIP_GROUP_ID
    out = capsys.readouterr().out
    assert GROUP_WARNING in out and "--allow-group" in out
    assert group_rig.answers == []


def test_a_channel_needs_no_second_yes(
    group_rig: AddRig, capsys: pytest.CaptureFixture[str]
) -> None:
    group_rig.answers.extend(["", "1", "yes"])

    assert cli_main.main(group_rig.argv()) == exit_codes.EXIT_OK

    assert load_channels(group_rig.rig.channels_path)[-1].channel_id == -1005555555555
    out = capsys.readouterr().out
    assert "Found: Gold Signals VIP -- a channel" in out
    assert "anyone in it can post" not in out


def test_a_source_telegram_does_not_describe_is_treated_as_a_possible_group(
    add_rig: AddRig, capsys: pytest.CaptureFixture[str]
) -> None:
    neither = types.Channel(id=7777777777, title="Mystery", photo=types.ChatPhotoEmpty(), date=None)
    add_rig.client.entities["https://t.me/mystery"] = neither
    add_rig.answers.append("no")

    code = cli_main.main(add_rig.argv("--link", "https://t.me/mystery", "--yes"))

    assert code == exit_codes.EXIT_FAILED
    out = capsys.readouterr().out
    assert "does not say whether this is a channel or a group" in out


def test_a_channel_this_account_has_not_joined_is_added_with_a_warning(
    add_rig: AddRig, capsys: pytest.CaptureFixture[str]
) -> None:
    not_joined = types.Channel(
        id=8888888888, title="Public Gold", photo=types.ChatPhotoEmpty(), date=None,
        broadcast=True, left=True,
    )
    add_rig.client.entities["https://t.me/publicgold"] = not_joined

    assert cli_main.main(add_rig.argv("--link", "https://t.me/publicgold", "--yes")) == 0

    out = capsys.readouterr().out
    assert "This Telegram account has not joined Public Gold" in out
    assert "Join it in the Telegram app" in out


@pytest.mark.parametrize("action", [("on", "3"), ("off", "7"), ()])
def test_allow_group_only_goes_with_add(
    rig: Rig, capsys: pytest.CaptureFixture[str], action: tuple[str, ...]
) -> None:
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv(*action, "--allow-group")) == exit_codes.EXIT_USAGE

    assert rig.channels_path.read_bytes() == before
    assert "only go with `.\\xaucopier channels add`" in capsys.readouterr().err


# -- F28 / G2L-3: a channel switched back on is read again from now ---------------------------

#: `_utcnow()` during these tests: with a fraction, which the cutoff drops.
SWITCHED_AT = NOW + timedelta(microseconds=654321)


def _state_row(db_path: Path, channel_id: int) -> tuple | None:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT last_processed_message_id, seeded_at_utc, seed_cutoff_utc FROM channel_state "
            "WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
    finally:
        conn.close()


def _set_state(db_path: Path, channel_id: int, pointer: int, seeded: str | None) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO channel_state (channel_id, last_processed_message_id, seeded_at_utc) "
            "VALUES (?, ?, ?) ON CONFLICT(channel_id) DO UPDATE SET "
            "last_processed_message_id = excluded.last_processed_message_id, "
            "seeded_at_utc = excluded.seeded_at_utc",
            (channel_id, pointer, seeded),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def on_rig(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Rig:
    monkeypatch.setattr(channels_module, "_utcnow", lambda: SWITCHED_AT)
    return rig


def _periods(db_path: Path, channel_id: int) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT off_at_utc, on_at_utc FROM channel_off_periods WHERE channel_id = ? "
            "ORDER BY period_id",
            (channel_id,),
        ).fetchall()
    finally:
        conn.close()


def _at(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(channels_module, "_utcnow", lambda: moment)


@pytest.mark.parametrize(
    ("pointer", "seeded"),
    [(5, "2026-09-01T00:00:00+00:00"), (0, "2026-09-01T00:00:00+00:00"), (5, None)],
    ids=["seeded", "seeded at 0: every post was newer", "a pointer from before the marker"],
)
def test_off_then_on_skips_exactly_the_time_it_was_off(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pointer: int,
    seeded: str | None,
) -> None:
    """Fix round 2 of F28: `channels off` records when, `channels on` when
    it came back -- whole seconds, UTC -- and nothing else: the next watch
    reads up to the switch-off, then from the switch-on
    (tests/test_channels_off_on.py runs it end to end)."""
    _set_state(rig.db_path, GOLD, pointer, seeded)
    _at(monkeypatch, NOW + timedelta(microseconds=654321))
    assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK
    assert _periods(rig.db_path, GOLD) == [("2026-09-23T12:00:00+00:00", None)]
    off_out = capsys.readouterr().out
    assert "What it posts from now until you switch it back on is never read" in off_out
    assert "What it posted before now that watch has not read yet is still read" in off_out

    _at(monkeypatch, NOW + timedelta(hours=2, microseconds=1))
    assert cli_main.main(rig.argv("on", "7")) == exit_codes.EXIT_OK

    assert _switches(rig.channels_path)[7] is True
    assert _periods(rig.db_path, GOLD) == [
        ("2026-09-23T12:00:00+00:00", "2026-09-23T14:00:00+00:00")
    ]
    # No re-seed: the pointer and marker stay, and no cutoff is written.
    assert _state_row(rig.db_path, GOLD) == (pointer, seeded, None)
    out = capsys.readouterr().out
    assert "Channel 7 (Gold Room) is now ON." in out
    assert (
        "The next `.\\xaucopier watch` reads what it posted until you switched it off "
        "(2026-09-23 12:00:00 UTC) that watch had not read yet, skips what it posted while "
        "it was off, and reads everything it posts from now on." in out
    )


@pytest.mark.parametrize(
    ("pointer", "seeded"),
    [(5, "2026-09-01T00:00:00+00:00"), (0, "2026-09-01T00:00:00+00:00"), (5, None)],
    ids=["seeded", "seeded at 0: every post was newer", "a pointer from before the marker"],
)
def test_on_of_a_channel_whose_switch_off_was_not_recorded_reads_it_from_now(
    on_rig: Rig, capsys: pytest.CaptureFixture[str], pointer: int, seeded: str | None
) -> None:
    """Quiet Signals is OFF in the file with no switch-off recorded (the
    database could not be written then, or the file was edited by hand):
    ruling M12 as round 1 had it -- the next watch reads it from now on."""
    _set_state(on_rig.db_path, QUIET, pointer, seeded)

    assert cli_main.main(on_rig.argv("on", "3")) == exit_codes.EXIT_OK

    assert _switches(on_rig.channels_path)[3] is True
    # Whole seconds, UTC; the pointer is kept (a re-seed never moves it back).
    assert _state_row(on_rig.db_path, QUIET) == (pointer, seeded, "2026-09-23T12:00:00+00:00")
    out = capsys.readouterr().out
    assert "Channel 3 (Quiet Signals) is now ON." in out
    assert (
        "When it was switched off was not recorded, so the next watch reads it from now on: "
        "everything it posted since watch last read it is skipped" in out
    )


@pytest.mark.parametrize("watch_running", [False, True], ids=["no watch", "watch running"])
def test_on_of_a_channel_whose_first_read_never_finished(
    on_rig: Rig, capsys: pytest.CaptureFixture[str], watch_running: bool
) -> None:
    """The reviewer's open (b), with no switch-off recorded: the first-attempt
    cutoff is moved to now only when no watch holds the session (none can
    be seeding the channel at the old one); otherwise it stays, and the
    user is told what that means."""
    conn = sqlite3.connect(on_rig.db_path)
    conn.execute(
        "INSERT INTO channel_state (channel_id, seed_cutoff_utc) VALUES (?, ?) "
        "ON CONFLICT(channel_id) DO UPDATE SET seed_cutoff_utc = excluded.seed_cutoff_utc",
        (QUIET, "2026-09-23T08:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    on_rig.watch_running = watch_running

    assert cli_main.main(on_rig.argv("on", "3")) == exit_codes.EXIT_OK

    cutoff = _state_row(on_rig.db_path, QUIET)[2]
    out = capsys.readouterr().out
    if watch_running:
        assert cutoff == "2026-09-23T08:00:00+00:00"
        assert "what it posted while it was off may still be read" in out
    else:
        assert cutoff == "2026-09-23T12:00:00+00:00"
        assert "the next watch reads it from now on" in out


def test_off_and_on_of_a_channel_never_read_record_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A channel switched OFF and ON again before watch read it at all
    behaves as if it was never off: its first read starts at that watch's
    start, like any new channel's."""
    before = _state_row(rig.db_path, GOLD)
    _at(monkeypatch, NOW)
    assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK
    off_out = capsys.readouterr().out
    _at(monkeypatch, NOW + timedelta(hours=1))
    assert cli_main.main(rig.argv("on", "7")) == exit_codes.EXIT_OK

    assert _periods(rig.db_path, GOLD) == []
    assert _state_row(rig.db_path, GOLD) == before
    assert "What it posts while it is off is never read, not even later." in off_out
    assert "still read" not in off_out
    out = capsys.readouterr().out
    assert "is now ON." in out and "skips" not in out and "from now on:" not in out


def test_a_second_off_replaces_one_whose_save_failed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`channels off` whose save fails leaves the channel ON with its
    switch-off recorded; no read skips an open period, and the next
    `channels off` records its own time instead."""
    _set_state(rig.db_path, GOLD, 13, "2026-09-01T00:00:00+00:00")
    _at(monkeypatch, NOW)
    with monkeypatch.context() as failing:
        _inject("replace", failing, rig)
        assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_FAILED
    assert _switches(rig.channels_path)[7] is True
    _at(monkeypatch, NOW + timedelta(hours=1))
    assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK

    assert _periods(rig.db_path, GOLD) == [("2026-09-23T13:00:00+00:00", None)]


def test_an_on_whose_save_failed_is_completed_by_the_next_on(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The database first, then the switch. `on` whose save fails leaves
    the channel OFF with its period closed; the next `on` extends that
    period to its own time -- what was posted in between is still skipped,
    and what was posted before the switch-off still read."""
    _set_state(rig.db_path, GOLD, 13, "2026-09-01T00:00:00+00:00")
    _at(monkeypatch, NOW)
    assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK
    _at(monkeypatch, NOW + timedelta(hours=1))
    with monkeypatch.context() as failing:
        _inject("replace", failing, rig)
        assert cli_main.main(rig.argv("on", "7")) == exit_codes.EXIT_FAILED
    assert _switches(rig.channels_path)[7] is False
    capsys.readouterr()

    _at(monkeypatch, NOW + timedelta(hours=3))
    assert cli_main.main(rig.argv("on", "7")) == exit_codes.EXIT_OK

    assert _switches(rig.channels_path)[7] is True
    assert _periods(rig.db_path, GOLD) == [
        ("2026-09-23T12:00:00+00:00", "2026-09-23T15:00:00+00:00")
    ]
    assert _state_row(rig.db_path, GOLD)[2] is None
    assert "skips what it posted while it was off" in capsys.readouterr().out


def test_off_while_the_database_is_held_still_switches_off_and_says_what_that_costs(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Switching a channel off must always work."""
    _set_state(rig.db_path, GOLD, 13, "2026-09-01T00:00:00+00:00")
    monkeypatch.setattr(db, "_BUSY_TIMEOUT_MS", 50)
    holder = sqlite3.connect(rig.db_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK
    finally:
        holder.rollback()
        holder.close()

    assert _switches(rig.channels_path)[7] is False
    assert _periods(rig.db_path, GOLD) == []
    out = capsys.readouterr().out
    assert "When it was switched off could not be saved: the database is busy" in out
    assert "everything it posted since watch last read it is skipped too" in out
    assert "Traceback" not in out


def test_off_with_a_database_from_a_newer_version_says_so_in_lower_case_after_the_colon(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Switching off still works; why its time could not be saved follows
    a colon, in lower case (final fix round 2, the FF25 c slip)."""
    from xaucopier.store.migrations import CURRENT_VERSION

    _set_state(rig.db_path, GOLD, 13, "2026-09-01T00:00:00+00:00")
    raw = sqlite3.connect(rig.db_path)
    raw.execute(f"PRAGMA user_version = {CURRENT_VERSION + 1}")
    raw.commit()
    raw.close()

    assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK

    assert _switches(rig.channels_path)[7] is False
    out = capsys.readouterr().out
    assert (
        f"When it was switched off could not be saved: the database at {rig.db_path} was "
        "written by a newer version of xaucopier"
    ) in out


def _hold(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """Another connection holding the database's write lock."""
    monkeypatch.setattr(db, "_BUSY_TIMEOUT_MS", 50)
    holder = sqlite3.connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def test_off_again_after_a_busy_database_saves_when_it_was_switched_off(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final fix FF11. `channels off` with the database busy still switches
    the channel off, but cannot save when. It said "Try again in a
    moment" -- and running it again only answered "already OFF". Its
    advice now names the command to run again, and that command, on a
    channel already OFF whose switch-off was never saved, saves it: the
    next `on` then skips only the time it was off, not everything since
    watch last read it."""
    _set_state(rig.db_path, GOLD, 13, "2026-09-01T00:00:00+00:00")
    _at(monkeypatch, NOW)
    holder = _hold(rig.db_path, monkeypatch)
    try:
        assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK
    finally:
        holder.rollback()
        holder.close()
    out = capsys.readouterr().out
    assert _switches(rig.channels_path)[7] is False and _periods(rig.db_path, GOLD) == []
    assert "Run `.\\xaucopier channels off 7` again in a moment to save it" in out
    assert "Try again in a moment" not in out

    _at(monkeypatch, NOW + timedelta(minutes=5))
    assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK

    out = capsys.readouterr().out
    assert "is already OFF" in out
    assert "When it was switched off had not been saved; it is saved now" in out
    assert _periods(rig.db_path, GOLD) == [("2026-09-23T12:05:00+00:00", None)]
    before = rig.channels_path.read_bytes()

    _at(monkeypatch, NOW + timedelta(hours=3))
    assert cli_main.main(rig.argv("on", "7")) == exit_codes.EXIT_OK
    assert _periods(rig.db_path, GOLD) == [
        ("2026-09-23T12:05:00+00:00", "2026-09-23T15:00:00+00:00")
    ]
    assert _state_row(rig.db_path, GOLD)[2] is None  # no re-seed: nothing more skipped
    assert rig.channels_path.read_bytes() != before


def test_off_again_after_a_busy_database_saves_it_though_an_earlier_period_waits(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final fix round 2 (FF11 case B). An earlier `off`/`on` left a closed
    OFF period no watch has skipped yet. Then `off` with the database busy:
    the advised re-run answered "already OFF. Nothing was changed." -- it
    saved only when there was no period at all -- and the next `on`
    extended the old period across the hour the channel was ON, so what it
    posted then was skipped. The re-run saves the switch-off whenever no
    period is OPEN; the hour it was ON is read."""
    _set_state(rig.db_path, GOLD, 13, "2026-09-01T00:00:00+00:00")
    _at(monkeypatch, NOW)
    assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK
    _at(monkeypatch, NOW + timedelta(minutes=10))
    assert cli_main.main(rig.argv("on", "7")) == exit_codes.EXIT_OK
    _at(monkeypatch, NOW + timedelta(minutes=60))
    holder = _hold(rig.db_path, monkeypatch)
    try:
        assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK
    finally:
        holder.rollback()
        holder.close()
    out = capsys.readouterr().out
    assert "Run `.\\xaucopier channels off 7` again in a moment to save it" in out

    _at(monkeypatch, NOW + timedelta(minutes=61))
    assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK

    out = capsys.readouterr().out
    assert "When it was switched off had not been saved; it is saved now" in out
    assert "Nothing was changed" not in out
    _at(monkeypatch, NOW + timedelta(minutes=90))
    assert cli_main.main(rig.argv("on", "7")) == exit_codes.EXIT_OK
    assert _periods(rig.db_path, GOLD) == [
        ("2026-09-23T12:00:00+00:00", "2026-09-23T12:10:00+00:00"),
        ("2026-09-23T13:01:00+00:00", "2026-09-23T13:30:00+00:00"),
    ]
    assert _state_row(rig.db_path, GOLD)[2] is None  # no re-seed


def test_off_of_a_channel_already_off_keeps_the_time_it_was_switched_off(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Its switch-off saved: `off` again changes nothing, the time
    included."""
    _set_state(rig.db_path, GOLD, 13, "2026-09-01T00:00:00+00:00")
    _at(monkeypatch, NOW)
    assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK
    capsys.readouterr()
    _at(monkeypatch, NOW + timedelta(hours=1))

    assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK

    out = capsys.readouterr().out
    assert out == "Channel 7 (Gold Room) is already OFF. Nothing was changed.\n"
    assert _periods(rig.db_path, GOLD) == [("2026-09-23T12:00:00+00:00", None)]


def test_off_of_a_channel_left_off_by_an_on_not_saved_reads_more_never_less(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An `on` whose save failed left the channel OFF with its period
    closed. In the database that looks exactly like FF11 case B (an
    `off`/`on` saved, then an `off` whose time was not), so `off` now
    saves a switch-off whenever no period is open (final fix round 2): a
    second period. The hour between the two is read although the channel
    was OFF -- more is read, never less; the other way skipped an hour the
    channel was ON."""
    _set_state(rig.db_path, GOLD, 13, "2026-09-01T00:00:00+00:00")
    _at(monkeypatch, NOW)
    assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK
    _at(monkeypatch, NOW + timedelta(hours=1))
    with monkeypatch.context() as failing:
        _inject("replace", failing, rig)
        assert cli_main.main(rig.argv("on", "7")) == exit_codes.EXIT_FAILED
    capsys.readouterr()
    _at(monkeypatch, NOW + timedelta(hours=2))

    assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK

    assert "it is saved now" in capsys.readouterr().out
    _at(monkeypatch, NOW + timedelta(hours=3))
    assert cli_main.main(rig.argv("on", "7")) == exit_codes.EXIT_OK
    assert _periods(rig.db_path, GOLD) == [
        ("2026-09-23T12:00:00+00:00", "2026-09-23T13:00:00+00:00"),
        ("2026-09-23T14:00:00+00:00", "2026-09-23T15:00:00+00:00"),
    ]


def test_off_of_a_channel_already_off_and_never_read_records_nothing(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main.main(rig.argv("off", "3")) == exit_codes.EXIT_OK

    assert "is already OFF. Nothing was changed." in capsys.readouterr().out
    assert _periods(rig.db_path, QUIET) == []


def test_off_of_a_channel_already_off_with_the_database_busy_says_to_run_it_again(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_state(rig.db_path, GOLD, 13, "2026-09-01T00:00:00+00:00")
    _at(monkeypatch, NOW)
    holder = _hold(rig.db_path, monkeypatch)
    try:
        assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK
        capsys.readouterr()
        assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK
    finally:
        holder.rollback()
        holder.close()

    out = capsys.readouterr().out
    assert "is already OFF" in out
    assert "Run `.\\xaucopier channels off 7` again in a moment to save it" in out
    assert _periods(rig.db_path, GOLD) == []


@pytest.mark.parametrize(
    "state",
    [None, (0, None)],
    ids=["never read: no row", "never seeded: a counter's row at 0"],
)
def test_on_of_a_channel_never_read_writes_no_cutoff(
    on_rig: Rig, capsys: pytest.CaptureFixture[str], state: tuple | None
) -> None:
    """Its first read seeds it anyway, at that watch's start."""
    if state is not None:
        _set_state(on_rig.db_path, QUIET, *state)
    before = _state_row(on_rig.db_path, QUIET)

    assert cli_main.main(on_rig.argv("on", "3")) == exit_codes.EXIT_OK

    assert _switches(on_rig.channels_path)[3] is True
    assert _state_row(on_rig.db_path, QUIET) == before
    assert "posted while it was off" not in capsys.readouterr().out


def test_on_with_no_database_yet_creates_none(
    on_rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = on_rig.db_path.with_name("not-yet.db")
    argv = [a if a != str(on_rig.db_path) else str(missing) for a in on_rig.argv("on", "3")]

    assert cli_main.main(argv) == exit_codes.EXIT_OK

    assert not missing.exists()
    assert _switches(on_rig.channels_path)[3] is True


def test_on_while_the_database_is_held_changes_nothing_and_says_so(
    on_rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cutoff and the switch go together: with the database held past
    its wait (10 s; shortened here), neither is written."""
    _set_state(on_rig.db_path, QUIET, 5, "2026-09-01T00:00:00+00:00")
    monkeypatch.setattr(db, "_BUSY_TIMEOUT_MS", 50)
    before = on_rig.channels_path.read_bytes()
    holder = sqlite3.connect(on_rig.db_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert cli_main.main(on_rig.argv("on", "3")) == exit_codes.EXIT_FAILED
    finally:
        holder.rollback()
        holder.close()

    assert on_rig.channels_path.read_bytes() == before
    assert _state_row(on_rig.db_path, QUIET)[2] is None
    err = capsys.readouterr().err
    assert "The database is busy" in err
    assert "Nothing was changed: channel 3 is still OFF" in err and "Traceback" not in err


def test_on_with_a_database_it_cannot_use_changes_nothing(
    on_rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = sqlite3.connect(on_rig.db_path)
    raw.execute("PRAGMA user_version = 99")
    raw.commit()
    raw.close()
    before = on_rig.channels_path.read_bytes()

    assert cli_main.main(on_rig.argv("on", "3")) == exit_codes.EXIT_FAILED

    assert on_rig.channels_path.read_bytes() == before
    err = capsys.readouterr().err
    assert "newer version of xaucopier; use that version" in err
    assert "Nothing was changed: channel 3 is still OFF" in err


def test_the_cutoff_is_written_before_the_switch_so_a_failed_save_leaves_it_off(
    on_rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The order, and why: cutoff first, then channels.toml. A save that
    fails after the cutoff leaves the channel OFF -- never read, so the
    cutoff does nothing, and the next `channels on` writes its own. The
    other order could leave it ON with no cutoff: everything posted while
    it was off read as new."""
    _set_state(on_rig.db_path, QUIET, 5, "2026-09-01T00:00:00+00:00")
    _inject("replace", monkeypatch, on_rig)

    assert cli_main.main(on_rig.argv("on", "3")) == exit_codes.EXIT_FAILED

    assert _switches(on_rig.channels_path)[3] is False
    assert _state_row(on_rig.db_path, QUIET)[2] == "2026-09-23T12:00:00+00:00"


def test_off_writes_only_when_it_was_switched_off(on_rig: Rig) -> None:
    """Round 1 pinned that OFF wrote nothing; fix round 2 records one row,
    and touches nothing else."""
    _set_state(on_rig.db_path, GOLD, 13, "2026-09-01T00:00:00+00:00")
    before = _state_row(on_rig.db_path, GOLD)

    assert cli_main.main(on_rig.argv("off", "7")) == exit_codes.EXIT_OK

    assert _periods(on_rig.db_path, GOLD) == [("2026-09-23T12:00:00+00:00", None)]
    assert _state_row(on_rig.db_path, GOLD) == before


# -- F15: what OFF does to the signals already recorded ---------------------------------------


def _patient_policy(rig: Rig, seconds: int = 86400) -> None:
    """A policy.toml (the shipped one) whose signals may be up to `seconds`
    old when traded -- so the seeded signals, hours old, still count as
    waiting."""
    shipped = (REPO / "config" / "policy.toml").read_bytes().decode("utf-8")
    assert "max_signal_age_seconds   = 120" in shipped
    (rig.config_dir / "policy.toml").write_bytes(
        shipped.replace("max_signal_age_seconds   = 120", f"max_signal_age_seconds   = {seconds}")
        .encode("utf-8")
    )


def test_off_says_how_many_signals_are_in_play_and_what_off_does_to_them(
    on_rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final review F15: OFF also stops reading the edits, deletions and
    follow-ups of the channel's signals already recorded. Gold Room has 3
    signals queued; one of them is marked traded here."""
    _patient_policy(on_rig)
    conn = sqlite3.connect(on_rig.db_path)
    conn.execute(
        "UPDATE outbox SET state = 'DONE' WHERE outbox_id = (SELECT MIN(outbox_id) FROM outbox)"
    )
    conn.commit()
    conn.close()

    assert cli_main.main(on_rig.argv("off", "7")) == exit_codes.EXIT_OK

    out = capsys.readouterr().out
    assert "Channel 7 (Gold Room) is now OFF." in out
    assert "2 of its signals are still waiting to be traded, and 1 has already been traded." in out
    assert "edits, deletions and follow-ups (like 'close all') are no longer read" in out
    assert "cancelled the next time `.\\xaucopier watch` starts" in out
    # Final fix round 3 (R3-1): a copy on a channel switched on is traded.
    assert "the same call posted by a channel that is switched on is still traded" in out
    assert "keeps its stop loss and take profit" in out
    assert "never read, not even later" in out  # M12: posts made while off stay unread


def test_a_signal_one_account_has_taken_counts_as_traded_and_a_skipped_one_not_at_all(
    on_rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gold Room's first signal is taken (CLAIMED) by one account and still
    waiting for a second: it is traded, not waiting. Its second signal's
    entry was skipped: neither. Its third is waiting."""
    _patient_policy(on_rig)
    conn = sqlite3.connect(on_rig.db_path)
    first, second, _ = [
        row[0] for row in conn.execute("SELECT signal_id FROM signals ORDER BY signal_id")
    ]
    conn.execute("UPDATE outbox SET state = 'CLAIMED' WHERE signal_id = ?", (first,))
    conn.execute(
        "INSERT INTO outbox (account_id, kind, signal_id, state) "
        "VALUES ('second-account', 'ENTRY', ?, 'QUEUED')",
        (first,),
    )
    conn.execute("UPDATE outbox SET state = 'SKIPPED' WHERE signal_id = ?", (second,))
    conn.commit()
    conn.close()

    assert cli_main.main(on_rig.argv("off", "7")) == exit_codes.EXIT_OK

    out = capsys.readouterr().out
    assert "1 of its signals is still waiting to be traded, and 1 has already been traded." in out


def test_a_signal_older_than_the_age_limit_is_too_old_to_be_traded_not_waiting(
    on_rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fix round 2 of F15: a queued entry is only "still waiting to be
    traded" while its signal is younger than the policy's
    max_signal_age_seconds (120 s here: no policy.toml beside the rig's
    channels.toml); an older one can never be traded, and weeks of
    watch-only queue must not read as dozens of signals about to trade.
    Gold Room's signals were posted 4, 5 and 6 hours before NOW; it is
    switched off 30 seconds after the newest."""
    monkeypatch.setattr(channels_module, "_utcnow", lambda: NOW - timedelta(hours=4, seconds=-30))

    assert cli_main.main(on_rig.argv("off", "7")) == exit_codes.EXIT_OK

    out = capsys.readouterr().out
    assert (
        "1 of its signals is still waiting to be traded, 2 are too old to be traded (posted "
        "more than 120 seconds ago), and 0 have already been traded." in out
    )
    assert "cancelled the next time `.\\xaucopier watch` starts" in out


def test_a_channel_whose_waiting_signals_are_all_too_old_says_so(
    on_rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hours after its last signal: every entry is still queued, none can
    be traded -- said as such, not as "none of its signals"."""
    assert cli_main.main(on_rig.argv("off", "7")) == exit_codes.EXIT_OK

    out = capsys.readouterr().out
    assert (
        "0 of its signals are still waiting to be traded, 3 are too old to be traded (posted "
        "more than 120 seconds ago), and 0 have already been traded." in out
    )
    assert "None of its signals" not in out


def test_the_age_limit_is_the_one_in_policy_toml(
    on_rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """max_signal_age_seconds = 5400 (90 min): at 30 minutes past the
    newest signal, the newest two (30 and 90 minutes old) are waiting."""
    _patient_policy(on_rig, 5400)
    monkeypatch.setattr(channels_module, "_utcnow", lambda: NOW - timedelta(hours=4, seconds=-1800))

    assert cli_main.main(on_rig.argv("off", "7")) == exit_codes.EXIT_OK

    assert (
        "2 of its signals are still waiting to be traded, 1 is too old to be traded (posted "
        "more than 5400 seconds ago), and 0 have already been traded."
    ) in capsys.readouterr().out


@pytest.mark.parametrize(
    "policy",
    [
        "max_signal_age_seconds = 0\n",
        "max_signal_age_seconds = 'soon'\n",
        "max_signal_age_seconds = true\n",
        "not toml [[\n",
    ],
    ids=["zero", "not a number", "true", "not readable"],
)
def test_a_policy_toml_it_cannot_use_falls_back_to_120_seconds(
    on_rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], policy: str
) -> None:
    (on_rig.config_dir / "policy.toml").write_bytes(policy.encode("utf-8"))
    monkeypatch.setattr(channels_module, "_utcnow", lambda: NOW - timedelta(hours=4, seconds=-30))

    assert cli_main.main(on_rig.argv("off", "7")) == exit_codes.EXIT_OK

    assert "2 are too old to be traded (posted more than 120 seconds ago)" in (
        capsys.readouterr().out
    )


def test_a_signal_whose_post_time_cannot_be_read_counts_as_waiting(
    on_rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not known to be too old, so the user is told it may still trade."""
    conn = sqlite3.connect(on_rig.db_path)
    conn.execute(
        "UPDATE messages SET posted_at_utc = 'not a time' WHERE channel_id = ? AND message_id = 10",
        (GOLD,),
    )
    conn.commit()
    conn.close()

    assert cli_main.main(on_rig.argv("off", "7")) == exit_codes.EXIT_OK

    assert (
        "1 of its signals is still waiting to be traded, 2 are too old to be traded"
        in capsys.readouterr().out
    )


def test_a_channel_whose_signals_were_all_traded_says_so(
    on_rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Traded, none waiting: never "None of its signals is waiting to be
    traded or already traded" (a mutant of that test survived round 1)."""
    conn = sqlite3.connect(on_rig.db_path)
    conn.execute("UPDATE outbox SET state = 'DONE' WHERE kind = 'ENTRY'")
    conn.commit()
    conn.close()

    assert cli_main.main(on_rig.argv("off", "7")) == exit_codes.EXIT_OK

    out = capsys.readouterr().out
    assert "0 of its signals are still waiting to be traded, and 3 have already been traded." in out
    assert "None of its signals" not in out
    assert "keeps its stop loss and take profit" in out


def test_off_of_a_channel_with_nothing_in_play_says_so(
    on_rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    on_rig.channels_path.write_bytes(
        SEEDED_TOML.replace("enabled    = false    # off for now", "enabled    = true").encode("utf-8")
    )

    assert cli_main.main(on_rig.argv("off", "3")) == exit_codes.EXIT_OK

    out = capsys.readouterr().out
    assert "None of its signals is waiting to be traded or already traded." in out


def test_off_with_no_database_still_switches_off(
    on_rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = on_rig.db_path.with_name("not-yet.db")
    argv = [a if a != str(on_rig.db_path) else str(missing) for a in on_rig.argv("off", "7")]

    assert cli_main.main(argv) == exit_codes.EXIT_OK

    assert not missing.exists()
    assert _switches(on_rig.channels_path)[7] is False
    assert "None of its signals is waiting" in capsys.readouterr().out


# -- G2L-3: a channel removed and added back is read again from now --------------------------


def test_add_of_a_channel_read_before_asks_the_next_watch_to_start_reading_it_from_now(
    add_rig: AddRig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Group-2 review leftover G2L-3: a channel removed from channels.toml
    and added back kept its old pointer, so everything posted while it was
    gone was read as new -- `channels add` now does what `channels on` does."""
    monkeypatch.setattr(channels_module, "_utcnow", lambda: SWITCHED_AT)
    _set_state(add_rig.rig.db_path, SILVER_ID, 40, "2026-09-01T00:00:00+00:00")

    assert cli_main.main(add_rig.argv("--link", "https://t.me/silver", "--yes")) == 0

    assert load_channels(add_rig.rig.channels_path)[-1].channel_id == SILVER_ID
    assert _state_row(add_rig.rig.db_path, SILVER_ID) == (
        40, "2026-09-01T00:00:00+00:00", "2026-09-23T12:00:00+00:00"
    )
    assert (
        "It was read before, so the next watch reads it from now on: what it posted since it "
        "was last read is skipped."
    ) in capsys.readouterr().out


def test_add_of_a_channel_whose_first_read_never_finished_reads_it_from_now(
    add_rig: AddRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`channels add` holds the session, so no watch is seeding the channel
    at its first cutoff: that cutoff is moved to now (the reviewer's open
    (b)), and nothing posted while it was gone is read."""
    monkeypatch.setattr(channels_module, "_utcnow", lambda: SWITCHED_AT)
    conn = sqlite3.connect(add_rig.rig.db_path)
    conn.execute(
        "INSERT INTO channel_state (channel_id, seed_cutoff_utc) VALUES (?, ?)",
        (SILVER_ID, "2026-09-20T08:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    assert cli_main.main(add_rig.argv("--link", "https://t.me/silver", "--yes")) == 0

    assert _state_row(add_rig.rig.db_path, SILVER_ID) == (0, None, "2026-09-23T12:00:00+00:00")


def test_add_of_a_channel_never_read_writes_no_cutoff(
    add_rig: AddRig, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main.main(add_rig.argv("--link", "https://t.me/silver", "--yes")) == 0

    assert _state_row(add_rig.rig.db_path, SILVER_ID) is None
    assert "It was read before" not in capsys.readouterr().out


def test_add_while_the_database_is_held_adds_nothing(
    add_rig: AddRig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_state(add_rig.rig.db_path, SILVER_ID, 40, "2026-09-01T00:00:00+00:00")
    monkeypatch.setattr(db, "_BUSY_TIMEOUT_MS", 50)
    before = add_rig.rig.channels_path.read_bytes()
    holder = sqlite3.connect(add_rig.rig.db_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        code = cli_main.main(add_rig.argv("--link", "https://t.me/silver", "--yes"))
    finally:
        holder.rollback()
        holder.close()

    assert code == exit_codes.EXIT_FAILED
    assert add_rig.rig.channels_path.read_bytes() == before
    err = capsys.readouterr().err
    assert "The database is busy" in err and "Nothing was added." in err


# -- channels.toml being replaced by another command while this one reads it (FF9) -------


class _Replacing:
    """`Path.open`, refusing channels.toml `times` times (None: always)
    with the PermissionError Windows gives a reader in the moment another
    xaucopier command replaces the file."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, times: int | None) -> None:
        self.times = times
        self.refused = 0
        real_open = Path.open

        def opened(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path.name == "channels.toml" and (self.times is None or self.refused < self.times):
                self.refused += 1
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", opened)


@pytest.mark.parametrize("command", [(), ("off", "7"), ("on", "3")], ids=["list", "off", "on"])
def test_a_read_that_meets_another_command_replacing_the_file_waits_for_it(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command
) -> None:
    """Final fix FF9: two `channels` commands at once. The one reading
    channels.toml in the moment the other replaces it got a PermissionError
    and said the setup was broken (exit 5) although the file was fine. A
    refusal that passes is waited out."""
    replacing = _Replacing(monkeypatch, times=3)

    assert cli_main.main(rig.argv(*command)) == exit_codes.EXIT_OK
    assert replacing.refused == 3
    assert "cannot be used" not in capsys.readouterr().err


@pytest.mark.parametrize("command", [(), ("off", "7"), ("on", "3")], ids=["list", "off", "on"])
def test_a_file_that_stays_locked_asks_to_run_the_command_again_not_to_fix_the_setup(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command
) -> None:
    """Refused for longer than about a second: exit 1, nothing changed,
    and the advice is to run the command again -- not a setup problem."""
    import xaucopier.config.loader as loader_module

    monkeypatch.setattr(loader_module, "REPLACE_WAIT_SECONDS", 0.2)
    before = rig.channels_path.read_bytes()
    _Replacing(monkeypatch, times=None)

    assert cli_main.main(rig.argv(*command)) == exit_codes.EXIT_FAILED

    err = capsys.readouterr().err
    assert "Run the command again" in err and "Nothing was changed" in err
    assert "If this keeps happening, check that the file can be opened." in err
    for wrong in ("exit code 5", "cannot be used", "PermissionError", "refuse to start"):
        assert wrong not in err
    monkeypatch.undo()
    assert rig.channels_path.read_bytes() == before


def test_off_while_watch_runs_says_watch_keeps_reading_it_until_restarted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final fix FF24: with watch running, `channels off` said what the
    channel posts from now on "is never read, not even later" -- while the
    running watch, which read the list at its start, keeps reading and
    recording it. It now says so."""
    _set_state(rig.db_path, GOLD, 13, "2026-09-01T00:00:00+00:00")
    rig.watch_running = True

    assert cli_main.main(rig.argv("off", "7")) == exit_codes.EXIT_OK

    out = capsys.readouterr().out
    assert "never read" not in out
    assert (
        "watch is running and keeps reading this channel until you restart it" in out
    )
    assert "watch is running now" in out  # how to restart it
