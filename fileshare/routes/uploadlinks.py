"""Inbound one-time upload links (upload-links spec). A stranger holding `https://<host>/u/<token>#<pub>`
seals one file to the link's public key and the owner adopts it (Task 3). The server holds the token
only as its SHA-256 and the link's private key only sealed under the owner's master key; it never
sees either key or any file content in the clear.
"""
import json
import re
import secrets
import sqlite3
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from fileshare import clock
from fileshare.deps import Principal, api_error, get_db, require_any
from fileshare.expiry import DEFAULT_TTL, expires_at
from fileshare.ids import format_id
from fileshare.routes import links
from fileshare.routes.files import (MAX_B64, MIN_ENC_META_LEN, SELECT_FILE, WRAPPED_DEK_LEN,
                                    _check_enc_meta, check_envelope, file_out_one, read_bounded_json,
                                    receive_sealed_blob)
from fileshare.security import b64u_decode, new_id, sha256_hex
from fileshare.sessions import session_name

router = APIRouter()

UUID_RE = re.compile(r"^[0-9a-f]{32}$")
UPLOAD_LINK_TTLS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
}
UPLOAD_LINK_ID_RE = re.compile(r"upl_[0-9a-f]{12}")
WRAPPED_LPRIV_LEN = 126
SEALED_DEK_LEN = 126
MAX_BODY = 2 * MAX_B64 + 1024   # wrapped_lpriv plus enc_label plus JSON framing
ADOPT_BODY_MAX = MAX_B64 + 1024   # wrapped_dek plus JSON framing

_NO_STORE = {"Cache-Control": "no-store"}

# A link is live for the public GET while none of these has happened yet.
_UL_LIVE = "revoked_at IS NULL AND expires_at > :now AND used_at IS NULL"

SELECT_UPLOAD_LINK = ("SELECT ul.*, d.name AS device_name FROM upload_links ul"
                      " LEFT JOIN devices d ON d.id = ul.created_by_device")


# --- owner endpoints ------------------------------------------------------------------------

def _state(row, now: str) -> str:
    if row["file_n"] is not None:
        return "received"
    if row["used_at"] is not None and row["file_uuid"] is not None:
        return "pending"
    if row["revoked_at"] is not None:
        return "revoked"
    if row["expires_at"] <= now:
        return "expired"
    return "waiting"


def upload_link_out(row, now: str) -> dict:
    if row["created_by_device"]:
        by = {"id": row["created_by_device"], "name": row["device_name"]}
    else:
        by = {"id": None, "name": row["created_by_session"] or "browser"}
    pending = None
    if row["file_n"] is None and row["used_at"] is not None and row["file_uuid"] is not None:
        pending = {"file_uuid": row["file_uuid"], "size": row["file_size"],
                   "sealed_dek": row["sealed_dek"], "enc_meta": row["enc_meta"]}
    return {
        "id": row["id"],
        "uuid": row["uuid"],
        "key_version": row["key_version"],
        "wrapped_lpriv": row["wrapped_lpriv"],
        "enc_label": row["enc_label"],
        "created_at": row["created_at"],
        "created_by": by,
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "used_at": row["used_at"],
        "state": _state(row, now),
        "pending": pending,
        "file": None if row["file_n"] is None else format_id(row["file_n"]),
    }


def _parse_upload_link_body(body: dict) -> tuple[str, int, str, str | None, str]:
    uuid = body.get("uuid")
    if not isinstance(uuid, str) or not UUID_RE.fullmatch(uuid):
        raise api_error(400, "bad_request", "uuid must be 32 lowercase hex characters")
    kv = body.get("key_version")
    if type(kv) is not int or not 1 <= kv <= 255:
        raise api_error(400, "bad_request", "key_version must be an integer in [1, 255]")
    wrapped = body.get("wrapped_lpriv")
    check_envelope(wrapped, "wrapped_lpriv", MAX_B64, lambda b: len(b) == WRAPPED_LPRIV_LEN,
                  code="bad_request")
    enc_label = body.get("enc_label")
    if enc_label is not None:
        check_envelope(enc_label, "enc_label", MAX_B64, lambda b: len(b) >= MIN_ENC_META_LEN,
                      code="bad_request")
    ttl = body.get("ttl")
    if not isinstance(ttl, str) or ttl not in UPLOAD_LINK_TTLS:
        raise api_error(400, "bad_request", "ttl must be one of " + ", ".join(UPLOAD_LINK_TTLS))
    return uuid, kv, wrapped, enc_label, ttl


