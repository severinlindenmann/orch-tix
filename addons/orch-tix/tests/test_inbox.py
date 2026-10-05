from datetime import datetime, timedelta, timezone

import pytest
from helpers import ADDON, REC, SHARING, CapturingRunner
from orch.addons.api import Intent, PendingDecision
from orch.core.events import Actor
from orch.core.ops import Ops
from orch.core.questions import question_hash
from orch.remote.verify import RemoteResult
from orch.testing import FakeRemote, Recording

NOW = datetime(2026, 10, 2, 9, 45, tzinfo=timezone.utc)
AGENT = Actor("agent", "claude-code", "cli", "s1")
HUMAN = Actor("human", "you", "tty")
WAIT = [SHARING, "inbox", "wait", "--after", "0", "--timeout", "25", "--json"]


def _decision(key, q=None, *, kind="answer", value="A", at="2026-10-02T09:41:07Z", did="a" * 32, target=None, note=""):
    if target is None:
        target = {"qid": q["id"], "hash": question_hash(q)} if kind == "answer" else {"gate": "plan", "hash": "sha256:" + "1" * 64}
    return {"id": "dec_" + did, "seq": 1, "ticket": "TIX-42", "kind": kind, "created_at": at, "error": None, "gen": 1,
            "decision": {"v": 1, "decision_id": "dec_" + did, "space": "f" * 32, "ticket": key, "kind": kind,
                         "target": target, "value": value, "note": note, "at": at, "pair": "ph_x", "mac": "m"}}


@pytest.fixture
def asked(tix_ws):
    ops = Ops(tix_ws.ws, AGENT)
    t = ops.new("Export")
    ops.ask(t.id, [{"text": "Which format?", "options": ["ISO 8601", "Local"]}])
    _link(tix_ws, t.id)
    return t.id


def _link(tix_ws, key):
    """The ticket is on the phone (a decision for a ticket without a local link is unlinked)."""
    from orch_tix.state import State
    State(tix_ws.ws.state_dir / "addons" / "orch-tix").link(key, by="auto", auto=True)
    return key


def _q(tix_ws, key):
    from orch.core import store
    return store.load(tix_ws.ws, key)[1].meta["questions"][0]


def test_paired_answer_applies_directly_and_acks(tix_ws, asked):
    runner = CapturingRunner(strict=True)          # fresh: the directory recordings would answer `inbox wait` first
    tix = tix_ws.load(ADDON, runner=runner)
    tix.obj.state.link(asked, by="auto", auto=True)
    remote = FakeRemote(RemoteResult("applied", "applied from iPhone", asked, 12))
    tix.ctx.remote_hook = remote
    runner.add(WAIT, stdout_json={"decisions": [_decision(asked, _q(tix_ws, asked))], "cursor": 1})
    runner.add([SHARING, "inbox", "ack", "dec_" + "a" * 32, "applied", "--json"],
               stdout_json={"id": "dec_" + "a" * 32, "ack": "applied"})
    p = next(p for p in tix.obj.providers if p.id == "inbox")
    assert p.always_on is True and p.mode == "long_poll"
    p.fetch(tix.ctx.provider_context(), "space", None)          # receive + direct apply
    assert remote.seen[0]["decision_id"] == "dec_" + "a" * 32 and remote.seen[0]["mac"] == "m"
    assert tix.obj.state.cursor() == 1
    p.fetch(tix.ctx.provider_context(), "space", None)          # sends the ack
    rec = tix.obj.state.decisions()["dec_" + "a" * 32]
    assert rec["outcome"] == "applied" and rec["acked"] is True
    assert tix.obj.decisions(None) == []
    assert [c[1:3] for c in runner.calls] == [("inbox", "wait"), ("inbox", "ack")]


@pytest.mark.parametrize("status,outcome", [("stale", "stale"), ("superseded", "superseded"),
                                            ("duplicate", "superseded"), ("answered-locally", "answered-locally"),
                                            ("unlinked", "unlinked")])
def test_remote_results_become_acks(tix, tix_ws, asked, status, outcome):
    tix.ctx.remote_hook = FakeRemote(RemoteResult(status, "x", asked))
    tix.obj.inbox_receive(tix.ctx.provider_context(), [_decision(asked, _q(tix_ws, asked))], now=NOW)
    assert next(iter(tix.obj.state.decisions().values()))["outcome"] == outcome


