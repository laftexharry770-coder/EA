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


# -- entry-shaped text that failed the channel parser (final review F07) ---------

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "messages" / "mmh_buy_4tp.json").read_text(
        encoding="utf-8"
    )
)
FIXTURE_TEXT: str = _FIXTURE["text"]
FIXTURE_HEADER = "XAUUSD | BUY SIGNAL"
assert FIXTURE_TEXT.count(FIXTURE_HEADER) == 1

#: Common gold entry layouts that are not this channel's template. Before
#: F07, the first five read as AMEND_SL follow-ups of the entry's own stop.
OTHER_LAYOUTS = [
    "BUY GOLD 4480\nSL: 4470\nTP: 4490",
    "Gold Sell Now @4502\nSL:4510\nTP:4495",
    "XAUUSD SELL\nEntry 4502\nStop Loss 4510\nTake Profit 4490",
    "GOLD BUY LIMIT 4480\nSL = 4470\nTP = 4495",
    "XAUUSD BUY @ 4480\nStoploss: 4470\nTakeprofit: 4490",
    # No gold alias: a side word, a stop and a target are enough.
    "BUY 4480\nSL 4470\nTP 4490",
    "SELL NOW 4502\nsl: 4510\ntp1: 4495",
    "SELL 4502\nStop Loss 4510\nTake Profit 4490",
    # No target: a gold alias, a side word and a stop are enough -- read
    # as a follow-up, "SL: 4470" would move a live trade's stop.
    "XAUUSD BUY NOW 4480\nSL: 4470",
    "GOLD SELL 4502 SL 4510",
    "XAU BUY 4480\nSL 4470",
]

#: The most likely template change: only the header differs.
CHANGED_HEADERS = [
    "XAUUSD BUY SIGNAL",
    "XAUUSD | BUY",
    "XAUUSD BUY NOW",
    "GOLD | BUY LIMIT",
    "XAUUSD - BUY SIGNAL",
    "XAU/USD | BUY SIGNAL",
    # Not an instrument's name: the channel's own header, reworded. The
    # body carries no gold alias, so only the header can tell -- and a
    # word that names no instrument is no reason to call it "not gold".
    "VIP | BUY SIGNAL",
    "SCALP | BUY SIGNAL",
    "GC | BUY SIGNAL",
]


@pytest.mark.parametrize("text", OTHER_LAYOUTS)
def test_an_entry_in_another_layout_is_parse_failed_never_a_followup(text):
    r = _route(text)
    assert r.kind == "parse_failed", r
    assert r.followup is None and r.signal is None


@pytest.mark.parametrize("header", CHANGED_HEADERS)
def test_the_fixture_under_a_changed_header_is_parse_failed(header):
    r = _route(FIXTURE_TEXT.replace(FIXTURE_HEADER, header))
    assert r.kind == "parse_failed", r
    assert r.followup is None and r.signal is None


def test_the_real_fixture_still_routes_to_signal():
    assert _route(FIXTURE_TEXT).kind == "signal"
    assert _route(FIXTURE_TEXT.replace(FIXTURE_HEADER, "XAUUSD | BUY SIGNAL NOW")).kind == "signal"


#: Every follow-up phrase the tests use: each still routes to a follow-up.
FOLLOWUP_PHRASES = [
    "Move SL to BE", "Close half now", "SL to 4460", "TP2 to 4550", "cancel the order",
    "SL to BE", "sl to breakeven", "Move SL to entry", "BREAKEVEN NOW",
    "close half", "Close 50%", "take half off", "close half now",
    "close all", "CLOSE ALL NOW", "close everything", "Close all now",
    "cancel the pending order", "SL to 4461", "TP2: 4550", "move TP1 to 4500",
    "tp to 4500",
    # A side word and a gold alias, but "SL to" is an instruction, not a value.
    "XAUUSD BUY running: move SL to 4475",
]


@pytest.mark.parametrize("text", FOLLOWUP_PHRASES)
def test_every_follow_up_phrase_still_routes_to_a_followup(text):
    assert _route(text).kind == "followup"


