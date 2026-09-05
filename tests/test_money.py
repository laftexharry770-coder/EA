import pytest
from xaucopier.domain.money import SymbolSpec, round_price, usd_to_points, normalize_volume

FXPRO = SymbolSpec(name="GOLD", digits=2, point=0.01, tick_size=0.01,
                   volume_min=0.01, volume_max=100.0, volume_step=0.01,
                   stops_level_points=0, freeze_level_points=0)

EXNESS = SymbolSpec(name="XAUUSDm", digits=3, point=0.001, tick_size=0.001,
                    volume_min=0.01, volume_max=100.0, volume_step=0.01,
                    stops_level_points=0, freeze_level_points=0)

def test_round_price_two_digit():
    assert round_price(4480.126, FXPRO) == 4480.13

def test_round_price_three_digit():
    assert round_price(4480.1264, EXNESS) == 4480.126

def test_round_price_is_idempotent():
    once = round_price(4480.126, FXPRO)
    assert round_price(once, FXPRO) == once

def test_usd_to_points_differs_by_digits():
    # The 10x trap, made explicit: 0.30 USD is 30 points on a 2-digit
    # feed and 300 on a 3-digit one.
    assert usd_to_points(0.30, FXPRO) == 30
    assert usd_to_points(0.30, EXNESS) == 300

def test_normalize_volume_snaps_to_step():
    assert normalize_volume(0.014, FXPRO) == 0.01
    assert normalize_volume(0.016, FXPRO) == 0.02

def test_normalize_volume_clamps_to_min():
    assert normalize_volume(0.003, FXPRO) == 0.01

def test_normalize_volume_rejects_above_max():
    with pytest.raises(ValueError):
        normalize_volume(500.0, FXPRO)
