from datetime import timedelta

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fileshare import clock
from fileshare.db import connect
from fileshare.deps import (Principal, api_error, client_ip, require_any, require_device,
                            require_device_any_state, require_session)
from fileshare.security import sha256_hex
from fileshare.sessions import create_session

TOKEN = "shd_" + "A" * 43


class Body(BaseModel):
    n: int


@pytest.fixture
def probe(app):
    @app.get("/_probe/session")
    def s(p: Principal = Depends(require_session)):
        return {"kind": p.kind}

    @app.post("/_probe/session")
    def sp(p: Principal = Depends(require_session)):
        return {"kind": p.kind}

    @app.get("/_probe/device")
    def d(p: Principal = Depends(require_device)):
        return {"kind": p.kind, "device": p.device["id"]}

    @app.get("/_probe/device-any-state")
    def das(p: Principal = Depends(require_device_any_state)):
        return {"kind": p.kind, "device": p.device["id"]}

    @app.post("/_probe/any")
    def a(p: Principal = Depends(require_any)):
        return {"kind": p.kind}

    @app.get("/_probe/boom")
    def boom():
        raise api_error(418, "teapot", "short and stout")

    @app.post("/_probe/validate")
    def validate(body: Body):
        return {"n": body.n}

    from fastapi import Request

    @app.get("/_probe/ip")
    def ip(request: Request):
        return {"ip": client_ip(request)}

    return app


def _session(settings, client):
    token = create_session(connect(settings.db_path))
    client.cookies.set("fs_session", token)
    return token


def _device(settings, revoked=False, approved=True):
    """Insert one device row directly. Default: an approved, active device."""
    conn = connect(settings.db_path)
    now = clock.now_iso()
    conn.execute(
        "INSERT INTO devices(id,name,project,hostname,token_hash,pubkey,fingerprint,platform,"
        "created_at,approved_at,device_bundle,revoked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("dev_000000000001", "macbook-pro", "orchestrator", "mbp.local", sha256_hex(TOKEN),
         "BA", "ABCD-EFGH", "darwin", now,
         now if approved else None, "bundle" if approved else None,
         now if revoked else None))
    return conn


def test_api_error_shape(probe, client):
    r = client.get("/_probe/boom")
    assert r.status_code == 418
    assert r.json() == {"error": "teapot", "detail": "short and stout"}


def test_validation_error_is_400_bad_request(probe, client):
    r = client.post("/_probe/validate", json={"n": "not a number"})
    assert r.status_code == 400
    assert r.json() == {"error": "bad_request", "detail": "invalid request"}


def test_unknown_route_uses_error_shape(client):
    r = client.get("/nope")
    assert r.status_code == 404
    assert r.json()["error"] == "http_404"


def test_session_required(probe, client):
    r = client.get("/_probe/session")
    assert r.status_code == 401
    assert r.json() == {"error": "unauthenticated", "detail": ""}


def test_session_ok(probe, client, settings):
    _session(settings, client)
    assert client.get("/_probe/session").json() == {"kind": "session"}


