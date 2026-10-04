from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.helpers.onboard import approve, onboard_device

T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
DEVICE_OUT_KEYS = {"id", "name", "project", "hostname", "platform", "fingerprint", "pubkey",
                   "status", "created_at", "approved_at", "last_seen_at", "revoked_at"}

# Every endpoint that accepts device auth. Task 12 extends this list with the files endpoints.
D_ENDPOINTS = [
    ("GET", "/api/whoami"),
    ("GET", "/api/devices/self/bundle"),
    ("DELETE", "/api/devices/self"),
    ("GET", "/api/files"),
    ("GET", "/api/files/FILE1"),
    ("GET", "/api/files/FILE1/blob"),
    ("POST", "/api/files"),
    ("DELETE", "/api/files/FILE1"),
    ("POST", "/api/files/FILE1/ack"),
    ("DELETE", "/api/files/FILE1/ack"),
    ("PATCH", "/api/files/FILE1"),
    ("PATCH", "/api/files/FILE1/meta"),
    ("GET", "/api/settings"),
    ("POST", "/api/files/FILE1/links"),
    ("GET", "/api/files/FILE1/links"),
    ("GET", "/api/links"),
    ("DELETE", "/api/links/lnk_000000000000"),
]
# The only D endpoints a pending device may call (§6).
PENDING_ALLOWED = {("GET", "/api/devices/self/bundle"), ("DELETE", "/api/devices/self")}


def listed(owner) -> list[dict]:
    return owner.client.get("/api/devices").json()["devices"]


def status_of(owner, device_id) -> str:
    return next(d["status"] for d in listed(owner) if d["id"] == device_id)


# --- whoami -------------------------------------------------------------------------

def test_whoami(owner, device):
    r = owner.client.get("/api/whoami", headers=device.headers)
    assert r.status_code == 200
    body = r.json()
    assert set(body["device"]) == DEVICE_OUT_KEYS
    assert body["device"]["id"] == device.id
    assert (body["device"]["name"], body["device"]["project"]) == ("laptop", "proj")
    assert body["device"]["status"] == "active"
    assert body["key_version"] == 1


def test_whoami_requires_device_token(owner):
    assert owner.client.get("/api/whoami").status_code == 401
    r = owner.client.get("/api/whoami", headers={"Authorization": "Bearer shd_nope"})
    assert r.status_code == 401
    assert r.json()["error"] == "unauthenticated"


# --- approval -----------------------------------------------------------------------

def test_pending_until_approved_then_bundle_opens(owner, sharing):
    dev = onboard_device(owner, sharing, "laptop", "proj", approve=False)
    assert dev.mk is None
    r = owner.client.get("/api/devices/self/bundle", headers=dev.headers)
    assert r.status_code == 202
    assert r.json() == {"status": "pending"}
    assert status_of(owner, dev.id) == "pending"

    approve(owner, sharing, dev.id, dev.pub)

    r = owner.client.get("/api/devices/self/bundle", headers=dev.headers)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"device_bundle", "key_version"}
    assert body["key_version"] == 1
    mk = sharing.open_device_bundle(dev.priv, dev.pub, dev.id, sharing.unb64u(body["device_bundle"]))
    assert mk == owner.mk
    d = next(d for d in listed(owner) if d["id"] == dev.id)
    assert d["status"] == "active" and d["approved_at"] is not None
    assert owner.client.get("/api/whoami", headers=dev.headers).status_code == 200


def test_bundle_sealed_to_another_device_key_cannot_be_opened(owner, sharing):
    a = onboard_device(owner, sharing, "laptop", "proj", approve=False)
    b = onboard_device(owner, sharing, "desk", "other", approve=False)
    wrong = sharing.seal_to_device(owner.mk, b.pub, a.id)   # approved the wrong fingerprint
    r = owner.client.post(f"/api/devices/{a.id}/approve", json={"device_bundle": sharing.b64u(wrong)})
    assert r.status_code == 204
    bundle = owner.client.get("/api/devices/self/bundle", headers=a.headers).json()["device_bundle"]
    with pytest.raises(sharing.IntegrityError):
        sharing.open_device_bundle(a.priv, a.pub, a.id, sharing.unb64u(bundle))


