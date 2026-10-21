"""Live market state and static account configuration, as frozen values."""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from datetime import datetime

#: One pip on gold (XAUUSD), in USD: fixed at $0.10 on every broker (the
#: user's decision of 2026-10-21). It is NEVER derived from a broker's
#: digits or point size: gold is quoted at 2 digits on some feeds and 3 on
#: others, and a pip read from the point -- as the EA's automatic CalcPip
#: does -- is $0.01 on a 3-digit feed, ten times too small. Only the two
#: policy settings in pips (be_buffer_pips, ladder_early_pips) use it;
#: every other price and distance is USD.
PIP_USD = 0.10

#: Policy keys that no longer exist, refused rather than ignored: a stale
#: key silently ignored would change the user's buffer or ladder.
_RETIRED_POLICY_KEYS = {
    "be_buffer_usd": (
        "be_buffer_usd is no longer used: the breakeven buffer is now set in pips, "
        "as be_buffer_pips (1 pip = $0.10 on gold, on every broker; "
        "be_buffer_pips = 1.0 is $0.10)"
    ),
    "trail_gap_usd": (
        "trail_gap_usd is no longer used: the old ladder's gap is gone. The stop now "
        "moves one take profit behind each take profit reached (ladder_early_pips "
        "can start a step early), and breakeven's buffer is be_buffer_pips"
    ),
}


def _is_distance(value: object) -> bool:
    """A finite number of zero or more, and not a bool (a bool is an int)."""
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


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
    # Half the positions open now and half rest as limit orders (the
    # user's amendment of 2026-09-25); False keeps the single entry rule.
    layered_entry: bool = True

    def __post_init__(self) -> None:
        # A quoted "false" is a non-empty string, so truthy: it would leave
        # layered entry on when the user wrote it off. The loader refuses it
        # too; this guards an AccountSpec built any other way.
        if not isinstance(self.layered_entry, bool):
            raise ValueError(
                f"layered_entry must be true or false (without quotes), "
                f"got {self.layered_entry!r}"
            )


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
    # Breakeven is a position's own fill + this many pips for a BUY (minus,
    # for a SELL), never past TP1 (domain/trailing.breakeven). PIP_USD each.
    be_buffer_pips: float = 1.0
    max_slippage_usd: float = 0.20
    pending_max_age_hours: int = 4
    pending_at: str = "favourable"
    # The trailing stop ladder (domain/trailing.py), like the user's EA:
    # TP1 reached -> breakeven, TPk reached -> TP(k-1). Entry k-1 of
    # ladder_early_pips starts step k that many pips before TPk; a missing
    # entry is 0, "on hit".
    trail_enabled: bool = True
    ladder_early_pips: tuple[float, ...] = ()

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
        if not _is_distance(self.be_buffer_pips):
            raise ValueError(
                f"be_buffer_pips must be a number of pips, 0 or more, "
                f"got {self.be_buffer_pips!r}"
            )
        early = self.ladder_early_pips
        # TOML has no tuple: a list from policy.toml is frozen into one.
        if isinstance(early, list):
            early = tuple(early)
            object.__setattr__(self, "ladder_early_pips", early)
        if not isinstance(early, tuple) or not all(_is_distance(pips) for pips in early):
            raise ValueError(
                f"ladder_early_pips must be a list of pips, each 0 or more, "
                f"like [0, 0, 20], got {self.ladder_early_pips!r}"
            )

    @classmethod
    def from_dict(cls, data: dict) -> "Policy":
        """Build from a loaded TOML dict, ignoring keys this version does not
        know. Unknown keys are a forward-compatibility concern, not an error;
        a `_points` key has already been rejected by the loader. A RETIRED
        key is an error, naming what replaced it: be_buffer_usd ignored
        would silently change the user's breakeven buffer."""
        retired = [message for key, message in _RETIRED_POLICY_KEYS.items() if key in data]
        if retired:
            raise ValueError("; ".join(retired))
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in fields})
