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


def test_a_reset_event_log_forgets_the_done_marks(tix_ws, tix, runner):
    """A log that starts over numbers from 1 again: marks left from the old numbering would make the periodic pass
    acknowledge every new event as 'already synced' and the phone would never hear of them."""
    prov = _setup(tix_ws, tix)
    ops = Ops(tix_ws.ws, AGENT)
    t = ops.new("Export")
    ops.ask(t.id, [{"text": "Which format?", "options": ["A", "B"]}])
    tix.obj.state.set_watch(10_000, {t.id: 10_000})            # the log was longer once
    prov.fetch(tix.ctx.provider_context(), "space", None)
    assert tix.obj.state.watch()["done"] == {}
    assert tix.obj.state.watch()["cursor"] < 10_000


def test_only_the_end_of_a_long_log_is_read(tmp_path, monkeypatch):
    import json
    log = tmp_path / "events.jsonl"
    log.write_text("".join(json.dumps({"seq": i, "ticket": "T-1", "kind": "note.added", "data": {"text": "x" * 80}}) + "\n"
                           for i in range(1, 501)))
    monkeypatch.setattr(watch, "TAIL_BYTES", 300)
    opened = []
    real = type(log).open

    def spy(self, *a, **k):
        f = real(self, *a, **k)
        orig = f.read
        f.read = lambda *x: (lambda b: (opened.append(len(b)), b)[1])(orig(*x))
        return f
    monkeypatch.setattr(type(log), "open", spy)
    events, newest = watch._read(log, 497)
    assert [e.seq for e in events] == [498, 499, 500] and newest == 500
    assert max(opened) < log.stat().st_size // 4                 # not the whole file
    assert watch._read(log, 1 << 62) == ([], 500)
    assert [e.seq for e in watch._read(log, 0)[0]][:2] == [1, 2]  # a cursor at the start still gets everything
