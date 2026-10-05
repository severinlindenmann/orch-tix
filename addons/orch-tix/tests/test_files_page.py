from datetime import datetime, timezone
from pathlib import Path

import pytest
from helpers import ADDON, SHARING, CapturingRunner, ok
from orch.addons.api import FileResult, Reveal, Upload
from orch.addons.runtime import SlotView
from orch.addons.widgets import Action, Card, Chips, Link, Search, Table, Text
from orch.core.events import Actor, read_events
from orch.core.ops import Ops
from orch.errors import OrchError
from orch.testing import make_snapshot

URL = "https://tix.example.invalid/p/tok#key-shown-once"
AGENT = Actor("agent", "claude-code", "cli", "s1")
FILE7 = {"id": "FILE7", "label": "report.pdf", "role": "neu", "text": "1.2 MB", "from": "iPhone", "project": "acme",
         "tags": ["weekly-report"], "created_at": "2026-10-02T09:00:00+00:00", "expires": "in 6 days",
         "transcript": False}
FILE8 = {**FILE7, "id": "FILE8", "label": "notes.txt", "from": "ingest-vm", "project": "other", "tags": ["notes"]}


def _walk(widgets):
    for w in widgets:
        yield w
        yield from _walk(getattr(w, "body", ()) or ())
        yield from _walk(getattr(w, "items", ()) or ())


def _fresh(tix_ws):
    runner = CapturingRunner(strict=True)
    return tix_ws.load(ADDON, runner=runner), runner


def _page(tix_ws, tix, **params):
    return tix.obj.widgets("page.orch-tix", SlotView(tix_ws.ws, tix, "page.orch-tix", None, params))


def _cell_text(cell):
    return cell.text if isinstance(cell, (Text, Link)) else cell


