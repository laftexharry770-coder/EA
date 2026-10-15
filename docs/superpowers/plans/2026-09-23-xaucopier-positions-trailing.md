# xaucopier Plan 1b — Position Count, Lot Size and the Basket Trailing Stop

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let the user set how many positions open per signal and the lot size of each, distribute them across the signal's take profits (staggered, first-N), and compute a basket trailing stop that tightens along a ladder as price clears each TP — all in the offline pure core, with no MT5 and no Telegram.

**Architecture:** Pure-core changes only. `config/accounts.py` and `domain/market.py` gain a position count; `domain/intent.py` separates a leg's unique position number (`leg_index`) from the TP it targets (`tp_index`); `domain/planner.py` distributes N positions over the surviving TPs; a new `domain/trailing.py` answers "what should each open position's stop be right now?"; `domain/followup_planner.py` refuses to loosen a stop. Plan 3's MT5 worker will call `trailing` on every tick. Nothing here imports `MetaTrader5` or does I/O; `tests/test_purity.py` already walks `domain/`.

**Tech Stack:** Python 3.14.7, stdlib, pytest.

**Spec:** `docs/superpowers/specs/2026-09-05-telegram-mt5-xauusd-copier-design.md`, plus the user-approved amendment recorded in the section "Approved design" below. The amendment supersedes spec decision #5 ("one position per TP") when `positions` is set.

**Branch:** off `master` after Plan 2 (`feat/ingest`) merges. Name: `feat/positions-trailing`. **Does not touch any Plan 2 file.**

## Approved design (user decisions, 2026-09-23 — verbatim intent, do not relitigate)

- **Positions and lot, per account.** `positions = N` (new) and `fixed_lot` (existing; it already means *lot per position*). Omitted `positions` means one position per TP — today's behaviour.
- **Staggered exits.** Each position carries its own TP and closes there.
  - `N == T` (T = surviving TPs): one position per TP.
  - `N > T`: one per TP, **extras on the last TP**.
  - `N < T`: the **first N TPs**. (The user explicitly chose this over "first and last".)
- **Basket trailing stop — the literal ladder the user described, gap `$2.00`.** For a BUY (SELL mirrors: "past" means below, the stop moves down):
  - bid ≥ TP1 → stop = **breakeven**, per position (its own fill + spread + `be_buffer_usd`)
  - bid ≥ TP1 + gap → stop = TP1
  - bid ≥ midpoint(TP2, TP3) → stop = TP2
  - bid ≥ TP3 + gap → stop = TP3
  - the pattern alternates for more TPs: **odd** TPn uses "gap past TPn", **even** TPn uses "halfway to TP(n+1)". The **last** TP has no rung — the last positions close there.
  - The user confirmed the alternation is deliberate.
- **Safety rules (controller-imposed):**
  - All distances in USD. Never pips — a gold "pip" is ambiguous and the user's brokers quote gold at 2 and 3 digits (the 10x trap).
  - The stop **only tightens**: always the tightest *earned and broker-valid* rung, never looser than the stop already in place.
  - A channel "SL to …" follow-up may tighten the stop, never loosen it. A loosening one is refused with a reason.

### Worked example — the acceptance test for Task 3
Real signal: BUY, zone 4480–4485, SL 4470, TPs 4490 / 4495 / 4500 / 4505. One position filled at 4482.55; spread 0.10; `be_buffer_usd` 0.10; gap 2.00. Breakeven = 4482.55 + 0.10 + 0.10 = **4482.75**.

| bid | target stop |
|---|---|
| 4489.99 | 4470.00 (no rung earned) |
| 4490.00 | **4482.75** (breakeven) |
| 4491.99 | 4482.75 |
| 4492.00 | **4490.00** (TP1) |
| 4497.49 | 4490.00 |
| 4497.50 | **4495.00** (TP2) |
| 4501.99 | 4495.00 |
| 4502.00 | **4500.00** (TP3) |
| then retraces to 4495.00 | **4500.00** — never loosens |

## Global Constraints

