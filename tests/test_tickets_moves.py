"""The T2 move matrix: allowed and refused moves for S, D and H; claims, takeover, release, pushes."""
from datetime import datetime, timedelta, timezone

import pytest

from tests.helpers.tickets import (claim, create, device_client, get, new_uuid,  # noqa: F401
                                   other_device, other_device_client, post_event)

T0 = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


class FakePusher:
    def __init__(self):
        self.calls = []

    def notify(self, ticket, event):
        self.calls.append((ticket["id"], ticket["status"], event["kind"]))


@pytest.fixture
def pusher(app):
    p = FakePusher()
    app.state.pusher = p
    return p


def reach(s, d, status):
    """A ticket in `status`, created by S; from in-progress on, claimed by device client d. -> (ticket, token)"""
    if status in ("backlog", "open"):
        return create(s, status=status), None
    t = create(s, status="open")
    tok = claim(d, t["id"])
    if status == "waiting":
        post_event(d, t["id"], "question", claim=tok, question_count=2, status_to="waiting")
    elif status in ("testing", "done"):
        post_event(d, t["id"], "test", claim=tok, passed=True, manual=False)
        if status == "done":
            post_event(d, t["id"], "status", claim=tok, status_to="done")
    assert get(s, t["id"])["status"] == status
    return t, tok


