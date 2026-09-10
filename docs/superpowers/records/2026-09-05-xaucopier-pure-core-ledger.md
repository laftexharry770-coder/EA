# SDD ledger — plan: C:/Users/Herbert/Projects/xaucopier/docs/superpowers/plans/2026-09-05-xaucopier-pure-core.md

Spec: docs/superpowers/specs/2026-09-05-telegram-mt5-xauusd-copier-design.md (read, binding)
Branch: feat/pure-core  Base: 9db8ccf

## Pre-flight conflict scan

### Cross-task rows (shared file or interface)
| Tasks | Produces -> Consumes | Finding |
|---|---|---|
| T1 -> T3 | load_policy_dict() -> dict; Policy.from_dict() | clean (fixed pre-plan) |
| T2 -> T7,T8,T9 | SymbolSpec field order/names | clean |
| T3 -> T5,T7,T8,T9 | Signal/MarketSnapshot/Policy/intents | clean |
| T4 -> T5,T6 | normalize() | clean |
| T5 -> T10 | parse() keyword-only signature | clean |
| T5 -> (nobody) | registry.parser_for() | DEFECT: dead code in this plan |
| T7 -> T8 | gates.surviving_tps() | clean |
| T8 -> T10 | plan_entry() | clean |
| T9 -> T3 | ModifyIntent/CloseIntent positional order | clean; account_id="" placeholder noted |
| T11 -> T2..T9 | forbidden-import list vs actual imports | clean (re, unicodedata, hashlib, math, dataclasses, datetime, enum only) |

### Per-task self-consistency rows
| Task | Tests vs code it specifies | Finding |
|---|---|---|
| T1 | 3 tests vs loader | clean |
| T2 | 7 tests vs money.py | clean |
| T3 | 9 tests vs 4 modules | clean |
| T4 | 7 tests vs normalize | clean |
| T5 | 12 tests vs parser | clean |
| T6 | count said 15, actual 16 | DEFECT: wrong expected count |
| T7 | stops_level 600pts = 6.00 USD < 12.10 actual distance | DEFECT: test asserts reject, gate would pass |
| T8 | two tests hit chase_gate before the path under test | DEFECT: both would fail |
| T9 | 10 tests vs followup_planner; BE/close-half arithmetic hand-checked | clean |
| T10 | golden-file path parents[3]; snapshot passes age+tick gates | clean |
| T11 | >=8 pure files; actual 13 | clean |

## Rulings

Ruling: Bootstrap the repo and copy spec+plan into it myself before Task 1 — the review scripts need a git root to diff against, and repo creation is controller setup, not reviewable work. Cost if wrong: none; Task 1 still owns pyproject/policy/loader.
Ruling: Work on branch feat/pure-core off a fresh local repo rather than a worktree — there is no existing main to protect and no shared remote. Cost if wrong: none; branch is local and discardable.
Ruling: Drop registry.py from Task 5 — nothing in this plan dispatches by channel, so it is dead code the review rubric would flag. Plan 2 introduces it at first use. Cost if wrong: Plan 2 writes ~15 lines that would otherwise exist.
Ruling: T7 stops_level_points 600 -> 1500 so min_dist (15.00 USD) actually exceeds the 12.10 USD sl distance. Cost if wrong: none; the assertion was simply unreachable before.
Ruling: T8 gets a PERMISSIVE policy (chase_floor_usd=50) for the two TP-dropping tests, plus a new test pinning that the DEFAULT policy still rejects a runaway on CHASE_EXCEEDED. Price cannot pass a TP without also running far past the zone, so the two behaviours cannot both be tested under one policy. Cost if wrong: a reviewer may argue the permissive policy is unrealistic; the added default-policy test covers that.
Ruling: T9 leaves ModifyIntent/CloseIntent account_id as "" — the pure follow-up planner has no account context. Plan 3 wires it at the executor boundary. Cost if wrong: Plan 3 threads one extra parameter.

