-- Schema v3: only sanitized error metadata, durable cooldowns and account samples.
ALTER TABLE logs ADD COLUMN http_status INTEGER NOT NULL DEFAULT 0;
ALTER TABLE logs ADD COLUMN error_code TEXT NOT NULL DEFAULT '';
ALTER TABLE logs ADD COLUMN error_message TEXT NOT NULL DEFAULT '';
ALTER TABLE logs ADD COLUMN channel_name TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_logs_status_time ON logs(http_status,created_at);
CREATE TABLE IF NOT EXISTS error_seen (
 id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS error_buckets (
 id TEXT PRIMARY KEY, last_sent_at REAL NOT NULL DEFAULT 0, pending_count INTEGER NOT NULL DEFAULT 0,
 sample TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS account_samples (
 id INTEGER PRIMARY KEY, quota INTEGER NOT NULL DEFAULT 0, threshold INTEGER NOT NULL DEFAULT 0,
 is_low INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
ALTER TABLE notifications ADD COLUMN kind TEXT NOT NULL DEFAULT 'channel';
ALTER TABLE notifications ADD COLUMN mention_all INTEGER NOT NULL DEFAULT 0;
PRAGMA user_version = 3;
