from pathlib import Path

from helpers import ADDON, PUSH, SHARING, CapturingRunner, ok
from orch.addons.loader import AddonRegistry
from orch.addons.outbox import Outbox, outbox_path, pump_all
from orch.core.events import Actor
from orch.core.ops import Ops

AGENT = Actor("agent", "claude-code", "cli", "s1")
SHARE = [SHARING, "share", "*", "--name", "*", "--ttl", "7d", "--tag", "context", "--json"]


def _registry(fw, tix):
    fw.ws._addons = AddonRegistry(fw.ws, {"orch-tix": tix})
    pump_all(fw.ws, fw.ws.addons)                     # the first pump only sets the event cursor


def _ask(fw, ops=None):
    ops = ops or Ops(fw.ws, AGENT)
    t = ops.new("Export the meter readings")
    ops.ask(t.id, [{"text": "Which format?", "options": ["ISO 8601", "Local"], "recommended": "A"}])
    return t.id


def _pushes(runner):
    return [c for c in runner.calls if c[1:3] == ("mirror", "push")]


def _pending(fw):
    return Outbox(outbox_path(fw.ws, "orch-tix")).pending()


def test_auto_on_question_links_and_pushes_once(tix_ws, tix, runner):
    _registry(tix_ws, tix)
    tid = _ask(tix_ws)
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    assert len(_pushes(runner)) == 1
    sent = runner.files[0]
    assert sent["key"] == tid and sent["rev"] == 1 and sent["needs"] == "question" and sent["doc"]["id"] == tid
    assert sent["gen"] == 1 and "--relink" not in _pushes(runner)[0]
    link = tix.obj.state.links()[tid]
    assert link["auto"] is True and link["rev"] == 1 and link["n"] == "TIX-42"
    assert _pending(tix_ws) == []
    out = tix.ctx.state_dir / "out"
    path = _pushes(runner)[0][_pushes(runner)[0].index("--file") + 1]
    assert path.startswith(str(out)) and str(tix_ws.root) in path           # inside the repo, for the CLI
    assert not list(out.glob("*"))                                           # payload files are removed


def test_manual_mode_never_links_by_itself(tix_ws, tix, runner):
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "link_mode": "manual"})
    _registry(tix_ws, tix)
    _ask(tix_ws)
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    assert _pushes(runner) == [] and tix.obj.state.links() == {}


def test_off_mode_acks_everything(tix_ws, tix, runner):
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "link_mode": "off"})
    _registry(tix_ws, tix)
    _ask(tix_ws)
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    assert _pushes(runner) == [] and _pending(tix_ws) == []


def test_unlinked_by_hand_is_never_auto_linked(tix_ws, tix, runner):
    runner.add([SHARING, "mirror", "unlink", "--key", "*", "--gen", "1", "--json"], stdout_json={"status": "unlinked"})
    _registry(tix_ws, tix)
    tid = _ask(tix_ws)
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    assert tix.obj.act("unlink", tid, tix.ctx.provider_context()) == f"Stopped syncing {tid}"
    assert tix.obj.state.links()[tid]["unlinked_by_hand"] is True
    Ops(tix_ws.ws, AGENT).ask(tid, [{"text": "And the delimiter?", "options": [";", ","]}])
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    assert len(_pushes(runner)) == 1                   # only the push before the unlink


def test_relink_by_hand_uses_relink_and_stores_the_gen(tix_ws):
    runner = CapturingRunner(strict=True)
    runner.add([SHARING, "mirror", "unlink", "--key", "*", "--gen", "1", "--json"], stdout_json={"status": "unlinked"})
    runner.add([SHARING, "mirror", "push", "--file", "*", "--relink", "--json"],
               stdout_json={"status": "pushed", "id": "TIX-77", "uuid": "1" * 32, "server_rev": None, "gen": 3})
    runner.add(PUSH, stdout_json={"status": "pushed", "id": "TIX-42", "uuid": "0" * 32, "server_rev": None, "gen": 1})
    tix = tix_ws.load(ADDON, runner=runner)
    tid = Ops(tix_ws.ws, AGENT).new("Export").id
    pctx = tix.ctx.provider_context()
    assert tix.obj.act("link", tid, pctx) == "Synced to TIX as TIX-42"
    assert tix.obj.act("unlink", tid, pctx) == f"Stopped syncing {tid}"
    assert tix.obj.act("link", tid, pctx) == "Synced to TIX as TIX-77"
    link = tix.obj.state.links()[tid]
    assert link["gen"] == 3 and link["n"] == "TIX-77" and not link.get("retired") and not link.get("relink")
    assert "--relink" in _pushes(runner)[-1] and runner.files[-1]["gen"] == 2


