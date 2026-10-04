import logging
import secrets
import threading
import time
from datetime import timedelta

import pytest

from fileshare import clock
from fileshare.admin import create_setup_code
from fileshare.db import connect, get_meta
from fileshare.routes import auth as auth_routes
from fileshare.security import b64u_encode

AUTH_KEY = bytes(range(32))


def _code(settings):
    return create_setup_code(connect(settings.db_path))


def _body(code, auth_key=AUTH_KEY, **over):
    body = {
        "setup_code": code,
        "kdf_salt": b64u_encode(secrets.token_bytes(16)),
        "kdf_iterations": 1000,
        "auth_key": b64u_encode(auth_key),
        "wrapped_mk": b64u_encode(b"\x01" + secrets.token_bytes(60)),
    }
    body.update(over)
    return body


def _setup(client, settings, **over):
    body = _body(_code(settings), **over)
    r = client.post("/api/setup", json=body)
    assert r.status_code == 204, r.text
    return body


REFUSED = {"error": "refused", "detail": ""}


def test_status_and_kdf_before_setup(client):
    assert client.get("/api/setup/status").json() == {"initialized": False}
    r = client.get("/api/kdf")
    assert r.status_code == 409 and r.json()["error"] == "not_initialized"


def test_setup_without_any_code_refused(client):
    r = client.post("/api/setup", json=_body("setup_" + "x" * 32))
    assert r.status_code == 403 and r.json() == REFUSED


def test_setup_success_logs_in_and_stores_params(client, settings):
    body = _setup(client, settings)
    assert client.get("/api/setup/status").json() == {"initialized": True}
    assert client.get("/api/kdf").json() == {"kdf_salt": body["kdf_salt"], "kdf_iterations": 1000}
    assert client.get("/api/keyblob").json() == {"wrapped_mk": body["wrapped_mk"], "key_version": 1}
    conn = connect(settings.db_path)
    assert get_meta(conn, "auth_hash").startswith("scrypt.")
    assert get_meta(conn, "setup_code_hash") is None


def test_setup_code_single_use(client, settings):
    code = _code(settings)
    assert client.post("/api/setup", json=_body(code)).status_code == 204
    r = client.post("/api/setup", json=_body(code))
    assert r.status_code == 403 and r.json() == REFUSED


def test_setup_code_expires(client, settings, frozen_clock):
    code = _code(settings)
    frozen_clock(clock.now() + timedelta(minutes=15, seconds=1))
    r = client.post("/api/setup", json=_body(code))
    assert r.status_code == 403 and r.json() == REFUSED


@pytest.mark.parametrize("over", [
    {"kdf_iterations": 999},
    {"kdf_iterations": 10_000_001},
    {"kdf_salt": b64u_encode(b"short")},
    {"auth_key": b"x" * 31},
    {"wrapped_mk": b64u_encode(b"\x01" + b"x" * 59)},
    {"wrapped_mk": "not base64!"},
])
def test_setup_bad_params_refused_and_code_not_spent(client, settings, over):
    code = _code(settings)
    r = client.post("/api/setup", json=_body(code, **over))
    assert r.status_code == 403 and r.json() == REFUSED
    assert client.post("/api/setup", json=_body(code)).status_code == 204


def test_setup_missing_field_is_bad_request(client, settings):
    body = _body(_code(settings))
    del body["wrapped_mk"]
    assert client.post("/api/setup", json=body).status_code == 400


def test_restore_with_fresh_code_replaces_key_and_kills_sessions(client, settings):
    _setup(client, settings)
    old_cookie = client.cookies.get("fs_session")
    conn = connect(settings.db_path)
    conn.execute("UPDATE meta SET value='4' WHERE key='key_version'")
    new = _setup(client, settings, auth_key=b"\x07" * 32, mode="restore")
    assert client.get("/api/keyblob").json() == {"wrapped_mk": new["wrapped_mk"], "key_version": 4}
    client.cookies.clear()  # avoid two same-named cookies in the jar
    client.cookies.set("fs_session", old_cookie)
    assert client.get("/api/keyblob").status_code == 401


