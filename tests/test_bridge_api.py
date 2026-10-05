"""The bridge mailbox (Remote, R8): routes under /api/bridge/{space}/..., auth binding, FIFO, wake-ups,
expiry, the host lease, quotas, size caps and the promise that stream frames never commit to the database.

Bodies here are random base64url text: the server never opens them, so neither do these tests."""
import asyncio
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from fileshare import bridge as br
from fileshare import tickets
from fileshare.app import create_app
from tests.helpers.onboard import onboard_device
from tests.helpers.tickets import b64u, device_client, device_client_for, fake_env, other_device, other_device_client  # noqa: F401

SPACE = "c" * 32
SPACE2 = "d" * 32
HOLDER = "host_aaaa"
HOLDER2 = "host_bbbb"


def rid() -> str:
    return os.urandom(16).hex()


def sealed(n: int = 40) -> str:
    return b64u(os.urandom(n))


def post_req(c, space=SPACE, **kw):
    body = {"id": rid(), "body": sealed(), **kw}
    r = c.post(f"/api/bridge/{space}/requests", json=body)
    r.sent = body
    return r


def host_poll(c, space=SPACE, holder=HOLDER, wait=0, take_over=0):
    return c.get(f"/api/bridge/{space}/requests", params={"holder": holder, "wait": wait, "take_over": take_over})


def host_resp(c, rid_, idx=0, last=True, body=None, holder=HOLDER, space=SPACE):
    return c.post(f"/api/bridge/{space}/responses/{rid_}", params={"holder": holder},
                  json={"idx": idx, "last": last, "body": body or sealed()})


def poll(c, space=SPACE, wait=0, tab=None):
    params = {"wait": wait, **({"tab": tab} if tab else {})}
    return c.get(f"/api/bridge/{space}/responses", params=params)


def error(r) -> str:
    return r.json()["error"]


def run_async(fn):
    box = {}

    def target():
        try:
            box["value"] = asyncio.run(fn())
        except BaseException as e:
            box["error"] = e

    t = threading.Thread(target=target)
    t.start()
    t.join(60)
    assert not t.is_alive(), "async test body hung"
    if "error" in box:
        raise box["error"]
    return box.get("value")


@pytest.fixture
def host(device_client):
    """The approved device that owns SPACE, and holds its lease."""
    r = device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    assert r.status_code == 201, r.text
    assert host_poll(device_client).status_code == 200
    return device_client


