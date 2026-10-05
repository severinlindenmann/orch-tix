# tests/browser/test_offline.py — the offline PWA (spec §16): the service worker, the encrypted
# outbox and the Note composer, end to end in Chromium with context.set_offline().
import io
import json
import re

import pytest
from playwright.sync_api import expect

from .conftest import login_ui

pytestmark = pytest.mark.browser

NOTE_TEXT = "the secret offline note: pineapple-7431"
NOTE_NAME = "offline-plan.md"
OFFLINE_TEXT = "You're offline. New notes, recordings and uploads will be sent when you're back online."


# Routes go on the context, which (unlike page.route) also covers requests made by a service worker.
# These tests need the real service worker, so they allow it explicitly (Playwright's default is
# "allow" too; saying it keeps them independent of any suite-wide setting).
@pytest.fixture
def browser_context_args(browser_context_args):
    return {**browser_context_args, "service_workers": "allow"}


@pytest.fixture(autouse=True)
def _chromium_only(browser_name):
    if browser_name != "chromium":
        pytest.skip("service worker offline emulation and the fake microphone are Chromium-only here")


def server_files(sim):
    """(id, meta, plaintext) for every live file on the server, decrypted like the browser would."""
    out = []
    for f in sim.request("GET", "/api/files?acked=1").json()["files"]:
        if f.get("deleted_at"):
            continue
        meta, dek = sim.s.open_file_meta(sim.mk, f)
        blob = sim.request("GET", f"/api/files/{f['id']}/blob").content
        pt = io.BytesIO()
        sim.s.decrypt_stream(dek, bytes.fromhex(f["uuid"]), io.BytesIO(blob), pt)
        out.append((f["id"], meta, pt.getvalue()))
    return out


def controlled(page, base_url, sim):
    """Signed in, with the service worker installed and controlling a freshly reloaded page."""
    login_ui(page, base_url, sim.passphrase)
    page.wait_for_load_state("networkidle")
    page.evaluate("async () => { await navigator.serviceWorker.ready; }")
    page.reload()
    page.wait_for_load_state("networkidle")
    page.wait_for_function("() => navigator.serviceWorker.controller !== null")
    return page


def go_offline(page):
    page.context.set_offline(True)
    page.reload()
    expect(page.locator("#offline")).to_be_visible()


# Every outbox record as {field: value}, the ciphertext blob as latin-1 text so a search sees its bytes.
READ_OUTBOX = """() => new Promise((resolve, reject) => {
  const q = indexedDB.open("fileshare");
  q.onerror = () => reject(q.error);
  q.onsuccess = () => {
    const db = q.result;
    if (!db.objectStoreNames.contains("outbox")) { db.close(); resolve([]); return; }
    const g = db.transaction("outbox").objectStore("outbox").getAll();
    g.onsuccess = () => {
      db.close();
      resolve(g.result.map((r) => {
        const o = {};
        for (const [k, v] of Object.entries(r)) {
          if (v instanceof ArrayBuffer) {
            let s = ""; for (const b of new Uint8Array(v)) s += String.fromCharCode(b);
            o[k] = { bytes: v.byteLength, latin1: s };
          } else o[k] = v;
        }
        return o;
      }));
    };
  };
})"""


def outbox_records(page):
    return page.evaluate(READ_OUTBOX)


def write_note(page, text=NOTE_TEXT, name=NOTE_NAME):
    page.locator("#note-btn").click()
    sheet = page.locator("dialog.note-sheet")
    expect(sheet).to_be_visible()
    if name is not None:
        sheet.locator("#note-name").fill(name)
    sheet.locator("#note-text").fill(text)
    sheet.get_by_role("button", name="Save").click()
    expect(sheet).to_have_count(0)


def test_the_worker_installs_precaches_the_shell_and_controls_the_page(page, live_server, sim):
    controlled(page, live_server.url, sim)
    build = page.evaluate("""() => new URL(navigator.serviceWorker.controller.scriptURL).searchParams.get("v")""")
    css_v = page.evaluate("""() => new URL(document.querySelector('link[rel=stylesheet]').href).searchParams.get("v")""")
    assert build and build == css_v
    # The login page registered the worker while signed out; the signed-in page fills in /settings.
    page.wait_for_function("""async () => Boolean(await caches.match("/settings"))""")
    cached = page.evaluate("""async () => {
      const names = await caches.keys();
      const c = await caches.open(names.find((n) => n.startsWith("shell-")));
      return { names, keys: (await c.keys()).map((r) => new URL(r.url).pathname) };
    }""")
    assert cached["names"] == [f"shell-{build}"]
    for path in ["/", "/t", "/files", "/settings", "/login", "/static/js/outbox.js", "/static/css/app.css",
                 "/static/fonts/manrope-latin-wght.woff2", "/static/js/needs.js", "/static/js/ticket.js", "/static/vendor/purify.min.js", "/manifest.webmanifest"]:
        assert path in cached["keys"], path


