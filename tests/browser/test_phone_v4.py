"""Phone v4 (orch-core M-report "TIX"): Today's blocking headline and decision cards, the Board (your move, agents with
a 5-segment bar, folded backlog, search), the ticket's journey, "Proof so far" and "Artifacts", and pinned images the
phone shows only after verifying their sha256 against the sealed mirror."""
import base64
import hashlib
import struct
import zlib

import httpx
import pytest
from playwright.sync_api import expect

from .conftest import EXAMPLE_DOC, FULL_DOC

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True)
def _chromium_only(browser_name):
    if browser_name != "chromium":
        pytest.skip("service worker offline emulation is Chromium-only here")


@pytest.fixture
def browser_context_args(browser_context_args):
    return {**browser_context_args, "service_workers": "allow"}


def png(r: int, g: int, b: int) -> bytes:
    """A real 2x2 PNG of one colour."""
    raw = b"".join(b"\x00" + bytes([r, g, b]) * 2 for _ in range(2))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _testing_doc(**extra):
    return {**FULL_DOC, "questions": [], "status": "in-progress", "needs": [],
            "sections": {**FULL_DOC["sections"], "Acceptance criteria": "- [x] Opens in Excel\n- [ ] One file per day"},
            "move": {"who": "agent", "kind": "working", "label": "claude-code is working · T2", "ref": "T2"}, **extra}


def test_today_headline_counts_blocking_decisions_and_puts_the_recommended_option_first(phone_page, mirror_with_question):
    page = phone_page("light")
    expect(page.locator("#needs-title")).to_have_text("1 decision")
    expect(page.locator("#needs-sub")).to_have_text("then the agents run on their own")
    card = page.locator(f'.ncard[data-n="{mirror_with_question.n}"]')
    expect(card.locator(".qa-opt").first).to_contain_text("ISO 8601")          # the recommended one, first
    expect(card.locator(".qa-opt").first.locator(".qa-rec")).to_have_count(1)
    # a non-blocking question does not count
    q = {**EXAMPLE_DOC["questions"][0], "blocking": False}
    mirror_with_question.push({**EXAMPLE_DOC, "questions": [q]}, rev=2, needs="question", open_questions=1)
    page.reload()
    expect(page.locator("#needs-title")).to_have_text("Nothing blocks the agents")
    expect(page.locator("#needs-sub")).to_have_text("1 when you have a moment")


def test_an_applied_decision_shows_a_receipt(phone_page, mirror_with_question):
    page = phone_page("light")
    card = page.locator(f'.ncard[data-n="{mirror_with_question.n}"]')
    card.get_by_role("button", name="1 · ISO 8601").click()
    card.get_by_role("button", name="Send answer").click()
    card.get_by_text("Sent · waiting for the desktop").wait_for()
    d = mirror_with_question.decisions()[0]
    r = httpx.post(f"{mirror_with_question.base}/api/decisions/{d['id']}/ack", json={"ack": "applied"},
                   headers=mirror_with_question.headers, timeout=30)
    assert r.status_code == 204
    page.reload()
    expect(page.locator(".receipt").first).to_contain_text("Answered from this phone · applied")
    expect(page.locator(".receipt").first).to_contain_text("DEMO-0038")


def test_board_your_move_agents_with_a_five_segment_bar_and_search(phone_page, mirror_with_question):
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/?view=tickets")
    you = page.locator(".brow.is-you")
    expect(you).to_have_count(1)
    expect(you).to_contain_text("DEMO-0038")
    doc = _testing_doc(tasks={"summary": {"done": 1, "total": 3}, "doing": "T2", "tasks": []})
    mirror_with_question.push(doc, rev=2, needs=None, open_questions=0, status="in-progress")
    page.reload()
    agent = page.locator(".bagent")
    expect(agent).to_have_count(1)
    expect(agent.locator(".pill")).to_contain_text("T2/3")
    assert agent.locator(".seg5 i").evaluate_all("els => els.map(e => e.className)") == [
        "seg-done", "seg-doing", "seg-todo", "seg-todo", "seg-todo"]
    page.get_by_label("Search tickets").fill("nothing like this")
    expect(page.locator(".bagent")).to_have_count(0)
    page.get_by_label("Search tickets").fill("meter")
    expect(page.locator(".bagent")).to_have_count(1)
    assert page.evaluate("() => document.activeElement.getAttribute('aria-label')") == "Search tickets"


