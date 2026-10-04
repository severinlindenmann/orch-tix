"""Public links (spec §17): https://<host>/p/<token>#<link key>.

The server holds a random token (as SHA-256 only) and `wrapped_dek_link`, the file's DEK sealed
under a link key that lives only in the URL fragment. It never sees that key, so it can serve the
ciphertext but never read it.
"""
import logging
import re
import secrets
import sqlite3
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from fileshare import clock
from fileshare.deps import Principal, api_error, client_ip, get_db, require_any
from fileshare.ids import format_id
from fileshare.routes.files import MAX_B64, WRAPPED_DEK_LEN, _load, check_envelope, read_bounded_json
from fileshare.routes.pages import STATIC_DIR  # noqa: F401  (re-exported for tests)
from fileshare.security import new_id, sha256_hex
from fileshare.sessions import session_name

router = APIRouter()

LINK_TTLS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
MAX_DOWNLOADS = 1000
MAX_BODY = MAX_B64 + 1024                  # wrapped_dek_link plus JSON framing
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")      # 32 random bytes, base64url
LINK_ID_RE = re.compile(r"lnk_[0-9a-f]{12}")
PUBLIC_LIMIT, PUBLIC_WINDOW_S = 60, 60

_PUBLIC_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
_NO_STORE = {"Cache-Control": "no-store"}

# A link is live while all of these hold. The download claim repeats them inside its UPDATE.
_LIVE = ("l.revoked_at IS NULL AND l.expires_at > :now"
         " AND (l.max_downloads IS NULL OR l.downloads < l.max_downloads)"
         " AND f.deleted_at IS NULL AND (f.expires_at IS NULL OR f.expires_at > :now)")

SELECT_LINK = ("SELECT l.*, d.name AS device_name FROM links l"
               " JOIN files f ON f.n = l.file_n"
               " LEFT JOIN devices d ON d.id = l.created_by_device")


# --- log redaction ------------------------------------------------------------------------

_TOKEN_PATH_RE = re.compile(r"(/p/|/u/|/api/public/)[^\s\"]*")


def redact_paths(text: str) -> str:
    return _TOKEN_PATH_RE.sub(r"\1<redacted>", text)


