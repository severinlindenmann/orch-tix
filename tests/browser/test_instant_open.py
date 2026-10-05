"""Instant open (the phone PWA): the service worker answers a navigation from its cached shell and
refreshes it in the background (stale-while-revalidate), and Needs you renders the last-known lists
(the ciphertext GET /api/spaces and /api/mirrors bodies kept in IndexedDB "lists") before the network
answers. Offline the cached list stays with "Offline — as of HH:MM". A cached copy older than this
phone's high-water mark ("seen") is left out, never shown as a rollback, and never moves the mark."""
import re
import time

import pytest
from playwright.sync_api import expect

from .conftest import EXAMPLE_DOC, login_ui

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True)
def _chromium_only(browser_name):
    if browser_name != "chromium":
        pytest.skip("service worker offline emulation is Chromium-only here")


@pytest.fixture
def browser_context_args(browser_context_args):
    return {**browser_context_args, "service_workers": "allow"}


READ_LISTS = """() => new Promise((resolve, reject) => {
  const q = indexedDB.open("fileshare");
  q.onerror = () => reject(q.error);
  q.onsuccess = () => {
    const db = q.result;
    if (!db.objectStoreNames.contains("lists")) { db.close(); resolve(null); return; }
    const tx = db.transaction("lists");
    const keys = tx.objectStore("lists").getAllKeys();
    const vals = tx.objectStore("lists").getAll();
    tx.oncomplete = () => { db.close(); resolve(Object.fromEntries(keys.result.map((k, i) => [k, vals.result[i]]))); };
  };
})"""

READ_SEEN = """(id) => new Promise((resolve, reject) => {
  const q = indexedDB.open("fileshare");
  q.onerror = () => reject(q.error);
  q.onsuccess = () => {
    const db = q.result;
    const g = db.transaction("seen").objectStore("seen").get(id);
    g.onsuccess = () => { db.close(); resolve(g.result ?? null); };
  };
})"""


class Hold:
    """Holds every request to one URL (pages and the service worker) until release()."""

    def __init__(self, context, url):
        self.routes = []
        self.context = context
        self.match = lambda u: u == url
        context.route(self.match, lambda route: self.routes.append(route))

    def release(self, fulfill=None):
        # The held routes first: unroute() would let them through unchanged.
        for r in self.routes:
            if fulfill:
                fulfill(r)
            else:
                r.continue_()
        self.context.unroute(self.match)


def card(page, mirror):
    return page.locator(f'article.ncard[data-n="{mirror.n}"]')


def test_a_navigation_opens_from_the_cached_shell_and_refreshes_it_in_the_background(phone_page, mirror_with_question, context):
    page = phone_page("light")
    base = mirror_with_question.base
    hold = Hold(context, base + "/")                 # the server never answers the page itself
    t0 = time.monotonic()
    page.goto(base + "/", wait_until="commit", timeout=10_000)
    card(page, mirror_with_question).wait_for(timeout=3_000)
    assert time.monotonic() - t0 < 3.5, "the cached shell must answer well before the old 4 s timeout"
    assert hold.routes, "the worker revalidates the page in the background"

    def marked(route):
        r = route.fetch()
        route.fulfill(response=r, body=r.text().replace("</html>", "<!-- refreshed --></html>"))
    hold.release(marked)
    page.wait_for_function("""async () => {
      for (const name of await caches.keys()) {
        const hit = name.startsWith('shell-') && await (await caches.open(name)).match('/');
        if (hit && (await hit.text()).includes('<!-- refreshed -->')) return true;
      }
      return false;
    }""", timeout=10_000)


def test_needs_you_renders_the_cached_list_before_the_network_answers(phone_page, mirror_with_question, context):
    page = phone_page("light")
    base = mirror_with_question.base
    stored = page.evaluate(READ_LISTS)
    assert set(stored) == {"spaces", "mirrors"}
    # Ciphertext only: the label and the ticket title never reach the cache in the clear.
    raw = repr(stored)
    assert "Acme Energy" not in raw and EXAMPLE_DOC["title"] not in raw and "ISO 8601" not in raw
    assert [m["uuid"] for m in stored["mirrors"]["body"]["mirrors"]] == [mirror_with_question.uuid]
    hold = Hold(context, base + "/api/mirrors")
    page.reload()
    card(page, mirror_with_question).get_by_text("Acme Energy").wait_for()
    expect(page.locator("#needs-loading")).to_be_hidden()
    assert hold.routes, "the network answer is still pending"
    hold.release()
    expect(page.locator("#needs-asof")).to_be_hidden()
    expect(card(page, mirror_with_question)).to_be_visible()


def test_offline_open_shows_the_last_known_list_and_when_it_arrived(phone_page, mirror_with_question, context):
    page = phone_page("light")
    context.set_offline(True)
    try:
        page.reload()
        card(page, mirror_with_question).get_by_text("Acme Energy").wait_for()
        expect(page.locator("#needs-asof")).to_have_text(re.compile(r"^Offline — as of \d\d:\d\d$"))
        expect(page.locator("#needs-error")).to_be_hidden()
        expect(page.locator("#needs-loading")).to_be_hidden()
    finally:
        context.set_offline(False)


