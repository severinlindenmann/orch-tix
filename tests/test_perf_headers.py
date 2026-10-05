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


def test_list_and_feed_carry_an_epoch_that_survives_restarts_but_not_a_wipe(owner_dc, session_client, settings):
    """A client holding a list + cursor learns from the epoch that the server started over (a wiped database numbers
    its events from 1 again, so an old cursor would answer 'nothing changed' and hide the whole list)."""
    from fileshare.db import connect, ensure_epoch
    owner_dc.put(f"/api/mirrors/{new_uuid()}", json=_body(needs="question"))
    listed = session_client.get("/api/mirrors").json()
    feed = session_client.get("/api/mirrors/changes", params={"after": listed["cursor"], "wait": 0}).json()
    assert isinstance(listed["epoch"], str) and len(listed["epoch"]) >= 16
    assert feed["epoch"] == listed["epoch"] and feed["head"] >= listed["cursor"]
    conn = connect(settings.db_path)
    assert ensure_epoch(conn) == listed["epoch"]                  # stable: a restart keeps it
    other = connect(settings.data_dir / "other.db")
    other.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    assert ensure_epoch(other) != listed["epoch"]                 # another database: another epoch


def test_deploy_stamp_follows_the_shipped_static_files(tmp_path):
    """`?v=<build>` is cached for a year (immutable): the stamp infra/sync.sh makes must change with a file's bytes even
    when the commit does not (an uncommitted edit), and stay a valid stamp."""
    import re
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    line = next(l for l in (root / "infra" / "sync.sh").read_text().splitlines() if l.startswith("BUILD="))
    probe = root / "fileshare" / "static" / ".stamp-probe"

    def stamp():
        out = subprocess.run(["bash", "-c", f'cd "{root}" && {line} && printf %s "$BUILD"'], capture_output=True, text=True, check=True)
        return out.stdout

    before = stamp()
    try:
        probe.write_text("one")
        one = stamp()
        probe.write_text("two")
        two = stamp()
    finally:
        probe.unlink(missing_ok=True)
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,40}", before) and len({before, one, two}) == 3
    assert stamp() == before
