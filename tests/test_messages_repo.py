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
from xaucopier.store import db, messages_repo
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


@pytest.mark.parametrize("relayed", ["pre-edit", "post-edit"])
def test_a_relay_of_an_edited_signal_is_still_a_relay_duplicate(conn, relayed):
    """Channel A's signal is edited (SL 4470 -> 4460), which rewrites its
    trade_hash. Channel B then relays the call inside the relay window:
    whichever version it copied, it is the same trade and must not become
    a second signal -- a second ENTRY (Task 10 review, Minor 3)."""
    assert _ingest(conn).outcome == "new"
    edited = _ingest(conn, text=EDITED_SL_TEXT, edit_date_utc=POSTED + timedelta(minutes=1))
    assert edited.outcome == "edited"

    relay = _ingest(
        conn,
        channel_cfg=CHANNEL_B,
        message_id=900,
        text=SIGNAL_TEXT if relayed == "pre-edit" else EDITED_SL_TEXT,
        posted_at_utc=POSTED + timedelta(minutes=2),
    )

    assert relay.outcome == "relay_duplicate"
    assert _count(conn, "signals") == 1
    row = conn.execute("SELECT trade_hash, first_trade_hash FROM signals").fetchone()
    assert row["trade_hash"] != row["first_trade_hash"]  # the edit did move trade_hash


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


#: The fixture under another header: the most likely template change,
#: now parse_failed (final review F07), so it feeds the counter too.
NO_PIPE_TEXT = SIGNAL_TEXT.replace("XAUUSD | BUY SIGNAL", "XAUUSD BUY SIGNAL")


def _template_alerts(conn) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT level, code, channel_id, message_id FROM alerts WHERE code = ? "
            "ORDER BY alert_id",
            (messages_repo.TEMPLATE_CHANGED_SUSPECTED,),
        ).fetchall()
    ]


def test_three_unreadable_signals_in_a_row_raise_one_critical_alert_per_streak(conn):
    """Final review F07 / ruling: when consecutive_parse_failures reaches 3,
    ONE CRITICAL TEMPLATE_CHANGED_SUSPECTED names the channel -- not again
    at 4, 5 and on; a signal read cleanly ends the streak, and the next
    streak of three raises it again."""
    for n, text in enumerate([NO_PIPE_TEXT, BROKEN_TEXT], start=1):
        _ingest(conn, message_id=9000 + n, text=text)
        conn.commit()
    assert _template_alerts(conn) == []
    _ingest(conn, message_id=9003, text=CHATTER_TEXT)  # chatter neither counts nor resets
    _ingest(conn, message_id=9004, text=NO_PIPE_TEXT)
    conn.commit()
    assert _template_alerts(conn) == [
        ("CRITICAL", messages_repo.TEMPLATE_CHANGED_SUSPECTED, CHANNEL_A.channel_id, 9004)
    ]
    (detail,) = conn.execute(
        "SELECT detail FROM alerts WHERE code = ?", (messages_repo.TEMPLATE_CHANGED_SUSPECTED,)
    ).fetchone()
    assert str(CHANNEL_A.channel_id) in detail and CHANNEL_A.name in detail
    for word in ("client", "listener", "RPC", "flood"):
        assert word not in detail
    for n in (9005, 9006):  # 4 and 5 in a row: no more
        _ingest(conn, message_id=n, text=BROKEN_TEXT)
    assert len(_template_alerts(conn)) == 1
    _ingest(conn, message_id=9007, text=SIGNAL_TEXT)  # read cleanly: the streak ends
    for n in (9008, 9009):
        _ingest(conn, message_id=n, text=BROKEN_TEXT)
    assert len(_template_alerts(conn)) == 1
    _ingest(conn, message_id=9010, text=NO_PIPE_TEXT)
    assert [row[3] for row in _template_alerts(conn)] == [9004, 9010]


