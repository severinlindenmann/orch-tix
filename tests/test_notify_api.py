"""Per-ticket phone notifications (default OFF), enforced on the server: a push for a mirrored ticket goes out only while
its `notify` is on; an agent message follows its ticket, or the space's "messages without a ticket" setting; a workspace
join request always notifies. The phone sets the switch (a browser session), the owner's desktop sets it through the
mirror push and merges the phone's changes."""
import hashlib

import pytest

from tests.helpers.tickets import device_client, fake_dek, fake_env, new_uuid, other_device, other_device_client  # noqa: F401

SPACE = "d" * 32
DEK = fake_dek()


class Recorder:
    def __init__(self):
        self.payloads = []

    def notify(self, ticket, event):
        raise AssertionError("v1 push for a mirror")

    def notify_payload(self, payload):
        self.payloads.append(payload)


@pytest.fixture
def pushes(app):
    app.state.pusher = Recorder()
    return app.state.pusher.payloads


@pytest.fixture
def space(device_client):
    assert device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()}).status_code == 201
    return SPACE


def _put(dc, u, rev, needs=None, oq=0, **extra):
    return dc.put(f"/api/mirrors/{u}", json={
        "space": SPACE, "mirror_rev": rev, "schema_version": "1.0.0", "status": "waiting", "priority": "normal",
        "needs": needs, "open_questions": oq, "key_version": 1, "wrapped_dek": DEK, "enc_content": fake_env(90),
        "event_uuid": hashlib.sha256(f"{u}{rev}".encode()).hexdigest()[:32], **extra})


def _msg(dc, **kw):
    return dc.post("/api/messages", json={"uuid": new_uuid(), "to_kind": "human", "to_id": "", "kind": "text",
                                          "key_version": 1, "enc_body": fake_env(), **kw})


def _row(session_client, tix):
    return session_client.get(f"/api/mirrors/{tix}").json()


# --- tickets ---------------------------------------------------------------------------------------------------

def test_a_mirror_starts_with_notifications_off_and_pushes_nothing(space, device_client, session_client, pushes):
    tix = _put(device_client, new_uuid(), 1, "question", 1).json()["id"]
    row = _row(session_client, tix)
    assert row["notify"] is False and row["notify_rev"] == 0
    assert pushes == []                                       # still on the phone (listed), just quiet
    assert session_client.get("/api/mirrors").json()["mirrors"][0]["id"] == tix


def test_a_mirror_with_notify_on_pushes_and_off_stays_quiet(space, device_client, pushes):
    u = new_uuid()
    tix = _put(device_client, u, 1, "question", 1, notify=True, notify_seen=0).json()["id"]
    assert [p["k"] for p in pushes] == ["question"] and pushes[0]["t"] == tix
    quiet = _put(device_client, new_uuid(), 1, "approval", notify=False).json()["id"]
    assert [p["t"] for p in pushes] == [tix] and quiet != tix


def test_turning_it_on_in_the_same_push_that_needs_the_human_pushes(space, device_client, pushes):
    u = new_uuid()
    _put(device_client, u, 1, None, notify=False)
    _put(device_client, u, 2, "question", 1, notify=True, notify_seen=0)
    assert [p["k"] for p in pushes] == ["question"]


def test_a_push_without_notify_leaves_the_switch_alone(space, device_client, session_client, pushes):
    u = new_uuid()
    tix = _put(device_client, u, 1, None, notify=True).json()["id"]
    _put(device_client, u, 2, "question", 1)                  # an older addon: no notify field
    assert _row(session_client, tix)["notify"] is True and [p["k"] for p in pushes] == ["question"]


def test_a_bad_notify_value_is_400(space, device_client):
    assert _put(device_client, new_uuid(), 1, notify="yes").status_code == 400
    assert _put(device_client, new_uuid(), 1, notify=True, notify_seen=-1).status_code == 400