- Python 3.14.7. `domain/` and `parsing/` stay pure: no I/O, no clock, no randomness (`tests/test_purity.py` enforces this; `trailing.py` is covered automatically because it lives in `domain/`).
- All distances and thresholds are USD floats. No config key may end in `_points`. `round_price()` is the only price rounding.
- Frozen dataclasses; `tuple`, never `list`.
- All datetimes timezone-aware UTC.
- `bool` is a subclass of `int`: every new integer config field needs the same bool guard `ordinal` and `magic_base` already have.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; commit with `git -c user.name="Herbert" -c user.email="laftexharry770@gmail.com" commit -m "..."`.

## Controller rulings (pre-flight, 2026-10-15; binding, they override the task text where they differ)

- **P1, the lot key.** The lot per position is the existing `fixed_lot`. No new `lot` key is added; the design note's "lot = L" is the same setting.
  - The account loader must now REJECT any unknown key in an account block with a `ConfigError` that names the key. A misspelled `positons = 4`, or a `lot = 0.05`, must never be silently ignored; for `lot`, `lots` and `lot_size` the message suggests `fixed_lot` (lot per position).
  - The shipped `config/accounts.toml` and every existing test config must still load.
  - Cost if wrong: an old config with an extra harmless key stops loading, with a clear message.
- **P2, layered entry is the default even when `positions` is unset.** The user's amendment (2026-09-25) is the newer, binding decision, so `layered_entry` defaults to true, with N = the number of surviving TPs when `positions` is omitted.
  - The Definition of done's "No Plan 2 behaviour changes when positions is unset, apart from the comment suffix", and Task 5's "the only expected change is the comment format", predate it and are superseded.
  - The expected golden changes are therefore the comment format AND the layered NOW/PENDING legs exactly as the amendment's rules give.
  - `layered_entry = false` must reproduce today's legs exactly, apart from the comment suffix. Task 5 adds a golden check of that too: the real fixture planned with `layered_entry = false` gives today's legs with only the comment changed.
  - Cost if wrong: the user expected their configuration to have to opt in. The amendment says default true.
- **P3, the trail replay (Task 5) runs twice.**
  - Once with `layered_entry = false`: all four positions fill at market at one price, reproducing the worked example exactly.
  - Once with `layered_entry = true`: the pending legs fill at their own limit prices once the scripted path reaches them, so their breakeven differs; the path starts at the worked example's ask 4483. Assert each leg's stop and the open count at every step, and that nothing ever loosens.
  - Pending legs still unfilled when the path reaches TP1 are cancelled (`pending_should_cancel`).
- **P4, minimum distance.** "Minimum distance" everywhere in Task 2b and Task 3 is `gates.min_stop_distance_usd(spec, snap, pol)` = max(stops_level_usd, 3 x spread). The amendment's worked table (0.30) is spread 0.10 with stops_level_usd ≤ 0.30. Never a second formula.
- **P5, task order.** The tasks run in the order 1, 2, 2b, 3, 4, 5. Task 2b builds on Task 2's `distribute()`. Task 3 and Task 4 need only Task 1. Task 5 needs all of them.

---
## File Structure

```
src/xaucopier/config/accounts.py     + positions (Task 1)
src/xaucopier/domain/market.py       + AccountSpec.positions, Policy.trail_* (Task 1)
src/xaucopier/domain/intent.py       + OrderIntent.tp_index (Task 1)
src/xaucopier/domain/errors.py       + RejectReason.WOULD_LOOSEN (Task 4)
src/xaucopier/domain/planner.py      distribute() + staggered legs (Task 2)
src/xaucopier/domain/trailing.py     NEW — ladder() and target_stops() (Task 3)
src/xaucopier/domain/followup_planner.py   never-loosen (Task 4)
config/accounts.toml                 commented example (Task 5)
docs/RUNBOOK.md                      how to set positions and lot (Task 5)
tests/test_distribute.py  tests/test_trailing.py  tests/test_trail_replay.py  (+ extended existing tests)
```

---

### Task 1: Configuration and domain types

