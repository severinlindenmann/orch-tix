"""The redesign (spec §18, Task 30): the phone's full-screen file view and its history entry, the
upload bottom sheet, the desktop detail pane and keyboard, the tab bar / sidebar on every signed-in
page, no sideways scrolling, and self-hosted fonts with no third-party request."""
import base64
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect

from .conftest import login_ui

pytestmark = pytest.mark.browser

PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1024, "height": 720}
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
APP_PAGES = ["/files", "/settings"]
SPY_UPLOADS = """() => {
  window.__metas = [];
  const orig = window.fetch;
  window.fetch = (input, init) => {
    if (String(input).endsWith('/api/files') && init && init.body instanceof FormData) {
      window.__metas.push(JSON.parse(init.body.get('meta')));
    }
    return orig(input, init);
  };
}"""


def ready(page):
    page.wait_for_load_state("networkidle")


@pytest.fixture
def phone(browser, live_server, sim):
    ctx = browser.new_context(viewport=PHONE)
    page = ctx.new_page()
    login_ui(page, live_server.url, sim.passphrase)
    yield page
    ctx.close()


@pytest.fixture
def desk(browser, live_server, sim):
    ctx = browser.new_context(viewport=DESKTOP)
    page = ctx.new_page()
    login_ui(page, live_server.url, sim.passphrase)
    yield page
    ctx.close()


def row(page, file_id):
    return page.locator(f'#file-list .frow[data-id="{file_id}"]')


# ---- the phone's file view


def test_phone_file_view_opens_full_screen_and_back_closes_it(phone, live_server, sim):
    f = sim.upload("notes.txt", b"hello phone", note="read me")
    phone.reload()
    ready(phone)
    row(phone, f["id"]).click()
    view = phone.locator("#detail.is-open")
    expect(view).to_be_visible()
    assert phone.url == f"{live_server.url}/files?f={f['id']}"
    expect(view.locator(".detail-name")).to_have_text("notes.txt")
    expect(view.locator(".detail-note")).to_have_text("read me")
    expect(view.locator("pre.preview-pre")).to_have_text("hello phone")
    expect(view.locator(".pill-exp")).to_have_text("Expires in 7 days")
    expect(view.locator(".pill-new")).to_be_visible()
    # It covers the tab bar and the dock; its own bottom bar holds the actions.
    bar = view.locator(".detail-actions").bounding_box()
    assert abs(bar["y"] + bar["height"] - PHONE["height"]) < 1
    for name in ("Mark as done", "Download", "Delete"):
        expect(view.get_by_role("button", name=name, exact=True)).to_be_visible()
    expect(view.get_by_role("button", name="Copy", exact=True)).to_be_hidden()  # in the menu on a phone

    # The browser's back button closes it (it was its own history entry) ...
    phone.go_back()
    expect(phone.locator("#detail.is-open")).to_have_count(0)
    expect(phone.locator("#detail")).to_be_hidden()
    assert phone.url == live_server.url + "/files"
    # ... and so does the view's own back control, which steps the history back too.
    row(phone, f["id"]).click()
    expect(phone.locator("#detail.is-open")).to_be_visible()
    phone.get_by_role("button", name="Back to files").click()
    expect(phone.locator("#detail.is-open")).to_have_count(0)
    assert phone.url == live_server.url + "/files"
    phone.go_forward()  # the entry is still there: forward reopens the file
    expect(phone.locator("#detail.is-open .detail-name")).to_have_text("notes.txt")


