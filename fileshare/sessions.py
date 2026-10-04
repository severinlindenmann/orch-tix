"""Server-side sessions: the cookie is a random token, the DB holds only its sha256.

A session lives until it has been idle for IDLE_S (30 days, sliding); there is no absolute cap
(spec §14 A). Renewal is throttled: `last_used_at` is rewritten at most once per RENEW_EVERY_S,
and every rewrite re-sends the cookie with a fresh max_age. So `last_used_at` is also "when the
browser last got a fresh cookie", and the cookie can never outlive the server-side session by
more than that hour.
"""
import sqlite3

from starlette.datastructures import MutableHeaders
from starlette.responses import Response

from fileshare import clock
from fileshare.security import new_session_token, sha256_hex

IDLE_S = 30 * 24 * 3600
RENEW_EVERY_S = 3600
NAME_MAX = 40


def cookie_name(settings) -> str:
    # __Host- requires Secure, Path=/ and no Domain; plain-http tests can't send it.
    return "__Host-fs_session" if settings.cookie_secure else "fs_session"


def clean_name(value) -> str | None:
    """A session name is 1–40 printable characters after stripping; anything else is None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not 1 <= len(value) <= NAME_MAX or not value.isprintable():
        return None
    return value


def create_session(conn: sqlite3.Connection, name: str = "") -> str:
    token = new_session_token()
    ts = clock.now_iso()
    conn.execute("INSERT INTO sessions(id_hash, created_at, last_used_at, name) VALUES (?,?,?,?)",
                 (sha256_hex(token), ts, ts, name))
    return token


def renew_session(conn: sqlite3.Connection, token: str | None) -> tuple[str | None, bool]:
    """(id_hash or None, renewed). `renewed` means the caller should re-send the cookie."""
    if not token:
        return None, False
    h = sha256_hex(token)
    row = conn.execute("SELECT last_used_at FROM sessions WHERE id_hash=?", (h,)).fetchone()
    if row is None:
        return None, False
    now = clock.now()
    idle = (now - clock.parse_iso(row["last_used_at"])).total_seconds()
    if idle > IDLE_S:
        conn.execute("DELETE FROM sessions WHERE id_hash=?", (h,))
        return None, False
    if idle < RENEW_EVERY_S:
        return h, False
    conn.execute("UPDATE sessions SET last_used_at=? WHERE id_hash=?", (clock.now_iso(now), h))
    return h, True


def resolve_session(conn: sqlite3.Connection, token: str | None) -> str | None:
    return renew_session(conn, token)[0]


def session_name(conn: sqlite3.Connection, id_hash: str) -> str:
    row = conn.execute("SELECT name FROM sessions WHERE id_hash=?", (id_hash,)).fetchone()
    return "" if row is None else row["name"]


def delete_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE id_hash=?", (sha256_hex(token),))


def set_cookie(response, settings, token: str) -> None:
    response.set_cookie(cookie_name(settings), token, max_age=IDLE_S, path="/",
                        secure=settings.cookie_secure, httponly=True, samesite="strict")


def clear_cookie(response, settings) -> None:
    response.delete_cookie(cookie_name(settings), path="/", secure=settings.cookie_secure,
                           httponly=True, samesite="strict")


class CookieRefreshMiddleware:
    """Re-sends the session cookie with a fresh max_age when require_session renewed it.

    require_session leaves the token in `scope["state"]["renew_cookie"]`. A response that
    already sets or clears the session cookie itself (login, logout, revoke-all) wins. Pure
    ASGI, like SecurityHeadersMiddleware, so streamed responses pass through untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})   # the same dict request.state writes into

        async def send_with_cookie(message):
            token = state.get("renew_cookie")
            if message["type"] == "http.response.start" and token:
                settings = scope["app"].state.settings
                prefix = (cookie_name(settings) + "=").encode("latin-1")
                headers = MutableHeaders(scope=message)
                if not any(k == b"set-cookie" and v.startswith(prefix) for k, v in headers.raw):
                    fresh = Response()
                    set_cookie(fresh, settings, token)
                    headers.append("set-cookie", fresh.headers["set-cookie"])
            await send(message)

        await self.app(scope, receive, send_with_cookie)
