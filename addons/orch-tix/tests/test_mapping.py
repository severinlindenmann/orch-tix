from orch.core.schema import example_document
from orch_tix.mapping import needs_of, open_questions, payload, redact

DOC = example_document()
SECRETS = ("Inventory\n2. Exporter", "One file per day", "Opens in Excel", "7f3c9a21")


def test_needs_of_maps_orch_kinds():
    assert needs_of({"needs": [{"kind": "answer"}]}) == "question"
    assert needs_of({"needs": [{"kind": "approve-plan"}]}) == "approval"
    assert needs_of({"needs": [{"kind": "approve-requirements"}]}) == "approval"
    assert needs_of({"needs": [{"kind": "re-approve"}]}) == "approval"
    assert needs_of({"needs": [{"kind": "verdict", "round": 3}]}) == "verdict"
    assert needs_of({"needs": [{"kind": "task"}, {"kind": "broken"}]}) is None
    assert needs_of({"needs": [{"kind": "task"}, {"kind": "answer"}]}) == "question"


def test_title_redaction_keeps_only_the_listed_fields():
    d = redact(DOC, "title", sync_log=False, context_artifacts=[])
    text = repr(d)
    assert d["title"] == DOC["title"] and d["questions"][0]["text"] == "Which timestamp format?"
    assert d["questions"][0]["options"][0]["label"] == "ISO 8601" and d["questions"][0]["hash"].startswith("sha256:")
    assert d["gates"]["plan"] == {"state": "approved", "hash": DOC["gates"]["plan"]["hash"], "covers": ["Plan"]}
    assert d["tasks"] == {"progress": {"done": 1, "total": 3}}
    assert d["claim"] == {"harness": "claude-code"} and d["redaction"] == "title"
    assert "sections" not in d and "branches" not in d and "prs" not in d
    for s in SECRETS:
        assert s not in text


def test_title_carries_the_first_verification_line_only():
    doc = {**DOC, "sections": {**DOC["sections"], "Verification": "\n\n  All 14 jobs export.  \nDetails: secret path"}}
    d = redact(doc, "title", sync_log=False, context_artifacts=[])
    assert d["verification_summary"] == "All 14 jobs export."
    assert "secret path" not in repr(d)


def test_title_summary_skips_fenced_blocks():
    text = '```orch\n{"type": "checks", "title": "secret"}\n```\n\n````md\n```\ninner\n````\nAll green on CI.\nmore'
    doc = {**DOC, "sections": {**DOC["sections"], "Verification": text}}
    d = redact(doc, "title", sync_log=False, context_artifacts=[])
    assert d["verification_summary"] == "All green on CI."
    only_fence = {**DOC, "sections": {**DOC["sections"], "Verification": "```orch\n{}\n```\n"}}
    assert "verification_summary" not in redact(only_fence, "title", sync_log=False, context_artifacts=[])


def test_title_drops_unknown_keys():
    doc = {**DOC, "x-new-field": "leak me"}
    assert "leak me" not in repr(redact(doc, "title", sync_log=False, context_artifacts=[]))
    assert "leak me" not in repr(redact(doc, "key-only", sync_log=False, context_artifacts=[]))


def test_key_only_has_no_text():
    d = redact(DOC, "key-only", sync_log=False, context_artifacts=[{"name": "plot.png", "file": "FILE91"}])
    assert set(d) <= {"schema_version", "id", "status", "needs", "created", "updated", "open_questions", "gates",
                      "redaction", "move"}
    assert set(d.get("move", {})) <= {"who", "kind"}                  # whose move and which kind, no label
    assert d["gates"]["plan"] == {"state": "approved"} and d["open_questions"] == 1
    assert "Export the meter" not in repr(d) and "timestamp" not in repr(d) and "plot.png" not in repr(d)


def test_full_drops_the_log_unless_asked():
    doc = {**DOC, "sections": {**DOC["sections"], "Log": "\n".join(f"- line {i}" for i in range(30))}}
    assert "Log" not in redact(doc, "full", sync_log=False, context_artifacts=[])["sections"]
    log = redact(doc, "full", sync_log=True, context_artifacts=[])["sections"]["Log"]
    assert log.count("\n") == 19 and "line 29" in log and "line 9\n" not in log
    full = redact(doc, "full", sync_log=False, context_artifacts=[])
    assert full["sections"]["Plan"] == DOC["sections"]["Plan"] and full["redaction"] == "full"


def test_payload_file_cleartext_fields():
    p = payload(DOC, key="DEMO-0038", gen=1, rev=7, level="title", sync_log=False, context_artifacts=[])
    assert {k for k in p if k != "doc"} == {"key", "gen", "rev", "status", "priority", "needs", "open_questions",
                                             "schema_version"}
    assert p["needs"] == "question" and p["open_questions"] == open_questions(DOC) == 1
    assert p["priority"] == "high" and p["status"] == "waiting" and p["gen"] == 1 and p["rev"] == 7
    no_prio = payload({**DOC, "priority": None}, key="DEMO-0038", gen=1, rev=1, level="key-only", sync_log=False,
                      context_artifacts=[])
    assert no_prio["priority"] == "normal" and "title" not in no_prio["doc"]


