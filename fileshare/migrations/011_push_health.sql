-- QA N-06 / N-08: push subscription health and held pushes that survive a restart.
-- A subscription remembers its last success and its consecutive failures, so one that never works again can be
-- dropped (fileshare.push.prune_failing) instead of staying forever, and the session it was made from, so a
-- re-subscribe from the same browser replaces its row instead of adding a second one.
ALTER TABLE push_subs ADD COLUMN session_hash TEXT;
ALTER TABLE push_subs ADD COLUMN last_success_at TEXT;
ALTER TABLE push_subs ADD COLUMN last_failure_at TEXT;
ALTER TABLE push_subs ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0;
CREATE INDEX push_subs_session ON push_subs(session_hash);
-- Pushes held inside a rate window (mirrors.NeedsPushGate: a workspace's needs pushes; messages.PushGate: a sender's
-- trailing message push). The gates keep them in memory for speed; this table is what a restart sends, so a deploy
-- inside the 60 s window no longer drops them (heldpush.py). kind "needs": ref "<space>|<ticket>"; kind "message":
-- ref the sender key, space/ticket the newest held message, sender its device id (NULL for a browser session).
CREATE TABLE push_held (
  kind     TEXT NOT NULL,
  ref      TEXT NOT NULL,
  space_id TEXT NOT NULL DEFAULT '',
  ticket   TEXT NOT NULL DEFAULT '',
  sender   TEXT,
  held_at  TEXT NOT NULL,
  PRIMARY KEY (kind, ref)
);
