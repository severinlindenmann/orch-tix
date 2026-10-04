"""Web Push: the pushworker subprocess, subscriptions, and the SubprocessPusher trigger (spec T7)."""
import dataclasses
import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from fileshare import push as push_module
from fileshare.db import connect, get_meta
from fileshare.security import b64u_decode, b64u_encode
from tests.helpers.tickets import claim, create, device_client, post_event  # noqa: F401

pytestmark = pytest.mark.real_pusher   # these tests are about SubprocessPusher itself

REPO = Path(__file__).resolve().parent.parent


def _genkeys() -> dict:
    proc = subprocess.run([sys.executable, "-m", "fileshare.pushworker", "genkeys"],
                          capture_output=True, text=True, timeout=30, cwd=REPO, check=True)
    return json.loads(proc.stdout)


def _receiver_keys() -> tuple[str, str]:
    """A plausible browser subscription keypair: p256dh (uncompressed P-256 point) and auth secret."""
    key = ec.generate_private_key(ec.SECP256R1())
    point = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return b64u_encode(point), b64u_encode(os.urandom(16))


# --- genkeys -------------------------------------------------------------------------------------

def test_genkeys_output_is_a_usable_keypair():
    keys = _genkeys()
    from py_vapid import Vapid
    Vapid.from_string(keys["private"])          # parseable; raises if not
    pub = b64u_decode(keys["public"])
    assert len(pub) == 65 and pub[0] == 0x04


# --- send: a local HTTP push endpoint -------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    requests: list = []
    status_for: dict = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        # self.headers is an email.message.Message: keep it (not dict(...)) so header lookups
        # below stay case-insensitive, matching HTTP semantics (pywebpush sends lowercase names).
        _Handler.requests.append({"path": self.path, "headers": self.headers, "body": body})
        self.send_response(_Handler.status_for.get(self.path, 201))
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def push_server():
    _Handler.requests = []
    _Handler.status_for = {}
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield server
    finally:
        server.shutdown()
        t.join(timeout=5)


