"""orch-core in-process + the orch-tix addon + the real sharing CLI against a local TIX (spec §13 P2/P4).
ask → mirror push → phone decision (BrowserSim) → inbox → desktop Apply → ack; then a paired phone applies
directly and `orch wait` returns. Never talks to tix.severin.io: the server is a loopback `live_server`."""
import os
import shutil
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("orch.remote.verify", reason="needs orch-core with A4-P0")
pytestmark = pytest.mark.e2e


REPO = Path(__file__).resolve().parents[2]                              # tests/client/ -> repo root
ADDON = REPO / "addons" / "orch-tix"
sys.path.insert(0, str(ADDON))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _post_answer(sim, mirror, space, *, qid, value, key=None, phone_id=None):
    """What the phone does: open the mirror, bind the answer to the question hash, seal, (sign,) POST."""
    from orch.remote.verify import mac_of
    s = sim.s
    tu = bytes.fromhex(mirror["uuid"])
    dek = s.open_(sim.mk, s.unb64u(mirror["wrapped_dek"]), s.aad_tdek(tu))
    doc = s.open_mirror(dek, tu, mirror["enc_content"])
    q = next(q for q in doc["questions"] if q["id"] == qid)
    du = os.urandom(16)
    d = {"v": 1, "decision_id": "dec_" + du.hex(), "space": space, "ticket": doc["id"], "kind": "answer",
         "target": {"qid": qid, "hash": q["hash"]}, "value": value, "note": "", "device": "e2e phone", "at": _now()}
    if key is not None:
        d["pair"] = phone_id
        d["mac"] = mac_of(key, d)
    r = sim.request("POST", "/api/decisions", json={"uuid": du.hex(), "space": space, "ticket": mirror["id"],
                                                    "kind": "answer", "key_version": 1,
                                                    "enc_body": s.seal_decision(dek, tu, du, d)})
    assert r.status_code == 201, r.text
    return doc


def _mirror(sim, space):
    (m,) = sim.request("GET", "/api/mirrors", params={"space": space}).json()["mirrors"]
    return m


def _install_cli(root: Path) -> Path:
    """The skill files the installer would put next to config.json (the handshake writes only config.json)."""
    dest = root / ".claude" / "skills" / "sharing"
    for name in ("sharing", "sharing.py"):
        shutil.copy2(REPO / "skill" / "sharing" / name, dest / name)
    (dest / "sharing").chmod(0o755)
    return dest / "sharing"


