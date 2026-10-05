"""QA N-06 (subscription health, dedupe) and N-08 (held pushes survive a restart)."""
import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from fileshare import heldpush, messages, mirrors
from fileshare import push as push_module
from fileshare.db import connect
from tests.helpers.tickets import device_client, fake_env, new_uuid  # noqa: F401
from tests.test_push_v2 import SPACE, _human_msg, _put, pushes  # noqa: F401

OLD = "2026-01-01T00:00:00Z"


def _sub(conn, sid, endpoint="https://push.example/x", created=OLD):
    conn.execute("INSERT INTO push_subs (id, session_name, endpoint, p256dh, auth, created_at) VALUES (?,?,?,?,?,?)",
                 (sid, "phone", endpoint, "p", "a", created))


def _worker(monkeypatch, result):
    def fake_run(cmd, input, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(result))
    monkeypatch.setattr(push_module.subprocess, "run", fake_run)


def _row(settings, sid):
    c = connect(settings.db_path)
    try:
        return c.execute("SELECT * FROM push_subs WHERE id = ?", (sid,)).fetchone()
    finally:
        c.close()


@pytest.mark.real_pusher
def test_a_failing_subscription_counts_up_logs_its_id_and_resets_on_success(app, settings, monkeypatch, caplog):
    c = connect(settings.db_path)
    _sub(c, "psh_a")
    push_module.ensure_vapid(c)
    c.close()
    p = push_module.SubprocessPusher(settings, settings.db_path)
    _worker(monkeypatch, {"gone": [], "failed": ["psh_a"], "ok": [], "errors": {"psh_a": "403"}})
    with caplog.at_level(logging.WARNING, logger="fileshare.push"):
        p._deliver_payload('{"v":2,"k":"question"}')
    assert "psh_a" in caplog.text and "403" in caplog.text
    r = _row(settings, "psh_a")
    assert r["failure_count"] == 1 and r["last_failure_at"] and r["last_success_at"] is None
    _worker(monkeypatch, {"gone": [], "failed": [], "ok": ["psh_a"], "errors": {}})
    p._deliver_payload('{"v":2,"k":"question"}')
    r = _row(settings, "psh_a")
    assert r["failure_count"] == 0 and r["last_success_at"]


@pytest.mark.real_pusher
def test_repeated_failures_over_days_drop_the_subscription_but_a_recent_one_stays(frozen_clock, app, settings, monkeypatch):
    frozen_clock(datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc))
    c = connect(settings.db_path)
    _sub(c, "psh_old", "https://push.example/old", created=OLD)
    _sub(c, "psh_new", "https://push.example/new", created="2026-09-24T11:00:00Z")      # the test clock is 2026-09-24
    push_module.ensure_vapid(c)
    c.close()
    p = push_module.SubprocessPusher(settings, settings.db_path)
    _worker(monkeypatch, {"gone": [], "failed": ["psh_old", "psh_new"], "ok": [], "errors": {}})
    for _ in range(push_module.PRUNE_AFTER_FAILURES):
        p._deliver_payload('{"v":2,"k":"question"}')
    assert _row(settings, "psh_old") is None                       # 5 failures in a row and no success for > 7 days
    assert _row(settings, "psh_new")["failure_count"] == push_module.PRUNE_AFTER_FAILURES   # a short outage keeps it


def test_resubscribing_from_the_same_session_replaces_the_row(session_client, settings):
    body = lambda ep: {"endpoint": ep, "keys": {"p256dh": "AAAA", "auth": "BBBB"}}
    assert session_client.post("/api/push/subscribe", json=body("https://push.example/1")).status_code == 201
    assert session_client.post("/api/push/subscribe", json=body("https://push.example/2")).status_code == 201
    c = connect(settings.db_path)
    rows = c.execute("SELECT endpoint, session_hash FROM push_subs").fetchall()
    c.close()
    assert [r["endpoint"] for r in rows] == ["https://push.example/2"] and rows[0]["session_hash"]
    assert session_client.post("/api/push/subscribe", json=body("https://push.example/2")).status_code == 201   # same endpoint
    c = connect(settings.db_path)
    assert c.execute("SELECT COUNT(*) FROM push_subs").fetchone()[0] == 1
    c.close()


# ---- N-08

def _restart(app, settings):
    """A new process: fresh in-memory gates over the same database."""
    app.state.needs_push_gate = mirrors.NeedsPushGate(mirrors.NEEDS_PUSH_EVERY_S, app.state.held_store)
    app.state.held_store = heldpush.HeldStore(settings.db_path)


def test_a_restart_inside_the_window_still_sends_the_held_needs(frozen_clock, app, settings, device_client, pushes):
    app.state.needs_push_gate.timer = lambda delay, fn: None            # the timer dies with the process
    t0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
    frozen_clock(t0)
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    tix = [_put(device_client, new_uuid(), 1, "question", 1).json()["id"] for _ in range(3)]
    assert [p["k"] for p in pushes] == ["question"]                    # one now, two held
    c = connect(settings.db_path)
    assert c.execute("SELECT COUNT(*) FROM push_held WHERE kind = 'needs'").fetchone()[0] == 2
    c.close()
    _restart(app, settings)
    assert mirrors.recover_held(app) == 2
    assert pushes[-1] == {"v": 2, "s": SPACE, "t": "", "k": "batch", "n": 2, "c": 3, "cs": 3, "ts": tix[1:]}
    assert mirrors.recover_held(app) == 0                              # sent once


def test_a_held_ticket_handled_before_the_restart_is_not_resent(frozen_clock, app, settings, device_client, pushes):
    app.state.needs_push_gate.timer = lambda delay, fn: None
    frozen_clock(datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc))
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    _put(device_client, new_uuid(), 1, "question", 1)
    u = new_uuid()
    _put(device_client, u, 1, "question", 1)                           # held
    _put(device_client, u, 2, None)                                    # handled while held: row removed
    c = connect(settings.db_path)
    assert c.execute("SELECT COUNT(*) FROM push_held").fetchone()[0] == 0
    c.close()
    n = len(pushes)
    _restart(app, settings)
    mirrors.recover_held(app)
    assert len(pushes) == n


def test_a_held_message_push_survives_a_restart_and_is_skipped_when_read(frozen_clock, app, settings, device_client,
                                                                          session_client, pushes):
    app.state.message_push_gate.timer = lambda delay, fn: None
    frozen_clock(datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc))
    _human_msg(device_client)
    frozen_clock(datetime(2026, 9, 24, 12, 0, 10, tzinfo=timezone.utc))
    _human_msg(device_client)                                          # held
    assert [p["n"] for p in pushes if p["k"] == "message"] == [1]
    assert messages.recover_held(app) == 1
    assert [p["n"] for p in pushes if p["k"] == "message"] == [1, 2]
    _human_msg(device_client)                                          # held again (the window is still open)
    for m in session_client.get("/api/messages").json()["messages"]:
        session_client.post(f"/api/messages/{m['id']}/ack")
    before = len(pushes)
    messages.recover_held(app)
    assert len([p for p in pushes[before:] if p["k"] == "message"]) == 0   # everything was read meanwhile