def test_gone_retires_the_link(tix_ws):
    runner = CapturingRunner(strict=True).add(PUSH, stdout_json={"status": "gone", "gen": 1})
    tix = tix_ws.load(ADDON, runner=runner)
    tid = Ops(tix_ws.ws, AGENT).new("Export").id
    tix.obj.state.link(tid, by="you", auto=False)
    assert "no longer on the phone" in tix.obj.act("push_now", tid, tix.ctx.provider_context())
    link = tix.obj.state.links()[tid]
    assert link["retired"] is True and link["unlinked_by_hand"] is False


def test_drain_coalesces_and_acks_on_stale(tix_ws):
    runner = CapturingRunner(strict=True).add(PUSH, stdout_json={"status": "stale", "id": None,
                                                                "uuid": "0" * 32, "server_rev": 6, "gen": 1})
    tix = tix_ws.load(ADDON, runner=runner)
    _registry(tix_ws, tix)
    ops = Ops(tix_ws.ws, AGENT)
    tid = _ask(tix_ws, ops)
    tix.obj.state.link(tid, by="you", auto=False)
    ops.log(tid, "working")
    ops.set_state(tid, "halfway")
    ops.ask(tid, [{"text": "Delimiter?", "options": [";", ","]}])
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    assert len(_pushes(runner)) == 1 and runner.files[0]["rev"] == 1
    assert _pending(tix_ws) == []
    assert tix.obj.state.links()[tid]["rev"] == 6      # raised to what the server already has


def test_failed_push_keeps_the_items_for_a_retry(tix_ws):
    runner = CapturingRunner(strict=True).add(PUSH, returncode=1,
                                              stdout_json={"error": "network", "detail": "cannot reach the server"})
    tix = tix_ws.load(ADDON, runner=runner)
    _registry(tix_ws, tix)
    tid = _ask(tix_ws)
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    assert _pending(tix_ws)
    assert tix.obj.state.links()[tid]["rev"] == 1              # reserved: the retry goes out as rev 2
    assert not tix.obj.state.links()[tid].get("n")
    assert any("cannot reach" in e for e in tix.obj.errors)


def test_context_artifact_is_shared_and_rides_along(tix_ws, tix, runner, tmp_path):
    _registry(tix_ws, tix)
    ops = Ops(tix_ws.ws, AGENT)
    tid = _ask(tix_ws, ops)
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    f = tmp_path / "plot.png"
    f.write_bytes(b"\x89PNG fake")
    ops.artifact_add(tid, f, "plot.png", context=True)
    ops.artifact_add(tid, f, "raw.png")                # not marked for context: stays on the desktop
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    shares = [c for c in runner.calls if c[1] == "share"]
    assert len(shares) == 1 and shares[0][shares[0].index("--name") + 1] == "plot.png"
    assert shares[0][2].endswith(f"{tid}/plot.png")
    assert runner.files[-1]["doc"]["context_artifacts"] == [{"name": "plot.png", "file": "FILE91"}]
    assert tix.obj.state.links()[tid]["context_artifacts"] == [{"name": "plot.png", "file": "FILE91"}]


def test_sync_artifacts_never_shares_nothing(tix_ws, tix, runner, tmp_path):
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "sync_artifacts": "never"})
    _registry(tix_ws, tix)
    ops = Ops(tix_ws.ws, AGENT)
    tid = _ask(tix_ws, ops)
    f = tmp_path / "plot.png"
    f.write_bytes(b"x")
    ops.artifact_add(tid, f, "plot.png", context=True)
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    assert not [c for c in runner.calls if c[1] == "share"]


def test_done_push_stores_done_at(tix_ws, tix, runner):
    ops = Ops(tix_ws.ws, AGENT)
    tid = ops.new("Export").id
    tix.obj.state.link(tid, by="you", auto=False)
    from orch.core.events import Actor as A
    Ops(tix_ws.ws, A("human", "you", "tty")).close(tid, "not needed")
    tix.obj.act("push_now", tid, tix.ctx.provider_context())
    assert runner.files[-1]["status"] == "done" and tix.obj.state.links()[tid]["done_at"]


def test_redaction_toggle_pushes_full_for_one_ticket(tix_ws, tix, runner):
    tid = Ops(tix_ws.ws, AGENT).new("Export").id
    pctx = tix.ctx.provider_context()
    tix.obj.act("link", tid, pctx)
    assert runner.files[-1]["doc"]["redaction"] == "title"
    tix.obj.act("redaction", tid, pctx)
    assert runner.files[-1]["doc"]["redaction"] == "full" and "sections" in runner.files[-1]["doc"]
    tix.obj.act("redaction", tid, pctx)
    assert runner.files[-1]["doc"]["redaction"] == "title"


# -- fix round 1: a retired link is never auto-linked ------------------------------------------------------------

