"""Tickets long-polls (spec T6): GET …/{ref}/events, GET /api/tickets/changes, GET /api/tickets/attention.

Concurrency is exercised two ways: TestClient requests in threads (a poll plus an insert), and raw ASGI
calls in a private event loop (to hold 20 polls open, and to disconnect or cancel them mid-poll).
"""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from fileshare import tickets
from tests.helpers.tickets import (claim, create, device_client, device_client_for, get,  # noqa: F401
                                   other_device, other_device_client, post_event)

T0 = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)


def _events(client, ref, after, wait=0, headers=None):
    return client.get(f"/api/tickets/{ref}/events", params={"after": after, "wait": wait}, headers=headers or {})


def _changes(client, after, wait=0):
    return client.get("/api/tickets/changes", params={"after": after, "wait": wait})


def _latest_seq(client, ref) -> int:
    return get(client, ref)["last_seq"]


def run_async(fn):
    """asyncio.run in a fresh thread, so a loop left running by another test can't interfere."""
    box = {}

    def target():
        try:
            box["value"] = asyncio.run(fn())
        except BaseException as e:      # re-raised in the test thread
            box["error"] = e

    t = threading.Thread(target=target)
    t.start()
    t.join(60)
    assert not t.is_alive(), "async test body hung"
    if "error" in box:
        raise box["error"]
    return box.get("value")


# --- ticket events: immediate, timeout, wake-up ------------------------------------------------

def test_after_below_latest_returns_at_once(device_client, session_client):
    t = create(session_client)
    ev = post_event(session_client, t["id"], "update")
    start = time.monotonic()
    r = _events(device_client, t["id"], after=0, wait=10)
    assert time.monotonic() - start < 1
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"events", "ticket", "cursor"}
    assert [e["kind"] for e in body["events"]] == ["created", "update"]
    assert body["events"][-1]["uuid"] == ev["uuid"]
    assert body["cursor"] == ev["seq"]
    assert body["ticket"]["id"] == t["id"] and body["ticket"]["last_seq"] == ev["seq"]
    assert r.headers["cache-control"] == "no-store"


def test_after_filters_by_seq(session_client):
    t = create(session_client)
    first = post_event(session_client, t["id"], "update")
    second = post_event(session_client, t["id"], "update")
    body = _events(session_client, t["id"], after=first["seq"]).json()
    assert [e["seq"] for e in body["events"]] == [second["seq"]]
    assert body["cursor"] == second["seq"]


def test_wait_1_with_nothing_new_returns_empty_after_about_1s(device_client, session_client):
    t = create(session_client)
    seq = _latest_seq(session_client, t["id"])
    start = time.monotonic()
    r = _events(device_client, t["id"], after=seq, wait=1)
    elapsed = time.monotonic() - start
    assert r.status_code == 200, r.text
    assert r.json()["events"] == [] and r.json()["cursor"] == seq
    assert r.json()["ticket"]["id"] == t["id"]
    assert 0.9 <= elapsed < 2.0


def test_wait_0_returns_at_once(session_client):
    t = create(session_client)
    seq = _latest_seq(session_client, t["id"])
    start = time.monotonic()
    r = _events(session_client, t["id"], after=seq, wait=0)
    assert time.monotonic() - start < 0.5
    assert r.status_code == 200 and r.json()["events"] == []


def test_poll_is_woken_by_an_event_on_the_same_ticket(device_client, session_client):
    t = create(session_client)
    seq = _latest_seq(session_client, t["id"])
    with ThreadPoolExecutor(1) as ex:
        start = time.monotonic()
        fut = ex.submit(_events, device_client, t["id"], seq, 10)
        time.sleep(0.4)
        ev = post_event(session_client, t["id"], "update")
        posted = time.monotonic()
        r = fut.result(15)
        woke = time.monotonic()
    assert r.status_code == 200, r.text
    assert [e["uuid"] for e in r.json()["events"]] == [ev["uuid"]]
    assert r.json()["cursor"] == ev["seq"]
    assert woke - posted < 1.0
    assert woke - start < 5


def test_poll_on_one_ticket_ignores_events_on_another(device_client, session_client):
    """Review Focus 1: a long-poll on TIX-1 while TIX-2 gets events returns nothing for TIX-2."""
    t1 = create(session_client)
    t2 = create(session_client)
    seq = _latest_seq(session_client, t2["id"])
    with ThreadPoolExecutor(1) as ex:
        start = time.monotonic()
        fut = ex.submit(_events, device_client, t1["id"], seq, 2)
        time.sleep(0.3)
        ev = post_event(session_client, t2["id"], "update")
        r = fut.result(10)
        elapsed = time.monotonic() - start
    assert r.status_code == 200, r.text
    assert r.json()["events"] == []
    assert r.json()["ticket"]["id"] == t1["id"]
    assert elapsed >= 1.9
    r2 = _events(device_client, t2["id"], after=seq)
    assert [e["uuid"] for e in r2.json()["events"]] == [ev["uuid"]]
    assert all(e["ticket"] == t2["id"] for e in r2.json()["events"])


