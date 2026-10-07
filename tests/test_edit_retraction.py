"""A retraction by edit: the poster appends "CANCELLED" to a signal
(ledger 161; group-3 leftover G3L-3).

Appending a retraction word leaves the trade hash unchanged, so the edit
used to count as cosmetic and the call stayed queued. An edit that ADDS
a retraction word -- CANCELLED, CANCELED, CANCEL, VOID or INVALID, as a
whole word, any case -- absent from the stored text is a withdrawal, as
an edit into chatter is: pending, the signal is VOIDED and withdrawn;
executed, a CRITICAL flag. It lives in `edits_repo`; the parser is not
touched. The post is recorded as no signal any more (`messages.kind`
'skip'), so every relay rule that re-reads a stored post to ask whether
it still shows the call (`relay_repo.post_shows_call`, the one helper)
sees a cancelled post as not showing it -- which is what makes a double
post cancelled one post at a time end with nothing claimable.

Whatever the stored version was -- chatter, the call without its stop,
a call already withdrawn -- an edit straight into the call WITH an added
retraction word is read as no signal: never completed within the 120 s
grace, never a post that stands again, never 'trade it by hand' (group 4
review, round 1).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from test_edits import (
    EDIT_1,
    SIGNAL_ID_MSG,
    SIGNAL_TEXT,
    A,
    _alerts,
    _mark_entry,
    _message,
    _outbox,
    _round,
    _rows,
    _run,
    _signal,
    _stored_signal,
    _stored_signal_among_chatter,
    _withdrawn,
)
from test_relay import ACCOUNTS, POSTED, VIP, _q, conn  # noqa: F401 -- conn: the fixture
from xaucopier.store import edits_repo, outbox
from xaucopier.store.messages_repo import ingest

CANCELLED = SIGNAL_TEXT + "\n❌ CANCELLED"


def _cancel(text: str, word: str) -> str:
    return f"{text}\n{word}"


# -- the event and the window: pending and executed ------------------------------


@pytest.mark.parametrize("path", ["event", "window"])
def test_appending_cancelled_voids_a_pending_signal(tmp_path, path: str) -> None:
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal_among_chatter(client, listener, time)
        if path == "event":
            assert await client.emit_edit(A, SIGNAL_ID_MSG, CANCELLED, edit_date=EDIT_1)
        else:
            with client.blackout():
                assert not await client.emit_edit(A, SIGNAL_ID_MSG, CANCELLED, edit_date=EDIT_1)
            await _round(listener, time)
        await _round(listener, time)  # nothing more on the next read

    _run(db_path, body)

    assert _signal(db_path)["state"] == "VOIDED"
    assert _withdrawn(db_path)
    assert _outbox(db_path, "ENTRY") == [("acct-1", "SKIPPED"), ("acct-2", "SKIPPED")]
    assert _alerts(db_path) == [("WARNING", edits_repo.SIGNAL_RETRACTED_BY_EDIT)]
    message = _message(db_path)
    assert (message["kind"], message["raw_text"]) == ("skip", CANCELLED)
    (detail,) = _q_detail(db_path)
    assert "CANCELLED" in detail


def _q_detail(db_path) -> list[str]:
    return [row[0] for row in _rows(db_path, "SELECT detail FROM alerts ORDER BY alert_id")]


def test_appending_cancelled_to_an_executed_signal_is_a_critical_flag(tmp_path) -> None:
    db_path = tmp_path / "store.db"

    async def body(client, listener, time):
        await _stored_signal(client, listener, time)
        _mark_entry(db_path, "acct-1", "DONE")
        assert await client.emit_edit(A, SIGNAL_ID_MSG, CANCELLED, edit_date=EDIT_1)

    _run(db_path, body)

    assert _signal(db_path)["state"] == "NEW"  # live legs are not closed
    assert _withdrawn(db_path)
    assert _alerts(db_path) == [("CRITICAL", edits_repo.LIVE_SIGNAL_RETRACTED_BY_EDIT)]
    assert _outbox(db_path, "ENTRY") == [("acct-1", "DONE"), ("acct-2", "SKIPPED")]


# -- which words, and only when added --------------------------------------------


def _one(conn, text: str, *, edit_to: str, message_id: int = 4001):  # noqa: F811
    """Store `text` on the VIP channel, edit it to `edit_to`; the result."""
    ingest(
        conn, channel_cfg=VIP, message_id=message_id, text=text, posted_at_utc=POSTED,
        edit_date_utc=None, reply_to_message_id=None, source="live",
        now_utc=POSTED + timedelta(seconds=1), accounts=ACCOUNTS,
    )
    conn.commit()
    result = edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=message_id, text=edit_to, posted_at_utc=POSTED,
        edit_date_utc=POSTED + timedelta(minutes=5), reply_to_message_id=None,
        now_utc=POSTED + timedelta(minutes=5), accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()
    return result


@pytest.mark.parametrize(
    "word",
    ["CANCELLED", "CANCELED", "CANCEL", "VOID", "INVALID", "cancelled", "Void", "❌ Cancelled!",
     "(invalid)"],
)
def test_each_retraction_word_added_withdraws_the_call(conn, word: str) -> None:  # noqa: F811
    result = _one(conn, SIGNAL_TEXT, edit_to=_cancel(SIGNAL_TEXT, word))
    assert result.outcome == "voided"
    assert _q(conn, "SELECT state, withdrawn_at_utc IS NOT NULL FROM signals") == [("VOIDED", 1)]


@pytest.mark.parametrize(
    "word",
    ["CANCELLATION", "INVALIDATED", "AVOID", "VOIDS", "precancel", "cancel-proof"],
)
def test_a_word_that_only_contains_a_retraction_word_is_cosmetic(conn, word: str) -> None:  # noqa: F811
    result = _one(conn, SIGNAL_TEXT, edit_to=_cancel(SIGNAL_TEXT, word))
    if word == "cancel-proof":
        # "cancel" IS a whole word there (a hyphen ends a word): a retraction.
        assert result.outcome == "voided"
        return
    assert result.outcome == "cosmetic"
    assert _q(conn, "SELECT state, withdrawn_at_utc IS NOT NULL FROM signals") == [("NEW", 0)]


def test_a_retraction_word_already_in_the_stored_text_is_not_added(conn) -> None:  # noqa: F811
    """A template that always carries such a word ("setup invalid below
    4460") is no retraction: only a word the edit ADDS is."""
    template = SIGNAL_TEXT + "\nSetup invalid below 4460"
    result = _one(conn, template, edit_to=template + " \U0001F525")
    assert result.outcome == "cosmetic"
    assert _q(conn, "SELECT state, withdrawn_at_utc IS NOT NULL FROM signals") == [("NEW", 0)]
    # ...but another word added to it is.
    again = edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=4001, text=template + "\nCANCELLED",
        posted_at_utc=POSTED, edit_date_utc=POSTED + timedelta(minutes=9),
        reply_to_message_id=None, now_utc=POSTED + timedelta(minutes=9), accounts=ACCOUNTS,
        detected_by="event",
    )
    assert again.outcome == "voided"