def _get_handler(name="report.pdf", data=b"%PDF-1.7"):
    def get(argv, timeout):
        out = Path(argv[argv.index("-o") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / name).write_bytes(data)
        return ok(argv, {"id": argv[2], "name": name, "mime": "application/pdf", "path": str(out / name)})
    return get


def test_page_lists_files_from_the_snapshot(tix, tix_ws):
    tix_ws.cache("orch-tix", make_snapshot("files", "all", items=(FILE7,)))
    widgets = _page(tix_ws, tix)
    table = next(w for w in _walk(widgets) if isinstance(w, Table))
    assert table.columns == ("File", "From", "Tags", "Expires", "")
    assert _cell_text(table.rows[0][0]) == "report.pdf"
    assert table.rows[0][4] == Action("download", "Download", "FILE7")
    assert any(isinstance(w, Action) and w.action == "upload" for w in _walk(widgets))
    assert any(isinstance(w, Action) and w.action == "upload_link" for w in _walk(widgets))
    for w in widgets:
        from orch.addons.widgets import widget_problems
        assert widget_problems(w, slot="page.orch-tix", manifest=tix.manifest) == []


def test_search_and_tag_filter_the_table(tix, tix_ws):
    tix_ws.cache("orch-tix", make_snapshot("files", "all", items=(FILE7, FILE8)))
    rows = lambda **p: [_cell_text(r[0]) for w in _walk(_page(tix_ws, tix, **p)) if isinstance(w, Table) for r in w.rows]
    assert rows() == ["report.pdf", "notes.txt"]
    assert rows(q="ingest") == ["notes.txt"]
    assert rows(tag="weekly-report") == ["report.pdf"]
    search = next(w for w in _walk(_page(tix_ws, tix, q="ingest")) if isinstance(w, Search))
    assert (search.name, search.value) == ("q", "ingest")
    tags = [w for w in _walk(_page(tix_ws, tix, tag="notes")) if isinstance(w, Link) and "tag=" in w.url]
    assert {t.text for t in tags} >= {"weekly-report", "notes"} and [t.text for t in tags if t.current] == ["notes"]


def test_a_selected_file_offers_link_and_attach_to_synced_tickets(tix, tix_ws):
    tix_ws.cache("orch-tix", make_snapshot("files", "all", items=(FILE7,)))
    ops = Ops(tix_ws.ws, AGENT)
    keys = [ops.new(f"T{i}").id for i in range(7)]
    for k in keys:
        tix.obj.state.link(k, by="you", auto=False)
    widgets = _page(tix_ws, tix, f="FILE7")
    actions = [w for w in _walk(widgets) if isinstance(w, Action)]
    assert Action("public_link", "Create link", "FILE7") in actions
    attach = [a for a in actions if a.action == "save_to_ticket"]
    assert len(attach) == 5 and attach[0].target == f"FILE7|{keys[0]}"
    assert any(isinstance(w, Text) and w.text == "Open the ticket page to attach to another" for w in _walk(widgets))


def test_messages_tab_lists_messages_with_mark_as_done(tix, tix_ws):
    tix_ws.cache("orch-tix", make_snapshot("messages", "default", items=(
        {"id": "msg_" + "e" * 32, "label": "ingest-vm", "role": "info", "text": "run <b>finished</b>",
         "ticket": "TIX-42", "local": "DEMO-0001", "files": ["FILE88"]},)))
    widgets = _page(tix_ws, tix, tab="messages")
    table = next(w for w in _walk(widgets) if isinstance(w, Table))
    assert table.columns == ("From", "Message", "Ticket", "")
    row = table.rows[0]
    assert _cell_text(row[0]) == "ingest-vm" and "run <b>finished</b>" in _cell_text(row[1])
    assert row[3] == Action("ack_message", "Mark as done", "msg_" + "e" * 32)
    tabs = next(w for w in _walk(widgets) if isinstance(w, Chips))
    assert [l.text for l in tabs.items if l.current] == ["Messages 1"]


def _offered(tix_ws, *items):
    tix_ws.cache("orch-tix", make_snapshot("files", "all", items=items or (FILE7,)))


def test_download_returns_a_file_result_in_state(tix_ws):
    tix, runner = _fresh(tix_ws)
    _offered(tix_ws)
    runner.handlers["get"] = _get_handler()
    result = tix.obj.act("download", "FILE7", tix.ctx.provider_context())
    assert isinstance(result, FileResult) and result.name == "report.pdf" and result.mime == "application/pdf"
    assert Path(result.path).resolve().is_relative_to(tix.ctx.state_dir.resolve())
    assert Path(result.path).read_bytes() == b"%PDF-1.7"
    assert runner.calls[-1][:3] == (SHARING, "get", "FILE7") and "--no-ack" in runner.calls[-1]
    assert "--force" in runner.calls[-1]


def test_download_refuses_a_bad_ref(tix_ws):
    tix, runner = _fresh(tix_ws)
    with pytest.raises(OrchError):
        tix.obj.act("download", "../etc/passwd", tix.ctx.provider_context())
    assert runner.calls == []


def test_public_link_is_a_reveal_and_never_logged(tix_ws):
    tix, runner = _fresh(tix_ws)
    runner.add([SHARING, "link", "FILE7", "--ttl", "7d", "--json"], stdout_json={"id": "lnk_1", "url": URL})
    result = tix.obj.act("public_link", "FILE7", tix.ctx.provider_context())
    assert result == Reveal("Public link (shown once)", URL)
    assert URL not in repr(result)
    assert all(URL not in e for e in tix.obj.errors)


def test_upload_link_is_a_reveal(tix_ws):
    tix, runner = _fresh(tix_ws)
    runner.add([SHARING, "upload-link", "create", "--json"], stdout_json={"id": "upl_1", "url": URL})
    assert tix.obj.act("upload_link", "", tix.ctx.provider_context()) == Reveal("Upload link (shown once)", URL)


def test_upload_shares_the_temp_file_under_its_name(tix_ws, tmp_path):
    tix, runner = _fresh(tix_ws)
    src = tmp_path / "in-abc"
    src.write_bytes(b"abc")
    runner.add([SHARING, "share", str(src), "--name", "notes.txt", "--ttl", "7d", "--json"], stdout_json={"id": "FILE93"})
    out = tix.obj.act("upload", "", tix.ctx.provider_context(), upload=Upload(src, "notes.txt", 3, "text/plain"))
    assert out == "Shared as FILE93"


def test_save_to_ticket_adds_a_remote_artifact(tix_ws):
    tix, runner = _fresh(tix_ws)
    key = Ops(tix_ws.ws, AGENT).new("Report").id
    _offered(tix_ws)
    tix.obj.state.link(key, by="you", auto=False)
    runner.handlers["get"] = _get_handler()
    msg = tix.obj.act("save_to_ticket", f"FILE7|{key}", tix.ctx.provider_context())
    assert msg == f"Attached FILE7 to {key} as remote-report.pdf"
    assert (tix_ws.ws.artifacts_dir / key / "remote-report.pdf").read_bytes() == b"%PDF-1.7"
    assert read_events(tix_ws.ws, key)[-1].actor == "agent:addon:orch-tix"
    assert not [p for p in (tix.ctx.state_dir / "dl").rglob("*") if p.is_file()]   # the temp copy is gone


def test_ack_message(tix_ws):
    tix, runner = _fresh(tix_ws)
    mid = "msg_" + "e" * 32
    runner.add([SHARING, "msg", "ack", mid, "--json"], stdout_json={"id": mid, "acked": True})
    assert tix.obj.act("ack_message", mid, tix.ctx.provider_context()) == "Done"
    with pytest.raises(OrchError):
        tix.obj.act("ack_message", "msg_nope", tix.ctx.provider_context())


def test_files_provider_items(tix_ws, tix):
    snap = next(p for p in tix.obj.providers if p.id == "files").fetch(tix.ctx.provider_context(), "all", None)
    assert snap.health == "ok"
    (item,) = snap.items
    assert item["id"] == "FILE7" and item["label"] == "report.pdf" and item["role"] == "neu"
    assert item["text"] == "1.2 MB" and item["from"] == "iPhone" and item["tags"] == ["weekly-report"]
    assert item["transcript"] is False and item["expires"].startswith("in ") or item["expires"] == "expired"


def test_always_attaches_message_files_to_the_linked_ticket_once(tix_ws):
    key = Ops(tix_ws.ws, AGENT).new("Nightly").id
    msg = {"id": "msg_" + "e" * 32, "seq": 5, "from": "ingest-vm", "to": "project:acme", "kind": "text",
           "text": "nightly run finished", "files": ["FILE88"], "ticket": "TIX-42",
           "created_at": "2026-10-02T09:41:07Z", "error": None}
    runner = CapturingRunner(strict=True).add([SHARING, "msg", "list", "--json"],
                                              stdout_json={"messages": [msg], "cursor": 5})
    runner.handlers["get"] = _get_handler("run.log", b"ok")
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "sync_artifacts": "always"})
    tix = tix_ws.load(ADDON, runner=runner)
    tix.obj.state.link(key, by="you", auto=False)
    tix.obj.state.commit_rev(key, 1, "TIX-42", gen=1)
    p = next(p for p in tix.obj.providers if p.id == "messages")
    p.fetch(tix.ctx.provider_context(), "default", None)
    p.fetch(tix.ctx.provider_context(), "default", None)
    assert (tix_ws.ws.artifacts_dir / key / "remote-run.log").read_bytes() == b"ok"
    assert len([c for c in runner.calls if c[1] == "get"]) == 1