def test_an_entry_in_another_layout_is_never_queued_as_a_follow_up(conn):
    """Final review F07's end-to-end case: with a signal live on the
    channel, an entry in another layout used to be stored as an AMEND_SL
    follow-up of its own stop, and queued. Now it is an unreadable signal:
    no followups row, no outbox row, and the counter moves."""
    _ingest(conn, message_id=4001, text=SIGNAL_TEXT)
    result = _ingest(conn, message_id=4010, text="BUY GOLD 4480\nSL: 4470\nTP: 4490")
    assert (result.outcome, result.kind, result.followup_id) == ("new", "parse_failed", None)
    assert _count(conn, "followups") == 0
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE kind = 'FOLLOWUP'").fetchone()[0] == 0
    (failures,) = conn.execute(
        "SELECT consecutive_parse_failures FROM channel_state WHERE channel_id = ?",
        (CHANNEL_A.channel_id,),
    ).fetchone()
    assert failures == 1


def _instruction_alerts(conn) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT level, code, channel_id, message_id, detail FROM alerts WHERE code = ? "
            "ORDER BY alert_id",
            (messages_repo.ENTRY_SHAPED_INSTRUCTION_NOT_QUEUED,),
        ).fetchall()
    ]


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("Close all GOLD BUY now, SL 4470 hit", "close the whole trade"),
        ("XAUUSD BUY TP1 hit ✅ move SL 4480", "move the stop loss to 4480"),
        # A new entry in another layout reads as "move the stop" too: the
        # user is told both readings, and that neither was acted on.
        ("BUY GOLD 4480\nSL: 4470\nTP: 4490", "move the stop loss to 4470"),
    ],
    ids=["close-all", "amend-sl", "entry-in-another-layout"],
)
def test_an_entry_shaped_instruction_is_reported_the_moment_it_is_not_queued(conn, text, words):
    """Ruling F07's cost: an oddly worded genuine follow-up that carries a
    side word and a stop price is recorded as unreadable instead of queued
    -- LOUDLY. The counter alone is not loud until a third one: each such
    message raises a WARNING naming the instruction it seemed to give."""
    _ingest(conn, message_id=4001, text=SIGNAL_TEXT)
    result = _ingest(conn, message_id=4010, text=text)
    assert (result.outcome, result.kind, result.followup_id) == ("new", "parse_failed", None)
    assert _count(conn, "followups") == 0
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE kind = 'FOLLOWUP'").fetchone()[0] == 0
    ((level, code, channel_id, message_id, detail),) = _instruction_alerts(conn)
    assert (level, channel_id, message_id) == ("WARNING", CHANNEL_A.channel_id, 4010)
    assert words in detail and "4010" in detail and CHANNEL_A.name in detail
    assert "by hand" in detail
    for word in ("client", "listener", "RPC", "flood"):
        assert word not in detail


@pytest.mark.parametrize(
    "text", [NO_PIPE_TEXT, BROKEN_TEXT, CHATTER_TEXT, SIGNAL_TEXT, "close all"],
    ids=["changed-header", "broken-zone", "chatter", "signal", "followup"],
)
def test_nothing_that_reads_as_no_instruction_raises_the_instruction_warning(conn, text):
    _ingest(conn, message_id=4010, text=text)
    assert _instruction_alerts(conn) == []


def test_the_template_alert_is_per_channel(conn):
    for n in (1, 2):
        _ingest(conn, message_id=9000 + n, text=BROKEN_TEXT)
        _ingest(conn, channel_cfg=CHANNEL_B, message_id=7000 + n, text=BROKEN_TEXT)
    assert _template_alerts(conn) == []
    _ingest(conn, channel_cfg=CHANNEL_B, message_id=7003, text=BROKEN_TEXT)
    assert [row[2] for row in _template_alerts(conn)] == [CHANNEL_B.channel_id]


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


# -- migration 4: who posted a message, and where a forward came from ---------
#
# ingest() stores them for every kind of message, as optional keyword
# arguments that default to NULL (the listener fills them from Telegram;
# a message stored before migration 4 has none).

_PROVENANCE_COLUMNS = (
    "sender_kind",
    "sender_id",
    "is_post",
    "fwd_from_channel_id",
    "fwd_from_post_id",
    "fwd_from_date_utc",
    "relay_of_signal_id",
    "relay_of_followup_id",
)


def _provenance(conn, message_id: int, channel_id: int = CHANNEL_A.channel_id):
    row = conn.execute(
        f"SELECT {', '.join(_PROVENANCE_COLUMNS)} FROM messages "
        "WHERE channel_id = ? AND message_id = ?",
        (channel_id, message_id),
    ).fetchone()
    return dict(zip(_PROVENANCE_COLUMNS, tuple(row)))