def test_phone_file_view_actions_work(phone, live_server, sim):
    phone.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live_server.url)
    f = sim.upload("act.md", b"# act\n")
    phone.reload()
    ready(phone)
    row(phone, f["id"]).click()
    view = phone.locator("#detail.is-open")
    expect(phone.frame_locator("#detail iframe.preview-md").locator("h1")).to_have_text("act")

    with phone.expect_download() as info:
        view.get_by_role("button", name="Download", exact=True).click()
    assert info.value.suggested_filename == "act.md"
    assert Path(info.value.path()).read_bytes() == b"# act\n"

    view.get_by_role("button", name="More actions").click()
    menu = view.get_by_role("menu")
    expect(menu.get_by_role("menuitem")).to_have_text(["Copy", "Copy ID", "Change expiry", "Share link"])
    menu.get_by_role("menuitem", name="Copy", exact=True).click()
    expect(phone.locator(".toast").last).to_have_text("Copied")
    assert phone.evaluate("navigator.clipboard.readText()") == "# act\n"
    expect(menu).to_be_hidden()

    view.get_by_role("button", name="Mark as done", exact=True).click()
    expect(view.get_by_role("button", name="Mark as not done", exact=True)).to_be_visible()
    assert sim.get_file(f["id"])["acked_at"] is not None

    view.get_by_role("button", name="Delete", exact=True).click()
    phone.locator("dialog.confirm").get_by_role("button", name="Delete").click()
    expect(phone.locator("#detail.is-open")).to_have_count(0)
    assert phone.url == live_server.url + "/files"
    assert phone.request.get(f"{live_server.url}/api/files/{f['id']}").status == 410


def test_a_file_url_opens_the_view_directly(phone, live_server, sim):
    f = sim.upload("deep.txt", b"deep link")
    phone.goto(f"{live_server.url}/?f={f['id']}")
    ready(phone)
    view = phone.locator("#detail.is-open")
    expect(view.locator(".detail-name")).to_have_text("deep.txt")
    view.get_by_role("button", name="Back to files").click()  # no entry of ours to step back to
    expect(phone.locator("#detail.is-open")).to_have_count(0)
    assert phone.url == live_server.url + "/files"


def test_share_link_menu_item_runs_the_hook(desk, sim):
    f = sim.upload("s.txt", b"s")
    desk.reload()
    ready(desk)
    row(desk, f["id"]).click()
    desk.get_by_role("button", name="More actions").click()
    expect(desk.locator('#file-menu [data-action="share-link"]')).to_be_visible()  # wired by Task 32 (spec §17)
    desk.keyboard.press("Escape")
    desk.evaluate("async () => { (await import('/static/js/fileview.js')).fileViewHooks.shareLink = (f) => { window.__shared = f.id; }; }")
    row(desk, f["id"]).click()
    desk.get_by_role("button", name="More actions").click()
    desk.get_by_role("menuitem", name="Share link").click()
    assert desk.evaluate("window.__shared") == f["id"]
    assert desk.locator("#note-btn").is_visible()   # wired by Task 31 (spec §16)
    assert desk.locator("#outbox-slot").count() == 1


# ---- the upload bottom sheet


def test_phone_upload_sheet_defaults_to_seven_days(phone, sim, tmp_path):
    path = tmp_path / "IMG_4821.png"
    path.write_bytes(PNG_1PX)
    ready(phone)
    phone.evaluate(SPY_UPLOADS)
    phone.locator("#dock").get_by_role("button", name="Upload").click()
    sheet = phone.locator("dialog.upload-sheet")
    expect(sheet.get_by_role("heading", name="Share")).to_be_visible()
    box = sheet.bounding_box()
    assert box["x"] == 0 and abs(box["width"] - PHONE["width"]) < 1
    assert abs(box["y"] + box["height"] - PHONE["height"]) < 1  # anchored to the bottom
    expect(sheet.get_by_role("button", name="Encrypt and share")).to_be_disabled()
    sheet.locator("#upload-input").set_input_files(str(path))
    expect(sheet.locator("img.upload-thumb")).to_be_visible()
    expect(sheet.locator("#upload-name-0")).to_have_value("IMG_4821.png")
    expect(sheet.locator("#upload-ttl [aria-pressed='true']")).to_have_text("7 days")
    sheet.locator("#upload-note").fill("check this")
    with phone.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/files")) as resp:
        sheet.get_by_role("button", name="Encrypt and share").click()
    assert resp.value.status == 201
    assert [m["ttl"] for m in phone.evaluate("window.__metas")] == ["7d"]
    expect(phone.locator("#file-list .frow", has_text="IMG_4821.png").locator(".exp")).to_have_text("7d")


# ---- the desktop detail pane and keyboard


