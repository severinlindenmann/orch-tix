"""Agent messages (TIX spec §8): sealed bodies, cleartext routing (who to whom, kind, size). A device sends
to a device, a project, a space or the human; the browser session sends to agents only. A device may tag
a message with a space (and a ticket of it) only when it owns that space; the browser may tag any space. Acks are per
recipient (ruling TIX-M1): a message acked by one device leaves only that device's list; the others (same
project, a later owner of the space) still see it until they ack it too. The browser acks as 'human'."""
import json
import logging
import threading

from fileshare import clock
from fileshare.deps import api_error, rate_slot
from fileshare.ids import format_id, format_ticket_id, parse_ticket_ref
from fileshare.mirrors import attention_total, owner_space, push_v2, space_row
from fileshare.notify import message_clear_wanted, message_push_allowed
from fileshare.sessions import session_name
from fileshare.tickets import _live_file_ns, _tx

log = logging.getLogger("fileshare.push")

TO_KINDS = ("device", "project", "space", "human")
KINDS = ("text", "file", "question")
MAX_FILES = 10
LIST_LIMIT = 100
MESSAGES_PER_HOUR = 120           # per sender (batch-2 review)
HUMAN_PUSH_EVERY_S = 60           # at most one "message" push per sending device per minute


def _daemon_timer(delay: float, fn) -> None:
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


class PushGate:
    """At most one push per key every `every_s` seconds; in memory, one process. The first message after a
    quiet minute is pushed at once (leading edge). Messages held back inside the minute get one push when it
    ends (trailing edge, batch 3 review m1): `due(key, later)` arms a single timer per key, which calls the
    `later` of the newest held message (its space and ticket, Task 9 review). Either push carries the
    sender's unread count in `n`. `timer` is replaceable in tests."""

    MAX_TRACKED = 10_000

    def __init__(self, every_s: int = HUMAN_PUSH_EVERY_S):
        self.every_s = every_s
        self._last: dict[str, float] = {}
        self._armed: dict[str, object] = {}       # key -> the newest held message's `later`
        self._lock = threading.Lock()
        self.timer = _daemon_timer

    def due(self, key: str, later=None) -> bool:
        now = clock.now().timestamp()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self.every_s:
                if later is not None:
                    if key not in self._armed:
                        self.timer(self.every_s - (now - last), lambda: self._trail(key))
                    self._armed[key] = later
                return False
            if len(self._last) >= self.MAX_TRACKED:
                self._last = {k: v for k, v in self._last.items() if now - v < self.every_s}
            self._last[key] = now
            return True

    def _trail(self, key: str) -> None:
        with self._lock:
            later = self._armed.pop(key, None)
            self._last[key] = clock.now().timestamp()      # the trailing push opens the next window
        if later is None:
            return
        try:
            later()
        except Exception:     # a push never fails anything; the next message pushes again
            pass


def message_out(row, acked_at: str | None = None) -> dict:
    return {"id": "msg_" + row["uuid"], "seq": row["seq"], "uuid": row["uuid"], "from_kind": row["from_kind"],
            "from_name": row["from_name"], "to_kind": row["to_kind"], "to_id": row["to_id"], "kind": row["kind"],
            "key_version": row["key_version"], "enc_body": row["enc_body"], "files": json.loads(row["files"]),
            "space": row["space_id"], "ticket": format_ticket_id(row["ticket_n"]) if row["ticket_n"] else None,
            "size": row["size"], "created_at": row["created_at"], "acked_at": acked_at}


def recipient_key(principal) -> str:
    """Who an ack belongs to: the device id, or 'human' for any browser session."""
    return "human" if principal.kind == "session" else principal.device["id"]


def _sender(conn, principal) -> tuple[str, str | None, str]:
    if principal.kind == "session":
        return "human", None, session_name(conn, principal.session_hash) or "browser"
    return "device", principal.device["id"], principal.device["name"]


def create_message(conn, app, *, principal, body: dict) -> dict:
    from_kind, from_device, from_name = _sender(conn, principal)
    if from_kind == "human" and body["to_kind"] == "human":
        raise api_error(400, "bad_request", "the browser sends messages to agents, not to itself")
    sender_key = from_device or "session:" + principal.session_hash
    with rate_slot(app.state.message_limiter, sender_key,
                   f"at most {MESSAGES_PER_HOUR} messages per hour"), _tx(conn):
        if conn.execute("SELECT 1 FROM messages WHERE uuid = ?", (body["uuid"],)).fetchone():
            raise api_error(409, "duplicate_uuid", "this message was already sent")
        ticket_n = None
        if body["space"] is not None:
            space_row(conn, body["space"])                                     # 404 no_space
            if principal.kind == "device":     # a space (and ticket) tag only from its owner (batch-2 review)
                owner_space(conn, body["space"], principal.device)            # 403 not_owner
        if body["ticket"] is not None:
            ticket_n = parse_ticket_ref(body["ticket"])
            if body["space"] is None or conn.execute(
                    "SELECT 1 FROM tickets WHERE n = ? AND mode = 'mirror' AND space_id = ? AND deleted_at IS NULL",
                    (ticket_n, body["space"])).fetchone() is None:
                raise api_error(400, "bad_ref", "ticket must be a live mirrored ticket of the message's space")
        files = [format_id(n) for n in _live_file_ns(conn, body["files"])]   # 400 bad_ref
        cur = conn.execute(
            "INSERT INTO messages (uuid, space_id, ticket_n, from_kind, from_device, from_name, to_kind, to_id, kind,"
            " key_version, enc_body, files, size, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (body["uuid"], body["space"], ticket_n, from_kind, from_device, from_name, body["to_kind"], body["to_id"],
             body["kind"], body["key_version"], body["enc_body"], json.dumps(files), body["size"], clock.now_iso()))
    seq = cur.lastrowid
    app.state.message_bus.bump(seq)
    # Phone notifications are off unless the owner chose them: a ticketed message follows its ticket, the rest the space's
    # setting. The message itself is stored and shown in the app either way.
    if body["to_kind"] == "human" and message_push_allowed(conn, body["space"], ticket_n):
        payload = {"v": 2, "s": body["space"] or "", "t": format_ticket_id(ticket_n) if ticket_n else "", "k": "message"}

        store = getattr(app.state, "held_store", None)

        def trailing() -> None:            # at the end of the sender's minute, on its own connection
            from fileshare import db
            if store is not None:
                store.remove("message", sender_key)
            c = db.connect(app.state.settings.db_path)
            try:
                unread = _unread_for_human(c, from_device)
                if unread and message_push_allowed(c, body["space"], ticket_n):
                    push_v2(app, {**payload, "n": unread, "c": attention_total(c)})
            finally:
                c.close()

        if app.state.message_push_gate.due(sender_key, trailing):
            push_v2(app, {**payload, "n": _unread_for_human(conn, from_device), "c": attention_total(conn)})
        elif store is not None:        # held for the trailing push: kept in the database too (QA N-08)
            store.put("message", sender_key, space=payload["s"], ticket=payload["t"], sender=from_device)
    return {"id": "msg_" + body["uuid"], "seq": seq}


