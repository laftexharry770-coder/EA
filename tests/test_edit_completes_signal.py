"""A post completed by a quick edit (final review F11, ruling C5; group-3
leftover G3L-1).

A poster sends a call missing a line -- or "XAUUSD BUY coming soon" --
and edits it into the full signal seconds later. The same channel
history used to give opposite trade outcomes depending on the update
stream's health: seen live, the first version was stored as parse_failed
or skip and the edit only raised an alert ("edits never open trades");
missed, the poll stored the final text as a new signal and it traded.
Ruling C5: a message stored as parse_failed or skip that an edit turns
into a valid signal within `EDIT_GRACE_SECONDS` (120 s) of its posted_at
becomes a NEW signal, fanned out, subject to relay dedup like any new
signal, with a WARNING `EDIT_COMPLETED_SIGNAL` -- so within the grace
every delivery path agrees. Beyond it, still no trade, but
`EDIT_CREATED_SIGNAL` is CRITICAL when it asks the user to trade by hand.

G3L-1: before an edit creates a signal, or advises trading by hand, the
same call already live or queued (`relay_repo.relay_of_signal`: the same
post, the same trade close together, through the call's replacement
chain) is looked for: then no second signal -- the message is linked as
a copy -- and the alert says the call is already queued, never "trade it
by hand". A message that already has a signals row (a VOIDED one
included) never gets a second.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from test_edits import (
    SIGNAL_ID_MSG,
    SIGNAL_TEXT,
    UNPARSEABLE_EDIT,
    A,
    _msg,
    _outbox,
    _round,
    _rows,
    _run,
)
from test_relay import ACCOUNTS, POSTED, RELAY, VIP, _q, conn  # noqa: F401 -- conn: the fixture
from xaucopier.parsing.router import route
from xaucopier.store import edits_repo
from xaucopier.store.messages_repo import ingest

CHATTER_FIRST = "XAUUSD BUY coming soon"
assert route(UNPARSEABLE_EDIT, channel_cfg=VIP, message_id=1, posted_at_utc=POSTED).kind == (
    "parse_failed"
)
assert route(CHATTER_FIRST, channel_cfg=VIP, message_id=1, posted_at_utc=POSTED).kind == "skip"
BY_HAND = "trade it by hand."


def _details(db_path) -> list[str]:
    return [row[0] for row in _rows(db_path, "SELECT detail FROM alerts ORDER BY alert_id")]


# -- the four delivery paths agree within the grace (F11) -----------------------

PATHS = ["both-delivered", "edit-dropped", "post-dropped", "both-dropped"]


def _paths_run(db_path, first: str, path: str, edit_after: timedelta):
    """The poster posts `first` at POSTED and edits it into the full
    signal `edit_after` later; the update stream delivers or drops each
    of the two events; then two poll rounds."""

    async def body(client, listener, time):
        post_dropped = path in ("post-dropped", "both-dropped")
        edit_dropped = path in ("edit-dropped", "both-dropped")
        if post_dropped:
            with client.blackout():
                await client.emit(A, _msg(SIGNAL_ID_MSG, first))
        else:
            assert await client.emit(A, _msg(SIGNAL_ID_MSG, first))
        if edit_dropped:
            with client.blackout():
                await client.emit_edit(A, SIGNAL_ID_MSG, SIGNAL_TEXT, edit_date=POSTED + edit_after)
        else:
            assert await client.emit_edit(
                A, SIGNAL_ID_MSG, SIGNAL_TEXT, edit_date=POSTED + edit_after
            )
        await _round(listener, time)
        await _round(listener, time)

    _run(db_path, body)


def _parse_failures(db_path) -> int:
    rows = _rows(db_path, "SELECT consecutive_parse_failures FROM channel_state WHERE channel_id = ?", (A,))
    return rows[0][0] if rows else 0


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("first", [UNPARSEABLE_EDIT, CHATTER_FIRST], ids=["parse_failed", "skip"])
def test_a_post_completed_within_the_grace_trades_on_every_delivery_path(tmp_path, path, first):
    db_path = tmp_path / "store.db"
    _paths_run(db_path, first, path, timedelta(seconds=40))

    assert _rows(db_path, "SELECT message_id, state, withdrawn_at_utc FROM signals") == [
        (SIGNAL_ID_MSG, "NEW", None)
    ]
    assert _outbox(db_path, "ENTRY") == [("acct-1", "QUEUED"), ("acct-2", "QUEUED")]
    assert _parse_failures(db_path) == 0
    alerts = _rows(db_path, "SELECT level, code FROM alerts ORDER BY alert_id")
    if path in ("both-delivered", "edit-dropped"):
        assert alerts == [("WARNING", edits_repo.EDIT_COMPLETED_SIGNAL)]
        (detail,) = _details(db_path)
        assert "queued" in detail and BY_HAND not in detail
    else:
        assert alerts == []  # the poll stored the complete post: nothing to say


@pytest.mark.parametrize("path", PATHS)
def test_a_post_completed_after_the_grace_is_not_traded_when_its_first_version_was_seen(
    tmp_path, path
):
    """Beyond the grace the ruling stands: an edit never opens a trade --
    and the alert asking the user to trade it by hand is CRITICAL. When
    the stream missed the first version, nothing shows it was not a
    signal: the poll trades the post as posted (the asymmetry ruling C5
    accepts)."""
    db_path = tmp_path / "store.db"
    _paths_run(db_path, UNPARSEABLE_EDIT, path, timedelta(minutes=5))

    if path in ("both-delivered", "edit-dropped"):
        assert _rows(db_path, "SELECT COUNT(*) FROM signals") == [(0,)]
        assert _outbox(db_path, "ENTRY") == []
        assert _rows(db_path, "SELECT level, code FROM alerts") == [
            ("CRITICAL", edits_repo.EDIT_CREATED_SIGNAL)
        ]
        (detail,) = _details(db_path)
        assert BY_HAND in detail
    else:
        assert _rows(db_path, "SELECT state FROM signals") == [("NEW",)]


# -- the grace's edge, at the store -----------------------------------------------


def _store(conn, message_id: int, text: str, *, channel=VIP, after=timedelta(0)):  # noqa: F811
    ingest(
        conn, channel_cfg=channel, message_id=message_id, text=text,
        posted_at_utc=POSTED + after, edit_date_utc=None, reply_to_message_id=None,
        source="live", now_utc=POSTED + after + timedelta(seconds=1), accounts=ACCOUNTS,
    )
    conn.commit()


def _edit(conn, message_id: int, text: str, at, *, channel=VIP, posted=POSTED):  # noqa: F811
    result = edits_repo.apply_edit(
        conn, channel_cfg=channel, message_id=message_id, text=text, posted_at_utc=posted,
        edit_date_utc=at, reply_to_message_id=None, now_utc=at, accounts=ACCOUNTS,
        detected_by="event",
    )
    conn.commit()
    return result


def _queued(conn) -> list[tuple]:  # noqa: F811
    return _q(
        conn,
        "SELECT signal_id, COUNT(*) FROM outbox WHERE kind = 'ENTRY' AND state = 'QUEUED' "
        "GROUP BY signal_id ORDER BY signal_id",
    )


def _alert_rows(conn) -> list[tuple]:  # noqa: F811
    return _q(conn, "SELECT level, code, message_id, signal_id FROM alerts ORDER BY alert_id")


@pytest.mark.parametrize(
    ("seconds", "completed"), [(0, True), (120, True), (121, False), (600, False)]
)
def test_the_grace_is_120_seconds_after_the_post_both_ends_included(conn, seconds, completed):  # noqa: F811
    _store(conn, 4001, UNPARSEABLE_EDIT)
    result = _edit(conn, 4001, SIGNAL_TEXT, POSTED + timedelta(seconds=seconds))
    assert edits_repo.EDIT_GRACE_SECONDS == 120
    if completed:
        assert result.outcome == "completed" and result.signal_id == 1
        assert _queued(conn) == [(1, 2)]
    else:
        assert result.outcome == "reclassified" and result.signal_id is None
        assert _queued(conn) == []


def test_a_follow_up_edited_into_a_signal_is_never_completed(conn):  # noqa: F811
    """Ruling C5 names parse_failed and skip only: a follow-up edited into
    a signal within seconds is still not traded (its instruction is
    withdrawn), and the alert asking to trade it by hand is CRITICAL."""
    _store(conn, 4001, "Close half now")
    result = _edit(conn, 4001, SIGNAL_TEXT, POSTED + timedelta(seconds=20))
    assert result.outcome == "reclassified"
    assert _q(conn, "SELECT COUNT(*) FROM signals") == [(0,)]
    assert (
        "CRITICAL", edits_repo.EDIT_CREATED_SIGNAL
    ) in [(level, code) for level, code, *_ in _alert_rows(conn)]


# -- a live match first (G3L-1) ----------------------------------------------------


@pytest.mark.parametrize(
    ("chatter_at", "back_at"),
    [(timedelta(minutes=3), timedelta(minutes=4)), (timedelta(seconds=40), timedelta(seconds=60))],
    ids=["hunt-d", "within-the-grace"],
)
def test_a_signal_edited_away_and_back_after_its_repost_was_promoted_is_no_second_signal(
    conn, chatter_at, back_at  # noqa: F811
):
    """Group-3 review HUNT d: the original is edited into chatter, its
    repost is promoted, then the original is edited back into the call.
    It has a VOIDED signals row of its own: never a second one, whatever
    the grace -- and no "trade it by hand", as the call is queued."""
    _store(conn, 4001, SIGNAL_TEXT)
    _store(conn, 4002, SIGNAL_TEXT, after=timedelta(seconds=20))
    away = _edit(conn, 4001, "Good morning traders", POSTED + chatter_at)
    assert away.promoted_signal_id == 2
    back = _edit(conn, 4001, SIGNAL_TEXT, POSTED + back_at)
    assert back.outcome != "completed"
    assert _q(conn, "SELECT signal_id, message_id, state FROM signals ORDER BY 1") == [
        (1, 4001, "VOIDED"), (2, 4002, "NEW")
    ]
    assert _queued(conn) == [(2, 2)]
    details = [d for (d,) in _q(conn, "SELECT detail FROM alerts")]
    assert not any(BY_HAND in d for d in details), details
    assert any("signal 2" in d for d in _q_last(conn))


def _q_last(conn) -> list[str]:  # noqa: F811
    return [d for (d,) in _q(conn, "SELECT detail FROM alerts ORDER BY alert_id DESC LIMIT 1")]


@pytest.mark.parametrize(
    ("edit_after", "code"),
    [(timedelta(seconds=30), edits_repo.EDIT_COMPLETED_SIGNAL),
     (timedelta(minutes=5), edits_repo.EDIT_CREATED_SIGNAL)],
    ids=["within-the-grace", "after-the-grace"],
)
@pytest.mark.parametrize("channel", ["same-channel", "other-channel"])
def test_chatter_edited_into_a_call_already_queued_is_linked_as_a_copy(
    conn, edit_after, code, channel  # noqa: F811
):
    """The call is live as signal 1. Chatter posted 10 s later -- on the
    same channel or a relay -- is edited into the same call: no second
    signal, the message is linked as a copy of signal 1, and the WARNING
    says the call is already queued as signal 1 -- never "trade it by
    hand"."""
    where = VIP if channel == "same-channel" else RELAY
    _store(conn, 4001, SIGNAL_TEXT)
    _store(conn, 7003 if where is RELAY else 4003, "chat", channel=where, after=timedelta(seconds=10))
    message_id = 7003 if where is RELAY else 4003
    result = _edit(
        conn, message_id, SIGNAL_TEXT, POSTED + timedelta(seconds=10) + edit_after,
        channel=where, posted=POSTED + timedelta(seconds=10),
    )
    assert result.signal_id is None
    assert _q(conn, "SELECT signal_id FROM signals") == [(1,)]
    assert _queued(conn) == [(1, 2)]
    assert _q(
        conn, "SELECT relay_of_signal_id FROM messages WHERE channel_id = ? AND message_id = ?",
        where.channel_id, message_id,
    ) == [(1,)]
    assert [(level, c) for level, c, *_ in _alert_rows(conn)] == [("WARNING", code)]
    (detail,) = _q_last(conn)
    assert "signal 1" in detail and "queued" in detail and BY_HAND not in detail