def test_a_cancelled_post_edited_again_but_still_cancelled_stays_withdrawn(conn) -> None:  # noqa: F811
    """A second edit that keeps the retraction word (a typo fixed, an
    emoji added) is still a retraction: recorded, never read as the call
    turned back into a signal."""
    _one(conn, SIGNAL_TEXT, edit_to=CANCELLED)
    result = edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=4001, text=CANCELLED + ", sorry traders",
        posted_at_utc=POSTED, edit_date_utc=POSTED + timedelta(minutes=9),
        reply_to_message_id=None, now_utc=POSTED + timedelta(minutes=9), accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()
    assert result.outcome == "recorded"
    assert [code for (code,) in _q(conn, "SELECT code FROM alerts ORDER BY alert_id")] == [
        edits_repo.SIGNAL_RETRACTED_BY_EDIT
    ]
    assert _q(conn, "SELECT kind FROM messages") == [("skip",)]


def test_a_repeat_delivery_of_the_cancelled_version_is_a_duplicate(conn) -> None:  # noqa: F811
    _one(conn, SIGNAL_TEXT, edit_to=CANCELLED)
    result = edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=4001, text=CANCELLED, posted_at_utc=POSTED,
        edit_date_utc=POSTED + timedelta(minutes=5), reply_to_message_id=None,
        now_utc=POSTED + timedelta(minutes=7), accounts=ACCOUNTS, detected_by="window",
    )
    assert result.outcome == "duplicate"