def test_post_with_wrong_origin_is_403_even_with_session(probe, client, settings):
    _session(settings, client)
    r = client.post("/_probe/session", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert r.json()["error"] == "bad_origin"


def test_post_without_origin_is_403(probe, app, settings):
    with TestClient(app, base_url="http://testserver") as bare:
        bare.cookies.set("fs_session", create_session(connect(settings.db_path)))
        r = bare.post("/_probe/session")
    assert r.status_code == 403 and r.json()["error"] == "bad_origin"


def test_post_with_matching_origin_ok(probe, client, settings):
    _session(settings, client)
    assert client.post("/_probe/session").json() == {"kind": "session"}


def test_device_ok_and_last_seen_throttled(probe, client, settings, frozen_clock):
    conn = _device(settings)
    t0 = clock.now()
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/_probe/device", headers=auth).json() == {"kind": "device", "device": "dev_000000000001"}
    seen = lambda: conn.execute("SELECT last_seen_at FROM devices").fetchone()[0]
    assert seen() == clock.now_iso(t0)
    frozen_clock(t0 + timedelta(seconds=30))
    client.get("/_probe/device", headers=auth)
    assert seen() == clock.now_iso(t0)
    frozen_clock(t0 + timedelta(seconds=61))
    client.get("/_probe/device", headers=auth)
    assert seen() == clock.now_iso(t0 + timedelta(seconds=61))


@pytest.mark.parametrize("header", [
    None, "Bearer", "Bearer ", "Basic abc", "Bearer shd_wrong", f"bearer {TOKEN}x", TOKEN,
])
def test_device_bad_tokens_401(probe, client, settings, header):
    _device(settings)
    headers = {} if header is None else {"Authorization": header}
    r = client.get("/_probe/device", headers=headers)
    assert r.status_code == 401 and r.json()["error"] == "unauthenticated"


def test_device_bearer_scheme_case_insensitive(probe, client, settings):
    _device(settings)
    assert client.get("/_probe/device", headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200


def test_revoked_device_401(probe, client, settings):
    _device(settings, revoked=True)
    r = client.get("/_probe/device", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 401 and r.json()["error"] == "revoked"


AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.parametrize("state,path,status,code", [
    ("active", "/_probe/device", 200, None),
    ("active", "/_probe/device-any-state", 200, None),
    ("pending", "/_probe/device", 403, "pending"),
    ("pending", "/_probe/device-any-state", 200, None),
    ("revoked", "/_probe/device", 401, "revoked"),
    ("revoked", "/_probe/device-any-state", 401, "revoked"),
    ("rejected", "/_probe/device", 401, "revoked"),            # reject == revoke of a pending device
    ("rejected", "/_probe/device-any-state", 401, "revoked"),
])
def test_device_states_on_both_deps(probe, client, settings, state, path, status, code):
    _device(settings,
            approved=state in ("active", "revoked"),
            revoked=state in ("revoked", "rejected"))
    r = client.get(path, headers=AUTH)
    assert r.status_code == status
    if code is None:
        assert r.json() == {"kind": "device", "device": "dev_000000000001"}
    else:
        assert r.json()["error"] == code


def test_pending_device_forbidden_via_require_any(probe, client, settings):
    _device(settings, approved=False)
    r = client.post("/_probe/any", headers=AUTH)
    assert r.status_code == 403 and r.json()["error"] == "pending"


def test_pending_device_still_updates_last_seen(probe, client, settings):
    conn = _device(settings, approved=False)
    client.get("/_probe/device-any-state", headers=AUTH)
    assert conn.execute("SELECT last_seen_at FROM devices").fetchone()[0] == clock.now_iso()


def test_require_any_prefers_bearer(probe, client, settings):
    _device(settings)
    _session(settings, client)
    assert client.post("/_probe/any", headers={"Authorization": f"Bearer {TOKEN}"}).json() == {"kind": "device"}
    assert client.post("/_probe/any").json() == {"kind": "session"}


def test_require_any_bad_bearer_does_not_fall_back_to_session(probe, client, settings):
    _session(settings, client)
    r = client.post("/_probe/any", headers={"Authorization": "Bearer shd_nope"})
    assert r.status_code == 401


def test_device_post_needs_no_origin(probe, app, settings):
    _device(settings)
    with TestClient(app, base_url="http://testserver") as bare:
        r = bare.post("/_probe/any", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_client_ip_prefers_x_real_ip(probe, client):
    assert client.get("/_probe/ip", headers={"X-Real-IP": "203.0.113.9"}).json() == {"ip": "203.0.113.9"}
    assert client.get("/_probe/ip", headers={"X-Real-IP": "2001:db8::1"}).json() == {"ip": "2001:db8::1"}
    # TestClient's peer is the non-IP "testclient", so with no usable header the result is "unknown".
    assert client.get("/_probe/ip").json() == {"ip": "unknown"}


def _raw_request(headers: dict[str, str], peer: str | None):
    from starlette.requests import Request
    scope = {
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
        "client": None if peer is None else (peer, 12345),
    }
    return Request(scope)


def test_client_ip_rejects_crlf_injection_in_header():
    evil = "1.2.3.4\r\nauth: failed attempt 1/10 from 6.6.6.6"
    assert client_ip(_raw_request({"X-Real-IP": evil}, "10.0.0.5")) == "10.0.0.5"
    assert client_ip(_raw_request({"X-Real-IP": evil}, None)) == "unknown"


@pytest.mark.parametrize("header,peer,want", [
    ("198.51.100.7", "10.0.0.5", "198.51.100.7"),
    (" 198.51.100.7 ", "10.0.0.5", "10.0.0.5"),
    ("junk", "10.0.0.5", "10.0.0.5"),
    ("", "10.0.0.5", "10.0.0.5"),
    (None, "10.0.0.5", "10.0.0.5"),
    (None, "testclient", "unknown"),
    ("junk", "junk\r\nx", "unknown"),
    ("fe80::1%eth0", "10.0.0.5", "10.0.0.5"),
    ("fe80::1%a\r\nauth: failed attempt from 6.6.6.6", "10.0.0.5", "10.0.0.5"),
    (None, "fe80::1%a\r\nx", "unknown"),
    (None, None, "unknown"),
])
def test_client_ip_only_returns_valid_ips(header, peer, want):
    headers = {} if header is None else {"X-Real-IP": header}
    assert client_ip(_raw_request(headers, peer)) == want