def test_desktop_selection_fills_the_pane_and_the_keyboard_moves_it(desk, sim):
    a = sim.upload("a.txt", b"alpha")
    b = sim.upload("b.txt", b"bravo")
    c = sim.upload("c.txt", b"charlie")
    desk.reload()
    ready(desk)
    pane = desk.get_by_role("region", name="Selected file")
    expect(pane).to_be_visible()
    expect(pane.locator(".detail-empty")).to_be_visible()
    # The three columns: sidebar, list, pane; no tab bar and no floating dock.
    side = desk.locator(".sidebar").bounding_box()
    lst = desk.locator(".list-col").bounding_box()
    pbox = pane.bounding_box()
    assert side["x"] == 0 and lst["x"] >= side["x"] + side["width"] - 1 and pbox["x"] >= lst["x"] + lst["width"] - 1
    assert abs(side["height"] - DESKTOP["height"]) < 1

    row(desk, b["id"]).click()
    expect(pane.locator(".detail-name")).to_have_text("b.txt")
    expect(pane.locator("pre.preview-pre")).to_have_text("bravo")
    for name in ("Mark as done", "Copy", "Download"):
        expect(pane.get_by_role("button", name=name, exact=True)).to_be_visible()
    expect(pane.get_by_role("button", name="Delete", exact=True)).to_be_hidden()  # in the menu on a desktop

    # "/" focuses the search box, wherever the focus was.
    desk.locator("body").click(position={"x": 5, "y": 700})
    desk.keyboard.press("/")
    expect(desk.locator("#search")).to_be_focused()
    # ↓/↑ move the selection on from the selected row (newest first: c, b, a); Enter opens it.
    desk.keyboard.press("ArrowDown")
    expect(row(desk, a["id"])).to_be_focused()
    expect(row(desk, a["id"])).to_have_class(re.compile(r"\bis-selected\b"))
    desk.keyboard.press("ArrowDown")  # stays on the last row
    expect(row(desk, a["id"])).to_be_focused()
    expect(pane.locator(".detail-name")).to_have_text("b.txt")  # moving doesn't open
    desk.keyboard.press("Enter")
    expect(pane.locator(".detail-name")).to_have_text("a.txt")
    expect(row(desk, a["id"])).to_have_attribute("aria-current", "true")
    for _ in range(3):
        desk.keyboard.press("ArrowUp")  # b, c, then stays on the first row
    expect(row(desk, c["id"])).to_be_focused()
    expect(row(desk, b["id"])).not_to_have_class(re.compile(r"\bis-selected\b"))
    desk.keyboard.press("Enter")
    expect(pane.locator(".detail-name")).to_have_text("c.txt")

    # Typing "/" inside the search box is just a character, and Escape closes the pane.
    desk.locator("#search").fill("")
    desk.locator("#search").type("a/")
    expect(desk.locator("#search")).to_have_value("a/")
    desk.locator("#search").fill("")
    row(desk, c["id"]).focus()
    desk.keyboard.press("Escape")
    expect(pane.locator(".detail-empty")).to_be_visible()


def test_device_and_project_filters_are_desktop_only(browser, live_server, sim, tmp_path):
    from .cli import make_repo, onboard_approved, run_cli
    repo = make_repo(tmp_path / "r")
    onboard_approved(sim, live_server.url, repo, "laptop", "proj")
    (repo / "x.txt").write_text("from the cli")
    r = run_cli(repo, live_server.url, "share", "x.txt")
    assert r.returncode == 0, r.stdout + r.stderr
    sim.upload("web.txt", b"w")
    for viewport, visible in ((DESKTOP, True), (PHONE, False)):
        ctx = browser.new_context(viewport=viewport)
        page = ctx.new_page()
        login_ui(page, live_server.url, sim.passphrase)
        ready(page)
        btn = page.get_by_role("button", name="Filter by device or project")
        if visible:
            btn.click()
            page.locator("#filter-project").select_option(label="proj")
            expect(page.locator("#file-list .frow")).to_have_count(1)
            expect(page.locator("#file-list .frow")).to_contain_text("x.txt")
        else:
            expect(btn).to_be_hidden()
        ctx.close()


# ---- chrome on every page


