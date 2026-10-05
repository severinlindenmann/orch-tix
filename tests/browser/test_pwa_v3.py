"""Task 9 (TIX on orch-core): the phone's four tabs, the Needs you list, the decision card, the
decision outbox, the join-request approval in Settings, and the retired board routes. A local
server only (live_server); a test device stands in for the desktop's addon."""
import os
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from .conftest import EXAMPLE_DOC, FULL_DOC, make_desktop

pytestmark = pytest.mark.browser
SHOTS = os.environ.get("FS_SHOTS")


@pytest.fixture(autouse=True)
def _chromium_only(browser_name):
    if browser_name != "chromium":
        pytest.skip("service worker offline emulation is Chromium-only here")


@pytest.fixture
def browser_context_args(browser_context_args):
    return {**browser_context_args, "service_workers": "allow"}


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_needs_you_lists_the_mirror_and_answers_it(phone_page, mirror_with_question, scheme):
    page = phone_page(scheme)
    page.get_by_role("link", name="Needs you").click()
    card = page.get_by_role("article").filter(has_text="Agent needs input")
    card.get_by_text("Acme Energy").wait_for()
    card.locator(".ncard-link").click()
    page.wait_for_url("**/t/*")
    page.get_by_role("radio", name="ISO 8601").check()
    page.get_by_role("button", name="Send answer").click()
    page.get_by_text("Sent · waiting for the desktop").wait_for()
    d = mirror_with_question.decisions()[0]
    assert d["kind"] == "answer" and d["ack"] is None
    # what the desktop opens: the v1 body under the ticket DEK, with the hash the phone saw
    s = mirror_with_question.sim.s
    body = s.open_inbox_item(mirror_with_question.sim.mk, {"uuid": d["uuid"], "ticket_uuid": mirror_with_question.uuid,
                                                          "ticket_wrapped_dek": s.b64u(s.seal(mirror_with_question.sim.mk, mirror_with_question.dek,
                                                                                              s.aad_tdek(bytes.fromhex(mirror_with_question.uuid)))),
                                                          "enc_body": d["enc_body"], "space": d["space"]})
    assert body["kind"] == "answer" and body["value"] == "A" and body["ticket"] == "DEMO-0038"
    assert body["target"] == {"qid": "Q1", "hash": EXAMPLE_DOC["questions"][0]["hash"]}
    assert body["decision_id"] == d["id"] and "mac" not in body


def test_targets_are_at_least_48px_and_status_has_text(phone_page, mirror_with_question):
    page = phone_page("light")
    page.get_by_role("article").first.wait_for()
    visible = "els => els.filter(e => e.getClientRects().length).map(e => [e.textContent.trim().slice(0, 30), e.getBoundingClientRect().height])"
    for label, h in page.locator("button, a[role=tab], nav a, .ncard-link").evaluate_all(visible):
        assert h >= 48, label
    assert page.locator(".pill").first.inner_text().strip()
    assert page.locator(".pill").first.locator("svg").count() == 1      # every status: an icon and text
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.locator("#decision").wait_for()
    for label, h in page.locator("button, .dq-opt").evaluate_all(visible):
        assert h >= 48, label


def test_decision_queue_survives_reload_and_sends_once(phone_page, mirror_with_question, context):
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.get_by_role("radio", name="ISO 8601").wait_for()
    context.set_offline(True)
    page.get_by_role("radio", name="ISO 8601").check()
    page.get_by_role("button", name="Send answer").click()
    page.get_by_role("button", name="Send answer").click(force=True)
    page.get_by_text("Queued · sends when you're online").wait_for()
    page.reload()
    context.set_offline(False)
    page.get_by_text("Sent · waiting for the desktop").wait_for()
    assert len(mirror_with_question.decisions()) == 1


def test_answer_deep_link_focuses_the_decision_card(phone_page, mirror_with_question):
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}#answer")
    page.locator("#decision").wait_for()
    page.wait_for_function("document.activeElement && document.activeElement.closest('#decision') !== null")


def test_old_board_routes_redirect(phone_page, mirror_with_question):
    page = phone_page("light")
    base = mirror_with_question.base
    page.goto(base + "/tickets")
    assert page.url == base + "/"
    page.goto(f"{base}/tickets/TIX-{mirror_with_question.n}")
    assert page.url == f"{base}/t/{mirror_with_question.n}"
    page.goto(base + "/devices")
    assert page.url == base + "/settings#devices"


def test_the_four_tabs_and_the_tickets_view(phone_page, mirror_with_question):
    page = phone_page("light")
    nav = page.get_by_role("navigation", name="Main")
    assert [x.strip() for x in nav.locator(".nav-label").all_inner_texts()] == ["Workspaces", "Needs you", "Tickets", "Files", "Settings"]
    expect(nav.locator("#needs-badge")).to_have_text("1")                 # set once the sealed list is open
    nav.get_by_role("link", name="Tickets").click()
    page.wait_for_url("**/?view=tickets")
    assert nav.get_by_role("link", name="Tickets").get_attribute("aria-current") == "page"
    page.get_by_role("heading", name="Tickets").wait_for()
    row = page.locator(".brow").filter(has_text="DEMO-0038")                # v4 board: your move, the move's label
    row.get_by_text("Answer Q1").wait_for()
    nav.get_by_role("link", name="Files").click()
    page.wait_for_url("**/files")
    page.locator("#file-list").wait_for(state="attached")


def test_an_ack_from_the_desktop_shows_its_outcome(phone_page, mirror_with_question):
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.get_by_role("radio", name="ISO 8601").check()
    page.get_by_role("button", name="Send answer").click()
    page.get_by_text("Sent · waiting for the desktop").wait_for()
    d = mirror_with_question.decisions()[0]
    r = httpx.post(f"{mirror_with_question.base}/api/decisions/{d['id']}/ack", json={"ack": "stale"},
                   headers=mirror_with_question.headers, timeout=30)
    assert r.status_code == 204, r.text
    page.locator("#decision").get_by_text("Not applied · the ticket moved on, see the desktop").wait_for()


