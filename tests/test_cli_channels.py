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
    for name in ("build_client", "load_session", "acquire_single_instance_lock"):
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
    monkeypatch.setattr(channels_module, "build_client", lambda blob: client)
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
    assert "The only parser built so far, mmh, understands only the MARKET MASTERY HUB" in out
    assert "nothing will be traded from it until a parser is built" in out
    assert "5-10 of its real messages" in out
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
    assert len([line for line in err.splitlines() if line.strip()]) == 1  # one plain sentence
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


@pytest.mark.parametrize("where", ["get_dialogs", "get_entity", "is_user_authorized"])
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
    assert len([line for line in err.splitlines() if line.strip()]) == 1
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
