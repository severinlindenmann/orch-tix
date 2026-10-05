"""Public links (spec §17): owner endpoints, the public meta/blob routes, and the viewer page."""
import hashlib
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fileshare import expiry
from fileshare.db import connect
from fileshare.routes import links as links_routes
from fileshare.security import b64u_encode
from tests.helpers.blobs import make_fake_blob
from tests.helpers.files import fake_meta, upload
from tests.helpers.onboard import onboard_device

T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
CREATE_KEYS = {"id", "token", "expires_at", "max_downloads"}
PUBLIC_KEYS = {"uuid", "key_version", "wrapped_dek_link", "enc_meta", "size", "created_at", "expires_at", "downloads_left"}
LINK_OUT_KEYS = {"id", "file", "created_at", "created_by", "expires_at", "downloads", "max_downloads"}
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
NOT_FOUND = {"error": "not_found", "detail": "this link has expired or was revoked"}


def _wdl() -> str:
    return b64u_encode(b"\x01" + os.urandom(60))     # 61 bytes = a sealed 32-byte DEK


def _up(client, headers=None, ttl=None, blob=None):
    u = os.urandom(16).hex()
    meta = fake_meta(u)
    if ttl is not None:
        meta["ttl"] = ttl
    r = upload(client, headers, u, meta=meta, blob=blob if blob is not None else make_fake_blob(u))
    assert r.status_code == 201, r.text
    return r.json()


def _link(client, ref="FILE1", headers=None, **body):
    body = {"wrapped_dek_link": _wdl(), "ttl": "7d"} | body
    return client.post(f"/api/files/{ref}/links", json=body, headers=headers or {})


def _mk(client, ref="FILE1", headers=None, **body) -> dict:
    r = _link(client, ref, headers, **body)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def bare(app):
    """A client with no cookie, no Origin and no bearer: a stranger holding a link."""
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def _db(settings):
    return connect(settings.db_path)


# --- create ----------------------------------------------------------------------------

def test_create_as_session(frozen_clock, owner, settings):
    frozen_clock(T0)
    _up(owner.client, ttl="30d")
    r = _link(owner.client, max_downloads=3)
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == CREATE_KEYS
    assert TOKEN_RE.fullmatch(body["token"])
    assert body["id"].startswith("lnk_")
    assert body["expires_at"] == "2026-10-01T12:00:00Z"
    assert body["max_downloads"] == 3
    row = _db(settings).execute("SELECT * FROM links").fetchone()
    assert (row["file_n"], row["created_by_device"], row["created_by_session"]) == (1, None, "browser")
    assert (row["downloads"], row["revoked_at"], row["max_downloads"]) == (0, None, 3)
    assert row["created_at"] == "2026-09-24T12:00:00Z"


def test_token_is_stored_only_as_its_sha256(owner, settings):
    _up(owner.client)
    body = _mk(owner.client)
    conn = _db(settings)
    row = conn.execute("SELECT * FROM links").fetchone()
    assert row["token_hash"] == hashlib.sha256(body["token"].encode()).hexdigest()
    # the raw token is nowhere in the database file or its WAL
    conn.execute("PRAGMA wal_checkpoint(FULL)")
    for p in settings.data_dir.rglob("fileshare.db*"):
        assert body["token"].encode() not in p.read_bytes(), p


def test_tokens_are_unique_per_link(owner):
    _up(owner.client)
    tokens = {_mk(owner.client)["token"] for _ in range(5)}
    assert len(tokens) == 5


def test_create_as_active_device_records_the_device(owner, sharing, settings, device):
    _up(owner.client)
    _mk(owner.client, headers=device.headers)
    row = _db(settings).execute("SELECT * FROM links").fetchone()
    assert (row["created_by_device"], row["created_by_session"]) == (device.id, None)
    listed = owner.client.get("/api/files/FILE1/links").json()["links"]
    assert listed[0]["created_by"] == {"id": device.id, "name": "laptop"}


