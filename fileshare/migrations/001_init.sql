CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE devices (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  project      TEXT NOT NULL,
  hostname     TEXT NOT NULL DEFAULT '',
  token_hash   TEXT NOT NULL UNIQUE,
  pubkey       TEXT NOT NULL,
  fingerprint  TEXT NOT NULL,
  platform     TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL,
  approved_at  TEXT,
  device_bundle TEXT,
  last_seen_at TEXT,
  revoked_at   TEXT
);

CREATE TABLE onboarding_tokens (
  id            TEXT PRIMARY KEY,
  lookup_hash   TEXT NOT NULL UNIQUE,
  created_at    TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  used_at       TEXT,
  device_id     TEXT REFERENCES devices(id)
);

CREATE TABLE files (
  n           INTEGER PRIMARY KEY AUTOINCREMENT,
  uuid        TEXT NOT NULL UNIQUE,
  size        INTEGER NOT NULL,
  key_version INTEGER NOT NULL,
  wrapped_dek TEXT NOT NULL,
  enc_meta    TEXT NOT NULL,
  device_id   TEXT REFERENCES devices(id),
  project     TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  deleted_at  TEXT
);
CREATE INDEX files_created ON files(created_at DESC);
CREATE INDEX files_device  ON files(device_id);

CREATE TABLE sessions (
  id_hash      TEXT PRIMARY KEY,
  created_at   TEXT NOT NULL,
  last_used_at TEXT NOT NULL
);
