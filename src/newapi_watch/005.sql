CREATE TABLE IF NOT EXISTS query_jobs (
 id TEXT PRIMARY KEY,
 dedupe_key TEXT NOT NULL,
 owner_hash TEXT NOT NULL,
 query TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'queued',
 progress TEXT NOT NULL DEFAULT '{}',
 result TEXT NOT NULL DEFAULT '',
 context_json TEXT NOT NULL DEFAULT '{}', notified_at REAL NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '',
 next_at REAL NOT NULL DEFAULT 0,
 created_at REAL NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL DEFAULT 0,
 UNIQUE(dedupe_key,owner_hash)
);
CREATE INDEX IF NOT EXISTS idx_query_jobs_schedule ON query_jobs(status,next_at,created_at);
CREATE INDEX IF NOT EXISTS idx_query_jobs_owner ON query_jobs(owner_hash,updated_at);
PRAGMA user_version = 6;
