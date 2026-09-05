# Telegram → MT5 XAUUSD Signal Copier — Design

**Date:** 2026-09-05
**Status:** Awaiting review
**Codename:** `xaucopier`

---

## 1. Purpose

Read XAUUSD trading signals from a Telegram channel the user subscribes to, and
execute them automatically on MetaTrader 5 accounts with the stop loss and take
profits the signal specifies — including follow-up instructions that modify live
trades.

The system is a faithful executor. It does not judge signal quality, does not
filter by expected value, and does not add strategy of its own. Whether the
channel is worth following is an empirical question answered by the demo soak in
§13, not by this design.

## 2. Decisions already taken

These were settled with the user before design and are not open for
relitigation:

| # | Decision | Consequence |
|---|---|---|
| 1 | User is a **subscriber**, not a channel admin | Telethon MTProto **user session**. Bot API is not viable. |
| 2 | **1–2 channels**, fixed message template | Deterministic regex parsing. No LLM in the hot path. |
| 3 | **One demo account** for v1; multi-account plumbing built in | Fan-out exists from day one but ships with one entry enabled. |
| 4 | **Fixed lot size**, per position | No risk-based sizing. A wide-stop signal risks proportionally more. |
| 5 | **One position per TP**, sharing one SL | A 4-TP signal opens 4 positions. At 0.01 that is 0.04 total. |

Note on (4)+(5): the fixed lot is necessarily **per position**. MT5's minimum
volume is 0.01 with a 0.01 step, so a 0.01 "total" cannot be split four ways.

## 3. Verified environment

Confirmed on this machine on 2026-09-05, not assumed:

- **Python 3.14.7** at `C:\Users\Herbert\AppData\Local\Python\pythoncore-3.14-64`
- **`MetaTrader5 5.0.6180`** publishes `cp314-cp314-win_amd64` — no Python
  downgrade required. (One design agent claimed otherwise; it was wrong.)
- **MT5** at `C:\Program Files\MetaTrader 5`, data dir
  `%APPDATA%\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075`
- **One** terminal install, **one** data dir, five broker logins swapped by
  rewriting `common.ini`. See §11 — this is the single largest operational risk
  in the whole system.

Known symbol and spread landscape (measured 2026-08-26/28):

| Server | Symbol | Digits | Spread min / avg (USD) |
|---|---|---|---|
| ICMarketsKE-Demo | `XAUUSD` | 2 | 0.000 / 0.085 |
| FxPro-MT5 Demo | `GOLD` | 2 | 0.150 / 0.173 |
| ExnessKE-MT5Trial9 | `XAUUSDm` | **3** | 0.260 / 0.262 |
| FBS-Demo | `XAUUSD` | 2 | 0.230 / 0.302 |
| JustMarkets-Demo3 | `XAUUSD.m` | — | not measured |

## 4. Architecture

```
Telegram ──▶ listener.py ──▶ state.db ──▶ worker.py ──▶ MT5 terminal
             (parse only)    (queue +      (one per       (GOLD / XAUUSD /
              async          ticket map)    account)       XAUUSD.m / XAUUSDm)
              no MT5 import  SQLite WAL     blocking
```

Three process kinds, one durable store:

- **`supervisor`** — the only thing Task Scheduler launches. Spawns the listener
  and one worker per enabled account, restarts them with backoff, and gives up
  loudly after 5 restarts in 10 minutes. Owns process lifecycle only; never
  imports `MetaTrader5`, never makes a trading decision.
- **`listener`** — one asyncio process. Telethon in, rows out. Normalises text,
  runs the channel's regex, writes to SQLite. **Never imports `MetaTrader5`.**
- **`worker`** — one blocking, single-threaded process per MT5 terminal. The
  only place that imports `MetaTrader5` and builds request dicts.
- **`state.db`** — one SQLite file (WAL). Simultaneously the work queue, the
  signal→ticket map, and the audit log. Dedup and idempotency are enforced by
  `UNIQUE` constraints, not by remembering to check.

### 4.1 The pure core

Parsing, gates, and the "what orders should exist" planner are **pure functions**
with no I/O, no network, and no `MetaTrader5` import. A test walks the AST of
`domain/` and fails the build on a forbidden import.

This matters more here than in most projects: **the MT5 Strategy Tester cannot
test any of this.** Replaying recorded channel messages through the parser and
planner in CI is the only regression net available. Every message ever received
is stored verbatim and becomes a fixture.

## 5. Module layout

