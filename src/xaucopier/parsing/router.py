"""Signal-first arbitration: routes one raw message to signal, followup,
skip, or parse_failed.

This closes the hazard the Plan 1 final review named: an entry signal whose
template breaks -- say the channel drops the word "SIGNAL" -- is correctly
rejected by the channel's signal parser. Under the naive "try parse(), fall
back to parse_followup()" routing, that same broken text is then read by
the follow-up parser as a legitimate instruction against a live trade
(observed case: it produced AMEND_SL price_hint=<the entry's own stop>).
That would turn a template change into "move the live position's stop."

The rule that prevents it, in order:

1. Run the channel's signal parser. `ok` -> kind="signal". Done.
2. PARSE_FAILED means the header matched and the structure then broke --
   this is exactly the hazard case above. Return kind="parse_failed" and
   stop. Never fall through to parse_followup from here.
3. NOT_A_SIGNAL means the message never claimed to be this channel's
   template -- but it may still be an entry in another layout, which is
   what a changed template looks like (final review F07): a header
   without its pipe, without the word SIGNAL, "BUY GOLD 4480 / SL: 4470 /
   TP: 4490". Such **entry-shaped** text is kind="parse_failed": it feeds
   the consecutive-failure counter, so a template change is a loud
   outage (spec 6.2), and it never becomes a follow-up -- read as one,
   "SL: 4470" in a brand-new entry would move a live trade's stop.
   Entry-shaped means a side word (BUY or SELL) and a stop *value*
   ("SL: 4470", "SL = 4470", "Stop Loss 4510", "Stoploss: 4470" -- not
   "SL to 4460", which is an instruction), together with a gold alias
   (XAUUSD, GOLD, XAU) or a target value ("TP: 4490", "TP1 4490", "Take
   Profit 4490"). One exception: a signal naming another instrument in
   the channel's own "SYMBOL | SIDE" header, with no gold alias anywhere
   ("US30 | BUY SIGNAL ...", "EURUSD | SELL ...", "SILVER | BUY ...") is
   not gold and not a changed template: it stays a skip, so a channel's
   index calls never raise a false alarm. The symbol must look like an
   instrument (an index's name and number such as "US30" or "NAS100", a
   currency / metal / coin pair, or a market's name): "VIP | BUY SIGNAL",
   "GC | BUY SIGNAL", a timeframe ("H1 | BUY SIGNAL") or a numbered tag
   ("VIP1 | BUY SIGNAL") over the channel's own body is the header
   reworded, and is parse_failed.
   When entry-shaped text ALSO reads as an instruction ("Close all GOLD
   BUY now, SL 4470 hit"), that follow-up rides along as
   `RouteResult.unqueued_followup` -- never queued, only reported, so a
   genuine instruction dropped by this rule is dropped loudly.
4. Anything else NOT_A_SIGNAL: only then is it worth asking whether it is
   a follow-up instruction: a hit is kind="followup", a miss is
   kind="skip".

skip and parse_failed must stay different outcomes: only parse_failed
feeds the consecutive-failure counter that trips PAUSE_NEW (spec 6.2). If
ordinary chatter counted as a parse failure, three unrelated messages
would trip the safety stop and train the operator to ignore it -- which
disarms the one detector that actually watches for a template change.

Pure: no I/O, no clock, no randomness (tests/test_purity.py enforces it).
`channel_cfg` is accepted structurally (see _ChannelCfgLike below) rather
than typed as config.channels.ChannelCfg: that module reads TOML from disk
and parsing/ must never import it, so router.py only ever needs the two
attributes a channel config always carries.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from xaucopier.domain.errors import RejectReason
from xaucopier.domain.signal import FollowUp, Signal
from xaucopier.parsing.followups import parse_followup
from xaucopier.parsing.normalize import normalize
from xaucopier.parsing.registry import parser_for

RouteKind = Literal["signal", "followup", "skip", "parse_failed"]

# -- entry shape (rule 3; final review F07) -------------------------------------
_NUM = r"\d+(?:\.\d+)?"
_SIDE_WORD = re.compile(r"\b(?:BUY|SELL)\b", re.I)
_GOLD_ALIAS = re.compile(r"\b(?:XAUUSD|GOLD|XAU)\b", re.I)
#: A stop given as a value -- "SL: 4470", "SL 4470", "SL = 4470", "SL @ 4470",
#: "Stop Loss 4510", "Stoploss: 4470" -- never "SL to 4460" (an instruction).
_STOP_VALUE = re.compile(rf"\b(?:SL|STOP\s*LOSS)\b\s*[:=@-]?\s*{_NUM}", re.I)
#: A target given as a value -- "TP: 4490", "TP1 4490", "TP2: 4495",
#: "Take Profit 4490", "Takeprofit: 4490".
_TARGET_VALUE = re.compile(rf"\b(?:TP\s*\d*|TAKE\s*PROFIT)\b\s*[:=@-]?\s*{_NUM}", re.I)
#: The channel's own "SYMBOL | SIDE" header shape, with its symbol -- a
#: broker's suffix after a dot, underscore or dash included ("US30.CASH",
#: "GER40_CASH"; final fix round 2).
_PIPE_HEADER = re.compile(
    r"\b([A-Z][A-Z0-9]{1,11}(?:[._-][A-Z0-9]{1,8})?)\s*\|\s*(?:BUY|SELL)\b", re.I
)
#: The codes a traded pair is made of -- currencies, metals other than gold,
#: coins and stablecoins: "EURUSD", "XAGUSD", "BTCUSDT" are two of them.
_PAIR_CODES = frozenset(
    {
        "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "SGD", "HKD", "NOK", "SEK",
        "DKK", "ZAR", "MXN", "TRY", "PLN", "CNH", "CNY", "HUF", "CZK", "INR",
        "XAG", "XPT", "XPD",
        "BTC", "ETH", "LTC", "XRP", "SOL", "BNB", "ADA", "DOGE", "DOT", "USDT", "USDC",
    }
)
#: Other markets known by a name rather than a pair or a number.
_MARKET_NAMES = frozenset(
    {
        "SILVER", "PLATINUM", "COPPER", "OIL", "USOIL", "UKOIL", "WTI", "BRENT", "CRUDE",
        "NGAS", "NATGAS", "NASDAQ", "NAS", "NDX", "DOW", "DJI", "SPX", "DAX", "FTSE",
        "NIKKEI", "DXY", "VIX", "BITCOIN", "ETHEREUM",
    }
)
#: An index's name and number -- "US30", "GER40", "NAS100", "SPX500",
#: "JP225", "UK100", "HK50": at least two letters, then at least two digits
#: (final fix FF3, controller decision). A timeframe ("H1", "M15", "D1")
#: has one letter; a numbered tag ("VIP1", "TOP1", "VIP2025") has letters
#: that name no index or market (`_INDEX_NAMES`, `_MARKET_NAMES`). A
#: broker's suffix may follow, after a dot, underscore or dash or not
#: ("US30CASH", "NAS100M", "US30.CASH", "NAS100.R", "GER40_CASH"; final
#: fix round 2).
_INDEX_LIKE = re.compile(r"([A-Z]{2,})(\d{2,})(?:[._-]?[A-Z0-9]*)")
#: The common index symbols with one letter -- Nikkei 225 ("N225",
#: "J225") and the CAC 40 ("F40") -- with a broker's suffix or not. Only
#: these: one letter and a number is otherwise a timeframe or a tag.
_ONE_LETTER_INDEX = re.compile(r"([A-Z]\d{2,})(?:[._-]?[A-Z0-9]*)")
_ONE_LETTER_INDEXES = frozenset({"N225", "J225", "F40"})
#: The letters of an index's symbol, as brokers and channels write them.
_INDEX_NAMES = frozenset(
    {
        "US", "USA", "USTEC", "UST", "NAS", "NDX", "SPX", "SP", "DJ", "DJI", "DOW", "WS",
        "GER", "DE", "DAX", "UK", "FTSE", "JP", "JPN", "NIK", "NIKKEI", "HK", "HSI", "AUS",
        "AU", "ASX", "FRA", "FR", "CAC", "EU", "EUSTX", "ESTX", "STOXX", "ESP", "ES", "IBEX",
        "IT", "ITA", "CHN", "CN", "CHINA", "SG", "SGP", "IND", "SA", "SWI", "SMI", "CH",
        "NL", "NETH", "AEX", "RUS", "RUT", "RTY", "TSX", "VIX", "RUSSELL",
    }
)


EntryShape = Literal["gold", "other_instrument"]


def entry_shape(text: str) -> EntryShape | None:
    """Whether `text`, which the channel's parser did not recognise, reads
    as a trade entry (rule 3 of the module docstring): a side word and a
    stop value, with a gold alias or a target value -- "gold". A side word
    and a stop value under another instrument's "SYMBOL | SIDE" header,
    with no gold alias anywhere, is "other_instrument": not gold, and no
    follow-up either. None for anything else."""
    clean = normalize(text)
    if not (_SIDE_WORD.search(clean) and _STOP_VALUE.search(clean)):
        return None
    gold = _GOLD_ALIAS.search(clean) is not None
    if not gold and _names_another_instrument(clean):
        return "other_instrument"
    if gold or _TARGET_VALUE.search(clean) is not None:
        return "gold"
    return None


def _names_another_instrument(clean: str) -> bool:
    """A "SYMBOL | SIDE" header whose symbol is another instrument's name
    (`_is_instrument`) and no gold alias (nor contains one: "XAUEUR",
    "GOLDUSD" are gold). "VIP | BUY", "SCALP | BUY", "GC | BUY" name no
    other instrument: the channel's header reworded, which is exactly the
    template change rule 3 must not let pass silently."""
    for match in _PIPE_HEADER.finditer(clean):
        symbol = match.group(1).upper()
        if "XAU" not in symbol and "GOLD" not in symbol and _is_instrument(symbol):
            return True
    return False


def _is_instrument(symbol: str) -> bool:
    """Whether a header's upper-cased `symbol` names a market: an index's
    name and number (`_INDEX_LIKE`: "US30", "NAS100", "GER40", with a
    broker's suffix or not -- never a timeframe such as "H1" or a numbered
    tag such as "VIP1"; and the one-letter "N225", "J225", "F40"), a pair of two
    `_PAIR_CODES` ("EURUSD", "XAGUSD", "BTCUSDT") or one of them alone
    ("BTC"), or a market's name (`_MARKET_NAMES`: "SILVER", "USOIL")."""
    index = _INDEX_LIKE.fullmatch(symbol)
    if index is not None:
        return index.group(1) in _INDEX_NAMES or index.group(1) in _MARKET_NAMES
    one_letter = _ONE_LETTER_INDEX.fullmatch(symbol)
    if one_letter is not None:
        return one_letter.group(1) in _ONE_LETTER_INDEXES
    if symbol in _PAIR_CODES or symbol in _MARKET_NAMES:
        return True
    return any(
        symbol[:cut] in _PAIR_CODES and symbol[cut:] in _PAIR_CODES
        for cut in range(3, len(symbol) - 2)
    )


