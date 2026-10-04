"""Task 6: `sharing msg send|list|wait|ack` between environments, against a live local server."""
import json
import os


def test_send_by_project_and_receive(make_device, cli):
    desk, vm = make_device("desk", "acme"), make_device("vm", "ingest")
    r = cli(desk.root, "msg", "send", "--to", "project:ingest", "-m", "nightly run finished", "--json")
    assert r.code == 0, r.err
    got = cli(vm.root, "msg", "wait", "--after", "0", "--timeout", "5", "--json").json()["messages"]
    assert got[0]["text"] == "nightly run finished" and got[0]["from"] == "desk"


def test_attach_uses_the_share_guards(make_device, cli):
    desk = make_device("desk", "acme")
    (desk.root / ".env").write_text("SECRET=1", encoding="utf-8")
    r = cli(desk.root, "msg", "send", "--to", "human", "-m", "x", "--attach", desk.root / ".env", "--json")
    assert r.code == 6 and "SECRET" not in r.out + r.err
    (desk.root / "log.txt").write_text("ok", encoding="utf-8")
    r = cli(desk.root, "msg", "send", "--to", "human", "-m", "log", "--attach", desk.root / "log.txt", "--json")
    assert r.code == 0 and r.json()["files"][0].startswith("FILE")


def test_control_characters_are_cleaned(make_device, cli):
    desk, vm = make_device("desk", "acme"), make_device("vm", "ingest")
    cli(desk.root, "msg", "send", "--to", "project:ingest", "-m", "a\x1b[31mb‮", "--json")
    text = cli(vm.root, "msg", "list", "--json").json()["messages"][0]["text"]
    assert "\x1b" not in text and "‮" not in text


def test_bad_recipient_is_usage(make_device, cli):
    desk = make_device("desk", "acme")
    assert cli(desk.root, "msg", "send", "--to", "everyone", "-m", "x", "--json").code == 1


# --- beyond the brief ---------------------------------------------------------------------------------------

def test_message_shape_and_the_browser_opens_it(make_device, cli, sim):
    desk, vm = make_device("desk", "acme"), make_device("vm", "ingest")
    (desk.root / "log.txt").write_text("ok", encoding="utf-8")
    sent = cli(desk.root, "msg", "send", "--to", "human", "-m", "build is green", "--attach", desk.root / "log.txt",
               "--kind", "question", "--json").json()
    assert sent["id"].startswith("msg_") and len(sent["id"]) == 36
    raw = sim.request("GET", "/api/messages", params={"after": 0, "wait": 0}).json()["messages"]
    assert [m["id"] for m in raw] == [sent["id"]]
    m = raw[0]
    assert m["kind"] == "question" and m["files"] == sent["files"] and m["size"] == 2
    assert "build is green" not in json.dumps(raw)                       # the server holds the sealed body only
    body = sim.s.open_msg(sim.mk, bytes.fromhex(m["uuid"]), m["enc_body"])
    assert body == {"text": "build is green", "files": sent["files"], "ticket": None, "from": "desk", "kind": "question"}
    f = sim.request("GET", f"/api/files/{sent['files'][0]}").json()
    assert f["tags"] == ["message"] and f["expires_at"] is not None
    assert cli(vm.root, "msg", "list", "--json").json()["messages"] == []   # not addressed to vm


def test_list_keys_and_ack_per_device(make_device, cli):
    desk, vm, vm2 = make_device("desk", "acme"), make_device("vm", "ingest"), make_device("vm2", "ingest")
    mid = cli(desk.root, "msg", "send", "--to", "project:ingest", "-m", "hi", "--json").json()["id"]
    got = cli(vm.root, "msg", "list", "--json").json()
    m = got["messages"][0]
    assert set(m) == {"id", "seq", "from", "to", "kind", "text", "files", "ticket", "created_at", "error"}
    assert m["to"] == "project:ingest" and m["kind"] == "text" and m["error"] is None and got["cursor"] == m["seq"]
    r = cli(vm.root, "msg", "ack", mid, "--json")
    assert r.code == 0 and r.json() == {"id": mid, "acked": True}
    assert cli(vm.root, "msg", "list", "--json").json()["messages"] == []
    assert [x["id"] for x in cli(vm2.root, "msg", "list", "--json").json()["messages"]] == [mid]   # TIX-M1


def test_send_to_a_device_and_wait_times_out_empty(make_device, cli):
    desk, vm = make_device("desk", "acme"), make_device("vm", "ingest")
    r = cli(vm.root, "msg", "wait", "--after", "0", "--timeout", "0", "--json")
    assert r.code == 0 and r.json() == {"messages": [], "cursor": 0}
    cli(desk.root, "msg", "send", "--to", f"device:{vm.device_id}", "-m", "for you only", "--json")
    got = cli(vm.root, "msg", "wait", "--after", "0", "--timeout", "5", "--json").json()
    assert got["messages"][0]["to"] == f"device:{vm.device_id}"
    after = got["cursor"]
    assert cli(vm.root, "msg", "wait", "--after", str(after), "--timeout", "0", "--json").json()["messages"] == []