class RedactTokens(logging.Filter):
    """Rewrites /p/<anything> and /api/public/<anything> to …/<redacted> in a log record.

    Installed on uvicorn's loggers: an access line carries the path, and the path carries the token."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and not record.exc_text:
            # Format the traceback now, redacted; handlers reuse exc_text instead of re-formatting.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if isinstance(record.exc_text, str):
            record.exc_text = redact_paths(record.exc_text)
            record.exc_info = None     # nothing may format the raw traceback again
        if isinstance(record.stack_info, str):
            record.stack_info = redact_paths(record.stack_info)
        if isinstance(record.msg, str):
            record.msg = redact_paths(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_paths(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact_paths(v) if isinstance(v, str) else v for k, v in record.args.items()}
        return True


def install_log_redaction() -> None:
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactTokens) for f in logger.filters):
            logger.addFilter(RedactTokens())


def revoke_device_links(conn: sqlite3.Connection, device_id: str, ts: str) -> int:
    """Revoking a device also revokes the live public links and the unused upload links it created."""
    n = conn.execute("UPDATE links SET revoked_at = ? WHERE created_by_device = ? AND revoked_at IS NULL",
                     (ts, device_id)).rowcount
    conn.execute(
        "UPDATE upload_links SET revoked_at = ? WHERE created_by_device = ? AND revoked_at IS NULL"
        " AND used_at IS NULL",
        (ts, device_id))
    return n


# --- owner endpoints ------------------------------------------------------------------------

def link_out(row) -> dict:
    if row["created_by_device"]:
        by = {"id": row["created_by_device"], "name": row["device_name"]}
    else:
        by = {"id": None, "name": row["created_by_session"] or "browser"}
    return {
        "id": row["id"],
        "file": format_id(row["file_n"]),
        "created_at": row["created_at"],
        "created_by": by,
        "expires_at": row["expires_at"],
        "downloads": row["downloads"],
        "max_downloads": row["max_downloads"],
    }


def _load_live_file(request: Request, conn, ref: str):
    """The file, or 404 for an unknown, deleted or expired one (§17: a link needs a live file)."""
    try:
        return _load(request, conn, ref)
    except HTTPException as e:
        if e.status_code == 410:
            raise api_error(404, "not_found", f"{ref} was deleted or has expired") from None
        raise


def _parse_link_body(body: dict) -> tuple[str, str, int | None]:
    wrapped = body.get("wrapped_dek_link")
    check_envelope(wrapped, "wrapped_dek_link", MAX_B64, lambda b: len(b) == WRAPPED_DEK_LEN, code="bad_request")
    ttl = body.get("ttl")
    if not isinstance(ttl, str) or ttl not in LINK_TTLS:
        raise api_error(400, "bad_request", "ttl must be one of " + ", ".join(LINK_TTLS))
    max_dl = body.get("max_downloads")
    if max_dl is not None and (type(max_dl) is not int or not 1 <= max_dl <= MAX_DOWNLOADS):
        raise api_error(400, "bad_request", f"max_downloads must be an integer in [1, {MAX_DOWNLOADS}] or null")
    return wrapped, ttl, max_dl


@router.post("/api/files/{ref}/links", status_code=201)
async def create_link(ref: str, request: Request, principal: Principal = Depends(require_any),
                      conn: sqlite3.Connection = Depends(get_db)):
    row = _load_live_file(request, conn, ref)
    wrapped, ttl, max_dl = _parse_link_body(await read_bounded_json(request, MAX_BODY))
    now = clock.now()
    expires = clock.now_iso(now + LINK_TTLS[ttl])
    if row["expires_at"] is not None and row["expires_at"] < expires:
        expires = row["expires_at"]                    # never outlives the file
    if principal.kind == "device":
        by_device, by_session = principal.device["id"], None
    else:
        by_device, by_session = None, session_name(conn, principal.session_hash) or "browser"
    link_id, token = new_id("lnk"), secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO links (id, file_n, token_hash, wrapped_dek_link, created_at, created_by_device,"
        " created_by_session, expires_at, max_downloads) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (link_id, row["n"], sha256_hex(token), wrapped, clock.now_iso(now), by_device, by_session,
         expires, max_dl))
    # The token is returned exactly once; the server keeps only its hash.
    return JSONResponse({"id": link_id, "token": token, "expires_at": expires, "max_downloads": max_dl},
                        status_code=201, headers=_NO_STORE)


def _live_links(conn, file_n: int | None = None) -> list[dict]:
    """Live links, newest first, for one file or (file_n None) for every file. Never the token."""
    rows = conn.execute(
        SELECT_LINK + " WHERE " + _LIVE + " AND (:n IS NULL OR l.file_n = :n)"
        " ORDER BY l.created_at DESC, l.rowid DESC",
        {"now": clock.now_iso(), "n": file_n}).fetchall()
    return [link_out(r) for r in rows]


@router.get("/api/files/{ref}/links")
def list_file_links(ref: str, request: Request, _: Principal = Depends(require_any),
                    conn: sqlite3.Connection = Depends(get_db)):
    row = _load(request, conn, ref)
    return JSONResponse({"links": _live_links(conn, row["n"])}, headers=_NO_STORE)


@router.get("/api/links")
def list_all_links(_: Principal = Depends(require_any), conn: sqlite3.Connection = Depends(get_db)):
    return JSONResponse({"links": _live_links(conn)}, headers=_NO_STORE)


@router.delete("/api/links/{link_id}", status_code=204)
def revoke_link(link_id: str, _: Principal = Depends(require_any), conn: sqlite3.Connection = Depends(get_db)):
    if not LINK_ID_RE.fullmatch(link_id):
        raise api_error(404, "not_found", "no such link")
    cur = conn.execute("UPDATE links SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                       (clock.now_iso(), link_id))
    if cur.rowcount == 0 and conn.execute("SELECT 1 FROM links WHERE id = ?", (link_id,)).fetchone() is None:
        raise api_error(404, "not_found", "no such link")
    return Response(status_code=204)


# --- public endpoints (no auth) ---------------------------------------------------------

def _gone() -> HTTPException:
    # One answer for unknown, revoked, expired, used up and deleted: nothing to tell them apart.
    return HTTPException(404, detail={"error": "not_found", "detail": "this link has expired or was revoked"},
                         headers=_PUBLIC_HEADERS)


def public_limit(request: Request) -> None:
    if not request.app.state.public_limiter.allow(client_ip(request)):
        raise HTTPException(429, detail={"error": "rate_limited", "detail": "too many requests; try again soon"},
                            headers=_PUBLIC_HEADERS)


def _token_hash(token: str) -> str:
    if not TOKEN_RE.fullmatch(token):
        raise _gone()
    return sha256_hex(token)


def claim_download(conn: sqlite3.Connection, token_hash: str):
    """Count one download, atomically, and return the file row; None if the link is not live.

    The whole check is inside the single UPDATE, so two concurrent downloads of the last
    remaining use can't both succeed."""
    now = clock.now_iso()
    cur = conn.execute(
        "UPDATE links SET downloads = downloads + 1 WHERE token_hash = ? AND revoked_at IS NULL"
        " AND expires_at > ? AND (max_downloads IS NULL OR downloads < max_downloads)"
        " AND file_n IN (SELECT n FROM files WHERE deleted_at IS NULL AND (expires_at IS NULL OR expires_at > ?))",
        (token_hash, now, now))
    if cur.rowcount != 1:
        return None
    return conn.execute("SELECT f.n, f.uuid FROM links l JOIN files f ON f.n = l.file_n WHERE l.token_hash = ?",
                        (token_hash,)).fetchone()


