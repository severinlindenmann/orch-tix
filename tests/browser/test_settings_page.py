"""The Settings page (spec §15): an end-to-end encrypted Deepgram API key."""
import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

KEY = "0123456789abcdef0123456789abcdef01234567"      # test value in Deepgram's format, not a real key
HOSTILE = "<img src=x onerror=window.__pwned=1>"
CONFLICT = "Settings changed elsewhere — reloaded, please retry"


def ready(page):
    """Let the startup requests (keys, GET /api/settings) settle before touching the page."""
    page.wait_for_load_state("networkidle")


def open_settings(page, live_server):
    page.goto(live_server.url + "/settings")
    ready(page)
    expect(page.locator("#deepgram-status")).not_to_have_text("Loading…")


def save_key(page, value):
    page.locator("#deepgram-key").fill(value)
    with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/api/settings")) as resp:
        page.locator("#deepgram-save").click()
    return resp.value


def storage_dump(page) -> str:
    return page.evaluate("() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)])")


def test_save_reload_reveal_remove(ui_page, live_server, sim):
    logs = []
    ui_page.on("console", lambda m: logs.append(m.text))
    sim.put_settings({"future_setting": {"keep": True}})          # a key this page doesn't know
    open_settings(ui_page, live_server)
    status = ui_page.locator("#deepgram-status")
    expect(status).to_have_text("Not set")
    expect(ui_page.locator("#deepgram-actions")).to_be_hidden()
    key_input = ui_page.locator("#deepgram-key")
    assert key_input.get_attribute("type") == "password"
    assert key_input.get_attribute("autocomplete") == "off"
    expect(ui_page.get_by_text("Stored end-to-end encrypted. Every approved device can read it; "
                               "after revoking a device, rotate the key at Deepgram.")).to_be_visible()

    # empty (after trimming) is refused without a request
    key_input.fill("   ")
    ui_page.locator("#deepgram-save").click()
    expect(ui_page.locator("#settings-msg")).to_contain_text("use Remove")
    assert sim.get_settings()[1] == 1

    assert save_key(ui_page, f"  {KEY}  ").status == 200
    expect(status).to_have_text(re.compile(r"^Configured ✓ · updated (just now|\d+ min ago)$"))
    expect(key_input).to_have_value("")
    obj, rev, _ = sim.get_settings()
    assert obj == {"deepgram_api_key": KEY, "future_setting": {"keep": True}}   # trimmed, unknown kept
    assert rev == 2

    ui_page.reload()
    ready(ui_page)
    expect(status).to_have_text(re.compile(r"^Configured ✓ · updated "))
    revealed = ui_page.locator("#deepgram-revealed")
    expect(revealed).to_be_hidden()
    assert KEY not in ui_page.content()                           # not in the DOM until Reveal
    ui_page.get_by_role("button", name="Reveal").click()
    expect(revealed).to_have_text(KEY)
    ui_page.get_by_role("button", name="Hide").click()
    expect(revealed).to_be_hidden()
    assert KEY not in ui_page.content()

    # Remove asks first; Cancel keeps the key
    ui_page.get_by_role("button", name="Remove").click()
    dlg = ui_page.locator("dialog.confirm")
    expect(dlg).to_be_visible()
    dlg.get_by_role("button", name="Cancel").click()
    assert sim.get_settings()[0]["deepgram_api_key"] == KEY
    ui_page.get_by_role("button", name="Remove").click()
    with ui_page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/api/settings")):
        ui_page.locator("dialog.confirm").get_by_role("button", name="Remove").click()
    expect(status).to_have_text("Not set")
    expect(ui_page.locator("#deepgram-actions")).to_be_hidden()
    assert sim.get_settings()[0] == {"future_setting": {"keep": True}}

    assert KEY not in storage_dump(ui_page)
    assert not any(KEY in line for line in logs), logs


def test_hostile_key_value_is_inert(ui_page, live_server, sim):
    sim.put_settings({"deepgram_api_key": HOSTILE})
    open_settings(ui_page, live_server)
    expect(ui_page.locator("#deepgram-status")).to_have_text(re.compile("^Configured ✓"))
    ui_page.get_by_role("button", name="Reveal").click()
    revealed = ui_page.locator("#deepgram-revealed")
    expect(revealed).to_have_text(HOSTILE)
    assert revealed.locator("img").count() == 0
    assert ui_page.locator("main img").count() == 0
    assert ui_page.evaluate("window.__pwned") is None