def test_ticket_tag_needs_this_workspaces_space(make_device, cli, tmp_path):
    desk, vm = make_device("desk", "acme"), make_device("vm", "ingest")
    assert cli(desk.root, "msg", "send", "--to", "human", "-m", "x", "--ticket", "TIX-1", "--json").json()["error"] \
        == "no_space"
    cli(desk.root, "space", "create", "--label", "Acme", "--json")
    p = desk.root / "m.json"
    p.write_text(json.dumps({"key": "DEMO-1", "gen": 1, "rev": 1, "status": "open", "priority": "normal",
                             "needs": None, "open_questions": 0, "schema_version": "1.0.0",
                             "doc": {"id": "DEMO-1", "title": "t"}}), encoding="utf-8")
    tix = cli(desk.root, "mirror", "push", "--file", p, "--json").json()["id"]
    r = cli(desk.root, "msg", "send", "--to", "project:ingest", "-m", "see ticket", "--ticket", tix.lower(), "--json")
    assert r.code == 0, r.out
    assert cli(vm.root, "msg", "list", "--json").json()["messages"][0]["ticket"] == tix


def test_bad_inputs_are_usage_errors(make_device, cli):
    desk = make_device("desk", "acme")
    for to in ("space:nothex", "project:", "device:a/b", "human:x", "project:" + "x" * 65):
        assert cli(desk.root, "msg", "send", "--to", to, "-m", "x", "--json").code == 1, to
    assert cli(desk.root, "msg", "send", "--to", "human", "-m", "x", "--kind", "shell", "--json").code == 1
    assert cli(desk.root, "msg", "send", "--to", "human", "-m", "", "--json").code == 1
    assert cli(desk.root, "msg", "ack", "msg_nothex", "--json").code == 1
    many = []
    for i in range(11):
        f = desk.root / f"f{i}.txt"
        f.write_text("x", encoding="utf-8")
        many += ["--attach", f]
    assert cli(desk.root, "msg", "send", "--to", "human", "-m", "x", *many, "--json").code == 1


def test_attachment_outside_the_repo_is_refused(make_device, cli, tmp_path):
    desk = make_device("desk", "acme")
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    assert cli(desk.root, "msg", "send", "--to", "human", "-m", "x", "--attach", outside, "--json").code == 6


def test_a_message_that_does_not_open_is_marked(make_device, cli, sim):
    desk, vm = make_device("desk", "acme"), make_device("vm", "ingest")
    u = os.urandom(16)
    bogus = sim.s.seal_msg(os.urandom(32), u, {"text": "x"})
    assert sim.request("POST", "/api/messages", json={"uuid": u.hex(), "to_kind": "project", "to_id": "ingest",
                                                      "kind": "text", "key_version": 1,
                                                      "enc_body": bogus}).status_code == 201
    m = cli(vm.root, "msg", "list", "--json").json()["messages"][0]
    assert m["text"] is None and m["error"] == "integrity" and m["from"] == "human"


def test_message_text_keeps_its_newlines(make_device, cli):
    """Batch 3 review m6: multi-line messages stay multi-line; other control characters are still replaced."""
    desk, vm = make_device("desk", "acme"), make_device("vm", "ingest")
    cli(desk.root, "msg", "send", "--to", "project:ingest", "-m", "line 1\nline 2\x1b[0m", "--json")
    text = cli(vm.root, "msg", "list", "--json").json()["messages"][0]["text"]
    assert text.startswith("line 1\nline 2") and "\x1b" not in text


def _post_msg(sim, body, **clear):
    u = os.urandom(16)
    req = {"uuid": u.hex(), "to_kind": "project", "to_id": "ingest", "kind": "text", "key_version": 1,
           "enc_body": sim.s.seal_msg(sim.mk, u, body), **clear}
    r = sim.request("POST", "/api/messages", json=req)
    assert r.status_code == 201, r.text


def test_a_message_takes_kind_files_and_ticket_from_the_sealed_body(make_device, cli, sim):
    """Batch 3 review I2: the sealed body is what counts; a cleartext that disagrees is flagged."""
    desk, vm = make_device("desk", "acme"), make_device("vm", "ingest")
    f = sim.upload("a.txt", b"a")
    _post_msg(sim, {"text": "ok", "files": [f["id"]], "ticket": None, "from": "human", "kind": "text"}, files=[f["id"]])
    _post_msg(sim, {"text": "evil", "files": [], "ticket": None, "from": "human", "kind": "text"}, files=[f["id"]])
    _post_msg(sim, {"text": "q", "files": [], "ticket": None, "from": "human", "kind": "question"})
    ok, swapped, kind = cli(vm.root, "msg", "list", "--json").json()["messages"]
    assert ok["error"] is None and ok["files"] == [f["id"]] and ok["text"] == "ok"
    assert swapped["error"] == "integrity" and swapped["text"] is None
    assert kind["error"] == "integrity" and kind["text"] is None
