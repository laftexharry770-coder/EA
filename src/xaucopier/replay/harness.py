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
from xaucopier.parsing.channel_mmh import parse


@dataclass(frozen=True, slots=True)
class Decision:
    message_id: int
    accepted: bool
    reason: str | None
    leg_count: int
    entry_kind: str | None
    prices: tuple[float, ...]

    def as_dict(self) -> dict:
        data = asdict(self)
        data["prices"] = list(self.prices)
        return data


def replay(
    fixtures: list[dict],
    account: AccountSpec,
    spec: SymbolSpec,
    policy: Policy,
    snapshot_for: Callable[[datetime], MarketSnapshot],
) -> list[Decision]:
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
            decisions.append(Decision(fixture["message_id"], False,
                                      result.reason.value, 0, None, ()))
            continue
        plan = plan_entry(result.signal, account, spec, snapshot_for(posted), policy)
        decisions.append(Decision(
            message_id=fixture["message_id"],
            accepted=plan.accepted,
            reason=plan.reason.value if plan.reason else None,
            leg_count=len(plan.legs),
            entry_kind=plan.legs[0].kind.value if plan.legs else None,
            prices=tuple(leg.tp for leg in plan.legs),
        ))
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

    def snapshot_for(posted: datetime) -> MarketSnapshot:
        return MarketSnapshot(bid=4482.0, ask=4482.10, tick_time_utc=posted,
                              spread_usd=0.10, median_spread_usd=0.10,
                              now_utc=posted + timedelta(seconds=5))

    decisions = replay(fixtures, account, spec, Policy(), snapshot_for)
    payload = json.dumps([d.as_dict() for d in decisions], indent=2)
    if args.write_golden:
        (root / "expected_decisions.json").write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {len(decisions)} decisions")
    else:
        print(payload)


if __name__ == "__main__":
    _main()