@pytest.mark.parametrize(
    "text",
    [
        "Good morning traders",
        "BUY now, don't miss it",  # a side word, no stop value
        "SL 4470 was hit, next one soon",  # a stop value, no side word
        "we BUY the dip\nTP: 4490",  # side and target, no stop value
        "SL: 4470\nTP: 4490",  # stop and target, no side word
    ],
)
def test_text_missing_a_side_or_a_stop_value_is_not_entry_shaped(text):
    assert _route(text).kind in ("skip", "followup")
    assert _route(text).kind != "parse_failed"


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        # Another instrument in the channel's own header shape: not gold, no alarm.
        ("US30 | BUY SIGNAL\n44800-44850\nSL: 44700\nTP1: 44900", "skip"),
        ("NAS100 | SELL\nSL: 19900\nTP: 19700", "skip"),
        ("NAS100 | SELL\nSL: 19900", "skip"),  # never a gold follow-up either
        # An instrument's name: a symbol with a digit, a currency, metal or
        # coin pair, or a well-known market's name.
        ("EURUSD | BUY SIGNAL\n1.1650-1.1655\nSL: 1.1620\nTP1: 1.1700", "skip"),
        ("XAGUSD | SELL SIGNAL\n52.10-52.20\nSL: 52.60\nTP1: 51.50", "skip"),
        ("BTCUSDT | BUY SIGNAL\n98000-98200\nSL: 97000\nTP1: 99500", "skip"),
        ("SILVER | BUY SIGNAL\n52.10-52.20\nSL: 51.60\nTP1: 52.80", "skip"),
        ("USOIL | SELL SIGNAL\n66.10-66.20\nSL: 66.90\nTP1: 65.20", "skip"),
        ("GER40 | BUY SIGNAL\n24100-24120\nSL: 24000\nTP1: 24300", "skip"),
        # ...but a word that names no instrument is the header reworded.
        ("VIP | BUY SIGNAL\n4480-4485\nSL: 4470\nTP1: 4490", "parse_failed"),
        ("SIGNAL | SELL\nSL: 4510\nTP: 4490", "parse_failed"),
        # ...but a symbol containing a gold alias is gold,
        ("GOLDUSD | BUY\n4480-4485\nSL: 4470\nTP1: 4490", "parse_failed"),
        ("XAUEUR | SELL\nSL: 4110\nTP1: 4090", "parse_failed"),
        # ...a gold alias anywhere makes it gold,
        ("US30 | BUY GOLD now\nSL: 4470\nTP: 4490", "parse_failed"),
        # ...and another layout naming no symbol is a gold entry.
        ("US30 BUY 44800\nSL: 44700\nTP: 44900", "parse_failed"),
    ],
)
def test_another_instruments_signal_in_the_channels_header_shape_stays_a_skip(text, kind):
    assert _route(text).kind == kind


# -- FF3: a number in the header is an instrument only when index-like ----------
#
# Controller decision: a symbol with digits names another market only when
# it is index-like -- at least 2 letters, then at least 2 digits, the
# letters a known index or market name (US30, GER40, NAS100, SPX500,
# JP225, UK100, HK50). A timeframe tag (H1, M15, D1) or a numbered tag
# (VIP1, TOP1, SIGNAL1, SCALP1, VIP2025) over the channel's own body is
# the header reworded: parse_failed, counted, and loud after three.


@pytest.mark.parametrize(
    "header",
    ["H1 | BUY SIGNAL", "H4 | BUY SIGNAL", "M5 | BUY SIGNAL", "M15 | BUY SIGNAL",
     "M30 | BUY SIGNAL", "D1 | BUY SIGNAL", "W1 | BUY SIGNAL", "VIP1 | BUY SIGNAL",
     "TOP1 | BUY SIGNAL", "VIP2025 | BUY SIGNAL", "SIGNAL1 | BUY SIGNAL",
     "SCALP1 | BUY SIGNAL", "VIP25 | BUY SIGNAL"],
)
def test_a_timeframe_or_numbered_tag_over_the_fixture_body_is_parse_failed(header):
    r = _route(FIXTURE_TEXT.replace(FIXTURE_HEADER, header))
    assert r.kind == "parse_failed", r


