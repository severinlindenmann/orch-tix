"""Already loaded tickets open offline (#73) and key links need no full decrypt (#14): the phone keeps each opened
ticket's sealed row in IndexedDB "tickets" (bounded, cleared on sign-out) and the sealed key -> number map in
"lists". Offline /t/<n> opens the stored copy with "Offline · last updated <time>", every action off; an older copy
than this phone already saw is refused. Local server only."""
import os
import re

import httpx
import pytest
from playwright.sync_api import expect

from .conftest import EXAMPLE_DOC, FULL_DOC, sealed_doc

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True)
def _chromium_only(browser_name):
    if browser_name != "chromium":
        pytest.skip("service worker offline emulation is Chromium-only here")


@pytest.fixture
def browser_context_args(browser_context_args):
    return {**browser_context_args, "service_workers": "allow"}


READ_STORE = """(name) => new Promise((resolve, reject) => {
  const q = indexedDB.open("fileshare");
  q.onerror = () => reject(q.error);
  q.onsuccess = () => {
    const db = q.result;
    const tx = db.transaction(name);
    const keys = tx.objectStore(name).getAllKeys();
    const vals = tx.objectStore(name).getAll();
    tx.oncomplete = () => { db.close(); resolve(Object.fromEntries(keys.result.map((k, i) => [k, vals.result[i]]))); };
  };
})"""

PUT_SEEN = """([key, mark]) => new Promise((ok, fail) => { const q = indexedDB.open("fileshare");
  q.onerror = () => fail(q.error);
  q.onsuccess = () => { const tx = q.result.transaction("seen", "readwrite");
    tx.objectStore("seen").put(mark, key); tx.oncomplete = () => { q.result.close(); ok(); }; }; })"""


def open_ticket(page, m):
    page.goto(f"{m.base}/t/{m.n}")
    page.get_by_role("radio", name="ISO 8601").wait_for()
    page.wait_for_function("""() => new Promise((ok) => { const q = indexedDB.open("fileshare");
      q.onsuccess = () => { const g = q.result.transaction("tickets").objectStore("tickets").count();
        g.onsuccess = () => { q.result.close(); ok(g.result > 0); }; }; })""")


def test_a_loaded_ticket_opens_offline_with_its_state_and_every_action_off(phone_page, mirror_with_question, context):
    m = mirror_with_question
    page = phone_page("light")
    open_ticket(page, m)
    expect(page.locator(".offline-note")).to_have_count(0)
    context.set_offline(True)
    try:
        page.reload()
        page.get_by_role("radio", name="ISO 8601").wait_for()
        note = page.locator(".offline-note")
        expect(note).to_contain_text(re.compile(r"Offline · last updated \d\d:\d\d"))
        expect(page.locator("#ticket-error")).to_be_hidden()
        expect(page.locator(".t-title")).to_contain_text(EXAMPLE_DOC["title"])
        expect(page.locator(".t-ids")).to_contain_text("Acme Energy")          # the label from the stored spaces
        for radio in page.get_by_role("radio").all():
            expect(radio).to_be_disabled()
        expect(page.locator("#decision textarea")).to_be_disabled()
        expect(page.get_by_role("button", name="Send answer")).to_be_disabled()
        expect(page.locator("#decision-bar")).to_contain_text("Offline: actions are off")
        assert m.decisions() == []
    finally:
        context.set_offline(False)
    # Back online: the live ticket replaces the stored copy and the actions are on again.
    expect(page.locator(".offline-note")).to_have_count(0, timeout=15_000)
    expect(page.get_by_role("radio", name="ISO 8601")).to_be_enabled()
    page.get_by_role("radio", name="ISO 8601").check()
    expect(page.get_by_role("button", name="Send answer")).to_be_enabled()


