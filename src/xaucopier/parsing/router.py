"""Signal-first arbitration: routes one raw message to signal, followup,
skip, or parse_failed.

This closes the hazard the Plan 1 final review named: an entry signal whose
template breaks -- say the channel drops the word "SIGNAL" -- is correctly
rejected by the channel's signal parser. Under the naive "try parse(), fall
back to parse_followup()" routing, that same broken text is then read by
the follow-up parser as a legitimate instruction against a live trade
(observed case: it produced AMEND_SL price_hint=<the entry's own stop>).
That would turn a template change into "move the live position's stop."

The rule that prevents it, in order:

1. Run the channel's signal parser. `ok` -> kind="signal". Done.
2. PARSE_FAILED means the header matched and the structure then broke --
   this is exactly the hazard case above. Return kind="parse_failed" and
   stop. Never fall through to parse_followup from here.
3. NOT_A_SIGNAL means the message never claimed to be this channel's
   template at all (no header, or a non-gold symbol). Only then is it
   worth asking whether it is a follow-up instruction: a hit is
   kind="followup", a miss is kind="skip".

skip and parse_failed must stay different outcomes: only parse_failed
feeds the consecutive-failure counter that trips PAUSE_NEW (spec 6.2). If
ordinary chatter counted as a parse failure, three unrelated messages
would trip the safety stop and train the operator to ignore it -- which
disarms the one detector that actually watches for a template change.

Pure: no I/O, no clock, no randomness (tests/test_purity.py enforces it).
`channel_cfg` is accepted structurally (see _ChannelCfgLike below) rather
than typed as config.channels.ChannelCfg: that module reads TOML from disk
and parsing/ must never import it, so router.py only ever needs the two
attributes a channel config always carries.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from xaucopier.domain.errors import RejectReason
from xaucopier.domain.signal import FollowUp, Signal
from xaucopier.parsing.followups import parse_followup
from xaucopier.parsing.registry import parser_for

RouteKind = Literal["signal", "followup", "skip", "parse_failed"]


class _ChannelCfgLike(Protocol):
    """The two fields router.route() actually needs from a channel config.

    Structural, not nominal: satisfied by config.channels.ChannelCfg
    without router.py importing that (impure) module.
    """
    channel_id: int
    parser: str


@dataclass(frozen=True, slots=True)
class RouteResult:
    kind: RouteKind
    signal: Signal | None = None
    followup: FollowUp | None = None
    detail: str = ""


def route(
    text: str,
    *,
    channel_cfg: _ChannelCfgLike,
    message_id: int,
    posted_at_utc: datetime,
    reply_to_message_id: int | None = None,
) -> RouteResult:
    parse = parser_for(channel_cfg.parser)
    result = parse(
        text,
        channel_id=channel_cfg.channel_id,
        message_id=message_id,
        posted_at_utc=posted_at_utc,
    )

    if result.ok:
        return RouteResult(kind="signal", signal=result.signal, detail=result.detail)

    if result.reason is RejectReason.PARSE_FAILED:
        # The hazard case: header matched, structure broke. Do not fall
        # through to parse_followup -- see module docstring.
        return RouteResult(kind="parse_failed", detail=result.detail)

    # NOT_A_SIGNAL: never claimed to be this template. Worth asking
    # parse_followup, which carries its own guard against entry-shaped text.
    followup = parse_followup(
        text,
        channel_id=channel_cfg.channel_id,
        message_id=message_id,
        posted_at_utc=posted_at_utc,
        reply_to_message_id=reply_to_message_id,
    )
    if followup is not None:
        return RouteResult(kind="followup", followup=followup, detail=result.detail)

    return RouteResult(kind="skip", detail=result.detail)
