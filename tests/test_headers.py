import base64
import hashlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fileshare.headers import PREVIEW_CSS, PREVIEW_CSS_SHA256, SecurityHeadersMiddleware, csp

CSS_FILE = Path(__file__).resolve().parent.parent / "fileshare" / "static" / "css" / "preview.css"


def test_preview_css_is_the_file_verbatim_and_hash_matches():
    assert PREVIEW_CSS == CSS_FILE.read_text(encoding="utf-8")
    assert PREVIEW_CSS_SHA256 == base64.b64encode(hashlib.sha256(PREVIEW_CSS.encode("utf-8")).digest()).decode()
    assert "url(" not in PREVIEW_CSS and "@import" not in PREVIEW_CSS


def test_csp_exact():
    assert csp() == (
        "default-src 'self'; script-src 'self'; "
        f"style-src 'self' 'sha256-{PREVIEW_CSS_SHA256}'; "
        "img-src 'self' blob:; media-src 'self' blob:; frame-src 'self'; "
        "connect-src 'self' https://api.deepgram.com; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )


def test_connect_src_allows_only_self_and_deepgram():
    # Spec §20: the browser posts audio to Deepgram itself. Nothing else is added.
    directives = dict(d.strip().split(" ", 1) for d in csp().split(";"))
    assert directives["connect-src"] == "'self' https://api.deepgram.com"
    assert "deepgram" not in csp().replace("connect-src 'self' https://api.deepgram.com", "")


def test_no_permissions_policy_so_the_microphone_stays_same_origin_default(client):
    # Task 24: the recorder needs getUserMedia. With no Permissions-Policy header the microphone is
    # allowed for this origin only (the default allowlist is 'self'); nothing may widen that.
    r = client.get("/healthz")
    assert "permissions-policy" not in r.headers


def _mini():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/api/x")
    def api_x():
        return {"x": 1}

    @app.get("/page")
    def page():
        return {"p": 1}

    return TestClient(app)


def test_headers_on_every_response():
    c = _mini()
    for path in ("/api/x", "/page", "/missing"):
        r = c.get(path)
        assert r.headers["content-security-policy"] == csp()
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"


def test_no_store_only_on_api():
    c = _mini()
    assert c.get("/api/x").headers["cache-control"] == "no-store"
    assert "cache-control" not in c.get("/page").headers


def test_real_app_sets_headers(client):
    r = client.get("/healthz")
    assert r.headers["content-security-policy"] == csp()
