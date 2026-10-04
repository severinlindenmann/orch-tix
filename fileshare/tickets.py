"""Tickets: the domain (spec T2, T4–T6). Request shapes are checked in routes/tickets.py; this module
owns the move matrix, claims, the event log and every write, each in one transaction.

The server never decrypts: enc_content and enc_body are opaque. The only facts it acts on are the
cleartext columns and the cleartext request flags (question_count, passed, manual, verdict).
"""
import asyncio
import hmac
import json
import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from starlette.concurrency import run_in_threadpool

from fileshare import clock
from fileshare.deps import api_error
from fileshare.ids import format_id, format_ticket_id, parse_ref, parse_ticket_ref
from fileshare.security import sha256_hex
from fileshare.sessions import session_name

log = logging.getLogger("fileshare.tickets")

STATUSES = ("backlog", "open", "in-progress", "waiting", "testing", "done")
KINDS = ("created", "update", "question", "answer", "test", "verdict", "status", "claim", "edit", "release",
         "mirror", "unlink", "ack")   # mirror/unlink/ack: mirrored tickets (fileshare.mirrors)
POSTABLE_KINDS = ("update", "question", "answer", "test", "verdict", "status")   # POST …/events
CREATE_STATUSES = ("backlog", "open")
TYPES = ("feature", "bug", "chore", "spike")
PRIORITIES = ("low", "normal", "high", "urgent")
VERDICTS = ("done", "follow-up")
HOLDER_STATUSES = ("in-progress", "waiting", "testing")   # a claim may be taken over only here
PUSH_STATUSES = ("waiting", "testing")

CLAIM_HEADER = "X-Claim-Token"
LISTENING_S = 90            # claim.state: poll_at younger than this
WORKING_S = 30 * 60         # claim.state: seen_at younger than this

POLL_STEP_S = 0.5           # long-poll loop step (spec T6)
MAX_WAIT_S = 30             # the largest accepted ?wait=
MAX_POLLS_PER_DEVICE = 20   # concurrent long-polls per device, else 429
POLL_LIMIT = 200            # events per long-poll answer; the client re-polls from the cursor


# --- process-wide state ----------------------------------------------------------------------

class Bus:
    """The highest event seq committed in this process (one uvicorn worker, spec T6).
    Long-polls (Task 3) compare it with their cursor before touching the DB."""

    def __init__(self, last_seq: int = 0):
        self.last_seq = last_seq
        self._lock = threading.Lock()

    def bump(self, seq: int) -> None:
        with self._lock:
            if seq > self.last_seq:
                self.last_seq = seq


_polls: dict[str, int] = {}
_polls_lock = threading.Lock()


def poll_count(device_id: str) -> int:
    """How many long-polls this device has in flight (one uvicorn worker, so one process)."""
    with _polls_lock:
        return _polls.get(device_id, 0)


@contextmanager
def poll_slot(device_id: str | None) -> Iterator[None]:
    """Hold one of the device's MAX_POLLS_PER_DEVICE long-poll slots (429 when all are taken).
    The slot is given back however the poll ends: answered, failed, disconnected or cancelled
    (Review Focus 3). Session polls (device_id None) aren't counted."""
    if device_id is None:
        yield
        return
    with _polls_lock:
        if _polls.get(device_id, 0) >= MAX_POLLS_PER_DEVICE:
            raise api_error(429, "rate_limited", f"at most {MAX_POLLS_PER_DEVICE} concurrent long-polls per device")
        _polls[device_id] = _polls.get(device_id, 0) + 1
    try:
        yield
    finally:
        with _polls_lock:
            left = _polls.get(device_id, 1) - 1
            if left > 0:
                _polls[device_id] = left
            else:
                _polls.pop(device_id, None)


async def wait_for_events(request, bus: Bus, after: int, wait: int, fetch: Callable[[], list]) -> list:
    """The long-poll loop (spec T6, "Waiting inside the server"). No DB connection is held while
    sleeping: `fetch` (sync; it opens and closes its own connection, in the threadpool) runs only
    when the bus has moved past what was last checked. Returns fetch's first non-empty result,
    or [] at the deadline or when the client has gone away."""
    deadline = time.monotonic() + wait
    checked = after
    while True:
        seen = bus.last_seq
        if seen > checked:
            found = await run_in_threadpool(fetch)
            if found:
                return found
            checked = seen          # nothing of ours up to `seen`: wait for the bus to move again
        remaining = deadline - time.monotonic()
        if remaining <= 0 or await request.is_disconnected():
            return []
        await asyncio.sleep(min(POLL_STEP_S, remaining))