@pytest.mark.parametrize("path", APP_PAGES)
def test_tab_bar_on_every_signed_in_page_at_390(phone, live_server, path):
    phone.goto(live_server.url + path)
    ready(phone)
    nav = phone.get_by_role("navigation", name="Main")
    expect(nav).to_be_visible()
    expect(nav.get_by_role("link")).to_have_text(["Needs you", "Tickets", "Files", "Settings"])
    box = nav.bounding_box()
    assert abs(box["y"] + box["height"] + 6 - PHONE["height"]) < 2  # fixed to the bottom (6 px padding)
    current = nav.locator('[aria-current="page"]')
    expect(current).to_have_attribute("href", path)
    for link in nav.get_by_role("link").all():
        assert link.bounding_box()["height"] >= 44
    expect(phone.locator(".sidebar-foot")).to_be_hidden()


@pytest.mark.parametrize("path", APP_PAGES)
def test_sidebar_on_every_signed_in_page_on_desktop(desk, live_server, path):
    desk.goto(live_server.url + path)
    ready(desk)
    side = desk.locator(".sidebar")
    expect(side.get_by_role("navigation", name="Main")).to_be_visible()
    expect(side.get_by_role("button", name="Onboard device")).to_be_visible()
    expect(side.locator("#browser-name")).to_be_visible()
    expect(side.get_by_role("button", name="Sign out", exact=True)).to_be_visible()
    assert side.bounding_box()["width"] == 208


def test_settings_on_a_phone_offers_rename_and_sign_out(phone, live_server):
    phone.goto(live_server.url + "/settings")
    ready(phone)
    card = phone.get_by_role("region", name="This browser")
    expect(card.locator(".browser-name-value")).to_have_text(phone.evaluate(
        "async () => (await import('/static/js/browsername.js')).deriveBrowserName(navigator)"))
    card.get_by_role("button", name="Sign out", exact=True).click()
    phone.wait_for_url(live_server.url + "/login")


@pytest.mark.parametrize("path", ["/login", "/setup"])
def test_signed_out_pages_have_no_app_nav(page, live_server, sim, path):
    page.goto(live_server.url + path)
    ready(page)
    expect(page.get_by_role("navigation")).to_have_count(0)


def _open_states(page, live_server, sim, path):
    """Yields after each state worth checking on `path`: the page, and on / the file view and the sheet."""
    page.goto(live_server.url + path)
    ready(page)
    yield "page"
    if path != "/":
        return
    page.locator("#file-list .frow").first.click()
    ready(page)
    yield "file view"
    page.keyboard.press("Escape")
    page.locator("#upload-btn").click()
    yield "sheet"
    page.keyboard.press("Escape")


@pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=["390", "1024"])
def test_no_horizontal_scroll_on_any_page(browser, live_server, sim, viewport):
    sim.upload("a-rather-long-file-name-that-keeps-going-and-going-" + "x" * 80 + ".md",
               b"# " + b"y" * 400, note="n" * 300)
    ctx = browser.new_context(viewport=viewport)
    page = ctx.new_page()
    for path in ["/login", "/setup"]:
        for state in _open_states(page, live_server, sim, path):
            assert page.evaluate("document.documentElement.scrollWidth") <= viewport["width"], (path, state)
    login_ui(page, live_server.url, sim.passphrase)
    for path in APP_PAGES:
        for state in _open_states(page, live_server, sim, path):
            width = page.evaluate("Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)")
            assert width <= viewport["width"], (path, state, width)
    ctx.close()


# ---- fonts


