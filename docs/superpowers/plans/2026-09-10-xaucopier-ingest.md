# xaucopier Ingest Implementation Plan (Plan 2 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read the user's Telegram signal channel live, parse every message through the Plan 1 pure core, and persist the decisions to SQLite — placing no orders. The deliverable is a **watch-only signal recorder** the user can run for days to build the fixture corpus and prove the parser against real traffic.

**Architecture:** One asyncio listener process. Telethon in, rows out. It never imports `MetaTrader5`. All SQLite work happens on a dedicated writer thread so a lock wait can never stall Telegram update reception. The event stream is treated as a low-latency *hint*; a 5-second history poll is the source of truth. SQLite is simultaneously the queue, the audit log, and (in Plan 3) the signal→ticket map.

**Tech Stack:** Python 3.14.7, `telethon`, `pywin32` (DPAPI), stdlib `sqlite3`, pytest. No ORM — plain SQL in `.sql` files.

**Spec:** `docs/superpowers/specs/2026-09-05-telegram-mt5-xauusd-copier-design.md` — §6, §10, §12 are the binding sections.

**Predecessor:** `docs/superpowers/plans/2026-09-05-xaucopier-pure-core.md` (complete, merged). Its ledger of 22 rulings is at `docs/superpowers/records/2026-09-05-xaucopier-pure-core-ledger.md`.

## A deliberate departure from Plan 1's format

Plan 1 transcribed complete implementations into the plan document. Reviewers then found **ten real defects in that plan-authored code**, including a gate that accepted an inverted-risk trade and a comma rule that corrupted prices by 1000x.

This plan therefore specifies **schemas, interfaces, invariants and test code exactly**, and describes implementation bodies rather than transcribing them. Where a behaviour is subtle, the test is given in full — the test is the specification. Implementers are expected to write the body and are explicitly told that plan text is not above suspicion.

## Global Constraints

- **Python 3.14.7**, 64-bit. Project root `C:\Users\Herbert\Projects\xaucopier`, branch off `master`.
- **`src/xaucopier/domain/` and `src/xaucopier/parsing/` remain pure.** `tests/test_purity.py` enforces no I/O imports, no `open()`, no clock, no randomness. Nothing in this plan may relax it. New I/O code lives in `ingest/`, `store/`, `config/`, `cli/`.
- **`ingest/` never imports `MetaTrader5`.** Add this to the purity test as a separate, narrower assertion.
- **All datetimes are timezone-aware UTC.** Telethon returns aware datetimes; keep them.
- **All thresholds are USD floats.** No identifier ending in `_points` may appear in config — `config/loader.py` already enforces this at any nesting depth.
- **No SQLite call on the asyncio event loop thread.** Enforced by Task 4's design and asserted by a test.
- **Secrets never enter the repo, a log line, or a report.** `.gitignore` already covers `config/secrets*` and `*.session`.
- Every commit message ends with:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- Commit with `git -c user.name="Herbert" -c user.email="laftexharry770@gmail.com" commit -m "..."`

---

## File Structure

```
src/xaucopier/
  config/
    loader.py            (exists) + channels.py, accounts.py    Task 1
  parsing/
    registry.py          channel_id -> parser                   Task 1
    router.py            signal-first arbitration                Task 2
  store/
    schema.sql           the whole schema, versioned             Task 3
    db.py                connection factory, WAL, writer thread  Task 4
    messages_repo.py     ingest(), dedup                         Task 5
    signals_repo.py      signal + followup rows                  Task 5
    outbox.py            leased claims                           Task 6
  ingest/
    secrets.py           DPAPI-wrapped StringSession             Task 7
    client.py            TelegramClient factory, mutex           Task 7
    listener.py          handlers, startup order                 Task 8
    poll.py              5s history poll, backfill               Task 9
    edits.py             edit + delete handling                  Task 10
  cli/
    main.py              setup-login, resolve-channels, watch, status   Tasks 7, 11
tests/
  test_registry.py  test_router.py  test_schema.py  test_db.py
  test_messages_repo.py  test_outbox.py  test_secrets.py
  test_listener.py  test_poll.py  test_edits.py  test_cli.py
  fakes/telegram.py      a fake Telethon client                  Task 8
```

