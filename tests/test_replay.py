import json
from datetime import datetime, timedelta, UTC
from pathlib import Path

from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.replay.harness import replay, Decision

FIXDIR = Path(__file__).parent / "fixtures/messages"
GOLDEN = Path(__file__).parent / "fixtures/expected_decisions.json"
SPEC = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)
ACCT = AccountSpec("ic_demo", "XAUUSD", fixed_lot=0.01, magic_base=77001)

def _snap_for(posted: datetime) -> MarketSnapshot:
    """Deterministic synthetic market: price sits inside the zone."""
    return MarketSnapshot(bid=4482.0, ask=4482.10, tick_time_utc=posted,
                          spread_usd=0.10, median_spread_usd=0.10,
                          now_utc=posted + timedelta(seconds=5))

def _fixtures():
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXDIR.glob("*.json"))]

def test_every_fixture_produces_a_decision():
    decisions = replay(_fixtures(), ACCT, SPEC, Policy(), _snap_for)
    assert len(decisions) == len(_fixtures())

def test_decisions_match_the_golden_file():
    decisions = replay(_fixtures(), ACCT, SPEC, Policy(), _snap_for)
    actual = [d.as_dict() for d in decisions]
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert actual == expected, (
        "A parser or planner change altered a historical decision.\n"
        "If this change is intended, regenerate with:\n"
        "  python -m xaucopier.replay.harness --write-golden"
    )

def test_the_real_message_yields_four_legs():
    decisions = replay(_fixtures(), ACCT, SPEC, Policy(), _snap_for)
    mmh = next(d for d in decisions if d.message_id == 4001)
    assert mmh.accepted and mmh.leg_count == 4
