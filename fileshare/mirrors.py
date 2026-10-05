"""Spaces and mirrored tickets (TIX spec §4, §9). orch-core is the master: the server stores sealed snapshots,
keeps the newest by mirror_rev, and enforces only who may write. No move matrix, no claims for mirrors."""
import hashlib
import logging
import re
import secrets
import sqlite3
import threading
from datetime import timedelta

from fileshare import clock
from fileshare.deps import api_error, rate_slot
from fileshare.ids import format_ticket_id
from fileshare.tickets import _tx, _write_event, Actor

SCHEMA_MAJORS = ("1",)
NEEDS = ("question", "approval", "verdict")
SPACE_CREATES_PER_HOUR = 10       # per device (Task 2 review)
MIRROR_CREATES_PER_HOUR = 300
MIRROR_UPDATES_PER_HOUR = 600       # per device: a runaway drain can't flood the server (final review M4)
_SEMVER = re.compile(r"(\d+)\.\d+\.\d+")
log = logging.getLogger("fileshare.mirrors")


def space_row(conn, space_id: str):
    row = conn.execute("SELECT * FROM spaces WHERE id = ? AND deleted_at IS NULL", (space_id,)).fetchone()
    if row is None:
        raise api_error(404, "no_space", "no such space; run `sharing space create` in the workspace")
    return row


NOT_OWNER_DETAIL = ("another device owns this space; ask to take it over with `sharing space join`; "
                    "approve it in TIX")


def owner_space(conn, space_id: str, device) -> sqlite3.Row:
    row = space_row(conn, space_id)
    if row["owner_device"] != device["id"]:
        raise api_error(403, "not_owner", NOT_OWNER_DETAIL)
    return row


def space_out(conn, row) -> dict:
    owner = conn.execute("SELECT name FROM devices WHERE id = ?", (row["owner_device"],)).fetchone()
    needs = conn.execute("SELECT COUNT(*) FROM tickets WHERE mode = 'mirror' AND space_id = ? AND deleted_at IS NULL"
                         " AND needs IS NOT NULL", (row["id"],)).fetchone()[0]
    return {"id": row["id"], "key_version": row["key_version"], "enc_label": row["enc_label"],
            "owner_device": row["owner_device"], "owner_name": owner["name"] if owner else "",
            "last_seen_at": row["last_seen_at"], "needs": needs, "created_at": row["created_at"]}


def create_space(conn, app, *, device, space_id: str, key_version: int, enc_label: str) -> dict:
    with rate_slot(app.state.space_create_limiter, device["id"],
                   f"at most {SPACE_CREATES_PER_HOUR} new spaces per hour per device"), _tx(conn):
        if conn.execute("SELECT 1 FROM spaces WHERE id = ?", (space_id,)).fetchone():
            raise api_error(409, "duplicate_uuid", "this space already exists")
        conn.execute("INSERT INTO spaces (id, owner_device, key_version, enc_label, created_at) VALUES (?, ?, ?, ?, ?)",
                     (space_id, device["id"], key_version, enc_label, clock.now_iso()))
    return space_out(conn, space_row(conn, space_id))


