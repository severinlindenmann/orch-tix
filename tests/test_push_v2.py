import hashlib

import pytest

from tests.helpers.tickets import device_client, fake_dek, fake_env, new_uuid, other_device, other_device_client  # noqa: F401

SPACE = "e" * 32
DEK = fake_dek()


class Recorder:
    def __init__(self):
        self.payloads = []

    def notify(self, ticket, event):
        raise AssertionError("v1 push for a mirror")

    def notify_payload(self, payload):
        self.payloads.append(payload)


@pytest.fixture
def pushes(app, monkeypatch):
    # These tests are about delivery (windows, counts, clears), so every ticket and message may notify here; the
    # switches themselves are tested in test_notify_api.py.
    from fileshare import messages
    monkeypatch.setattr(messages, "message_push_allowed", lambda *a, **k: True)
    monkeypatch.setattr(messages, "message_clear_wanted", lambda *a, **k: True)
    app.state.pusher = Recorder()
    return app.state.pusher.payloads


def _put(dc, u, rev, needs, oq=0):
    return dc.put(f"/api/mirrors/{u}", json={
        "space": SPACE, "mirror_rev": rev, "schema_version": "1.0.0", "status": "waiting", "priority": "normal",
        "needs": needs, "notify": True, "open_questions": oq, "key_version": 1, "wrapped_dek": DEK, "enc_content": fake_env(90),
        "event_uuid": hashlib.sha256(f"{u}{rev}".encode()).hexdigest()[:32]})


def test_needs_transitions_push_v2_only(frozen_clock, device_client, pushes):
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    frozen_clock(t0)
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    u = new_uuid()
    _put(device_client, u, 1, None)
    assert pushes == []
    tix = _put(device_client, u, 2, "question", 2).json()["id"]
    assert pushes == [{"v": 2, "s": SPACE, "t": tix, "k": "question", "n": 2, "c": 1, "cs": 1}]
    _put(device_client, u, 3, "question", 2)
    assert len(pushes) == 1                                  # same kind: nothing new
    frozen_clock(t0 + timedelta(seconds=61))                 # past the space's push window (feedback round B)
    _put(device_client, u, 4, "approval")
    assert pushes[-1]["k"] == "approval"
    _put(device_client, u, 5, None)
    assert pushes[-1] == {"v": 2, "s": SPACE, "t": tix, "k": "clear", "n": 0, "c": 0, "cs": 0}


def test_payload_never_holds_text(device_client, pushes):
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    _put(device_client, new_uuid(), 1, "verdict")
    assert set(pushes[0]) == {"v", "s", "t", "k", "n", "c", "cs"}


def test_message_to_human_pushes(device_client, pushes):
    r = device_client.post("/api/messages", json={"uuid": new_uuid(), "to_kind": "human", "to_id": "", "kind": "text",
                                                  "key_version": 1, "enc_body": fake_env()})
    assert r.status_code == 201 and pushes[-1]["k"] == "message"


# --- beyond the brief ---------------------------------------------------------------------------------------

def test_unlink_of_a_needing_mirror_pushes_clear(device_client, pushes):
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    u = new_uuid()
    tix = _put(device_client, u, 1, "question", 1).json()["id"]
    assert device_client.delete(f"/api/mirrors/{u}", params={"space": SPACE}).status_code == 204
    assert pushes[-1] == {"v": 2, "s": SPACE, "t": tix, "k": "clear", "n": 0, "c": 0, "cs": 0}


def test_count_includes_open_human_messages(device_client, session_client, pushes):
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    mid = device_client.post("/api/messages", json={"uuid": new_uuid(), "to_kind": "human", "to_id": "",
                                                    "kind": "text", "key_version": 1, "enc_body": fake_env()}).json()["id"]
    assert pushes[-1] == {"v": 2, "s": "", "t": "", "k": "message", "n": 1, "c": 1}
    _put(device_client, new_uuid(), 1, "question", 1)
    assert pushes[-1]["c"] == 2
    session_client.post(f"/api/messages/{mid}/ack")
    assert pushes[-1] == {"v": 2, "s": "", "t": "", "k": "clear", "w": "message", "n": 0, "c": 1}   # read: withdrawn
    _put(device_client, new_uuid(), 1, "approval")


def test_message_push_names_space_and_ticket_only(device_client, pushes):
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    tix = _put(device_client, new_uuid(), 1, None).json()["id"]
    device_client.post("/api/messages", json={"uuid": new_uuid(), "to_kind": "human", "to_id": "", "kind": "question",
                                              "key_version": 1, "enc_body": fake_env(), "space": SPACE, "ticket": tix})
    assert pushes[-1] == {"v": 2, "s": SPACE, "t": tix, "k": "message", "n": 1, "c": 1}


def test_a_failing_pusher_never_fails_a_mirror_write(app, device_client):
    class Boom:
        def notify_payload(self, payload):
            raise RuntimeError("push down")

    app.state.pusher = Boom()
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    assert _put(device_client, new_uuid(), 1, "question", 1).status_code == 200


