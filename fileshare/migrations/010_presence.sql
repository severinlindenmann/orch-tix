-- R9 (Remote): host presence, one row per workspace. Only clear status lives here: timestamps (unix
-- seconds), three counts, a short Factory state code and integers. No name of any kind is stored.
CREATE TABLE IF NOT EXISTS presence (
  space_id       TEXT PRIMARY KEY REFERENCES spaces(id),
  device_id      TEXT NOT NULL,
  hb_at          REAL NOT NULL,
  bye_at         REAL,
  sessions       INTEGER NOT NULL,
  in_progress    INTEGER NOT NULL,
  needs_you      INTEGER NOT NULL,
  factory        TEXT NOT NULL,
  children_done  INTEGER,
  children_total INTEGER,
  budget_pct     INTEGER
);