def test_unpaired_answer_becomes_apply_ignore_on_its_question(tix, tix_ws, asked, runner):
    tix.obj.state.link(asked, by="auto", auto=True)
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "this phone is not paired"))
    tix.obj.inbox_receive(tix.ctx.provider_context(), [_decision(asked, _q(tix_ws, asked), note="ok for Excel")],
                          now=NOW)
    (d,) = tix.obj.decisions(None)
    assert isinstance(d, PendingDecision) and d.anchor == "Q1" and d.ticket == asked and not d.stale
    assert d.title == "Answer from phone: ISO 8601" and d.choices == (("apply", "Apply"), ("ignore", "Ignore"))
    intent = tix.obj.resolve(d.id, "apply", None)
    assert intent == Intent("answer", ref=asked, qid="Q1", value="A", reason="ok for Excel",
                            expected_hash=question_hash(_q(tix_ws, asked)))
    assert tix.obj.state.decisions()[d.id]["outcome"] == "applying"
    assert tix.obj.decisions(None) == []
    tix.obj.on_intent_result(d.id, "applied", "ok")
    assert tix.obj.state.decisions()[d.id]["outcome"] == "applied"
    assert tix.obj.resolve(d.id, "apply", None) == "Already applied"


def test_multi_answer_title_joins_labels(tix, tix_ws):
    ops = Ops(tix_ws.ws, AGENT)
    t = ops.new("Export")
    ops.ask(t.id, [{"text": "Which columns?", "type": "multi", "options": ["Time", "Value", "Meter"]}])
    _link(tix_ws, t.id)
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "x"))
    tix.obj.inbox_receive(tix.ctx.provider_context(), [_decision(t.id, _q(tix_ws, t.id), value="A,C")], now=NOW)
    (d,) = tix.obj.decisions(None)
    assert d.title == "Answer from phone: Time, Meter"
    assert tix.obj.resolve(d.id, "apply", None).value == "A,C"


def test_refused_intent_is_stale(tix, tix_ws, asked):
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "x"))
    tix.obj.inbox_receive(tix.ctx.provider_context(), [_decision(asked, _q(tix_ws, asked))], now=NOW)
    (d,) = tix.obj.decisions(None)
    tix.obj.resolve(d.id, "apply", None)
    tix.obj.on_intent_result(d.id, "refused", "the question changed")
    rec = tix.obj.state.decisions()[d.id]
    assert rec["outcome"] == "stale" and rec["message"] == "the question changed"


def test_ignore_acks_ignored(tix, tix_ws, asked):
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "x"))
    tix.obj.inbox_receive(tix.ctx.provider_context(), [_decision(asked, _q(tix_ws, asked))], now=NOW)
    (d,) = tix.obj.decisions(None)
    assert tix.obj.resolve(d.id, "ignore", None) == "Ignored"
    assert tix.obj.state.decisions()[d.id]["outcome"] == "ignored"


def test_answered_on_the_desktop_meanwhile(tix, tix_ws, asked):
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "x"))
    tix.obj.inbox_receive(tix.ctx.provider_context(), [_decision(asked, _q(tix_ws, asked))], now=NOW)
    (d,) = tix.obj.decisions(None)
    Ops(tix_ws.ws, HUMAN).answer(asked, "Q1", "B", expected_hash=question_hash(_q(tix_ws, asked)))
    (again,) = tix.obj.decisions(None)
    assert again.stale is True                         # drawn stale before reconcile writes it
    from orch_tix.inbox import reconcile
    reconcile(tix.obj, tix.ctx.provider_context(), NOW)
    assert next(iter(tix.obj.state.decisions().values()))["outcome"] == "answered-locally"


def test_changed_question_is_stale(tix, tix_ws, asked):
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "x"))
    q = dict(_q(tix_ws, asked), text="Something else")
    tix.obj.inbox_receive(tix.ctx.provider_context(), [_decision(asked, q)], now=NOW)
    assert next(iter(tix.obj.state.decisions().values()))["outcome"] == "stale"


