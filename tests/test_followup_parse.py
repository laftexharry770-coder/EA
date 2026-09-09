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


@pytest.mark.parametrize("text", [
    "don't close all",
    "do not close all",
    "never close all",
    "please dont close all yet",
    "don't cancel the pending order",
    "don't move sl to be",
    "don\u2019t close half",
])
def test_negated_instruction_returns_none(text):
    # A negated instruction must never be executed as if it were an order:
    # loud failure (defer to a human) beats guessing wrong on live money.
    assert _p(text) is None


def test_amend_tp_captures_index_and_price():
    f = _p("TP2: 4550")
    assert f.op is FollowUpOp.AMEND_TP
    assert f.price_hint == 4550.0
    assert f.tp_index_hint == 2


def test_amend_tp_move_variant_captures_index():
    f = _p("move TP1 to 4500")
    assert f.op is FollowUpOp.AMEND_TP
    assert f.price_hint == 4500.0
    assert f.tp_index_hint == 1


def test_amend_tp_without_index_leaves_hint_none():
    f = _p("tp to 4500")
    assert f.op is FollowUpOp.AMEND_TP
    assert f.price_hint == 4500.0
    assert f.tp_index_hint is None


# --- I5: the entry-signal guard must be structural, not the literal word
# "SIGNAL". A template that drops that one word produced a message which
# channel_mmh rejects and parse_followup then reads as an AMEND_SL against
# whatever trade is live -- under the obvious "try parse, fall back to
# parse_followup" routing, a brand-new entry becomes "move the live stop".

def test_a_signal_missing_the_word_signal_is_not_a_followup():
    text = "XAUUSD | BUY\n4480-4485\nSL: 4470\nTP1: 4490\nTP2: 4495"
    assert _p(text) is None

def test_a_bare_entry_zone_line_blocks_the_parse():
    text = "BUY\n4480-4485\nSL: 4470"
    assert _p(text) is None

def test_an_sl_value_plus_an_indexed_tp_blocks_the_parse():
    # No zone line at all, but the SL-value + indexed-TP pair is entry
    # structure, not a follow-up command.
    text = "GOLD BUY\nSL: 4470\nTP1: 4490\nTP2: 4495"
    assert _p(text) is None

def test_an_en_dash_entry_zone_line_blocks_the_parse():
    text = "XAUUSD | BUY\n4480–4485\nSL: 4470\nTP1: 4490"
    assert _p(text) is None

def test_real_amend_sl_still_parses():
    f = _p("SL to 4461")
    assert f.op is FollowUpOp.AMEND_SL
    assert f.price_hint == 4461.0

def test_close_all_still_parses():
    assert _p("close all").op is FollowUpOp.CLOSE_ALL

def test_amend_tp_with_an_index_is_not_swallowed_by_the_guard():
    # The important false-positive control: one indexed TP, no SL value, so
    # this is NOT entry structure and must still reach AMEND_TP.
    f = _p("TP2 to 4550")
    assert f is not None
    assert f.op is FollowUpOp.AMEND_TP
    assert f.tp_index_hint == 2
    assert f.price_hint == 4550.0

def test_colon_form_amend_tp_is_not_swallowed_by_the_guard():
    f = _p("TP2: 4550")
    assert f is not None and f.op is FollowUpOp.AMEND_TP

def test_breakeven_still_parses():
    assert _p("SL to BE").op is FollowUpOp.SL_TO_BE
