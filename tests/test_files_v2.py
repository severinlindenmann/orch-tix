"""v2 file features (spec §14): browser names on uploads (B), acknowledge (C), expiry (E), meta update (G)."""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fileshare import clock, expiry
from fileshare.app import create_app
from fileshare.db import connect
from fileshare.security import b64u_encode
from tests.helpers.files import fake_meta, upload
from tests.helpers.onboard import onboard_device

T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
FILEOUT_KEYS = {"id", "n", "uuid", "size", "key_version", "wrapped_dek", "enc_meta", "device", "project",
                "created_at", "deleted_at", "acked_at", "acked_by", "expires_at", "tags"}


def _up(client, headers=None, **meta_over):
    u = os.urandom(16).hex()
    meta = fake_meta(u) | meta_over
    r = upload(client, headers, u, meta=meta)
    assert r.status_code == 201, r.text
    return r.json()


def _get(owner, ref="FILE1", headers=None):
    return owner.client.get(f"/api/files/{ref}", headers=headers or {})


# --- B: browser uploads carry the session name ------------------------------------------

def test_fileout_has_v2_keys(owner, device):
    f = _up(owner.client, device.headers)
    assert set(f) == FILEOUT_KEYS
    assert f["device"] == {"id": device.id, "name": "laptop"}
    assert (f["acked_at"], f["acked_by"]) == (None, None)


def test_browser_upload_shows_session_name(owner):
    assert owner.client.patch("/api/sessions/self", json={"name": "iPhone · Safari"}).status_code == 204
    f = _up(owner.client)
    assert f["device"] == {"id": None, "name": "iPhone · Safari"}
    assert f["project"] == "web"
    # the name is recorded at upload time; a later rename does not rewrite history
    owner.client.patch("/api/sessions/self", json={"name": "renamed"})
    assert _get(owner).json()["device"] == {"id": None, "name": "iPhone · Safari"}


def test_unnamed_session_upload_shows_browser(owner):
    f = _up(owner.client)
    assert f["device"] == {"id": None, "name": "browser"}


# --- C: acknowledge ----------------------------------------------------------------------

def test_ack_as_session_and_idempotent(frozen_clock, owner):
    frozen_clock(T0)
    _up(owner.client)
    assert owner.client.post("/api/files/FILE1/ack").status_code == 204
    f = _get(owner).json()
    assert (f["acked_at"], f["acked_by"]) == ("2026-09-24T12:00:00Z", "you")
    frozen_clock(T0 + timedelta(hours=1))
    owner.client.patch("/api/sessions/self", json={"name": "phone"})
    assert owner.client.post("/api/files/FILE1/ack").status_code == 204
    f = _get(owner).json()
    assert (f["acked_at"], f["acked_by"]) == ("2026-09-24T12:00:00Z", "you")   # first ack kept


def test_ack_by_named_session(owner):
    owner.client.patch("/api/sessions/self", json={"name": "MacBook · Chrome"})
    _up(owner.client)
    owner.client.post("/api/files/FILE1/ack")
    assert _get(owner).json()["acked_by"] == "MacBook · Chrome"


def test_ack_as_any_device_and_unack(frozen_clock, owner, sharing, device):
    frozen_clock(T0)
    other = onboard_device(owner, sharing, "desk", "other")
    _up(owner.client, device.headers)             # uploaded by laptop, acked by desk
    assert owner.client.post("/api/files/FILE1/ack", headers=other.headers).status_code == 204
    f = _get(owner).json()
    assert (f["acked_at"], f["acked_by"]) == ("2026-09-24T12:00:00Z", "desk")
    assert owner.client.delete("/api/files/FILE1/ack", headers=device.headers).status_code == 204
    f = _get(owner).json()
    assert (f["acked_at"], f["acked_by"]) == (None, None)
    assert owner.client.delete("/api/files/FILE1/ack").status_code == 204        # unack is idempotent
    frozen_clock(T0 + timedelta(hours=2))
    owner.client.post("/api/files/FILE1/ack", headers=device.headers)
    f = _get(owner).json()
    assert (f["acked_at"], f["acked_by"]) == ("2026-09-24T14:00:00Z", "laptop")


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_ack_missing_or_deleted(owner, method):
    assert owner.client.request(method, "/api/files/FILE9/ack").status_code == 404
    _up(owner.client)
    owner.client.delete("/api/files/FILE1")
    r = owner.client.request(method, "/api/files/FILE1/ack")
    assert r.status_code == 410 and r.json()["error"] == "deleted"