## Task log
Task 1: fix round 1/5 (2 addressed, 0 open — list recursion in _points guard; false file claim in report; commits 107f571..7c51df9)
Task 1: minor (deferred): loader does not wrap FileNotFoundError/TOMLDecodeError as ConfigError
Task 1: minor (deferred): plan document's loader code block still shows the pre-fix version without list recursion
Task 1: Ruling: reviewer's "word points appears in function/test names" finding is not a violation — the constraint governs thresholds expressed in points, not the literal substring, and a guard whose job is detecting that substring cannot avoid it. Cost if wrong: a later grep-based lint would need an allowlist entry.
Task 1: complete (commits d4f87e5..7c51df9, review clean)
Task 2: minor (deferred): normalize_volume rounds to 8 dp twice (dead second round); int() wrapper on round() is redundant
Task 2: minor (deferred): normalize_volume silently clamps negative/zero volume to volume_min instead of raising, unlike its volume_max branch
Task 2: minor (deferred): round-half-to-even vs half-up is an undocumented choice in a financial-precision module
Task 2: Resolved reviewer's warning item: "are round_price/usd_to_points the only such functions project-wide" is trivially true at this commit (the diff only adds files); carried to the final whole-branch review as a grep sweep.
Task 2: complete (commits 7c51df9..c04775c, review clean)
Task 3: Ruling: the brief's EntryZone.__post_init__ swap was defective (set lo=hi, then hi=lo reads the already-overwritten lo, collapsing both bounds to the original hi). The implementer deviated from the literal brief code to fix it. Deviation accepted — the spec requires an ordered zone and the brief's code cannot produce one. Cost if wrong: none; the reviewer verifies the replacement independently.
Task 3: minor (deferred): signal.py imports `field` from dataclasses but never uses it (dead import, ruff F401) — originates from the brief's own template
Task 3: minor (deferred): MarketSnapshot, AccountSpec, FollowUp, ModifyIntent, CloseIntent, Plan.accept, Side.sign, EntryZone.near_edge/contains have no direct unit test (plan-mandated scope)
Task 3: Resolved reviewer's warning item: the brief's "Consumes SymbolSpec from Task 2" is stale metadata I wrote — none of Task 3's four modules need SymbolSpec, and the brief's own code never references it. Not a missed requirement.
Task 3: Resolved reviewer's warning item: Co-Authored-By trailer verified present on all four commits via `git log --format=%(trailers)`.
Task 3: complete (commits c04775c..0bc6365, review clean)
Task 4: minor (deferred): _WS_RUN collapses only ASCII space/tab, not Unicode Zs separators (en/thin/ideographic space survive)
Task 4: Ruling: the fix implementer committed 3195442 then died on an API connection drop before writing its fix report. Rather than re-dispatch an agent purely for bookkeeping, the controller verified the landed state (clean tree, 33/33 green, all seven behavioural cases hand-checked) and appended the evidence to task-4-report.md, clearly marked as controller-recorded. No controller-authored code. Cost if wrong: the re-reviewer is judging evidence the controller transcribed rather than the implementer produced; the diff itself is unaffected and is still independently reviewed.
Task 4: fix round 1/5 (1 addressed, 1 new — per-comma digit counting corrupted 3-decimal prices; fix introduced double-decimal-point on multi-comma runs; commits 0ac9ea4..3195442)
Task 4: fix round 2/5 (1 addressed, 0 open — single-pass whole-run classification; commits 3195442..44164ee)
Task 4: Ruling: the round-1 regression ("4,480,25" -> "4.480.25") was accepted into a second round rather than parked, because the pre-fix code handled that input correctly. A fix that loses capability the code already had is a regression, not a deferred nicety. Cost if wrong: one extra fix round on a non-safety-critical path (the bad output fails NUM matching and is hard-rejected, never mis-traded).
Task 4: Ruling: implementer model raised from haiku to sonnet for Tasks 5-11. The cheap tier is sanctioned for transcription when the plan text is correct; this plan's text has now produced three real defects (EntryZone swap, _points list recursion, comma disambiguation), so implementers must be able to catch my errors rather than transcribe them. Cost if wrong: higher per-task token spend on tasks that were in fact pure transcription.
Task 4: complete (commits 0bc6365..44164ee, review clean)
Task 5: minor (deferred): `if alias not in GOLD_ALIASES` in channel_mmh.py is unreachable — _HEAD's alternation can only capture those three literals, so no test can reach the guard (plan-mandated)
Task 5: minor (deferred): unused `import pytest` in tests/test_parser_mmh.py (plan-mandated, would trip ruff F401)
Task 5: fix round 1/5 (1 addressed, 0 open — parser resolved ambiguous messages first-match-wins into a wrong-but-plausible Signal; commits 709bf72..31b5a70)
Task 5: Ruling: the reviewer's Critical ambiguity finding overrides the brief's own code. The spec's parser contract is loud failure over wrong trade, and "first match wins" on a duplicated SL or two concatenated signal blocks produces a complete, plausible Signal that no downstream gate can detect as wrong. Cost if wrong: a legitimate message that happens to repeat a field is now rejected rather than traded — a missed trade, which is the cheap direction of this error.
Task 5: complete (commits 44164ee..31b5a70, review clean)
Task 6: minor (deferred): no dedicated AMEND_TP test existed pre-fix (now covered by the tp_index_hint tests)
Task 6: minor (deferred): _IS_SIGNAL is a blunt "contains the word SIGNAL" check; a follow-up mentioning "signal" in prose is dropped entirely (fails safe)
Task 6: minor (deferred): CLOSE_HALF is tested before CLOSE_ALL, so a message containing both keywords picks CLOSE_HALF
Task 6: minor (deferred): AMEND_SL's operator is optional but AMEND_TP's is mandatory, so "TP 4500" without punctuation does not match (fails safe)
Task 6: minor (deferred): NUM regex constant duplicated between followups.py and channel_mmh.py
Task 6: fix round 1/5 (3 addressed, 0 open — negation blindness, discarded tp_index_hint, list-not-tuple; commits 1ff1f76..f972c82)
Task 6: Ruling: the negation guard is deliberately whole-message and therefore over-eager — the re-review confirmed "close all, don't wait" now returns None, dropping a legitimate close. Accepted: this system's governing principle is loud failure over wrong action, and dropping an instruction (human notices nothing happened) is strictly cheaper than closing a position the poster said not to close. Cost if wrong: some real follow-ups are ignored until real message samples justify a narrower guard.
Task 6: complete (commits 31b5a70..f972c82, review clean)
Task 7: minor (deferred): unused `import pytest` in tests/test_gates.py
Task 7: minor (deferred): magnitude_gate builds a local list literal to iterate (ephemeral, not a domain sequence, but reads against the tuple rule)
Task 7: minor (deferred): chase_floor_usd can exceed the whole zone-to-TP1 distance on tight setups; caught downstream by ALL_TPS_BEHIND, worth watching when tuning
Task 7: fix round 1/5 (1 addressed, 0 open — geometry_gate compared sl to the zone midpoint, accepting a stop inside the entry zone; commits 851c4e2..af462a1)
Task 7: Ruling: geometry_gate must compare the stop against the zone's outer edge, not its midpoint. The brief's own logic accepted BUY zone=(4470,4485) sl=4475, which the planner would fill at 4470 leaving the stop above entry — an inverted-risk position that passed all nine gates. Cost if wrong: signals whose stop sits inside a wide quoted zone are now rejected rather than traded; that is the safe direction.
Task 7: complete (commits f972c82..af462a1, review clean)
Task 8: minor (deferred): the ALL_TPS_BEHIND branch in plan_entry is unreachable — invalidation_gate calls the same surviving_tps earlier and always rejects first. Left as defence in depth.
Task 8: minor (deferred): 20-char comment boundary untested for a very large message_id (reviewer verified int64 max fits in 16 chars)
Task 8: fix round 1/5 (1 review finding + 2 controller-ruled items addressed, 0 open; commits b14d96a..3869f74)
Task 8: Ruling: pending_at validation and SELL test coverage were ruled INTO the fix round despite being reviewer-Minor. Rationale: an unvalidated pending_at means a TOML typo silently turns TP1 from a 1.0R target into a 0.33R one with no error, and the planner is the single money-losing surface in the system with zero regression net for half the direction space. Cost if wrong: a slightly wider fix diff than the review strictly demanded.
Task 8: Ruling: my fix brief's SELL-below-zone expectation (bid 4475 -> LIMIT at 4485) was wrong; chase_gate correctly rejects it (overshoot 5.00 vs budget 2.50). The implementer adjusted the input to bid 4479.5 to reach the LIMIT path rather than weakening the assertion. Accepted. Cost if wrong: none; the corrected test exercises the intended behaviour.
Task 8: complete (commits af462a1..3869f74, review clean)
Task 9: minor (deferred): FollowUpPlan.intents annotated as a bare `tuple` with no element type
Task 9: minor (deferred): CloseIntent construction repeated near-identically across three branches; a _close(leg) helper would DRY it
Task 9: Ruling: the implementer had to change one line of an existing test (test_cancel_pending_only_touches_pending_legs: i.position_ticket -> i.order_ticket). Accepted — CancelIntent deliberately names its field order_ticket because a pending order's ticket is an order ticket, so the old assertion could not survive Item 1. The assertion's substance is unchanged. Cost if wrong: none; the test still pins that only pending legs are touched.
Task 9: Ruling: commission_in_price PARKED, not fixed. The spec's breakeven formula has three cost terms and the code has two, but no commission field exists in Policy or MarketSnapshot, and modelling it needs per-lot commission and contract size that only arrive from the broker in Plan 3. Default be_buffer_usd (0.10 USD/oz) exceeds typical gold commission in price terms (~0.07 USD/oz round turn), so the buffer absorbs it. A comment now records this. Cost if wrong: breakeven stops sit fractionally inside true flat on a high-commission account, exiting at a small loss rather than exactly flat. Plan 3 must revisit.
Task 9: fix round 1/5 (5 addressed, 0 open — close/cancel conflation, unvalidated AMEND_TP, no min-stop-distance on BE/amend, silent no-op, untested amend branch; commits 6eab565..5e74950)
Task 9: complete (commits 3869f74..5e74950, review clean)
Task 10: complete (commit c44a6e4, review clean)
Task 11: complete (commits aa0ccb9..4508f70, review clean after one fix round)
Task 11: fix round 1/5 (1 addressed, 0 open — open() detection only caught bare calls, missing io.open/builtins.open/Path().open/aliased imports; commit 4508f70)
Task 11: Ruling: the open()-detection hole was fixed rather than deferred despite the reviewer approving the gate. A purity guard's entire value is preventing FUTURE impurity; one that misses the two idiomatic ways to open a file reports safety that does not exist. Cost if wrong: a slightly larger test file than the brief specified.
ALL 11 TASKS COMPLETE. 175 tests passing.
FINAL WHOLE-BRANCH REVIEW: 2 Critical + 8 Important findings. One fix wave (5 commits), one scoped re-review: all addressed, no new breakage. 224/224 green.
Ruling: the final review's C1/C2 were fixed rather than parked despite eleven task reviews having passed. Both were deterministic wrong-action bugs (CLOSE_HALF closing the nearest legs on every SELL; AMEND_TP flattening the whole TP ladder). Cost if wrong: none — these were unambiguous.
Ruling: I2 reverses my own Task 8 PERMISSIVE ruling. I had treated the chase gate masking the TP-drop path as a test-ergonomics problem; the final review showed it was a production defect — the gate was rejecting signals whose entry would have been a fixed-price limit order, making the spec's pending path nearly unreachable. chase_gate now applies only to MARKET entries. Cost if wrong: signals that have run far past the zone now rest as limit orders at the favourable edge until pending_max_age_hours expires, instead of being rejected outright.
Ruling: I8 fixed minimally (a distinct PENDING_ONLY reason) rather than fully. The full fix needs an intent type for modifying a pending order, which is a different broker operation and belongs with the executor in Plan 3. Cost if wrong: an amended stop on a not-yet-filled entry alerts a human instead of being applied automatically.
PLAN 1 COMPLETE.
