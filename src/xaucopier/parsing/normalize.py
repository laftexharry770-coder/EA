"""Fold a raw Telegram message into a canonical form the regexes can match.

Runs before every parser. Deliberately conservative: it removes decoration
and normalises separators, and changes nothing that could alter a number's
value except the comma-disambiguation rule, which is anchored on digit
context.

Comma disambiguation is a single pass over each whole numeric run (a
maximal stretch of digits and commas, e.g. the "4,480,25" in "SL: 4,480,25"),
classified by the shape of that *entire run* before anything is rewritten.
This matters because a real numeric run can mix a thousands separator and a
decimal comma in the same token (e.g. "4,480,25" -- gold trades in exactly
this range), which neither "always thousands" nor "always decimal" handles
correctly on its own. In order, the first matching rule wins:

1. Pure grouping -- one to three leading digits, then one or more groups of
   exactly three digits, and nothing else (e.g. "4,480" or "1,234,567").
   Every comma is removed. Checked first: "1,234,567" also has the shape
   "grouping + a trailing 1-3-digit group" (rule 2), so grouping must win
   before rule 2 gets a chance to misread the last group as a decimal tail.
2. Grouping plus a decimal tail -- the same grouped shape as rule 1, but
   followed by one more comma and 1-3 trailing digits (e.g. "4,480,25" or
   "12,345,6"). All commas except the last are removed; the last becomes
   the decimal point.
3. A single decimal comma -- any run of digits, one comma, then 1-3 digits
   (e.g. "4480,25" or "4480,255" -- this bot only ever sees 1-3 decimal
   gold prices). The comma becomes the decimal point.
4. Anything else is left unchanged. A run like "4,4805" is neither a valid
   grouping nor a plausible 1-3-digit decimal, so rewriting it either way
   would invent a different, valid-looking number -- it is left alone
   rather than silently reinterpreted.

Everything outside a numeric run (prose commas, a trailing comma with no
digit after it) is never touched, so it is safe by construction.
"""
from __future__ import annotations

import re
import unicodedata

# Unicode categories stripped as decoration:
#   So = symbol/other (most emoji)
#   Cf = format (zero-width joiner, BOM, direction marks)
#   Mn = non-spacing mark -- this is where VARIATION SELECTOR-16 (U+FE0F)
#        actually lives, not Cf. The whole VS-16 emoji family arrives with
#        one of these trailing every glyph.
#   Me = enclosing mark -- COMBINING ENCLOSING KEYCAP (U+20E3), the tail of
#        the keycap digits.
# The two mark categories matter more than they look: _ZONE is the only
# line-anchored regex (^...$), and a leftover U+FE0F is non-whitespace, so
# .strip() will not remove it, the anchor fails, and a valid signal is hard
# rejected on decoration alone.
_DECORATION = {"So", "Cf", "Mn", "Me"}

# A maximal run of digits and commas, anchored on digits at both ends so a
# trailing bare comma (e.g. "4470,") is never swept in.
_NUMERIC_RUN = re.compile(r"\d[\d,]*\d|\d")

_PURE_GROUPING = re.compile(r"^\d{1,3}(?:,\d{3})+$")
_GROUPING_WITH_DECIMAL_TAIL = re.compile(r"^\d{1,3}(?:,\d{3})+,\d{1,3}$")
_SINGLE_DECIMAL = re.compile(r"^\d+,\d{1,3}$")

_WS_RUN = re.compile(r"[ \t]+")


def _rewrite_numeric_run(match: re.Match[str]) -> str:
    run = match.group(0)
    if _PURE_GROUPING.match(run):
        return run.replace(",", "")
    if _GROUPING_WITH_DECIMAL_TAIL.match(run):
        head, _, tail = run.rpartition(",")
        return head.replace(",", "") + "." + tail
    if _SINGLE_DECIMAL.match(run):
        head, _, tail = run.rpartition(",")
        return head + "." + tail
    return run


def normalize(text: str) -> str:
    # NFKC folds full-width digits and compatibility forms to ASCII.
    text = unicodedata.normalize("NFKC", text)
    # One pass: drop decoration, and fold every Unicode space separator
    # (Zs) to a plain space so the whitespace collapse below sees them.
    # NFKC already folds most Zs characters, but not all -- U+1680 OGHAM
    # SPACE MARK has no compatibility decomposition -- so the rule is the
    # category itself, never a hand-kept list of code points.
    text = "".join(
        " " if unicodedata.category(ch) == "Zs" else ch
        for ch in text
        if unicodedata.category(ch) not in _DECORATION
    )
    # One pass, classifying each whole numeric run before rewriting it --
    # see the module docstring for the four rules and their order.
    text = _NUMERIC_RUN.sub(_rewrite_numeric_run, text)

    lines = []
    for raw_line in text.splitlines():
        line = _WS_RUN.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)
