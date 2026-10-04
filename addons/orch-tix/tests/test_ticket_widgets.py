"""Ticket widgets on the phone (orch_tix.ticket_widgets): only at full, core documents inline, agent HTML as a
context FILE shared once, and never a sealed doc over the server's limit."""
import json

import pytest

from helpers import SHARING
from orch.core.events import Actor
from orch.core.ops import Ops

pytest.importorskip("orch.widgets", reason="needs orch-core with ticket widgets")

AGENT = Actor("agent", "claude-code", "cli", "s1")
HUMAN = Actor("human", "you", "tty")
BARS = {"type": "stats", "title": "Bundle size", "source": "size.txt", "items": [{"label": "main.js", "value": "412 kB"}]}
MATRIX = {"widget": "decision-matrix@1", "title": "Storage", "data": {
    "scale": {"min": 0, "max": 10}, "pick": "sqlite",
    "criteria": [{"id": "setup", "label": "Setup effort", "weight": 3}],
    "options": [{"id": "sqlite", "label": "SQLite", "scores": [10]}, {"id": "pg", "label": "Postgres", "scores": [6]}]}}


def fence(block) -> str:
    return "```orch\n" + json.dumps(block) + "\n```"


def pinned(ws, block: dict) -> dict:
    """The template pin `orch widget add` writes: registry.template_digest of the version now."""
    from orch.widgets import registry
    if "widget" not in block:
        return block
    _, current = registry.template_state(ws.home, block["widget"], None)
    return {**block, "sha256": current}


def html_on(ws) -> None:
    """Agent HTML turned on the human's way (a signed ledger entry plus the config), as orch-core's own tests do;
    the ledger is the isolated one of the orch_user_dir fixture."""
    Ops(ws, HUMAN).set_widgets_html(True)


def _ticket(tix_ws, tix, settings: dict, *, html: bool = True) -> str:
    if html:
        html_on(tix_ws.ws)
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, **settings})
    ops = Ops(tix_ws.ws, AGENT)
    tid = ops.new("Export").id
    ops.set_section(tid, "Findings", f"Measured twice.\n\n{fence(BARS)}\n\nThe options:\n\n{fence(pinned(tix_ws.ws, MATRIX))}\n")
    tix.obj.act("link", tid, tix.ctx.provider_context())
    return tid


def _shares(runner):
    return [c for c in runner.calls if c[1] == "share"]


def test_full_sends_text_inline_core_documents_and_shares_agent_html(tix_ws, tix, runner):
    _ticket(tix_ws, tix, {"redaction": "full", "sync_widget_docs": "always"})
    doc = runner.files[-1]["doc"]
    assert doc["widgets_format"] == "orch.widgets.v1"
    bars, matrix = doc["widgets"]
    assert {k: bars[k] for k in ("section", "index", "key", "layer", "name", "title", "source")} == {
        "section": "Findings", "index": 0, "key": "0", "layer": "type", "name": "stats", "title": "Bundle size",
        "source": "size.txt"}
    assert "412" in bars["text"] and bars["doc"].startswith("<!doctype html>") and "file" not in bars
    assert matrix["layer"] == "widget" and matrix["name"] == "decision-matrix@1" and matrix["file"] == "FILE91"
    assert "doc" not in matrix
    (share,) = _shares(runner)
    assert share[share.index("--ttl") + 1] == "7d" and share[share.index("--tag") + 1] == "context"
    assert not list((tix.ctx.state_dir / "out").glob("*"))                # the plaintext document is removed


def test_a_second_push_reuses_the_shared_document(tix_ws, tix, runner):
    tid = _ticket(tix_ws, tix, {"redaction": "full", "sync_widget_docs": "always"})
    tix.obj.act("push_now", tid, tix.ctx.provider_context())
    assert len(_shares(runner)) == 1 and runner.files[-1]["doc"]["widgets"][1]["file"] == "FILE91"


@pytest.mark.parametrize("level", ["title", "key-only"])
def test_below_full_no_widget_leaves_the_desktop(tix_ws, tix, runner, level):
    _ticket(tix_ws, tix, {"redaction": level})
    doc = runner.files[-1]["doc"]
    assert "widgets" not in doc and "widgets_format" not in doc and not _shares(runner)


