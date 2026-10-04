"""Encrypted settings (spec §15): GET/PUT /api/settings with an optimistic rev."""
import threading
from datetime import datetime, timezone

import pytest

from fileshare.db import connect
from fileshare.routes import settings as settings_routes
from fileshare.security import b64u_encode
from tests.helpers.onboard import onboard_device

T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


def _enc(sharing, mk, obj=None):
    return sharing.seal_settings(mk, {"deepgram_api_key": "dg-test-key"} if obj is None else obj)


def _put(client, enc, rev, **kw):
    return client.put("/api/settings", json={"enc_settings": enc, "rev": rev}, **kw)


def test_get_before_any_write(owner):
    r = owner.client.get("/api/settings")
    assert r.status_code == 200
    assert r.json() == {"enc_settings": None, "rev": 0, "updated_at": None}
    assert r.headers["cache-control"] == "no-store"


def test_first_put_then_get(frozen_clock, owner, sharing):
    frozen_clock(T0)
    enc = _enc(sharing, owner.mk)
    r = _put(owner.client, enc, 0)
    assert r.status_code == 200, r.text
    assert r.json() == {"rev": 1}
    assert owner.client.get("/api/settings").json() == {
        "enc_settings": enc, "rev": 1, "updated_at": "2026-09-24T12:00:00Z"}
    assert sharing.open_settings(owner.mk, owner.client.get("/api/settings").json()["enc_settings"]) == {
        "deepgram_api_key": "dg-test-key"}


def test_second_put_needs_current_rev(owner, sharing):
    assert _put(owner.client, _enc(sharing, owner.mk), 0).json() == {"rev": 1}
    enc2 = _enc(sharing, owner.mk, {})
    for stale in (0, 2, 7):
        r = _put(owner.client, enc2, stale)
        assert r.status_code == 409 and r.json()["error"] == "conflict", stale
        assert set(r.json()) == {"error", "detail"}
    assert _put(owner.client, enc2, 1).json() == {"rev": 2}
    body = owner.client.get("/api/settings").json()
    assert (body["enc_settings"], body["rev"]) == (enc2, 2)


def test_first_put_with_nonzero_rev_conflicts(owner, sharing):
    r = _put(owner.client, _enc(sharing, owner.mk), 1)
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    assert owner.client.get("/api/settings").json()["rev"] == 0


@pytest.mark.parametrize("rev", [0, 1])
def test_concurrent_puts_with_same_rev_have_one_winner(owner, sharing, rev):
    if rev:
        assert _put(owner.client, _enc(sharing, owner.mk), 0).status_code == 200
    n = 8
    barrier = threading.Barrier(n)
    results, lock = [], threading.Lock()

    def attempt(i):
        enc = _enc(sharing, owner.mk, {"deepgram_api_key": f"k{i}"})
        barrier.wait()
        r = _put(owner.client, enc, rev)
        with lock:
            results.append((r.status_code, enc))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = sorted(c for c, _ in results)
    assert codes == [200] + [409] * (n - 1), codes
    winner = next(e for c, e in results if c == 200)
    body = owner.client.get("/api/settings").json()
    assert (body["enc_settings"], body["rev"]) == (winner, rev + 1)


def test_store_is_conditional_at_the_sql_level(settings, owner):
    # two connections, same expected rev: the second must lose even without the HTTP layer
    a, b = connect(settings.db_path), connect(settings.db_path)
    try:
        env = b64u_encode(b"\x01" + bytes(40))
        assert settings_routes.store(a, env, 0) == 1
        assert settings_routes.store(b, env, 0) is None
        assert settings_routes.store(b, env, 1) == 2
        assert settings_routes.store(a, env, 1) is None
    finally:
        a.close()
        b.close()


def test_device_can_read_but_not_write(owner, sharing, device):
    enc = _enc(sharing, owner.mk)
    _put(owner.client, enc, 0)
    r = device_client_get(owner, device)
    assert r.status_code == 200 and r.json()["enc_settings"] == enc
    assert sharing.open_settings(device.mk, r.json()["enc_settings"])["deepgram_api_key"] == "dg-test-key"
    # a device token never writes, even with the right rev and a good envelope
    r = owner.client.put("/api/settings", json={"enc_settings": enc, "rev": 1}, headers=device.headers)
    assert r.status_code == 403 and r.json()["error"] == "forbidden"
    assert owner.client.get("/api/settings").json()["rev"] == 1


