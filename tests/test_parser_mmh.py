import json
from datetime import datetime, UTC
from pathlib import Path
import pytest

from xaucopier.parsing.channel_mmh import parse
from xaucopier.domain.signal import Side
from xaucopier.domain.errors import RejectReason

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/messages/mmh_buy_4tp.json").read_text(encoding="utf-8")
)
POSTED = datetime(2026, 9, 5, 10, 45, tzinfo=UTC)

def _parse(text: str):
    return parse(text, channel_id=-1001111111111, message_id=4001, posted_at_utc=POSTED)

def test_parses_the_real_message():
    r = _parse(FIXTURE["text"])
    assert r.ok, r.detail
    assert r.signal.side is Side.BUY
    assert r.signal.symbol_alias == "XAUUSD"
    assert (r.signal.zone.lo, r.signal.zone.hi) == (4480.0, 4485.0)
    assert r.signal.sl == 4470.0

def test_parses_tp4_with_space_before_colon():
    r = _parse(FIXTURE["text"])
    assert r.signal.tps == (4490.0, 4495.0, 4500.0, 4505.0)

def test_tps_are_ordered_by_index_not_file_order():
    text = "XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4470\nTP3: 4500\nTP1: 4490\nTP2: 4495"
    r = _parse(text)
    assert r.signal.tps == (4490.0, 4495.0, 4500.0)

def test_parses_sell():
    text = "XAUUSD | SELL SIGNAL\n4485-4480\nSL: 4495\nTP1: 4470"
    r = _parse(text)
    assert r.signal.side is Side.SELL
    assert r.signal.tps == (4470.0,)

def test_missing_sl_is_hard_reject():
    text = "XAUUSD | BUY SIGNAL\n4480-4485\nTP1: 4490"
    r = _parse(text)
    assert r.ok is False
    assert r.reason is RejectReason.PARSE_FAILED

def test_missing_zone_is_hard_reject():
    text = "XAUUSD | BUY SIGNAL\nSL: 4470\nTP1: 4490"
    assert _parse(text).ok is False

def test_no_tps_is_hard_reject():
    text = "XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4470"
    assert _parse(text).ok is False

def test_non_gold_symbol_is_skipped():
    text = "US30 | BUY SIGNAL\n44800-44850\nSL: 44700\nTP1: 44900"
    r = _parse(text)
    assert r.ok is False
    # I1: a well-formed index signal is not a broken gold template. It is
    # skipped (spec 6.2), so it must not feed consecutive_parse_failures.
    assert r.reason is RejectReason.NOT_A_SIGNAL

def test_chatter_is_rejected_without_raising():
    r = _parse("Good morning traders, be patient today")
    assert r.ok is False
    # I1: ordinary channel chatter is certain and constant. Reporting it as
    # PARSE_FAILED trips the three-strike PAUSE_NEW safety stop on noise and
    # teaches the operator to ignore the one detector for a real template
    # change.
    assert r.reason is RejectReason.NOT_A_SIGNAL

def test_gold_header_with_no_sl_is_a_real_parse_failure():
    # I1's other side: the header matched, so this IS our template, and the
    # structure is broken. That must still count toward the failure streak.
    r = _parse("XAUUSD | BUY SIGNAL\n4480-4485\nTP1: 4490")
    assert r.ok is False
    assert r.reason is RejectReason.PARSE_FAILED

def test_gold_header_with_no_zone_is_a_real_parse_failure():
    r = _parse("XAUUSD | BUY SIGNAL\nSL: 4470\nTP1: 4490")
    assert r.reason is RejectReason.PARSE_FAILED

def test_gold_header_with_no_tps_is_a_real_parse_failure():
    r = _parse("XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4470")
    assert r.reason is RejectReason.PARSE_FAILED

def test_an_empty_message_is_skipped_not_failed():
    assert _parse("").reason is RejectReason.NOT_A_SIGNAL

def test_en_dash_zone_is_accepted():
    text = "XAUUSD | BUY SIGNAL\n4480–4485\nSL: 4470\nTP1: 4490"
    assert _parse(text).ok is True

def test_content_hash_is_stable_across_cosmetic_edits():
    a = _parse("XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4470\nTP1: 4490").signal
    b = _parse("\U0001F680 XAUUSD | BUY SIGNAL\n\U0001F4CD 4480-4485\nSL:  4470\nTP1: 4490").signal
    assert a.content_hash == b.content_hash

def test_content_hash_changes_when_sl_changes():
    a = _parse("XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4470\nTP1: 4490").signal
    b = _parse("XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4460\nTP1: 4490").signal
    assert a.content_hash != b.content_hash

def test_duplicate_sl_is_hard_reject():
    text = "XAUUSD | BUY SIGNAL\n4480-4485\nOld SL: 4470 -- update below\nNew SL: 4475\nTP1: 4490"
    r = _parse(text)
    assert r.ok is False

def test_duplicate_header_is_hard_reject():
    text = (
        "XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4470\nTP1: 4490\n"
        "XAUUSD | SELL SIGNAL\n4500-4505\nSL: 4510\nTP1: 4520"
    )
    r = _parse(text)
    assert r.ok is False

def test_duplicate_entry_zone_is_hard_reject():
    text = "XAUUSD | BUY SIGNAL\n4480-4485\n4500-4505\nSL: 4470\nTP1: 4490"
    r = _parse(text)
    assert r.ok is False

def test_duplicate_tp_index_is_hard_reject():
    text = "XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4470\nTP1: 4490\nTP1: 4491\nTP3: 4500"
    r = _parse(text)
    assert r.ok is False

def test_gapped_tp_indices_is_hard_reject():
    text = "XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4470\nTP1: 4490\nTP3: 4500"
    r = _parse(text)
    assert r.ok is False

def test_tp_indices_not_starting_at_one_is_hard_reject():
    text = "XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4470\nTP2: 4490\nTP3: 4500"
    r = _parse(text)
    assert r.ok is False

def test_two_concatenated_signal_blocks_is_hard_reject():
    text = (
        "XAUUSD | BUY SIGNAL\n"
        "4480-4485\n"
        "SL: 4470\n"
        "TP1: 4490\n"
        "TP2: 4495\n"
        "XAUUSD | SELL SIGNAL\n"
        "4500-4505\n"
        "SL: 4510\n"
        "TP1: 4520"
    )
    r = _parse(text)
    assert r.ok is False
