import ipaddress
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, NoReturn

from fastapi import Depends, HTTPException, Request

from fileshare import clock
from fileshare.db import connect
from fileshare.security import sha256_hex
from fileshare.sessions import cookie_name, renew_session

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LAST_SEEN_EVERY_S = 60


def api_error(status: int, code: str, detail: str = "") -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "detail": detail})


@contextmanager
def rate_slot(limiter, key: str, detail: str) -> Iterator[None]:
    """Spend one WindowLimiter slot for the block (429 rate_limited when none is left); the slot is given
    back when the block fails (a rollback, a duplicate, uuid_taken, ...), so only successes count."""
    if not limiter.allow(key):
        raise api_error(429, "rate_limited", detail)
    try:
        yield
    except BaseException:
        limiter.refund(key)
        raise


async def read_json(request: Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise api_error(400, "bad_request", "body must be a JSON object")
    if not isinstance(body, dict):
        raise api_error(400, "bad_request", "body must be a JSON object")
    return body


class Refused(Exception):
    """Raised inside attempt(): the attempt counts as failed and becomes this error response."""

    def __init__(self, status: int, code: str = "refused", detail: str = ""):
        self.status, self.code, self.detail = status, code, detail


def refuse(status: int, code: str = "refused", detail: str = "") -> NoReturn:
    raise Refused(status, code, detail)


@contextmanager
def attempt(request: Request) -> Iterator[str]:
    """Reserve a rate-limit slot for one login/setup/handshake attempt and always release it.

    The slot is taken before any work, so parallel requests from one IP cannot all pass
    the limit check before their failures are recorded. Only a Refused counts as failed."""
    limiter = request.app.state.limiter
    ip = client_ip(request)
    if not limiter.try_begin(ip):
        raise api_error(429, "rate_limited")
    failed = False
    try:
        yield ip
    except Refused as exc:
        failed = True
        raise api_error(exc.status, exc.code, exc.detail) from None
    finally:
        limiter.end(ip, failed=failed)


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _valid_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    # An IPv6 scope id ("fe80::1%<anything>") is accepted verbatim, CR/LF included.
    if getattr(ip, "scope_id", None) is not None:
        return None
    return str(ip)


def client_ip(request: Request) -> str:
    """Always a valid IP or "unknown": the result lands in a fail2ban-parsed log line,
    so CR/LF or other junk must never get through."""
    # X-Real-IP is trusted only because uvicorn binds 127.0.0.1 behind Caddy, which sets it.
    return (_valid_ip(request.headers.get("X-Real-IP"))
            or _valid_ip(request.client.host if request.client else None)
            or "unknown")


def check_origin(request: Request) -> None:
    public_url = request.app.state.settings.public_url
    origin = request.headers.get("Origin")
    if not origin:
        raise api_error(403, "origin_required", f"send the header `Origin: {public_url}`")
    if origin != public_url:
        raise api_error(403, "bad_origin", f"the Origin header must be exactly {public_url}")


@dataclass
class Principal:
    kind: Literal["session", "device"]
    device: sqlite3.Row | None = None
    session_hash: str | None = None     # sessions.id_hash for kind == "session"


def require_session(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Principal:
    if request.method not in _SAFE_METHODS:
        check_origin(request)
    token = request.cookies.get(cookie_name(request.app.state.settings))
    h, renewed = renew_session(conn, token)
    if h is None:
        raise api_error(401, "unauthenticated")
    if renewed:
        # sessions.CookieRefreshMiddleware re-sends the cookie with a fresh 30-day max_age
        request.state.renew_cookie = token
    return Principal(kind="session", session_hash=h)


def _bearer(request: Request) -> str | None:
    raw = request.headers.get("Authorization", "")
    scheme, _, token = raw.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token.startswith("shd_"):
        return None
    return token


def _device_principal(request: Request, conn: sqlite3.Connection) -> Principal:
    """Authenticates the bearer and rejects revoked devices; says nothing yet about approval."""
    token = _bearer(request)
    if token is None:
        raise api_error(401, "unauthenticated")
    row = conn.execute("SELECT * FROM devices WHERE token_hash=?", (sha256_hex(token),)).fetchone()
    if row is None:
        raise api_error(401, "unauthenticated")
    if row["revoked_at"] is not None:
        raise api_error(401, "revoked")
    now = clock.now()
    last = row["last_seen_at"]
    if last is None or (now - clock.parse_iso(last)).total_seconds() >= LAST_SEEN_EVERY_S:
        conn.execute("UPDATE devices SET last_seen_at=? WHERE id=?", (clock.now_iso(now), row["id"]))
    return Principal(kind="device", device=row)


def require_device(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Principal:
    p = _device_principal(request, conn)
    if p.device["approved_at"] is None:
        # §6: a pending device may only fetch its bundle or delete itself
        raise api_error(403, "pending", "approve this device's fingerprint in the web UI")
    return p


def require_device_any_state(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Principal:
    return _device_principal(request, conn)


def require_any(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> Principal:
    if request.headers.get("Authorization"):
        return require_device(request, conn)
    return require_session(request, conn)
