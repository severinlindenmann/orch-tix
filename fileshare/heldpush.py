"""Pushes held inside a rate window survive a restart (QA N-08). `NeedsPushGate` and `PushGate` keep what they hold in
memory; they also write it here, one row per held thing, and delete the row when it is sent, dropped or withdrawn.
After a restart `mirrors.recover_held` and `messages.recover_held` send what is still in the table. Every call opens a
short connection of its own and never raises: a push is best effort and bookkeeping must not break a request."""
import logging

from fileshare import clock
from fileshare.db import connect

log = logging.getLogger("fileshare.push")

MAX_ROWS = 5000        # a flood cannot grow the table without bound


class HeldStore:
    def __init__(self, db_path):
        self.db_path = db_path

    def _run(self, fn):
        try:
            conn = connect(self.db_path)
            try:
                return fn(conn)
            finally:
                conn.close()
        except Exception as e:
            log.warning("push: could not keep a held push: %s", type(e).__name__)
            return None

    def put(self, kind: str, ref: str, *, space: str = "", ticket: str = "", sender: str | None = None) -> None:
        def go(conn):
            if conn.execute("SELECT COUNT(*) FROM push_held").fetchone()[0] >= MAX_ROWS:
                conn.execute("DELETE FROM push_held WHERE rowid IN (SELECT rowid FROM push_held ORDER BY held_at LIMIT 100)")
            conn.execute("INSERT INTO push_held (kind, ref, space_id, ticket, sender, held_at) VALUES (?, ?, ?, ?, ?, ?)"
                         " ON CONFLICT(kind, ref) DO UPDATE SET space_id = excluded.space_id, ticket = excluded.ticket,"
                         " sender = excluded.sender, held_at = excluded.held_at",
                         (kind, ref, space, ticket, sender, clock.now_iso()))
        self._run(go)

    def remove(self, kind: str, ref: str) -> None:
        self._run(lambda conn: conn.execute("DELETE FROM push_held WHERE kind = ? AND ref = ?", (kind, ref)))

    def remove_space(self, kind: str, space: str) -> None:
        self._run(lambda conn: conn.execute("DELETE FROM push_held WHERE kind = ? AND space_id = ?", (kind, space)))

    def take(self, kind: str) -> list[dict]:
        """Every held row of `kind`, removed in the same step (the caller sends them now)."""
        def go(conn):
            rows = [dict(r) for r in conn.execute("SELECT * FROM push_held WHERE kind = ? ORDER BY held_at", (kind,))]
            conn.execute("DELETE FROM push_held WHERE kind = ?", (kind,))
            return rows
        return self._run(go) or []