def test_the_needs_clear_is_sent_only_for_a_ticket_that_notifies(space, device_client, pushes):
    quiet, loud = new_uuid(), new_uuid()
    _put(device_client, quiet, 1, "question", 1, notify=False)
    _put(device_client, quiet, 2, None, notify=False)
    assert pushes == []
    tix = _put(device_client, loud, 1, "question", 1, notify=True).json()["id"]
    _put(device_client, loud, 2, None, notify=True)
    assert [p["k"] for p in pushes] == ["question", "clear"] and pushes[-1]["t"] == tix


def test_the_phone_switch_round_trip(space, device_client, session_client, pushes):
    u = new_uuid()
    tix = _put(device_client, u, 1, "question", 1, notify=False).json()["id"]
    r = session_client.put(f"/api/mirrors/{tix}/notify", json={"on": True})
    assert r.status_code == 200 and r.json()["notify"] is True and r.json()["notify_rev"] == 1
    assert _row(session_client, tix)["notify"] is True
    # the desktop reads it back with the cheap listing
    state = device_client.get(f"/api/spaces/{SPACE}/notify").json()
    assert state["mirrors"] == [{"id": tix, "uuid": u, "notify": True, "notify_rev": 1}] and state["messages"] is False
    # the next push (needs changes) now notifies, even from a desktop that still says off (it has not merged rev 1)
    _put(device_client, u, 2, "approval", 0, notify=False, notify_seen=0)
    assert _row(session_client, tix)["notify"] is True and pushes[-1]["k"] == "approval"
    # once the desktop reports it merged rev 1, its value counts again
    _put(device_client, u, 3, "approval", 0, notify=False, notify_seen=1)
    assert _row(session_client, tix)["notify"] is False


def test_the_phone_turning_it_off_withdraws_the_shown_push(space, device_client, session_client, pushes):
    tix = _put(device_client, new_uuid(), 1, "question", 1, notify=True).json()["id"]
    session_client.put(f"/api/mirrors/{tix}/notify", json={"on": False})
    assert pushes[-1]["k"] == "clear" and pushes[-1]["t"] == tix
    n = len(pushes)
    session_client.put(f"/api/mirrors/{tix}/notify", json={"on": False})      # no change: nothing sent
    assert len(pushes) == n


def test_only_the_browser_sets_the_phone_switch(space, device_client, session_client):
    tix = _put(device_client, new_uuid(), 1, None).json()["id"]
    assert device_client.put(f"/api/mirrors/{tix}/notify", json={"on": True}).status_code == 403   # an agent's credential
    assert session_client.put(f"/api/mirrors/{tix}/notify", json={"on": "yes"}).status_code == 400
    assert session_client.put("/api/mirrors/TIX-99999/notify", json={"on": True}).status_code == 404
    assert _row(session_client, tix)["notify"] is False


def test_a_cross_origin_phone_switch_is_refused(space, device_client, session_client):
    tix = _put(device_client, new_uuid(), 1, None).json()["id"]
    r = session_client.put(f"/api/mirrors/{tix}/notify", json={"on": True}, headers={"Origin": "https://evil.example"})
    assert r.status_code in (400, 403) and _row(session_client, tix)["notify"] is False


# --- messages --------------------------------------------------------------------------------------------------

def test_a_message_without_a_ticket_is_quiet_by_default(space, device_client, session_client, pushes):
    assert _msg(device_client, space=SPACE).status_code == 201
    assert pushes == []
    assert session_client.get("/api/messages").status_code == 200       # still in the app


def test_a_message_without_a_ticket_follows_the_space_setting(space, device_client, pushes):
    r = device_client.put(f"/api/spaces/{SPACE}/notify", json={"messages": True})
    assert r.status_code == 200 and r.json()["notify_messages"] is True
    _msg(device_client, space=SPACE)
    assert [p["k"] for p in pushes] == ["message"]
    device_client.put(f"/api/spaces/{SPACE}/notify", json={"messages": False})
    n = len(pushes)
    _msg(device_client, space=SPACE, uuid=new_uuid())
    assert len(pushes) == n