def test_create_records_the_session_name(owner):
    owner.client.patch("/api/sessions/self", json={"name": "iPhone · Safari"})
    _up(owner.client)
    _mk(owner.client)
    assert owner.client.get("/api/files/FILE1/links").json()["links"][0]["created_by"] == {
        "id": None, "name": "iPhone · Safari"}


# --- auth matrix -----------------------------------------------------------------------

OWNER_ROUTES = [
    ("POST", "/api/files/FILE1/links"),
    ("GET", "/api/files/FILE1/links"),
    ("GET", "/api/links"),
    ("DELETE", "/api/links/lnk_000000000000"),
]


@pytest.mark.parametrize("method,path", OWNER_ROUTES)
def test_owner_routes_need_auth(owner, bare, method, path):
    _up(owner.client)
    r = bare.request(method, path, json={"wrapped_dek_link": _wdl(), "ttl": "7d"},
                     headers={"Origin": "http://testserver"})
    assert r.status_code == 401 and r.json()["error"] == "unauthenticated"


@pytest.mark.parametrize("method,path", [m for m in OWNER_ROUTES if m[0] != "GET"])
@pytest.mark.parametrize("origin", [None, "https://evil.example", ""])
def test_session_writes_check_origin(owner, app, method, path, origin):
    _up(owner.client)
    c = TestClient(app, base_url="http://testserver")
    c.cookies = owner.client.cookies
    headers = {} if origin is None else {"Origin": origin}
    r = c.request(method, path, json={"wrapped_dek_link": _wdl(), "ttl": "7d"}, headers=headers)
    assert r.status_code == 403 and r.json()["error"] in ("bad_origin", "origin_required")


@pytest.mark.parametrize("method,path", OWNER_ROUTES)
def test_pending_device_is_403(owner, sharing, method, path):
    _up(owner.client)
    dev = onboard_device(owner, sharing, "p", "proj", approve=False)
    r = owner.client.request(method, path, headers=dev.headers, json={"wrapped_dek_link": _wdl(), "ttl": "7d"})
    assert r.status_code == 403 and r.json()["error"] == "pending"


@pytest.mark.parametrize("method,path", OWNER_ROUTES)
def test_revoked_device_is_401(owner, device, method, path):
    _up(owner.client)
    assert owner.client.delete(f"/api/devices/{device.id}").status_code == 204
    r = owner.client.request(method, path, headers=device.headers, json={"wrapped_dek_link": _wdl(), "ttl": "7d"})
    assert r.status_code == 401 and r.json()["error"] == "revoked"


def test_any_active_device_can_link_list_and_revoke(owner, sharing, device):
    other = onboard_device(owner, sharing, "desk", "p2")
    _up(owner.client, device.headers)
    made = _mk(owner.client, headers=other.headers)
    assert [x["id"] for x in owner.client.get("/api/files/FILE1/links", headers=device.headers).json()["links"]] \
        == [made["id"]]
    assert owner.client.delete(f"/api/links/{made['id']}", headers=device.headers).status_code == 204


# --- validation ------------------------------------------------------------------------

@pytest.mark.parametrize("ttl,delta", [("1h", timedelta(hours=1)), ("1d", timedelta(days=1)),
                                       ("7d", timedelta(days=7)), ("30d", timedelta(days=30))])