class NullPusher:
    """The default app.state.pusher: sends nothing (Task 4 installs the Web Push one)."""

    def notify(self, ticket: dict, event: dict) -> None:
        pass

    def notify_payload(self, payload: dict) -> None:
        pass


# --- actors -------------------------------------------------------------------------------------

@dataclass
class Actor:
    kind: Literal["device", "web"]
    device_id: str | None
    name: str
    holder: bool = False

    def out(self) -> dict:
        return {"kind": self.kind, "id": self.device_id, "name": self.name}


def principal_actor(conn: sqlite3.Connection, principal) -> Actor:
    """The actor for a request that concerns no ticket's claim (create, list)."""
    if principal.kind == "session":
        return Actor("web", None, session_name(conn, principal.session_hash) or "browser")
    return Actor("device", principal.device["id"], principal.device["name"])


def actor_for(request, principal, conn: sqlite3.Connection, ticket_row) -> Actor:
    """S, D or H. A device sending X-Claim-Token must hold the ticket's live claim (else 409
    claim_lost); a valid token marks it as the holder and refreshes claim_seen_at (spec T6)."""
    actor = principal_actor(conn, principal)
    if actor.kind == "web":
        return actor
    token = request.headers.get(CLAIM_HEADER)
    if token is None:
        return actor
    stored = ticket_row["claim_token_hash"]
    if (not stored or ticket_row["claim_device_id"] != actor.device_id
            or not hmac.compare_digest(stored, sha256_hex(token))):
        raise api_error(409, "claim_lost", f"this device no longer holds the claim on "
                                           f"{format_ticket_id(ticket_row['n'])}")
    conn.execute("UPDATE tickets SET claim_seen_at = ? WHERE n = ? AND claim_token_hash = ?",
                 (clock.now_iso(), ticket_row["n"], stored))
    actor.holder = True
    return actor


def new_claim_token() -> str:
    return "clm_" + secrets.token_urlsafe(32)


# --- output ----------------------------------------------------------------------------------------

def _qs(xs: Sequence) -> str:
    return ",".join("?" * len(xs))


def _claim_state(row, now: datetime) -> str:
    def age(ts):
        return None if ts is None else (now - clock.parse_iso(ts)).total_seconds()
    poll, seen = age(row["claim_poll_at"]), age(row["claim_seen_at"])
    if poll is not None and poll < LISTENING_S:
        return "listening"
    if seen is not None and seen < WORKING_S:
        return "working"
    return "gone"


def tickets_out(conn: sqlite3.Connection, rows: Sequence, now: datetime | None = None) -> list[dict]:
    """TicketOut (spec T6) for many rows, with a fixed number of queries (no N+1)."""
    now = now or clock.now()
    ns = [r["n"] for r in rows]
    labels: dict[int, list[str]] = {}
    blocks: dict[int, list[str]] = {}
    files: dict[int, list[str]] = {}
    last_seq: dict[int, int] = {}
    names: dict[str, str] = {}
    if ns:
        q = _qs(ns)
        for r in conn.execute(f"SELECT ticket_n, label FROM ticket_labels WHERE ticket_n IN ({q})"
                              " ORDER BY ticket_n, label", ns):
            labels.setdefault(r[0], []).append(r[1])
        for r in conn.execute(f"SELECT ticket_n, blocked_by_n FROM ticket_blocks WHERE ticket_n IN ({q})"
                              " ORDER BY ticket_n, blocked_by_n", ns):
            blocks.setdefault(r[0], []).append(format_ticket_id(r[1]))
        for r in conn.execute(f"SELECT ticket_n, file_n FROM ticket_files WHERE ticket_n IN ({q})"
                              " ORDER BY ticket_n, file_n", ns):
            files.setdefault(r[0], []).append(format_id(r[1]))
        for r in conn.execute(f"SELECT ticket_n, MAX(seq) FROM ticket_events WHERE ticket_n IN ({q})"
                              " GROUP BY ticket_n", ns):
            last_seq[r[0]] = r[1]
        dev_ids = sorted({r["claim_device_id"] for r in rows if r["claim_device_id"]})
        if dev_ids:
            names = {r[0]: r[1] for r in conn.execute(
                f"SELECT id, name FROM devices WHERE id IN ({_qs(dev_ids)})", dev_ids)}
    out = []
    for r in rows:
        n = r["n"]
        claim = None
        if r["claim_device_id"]:
            claim = {"device": {"id": r["claim_device_id"], "name": names.get(r["claim_device_id"])},
                     "state": _claim_state(r, now), "seen_at": r["claim_seen_at"], "poll_at": r["claim_poll_at"]}
        out.append({
            "id": format_ticket_id(n),
            "n": n,
            "uuid": r["uuid"],
            "key_version": r["key_version"],
            "wrapped_dek": r["wrapped_dek"],
            "enc_content": r["enc_content"],
            "status": r["status"],
            "project": r["project"],
            "type": r["type"],
            "priority": r["priority"],
            "labels": labels.get(n, []),
            "due": r["due"],
            "parent": None if r["parent_n"] is None else format_ticket_id(r["parent_n"]),
            "blocked_by": blocks.get(n, []),
            "created_by": {"kind": "device" if r["created_by_device"] else "web",
                           "id": r["created_by_device"], "name": r["created_by_name"]},
            "opened_by": r["opened_by_name"],
            "rev": r["rev"],
            "open_questions": r["open_questions"],
            "files": files.get(n, []),
            "claim": claim,
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "deleted_at": r["deleted_at"],       # set only on a tombstone (Task 3's /changes emits those)
            "archived_at": r["archived_at"],     # migrated to orch-core: read-only, tombstoned 90 days later
            "last_seq": last_seq.get(n, 0),
        })
    return out