def test_sync_artifacts_never_sends_agent_html_as_text_only(tix_ws, tix, runner):
    _ticket(tix_ws, tix, {"redaction": "full", "sync_artifacts": "never", "sync_widget_docs": "always"})
    bars, matrix = runner.files[-1]["doc"]["widgets"]
    assert "doc" in bars and "file" not in matrix and "doc" not in matrix and matrix["text"]
    assert not _shares(runner)


def test_a_doc_over_the_server_limit_drops_the_documents_and_keeps_the_text(tix_ws, tix):
    from orch_tix import ticket_widgets
    body = {"doc": {"id": "DEMO-1", "widgets": [{"text": "t", "doc": "x" * 900_000}]}}
    ticket_widgets.fit(tix.obj, "DEMO-1", body)
    assert body["doc"]["widgets"] == [{"text": "t"}] and "too large" in tix.obj.errors[-1]
    small = {"doc": {"widgets": [{"text": "t", "doc": "x" * 1000}]}}
    assert ticket_widgets.fit(tix.obj, "DEMO-1", small)["doc"]["widgets"][0]["doc"]


ONE_OFF = ('<p id="v">…</p><script>var d = orch.data; document.getElementById("v").textContent = '
           '"One-off: " + d.runs + " runs, all green"; orch.text("One-off: " + d.runs + " runs"); orch.ready();</script>')
CHECKS = {"type": "checks", "title": "Verdicts", "source": "pytest -q", "rows": [
    {"ac": "AC1", "verdict": "met", "evidence": "the export opens in Excel 16 and LibreOffice with every column typed",
     "ref": "tests/test_export.py::test_every_job_exports_a_file_excel_opens"},
    {"ac": "AC2", "verdict": "unproven", "evidence": "needs a phone to try"}]}


