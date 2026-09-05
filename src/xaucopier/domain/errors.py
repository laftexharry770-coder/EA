"""Every reason this system declines to trade. One enum, no free text."""
from __future__ import annotations

from enum import Enum


class RejectReason(str, Enum):
    STALE = "stale"
    TICK_STALE = "tick_stale"
    SPREAD_WIDE = "spread_wide"
    GEOMETRY_CONFLICT = "geometry_conflict"
    MAGNITUDE_BAND = "magnitude_band"
    SL_BREACHED = "sl_breached"
    ALL_TPS_BEHIND = "all_tps_behind"
    CHASE_EXCEEDED = "chase_exceeded"
    STOPS_TOO_TIGHT = "stops_too_tight"
    INVALID_VOLUME = "invalid_volume"
    MARKET_SHUT = "market_shut"
    DUPLICATE_SIGNAL = "duplicate_signal"
    PARSE_FAILED = "parse_failed"
    NOT_APPLICABLE = "not_applicable"
    AMBIGUOUS_TARGET = "ambiguous_target"
