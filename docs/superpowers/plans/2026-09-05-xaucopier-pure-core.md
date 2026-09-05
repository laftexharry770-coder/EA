# xaucopier Pure Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the offline, side-effect-free core of the XAUUSD signal copier — everything that decides *what orders should exist* — so that recorded Telegram messages can be replayed through it in CI with zero network and zero MetaTrader5 import.

**Architecture:** A pure functional core. Text enters `normalize()`, becomes a frozen `Signal` via a per-channel regex parser, passes an ordered chain of pure predicate gates, and is turned into a list of frozen `OrderIntent` objects by `plan_entry()`. No I/O anywhere. An AST-walking test enforces the boundary by failing the build if `domain/` ever imports `telethon`, `MetaTrader5`, `sqlite3`, or `requests`.

**Tech Stack:** Python 3.14.7, stdlib only for the core (`re`, `unicodedata`, `dataclasses`, `enum`, `tomllib`, `datetime`), pytest for tests.

**Spec:** `docs/superpowers/specs/2026-09-05-telegram-mt5-xauusd-copier-design.md`

## Global Constraints

- **Python 3.14.7**, 64-bit. `MetaTrader5 5.0.6180` publishes a `cp314` wheel — do not add a second Python.
- **Project root:** `C:\Users\Herbert\Projects\xaucopier`
- **`domain/` and `parsing/` import no I/O library.** No `telethon`, `MetaTrader5`, `sqlite3`, `requests`, `open()`, or `socket`. Enforced by `tests/test_purity.py` (Task 11).
- **All distances and thresholds are USD floats.** The word `points` appears in exactly one function, `usd_to_points()`. Any config key ending in `_points` is a load-time error. Gold is 2-digit on FxPro/ICMarkets/FBS and 3-digit on Exness; a points-based threshold is silently 10× wrong across brokers.
- **Prices are rounded only by `money.round_price()`.** No `round()` on a price anywhere else.
- **A partial parse is a hard reject.** Never construct a best-effort `Signal` from an incomplete match.
- **Frozen dataclasses everywhere in `domain/`.** Sequences are `tuple`, never `list`.
- **All datetimes are timezone-aware UTC.** Never `datetime.now()` without `tz=UTC`.
- Every commit message ends with:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

---

## File Structure

```
C:\Users\Herbert\Projects\xaucopier\
  pyproject.toml
  config/
    policy.toml               risk/gate thresholds, all USD
  src/xaucopier/
    domain/
      __init__.py
      money.py                SymbolSpec; round_price, usd_to_points, normalize_volume
      signal.py               Side, EntryZone, Signal, FollowUp, FollowUpOp
      market.py               MarketSnapshot, AccountSpec, Policy
      intent.py               OrderKind, OrderIntent, ModifyIntent, CloseIntent, Plan
      errors.py               RejectReason
      gates.py                nine pure predicates
      planner.py              plan_entry()
      followup_planner.py     plan_followup()
    parsing/
      __init__.py
      normalize.py            NFKC, emoji strip, separator handling
      result.py               ParseResult
      channel_mmh.py          MARKET MASTERY HUB parser
      followups.py            follow-up grammar
      registry.py             channel_id -> parser
    config/
      __init__.py
      loader.py               tomllib load + _points assertion
    replay/
      __init__.py
      harness.py              fixtures -> decisions, golden-file diff
  tests/
    fixtures/messages/*.json  every real message, verbatim
    test_money.py  test_normalize.py  test_parser_mmh.py
    test_followup_parse.py  test_gates.py  test_planner.py
    test_followup_planner.py  test_replay.py  test_purity.py
```

Each file has one responsibility and stays small. `money.py` owns all rounding so the 2-vs-3-digit trap has exactly one place to be wrong. `gates.py` holds only predicates and `planner.py` only composition, so a gate can be tested without constructing a plan.

---

### Task 1: Project skeleton and config loader

**Files:**
- Create: `pyproject.toml`, `config/policy.toml`
- Create: `src/xaucopier/config/__init__.py`, `src/xaucopier/config/loader.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing
- Produces: `load_policy_dict(path: Path) -> dict[str, Any]` and `ConfigError`. It raises `ConfigError` on any key ending in `_points`, at any nesting depth. Task 3 adds `Policy.from_dict()`, which turns this dict into the frozen `Policy` the planner consumes.

- [ ] **Step 1: Create the project and repo**

```bash
mkdir -p /c/Users/Herbert/Projects/xaucopier/src/xaucopier/config
mkdir -p /c/Users/Herbert/Projects/xaucopier/tests
cd /c/Users/Herbert/Projects/xaucopier && git init
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "xaucopier"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

- [ ] **Step 3: Write `config/policy.toml`**

All values USD. No key may end in `_points`.

```toml
max_signal_age_seconds   = 120
tick_freshness_seconds   = 15
max_spread_usd           = 0.50
spread_median_multiple   = 3.0
magnitude_band_pct       = 3.0
chase_floor_usd          = 1.00
chase_tp1_fraction       = 0.25
stops_distance_spread_multiple = 3.0
be_buffer_usd            = 0.10
max_slippage_usd         = 0.20
pending_max_age_hours    = 4
pending_at               = "favourable"
```

- [ ] **Step 4: Write the failing test**

```python
# tests/test_config.py
import pytest
from pathlib import Path
from xaucopier.config.loader import load_policy_dict, ConfigError

def test_loads_known_keys(tmp_path: Path):
    p = tmp_path / "policy.toml"
    p.write_text('max_spread_usd = 0.50\npending_at = "favourable"\n', encoding="utf-8")
    cfg = load_policy_dict(p)
    assert cfg["max_spread_usd"] == 0.50
    assert cfg["pending_at"] == "favourable"

def test_rejects_points_key(tmp_path: Path):
    p = tmp_path / "policy.toml"
    p.write_text("max_step_points = 400\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="_points"):
        load_policy_dict(p)

def test_rejects_nested_points_key(tmp_path: Path):
    p = tmp_path / "policy.toml"
    p.write_text("[accounts.fxpro]\nsl_buffer_points = 30\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="_points"):
        load_policy_dict(p)
```

- [ ] **Step 5: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'xaucopier.config.loader'`

- [ ] **Step 6: Write the implementation**

```python
# src/xaucopier/config/loader.py
"""Config loading. The only place a raw TOML file becomes Python."""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised for a structurally invalid config file."""


def _assert_no_points_keys(node: Any, trail: str = "") -> None:
    """Reject any key ending in ``_points``.

    Gold is 2-digit on FxPro/ICMarkets/FBS and 3-digit on Exness, so a
    threshold expressed in points means a 10x different distance depending
    on the broker. Every threshold in this system is USD; the conversion
    lives in money.usd_to_points and nowhere else.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{trail}.{key}" if trail else key
            if key.endswith("_points"):
                raise ConfigError(
                    f"config key {path!r} ends in '_points'; "
                    "all thresholds must be USD (see spec section 3)"
                )
            _assert_no_points_keys(value, path)


