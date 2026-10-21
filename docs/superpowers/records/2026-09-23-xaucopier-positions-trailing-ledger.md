# SDD ledger — plan: docs/superpowers/plans/2026-09-23-xaucopier-positions-trailing.md
Worktree: C:\Users\Herbert\Projects\xaucopier-plan1b. Branch: feat/positions-trailing, off master 289c117. .venv is a junction to the main checkout's venv.
Spec: docs/superpowers/specs/2026-09-05-telegram-mt5-xauusd-copier-design.md, plus the amendment docs/superpowers/specs/2026-09-23-xaucopier-positions-trailing-amendment.md.
User direction (2026-10-15): "finish up as fast as possible but efficiently". Never skip checks. A re-check is not needed when everything is green. Use `pytest -n 6`.

## Pre-flight scan
| pair / task | produces -> consumes | finding |
|---|---|---|
| T1 -> T2 | AccountSpec.positions, OrderIntent.tp_index -> plan_entry/distribute | consistent |
| T1 -> T2b | AccountCfg/AccountSpec (T2b adds layered_entry, same guards) | consistent; both touch config/accounts.py and market.py, run in sequence |
| T2 -> T2b | distribute() -> split_now_pending; both edit planner.py | sequential (P5) |
| T1 -> T3 | Policy.trail_enabled/trail_gap_usd -> target_stops | consistent; be_buffer_usd and pending_max_age_hours already exist in Policy |
| T3 <-> T4 | BE semantics (fill +/- spread + be_buffer) vs SL_TO_BE NOT_APPLICABLE once trailed | consistent |
| T2b <-> T5 | golden regeneration | CONFLICT: T5 says the only change is the comment format; T2b says regenerate with layered default true -> P2 |
| T2b <-> DoD | "no behaviour change when positions unset" vs layered default true | CONFLICT -> P2 |
| T3/T2b <-> T5 trail replay | replay with positions=4 under layered default: pending legs | CONFLICT (fills unspecified) -> P3 |
| design note <-> T1 | `lot = L` vs existing `fixed_lot` | CONFLICT -> P1 |
| T2b, T3 | "minimum distance" | one formula, gates.min_stop_distance_usd = max(stops_level, 3 x spread); 0.30 in the worked table -> P4 |
| T1 self | bool guard, 1..20 cap, omitted = None | consistent |
| T2 self | the table rows vs "distribute over surviving" | consistent |
| T2b self | the amendment table (4483/4486/4487.50/4479) vs pending_levels k=2 (top..better edge inclusive) | checked: 4482.70/4480, 4485/4480 consistent; chase 1.25 = max(1.00, 0.25 x 5) |
| T3 self | the worked-example table vs the ladder rule | consistent (4492 = TP1+2, 4497.50 = mid(4495, 4500), 4502 = TP3+2) |
| T4 self | consistent |
| T5 self | consistent apart from the P2/P3 conflicts |

Ruling: P1, `fixed_lot` is the lot per position, and unknown account keys are refused with a ConfigError (`lot` suggests `fixed_lot`). Why: a silently ignored key changes the trade size. Cost if wrong: an old config with a stray key stops loading, with a clear message.
Ruling: P2, layered_entry defaults to true even when positions is unset (N = surviving TPs), superseding the DoD/T5 "comment only" lines; layered_entry=false must reproduce today's legs. Why: the 2026-09-25 amendment is the newer user decision. Cost if wrong: the user expected an opt-in.
Ruling: P3, the trail replay runs both with layered_entry=false (the worked example exactly) and true (pending legs fill at their limits; unfilled ones are cancelled at TP1). Why: the plan predates layered entry. Cost if wrong: one extra test.
Ruling: P4, there is one minimum-distance formula (gates.min_stop_distance_usd). Why: the Global Constraints say never two formulas. Cost if wrong: none.
Ruling: P5, the order is 1, 2, 2b, 3, 4, 5, sequential, in one worktree. Why: the skill forbids parallel implementers in one tree. Cost if wrong: wall-clock time only.

