"""Tags (spec §19): normalisation, upload, the all-tags filter, PUT, /api/tags, the auth matrix."""
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fileshare import expiry, tags
from fileshare.db import connect
from tests.helpers.blobs import make_fake_blob
from tests.helpers.files import fake_meta, upload
from tests.helpers.onboard import onboard_device

T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


def _up(client, headers=None, tags_=None, ttl=None) -> dict:
    u = os.urandom(16).hex()
    meta = fake_meta(u)
    if tags_ is not None:
        meta["tags"] = tags_
    if ttl is not None:
        meta["ttl"] = ttl
    r = upload(client, headers, u, meta=meta, blob=make_fake_blob(u))
    assert r.status_code == 201, r.text
    return r.json()


def _ids(resp) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [f["id"] for f in resp.json()["files"]]


def _put(client, ref, value, headers=None):
    return client.put(f"/api/files/{ref}/tags", json={"tags": value}, headers=headers or {})


@pytest.fixture
def bare(app):
    with TestClient(app, base_url="http://testserver") as c:
        yield c


# --- normalisation (one helper) --------------------------------------------------------

# The full good and bad sets; tests/client/test_cli_tags.py runs the CLI's copy of the rules over them too.
GOOD_TAGS = [
    ("notes", "notes"),
    ("  Notes  ", "notes"),
    ("Weekly Report", "weekly-report"),
    ("weekly_report", "weekly-report"),
    ("A_b c", "a-b-c"),
    ("x", "x"),
    ("0day", "0day"),
    ("a" * 40, "a" * 40),
    ("q3-2026", "q3-2026"),
    ("A_ B", "a--b"),
    ("debug_log", "debug-log"),
    ("a\x85", "a"),  # str.strip() removes \x85 and \x1c-\x1f (JS's trim() doesn't)
    ("\x1ca", "a"),
]
BAD_TAGS = [
    "", " ", "   ", "-lead", "_lead", " _x", "a" * 41, "ümlaut", "a.b", "a/b", "a\nb", "a\tb",
    "emoji🙂", "İstanbul", "\ufeffa", None, 7, ["x"], {"x": 1}, True,
]


@pytest.mark.parametrize("raw,want", GOOD_TAGS)
def test_normalise_tag(raw, want):
    assert tags.normalize_tag(raw) == want


@pytest.mark.parametrize("raw", BAD_TAGS)
def test_bad_tag_is_rejected(raw):
    with pytest.raises(tags.BadTag):
        tags.normalize_tag(raw)


def test_normalise_tags_sorts_and_dedupes():
    assert tags.normalize_tags(["b", "A", "a", " b ", "Weekly Report", "weekly_report"]) \
        == ["a", "b", "weekly-report"]
    assert tags.normalize_tags([]) == []


def test_at_most_ten_tags_after_dedupe():
    ten = [f"t{i}" for i in range(10)]
    assert tags.normalize_tags(ten + ["T0", "t1 "]) == sorted(ten)
    with pytest.raises(tags.BadTag):
        tags.normalize_tags(ten + ["t10"])


@pytest.mark.parametrize("raw", ["notes", None, 3, {"a": 1}, ("a",)])
def test_tags_must_be_a_list(raw):
    with pytest.raises(tags.BadTag):
        tags.normalize_tags(raw)


# --- upload ----------------------------------------------------------------------------

def test_upload_without_tags_has_an_empty_list(owner):
    assert _up(owner.client)["tags"] == []


def test_upload_stores_normalised_sorted_tags(owner, settings):
    f = _up(owner.client, tags_=["Weekly Report", "notes", "notes"])
    assert f["tags"] == ["notes", "weekly-report"]
    rows = connect(settings.db_path).execute("SELECT file_n, tag FROM file_tags ORDER BY tag").fetchall()
    assert [tuple(r) for r in rows] == [(1, "notes"), (1, "weekly-report")]
    assert owner.client.get("/api/files/FILE1").json()["tags"] == ["notes", "weekly-report"]


@pytest.mark.parametrize("bad", [["ok", "no/slash"], "notes", [1], [f"t{i}" for i in range(11)], ["a" * 41],
                                 None])
