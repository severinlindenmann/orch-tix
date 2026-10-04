"""Agent messages HTTP API (TIX spec §8). Routing is cleartext and checked here; bodies stay sealed."""
import re
import sqlite3

from fastapi import APIRouter, Depends, Request, Response

from fileshare import messages, tickets
from fileshare.deps import Principal, api_error, get_db, require_any
from fileshare.ids import parse_ticket_ref
from fileshare.routes.files import UUID_RE, check_envelope, read_bounded_json
from fileshare.routes.mirrors import _kv, _space_id
from fileshare.routes.tickets import MIN_ENC_LEN, _authenticate, _choice, _files, _poll_params, _short, _uuid

router = APIRouter()
MAX_MESSAGE_B64 = 65536
MAX_SIZE = 1 << 40
_TO_ID = re.compile(r"^[A-Za-z0-9._-]{0,64}$")


def _shape(raw: dict) -> dict:
    to_kind = _choice(raw.get("to_kind"), "to_kind", messages.TO_KINDS)
    to_id = raw.get("to_id")
    if not isinstance(to_id, str) or not _TO_ID.fullmatch(to_id):
        raise api_error(400, "bad_request", "to_id must be up to 64 of A-Z a-z 0-9 . _ -")
    if (to_id == "") != (to_kind == "human"):
        raise api_error(400, "bad_request", "to_id is empty exactly for to_kind human")
    if to_kind == "space" and not UUID_RE.fullmatch(to_id):
        raise api_error(400, "bad_request", "a space id is 32 lowercase hex characters")
    files = raw.get("files", [])
    if not isinstance(files, list) or len(files) > messages.MAX_FILES:
        raise api_error(400, "bad_request", f"files is a list of at most {messages.MAX_FILES} FILE ids")
    ticket = raw.get("ticket")
    if ticket is not None and (not isinstance(ticket, str) or parse_ticket_ref(ticket) is None):
        raise api_error(400, "bad_ref", "ticket must be null or a TIX id")
    space = raw.get("space")
    size = raw.get("size", 0)
    if type(size) is not int or not 0 <= size <= MAX_SIZE:
        raise api_error(400, "bad_request", "size must be a non-negative integer")
    check_envelope(raw.get("enc_body"), "enc_body", MAX_MESSAGE_B64, lambda b: len(b) >= MIN_ENC_LEN,
                   code="bad_request")
    return {"uuid": _uuid(raw.get("uuid"), "uuid"), "to_kind": to_kind, "to_id": to_id,
            "kind": _choice(raw.get("kind"), "kind", messages.KINDS), "key_version": _kv(raw.get("key_version")),
            "enc_body": raw["enc_body"], "files": _files(files), "space": None if space is None else _space_id(space),
            "ticket": ticket, "size": size}


@router.post("/api/messages", status_code=201)
async def post_message(request: Request, principal: Principal = Depends(require_any),
                       conn: sqlite3.Connection = Depends(get_db)):
    body = _shape(await read_bounded_json(request, MAX_MESSAGE_B64 + 4096))
    return messages.create_message(conn, request.app, principal=principal, body=body)


@router.get("/api/messages")
async def list_messages(request: Request, after: str = "0", wait: str = "0"):
    after_n, wait_s = _poll_params(after, wait)
    principal = await _authenticate(request, require_any)
    with tickets.poll_slot(principal.device["id"] if principal.kind == "device" else None):   # 429
        found = await tickets.wait_for_events(
            request, request.app.state.message_bus, after_n, wait_s,
            lambda: _short(request, lambda conn: messages.messages_for(conn, principal, after_n)))
    return {"messages": found, "cursor": found[-1]["seq"] if found else after_n}


@router.post("/api/messages/{message_id}/ack", status_code=204)
def ack_message(message_id: str, principal: Principal = Depends(require_any), conn: sqlite3.Connection = Depends(get_db)):
    raw = message_id.removeprefix("msg_")
    if not UUID_RE.fullmatch(raw):
        raise api_error(400, "bad_request", "message id must be msg_ and 32 lowercase hex characters")
    messages.ack_message(conn, principal, raw)
    return Response(status_code=204)
