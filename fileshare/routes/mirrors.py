"""Spaces and mirrors HTTP API (TIX spec §9). Shapes are checked here; fileshare.mirrors does the rest."""
import sqlite3

from fastapi import APIRouter, Depends, Request, Response

from fileshare import mirrors, tickets
from fileshare.deps import Principal, api_error, get_db, require_any, require_session
from fileshare.ids import parse_ticket_ref
from fileshare.routes.files import UUID_RE, WRAPPED_DEK_LEN, check_envelope, read_bounded_json
from fileshare.routes.tickets import MIN_ENC_LEN, _authenticate, _choice, _poll_params, _short, _uuid
from fileshare.sessions import session_name

router = APIRouter()
MAX_MIRROR_B64 = 1 << 20
MAX_LABEL_B64 = 4096


def require_device_only(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Principal:
    """Device-only routes: an approved device; a browser session gets 403 (it never writes mirrors)."""
    principal = require_any(request, conn)
    if principal.kind != "device":
        raise api_error(403, "forbidden", "only a workspace device does this; the browser reads spaces and mirrors")
    return principal


def _space_id(v) -> str:
    if not isinstance(v, str) or not UUID_RE.fullmatch(v):
        raise api_error(400, "bad_request", "space must be 32 lowercase hex characters")
    return v


def _kv(v) -> int:
    if type(v) is not int or not 1 <= v <= 255:
        raise api_error(400, "bad_request", "key_version must be an integer in [1, 255]")
    return v


@router.post("/api/spaces", status_code=201)
async def create_space(request: Request, principal: Principal = Depends(require_device_only),
                       conn: sqlite3.Connection = Depends(get_db)):
    body = await read_bounded_json(request, MAX_LABEL_B64 + 1024)
    check_envelope(body.get("enc_label"), "enc_label", MAX_LABEL_B64, lambda b: len(b) >= MIN_ENC_LEN, code="bad_request")
    return mirrors.create_space(conn, request.app, device=principal.device, space_id=_space_id(body.get("id")),
                                key_version=_kv(body.get("key_version")), enc_label=body["enc_label"])


@router.get("/api/spaces")
def list_spaces(_: Principal = Depends(require_any), conn: sqlite3.Connection = Depends(get_db)):
    return {"spaces": mirrors.list_spaces(conn)}


def require_session_only(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Principal:
    """Browser-only routes: a device (a remote agent included) gets 403, never 401 (ruling TIX-J1)."""
    principal = require_any(request, conn)       # a session write is Origin-checked inside require_session
    if principal.kind != "session":
        raise api_error(403, "forbidden", "only your browser approves this, never a device")
    return principal


def _join_id(v: str) -> str:
    raw = v.removeprefix("jr_")
    if not UUID_RE.fullmatch(raw):
        raise api_error(400, "bad_request", "a join request id is jr_ and 32 lowercase hex characters")
    return raw


@router.post("/api/spaces/{space_id}/join")
def join_space(space_id: str, response: Response, request: Request, principal: Principal = Depends(require_device_only),
               conn: sqlite3.Connection = Depends(get_db)):
    """Ask to take the space over. Nothing changes until the browser approves (202 pending)."""
    req, space = mirrors.request_join(conn, request.app, device=principal.device, space_id=_space_id(space_id))
    if req is None:
        return {"status": "owner", "request": None, "space": space}
    response.status_code = 202
    return {"status": req["status"], "request": req, "space": space}


@router.get("/api/join-requests")
def list_join_requests(status: str = "pending", _: Principal = Depends(require_session_only),
                       conn: sqlite3.Connection = Depends(get_db)):
    return {"requests": mirrors.list_join_requests(conn, status=_choice(status, "status", mirrors.JOIN_STATUSES))}


@router.get("/api/spaces/{space_id}/join-requests/{req_id}")
def get_join_request(space_id: str, req_id: str, principal: Principal = Depends(require_any),
                     conn: sqlite3.Connection = Depends(get_db)):
    row = mirrors.join_request_row(conn, _space_id(space_id), _join_id(req_id))
    if principal.kind == "device" and row["device_id"] != principal.device["id"]:
        raise api_error(403, "forbidden", "only the requesting device or your browser reads this request")
    return mirrors.join_request_out(conn, row)


def _decide(request: Request, space_id: str, req_id: str, principal: Principal, conn, decision: str) -> dict:
    return mirrors.decide_join(conn, request.app, space_id=_space_id(space_id), req_id=_join_id(req_id),
                               decision=decision,
                               decided_by=session_name(conn, principal.session_hash) or "browser")


@router.post("/api/spaces/{space_id}/join-requests/{req_id}/approve")
def approve_join(space_id: str, req_id: str, request: Request, principal: Principal = Depends(require_session_only),
                 conn: sqlite3.Connection = Depends(get_db)):
    return _decide(request, space_id, req_id, principal, conn, "approved")


@router.post("/api/spaces/{space_id}/join-requests/{req_id}/deny")
def deny_join(space_id: str, req_id: str, request: Request, principal: Principal = Depends(require_session_only),
              conn: sqlite3.Connection = Depends(get_db)):
    return _decide(request, space_id, req_id, principal, conn, "denied")


@router.get("/api/mirrors/changes")
async def mirror_changes(request: Request, after: str = "0", wait: str = "0"):
    after_n, wait_s = _poll_params(after, wait)
    await _authenticate(request, require_session)
    cursor = {"seq": after_n}

    def fetch() -> list[dict]:
        found, cursor["seq"] = _short(request, lambda conn: mirrors.mirror_changes_after(conn, after_n))
        return found

    found = await tickets.wait_for_events(request, request.app.state.ticket_bus, after_n, wait_s, fetch)
    return {"mirrors": found, "cursor": cursor["seq"] if found else after_n}


@router.get("/api/mirrors")
def list_mirrors(space: str | None = None, _: Principal = Depends(require_any), conn: sqlite3.Connection = Depends(get_db)):
    return {"mirrors": mirrors.list_mirrors(conn, None if space is None else _space_id(space))}


@router.get("/api/mirrors/u/{uuid}")
def mirror_by_uuid(uuid: str, space: str, principal: Principal = Depends(require_device_only),
                   conn: sqlite3.Connection = Depends(get_db)):
    space_id = _space_id(space)
    mirrors.owner_space(conn, space_id, principal.device)          # 404 no_space, 403 not_owner
    row = mirrors.get_mirror_by_uuid(conn, space_id, _uuid(uuid, "uuid"))
    if row is None or row["deleted_at"] is not None:
        raise api_error(404 if row is None else 410, "not_found" if row is None else "gone", "no such mirror")
    return {**mirrors.mirror_out(row), **mirrors.writer_of(conn, row)}


@router.get("/api/mirrors/{ref}")
def get_mirror(ref: str, _: Principal = Depends(require_any), conn: sqlite3.Connection = Depends(get_db)):
    n = parse_ticket_ref(ref)
    row = None if n is None else conn.execute("SELECT * FROM tickets WHERE n = ? AND mode = 'mirror'", (n,)).fetchone()
    if row is None or row["deleted_at"] is not None:
        raise api_error(404, "not_found", f"no mirror {ref}")
    return mirrors.mirror_out(row)


@router.put("/api/mirrors/{uuid}")
async def put_mirror(uuid: str, request: Request, principal: Principal = Depends(require_device_only),
                     conn: sqlite3.Connection = Depends(get_db)):
    raw = await read_bounded_json(request, MAX_MIRROR_B64 + 8192)
    rev = raw.get("mirror_rev")
    if type(rev) is not int or rev < 1:
        raise api_error(400, "bad_request", "mirror_rev must be a positive integer")
    oq = raw.get("open_questions")
    if type(oq) is not int or not 0 <= oq <= 20:
        raise api_error(400, "bad_request", "open_questions must be an integer in [0, 20]")
    needs = raw.get("needs")
    if needs is not None and needs not in mirrors.NEEDS:
        raise api_error(400, "bad_request", "needs must be null, question, approval or verdict")
    if raw.get("wrapped_dek") is not None:
        check_envelope(raw["wrapped_dek"], "wrapped_dek", 4096, lambda b: len(b) == WRAPPED_DEK_LEN, code="bad_request")
    check_envelope(raw.get("enc_content"), "enc_content", MAX_MIRROR_B64, lambda b: len(b) >= MIN_ENC_LEN, code="bad_request")
    body = {"space": _space_id(raw.get("space")), "mirror_rev": rev, "schema_version": mirrors.check_schema(raw.get("schema_version")),
            "status": _choice(raw.get("status"), "status", tickets.STATUSES),
            "priority": _choice(raw.get("priority", "normal"), "priority", tickets.PRIORITIES),
            "needs": needs, "open_questions": oq, "key_version": _kv(raw.get("key_version")),
            "wrapped_dek": raw.get("wrapped_dek"), "enc_content": raw["enc_content"],
            "event_uuid": _uuid(raw.get("event_uuid"), "event_uuid")}
    if raw.get("notify") is not None:        # cleartext: whether this ticket may notify the phone (default off)
        if type(raw["notify"]) is not bool:
            raise api_error(400, "bad_request", "notify must be true or false")
        body["notify"] = raw["notify"]
        seen = raw.get("notify_seen", 0)
        if type(seen) is not int or seen < 0:
            raise api_error(400, "bad_request", "notify_seen must be a non-negative integer")
        body["notify_seen"] = seen
    result, before, after = mirrors.upsert_mirror(conn, request.app, device=principal.device,
                                                  uuid=_uuid(uuid, "uuid"), body=body)
    mirrors.after_needs_change(conn, request.app, space=body["space"], ticket=result["id"], before=before,
                               after=after, open_questions=oq)
    return result


@router.put("/api/mirrors/{ref}/notify")
async def put_mirror_notify(ref: str, request: Request, principal: Principal = Depends(require_session_only),
                            conn: sqlite3.Connection = Depends(get_db)):
    """The phone (your browser session; never a device, so an agent's `sharing` credential cannot) turns phone
    notifications for one mirrored ticket on or off."""
    raw = await read_bounded_json(request, 1024)
    if type(raw.get("on")) is not bool:
        raise api_error(400, "bad_request", "on must be true or false")
    n = parse_ticket_ref(ref)
    if n is None:
        raise api_error(404, "not_found", f"no mirror {ref}")
    return mirrors.set_notify(conn, request.app, ref_n=n, on=raw["on"],
                              actor_name=session_name(conn, principal.session_hash) or "browser")


@router.put("/api/spaces/{space_id}/notify")
async def put_space_notify(space_id: str, request: Request, principal: Principal = Depends(require_device_only),
                           conn: sqlite3.Connection = Depends(get_db)):
    """The owner's desktop: phone notifications for agent messages that name no ticket (default off)."""
    raw = await read_bounded_json(request, 1024)
    if type(raw.get("messages")) is not bool:
        raise api_error(400, "bad_request", "messages must be true or false")
    return mirrors.set_space_notify(conn, device=principal.device, space_id=_space_id(space_id), messages=raw["messages"])


@router.get("/api/spaces/{space_id}/notify")
def get_notify_state(space_id: str, principal: Principal = Depends(require_device_only),
                     conn: sqlite3.Connection = Depends(get_db)):
    """What the owner's desktop needs to merge phone changes: per mirror the switch and its phone-change counter."""
    space = _space_id(space_id)
    row = mirrors.owner_space(conn, space, principal.device)
    return {"messages": bool(row["notify_messages"]), "mirrors": mirrors.notify_state(conn, space)}


@router.delete("/api/mirrors/{uuid}", status_code=204)
def delete_mirror(uuid: str, space: str, request: Request, principal: Principal = Depends(require_device_only),
                  conn: sqlite3.Connection = Depends(get_db)):
    n, before = mirrors.unlink_mirror(conn, request.app, device=principal.device, space_id=_space_id(space),
                                      uuid=_uuid(uuid, "uuid"))
    mirrors.after_needs_change(conn, request.app, space=space, ticket=f"TIX-{n}", before=before, after=None,
                               open_questions=0)
    return Response(status_code=204)
