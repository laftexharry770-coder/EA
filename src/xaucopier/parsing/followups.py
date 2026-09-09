"""Grammar for messages that modify a live trade.

Order matters: the first matching rule wins, and SL_TO_BE is tested before
AMEND_SL so that "SL to BE" is never read as an amend with a missing price.

Safety: a negated message (containing "don't", "do not", "never", or "hold
off") never dispatches an op -- parse_followup returns None regardless of
what else the message contains. Follow-up messages are short, and the
governing principle here is loud failure over wrong action: a poster typing
"don't close all yet" must never result in the position being closed, so
the safe outcome is "no operation, deferred to a human," not a best-effort
guess at the intended op.
"""
from __future__ import annotations

import re
from datetime import datetime

from xaucopier.domain.signal import FollowUp, FollowUpOp
from xaucopier.parsing.normalize import normalize

NUM = r"\d+(?:\.\d+)?"

# A message that looks like a full entry signal is never a follow-up.
#
# The word "SIGNAL" alone is too thin a guard: an entry that drops that one
# word is rejected by channel_mmh.parse and then reads here as
# "AMEND_SL price_hint=<the entry's stop>". Under the obvious "try parse,
# fall back to parse_followup" routing, a template change would move a live
# trade's stop to a brand-new signal's level. So the test is structural --
# entry *shape*, not entry vocabulary:
#
#   _ZONE_LINE   a line that is nothing but NUM-NUM (mirrors channel_mmh's
#                _ZONE, the one line-anchored pattern an entry always has)
#   _SL_VALUE + _INDEXED_TP   a stop written as a value together with at
#                least one numbered target -- the rest of an entry's body
#
# Note what does NOT trip this: "TP2 to 4550" has an indexed target but no
# SL value, and "SL to 4461" has neither (a bare "to" is not a value), so
# both still parse as the amends they are.
_HAS_SIGNAL_WORD = re.compile(r"\bSIGNAL\b", re.I)
_ZONE_LINE = re.compile(rf"^{NUM}\s*[-–—]\s*{NUM}$", re.M)
_SL_VALUE = re.compile(rf"\bSL\b\s*:?\s*{NUM}", re.I)
_INDEXED_TP = re.compile(rf"\bTP\s*\d+\s*:?\s*{NUM}", re.I)


def _looks_like_an_entry_signal(clean: str) -> bool:
    if _HAS_SIGNAL_WORD.search(clean):
        return True
    if _ZONE_LINE.search(clean):
        return True
    return bool(_SL_VALUE.search(clean) and _INDEXED_TP.search(clean))

# normalize() does not fold curly punctuation, so "don't" may arrive with
# either an ASCII apostrophe or U+2019; "dont" with no apostrophe at all is
# also common. Any of these anywhere in the message blanks the whole parse.
_NEGATION = re.compile(r"\b(?:do\s*n['\u2019]?t|do\s+not|never|hold\s+off)\b", re.I)

_RULES: tuple[tuple[FollowUpOp, re.Pattern[str]], ...] = (
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
        rf"\btp\s*(\d*)\b\s*(?:to|:|=|@)\s*({NUM})", re.I)),
)


def parse_followup(
    text: str,
    *,
    channel_id: int,
    message_id: int,
    posted_at_utc: datetime,
    reply_to_message_id: int | None = None,
) -> FollowUp | None:
    clean = normalize(text)
    if _NEGATION.search(clean):
        return None
    if _looks_like_an_entry_signal(clean):
        return None

    for op, pattern in _RULES:
        match = pattern.search(clean)
        if match is None:
            continue
        price = None
        tp_index = None
        if op is FollowUpOp.AMEND_TP:
            idx_str = match.group(1)
            tp_index = int(idx_str) if idx_str else None
            price = float(match.group(2))
        elif op is FollowUpOp.AMEND_SL and match.groups():
            price = float(match.group(1))
        return FollowUp(
            channel_id=channel_id,
            message_id=message_id,
            op=op,
            posted_at_utc=posted_at_utc,
            reply_to_message_id=reply_to_message_id,
            price_hint=price,
            tp_index_hint=tp_index,
            raw=clean,
        )
    return None