def test_ticket_journey_never_names_who_agreed_without_a_signed_ledger(phone_page, mirror_with_question):
    mirror_with_question.push(_testing_doc(), rev=2, needs=None, open_questions=0, status="in-progress")
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    j = page.locator(".journey")
    expect(j.locator(".journey-words")).to_have_text("Asked ✓ · Agreed ✓ · Doing · Proven · Done")
    assert j.locator(".seg5 i").evaluate_all("els => els.map(e => e.className)") == [
        "seg-done", "seg-done", "seg-doing", "seg-todo", "seg-todo"]
    expect(j.locator(".journey-who")).to_have_text("Agreed: req + plan, approval not signed here")
    assert "you" not in j.inner_text().split()


def test_proof_and_artifacts_show_a_pinned_image_only_when_its_sha256_verifies(phone_page, mirror_with_question):
    good, bad = png(0, 160, 120), png(200, 0, 0)
    f_good = mirror_with_question.sim.upload("proof.png", good)["id"]
    f_bad = mirror_with_question.sim.upload("forged.png", bad)["id"]
    items = [
        {"source": "file", "kind": "screenshot", "label": "every cluster tagged", "name": "proof.png",
         "sha256": hashlib.sha256(good).hexdigest(), "ac": 1},
        {"source": "file", "kind": "screenshot", "label": "dialog", "name": "forged.png",
         "sha256": hashlib.sha256(b"what the desktop pinned").hexdigest()},
        {"source": "link", "kind": "link", "label": "PR #31"},
    ]
    doc = _testing_doc(artifact_items=items, context_artifacts=[{"name": "proof.png", "file": f_good},
                                                                {"name": "forged.png", "file": f_bad}])
    mirror_with_question.push(doc, rev=2, needs=None, open_questions=0, status="in-progress")
    page = phone_page("light")
    requests = []
    page.on("request", lambda r: requests.append(r.url))
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    proof = page.locator(".proof")
    expect(proof.locator(".card-h")).to_have_text("Proof so far · AC 1/2")
    expect(proof.locator('.pin[data-state="verified"] img')).to_have_count(1)
    expect(proof).to_contain_text("Opens in Excel")
    arts = page.locator(".arts")
    expect(arts.locator(".card-h")).to_have_text("Artifacts · 3")
    expect(arts.locator('.pin[data-name="proof.png"][data-state="verified"] img')).to_have_count(1)
    forged = arts.locator('.pin[data-name="forged.png"]')
    expect(forged).to_have_attribute("data-state", "unverified")
    expect(forged.locator("img")).to_have_count(0)                     # never an unverified image
    expect(forged).to_contain_text("dialog")                          # its alt text instead
    expect(arts).to_contain_text("PR #31")
    src = proof.locator("img").get_attribute("src")
    assert src.startswith("blob:")
    assert not any("evil" in u for u in requests)


def test_proof_lists_the_checks_orch_ran_and_artifacts_say_who_added_them(phone_page, mirror_with_question):
    """Schema 1.7: receipts of `orch task done --run` (facts only), `by` on artifacts, the idle note."""
    run_ok = {"exit": 0, "timed_out": False, "commit": "1a2b3c4" + "0" * 33, "dirty": False, "seconds": 42,
              "check": "verify", "steps": [{"name": "build", "status": "pass", "seconds": 30},
                                           {"name": "test", "status": "pass", "seconds": 12}]}
    run_bad = {"exit": 1, "timed_out": False, "commit": None, "dirty": True, "seconds": 3, "check": None,
               "steps": [{"name": "verify", "status": "fail", "seconds": 3}]}
    items = [
        {"source": "file", "kind": "receipt", "label": "T2 verify: passed", "name": "receipt-T2-a.log",
         "task": "T2", "by": "agent:claude-code", "run": run_ok},
        {"source": "file", "kind": "receipt", "label": "T3 verify: failed", "name": "receipt-T3-a.log",
         "task": "T3", "by": "agent:claude-code", "run": run_bad},
        {"source": "link", "kind": "link", "label": "PR #31", "by": "human:you"},
    ]
    doc = _testing_doc(artifact_items=items, revalidate={"idle_days": 40})
    mirror_with_question.push(doc, rev=2, needs=None, open_questions=0, status="in-progress")
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    runs = page.locator(".proof .proof-runs li")
    expect(runs).to_have_count(2)
    expect(runs.nth(0)).to_contain_text("T2 verify · passed · 1a2b3c4 · 42s")
    expect(runs.nth(0)).to_contain_text("build ✓ · test ✓")
    expect(runs.nth(1)).to_contain_text("T3 verify · failed (exit 1) · uncommitted changes · 3s")
    expect(runs.nth(1).locator(".t-err")).to_have_count(1)
    arts = page.locator(".arts")
    expect(arts).to_contain_text("· link · by you")
    expect(arts).to_contain_text("· receipt · by claude-code")
    expect(page.locator(".t-idle")).to_have_text("Untouched for 40 days — check it still holds")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")  # no sideways scroll at 390 px