def test_old_decision_is_acked_stale(tix, tix_ws, asked):
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "x"))
    old = _decision(asked, _q(tix_ws, asked), at=(NOW - timedelta(days=15)).isoformat())
    tix.obj.inbox_receive(tix.ctx.provider_context(), [old], now=NOW)
    assert next(iter(tix.obj.state.decisions().values()))["outcome"] == "stale"


def test_decision_for_unlinked_ticket_is_acked_unlinked(tix, tix_ws, asked):
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "x"))
    d = _decision(asked, _q(tix_ws, asked))
    d["decision"]["ticket"] = "DEMO-0999"
    tix.obj.inbox_receive(tix.ctx.provider_context(), [d], now=NOW)
    assert next(iter(tix.obj.state.decisions().values()))["outcome"] == "unlinked"


def test_decision_for_a_retired_link_is_unlinked(tix, tix_ws, asked):
    tix.ctx.remote_hook = FakeRemote(RemoteResult("applied", "must not be called"))
    tix.obj.state.link(asked, by="you", auto=False)
    tix.obj.state.unlink(asked)
    tix.obj.inbox_receive(tix.ctx.provider_context(), [_decision(asked, _q(tix_ws, asked))], now=NOW)
    assert next(iter(tix.obj.state.decisions().values()))["outcome"] == "unlinked"
    assert tix.ctx.remote_hook.seen == []


def test_comment_is_logged_and_applied(tix, tix_ws, asked):
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "x"))
    d = _decision(asked, kind="comment", value="Looks fine to me", target={})
    tix.obj.inbox_receive(tix.ctx.provider_context(), [d], now=NOW)
    from orch.core import store
    assert "phone: Looks fine to me" in store.load(tix_ws.ws, asked)[1].section("Log")
    assert next(iter(tix.obj.state.decisions().values()))["outcome"] == "applied"


def test_a_move_from_the_phone_is_never_applied(tix, tix_ws, asked):
    tix.ctx.remote_hook = FakeRemote(RemoteResult("applied", "must not be called"))
    d = _decision(asked, kind="move", value="done", target={})
    tix.obj.inbox_receive(tix.ctx.provider_context(), [d], now=NOW)
    assert tix.obj.decisions(None) == [] and tix.ctx.remote_hook.seen == []
    assert next(iter(tix.obj.state.decisions().values()))["outcome"] == "stale"


def _plan_ticket(tix_ws):
    ops = Ops(tix_ws.ws, AGENT)
    t = ops.new("Export", size="l")
    ops.set_section(t.id, "Plan", "1. Do it")
    return _link(tix_ws, t.id)


def test_approval_and_request_changes_become_intents(tix, tix_ws):
    tid = _plan_ticket(tix_ws)
    gate_hash = tix.ctx.document(tid)["gates"]["plan"]["hash"]
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "x"))
    items = [_decision(tid, kind="approve", value=None, target={"gate": "plan", "hash": gate_hash}, did="1" * 32),
             _decision(tid, kind="request_changes", value="Split step 1", target={"gate": "plan", "hash": gate_hash},
                       did="2" * 32)]
    tix.obj.inbox_receive(tix.ctx.provider_context(), items, now=NOW)
    by_id = {d.id: d for d in tix.obj.decisions(None)}
    a, rc = by_id["dec_" + "1" * 32], by_id["dec_" + "2" * 32]
    assert a.title == "Approval from phone · plan" and a.anchor == "gate:plan"
    assert rc.title == "Changes requested from phone · plan" and rc.body == "Split step 1"
    assert tix.obj.resolve(a.id, "apply", None) == Intent("approve", ref=tid, gate="plan", expected_hash=gate_hash)
    assert tix.obj.resolve(rc.id, "apply", None) == Intent("request_changes", ref=tid, gate="plan",
                                                           reason="Split step 1", expected_hash=gate_hash)


def test_changed_gate_is_stale(tix, tix_ws):
    tid = _plan_ticket(tix_ws)
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "x"))
    tix.obj.inbox_receive(tix.ctx.provider_context(),
                          [_decision(tid, kind="approve", value=None, target={"gate": "plan", "hash": "sha256:" + "0" * 64})],
                          now=NOW)
    assert next(iter(tix.obj.state.decisions().values()))["outcome"] == "stale"


