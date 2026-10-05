"""T09 (server) and T12: compressed JSON, a cursor on /api/mirrors, validators and long-lived caching."""
import gzip
import json

import pytest

from fileshare.routes import pages
from tests.helpers.tickets import device_client, new_uuid, other_device, other_device_client  # noqa: F401
from tests.test_mirrors_api import _body, owner_dc  # noqa: F401
from tests.test_pages import _mark_initialized


@pytest.fixture(autouse=True)
def _fresh_stamp():
    pages.build_stamp.cache_clear()
    yield
    pages.build_stamp.cache_clear()


def test_big_json_gets_gzip(client):
    from fastapi.testclient import TestClient  # noqa: PLC0415
    app = client.app
    @app.get("/api/_big")
    def big():
        return {"x": "a" * 5000}
    c = TestClient(app)
    r = c.get("/api/_big", headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip" and r.json() == {"x": "a" * 5000}
    assert r.headers["cache-control"] == "no-store"
    assert "content-encoding" not in c.get("/api/_big", headers={"Accept-Encoding": "identity"}).headers


def test_encrypted_blobs_are_not_compressed(client):
    from fastapi.responses import Response  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415
    @client.app.get("/api/_blob")
    def blob():
        return Response(b"\0" * 5000, media_type="application/octet-stream")
    r = TestClient(client.app).get("/api/_blob", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in r.headers and len(r.content) == 5000


def test_mirror_list_carries_the_cursor_the_changes_feed_continues_from(owner_dc, session_client):
    u = new_uuid()
    owner_dc.put(f"/api/mirrors/{u}", json=_body(needs="question"))
    listed = session_client.get("/api/mirrors").json()
    assert isinstance(listed["cursor"], int) and listed["cursor"] > 0
    assert session_client.get("/api/mirrors/changes", params={"after": listed["cursor"], "wait": 0}).json()["mirrors"] == []
    owner_dc.put(f"/api/mirrors/{u}", json=_body(needs="approval", rev=2))
    delta = session_client.get("/api/mirrors/changes", params={"after": listed["cursor"], "wait": 0}).json()
    assert [m["uuid"] for m in delta["mirrors"]] == [u]


def test_shell_pages_and_service_worker_have_etags_and_answer_304(client, settings):
    _mark_initialized(settings)
    for path in ("/login", "/sw.js", "/manifest.webmanifest"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", path
        etag = r.headers["etag"]
        again = client.get(path, headers={"If-None-Match": etag})
        assert again.status_code == 304 and again.content == b"", path
        assert again.headers["etag"] == etag and again.headers["cache-control"] == "no-cache"
        assert client.get(path, headers={"If-None-Match": 'W/"other"'}).status_code == 200


def test_stamped_static_is_immutable_only_for_the_deployed_build(client, monkeypatch):
    monkeypatch.setenv("FS_BUILD", "abc123")
    cc = lambda url: client.get(url).headers["cache-control"]  # noqa: E731
    assert cc("/static/css/app.css?v=abc123") == "public, max-age=31536000, immutable"
    assert cc("/static/css/app.css?v=old999") == "no-cache"        # another build's URL is never trusted
    assert cc("/static/css/app.css") == "no-cache"                 # dynamic imports carry no stamp
    assert client.get("/static/css/app.css").headers["etag"]
    monkeypatch.setenv("FS_BUILD", "dev")
    assert cc("/static/css/app.css?v=dev") == "no-cache"