def test_offline_reload_opens_from_the_cache_and_shows_the_offline_state(page, live_server, sim):
    controlled(page, live_server.url, sim)
    go_offline(page)
    expect(page.locator("#offline")).to_contain_text(OFFLINE_TEXT)
    expect(page.locator("#offline h2")).to_have_text("Offline")
    expect(page.locator("#load-error")).to_be_hidden()
    for name in ("Upload", "New note", "Record audio"):
        expect(page.get_by_role("button", name=name).first).to_be_enabled()
    # The other pages open from the cache too.
    page.goto(live_server.url + "/settings")
    expect(page.locator("h1")).to_contain_text("Settings")
    expect(page.locator("#devices-title")).to_contain_text("Devices")
    page.context.set_offline(False)


def test_a_note_written_offline_is_queued_encrypted_and_uploads_once_online(page, live_server, sim):
    controlled(page, live_server.url, sim)
    before = {f[0] for f in server_files(sim)}
    go_offline(page)
    write_note(page)
    slot = page.locator("#outbox-slot")
    expect(slot).to_be_visible()
    expect(slot.locator(".outbox-head")).to_contain_text("Waiting to upload · 1")
    row = slot.locator(".frow-pending")
    expect(row).to_have_count(1)
    expect(row).to_contain_text(NOTE_NAME)

    records = outbox_records(page)
    assert len(records) == 1
    rec = records[0]
    assert set(rec) == {"seq", "state", "uuid", "key_version", "wrapped_dek", "enc_meta", "ttl", "blob", "size",
                        "created_at", "kind"}
    assert rec["kind"] == "note" and rec["ttl"] == "7d" and rec["state"] == "pending"
    assert rec["blob"]["bytes"] == rec["size"]
    dump = json.dumps(records)
    for secret in (NOTE_TEXT, "pineapple", NOTE_NAME, "offline-plan", "text/markdown"):
        assert secret not in dump, secret
        assert secret not in rec["blob"]["latin1"], secret

    # After a reload the name is decrypted from enc_meta (the MK is here); still nothing is sent.
    page.reload()
    expect(page.locator("#outbox-slot .frow-pending")).to_contain_text(NOTE_NAME)
    assert {f[0] for f in server_files(sim)} == before

    page.context.set_offline(False)
    expect(page.locator("#outbox-slot")).to_be_hidden(timeout=20_000)
    expect(page.locator("#offline")).to_be_hidden()
    expect(page.locator("#file-list .frow").filter(has_text=NOTE_NAME)).to_have_count(1, timeout=20_000)
    assert outbox_records(page) == []
    new = [f for f in server_files(sim) if f[0] not in before]
    assert len(new) == 1
    _, meta, pt = new[0]
    assert meta["name"] == NOTE_NAME and meta["mime"] == "text/markdown"
    assert pt.decode() == NOTE_TEXT


def test_a_duplicate_uuid_on_retry_counts_as_done(page, live_server, sim):
    controlled(page, live_server.url, sim)
    posts = []

    # The first POST reaches the server and commits, but its answer is lost on the way back.
    def lose_first_answer(route):
        if route.request.method != "POST":
            route.continue_()
            return
        posts.append(1)
        if len(posts) == 1:
            route.fetch()
            route.abort()
            return
        response = route.fetch()
        posts[-1] = response.status
        route.fulfill(response=response)

    page.context.route("**/api/files", lose_first_answer)
    write_note(page, name="dup.md")
    expect(page.locator("#outbox-slot .frow-pending")).to_contain_text("dup.md")
    page.locator("#outbox-retry").click()
    expect(page.locator("#outbox-slot")).to_be_hidden(timeout=20_000)
    assert posts == [1, 409]
    assert outbox_records(page) == []
    assert [m["name"] for _, m, _ in server_files(sim)].count("dup.md") == 1
    expect(page.locator("#file-list .frow").filter(has_text="dup.md")).to_have_count(1)


def test_sign_out_with_a_queue_asks_and_discarding_clears_it(page, live_server, sim):
    controlled(page, live_server.url, sim)
    go_offline(page)
    write_note(page, name="kept.md")
    expect(page.locator("#outbox-slot .frow-pending")).to_have_count(1)

    page.locator(".sidebar").get_by_role("button", name="Sign out").click()
    dlg = page.locator("dialog.confirm")
    expect(dlg).to_contain_text("Discard 1 item that hasn't uploaded?")
    dlg.get_by_role("button", name="Cancel").click()
    expect(dlg).to_have_count(0)
    assert page.url == live_server.url + "/files" and len(outbox_records(page)) == 1

    page.locator(".sidebar").get_by_role("button", name="Sign out").click()
    page.locator("dialog.confirm").get_by_role("button", name="Discard and sign out").click()
    page.wait_for_url(re.compile(r"/login"))
    assert outbox_records(page) == []
    keys = page.evaluate("async () => (await import('/static/js/keystore.js')).loadKeys()")
    assert keys is None
    page.context.set_offline(False)


