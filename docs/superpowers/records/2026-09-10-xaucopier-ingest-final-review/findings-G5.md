# Findings for fix-wave G5 only (extracted from findings.md)


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

## F37 [Minor] resolve-channels calls client.connect() outside its try, so a permanent error raised by connect() is a raw traceback (exit 1), not report_permanent_error's exit 5 or 3

- Reviewer area: D-security
- Owner in the fix wave: **group 5 (CLI)**
- Location: `src/xaucopier/cli/resolve.py:542`

### Scenario
I ran it: cli_main.main(['resolve-channels', ..., '--link', '@x', '--yes']) with a fake client whose connect() raises. ConnectionApiIdInvalidError gave an UNCAUGHT 10-line traceback, exit 1. So did AuthKeyUnregisteredError. CONNECTION_API_ID_INVALID is an initConnection error, and Telethon's connect() awaits InitConnection, so connect() is where a bad api_id surfaces first. channels add has connect() inside its try (channels.py:426) and handles both. This is adjacent to run-1's M7, but M7's proposed fix (catch RPCError after the permanent branch) does not reach connect() while it stays outside the try.

### Reviewer's proposed fix
Move `await client.connect()` inside the `try:` in _resolve_all (the `finally: await client.disconnect()` is safe on a failed connect), as channels.py does.