@router.get("/api/public/{token}")
def public_meta(token: str, _l: None = Depends(public_limit), conn: sqlite3.Connection = Depends(get_db)):
    h = _token_hash(token)
    row = conn.execute(
        "SELECT l.wrapped_dek_link, l.expires_at, l.max_downloads, l.downloads, f.uuid, f.key_version, f.enc_meta, f.size, f.created_at"
        " FROM links l JOIN files f ON f.n = l.file_n WHERE l.token_hash = :h AND " + _LIVE,
        {"h": h, "now": clock.now_iso()}).fetchone()
    if row is None:
        raise _gone()
    return JSONResponse({"uuid": row["uuid"], "key_version": row["key_version"],
                         "wrapped_dek_link": row["wrapped_dek_link"], "enc_meta": row["enc_meta"],
                         "size": row["size"], "created_at": row["created_at"], "expires_at": row["expires_at"],
                         # §17: lets the viewer avoid spending a limited link's download on a preview.
                         "downloads_left": None if row["max_downloads"] is None
                         else row["max_downloads"] - row["downloads"]},
                        headers=_PUBLIC_HEADERS)


@router.get("/api/public/{token}/blob")
def public_blob(token: str, request: Request, _l: None = Depends(public_limit),
                conn: sqlite3.Connection = Depends(get_db)):
    h = _token_hash(token)
    row = conn.execute("SELECT f.uuid FROM links l JOIN files f ON f.n = l.file_n WHERE l.token_hash = ?",
                       (h,)).fetchone()
    # Check the blob first, so a missing blob never uses up a download.
    path = None if row is None else request.app.state.blobs.path_for(row["uuid"])
    if path is None or not path.is_file() or claim_download(conn, h) is None:
        raise _gone()
    return FileResponse(path, media_type="application/octet-stream", headers=_PUBLIC_HEADERS)


@router.get("/api/public/{rest:path}")
def public_other(rest: str, _l: None = Depends(public_limit)):
    raise _gone()