**Interfaces produced:**
- `AccountCfg.positions: int | None` — `None` (key omitted) means one position per surviving TP, exactly today's behaviour.
- `AccountSpec.positions: int | None = None` in `domain/market.py`.
- `Policy.trail_enabled: bool = True`, `Policy.trail_gap_usd: float = 2.00`.
- `OrderIntent.tp_index: int` — the index into `sig.tps` of the take profit this leg targets. **`leg_index` stays unique per leg**: it becomes the position's ordinal within the plan (0..N-1). Two legs may now share a `tp_index`; they never share a `leg_index`.

**Why `leg_index` and `tp_index` must split.** Until now a leg's `leg_index` was also the TP it targeted, which Plan 1's review made the index into `sig.tps` so crash reconciliation could tell legs apart. With `N > T`, several positions target the last TP, so one number can no longer be both. Both are persisted by Plan 3's write-ahead intent, so identity survives a crash.

**Invariants, each with a test:**
- `positions` is an `int`, **not a bool** (same guard as `ordinal`/`magic_base`), `>= 1`, and `<= 20` — a sanity cap so a typo like `positions = 100` cannot open a hundred positions. Out of range is a `ConfigError` naming the value.
- `trail_gap_usd > 0`. No key ends in `_points` (the existing loader already enforces this).
- An account file without `positions` still loads, with `positions is None`.

---

### Task 2: Staggered distribution in the planner

**Interface:** a pure helper in `domain/planner.py`:

```python
def distribute(
    n_positions: int | None,
    surviving: tuple[tuple[int, float], ...],   # (tp_index, tp) in TP order
) -> tuple[tuple[int, float], ...]:             # one (tp_index, tp) per position
```

- `None` → `surviving` unchanged (one per TP).
- `N >= T` → `surviving` plus `N - T` copies of the **last** surviving entry.
- `N < T` → the **first N** of `surviving`.

`plan_entry` builds one leg per entry, with `leg_index` = position ordinal, `tp_index` from `distribute`, `volume = account.fixed_lot`, and the shared SL. **The comment becomes `{base36}-{leg_index}t{tp_index}`**, so a position is self-describing even if its database row is lost. `_comment` keeps truncating the base36 *prefix*, never the suffix.

**Distribute over SURVIVING TPs**, not all of them — a position aimed at a TP price has already passed would close at once or be rejected.

**Tests (write first):**

| positions | surviving TPs | expected leg TPs |
|---|---|---|
| `None` | 4490, 4495, 4500, 4505 | 4490, 4495, 4500, 4505 (unchanged) |
| 4 | same | 4490, 4495, 4500, 4505 |
| 6 | same | 4490, 4495, 4500, 4505, 4505, 4505 |
| 2 | same | 4490, 4495 |
| 1 | same | 4490 |
| 3 | 4495, 4500, 4505 (TP1 passed) | 4495, 4500, 4505 |
| 5 | 4505 only | 4505 × 5 |

Also: `leg_index` values are exactly `0..N-1`; `tp_index` is the index into the original `sig.tps`; every comment is unique and fits 20 characters for a very large `message_id`; the SELL mirror; every leg carries `fixed_lot`. **All existing planner tests stay green** — `positions = None` is today's behaviour; only the comment format changes.

---

### Task 2b: Layered entry — some positions now, some as pending orders (user amendment 2026-09-25)

The binding rules and worked table are in `docs/superpowers/specs/2026-09-23-xaucopier-positions-trailing-amendment.md` → "AMENDMENT 2026-09-25 — Layered entry". **Read it first; it supersedes `_entry_for`'s single rule.**

**Interfaces:**
- `AccountCfg.layered_entry: bool = True` / `AccountSpec.layered_entry: bool = True`. This is a real bool, with the same isinstance guard as `enabled` and `allow_live`. False keeps today's single rule.
- `split_now_pending(targets) -> tuple[tuple[bool, int, float], ...]`: pure. It alternates over `distribute()`'s output in TP order: ordinal 0, 2, 4... are NOW, and 1, 3, 5... are PENDING.
- `pending_levels(side, zone, top, k) -> tuple[float, ...]`: pure. k≥2 gives k levels evenly spaced from `top` to the better edge, both inclusive. k=1 gives the midpoint. Every level goes through `round_price`.
- `plan_entry` emits one leg per position:
  - NOW legs are MARKET, or LIMIT at the nearest edge when the overshoot exceeds the chase budget.
  - PENDING legs are LIMIT at their spread level.
  - Every leg carries the shared SL, its own `tp_index`/TP, `fixed_lot`, and `expires = posted_at + pending_max_age_hours` on each LIMIT.
