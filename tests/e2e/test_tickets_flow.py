"""End-to-end legacy tickets (Task 12): a live server and a real onboarded CLI. Since sharing 2.0.0 legacy
tickets are read-only: list and show work, `wait` points to `orch wait`, the write commands are gone, and a
server with FS_LEGACY_TICKETS=readonly answers every legacy write with 410 before it even authenticates."""
import json
import urllib.error
import urllib.request

import pytest

from tests.conftest import run_live_server
from tests.e2e.test_onboard_share_get import cli as run_cli
from tests.e2e.test_onboard_share_get import device_env, make_repo, onboard
from tests.helpers.tickets import sim_ticket

pytestmark = pytest.mark.e2e


def test_legacy_tickets_are_read_only(live_server, sim, sharing, tmp_path):
    repo = make_repo(tmp_path, "repo-tix")
    env = device_env(live_server)
    rc, out, _dev = onboard(repo, env, sim, tmp_path, "--device", "laptop-tix", "--project", "repo-tix", "--yes")
    assert rc == 0, out
    ref = sim_ticket(sim, project="repo-tix", title="Add dark mode", status="open", body="Make it *dark*.\n")

    r = run_cli(repo, env, "tickets", "list", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    assert [t["id"] for t in json.loads(r.stdout)] == [ref]
    shown = json.loads(run_cli(repo, env, "tickets", "show", ref, "--json").stdout)
    assert shown["title"] == "Add dark mode" and shown["status"] == "open"

    waited = run_cli(repo, env, "tickets", "wait", ref, "--json")
    assert waited.returncode == 6 and json.loads(waited.stdout)["error"] == "legacy_readonly"
    for cmd in ("new", "claim", "ask", "test", "move"):
        assert run_cli(repo, env, "tickets", cmd, ref, "--json").returncode == 1, cmd


def test_a_frozen_server_refuses_legacy_writes_with_410(tmp_path):
    with run_live_server(tmp_path, FS_LEGACY_TICKETS="readonly") as srv:
        for method, path in (("POST", "/api/tickets"), ("POST", "/api/tickets/TIX-1/events"),
                             ("PATCH", "/api/tickets/TIX-1"), ("POST", "/api/tickets/TIX-1/claim")):
            req = urllib.request.Request(srv.url + path, data=b"{}", method=method,
                                         headers={"Content-Type": "application/json"})
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)
            assert e.value.code == 410 and json.loads(e.value.read())["error"] == "legacy_readonly", (method, path)