def test_message_files_are_not_attached_on_request_mode(tix_ws):
    key = Ops(tix_ws.ws, AGENT).new("Nightly").id
    msg = {"id": "msg_" + "f" * 32, "from": "vm", "text": "x", "files": ["FILE88"], "ticket": "TIX-42", "error": None}
    runner = CapturingRunner(strict=True).add([SHARING, "msg", "list", "--json"], stdout_json={"messages": [msg]})
    tix = tix_ws.load(ADDON, runner=runner)
    tix.obj.state.link(key, by="you", auto=False)
    tix.obj.state.commit_rev(key, 1, "TIX-42", gen=1)
    next(p for p in tix.obj.providers if p.id == "messages").fetch(tix.ctx.provider_context(), "default", None)
    assert not [c for c in runner.calls if c[1] == "get"]


def test_selected_file_shows_the_share_time_readably(tix, tix_ws):
    tix_ws.cache("orch-tix", make_snapshot("files", "all", items=(FILE7,)))
    from orch.addons.widgets import KV
    kv = dict(next(w for w in _walk(_page(tix_ws, tix, f="FILE7")) if isinstance(w, KV)).rows)
    assert kv["Shared"] == "2026-10-02 09:00 UTC" and kv["From"] == "iPhone"


# -- Task 11 fixes -------------------------------------------------------------------------------------------

def test_download_refuses_a_file_the_page_does_not_offer(tix_ws):
    tix, runner = _fresh(tix_ws)
    _offered(tix_ws)                                       # FILE7 only
    with pytest.raises(OrchError, match="not on the Shared files page"):
        tix.obj.act("download", "FILE8", tix.ctx.provider_context())
    assert runner.calls == []


def test_save_to_ticket_refuses_targets_the_page_does_not_offer(tix_ws):
    tix, runner = _fresh(tix_ws)
    ops = Ops(tix_ws.ws, AGENT)
    synced, other = ops.new("Synced").id, ops.new("Not synced").id
    tix.obj.state.link(synced, by="you", auto=False)
    _offered(tix_ws)
    pctx = tix.ctx.provider_context()
    with pytest.raises(OrchError, match="not on the Shared files page"):
        tix.obj.act("save_to_ticket", f"FILE8|{synced}", pctx)
    with pytest.raises(OrchError, match="not synced to TIX"):
        tix.obj.act("save_to_ticket", f"FILE7|{other}", pctx)
    tix.obj.state.unlink(synced)
    with pytest.raises(OrchError, match="not synced to TIX"):
        tix.obj.act("save_to_ticket", f"FILE7|{synced}", pctx)
    assert runner.calls == []


