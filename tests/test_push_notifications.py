"""Push notification behaviour (QA pass 2026-10-05): a clear only for tickets the phone was shown, ordered delivery with
a Topic header, and a read message withdraws its notification. Everything is local: in-process app, a recording
pusher, and a fake HTTP push endpoint (127.0.0.1) for the real SubprocessPusher."""
import hashlib
import http.server
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from fileshare import push as push_module
from fileshare.db import connect
from fileshare.security import b64u_encode
from tests.helpers.tickets import device_client, fake_dek, fake_env, new_uuid, other_device, other_device_client  # noqa: F401

SPACE = "e" * 32
DEK = fake_dek()
T0 = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


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


def _space(dc, sid=SPACE):
    dc.post("/api/spaces", json={"id": sid, "key_version": 1, "enc_label": fake_env()})


def _put(dc, u, rev, needs, oq=0, space=SPACE):
    return dc.put(f"/api/mirrors/{u}", json={
        "space": space, "mirror_rev": rev, "schema_version": "1.0.0", "status": "waiting", "priority": "normal",
        "needs": needs, "notify": True, "open_questions": oq, "key_version": 1, "wrapped_dek": DEK, "enc_content": fake_env(90),
        "event_uuid": hashlib.sha256(f"{u}{rev}".encode()).hexdigest()[:32]})


def _decide(sc, ticket, space=SPACE, kind="answer"):
    return sc.post("/api/decisions", json={"uuid": new_uuid(), "space": space, "ticket": ticket, "kind": kind,
                                           "key_version": 1, "enc_body": fake_env(80)})


# ---- Scenario 1: handled on the desktop / MC / CLI = the addon syncs needs=None ------------------------------------
def test_handled_on_desktop_sends_clear(frozen_clock, device_client, pushes):
    frozen_clock(T0)
    _space(device_client)
    u = new_uuid()
    tix = _put(device_client, u, 1, "question", 1).json()["id"]
    assert pushes[-1]["k"] == "question"
    _put(device_client, u, 2, None)                       # desktop answered; addon syncs needs=None
    assert pushes[-1] == {"v": 2, "s": SPACE, "t": tix, "k": "clear", "n": 0, "c": 0, "cs": 0}


# ---- Scenario 2: phone answers; desktop applies; the phone is told "Handled on desktop" ---------------------------
def test_phone_decision_itself_sends_no_push_but_the_apply_sends_clear(frozen_clock, device_client, session_client, pushes):
    frozen_clock(T0)
    _space(device_client)
    u = new_uuid()
    tix = _put(device_client, u, 1, "question", 1).json()["id"]
    n0 = len(pushes)
    assert _decide(session_client, tix).status_code == 201
    assert len(pushes) == n0                              # the decision creates no push (good)
    _put(device_client, u, 2, None)                       # desktop applied it
    assert pushes[-1]["k"] == "clear"                     # ...and the phone that just answered gets "Handled on desktop"


# ---- Scenario 3: clear for a ticket whose "needs you" push was held by the 60 s window -----------------------------
def test_no_clear_for_a_ticket_that_was_never_pushed(frozen_clock, device_client, pushes):
    frozen_clock(T0)
    _space(device_client)
    ua, ub = new_uuid(), new_uuid()
    _put(device_client, ua, 1, "question", 1)             # pushed at once (leading edge)
    tb = _put(device_client, ub, 1, "question", 1).json()["id"]   # held inside the window
    assert len(pushes) == 1
    _put(device_client, ub, 2, None)                      # handled before the window ended
    assert not [p for p in pushes if p["k"] == "clear" and p["t"] == tb]


def test_held_then_cleared_ticket_is_not_in_the_batch(frozen_clock, device_client, app, pushes):
    frozen_clock(T0)
    _space(device_client)
    ua, ub, uc = new_uuid(), new_uuid(), new_uuid()
    _put(device_client, ua, 1, "question", 1)
    _put(device_client, ub, 1, "question", 1)
    _put(device_client, uc, 1, "approval")
    _put(device_client, ub, 2, None)
    app.state.needs_push_gate._trail(SPACE, lambda sp, held: __import__("fileshare.mirrors", fromlist=["x"])._flush_held(app, sp, held))
    last = pushes[-1]
    assert last["k"] != "batch" or "TIX-%s" % 0 not in last.get("ts", [])


# ---- Scenario 4: a human reads a message on the desktop/web: does the phone notification go away? ----------------
def test_message_read_on_web_withdraws_the_phone_notification(device_client, session_client, pushes):
    _space(device_client)
    mid = device_client.post("/api/messages", json={"uuid": new_uuid(), "to_kind": "human", "to_id": "", "kind": "text",
                                                    "key_version": 1, "enc_body": fake_env(), "space": SPACE}).json()["id"]
    assert pushes[-1]["k"] == "message"
    n = len(pushes)
    assert session_client.post(f"/api/messages/{mid}/ack").status_code in (200, 204)
    assert len(pushes) == n + 1 and pushes[-1]["k"] == "clear"