`ingest/` is tested against a **fake Telegram client**, not the network. Only the one-time login and `resolve-channels` touch the real API, and those are manual CLI steps in the runbook.

---

### Task 1: Config for channels and accounts, and the parser registry

**Files:** create `src/xaucopier/config/channels.py`, `src/xaucopier/config/accounts.py`, `src/xaucopier/parsing/registry.py`, `config/channels.toml`, `config/accounts.toml`; test `tests/test_registry.py`, `tests/test_config_entities.py`

**Interfaces produced:**
- `ChannelCfg(channel_id: int, name: str, parser: str, ordinal: int, enabled: bool)` — frozen
- `AccountCfg(account_id: str, broker: str, terminal_path: str, login: int, server: str, symbol: str, fixed_lot: float, magic_base: int, ordinal: int, allow_live: bool, enabled: bool)` — frozen
- `load_channels(path) -> tuple[ChannelCfg, ...]`, `load_accounts(path) -> tuple[AccountCfg, ...]`
- `registry.parser_for(name: str) -> Callable[..., ParseResult]`

**Invariants to enforce at load, each with a test:**
- `channel_id` is an **integer**, never a username. A username can be re-pointed at a different channel; an id cannot.
- `ordinal` is **explicit and operator-assigned** on both channels and accounts, unique within its file. It is never derived from list order — spec §7.2 and the Plan 1 review both flag that reordering a config file would otherwise silently reassign the magic numbers identifying live positions.
- `magic_base` is unique across accounts.
- An unknown `parser` name fails at load, not at first message.
- `allow_live` defaults to **`false`** and a `true` value with `trade_mode` unset is a load error. (Plan 3 consumes this; the guard belongs where the config is defined.)

**Steps:** write the tests first (they are the specification), then the loaders, then `config/*.toml` with the real channel placeholder and the ICMarkets demo account. Commit.

---

### Task 2: Router — signal-first arbitration

**Files:** create `src/xaucopier/parsing/router.py`; test `tests/test_router.py`

This closes the hazard the Plan 1 final review named: an entry signal whose template breaks can be read by the follow-up parser as an instruction against a live trade.

**Interface:** `route(text, *, channel_cfg, message_id, posted_at_utc, reply_to_message_id) -> RouteResult`
where `RouteResult` is a frozen union-ish record: `kind: Literal["signal","followup","skip","parse_failed"]`, plus `signal: Signal | None`, `followup: FollowUp | None`, `detail: str`.

**The arbitration rule, in order:**
1. Run the channel's signal parser. `ok` → `kind="signal"`. Done.
2. If it returned **`PARSE_FAILED`** (the header matched but the structure broke), return `kind="parse_failed"`. **Do not fall through to the follow-up parser** — this is the exact case that produced a spurious `AMEND_SL`.
3. If it returned **`NOT_A_SIGNAL`** (no header, or a non-gold symbol), try `parse_followup`. A hit → `kind="followup"`; otherwise `kind="skip"`.

**Tests (write these verbatim; they encode the hazard):**

```python
def test_valid_signal_routes_to_signal(): ...
def test_broken_entry_signal_never_becomes_a_followup():
    # header matched, structure broken -> parse_failed, and NOT an AMEND_SL
    text = "XAUUSD | BUY SIGNAL\n4480-4485\nTP1: 4490"   # no SL
    r = route(text, ...)
    assert r.kind == "parse_failed"
    assert r.followup is None

def test_signal_missing_the_word_signal_does_not_become_an_amend():
    text = "XAUUSD | BUY\n4480-4485\nSL: 4470\nTP1: 4490\nTP2: 4495"
    r = route(text, ...)
    assert r.kind in ("skip", "parse_failed")
    assert r.followup is None

def test_real_followup_routes_to_followup(): # "close all" -> kind == "followup"
def test_amend_tp_still_routes_to_followup(): # "TP2 to 4550"
def test_chatter_routes_to_skip():            # "Good morning traders"
def test_us30_signal_routes_to_skip():        # non-gold, never mapped onto gold
```

