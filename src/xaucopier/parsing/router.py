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
   ("US30 | BUY SIGNAL ...") is not gold and not a changed template: it
   stays a skip, so a channel's index calls never raise a false alarm.
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
#: The channel's own "SYMBOL | SIDE" header shape, with its symbol.
_PIPE_HEADER = re.compile(r"\b([A-Z][A-Z0-9]{1,9})\s*\|\s*(?:BUY|SELL)\b", re.I)


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
    """A "SYMBOL | SIDE" header whose symbol is no gold alias (and does
    not contain one: "XAUEUR", "GOLDUSD" are gold)."""
    for match in _PIPE_HEADER.finditer(clean):
        symbol = match.group(1).upper()
        if "XAU" not in symbol and "GOLD" not in symbol:
            return True
    return False


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
    if shape == "gold":
        return RouteResult(
            kind="parse_failed",
            detail=f"reads as a trade entry, but not in this channel's format ({result.detail})",
        )
    if shape == "other_instrument":
        return RouteResult(kind="skip", detail="another instrument's signal, not gold")

    # Worth asking parse_followup, which carries its own guard too.
    followup = parse_followup(
        text,
        channel_id=channel_cfg.channel_id,
        message_id=message_id,
        posted_at_utc=posted_at_utc,
        reply_to_message_id=reply_to_message_id,
    )
    if followup is not None:
        return RouteResult(kind="followup", followup=followup, detail=result.detail)

    return RouteResult(kind="skip", detail=result.detail)