@router.post("/api/upload-links", status_code=201)
async def create_upload_link(request: Request, principal: Principal = Depends(require_any),
                             conn: sqlite3.Connection = Depends(get_db)):
    uuid, kv, wrapped, enc_label, ttl = _parse_upload_link_body(
        await read_bounded_json(request, MAX_BODY))
    now = clock.now()
    expires = clock.now_iso(now + UPLOAD_LINK_TTLS[ttl])
    if principal.kind == "device":
        by_device, by_session = principal.device["id"], None
    else:
        by_device, by_session = None, session_name(conn, principal.session_hash) or "browser"
    link_id, token = new_id("upl"), secrets.token_urlsafe(32)
    try:
        conn.execute(
            "INSERT INTO upload_links (id, uuid, token_hash, key_version, wrapped_lpriv, enc_label,"
            " created_at, created_by_device, created_by_session, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (link_id, uuid, sha256_hex(token), kv, wrapped, enc_label, clock.now_iso(now),
             by_device, by_session, expires))
    except sqlite3.IntegrityError as e:
        if "upload_links.uuid" in str(e):
            raise api_error(409, "duplicate_uuid", "an upload link with this uuid already exists") from None
        raise
    # The token is returned exactly once; the server keeps only its hash.
    return JSONResponse({"id": link_id, "token": token, "expires_at": expires},
                        status_code=201, headers=_NO_STORE)


@router.get("/api/upload-links")
def list_upload_links(_: Principal = Depends(require_any), conn: sqlite3.Connection = Depends(get_db)):
    now = clock.now_iso()
    rows = conn.execute(SELECT_UPLOAD_LINK + " ORDER BY ul.created_at DESC, ul.rowid DESC").fetchall()
    return JSONResponse({"links": [upload_link_out(r, now) for r in rows]}, headers=_NO_STORE)