@pytest.mark.parametrize("mode", [None, "new"])
def test_new_setup_after_init_refused_and_code_not_spent(client, settings, mode):
    first = _setup(client, settings)
    code = _code(settings)
    over = {} if mode is None else {"mode": mode}
    r = client.post("/api/setup", json=_body(code, auth_key=b"\x07" * 32, **over))
    assert r.status_code == 403 and r.json() == REFUSED
    assert client.get("/api/keyblob").json()["wrapped_mk"] == first["wrapped_mk"]
    assert get_meta(connect(settings.db_path), "setup_code_hash") is not None
    restored = _body(code, auth_key=b"\x07" * 32, mode="restore")
    assert client.post("/api/setup", json=restored).status_code == 204
    assert client.get("/api/keyblob").json()["wrapped_mk"] == restored["wrapped_mk"]


def test_new_setup_after_init_counts_toward_rate_limit(client, settings):
    _setup(client, settings)
    ip = {"X-Real-IP": "203.0.113.11"}
    for _ in range(10):
        assert client.post("/api/setup", json=_body(_code(settings)), headers=ip).status_code == 403
    r = client.post("/api/setup", json=_body(_code(settings), mode="restore"), headers=ip)
    assert r.status_code == 429


@pytest.mark.parametrize("mode", ["new", "restore"])
def test_first_setup_accepts_either_mode(client, settings, mode):
    _setup(client, settings, mode=mode)
    assert client.get("/api/setup/status").json() == {"initialized": True}


def test_setup_unknown_mode_is_bad_request(client, settings):
    assert client.post("/api/setup", json=_body(_code(settings), mode="fork")).status_code == 400


def test_login_logout_cycle(client, settings):
    _setup(client, settings)
    client.cookies.clear()
    assert client.get("/api/keyblob").status_code == 401
    r = client.post("/api/login", json={"auth_key": b64u_encode(AUTH_KEY)})
    assert r.status_code == 204
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert client.get("/api/keyblob").status_code == 200
    token = client.cookies.get("fs_session")
    assert client.post("/api/logout").status_code == 204
    client.cookies.clear()
    client.cookies.set("fs_session", token)  # replay the old cookie: must be dead server-side
    assert client.get("/api/keyblob").status_code == 401


@pytest.mark.parametrize("auth_key", [b64u_encode(b"\x00" * 32), b64u_encode(b"x" * 16), "!!", ""])
def test_login_wrong_or_malformed_is_uniform_refused(client, settings, auth_key):
    _setup(client, settings)
    r = client.post("/api/login", json={"auth_key": auth_key})
    assert r.status_code == 401 and r.json() == REFUSED


def test_login_before_setup_refused(client):
    r = client.post("/api/login", json={"auth_key": b64u_encode(AUTH_KEY)})
    assert r.status_code == 401 and r.json() == REFUSED


def test_login_rate_limited_after_10_failures_and_logged(client, settings, caplog):
    _setup(client, settings)
    ip = {"X-Real-IP": "203.0.113.9"}
    with caplog.at_level(logging.WARNING, logger="fileshare.auth"):
        for _ in range(10):
            assert client.post("/api/login", json={"auth_key": b64u_encode(b"\x00" * 32)}, headers=ip).status_code == 401
    r = client.post("/api/login", json={"auth_key": b64u_encode(AUTH_KEY)}, headers=ip)
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    assert "auth: failed attempt 10/10 from 203.0.113.9" in [x.getMessage() for x in caplog.records]
    other = client.post("/api/login", json={"auth_key": b64u_encode(AUTH_KEY)}, headers={"X-Real-IP": "198.51.100.1"})
    assert other.status_code == 204


def test_successful_login_resets_counter(client, settings):
    _setup(client, settings)
    ip = {"X-Real-IP": "203.0.113.9"}
    for _ in range(9):
        client.post("/api/login", json={"auth_key": b64u_encode(b"\x00" * 32)}, headers=ip)
    assert client.post("/api/login", json={"auth_key": b64u_encode(AUTH_KEY)}, headers=ip).status_code == 204
    for _ in range(9):
        assert client.post("/api/login", json={"auth_key": b64u_encode(b"\x00" * 32)}, headers=ip).status_code == 401


def test_setup_failures_count_toward_rate_limit(client, settings):
    ip = {"X-Real-IP": "203.0.113.10"}
    for _ in range(10):
        client.post("/api/setup", json=_body("setup_" + "y" * 32), headers=ip)
    r = client.post("/api/setup", json=_body(_code(settings)), headers=ip)
    assert r.status_code == 429


