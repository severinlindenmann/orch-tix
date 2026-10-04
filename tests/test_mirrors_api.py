import hashlib

import pytest

from tests.helpers.tickets import device_client, fake_dek, fake_env, new_uuid, other_device, other_device_client  # noqa: F401

SPACE = "a" * 32
DEK = fake_dek()          # one mirror keeps its wrapped DEK across updates (409 dek_mismatch otherwise)


def _space(dc, space=SPACE):
    r = dc.post("/api/spaces", json={"id": space, "key_version": 1, "enc_label": fake_env()})
    assert r.status_code == 201, r.text
    return r.json()


def _body(space=SPACE, rev=1, needs=None, **kw):
    body = {"space": space, "mirror_rev": rev, "schema_version": "1.0.0", "status": "waiting", "priority": "high",
            "needs": needs, "open_questions": 1 if needs == "question" else 0, "key_version": 1,
            "wrapped_dek": DEK, "enc_content": fake_env(200),
            "event_uuid": hashlib.sha256(f"{space}|{rev}".encode()).hexdigest()[:32]}
    body.update(kw)
    return body


@pytest.fixture
def owner_dc(device_client):
    _space(device_client)
    return device_client


def test_first_push_creates_a_tix_id(owner_dc):
    u = new_uuid()
    r = owner_dc.put(f"/api/mirrors/{u}", json=_body(needs="question"))
    assert r.status_code == 200 and r.json()["created"] is True and r.json()["id"].startswith("TIX-")
    got = owner_dc.get(f"/api/mirrors/u/{u}", params={"space": SPACE}).json()
    assert got["needs"] == "question" and got["wrapped_dek"] and got["schema_version"] == "1.0.0"


def test_older_rev_is_stale_rev_and_kept_newest(owner_dc):
    u = new_uuid()
    owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=5))
    r = owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=4))
    assert r.status_code == 409 and r.json()["error"] == "stale_rev"
    assert owner_dc.get(f"/api/mirrors/u/{u}", params={"space": SPACE}).json()["mirror_rev"] == 5


def test_same_rev_again_is_duplicate(owner_dc):
    u = new_uuid()
    body = _body(rev=3)
    owner_dc.put(f"/api/mirrors/{u}", json=body)
    r = owner_dc.put(f"/api/mirrors/{u}", json=body)
    assert r.status_code == 409 and r.json()["error"] == "duplicate_uuid"


def test_unknown_schema_major_is_422(owner_dc):
    r = owner_dc.put(f"/api/mirrors/{new_uuid()}", json=_body(schema_version="2.0.0"))
    assert r.status_code == 422 and r.json()["error"] == "schema_unsupported" and "schema 1" in r.json()["detail"]


def test_create_needs_a_wrapped_dek(owner_dc):
    r = owner_dc.put(f"/api/mirrors/{new_uuid()}", json=_body(wrapped_dek=None))
    assert r.status_code == 400


def test_non_owner_cannot_write_a_mirror(owner_dc, other_device_client):
    r = other_device_client.put(f"/api/mirrors/{new_uuid()}", json=_body())
    assert r.status_code == 403 and r.json()["error"] == "not_owner"


def test_session_cannot_write_a_mirror(owner_dc, session_client):
    r = session_client.put(f"/api/mirrors/{new_uuid()}", json=_body())
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


def test_unlink_tombstones_and_retires_the_uuid(owner_dc, app):
    u = new_uuid()
    owner_dc.put(f"/api/mirrors/{u}", json=_body())
    assert owner_dc.delete(f"/api/mirrors/{u}", params={"space": SPACE}).status_code == 204
    from fileshare.db import connect
    conn = connect(app.state.settings.db_path)
    try:
        row = conn.execute("SELECT * FROM tickets WHERE uuid = ?", (u,)).fetchone()
    finally:
        conn.close()
    assert row["enc_content"] is None and row["wrapped_dek"] is None and row["deleted_at"]
    r = owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=9))
    assert r.status_code == 410


def test_session_lists_mirrors_and_long_polls_changes(owner_dc, session_client):
    u = new_uuid()
    owner_dc.put(f"/api/mirrors/{u}", json=_body(needs="approval"))
    ms = session_client.get("/api/mirrors", params={"space": SPACE}).json()["mirrors"]
    assert [m["uuid"] for m in ms] == [u] and ms[0]["needs"] == "approval"
    r = session_client.get("/api/mirrors/changes", params={"after": 0, "wait": 0}).json()
    assert r["mirrors"][0]["uuid"] == u and r["cursor"] > 0