`skip` and `parse_failed` are **different**: only `parse_failed` feeds the consecutive-failure counter that trips `PAUSE_NEW` (spec §6.2). Assert that distinction.

---

### Task 3: The schema

**Files:** create `src/xaucopier/store/schema.sql`, `src/xaucopier/store/migrations.py`; test `tests/test_schema.py`

Tables for this plan. Plan 3 adds `legs`, `fills`, `account_state`, `alerts` — leave room, do not create them now.

```sql
CREATE TABLE messages (
  channel_id      INTEGER NOT NULL,
  message_id      INTEGER NOT NULL,
  kind            TEXT    NOT NULL,   -- signal | followup | skip | parse_failed
  content_hash    TEXT,               -- trade identity; NULL for non-signals
  raw_text        TEXT    NOT NULL,   -- verbatim, always, even for rejects
  edit_date_utc   TEXT,
  received_at_utc TEXT    NOT NULL,
  posted_at_utc   TEXT    NOT NULL,
  source          TEXT    NOT NULL,   -- live | backfill | poll
  PRIMARY KEY (channel_id, message_id)
);
CREATE TABLE signals (
  signal_id   INTEGER PRIMARY KEY,
  channel_id  INTEGER NOT NULL,
  message_id  INTEGER NOT NULL,
  parsed_json TEXT    NOT NULL,
  trade_hash  TEXT    NOT NULL,
  state       TEXT    NOT NULL,   -- NEW | SUPERSEDED | VOIDED | INVALID
  version     INTEGER NOT NULL DEFAULT 1,
  UNIQUE (channel_id, message_id),
  FOREIGN KEY (channel_id, message_id) REFERENCES messages(channel_id, message_id)
);
CREATE INDEX idx_signals_trade_hash ON signals(trade_hash);
CREATE TABLE followups (
  followup_id         INTEGER PRIMARY KEY,
  channel_id          INTEGER NOT NULL,
  message_id          INTEGER NOT NULL,
  op                  TEXT    NOT NULL,
  parsed_json         TEXT    NOT NULL,
  reply_to_message_id INTEGER,
  target_signal_id    INTEGER,
  resolution_path     TEXT,
  UNIQUE (channel_id, message_id)
);
CREATE TABLE outbox (
  outbox_id     INTEGER PRIMARY KEY,
  account_id    TEXT    NOT NULL,
  kind          TEXT    NOT NULL,   -- ENTRY | FOLLOWUP
  signal_id     INTEGER,
  followup_id   INTEGER,
  state         TEXT    NOT NULL,   -- QUEUED | CLAIMED | DONE | SKIPPED
  reason        TEXT,
  claimed_at_utc TEXT,
  claimed_by_pid INTEGER,
  attempts      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_outbox_claim ON outbox(account_id, state, outbox_id);
CREATE TABLE channel_state (
  channel_id                INTEGER PRIMARY KEY,
  last_processed_message_id INTEGER NOT NULL DEFAULT 0,
  consecutive_parse_failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE metrics (
  day_key TEXT NOT NULL, name TEXT NOT NULL, value INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day_key, name)
);
```

**Tests:** the schema applies to a fresh file; `UNIQUE(channel_id, message_id)` actually rejects a duplicate; `migrations.py` is idempotent (applying twice is a no-op); `user_version` is set and readable.

---

### Task 4: Connection factory and the writer thread

**Files:** create `src/xaucopier/store/db.py`; test `tests/test_db.py`

**Why a writer thread:** Python's `sqlite3` is synchronous. A commit on the asyncio loop thread blocks it for the duration of the lock wait — and a stalled event loop is a documented cause of *dropped Telegram channel updates*, which is a missed trade. Every write from the listener goes through a queue to one dedicated thread.