def test_upload_with_a_bad_tag_is_400_and_stores_nothing(owner, settings, bad):
    u = os.urandom(16).hex()
    meta = fake_meta(u) | {"tags": bad}
    r = upload(owner.client, None, u, meta=meta, blob=make_fake_blob(u))
    assert r.status_code == 400 and r.json()["error"] == "bad_tag", r.text
    conn = connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM file_tags").fetchone()[0] == 0
    for d in ("blobs", "tmp"):
        assert [p for p in (settings.data_dir / d).rglob("*") if p.is_file()] == []


def test_upload_from_a_device_with_tags(owner, device):
    f = _up(owner.client, device.headers, tags_=["debugging"])
    assert f["tags"] == ["debugging"] and f["device"]["id"] == device.id


def test_a_failed_blob_commit_leaves_no_tag_rows(owner, settings, app, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(app.state.blobs, "commit", boom)
    u = os.urandom(16).hex()
    with pytest.raises(OSError):
        upload(owner.client, None, u, meta=fake_meta(u) | {"tags": ["notes"]}, blob=make_fake_blob(u))
    _assert_nothing_stored(settings)


def _assert_nothing_stored(settings):
    conn = connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM file_tags").fetchone()[0] == 0
    for d in ("blobs", "tmp"):
        assert [p for p in (settings.data_dir / d).rglob("*") if p.is_file()] == [], d


class _FailingConn(sqlite3.Connection):
    """A connection whose chosen statements fail as SQLITE_BUSY would."""
    fail_on: set = set()

    def execute(self, sql, *args):
        if sql.strip() in self.fail_on:
            raise sqlite3.OperationalError("database is locked")
        return super().execute(sql, *args)


def _failing_connect(fail_on: set):
    def connect_(path):
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10,
                               factory=_FailingConn)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.fail_on = fail_on
        return conn
    return connect_


@pytest.mark.parametrize("stmt", ["BEGIN IMMEDIATE", "COMMIT"])
def test_upload_busy_on_begin_or_commit_leaves_no_row_blob_or_tmp(owner, settings, monkeypatch, stmt):
    from fileshare import deps
    monkeypatch.setattr(deps, "connect", _failing_connect({stmt}))
    u = os.urandom(16).hex()
    with pytest.raises(sqlite3.OperationalError):
        upload(owner.client, None, u, meta=fake_meta(u) | {"tags": ["notes"]}, blob=make_fake_blob(u))
    _assert_nothing_stored(settings)


@pytest.mark.parametrize("stmt", ["BEGIN IMMEDIATE", "COMMIT"])
def test_put_busy_on_begin_or_commit_keeps_the_old_tags(owner, monkeypatch, stmt):
    _up(owner.client, tags_=["keep"])
    from fileshare import deps
    real = deps.connect
    monkeypatch.setattr(deps, "connect", _failing_connect({stmt}))
    with pytest.raises(sqlite3.OperationalError):
        _put(owner.client, "FILE1", ["new"])
    monkeypatch.setattr(deps, "connect", real)
    assert owner.client.get("/api/files/FILE1").json()["tags"] == ["keep"]


# --- file objects ----------------------------------------------------------------------

def test_list_and_get_carry_sorted_tags(owner):
    _up(owner.client, tags_=["zeta", "alpha"])
    _up(owner.client)
    files = owner.client.get("/api/files").json()["files"]
    assert [(f["id"], f["tags"]) for f in files] == [("FILE2", []), ("FILE1", ["alpha", "zeta"])]
    assert owner.client.get("/api/files/1").json()["tags"] == ["alpha", "zeta"]


def test_list_loads_tags_in_one_query(owner, app, monkeypatch):
    """No N+1: a page of files costs one tag query, however many files it holds."""
    for i in range(6):
        _up(owner.client, tags_=[f"t{i}", "common"])
    seen = []
    real = connect

    def spying(path):
        conn = real(path)
        conn.set_trace_callback(seen.append)
        return conn
    from fileshare import deps
    monkeypatch.setattr(deps, "connect", spying)
    assert len(owner.client.get("/api/files?limit=50").json()["files"]) == 6
    tag_queries = [s for s in seen if "file_tags" in s]
    assert len(tag_queries) == 1, tag_queries