def test_events_poll_is_capped_at_200(session_client):
    t = create(session_client)
    for _ in range(205):
        post_event(session_client, t["id"], "update")
    body = _events(session_client, t["id"], after=0).json()
    assert len(body["events"]) == 200
    assert body["cursor"] == body["events"][-1]["seq"]
    rest = _events(session_client, t["id"], after=body["cursor"]).json()
    assert len(rest["events"]) == 6          # 1 created + 205 updates


# --- refs, auth ---------------------------------------------------------------------------------

def test_events_poll_refs(session_client):
    t = create(session_client)
    assert _events(session_client, f"tix{t['n']}", 0).status_code == 200
    assert _events(session_client, "TIX-999", 0).json()["error"] == "not_found"
    assert _events(session_client, "bogus", 0).status_code == 404
    assert session_client.delete(f"/api/tickets/{t['id']}").status_code == 204
    r = _events(session_client, t["id"], 0)
    assert r.status_code == 410 and r.json()["error"] == "deleted"


def test_events_poll_needs_auth(app, session_client):
    t = create(session_client)
    from fastapi.testclient import TestClient
    anon = TestClient(app, base_url="http://testserver")
    assert _events(anon, t["id"], 0).status_code == 401


# --- claim heartbeat -----------------------------------------------------------------------------

def test_claim_token_poll_marks_listening(frozen_clock, device_client, session_client):
    frozen_clock(T0)
    t = create(session_client)
    tok = claim(device_client, t["id"])
    assert get(session_client, t["id"])["claim"]["state"] == "working"
    r = _events(device_client, t["id"], 0, headers={"X-Claim-Token": tok})
    assert r.status_code == 200, r.text
    c = get(session_client, t["id"])["claim"]
    assert c["poll_at"] == "2026-09-25T12:00:00Z" and c["seen_at"] == "2026-09-25T12:00:00Z"
    assert c["state"] == "listening"
    assert r.json()["ticket"]["claim"]["state"] == "listening"
    frozen_clock(T0 + timedelta(seconds=91))
    assert get(session_client, t["id"])["claim"]["state"] == "working"
    frozen_clock(T0 + timedelta(minutes=31))
    assert get(session_client, t["id"])["claim"]["state"] == "gone"


def test_poll_without_token_does_not_mark_listening(frozen_clock, device_client, session_client):
    frozen_clock(T0)
    t = create(session_client)
    claim(device_client, t["id"])
    assert _events(device_client, t["id"], 0).status_code == 200
    assert get(session_client, t["id"])["claim"]["poll_at"] is None


def test_wrong_claim_token_is_claim_lost(device_client, other_device_client, session_client):
    t = create(session_client)
    tok = claim(device_client, t["id"])
    r = _events(device_client, t["id"], 0, headers={"X-Claim-Token": "clm_wrong"})
    assert r.status_code == 409 and r.json()["error"] == "claim_lost"
    r = _events(other_device_client, t["id"], 0, headers={"X-Claim-Token": tok})
    assert r.status_code == 409 and r.json()["error"] == "claim_lost"
    unclaimed = create(session_client)
    r = _events(device_client, unclaimed["id"], 0, headers={"X-Claim-Token": tok})
    assert r.status_code == 409 and r.json()["error"] == "claim_lost"
    assert get(session_client, t["id"])["claim"]["poll_at"] is None


def test_takeover_wakes_the_old_holders_poll(device_client, other_device_client, session_client):
    t = create(session_client)
    tok = claim(device_client, t["id"])
    seq = _latest_seq(session_client, t["id"])
    with ThreadPoolExecutor(1) as ex:
        fut = ex.submit(_events, device_client, t["id"], seq, 10, {"X-Claim-Token": tok})
        time.sleep(0.3)
        claim(other_device_client, t["id"], takeover=True)
        r = fut.result(15)
    assert [e["kind"] for e in r.json()["events"]] == ["claim"]
    r = _events(device_client, t["id"], 0, headers={"X-Claim-Token": tok})
    assert r.status_code == 409 and r.json()["error"] == "claim_lost"


# --- per-device concurrency limit and cleanup ----------------------------------------------------

