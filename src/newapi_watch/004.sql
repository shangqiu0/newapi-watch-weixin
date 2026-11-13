-- v4: bounded current monitoring state, not a raw business-data mirror.
CREATE TABLE IF NOT EXISTS monitor_checks (
 id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
 last_at REAL NOT NULL DEFAULT 0, last_ok_at REAL NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
 created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS monitor_states (
 id TEXT PRIMARY KEY, is_active INTEGER NOT NULL DEFAULT 0,
 streak INTEGER NOT NULL DEFAULT 0, last_sent_at REAL NOT NULL DEFAULT 0,
 evidence TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL DEFAULT 0
);
ALTER TABLE notifications ADD COLUMN binding_ids TEXT NOT NULL DEFAULT '[]';
PRAGMA user_version = 4;