# (from, actor, kind, request fields, expected: a status, or "error_code:http")
MATRIX = [
    # backlog <-> open, S and D
    ("backlog", "D", "status", {"status_to": "open"}, "open"),
    ("open", "D", "status", {"status_to": "backlog"}, "backlog"),
    ("backlog", "S", "status", {"status_to": "open"}, "open"),
    ("open", "S", "status", {"status_to": "backlog"}, "backlog"),
    # open -> in-progress for D only through claim
    ("open", "D", "status", {"status_to": "in-progress"}, "bad_move:409"),
    ("backlog", "D", "status", {"status_to": "testing"}, "bad_move:409"),
    # in-progress -> waiting: H with question_count >= 1
    ("in-progress", "H", "question", {"question_count": 1, "status_to": "waiting"}, "waiting"),
    ("in-progress", "H", "question", {"question_count": 20}, "waiting"),
    ("in-progress", "H", "question", {}, "bad_question:400"),
    ("in-progress", "H", "question", {"question_count": 0}, "bad_question:400"),
    ("in-progress", "H", "question", {"question_count": 21}, "bad_question:400"),
    ("in-progress", "H", "question", {"question_count": True}, "bad_question:400"),
    ("in-progress", "H", "question", {"question_count": 1, "status_to": "testing"}, "bad_move:409"),
    ("in-progress", "D", "question", {"question_count": 1}, "bad_move:409"),
    ("in-progress", "S", "question", {"question_count": 1}, "bad_move:409"),
    ("waiting", "H", "question", {"question_count": 1}, "bad_move:409"),
    ("in-progress", "H", "status", {"status_to": "waiting"}, "bad_move:409"),
    # tests
    ("in-progress", "H", "test", {"passed": False, "manual": False}, "in-progress"),
    ("in-progress", "H", "test", {"passed": True, "manual": False}, "testing"),
    ("in-progress", "H", "test", {"passed": False, "manual": True}, "testing"),
    ("in-progress", "H", "test", {"passed": True, "manual": False, "status_to": "testing"}, "testing"),
    ("in-progress", "H", "test", {"passed": False, "manual": False, "status_to": "testing"}, "bad_move:409"),
    ("in-progress", "H", "test", {"passed": "yes", "manual": False}, "bad_request:400"),
    ("in-progress", "H", "test", {"passed": True, "manual": None}, "bad_request:400"),
    ("in-progress", "D", "test", {"passed": True, "manual": False}, "bad_move:409"),
    ("in-progress", "S", "test", {"passed": True, "manual": False}, "bad_move:409"),
    ("waiting", "H", "test", {"passed": True, "manual": False}, "bad_move:409"),
    ("in-progress", "H", "status", {"status_to": "testing"}, "bad_move:409"),
    # answers: S only, from waiting
    ("waiting", "S", "answer", {"status_to": "in-progress"}, "in-progress"),
    ("waiting", "S", "answer", {}, "in-progress"),
    ("waiting", "H", "answer", {}, "bad_move:409"),
    ("waiting", "D", "answer", {}, "bad_move:409"),
    ("in-progress", "S", "answer", {}, "bad_move:409"),
    ("waiting", "H", "status", {"status_to": "in-progress"}, "bad_move:409"),
    # verdicts: S only, from testing
    ("testing", "S", "verdict", {"verdict": "done"}, "done"),
    ("testing", "S", "verdict", {"verdict": "follow-up"}, "in-progress"),
    ("testing", "S", "verdict", {"verdict": "follow-up", "status_to": "done"}, "bad_move:409"),
    ("testing", "S", "verdict", {"verdict": "maybe"}, "bad_request:400"),
    ("testing", "S", "verdict", {}, "bad_request:400"),
    ("testing", "H", "verdict", {"verdict": "done"}, "bad_move:409"),
    ("testing", "D", "verdict", {"verdict": "done"}, "bad_move:409"),
    ("in-progress", "S", "verdict", {"verdict": "done"}, "bad_move:409"),
    # testing -> done by H status (then: done)
    ("testing", "H", "status", {"status_to": "done"}, "done"),
    ("testing", "D", "status", {"status_to": "done"}, "bad_move:409"),
    ("testing", "H", "status", {"status_to": "in-progress"}, "bad_move:409"),
    # done -> open: S only
    ("done", "S", "status", {"status_to": "open"}, "open"),
    ("done", "D", "status", {"status_to": "open"}, "bad_move:409"),
    # S overrides any other move; D can't
    ("waiting", "S", "status", {"status_to": "backlog"}, "backlog"),
    ("testing", "S", "status", {"status_to": "open"}, "open"),
    ("in-progress", "S", "status", {"status_to": "done"}, "done"),
    ("open", "S", "status", {"status_to": "testing"}, "testing"),
    ("done", "S", "status", {"status_to": "waiting"}, "waiting"),
    ("in-progress", "D", "status", {"status_to": "backlog"}, "bad_move:409"),
    ("waiting", "D", "status", {"status_to": "open"}, "bad_move:409"),
    ("open", "S", "status", {"status_to": "open"}, "bad_move:409"),
    ("open", "S", "status", {"status_to": "nope"}, "bad_request:400"),
    ("open", "S", "status", {}, "bad_request:400"),
    # updates never move: S and D anywhere, H while holding
    ("waiting", "S", "update", {}, "waiting"),
    ("done", "S", "update", {}, "done"),
    ("backlog", "D", "update", {}, "backlog"),
    ("testing", "D", "update", {}, "testing"),
    ("in-progress", "H", "update", {}, "in-progress"),
    ("in-progress", "H", "update", {"status_to": "waiting"}, "bad_move:409"),
]


@pytest.mark.parametrize("frm,actor,kind,fields,expect", MATRIX,
                         ids=[f"{m[0]}-{m[1]}-{m[2]}-{i}" for i, m in enumerate(MATRIX)])
def test_move_matrix(session_client, device_client, other_device_client, frm, actor, kind, fields, expect):
    t, tok = reach(session_client, device_client, frm)
    before = get(session_client, t["id"])
    if actor == "S":
        client, token = session_client, None
    elif actor == "H":
        client, token = device_client, tok
    else:   # a device that doesn't hold the claim
        client, token = other_device_client, None
    r = post_event(client, t["id"], kind, claim=token, raw=True, **fields)
    after = get(session_client, t["id"])
    if ":" in expect:
        code, http = expect.split(":")
        assert r.status_code == int(http), r.text
        assert r.json()["error"] == code
        assert after["status"] == before["status"] and after["last_seq"] == before["last_seq"]
        return
    assert r.status_code == 201, r.text
    ev = r.json()
    assert after["status"] == expect
    if expect != before["status"]:
        assert (ev["status_from"], ev["status_to"]) == (before["status"], expect)
    else:
        assert ev["status_from"] is None and ev["status_to"] is None
    assert after["last_seq"] == ev["seq"]


