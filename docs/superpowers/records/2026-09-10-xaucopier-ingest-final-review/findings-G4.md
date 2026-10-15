# Findings for fix-wave G4 only (extracted from findings.md)


---

## F03 [Important] The window re-read never covers messages stored above the pointer, so a quick deletion of the newest signal goes unseen until the channel posts again

- Reviewer area: C-edits
- Owner in the fix wave: **group 4 (edits/router)**
- Location: `src/xaucopier/ingest/edits.py:247`

### Scenario
I ran this (probe_a.py, probe_a2.py). Setup: pointer at 3004 after four chatter messages. Signal 4001 arrives live and is stored with 2 QUEUED ENTRY rows. The pointer does not move, because only history reads move it. The poster deletes 4001 before the next round and the MessageDeleted update is dropped. The forward read `get_messages(min_id=3004)` returns nothing (4001 is gone), so the pointer stays at 3004. `window_versions(at_or_below=pointer)` only ever asks for 3001-3004. After 50 rounds (~250 s): 4001 is NEW and not withdrawn, deleted_at is NULL, alerts [], delete_missed_by_eventstream is 0, and a real `outbox.claim()` returns its ENTRY. Only when a newer message (4002) is posted does the pointer pass 4001, which is then voided 2 rounds later. The docstring (edits.py:15-20) says above-pointer messages 'belong to the forward read, which meets their current version anyway when it passes them'. That holds for edits but not for deletions, because the forward read never passes a deleted message. The delay equals the time until the channel's next post, which can be hours.

### Reviewer's proposed fix
Take the window over the newest stored messages of the channel regardless of the pointer: drop the `message_id <= ?` bound in `window_versions`, or pass max(pointer, max stored id). Every row in it is stored, so an absence is still meaningful. The breaker, the two-read rule and the in-job decision all still apply, and the window still never writes the pointer. Update the docstring and add a test with a live-stored newest message that is deleted while the event is dropped.

### Verifier evidence
Probe: verify-C-edits/v2.py (single channel A).
Setup: the pointer is seeded at 3000; chatter 3001-3004 arrives live; 1 round moves the pointer to 3004. Signal 4001 arrives live and is stored with 2 QUEUED ENTRY rows. Then emit_delete(A, 4001) runs inside a blackout, so the event is dropped (delivered=False).

After 50 rounds:
- the pointer is still 3004;
- the union of all ids= the window asked for is [3001, 3002, 3003, 3004], never 4001;
- signal (1, 4001, 'NEW', withdrawn 0); deleted_at NULL; alerts []; metrics [];
- claim(acct-2) -> ('ENTRY', 1, 2).

After chatter 4002 is posted:
- +1 round: ('NEW', 0), so 4001 is pending;
- +2 rounds: withdrawn=1, deleted_detected_by='window', delete_missed_by_eventstream=1.
The signal's state was LIVE_SIGNAL_DELETED because my mid-run claim had moved acct-2 to CLAIMED, which shows a worker can take it during the gap.

Control F2-ctl (delete after one round had moved the pointer onto 4001): VOIDED within 3 rounds, with WARNING plus CRITICAL WINDOW_VOIDED_SIGNALS.

Variant v2b.py (an edit instead of a delete, event dropped): the forward read re-fetches 4001 above the pointer and applies it (INVALID, CRITICAL, edit_missed_by_eventstream=1). So the edits.py:15-20 docstring holds for edits but not for deletions, exactly as claimed.

Trigger: a deletion within one poll interval (about 5 s) of a live-delivered post, with the MessageDeleted update dropped. The delay lasts until the next post in that channel. With a prompt Plan 3 worker the entry is probably already taken, so the main harm is a late LIVE_SIGNAL_DELETED alert. A lagging worker can still trade the retracted call.

---

## F11 [Minor] A post completed by a quick edit is traded only if the update stream missed the first version

- Reviewer area: C-edits
- Owner in the fix wave: **group 4 (edits/router)**
- Location: `src/xaucopier/store/edits_repo.py:818`

### Scenario
I ran this (probe_d.py, part C). Poster posts 'XAUUSD | BUY SIGNAL' with the zone but no SL (parse_failed), or chatter, then within 40 s edits it into the full signal. Healthy stream: the first version is stored live, the edit goes through `_edit_of_other`, and the result is 'reclassified' with WARNING EDIT_CREATED_SIGNAL, no signals row and 0 ENTRY rows (the parse_failed version also counts toward PAUSE_NEW). Both events dropped: the forward read stores the final text as new, with a signals row and 2 QUEUED ENTRY rows. The same channel history gives opposite trade outcomes depending on stream health. The rule that edits never open trades is a settled Task 10 ruling. The new evidence is this path-dependence: the healthy path is the one that misses the trade. Every other edit type I tried gives the same result on all four paths.