def test_context_artifacts_ride_along_at_title():
    d = redact(DOC, "title", sync_log=False, context_artifacts=[{"name": "plot.png", "file": "FILE91"}])
    assert d["context_artifacts"] == [{"name": "plot.png", "file": "FILE91"}]


def test_needs_carry_no_detail_text_below_full():
    doc = {**DOC, "needs": [{"kind": "answer", "detail": "Q1, Q3"}, {"kind": "verdict", "detail": "free text", "round": 7},
                            {"kind": "re-approve", "detail": "plan"}]}
    for level in ("title", "key-only"):
        assert redact(doc, level, sync_log=False, context_artifacts=[])["needs"] == [
            {"kind": "answer", "qids": ["Q1", "Q3"]}, {"kind": "verdict", "round": 7}, {"kind": "re-approve"}]
    assert redact(doc, "full", sync_log=False, context_artifacts=[])["needs"] == doc["needs"]


def test_answers_and_notes_stay_on_the_desktop_below_full():
    """Final review M7: an answer (and its note) can carry client detail; the phone needs only that it is done."""
    q = {**DOC["questions"][0], "answer": "B", "note": "billing wants local time", "answered": "2026-10-02T09:30Z",
         "via": "dashboard"}
    doc = {**DOC, "questions": [q]}
    t = redact(doc, "title", sync_log=False, context_artifacts=[])["questions"][0]
    assert "answer" not in t and "note" not in t and t["answered"] == "2026-10-02T09:30Z"
    assert "billing" not in repr(redact(doc, "key-only", sync_log=False, context_artifacts=[]))
    assert redact(doc, "full", sync_log=False, context_artifacts=[])["questions"][0]["note"] == "billing wants local time"


# ---- night build part 2: what the requirements hash covers reaches the phone; history from events

def test_title_keeps_what_each_gate_hash_covers():
    doc = {**DOC, "gates": {"requirements": {"state": "pending", "hash": "sha256:" + "a" * 64,
                                             "covers": ["Summary", "Requirements", "size", "type"], "via": "x"}}}
    d = redact(doc, "title", sync_log=False, context_artifacts=[])
    assert d["gates"]["requirements"] == {"state": "pending", "hash": "sha256:" + "a" * 64,
                                          "covers": ["Summary", "Requirements", "size", "type"]}
    assert "covers" not in redact(doc, "key-only", sync_log=False, context_artifacts=[])["gates"]["requirements"]
    full = redact(doc, "full", sync_log=False, context_artifacts=[])
    assert full["gates"]["requirements"]["covers"] == ["Summary", "Requirements", "size", "type"]


def test_history_is_redacted_per_level():
    h = [{"seq": 3, "at": "2026-10-02T09:00Z", "who": "you", "what": "asked for changes on the plan", "text": "leak me"}]
    assert redact(DOC, "full", sync_log=False, context_artifacts=[], history=h)["history"] == h
    assert redact(DOC, "title", sync_log=False, context_artifacts=[], history=h)["history"] == [
        {"seq": 3, "at": "2026-10-02T09:00Z", "who": "you", "what": "asked for changes on the plan"}]
    assert "history" not in redact(DOC, "key-only", sync_log=False, context_artifacts=[], history=h)
    assert "history" not in redact(DOC, "title", sync_log=False, context_artifacts=[])


def test_title_keeps_the_verdict_hash_and_round_key_only_does_not():
    doc = {**DOC, "status": "testing", "verdict": {"hash": "sha256:" + "c" * 64, "round": 4, "extra": "no"}}
    assert redact(doc, "title", sync_log=False, context_artifacts=[])["verdict"] == {"hash": "sha256:" + "c" * 64, "round": 4}
    assert redact(doc, "full", sync_log=False, context_artifacts=[])["verdict"]["hash"] == "sha256:" + "c" * 64
    assert "verdict" not in redact(doc, "key-only", sync_log=False, context_artifacts=[])


def test_move_and_together_reach_the_phone_per_level():
    doc = {**DOC, "move": {"who": "you", "kind": "approve-requirements", "label": "Approve requirements and plan",
                           "ref": "requirements", "why": "The plan is drafted too."},
           "needs": [{"kind": "approve-requirements", "detail": "secret detail", "together": True}]}
    t = redact(doc, "title", sync_log=False, context_artifacts=[])
    assert t["move"]["label"] == "Approve requirements and plan" and t["needs"] == [{"kind": "approve-requirements",
                                                                                      "together": True}]
    k = redact(doc, "key-only", sync_log=False, context_artifacts=[])
    assert k["move"] == {"who": "you", "kind": "approve-requirements"}
    assert redact(doc, "full", sync_log=False, context_artifacts=[])["move"]["why"] == "The plan is drafted too."