def test_verdict_maps_send_back_and_checks_the_round(tix, tix_ws):
    ops = Ops(tix_ws.ws, AGENT)
    tid = _link(tix_ws, ops.new("Export").id)
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "x"))
    tix.obj.inbox_receive(tix.ctx.provider_context(),
                          [_decision(tid, kind="verdict", value="follow-up", note="CSV is empty",
                                     target={"status": "testing", "round": 0})], now=NOW)
    assert next(iter(tix.obj.state.decisions().values()))["outcome"] == "stale"     # not in testing


def test_ticket_request_offers_create_in_backlog(tix, tix_ws):
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "x"))
    req = {"id": "dec_" + "b" * 32, "seq": 2, "ticket": None, "kind": "ticket_request", "created_at": "2026-10-02T09:41:07Z",
           "error": None, "decision": {"v": 1, "decision_id": "dec_" + "b" * 32, "kind": "ticket_request",
                                       "value": {"title": "Check the backup job", "body": "spoken"}, "at": "2026-10-02T09:41:07Z"}}
    tix.obj.inbox_receive(tix.ctx.provider_context(), [req], now=NOW)
    (d,) = tix.obj.decisions(None)
    assert d.choices == (("apply", "Create in backlog"), ("ignore", "Ignore")) and d.ticket is None
    assert d.title == "New ticket from phone" and d.body == "Check the backup job"
    assert tix.obj.resolve(d.id, "apply", None) == Intent("new", value="Check the backup job", reason="spoken")
    assert len(tix.ctx.remote_hook.seen) == 1          # handed to core first; the card is the pending fallback


def test_a_signed_ticket_request_is_created_by_core_without_a_desktop_card(tix, tix_ws):
    tix.ctx.remote_hook = FakeRemote(RemoteResult("applied", "created in backlog", "DEMO-0042", 31))
    req = {"id": "dec_" + "b" * 32, "seq": 2, "ticket": None, "kind": "ticket_request", "created_at": "2026-10-02T09:41:07Z",
           "error": None, "decision": {"v": 1, "decision_id": "dec_" + "b" * 32, "kind": "ticket_request", "space": "f" * 32,
                                       "value": {"title": "Check the backup job", "body": ""}, "at": "2026-10-02T09:41:07Z",
                                       "device": "iPhone", "pair": "ph_x", "mac": "m"}}
    tix.obj.inbox_receive(tix.ctx.provider_context(), [req], now=NOW)
    assert tix.obj.decisions(None) == []
    rec = next(iter(tix.obj.state.decisions().values()))
    assert rec["outcome"] == "applied" and rec["ticket"] == "DEMO-0042"


def test_requirements_and_plan_together_check_both_hashes_and_never_apply_half(tix, tix_ws):
    tid = _plan_ticket(tix_ws)
    doc = tix.ctx.document(tid)
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "kind switched off"))
    good = {"gate": "requirements", "hash": doc["gates"]["requirements"]["hash"], "plan_hash": doc["gates"]["plan"]["hash"]}
    stale = {**good, "plan_hash": "sha256:" + "9" * 64}
    tix.obj.inbox_receive(tix.ctx.provider_context(), [
        _decision(tid, kind="approve", value=None, target=good),
        _decision(tid, kind="approve", value=None, target=stale, did="d" * 32)], now=NOW)
    recs = tix.obj.state.decisions()
    assert recs["dec_" + "d" * 32]["outcome"] == "stale" and "plan changed" in recs["dec_" + "d" * 32]["message"]
    (card,) = tix.obj.decisions(None)
    assert card.choices == (("ignore", "Ignore"),) and "approve them on the ticket page" in card.body
    assert isinstance(tix.obj.resolve(card.id, "apply", None), str)        # refused: never the requirements alone
    assert recs["dec_" + "a" * 32]["outcome"] is None


def test_undecryptable_item_is_stale_not_a_crash(tix):
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "x"))
    bad = {"id": "dec_" + "c" * 32, "seq": 3, "ticket": "TIX-1", "kind": "answer", "created_at": "2026-10-02T09:41:07Z",
           "decision": None, "error": "integrity"}
    tix.obj.inbox_receive(tix.ctx.provider_context(), [bad], now=NOW)
    rec = tix.obj.state.decisions()["dec_" + "c" * 32]
    assert rec["outcome"] == "stale" and "decrypted" in rec["message"]


