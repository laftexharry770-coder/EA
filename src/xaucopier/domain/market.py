"""Live market state and static account configuration, as frozen values."""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    bid: float
    ask: float
    tick_time_utc: datetime
    spread_usd: float
    median_spread_usd: float
    now_utc: datetime

    def __post_init__(self) -> None:
        for name in ("tick_time_utc", "now_utc"):
            if getattr(self, name).tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware UTC")

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True, slots=True)
class AccountSpec:
    account_id: str
    symbol: str
    fixed_lot: float
    magic_base: int
    allow_live: bool = False
    # How many positions one signal opens; None means one per surviving
    # take profit. The config loader enforces 1..MAX_POSITIONS.
    positions: int | None = None


@dataclass(frozen=True, slots=True)
class Policy:
    max_signal_age_seconds: int = 120
    tick_freshness_seconds: int = 15
    max_spread_usd: float = 0.50
    spread_median_multiple: float = 3.0
    magnitude_band_pct: float = 3.0
    chase_floor_usd: float = 1.00
    chase_tp1_fraction: float = 0.25
    stops_distance_spread_multiple: float = 3.0
    be_buffer_usd: float = 0.10
    max_slippage_usd: float = 0.20
    pending_max_age_hours: int = 4
    pending_at: str = "favourable"
    # The basket trailing stop (domain/trailing.py). trail_gap_usd is how far
    # price must clear an odd-numbered TP before the stop moves to that TP.
    trail_enabled: bool = True
    trail_gap_usd: float = 2.00

    def __post_init__(self) -> None:
        if self.pending_at not in ("favourable", "near"):
            raise ValueError(
                f"pending_at={self.pending_at!r} is not valid; "
                'expected "favourable" or "near"'
            )
        # A quoted "false" is a non-empty string, so truthy: it would switch
        # the trail on when the user wrote it off.
        if not isinstance(self.trail_enabled, bool):
            raise ValueError(
                f"trail_enabled must be true or false (without quotes), "
                f"got {self.trail_enabled!r}"
            )
        gap = self.trail_gap_usd
        if (isinstance(gap, bool) or not isinstance(gap, (int, float))
                or not math.isfinite(gap) or gap <= 0):
            raise ValueError(
                f"trail_gap_usd must be a positive distance in USD, got {gap!r}"
            )

    @classmethod
    def from_dict(cls, data: dict) -> "Policy":
        """Build from a loaded TOML dict, ignoring keys this version does not
        know. Unknown keys are a forward-compatibility concern, not an error;
        a `_points` key has already been rejected by the loader."""
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in fields})