def test_bundle_is_bound_to_the_device_id(owner, sharing):
    a = onboard_device(owner, sharing, "laptop", "proj", approve=False)
    b = onboard_device(owner, sharing, "desk", "other", approve=False)
    wrong = sharing.seal_to_device(owner.mk, a.pub, b.id)   # right key, other device's id
    assert owner.client.post(f"/api/devices/{a.id}/approve",
                             json={"device_bundle": sharing.b64u(wrong)}).status_code == 204
    bundle = owner.client.get("/api/devices/self/bundle", headers=a.headers).json()["device_bundle"]
    with pytest.raises(sharing.IntegrityError):
        sharing.open_device_bundle(a.priv, a.pub, a.id, sharing.unb64u(bundle))


def test_approve_twice_is_409(owner, sharing, device):
    bundle = sharing.seal_to_device(owner.mk, device.pub, device.id)
    r = owner.client.post(f"/api/devices/{device.id}/approve", json={"device_bundle": sharing.b64u(bundle)})
    assert r.status_code == 409
    assert r.json()["error"] == "not_pending"


def test_approve_revoked_is_409(owner, sharing):
    dev = onboard_device(owner, sharing, "laptop", "proj", approve=False)
    assert owner.client.delete(f"/api/devices/{dev.id}").status_code == 204
    bundle = sharing.seal_to_device(owner.mk, dev.pub, dev.id)
    r = owner.client.post(f"/api/devices/{dev.id}/approve", json={"device_bundle": sharing.b64u(bundle)})
    assert r.status_code == 409
    assert r.json()["error"] == "not_pending"


def test_approve_unknown_is_404(owner, sharing):
    _, pub = sharing.new_device_keypair()
    bundle = sharing.seal_to_device(owner.mk, pub, "dev_000000000000")
    r = owner.client.post("/api/devices/dev_000000000000/approve",
                          json={"device_bundle": sharing.b64u(bundle)})
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_approve_requires_session(owner, sharing, app):
    dev = onboard_device(owner, sharing, "laptop", "proj", approve=False)
    bundle = sharing.b64u(sharing.seal_to_device(owner.mk, dev.pub, dev.id))
    bare = TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver"})
    assert bare.post(f"/api/devices/{dev.id}/approve", json={"device_bundle": bundle}).status_code == 401
    # a device cannot approve itself either
    r = bare.post(f"/api/devices/{dev.id}/approve", json={"device_bundle": bundle}, headers=dev.headers)
    assert r.status_code == 401
    assert status_of(owner, dev.id) == "pending"