def rows(settings) -> int:
    conn = sqlite3.connect(settings.db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM bridge_msgs").fetchone()[0]
    finally:
        conn.close()


# --- auth binding ----------------------------------------------------------------------------------

def test_every_bridge_route_refuses_an_anonymous_caller(app, settings):
    from fileshare.routes import bridge as bridge_routes      # FastAPI nests included routers in app.routes
    paths = {(m, r.path) for r in bridge_routes.router.routes for m in r.methods}
    assert len(paths) >= 6                             # derived from the app, not listed by hand
    anon = TestClient(app, base_url="http://testserver", headers={"Origin": settings.public_url})
    for method, path in sorted(paths):
        url = path.replace("{space}", SPACE).replace("{rid}", rid())
        r = anon.request(method, url, params={"holder": HOLDER}, json={} if method == "POST" else None)
        assert r.status_code == 401, (method, path, r.status_code)


def test_unknown_space_is_404_for_clients_and_hosts(session_client, device_client):
    assert post_req(session_client).status_code == 404
    assert poll(session_client).json()["error"] == "no_space"
    assert host_poll(device_client).status_code == 404
    assert host_resp(device_client, rid()).status_code == 404


def test_a_browser_session_can_never_host(session_client, host):
    assert host_poll(session_client).status_code == 403
    assert host_resp(session_client, rid()).status_code == 403
    assert session_client.delete(f"/api/bridge/{SPACE}/host", params={"holder": HOLDER}).status_code == 403


def test_only_the_owner_device_hosts(host, other_device_client):
    r = host_poll(other_device_client)
    assert r.status_code == 403 and error(r) == "not_owner"
    assert host_resp(other_device_client, rid()).status_code == 403


def test_a_revoked_device_is_refused_on_both_sides(host, device, session_client):
    assert session_client.delete(f"/api/devices/{device.id}").status_code == 204
    assert host_poll(host).status_code == 401
    assert post_req(host).status_code == 401
    assert poll(host).status_code == 401


def test_a_pending_device_is_refused(owner, sharing, app, host):
    pending = onboard_device(owner, sharing, name="phone", project="p", approve=False)
    c = device_client_for(app, pending)
    assert post_req(c).status_code == 403 and poll(c).status_code == 403


def test_a_device_may_be_a_client_of_a_space_it_does_not_own(host, other_device_client):
    r = post_req(other_device_client)
    assert r.status_code == 201 and r.json()["host_online"] is True
    got = host_poll(host).json()["requests"]
    assert [x["id"] for x in got] == [r.sent["id"]]


def test_a_session_write_needs_the_origin_header(host, session_client):
    r = session_client.post(f"/api/bridge/{SPACE}/requests", json={"id": rid(), "body": sealed()},
                            headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


# --- the happy path, FIFO, isolation ----------------------------------------------------------------

def test_page_round_trip_in_order_and_nothing_left_behind(host, session_client, settings):
    r = post_req(session_client)
    assert r.status_code == 201 and r.json()["expires_in"] == 60
    got = host_poll(host).json()
    assert got["requests"] == [{"id": r.sent["id"], "body": r.sent["body"]}]    # byte for byte
    assert host_poll(host).json()["requests"] == []                              # deleted after delivery
    a, b = sealed(), sealed()
    assert host_resp(host, r.sent["id"], idx=0, last=False, body=a).status_code == 204
    assert host_resp(host, r.sent["id"], idx=1, last=True, body=b).status_code == 204
    chunks = poll(session_client).json()["chunks"]
    assert chunks == [{"id": r.sent["id"], "idx": 0, "last": False, "stream": False, "body": a},
                      {"id": r.sent["id"], "idx": 1, "last": True, "stream": False, "body": b}]
    assert poll(session_client).json() == {"chunks": []}
    assert rows(settings) == 0
    assert host_resp(host, r.sent["id"]).status_code == 404            # the route ended with the last chunk


def test_requests_reach_the_host_first_in_first_out(host, session_client, device):
    sent = [post_req(session_client).sent["id"] for _ in range(5)]
    assert [x["id"] for x in host_poll(host).json()["requests"]] == sent


def test_a_poll_returns_everything_ready_for_any_open_request(host, session_client):
    ids = [post_req(session_client).sent["id"] for _ in range(3)]
    host_poll(host)
    for i in reversed(ids):
        host_resp(host, i)
    got = poll(session_client).json()["chunks"]
    assert sorted(c["id"] for c in got) == sorted(ids)


def test_a_client_sees_only_its_own_responses(host, session_client, other_device_client):
    mine, theirs = post_req(session_client).sent["id"], post_req(other_device_client).sent["id"]
    host_poll(host)
    host_resp(host, mine), host_resp(host, theirs)
    assert [c["id"] for c in poll(session_client).json()["chunks"]] == [mine]
    assert [c["id"] for c in poll(other_device_client).json()["chunks"]] == [theirs]
    assert session_client.delete(f"/api/bridge/{SPACE}/requests/{theirs}").status_code == 404


def test_tabs_of_one_session_do_not_see_each_others_responses(host, session_client):
    a, b = post_req(session_client, tab="tab_a").sent["id"], post_req(session_client, tab="tab_b").sent["id"]
    host_poll(host)
    host_resp(host, a), host_resp(host, b)
    assert [c["id"] for c in poll(session_client, tab="tab_b").json()["chunks"]] == [b]
    assert [c["id"] for c in poll(session_client, tab="tab_a").json()["chunks"]] == [a]


def test_mailboxes_do_not_leak_into_each_other(host, session_client, app, owner, sharing, settings):
    second = onboard_device(owner, sharing, name="second", project="p2")
    c2 = device_client_for(app, second)
    assert c2.post("/api/spaces", json={"id": SPACE2, "key_version": 1, "enc_label": fake_env()}).status_code == 201
    host_poll(c2, SPACE2)
    one, two = post_req(session_client, SPACE), post_req(session_client, SPACE2)
    assert [x["id"] for x in host_poll(host).json()["requests"]] == [one.sent["id"]]
    assert [x["id"] for x in host_poll(c2, SPACE2).json()["requests"]] == [two.sent["id"]]
    assert host_resp(host, two.sent["id"]).status_code == 404          # another mailbox's id is no id here
    host_resp(c2, two.sent["id"], space=SPACE2)
    assert poll(session_client, SPACE).json() == {"chunks": []}
    assert [c["id"] for c in poll(session_client, SPACE2).json()["chunks"]] == [two.sent["id"]]


def test_a_response_needs_a_fetched_request_and_stops_after_last(host, session_client):
    i = post_req(session_client).sent["id"]
    assert error(host_resp(host, i)) == "not_delivered"
    host_poll(host)
    assert host_resp(host, i, last=True).status_code == 204
    assert error(host_resp(host, i)) == "already_complete"


def test_cancel_frees_the_slot_and_drops_what_is_buffered(host, session_client, settings):
    i = post_req(session_client, stream=True).sent["id"]
    host_poll(host)
    host_resp(host, i, last=False)
    assert session_client.delete(f"/api/bridge/{SPACE}/requests/{i}").status_code == 204
    assert poll(session_client).json() == {"chunks": []}
    assert host_resp(host, i).status_code == 404
    j = post_req(session_client)
    assert session_client.delete(f"/api/bridge/{SPACE}/requests/{j.sent['id']}").status_code == 204
    assert rows(settings) == 0 and host_poll(host).json()["requests"] == []


# --- frames never commit ----------------------------------------------------------------------------

def _data_version(settings, conn):
    return conn.execute("PRAGMA data_version").fetchone()[0]


def test_stream_frames_cost_no_database_commits(host, session_client, settings):
    i = post_req(session_client, stream=True).sent["id"]
    host_poll(host)
    host_resp(host, i, idx=0, last=False)           # warm up: auth bookkeeping (last_seen) may write once
    poll(session_client)
    watcher = sqlite3.connect(settings.db_path)
    try:
        before = _data_version(settings, watcher)
        for n in range(1, 41):
            assert host_resp(host, i, idx=n, last=n == 40).status_code == 204
        got = poll(session_client).json()["chunks"]
        assert [c["idx"] for c in got] == list(range(1, 33))      # one poll hands over at most 32 chunks
        got += poll(session_client).json()["chunks"]
        assert [c["idx"] for c in got] == list(range(1, 41)) and all(c["stream"] for c in got) and got[-1]["last"]
        assert _data_version(settings, watcher) == before          # not one commit for 40 frames and their delivery
        assert watcher.execute("SELECT COUNT(*) FROM bridge_msgs").fetchone()[0] == 0
        j = post_req(session_client).sent["id"]                    # and a page response does commit: the counter works
        host_poll(host)
        host_resp(host, j)
        assert _data_version(settings, watcher) != before
    finally:
        watcher.close()


# --- expiry -------------------------------------------------------------------------------------------

def test_requests_expire_after_60_seconds_and_are_purged(host, session_client, frozen_clock, settings):
    from datetime import datetime, timezone
    t0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    frozen_clock(t0)
    post_req(session_client)
    assert rows(settings) == 1
    frozen_clock(t0 + timedelta(seconds=61))
    assert host_poll(host).json()["requests"] == []
    post_req(session_client)                          # any later write purges what expired
    assert rows(settings) == 1


def test_responses_and_frames_expire_after_60_seconds(host, session_client, frozen_clock):
    from datetime import datetime, timezone
    t0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    frozen_clock(t0)
    page, stream = post_req(session_client).sent["id"], post_req(session_client, stream=True).sent["id"]
    host_poll(host)
    host_resp(host, page, last=False)
    host_resp(host, stream, last=False)
    frozen_clock(t0 + timedelta(seconds=30))
    host_poll(host)                                    # the host keeps polling, so its lease holds
    frozen_clock(t0 + timedelta(seconds=61))
    assert poll(session_client).json() == {"chunks": []}
    assert host_resp(host, page).status_code == 404    # the request itself expired too


def test_a_stream_lives_as_long_as_its_frames_arrive(host, session_client, frozen_clock):
    from datetime import datetime, timezone
    t0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    frozen_clock(t0)
    i = post_req(session_client, stream=True).sent["id"]
    host_poll(host)
    for k in range(1, 5):
        frozen_clock(t0 + timedelta(seconds=40 * k))
        host_poll(host)
        assert host_resp(host, i, idx=k, last=False).status_code == 204
        assert [c["idx"] for c in poll(session_client).json()["chunks"]] == [k]


# --- wake-ups --------------------------------------------------------------------------------------------

def _async_client(app, token):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                             headers={"Authorization": f"Bearer {token}"})


def test_a_waiting_host_is_woken_by_a_post_not_by_a_timer(app, device, host, other_device):
    async def body():
        async with _async_client(app, device.token) as h, _async_client(app, other_device.token) as c:
            t = asyncio.create_task(h.get(f"/api/bridge/{SPACE}/requests", params={"holder": HOLDER, "wait": 20}))
            await asyncio.sleep(0.3)
            start = time.monotonic()
            sent = await c.post(f"/api/bridge/{SPACE}/requests", json={"id": rid(), "body": sealed()})
            got = await asyncio.wait_for(t, 5)
            return time.monotonic() - start, sent.json()["id"], got.json()["requests"]

    took, sent, got = run_async(body)
    assert took < 1.0 and [x["id"] for x in got] == [sent]      # the poll slice is 2 s: this was the wake-up


def test_every_waiting_response_poll_is_woken(app, device, host, other_device):
    async def body():
        async with _async_client(app, device.token) as h, _async_client(app, other_device.token) as c:
            ids = [rid(), rid()]
            for i, tab in zip(ids, ("t_a", "t_b")):
                await c.post(f"/api/bridge/{SPACE}/requests", json={"id": i, "body": sealed(), "tab": tab})
            await h.get(f"/api/bridge/{SPACE}/requests", params={"holder": HOLDER})
            polls = [asyncio.create_task(c.get(f"/api/bridge/{SPACE}/responses", params={"wait": 20, "tab": t}))
                     for t in ("t_a", "t_b")]
            await asyncio.sleep(0.3)
            start = time.monotonic()
            for i in ids:
                await h.post(f"/api/bridge/{SPACE}/responses/{i}", params={"holder": HOLDER},
                             json={"idx": 0, "last": True, "body": sealed()})
            got = await asyncio.wait_for(asyncio.gather(*polls), 5)
            return time.monotonic() - start, [g.json()["chunks"][0]["id"] for g in got], ids

    took, got, ids = run_async(body)
    assert took < 1.0 and got == ids


def test_a_post_between_the_fetch_and_the_wait_is_not_lost(app, device, host, other_device, monkeypatch):
    """The wake-up is armed before the fetch: a request that lands after the fetch has looked, but before
    the poll starts waiting, still ends the wait at once instead of after the 2 s slice."""
    real = br.take_requests
    calls, loop_box = [], []

    def racy(conn, space, now):
        out = real(conn, space, now)                    # looks first: nothing there yet
        if not calls:
            calls.append(1)
            br.insert_request(conn, space, rid(), sealed(), now)       # ... then the request arrives
            loop_box[0].call_soon_threadsafe(app.state.bridge.box(space).req_wake.fire)
        return out

    async def body():
        loop = asyncio.get_running_loop()
        loop_box.append(loop)
        monkeypatch.setattr(br, "take_requests", racy)
        async with _async_client(app, device.token) as h:
            start = time.monotonic()
            r = await asyncio.wait_for(h.get(f"/api/bridge/{SPACE}/requests", params={"holder": HOLDER, "wait": 20}), 10)
            return time.monotonic() - start, r.json()["requests"]

    took, got = run_async(body)
    assert len(got) == 1 and took < 1.0


# --- the host lease -------------------------------------------------------------------------------------

def test_a_second_host_is_refused_until_it_takes_over(host, session_client):
    r = host_poll(host, holder=HOLDER2)
    assert r.status_code == 409 and error(r) == "host_taken"
    post_req(session_client)
    assert host_poll(host, holder=HOLDER2, take_over=1).status_code == 200
    stale = host_resp(host, rid(), holder=HOLDER)
    assert stale.status_code == 409 and error(stale) == "lease_lost"       # the old host can no longer answer
    assert error(host_poll(host, holder=HOLDER)) == "host_taken"           # nor quietly take it back


def test_the_lease_expires_when_the_host_stops_polling(host, frozen_clock):
    from datetime import datetime, timezone
    t0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    frozen_clock(t0)
    host_poll(host)
    frozen_clock(t0 + timedelta(seconds=br.LEASE_S - 1))
    assert host_poll(host, holder=HOLDER2).status_code == 409
    frozen_clock(t0 + timedelta(seconds=2 * br.LEASE_S))
    assert host_poll(host, holder=HOLDER2).status_code == 200


def test_a_released_lease_can_be_taken_at_once(host):
    assert host.delete(f"/api/bridge/{SPACE}/host", params={"holder": HOLDER2}).status_code == 204   # not the holder: no effect
    assert host_poll(host, holder=HOLDER2).status_code == 409
    assert host.delete(f"/api/bridge/{SPACE}/host", params={"holder": HOLDER}).status_code == 204
    assert host_poll(host, holder=HOLDER2).status_code == 200


def test_the_lease_follows_a_change_of_owner(host, app, owner, sharing, session_client):
    new_owner = onboard_device(owner, sharing, name="newer", project="n")
    nc = device_client_for(app, new_owner)
    jr = nc.post(f"/api/spaces/{SPACE}/join").json()["request"]["id"]
    assert session_client.post(f"/api/spaces/{SPACE}/join-requests/{jr}/approve").status_code == 200
    assert host_poll(host).status_code == 403                  # the old owner's lease is worth nothing
    assert host_poll(nc, holder=HOLDER2).status_code == 200    # and the new owner needs no take-over


def test_post_reports_whether_a_host_is_polling(device_client, session_client):
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    assert post_req(session_client).json()["host_online"] is False
    host_poll(device_client)
    assert post_req(session_client).json()["host_online"] is True


# --- quotas ------------------------------------------------------------------------------------------------

def test_open_requests_are_limited_per_client_not_per_mailbox(host, session_client, other_device_client):
    for _ in range(br.CLIENT_INFLIGHT):
        assert post_req(session_client).status_code == 201
    r = post_req(session_client)
    assert r.status_code == 429 and error(r) == "too_many_inflight"
    assert post_req(other_device_client).status_code == 201     # the device's quota is its own


def test_open_streams_are_limited(host, session_client):
    for _ in range(br.CLIENT_STREAMS):
        assert post_req(session_client, stream=True).status_code == 201
    r = post_req(session_client, stream=True)
    assert r.status_code == 429 and error(r) == "too_many_streams"
    assert post_req(session_client).status_code == 201


def test_requests_per_second_are_limited_for_both_principals(host, session_client, other_device_client, frozen_clock):
    from datetime import datetime, timezone
    frozen_clock(datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc))      # the window never moves
    for c in (session_client, other_device_client):
        codes = []
        for _ in range(br.POSTS_PER_S + 1):
            r = post_req(c)
            codes.append(r.status_code)
            if r.status_code == 201:
                c.delete(f"/api/bridge/{SPACE}/requests/{r.sent['id']}")
        assert codes[-1] == 429 and set(codes[:-1]) == {201}
        assert error(r) == "rate_limited"