def test_each_ttl(frozen_clock, owner, ttl, delta):
    frozen_clock(T0)
    _up(owner.client, ttl="never")
    body = _mk(owner.client, ttl=ttl)
    assert body["expires_at"] == (T0 + delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert body["max_downloads"] is None


@pytest.mark.parametrize("ttl", ["never", "2d", "", None, 7, "1H", " 1d", ["1d"]])
def test_bad_ttl_is_400(owner, ttl):
    _up(owner.client)
    r = _link(owner.client, ttl=ttl)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_missing_ttl_is_400(owner):
    _up(owner.client)
    r = owner.client.post("/api/files/FILE1/links", json={"wrapped_dek_link": _wdl()})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_link_expiry_is_capped_at_the_files(frozen_clock, owner):
    frozen_clock(T0)
    _up(owner.client, ttl="1d")
    assert _mk(owner.client, ttl="7d")["expires_at"] == "2026-09-25T12:00:00Z"
    assert _mk(owner.client, ttl="30d")["expires_at"] == "2026-09-25T12:00:00Z"
    assert _mk(owner.client, ttl="1h")["expires_at"] == "2026-09-24T13:00:00Z"
    # a later file-ttl change doesn't move existing links, but new ones follow the new cap
    frozen_clock(T0 + timedelta(hours=2))
    assert owner.client.patch("/api/files/FILE1", json={"ttl": "7d"}).status_code == 204
    assert _mk(owner.client, ttl="30d")["expires_at"] == "2026-10-01T14:00:00Z"


@pytest.mark.parametrize("m", [1, 2, 1000])
def test_max_downloads_valid(owner, m):
    _up(owner.client)
    assert _mk(owner.client, max_downloads=m)["max_downloads"] == m


def test_max_downloads_null_means_unlimited(owner):
    _up(owner.client)
    assert _mk(owner.client, max_downloads=None)["max_downloads"] is None


@pytest.mark.parametrize("m", [0, -1, 1001, "5", 1.0, 2.5, True, False, [1], {}])
def test_bad_max_downloads_is_400(owner, m):
    _up(owner.client)
    r = _link(owner.client, max_downloads=m)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


@pytest.mark.parametrize("w", [
    None, 5, "", "!!!", "A" * 5000,
    b64u_encode(b"\x01" + os.urandom(59)),          # 60 bytes: too short for a sealed DEK
    b64u_encode(b"\x01" + os.urandom(61)),          # 62 bytes: too long
    b64u_encode(b"\x02" + os.urandom(60)),          # wrong version byte
    b64u_encode(b"\x01" + os.urandom(60)) + "=",    # padded
])
def test_bad_wrapped_dek_link_is_400(owner, w):
    _up(owner.client)
    r = _link(owner.client, wrapped_dek_link=w)
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    assert set(r.json()) == {"error", "detail"}


@pytest.mark.parametrize("body", [b"not json", b"[]", b'"x"', b"\xff\xfe", b""])
def test_non_object_body_is_400(owner, body):
    _up(owner.client)
    r = owner.client.post("/api/files/FILE1/links", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_oversized_body_is_413(owner):
    _up(owner.client)
    big = b'{"ttl":"7d","wrapped_dek_link":"' + b"A" * 20000 + b'"}'
    r = owner.client.post("/api/files/FILE1/links", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"] == "too_large"

    def stream():
        yield big

    r = owner.client.post("/api/files/FILE1/links", content=stream(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


@pytest.mark.parametrize("ref", ["FILE9", "nope", "0"])
def test_link_for_unknown_file_is_404(owner, ref):
    _up(owner.client)
    r = _link(owner.client, ref)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_link_for_deleted_file_is_404(owner):
    _up(owner.client)
    assert owner.client.delete("/api/files/FILE1").status_code == 204
    r = _link(owner.client)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_link_for_expired_file_is_404(frozen_clock, owner):
    frozen_clock(T0)
    _up(owner.client, ttl="1d")
    frozen_clock(T0 + timedelta(days=1))
    r = _link(owner.client)
    assert r.status_code == 404 and r.json()["error"] == "not_found"


# --- list and revoke ------------------------------------------------------------------

def test_list_shows_live_links_without_tokens(frozen_clock, owner, bare):
    frozen_clock(T0)
    _up(owner.client, ttl="never")
    _up(owner.client, ttl="never")
    a = _mk(owner.client, ttl="1h")
    b = _mk(owner.client, ttl="7d", max_downloads=1)
    c = _mk(owner.client, ttl="30d")
    d = _mk(owner.client, ttl="30d", max_downloads=2)
    other = _mk(owner.client, "FILE2")
    assert owner.client.delete(f"/api/links/{c['id']}").status_code == 204
    assert bare.get(f"/api/public/{b['token']}/blob").status_code == 200      # used up
    assert bare.get(f"/api/public/{d['token']}/blob").status_code == 200      # 1 of 2
    r = owner.client.get("/api/files/FILE1/links")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    links = r.json()["links"]
    assert [x["id"] for x in links] == [d["id"], a["id"]]          # newest first
    for x in links:
        assert set(x) == LINK_OUT_KEYS
        assert all(t["token"] not in str(x) for t in (a, b, c, d))
    by_id = {x["id"]: x for x in links}
    assert by_id[d["id"]]["downloads"] == 1 and by_id[d["id"]]["max_downloads"] == 2
    assert by_id[a["id"]]["file"] == "FILE1"
    assert by_id[a["id"]]["created_by"] == {"id": None, "name": "browser"}
    # an hour later the 1h link has gone too
    frozen_clock(T0 + timedelta(hours=1))
    assert [x["id"] for x in owner.client.get("/api/files/FILE1/links").json()["links"]] == [d["id"]]
    # the all-links view covers every file
    assert {x["id"] for x in owner.client.get("/api/links").json()["links"]} == {d["id"], other["id"]}


def test_list_for_deleted_or_unknown_file(owner):
    _up(owner.client)
    assert owner.client.get("/api/files/FILE9/links").status_code == 404
    owner.client.delete("/api/files/FILE1")
    r = owner.client.get("/api/files/FILE1/links")
    assert r.status_code == 410 and r.json()["error"] == "deleted"


def test_revoke_is_idempotent_and_unknown_is_404(frozen_clock, owner, settings, bare):
    frozen_clock(T0)
    _up(owner.client)
    made = _mk(owner.client)
    assert owner.client.delete(f"/api/links/{made['id']}").status_code == 204
    frozen_clock(T0 + timedelta(minutes=5))
    assert owner.client.delete(f"/api/links/{made['id']}").status_code == 204
    assert _db(settings).execute("SELECT revoked_at FROM links").fetchone()[0] == "2026-09-24T12:00:00Z"
    for bad in ("lnk_ffffffffffff", "nope", "x" * 300):
        r = owner.client.delete(f"/api/links/{bad}")
        assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert bare.get(f"/api/public/{made['token']}").status_code == 404


# --- public routes --------------------------------------------------------------------

def test_public_meta(frozen_clock, owner, bare):
    frozen_clock(T0)
    f = _up(owner.client, ttl="30d")
    made = _mk(owner.client, ttl="1d")
    wdl = owner.client.get("/api/files/FILE1")   # sanity: the file itself is untouched
    assert wdl.status_code == 200
    r = bare.get(f"/api/public/{made['token']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == PUBLIC_KEYS
    assert (body["uuid"], body["key_version"], body["enc_meta"], body["size"], body["created_at"]) == (
        f["uuid"], 1, f["enc_meta"], f["size"], f["created_at"])
    assert body["expires_at"] == "2026-09-25T12:00:00Z"
    assert body["wrapped_dek_link"] != f["wrapped_dek"]
    assert body["downloads_left"] is None                # unlimited
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "set-cookie" not in r.headers


def test_public_meta_does_not_count_a_download(owner, bare, settings):
    _up(owner.client)
    made = _mk(owner.client, max_downloads=1)
    for _ in range(3):
        assert bare.get(f"/api/public/{made['token']}").status_code == 200
    assert _db(settings).execute("SELECT downloads FROM links").fetchone()[0] == 0


def test_public_meta_counts_down_downloads_left(owner, bare):
    _up(owner.client)
    made = _mk(owner.client, max_downloads=3)
    meta = lambda: bare.get(f"/api/public/{made['token']}")  # noqa: E731
    assert meta().json()["downloads_left"] == 3
    for left in (2, 1):
        assert bare.get(f"/api/public/{made['token']}/blob").status_code == 200
        assert meta().json()["downloads_left"] == left
    assert bare.get(f"/api/public/{made['token']}/blob").status_code == 200
    assert meta().status_code == 404                        # used up: the same 404 as ever
    unlimited = _mk(owner.client)
    bare.get(f"/api/public/{unlimited['token']}/blob")
    assert bare.get(f"/api/public/{unlimited['token']}").json()["downloads_left"] is None


def test_public_blob_streams_and_counts(owner, bare, settings):
    u = os.urandom(16).hex()
    blob = make_fake_blob(u, body_len=5000)
    meta = fake_meta(u)
    assert upload(owner.client, None, u, blob=blob, meta=meta).status_code == 201
    made = _mk(owner.client)
    for i in range(1, 4):
        r = bare.get(f"/api/public/{made['token']}/blob")
        assert r.status_code == 200
        assert r.content == blob
        assert r.headers["content-type"] == "application/octet-stream"
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert _db(settings).execute("SELECT downloads FROM links").fetchone()[0] == i


def test_max_downloads_is_enforced(owner, bare):
    _up(owner.client)
    made = _mk(owner.client, max_downloads=2)
    assert bare.get(f"/api/public/{made['token']}/blob").status_code == 200
    assert bare.get(f"/api/public/{made['token']}").status_code == 200
    assert bare.get(f"/api/public/{made['token']}/blob").status_code == 200
    for path in ("", "/blob"):
        r = bare.get(f"/api/public/{made['token']}{path}")
        assert (r.status_code, r.json()) == (404, NOT_FOUND)


def test_two_concurrent_downloads_of_a_single_use_link_have_one_winner(owner, app, settings):
    _up(owner.client)
    made = _mk(owner.client, max_downloads=1)
    for _round in range(5):
        barrier = threading.Barrier(2)
        codes, lock = [], threading.Lock()
        tok = made["token"]

        def fetch():
            with TestClient(app, base_url="http://testserver") as c:
                barrier.wait()
                r = c.get(f"/api/public/{tok}/blob")
            with lock:
                codes.append(r.status_code)

        ts = [threading.Thread(target=fetch) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert sorted(codes) == [200, 404], codes
        assert _db(settings).execute("SELECT downloads FROM links WHERE id=?", (made["id"],)).fetchone()[0] == 1
        made = _mk(owner.client, max_downloads=1)


def test_claim_download_is_atomic_across_connections(owner, settings):
    """The SQL itself: two connections race for the last download of a max-1 link; exactly one wins."""
    _up(owner.client)
    made = _mk(owner.client, max_downloads=1)
    h = hashlib.sha256(made["token"].encode()).hexdigest()
    c1, c2 = _db(settings), _db(settings)
    barrier = threading.Barrier(2)
    wins = []

    def race(conn):
        barrier.wait()
        wins.append(links_routes.claim_download(conn, h) is not None)

    ts = [threading.Thread(target=race, args=(c,)) for c in (c1, c2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(wins) == [False, True]
    assert c1.execute("SELECT downloads FROM links").fetchone()[0] == 1


def _dead_links(frozen_clock, owner, settings) -> list[str]:
    """One token per way a link can be dead, all created at T0."""
    frozen_clock(T0)
    tokens = []
    _up(owner.client, ttl="never")                                    # FILE1
    revoked = _mk(owner.client)
    owner.client.delete(f"/api/links/{revoked['id']}")
    tokens.append(revoked["token"])
    tokens.append(_mk(owner.client, ttl="1h")["token"])             # expired (clock moves below)
    used = _mk(owner.client, max_downloads=1)
    assert TestClient(owner.client.app).get(f"/api/public/{used['token']}/blob").status_code == 200
    tokens.append(used["token"])
    _up(owner.client, ttl="never")                                    # FILE2, deleted
    tokens.append(_mk(owner.client, "FILE2")["token"])
    owner.client.delete("/api/files/FILE2")
    _up(owner.client, ttl="1d")                                       # FILE3, expires at T0+1d
    tokens.append(_mk(owner.client, "FILE3", ttl="1h")["token"])
    tokens.append(b64u_encode(os.urandom(32)))                       # unknown, well-formed
    frozen_clock(T0 + timedelta(days=1))
    return tokens


@pytest.mark.parametrize("suffix", ["", "/blob"])
def test_every_dead_link_gets_the_same_404(frozen_clock, owner, settings, bare, suffix):
    tokens = _dead_links(frozen_clock, owner, settings)
    bad_format = ["short", "A" * 44, "A" * 42 + "!", "%2e%2e", "A" * 43 + "%00"]
    responses = [bare.get(f"/api/public/{t}{suffix}") for t in tokens + bad_format]
    responses.append(bare.get("/api/public/a/b/c"))
    for r in responses:
        assert r.status_code == 404, r.url
        assert r.json() == NOT_FOUND, r.url
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["referrer-policy"] == "no-referrer"


def test_deleting_a_file_revokes_its_links(frozen_clock, owner, settings, bare):
    frozen_clock(T0)
    _up(owner.client)
    made = _mk(owner.client)
    assert owner.client.delete("/api/files/FILE1").status_code == 204
    row = _db(settings).execute("SELECT revoked_at FROM links").fetchone()
    assert row["revoked_at"] == "2026-09-24T12:00:00Z"
    assert bare.get(f"/api/public/{made['token']}").status_code == 404


def test_expiry_sweep_revokes_the_files_links(frozen_clock, owner, settings, app):
    frozen_clock(T0)
    _up(owner.client, ttl="1d")
    _up(owner.client, ttl="never")
    _mk(owner.client, "FILE1", ttl="1h")
    keep = _mk(owner.client, "FILE2", ttl="30d")
    frozen_clock(T0 + timedelta(days=2))
    conn = _db(settings)
    assert expiry.expire_files(conn, app.state.blobs) == 1
    rows = {r["file_n"]: r["revoked_at"] for r in conn.execute("SELECT file_n, revoked_at FROM links")}
    assert rows == {1: "2026-09-26T12:00:00Z", 2: None}
    assert TestClient(app).get(f"/api/public/{keep['token']}").status_code == 200


# --- rate limit -----------------------------------------------------------------------

def test_public_routes_are_rate_limited_per_ip(frozen_clock, owner, bare):
    frozen_clock(T0)
    _up(owner.client)
    tok = _mk(owner.client)["token"]
    ip_a = {"X-Real-IP": "203.0.113.7"}
    for i in range(60):
        path = f"/api/public/{tok}" if i % 2 else f"/api/public/{b64u_encode(os.urandom(32))}"
        assert bare.get(path, headers=ip_a).status_code in (200, 404), i
    for path in (f"/api/public/{tok}", f"/api/public/{tok}/blob", "/api/public/unknown"):
        r = bare.get(path, headers=ip_a)
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"
        assert r.headers["cache-control"] == "no-store"
    # another IP is unaffected, and the owner routes are not limited by it
    assert bare.get(f"/api/public/{tok}", headers={"X-Real-IP": "203.0.113.8"}).status_code == 200
    assert owner.client.get("/api/files/FILE1/links", headers=ip_a).status_code == 200
    # a minute later the window has moved on
    frozen_clock(T0 + timedelta(seconds=61))
    assert bare.get(f"/api/public/{tok}", headers=ip_a).status_code == 200


def test_rate_limit_logs_nothing_on_the_auth_logger(frozen_clock, owner, bare, caplog):
    frozen_clock(T0)
    with caplog.at_level(logging.WARNING, logger="fileshare.auth"):
        for _ in range(65):
            bare.get("/api/public/unknown", headers={"X-Real-IP": "203.0.113.9"})
    assert not [r for r in caplog.records if r.name == "fileshare.auth"]


# --- the viewer page and the service worker -------------------------------------------

def test_viewer_page_needs_no_session(owner, bare):
    _up(owner.client)
    tok = _mk(owner.client)["token"]
    r = bare.get(f"/p/{tok}", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "/static/js/public.js?v=" in r.text
    assert "{{" not in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "content-security-policy" in r.headers
    assert "set-cookie" not in r.headers


def test_viewer_page_is_served_before_setup_too(bare):
    r = bare.get(f"/p/{b64u_encode(os.urandom(32))}", follow_redirects=False)
    assert r.status_code == 200


@pytest.mark.parametrize("tok", ["short", "A" * 44, "A" * 42 + "!", "A" * 42 + "."])
def test_viewer_page_with_a_bad_token_is_the_same_page_as_404(bare, tok):
    good = bare.get(f"/p/{b64u_encode(os.urandom(32))}")
    r = bare.get(f"/p/{tok}", follow_redirects=False)
    assert r.status_code == 404
    assert r.text == good.text
    assert r.headers["cache-control"] == "no-store"


def test_service_worker_route(bare):
    r = bare.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].split(";")[0] == "text/javascript"
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["service-worker-allowed"] == "/"
    assert "content-security-policy" in r.headers
    assert r.content == (links_routes.STATIC_DIR / "sw.js").read_bytes()


# --- the token never reaches a log line -----------------------------------------------

def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                             ("127.0.0.1:5000", "GET", path, "1.1", 200), None)


@pytest.mark.parametrize("path,want", [
    ("/p/abcDEF_-123", "/p/<redacted>"),
    ("/p/abc?x=1", "/p/<redacted>"),
    ("/api/public/abc", "/api/public/<redacted>"),
    ("/api/public/abc/blob", "/api/public/<redacted>"),
    ("/api/public/", "/api/public/<redacted>"),
    ("/api/files/FILE1", "/api/files/FILE1"),
    ("/static/js/public.js?v=1", "/static/js/public.js?v=1"),
])
def test_access_log_filter_redacts_tokens(path, want):
    rec = _access_record(path)
    assert links_routes.RedactTokens().filter(rec) is True
    assert rec.args[2] == want
    assert want in rec.getMessage()
    if path.startswith(("/p/", "/api/public/")):
        assert "abc" not in rec.getMessage()


def test_access_log_filter_redacts_a_preformatted_message():
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                            'GET /p/SECRETTOKEN and /api/public/SECRETTOKEN/blob', None, None)
    links_routes.RedactTokens().filter(rec)
    assert "SECRETTOKEN" not in rec.getMessage()


def test_access_log_filter_is_installed_on_uvicorn_access(app):
    assert any(isinstance(f, links_routes.RedactTokens) for f in logging.getLogger("uvicorn.access").filters)
    # create_app is idempotent about it
    from fileshare.app import create_app
    create_app(app.state.settings)
    assert sum(isinstance(f, links_routes.RedactTokens)
               for f in logging.getLogger("uvicorn.access").filters) == 1


def test_a_link_holder_decrypts_with_only_the_url(live_server, sim, sharing):
    import io
    import urllib.parse
    import httpx
    sim.upload("notes.md", b"# hello\n" * 1000, note="for you")
    url, made = sim.create_link("FILE1", ttl="1d", max_downloads=1)
    parts = urllib.parse.urlsplit(url)
    token, lk = parts.path.removeprefix("/p/"), sharing.unb64u(parts.fragment)
    assert token == made["token"] and len(lk) == 32
    with httpx.Client(base_url=live_server.url) as c:        # no cookie, no Origin
        pub = c.get(f"/api/public/{token}").json()
        meta, dek = sharing.open_link_file(lk, pub)
        assert (meta["name"], meta["note"]) == ("notes.md", "for you")
        out = io.BytesIO()
        sharing.decrypt_stream(dek, bytes.fromhex(pub["uuid"]), io.BytesIO(c.get(f"/api/public/{token}/blob").content), out)
        assert out.getvalue() == b"# hello\n" * 1000
        assert c.get(f"/api/public/{token}/blob").status_code == 404      # max_downloads=1


def test_token_never_reaches_the_live_servers_log(live_server, sim):
    import httpx
    sim.upload("a.txt", b"hello", ttl="7d")
    r = sim.request("POST", "/api/files/FILE1/links", json={"wrapped_dek_link": _wdl(), "ttl": "1d"})
    assert r.status_code == 201, r.text
    tok = r.json()["token"]
    with httpx.Client(base_url=live_server.url) as c:
        assert c.get(f"/p/{tok}").status_code == 200
        assert c.get(f"/p/{tok}?utm=1").status_code == 200
        assert c.get(f"/api/public/{tok}").status_code == 200
        assert c.get(f"/api/public/{tok}/blob").status_code == 200
        assert c.get(f"/p/{tok[:20]}").status_code == 404
        assert c.get(f"/api/public/{tok[:20]}").status_code == 404
    sim.request("DELETE", f"/api/links/{r.json()['id']}")
    with httpx.Client(base_url=live_server.url) as c:
        assert c.get(f"/api/public/{tok}").status_code == 404
    log = live_server.log_path.read_text(encoding="utf-8", errors="replace")
    assert "/p/<redacted>" in log and "/api/public/<redacted>" in log   # access logging is on
    assert tok not in log and tok[:20] not in log


# --- fix round 1 ------------------------------------------------------------------------

def test_revoking_a_device_revokes_the_links_it_created(frozen_clock, owner, sharing, settings, bare):
    frozen_clock(T0)
    laptop = onboard_device(owner, sharing, "laptop", "proj")
    desk = onboard_device(owner, sharing, "desk", "proj")
    _up(owner.client)
    by_laptop = _mk(owner.client, headers=laptop.headers)
    by_desk = _mk(owner.client, headers=desk.headers)
    by_browser = _mk(owner.client)
    frozen_clock(T0 + timedelta(minutes=1))
    assert owner.client.delete(f"/api/devices/{laptop.id}").status_code == 204
    rows = {r["id"]: r["revoked_at"] for r in _db(settings).execute("SELECT id, revoked_at FROM links")}
    assert rows == {by_laptop["id"]: "2026-09-24T12:01:00Z", by_desk["id"]: None, by_browser["id"]: None}
    assert bare.get(f"/api/public/{by_laptop['token']}").status_code == 404
    assert bare.get(f"/api/public/{by_desk['token']}").status_code == 200
    # a device that revokes itself (uninstall) takes its links with it too
    assert owner.client.delete("/api/devices/self", headers=desk.headers).status_code == 204
    assert bare.get(f"/api/public/{by_desk['token']}").status_code == 404
    assert bare.get(f"/api/public/{by_browser['token']}").status_code == 200


def test_a_missing_blob_does_not_use_up_a_download(owner, bare, settings, app):
    f = _up(owner.client)
    made = _mk(owner.client, max_downloads=1)
    path = app.state.blobs.path_for(f["uuid"])
    saved = path.read_bytes()
    path.unlink()
    r = bare.get(f"/api/public/{made['token']}/blob")
    assert (r.status_code, r.json()) == (404, NOT_FOUND)
    assert _db(settings).execute("SELECT downloads FROM links").fetchone()[0] == 0
    path.write_bytes(saved)
    assert bare.get(f"/api/public/{made['token']}/blob").content == saved


def test_log_filter_scrubs_tracebacks_and_stack_info():
    import sys
    try:
        raise RuntimeError("failed on /api/public/SECRETTOKEN/blob")
    except RuntimeError:
        exc = sys.exc_info()
    rec = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "boom", None, exc)
    rec.stack_info = 'File "x", in GET /p/SECRETTOKEN'
    links_routes.RedactTokens().filter(rec)
    out = logging.Formatter("%(message)s").format(rec)
    assert "SECRETTOKEN" not in out
    assert "RuntimeError" in out and "/api/public/<redacted>" in out and "/p/<redacted>" in out


def test_exceptions_logged_through_uvicorn_are_redacted(app, caplog):
    logger = logging.getLogger("uvicorn.error")
    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        try:
            raise ValueError("bad path /p/SECRETTOKEN")
        except ValueError:
            logger.exception("Exception in ASGI application")
    text = "\n".join(caplog.handler.format(r) for r in caplog.records)
    assert "ValueError" in text and "/p/<redacted>" in text
    assert "SECRETTOKEN" not in text
