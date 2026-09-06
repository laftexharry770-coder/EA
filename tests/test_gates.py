from datetime import datetime, timedelta, UTC
import pytest

from xaucopier.domain import gates
from xaucopier.domain.errors import RejectReason
from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.signal import EntryZone, Side, Signal

NOW = datetime(2026, 9, 5, 10, 45, tzinfo=UTC)
SPEC = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)
POLICY = Policy()

def snap(bid=4482.0, ask=4482.10, spread=0.10, median=0.10, tick_age=0.0, now=NOW):
    return MarketSnapshot(bid=bid, ask=ask,
                          tick_time_utc=now - timedelta(seconds=tick_age),
                          spread_usd=spread, median_spread_usd=median, now_utc=now)

def sig(side=Side.BUY, zone=(4480.0, 4485.0), sl=4470.0,
        tps=(4490.0, 4495.0), age=5.0):
    return Signal(channel_id=-1, message_id=1, side=side, symbol_alias="XAUUSD",
                  zone=EntryZone(*zone), sl=sl, tps=tps,
                  posted_at_utc=NOW - timedelta(seconds=age),
                  content_hash="h", source="live")

def test_age_gate_accepts_fresh():
    assert gates.age_gate(sig(age=5), SPEC, snap(), POLICY).ok

def test_age_gate_rejects_stale():
    r = gates.age_gate(sig(age=600), SPEC, snap(), POLICY)
    assert not r.ok and r.reason is RejectReason.STALE

def test_age_gate_ignores_source():
    # Provenance is not a gate; only the message's own timestamp is. A
    # backfilled message found 5s after posting is 5s old and must trade.
    backfilled = Signal(channel_id=-1, message_id=1, side=Side.BUY,
                        symbol_alias="XAUUSD", zone=EntryZone(4480.0, 4485.0),
                        sl=4470.0, tps=(4490.0,),
                        posted_at_utc=NOW - timedelta(seconds=5),
                        content_hash="h", source="backfill")
    assert gates.age_gate(backfilled, SPEC, snap(), POLICY).ok

def test_tick_freshness_rejects_old_tick():
    r = gates.tick_freshness_gate(sig(), SPEC, snap(tick_age=60), POLICY)
    assert not r.ok and r.reason is RejectReason.TICK_STALE

def test_spread_gate_rejects_absolute_breach():
    r = gates.spread_gate(sig(), SPEC, snap(spread=0.90, median=0.10), POLICY)
    assert not r.ok and r.reason is RejectReason.SPREAD_WIDE

def test_spread_gate_rejects_median_multiple_breach():
    r = gates.spread_gate(sig(), SPEC, snap(spread=0.45, median=0.10), POLICY)
    assert not r.ok and r.reason is RejectReason.SPREAD_WIDE

def test_geometry_gate_accepts_consistent_buy():
    assert gates.geometry_gate(sig(), SPEC, snap(), POLICY).ok

def test_geometry_gate_rejects_buy_with_tps_below():
    r = gates.geometry_gate(sig(tps=(4460.0, 4455.0)), SPEC, snap(), POLICY)
    assert not r.ok and r.reason is RejectReason.GEOMETRY_CONFLICT

def test_geometry_gate_rejects_sl_on_wrong_side():
    r = gates.geometry_gate(sig(sl=4499.0), SPEC, snap(), POLICY)
    assert not r.ok and r.reason is RejectReason.GEOMETRY_CONFLICT

def test_geometry_gate_rejects_buy_sl_inside_zone():
    # Reviewer's reproduction: sl sits inside the entry zone (past the
    # midpoint check but still on the wrong side of the far edge). A BUY
    # LIMIT filling at the favourable edge (4470) would have its "stop"
    # above entry -- inverted risk.
    r = gates.geometry_gate(
        sig(side=Side.BUY, zone=(4470.0, 4485.0), sl=4475.0, tps=(4490.0, 4495.0)),
        SPEC, snap(), POLICY)
    assert not r.ok and r.reason is RejectReason.GEOMETRY_CONFLICT