```
src/xaucopier/
  domain/            pure, no I/O
    money.py         the ONLY price/volume rounding in the codebase
    signal.py        Signal, EntryZone, Side, FollowUp
    intent.py        OrderIntent / ModifyIntent / CloseIntent / CancelIntent
    market.py        SymbolSpec, MarketSnapshot
    gates.py         the pre-trade predicates (§9)
    planner.py       Signal + Snapshot -> list[OrderIntent]
    followup_planner.py
  parsing/
    normalize.py     NFKC, strip emoji/zero-width, fold digits, comma->dot
    channel_mmh.py   one module per channel, keyed by numeric channel_id
    followups.py     the follow-up grammar
  store/
    schema.sql  db.py  repos  outbox.py
  ingest/
    session.py       DPAPI-wrapped StringSession, single-instance mutex
    listener.py  handlers.py  backfill.py  poll.py
  exec/
    protocol.py      Executor Protocol — keeps the EA option open
    mt5_session.py   initialize + preflight (§11)
    symbols.py       per-broker symbol map -> SymbolSpec
    executor.py      the only file importing MetaTrader5
    retcodes.py      the retcode classification table
    worker.py  reconcile.py  guards.py
  control/
    killswitch.py  daylimit.py  alerts.py  flatten.py
  supervisor/
    main.py  cli.py
  replay/
    harness.py       replay fixtures through parser+planner, golden-file diff
```

## 6. The signal contract

### 6.1 Observed format

From a real message (2026-09-05), channel *MARKET MASTERY HUB / COMMUNITY*,
forwarded from *MARKET MASTERY HUB VIP*:

```
🚀 XAUUSD | BUY SIGNAL 🟢
📍 4480-4485
🔴 SL: 4470
🎯 TP1: 4490
🎯 TP2: 4495
🎯 TP3: 4500
🎯 TP4 :4505
⚠️ Manage your risk. Trade with discipline.
```

The channel title and the "Forwarded from" line are Telegram UI chrome. The
parser receives only the body; forward origin arrives separately as
`message.fwd_from`.

### 6.2 Parsing rules

After `normalize.py` (NFKC, strip emoji and zero-width, fold full-width digits,
decimal comma → dot, collapse whitespace):

| Field | Pattern | Notes |
|---|---|---|
| Symbol + side | `(XAUUSD\|GOLD\|XAU)\s*\|\s*(BUY\|SELL)\s+SIGNAL` | Alias whitelist; anything else (US30, BTC) is **skipped**, never mapped onto gold |
| Entry zone | a bare `NUM[-–—]NUM` on its own line | Hyphen, en-dash and em-dash all accepted |
| SL | `SL\s*:?\s*(NUM)` | |
| TPs | `TP\s*(\d+)\s*:?\s*(NUM)`, all matches | **Whitespace-tolerant on both sides of the colon** — the real sample contains `TP4 :4505` |

`NUM` is `[0-9]+(?:\.[0-9]+)?`.

**A partial match is a hard reject.** The regex must match the full expected
structure (side + symbol + entry + SL + at least one TP) or the message is
rejected and the channel's `consecutive_parse_failures` counter increments.
Three consecutive failures auto-trip `PAUSE_NEW` — a template change becomes a
loud outage, never a wrong trade. There is no best-effort partial signal.

### 6.3 Direction is derived, then cross-checked

The side is derived **independently** from the level layout (all TPs on one side
of the zone, SL on the other) and cross-checked against the `BUY`/`SELL`
keyword. Disagreement is a hard reject with an alert — never a coin flip. This
catches a poster who writes "BUY" above sell levels, which happens.

## 7. Order construction

### 7.1 Entry: limit at the favourable edge

For the sample signal, where you fill inside the zone changes the trade
materially:

| Fill | Risk | TP1 | TP2 | TP3 | TP4 |
|---|---|---|---|---|---|
| 4480 (favourable edge) | 10 | 1.0R | 1.5R | 2.0R | 2.5R |
| 4485 (unfavourable edge) | 15 | **0.33R** | 0.67R | 1.0R | 1.33R |

Rule, for a BUY (mirrored for SELL):

- `Ask < zone_lo` → **market** (price is already better than the zone)
- `zone_lo ≤ Ask ≤ zone_hi` → **market** at Ask
- `Ask > zone_hi` → **BUY_LIMIT at `zone_lo`**, expiring at
  `posted_at + pending_max_age_hours`

Placing the pending at the *favourable* edge rather than the near edge is a
deliberate choice: it trades fill rate for R. It is a config flag
(`pending_at: favourable | near`) so the soak can measure the difference.

### 7.2 Legs

One `OrderIntent` per surviving TP, each at `fixed_lot`, all sharing the signal's
SL. A TP already behind price is dropped as a leg; if no leg survives, the whole
signal is rejected (`ALL_TPS_BEHIND`).

