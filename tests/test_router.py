"""Router tests -- these encode the Plan 1 final-review hazard directly.

An entry signal whose template breaks (header matched, structure broken) is
PARSE_FAILED, not NOT_A_SIGNAL. If the caller fell through to
parse_followup on PARSE_FAILED, a broken template could be read as an
AMEND_SL against a live trade. The arbitration rule in router.route()
exists solely to make that impossible -- see the module docstring there.

skip and parse_failed must also stay distinct outcomes: only parse_failed
feeds the consecutive-failure counter that trips PAUSE_NEW (spec 6.2).
"""
from datetime import datetime, UTC

from xaucopier.config.channels import ChannelCfg
from xaucopier.domain.signal import FollowUpOp, Side
from xaucopier.parsing.router import route

POSTED = datetime(2026, 9, 5, 11, 5, tzinfo=UTC)

CFG = ChannelCfg(
    channel_id=-1001111111111,
    name="MARKET MASTERY HUB / COMMUNITY",
    parser="mmh",
    ordinal=1,
    enabled=True,
)


def _route(text: str, reply_to: int | None = None):
    return route(
        text,
        channel_cfg=CFG,
        message_id=4001,
        posted_at_utc=POSTED,
        reply_to_message_id=reply_to,
    )


def test_valid_signal_routes_to_signal():
    text = "XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4470\nTP1: 4490\nTP2: 4495"
    r = _route(text)
    assert r.kind == "signal"
    assert r.signal is not None
    assert r.signal.side is Side.BUY
    assert r.signal.zone.lo == 4480.0 and r.signal.zone.hi == 4485.0
    assert r.followup is None


def test_broken_entry_signal_never_becomes_a_followup():
    # header matched, structure broken -> parse_failed, and NOT an AMEND_SL
    text = "XAUUSD | BUY SIGNAL\n4480-4485\nTP1: 4490"   # no SL
    r = _route(text)
    assert r.kind == "parse_failed"
    assert r.followup is None


def test_signal_missing_the_word_signal_does_not_become_an_amend():
    text = "XAUUSD | BUY\n4480-4485\nSL: 4470\nTP1: 4490\nTP2: 4495"
    r = _route(text)
    assert r.kind in ("skip", "parse_failed")
    assert r.followup is None


def test_real_followup_routes_to_followup():
    r = _route("close all")
    assert r.kind == "followup"
    assert r.followup is not None
    assert r.followup.op is FollowUpOp.CLOSE_ALL
    assert r.signal is None


def test_amend_tp_still_routes_to_followup():
    r = _route("TP2 to 4550")
    assert r.kind == "followup"
    assert r.followup is not None
    assert r.followup.op is FollowUpOp.AMEND_TP
    assert r.followup.tp_index_hint == 2
    assert r.followup.price_hint == 4550.0


def test_chatter_routes_to_skip():
    r = _route("Good morning traders")
    assert r.kind == "skip"
    assert r.signal is None
    assert r.followup is None


def test_us30_signal_routes_to_skip():
    # Non-gold: a well-formed index signal is never mapped onto gold, and
    # its "SIGNAL" wording also blocks the follow-up guard, so it skips.
    text = "US30 | BUY SIGNAL\n44800-44850\nSL: 44700\nTP1: 44900"
    r = _route(text)
    assert r.kind == "skip"
    assert r.signal is None
    assert r.followup is None


def test_skip_and_parse_failed_are_distinct_outcomes():
    # This is the load-bearing distinction: only parse_failed feeds the
    # consecutive-failure counter (spec 6.2). If chatter counted as a parse
    # failure, three ordinary messages would trip PAUSE_NEW.
    chatter = _route("Good morning traders")
    broken = _route("XAUUSD | BUY SIGNAL\n4480-4485\nTP1: 4490")
    assert chatter.kind == "skip"
    assert broken.kind == "parse_failed"
    assert chatter.kind != broken.kind


def test_reply_to_message_id_is_carried_through_to_followup():
    r = _route("close all", reply_to=9)
    assert r.followup is not None
    assert r.followup.reply_to_message_id == 9
