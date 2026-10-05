import pytest

from tests.helpers.tickets import device_client, fake_env, new_uuid, other_device, other_device_client  # noqa: F401


def _send(c, to_kind, to_id, **kw):
    body = {"uuid": new_uuid(), "to_kind": to_kind, "to_id": to_id, "kind": "text", "key_version": 1,
            "enc_body": fake_env()}
    body.update(kw)
    return c.post("/api/messages", json=body)


def test_device_receives_by_project(device_client, other_device_client):
    vm = other_device_client                                   # project "other"
    assert _send(device_client, "project", "other").status_code == 201
    got = vm.get("/api/messages", params={"after": 0, "wait": 0}).json()["messages"]
    assert len(got) == 1 and got[0]["from_kind"] == "device"
    assert device_client.get("/api/messages", params={"after": 0, "wait": 0}).json()["messages"] == []


def test_session_may_not_message_human_but_receives(session_client, device_client):
    assert _send(session_client, "human", "").status_code == 400
    _send(device_client, "human", "")
    assert len(session_client.get("/api/messages", params={"after": 0, "wait": 0}).json()["messages"]) == 1


def test_files_must_be_live(device_client):
    assert _send(device_client, "project", "x", files=["FILE999"]).status_code == 400


def test_ack_by_a_recipient_only(device_client, session_client):
    mid = _send(device_client, "human", "").json()["id"]
    assert device_client.post(f"/api/messages/{mid}/ack").status_code == 403
    assert session_client.post(f"/api/messages/{mid}/ack").status_code == 204


# --- beyond the brief ---------------------------------------------------------------------------------------

SPACE = "f" * 32


def test_device_receives_by_id_and_by_owned_space(device_client, other_device, other_device_client):
    other_device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    vm_id = other_device.id
    _send(device_client, "device", vm_id)
    _send(device_client, "space", SPACE)
    got = other_device_client.get("/api/messages", params={"after": 0, "wait": 0}).json()
    assert sorted(m["to_kind"] for m in got["messages"]) == ["device", "space"]
    assert got["cursor"] == got["messages"][-1]["seq"]


def test_message_shape_and_routing_cleartext_only(device_client, other_device_client):
    body = fake_env()
    _send(device_client, "project", "other", kind="question", enc_body=body, size=1234)
    m = other_device_client.get("/api/messages", params={"after": 0, "wait": 0}).json()["messages"][0]
    assert set(m) == {"id", "seq", "uuid", "from_kind", "from_name", "to_kind", "to_id", "kind", "key_version",
                      "enc_body", "files", "space", "ticket", "size", "created_at", "acked_at"}
    assert m["id"] == "msg_" + m["uuid"] and m["enc_body"] == body and m["size"] == 1234 and m["files"] == []
    assert m["from_name"] == "laptop" and m["space"] is None and m["ticket"] is None


def test_acked_messages_leave_the_list_and_ack_is_idempotent(device_client, session_client):
    mid = _send(device_client, "human", "").json()["id"]
    assert session_client.post(f"/api/messages/{mid}/ack").status_code == 204
    assert session_client.post(f"/api/messages/{mid}/ack").status_code == 204
    assert session_client.get("/api/messages", params={"after": 0, "wait": 0}).json()["messages"] == []


def test_session_messages_an_agent(session_client, device_client):
    assert _send(session_client, "project", "proj").status_code == 201
    m = device_client.get("/api/messages", params={"after": 0, "wait": 0}).json()["messages"][0]
    assert m["from_kind"] == "human"


def test_duplicate_uuid_is_409(device_client):
    u = new_uuid()
    assert _send(device_client, "human", "", uuid=u).status_code == 201
    r = _send(device_client, "human", "", uuid=u)
    assert r.status_code == 409 and r.json()["error"] == "duplicate_uuid"


def test_bad_routing_is_400(device_client):
    assert _send(device_client, "human", "x").status_code == 400            # human has no id
    assert _send(device_client, "project", "").status_code == 400           # everyone else needs one
    assert _send(device_client, "project", "a/b").status_code == 400
    assert _send(device_client, "team", "x").status_code == 400
    assert _send(device_client, "project", "x", kind="shell").status_code == 400
    assert _send(device_client, "project", "x", files=[f"FILE{i}" for i in range(11)]).status_code == 400
    assert _send(device_client, "project", "x", ticket="TIX-1").status_code == 400   # a ticket needs its space
    assert _send(device_client, "project", "x", space="a" * 32).status_code == 404   # no such space


def test_ticket_must_be_a_live_mirror_of_the_space(device_client):
    import hashlib
    from tests.helpers.tickets import fake_dek
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    tix = device_client.put(f"/api/mirrors/{new_uuid()}", json={
        "space": SPACE, "mirror_rev": 1, "schema_version": "1.0.0", "status": "open", "priority": "normal",
        "needs": None, "open_questions": 0, "key_version": 1, "wrapped_dek": fake_dek(), "enc_content": fake_env(90),
        "event_uuid": hashlib.sha256(b"x").hexdigest()[:32]}).json()["id"]
    assert _send(device_client, "human", "", space=SPACE, ticket=tix).status_code == 201
    assert _send(device_client, "human", "", space=SPACE, ticket="TIX-9999").status_code == 400