def device_client_get(owner, device):
    # a bearer header wins over the session cookie the owner's TestClient also carries
    return owner.client.get("/api/settings", headers=device.headers)


def test_device_put_is_403_without_cookie(client, owner, sharing, device):
    client.cookies.clear()
    r = client.put("/api/settings", json={"enc_settings": _enc(sharing, owner.mk), "rev": 0},
                   headers=device.headers)
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


def test_pending_device_gets_403_and_revoked_401(owner, sharing, device):
    pending = onboard_device(owner, sharing, "desk", "proj", approve=False)
    r = owner.client.get("/api/settings", headers=pending.headers)
    assert r.status_code == 403 and r.json()["error"] == "pending"
    assert owner.client.delete(f"/api/devices/{device.id}").status_code == 204
    r = owner.client.get("/api/settings", headers=device.headers)
    assert r.status_code == 401 and r.json()["error"] == "revoked"


def test_unauthenticated(client):
    assert client.get("/api/settings").status_code == 401
    assert client.put("/api/settings", json={"enc_settings": "x", "rev": 0}).status_code == 401


def test_put_checks_origin(owner, sharing):
    enc = _enc(sharing, owner.mk)
    for origin in ("https://evil.example", ""):
        r = _put(owner.client, enc, 0, headers={"Origin": origin})
        assert r.status_code == 403 and r.json()["error"] == "bad_origin"
    assert owner.client.get("/api/settings").json()["rev"] == 0


@pytest.mark.parametrize("body", [
    {"enc_settings": b64u_encode(b"\x02" + bytes(40)), "rev": 0},       # wrong version byte
    {"enc_settings": b64u_encode(b"\x01" + bytes(27)), "rev": 0},       # shorter than nonce+tag
    {"enc_settings": "not base64!", "rev": 0},
    {"enc_settings": "", "rev": 0},
    {"enc_settings": None, "rev": 0},
    {"enc_settings": 5, "rev": 0},
    {"rev": 0},
    {"enc_settings": b64u_encode(b"\x01" + bytes(40))},
    {"enc_settings": b64u_encode(b"\x01" + bytes(40)), "rev": -1},
    {"enc_settings": b64u_encode(b"\x01" + bytes(40)), "rev": "0"},
    {"enc_settings": b64u_encode(b"\x01" + bytes(40)), "rev": 0.0},
    {"enc_settings": b64u_encode(b"\x01" + bytes(40)), "rev": True},
    [],
])
def test_bad_body_is_400(owner, body):
    r = owner.client.put("/api/settings", json=body)
    assert r.status_code == 400, r.text
    assert set(r.json()) == {"error", "detail"}
    assert owner.client.get("/api/settings").json()["rev"] == 0


@pytest.mark.parametrize("raw", [b"{not json", b"\xff\xfe", b""])
def test_invalid_json_is_400(owner, raw):
    r = owner.client.put("/api/settings", content=raw, headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_cap_is_65536_chars(owner):
    # the largest canonical base64url string of at most 65536 chars that starts with 0x01
    raw = b"\x01" + bytes(49151)                   # 49152 bytes -> exactly 65536 chars
    enc = b64u_encode(raw)
    assert len(enc) == 65536
    assert owner.client.put("/api/settings", json={"enc_settings": enc, "rev": 0}).json() == {"rev": 1}
    over = b64u_encode(b"\x01" + bytes(49152))     # 65538 chars
    r = owner.client.put("/api/settings", json={"enc_settings": over, "rev": 1})
    assert r.status_code == 400


def test_oversized_body_is_413(owner):
    body = b'{"rev": 0, "enc_settings": "' + b"A" * 200_000 + b'"}'
    r = owner.client.put("/api/settings", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"] == "too_large"


def test_streamed_oversized_body_is_413(owner):
    def chunks():
        yield b'{"rev": 0, "enc_settings": "'
        for _ in range(10):
            yield b"A" * 20_000
        yield b'"}'

    r = owner.client.put("/api/settings", content=chunks(), headers={"Content-Type": "application/json"})
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 413 and r.json()["error"] == "too_large"