- `magic` = an **explicit, operator-assigned** integer per (channel, account)
  declared in config — **never derived from list order in a YAML file.**
  Reordering the config must not orphan live positions.
- `comment` = `base36(signal_id) + "-" + leg_index`, ≤ 20 chars. The leg index is
  mandatory: without it the legs are indistinguishable at the broker.

### 7.3 Never widen an SL to satisfy stops level

If the signal's SL is inside `min_dist = max(stops_level × point, 3 × spread)`,
the signal is **rejected** (`STOPS_TOO_TIGHT`). It is never clamped outward.
Under fixed-lot sizing a wider stop is a strictly larger dollar loss with no
volume compensation. TPs may be clamped outward; every clamp is logged with its
delta.

### 7.4 Never leave a naked position

On `10016 INVALID_STOPS` the fallback is: send without SL/TP, then immediately
`TRADE_ACTION_SLTP`. If that modify fails twice within 10 seconds, **close the
position** and mark the leg `FAILED`. A live gold position with `sl == 0` is a
top-priority reconciler condition, enforced by the 200 ms worker loop, not the
30 s reconciler.

## 8. Follow-ups

Supported ops: `SL_TO_BE`, `CLOSE_HALF`, `CLOSE_ALL`, `CANCEL_PENDING`,
`AMEND_SL`, `AMEND_TP`.

### 8.1 Target resolution

Ordered chain, no guessing:

1. `reply_to_msg_id` → the signal it replies to. Authoritative.
2. An explicit price or TP number in the text, matched against live signals.
3. Exactly **one** live signal for that channel+symbol.
4. Otherwise → **no action** + operator alert with the candidate list.

Constraints on the chain:

- Never resolve to a signal whose `message_id` is **greater** than the
  follow-up's.
- If the temporally nearest unresolved signal is `SKIPPED`/`NEVER_EXECUTED`, the
  answer is **no-op + alert** — never fall through to a different signal. (A
  rejected entry does not remove its follow-ups from the stream.)
- A **backfilled** destructive follow-up resolved by anything weaker than rule
  (1) is queued for operator approval, not auto-executed.

### 8.2 Semantics, committed here

- **`SL_TO_BE`** — `price_open ± (spread + commission_in_price + be_buffer)`, per
  leg, using **that leg's actual fill**, never the signal's quoted entry (each
  broker filled somewhere else). If the resulting price is on the wrong side of
  the live tick, emit nothing.
- **`CLOSE_HALF`** — closes `ceil(n_open_legs / 2)` **whole legs**, furthest TPs
  first. "Half of each leg" is not implementable at 0.01 minimum volume and is
  never attempted. The leg list is **captured and persisted when the command is
  first planned**, never recomputed from the live count — otherwise a crash
  mid-execution compounds on retry.
- **`CLOSE_ALL`** — scoped by magic to that signal's own legs. Never touches a
  manual trade.
- **`AMEND_SL` / `AMEND_TP`** — same side/geometry validation as any entry.
  Direction, symbol and entry may **not** be amended; such an edit quarantines
  the signal and alerts.

### 8.3 Edited and deleted messages

- An edit producing a material diff (SL/TP/entry/side) on an **unexecuted**
  signal replaces the parse; on an **executed** signal it enqueues an
  `AMEND_SL`/`AMEND_TP` follow-up. Cosmetic edits are logged and dropped.
- `ingest()` must have **three** outcomes, not two: new / unchanged / changed.
  `INSERT OR IGNORE` alone silently swallows every correction.
- Deletions are handled both via `events.MessageDeleted` and by treating "a
  message id in my recent window that history no longer returns" as a deletion.
  Policy: no live legs → `VOIDED`; live legs → flag and alert, do **not**
  auto-close (a delete may be cosmetic).

## 9. Safety guards

Each of these was raised by an adversarial critic against every candidate
design. They are requirements, not nice-to-haves.

