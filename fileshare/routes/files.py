import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from starlette.datastructures import UploadFile

from fileshare import clock
from fileshare.blobs import BlobError, TooLarge
from fileshare.deps import Principal, api_error, get_db, read_json, require_any
from fileshare.expiry import DEFAULT_TTL, TTLS, expire_files, expires_at, tombstone
from fileshare.ids import format_id, parse_ref
from fileshare.security import b64u_decode
from fileshare.sessions import session_name
from fileshare.tags import BadTag, normalize_tags, set_tags, tags_for

router = APIRouter()

UUID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_B64 = 4096                  # wrapped_dek (exact size is checked too)
MAX_ENC_META_B64 = 524288       # enc_meta; raised for transcripts (§14 G)
WRAPPED_DEK_LEN = 1 + 12 + 32 + 16
MIN_ENC_META_LEN = 1 + 12 + 16
MULTIPART_SLACK = 64 * 1024
META_BODY_SLACK = 4096          # JSON framing around enc_meta in PATCH .../meta
MAX_TAGS_BODY = 4096            # PUT .../tags: 10 tags of 40 chars fit many times over
READ_SIZE = 1 << 20

SELECT_FILE = ("SELECT f.*, d.name AS device_name FROM files f"
               " LEFT JOIN devices d ON d.id = f.device_id")


def file_out(row, tags: list[str]) -> dict:
    gone = row["deleted_at"] is not None
    return {
        "id": format_id(row["n"]),
        "n": row["n"],
        "uuid": row["uuid"],
        "size": row["size"],
        "key_version": row["key_version"],
        "wrapped_dek": None if gone else row["wrapped_dek"],
        "enc_meta": None if gone else row["enc_meta"],
        "device": _uploader(row),
        "project": row["project"],
        "created_at": row["created_at"],
        "deleted_at": row["deleted_at"],
        "acked_at": row["acked_at"],
        "acked_by": row["acked_by"],
        "expires_at": row["expires_at"],
        "tags": [] if gone else tags,
    }


def file_out_one(conn, row) -> dict:
    return file_out(row, tags_for(conn, [row["n"]]).get(row["n"], []))


def _bad_tag(e: BadTag):
    return api_error(400, "bad_tag", str(e))


def _uploader(row) -> dict:
    if row["device_id"]:
        return {"id": row["device_id"], "name": row["device_name"]}
    # a browser upload: the session's name at upload time (§14 B)
    return {"id": None, "name": row["session_name"] or "browser"}


def _int_param(raw: str, lo: int, hi: int, name: str) -> int:
    if not raw.isdigit() or not lo <= int(raw) <= hi:
        raise api_error(400, "bad_request", f"{name} must be an integer in [{lo}, {hi}]")
    return int(raw)


def _load(request: Request, conn, ref: str):
    n = parse_ref(ref)
    row = None if n is None else conn.execute(SELECT_FILE + " WHERE f.n = ?", (n,)).fetchone()
    if row is None:
        raise api_error(404, "not_found", f"no file {ref}")
    now_ts = clock.now_iso()
    if row["deleted_at"] is None and row["expires_at"] is not None and row["expires_at"] <= now_ts:
        # Expiry is exact: a file past expires_at is gone now, not at the next hourly sweep.
        tombstone(conn, request.app.state.blobs, row["n"], row["uuid"], now_ts)
        row = conn.execute(SELECT_FILE + " WHERE f.n = ?", (n,)).fetchone()
    if row["deleted_at"] is not None:
        raise HTTPException(410, detail={"error": "deleted",
                                         "detail": f"{format_id(n)} was deleted",
                                         "file": file_out(row, [])})
    return row


def _parse_meta(raw) -> dict:
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
    check_envelope(m.get("wrapped_dek"), "wrapped_dek", MAX_B64, lambda b: len(b) == WRAPPED_DEK_LEN)
    _check_enc_meta(m.get("enc_meta"))
    ttl = m.get("ttl", DEFAULT_TTL)
    if not isinstance(ttl, str) or ttl not in TTLS:
        raise bad("ttl must be one of " + ", ".join(TTLS))
    try:
        tags = normalize_tags(m.get("tags", []))
    except BadTag as e:
        raise _bad_tag(e)
    return {"uuid": uuid, "key_version": kv, "wrapped_dek": m["wrapped_dek"], "enc_meta": m["enc_meta"],
            "ttl": ttl, "tags": tags}