def test_a_pinned_gate_image_on_the_approval_card(phone_page, mirror_with_question):
    good = png(10, 20, 200)
    fid = mirror_with_question.sim.upload("dialog.png", good)["id"]
    gates = {**FULL_DOC["gates"], "plan": {**FULL_DOC["gates"]["plan"], "state": "pending"}}
    doc = {**FULL_DOC, "questions": [{**q, "answer": "A"} for q in FULL_DOC["questions"]], "gates": gates,
           "sections": {**FULL_DOC["sections"], "Plan": "1. Inventory\n2. Exporter ![dialog](artifact:dialog.png)"},
           "needs": [{"kind": "approve-plan"}], "move": {"who": "you", "kind": "approve-plan", "label": "Approve plan", "ref": "plan"},
           "artifact_items": [{"source": "file", "kind": "screenshot", "label": "export dialog", "name": "dialog.png",
                               "sha256": hashlib.sha256(good).hexdigest()}],
           "context_artifacts": [{"name": "dialog.png", "file": fid}]}
    mirror_with_question.push(doc, rev=2, needs="approval", open_questions=0)
    page = phone_page("light")
    card = page.locator(f'.ncard[data-n="{mirror_with_question.n}"]')
    expect(card.locator('.pin-card[data-state="verified"] img')).to_have_count(1)
    expect(card.get_by_role("link", name="Review and approve")).to_be_visible()
    expect(page.locator("#needs-title")).to_have_text("1 decision")


# ---- review T7: what the phone refuses although the hash would match ----

def _image_ticket(mirror, items, context):
    doc = _testing_doc(artifact_items=items, context_artifacts=context)
    mirror.push(doc, rev=2, needs=None, open_questions=0, status="in-progress")


def _item(name, data, label):
    return {"source": "file", "kind": "screenshot", "label": label, "name": name, "sha256": hashlib.sha256(data).hexdigest()}


def test_a_correct_hash_is_refused_for_a_non_raster_type_or_another_name(phone_page, mirror_with_question):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    good = png(1, 2, 3)
    f_svg = mirror_with_question.sim.upload("logo.png", svg, mime="image/svg+xml")["id"]     # raster name, svg type
    f_other = mirror_with_question.sim.upload("other.png", good)["id"]                        # the bytes, another name
    _image_ticket(mirror_with_question, [_item("logo.png", svg, "logo"), _item("chart.png", good, "chart")],
                  [{"name": "logo.png", "file": f_svg}, {"name": "chart.png", "file": f_other}])
    page = phone_page("light")
    page.on("dialog", lambda d: pytest.fail("a dialog opened"))
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    for name in ("logo.png", "chart.png"):
        pin = page.locator(f'.arts .pin[data-name="{name}"]')
        expect(pin).to_have_attribute("data-state", "unverified")
        expect(pin.locator("img")).to_have_count(0)


