import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from fileshare import clock
from fileshare.db import get_meta
from fileshare.deps import (Principal, api_error, get_db, read_json, require_any, require_device,
                            require_device_any_state, require_session)
from fileshare.routes.links import revoke_device_links
from fileshare.sessions import session_name
from fileshare.tickets import release_claims_of_device
from fileshare.security import P256_PUB_LEN, b64u_decode, b64u_encode, valid_p256_point

router = APIRouter()

# eph_pub (65) || seal(wk, MK): version (1) + nonce (12) + 32-byte key + tag (16)   (§4.6)
BUNDLE_LEN = P256_PUB_LEN + 1 + 12 + 32 + 16

# pubkey is public: the browser needs it to seal MK to the device (§4.6).
# token_hash and device_bundle never leave the server through device_out.
PUBLIC_FIELDS = ("id", "name", "project", "hostname", "platform", "fingerprint", "pubkey",
                 "created_at", "approved_at", "last_seen_at", "revoked_at")


def device_status(row) -> Literal["pending", "active", "revoked"]:
    if row["revoked_at"] is not None:
        return "revoked"
    if row["approved_at"] is not None:
        return "active"
    return "pending"


def device_out(row) -> dict:
    out = {k: row[k] for k in PUBLIC_FIELDS}
    out["status"] = device_status(row)
    return out


def _key_version(conn) -> int:
    return int(get_meta(conn, "key_version") or 1)


def require_browser_list(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Principal:
    """The device list is browser-only (controller ruling). A device token gets an explicit 403, not 401,
    so the CLI does not mistake it for a revoked device."""
    principal = require_any(request, conn)
    if principal.kind != "session":
        raise api_error(403, "forbidden", "the device list is only shown in your browser")
    return principal


@router.get("/api/devices")
def list_devices(_: Principal = Depends(require_browser_list),
                 conn: sqlite3.Connection = Depends(get_db)):
    # pending (needs your attention) first, then active, then revoked; newest first inside each group
    rows = conn.execute(
        "SELECT * FROM devices"
        " ORDER BY revoked_at IS NOT NULL, approved_at IS NOT NULL, created_at DESC, id").fetchall()
    return {"devices": [device_out(r) for r in rows]}


# The two /self routes are declared before /{device_id} so "self" is never treated as an id.
@router.get("/api/devices/self/bundle")
def self_bundle(p: Principal = Depends(require_device_any_state),
                conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT approved_at, device_bundle FROM devices WHERE id = ?",
                       (p.device["id"],)).fetchone()
    if row["approved_at"] is None:
        return JSONResponse({"status": "pending"}, status_code=202)
    return {"device_bundle": row["device_bundle"], "key_version": _key_version(conn)}


@router.delete("/api/devices/self", status_code=204)
def revoke_self(request: Request, p: Principal = Depends(require_device_any_state),
                conn: sqlite3.Connection = Depends(get_db)):
    ts = clock.now_iso()
    conn.execute("UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (ts, p.device["id"]))
    revoke_device_links(conn, p.device["id"], ts)
    release_claims_of_device(conn, request.app, p.device["id"])     # spec T4: recorded as web
    return Response(status_code=204)


@router.post("/api/devices/{device_id}/approve", status_code=204)
async def approve_device(device_id: str, request: Request,
                         _: Principal = Depends(require_session),
                         conn: sqlite3.Connection = Depends(get_db)):
    body = await read_json(request)
    try:
        bundle = b64u_decode(body["device_bundle"])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise api_error(400, "bad_request", "device_bundle must be base64url")
    if (len(bundle) != BUNDLE_LEN
            or not valid_p256_point(bundle[:P256_PUB_LEN])
            or bundle[P256_PUB_LEN] != 1):
        raise api_error(400, "bad_request",
                        f"device_bundle must be {BUNDLE_LEN} bytes: an ephemeral P-256 point"
                        " followed by a v1 envelope of the 32-byte key")
    cur = conn.execute(
        "UPDATE devices SET approved_at = ?, device_bundle = ?"
        " WHERE id = ? AND approved_at IS NULL AND revoked_at IS NULL",
        (clock.now_iso(), b64u_encode(bundle), device_id))
    if cur.rowcount == 1:
        return Response(status_code=204)
    if conn.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)).fetchone() is None:
        raise api_error(404, "not_found", "no such device")
    raise api_error(409, "not_pending", "device is already approved or was revoked")


@router.delete("/api/devices/{device_id}", status_code=204)
def revoke_device(device_id: str, request: Request, p: Principal = Depends(require_session),
                  conn: sqlite3.Connection = Depends(get_db)):
    # Also "Reject" for a pending device (§6).
    ts = clock.now_iso()
    cur = conn.execute("UPDATE devices SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?", (ts, device_id))
    if cur.rowcount == 0:
        raise api_error(404, "not_found", "no such device")
    revoke_device_links(conn, device_id, ts)     # its public links die with it (§17)
    # and its ticket claims, each a `release` event by this browser session (spec T4)
    release_claims_of_device(conn, request.app, device_id, session_name(conn, p.session_hash) or "browser")
    return Response(status_code=204)


@router.get("/api/whoami")
def whoami(p: Principal = Depends(require_device),
           conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (p.device["id"],)).fetchone()
    return {"device": device_out(row), "key_version": _key_version(conn)}
