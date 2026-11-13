CREATE TABLE IF NOT EXISTS platforms (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
 config TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bindings (
 id TEXT PRIMARY KEY, platform_id TEXT NOT NULL REFERENCES platforms(id), kind TEXT NOT NULL,
 name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, config TEXT NOT NULL,
 bot_fingerprint TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_bot ON bindings(bot_fingerprint) WHERE kind='bot' AND enabled=1;
CREATE INDEX IF NOT EXISTS idx_bindings_platform ON bindings(platform_id);
CREATE TABLE IF NOT EXISTS accounts (
 id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, username TEXT NOT NULL, csrf_hash TEXT NOT NULL,
 expires_at REAL NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS login_attempts (
 id TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0, until_at REAL NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS target_deliveries (
 id TEXT PRIMARY KEY, platform_id TEXT NOT NULL REFERENCES platforms(id),
 binding_id TEXT NOT NULL REFERENCES bindings(id), created_at REAL NOT NULL, updated_at REAL NOT NULL
);
PRAGMA user_version=1;
