"""Host presence (Remote, R9): the pure state function, the heartbeat and goodbye routes (auth, host binding,
strict schema, caps, rate) and the client listing. Names never appear in any of it."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fileshare import presence as pr
from tests.helpers.tickets import device_client, fake_env, other_device, other_device_client  # noqa: F401

SPACE = "e" * 32
SPACE2 = "f" * 32
BEAT = {"sessions": 2, "in_progress": 3, "needs_you": 1, "factory": "running",
        "children_done": 4, "children_total": 9, "budget_pct": 37}
T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


def hb(c, space=SPACE, **over):
    body = {**BEAT, **over}
    return c.post(f"/api/presence/{space}/heartbeat", json={k: v for k, v in body.items() if v is not ...})


def bye(c, space=SPACE, json=None):
    return c.post(f"/api/presence/{space}/goodbye", json={} if json is None else json)


def listing(c) -> dict:
    r = c.get("/api/presence")
    assert r.status_code == 200, r.text
    return {s["id"]: s for s in r.json()["spaces"]}


@pytest.fixture
def host(device_client):
    r = device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    assert r.status_code == 201, r.text
    return device_client


# --- the state function ---------------------------------------------------------------------------

def row(hb_at, bye_at=None):
    return {"hb_at": hb_at, "bye_at": bye_at}


@pytest.mark.parametrize("age,state", [(0, "online"), (29.9, "online"), (30, "not_answering"), (300, "not_answering"),
                                       (300.1, "lost"), (86400, "lost")])
def test_state_follows_the_heartbeat_age(age, state):
    assert pr.derive_state(row(1000.0), 1000.0 + age) == state


def test_never_started_and_stopped():
    assert pr.derive_state(None, 5.0) == "never_started"
    assert pr.derive_state(row(1000.0, 1005.0), 1006.0) == "stopped"
    assert pr.derive_state(row(1000.0, 1005.0), 9999.0) == "stopped"        # a goodbye stays a stop, not "lost"
    assert pr.derive_state(row(1010.0, 1005.0), 1011.0) == "online"         # a newer heartbeat ends the stop


def test_every_state_is_listed():
    seen = {pr.derive_state(None, 0), pr.derive_state(row(0, 1), 2), pr.derive_state(row(0), 1),
            pr.derive_state(row(0), 100), pr.derive_state(row(0), 1000)}
    assert seen == set(pr.STATES)


# --- auth and host binding ------------------------------------------------------------------------

def test_every_presence_route_refuses_an_anonymous_caller(app, settings):
    paths = set()
    for r in app.routes:
        for ctx in (r.effective_route_contexts() if hasattr(r, "effective_route_contexts") else [r]):
            if getattr(ctx, "path", "").startswith("/api/presence"):
                paths |= {(m, ctx.path) for m in ctx.methods}
    assert len(paths) >= 3                             # derived from the app, not listed by hand
    anon = TestClient(app, base_url="http://testserver", headers={"Origin": settings.public_url})
    for method, path in sorted(paths):
        r = anon.request(method, path.replace("{space}", SPACE), json={} if method == "POST" else None)
        assert r.status_code == 401, (method, path, r.status_code)


def test_a_browser_session_can_never_report(session_client, host):
    assert hb(session_client).status_code == 403
    assert bye(session_client).status_code == 403


def test_only_the_owner_device_reports(host, other_device_client):
    r = hb(other_device_client)
    assert r.status_code == 403 and r.json()["error"] == "not_owner"
    assert bye(other_device_client).status_code == 403
    assert listing(host)[SPACE]["state"] == "never_started"


def test_unknown_space_and_bad_id(device_client):
    assert hb(device_client).status_code == 404
    assert hb(device_client, space="nope").status_code == 400


def test_a_revoked_device_is_refused(host, device, session_client):
    assert session_client.delete(f"/api/devices/{device.id}").status_code == 204
    assert hb(host).status_code == 401


# --- heartbeat and goodbye ------------------------------------------------------------------------

def test_a_heartbeat_is_listed_with_its_clear_fields(host, session_client, frozen_clock):
    frozen_clock(T0)
    assert listing(session_client)[SPACE]["state"] == "never_started"
    assert hb(host).status_code == 204
    s = listing(session_client)[SPACE]
    assert s["state"] == "online"
    assert {k: s[k] for k in pr.FIELDS} == BEAT
    assert s["last_seen"] == T0.timestamp() and s["goodbye_at"] is None
    assert s["enc_label"] and s["owner_name"]                     # the sealed label rides along, never decoded here
    frozen_clock(T0 + timedelta(seconds=45))
    assert listing(session_client)[SPACE]["state"] == "not_answering"
    frozen_clock(T0 + timedelta(minutes=6))
    assert listing(session_client)[SPACE]["state"] == "lost"


def test_a_device_can_read_the_listing_too(host):
    assert hb(host).status_code == 204
    assert listing(host)[SPACE]["state"] == "online"


def test_goodbye_marks_a_clean_stop_and_a_heartbeat_ends_it(host, session_client, frozen_clock):
    frozen_clock(T0)
    hb(host)
    frozen_clock(T0 + timedelta(seconds=5))
    assert bye(host).status_code == 204
    s = listing(session_client)[SPACE]
    assert s["state"] == "stopped" and s["goodbye_at"] == (T0 + timedelta(seconds=5)).timestamp()
    frozen_clock(T0 + timedelta(minutes=30))
    assert listing(session_client)[SPACE]["state"] == "stopped"
    hb(host)
    assert listing(session_client)[SPACE]["state"] == "online"


def test_goodbye_without_a_heartbeat_creates_nothing(host, session_client):
    assert bye(host).status_code == 204
    assert listing(session_client)[SPACE]["state"] == "never_started"


def test_optional_numbers_may_be_absent_and_stay_unknown(host, session_client):
    r = host.post(f"/api/presence/{SPACE}/heartbeat",
                  json={"sessions": 0, "in_progress": 0, "needs_you": 0, "factory": "none"})
    assert r.status_code == 204
    s = listing(session_client)[SPACE]
    assert s["children_done"] is None and s["children_total"] is None and s["budget_pct"] is None
    assert s["sessions"] == 0


@pytest.mark.parametrize("over", [
    {"extra": "x"}, {"name": "my workspace"}, {"factory": "busy"}, {"factory": None}, {"sessions": -1},
    {"sessions": True}, {"sessions": 1.5}, {"sessions": "3"}, {"in_progress": 10_000}, {"budget_pct": 1000},
    {"children_done": 5, "children_total": 4}, {"children_done": 1, "children_total": None},
    {"needs_you": None},
])
def test_the_heartbeat_schema_is_strict(host, over):
    body = {**BEAT, **over}
    r = host.post(f"/api/presence/{SPACE}/heartbeat", json=body)
    assert r.status_code == 400 and r.json()["error"] == "bad_request", (over, r.text)


def test_a_missing_required_field_is_refused(host):
    for k in ("sessions", "in_progress", "needs_you", "factory"):
        body = {x: v for x, v in BEAT.items() if x != k}
        assert host.post(f"/api/presence/{SPACE}/heartbeat", json=body).status_code == 400, k


def test_bodies_must_be_small_objects(host):
    assert host.post(f"/api/presence/{SPACE}/heartbeat", content=b"x" * 600,
                     headers={"content-type": "application/json"}).status_code == 413
    assert host.post(f"/api/presence/{SPACE}/heartbeat", json=[1]).status_code == 400
    assert host.post(f"/api/presence/{SPACE}/heartbeat", content=b"nope",
                     headers={"content-type": "application/json"}).status_code == 400
    assert bye(host, json={"note": "hi"}).status_code == 400


def test_the_rate_is_capped_per_device(host):
    codes = [hb(host).status_code for _ in range(pr.BEATS_PER_MIN + 3)]
    assert codes[:pr.BEATS_PER_MIN] == [204] * pr.BEATS_PER_MIN
    assert set(codes[pr.BEATS_PER_MIN:]) == {429}


def test_a_refused_heartbeat_changes_nothing(host, session_client):
    hb(host)
    assert hb(host, sessions=-5).status_code == 400
    assert listing(session_client)[SPACE]["sessions"] == 2


def test_nothing_but_the_clear_fields_is_stored(host, settings):
    hb(host)
    conn = sqlite3.connect(settings.db_path)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(presence)")]
    finally:
        conn.close()
    assert set(cols) == {"space_id", "device_id", "hb_at", "bye_at", *pr.FIELDS}


def test_the_listing_covers_every_space(host, session_client):
    host.post("/api/spaces", json={"id": SPACE2, "key_version": 1, "enc_label": fake_env()})
    hb(host, space=SPACE)
    got = listing(session_client)
    assert got[SPACE]["state"] == "online" and got[SPACE2]["state"] == "never_started"
    assert got[SPACE2]["sessions"] is None                          # never reported: unknown, not zero


def test_a_former_owners_heartbeat_does_not_count_after_a_takeover(host, session_client, settings, other_device):
    hb(host)
    assert listing(session_client)[SPACE]["state"] == "online"
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute("UPDATE spaces SET owner_device = ? WHERE id = ?", (other_device.id, SPACE))
        conn.commit()
    finally:
        conn.close()
    s = listing(session_client)[SPACE]
    assert s["state"] == "never_started" and s["sessions"] is None


def test_the_migration_is_idempotent(settings):
    from fileshare.db import connect
    sql = (__import__("pathlib").Path(pr.__file__).parent / "migrations" / "010_presence.sql").read_text()
    conn = connect(settings.db_path)
    try:
        conn.executescript(sql)
        conn.executescript(sql)
    finally:
        conn.close()