def test_key_only_says_to_answer_on_the_desktop(phone_page, mirror_with_question):
    # The real key-only doc (the addon's mapping): needs and gate states, no title, questions or text.
    import json
    levels = json.loads((Path(__file__).parents[1] / "vectors" / "addon-docs.json").read_text(encoding="utf-8"))["levels"]
    mirror_with_question.push({**levels["key-only"], "id": "DEMO-0038"}, rev=2)
    page = phone_page("light")
    page.get_by_role("article").get_by_text("Question on DEMO-0038 · open on desktop").wait_for()
    page.get_by_role("article").first.click()
    page.locator("#decision").get_by_text("Answer on the desktop.").wait_for()
    assert page.get_by_role("button", name="Send answer").count() == 0


def test_a_join_request_is_approved_in_settings(phone_page, live_server, sim, mirror_with_question):
    s = sim.s
    other_space, other = make_desktop(live_server, sim, name="mac-mini", label="Orchestrator")
    r = httpx.post(f"{live_server.url}/api/spaces/{mirror_with_question.space}/join", headers=other, timeout=30)
    assert r.status_code == 202, r.text
    page = phone_page("light")
    page.get_by_text("mac-mini wants to sync Acme Energy").wait_for()       # the banner on Needs you
    page.goto(live_server.url + "/settings#join")
    row = page.locator(".join-row").filter(has_text="mac-mini wants to sync Acme Energy")
    row.get_by_role("button", name="Approve").click()
    page.get_by_role("dialog").get_by_role("button", name="Approve").click()
    page.locator("#join-card").wait_for(state="hidden")   # an empty block is not shown
    spaces = {x["id"]: x for x in sim.request("GET", "/api/spaces").json()["spaces"]}
    assert spaces[mirror_with_question.space]["owner_name"] == "mac-mini"
    assert other_space in spaces


def test_a_join_request_can_be_denied(phone_page, live_server, sim, mirror_with_question):
    _, other = make_desktop(live_server, sim, name="ci-runner", label="CI")
    assert httpx.post(f"{live_server.url}/api/spaces/{mirror_with_question.space}/join", headers=other, timeout=30).status_code == 202
    page = phone_page("light")
    page.goto(live_server.url + "/settings#join")
    page.locator(".join-row").filter(has_text="ci-runner").get_by_role("button", name="Deny").click()
    page.locator("#join-card").wait_for(state="hidden")   # an empty block is not shown
    spaces = {x["id"]: x for x in sim.request("GET", "/api/spaces").json()["spaces"]}
    assert spaces[mirror_with_question.space]["owner_name"] == "macbook-pro"


def test_titles_in_notifications_is_off_until_switched_on(phone_page, live_server, mirror_with_question):
    page = phone_page("light")
    page.goto(live_server.url + "/settings")
    toggle = page.locator("#titles-toggle")
    assert not toggle.is_checked()
    page.locator('label[for="titles-toggle"]').click()
    page.wait_for_function("""async () => {
      const db = await new Promise((ok) => { const r = indexedDB.open("fileshare"); r.onsuccess = () => ok(r.result); });
      const v = await new Promise((ok) => { const g = db.transaction("prefs").objectStore("prefs").get("show_titles"); g.onsuccess = () => ok(g.result); });
      db.close();
      return v === true;
    }""")
    page.reload()
    assert page.locator("#titles-toggle").is_checked()


def test_the_label_cache_for_the_service_worker(phone_page, mirror_with_question):
    page = phone_page("light")
    page.get_by_role("article").first.wait_for()
    got = page.evaluate("""async (space) => {
      const db = await new Promise((ok) => { const r = indexedDB.open("fileshare"); r.onsuccess = () => ok(r.result); });
      const v = await new Promise((ok) => { const g = db.transaction("labels").objectStore("labels").get(space); g.onsuccess = () => ok(g.result); });
      db.close();
      return v;
    }""", mirror_with_question.space)
    assert got == {"label": "Acme Energy", "titles": True}


def test_no_request_leaves_this_origin(phone_page, mirror_with_question):
    page = phone_page("light")
    hosts = set()
    page.on("request", lambda r: hosts.add(r.url.split("/")[2]))
    page.reload()
    page.get_by_role("article").first.wait_for()
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.locator("#decision").wait_for()
    assert hosts == {mirror_with_question.base.split("/")[2]}
    fonts = page.evaluate("[...document.fonts].filter(f => f.status === 'loaded').map(f => f.family)")
    assert {"Figtree", "Manrope"} <= {f.strip('"') for f in fonts}


@pytest.mark.skipif(not SHOTS, reason="set FS_SHOTS=<dir> to take the screenshots")
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_screenshots_390(phone_page, live_server, sim, mirror_with_question, scheme):
    out = Path(SHOTS)
    out.mkdir(parents=True, exist_ok=True)
    _, other = make_desktop(live_server, sim, name="mac-mini", label="Orchestrator")
    httpx.post(f"{live_server.url}/api/spaces/{mirror_with_question.space}/join", headers=other, timeout=30)
    page = phone_page(scheme)
    page.get_by_role("article").first.wait_for()
    page.wait_for_timeout(300)
    page.screenshot(path=out / f"needs-you-{scheme}.png", full_page=True)
    page.goto(live_server.url + "/?view=tickets")
    page.locator(".brow, .bagent").first.wait_for()
    page.screenshot(path=out / f"tickets-{scheme}.png", full_page=True)
    page.goto(f"{live_server.url}/t/{mirror_with_question.n}")
    page.get_by_role("radio", name="ISO 8601").check()
    page.screenshot(path=out / f"ticket-answer-{scheme}.png")
    page.get_by_role("button", name="Send answer").click()
    page.get_by_text("Sent · waiting for the desktop").wait_for()
    page.screenshot(path=out / f"ticket-sent-{scheme}.png")
    mirror_with_question.push(_approval_doc(FULL_DOC), rev=2, needs="approval", open_questions=0)
    page.reload()
    page.get_by_role("button", name="Approve plan").wait_for()
    page.screenshot(path=out / f"ticket-approval-{scheme}.png", full_page=True)
    page.get_by_role("button", name="Approve plan").click()
    page.get_by_role("dialog").wait_for()
    page.screenshot(path=out / f"ticket-approve-confirm-{scheme}.png")
    page.keyboard.press("Escape")
    mirror_with_question.push(_verdict_doc("- AC1: 12 tests passed · CI green · 1 manual step", rnd=1),
                              rev=3, needs="verdict", open_questions=0, status="testing")
    page.reload()
    page.get_by_role("button", name="Done").wait_for()
    page.screenshot(path=out / f"ticket-verdict-{scheme}.png")
    page.get_by_role("button", name="Done").click()
    page.get_by_role("dialog").wait_for()
    page.screenshot(path=out / f"ticket-verdict-confirm-{scheme}.png")
    page.keyboard.press("Escape")
    page.goto(live_server.url + "/files")
    page.wait_for_load_state("networkidle")
    page.screenshot(path=out / f"files-{scheme}.png")
    page.goto(live_server.url + "/settings")
    page.locator(".join-row").first.wait_for()
    page.screenshot(path=out / f"settings-{scheme}.png", full_page=True)