def test_subprocess_pusher_sends_the_v2_payload_compact(app, settings, monkeypatch):
    from fileshare import push as push_module
    sent = []
    monkeypatch.setattr(push_module.SubprocessPusher, "_deliver_payload", lambda self, p, topic=None, *rest: sent.append((p, topic)))
    monkeypatch.setattr(push_module.SubprocessPusher, "_submit", lambda self, fn, *a: fn(*a))
    push_module.SubprocessPusher(settings, settings.db_path).notify_payload(
        {"v": 2, "s": SPACE, "t": "TIX-1", "k": "question", "n": 1, "c": 1})
    assert sent == [('{"v":2,"s":"' + SPACE + '","t":"TIX-1","k":"question","n":1,"c":1}', push_module.topic_for(
        {"v": 2, "s": SPACE, "t": "TIX-1", "k": "question"}))]
    assert len(sent[0][1]) <= 32


def test_null_pusher_has_notify_payload():
    from fileshare.tickets import NullPusher
    assert NullPusher().notify_payload({"v": 2}) is None


def test_the_default_test_app_has_a_null_pusher(app):
    """Batch-2 review: the suite never starts SubprocessPusher threads unless a test asks for it."""
    from fileshare.tickets import NullPusher
    assert isinstance(app.state.pusher, NullPusher)


@pytest.mark.real_pusher
def test_real_pusher_marker_keeps_the_subprocess_pusher(app):
    from fileshare import push as push_module
    assert isinstance(app.state.pusher, push_module.SubprocessPusher)


# --- batch-2 review: message flood -------------------------------------------------------------------------

def _human_msg(dc, **kw):
    return dc.post("/api/messages", json={"uuid": new_uuid(), "to_kind": "human", "to_id": "", "kind": "text",
                                          "key_version": 1, "enc_body": fake_env(), **kw})


def test_a_flood_of_human_messages_gives_at_most_two_pushes(device_client, pushes):
    for _ in range(50):
        assert _human_msg(device_client).status_code == 201
    msg_pushes = [p for p in pushes if p["k"] == "message"]
    assert 1 <= len(msg_pushes) <= 2


def test_human_message_pushes_coalesce_per_sender_per_minute(frozen_clock, device_client, other_device_client,
                                                             pushes):
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    frozen_clock(t0)
    for _ in range(3):
        _human_msg(device_client)
    assert [p["n"] for p in pushes if p["k"] == "message"] == [1]
    _human_msg(other_device_client)                           # another sender has its own minute
    assert [p["n"] for p in pushes if p["k"] == "message"] == [1, 1]
    frozen_clock(t0 + timedelta(seconds=61))
    _human_msg(device_client)
    msg = [p for p in pushes if p["k"] == "message"]
    assert len(msg) == 3 and msg[-1]["n"] == 4 and msg[-1]["c"] == 5      # n = this sender's unread messages


def test_messages_are_limited_per_device(app, device_client, other_device_client):
    from fileshare import messages
    assert messages.MESSAGES_PER_HOUR == 120
    for _ in range(messages.MESSAGES_PER_HOUR):
        assert _human_msg(device_client).status_code == 201
    r = _human_msg(device_client)
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    assert _human_msg(other_device_client).status_code == 201             # per device


def test_a_failed_message_spends_no_slot(app, device_client):
    from fileshare.security import WindowLimiter
    app.state.message_limiter = WindowLimiter(1, 3600)
    u = new_uuid()
    assert _human_msg(device_client, uuid=u).status_code == 201
    app.state.message_limiter = WindowLimiter(1, 3600)
    assert _human_msg(device_client, uuid=u).status_code == 409             # duplicate: refunded
    assert _human_msg(device_client).status_code == 201
    assert _human_msg(device_client).status_code == 429


def test_messages_held_in_the_window_get_a_push_when_it_ends(frozen_clock, app, device_client, pushes):
    """Batch 3 review m1: not only the leading edge. Messages held back inside the minute are pushed
    once when it ends (n = the sender's unread count), even if no later message arrives."""
    from datetime import datetime, timedelta, timezone
    timers = []
    app.state.message_push_gate.timer = lambda delay, fn: timers.append((delay, fn))
    t0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    frozen_clock(t0)
    _human_msg(device_client)                                   # leading edge: pushed now
    frozen_clock(t0 + timedelta(seconds=10))
    _human_msg(device_client)
    _human_msg(device_client)                                   # held; one trailing timer, not two
    assert [p["n"] for p in pushes if p["k"] == "message"] == [1]
    assert len(timers) == 1 and timers[0][0] == pytest.approx(50, abs=1)
    frozen_clock(t0 + timedelta(seconds=60))
    timers[0][1]()                                              # the window ends
    assert [p["n"] for p in pushes if p["k"] == "message"] == [1, 3]
    frozen_clock(t0 + timedelta(seconds=61))
    _human_msg(device_client)                                   # the trailing push opened a new window
    assert [p["n"] for p in pushes if p["k"] == "message"] == [1, 3]
    assert len(timers) == 2