@pytest.mark.parametrize(
    "header",
    ["US30 | BUY SIGNAL", "NAS100 | BUY SIGNAL", "GER40 | BUY SIGNAL", "SPX500 | BUY SIGNAL",
     "JP225 | BUY SIGNAL", "UK100 | BUY SIGNAL", "HK50 | BUY SIGNAL", "US500 | BUY SIGNAL",
     "NASDAQ100 | BUY SIGNAL"],
)
def test_an_index_over_the_fixture_body_is_another_instruments_skip(header):
    r = _route(FIXTURE_TEXT.replace(FIXTURE_HEADER, header))
    assert r.kind == "skip", r


@pytest.mark.parametrize(
    "header",
    ["US30.CASH | BUY SIGNAL", "NAS100.R | BUY SIGNAL", "GER40_CASH | BUY SIGNAL",
     "US30-CASH | BUY SIGNAL", "N225 | BUY SIGNAL", "J225 | BUY SIGNAL", "F40 | BUY SIGNAL",
     "RUSSELL2000 | BUY SIGNAL", "N225.CASH | BUY SIGNAL"],
)
def test_an_index_with_a_brokers_suffix_or_a_one_letter_name_is_another_instruments_skip(
    header,
):
    """Final fix round 2 (the re-check's FF3 nit): an index written with a
    dot, underscore or dash before its broker's suffix, the common
    one-letter index symbols and RUSSELL2000 are another market -- skipped,
    not counted as the channel's template changing."""
    r = _route(FIXTURE_TEXT.replace(FIXTURE_HEADER, header))
    assert r.kind == "skip", r


@pytest.mark.parametrize(
    "header",
    ["XAUUSD.m | BUY SIGNAL", "GOLD.CASH | BUY SIGNAL", "VIP1.A | BUY SIGNAL",
     "H1.CASH | BUY SIGNAL", "N1 | BUY SIGNAL", "F4 | BUY SIGNAL", "J22 | BUY SIGNAL"],
)
def test_a_suffix_never_makes_a_tag_or_gold_another_instrument(header):
    """Controls: a separator before a suffix does not turn gold, a numbered
    tag or a timeframe into another market, and only the listed one-letter
    index symbols are one."""
    r = _route(FIXTURE_TEXT.replace(FIXTURE_HEADER, header))
    assert r.kind != "skip", r


# -- FF4: the two instrument guards, pinned -------------------------------------


@pytest.mark.parametrize("header", ["BTCPRO | BUY SIGNAL", "EURONEWS | BUY SIGNAL"])
def test_a_word_that_only_starts_with_a_currency_is_no_pair(header):
    """Both halves must be codes: "BTC" + "PRO" is no pair."""
    r = _route(FIXTURE_TEXT.replace(FIXTURE_HEADER, header))
    assert r.kind == "parse_failed", r


def test_gold_with_a_number_in_the_header_is_gold(monkeypatch):
    """A symbol containing a gold alias is never another instrument -- even
    one the instrument lists would name (here a gold coin added to them)."""
    from xaucopier.parsing import router

    assert _route(FIXTURE_TEXT.replace(FIXTURE_HEADER, "GOLD1 | BUY SIGNAL")).kind == "parse_failed"
    monkeypatch.setattr(router, "_PAIR_CODES", router._PAIR_CODES | {"XAUT"})
    r = _route(FIXTURE_TEXT.replace(FIXTURE_HEADER, "XAUTUSD | BUY SIGNAL"))
    assert r.kind == "parse_failed", r


@pytest.mark.parametrize("header", ["US30CASH | BUY SIGNAL", "NAS100M | BUY SIGNAL"])
def test_an_index_with_a_brokers_suffix_is_another_instruments_skip(header):
    assert _route(FIXTURE_TEXT.replace(FIXTURE_HEADER, header)).kind == "skip"