def test_offline_without_a_cache_keeps_the_error(phone_page, mirror_with_question, context):
    page = phone_page("light")
    page.evaluate("""() => new Promise((ok) => { const q = indexedDB.open("fileshare");
      q.onsuccess = () => { const tx = q.result.transaction("lists", "readwrite"); tx.objectStore("lists").clear();
        tx.oncomplete = () => { q.result.close(); ok(); }; }; })""")
    context.set_offline(True)
    try:
        page.reload()
        expect(page.locator("#needs-error")).to_contain_text("You're offline.")
        expect(page.locator("#needs-asof")).to_be_hidden()
    finally:
        context.set_offline(False)


def test_a_cached_copy_older_than_seen_is_left_out_never_a_rollback_and_never_lowers_seen(phone_page, mirror_with_question, context):
    page = phone_page("light")                       # the cached lists now hold mirror_rev 1
    base = mirror_with_question.base
    mirror_with_question.push(EXAMPLE_DOC, rev=2)
    page.goto(f"{base}/t/{mirror_with_question.n}")  # this phone opens rev 2: seen moves to 2
    page.get_by_role("radio", name="ISO 8601").wait_for()
    assert page.evaluate(READ_SEEN, f"{mirror_with_question.space}|{EXAMPLE_DOC['id']}") == {"gen": 1, "mirror_rev": 2}
    hold = Hold(context, base + "/api/mirrors")
    page.goto(base + "/")
    page.locator('#needs-list[data-from="cache"]').wait_for(state="attached")
    assert hold.routes                               # the server's list is still pending
    expect(page.locator("#needs-loading")).to_be_visible()   # a row was left out: still loading
    expect(page.get_by_text("The server sent an older copy")).to_have_count(0)
    expect(card(page, mirror_with_question)).to_have_count(0)
    expect(page.get_by_text("Nothing needs you right now")).to_have_count(0)
    assert page.evaluate(READ_SEEN, f"{mirror_with_question.space}|{EXAMPLE_DOC['id']}") == {"gen": 1, "mirror_rev": 2}
    hold.release()
    card(page, mirror_with_question).get_by_text("Acme Energy").wait_for()
    expect(page.get_by_text("The server sent an older copy")).to_have_count(0)
    assert page.evaluate(READ_SEEN, f"{mirror_with_question.space}|{EXAMPLE_DOC['id']}") == {"gen": 1, "mirror_rev": 2}


def test_sign_out_clears_the_cached_lists_and_a_signed_out_open_ends_on_login(page, live_server, sim, mirror_with_question):
    login_ui(page, live_server.url, sim.passphrase, then=None)
    page.evaluate("async () => { await navigator.serviceWorker.ready; }")
    page.reload()
    page.wait_for_function("() => navigator.serviceWorker.controller !== null")
    page.locator('#needs-list[data-from="server"]').wait_for(state="attached")   # the lists are stored
    page.goto(live_server.url + "/files")
    assert set(page.evaluate(READ_LISTS) or {}) == {"spaces", "mirrors"}
    page.get_by_role("button", name="Sign out", exact=True).click()
    page.wait_for_url(live_server.url + "/login")
    assert page.evaluate(READ_LISTS) == {}
    for path in ("/", f"/t/{mirror_with_question.n}", "/?view=tickets"):
        page.goto(live_server.url + path)            # the worker answers with the cached shell
        page.wait_for_url(re.compile(r"/login(\?|$)"))


def test_an_expired_session_with_keys_still_here_ends_on_login(phone_page, mirror_with_question, context):
    page = phone_page("light")
    base = mirror_with_question.base
    context.clear_cookies()                          # the session is gone; the keys and the lists are not
    page.goto(base + "/")
    page.wait_for_url(re.compile(r"/login\?next="))
    page.wait_for_function(f"({READ_LISTS})().then((l) => l && Object.keys(l).length === 0)")
    page.goto(f"{base}/t/{mirror_with_question.n}")
    page.wait_for_url(re.compile(r"/login"))


# ---- fix round 1

PUT_MARKER = """() => new Promise((ok, fail) => { const q = indexedDB.open("fileshare");
  q.onerror = () => fail(q.error);
  q.onsuccess = () => { const tx = q.result.transaction("lists", "readwrite");
    tx.objectStore("lists").put({ body: {}, at: 1 }, "marker"); tx.oncomplete = () => { q.result.close(); ok(); }; }; })"""


def sign_out(page, base):
    page.goto(base + "/files")
    page.get_by_role("button", name="Sign out", exact=True).click()
    page.wait_for_url(base + "/login")