def test_a_tombstone_has_no_tags(owner):
    _up(owner.client, tags_=["notes"])
    assert owner.client.delete("/api/files/FILE1").status_code == 204
    r = owner.client.get("/api/files/FILE1")
    assert r.status_code == 410 and r.json()["file"]["tags"] == []
    listed = owner.client.get("/api/files").json()["files"]
    assert listed[0]["id"] == "FILE1" and listed[0]["tags"] == []


# --- the tag filter --------------------------------------------------------------------

def test_filter_requires_all_given_tags(owner):
    _up(owner.client, tags_=["a"])              # FILE1
    _up(owner.client, tags_=["a", "b"])         # FILE2
    _up(owner.client, tags_=["b"])              # FILE3
    _up(owner.client, tags_=["a", "b", "c"])    # FILE4
    _up(owner.client)                           # FILE5
    assert _ids(owner.client.get("/api/files?tag=a")) == ["FILE4", "FILE2", "FILE1"]
    assert _ids(owner.client.get("/api/files?tag=a&tag=b")) == ["FILE4", "FILE2"]
    assert _ids(owner.client.get("/api/files?tag=b&tag=a&tag=c")) == ["FILE4"]
    assert _ids(owner.client.get("/api/files?tag=nope")) == []


def test_filter_normalises_its_input(owner):
    _up(owner.client, tags_=["weekly-report"])
    assert _ids(owner.client.get("/api/files", params={"tag": "Weekly Report"})) == ["FILE1"]
    assert _ids(owner.client.get("/api/files", params=[("tag", "weekly_report"), ("tag", "WEEKLY-REPORT")])) \
        == ["FILE1"]


@pytest.mark.parametrize("q", ["tag=", "tag=a/b", "tag=" + "a" * 41,
                               "&".join(f"tag=t{i}" for i in range(11))])
def test_bad_filter_tag_is_400(owner, q):
    r = owner.client.get("/api/files?" + q)
    assert r.status_code == 400 and r.json()["error"] == "bad_tag"


def test_filter_combines_with_acked(owner):
    _up(owner.client, tags_=["r"])   # FILE1
    _up(owner.client, tags_=["r"])   # FILE2
    _up(owner.client)                # FILE3
    assert owner.client.post("/api/files/FILE2/ack").status_code == 204
    assert _ids(owner.client.get("/api/files?tag=r")) == ["FILE1"]
    assert _ids(owner.client.get("/api/files?tag=r&acked=1")) == ["FILE2", "FILE1"]


def test_filter_excludes_tombstones(owner):
    _up(owner.client, tags_=["r"])
    _up(owner.client, tags_=["r"])
    owner.client.delete("/api/files/FILE1")
    assert _ids(owner.client.get("/api/files?tag=r")) == ["FILE2"]


