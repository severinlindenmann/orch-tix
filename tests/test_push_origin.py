"""QA #55: the clear says where the decision came from, and the deciding phone's subscription gets no copy."""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from fileshare import push as push_module
from fileshare.db import connect
from tests.helpers.tickets import device_client, fake_env, new_uuid, other_device, other_device_client  # noqa: F401
from tests.test_push_notifications import _decide, _space   # noqa: F401
from tests.test_push_notifications import DEK, SPACE, _put

T0 = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def _put_via(dc, u, rev, via):
    return dc.put(f"/api/mirrors/{u}", json={
        "space": SPACE, "mirror_rev": rev, "schema_version": "1.0.0", "status": "waiting", "priority": "normal",
        "needs": None, "open_questions": 0, "key_version": 1, "wrapped_dek": DEK, "enc_content": fake_env(90),
        "event_uuid": f"{rev:032x}", **({"decided_via": via} if via else {})})


class Rec:
    def __init__(self):
        self.sent = []

    def notify(self, *a):
        raise AssertionError

    def notify_payload(self, payload, exclude_sessions=None):
        self.sent.append((payload, set(exclude_sessions or ())))


@pytest.fixture
def rec(app):
    app.state.pusher = Rec()
    return app.state.pusher.sent


def _owner_hash(settings):
    c = connect(settings.db_path)
    try:
        return c.execute("SELECT id_hash FROM sessions").fetchone()["id_hash"]
    finally:
        c.close()


def test_a_clear_after_a_phone_decision_says_phone_and_skips_that_session(frozen_clock, settings, device_client,
                                                                            session_client, rec):
    frozen_clock(T0)
    _space(device_client)
    u = new_uuid()
    tix = _put(device_client, u, 1, "question", 1).json()["id"]
    assert _decide(session_client, tix).status_code == 201
    frozen_clock(T0 + timedelta(seconds=30))
    assert _put_via(device_client, u, 2, "phone").status_code == 200    # Mission Control applied the phone's answer
    payload, excluded = rec[-1]
    assert payload["k"] == "clear" and payload["via"] == "phone"
    assert excluded == {_owner_hash(settings)}


def test_a_clear_after_a_desktop_answer_has_no_origin_and_goes_to_everyone(frozen_clock, device_client, rec):
    frozen_clock(T0)
    _space(device_client)
    u = new_uuid()
    _put(device_client, u, 1, "question", 1)
    _put(device_client, u, 2, None)
    payload, excluded = rec[-1]
    assert payload["k"] == "clear" and "via" not in payload and excluded == set()


def test_a_pending_phone_decision_alone_is_not_enough_the_desktop_must_say_so(frozen_clock, device_client, session_client, rec):
    frozen_clock(T0)
    _space(device_client)
    u = new_uuid()
    tix = _put(device_client, u, 1, "question", 1).json()["id"]
    assert _decide(session_client, tix).status_code == 201
    _put(device_client, u, 2, None)                           # the desktop answered locally while the phone's waited
    payload, excluded = rec[-1]
    assert "via" not in payload and excluded == set()


def test_an_old_phone_decision_excludes_nobody_even_when_the_desktop_says_phone(frozen_clock, device_client, session_client, rec):
    frozen_clock(T0)
    _space(device_client)
    u = new_uuid()
    tix = _put(device_client, u, 1, "question", 1).json()["id"]
    assert _decide(session_client, tix).status_code == 201
    frozen_clock(T0 + timedelta(minutes=20))                  # older than the 15 minute window
    _put_via(device_client, u, 2, "phone")
    payload, excluded = rec[-1]
    assert payload["via"] == "phone" and excluded == set()


def test_decided_via_must_be_phone_or_absent(device_client):
    _space(device_client)
    assert _put_via(device_client, new_uuid(), 1, "tablet").status_code == 400


def test_a_join_decision_names_the_outcome_and_skips_the_deciding_session(frozen_clock, settings, device_client,
                                                                            other_device_client, session_client, rec):
    frozen_clock(T0)
    _space(device_client)
    r = other_device_client.post(f"/api/spaces/{SPACE}/join")
    assert r.status_code == 202, r.text
    rid = r.json()["request"]["id"]
    assert session_client.post(f"/api/spaces/{SPACE}/join-requests/{rid}/approve").json()["status"] == "approved"
    payload, excluded = rec[-1]
    assert payload["k"] == "clear" and payload["w"] == "join" and payload["r"] == "approved"
    assert excluded == {_owner_hash(settings)}


@pytest.mark.real_pusher
def test_the_excluded_session_gets_no_push(app, settings, monkeypatch):
    c = connect(settings.db_path)
    for sid, ep, sess in (("psh_a", "https://fcm.googleapis.com/a", "H1"), ("psh_b", "https://fcm.googleapis.com/b", "H2")):
        c.execute("INSERT INTO push_subs (id, session_name, endpoint, p256dh, auth, created_at, session_hash) VALUES (?,?,?,?,?,?,?)",
                  (sid, "x", ep, "p", "a", "2026-10-05T00:00:00Z", sess))
    push_module.ensure_vapid(c)
    c.close()
    seen = []

    def fake_run(cmd, input, capture_output, text, timeout):
        seen.append(json.loads(input))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"gone": [], "failed": []}))

    monkeypatch.setattr(push_module.subprocess, "run", fake_run)
    push_module.SubprocessPusher(settings, settings.db_path)._deliver_payload('{"v":2,"k":"clear"}', None, 86400, True, frozenset({"H1"}))
    assert [s["id"] for s in seen[0]["subs"]] == ["psh_b"]