### Reviewer's proposed fix
This is a decision for the controller, not a mechanical fix. One option: treat a parse_failed or skip message edited into a signal as new, within a short grace period after posted_at, still under the age gate. Another: keep the ruling, but make EDIT_CREATED_SIGNAL CRITICAL and document that a blackout makes such a message tradeable, so the two paths at least say the same thing.

### Verifier evidence
Probe: verify-C-edits/v4.py. The first version is the fixture without its SL line; route() says 'parse_failed'. It is edited into the full signal 40 s later. All four delivery paths ran for 3 rounds each:
- NewMessage delivered, edit delivered: signals [], outbox [], WARNING EDIT_CREATED_SIGNAL, consecutive_parse_failures=1.
- NewMessage delivered, edit dropped: same, and the window found it (edit_missed_by_eventstream=1).
- NewMessage dropped, edit delivered: the edit of an unknown message is ignored, then the poll stores the final text as new. Signal NEW with 2 QUEUED ENTRY rows, parse_failures=0.
- Both dropped: the same as the previous path.
Chatter first ('XAUUSD BUY coming soon'): delivered gives EDIT_CREATED_SIGNAL and no trade; dropped gives a trade.

So the path-dependence is real, as described. It is also the direct, intended result of the settled Task 10 ruling that edits never create trades. The EDIT_CREATED_SIGNAL alert tells the user to trade it by hand, and the Task 5 ruling on the parse-failure counter covers the PAUSE_NEW count.

The asymmetry cannot be avoided: when the first version was never seen, nothing shows that it was not a signal. The blackout path trades the poster's final, complete call under the normal age gate, which is not itself harmful. No concrete harm beyond what the ruling already accepts.

Downgraded to a documentation and alert-level question for the controller: possibly make EDIT_CREATED_SIGNAL CRITICAL, since it asks the user to act by hand.

---

## F18 [Minor] A MessageDeleted event that arrives while the forward read is holding the message is thrown away, so on a tiny window the deletion is never applied

- Reviewer area: C-edits
- Owner in the fix wave: **group 4 (edits/router)**
- Location: `src/xaucopier/store/edits_repo.py:869`

### Scenario
I ran this (probe_f.py, probe_f2.py). The forward read fetches 4001. The MessageDeleted event for 4001 is handled before the read's ingest job, so `apply_deletion` returns 'unknown' and nothing is recorded. The read then stores 4001 as a new signal with QUEUED entries. With 3 other messages in the window it heals one round later (VOIDED). On a channel whose window holds only 4001 (first run, or a new channel), the read is all-absent and trips the breaker every round. The signal stays NEW with claimable entries, and the only alert is WINDOW_READ_SUSPECT, whose text says 'MessageDeleted events still act'. Here that is untrue: the event was discarded. The race is narrow.

### Reviewer's proposed fix
Keep a small tombstone of event-deleted ids that were not stored yet, in memory per channel or as a table, and have `_ingest_new` (or `ingest_history`) check it and store the message already marked deleted by the event. Or reword the WINDOW_READ_SUSPECT text so it does not promise the event acted.

---

## F07 [Critical] A changed signal header is a silent outage, not the loud one spec 6.2 requires, and common entry layouts are read as AMEND_SL follow-ups. PAUSE_NEW has no owner, and the user-facing format caveat is false.

- Reviewer area: G-crosscut
- Owner in the fix wave: **group 4 (router/alert) + group 5 (caveat text)**
- Location: `C:\Users\Herbert\Projects\xaucopier\src\xaucopier\parsing\router.py:91`

### Scenario
RAN (scratchpad/final/G-crosscut/router_probe.py, probe_g.py P2, probe_formats.py; real route() and messages_repo.ingest on temp DBs).

(1) Silent outage. I changed only the header of the real fixture mmh_buy_4tp.json: 'XAUUSD BUY SIGNAL' (no pipe), 'XAUUSD | BUY' (no SIGNAL), 'XAUUSD BUY NOW', 'GOLD | BUY LIMIT'. Each one gives kind=skip. After ingest, channel_state has no row (counter 0) and there are no alerts; status prints 'unreadable signals in a row: 0'. A header change is the most likely template change, and it silently stops every signal. Spec 6.2 says 'a template change becomes a loud outage'. Three failures that keep the header do push the counter to 3, but nothing alerts. PAUSE_NEW exists in no code and in no PLAN3-OBLIGATIONS item, although signals_repo.py:259 says 'Plan 3 reads this'.