@pytest.mark.parametrize(
    ("text", "kind"),
    [(SIGNAL_TEXT, "signal"), (CHATTER_TEXT, "skip"), (BROKEN_TEXT, "parse_failed"),
     ("close all", "followup")],
    ids=["signal", "skip", "parse_failed", "followup"],
)
def test_provenance_columns_default_to_null(conn, text, kind):
    result = _ingest(conn, message_id=9401, text=text)
    assert (result.outcome, result.kind) == ("new", kind)
    assert _provenance(conn, 9401) == dict.fromkeys(_PROVENANCE_COLUMNS)


@pytest.mark.parametrize(
    ("text", "kind"),
    [(SIGNAL_TEXT, "signal"), (CHATTER_TEXT, "skip"), (BROKEN_TEXT, "parse_failed"),
     ("close all", "followup")],
    ids=["signal", "skip", "parse_failed", "followup"],
)
def test_ingest_stores_sender_and_forward_provenance(conn, text, kind):
    from datetime import timezone

    forwarded_at = datetime(2026, 9, 5, 13, 30, 15, tzinfo=timezone(timedelta(hours=3)))
    result = ingest(
        conn,
        channel_cfg=CHANNEL_A,
        message_id=9402,
        text=text,
        posted_at_utc=POSTED,
        edit_date_utc=None,
        reply_to_message_id=None,
        source="live",
        now_utc=NOW,
        accounts=(),
        sender_kind="channel",
        sender_id=CHANNEL_A.channel_id,
        is_post=True,
        fwd_from_channel_id=-1003333333333,
        fwd_from_post_id=77,
        fwd_from_date_utc=forwarded_at,
    )
    assert (result.outcome, result.kind) == ("new", kind)
    assert _provenance(conn, 9402) == {
        "sender_kind": "channel",
        "sender_id": CHANNEL_A.channel_id,
        "is_post": 1,
        "fwd_from_channel_id": -1003333333333,
        "fwd_from_post_id": 77,
        # Stored in UTC, like every other timestamp in the store.
        "fwd_from_date_utc": "2026-09-05T10:30:15+00:00",
        "relay_of_signal_id": None,
        "relay_of_followup_id": None,
    }


def test_a_group_members_message_is_stored_as_not_a_post(conn):
    _ingest(conn, message_id=9403, text=CHATTER_TEXT)
    ingest(
        conn,
        channel_cfg=CHANNEL_A,
        message_id=9404,
        text=CHATTER_TEXT,
        posted_at_utc=POSTED,
        edit_date_utc=None,
        reply_to_message_id=None,
        source="poll",
        now_utc=NOW,
        accounts=(),
        sender_kind="user",
        sender_id=424242,
        is_post=False,
    )
    assert _provenance(conn, 9404)["is_post"] == 0
    assert _provenance(conn, 9404)["sender_kind"] == "user"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        # The raw positive Telegram id of a channel is not the stored form.
        ({"fwd_from_channel_id": 3333333333}, "fwd_from_channel_id"),
        ({"fwd_from_date_utc": datetime(2026, 9, 5, 10, 30)}, "fwd_from_date_utc"),
        ({"is_post": 1}, "is_post"),
        ({"sender_kind": "bot"}, "sender_kind"),
        # Group-1 review: only the sign was checked. A bare negated id is
        # not the -100... form, and would never match a stored channel_id
        # in the forward lookup (F09) -- a late relay copy would become a
        # second ENTRY. Every channel peer id is -(10**12 + id).
        ({"fwd_from_channel_id": -3333333333}, "fwd_from_channel_id"),
        ({"fwd_from_channel_id": -1000000000000}, "fwd_from_channel_id"),
        ({"fwd_from_channel_id": "-1003333333333"}, "fwd_from_channel_id"),
        ({"fwd_from_channel_id": -1003333333333.0}, "fwd_from_channel_id"),
        ({"fwd_from_channel_id": True}, "fwd_from_channel_id"),
        ({"sender_id": "abc"}, "sender_id"),
        ({"sender_id": True}, "sender_id"),
        ({"sender_id": 42.0}, "sender_id"),
        ({"fwd_from_post_id": "x"}, "fwd_from_post_id"),
        ({"fwd_from_post_id": True}, "fwd_from_post_id"),
        ({"fwd_from_post_id": 0}, "fwd_from_post_id"),
        ({"fwd_from_date_utc": "2026-09-05T10:30:00+00:00"}, "fwd_from_date_utc"),
        ({"sender_kind": ["user"]}, "sender_kind"),
    ],
)
def test_malformed_provenance_is_refused_before_anything_is_stored(conn, kwargs, match):
    with pytest.raises(ValueError, match=match):
        ingest(
            conn,
            channel_cfg=CHANNEL_A,
            message_id=9405,
            text=SIGNAL_TEXT,
            posted_at_utc=POSTED,
            edit_date_utc=None,
            reply_to_message_id=None,
            source="live",
            now_utc=NOW,
            accounts=(),
            **kwargs,
        )
    assert _count(conn, "messages") == 0
    assert _count(conn, "signals") == 0