def check_envelope(v, field: str, max_len: int, size_ok, code: str = "bad_meta") -> None:
    """Shape only: a base64url v1 envelope of a plausible size. The server has no key."""
    def bad(why):
        return api_error(400, code, why)
    if not isinstance(v, str) or not 1 <= len(v) <= max_len:
        raise bad(f"{field} must be a base64url string of at most {max_len} chars")
    try:
        decoded = b64u_decode(v)
    except ValueError:
        raise bad(f"{field} is not base64url")
    if decoded[:1] != b"\x01" or not size_ok(decoded):
        raise bad(f"{field} is not a v1 envelope of the expected size")


def _check_enc_meta(v) -> None:
    check_envelope(v, "enc_meta", MAX_ENC_META_B64, lambda b: len(b) >= MIN_ENC_META_LEN)


async def read_bounded_json(request: Request, max_bytes: int) -> dict:
    """Like read_json, but never buffers more than max_bytes, with or without Content-Length."""
    def too_large():
        return api_error(413, "too_large", f"body is limited to {max_bytes} bytes")
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > max_bytes:
        raise too_large()
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > max_bytes:
            raise too_large()
    try:
        body = json.loads(bytes(buf))
    except (ValueError, UnicodeDecodeError):
        raise api_error(400, "bad_request", "body must be a JSON object")
    if not isinstance(body, dict):
        raise api_error(400, "bad_request", "body must be a JSON object")
    return body


@router.get("/api/files")
def list_files(request: Request, limit: str = "50", before: str | None = None, acked: str = "0",
               tag: list[str] = Query(default=[]),
               _: Principal = Depends(require_any), conn: sqlite3.Connection = Depends(get_db)):
    expire_files(conn, request.app.state.blobs)   # exact expiry; cheap via the files_expires index
    lim = _int_param(limit, 1, 200, "limit")
    bef = None if before is None else _int_param(before, 1, 2 ** 62, "before")
    show_acked = _int_param(acked, 0, 1, "acked")
    try:
        # the same rules as stored tags: more than 10 distinct ones could never all match one file
        want = normalize_tags(tag)
    except BadTag as e:
        raise _bad_tag(e)
    # Every filter lives in SQL, ahead of LIMIT, so pages stay full and the cursor exact.
    # Tombstones are always listed, acknowledged or not; they carry no tags, so a tag filter drops them.
    sql = (SELECT_FILE + " WHERE (? IS NULL OR f.n < ?)"
           " AND (? = 1 OR f.acked_at IS NULL OR f.deleted_at IS NOT NULL)")
    params: list = [bef, bef, show_acked]
    for t in want:   # the file must carry ALL the given tags
        sql += " AND EXISTS (SELECT 1 FROM file_tags ft WHERE ft.file_n = f.n AND ft.tag = ?)"
        params.append(t)
    rows = conn.execute(sql + " ORDER BY f.n DESC LIMIT ?", (*params, lim + 1)).fetchall()
    more = len(rows) > lim
    rows = rows[:lim]
    tags = tags_for(conn, [r["n"] for r in rows if r["deleted_at"] is None])   # one query per page
    return {"files": [file_out(r, tags.get(r["n"], [])) for r in rows],
            "next_before": rows[-1]["n"] if more else None}


@router.get("/api/files/{ref}")
def get_file(ref: str, request: Request, _: Principal = Depends(require_any),
             conn: sqlite3.Connection = Depends(get_db)):
    return file_out_one(conn, _load(request, conn, ref))