def test_setup_body_is_bounded_before_parsing(client, settings):
    # Unauthenticated memory-DoS guard: the body is capped before FastAPI buffers/parses it.
    big = _body(_code(settings), name="z" * 9000)
    r = client.post("/api/setup", json=big)
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    # A normal body still works, and the code was not spent by the oversize attempt.
    assert client.post("/api/setup", json=_body(_code(settings))).status_code == 204


def test_login_body_is_bounded_before_parsing(client, settings):
    _setup(client, settings)
    client.cookies.clear()
    r = client.post("/api/login", json={"auth_key": b64u_encode(AUTH_KEY), "name": "z" * 9000})
    assert r.status_code == 413 and r.json()["error"] == "too_large"
    # A normal login still works after the oversize attempt.
    assert client.post("/api/login", json={"auth_key": b64u_encode(AUTH_KEY)}).status_code == 204


def test_login_oversize_body_does_not_consume_rate_limit(client, settings):
    _setup(client, settings)
    client.cookies.clear()
    ip = {"X-Real-IP": "203.0.113.77"}
    # 20 oversize bodies (far past the limit of 10) must not lock the IP out: they 413 before
    # any auth work, so a genuine login still succeeds afterwards, and wrong keys still rate-limit.
    for _ in range(20):
        r = client.post("/api/login", json={"auth_key": b64u_encode(AUTH_KEY), "name": "z" * 9000}, headers=ip)
        assert r.status_code == 413
    for _ in range(10):
        assert client.post("/api/login", json={"auth_key": b64u_encode(b"\x00" * 32)}, headers=ip).status_code == 401
    assert client.post("/api/login", json={"auth_key": b64u_encode(AUTH_KEY)}, headers=ip).status_code == 429


def test_login_and_setup_require_origin(client, settings):
    evil = {"Origin": "https://evil.example"}
    assert client.post("/api/setup", json=_body(_code(settings)), headers=evil).status_code == 403
    _setup(client, settings)
    r = client.post("/api/login", json={"auth_key": b64u_encode(AUTH_KEY)}, headers=evil)
    assert r.status_code == 403 and r.json()["error"] == "bad_origin"


def test_logout_requires_session_and_origin(client, settings):
    assert client.post("/api/logout").status_code == 401
    _setup(client, settings)
    assert client.post("/api/logout", headers={"Origin": "https://evil.example"}).status_code == 403


def test_session_idle_expiry_via_api(client, settings, frozen_clock):
    _setup(client, settings)
    t0 = clock.now()
    frozen_clock(t0 + timedelta(days=29))
    assert client.get("/api/keyblob").status_code == 200
    frozen_clock(t0 + timedelta(days=29 + 31))
    assert client.get("/api/keyblob").status_code == 401


THIRTY_DAYS = 30 * 24 * 3600


def _session_cookies(r) -> list[str]:
    return [v for k, v in r.headers.multi_items() if k == "set-cookie" and v.startswith("fs_session=")]


def test_login_cookie_lasts_thirty_days(client, settings):
    _setup(client, settings)
    r = client.post("/api/login", json={"auth_key": b64u_encode(AUTH_KEY)})
    [cookie] = _session_cookies(r)
    assert f"max-age={THIRTY_DAYS}" in cookie.lower()


def test_cookie_is_refreshed_when_renewed_after_an_hour(client, settings, frozen_clock):
    _setup(client, settings)
    t0 = clock.now()
    token = client.cookies.get("fs_session")
    frozen_clock(t0 + timedelta(minutes=30))
    assert _session_cookies(client.get("/api/keyblob")) == []
    frozen_clock(t0 + timedelta(minutes=61))
    r = client.get("/api/keyblob")
    assert r.status_code == 200
    [cookie] = _session_cookies(r)
    assert cookie.startswith(f"fs_session={token};")
    assert f"max-age={THIRTY_DAYS}" in cookie.lower()
    assert "httponly" in cookie.lower() and "samesite=strict" in cookie.lower()
    frozen_clock(t0 + timedelta(minutes=90))
    assert _session_cookies(client.get("/api/keyblob")) == []   # refreshed at most once an hour


def test_cookie_refresh_does_not_undo_logout(client, settings, frozen_clock):
    _setup(client, settings)
    frozen_clock(clock.now() + timedelta(hours=2))
    r = client.post("/api/logout")
    assert r.status_code == 204
    [cookie] = _session_cookies(r)
    assert "max-age=0" in cookie.lower()


