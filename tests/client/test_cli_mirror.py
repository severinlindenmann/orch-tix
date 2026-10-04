"""Task 5: `sharing space|mirror|inbox|devices` against a live local server (never production)."""
import json
import os
import threading
import time

import pytest


def _payload(tmp, key="DEMO-0038", gen=1, rev=1, needs="question", **doc):
    body = {"key": key, "gen": gen, "rev": rev, "status": "waiting", "priority": "high", "needs": needs,
            "open_questions": 1 if needs == "question" else 0, "schema_version": "1.0.0",
            "doc": {"schema_version": "1.0.0", "id": key, "title": "Export the meter readings", **doc}}
    p = tmp / f"m{rev}.json"
    p.write_text(json.dumps(body), encoding="utf-8")
    return p


@pytest.fixture
def desk(make_device, cli):
    d = make_device("desk", "acme")
    r = cli(d.root, "space", "create", "--label", "Acme Energy", "--json")
    assert r.code == 0, r.err
    return d


def test_space_create_show_and_refuse_twice(desk, cli):
    shown = cli(desk.root, "space", "show", "--json").json()
    assert shown["label"] == "Acme Energy" and shown["owner"] is True and len(shown["space_id"]) == 32
    assert cli(desk.root, "space", "create", "--label", "x", "--json").code == 6
    assert (desk.root / ".claude" / "skills" / "sharing" / "space.json").stat().st_mode & 0o077 == 0


def test_push_then_browser_reads_the_sealed_doc(desk, cli, sim, tmp_path):
    out = cli(desk.root, "mirror", "push", "--file", _payload(desk.root), "--json").json()
    assert out["status"] == "pushed" and out["id"].startswith("TIX-")
    m = sim.request("GET", f"/api/mirrors/{out['id']}").json()
    s = sim.s
    dek = s.open_(sim.mk, s.unb64u(m["wrapped_dek"]), s.aad_tdek(bytes.fromhex(m["uuid"])))
    doc = s.open_mirror(dek, bytes.fromhex(m["uuid"]), m["enc_content"])
    assert doc["title"] == "Export the meter readings" and m["needs"] == "question"
    raw = sim.request("GET", f"/api/mirrors/{out['id']}").text
    assert "DEMO-0038" not in raw and "meter" not in raw


def test_push_twice_is_duplicate_and_exit_0(desk, cli):
    p = _payload(desk.root, rev=2)
    assert cli(desk.root, "mirror", "push", "--file", p, "--json").json()["status"] == "pushed"
    r = cli(desk.root, "mirror", "push", "--file", p, "--json")
    assert r.code == 0 and r.json()["status"] == "duplicate"
    r = cli(desk.root, "mirror", "push", "--file", _payload(desk.root, rev=1), "--json")
    assert r.code == 0 and r.json()["status"] == "stale"


def test_inbox_wait_returns_a_decrypted_decision_and_ack(desk, cli, sim):
    out = cli(desk.root, "mirror", "push", "--file", _payload(desk.root), "--json").json()
    m = sim.request("GET", f"/api/mirrors/{out['id']}").json()
    s = sim.s
    tu = bytes.fromhex(m["uuid"])
    dek = s.open_(sim.mk, s.unb64u(m["wrapped_dek"]), s.aad_tdek(tu))
    du = os.urandom(16)
    space = cli(desk.root, "space", "show", "--json").json()["space_id"]
    body = {"v": 1, "decision_id": "dec_" + du.hex(), "space": space, "ticket": "DEMO-0038", "kind": "answer",
            "target": {"qid": "Q1", "hash": "sha256:" + "0" * 64}, "value": "A"}
    r = sim.request("POST", "/api/decisions", json={"uuid": du.hex(), "space": space, "ticket": out["id"],
                                                    "kind": "answer", "key_version": 1,
                                                    "enc_body": s.seal_decision(dek, tu, du, body)})
    assert r.status_code == 201
    got = cli(desk.root, "inbox", "wait", "--after", "0", "--timeout", "5", "--json").json()
    assert got["decisions"][0]["decision"] == body and got["cursor"] >= 1
    assert cli(desk.root, "inbox", "ack", "dec_" + du.hex(), "applied", "--json").code == 0
    assert cli(desk.root, "inbox", "list", "--json").json()["decisions"] == []