def test_polls_are_limited_per_client_and_leave_the_ticket_slots_alone(app, host, device, session_client):
    async def body():
        async with _async_client(app, device.token) as c:
            held = [asyncio.create_task(c.get(f"/api/bridge/{SPACE}/responses", params={"wait": 20, "tab": f"t{i}"}))
                    for i in range(br.CLIENT_POLLS)]
            for _ in range(100):
                if app.state.bridge.polls.get("d:" + device.id) == br.CLIENT_POLLS:
                    break
                await asyncio.sleep(0.05)
            over = await c.get(f"/api/bridge/{SPACE}/responses")
            ticket_slots = tickets.poll_count(device.id)
            for t in held:
                t.cancel()
            await asyncio.gather(*held, return_exceptions=True)
            return over, ticket_slots

    over, ticket_slots = run_async(body)
    assert over.status_code == 429 and error(over) == "too_many_polls"
    assert ticket_slots == 0
    assert app.state.bridge.polls == {}


def test_host_polls_are_limited(app, host, device):
    async def body():
        async with _async_client(app, device.token) as c:
            held = [asyncio.create_task(c.get(f"/api/bridge/{SPACE}/requests", params={"holder": HOLDER, "wait": 20}))
                    for _ in range(br.HOST_POLLS)]
            for _ in range(100):
                if app.state.bridge.polls.get("h:" + device.id) == br.HOST_POLLS:
                    break
                await asyncio.sleep(0.05)
            over = await c.get(f"/api/bridge/{SPACE}/requests", params={"holder": HOLDER})
            for t in held:
                t.cancel()
            await asyncio.gather(*held, return_exceptions=True)
            return over

    over = run_async(body)
    assert over.status_code == 429 and error(over) == "too_many_polls"