| # | Guard |
|---|---|
| G1 | **Account identity.** Preflight hard-fails unless `account_info().login`, `.server` match config **and** `.trade_mode == DEMO` while `allow_live: false`. Re-asserted on every reconnect and every reconcile pass. See §11. |
| G2 | **Re-gate before every leg and every retry.** A retry is a re-plan against a fresh tick, never a resend of a frozen request dict. A leg failing the re-gate is `SKIPPED_ON_LEG`; the signal is `PARTIAL_SET`. |
| G3 | **Never fight the human.** Self-heal an SL only when it is `0` (broker dropped it) or equals a value this system wrote. Any other divergence is `HUMAN_OVERRIDE`: adopt the observed value, alert once, never write to that ticket again. Add a reconcile direction for **volume** mismatch (manual partial close). |
| G4 | **External dead-man's switch.** A heartbeat goes *outward* on a fixed cadence carrying open-leg count, equity and kill level. Something off-box alerts on its absence. Silence must be the alarm, not the all-clear. |
| G5 | **`flatten` is a standalone entrypoint.** `python -m xaucopier.control.flatten --account X`, whose only preflight is initialize + connected + identity. A latched HALT must not block it. The states that make you want it are the states where the worker is dead. |
| G6 | **One age gate, on the message's own timestamp.** Provenance (live vs backfill) is a logged field, never a gate. Gating backfill at 0–20 s makes the safety net catch nothing while logging that it works. |
| G7 | **Content-hash dedup across channels**, over a rolling window, applied before outbox fan-out. `UNIQUE(channel_id, message_id)` cannot express "the other channel relayed the same call". |
| G8 | **Broker server time ≠ UTC.** Derive `server_time_offset` at worker startup and route every history/position time comparison through explicit converters. |
| G9 | **Terminal warmup.** A freshly reconnected terminal returns an *empty* — not `None` — position list for seconds. Never let an empty result drive a `CLOSED` transition; require a real closing deal from `history_deals_get`. |
| G10 | **Suspend/resume detection.** Track monotonic alongside wall clock. A wall jump ≫ loop period is a RESUME: reset staleness epochs, force reconnect + re-preflight + full reconcile, suppress auto-trips for a grace window. |
| G11 | **Leg identity.** Bind to `result.order` (on hedging, position ticket == opening order ticket), never by scanning `positions_get()`. `UNIQUE(account_id, position_ticket)`. |
| G12 | **Kill switch re-read before every send**, not once per signal. `FLATTEN` re-sweeps until two consecutive passes find zero positions with our magic. |
| G13 | **No SQLite on the asyncio loop.** All listener DB writes go through a dedicated writer thread. A 5 s lock wait on the loop thread stalls update reception — the exact cause of missed signals. |
| G14 | **Write-ahead INTENT.** The `legs` row is committed and fsynced *before* `order_send`. A crash mid-send is recoverable rather than a mystery. |
| G15 | **Executor supervision, both directions.** Each worker heartbeats; the listener alerts when an account's heartbeat goes stale. "Executor dead, listener alive" is the dangerous asymmetry — positions live, follow-ups unreachable. |

## 10. Telegram ingestion

- **Credential.** `api_id`/`api_hash` from my.telegram.org (one pair per phone
  number; sample IDs trigger `API_ID_PUBLISHED_FLOOD`). Stored with the session
  in a DPAPI-wrapped blob, user-scoped, NTFS-ACL'd, never in a cloud-synced path.
- **The session is a bearer credential** equal to the whole Telegram account — it
  bypasses both the login code and 2FA. Never copied between machines: the same
  auth key used from two IPs is permanently invalidated
  (`AuthKeyDuplicatedError`). A named mutex prevents a second process (including
  an ad-hoc debug script) from opening it.
- **Login is interactive exactly once**, in a console, before the scheduled task
  is ever enabled. `device_model` is set to something recognisable so the session
  is not tidied away from the user's phone.