def _run_send(req: dict, allow_http: bool = True) -> dict:
    env = dict(os.environ)
    if allow_http:
        env["FS_PUSH_ALLOW_HTTP"] = "1"
    proc = subprocess.run([sys.executable, "-m", "fileshare.pushworker", "send"],
                          input=json.dumps(req), capture_output=True, text=True, timeout=30,
                          cwd=REPO, env=env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_send_headers_and_a_gone_subscription(push_server):
    port = push_server.server_address[1]
    keys = _genkeys()
    p256dh, auth = _receiver_keys()
    _Handler.status_for["/gone"] = 410
    req = {
        "private": keys["private"],
        "sub": "https://tix.severin.io",
        "subs": [
            {"id": "psh_ok0000000", "endpoint": f"http://127.0.0.1:{port}/ok",
             "keys": {"p256dh": p256dh, "auth": auth}},
            {"id": "psh_gone00000", "endpoint": f"http://127.0.0.1:{port}/gone",
             "keys": {"p256dh": p256dh, "auth": auth}},
        ],
        "payload": json.dumps({"t": "TIX-1", "s": "waiting", "p": "proj", "k": "question", "n": 1}),
        "ttl": 86400,
    }
    result = _run_send(req)
    assert result == {"gone": ["psh_gone00000"], "failed": []}
    assert len(_Handler.requests) == 2
    ok = next(r for r in _Handler.requests if r["path"] == "/ok")
    assert ok["headers"]["Authorization"].startswith("vapid t=")
    assert ok["headers"]["Content-Encoding"] == "aes128gcm"
    assert ok["headers"]["TTL"] == "86400"


def test_send_refuses_http_without_the_env_flag(push_server):
    port = push_server.server_address[1]
    keys = _genkeys()
    p256dh, auth = _receiver_keys()
    req = {
        "private": keys["private"], "sub": "https://tix.severin.io",
        "subs": [{"id": "psh_x", "endpoint": f"http://127.0.0.1:{port}/ok",
                  "keys": {"p256dh": p256dh, "auth": auth}}],
        "payload": "{}", "ttl": 86400,
    }
    result = _run_send(req, allow_http=False)
    assert result == {"gone": [], "failed": ["psh_x"]}
    assert _Handler.requests == []


# --- /api/push/subscribe and /api/push/vapid ------------------------------------------------------

def test_subscribe_upsert_and_delete(session_client):
    p256dh, auth = _receiver_keys()
    body = {"endpoint": "https://push.example.com/ep1", "keys": {"p256dh": p256dh, "auth": auth}}
    r1 = session_client.post("/api/push/subscribe", json=body)
    assert r1.status_code == 201
    sub_id = r1.json()["id"]
    r2 = session_client.post("/api/push/subscribe", json=body)   # same endpoint: upsert
    assert r2.status_code == 201 and r2.json()["id"] == sub_id
    r3 = session_client.request("DELETE", "/api/push/subscribe", json={"endpoint": body["endpoint"]})
    assert r3.status_code == 204


def test_subscribe_non_https_endpoint_is_400(session_client):
    p256dh, auth = _receiver_keys()
    r = session_client.post("/api/push/subscribe",
                            json={"endpoint": "http://push.example.com/ep",
                                  "keys": {"p256dh": p256dh, "auth": auth}})
    assert r.status_code == 400


def test_subscribe_refused_for_a_device(device_client):
    p256dh, auth = _receiver_keys()
    r = device_client.post("/api/push/subscribe",
                           json={"endpoint": "https://push.example.com/ep",
                                 "keys": {"p256dh": p256dh, "auth": auth}})
    assert r.status_code in (401, 403)


def test_vapid_generates_once_and_is_stable(session_client):
    r1 = session_client.get("/api/push/vapid")
    r2 = session_client.get("/api/push/vapid")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["public_key"] == r2.json()["public_key"]
    pub = b64u_decode(r1.json()["public_key"])
    assert len(pub) == 65 and pub[0] == 0x04


# --- SubprocessPusher: gone-id pruning and the notify() -> _deliver dispatch -----------------------

class _SyncThread:
    """Runs the target immediately: makes SubprocessPusher.notify() deterministic in tests."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def test_subprocess_pusher_deletes_gone_subscriptions(app, settings, monkeypatch):
    conn = connect(settings.db_path)
    conn.execute("INSERT INTO push_subs (id, session_name, endpoint, p256dh, auth, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("psh_a", "browser", "https://push.example/a", "p", "a", "2026-01-01T00:00:00Z"))
    conn.execute("INSERT INTO push_subs (id, session_name, endpoint, p256dh, auth, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("psh_b", "browser", "https://push.example/b", "p", "a", "2026-01-01T00:00:00Z"))
    conn.close()

    # Pre-generate the VAPID keys with the real worker, so _deliver's ensure_vapid() call below
    # hits meta and never calls the monkeypatched subprocess.run itself.
    conn = connect(settings.db_path)
    push_module.ensure_vapid(conn)
    conn.close()

    calls = []

    def fake_run(cmd, input, capture_output, text, timeout):
        calls.append(json.loads(input))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"gone": ["psh_a"], "failed": []}))

    monkeypatch.setattr(push_module.subprocess, "run", fake_run)
    pusher = push_module.SubprocessPusher(settings, settings.db_path)
    pusher._deliver({"id": "TIX-1", "status": "waiting", "project": "proj", "open_questions": 1},
                    {"kind": "question"})

    assert len(calls) == 1
    conn = connect(settings.db_path)
    assert calls[0]["private"] == get_meta(conn, "vapid_private")   # never the public key
    conn.close()
    ids = sorted(s["id"] for s in calls[0]["subs"])
    assert ids == ["psh_a", "psh_b"]

    conn = connect(settings.db_path)
    left = {r["id"] for r in conn.execute("SELECT id FROM push_subs")}
    conn.close()
    assert left == {"psh_b"}


def test_subprocess_pusher_delivers_for_real(app, settings, push_server, monkeypatch):
    """No stub: _deliver runs the real pushworker with the stored VAPID keys, and the local push
    endpoint receives a VAPID-signed request (a public key passed as private would fail here)."""
    port = push_server.server_address[1]
    p256dh, auth = _receiver_keys()
    conn = connect(settings.db_path)
    conn.execute("INSERT INTO push_subs (id, session_name, endpoint, p256dh, auth, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("psh_real", "browser", f"http://127.0.0.1:{port}/real", p256dh, auth,
                 "2026-01-01T00:00:00Z"))
    conn.close()
    monkeypatch.setenv("FS_PUSH_ALLOW_HTTP", "1")
    # VAPID needs an https: (or mailto:) sub claim, as FS_PUBLIC_URL is in production.
    https = dataclasses.replace(settings, public_url="https://tix.severin.io")
    pusher = push_module.SubprocessPusher(https, settings.db_path)
    pusher._deliver({"id": "TIX-1", "status": "waiting", "project": "proj", "open_questions": 1},
                    {"kind": "question"})
    got = [r for r in _Handler.requests if r["path"] == "/real"]
    assert len(got) == 1
    assert got[0]["headers"]["Authorization"].startswith("vapid t=")
    assert got[0]["headers"]["Content-Encoding"] == "aes128gcm"


def test_pusher_notified_for_waiting_testing_and_h_close_not_update_or_failed_test(
        app, session_client, device_client, monkeypatch):
    assert isinstance(app.state.pusher, push_module.SubprocessPusher)
    monkeypatch.setattr(push_module.threading, "Thread", _SyncThread)
    calls = []
    monkeypatch.setattr(
        push_module.SubprocessPusher, "_deliver",
        lambda self, ticket, event: calls.append((ticket["id"], ticket["status"], event["kind"])))

    t = create(session_client, status="open")
    tok = claim(device_client, t["id"])
    post_event(device_client, t["id"], "update", claim=tok)
    post_event(device_client, t["id"], "test", claim=tok, passed=False, manual=False)
    assert calls == []

    post_event(device_client, t["id"], "question", claim=tok, question_count=1, status_to="waiting")
    assert calls == [(t["id"], "waiting", "question")]

    post_event(session_client, t["id"], "answer")
    post_event(device_client, t["id"], "test", claim=tok, passed=True, manual=False)
    assert calls[-1] == (t["id"], "testing", "test")

    post_event(device_client, t["id"], "status", claim=tok, status_to="done")   # H closes: push
    assert calls[-1] == (t["id"], "done", "status")


def test_a_failing_worker_never_fails_the_notify_call(app, settings, monkeypatch):
    conn = connect(settings.db_path)
    conn.execute("INSERT INTO push_subs (id, session_name, endpoint, p256dh, auth, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("psh_a", "browser", "https://push.example/a", "p", "a", "2026-01-01T00:00:00Z"))
    conn.close()

    conn = connect(settings.db_path)
    push_module.ensure_vapid(conn)
    conn.close()

    def boom(*a, **kw):
        raise OSError("no subprocess for you")

    monkeypatch.setattr(push_module.subprocess, "run", boom)
    pusher = push_module.SubprocessPusher(settings, settings.db_path)
    pusher._deliver({"id": "TIX-1", "status": "waiting", "project": "proj", "open_questions": 1},
                    {"kind": "question"})   # must not raise


def test_a_failing_genkeys_never_escapes_and_logs(app, settings, monkeypatch, caplog):
    """ensure_vapid()'s subprocess call (genkeys, on first use) must not escape the daemon
    thread, and the spec's `push: <n> failed` line must still be logged (Task 4 fix round 1)."""
    conn = connect(settings.db_path)
    conn.execute("INSERT INTO push_subs (id, session_name, endpoint, p256dh, auth, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("psh_a", "browser", "https://push.example/a", "p", "a", "2026-01-01T00:00:00Z"))
    conn.close()
    # No pre-generated VAPID keys: _deliver's ensure_vapid() call must hit the (faked) genkeys.

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="pushworker genkeys", timeout=30)

    monkeypatch.setattr(push_module.subprocess, "run", boom)
    monkeypatch.setattr(push_module.threading, "Thread", _SyncThread)
    caplog.set_level("WARNING", logger="fileshare.push")

    pusher = push_module.SubprocessPusher(settings, settings.db_path)
    pusher.notify({"id": "TIX-1", "status": "waiting", "project": "proj", "open_questions": 1},
                  {"kind": "question"})   # runs synchronously via _SyncThread; must not raise

    assert any("push: 1 failed" in r.message for r in caplog.records)
    conn = connect(settings.db_path)
    left = {r["id"] for r in conn.execute("SELECT id FROM push_subs")}
    conn.close()
    assert left == {"psh_a"}   # a genkeys failure never touches push_subs


def _recording_pusher(monkeypatch):
    monkeypatch.setattr(push_module.threading, "Thread", _SyncThread)
    calls = []
    monkeypatch.setattr(
        push_module.SubprocessPusher, "_deliver",
        lambda self, ticket, event: calls.append((ticket["id"], ticket["status"], event)))
    return calls


def test_a_web_move_into_waiting_or_testing_never_pushes(app, session_client, device_client, monkeypatch):
    """Severin's own moves in the browser don't notify his phone; a device's still do (M1)."""
    calls = _recording_pusher(monkeypatch)
    t = create(session_client, status="open")
    post_event(session_client, t["id"], "status", status_to="waiting")
    post_event(session_client, t["id"], "status", status_to="testing")
    post_event(session_client, t["id"], "status", status_to="in-progress")
    assert calls == []

    u = create(session_client, status="open")
    tok = claim(device_client, u["id"])
    post_event(device_client, u["id"], "question", claim=tok, question_count=1, status_to="waiting")
    assert [(c[1], c[2]["kind"]) for c in calls] == [("waiting", "question")]


def test_a_manual_test_push_carries_m(app, session_client, device_client, monkeypatch):
    """A manual test moves to testing without any command run: the event handed to the pusher says
    so (M2), and a passed automatic test doesn't."""
    calls = _recording_pusher(monkeypatch)
    t = create(session_client, status="open")
    tok = claim(device_client, t["id"])
    post_event(device_client, t["id"], "test", claim=tok, passed=False, manual=True)
    assert len(calls) == 1 and calls[0][1] == "testing" and calls[0][2].get("manual") is True

    u = create(session_client, status="open")
    tok = claim(device_client, u["id"])
    r = post_event(device_client, u["id"], "test", claim=tok, passed=True, manual=False)
    assert calls[-1][1] == "testing" and not calls[-1][2].get("manual")
    assert "manual" not in r   # EventOut itself is unchanged


def test_the_push_payload_has_m_only_for_a_manual_test(app, settings, monkeypatch):
    conn = connect(settings.db_path)
    conn.execute("INSERT INTO push_subs (id, session_name, endpoint, p256dh, auth, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("psh_a", "browser", "https://push.example/a", "p", "a", "2026-01-01T00:00:00Z"))
    push_module.ensure_vapid(conn)
    conn.close()
    sent = []

    def fake_run(cmd, input, capture_output, text, timeout):
        sent.append(json.loads(json.loads(input)["payload"]))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"gone": [], "failed": []}))

    monkeypatch.setattr(push_module.subprocess, "run", fake_run)
    pusher = push_module.SubprocessPusher(settings, settings.db_path)
    ticket = {"id": "TIX-1", "status": "testing", "project": "proj", "open_questions": 0}
    pusher._deliver(ticket, {"kind": "test", "manual": True})
    pusher._deliver(ticket, {"kind": "test"})
    assert sent[0] == {"t": "TIX-1", "s": "testing", "p": "proj", "k": "test", "n": 0, "m": True}
    assert sent[1] == {"t": "TIX-1", "s": "testing", "p": "proj", "k": "test", "n": 0}
