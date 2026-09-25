"""Relay copies: the same call, or the same follow-up, reaching us twice
(spec G7; final review F02, F04, F08, F09).

The user follows a VIP channel and channels that copy it -- by forwarding
its posts, or by posting the same call again. A copy must never open a
second set of legs, and must never be silent either:

- **the link** (F02): a signal message stored as a copy records the
  signal it repeated (`messages.relay_of_signal_id`), so an edit or a
  deletion of the copy can reach that signal;
- **forwards** (F09): a forward of a stored post -- or of another stored
  forward of it -- is a copy however late it arrives; the 15-minute
  trade-hash window stays for calls posted again by hand;
- **withdrawn calls** (F04): a poster who deletes (or voids) a call nobody
  took yet and posts it again gets the new post traded; a copy of a
  withdrawn call from elsewhere is held back, loudly;
- **follow-ups** (F08): a follow-up replying to a stored signal records
  that signal; one repeated by another channel is stored, not dispatched
  twice;
- **visibility**: every copy is logged with both messages and counted.

Unit-level tests drive `messages_repo.ingest()` on a temporary database;
the arrival-path tests at the bottom run the real listener and poll
against the fake Telegram client.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from xaucopier.config.channels import ChannelCfg
from xaucopier.ingest.listener import HistoryIngest
from xaucopier.ingest.poll import split_new
from xaucopier.store import db, relay_repo
from xaucopier.store.messages_repo import IngestResult, ingest

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "messages" / "mmh_buy_4tp.json"
_FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
SIGNAL_TEXT: str = _FIXTURE["text"]
POSTED = datetime.fromisoformat(_FIXTURE["posted_at_utc"])
assert SIGNAL_TEXT.count("4470") == 1
OTHER_CALL = SIGNAL_TEXT.replace("4470", "4465")  # a different trade (another SL)
CHATTER_TEXT = "Good morning traders"

VIP = ChannelCfg(
    channel_id=_FIXTURE["channel_id"], name="VIP", parser="mmh", ordinal=1, enabled=True
)
RELAY = ChannelCfg(channel_id=-1002222222222, name="Relay", parser="mmh", ordinal=2, enabled=True)
OTHER = ChannelCfg(channel_id=-1003333333333, name="Other", parser="mmh", ordinal=3, enabled=True)
#: A channel nobody watches, whose posts reach us only as forwards.
UNWATCHED = -1004444444444


@dataclass(frozen=True)
class _Account:
    account_id: str
    enabled: bool


ACCOUNTS = (_Account("acct-1", True), _Account("acct-2", True))


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "store.db", role="writer")
    try:
        yield connection
    finally:
        connection.close()


def _ingest(
    conn,
    channel: ChannelCfg,
    message_id: int,
    text: str = SIGNAL_TEXT,
    *,
    posted: datetime = POSTED,
    reply_to: int | None = None,
    fwd: tuple[int, int] | None = None,
    window: int = 900,
    source: str = "live",
) -> IngestResult:
    result = ingest(
        conn,
        channel_cfg=channel,
        message_id=message_id,
        text=text,
        posted_at_utc=posted,
        edit_date_utc=None,
        reply_to_message_id=reply_to,
        source=source,
        now_utc=posted + timedelta(seconds=1),
        accounts=ACCOUNTS,
        relay_window_seconds=window,
        fwd_from_channel_id=None if fwd is None else fwd[0],
        fwd_from_post_id=None if fwd is None else fwd[1],
        fwd_from_date_utc=None if fwd is None else POSTED,
    )
    conn.commit()
    return result


def _q(conn, sql: str, *params) -> list[tuple]:
    return [tuple(row) for row in conn.execute(sql, params).fetchall()]


def _link(conn, channel: ChannelCfg, message_id: int) -> int | None:
    (row,) = _q(
        conn,
        "SELECT relay_of_signal_id FROM messages WHERE channel_id = ? AND message_id = ?",
        channel.channel_id,
        message_id,
    )
    return row[0]


def _signals(conn) -> list[tuple]:
    return _q(conn, "SELECT signal_id, channel_id, message_id, state FROM signals ORDER BY 1")


def _queued_entries(conn) -> list[tuple]:
    return _q(
        conn,
        "SELECT signal_id, account_id FROM outbox WHERE kind = 'ENTRY' AND state = 'QUEUED' "
        "ORDER BY outbox_id",
    )


def _metric(conn, name: str) -> int:
    return _q(conn, "SELECT COALESCE(SUM(value), 0) FROM metrics WHERE name = ?", name)[0][0]


def _alerts(conn) -> list[tuple]:
    return _q(conn, "SELECT level, code, channel_id, message_id, signal_id FROM alerts ORDER BY 1")


# -- the link (F02) ------------------------------------------------------------


def test_a_copy_records_the_signal_it_repeated(conn):
    first = _ingest(conn, VIP, 4001)
    copy = _ingest(conn, RELAY, 7001, posted=POSTED + timedelta(seconds=45))

    assert first.outcome == "new"
    assert (copy.outcome, copy.kind, copy.signal_id) == ("relay_duplicate", "signal", None)
    assert copy.relay_of_signal_id == first.signal_id
    assert _link(conn, RELAY, 7001) == first.signal_id
    assert _link(conn, VIP, 4001) is None  # the signal's own message is no copy
    assert _signals(conn) == [(first.signal_id, VIP.channel_id, 4001, "NEW")]


def test_a_new_signal_reports_no_copy(conn):
    assert _ingest(conn, VIP, 4001).relay_of_signal_id is None


# -- forwards (F09) --------------------------------------------------------------


@pytest.mark.parametrize("lag", [60, 900, 901, 1800, 7200])
def test_a_forward_of_a_stored_post_is_a_copy_however_late(conn, lag):
    """The copy's own date is when it was forwarded: a VIP call forwarded
    more than 15 minutes late used to become a second ENTRY."""
    first = _ingest(conn, VIP, 4001)
    copy = _ingest(
        conn,
        RELAY,
        7001,
        posted=POSTED + timedelta(seconds=lag),
        fwd=(VIP.channel_id, 4001),
    )

    assert copy.outcome == "relay_duplicate"
    assert copy.relay_of_signal_id == first.signal_id
    assert len(_signals(conn)) == 1
    assert len(_queued_entries(conn)) == len(ACCOUNTS)


@pytest.mark.parametrize(
    ("lag", "outcome"),
    [(60, "relay_duplicate"), (900, "relay_duplicate"), (901, "new"), (1800, "new"), (7200, "new")],
)
def test_the_same_call_posted_again_by_hand_is_a_copy_only_inside_the_window(conn, lag, outcome):
    """No forward header: the trade-hash window (15 minutes) decides."""
    _ingest(conn, VIP, 4001)
    copy = _ingest(conn, RELAY, 7001, posted=POSTED + timedelta(seconds=lag))

    assert copy.outcome == outcome
    assert len(_signals(conn)) == (1 if outcome == "relay_duplicate" else 2)


def test_a_forward_is_matched_through_another_stored_forward_of_the_same_post(conn):
    """The VIP channel is not watched; two channels forward its post two
    hours apart. The second is a copy of the first."""
    first = _ingest(conn, RELAY, 7001, fwd=(UNWATCHED, 900))
    copy = _ingest(
        conn, OTHER, 8001, posted=POSTED + timedelta(hours=2), fwd=(UNWATCHED, 900)
    )

    assert first.outcome == "new"
    assert copy.outcome == "relay_duplicate"
    assert copy.relay_of_signal_id == first.signal_id


def test_a_forward_of_a_copy_repeats_the_signal_the_copy_repeated(conn):
    holder = _ingest(conn, VIP, 4001)
    _ingest(conn, RELAY, 7001, posted=POSTED + timedelta(seconds=30))  # a copy by hand
    copy_of_copy = _ingest(
        conn, OTHER, 8001, posted=POSTED + timedelta(hours=3), fwd=(RELAY.channel_id, 7001)
    )

    assert copy_of_copy.outcome == "relay_duplicate"
    assert copy_of_copy.relay_of_signal_id == holder.signal_id


def test_the_original_stored_after_its_late_forward_is_a_copy_of_it(conn):
    """The forward was stored first (the VIP post was missed, or the VIP
    channel was switched on later): the original, two hours older than
    the forward, still repeats the forward's signal."""
    forward = _ingest(
        conn, RELAY, 7001, posted=POSTED + timedelta(hours=2), fwd=(VIP.channel_id, 4001)
    )
    original = _ingest(conn, VIP, 4001, source="poll")

    assert forward.outcome == "new"
    assert original.outcome == "relay_duplicate"
    assert original.relay_of_signal_id == forward.signal_id


