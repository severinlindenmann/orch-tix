from helpers import ADDON, SHARING
from orch.addons.runtime import SlotView
from orch.addons.widgets import KV, Action, Callout, Card, Copy, Table, Text, Tile, widget_problems
from orch.core import store
from orch.core.events import Actor
from orch.core.ops import Ops

AGENT = Actor("agent", "claude-code", "cli", "s1")


def _walk(widgets):
    for w in widgets:
        yield w
        if isinstance(w, Card):
            yield from _walk(w.body)


def _view(fw, tix, slot, ticket=None):
    return SlotView(fw.ws, tix, slot, ticket)


def _kv(widgets) -> dict:
    return {k: v for w in _walk(widgets) if isinstance(w, KV) for k, v in w.rows}


def _check(tix, slot, widgets):
    for w in widgets:
        assert widget_problems(w, slot=slot, manifest=tix.manifest) == []


def test_workspace_section_shows_the_space(tix_ws, tix):
    tix.obj.state.save_space({"space_id": "f" * 32, "label": "Acme Energy", "server": "https://tix.example.invalid",
                              "device": "desk", "last_seen_at": "2026-10-02T09:40:00Z"})
    ws = tix.obj.widgets("workspace.settings", _view(tix_ws, tix, "workspace.settings"))
    _check(tix, "workspace.settings", ws)
    kv = _kv(ws)
    assert kv["Space"] == "Acme Energy" and kv["This device"] == "desk" and kv["Server"] == "https://tix.example.invalid"
    assert kv["Desktop last seen"] == "2026-10-02T09:40:00Z" and kv["Linked tickets"] == 0
    assert any(isinstance(w, Action) and w.action == "push_now" for w in _walk(ws))
    assert not any(isinstance(w, Callout) and w.title == "Set the sharing path" for w in _walk(ws))


def test_workspace_section_asks_for_the_cli_path(tix_ws, tix):
    tix_ws.enable("orch-tix", {"sharing_path": ""})
    ws = tix.obj.widgets("workspace.settings", _view(tix_ws, tix, "workspace.settings"))
    _check(tix, "workspace.settings", ws)
    assert any(isinstance(w, Callout) and w.title == "Set the sharing path" for w in _walk(ws))
    (copy,) = [w for w in _walk(ws) if isinstance(w, Copy)]
    assert copy.text == str(tix_ws.root / ".claude/skills/sharing/sharing")


def test_workspace_section_lists_errors_and_warns_at_full(tix_ws, tix):
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "redaction": "full"})
    tix.obj.note_error("push DEMO-0001: cannot reach the server")
    ws = tix.obj.widgets("workspace.settings", _view(tix_ws, tix, "workspace.settings"))
    titles = [w.title for w in _walk(ws) if isinstance(w, Callout)]
    assert "Other devices can read these tickets" not in titles          # the device list is unknown here
    assert any(isinstance(w, Text) and w.text == "Every TIX device can read synced tickets" for w in _walk(ws))
    assert any(isinstance(w, Callout) and w.role == "err" and "cannot reach" in w.text for w in _walk(ws))
    assert any(isinstance(w, Table) for w in _walk(ws))


def test_ticket_panel_not_linked(tix_ws, tix):
    tid = Ops(tix_ws.ws, AGENT).new("Export").id
    t = store.load(tix_ws.ws, tid)[1]
    ws = tix.obj.widgets("ticket.sync", _view(tix_ws, tix, "ticket.sync", t))
    _check(tix, "ticket.sync", ws)
    assert any(isinstance(w, Text) and w.text == "Not on the phone." for w in _walk(ws))
    (a,) = [w for w in _walk(ws) if isinstance(w, Action)]
    assert (a.action, a.target) == ("link", tid)


def test_ticket_panel_linked(tix_ws, tix, tmp_path):
    ops = Ops(tix_ws.ws, AGENT)
    tid = ops.new("Export").id
    f = tmp_path / "plot.png"
    f.write_bytes(b"x")
    ops.artifact_add(tid, f, "plot.png")
    tix.obj.state.link(tid, by="you", auto=False)
    tix.obj.state.commit_rev(tid, 3, "TIX-42", gen=1)
    t = store.load(tix_ws.ws, tid)[1]
    ws = tix.obj.widgets("ticket.sync", _view(tix_ws, tix, "ticket.sync", t))
    _check(tix, "ticket.sync", ws)
    kv = _kv(ws)
    assert kv["TIX"] == "TIX-42" and kv["Shows"] == "Title and questions" and kv["Last push"] == "rev 3"
    actions = {(w.action, w.target) for w in _walk(ws) if isinstance(w, Action)}
    assert {("push_now", tid), ("redaction", tid), ("unlink", tid), ("send_artifact", f"{tid}|plot.png")} <= actions


def test_summary_tile_only_with_pending_phone_answers(tix_ws, tix):
    assert tix.obj.widgets("today.summary", _view(tix_ws, tix, "today.summary")) == []
    tix.obj.state.put_decision("dec_" + "a" * 32, {"kind": "answer", "ticket": "DEMO-0001", "tix": "TIX-1",
                                                   "decision": {}, "received_at": "2026-10-02T09:00:00+00:00",
                                                   "outcome": None, "message": "", "acked": False})
    (tile,) = tix.obj.widgets("today.summary", _view(tix_ws, tix, "today.summary"))
    assert isinstance(tile, Tile) and tile.label == "Phone answers" and tile.value == 1


def test_ticket_panel_offers_no_send_at_key_only(tix_ws, tix, tmp_path):
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "redaction": "key-only"})
    ops = Ops(tix_ws.ws, AGENT)
    tid = ops.new("Export").id
    f = tmp_path / "plot.png"
    f.write_bytes(b"x")
    ops.artifact_add(tid, f, "plot.png")
    tix.obj.state.link(tid, by="you", auto=False)
    t = store.load(tix_ws.ws, tid)[1]
    ws = tix.obj.widgets("ticket.sync", _view(tix_ws, tix, "ticket.sync", t))
    assert not [w for w in _walk(ws) if isinstance(w, Action) and w.action == "send_artifact"]