def test_a_recording_made_offline_is_queued_and_uploads_later(page, live_server, sim):
    page.context.grant_permissions(["microphone"])
    controlled(page, live_server.url, sim)
    go_offline(page)
    page.get_by_role("button", name="Record audio").click()
    dlg = page.locator("dialog.recorder")
    expect(dlg.locator(".recorder-status")).to_have_text("Recording…")
    page.wait_for_timeout(1200)
    dlg.get_by_role("button", name="Stop & upload").click()
    expect(dlg).to_have_count(0, timeout=20_000)  # queued: the recorder closes
    row = page.locator("#outbox-slot .frow-pending")
    expect(row).to_contain_text(re.compile(r"recording-\d{8}-\d{6}\.webm"))
    (rec,) = outbox_records(page)
    assert rec["kind"] == "recording"

    page.context.set_offline(False)
    expect(page.locator("#outbox-slot")).to_be_hidden(timeout=20_000)
    names = [m["name"] for _, m, _ in server_files(sim)]
    assert any(re.fullmatch(r"recording-\d{8}-\d{6}\.webm", n) for n in names)


def test_api_requests_are_never_served_from_the_cache(page, live_server, sim):
    controlled(page, live_server.url, sim)
    assert page.evaluate("async () => (await fetch('/api/files')).status") == 200
    page.context.set_offline(True)
    outcome = page.evaluate("""async () => {
      try { const r = await fetch('/api/files'); return 'served ' + r.status; } catch (e) { return 'failed'; }
    }""")
    assert outcome == "failed"
    page.context.set_offline(False)
    cached = page.evaluate("""async () => {
      const out = [];
      for (const n of await caches.keys()) for (const r of await (await caches.open(n)).keys()) out.push(new URL(r.url).pathname);
      return out;
    }""")
    assert cached and not [p for p in cached if re.match(r"^/(api|p|skill)/|^/onboarding|^/sw\.js", p)]


def test_the_note_composer_saves_a_markdown_file_online(page, live_server, sim):
    login_ui(page, live_server.url, sim.passphrase)
    page.wait_for_load_state("networkidle")
    page.locator("#note-btn").click()
    sheet = page.locator("dialog.note-sheet")
    expect(sheet.locator("#note-text")).to_be_focused()
    save = sheet.get_by_role("button", name="Save")
    expect(save).to_be_disabled()
    sheet.locator("#note-text").fill("   ")
    expect(save).to_be_disabled()
    expect(sheet.locator("#note-name")).to_have_attribute("placeholder", re.compile(r"^note-\d{4}-\d{2}-\d{2}-\d{4}\.md$"))
    expect(sheet.locator('#note-ttl [aria-pressed="true"]')).to_have_text("7 days")
    sheet.locator('#note-ttl [data-ttl="1d"]').click()
    sheet.locator("#note-text").fill("# Groceries\n- milk\n")
    save.click()
    expect(page.locator("#file-list .frow").filter(has_text=re.compile(r"note-\d{4}-\d{2}-\d{2}-\d{4}\.md"))).to_have_count(1)
    (fid, meta, pt), = [f for f in server_files(sim) if f[1]["name"].startswith("note-")]
    assert meta["mime"] == "text/markdown" and pt == b"# Groceries\n- milk\n"
    expires = sim.get_file(fid)["expires_at"]
    assert expires is not None
    assert outbox_records(page) == []


def test_the_note_button_is_in_the_phone_dock(browser, live_server, sim):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    try:
        p = ctx.new_page()
        login_ui(p, live_server.url, sim.passphrase)
        p.wait_for_load_state("networkidle")
        btn = p.locator("#dock #note-btn")
        expect(btn).to_be_visible()
        box = btn.bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44
        btn.tap()
        expect(p.locator("dialog.note-sheet")).to_be_visible()
    finally:
        ctx.close()


def test_a_refused_item_is_marked_failed_with_retry_and_discard(page, live_server, sim):
    controlled(page, live_server.url, sim)
    go_offline(page)
    write_note(page, name="refused.md")
    expect(page.locator("#outbox-slot .frow-pending")).to_contain_text("refused.md")  # queued, not in flight

    def refuse(route):
        if route.request.method != "POST":
            route.continue_()
            return
        route.fulfill(status=400, content_type="application/json",
                      body=json.dumps({"error": "bad_request", "detail": "no"}))

    page.context.route("**/api/files", refuse)
    page.context.set_offline(False)
    row = page.locator("#outbox-slot .frow-failed")
    expect(row).to_contain_text("refused.md", timeout=20_000)
    expect(row).to_contain_text("Couldn't upload (bad_request)")
    (rec,) = outbox_records(page)
    assert rec["state"] == "failed"
    page.context.unroute("**/api/files")
    expect(row.get_by_role("button", name="Retry")).to_be_visible()
    row.get_by_role("button", name="Discard").click()
    expect(page.locator("#outbox-slot")).to_be_hidden()
    assert outbox_records(page) == []
    assert "refused.md" not in [m["name"] for _, m, _ in server_files(sim)]