- `pending_should_cancel(side, tps, bid, ask, now, expires) -> bool`: pure. It is true once price has reached TP1 (bid ≥ TP1 for a BUY; ask ≤ TP1 for a SELL), or once `now >= expires`. Plan 3 calls it every tick.

**Rules that are easy to get wrong:**
- **A pending BUY limit must sit at least the broker minimum distance BELOW the current ask** (`gates.min_stop_distance_usd`). A SELL limit sits at least that distance above the bid. When price is inside the zone, `top` = price − min distance for a BUY. If `top` ≤ the better edge, the case becomes "better than the zone": all positions open now.
- The chase budget is the existing chase gate's formula. Beyond it, the NOW half becomes limits at the nearest edge; the plan is **not** rejected.
- All other gates still run first, and a rejection there still rejects the whole signal.
- N=1 means one NOW position and no pending.

**Tests (write first):**
- Every row of the amendment's worked table.
- The SELL mirror of each row.
- N = 1, 2, 3, 5 and 6 with the alternation shown in the amendment.
- A pending level that would fall inside the minimum distance.
- `layered_entry = false` reproduces today's `_entry_for` exactly.
- `pending_should_cancel` at TP1 − 0.01, at TP1, and at the 4 h expiry boundary.
- Unique `leg_index` values and comments across NOW and PENDING legs.

**Replay:** regenerate the golden file (Task 5) with the default `layered_entry = true`, and hand-check the four worked rows.

---
### Task 3: The basket trailing stop — `domain/trailing.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class Rung:
    trigger: float          # price at which this rung is earned
    stop: float | None      # new stop price; None means breakeven, computed per position
    label: str              # "BE", "TP1", "TP2", ... for logs and alerts

def ladder(side: Side, tps: tuple[float, ...], gap_usd: float) -> tuple[Rung, ...]

@dataclass(frozen=True, slots=True)
class OpenLeg:
    position_ticket: int
    price_open: float       # this position's own fill
    current_sl: float

@dataclass(frozen=True, slots=True)
class StopMove:
    position_ticket: int
    new_sl: float
    rung: str