def test_a_failed_get_leaves_no_folder_behind(tix_ws):
    tix, runner = _fresh(tix_ws)
    _offered(tix_ws)

    def get_fails_after_writing(argv, timeout):
        out = Path(argv[argv.index("-o") + 1])
        (out / "report.pdf.part").write_bytes(b"half a plaintext")
        return ok(argv, {}).__class__(tuple(argv), 4, '{"error": "network", "detail": "cannot reach"}', "")

    runner.handlers["get"] = get_fails_after_writing
    with pytest.raises(OrchError):
        tix.obj.act("download", "FILE7", tix.ctx.provider_context())
    assert not [p for p in (tix.ctx.state_dir / "dl").rglob("*")]


def test_the_files_provider_sweeps_stale_download_folders(tix_ws, tix):
    import os
    import time
    stale = tix.ctx.state_dir / "dl" / "0123456789abcdef"
    stale.mkdir(parents=True)
    (stale / "report.pdf").write_bytes(b"plaintext left by a crash")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    fresh = tix.ctx.state_dir / "dl" / "fedcba9876543210"
    fresh.mkdir()
    next(p for p in tix.obj.providers if p.id == "files").fetch(tix.ctx.provider_context(), "all", None)
    assert not stale.exists() and fresh.exists()


def test_auto_attach_takes_at_most_3_files_per_tick(tix_ws):
    key = Ops(tix_ws.ws, AGENT).new("Nightly").id
    msg = {"id": "msg_" + "e" * 32, "from": "vm", "text": "logs", "ticket": "TIX-42", "error": None,
           "files": [f"FILE{n}" for n in range(81, 86)]}
    runner = CapturingRunner(strict=True).add([SHARING, "msg", "list", "--json"], stdout_json={"messages": [msg]})
    runner.handlers["get"] = lambda argv, timeout: _get_handler(f"{argv[2]}.log", b"x")(argv, timeout)
    tix_ws.enable("orch-tix", {"sharing_path": SHARING, "sync_artifacts": "always"})
    tix = tix_ws.load(ADDON, runner=runner)
    tix.obj.state.link(key, by="you", auto=False)
    tix.obj.state.commit_rev(key, 1, "TIX-42", gen=1)
    p = next(p for p in tix.obj.providers if p.id == "messages")
    p.fetch(tix.ctx.provider_context(), "default", None)
    assert len([c for c in runner.calls if c[1] == "get"]) == 3
    p.fetch(tix.ctx.provider_context(), "default", None)
    assert len([c for c in runner.calls if c[1] == "get"]) == 5
    assert len(list((tix_ws.ws.artifacts_dir / key).glob("remote-*"))) == 5


def test_only_done_files_say_all_caught_up_and_the_done_chip_lists_them(tix, tix_ws):
    """QA TF-04: "No files on the share yet" is wrong when the share holds done files."""
    done = {**FILE7, "done": True}
    tix_ws.cache("orch-tix", make_snapshot("files", "all", items=(done, {**FILE8, "done": True})))
    table = next(w for w in _walk(_page(tix_ws, tix)) if isinstance(w, Table))
    assert table.rows == () and "All caught up: 2 done files hidden" in table.empty
    chip = next(w for w in _walk(_page(tix_ws, tix)) if isinstance(w, Link) and w.text == "Done 2")
    assert chip.url.endswith("done=1") and not chip.current
    shown = next(w for w in _walk(_page(tix_ws, tix, done="1")) if isinstance(w, Table))
    assert [_cell_text(r[0]) for r in shown.rows] == ["report.pdf", "notes.txt"]
    assert shown.rows[0][3] == "done · in 6 days"


def test_open_files_stay_listed_and_done_ones_wait(tix, tix_ws):
    tix_ws.cache("orch-tix", make_snapshot("files", "all", items=(FILE7, {**FILE8, "done": True})))
    table = next(w for w in _walk(_page(tix_ws, tix)) if isinstance(w, Table))
    assert [_cell_text(r[0]) for r in table.rows] == ["report.pdf"]


def test_a_truly_empty_share_keeps_the_upload_hint(tix, tix_ws):
    tix_ws.cache("orch-tix", make_snapshot("files", "all", items=()))
    table = next(w for w in _walk(_page(tix_ws, tix)) if isinstance(w, Table))
    assert "No files on the share yet" in table.empty