def test_gone_after_a_manual_unlink_keeps_it_unlinked_by_hand(tix_ws):
    """A push in flight answers `gone` after the human pressed Stop syncing: the hand flag survives."""
    runner = CapturingRunner(strict=True)
    tix = tix_ws.load(ADDON, runner=runner)
    _registry(tix_ws, tix)
    tid = _ask(tix_ws)

    def push_races_the_unlink(argv, timeout):
        tix.obj.state.unlink(tid, by_hand=True)            # the human's unlink lands while the push is out
        return ok(argv, {"status": "gone", "gen": 1})

    runner.handlers["mirror"] = push_races_the_unlink
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    link = tix.obj.state.links()[tid]
    assert link["retired"] is True and link["unlinked_by_hand"] is True
    del runner.handlers["mirror"]
    Ops(tix_ws.ws, AGENT).ask(tid, [{"text": "And the delimiter?", "options": [";", ","]}])
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    assert len(_pushes(runner)) == 1


def test_a_link_retired_by_gone_is_never_auto_linked(tix_ws):
    runner = CapturingRunner(strict=True).add(PUSH, stdout_json={"status": "gone", "gen": 1})
    tix = tix_ws.load(ADDON, runner=runner)
    _registry(tix_ws, tix)
    tid = _ask(tix_ws)
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    assert tix.obj.state.links()[tid]["retired"] is True
    Ops(tix_ws.ws, AGENT).ask(tid, [{"text": "And the delimiter?", "options": [";", ","]}])
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    assert len(_pushes(runner)) == 1 and _pending(tix_ws) == []


def test_a_done_unlinked_ticket_is_never_auto_linked(tix_ws, tix, runner):
    _registry(tix_ws, tix)
    tid = _ask(tix_ws)
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    tix.obj.state.unlink(tid, by_hand=False)               # what the 7-day done cleanup does
    Ops(tix_ws.ws, AGENT).ask(tid, [{"text": "And the delimiter?", "options": [";", ","]}])
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    assert len(_pushes(runner)) == 1
    runner.add([SHARING, "mirror", "push", "--file", "*", "--relink", "--json"],
               stdout_json={"status": "pushed", "id": "TIX-43", "uuid": "1" * 32, "server_rev": None, "gen": 2})
    assert tix.obj.act("link", tid, tix.ctx.provider_context()).startswith("Synced to TIX")
    assert "--relink" in _pushes(runner)[-1]                # only the explicit link action re-links


# -- fix round 1: context artifacts --------------------------------------------------------------------------

def _artifact(tix_ws, tmp_path, tid, name="plot.png", context=True):
    f = tmp_path / name
    f.write_bytes(b"x")
    Ops(tix_ws.ws, AGENT).artifact_add(tid, f, name, context=context)


def test_key_only_workspace_shares_no_context_artifact(tix_ws, tix, runner, tmp_path):
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "redaction": "key-only", "sync_artifacts": "always"})
    _registry(tix_ws, tix)
    tid = _ask(tix_ws)
    _artifact(tix_ws, tmp_path, tid)
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    assert not [c for c in runner.calls if c[1] == "share"]
    assert len(_pushes(runner)) == 1 and "context_artifacts" not in runner.files[-1]["doc"]


def test_key_only_ticket_override_shares_no_context_artifact(tix_ws, tix, runner, tmp_path):
    _registry(tix_ws, tix)
    tid = _ask(tix_ws)
    tix.obj.state.link(tid, by="you", auto=False)
    tix.obj.state.set_redaction(tid, "key-only")
    _artifact(tix_ws, tmp_path, tid)
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    assert not [c for c in runner.calls if c[1] == "share"] and len(_pushes(runner)) == 1


def test_send_artifact_is_refused_at_key_only(tix_ws, tix, runner, tmp_path):
    import pytest
    from orch.errors import OrchError
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "redaction": "key-only"})
    tid = Ops(tix_ws.ws, AGENT).new("Export").id
    _artifact(tix_ws, tmp_path, tid)
    pctx = tix.ctx.provider_context()
    tix.obj.act("link", tid, pctx)
    with pytest.raises(OrchError, match="Key only"):
        tix.obj.act("send_artifact", f"{tid}|plot.png", pctx)
    assert not [c for c in runner.calls if c[1] == "share"]


def test_a_failed_share_is_noted_and_the_push_still_goes(tix_ws, tmp_path):
    runner = CapturingRunner(strict=True).add(PUSH, stdout_json={"status": "pushed", "id": "TIX-42", "uuid": "0" * 32,
                                                                "server_rev": None, "gen": 1})
    runner.add(SHARE, returncode=6, stdout_json={"error": "refused", "detail": "plot.png looks like a secret"})
    tix = tix_ws.load(ADDON, runner=runner)
    _registry(tix_ws, tix)
    tid = _ask(tix_ws)
    _artifact(tix_ws, tmp_path, tid)
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    assert len(_pushes(runner)) == 1 and _pending(tix_ws) == []
    assert any("plot.png" in e and "secret" in e for e in tix.obj.errors)
    assert runner.files[-1]["doc"]["context_artifacts"] == []