@router.delete("/api/upload-links/{link_id}", status_code=204)
def revoke_upload_link(link_id: str, request: Request, _: Principal = Depends(require_any),
                       conn: sqlite3.Connection = Depends(get_db)):
    if not UPLOAD_LINK_ID_RE.fullmatch(link_id):
        raise api_error(404, "not_found", "no such upload link")
    # BEGIN IMMEDIATE, so a concurrent adopt (also BEGIN IMMEDIATE) can't land between our read of
    # `file_n IS NULL` and our UPDATE: the pending UPDATE below is itself guarded by that same
    # condition, and only the one that actually cleared the pending columns deletes the blob, and
    # only after COMMIT (an adopted file must never lose the blob it points at).
    blob_to_delete = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM upload_links WHERE id = ?", (link_id,)).fetchone()
        if row is None:
            raise api_error(404, "not_found", "no such upload link")
        if row["file_n"] is not None:
            conn.execute("COMMIT")
            return Response(status_code=204)     # received: nothing left to undo
        ts = clock.now_iso()
        if row["used_at"] is not None and row["file_uuid"] is not None:
            # pending: throw away the bad upload (the blob and its pending columns), and mark it
            # gone -- unless an adopt won the race since our SELECT, in which case there is nothing
            # left to revoke (the row is now "received" and its blob must stay put).
            cur = conn.execute(
                "UPDATE upload_links SET file_uuid = NULL, file_size = NULL, sealed_dek = NULL,"
                " enc_meta = NULL, revoked_at = COALESCE(revoked_at, ?)"
                " WHERE id = ? AND file_n IS NULL", (ts, link_id))
            if cur.rowcount == 1:
                blob_to_delete = row["file_uuid"]
        else:
            conn.execute("UPDATE upload_links SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                         (ts, link_id))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    if blob_to_delete is not None:
        request.app.state.blobs.delete(blob_to_delete)
    return Response(status_code=204)


# --- public endpoint (no auth) ---------------------------------------------------------------

@router.get("/api/public/u/{token}")
def public_upload_link_meta(token: str, request: Request, _l: None = Depends(links.public_limit),
                            conn: sqlite3.Connection = Depends(get_db)):
    h = links._token_hash(token)
    row = conn.execute(
        "SELECT uuid, key_version, expires_at FROM upload_links WHERE token_hash = :h AND " + _UL_LIVE,
        {"h": h, "now": clock.now_iso()}).fetchone()
    if row is None:
        raise links._gone()
    return JSONResponse({"uuid": row["uuid"], "key_version": row["key_version"],
                         "max_bytes": request.app.state.settings.max_upload,
                         "expires_at": row["expires_at"]},
                        headers=links._PUBLIC_HEADERS)


class _DuplicateUuid(Exception):
    pass


class _LinkGone(Exception):
    pass


def _parse_drop_meta(raw) -> dict:
    """Like files._parse_meta, but the sealed_dek envelope shape differs from wrapped_dek's: it
    begins with an ephemeral public key (0x04), not a v1 seal header."""
    def bad(why):
        return api_error(400, "bad_meta", why)
    if not isinstance(raw, str):
        raise bad("meta part missing")
    try:
        m = json.loads(raw)
    except ValueError:
        raise bad("meta is not JSON")
    if not isinstance(m, dict):
        raise bad("meta must be a JSON object")
    uuid, kv = m.get("uuid"), m.get("key_version")
    if not isinstance(uuid, str) or not UUID_RE.fullmatch(uuid):
        raise bad("uuid must be 32 lowercase hex characters")
    if type(kv) is not int or not 1 <= kv <= 255:
        raise bad("key_version must be an integer in [1, 255]")
    sealed = m.get("sealed_dek")
    if not isinstance(sealed, str) or not 1 <= len(sealed) <= MAX_B64:
        raise bad(f"sealed_dek must be a base64url string of at most {MAX_B64} chars")
    try:
        decoded = b64u_decode(sealed)
    except ValueError:
        raise bad("sealed_dek is not base64url")
    if len(decoded) != SEALED_DEK_LEN or decoded[0:1] != b"\x04" or decoded[65:66] != b"\x01":
        raise bad("sealed_dek is not the expected shape")
    _check_enc_meta(m.get("enc_meta"))
    return {"uuid": uuid, "key_version": kv, "sealed_dek": sealed, "enc_meta": m["enc_meta"]}


@router.post("/api/public/u/{token}", status_code=201)
async def public_upload(token: str, request: Request, _l: None = Depends(links.public_limit),
                        conn: sqlite3.Connection = Depends(get_db)):
    store = request.app.state.blobs
    h = links._token_hash(token)
    row = conn.execute(
        "SELECT key_version FROM upload_links WHERE token_hash = :h AND " + _UL_LIVE,
        {"h": h, "now": clock.now_iso()}).fetchone()
    if row is None:
        raise links._gone()          # cheap reject before reading any body

    def parse_meta(raw):
        m = _parse_drop_meta(raw)
        if m["key_version"] != row["key_version"]:
            raise api_error(400, "bad_meta", "key_version does not match the link")
        return m

    meta, tmp, size = await receive_sealed_blob(request, parse_meta)

    # One transaction: the single-use claim on the token and the blob's rename into place. The
    # blob moves only after the UPDATE claimed the link and before COMMIT, so a lost race (someone
    # else's upload already claimed this token) or any failure leaves neither an orphan blob nor a
    # used-up link (global-constraints.md Review Focus 2).
    moved = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
                "SELECT 1 FROM files WHERE uuid = ? UNION ALL"
                " SELECT 1 FROM upload_links WHERE file_uuid = ?",
                (meta["uuid"], meta["uuid"])).fetchone():
            raise _DuplicateUuid()
        now = clock.now_iso()
        cur = conn.execute(
            "UPDATE upload_links SET used_at = ?, file_uuid = ?, file_size = ?, sealed_dek = ?, enc_meta = ?"
            " WHERE token_hash = ? AND used_at IS NULL AND revoked_at IS NULL AND expires_at > ?",
            (now, meta["uuid"], size, meta["sealed_dek"], meta["enc_meta"], h, now))
        if cur.rowcount != 1:
            raise _LinkGone()
        store.commit(tmp, meta["uuid"])
        moved = True
        conn.execute("COMMIT")
    except BaseException as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if moved:
            store.delete(meta["uuid"])
        store.discard(tmp)
        if isinstance(exc, _LinkGone):
            raise links._gone() from None
        if isinstance(exc, _DuplicateUuid):
            raise api_error(409, "duplicate_uuid", "a file with this uuid already exists") from None
        raise
    return JSONResponse({"ok": True, "size": size}, status_code=201, headers=links._PUBLIC_HEADERS)


