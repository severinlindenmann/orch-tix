from tests.helpers.tickets import device_client, fake_env  # noqa: F401

SPACE = "b" * 32


def test_create_list_and_owner(device_client, session_client):
    r = device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    assert r.status_code == 201 and r.json()["owner_device"]
    spaces = session_client.get("/api/spaces").json()["spaces"]
    assert [s["id"] for s in spaces] == [SPACE] and spaces[0]["needs"] == 0


def test_duplicate_space_is_409(device_client):
    body = {"id": SPACE, "key_version": 1, "enc_label": fake_env()}
    device_client.post("/api/spaces", json=body)
    assert device_client.post("/api/spaces", json=body).status_code == 409


def test_session_cannot_create_a_space(session_client):
    assert session_client.post("/api/spaces", json={"id": SPACE, "key_version": 1,
                                                    "enc_label": fake_env()}).status_code == 403


def test_bad_space_id_is_400(device_client):
    assert device_client.post("/api/spaces", json={"id": "x", "key_version": 1, "enc_label": fake_env()}).status_code == 400


# --- join requests (ruling TIX-J1) ----------------------------------------------------------------------

import hashlib  # noqa: E402

import pytest  # noqa: E402

from tests.helpers.tickets import fake_dek, new_uuid, other_device, other_device_client  # noqa: E402,F401


class Recorder:
    def __init__(self):
        self.payloads = []

    def notify(self, ticket, event):
        raise AssertionError("v1 push for a space")

    def notify_payload(self, payload, exclude_sessions=None):
        self.payloads.append(payload)


@pytest.fixture
def owned(device_client):
    assert device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()}).status_code == 201
    return device_client


def _mirror(dc, u, rev=1):
    return dc.put(f"/api/mirrors/{u}", json={
        "space": SPACE, "mirror_rev": rev, "schema_version": "1.0.0", "status": "open", "priority": "normal",
        "needs": None, "open_questions": 0, "key_version": 1, "wrapped_dek": fake_dek(), "enc_content": fake_env(90),
        "event_uuid": hashlib.sha256(f"{u}|{rev}".encode()).hexdigest()[:32]})


def _owner_of(session_client):
    return next(s for s in session_client.get("/api/spaces").json()["spaces"] if s["id"] == SPACE)["owner_name"]


def test_a_remote_device_cannot_take_over(owned, other_device_client, session_client):
    r = other_device_client.post(f"/api/spaces/{SPACE}/join")
    assert r.status_code == 202 and r.json()["status"] == "pending" and r.json()["request"]["id"].startswith("jr_")
    assert _owner_of(session_client) == "laptop"
    assert _mirror(other_device_client, new_uuid()).json()["error"] == "not_owner"
    assert _mirror(owned, new_uuid()).status_code == 200


def test_a_device_cannot_approve_or_list(owned, other_device_client):
    req = other_device_client.post(f"/api/spaces/{SPACE}/join").json()["request"]["id"]
    for dc in (other_device_client, owned):
        r = dc.post(f"/api/spaces/{SPACE}/join-requests/{req}/approve")
        assert r.status_code == 403 and r.json()["error"] == "forbidden"
        assert dc.post(f"/api/spaces/{SPACE}/join-requests/{req}/deny").status_code == 403
        assert dc.get("/api/join-requests").status_code == 403


def test_a_session_approves_and_the_old_owner_gets_403(owned, other_device_client, session_client):
    req = other_device_client.post(f"/api/spaces/{SPACE}/join").json()["request"]["id"]
    assert [x["id"] for x in session_client.get("/api/join-requests").json()["requests"]] == [req]
    r = session_client.post(f"/api/spaces/{SPACE}/join-requests/{req}/approve")
    assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["decided_at"]
    assert _owner_of(session_client) == "desktop"
    r = _mirror(owned, new_uuid())
    assert r.status_code == 403 and r.json()["error"] == "not_owner"
    assert owned.get("/api/inbox/changes", params={"space": SPACE, "wait": 0}).status_code == 403
    assert _mirror(other_device_client, new_uuid()).status_code == 200
    assert session_client.get("/api/join-requests").json()["requests"] == []
    approved = session_client.get("/api/join-requests", params={"status": "approved"}).json()["requests"]
    assert approved[0]["decided_by"] and approved[0]["device_name"] == "desktop"     # the audit trail


def test_deny_keeps_the_owner_and_decisions_are_final(owned, other_device_client, session_client):
    req = other_device_client.post(f"/api/spaces/{SPACE}/join").json()["request"]["id"]
    assert session_client.post(f"/api/spaces/{SPACE}/join-requests/{req}/deny").json()["status"] == "denied"
    assert session_client.post(f"/api/spaces/{SPACE}/join-requests/{req}/deny").status_code == 200
    r = session_client.post(f"/api/spaces/{SPACE}/join-requests/{req}/approve")
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    assert _owner_of(session_client) == "laptop"
    assert other_device_client.get(f"/api/spaces/{SPACE}/join-requests/{req}").json()["status"] == "denied"


def test_asking_twice_is_one_request_and_the_owner_asking_is_a_no_op(owned, other_device_client):
    a = other_device_client.post(f"/api/spaces/{SPACE}/join").json()["request"]["id"]
    assert other_device_client.post(f"/api/spaces/{SPACE}/join").json()["request"]["id"] == a
    r = owned.post(f"/api/spaces/{SPACE}/join")
    assert r.status_code == 200 and r.json()["status"] == "owner" and r.json()["request"] is None