- **The event stream is a hint, not a queue.** Telethon has documented,
  unresolved 1–2 minute channel blackouts (issue #4361). Startup calls
  `get_dialogs()` to prime per-channel `pts`, then a **history poll every 5 s**
  from `last_processed_message_id` is the source of truth. `catch_up` is enabled
  as a bonus and depended on for nothing. The count of messages the poll found
  that the event stream never delivered is logged as the system's real
  reliability metric.
- `AuthKeyUnregisteredError` / `SessionRevokedError` are **terminal** — alert
  out-of-band and exit; retrying can never succeed.

## 11. MT5 terminals and the shared-login hazard

**This is the highest-risk item in the design, because of how this machine is
currently used.** Today five broker logins share one terminal and one data dir,
swapped by rewriting `common.ini`. `mt5.initialize(path=...)` with no credentials
attaches to whatever account that terminal last had, and every conventional
preflight check passes on the wrong account.

Required:

1. Pass `login`/`password`/`server` explicitly to `initialize()`.
2. Hard-assert `account_info().login`, `.server`, `.trade_mode` (G1).
3. `allow_live: false` per account for v1, so a live account is refused outright.
4. Persist `account_login` on every `legs` row; refuse to reconcile rows from a
   different login.
5. For v1, a **dedicated portable terminal copy** for the demo account, separate
   from the terminal the user drives by hand. This removes the hazard rather
   than guarding it.

Preflight must also assert: `connected`, `trade_allowed`, **not**
`tradeapi_disabled` (the Python-only kill switch that is easy to miss),
`trade_expert`, and `margin_mode == RETAIL_HEDGING` — multi-TP is meaningless on
a netting account, so refuse rather than degrade silently. Preflight retries with
backoff for a bounded warmup, because "terminal not connected yet" at boot is
expected, not an error.

## 12. State, restart and reconciliation

One SQLite file, WAL, `synchronous=FULL` on the worker's connection.
`idempotency_key = sha1(channel_id|message_id|leg_index|account_id)` with a
`UNIQUE` constraint — a second execution attempt raises `IntegrityError` instead
of opening a second position.

Reconciliation runs at worker startup and every 30 s, with five directions:

| Direction | Action |
|---|---|
| DB open, terminal absent | Confirm via a real closing deal; write realised P/L; `CLOSED`. Absent-with-no-deal is `UNVERIFIED`, retried — never a direct close (G9). |
| Terminal has our magic, DB doesn't | Adopt via comment leg-index + open time + volume; on no match `QUARANTINE`: alert, attach a protective SL, never silently close. |
| Terminal position without our magic | The user's own trade. Never touched, never counted. |
| Stuck `INTENT` / claimed with no counterpart | Bounded search by magic and time window, then `ABANDONED`. **Entries** are never resent; **closes/cancels** retry until the broker-side target is verifiably gone. |
| Volume changed underneath us | Manual partial close. Adopt the new volume, mark `MANUALLY_MANAGED`, alert. |

Dispatch claims carry a **lease** (`claimed_at`, reclaim after 90 s), so a
process killed between claim and first INTENT write cannot silently drop a
signal.

## 13. Testing and rollout

1. **Offline replay** — every recorded message replays through
   normalize → parse → plan in CI against a golden-file of expected decisions. A
   change that would have altered a historical decision shows up as a diff.
2. **Paper mode** — the full path with `order_send` replaced by a `FakeExecutor`.
   Run against live traffic; assert the decisions look right.
3. **Fault injection** — kill the worker mid-multi-leg send three times; confirm
   the reconciler adopts rather than duplicates. Toggle Algo Trading off mid-run.
   Sleep the machine with positions open.
4. **Demo soak, 2–4 weeks**, one account, full raw-message capture plus
   per-signal latency, spread-at-fill and realised slippage.

At the end of the soak, compare realised P/L against the channel's own claimed
results. **The gap between those two numbers is the actual decision input** for
whether this is ever worth running with real money — not whether the code is
finished.

## 14. Build phases

| Phase | Content | Est. |
|---|---|---|
| 0 | Repo, venv, config schema, secrets/DPAPI, one-time Telegram login runbook | 1 d |
| 1 | Pure core: domain, parser against real fixtures, gates, planner, follow-up planner, full pytest | 4–5 d |
| 2 | Store: schema, repos, outbox with leased claims, crash-mid-write tests | 2 d |
| 3 | Ingest: listener, 5 s poll, backfill, edit/delete handling, writer thread | 3 d |
| 4 | Execution: session + preflight (§11), symbols, retcode table, executor, worker | 5 d |
| 5 | Reconciliation (5 directions), follow-up ops, kill switch, day limit, alerts, external heartbeat | 4–5 d |
| 6 | Supervisor, Task Scheduler, replay harness, runbook | 2 d |

≈ **21–23 working days** to soak-ready. Adding brokers 2–5 afterwards is ~half a
day each — portable install, config block, paper week — with no code change.

## 15. Out of scope for v1

- Image/OCR signals (the current channel is text-only)
- Risk-based position sizing
- Any MQL5 EA. The `Executor` Protocol keeps that door open — a
  `FileBridgeExecutor` plus an EA slots in underneath without touching anything
  above the line — but it is not built.
- A VPS deployment
- An in-terminal equity-stop EA for the case where the Python side dies entirely.
  This is a real residual gap (hard power loss leaves positions unmanaged until
  the box returns) and is the most likely v2 item.

## 16. Open questions for the user

1. **Where should the project live?** The current working directory
   (`Downloads\Releases-26.X-release2`) is an unrelated download folder.
2. **Which demo account** for v1? ICMarkets has the tightest spread (0.085 avg)
   which is the friendliest to a 5 USD TP1; FxPro `GOLD` has the most local
   history.
3. **Sample follow-up messages** — none captured yet. §8's grammar is designed
   from the ops themselves, not from observed text, and must be rebuilt against
   real messages before it is trusted.
4. **`pending_at`** — favourable edge (better R, lower fill rate) or near edge?
   Default is favourable; the soak measures it.