def test_a_full_mailbox_pushes_back_on_the_host(host, session_client, monkeypatch):
    monkeypatch.setattr(br, "BOX_FRAME_BYTES", 100)
    monkeypatch.setattr(br, "BOX_PAGE_BYTES", 100)
    s, p = post_req(session_client, stream=True).sent["id"], post_req(session_client).sent["id"]
    host_poll(host)
    for i in (s, p):
        assert host_resp(host, i, last=False, body=sealed(60)).status_code == 204
        r = host_resp(host, i, last=False, body=sealed(60))
        assert r.status_code == 429 and error(r) == "mailbox_full"


def test_too_many_queued_requests_for_a_slow_host(host, session_client, monkeypatch):
    monkeypatch.setattr(br, "BOX_QUEUED_REQUESTS", 2)
    assert post_req(session_client).status_code == 201 and post_req(session_client).status_code == 201
    r = post_req(session_client)
    assert r.status_code == 429 and error(r) == "mailbox_full"


# --- size caps and shapes ---------------------------------------------------------------------------------

def _body_of(n_bytes: int) -> str:
    return "A" * br.b64_len(n_bytes)


def test_request_and_chunk_size_caps(host, session_client):
    ok = session_client.post(f"/api/bridge/{SPACE}/requests", json={"id": rid(), "body": _body_of(br.MAX_REQUEST_BYTES)})
    assert ok.status_code == 201
    big = session_client.post(f"/api/bridge/{SPACE}/requests",
                              json={"id": rid(), "body": _body_of(br.MAX_REQUEST_BYTES + 3)})
    assert big.status_code == 413 and error(big) == "too_large"
    i = post_req(session_client).sent["id"]
    host_poll(host)
    assert br.MAX_CHUNK_BYTES == 256 * 1024
    assert host_resp(host, i, last=False, body=_body_of(br.MAX_CHUNK_BYTES)).status_code == 204
    over = host_resp(host, i, body=_body_of(br.MAX_CHUNK_BYTES + 3))
    assert over.status_code == 413 and error(over) == "too_large"