def test_question_round_trip(make_device, cli, sim, tmp_path, monkeypatch):
    if shutil.which("uv") is None:
        pytest.skip("the sharing wrapper needs uv")
    from fastapi.testclient import TestClient
    from orch.addons.loader import AddonRegistry
    from orch.addons.outbox import pump_all
    from orch.core import store
    from orch.core.events import Actor
    from orch.core.ops import Ops
    from orch.core.wait import wait_for_human
    from orch.dashboard.app import create_app
    from orch.remote import store as phones
    from orch.testing import fake_workspace

    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "orch-user"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for var in ("ORCH_HOME", "ORCH_HARNESS", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "ORCH_SESSION", "ORCH_MODEL"):
        monkeypatch.delenv(var, raising=False)
    desk = make_device("desk", "acme")
    assert cli(desk.root, "space", "create", "--label", "Acme Energy", "--json").code == 0
    space = cli(desk.root, "space", "show", "--json").json()["space_id"]
    fw = fake_workspace(desk.root, customer="Acme Energy", prefix="DEMO")
    fw.enable("orch-tix", {"sharing_path": str(_install_cli(desk.root))})
    tix = fw.load(ADDON)                                     # the real SubprocessRunner: argv, allowlist, timeout
    fw.ws._addons = AddonRegistry(fw.ws, {"orch-tix": tix})
    pctx = tix.ctx.provider_context()
    health = next(p for p in tix.obj.providers if p.id == "health").fetch(pctx, "default", None)
    assert health.health == "ok", health.message
    assert tix.obj.state.space()["space_id"] == space
    pump_all(fw.ws, fw.ws.addons)

    agent = Ops(fw.ws, Actor("agent", "claude-code", "cli", "e2e"))
    key = agent.new("Export the meter readings").id
    agent.ask(key, [{"text": "Which format?", "options": ["ISO 8601", "Local"], "recommended": "A"}])
    pump_all(fw.ws, fw.ws.addons)
    assert tix.obj.errors == []
    m = _mirror(sim, space)
    assert m["needs"] == "question"

    doc = _post_answer(sim, m, space, qid="Q1", value="A")
    assert doc["title"] == "Export the meter readings" and doc["redaction"] == "title" and "sections" not in doc
    inbox = next(p for p in tix.obj.providers if p.id == "inbox")
    assert inbox.fetch(pctx, "space", None).health == "ok"
    (pending,) = tix.obj.decisions(None)
    assert pending.anchor == "Q1" and pending.title == "Answer from phone: ISO 8601"
    with TestClient(create_app(fw.ws, "tok")) as c:
        fw.ws._addons = AddonRegistry(fw.ws, {"orch-tix": tix})   # startup reloaded only trusted addons
        assert c.get("/?token=tok").status_code == 200
        r = c.post("/addons/orch-tix/decisions", data={"id": pending.id, "choice": "apply"},
                   headers={"origin": "http://testserver"}, follow_redirects=False)
        assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]
    q1 = store.load(fw.ws, key)[1].meta["questions"][0]
    assert q1["answer"] == "A" and q1["via"] == "dashboard"
    inbox.fetch(pctx, "space", None)                         # acks first
    assert sim.request("GET", "/api/decisions", params={"space": space}).json()["decisions"][0]["ack"] == "applied"

    phone, _ = phones.pair(fw.ws.root, label="iPhone", addon="orch-tix")
    agent.ask(key, [{"text": "Which delimiter?", "options": [";", ","]}])
    pump_all(fw.ws, fw.ws.addons)
    got = {}
    waiter = threading.Thread(target=lambda: got.setdefault("e", wait_for_human(fw.ws, key, timeout=60, poll=0.2)))
    waiter.start()
    _post_answer(sim, _mirror(sim, space), space, qid="Q2", value="A", key=phone.key, phone_id=phone.id)
    inbox.fetch(pctx, "space", None)                         # direct apply, no desktop click
    waiter.join(70)
    assert got["e"].kind == "question.answered" and got["e"].via == "phone:iPhone"
    assert tix.obj.decisions(None) == []
    inbox.fetch(pctx, "space", None)
    acks = [d["ack"] for d in sim.request("GET", "/api/decisions", params={"space": space}).json()["decisions"]]
    assert acks == ["applied", "applied"]