def load_policy_dict(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    _assert_no_points_keys(data)
    return data
```

- [ ] **Step 7: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: 3 passed

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml config/policy.toml src/xaucopier/config tests/test_config.py
git commit -m "feat: project skeleton and config loader with _points guard

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: money.py — the only rounding in the codebase

**Files:**
- Create: `src/xaucopier/domain/__init__.py`, `src/xaucopier/domain/money.py`
- Test: `tests/test_money.py`

**Interfaces:**
- Consumes: nothing
- Produces: `SymbolSpec` (frozen dataclass), `round_price(price: float, spec: SymbolSpec) -> float`, `usd_to_points(usd: float, spec: SymbolSpec) -> int`, `normalize_volume(volume: float, spec: SymbolSpec) -> float`

- [ ] **Step 1: Write the failing test**

The last two tests are the whole reason this module is isolated: identical USD inputs must produce identical *prices* but different *point* counts across a 2-digit and a 3-digit gold feed.

```python
# tests/test_money.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_money.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'xaucopier.domain.money'`

- [ ] **Step 3: Write the implementation**

```python
# src/xaucopier/domain/money.py
"""Price and volume arithmetic. The ONLY module allowed to round a price.

Gold is quoted to 2 decimals on FxPro/ICMarkets/FBS and 3 on Exness. Every
threshold in this system is a USD float; usd_to_points is the single point
of conversion, and it needs the SymbolSpec to know which feed it is on.
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_money.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/xaucopier/domain tests/test_money.py
git commit -m "feat: money.py, the single source of price and volume rounding

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Domain types

**Files:**
- Create: `src/xaucopier/domain/signal.py`, `src/xaucopier/domain/market.py`, `src/xaucopier/domain/intent.py`, `src/xaucopier/domain/errors.py`
- Test: `tests/test_domain_types.py`

**Interfaces:**
- Consumes: `SymbolSpec` from Task 2
- Produces:
  - `Side` (`BUY`/`SELL`), `EntryZone(lo, hi)`, `Signal(...)`, `FollowUpOp`, `FollowUp(...)` — `signal.py`
  - `MarketSnapshot(bid, ask, tick_time_utc, spread_usd, median_spread_usd, now_utc)`, `AccountSpec(...)`, `Policy(...)` — `market.py`
  - `OrderKind` (`MARKET`/`LIMIT`), `OrderIntent(...)`, `ModifyIntent(...)`, `CloseIntent(...)`, `Plan(accepted, reason, legs)` — `intent.py`
  - `RejectReason` — `errors.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_domain_types.py
import dataclasses
import pytest
from datetime import datetime, UTC
from xaucopier.domain.signal import Side, EntryZone, Signal
from xaucopier.domain.intent import Plan, OrderKind
from xaucopier.domain.errors import RejectReason

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
```

The test file's imports are:

```python
from xaucopier.domain.signal import Side, EntryZone, Signal
from xaucopier.domain.intent import Plan, OrderKind
from xaucopier.domain.errors import RejectReason
from xaucopier.domain.market import Policy
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_domain_types.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'xaucopier.domain.signal'`

- [ ] **Step 3: Write `errors.py`**

```python
# src/xaucopier/domain/errors.py
"""Every reason this system declines to trade. One enum, no free text."""
from __future__ import annotations

from enum import Enum


class RejectReason(str, Enum):
    STALE = "stale"
    TICK_STALE = "tick_stale"
    SPREAD_WIDE = "spread_wide"
    GEOMETRY_CONFLICT = "geometry_conflict"
    MAGNITUDE_BAND = "magnitude_band"
    SL_BREACHED = "sl_breached"
    ALL_TPS_BEHIND = "all_tps_behind"
    CHASE_EXCEEDED = "chase_exceeded"
    STOPS_TOO_TIGHT = "stops_too_tight"
    INVALID_VOLUME = "invalid_volume"
    MARKET_SHUT = "market_shut"
    DUPLICATE_SIGNAL = "duplicate_signal"
    PARSE_FAILED = "parse_failed"
    NOT_APPLICABLE = "not_applicable"
    AMBIGUOUS_TARGET = "ambiguous_target"
```

- [ ] **Step 4: Write `signal.py`**

```python
# src/xaucopier/domain/signal.py
"""The parsed vocabulary of a channel message. Frozen, no I/O."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """+1 for BUY, -1 for SELL. Lets geometry checks stay side-agnostic."""
        return 1 if self is Side.BUY else -1


@dataclass(frozen=True, slots=True)
class EntryZone:
    lo: float
    hi: float

    def __post_init__(self) -> None:
        if self.lo > self.hi:
            object.__setattr__(self, "lo", self.hi)
            object.__setattr__(self, "hi", self.lo)

    def favourable_edge(self, side: Side) -> float:
        """The edge that gives the better fill: low for a buy, high for a sell."""
        return self.lo if side is Side.BUY else self.hi

    def near_edge(self, side: Side) -> float:
        return self.hi if side is Side.BUY else self.lo

    def contains(self, price: float) -> bool:
        return self.lo <= price <= self.hi


class FollowUpOp(Enum):
    SL_TO_BE = "sl_to_be"
    CLOSE_HALF = "close_half"
    CLOSE_ALL = "close_all"
    CANCEL_PENDING = "cancel_pending"
    AMEND_SL = "amend_sl"
    AMEND_TP = "amend_tp"


@dataclass(frozen=True, slots=True)
class Signal:
    channel_id: int
    message_id: int
    side: Side
    symbol_alias: str
    zone: EntryZone
    sl: float
    tps: tuple[float, ...]
    posted_at_utc: datetime
    content_hash: str
    source: str = "live"

    def __post_init__(self) -> None:
        if self.posted_at_utc.tzinfo is None:
            raise ValueError("posted_at_utc must be timezone-aware UTC")
        if not self.tps:
            raise ValueError("a signal needs at least one take profit")


@dataclass(frozen=True, slots=True)
class FollowUp:
    channel_id: int
    message_id: int
    op: FollowUpOp
    posted_at_utc: datetime
    reply_to_message_id: int | None = None
    price_hint: float | None = None
    tp_index_hint: int | None = None
    raw: str = ""

    def __post_init__(self) -> None:
        if self.posted_at_utc.tzinfo is None:
            raise ValueError("posted_at_utc must be timezone-aware UTC")
```

- [ ] **Step 5: Write `market.py`**

```python
# src/xaucopier/domain/market.py
"""Live market state and static account configuration, as frozen values."""
from __future__ import annotations

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

    @classmethod
    def from_dict(cls, data: dict) -> "Policy":
        """Build from a loaded TOML dict, ignoring keys this version does not
        know. Unknown keys are a forward-compatibility concern, not an error;
        a `_points` key has already been rejected by the loader."""
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in fields})
```

`market.py` needs `import dataclasses` at the top for this.

- [ ] **Step 6: Write `intent.py`**

```python
# src/xaucopier/domain/intent.py
"""What the planner emits. An intent is a decision, not an action."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from xaucopier.domain.errors import RejectReason
from xaucopier.domain.signal import Side


class OrderKind(Enum):
    MARKET = "market"
    LIMIT = "limit"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    account_id: str
    symbol: str
    side: Side
    kind: OrderKind
    volume: float
    price: float
    sl: float
    tp: float
    leg_index: int
    magic: int
    comment: str
    deviation_points: int
    expires_at_utc: datetime | None = None


@dataclass(frozen=True, slots=True)
class ModifyIntent:
    account_id: str
    position_ticket: int
    sl: float
    tp: float
    leg_index: int


@dataclass(frozen=True, slots=True)
class CloseIntent:
    account_id: str
    position_ticket: int
    volume: float
    leg_index: int


@dataclass(frozen=True, slots=True)
class Plan:
    accepted: bool
    reason: RejectReason | None
    legs: tuple[OrderIntent, ...]
    notes: tuple[str, ...] = ()

    @classmethod
    def rejected(cls, reason: RejectReason, notes: tuple[str, ...] = ()) -> "Plan":
        return cls(accepted=False, reason=reason, legs=(), notes=notes)

    @classmethod
    def accept(cls, legs: tuple[OrderIntent, ...], notes: tuple[str, ...] = ()) -> "Plan":
        return cls(accepted=True, reason=None, legs=legs, notes=notes)
```

- [ ] **Step 7: Run test to verify it passes**

Run: `python -m pytest tests/test_domain_types.py -v`
Expected: 9 passed

- [ ] **Step 8: Commit**

```bash
git add src/xaucopier/domain tests/test_domain_types.py
git commit -m "feat: frozen domain types for signals, market state and intents

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Text normalisation

**Files:**
- Create: `src/xaucopier/parsing/__init__.py`, `src/xaucopier/parsing/normalize.py`
- Test: `tests/test_normalize.py`

**Interfaces:**
- Consumes: nothing
- Produces: `normalize(text: str) -> str`

- [ ] **Step 1: Write the failing test**

The first test uses the real message captured on 2026-09-05, emoji included.

```python
# tests/test_normalize.py
from xaucopier.parsing.normalize import normalize

REAL = (
    "\U0001F680 XAUUSD | BUY SIGNAL \U0001F7E2\n"
    "\U0001F4CD 4480-4485\n"
    "\U0001F534 SL: 4470\n"
    "\U0001F3AF TP1: 4490\n"
    "\U0001F3AF TP4 :4505\n"
)

def test_strips_emoji_and_keeps_lines():
    out = normalize(REAL)
    assert out.splitlines()[0] == "XAUUSD | BUY SIGNAL"
    assert "4480-4485" in out.splitlines()[1]

def test_strips_zero_width_and_nbsp():
    assert normalize("SL:\u200b\u00a04470") == "SL: 4470"

def test_collapses_internal_whitespace():
    assert normalize("TP4   :4505") == "TP4 :4505"

def test_removes_thousands_separator():
    assert normalize("SL: 4,480") == "SL: 4480"

def test_converts_decimal_comma():
    assert normalize("SL: 4480,25") == "SL: 4480.25"

def test_folds_fullwidth_digits():
    assert normalize("\uff14\uff14\uff18\uff10") == "4480"

def test_drops_blank_lines():
    assert normalize("A\n\n\nB") == "A\nB"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_normalize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'xaucopier.parsing.normalize'`

- [ ] **Step 3: Write the implementation**

```python
# src/xaucopier/parsing/normalize.py
"""Fold a raw Telegram message into a canonical form the regexes can match.

Runs before every parser. Deliberately conservative: it removes decoration
and normalises separators, and changes nothing that could alter a number's
value except the two separator rules, which are anchored on digit context.
"""
from __future__ import annotations

import re
import unicodedata

# Unicode categories: So = symbol/other (most emoji), Cf = format
# (zero-width joiners, variation selectors, direction marks).
_DECORATION = {"So", "Cf"}

_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d{1,2}(?!\d))")
_WS_RUN = re.compile(r"[ \t]+")


def normalize(text: str) -> str:
    # NFKC folds full-width digits and compatibility forms to ASCII.
    text = unicodedata.normalize("NFKC", text)
    text = "".join(
        " " if ch in "\u00a0\u2007\u202f" else ch
        for ch in text
        if unicodedata.category(ch) not in _DECORATION
    )
    # Thousands first: 4,480 -> 4480. Then decimal: 4480,25 -> 4480.25.
    text = _THOUSANDS.sub("", text)
    text = _DECIMAL_COMMA.sub(".", text)

    lines = []
    for raw_line in text.splitlines():
        line = _WS_RUN.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_normalize.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/xaucopier/parsing tests/test_normalize.py
git commit -m "feat: message normalisation with emoji strip and separator folding

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: MARKET MASTERY HUB parser

**Files:**
- Create: `src/xaucopier/parsing/result.py`, `src/xaucopier/parsing/channel_mmh.py`, `src/xaucopier/parsing/registry.py`
- Create: `tests/fixtures/messages/mmh_buy_4tp.json`
- Test: `tests/test_parser_mmh.py`

**Interfaces:**
- Consumes: `normalize()` (Task 4), `Signal`, `Side`, `EntryZone` (Task 3), `RejectReason` (Task 3)
- Produces: `ParseResult(ok: bool, signal: Signal | None, reason: RejectReason | None, detail: str)`, `parse(text, channel_id, message_id, posted_at_utc) -> ParseResult`, `parser_for(channel_id) -> Callable`

- [ ] **Step 1: Save the real message as a fixture**

```json
{
  "channel_id": -1001111111111,
  "message_id": 4001,
  "posted_at_utc": "2026-09-05T10:45:00+00:00",
  "text": "\ud83d\ude80 XAUUSD | BUY SIGNAL \ud83d\udfe2\n\ud83d\udccd 4480-4485\n\ud83d\udd34 SL: 4470\n\ud83c\udfaf TP1: 4490\n\ud83c\udfaf TP2: 4495\n\ud83c\udfaf TP3: 4500\n\ud83c\udfaf TP4 :4505\n\u26a0\ufe0f Manage your risk. Trade with discipline.",
  "expect": "accept"
}
```

- [ ] **Step 2: Write the failing test**

`test_parses_tp4_with_space_before_colon` is the one that matters — the real channel wrote `TP4 :4505`, and a naive regex silently drops it, turning a 4-leg trade into 3.

```python
# tests/test_parser_mmh.py
import json
from datetime import datetime, UTC
from pathlib import Path
import pytest

from xaucopier.parsing.channel_mmh import parse
from xaucopier.domain.signal import Side
from xaucopier.domain.errors import RejectReason

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/messages/mmh_buy_4tp.json").read_text(encoding="utf-8")
)
POSTED = datetime(2026, 9, 5, 10, 45, tzinfo=UTC)