def test_ack_checks_origin(owner):
    _up(owner.client)
    r = owner.client.post("/api/files/FILE1/ack", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"] == "bad_origin"


def _ids(body):
    return [f["id"] for f in body["files"]]


def test_list_hides_acked_unless_asked(owner, device):
    for _ in range(3):
        _up(owner.client, device.headers)
    owner.client.post("/api/files/FILE2/ack")
    assert _ids(owner.client.get("/api/files").json()) == ["FILE3", "FILE1"]
    assert _ids(owner.client.get("/api/files?acked=0").json()) == ["FILE3", "FILE1"]
    assert _ids(owner.client.get("/api/files?acked=1").json()) == ["FILE3", "FILE2", "FILE1"]
    assert _ids(owner.client.get("/api/files", headers=device.headers).json()) == ["FILE3", "FILE1"]


def test_acked_tombstones_stay_listed(owner):
    _up(owner.client)
    owner.client.post("/api/files/FILE1/ack")
    owner.client.delete("/api/files/FILE1")
    [f] = owner.client.get("/api/files").json()["files"]
    assert f["id"] == "FILE1" and f["deleted_at"] is not None


@pytest.mark.parametrize("qs", ["acked=2", "acked=yes", "acked="])
def test_list_rejects_bad_acked(owner, qs):
    r = owner.client.get(f"/api/files?{qs}")
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_pagination_with_acked_rows(owner):
    for _ in range(8):
        _up(owner.client)
    for n in (8, 7, 5, 4, 3):
        owner.client.post(f"/api/files/FILE{n}/ack")
    # visible: 6, 2, 1 — pages of 2 must not come back short or skip rows
    p1 = owner.client.get("/api/files?limit=2").json()
    assert _ids(p1) == ["FILE6", "FILE2"] and p1["next_before"] == 2
    p2 = owner.client.get(f"/api/files?limit=2&before={p1['next_before']}").json()
    assert _ids(p2) == ["FILE1"] and p2["next_before"] is None
    p = owner.client.get("/api/files?limit=3").json()
    assert _ids(p) == ["FILE6", "FILE2", "FILE1"] and p["next_before"] is None
    a = owner.client.get("/api/files?limit=3&acked=1").json()
    assert _ids(a) == ["FILE8", "FILE7", "FILE6"] and a["next_before"] == 6
    a2 = owner.client.get("/api/files?limit=3&acked=1&before=6").json()
    assert _ids(a2) == ["FILE5", "FILE4", "FILE3"] and a2["next_before"] == 3


# --- E: expiry ---------------------------------------------------------------------------

def test_default_ttl_is_seven_days(frozen_clock, owner, device):
    frozen_clock(T0)
    f = _up(owner.client, device.headers)
    assert f["created_at"] == "2026-09-24T12:00:00Z"
    assert f["expires_at"] == "2026-10-01T12:00:00Z"


@pytest.mark.parametrize("ttl,expires", [("1d", "2026-09-25T12:00:00Z"), ("7d", "2026-10-01T12:00:00Z"),
                                         ("30d", "2026-10-24T12:00:00Z"), ("never", None)])
def test_each_ttl(frozen_clock, owner, ttl, expires):
    frozen_clock(T0)
    assert _up(owner.client, ttl=ttl)["expires_at"] == expires


@pytest.mark.parametrize("ttl", ["2d", "", "7D", 7, None, "forever"])
def test_bad_ttl_is_bad_meta(owner, ttl):
    u = os.urandom(16).hex()
    r = upload(owner.client, meta=fake_meta(u) | {"ttl": ttl}, uuid_hex=u)
    assert r.status_code == 400 and r.json()["error"] == "bad_meta"


def test_patch_ttl_as_session_recomputes_from_now(frozen_clock, owner, device):
    frozen_clock(T0)
    _up(owner.client, device.headers, ttl="1d")
    frozen_clock(T0 + timedelta(hours=5))
    assert owner.client.patch("/api/files/FILE1", json={"ttl": "30d"}).status_code == 204
    assert _get(owner).json()["expires_at"] == "2026-10-24T17:00:00Z"
    assert owner.client.patch("/api/files/FILE1", json={"ttl": "never"}).status_code == 204
    assert _get(owner).json()["expires_at"] is None


def test_patch_ttl_device_permissions(frozen_clock, owner, sharing, device):
    frozen_clock(T0)
    other = onboard_device(owner, sharing, "desk", "other")
    _up(owner.client, device.headers)
    r = owner.client.patch("/api/files/FILE1", json={"ttl": "never"}, headers=other.headers)
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert _get(owner).json()["expires_at"] == "2026-10-01T12:00:00Z"
    assert owner.client.patch("/api/files/FILE1", json={"ttl": "1d"}, headers=device.headers).status_code == 204
    assert _get(owner).json()["expires_at"] == "2026-09-25T12:00:00Z"


@pytest.mark.parametrize("body", [{}, {"ttl": "2d"}, {"ttl": None}, {"ttl": 1}])
def test_patch_ttl_rejects_bad_body(owner, body):
    _up(owner.client)
    r = owner.client.patch("/api/files/FILE1", json=body)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_patch_ttl_missing_or_deleted(owner):
    assert owner.client.patch("/api/files/FILE1", json={"ttl": "1d"}).status_code == 404
    _up(owner.client)
    owner.client.delete("/api/files/FILE1")
    assert owner.client.patch("/api/files/FILE1", json={"ttl": "1d"}).status_code == 410


def test_sweep_tombstones_expired_and_keeps_never(frozen_clock, owner, app, settings):
    frozen_clock(T0)
    a = _up(owner.client)                     # 7d
    b = _up(owner.client, ttl="never")
    c = _up(owner.client, ttl="30d")
    blobs = app.state.blobs
    conn = connect(settings.db_path)
    try:
        assert expiry.expire_files(conn, blobs, T0 + timedelta(days=7) - timedelta(seconds=1)) == 0
        assert expiry.expire_files(conn, blobs, T0 + timedelta(days=8)) == 1
        assert expiry.expire_files(conn, blobs, T0 + timedelta(days=8)) == 0   # idempotent
    finally:
        conn.close()
    r = _get(owner, a["id"])
    assert r.status_code == 410
    gone = r.json()["file"]
    assert gone["deleted_at"] == "2026-10-02T12:00:00Z"
    assert gone["wrapped_dek"] is None and gone["enc_meta"] is None
    assert not blobs.path_for(a["uuid"]).exists()
    raw = connect(settings.db_path).execute("SELECT wrapped_dek, enc_meta FROM files WHERE n=1").fetchone()
    assert tuple(raw) == ("", "")
    for f in (b, c):
        assert _get(owner, f["id"]).status_code == 200
        assert blobs.path_for(f["uuid"]).exists()


def test_startup_sweep_expires_files(frozen_clock, owner, settings, app):
    frozen_clock(T0)
    f = _up(owner.client, ttl="1d")
    frozen_clock(T0 + timedelta(days=2))
    create_app(settings)
    assert not app.state.blobs.path_for(f["uuid"]).exists()
    assert _get(owner).status_code == 410


def test_background_sweep_runs_periodically(frozen_clock, monkeypatch, owner, settings):
    frozen_clock(T0)
    f = _up(owner.client, ttl="1d")
    monkeypatch.setattr(expiry, "SWEEP_EVERY_S", 0.05)
    app2 = create_app(settings)          # startup sweep: nothing is due yet
    with TestClient(app2):
        frozen_clock(T0 + timedelta(days=2))
        deadline = time.monotonic() + 5
        while _get(owner).status_code != 410 and time.monotonic() < deadline:
            time.sleep(0.05)
    assert _get(owner).status_code == 410
    assert not app2.state.blobs.path_for(f["uuid"]).exists()


# --- G: metadata update ------------------------------------------------------------------

def _env(n_bytes: int) -> str:
    return b64u_encode(b"\x01" + os.urandom(n_bytes - 1))


def test_patch_meta_replaces_enc_meta_only(owner, device):
    f = _up(owner.client, device.headers)
    new = _env(200)
    assert owner.client.patch("/api/files/FILE1/meta", json={"enc_meta": new}).status_code == 204
    g = _get(owner).json()
    assert g["enc_meta"] == new
    assert g["wrapped_dek"] == f["wrapped_dek"]
    assert g["uuid"] == f["uuid"] and g["size"] == f["size"]
    assert owner.client.get("/api/files/FILE1/blob").status_code == 200


def test_patch_meta_by_any_active_device_accepts_large(owner, sharing, device):
    other = onboard_device(owner, sharing, "desk", "other")
    _up(owner.client, device.headers)
    big = _env(225_000)                      # 300 000 base64 chars
    assert len(big) == 300_000
    r = owner.client.patch("/api/files/FILE1/meta", json={"enc_meta": big}, headers=other.headers)
    assert r.status_code == 204, r.text
    assert _get(owner).json()["enc_meta"] == big


def test_upload_accepts_large_enc_meta(owner):
    big = _env(300_000)
    assert len(big) == 400_000
    assert _up(owner.client, enc_meta=big)["enc_meta"] == big


@pytest.mark.parametrize("enc_meta", [
    None, 5, "", "not base64!",
    b64u_encode(b"\x02" + os.urandom(40)),          # wrong envelope version
    b64u_encode(b"\x01" + os.urandom(27)),          # shorter than nonce + tag
    "A" * 524289,                                   # over the cap
])
def test_patch_meta_rejects_bad_envelope(owner, enc_meta):
    f = _up(owner.client)
    r = owner.client.patch("/api/files/FILE1/meta", json={"enc_meta": enc_meta})
    assert r.status_code == 400 and r.json()["error"] == "bad_meta"
    assert _get(owner).json()["enc_meta"] == f["enc_meta"]


def test_patch_meta_rejects_non_object_body(owner):
    _up(owner.client)
    r = owner.client.patch("/api/files/FILE1/meta", content=b"[1]",
                           headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_patch_meta_missing_or_deleted(owner):
    assert owner.client.patch("/api/files/FILE1/meta", json={"enc_meta": _env(50)}).status_code == 404
    _up(owner.client)
    owner.client.delete("/api/files/FILE1")
    r = owner.client.patch("/api/files/FILE1/meta", json={"enc_meta": _env(50)})
    assert r.status_code == 410 and r.json()["error"] == "deleted"


def test_patch_meta_checks_origin(owner):
    _up(owner.client)
    r = owner.client.patch("/api/files/FILE1/meta", json={"enc_meta": _env(50)},
                           headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"] == "bad_origin"


def test_wrapped_dek_cap_unchanged(owner):
    u = os.urandom(16).hex()
    r = upload(owner.client, uuid_hex=u, meta=fake_meta(u) | {"wrapped_dek": _env(62)})
    assert r.status_code == 400 and r.json()["error"] == "bad_meta"


def test_patch_meta_refuses_oversized_body_early(owner):
    _up(owner.client)
    body = b'{"enc_meta": "' + b"A" * 600_000 + b'"}'
    r = owner.client.patch("/api/files/FILE1/meta", content=body,
                           headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"] == "too_large"


# --- exact expiry: no sweep needed ---------------------------------------------------------

def _expire_first_file(frozen_clock, owner):
    frozen_clock(T0)
    f = _up(owner.client, ttl="1d")
    keep = _up(owner.client, ttl="never")
    frozen_clock(T0 + timedelta(days=1, seconds=1))   # past expires_at; the hourly sweep never ran
    return f, keep


def test_expired_file_is_gone_on_get_and_blob(frozen_clock, owner, app):
    f, _ = _expire_first_file(frozen_clock, owner)
    r = _get(owner)
    assert r.status_code == 410 and r.json()["error"] == "deleted"
    assert r.json()["file"]["deleted_at"] == "2026-09-25T12:00:01Z"
    assert r.json()["file"]["enc_meta"] is None
    assert owner.client.get("/api/files/FILE1/blob").status_code == 410
    assert not app.state.blobs.path_for(f["uuid"]).exists()


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/files/FILE1/blob", None),
    ("PATCH", "/api/files/FILE1", {"ttl": "never"}),
    ("PATCH", "/api/files/FILE1/meta", {"enc_meta": b64u_encode(b"\x01" + bytes(40))}),
    ("POST", "/api/files/FILE1/ack", None),
    ("DELETE", "/api/files/FILE1", None),
])
def test_expired_file_cannot_be_revived_or_touched(frozen_clock, owner, method, path, body):
    _expire_first_file(frozen_clock, owner)
    r = owner.client.request(method, path, json=body)
    assert r.status_code == 410 and r.json()["error"] == "deleted"
    assert _get(owner).json()["file"]["expires_at"] == "2026-09-25T12:00:00Z"   # ttl not revived


def test_list_expires_due_files_first(frozen_clock, owner, app):
    f, keep = _expire_first_file(frozen_clock, owner)
    listed = {x["id"]: x for x in owner.client.get("/api/files").json()["files"]}
    assert listed["FILE1"]["deleted_at"] == "2026-09-25T12:00:01Z"   # shown as a tombstone
    assert listed["FILE1"]["wrapped_dek"] is None
    assert listed[keep["id"]]["deleted_at"] is None
    assert not app.state.blobs.path_for(f["uuid"]).exists()


def test_patch_meta_streamed_oversize_body_is_413(owner):
    _up(owner.client)

    def chunks():                                  # no Content-Length: chunked transfer
        yield b'{"enc_meta": "'
        for _ in range(20):
            yield b"A" * 50_000
        yield b'"}'

    r = owner.client.patch("/api/files/FILE1/meta", content=chunks(),
                           headers={"Content-Type": "application/json"})
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 413 and r.json()["error"] == "too_large"


@pytest.mark.parametrize("raw", [b"{not json", b"\xff\xfe", b""])
def test_patch_meta_invalid_json_is_bad_request(owner, raw):
    _up(owner.client)
    r = owner.client.patch("/api/files/FILE1/meta", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