def test_a_call_withdrawn_on_this_channel_completed_by_an_edit_is_the_call_posted_again(conn):  # noqa: F811
    """Relay dedup exactly as for a new post: this channel withdrew the
    call (its only post deleted); a post of it completed by an edit
    within the grace is the call posted again -- a new signal replacing
    the withdrawn one."""
    _store(conn, 4001, SIGNAL_TEXT)
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4001, now_utc=POSTED + timedelta(seconds=20),
        accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()
    _store(conn, 4003, UNPARSEABLE_EDIT, after=timedelta(seconds=30))
    result = _edit(
        conn, 4003, SIGNAL_TEXT, POSTED + timedelta(seconds=50), posted=POSTED + timedelta(seconds=30)
    )
    assert result.outcome == "completed" and result.signal_id == 2
    assert _q(conn, "SELECT signal_id, state, promoted_from_signal_id, replacement_kind "
                    "FROM signals ORDER BY 1") == [(1, "VOIDED", None, None),
                                                   (2, "NEW", 1, "posted_again")]
    assert _queued(conn) == [(2, 2)]


def test_a_copy_of_a_call_another_channel_withdrew_is_held_back_loudly(conn):  # noqa: F811
    """...and a copy of a call withdrawn on another channel is held back,
    with the CRITICAL COPY_OF_WITHDRAWN_SIGNAL a new post would get."""
    _store(conn, 4001, SIGNAL_TEXT)
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4001, now_utc=POSTED + timedelta(seconds=20),
        accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()
    _store(conn, 7003, UNPARSEABLE_EDIT, channel=RELAY, after=timedelta(seconds=30))
    result = _edit(
        conn, 7003, SIGNAL_TEXT, POSTED + timedelta(seconds=50), channel=RELAY,
        posted=POSTED + timedelta(seconds=30),
    )
    assert result.outcome == "completed" and result.signal_id is None
    assert _q(conn, "SELECT COUNT(*) FROM signals") == [(1,)]
    assert _queued(conn) == []
    codes = [(level, c) for level, c, *_ in _alert_rows(conn)]
    assert ("CRITICAL", "COPY_OF_WITHDRAWN_SIGNAL") in codes
    assert ("WARNING", edits_repo.EDIT_COMPLETED_SIGNAL) in codes


