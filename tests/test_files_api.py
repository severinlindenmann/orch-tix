import dataclasses
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fileshare.app import create_app
from tests.helpers.blobs import make_fake_blob
from tests.helpers.files import fake_meta, upload
from tests.helpers.onboard import onboard_device

REPO = Path(__file__).resolve().parents[1]


def tmp_files(settings):
    return list((settings.data_dir / "tmp").iterdir())


def test_device_upload_returns_fileout(owner, device):
    u = os.urandom(16).hex()
    blob = make_fake_blob(u, body_len=100)
    meta = fake_meta(u)
    r = upload(owner.client, device.headers, u, blob, meta)
    assert r.status_code == 201, r.text
    f = r.json()
    assert f == {
        "id": "FILE1", "n": 1, "uuid": u, "size": len(blob), "key_version": 1,
        "wrapped_dek": meta["wrapped_dek"], "enc_meta": meta["enc_meta"],
        "device": {"id": device.id, "name": "laptop"}, "project": "proj",
        "created_at": f["created_at"], "deleted_at": None,
        "acked_at": None, "acked_by": None, "expires_at": f["expires_at"],
        "tags": [],
    }


def test_session_upload_is_recorded_as_web(owner):
    r = upload(owner.client)
    assert r.status_code == 201, r.text
    assert r.json()["device"] == {"id": None, "name": "browser"}
    assert r.json()["project"] == "web"


def test_project_comes_from_device_not_request(owner, device):
    u = os.urandom(16).hex()
    meta = fake_meta(u) | {"project": "spoofed"}
    r = upload(owner.client, device.headers, u, meta=meta)
    assert r.status_code == 201
    assert r.json()["project"] == "proj"


def test_blob_roundtrip_and_disk_layout(owner, device, app):
    u = os.urandom(16).hex()
    blob = make_fake_blob(u, body_len=5000)
    upload(owner.client, device.headers, u, blob)
    r = owner.client.get("/api/files/FILE1/blob", headers=device.headers)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.content == blob
    path = app.state.blobs.path_for(u)
    assert path.parts[-3:] == (u[0:2], u[2:4], f"{u}.shr")
    assert path.read_bytes() == blob


@pytest.mark.parametrize("ref", ["FILE1", "file1", "File1", "1"])
def test_ref_forms_resolve(owner, device, ref):
    upload(owner.client, device.headers)
    r = owner.client.get(f"/api/files/{ref}", headers=device.headers)
    assert r.status_code == 200
    assert r.json()["id"] == "FILE1"


@pytest.mark.parametrize("ref", ["FILE0", "0", "FILE01", "abc", "FILE-1", "FILE2"])
def test_bad_or_unknown_ref_is_404(owner, device, ref):
    upload(owner.client, device.headers)
    r = owner.client.get(f"/api/files/{ref}", headers=device.headers)
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_list_newest_first_with_cursor(owner, device):
    for _ in range(3):
        upload(owner.client, device.headers)
    page = owner.client.get("/api/files?limit=2", headers=device.headers).json()
    assert [f["id"] for f in page["files"]] == ["FILE3", "FILE2"]
    assert page["next_before"] == 2
    page2 = owner.client.get("/api/files?limit=2&before=2", headers=device.headers).json()
    assert [f["id"] for f in page2["files"]] == ["FILE1"]
    assert page2["next_before"] is None


def test_list_default_limit_is_50(owner):
    body = owner.client.get("/api/files").json()
    assert body == {"files": [], "next_before": None}


@pytest.mark.parametrize("qs", ["limit=0", "limit=201", "limit=abc", "before=x", "before=-1"])
def test_list_rejects_bad_params(owner, qs):
    r = owner.client.get(f"/api/files?{qs}")
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def _mutated_meta(u, change):
    m = fake_meta(u)
    change(m)
    return m


@pytest.mark.parametrize("change", [
    lambda m: m.update(uuid=m["uuid"].upper()),
    lambda m: m.update(uuid=m["uuid"][:30]),
    lambda m: m.update(key_version="1"),
    lambda m: m.update(key_version=True),
    lambda m: m.update(key_version=0),
    lambda m: m.update(wrapped_dek="not base64!"),
    lambda m: m.update(wrapped_dek="AQ"),
    lambda m: m.update(enc_meta="A" * 524289),
    lambda m: m.update(wrapped_dek="A" * 5000),
    lambda m: m.update(enc_meta="A" * 5000),   # decodes, but not a v1 envelope
    lambda m: m.pop("enc_meta"),
])
def test_bad_meta_is_400(owner, device, settings, change):
    u = os.urandom(16).hex()
    r = upload(owner.client, device.headers, u, meta=_mutated_meta(u, change))
    assert r.status_code == 400
    assert r.json()["error"] == "bad_meta"
    assert tmp_files(settings) == []


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", "null"])
def test_meta_must_be_a_json_object(owner, device, raw):
    r = upload(owner.client, device.headers, meta=raw)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_meta"


def test_missing_blob_part_is_400(owner, device):
    r = owner.client.post("/api/files", data={"meta": json.dumps(fake_meta(os.urandom(16).hex()))},
                          headers=device.headers)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_json_body_is_400(owner, device):
    r = owner.client.post("/api/files", json={"meta": {}}, headers=device.headers)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


@pytest.mark.parametrize("blob_fn", [
    lambda u: b"NOPE" + make_fake_blob(u)[4:],
    lambda u: make_fake_blob(os.urandom(16).hex()),
    lambda u: make_fake_blob(u, key_version=2),
    lambda u: make_fake_blob(u)[:40],
])
def test_bad_blob_is_400_and_leaves_nothing(owner, device, settings, blob_fn):
    u = os.urandom(16).hex()
    r = upload(owner.client, device.headers, u, blob=blob_fn(u))
    assert r.status_code == 400
    assert r.json()["error"] == "bad_blob"
    assert tmp_files(settings) == []
    assert owner.client.get("/api/files").json()["files"] == []