def test_live_files_are_kept_by_id(device_client):
    from tests.helpers.files import upload
    r = upload(device_client)
    assert r.status_code == 201, r.text
    f = r.json()["id"]
    assert _send(device_client, "project", "other", files=[f, f]).status_code == 201


# --- TIX-M1: acks are per recipient device ------------------------------------------------------------------

@pytest.fixture
def twin_client(app, owner, sharing):
    """A second device in project "other", next to other_device_client."""
    from tests.helpers.onboard import onboard_device
    from tests.helpers.tickets import device_client_for
    return device_client_for(app, onboard_device(owner, sharing, name="vm2", project="other"))


def _ids(c):
    return [m["id"] for m in c.get("/api/messages", params={"after": 0, "wait": 0}).json()["messages"]]


def test_project_message_stays_for_other_devices_until_each_acks(device_client, other_device_client, twin_client):
    mid = _send(device_client, "project", "other").json()["id"]
    assert _ids(other_device_client) == [mid] and _ids(twin_client) == [mid]
    assert other_device_client.post(f"/api/messages/{mid}/ack").status_code == 204
    assert _ids(other_device_client) == []
    assert _ids(twin_client) == [mid]                                  # still visible to the other device
    assert twin_client.post(f"/api/messages/{mid}/ack").status_code == 204
    assert _ids(twin_client) == []


def test_ack_by_one_device_never_hides_a_message_from_the_human(device_client, other_device_client, session_client):
    to_project = _send(device_client, "project", "other").json()["id"]
    to_human = _send(device_client, "human", "").json()["id"]
    assert other_device_client.post(f"/api/messages/{to_project}/ack").status_code == 204
    assert _ids(session_client) == [to_human]
    assert session_client.post(f"/api/messages/{to_human}/ack").status_code == 204
    assert _ids(session_client) == []


def test_space_message_ack_is_per_owner_device(device_client, other_device_client, twin_client, session_client):
    other_device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    mid = _send(twin_client, "space", SPACE).json()["id"]
    assert other_device_client.post(f"/api/messages/{mid}/ack").status_code == 204
    req = device_client.post(f"/api/spaces/{SPACE}/join").json()["request"]["id"]
    assert session_client.post(f"/api/spaces/{SPACE}/join-requests/{req}/approve").status_code == 200
    assert _ids(device_client) == [mid]           # the new owner has not acked it yet
    assert _ids(other_device_client) == []


def test_human_messages_open_counts_per_recipient(app, device_client, other_device_client, session_client):
    from fileshare.mirrors import human_messages_open
    mid = _send(device_client, "human", "").json()["id"]
    _send(device_client, "project", "other")
    from fileshare.db import connect
    conn = connect(app.state.settings.db_path)
    try:
        assert human_messages_open(conn) == 1
        session_client.post(f"/api/messages/{mid}/ack")
        assert human_messages_open(conn) == 0
    finally:
        conn.close()


# --- batch-2 review: space and ticket tags only from the space's owner ---------------------------------------

def test_a_device_tags_only_a_space_it_owns(app, device_client, other_device_client, session_client):
    """Refused, not silently dropped: a device that does not own the space gets 403 not_owner and nothing is
    stored or pushed; the owner and the browser may tag it."""
    class Rec:
        payloads = []

        def notify_payload(self, p):
            self.payloads.append(p)
    app.state.pusher = Rec()
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    r = _send(other_device_client, "human", "", space=SPACE)
    assert r.status_code == 403 and r.json()["error"] == "not_owner"
    assert _ids(session_client) == [] and Rec.payloads == []
    device_client.put(f"/api/spaces/{SPACE}/notify", json={"messages": True})     # messages without a ticket notify
    assert _send(device_client, "human", "", space=SPACE).status_code == 201
    assert _send(session_client, "project", "proj", space=SPACE).status_code == 201
    assert [p["s"] for p in Rec.payloads] == [SPACE]


def test_a_ticket_tag_from_a_non_owner_is_refused(device_client, other_device_client):
    import hashlib
    from tests.helpers.tickets import fake_dek
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    tix = device_client.put(f"/api/mirrors/{new_uuid()}", json={
        "space": SPACE, "mirror_rev": 1, "schema_version": "1.0.0", "status": "open", "priority": "normal",
        "needs": None, "open_questions": 0, "key_version": 1, "wrapped_dek": fake_dek(), "enc_content": fake_env(90),
        "event_uuid": hashlib.sha256(b"y").hexdigest()[:32]}).json()["id"]
    r = _send(other_device_client, "human", "", space=SPACE, ticket=tix)
    assert r.status_code == 403 and r.json()["error"] == "not_owner"


def test_device_recipient_may_be_a_name_and_unknown_ones_are_refused(device_client, other_device, other_device_client):
    assert _send(device_client, "device", other_device.name).status_code == 201     # by name
    got = other_device_client.get("/api/messages", params={"after": 0, "wait": 0}).json()["messages"]
    assert [m["to_id"] for m in got] == [other_device.id]                          # stored under the id
    r = _send(device_client, "device", "dev_nonexistent")
    assert r.status_code == 404 and r.json()["error"] == "unknown_device"
    assert _send(device_client, "device", "no-such-name").json()["error"] == "unknown_device"
