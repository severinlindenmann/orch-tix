-- Tickets (spec T4), verbatim, plus ticket_events.files (controller ruling 2026-09-25).
CREATE TABLE tickets (
  n            INTEGER PRIMARY KEY AUTOINCREMENT,    -- TIX-<n>, never reused
  uuid         TEXT NOT NULL UNIQUE,                 -- 32 hex
  key_version  INTEGER NOT NULL,
  wrapped_dek  TEXT,                                 -- NULL once deleted
  enc_content  TEXT,                                 -- NULL once deleted
  status       TEXT NOT NULL,                        -- T2 values
  project      TEXT NOT NULL,
  type         TEXT NOT NULL DEFAULT 'feature',      -- feature | bug | chore | spike
  priority     TEXT NOT NULL DEFAULT 'normal',       -- low | normal | high | urgent
  due          TEXT,                                 -- YYYY-MM-DD or NULL
  parent_n     INTEGER REFERENCES tickets(n),
  created_by_device TEXT REFERENCES devices(id),     -- NULL = browser
  created_by_name   TEXT NOT NULL,                   -- device name or browser session name at the time
  opened_by_name    TEXT,                            -- who last moved it to open
  rev          INTEGER NOT NULL DEFAULT 1,
  claim_device_id TEXT REFERENCES devices(id),
  claim_token_hash TEXT,                             -- sha256 hex of clm_<43 chars>
  claim_seen_at TEXT,                                -- any holder request
  claim_poll_at TEXT,                                -- holder long-poll heartbeat
  open_questions INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  deleted_at   TEXT
);
CREATE INDEX tickets_status ON tickets(status, updated_at DESC);
CREATE TABLE ticket_labels (ticket_n INTEGER NOT NULL REFERENCES tickets(n), label TEXT NOT NULL,
                            PRIMARY KEY (ticket_n, label));
CREATE TABLE ticket_blocks (ticket_n INTEGER NOT NULL REFERENCES tickets(n),
                            blocked_by_n INTEGER NOT NULL REFERENCES tickets(n),
                            PRIMARY KEY (ticket_n, blocked_by_n));
CREATE TABLE ticket_files  (ticket_n INTEGER NOT NULL REFERENCES tickets(n),
                            file_n INTEGER NOT NULL REFERENCES files(n),
                            PRIMARY KEY (ticket_n, file_n));
CREATE TABLE ticket_events (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,    -- global cursor for long-polls
  ticket_n     INTEGER NOT NULL REFERENCES tickets(n),
  uuid         TEXT NOT NULL UNIQUE,                 -- client-generated; a retry is idempotent
  kind         TEXT NOT NULL,                        -- created|update|question|answer|test|verdict|status|claim|edit|release
  actor_kind   TEXT NOT NULL,                        -- device | web
  actor_id     TEXT,                                 -- device id, NULL for web
  actor_name   TEXT NOT NULL,
  status_from  TEXT,
  status_to    TEXT,
  enc_body     TEXT,                                 -- NULL for bodiless kinds, and once deleted
  files        TEXT NOT NULL DEFAULT '[]',           -- JSON list of the FILE ids this event linked
  created_at   TEXT NOT NULL
);
CREATE INDEX ticket_events_ticket ON ticket_events(ticket_n, seq);
CREATE TABLE push_subs (
  id           TEXT PRIMARY KEY,                     -- "psh_" + 12 hex
  session_name TEXT NOT NULL,
  endpoint     TEXT NOT NULL UNIQUE,
  p256dh       TEXT NOT NULL,
  auth         TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
