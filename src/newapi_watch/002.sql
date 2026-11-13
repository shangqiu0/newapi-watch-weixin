-- v2 additive migration: acknowledgement applies only to the observed alert generation.
CREATE TABLE IF NOT EXISTS alert_acknowledgements (
 id TEXT PRIMARY KEY,
 alert_id TEXT NOT NULL REFERENCES alerts(id),
 generation INTEGER NOT NULL DEFAULT 1,
 actor_hash TEXT NOT NULL DEFAULT '',
 created_at REAL NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL DEFAULT 0,
 UNIQUE(alert_id,generation)
);
-- Persist operator retry requests, so transport replay cannot reset a second dead-letter cycle.
CREATE TABLE IF NOT EXISTS notification_retries (
 id TEXT PRIMARY KEY,
 notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
 created_at REAL NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL DEFAULT 0
);
-- Audits intentionally omit raw command text, parameters, usernames, URLs and results.
CREATE TABLE IF NOT EXISTS command_audits (
 id TEXT PRIMARY KEY,
 source TEXT NOT NULL DEFAULT 'local',
 actor_hash TEXT NOT NULL DEFAULT '',
 action TEXT NOT NULL DEFAULT 'help',
 outcome TEXT NOT NULL DEFAULT 'success',
 duration_ms INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audits_time ON command_audits(created_at,id);
PRAGMA user_version=2;