def test_a_newer_builds_page_is_not_cached_by_the_old_worker_which_registers_the_new_one(phone_page, mirror_with_question, context):
    page = phone_page("light")
    base = mirror_with_question.base
    old = page.evaluate("async () => new URL((await navigator.serviceWorker.ready).active.scriptURL).searchParams.get('v')")

    def newer(route):
        r = route.fetch()
        route.fulfill(response=r, headers={**r.headers, "x-build": "NEW9"},
                      body=r.text().replace(f"?v={old}", "?v=NEW9").replace("</html>", "<!-- NEW9 --></html>"))
    context.route(lambda u: u == base + "/", newer)
    page.goto(base + "/")                              # the old shell answers; the refresh is the new build
    card(page, mirror_with_question).wait_for()
    page.wait_for_function("""async () => {
      const reg = await navigator.serviceWorker.getRegistration('/');
      return [reg?.installing, reg?.waiting, reg?.active].some((w) => w && w.scriptURL.includes('v=NEW9'));
    }""", timeout=15_000)
    assert not page.evaluate("""async () => {
      for (const name of await caches.keys()) {
        const hit = await (await caches.open(name)).match('/');
        if (hit && (await hit.text()).includes('<!-- NEW9 -->')) return true;
      }
      return false;
    }"""), "the new build's page never lands in the old build's cache"


def test_the_ticket_request_card_follows_the_servers_workspaces(phone_page, mirror_with_question, live_server, sim, context):
    from .conftest import make_desktop
    page = phone_page("light")
    base = mirror_with_question.base
    page.goto(base + "/?view=tickets")
    page.locator('#needs-list[data-from="server"]').wait_for(state="attached")   # the lists: one workspace
    make_desktop(live_server, sim, name="beta-desk", label="Beta Works")
    hold = Hold(context, base + "/api/mirrors")
    page.reload()
    page.locator('#needs-list[data-from="cache"]').wait_for(state="attached")
    expect(page.locator("#req-space option")).to_have_text(["Acme Energy"])
    page.locator("#req-title").fill("Typed before the server answered")
    hold.release()
    page.locator('#needs-list[data-from="server"]').wait_for(state="attached")
    expect(page.locator("#req-space option")).to_have_count(2)
    assert sorted(page.locator("#req-space option").all_inner_texts()) == ["Acme Energy", "Beta Works"]
    expect(page.locator("#req-title")).to_have_value("Typed before the server answered")
    expect(page.get_by_role("button", name="Send request")).to_be_enabled()


def test_signing_in_and_a_keyless_open_clear_lists_left_by_another_key(page, live_server, sim, mirror_with_question):
    base = live_server.url
    login_ui(page, base, sim.passphrase, then=None)
    page.evaluate("async () => { await navigator.serviceWorker.ready; }")
    sign_out(page, base)
    page.evaluate(PUT_MARKER)                          # no keys now; something left in the lists
    page.goto(base + "/")
    page.wait_for_url(re.compile(r"/login"))
    assert "marker" not in (page.evaluate(READ_LISTS) or {})
    page.evaluate(PUT_MARKER)
    page.locator("#passphrase").fill(sim.passphrase)
    page.get_by_role("button", name="Log in").click()
    page.wait_for_url(base + "/")
    page.locator('#needs-list[data-from="server"]').wait_for(state="attached")
    assert set(page.evaluate(READ_LISTS)) == {"spaces", "mirrors"}


def test_an_expired_session_on_the_ticket_page_ends_on_login(phone_page, mirror_with_question, context):
    page = phone_page("light")
    base = mirror_with_question.base
    page.goto(f"{base}/t/{mirror_with_question.n}")
    page.get_by_role("radio", name="ISO 8601").wait_for()
    context.clear_cookies()                            # keys still here
    page.goto(f"{base}/t/{mirror_with_question.n}")
    page.wait_for_url(re.compile(rf"/login\?next=%2Ft%2F{mirror_with_question.n}$"))
    assert page.evaluate(READ_LISTS) == {}


def test_an_expired_session_on_settings_ends_on_login(phone_page, mirror_with_question, context):
    page = phone_page("light")
    base = mirror_with_question.base
    page.wait_for_function("async () => !!(await caches.match('/settings'))")   # the cached Settings shell
    context.clear_cookies()
    page.goto(base + "/settings")
    page.wait_for_url(re.compile(r"/login\?next=%2Fsettings$"))


def test_rows_left_out_of_the_cached_list_are_counted_offline(phone_page, mirror_with_question, context):
    page = phone_page("light")                         # the cached lists hold mirror_rev 1
    base = mirror_with_question.base
    mirror_with_question.push(EXAMPLE_DOC, rev=2)
    page.goto(f"{base}/t/{mirror_with_question.n}")    # seen moves to 2
    page.get_by_role("radio", name="ISO 8601").wait_for()
    context.set_offline(True)
    try:
        page.goto(base + "/")
        expect(page.locator("#needs-asof")).to_have_text(re.compile(r"^Offline — as of \d\d:\d\d$"))
        expect(page.locator("#needs-skipped")).to_have_text("1 ticket not shown until you're back online.")
        expect(page.get_by_text("Nothing needs you right now")).to_have_count(0)
        expect(page.locator("#needs-loading")).to_be_hidden()
    finally:
        context.set_offline(False)