Task 1: complete (commits 7d201ce..ca1b6bf, review clean)
Task 1: minor (deferred): the `_points` guard test cannot tell that guard apart from the unknown-key refusal.
Task 1: minor (deferred): AMEND_TP picks its target by leg_index (the hazard became Task 2's Important finding).
Task 1: cannot-verify resolved by the controller: the user's live config/accounts.toml (main checkout, clean against git) uses only the keys account_id, allow_live, broker, enabled, fixed_lot, login, magic_base, ordinal, server, symbol and terminal_path, so ruling P1's unknown-key refusal does not break it.
Ruling: T2-I1 (plan-mandated Important): AMEND_TP still matches LegView.leg_index == tp_index_hint - 1. Fix it in Task 2: add LegView.tp_index and match AMEND_TP on it, so every position on that TP is amended. Why: Task 2's ordinal leg_index broke it even with positions unset, and leaving it latent until Plan 3 risks it being forgotten. Cost if wrong: a few more lines in Task 2's diff.
Ruling: T2-m1: leg_index is the position's ordinal even with positions unset (TP1/TP2 passed gives leg_index [0,1], tp_index [2,3]). Why: the binding interface says so; "only the comment format changes" was loose wording. Cost if wrong: none; nothing downstream reads the old meaning once T2-I1 is fixed.
Ruling: T2-m2: Decision.comments in the replay harness is accepted. Why: it makes the golden file show the comment change at all, and Task 5 needs that. Cost if wrong: none.
Task 2: fix round 1/5 started, agent killed by the weekly usage limit with uncommitted work in followup_planner.py and test_followup_planner.py (backup: scratchpad backup/plan1b-T2-fix1-partial.diff). Relaunching.
Task 2: fix round 1/5 (1 addressed, 0 open: AMEND_TP now matches on LegView.tp_index; commits 0d3fcdf..a27349d)
Task 2: complete (commits ca1b6bf..a27349d, review clean after 1 fix round)
Task 2: minor (deferred): the AMEND_TP geometry loop returns GEOMETRY_CONFLICT on the first failing leg, so the whole amend is refused.
Task 2b: complete (commits a27349d..050da9a, review clean)
Task 2b: minor (deferred): beyond the chase budget the NOW limits rest at min(zone.hi, ask - min_dist), not strictly at the edge (broker minimum distance).
Task 2b: minor (deferred): a zone narrower than (min_dist - overshoot) beyond the chase budget rests the legs just outside the zone.
Task 2b: minor (deferred): pending_levels takes a fifth parameter, spec, for round_price.
Ruling: T2b-cv1: for a signal already past TP1 when planned, Plan 3 calls pending_should_cancel with sig.tps (the amendment's literal TP1), and a plan whose limit legs would be cancelled at once is reported as "not placed: price already past TP1" instead of placing and cancelling them. Recorded as PLAN3-OBLIGATIONS #52. Why: the user's rule cancels pending orders once TP1 is reached; placing orders that are cancelled on the next tick is pointless broker traffic. Cost if wrong: a very late signal places nothing (the 2-minute age gate makes this rare).
Task 3: fix round 1/5 (1 addressed, 0 open: breakeven no longer ratchets with the spread; commits 8662f0f..e052b3d)
Ruling: T3-I1: accept the fixer's option. A leg whose stop is already at or past its own fill + be_buffer_usd counts as at breakeven, and the BE rung no longer moves it; the TP rungs still do. Why: without it, every new spread high is another modify request, and the re-review confirmed it. Cost if wrong: the breakeven stop sits a spread's width closer to the fill than the literal formula.
Ruling: T3-cv2: the trailing ladder is given sig.tps, all of them, in the poster's numbering, never only the surviving TPs or only the TPs that have positions. Recorded as PLAN3-OBLIGATIONS #53. Why: the approved ladder is defined on the poster's TP1..TPT. Cost if wrong: with TP1 already passed at entry, the BE rung triggers at once (correct: price is past TP1).
Task 3: complete (commits 050da9a..e052b3d, review clean after 1 fix round)
Task 3: minor (deferred): a rung is earned only on the current tick, with no memory of the high.
Task 3: minor (deferred): the breakeven formula exists twice (trailing._breakeven and followup_planner._breakeven_price), with a test pinning them equal.
Task 3: minor (deferred): ladder((nan,)) returns a BE rung.
Task 3: minor (deferred): an unused `before` parameter in a test helper.
Task 3: minor (deferred): Policy.be_buffer_usd has no validation.
Task 4: complete (commits e052b3d..aef6c63, review clean)
Task 4: minor (deferred): _compare_stop re-implements trailing._tightens.
Task 4: minor (deferred): the trailed() helper's `or` defaults.
Task 4: minor (deferred): a weak "3" in note assertion.
Task 4: minor (deferred): a list-typed helper signature.
Task 5: implementer killed by the session limit with uncommitted work: config/accounts.toml, RUNBOOK, tests/test_account_positions.py, test_cli_watch.py, test_replay.py, test_runbook.py, and a new tests/test_trail_replay.py (251 lines). Backup: scratchpad backup/plan1b-T5-partial.diff. Relaunching.
User request 2026-10-21: "once tp 1 is hit, it deletes the pending orders that have not been activated". This is already the approved rule (pending_should_cancel, built in Task 2b; PLAN3-OBLIGATIONS #44 now spells it out: EVERY unfilled limit of the signal, removal confirmed). CONTROLLER TODO after Task 5: check that the RUNBOOK says this plainly; if not, have it added.
Task 5: complete (commits aef6c63..138ce55, review clean; 4137 tests, -n 6 and serial)
Task 5: minor (deferred): RUNBOOK "a single position always opens at once" is too strong (beyond the chase budget it rests as a limit).
Task 5: minor (deferred): the RUNBOOK's "not placed: price already past TP1" sentence is Plan 3 behaviour (#52). Plan 3 must emit exactly that reason or update the RUNBOOK.
Task 5: minor (deferred): "once a trade is in profit" should be "once the price reaches the first take profit".
Task 5: minor (deferred): the _real_signal() test helper is duplicated.
Controller TODO done: the RUNBOOK says that pending orders are cancelled at TP1 or after 4 h (Task 5 checked it against plan_entry).

USER DECISIONS 2026-10-21. The user pointed at their EA, MQL5/Experts/Target Trend EA.mq5 ("makes sure the positions are assigned accordingly to the tp levels and breakeven buffer should be in pips and the sl trailing should be like this EA"), and answered:
- extra positions cycle like the EA's TPS_SPLIT;
- 1 pip = $0.10, fixed;
- every ladder step fires on hit by default.
These become Task 6 (amendment 2026-10-21), which supersedes the 2026-09-23 ladder, the N>T "extras on the last TP" rule, and the spread term in breakeven.
USER DIRECTION 2026-10-21: "without re-reviewing". From now on, each task gets ONE review. Fixes are verified by the fixer's own tests and NOT re-reviewed, and there is no final whole-branch review. Applied to Task 6 (the workflow is stopped if it reaches a re-review) and to Plan 3.
Task 6: implemented (commits 3e702fa..f1dd65f, 4242 tests, 16/16 mutants killed). The first two attempts were killed (by the usage limit, then by a network error); the third kept their work. Its review was killed by the session limit and is being resumed.
Ruling: T6-c1: the default replay walks assert BE -> 4490 -> 4495 and then a close at TP4. Why: step 4 (stop to TP3) is reached exactly at TP4, where the last position closes, so "-> 4500" cannot happen on defaults; a walk with ladder_early_pips=[0,0,0,20] reaches 4500 at 4503. Cost if wrong: none (it is the amendment's own rule).
Ruling: T6-c2: plan_followup takes a keyword-only tps (sig.tps), so SL-to-BE can cap at TP1. Why: one breakeven formula. Cost if wrong: Plan 3 must pass it (added to its brief).
Ruling: T6-c3: Policy.from_dict refuses retired keys with ValueError (ConfigError subclasses it). Why: domain/ may not import config/. Cost if wrong: none.
Ruling: T6-c4: a stop at the price itself is refused even when the broker's minimum distance is 0, which is stricter than the EA. Why: safety. Cost if wrong: in that edge case a step comes one tick later.
Note: the user's main checkout has an unmodified, tracked config/policy.toml, so the Plan 1b merge replaces be_buffer_usd with be_buffer_pips. If the user ever edits it locally, the refusal message names the new key.
Task 6: complete (commits 3e702fa..f1dd65f, review clean)
Task 6: minor (deferred): no layered_entry=true walk reaches the 4500 step.
Task 6: minor (deferred): plan_followup requires tps for every op (PLAN3 #55).
Task 6: minor (deferred): the retired-key ValueError needs a handler in the Plan 3 CLI (PLAN3 #56).
Task 6: minor (deferred): the shipped policy-file assertions appear twice.
Final whole-branch review: SKIPPED by user direction ("without re-reviewing", 2026-10-21). The controller re-ran the full suite at f1dd65f: 4242 passed.
Plan 1b COMPLETE; merging feat/positions-trailing into master.
