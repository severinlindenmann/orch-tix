import functools
import hashlib
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import NoReturn

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from fileshare import clock
from fileshare.deps import Principal, api_error, attempt, get_db, read_json, refuse, require_session
from fileshare.routes.devices import device_out
from fileshare.security import (b64u_decode, b64u_encode, fingerprint, new_device_token,
                                new_id, sha256_hex, valid_p256_point)

router = APIRouter()

TOKEN_TTL = timedelta(minutes=15)
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$", re.ASCII)
PLATFORMS = {"darwin", "linux", "windows"}
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
SKILL_FILES = {
    "SKILL.md": "text/markdown; charset=utf-8",
    "tickets-SKILL.md": "text/markdown; charset=utf-8",
    "sharing.py": "text/x-python; charset=utf-8",
    "sharing": "text/x-shellscript; charset=utf-8",
    "sharing.cmd": "text/plain; charset=utf-8",
}
VERSION_RE = re.compile(r'^VERSION\s*=\s*"(\d+\.\d+\.\d+)"', re.M)
NO_STORE = {"Cache-Control": "no-store"}


def _refuse_code() -> NoReturn:
    refuse(410, "refused", "onboarding code refused")


def _token_row(conn, token_id: str):
    row = conn.execute("SELECT * FROM onboarding_tokens WHERE id = ?", (token_id,)).fetchone()
    if row is None:
        raise api_error(404, "not_found", "no such onboarding token")
    return row


def _token_out(conn, row) -> dict:
    device = None
    if row["device_id"]:
        d = conn.execute("SELECT * FROM devices WHERE id = ?", (row["device_id"],)).fetchone()
        if d is not None:
            device = device_out(d)
    return {"id": row["id"], "expires_at": row["expires_at"],
            "used_at": row["used_at"], "device": device}