@pytest.mark.parametrize("bad", [{"body": "not base64!"}, {"body": ""}, {"body": 5}, {"id": "xyz"}, {"tab": "a b"},
                                 {"stream": "yes"}, {"body": "ab=="}])
def test_malformed_requests_are_400(host, session_client, bad):
    body = {"id": rid(), "body": sealed(), **bad}
    assert session_client.post(f"/api/bridge/{SPACE}/requests", json=body).status_code == 400


def test_a_duplicate_id_is_409(host, session_client):
    r = post_req(session_client)
    again = session_client.post(f"/api/bridge/{SPACE}/requests", json=r.sent)
    assert again.status_code == 409 and error(again) == "duplicate_id"


@pytest.mark.parametrize("params", [{"wait": "26"}, {"wait": "x"}, {"tab": "no spaces"}])
def test_bad_poll_parameters_are_400(host, session_client, params):
    assert session_client.get(f"/api/bridge/{SPACE}/responses", params=params).status_code == 400


def test_host_calls_need_a_holder(host):
    assert host.get(f"/api/bridge/{SPACE}/requests").status_code == 400
    assert host.post(f"/api/bridge/{SPACE}/responses/{rid()}", json={"idx": 0, "last": True, "body": sealed()}).status_code == 400
    assert host_poll(host, take_over=2).status_code == 400


