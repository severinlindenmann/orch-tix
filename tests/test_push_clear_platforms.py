"""Regression tests for the live iPhone findings after #47: the per-workspace count in every needs push (`cs`), no
clear push to iOS (it cannot replace a notification), short TTL for needs pushes."""
import json
import subprocess

import pytest

from fileshare import push as push_module
from fileshare.db import connect
from tests.helpers.tickets import device_client, fake_dek, fake_env, new_uuid  # noqa: F401
from tests.test_push_v2 import DEK, SPACE, _put, pushes  # noqa: F401

OTHER = "d" * 32


def _space(dc, sid):
    dc.post("/api/spaces", json={"id": sid, "key_version": 1, "enc_label": fake_env()})


def test_cs_counts_only_the_workspace_while_c_counts_everything(frozen_clock, device_client, pushes):
    _space(device_client, SPACE)
    _space(device_client, OTHER)
    for i in range(3):          # three needs in ANOTHER workspace
        device_client.put(f"/api/mirrors/{new_uuid()}", json={
            "space": OTHER, "mirror_rev": 1, "schema_version": "1.0.0", "status": "waiting", "priority": "normal",
            "needs": "question", "open_questions": 1, "key_version": 1, "wrapped_dek": DEK, "enc_content": fake_env(90),
            "event_uuid": f"{i:032x}"})
    u = new_uuid()
    tix = _put(device_client, u, 1, "question", 1).json()["id"]
    assert pushes[-1]["c"] == 4 and pushes[-1]["cs"] == 1
    _put(device_client, u, 2, None)
    assert pushes[-1] == {"v": 2, "s": SPACE, "t": tix, "k": "clear", "n": 0, "c": 3, "cs": 0}   # 3 elsewhere, 0 here


def test_the_message_read_clear_carries_the_message_marker(device_client, session_client, pushes):
    _space(device_client, SPACE)
    mid = device_client.post("/api/messages", json={"uuid": new_uuid(), "to_kind": "human", "to_id": "", "space": SPACE,
                                                    "kind": "text", "key_version": 1, "enc_body": fake_env()}).json()["id"]
    assert session_client.post(f"/api/messages/{mid}/ack").status_code == 204
    assert pushes[-1]["k"] == "clear" and pushes[-1]["w"] == "message" and pushes[-1]["s"] == SPACE


def test_endpoints_by_platform():
    assert push_module.replaces_by_tag("https://fcm.googleapis.com/fcm/send/abc")
    assert push_module.replaces_by_tag("https://updates.push.services.mozilla.com/wpush/v2/x")
    assert not push_module.replaces_by_tag("https://web.push.apple.com/QAbc")
    assert push_module.replaces_by_tag("https://evil.example/web.push.apple.com")


def test_needs_pushes_expire_early_and_a_clear_keeps_the_day():
    assert push_module.ttl_for({"v": 2, "k": "question"}) == push_module.NEEDS_TTL_S
    assert push_module.ttl_for({"v": 2, "k": "batch"}) == push_module.NEEDS_TTL_S
    assert push_module.ttl_for({"v": 2, "k": "clear"}) == push_module.TTL_S


@pytest.mark.real_pusher
def test_a_clear_is_sent_to_chrome_but_not_to_an_iphone(app, settings, monkeypatch):
    conn = connect(settings.db_path)
    for sid, url in (("psh_chrome", "https://fcm.googleapis.com/fcm/send/1"), ("psh_iphone", "https://web.push.apple.com/Q1")):
        conn.execute("INSERT INTO push_subs (id, session_name, endpoint, p256dh, auth, created_at) VALUES (?,?,?,?,?,?)",
                     (sid, "x", url, "p", "a", "2026-10-05T00:00:00Z"))
    push_module.ensure_vapid(conn)
    conn.close()
    reqs = []

    def fake_run(cmd, input, capture_output, text, timeout):
        reqs.append(json.loads(input))
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"gone": [], "failed": []}))

    monkeypatch.setattr(push_module.subprocess, "run", fake_run)
    p = push_module.SubprocessPusher(settings, settings.db_path)
    p._deliver_payload('{"v":2,"k":"clear"}', None, push_module.TTL_S, True)
    p._deliver_payload('{"v":2,"k":"question"}', None, push_module.NEEDS_TTL_S, False)
    assert [s["id"] for s in reqs[0]["subs"]] == ["psh_chrome"] and reqs[0]["ttl"] == push_module.TTL_S
    assert sorted(s["id"] for s in reqs[1]["subs"]) == ["psh_chrome", "psh_iphone"] and reqs[1]["ttl"] == push_module.NEEDS_TTL_S
