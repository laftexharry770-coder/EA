"""The outcome of running a channel parser over one raw message."""
from __future__ import annotations

from dataclasses import dataclass

from xaucopier.domain.errors import RejectReason
from xaucopier.domain.signal import Signal


@dataclass(frozen=True, slots=True)
class ParseResult:
    ok: bool
    signal: Signal | None = None
    reason: RejectReason | None = None
    detail: str = ""

    @classmethod
    def fail(cls, detail: str) -> "ParseResult":
        """This channel's template was recognised and then did not hold.

        PARSE_FAILED is the signal that a template has changed, and three in
        a row auto-trip PAUSE_NEW (spec 6.2). Reserve it for messages that
        really are this channel's format: everything else must use skip(),
        or ordinary chatter disarms the one detector that matters.
        """
        return cls(ok=False, reason=RejectReason.PARSE_FAILED, detail=detail)

    @classmethod
    def skip(cls, detail: str) -> "ParseResult":
        """Not this parser's business: chatter, or a non-gold symbol.

        Distinct from fail() so the caller's consecutive-failure counter
        stays a template-change detector rather than a chatter counter.
        """
        return cls(ok=False, reason=RejectReason.NOT_A_SIGNAL, detail=detail)
