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
        return cls(ok=False, reason=RejectReason.PARSE_FAILED, detail=detail)
