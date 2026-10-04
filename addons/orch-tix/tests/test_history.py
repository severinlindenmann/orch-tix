"""History entries from orch events (orch_tix/history.py): fixed words in `what`, free text only in `text`."""
from types import SimpleNamespace

from orch_tix.history import KEEP, describe, entry, merge, redact, stored


def ev(kind, data=None, actor="agent:claude-code:7f3c9a21", seq=1, ticket="DEMO-0038"):
    return SimpleNamespace(seq=seq, at="2026-10-02T09:00Z", ticket=ticket, kind=kind, actor=actor, via="cli", data=data or {})


def test_entries_name_who_and_what():
    # the event log is agent-writable: a human event is what the log claims, never "you"
    assert entry(ev("gate.approved", {"gate": "plan"}, actor="human:you")) == {
        "seq": 1, "at": "2026-10-02T09:00Z", "who": "human (desktop log)", "what": "approved the plan"}
    assert entry(ev("task.moved", {"task": "T2", "now": "doing"}))["what"] == "started T2"
    assert entry(ev("question.asked", {"qids": ["Q1", "Q2"]}))["what"] == "asked Q1, Q2"
    assert entry(ev("ticket.moved", {"from": "open", "to": "in-progress"}))["who"] == "claude-code"
    assert entry(ev("ticket.created", ticket=None)) is None


def test_free_text_never_lands_in_what():
    what, text = describe("gate.changes_requested", {"gate": "plan", "message": "use the client's SFTP"})
    assert what == "asked for changes on the plan" and text == "use the client's SFTP"
    what, text = describe("ticket.moved", {"from": "<b>x", "to": "done", "command": "close", "reason": "dup"})
    assert what == "closed the ticket (? → done)" and text == "dup"
    assert describe("ticket.edited", {"section": "Plan; rm -rf /"})[0] == "edited a section"
    assert describe("ticket.edited", {"section": "Acme Energy Secret Merger"})[0] == "edited a section"
    assert describe("ticket.edited", {"section": "Acceptance criteria"})[0] == "edited Acceptance criteria"
    assert describe("log.added", {"text": "secret client detail"}) == ("logged a note", "secret client detail")
    assert describe("question.answered", {"qid": "Q1 (A)"})[0] == "answered a question"


def test_merge_keeps_the_newest_by_seq_and_redact_sends_the_last_twenty():
    old = [{"seq": s, "at": "", "who": "you", "what": "x"} for s in range(1, KEEP + 1)]
    merged = merge(old, [{"seq": KEEP + 1, "at": "", "who": "you", "what": "y"}, old[0]])
    assert len(merged) == KEEP and merged[-1]["seq"] == KEEP + 1 and merged[0]["seq"] == 2
    assert len(redact(merged, "full")) == 20 and redact(merged, "key-only") == []


def test_history_json_keeps_the_text_only_at_full():
    e = [{"seq": 1, "at": "", "who": "claude-code", "what": "logged a note", "text": "client detail"}]
    assert stored(e, "full") == e
    assert stored(e, "title") == [{"seq": 1, "at": "", "who": "claude-code", "what": "logged a note"}]
    assert stored(e, "key-only")[0].get("text") is None


def test_our_own_writes_are_recorded_as_history_without_a_sync():
    from orch_tix.sync import on_event
    box = []
    put = SimpleNamespace(put=box.append)
    on_event(SimpleNamespace(seq=9, at="2026-10-02T10:00Z", ticket="DEMO-0038", kind="question.answered",
                             actor="human:you", via="addon:orch-tix", data={"qid": "Q1"}), put)
    assert box == [{"op": "history", "ticket": "DEMO-0038", "seq": 9, "kind": "question.answered",
                    "ev": {"seq": 9, "at": "2026-10-02T10:00Z", "who": "human (desktop log)", "what": "answered Q1"}}]
    box.clear()
    on_event(SimpleNamespace(seq=10, at="", ticket="DEMO-0038", kind="addon.decision", actor="human:you",
                             via="addon:orch-tix", data={}), put)
    assert box == []