def test_a_refused_decision_never_blocks_the_bar_and_can_be_discarded(phone_page, mirror_with_question, context):
    """Task 9 review fix 4: the outbox marks a 4xx decision failed; the page says why and offers Discard."""
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.get_by_role("radio", name="ISO 8601").wait_for()
    context.set_offline(True)
    page.get_by_role("radio", name="ISO 8601").check()
    page.get_by_role("button", name="Send answer").click()
    page.get_by_text("Queued · sends when you're online").wait_for()
    page.route("**/api/decisions", lambda route: route.fulfill(
        status=400, content_type="application/json",
        body='{"error": "bad_ref", "detail": "no live mirrored ticket with that id in this space"}')
        if route.request.method == "POST" else route.continue_())
    context.set_offline(False)
    page.get_by_text("Not sent: no longer on the phone").wait_for()
    assert page.get_by_role("button", name="Send answer").is_enabled()
    page.get_by_role("button", name="Discard").click()
    page.get_by_text("Not sent: no longer on the phone").wait_for(state="detached")
    assert mirror_with_question.decisions() == []


def test_the_ticket_page_reads_the_addon_doc_at_title_and_full(phone_page, mirror_with_question):
    """The doc the orch-tix addon seals (tests/vectors/addon-docs.json): task progress, the verification
    summary and the artifact link at title; the history from orch events (what at title, with the text at full),
    never the Log lines."""
    import json
    from pathlib import Path
    levels = json.loads((Path(__file__).parents[1] / "vectors" / "addon-docs.json").read_text(encoding="utf-8"))["levels"]
    page = phone_page("light")
    url = f"{mirror_with_question.base}/t/{mirror_with_question.n}"
    title = {**levels["title"], "id": EXAMPLE_DOC["id"], "status": "testing", "needs": [{"kind": "verdict", "round": 7}]}
    mirror_with_question.push(title, rev=2, needs="verdict", open_questions=0, status="testing")
    page.goto(url)
    page.locator(".t-head .strip").get_by_text("T 1/3").wait_for()        # the header's progress strip
    page.locator('.chap[data-chapter="3"] summary').click()
    page.get_by_text("Tasks: 1 of 3").wait_for()
    page.get_by_text("All 14 jobs export; Excel opens every file.").wait_for()
    assert page.get_by_role("link", name="report.md").get_attribute("href") == "/files?f=FILE7"
    page.locator("#hist-title").click()                                    # History is a collapsed chapter
    page.get_by_text("From the desktop's event log — not verified.").wait_for()
    assert page.locator(".hist li").count() == 6
    assert " you " not in " " + " ".join(page.locator(".hist li").all_inner_texts()) + " "    # never "you"
    page.locator(".hist li").filter(has_text="human (desktop log) asked for changes on the plan").wait_for()
    assert page.locator(".hist-text").count() == 0                         # title: no free text
    assert "asked Q1" in page.locator(".hist li").first.inner_text()       # newest first
    full = {**levels["full+log"], "id": EXAMPLE_DOC["id"]}
    mirror_with_question.push(full, rev=3, needs="question", open_questions=1)
    page.reload()
    page.locator("#hist-title").click()
    page.locator(".hist li").filter(has_text="split the exporter").wait_for()      # full: the event's text
    assert page.get_by_text("agent: step 24").count() == 0                 # never the Log


def test_an_older_copy_after_a_reload_shows_the_banner_and_no_decision_card(phone_page, mirror_with_question):
    """Task 9 review: the high-water mark lives in IndexedDB ("seen"), so a server that hands back an older
    snapshot is noticed even after a reload, on the ticket page and on Needs you."""
    page = phone_page("light")
    newer = {**EXAMPLE_DOC, "gen": 1, "mirror_rev": 2}
    mirror_with_question.push(newer, rev=2)
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.get_by_role("radio", name="ISO 8601").wait_for()
    mirror_with_question.push({**EXAMPLE_DOC, "gen": 1, "mirror_rev": 1}, rev=3)    # older content, newer server rev
    page.reload()
    page.get_by_text("The server sent an older copy of this ticket than this phone already saw.").wait_for()
    assert page.locator("#decision").count() == 0
    page.goto(mirror_with_question.base + "/")
    page.get_by_text("The server sent an older copy · open it later").wait_for()


# ---- Task 10 review fix round 1 (I2): the ticket page never misses `online` during its first load ----
# From the reviewer's probe (scratchpad rev5/probes/test_zz_flake_diag.py): after the reload the first
# GET of the mirror fails late (offline when it started), so `online` fires while the page is still
# awaiting it. The page must still load and show the queued decision as sent.
FAIL_FIRST_MIRROR_GET_LATE = """
(() => {
  if (!location.pathname.startsWith("/t/") || sessionStorage.getItem("failFirstGet") !== "1") return;
  sessionStorage.removeItem("failFirstGet");
  const f = window.fetch;
  let first = true;
  window.fetch = function (input, init) {
    const url = String((input && input.url) || input);
    if (first && url.includes("/api/mirrors/TIX-")) {
      first = false;
      return new Promise((ok) => setTimeout(ok, 1500)).then(() => { throw new TypeError("Failed to fetch"); });
    }
    return f.call(this, input, init);
  };
})();
"""


def test_a_first_load_that_fails_while_going_online_still_sends_and_shows_it(phone_page, mirror_with_question, context):
    page = phone_page("light")
    page.add_init_script(FAIL_FIRST_MIRROR_GET_LATE)
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.get_by_role("radio", name="ISO 8601").wait_for()
    context.set_offline(True)
    page.get_by_role("radio", name="ISO 8601").check()
    page.get_by_role("button", name="Send answer").click()
    page.get_by_text("Queued · sends when you're online").wait_for()
    page.evaluate("sessionStorage.setItem('failFirstGet', '1')")
    page.reload()
    context.set_offline(False)
    page.get_by_text("Sent · waiting for the desktop").wait_for(timeout=15_000)
    assert len(mirror_with_question.decisions()) == 1


