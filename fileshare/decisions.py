"""Phone decisions (TIX spec §4.2): sealed, written by a browser session only, read and acked by the space's
owner device. The server never judges a decision; the desktop does."""
import hashlib

from fileshare import clock
from fileshare.deps import api_error
from fileshare.ids import format_ticket_id, parse_ticket_ref
from fileshare.mirrors import owner_space, space_row
from fileshare.tickets import Actor, _tx, _write_event

KINDS = ("answer", "approve", "request_changes", "verdict", "comment", "ticket_request")
ACKS = ("applied", "ignored", "stale", "superseded", "answered-locally", "unlinked")
# Non-final (feedback fix round F1): the desktop holds the decision and says why it was not applied at once. Fixed
# codes only (no free text on the server); a final ack replaces one, never the other way round.
WAITING = ("waiting-unpaired", "waiting-signature", "waiting-switched-off", "waiting-time", "waiting-check")
LIST_LIMIT = 200
INBOX_LIMIT = 100


def decision_out(row) -> dict:
    return {"id": "dec_" + row["uuid"], "seq": row["seq"], "uuid": row["uuid"], "space": row["space_id"],
            "ticket": format_ticket_id(row["ticket_n"]) if row["ticket_n"] else None, "kind": row["kind"],
            "key_version": row["key_version"], "enc_body": row["enc_body"], "created_at": row["created_at"],
            "session_name": row["session_name"], "ack": row["ack"], "ack_at": row["ack_at"]}


def create_decision(conn, app, *, session_name: str, body: dict) -> dict:
    with _tx(conn):
        # The duplicate check comes first: a double tap or a resend after a timeout is "already sent" (409),
        # even if the ticket was unlinked in between (Review Focus 4).
        if conn.execute("SELECT 1 FROM decisions WHERE uuid = ?", (body["uuid"],)).fetchone():
            raise api_error(409, "duplicate_uuid", "this decision was already sent")
        space_row(conn, body["space"])
        ticket_n = None
        if body["kind"] == "ticket_request":
            if body["ticket"] is not None:
                raise api_error(400, "bad_request", "a ticket_request names no ticket")
        else:
            ticket_n = parse_ticket_ref(body["ticket"]) if isinstance(body["ticket"], str) else None
            row = None if ticket_n is None else conn.execute(
                "SELECT 1 FROM tickets WHERE n = ? AND mode = 'mirror' AND space_id = ? AND deleted_at IS NULL",
                (ticket_n, body["space"])).fetchone()
            if row is None:
                raise api_error(400, "bad_ref", "no live mirrored ticket with that id in this space")
        cur = conn.execute("INSERT INTO decisions (uuid, space_id, ticket_n, kind, session_name, key_version, enc_body,"
                           " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (body["uuid"], body["space"], ticket_n, body["kind"], session_name, body["key_version"],
                            body["enc_body"], clock.now_iso()))
    app.state.inbox_bus.bump(cur.lastrowid)
    return {"id": "dec_" + body["uuid"], "seq": cur.lastrowid}


def list_decisions(conn, *, space_id: str, ticket_n: int | None = None) -> list[dict]:
    q, args = "SELECT * FROM decisions WHERE space_id = ?", [space_id]
    if ticket_n is not None:
        q += " AND ticket_n = ?"
        args.append(ticket_n)
    rows = conn.execute(q + " ORDER BY seq DESC LIMIT ?", (*args, LIST_LIMIT)).fetchall()
    return [decision_out(r) for r in rows]


def heartbeat(conn, *, device, space_id: str) -> None:
    """The owner device is polling its inbox: stamp spaces.last_seen_at (the desktop heartbeat, ruling R3)."""
    owner_space(conn, space_id, device)
    conn.execute("UPDATE spaces SET last_seen_at = ? WHERE id = ?", (clock.now_iso(), space_id))


def inbox_after(conn, *, device, space_id: str, after: int, limit: int = INBOX_LIMIT) -> list[dict]:
    heartbeat(conn, device=device, space_id=space_id)
    rows = conn.execute("SELECT d.*, t.uuid AS ticket_uuid, t.wrapped_dek AS ticket_wrapped_dek FROM decisions d"
                        " LEFT JOIN tickets t ON t.n = d.ticket_n WHERE d.space_id = ? AND d.seq > ? AND d.ack IS NULL"
                        " ORDER BY d.seq LIMIT ?", (space_id, after, limit)).fetchall()
    return [decision_out(r) | {"ticket_uuid": r["ticket_uuid"], "ticket_wrapped_dek": r["ticket_wrapped_dek"]}
            for r in rows]


def ack_decision(conn, app, *, device, uuid: str, ack: str) -> None:
    now = clock.now_iso()
    ev = None
    with _tx(conn):
        row = conn.execute("SELECT * FROM decisions WHERE uuid = ?", (uuid,)).fetchone()
        if row is None:
            raise api_error(404, "not_found", "no such decision")
        owner_space(conn, row["space_id"], device)
        if row["ack"] == ack:
            return
        if row["ack"] is not None and row["ack"] not in WAITING:
            raise api_error(409, "conflict", f"already acked as {row['ack']}")
        conn.execute("UPDATE decisions SET ack = ?, ack_at = ? WHERE uuid = ? AND (ack IS NULL OR ack IN"
                     f" ({','.join('?' * len(WAITING))}))", (ack, now, uuid, *WAITING))
        if row["ticket_n"]:
            tag = f"ack|{uuid}" if ack not in WAITING else f"ack|{uuid}|{ack}"     # a waiting ack, then the final
            ev = _write_event(conn, row["ticket_n"], uuid=hashlib.sha256(tag.encode()).hexdigest()[:32],
                              kind="ack", actor=Actor("device", device["id"], device["name"]), status_from=None,
                              status_to=None, enc_body=None, now=now)
    if ev is not None:
        app.state.ticket_bus.bump(ev["seq"])     # wakes the PWA's /api/mirrors/changes