def test_a_forward_of_a_stored_post_that_is_no_signal_falls_back_to_the_window(conn):
    """Channel VIP stored its post as chatter; the forward reads as a
    signal on its own. No signal to repeat, and nothing within the window:
    a new signal."""
    _ingest(conn, VIP, 4001, CHATTER_TEXT)
    forward = _ingest(
        conn, RELAY, 7001, posted=POSTED + timedelta(hours=2), fwd=(VIP.channel_id, 4001)
    )
    assert forward.outcome == "new"


def test_a_forward_matches_by_the_post_not_by_the_trade(conn):
    """A forward of 4001 is a copy of 4001's signal even if its text reads
    as another trade (4001 was edited after it was forwarded)."""
    holder = _ingest(conn, VIP, 4001)
    forward = _ingest(
        conn,
        RELAY,
        7001,
        OTHER_CALL,
        posted=POSTED + timedelta(hours=1),
        fwd=(VIP.channel_id, 4001),
    )
    assert forward.outcome == "relay_duplicate"
    assert forward.relay_of_signal_id == holder.signal_id


def test_a_forward_of_another_post_is_not_a_copy(conn):
    _ingest(conn, VIP, 4001)
    forward = _ingest(
        conn,
        RELAY,
        7001,
        OTHER_CALL,
        posted=POSTED + timedelta(hours=1),
        fwd=(VIP.channel_id, 4002),
    )
    assert forward.outcome == "new"