@router.get("/api/files/{ref}/blob")
def get_blob(ref: str, request: Request, _: Principal = Depends(require_any),
             conn: sqlite3.Connection = Depends(get_db)):
    row = _load(request, conn, ref)
    path = request.app.state.blobs.path_for(row["uuid"])
    if not path.is_file():
        raise api_error(404, "not_found", f"blob for {format_id(row['n'])} is missing")
    return FileResponse(path, media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store"})


class _DuplicateUuid(Exception):
    pass


_UUID_UNIQUE = "UNIQUE constraint failed: files.uuid"


async def receive_sealed_blob(request: Request, parse_meta: Callable[[object], dict]) -> tuple[dict, Path, int]:
    """Receive one sealed blob over multipart/form-data (a `meta` and a `blob` part): the
    Content-Length pre-check, the multipart and meta parsing (via `parse_meta`, so a caller can
    plug in its own meta shape and any extra checks), streaming into the blob store, and header
    validation. Returns (meta, tmp_path, size); the same api_errors as always (`bad_request`,
    `too_large`, whatever `parse_meta` raises, `bad_blob`). The caller owns what happens next: a
    tmp file it must either commit (renaming into the store) or discard, inside its own
    transaction (upload_file's and uploadlinks.public_upload's differ)."""
    settings = request.app.state.settings
    store = request.app.state.blobs
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > settings.max_upload + MULTIPART_SLACK:
        raise api_error(413, "too_large", f"limit is {settings.max_upload} bytes")
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise api_error(400, "bad_request", "expected multipart/form-data with meta and blob parts")
    form = await request.form()
    try:
        blob = form.get("blob")
        if not isinstance(blob, UploadFile):
            raise api_error(400, "bad_request", "blob part missing")
        meta = parse_meta(form.get("meta"))

        async def chunks():
            while True:
                data = await blob.read(READ_SIZE)
                if not data:
                    return
                yield data

        try:
            tmp, size = await store.receive(chunks(), settings.max_upload)
        except TooLarge:
            raise api_error(413, "too_large", f"limit is {settings.max_upload} bytes")
    finally:
        await form.close()

    try:
        key_version = store.validate_header(tmp, meta["uuid"])
    except BlobError as e:
        store.discard(tmp)
        raise api_error(400, "bad_blob", str(e) or "invalid blob")
    if key_version != meta["key_version"]:
        store.discard(tmp)
        raise api_error(400, "bad_blob", "blob key_version does not match meta")
    return meta, tmp, size


@router.post("/api/files", status_code=201)
async def upload_file(request: Request, principal: Principal = Depends(require_any),
                      conn: sqlite3.Connection = Depends(get_db)):
    store = request.app.state.blobs
    meta, tmp, size = await receive_sealed_blob(request, _parse_meta)

    if principal.kind == "device":
        device_id, project, sess_name = principal.device["id"], principal.device["project"], None
    else:
        device_id, project = None, "web"
        sess_name = session_name(conn, principal.session_hash) or None
    now = clock.now()
    # One transaction: the file row, its tags and the blob's rename into place. The blob moves only
    # after the INSERTs succeeded and before COMMIT, so a failure anywhere (SQLITE_BUSY on BEGIN or
    # COMMIT, a duplicate uuid, a failed rename) leaves neither an orphan row nor an orphan blob.
    moved = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        # The only 409: this uuid is already taken (a client retrying a request the server already
        # committed relies on it, spec §16). Checked inside the write transaction, so it can't race.
        if conn.execute("SELECT 1 FROM files WHERE uuid = ?", (meta["uuid"],)).fetchone():
            raise _DuplicateUuid()
        cur = conn.execute(
            "INSERT INTO files (uuid, size, key_version, wrapped_dek, enc_meta, device_id, project,"
            " created_at, session_name, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (meta["uuid"], size, meta["key_version"], meta["wrapped_dek"], meta["enc_meta"],
             device_id, project, clock.now_iso(now), sess_name, expires_at(meta["ttl"], now)))
        n = cur.lastrowid
        set_tags(conn, n, meta["tags"])
        store.commit(tmp, meta["uuid"])
        moved = True
        conn.execute("COMMIT")
    except BaseException as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if moved:
            store.delete(meta["uuid"])
        store.discard(tmp)
        if isinstance(exc, _DuplicateUuid) or (
                isinstance(exc, sqlite3.IntegrityError) and _UUID_UNIQUE in str(exc)):
            raise api_error(409, "duplicate_uuid", "a file with this uuid already exists") from None
        if isinstance(exc, sqlite3.IntegrityError):
            # Any other constraint (a tag row, a foreign key) is a server bug, not a duplicate:
            # answering 409 here would make an outbox drop an upload that never happened.
            raise api_error(500, "integrity_error", "the upload could not be stored") from None
        raise
    return file_out(conn.execute(SELECT_FILE + " WHERE f.n = ?", (n,)).fetchone(), meta["tags"])


@router.delete("/api/files/{ref}", status_code=204)
def delete_file(ref: str, request: Request, principal: Principal = Depends(require_any),
                conn: sqlite3.Connection = Depends(get_db)):
    row = _load(request, conn, ref)
    if principal.kind == "device" and row["device_id"] != principal.device["id"]:
        raise api_error(403, "forbidden", "devices may only delete files they uploaded")
    tombstone(conn, request.app.state.blobs, row["n"], row["uuid"], clock.now_iso())
    return Response(status_code=204)


