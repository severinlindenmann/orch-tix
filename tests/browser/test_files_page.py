"""Files page fixes carried in from the Task 18 review: reload race, tombstone-only pages, logout, hostile names."""
import re
import time

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

HOSTILE = "<img src=x onerror=window.__pwned=1>"
LOAD_KEYS = "async () => (await import('/static/js/keystore.js')).loadKeys()"


def rows(page):
    return page.locator("#file-list .frow")


def wait_for(cond, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def test_reload_during_load_more_drops_the_stale_page(ui_page, sim):
    for i in range(51):
        sim.upload(f"f{i}.txt", b"x")
    ui_page.reload()
    expect(rows(ui_page)).to_have_count(50)

    held = []
    ui_page.route(re.compile(r"/api/files\?.*before="), lambda route: held.append(route))
    ui_page.get_by_role("button", name="Load more").click()
    wait_for(lambda: (ui_page.wait_for_timeout(20), held)[1])

    # Something changed (an upload, a delete): the list restarts from the newest page.
    with ui_page.expect_response(lambda r: "/api/files?" in r.url and "before=" not in r.url):
        ui_page.evaluate("window.dispatchEvent(new CustomEvent('fs:files-changed'))")
    expect(rows(ui_page)).to_have_count(50)

    # Now the stale "page 2" response lands; it must not replace or join the fresh list.
    with ui_page.expect_response(lambda r: "before=" in r.url):
        held[0].continue_()
    ui_page.wait_for_timeout(500)
    expect(rows(ui_page)).to_have_count(50)
    expect(rows(ui_page).first).to_contain_text("FILE51")
    expect(ui_page.locator('#file-list [data-id="FILE1"]')).to_have_count(0)
    expect(ui_page.get_by_role("button", name="Load more")).to_be_visible()
    ui_page.unroute(re.compile(r"/api/files\?.*before="))
    ui_page.get_by_role("button", name="Load more").click()
    expect(rows(ui_page)).to_have_count(51)


def test_page_of_only_tombstones_fetches_on(ui_page, sim):
    keep = sim.upload("keep.md", b"# keep", note="the only live file")
    for i in range(50):
        f = sim.upload(f"gone{i}.txt", b"x")
        sim.delete(f["id"])
    ui_page.reload()
    expect(rows(ui_page)).to_have_count(1)
    expect(rows(ui_page).first).to_contain_text("keep.md")
    expect(rows(ui_page).first).to_contain_text(keep["id"])
    expect(ui_page.get_by_role("button", name="Load more")).to_be_hidden()
    expect(ui_page.locator("#empty")).to_be_hidden()


def test_only_tombstones_shows_the_empty_state(ui_page, sim):
    for i in range(51):
        f = sim.upload(f"gone{i}.txt", b"x")
        sim.delete(f["id"])
    ui_page.reload()
    expect(ui_page.locator("#empty")).to_be_visible()
    expect(ui_page.get_by_role("button", name="Load more")).to_be_hidden()


def test_logout_clears_indexeddb_and_lands_on_login(ui_page, live_server):
    assert ui_page.evaluate(LOAD_KEYS) is not None
    ui_page.get_by_role("button", name="Sign out", exact=True).click()
    ui_page.wait_for_url(live_server.url + "/login")
    assert ui_page.evaluate(LOAD_KEYS) is None


def test_logout_clears_keys_even_when_the_server_call_fails(ui_page, live_server):
    ui_page.route("**/api/logout", lambda route: route.fulfill(
        status=500, content_type="application/json", body='{"error":"internal","detail":"boom"}'))
    ui_page.get_by_role("button", name="Sign out", exact=True).click()
    ui_page.wait_for_url(live_server.url + "/login")
    assert ui_page.evaluate(LOAD_KEYS) is None


def test_logout_redirects_and_warns_when_clearing_keys_fails(ui_page, live_server):
    # (wrapped in an arrow: Playwright would call a bare function expression)
    ui_page.evaluate("() => { IDBFactory.prototype.open = () => { throw new DOMException('blocked', 'UnknownError'); }; }")
    ui_page.get_by_role("button", name="Sign out", exact=True).click()
    expect(ui_page.locator(".toast-error")).to_contain_text("Couldn't clear the keys, queued uploads or cached lists stored in this browser")
    ui_page.wait_for_url(live_server.url + "/login")


def test_hostile_name_and_note_render_as_text(ui_page, sim):
    f = sim.upload(HOSTILE, b"x", note=HOSTILE)
    for width in (1280, 390):
        ui_page.set_viewport_size({"width": width, "height": 900})
        ui_page.reload()
        item = ui_page.locator(f'[data-id="{f["id"]}"]:visible')
        expect(item).to_have_count(1)
        expect(item.locator(".fname")).to_have_text(HOSTILE)
        expect(item.locator(".fnote")).to_have_text(HOSTILE)
        assert item.locator("img").count() == 0
    assert ui_page.locator("img").count() == 0
    assert ui_page.evaluate("window.__pwned") is None
