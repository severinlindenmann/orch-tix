import re

import httpx
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

PASS = "correct horse battery staple"
NEW_PASS = "a different long passphrase"
SLOW = 30_000  # PBKDF2 at 600k iterations in headless Chromium


def _ui_setup(page, live_server):
    page.goto(live_server.url + "/setup")
    page.locator("#setup-code").fill(live_server.setup_code())
    page.locator("#new-pass").fill(PASS)
    page.locator("#new-pass2").fill(PASS)
    with page.expect_request("**/api/setup") as req:
        page.get_by_role("button", name="Create key and finish setup").click()
    assert req.value.post_data_json["mode"] == "new"
    key = page.locator("#recovery-key")
    expect(key).to_have_text(re.compile(r"^shrk(-[A-Z2-7]{5}){11}$"), timeout=SLOW)
    return key.inner_text()


PROBE_SEAL = """async () => {
  const c = await import('/static/js/crypto.js');
  const k = await (await import('/static/js/keystore.js')).loadKeys();
  return c.b64u(await c.seal(k.mk, new TextEncoder().encode('probe'), new Uint8Array()));
}"""
PROBE_OPEN = """async (env) => {
  const c = await import('/static/js/crypto.js');
  const k = await (await import('/static/js/keystore.js')).loadKeys();
  return new TextDecoder().decode(await c.open(k.mk, c.unb64u(env), new Uint8Array()));
}"""


def test_setup_stores_non_extractable_keys_and_shows_recovery_key(page, live_server):
    _ui_setup(page, live_server)
    cont = page.get_by_role("button", name="Continue")
    expect(cont).to_be_disabled()
    page.get_by_label("I stored the recovery key somewhere safe, offline").check()
    expect(cont).to_be_enabled()
    flags = page.evaluate("""async () => {
      const k = await (await import('/static/js/keystore.js')).loadKeys();
      return [k.mk.extractable, k.kek.extractable, k.keyVersion];
    }""")
    assert flags == [False, False, 1]
    assert httpx.get(live_server.url + "/api/kdf").json()["kdf_iterations"] == 600000


def test_setup_validates_passphrase_before_any_request(page, live_server):
    page.goto(live_server.url + "/setup")
    page.locator("#setup-code").fill("setup_whatever")
    page.locator("#new-pass").fill("short")
    page.locator("#new-pass2").fill("short")
    page.get_by_role("button", name="Create key and finish setup").click()
    expect(page.locator("#setup-error")).to_have_text("Use at least 12 characters.")
    page.locator("#new-pass").fill(PASS)
    page.locator("#new-pass2").fill(PASS + "x")
    page.get_by_role("button", name="Create key and finish setup").click()
    expect(page.locator("#setup-error")).to_have_text("Passphrases don't match.")


def test_wrong_setup_code_is_refused(page, live_server):
    page.goto(live_server.url + "/setup")
    page.locator("#setup-code").fill("setup_not-a-real-code-at-all-000000")
    page.locator("#new-pass").fill(PASS)
    page.locator("#new-pass2").fill(PASS)
    page.get_by_role("button", name="Create key and finish setup").click()
    expect(page.locator("#setup-error")).to_contain_text("wrong, expired or already used", timeout=SLOW)


