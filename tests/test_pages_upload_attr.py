# tests/test_pages_upload_attr.py
import re

import pytest

from fileshare import db


@pytest.fixture(autouse=True)
def _initialized(app, settings):
    conn = db.connect(settings.db_path)
    try:
        db.set_meta(conn, "auth_hash", "scrypt.x.y")
    finally:
        conn.close()


def test_index_carries_the_upload_limit(client, settings):
    html = client.get("/files").text
    assert f'data-max-upload="{settings.max_upload}"' in html


def test_index_has_upload_entry_and_hooks(client):
    html = client.get("/files").text
    for needle in ('id="upload-btn"', 'id="dropzone"', 'id="dock"', 'id="paste-btn"', 'id="record-btn"', 'id="banner"'):
        assert needle in html, needle
    assert re.search(r'<script type="module" src="/static/js/upload\.js\?v=[^"]+"></script>', html)
    assert "/static/js/onboard.js" not in html
