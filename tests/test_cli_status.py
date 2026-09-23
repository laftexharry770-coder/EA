"""Tests for `xaucopier status` (and `store/report.py` behind it).

The database is seeded through the real store code, not hand-written
rows, so the states `status` reports are the states the system really
produces: signals ingested by `messages_repo.ingest`; one VOIDED and one
flagged-live by `edits_repo.apply_deletion`; one INVALID by
`edits_repo.apply_edit`; the alerts and SKIPPED outbox rows those calls
themselves raise; metrics and heartbeats through their own modules.

Two properties are pinned hardest: the counts are exact, and `status`
writes nothing -- its own connection's `total_changes`, the database's
`data_version` seen from a second connection, and the file bytes are all
unchanged.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import xaucopier.cli.main as cli_main
import xaucopier.cli.status as status_module
from xaucopier.cli import exit_codes
from xaucopier.config.accounts import load_accounts
from xaucopier.config.channels import ChannelCfg
from xaucopier.store import alerts, db, edits_repo, heartbeats, messages_repo, metrics, report

REPO = Path(__file__).resolve().parents[1]
SIGNAL_TEXT: str = json.loads(
    (REPO / "tests" / "fixtures" / "messages" / "mmh_buy_4tp.json").read_text(encoding="utf-8")
)["text"]
FLIPPED_TEXT = SIGNAL_TEXT.replace("BUY", "SELL")
CHATTER = "Good morning traders"
BROKEN_SIGNAL = "XAUUSD | BUY SIGNAL\nSL: 4470"
FOLLOWUP = "SL to BE"

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
YESTERDAY = NOW - timedelta(days=1)
A = -1001111111111
B = -1002222222222
CHANNEL_A = ChannelCfg(channel_id=A, name="Test Channel", parser="mmh", ordinal=1, enabled=True)
CHANNEL_B = ChannelCfg(channel_id=B, name="Other", parser="mmh", ordinal=2, enabled=True)
ACCOUNTS = load_accounts(REPO / "config" / "accounts.toml")  # read, never written
assert [a.account_id for a in ACCOUNTS if a.enabled] == ["ic_demo"]

CHANNELS_TOML = f"""
[[channels]]
channel_id = {A}
name       = "Test Channel"
parser     = "mmh"
ordinal    = 1
enabled    = true

