-- Public links (spec §17). The token is stored only as its SHA-256, and the link key never reaches the server.
CREATE TABLE links (
  id                 TEXT PRIMARY KEY,
  file_n             INTEGER NOT NULL REFERENCES files(n),
  token_hash         TEXT UNIQUE NOT NULL,
  wrapped_dek_link   TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  created_by_device  TEXT NULL,
  created_by_session TEXT NULL,
  expires_at         TEXT NOT NULL,
  max_downloads      INTEGER NULL,
  downloads          INTEGER NOT NULL DEFAULT 0,
  revoked_at         TEXT NULL
);
CREATE INDEX links_file ON links(file_n)