def test_device_cannot_answer(device_client, session_client):
    t = create(session_client, status="open")
    tok = claim(device_client, t["id"])
    post_event(device_client, t["id"], "question", status_to="waiting", question_count=1, claim=tok)
    r = post_event(device_client, t["id"], "answer", status_to="in-progress", raw=True)
    assert r.status_code == 409 and r.json()["error"] == "bad_move"
    r = post_event(device_client, t["id"], "answer", status_to="in-progress", claim=tok, raw=True)
    assert r.status_code == 409 and r.json()["error"] == "bad_move"


def test_answer_moves_waiting_back(device_client, session_client):
    t = create(session_client, status="open")
    tok = claim(device_client, t["id"])
    post_event(device_client, t["id"], "question", status_to="waiting", question_count=3, claim=tok)
    assert get(session_client, t["id"])["open_questions"] == 3
    post_event(session_client, t["id"], "answer", status_to="in-progress")
    got = get(session_client, t["id"])
    assert got["status"] == "in-progress" and got["open_questions"] == 0
    assert got["claim"]["device"]["name"] == "laptop"


def test_claim_takeover_and_claim_lost(device_client, other_device_client, session_client, other_device):
    t = create(session_client, status="open")
    tok1 = claim(device_client, t["id"])
    r = claim(other_device_client, t["id"], raw=True)
    assert r.status_code == 409 and r.json()["error"] == "claimed"
    tok2 = claim(other_device_client, t["id"], takeover=True)
    assert tok2 != tok1
    r = post_event(device_client, t["id"], "update", claim=tok1, raw=True)
    assert r.status_code == 409 and r.json()["error"] == "claim_lost"
    got = get(session_client, t["id"])
    assert got["status"] == "in-progress" and got["claim"]["device"]["id"] == other_device.id
    post_event(other_device_client, t["id"], "update", claim=tok2)
    # the lost token is refused on every endpoint that reads the header
    r = device_client.get(f"/api/tickets/{t['id']}", headers={"X-Claim-Token": tok1})
    assert r.status_code == 409 and r.json()["error"] == "claim_lost"
    r = device_client.patch(f"/api/tickets/{t['id']}", headers={"X-Claim-Token": tok1},
                            json={"rev": 1, "event_uuid": new_uuid()})
    assert r.status_code == 409 and r.json()["error"] == "claim_lost"


