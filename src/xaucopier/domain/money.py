"""Price and volume arithmetic. The ONLY module allowed to round a price.

Gold is quoted to 2 decimals on FxPro/ICMarkets/FBS and 3 on Exness. Every
threshold in this system is a USD float; usd_to_points is the single point
of conversion, and it needs the SymbolSpec to know which feed it is on.
The two policy settings in pips (be_buffer_pips, ladder_early_pips) are
turned into USD with the fixed market.PIP_USD, never with a feed's point.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    """Immutable snapshot of a broker's contract terms for one symbol."""
    name: str
    digits: int
    point: float
    tick_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int
    freeze_level_points: int

    @property
    def stops_level_usd(self) -> float:
        return self.stops_level_points * self.point

    @property
    def freeze_level_usd(self) -> float:
        return self.freeze_level_points * self.point


def round_price(price: float, spec: SymbolSpec) -> float:
    """Snap a price to the symbol's tick grid, then to its digit count."""
    snapped = round(price / spec.tick_size) * spec.tick_size
    return round(snapped, spec.digits)


def usd_to_points(usd: float, spec: SymbolSpec) -> int:
    """Convert a USD distance to broker points. The only such conversion."""
    return int(round(usd / spec.point))


def normalize_volume(volume: float, spec: SymbolSpec) -> float:
    """Snap a lot size to the broker's volume step and clamp to its minimum.

    Raises rather than clamping at the top: a request above volume_max is a
    bug upstream, not something to silently shrink.
    """
    if volume > spec.volume_max:
        raise ValueError(f"volume {volume} exceeds volume_max {spec.volume_max}")
    stepped = round(volume / spec.volume_step) * spec.volume_step
    stepped = round(stepped, 8)
    if stepped < spec.volume_min:
        return spec.volume_min
    return round(stepped, 8)