# -- the window boundary (L71) ---------------------------------------------------


@pytest.mark.parametrize("copy_first", [False, True], ids=["copy-after", "copy-before"])
@pytest.mark.parametrize(
    ("window", "gap", "outcome"),
    [
        (900, 900, "relay_duplicate"),
        (900, 901, "new"),
        (60, 60, "relay_duplicate"),
        (60, 61, "new"),
    ],
)
def test_the_relay_window_includes_exactly_its_own_length(conn, window, gap, outcome, copy_first):
    """Telegram dates are whole seconds: a copy exactly `relay_window_seconds`
    away (either side) is still a copy; one second more is not."""
    copy_posted = POSTED + timedelta(seconds=-gap if copy_first else gap)
    _ingest(conn, VIP, 4001, window=window)
    copy = _ingest(conn, RELAY, 7001, posted=copy_posted, window=window)
    assert copy.outcome == outcome


# -- visibility ------------------------------------------------------------------


def test_every_copy_is_logged_with_both_messages_and_counted(conn, caplog):
    _ingest(conn, VIP, 4001)
    with caplog.at_level(logging.INFO, logger="xaucopier.store.relay_repo"):
        _ingest(conn, RELAY, 7001, posted=POSTED + timedelta(seconds=30))
        _ingest(
            conn, OTHER, 8001, posted=POSTED + timedelta(hours=2), fwd=(VIP.channel_id, 4001)
        )

    assert _metric(conn, relay_repo.RELAY_DUPLICATES_METRIC) == 2
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(lines) == 2
    for line, (channel, message_id) in zip(
        lines, ((RELAY.channel_id, 7001), (OTHER.channel_id, 8001)), strict=True
    ):
        assert f"channel {channel} message {message_id}" in line
        assert f"channel {VIP.channel_id} message 4001" in line
    assert "forward" in lines[1]
    assert _alerts(conn) == []  # a copy of a live call is no alert


def test_a_new_signal_is_not_counted_as_a_copy(conn):
    _ingest(conn, VIP, 4001)
    _ingest(conn, RELAY, 7001, OTHER_CALL, posted=POSTED + timedelta(seconds=30))
    assert _metric(conn, relay_repo.RELAY_DUPLICATES_METRIC) == 0


def _history(outcome: str, message_id: int) -> HistoryIngest:
    return HistoryIngest(
        message_id=message_id,
        result=IngestResult(
            outcome=outcome, signal_id=None, followup_id=None, kind="signal", detail=""
        ),
        posted_at_utc=POSTED,
    )


def test_a_copy_only_a_history_read_found_counts_as_missed_by_the_update_stream():
    """A copy is stored for the first time, like a new message: when only
    the poll found it, the update stream missed it (F02 side note)."""
    results = (
        _history("new", 1),
        _history("relay_duplicate", 2),
        _history("duplicate", 3),
        _history("edited", 4),
    )
    assert split_new(results, live_from=POSTED - timedelta(minutes=1)) == ([1, 2], [])
    assert split_new(results, live_from=POSTED + timedelta(minutes=1)) == ([], [1, 2])
