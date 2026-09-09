"""Parser for MARKET MASTERY HUB.

Observed format (2026-09-05):

    XAUUSD | BUY SIGNAL
    4480-4485
    SL: 4470
    TP1: 4490
    ...
    TP4 :4505      <- note the space before the colon; tolerate both sides

A partial match is a hard reject. This parser never returns a best-effort
Signal: a template change must surface as a loud parse failure, never as a
trade with a missing leg.

Rejection comes in two flavours, and the difference is load-bearing. A
message that never claimed to be this template -- chatter, or a signal for
another symbol -- is *skipped* (NOT_A_SIGNAL). Only a message whose header
matched and whose structure then failed is a *failure* (PARSE_FAILED),
because that is what the caller's consecutive-failure counter watches, and
three of those trip PAUSE_NEW (spec 6.2). Counting chatter there would trip
the safety stop on noise and train the operator to ignore it.

Ambiguity is also a hard reject, not just absence. The header, entry zone
and stop loss must each match exactly once in the message -- two matches
(a stale value re-stated, or two signal blocks concatenated together) is
rejected rather than silently resolved by "first match wins". Take profit
indices must be unique and form a contiguous run starting at 1 (1..N) --
a duplicate index, a gap, or a run that skips TP1 is rejected rather than
assembled into a plausible-looking but wrong ladder.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

from xaucopier.domain.signal import EntryZone, Side, Signal
from xaucopier.parsing.normalize import normalize
from xaucopier.parsing.result import ParseResult

NUM = r"\d+(?:\.\d+)?"
GOLD_ALIASES = {"XAUUSD", "GOLD", "XAU"}

_HEAD = re.compile(rf"\b(XAUUSD|GOLD|XAU)\b\s*\|\s*\b(BUY|SELL)\b\s+SIGNAL", re.I)
_ZONE = re.compile(rf"^({NUM})\s*[-–—]\s*({NUM})$", re.M)
_SL = re.compile(rf"\bSL\b\s*:?\s*({NUM})", re.I)
_TP = re.compile(rf"\bTP\s*(\d+)\s*:?\s*({NUM})", re.I)


def _content_hash(side: Side, zone: EntryZone, sl: float, tps: tuple[float, ...]) -> str:
    """Identity of the *trade*, not the message.

    Two messages describing the same call hash the same even if the emoji,
    spacing or wording differ -- which is what makes cross-channel relay
    dedup possible.
    """
    payload = "|".join([
        side.value,
        f"{zone.lo:.3f}", f"{zone.hi:.3f}", f"{sl:.3f}",
        ",".join(f"{tp:.3f}" for tp in tps),
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def parse(
    text: str,
    *,
    channel_id: int,
    message_id: int,
    posted_at_utc: datetime,
    source: str = "live",
) -> ParseResult:
    clean = normalize(text)

    heads = list(_HEAD.finditer(clean))
    # No header and a non-gold alias are both "not addressed to us", not a
    # broken template: they skip. Only a message that announced itself as
    # this channel's gold format and then failed to hold it is a parse
    # failure, because that is the streak that trips PAUSE_NEW.
    if not heads:
        return ParseResult.skip("no 'SYMBOL | SIDE SIGNAL' header")
    if len(heads) > 1:
        return ParseResult.fail("ambiguous message: multiple headers found")
    head = heads[0]
    alias = head.group(1).upper()
    if alias not in GOLD_ALIASES:
        return ParseResult.skip(f"symbol {alias!r} is not gold")
    side = Side.BUY if head.group(2).upper() == "BUY" else Side.SELL

    zone_matches = list(_ZONE.finditer(clean))
    if not zone_matches:
        return ParseResult.fail("no entry zone line")
    if len(zone_matches) > 1:
        return ParseResult.fail("ambiguous message: multiple entry zone lines found")
    zone_match = zone_matches[0]
    zone = EntryZone(float(zone_match.group(1)), float(zone_match.group(2)))

    sl_matches = list(_SL.finditer(clean))
    if not sl_matches:
        return ParseResult.fail("no stop loss")
    if len(sl_matches) > 1:
        return ParseResult.fail("ambiguous message: multiple stop losses found")
    sl = float(sl_matches[0].group(1))

    tp_pairs = [(int(i), float(v)) for i, v in _TP.findall(clean)]
    if not tp_pairs:
        return ParseResult.fail("no take profits")
    indices = [i for i, _ in tp_pairs]
    if len(set(indices)) != len(indices):
        return ParseResult.fail("ambiguous message: duplicate take profit index")
    if sorted(indices) != list(range(1, len(indices) + 1)):
        return ParseResult.fail(
            "ambiguous message: take profit indices are not a contiguous run starting at 1"
        )
    tps = tuple(value for _, value in sorted(tp_pairs, key=lambda pair: pair[0]))

    signal = Signal(
        channel_id=channel_id,
        message_id=message_id,
        side=side,
        symbol_alias="XAUUSD",
        zone=zone,
        sl=sl,
        tps=tps,
        posted_at_utc=posted_at_utc,
        content_hash=_content_hash(side, zone, sl, tps),
        source=source,
    )
    return ParseResult(ok=True, signal=signal)