def test_a_trailing_push_is_skipped_when_everything_was_read(frozen_clock, app, device_client, session_client, pushes):
    from datetime import datetime, timedelta, timezone
    timers = []
    app.state.message_push_gate.timer = lambda delay, fn: timers.append((delay, fn))
    t0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    frozen_clock(t0)
    _human_msg(device_client)
    frozen_clock(t0 + timedelta(seconds=5))
    _human_msg(device_client)
    for m in session_client.get("/api/messages").json()["messages"]:
        assert session_client.post(f"/api/messages/{m['id']}/ack").status_code == 204
    frozen_clock(t0 + timedelta(seconds=60))
    timers[0][1]()
    assert [p["n"] for p in pushes if p["k"] == "message"] == [1]


def test_the_trailing_push_names_the_latest_held_message(frozen_clock, app, device_client, pushes):
    """Task 9 review: the push at the end of the window carries the newest held message's space and ticket,
    not the one that armed the timer."""
    from datetime import datetime, timedelta, timezone
    timers = []
    app.state.message_push_gate.timer = lambda delay, fn: timers.append((delay, fn))
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    tix = _put(device_client, new_uuid(), 1, None).json()["id"]
    t0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    frozen_clock(t0)
    _human_msg(device_client)                                   # leading edge, no space
    frozen_clock(t0 + timedelta(seconds=10))
    _human_msg(device_client)                                   # held: arms the timer, no space
    frozen_clock(t0 + timedelta(seconds=20))
    assert _human_msg(device_client, space=SPACE, ticket=tix).status_code == 201   # held, the newest
    assert len(timers) == 1
    frozen_clock(t0 + timedelta(seconds=60))
    timers[0][1]()
    last = [p for p in pushes if p["k"] == "message"][-1]
    assert (last["s"], last["t"], last["n"]) == (SPACE, tix, 3)


# ---- feedback round B: at most one needs push per workspace and minute; the rest folded into one summary ----

def _burst(device_client, k):
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    return [_put(device_client, new_uuid(), 1, "question", 1).json()["id"] for _ in range(k)]


def test_ten_tickets_needing_you_at_once_give_one_push_then_one_summary(frozen_clock, app, device_client, pushes):
    from datetime import datetime, timedelta, timezone
    timers = []
    app.state.needs_push_gate.timer = lambda delay, fn: timers.append((delay, fn))
    t0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    frozen_clock(t0)
    tix = _burst(device_client, 10)
    assert [p["k"] for p in pushes] == ["question"] and pushes[0]["t"] == tix[0]     # the first at once
    assert len(timers) == 1 and timers[0][0] == pytest.approx(60, abs=1)            # one trailing timer
    frozen_clock(t0 + timedelta(seconds=60))
    timers[0][1]()
    assert len(pushes) == 2
    assert pushes[1] == {"v": 2, "s": SPACE, "t": "", "k": "batch", "n": 9, "c": 10, "cs": 10, "ts": tix[1:]}


def test_a_held_ticket_handled_meanwhile_leaves_the_summary(frozen_clock, app, device_client, pushes):
    from datetime import datetime, timedelta, timezone
    timers = []
    app.state.needs_push_gate.timer = lambda delay, fn: timers.append((delay, fn))
    t0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    frozen_clock(t0)
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    u1, u2, u3 = new_uuid(), new_uuid(), new_uuid()
    _put(device_client, u1, 1, "question", 1)
    t2 = _put(device_client, u2, 1, "approval").json()["id"]
    _put(device_client, u3, 1, "question", 1)
    _put(device_client, u3, 2, None)                          # handled on the desktop while still held: the phone
    assert [p["k"] for p in pushes] == ["question"]           # never saw it, so no "Handled on desktop" either
    frozen_clock(t0 + timedelta(seconds=60))
    timers[0][1]()
    # one held ticket left: its own push, not a summary
    assert pushes[-1] == {"v": 2, "s": SPACE, "t": t2, "k": "approval", "n": 0, "c": 2, "cs": 2}


def test_the_window_is_per_workspace(frozen_clock, app, device_client, pushes):
    other = "d" * 32
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    device_client.post("/api/spaces", json={"id": other, "key_version": 1, "enc_label": fake_env()})
    _put(device_client, new_uuid(), 1, "question", 1)
    r = device_client.put(f"/api/mirrors/{new_uuid()}", json={
        "space": other, "mirror_rev": 1, "schema_version": "1.0.0", "status": "waiting", "priority": "normal",
        "needs": "question", "notify": True, "open_questions": 1, "key_version": 1, "wrapped_dek": DEK, "enc_content": fake_env(90),
        "event_uuid": "f" * 32})
    assert r.status_code in (200, 201)
    assert [p["s"] for p in pushes] == [SPACE, other]