def test_restore_keeps_the_same_master_key_and_rotates_the_passphrase(page, live_server):
    recovery = _ui_setup(page, live_server)
    probe = page.evaluate(PROBE_SEAL)
    page.get_by_label("I stored the recovery key somewhere safe, offline").check()
    page.get_by_role("button", name="Continue").click()

    page.goto(live_server.url + "/setup")
    expect(page.locator("#already-note")).to_be_visible()
    expect(page.locator("#restore-form")).to_be_visible()
    page.locator("#restore-code").fill(live_server.setup_code())
    i = len(recovery) // 2
    while not recovery[i].isalnum():
        i += 1
    typo = recovery[:i] + ("Q" if recovery[i] != "Q" else "R") + recovery[i + 1:]
    page.locator("#recovery").fill(typo)
    page.locator("#restore-pass").fill(NEW_PASS)
    page.locator("#restore-pass2").fill(NEW_PASS)
    page.get_by_role("button", name="Restore access").click()
    expect(page.locator("#restore-error")).to_contain_text("recovery key doesn't check out")

    page.locator("#recovery").fill(recovery)
    with page.expect_request("**/api/setup") as req:
        page.get_by_role("button", name="Restore access").click()
    assert req.value.post_data_json["mode"] == "restore"
    page.wait_for_url(live_server.url + "/", timeout=SLOW)
    page.goto(live_server.url + "/setup")
    assert page.evaluate(PROBE_OPEN, probe) == "probe"

    page.context.clear_cookies()
    page.goto(live_server.url + "/login")
    page.locator("#passphrase").fill(PASS)
    page.get_by_role("button", name="Log in").click()
    expect(page.locator("#login-error")).to_have_text("Wrong passphrase.", timeout=SLOW)
    page.locator("#passphrase").fill(NEW_PASS)
    page.get_by_role("button", name="Log in").click()
    page.wait_for_url(live_server.url + "/", timeout=SLOW)


def test_initialized_setup_page_offers_no_working_new_setup(page, live_server, sim):
    posted = []
    page.on("request", lambda r: posted.append(r.url) if r.method == "POST" else None)
    page.goto(live_server.url + "/setup")
    expect(page.locator("#already-note")).to_be_visible()
    expect(page.locator("#restore-form")).to_be_visible()
    expect(page.locator("#tab-new")).to_be_hidden()
    expect(page.locator("#setup-form")).to_be_hidden()
    page.locator("#tab-restore").focus()
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#setup-form")).to_be_hidden()
    expect(page.locator("#restore-form")).to_be_visible()
    # Even a forced submit of the hidden form must not post a fresh master key.
    page.evaluate("""(code) => {
      document.getElementById('setup-code').value = code;
      document.getElementById('new-pass').value = 'correct horse battery staple';
      document.getElementById('new-pass2').value = 'correct horse battery staple';
      document.getElementById('setup-form').requestSubmit();
    }""", live_server.setup_code())
    page.wait_for_timeout(1500)
    assert not any(u.endswith("/api/setup") for u in posted)
    expect(page.locator("#recovery-screen")).to_be_hidden()


def test_login_next_cannot_redirect_off_origin(page, live_server, sim):
    # The URL parser strips tab/newline, so "/\t/evil.com" used to resolve to https://evil.com/.
    page.goto(live_server.url + "/login?next=%2F%09%2Fevil.com")
    page.locator("#passphrase").fill(sim.passphrase)
    page.get_by_role("button", name="Log in").click()
    page.wait_for_url(live_server.url + "/", timeout=SLOW)


@pytest.mark.parametrize("kdf", [
    {"kdf_salt": "AAAAAAAAAAAAAAAAAAAAAA", "kdf_iterations": 1},
    {"kdf_salt": "AAAAAAAAAAAAAAAAAAAAAA", "kdf_iterations": 10_000_001},
    {"kdf_salt": "AAAAAAAAAAAAAAAAAAA", "kdf_iterations": 600000},  # 14 bytes
], ids=["too-few-iterations", "too-many-iterations", "short-salt"])
def test_login_refuses_unsafe_kdf_params_and_sends_nothing(page, live_server, sim, kdf):
    page.route("**/api/kdf", lambda route: route.fulfill(status=200, json=kdf))
    posted = []
    page.on("request", lambda r: posted.append(r.url) if r.method == "POST" else None)
    page.goto(live_server.url + "/login")
    page.locator("#passphrase").fill(sim.passphrase)
    page.get_by_role("button", name="Log in").click()
    expect(page.locator("#login-error")).to_contain_text("server returned unsafe key parameters")
    expect(page.get_by_role("button", name="Log in")).to_be_enabled()
    assert posted == []
