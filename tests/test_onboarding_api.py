import dataclasses
import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fileshare.app import create_app
from fileshare.routes.devices import device_out, device_status
from fileshare.routes.onboarding import skill_manifest
from fileshare.security import b64u_encode, fingerprint, sha256_hex
from tests.helpers.onboard import create_token, handshake, new_code

T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
CODE_RE = re.compile(r"^shr1\.[A-Za-z0-9_-]{22}$")
DEVICE_OUT_KEYS = {"id", "name", "project", "hostname", "platform", "fingerprint", "pubkey",
                   "status", "created_at", "approved_at", "last_seen_at", "revoked_at"}


def db(settings):
    conn = sqlite3.connect(settings.data_dir / "fileshare.db")
    conn.row_factory = sqlite3.Row
    return conn


def valid_body(sharing, lookup: bytes, **over) -> dict:
    _, pub = sharing.new_device_keypair()
    body = {"lookup": b64u_encode(lookup), "device_name": "laptop", "project": "proj",
            "hostname": "h", "platform": "linux", "device_pub": b64u_encode(pub)}
    body.update(over)
    return body


# --- device_status / device_out ----------------------------------------------

BASE_ROW = {"id": "dev_x", "name": "n", "project": "p", "hostname": "h", "platform": "linux",
            "fingerprint": "ABCD-EFGH", "pubkey": "AA", "created_at": "t0",
            "approved_at": None, "last_seen_at": None, "revoked_at": None,
            "token_hash": "secret", "device_bundle": "bundle"}


@pytest.mark.parametrize("approved,revoked,status", [
    (None, None, "pending"), ("t1", None, "active"), ("t1", "t2", "revoked"), (None, "t2", "revoked"),
])
def test_device_status(approved, revoked, status):
    row = BASE_ROW | {"approved_at": approved, "revoked_at": revoked}
    assert device_status(row) == status
    out = device_out(row)
    assert set(out) == DEVICE_OUT_KEYS
    assert out["status"] == status
    assert "token_hash" not in out and "device_bundle" not in out


# --- onboarding tokens --------------------------------------------------------

def test_new_code_format():
    code, lookup = new_code()
    assert CODE_RE.fullmatch(code)
    assert code == "shr1." + b64u_encode(lookup)


def test_create_token_requires_session(client):
    r = client.post("/api/onboarding-tokens", json={"lookup": b64u_encode(bytes(16))})
    assert r.status_code == 401
    assert r.json()["error"] == "unauthenticated"


def test_create_token_returns_id_and_expiry(owner):
    r = owner.client.post("/api/onboarding-tokens", json={"lookup": b64u_encode(bytes(16))})
    assert r.status_code == 201
    assert set(r.json()) == {"id", "expires_at"}
    assert r.json()["id"].startswith("onb_")


@pytest.mark.parametrize("body", [
    {},
    {"lookup": b64u_encode(bytes(15))},
    {"lookup": b64u_encode(bytes(17))},
    {"lookup": "not base64!"},
    {"lookup": 7},
])
def test_create_token_validates_lookup(owner, body):
    r = owner.client.post("/api/onboarding-tokens", json=body)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_duplicate_lookup_is_409(owner):
    _, lookup = new_code()
    create_token(owner, lookup)
    r = owner.client.post("/api/onboarding-tokens", json={"lookup": b64u_encode(lookup)})
    assert r.status_code == 409


# --- handshake ------------------------------------------------------------------

def test_handshake_creates_pending_device(owner, sharing, settings):
    _, lookup = new_code()
    create_token(owner, lookup)
    r, priv, pub = handshake(owner.client, sharing, lookup, "laptop", "proj", "host1", "darwin")
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"device_id", "device_token", "fingerprint", "server_url"}
    assert body["device_id"].startswith("dev_")
    assert body["device_token"].startswith("shd_")
    assert body["server_url"] == "http://testserver"
    assert body["fingerprint"] == fingerprint(pub) == sharing.fingerprint(pub)
    row = db(settings).execute("SELECT * FROM devices").fetchone()
    assert row["pubkey"] == b64u_encode(pub)
    assert row["fingerprint"] == body["fingerprint"]
    assert (row["platform"], row["hostname"]) == ("darwin", "host1")
    assert row["approved_at"] is None and row["device_bundle"] is None and row["revoked_at"] is None