def _asgi_poll(app, path, token, disconnect: asyncio.Event):
    """One raw ASGI GET. The client 'disconnects' (http.disconnect) once `disconnect` is set."""
    sent = {"request": False}
    out = {"status": None, "body": b""}

    async def receive():
        if not sent["request"]:
            sent["request"] = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    p, _, q = path.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": p, "raw_path": p.encode(), "query_string": q.encode(),
             "root_path": "", "headers": [(b"host", b"testserver"),
                                          (b"authorization", f"Bearer {token}".encode())],
             "client": ("127.0.0.1", 50000), "server": ("testserver", 80), "state": {}}

    async def go():
        await app(scope, receive, send)
        return out
    return go()


async def _until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, "condition never became true"
        await asyncio.sleep(0.02)


def test_21st_concurrent_poll_is_429_and_disconnects_free_every_slot(app, device, device_client, session_client):
    t = create(session_client)
    seq = _latest_seq(session_client, t["id"])
    path = f"/api/tickets/{t['id']}/events?after={seq}&wait=30"
    dev_id = device.id

    async def body():
        gone = asyncio.Event()
        polls = [asyncio.create_task(_asgi_poll(app, path, device.token, gone)) for _ in range(20)]
        await _until(lambda: tickets.poll_count(dev_id) == 20)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                     headers={"Authorization": f"Bearer {device.token}"}) as c:
            r = await c.get(path)
            r_changes_other = await c.get(f"/api/tickets/{t['id']}/events?after=0&wait=0")
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"
        assert r_changes_other.status_code == 429
        assert tickets.poll_count(dev_id) == 20
        start = time.monotonic()
        gone.set()                                   # every client goes away mid-poll
        results = await asyncio.wait_for(asyncio.gather(*polls), 5)
        assert time.monotonic() - start < 2          # noticed within a poll step or so, not after 30 s
        return results

    run_async(body)
    assert tickets.poll_count(dev_id) == 0
    assert _events(device_client, t["id"], seq, 0).status_code == 200


def test_cancelled_polls_free_their_slots(app, device, device_client, session_client):
    t = create(session_client)
    seq = _latest_seq(session_client, t["id"])
    dev_id = device.id

    async def body():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                     headers={"Authorization": f"Bearer {device.token}"}) as c:
            tasks = [asyncio.create_task(c.get(f"/api/tickets/{t['id']}/events",
                                               params={"after": seq, "wait": 30})) for _ in range(5)]
            await _until(lambda: tickets.poll_count(dev_id) == 5)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    run_async(body)
    assert tickets.poll_count(dev_id) == 0


def test_finished_and_failed_polls_free_their_slots(device, device_client, session_client):
    t = create(session_client)
    dev_id = device.id
    assert _events(device_client, t["id"], 0, 0).status_code == 200
    assert _events(device_client, t["id"], _latest_seq(session_client, t["id"]), 1).status_code == 200
    assert _events(device_client, t["id"], 0, 0, headers={"X-Claim-Token": "clm_x"}).status_code == 409
    assert _events(device_client, "TIX-999", 0, 0).status_code == 404
    assert tickets.poll_count(dev_id) == 0


def test_poll_limit_is_per_device(app, device, other_device, session_client):
    t = create(session_client)
    seq = _latest_seq(session_client, t["id"])
    path = f"/api/tickets/{t['id']}/events?after={seq}&wait=30"
    dev_id = device.id

    async def body():
        gone = asyncio.Event()
        polls = [asyncio.create_task(_asgi_poll(app, path, device.token, gone)) for _ in range(20)]
        await _until(lambda: tickets.poll_count(dev_id) == 20)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                     headers={"Authorization": f"Bearer {other_device.token}"}) as c:
            r = await c.get(path.replace("wait=30", "wait=0"))
        gone.set()
        await asyncio.wait_for(asyncio.gather(*polls), 5)
        return r.status_code

    assert run_async(body) == 200
    assert tickets.poll_count(dev_id) == 0


def test_constants():
    assert tickets.POLL_STEP_S == 0.5
    assert tickets.MAX_WAIT_S == 30
    assert tickets.MAX_POLLS_PER_DEVICE == 20


# --- board changes -------------------------------------------------------------------------------

def test_changes_returns_tickets_changed_after_the_cursor(session_client, device_client):
    a = create(session_client)
    b = create(session_client)
    cursor = _latest_seq(session_client, b["id"])
    r = _changes(session_client, 0)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"tickets", "cursor"}
    assert {x["id"] for x in r.json()["tickets"]} >= {a["id"], b["id"]}
    assert r.json()["cursor"] == cursor
    r = _changes(session_client, cursor)
    assert r.json() == {"tickets": [], "cursor": cursor}
    ev = post_event(device_client, a["id"], "update")
    body = _changes(session_client, cursor).json()
    assert [x["id"] for x in body["tickets"]] == [a["id"]]
    assert body["tickets"][0]["last_seq"] == ev["seq"]
    assert body["cursor"] == ev["seq"]