def test_the_rev_the_cli_stored_is_kept(tix_ws):
    """Final review I1: after a takeover the CLI pushes at server_rev + 1 and says so; the link follows."""
    runner = CapturingRunner(strict=True).add(PUSH, stdout_json={"status": "pushed", "id": "TIX-42", "uuid": "0" * 32,
                                                                "server_rev": 6, "gen": 1, "rev": 7})
    tix = tix_ws.load(ADDON, runner=runner)
    tid = Ops(tix_ws.ws, AGENT).new("Export").id
    assert tix.obj.act("link", tid, tix.ctx.provider_context()).startswith("Synced to TIX")
    assert tix.obj.state.links()[tid]["rev"] == 7


def test_a_push_that_timed_out_after_storing_never_hides_the_newer_snapshot(tix_ws):
    """Final review I4: the server stored rev 1, the CLI call timed out. The retry carries a newer doc; it must
    go out at a new rev (reserved before the call), not as rev 1 again (a duplicate, acked and lost)."""
    import json as _json
    from orch.addons.runner import AddonRunError
    server = {"rev": 0, "doc": None, "calls": 0}

    def mirror(argv, timeout):
        body = _json.loads(Path(argv[argv.index("--file") + 1]).read_text(encoding="utf-8"))
        server["calls"] += 1
        if body["rev"] <= server["rev"]:
            return ok(argv, {"status": "duplicate", "id": "TIX-42", "uuid": "0" * 32, "server_rev": server["rev"],
                             "gen": 1, "rev": body["rev"]})
        server.update(rev=body["rev"], doc=body["doc"])
        if server["calls"] == 1:
            raise AddonRunError("sharing timed out after 30 s")
        return ok(argv, {"status": "pushed", "id": "TIX-42", "uuid": "0" * 32, "server_rev": server["rev"] - 1,
                         "gen": 1, "rev": body["rev"]})

    runner = CapturingRunner(strict=True)
    runner.handlers["mirror"] = mirror
    tix = tix_ws.load(ADDON, runner=runner)
    _registry(tix_ws, tix)
    ops = Ops(tix_ws.ws, AGENT)
    tid = _ask(tix_ws, ops)
    pump_all(tix_ws.ws, tix_ws.ws.addons)                       # stored on the server, but the call timed out
    assert server["rev"] == 1 and _pending(tix_ws)
    ops.ask(tid, [{"text": "Delimiter?", "options": [";", ","]}])
    pump_all(tix_ws.ws, tix_ws.ws.addons)                       # the retry, with the newer doc
    assert server["rev"] == 2 and [q["text"] for q in server["doc"]["questions"]][-1] == "Delimiter?"
    assert _pending(tix_ws) == [] and tix.obj.state.links()[tid]["rev"] == 2


def test_the_phone_history_comes_from_events(tix_ws, tix, runner):
    """Night build part 2: each push carries the ticket's history from orch events (who, what), never Log text."""
    _registry(tix_ws, tix)
    tid = _ask(tix_ws)
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    hist = runner.files[0]["doc"]["history"]
    assert [(h["who"], h["what"]) for h in hist] == [("claude-code", "created the ticket"), ("claude-code", "asked Q1")]
    assert all(set(h) <= {"seq", "at", "who", "what"} for h in hist)          # title: no free text
    Ops(tix_ws.ws, AGENT).log(tid, "client detail that stays on the desktop")
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    last = runner.files[-1]["doc"]["history"]
    assert last[-1]["what"] == "logged a note" and "client detail" not in repr(runner.files[-1])
    assert [h["seq"] for h in last] == sorted({h["seq"] for h in last})


def test_a_decision_applied_through_the_addon_reaches_the_history_and_text_stays_off_title(tix_ws, tix, runner):
    _registry(tix_ws, tix)
    tid = _ask(tix_ws)
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    box = Outbox(outbox_path(tix_ws.ws, "orch-tix"))
    box.put({"op": "history", "ticket": tid, "seq": 999, "kind": "question.answered",
             "ev": {"seq": 999, "at": "2026-10-02T10:00Z", "who": "human (desktop log)", "what": "answered Q1",
                    "text": "never stored at title"}})
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    assert len(_pushes(runner)) == 1                                   # history alone pushes nothing
    kept = tix.obj.state.history(tid)
    assert kept[-1]["what"] == "answered Q1" and "text" not in kept[-1]
    Ops(tix_ws.ws, AGENT).ask(tid, [{"text": "And the delimiter?", "options": [";", ","]}])
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    sent = [h["what"] for h in runner.files[-1]["doc"]["history"]]
    assert "answered Q1" in sent and "asked Q2" in sent