def test_an_approval_card_is_off_offline_too(phone_page, mirror_with_question, context):
    m = mirror_with_question
    gates = {**FULL_DOC["gates"], "plan": {**FULL_DOC["gates"]["plan"], "state": "pending"}}
    doc = {**FULL_DOC, "questions": [{**q, "answer": "A"} for q in FULL_DOC["questions"]], "gates": gates,
           "needs": [{"kind": "approve-plan"}], "move": {"who": "you", "kind": "approve-plan", "label": "Approve plan", "ref": "plan"}}
    m.push(doc, rev=2, needs="approval", open_questions=0)
    page = phone_page("light")
    page.goto(f"{m.base}/t/{m.n}")
    expect(page.get_by_role("button", name=re.compile("^Approve"))).to_be_enabled()
    page.wait_for_function("""() => new Promise((ok) => { const q = indexedDB.open("fileshare");
      q.onsuccess = () => { const g = q.result.transaction("tickets").objectStore("tickets").count();
        g.onsuccess = () => { q.result.close(); ok(g.result > 0); }; }; })""")
    context.set_offline(True)
    try:
        page.reload()
        expect(page.locator(".offline-note")).to_be_visible()
        expect(page.get_by_role("button", name=re.compile("^Approve"))).to_be_disabled()
        expect(page.get_by_role("button", name="Request changes")).to_be_disabled()
        expect(page.locator("#decision textarea")).to_be_disabled()
    finally:
        context.set_offline(False)


def test_a_ticket_never_opened_opens_from_the_last_known_list_and_an_unknown_one_says_it_loads_when_back(
        phone_page, mirror_with_question, context):
    m = mirror_with_question
    page = phone_page("light")                       # Needs you stored the list; the ticket itself was never opened
    assert page.evaluate(READ_STORE, "tickets") == {}
    context.set_offline(True)
    try:
        page.goto(f"{m.base}/t/{m.n}")
        expect(page.locator(".offline-note")).to_contain_text("Offline · last updated")
        expect(page.get_by_role("button", name="Send answer")).to_be_disabled()
        page.goto(f"{m.base}/t/9999")
        page.get_by_text("You're offline. The ticket loads when you're back.").wait_for()
        expect(page.locator(".offline-note")).to_have_count(0)
    finally:
        context.set_offline(False)


def test_what_is_stored_is_the_servers_ciphertext_and_nothing_readable(phone_page, mirror_with_question):
    m = mirror_with_question
    page = phone_page("light")
    open_ticket(page, m)
    tickets = page.evaluate(READ_STORE, "tickets")
    assert list(tickets) == [str(m.n)]
    entry = tickets[str(m.n)]
    server = httpx.get(f"{m.base}/api/mirrors/{m.id}", cookies={c["name"]: c["value"] for c in page.context.cookies()}, timeout=30)
    if server.status_code == 200:
        assert entry["row"]["enc_content"] == server.json()["enc_content"]
    raw = repr(tickets) + repr(page.evaluate(READ_STORE, "lists"))
    for plain in (EXAMPLE_DOC["title"], EXAMPLE_DOC["id"], "ISO 8601", "Acme Energy"):
        assert plain not in raw, plain
    assert "doc" not in entry["row"] and "dek" not in entry["row"]


def test_a_stored_copy_older_than_the_seen_mark_is_refused_offline(phone_page, mirror_with_question, context):
    m = mirror_with_question
    page = phone_page("light")
    open_ticket(page, m)
    # This phone saw a newer snapshot of the ticket than the stored one (e.g. on another tab, then storage was rolled back).
    page.evaluate(PUT_SEEN, [f"{m.space}|{EXAMPLE_DOC['id']}", {"gen": 1, "mirror_rev": 99}])
    context.set_offline(True)
    try:
        page.reload()
        page.get_by_text("You're offline. The ticket loads when you're back.").wait_for()
        expect(page.get_by_role("radio")).to_have_count(0)
        mark = page.evaluate(READ_STORE, "seen")[f"{m.space}|{EXAMPLE_DOC['id']}"]
        assert mark == {"gen": 1, "mirror_rev": 99}                  # a cached copy never moves the mark
    finally:
        context.set_offline(False)