(2) Misread entries. 4 of 6 common gold entry layouts route as follow-ups:
- 'BUY GOLD 4480\nSL: 4470\nTP: 4490' gives amend_sl 4470
- 'Gold Sell Now @4502\nSL:4510\nTP:4495' gives amend_sl 4510
- 'XAUUSD SELL\nEntry 4502\nStop Loss 4510\nTake Profit 4490' gives amend_sl 4510
- 'GOLD BUY LIMIT 4480\nSL = 4470\nTP = 4495' gives amend_sl 4470
Ingest queues a FOLLOWUP outbox row for each (P2: ('FOLLOWUP',1,'QUEUED')). This contradicts three things: the FORMAT_CAVEAT that `channels add` prints (cli/channels.py:83, 'nothing will be traded from it'), the caveat given to the user at ledger line 184, and run 1's E-cli verified_clean claim that such channels are 'skipped, not traded'.

REASONED, not run (Plan 3): on the default COMMUNITY source, which carries forwarded MMH signals, suppose an admin posts their own call in one of these layouts while exactly one MMH signal is live. Spec 8.1 rule 3 resolves it to that signal and moves the live stop to the new call's SL. The new call itself is never traded.

### Reviewer's proposed fix
In route(), before calling parse_followup on a NOT_A_SIGNAL result, classify entry-shaped text as parse_failed. Entry-shaped means: a gold alias plus a BUY/SELL side word plus an SL value, or a side word plus an SL value plus any TP value. That text then feeds the counter and never becomes a follow-up. Raise an alert, not just a status number, when consecutive_parse_failures reaches 3, and when a channel records N messages with no signal. Add spec 6.2 PAUSE_NEW to PLAN3-OBLIGATIONS. Correct FORMAT_CAVEAT until this lands.

### Verifier evidence
Probe: verify-G-crosscut/v_probe.py, using the real route(), messages_repo.ingest() and the status render on temp DBs.

F1a, header variants of mmh_buy_4tp.json:
- 'XAUUSD BUY SIGNAL', 'XAUUSD | BUY', 'XAUUSD BUY NOW', 'GOLD | BUY LIMIT' and 'XAUUSD - BUY SIGNAL' all give kind=skip, detail "no 'SYMBOL | SIDE SIGNAL' header".
- 'XAUUSD | BUY SIGNAL NOW' still parses.
- I ingested 5 signals with the no-pipe header. Result: channel_state=[], alerts=[], signals=0, outbox=[]. Status shows 'ALERTS: none' and 'newest #4104 ... skip', then 'unreadable signals in a row: 0'.

F1b, header kept and zone line broken, three times: all three give kind=parse_failed. channel_state is (-1001111111111, 3), alerts=[], and status shows 'ALERTS: none ... unreadable signals in a row: 3'.

F1c, route() on 10 layouts. These give followup amend_sl:
- 'BUY GOLD 4480\nSL: 4470\nTP: 4490' (4470)
- 'Gold Sell Now @4502\nSL:4510\nTP:4495' (4510)
- 'XAUUSD SELL\nEntry 4502\nStop Loss 4510\nTake Profit 4490' (4510)
- 'GOLD BUY LIMIT 4480\nSL = 4470\nTP = 4495' (4470)
- 'XAUUSD BUY @ 4480\nStoploss: 4470\nTakeprofit: 4490' (4470)

The other 5 layouts (indexed TP, zone line, lowercase 'tp') give skip. I then ingested one of the followup layouts on the MMH channel while signal 1 was live. Result: followups (1, 4010, 'amend_sl', target NULL, price 4470), outbox [(ENTRY 1 QUEUED), (FOLLOWUP fu1 QUEUED)], counter 0.

PAUSE_NEW appears only in docstrings (router, channel_mmh, result, signals_repo) and in no PLAN3-OBLIGATIONS item.

Overstated:
- The spec still owns PAUSE_NEW, so it has an owner.
- The format caveat ('nothing will be traded from it') mostly holds for a channel that is entirely non-MMH. That channel never has a signal row, so spec 8.1 rules 1 and 3 give no action plus an alert. The caveat is contradicted only on a channel that mixes the MMH template with other layouts, which is the case the caveat does not describe.
- Header→skip is the plan's explicit design (ingest plan, router rules 2-3).

The harm is still concrete: the most likely kind of template change stops signal capture while status looks healthy, and the router's claim that parse_followup guards against entry-shaped text fails for common layouts.

---

## Ledger lines referenced by this group
L161: Task 10: minor (deferred to spec/parser): appending "CANCELLED" leaves the trade hash unchanged, so that retraction is treated as cosmetic.
L164: Task 10: MUST-FIX-BEFORE-MERGE — circuit breaker boundaries untested: 4 breaker mutants survive, two in the DANGEROUS direction (all-absent clause removed lets a 1-2 message window be voided on an all-None read; SUSPECT_MIN_ABSENT=4 lets 3/5 be voided). Code correct today; nothing pins it. Add a parametrized unit test of is_suspect.
L165: Task 10: minor (deferred): absences.seen() untested (removal survives)