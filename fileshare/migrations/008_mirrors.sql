-- A4 (TIX on orch-core): spaces, mirrored tickets, phone decisions, agent messages, legacy archive.
CREATE TABLE spaces (
  id           TEXT PRIMARY KEY,
  owner_device TEXT NOT NULL REFERENCES devices(id),
  key_version  INTEGER NOT NULL,
  enc_label    TEXT NOT NULL,
  last_seen_at TEXT,
  owner_gen    INTEGER NOT NULL DEFAULT 0,   -- bumped on every ownership transfer (join approved)
  created_at   TEXT NOT NULL,
  deleted_at   TEXT
);
-- A device asks to take a space over; only the browser session approves (ruling TIX-J1). Rows are
-- never deleted: they are the audit trail of who took a space over, when, approved by whom.
CREATE TABLE space_join_requests (
  id           TEXT PRIMARY KEY,
  space_id     TEXT NOT NULL REFERENCES spaces(id),
  device_id    TEXT NOT NULL REFERENCES devices(id),
  from_device  TEXT NOT NULL,                -- the owner when the request was made
  status       TEXT NOT NULL DEFAULT 'pending',
  created_at   TEXT NOT NULL,
  decided_at   TEXT,
  decided_by   TEXT
);
CREATE INDEX space_join_requests_pending ON space_join_requests(space_id) WHERE status = 'pending';
ALTER TABLE tickets ADD COLUMN mode TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE tickets ADD COLUMN space_id TEXT REFERENCES spaces(id);
ALTER TABLE tickets ADD COLUMN schema_version TEXT;
ALTER TABLE tickets ADD COLUMN mirror_rev INTEGER;
ALTER TABLE tickets ADD COLUMN needs TEXT;
ALTER TABLE tickets ADD COLUMN archived_at TEXT;
CREATE UNIQUE INDEX tickets_mirror ON tickets(space_id, uuid) WHERE mode = 'mirror';
CREATE INDEX tickets_needs ON tickets(needs) WHERE mode = 'mirror' AND deleted_at IS NULL;
CREATE TABLE decisions (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  uuid         TEXT NOT NULL UNIQUE,
  space_id     TEXT NOT NULL REFERENCES spaces(id),
  ticket_n     INTEGER REFERENCES tickets(n),
  kind         TEXT NOT NULL,
  session_name TEXT NOT NULL,
  key_version  INTEGER NOT NULL,
  enc_body     TEXT,
  created_at   TEXT NOT NULL,
  ack          TEXT,
  ack_at       TEXT
);
CREATE INDEX decisions_space ON decisions(space_id, seq);
CREATE TABLE messages (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  uuid         TEXT NOT NULL UNIQUE,
  space_id     TEXT REFERENCES spaces(id),
  ticket_n     INTEGER REFERENCES tickets(n),
  from_kind    TEXT NOT NULL,
  from_device  TEXT REFERENCES devices(id),
  from_name    TEXT NOT NULL,
  to_kind      TEXT NOT NULL,
  to_id        TEXT NOT NULL,
  kind         TEXT NOT NULL,
  key_version  INTEGER NOT NULL,
  enc_body     TEXT,
  files        TEXT NOT NULL DEFAULT '[]',
  size         INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL
);
CREATE INDEX messages_to ON messages(to_kind, to_id, seq);
-- Acks are per recipient (ruling TIX-M1): a message to a project or a space stays visible to every other
-- device until that device acks it. recipient = the device id, or 'human' for the browser.
CREATE TABLE message_acks (
  message_seq  INTEGER NOT NULL REFERENCES messages(seq),
  recipient    TEXT NOT NULL,
  acked_at     TEXT NOT NULL,
  PRIMARY KEY (message_seq, recipient)
);
