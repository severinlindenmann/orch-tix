"""Web Push (spec T7): the server-side half. All cipher work happens in the
`python -m fileshare.pushworker` subprocess; this module never imports `cryptography` or
`pywebpush`, so `tests/test_files_api.py::test_server_never_imports_cryptography` stays green.
"""
import json
import logging
import subprocess
import sys
import threading

from fileshare.db import connect, get_meta, set_meta

log = logging.getLogger("fileshare.push")

PUSH_TIMEOUT_S = 30
GENKEYS_TIMEOUT_S = 30
TTL_S = 86400


def ensure_vapid(conn) -> str:
    """The VAPID public key (b64u), generating and storing the key pair on first use (spec T6/T9)."""
    public = get_meta(conn, "vapid_public")
    if public is not None:
        return public
    proc = subprocess.run([sys.executable, "-m", "fileshare.pushworker", "genkeys"],
                          capture_output=True, text=True, timeout=GENKEYS_TIMEOUT_S, check=True)
    keys = json.loads(proc.stdout)
    # A double-checked write: two concurrent first-uses could both reach here (isolation_level=None
    # means no implicit transaction), so BEGIN IMMEDIATE serialises them and the loser keeps the
    # winner's keys instead of overwriting them.
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = get_meta(conn, "vapid_public")
        if existing is not None:
            conn.execute("COMMIT")
            return existing
        set_meta(conn, "vapid_private", keys["private"])
        set_meta(conn, "vapid_public", keys["public"])
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return keys["public"]


class SubprocessPusher:
    """app.state.pusher: runs `pushworker send` in a daemon thread for every subscription, then
    prunes the ones the push service says are gone (spec T7)."""

    def __init__(self, settings, db_path):
        self.settings = settings
        self.db_path = db_path

    def notify(self, ticket: dict, event: dict) -> None:
        threading.Thread(target=self._deliver, args=(ticket, event), daemon=True).start()

    def notify_payload(self, payload: dict) -> None:
        """Push v2 (TIX spec §7): one ready cleartext payload to every subscription. The caller builds it
        from cleartext routing only (space id, TIX id, kind, counts), never a title or text."""
        threading.Thread(target=self._deliver_payload, args=(json.dumps(payload, separators=(",", ":")),),
                         daemon=True).start()

    def _deliver(self, ticket: dict, event: dict) -> None:
        """Push v1 for a legacy ticket: build the fields, then hand the payload on."""
        fields = {"t": ticket["id"], "s": ticket["status"], "p": ticket["project"],
                  "k": event["kind"], "n": ticket["open_questions"]}
        if event.get("manual"):
            fields["m"] = True        # a manual test: nothing ran, so not "tests passed"
        self._deliver_payload(json.dumps(fields))

    def _deliver_payload(self, payload: str) -> None:
        conn = connect(self.db_path)
        try:
            subs = conn.execute("SELECT id, endpoint, p256dh, auth FROM push_subs").fetchall()
            if not subs:
                return
            try:
                ensure_vapid(conn)                      # returns the PUBLIC key; the worker signs
                private = get_meta(conn, "vapid_private")   # with the private one
                if not private:
                    raise LookupError("vapid_private missing")
            except Exception as e:
                # genkeys can time out, exit non-zero, or print bad JSON. It must never escape this
                # daemon thread (only threading.excepthook would see it) or skip the spec's log line.
                log.warning("push: %d failed (vapid: %s)", len(subs), type(e).__name__)
                return
        finally:
            conn.close()
        req = {
            "private": private,
            "sub": self.settings.public_url,
            "subs": [{"id": r["id"], "endpoint": r["endpoint"],
                      "keys": {"p256dh": r["p256dh"], "auth": r["auth"]}} for r in subs],
            "payload": payload,
            "ttl": TTL_S,
        }
        try:
            proc = subprocess.run([sys.executable, "-m", "fileshare.pushworker", "send"],
                                  input=json.dumps(req), capture_output=True, text=True,
                                  timeout=PUSH_TIMEOUT_S)
            result = json.loads(proc.stdout)
        except Exception as e:
            log.warning("push: worker failed: %s", e)
            return
        failed = result.get("failed", [])
        if failed:
            log.warning("push: %d failed", len(failed))
        gone = result.get("gone", [])
        if gone:
            conn = connect(self.db_path)
            try:
                qs = ",".join("?" * len(gone))
                conn.execute(f"DELETE FROM push_subs WHERE id IN ({qs})", gone)
            finally:
                conn.close()
