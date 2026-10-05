"""QA #54: the phone hears about a ticket at once (the watcher), and the periodic outbox pass does not push it again."""
import pytest
from helpers import PUSH, SHARING
from orch.addons.loader import AddonRegistry
from orch.addons.outbox import pump_all
from orch.core.events import Actor
from orch.core.ops import Ops

from orch_tix import watch

AGENT = Actor("agent", "claude-code", "cli", "s1")


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(watch, "WAIT_S", 0.0)       # one look at the log per fetch
    monkeypatch.setattr(watch, "POLL_S", 0.0)


def _setup(fw, tix):
    fw.ws._addons = AddonRegistry(fw.ws, {"orch-tix": tix})
    pump_all(fw.ws, fw.ws.addons)                   # the first pump sets the event cursor
    tix.obj.state.save_space({"server": "https://tix.example", "space_id": "f" * 32})
    prov = tix.obj.providers[1]
    assert prov.id == "needs-watch"
    prov.fetch(tix.ctx.provider_context(), "space", None)     # the first fetch starts at now
    return prov


def _pushes(runner):
    return [c for c in runner.calls if c[1:3] == ("mirror", "push")]


def test_a_question_reaches_the_phone_in_one_fetch_and_the_periodic_pass_adds_nothing(tix_ws, tix, runner):
    prov = _setup(tix_ws, tix)
    ops = Ops(tix_ws.ws, AGENT)
    t = ops.new("Export the meter readings")
    ops.ask(t.id, [{"text": "Which format?", "options": ["ISO 8601", "Local"]}])
    prov.fetch(tix.ctx.provider_context(), "space", None)     # no pump_all yet: this is the immediate path
    assert len(_pushes(runner)) == 1 and runner.files[0]["needs"] == "question"
    pump_all(tix_ws.ws, tix_ws.ws.addons)                      # the fallback pass finds it already done
    assert len(_pushes(runner)) == 1


def test_a_later_change_is_not_swallowed_by_the_done_mark(tix_ws, tix, runner):
    prov = _setup(tix_ws, tix)
    ops = Ops(tix_ws.ws, AGENT)
    t = ops.new("Export")
    ops.ask(t.id, [{"text": "Which format?", "options": ["A", "B"]}])
    prov.fetch(tix.ctx.provider_context(), "space", None)
    ops.ask(t.id, [{"text": "And the delimiter?", "options": [";", ","]}])
    pump_all(tix_ws.ws, tix_ws.ws.addons)                      # the watcher has not run for the second ask
    assert len(_pushes(runner)) == 2


def test_the_watcher_needs_a_space_and_a_link_mode(tix_ws, tix):
    prov = tix.obj.providers[1]
    assert prov.scopes(tix.ctx.provider_context()) == []       # no space yet
    tix.obj.state.save_space({"server": "https://tix.example", "space_id": "f" * 32})
    assert prov.scopes(tix.ctx.provider_context()) == ["space"]
    assert prov.scopes(type("C", (), {"settings": {"link_mode": "off"}, "addon": tix.ctx})()) == []


def test_a_failing_push_is_left_to_the_periodic_pass(tix_ws, tix, runner):
    from orch_tix.cli import SharingError
    prov = _setup(tix_ws, tix)
    ops = Ops(tix_ws.ws, AGENT)
    t = ops.new("Export")
    calls = {"n": 0}
    real = tix.obj.sharing

    def flaky(pctx):
        s = real(pctx)
        orig = s.run_json

        def run_json(*a, **k):
            if a[:2] == ("mirror", "push") and calls["n"] == 0:
                calls["n"] += 1
                raise SharingError("retry", "offline")
            return orig(*a, **k)
        s.run_json = run_json
        return s
    tix.obj.sharing = flaky
    ops.ask(t.id, [{"text": "Which format?", "options": ["A", "B"]}])
    prov.fetch(tix.ctx.provider_context(), "space", None)      # offline: nothing pushed, nothing marked done
    assert tix.obj.state.watch()["done"] == {}
    pump_all(tix_ws.ws, tix_ws.ws.addons)                      # the fallback retries and succeeds
    assert len(_pushes(runner)) == 1


def test_the_push_after_an_applied_phone_decision_says_phone_once_and_only_without_needs(tix_ws):
    """QA #55: Mission Control tells the server it applied a phone answer (decided_via), once."""
    from datetime import datetime, timezone

    from helpers import ADDON, CapturingRunner
    pushed = {"status": "pushed", "id": "TIX-42", "uuid": "0" * 32, "server_rev": None, "gen": 1}
    runner = CapturingRunner(strict=True).add(PUSH, stdout_json=pushed).add(PUSH, stdout_json=pushed).add(PUSH, stdout_json=pushed)
    tix = tix_ws.load(ADDON, runner=runner)
    ops = Ops(tix_ws.ws, AGENT)
    t = ops.new("Export")                                       # no needs
    tix.obj.state.link(t.id, by="you", auto=False)
    rec = {"kind": "answer", "ticket": t.id, "outcome": "applied", "received_at": datetime.now(timezone.utc).isoformat()}
    tix.obj.state.put_decision("dec_" + "a" * 32, rec)
    pctx = tix.ctx.provider_context()
    tix.obj.act("push_now", t.id, pctx)
    assert runner.files[-1]["decided_via"] == "phone"
    tix.obj.act("push_now", t.id, pctx)
    assert "decided_via" not in runner.files[-1]                # announced once
    tix.obj.state.put_decision("dec_" + "b" * 32, {**rec, "outcome": None})
    ops.ask(t.id, [{"text": "Which format?", "options": ["A", "B"]}])
    tix.obj.act("push_now", t.id, pctx)
    assert "decided_via" not in runner.files[-1]                # needs something again, or not decided: never
