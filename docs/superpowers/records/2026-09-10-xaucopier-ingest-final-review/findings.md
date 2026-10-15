# Plan 2 final review - consolidated findings

Source: workflow wf_7aa3e76c-47e (two runs). Every Critical/Important finding was independently reproduced by a verifier. The controller's RULINGS for each item are in the ledger (progress.md, the 'Ruling (...)' lines after 'FINAL REVIEW'); where a ruling differs from a reviewer's proposed fix, THE RULING WINS.

Severity shown is AFTER verification.

---

## F01 [Critical] The first-run seed takes its cutoff from each process's own start time, so a restart before the first seed succeeds re-seeds from the new start and silently skips every post the update stream dropped in the earlier run (a fourth way to lose a message)

- Reviewer area: B-listener-poll
- Owner in the fix wave: **group 2 (ingest core)**
- Location: `src/xaucopier/ingest/listener.py:957`

### Scenario
RAN, two ways. Script: C:\Users\Herbert\AppData\Local\Temp\claude\C--Users-Herbert-Downloads-Releases-26-X-release2-Releases-26-X-release2\da4bdf78-76b7-45a7-b86c-577750694fe8\scratchpad\final\listener-poll\probe_targeted.py (P1) and probe_p1b.py. Real Listener, HistoryPoll and store on a temp DB; fake client only.

(1) P1, flood. First `watch` for channel A: pointer 0, history ids 1-50 dated 3 days old. GetHistory is flood-waited (300 s, and again with 3600 s), so the startup seed fails and the poll's late seed is deferred every round. Message 51 arrives live and is stored. Message 52 is posted 90 s after start during a blackout. Message 53 arrives live. Run 1 ends, and at that point channel_state has no row. The user restarts 5 minutes after run 1 began, and the flood is over. Run 2's seed anchors at the newest message before (run-2 start - 60 s), which is 53. Result: pointer 53, stored [51, 53], 52 LOST. missed_by_eventstream=0, history_skipped=0. The only log line is the seed WARNING, which says 'Everything after it is ingested; nothing before it is.'

(2) P1b, no flood. Only the seed's anchor query (offset_date) times out during run 1. Ten posts arrive 15 s apart. 55 (at +75 s) and 58 (at +120 s) are dropped by the stream. Restart at +4 min. Result: pointer 60, stored [51,52,53,54,56,57,59,60], LOST [55, 58], every counter 0.

Why nothing catches it: the Task 9 fix anchors a late seed at listener-start-minus-60 s, but `_started_at` exists only in memory and only for this process. 'Pointer 0' is re-read as 'first run' by the next process, which anchors at its own start. Live-only chatter does not even create a channel_state row. Nothing on disk records that the channel was being watched, or since when.

This is NOT one of the tracked items. It is not the sub-second truncation, not the guard-checks-once item and not clock skew. It is also not the ledger minors at lines 114/144: those cover an EMPTY channel whose seed SUCCEEDED at 0. Here the channel is non-empty and no seed ever succeeded, so a persisted 'seeded' marker would not fix it. The planned fix for the guard-checks-once item ('re-run the seed while the pointer is 0') keeps channels unseeded for longer, which widens this hole.

Exposure: the very first `watch` on a new install (every channel is on its first run, and a new user is the one most likely to stop and restart), and every channel added later with `channels add` (Task 12).

### Reviewer's proposed fix
Persist the first-run cutoff per channel, and let the first attempt win. In start(), before any seed RPC, write `seed_cutoff_utc` in one writer job for every enabled channel whose pointer is 0 and which has no cutoff yet. Put it in a new channel_state column or a small table, and write it INSERT-only-if-absent. _seed_pointer must then use the stored cutoff, never this process's `_started_at`. Build this together with the clock-skew MUST-FIX, so the persisted value is min(local, server) at the first attempt. Regression test: run 1's seed fails for the whole run (a flood, or an anchor timeout); a blackout drops a post more than 60 s after start; restart; assert the post is stored (source poll or backfill) and counted in missed_by_eventstream.

### Verifier evidence
Probe: C:\Users\Herbert\AppData\Local\Temp\claude\C--Users-Herbert-Downloads-Releases-26-X-release2-Releases-26-X-release2\da4bdf78-76b7-45a7-b86c-577750694fe8\scratchpad\final\verify-B-listener-poll\probe_v1.py. Written independently of the original reviewer's scripts. It runs the real Listener, HistoryPoll and store on TemporaryDirectory DBs, with the fake client only, via tests/test_poll.py's ScriptedClient and SteppedTime. Run with PYTHONDONTWRITEBYTECODE=1 ./.venv/Scripts/python.exe probe_v1.py. History ids 1-50 are dated 3 days old; message 52 is the real MMH signal fixture.

S1 (the claim). Run 1: startup seed raises ConnectionError, then the poll's seed gets FloodWaitError(3600) and is deferred. 51 arrives live at +30s, 52 (SIGNAL) is blacked out at +90s, 53 arrives live at +120s, and run 1 is closed at +150s. After run 1: pointer=0, stored=[(51,'live'),(53,'live')], channel_state=[] (no row at all). Run 2 starts at +300s with no failures. Its seed offset_date is 11:04:00, which is run-2 start minus 60s. After run 2: pointer=53, stored=[(51,'live'),(53,'live')], signals=[], outbox_rows=0, metrics=[], alerts=[]. SIGNAL 52 is lost. The only log line is the seed WARNING ('Everything after it is ingested; nothing before it is.').