def test_ack_conflict_after_unlink_counts_as_done(tix_ws, asked):
    runner = CapturingRunner(strict=True)
    runner.add([SHARING, "inbox", "ack", "*", "stale", "--json"], returncode=7,
               stdout_json={"error": "conflict", "detail": "already acked as unlinked"})
    tix = tix_ws.load(ADDON, runner=runner)
    tix.obj.state.put_decision("dec_" + "d" * 32, {"kind": "answer", "ticket": asked, "tix": "TIX-1", "decision": {},
                                                   "received_at": NOW.isoformat(), "outcome": "stale", "message": "",
                                                   "acked": False})
    p = next(p for p in tix.obj.providers if p.id == "inbox")
    p.fetch(tix.ctx.provider_context(), "space", None)
    assert tix.obj.state.decisions()["dec_" + "d" * 32]["acked"] is True


def test_offline_inbox_reports_health(tix_ws):
    runner = CapturingRunner(strict=True).add(WAIT, returncode=4, stdout_json={"error": "network",
                                                                              "detail": "cannot reach the server"})
    tix = tix_ws.load(ADDON, runner=runner)
    snap = next(p for p in tix.obj.providers if p.id == "inbox").fetch(tix.ctx.provider_context(), "space", None)
    assert snap.health == "offline" and "cannot reach" in snap.message


def test_inbox_scope_needs_a_space_and_a_link_mode(tix_ws, tix):
    p = next(p for p in tix.obj.providers if p.id == "inbox")
    pctx = tix.ctx.provider_context()
    assert p.scopes(pctx) == []
    tix.obj.state.save_space({"space_id": "f" * 32, "label": "Acme", "server": "https://tix.example.invalid"})
    assert p.scopes(pctx) == ["space"]
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "link_mode": "off"})
    assert p.scopes(pctx) == []


def test_stuck_applying_goes_back_to_pending(tix, tix_ws, asked):
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "x"))
    tix.obj.inbox_receive(tix.ctx.provider_context(), [_decision(asked, _q(tix_ws, asked))], now=NOW)
    (d,) = tix.obj.decisions(None)
    tix.obj.resolve(d.id, "apply", None)
    tix.obj.state.update_decision(d.id, applying_at=(NOW - timedelta(minutes=11)).isoformat())
    from orch_tix.inbox import reconcile
    reconcile(tix.obj, tix.ctx.provider_context(), NOW)
    assert tix.obj.state.decisions()[d.id]["outcome"] is None


def test_pairing_target_uses_the_cached_space(tix):
    assert tix.obj.pairing_target(None) is None
    tix.obj.state.save_space({"space_id": "f" * 32, "label": "Acme Energy", "server": "https://tix.example.invalid"})
    t = tix.obj.pairing_target(None)
    assert t.url == "https://tix.example.invalid/pair#" + "f" * 32 and t.label == "TIX"
    tix.obj.state.save_space({"space_id": "f" * 32, "label": "Acme", "server": "http://127.0.0.1:8000"})
    assert tix.obj.pairing_target(None) is None        # a pairing link is https only


def test_health_caches_the_space(tix_ws, tix):
    snap = next(p for p in tix.obj.providers if p.id == "health").fetch(tix.ctx.provider_context(), "default", None)
    assert snap.health == "ok"
    space = tix.obj.state.space()
    assert space["space_id"] == "f" * 32 and space["device"] == "desk" and space["server"] == "https://tix.example.invalid"


def test_health_without_a_space_says_how(tix_ws):
    runner = CapturingRunner.from_dir(REC, strict=True)
    runner.recordings.insert(0, Recording((SHARING, "space", "show", "--json"), 3,
                                          '{"error": "no_space", "detail": "no space"}'))
    tix = tix_ws.load(ADDON, runner=runner)
    snap = next(p for p in tix.obj.providers if p.id == "health").fetch(tix.ctx.provider_context(), "default", None)
    assert snap.health == "auth_required" and "sharing space create" in snap.message


