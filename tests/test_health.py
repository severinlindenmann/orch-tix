from fastapi.testclient import TestClient

from fileshare.app import create_app
from fileshare.settings import Settings


def test_healthz_returns_ok(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, public_url="http://testserver", cookie_secure=False))
    resp = TestClient(app).get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