def _parse(text: str):
    return parse(text, channel_id=-1001111111111, message_id=4001, posted_at_utc=POSTED)

def test_parses_the_real_message():
    r = _parse(FIXTURE["text"])
    assert r.ok, r.detail
    assert r.signal.side is Side.BUY
    assert r.signal.symbol_alias == "XAUUSD"
    assert (r.signal.zone.lo, r.signal.zone.hi) == (4480.0, 4485.0)
    assert r.signal.sl == 4470.0

def test_parses_tp4_with_space_before_colon():
    r = _parse(FIXTURE["text"])
    assert r.signal.tps == (4490.0, 4495.0, 4500.0, 4505.0)

def test_tps_are_ordered_by_index_not_file_order():
    text = "XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4470\nTP3: 4500\nTP1: 4490\nTP2: 4495"
    r = _parse(text)
    assert r.signal.tps == (4490.0, 4495.0, 4500.0)

def test_parses_sell():
    text = "XAUUSD | SELL SIGNAL\n4485-4480\nSL: 4495\nTP1: 4470"
    r = _parse(text)
    assert r.signal.side is Side.SELL
    assert r.signal.tps == (4470.0,)

def test_missing_sl_is_hard_reject():
    text = "XAUUSD | BUY SIGNAL\n4480-4485\nTP1: 4490"
    r = _parse(text)
    assert r.ok is False
    assert r.reason is RejectReason.PARSE_FAILED

def test_missing_zone_is_hard_reject():
    text = "XAUUSD | BUY SIGNAL\nSL: 4470\nTP1: 4490"
    assert _parse(text).ok is False

def test_no_tps_is_hard_reject():
    text = "XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4470"
    assert _parse(text).ok is False

def test_non_gold_symbol_is_skipped():
    text = "US30 | BUY SIGNAL\n44800-44850\nSL: 44700\nTP1: 44900"
    r = _parse(text)
    assert r.ok is False

def test_chatter_is_rejected_without_raising():
    r = _parse("Good morning traders, be patient today")
    assert r.ok is False
    assert r.reason is RejectReason.PARSE_FAILED

def test_en_dash_zone_is_accepted():
    text = "XAUUSD | BUY SIGNAL\n4480\u20134485\nSL: 4470\nTP1: 4490"
    assert _parse(text).ok is True

def test_content_hash_is_stable_across_cosmetic_edits():
    a = _parse("XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4470\nTP1: 4490").signal
    b = _parse("\U0001F680 XAUUSD | BUY SIGNAL\n\U0001F4CD 4480-4485\nSL:  4470\nTP1: 4490").signal
    assert a.content_hash == b.content_hash

def test_content_hash_changes_when_sl_changes():
    a = _parse("XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4470\nTP1: 4490").signal
    b = _parse("XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4460\nTP1: 4490").signal
    assert a.content_hash != b.content_hash
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_parser_mmh.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'xaucopier.parsing.channel_mmh'`

- [ ] **Step 4: Write `result.py`**

```python
# src/xaucopier/parsing/result.py
from __future__ import annotations

from dataclasses import dataclass

from xaucopier.domain.errors import RejectReason
from xaucopier.domain.signal import Signal


@dataclass(frozen=True, slots=True)
class ParseResult:
    ok: bool
    signal: Signal | None = None
    reason: RejectReason | None = None
    detail: str = ""

    @classmethod
    def fail(cls, detail: str) -> "ParseResult":
        return cls(ok=False, reason=RejectReason.PARSE_FAILED, detail=detail)