def test_fonts_load_from_static_and_nothing_leaves_the_origin(browser, live_server, sim):
    ctx = browser.new_context(viewport=PHONE)
    page = ctx.new_page()
    urls = []
    page.on("request", lambda r: urls.append(r.url))
    login_ui(page, live_server.url, sim.passphrase)
    sim.upload("f.txt", b"x")
    for path in ["/files", "/settings", "/login"]:
        page.goto(live_server.url + path)
        ready(page)
        page.evaluate("document.fonts.ready.then(() => true)")
    origin = urlparse(live_server.url).netloc
    foreign = [u for u in urls if urlparse(u).scheme in ("http", "https") and urlparse(u).netloc != origin]
    assert foreign == []
    fonts = {urlparse(u).path for u in urls if u.endswith(".woff2")}
    assert fonts and all(p.startswith("/static/fonts/") for p in fonts), fonts
    assert {"/static/fonts/manrope-latin-wght.woff2", "/static/fonts/figtree-latin-wght.woff2"} <= fonts
    assert page.evaluate("document.fonts.check('16px Manrope') && document.fonts.check('16px Figtree')")
    family = page.evaluate("getComputedStyle(document.body).fontFamily")
    assert family.startswith("Manrope"), family
    ctx.close()


# ---- fix round 1: tap targets, keyboard picker, the modal file view, ?f= validation

# Every visible control's height. The file input is left out on purpose: it is a 1 px sr-only
# element that the "Choose files" button opens; it is never tapped.
SMALL_CONTROLS = """(root) => [...(root || document).querySelectorAll('button, a[href], input, select, textarea, summary, [role="menuitem"]')]
  .filter((n) => n.checkVisibility() && !n.closest('[inert]') && !(n.type === 'file' && n.classList.contains('sr-only')))
  .map((n) => [n.outerHTML.slice(0, 90), n.getBoundingClientRect().height])
  .filter(([, h]) => h < 44)"""


def test_every_phone_control_is_at_least_44px(phone, live_server, sim, tmp_path):
    f = sim.upload("t.json", b'{"a": 1}', note="a note")
    memo = sim.upload("m.mp3", b"\xff\xfb" + b"\0" * 400)
    sim.set_transcript(memo["id"], {"text": "hi", "language": "en", "model": "nova-3",
                                    "created_at": "2026-09-24T12:00:00Z", "by": "x"})
    phone.reload()
    ready(phone)
    assert phone.evaluate(SMALL_CONTROLS) == [], "files"
    row(phone, f["id"]).click()
    ready(phone)
    expect(phone.locator("#detail pre.preview-pre")).to_be_visible()  # the JSON Raw toggle is there too
    assert phone.evaluate(SMALL_CONTROLS) == [], "file view (json)"
    phone.get_by_role("button", name="More actions").click()
    assert phone.evaluate(SMALL_CONTROLS) == [], "file view menu"
    phone.keyboard.press("Escape")
    phone.go_back()
    row(phone, memo["id"]).click()
    ready(phone)
    expect(phone.locator("#detail section.transcript")).to_be_visible()
    assert phone.evaluate(SMALL_CONTROLS) == [], "file view (audio, transcript)"
    phone.go_back()
    phone.locator("#dock").get_by_role("button", name="Upload").click()
    assert phone.evaluate(SMALL_CONTROLS) == [], "sheet, empty"
    path = tmp_path / "x.txt"
    path.write_text("x")
    phone.locator("#upload-input").set_input_files(str(path))
    assert phone.evaluate(SMALL_CONTROLS) == [], "sheet, with a file"
    phone.keyboard.press("Escape")
    for page_path in ("/settings",):
        phone.goto(live_server.url + page_path)
        ready(phone)
        assert phone.evaluate(SMALL_CONTROLS) == [], page_path


def test_upload_sheet_file_picker_works_by_keyboard_alone(desk, tmp_path):
    # Listen from the start: file chooser interception is then on well before Enter is pressed.
    # (Registered only around the key press, it could lose the race under load; the native picker
    # then opened and was dismissed.)
    choosers = []
    desk.on("filechooser", lambda fc: choosers.append(fc))
    ready(desk)
    path = tmp_path / "kb.txt"
    path.write_text("keyboard")
    desk.locator("#upload-btn").focus()
    desk.keyboard.press("Enter")
    sheet = desk.locator("dialog.upload-sheet")
    expect(sheet).to_be_visible()
    pick = sheet.get_by_role("button", name="Choose files")
    expect(pick).to_be_focused()  # focused when the sheet opens with no file chosen
    desk.keyboard.press("Shift+Tab")
    desk.keyboard.press("Tab")
    expect(pick).to_be_focused()  # and reachable with Tab
    desk.keyboard.press("Enter")
    for _ in range(100):
        if choosers:
            break
        desk.wait_for_timeout(50)
    assert choosers, "Enter on Choose files opened no file chooser"
    choosers[0].set_files(str(path))
    expect(sheet.locator("#upload-name-0")).to_have_value("kb.txt")
    expect(sheet.get_by_role("button", name="Encrypt and share")).to_be_enabled()