**Interface:**
- `connect(path, *, role: Literal["writer","reader"]) -> sqlite3.Connection` — WAL, `foreign_keys=ON`, `busy_timeout=10000`; `synchronous=FULL` for `writer`, `NORMAL` for `reader`.
- `class Writer` — `start()`, `stop(timeout)`, and `submit(fn) -> concurrent.futures.Future`. `fn` takes the connection and runs on the writer thread. `await asyncio.wrap_future(...)` is how the listener awaits a write.
- The writer owns the only write connection. Readers get their own.

**Tests:** a write submitted from an asyncio coroutine completes and is visible to a reader; an exception inside `fn` surfaces through the future rather than killing the thread; `stop()` drains queued work; two concurrent submits serialise without `database is locked`; **a test asserting the listener module never calls `sqlite3.connect` directly** (AST walk over `ingest/`, same technique as `test_purity.py`).

---

### Task 5: Message ingest and dedup

**Files:** create `src/xaucopier/store/messages_repo.py`, `src/xaucopier/store/signals_repo.py`; test `tests/test_messages_repo.py`

**Interface:** `ingest(conn, *, channel_cfg, message_id, text, posted_at_utc, edit_date_utc, reply_to_message_id, source) -> IngestResult` with `IngestResult(outcome: Literal["new","duplicate","edited","relay_duplicate"], signal_id, followup_id, kind, detail)`.

**Three outcomes, not two** — this was a Plan 1 review finding in miniature and it is load-bearing here:
- unseen `(channel_id, message_id)` → parse via the router, insert, `"new"`
- seen, `content_hash` unchanged → `"duplicate"`, no work
- seen, `content_hash` changed **and** `edit_date_utc` is not null → `"edited"` (Task 10 acts on it)

`INSERT OR IGNORE` alone silently swallows every correction. Assert that.

**Cross-channel relay dedup** (spec G7): before enqueuing, reject a signal whose `trade_hash` matches an accepted signal from **any** channel within a rolling window (default 15 min) → `"relay_duplicate"`. `UNIQUE(channel_id, message_id)` cannot express "the other channel relayed the same call", and the user's channel is itself a relay of a VIP channel.

**Tests:** the same message twice → `new` then `duplicate`, one signal row. The same *trade* from two different channels 45 s apart → `new` then `relay_duplicate`, one signal row. An edited SL → `edited`, and the stored parse reflects the new value. A cosmetic edit (whitespace only) → `duplicate`. `raw_text` is stored verbatim for a `skip` message too — every message becomes a fixture.

---

### Task 6: Outbox with leased claims

**Files:** create `src/xaucopier/store/outbox.py`; test `tests/test_outbox.py`

**Interface:** `fan_out(conn, *, signal_id|followup_id, kind, accounts) -> int`, `claim(conn, account_id, pid, lease_seconds=90) -> OutboxRow | None`, `mark(conn, outbox_id, state, reason=None)`, `sweep_expired(conn, now_utc, lease_seconds) -> int`.

**The lease is the point.** A plain `UPDATE ... SET state='CLAIMED'` is a one-way door: a process killed between claiming and its first durable write leaves a row invisible to both the main loop and any recovery sweep — a silently dropped signal that appears in no report. `claim` therefore matches `state='QUEUED' OR (state='CLAIMED' AND claimed_at_utc < now - lease)`, and records `claimed_by_pid`.

Use `BEGIN IMMEDIATE ... RETURNING` so exactly one worker takes a row.

**Tests:** two concurrent claims on one row → one gets it, one gets `None`; a claim older than the lease is reclaimable and the reclaim is counted; `fan_out` writes one row per **enabled** account and none for disabled ones; fan-out and the signal insert happen in **one transaction** (kill between them and neither exists).

---

### Task 7: Secrets, the Telegram client, and the one-time login

**Files:** create `src/xaucopier/ingest/secrets.py`, `src/xaucopier/ingest/client.py`, `src/xaucopier/cli/main.py` (first commands); test `tests/test_secrets.py`

**This is where the user's credentials land. Read spec §10 before writing a line.**

