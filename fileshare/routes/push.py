"""Push subscription and VAPID endpoints (spec T6, T7). S only."""
import sqlite3

from fastapi import APIRouter, Depends, Request, Response

from fileshare import clock
from fileshare.deps import Principal, api_error, get_db, require_session
from fileshare.push import ensure_vapid
from fileshare.routes.files import read_bounded_json
from fileshare.security import b64u_decode, new_id
from fileshare.sessions import session_name
from fileshare.tickets import _tx

router = APIRouter()

MAX_SUBSCRIBE_BODY = 8192


def _b64u_field(v, field: str) -> str:
    if not isinstance(v, str) or not v:
        raise api_error(400, "bad_request", f"{field} must be a non-empty base64url string")
    try:
        b64u_decode(v)
    except ValueError:
        raise api_error(400, "bad_request", f"{field} is not base64url")
    return v


@router.get("/api/push/vapid")
def get_vapid(principal: Principal = Depends(require_session),
             conn: sqlite3.Connection = Depends(get_db)):
    return {"public_key": ensure_vapid(conn)}


@router.post("/api/push/subscribe", status_code=201)
async def subscribe(request: Request, principal: Principal = Depends(require_session),
                    conn: sqlite3.Connection = Depends(get_db)):
    body = await read_bounded_json(request, MAX_SUBSCRIBE_BODY)
    endpoint = body.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise api_error(400, "bad_request", "endpoint must be an https URL")
    keys = body.get("keys")
    if not isinstance(keys, dict):
        raise api_error(400, "bad_request", "keys is required")
    p256dh = _b64u_field(keys.get("p256dh"), "p256dh")
    auth = _b64u_field(keys.get("auth"), "auth")
    name = session_name(conn, principal.session_hash) or "browser"
    existing = conn.execute("SELECT id FROM push_subs WHERE endpoint = ?", (endpoint,)).fetchone()
    sub_id = existing["id"] if existing else new_id("psh")
    session = principal.session_hash
    with _tx(conn):
        conn.execute(
            "INSERT INTO push_subs (id, session_name, endpoint, p256dh, auth, created_at, session_hash) VALUES"
            " (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET"
            " session_name = excluded.session_name, p256dh = excluded.p256dh, auth = excluded.auth,"
            " session_hash = excluded.session_hash, failure_count = 0",
            (sub_id, name, endpoint, p256dh, auth, clock.now_iso(), session))
        # One browser, one subscription (QA N-06): when the push service rotated the endpoint, the old row of the
        # same session is replaced, not kept as a second phone to push to.
        if session:
            conn.execute("DELETE FROM push_subs WHERE session_hash = ? AND endpoint != ?", (session, endpoint))
    return {"id": sub_id}


@router.delete("/api/push/subscribe", status_code=204)
async def unsubscribe(request: Request, principal: Principal = Depends(require_session),
                      conn: sqlite3.Connection = Depends(get_db)):
    body = await read_bounded_json(request, MAX_SUBSCRIBE_BODY)
    endpoint = body.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        raise api_error(400, "bad_request", "endpoint is required")
    conn.execute("DELETE FROM push_subs WHERE endpoint = ?", (endpoint,))
    return Response(status_code=204)