def test_only_the_requesting_device_reads_its_request(owned, other_device_client, session_client):
    req = other_device_client.post(f"/api/spaces/{SPACE}/join").json()["request"]["id"]
    assert owned.get(f"/api/spaces/{SPACE}/join-requests/{req}").status_code == 403
    assert session_client.get(f"/api/spaces/{SPACE}/join-requests/{req}").json()["status"] == "pending"
    assert session_client.get(f"/api/spaces/{SPACE}/join-requests/jr_{new_uuid()}").status_code == 404


def test_a_join_request_pushes_without_names(app, owned, other_device_client):
    app.state.pusher = Recorder()
    other_device_client.post(f"/api/spaces/{SPACE}/join")
    other_device_client.post(f"/api/spaces/{SPACE}/join")              # asking again: no second push
    assert app.state.pusher.payloads == [{"v": 2, "s": SPACE, "t": "", "k": "join", "n": 1, "c": 1}]   # a pending join counts (TIX-M1)


def test_approve_is_origin_checked(owned, other_device_client, session_client):
    req = other_device_client.post(f"/api/spaces/{SPACE}/join").json()["request"]["id"]
    r = session_client.post(f"/api/spaces/{SPACE}/join-requests/{req}/approve", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and _owner_of(session_client) == "laptop"


def test_pending_join_requests_count_in_attention_until_decided(app, owned, other_device_client, session_client):
    from fileshare.db import connect
    from fileshare.mirrors import attention_total
    req = other_device_client.post(f"/api/spaces/{SPACE}/join").json()["request"]["id"]
    conn = connect(app.state.settings.db_path)
    try:
        assert attention_total(conn) == 1
        assert session_client.post(f"/api/spaces/{SPACE}/join-requests/{req}/deny").status_code == 200
        assert attention_total(conn) == 0
    finally:
        conn.close()


def test_a_failed_space_create_spends_no_slot(app, device_client, other_device_client):
    """Batch-2 review: a slot is spent only on success (a duplicate refunds it)."""
    from fileshare.security import WindowLimiter
    app.state.space_create_limiter = WindowLimiter(1, 3600)
    body = {"id": SPACE, "key_version": 1, "enc_label": fake_env()}
    assert device_client.post("/api/spaces", json=body).status_code == 201
    assert other_device_client.post("/api/spaces", json=body).status_code == 409        # duplicate: refunded
    r = other_device_client.post("/api/spaces", json=body | {"id": "e" * 32})
    assert r.status_code == 201, r.text
    assert other_device_client.post("/api/spaces", json=body | {"id": "d" * 32}).status_code == 429


def test_not_owner_says_how_to_take_the_space_over(owned, other_device_client):
    r = other_device_client.put(f"/api/mirrors/{new_uuid()}", json={
        "space": SPACE, "mirror_rev": 1, "schema_version": "1.0.0", "status": "open", "priority": "normal",
        "needs": None, "open_questions": 0, "key_version": 1, "wrapped_dek": fake_dek(), "enc_content": fake_env(90),
        "event_uuid": new_uuid()})
    assert r.status_code == 403 and r.json()["error"] == "not_owner"
    assert "ask to take it over with `sharing space join`; approve it in TIX" in r.json()["detail"]


# --- batch-2 review: join requests expire after 7 days; a decision clears the phone's notification -----------

def test_a_join_request_expires_after_seven_days(frozen_clock, app, owned, other_device_client, session_client):
    from datetime import datetime, timedelta, timezone
    from fileshare.db import connect
    from fileshare.mirrors import JOIN_REQUEST_TTL_DAYS, attention_total
    assert JOIN_REQUEST_TTL_DAYS == 7
    t0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    frozen_clock(t0)
    req = other_device_client.post(f"/api/spaces/{SPACE}/join").json()["request"]["id"]
    frozen_clock(t0 + timedelta(days=7) - timedelta(seconds=1))
    assert [r["id"] for r in session_client.get("/api/join-requests").json()["requests"]] == [req]
    frozen_clock(t0 + timedelta(days=7, seconds=1))
    assert session_client.get("/api/join-requests").json()["requests"] == []
    expired = session_client.get("/api/join-requests", params={"status": "expired"}).json()["requests"]
    assert [(r["id"], r["status"]) for r in expired] == [(req, "expired")]
    assert other_device_client.get(f"/api/spaces/{SPACE}/join-requests/{req}").json()["status"] == "expired"
    r = session_client.post(f"/api/spaces/{SPACE}/join-requests/{req}/approve")
    assert r.status_code == 409 and r.json()["error"] == "expired" and _owner_of(session_client) == "laptop"
    conn = connect(app.state.settings.db_path)
    try:
        assert attention_total(conn) == 0
    finally:
        conn.close()
    again = other_device_client.post(f"/api/spaces/{SPACE}/join")
    assert again.status_code == 202 and again.json()["request"]["id"] != req       # asking again starts over
    assert again.json()["request"]["status"] == "pending"


def test_deciding_a_join_request_pushes_a_silent_clear(app, owned, other_device_client, session_client):
    req = other_device_client.post(f"/api/spaces/{SPACE}/join").json()["request"]["id"]
    app.state.pusher = Recorder()
    assert session_client.post(f"/api/spaces/{SPACE}/join-requests/{req}/deny").status_code == 200
    assert session_client.post(f"/api/spaces/{SPACE}/join-requests/{req}/deny").status_code == 200   # no-op
    assert app.state.pusher.payloads == [{"v": 2, "s": SPACE, "t": "", "k": "clear", "n": 0, "c": 0}]
