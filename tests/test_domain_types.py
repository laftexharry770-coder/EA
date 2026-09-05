import dataclasses
import pytest
from datetime import datetime, UTC
from xaucopier.domain.signal import Side, EntryZone, Signal
from xaucopier.domain.intent import Plan, OrderKind
from xaucopier.domain.errors import RejectReason
from xaucopier.domain.market import Policy


def _sig(**kw):
    base = dict(
        channel_id=-1001234, message_id=42, side=Side.BUY, symbol_alias="XAUUSD",
        zone=EntryZone(4480.0, 4485.0), sl=4470.0, tps=(4490.0, 4495.0),
        posted_at_utc=datetime(2026, 9, 5, 10, 45, tzinfo=UTC),
        content_hash="abc", source="live",
    )
    base.update(kw)
    return Signal(**base)


def test_signal_is_frozen():
    s = _sig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.sl = 1.0


def test_entry_zone_orders_its_bounds():
    assert EntryZone(4485.0, 4480.0) == EntryZone(4480.0, 4485.0)


def test_zone_favourable_edge_is_low_for_buy():
    assert EntryZone(4480.0, 4485.0).favourable_edge(Side.BUY) == 4480.0
    assert EntryZone(4480.0, 4485.0).favourable_edge(Side.SELL) == 4485.0


def test_signal_requires_aware_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        _sig(posted_at_utc=datetime(2026, 9, 5, 10, 45))


def test_signal_requires_at_least_one_tp():
    with pytest.raises(ValueError, match="at least one"):
        _sig(tps=())


def test_plan_rejected_carries_reason_and_no_legs():
    p = Plan.rejected(RejectReason.STALE)
    assert p.accepted is False
    assert p.reason is RejectReason.STALE
    assert p.legs == ()


def test_order_kind_members():
    assert {k.name for k in OrderKind} == {"MARKET", "LIMIT"}


def test_policy_from_dict_applies_known_keys():
    p = Policy.from_dict({"max_spread_usd": 0.25, "pending_at": "near"})
    assert p.max_spread_usd == 0.25
    assert p.pending_at == "near"


def test_policy_from_dict_ignores_unknown_keys():
    p = Policy.from_dict({"max_spread_usd": 0.25, "future_setting": True})
    assert p.max_spread_usd == 0.25
    assert p.max_signal_age_seconds == 120  # default preserved