def target_stops(
    side: Side, tps: tuple[float, ...], legs: tuple[OpenLeg, ...],
    snap: MarketSnapshot, spec: SymbolSpec, pol: Policy,
) -> tuple[StopMove, ...]
```

**`ladder` — the approved rule.** Given T take profits (TP1..TPT, in the poster's numbering):
- rung **BE**: trigger TP1, `stop=None`
- for n = 1 .. T-1:
  - n odd: trigger `TPn + gap` (BUY) or `TPn - gap` (SELL); stop `TPn`
  - n even: trigger `midpoint(TPn, TP(n+1))`; stop `TPn`
- no rung for TPT (the last positions close there).

TPs that are **not strictly monotonic in the trade's direction** (for a BUY, TP1 < TP2 < …) make the ladder ill-defined. Return `()` — no trailing, never a guess — and let the caller report it. Entry acceptance is **not** changed; the poster's TP numbering is used as written.

**`target_stops` — the rule for each open leg:**
- the price is **bid for a BUY, ask for a SELL** (the price you would exit at), the same convention `followup_planner` uses;
- a rung is *earned* once the price has reached its trigger (BUY `>=`, SELL `<=`);
- for each leg, the candidate stops are every earned rung's stop, with `BE = price_open ± (spread_usd + be_buffer_usd)` for that leg;
- keep only candidates the **broker will accept**: at least `gates.min_stop_distance_usd(spec, snap, pol)` from the price, on the correct side;
- take the **tightest** valid candidate (BUY: max, SELL: min);
- emit a `StopMove` **only if it is strictly tighter than `current_sl`**. The stop never loosens;
- every price goes through `round_price(…, spec)`.
- `pol.trail_enabled == False` → `()`.

Reuse `gates.min_stop_distance_usd`; do not duplicate the formula.

**Tests (write first) — the worked example is the acceptance test:**
- every row of the worked-example table in "Approved design", one assertion per row, including the retrace that must **not** loosen;
- the exact SELL mirror of that table;
- two legs with fills 4482.55 and 4483.10 get **different** breakeven stops at 4490;
- a rung whose stop would sit inside `min_dist` of the price is skipped in favour of the next-tightest valid rung — widen the spread in the snapshot to force it;
- 1-TP and 2-TP signals produce the right, short ladders; a 5-TP signal alternates (TP4 even → halfway to TP5);
- non-monotonic TPs → `ladder()` returns `()` and `target_stops` returns `()`;
- a 3-digit `SymbolSpec` (Exness) rounds stops to 3 decimals and yields the same levels in USD — **the 10x trap test**;
- `trail_enabled=False` → `()`.

---

### Task 4: Follow-ups can tighten the stop, never loosen it

**Interfaces:** `RejectReason.WOULD_LOOSEN = "would_loosen"`.

`plan_followup` already sees each leg's live stop (`LegView.sl`):
- **`AMEND_SL`** — per leg, skip it if the new stop is looser than `LegView.sl` (BUY: lower; SELL: higher). If every leg is skipped, return `WOULD_LOOSEN` with a note naming both prices. Otherwise proceed as today for the legs it tightens.
- **`SL_TO_BE`** — skip a leg whose current stop is already tighter than its breakeven. This is the normal case once the trail has passed TP1, so it is **not** an error: return `NOT_APPLICABLE` with a note if no leg moves.

**Tests:** an amend below a BUY basket already trailed to TP1 → `WOULD_LOOSEN`, no intents; an amend above it → applied; mixed legs (the per-position BE left some looser) → only the legs it tightens; `SL_TO_BE` after the trail reached TP1 → `NOT_APPLICABLE`, no intents; each SELL mirror. All existing follow-up tests stay green.

---

### Task 5: Replay, golden file, runbook

- Regenerate `tests/fixtures/expected_decisions.json` with `--write-golden`, then **read it and hand-check it**. For the real fixture the only expected change is the comment format (`-0` → `-0t0`, etc.). **Any other difference is a bug upstream — investigate, never paste it into the golden file.**
- **`tests/test_trail_replay.py`** — the end-to-end acceptance test. Take the real fixture signal, plan it with `positions = 4`, simulate fills, then walk a scripted bid path (4488 → 4490 → 4492 → 4497.50 → 4502 → back to 4495). At each step, apply `target_stops` and close any position whose TP was reached. Assert the stops and the open-position count match the worked example: 4 → 3 → 2 → 1 open, stops BE → 4490 → 4495 → 4500, and no loosening on the retrace.
- `config/accounts.toml`: add a **commented** example (`# positions = 4   # how many positions to open per signal`) next to `fixed_lot`, with a comment that `fixed_lot` is the lot size **per position**. Leave `positions` unset, so behaviour does not change until the user chooses.
- `docs/RUNBOOK.md`: a short plain-English section, "Choosing how many positions and what lot size", explaining the two settings, the staggered rule, what the trailing stop does (the worked example as a plain table), and that total exposure is positions × lot.

---

## Definition of done
- Suite green; every task's mutation check kills its mutants. At minimum: `distribute` puts extras on the first TP instead of the last; the ladder uses "gap past" for an even TP; `target_stops` loosens on a retrace; `target_stops` ignores `min_dist`; the amend guard lets a loosening amend through; the trail uses ask for a BUY.
- `test_trail_replay` reproduces the worked example exactly.
- No Plan 2 behaviour changes when `positions` is unset, apart from the comment suffix.

## Deferred to Plan 3 (the MT5 worker)
- Calling `target_stops` on every tick and sending `TRADE_ACTION_SLTP` only for the returned moves.
- Rate-limiting modifies (a fast market must not send one per tick).
- Persisting the achieved rung so a restart does not recompute from scratch — though `target_stops` is safe to recompute, because it never loosens relative to the broker's live stop.