# -- a double post, cancelled one post at a time (G3L-3) ---------------------------


def _claimable(conn) -> list[tuple]:  # noqa: F811
    return _q(
        conn,
        "SELECT o.account_id, o.signal_id FROM outbox o JOIN signals s "
        "ON s.signal_id = o.signal_id WHERE o.kind = 'ENTRY' AND o.state = 'QUEUED' "
        "AND s.state = 'NEW' AND s.withdrawn_at_utc IS NULL ORDER BY 1, 2",
    )


def _edit_to(conn, message_id: int, text: str, at) -> None:  # noqa: F811
    edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=message_id, text=text, posted_at_utc=POSTED,
        edit_date_utc=at, reply_to_message_id=None, now_utc=at, accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()


@pytest.mark.parametrize("first", [4001, 4002], ids=["original-first", "repost-first"])
@pytest.mark.parametrize("executed", [False, True], ids=["pending", "executed"])
def test_a_double_post_cancelled_one_post_at_a_time_ends_with_nothing_claimable(
    conn, first: int, executed: bool  # noqa: F811
) -> None:
    """The VIP posts the call twice, then appends CANCELLED to both, one
    at a time. After the first, the call stands once (on the other post,
    promoted if it was the signal's own that went); after the second,
    nothing of it is claimable -- a cancelled post never counts as a post
    still showing the call, so it is never promoted or counted as the one
    that keeps the call standing."""
    for message_id, after in ((4001, 0), (4002, 60)):
        ingest(
            conn, channel_cfg=VIP, message_id=message_id, text=SIGNAL_TEXT,
            posted_at_utc=POSTED + timedelta(seconds=after), edit_date_utc=None,
            reply_to_message_id=None, source="live",
            now_utc=POSTED + timedelta(seconds=after + 1), accounts=ACCOUNTS,
        )
        conn.commit()
    if executed:
        conn.execute("UPDATE outbox SET state = 'DONE' WHERE account_id = 'acct-1'")
        conn.commit()
    second = 4001 + 4002 - first
    _edit_to(conn, first, SIGNAL_TEXT + "\nCANCELLED", POSTED + timedelta(minutes=3))
    standing = _claimable(conn)
    assert len(standing) == (1 if executed else 2)  # the call stands, once
    _edit_to(conn, second, SIGNAL_TEXT + "\nCANCELLED", POSTED + timedelta(minutes=4))
    assert _claimable(conn) == []
    if executed:
        # The executed signal is flagged, loudly.
        (signal_id,) = _q(conn, "SELECT DISTINCT signal_id FROM outbox WHERE state = 'DONE'")[0]
        assert _q(conn, "SELECT withdrawn_at_utc IS NOT NULL FROM signals WHERE signal_id = ?",
                  signal_id) == [(1,)]
        assert _q(conn, "SELECT COUNT(*) FROM alerts WHERE level = 'CRITICAL' AND signal_id = ?",
                  signal_id)[0][0] >= 1
    assert outbox.claim(conn, account_id="acct-2", pid=1, now_utc=POSTED + timedelta(hours=1)) is None


def test_a_cancelled_post_is_not_a_post_that_shows_the_call(conn) -> None:  # noqa: F811
    """The one helper every relay rule re-reads a stored post with."""
    from xaucopier.store import relay_repo

    _one(conn, SIGNAL_TEXT, edit_to=CANCELLED)
    (post,) = conn.execute("SELECT * FROM messages").fetchall()
    (signal,) = conn.execute("SELECT * FROM signals").fetchall()
    assert not relay_repo.post_shows_call(post, signal, channel_cfg=VIP)
    assert relay_repo.read_post(post, channel_cfg=VIP).kind == "skip"
    # ...while its text alone would still parse as the call.
    from xaucopier.parsing.router import route

    assert route(CANCELLED, channel_cfg=VIP, message_id=4001, posted_at_utc=POSTED).kind == "signal"