[[channels]]
channel_id = 0
name       = "Not Yet Resolved"
parser     = "mmh"
ordinal    = 2
enabled    = false
"""


def _ingest(conn, channel, message_id, text, *, source, posted, reply_to=None):
    return messages_repo.ingest(
        conn,
        channel_cfg=channel,
        message_id=message_id,
        text=text,
        posted_at_utc=posted,
        edit_date_utc=None,
        reply_to_message_id=reply_to,
        source=source,
        now_utc=NOW,
        accounts=ACCOUNTS,
    )


@dataclass(frozen=True)
class Seeded:
    db_path: Path
    config_dir: Path
    secrets_path: Path


@pytest.fixture
def seeded(tmp_path: Path) -> Seeded:
    db_path = tmp_path / "state.db"
    conn = db.connect(db_path, role="writer")
    try:
        t0 = NOW - timedelta(hours=6)
        # Four signals an hour apart (outside the 15-minute relay window).
        for offset, (message_id, source) in enumerate(
            [(10, "live"), (11, "poll"), (12, "backfill"), (13, "live")]
        ):
            result = _ingest(
                conn, CHANNEL_A, message_id, SIGNAL_TEXT, source=source,
                posted=t0 + timedelta(hours=offset),
            )
            assert result.outcome == "new" and result.signal_id is not None
        _ingest(conn, CHANNEL_A, 20, CHATTER, source="live", posted=t0)
        _ingest(conn, CHANNEL_A, 21, CHATTER, source="poll", posted=t0)
        _ingest(conn, CHANNEL_A, 22, BROKEN_SIGNAL, source="live", posted=t0)
        _ingest(conn, CHANNEL_A, 30, FOLLOWUP, source="live", posted=t0, reply_to=10)
        _ingest(conn, CHANNEL_B, 5, CHATTER, source="live", posted=t0)
        conn.commit()

        # Signal 2 (message 11): deleted before any trade -> VOIDED, WARNING.
        edits_repo.apply_deletion(
            conn, channel_id=A, message_id=11, now_utc=NOW, detected_by="event"
        )
        # Signal 3 (message 12): re-aimed BUY -> SELL by an edit the event
        # stream dropped -> INVALID, CRITICAL, edit_missed_by_eventstream.
        edits_repo.apply_edit(
            conn,
            channel_cfg=CHANNEL_A,
            message_id=12,
            text=FLIPPED_TEXT,
            posted_at_utc=t0 + timedelta(hours=2),
            edit_date_utc=NOW - timedelta(minutes=30),
            reply_to_message_id=None,
            now_utc=NOW,
            accounts=ACCOUNTS,
            detected_by="window",
        )
        # Signal 4 (message 13): executed (its ENTRY DONE, as Plan 3 would
        # leave it), then deleted -> stays NEW, flagged, CRITICAL.
        conn.execute("UPDATE outbox SET state = 'DONE' WHERE kind = 'ENTRY' AND signal_id = 4")
        edits_repo.apply_deletion(
            conn, channel_id=A, message_id=13, now_utc=NOW, detected_by="window"
        )
        # An acknowledged alert must not be shown.
        acked = alerts.record(conn, now_utc=NOW, level="CRITICAL", code="OLD_NEWS", detail="done")
        alerts.ack(conn, alert_id=acked)

        today, yesterday = metrics.day_key_for(NOW), metrics.day_key_for(YESTERDAY)
        metrics.bump(conn, day_key=today, name="missed_by_eventstream", value=2)
        metrics.bump(conn, day_key=yesterday, name="missed_by_eventstream", value=3)
        metrics.bump(conn, day_key=yesterday, name="history_skipped")
        metrics.bump(conn, day_key=today, name="some_new_counter", value=7)

        heartbeats.beat(conn, name=heartbeats.WATCH_STARTED, now_utc=NOW - timedelta(hours=7))
        heartbeats.beat(
            conn, name=heartbeats.POLL_ROUND, now_utc=NOW - timedelta(seconds=3),
            detail="1 of 1 channel(s) read",
        )
        heartbeats.beat(conn, name=heartbeats.channel_read_name(A), now_utc=NOW - timedelta(seconds=3))
        conn.commit()
    finally:
        conn.close()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "channels.toml").write_text(CHANNELS_TOML, encoding="utf-8")
    secrets_path = tmp_path / "session.dpapi"
    secrets_path.write_bytes(b"ciphertext stand-in")
    return Seeded(db_path=db_path, config_dir=config_dir, secrets_path=secrets_path)


def _argv(seeded: Seeded) -> list[str]:
    return [
        "status",
        "--db", str(seeded.db_path),
        "--config-dir", str(seeded.config_dir),
        "--secrets", str(seeded.secrets_path),
    ]


# -- the counts --------------------------------------------------------------------------


def test_collect_reports_exact_counts_from_real_store_states(seeded: Seeded) -> None:
    conn = db.connect_readonly(seeded.db_path)
    try:
        before = conn.total_changes
        rep = report.collect(conn, today=metrics.day_key_for(NOW))
        assert conn.total_changes == before == 0
    finally:
        conn.close()

    assert rep.messages_by_kind == (("followup", 1), ("parse_failed", 1), ("signal", 4), ("skip", 3))
    assert rep.messages_total == 9
    assert rep.messages_by_source == (("backfill", 1), ("live", 6), ("poll", 2))
    assert rep.signals_by_state == (("INVALID", 1), ("NEW", 2), ("VOIDED", 1))
    assert rep.withdrawn_signals == 2  # the voided one and the deleted live one
    assert rep.followups == 1
    # ENTRY x4 + FOLLOWUP x1, one enabled account each: signal 1's ENTRY and
    # the follow-up still QUEUED, signal 4's DONE, signals 2 and 3 SKIPPED.
    assert rep.outbox_by_state == (("DONE", 1), ("QUEUED", 2), ("SKIPPED", 2))

    assert [(a.level, a.code) for a in rep.alerts] == [
        ("CRITICAL", edits_repo.SIGNAL_INVALIDATED_BY_EDIT),
        ("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED),
        ("WARNING", edits_repo.SIGNAL_VOIDED_BY_DELETION),
    ]
    assert [(d.signal_id, d.channel_id, d.message_id, d.detected_by) for d in rep.deleted_live_signals] == [
        (4, A, 13, "window")
    ]
    assert [(s.signal_id, s.account_id, s.kind) for s in rep.skipped_outbox] == [
        (2, "ic_demo", "ENTRY"),
        (3, "ic_demo", "ENTRY"),
    ]
    assert "withdrawn" in rep.skipped_outbox[0].reason
    assert "invalidated" in rep.skipped_outbox[1].reason

    assert rep.metric("missed_by_eventstream") == report.MetricTotal("missed_by_eventstream", 2, 5)
    assert rep.metric("history_skipped") == report.MetricTotal("history_skipped", 0, 1)
    assert rep.metric("edit_missed_by_eventstream").all_time == 1
    assert rep.metric("delete_missed_by_eventstream").all_time == 1  # the window one only
    assert rep.metric("outbox_lease_reclaimed") == report.MetricTotal("outbox_lease_reclaimed", 0, 0)

    by_channel = {c.channel_id: c for c in rep.channels}
    assert by_channel[A].messages == 8 and by_channel[A].last_message_id == 30
    assert by_channel[A].last_kind == "followup" and by_channel[A].last_source == "live"
    assert by_channel[B].messages == 1
    # One unreadable signal (22) since the last good one; chatter and
    # follow-ups neither count towards the streak nor reset it.
    assert by_channel[A].consecutive_parse_failures == 1


# -- the report, as the user reads it -----------------------------------------------------------


def test_status_prints_alerts_first_critical_first_and_every_required_number(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(status_module, "_utcnow", lambda: NOW)

    assert cli_main.main(_argv(seeded)) == exit_codes.EXIT_OK
    out = capsys.readouterr().out

    lines = out.splitlines()
    alert_header = next(i for i, line in enumerate(lines) if line.startswith("ALERTS:"))
    assert lines[alert_header] == "ALERTS: 3 unacknowledged (2 CRITICAL, 1 WARNING)"
    # Alerts are the first section after the title, before anything else.
    assert alert_header < next(i for i, line in enumerate(lines) if line == "WATCH")
    order = [line.split("]")[0] for line in lines if line.startswith("  [")]
    assert order == ["  [CRITICAL", "  [CRITICAL", "  [WARNING"]
    assert "OLD_NEWS" not in out

    for expected in (
        "MESSAGES RECORDED: 9",
        "signals 4 | follow-ups 1 | chatter (not a signal) 3 | looked like a signal but could not be read 1",
        "arrived via: live feed 6 | 5-second check 2 | catch-up at start 1",
        "valid (NEW) 2 | INVALID -- re-aimed or broken by an edit 1 | "
        "VOIDED -- deleted or withdrawn before any trade 1",
        "LIVE SIGNALS WHOSE MESSAGE WAS DELETED: 1",
        f"signal 4 (channel {A}, message 13)",
        "QUEUED 2 | CLAIMED 0 | DONE 1 | SKIPPED 2",
        "some_new_counter: 7 / 7",
        "RUNNING: started",
        "last check of the channel 3 s ago (1 of 1 channel(s) read)",
        "Telegram session file: present",
        f"Test Channel (id {A}) -- switched on",
        "Not Yet Resolved -- id not looked up yet (run resolve-channels)",
        f"id {B} (not in channels.toml)",
    ):
        assert expected in out, expected

    def row(name: str) -> list[str]:
        (line,) = [line for line in lines if f"({name})" in line]
        return line.split()

    assert row("missed_by_eventstream")[-2:] == ["2", "5"]
    assert row("edit_missed_by_eventstream")[-2:] == ["1", "1"]
    assert row("delete_missed_by_eventstream")[-2:] == ["1", "1"]
    assert row("history_skipped")[-6:] == ["0", "1", "<--", "look", "into", "this"]
    assert row("ingest_new_no_accounts")[-2:] == ["0", "0"]
    assert row("outbox_lease_reclaimed")[-2:] == ["0", "0"]


class _Recorder:
    """Stands in for the connection `status` opens, recording its
    `total_changes` when `status` closes it."""

    def __init__(self, conn: sqlite3.Connection, seen: list[int]) -> None:
        self._conn = conn
        self._seen = seen

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def close(self) -> None:
        self._seen.append(self._conn.total_changes)
        self._conn.close()


def _files(db_path: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in db_path.parent.glob(db_path.name + "*")
        if not p.name.endswith("-shm")  # SQLite's shared-memory index, not data
    }


def test_status_writes_nothing(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[int] = []
    real = db.connect_readonly
    monkeypatch.setattr(db, "connect_readonly", lambda path: _Recorder(real(path), seen))
    observer = real(seeded.db_path)  # not recorded: only status's own connection is
    try:
        (version_before,) = observer.execute("PRAGMA data_version").fetchone()
        files_before = _files(seeded.db_path)

        assert cli_main.main(_argv(seeded)) == 0

        (version_after,) = observer.execute("PRAGMA data_version").fetchone()
    finally:
        observer.close()

    assert seen == [0], "status's own connection changed rows"
    assert version_after == version_before, "another connection committed to the database"
    after = _files(seeded.db_path)
    assert after[seeded.db_path.name] == files_before[seeded.db_path.name]
    wal = seeded.db_path.name + "-wal"
    assert after.get(wal) in (files_before.get(wal), hashlib.sha256(b"").hexdigest())
    capsys.readouterr()


def test_status_without_a_database_says_so_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "state.db"

    code = cli_main.main(["status", "--db", str(db_path), "--config-dir", str(tmp_path)])

    assert code == exit_codes.EXIT_FAILED
    assert not db_path.exists()
    assert "No recordings yet" in capsys.readouterr().err


def test_status_never_migrates_an_older_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "state.db"
    raw = sqlite3.connect(db_path)
    raw.execute("PRAGMA user_version = 2")
    raw.commit()
    raw.close()
    before = db_path.read_bytes()

    assert cli_main.main(["status", "--db", str(db_path), "--config-dir", str(tmp_path)]) == 1
    assert db_path.read_bytes() == before
    assert "Run `xaucopier watch` once" in capsys.readouterr().err


# -- is watch alive? ------------------------------------------------------------------------------


def _report(*beats: heartbeats.Heartbeat) -> report.StatusReport:
    return report.StatusReport(
        messages_by_kind=(), messages_by_source=(), signals_by_state=(), withdrawn_signals=0,
        followups=0, outbox_by_state=(), metrics=(), alerts=(), deleted_live_signals=(),
        skipped_outbox=(), channels=(), heartbeats=tuple(beats),
    )


def _render(rep: report.StatusReport, tmp_path: Path) -> str:
    return status_module.render(
        rep, now=NOW, db_path=tmp_path / "state.db", secrets_path=tmp_path / "missing.dpapi",
        configured=(CHANNEL_A,), config_problem=None,
    )


def _beat(name: str, ago: timedelta, detail: str | None = None) -> heartbeats.Heartbeat:
    return heartbeats.Heartbeat(name=name, at_utc=NOW - ago, detail=detail)


@pytest.mark.parametrize(
    ("beats", "expected"),
    [
        ((), "watch has never run against this database."),
        (
            (_beat(heartbeats.WATCH_STARTED, timedelta(hours=1)),
             _beat(heartbeats.POLL_ROUND, timedelta(seconds=4), "1 of 1 channel(s) read")),
            "RUNNING: started",
        ),
        (
            (_beat(heartbeats.WATCH_STARTED, timedelta(hours=1)),
             _beat(heartbeats.POLL_ROUND, timedelta(minutes=10))),
            "NOT HEALTHY: the last check of the channel finished 10 min ago",
        ),
        (
            (_beat(heartbeats.WATCH_STARTED, timedelta(seconds=20)),),
            "STARTING:",
        ),
        (
            (_beat(heartbeats.WATCH_STARTED, timedelta(hours=1)),),
            "NOT HEALTHY: started",
        ),
        (
            (_beat(heartbeats.WATCH_STARTED, timedelta(hours=2)),
             _beat(heartbeats.WATCH_STOPPED, timedelta(hours=1), "3")),
            "Telegram has logged this session out. Run `xaucopier setup-login`",
        ),
        (
            (_beat(heartbeats.WATCH_STARTED, timedelta(hours=2)),
             _beat(heartbeats.WATCH_STOPPED, timedelta(hours=1), "130")),
            "NOT RUNNING: stopped",
        ),
    ],
)
def test_the_watch_section_tells_running_from_stuck_from_stopped(
    tmp_path: Path, beats: tuple, expected: str
) -> None:
    out = _render(_report(*beats), tmp_path)

    assert expected in out
    assert "Telegram session file: MISSING" in out


def test_a_channel_not_read_lately_is_flagged_while_watch_runs(tmp_path: Path) -> None:
    out = _render(
        _report(
            _beat(heartbeats.WATCH_STARTED, timedelta(hours=1)),
            _beat(heartbeats.POLL_ROUND, timedelta(seconds=4), "0 of 1 channel(s) read"),
            _beat(heartbeats.channel_read_name(A), timedelta(minutes=30)),
        ),
        tmp_path,
    )

    assert "last read by the 5-second check: 30 min ago  <-- NOT BEING READ" in out