def test_duplicate_uuid_is_409(owner, device, settings):
    u = os.urandom(16).hex()
    assert upload(owner.client, device.headers, u).status_code == 201
    r = upload(owner.client, device.headers, u)
    assert r.status_code == 409
    assert r.json()["error"] == "duplicate_uuid"
    assert len(owner.client.get("/api/files").json()["files"]) == 1
    assert tmp_files(settings) == []


def test_a_uuid_unique_violation_from_the_insert_is_still_409(owner, device, settings, monkeypatch):
    """Belt and braces: if the pre-check ever misses, SQLite's own uuid UNIQUE error is a duplicate."""
    import sqlite3

    from fileshare.routes import files as files_routes
    u = os.urandom(16).hex()

    def boom(conn, n, tags):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: files.uuid")
    monkeypatch.setattr(files_routes, "set_tags", boom)
    r = upload(owner.client, device.headers, u)
    assert r.status_code == 409 and r.json()["error"] == "duplicate_uuid"
    assert tmp_files(settings) == []


@pytest.mark.parametrize("message", ["FOREIGN KEY constraint failed",
                                     "UNIQUE constraint failed: file_tags.file_n, file_tags.tag",
                                     "NOT NULL constraint failed: files.enc_meta"])
def test_another_integrity_error_is_not_a_duplicate(owner, device, settings, monkeypatch, message):
    import sqlite3

    from fileshare.routes import files as files_routes
    u = os.urandom(16).hex()

    def boom(conn, n, tags):
        raise sqlite3.IntegrityError(message)
    monkeypatch.setattr(files_routes, "set_tags", boom)
    r = upload(owner.client, device.headers, u)
    assert r.status_code == 500
    assert r.json()["error"] == "integrity_error"
    assert owner.client.get("/api/files").json()["files"] == []   # rolled back
    assert tmp_files(settings) == []
    assert not (settings.data_dir / "blobs").exists() or not any((settings.data_dir / "blobs").rglob(u + "*"))


def test_delete_permissions(owner, sharing, device):
    other = onboard_device(owner, sharing, "desk", "proj2")
    upload(owner.client, device.headers)   # FILE1 by laptop
    upload(owner.client, other.headers)    # FILE2 by desk
    r = owner.client.delete("/api/files/FILE1", headers=other.headers)
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden"
    assert owner.client.delete("/api/files/FILE1", headers=device.headers).status_code == 204
    assert owner.client.delete("/api/files/FILE2").status_code == 204   # session may delete any


def test_tombstone_behaviour(owner, device, app):
    u = os.urandom(16).hex()
    upload(owner.client, device.headers, u)
    assert owner.client.delete("/api/files/FILE1").status_code == 204
    r = owner.client.get("/api/files/file1", headers=device.headers)
    assert r.status_code == 410
    body = r.json()
    assert body["error"] == "deleted"
    assert body["detail"] == "FILE1 was deleted"
    assert body["file"]["deleted_at"] is not None
    assert body["file"]["wrapped_dek"] is None and body["file"]["enc_meta"] is None
    assert owner.client.get("/api/files/FILE1/blob").status_code == 410
    assert owner.client.delete("/api/files/FILE1").status_code == 410
    assert not app.state.blobs.path_for(u).exists()
    listed = owner.client.get("/api/files").json()["files"]
    assert [(f["id"], f["deleted_at"] is not None) for f in listed] == [("FILE1", True)]


def test_ids_are_never_reused(owner, device):
    upload(owner.client, device.headers)
    owner.client.delete("/api/files/FILE1")
    assert upload(owner.client, device.headers).json()["id"] == "FILE2"


def test_files_require_auth(app):
    bare = TestClient(app, base_url="http://testserver")
    assert bare.get("/api/files").status_code == 401
    assert bare.get("/api/files/FILE1/blob").status_code == 401


def test_session_upload_checks_origin(owner):
    u = os.urandom(16).hex()
    r = owner.client.post(
        "/api/files",
        data={"meta": json.dumps(fake_meta(u))},
        files={"blob": ("blob", make_fake_blob(u), "application/octet-stream")},
        headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert r.json()["error"] == "bad_origin"


def test_startup_sweep_removes_orphans_and_stale_tmp(owner, device, settings, app):
    live = os.urandom(16).hex()
    upload(owner.client, device.headers, live)
    orphan = os.urandom(16).hex()
    orphan_path = app.state.blobs.path_for(orphan)
    orphan_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_path.write_bytes(make_fake_blob(orphan))
    stale = settings.data_dir / "tmp" / "stale-upload"
    stale.write_bytes(b"x")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    fresh = settings.data_dir / "tmp" / "fresh-upload"
    fresh.write_bytes(b"x")

    create_app(settings)

    assert app.state.blobs.path_for(live).exists()
    assert not orphan_path.exists()
    assert not stale.exists()
    assert fresh.exists()


def test_server_never_imports_cryptography(tmp_path):
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from fileshare.app import create_app\n"
        "from fileshare.settings import Settings\n"
        f"create_app(Settings(data_dir=Path({str(tmp_path)!r}), public_url='http://x'))\n"
        "bad = sorted(m for m in sys.modules if m == 'cryptography' or m.startswith('cryptography.'))\n"
        "assert not bad, bad\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO)
    assert r.returncode == 0, r.stderr