def test_the_outbox_status_shows_even_when_the_ticket_does_not_load(phone_page, mirror_with_question, context):
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.get_by_role("radio", name="ISO 8601").wait_for()
    context.set_offline(True)
    page.get_by_role("radio", name="ISO 8601").check()
    page.get_by_role("button", name="Send answer").click()
    page.get_by_text("Queued · sends when you're online").wait_for()
    page.reload()                                   # offline: the shell opens, the mirror GET fails; the phone has the list (QA T08)
    page.get_by_text("You're offline. This is the copy from", exact=False).wait_for()
    page.get_by_text("Queued · sends when you're online").wait_for()


def test_settings_lists_archived_legacy_tickets_read_only(phone_page, live_server, sim):
    """Task 12's archived_at: the Archive card lists migrated legacy tickets, decrypted here, read-only."""
    s = sim.s
    made = []
    for title, archive in (("Old CSV export ticket", True), ("Still on the old board", False)):
        c = s.new_ticket_crypto(sim.mk, {"title": title, "body": "Body of " + title, "fm": {}})
        r = sim.request("POST", "/api/tickets", json={k: v for k, v in c.items() if not k.startswith("_")} | {
            "event_uuid": os.urandom(16).hex(), "status": "open", "project": "acme"})
        assert r.status_code == 201, r.text
        t = r.json()
        if archive:
            eu = os.urandom(16)
            body = s.seal_ticket_event(c["_dek"], bytes.fromhex(c["uuid"]), eu, {"kind": "update", "text": "Migrated"})
            assert sim.request("POST", f"/api/tickets/{t['id']}/archive", json={"uuid": eu.hex(), "enc_body": body}).status_code == 200
        made.append(t)
    page = phone_page("light")
    page.goto(live_server.url + "/settings#archive")
    row = page.locator(".archive-row")
    row.first.wait_for()
    assert row.count() == 1
    assert "Old CSV export ticket" in row.inner_text() and "readable until" in row.inner_text()
    assert page.get_by_text("Still on the old board").count() == 0
    assert row.locator("button, input, textarea, select, a").count() == 0      # read-only: nothing to act on
    row.locator("summary").click()
    row.get_by_text("Body of Old CSV export ticket").wait_for()


# ---- final review I3: the phone trusts the sealed doc, never the cleartext routing ----

def test_a_relabelled_cleartext_needs_never_changes_the_card(phone_page, mirror_with_question):
    """The server says verdict; the sealed doc says a question is open. The phone shows the question."""
    mirror_with_question.push(EXAMPLE_DOC, rev=2, needs="verdict", open_questions=0, status="testing")
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/")
    card = page.locator(f'.ncard[data-n="{mirror_with_question.n}"]')
    card.wait_for()
    assert "Answer" in card.inner_text() and "Verdict" not in card.inner_text()
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.get_by_role("radio", name="ISO 8601").wait_for()
    assert page.get_by_role("button", name="Done").count() == 0


def test_a_mirror_moved_to_another_space_shows_an_integrity_banner_and_no_actions(phone_page, mirror_with_question):
    import json as _json
    other = "f" * 32

    def moved(route):
        r = route.fetch()
        body = r.json()
        for m in body.get("mirrors", [body]):
            if isinstance(m, dict) and "space" in m:
                m["space"] = other
        route.fulfill(response=r, body=_json.dumps(body))
    page = phone_page("light")
    page.route("**/api/mirrors/TIX-*", moved)
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.get_by_text("doesn't match its sealed content").wait_for()
    heading = page.locator("h1.t-title").inner_text()
    assert "decrypt" not in heading.lower() and heading == "Ticket held back"   # neutral, above the banner
    assert page.locator("#decision").count() == 0
    assert page.locator("#decision-bar button:visible").count() == 0



# ---- final review I2: the gate text is the doc's sections; the phone approves only at full ----

def _approval_doc(base, gate="plan"):
    """A ticket waiting for its plan approval: the question answered, the plan gate pending."""
    gates = {**base["gates"], gate: {**base["gates"][gate], "state": "pending"}}
    return {**base, "questions": [{**q, "answer": "A"} for q in base.get("questions", [])], "gates": gates,
            "needs": [{"kind": f"approve-{gate}"}],
            "move": {"who": "you", "kind": f"approve-{gate}", "label": f"Approve {gate}", "ref": gate}}


