"""Inbound one-time upload links (upload-links spec): owner create/list/revoke, public constraints,
and the /u/ page. Task 3 extends this with the anonymous upload + adoption routes."""
import hashlib
import os
import re
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fileshare.db import connect
from fileshare.routes import links as links_routes
from fileshare.routes import uploadlinks
from fileshare.security import b64u_encode
from tests.helpers.blobs import make_fake_blob
from tests.helpers.files import drop, fake_drop_meta, fake_meta, upload
from tests.helpers.onboard import onboard_device

T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
CREATE_KEYS = {"id", "token", "expires_at"}
UPLOAD_LINK_OUT_KEYS = {"id", "uuid", "key_version", "wrapped_lpriv", "enc_label", "created_at",
                        "created_by", "expires_at", "revoked_at", "used_at", "state", "pending", "file"}
PUBLIC_KEYS = {"uuid", "key_version", "max_bytes", "expires_at"}
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
NOT_FOUND = {"error": "not_found", "detail": "this link has expired or was revoked"}


def _wlp() -> str:
    return b64u_encode(b"\x01" + os.urandom(125))     # 126 bytes: a sealed link private key


def _wlabel() -> str:
    return b64u_encode(b"\x01" + os.urandom(30))       # a plausible sealed label


def _db(settings):
    return connect(settings.db_path)


def _uplink(client, headers=None, **body) -> dict:
    body = {"uuid": os.urandom(16).hex(), "key_version": 1, "wrapped_lpriv": _wlp(), "ttl": "1d"} | body
    return client.post("/api/upload-links", json=body, headers=headers or {})


def _mk(client, headers=None, **body) -> dict:
    r = _uplink(client, headers, **body)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def bare(app):
    with TestClient(app, base_url="http://testserver") as c:
        yield c


# --- create ------------------------------------------------------------------------------

