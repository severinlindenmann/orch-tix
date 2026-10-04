"""Task 10: pairing the phone with a desktop (orch-core remote humans, spec §6.4). The key lives in the
URL fragment, then in IndexedDB as a non-extractable HMAC key; a paired phone signs its decisions."""
import pytest

pytestmark = pytest.mark.browser
FRAG = "a" * 32 + ".ph_0123456789ab." + "A" * 43
CHECK_ZERO_KEY = "778 075"   # orch.remote.store.check_code(bytes(32)), as the page groups it

PAIRS_COUNT = """async () => { const db = await new Promise(r => { const q = indexedDB.open('fileshare');
    q.onsuccess = () => r(q.result); }); return db.objectStoreNames.contains('pairs')
    ? await new Promise(r => { const g = db.transaction('pairs').objectStore('pairs').count(); g.onsuccess = () => r(g.result); })
    : 0; }"""
PAIR_KEY = """async (space) => { const db = await new Promise(r => { const q = indexedDB.open('fileshare');
    q.onsuccess = () => r(q.result); }); const rec = await new Promise(r => {
    const g = db.transaction('pairs').objectStore('pairs').get(space); g.onsuccess = () => r(g.result); });
    let exported = true; try { await crypto.subtle.exportKey('raw', rec.key); } catch { exported = false; }
    return { extractable: rec.key.extractable, usages: rec.key.usages, exported, phoneId: rec.phoneId,
             keys: Object.keys(rec).sort() }; }"""
STANDALONE = """(() => { const real = window.matchMedia.bind(window);
    window.matchMedia = (q) => q === '(display-mode: standalone)'
      ? { matches: true, media: q, addEventListener() {}, removeEventListener() {} } : real(q); })()"""


def _pair_in_settings(page, base, link):
    page.goto(f"{base}/settings#pair")
    page.get_by_label("Pairing link").fill(link)
    page.get_by_role("button", name="Pair", exact=True).click()


def test_pair_page_outside_standalone_offers_copy_and_stores_nothing(phone_page):
    page = phone_page("light")
    base = page.url.split("/", 3)[0] + "//" + page.url.split("/", 3)[2]
    page.goto(f"{base}/pair#{FRAG}")
    page.get_by_text("Open this in the TIX app").wait_for()
    assert page.url == f"{base}/pair"                                  # fragment stripped
    page.get_by_role("button", name="Copy pairing link").wait_for()
    page.get_by_text("Settings → Pair with a desktop").wait_for()
    page.get_by_text("After pasting, clear the clipboard").wait_for()
    assert page.evaluate(PAIRS_COUNT) == 0


def test_copy_pairing_link_copies_the_whole_link(phone_page, context):
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    page = phone_page("light")
    base = page.url.split("/", 3)[0] + "//" + page.url.split("/", 3)[2]
    page.goto(f"{base}/pair#{FRAG}")
    page.get_by_role("button", name="Copy pairing link").click()
    page.get_by_text("Copied").wait_for()
    assert page.evaluate("navigator.clipboard.readText()") == f"{base}/pair#{FRAG}"
    assert page.evaluate(PAIRS_COUNT) == 0


def test_a_malformed_pair_link_says_so_and_stores_nothing(phone_page):
    page = phone_page("light")
    base = page.url.split("/", 3)[0] + "//" + page.url.split("/", 3)[2]
    page.goto(f"{base}/pair#{FRAG}A")
    page.get_by_text("This pairing link doesn't work").wait_for()
    assert page.url == f"{base}/pair"
    assert page.evaluate(PAIRS_COUNT) == 0


def test_pair_page_in_the_installed_app_shows_the_check_code_and_stores_on_yes(phone_page):
    page = phone_page("light")
    base = page.url.split("/", 3)[0] + "//" + page.url.split("/", 3)[2]
    page.add_init_script(STANDALONE)
    page.goto(f"{base}/pair#{FRAG}")
    page.get_by_text("Does the desktop show the same code?").wait_for()
    assert page.url == f"{base}/pair"
    assert page.locator(".pair-code").inner_text() == CHECK_ZERO_KEY
    page.get_by_role("button", name="Yes, pair").click()
    page.get_by_text("Paired with this workspace").wait_for()
    key = page.evaluate(PAIR_KEY, "a" * 32)
    assert key == {"extractable": False, "usages": ["sign"], "exported": False, "phoneId": "ph_0123456789ab",
                   "keys": ["key", "label", "paired_at", "phoneId", "space"]}


