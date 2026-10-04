import dataclasses
import os

import pytest

from tests.helpers.blobs import make_fake_blob
from tests.helpers.files import upload


@pytest.fixture
def settings(settings):
    return dataclasses.replace(settings, max_upload=1024)


def test_upload_over_limit_is_413_and_leaves_nothing(owner, device, settings):
    u = os.urandom(16).hex()
    r = upload(owner.client, device.headers, u, blob=make_fake_blob(u, body_len=2000))
    assert r.status_code == 413
    assert r.json()["error"] == "too_large"
    assert "1024" in r.json()["detail"]
    assert list((settings.data_dir / "tmp").iterdir()) == []
    assert owner.client.get("/api/files").json()["files"] == []


def test_upload_at_limit_is_accepted(owner, device):
    u = os.urandom(16).hex()
    blob = make_fake_blob(u, body_len=1024 - 34)
    assert len(blob) == 1024
    assert upload(owner.client, device.headers, u, blob=blob).status_code == 201