def test_create_as_session(frozen_clock, owner, settings):
    frozen_clock(T0)
    r = _uplink(owner.client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == CREATE_KEYS
    assert TOKEN_RE.fullmatch(body["token"])
    assert body["id"].startswith("upl_")
    assert body["expires_at"] == "2026-09-25T12:00:00Z"
    row = _db(settings).execute("SELECT * FROM upload_links").fetchone()
    assert row["token_hash"] == hashlib.sha256(body["token"].encode()).hexdigest()
    assert (row["created_by_device"], row["created_by_session"]) == (None, "browser")
    # the raw token is nowhere in the database file or its WAL
    conn = _db(settings)
    conn.execute("PRAGMA wal_checkpoint(FULL)")
    for p in settings.data_dir.rglob("fileshare.db*"):
        assert body["token"].encode() not in p.read_bytes(), p


def test_create_as_device_records_the_device(owner, sharing, device):
    made = _mk(owner.client, headers=device.headers)
    listed = owner.client.get("/api/upload-links").json()["links"]
    by_id = {x["id"]: x for x in listed}
    assert by_id[made["id"]]["created_by"] == {"id": device.id, "name": "laptop"}


def test_tokens_are_unique_per_link(owner):
    tokens = {_mk(owner.client)["token"] for _ in range(5)}
    assert len(tokens) == 5


def test_unauthenticated_create_is_401(bare):
    r = bare.post("/api/upload-links", json={"uuid": os.urandom(16).hex(), "key_version": 1,
                                              "wrapped_lpriv": _wlp(), "ttl": "1d"},
                  headers={"Origin": "http://testserver"})
    assert r.status_code == 401 and r.json()["error"] == "unauthenticated"


@pytest.mark.parametrize("ttl", ["30d", "2d", "", None, 7, "1H", " 1d", ["1d"]])
def test_bad_ttl_is_400(owner, ttl):
    r = _uplink(owner.client, ttl=ttl)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


@pytest.mark.parametrize("uuid", [None, "", "x" * 32, "A" * 32, "0" * 31, "0" * 33, 12, ["0" * 32]])
def test_bad_uuid_is_400(owner, uuid):
    r = _uplink(owner.client, uuid=uuid)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


@pytest.mark.parametrize("w", [
    None, 5, "", "!!!", "A" * 5000,
    b64u_encode(b"\x01" + os.urandom(124)),         # 125 bytes: too short
    b64u_encode(b"\x01" + os.urandom(126)),         # 127 bytes: too long
    b64u_encode(b"\x02" + os.urandom(125)),         # wrong version byte
    b64u_encode(b"\x01" + os.urandom(125)) + "=",   # padded
])
def test_bad_wrapped_lpriv_is_400(owner, w):
    r = _uplink(owner.client, wrapped_lpriv=w)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert set(r.json()) == {"error", "detail"}


@pytest.mark.parametrize("label", [5, "", "!!!", b64u_encode(b"\x02" + os.urandom(30))])
def test_bad_enc_label_is_400(owner, label):
    r = _uplink(owner.client, enc_label=label)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_enc_label_is_optional_and_may_be_null(owner):
    made = _mk(owner.client, enc_label=None)
    listed = owner.client.get("/api/upload-links").json()["links"]
    assert listed[0]["enc_label"] is None
    made2 = _mk(owner.client, enc_label=_wlabel())
    listed = owner.client.get("/api/upload-links").json()["links"]
    by_id = {x["id"]: x for x in listed}
    assert by_id[made2["id"]]["enc_label"] is not None


def test_duplicate_uuid_is_409(owner):
    u = os.urandom(16).hex()
    _mk(owner.client, uuid=u)
    r = _uplink(owner.client, uuid=u)
    assert r.status_code == 409 and r.json()["error"] == "duplicate_uuid"


@pytest.mark.parametrize("body", [b"not json", b"[]", b'"x"', b"\xff\xfe", b""])
def test_non_object_body_is_400(owner, body):
    r = owner.client.post("/api/upload-links", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_session_write_checks_origin(owner, app):
    c = TestClient(app, base_url="http://testserver")
    c.cookies = owner.client.cookies
    r = c.post("/api/upload-links", json={"uuid": os.urandom(16).hex(), "key_version": 1,
                                          "wrapped_lpriv": _wlp(), "ttl": "1d"})
    assert r.status_code == 403 and r.json()["error"] == "bad_origin"


# --- list ---------------------------------------------------------------------------------

def test_list_shows_waiting_state_without_tokens(frozen_clock, owner):
    frozen_clock(T0)
    made = _mk(owner.client)
    listed = owner.client.get("/api/upload-links")
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    links = listed.json()["links"]
    assert len(links) == 1
    x = links[0]
    assert set(x) == UPLOAD_LINK_OUT_KEYS
    assert x["state"] == "waiting"
    assert x["pending"] is None
    assert x["file"] is None
    assert x["id"] == made["id"]
    assert made["token"] not in str(links)
    assert hashlib.sha256(made["token"].encode()).hexdigest() not in str(links)


def test_list_is_newest_first(owner):
    a = _mk(owner.client)
    b = _mk(owner.client)
    c = _mk(owner.client)
    assert [x["id"] for x in owner.client.get("/api/upload-links").json()["links"]] == [
        c["id"], b["id"], a["id"]]


def test_list_needs_auth(bare):
    r = bare.get("/api/upload-links", headers={"Origin": "http://testserver"})
    assert r.status_code == 401 and r.json()["error"] == "unauthenticated"


def test_any_active_device_can_list_and_revoke(owner, sharing, device):
    other = onboard_device(owner, sharing, "desk", "p2")
    made = _mk(owner.client, headers=other.headers)
    assert [x["id"] for x in owner.client.get("/api/upload-links", headers=device.headers).json()["links"]] == [
        made["id"]]
    assert owner.client.delete(f"/api/upload-links/{made['id']}", headers=device.headers).status_code == 204


# --- public GET -----------------------------------------------------------------------------

def test_public_get_on_a_live_token(frozen_clock, owner, bare, settings):
    frozen_clock(T0)
    made = _mk(owner.client, ttl="1h")
    r = bare.get(f"/api/public/u/{made['token']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == PUBLIC_KEYS
    row = _db(settings).execute("SELECT uuid, key_version FROM upload_links").fetchone()
    assert (body["uuid"], body["key_version"]) == (row["uuid"], row["key_version"])
    assert body["max_bytes"] == 209_715_200
    assert body["expires_at"] == "2026-09-24T13:00:00Z"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in r.headers


def test_public_get_unknown_token(bare):
    r = bare.get(f"/api/public/u/{b64u_encode(os.urandom(32))}")
    assert r.status_code == 404 and r.json() == NOT_FOUND


@pytest.mark.parametrize("tok", ["short", "A" * 44, "A" * 42 + "!", "%2e%2e"])
def test_public_get_malformed_token(bare, tok):
    r = bare.get(f"/api/public/u/{tok}")
    assert r.status_code == 404 and r.json() == NOT_FOUND


def test_public_get_revoked_token(owner, bare):
    made = _mk(owner.client)
    assert owner.client.delete(f"/api/upload-links/{made['id']}").status_code == 204
    r = bare.get(f"/api/public/u/{made['token']}")
    assert r.status_code == 404 and r.json() == NOT_FOUND


def test_public_get_expired_token(frozen_clock, owner, bare):
    frozen_clock(T0)
    made = _mk(owner.client, ttl="1h")
    frozen_clock(T0 + timedelta(hours=1))
    r = bare.get(f"/api/public/u/{made['token']}")
    assert r.status_code == 404 and r.json() == NOT_FOUND


def test_outbound_public_link_still_works_and_upload_link_is_reached(owner, bare):
    """Router order: uploadlinks.router must be included before links.router, or
    /api/public/u/{token} would be swallowed by links' catch-all."""
    from tests.helpers.blobs import make_fake_blob
    from tests.helpers.files import fake_meta, upload
    u = os.urandom(16).hex()
    r = upload(owner.client, None, u, meta=fake_meta(u), blob=make_fake_blob(u))
    assert r.status_code == 201, r.text
    out_r = owner.client.post("/api/files/FILE1/links", json={
        "wrapped_dek_link": b64u_encode(b"\x01" + os.urandom(60)), "ttl": "7d"})
    assert out_r.status_code == 201, out_r.text
    assert bare.get(f"/api/public/{out_r.json()['token']}").status_code == 200
    made = _mk(owner.client)
    assert bare.get(f"/api/public/u/{made['token']}").status_code == 200


# --- delete / revoke ------------------------------------------------------------------------

def test_delete_unused_revokes_it(frozen_clock, owner, bare, settings):
    frozen_clock(T0)
    made = _mk(owner.client)
    assert owner.client.delete(f"/api/upload-links/{made['id']}").status_code == 204
    listed = owner.client.get("/api/upload-links").json()["links"]
    assert listed[0]["state"] == "revoked"
    assert bare.get(f"/api/public/u/{made['token']}").status_code == 404


def test_delete_unknown_id_is_404(owner):
    r = owner.client.delete("/api/upload-links/upl_000000000000")
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_delete_bad_id_is_404(owner):
    r = owner.client.delete("/api/upload-links/bogus")
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_delete_is_idempotent(owner, settings):
    made = _mk(owner.client)
    assert owner.client.delete(f"/api/upload-links/{made['id']}").status_code == 204
    assert owner.client.delete(f"/api/upload-links/{made['id']}").status_code == 204


def test_delete_pending_discards_the_blob_and_clears_pending_columns(owner, app, settings):
    """The DELETE "pending" branch, exercised by inserting a pending row directly (Task 3 supplies
    the real upload route and its own end-to-end test of this path)."""
    made = _mk(owner.client)
    file_uuid = os.urandom(16).hex()
    blob_path = app.state.blobs.path_for(file_uuid)
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(b"pretend-ciphertext")
    conn = _db(settings)
    conn.execute(
        "UPDATE upload_links SET used_at = ?, file_uuid = ?, file_size = ?, sealed_dek = ?, enc_meta = ?"
        " WHERE id = ?",
        (uploadlinks.clock.now_iso(), file_uuid, 19, _wlp(), _wlabel(), made["id"]))
    conn.close()
    row = _db(settings).execute("SELECT * FROM upload_links WHERE id = ?", (made["id"],)).fetchone()
    assert uploadlinks._state(row, uploadlinks.clock.now_iso()) == "pending"
    assert owner.client.delete(f"/api/upload-links/{made['id']}").status_code == 204
    assert not blob_path.is_file()
    row = _db(settings).execute("SELECT * FROM upload_links WHERE id = ?", (made["id"],)).fetchone()
    assert row["file_uuid"] is None and row["sealed_dek"] is None and row["enc_meta"] is None
    assert uploadlinks._state(row, uploadlinks.clock.now_iso()) == "revoked"


def test_delete_received_is_a_noop(owner, settings):
    from tests.helpers.blobs import make_fake_blob
    from tests.helpers.files import fake_meta, upload
    u = os.urandom(16).hex()
    r = upload(owner.client, None, u, meta=fake_meta(u), blob=make_fake_blob(u))
    assert r.status_code == 201, r.text
    made = _mk(owner.client)
    conn = _db(settings)
    conn.execute("UPDATE upload_links SET file_n = 1 WHERE id = ?", (made["id"],))
    conn.close()
    assert owner.client.delete(f"/api/upload-links/{made['id']}").status_code == 204
    row = _db(settings).execute("SELECT * FROM upload_links WHERE id = ?", (made["id"],)).fetchone()
    assert row["file_n"] == 1 and row["revoked_at"] is None


def test_revoking_the_creating_device_revokes_its_waiting_upload_links(owner, sharing):
    laptop = onboard_device(owner, sharing, "laptop", "proj")
    desk = onboard_device(owner, sharing, "desk", "proj")
    by_laptop = _mk(owner.client, headers=laptop.headers)
    by_desk = _mk(owner.client, headers=desk.headers)
    by_browser = _mk(owner.client)
    assert owner.client.delete(f"/api/devices/{laptop.id}").status_code == 204
    listed = {x["id"]: x["state"] for x in owner.client.get("/api/upload-links").json()["links"]}
    assert listed[by_laptop["id"]] == "revoked"
    assert listed[by_desk["id"]] == "waiting"
    assert listed[by_browser["id"]] == "waiting"


# --- the /u/ page ----------------------------------------------------------------------------

def test_drop_page_valid_shape_token(bare):
    r = bare.get(f"/u/{b64u_encode(os.urandom(32))}", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "/static/js/drop.js?v=" in r.text
    assert "{{" not in r.text
    assert r.headers["cache-control"] == "no-store"


def test_drop_page_short_token_is_404(bare):
    good = bare.get(f"/u/{b64u_encode(os.urandom(32))}")
    r = bare.get("/u/short", follow_redirects=False)
    assert r.status_code == 404
    assert r.text == good.text


# --- log redaction ----------------------------------------------------------------------------

def _access_record(path: str):
    import logging
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                             ("127.0.0.1:5000", "GET", path, "1.1", 200), None)


@pytest.mark.parametrize("path,want", [
    ("/u/abcDEF_-123", "/u/<redacted>"),
    ("/u/abc?x=1", "/u/<redacted>"),
    ("/api/public/u/abc", "/api/public/<redacted>"),
])
def test_access_log_filter_redacts_upload_link_paths(path, want):
    rec = _access_record(path)
    assert links_routes.RedactTokens().filter(rec) is True
    assert rec.args[2] == want
    assert "abc" not in rec.getMessage()


# --- public upload (drop) -------------------------------------------------------------------

def _tmp_files(settings):
    return list((settings.data_dir / "tmp").iterdir())


def _link_state(settings, link_id: str) -> str:
    row = _db(settings).execute("SELECT * FROM upload_links WHERE id = ?", (link_id,)).fetchone()
    return uploadlinks._state(row, uploadlinks.clock.now_iso())


def _wdek() -> str:
    return b64u_encode(b"\x01" + os.urandom(60))     # 61 bytes: a sealed 32-byte DEK


def test_drop_to_waiting_link_succeeds(owner, bare, settings, app):
    made = _mk(owner.client)
    u = os.urandom(16).hex()
    blob = make_fake_blob(u, body_len=123)
    r = drop(bare, made["token"], u, blob)
    assert r.status_code == 201, r.text
    assert r.json() == {"ok": True, "size": len(blob)}
    assert set(r.json()) == {"ok", "size"}
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    listed = owner.client.get("/api/upload-links").json()["links"][0]
    assert listed["state"] == "pending"
    assert listed["pending"] == {"file_uuid": u, "size": len(blob), "sealed_dek": listed["pending"]["sealed_dek"],
                                 "enc_meta": listed["pending"]["enc_meta"]}
    assert app.state.blobs.path_for(u).is_file()
    # a second upload to the same (now used) token is dead
    r2 = drop(bare, made["token"])
    assert r2.status_code == 404 and r2.json() == NOT_FOUND
    assert bare.get(f"/api/public/u/{made['token']}").status_code == 404


def test_bad_blob_leaves_link_waiting_with_no_orphan(owner, bare, settings):
    made = _mk(owner.client)
    u = os.urandom(16).hex()
    bad_blob = b"NOTV" + os.urandom(60)
    r = drop(bare, made["token"], u, bad_blob)
    assert r.status_code == 400 and r.json()["error"] == "bad_blob"
    assert _link_state(settings, made["id"]) == "waiting"
    assert not settings.data_dir.joinpath("blobs", u[0:2], u[2:4], f"{u}.shr").exists()
    assert _tmp_files(settings) == []


def test_blob_key_version_mismatch_leaves_link_waiting_with_no_orphan(owner, bare, settings):
    made = _mk(owner.client)
    u = os.urandom(16).hex()
    blob = make_fake_blob(u, key_version=2)
    r = drop(bare, made["token"], u, blob)
    assert r.status_code == 400 and r.json()["error"] == "bad_blob"
    assert _link_state(settings, made["id"]) == "waiting"
    assert _tmp_files(settings) == []


def test_oversized_drop_leaves_link_waiting_with_no_orphan(owner, bare, settings, app):
    made = _mk(owner.client)
    object.__setattr__(app.state.settings, "max_upload", 10)   # Settings is a frozen dataclass
    u = os.urandom(16).hex()
    r = drop(bare, made["token"], u, make_fake_blob(u, body_len=1000))
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    assert _link_state(settings, made["id"]) == "waiting"
    assert not settings.data_dir.joinpath("blobs", u[0:2], u[2:4], f"{u}.shr").exists()
    assert _tmp_files(settings) == []


def test_drop_meta_key_version_must_match_the_link(owner, bare, settings):
    made = _mk(owner.client, key_version=2)
    u = os.urandom(16).hex()
    r = drop(bare, made["token"], u, meta=fake_drop_meta(u, key_version=1))
    assert r.status_code == 400 and r.json()["error"] == "bad_meta"
    assert _link_state(settings, made["id"]) == "waiting"
    assert _tmp_files(settings) == []


def test_drop_to_revoked_link_is_404(owner, bare):
    made = _mk(owner.client)
    assert owner.client.delete(f"/api/upload-links/{made['id']}").status_code == 204
    r = drop(bare, made["token"])
    assert r.status_code == 404 and r.json() == NOT_FOUND


def test_drop_to_expired_link_is_404(frozen_clock, owner, bare):
    frozen_clock(T0)
    made = _mk(owner.client, ttl="1h")
    frozen_clock(T0 + timedelta(hours=1))
    r = drop(bare, made["token"])
    assert r.status_code == 404 and r.json() == NOT_FOUND


def test_drop_duplicate_uuid_of_an_existing_file_is_409(owner, bare, settings):
    u = os.urandom(16).hex()
    assert upload(owner.client, None, u, meta=fake_meta(u), blob=make_fake_blob(u)).status_code == 201
    made = _mk(owner.client)
    r = drop(bare, made["token"], u)
    assert r.status_code == 409 and r.json()["error"] == "duplicate_uuid"
    assert _link_state(settings, made["id"]) == "waiting"


def test_drop_duplicate_uuid_of_another_pending_upload_is_409(owner, bare, settings):
    u = os.urandom(16).hex()
    made1 = _mk(owner.client)
    assert drop(bare, made1["token"], u).status_code == 201
    made2 = _mk(owner.client)
    r = drop(bare, made2["token"], u)
    assert r.status_code == 409 and r.json()["error"] == "duplicate_uuid"
    assert _link_state(settings, made2["id"]) == "waiting"


def test_two_concurrent_drops_to_one_token_have_one_winner(owner, app, settings):
    made = _mk(owner.client)
    barrier = threading.Barrier(2)
    results, lock = [], threading.Lock()
    uuids = [os.urandom(16).hex(), os.urandom(16).hex()]

    def post(i):
        with TestClient(app, base_url="http://testserver") as c:
            barrier.wait()
            r = drop(c, made["token"], uuids[i])
        with lock:
            results.append((r.status_code, uuids[i]))

    ts = [threading.Thread(target=post, args=(i,)) for i in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(c for c, _ in results) == [201, 404], results
    winner = next(u for c, u in results if c == 201)
    loser = next(u for c, u in results if c == 404)
    assert app.state.blobs.path_for(winner).is_file()
    assert not app.state.blobs.path_for(loser).is_file()
    row = _db(settings).execute("SELECT file_uuid FROM upload_links WHERE id = ?", (made["id"],)).fetchone()
    assert row["file_uuid"] == winner


# --- adopt -----------------------------------------------------------------------------------

def test_adopt_a_pending_link_creates_a_file(frozen_clock, owner, bare, settings):
    frozen_clock(T0)
    made = _mk(owner.client)
    u = os.urandom(16).hex()
    blob = make_fake_blob(u, body_len=77)
    assert drop(bare, made["token"], u, blob).status_code == 201
    r = owner.client.post(f"/api/upload-links/{made['id']}/adopt", json={"wrapped_dek": _wdek()})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["project"] == "upload"
    assert body["device"] == {"id": None, "name": "upload link"}
    assert body["uuid"] == u
    assert body["expires_at"] == "2026-10-01T12:00:00Z"
    listed = owner.client.get("/api/upload-links").json()["links"][0]
    assert listed["state"] == "received"
    assert listed["file"] == "FILE1"
    assert listed["pending"] is None
    blob_r = owner.client.get("/api/files/FILE1/blob")
    assert blob_r.status_code == 200 and blob_r.content == blob


def test_adopt_twice_is_409_already_adopted_with_one_files_row(owner, bare, settings):
    made = _mk(owner.client)
    drop(bare, made["token"])
    wdek = _wdek()
    r1 = owner.client.post(f"/api/upload-links/{made['id']}/adopt", json={"wrapped_dek": wdek})
    assert r1.status_code == 201, r1.text
    r2 = owner.client.post(f"/api/upload-links/{made['id']}/adopt", json={"wrapped_dek": wdek})
    assert r2.status_code == 409 and r2.json()["error"] == "already_adopted"
    assert _db(settings).execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1


def test_two_concurrent_adopts_have_one_winner(owner, bare, app, settings):
    made = _mk(owner.client)
    drop(bare, made["token"])
    wdek = _wdek()
    barrier = threading.Barrier(2)
    codes, lock = [], threading.Lock()

    def post():
        with TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver"}) as c:
            c.cookies = owner.client.cookies
            barrier.wait()
            r = c.post(f"/api/upload-links/{made['id']}/adopt", json={"wrapped_dek": wdek})
        with lock:
            codes.append(r.status_code)

    ts = [threading.Thread(target=post) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(codes) == [201, 409], codes
    assert _db(settings).execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1


def test_adopt_waiting_link_is_409_not_pending(owner):
    made = _mk(owner.client)
    r = owner.client.post(f"/api/upload-links/{made['id']}/adopt", json={"wrapped_dek": _wdek()})
    assert r.status_code == 409 and r.json()["error"] == "not_pending"


@pytest.mark.parametrize("w", [
    None, 5, "", "!!!",
    b64u_encode(b"\x01" + os.urandom(59)),     # too short
    b64u_encode(b"\x01" + os.urandom(61)),     # too long
    b64u_encode(b"\x02" + os.urandom(60)),     # wrong version byte
])
def test_adopt_bad_wrapped_dek_is_400(owner, bare, w):
    made = _mk(owner.client)
    drop(bare, made["token"])
    r = owner.client.post(f"/api/upload-links/{made['id']}/adopt", json={"wrapped_dek": w})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_adopt_unknown_link_is_404(owner):
    r = owner.client.post("/api/upload-links/upl_000000000000/adopt", json={"wrapped_dek": _wdek()})
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_unauthenticated_adopt_is_401(owner, bare):
    made = _mk(owner.client)
    drop(bare, made["token"])
    r = bare.post(f"/api/upload-links/{made['id']}/adopt", json={"wrapped_dek": _wdek()},
                 headers={"Origin": "http://testserver"})
    assert r.status_code == 401 and r.json()["error"] == "unauthenticated"


def test_delete_pending_link_via_real_upload_then_adopt_is_409(owner, bare, app, settings):
    made = _mk(owner.client)
    u = os.urandom(16).hex()
    assert drop(bare, made["token"], u).status_code == 201
    assert owner.client.delete(f"/api/upload-links/{made['id']}").status_code == 204
    assert not app.state.blobs.path_for(u).is_file()
    assert _link_state(settings, made["id"]) == "revoked"
    r = owner.client.post(f"/api/upload-links/{made['id']}/adopt", json={"wrapped_dek": _wdek()})
    assert r.status_code == 409 and r.json()["error"] == "not_pending"


def test_adopt_with_a_missing_blob_marks_the_link_revoked(owner, bare, app, settings):
    made = _mk(owner.client)
    u = os.urandom(16).hex()
    assert drop(bare, made["token"], u).status_code == 201
    app.state.blobs.path_for(u).unlink()     # simulate an interrupted disk write / a lost blob
    r = owner.client.post(f"/api/upload-links/{made['id']}/adopt", json={"wrapped_dek": _wdek()})
    assert r.status_code == 409 and r.json()["error"] == "not_pending"
    assert _link_state(settings, made["id"]) == "revoked"
    row = _db(settings).execute("SELECT * FROM upload_links WHERE id = ?", (made["id"],)).fetchone()
    assert row["revoked_at"] is not None and row["file_uuid"] is None


def test_revoke_after_a_successful_adopt_is_a_noop_and_keeps_the_blob(owner, bare, app, settings):
    made = _mk(owner.client)
    u = os.urandom(16).hex()
    assert drop(bare, made["token"], u).status_code == 201
    r = owner.client.post(f"/api/upload-links/{made['id']}/adopt", json={"wrapped_dek": _wdek()})
    assert r.status_code == 201, r.text
    assert owner.client.delete(f"/api/upload-links/{made['id']}").status_code == 204
    assert app.state.blobs.path_for(u).is_file()
    row = _db(settings).execute("SELECT * FROM upload_links WHERE id = ?", (made["id"],)).fetchone()
    assert row["file_n"] is not None and row["revoked_at"] is None


def test_revoke_racing_adopt_never_orphans_an_adopted_file_or_touches_a_received_row(owner, bare, app, settings):
    """global-constraints.md Review Focus 3, plus the revoke/adopt race the fix-round-1 review
    flagged: whichever of {adopt, revoke} commits first must decide the outcome cleanly, with no
    window where an adopted files row's blob has already been deleted by the loser."""
    for _round in range(5):
        made = _mk(owner.client)
        u = os.urandom(16).hex()
        assert drop(bare, made["token"], u).status_code == 201
        wdek = _wdek()
        barrier = threading.Barrier(2)
        results, lock = {}, threading.Lock()

        def do_adopt():
            with TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver"}) as c:
                c.cookies = owner.client.cookies
                barrier.wait()
                r = c.post(f"/api/upload-links/{made['id']}/adopt", json={"wrapped_dek": wdek})
            with lock:
                results["adopt"] = r.status_code

        def do_revoke():
            with TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver"}) as c:
                c.cookies = owner.client.cookies
                barrier.wait()
                r = c.delete(f"/api/upload-links/{made['id']}")
            with lock:
                results["revoke"] = r.status_code

        ts = [threading.Thread(target=do_adopt), threading.Thread(target=do_revoke)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        assert results["revoke"] == 204, results
        assert results["adopt"] in (201, 409), results
        row = _db(settings).execute("SELECT * FROM upload_links WHERE id = ?", (made["id"],)).fetchone()
        files_row = _db(settings).execute("SELECT * FROM files WHERE uuid = ?", (u,)).fetchone()
        if results["adopt"] == 201:
            # adopt won: the losing revoke must leave the now-received row untouched, and the
            # file's blob must still be on disk.
            assert files_row is not None
            assert app.state.blobs.path_for(u).is_file()
            assert row["file_n"] is not None and row["revoked_at"] is None
        else:
            # revoke won: nothing was ever adopted (no orphan files row), and the blob is gone.
            assert results["adopt"] == 409 and files_row is None
            assert not app.state.blobs.path_for(u).is_file()
            assert row["file_n"] is None and row["revoked_at"] is not None