def test_same_needs_resync_does_not_push_again(frozen_clock, device_client, pushes):
    frozen_clock(T0)
    _space(device_client)
    u = new_uuid()
    _put(device_client, u, 1, "question", 1)
    _put(device_client, u, 2, "question", 1)
    _put(device_client, u, 3, "question", 3)              # more open questions, same kind
    assert len(pushes) == 1


def test_flap_none_question_pushes_again_within_window_is_held(frozen_clock, device_client, pushes):
    """A need that flaps question -> none -> question inside 60 s: clear goes out at once, the second need is held."""
    frozen_clock(T0)
    _space(device_client)
    u = new_uuid()
    _put(device_client, u, 1, "question", 1)
    _put(device_client, u, 2, None)
    _put(device_client, u, 3, "question", 1)
    kinds = [p["k"] for p in pushes]
    assert kinds == ["question", "clear"]                 # the re-need is held, a trailing summary follows at +60 s


# ---- Scenario 7: restart loses held pushes (in-memory gate) ---------------------------------------------------

class _H(http.server.BaseHTTPRequestHandler):
    log: list = []
    status_for: dict = {}
    delay_for: dict = {}

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        _H.log.append({"t": time.time(), "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                       "body": body})
        self.send_response(_H.status_for.get(self.path, 201))
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def endpoint():
    _H.log, _H.status_for = [], {}
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _sub(conn, endpoint_url, idx):
    key = ec.generate_private_key(ec.SECP256R1())
    pt = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    conn.execute("INSERT INTO push_subs (id, session_name, endpoint, p256dh, auth, created_at) VALUES (?,?,?,?,?,?)",
                 (f"psh_{idx:012d}", "qa-fake", endpoint_url, b64u_encode(pt), b64u_encode(os.urandom(16)), "2026-10-05T00:00:00Z"))
    return key


@pytest.mark.real_pusher
def test_real_pusher_headers_and_pruning(app, settings, endpoint, monkeypatch):
    monkeypatch.setenv("FS_PUSH_ALLOW_HTTP", "1")
    port = endpoint.server_address[1]
    conn = connect(settings.db_path)
    for i, path in enumerate(("/ok", "/gone", "/bad", "/forbidden")):
        _sub(conn, f"http://127.0.0.1:{port}{path}", i)
    _H.status_for.update({"/gone": 410, "/bad": 400, "/forbidden": 403})
    p = push_module.SubprocessPusher(__import__('dataclasses').replace(settings, public_url='https://tix.example'), settings.db_path)
    p._deliver_payload(json.dumps({"v": 2, "s": SPACE, "t": "TIX-1", "k": "question", "n": 1, "c": 1}))
    left = {r["id"] for r in conn.execute("SELECT id FROM push_subs")}
    assert "psh_000000000001" not in left                 # 410 pruned
    assert {"psh_000000000002", "psh_000000000003"} <= left   # 400/403 (Apple's BadJwt/Forbidden) are kept forever
    ok = next(r for r in _H.log if r["path"] == "/ok")["headers"]
    assert ok["ttl"] == "86400"
    if "topic" in ok:
        pytest.skip("patched main: Topic header present (see N-03)")
    assert "urgency" not in ok                            # no collapse key: an offline phone queues every push




@pytest.mark.real_pusher
def test_clear_never_overtakes_needs_and_topic_is_set(app, settings, endpoint, monkeypatch):
    """Needs for patched main (patches/N-03): ordered delivery and a per-ticket Topic header."""
    monkeypatch.setenv("FS_PUSH_ALLOW_HTTP", "1")
    port = endpoint.server_address[1]
    conn = connect(settings.db_path)
    priv = _sub(conn, f"http://127.0.0.1:{port}/ok", 0)
    import http_ece
    from fileshare.security import b64u_decode
    p = push_module.SubprocessPusher(__import__('dataclasses').replace(settings, public_url='https://tix.example'), settings.db_path)
    for i in range(6):
        _H.log.clear()
        p.notify_payload({"v": 2, "s": SPACE, "t": f"TIX-{i}", "k": "question", "n": 1, "c": 1})
        p.notify_payload({"v": 2, "s": SPACE, "t": f"TIX-{i}", "k": "clear", "n": 0, "c": 0})
        deadline = time.time() + 20
        while len(_H.log) < 2 and time.time() < deadline:
            time.sleep(0.05)
        auth = b64u_decode(conn.execute("SELECT auth FROM push_subs").fetchone()["auth"])
        kinds = [json.loads(http_ece.decrypt(r["body"], private_key=priv, auth_secret=auth, version="aes128gcm"))["k"]
                 for r in sorted(_H.log, key=lambda r: r["t"])]
        assert kinds == ["question", "clear"]
        topics = {r["headers"].get("topic") for r in _H.log}
        assert len(topics) == 1 and None not in topics and len(next(iter(topics))) <= 32