def test_unlink_and_status(desk, cli):
    cli(desk.root, "mirror", "push", "--file", _payload(desk.root), "--json")
    st = cli(desk.root, "mirror", "status", "--json").json()["mirrors"]
    assert [m["key"] for m in st] == ["DEMO-0038"]
    assert cli(desk.root, "mirror", "unlink", "--key", "DEMO-0038", "--gen", "1", "--json").json()["status"] == "unlinked"
    assert cli(desk.root, "mirror", "status", "--json").json()["mirrors"] == []


def test_space_join_refuses_agents(desk, cli, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    sid = cli(desk.root, "space", "show", "--json").json()["space_id"]
    assert cli(desk.root, "space", "join", sid, "--label", "x", "--json").code == 6


def test_bad_input_file_is_usage_error(desk, cli):
    p = desk.root / "bad.json"
    p.write_text("{}", encoding="utf-8")
    assert cli(desk.root, "mirror", "push", "--file", p, "--json").code == 1


# --- binding rulings beyond the brief -------------------------------------------------------------------------

def test_devices_is_browser_only_and_says_so(desk, cli):
    """USER ruling: GET /api/devices is for the browser; the CLI turns the 403 into a plain refusal."""
    r = cli(desk.root, "devices", "--json")
    assert r.code == 6 and r.json()["error"] == "browser_only"
    assert "browser" in r.json()["detail"] and "revoked" not in r.json()["detail"]
    assert "shd_" not in r.out + r.err and "Traceback" not in r.err
    plain = cli(desk.root, "devices")
    assert plain.code == 6 and "browser" in plain.err


def test_unlink_twice_and_unknown_is_absent(desk, cli):
    """404 on unlink counts as success (batch-1 review)."""
    cli(desk.root, "mirror", "push", "--file", _payload(desk.root), "--json")
    assert cli(desk.root, "mirror", "unlink", "--key", "DEMO-0038", "--gen", "1", "--json").json()["status"] == "unlinked"
    r = cli(desk.root, "mirror", "unlink", "--key", "DEMO-0038", "--gen", "1", "--json")
    assert r.code == 0 and r.json()["status"] == "absent"
    r = cli(desk.root, "mirror", "unlink", "--key", "NEVER-1", "--gen", "1", "--json")
    assert r.code == 0 and r.json()["status"] == "absent"


def test_update_reuses_the_stored_dek_and_reports_server_rev(desk, cli, sim):
    first = cli(desk.root, "mirror", "push", "--file", _payload(desk.root, rev=1), "--json").json()
    assert first["server_rev"] is None and first["gen"] == 1
    wrapped = sim.request("GET", f"/api/mirrors/{first['id']}").json()["wrapped_dek"]
    second = cli(desk.root, "mirror", "push", "--file", _payload(desk.root, rev=2), "--json")
    assert second.code == 0, second.out + second.err          # a new DEK would be 409 dek_mismatch
    assert second.json() == {"status": "pushed", "id": first["id"], "uuid": first["uuid"], "server_rev": 1, "gen": 1,
                             "rev": 2}
    m = sim.request("GET", f"/api/mirrors/{first['id']}").json()
    assert m["wrapped_dek"] == wrapped and m["mirror_rev"] == 2


def test_a_late_push_after_unlink_is_gone_and_recreates_nothing(desk, cli, sim):
    """Batch 3 review I1: a 410 on a retired uuid is reported, never silently re-linked."""
    first = cli(desk.root, "mirror", "push", "--file", _payload(desk.root), "--json").json()
    cli(desk.root, "mirror", "unlink", "--key", "DEMO-0038", "--gen", "1", "--json")
    r = cli(desk.root, "mirror", "push", "--file", _payload(desk.root, rev=3), "--json")
    assert r.code == 0, r.out + r.err
    assert r.json() == {"status": "gone", "gen": 1}
    assert cli(desk.root, "mirror", "status", "--json").json()["mirrors"] == []
    assert sim.request("GET", f"/api/mirrors/{first['id']}").status_code == 404


def test_relink_bumps_the_link_generation(desk, cli, sharing):
    """Only --relink moves to the next generation (a new uuid and TIX id)."""
    first = cli(desk.root, "mirror", "push", "--file", _payload(desk.root), "--json").json()
    cli(desk.root, "mirror", "unlink", "--key", "DEMO-0038", "--gen", "1", "--json")
    r = cli(desk.root, "mirror", "push", "--file", _payload(desk.root, rev=3), "--relink", "--json")
    assert r.code == 0, r.out + r.err
    again = r.json()
    space = cli(desk.root, "space", "show", "--json").json()["space_id"]
    assert again["status"] == "pushed" and again["gen"] == 2 and again["id"] != first["id"]
    assert again["uuid"] == sharing.mirror_uuid(space, "DEMO-0038", 2)
    st = cli(desk.root, "mirror", "status", "--json").json()["mirrors"]
    assert [(m["key"], m["gen"], m["rev"]) for m in st] == [("DEMO-0038", 2, 3)]


def test_the_sealed_doc_carries_mirror_rev_and_gen(desk, cli, sim):
    """Batch 3 review m4: the phone can tell an older snapshot (a rollback) from the doc itself."""
    out = cli(desk.root, "mirror", "push", "--file", _payload(desk.root, rev=5), "--json").json()
    m = sim.request("GET", f"/api/mirrors/{out['id']}").json()
    s = sim.s
    tu = bytes.fromhex(m["uuid"])
    doc = s.open_mirror(s.open_(sim.mk, s.unb64u(m["wrapped_dek"]), s.aad_tdek(tu)), tu, m["enc_content"])
    assert doc["mirror_rev"] == 5 and doc["gen"] == 1 and doc["title"] == "Export the meter readings"


def test_space_create_reuses_the_id_after_a_lost_answer(make_device, cli, sharing, sim):
    """Batch 3 review m3: the id is stored before the POST, so a retry reuses it (one space, not two)."""
    d = make_device("desk2", "acme")
    sp = d.root / ".claude" / "skills" / "sharing" / "space.json"
    sid = "b" * 32
    # what a lost answer leaves behind: the id written, the POST done, the reply never read
    sharing.write_config(sp, {"space_id": sid, "label": "Acme Energy", "pending": True})
    first = cli(d.root, "space", "create", "--label", "Acme Energy", "--json")
    assert first.code == 0, first.out + first.err
    assert first.json()["space_id"] == sid
    again = cli(d.root, "space", "create", "--label", "Acme Energy", "--json")
    assert again.code == 6                     # done now: a second create is refused
    assert [x["id"] for x in sim.request("GET", "/api/spaces").json()["spaces"]].count(sid) == 1
    assert "pending" not in sharing._read_json(sp, "space.json")


def test_mirror_push_needs_a_space(make_device, cli):
    d = make_device("bare", "acme")
    r = cli(d.root, "mirror", "push", "--file", _payload(d.root), "--json")
    assert r.code == 3 and r.json()["error"] == "no_space"
    assert cli(d.root, "space", "show", "--json").json()["error"] == "no_space"


def _approve_join_when_asked(sim, space_id, decision="approve", timeout=30):
    def run():
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reqs = sim.request("GET", "/api/join-requests").json()["requests"]
            mine = [q for q in reqs if q["space"] == space_id]
            if mine:
                r = sim.request("POST", f"/api/spaces/{space_id}/join-requests/{mine[0]['id']}/{decision}")
                assert r.status_code == 200, r.text
                return
            time.sleep(0.05)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


@pytest.fixture
def human_terminal(sharing, monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("ORCH_HARNESS", raising=False)
    monkeypatch.setattr(sharing, "_interactive", lambda: True)
    monkeypatch.setattr(sharing, "JOIN_POLL_S", 0.05)


def test_space_join_waits_for_the_browser_then_takes_over(desk, make_device, cli, sim, human_terminal):
    sid = cli(desk.root, "space", "show", "--json").json()["space_id"]
    laptop = make_device("laptop", "acme")
    t = _approve_join_when_asked(sim, sid)
    r = cli(laptop.root, "space", "join", sid, "--json", stdin=(sid + "\n").encode())
    t.join(5)
    assert r.code == 0, r.out + r.err
    assert r.json() == {"space_id": sid, "label": "Acme Energy", "status": "approved"}
    assert cli(laptop.root, "space", "show", "--json").json()["owner"] is True
    assert cli(desk.root, "space", "show", "--json").json()["owner"] is False


def test_space_join_denied_writes_nothing(desk, make_device, cli, sim, human_terminal):
    sid = cli(desk.root, "space", "show", "--json").json()["space_id"]
    laptop = make_device("laptop", "acme")
    t = _approve_join_when_asked(sim, sid, decision="deny")
    r = cli(laptop.root, "space", "join", sid, "--label", "x", "--json", stdin=(sid + "\n").encode())
    t.join(5)
    assert r.code == 6 and "denied" in r.json()["detail"]
    assert not (laptop.root / ".claude" / "skills" / "sharing" / "space.json").exists()
    assert cli(desk.root, "space", "show", "--json").json()["owner"] is True


def test_space_join_needs_the_typed_space_id(desk, make_device, cli, human_terminal):
    sid = cli(desk.root, "space", "show", "--json").json()["space_id"]
    laptop = make_device("laptop", "acme")
    r = cli(laptop.root, "space", "join", sid, "--json", stdin=b"nope\n")
    assert r.code == 6 and not (laptop.root / ".claude" / "skills" / "sharing" / "space.json").exists()


def test_space_join_times_out_while_pending(desk, make_device, cli, sharing, monkeypatch, human_terminal):
    monkeypatch.setattr(sharing, "JOIN_TIMEOUT_S", 0.2)
    sid = cli(desk.root, "space", "show", "--json").json()["space_id"]
    laptop = make_device("laptop", "acme")
    r = cli(laptop.root, "space", "join", sid, "--json", stdin=(sid + "\n").encode())
    assert r.code == 3 and r.json()["error"] == "pending" and "sharing space join" in r.json()["detail"]
    assert not (laptop.root / ".claude" / "skills" / "sharing" / "space.json").exists()


def test_a_relabelled_decision_is_flagged_and_its_sealed_values_stay_out(desk, cli, sim):
    """Batch 3 review I2: the cleartext says approve, the sealed body says comment: integrity, never applied."""
    out = cli(desk.root, "mirror", "push", "--file", _payload(desk.root), "--json").json()
    m = sim.request("GET", f"/api/mirrors/{out['id']}").json()
    s = sim.s
    tu = bytes.fromhex(m["uuid"])
    dek = s.open_(sim.mk, s.unb64u(m["wrapped_dek"]), s.aad_tdek(tu))
    space = cli(desk.root, "space", "show", "--json").json()["space_id"]
    du = os.urandom(16)
    body = {"v": 1, "decision_id": "dec_" + du.hex(), "ticket": "DEMO-0038", "kind": "comment", "value": "rm -rf"}
    assert sim.request("POST", "/api/decisions", json={"uuid": du.hex(), "space": space, "ticket": out["id"],
                                                       "kind": "approve", "key_version": 1,
                                                       "enc_body": s.seal_decision(dek, tu, du, body)}).status_code == 201
    got = cli(desk.root, "inbox", "list", "--json").json()["decisions"][0]
    assert got["error"] == "integrity" and got["decision"] is None
    assert got["kind"] == "approve" and "rm -rf" not in json.dumps(got)


def test_a_decision_with_another_id_inside_is_flagged(desk, cli, sim):
    out = cli(desk.root, "mirror", "push", "--file", _payload(desk.root), "--json").json()
    m = sim.request("GET", f"/api/mirrors/{out['id']}").json()
    s = sim.s
    tu = bytes.fromhex(m["uuid"])
    dek = s.open_(sim.mk, s.unb64u(m["wrapped_dek"]), s.aad_tdek(tu))
    space = cli(desk.root, "space", "show", "--json").json()["space_id"]
    du = os.urandom(16)
    body = {"v": 1, "decision_id": "dec_" + "0" * 32, "ticket": "DEMO-0038", "kind": "answer",
            "target": {"qid": "Q1", "hash": "sha256:" + "0" * 64}, "value": "A"}
    sim.request("POST", "/api/decisions", json={"uuid": du.hex(), "space": space, "ticket": out["id"], "kind": "answer",
                                               "key_version": 1, "enc_body": s.seal_decision(dek, tu, du, body)})
    got = cli(desk.root, "inbox", "list", "--json").json()["decisions"][0]
    assert got["error"] == "integrity" and got["decision"] is None


def test_inbox_marks_a_decision_that_does_not_open(desk, cli, sim):
    out = cli(desk.root, "mirror", "push", "--file", _payload(desk.root), "--json").json()
    space = cli(desk.root, "space", "show", "--json").json()["space_id"]
    du = os.urandom(16)
    s = sim.s
    bogus = s.seal_decision(os.urandom(32), bytes.fromhex(out["uuid"]), du, {"v": 1})
    assert sim.request("POST", "/api/decisions", json={"uuid": du.hex(), "space": space, "ticket": out["id"],
                                                       "kind": "answer", "key_version": 1,
                                                       "enc_body": bogus}).status_code == 201
    got = cli(desk.root, "inbox", "list", "--json").json()["decisions"]
    # No unbound cleartext at the top (Task 10 review I3): an item that does not open names no ticket.
    assert got[0]["decision"] is None and got[0]["error"] == "integrity" and got[0]["ticket"] is None


def test_not_owner_is_a_refusal_not_a_broken_device(desk, make_device, cli, sharing):
    """Exit 6 (the device is fine) with the server's hint, never exit 3 (which says re-onboard)."""
    sid = cli(desk.root, "space", "show", "--json").json()["space_id"]
    laptop = make_device("laptop", "acme")
    sharing.write_config(laptop.root / ".claude" / "skills" / "sharing" / "space.json", {"space_id": sid, "label": "x"})
    r = cli(laptop.root, "mirror", "push", "--file", _payload(laptop.root), "--json")
    assert r.code == 6 and r.json()["error"] == "not_owner"
    assert "sharing space join" in r.json()["detail"] and "approve it in TIX" in r.json()["detail"]


def test_join_wait_stops_on_an_expired_request(sharing, monkeypatch):
    class FakeApi:
        def __init__(self):
            self.answers = [{"status": "pending"}, {"status": "expired"}]

        def get_json(self, path):
            return self.answers.pop(0)
    monkeypatch.setattr(sharing, "JOIN_POLL_S", 0)
    assert sharing._wait_for_join(FakeApi(), "a" * 32, "jr_" + "b" * 32) == "expired"


def test_space_create_retry_after_the_server_took_it_is_success(desk, cli, sharing, sim):
    """The POST reached the server but the answer was lost: the retry gets 409 and the space is ours."""
    sp = desk.root / ".claude" / "skills" / "sharing" / "space.json"
    sid = cli(desk.root, "space", "show", "--json").json()["space_id"]
    sharing.write_config(sp, {"space_id": sid, "label": "Acme Energy", "pending": True})
    r = cli(desk.root, "space", "create", "--label", "Acme Energy", "--json")
    assert r.code == 0, r.out + r.err
    assert r.json()["space_id"] == sid
    assert [x["id"] for x in sim.request("GET", "/api/spaces").json()["spaces"]] == [sid]


def test_space_create_never_adopts_a_space_another_device_owns(desk, make_device, cli, sharing):
    sid = cli(desk.root, "space", "show", "--json").json()["space_id"]
    other = make_device("other", "acme")
    sharing.write_config(other.root / ".claude" / "skills" / "sharing" / "space.json",
                         {"space_id": sid, "label": "x", "pending": True})
    r = cli(other.root, "space", "create", "--label", "x", "--json")
    assert r.code != 0 and r.json()["error"] == "duplicate_uuid"


# --- Task 9 review fix 3: the top-level ticket is bound to the sealed ticket and space ------------------------

def _seal_for(sim, cli, desk, tix, body):
    """Seal `body` under the mirror `tix`'s DEK and post it routed to `tix`, as a browser session would."""
    m = sim.request("GET", f"/api/mirrors/{tix}").json()
    s = sim.s
    tu = bytes.fromhex(m["uuid"])
    dek = s.open_(sim.mk, s.unb64u(m["wrapped_dek"]), s.aad_tdek(tu))
    du = bytes.fromhex(body["decision_id"][4:])
    space = cli(desk.root, "space", "show", "--json").json()["space_id"]
    r = sim.request("POST", "/api/decisions", json={"uuid": du.hex(), "space": space, "ticket": tix, "kind": body["kind"],
                                                    "key_version": 1, "enc_body": s.seal_decision(dek, tu, du, body)})
    assert r.status_code == 201, r.text
    return space


def test_a_decision_routed_to_another_live_mirror_is_flagged(desk, cli, sim):
    """Probe: the cleartext ticket (and so the DEK and AAD) is another live mirror B, the sealed ticket names A.
    It opens, but it must never be applied to A or reported as B."""
    a = cli(desk.root, "mirror", "push", "--file", _payload(desk.root, key="DEMO-0038"), "--json").json()["id"]
    b = cli(desk.root, "mirror", "push", "--file", _payload(desk.root, key="DEMO-0039", rev=2), "--json").json()["id"]
    assert a != b
    du = os.urandom(16).hex()
    space = cli(desk.root, "space", "show", "--json").json()["space_id"]
    body = {"v": 1, "decision_id": "dec_" + du, "space": space, "ticket": "DEMO-0038", "kind": "answer",
            "target": {"qid": "Q1", "hash": "sha256:" + "0" * 64}, "value": "A"}
    _seal_for(sim, cli, desk, b, body)
    got = cli(desk.root, "inbox", "list", "--json").json()["decisions"][0]
    assert got["error"] == "integrity" and got["decision"] is None


def test_a_decision_sealed_for_another_space_is_flagged(desk, cli, sim):
    tix = cli(desk.root, "mirror", "push", "--file", _payload(desk.root), "--json").json()["id"]
    du = os.urandom(16).hex()
    body = {"v": 1, "decision_id": "dec_" + du, "space": "f" * 32, "ticket": "DEMO-0038", "kind": "answer",
            "target": {"qid": "Q1", "hash": "sha256:" + "0" * 64}, "value": "A"}
    _seal_for(sim, cli, desk, tix, body)
    got = cli(desk.root, "inbox", "list", "--json").json()["decisions"][0]
    assert got["error"] == "integrity" and got["decision"] is None


def test_a_bound_decision_reports_its_ticket(desk, cli, sim):
    tix = cli(desk.root, "mirror", "push", "--file", _payload(desk.root), "--json").json()["id"]
    du = os.urandom(16).hex()
    space = cli(desk.root, "space", "show", "--json").json()["space_id"]
    body = {"v": 1, "decision_id": "dec_" + du, "space": space, "ticket": "DEMO-0038", "kind": "answer",
            "target": {"qid": "Q1", "hash": "sha256:" + "0" * 64}, "value": "A"}
    _seal_for(sim, cli, desk, tix, body)
    got = cli(desk.root, "inbox", "list", "--json").json()["decisions"][0]
    assert got["error"] is None and got["ticket"] == tix and got["decision"]["ticket"] == "DEMO-0038" and got["gen"] == 1


# --- Task 10 review fix round 1 (I3): the top-level ticket is never the server's unbound cleartext -----------

def test_a_server_that_rewrites_only_the_cleartext_ticket_changes_nothing(desk, cli, sim, sharing, monkeypatch):
    """The inbox item's `ticket` (TIX-n) is cleartext the server could rewrite while keeping the ticket_uuid and
    the sealed body intact. The CLI reports the TIX id of the mirror at ticket_uuid, never that field."""
    a = cli(desk.root, "mirror", "push", "--file", _payload(desk.root, key="DEMO-0038"), "--json").json()["id"]
    b = cli(desk.root, "mirror", "push", "--file", _payload(desk.root, key="DEMO-0039", rev=2), "--json").json()["id"]
    du = os.urandom(16).hex()
    space = cli(desk.root, "space", "show", "--json").json()["space_id"]
    body = {"v": 1, "decision_id": "dec_" + du, "space": space, "ticket": "DEMO-0038", "kind": "answer",
            "target": {"qid": "Q1", "hash": "sha256:" + "0" * 64}, "value": "A"}
    _seal_for(sim, cli, desk, a, body)
    real = sharing.Api.get_json

    def rewriting(self, path, *args, **kw):
        out = real(self, path, *args, **kw)
        if path.startswith("/api/inbox/changes"):
            for item in out.get("decisions", []):
                item["ticket"] = b                     # the cleartext only; uuid, DEK and body stay A's
        return out
    monkeypatch.setattr(sharing.Api, "get_json", rewriting)
    got = cli(desk.root, "inbox", "list", "--json").json()["decisions"][0]
    assert got["error"] is None and got["decision"]["ticket"] == "DEMO-0038"
    assert got["ticket"] == a


def test_an_integrity_item_reports_no_ticket(desk, cli, sim):
    a = cli(desk.root, "mirror", "push", "--file", _payload(desk.root, key="DEMO-0038"), "--json").json()["id"]
    b = cli(desk.root, "mirror", "push", "--file", _payload(desk.root, key="DEMO-0039", rev=2), "--json").json()["id"]
    assert a != b
    du = os.urandom(16).hex()
    space = cli(desk.root, "space", "show", "--json").json()["space_id"]
    body = {"v": 1, "decision_id": "dec_" + du, "space": space, "ticket": "DEMO-0038", "kind": "answer",
            "target": {"qid": "Q1", "hash": "sha256:" + "0" * 64}, "value": "A"}
    _seal_for(sim, cli, desk, b, body)
    got = cli(desk.root, "inbox", "list", "--json").json()["decisions"][0]
    assert got["error"] == "integrity" and got["ticket"] is None


def test_after_a_takeover_the_new_owner_pushes_above_the_old_rev(desk, make_device, cli, sim, human_terminal):
    """Final review I1: the server keeps mirror_rev on takeover. The new owner's links start low, so the CLI
    pushes at server_rev + 1 (it never wrote that rev itself) and reports the rev it used."""
    for rev in (1, 2, 3):
        assert cli(desk.root, "mirror", "push", "--file", _payload(desk.root, rev=rev), "--json").json()["status"] == "pushed"
    sid = cli(desk.root, "space", "show", "--json").json()["space_id"]
    laptop = make_device("laptop", "acme")
    t = _approve_join_when_asked(sim, sid)
    assert cli(laptop.root, "space", "join", sid, "--json", stdin=(sid + "\n").encode()).code == 0
    t.join(5)
    out = cli(laptop.root, "mirror", "push", "--file", _payload(laptop.root, rev=1), "--json").json()
    assert out["status"] == "pushed" and out["rev"] == 4 and out["server_rev"] == 3
    m = sim.request("GET", f"/api/mirrors/{out['id']}").json()
    assert m["mirror_rev"] == 4
    s = sim.s
    dek = s.open_(sim.mk, s.unb64u(m["wrapped_dek"]), s.aad_tdek(bytes.fromhex(m["uuid"])))
    assert s.open_mirror(dek, bytes.fromhex(m["uuid"]), m["enc_content"])["mirror_rev"] == 4
    again = cli(laptop.root, "mirror", "push", "--file", _payload(laptop.root, rev=1), "--json").json()
    assert again["status"] == "stale" and again["server_rev"] == 4      # rev 4 is the laptop's own now


def test_own_pushes_keep_the_stale_semantics(desk, cli):
    """An older snapshot of this device's own (a retry, two drains racing) is still stale, never re-based."""
    assert cli(desk.root, "mirror", "push", "--file", _payload(desk.root, rev=3), "--json").json()["rev"] == 3
    r = cli(desk.root, "mirror", "push", "--file", _payload(desk.root, rev=2), "--json").json()
    assert r["status"] == "stale" and r["server_rev"] == 3


def test_an_older_own_payload_stays_stale_without_any_local_rev_file(desk, cli):
    """Polish I1: the server names the last writer, so no local file decides whether to re-base."""
    assert cli(desk.root, "mirror", "push", "--file", _payload(desk.root, rev=3), "--json").json()["rev"] == 3
    for leftover in (desk.root / ".claude" / "skills" / "sharing").glob("mirror-revs.json"):
        leftover.unlink()
    r = cli(desk.root, "mirror", "push", "--file", _payload(desk.root, rev=2), "--json").json()
    assert r["status"] == "stale" and r["server_rev"] == 3 and r["rev"] == 2
    assert not (desk.root / ".claude" / "skills" / "sharing" / "mirror-revs.json").exists()
