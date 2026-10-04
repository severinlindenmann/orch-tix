"""Feedback round E: the "Sent to TIX" log on the addon page: every push (fields by name, size, result) and every
phone decision received (outcome and reason), newest first, the last 200, in the addon's state dir (0600)."""
import json
import os
import stat

from helpers import ADDON, PUSH, SHARING, CapturingRunner, ok  # noqa: F401
from orch.addons.loader import AddonRegistry
from orch.addons.outbox import pump_all
from orch.addons.runtime import SlotView
from orch.addons.widgets import Card, Table, Time
from orch.core.events import Actor
from orch.core.ops import Ops
from orch.testing import FakeRemote
from orch.remote.verify import RemoteResult

AGENT = Actor("agent", "claude-code", "cli", "s1")
SECRETS = ("Which format?", "ISO 8601", "Export the meter readings")


def _walk(widgets):
    for w in widgets:
        yield w
        yield from _walk(getattr(w, "body", ()) or ())


def _setup(fw, tix):
    fw.ws._addons = AddonRegistry(fw.ws, {"orch-tix": tix})
    pump_all(fw.ws, fw.ws.addons)
    ops = Ops(fw.ws, AGENT)
    t = ops.new("Export the meter readings")
    ops.ask(t.id, [{"text": "Which format?", "options": ["ISO 8601", "Local"], "recommended": "A"}])
    pump_all(fw.ws, fw.ws.addons)
    return t.id


def test_a_push_is_logged_with_field_names_size_and_result_never_sealed_text(tix_ws, tix, runner):
    tid = _setup(tix_ws, tix)
    (e,) = tix.obj.state.sent_log()
    assert e["kind"] == "push" and e["key"] == tid and e["tix"] == "TIX-42" and e["level"] == "title"
    assert e["result"] == "ok" and e["size"] > 100
    assert "questions" in e["fields"] and "title" in e["fields"] and "sections" not in " ".join(e["fields"])
    assert not any(s in json.dumps(e) for s in SECRETS)
    path = tix.ctx.state_dir / "sentlog.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_failed_push_is_logged_as_retry(tix_ws):
    runner = CapturingRunner(strict=True)
    runner.add(PUSH, returncode=1, stdout_json={"error": "network", "detail": "server unreachable"})
    tix = tix_ws.load(ADDON, runner=runner)
    tid = _setup(tix_ws, tix)
    e = tix.obj.state.sent_log()[0]
    assert e["key"] == tid and e["result"] == "retry" and "network" in e["reason"]


def test_received_decisions_are_logged_with_their_outcome(tix_ws, tix, runner):
    tid = _setup(tix_ws, tix)
    tix.ctx.remote_hook = FakeRemote(RemoteResult("stale", "the question changed since"))
    item = {"id": "dec_" + "a" * 32, "seq": 1, "ticket": "TIX-42", "kind": "answer", "created_at": "2026-10-02T09:41:07Z",
            "error": None, "gen": 1,
            "decision": {"v": 1, "decision_id": "dec_" + "a" * 32, "space": "f" * 32, "ticket": tid, "kind": "answer",
                         "target": {"qid": "Q1", "hash": "sha256:" + "0" * 64}, "value": "A", "note": "",
                         "at": "2026-10-02T09:41:07Z"}}
    from datetime import datetime, timezone
    tix.obj.inbox_receive(tix.ctx.provider_context(), [item], now=datetime(2026, 10, 2, 9, 42, tzinfo=timezone.utc))
    e = tix.obj.state.sent_log()[0]
    assert e["kind"] == "decision answer" and e["key"] == tid and e["result"] == "refused" and e["reason"]


def test_the_log_is_bounded_and_newest_first(tix_ws, tix):
    for i in range(205):
        tix.obj.state.log({"kind": "push", "key": f"DEMO-{i:04d}", "result": "ok"})
    log = tix.obj.state.sent_log()
    assert len(log) == 200 and log[0]["key"] == "DEMO-0204" and log[-1]["key"] == "DEMO-0005"


def test_the_page_has_a_sent_to_tix_tab(tix_ws, tix, runner):
    tid = _setup(tix_ws, tix)
    widgets = tix.obj.widgets("page.orch-tix", SlotView(tix_ws.ws, tix, "page.orch-tix", None, {"tab": "log"}))
    card = next(w for w in _walk(widgets) if isinstance(w, Card) and w.title == "Sent to TIX")
    table = next(w for w in _walk(card.body) if isinstance(w, Table))
    assert table.columns == ("When", "Ticket", "What", "Size", "Result")
    row = table.rows[0]
    assert isinstance(row[0], Time) and row[1] == tid
    assert "push" in row[2].text and "title" in row[2].text and row[4].text == "ok"