def ticket_out(conn: sqlite3.Connection, row, now: datetime | None = None) -> dict:
    return tickets_out(conn, [row], now)[0]


def event_out(conn: sqlite3.Connection, row) -> dict:
    """EventOut (spec T5). `files` are the FILE ids this event linked, kept in ticket_events.files."""
    return {
        "seq": row["seq"],
        "ticket": format_ticket_id(row["ticket_n"]),
        "uuid": row["uuid"],
        "kind": row["kind"],
        "actor": {"kind": row["actor_kind"], "id": row["actor_id"], "name": row["actor_name"]},
        "status_from": row["status_from"],
        "status_to": row["status_to"],
        "created_at": row["created_at"],
        "files": json.loads(row["files"]),
        "enc_body": row["enc_body"],
    }


def events_of(conn: sqlite3.Connection, n: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM ticket_events WHERE ticket_n = ? ORDER BY seq", (n,)).fetchall()
    return [event_out(conn, r) for r in rows]


def events_after(conn: sqlite3.Connection, n: int, after: int, limit: int = POLL_LIMIT) -> list[dict]:
    """This ticket's events with seq > after, ascending, at most `limit` (the ticket long-poll)."""
    rows = conn.execute("SELECT * FROM ticket_events WHERE ticket_n = ? AND seq > ? ORDER BY seq LIMIT ?",
                        (n, after, limit)).fetchall()
    return [event_out(conn, r) for r in rows]


def changes_after(conn: sqlite3.Connection, after: int, limit: int = POLL_LIMIT) -> tuple[list[dict], int]:
    """The board long-poll: (TicketOut of every ticket with an event in the next `limit` events
    after `after`, tombstones included so the board drops them; the cursor, the last seq read)."""
    evs = conn.execute("SELECT e.seq, e.ticket_n FROM ticket_events e JOIN tickets t ON t.n = e.ticket_n"
                       " WHERE e.seq > ? AND t.mode = 'legacy' ORDER BY e.seq LIMIT ?", (after, limit)).fetchall()
    if not evs:
        return [], after
    ns = list(dict.fromkeys(r["ticket_n"] for r in evs))
    rows = conn.execute(f"SELECT * FROM tickets WHERE n IN ({_qs(ns)}) ORDER BY updated_at DESC, n DESC",
                        ns).fetchall()
    return tickets_out(conn, rows), evs[-1]["seq"]


def attention_count(conn: sqlite3.Connection) -> int:
    """Live tickets in waiting or testing (the nav badge)."""
    return conn.execute(f"SELECT COUNT(*) FROM tickets WHERE mode = 'legacy' AND deleted_at IS NULL"
                        f" AND status IN ({_qs(PUSH_STATUSES)})",
                        PUSH_STATUSES).fetchone()[0]


def mark_polling(conn: sqlite3.Connection, ticket_row) -> None:
    """The holder is long-polling: claim.state reads `listening` for LISTENING_S (spec T6)."""
    conn.execute("UPDATE tickets SET claim_poll_at = ? WHERE n = ? AND claim_token_hash = ?",
                 (clock.now_iso(), ticket_row["n"], ticket_row["claim_token_hash"]))


def ticket_out_n(conn: sqlite3.Connection, n: int) -> dict:
    """TicketOut by number, a tombstone included (a ticket deleted while someone polled it)."""
    return ticket_out(conn, _fetch(conn, n))


# --- loading ------------------------------------------------------------------------------------------

def _fetch(conn: sqlite3.Connection, n: int):
    # Legacy tickets only: a mirror row (fileshare.mirrors) is 404 to every legacy route.
    return conn.execute("SELECT * FROM tickets WHERE n = ? AND mode = 'legacy'", (n,)).fetchone()


def _gone(n: int):
    e = api_error(410, "deleted", f"{format_ticket_id(n)} was deleted")
    e.detail["ticket"] = format_ticket_id(n)
    return e


def load_ticket(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """The ticket row for any accepted ref form; 404 not_found, or 410 deleted for a tombstone."""
    n = parse_ticket_ref(ref)
    row = None if n is None else _fetch(conn, n)
    if row is None:
        raise api_error(404, "not_found", f"no ticket {ref}")
    if row["deleted_at"] is not None:
        raise _gone(n)
    return row


def list_tickets(conn: sqlite3.Connection, *, statuses: Sequence[str] = (), project: str | None = None,
                 labels: Sequence[str] = (), limit: int = 500) -> list[dict]:
    """Newest updated_at first, tombstones excluded; every given label must match."""
    sql = "SELECT * FROM tickets t WHERE t.mode = 'legacy' AND t.deleted_at IS NULL"
    params: list = []
    if statuses:
        sql += f" AND t.status IN ({_qs(statuses)})"
        params += list(statuses)
    if project is not None:
        sql += " AND t.project = ?"
        params.append(project)
    for label in labels:
        sql += " AND EXISTS (SELECT 1 FROM ticket_labels l WHERE l.ticket_n = t.n AND l.label = ?)"
        params.append(label)
    rows = conn.execute(sql + " ORDER BY t.updated_at DESC, t.n DESC LIMIT ?", (*params, limit)).fetchall()
    return tickets_out(conn, rows)


# --- the move matrix (spec T2) ------------------------------------------------------------------------

def check_move(actor: Actor, ticket_row, kind: str, status_to: str | None, body_flags: dict) -> str | None:
    """The status this event moves the ticket to, or None when it stays put. Raises 409 bad_move
    for anything outside T2. A given `status_to` must equal the result (or the current status).
    body_flags carries the validated cleartext flags: passed/manual (test), verdict (verdict)."""
    cur = ticket_row["status"]
    web = actor.kind == "web"

    def refuse(why: str):
        return api_error(409, "bad_move", f"{kind} from {cur}: {why}")

    if status_to is not None and status_to not in STATUSES:
        raise api_error(400, "bad_request", "status_to must be one of " + ", ".join(STATUSES))
    if kind == "update":
        new = None                                  # anyone, any status
    elif kind == "question":
        if not actor.holder:
            raise refuse("only the claim holder asks questions")
        if cur != "in-progress":
            raise refuse("questions are asked from in-progress")
        new = "waiting"
    elif kind == "test":
        if not actor.holder:
            raise refuse("only the claim holder posts test results")
        if cur != "in-progress":
            raise refuse("tests are reported from in-progress")
        new = "testing" if body_flags["passed"] or body_flags["manual"] else None
    elif kind == "answer":
        if not web:
            raise refuse("only the browser answers questions")
        if cur != "waiting":
            raise refuse("answers need a waiting ticket")
        new = "in-progress"
    elif kind == "verdict":
        if not web:
            raise refuse("only the browser gives a verdict")
        if cur != "testing":
            raise refuse("verdicts need a ticket in testing")
        new = "done" if body_flags["verdict"] == "done" else "in-progress"
    elif kind == "status":
        if status_to is None:
            raise api_error(400, "bad_request", "a status event needs status_to")
        if status_to == cur:
            raise refuse("the ticket is already there")
        if web:
            return status_to                        # the manual override: any -> any other
        if {cur, status_to} == {"backlog", "open"}:
            return status_to
        if actor.holder and cur == "testing" and status_to == "done":
            return status_to
        raise refuse(f"a device can't move it to {status_to}")
    else:
        raise api_error(400, "bad_request", f"{kind} events can't be posted")
    if status_to is not None and status_to != (new or cur):
        raise refuse(f"this event moves it to {new or cur}, not {status_to}")
    return new


# --- writes --------------------------------------------------------------------------------------------

@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _dup(what: str):
    return api_error(409, "duplicate_uuid", f"{what} with this uuid already exists")


def check_new_event_uuid(conn: sqlite3.Connection, uuid: str) -> None:
    """409 duplicate_uuid for a retry. Routes call it before judging the move, so a retried event
    whose first attempt already moved the ticket is a duplicate, not a bad_move (Review Focus 4)."""
    if conn.execute("SELECT 1 FROM ticket_events WHERE uuid = ?", (uuid,)).fetchone():
        raise _dup("an event")


def _live_ticket_ns(conn: sqlite3.Connection, ns: Sequence[int]) -> None:
    ns = list(dict.fromkeys(ns))
    if not ns:
        return
    found = {r[0] for r in conn.execute(
        f"SELECT n FROM tickets WHERE mode = 'legacy' AND deleted_at IS NULL AND n IN ({_qs(ns)})", ns)}
    missing = [format_ticket_id(n) for n in ns if n not in found]
    if missing:
        raise api_error(400, "bad_ref", "no such ticket: " + ", ".join(missing))


def _live_file_ns(conn: sqlite3.Connection, files: Sequence[str]) -> list[int]:
    ns = []
    for f in files:
        n = parse_ref(f)
        row = None if n is None else conn.execute(
            "SELECT 1 FROM files WHERE n = ? AND deleted_at IS NULL", (n,)).fetchone()
        if row is None:
            raise api_error(400, "bad_ref", f"no live file {f}")
        ns.append(n)
    return list(dict.fromkeys(ns))


def _write_event(conn, n: int, *, uuid: str, kind: str, actor: Actor, status_from: str | None,
                 status_to: str | None, enc_body: str | None, now: str, files: Sequence[str] = ()):
    """INSERT one event. The caller holds the transaction."""
    if conn.execute("SELECT 1 FROM ticket_events WHERE uuid = ?", (uuid,)).fetchone():
        raise _dup("an event")
    cur = conn.execute(
        "INSERT INTO ticket_events (ticket_n, uuid, kind, actor_kind, actor_id, actor_name,"
        " status_from, status_to, enc_body, files, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (n, uuid, kind, actor.kind, actor.device_id, actor.name, status_from, status_to, enc_body,
         json.dumps(list(files)), now))
    return conn.execute("SELECT * FROM ticket_events WHERE seq = ?", (cur.lastrowid,)).fetchone()


def _should_push(actor: Actor, status_to: str | None) -> bool:
    """Waiting and testing push only when a device moved it there: Severin's own browser moves
    never notify his phone. H closing a ticket (testing -> done) pushes too (spec T7)."""
    return (actor.kind == "device" and status_to in PUSH_STATUSES) or (actor.holder and status_to == "done")


def _committed(conn, app, n: int, ev_row, actor: Actor, status_to: str | None, manual: bool = False) -> dict:
    """After COMMIT: wake long-polls, maybe push. Returns EventOut. `manual` (a manual test's
    cleartext flag) reaches the pusher only, never EventOut."""
    app.state.ticket_bus.bump(ev_row["seq"])
    ev = event_out(conn, ev_row)
    if _should_push(actor, status_to):
        try:
            app.state.pusher.notify(ticket_out(conn, _fetch(conn, n)), {**ev, "manual": True} if manual else ev)
        except Exception as e:     # a push must never fail the request (spec T7)
            log.warning("push: notify failed: %s", e)
    return ev


_CLEAR_CLAIM = "claim_device_id = NULL, claim_token_hash = NULL, claim_seen_at = NULL, claim_poll_at = NULL"


def insert_event(conn: sqlite3.Connection, app, ticket_row, *, uuid: str, kind: str, actor: Actor,
                 status_to: str | None, enc_body: str | None, files: Sequence[str],
                 question_count: int | None = None, claim: tuple[str, str] | None = None,
                 manual: bool = False) -> dict:
    """Write one event and its effects in one transaction, then bump the bus and (T7) push.

    `status_to` is check_move's result: the new status, or None to keep the current one (then the
    event's status_from/status_to are both NULL). `files` are FILE ids to link. `claim` is
    (device_id, token_hash) for a claim event. The ticket must still be as `ticket_row` saw it
    (status, and claim for holder/claim/release events), else 409 conflict (or claim_lost for H).
    `manual` is a test event's cleartext flag, handed to the pusher (spec T7 `m`)."""
    n = ticket_row["n"]
    now = clock.now_iso()
    with _tx(conn):
        fresh = _fetch(conn, n)
        if fresh["deleted_at"] is not None:
            raise _gone(n)
        if conn.execute("SELECT 1 FROM ticket_events WHERE uuid = ?", (uuid,)).fetchone():
            raise _dup("an event")
        if fresh["claim_token_hash"] != ticket_row["claim_token_hash"]:
            if actor.holder:
                raise api_error(409, "claim_lost", f"the claim on {format_ticket_id(n)} was taken over")
            if kind in ("claim", "release"):
                raise api_error(409, "conflict", "the claim changed meanwhile; reload and retry")
        if kind != "update" and fresh["status"] != ticket_row["status"]:
            raise api_error(409, "conflict", "the ticket changed meanwhile; reload and retry")
        file_ns = _live_file_ns(conn, files)
        status_from = fresh["status"] if status_to is not None else None
        ev_row = _write_event(conn, n, uuid=uuid, kind=kind, actor=actor, status_from=status_from,
                              status_to=status_to, enc_body=enc_body, now=now,
                              files=[format_id(f) for f in file_ns])
        sets, params = ["updated_at = ?"], [now]
        if status_to is not None:
            sets.append("status = ?")
            params.append(status_to)
            if status_to == "open":
                sets.append("opened_by_name = ?")
                params.append(actor.name)
        if kind == "question":
            sets.append("open_questions = ?")
            params.append(question_count)
        elif kind == "answer":
            sets.append("open_questions = 0")
        if status_to == "done" or kind == "release":
            sets.append(_CLEAR_CLAIM)
        elif claim is not None:
            sets.append("claim_device_id = ?, claim_token_hash = ?, claim_seen_at = ?, claim_poll_at = NULL")
            params += [claim[0], claim[1], now]
        conn.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE n = ?", (*params, n))
        conn.executemany("INSERT OR IGNORE INTO ticket_files (ticket_n, file_n) VALUES (?, ?)",
                         [(n, f) for f in file_ns])
    return _committed(conn, app, n, ev_row, actor, status_to, manual)


def create_ticket(conn: sqlite3.Connection, app, *, actor: Actor, uuid: str, key_version: int,
                  wrapped_dek: str, enc_content: str, status: str, project: str, type: str,
                  priority: str, labels: Sequence[str], due: str | None, parent: int | None,
                  blocked_by: Sequence[int], event_uuid: str, files: Sequence[str] = ()) -> dict:
    """Insert the ticket, its labels and blockers, and its `created` event (linking `files`, FILE ids).
    Returns TicketOut."""
    if status not in CREATE_STATUSES:
        raise api_error(409, "bad_move", "tickets are created in backlog or open")
    now = clock.now_iso()
    with _tx(conn):
        if conn.execute("SELECT 1 FROM tickets WHERE uuid = ?", (uuid,)).fetchone():
            raise _dup("a ticket")
        if conn.execute("SELECT 1 FROM ticket_events WHERE uuid = ?", (event_uuid,)).fetchone():
            raise _dup("an event")
        _live_ticket_ns(conn, [*([parent] if parent is not None else []), *blocked_by])
        file_ns = _live_file_ns(conn, files)
        cur = conn.execute(
            "INSERT INTO tickets (uuid, key_version, wrapped_dek, enc_content, status, project, type, priority,"
            " due, parent_n, created_by_device, created_by_name, opened_by_name, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid, key_version, wrapped_dek, enc_content, status, project, type, priority, due, parent,
             actor.device_id, actor.name, actor.name if status == "open" else None, now, now))
        n = cur.lastrowid
        _set_links(conn, n, labels, blocked_by)
        ev_row = _write_event(conn, n, uuid=event_uuid, kind="created", actor=actor, status_from=None,
                              status_to=status, enc_body=None, now=now, files=[format_id(f) for f in file_ns])
        conn.executemany("INSERT OR IGNORE INTO ticket_files (ticket_n, file_n) VALUES (?, ?)",
                         [(n, f) for f in file_ns])
    _committed(conn, app, n, ev_row, actor, status)
    return ticket_out(conn, _fetch(conn, n))