def test_legacy_lists_do_not_show_mirrors(owner_dc, session_client):
    owner_dc.put(f"/api/mirrors/{new_uuid()}", json=_body())
    assert session_client.get("/api/tickets").json()["tickets"] == []


def test_legacy_writes_are_410_when_frozen(tmp_path):
    from fastapi.testclient import TestClient
    from fileshare.app import create_app
    from fileshare.settings import Settings
    app = create_app(Settings(data_dir=tmp_path / "d", public_url="http://testserver", cookie_secure=False,
                              legacy_tickets="readonly"))
    with TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver"}) as c:
        r = c.post("/api/tickets", json={})
        assert r.status_code == 410 and r.json()["error"] == "legacy_readonly"


def test_non_owner_cannot_read_a_mirror_by_uuid(owner_dc, other_device_client):
    u = new_uuid()
    owner_dc.put(f"/api/mirrors/{u}", json=_body())
    r = other_device_client.get(f"/api/mirrors/u/{u}", params={"space": SPACE})
    assert r.status_code == 403 and r.json()["error"] == "not_owner"


def test_approved_join_moves_ownership(owner_dc, other_device_client, session_client):
    r = other_device_client.post(f"/api/spaces/{SPACE}/join")
    assert r.status_code == 202 and r.json()["space"]["owner_name"] == "laptop"
    req = r.json()["request"]["id"]
    assert session_client.post(f"/api/spaces/{SPACE}/join-requests/{req}/approve").json()["status"] == "approved"
    assert owner_dc.put(f"/api/mirrors/{new_uuid()}", json=_body()).status_code == 403
    assert other_device_client.put(f"/api/mirrors/{new_uuid()}", json=_body()).status_code == 200


def test_legacy_ticket_routes_do_not_see_mirrors(owner_dc, session_client):
    tix = owner_dc.put(f"/api/mirrors/{new_uuid()}", json=_body(needs="question")).json()["id"]
    assert session_client.get(f"/api/tickets/{tix}").status_code == 404
    assert session_client.get(f"/api/mirrors/{tix}").json()["needs"] == "question"
    assert session_client.get("/api/tickets/changes", params={"after": 0, "wait": 0}).json()["tickets"] == []


def test_mirror_uuid_used_elsewhere_is_uuid_taken(owner_dc):
    from tests.helpers.tickets import create
    legacy = create(owner_dc)
    r = owner_dc.put(f"/api/mirrors/{legacy['uuid']}", json=_body())
    assert r.status_code == 409 and r.json()["error"] == "uuid_taken"


def test_mirror_uuid_of_another_space_is_uuid_taken(owner_dc):
    u = new_uuid()
    assert owner_dc.put(f"/api/mirrors/{u}", json=_body()).status_code == 200
    other = "9" * 32
    _space(owner_dc, other)
    r = owner_dc.put(f"/api/mirrors/{u}", json=_body(space=other))
    assert r.status_code == 409 and r.json()["error"] == "uuid_taken"


def test_newer_rev_with_a_reused_event_uuid_is_accepted(owner_dc):
    u = new_uuid()
    ev = new_uuid()
    assert owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=1, event_uuid=ev)).status_code == 200
    r = owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=2, event_uuid=ev))
    assert r.status_code == 200 and r.json()["mirror_rev"] == 2
    r = owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=2, event_uuid=ev))
    assert r.status_code == 409 and r.json()["error"] == "duplicate_uuid"      # (uuid, rev) seen: a retry


def test_takeover_keeps_mirror_rev_so_the_phone_never_sees_a_rollback(owner_dc, other_device_client, session_client):
    """Final review I1: ownership moves, the rev stays. The new owner pushes above it (the CLI jumps to
    server_rev + 1); an old rev is stale, never a silent overwrite."""
    u = new_uuid()
    for rev in (1, 2, 3):
        assert owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=rev)).status_code == 200
    req = other_device_client.post(f"/api/spaces/{SPACE}/join").json()["request"]["id"]
    session_client.post(f"/api/spaces/{SPACE}/join-requests/{req}/approve")
    assert other_device_client.get(f"/api/mirrors/u/{u}", params={"space": SPACE}).json()["mirror_rev"] == 3
    r = other_device_client.put(f"/api/mirrors/{u}", json=_body(rev=1))
    assert r.status_code == 409 and r.json()["error"] == "stale_rev"
    r = other_device_client.put(f"/api/mirrors/{u}", json=_body(rev=4))
    assert r.status_code == 200 and r.json()["mirror_rev"] == 4 and r.json()["created"] is False