def test_claim_response_and_event(device_client, session_client, device):
    t = create(session_client, status="open")
    r = device_client.post(f"/api/tickets/{t['id']}/claim", json={"event_uuid": new_uuid()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["claim_token"].startswith("clm_") and len(body["claim_token"]) == 4 + 43
    assert body["ticket"]["status"] == "in-progress"
    assert body["ticket"]["claim"]["device"] == {"id": device.id, "name": device.name}
    assert body["ticket"]["claim"]["state"] == "working"
    ev = body["event"]
    assert ev["kind"] == "claim" and (ev["status_from"], ev["status_to"]) == ("open", "in-progress")
    assert ev["actor"] == {"kind": "device", "id": device.id, "name": device.name}
    assert ev["enc_body"] is None


@pytest.mark.parametrize("status", ["backlog", "done"])
def test_claim_from_backlog_or_done_is_bad_move(device_client, session_client, status):
    t, _ = reach(session_client, device_client, status)
    r = claim(device_client, t["id"], raw=True)
    assert r.status_code == 409 and r.json()["error"] == "bad_move"
    r = claim(device_client, t["id"], raw=True, takeover=True)
    assert r.status_code == 409 and r.json()["error"] == "bad_move"


@pytest.mark.parametrize("status", ["in-progress", "waiting", "testing"])
def test_takeover_keeps_the_status(device_client, other_device_client, session_client, status):
    t, _ = reach(session_client, device_client, status)
    assert claim(other_device_client, t["id"], raw=True).json()["error"] == "claimed"
    r = other_device_client.post(f"/api/tickets/{t['id']}/claim", json={"event_uuid": new_uuid(), "takeover": True})
    assert r.status_code == 200
    assert r.json()["ticket"]["status"] == status
    assert r.json()["event"]["status_to"] is None


def test_claim_after_release_needs_no_takeover(device_client, other_device_client, session_client):
    t, tok = reach(session_client, device_client, "waiting")
    assert device_client.delete(f"/api/tickets/{t['id']}/claim", headers={"X-Claim-Token": tok}).status_code == 204
    claim(other_device_client, t["id"])
    assert get(session_client, t["id"])["status"] == "waiting"


def test_session_cannot_claim(session_client):
    t = create(session_client)
    assert claim(session_client, t["id"], raw=True).status_code in (401, 403)


def test_duplicate_claim_event_uuid(device_client, session_client):
    t = create(session_client)
    u = new_uuid()
    claim(device_client, t["id"], event_uuid=u)
    r = claim(device_client, t["id"], raw=True, event_uuid=u, takeover=True)
    assert r.status_code == 409 and r.json()["error"] == "duplicate_uuid"


def test_a_retried_question_is_a_duplicate_not_a_bad_move(device_client, session_client):
    t, tok = reach(session_client, device_client, "in-progress")
    u = new_uuid()
    post_event(device_client, t["id"], "question", claim=tok, uuid=u, question_count=1)
    r = post_event(device_client, t["id"], "question", claim=tok, uuid=u, question_count=1, raw=True)
    assert r.status_code == 409 and r.json()["error"] == "duplicate_uuid"


def test_release_by_holder_and_by_session(device_client, session_client, other_device_client):
    t, tok = reach(session_client, device_client, "in-progress")
    # a device that isn't the holder can't release
    assert other_device_client.delete(f"/api/tickets/{t['id']}/claim").status_code == 403
    assert device_client.delete(f"/api/tickets/{t['id']}/claim", headers={"X-Claim-Token": tok}).status_code == 204
    got = get(session_client, t["id"])
    assert got["claim"] is None and got["status"] == "in-progress"
    assert got["events"][-1]["kind"] == "release"
    assert got["events"][-1]["actor"]["kind"] == "device"
    t2, _ = reach(session_client, device_client, "testing")
    assert session_client.delete(f"/api/tickets/{t2['id']}/claim").status_code == 204
    got = get(session_client, t2["id"])
    assert got["claim"] is None and got["status"] == "testing" and got["events"][-1]["kind"] == "release"
    assert got["events"][-1]["actor"]["kind"] == "web"


@pytest.mark.parametrize("how", ["verdict", "holder", "override"])
def test_done_releases_the_claim(device_client, session_client, how):
    t, tok = reach(session_client, device_client, "testing")
    if how == "verdict":
        post_event(session_client, t["id"], "verdict", verdict="done")
    elif how == "holder":
        post_event(device_client, t["id"], "status", claim=tok, status_to="done")
    else:
        post_event(session_client, t["id"], "status", status_to="done")
    got = get(session_client, t["id"])
    assert got["status"] == "done" and got["claim"] is None
    r = post_event(device_client, t["id"], "update", claim=tok, raw=True)
    assert r.status_code == 409 and r.json()["error"] == "claim_lost"


@pytest.mark.parametrize("how", ["verdict", "holder"])
def test_a_final_update_on_a_done_ticket_is_accepted(device_client, session_client, how):
    """tickets-SKILL.md step 7: after a done verdict (or `then: done`) the agent posts a summary. The
    claim is gone, so the update goes without a claim token, and it doesn't reopen the ticket."""
    t, tok = reach(session_client, device_client, "testing")
    if how == "verdict":
        post_event(session_client, t["id"], "verdict", verdict="done")
    else:
        post_event(device_client, t["id"], "status", claim=tok, status_to="done")
    ev = post_event(device_client, t["id"], "update")
    assert ev["kind"] == "update" and ev["actor"]["kind"] == "device"
    got = get(session_client, t["id"])
    assert got["status"] == "done" and got["claim"] is None
    assert got["events"][-1]["kind"] == "update"


def test_session_override_keeps_the_claim(device_client, session_client):
    t, tok = reach(session_client, device_client, "waiting")
    post_event(session_client, t["id"], "status", status_to="open")
    got = get(session_client, t["id"])
    assert got["status"] == "open" and got["claim"] is not None and got["opened_by"] == "browser"
    post_event(device_client, t["id"], "update", claim=tok)


def test_revoking_a_device_releases_its_claims(device_client, session_client, device):
    t1, _ = reach(session_client, device_client, "waiting")
    t2, _ = reach(session_client, device_client, "in-progress")
    assert session_client.delete(f"/api/devices/{device.id}").status_code == 204
    for t, status in ((t1, "waiting"), (t2, "in-progress")):
        got = get(session_client, t["id"])
        assert got["claim"] is None and got["status"] == status
        ev = got["events"][-1]
        assert ev["kind"] == "release" and ev["actor"]["kind"] == "web" and ev["status_to"] is None


def test_self_revoke_releases_claims(device_client, session_client):
    t, _ = reach(session_client, device_client, "in-progress")
    assert device_client.delete("/api/devices/self").status_code == 204
    got = get(session_client, t["id"])
    assert got["claim"] is None and got["events"][-1]["kind"] == "release"


def test_claim_state(frozen_clock, device_client, session_client, settings):
    frozen_clock(T0)
    t, tok = reach(session_client, device_client, "in-progress")
    assert get(session_client, t["id"])["claim"]["state"] == "working"
    frozen_clock(T0 + timedelta(minutes=29))
    assert get(session_client, t["id"])["claim"]["state"] == "working"
    frozen_clock(T0 + timedelta(minutes=31))
    assert get(session_client, t["id"])["claim"]["state"] == "gone"
    # any holder request refreshes seen_at
    r = device_client.get(f"/api/tickets/{t['id']}", headers={"X-Claim-Token": tok})
    assert r.status_code == 200
    c = get(session_client, t["id"])["claim"]
    assert c["state"] == "working" and c["seen_at"] == "2026-09-25T12:31:00Z" and c["poll_at"] is None
    # a poll heartbeat under 90 s old means listening (Task 3 writes claim_poll_at)
    from fileshare.db import connect
    conn = connect(settings.db_path)
    try:
        conn.execute("UPDATE tickets SET claim_poll_at=? WHERE n=?", ("2026-09-25T12:30:00Z", t["n"]))
    finally:
        conn.close()
    assert get(session_client, t["id"])["claim"]["state"] == "listening"
    frozen_clock(T0 + timedelta(minutes=31, seconds=30))
    assert get(session_client, t["id"])["claim"]["state"] == "working"


def test_pushes_fire_only_on_waiting_testing_and_holder_done(pusher, device_client, session_client):
    t, tok = reach(session_client, device_client, "testing")
    assert pusher.calls == [(t["id"], "testing", "test")]
    post_event(device_client, t["id"], "status", claim=tok, status_to="done")
    assert pusher.calls[-1] == (t["id"], "done", "status")
    n = len(pusher.calls)
    t2, tok2 = reach(session_client, device_client, "waiting")
    assert pusher.calls[n:] == [(t2["id"], "waiting", "question")]
    post_event(session_client, t2["id"], "answer")
    post_event(device_client, t2["id"], "update", claim=tok2)
    post_event(device_client, t2["id"], "test", claim=tok2, passed=True, manual=False)
    post_event(session_client, t2["id"], "verdict", verdict="done")          # S closing: no push
    assert pusher.calls[n:] == [(t2["id"], "waiting", "question"), (t2["id"], "testing", "test")]


def test_a_failing_pusher_never_fails_the_request(app, device_client, session_client):
    class Boom:
        def notify(self, ticket, event):
            raise RuntimeError("boom")
    app.state.pusher = Boom()
    reach(session_client, device_client, "waiting")


def test_ticket_bus_tracks_the_last_seq(app, session_client):
    t = create(session_client)
    ev = post_event(session_client, t["id"], "update")
    assert app.state.ticket_bus.last_seq == ev["seq"]


def test_ticket_bus_starts_from_the_db(settings, session_client):
    t = create(session_client)
    ev = post_event(session_client, t["id"], "update")
    from fileshare.app import create_app
    assert create_app(settings).state.ticket_bus.last_seq == ev["seq"]