def test_no_on_the_check_code_stores_nothing(phone_page):
    page = phone_page("light")
    base = page.url.split("/", 3)[0] + "//" + page.url.split("/", 3)[2]
    _pair_in_settings(page, base, f"{base}/pair#{FRAG}")
    page.get_by_text("Does the desktop show the same code?").wait_for()
    page.get_by_role("button", name="No", exact=True).click()
    page.get_by_text("Not paired. Nothing was stored.").wait_for()
    assert page.evaluate(PAIRS_COUNT) == 0


def test_a_link_for_another_server_is_refused(phone_page):
    page = phone_page("light")
    base = page.url.split("/", 3)[0] + "//" + page.url.split("/", 3)[2]
    _pair_in_settings(page, base, f"https://elsewhere.example/pair#{FRAG}")
    page.get_by_text("That isn't a pairing link for this TIX").wait_for()
    assert page.get_by_label("Pairing link").input_value() == ""
    assert page.evaluate(PAIRS_COUNT) == 0


def test_paste_in_settings_shows_the_check_code_and_stores_on_yes(phone_page):
    page = phone_page("light")
    base = page.url.split("/", 3)[0] + "//" + page.url.split("/", 3)[2]
    page.goto(f"{base}/settings#pair")
    page.get_by_label("Pairing link").fill(f"{base}/pair#{FRAG}")
    page.get_by_role("button", name="Pair", exact=True).click()
    page.get_by_text("Does the desktop show the same code?").wait_for()
    page.get_by_role("button", name="Yes, pair").click()
    page.get_by_text("Paired with this workspace").wait_for()
    assert page.get_by_label("Pairing link").input_value() == ""
    row = page.locator(f'.pair-row[data-space="{"a" * 32}"]')
    row.wait_for()
    row.get_by_role("button", name="Forget").click()
    page.get_by_role("dialog").get_by_role("button", name="Forget").click()
    page.get_by_text("Not paired with any desktop.").wait_for()
    assert page.evaluate(PAIRS_COUNT) == 0


def test_paired_decision_carries_a_mac(phone_page, mirror_with_question, sim):
    page = phone_page("light")
    base = page.url.split("/", 3)[0] + "//" + page.url.split("/", 3)[2]
    page.goto(f"{base}/settings#pair")
    page.get_by_label("Pairing link").fill(f"{base}/pair#{mirror_with_question.space}.ph_0123456789ab.{'A' * 43}")
    page.get_by_role("button", name="Pair", exact=True).click()
    page.get_by_role("button", name="Yes, pair").click()
    page.get_by_text("Paired with this workspace").wait_for()
    page.goto(f"{base}/t/{mirror_with_question.n}")
    page.get_by_role("radio", name="ISO 8601").check()
    page.get_by_role("button", name="Send answer").click()
    page.get_by_text("Sent · applying on your desktop").wait_for()          # paired: no desktop Apply (round A)
    assert page.get_by_text("isn't paired with the desktop").count() == 0
    d = sim.s.open_inbox_item(sim.mk, mirror_with_question.inbox_items()[0])
    assert d["pair"] == "ph_0123456789ab" and len(d["mac"]) == 43
    # The MAC is orch-core's mac_of over the canonical JSON of the rest, with the all-zero test key.
    import base64
    import hashlib
    import hmac
    body = sim.s.canonical_json({k: v for k, v in d.items() if k != "mac"})
    assert d["mac"] == base64.urlsafe_b64encode(hmac.new(bytes(32), body, hashlib.sha256).digest()).decode().rstrip("=")