def test_a_message_that_belongs_to_no_space_is_quiet(space, device_client, pushes):
    device_client.put(f"/api/spaces/{SPACE}/notify", json={"messages": True})
    _msg(device_client)
    assert pushes == []


def test_a_ticketed_message_follows_its_ticket_not_the_space_setting(space, device_client, pushes):
    quiet = _put(device_client, new_uuid(), 1, None, notify=False).json()["id"]
    loud = _put(device_client, new_uuid(), 1, None, notify=True).json()["id"]
    device_client.put(f"/api/spaces/{SPACE}/notify", json={"messages": True})   # the space setting does not apply
    _msg(device_client, space=SPACE, ticket=quiet)
    assert pushes == []
    _msg(device_client, space=SPACE, ticket=loud)
    assert [(p["k"], p["t"]) for p in pushes] == [("message", loud)]


def test_a_suppressed_message_does_not_use_up_the_minute(frozen_clock, space, device_client, pushes):
    from datetime import datetime, timezone
    frozen_clock(datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc))
    _msg(device_client, space=SPACE)                                    # off: no push, no window consumed
    device_client.put(f"/api/spaces/{SPACE}/notify", json={"messages": True})
    _msg(device_client, space=SPACE)
    assert [p["k"] for p in pushes] == ["message"]


def test_only_the_owner_sets_the_message_setting(space, other_device_client, session_client):
    assert other_device_client.put(f"/api/spaces/{SPACE}/notify", json={"messages": True}).status_code == 403
    assert session_client.put(f"/api/spaces/{SPACE}/notify", json={"messages": True}).status_code == 403
    assert [s["notify_messages"] for s in session_client.get("/api/spaces").json()["spaces"]] == [False]


def test_reading_a_quiet_message_sends_no_clear(space, device_client, session_client, pushes):
    mid = _msg(device_client, space=SPACE).json()["id"]
    session_client.post(f"/api/messages/{mid}/ack")
    assert pushes == []


# --- join requests ---------------------------------------------------------------------------------------------

def test_a_join_request_always_notifies(space, other_device_client, pushes):
    r = other_device_client.post(f"/api/spaces/{SPACE}/join")
    assert r.status_code == 202
    assert [p["k"] for p in pushes] == ["join"]                         # nothing is switched on anywhere


# --- migration -------------------------------------------------------------------------------------------------

NOTIFY_MIGRATION = 13   # 010 presence, 011 push health, 012 decision origin come first


def test_the_migration_turns_existing_mirrors_and_spaces_off(tmp_path, monkeypatch):
    import fileshare.db as db
    old = tmp_path / "m"
    old.mkdir()
    for p in sorted(db.MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        if int(p.name[:3]) < NOTIFY_MIGRATION:      # every migration before the notify one (others may sit between)
            (old / p.name).write_text(p.read_text())
    conn = db.connect(tmp_path / "old.db")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", old)
    assert db.migrate(conn) < NOTIFY_MIGRATION
    conn.execute("PRAGMA foreign_keys=OFF")        # the device a real row points at is not what this test is about
    conn.execute("INSERT INTO spaces (id, owner_device, key_version, enc_label, created_at) VALUES ('s1','d1',1,'x','2026-01-01')")
    conn.execute("INSERT INTO tickets (uuid, key_version, status, project, created_by_name, created_at, updated_at, mode, space_id)"
                 " VALUES ('u1', 1, 'waiting', 'p', 'n', '2026-01-01', '2026-01-01', 'mirror', 's1')")
    monkeypatch.undo()
    assert db.migrate(conn) >= NOTIFY_MIGRATION
    row = conn.execute("SELECT notify, notify_phone_rev FROM tickets").fetchone()
    assert (row["notify"], row["notify_phone_rev"]) == (0, 0)
    assert conn.execute("SELECT notify_messages FROM spaces").fetchone()[0] == 0