```

- [ ] **Step 5: Write `channel_mmh.py`**

```python
# src/xaucopier/parsing/channel_mmh.py
"""Parser for MARKET MASTERY HUB.

Observed format (2026-09-05):

    XAUUSD | BUY SIGNAL
    4480-4485
    SL: 4470
    TP1: 4490
    ...
    TP4 :4505      <- note the space before the colon; tolerate both sides

A partial match is a hard reject. This parser never returns a best-effort
Signal: a template change must surface as a loud parse failure, never as a
trade with a missing leg.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

from xaucopier.domain.signal import EntryZone, Side, Signal
from xaucopier.parsing.normalize import normalize
from xaucopier.parsing.result import ParseResult

NUM = r"\d+(?:\.\d+)?"
GOLD_ALIASES = {"XAUUSD", "GOLD", "XAU"}

_HEAD = re.compile(rf"\b(XAUUSD|GOLD|XAU)\b\s*\|\s*\b(BUY|SELL)\b\s+SIGNAL", re.I)
_ZONE = re.compile(rf"^({NUM})\s*[-\u2013\u2014]\s*({NUM})$", re.M)
_SL = re.compile(rf"\bSL\b\s*:?\s*({NUM})", re.I)
_TP = re.compile(rf"\bTP\s*(\d+)\s*:?\s*({NUM})", re.I)


def _content_hash(side: Side, zone: EntryZone, sl: float, tps: tuple[float, ...]) -> str:
    """Identity of the *trade*, not the message.

    Two messages describing the same call hash the same even if the emoji,
    spacing or wording differ — which is what makes cross-channel relay
    dedup possible.
    """
    payload = "|".join([
        side.value,
        f"{zone.lo:.3f}", f"{zone.hi:.3f}", f"{sl:.3f}",
        ",".join(f"{tp:.3f}" for tp in tps),
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def parse(
    text: str,
    *,
    channel_id: int,
    message_id: int,
    posted_at_utc: datetime,
    source: str = "live",
) -> ParseResult:
    clean = normalize(text)

    head = _HEAD.search(clean)
    if head is None:
        return ParseResult.fail("no 'SYMBOL | SIDE SIGNAL' header")
    alias = head.group(1).upper()
    if alias not in GOLD_ALIASES:
        return ParseResult.fail(f"symbol {alias!r} is not gold")
    side = Side.BUY if head.group(2).upper() == "BUY" else Side.SELL

    zone_match = _ZONE.search(clean)
    if zone_match is None:
        return ParseResult.fail("no entry zone line")
    zone = EntryZone(float(zone_match.group(1)), float(zone_match.group(2)))

    sl_match = _SL.search(clean)
    if sl_match is None:
        return ParseResult.fail("no stop loss")
    sl = float(sl_match.group(1))

    tp_pairs = [(int(i), float(v)) for i, v in _TP.findall(clean)]
    if not tp_pairs:
        return ParseResult.fail("no take profits")
    tps = tuple(value for _, value in sorted(tp_pairs, key=lambda pair: pair[0]))

    signal = Signal(
        channel_id=channel_id,
        message_id=message_id,
        side=side,
        symbol_alias="XAUUSD",
        zone=zone,
        sl=sl,
        tps=tps,
        posted_at_utc=posted_at_utc,
        content_hash=_content_hash(side, zone, sl, tps),
        source=source,
    )
    return ParseResult(ok=True, signal=signal)
```

- [ ] **Step 6: Write `registry.py`**

```python
# src/xaucopier/parsing/registry.py
"""Maps a numeric channel id to its parser. Numeric ids only — a username
can be re-pointed at a different channel, an id cannot.
"""
from __future__ import annotations

from typing import Callable

from xaucopier.parsing import channel_mmh
from xaucopier.parsing.result import ParseResult

_PARSERS: dict[str, Callable[..., ParseResult]] = {
    "mmh": channel_mmh.parse,
}


def parser_for(name: str) -> Callable[..., ParseResult]:
    try:
        return _PARSERS[name]
    except KeyError:
        raise KeyError(f"no parser registered under {name!r}") from None
```

- [ ] **Step 7: Run test to verify it passes**

Run: `python -m pytest tests/test_parser_mmh.py -v`
Expected: 12 passed

- [ ] **Step 8: Commit**

```bash
git add src/xaucopier/parsing tests/test_parser_mmh.py tests/fixtures
git commit -m "feat: MARKET MASTERY HUB parser with hard-reject on partial match

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Follow-up grammar

**Files:**
- Create: `src/xaucopier/parsing/followups.py`
- Test: `tests/test_followup_parse.py`

**Interfaces:**
- Consumes: `normalize()`, `FollowUp`, `FollowUpOp`
- Produces: `parse_followup(text, *, channel_id, message_id, posted_at_utc, reply_to_message_id) -> FollowUp | None`

> **Note for the implementer:** this grammar is designed from the operations, not from observed messages — the user has not yet supplied real follow-up text. Treat every pattern here as provisional. When real messages arrive, add them as fixtures first and adjust the patterns to match; do not adjust the messages to match the patterns.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_followup_parse.py
from datetime import datetime, UTC
import pytest

from xaucopier.parsing.followups import parse_followup
from xaucopier.domain.signal import FollowUpOp

POSTED = datetime(2026, 9, 5, 11, 5, tzinfo=UTC)

def _p(text: str, reply_to: int | None = None):
    return parse_followup(text, channel_id=-1001, message_id=9,
                          posted_at_utc=POSTED, reply_to_message_id=reply_to)

@pytest.mark.parametrize("text", [
    "SL to BE", "sl to breakeven", "Move SL to entry", "BREAKEVEN NOW",
])
def test_breakeven_variants(text):
    assert _p(text).op is FollowUpOp.SL_TO_BE

@pytest.mark.parametrize("text", ["close half", "Close 50%", "take half off"])
def test_close_half_variants(text):
    assert _p(text).op is FollowUpOp.CLOSE_HALF

@pytest.mark.parametrize("text", ["close all", "CLOSE ALL NOW", "close everything"])
def test_close_all_variants(text):
    assert _p(text).op is FollowUpOp.CLOSE_ALL

def test_cancel_pending():
    assert _p("cancel the pending order").op is FollowUpOp.CANCEL_PENDING

def test_amend_sl_captures_price():
    f = _p("SL to 4461")
    assert f.op is FollowUpOp.AMEND_SL
    assert f.price_hint == 4461.0

def test_breakeven_beats_amend_when_both_could_match():
    # "SL to BE" must not be read as an amend with no price.
    assert _p("SL to BE").op is FollowUpOp.SL_TO_BE

def test_reply_id_is_carried_through():
    assert _p("close all", reply_to=4001).reply_to_message_id == 4001

def test_unrelated_chatter_returns_none():
    assert _p("great trade everyone \U0001F525") is None

def test_a_full_signal_is_not_a_followup():
    text = "XAUUSD | BUY SIGNAL\n4480-4485\nSL: 4470\nTP1: 4490"
    assert _p(text) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_followup_parse.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'xaucopier.parsing.followups'`

- [ ] **Step 3: Write the implementation**

```python
# src/xaucopier/parsing/followups.py
"""Grammar for messages that modify a live trade.