# --- what the server keeps and logs --------------------------------------------------------------------------

def test_logs_carry_only_opaque_ids(host, session_client, caplog, device):
    caplog.set_level(logging.DEBUG)
    a, b = sealed(30), sealed(30)
    r = session_client.post(f"/api/bridge/{SPACE}/requests", json={"id": rid(), "body": a})
    host_poll(host, holder=HOLDER2)                                    # refused: logs the refusal
    host_poll(host, holder=HOLDER2, take_over=1)
    post_req(session_client, stream=True)
    for _ in range(br.CLIENT_STREAMS):
        post_req(session_client, stream=True)
    host_resp(host, r.json()["id"], body=b, holder=HOLDER2)
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "bridge" in text
    assert a not in text and b not in text and "laptop" not in text and "proj" not in text


def test_nothing_the_server_stores_is_decoded(host, session_client, settings):
    body = sealed(50)
    session_client.post(f"/api/bridge/{SPACE}/requests", json={"id": rid(), "body": body})
    conn = sqlite3.connect(settings.db_path)
    try:
        assert [r[0] for r in conn.execute("SELECT body FROM bridge_msgs")] == [body]
        cols = [r[1] for r in conn.execute("PRAGMA table_info(bridge_msgs)")]
    finally:
        conn.close()
    assert set(cols) == {"n", "space_id", "kind", "rid", "idx", "last", "body", "expires_at"}   # ids, sizes, timing


def test_a_restart_drops_what_was_waiting(host, session_client, settings):
    post_req(session_client)
    assert rows(settings) == 1
    create_app(settings)
    assert rows(settings) == 0
