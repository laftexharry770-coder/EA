-- Migration 1: the original schema. Later migrations change it in
-- migrations.py (migration 2: the alerts table, messages deletion marks,
-- followups.edit_seq) -- read both for the current shape.
CREATE TABLE IF NOT EXISTS messages (
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
CREATE TABLE IF NOT EXISTS signals (
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
CREATE INDEX IF NOT EXISTS idx_signals_trade_hash ON signals(trade_hash);
CREATE TABLE IF NOT EXISTS followups (
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
CREATE TABLE IF NOT EXISTS outbox (
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
CREATE INDEX IF NOT EXISTS idx_outbox_claim ON outbox(account_id, state, outbox_id);
CREATE TABLE IF NOT EXISTS channel_state (
  channel_id                INTEGER PRIMARY KEY,
  last_processed_message_id INTEGER NOT NULL DEFAULT 0,
  consecutive_parse_failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS metrics (
  day_key TEXT NOT NULL, name TEXT NOT NULL, value INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day_key, name)
);
