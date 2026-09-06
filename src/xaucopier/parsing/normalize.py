"""Fold a raw Telegram message into a canonical form the regexes can match.

Runs before every parser. Deliberately conservative: it removes decoration
and normalises separators, and changes nothing that could alter a number's
value except the two separator rules, which are anchored on digit context.

Comma disambiguation is done in two ordered passes, by the shape of the
*whole numeric token* rather than by counting digits around one comma:

1. A comma is a thousands separator only when the entire numeric run is
   properly grouped: one to three leading digits, then one or more groups
   of exactly three digits (e.g. "4,480" or "1,234,567"). Only commas
   inside such a fully-grouped run are stripped.
2. Anything left over: a comma still sitting between digits is a decimal
   comma *if* it is followed by a plausible number of fractional digits
   (1-3 -- this bot only ever sees 1-3 decimal gold prices). A comma
   followed by 4+ digits that also failed rule 1 is neither a valid
   grouping nor a plausible decimal (e.g. "4,4805") and is left alone
   rather than silently reinterpreted as a different, valid-looking
   number.
"""
from __future__ import annotations

import re
import unicodedata

# Unicode categories: So = symbol/other (most emoji), Cf = format
# (zero-width joiners, variation selectors, direction marks).
_DECORATION = {"So", "Cf"}

_THOUSANDS_GROUP = re.compile(r"\b\d{1,3}(?:,\d{3})+(?![\d,])")
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d{1,3}(?!\d))")
_WS_RUN = re.compile(r"[ \t]+")


def normalize(text: str) -> str:
    # NFKC folds full-width digits and compatibility forms to ASCII.
    text = unicodedata.normalize("NFKC", text)
    text = "".join(
        " " if ch in "   " else ch
        for ch in text
        if unicodedata.category(ch) not in _DECORATION
    )
    # Grouped-thousands first (whole-token shape): 4,480 -> 4480,
    # 1,234,567 -> 1234567. Only commas inside a fully-grouped run are
    # stripped, so a malformed run like "4,4805" is untouched here.
    text = _THOUSANDS_GROUP.sub(lambda m: m.group(0).replace(",", ""), text)
    # Then decimal, for whatever comma is left between digits with a
    # plausible (1-3 digit) fractional tail: 4480,25 -> 4480.25,
    # 4480,255 -> 4480.255. "4,4805" still fails here (4-digit tail).
    text = _DECIMAL_COMMA.sub(".", text)

    lines = []
    for raw_line in text.splitlines():
        line = _WS_RUN.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)
