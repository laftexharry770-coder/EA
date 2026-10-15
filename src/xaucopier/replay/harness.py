"""Replay recorded messages through parse -> plan and report the decision.

The MT5 Strategy Tester cannot exercise any of the logic in domain/, so this
is the only regression net the money-critical path has. Every message ever
received is stored verbatim and replayed on every commit.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.planner import plan_entry
from xaucopier.domain.signal import Signal
from xaucopier.parsing.channel_mmh import parse


@dataclass(frozen=True, slots=True)
class Decision:
    message_id: int
    accepted: bool
    reason: str | None
    leg_count: int
    entry_kind: str | None
    entry_price: float | None
    sl: float | None
    leg_indices: tuple[int, ...]
    prices: tuple[float, ...]
    #: Each leg's broker comment. It is the one label a position carries at
    #: the broker, so a change to its format is a change to a historical
    #: decision and must show in the golden file.
    comments: tuple[str, ...]

    def as_dict(self) -> dict:
        data = asdict(self)
        data["leg_indices"] = list(self.leg_indices)
        data["prices"] = list(self.prices)
        data["comments"] = list(self.comments)
        return data


def replay(
    fixtures: list[dict],
    account: AccountSpec,
    spec: SymbolSpec,
    policy: Policy,
    snapshot_for: Callable[[Signal], MarketSnapshot],
) -> list[Decision]:
    """snapshot_for takes the parsed Signal (not just its timestamp) so the
    synthetic market can be derived from the message's OWN levels -- see
    Item I6. A hard-coded price silently turns any fixture whose zone sits
    far from that price into a baked-in magnitude_band rejection."""
    decisions: list[Decision] = []
    for fixture in fixtures:
        posted = datetime.fromisoformat(fixture["posted_at_utc"])
        result = parse(
            fixture["text"],
            channel_id=fixture["channel_id"],
            message_id=fixture["message_id"],
            posted_at_utc=posted,
        )
        if not result.ok:
            decision = Decision(fixture["message_id"], False, result.reason.value,
                                0, None, None, None, (), (), ())
        else:
            plan = plan_entry(result.signal, account, spec, snapshot_for(result.signal), policy)
            decision = Decision(
                message_id=fixture["message_id"],
                accepted=plan.accepted,
                reason=plan.reason.value if plan.reason else None,
                leg_count=len(plan.legs),
                entry_kind=plan.legs[0].kind.value if plan.legs else None,
                entry_price=plan.legs[0].price if plan.legs else None,
                sl=plan.legs[0].sl if plan.legs else None,
                leg_indices=tuple(leg.leg_index for leg in plan.legs),
                prices=tuple(leg.tp for leg in plan.legs),
                comments=tuple(leg.comment for leg in plan.legs),
            )
        if "expect" in fixture:
            want_accept = fixture["expect"] == "accept"
            assert decision.accepted is want_accept, (
                f"fixture {fixture['message_id']} declared expect={fixture['expect']!r} "
                f"but decided accepted={decision.accepted}"
            )
        decisions.append(decision)
    return decisions


def _main() -> None:
    parser = argparse.ArgumentParser(description="Replay recorded messages.")
    parser.add_argument("--write-golden", action="store_true",
                        help="overwrite the expected-decisions golden file")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
    fixtures = [json.loads(p.read_text(encoding="utf-8"))
                for p in sorted((root / "messages").glob("*.json"))]
    spec = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)
    account = AccountSpec("ic_demo", "XAUUSD", fixed_lot=0.01, magic_base=77001)

    def snapshot_for(signal: Signal) -> MarketSnapshot:
        # A pure function of the fixture's own signal, not a wall clock and
        # not a hard-coded price -- the mid tracks the entry zone the poster
        # actually quoted, so a new fixture at a different gold level does
        # not replay as a baked-in magnitude_band rejection (Item I6).
        spread = 0.10
        mid = (signal.zone.lo + signal.zone.hi) / 2.0
        return MarketSnapshot(bid=mid - spread / 2, ask=mid + spread / 2,
                              tick_time_utc=signal.posted_at_utc,
                              spread_usd=spread, median_spread_usd=spread,
                              now_utc=signal.posted_at_utc + timedelta(seconds=5))

    decisions = replay(fixtures, account, spec, Policy(), snapshot_for)
    payload = json.dumps([d.as_dict() for d in decisions], indent=2)
    if args.write_golden:
        (root / "expected_decisions.json").write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {len(decisions)} decisions")
    else:
        print(payload)


if __name__ == "__main__":
    _main()