def test_a_cancelled_repeat_deleted_later_changes_nothing_more(conn) -> None:  # noqa: F811
    """A repeat cancelled while the original stood was a repeat removal
    (one WARNING; the call stands). Deleting it afterwards is only
    recorded: it had stopped showing the call already -- read as the relay
    rules read a post, not by its text, which still parses as the call."""
    for message_id, after in ((4001, 0), (4002, 60)):
        ingest(
            conn, channel_cfg=VIP, message_id=message_id, text=SIGNAL_TEXT,
            posted_at_utc=POSTED + timedelta(seconds=after), edit_date_utc=None,
            reply_to_message_id=None, source="live",
            now_utc=POSTED + timedelta(seconds=after + 1), accounts=ACCOUNTS,
        )
        conn.commit()
    _edit_to(conn, 4002, CANCELLED, POSTED + timedelta(minutes=3))
    assert [c for (c,) in _q(conn, "SELECT code FROM alerts")] == [
        edits_repo.SIGNAL_REPEAT_REMOVED
    ]
    result = edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4002, now_utc=POSTED + timedelta(minutes=4),
        accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()
    assert result.outcome == "deleted"
    assert [c for (c,) in _q(conn, "SELECT code FROM alerts")] == [
        edits_repo.SIGNAL_REPEAT_REMOVED
    ]
    assert len(_claimable(conn)) == 2  # the call stands on 4001