def test_unset_cli_path_is_not_a_login_problem(tix_ws):
    """QA TF-02: with no sharing path nothing needs a login; the health and inbox providers say so quietly."""
    tix_ws.enable("orch-tix", {"sharing_path": ""})
    tix = tix_ws.load(ADDON, runner=CapturingRunner.from_dir(REC, strict=True))
    for pid, scope in (("health", "default"), ("inbox", "space")):
        snap = next(p for p in tix.obj.providers if p.id == pid).fetch(tix.ctx.provider_context(), scope, None)
        assert snap.health == "never_fetched", pid
        assert "sharing CLI path" in snap.message


def test_done_ticket_unlinks_after_7_days(tix_ws, tix, runner):
    tid = Ops(tix_ws.ws, AGENT).new("Export").id
    tix.obj.state.link(tid, by="you", auto=False)
    tix.obj.state.commit_rev(tid, 1, "TIX-42", gen=1, done=True)
    links = tix.obj.state.links()
    links[tid]["done_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    tix.obj.state._write("links.json", links)
    next(p for p in tix.obj.providers if p.id == "health").fetch(tix.ctx.provider_context(), "default", None)
    assert [c for c in runner.calls if c[1:3] == ("mirror", "unlink")]
    link = tix.obj.state.links()[tid]
    assert link["retired"] is True and link["unlinked_by_hand"] is False and link["retired_why"] == "done"


def test_full_redaction_with_foreign_devices_warns(tix_ws, tix):
    from orch.addons.runtime import SlotView
    from orch.addons.widgets import Callout, Card
    from orch.testing import make_snapshot

    def callouts():
        out = []
        for w in tix.obj.widgets("workspace.settings", SlotView(tix_ws.ws, tix, "workspace.settings")):
            out += [c.title for c in (w.body if isinstance(w, Card) else ()) if isinstance(c, Callout)]
        return out

    tix.obj.state.save_space({"space_id": "f" * 32, "label": "Acme", "project": "acme"})
    tix_ws.cache("orch-tix", make_snapshot("devices", "default", items=[
        {"id": "d1", "label": "desk", "role": "ok", "text": "active", "project": "acme"}]))
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "redaction": "full"})
    assert "Other devices can read these tickets" not in callouts()
    tix_ws.cache("orch-tix", make_snapshot("devices", "default", items=[]))      # unknown (browser only)
    assert "Other devices can read these tickets" not in callouts()
    tix_ws.cache("orch-tix", make_snapshot("devices", "default", items=[
        {"id": "d1", "label": "desk", "role": "ok", "text": "active", "project": "acme"},
        {"id": "d2", "label": "vm", "role": "ok", "text": "active", "project": "other-client"}]))
    assert "Other devices can read these tickets" in callouts()
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "redaction": "title"})
    assert "Other devices can read these tickets" not in callouts()


def test_devices_are_browser_only_and_say_so(tix_ws, tix):
    snap = next(p for p in tix.obj.providers if p.id == "devices").fetch(tix.ctx.provider_context(), "default", None)
    assert snap.health == "ok" and snap.items == () and "browser" in snap.message


def test_messages_naming_a_linked_ticket_are_logged_once(tix_ws, asked):
    msg = {"id": "msg_" + "e" * 32, "seq": 5, "from": "ingest-vm", "to": "project:acme", "kind": "text",
           "text": "nightly run finished", "files": ["FILE88"], "ticket": "TIX-42", "created_at": "2026-10-02T09:41:07Z",
           "error": None}
    runner = CapturingRunner(strict=True).add([SHARING, "msg", "list", "--json"],
                                              stdout_json={"messages": [msg], "cursor": 5})
    tix = tix_ws.load(ADDON, runner=runner)
    tix.obj.state.link(asked, by="you", auto=False)
    tix.obj.state.commit_rev(asked, 1, "TIX-42", gen=1)
    p = next(p for p in tix.obj.providers if p.id == "messages")
    snap = p.fetch(tix.ctx.provider_context(), "default", None)
    p.fetch(tix.ctx.provider_context(), "default", None)
    from orch.core import store
    log = store.load(tix_ws.ws, asked)[1].section("Log")
    assert log.count("ingest-vm: nightly run finished") == 1
    assert snap.items[0]["label"] == "ingest-vm" and snap.items[0]["role"] == "info"


# -- fix round 1 --------------------------------------------------------------------------------------------------