Order matters: the first matching rule wins, and SL_TO_BE is tested before
AMEND_SL so that "SL to BE" is never read as an amend with a missing price.
"""
from __future__ import annotations

import re
from datetime import datetime

from xaucopier.domain.signal import FollowUp, FollowUpOp
from xaucopier.parsing.normalize import normalize

NUM = r"\d+(?:\.\d+)?"

# A message that looks like a full entry signal is never a follow-up.
_IS_SIGNAL = re.compile(r"\bSIGNAL\b", re.I)

_RULES: list[tuple[FollowUpOp, re.Pattern[str]]] = [
    (FollowUpOp.SL_TO_BE, re.compile(
        r"\b(?:sl|stop)\b[^\n]{0,20}\b(?:be|breakeven|break\s*even|entry)\b"
        r"|\bbreak\s*even\b", re.I)),
    (FollowUpOp.CLOSE_HALF, re.compile(
        r"\b(?:close|take)\b[^\n]{0,15}\b(?:half|50\s*%)\b", re.I)),
    (FollowUpOp.CLOSE_ALL, re.compile(
        r"\bclose\b[^\n]{0,15}\b(?:all|everything|now)\b", re.I)),
    (FollowUpOp.CANCEL_PENDING, re.compile(
        r"\bcancel\b[^\n]{0,20}\b(?:pending|order|limit)\b", re.I)),
    (FollowUpOp.AMEND_SL, re.compile(
        rf"\b(?:sl|stop\s*loss)\b\s*(?:to|:|=|@)?\s*({NUM})", re.I)),
    (FollowUpOp.AMEND_TP, re.compile(
        rf"\btp\s*\d*\b\s*(?:to|:|=|@)\s*({NUM})", re.I)),
]


def parse_followup(
    text: str,
    *,
    channel_id: int,
    message_id: int,
    posted_at_utc: datetime,
    reply_to_message_id: int | None = None,
) -> FollowUp | None:
    clean = normalize(text)
    if _IS_SIGNAL.search(clean):
        return None

    for op, pattern in _RULES:
        match = pattern.search(clean)
        if match is None:
            continue
        price = None
        if op in (FollowUpOp.AMEND_SL, FollowUpOp.AMEND_TP) and match.groups():
            price = float(match.group(1))
        return FollowUp(
            channel_id=channel_id,
            message_id=message_id,
            op=op,
            posted_at_utc=posted_at_utc,
            reply_to_message_id=reply_to_message_id,
            price_hint=price,
            raw=clean,
        )
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_followup_parse.py -v`
Expected: 15 passed

- [ ] **Step 5: Commit**

```bash
git add src/xaucopier/parsing/followups.py tests/test_followup_parse.py
git commit -m "feat: provisional follow-up grammar, ordered so BE beats amend

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: The gate chain

**Files:**
- Create: `src/xaucopier/domain/gates.py`
- Test: `tests/test_gates.py`

**Interfaces:**
- Consumes: `Signal`, `Side`, `MarketSnapshot`, `Policy`, `SymbolSpec`, `RejectReason`
- Produces: `GateResult(ok: bool, reason: RejectReason | None, detail: str)` and nine predicates: `age_gate`, `tick_freshness_gate`, `spread_gate`, `geometry_gate`, `magnitude_gate`, `invalidation_gate`, `chase_gate`, `stops_distance_gate`, `volume_gate`. Each takes `(signal, spec, snapshot, policy)` except `volume_gate(account, spec)`.

- [ ] **Step 1: Write the failing test**

`test_stops_distance_rejects_rather_than_widens` encodes spec §7.3: under fixed-lot sizing, widening an SL to satisfy the broker's stops level is a strictly larger loss with no volume compensation, so it must reject.

```python
# tests/test_gates.py
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
    tight = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 600, 0)
    r = gates.stops_distance_gate(sig(), tight, snap(), POLICY)
    assert not r.ok and r.reason is RejectReason.STOPS_TOO_TIGHT

def test_volume_gate_rejects_sub_minimum_lot():
    acct = AccountSpec("demo", "XAUUSD", fixed_lot=0.004, magic_base=77001)
    r = gates.volume_gate(acct, SPEC)
    assert not r.ok and r.reason is RejectReason.INVALID_VOLUME

def test_volume_gate_accepts_valid_lot():
    acct = AccountSpec("demo", "XAUUSD", fixed_lot=0.01, magic_base=77001)
    assert gates.volume_gate(acct, SPEC).ok
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gates.py -v`
Expected: FAIL — `ImportError: cannot import name 'gates'`

- [ ] **Step 3: Write the implementation**

```python
# src/xaucopier/domain/gates.py
"""Pre-trade predicates. Pure: no I/O, no clock, no randomness.

