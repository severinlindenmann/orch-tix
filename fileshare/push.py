"""Web Push (spec T7): the server-side half. All cipher work happens in the
`python -m fileshare.pushworker` subprocess; this module never imports `cryptography` or
`pywebpush`, so `tests/test_files_api.py::test_server_never_imports_cryptography` stays green.
"""
import json
import logging
import subprocess
import sys
import collections
import threading
import hashlib
from urllib.parse import urlsplit
import base64

from fileshare.db import connect, get_meta, set_meta

log = logging.getLogger("fileshare.push")

PUSH_TIMEOUT_S = 30
GENKEYS_TIMEOUT_S = 30
TTL_S = 86400
# A "needs you" that reaches a phone late is worse than none (it can announce something already handled): needs-type
# pushes expire at the push service after 30 minutes; a clear and the rest keep the day.
NEEDS_TTL_S = 1800
APPLE_PUSH_HOSTS = ("web.push.apple.com",)


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


def topic_for(payload: dict) -> str | None:
    """Web Push `Topic` (RFC 8030 5.4, max 32 chars): a push the phone has not received yet is REPLACED by a newer
    one with the same topic at the push service. One topic per (workspace, ticket) so a queued "needs you" for a
    phone that is offline is replaced by the "clear" that follows it; non-ticket kinds get a topic per kind and
    workspace."""
    if payload.get("v") != 2:
        return None
    key = f"{payload.get('s', '')}|{payload.get('t') or payload.get('k', '')}"
    return base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()[:18]).decode().rstrip("=")   # 24 chars


def ttl_for(payload: dict) -> int:
    return NEEDS_TTL_S if payload.get("v") == 2 and payload.get("k") in ("question", "approval", "verdict", "batch", "message", "join") else TTL_S


def replaces_by_tag(endpoint: str) -> bool:
    """Chrome, Firefox and Android replace a notification by its tag; iOS (WebKit, web.push.apple.com) shows every
    push as a NEW notification next to the old one (live test 2026-10-05), so a "clear" would only add noise there."""
    try:
        host = (urlsplit(endpoint).hostname or "").lower()
    except ValueError:
        return True
    return not any(host == h or host.endswith("." + h) for h in APPLE_PUSH_HOSTS)


class SubprocessPusher:
    """app.state.pusher: runs `pushworker send` in a daemon thread for every subscription, then
    prunes the ones the push service says are gone (spec T7)."""

    def __init__(self, settings, db_path):
        self.settings = settings
        self.db_path = db_path
        # One worker: pushes leave in the order they were raised, so a "clear" can never overtake the "needs you"
        # it withdraws (QA N-03). Each push is still a subprocess; the queue only serialises them.
        self._q: collections.deque = collections.deque()
        self._running = False
        self._lock = threading.Lock()

    def _submit(self, fn, *args) -> None:
        with self._lock:
            self._q.append((fn, args))
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._q:
                    self._running = False
                    return
                fn, args = self._q.popleft()
            try:
                fn(*args)
            except Exception as e:      # a push never fails anything
                log.warning("push: delivery crashed: %s", type(e).__name__)

    def notify(self, ticket: dict, event: dict) -> None:
        self._submit(self._deliver, ticket, event)

    def notify_payload(self, payload: dict) -> None:
        """Push v2 (TIX spec §7): one ready cleartext payload to every subscription. The caller builds it
        from cleartext routing only (space id, TIX id, kind, counts), never a title or text."""
        self._submit(self._deliver_payload, json.dumps(payload, separators=(",", ":")), topic_for(payload),
                     ttl_for(payload), payload.get("k") == "clear")

    def _deliver(self, ticket: dict, event: dict) -> None:
        """Push v1 for a legacy ticket: build the fields, then hand the payload on."""
        fields = {"t": ticket["id"], "s": ticket["status"], "p": ticket["project"],
                  "k": event["kind"], "n": ticket["open_questions"]}
        if event.get("manual"):
            fields["m"] = True        # a manual test: nothing ran, so not "tests passed"
        self._deliver_payload(json.dumps(fields))

    def _deliver_payload(self, payload: str, topic: str | None = None, ttl: int = TTL_S, is_clear: bool = False) -> None:
        conn = connect(self.db_path)
        try:
            subs = conn.execute("SELECT id, endpoint, p256dh, auth FROM push_subs").fetchall()
            if is_clear:        # iOS cannot replace a notification by tag: a clear would only add one (see replaces_by_tag)
                subs = [r for r in subs if replaces_by_tag(r["endpoint"])]
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
            "ttl": ttl,
            "topic": topic,
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
        gone = result.get("gone", [])
        try:
            kind = json.loads(payload).get("k")
        except (ValueError, AttributeError):
            kind = None
        # one line per push, no ids or text: the owner can see "was a push sent, to how many, did they fail"
        log.info("push: k=%s sent=%d failed=%d gone=%d", kind, len(subs) - len(failed) - len(gone), len(failed), len(gone))
        if failed:
            log.warning("push: %d failed", len(failed))
        if gone:
            conn = connect(self.db_path)
            try:
                qs = ",".join("?" * len(gone))
                conn.execute(f"DELETE FROM push_subs WHERE id IN ({qs})", gone)
            finally:
                conn.close()
