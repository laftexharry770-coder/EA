"""Tests for `xaucopier export-fixtures`.

The files it writes must be ones the Plan 1 replay harness actually
parses: each test here loads the output folder the way the harness does
and runs `replay()` over it, and a real signal exported from the store
must replay to the same decision as the hand-checked fixture.

It must never overwrite anything -- above all the hand-checked
`mmh_buy_4tp.json`, whose message is in the seeded store on purpose --
and never write a second fixture for a message one already covers.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import xaucopier.cli.main as cli_main
from xaucopier.cli import exit_codes
from xaucopier.cli.fixtures import fixture_name
from xaucopier.config.accounts import load_accounts
from xaucopier.config.channels import ChannelCfg
from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.signal import Signal
from xaucopier.replay.harness import replay
from xaucopier.store import db, messages_repo

REPO = Path(__file__).resolve().parents[1]
HAND_CHECKED = REPO / "tests" / "fixtures" / "messages" / "mmh_buy_4tp.json"
FIXTURE = json.loads(HAND_CHECKED.read_text(encoding="utf-8"))
A = FIXTURE["channel_id"]
B = -1002222222222
POSTED = datetime.fromisoformat(FIXTURE["posted_at_utc"])
NOW = POSTED + timedelta(hours=3)
ACCOUNTS = load_accounts(REPO / "config" / "accounts.toml")

# The harness's own replay settings (tests/test_replay.py).
SPEC = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)
ACCT = AccountSpec("ic_demo", "XAUUSD", fixed_lot=0.01, magic_base=77001)


def _snap_for(signal: Signal) -> MarketSnapshot:
    spread = 0.10
    mid = (signal.zone.lo + signal.zone.hi) / 2.0
    return MarketSnapshot(
        bid=mid - spread / 2, ask=mid + spread / 2, tick_time_utc=signal.posted_at_utc,
        spread_usd=spread, median_spread_usd=spread,
        now_utc=signal.posted_at_utc + timedelta(seconds=5),
    )


def _channel(channel_id: int) -> ChannelCfg:
    return ChannelCfg(channel_id=channel_id, name=str(channel_id), parser="mmh", ordinal=1, enabled=True)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    db_path = tmp_path / "state.db"
    conn = db.connect(db_path, role="writer")
    try:
        for channel_id, message_id, text, posted, reply_to in [
            (A, FIXTURE["message_id"], FIXTURE["text"], POSTED, None),  # = the hand-checked one
            (A, 4002, FIXTURE["text"], POSTED + timedelta(hours=1), None),
            (A, 4003, "Good morning traders", POSTED, None),
            (A, 4004, "SL to BE", POSTED + timedelta(hours=2), 4002),
            (B, 7, "XAUUSD | BUY SIGNAL\nSL: 4470", POSTED, None),
        ]:
            messages_repo.ingest(
                conn, channel_cfg=_channel(channel_id), message_id=message_id, text=text,
                posted_at_utc=posted, edit_date_utc=None, reply_to_message_id=reply_to,
                source="live", now_utc=NOW, accounts=ACCOUNTS,
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


@pytest.fixture
def out(tmp_path: Path) -> Path:
    folder = tmp_path / "fixtures" / "messages"
    folder.mkdir(parents=True)
    shutil.copyfile(HAND_CHECKED, folder / HAND_CHECKED.name)
    return folder


def _digests(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())}


def _harness_fixtures(folder: Path) -> list[dict]:
    # Exactly how the harness and tests/test_replay.py load them.
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]


def _export(store: Path, out: Path) -> int:
    return cli_main.main(["export-fixtures", "--db", str(store), "--out", str(out)])


def test_export_writes_harness_fixtures_that_replay_and_skips_the_hand_checked_one(
    store: Path, out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hand_checked_before = (out / HAND_CHECKED.name).read_bytes()
    store_before = hashlib.sha256(store.read_bytes()).hexdigest()

    assert _export(store, out) == exit_codes.EXIT_OK

    assert (out / HAND_CHECKED.name).read_bytes() == hand_checked_before
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [
            HAND_CHECKED.name,
            fixture_name(A, 4002),
            fixture_name(A, 4003),
            fixture_name(A, 4004),
            fixture_name(B, 7),
        ]
    )
    exported = json.loads((out / fixture_name(A, 4002)).read_text(encoding="utf-8"))
    # The harness's schema exactly -- and no `expect`: only a human adds that.
    assert exported == {
        "channel_id": A,
        "message_id": 4002,
        "posted_at_utc": (POSTED + timedelta(hours=1)).isoformat(),
        "text": FIXTURE["text"],
    }

    fixtures = _harness_fixtures(out)
    decisions = {d.message_id: d for d in replay(fixtures, ACCT, SPEC, Policy(), _snap_for)}
    assert sorted(decisions) == [7, 4001, 4002, 4003, 4004]
    # A real signal exported from the store replays exactly like the
    # hand-checked copy of the same text.
    assert decisions[4002].accepted and decisions[4002].leg_count == 4
    assert decisions[4002].prices == decisions[4001].prices
    assert not decisions[4003].accepted and not decisions[4004].accepted and not decisions[7].accepted

    assert hashlib.sha256(store.read_bytes()).hexdigest() == store_before
    out_text = capsys.readouterr().out
    assert "Wrote 4 new fixture file(s)" in out_text
    assert "Skipped 1 message(s) an existing fixture already covers" in out_text
    assert "--write-golden" in out_text


def test_a_second_export_writes_nothing_and_changes_nothing(
    store: Path, out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _export(store, out) == 0
    after_first = _digests(out)

    assert _export(store, out) == 0

    assert _digests(out) == after_first
    assert "Wrote 0 new fixture file(s)" in capsys.readouterr().out


def test_a_file_already_at_the_target_name_is_never_overwritten(
    store: Path, out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Even one that cannot be read -- a developer's half-finished edit --
    is left exactly as it is."""
    taken = out / fixture_name(B, 7)
    taken.write_bytes(b'{"channel_id": -1002222222222, "message_id": 7, "text": "hand edit in prog')
    before = taken.read_bytes()

    assert _export(store, out) == 0

    assert taken.read_bytes() == before
    captured = capsys.readouterr()
    assert "whose file name was already taken" in captured.out
    assert f"could not read {taken}" in captured.err
    assert not [p for p in out.iterdir() if p.name.endswith(".tmp")]


def test_without_a_database_it_says_so_and_creates_nothing(
    tmp_path: Path, out: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "none.db"
    before = _digests(out)

    assert _export(missing, out) == exit_codes.EXIT_FAILED

    assert not missing.exists()
    assert _digests(out) == before
    assert "No recordings yet" in capsys.readouterr().err