def test_conflict_reloads_and_asks_to_retry(ui_page, live_server, sim):
    sim.put_settings({"deepgram_api_key": "first-key-from-elsewhere"})
    open_settings(ui_page, live_server)
    expect(ui_page.locator("#deepgram-status")).to_have_text(re.compile("^Configured ✓"))
    # another browser writes between this page's load and its save
    sim.put_settings({"deepgram_api_key": "second-key-from-elsewhere", "other": "x"})
    assert save_key(ui_page, KEY).status == 409
    msg = ui_page.locator("#settings-msg")
    expect(msg).to_have_text(CONFLICT)
    expect(ui_page.locator("#deepgram-key")).to_have_value(KEY)     # kept for the retry
    assert sim.get_settings()[0]["deepgram_api_key"] == "second-key-from-elsewhere"
    # the page reloaded rev 2, so the retry wins and keeps the other browser's extra key
    assert save_key(ui_page, KEY).status == 200
    expect(msg).to_be_hidden()
    assert sim.get_settings()[0] == {"deepgram_api_key": KEY, "other": "x"}


def test_undecryptable_settings_block_saving_until_reset(ui_page, live_server, sim):
    # a valid-looking envelope under the wrong key: the page must not overwrite what it can't read
    other = sim.s.b64u(sim.s.seal(bytes(32), b'{"deepgram_api_key":"unreadable-000000"}', sim.s.AAD_SETTINGS))
    sim._check(sim.request("PUT", "/api/settings", json={"enc_settings": other, "rev": 0}), 200)
    reset = ui_page.locator("#settings-reset")
    sim.put_settings({"deepgram_api_key": KEY}, rev=1)    # readable settings: no Reset button
    open_settings(ui_page, live_server)
    expect(reset).to_be_hidden()
    sim._check(sim.request("PUT", "/api/settings", json={"enc_settings": other, "rev": 2}), 200)

    ui_page.reload()
    ready(ui_page)
    expect(ui_page.locator("#deepgram-status")).to_have_text("Couldn't decrypt")
    expect(ui_page.locator("#settings-msg")).to_contain_text("Couldn't decrypt the settings")
    expect(ui_page.locator("#deepgram-save")).to_be_disabled()
    expect(reset).to_be_visible()

    # Cancel changes nothing
    reset.click()
    dlg = ui_page.locator("dialog.confirm")
    expect(dlg).to_contain_text("This replaces the unreadable settings with empty ones; "
                                "you'll need to re-enter your Deepgram key")
    dlg.get_by_role("button", name="Cancel").click()
    assert sim.request("GET", "/api/settings").json()["rev"] == 3

    reset.click()
    with ui_page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/api/settings")) as resp:
        ui_page.locator("dialog.confirm").get_by_role("button", name="Reset settings").click()
    assert resp.value.status == 200
    expect(ui_page.locator("#deepgram-status")).to_have_text("Not set")
    expect(reset).to_be_hidden()
    expect(ui_page.locator("#settings-msg")).to_be_hidden()
    assert sim.get_settings()[:2] == ({}, 4)

    assert save_key(ui_page, KEY).status == 200
    expect(ui_page.locator("#deepgram-status")).to_have_text(re.compile("^Configured ✓"))
    assert sim.get_settings()[0] == {"deepgram_api_key": KEY}


def test_nav_links_settings_on_every_page(ui_page, live_server):
    for path in ("/files", "/settings"):
        ui_page.goto(live_server.url + path)
        ready(ui_page)
        link = ui_page.locator('nav[aria-label="Main"] a[href="/settings"]')
        expect(link).to_have_count(1)
        expect(link).to_be_visible()
    ui_page.goto(live_server.url + "/files")
    ready(ui_page)
    ui_page.locator('nav[aria-label="Main"] a[href="/settings"]').click()
    ui_page.wait_for_url(live_server.url + "/settings")
    ready(ui_page)
    expect(ui_page.get_by_role("heading", name="Settings")).to_be_visible()


def test_settings_needs_login(page, live_server, sim):
    page.goto(live_server.url + "/settings")
    page.wait_for_url(re.compile(r"/login\?next=%2Fsettings$"))
    page.locator("#passphrase").fill(sim.passphrase)
    page.get_by_role("button", name="Log in").click()
    page.wait_for_url(live_server.url + "/settings", timeout=30_000)
    ready(page)
    expect(page.locator("#deepgram-status")).to_have_text("Not set")


@pytest.mark.parametrize("path", ["/settings", "/files"])
def test_phone_width(browser, live_server, sim, path):
    from .conftest import login_ui
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    try:
        page = ctx.new_page()
        login_ui(page, live_server.url, sim.passphrase)
        page.goto(live_server.url + path)
        ready(page)
        assert page.evaluate("document.documentElement.scrollWidth") <= 390
        expect(page.locator('nav[aria-label="Main"] a[href="/settings"]')).to_be_visible()
        if path == "/settings":
            card = page.locator(".settings-card").first.bounding_box()
            assert card["x"] >= 0 and card["x"] + card["width"] <= 390
            page.locator("#advanced > summary").click()          # Deepgram sits in the Advanced fold on a phone
            assert save_key(page, KEY).status == 200
            expect(page.locator("#deepgram-status")).to_have_text(re.compile("^Configured ✓"))
            save = page.locator("#deepgram-save").bounding_box()
            assert save["x"] + save["width"] <= 390
    finally:
        ctx.close()
