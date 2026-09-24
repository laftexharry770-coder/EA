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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import xaucopier.cli.channels as channels_module
import xaucopier.cli.main as cli_main
import xaucopier.cli.resolve as resolve_module
from xaucopier.cli import exit_codes
from xaucopier.config.accounts import load_accounts
from xaucopier.config.channels import ChannelCfg, load_channels
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
    twin = EXTRA.split("[[channels]]")[1].replace("ordinal    = 7\nenabled    = true",
                                                 "ordinal    = 9\nenabled    = false")
    rig.channels_path.write_bytes((SEEDED_TOML + "\n[[channels]]" + twin).encode("utf-8"))
    before = rig.channels_path.read_bytes()

    assert cli_main.main(rig.argv("on", "9")) == exit_codes.EXIT_FAILED

    assert rig.channels_path.read_bytes() == before
    assert "same Telegram channel" in capsys.readouterr().err


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
        monkeypatch.setattr(resolve_module, "load_channels", _boom)
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

    assert code == exit_codes.EXIT_CONFIG_ERROR
    if where == "changed meanwhile":
        assert rig.channels_path.read_bytes() == before + b"# edited by hand\n"
    else:
        assert rig.channels_path.read_bytes() == before
    assert _no_temp_files(rig)
    err = capsys.readouterr().err
    assert "The file was not changed" in err
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