- `api_id`/`api_hash` come from my.telegram.org — one pair per phone number. A sample id from a tutorial triggers `API_ID_PUBLISHED_FLOOD` and bricks the client.
- The session is a **`StringSession` wrapped with Windows DPAPI** (`win32crypt.CryptProtectData`, user-scoped), stored at `config/secrets/session.dpapi` with restrictive NTFS ACLs, never in a cloud-synced path.
- **The session is a bearer credential equal to the whole Telegram account** — it bypasses both the login code and 2FA. Never copied between machines: the same auth key used from two IPs is permanently invalidated (`AuthKeyDuplicatedError`).
- A **named Windows mutex** (`Global\xaucopier_listener`) prevents a second process — including an ad-hoc debug script — from opening the session and producing `database is locked`.
- Client options: `connection_retries=None` (infinite), `retry_delay=5`, `flood_sleep_threshold=120`, `device_model="XAUCOPIER-TRADEBOX"` so the session is recognisable in Settings → Devices and not tidied away by accident.
- `AuthKeyUnregisteredError` / `SessionRevokedError` / `AuthKeyDuplicatedError` are **terminal** — alert and exit non-zero, never retry. Retrying can never succeed.

**CLI:** `xaucopier setup-login` runs the interactive login in a console — phone, code, 2FA password — prompting the user directly. **The implementer must never hard-code, log, echo, or store these values anywhere but the DPAPI blob.**

**Tests** (no network): DPAPI round-trips a string; the blob is not readable as plaintext; a second client instance fails fast on the mutex; the terminal auth errors map to exit code 3 and are not retried. Mock `win32crypt` if `pywin32` is unavailable in CI, but the real path must be exercised on this machine.

---

### Task 8: The listener

**Files:** create `src/xaucopier/ingest/listener.py`, `tests/fakes/telegram.py`; test `tests/test_listener.py`

**Startup order is fixed and each step has a reason:**
1. connect
2. `await client.get_dialogs()` — this primes the per-channel `pts` state Telethon's update engine needs. Not cosmetic; skipping it degrades update delivery.
3. for each configured channel, backfill from `channel_state.last_processed_message_id`
4. register handlers
5. start the poll task (Task 9)
6. `run_until_disconnected()`, wrapped so **any** return exits non-zero for the supervisor

**The handler does exactly four things and nothing else:** normalise, route, submit the ingest write to the writer thread, return. Target under 5 ms. No `get_entity`, no `get_reply_message`, no extra RPC — every one of those is a network round trip on the update path.

**`tests/fakes/telegram.py`** provides a fake client that can emit synthetic messages, simulate a disconnect, and simulate an update blackout. All listener tests run against it.

**Tests:** a message emitted by the fake produces exactly one `messages` row and one `outbox` row per enabled account; a duplicate emission produces no second row; the handler performs no direct `sqlite3` call; a handler exception does not kill the client.

---

### Task 9: The history poll and backfill

**Files:** create `src/xaucopier/ingest/poll.py`; test `tests/test_poll.py`

