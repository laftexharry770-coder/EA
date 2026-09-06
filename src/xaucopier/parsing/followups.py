"""Grammar for messages that modify a live trade.

Order matters: the first matching rule wins, and SL_TO_BE is tested before
AMEND_SL so that "SL to BE" is never read as an amend with a missing price.
"""
from __future__ import annotations

import re
from datetime import datetime

from xaucopier.domain.signal import FollowUp, FollowUpOp
from xaucopier.parsing.normalize import normalize

NUM = r"\d+(?:\.\d+)?"

# A message that looks like a full entry signal is never a follow-up.
_IS_SIGNAL = re.compile(r"\bSIGNAL\b", re.I)

_RULES: list[tuple[FollowUpOp, re.Pattern[str]]] = [
    (FollowUpOp.SL_TO_BE, re.compile(
        r"\b(?:sl|stop)\b[^\n]{0,20}\b(?:be|breakeven|break\s*even|entry)\b"
        r"|\bbreak\s*even\b", re.I)),
    (FollowUpOp.CLOSE_HALF, re.compile(
        r"\b(?:close|take)\b[^\n]{0,15}\b(?:half\b|50\s*%)", re.I)),
    (FollowUpOp.CLOSE_ALL, re.compile(
        r"\bclose\b[^\n]{0,15}\b(?:all|everything|now)\b", re.I)),
    (FollowUpOp.CANCEL_PENDING, re.compile(
        r"\bcancel\b[^\n]{0,20}\b(?:pending|order|limit)\b", re.I)),
    (FollowUpOp.AMEND_SL, re.compile(
        rf"\b(?:sl|stop\s*loss)\b\s*(?:to|:|=|@)?\s*({NUM})", re.I)),
    (FollowUpOp.AMEND_TP, re.compile(
        rf"\btp\s*\d*\b\s*(?:to|:|=|@)\s*({NUM})", re.I)),
]


def parse_followup(
    text: str,
    *,
    channel_id: int,
    message_id: int,
    posted_at_utc: datetime,
    reply_to_message_id: int | None = None,
) -> FollowUp | None:
    clean = normalize(text)
    if _IS_SIGNAL.search(clean):
        return None

    for op, pattern in _RULES:
        match = pattern.search(clean)
        if match is None:
            continue
        price = None
        if op in (FollowUpOp.AMEND_SL, FollowUpOp.AMEND_TP) and match.groups():
            price = float(match.group(1))
        return FollowUp(
            channel_id=channel_id,
            message_id=message_id,
            op=op,
            posted_at_utc=posted_at_utc,
            reply_to_message_id=reply_to_message_id,
            price_hint=price,
            raw=clean,
        )
    return None
