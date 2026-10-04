import hmac
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ValidationError

from fileshare import clock
from fileshare.db import get_meta, set_meta
from fileshare.deps import (Principal, api_error, attempt, check_origin, get_db, read_json, refuse,
                            require_session)
from fileshare.routes.files import read_bounded_json
from fileshare.security import b64u_decode, hash_secret, sha256_hex, verify_secret
from fileshare.sessions import (clean_name, clear_cookie, cookie_name, create_session, delete_session,
                                session_name, set_cookie)

router = APIRouter()

WRAPPED_MK_LEN = 1 + 12 + 32 + 16
MIN_ITER, MAX_ITER = 1000, 10_000_000
# Cap the login/setup bodies before they are buffered/parsed: without this an unauthenticated
# client could stream a huge body, since these are the only routes that took a Pydantic body
# param (FastAPI buffers and parses it before the in-body rate limiter runs). 8 KiB is far more
# than any legitimate login/setup body (a 32-byte key, a 16-byte salt, one wrapped MK, a name).
MAX_AUTH_BODY = 8192


async def _read_auth_body(request: Request, model: type[BaseModel]) -> BaseModel:
    """Bound the body to MAX_AUTH_BODY (413 on oversize) before validating it into `model`.
    A shape error becomes the same 400 bad_request FastAPI's validation handler would return."""
    body = await read_bounded_json(request, MAX_AUTH_BODY)
    try:
        return model.model_validate(body)
    except ValidationError:
        raise api_error(400, "bad_request", "invalid request") from None


class SetupIn(BaseModel):
    setup_code: str
    kdf_salt: str
    kdf_iterations: int
    auth_key: str
    wrapped_mk: str
    # "restore" re-wraps the existing MK under a new passphrase. "new" (the default) is only
    # valid while the share is uninitialized: a fresh MK would orphan every existing file.
    mode: Literal["new", "restore"] = "new"
    # Optional browser label (spec §14 B). Any type is accepted so a bad name can never turn a
    # login into a different error; clean_name() maps anything invalid to "" (unnamed).
    name: Any = None


class LoginIn(BaseModel):
    auth_key: str
    name: Any = None


def _decoded_len(value: str, want: int) -> bool:
    try:
        return len(b64u_decode(value)) == want
    except ValueError:
        return False


def _code_valid(conn: sqlite3.Connection, code: str) -> bool:
    stored = get_meta(conn, "setup_code_hash")
    expires = get_meta(conn, "setup_code_expires_at")
    if not stored or not expires:
        return False
    if not hmac.compare_digest(stored, sha256_hex(code)):
        return False
    return clock.now() < clock.parse_iso(expires)


def _mode_allowed(conn: sqlite3.Connection, mode: str) -> bool:
    return mode == "restore" or get_meta(conn, "auth_hash") is None


def _logged_in_response(request: Request, conn: sqlite3.Connection, name) -> Response:
    resp = Response(status_code=204)
    set_cookie(resp, request.app.state.settings, create_session(conn, clean_name(name) or ""))
    return resp


@router.get("/api/setup/status")
def setup_status(conn: sqlite3.Connection = Depends(get_db)):
    return {"initialized": get_meta(conn, "auth_hash") is not None}


@router.post("/api/setup", status_code=204)
async def setup(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    check_origin(request)
    body = await _read_auth_body(request, SetupIn)
    with attempt(request) as ip:
        shapes_ok = (
            MIN_ITER <= body.kdf_iterations <= MAX_ITER
            and _decoded_len(body.kdf_salt, 16)
            and _decoded_len(body.auth_key, 32)
            and _decoded_len(body.wrapped_mk, WRAPPED_MK_LEN)
        )
        # Every check runs before the code is spent, so a refused attempt leaves it usable.
        if not shapes_ok or not _mode_allowed(conn, body.mode) or not _code_valid(conn, body.setup_code):
            refuse(403)
        auth_hash = hash_secret(b64u_decode(body.auth_key))
        conn.execute("BEGIN IMMEDIATE")
        try:
            # lost a race for the same code, or another setup initialized the share meanwhile
            if not _code_valid(conn, body.setup_code) or not _mode_allowed(conn, body.mode):
                conn.execute("ROLLBACK")
                refuse(403)
            conn.execute("DELETE FROM meta WHERE key IN ('setup_code_hash','setup_code_expires_at')")
            set_meta(conn, "kdf_salt", body.kdf_salt)
            set_meta(conn, "kdf_iterations", str(body.kdf_iterations))
            set_meta(conn, "auth_hash", auth_hash)
            set_meta(conn, "wrapped_mk", body.wrapped_mk)
            if get_meta(conn, "key_version") is None:
                set_meta(conn, "key_version", "1")
            conn.execute("DELETE FROM sessions")
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        request.app.state.limiter.reset(ip)
        return _logged_in_response(request, conn, body.name)


@router.get("/api/kdf")
def kdf(conn: sqlite3.Connection = Depends(get_db)):
    salt = get_meta(conn, "kdf_salt")
    iterations = get_meta(conn, "kdf_iterations")
    if salt is None or iterations is None or get_meta(conn, "auth_hash") is None:
        raise api_error(409, "not_initialized")
    return {"kdf_salt": salt, "kdf_iterations": int(iterations)}


@router.post("/api/login", status_code=204)
async def login(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    check_origin(request)
    body = await _read_auth_body(request, LoginIn)
    with attempt(request) as ip:
        try:
            key = b64u_decode(body.auth_key)
        except ValueError:
            key = b""
        if len(key) != 32 or not verify_secret(key, get_meta(conn, "auth_hash")):
            refuse(401)
        request.app.state.limiter.reset(ip)
        return _logged_in_response(request, conn, body.name)


@router.post("/api/logout", status_code=204)
def logout(request: Request, _: Principal = Depends(require_session),
           conn: sqlite3.Connection = Depends(get_db)):
    settings = request.app.state.settings
    delete_session(conn, request.cookies.get(cookie_name(settings)))
    resp = Response(status_code=204)
    clear_cookie(resp, settings)
    return resp


@router.get("/api/keyblob")
def keyblob(_: Principal = Depends(require_session), conn: sqlite3.Connection = Depends(get_db)):
    return {"wrapped_mk": get_meta(conn, "wrapped_mk"),
            "key_version": int(get_meta(conn, "key_version") or 1)}


@router.post("/api/sessions/revoke-all", status_code=204)
def revoke_all(request: Request, _: Principal = Depends(require_session),
               conn: sqlite3.Connection = Depends(get_db)):
    """Sign out everywhere: every session, the caller's included."""
    conn.execute("DELETE FROM sessions")
    resp = Response(status_code=204)
    clear_cookie(resp, request.app.state.settings)
    return resp


@router.get("/api/sessions/self")
def get_session_self(p: Principal = Depends(require_session), conn: sqlite3.Connection = Depends(get_db)):
    return {"name": session_name(conn, p.session_hash)}


@router.patch("/api/sessions/self", status_code=204)
async def rename_session_self(request: Request, p: Principal = Depends(require_session),
                              conn: sqlite3.Connection = Depends(get_db)):
    name = clean_name((await read_json(request)).get("name"))
    if name is None:
        raise api_error(400, "bad_request", "name must be 1-40 printable characters")
    conn.execute("UPDATE sessions SET name=? WHERE id_hash=?", (name, p.session_hash))
    return Response(status_code=204)
