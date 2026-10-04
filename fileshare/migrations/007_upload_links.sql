-- Inbound one-time upload links (upload-links spec). Only the token's SHA-256 is stored; the link's
-- private key is sealed under the owner's master key, its public key lives only in the URL fragment.
CREATE TABLE upload_links (
  id                 TEXT PRIMARY KEY,
  uuid               TEXT NOT NULL UNIQUE,
  token_hash         TEXT NOT NULL UNIQUE,
  key_version        INTEGER NOT NULL,
  wrapped_lpriv      TEXT NOT NULL,
  enc_label          TEXT NULL,
  created_at         TEXT NOT NULL,
  created_by_device  TEXT NULL,
  created_by_session TEXT NULL,
  expires_at         TEXT NOT NULL,
  revoked_at         TEXT NULL,
  used_at            TEXT NULL,
  file_uuid          TEXT NULL UNIQUE,
  file_size          INTEGER NULL,
  sealed_dek         TEXT NULL,
  enc_meta           TEXT NULL,
  file_n             INTEGER NULL REFERENCES files(n),
  adopted_at         TEXT NULL
);
CREATE INDEX upload_links_created ON upload_links(created_at DESC)