@pytest.mark.parametrize("size", [None, "12", 10 * 1024 * 1024, 5])
def test_a_missing_wrong_or_too_large_size_is_refused_before_decrypting(phone_page, mirror_with_question, size):
    good = png(9, 9, 9)
    fid = mirror_with_question.sim.upload("proof.png", good)["id"]
    item = {**_item("proof.png", good, "proof"), "ac": 1}
    _image_ticket(mirror_with_question, [item], [{"name": "proof.png", "file": fid}])
    page = phone_page("light")
    blob_requests = []
    page.on("request", lambda r: blob_requests.append(r.url) if r.url.endswith("/blob") else None)

    def lie(route):
        resp = route.fetch()
        body = resp.json()
        if size is None:
            body.pop("size", None)
        else:
            body["size"] = size
        route.fulfill(response=resp, json=body)
    page.route(f"**/api/files/{fid}", lie)
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    pin = page.locator('.proof .pin[data-name="proof.png"]')
    expect(pin).to_have_attribute("data-state", "unverified")
    expect(pin.locator("img")).to_have_count(0)
    if size != 5:
        assert blob_requests == []                     # refused before a single ciphertext byte was fetched


def test_a_receipt_names_this_phone_only_for_its_own_decisions(phone_page, mirror_with_question):
    import os
    s = mirror_with_question.sim
    u = os.urandom(16).hex()
    body = {"v": 1, "decision_id": "dec_" + u, "space": mirror_with_question.space, "ticket": "DEMO-0038", "kind": "answer",
            "target": {"qid": "Q1", "hash": EXAMPLE_DOC["questions"][0]["hash"]}, "value": "A", "note": "",
            "device": "another browser", "at": "2026-10-04T08:00:00Z"}
    enc = s.s.seal_decision(mirror_with_question.dek, bytes.fromhex(mirror_with_question.uuid), bytes.fromhex(u), body)
    r = s.request("POST", "/api/decisions", json={"uuid": u, "space": mirror_with_question.space, "ticket": mirror_with_question.id,
                                                  "kind": "answer", "key_version": 1, "enc_body": enc})
    assert r.status_code == 201, r.text
    assert httpx.post(f"{mirror_with_question.base}/api/decisions/dec_{u}/ack", json={"ack": "applied"},
                      headers=mirror_with_question.headers, timeout=30).status_code == 204
    page = phone_page("light")
    expect(page.locator(".receipt").first).to_contain_text("Answered from TIX · applied")


SHOTS = __import__("os").environ.get("FS_SHOTS")


@pytest.mark.skipif(not SHOTS, reason="set FS_SHOTS=<dir> to take the screenshots")
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_screenshots_v4_390(phone_page, mirror_with_question, scheme):
    from pathlib import Path
    out = Path(SHOTS)
    out.mkdir(parents=True, exist_ok=True)
    page = phone_page(scheme)
    page.locator(".ncard").first.wait_for()
    page.wait_for_timeout(300)
    page.screenshot(path=out / f"v4-today-{scheme}.png", full_page=True)
    good, dlg = png(0, 160, 120), png(40, 90, 220)
    f1 = mirror_with_question.sim.upload("proof.png", good)["id"]
    f2 = mirror_with_question.sim.upload("dialog.png", dlg)["id"]
    items = [{"source": "file", "kind": "screenshot", "label": "every cluster tagged", "name": "proof.png",
              "sha256": hashlib.sha256(good).hexdigest(), "ac": 1},
             {"source": "file", "kind": "screenshot", "label": "export dialog", "name": "dialog.png",
              "sha256": hashlib.sha256(dlg).hexdigest()},
             {"source": "file", "kind": "report", "label": "repro log", "name": "repro.log", "sha256": "e" * 64},
             {"source": "link", "kind": "link", "label": "PR #31"}]
    doc = _testing_doc(artifact_items=items, tasks={"summary": {"done": 1, "total": 3}, "doing": "T2", "tasks": []},
                       context_artifacts=[{"name": "proof.png", "file": f1}, {"name": "dialog.png", "file": f2}])
    mirror_with_question.push(doc, rev=2, needs=None, open_questions=0, status="in-progress")
    page.goto(f"{mirror_with_question.base}/?view=tickets")
    page.locator(".bagent").first.wait_for()
    page.screenshot(path=out / f"v4-board-{scheme}.png", full_page=True)
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.locator('.proof .pin[data-state="verified"]').wait_for()
    page.locator('.arts .pin[data-name="dialog.png"][data-state="verified"]').wait_for()
    page.screenshot(path=out / f"v4-ticket-{scheme}.png", full_page=True)
