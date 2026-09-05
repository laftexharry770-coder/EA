"""Fold a raw Telegram message into a canonical form the regexes can match.

Runs before every parser. Deliberately conservative: it removes decoration
and normalises separators, and changes nothing that could alter a number's
value except the two separator rules, which are anchored on digit context.
"""
from __future__ import annotations

import re
import unicodedata

# Unicode categories: So = symbol/other (most emoji), Cf = format
# (zero-width joiners, variation selectors, direction marks).
_DECORATION = {"So", "Cf"}

_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d{1,2}(?!\d))")
_WS_RUN = re.compile(r"[ \t]+")


def normalize(text: str) -> str:
    # NFKC folds full-width digits and compatibility forms to ASCII.
    text = unicodedata.normalize("NFKC", text)
    text = "".join(
        " " if ch in "   " else ch
        for ch in text
        if unicodedata.category(ch) not in _DECORATION
    )
    # Thousands first: 4,480 -> 4480. Then decimal: 4480,25 -> 4480.25.
    text = _THOUSANDS.sub("", text)
    text = _DECIMAL_COMMA.sub(".", text)

    lines = []
    for raw_line in text.splitlines():
        line = _WS_RUN.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)