def test_an_old_post_edited_into_the_live_call_is_linked_to_it(conn):  # noqa: F811
    """Past the grace, a chatter post half an hour older than the call,
    edited into it a minute after the call was posted: not a relay match
    by its own date, but the live call named for it (near the edit,
    ruling R6-1) is the call it now shows -- linked as its copy, and the
    alert says the call is queued (G3L-1)."""
    _store(conn, 4000, "chat", after=-timedelta(minutes=30))
    _store(conn, 4001, SIGNAL_TEXT)
    result = _edit(
        conn, 4000, SIGNAL_TEXT, POSTED + timedelta(minutes=1),
        posted=POSTED - timedelta(minutes=30),
    )
    assert result.outcome == "reclassified"
    assert _q(conn, "SELECT relay_of_signal_id FROM messages WHERE message_id = 4000") == [(1,)]
    assert [(level, c) for level, c, *_ in _alert_rows(conn)] == [
        ("WARNING", edits_repo.EDIT_CREATED_SIGNAL)
    ]
    assert "signal 1" in _q_last(conn)[0] and BY_HAND not in _q_last(conn)[0]


@pytest.mark.parametrize("back_after", [timedelta(seconds=60), timedelta(minutes=5)])
def test_a_lone_signal_edited_away_and_back_is_never_a_second_signal(conn, back_after):  # noqa: F811
    """A message with a signals row of its own -- VOIDED here, withdrawn by
    its edit into chatter -- is never given a second one, within the grace
    or not: edits never re-open a withdrawn call. The alert asks the user
    to trade it by hand, CRITICAL."""
    _store(conn, 4001, SIGNAL_TEXT)
    _edit(conn, 4001, "Good morning traders", POSTED + timedelta(seconds=30))
    back = _edit(conn, 4001, SIGNAL_TEXT, POSTED + back_after)
    assert back.outcome == "reclassified"
    assert _q(conn, "SELECT signal_id, state FROM signals") == [(1, "VOIDED")]
    assert _queued(conn) == []
    assert _alert_rows(conn)[-1][:2] == ("CRITICAL", edits_repo.EDIT_CREATED_SIGNAL)
    assert BY_HAND in _q_last(conn)[0]