def _set_links(conn, n: int, labels: Sequence[str] | None, blocked_by: Sequence[int] | None) -> None:
    if labels is not None:
        conn.execute("DELETE FROM ticket_labels WHERE ticket_n = ?", (n,))
        conn.executemany("INSERT INTO ticket_labels (ticket_n, label) VALUES (?, ?)", [(n, x) for x in labels])
    if blocked_by is not None:
        conn.execute("DELETE FROM ticket_blocks WHERE ticket_n = ?", (n,))
        conn.executemany("INSERT INTO ticket_blocks (ticket_n, blocked_by_n) VALUES (?, ?)",
                         [(n, b) for b in dict.fromkeys(blocked_by)])


EDITABLE = ("enc_content", "type", "priority", "labels", "due", "parent", "blocked_by")
_COLUMN = {"enc_content": "enc_content", "type": "type", "priority": "priority", "due": "due",
           "parent": "parent_n"}


def edit_ticket(conn: sqlite3.Connection, app, ticket_row, *, actor: Actor, rev: int, changes: dict,
                event_uuid: str, enc_body: str | None) -> dict:
    """PATCH: apply `changes` (validated; keys from EDITABLE, parent/blocked_by as ticket numbers)
    if the ticket is still at `rev`, bump rev, write an `edit` event. Returns TicketOut."""
    n = ticket_row["n"]
    now = clock.now_iso()
    with _tx(conn):
        fresh = _fetch(conn, n)
        if fresh["deleted_at"] is not None:
            raise _gone(n)
        if conn.execute("SELECT 1 FROM ticket_events WHERE uuid = ?", (event_uuid,)).fetchone():
            raise _dup("an event")
        if fresh["rev"] != rev:
            raise api_error(409, "conflict", f"{format_ticket_id(n)} is at rev {fresh['rev']}; reload and retry")
        parent = changes.get("parent")
        blockers = changes.get("blocked_by") or []
        if parent == n or n in blockers:
            raise api_error(400, "bad_ref", "a ticket can't be its own parent or blocker")
        _live_ticket_ns(conn, [*([parent] if parent is not None else []), *blockers])
        sets, params = ["rev = rev + 1", "updated_at = ?"], [now]
        for key, col in _COLUMN.items():
            if key in changes:
                sets.append(f"{col} = ?")
                params.append(changes[key])
        conn.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE n = ?", (*params, n))
        _set_links(conn, n, changes.get("labels"), changes.get("blocked_by"))
        ev_row = _write_event(conn, n, uuid=event_uuid, kind="edit", actor=actor, status_from=None,
                              status_to=None, enc_body=enc_body, now=now)
    _committed(conn, app, n, ev_row, actor, None)
    return ticket_out(conn, _fetch(conn, n))