def test_dismissing_the_file_picker_keeps_the_upload_sheet_open(desk):
    ready(desk)
    desk.locator("#upload-btn").click()
    sheet = desk.locator("dialog.upload-sheet")
    expect(sheet).to_be_visible()
    # What Chrome fires on the input when the user closes the picker without choosing: it bubbles.
    desk.evaluate("() => document.getElementById('upload-input').dispatchEvent(new Event('cancel', { bubbles: true }))")
    expect(sheet).to_be_visible()
    desk.keyboard.press("Escape")  # the dialog's own cancel still closes it
    expect(sheet).to_have_count(0)


def test_phone_file_view_is_a_modal_and_tab_stays_inside(phone, sim):
    f = sim.upload("modal.txt", b"modal")
    phone.reload()
    ready(phone)
    row(phone, f["id"]).click()
    view = phone.locator("#detail")
    expect(view).to_have_attribute("role", "dialog")
    expect(view).to_have_attribute("aria-modal", "true")
    for sel in (".list-col", "#dock", ".sidebar"):
        assert phone.locator(sel).evaluate("n => n.inert"), sel
    expect(view.locator("pre.preview-pre")).to_have_text("modal")
    for _ in range(20):
        phone.keyboard.press("Tab")
        assert phone.evaluate("() => document.getElementById('detail').contains(document.activeElement)")
    for _ in range(6):
        phone.keyboard.press("Shift+Tab")
        assert phone.evaluate("() => document.getElementById('detail').contains(document.activeElement)")

    # Growing to a desktop turns the view into the pane: no longer a modal, nothing inert.
    phone.set_viewport_size(DESKTOP)
    expect(view).not_to_have_attribute("role", "dialog")
    assert not phone.locator(".list-col").evaluate("n => n.inert")
    phone.set_viewport_size(PHONE)
    expect(view).to_have_attribute("aria-modal", "true")
    view.get_by_role("button", name="Back to files").click()
    expect(view).not_to_have_attribute("aria-modal", "true")
    for sel in (".list-col", "#dock", ".sidebar"):
        assert not phone.locator(sel).evaluate("n => n.inert"), sel


@pytest.mark.parametrize("bad", ["<script>", "FILE7x", "../settings", "7;1", "file-7", ""])
def test_invalid_file_ref_is_ignored_and_dropped(desk, live_server, bad):
    urls = []
    desk.on("request", lambda r: urls.append(r.url))
    desk.goto(f"{live_server.url}/?f={bad}")
    ready(desk)
    assert desk.url == live_server.url + "/files"
    expect(desk.locator("#detail.is-open")).to_have_count(0)
    assert not [u for u in urls if re.search(r"/api/files/[^?]", u)], urls


def test_a_file_ref_by_bare_number_opens_it(desk, live_server, sim):
    f = sim.upload("seven.txt", b"7")
    desk.goto(f"{live_server.url}/?f={f['n']}")
    ready(desk)
    expect(desk.locator("#detail.is-open .detail-name")).to_have_text("seven.txt")


def test_a_link_to_a_deleted_file_shows_it_as_deleted(desk, live_server, sim):
    f = sim.upload("gone.txt", b"gone")
    for _ in range(51):  # push it off the first page, so openWanted has to fetch it
        sim.upload("filler.txt", b"x")
    sim.delete(f["id"])
    desk.goto(f"{live_server.url}/?f={f['id']}")
    ready(desk)
    view = desk.locator("#detail.is-open")
    expect(view.locator(".detail-name")).to_have_text("Deleted file")
    expect(view).to_contain_text(f"{f['id']} was deleted.")
    expect(view.locator(".detail-actions")).to_be_hidden()
    assert desk.locator(".banner-decrypt").count() == 0
