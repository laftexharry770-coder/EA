"""Live market state and static account configuration, as frozen values."""
from __future__ import annotations

import dataclasses
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

    def __post_init__(self) -> None:
        if self.pending_at not in ("favourable", "near"):
            raise ValueError(
                f"pending_at={self.pending_at!r} is not valid; "
                'expected "favourable" or "near"'
            )

    @classmethod
    def from_dict(cls, data: dict) -> "Policy":
        """Build from a loaded TOML dict, ignoring keys this version does not
        know. Unknown keys are a forward-compatibility concern, not an error;
        a `_points` key has already been rejected by the loader."""
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in fields})