`now` always arrives inside the MarketSnapshot so a test can pin it.
"""
from __future__ import annotations

from dataclasses import dataclass

from xaucopier.domain.errors import RejectReason
from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.signal import Side, Signal


@dataclass(frozen=True, slots=True)
class GateResult:
    ok: bool
    reason: RejectReason | None = None
    detail: str = ""


_OK = GateResult(True)


def _no(reason: RejectReason, detail: str) -> GateResult:
    return GateResult(False, reason, detail)


def age_gate(sig: Signal, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> GateResult:
    """Age is measured from the message's own timestamp, never from how it
    was discovered. Gating backfilled messages separately makes the history
    poll catch nothing while logging that it works."""
    age = (snap.now_utc - sig.posted_at_utc).total_seconds()
    if age > pol.max_signal_age_seconds:
        return _no(RejectReason.STALE, f"{age:.0f}s old")
    return _OK


def tick_freshness_gate(sig: Signal, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> GateResult:
    age = (snap.now_utc - snap.tick_time_utc).total_seconds()
    if age > pol.tick_freshness_seconds:
        return _no(RejectReason.TICK_STALE, f"tick {age:.0f}s old")
    return _OK


def spread_gate(sig: Signal, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> GateResult:
    if snap.spread_usd > pol.max_spread_usd:
        return _no(RejectReason.SPREAD_WIDE, f"{snap.spread_usd:.3f} > {pol.max_spread_usd}")
    ceiling = snap.median_spread_usd * pol.spread_median_multiple
    if snap.median_spread_usd > 0 and snap.spread_usd > ceiling:
        return _no(RejectReason.SPREAD_WIDE, f"{snap.spread_usd:.3f} > {ceiling:.3f} (median x{pol.spread_median_multiple})")
    return _OK


def geometry_gate(sig: Signal, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> GateResult:
    """Derive the side from the level layout and cross-check the keyword.

    A poster who writes BUY above sell levels must produce a hard reject,
    never a coin flip.
    """
    mid_zone = (sig.zone.lo + sig.zone.hi) / 2.0
    tps_above = all(tp > sig.zone.hi for tp in sig.tps)
    tps_below = all(tp < sig.zone.lo for tp in sig.tps)
    if not (tps_above or tps_below):
        return _no(RejectReason.GEOMETRY_CONFLICT, "take profits straddle the entry zone")
    derived = Side.BUY if tps_above else Side.SELL
    if derived is not sig.side:
        return _no(RejectReason.GEOMETRY_CONFLICT,
                   f"levels imply {derived.value}, message says {sig.side.value}")
    if derived is Side.BUY and sig.sl >= mid_zone:
        return _no(RejectReason.GEOMETRY_CONFLICT, "buy stop is not below entry")
    if derived is Side.SELL and sig.sl <= mid_zone:
        return _no(RejectReason.GEOMETRY_CONFLICT, "sell stop is not above entry")
    return _OK


def magnitude_gate(sig: Signal, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> GateResult:
    """Every level must sit within a band of the live mid. Catches a parse
    that produced 447 or 44700 instead of 4470."""
    band = snap.mid * pol.magnitude_band_pct / 100.0
    for label, level in [("zone.lo", sig.zone.lo), ("zone.hi", sig.zone.hi),
                         ("sl", sig.sl), *[(f"tp{i+1}", tp) for i, tp in enumerate(sig.tps)]]:
        if abs(level - snap.mid) > band:
            return _no(RejectReason.MAGNITUDE_BAND, f"{label}={level} vs mid {snap.mid:.2f}")
    return _OK


def invalidation_gate(sig: Signal, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> GateResult:
    price = snap.ask if sig.side is Side.BUY else snap.bid
    if sig.side is Side.BUY and price <= sig.sl:
        return _no(RejectReason.SL_BREACHED, f"ask {price} at/below sl {sig.sl}")
    if sig.side is Side.SELL and price >= sig.sl:
        return _no(RejectReason.SL_BREACHED, f"bid {price} at/above sl {sig.sl}")
    if not surviving_tps(sig, snap):
        return _no(RejectReason.ALL_TPS_BEHIND, "price is past every take profit")
    return _OK


def surviving_tps(sig: Signal, snap: MarketSnapshot) -> tuple[float, ...]:
    """Take profits still ahead of price. A leg for a passed TP is pointless."""
    price = snap.ask if sig.side is Side.BUY else snap.bid
    if sig.side is Side.BUY:
        return tuple(tp for tp in sig.tps if tp > price)
    return tuple(tp for tp in sig.tps if tp < price)


def chase_gate(sig: Signal, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> GateResult:
    """How far past the zone price has already run.

    Budget scales with the trade: a fraction of the distance from the zone
    to TP1, floored so tight scalps are not rejected outright.
    """
    price = snap.ask if sig.side is Side.BUY else snap.bid
    near = sig.zone.near_edge(sig.side)
    overshoot = (price - near) if sig.side is Side.BUY else (near - price)
    if overshoot <= 0:
        return _OK
    budget = max(pol.chase_floor_usd, pol.chase_tp1_fraction * abs(sig.tps[0] - near))
    if overshoot > budget:
        return _no(RejectReason.CHASE_EXCEEDED, f"{overshoot:.2f} past zone, budget {budget:.2f}")
    return _OK


def stops_distance_gate(sig: Signal, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> GateResult:
    """Reject a stop the broker will not accept. Never widen it.

    Under fixed-lot sizing a wider stop is a strictly larger dollar loss
    with no volume compensation, so clamping an SL outward silently changes
    the trade. Take profits may be clamped; the stop may not.
    """
    price = snap.ask if sig.side is Side.BUY else snap.bid
    min_dist = max(spec.stops_level_usd, pol.stops_distance_spread_multiple * snap.spread_usd)
    if abs(price - sig.sl) < min_dist:
        return _no(RejectReason.STOPS_TOO_TIGHT,
                   f"sl {abs(price - sig.sl):.2f} from price, min {min_dist:.2f}")
    return _OK


def volume_gate(account: AccountSpec, spec: SymbolSpec) -> GateResult:
    if account.fixed_lot < spec.volume_min:
        return _no(RejectReason.INVALID_VOLUME,
                   f"fixed_lot {account.fixed_lot} below min {spec.volume_min}")
    if account.fixed_lot > spec.volume_max:
        return _no(RejectReason.INVALID_VOLUME,
                   f"fixed_lot {account.fixed_lot} above max {spec.volume_max}")
    steps = account.fixed_lot / spec.volume_step
    if abs(steps - round(steps)) > 1e-9:
        return _no(RejectReason.INVALID_VOLUME,
                   f"fixed_lot {account.fixed_lot} is not a multiple of {spec.volume_step}")
    return _OK
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_gates.py -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add src/xaucopier/domain/gates.py tests/test_gates.py
git commit -m "feat: nine pure pre-trade gates; stops-too-tight rejects, never widens

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: The entry planner

**Files:**
- Create: `src/xaucopier/domain/planner.py`
- Test: `tests/test_planner.py`

**Interfaces:**
- Consumes: everything from Tasks 2, 3, 7
- Produces: `plan_entry(sig: Signal, account: AccountSpec, spec: SymbolSpec, snap: MarketSnapshot, pol: Policy) -> Plan`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_planner.py
from datetime import datetime, timedelta, UTC

from xaucopier.domain.errors import RejectReason
from xaucopier.domain.intent import OrderKind
from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.planner import plan_entry
from xaucopier.domain.signal import EntryZone, Side, Signal

NOW = datetime(2026, 9, 5, 10, 45, tzinfo=UTC)
SPEC = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)
EXNESS = SymbolSpec("XAUUSDm", 3, 0.001, 0.001, 0.01, 100.0, 0.01, 0, 0)
ACCT = AccountSpec("ic_demo", "XAUUSD", fixed_lot=0.01, magic_base=77001)
POLICY = Policy()

def snap(bid, ask, spread=0.10, median=0.10):
    return MarketSnapshot(bid=bid, ask=ask, tick_time_utc=NOW,
                          spread_usd=spread, median_spread_usd=median, now_utc=NOW)

def sig(tps=(4490.0, 4495.0, 4500.0, 4505.0)):
    return Signal(channel_id=-1, message_id=4001, side=Side.BUY,
                  symbol_alias="XAUUSD", zone=EntryZone(4480.0, 4485.0),
                  sl=4470.0, tps=tps, posted_at_utc=NOW - timedelta(seconds=5),
                  content_hash="h")

def test_one_leg_per_tp():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert plan.accepted
    assert len(plan.legs) == 4
    assert [leg.tp for leg in plan.legs] == [4490.0, 4495.0, 4500.0, 4505.0]

def test_every_leg_carries_the_shared_sl_and_fixed_lot():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert {leg.sl for leg in plan.legs} == {4470.0}
    assert {leg.volume for leg in plan.legs} == {0.01}

def test_market_when_price_inside_zone():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert all(leg.kind is OrderKind.MARKET for leg in plan.legs)
    assert plan.legs[0].price == 4482.10

def test_market_when_price_better_than_zone():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4478.0, 4478.10), POLICY)
    assert plan.legs[0].kind is OrderKind.MARKET

def test_limit_at_favourable_edge_when_price_above_zone():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4485.5, 4485.60), POLICY)
    assert all(leg.kind is OrderKind.LIMIT for leg in plan.legs)
    assert plan.legs[0].price == 4480.0  # favourable edge, not 4485

def test_limit_at_near_edge_when_policy_says_so():
    pol = Policy(pending_at="near")
    plan = plan_entry(sig(), ACCT, SPEC, snap(4485.5, 4485.60), pol)
    assert plan.legs[0].price == 4485.0

def test_pending_legs_carry_an_expiry():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4485.5, 4485.60), POLICY)
    assert plan.legs[0].expires_at_utc is not None

def test_market_legs_carry_no_expiry():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert plan.legs[0].expires_at_utc is None

def test_passed_tps_are_dropped_as_legs():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4496.0, 4496.10), POLICY)
    assert [leg.tp for leg in plan.legs] == [4500.0, 4505.0]

def test_rejected_when_no_tp_survives():
    plan = plan_entry(sig(tps=(4490.0,)), ACCT, SPEC, snap(4499.0, 4499.10), POLICY)
    assert not plan.accepted
    assert plan.reason is RejectReason.ALL_TPS_BEHIND

def test_leg_index_is_in_the_comment():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    comments = [leg.comment for leg in plan.legs]
    assert len(set(comments)) == 4
    assert comments[0].endswith("-0")

def test_comment_fits_broker_limit():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert all(len(leg.comment) <= 20 for leg in plan.legs)

def test_magic_is_constant_across_legs():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert {leg.magic for leg in plan.legs} == {77001}

def test_deviation_is_digit_aware():
    two = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    three = plan_entry(sig(), ACCT, EXNESS, snap(4482.0, 4482.10), POLICY)
    assert two.legs[0].deviation_points == 20    # 0.20 USD at point 0.01
    assert three.legs[0].deviation_points == 200  # same USD, 3-digit feed

def test_stale_signal_is_rejected_before_planning():
    old = Signal(channel_id=-1, message_id=1, side=Side.BUY, symbol_alias="XAUUSD",
                 zone=EntryZone(4480.0, 4485.0), sl=4470.0, tps=(4490.0,),
                 posted_at_utc=NOW - timedelta(seconds=900), content_hash="h")
    plan = plan_entry(old, ACCT, SPEC, snap(4482.0, 4482.10), POLICY)
    assert not plan.accepted and plan.reason is RejectReason.STALE

def test_wide_spread_is_rejected():
    plan = plan_entry(sig(), ACCT, SPEC, snap(4482.0, 4483.0, spread=1.0), POLICY)
    assert not plan.accepted and plan.reason is RejectReason.SPREAD_WIDE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_planner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'xaucopier.domain.planner'`

- [ ] **Step 3: Write the implementation**

```python
# src/xaucopier/domain/planner.py
"""Turn one Signal into the set of orders that should exist.

The entire money-losing surface of the system is this function. It is pure,
so a year of recorded messages replays through it offline.
"""
from __future__ import annotations

from datetime import timedelta

from xaucopier.domain import gates
from xaucopier.domain.errors import RejectReason
from xaucopier.domain.intent import OrderIntent, OrderKind, Plan
from xaucopier.domain.market import AccountSpec, MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec, round_price, usd_to_points
from xaucopier.domain.signal import Side, Signal

_SIGNAL_GATES = (
    gates.age_gate,
    gates.tick_freshness_gate,
    gates.spread_gate,
    gates.geometry_gate,
    gates.magnitude_gate,
    gates.invalidation_gate,
    gates.chase_gate,
    gates.stops_distance_gate,
)


def _comment(sig: Signal, leg_index: int) -> str:
    """base36(message_id)-leg. The leg index is mandatory: without it the
    legs of one signal are indistinguishable at the broker, and reconciliation
    cannot tell which position is which."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    n, out = sig.message_id, ""
    while True:
        n, rem = divmod(n, 36)
        out = digits[rem] + out
        if n == 0:
            break
    return f"{out}-{leg_index}"[:20]


