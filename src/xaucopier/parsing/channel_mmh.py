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

    head = _HEAD.search(clean)
    if head is None:
        return ParseResult.fail("no 'SYMBOL | SIDE SIGNAL' header")
    alias = head.group(1).upper()
    if alias not in GOLD_ALIASES:
        return ParseResult.fail(f"symbol {alias!r} is not gold")
    side = Side.BUY if head.group(2).upper() == "BUY" else Side.SELL

    zone_match = _ZONE.search(clean)
    if zone_match is None:
        return ParseResult.fail("no entry zone line")
    zone = EntryZone(float(zone_match.group(1)), float(zone_match.group(2)))

    sl_match = _SL.search(clean)
    if sl_match is None:
        return ParseResult.fail("no stop loss")
    sl = float(sl_match.group(1))

    tp_pairs = [(int(i), float(v)) for i, v in _TP.findall(clean)]
    if not tp_pairs:
        return ParseResult.fail("no take profits")
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