@router.post("/api/onboarding-tokens", status_code=201)
async def create_token(request: Request, _: Principal = Depends(require_session),
                       conn: sqlite3.Connection = Depends(get_db)):
    body = await read_json(request)
    try:
        lookup = b64u_decode(body["lookup"])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise api_error(400, "bad_request", "lookup must be base64url")
    if len(lookup) != 16:
        raise api_error(400, "bad_request", "lookup must be 16 bytes")
    now = clock.now()
    token_id = new_id("onb")
    expires_at = clock.now_iso(now + TOKEN_TTL)
    try:
        conn.execute(
            "INSERT INTO onboarding_tokens (id, lookup_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token_id, sha256_hex(lookup), clock.now_iso(now), expires_at))
    except sqlite3.IntegrityError:
        raise api_error(409, "duplicate", "lookup already registered")
    return {"id": token_id, "expires_at": expires_at}


@router.get("/api/onboarding-tokens/{token_id}")
def get_token(token_id: str, _: Principal = Depends(require_session),
              conn: sqlite3.Connection = Depends(get_db)):
    return _token_out(conn, _token_row(conn, token_id))


@router.delete("/api/onboarding-tokens/{token_id}", status_code=204)
def delete_token(token_id: str, _: Principal = Depends(require_session),
                 conn: sqlite3.Connection = Depends(get_db)):
    _token_row(conn, token_id)
    conn.execute("DELETE FROM onboarding_tokens WHERE id = ?", (token_id,))
    return Response(status_code=204)


def _handshake_fields(body):
    """Everything except the public key; None means a uniform 410."""
    if not isinstance(body, dict):
        return None
    try:
        lookup = b64u_decode(body["lookup"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    name, project = body.get("device_name"), body.get("project")
    hostname, platform = body.get("hostname", ""), body.get("platform")
    ok = (len(lookup) == 16
          and isinstance(name, str) and NAME_RE.fullmatch(name) is not None
          and isinstance(project, str) and NAME_RE.fullmatch(project) is not None
          and isinstance(hostname, str) and len(hostname) <= 255
          and isinstance(platform, str) and platform in PLATFORMS)
    return (lookup, name, project, hostname, platform) if ok else None


def _device_pub(body) -> bytes | None:
    raw = body.get("device_pub")
    if not isinstance(raw, str):
        return None
    try:
        pub = b64u_decode(raw)
    except ValueError:
        return None
    return pub if valid_p256_point(pub) else None


@router.post("/api/handshake", status_code=201)
async def handshake(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    # §6: rate-limited like login. The slot is reserved before reading the body, and
    # every refusal below (410 or the 400 for a bad key) is recorded as a failure.
    with attempt(request):
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            body = None
        # All validation runs before the UPDATE, so malformed input never spends a code.
        fields = _handshake_fields(body)
        if fields is None:
            _refuse_code()
        pub = _device_pub(body)
        if pub is None:
            refuse(400, "bad_request",
                   "device_pub must be an uncompressed P-256 point (65 bytes, base64url)")
        lookup, name, project, hostname, platform = fields

        now = clock.now_iso()
        lookup_hash = sha256_hex(lookup)
        device_id, token = new_id("dev"), new_device_token()
        fp = fingerprint(pub)
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Single use: this conditional UPDATE plus the rowcount check, under BEGIN IMMEDIATE.
            # ISO timestamps from clock.now_iso() sort as strings, so `expires_at > ?` is correct.
            cur = conn.execute(
                "UPDATE onboarding_tokens SET used_at = ?"
                " WHERE lookup_hash = ? AND used_at IS NULL AND expires_at > ?",
                (now, lookup_hash, now))
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                _refuse_code()
            conn.execute(
                "INSERT INTO devices (id, name, project, hostname, token_hash, pubkey, fingerprint,"
                " platform, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (device_id, name, project, hostname, sha256_hex(token), b64u_encode(pub), fp,
                 platform, now))
            conn.execute("UPDATE onboarding_tokens SET device_id = ? WHERE lookup_hash = ?",
                         (device_id, lookup_hash))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return {
        "device_id": device_id,
        "device_token": token,
        "fingerprint": fp,
        "server_url": request.app.state.settings.public_url,
    }


def _installer(request: Request, template: str) -> PlainTextResponse:
    text = (TEMPLATES / template).read_text(encoding="utf-8")
    return PlainTextResponse(text.replace("{{PUBLIC_URL}}", request.app.state.settings.public_url),
                             headers=NO_STORE)


@router.get("/onboarding.txt")
def onboarding_txt(request: Request):
    return _installer(request, "onboarding.sh")


@router.get("/onboarding.ps1")
def onboarding_ps1(request: Request):
    return _installer(request, "onboarding.ps1")


@functools.lru_cache(maxsize=8)
def skill_manifest(skill_dir: Path) -> dict:
    """Computed once per skill directory ("at startup", §6); a deploy restarts the service."""
    src = skill_dir / "sharing.py"
    m = VERSION_RE.search(src.read_text(encoding="utf-8")) if src.is_file() else None
    if m is None:
        raise LookupError(f"no VERSION in {src}")
    files = {name: hashlib.sha256((skill_dir / name).read_bytes()).hexdigest()
             for name in sorted(SKILL_FILES) if (skill_dir / name).is_file()}
    return {"version": m.group(1), "files": files}


# Declared before /skill/{name}: "manifest.json" is not a skill file.
@router.get("/skill/manifest.json")
def manifest(request: Request):
    try:
        body = skill_manifest(request.app.state.settings.skill_dir)
    except LookupError:
        raise api_error(404, "not_found", "skill not available on this server")
    return JSONResponse({"version": body["version"], "files": dict(body["files"])}, headers=NO_STORE)


@router.get("/skill/{name}")
def skill_file(name: str, request: Request):
    media = SKILL_FILES.get(name)
    path = request.app.state.settings.skill_dir / name
    if media is None or not path.is_file():
        raise api_error(404, "not_found", "no such skill file")
    return FileResponse(path, media_type=media, headers=NO_STORE)