@router.post("/api/upload-links/{link_id}/adopt", status_code=201)
async def adopt_upload_link(link_id: str, request: Request, _: Principal = Depends(require_any),
                            conn: sqlite3.Connection = Depends(get_db)):
    body = await read_bounded_json(request, ADOPT_BODY_MAX)
    wrapped_dek = body.get("wrapped_dek")
    check_envelope(wrapped_dek, "wrapped_dek", MAX_B64, lambda b: len(b) == WRAPPED_DEK_LEN, code="bad_request")
    if not UPLOAD_LINK_ID_RE.fullmatch(link_id):
        raise api_error(404, "not_found", "no such upload link")
    store = request.app.state.blobs
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM upload_links WHERE id = ?", (link_id,)).fetchone()
        if row is None:
            raise api_error(404, "not_found", "no such upload link")
        if row["file_n"] is not None:
            raise api_error(409, "already_adopted", "this link has already been adopted")
        if row["file_uuid"] is None:
            raise api_error(409, "not_pending", "this link has no pending upload")
        if not store.path_for(row["file_uuid"]).is_file():
            # The upload's blob is gone (e.g. an interrupted disk write): clear the dangling pending
            # columns and mark the link revoked, so it reports truthfully as "revoked" (not
            # "waiting" or "expired") and the purge sweep's discarded rule eventually collects it.
            # Commit this cleanup now, since this whole request still ends in an error.
            conn.execute(
                "UPDATE upload_links SET file_uuid = NULL, file_size = NULL, sealed_dek = NULL,"
                " enc_meta = NULL, revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
                (clock.now_iso(), link_id))
            conn.execute("COMMIT")
            raise api_error(409, "not_pending", "the uploaded blob is missing")
        now = clock.now()
        now_iso = clock.now_iso(now)
        expires = expires_at(DEFAULT_TTL, now)
        cur = conn.execute(
            "INSERT INTO files (uuid, size, key_version, wrapped_dek, enc_meta, device_id, project,"
            " created_at, session_name, expires_at)"
            " VALUES (?, ?, ?, ?, ?, NULL, 'upload', ?, 'upload link', ?)",
            (row["file_uuid"], row["file_size"], row["key_version"], wrapped_dek, row["enc_meta"],
             now_iso, expires))
        n = cur.lastrowid
        upd = conn.execute(
            "UPDATE upload_links SET file_n = ?, adopted_at = ?, sealed_dek = NULL"
            " WHERE id = ? AND file_n IS NULL",
            (n, now_iso, link_id))
        if upd.rowcount != 1:
            # Someone else adopted this link between our SELECT and our INSERT/UPDATE.
            raise api_error(409, "already_adopted", "this link has already been adopted")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    file_row = conn.execute(SELECT_FILE + " WHERE f.n = ?", (n,)).fetchone()
    return JSONResponse(file_out_one(conn, file_row), status_code=201, headers=_NO_STORE)