def test_only_hashes_are_stored(owner, sharing, settings):
    _, lookup = new_code()
    create_token(owner, lookup)
    body = handshake(owner.client, sharing, lookup)[0].json()
    conn = db(settings)
    tok = conn.execute("SELECT lookup_hash FROM onboarding_tokens").fetchone()
    assert tok["lookup_hash"] == sha256_hex(lookup)
    dev = conn.execute("SELECT token_hash FROM devices").fetchone()
    assert dev["token_hash"] == sha256_hex(body["device_token"])
    dump = "\n".join(conn.iterdump())
    assert body["device_token"] not in dump
    assert b64u_encode(lookup) not in dump


def test_handshake_twice_is_refused(owner, sharing):
    _, lookup = new_code()
    create_token(owner, lookup)
    assert handshake(owner.client, sharing, lookup)[0].status_code == 201
    r = handshake(owner.client, sharing, lookup, name="other")[0]
    assert r.status_code == 410
    assert r.json()["error"] == "refused"


def test_unknown_lookup_is_refused_identically(owner, sharing):
    r = handshake(owner.client, sharing, new_code()[1])[0]
    assert r.status_code == 410
    assert r.json()["error"] == "refused"


def test_expired_token_is_refused(frozen_clock, owner, sharing):
    frozen_clock(T0)
    _, lookup = new_code()
    create_token(owner, lookup)
    frozen_clock(T0 + timedelta(minutes=15, seconds=1))
    assert handshake(owner.client, sharing, lookup)[0].status_code == 410


def test_token_valid_just_before_expiry(frozen_clock, owner, sharing):
    frozen_clock(T0)
    _, lookup = new_code()
    token_id = create_token(owner, lookup)
    assert owner.client.get(f"/api/onboarding-tokens/{token_id}").json()["expires_at"] == "2026-09-24T12:15:00Z"
    frozen_clock(T0 + timedelta(minutes=14, seconds=59))
    assert handshake(owner.client, sharing, lookup)[0].status_code == 201


@pytest.mark.parametrize("field,value", [
    ("device_name", ""),
    ("device_name", "a" * 65),
    ("device_name", "has space"),
    ("device_name", "../x"),
    ("device_name", "naïve"),
    ("project", "a/b"),
    ("project", 7),
    ("hostname", "h" * 256),
    ("platform", "plan9"),
    ("platform", None),
    ("platform", ["linux"]),
])
def test_invalid_input_is_refused_without_spending_token(owner, sharing, field, value):
    _, lookup = new_code()
    create_token(owner, lookup)
    r = owner.client.post("/api/handshake", json=valid_body(sharing, lookup, **{field: value}))
    assert r.status_code == 410
    assert r.json()["error"] == "refused"
    assert handshake(owner.client, sharing, lookup)[0].status_code == 201


@pytest.mark.parametrize("bad_pub", [
    None,
    "not base64!",
    "AAAA",
    "off-curve",
])
def test_bad_device_pub_is_400_without_spending_token(owner, sharing, bad_pub):
    _, lookup = new_code()
    create_token(owner, lookup)
    body = valid_body(sharing, lookup)
    if bad_pub is None:
        del body["device_pub"]
    elif bad_pub == "off-curve":
        _, pub = sharing.new_device_keypair()
        body["device_pub"] = b64u_encode(pub[:-1] + bytes([pub[-1] ^ 1]))
    else:
        body["device_pub"] = bad_pub
    r = owner.client.post("/api/handshake", json=body)
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"
    assert handshake(owner.client, sharing, lookup)[0].status_code == 201


def test_non_json_handshake_is_refused(client):
    r = client.post("/api/handshake", content=b"nope", headers={"Content-Type": "application/json"})
    assert r.status_code == 410


def test_handshake_rate_limited_after_ten_failures(owner, sharing):
    for _ in range(10):
        assert handshake(owner.client, sharing, new_code()[1])[0].status_code == 410
    r = handshake(owner.client, sharing, new_code()[1])[0]
    assert r.status_code == 429
    assert r.json()["error"] == "rate_limited"


def test_token_status_shows_pending_device_after_use(owner, sharing):
    _, lookup = new_code()
    token_id = create_token(owner, lookup)
    before = owner.client.get(f"/api/onboarding-tokens/{token_id}").json()
    assert set(before) == {"id", "expires_at", "used_at", "device"}
    assert before["used_at"] is None and before["device"] is None
    r, _, pub = handshake(owner.client, sharing, lookup, "thinkpad", "etl", "tp", "windows")
    after = owner.client.get(f"/api/onboarding-tokens/{token_id}").json()
    assert after["used_at"] is not None
    dev = after["device"]
    assert set(dev) == DEVICE_OUT_KEYS
    assert dev["id"] == r.json()["device_id"]
    assert (dev["name"], dev["project"], dev["platform"]) == ("thinkpad", "etl", "windows")
    assert dev["status"] == "pending"
    assert dev["fingerprint"] == fingerprint(pub)
    assert dev["pubkey"] == b64u_encode(pub)