def test_sign_out_everywhere_with_a_queue_asks_and_discards_it(page, live_server, sim):
    controlled(page, live_server.url, sim)
    go_offline(page)
    write_note(page, name="waiting.md")
    expect(page.locator("#outbox-slot .frow-pending")).to_contain_text("waiting.md")
    # Back online, but the server errors: the item stays queued, retrying with backoff.
    page.context.route("**/api/files", lambda route: route.fulfill(status=503, body="") if route.request.method == "POST"
                       else route.continue_())
    page.context.set_offline(False)
    page.goto(live_server.url + "/settings")
    page.wait_for_load_state("networkidle")
    page.locator("#signout-all").click()
    page.locator("dialog.confirm").get_by_role("button", name="Sign out everywhere").click()
    dlg = page.locator("dialog.confirm")
    expect(dlg).to_contain_text("Discard 1 item that hasn't uploaded?")
    dlg.get_by_role("button", name="Discard and sign out").click()
    page.wait_for_url(re.compile(r"/login"))
    assert outbox_records(page) == []


# Turns this origin's fileshare DB back into what a pre-§16 browser has: version 1, only "keys",
# holding the same non-extractable keys (CryptoKeys survive the structured clone).
DOWNGRADE_TO_V1 = """async () => {
  const req = (r) => new Promise((ok, bad) => { r.onsuccess = () => ok(r.result); r.onerror = () => bad(r.error); });
  const db2 = await req(indexedDB.open("fileshare"));
  const rec = await req(db2.transaction("keys").objectStore("keys").get("current"));
  db2.close();
  await req(indexedDB.deleteDatabase("fileshare"));
  const open1 = indexedDB.open("fileshare", 1);
  open1.onupgradeneeded = () => open1.result.createObjectStore("keys");
  const db1 = await req(open1);
  await new Promise((ok, bad) => {
    const tx = db1.transaction("keys", "readwrite");
    tx.objectStore("keys").put(rec, "current");
    tx.oncomplete = ok; tx.onerror = () => bad(tx.error);
  });
  const out = { version: db1.version, stores: [...db1.objectStoreNames], extractable: rec.mk.extractable };
  db1.close();
  return out;
}"""

DB_STATE = """async () => {
  const db = await new Promise((ok, bad) => { const r = indexedDB.open("fileshare"); r.onsuccess = () => ok(r.result); r.onerror = () => bad(r.error); });
  const out = { version: db.version, stores: [...db.objectStoreNames].sort() };
  db.close();
  return out;
}"""


def test_v1_keys_survive_the_upgrade_to_the_outbox_db(page, live_server, sim):
    f = sim.upload("before-upgrade.txt", b"hi")
    login_ui(page, live_server.url, sim.passphrase)
    page.wait_for_load_state("networkidle")
    # A same-origin page that opens no database, so the delete isn't blocked.
    page.goto(live_server.url + "/static/precache.json")
    seeded = page.evaluate(DOWNGRADE_TO_V1)
    assert seeded == {"version": 1, "stores": ["keys"], "extractable": False}

    page.goto(live_server.url + "/files")
    page.wait_for_load_state("networkidle")
    assert page.url == live_server.url + "/files"          # still signed in: no bounce to /login
    expect(page.locator(f'.frow[data-id="{f["id"]}"]')).to_contain_text("before-upgrade.txt")  # MK decrypts
    assert page.evaluate(DB_STATE) == {"version": 7, "stores": ["keys", "labels", "lists", "outbox", "pairs", "prefs", "seen", "tickets"]}
    keys = page.evaluate("async () => { const k = await (await import('/static/js/keystore.js')).loadKeys();"
                         " return k && { mk: k.mk.extractable, kek: k.kek.extractable, v: k.keyVersion }; }")
    assert keys == {"mk": False, "kek": False, "v": sim.key_version}
    # And the new store works: a note written now goes through the outbox path offline.
    page.context.set_offline(True)
    write_note(page, name="after-upgrade.md")
    expect(page.locator("#outbox-slot .frow-pending")).to_contain_text("after-upgrade.md")
    page.context.set_offline(False)
    expect(page.locator("#outbox-slot")).to_be_hidden(timeout=20_000)
