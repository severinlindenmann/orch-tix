import time

from fileshare import mirrors
from tests.helpers.tickets import device_client, fake_env, new_uuid  # noqa: F401
from tests.test_push_v2 import SPACE, _put, pushes  # noqa: F401


def test_a_burst_gets_its_summary_from_the_real_timer(app, device_client, pushes):
    app.state.needs_push_gate = mirrors.NeedsPushGate(1)          # a one second window, the real daemon timer
    device_client.post("/api/spaces", json={"id": SPACE, "key_version": 1, "enc_label": fake_env()})
    tix = [_put(device_client, new_uuid(), 1, "question", 1).json()["id"] for _ in range(3)]
    assert [p["k"] for p in pushes] == ["question"]
    deadline = time.time() + 5
    while len(pushes) < 2 and time.time() < deadline:
        time.sleep(0.1)
    assert [p["k"] for p in pushes] == ["question", "batch"], pushes
    assert pushes[1]["ts"] == tix[1:] and pushes[1]["cs"] == 3