def test_decision_for_a_never_linked_ticket_is_unlinked(tix, tix_ws):
    tid = Ops(tix_ws.ws, AGENT).new("Export").id
    Ops(tix_ws.ws, AGENT).ask(tid, [{"text": "Which format?", "options": ["ISO 8601", "Local"]}])
    tix.ctx.remote_hook = FakeRemote(RemoteResult("applied", "must not be called"))
    tix.obj.inbox_receive(tix.ctx.provider_context(), [_decision(tid, _q(tix_ws, tid))], now=NOW)
    assert next(iter(tix.obj.state.decisions().values()))["outcome"] == "unlinked"
    assert tix.ctx.remote_hook.seen == []


def _testing_ticket(tix_ws):
    from orch.clock import stamp
    from orch.core import store
    from orch.core.ids import next_id
    from orch.core.model import new_ticket
    t = new_ticket(next_id(tix_ws.ws), "Export", type="feature", priority="normal", size="m", created=stamp())
    t.meta["status"] = "testing"
    store.save(tix_ws.ws, t)
    return _link(tix_ws, t.id)


@pytest.mark.parametrize("value,expected", [("done", "done"), ("follow-up", "follow-up"), ("send_back", "follow-up")])
def test_verdict_values_map_to_core_names(tix, tix_ws, value, expected):
    tid = _testing_ticket(tix_ws)
    rnd = tix.ctx.document(tid)["needs"][0].get("round", 0)
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "x"))
    vh = tix.ctx.document(tid)["verdict"]["hash"]
    tix.obj.inbox_receive(tix.ctx.provider_context(), [_decision(tid, kind="verdict", value=value, note="why",
                                                                  target={"status": "testing", "round": rnd, "hash": vh})], now=NOW)
    (d,) = tix.obj.decisions(None)
    # the verdict intent carries the verdict hash the phone showed (core refuses one without it)
    assert tix.obj.resolve(d.id, "apply", None) == Intent("verdict", ref=tid, value=expected, reason="why", expected_hash=vh)


def test_a_verdict_without_the_verdict_hash_or_with_another_is_stale(tix, tix_ws):
    tid = _testing_ticket(tix_ws)
    rnd = tix.ctx.document(tid)["needs"][0].get("round", 0)
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", "x"))
    tix.obj.inbox_receive(tix.ctx.provider_context(), [
        _decision(tid, kind="verdict", value="done", target={"status": "testing", "round": rnd}),
        _decision(tid, kind="verdict", value="done", did="e" * 32,
                  target={"status": "testing", "round": rnd, "hash": "sha256:" + "0" * 64})], now=NOW)
    outcomes = sorted(r["outcome"] for r in tix.obj.state.decisions().values())
    assert outcomes == ["stale", "stale"]


def test_an_unknown_verdict_value_is_stale(tix, tix_ws):
    tid = _testing_ticket(tix_ws)
    rnd = tix.ctx.document(tid)["needs"][0].get("round", 0)
    tix.ctx.remote_hook = FakeRemote(RemoteResult("applied", "must not be called"))
    tix.obj.inbox_receive(tix.ctx.provider_context(), [_decision(tid, kind="verdict", value="maybe",
                                                                  target={"status": "testing", "round": rnd})], now=NOW)
    assert next(iter(tix.obj.state.decisions().values()))["outcome"] == "stale"
    assert tix.obj.decisions(None) == [] and tix.ctx.remote_hook.seen == []


def test_a_failing_ack_is_marked_and_does_not_block_the_wait(tix_ws, asked):
    runner = CapturingRunner(strict=True)
    runner.add([SHARING, "inbox", "ack", "*", "stale", "--json"], returncode=1,
               stdout_json={"error": "bad_request", "detail": "ack must be one of applied"})
    runner.add(WAIT, stdout_json={"decisions": [], "cursor": 0})
    tix = tix_ws.load(ADDON, runner=runner)
    tix.obj.state.put_decision("dec_" + "d" * 32, {"kind": "answer", "ticket": asked, "tix": "TIX-1", "decision": {},
                                                   "received_at": NOW.isoformat(), "outcome": "stale", "message": "",
                                                   "acked": False})
    snap = next(p for p in tix.obj.providers if p.id == "inbox").fetch(tix.ctx.provider_context(), "space", None)
    assert snap.health == "ok"
    rec = tix.obj.state.decisions()["dec_" + "d" * 32]
    assert rec["acked"] is False and "ack must be" in rec["ack_error"]
    assert [c[1:3] for c in runner.calls] == [("inbox", "ack"), ("inbox", "wait")]


