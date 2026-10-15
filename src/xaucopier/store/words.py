"""Words the store's alerts and rules share: the retraction words, and an
instruction said in plain words.

`edits_repo` decides with the retraction words (an edit that adds one
withdraws a call, ledger 161; a post first read already edited and
carrying one is no signal, final fix FF6); `relay_repo` names them in an
alert about a copy that says CANCELLED itself (final fix FF7); and
`messages_repo` and `edits_repo` both name an instruction that was not
carried out (final fix FF2). Here, so neither module has to import the
other for them.
"""
from __future__ import annotations

import re

from xaucopier.domain.signal import FollowUp
from xaucopier.parsing.normalize import normalize

#: Whole words, any case: an edit that ADDS one to a signal withdraws it.
RETRACTION_WORDS = ("CANCELLED", "CANCELED", "CANCEL", "VOID", "INVALID")
_RETRACTION_WORD = re.compile(r"\b(?:" + "|".join(RETRACTION_WORDS) + r")\b", re.IGNORECASE)

_INSTRUCTION_WORDS = {
    "sl_to_be": "move the stop loss to the entry price",
    "close_half": "close half of the trade",
    "close_all": "close the whole trade",
    "cancel_pending": "cancel the orders not filled yet",
    "amend_sl": "move the stop loss",
    "amend_tp": "move a take profit",
}


def retraction_words(text: str) -> frozenset[str]:
    """The retraction words (`RETRACTION_WORDS`) `text` carries, upper-cased."""
    return frozenset(match.group(0).upper() for match in _RETRACTION_WORD.finditer(normalize(text)))


def said(words: frozenset[str]) -> str:
    """Retraction words as an alert says them: "CANCELLED", "CANCEL / VOID"."""
    return " / ".join(sorted(words))


def instruction_words(followup: FollowUp) -> str:
    """A follow-up's instruction in plain words, e.g. "move the stop loss to
    4470" or "close the whole trade"."""
    words = _INSTRUCTION_WORDS.get(followup.op.value, followup.op.value)
    if followup.op.value == "amend_tp" and followup.tp_index_hint is not None:
        words = f"move take profit {followup.tp_index_hint}"
    if followup.op.value in ("amend_sl", "amend_tp") and followup.price_hint is not None:
        words = f"{words} to {followup.price_hint:g}"
    return words