# -- schema 1.7: receipts, who added an artifact, idle tickets ------------------------------------------------------

def _receipt(doc):
    return next(i for i in doc["artifact_items"] if i.get("kind") == "receipt")


def test_title_keeps_receipt_facts_and_step_names_not_commands():
    d = redact(DOC, "title", sync_log=False, context_artifacts=[])
    item = _receipt(d)
    assert item["run"]["exit"] == 1 and item["run"]["check"] == "verify" and item["run"]["dirty"] is False
    assert [(s["name"], s["status"]) for s in item["run"]["steps"]] == [("build", "pass"), ("test", "fail")]
    assert "pytest" not in repr(d) and "npm run build" not in repr(d) and "label" not in item


def test_title_names_who_added_an_artifact_without_the_session():
    item = _receipt(redact(DOC, "title", sync_log=False, context_artifacts=[]))
    assert item["by"] == "agent:claude-code"


def test_a_human_stays_human_and_junk_is_dropped():
    from orch_tix.mapping import _by
    assert _by("human:you") == "human:you"
    assert _by("agent:codex:1234abcd") == "agent:codex"
    assert _by("<img src=x>") is None and _by(None) is None


def test_title_keeps_idle_days_and_key_only_drops_it():
    doc = {**DOC, "revalidate": {"idle_days": 40, "x": "leak"}}
    assert redact(doc, "title", sync_log=False, context_artifacts=[])["revalidate"] == {"idle_days": 40}
    assert "revalidate" not in redact(doc, "key-only", sync_log=False, context_artifacts=[])
    assert redact(DOC, "title", sync_log=False, context_artifacts=[]).get("revalidate") is None


def test_full_passes_receipts_through():
    d = redact(DOC, "full", sync_log=False, context_artifacts=[])
    assert _receipt(d)["run"]["steps"][1]["status"] == "fail"


def test_the_verification_summary_skips_widget_blocks():
    fence = '```orch\n{"type": "gates", "id": "receipt-t2", "items": [{"name": "verify", "status": "pass"}]}\n```'
    doc = {**DOC, "sections": {**DOC["sections"], "Verification": fence + "\n\n- AC1 opens in Excel, checked by hand"}}
    d = redact(doc, "title", sync_log=False, context_artifacts=[])
    assert d["verification_summary"] == "- AC1 opens in Excel, checked by hand"
    only = {**DOC, "sections": {**DOC["sections"], "Verification": fence}}
    assert "verification_summary" not in redact(only, "title", sync_log=False, context_artifacts=[])


def _summary(text):
    doc = {**DOC, "sections": {**DOC["sections"], "Verification": text}}
    return redact(doc, "title", sync_log=False, context_artifacts=[]).get("verification_summary", "")


def test_title_summary_follows_commonmark_fences_and_never_leaks_a_fence_body():
    # a 4-space-indented backtick line is code inside the fence, not its close
    assert _summary('```orch\n{"x": 1}\n    ```\nSECRET fence body\n```\nprose') == "prose"
    assert _summary("```orch\n{}\n    ```\nSECRET fence body") == ""          # unclosed: the body never goes
    assert _summary("```\nSECRET\n``` trailing\nSECRET2\n```\nok") == "ok"      # text after the run: not a close
    assert _summary("~~~\nSECRET\n```\nSECRET2\n~~~\nok") == "ok"             # tildes close on tildes only
    assert _summary("````\nSECRET\n```\nSECRET2\n````\nok") == "ok"           # nested shorter run stays inside
    assert _summary("```\nSECRET\n~~~\nSECRET2\n```\nok") == "ok"
    assert _summary("``` a`b\nStill prose") == "``` a`b"                         # backtick in the info: no fence
    assert _summary("   ```\nSECRET\n   ```\nok") == "ok"                        # up to 3 spaces opens and closes
    assert _summary("    ```\nindented code is prose to this rule") == "```"      # 4 spaces: no fence
    assert _summary("\n\n  All green.  \nmore") == "All green."


def test_title_summary_stops_at_every_line_separator():
    for sep in ("\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\r", "\r\n", "\n"):
        assert _summary(f"prose{sep}SECRET") == "prose", repr(sep)
    assert _summary("prose \u2028SECRET") == "prose"
    out = _summary("prose\r```orch\rSECRET\r```")
    assert out == "prose" and "SECRET" not in out
    assert _summary("```orch\rSECRET\r```\rok") == "ok"
    assert _summary("```orch\r\nSECRET\r\n```\r\nok") == "ok"


def test_title_keeps_which_checkout_a_receipt_ran_in():
    items = [{**i, "run": {**i["run"], "repo": "acme-app"}} if i.get("kind") == "receipt" else i
             for i in DOC["artifact_items"]]
    d = redact({**DOC, "artifact_items": items}, "title", sync_log=False, context_artifacts=[])
    assert _receipt(d)["run"]["repo"] == "acme-app"
