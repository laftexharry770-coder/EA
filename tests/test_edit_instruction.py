"""An edit into entry-shaped text that also reads as an instruction is
never silent (final fix FF2, group-4 leftover (1)).

Entry-shaped text -- a side word and an SL price -- that the channel's
parser cannot read is an unreadable signal, never a follow-up (ruling
F07). At ingest such a message raises ENTRY_SHAPED_INSTRUCTION_NOT_QUEUED
naming the instruction it could also be. An EDIT into such text raised
nothing (chatter) or only "follow-up withdrawn" (a follow-up): a close
instruction typed into a reply after it was posted went unnoticed. Both
now name the instruction, in the same plain words, and say it was NOT
done -- within the 120 s grace and after it alike.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from test_relay import ACCOUNTS, POSTED, VIP, _ingest, _q, conn  # noqa: F401 -- conn: the fixture
from xaucopier.store import edits_repo, messages_repo

INSTRUCTION = "Close all GOLD BUY now, SL 4470 hit"
SAID = "close the whole trade"


def _reply_then_edit(conn, first: str, *, after: int):  # noqa: F811
    _ingest(conn, VIP, 4001)
    posted = POSTED + timedelta(minutes=1)
    _ingest(conn, VIP, 4002, first, posted=posted, reply_to=4001)
    result = edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=4002, text=INSTRUCTION, posted_at_utc=posted,
        edit_date_utc=posted + timedelta(seconds=after), reply_to_message_id=4001,
        now_utc=posted + timedelta(seconds=after), accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()
    return result


def _alerts(conn) -> list[tuple]:  # noqa: F811
    return _q(conn, "SELECT level, code, message_id, detail FROM alerts ORDER BY alert_id")


@pytest.mark.parametrize("after", [30, 600], ids=["within-the-grace", "after-it"])
def test_chatter_edited_into_an_entry_shaped_instruction_names_it(conn, after):  # noqa: F811
    result = _reply_then_edit(conn, "Gold update soon", after=after)

    assert (result.outcome, result.kind) == ("recorded", "parse_failed")
    ((level, code, message_id, detail),) = _alerts(conn)
    assert (level, code, message_id) == (
        "WARNING", messages_repo.ENTRY_SHAPED_INSTRUCTION_NOT_QUEUED, 4002,
    )
    assert f"'{SAID}'" in detail
    assert "NOT done" in detail and "carry it out by hand" in detail
    assert "edit" in detail
    assert _q(conn, "SELECT COUNT(*) FROM outbox WHERE kind = 'FOLLOWUP'") == [(0,)]


@pytest.mark.parametrize("after", [30, 600], ids=["within-the-grace", "after-it"])
def test_a_follow_up_edited_into_an_entry_shaped_instruction_names_it(conn, after):  # noqa: F811
    result = _reply_then_edit(conn, "Close half now", after=after)

    assert result.outcome == "withdrawn"
    ((level, code, message_id, detail),) = _alerts(conn)
    assert (level, code, message_id) == ("WARNING", edits_repo.FOLLOWUP_RETRACTED_BY_EDIT, 4002)
    assert f"'{SAID}'" in detail
    assert "NOT done" in detail and "carry it out by hand" in detail
    assert _q(conn, "SELECT state FROM outbox WHERE kind = 'FOLLOWUP'") == [
        ("SKIPPED",), ("SKIPPED",)
    ]


@pytest.mark.parametrize("after", [30, 600], ids=["within-the-grace", "after-it"])
def test_chatter_edited_into_a_plain_instruction_is_as_before(conn, after):  # noqa: F811
    """Control: an edit into a readable instruction is a follow-up an edit
    created -- EDIT_CREATED_FOLLOWUP, nothing queued."""
    _ingest(conn, VIP, 4001)
    posted = POSTED + timedelta(minutes=1)
    _ingest(conn, VIP, 4002, "Gold update soon", posted=posted, reply_to=4001)
    result = edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=4002, text="Close all now", posted_at_utc=posted,
        edit_date_utc=posted + timedelta(seconds=after), reply_to_message_id=4001,
        now_utc=posted + timedelta(seconds=after), accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()

    assert (result.outcome, result.kind) == ("reclassified", "followup")
    assert [row[:2] for row in _alerts(conn)] == [("WARNING", edits_repo.EDIT_CREATED_FOLLOWUP)]


def test_a_follow_up_edited_into_chatter_names_no_instruction(conn):  # noqa: F811
    """Control: the withdrawal alert names an instruction only when the new
    text could be one."""
    _ingest(conn, VIP, 4001)
    posted = POSTED + timedelta(minutes=1)
    _ingest(conn, VIP, 4002, "Close half now", posted=posted, reply_to=4001)
    edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=4002, text="Good morning traders",
        posted_at_utc=posted, edit_date_utc=posted + timedelta(seconds=30),
        reply_to_message_id=4001, now_utc=posted + timedelta(seconds=30), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()

    ((_, code, _, detail),) = _alerts(conn)
    assert code == edits_repo.FOLLOWUP_RETRACTED_BY_EDIT
    assert "by hand" not in detail
