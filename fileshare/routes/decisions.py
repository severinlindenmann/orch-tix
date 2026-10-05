"""Decisions and the desktop inbox HTTP API (TIX spec §4.2–§4.4, §9.2). A decision comes only from the
browser session; the space's owner device long-polls the inbox and acks."""
import sqlite3

from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from fileshare import decisions, mirrors, tickets
from fileshare.deps import Principal, api_error, get_db, rate_slot, require_any
from fileshare.ids import parse_ticket_ref
from fileshare.routes.files import UUID_RE, check_envelope, read_bounded_json
from fileshare.routes.mirrors import _kv, _space_id, require_device_only
from fileshare.routes.tickets import MIN_ENC_LEN, _authenticate, _poll_params, _short, _uuid
from fileshare.sessions import session_name

router = APIRouter()
MAX_DECISION_B64 = 65536
DECISIONS_PER_HOUR = 120


def _shape(raw: dict) -> dict:
    kind = raw.get("kind")
    if kind not in decisions.KINDS:
        raise api_error(400, "bad_request", "kind must be one of " + ", ".join(decisions.KINDS))
    ticket = raw.get("ticket")
    if ticket is not None and (not isinstance(ticket, str) or parse_ticket_ref(ticket) is None):
        raise api_error(400, "bad_ref", "ticket must be null or a TIX id")
    if kind != "ticket_request" and ticket is None:
        raise api_error(400, "bad_request", f"a {kind} decision names its ticket")
    check_envelope(raw.get("enc_body"), "enc_body", MAX_DECISION_B64, lambda b: len(b) >= MIN_ENC_LEN,
                   code="bad_request")
    return {"uuid": _uuid(raw.get("uuid"), "uuid"), "space": _space_id(raw.get("space")), "ticket": ticket,
            "kind": kind, "key_version": _kv(raw.get("key_version")), "enc_body": raw["enc_body"]}


def _decision_uuid(decision_id: str) -> str:
    raw = decision_id.removeprefix("dec_")
    if not UUID_RE.fullmatch(raw):
        raise api_error(400, "bad_request", "decision id must be dec_ and 32 lowercase hex characters")
    return raw


@router.post("/api/decisions", status_code=201)
async def post_decision(request: Request, principal: Principal = Depends(require_any),
                        conn: sqlite3.Connection = Depends(get_db)):
    if principal.kind != "session":
        raise api_error(403, "forbidden", "decisions come from your browser session, never from a device")
    body = _shape(await read_bounded_json(request, MAX_DECISION_B64 + 4096))
    with rate_slot(request.app.state.decision_limiter, principal.session_hash,
                   f"at most {DECISIONS_PER_HOUR} decisions per hour"):       # a failed post spends no slot
        return decisions.create_decision(conn, request.app,
                                         session_name=session_name(conn, principal.session_hash) or "browser",
                                         body=body, session_hash=principal.session_hash)


@router.get("/api/decisions")
def list_decisions(space: str, ticket: str | None = None, principal: Principal = Depends(require_any),
                   conn: sqlite3.Connection = Depends(get_db)):
    space_id = _space_id(space)
    if principal.kind == "device":
        mirrors.owner_space(conn, space_id, principal.device)      # 404 no_space, 403 not_owner
    else:
        mirrors.space_row(conn, space_id)
    ticket_n = None
    if ticket is not None:
        ticket_n = parse_ticket_ref(ticket)
        if ticket_n is None:
            raise api_error(400, "bad_ref", "ticket must be a TIX id")
    return {"decisions": decisions.list_decisions(conn, space_id=space_id, ticket_n=ticket_n)}


@router.get("/api/inbox/changes")
async def inbox_changes(request: Request, space: str, after: str = "0", wait: str = "0"):
    after_n, wait_s = _poll_params(after, wait)
    space_id = _space_id(space)
    principal = await _authenticate(request, require_device_only)
    device = principal.device
    with tickets.poll_slot(device["id"]):                                         # 429
        # The owner check and the heartbeat run even when nothing is new (R3: the poll records "last seen").
        await run_in_threadpool(_short, request,
                                lambda conn: decisions.heartbeat(conn, device=device, space_id=space_id))
        found = await tickets.wait_for_events(
            request, request.app.state.inbox_bus, after_n, wait_s,
            lambda: _short(request, lambda conn: decisions.inbox_after(conn, device=device, space_id=space_id,
                                                                       after=after_n)))
    return {"decisions": found, "cursor": found[-1]["seq"] if found else after_n}


@router.post("/api/decisions/{decision_id}/ack", status_code=204)
async def ack_decision(decision_id: str, request: Request, principal: Principal = Depends(require_device_only),
                       conn: sqlite3.Connection = Depends(get_db)):
    uuid = _decision_uuid(decision_id)
    body = await read_bounded_json(request, 1024)
    ack = body.get("ack")
    if ack not in decisions.ACKS and ack not in decisions.WAITING:
        raise api_error(400, "bad_request", "ack must be one of " + ", ".join(decisions.ACKS + decisions.WAITING))
    decisions.ack_decision(conn, request.app, device=principal.device, uuid=uuid, ack=ack)
    return Response(status_code=204)
