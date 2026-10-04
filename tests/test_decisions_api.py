import hashlib

import pytest

from tests.helpers.tickets import device_client, fake_dek, fake_env, new_uuid, other_device, other_device_client  # noqa: F401

SPACE = "c" * 32


@pytest.fixture
def mirror(device_client):
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    u = new_uuid()
    r = device_client.put(f"/api/mirrors/{u}", json={
        "space": SPACE, "mirror_rev": 1, "schema_version": "1.0.0", "status": "waiting", "priority": "normal",
        "needs": "question", "open_questions": 1, "key_version": 1, "wrapped_dek": fake_dek(),
        "enc_content": fake_env(100), "event_uuid": hashlib.sha256(b"m1").hexdigest()[:32]})
    return r.json()["id"]


def _post(c, ticket, kind="answer", uuid=None, **kw):
    body = {"uuid": uuid or new_uuid(), "space": SPACE, "ticket": ticket, "kind": kind, "key_version": 1,
            "enc_body": fake_env(80)}
    body.update(kw)
    return c.post("/api/decisions", json=body)


def test_session_posts_and_owner_long_polls(session_client, device_client, mirror):
    r = _post(session_client, mirror)
    assert r.status_code == 201 and r.json()["id"].startswith("dec_")
    inbox = device_client.get("/api/inbox/changes", params={"space": SPACE, "after": 0, "wait": 0}).json()
    item = inbox["decisions"][0]
    assert item["kind"] == "answer" and item["ticket"] == mirror and item["ticket_wrapped_dek"] and inbox["cursor"] >= 1


def test_device_cannot_post_decisions(device_client, mirror):
    r = _post(device_client, mirror)
    assert r.status_code == 403 and r.json()["error"] == "forbidden"


def test_duplicate_decision_uuid_is_409(session_client, mirror):
    u = new_uuid()
    assert _post(session_client, mirror, uuid=u).status_code == 201
    r = _post(session_client, mirror, uuid=u)
    assert r.status_code == 409 and r.json()["error"] == "duplicate_uuid"


def test_ticket_request_has_no_ticket_and_others_need_one(session_client, mirror):
    assert _post(session_client, None, kind="ticket_request").status_code == 201
    assert _post(session_client, None, kind="answer").status_code == 400
    assert _post(session_client, mirror, kind="ticket_request").status_code == 400


def test_decision_for_a_mirror_of_another_space_is_400(session_client, device_client, mirror):
    other = "d" * 32
    device_client.post("/api/spaces", json={"id": other, "key_version": 1, "enc_label": fake_env()})
    assert _post(session_client, mirror, space=other).status_code == 400


def test_ack_hides_it_from_the_inbox_and_is_idempotent(session_client, device_client, mirror):
    dec = _post(session_client, mirror).json()["id"]
    assert device_client.post(f"/api/decisions/{dec}/ack", json={"ack": "applied"}).status_code == 204
    assert device_client.post(f"/api/decisions/{dec}/ack", json={"ack": "applied"}).status_code == 204
    assert device_client.post(f"/api/decisions/{dec}/ack", json={"ack": "ignored"}).status_code == 409
    assert device_client.get("/api/inbox/changes", params={"space": SPACE, "after": 0, "wait": 0}).json()["decisions"] == []
    listed = session_client.get("/api/decisions", params={"space": SPACE}).json()["decisions"]
    assert listed[0]["ack"] == "applied" and listed[0]["ack_at"]


def test_only_the_owner_acks(session_client, other_device_client, mirror):
    dec = _post(session_client, mirror).json()["id"]
    vm = other_device_client
    assert vm.post(f"/api/decisions/{dec}/ack", json={"ack": "applied"}).status_code == 403
    assert vm.get("/api/inbox/changes", params={"space": SPACE, "after": 0, "wait": 0}).status_code == 403


def test_inbox_stamps_the_desktop_heartbeat(session_client, device_client, mirror):
    device_client.get("/api/inbox/changes", params={"space": SPACE, "after": 0, "wait": 0})
    space = next(s for s in session_client.get("/api/spaces").json()["spaces"] if s["id"] == SPACE)
    assert space["last_seen_at"]


def test_unlink_acks_pending_decisions_unlinked(session_client, device_client, mirror):
    dec = _post(session_client, mirror).json()["id"]
    uuid = session_client.get(f"/api/mirrors/{mirror}").json()["uuid"]
    device_client.delete(f"/api/mirrors/{uuid}", params={"space": SPACE})
    listed = session_client.get("/api/decisions", params={"space": SPACE}).json()["decisions"]
    assert next(d for d in listed if d["id"] == dec)["ack"] == "unlinked"


def test_bad_ack_value_is_400(session_client, device_client, mirror):
    dec = _post(session_client, mirror).json()["id"]
    assert device_client.post(f"/api/decisions/{dec}/ack", json={"ack": "done"}).status_code == 400


# --- beyond the brief ---------------------------------------------------------------------------------------

def test_a_session_cannot_poll_the_inbox_or_ack(session_client, mirror):
    dec = _post(session_client, mirror).json()["id"]
    assert session_client.get("/api/inbox/changes", params={"space": SPACE, "wait": 0}).status_code == 403
    assert session_client.post(f"/api/decisions/{dec}/ack", json={"ack": "applied"}).status_code == 403


def test_non_owner_device_cannot_list_decisions(session_client, other_device_client, device_client, mirror):
    _post(session_client, mirror)
    assert other_device_client.get("/api/decisions", params={"space": SPACE}).status_code == 403
    assert len(device_client.get("/api/decisions", params={"space": SPACE}).json()["decisions"]) == 1


