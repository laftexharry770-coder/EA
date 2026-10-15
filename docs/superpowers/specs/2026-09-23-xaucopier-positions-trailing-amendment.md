# Plan 1b — approved design (user-approved 2026-09-23)

To be moved into docs/superpowers/specs/ as a spec amendment when Plan 1b starts,
AFTER Plan 2 (feat/ingest) merges and BEFORE Plan 3. Does not touch Plan 2 files.

## Positions and lot (per account, config/accounts.toml)
- `positions = N` and `lot = L` (lot per position). Omitted -> one position per TP (today).
- Supersedes spec decision #5 ("one position per TP") when set.

## Distribution of N positions over T TPs — STAGGERED (user choice)
- Each position has its own TP and closes there.
- N == T: one per TP.
- N >  T: one per TP; extras on the LAST TP.
- N <  T: the FIRST N TPs (user choice). N=2 of 4 -> TP1,TP2. N=3 -> TP1,TP2,TP3.

## Basket trailing stop — literal ladder as described (user choice), gap $2.00
BUY shown; SELL mirrors (past = below, SL moves down).
- price >= TP1            -> SL = breakeven (EACH position's own fill + spread + buffer)
- price >= TP1 + $2.00    -> SL = TP1
- price >= mid(TP2, TP3)  -> SL = TP2
- price >= TP3 + $2.00    -> SL = TP3
- pattern alternates for more TPs: odd TPn uses "gap past TPn", even TPn uses
  "halfway to TP(n+1)"; no rung for the last TP (last positions close there).
Worked on the real signal (BUY, SL 4470, TPs 4490/4495/4500/4505, 4 positions):
  4490 pos1 closes + SL->BE | 4492 SL->4490 | 4495 pos2 closes |
  4497.50 SL->4495 | 4500 pos3 closes | 4502 SL->4500 | 4505 pos4 closes.

## Rules (controller-imposed, safety only)
- Distances in USD, NEVER pips (gold pip is ambiguous; 2- vs 3-digit brokers = 10x trap).
- Stop only TIGHTENS: always the highest rung whose trigger has been reached.
- Channel "SL to ..." follow-ups may tighten, never loosen; a loosening one is ignored + alert.
- Breakeven is per-position (positions can fill cents apart).

## Where it lives
- config/accounts.py: `positions`, `lot`.
- domain/planner.py: staggered N-position distribution (first-N rule).
- domain/trailing.py (NEW, pure): given side, TPs, per-position fills, price, current
  SLs, policy -> target SL per position; never loosens. Offline-tested on the real signal.
- Replay golden file regenerated and hand-checked.
- Plan 3 worker calls domain/trailing on each tick; sends a modify only when the target tightens.

## AMENDMENT 2026-09-25 — Layered entry (user-approved; supersedes the old "market inside the zone, one limit otherwise" rule)
User's words: "if price is within the range, place the orders and still open positions, but if price is not in
the range open positions at the price nearest to the range and still place orders". Confirmed answers:
- **Split: half and half.** Of the N positions, ceil(N/2) open NOW and floor(N/2) are PENDING limit orders
  (odd N: the extra one opens now; N=1: one position, opens now).
- **TPs: mixed.** Distribute N over the surviving TPs exactly as before (first-N; extras on the last TP), then
  alternate in TP order: position ordinals 0,2,4,... are NOW, 1,3,5,... are PENDING.
  N=4, 4 TPs: NOW -> TP1, TP3; PENDING -> TP2, TP4.  N=3: NOW TP1, TP3; PENDING TP2.
  N=6, 4 TPs (targets TP1,TP2,TP3,TP4,TP4,TP4): NOW TP1,TP3,TP4; PENDING TP2,TP4,TP4.
- **Price inside the zone** (BUY: zone.lo <= ask <= zone.hi): NOW -> market. PENDING -> buy limits spread
  evenly over the part of the zone BETTER than the current price, from just below price (price minus the
  broker's minimum distance) down to the better edge (zone.lo), inclusive of the better edge; one pending order
  goes to the middle of that range. If that range is empty (price within the minimum distance of the better
  edge), treat as "better than the zone" below.
- **Price outside the zone, worse side** (BUY: ask > zone.hi):
  - NOW: market if ask - zone.hi <= the chase budget (max($1.00, 0.25 x (TP1 - zone.hi)) = $1.25 on the real
    signal); otherwise buy limits at the NEAREST edge (zone.hi). ("Market if close, else edge".)
  - PENDING: spread evenly across the WHOLE zone, from the nearest edge to the better edge inclusive
    ("all of the above": near edge, middle, better edge); one pending order goes to the middle.
- **Price better than the whole zone** (BUY: ask < zone.lo): ALL N positions open now at market.
- **Unfilled pending orders are cancelled when price reaches TP1, or after 4 hours** (pending_max_age_hours),
  whichever comes first.
- Every order (market or pending) carries the shared SL and its own TP. The trailing ladder applies to every
  FILLED position (breakeven from each one's own fill). All existing gates still run first (age, spread,
  geometry, invalidation, stops distance); the chase gate no longer rejects the plan — "too far" now means
  "limits at the nearest edge".
- SELL mirrors everything (zone.hi is the better edge, "worse side" is below the zone, bid is the reference).
- A per-account switch `layered_entry = true` (default true) keeps the old single-rule behaviour reachable
  (false) for testing and comparison.

Worked example (acceptance test for Task 2b) — BUY zone 4480-4485, SL 4470, TPs 4490/4495/4500/4505,
positions 4, lot 0.01, minimum distance 0.30:
| ask   | NOW (2 x 0.01)                         | PENDING (2 x 0.01)                         |
|-------|----------------------------------------|--------------------------------------------|
| 4483  | market 4483 -> TP1 4490, TP3 4500      | limits 4482.70, 4480 -> TP2 4495, TP4 4505 |
| 4486  | market 4486 (1.00 <= 1.25) -> TP1, TP3 | limits 4485, 4480 -> TP2, TP4              |
| 4487.50 | limits at 4485 (2.50 > 1.25) -> TP1, TP3 | limits 4485, 4480 -> TP2, TP4          |
| 4479  | market 4479 x 4 -> TP1, TP2, TP3, TP4 (all open now)                               |
Price later reaches 4490 -> every unfilled pending order is cancelled.