def test_the_first_channel_id_in_the_minus_100_form_is_accepted(conn):
    """The boundary of the -100... check: -(10**12 + 1) is channel 1."""
    ingest(
        conn,
        channel_cfg=CHANNEL_A,
        message_id=9406,
        text=CHATTER_TEXT,
        posted_at_utc=POSTED,
        edit_date_utc=None,
        reply_to_message_id=None,
        source="live",
        now_utc=NOW,
        accounts=(),
        sender_id=-1000000000001,
        fwd_from_channel_id=-1000000000001,
        fwd_from_post_id=1,
    )
    provenance = _provenance(conn, 9406)
    assert provenance["fwd_from_channel_id"] == -1000000000001
    assert provenance["fwd_from_post_id"] == 1
    assert provenance["sender_id"] == -1000000000001


# -- final review F39: parsed_json.source is where the message really came from


def _parsed_source(conn) -> str:
    (source,) = conn.execute(
        "SELECT json_extract(parsed_json, '$.source') FROM signals "
        "WHERE channel_id = ? AND message_id = ?",
        (CHANNEL_A.channel_id, SIGNAL_MESSAGE_ID),
    ).fetchone()
    return source


@pytest.mark.parametrize("source", ["live", "poll", "backfill"])
def test_signal_parsed_json_records_the_messages_real_source(conn, source):
    """The parser's Signal.source is always 'live' (routing takes no
    source), and signals.parsed_json copied it -- so every signal the poll
    or backfill recovered said 'live', the one provenance value Plan 3 is
    told to read at claim time."""
    assert _ingest(conn, source=source).outcome == "new"
    assert _parsed_source(conn) == source
    (stored,) = conn.execute("SELECT source FROM messages").fetchone()
    assert stored == source


def test_an_edit_keeps_the_signals_original_source(conn):
    """An edit re-derives parsed_json in full; the message's provenance is
    how it was first stored, whichever path brought the new version."""
    assert _ingest(conn, source="poll").outcome == "new"
    edited = _ingest(
        conn, text=EDITED_SL_TEXT, edit_date_utc=POSTED + timedelta(minutes=2), source="live"
    )
    assert edited.outcome == "edited"
    assert json.loads(
        conn.execute("SELECT parsed_json FROM signals").fetchone()["parsed_json"]
    )["sl"] == 4460.0
    assert _parsed_source(conn) == "poll"


def test_the_template_alert_also_covers_a_channel_that_never_used_the_layout(conn):
    """Group-4 review leftover G4L-1: "the channel has probably changed how it
    writes its signals" misleads on a channel that never used the MMH
    layout, and on a group where members post their own calls."""
    for n, text in enumerate([NO_PIPE_TEXT, BROKEN_TEXT, NO_PIPE_TEXT], start=1):
        _ingest(conn, message_id=9100 + n, text=text)
    conn.commit()
    (detail,) = conn.execute(
        "SELECT detail FROM alerts WHERE code = ?", (messages_repo.TEMPLATE_CHANGED_SUSPECTED,)
    ).fetchone()
    assert "changed how it writes its signals" in detail
    assert "or this channel writes some calls in a layout this program does not read" in detail
    assert "group" in detail
    assert "NOT traded" in detail