def test_legacy_freeze_covers_every_write_and_keeps_reads(tmp_path):
    from fastapi.testclient import TestClient
    from fileshare.app import create_app
    from fileshare.settings import Settings
    app = create_app(Settings(data_dir=tmp_path / "d", public_url="http://testserver", cookie_secure=False,
                              legacy_tickets="readonly"))
    with TestClient(app, base_url="http://testserver", headers={"Origin": "http://testserver"}) as c:
        for method, path in [("PATCH", "/api/tickets/TIX-1"), ("DELETE", "/api/tickets/TIX-1"),
                             ("POST", "/api/tickets/TIX-1/events"), ("POST", "/api/tickets/TIX-1/claim"),
                             ("DELETE", "/api/tickets/TIX-1/claim")]:
            r = c.request(method, path, json={})
            assert r.status_code == 410 and r.json()["error"] == "legacy_readonly", (method, path)
        assert c.get("/api/tickets").status_code == 401        # reads are not frozen, only authenticated


def test_legacy_tickets_setting_from_env():
    from fileshare.settings import Settings
    assert Settings.from_env({"FS_LEGACY_TICKETS": "readonly"}).legacy_tickets == "readonly"
    assert Settings.from_env({}).legacy_tickets == "writable"
    assert Settings.from_env({"FS_LEGACY_TICKETS": " ReadOnly "}).legacy_tickets == "readonly"


@pytest.mark.parametrize("bad", ["nope", "read-only", "true", "frozen"])
def test_a_misspelt_legacy_tickets_value_refuses_to_start(bad, tmp_path):
    """Final review M1: a typo must not silently leave legacy tickets writable."""
    from fileshare.settings import Settings
    with pytest.raises(ValueError, match="FS_LEGACY_TICKETS must be writable or readonly"):
        Settings.from_env({"FS_LEGACY_TICKETS": bad})
    with pytest.raises(ValueError, match="FS_LEGACY_TICKETS"):
        Settings(data_dir=tmp_path, public_url="http://x", legacy_tickets=bad)


# --- Task 2 review minors ----------------------------------------------------------------------------------

def test_update_with_another_wrapped_dek_or_key_version_is_dek_mismatch(owner_dc):
    u = new_uuid()
    owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=1))
    r = owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=2, wrapped_dek=fake_dek()))
    assert r.status_code == 409 and r.json()["error"] == "dek_mismatch"
    r = owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=2, key_version=2))
    assert r.status_code == 409 and r.json()["error"] == "dek_mismatch"
    assert owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=2, wrapped_dek=None)).status_code == 200
    assert owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=3)).status_code == 200      # the same DEK again
    assert owner_dc.get(f"/api/mirrors/u/{u}", params={"space": SPACE}).json()["wrapped_dek"] == DEK


def test_gone_message_names_the_new_link_generation(owner_dc):
    u = new_uuid()
    owner_dc.put(f"/api/mirrors/{u}", json=_body())
    owner_dc.delete(f"/api/mirrors/{u}", params={"space": SPACE})
    r = owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=2))
    assert r.status_code == 410 and "new link generation" in r.json()["detail"]


def test_mirror_creates_are_rate_limited_per_device(app, owner_dc):
    from fileshare.security import WindowLimiter
    app.state.mirror_create_limiter = WindowLimiter(2, 3600)
    u = new_uuid()
    assert owner_dc.put(f"/api/mirrors/{u}", json=_body()).status_code == 200
    assert owner_dc.put(f"/api/mirrors/{new_uuid()}", json=_body()).status_code == 200
    r = owner_dc.put(f"/api/mirrors/{new_uuid()}", json=_body())
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    assert owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=2)).status_code == 200      # updates are not limited


def test_space_creates_are_rate_limited_per_device(app, device_client, other_device_client):
    from fileshare.security import WindowLimiter
    app.state.space_create_limiter = WindowLimiter(1, 3600)
    _space(device_client)
    r = device_client.post("/api/spaces", json={"id": "1" * 32, "key_version": 1, "enc_label": fake_env()})
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    _space(other_device_client, "2" * 32)                                                # another device has its own


def test_default_limits():
    from fileshare import mirrors
    assert (mirrors.SPACE_CREATES_PER_HOUR, mirrors.MIRROR_CREATES_PER_HOUR) == (10, 300)