def _verdict_doc(verification="- AC1: opened 3 files in Excel", rnd=2, **extra):
    """A full ticket in testing with schema 1.3's verdict {hash, round}: orch.core.epics.verdict_hash of one ticket
    (canonical JSON of [{ac, id, status, verification}])."""
    import hashlib
    import json as _json
    doc = {**FULL_DOC, "questions": [], "status": "testing", "needs": [{"kind": "verdict", "round": rnd}],
           "sections": {**FULL_DOC["sections"], "Verification": verification}, **extra}
    canon = _json.dumps([{"id": doc["id"], "status": doc["status"], "ac": doc["sections"]["Acceptance criteria"],
                          "verification": verification}], ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    doc["verdict"] = {"hash": "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest(), "round": rnd}
    return doc


def test_at_full_the_approval_card_shows_the_plan_and_the_other_sections(phone_page, mirror_with_question):
    mirror_with_question.push(_approval_doc(FULL_DOC), rev=2, needs="approval", open_questions=0)
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    card = page.locator("#decision")
    card.get_by_text("1. Inventory").wait_for()                            # sections.Plan
    assert "shows only titles" not in card.inner_text()
    assert card.locator(".gate-text").count() == 1                         # the Plan, not repeated in a chapter
    assert page.locator(".chap .section-name", has_text="Plan").count() == 0
    asked = page.locator('.chap[data-chapter="1"]')
    asked.locator("summary").click()
    asked.get_by_text("The client needs the readings as CSV.").wait_for()  # sections.Ask, read-only
    assert asked.locator("button, input, textarea, select").count() == 0
    # Approve asks once more in a bottom sheet that names the hash; Cancel sends nothing.
    page.get_by_role("button", name="Approve plan").click()
    sheet = page.get_by_role("dialog", name="Approve the plan?")
    short = FULL_DOC["gates"]["plan"]["hash"][7:11]
    sheet.get_by_text(f"plan · sha256:{short}…").wait_for()
    assert page.evaluate("() => document.activeElement.textContent") == "Cancel"
    sheet.get_by_role("button", name="Cancel").click()
    sheet.wait_for(state="detached")
    assert mirror_with_question.inbox_items() == []
    page.get_by_role("button", name="Approve plan").click()
    page.get_by_role("dialog").get_by_role("button", name="Confirm approval").click()
    page.get_by_text("Sent · waiting for the desktop").wait_for()
    body = mirror_with_question.sim.s.open_inbox_item(mirror_with_question.sim.mk, mirror_with_question.inbox_items()[0])
    assert body["kind"] == "approve" and body["target"] == {"gate": "plan", "hash": FULL_DOC["gates"]["plan"]["hash"]}


def test_at_title_the_plan_stays_on_the_desktop_and_approve_is_not_offered(phone_page, mirror_with_question):
    mirror_with_question.push(_approval_doc(EXAMPLE_DOC), rev=2, needs="approval", open_questions=0)
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.locator("#decision").get_by_text("shows only titles").wait_for()
    assert page.get_by_role("button", name="Approve plan").count() == 0
    page.get_by_role("button", name="Request changes").wait_for()


# ---- final review I7: messages to the human (spec §8) and the ticket request (spec §5.5) ----

def _message_to_human(mf, text, **kw):
    s = mf.sim.s
    u = os.urandom(16)
    body = {"text": text, "files": [], "ticket": None, "from": "macbook-pro", **kw}
    r = httpx.post(f"{mf.base}/api/messages", json={"uuid": u.hex(), "to_kind": "human", "to_id": "", "kind": "text",
                                                   "key_version": 1, "enc_body": s.seal_msg(mf.sim.mk, u, body)},
                   headers=mf.headers, timeout=30)
    assert r.status_code == 201, r.text
    return "msg_" + u.hex()


def test_a_message_to_the_human_shows_as_text_and_ack_clears_it(phone_page, mirror_with_question):
    _message_to_human(mirror_with_question, "Nightly run finished.\n<b>not bold</b> javascript:alert(1)")
    page = phone_page("light")
    card = page.locator(".msg-card")
    card.wait_for()
    assert "From macbook-pro" in card.inner_text()
    text = card.locator(".msg-text")
    assert text.inner_text() == "Nightly run finished.\n<b>not bold</b> javascript:alert(1)"   # data, never markup
    assert text.locator("b, a").count() == 0
    card.get_by_role("button", name="Mark as read").click()
    card.wait_for(state="detached")
    assert mirror_with_question.sim.request("GET", "/api/messages?after=0&wait=0").json()["messages"] == []


def test_a_ticket_request_is_sealed_for_its_space_with_value_title_and_body(phone_page, mirror_with_question):
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/?view=tickets")
    page.get_by_text("New ticket request").click()
    page.get_by_label("Title").fill("Rotate the API key")
    page.get_by_label("Details").fill("before Friday")
    page.get_by_role("button", name="Send request").click()
    page.get_by_text("Sent · waiting for the desktop").wait_for()
    item = mirror_with_question.inbox_items()[0]
    assert item["kind"] == "ticket_request" and item["ticket"] is None
    d = mirror_with_question.sim.s.open_inbox_item(mirror_with_question.sim.mk, item)
    assert d["kind"] == "ticket_request" and d["space"] == mirror_with_question.space
    assert d["value"] == {"title": "Rotate the API key", "body": "before Friday"} and "ticket" not in d


# ---- polish: the nav badge counts what Needs you shows (sealed needs + messages + join requests) ----

def test_the_badge_counts_sealed_needs_and_messages_never_the_cleartext(phone_page, mirror_with_question):
    # The sealed doc has an open question; the cleartext routing says nothing needs the human. With one message
    # to the human the badge is 2 (the old cleartext count would hide it).
    mirror_with_question.push(EXAMPLE_DOC, rev=2, needs=None, open_questions=0)
    _message_to_human(mirror_with_question, "Build is green.")
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/settings")
    page.wait_for_function("() => document.getElementById('needs-badge').textContent === '2'")


# ---- the phone screens on the shared design system: decision cards, confirm sheets, inbox zero ----

def test_a_needs_you_card_answers_its_question_in_place(phone_page, mirror_with_question):
    """One open single-choice question: numbered 48 px options, the recommended one marked; a tap only arms."""
    page = phone_page("light")
    page.on("dialog", lambda d: pytest.fail(f"browser popup: {d.message}"))
    card = page.locator(f'.ncard[data-n="{mirror_with_question.n}"]')
    opt = card.get_by_role("button", name="A · ISO 8601")
    opt.wait_for()
    assert opt.locator(".qa-rec").inner_text().strip() == "recommended"
    assert card.get_by_role("button", name="B · Local time").locator(".qa-rec").count() == 0
    for h in card.locator(".qa-opt").evaluate_all("els => els.map(e => e.getBoundingClientRect().height)"):
        assert h >= 48
    assert not card.get_by_role("button", name="Send answer").is_visible()
    opt.click()
    assert opt.get_attribute("aria-pressed") == "true"
    card.get_by_text("Answer Q1 with A · ISO 8601?").wait_for()
    assert mirror_with_question.decisions() == []                         # the first tap never commits
    card.get_by_role("button", name="Cancel").click()
    assert opt.get_attribute("aria-pressed") == "false"
    opt.click()
    card.get_by_role("button", name="Send answer").click()
    card.get_by_text("Sent · waiting for the desktop").wait_for()
    assert opt.is_disabled()
    d = mirror_with_question.decisions()[0]
    s = mirror_with_question.sim.s
    body = s.open_inbox_item(mirror_with_question.sim.mk, mirror_with_question.inbox_items()[0])
    assert d["kind"] == "answer" and body["value"] == "A"
    assert body["target"] == {"qid": "Q1", "hash": EXAMPLE_DOC["questions"][0]["hash"]}
    # after a reload the card says the answer is on its way instead of offering the options again
    page.reload()
    card.get_by_text("Sent · waiting for the desktop").wait_for()
    assert card.get_by_role("button", name="A · ISO 8601").is_disabled()


def test_an_approval_card_opens_the_read_and_approve_view(phone_page, mirror_with_question):
    mirror_with_question.push(_approval_doc(FULL_DOC), rev=2, needs="approval", open_questions=0)
    page = phone_page("light")
    card = page.locator(f'.ncard[data-n="{mirror_with_question.n}"]')
    card.get_by_text("Approve plan", exact=True).wait_for()               # the chip: the verb of your move
    card.get_by_text("2 steps · claude-code waits").wait_for()
    assert card.locator(".qa-opt").count() == 0
    card.get_by_role("link", name="Review and approve").click()
    page.wait_for_url("**/t/*#answer")
    page.locator("#decision").get_by_text("1. Inventory").wait_for()
    assert page.locator(".t-head").get_by_text("Approve plan", exact=True).is_visible()
    assert page.locator(".t-head .journey-words").inner_text() == "Asked ✓ · Agreed · Doing · Proven · Done"


def test_done_asks_in_a_confirm_sheet_and_escape_sends_nothing(phone_page, mirror_with_question):
    doc = _verdict_doc()
    mirror_with_question.push(doc, rev=2, needs="verdict", open_questions=0, status="testing")
    page = phone_page("light")
    page.on("dialog", lambda d: pytest.fail(f"browser popup: {d.message}"))
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    card = page.locator("#decision")
    card.get_by_text("- AC1: opened 3 files in Excel").wait_for()
    assert [t.strip() for t in card.locator(".gate-name").all_inner_texts()] == [
        "Acceptance criteria · full text", "Verification · full text"]           # what the verdict hash covers
    card.get_by_text("- AC1: opened 3 files in Excel").wait_for()
    page.get_by_role("button", name="Done").click()
    sheet = page.get_by_role("dialog", name="Accept DEMO-0038 as done?")
    sheet.wait_for()
    page.keyboard.press("Escape")
    sheet.wait_for(state="detached")
    assert mirror_with_question.decisions() == []
    page.get_by_role("button", name="Done").click()
    page.get_by_role("dialog").get_by_role("button", name="Confirm done").click()
    page.get_by_text("Sent · waiting for the desktop").wait_for()
    body = mirror_with_question.sim.s.open_inbox_item(mirror_with_question.sim.mk, mirror_with_question.inbox_items()[0])
    assert body["kind"] == "verdict" and body["value"] == "done"
    assert body["target"] == {"status": "testing", "round": 2, "hash": doc["verdict"]["hash"]}


def test_inbox_zero_is_a_calm_card_with_an_honest_line(phone_page, mirror_with_question):
    answered = [{**q, "answer": "A"} for q in EXAMPLE_DOC["questions"]]
    mirror_with_question.push({**EXAMPLE_DOC, "questions": answered, "needs": []}, rev=2, needs=None, open_questions=0)
    page = phone_page("light")
    zero = page.locator(".inbox-zero")
    zero.get_by_text("Nothing needs you right now").wait_for()
    zero.get_by_text("1 linked ticket, none waiting on you.", exact=False).wait_for()
    assert page.locator(".ncard").count() == 0


# ---- review fix round 1 ----

def _block_decision_posts(page):
    """POST /api/decisions fails like a dropped connection: the phone queues the decision in its outbox."""
    page.route("**/api/decisions", lambda route: route.abort() if route.request.method == "POST" else route.continue_())


def test_a_queued_answer_shows_queued_after_a_reload(phone_page, mirror_with_question):
    page = phone_page("light")
    _block_decision_posts(page)
    card = page.locator(f'.ncard[data-n="{mirror_with_question.n}"]')
    card.get_by_role("button", name="A · ISO 8601").click()
    card.get_by_role("button", name="Send answer").click()
    card.get_by_text("Queued · sends when you're online").wait_for()
    page.reload()
    card.get_by_text("Queued · sends when you're online").wait_for()
    assert card.get_by_role("button", name="A · ISO 8601").is_disabled()


def test_a_queued_answer_to_q1_never_hides_q2(phone_page, mirror_with_question):
    page = phone_page("light")
    _block_decision_posts(page)
    card = page.locator(f'.ncard[data-n="{mirror_with_question.n}"]')
    card.get_by_role("button", name="A · ISO 8601").click()
    card.get_by_role("button", name="Send answer").click()
    card.get_by_text("Queued · sends when you're online").wait_for()
    q1 = EXAMPLE_DOC["questions"][0]
    q2 = {**q1, "id": "Q2", "text": "Which delimiter?", "hash": "sha256:" + "e" * 64,
          "options": [{"key": "C", "label": "Comma", "cost": None}, {"key": "S", "label": "Semicolon", "cost": None}],
          "recommended": "S"}
    mirror_with_question.push({**EXAMPLE_DOC, "questions": [{**q1, "answer": "A"}, q2],
                               "needs": [{"kind": "answer", "qids": ["Q2"]}]}, rev=2, needs="question", open_questions=1)
    page.reload()
    opt = card.get_by_role("button", name="S · Semicolon")
    opt.wait_for()
    assert opt.is_enabled()
    assert card.get_by_text("Queued · sends when you're online").count() == 0


def test_arming_keeps_focus_on_the_option(phone_page, mirror_with_question):
    page = phone_page("light")
    opt = page.locator(f'.ncard[data-n="{mirror_with_question.n}"]').get_by_role("button", name="B · Local time")
    opt.click()
    assert page.evaluate("() => document.activeElement.dataset.key") == "B"


def test_typing_a_ticket_request_survives_a_list_refresh(phone_page, mirror_with_question):
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/?view=tickets")
    page.locator('#needs-list[data-from="server"]').wait_for(state="attached")
    page.get_by_text("New ticket request").click()
    title = page.get_by_label("Title")
    title.fill("Rotate the API")
    page.evaluate("() => { window.__card = document.querySelector('.req-card'); }")
    page.evaluate("""() => { window.__renders = 0; new MutationObserver(() => window.__renders++)
      .observe(document.querySelector('.needs-sections'), { childList: true }); }""")
    mirror_with_question.push({**EXAMPLE_DOC, "title": "Export the readings, renamed"}, rev=2)   # the long-poll re-renders
    page.wait_for_function("() => window.__renders > 0")
    assert page.evaluate("() => document.activeElement.id") == "req-title"
    assert title.input_value() == "Rotate the API"
    assert page.evaluate("() => window.__card === document.querySelector('.req-card') && window.__card.isConnected")


def test_a_plan_changed_while_the_approve_sheet_is_open_sends_nothing(phone_page, mirror_with_question):
    mirror_with_question.push(_approval_doc(FULL_DOC), rev=2, needs="approval", open_questions=0)
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.get_by_role("button", name="Approve plan").click()
    page.get_by_role("dialog").wait_for()
    changed = _approval_doc({**FULL_DOC, "sections": {**FULL_DOC["sections"], "Plan": "1. Inventory\n2. Exporter\n3. Deploy"},
                             "gates": {**FULL_DOC["gates"], "plan": {**FULL_DOC["gates"]["plan"], "hash": "sha256:" + "9" * 64}}})
    mirror_with_question.push(changed, rev=3, needs="approval", open_questions=0)
    page.locator("#decision").get_by_text("3. Deploy").wait_for()
    page.get_by_role("dialog").get_by_role("button", name="Confirm approval").click()
    page.get_by_text("The plan changed while you looked: review it again").wait_for()
    assert mirror_with_question.inbox_items() == []


def test_a_new_testing_round_while_the_done_sheet_is_open_sends_nothing(phone_page, mirror_with_question):
    mirror_with_question.push(_verdict_doc("- round 2", rnd=2), rev=2, needs="verdict", open_questions=0, status="testing")
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.get_by_role("button", name="Done").click()
    page.get_by_role("dialog").wait_for()
    mirror_with_question.push(_verdict_doc("- round 5", rnd=5), rev=3, needs="verdict", open_questions=0, status="testing")
    page.locator("#decision").get_by_text("round 5").wait_for()
    page.get_by_role("dialog").get_by_role("button", name="Confirm done").click()
    page.get_by_text("The ticket changed while you looked: review it again").wait_for()
    assert mirror_with_question.inbox_items() == []


def test_the_decision_bar_never_covers_the_end_of_the_ticket(phone_page, mirror_with_question):
    mirror_with_question.push(_approval_doc(FULL_DOC), rev=2, needs="approval", open_questions=0)
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.get_by_role("button", name="Approve plan").wait_for()
    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    last = page.evaluate("() => [...document.querySelectorAll('#ticket > *')].pop().getBoundingClientRect().bottom")
    bar = page.evaluate("() => document.getElementById('decision-bar').getBoundingClientRect().top")
    assert last <= bar


# ---- part 2: the phone shows everything the gate hash covers (gates.<g>.covers, hash v2) and checks the hash ----

def test_a_requirements_approval_shows_size_and_type_and_checks_the_hash(phone_page, mirror_with_question):
    mirror_with_question.push(_approval_doc(FULL_DOC, "requirements"), rev=2, needs="approval", open_questions=0)
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    card = page.locator("#decision")
    card.locator(".gate-meta").wait_for()
    assert [t.strip() for t in card.locator(".gate-name").all_inner_texts()] == [
        "Requirements · full text", "Acceptance criteria · full text", "Out of scope · full text", "Also bound by this approval"]
    card.locator(".gate-meta").get_by_text("M", exact=True).wait_for()
    card.locator(".gate-meta").get_by_text("feature", exact=True).wait_for()
    approve = page.get_by_role("button", name="Approve requirements")
    expect(approve).to_be_enabled()                                       # the phone's own hash matched
    approve.click()
    page.get_by_role("dialog").get_by_text("with size m and type feature", exact=False).wait_for()
    page.get_by_role("dialog").get_by_role("button", name="Confirm approval").click()
    page.get_by_text("Sent · waiting for the desktop").wait_for()
    body = mirror_with_question.sim.s.open_inbox_item(mirror_with_question.sim.mk, mirror_with_question.inbox_items()[0])
    assert body["target"] == {"gate": "requirements", "hash": FULL_DOC["gates"]["requirements"]["hash"]}


def test_without_covers_the_phone_refuses_to_approve(phone_page, mirror_with_question):
    doc = _approval_doc(FULL_DOC)
    doc["gates"]["plan"] = {k: v for k, v in doc["gates"]["plan"].items() if k != "covers"}
    mirror_with_question.push(doc, rev=2, needs="approval", open_questions=0)
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.locator("#decision").get_by_text("doesn't say what the approval covers").wait_for()
    assert page.get_by_role("button", name="Approve plan").count() == 0
    page.get_by_role("button", name="Request changes").wait_for()


def test_text_that_does_not_match_the_hash_is_never_approved(phone_page, mirror_with_question):
    doc = _approval_doc(FULL_DOC)
    doc["sections"] = {**doc["sections"], "Plan": "1. Inventory\n2. Exporter\n3. Also delete the old exports"}
    mirror_with_question.push(doc, rev=2, needs="approval", open_questions=0)      # the hash is still the old text's
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.locator("#decision").get_by_text("doesn't match what the approval binds").wait_for()
    expect(page.get_by_role("button", name="Approve plan")).to_be_disabled()


# ---- security re-review: hidden characters are shown and decide nothing ----

def test_a_hidden_character_in_the_plan_is_a_badge_and_blocks_approve(phone_page, mirror_with_question):
    doc = _approval_doc(FULL_DOC)
    doc["sections"] = {**doc["sections"], "Plan": "1. Inventory\n2. Export\u202Eer"}
    mirror_with_question.push(doc, rev=2, needs="approval", open_questions=0)
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    card = page.locator("#decision")
    card.locator(".hidden-char").get_by_text("<U+202E>").wait_for()
    card.get_by_text("Ask the agent to remove them", exact=False).wait_for()
    assert page.get_by_role("button", name="Approve plan").count() == 0
    page.get_by_role("button", name="Request changes").wait_for()


def test_a_hidden_character_in_a_question_is_never_answered_from_the_phone(phone_page, mirror_with_question):
    q = {**EXAMPLE_DOC["questions"][0], "text": "Which time\u200Bstamp format?"}
    mirror_with_question.push({**EXAMPLE_DOC, "questions": [q]}, rev=2, needs="question", open_questions=1)
    page = phone_page("light")
    card = page.locator(f'.ncard[data-n="{mirror_with_question.n}"]')
    card.get_by_role("link", name="Answer on the ticket").wait_for()     # no one-tap options
    assert card.locator(".qa-opt").count() == 0
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.locator("#decision .hidden-char").get_by_text("<U+200B>").wait_for()
    page.get_by_role("radio", name="ISO 8601").check()
    expect(page.get_by_role("button", name="Send answer")).to_be_disabled()


# ---- schema 1.3: the verdict hash ----

def test_without_a_verdict_hash_the_phone_gives_no_verdict(phone_page, mirror_with_question):
    doc = _verdict_doc()
    del doc["verdict"]
    mirror_with_question.push(doc, rev=2, needs="verdict", open_questions=0, status="testing")
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.locator("#decision").get_by_text("doesn't send the verdict hash yet").wait_for()
    assert page.get_by_role("button", name="Done").count() == 0 and page.get_by_role("button", name="Send back").count() == 0


def test_an_epic_verdict_stays_on_the_desktop(phone_page, mirror_with_question):
    mirror_with_question.push(_verdict_doc(type="epic"), rev=2, needs="verdict", open_questions=0, status="testing")
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.locator("#decision").get_by_text("give it on the desktop", exact=False).wait_for()
    assert page.get_by_role("button", name="Done").count() == 0 and page.get_by_role("button", name="Send back").count() == 0


def test_at_title_send_back_carries_the_verdict_hash_and_done_stays_on_the_desktop(phone_page, mirror_with_question):
    full = _verdict_doc(rnd=4)
    title = {**EXAMPLE_DOC, "questions": [], "status": "testing", "needs": [{"kind": "verdict", "round": 4}],
             "verdict": full["verdict"], "verification_summary": "AC1 opened in Excel"}
    mirror_with_question.push(title, rev=2, needs="verdict", open_questions=0, status="testing")
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.locator("#decision").get_by_text("Accept on the desktop", exact=False).wait_for()
    assert page.get_by_role("button", name="Done").count() == 0
    page.locator("#decision-note").fill("The CSV is empty")
    page.get_by_role("button", name="Send back").click()
    page.get_by_text("Sent · waiting for the desktop").wait_for()
    body = mirror_with_question.sim.s.open_inbox_item(mirror_with_question.sim.mk, mirror_with_question.inbox_items()[0])
    assert body["value"] == "follow-up" and body["target"] == {"status": "testing", "round": 4, "hash": full["verdict"]["hash"]}


def test_evidence_that_does_not_match_the_verdict_hash_is_never_accepted(phone_page, mirror_with_question):
    doc = _verdict_doc()
    doc["sections"] = {**doc["sections"], "Verification": "- AC1: opened 3 files in Excel (and nothing else broke)"}
    mirror_with_question.push(doc, rev=2, needs="verdict", open_questions=0, status="testing")    # hash of the old text
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.locator("#decision").get_by_text("don't match what the verdict binds").wait_for()
    expect(page.get_by_role("button", name="Done")).to_be_disabled()


# ---- feedback round A (schema 1.4): requirements and plan approved together, the document's move chip ----

def test_requirements_and_plan_are_approved_together_in_one_decision(phone_page, mirror_with_question):
    base = {**FULL_DOC, "questions": [{**q, "answer": "A"} for q in FULL_DOC["questions"]],
            "gates": {**FULL_DOC["gates"], "requirements": {**FULL_DOC["gates"]["requirements"], "state": "pending"},
                      "plan": {**FULL_DOC["gates"]["plan"], "state": "pending"}},
            "needs": [{"kind": "approve-requirements", "together": True}],
            "move": {"who": "you", "kind": "approve-requirements", "label": "Approve requirements and plan", "ref": "requirements"}}
    mirror_with_question.push(base, rev=2, needs="approval", open_questions=0)
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    card = page.locator("#decision")
    card.get_by_text("1. Inventory").wait_for()                                   # the plan
    card.get_by_text("- One file per day").wait_for()                             # the requirements
    page.locator(".t-head").get_by_text("Approve requirements and plan").wait_for()   # the document's move
    btn = page.get_by_role("button", name="Approve requirements and plan")
    expect(btn).to_be_enabled()                                                   # both hashes checked
    btn.click()
    page.get_by_role("dialog", name="Approve requirements and plan?").get_by_role("button", name="Confirm approval").click()
    page.get_by_text("Sent · waiting for the desktop").wait_for()
    items = mirror_with_question.inbox_items()
    assert len(items) == 1                                                        # ONE decision
    body = mirror_with_question.sim.s.open_inbox_item(mirror_with_question.sim.mk, items[0])
    assert body["kind"] == "approve" and body["target"] == {
        "gate": "requirements", "hash": FULL_DOC["gates"]["requirements"]["hash"], "plan_hash": FULL_DOC["gates"]["plan"]["hash"]}


def test_an_unpaired_phone_says_why_its_decision_waits(phone_page, mirror_with_question):
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.get_by_role("radio", name="ISO 8601").check()
    page.get_by_role("button", name="Send answer").click()
    page.get_by_text("Sent · waiting for the desktop").wait_for()
    page.get_by_text("Pair this phone so answers apply directly", exact=False).wait_for()


# ---- feedback fix round F1: a held decision says why; no ack for two minutes says so ----

def test_a_waiting_ack_says_why_and_a_later_apply_replaces_it(phone_page, mirror_with_question):
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.get_by_role("radio", name="ISO 8601").check()
    page.get_by_role("button", name="Send answer").click()
    page.get_by_text("Sent · waiting for the desktop").wait_for()
    d = mirror_with_question.decisions()[0]
    ack = lambda value: httpx.post(f"{mirror_with_question.base}/api/decisions/{d['id']}/ack", json={"ack": value},
                                   headers=mirror_with_question.headers, timeout=30)
    assert ack("waiting-switched-off").status_code == 204
    page.locator("#decision").get_by_text(
        "Not applied · decisions of this kind from the phone are switched off · open the desktop").wait_for()
    assert ack("applied").status_code == 204                                 # the owner applied it on the desktop
    page.locator("#decision").get_by_text("Applied on desktop").wait_for()


def test_no_ack_after_two_minutes_reads_not_applied_yet(phone_page, mirror_with_question):
    page = phone_page("light")
    page.goto(f"{mirror_with_question.base}/t/{mirror_with_question.n}")
    page.get_by_role("radio", name="ISO 8601").check()
    page.get_by_role("button", name="Send answer").click()
    page.get_by_text("Sent · waiting for the desktop").wait_for()
    # the phone's clock moves on three minutes; the next render says it
    page.evaluate("() => { const real = Date.now; Date.now = () => real() + 180000; }")
    page.evaluate("() => window.dispatchEvent(new Event('fs:decisions-changed'))")
    page.locator("#decision").get_by_text("Not applied yet · open the desktop").wait_for()