def test_shared_files_round_trip(make_device, cli, sim, live_server, tmp_path, monkeypatch):
    """Task 11: upload through the addon, list it, download it as a FileResult, attach it to a ticket, make a
    public link (a Reveal), and mark a message as done — all through the real CLI against the loopback server."""
    if shutil.which("uv") is None:
        pytest.skip("the sharing wrapper needs uv")
    from orch.addons.api import FileResult, Reveal, Upload
    from orch.core.events import Actor
    from orch.core.ops import Ops
    from orch.testing import fake_workspace

    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "orch-user"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    desk = make_device("desk", "acme")
    fw = fake_workspace(desk.root, customer="Acme Energy", prefix="DEMO")
    fw.enable("orch-tix", {"sharing_path": str(_install_cli(desk.root))})
    tix = fw.load(ADDON)
    pctx = tix.ctx.provider_context()

    src = tix.ctx.state_dir / "in" / "upload-1"            # where core puts an accepts_file upload
    src.parent.mkdir(parents=True)
    src.write_bytes(b"meter;value\n1;2\n")
    shared = tix.obj.act("upload", "", pctx, upload=Upload(src, "readings.csv", src.stat().st_size, "text/csv"))
    assert shared.startswith("Shared as FILE")
    ref = shared.removeprefix("Shared as ")

    snap = next(p for p in tix.obj.providers if p.id == "files").fetch(pctx, "all", None)
    assert snap.health == "ok", snap.message
    assert [(i["id"], i["label"]) for i in snap.items] == [(ref, "readings.csv")]
    fw.cache("orch-tix", snap)                              # what the page offers: actions take only these

    got = tix.obj.act("download", ref, pctx)
    assert isinstance(got, FileResult) and Path(got.path).read_bytes() == b"meter;value\n1;2\n"

    key = Ops(fw.ws, Actor("agent", "claude-code", "cli", "e2e")).new("Readings").id
    tix.obj.state.link(key, by="you", auto=False)          # attach offers synced tickets only
    assert tix.obj.act("save_to_ticket", f"{ref}|{key}", pctx) == f"Attached {ref} to {key} as remote-readings.csv"
    assert (fw.ws.artifacts_dir / key / "remote-readings.csv").read_bytes() == b"meter;value\n1;2\n"

    link = tix.obj.act("public_link", ref, pctx)
    assert isinstance(link, Reveal) and link.text.startswith(live_server.url + "/")
    assert "#" in link.text                                 # the key lives only in the fragment

    other = make_device("vm", "acme")
    assert cli(other.root, "msg", "send", "--to", "project:acme", "-m", "nightly run finished", "--json").code == 0
    msgs = next(p for p in tix.obj.providers if p.id == "messages").fetch(pctx, "default", None)
    (m,) = msgs.items
    assert m["text"] == "nightly run finished"
    assert tix.obj.act("ack_message", m["id"], pctx) == "Done"
    assert next(p for p in tix.obj.providers if p.id == "messages").fetch(pctx, "default", None).items == ()