def test_a_relay_copy_cancelled_withdraws_the_call_it_repeats(conn) -> None:  # noqa: F811
    """Ruling C2 as for any edit away of a cross-channel copy: one source
    withdrew the call."""
    from test_relay import RELAY

    ingest(
        conn, channel_cfg=VIP, message_id=4001, text=SIGNAL_TEXT, posted_at_utc=POSTED,
        edit_date_utc=None, reply_to_message_id=None, source="live",
        now_utc=POSTED + timedelta(seconds=1), accounts=ACCOUNTS,
    )
    ingest(
        conn, channel_cfg=RELAY, message_id=7001, text=SIGNAL_TEXT,
        posted_at_utc=POSTED + timedelta(seconds=30), edit_date_utc=None,
        reply_to_message_id=None, source="live", now_utc=POSTED + timedelta(seconds=31),
        accounts=ACCOUNTS,
    )
    conn.commit()
    result = edits_repo.apply_edit(
        conn, channel_cfg=RELAY, message_id=7001, text=CANCELLED, posted_at_utc=POSTED,
        edit_date_utc=POSTED + timedelta(minutes=3), reply_to_message_id=None,
        now_utc=POSTED + timedelta(minutes=3), accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()
    assert result.outcome == "voided"
    assert _claimable(conn) == []
    (detail,) = _q(conn, "SELECT detail FROM alerts WHERE code = ?",
                   edits_repo.SIGNAL_RETRACTED_BY_EDIT)[0]
    assert "message 7001" in detail and "CANCELLED" in detail


def test_chatter_that_mentioned_a_cancel_edited_into_a_signal_is_no_retraction(conn) -> None:  # noqa: F811
    """Only a post retracted *as a signal* stays retracted: chatter that
    said "CANCELLED" and is then edited into a call keeping the word is
    the chatter turned into a signal, handled as such -- not a retraction
    of anything."""
    chatter = "Previous trade CANCELLED, new signal soon"
    result = _one(conn, chatter, edit_to=SIGNAL_TEXT + "\n" + chatter)
    assert result.outcome == "reclassified"
    assert _q(conn, "SELECT kind FROM messages") == [("signal",)]


@pytest.mark.parametrize("named_first", [4001, 4002], ids=["cancelled-first", "repeat-first"])
def test_a_cancelled_holder_deleted_with_its_repeat_is_not_the_post_that_withdrew_the_call(
    conn, named_first: int  # noqa: F811
) -> None:
    """G3L-3 where no SQL filter guards the re-read (`_holder_speaks`). The
    call was executed; its own message 4001 was cancelled while the repeat
    4002 stood (a repeat removal: the trade continues). Then one report
    deletes both. The call's last standing post is the repeat, so the
    repeat's deletion is what withdraws it -- a CRITICAL flag naming 4002,
    recorded as the post that withdrew it -- whichever post the report
    names first. Read by its text alone, the cancelled 4001 would still
    'show the call', and its deletion would be taken as the withdrawal."""
    for message_id, after in ((4001, 0), (4002, 60)):
        ingest(
            conn, channel_cfg=VIP, message_id=message_id, text=SIGNAL_TEXT,
            posted_at_utc=POSTED + timedelta(seconds=after), edit_date_utc=None,
            reply_to_message_id=None, source="live",
            now_utc=POSTED + timedelta(seconds=after + 1), accounts=ACCOUNTS,
        )
        conn.commit()
    conn.execute("UPDATE outbox SET state = 'DONE' WHERE account_id = 'acct-1'")
    conn.commit()
    _edit_to(conn, 4001, CANCELLED, POSTED + timedelta(minutes=3))
    assert [c for (c,) in _q(conn, "SELECT code FROM alerts")] == [
        edits_repo.SIGNAL_REPEAT_REMOVED
    ]
    for message_id in (named_first, 4001 + 4002 - named_first):
        edits_repo.apply_deletion(
            conn, channel_cfg=VIP, message_id=message_id, now_utc=POSTED + timedelta(minutes=4),
            accounts=ACCOUNTS, detected_by="event", also_deleted=frozenset({4001, 4002}),
        )
        conn.commit()
    assert _claimable(conn) == []
    assert _q(
        conn, "SELECT signal_id, withdrawn_at_utc IS NOT NULL, closed_by_message_id FROM signals"
    ) == [(1, 1, 4002)]
    assert _q(conn, "SELECT level, code, message_id FROM alerts ORDER BY alert_id") == [
        ("WARNING", edits_repo.SIGNAL_REPEAT_REMOVED, 4001),
        ("CRITICAL", edits_repo.LIVE_SIGNAL_DELETED, 4002),
    ]


# -- a post that was no signal, edited straight into a cancelled call ------------

#: A first version that is no signal: chatter announcing the call, and the
#: call without its stop (parse_failed).
ANNOUNCED = "XAUUSD BUY coming soon, get ready"
NO_STOP = SIGNAL_TEXT.replace("\U0001f534 SL: 4470\n", "")


def _no_signal_then(conn, first: str, edit_to: str, after: timedelta):  # noqa: F811
    ingest(
        conn, channel_cfg=VIP, message_id=4001, text=first, posted_at_utc=POSTED,
        edit_date_utc=None, reply_to_message_id=None, source="live",
        now_utc=POSTED + timedelta(seconds=1), accounts=ACCOUNTS,
    )
    conn.commit()
    result = edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=4001, text=edit_to, posted_at_utc=POSTED,
        edit_date_utc=POSTED + after, reply_to_message_id=None,
        now_utc=POSTED + after, accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()
    return result


@pytest.mark.parametrize(
    "after", [timedelta(seconds=60), timedelta(minutes=10)], ids=["within-the-grace", "after-it"]
)
@pytest.mark.parametrize("first", [ANNOUNCED, NO_STOP], ids=["chatter", "parse_failed"])
def test_a_post_that_was_no_signal_edited_straight_into_a_cancelled_call_is_never_traded(
    conn, first: str, after: timedelta  # noqa: F811
) -> None:
    """Ledger 161 read as the ruling says: an edit that ADDS a retraction
    word absent from the stored text is a withdrawal -- whatever the stored
    version was. The announcement (or the call without its stop) edited
    into the call with CANCELLED appended says the call is off: within the
    120 s grace it is not completed into a new signal, and after it the
    user is never told to trade it by hand."""
    assert route_kind(first) in ("skip", "parse_failed")
    result = _no_signal_then(conn, first, CANCELLED, after)
    assert result.signal_id is None
    assert _q(conn, "SELECT COUNT(*) FROM signals") == [(0,)]
    assert _q(conn, "SELECT COUNT(*) FROM outbox") == [(0,)]
    assert _q(conn, "SELECT kind, raw_text FROM messages") == [("skip", CANCELLED)]
    codes = [code for (code,) in _q(conn, "SELECT code FROM alerts")]
    assert edits_repo.EDIT_COMPLETED_SIGNAL not in codes
    assert edits_repo.EDIT_CREATED_SIGNAL not in codes
    assert all("by hand" not in detail for (detail,) in _q(conn, "SELECT detail FROM alerts"))


def test_the_same_edit_without_the_word_is_still_completed(conn) -> None:  # noqa: F811
    """The control: the announcement edited into the plain call within the
    grace is a new signal (ruling C5) -- only the added word withdraws it."""
    result = _no_signal_then(conn, ANNOUNCED, SIGNAL_TEXT, timedelta(seconds=60))
    assert result.signal_id is not None
    assert len(_claimable(conn)) == 2


def test_a_signal_edited_into_chatter_then_into_a_cancelled_call_stays_withdrawn(
    conn,  # noqa: F811
) -> None:
    """The call, withdrawn by an edit into chatter, then edited into the
    call with CANCELLED: still withdrawn, no second signals row, and no
    advice to trade it by hand."""
    _one(conn, SIGNAL_TEXT, edit_to="Wait for confirmation")
    _edit_to(conn, 4001, CANCELLED, POSTED + timedelta(minutes=6))
    assert _q(conn, "SELECT signal_id, state, withdrawn_at_utc IS NOT NULL FROM signals") == [
        (1, "VOIDED", 1)
    ]
    assert _claimable(conn) == []
    codes = [code for (code,) in _q(conn, "SELECT code FROM alerts")]
    assert edits_repo.EDIT_CREATED_SIGNAL not in codes
    assert all("by hand" not in detail for (detail,) in _q(conn, "SELECT detail FROM alerts"))


def test_a_post_edited_away_and_back_as_a_cancelled_call_never_keeps_a_promoted_repost(
    conn,  # noqa: F811
) -> None:
    """The VIP double-posts. The original is edited into chatter (its call
    withdrawn, the repost promoted), then into the call with CANCELLED --
    a post that says the call is off, never a post that stands again. So
    when the repost is deleted too, no post carries the call: the promoted
    signal is voided and nothing is claimable."""
    for message_id, after in ((4001, 0), (4002, 30)):
        ingest(
            conn, channel_cfg=VIP, message_id=message_id, text=SIGNAL_TEXT,
            posted_at_utc=POSTED + timedelta(seconds=after), edit_date_utc=None,
            reply_to_message_id=None, source="live",
            now_utc=POSTED + timedelta(seconds=after + 1), accounts=ACCOUNTS,
        )
        conn.commit()
    _edit_to(conn, 4001, "Good morning traders", POSTED + timedelta(minutes=3))
    assert _q(conn, "SELECT signal_id, message_id, state FROM signals ORDER BY 1") == [
        (1, 4001, "VOIDED"), (2, 4002, "NEW")
    ]
    _edit_to(conn, 4001, CANCELLED, POSTED + timedelta(minutes=4))
    assert _q(conn, "SELECT kind FROM messages WHERE message_id = 4001") == [("skip",)]
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4002, now_utc=POSTED + timedelta(minutes=5),
        accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()
    assert _claimable(conn) == []
    assert _q(conn, "SELECT signal_id, state, withdrawn_at_utc IS NOT NULL FROM signals "
                    "ORDER BY 1") == [(1, "VOIDED", 1), (2, "VOIDED", 1)]
    assert outbox.claim(conn, account_id="acct-2", pid=1, now_utc=POSTED + timedelta(hours=1)) is None


def route_kind(text: str) -> str:
    from xaucopier.parsing.router import route

    return route(text, channel_cfg=VIP, message_id=4001, posted_at_utc=POSTED).kind


# -- FF6 / FF15: a post first read already edited and cancelled ---------------------
#
# Controller decision, option (a): a post read for the first time as a
# signal that carries a retraction word is stored as no signal ('skip'),
# not traded, with a WARNING -- ONLY when Telegram marks it as edited
# (`edit_date` set): the NewMessage update was dropped and the poster
# appended CANCELLED before the next read, or watch caught up after a
# stop. An unedited post keeps today's behaviour, so a template that
# always carries e.g. "invalid" still trades.

TEMPLATE_WITH_INVALID = SIGNAL_TEXT + "\nSetup invalid below 4460"


def _first_read(conn, text: str, *, message_id: int = 4001, edited: bool, channel=VIP,  # noqa: F811
                posted=POSTED, fwd=None):
    result = ingest(
        conn, channel_cfg=channel, message_id=message_id, text=text, posted_at_utc=posted,
        edit_date_utc=posted + timedelta(seconds=40) if edited else None,
        reply_to_message_id=None, source="history", now_utc=posted + timedelta(minutes=2),
        accounts=ACCOUNTS,
        fwd_from_channel_id=None if fwd is None else fwd[0],
        fwd_from_post_id=None if fwd is None else fwd[1],
        fwd_from_date_utc=None if fwd is None else POSTED,
    )
    conn.commit()
    return result


def test_a_post_first_read_edited_and_cancelled_is_not_traded(conn) -> None:  # noqa: F811
    result = _first_read(conn, CANCELLED, edited=True)

    assert (result.outcome, result.kind, result.signal_id) == ("new", "skip", None)
    assert _q(conn, "SELECT COUNT(*) FROM signals") == [(0,)]
    assert _q(conn, "SELECT COUNT(*) FROM outbox") == [(0,)]
    assert _q(conn, "SELECT kind, raw_text FROM messages") == [("skip", CANCELLED)]
    (alert,) = _q(conn, "SELECT level, code, message_id, detail FROM alerts")
    assert alert[:3] == ("WARNING", edits_repo.CANCELLED_WHEN_FIRST_READ, 4001)
    assert "was edited" in alert[3] and "CANCELLED" in alert[3]
    assert "it was not traded" in alert[3]


def test_an_unedited_post_whose_template_says_invalid_still_trades(conn) -> None:  # noqa: F811
    result = _first_read(conn, TEMPLATE_WITH_INVALID, edited=False)

    assert (result.outcome, result.kind, result.signal_id) == ("new", "signal", 1)
    assert _q(conn, "SELECT state FROM outbox WHERE kind = 'ENTRY'") == [("QUEUED",), ("QUEUED",)]
    assert _q(conn, "SELECT COUNT(*) FROM alerts") == [(0,)]


def test_an_edited_post_with_no_retraction_word_still_trades(conn) -> None:  # noqa: F811
    result = _first_read(conn, SIGNAL_TEXT, edited=True)

    assert (result.kind, result.signal_id) == ("signal", 1)
    assert _q(conn, "SELECT COUNT(*) FROM outbox WHERE kind = 'ENTRY'") == [(2,)]


def test_a_post_first_read_cancelled_stays_so_while_the_word_stays(conn) -> None:  # noqa: F811
    _first_read(conn, CANCELLED, edited=True)
    later = edits_repo.apply_edit(
        conn, channel_cfg=VIP, message_id=4001, text=CANCELLED + " - sorry",
        posted_at_utc=POSTED, edit_date_utc=POSTED + timedelta(seconds=60), reply_to_message_id=None,
        now_utc=POSTED + timedelta(seconds=61), accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()

    assert later.kind == "skip"
    assert _q(conn, "SELECT COUNT(*) FROM signals") == [(0,)]


@pytest.mark.parametrize("bump", [False, True], ids=["by-hand", "forward"])
def test_a_repost_first_read_cancelled_is_never_promoted(conn, bump) -> None:  # noqa: F811
    """FF15: 4001 is signal 1; the same call posted again as 4002 is first
    read already edited and CANCELLED. Deleting 4001 withdraws the call:
    the cancelled repost is no post of it, so it is never promoted."""
    _first_read(conn, SIGNAL_TEXT, edited=False)
    repost = _first_read(
        conn, CANCELLED, message_id=4002, edited=True, posted=POSTED + timedelta(seconds=30),
        fwd=(VIP.channel_id, 4001) if bump else None,
    )
    assert (repost.outcome, repost.kind) == ("new", "skip")

    deletion = edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4001, now_utc=POSTED + timedelta(minutes=3),
        accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()

    assert (deletion.outcome, deletion.promoted_signal_id) == ("voided", None)
    assert _q(conn, "SELECT signal_id, state FROM signals") == [(1, "VOIDED")]
    assert _q(conn, "SELECT COUNT(*) FROM outbox WHERE state = 'QUEUED'") == [(0,)]


def test_a_follow_up_first_read_edited_and_saying_cancel_is_still_queued(conn) -> None:  # noqa: F811
    """Only a post that reads as a signal is read as a retraction: an
    instruction that mentions cancelling is still the instruction."""
    _first_read(conn, SIGNAL_TEXT, edited=False)
    followup = ingest(
        conn, channel_cfg=VIP, message_id=4005, text="Close half now, cancel the pending orders",
        posted_at_utc=POSTED + timedelta(minutes=1),
        edit_date_utc=POSTED + timedelta(minutes=2), reply_to_message_id=4001, source="history",
        now_utc=POSTED + timedelta(minutes=3), accounts=ACCOUNTS,
    )
    conn.commit()

    assert (followup.kind, followup.followup_id) == ("followup", 1)
    assert _q(conn, "SELECT COUNT(*) FROM outbox WHERE kind = 'FOLLOWUP'") == [(2,)]
