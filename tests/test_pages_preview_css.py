import base64
import hashlib
import re

import pytest

from fileshare import db
from fileshare.headers import PREVIEW_CSS, PREVIEW_CSS_SHA256


@pytest.fixture(autouse=True)
def _initialized(app, settings):
    # "/files" redirects to /setup until an owner exists; mark the server initialized.
    conn = db.connect(settings.db_path)
    try:
        db.set_meta(conn, "auth_hash", "scrypt.x.y")
    finally:
        conn.close()


def test_preview_css_survives_the_html_parser_verbatim():
    assert "<" not in PREVIEW_CSS
    assert "&" not in PREVIEW_CSS
    assert "\r" not in PREVIEW_CSS


def test_index_serves_preview_css_byte_identical(client):
    html = client.get("/files").text
    m = re.search(r'<template id="preview-css">(.*?)</template>', html, re.S)
    assert m, "files.html must contain <template id=\"preview-css\">{{PREVIEW_CSS}}</template>"
    assert m.group(1) == PREVIEW_CSS
    digest = base64.b64encode(hashlib.sha256(m.group(1).encode("utf-8")).digest()).decode()
    assert digest == PREVIEW_CSS_SHA256


def test_csp_allows_exactly_the_served_style(client):
    csp = client.get("/files").headers["content-security-policy"]
    assert f"'sha256-{PREVIEW_CSS_SHA256}'" in csp
    assert "'unsafe-inline'" not in csp


def test_vendor_scripts_load_before_modules(client):
    html = client.get("/files").text
    md = html.index("/static/vendor/markdown-it.min.js")
    purify = html.index("/static/vendor/purify.min.js")
    first_module = html.index('type="module"')
    assert md < first_module and purify < first_module