def test_list_filters_by_ticket(session_client, mirror):
    _post(session_client, mirror)
    _post(session_client, None, kind="ticket_request")
    assert len(session_client.get("/api/decisions", params={"space": SPACE}).json()["decisions"]) == 2
    only = session_client.get("/api/decisions", params={"space": SPACE, "ticket": mirror}).json()["decisions"]
    assert [d["ticket"] for d in only] == [mirror]


def test_inbox_cursor_skips_what_was_seen(session_client, device_client, mirror):
    first = _post(session_client, mirror).json()["seq"]
    _post(session_client, mirror)
    got = device_client.get("/api/inbox/changes", params={"space": SPACE, "after": first, "wait": 0}).json()
    assert len(got["decisions"]) == 1 and got["cursor"] > first


def test_ack_writes_an_ack_event_for_the_pwa(session_client, device_client, mirror):
    dec = _post(session_client, mirror).json()["id"]
    cursor = session_client.get("/api/mirrors/changes", params={"after": 0, "wait": 0}).json()["cursor"]
    device_client.post(f"/api/decisions/{dec}/ack", json={"ack": "applied"})
    after = session_client.get("/api/mirrors/changes", params={"after": cursor, "wait": 0}).json()
    assert [m["id"] for m in after["mirrors"]] == [mirror] and after["cursor"] > cursor


def test_resend_after_unlink_is_still_duplicate(session_client, device_client, mirror):
    u = new_uuid()
    _post(session_client, mirror, uuid=u)
    uuid = session_client.get(f"/api/mirrors/{mirror}").json()["uuid"]
    device_client.delete(f"/api/mirrors/{uuid}", params={"space": SPACE})
    assert _post(session_client, mirror, uuid=u).json()["error"] == "duplicate_uuid"


def test_decisions_are_rate_limited_per_session(app, session_client, mirror):
    from fileshare.security import WindowLimiter
    app.state.decision_limiter = WindowLimiter(2, 3600)
    assert _post(session_client, mirror).status_code == 201
    assert _post(session_client, mirror).status_code == 201
    r = _post(session_client, mirror)
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"


def test_ack_of_unknown_decision_is_404_and_bad_id_400(device_client, mirror):
    assert device_client.post(f"/api/decisions/dec_{new_uuid()}/ack", json={"ack": "applied"}).status_code == 404
    assert device_client.post("/api/decisions/dec_xyz/ack", json={"ack": "applied"}).status_code == 400


def test_a_failed_decision_spends_no_slot(app, session_client, mirror):
    """Batch-2 review: a duplicate or a bad ref refunds the session's decision slot."""
    from fileshare.security import WindowLimiter
    app.state.decision_limiter = WindowLimiter(1, 3600)
    assert _post(session_client, "TIX-9999").status_code == 400                    # bad_ref: refunded
    u = new_uuid()
    assert _post(session_client, mirror, uuid=u).status_code == 201
    app.state.decision_limiter = WindowLimiter(2, 3600)
    assert _post(session_client, mirror).status_code == 201
    assert _post(session_client, mirror, uuid=u).status_code == 409                # duplicate: refunded
    assert _post(session_client, mirror).status_code == 201
    assert _post(session_client, mirror).status_code == 429


# ---- feedback fix round F1: a non-final "waiting" ack says why a decision was not applied; a final one follows ----

def test_a_waiting_ack_is_not_final(session_client, device_client, mirror):
    dec = _post(session_client, mirror).json()["id"]
    assert device_client.post(f"/api/decisions/{dec}/ack", json={"ack": "waiting-switched-off"}).status_code == 204
    assert device_client.post(f"/api/decisions/{dec}/ack", json={"ack": "waiting-switched-off"}).status_code == 204
    listed = session_client.get("/api/decisions", params={"space": SPACE}).json()["decisions"]
    assert next(d for d in listed if d["id"] == dec)["ack"] == "waiting-switched-off"
    assert device_client.post(f"/api/decisions/{dec}/ack", json={"ack": "applied"}).status_code == 204   # Apply later
    assert device_client.post(f"/api/decisions/{dec}/ack", json={"ack": "waiting-check"}).status_code == 409
    listed = session_client.get("/api/decisions", params={"space": SPACE}).json()["decisions"]
    assert next(d for d in listed if d["id"] == dec)["ack"] == "applied"


def test_only_the_known_waiting_codes(session_client, device_client, mirror):
    dec = _post(session_client, mirror).json()["id"]
    assert device_client.post(f"/api/decisions/{dec}/ack", json={"ack": "waiting-because I say so"}).status_code == 400
    for code in ("waiting-unpaired", "waiting-signature", "waiting-time", "waiting-check"):
        assert device_client.post(f"/api/decisions/{dec}/ack", json={"ack": code}).status_code == 204


def test_unlink_finalizes_a_waiting_decision(session_client, device_client, mirror):
    dec = _post(session_client, mirror).json()["id"]
    device_client.post(f"/api/decisions/{dec}/ack", json={"ack": "waiting-unpaired"})
    uuid = session_client.get(f"/api/mirrors/{mirror}").json()["uuid"]
    device_client.delete(f"/api/mirrors/{uuid}", params={"space": SPACE})
    listed = session_client.get("/api/decisions", params={"space": SPACE}).json()["decisions"]
    assert next(d for d in listed if d["id"] == dec)["ack"] == "unlinked"