def test_geometry_gate_rejects_buy_sl_at_zone_lo():
    r = gates.geometry_gate(
        sig(side=Side.BUY, zone=(4480.0, 4485.0), sl=4480.0, tps=(4490.0, 4495.0)),
        SPEC, snap(), POLICY)
    assert not r.ok and r.reason is RejectReason.GEOMETRY_CONFLICT

def test_geometry_gate_accepts_buy_sl_just_below_zone_lo():
    r = gates.geometry_gate(
        sig(side=Side.BUY, zone=(4480.0, 4485.0), sl=4479.0, tps=(4490.0, 4495.0)),
        SPEC, snap(), POLICY)
    assert r.ok

def test_geometry_gate_rejects_sell_sl_inside_zone():
    r = gates.geometry_gate(
        sig(side=Side.SELL, zone=(4470.0, 4485.0), sl=4480.0, tps=(4460.0, 4455.0)),
        SPEC, snap(), POLICY)
    assert not r.ok and r.reason is RejectReason.GEOMETRY_CONFLICT

def test_geometry_gate_rejects_sell_sl_at_zone_hi():
    r = gates.geometry_gate(
        sig(side=Side.SELL, zone=(4480.0, 4485.0), sl=4485.0, tps=(4470.0, 4465.0)),
        SPEC, snap(), POLICY)
    assert not r.ok and r.reason is RejectReason.GEOMETRY_CONFLICT

def test_geometry_gate_accepts_sell_sl_just_above_zone_hi():
    r = gates.geometry_gate(
        sig(side=Side.SELL, zone=(4480.0, 4485.0), sl=4486.0, tps=(4470.0, 4465.0)),
        SPEC, snap(), POLICY)
    assert r.ok

def test_magnitude_gate_rejects_absurd_level():
    r = gates.magnitude_gate(sig(sl=100.0), SPEC, snap(), POLICY)
    assert not r.ok and r.reason is RejectReason.MAGNITUDE_BAND

def test_invalidation_gate_rejects_breached_sl():
    r = gates.invalidation_gate(sig(), SPEC, snap(bid=4465.0, ask=4465.1), POLICY)
    assert not r.ok and r.reason is RejectReason.SL_BREACHED

def test_invalidation_gate_rejects_when_all_tps_behind():
    r = gates.invalidation_gate(sig(tps=(4490.0,)), SPEC,
                                snap(bid=4499.0, ask=4499.1), POLICY)
    assert not r.ok and r.reason is RejectReason.ALL_TPS_BEHIND

def test_chase_gate_rejects_runaway_price():
    r = gates.chase_gate(sig(), SPEC, snap(bid=4488.0, ask=4488.1), POLICY)
    assert not r.ok and r.reason is RejectReason.CHASE_EXCEEDED

def test_chase_gate_allows_small_overshoot():
    assert gates.chase_gate(sig(), SPEC, snap(bid=4485.4, ask=4485.5), POLICY).ok

def test_stops_distance_rejects_rather_than_widens():
    # The sl sits 12.10 from the ask, so stops_level must exceed that to
    # reject: 1500 points x 0.01 = 15.00 USD minimum distance.
    tight = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 1500, 0)
    r = gates.stops_distance_gate(sig(), tight, snap(), POLICY)
    assert not r.ok and r.reason is RejectReason.STOPS_TOO_TIGHT

def test_volume_gate_rejects_sub_minimum_lot():
    acct = AccountSpec("demo", "XAUUSD", fixed_lot=0.004, magic_base=77001)
    r = gates.volume_gate(acct, SPEC)
    assert not r.ok and r.reason is RejectReason.INVALID_VOLUME

def test_volume_gate_accepts_valid_lot():
    acct = AccountSpec("demo", "XAUUSD", fixed_lot=0.01, magic_base=77001)
    assert gates.volume_gate(acct, SPEC).ok
