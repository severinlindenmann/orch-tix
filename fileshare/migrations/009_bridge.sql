-- R8 (Orch Remote): the bridge mailbox. Only what must survive a moment lives here: requests waiting for
-- their host and page-response chunks not yet fetched. Bodies are sealed by the clients and stored as the
-- base64url text they arrived in; the server never decodes or parses them. Stream frames never touch the
-- database. Rows expire after 60 s (expires_at, unix seconds) and are deleted on delivery.
CREATE TABLE bridge_msgs (
  n          INTEGER PRIMARY KEY AUTOINCREMENT,
  space_id   TEXT NOT NULL,
  kind       TEXT NOT NULL,
  rid        TEXT NOT NULL,
  idx        INTEGER NOT NULL,
  last       INTEGER NOT NULL,
  body       TEXT NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX bridge_msgs_q ON bridge_msgs(space_id, kind, n);
CREATE INDEX bridge_msgs_exp ON bridge_msgs(expires_at);
