import json
from datetime import timedelta
from pathlib import Path

from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.signal import Signal
from xaucopier.replay.harness import replay, Decision

FIXDIR = Path(__file__).parent / "fixtures/messages"
GOLDEN = Path(__file__).parent / "fixtures/expected_decisions.json"
SPEC = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)
ACCT = AccountSpec("ic_demo", "XAUUSD", fixed_lot=0.01, magic_base=77001)

def _snap_for(signal: Signal) -> MarketSnapshot:
    """Deterministic synthetic market derived from the signal's own entry
    zone (mid), not a hard-coded price -- must stay consistent with the
    harness's own snapshot_for (see Item I6)."""
    spread = 0.10
    mid = (signal.zone.lo + signal.zone.hi) / 2.0
    return MarketSnapshot(bid=mid - spread / 2, ask=mid + spread / 2,
                          tick_time_utc=signal.posted_at_utc,
                          spread_usd=spread, median_spread_usd=spread,
                          now_utc=signal.posted_at_utc + timedelta(seconds=5))

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