def test_end_to_end_documents_are_real_frames_with_kit_and_data(tix_ws, tix, runner, tmp_path):
    """Against the integrated orch-core: a template and a one-off html become frame documents (CSP, nonce, kit,
    data, fonts), not the text-only page; a core document is chrome-less. TIX_WIDGET_DOCS=<dir> keeps them for
    tests/browser/test_widgets.py::test_end_to_end_documents_draw_in_the_pwa."""
    import hashlib
    import os
    from pathlib import Path

    from helpers import ok
    shared = {}

    def share(argv, timeout):
        shared[argv[argv.index("--name") + 1]] = Path(argv[2]).read_text(encoding="utf-8")
        return ok(argv, {"id": f"FILE{100 + len(shared)}"})
    runner.handlers["share"] = share
    html_on(tix_ws.ws)
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "redaction": "full", "sync_widget_docs": "always"})
    ops = Ops(tix_ws.ws, AGENT)
    tid = ops.new("Export").id
    folder = tix_ws.ws.artifacts_dir / tid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "runs.html").write_text(ONE_OFF, encoding="utf-8")
    html = {"html": f"artifacts/{tid}/runs.html", "sha256": hashlib.sha256(ONE_OFF.encode()).hexdigest(),
            "title": "Runs", "caption": "Three runs", "data": {"runs": 3}}
    findings = "\n\n".join(fence(pinned(tix_ws.ws, b)) for b in (CHECKS, MATRIX, html))
    ops.set_section(tid, "Findings", findings)
    tix.obj.act("link", tid, tix.ctx.provider_context())
    checks, matrix, one_off = runner.files[-1]["doc"]["widgets"]
    assert checks["layer"] == "type" and "doc" in checks and matrix["file"] and one_off["file"]
    body = checks["doc"][checks["doc"].index("<body"):]
    assert "<figcaption" not in body and "<details" not in body and "w-checks" in body
    assert "@font-face" in checks["doc"]
    docs = {"checks": checks["doc"]}
    for w in (matrix, one_off):
        (name,) = [n for n in shared if n.endswith(f"-widget-{w['key']}.html")]
        doc = shared[name]
        assert '<meta name="orch-frame" content="' in doc and "window.orch" in doc and 'id="orch-data"' in doc
        assert "@font-face" in doc and "widget not drawn" not in doc and 'class="w-doc"' not in doc
        docs[w["layer"]] = doc
    assert '"pick": "sqlite"' in docs["widget"] or '"pick":"sqlite"' in docs["widget"]
    assert '"runs": 3' in docs["html"] or '"runs":3' in docs["html"]
    if os.environ.get("TIX_WIDGET_DOCS"):
        out = Path(os.environ["TIX_WIDGET_DOCS"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "findings.txt").write_text(findings, encoding="utf-8")
        (out / "widgets.json").write_text(json.dumps(runner.files[-1]["doc"]["widgets"]), encoding="utf-8")
        for k, v in docs.items():
            (out / f"{k}.html").write_text(v, encoding="utf-8")


def test_agent_html_stays_home_when_the_owner_opts_out(tix_ws, tix, runner):
    _ticket(tix_ws, tix, {"redaction": "full", "sync_widget_docs": "never"})
    bars, matrix = runner.files[-1]["doc"]["widgets"]
    assert "doc" in bars and "file" not in matrix and "doc" not in matrix and matrix["text"]
    assert not _shares(runner)


def test_defaults_stay_private_title_and_no_widgets_shared(tix_ws, tix, runner):
    _ticket(tix_ws, tix, {})
    doc = runner.files[-1]["doc"]
    assert doc["redaction"] == "title" and "widgets" not in doc and not _shares(runner)


def test_full_without_the_setting_shares_no_agent_html(tix_ws, tix, runner):
    _ticket(tix_ws, tix, {"redaction": "full"})
    bars, matrix = runner.files[-1]["doc"]["widgets"]
    assert "doc" in bars and "file" not in matrix and not _shares(runner)


def test_entries_pin_fence_and_document_bytes(tix_ws, tix, runner):
    import hashlib
    tid = _ticket(tix_ws, tix, {"redaction": "full", "sync_widget_docs": "always"})
    bars, matrix = runner.files[-1]["doc"]["widgets"]
    assert bars["sha256"] == hashlib.sha256(bars["doc"].encode()).hexdigest()
    assert len(matrix["sha256"]) == 64 and len(matrix["raw_sha256"]) == 64 and len(bars["raw_sha256"]) == 64
    tix.obj.act("push_now", tid, tix.ctx.provider_context())
    assert runner.files[-1]["doc"]["widgets"][1]["sha256"] == matrix["sha256"]   # a reused FILE keeps its pin


def _rm(runner):
    from helpers import ok
    gone = []
    runner.handlers["rm"] = lambda argv, timeout: gone.append(argv[2]) or ok(argv, {"id": argv[2], "deleted": True})
    return gone


def test_a_shared_document_is_deleted_when_its_block_changes_goes_or_leaves_the_phone(tix_ws, tix, runner):
    from helpers import ok
    gone = _rm(runner)
    ids = iter(f"FILE{n}" for n in range(200, 210))
    runner.handlers["share"] = lambda argv, timeout: ok(argv, {"id": next(ids)})
    tid = _ticket(tix_ws, tix, {"redaction": "full", "sync_widget_docs": "always"})
    assert runner.files[-1]["doc"]["widgets"][1]["file"] == "FILE200" and not gone
    ops = Ops(tix_ws.ws, AGENT)
    changed = pinned(tix_ws.ws, {**MATRIX, "data": {**MATRIX["data"], "pick": "pg"}})  # another document: shared anew, the old FILE goes
    ops.set_section(tid, "Findings", f"{fence(BARS)}\n\n{fence(changed)}\n")
    tix.obj.act("push_now", tid, tix.ctx.provider_context())
    assert runner.files[-1]["doc"]["widgets"][1]["file"] == "FILE201" and gone == ["FILE200"]
    ops.set_section(tid, "Findings", f"{fence(BARS)}\n")             # the block is gone
    tix.obj.act("push_now", tid, tix.ctx.provider_context())
    assert gone == ["FILE200", "FILE201"]
    assert not tix.obj.state.links()[tid].get("widget_files")


def test_redaction_below_full_deletes_the_shared_documents(tix_ws, tix, runner):
    gone = _rm(runner)
    tid = _ticket(tix_ws, tix, {"redaction": "full", "sync_widget_docs": "always"})
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "redaction": "title", "sync_widget_docs": "always"})
    tix.obj.act("push_now", tid, tix.ctx.provider_context())
    assert "widgets" not in runner.files[-1]["doc"] and gone == ["FILE91"]


def test_the_cache_keys_on_the_document_not_its_nonce():
    from orch_tix.ticket_widgets import doc_sha
    a = '<meta name="orch-frame" content="aaaaaaaa11"><p>x</p>'
    assert doc_sha(a) == doc_sha(a.replace("aaaaaaaa11", "bbbbbbbb22"))
    assert doc_sha(a) != doc_sha(a.replace("<p>x</p>", "<p>y</p>"))