def archive_ticket(conn: sqlite3.Connection, app, ticket_row, *, actor: Actor, uuid: str, enc_body: str) -> dict:
    """Task 12: mark a legacy ticket migrated (archived_at) with one sealed `update` event ("Migrated to … as
    DEMO-0042"). Allowed while the legacy freeze is on. Archiving an archived ticket changes nothing."""
    n = ticket_row["n"]
    if ticket_row["archived_at"] is not None:
        return ticket_out(conn, ticket_row)
    now = clock.now_iso()
    with _tx(conn):
        fresh = _fetch(conn, n)
        if fresh["deleted_at"] is not None:
            raise _gone(n)
        if fresh["archived_at"] is not None:
            ev_row = None
        else:
            ev_row = _write_event(conn, n, uuid=uuid, kind="update", actor=actor, status_from=None, status_to=None,
                                  enc_body=enc_body, now=now)
            conn.execute(f"UPDATE tickets SET archived_at = ?, updated_at = ?, {_CLEAR_CLAIM} WHERE n = ?",
                         (now, now, n))
    if ev_row is not None:
        app.state.ticket_bus.bump(ev_row["seq"])
    return ticket_out(conn, _fetch(conn, n))


def _blank(conn: sqlite3.Connection, n: int, now: str) -> None:
    """The tombstone's blanking (spec T4), inside the caller's transaction."""
    conn.execute(f"UPDATE tickets SET wrapped_dek = NULL, enc_content = NULL, deleted_at = ?, updated_at = ?,"
                 f" {_CLEAR_CLAIM} WHERE n = ?", (now, now, n))
    conn.execute("UPDATE ticket_events SET enc_body = NULL WHERE ticket_n = ?", (n,))
    conn.execute("DELETE FROM ticket_labels WHERE ticket_n = ?", (n,))
    conn.execute("DELETE FROM ticket_blocks WHERE ticket_n = ? OR blocked_by_n = ?", (n, n))
    conn.execute("DELETE FROM ticket_files WHERE ticket_n = ?", (n,))


