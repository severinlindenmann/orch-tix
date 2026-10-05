"""Per-ticket phone notifications on the desktop side: default off, the human's choice reaches the mirror as the
cleartext `notify`, the phone's change comes back, and messages without a ticket follow the workspace setting."""
import json

import pytest

from helpers import ADDON, PUSH, SHARING, CapturingRunner, ok
from orch.addons.loader import AddonRegistry
from orch.addons.outbox import pump_all
from orch.core.events import Actor
from orch.core.ops import Ops

AGENT = Actor("agent", "claude-code", "cli", "s1")
STATE = [SHARING, "mirror", "notify-state", "--json"]


def _setup(tix_ws, **answers):
    runner = CapturingRunner(strict=True)
    runner.add(PUSH, stdout_json={"status": "pushed", "id": "TIX-42", "uuid": "0" * 32, "server_rev": None, "gen": 1,
                                  **answers})
    tix = tix_ws.load(ADDON, runner=runner)
    tix_ws.ws._addons = AddonRegistry(tix_ws.ws, {"orch-tix": tix})
    tid = Ops(tix_ws.ws, AGENT).new("Export the meter readings").id
    return tix, runner, tid, tix.ctx.provider_context()


def _human_sets(ws, tid, on):
    from orch.addons import ticket_options
    return ticket_options.set_value(ws, tid, "orch-tix", "notify", on, Actor("human", "you", "dashboard"))


def test_manifest_declares_the_option_off_by_default_and_the_message_setting_off():
    data = json.loads((ADDON / "orch-addon.json").read_text())
    opt, = data["ticket_options"]
    assert opt["id"] == "notify" and opt["default"] is False and "Notify my phone" in opt["label"]
    field = next(f for f in data["settings_schema"] if f["key"] == "notify_unticketed")
    assert field["type"] == "bool" and field["default"] is False
    from orch.addons.manifest import parse_manifest
    assert parse_manifest(data).ticket_option("notify") is not None


def test_a_synced_ticket_carries_notify_off_unless_a_human_turned_it_on(tix_ws):
    tix, runner, tid, pctx = _setup(tix_ws)
    tix.obj.act("link", tid, pctx)
    assert runner.files[-1]["notify"] is False and runner.files[-1]["notify_seen"] == 0
    assert _human_sets(tix_ws.ws, tid, True) is True
    tix.obj.act("push_now", tid, pctx)
    assert runner.files[-1]["notify"] is True


def test_the_flag_never_enters_the_sealed_doc(tix_ws):
    tix, runner, tid, pctx = _setup(tix_ws)
    _human_sets(tix_ws.ws, tid, True)
    tix.obj.act("link", tid, pctx)
    assert "notify" not in json.dumps(runner.files[-1]["doc"])


def test_an_agent_cannot_turn_it_on(tix_ws):
    from orch.errors import HumanOnlyError
    from orch.addons import ticket_options
    tix, runner, tid, pctx = _setup(tix_ws)
    with pytest.raises(HumanOnlyError):
        ticket_options.set_value(tix_ws.ws, tid, "orch-tix", "notify", True, AGENT)
    assert tix.ctx.ticket_option(tid, "notify") is False


def test_changing_the_option_on_the_desktop_syncs_the_ticket(tix_ws):
    tix, runner, tid, pctx = _setup(tix_ws)
    tix.obj.act("link", tid, pctx)
    pump_all(tix_ws.ws, tix_ws.ws.addons)             # sets the cursor past the events so far
    n = len([c for c in runner.calls if c[1:3] == ("mirror", "push")])
    _human_sets(tix_ws.ws, tid, True)
    pump_all(tix_ws.ws, tix_ws.ws.addons)
    pushes = [c for c in runner.calls if c[1:3] == ("mirror", "push")]
    assert len(pushes) == n + 1 and runner.files[-1]["notify"] is True


def test_a_phone_change_is_adopted_as_the_addon_not_as_the_human(tix_ws):
    tix, runner, tid, pctx = _setup(tix_ws)
    tix.obj.act("link", tid, pctx)
    runner.add(STATE, stdout_json={"messages": False, "mirrors": [{"id": "TIX-42", "uuid": "0" * 32, "notify": True, "notify_rev": 1}]})
    from orch_tix import notify
    assert notify.merge_phone_changes(tix.obj, pctx) == 1
    assert tix.ctx.ticket_option(tid, "notify") is True
    assert tix.obj.state.links()[tid]["notify_seen"] == 1
    from orch.core.events import read_events
    assert [e.via for e in read_events(tix_ws.ws, tid) if e.kind == "ticket.option"] == ["addon:orch-tix"]
    assert notify.merge_phone_changes(tix.obj, pctx) == 0           # the same counter is not merged twice
    tix.obj.act("push_now", tid, pctx)
    assert runner.files[-1]["notify"] is True and runner.files[-1]["notify_seen"] == 1


def test_a_push_that_the_server_answers_with_the_phones_choice_adopts_it(tix_ws):
    tix, runner, tid, pctx = _setup(tix_ws, notify=True, notify_rev=3)      # we sent off, the phone had turned it on
    tix.obj.act("link", tid, pctx)
    assert tix.ctx.ticket_option(tid, "notify") is True
    assert tix.obj.state.links()[tid]["notify_seen"] == 3


def test_a_push_the_server_accepted_only_records_the_counter(tix_ws):
    tix, runner, tid, pctx = _setup(tix_ws, notify=False, notify_rev=2)
    tix.obj.act("link", tid, pctx)
    assert tix.ctx.ticket_option(tid, "notify") is False
    assert tix.obj.state.links()[tid]["notify_seen"] == 2


def test_messages_without_a_ticket_follow_the_workspace_setting(tix_ws):
    runner = CapturingRunner(strict=True)
    runner.add([SHARING, "space", "notify", "--messages", "*", "--json"], stdout_json={"messages": True})
    tix = tix_ws.load(ADDON, runner=runner)
    pctx = tix.ctx.provider_context()
    from orch_tix import notify
    notify.push_message_setting(tix.obj, pctx)                       # default off: the server already starts off
    assert runner.calls == []
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "notify_unticketed": True})
    notify.push_message_setting(tix.obj, pctx)
    notify.push_message_setting(tix.obj, pctx)                       # once, not every cycle
    assert len(runner.calls) == 1 and runner.calls[0][2:5] == ("notify", "--messages", "on")
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "notify_unticketed": False})
    notify.push_message_setting(tix.obj, pctx)                       # turned off again: sent
    assert len(runner.calls) == 2 and runner.calls[1][2:5] == ("notify", "--messages", "off")