def plan_entry(
    sig: Signal,
    account: AccountSpec,
    spec: SymbolSpec,
    snap: MarketSnapshot,
    pol: Policy,
) -> Plan:
    volume = gates.volume_gate(account, spec)
    if not volume.ok:
        return Plan.rejected(volume.reason, (volume.detail,))

    for gate in _SIGNAL_GATES:
        result = gate(sig, spec, snap, pol)
        if not result.ok:
            return Plan.rejected(result.reason, (result.detail,))

    surviving = gates.surviving_tps(sig, snap)
    if not surviving:
        return Plan.rejected(RejectReason.ALL_TPS_BEHIND, ("no take profit ahead of price",))

    price = snap.ask if sig.side is Side.BUY else snap.bid
    kind, entry_price, expires = _entry_for(sig, snap, pol, price)

    legs = tuple(
        OrderIntent(
            account_id=account.account_id,
            symbol=account.symbol,
            side=sig.side,
            kind=kind,
            volume=account.fixed_lot,
            price=round_price(entry_price, spec),
            sl=round_price(sig.sl, spec),
            tp=round_price(tp, spec),
            leg_index=index,
            magic=account.magic_base,
            comment=_comment(sig, index),
            deviation_points=usd_to_points(pol.max_slippage_usd, spec),
            expires_at_utc=expires,
        )
        for index, tp in enumerate(surviving)
    )
    notes = (f"{len(sig.tps) - len(surviving)} take profit(s) already passed",) \
        if len(surviving) != len(sig.tps) else ()
    return Plan.accept(legs, notes)


def _entry_for(sig: Signal, snap: MarketSnapshot, pol: Policy, price: float):
    """Market inside or better than the zone; a limit otherwise.

    Where you fill inside the zone materially changes R: on the observed
    4480-4485 / SL 4470 signal, filling at 4480 makes TP1 a 1.0R target and
    filling at 4485 makes it 0.33R. The default places the limit at the
    favourable edge, trading fill rate for R.
    """
    inside_or_better = (
        price <= sig.zone.hi if sig.side is Side.BUY else price >= sig.zone.lo
    )
    if inside_or_better:
        return OrderKind.MARKET, price, None
    edge = (sig.zone.favourable_edge(sig.side)
            if pol.pending_at == "favourable"
            else sig.zone.near_edge(sig.side))
    expires = sig.posted_at_utc + timedelta(hours=pol.pending_max_age_hours)
    return OrderKind.LIMIT, edge, expires
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_planner.py -v`
Expected: 16 passed

- [ ] **Step 5: Commit**

```bash
git add src/xaucopier/domain/planner.py tests/test_planner.py
git commit -m "feat: entry planner, one leg per surviving TP, limit at favourable edge

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: The follow-up planner

**Files:**
- Create: `src/xaucopier/domain/followup_planner.py`
- Test: `tests/test_followup_planner.py`

**Interfaces:**
- Consumes: `FollowUp`, `FollowUpOp`, `ModifyIntent`, `CloseIntent`, `SymbolSpec`, `MarketSnapshot`, `Policy`
- Produces: `LegView(position_ticket, leg_index, side, volume, price_open, sl, tp, is_pending)` and `plan_followup(fu, legs, spec, snap, pol) -> FollowUpPlan(intents, reason, notes)`

- [ ] **Step 1: Write the failing test**

`test_close_half_picks_furthest_tps_first` and `test_breakeven_uses_actual_fill_not_signal_entry` both encode spec §8.2 — the second matters because each broker fills somewhere different, so the signal's quoted entry is the wrong reference.

```python
# tests/test_followup_planner.py
from datetime import datetime, UTC

from xaucopier.domain.followup_planner import LegView, plan_followup
from xaucopier.domain.intent import CloseIntent, ModifyIntent
from xaucopier.domain.market import MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec
from xaucopier.domain.signal import FollowUp, FollowUpOp, Side

NOW = datetime(2026, 9, 5, 11, 5, tzinfo=UTC)
SPEC = SymbolSpec("XAUUSD", 2, 0.01, 0.01, 0.01, 100.0, 0.01, 0, 0)
POLICY = Policy()
SNAP = MarketSnapshot(bid=4488.0, ask=4488.10, tick_time_utc=NOW,
                      spread_usd=0.10, median_spread_usd=0.10, now_utc=NOW)

def legs(n=3, price_open=4482.10):
    return tuple(
        LegView(position_ticket=1000 + i, leg_index=i, side=Side.BUY, volume=0.01,
                price_open=price_open, sl=4470.0, tp=4490.0 + 5 * i, is_pending=False)
        for i in range(n)
    )

def fu(op):
    return FollowUp(channel_id=-1, message_id=9, op=op, posted_at_utc=NOW)

def test_breakeven_emits_one_modify_per_leg():
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs(), SPEC, SNAP, POLICY)
    assert len(plan.intents) == 3
    assert all(isinstance(i, ModifyIntent) for i in plan.intents)

def test_breakeven_uses_actual_fill_not_signal_entry():
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs(price_open=4483.50), SPEC, SNAP, POLICY)
    # fill + spread + buffer = 4483.50 + 0.10 + 0.10
    assert plan.intents[0].sl == 4483.70

def test_breakeven_preserves_each_legs_own_tp():
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs(), SPEC, SNAP, POLICY)
    assert [i.tp for i in plan.intents] == [4490.0, 4495.0, 4500.0]

def test_breakeven_skipped_when_price_has_not_cleared_it():
    snap = MarketSnapshot(bid=4482.0, ask=4482.10, tick_time_utc=NOW,
                          spread_usd=0.10, median_spread_usd=0.10, now_utc=NOW)
    plan = plan_followup(fu(FollowUpOp.SL_TO_BE), legs(), SPEC, snap, POLICY)
    assert plan.intents == ()

def test_close_half_closes_ceil_of_open_legs():
    plan = plan_followup(fu(FollowUpOp.CLOSE_HALF), legs(3), SPEC, SNAP, POLICY)
    assert len(plan.intents) == 2

def test_close_half_picks_furthest_tps_first():
    plan = plan_followup(fu(FollowUpOp.CLOSE_HALF), legs(3), SPEC, SNAP, POLICY)
    assert {i.position_ticket for i in plan.intents} == {1002, 1001}

def test_close_half_closes_whole_legs_never_fractions():
    plan = plan_followup(fu(FollowUpOp.CLOSE_HALF), legs(3), SPEC, SNAP, POLICY)
    assert all(i.volume == 0.01 for i in plan.intents)

def test_close_all_covers_every_leg():
    plan = plan_followup(fu(FollowUpOp.CLOSE_ALL), legs(4), SPEC, SNAP, POLICY)
    assert len(plan.intents) == 4
    assert all(isinstance(i, CloseIntent) for i in plan.intents)

def test_cancel_pending_only_touches_pending_legs():
    mixed = legs(2) + (
        LegView(position_ticket=2000, leg_index=2, side=Side.BUY, volume=0.01,
                price_open=0.0, sl=4470.0, tp=4500.0, is_pending=True),
    )
    plan = plan_followup(fu(FollowUpOp.CANCEL_PENDING), mixed, SPEC, SNAP, POLICY)
    assert [i.position_ticket for i in plan.intents] == [2000]

def test_no_legs_is_a_noop_not_an_exception():
    plan = plan_followup(fu(FollowUpOp.CLOSE_ALL), (), SPEC, SNAP, POLICY)
    assert plan.intents == ()
    assert plan.reason is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_followup_planner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'xaucopier.domain.followup_planner'`

- [ ] **Step 3: Write the implementation**