S1-CONTROL (the same, but run 1's seed succeeds): stored=[(51,live),(52,poll),(53,live)], signals=[(52,)], outbox_rows=2, missed_by_eventstream=1.

S2 (broader than claimed: no stream drop needed). Run 1's seed fails all run and 51 is stored live. Watch is stopped at +120s. SIGNAL 52 is posted at +180s while watch is off. Restart at +600s. After run 2: pointer=52, stored=[(51,'live')], signals=[], outbox=0, metrics=[], alerts=[]. The post is lost. S2-CONTROL (seed succeeded in run 1): 52 is stored as 'backfill' with 2 outbox rows. This breaks RUNBOOK.md:99-100's promise that watch 'catches up on the posts made while it was off'.

S3 (the trigger is steered by the product itself). Every GetHistory in run 1 hangs, with rpc_timeout=0.05 s. status._watch_lines at +150s prints: 'NOT HEALTHY: ... could read only 0 of 1 channel(s) ... If this lasts more than a few minutes: If the watch window is still open, click in it, press Ctrl+C and answer Y; then start it again'. The user follows that advice and restarts at +300s: pointer=53, stored=[51,53], and SIGNAL 52 is lost with 0 signals, 0 outbox rows, 0 metrics and 0 alerts.

S4 (bounds the window). The same as S1, but restart at +140s: 52 is recovered as 'backfill'. So the posts lost are exactly those not delivered live that are dated in [run-1 start minus 60s, run-2 start minus 60s).

Code read: _seed_pointer uses self._started_at, which only start() sets, in memory. _pointer_seeded is also in memory. A live ingest writes no channel_state row. So nothing on disk marks the channel as already watched. This is distinct from ledger minors 114 and 144 (an empty channel whose seed succeeded at 0) and from the three tracked seed MUST-FIX items: the clock-skew fix's min(local, server) start time is still per-process. It shares their root cause (first-run state is never persisted), and a persisted first-attempt cutoff would also close 114 and 144.

Safety check: the real checkout is unchanged. git status --porcelain is empty, HEAD is 374b2e4, there are no .db files, config/secrets does not exist, and the config/*.toml mtimes are unchanged (Sep 10).

Caveats on the claim. The exposure line is broader than the actual trigger. A plain stop-and-restart on a new install is harmless. The loss needs the first run's seed to fail on every attempt: the startup seed plus every 5 s poll round, until the process stops. That means a long GetHistory flood, repeated timeouts, or a guard refusal. On a healthy first run the startup seed succeeds in one RPC. Once that condition holds, though, status's own advice produces the restart (S3), and downtime posts are lost too (S2). On the proposed fix: a persisted cutoff that is never bounded could, after a long gap, ingest weeks of history as new rows. It should warn or alert, not silently ingest, when the stored cutoff is old.

---

## F02 [Critical] When the relay copy holds the trade, edits and deletions of the original are silently ignored, and claim() still hands out the entry

- Reviewer area: C-edits
- Owner in the fix wave: **group 3 (relay)**
- Location: `src/xaucopier/store/edits_repo.py:533`

### Scenario
I ran this (probe_e.py, probe_e2.py) with two watched channels: VIP A and relay B, both with the mmh parser. A's NewMessage is dropped in a blackout. B's copy of the same call arrives live 2 s later, so B's copy gets the signals row and ENTRY rows. A's message is later recovered by the poll and stored as a relay duplicate (kind='signal', no signals row). Then the poster edits A from BUY to SELL, or deletes A, and the event is delivered. `_edit_of_signal` finds row None and returns 'recorded' (INFO log only). `apply_deletion` finds signal_id None and just marks the message. Result: B's signal stays NEW, is not withdrawn, and gets 0 alerts. A real `outbox.claim()` on a fresh connection then returns B's BUY ENTRY. Control run with A ingested first: the same re-aim gives INVALID + CRITICAL SIGNAL_INVALIDATED_BY_EDIT with entries SKIPPED, and the same delete gives LIVE_SIGNAL_DELETED. Executed variant: acct-1 DONE on B, A re-aimed. The live BUY keeps running with no alert and acct-2's QUEUED BUY entry is still claimable. An SL edit of A is dropped too: no AMEND_SL, 0 alerts. This is the 'silent re-aim' failure, reached through relay dedup.

### Reviewer's proposed fix
Record which signal a relay duplicate matched at ingest (for example a messages.relay_of_signal_id column, or look it up again from the stored content_hash and posted_at with find_relay_duplicate). In `_edit_of_signal` (row is None) and in `apply_deletion` (kind='signal', no signals row), apply the policy to that signal. At minimum: a re-aim or unparseable edit invalidates it with a CRITICAL alert. A deletion raises a CRITICAL alert naming it; whether to withdraw it is a controller call, because a relay admin's tidy-up must not false-void the original. An SL/TP edit amends it or alerts. Never leave these as an INFO log.

### Verifier evidence
Probe: scratchpad/final/verify-C-edits/v1.py and v1b.py (harness vh.py), run with `.venv/Scripts/python.exe v1.py`. This drives the real Listener, HistoryPoll and store against a temp DB and the fake client. Channels are A=-1001111111111 (VIP) and B=-1002222222222 (relay), both mmh, with 2 accounts. A 4001 and B 7001 are the identical fixture call, with B posted 2 s later.

F1a (A emitted in a blackout, B live, 2 rounds, then an A BUY->SELL edit event is delivered): signals [(1, B, 7001, 'NEW', withdrawn 0, 'buy', v1)]; outbox both ENTRY QUEUED; alerts []; claim(acct-2) -> ('ENTRY', 1, 2).
Control F1a-ctl (A live first): signal INVALID, both ENTRY SKIPPED, CRITICAL SIGNAL_INVALIDATED_BY_EDIT, claim -> None.

F1b (B holds, A deleted, event delivered): A is marked deleted_detected_by='event'; B stays NEW, not withdrawn, alerts [], claim -> ENTRY 2.
Control: VOIDED plus WARNING SIGNAL_VOIDED_BY_DELETION, claim -> None.

F1c (B holds, acct-1 ENTRY DONE, A SL 4470->4460): signal v1, 0 FOLLOWUP rows, alerts [].
Control: v2 plus 2 FOLLOWUP (AMEND_SL) rows fanned out.

F1d (B holds, acct-1 DONE, A re-aimed): alerts [], and acct-2's QUEUED BUY is still claimable.

Widening, which needs no blackout of A alone:
- F1e: B's update is simply processed before A's. Same silent result.
- F1g: channels configured [B, A], both posted while the listener was down, so startup backfill stores B first. B holds; A's re-aim gives alerts [] and claim -> ENTRY.
- F1h: a blackout drops both, and the poll recovers them in config order. Same result.

Mitigant F1f: if the relay mirrors the edit onto 7001, B's signal goes INVALID with a CRITICAL alert. So the defect needs a relay that does not mirror edits or deletions.

The current config has one channel (disabled). The spec (G7) and Task 12 make relay pairs an intended setup.

Side note: count_missed counts only outcome 'new'. A message the stream missed that becomes a relay_duplicate adds nothing to missed_by_eventstream (F1a metrics []), and relay_duplicate has no log, alert or metric anywhere in src/.

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

## F04 [Critical] A poster's delete-and-repost of the same call is silently swallowed as a relay duplicate of the VOIDED original, so the repost never gets an ENTRY

- Reviewer area: A-store
- Owner in the fix wave: **group 3 (relay)**
- Location: `src/xaucopier/store/signals_repo.py:89`

### Scenario
I ran this end to end with the real Listener, poll and mmh parser, using the fake Telegram client and the tests/test_edits.py harness (probe p1_repost.py). Channel A posts signal 4001 and it is stored with 2 QUEUED ENTRY rows. The poster deletes 4001 (emit_delete). apply_deletion VOIDs it, withdraws it and SKIPs both ENTRY rows. Two minutes later the poster reposts the identical call as message 4002. find_relay_duplicate matches on trade_hash or first_trade_hash with no state filter, so it finds the VOIDED signal 1 and 4002 becomes 'relay_duplicate': a messages row with no signals row and no outbox rows. Result: signals=[(1,4001,'VOIDED')], QUEUED ENTRY rows=0. The only alert is WARNING SIGNAL_VOIDED_BY_DELETION ('Nothing had been executed'). Nothing logs, counts or alerts the swallowed repost; the listener does not log relay_duplicate at all. The reverse order (repost seen first, deletion detected afterwards, e.g. by the window re-read) gives the same result. The same happens when the original was INVALIDated (for example edited into something unparseable, which keeps its trade_hash) or withdrawn by an edit into chatter, and then reposted. This contradicts edits_repo's own design. Its docstring (lines 46-49) deliberately creates no signals row for an edit-created signal because 'it would make a genuine repost of the same call a relay duplicate, and so never traded', and it calls deletion-as-repost a real pattern (line 55). Once Plan 3 runs, this is a missed trade with a still-visible call on the channel.

### Reviewer's proposed fix
Make find_relay_duplicate ignore signals that were withdrawn or invalidated while unexecuted (state 'VOIDED', and arguably 'INVALID') when the new message comes from the same channel and was posted after the withdrawal. For the cross-channel and the 'repost seen first' orderings, either promote the surviving copy or raise a CRITICAL alert: when _withdraw_signal VOIDs a pending signal, look up same-hash messages inside the window that have no signals row and are not deleted, and alert on them. At minimum, record a metric and a log line for every relay_duplicate outcome so status can show it. Add the delete-then-repost and repost-then-delete orderings as tests.

### Verifier evidence
Probe v1_repost.py (scratchpad/final/verify-A-store) runs the real Listener, HistoryPoll and store through the tests/test_edits.py harness with FakeTelegramClient on a temp db. The same call is reposted as msg 4012, 2 min after 4001 (inside the default 900 s relay window). A delete-then-repost: signals=[(1,4001,'VOIDED')], ENTRY rows=[(1,acct-1,SKIPPED),(1,acct-2,SKIPPED)], QUEUED ENTRY=0, alerts=[WARNING SIGNAL_VOIDED_BY_DELETION], metrics=[]. B repost-then-delete by event: same result. B2 repost-then-delete by window re-read: same, plus CRITICAL WINDOW_VOIDED_SIGNALS and delete_missed_by_eventstream=1; nothing names the repost. C original edited to unparseable (INVALID), then a clean repost at +6 min: signals=[(1,4001,'INVALID')], QUEUED ENTRY=0. D original edited into chatter (VOIDED), then repost: QUEUED ENTRY=0. Control E, same delete-and-repost at +16 min (outside the window): signal 2 NEW with 2 QUEUED ENTRY, so the result depends only on the relay window. Control F, original already DONE and then deleted, then repost: the repost is still a relay duplicate. That is correct here: no double trade, and a CRITICAL LIVE_SIGNAL_DELETED fires. In every case the repost is a messages row with no signals row and no outbox rows. No log line mentions it, because grep finds 'relay' nowhere in src/xaucopier/ingest, cli or store/report.py. No metric or status line records it either. The SQL at signals_repo.py:89-95 has no state filter. This contradicts edits_repo.py:46-49, which avoids creating a signals row for exactly this reason, so that a genuine repost is still traded. The ledger has no ruling on it and no test pins it. Caveat: under Plan 3 an ENTRY is normally DONE or CLAIMED within seconds (spec 8.x: market order or BUY_LIMIT placed at once). A withdrawn original is then flagged rather than VOIDED, and deduplicating the repost is correct, as control F shows. The missed trade needs the original to still be unexecuted when it is withdrawn: a worker outage or backlog, a transient gate SKIP, or a delete faster than claim latency. It still meets the calibration's 'can cause a missed signal' and is completely silent.

---

## F04b [(same bug as F04, second reviewer)] If the poster deletes a signal nobody has taken yet and reposts the identical call, the repost is never traded

- Reviewer area: C-edits
- Owner in the fix wave: **group 3 (relay)**
- Location: `src/xaucopier/store/signals_repo.py:89`

### Scenario
I ran this (probe_b.py). Signal 4001 is stored. The poster deletes it and the event is delivered, so it is VOIDED and withdrawn with both ENTRY rows SKIPPED. One minute later the poster reposts the identical call as 4006 (same trade hash; an added emoji gives the same result). `find_relay_duplicate` matches the VOIDED row because it filters on no state, so 4006 becomes relay_duplicate: no signals row, 0 ENTRY rows, and only a WARNING SIGNAL_VOIDED_BY_DELETION. Nothing mentions the repost; relay_duplicate has no alert or metric anywhere. With the deletion event dropped it is the same: the repost is deduped against the still-NEW original, the window voids the original 10 s later, and the net result is 0 trades. This contradicts edits_repo.py:47-49, which refuses to create signals rows for edit-made signals because a signals row 'would make a genuine repost of the same call a relay duplicate, and so never traded'. A VOIDED row does exactly that. It meets the calibration's letter of Critical (a missed signal), but only when the original was never executed at deletion time: in watch-only mode, or in Plan 3 while the worker is not claiming. If the original was executed, deduping the repost is correct.

### Reviewer's proposed fix
At minimum, make it loud: when ingest classifies a message as a relay_duplicate of a signal that is withdrawn or VOIDED-and-never-executed, raise a CRITICAL alert naming both messages. When `_withdraw_signal` voids a signal, look for relay duplicates it already suppressed and alert on them. Then decide whether same-channel matches of a VOIDED, never-executed signal should be excluded from dedup. Do not exclude cross-channel matches blindly: that would trade a relay's copy of a call the VIP retracted.

### Verifier evidence
Probe: verify-C-edits/v3.py. Signal 4001 is stored (live, 1 round), then deleted, then 4006 is reposted 60 s later (NOW moved to match).

F3a (delete delivered, identical repost): signals [(1, 4001, 'VOIDED', withdrawn 1)] only. 4006 is stored as kind 'signal' with no signals row. The outbox holds only 4001's 2 ENTRY rows, both SKIPPED. alerts [('WARNING', 'SIGNAL_VOIDED_BY_DELETION', 4001)], nothing about 4006. claim(acct-2) -> None.

F3b (repost with an added emoji): identical result.

F3c (delete event dropped): the repost is deduped against the still-NEW 4001, then the window voids 4001 (WARNING plus CRITICAL WINDOW_VOIDED_SIGNALS). Net: 0 claimable entries.

Controls:
- F3d (repost with SL 4465, a different hash): a new signal 2 with 2 QUEUED ENTRY rows, claim -> ENTRY 4.
- F3f (identical repost 20 min later, outside the 900 s window): traded, signal 2 NEW.
- F3e (original executed, acct-1 DONE, before the delete): CRITICAL LIVE_SIGNAL_DELETED and the repost deduped. That is correct, as the finding says.

Cause: find_relay_duplicate filters on no state or withdrawn_at, and grep shows relay_duplicate is never logged, alerted or counted anywhere in src/. This contradicts the edits_repo.py:47-49 rationale.

The harm needs the original to be unexecuted when it is deleted: watch-only mode, or a Plan 3 worker that is paused, down or lagging. With a prompt worker the original is usually DONE first, and then the dedup is correct.

---

## F05 [Important] `channels add` (and `resolve-channels`) offers supergroups exactly like channels and switches one on with no warning. Nothing filters senders, so any member's chatter becomes a queued follow-up.

- Reviewer area: E-cli
- Owner in the fix wave: **group 5 (CLI) + group 2 (metadata capture)**
- Location: `src/xaucopier/cli/resolve.py:462`

### Scenario
RAN IT (probe3 part A: fake client, real CLI, real messages_repo.ingest). get_dialogs returned a broadcast channel and its discussion supergroup, both titled 'Gold Signals VIP'. `channels add`, then Enter, `2`, `yes`. The list printed `1. Gold Signals VIP` and `2. Gold Signals VIP` with nothing to tell them apart. The FORMAT_CAVEAT is about message layout, not groups. The result was 'Added: Gold Signals VIP is now channel 2, switched ON (parser mmh)' with channel_id -1006666666666 (megagroup). Two ordinary member messages were then ingested for that chat: 'anyone else going to close all now? market looks scary' and 'i moved my sl to entry already'. They became followups (close_all, sl_to_be) plus outbox rows (1,'ic_demo','FOLLOWUP','QUEUED') and (2,'ic_demo','FOLLOWUP','QUEUED'). `joined_channels()` deliberately includes supergroups ('Every channel (and supergroup)'). grep finds no megagroup/broadcast/sender/post_author check anywhere in ingest/ or cli/, and the spec and PLAN3-OBLIGATIONS never mention groups. A discussion group often shares its channel's title and receives auto-forwarded copies of every signal, so the group's own chat holds signals that member follow-ups can target. Reasoned only, not run: once Plan 3 runs, a stranger's 'close all now' is a QUEUED CLOSE_ALL for the live account.

### Reviewer's proposed fix
In `joined_channels()`/`_as_channel()`, list only `broadcast=True` channels by default. Either refuse `megagroup=True` or require a separate explicit confirmation that says in plain words 'this is a group: anyone in it can post, and their messages will be read as instructions'. Show the kind next to each title in the picker, and add one runbook sentence. Consider also warning when `entity.left` is true (not joined: the channel would only be read by the 5-second check).

### Verifier evidence
Probe: C:\Users\Herbert\AppData\Local\Temp\claude\C--Users-Herbert-Downloads-Releases-26-X-release2-Releases-26-X-release2\da4bdf78-76b7-45a7-b86c-577750694fe8\scratchpad\final\verify-E-cli\probe_groups.py. It runs the real cli_main.main on temp config copies. Only the Telegram client (a fake subclass), load_session and the mutex are doubled.

A1, `channels add` then Enter, 2, yes. get_dialogs returned a broadcast Channel(id=5555555555, broadcast=True) and a supergroup Channel(id=6666666666, megagroup=True), both titled 'Gold Signals VIP'. Output: '1. Gold Signals VIP' / '2. Gold Signals VIP', then 'Found: Gold Signals VIP -- numeric id -1006666666666', the mmh FORMAT_CAVEAT (about message layout only), then 'Added: Gold Signals VIP is now channel 2, switched ON (parser mmh).' Exit 0. The word 'group' never appears in stdout or stderr. The appended block has channel_id = -1006666666666 and enabled = true.

A2, `channels add --link https://t.me/goldchat --yes`, where get_entity returns the supergroup. Exit 0, same 'Added ... switched ON', no warning. `_as_channel` checks only isinstance(entity, types.Channel), which a supergroup passes.

A3, `resolve-channels` on the SHIPPED placeholder 'MARKET MASTERY HUB / COMMUNITY', with dialogs = MMH VIP (broadcast) and a supergroup titled 'MARKET MASTERY HUB / COMMUNITY'. Picking 2 printed "Saved: 'MARKET MASTERY HUB / COMMUNITY' is now channel -1007777777777, switched on." The file now has that id and enabled = true, and no warning was given. So the user's own primary setup path has the same property, not just `channels add`.

B, real `watch` (listener, poll and store all real) on the channels.toml from A1. Messages emitted to chat -1006666666666:
- 201: the signal (the auto-forward)
- 202: 'anyone else going to close all now? market looks scary'
- 203: 'i moved my sl to entry already'
- 204: 'close everything, this is going to reverse', reply_to=201
- 205: 'sl 4400 for me', reply_to=201

DB after the run:
- messages: 201 signal, 202-205 followup, all source 'live'
- followups: (1, 202, close_all, reply None), (2, 203, sl_to_be, reply None), (3, 204, close_all, reply_to 201), (4, 205, amend_sl price_hint 4400.0, reply_to 201)
- outbox: (1, ic_demo, ENTRY, QUEUED, signal 1), then (2..5, ic_demo, FOLLOWUP, QUEUED, followups 1..4)

Grep: no megagroup, broadcast, sender, from_id, post or fwd check anywhere in src/. The only hit is the docstring 'Every channel (and supergroup)' at resolve.py:463. The messages table (schema.sql:4-15 plus migrations) has no sender, post or fwd_from column, and normalise_message reads none of them. So Plan 3 cannot tell a member's message from an admin's post after the fact, and the fix cannot be left to Plan 3. Spec 8.1 rule 1 makes reply_to authoritative, and a member's comment under a channel post is exactly such a reply. The 204 case would therefore resolve to the live signal: a stranger's 'close everything' closes the trade. Rule 3 ('exactly one live signal') would resolve 202/203 the same way. That last step is reasoned from the spec; Plan 3 is not built yet.

The ledger, PLAN3-OBLIGATIONS.md, the plan and the RUNBOOK do not mention groups or senders, so this is not already tracked. After the run, the repo is unchanged: git status clean, config/*.toml sha256 identical, no config/secrets, no .db files.

Caveat on the proposed fix: the shipped source is titled 'MARKET MASTERY HUB / COMMUNITY' and the spec says its posts are 'forwarded from MARKET MASTERY HUB VIP'. It may itself be a supergroup. If it is, a hard 'refuse megagroup' would lock the user out of their only source, and this finding becomes Critical on the default path. The safer fix: show the kind next to each entry in the picker, require an explicit plain-words confirmation for a group, and record message.post / from_id / fwd_from so a later filter can accept only admin posts or linked-channel auto-forwards. This should also be added as a VERIFY-ON-FIRST-LOGIN item: check whether the real MMH COMMUNITY is broadcast or megagroup.

---

## F06 [Important] A dead-session transport -404 (Telethon AuthKeyNotFound, not an RPCError) is classified as retryable: watch exits 4 "safe to start it again" forever, with no TELEGRAM_SESSION_DEAD alert

- Reviewer area: D-security
- Owner in the fix wave: **group 2 (ingest core)**
- Location: `src/xaucopier/ingest/client.py:56`

### Scenario
I ran this two ways. (1) Real Telethon 1.44: a real TelegramClient and MTProtoSender behind a Connection subclass that never opens a socket and answers every send with the 4-byte transport error -404 ('auth key not found'). I drove it through cli_main.main(["watch", ...]) twice, as a supervisor that restarts on exit 4 would; only the session loader and the mutex were stubbed. Telethon logs 'Server does not know about the current auth key; the session may need to be recreated', and client.connect() raises telethon.errors.common.AuthKeyNotFound. That is not in TERMINAL_AUTH_ERRORS, so run_listener() does not catch it, and watch.py:325's catch-all returns 4. Both attempts gave exit 4 ('watch stopped unexpectedly -- safe to start it again'), watch_stopped=4, unacked alerts [], classify_auth_error(AuthKeyNotFound())=='retryable', exit_code_for(...) is None, isinstance(RPCError) False. (2) The existing permanent-error rig (TroubleClient from tests/test_cli_permanent_errors.py): AuthKeyNotFound raised at connect, get_dialogs, or from run_until_disconnected (Telethon's update loop funnels it there through its generic `except Exception`) gives exit 4, no alert and no 'setup-login' text. The 401 control gives exit 3 plus TELEGRAM_SESSION_DEAD. The real `status` run after three such restarts prints 'ALERTS: none' and 'exit code 4: watch stopped unexpectedly -- safe to start it again'. The runbook's exit-4 row says 'Safe to start watch again', and exit_codes.RESTARTABLE={4}. So Plan 3's supervisor would restart it forever, each run dead within a second and dialling Telegram with a dead key, while nothing is recorded and the user is never told to re-login. -404 is how MTProto reports a key the server no longer knows (Telethon's own docstring: 'you may need to recreate the session file and login again'). The runbook's exit-3 row promises that a session 'logged out ... from Settings -> Devices' or 'used ... somewhere else at the same time' stops watch for good, and the forgotten-key half of those cases arrives as -404, not a 401. NOT verified: which live Telegram actions produce -404 rather than a 401, since contacting Telegram is forbidden. Telethon says -404 can occasionally be temporary, which is why I rate this Important rather than Critical; Task 11 rated the same class (permanent error exits 4) Critical. Note: the fake-only 'backfill' variant of this probe hangs, but that is an artifact of the fake (its run_until_disconnected never ends). In real Telethon a -404 also ends run_until_disconnected().

### Reviewer's proposed fix
Add `telethon.errors.common.AuthKeyNotFound` to TERMINAL_AUTH_ERRORS. It then flows automatically through PERMANENT_TELEGRAM_ERRORS to run_listener, the backfill re-raise, the poll's terminal branch, resolve-channels and channels add. Add it to PERMANENT in tests/test_cli_permanent_errors.py (it takes no `request` argument, so the table's `error_class(request=None)` needs a special case) and to the client.py docstring and the runbook exit-3 row. A rare temporary -404 then costs one manual restart, because the existing auto-clear of TELEGRAM_SESSION_DEAD after a successful poll round already covers that case. An alternative, if a single -404 should not be final: exit 3 only after N consecutive AuthKeyNotFound at startup.

### Verifier evidence
All probes are in C:\Users\Herbert\AppData\Local\Temp\claude\C--Users-Herbert-Downloads-Releases-26-X-release2-Releases-26-X-release2\da4bdf78-76b7-45a7-b86c-577750694fe8\scratchpad\final\verify-D-security\. The checkout was unchanged afterwards: HEAD 374b2e4, git status clean, no .db files, no config/secrets, and nothing contacted Telegram (asyncio create_connection and sock_connect were patched to raise).

PROBE 1, real Telethon 1.44 (probe1_real_telethon.py normal). I ran the real TelegramClient, MTProtoSender and the real build_client(). I patched only client_module.TelegramClient so it injects a ConnectionTcpFull subclass. That subclass opens no socket and answers every packet with a normal TcpFull frame whose 4-byte body is int32 -404. load_secrets and the mutex were stubbed, and the session was a StringSession with a random 256-byte AuthKey. I ran cli_main.main(['watch', ...]) twice, then 'status'. Output:
  watch attempt 1: exit=4. Log: 'telethon.network.mtprotosender: Server does not know about the current auth key; the session may need to be recreated', then 'xaucopier.cli.watch: watch stopped on an unexpected error', then 'telethon.errors.common.AuthKeyNotFound', raised from listener.py:1010 'await self.client.connect()'.
  watch attempt 2: exit=4, the same.
  watch_stopped = 4; unacked alerts = []; fake transport connects=2, packets=2 (each run died after 1 packet).
  status: 'ALERTS: none' and 'NOT RUNNING: ... exit code 4: watch stopped unexpectedly -- safe to start it again.'
  classify_auth_error(AuthKeyNotFound()) = retryable; exit_code_for = None; isinstance(RPCError) = False.

PROBE 2, the repo's own rig (test_probe2_rig.py). I reused TroubleClient and watch_rig and added a connect() failure site. I ran it with pytest -p no:cacheprovider, --rootdir and --basetemp in the scratchpad. 9 passed. Output:
  AuthKeyNotFound at connect, at get_dialogs and at the update loop: exit=4, watch_stopped=4, unacked=[], 'setup-login' in stderr=False.
  AuthKeyNotFound at poll: exit=4 (only the fake's watchdog stopped it). The poll made 32 failed reads in about 1 s because it treats the error as retryable. In real Telethon, a -404 also fails run_until_disconnected(), so the real outcome is still exit 4.
  401 control (AuthKeyUnregisteredError) at connect, get_dialogs, the update loop and the poll: exit=3, unacked=[('CRITICAL','TELEGRAM_SESSION_DEAD')], 'setup-login' in stderr=True, poll failed_reads=1.
  Supervisor simulation: 3 restarts gave [(4, restartable=True)] x3. The real status then printed 'ALERTS: none' and 'exit code 4: watch stopped unexpectedly -- safe to start it again'.

Refutation attempts that failed:
- Nothing in src/, tests/, docs, the spec or the ledger mentions AuthKeyNotFound or -404, so this is not a settled ruling. The Task 11 ruling lists RPCError classes only, and AuthKeyNotFound is a plain Exception.
- run_listener catches only ListenerStopped, TERMINAL_AUTH_ERRORS and API_CREDENTIAL_ERRORS. watch.py:325's catch-all therefore returns 4, and exit_codes.RESTARTABLE={4}.
- Telethon's own code treats it as needing a new session: the docstring says 'you may need to recreate the session file and login again', and DestroyAuthKeyOk is mapped to AuthKeyNotFound.

Why Important and not Critical:
- The session is dead either way, so the misclassification does not itself lose signals. What it costs is the diagnosis: the user is told to restart, not to run setup-login, and a Plan 3 supervisor would restart on exit 4 forever.
- status does not report healthy; it shows NOT RUNNING.
- Which real Telegram actions send -404 rather than a 401 is unverified, since no contact with Telegram was allowed. Telethon says -404 can be temporary. A plausible real path: Telegram destroys the key after AUTH_KEY_DUPLICATED, so the next start gets -404. In that sequence the earlier TELEGRAM_SESSION_DEAD alert is still unacked, which partly mitigates it.
- The proposed fix is sound. Adding AuthKeyNotFound to TERMINAL_AUTH_ERRORS makes a temporary -404 cost one manual restart, and the existing auto-clear covers it. The test table's error_class(request=None) needs a special case for it.

Side observation (unverified framing, not part of the claim): I sent -404 in the TcpFull length/seq header instead, as Telethon issue 4042 reports for -429 (probe1 odd mode). Real Telethon then never raises AuthKeyNotFound. It logs 'Server sent invalid buffer' and reconnects in a tight loop: 1105 reconnects in 30 s. connect() never returns, and watch hangs in start() with only watch_started stamped. status shows 'STARTING', then 'NOT HEALTHY ... may be stuck connecting' after 2 min. The proposed fix would not cover this case; only a bounded connect() at startup would.

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

## F08 [Important] Relayed follow-ups are neither deduplicated nor linked to the signal. Once Plan 3 runs, a CLOSE_HALF either executes twice or finds no target, depending on which channel's copy of the signal arrived first.

- Reviewer area: G-crosscut
- Owner in the fix wave: **group 3 (relay)**
- Location: `C:\Users\Herbert\Projects\xaucopier\src\xaucopier\store\messages_repo.py:278`

### Scenario
RAN, three ways.

Through the real Listener, HistoryPoll, Writer and fake client (e2e.py): A/4001 (fixture) was stored as a signal, and B/5001 60 s later became a relay_duplicate. 'Move SL to BE' replying on each channel (A/4002 to 4001, B/5002 to 5001) produced two followups rows with target_signal_id NULL and two QUEUED FOLLOWUP rows. claim() handed out ENTRY 1, FOLLOWUP 1 and FOLLOWUP 2.

probe_g.py P1a: the same with 'Close half now'. Claims were [ENTRY 1, FOLLOWUP 1, FOLLOWUP 2], and nothing marks follow-up 2 as a relay of follow-up 1.

probe_g.py P1b: B's copy stored first (A's copy blacked out, then recovered by the poll). The signal row is B/5001 and A/4001 is the relay_duplicate. A's 'Close half now' replying to 4001 is stored on channel A with reply_to 4001. Channel A has 0 signal rows, and `messages` has no column linking A/4001 to signal 1.

REASONED, not run (Plan 3): under spec 8.1, A's close-half finds no signal at (A,4001) and no live signal on channel A, so it becomes 'no action + alert' and is missed. If Plan 3 instead follows the messages.relay_of_signal_id link that ruling C2 adds, P1a's two rows both resolve to signal 1. CLOSE_HALF then runs twice: ceil(4/2)=2 legs, then ceil(2/2)=1, so 3 of 4 legs close. Either Plan 3 choice is wrong in one of the two orders, and Telegram delivery timing picks the order.

### Reviewer's proposed fix
When a follow-up replies to a relay-copy message, record the resolved target on the followups row, using relay_of_signal_id from the C2 fix. Before fan-out, store a follow-up without an outbox row as a relay duplicate, and log and count it, when either: an existing follow-up with the same op and normalised content hash targets the same signal within the relay window; or it carries the same fwd_from channel_post (see the delayed-relay finding). Extend ruling C2 to state follow-up semantics. Add a PLAN3 obligation that reply resolution follows relay_of_signal_id.

### Verifier evidence
Probe: v_probe.py F2a/F2b, via ingest() on temp DBs.

F2a, 'Close half now' and 'Move SL to BE': A/4001 gives new, B/5001 (+60 s) gives relay_duplicate, A/4002 (reply 4001) gives new/followup, B/5002 (reply 5001) gives new/followup. Result:
- signals: [(1, A, 4001)]
- followups: (1, A, 4002, close_half, reply 4001, target NULL) and (2, B, 5002, close_half, reply 5001, target NULL)
- outbox: ENTRY 1, FOLLOWUP fu1, FOLLOWUP fu2, all QUEUED
- claims: [('ENTRY',1,None), ('FOLLOWUP',None,1), ('FOLLOWUP',None,2)]
- The sl_to_be run gives the same result.

The messages columns have no relay link: channel_id, message_id, kind, content_hash, raw_text, edit_date_utc, received_at_utc, posted_at_utc, source, deleted_at_utc, deleted_detected_by.

F2b, B stored first: B/5001 gives new and A/4001 gives relay_duplicate. The signals row is (1, B, 5001), and channel A has 0 signal rows. A/4002 close_half is stored with reply 4001 and target NULL, and messages still shows A/4001 as kind='signal'.

The Plan 2 facts are confirmed. The double CLOSE_HALF is reasoned, not run, and holds only if Plan 3 resolves a reply to a relay copy through the relay link. Ruling C2's pending messages.relay_of_signal_id makes that choice tempting, and no obligation says what to do for follow-ups.

With the spec 8.1 rules taken literally, a follow-up relayed on both channels executes once, from whichever copy holds the signal, plus one alert. A miss plus an alert happens only when the follow-up is not relayed to the channel that holds the signal. So the claim that either choice is wrong in one of the two orders is overstated, and nothing here executes in Plan 2. This is a real gap at the Plan 2/Plan 3 boundary: ingest needs follow-up relay dedup, or Plan 3 needs an obligation. Downgraded from Critical.

---

## F09 [Important] Relay dedup compares forward times within 900 s and ignores fwd_from, so a copy relayed more than 15 minutes late becomes a second ENTRY

- Reviewer area: G-crosscut
- Owner in the fix wave: **group 3 (relay) + group 2 (metadata capture)**
- Location: `C:\Users\Herbert\Projects\xaucopier\src\xaucopier\store\messages_repo.py:300`

### Scenario
RAN the mechanism (probe_window.py): fixture on channel A at time P, identical copy on channel B at P+lag.
- Lag 60 s or 900 s: relay_duplicate, 1 signal, 1 ENTRY.
- Lag 901 s, 1800 s or 7200 s: outcome new, 2 signals, 2 claimable ENTRY rows.

The listener stores message.date as posted_at (listener.py:286). For a forwarded message that is the time it was forwarded into the relaying channel. The listener captures nothing from fwd_from.

REASONED (Telegram semantics; the real lag is unknown until first login): spec 6.1 says the user's COMMUNITY source forwards VIP, and G7 exists so the user can follow both, which Task 12 now enables. If COMMUNITY republishes VIP calls more than 15 minutes late (a common free/VIP arrangement), each such signal opens a second full set of legs once Plan 3 runs. G6's age gate uses the copy's own (forward) timestamp, so it passes the late copy.

### Reviewer's proposed fix
The C10 fix already records the forwarded-from channel id. Also record fwd_from.channel_post and fwd_from.date. Treat a (forwarded-from channel id, channel_post) pair that matches an already-stored message as an exact relay duplicate regardless of lag, for signals and follow-ups alike. Keep the hash window for manual reposts. Add a VERIFY-ON-FIRST-LOGIN item: measure COMMUNITY's forward lag.

### Verifier evidence
Probe: v_probe.py F3. The fixture goes on channel A at P and an identical copy on channel B at P+lag, then I drain claim().

- lag 60 s: relay_duplicate, signals=1, queued ENTRY=1, claims [('ENTRY',1)]
- lag 900 s: relay_duplicate, signals=1, queued ENTRY=1
- lag 901 s: new, signals=2, queued ENTRY=2, claims [('ENTRY',1),('ENTRY',2)]
- lag 1800 s: new, signals=2, 2 ENTRY claims
- lag 7200 s: new, signals=2, 2 ENTRY claims

The code matches the claim:
- listener.py:286 stores posted_at_utc=_to_utc(message.date).
- A grep for fwd_from or channel_post over src and tests finds nothing.
- find_relay_duplicate compares abs(posted difference) <= 900 s.
- The listener never passes relay_window_seconds, so the default of 900 applies.
- policy max_signal_age_seconds = 120 is measured against the copy's own date, so a late forward passes it.

The mechanism is real. The harm depends on two things not yet known: the user following both VIP and COMMUNITY (only COMMUNITY is configured today), and COMMUNITY's actual forward lag. This is not a duplicate of ledger 166, C4/C6 or C10, since C10 records the forwarded-from channel id but not channel_post.

---

## F10 [Important] The config loaders accept non-booleans for the gate fields enabled and allow_live. A quoted "false" loads as truthy, so the gate fails open.

- Reviewer area: G-crosscut
- Owner in the fix wave: **group 1 (store/config)**
- Location: `C:\Users\Herbert\Projects\xaucopier\src\xaucopier\config\accounts.py:99`

### Scenario
RAN (probe_g.py P3) on temp copies of config/accounts.toml:
- `allow_live = "false"` loads as AccountCfg.allow_live == 'false', which is truthy.
- An account with `enabled = "false"` loads truthy, and ingest fans out a QUEUED ENTRY row to it.
- A channel with `enabled = "false"` (channels.py:82) passes the listener's `if channel.enabled` filter (listener.py:400), so it is watched, and status shows 'switched on'.
The loaders guard ordinal, magic_base and channel_id against bool-for-int (ruling at ledger line 47), but not the reverse case on the fields that act as gates.

REASONED, latent until Plan 3: allow_live is the hard gate against a live account (spec G1 and the accounts.py docstring). A quoted "false" opens it.

### Reviewer's proposed fix
Require isinstance(value, bool) for channel and account `enabled` and for `allow_live`, and raise ConfigError otherwise. Fold in the login type guard from ledger minor 50.

### Verifier evidence
Probe: verify-G-crosscut/v_probe5.py, on temp copies of config/*.toml.

Accounts:
- allow_live="false" loads as 'false' (bool True).
- enabled="false" loads as 'false' (bool True).
- allow_live=1 loads as 1 (True).
- enabled=0 loads as 0 (False).

Channels:
- With a real id, enabled="false" loads as 'false'. watch.check_setup lists the channel as enabled, and status shows 'MARKET MASTERY HUB / COMMUNITY (id -1001111111111) -- switched on'.
- With the placeholder id 0, enabled="false" instead raises a misleading ConfigError, 'enabled with channel_id=0'.

Ingest with the enabled="false" account queues an outbox row [('ic_demo','ENTRY','QUEUED')], and ingest_new_no_accounts stays 0. The cause is the missing isinstance(bool) check at accounts.py:99-100 and channels.py:82; the listener's `if channel.enabled` filter at listener.py:400 then passes the string.

Trigger: the value has to be hand-edited with quotes. The runbook never tells the user to edit these fields, and `channels on/off` writes real booleans. Still, allow_live is the hard gate against a live account (G1), it fails open, and the fix is a few lines. This matches the ruling at ledger line 47 on bool-for-int.

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

## F12 [Minor] outbox.mark() has no ownership fence: the stale claimant's mark is accepted and the current claimant's is refused

- Reviewer area: A-store
- Owner in the fix wave: **group 1 (store/config)**
- Location: `src/xaucopier/store/outbox.py:349`

### Scenario
Ran (p2_outbox.py, case D). pid 1 claims row 1 at T0. At T0+90s+1us, pid 2 claims the same row, so its lease was reclaimed (attempts=2). pid 1 then finishes late and calls mark(outbox_id=1, DONE), which is ACCEPTED: the row becomes ('DONE', 'pid 1 done (late)', claimed_by_pid=2, attempts=2). pid 2's own mark then raises ValueError. mark()'s docstring says it refuses a row whose lease was 'reclaimed by someone else', which is exactly the case it accepts. The WHERE clause only checks state='CLAIMED', and the API takes no claimant token (pid, claimed_at or attempts), so a caller cannot even express the fence. This is latent until Plan 3. Once a worker is slower than the 90 s lease, the outbox records the wrong owner's result, and the owner that is really executing gets an exception after it has sent orders.

### Reviewer's proposed fix
Give mark() a fence: require the claimed_by_pid and the attempts value (or claimed_at_utc) that claim() returned, and match them in the WHERE clause (`... AND claimed_by_pid = ? AND attempts = ?`). Raise when they do not match. Add a test where the lease is stolen and the stale owner then tries to mark.

### Verifier evidence
Probe v2_outbox.py, case F2. pid1 claims at T0 and pid2 claims at T0+90s+1us, getting (claimed_by_pid=2, attempts=2). pid1's late mark(DONE) is ACCEPTED, giving ('DONE','pid 1 done (late)', claimed_by_pid=2, attempts=2). pid2's mark then raises ValueError. mark() takes only conn, outbox_id, state and reason, so no fence can be expressed. The docstring's 'reclaimed by someone else' guarantee is false. Case F2b is worse: a stale owner's SKIPPED mark lands while pid2 is the live owner, and edits_repo.trade_status then reports 'pending' for a signal pid2 is actually executing. The claimed harm is overstated, though. The double execution comes from the lease steal at claim time, and a mark fence cannot prevent it. Exactly one of the two marks raises with or without a fence (whichever lands second), so the steal is detected either way. The fence only changes which worker is told and keeps reason and claimed_by_pid consistent. trade_status-via-outbox is already a recorded interim proxy that Plan 3 replaces with the legs table. There are no callers on this branch (grep finds no outbox.claim, mark or sweep_expired outside store/outbox.py). Latent: a Plan 3 worker-design hardening item.

---

## F13 [Minor] claim() reclaims expired leases itself without recording outbox_lease_reclaimed, contradicting the module's stated guarantee

- Reviewer area: A-store
- Owner in the fix wave: **group 1 (store/config)**
- Location: `src/xaucopier/store/outbox.py:305`

### Scenario
Ran (p2_outbox.py, cases C and C'). A row is claimed at T0, and claim() is called again at T0+90s+1us with no sweep_expired() first. claim() takes the expired CLAIMED row directly (attempts=2), and outbox_lease_reclaimed stays at 0. On the sweep_expired() path the same reclaim records metric=1. The module docstring (lines 15-19) says every reclaim is recorded because 'a reclaim means a worker died mid-claim ... not just a queue mechanic that quietly heals itself', and report/status lists outbox_lease_reclaimed among the counters that should stay at zero. Unless Plan 3 always sweeps immediately before every claim, and even then within the gap between the two calls, dead workers heal silently while status shows 0. This is latent until Plan 3.

### Reviewer's proposed fix
In claim(), count the rows it takes that were CLAIMED rather than QUEUED (for example, capture the prior state with a CTE, or run the same reset-and-bump as sweep_expired inside claim's own BEGIN IMMEDIATE before selecting) and bump LEASE_RECLAIMED_METRIC in the same transaction. Alternatively, drop the CLAIMED-expired branch from claim() so that only sweep_expired can reclaim.

### Verifier evidence
Probe v2_outbox.py, case F3. Claim at T0, then claim at T0+90s+1us with no sweep first: the row is reclaimed with (pid 2, attempts 2) and metrics=[]. Case F3' sweeps first and then claims: swept 1, metrics=[('2026-09-24','outbox_lease_reclaimed',1)]. The code confirms it: claim()'s second UPDATE (outbox.py:305-321) selects state='QUEUED' OR expired CLAIMED and never counts the CLAIMED ones. The module docstring (lines 13-19) promises that every reclaim is recorded. The row is still handed out, so no trade is lost, and attempts>1 remains on the row as a trace. Only the status counter undercounts, and dead workers are also meant to be caught by G15 executor heartbeats. It is latent with no callers on this branch. An observability contradiction worth fixing when Plan 3 wires the worker.

---

## F14 [Minor] claim()/sweep_expired() compare ISO strings, so a timezone-aware now_utc that is not UTC reclaims a fresh lease (two holders of one ENTRY)

- Reviewer area: A-store
- Owner in the fix wave: **group 1 (store/config)**
- Location: `src/xaucopier/store/outbox.py:282`

### Scenario
Ran (p2_outbox.py, case E). pid 1 claims with now_utc=T0 expressed as UTC-5, which stores claimed_at_utc='2026-09-24T05:00:00-05:00'. Five seconds later a claim with a UTC now_utc gets 'RECLAIMED by pid 2, attempts=2', and sweep_expired 5 s after a UTC-5 claim resets 1 fresh lease. The cause is that `claimed_at_utc < cutoff_iso` compares strings across different offsets. A naive now_utc is also accepted by claim() and stored without an offset ('2026-09-24T10:00:00'), while sweep_expired crashes on a naive now only when it has something to count. Every other store writer normalises or refuses: metrics.day_key_for converts to UTC, and alerts.record and heartbeats.beat both refuse naive times and store astimezone(UTC). The lease functions do neither. Once Plan 3 runs, two workers can hold the same ENTRY and each open the trade. This is latent, and triggered by a caller that passes local or MT5-derived time.

### Reviewer's proposed fix
In claim() and sweep_expired(), refuse a naive now_utc (as day_key_for does) and normalise with `now_utc = now_utc.astimezone(UTC)` before computing cutoff_iso and now_iso, so that every stored claimed_at_utc uses the '+00:00' form. Add a test that claims with a non-UTC offset and then sweeps with UTC.

### Verifier evidence
Probe v2_outbox.py, case F4. A claim with now_utc=T0 at UTC-5 stores '2026-09-24T05:00:00-05:00'. A UTC claim 5 s later is RECLAIMED (pid 2, attempts 2). Case F4b: a sweep 5 s after a UTC-5 claim resets 1 fresh lease. I also found the opposite direction, which the finding does not describe (F4c). After a claim at UTC+3, a UTC claim 150 s later does NOT reclaim, and a sweep 2 h later resets 0, so a dead worker's ENTRY stays stuck for the length of the offset. One sub-claim is wrong. On the naive path, claim() accepts a naive now_utc and stores '2026-09-24T10:00:00' (F4d). But sweep_expired raises ValueError on a naive now even with nothing to reclaim (F4e), because metrics.day_key_for runs unconditionally at outbox.py:387. It does not raise only 'when it has something to count'. So a worker that uses naive time consistently crashes loudly on its first sweep, and only the aware-but-not-UTC case is silent. The mechanism is real and inconsistent with the store's normalise-at-the-boundary convention (day_key_for, alerts, heartbeats, listener._to_utc), and the fix is two lines. But the trigger needs a caller that violates a parameter literally named now_utc. There are no callers on this branch. The ledger already classes the sibling gap (ingest() not validating tz-awareness) as a deferred minor. Recommend fixing it cheaply now or adding it to PLAN3-OBLIGATIONS, since G8 means the worker will handle broker server time.

---

## F15 [Minor] `channels off N` stops all tracking of channel N's already-recorded signals but leaves their ENTRY rows claimable. The poster's later deletion or 'close all' is never seen, and no Plan 3 obligation or runbook line covers this.

- Reviewer area: E-cli
- Owner in the fix wave: **group 2 (watch-start skip) + group 5 (channels off text, runbook)**
- Location: `src/xaucopier/cli/channels.py:310`

### Scenario
RAN IT (p1_off.py). It uses the real CLI, config loader, Listener, HistoryPoll and store, with a fresh fake client per 'process' and a temp config and DB. Setup: channels.toml has A (ordinal 1) and B (ordinal 2), both ON. Run 1: B posts the MMH fixture signal 7001, giving signals=[(1,B,7001,'NEW')] and outbox rows ENTRY acct-1 QUEUED and ENTRY acct-2 QUEUED. Then `channels off 2` exits 0 with 'Channel 2 (Second channel B) is now OFF. watch reads this list when it starts...'. `channels` then lists B as OFF with 'messages: 1 | signals: 1' and nothing about the queued entries. Run 2 (restart with the new file): B's poster deletes 7001 and posts 'close all now' as a reply to it. Both events are delivered and there are 0 handler errors. FINAL: messages unchanged (7001 not marked deleted, 7002 never stored); signal 1 still NEW and not withdrawn; both ENTRY rows still QUEUED; alerts=[]; metrics=[]; claim(acct-2) returns ('ENTRY', 1, 2), so a worker would open the withdrawn call. CONTROL with B left ON, same events: signal VOIDED and withdrawn, both ENTRY rows SKIPPED, WARNING SIGNAL_VOIDED_BY_DELETION, the close-all stored as a FOLLOWUP, and claim() returns the follow-up rather than the entry. Cause: listener._channels_by_id and poll.enabled_channels drop OFF channels completely. That is correct for new posts, but it also drops edits, deletions and follow-ups of signals that are still queued, or, under Plan 3, still live. claim()'s _DEAD_ENTRY check looks only at signal state and withdrawn_at_utc, never at whether the channel is enabled. RUNBOOK.md:163 tells the user OFF 'stops taking signals from channel number 2. Nothing about it is deleted'. PLAN3-OBLIGATIONS.md (#1-#33) has no item on OFF channels; #29 covers only the edit window of enabled channels. Harm today is zero: Plan 2 is watch-only, and obligation #14 discards the backlog. Once Plan 3 runs, switching off a channel with an unexecuted or live signal means a deleted call is still traded and that trade's CLOSE_ALL, SL_TO_BE and AMEND_SL follow-ups are silently dropped. REASONED, not run (the fix is not built yet): the ledger's M12 ruling makes `channels on` re-seed at 'now', which would make the drop permanent across an OFF/ON cycle. The 'close all now' posted while OFF would never be read even after switching back on.

### Reviewer's proposed fix
Before the fix wave implements the M12 ruling, decide what OFF means for a channel that still has signals in play, and record it as a PLAN3-OBLIGATIONS item. Option (a): watch keeps reading edits, deletions and follow-ups (not new signals) for an OFF channel while any of its signals has a QUEUED or CLAIMED entry or live legs. Option (b): at watch start, SKIP the QUEUED ENTRY rows of disabled channels with the reason 'channel switched off', and make claim() refuse them; this is the fail-safe direction, and it needs to be paired with (a) for live legs under Plan 3. In both cases `channels off N` should print how many of that channel's signals are still queued and what OFF does to them. RUNBOOK.md:163 should say in plain words that switching a channel off also stops following up on trades already taken from it. The M12 re-seed must not skip posts that target signals still in play.

### Verifier evidence
Probe: scratchpad/final/verify-E-cli/v_off.py. I wrote it myself and did not reuse p1_off.py. It runs the real cli_main.main for `watch`, `channels off 2` and `channels`, with the real loader, Listener, HistoryPoll and store. Telegram is the ScriptedWatchClient fake, and the DPAPI, mutex and poll interval are doubled as in tests/watch_rig.py. Config: A=-1001111111111 (ordinal 1) and B=-1002222222222 (ordinal 2), both ON, with the real accounts.toml (ic_demo). Run 1: B posts the MMH fixture as 7001 live, giving signals=[(1,B,7001,'NEW',None)] and outbox=[(1,'ic_demo','ENTRY','QUEUED',None)]. Run 2 uses a fresh client with the same history.

DEL-OFF: `channels off 2` exits 0 with 'Channel 2 (Second channel B) is now OFF. watch reads this list when it starts...'. `channels` lists B as OFF with 'messages: 1 | signals: 1' and nothing about the queued entry. In run 2, emit_delete(B,7001) returns True and the reply 'close all now' (7002, reply_to 7001) returns True. The banner shows only 'watching: First channel A'. After the run: messages(B)=[(7001,'signal','live',None)] (not marked deleted, 7002 never stored); signals=[(1,7001,'NEW',withdrawn=0)]; followups=[]; outbox=[(1,'ic_demo','ENTRY','QUEUED',None)]; alerts=[]; metrics=[]; channel_state B pointer=7001. outbox.claim(ic_demo) returns ('ENTRY', outbox 1, signal 1).

DEL-ON control, same events: messages 7001 deleted_at set, 7002 stored as followup; signals=[(1,7001,'VOIDED',1)]; ENTRY 'SKIPPED' with reason 'signal withdrawn: message deleted, found by event'; FOLLOWUP 2 QUEUED; alert WARNING SIGNAL_VOIDED_BY_DELETION. claim() returns ('FOLLOWUP', 2, None, followup 1).

EDIT-OFF, an extra variant: the poster edits 7001 from SL 4470 to SL 4460 (delivered=True). The signal stays at version 1 with SL 4470.0 and claim() returns the ENTRY with the stale SL. In EDIT-ON the signal becomes version 2 with SL 4460.0.

Code read confirms the cause. listener.py:398-400 builds _channels_by_id from enabled channels only. channel_cfg_for() gates all three handlers (lines 1103, 1126, 1151). poll.py:242 iterates listener.enabled_channels. outbox._DEAD_ENTRY (line 149) checks only signals.state and withdrawn_at_utc. The gap is new to the ledger and obligations. Ledger M12 covers only 'OFF replays on ON', and the M12 ruling only re-seeds at 'now'. PLAN3-OBLIGATIONS #1-#33 contain nothing about disabled channels; #29 covers only the window counting chatter. RUNBOOK.md (docs/RUNBOOK.md:163) says only 'stops taking signals ... Nothing about it is deleted'.

The finding is overstated on the entry side. config/policy.toml has max_signal_age_seconds = 120, and domain/gates.py:33-35 rejects older signals as STALE. Obligation #14 discards the watch-only backlog. A Plan 3 worker claims a QUEUED entry within seconds of it being queued. So 'a worker would open the withdrawn call' needs a deletion followed by a claim, both inside 120 s of posting, with an OFF-and-restart in between. That is practically unreachable.

The real residual is on the follow-up side, for live legs under Plan 3. After OFF, the channel's CLOSE_ALL, SL_TO_BE and SL-edit amendments are dropped silently, with no alert and no status line, and the position runs to its broker SL or TP. This is a semantics decision the user never saw. The pending M12 re-seed makes it permanent across OFF and ON; I reasoned this and did not build it. Plan 2 harm is zero because it is watch-only. Repo after the probes: git status --porcelain shows 0 lines, no .db files, config/secrets absent, and Telegram was never contacted.

---

## F16 [Minor] Several Plan 3 hand-offs live only in the ledger, which is deleted at Plan 2 finish; the audit's 'all obligations consolidated' claim is incomplete

- Reviewer area: G-crosscut
- Owner in the fix wave: **controller (docs)**
- Location: `C:\Users\Herbert\Projects\xaucopier\.superpowers\sdd\2026-09-10-xaucopier-ingest\progress.md:197`

### Scenario
CHECKED BY GREP (no runtime probe) against C:\Users\Herbert\Documents\xaucopier-plan1b\PLAN3-OBLIGATIONS.md and docs/. None of these has a durable home:
- Spec 6.2 PAUSE_NEW: no match for PAUSE_NEW, parse_fail or template. The file says it was compiled from spec 7-12, so 6.2 falls outside its range.
- Spec 12 idempotency_key = sha1(channel_id|message_id|leg_index|account_id) UNIQUE: no match for idempot or sha1. This is the constraint that turns a second execution into an IntegrityError.
- Ledger 161: I ran it (probe_g.py P4). Appending '\n❌ CANCELLED' by edit is treated as cosmetic: the signal stays NEW, version 1, and the ENTRY is claimable. Replies 'Signal cancelled ❌', 'Cancel this signal' and 'Cancel this trade' all route to skip; only 'cancel the order' becomes a follow-up.
- Ledger 166: a relay of an intermediate edited version becomes a second ENTRY.
- Ledger 145: client.connect() and get_dialogs() are unbounded at start, so a supervisor needs a startup deadline.
- Every VERIFY-ON-FIRST-LOGIN item: 138 (offset_date semantics), 174 (resolve-channels) and ruling 216 (is COMMUNITY a channel or a supergroup). 'VERIFY-ON-FIRST-LOGIN' appears nowhere outside the ledger.
The ledger's own rule is that Plan 3's brief is written against PLAN3-OBLIGATIONS, so these are lost once the ledger is deleted.

### Reviewer's proposed fix
Before deleting the ledger, add these items to PLAN3-OBLIGATIONS.md, and add a VERIFY-ON-FIRST-LOGIN checklist (in the obligations file or the runbook). Widen the file's stated source range to include spec 6. Ledger 161 should get a real owner, the parser or router, before Plan 3 executes anything.

### Verifier evidence
Checked by grep. PLAN3-OBLIGATIONS.md (78 lines, items 1-33) has 0 matches for each of: PAUSE_NEW, parse_fail, template, idempot, sha1, IntegrityError, VERIFY, offset_date, resolve-channels, supergroup, 'sender filter', get_dialogs, client.connect, deadline, synchronous, '6.2'. VERIFY-ON-FIRST-LOGIN appears only at progress.md lines 138, 174 and 216. `git check-ignore` shows .superpowers/ is gitignored (.gitignore:5), so the ledger is not in git.

The one real omission is §12 idempotency_key UNIQUE, which falls inside the file's own stated §7-§12 scope. The VERIFY-ON-FIRST-LOGIN checks also have no durable home.

Mitigations:
- PAUSE_NEW (§6.2) and idempotency_key (§12) live in the binding spec, which persists in docs/. They are not ledger-only as claimed.
- The Plan 1 ledger was preserved in docs/superpowers/records by commit 0b9b494 (the branch base), so preserving ledgers is the established practice.
- Ledger items 145, 161 and 166 are already recorded deferred minors. Re-probing 161 (CANCELLED) re-reports a tracked item.

This is a documentation hand-off gap with no runtime path. Downgraded.

---

## F17 [Minor] missed_by_eventstream also counts backlog posted while `watch` was down, not only what the update stream dropped

- Reviewer area: B-listener-poll
- Owner in the fix wave: **group 2 (ingest core)**
- Location: `src/xaucopier/ingest/poll.py:325`

### Scenario
REASONED, not run. Backfill reads at most DEFAULT_BACKFILL_LIMIT (500) messages per channel. The rest of a longer backlog is ingested by the poll with source='poll', and count_missed() counts every 'new' result. Two cases produce such a backlog: a channel switched back on with `channels on N` after weeks off, and a PC that was off for a long time on a busy channel. Every message past 500 is then logged as 'the update stream never delivered' and added to what the docstring calls 'the system's real reliability metric'. The stream could not have delivered posts made while the process was down. The late-seed path adds up to 60 s of pre-start posts in the same way. The module docstring names only the 'posted moments before a round' race as a source of over-counting.

### Reviewer's proposed fix
Count a poll 'new' as missed only if its posted_at_utc is at or after the listener's started_at (minus the seed margin). Alternatively, count backlog recoveries under a separate metric, so `status` can tell 'stream dropped it' from 'posted while stopped'.

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

## F19 [Minor] migrate() reads user_version outside its transaction, so two processes upgrading the same file race and one fails

- Reviewer area: A-store
- Owner in the fix wave: **group 1 (store/config)**
- Location: `src/xaucopier/store/migrations.py:243`

### Scenario
Ran (p3_migrations.py, case 6). Two connections open the same v1 file at once, and A's migration 2 is slowed by 1.5 s. Result: A ('ok', 3); B OperationalError 'duplicate column name: deleted_at_utc'. B read user_version=1 before its deferred BEGIN, waited for A's lock, then re-ran the ALTERs. The failure is loud and the next run heals it, but watch's stamp_start maps a non-'locked' sqlite3.Error to SetupError, which is exit 5 with 'the database cannot be used'. This will be reachable when Plan 3's worker and watch both open the file for the first time after migration 4.

### Reviewer's proposed fix
Use `BEGIN IMMEDIATE`, then re-read PRAGMA user_version inside the transaction and return early if it is already at CURRENT_VERSION.

---

## F20 [Minor] Writer self-commit detection only catches a trailing commit; commit-then-write jobs are neither detected nor atomic

- Reviewer area: A-store
- Owner in the fix wave: **group 1 (store/config)**
- Location: `src/xaucopier/store/db.py:354`

### Scenario
Ran (p4_writer.py, case 3). A job that writes, commits, then writes and raises resolves with its own ValueError, yet the first write is durable. A job that writes, commits and writes again resolves successfully. Durable rows: ['a','first','x1','x2']. WriterMisuseError's docstring presents detection as complete. No fn in src commits today (grep: the only commit/rollback outside db.py are in cli/ack.py and cli/watch.py on their own connections), so this is latent.

### Reviewer's proposed fix
Hand fn a thin connection proxy whose commit(), rollback(), isolation_level/autocommit setters and execute('BEGIN'/'COMMIT'/'ROLLBACK'/'SAVEPOINT'/'RELEASE') raise WriterMisuseError before anything runs. This would also make a claim() or sweep_expired() routed through the Writer fail on an EMPTY queue, which closes Plan 3 obligation 15's 'looks healthy in idle testing' trap at the source.

---

## F21 [Minor] A Writer job's reads before its first write run outside the job's transaction

- Reviewer area: A-store
- Owner in the fix wave: **group 1 (store/config)**
- Location: `src/xaucopier/store/db.py:350`

### Scenario
Ran (p5_misc.py, case a). A job reads outbox state 'QUEUED' (in_transaction=False). Another writer connection's UPDATE to 'CLAIMED' commits mid-job without blocking, and the job then commits a decision based on the stale read. This is safe today only by accident of ordering: apply_deletion and _reconcile_version each issue an UPDATE messages before trade_status() reads the outbox, so the write lock is already held. A future job that reads trade_status before its first write would decide 'pending'/VOIDED against a Plan 3 worker's concurrent claim.

### Reviewer's proposed fix
Have _run_job issue `conn.execute('BEGIN IMMEDIATE')` before fn (legacy mode will not add a second BEGIN), so every job reads and writes under the write lock. The cost is negligible because the Writer is the listener's only writer.

---

## F22 [Minor] connect() silently accepts a database from a newer schema version

- Reviewer area: A-store
- Owner in the fix wave: **group 1 (store/config)**
- Location: `src/xaucopier/store/migrations.py:245`

### Scenario
Ran (p5_misc.py, case b). With user_version set to 4, connect(role='writer') accepts the file and leaves user_version 4, while connect_readonly raises SchemaVersionError. If the user rolls back from a Plan 3 build to this one, watch writes into a schema it does not understand.

### Reviewer's proposed fix
Make migrate() (or connect()) raise SchemaVersionError when current > CURRENT_VERSION.

---

## F23 [Minor] `channels add`/`resolve-channels` show a raw Python traceback when the lookup hits a Telegram error that is not permanent

- Reviewer area: E-cli
- Owner in the fix wave: **group 5 (CLI)**
- Location: `src/xaucopier/cli/channels.py:434`

### Scenario
RAN IT (probe3 part B, fake client). A FloodWaitError of 300 s from get_dialogs (Telethon raises any flood wait over flood_sleep_threshold=120), RpcCallFailError (Telegram 500) from get_dialogs, and a generic BadRequestError from is_user_authorized each escaped `_add` → asyncio.run → cmd_add, which catches only KeyboardInterrupt/EOFError. The user sees a full traceback ending in 'telethon.errors.rpcerrorlist.FloodWaitError: A wait of 300 seconds is required'. No harm to data: the file is unchanged and the lock is released. `_resolve_all` catches only PERMANENT_TELEGRAM_ERRORS around the same `_choose()`/get_dialogs call, so it has the same gap.

### Reviewer's proposed fix
In `_add` and `_resolve_all`, catch `errors.RPCError` after the permanent-error branch and print one plain sentence. For a FloodWaitError say 'Telegram asks to wait about N minutes, then run it again'. Exit 1, 'Nothing was added/changed.'

---

## F24 [Minor] With no network, `setup-login`, `channels add` and `resolve-channels` never end. The 'no network → exit 1' contract cannot happen.

- Reviewer area: E-cli
- Owner in the fix wave: **group 5 (CLI) using group 2's bounded-connect helper**
- Location: `src/xaucopier/ingest/client.py:166`

### Scenario
RAN IT (probe6). The real client was pointed at 127.0.0.1:9 (closed; localhost only, Telegram never contacted). Both `setup-login` and `channels add` were still running when killed at 45 s. They printed 'Attempt N at connecting failed: ConnectionRefusedError: [WinError 1225] The remote computer refused the network connection' every 5–7 s (6 attempts). build_client sets connection_retries=None, and Telethon's retry_range(None) never ends. main.py's docstring promises 'no network (1) ... ends in one plain sentence', and channels.py:436's 'Could not reach Telegram ... Nothing was added' branch is unreachable for a failed connect. Meanwhile `channels add` holds the session mutex, so a `watch` started in another window exits 6 with a message naming only 'resolve-channels'. The root cause is shared with the ledger's deferred listener minor ('client.connect() ... in start() remain unbounded'), but the one-shot commands are not covered by it.

### Reviewer's proposed fix
For the one-shot commands, bound the connect: wrap `client.connect()`/`client.start()` in `asyncio.wait_for(..., 30)`, or build the client with a small connection_retries for these commands only. Map the timeout to the existing 'Could not reach Telegram' message and exit 1. Add 'channels add' to watch's exit-6 message.

---

## F25 [Minor] A file at the database path that is not a database crashes `status`, `ack` and `export-fixtures` with a traceback

- Reviewer area: E-cli
- Owner in the fix wave: **group 5 (CLI)**
- Location: `src/xaucopier/cli/status.py:423`

### Scenario
RAN IT (probe5). --db pointing at a 2.9 KB non-SQLite file: `status`, `ack`, `ack 1` and `export-fixtures` each raised an uncaught sqlite3.DatabaseError ('file is not a database'). `channels` handles it (read_totals catches sqlite3.Error), and so does `watch` (stamp_start → SetupError, exit 5).

### Reviewer's proposed fix
Catch sqlite3.DatabaseError around connect_readonly/collect in status, ack._list/_clear and fixtures, and say plainly that the file at <path> is not xaucopier's database. Exit 1.

---

## F26 [Minor] The runbook says every command prints an exit code. Only a `watch` that actually started does.

- Reviewer area: E-cli
- Owner in the fix wave: **group 5 (CLI)**
- Location: `docs/RUNBOOK.md:197`

### Scenario
Checked (probe4 via xaucopier.cmd, and PowerShell). 'When `watch` (or any command) stops, it prints an exit code'. In practice `channels add` (6), `resolve-channels` (6) and `watch`'s own lock refusal (6) printed no code, and PowerShell does not show $LASTEXITCODE. Only `watch stopped (exit code N: ...)` and the exit-5 setup message print one. The exit-6 row ('channels add and resolve-channels also stop with this code') and step 5's '(exit code 6)' point the user at a number they never see.

### Reviewer's proposed fix
Append '(exit code N)' to each refusal/failure message in the CLI (e.g. in main() after func returns), or reword the runbook to match each command's message rather than a code.

---

## F27 [Minor] 'Channel number' means two different things: resolve-channels calls the Telegram id 'channel -100…', while `channels` means the ordinal

- Reviewer area: E-cli
- Owner in the fix wave: **group 5 (CLI)**
- Location: `src/xaucopier/cli/resolve.py:591`

### Scenario
Read at HEAD. resolve-channels ends with "Saved: 'X' is now channel -1001234567890, switched on." RUNBOOK step 3 says 'It writes the channel's number into config\channels.toml'. The new section then says 'The number to type is the one `.\xaucopier channels` shows' (the ordinal, e.g. 1). A user who types `channels off -1001234567890` gets a harmless 'There is no channel number…'. The same mixed wording also appears in channels list lines ('id: … (looked up)').

### Reviewer's proposed fix
Say "Saved: 'X' -- its Telegram id -100… -- and switched it on. It is channel N in `.\xaucopier channels`." Reword RUNBOOK step 3 to 'writes the channel's Telegram id'.

---

## F28 [Minor] OFF is really 'paused': switching a channel back ON replays every post made while it was OFF as new signals and follow-ups

- Reviewer area: E-cli
- Owner in the fix wave: **group 2 (watch side) + group 5 (`channels on` side)**
- Location: `docs/RUNBOOK.md:163`

### Scenario
REASONED from code, not run. `channels off` only flips `enabled`, and channel_state.last_processed_message_id stays where it was. On the next `watch` with the channel ON, the backfill walks get_messages(min_id=pointer, reverse=True) (listener.py:320, up to DEFAULT_BACKFILL_LIMIT=500 per read), and the poll continues above the pointer. So everything posted during the OFF period is ingested with QUEUED ENTRY/FOLLOWUP outbox rows, and the poll's share is counted as 'missed by Telegram's live feed', a false 'dozens a day' signal per the runbook. The runbook says OFF 'stops taking signals from channel number 2'. Plan 3's age gate limits the entry risk.

### Reviewer's proposed fix
Choose and document one behaviour. Either (a) have `channels on` (or watch, for a channel re-enabled after being off) seed like a first run, i.e. anchor below start-60 s and skip the OFF-period history, or (b) state in the runbook that switching back on catches up on everything posted while off.

---

## F29 [Minor] setup-login shows jargon and an alarming Telethon line that the runbook does not mention

- Reviewer area: E-cli
- Owner in the fix wave: **group 5 (CLI)**
- Location: `src/xaucopier/cli/main.py:263`

### Scenario
RAN IT (probe1 S1/S3/S7/S8). A successful login prints Telethon's own 'Signed in successfully as Me; remember to not break the ToS or you will risk an account ban!' before 'Login succeeded'. In the layer-2 case (S4, unreachable today) Telethon prints 'Signed in successfully as ;' just before our 'Telegram did not log this program in'. setup-login's own text says 'the client permanently flood-blocked' and 'do not proceed to run the listener'. The user knows neither 'client' nor 'listener', only `watch`.

### Reviewer's proposed fix
Redirect stdout during client.start() (contextlib.redirect_stdout to a buffer) so only xaucopier's own sentences show. Replace 'client'/'flood-blocked'/'listener' with 'this program'/'blocked by Telegram'/'`.\xaucopier watch`'.

---

## F30 [Minor] A transient 'channels.toml was changed by something else — run the command again' exits 5, which the runbook says restarting cannot fix

- Reviewer area: E-cli
- Owner in the fix wave: **group 5 (CLI)**
- Location: `src/xaucopier/cli/channels.py:392`

### Scenario
Read at HEAD. Every save_switch/save_added failure, including the new unchanged-since-read race check and a transient os.replace PermissionError, returns EXIT_CONFIG_ERROR (5). The runbook row for 5 says 'Something in the setup is wrong … Restarting will not help until it is fixed', but running the command again is exactly the fix here.

### Reviewer's proposed fix
Return EXIT_FAILED (1) for the 'changed by something else' race and for OSError from os.replace. Keep 5 for a file the loader rejects.

---

## F31 [Minor] The Writer reports a full disk (or any rollback SQLite makes itself) as "fn called commit()/rollback() itself", and its docstring wrongly says the job's earlier writes are final

- Reviewer area: A-store
- Owner in the fix wave: **group 1 (store/config)**
- Location: `src/xaucopier/store/db.py:354`

### Scenario
I ran this (scratchpad/final/store2/p5_full.py). I capped the writer connection with PRAGMA max_page_count to simulate a nearly full disk, then submitted a job that inserts one small row and then one 200 KB row. The future resolved with WriterMisuseError('fn called commit()/rollback() itself...'), whose __cause__ is OperationalError 'database or disk is full'. The small row was NOT durable (rows=[]). In p2_writer.py W5, INSERT OR ROLLBACK gives the same result. The cause: SQLite rolled back the whole transaction on its own, so total_changes had moved while in_transaction was False, which the detector reads as self-management. The docstrings for WriterMisuseError (db.py:142-150) and _run_job (db.py:355-358) say this combination can only mean fn committed and that 'Whatever fn already did ... is final'. Both statements are false here. Impact is diagnostic only. Nothing outside db.py catches WriterMisuseError, so the listener treats it like any other job failure (retry, then skip). The real cause is still visible in the exc_info traceback chain. But the headline log line on a full disk blames a code bug that does not exist. migrate() is NOT affected: with disk full mid-migration-2, the transaction stayed open, ROLLBACK succeeded, and the real 'database or disk is full' was raised with the file intact at v1 (p6_migfull.py).

### Reviewer's proposed fix
In the misuse branch, when fn_exc is a sqlite3.Error, resolve the future with fn_exc itself (SQLite already rolled back, so nothing was committed) and keep WriterMisuseError for the case where fn returned normally or raised a non-sqlite error. Alternatively, reword both docstrings: an SQLite-initiated rollback (SQLITE_FULL/IOERR/NOMEM, OR ROLLBACK, RAISE(ROLLBACK)) produces the same signature and leaves nothing durable.

---

## F32 [Minor] `watch` crashes at its banner with a traceback (exit 1, before recording anything) when a channel name has an emoji and output is redirected. `channels` and `status` guard against exactly this; `watch`, `resolve-channels`, the pickers and `ack` do not.

- Reviewer area: E-cli
- Owner in the fix wave: **group 5 (CLI)**
- Location: `src/xaucopier/cli/watch.py:310`

### Scenario
RAN IT (p2_emoji.py). channels.toml holds one channel whose name was written by resolve.append_channel, the exact text `channels add` writes: '\U0001F947 GOLD VIP SIGNALS \U0001F4C8'. Children run the real CLI with stdout and stderr redirected to files; load_session and _run are stubbed so nothing connects. Python 3.14 chooses cp1252 for a redirected stdout. `watch` printed the first banner line, then died: `print(f"  watching: {channel.name} ...")` raised UnicodeEncodeError: 'charmap' codec can't encode character '\U0001f947'. Exit was 1, a full traceback went to stderr, and watch_stopped was never stamped. `status` on the same DB right afterwards printed 'STARTING: started ... (1 s ago); the first check has not finished yet.', then NOT HEALTHY after 2 minutes. `status` and `channels` with the same redirect exited 0 and printed '? GOLD VIP SIGNALS ?' through their guarded _write(). The runbook's interactive console is unaffected, because the console writes UTF-16. But `channels add` copies Telegram titles verbatim, and emoji are common in signal-channel titles. Any redirect breaks it: `> log.txt`, `| Tee-Object`, Task Scheduler, or Plan 3's supervisor. Exit 1 is not in RESTARTABLE, so a supervisor leaves watch dead. The same unguarded print() calls are in resolve.py:494/548/567/591 (picker and messages), watch.py:310, and ack.py:53 for alert details.

### Reviewer's proposed fix
Make the guard global rather than per module. At the top of main(), call `sys.stdout.reconfigure(errors="replace")` and do the same for `sys.stderr`, which covers every command at once. Or route watch's banner and resolve's prints through the existing channels._write. Add one test that runs `watch`'s startup with a cp1252 stdout and an emoji channel name.

---

## F33 [Minor] Two concurrent `channels` writes share one fixed temp name (.channels.toml.resolve-tmp), which defeats the 'unchanged since read' guard. Each command then reports the opposite of what happened.

- Reviewer area: E-cli
- Owner in the fix wave: **group 5 (CLI)**
- Location: `src/xaucopier/cli/resolve.py:299`

### Scenario
RAN IT (p3_race.py). Two real CLI subprocesses run on a temp channels.toml (1 ON, 2 ON, 3 OFF). File handshakes control only the timing. A = `channels on 3` passes every check and pauses just before os.replace. B = `channels off 1` runs `tmp.unlink(missing_ok=True)`, which deletes A's temp, then writes its own temp and pauses. A's os.replace then moves B's text over channels.toml. Output: A exited 0 with 'Channel 3 (Three) is now ON.'. B exited 5 with 'Could not save the change ... [Errno 2] No such file ... .channels.toml.resolve-tmp. The file was not changed: channel 1 is still ON.' The actual file: [(1,'One',False),(2,'Two',True),(3,'Three',False)]. So channel 3 is still OFF and channel 1 is now OFF: both statements are false. The file itself is always valid and no temp file is left. `channels on/off` take no mutex, and only `channels add` and `resolve-channels` do, so nothing serialises writers. The real-world window is microseconds and needs two commands at once, hence Minor.

### Reviewer's proposed fix
Give each writer a unique temp name, for example `path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")`. Then compare path.read_bytes() with the original once more just before os.replace (still a TOCTOU window, but much smaller), or take a short-lived file lock (msvcrt.locking on a sidecar lock file) around the read, check and replace sequence in write_config_atomically.

---

## F34 [Minor] setup-login: a failure after Telegram has accepted the login reports only 'Could not reach Telegram... Nothing was saved'. It never mentions the orphaned XAUCOPIER-TRADEBOX device now on the account, as the save-failure branch does.

- Reviewer area: E-cli
- Owner in the fix wave: **group 5 (CLI)**
- Location: `src/xaucopier/cli/main.py:272`

### Scenario
RAN IT (p5_login_drop.py). It drives the real Telethon 1.44 start(), sign_in() and _on_login() offline, with a simulated server as in run 1's harness. In S9 (2FA) CheckPassword succeeds, so the server-side session exists, and then Telethon's own post-login GetStateRequest inside _on_login fails. In S10 (no 2FA) SignIn succeeds and then the same happens. Both runs: exit 1, saved=False, server_authorized=True, and stderr says 'Could not reach Telegram (ConnectionError). Check the internet connection, then run `.\xaucopier setup-login` again. Nothing was saved.' The retry creates a second XAUCOPIER-TRADEBOX device. The first is orphaned: its key was discarded, so this is a leftover device, not a leak. Compare main.py:300 (save failed after login): 'IMPORTANT: a live XAUCOPIER-TRADEBOX session now exists ... terminate that session by hand'. Caveat: I simulated the failure with ConnectionError. With connection_retries=None, real Telethon would more likely retry a dropped connection forever (the ledger's M8). The reachable triggers are an RPC error from GetState or GetDifference, such as a 500 or a flood wait over 120 s. No unauthorised session is ever saved.

### Reviewer's proposed fix
Track whether a sign-in completed, for example by checking `client._authorized` (or a flag set in the code/password callbacks) in the except branch. When it did, append the same 'open Telegram -> Settings -> Devices and terminate the extra XAUCOPIER-TRADEBOX' sentence that the Ctrl+C and save-failure branches already print.

---

## F35 [Minor] secrets.load() lets PermissionError/TypeError escape despite its 'never a bare OSError' contract; the ACL itself hides load()'s own 'wrong Windows user' message

- Reviewer area: D-security
- Owner in the fix wave: **group 2 (ingest core)**
- Location: `src/xaucopier/ingest/secrets.py:187`

### Scenario
I ran this through cli_main.main(["watch", ...]) with build_client stubbed to refuse. I saved a DPAPI blob of fake values, then used icacls to deny the current user read. Result: an UNCAUGHT PermissionError and a 9-line traceback, exit 1, where it should be SetupError, exit 5, with a plain message. With the deny removed, the same file loads fine. `--secrets` pointing at a folder also gives an uncaught PermissionError (path.exists() is True for a directory). A DPAPI-valid payload that is a JSON array gives an uncaught TypeError. The realistic trigger is setup-login run in a PowerShell opened as a different Windows user (e.g. 'Run as administrator' with a separate admin account). DPAPI and icacls then both bind the file to that user, and watch, resolve-channels and channels add, run from the everyday account, cannot read it. load() never reaches its own 'tied to the Windows user account ... run setup-login again under this account' message, because the ACL pre-empts it. The failure is loud (exit 1, not restartable), not silent.

### Reviewer's proposed fix
In load(): use `path.is_file()`. Wrap read_bytes() in `except OSError as exc: raise SecretsError(... 'it may belong to another Windows user; run setup-login again under this account' ...) from exc`. Add TypeError to the malformed branch, or check that `data` is a dict with an int api_id and str fields.

---

## F36 [Minor] acquire_single_instance_lock() raises a raw pywintypes.error (5) when the mutex exists but cannot be opened, while session_in_use() maps the same case to 'in use'

- Reviewer area: D-security
- Owner in the fix wave: **group 2 (ingest core)**
- Location: `src/xaucopier/ingest/client.py:117`

### Scenario
I ran this with a probe-only mutex name, so the real Global\xaucopier_listener was never touched. A child process held the mutex with a DACL granting Everyone only SYNCHRONIZE. session_in_use() returned True, and cli_main.main(["watch", ...]) raised an uncaught pywintypes.error (5, 'CreateMutex', 'Access is denied.'): a 6-line traceback, exit 1. All three call sites (watch, resolve-channels, channels add) catch only AlreadyRunningError. It fails closed: the session is never opened. Reasoned but not run elevated: that DACL is the shape a `watch` started in an elevated PowerShell leaves for a normal window (High-integrity object, no-write-up), so a non-developer then sees a traceback instead of the exit-6 'already running' message.

### Reviewer's proposed fix
In acquire_single_instance_lock(), catch pywintypes.error with winerror == ERROR_ACCESS_DENIED and raise AlreadyRunningError, adding a hint that it may be running in an administrator window.

---

## F37 [Minor] resolve-channels calls client.connect() outside its try, so a permanent error raised by connect() is a raw traceback (exit 1), not report_permanent_error's exit 5 or 3

- Reviewer area: D-security
- Owner in the fix wave: **group 5 (CLI)**
- Location: `src/xaucopier/cli/resolve.py:542`

### Scenario
I ran it: cli_main.main(['resolve-channels', ..., '--link', '@x', '--yes']) with a fake client whose connect() raises. ConnectionApiIdInvalidError gave an UNCAUGHT 10-line traceback, exit 1. So did AuthKeyUnregisteredError. CONNECTION_API_ID_INVALID is an initConnection error, and Telethon's connect() awaits InitConnection, so connect() is where a bad api_id surfaces first. channels add has connect() inside its try (channels.py:426) and handles both. This is adjacent to run-1's M7, but M7's proposed fix (catch RPCError after the permanent branch) does not reach connect() while it stays outside the try.

### Reviewer's proposed fix
Move `await client.connect()` inside the `try:` in _resolve_all (the `finally: await client.disconnect()` is safe on a failed connect), as channels.py does.

---

## F38 [Minor] Nothing in the suite pins DPAPI user scope (dwFlags=0): a CRYPTPROTECT_LOCAL_MACHINE mutant passes every test

- Reviewer area: D-security
- Owner in the fix wave: **group 2 (ingest core)**
- Location: `tests/test_secrets.py:28`

### Scenario
I ran it. A scratch pytest plugin (-p) forced flags=0x4 (CRYPTPROTECT_LOCAL_MACHINE) into every save(). All 58 tests in test_secrets.py, test_client.py and test_cli.py passed, because a same-user round trip decrypts either way. No test mentions CryptProtectData, LOCAL_MACHINE or the flags. The code is correct at HEAD. A spy on CryptProtectData showed flags=0, and the saved blob's dwFlags field (offset 40) is 0x0, with its master key under the user's %APPDATA%\Microsoft\Protect. A LOCAL_MACHINE reference blob shows 0x4. Losing user scope would let any local account decrypt the whole Telegram account credential, and nothing would catch the regression.

### Reviewer's proposed fix
Add a test that spies win32crypt.CryptProtectData and asserts flags == 0, and/or parses the saved blob and asserts `struct.unpack_from('<I', raw, 40)[0] & 0x4 == 0`.

---

## F39 [Minor] signals.parsed_json.source always says 'live', even for signals the poll or backfill recovered. The premise of ruling 51, 'nothing consumes it', is false.

- Reviewer area: G-crosscut
- Owner in the fix wave: **group 1 (store/config)**
- Location: `C:\Users\Herbert\Projects\xaucopier\src\xaucopier\store\signals_repo.py:42`

### Scenario
RAN (probe_src.py): a signal ingested with source='poll' or 'backfill' stores messages.source as 'poll' or 'backfill', but json_extract(signals.parsed_json,'$.source') is 'live'. PLAN3 obligation 16 tells the worker to read parsed_json at claim time, and G6 makes provenance a logged field. So the one provenance value Plan 3 is told to read is wrong for every signal the poll recovered.

### Reviewer's proposed fix
Drop 'source' from signal_to_json, or write messages.source into it at ingest.

---

## F40 [Minor] load_channels accepts a duplicate channel_id; the listener silently keeps whichever enabled block comes last

- Reviewer area: G-crosscut
- Owner in the fix wave: **group 1 (store/config)**
- Location: `C:\Users\Herbert\Projects\xaucopier\src\xaucopier\config\channels.py:48`

### Scenario
RAN (probe_g.py P3): two enabled [[channels]] blocks with the same channel_id (ordinals 2 and 3) load without error. The listener's by-id map (listener.py:399) keeps ordinal 3, while `channels` lists both as ON. `channels on` refuses a twin, but a hand edit is not blocked by the loader. Ordinals feed magic numbers (PLAN3 obligations 11 and 33).

### Reviewer's proposed fix
Reject a duplicate channel_id in load_channels with a ConfigError that names both blocks.