# ---- feedback fix round F1: a held decision is acked non-finally with why, so the phone can say it ----

@pytest.mark.parametrize("message,code", [
    ("this phone is not paired with this workspace", "waiting-unpaired"),
    ("the phone's signature did not verify", "waiting-signature"),
    ("approve from the phone is switched off (Workspace → Phones); apply it here", "waiting-switched-off"),
    ("the decision's time is not plausible; apply it on the desktop if it is right", "waiting-time"),
    ("something else", "waiting-check")])
def test_a_held_decision_is_acked_waiting_with_its_reason(tix_ws, asked, message, code):
    runner = CapturingRunner(strict=True)
    tix = tix_ws.load(ADDON, runner=runner)
    tix.obj.state.link(asked, by="auto", auto=True)
    tix.ctx.remote_hook = FakeRemote(RemoteResult("pending", message))
    runner.add(WAIT, stdout_json={"decisions": [_decision(asked, _q(tix_ws, asked))], "cursor": 1})
    runner.add([SHARING, "inbox", "ack", "dec_" + "a" * 32, code, "--json"], stdout_json={"id": "x", "ack": code})
    runner.add([SHARING, "inbox", "wait", "--after", "1", "--timeout", "25", "--json"],
               stdout_json={"decisions": [], "cursor": 1})
    p = next(p for p in tix.obj.providers if p.id == "inbox")
    p.fetch(tix.ctx.provider_context(), "space", None)
    p.fetch(tix.ctx.provider_context(), "space", None)                 # the waiting ack goes out once
    p.fetch(tix.ctx.provider_context(), "space", None)
    assert [c[1:5] for c in runner.calls if c[1:3] == ("inbox", "ack")] == [("inbox", "ack", "dec_" + "a" * 32, code)]
    rec = tix.obj.state.decisions()["dec_" + "a" * 32]
    assert rec["outcome"] is None and rec["waiting_acked"] is True      # still on the desktop for an Apply


# ---- orch-core API 2.4: the reason is RemoteResult.code, never the message text ----

def _held(tix_ws, asked, result, did="a" * 32):
    tix = tix_ws.load(ADDON, runner=CapturingRunner(strict=False))
    tix.obj.state.link(asked, by="auto", auto=True)
    tix.ctx.remote_hook = FakeRemote(result)
    tix.obj.inbox_receive(tix.ctx.provider_context(), [_decision(asked, _q(tix_ws, asked), did=did)], now=NOW)
    return tix.obj.state.decisions()["dec_" + did]


@pytest.mark.parametrize("code,waiting", [
    ("not-paired", "waiting-unpaired"), ("bad-signature", "waiting-signature"),
    ("kind-switched-off", "waiting-switched-off"), ("implausible-time", "waiting-time"),
    ("malformed", "waiting-check"), ("kind-not-allowed", "waiting-check"), ("question-not-found", "waiting-check"),
    ("refused-retry", "waiting-check"), ("a-code-from-the-future", "waiting-check")])
def test_a_pending_code_picks_the_waiting_reason_whatever_the_message_says(tix_ws, asked, code, waiting):
    rec = _held(tix_ws, asked, RemoteResult("pending", "the signature is not paired and switched off", code=code))
    assert rec["outcome"] is None and rec["waiting"] == waiting


def test_refused_final_is_final_and_refused_retry_keeps_waiting(tix_ws, asked):
    assert _held(tix_ws, asked, RemoteResult("stale", "validation refused", code="refused-final"))["outcome"] == "stale"
    retry = _held(tix_ws, asked, RemoteResult("pending", "try later", code="refused-retry"), did="c" * 32)
    assert retry["outcome"] is None and retry["waiting"] == "waiting-check"


def test_an_unknown_code_falls_back_by_status(tix_ws, asked):
    assert _held(tix_ws, asked, RemoteResult("superseded", "x", code="new-code"))["outcome"] == "superseded"