def recover_held(app) -> int:
    """After a restart: the trailing "message" pushes the previous process still held (QA N-08), one per sender, when
    something from that sender is still unread."""
    store = getattr(app.state, "held_store", None)
    if store is None:
        return 0
    from fileshare import db
    rows = store.take("message")
    for r in rows:
        c = db.connect(app.state.settings.db_path)
        try:
            unread = _unread_for_human(c, r["sender"])
            if unread:
                push_v2(app, {"v": 2, "s": r["space_id"], "t": r["ticket"], "k": "message", "n": unread,
                              "c": attention_total(c)})
        except Exception as e:      # a push never fails anything
            log.warning("push: recovering a held message push failed: %s", e)
        finally:
            c.close()
    return len(rows)


def _unread_for_human(conn, from_device) -> int:
    return conn.execute("SELECT COUNT(*) FROM messages m WHERE m.to_kind = 'human' AND m.from_device IS ? AND"
                        " NOT EXISTS (SELECT 1 FROM message_acks a WHERE a.message_seq = m.seq AND"
                        " a.recipient = 'human')", (from_device,)).fetchone()[0]


def _recipient_clause(conn, principal) -> tuple[str, list]:
    """SQL for "addressed to this principal". A device: its id, its project, the spaces it owns."""
    if principal.kind == "session":
        return "to_kind = 'human'", []
    device = principal.device
    owned = [r["id"] for r in conn.execute("SELECT id FROM spaces WHERE owner_device = ? AND deleted_at IS NULL",
                                           (device["id"],))]
    clause = "((to_kind = 'device' AND to_id = ?) OR (to_kind = 'project' AND to_id = ?)"
    args: list = [device["id"], device["project"]]
    if owned:
        clause += f" OR (to_kind = 'space' AND to_id IN ({','.join('?' * len(owned))}))"
        args += owned
    return clause + ")", args


def messages_for(conn, principal, after: int, limit: int = LIST_LIMIT) -> list[dict]:
    clause, args = _recipient_clause(conn, principal)
    if principal.kind == "device":       # never your own messages back (a device writing to its own project)
        clause += " AND (from_device IS NULL OR from_device != ?)"
        args.append(principal.device["id"])
    rows = conn.execute(f"SELECT * FROM messages WHERE {clause} AND seq > ? AND NOT EXISTS"
                        " (SELECT 1 FROM message_acks a WHERE a.message_seq = messages.seq AND a.recipient = ?)"
                        " ORDER BY seq LIMIT ?", (*args, after, recipient_key(principal), limit)).fetchall()
    return [message_out(r) for r in rows]


def ack_message(conn, principal, uuid: str, app=None) -> None:
    clause, args = _recipient_clause(conn, principal)
    with _tx(conn):
        row = conn.execute("SELECT * FROM messages WHERE uuid = ?", (uuid,)).fetchone()
        if row is None:
            raise api_error(404, "not_found", "no such message")
        if conn.execute(f"SELECT 1 FROM messages WHERE uuid = ? AND {clause}", (uuid, *args)).fetchone() is None:
            raise api_error(403, "forbidden", "only a recipient acks a message")
        conn.execute("INSERT OR IGNORE INTO message_acks (message_seq, recipient, acked_at) VALUES (?, ?, ?)",
                     (row["seq"], recipient_key(principal), clock.now_iso()))
    # The owner read it in the browser: withdraw the phone's "Message from agent" once nothing unread is left in
    # that workspace (QA N-04). `w` tells the service worker which notification the clear belongs to.
    if app is not None and principal.kind == "session" and row["to_kind"] == "human":
        left = conn.execute("SELECT COUNT(*) FROM messages m WHERE m.to_kind = 'human' AND m.space_id IS ? AND NOT EXISTS"
                            " (SELECT 1 FROM message_acks a WHERE a.message_seq = m.seq AND a.recipient = 'human')",
                            (row["space_id"],)).fetchone()[0]
        if left == 0 and message_clear_wanted(conn, row["space_id"]):
            push_v2(app, {"v": 2, "s": row["space_id"] or "", "t": "", "k": "clear", "w": "message", "n": 0,
                          "c": attention_total(conn)})