def test_a_ticket_gone_from_the_server_is_forgotten(phone_page, mirror_with_question):
    m = mirror_with_question
    page = phone_page("light")
    open_ticket(page, m)
    page.goto(f"{m.base}/t/9999")
    page.get_by_text("This ticket is no longer on the phone.").wait_for()
    # (an unknown number never had an entry; the known one is untouched)
    assert list(page.evaluate(READ_STORE, "tickets")) == [str(m.n)]


def test_sign_out_clears_the_stored_tickets_and_the_key_map(phone_page, mirror_with_question):
    m = mirror_with_question
    page = phone_page("light")
    open_ticket(page, m)
    assert set(page.evaluate(READ_STORE, "lists")) >= {"keymap"}
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{m.base}/files")
    page.get_by_role("button", name="Sign out", exact=True).click()
    page.wait_for_url(m.base + "/login")
    assert page.evaluate(READ_STORE, "tickets") == {}
    assert page.evaluate(READ_STORE, "lists") == {}


def _other_ticket(m, key, title):
    s = m.sim.s
    other = {**FULL_DOC, "id": key, "title": title, "questions": [], "needs": []}
    uuid = s.mirror_uuid(m.space, key, 1)
    dek = os.urandom(32)
    tu = bytes.fromhex(uuid)
    r = httpx.put(f"{m.base}/api/mirrors/{uuid}", headers=m.headers, timeout=30, json={
        "space": m.space, "mirror_rev": 1, "schema_version": "1.0.0", "status": "open", "priority": "normal",
        "needs": None, "open_questions": 0, "key_version": 1,
        "wrapped_dek": s.b64u(s.seal(m.sim.mk, dek, s.aad_tdek(tu))),
        "enc_content": s.seal_mirror(dek, tu, sealed_doc(other, 1)),
        "event_uuid": s.mirror_event_uuid(m.space, key, 1, 1)})
    assert r.status_code == 200, r.text
    return r.json()["n"]


# Every decrypt with the code path that asked for it (the tab-bar badge opens the whole list on every page by itself;
# what matters is that the key links do not).
COUNT_DECRYPTS = """(() => {
  const orig = crypto.subtle.decrypt.bind(crypto.subtle);
  Error.stackTraceLimit = 60;
  window.__stacks = [];
  crypto.subtle.decrypt = (...a) => { window.__stacks.push(new Error().stack); return orig(...a); };
})();"""


def test_key_links_come_from_the_sealed_map_without_opening_the_whole_list(phone_page, mirror_with_question):
    m = mirror_with_question
    numbers = {f"DEMO-01{i:02d}": _other_ticket(m, f"DEMO-01{i:02d}", f"Other ticket {i}") for i in range(6)}
    doc = {**FULL_DOC, "questions": [], "status": "in-progress", "needs": [],
           "sections": {**FULL_DOC["sections"], "Context": "Builds on DEMO-0103."}}
    m.push(doc, rev=2, needs=None, open_questions=0, status="in-progress")
    page = phone_page("light")
    page.goto(f"{m.base}/?view=tickets")
    page.locator(".brow-plain, .bfold, .tcard, .brow").first.wait_for()
    page.wait_for_function("""() => new Promise((ok) => { const q = indexedDB.open("fileshare");
      q.onsuccess = () => { const g = q.result.transaction("lists").objectStore("lists").get("keymap");
        g.onsuccess = () => { q.result.close(); ok(Boolean(g.result)); }; }; })""")
    page.add_init_script(COUNT_DECRYPTS)
    page.goto(f"{m.base}/t/{m.n}")
    page.locator('.chap[data-chapter="1"] > summary').click()
    link = page.locator(".section-text a.key-link")
    expect(link).to_have_text("DEMO-0103")
    expect(link).to_have_attribute("href", f"/t/{numbers['DEMO-0103']}")
    page.wait_for_function("window.__stacks.some((s) => s.includes('loadKeyLinks'))")
    stacks = page.evaluate("window.__stacks")
    assert sum("loadKeyLinks" in st for st in stacks) == 1          # the sealed map: one small decrypt
    assert not any("loadCachedLists" in st for st in stacks)         # not one per mirrored ticket
