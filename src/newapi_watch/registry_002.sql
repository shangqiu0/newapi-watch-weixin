-- Registry v2: logical deletion keeps encrypted configuration/history recoverable.
-- deleted_at: removal timestamp; NULL means this is a current monitored platform.
ALTER TABLE platforms ADD COLUMN deleted_at REAL DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_platforms_active ON platforms(created_at,id) WHERE deleted_at IS NULL;
PRAGMA user_version = 2;
