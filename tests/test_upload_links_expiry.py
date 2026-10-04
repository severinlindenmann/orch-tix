"""expire_upload_links: the four purge rules (global-constraints.md Review Focus 1) and the
server-restart pending-upload survival test."""
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fileshare import clock
from fileshare.app import create_app
from fileshare.db import connect
from fileshare.expiry import expire_upload_links
from tests.helpers.blobs import make_fake_blob
from tests.helpers.files import drop

T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def bare(app):
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def _db(settings):
    return connect(settings.db_path)


def _mk(client, headers=None, **body) -> dict:
    from fileshare.security import b64u_encode
    body = {"uuid": os.urandom(16).hex(), "key_version": 1,
            "wrapped_lpriv": b64u_encode(b"\x01" + os.urandom(125)), "ttl": "1d"} | body
    r = client.post("/api/upload-links", json=body, headers=headers or {})
    assert r.status_code == 201, r.text
    return r.json()


def _insert_link(conn, **overrides) -> str:
    from fileshare.security import b64u_encode
    base = dict(
        id=f"upl_{os.urandom(6).hex()}",
        uuid=os.urandom(16).hex(),
        token_hash=os.urandom(32).hex(),
        key_version=1,
        wrapped_lpriv=b64u_encode(b"\x01" + os.urandom(125)),
        enc_label=None,
        created_at=clock.now_iso(),
        created_by_device=None,
        created_by_session="browser",
        expires_at=clock.now_iso(),
        revoked_at=None,
        used_at=None,
        file_uuid=None,
        file_size=None,
        sealed_dek=None,
        enc_meta=None,
        file_n=None,
        adopted_at=None,
    )
    base.update(overrides)
    cols = ", ".join(base)
    qs = ", ".join("?" for _ in base)
    conn.execute(f"INSERT INTO upload_links ({cols}) VALUES ({qs})", tuple(base.values()))
    return base["id"]


def _insert_file(conn, uuid_hex: str) -> int:
    cur = conn.execute(
        "INSERT INTO files (uuid, size, key_version, wrapped_dek, enc_meta, device_id, project,"
        " created_at, session_name, expires_at)"
        " VALUES (?, 1, 1, 'w', 'e', NULL, 'upload', ?, 'upload link', NULL)",
        (uuid_hex, clock.now_iso()))
    return cur.lastrowid


def _ids(conn) -> set[str]:
    return {r["id"] for r in conn.execute("SELECT id FROM upload_links").fetchall()}


# --- Review Focus 1: restart between upload and adoption -------------------------------------

def test_restart_keeps_pending_upload_alive_and_still_sweeps_orphans(owner, bare, app, settings):
    made = _mk(owner.client)
    u = os.urandom(16).hex()
    blob = make_fake_blob(u, body_len=55)
    assert drop(bare, made["token"], u, blob).status_code == 201

    orphan = os.urandom(16).hex()
    orphan_path = app.state.blobs.path_for(orphan)
    orphan_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_path.write_bytes(make_fake_blob(orphan))

    create_app(settings)   # a fresh app on the same settings: simulates a server restart

    assert app.state.blobs.path_for(u).is_file(), "the pending upload must survive a restart"
    assert not orphan_path.exists(), "an unrelated stray blob is still swept"

    from fileshare.security import b64u_encode
    r = owner.client.post(f"/api/upload-links/{made['id']}/adopt",
                          json={"wrapped_dek": b64u_encode(b"\x01" + os.urandom(60))})
    assert r.status_code == 201, r.text


# --- the four purge rules ---------------------------------------------------------------------

def test_purge_unused_link_more_than_7_days_past_expiry_or_revocation(frozen_clock, settings, app):
    frozen_clock(T0)
    conn = _db(settings)
    old = clock.now_iso(T0 - timedelta(days=7, seconds=1))
    edge = clock.now_iso(T0 - timedelta(days=6))
    by_expiry = _insert_link(conn, expires_at=old)
    by_revoke = _insert_link(conn, expires_at=clock.now_iso(T0 + timedelta(days=1)), revoked_at=old)
    kept = _insert_link(conn, expires_at=edge)
    n = expire_upload_links(conn, app.state.blobs)
    assert n == 2
    assert _ids(conn) == {kept}


def test_purge_pending_link_more_than_30_days_deletes_row_and_blob(frozen_clock, settings, app):
    frozen_clock(T0)
    conn = _db(settings)
    blobs = app.state.blobs
    old_uuid, kept_uuid = os.urandom(16).hex(), os.urandom(16).hex()
    for u in (old_uuid, kept_uuid):
        p = blobs.path_for(u)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(make_fake_blob(u))
    old = clock.now_iso(T0 - timedelta(days=30, seconds=1))
    edge = clock.now_iso(T0 - timedelta(days=29))
    old_id = _insert_link(conn, used_at=old, file_uuid=old_uuid, file_size=1,
                          sealed_dek=None, enc_meta=None)
    kept_id = _insert_link(conn, used_at=edge, file_uuid=kept_uuid, file_size=1,
                           sealed_dek=None, enc_meta=None)
    n = expire_upload_links(conn, blobs)
    assert n == 1
    assert _ids(conn) == {kept_id}
    assert not blobs.path_for(old_uuid).is_file()
    assert blobs.path_for(kept_uuid).is_file()


def test_purge_received_link_more_than_30_days_deletes_row_but_keeps_the_file(frozen_clock, settings, app):
    frozen_clock(T0)
    conn = _db(settings)
    n_old = _insert_file(conn, os.urandom(16).hex())
    n_kept = _insert_file(conn, os.urandom(16).hex())
    old = clock.now_iso(T0 - timedelta(days=30, seconds=1))
    edge = clock.now_iso(T0 - timedelta(days=29))
    old_id = _insert_link(conn, file_n=n_old, adopted_at=old)
    kept_id = _insert_link(conn, file_n=n_kept, adopted_at=edge)
    n = expire_upload_links(conn, app.state.blobs)
    assert n == 1
    assert _ids(conn) == {kept_id}
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2


def test_purge_discarded_link_more_than_7_days_deletes_row(frozen_clock, settings, app):
    frozen_clock(T0)
    conn = _db(settings)
    old = clock.now_iso(T0 - timedelta(days=7, seconds=1))
    edge = clock.now_iso(T0 - timedelta(days=6))
    old_id = _insert_link(conn, used_at=old, revoked_at=old, file_uuid=None)
    kept_id = _insert_link(conn, used_at=edge, revoked_at=edge, file_uuid=None)
    n = expire_upload_links(conn, app.state.blobs)
    assert n == 1
    assert _ids(conn) == {kept_id}


def test_rows_inside_every_window_are_kept(frozen_clock, settings, app):
    frozen_clock(T0)
    conn = _db(settings)
    n_file = _insert_file(conn, os.urandom(16).hex())
    waiting = _insert_link(conn, expires_at=clock.now_iso(T0 + timedelta(days=1)))
    pending = _insert_link(conn, used_at=clock.now_iso(T0), file_uuid=os.urandom(16).hex(), file_size=1)
    received = _insert_link(conn, file_n=n_file, adopted_at=clock.now_iso(T0))
    discarded = _insert_link(conn, used_at=clock.now_iso(T0), revoked_at=clock.now_iso(T0), file_uuid=None)
    n = expire_upload_links(conn, app.state.blobs)
    assert n == 0
    assert _ids(conn) == {waiting, pending, received, discarded}