def test_approve_checks_origin(owner, sharing):
    dev = onboard_device(owner, sharing, "laptop", "proj", approve=False)
    bundle = sharing.b64u(sharing.seal_to_device(owner.mk, dev.pub, dev.id))
    r = owner.client.post(f"/api/devices/{dev.id}/approve", json={"device_bundle": bundle},
                          headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert r.json()["error"] == "bad_origin"


@pytest.mark.parametrize("mutate", [
    lambda b: b[:-1],                              # 125 bytes
    lambda b: b + b"\x00",                         # 127 bytes
    lambda b: b"\x02" + b[1:],                     # ephemeral key not uncompressed
    lambda b: b[:1] + bytes(64) + b[65:],          # ephemeral key off the curve
    lambda b: b[:65] + b"\x02" + b[66:],           # envelope version 2
])
def test_approve_rejects_malformed_bundle(owner, sharing, mutate):
    dev = onboard_device(owner, sharing, "laptop", "proj", approve=False)
    good = sharing.seal_to_device(owner.mk, dev.pub, dev.id)
    assert len(good) == 126
    r = owner.client.post(f"/api/devices/{dev.id}/approve",
                          json={"device_bundle": sharing.b64u(mutate(good))})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"
    assert status_of(owner, dev.id) == "pending"


@pytest.mark.parametrize("body", [{}, {"device_bundle": "not base64!"}, {"device_bundle": 5}])
def test_approve_rejects_non_base64(owner, sharing, body):
    dev = onboard_device(owner, sharing, "laptop", "proj", approve=False)
    r = owner.client.post(f"/api/devices/{dev.id}/approve", json=body)
    assert r.status_code == 400
    assert status_of(owner, dev.id) == "pending"


def test_reject_pending_device(owner, sharing):
    dev = onboard_device(owner, sharing, "stranger", "proj", approve=False)
    assert owner.client.delete(f"/api/devices/{dev.id}").status_code == 204
    r = owner.client.get("/api/devices/self/bundle", headers=dev.headers)
    assert r.status_code == 401
    assert r.json()["error"] == "revoked"
    assert status_of(owner, dev.id) == "revoked"


@pytest.mark.parametrize("method,path", D_ENDPOINTS)
def test_pending_device_forbidden_everywhere_but_bundle(owner, sharing, method, path):
    dev = onboard_device(owner, sharing, "laptop", "proj", approve=False)
    r = owner.client.request(method, path, headers=dev.headers)
    if (method, path) in PENDING_ALLOWED:
        assert r.status_code in (202, 204), r.text
    else:
        assert r.status_code == 403, r.text
        assert r.json()["error"] == "pending"


# --- listing and revocation ---------------------------------------------------------

def test_list_devices(owner, sharing, device):
    second = onboard_device(owner, sharing, "desk", "other")
    devices = listed(owner)
    assert {d["id"] for d in devices} == {device.id, second.id}
    one = next(d for d in devices if d["id"] == device.id)
    assert set(one) == DEVICE_OUT_KEYS
    assert "token_hash" not in one and "device_bundle" not in one
    assert one["pubkey"] == sharing.b64u(device.pub)
    assert one["fingerprint"] == sharing.fingerprint(device.pub)


def test_list_devices_requires_session(app, device):
    assert TestClient(app, base_url="http://testserver").get("/api/devices").status_code == 401


def test_list_devices_is_an_explicit_403_for_a_device(app, device):
    """Controller ruling: the device list is browser-only; a device token gets 403 forbidden, never 401
    (which the CLI would read as "revoked")."""
    r = TestClient(app, base_url="http://testserver", headers=device.headers).get("/api/devices")
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert "devices" not in r.json()


def test_devices_listed_pending_then_active_then_revoked(owner, sharing):
    active = onboard_device(owner, sharing, "a", "p")
    revoked = onboard_device(owner, sharing, "r", "p")
    pending = onboard_device(owner, sharing, "n", "p", approve=False)
    assert owner.client.delete(f"/api/devices/{revoked.id}").status_code == 204
    assert [d["id"] for d in listed(owner)] == [pending.id, active.id, revoked.id]
    assert [d["status"] for d in listed(owner)] == ["pending", "active", "revoked"]


def test_revoke_unknown_device_is_404(owner):
    r = owner.client.delete("/api/devices/dev_000000000000")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_revoke_is_idempotent_and_keeps_first_timestamp(frozen_clock, owner, device):
    frozen_clock(T0)
    owner.client.delete(f"/api/devices/{device.id}")
    frozen_clock(T0 + timedelta(hours=1))
    assert owner.client.delete(f"/api/devices/{device.id}").status_code == 204
    assert listed(owner)[0]["revoked_at"] == "2026-09-24T12:00:00Z"


def test_self_revoke(owner, device):
    assert owner.client.delete("/api/devices/self", headers=device.headers).status_code == 204
    assert owner.client.get("/api/whoami", headers=device.headers).status_code == 401


def test_self_revoke_while_pending(owner, sharing):
    dev = onboard_device(owner, sharing, "laptop", "proj", approve=False)
    assert owner.client.delete("/api/devices/self", headers=dev.headers).status_code == 204
    assert owner.client.get("/api/devices/self/bundle", headers=dev.headers).status_code == 401


def test_self_routes_are_not_shadowed_by_id_routes(owner, device):
    # A session calling /self must not be treated as "the device with id 'self'".
    assert owner.client.delete("/api/devices/self").status_code == 401
    assert owner.client.get("/api/devices/self/bundle").status_code == 401


def test_last_seen_is_throttled_to_once_a_minute(frozen_clock, owner, sharing):
    frozen_clock(T0)
    dev = onboard_device(owner, sharing, "laptop", "proj")
    owner.client.get("/api/whoami", headers=dev.headers)

    def last_seen():
        return listed(owner)[0]["last_seen_at"]

    assert last_seen() == "2026-09-24T12:00:00Z"
    frozen_clock(T0 + timedelta(seconds=30))
    owner.client.get("/api/whoami", headers=dev.headers)
    assert last_seen() == "2026-09-24T12:00:00Z"
    frozen_clock(T0 + timedelta(seconds=61))
    owner.client.get("/api/whoami", headers=dev.headers)
    assert last_seen() == "2026-09-24T12:01:01Z"


@pytest.mark.parametrize("method,path", D_ENDPOINTS)
def test_revoked_device_gets_401_everywhere(owner, device, method, path):
    assert owner.client.delete(f"/api/devices/{device.id}").status_code == 204
    r = owner.client.request(method, path, headers=device.headers)
    assert r.status_code == 401
    assert r.json()["error"] == "revoked"