**Why this exists:** Telethon has documented, unresolved 1–2 minute channel blackouts where `NewMessage` never fires (issue #4361). A missed post is a missed trade, silently. The event stream is a low-latency hint; **this poll is the source of truth.**

- Every **5 seconds**, `get_messages(channel, min_id=last_processed_message_id, reverse=True)` per channel.
- 5 s, not 30 s, and this is deliberate: spec §9 G6 requires one age gate on the message's own timestamp, and the Plan 1 review found that a slow poll plus a tight age gate makes the safety net catch nothing while logging that it works. At 5 s a rescued message is still well inside `max_signal_age_seconds`.
- Every message the poll finds that the event stream never delivered increments `metrics(day_key, 'missed_by_eventstream')`. **This counter is the system's real reliability metric** — surface it in `status`.
- `catch_up=True` is set on the client as a bonus and depended on for nothing.
- Wrap every RPC in `asyncio.wait_for` with a hard timeout and catch `FloodWaitError` around each.

**Tests:** with the fake in blackout, a message posted during the blackout is ingested by the poll within one interval and the missed counter increments; a message delivered by both paths is ingested exactly once; a `FloodWaitError` does not kill the loop; the poll advances `last_processed_message_id` monotonically.

---

### Task 10: Edits and deletions

**Files:** create `src/xaucopier/ingest/edits.py`; test `tests/test_edits.py`

**Edits** (spec §8.3): gate on `edit_date is not None` **and** a material diff of the re-parsed struct against the stored parse — SL, TP, entry, or side. Cosmetic edits are logged and dropped.
- material edit on an **unexecuted** signal → replace the parse in place, bump `signals.version`
- material edit on an **executed** signal → enqueue an `AMEND_SL`/`AMEND_TP` follow-up (Plan 3 applies it)
- an edit changing **direction, symbol or entry** → `state='INVALID'` plus an operator alert. Never silently re-aim a live trade.

**Deletions:** register `events.MessageDeleted`, **and** treat "a message id in my recent window that `get_messages` no longer returns" as a deletion — the poll already re-reads that window, so this is nearly free and covers the delivery gap. Policy: no executed legs → `state='VOIDED'`; live legs → flag and alert, **do not auto-close** (a delete may be cosmetic).

**Tests:** an SL edit on a NEW signal updates the parse and bumps `version`; a whitespace-only edit does not; a direction edit sets `INVALID`; a delete of an unexecuted signal sets `VOIDED`; a message absent from a re-read window is detected as deleted.

---

### Task 11: The `watch` CLI — the deliverable

**Files:** extend `src/xaucopier/cli/main.py`; create `docs/RUNBOOK.md`; test `tests/test_cli.py`

Commands:
- `setup-login` — the one-time interactive Telegram login (Task 7)
- `resolve-channels` — resolve a username or invite link to a **numeric id once**, persist it to `config/channels.toml`. `get_entity` is never called at runtime.
- `watch` — the deliverable. Runs the listener; **contains no order-placing code at all**, and `MetaTrader5` is not importable from this path.
- `status` — rows ingested, signals parsed, skips, parse failures, `missed_by_eventstream`, last message per channel, session health.
- `export-fixtures` — dump every stored `raw_text` into `tests/fixtures/messages/*.json` so the Plan 1 replay harness can consume real traffic.

`docs/RUNBOOK.md` is written **for the user, not for a developer**: get api_id/api_hash, run `setup-login`, run `resolve-channels`, run `watch`, read `status`. Name what each step will prompt for and state plainly that the code never stores the phone code or 2FA password.

**Tests:** `watch` starts and stops cleanly against the fake; `status` reports accurate counts; `export-fixtures` produces files the Plan 1 harness parses; a smoke test asserting no module reachable from `watch` imports `MetaTrader5`.

---

## Definition of done

- `xaucopier watch` runs against the real channel and records signals for hours without intervention.
- `xaucopier status` shows a `missed_by_eventstream` count — proving the poll is doing real work.
- `xaucopier export-fixtures` feeds real messages into the Plan 1 replay harness, and the golden file grows to cover them.
- No order is placed, and no code path reachable from `watch` can place one.

## What this plan does NOT cover

Plan 3 owns: MT5 session and preflight (including the **account-identity assertion**, spec G1 — the single highest-risk item in the design), symbol resolution, order placement, reconciliation, the kill switch, the daily loss limit, the supervisor, and the external dead-man's switch.

Also deferred to Plan 3, carried forward from Plan 1's review: capturing the `CLOSE_HALF` leg list at plan time so a retry cannot recompute it; a distinct intent for modifying a *pending* order; spec §8.1's full follow-up target-resolution chain; per-channel magic offsets; §7.3's TP-clamp logging.

## Known risk

Spec §8's follow-up grammar was designed from the *operations*, not from observed messages — the user has never supplied a real follow-up. Task 11's `export-fixtures` exists partly to close this: once `watch` has captured real "SL to BE" / "close half" traffic, the grammar must be re-tested against it **before** Plan 3 acts on any follow-up. Add captured follow-ups as fixtures first, watch them fail, then adjust the patterns — never the reverse.