@router.patch("/api/files/{ref}", status_code=204)
async def change_expiry(ref: str, request: Request, principal: Principal = Depends(require_any),
                        conn: sqlite3.Connection = Depends(get_db)):
    row = _load(request, conn, ref)
    if principal.kind == "device" and row["device_id"] != principal.device["id"]:
        raise api_error(403, "forbidden", "devices may only change the expiry of files they uploaded")
    ttl = (await read_json(request)).get("ttl")
    if not isinstance(ttl, str) or ttl not in TTLS:
        raise api_error(400, "bad_request", "ttl must be one of " + ", ".join(TTLS))
    # recomputed from now, not from created_at: "keep it 7 more days"
    conn.execute("UPDATE files SET expires_at = ? WHERE n = ? AND deleted_at IS NULL",
                 (expires_at(ttl, clock.now()), row["n"]))
    return Response(status_code=204)


def _acker(principal: Principal, conn: sqlite3.Connection) -> str:
    if principal.kind == "device":
        return principal.device["name"]
    return session_name(conn, principal.session_hash) or "you"


@router.post("/api/files/{ref}/ack", status_code=204)
def ack_file(ref: str, request: Request, principal: Principal = Depends(require_any),
             conn: sqlite3.Connection = Depends(get_db)):
    row = _load(request, conn, ref)
    # idempotent: the first acknowledgement (time and who) is kept
    conn.execute("UPDATE files SET acked_at = ?, acked_by = ? WHERE n = ? AND acked_at IS NULL",
                 (clock.now_iso(), _acker(principal, conn), row["n"]))
    return Response(status_code=204)


@router.delete("/api/files/{ref}/ack", status_code=204)
def unack_file(ref: str, request: Request, _: Principal = Depends(require_any),
               conn: sqlite3.Connection = Depends(get_db)):
    row = _load(request, conn, ref)
    conn.execute("UPDATE files SET acked_at = NULL, acked_by = NULL WHERE n = ?", (row["n"],))
    return Response(status_code=204)


@router.patch("/api/files/{ref}/meta", status_code=204)
async def replace_meta(ref: str, request: Request, _: Principal = Depends(require_any),
                       conn: sqlite3.Connection = Depends(get_db)):
    """Replace enc_meta (e.g. to add a transcript, §14 G). wrapped_dek and the blob never change."""
    row = _load(request, conn, ref)
    enc_meta = (await read_bounded_json(request, MAX_ENC_META_B64 + META_BODY_SLACK)).get("enc_meta")
    _check_enc_meta(enc_meta)
    conn.execute("UPDATE files SET enc_meta = ? WHERE n = ? AND deleted_at IS NULL", (enc_meta, row["n"]))
    return Response(status_code=204)


def _load_live(request: Request, conn, ref: str):
    """The file, or 404 for an unknown, deleted or expired one: tags need a live file."""
    try:
        return _load(request, conn, ref)
    except HTTPException as e:
        if e.status_code == 410:
            raise api_error(404, "not_found", f"{ref} was deleted or has expired") from None
        raise


@router.put("/api/files/{ref}/tags")
async def replace_tags(ref: str, request: Request, _: Principal = Depends(require_any),
                       conn: sqlite3.Connection = Depends(get_db)):
    """Replace a file's tags (§19). Any session or active device, on any file, like ack."""
    row = _load_live(request, conn, ref)
    body = await read_bounded_json(request, MAX_TAGS_BODY)
    try:
        tags = normalize_tags(body.get("tags"))
    except BadTag as e:
        raise _bad_tag(e)
    try:
        conn.execute("BEGIN IMMEDIATE")
        live = conn.execute("SELECT 1 FROM files WHERE n = ? AND deleted_at IS NULL", (row["n"],)).fetchone()
        if live is None:   # a delete or the sweep won the race since _load
            raise api_error(404, "not_found", f"{ref} was deleted or has expired")
        set_tags(conn, row["n"], tags)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"tags": tags}


@router.get("/api/tags")
def list_tags(request: Request, _: Principal = Depends(require_any),
              conn: sqlite3.Connection = Depends(get_db)):
    """The tags in use on live, unexpired files: for suggestions and discovery (§19)."""
    expire_files(conn, request.app.state.blobs)
    rows = conn.execute(
        "SELECT ft.tag AS tag, COUNT(*) AS count, MAX(f.created_at) AS last_used"
        " FROM file_tags ft JOIN files f ON f.n = ft.file_n"
        " WHERE f.deleted_at IS NULL AND (f.expires_at IS NULL OR f.expires_at > ?)"
        " GROUP BY ft.tag ORDER BY count DESC, ft.tag",
        (clock.now_iso(),)).fetchall()
    return {"tags": [{"tag": r["tag"], "count": r["count"], "last_used": r["last_used"]} for r in rows]}