def test_paging_with_a_tag_never_skips_or_repeats(owner):
    want = []
    for i in range(1, 24):
        tagged = i % 3 != 0
        _up(owner.client, tags_=["r", "x"] if tagged else ["x"])
        if tagged and i % 4 != 0:
            want.append(f"FILE{i}")
    for i in range(4, 24, 4):
        assert owner.client.post(f"/api/files/FILE{i}/ack").status_code == 204
    want.reverse()
    got, before, pages = [], None, 0
    while True:
        q = "/api/files?tag=r&tag=x&limit=3" + (f"&before={before}" if before else "")
        body = owner.client.get(q).json()
        assert len(body["files"]) == 3 or body["next_before"] is None   # pages stay full
        got += [f["id"] for f in body["files"]]
        pages += 1
        before = body["next_before"]
        if before is None:
            break
    assert got == want and len(set(got)) == len(got)
    assert pages == -(-len(want) // 3)


# --- PUT /api/files/{ref}/tags ---------------------------------------------------------

def test_put_replaces_tags(owner, settings):
    _up(owner.client, tags_=["old", "keep"])
    r = _put(owner.client, "file1", ["keep", "New One", "new_one"])
    assert r.status_code == 200 and r.json() == {"tags": ["keep", "new-one"]}
    assert owner.client.get("/api/files/FILE1").json()["tags"] == ["keep", "new-one"]
    assert _put(owner.client, "1", []).json() == {"tags": []}
    assert connect(settings.db_path).execute("SELECT COUNT(*) FROM file_tags").fetchone()[0] == 0


def test_any_active_device_may_tag_any_file(owner, sharing, device):
    other = onboard_device(owner, sharing, "desk", "p2")
    _up(owner.client, device.headers)
    r = _put(owner.client, "FILE1", ["notes"], headers=other.headers)
    assert r.status_code == 200 and r.json() == {"tags": ["notes"]}
    _up(owner.client)                                       # a browser upload
    assert _put(owner.client, "FILE2", ["x"], headers=device.headers).status_code == 200


def test_put_does_not_touch_other_files(owner):
    _up(owner.client, tags_=["a"])
    _up(owner.client, tags_=["a"])
    _put(owner.client, "FILE1", ["b"])
    assert owner.client.get("/api/files/FILE2").json()["tags"] == ["a"]


@pytest.mark.parametrize("value", [["ok", "no/slash"], "notes", None, [None], [f"t{i}" for i in range(11)]])
def test_put_bad_tag_is_400_and_keeps_the_old_tags(owner, value):
    _up(owner.client, tags_=["keep"])
    r = _put(owner.client, "FILE1", value)
    assert r.status_code == 400 and r.json()["error"] == "bad_tag"
    assert owner.client.get("/api/files/FILE1").json()["tags"] == ["keep"]


def test_put_without_tags_key_is_400(owner):
    _up(owner.client)
    r = owner.client.put("/api/files/FILE1/tags", json={})
    assert r.status_code == 400 and r.json()["error"] == "bad_tag"


@pytest.mark.parametrize("body", [b"not json", b"[]", b'"x"', b""])
def test_put_non_object_body_is_400(owner, body):
    _up(owner.client)
    r = owner.client.put("/api/files/FILE1/tags", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"


def test_put_body_is_bounded(owner):
    _up(owner.client)
    big = b'{"tags": ["a"], "pad": "' + b"x" * 10000 + b'"}'
    r = owner.client.put("/api/files/FILE1/tags", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"] == "too_large"

    def chunks():                       # no Content-Length: still refused, never buffered whole
        yield b'{"tags": ["a"], "pad": "'
        for _ in range(10):
            yield b"x" * 1000
        yield b'"}'
    r = owner.client.put("/api/files/FILE1/tags", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


@pytest.mark.parametrize("ref", ["FILE9", "nope", "0"])
def test_put_unknown_file_is_404(owner, ref):
    _up(owner.client)
    r = _put(owner.client, ref, ["a"])
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_put_deleted_file_is_404(owner, settings):
    _up(owner.client, tags_=["a"])
    owner.client.delete("/api/files/FILE1")
    r = _put(owner.client, "FILE1", ["b"])
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert connect(settings.db_path).execute("SELECT COUNT(*) FROM file_tags").fetchone()[0] == 0


def test_put_expired_file_is_404(frozen_clock, owner):
    frozen_clock(T0)
    _up(owner.client, ttl="1d")
    frozen_clock(T0 + timedelta(days=1, seconds=1))
    r = _put(owner.client, "FILE1", ["b"])
    assert r.status_code == 404 and r.json()["error"] == "not_found"


# --- GET /api/tags ---------------------------------------------------------------------

def test_tags_counts_live_files_sorted_by_count_then_name(frozen_clock, owner):
    frozen_clock(T0)
    _up(owner.client, tags_=["notes", "b"])                       # FILE1 12:00
    frozen_clock(T0 + timedelta(hours=1))
    _up(owner.client, tags_=["notes", "a"])                       # FILE2 13:00
    frozen_clock(T0 + timedelta(hours=2))
    _up(owner.client, tags_=["notes"])                            # FILE3 14:00
    assert owner.client.post("/api/files/FILE3/ack").status_code == 204   # acked files still count
    r = owner.client.get("/api/tags")
    assert r.status_code == 200
    assert r.json() == {"tags": [
        {"tag": "notes", "count": 3, "last_used": "2026-09-24T14:00:00Z"},
        {"tag": "a", "count": 1, "last_used": "2026-09-24T13:00:00Z"},
        {"tag": "b", "count": 1, "last_used": "2026-09-24T12:00:00Z"},
    ]}


def test_tags_exclude_deleted_and_expired_files(frozen_clock, owner, settings):
    frozen_clock(T0)
    _up(owner.client, tags_=["gone", "keep"])                     # FILE1: deleted
    _up(owner.client, tags_=["short", "keep"], ttl="1d")          # FILE2: expires
    _up(owner.client, tags_=["keep"], ttl="30d")                  # FILE3
    owner.client.delete("/api/files/FILE1")
    frozen_clock(T0 + timedelta(days=1, seconds=1))
    assert owner.client.get("/api/tags").json() == {"tags": [
        {"tag": "keep", "count": 1, "last_used": "2026-09-24T12:00:00Z"}]}
    assert owner.client.get("/api/tags").json()["tags"][0]["count"] == 1


def test_tags_empty(owner):
    assert owner.client.get("/api/tags").json() == {"tags": []}


def test_delete_and_sweep_remove_tag_rows(frozen_clock, owner, settings, app):
    frozen_clock(T0)
    _up(owner.client, tags_=["a", "b"])
    _up(owner.client, tags_=["c"], ttl="1d")
    _up(owner.client, tags_=["d"], ttl="30d")
    owner.client.delete("/api/files/FILE1")
    conn = connect(settings.db_path)
    assert [r["tag"] for r in conn.execute("SELECT tag FROM file_tags ORDER BY tag")] == ["c", "d"]
    assert expiry.expire_files(conn, app.state.blobs, T0 + timedelta(days=2)) == 1
    assert [r["tag"] for r in conn.execute("SELECT tag FROM file_tags ORDER BY tag")] == ["d"]


# --- auth matrix -----------------------------------------------------------------------

TAG_ROUTES = [
    ("PUT", "/api/files/FILE1/tags"),
    ("GET", "/api/tags"),
    ("GET", "/api/files?tag=a"),
]


@pytest.mark.parametrize("method,path", TAG_ROUTES)
def test_tag_routes_need_auth(owner, bare, method, path):
    _up(owner.client)
    r = bare.request(method, path, json={"tags": ["a"]}, headers={"Origin": "http://testserver"})
    assert r.status_code == 401 and r.json()["error"] == "unauthenticated"


@pytest.mark.parametrize("origin", [None, "https://evil.example", ""])
def test_session_put_checks_origin(owner, app, origin):
    _up(owner.client, tags_=["keep"])
    c = TestClient(app, base_url="http://testserver")
    c.cookies = owner.client.cookies
    headers = {} if origin is None else {"Origin": origin}
    r = c.put("/api/files/FILE1/tags", json={"tags": ["a"]}, headers=headers)
    assert r.status_code == 403 and r.json()["error"] == "bad_origin"
    assert owner.client.get("/api/files/FILE1").json()["tags"] == ["keep"]


def test_session_gets_need_no_origin(owner, app):
    _up(owner.client, tags_=["a"])
    c = TestClient(app, base_url="http://testserver")
    c.cookies = owner.client.cookies
    assert c.get("/api/tags").status_code == 200
    assert c.get("/api/files?tag=a").status_code == 200


def test_device_put_needs_no_origin(owner, app, device):
    _up(owner.client)
    c = TestClient(app, base_url="http://testserver")
    assert c.put("/api/files/FILE1/tags", json={"tags": ["a"]}, headers=device.headers).status_code == 200
    assert c.get("/api/tags", headers=device.headers).json()["tags"][0]["tag"] == "a"


@pytest.mark.parametrize("method,path", TAG_ROUTES)
def test_pending_device_is_403(owner, sharing, method, path):
    _up(owner.client)
    dev = onboard_device(owner, sharing, "p", "proj", approve=False)
    r = owner.client.request(method, path, headers=dev.headers, json={"tags": ["a"]})
    assert r.status_code == 403 and r.json()["error"] == "pending"


@pytest.mark.parametrize("method,path", TAG_ROUTES)
def test_revoked_device_is_401(owner, device, method, path):
    _up(owner.client)
    assert owner.client.delete(f"/api/devices/{device.id}").status_code == 204
    r = owner.client.request(method, path, headers=device.headers, json={"tags": ["a"]})
    assert r.status_code == 401 and r.json()["error"] == "revoked"