def test_changes_is_woken_by_any_ticket(session_client, device_client):
    t = create(session_client)
    cursor = _latest_seq(session_client, t["id"])
    with ThreadPoolExecutor(1) as ex:
        fut = ex.submit(_changes, session_client, cursor, 10)
        time.sleep(0.3)
        posted = time.monotonic()
        post_event(device_client, t["id"], "update")
        r = fut.result(15)
        woke = time.monotonic()
    assert [x["id"] for x in r.json()["tickets"]] == [t["id"]]
    assert woke - posted < 1.0


def test_changes_waits_then_returns_empty(session_client):
    t = create(session_client)
    cursor = _latest_seq(session_client, t["id"])
    start = time.monotonic()
    r = _changes(session_client, cursor, 1)
    assert 0.9 <= time.monotonic() - start < 2.0
    assert r.json() == {"tickets": [], "cursor": cursor}


def test_changes_include_tombstones(session_client):
    t = create(session_client)
    cursor = _latest_seq(session_client, t["id"])
    assert session_client.delete(f"/api/tickets/{t['id']}").status_code == 204
    body = _changes(session_client, cursor).json()
    assert [x["id"] for x in body["tickets"]] == [t["id"]]
    gone = body["tickets"][0]
    assert gone["deleted_at"] is not None and gone["enc_content"] is None and gone["wrapped_dek"] is None
    assert body["cursor"] > cursor


def test_changes_is_session_only(app, device_client):
    assert _changes(device_client, 0).status_code in (401, 403)
    from fastapi.testclient import TestClient
    anon = TestClient(app, base_url="http://testserver")
    assert _changes(anon, 0).status_code == 401


# --- attention --------------------------------------------------------------------------------------

def _reach_waiting(session_client, device_client):
    t = create(session_client)
    tok = claim(device_client, t["id"])
    post_event(device_client, t["id"], "question", claim=tok, question_count=1)
    return t


def _reach_testing(session_client, device_client):
    t = create(session_client)
    tok = claim(device_client, t["id"])
    post_event(device_client, t["id"], "test", claim=tok, passed=True, manual=False)
    return t


def test_attention_counts_waiting_and_testing(session_client, device_client):
    assert session_client.get("/api/tickets/attention").json() == {"count": 0}
    create(session_client)                                   # open
    t = create(session_client)
    claim(device_client, t["id"])                            # in-progress
    create(session_client, status="backlog")
    _reach_waiting(session_client, device_client)
    testing = _reach_testing(session_client, device_client)
    r = session_client.get("/api/tickets/attention")
    assert r.status_code == 200 and r.json() == {"count": 2}
    assert r.headers["cache-control"] == "no-store"
    assert session_client.delete(f"/api/tickets/{testing['id']}").status_code == 204
    assert session_client.get("/api/tickets/attention").json() == {"count": 1}


def test_attention_is_session_only(app, device_client):
    assert device_client.get("/api/tickets/attention").status_code in (401, 403)
    from fastapi.testclient import TestClient
    anon = TestClient(app, base_url="http://testserver")
    assert anon.get("/api/tickets/attention").status_code == 401


# --- parameter validation ------------------------------------------------------------------------

@pytest.mark.parametrize("params", [{"wait": "31"}, {"wait": "-1"}, {"wait": "1.5"}, {"wait": "x"},
                                    {"wait": "²"}, {"wait": "9" * 5000}, {"after": "-1"}, {"after": "x"},
                                    {"after": "²"}, {"after": "9" * 5000}])
def test_bad_params_are_400(session_client, params):
    t = create(session_client)
    q = {"after": "0", "wait": "0", **params}
    r = session_client.get(f"/api/tickets/{t['id']}/events", params=q)
    assert r.status_code == 400 and r.json()["error"] == "bad_request", r.text
    r = session_client.get("/api/tickets/changes", params=q)
    assert r.status_code == 400 and r.json()["error"] == "bad_request", r.text


def test_wait_30_is_accepted_and_defaults_are_immediate(session_client):
    t = create(session_client)
    ev = post_event(session_client, t["id"], "update")
    assert session_client.get(f"/api/tickets/{t['id']}/events", params={"after": 0, "wait": 30}).status_code == 200
    start = time.monotonic()
    r = session_client.get(f"/api/tickets/{t['id']}/events", params={"after": ev["seq"]})
    assert r.status_code == 200 and time.monotonic() - start < 0.5
    r = session_client.get(f"/api/tickets/{t['id']}/events")
    assert r.status_code == 200 and len(r.json()["events"]) == 2
    assert session_client.get("/api/tickets/changes").status_code == 200