def test_upload_and_single_use_download_through_core_action_routes(make_device, sim, tmp_path, monkeypatch):
    """Task 11 review: the real Mission Control POSTs. Upload through core's accepts_file form field, then
    Download: core stages the FileResult and serves it once."""
    if shutil.which("uv") is None:
        pytest.skip("the sharing wrapper needs uv")
    from fastapi.testclient import TestClient
    from orch.addons.loader import AddonRegistry
    from orch.dashboard.app import create_app
    from orch.testing import fake_workspace

    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "orch-user"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    desk = make_device("desk", "acme")
    fw = fake_workspace(desk.root, customer="Acme Energy", prefix="DEMO")
    fw.enable("orch-tix", {"sharing_path": str(_install_cli(desk.root))})
    tix = fw.load(ADDON)
    origin = {"origin": "http://testserver"}
    with TestClient(create_app(fw.ws, "tok")) as c:
        fw.ws._addons = AddonRegistry(fw.ws, {"orch-tix": tix})   # startup reloaded only trusted addons
        assert c.get("/?token=tok").status_code == 200
        r = c.post("/addons/orch-tix/actions/upload", files={"file": ("readings.csv", b"meter;value\n1;2\n", "text/csv")},
                   headers=origin, follow_redirects=False)
        assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]
        assert "Shared+as+FILE" in r.headers["location"]
        assert not list((tix.ctx.state_dir / "in").glob("*"))             # core removed the upload copy
        pctx = tix.ctx.provider_context()
        snap = next(p for p in tix.obj.providers if p.id == "files").fetch(pctx, "all", None)
        fw.cache("orch-tix", snap)
        (item,) = snap.items
        r = c.post("/addons/orch-tix/actions/download", data={"target": item["id"]}, headers=origin,
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/addons/orch-tix/files/"), r.headers
        got = c.get(r.headers["location"])
        assert got.status_code == 200 and got.content == b"meter;value\n1;2\n"
        assert "attachment" in got.headers["content-disposition"]
        assert c.get(r.headers["location"]).status_code == 404               # single use
        r = c.post("/addons/orch-tix/actions/download", data={"target": "FILE999"}, headers=origin,
                   follow_redirects=False)
        assert "err=" in r.headers["location"]                                 # not offered: refused


def test_legacy_migration_into_a_real_orch_workspace(make_device, cli, sim, sharing, tmp_path, monkeypatch):
    """Task 12: `sharing tickets migrate --apply` with the real orch CLI into a local workspace."""
    from orch.core import store
    from orch.testing import fake_workspace
    from tests.helpers.tickets import sim_list, sim_ticket

    orch = shutil.which("orch")
    if orch is None:
        pytest.skip("needs the orch CLI on PATH")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "orch-user"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for var in ("CLAUDECODE", "ORCH_HARNESS", "ORCH_HOME", "CLAUDE_CODE_SESSION_ID", "ORCH_SESSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sharing, "_interactive", lambda: True)
    desk = make_device("desk", "acme")
    questions = {"text": "", "allow_text": True, "questions": [
        {"id": "fmt", "text": "Which timestamp format?", "type": "single", "options": ["ISO 8601", "Local time"],
         "recommended": "ISO 8601", "required": True}]}
    sim_ticket(sim, project="acme", title="Fix export", status="waiting", body="CSV is empty.", device=desk,
               questions=questions, fm={"acceptance": ["Opens in Excel"]})
    sim_ticket(sim, project="acme", title="Old one", status="done")
    fw = fake_workspace(tmp_path / "acme-ws", customer="Acme Energy", prefix="DEMO")
    r = cli(desk.root, "tickets", "migrate", "--apply", "--workspace", fw.root, "--orch", orch, "--json",
            stdin=b"migrate\n")
    assert r.code == 0, r.out + r.err
    (m,) = r.json()["migrated"]
    t = store.load(fw.ws, m["to"])[1]
    assert t.status == "backlog" and t.title == "Fix export"
    assert t.section("Ask").strip() == "CSV is empty."
    assert "- [ ] Opens in Excel" in t.section("Acceptance criteria")
    assert f"Was waiting on TIX as {m['from']}." in t.section("Context")
    (q,) = t.meta["questions"]
    assert q["text"] == "Which timestamp format?" and q["recommended"] == "A" and q["answer"] is None
    legacy = {x["id"]: x for x in sim_list(sim)}
    assert legacy[m["from"]]["archived_at"] and not next(x for x in legacy.values() if x["status"] == "done")["archived_at"]


def test_a_forged_legacy_body_gives_exactly_the_expected_sections(make_device, cli, sim, sharing, tmp_path,
                                                                    monkeypatch):
    """Task 12 review I1: `## Plan`, `## Log` and an unclosed fence in a legacy body stay text in the Ask."""
    from orch.core import store
    from orch.testing import fake_workspace
    from tests.helpers.tickets import sim_ticket

    orch = shutil.which("orch")
    if orch is None:
        pytest.skip("needs the orch CLI on PATH")
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "orch-user"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for var in ("CLAUDECODE", "ORCH_HARNESS", "ORCH_HOME", "CLAUDE_CODE_SESSION_ID", "ORCH_SESSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sharing, "_interactive", lambda: True)
    desk = make_device("desk", "acme")
    body = "Make it dark.\n## Plan\nsteal the plan\n## Log\n- fake log line\n```\nunclosed fence\n"
    sim_ticket(sim, project="acme", title="Forged", status="open", body=body,
               fm={"testing": {"run": ["```", "pytest"]}})
    fw = fake_workspace(tmp_path / "acme-ws", customer="Acme Energy", prefix="DEMO")
    r = cli(desk.root, "tickets", "migrate", "--apply", "--workspace", fw.root, "--orch", orch, "--json",
            stdin=b"migrate\n")
    assert r.code == 0, r.out + r.err
    (m,) = r.json()["migrated"]
    t = store.load(fw.ws, m["to"])[1]
    assert t.section("Plan").strip() == "" and "fake log line" not in t.section("Log")
    ask = t.section("Ask")
    assert "steal the plan" in ask and "unclosed fence" in ask and "fake log line" in ask
    assert "pytest" in t.section("Verification") and f"Was open on TIX as {m['from']}." in t.section("Context")
    filled = {k for k, v in t.sections.items() if v.strip() and k != "Log"}
    assert filled == {"Ask", "Verification", "Context"}                  # nothing forged a Plan or another section