def list_spaces(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM spaces WHERE deleted_at IS NULL ORDER BY created_at").fetchall()
    return [space_out(conn, r) for r in rows]


JOIN_DECISIONS = ("approved", "denied")
JOIN_STATUSES = ("pending", *JOIN_DECISIONS, "expired")
JOIN_REQUEST_TTL_DAYS = 7     # a pending request older than this can no longer be approved (batch-2 review)


def _join_cutoff() -> str:
    return clock.now_iso(clock.now() - timedelta(days=JOIN_REQUEST_TTL_DAYS))


def join_status(row) -> str:
    """The stored status, except that a pending request past its TTL reads as "expired" (reads never write)."""
    if row["status"] == "pending" and row["created_at"] < _join_cutoff():
        return "expired"
    return row["status"]


def join_request_out(conn, row) -> dict:
    dev = conn.execute("SELECT name FROM devices WHERE id = ?", (row["device_id"],)).fetchone()
    return {"id": "jr_" + row["id"], "space": row["space_id"], "device_id": row["device_id"],
            "device_name": dev["name"] if dev else "", "status": join_status(row), "created_at": row["created_at"],
            "decided_at": row["decided_at"], "decided_by": row["decided_by"]}


def request_join(conn, app, *, device, space_id: str) -> tuple[dict | None, dict]:
    """A device asks to take the space over (ruling TIX-J1). Ownership does not change here; the browser
    session approves. Returns (request or None when the device already owns it, space). One pending request
    per device and space: asking again returns the same one and does not push again."""
    created = False
    with _tx(conn):
        space = space_row(conn, space_id)
        if space["owner_device"] == device["id"]:
            return None, space_out(conn, space)
        row = conn.execute("SELECT * FROM space_join_requests WHERE space_id = ? AND device_id = ? AND status = 'pending'",
                           (space_id, device["id"])).fetchone()
        if row is not None and join_status(row) == "expired":      # asking again after 7 days starts over
            conn.execute("UPDATE space_join_requests SET status = 'expired', decided_at = ?, decided_by = 'expiry'"
                         " WHERE id = ?", (clock.now_iso(), row["id"]))
            row = None
        if row is None:
            rid = secrets.token_hex(16)
            conn.execute("INSERT INTO space_join_requests (id, space_id, device_id, from_device, created_at)"
                         " VALUES (?, ?, ?, ?, ?)", (rid, space_id, device["id"], space["owner_device"], clock.now_iso()))
            row = conn.execute("SELECT * FROM space_join_requests WHERE id = ?", (rid,)).fetchone()
            created = True
    if created:
        pending = conn.execute("SELECT COUNT(*) FROM space_join_requests WHERE space_id = ? AND status = 'pending'"
                               " AND created_at >= ?", (space_id, _join_cutoff())).fetchone()[0]
        # "<device> wants to sync <space>": the service worker fills the names in; the payload has none.
        push_v2(app, {"v": 2, "s": space_id, "t": "", "k": "join", "n": pending, "c": attention_total(conn)})
    return join_request_out(conn, row), space_out(conn, space_row(conn, space_id))


def join_request_row(conn, space_id: str, req_id: str):
    row = conn.execute("SELECT * FROM space_join_requests WHERE id = ? AND space_id = ?", (req_id, space_id)).fetchone()
    if row is None:
        raise api_error(404, "not_found", "no such join request")
    return row


def list_join_requests(conn, *, status: str = "pending") -> list[dict]:
    rows = conn.execute("SELECT r.* FROM space_join_requests r JOIN spaces s ON s.id = r.space_id"
                        " WHERE r.status IN ('pending', ?) AND s.deleted_at IS NULL ORDER BY r.created_at",
                        (status,)).fetchall()
    return [out for out in (join_request_out(conn, r) for r in rows) if out["status"] == status]


def decide_join(conn, app, *, space_id: str, req_id: str, decision: str, decided_by: str) -> dict:
    """The browser session approves or denies. Approval moves ownership and bumps owner_gen; mirror_rev is
    kept (final review I1: a reset let the phone see the new owner's rev 1 as a rollback). The new owner's CLI
    pushes above the stored rev. The request row is the audit."""
    now = clock.now_iso()
    with _tx(conn):
        space_row(conn, space_id)
        row = join_request_row(conn, space_id, req_id)
        if row["status"] == decision:
            return join_request_out(conn, row)
        if join_status(row) == "expired":
            raise api_error(409, "expired", f"this request expired after {JOIN_REQUEST_TTL_DAYS} days; the device "
                                            "asks again with `sharing space join`")
        if row["status"] != "pending":
            raise api_error(409, "conflict", f"this request was already {row['status']}")
        if decision == "approved":
            dev = conn.execute("SELECT revoked_at, approved_at FROM devices WHERE id = ?", (row["device_id"],)).fetchone()
            if dev is None or dev["revoked_at"] is not None or dev["approved_at"] is None:
                raise api_error(409, "conflict", "the requesting device is no longer active")
            conn.execute("UPDATE spaces SET owner_device = ?, owner_gen = owner_gen + 1 WHERE id = ?",
                         (row["device_id"], space_id))
        conn.execute("UPDATE space_join_requests SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
                     (decision, now, decided_by, req_id))
    log.info("space %s: join request jr_%s %s (device %s, was owned by %s)", space_id, req_id, decision,
             row["device_id"], row["from_device"])
    # Replace the phone's "<device> wants to sync" notification silently (the SW closes it on "clear").
    push_v2(app, {"v": 2, "s": space_id, "t": "", "k": "clear", "n": 0, "c": attention_total(conn)})
    return join_request_out(conn, join_request_row(conn, space_id, req_id))


def mirror_out(row) -> dict:
    return {"id": format_ticket_id(row["n"]), "n": row["n"], "uuid": row["uuid"], "space": row["space_id"],
            "status": row["status"], "priority": row["priority"], "needs": row["needs"],
            "open_questions": row["open_questions"], "schema_version": row["schema_version"],
            "mirror_rev": row["mirror_rev"], "key_version": row["key_version"], "wrapped_dek": row["wrapped_dek"],
            "enc_content": row["enc_content"], "updated_at": row["updated_at"], "created_at": row["created_at"]}


def writer_of(conn, row) -> dict:
    """Who stored the current snapshot and the space's ownership generation, for the owner's uuid lookup. The CLI
    pushes above `mirror_rev` only when another device wrote it (a takeover); its own older revs stay stale."""
    ev = conn.execute("SELECT actor_id FROM ticket_events WHERE ticket_n = ? AND kind = 'mirror'"
                      " ORDER BY seq DESC LIMIT 1", (row["n"],)).fetchone()
    gen = conn.execute("SELECT owner_gen FROM spaces WHERE id = ?", (row["space_id"],)).fetchone()
    return {"last_writer_device": ev["actor_id"] if ev else None, "owner_gen": gen["owner_gen"] if gen else 0}


def get_mirror_by_uuid(conn, space_id: str, uuid: str):
    return conn.execute("SELECT * FROM tickets WHERE mode = 'mirror' AND space_id = ? AND uuid = ?",
                        (space_id, uuid)).fetchone()


def needs_total(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM tickets WHERE mode = 'mirror' AND deleted_at IS NULL"
                        " AND needs IS NOT NULL").fetchone()[0]


def check_schema(version) -> str:
    m = _SEMVER.fullmatch(version) if isinstance(version, str) else None
    if m is None:
        raise api_error(400, "bad_request", "schema_version must be semver like 1.0.0")
    if m.group(1) not in SCHEMA_MAJORS:
        raise api_error(422, "schema_unsupported",
                        f"this TIX understands orch ticket schema 1.x, not {version}; update TIX")
    return version


def snapshot_event_uuid(mirror_uuid: str, owner_gen: int, rev: int, event_uuid: str) -> str:
    """The stored event uuid of one snapshot. Dedupe is keyed on (event uuid, rev) within one ownership
    generation (ruling TIX-J2): a retry of the same push is a duplicate, while a newer rev that reuses an event
    uuid is a new snapshot."""
    return hashlib.sha256(f"mirror|{mirror_uuid}|{owner_gen}|{rev}|{event_uuid}".encode()).hexdigest()[:32]


def upsert_mirror(conn, app, *, device, uuid: str, body: dict) -> tuple[dict, str | None, str | None]:
    """Insert or replace the sealed snapshot. Returns (result, needs before, needs after) for the push trigger."""
    space_id = body["space"]
    now = clock.now_iso()
    actor = Actor("device", device["id"], device["name"])
    limiter, spent = app.state.mirror_create_limiter, []
    try:
        with _tx(conn):
            n, before, created, ev = _upsert_in_tx(conn, device, space_id, uuid, body, now, actor, limiter, spent,
                                                   app.state.mirror_update_limiter)
    except BaseException:          # a slot counts only once the snapshot is committed (batch-2 review)
        for which in spent:
            which.refund(device["id"])
        raise
    app.state.ticket_bus.bump(ev["seq"])
    return ({"id": format_ticket_id(n), "n": n, "uuid": uuid, "mirror_rev": body["mirror_rev"], "created": created},
            before, body["needs"])


def _upsert_in_tx(conn, device, space_id: str, uuid: str, body: dict, now: str, actor, limiter, spent: list,
                  update_limiter=None):
    """upsert_mirror inside its transaction: (n, needs before, created, event). A create takes a limiter
    slot and notes it in `spent`, so the caller refunds it if anything later fails."""
    space = owner_space(conn, space_id, device)
    row = get_mirror_by_uuid(conn, space_id, uuid)
    if row is not None and row["deleted_at"] is not None:
        raise api_error(410, "gone", f"{format_ticket_id(row['n'])} was unlinked; linking it again uses a new"
                                     " link generation, so a new mirror uuid and TIX id")
    ev_uuid = snapshot_event_uuid(uuid, space["owner_gen"], body["mirror_rev"], body["event_uuid"])
    if row is not None and body["mirror_rev"] <= (row["mirror_rev"] or 0):
        if conn.execute("SELECT 1 FROM ticket_events WHERE uuid = ?", (ev_uuid,)).fetchone():
            raise api_error(409, "duplicate_uuid", "this snapshot was already stored")
        raise api_error(409, "stale_rev", "a newer snapshot is already stored")
    if row is None:
        if not body.get("wrapped_dek"):
            raise api_error(400, "bad_request", "the first snapshot of a mirror carries wrapped_dek")
        if not limiter.allow(device["id"]):
            raise api_error(429, "rate_limited", f"at most {MIRROR_CREATES_PER_HOUR} new mirrors per hour per device")
        spent.append(limiter)
        if conn.execute("SELECT 1 FROM tickets WHERE uuid = ?", (uuid,)).fetchone():
            raise api_error(409, "uuid_taken", "this uuid belongs to another ticket (another space or a"
                                               " legacy ticket)")
        cur = conn.execute(
            "INSERT INTO tickets (uuid, key_version, wrapped_dek, enc_content, status, project, priority,"
            " created_by_device, created_by_name, open_questions, created_at, updated_at, mode, space_id,"
            " schema_version, mirror_rev, needs) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'mirror', ?, ?, ?, ?)",
            (uuid, body["key_version"], body["wrapped_dek"], body["enc_content"], body["status"], device["project"],
             body["priority"], device["id"], device["name"], body["open_questions"], now, now, space_id,
             body["schema_version"], body["mirror_rev"], body["needs"]))
        n, before, created = cur.lastrowid, None, True
    else:
        if body["key_version"] != row["key_version"] or (body.get("wrapped_dek") is not None
                                                         and body["wrapped_dek"] != row["wrapped_dek"]):
            raise api_error(409, "dek_mismatch", "an update keeps the mirror's wrapped_dek and key_version;"
                                                 " read them with GET /api/mirrors/u/{uuid}")
        if update_limiter is not None:
            if not update_limiter.allow(device["id"]):
                raise api_error(429, "rate_limited",
                                f"at most {MIRROR_UPDATES_PER_HOUR} mirror updates per hour per device")
            spent.append(update_limiter)
        conn.execute("UPDATE tickets SET enc_content = ?, status = ?, priority = ?, needs = ?, open_questions = ?,"
                     " schema_version = ?, mirror_rev = ?, updated_at = ?, rev = rev + 1 WHERE n = ?",
                     (body["enc_content"], body["status"], body["priority"], body["needs"], body["open_questions"],
                      body["schema_version"], body["mirror_rev"], now, row["n"]))
        n, before, created = row["n"], row["needs"], False
    ev = _write_event(conn, n, uuid=ev_uuid, kind="mirror", actor=actor, status_from=None,
                      status_to=None, enc_body=None, now=now)
    return n, before, created, ev


def unlink_mirror(conn, app, *, device, space_id: str, uuid: str) -> tuple[int, str | None]:
    now = clock.now_iso()
    actor = Actor("device", device["id"], device["name"])
    with _tx(conn):
        owner_space(conn, space_id, device)
        row = get_mirror_by_uuid(conn, space_id, uuid)
        if row is None or row["deleted_at"] is not None:
            raise api_error(404, "not_found", "no such mirror")
        conn.execute("UPDATE tickets SET wrapped_dek = NULL, enc_content = NULL, needs = NULL, deleted_at = ?,"
                     " updated_at = ? WHERE n = ?", (now, now, row["n"]))
        conn.execute("UPDATE decisions SET ack = 'unlinked', ack_at = ? WHERE ticket_n = ? AND (ack IS NULL OR ack LIKE"
                     " 'waiting-%')", (now, row["n"]))
        ev = _write_event(conn, row["n"], uuid=hashlib.sha256(f"unlink|{uuid}".encode()).hexdigest()[:32],
                          kind="unlink", actor=actor, status_from=None, status_to=None, enc_body=None, now=now)
    app.state.ticket_bus.bump(ev["seq"])
    return row["n"], row["needs"]


def list_mirrors(conn, space_id: str | None = None) -> list[dict]:
    q = "SELECT * FROM tickets WHERE mode = 'mirror' AND deleted_at IS NULL"
    args: tuple = ()
    if space_id is not None:
        q, args = q + " AND space_id = ?", (space_id,)
    return [mirror_out(r) for r in conn.execute(q + " ORDER BY updated_at DESC", args).fetchall()]


def mirror_changes_after(conn, after: int, limit: int = 200) -> tuple[list[dict], int]:
    rows = conn.execute("SELECT e.seq, e.ticket_n FROM ticket_events e JOIN tickets t ON t.n = e.ticket_n"
                        " WHERE e.seq > ? AND t.mode = 'mirror' ORDER BY e.seq LIMIT ?", (after, limit)).fetchall()
    if not rows:
        return [], after
    ns = list(dict.fromkeys(r["ticket_n"] for r in rows))
    out = [mirror_out(r) | {"deleted": r["deleted_at"] is not None}
           for r in conn.execute(f"SELECT * FROM tickets WHERE n IN ({','.join('?' * len(ns))})", ns).fetchall()]
    return out, rows[-1]["seq"]


def human_messages_open(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM messages m WHERE m.to_kind = 'human' AND NOT EXISTS"
                        " (SELECT 1 FROM message_acks a WHERE a.message_seq = m.seq AND a.recipient = 'human')"
                        ).fetchone()[0]


def join_requests_open(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM space_join_requests r JOIN spaces s ON s.id = r.space_id"
                        " WHERE r.status = 'pending' AND r.created_at >= ? AND s.deleted_at IS NULL",
                        (_join_cutoff(),)).fetchone()[0]


def attention_total(conn) -> int:
    """`c` in push v2: mirrors that need the human, unread messages to the human and pending join requests
    (ruling TIX-M1), across spaces."""
    return needs_total(conn) + human_messages_open(conn) + join_requests_open(conn)


def push_v2(app, payload: dict) -> None:
    try:
        app.state.pusher.notify_payload(payload)
    except Exception as e:      # a push never fails the request (spec T7)
        log.warning("push: notify failed: %s", e)


NEEDS_PUSH_EVERY_S = 60
BATCH_TICKETS_MAX = 20


def _daemon_timer(delay: float, fn) -> None:
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


class NeedsPushGate:
    """At most one needs push per workspace every `every_s` seconds (feedback round B: an agent that makes ten
    tickets need you at once sends one push, not ten). In memory, one process. The first transition after a quiet
    window is pushed at once; the ones inside the window are held (their TIX ids) and one timer per workspace sends
    them when it ends: one ticket as its own push, more as one "batch" push. A clear is never held (it replaces the
    notification silently) and drops its ticket from the held ones. `timer` is replaceable in tests."""

    MAX_TRACKED = 10_000

    def __init__(self, every_s: int = NEEDS_PUSH_EVERY_S):
        self.every_s = every_s
        self._last: dict[str, float] = {}
        self._held: dict[str, list[str]] = {}
        # (space, ticket) pairs the phone was told about / was NOT told about yet (held in a window): a clear for a
        # ticket in the second set is pointless and would show "Handled on desktop" for something never shown.
        self._shown: set[tuple[str, str]] = set()
        self._unshown: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self.timer = _daemon_timer

    def mark_shown(self, space: str, ticket: str) -> None:
        with self._lock:
            if len(self._shown) >= self.MAX_TRACKED:
                self._shown.clear()
            self._shown.add((space, ticket))
            self._unshown.discard((space, ticket))

    def clear_wanted(self, space: str, ticket: str) -> bool:
        """The ticket stopped needing you: False only when its needs push is still held (the phone never saw it)."""
        with self._lock:
            key = (space, ticket)
            self._shown.discard(key)
            held = ticket in self._held.get(space, [])
            if held:
                self._held[space].remove(ticket)
            was_unshown = key in self._unshown
            self._unshown.discard(key)
            return not (held and was_unshown)

    def due(self, space: str, ticket: str, flush) -> bool:
        """True: push this transition now. False: it is held; `flush(space, tickets)` runs when the window ends."""
        now = clock.now().timestamp()
        with self._lock:
            last = self._last.get(space)
            if last is not None and now - last < self.every_s:
                held = self._held.get(space)
                if held is None:
                    held = self._held[space] = []
                    self.timer(self.every_s - (now - last), lambda: self._trail(space, flush))
                if ticket in held:
                    held.remove(ticket)
                held.append(ticket)
                if (space, ticket) not in self._shown:
                    if len(self._unshown) >= self.MAX_TRACKED:
                        self._unshown.clear()
                    self._unshown.add((space, ticket))
                return False
            if len(self._last) >= self.MAX_TRACKED:
                self._last = {k: v for k, v in self._last.items() if now - v < self.every_s}
            self._last[space] = now
            return True

    def drop(self, space: str, ticket: str) -> None:
        with self._lock:
            if ticket in self._held.get(space, []):
                self._held[space].remove(ticket)

    def _trail(self, space: str, flush) -> None:
        with self._lock:
            held = self._held.pop(space, None)
            if held:
                self._last[space] = clock.now().timestamp()     # the summary opens the next window
        if not held:
            return
        try:
            flush(space, held)
        except Exception as e:      # a push never fails anything
            log.warning("push: summary failed: %s", e)


def _flush_held(app, space: str, held: list[str]) -> None:
    """The end of a workspace's window: the held tickets that still need the human, on a connection of its own."""
    from fileshare import db
    c = db.connect(app.state.settings.db_path)
    try:
        live = []
        for t in held:
            n = int(t.removeprefix("TIX-"))
            row = c.execute("SELECT needs, open_questions FROM tickets WHERE n = ? AND space_id = ? AND mode = 'mirror'"
                            " AND deleted_at IS NULL AND needs IS NOT NULL", (n, space)).fetchone()
            if row is not None:
                live.append((t, row["needs"], row["open_questions"]))
        if not live:
            return
        total = attention_total(c)
        gate = getattr(app.state, "needs_push_gate", None)
        for t, _, _ in live:
            if gate is not None:
                gate.mark_shown(space, t)
        if len(live) == 1:
            t, needs, oq = live[0]
            push_v2(app, {"v": 2, "s": space, "t": t, "k": needs, "n": oq if needs == "question" else 0, "c": total})
        else:
            push_v2(app, {"v": 2, "s": space, "t": "", "k": "batch", "n": len(live), "c": total,
                          "ts": [t for t, _, _ in live][-BATCH_TICKETS_MAX:]})
    finally:
        c.close()


def after_needs_change(conn, app, *, space, ticket, before, after, open_questions) -> None:
    """Push only when a mirror starts needing the human or needs something else; replace it when it stops.
    The payload is cleartext routing only (TIX spec §7): no title, text, client name or local key. Needs pushes
    are coalesced per workspace (NeedsPushGate); a clear goes out at once."""
    if before == after:
        return
    gate = getattr(app.state, "needs_push_gate", None)
    if after is None:
        if gate is not None and not gate.clear_wanted(space, ticket):
            return          # its "needs you" never left the server (held in the window): nothing to withdraw
        push_v2(app, {"v": 2, "s": space, "t": ticket, "k": "clear", "n": 0, "c": attention_total(conn)})
        return
    if gate is not None and not gate.due(space, ticket, lambda sp, held: _flush_held(app, sp, held)):
        return
    if gate is not None:
        gate.mark_shown(space, ticket)
    push_v2(app, {"v": 2, "s": space, "t": ticket, "k": after, "n": open_questions,
                  "c": attention_total(conn)})