class _ChannelCfgLike(Protocol):
    """The two fields router.route() actually needs from a channel config.

    Structural, not nominal: satisfied by config.channels.ChannelCfg
    without router.py importing that (impure) module.
    """
    channel_id: int
    parser: str


@dataclass(frozen=True, slots=True)
class RouteResult:
    kind: RouteKind
    signal: Signal | None = None
    followup: FollowUp | None = None
    detail: str = ""
    #: Only for an entry-shaped parse_failed (rule 3): the instruction
    #: `parse_followup` reads in it ("Close all GOLD BUY now, SL 4470 hit").
    #: NEVER queued -- entry-shaped text is no follow-up -- only reported,
    #: so an oddly worded genuine instruction is dropped loudly (ruling F07).
    unqueued_followup: FollowUp | None = None


def route(
    text: str,
    *,
    channel_cfg: _ChannelCfgLike,
    message_id: int,
    posted_at_utc: datetime,
    reply_to_message_id: int | None = None,
) -> RouteResult:
    parse = parser_for(channel_cfg.parser)
    result = parse(
        text,
        channel_id=channel_cfg.channel_id,
        message_id=message_id,
        posted_at_utc=posted_at_utc,
    )

    if result.ok:
        return RouteResult(kind="signal", signal=result.signal, detail=result.detail)

    if result.reason is RejectReason.PARSE_FAILED:
        # The hazard case: header matched, structure broke. Do not fall
        # through to parse_followup -- see module docstring.
        return RouteResult(kind="parse_failed", detail=result.detail)

    # NOT_A_SIGNAL: never claimed to be this template -- but an entry in
    # another layout is a changed template, never a follow-up (rule 3).
    shape = entry_shape(text)
    if shape == "other_instrument":
        return RouteResult(kind="skip", detail="another instrument's signal, not gold")

    # Worth asking parse_followup, which carries its own guard too -- and
    # for entry-shaped text, only so the caller can say what was not done.
    followup = parse_followup(
        text,
        channel_id=channel_cfg.channel_id,
        message_id=message_id,
        posted_at_utc=posted_at_utc,
        reply_to_message_id=reply_to_message_id,
    )
    if shape == "gold":
        return RouteResult(
            kind="parse_failed",
            detail=f"reads as a trade entry, but not in this channel's format ({result.detail})",
            unqueued_followup=followup,
        )
    if followup is not None:
        return RouteResult(kind="followup", followup=followup, detail=result.detail)

    return RouteResult(kind="skip", detail=result.detail)