# -- the privacy gate: what leaves the machine at each level and setting ---------------------------------------------

SECRETS = ("412 kB", "main.js", "Measured twice", "sqlite", "SQLite", "decision-matrix", "Bundle size", "Storage")


def _leaks(runner) -> list[str]:
    sent = json.dumps(runner.files[-1], ensure_ascii=False)
    return [s for s in SECRETS if s in sent]


def _files_of(tix, tid) -> dict:
    return tix.obj.state.links()[tid].get("widget_files") or {}


@pytest.mark.parametrize("docs", ["never", "always"])
@pytest.mark.parametrize("level", ["title", "key-only"])
def test_below_full_no_section_widget_document_hash_or_file_leaves(tix_ws, tix, runner, level, docs):
    import hashlib
    tid = _ticket(tix_ws, tix, {"redaction": level, "sync_widget_docs": docs})
    doc = runner.files[-1]["doc"]
    assert doc["redaction"] == level and "widgets" not in doc and "widgets_format" not in doc and "sections" not in doc
    assert _leaks(runner) == []
    assert not _shares(runner) and _files_of(tix, tid) == {}
    for w in (BARS, MATRIX):                       # no digest of a fence's text either
        assert hashlib.sha256(json.dumps(w).encode()).hexdigest() not in json.dumps(runner.files[-1])


def test_full_with_agent_html_off_sends_text_and_core_documents_only(tix_ws, tix, runner):
    tid = _ticket(tix_ws, tix, {"redaction": "full", "sync_widget_docs": "never"})
    bars, matrix = runner.files[-1]["doc"]["widgets"]
    assert set(matrix) == {"section", "index", "key", "layer", "name", "title", "text", "raw_sha256"}
    assert "doc" in bars and "file" not in bars
    assert not _shares(runner) and _files_of(tix, tid) == {}


def test_a_workspace_that_turns_agent_html_off_after_sharing_sends_and_keeps_nothing(tix_ws, tix, runner):
    gone = _rm(runner)
    tid = _ticket(tix_ws, tix, {"redaction": "full", "sync_widget_docs": "always"})
    assert runner.files[-1]["doc"]["widgets"][1]["file"] == "FILE91" and len(_shares(runner)) == 1
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "redaction": "full", "sync_widget_docs": "never"})
    tix.obj.act("push_now", tid, tix.ctx.provider_context())
    matrix = runner.files[-1]["doc"]["widgets"][1]
    assert "file" not in matrix and "sha256" not in matrix and "doc" not in matrix
    assert gone == ["FILE91"] and len(_shares(runner)) == 1       # the shared document is deleted, never re-sent
    assert _files_of(tix, tid) == {}


def test_the_panel_title_only_override_beats_a_full_workspace(tix_ws, tix, runner):
    gone = _rm(runner)
    tid = _ticket(tix_ws, tix, {"redaction": "full", "sync_widget_docs": "always"})
    assert len(_shares(runner)) == 1
    tix.obj.act("redaction", tid, tix.ctx.provider_context())     # "Show only the title on the phone"
    doc = runner.files[-1]["doc"]
    assert doc["redaction"] == "title" and "widgets" not in doc and "sections" not in doc
    assert _leaks(runner) == [] and gone == ["FILE91"] and len(_shares(runner)) == 1


def test_full_without_sharing_when_artifacts_are_never_sent(tix_ws, tix, runner):
    _ticket(tix_ws, tix, {"redaction": "full", "sync_widget_docs": "always", "sync_artifacts": "never"})
    matrix = runner.files[-1]["doc"]["widgets"][1]
    assert "file" not in matrix and "sha256" not in matrix and not _shares(runner)


def test_with_agent_html_off_on_the_desktop_no_note_page_is_ever_shared(tix_ws, tix, runner):
    tid = _ticket(tix_ws, tix, {"redaction": "full", "sync_widget_docs": "always"}, html=False)
    bars, matrix = runner.files[-1]["doc"]["widgets"]
    assert "doc" in bars and set(matrix) == {"section", "index", "key", "layer", "name", "title", "text", "raw_sha256"}
    assert "off" in matrix["text"].lower() or matrix["text"]       # the text alternative still goes
    assert not _shares(runner) and _files_of(tix, tid) == {}
