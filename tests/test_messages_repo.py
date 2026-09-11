"""Tests for messages_repo.ingest(): the three-outcome dedup rule and
cross-channel relay dedup (spec G7).

Drives the real router -- and through it the real `mmh` parser -- end to
end, using the fixture message at tests/fixtures/messages/mmh_buy_4tp.json
(a genuine XAUUSD BUY signal). These tests are the specification for
ingest(); see the brief at
.superpowers/sdd/2026-09-10-xaucopier-ingest/task-5-brief.md and the three
controller rulings on top of it (no fan_out() here, now_utc is an explicit
parameter, Signal.source stays informational).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from xaucopier.config.channels import ChannelCfg
from xaucopier.domain.signal import FollowUpOp
from xaucopier.store import db
from xaucopier.store.messages_repo import ingest

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "messages" / "mmh_buy_4tp.json"
_FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
SIGNAL_TEXT = _FIXTURE["text"]
SIGNAL_MESSAGE_ID = _FIXTURE["message_id"]
POSTED = datetime.fromisoformat(_FIXTURE["posted_at_utc"])

# The fixture's SL appears exactly once ("SL: 4470"); editing it in place
# is how the SL-edit test produces a message that is the *same trade
# family* but a genuinely different content_hash.
assert SIGNAL_TEXT.count("4470") == 1
EDITED_SL_TEXT = SIGNAL_TEXT.replace("4470", "4460")

CHANNEL_A = ChannelCfg(
    channel_id=_FIXTURE["channel_id"],
    name="Channel A (source)",
    parser="mmh",
    ordinal=1,
    enabled=True,
)
CHANNEL_B = ChannelCfg(
    channel_id=-1002222222222,
    name="Channel B (relay)",
    parser="mmh",
    ordinal=2,
    enabled=True,
)

BROKEN_TEXT = "XAUUSD | BUY SIGNAL\n4480-4485\nTP1: 4490"  # no SL -> parse_failed
CHATTER_TEXT = "Good morning traders"  # -> skip

NOW = datetime(2026, 9, 5, 11, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "store.db", role="writer")
    try:
        yield connection
    finally:
        connection.close()


def _ingest(
    conn,
    *,
    channel_cfg: ChannelCfg = CHANNEL_A,
    message_id: int = SIGNAL_MESSAGE_ID,
    text: str = SIGNAL_TEXT,
    posted_at_utc: datetime = POSTED,
    edit_date_utc: datetime | None = None,
    reply_to_message_id: int | None = None,
    source: str = "live",
    now_utc: datetime = NOW,
    accounts: tuple = (),
    relay_window_seconds: int = 900,
):
    # accounts=() explicitly: these tests are the spec for ingest()'s
    # dedup/routing behaviour and deliberately don't care about the
    # outbox (that's tests/test_outbox.py's job) -- but ingest() no
    # longer defaults accounts on its own (Task 6 review round 1: a
    # silent default there is exactly the silently-dropped-signal
    # failure mode the lease design exists to prevent), so every call
    # site, including this one, must say so on purpose.
    return ingest(
        conn,
        channel_cfg=channel_cfg,
        message_id=message_id,
        text=text,
        posted_at_utc=posted_at_utc,
        edit_date_utc=edit_date_utc,
        reply_to_message_id=reply_to_message_id,
        source=source,
        now_utc=now_utc,
        accounts=accounts,
        relay_window_seconds=relay_window_seconds,
    )


def _count(conn, table: str) -> int:
    (n,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return n


# -- three outcomes for a seen message ---------------------------------------


def test_same_message_twice_is_new_then_duplicate(conn):
    first = _ingest(conn)
    second = _ingest(conn)

    assert first.outcome == "new"
    assert first.kind == "signal"
    assert second.outcome == "duplicate"
    assert _count(conn, "messages") == 1
    assert _count(conn, "signals") == 1


def test_relay_duplicate_within_window_shares_one_signal_row(conn):
    first = _ingest(conn, channel_cfg=CHANNEL_A, message_id=4001, posted_at_utc=POSTED)
    second = _ingest(
        conn,
        channel_cfg=CHANNEL_B,
        message_id=5001,
        posted_at_utc=POSTED + timedelta(seconds=45),
    )

    assert first.outcome == "new"
    assert second.outcome == "relay_duplicate"
    assert second.kind == "signal"
    assert second.signal_id is None
    # Every message is a fixture: both raw messages are kept even though
    # only one signal was accepted.
    assert _count(conn, "messages") == 2
    assert _count(conn, "signals") == 1


def test_relay_outside_window_is_new_for_both(conn):
    first = _ingest(conn, channel_cfg=CHANNEL_A, message_id=4001, posted_at_utc=POSTED)
    second = _ingest(
        conn,
        channel_cfg=CHANNEL_B,
        message_id=5002,
        posted_at_utc=POSTED + timedelta(minutes=20),
    )

    assert first.outcome == "new"
    assert second.outcome == "new"
    assert _count(conn, "messages") == 2
    assert _count(conn, "signals") == 2


def test_sl_edit_is_recorded_as_edited_not_swallowed(conn):
    """This is the hazard the brief names directly: a bare INSERT OR
    IGNORE on (channel_id, message_id) can never see that the SL changed,
    because it never looks at content at all -- the correction would be
    silently dropped and the original (now-wrong) SL would stay on file
    forever. ingest() must instead detect the change and record it.
    """
    first = _ingest(conn)
    assert first.outcome == "new"

    edit_time = POSTED + timedelta(minutes=2)
    second = _ingest(conn, text=EDITED_SL_TEXT, edit_date_utc=edit_time)

    assert second.outcome == "edited"
    assert _count(conn, "signals") == 1  # updated in place, not duplicated

    row = conn.execute(
        "SELECT parsed_json, version FROM signals WHERE channel_id = ? AND message_id = ?",
        (CHANNEL_A.channel_id, SIGNAL_MESSAGE_ID),
    ).fetchone()
    parsed = json.loads(row["parsed_json"])
    assert parsed["sl"] == 4460.0  # the correction landed
    assert row["version"] == 2

    message_row = conn.execute(
        "SELECT raw_text, edit_date_utc FROM messages WHERE channel_id = ? AND message_id = ?",
        (CHANNEL_A.channel_id, SIGNAL_MESSAGE_ID),
    ).fetchone()
    assert message_row["raw_text"] == EDITED_SL_TEXT
    assert message_row["edit_date_utc"] == edit_time.isoformat()


def test_cosmetic_edit_is_duplicate(conn):
    first = _ingest(conn)
    assert first.outcome == "new"

    cosmetic_text = SIGNAL_TEXT + "   \n\U0001F680 \U0001F680"
    second = _ingest(
        conn, text=cosmetic_text, edit_date_utc=POSTED + timedelta(minutes=1)
    )

    assert second.outcome == "duplicate"
    assert _count(conn, "signals") == 1
    row = conn.execute(
        "SELECT version FROM signals WHERE channel_id = ? AND message_id = ?",
        (CHANNEL_A.channel_id, SIGNAL_MESSAGE_ID),
    ).fetchone()
    assert row["version"] == 1


# -- parse_failed bookkeeping -------------------------------------------------


def test_parse_failed_increments_skip_leaves_unchanged_signal_resets(conn):
    def failures():
        row = conn.execute(
            "SELECT consecutive_parse_failures FROM channel_state WHERE channel_id = ?",
            (CHANNEL_A.channel_id,),
        ).fetchone()
        return row["consecutive_parse_failures"]

    r1 = _ingest(conn, message_id=9001, text=BROKEN_TEXT)
    assert r1.outcome == "new" and r1.kind == "parse_failed"
    assert failures() == 1

    r2 = _ingest(conn, message_id=9002, text=CHATTER_TEXT)
    assert r2.outcome == "new" and r2.kind == "skip"
    assert failures() == 1  # skip must not touch the counter

    r3 = _ingest(conn, message_id=9003, text=SIGNAL_TEXT)
    assert r3.outcome == "new" and r3.kind == "signal"
    assert failures() == 0


# -- raw_text is a verbatim fixture for every outcome ------------------------


def test_skip_message_stores_raw_text_and_nonnull_content_hash(conn):
    result = _ingest(conn, message_id=9101, text=CHATTER_TEXT)
    assert result.outcome == "new"
    assert result.kind == "skip"

    row = conn.execute(
        "SELECT raw_text, content_hash, kind FROM messages "
        "WHERE channel_id = ? AND message_id = ?",
        (CHANNEL_A.channel_id, 9101),
    ).fetchone()
    assert row["raw_text"] == CHATTER_TEXT
    assert row["kind"] == "skip"
    assert row["content_hash"] is not None


# -- follow-ups ---------------------------------------------------------------


def test_followup_stores_row_with_reply_to_message_id(conn):
    result = _ingest(
        conn, message_id=9201, text="close all", reply_to_message_id=4001
    )
    assert result.outcome == "new"
    assert result.kind == "followup"
    assert result.followup_id is not None

    row = conn.execute(
        "SELECT op, reply_to_message_id FROM followups WHERE followup_id = ?",
        (result.followup_id,),
    ).fetchone()
    assert row["op"] == FollowUpOp.CLOSE_ALL.value
    assert row["reply_to_message_id"] == 4001


# -- now_utc is the clock, not the wall clock --------------------------------


def test_received_at_utc_is_the_passed_now_not_wall_clock(conn):
    fixed_now = datetime(2020, 1, 1, tzinfo=UTC)
    _ingest(conn, message_id=9301, now_utc=fixed_now)

    row = conn.execute(
        "SELECT received_at_utc FROM messages WHERE channel_id = ? AND message_id = ?",
        (CHANNEL_A.channel_id, 9301),
    ).fetchone()
    assert row["received_at_utc"] == fixed_now.isoformat()