def tombstone_archived(conn: sqlite3.Connection, cutoff: str, now: str, bus=None) -> int:
    """Tombstone every legacy ticket archived at or before `cutoff` (the 90-day read-only window is over).
    Mirrors are never touched. With `bus` (the app's ticket_bus), board long-polls wake for each tombstone.
    Returns how many."""
    actor = Actor("web", None, "archive")
    count = 0
    rows = conn.execute("SELECT n FROM tickets WHERE mode = 'legacy' AND deleted_at IS NULL"
                        " AND archived_at IS NOT NULL AND archived_at <= ? ORDER BY n", (cutoff,)).fetchall()
    for r in rows:
        with _tx(conn):
            fresh = _fetch(conn, r["n"])
            if fresh is None or fresh["deleted_at"] is not None:
                continue
            ev_row = _write_event(conn, r["n"], uuid=secrets.token_hex(16), kind="edit", actor=actor,
                                  status_from=None, status_to=None, enc_body=None, now=now)
            _blank(conn, r["n"], now)
        if bus is not None:
            bus.bump(ev_row["seq"])
        count += 1
    return count


def archive_all_legacy(conn: sqlite3.Connection, now: str) -> int:
    """`python -m fileshare.admin archive-legacy`, the last switch-over step: stamp archived_at on every live
    legacy ticket without one, and clear every legacy claim (no agent works on a legacy ticket any more)."""
    with _tx(conn):
        n = conn.execute("UPDATE tickets SET archived_at = ? WHERE mode = 'legacy' AND deleted_at IS NULL"
                         " AND archived_at IS NULL", (now,)).rowcount
        conn.execute(f"UPDATE tickets SET {_CLEAR_CLAIM} WHERE mode = 'legacy' AND claim_device_id IS NOT NULL")
        return n