@pytest.mark.parametrize("where", ["same-channel", "other-channel"])
def test_chatter_edited_into_a_withdrawn_call_after_the_grace_is_not_linked(conn, where):  # noqa: F811
    """The call was withdrawn (its only post deleted, nothing executed).
    Chatter edited into it past the grace is no copy of a live call: not
    linked, nothing queued, and the alert asks the user to trade it by
    hand if it is real -- CRITICAL."""
    channel = VIP if where == "same-channel" else RELAY
    message_id = 4003 if channel is VIP else 7003
    _store(conn, 4001, SIGNAL_TEXT)
    edits_repo.apply_deletion(
        conn, channel_cfg=VIP, message_id=4001, now_utc=POSTED + timedelta(seconds=20),
        accounts=ACCOUNTS, detected_by="event",
    )
    conn.commit()
    _store(conn, message_id, "chat", channel=channel, after=timedelta(seconds=10))
    result = _edit(
        conn, message_id, SIGNAL_TEXT, POSTED + timedelta(minutes=5), channel=channel,
        posted=POSTED + timedelta(seconds=10),
    )
    assert result.outcome == "reclassified"
    assert _q(
        conn, "SELECT relay_of_signal_id FROM messages WHERE channel_id = ? AND message_id = ?",
        channel.channel_id, message_id,
    ) == [(None,)]
    assert _queued(conn) == []
    assert _alert_rows(conn)[-1][:2] == ("CRITICAL", edits_repo.EDIT_CREATED_SIGNAL)
    assert _q_last(conn)[0].split(" [found by")[0].endswith(BY_HAND[:-1] + ".")


def test_a_withdrawn_post_edited_back_while_the_same_call_is_live_elsewhere_is_a_warning(conn):  # noqa: F811
    """A post with its own withdrawn signal, edited back into its call
    while another live signal shows that call (posted again with another
    stop -- another trade, so no replacement link): the alert names the
    live signal and says NOT to trade it by hand -- a WARNING, as it asks
    nothing of the user."""
    other_stop = SIGNAL_TEXT.replace("4470", "4465")
    _store(conn, 4001, SIGNAL_TEXT)
    _edit(conn, 4001, "Good morning traders", POSTED + timedelta(minutes=3))
    _store(conn, 4020, other_stop, after=timedelta(minutes=4))
    assert _q(conn, "SELECT signal_id, state FROM signals ORDER BY 1") == [
        (1, "VOIDED"), (2, "NEW")
    ]
    back = _edit(conn, 4001, SIGNAL_TEXT, POSTED + timedelta(minutes=6))
    assert back.outcome == "reclassified"
    assert _alert_rows(conn)[-1][:2] == ("WARNING", edits_repo.EDIT_CREATED_SIGNAL)
    detail = _q_last(conn)[0]
    assert "signal 2" in detail and "do NOT trade it by hand as well" in detail
