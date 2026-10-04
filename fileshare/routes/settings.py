"""Account-wide encrypted settings (spec §15). The server stores one opaque envelope and a rev."""
import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from fileshare import clock
from fileshare.deps import Principal, api_error, get_db, require_any, require_session
from fileshare.routes.files import check_envelope, read_bounded_json

router = APIRouter()

MAX_ENC_SETTINGS_B64 = 65536
MIN_ENC_SETTINGS_LEN = 1 + 12 + 16          # version byte, nonce, tag
BODY_SLACK = 4096                           # JSON framing around enc_settings and rev
_NO_STORE = {"Cache-Control": "no-store"}


def store(conn: sqlite3.Connection, enc_settings: str, rev: int) -> int | None:
    """Write enc_settings only if the stored rev is still `rev`; return the new rev, or None on conflict.

    Each branch is a single statement, so SQLite makes the check-and-write atomic: of two writers
    holding the same rev, exactly one changes a row."""
    now = clock.now_iso()
    if rev == 0:
        cur = conn.execute("INSERT INTO settings (id, enc_settings, rev, updated_at) VALUES (1, ?, 1, ?)"
                           " ON CONFLICT(id) DO NOTHING", (enc_settings, now))
    else:
        cur = conn.execute("UPDATE settings SET enc_settings = ?, rev = rev + 1, updated_at = ?"
                           " WHERE id = 1 AND rev = ?", (enc_settings, now, rev))
    return rev + 1 if cur.rowcount == 1 else None


@router.get("/api/settings")
def get_settings(_: Principal = Depends(require_any), conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT enc_settings, rev, updated_at FROM settings WHERE id = 1").fetchone()
    body = ({"enc_settings": None, "rev": 0, "updated_at": None} if row is None else
            {"enc_settings": row["enc_settings"], "rev": row["rev"], "updated_at": row["updated_at"]})
    return JSONResponse(body, headers=_NO_STORE)


def _browser_only(request: Request) -> None:
    # Devices read settings but never write them (§15); refuse before the session check,
    # so a bearer token gets 403 rather than a misleading 401.
    if request.headers.get("Authorization"):
        raise api_error(403, "forbidden", "devices can't change settings; use the Settings page")


@router.put("/api/settings")
async def put_settings(request: Request, _b: None = Depends(_browser_only),
                       _: Principal = Depends(require_session), conn: sqlite3.Connection = Depends(get_db)):
    body = await read_bounded_json(request, MAX_ENC_SETTINGS_B64 + BODY_SLACK)
    enc, rev = body.get("enc_settings"), body.get("rev")
    check_envelope(enc, "enc_settings", MAX_ENC_SETTINGS_B64, lambda b: len(b) >= MIN_ENC_SETTINGS_LEN,
                   code="bad_request")
    if type(rev) is not int or rev < 0:
        raise api_error(400, "bad_request", "rev must be a non-negative integer")
    new_rev = store(conn, enc, rev)
    if new_rev is None:
        raise api_error(409, "conflict", "settings changed elsewhere; reload and retry")
    return JSONResponse({"rev": new_rev}, headers=_NO_STORE)