def delete_ticket(conn: sqlite3.Connection, app, ticket_row, *, actor: Actor) -> None:
    """The tombstone (spec T4): content, keys and event bodies go, the claim is released, labels,
    blocks (both ways) and file links are removed. Linked FILEs stay. The number stays retired.
    A bodiless `edit` event records it, so board long-polls (/changes) see the tombstone."""
    n = ticket_row["n"]
    now = clock.now_iso()
    with _tx(conn):
        fresh = _fetch(conn, n)
        if fresh["deleted_at"] is not None:
            raise _gone(n)
        ev_row = _write_event(conn, n, uuid=secrets.token_hex(16), kind="edit", actor=actor, status_from=None,
                              status_to=None, enc_body=None, now=now)
        _blank(conn, n, now)
    app.state.ticket_bus.bump(ev_row["seq"])       # no push: a delete isn't a T7 trigger


def claim_ticket(conn: sqlite3.Connection, app, ticket_row, *, actor: Actor, takeover: bool,
                 event_uuid: str) -> tuple[str, dict, dict]:
    """POST …/claim for a device: (claim_token, TicketOut, EventOut). From open it moves to
    in-progress; in in-progress/waiting/testing an existing claim needs `takeover` (409 claimed)."""
    cur = ticket_row["status"]
    if cur not in ("open", *HOLDER_STATUSES):
        raise api_error(409, "bad_move", f"a ticket in {cur} can't be claimed")
    if cur != "open" and ticket_row["claim_device_id"] and not takeover:
        raise api_error(409, "claimed", f"{format_ticket_id(ticket_row['n'])} is claimed; pass takeover")
    token = new_claim_token()
    ev = insert_event(conn, app, ticket_row, uuid=event_uuid, kind="claim", actor=actor,
                      status_to="in-progress" if cur == "open" else None, enc_body=None, files=[],
                      claim=(actor.device_id, sha256_hex(token)))
    return token, ticket_out(conn, _fetch(conn, ticket_row["n"])), ev


def release_claim(conn: sqlite3.Connection, app, ticket_row, *, actor: Actor) -> dict | None:
    """DELETE …/claim: a `release` event, status unchanged. None when nobody holds the claim."""
    if not ticket_row["claim_device_id"]:
        return None
    return insert_event(conn, app, ticket_row, uuid=secrets.token_hex(16), kind="release", actor=actor,
                        status_to=None, enc_body=None, files=[])


def release_claims_of_device(conn: sqlite3.Connection, app, device_id: str, actor_name: str = "browser") -> None:
    """A revoked device loses every claim, each recorded as a `release` event by web (spec T4)."""
    actor = Actor("web", None, actor_name)
    races = 0
    while True:
        row = conn.execute("SELECT * FROM tickets WHERE claim_device_id = ? AND deleted_at IS NULL"
                           " ORDER BY n LIMIT 1", (device_id,)).fetchone()
        if row is None:
            return
        try:
            release_claim(conn, app, row, actor=actor)
        except Exception as e:
            # a concurrent write changed the ticket between the SELECT and the release: re-read it
            races += 1
            if getattr(e, "status_code", None) != 409 or races > 10:
                raise