def test_token_status_unknown_is_404(owner):
    r = owner.client.get("/api/onboarding-tokens/onb_000000000000")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_delete_token_revokes_it(owner, sharing):
    _, lookup = new_code()
    token_id = create_token(owner, lookup)
    assert owner.client.delete(f"/api/onboarding-tokens/{token_id}").status_code == 204
    assert owner.client.get(f"/api/onboarding-tokens/{token_id}").status_code == 404
    assert handshake(owner.client, sharing, lookup)[0].status_code == 410
    assert owner.client.delete(f"/api/onboarding-tokens/{token_id}").status_code == 404


# --- installers -----------------------------------------------------------------

def test_onboarding_txt_is_public_and_templated(client):
    r = client.get("/onboarding.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["cache-control"] == "no-store"
    assert "http://testserver" in r.text
    assert "{{PUBLIC_URL}}" not in r.text
    assert r.text.startswith("#!/usr/bin/env bash")
    assert 'main "$@"' in r.text


def test_onboarding_ps1_is_public_and_templated(client):
    r = client.get("/onboarding.ps1")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["cache-control"] == "no-store"
    assert "http://testserver" in r.text
    assert "{{PUBLIC_URL}}" not in r.text
    assert "param(" in r.text
    assert "throw" in r.text
    # `exit` inside a script block run with `& ([scriptblock]::Create(...))` would close the user's shell.
    assert not re.search(r"^\s*exit\b", r.text, re.M | re.I)


# --- skill files and manifest --------------------------------------------------------

def make_skill_dir(tmp_path, version="1.2.3"):
    skill = tmp_path / "skill"
    skill.mkdir()
    files = {
        "SKILL.md": b"# skill\n",
        "tickets-SKILL.md": b"# tickets\n",
        "sharing.py": f'import sys\nVERSION = "{version}"   # semver\n'.encode(),
        "sharing": b"#!/usr/bin/env bash\n",
        "sharing.cmd": b"@echo off\r\n",
    }
    for name, content in files.items():
        (skill / name).write_bytes(content)
    (skill / "secret.txt").write_text("nope\n")
    return skill, files


def skill_client(settings, skill):
    return TestClient(create_app(dataclasses.replace(settings, skill_dir=skill)),
                      base_url="http://testserver")


def test_skill_files_served_from_allowlist(settings, tmp_path):
    skill, files = make_skill_dir(tmp_path)
    c = skill_client(settings, skill)
    for name, content in files.items():
        r = c.get(f"/skill/{name}")
        assert r.status_code == 200, name
        assert r.content == content
        assert r.headers["cache-control"] == "no-store"
    assert c.get("/skill/secret.txt").status_code == 404
    assert c.get("/skill/..%2Fsecret.txt").status_code == 404
    assert c.get("/skill/%2E%2E/pyproject.toml").status_code == 404


def test_missing_skill_file_is_404(settings, tmp_path):
    c = skill_client(settings, tmp_path / "empty")
    assert c.get("/skill/SKILL.md").status_code == 404


def test_skill_manifest_lists_version_and_hashes(settings, tmp_path):
    skill, files = make_skill_dir(tmp_path, "1.2.3")
    c = skill_client(settings, skill)
    r = c.get("/skill/manifest.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {"version": "1.2.3",
                        "files": {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}}
    for name, digest in r.json()["files"].items():
        assert hashlib.sha256(c.get(f"/skill/{name}").content).hexdigest() == digest


def test_manifest_omits_missing_files(settings, tmp_path):
    skill, _ = make_skill_dir(tmp_path)
    (skill / "sharing.cmd").unlink()
    body = skill_client(settings, skill).get("/skill/manifest.json").json()
    assert set(body["files"]) == {"SKILL.md", "tickets-SKILL.md", "sharing.py", "sharing"}


def test_manifest_without_version_is_404(settings, tmp_path):
    skill, _ = make_skill_dir(tmp_path)
    (skill / "sharing.py").write_text("print('no version here')\n")
    r = skill_client(settings, skill).get("/skill/manifest.json")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_manifest_is_computed_once_per_dir(tmp_path):
    skill, _ = make_skill_dir(tmp_path, "1.0.0")
    first = skill_manifest(skill)
    (skill / "sharing.py").write_text('VERSION = "9.9.9"\n')
    assert skill_manifest(skill) == first
