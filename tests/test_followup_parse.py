from datetime import datetime, UTC
import pytest

from xaucopier.parsing.followups import parse_followup
from xaucopier.domain.signal import FollowUpOp

POSTED = datetime(2026, 9, 5, 11, 5, tzinfo=UTC)


def _p(text: str, reply_to: int | None = None):
    return parse_followup(text, channel_id=-1001, message_id=9,
                          posted_at_utc=POSTED, reply_to_message_id=reply_to)


@pytest.mark.parametrize("text", [
    "SL to BE", "sl to breakeven", "Move SL to entry", "BREAKEVEN NOW",
])
def test_breakeven_variants(text):
    assert _p(text).op is FollowUpOp.SL_TO_BE


@pytest.mark.parametrize("text", ["close half", "Close 50%", "take half off"])
def test_close_half_variants(text):
    assert _p(text).op is FollowUpOp.CLOSE_HALF


@pytest.mark.parametrize("text", ["close all", "CLOSE ALL NOW", "close everything"])
def test_close_all_variants(text):
    assert _p(text).op is FollowUpOp.CLOSE_ALL


def test_cancel_pending():
    assert _p("cancel the pending order").op is FollowUpOp.CANCEL_PENDING


def test_amend_sl_captures_price():
    f = _p("SL to 4461")
    assert f.op is FollowUpOp.AMEND_SL
    assert f.price_hint == 4461.0


def test_breakeven_beats_amend_when_both_could_match():
    # "SL to BE" must not be read as an amend with no price.
    assert _p("SL to BE").op is FollowUpOp.SL_TO_BE


def test_reply_id_is_carried_through():
    assert _p("close all", reply_to=4001).reply_to_message_id == 4001


def test_unrelated_chatter_returns_none():
    assert _p("great trade everyone \U0001F525") is None


def test_a_full_signal_is_not_a_followup():
    text = "XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4470\nTP1: 4490"
    assert _p(text) is None