def test_a_paired_ticket_request_is_signed_for_core(phone_page, mirror_with_question, sim):
    """Feedback round A: core creates the backlog ticket from a signed request; the body is the one it verifies."""
    page = phone_page("light")
    base = page.url.split("/", 3)[0] + "//" + page.url.split("/", 3)[2]
    page.goto(f"{base}/settings#pair")
    page.get_by_label("Pairing link").fill(f"{base}/pair#{mirror_with_question.space}.ph_0123456789ab.{'A' * 43}")
    page.get_by_role("button", name="Pair", exact=True).click()
    page.get_by_role("button", name="Yes, pair").click()
    page.get_by_text("Paired with this workspace").wait_for()
    page.goto(f"{base}/?view=tickets")
    page.get_by_label("Title").fill("  Rotate   the API key ")
    page.get_by_role("button", name="Send request").click()
    page.get_by_text("Sent · applying on your desktop").wait_for()
    d = sim.s.open_inbox_item(sim.mk, mirror_with_question.inbox_items()[0])
    assert d["kind"] == "ticket_request" and d["pair"] == "ph_0123456789ab" and len(d["mac"]) == 43
    assert d["value"] == {"title": "Rotate the API key", "body": ""} and "ticket" not in d and "target" not in d
    import base64
    import hashlib
    import hmac
    body = sim.s.canonical_json({k: v for k, v in d.items() if k != "mac"})
    assert d["mac"] == base64.urlsafe_b64encode(hmac.new(bytes(32), body, hashlib.sha256).digest()).decode().rstrip("=")


def test_unpaired_decision_has_no_mac(phone_page, mirror_with_question, sim):
    page = phone_page("light")
    base = page.url.split("/", 3)[0] + "//" + page.url.split("/", 3)[2]
    page.goto(f"{base}/t/{mirror_with_question.n}")
    page.get_by_role("radio", name="ISO 8601").check()
    page.get_by_role("button", name="Send answer").click()
    page.get_by_text("Sent · waiting for the desktop").wait_for()
    d = sim.s.open_inbox_item(sim.mk, mirror_with_question.inbox_items()[0])
    assert "mac" not in d and "pair" not in d


# ---- review fix round 1 (I1): the key never reaches a request URL or a history entry ----
KEY_PART = "ph_0123456789ab." + "A" * 43


def _watch(page, context):
    """Every request URL (pages and the service worker) and every URL the page navigated to."""
    seen = {"requests": [], "navigations": []}
    context.on("request", lambda r: seen["requests"].append(r.url))
    page.on("framenavigated", lambda f: seen["navigations"].append(f.url) if f == page.main_frame else None)
    return seen


def _assert_no_key(seen, page):
    assert not [u for u in seen["requests"] if KEY_PART in u], seen["requests"]
    assert not [u for u in seen["navigations"][1:] if KEY_PART in u], seen["navigations"]
    assert KEY_PART not in page.url
    assert KEY_PART not in page.evaluate("location.href + ' ' + JSON.stringify(history.state)")


def test_a_slow_pair_page_never_falls_back_to_the_app_shell(phone_page, context):
    page = phone_page("light")
    base = page.url.split("/", 3)[0] + "//" + page.url.split("/", 3)[2]
    page.goto(f"{base}/files")                     # the worker caches the shell pages as they are visited

    delayed = []

    def slow(route):
        import time
        delayed.append(route.request.url)
        time.sleep(5)                              # past the worker's 4 s navigation timeout
        route.continue_()
    context.route("**/pair", slow)
    seen = _watch(page, context)
    page.goto(f"{base}/pair#{FRAG}", wait_until="commit", timeout=60_000)
    page.get_by_text("Open this in the TIX app").wait_for(timeout=60_000)
    assert delayed, "the worker's fetch of /pair was not delayed (PW_EXPERIMENTAL_SERVICE_WORKER_NETWORK_EVENTS)"
    assert page.url == f"{base}/pair"
    _assert_no_key(seen, page)


def test_an_offline_pair_page_opens_the_pair_shell_and_leaks_nothing(phone_page, context):
    page = phone_page("light")
    base = page.url.split("/", 3)[0] + "//" + page.url.split("/", 3)[2]
    page.wait_for_function("async () => !!(await caches.match('/pair'))")   # precached by the worker
    seen = _watch(page, context)
    context.set_offline(True)
    page.goto(f"{base}/pair#{FRAG}")
    page.get_by_text("Open this in the TIX app").wait_for()
    assert page.url == f"{base}/pair"
    _assert_no_key(seen, page)
    assert page.evaluate(PAIRS_COUNT) == 0
