"""Tickets test helpers: valid-looking (never decryptable) envelopes, device clients, create/claim/event.

The server only format-checks envelopes, so random bytes behind the v1 version byte are enough.
Import the fixtures into a test module (`from tests.helpers.tickets import device_client, ...`).
"""
import os

import pytest
from fastapi.testclient import TestClient

from fileshare.security import b64u_encode
from tests.helpers.onboard import onboard_device

OMIT = object()   # pass as a keyword value to drop that field from the request body


def b64u(b: bytes) -> str:
    return b64u_encode(b)


def new_uuid() -> str:
    return os.urandom(16).hex()


def fake_dek() -> str:
    return b64u(b"\x01" + os.urandom(12 + 32 + 16))


def fake_env(n: int = 40) -> str:
    return b64u(b"\x01" + os.urandom(n))


def _body(base: dict, kw: dict) -> dict:
    base.update(kw)
    return {k: v for k, v in base.items() if v is not OMIT}


def create(client, status="open", raw=False, headers=None, **kw):
    body = _body({"uuid": new_uuid(), "key_version": 1, "wrapped_dek": fake_dek(),
                  "enc_content": fake_env(), "status": status, "project": "proj",
                  "event_uuid": new_uuid()}, kw)
    r = client.post("/api/tickets", json=body, headers=headers or {})
    if raw:
        return r
    assert r.status_code == 201, r.text
    return r.json()


def post_event(client, ref, kind, raw=False, claim=None, **kw):
    body = _body({"uuid": new_uuid(), "kind": kind, "enc_body": fake_env()}, kw)
    if kind == "test":
        body.setdefault("passed", True)
        body.setdefault("manual", False)
    headers = {"X-Claim-Token": claim} if claim else {}
    r = client.post(f"/api/tickets/{ref}/events", json=body, headers=headers)
    if raw:
        return r
    assert r.status_code == 201, r.text
    return r.json()


def claim(client, ref, takeover=False, raw=False, **kw):
    body = _body({"event_uuid": new_uuid()}, kw)
    if takeover:
        body["takeover"] = True
    r = client.post(f"/api/tickets/{ref}/claim", json=body)
    if raw:
        return r
    assert r.status_code == 200, r.text
    return r.json()["claim_token"]


def get(client, ref) -> dict:
    r = client.get(f"/api/tickets/{ref}")
    assert r.status_code == 200, r.text
    return r.json()


def device_client_for(app, creds) -> TestClient:
    """A client that speaks as the device: its bearer on every request, no session cookie."""
    return TestClient(app, base_url="http://testserver",
                      headers={"Authorization": f"Bearer {creds.token}"})


@pytest.fixture
def device_client(app, device):
    return device_client_for(app, device)


@pytest.fixture
def other_device(owner, sharing):
    return onboard_device(owner, sharing, name="desktop", project="other")


@pytest.fixture
def other_device_client(app, other_device):
    return device_client_for(app, other_device)


# --- legacy tickets seeded through a live server, for `sharing tickets migrate` (Task 12) ------------------

STATUSES = ("backlog", "open", "in-progress", "waiting", "testing", "done")


def sim_ticket(sim, *, project, title, status, body="", fm=None, labels=(), device=None, questions=None) -> str:
    """A legacy ticket created by the browser (content sealed under MK like the PWA does), then moved to
    `status`. With `questions` (a validated question body) the `device` (a DeviceRepo) claims it and asks them,
    which is how a ticket really reaches waiting. Returns its TIX id."""
    import httpx
    s = sim.s
    tc = s.new_ticket_crypto(sim.mk, {"title": title, "body": body, "fm": fm or {}})
    r = sim.request("POST", "/api/tickets", json={
        "uuid": tc["uuid"], "key_version": 1, "wrapped_dek": tc["wrapped_dek"], "enc_content": tc["enc_content"],
        "status": "open", "project": project, "labels": list(labels), "event_uuid": new_uuid()})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    if questions is not None:
        dev = httpx.Client(base_url=str(sim.http.base_url), headers={"Authorization": f"Bearer {device.token}"})
        c = dev.post(f"/api/tickets/{tid}/claim", json={"event_uuid": new_uuid()})
        assert c.status_code == 200, c.text
        eu = os.urandom(16)
        q = dev.post(f"/api/tickets/{tid}/events", headers={"X-Claim-Token": c.json()["claim_token"]}, json={
            "uuid": eu.hex(), "kind": "question", "question_count": len(questions["questions"]),
            "enc_body": s.seal_ticket_event(tc["_dek"], bytes.fromhex(tc["uuid"]), eu, questions)})
        assert q.status_code == 201, q.text
    if status != ("waiting" if questions is not None else "open"):
        m = sim.request("POST", f"/api/tickets/{tid}/events",
                        json={"uuid": new_uuid(), "kind": "status", "status_to": status})
        assert m.status_code == 201, m.text
    return tid


def sim_list(sim) -> list[dict]:
    """Every live legacy ticket with its events and archived_at, as the browser sees them."""
    q = "&".join(f"status={x}" for x in STATUSES)
    rows = sim.request("GET", f"/api/tickets?{q}").json()["tickets"]
    return [sim.request("GET", f"/api/tickets/{t['id']}").json() for t in rows]