def test_unlink_acks_pending_decisions_unlinked(owner_dc, session_client):
    u = new_uuid()
    tix = owner_dc.put(f"/api/mirrors/{u}", json=_body(needs="question")).json()["id"]
    dec = session_client.post("/api/decisions", json={"uuid": new_uuid(), "space": SPACE, "ticket": tix,
                                                      "kind": "answer", "key_version": 1, "enc_body": fake_env(80)})
    assert dec.status_code == 201
    owner_dc.delete(f"/api/mirrors/{u}", params={"space": SPACE})
    got = session_client.get("/api/decisions", params={"space": SPACE}).json()["decisions"]
    assert [(d["id"], d["ack"]) for d in got] == [(dec.json()["id"], "unlinked")] and got[0]["ack_at"]
    assert owner_dc.get("/api/inbox/changes", params={"space": SPACE, "wait": 0}).json()["decisions"] == []


def test_tombstones_in_changes_carry_deleted_true(owner_dc, session_client):
    u = new_uuid()
    owner_dc.put(f"/api/mirrors/{u}", json=_body())
    cursor = session_client.get("/api/mirrors/changes", params={"after": 0, "wait": 0}).json()["cursor"]
    owner_dc.delete(f"/api/mirrors/{u}", params={"space": SPACE})
    r = session_client.get("/api/mirrors/changes", params={"after": cursor, "wait": 0}).json()
    [m] = r["mirrors"]
    assert m["uuid"] == u and m["deleted"] is True and m["enc_content"] is None and m["wrapped_dek"] is None


def test_a_failed_mirror_create_spends_no_slot(app, owner_dc):
    """Batch-2 review: uuid_taken (and any other failure) refunds the create slot."""
    from fileshare.security import WindowLimiter
    from tests.helpers.tickets import create
    legacy = create(owner_dc)
    app.state.mirror_create_limiter = WindowLimiter(1, 3600)
    assert owner_dc.put(f"/api/mirrors/{legacy['uuid']}", json=_body()).status_code == 409   # uuid_taken
    assert owner_dc.put(f"/api/mirrors/{new_uuid()}", json=_body()).status_code == 200
    r = owner_dc.put(f"/api/mirrors/{new_uuid()}", json=_body())
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"


def test_mirror_updates_are_rate_limited_per_device(app, owner_dc, other_device_client):
    """Final review M4: a runaway drain can't flood the server; refusals and stale pushes spend no slot."""
    from fileshare.security import WindowLimiter
    app.state.mirror_update_limiter = WindowLimiter(2, 3600)
    u = new_uuid()
    assert owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=1)).status_code == 200      # a create: other limiter
    assert owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=2)).status_code == 200
    assert owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=1)).status_code == 409      # stale: no slot spent
    assert owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=3)).status_code == 200
    r = owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=4))
    assert r.status_code == 429 and r.json()["error"] == "rate_limited" and "per hour" in r.json()["detail"]
    assert owner_dc.get(f"/api/mirrors/u/{u}", params={"space": SPACE}).json()["mirror_rev"] == 3


def test_the_default_update_limit_is_600_an_hour(app):
    assert app.state.mirror_update_limiter.limit == 600 and app.state.mirror_update_limiter.window_s == 3600


def test_the_owner_lookup_names_the_last_writer_and_owner_gen(owner_dc, other_device_client, session_client, device,
                                                              other_device):
    """Polish I1: the CLI re-bases only above a rev another device wrote; the server says who wrote it."""
    u = new_uuid()
    assert owner_dc.put(f"/api/mirrors/{u}", json=_body(rev=1)).status_code == 200
    got = owner_dc.get(f"/api/mirrors/u/{u}", params={"space": SPACE}).json()
    assert got["last_writer_device"] == device.id and got["owner_gen"] == 0
    req = other_device_client.post(f"/api/spaces/{SPACE}/join").json()["request"]["id"]
    session_client.post(f"/api/spaces/{SPACE}/join-requests/{req}/approve")
    got = other_device_client.get(f"/api/mirrors/u/{u}", params={"space": SPACE}).json()
    assert got["last_writer_device"] == device.id and got["owner_gen"] == 1
    assert other_device_client.put(f"/api/mirrors/{u}", json=_body(rev=2)).status_code == 200
    got = other_device_client.get(f"/api/mirrors/u/{u}", params={"space": SPACE}).json()
    assert got["last_writer_device"] == other_device.id