def test_revoke_all_kills_every_session(client, settings, app):
    from fastapi.testclient import TestClient
    _setup(client, settings)
    with TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver"}) as other:
        assert other.post("/api/login", json={"auth_key": b64u_encode(AUTH_KEY)}).status_code == 204
        assert other.get("/api/keyblob").status_code == 200
        r = client.post("/api/sessions/revoke-all")
        assert r.status_code == 204
        [cookie] = _session_cookies(r)
        assert "max-age=0" in cookie.lower()
        assert other.get("/api/keyblob").status_code == 401
    token_count = connect(settings.db_path).execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert token_count == 0
    client.cookies.clear()
    assert client.get("/api/keyblob").status_code == 401


def test_revoke_all_requires_session_and_origin(client, settings):
    assert client.post("/api/sessions/revoke-all").status_code == 401
    _setup(client, settings)
    r = client.post("/api/sessions/revoke-all", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"] == "bad_origin"
    assert client.get("/api/keyblob").status_code == 200


# --- B: session names -----------------------------------------------------------------

def _login(client, **extra):
    return client.post("/api/login", json={"auth_key": b64u_encode(AUTH_KEY), **extra})


def test_login_with_name_shows_in_sessions_self(client, settings):
    _setup(client, settings)
    assert client.get("/api/sessions/self").json() == {"name": ""}   # setup without a name
    assert _login(client, name="  iPhone · Safari  ").status_code == 204
    assert client.get("/api/sessions/self").json() == {"name": "iPhone · Safari"}


def test_setup_with_name(client, settings):
    _setup(client, settings, name="MacBook · Chrome")
    assert client.get("/api/sessions/self").json() == {"name": "MacBook · Chrome"}


@pytest.mark.parametrize("name", ["", "   ", "x" * 41, "a\nb", "tab\there", "nul\x00", 5, None, ["a"]])
def test_login_with_invalid_name_still_logs_in_unnamed(client, settings, name):
    _setup(client, settings)
    assert _login(client, name=name).status_code == 204
    assert client.get("/api/sessions/self").json() == {"name": ""}


def test_login_name_does_not_change_refusal(client, settings):
    _setup(client, settings)
    r = client.post("/api/login", json={"auth_key": b64u_encode(b"\x00" * 32), "name": "x"})
    assert r.status_code == 401 and r.json() == REFUSED


def test_rename_session(client, settings):
    _setup(client, settings)
    r = client.patch("/api/sessions/self", json={"name": " Windows PC · Edge "})
    assert r.status_code == 204
    assert client.get("/api/sessions/self").json() == {"name": "Windows PC · Edge"}
    assert client.patch("/api/sessions/self", json={"name": "x" * 40}).status_code == 204


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "  "}, {"name": "x" * 41},
                                  {"name": "a\u0007b"}, {"name": 3}, {"name": None}])
def test_rename_rejects_invalid_names(client, settings, body):
    _setup(client, settings, name="keep")
    r = client.patch("/api/sessions/self", json=body)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert client.get("/api/sessions/self").json() == {"name": "keep"}


def test_sessions_self_requires_session_and_origin(client, settings):
    assert client.get("/api/sessions/self").status_code == 401
    assert client.patch("/api/sessions/self", json={"name": "x"}).status_code == 401
    _setup(client, settings)
    r = client.patch("/api/sessions/self", json={"name": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_concurrent_login_burst_cannot_exceed_limit(client, settings, caplog, monkeypatch):
    _setup(client, settings)

    def slow_verify(secret, stored):
        time.sleep(0.2)
        return False

    monkeypatch.setattr(auth_routes, "verify_secret", slow_verify)
    n = 25
    barrier = threading.Barrier(n)
    statuses = []
    lock = threading.Lock()
    ip = {"X-Real-IP": "203.0.113.77"}

    def attempt():
        barrier.wait()
        r = client.post("/api/login", json={"auth_key": b64u_encode(b"\x00" * 32)}, headers=ip)
        with lock:
            statuses.append(r.status_code)

    with caplog.at_level(logging.WARNING, logger="fileshare.auth"):
        threads = [threading.Thread(target=attempt) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(statuses) == n
    assert statuses.count(401) <= 10
    assert statuses.count(401) + statuses.count(429) == n
    lines = [m for m in (x.getMessage() for x in caplog.records)
             if m.startswith("auth: failed attempt ") and m.endswith(" from 203.0.113.77")]
    assert len(lines) <= 10


def test_api_responses_not_cached(client):
    assert client.get("/api/setup/status").headers["cache-control"] == "no-store"