```python
# src/xaucopier/domain/followup_planner.py
"""Turn a follow-up command into intents against the legs that actually exist.

LegView is read from the broker, never from our own database — the DB can be
stale, the broker cannot. Ambiguity always resolves to doing nothing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from xaucopier.domain.errors import RejectReason
from xaucopier.domain.intent import CloseIntent, ModifyIntent
from xaucopier.domain.market import MarketSnapshot, Policy
from xaucopier.domain.money import SymbolSpec, round_price
from xaucopier.domain.signal import FollowUp, FollowUpOp, Side


@dataclass(frozen=True, slots=True)
class LegView:
    """Live broker truth for one leg."""
    position_ticket: int
    leg_index: int
    side: Side
    volume: float
    price_open: float
    sl: float
    tp: float
    is_pending: bool


@dataclass(frozen=True, slots=True)
class FollowUpPlan:
    intents: tuple
    reason: RejectReason | None = None
    notes: tuple[str, ...] = ()


def _breakeven_price(leg: LegView, snap: MarketSnapshot, pol: Policy, spec: SymbolSpec) -> float:
    """Breakeven is the leg's OWN fill plus costs, never the signal's quoted
    entry — each broker filled somewhere different."""
    offset = snap.spread_usd + pol.be_buffer_usd
    raw = leg.price_open + offset if leg.side is Side.BUY else leg.price_open - offset
    return round_price(raw, spec)


def plan_followup(
    fu: FollowUp,
    legs: tuple[LegView, ...],
    spec: SymbolSpec,
    snap: MarketSnapshot,
    pol: Policy,
) -> FollowUpPlan:
    open_legs = tuple(leg for leg in legs if not leg.is_pending)
    pending_legs = tuple(leg for leg in legs if leg.is_pending)

    if not legs:
        return FollowUpPlan((), RejectReason.NOT_APPLICABLE, ("no legs for this signal",))

    if fu.op is FollowUpOp.SL_TO_BE:
        intents = []
        for leg in open_legs:
            be = _breakeven_price(leg, snap, pol, spec)
            price = snap.bid if leg.side is Side.BUY else snap.ask
            # Refuse a stop that is already on the wrong side of the market.
            if leg.side is Side.BUY and be >= price:
                continue
            if leg.side is Side.SELL and be <= price:
                continue
            intents.append(ModifyIntent(account_id="", position_ticket=leg.position_ticket,
                                        sl=be, tp=leg.tp, leg_index=leg.leg_index))
        return FollowUpPlan(tuple(intents))

    if fu.op is FollowUpOp.CLOSE_HALF:
        if not open_legs:
            return FollowUpPlan((), RejectReason.NOT_APPLICABLE, ("nothing open",))
        count = math.ceil(len(open_legs) / 2)
        # Furthest take profits first: keep the legs nearest to paying out.
        ordered = sorted(open_legs, key=lambda leg: leg.tp, reverse=True)
        chosen = ordered[:count]
        return FollowUpPlan(tuple(
            CloseIntent(account_id="", position_ticket=leg.position_ticket,
                        volume=leg.volume, leg_index=leg.leg_index)
            for leg in chosen
        ))

    if fu.op is FollowUpOp.CLOSE_ALL:
        return FollowUpPlan(tuple(
            CloseIntent(account_id="", position_ticket=leg.position_ticket,
                        volume=leg.volume, leg_index=leg.leg_index)
            for leg in legs
        ))

    if fu.op is FollowUpOp.CANCEL_PENDING:
        if not pending_legs:
            return FollowUpPlan((), RejectReason.NOT_APPLICABLE, ("nothing pending",))
        return FollowUpPlan(tuple(
            CloseIntent(account_id="", position_ticket=leg.position_ticket,
                        volume=leg.volume, leg_index=leg.leg_index)
            for leg in pending_legs
        ))

    if fu.op in (FollowUpOp.AMEND_SL, FollowUpOp.AMEND_TP):
        if fu.price_hint is None:
            return FollowUpPlan((), RejectReason.AMBIGUOUS_TARGET, ("amend without a price",))
        new = round_price(fu.price_hint, spec)
        price = snap.bid if open_legs and open_legs[0].side is Side.BUY else snap.ask
        intents = []
        for leg in open_legs:
            if fu.op is FollowUpOp.AMEND_SL:
                if leg.side is Side.BUY and new >= price:
                    return FollowUpPlan((), RejectReason.GEOMETRY_CONFLICT,
                                        (f"amended buy stop {new} is at/above price {price}",))
                if leg.side is Side.SELL and new <= price:
                    return FollowUpPlan((), RejectReason.GEOMETRY_CONFLICT,
                                        (f"amended sell stop {new} is at/below price {price}",))
                intents.append(ModifyIntent("", leg.position_ticket, new, leg.tp, leg.leg_index))
            else:
                intents.append(ModifyIntent("", leg.position_ticket, leg.sl, new, leg.leg_index))
        return FollowUpPlan(tuple(intents))

    return FollowUpPlan((), RejectReason.NOT_APPLICABLE, (f"unhandled op {fu.op}",))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_followup_planner.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/xaucopier/domain/followup_planner.py tests/test_followup_planner.py
git commit -m "feat: follow-up planner; BE from actual fill, close-half by whole legs

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: Replay harness and golden file

**Files:**
- Create: `src/xaucopier/replay/__init__.py`, `src/xaucopier/replay/harness.py`
- Create: `tests/fixtures/expected_decisions.json`
- Test: `tests/test_replay.py`

**Interfaces:**
- Consumes: `parse()`, `plan_entry()`, all domain types
- Produces: `Decision(message_id, accepted, reason, leg_count, entry_kind, prices)` and `replay(fixtures: list[dict], account, spec, pol) -> list[Decision]`

- [ ] **Step 1: Write the failing test**

This is the regression net: a parser or planner change that would have altered a historical decision shows up as a diff against the golden file.

```python
# tests/test_replay.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_replay.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'xaucopier.replay.harness'`

- [ ] **Step 3: Write the implementation**

```python
# src/xaucopier/replay/harness.py
"""Replay recorded messages through parse -> plan and report the decision.

The MT5 Strategy Tester cannot exercise any of the logic in domain/, so this
is the only regression net the money-critical path has. Every message ever
received is stored verbatim and replayed on every commit.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
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

    from datetime import timedelta
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
```

- [ ] **Step 4: Generate the golden file**

Run: `python -m xaucopier.replay.harness --write-golden`
Expected: `wrote 1 decisions`

Inspect `tests/fixtures/expected_decisions.json` by hand before committing it. It should show `message_id: 4001`, `accepted: true`, `leg_count: 4`, `entry_kind: "market"`. **If it does not, the bug is upstream — fix it, do not adjust the golden file.**

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_replay.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add src/xaucopier/replay tests/test_replay.py tests/fixtures/expected_decisions.json
git commit -m "feat: replay harness with golden-file regression net

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11: Purity enforcement

**Files:**
- Test: `tests/test_purity.py`

**Interfaces:**
- Consumes: the `src/xaucopier/domain` and `src/xaucopier/parsing` trees
- Produces: nothing — this task only adds a guard

- [ ] **Step 1: Write the failing test**

Written last so it fails only if a previous task smuggled in an import. It should pass immediately; if it doesn't, an earlier task is wrong.

```python
# tests/test_purity.py
import ast
from pathlib import Path
import pytest

FORBIDDEN = {"telethon", "MetaTrader5", "sqlite3", "requests", "socket",
             "urllib", "http", "subprocess", "threading", "asyncio"}
PURE_DIRS = ["domain", "parsing"]
ROOT = Path(__file__).resolve().parents[1] / "src" / "xaucopier"

def _pure_files():
    for name in PURE_DIRS:
        yield from sorted((ROOT / name).rglob("*.py"))

def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots

@pytest.mark.parametrize("path", list(_pure_files()), ids=lambda p: p.name)
def test_no_io_imports_in_pure_core(path: Path):
    offending = _imported_roots(path) & FORBIDDEN
    assert not offending, f"{path.name} imports {sorted(offending)}; domain/ and parsing/ must stay pure"

def test_no_open_calls_in_pure_core():
    for path in _pure_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "open", f"{path.name} calls open()"

def test_pure_core_has_files_to_check():
    # Guards against the parametrize silently collecting nothing.
    assert len(list(_pure_files())) >= 8
```

- [ ] **Step 2: Run the full suite**

Run: `python -m pytest -v`
Expected: all tests pass, including the purity checks.

- [ ] **Step 3: Commit**

```bash
git add tests/test_purity.py
git commit -m "test: enforce the pure-core boundary by AST walk

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Definition of done

- `python -m pytest` passes with ~90 tests across 9 files.
- `python -m xaucopier.replay.harness` prints the decision for every recorded message.
- No file under `domain/` or `parsing/` imports an I/O library.
- Nothing in this plan imports `MetaTrader5` or `telethon`, and nothing places an order.

## What this plan does NOT cover

Deliberately deferred to Plans 2 and 3:

- Telegram session, listener, backfill, edit/delete handling (Plan 2)
- SQLite store, outbox, dedup across channels (Plan 2)
- MT5 session, preflight, **account-identity assertion (spec G1)**, symbol resolution, order sending, reconciliation, kill switch, supervisor (Plan 3)

The account-identity assertion is the single highest-risk item in the spec and belongs with the code that can actually send an order. It is not forgotten.

## Known gap

Task 6's follow-up grammar is written from the operations rather than from observed messages, because no real follow-up text has been captured yet. Its patterns are provisional. When real messages arrive: add them as fixtures **first**, watch them fail, then adjust the patterns.
