-- v2 (spec §14). Additive only: every existing row stays valid, with the new columns
-- empty/NULL (sessions unnamed, files never expire, nothing acknowledged).
ALTER TABLE sessions ADD COLUMN name TEXT NOT NULL DEFAULT '';

ALTER TABLE files ADD COLUMN session_name TEXT;
ALTER TABLE files ADD COLUMN acked_at TEXT;
ALTER TABLE files ADD COLUMN acked_by TEXT;
ALTER TABLE files ADD COLUMN expires_at TEXT;
CREATE INDEX files_expires ON files(expires_at) WHERE expires_at IS NOT NULL;
