"""Public links in the web UI (spec §17, Task 32): the owner's Share link dialog and the file view's
Public links list, and the viewer at /p/<token>#<key> in a context with no session."""
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .conftest import login_ui

pytestmark = pytest.mark.browser

PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1024, "height": 720}
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c63"
    "6460f85f0f0002870180eb47ba920000000049454e44ae426082"
)
HOSTILE = '<img src=x onerror="window.__pwned=1">'
TRANSCRIPT = {"text": "Ship the release on Friday.", "language": "en", "model": "nova-3",
              "created_at": "2026-09-24T12:00:00Z", "by": "dev-a"}
# The effective tap target of every visible control: a checkbox counts by the label around it.
SMALL_CONTROLS = """(root) => [...(root || document).querySelectorAll('button, a[href], input, select, textarea, summary, [role="menuitem"]')]
  .filter((n) => n.checkVisibility() && !n.closest('[inert]'))
  .map((n) => [n.outerHTML.slice(0, 90), (n.type === 'checkbox' && n.closest('label') ? n.closest('label') : n).getBoundingClientRect().height])
  .filter(([, h]) => h < 44)"""


def ready(page):
    page.wait_for_load_state("networkidle")


@pytest.fixture
def desk(browser, live_server, sim):
    ctx = browser.new_context(viewport=DESKTOP)
    page = ctx.new_page()
    login_ui(page, live_server.url, sim.passphrase)
    yield page
    ctx.close()


@pytest.fixture
def phone(browser, live_server, sim):
    ctx = browser.new_context(viewport=PHONE)
    page = ctx.new_page()
    login_ui(page, live_server.url, sim.passphrase)
    yield page
    ctx.close()


@pytest.fixture
def stranger(browser):
    """A fresh context with no session and no cookies: someone who only has the link."""
    ctx = browser.new_context(viewport=PHONE)
    page = ctx.new_page()
    yield page
    ctx.close()


def row(page, file_id):
    return page.locator(f'#file-list .frow[data-id="{file_id}"]')


def open_file(page, file_id):
    page.reload()
    ready(page)
    row(page, file_id).click()
    ready(page)
    return page.locator("#detail")


def open_share(page):
    page.locator("#detail").get_by_role("button", name="More actions").click()
    page.get_by_role("menuitem", name="Share link").click()
    sheet = page.locator("dialog.link-sheet")
    expect(sheet).to_be_visible()
    return sheet


def link_key(url):
    return url.split("#", 1)[1]


def token_of(url):
    return re.search(r"/p/([A-Za-z0-9_-]{43})#", url).group(1)


def view_link(page, url):
    page.goto("about:blank")  # a fresh load, even when only the fragment differs from the current URL
    page.goto(url)
    ready(page)
    return page.locator("#public")


# ---- the owner's dialog and list


def test_create_a_link_copy_it_and_see_it_listed(desk, live_server, sim):
    desk.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live_server.url)
    f = sim.upload("plan.txt", b"the plan", ttl="30d")
    view = open_file(desk, f["id"])
    expect(view.locator("section.links-card")).to_be_hidden()   # no links yet

    sheet = open_share(desk)
    seg = sheet.locator("#link-ttl")
    expect(seg.get_by_role("button")).to_have_text(["1 hour", "1 day", "7 days", "30 days"])
    expect(seg.get_by_role("button", name="7 days")).to_have_attribute("aria-pressed", "true")
    for b in seg.get_by_role("button").all():
        expect(b).to_be_enabled()
    expect(sheet.locator(".link-cap")).to_be_hidden()
    expect(sheet.locator("#link-max")).to_be_disabled()
    expect(sheet.locator(".link-limit-hint")).to_be_hidden()
    sheet.get_by_label("Limit downloads").check()
    expect(sheet.locator(".link-limit-hint")).to_have_text("Each person who opens and previews or downloads uses one.")
    expect(sheet.locator("#link-max")).to_be_enabled()
    expect(sheet.locator("#link-max")).to_have_value("1")
    sheet.locator("#link-max").fill("3")
    seg.get_by_role("button", name="1 day").click()
    sheet.get_by_role("button", name="Create link").click()

    field = sheet.locator("#link-url")
    expect(field).to_have_value(re.compile(rf"^{re.escape(live_server.url)}/p/[A-Za-z0-9_-]{{43}}#[A-Za-z0-9_-]{{43}}$"))
    url = field.input_value()
    expect(field).to_have_attribute("readonly", "")
    expect(sheet).to_contain_text(
        "Shown once. Anyone with this link can read this file, including its note and transcript.")
    sheet.get_by_role("button", name="Copy link").click()
    assert desk.evaluate("navigator.clipboard.readText()") == url

    links = sim.request("GET", f"/api/files/{f['id']}/links").json()["links"]
    assert len(links) == 1 and links[0]["max_downloads"] == 3
    assert links[0]["created_by"]["name"]
    exp = links[0]["expires_at"]
    assert exp < f["expires_at"]  # 1 day, not 30

    # The list in the file view shows it, without the URL anywhere.
    card = view.locator("section.links-card")
    expect(card).to_be_visible()
    expect(card.locator(".link-row")).to_have_count(1)
    expect(card.locator(".link-row-head")).to_have_text("Expires in 1 day · 0/3 downloads")
    expect(card.locator(".link-row-sub")).to_have_text(re.compile(r"^Created just now by .+"))

    sheet.get_by_role("button", name="Done").click()
    expect(desk.locator("dialog.link-sheet")).to_have_count(0)
    assert token_of(url) not in desk.content() and link_key(url) not in desk.content()


def test_expiry_options_are_capped_at_the_file(desk, sim):
    f = sim.upload("short.txt", b"short", ttl="1d")
    open_file(desk, f["id"])
    sheet = open_share(desk)
    seg = sheet.locator("#link-ttl")
    expect(seg.get_by_role("button", name="1 day")).to_have_attribute("aria-pressed", "true")
    expect(seg.get_by_role("button", name="7 days")).to_be_disabled()
    expect(seg.get_by_role("button", name="30 days")).to_be_disabled()
    expect(seg.get_by_role("button", name="1 hour")).to_be_enabled()
    expect(sheet.locator(".link-cap")).to_be_hidden()   # 1 day, cut by seconds only

    sheet.get_by_label("Limit downloads").check()
    sheet.locator("#link-max").fill("1001")
    sheet.get_by_role("button", name="Create link").click()
    expect(sheet.get_by_role("alert")).to_have_text("Enter a number from 1 to 1000.")
    assert sim.request("GET", f"/api/files/{f['id']}/links").json()["links"] == []
    desk.keyboard.press("Escape")
    expect(desk.locator("dialog.link-sheet")).to_have_count(0)


def test_a_longer_option_is_offered_once_and_the_hint_shows_when_it_is_cut(desk, sim):
    f = sim.upload("week.txt", b"week", ttl="7d")
    open_file(desk, f["id"])
    # Two hours later: the 7-day file has 6 d 22 h left.
    desk.evaluate("() => { const real = Date.now.bind(Date); Date.now = () => real() + 2 * 3600e3; }")
    sheet = open_share(desk)
    seg = sheet.locator("#link-ttl")
    for name in ("1 hour", "1 day", "7 days"):   # 7 d: the smallest option past the file, clamped by the server
        expect(seg.get_by_role("button", name=name)).to_be_enabled()
    expect(seg.get_by_role("button", name="30 days")).to_be_disabled()
    expect(seg.get_by_role("button", name="7 days")).to_have_attribute("aria-pressed", "true")
    cap = sheet.locator(".link-cap")
    expect(cap).to_have_text("A link never outlives its file (expires in 7 days).")   # cut by 2 h
    seg.get_by_role("button", name="1 day").click()
    expect(cap).to_be_hidden()
    seg.get_by_role("button", name="7 days").click()
    expect(cap).to_be_visible()


def test_cli_links_show_up_and_revoke_asks_first(desk, live_server, sim, stranger):
    f = sim.upload("cli.txt", b"from the cli")
    url, made = sim.create_link(f["id"], ttl="7d")
    view = open_file(desk, f["id"])
    card = view.locator("section.links-card")
    expect(card.locator(".link-row")).to_have_count(1)
    expect(card.locator(".link-row-head")).to_have_text("Expires in 7 days · 0 downloads")

    card.get_by_role("button", name="Revoke link").click()
    confirm = desk.locator("dialog.confirm")
    expect(confirm).to_contain_text("Revoke this link?")
    confirm.get_by_role("button", name="Cancel").click()
    assert len(sim.request("GET", f"/api/files/{f['id']}/links").json()["links"]) == 1

    card.get_by_role("button", name="Revoke link").click()
    desk.locator("dialog.confirm").get_by_role("button", name="Revoke").click()
    expect(card).to_be_hidden()
    assert sim.request("GET", f"/api/files/{f['id']}/links").json()["links"] == []

    page = view_link(stranger, url)
    expect(page.locator(".pub-state-title")).to_have_text("This link has expired or was revoked")


def test_hostile_creator_name_in_the_list_is_text(desk, live_server, sim):
    f = sim.upload("h.txt", b"h")
    r = sim.request("PATCH", "/api/sessions/self", json={"name": HOSTILE})  # the session's name is the "by"
    assert r.status_code in (200, 204), r.text
    sim.create_link(f["id"])
    view = open_file(desk, f["id"])
    expect(view.locator("section.links-card .link-row-sub")).to_have_text(f"Created just now by {HOSTILE}")
    assert view.locator("section.links-card img").count() == 0
    assert desk.evaluate("window.__pwned") is None


# ---- the viewer


def test_viewer_shows_the_file_drops_the_key_and_downloads(stranger, live_server, sim):
    f = sim.upload("photo.png", PNG_1PX, note="the whiteboard")
    url, _ = sim.create_link(f["id"])
    requests = []
    stranger.on("request", lambda r: requests.append(r))
    page = view_link(stranger, url)
    expect(page.locator(".pub-name")).to_have_text("photo.png")
    expect(page.locator(".pub-note")).to_have_text("the whiteboard")
    expect(page.locator(".pub-facts")).to_have_text(re.compile(r"^\d+ B · Expires in 7 days$"))
    expect(page.locator("img.preview-img")).to_be_visible()
    assert stranger.evaluate("document.querySelector('img.preview-img').naturalWidth") == 1
    assert "#" not in stranger.url and stranger.url == url.split("#")[0]
    assert stranger.evaluate("location.href") == url.split("#")[0]
    expect(stranger.locator(".pub-foot")).to_have_text("Shared end-to-end encrypted via tix")
    assert stranger.locator('a[href*="login"]').count() == 0
    assert stranger.evaluate("navigator.serviceWorker.controller") is None
    assert stranger.evaluate("async () => (await indexedDB.databases()).map((d) => d.name)") == []

    with stranger.expect_download() as info:
        page.get_by_role("button", name="Download", exact=True).click()
    assert info.value.suggested_filename == "photo.png"
    assert Path(info.value.path()).read_bytes() == PNG_1PX
    ready(stranger)

    # Preview and Download shared one blob fetch; the key is in no request URL and no Referer.
    key = link_key(url)
    blob_gets = [r for r in requests if r.url.endswith("/blob")]
    assert len(blob_gets) == 1
    assert sim.request("GET", f"/api/files/{f['id']}/links").json()["links"][0]["downloads"] == 1
    assert requests, "no requests seen"
    for r in requests:
        assert key not in r.url, r.url
        assert "#" not in r.url, r.url
        headers = r.all_headers()
        assert not any(key in v for v in headers.values()), r.url
        if r.url.startswith("http"):  # blob: URLs (the decrypted image) never reach the network
            assert "referer" not in {k.lower() for k in headers}, r.url


def test_markdown_and_text_render_through_the_sanitiser(stranger, sim):
    md = sim.upload("readme.md", b"# Title\n\n<script>window.__pwned=1</script>\n\n[x](javascript:alert(1))\n")
    url, _ = sim.create_link(md["id"])
    view_link(stranger, url)
    frame = stranger.frame_locator("#public iframe.preview-md")
    expect(frame.locator("h1")).to_have_text("Title")
    assert frame.locator("script").count() == 0
    iframe = stranger.locator("#public iframe.preview-md")
    expect(iframe).to_have_attribute("sandbox", "")
    assert stranger.evaluate("window.__pwned") is None

    t = sim.upload("log.txt", b"line one\nline two")
    url, _ = sim.create_link(t["id"])
    view_link(stranger, url)
    expect(stranger.locator("#public pre.preview-pre")).to_have_text("line one\nline two")


def test_other_types_get_no_preview_and_spend_no_download_until_asked(stranger, sim):
    f = sim.upload("archive.zip", b"PK\x03\x04zipzip")
    url, _ = sim.create_link(f["id"], max_downloads=1)
    page = view_link(stranger, url)
    expect(page.locator(".pub-name")).to_have_text("archive.zip")
    expect(page.locator(".pub-preview")).to_have_text("No preview for this file type — Download it instead.")
    assert sim.request("GET", f"/api/files/{f['id']}/links").json()["links"][0]["downloads"] == 0
    with stranger.expect_download() as info:
        page.get_by_role("button", name="Download", exact=True).click()
    assert Path(info.value.path()).read_bytes() == b"PK\x03\x04zipzip"


@pytest.mark.parametrize("damage", ["flip", "missing", "empty", "short"])
def test_a_damaged_or_missing_key_says_so(stranger, sim, damage):
    f = sim.upload("secret.txt", b"secret")
    url, _ = sim.create_link(f["id"])
    base, key = url.split("#")
    if damage == "flip":
        i = 5
        bad = key[:i] + ("A" if key[i] != "A" else "B") + key[i + 1:]
        target = f"{base}#{bad}"
    elif damage == "missing":
        target = base
    elif damage == "empty":
        target = base + "#"
    else:
        target = f"{base}#{key[:-2]}"
    page = view_link(stranger, target)
    expect(page.locator(".pub-state-title")).to_have_text("This link is incomplete or damaged")
    assert page.locator(".pub-name").count() == 0
    assert "#" not in stranger.evaluate("location.href")
    assert "secret" not in stranger.content()


def test_a_revoked_or_used_up_link_says_expired(stranger, sim):
    f = sim.upload("once.txt", b"only once")
    url, made = sim.create_link(f["id"], max_downloads=1)
    page = view_link(stranger, url)
    page.get_by_role("button", name="Preview (uses 1 of 1 download left)").click()
    expect(page.locator("pre.preview-pre")).to_have_text("only once")   # asking for the preview spends it
    with stranger.expect_download() as info:                           # ... and Download reuses those bytes
        page.get_by_role("button", name="Download", exact=True).click()
    assert Path(info.value.path()).read_bytes() == b"only once"
    ready(stranger)
    assert len(sim.request("GET", f"/api/files/{f['id']}/links").json()["links"]) == 0   # used up
    page = view_link(stranger, url)
    expect(page.locator(".pub-state-title")).to_have_text("This link has expired or was revoked")

    url2, made2 = sim.create_link(f["id"])
    assert sim.request("DELETE", f"/api/links/{made2['id']}").status_code == 204
    page = view_link(stranger, url2)
    expect(page.locator(".pub-state-title")).to_have_text("This link has expired or was revoked")


def test_loading_a_limited_link_spends_nothing(stranger, sim):
    f = sim.upload("twice.txt", b"look, then keep")
    url, _ = sim.create_link(f["id"], max_downloads=2)
    blobs = []
    stranger.on("request", lambda r: blobs.append(r.url) if r.url.endswith("/blob") else None)
    page = view_link(stranger, url)
    expect(page.get_by_role("button", name="Preview (uses 1 of 2 downloads left)")).to_be_visible()
    assert page.locator("pre.preview-pre").count() == 0

    once, _ = sim.create_link(f["id"], max_downloads=1)
    for _ in range(2):                                   # load, then reload: nothing is spent
        page = view_link(stranger, once)
        expect(page.get_by_role("button", name="Preview (uses 1 of 1 download left)")).to_be_visible()
    assert blobs == []
    with stranger.expect_download() as info:
        page.get_by_role("button", name="Download", exact=True).click()
    assert Path(info.value.path()).read_bytes() == b"look, then keep"
    ready(stranger)
    assert len(blobs) == 1
    page = view_link(stranger, once)
    expect(page.locator(".pub-state-title")).to_have_text("This link has expired or was revoked")


@pytest.mark.parametrize("tamper", ["flipped", "not-json", "wrong-shape"])
def test_a_tampered_meta_says_damaged(stranger, sim, tamper):
    s = sim.s
    f = sim.upload("meta.txt", b"meta")
    url, _ = sim.create_link(f["id"])
    row = sim.get_file(f["id"])
    uuid = bytes.fromhex(row["uuid"])
    if tamper == "flipped":
        raw = bytearray(s.unb64u(row["enc_meta"]))
        raw[20] ^= 1
        enc = s.b64u(bytes(raw))
    else:
        _, dek = s.open_file_meta(sim.mk, row)
        pt = b"{not json" if tamper == "not-json" else b'{"name": 7}'
        enc = s.b64u(s.seal(dek, pt, s.aad_meta(uuid)))
    assert sim.request("PATCH", f"/api/files/{f['id']}/meta", json={"enc_meta": enc}).status_code == 204
    console = []
    stranger.on("console", lambda m: console.append(m.text))
    page = view_link(stranger, url)
    expect(page.locator(".pub-state-title")).to_have_text("This link is incomplete or damaged")
    assert console == []


def test_a_hostile_name_and_note_are_inert(stranger, sim):
    f = sim.upload(HOSTILE + ".md", b"# fine", note=HOSTILE)
    url, _ = sim.create_link(f["id"])
    page = view_link(stranger, url)
    expect(page.locator(".pub-name")).to_have_text(HOSTILE + ".md")
    expect(page.locator(".pub-note")).to_have_text(HOSTILE)
    assert page.locator("img").count() == 0
    assert stranger.evaluate("window.__pwned") is None
    assert stranger.title() == "Shared file · tix"


def test_the_transcript_of_an_audio_file_is_shown(stranger, sim):
    f = sim.upload("memo.mp3", b"\xff\xfb" + b"\0" * 400)
    sim.set_transcript(f["id"], {**TRANSCRIPT, "by": HOSTILE})
    url, _ = sim.create_link(f["id"])
    page = view_link(stranger, url)
    expect(page.locator("audio.preview-audio")).to_have_count(1)
    card = page.locator("section.transcript")
    expect(card.locator(".transcript-text")).to_have_text("Ship the release on Friday.")
    expect(card.locator(".transcript-meta")).to_contain_text(HOSTILE)
    assert page.locator("img").count() == 0
    assert stranger.evaluate("window.__pwned") is None


def test_viewer_at_390_has_no_sideways_scroll_and_big_tap_targets(stranger, sim):
    f = sim.upload("a-very-long-file-name-without-any-break-" + "x" * 120 + ".mp3", b"\xff\xfb" + b"\0" * 400,
                   note="n" * 400)
    sim.set_transcript(f["id"], {**TRANSCRIPT, "text": "word " * 300})
    url, _ = sim.create_link(f["id"])
    view_link(stranger, url)
    expect(stranger.locator("section.transcript")).to_be_visible()
    width = stranger.evaluate("Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)")
    assert width <= PHONE["width"]
    assert stranger.evaluate(SMALL_CONTROLS) == []
    card = stranger.locator(".pub-card").bounding_box()
    assert card["x"] >= 15 and card["width"] <= PHONE["width"] - 30

    limited, _ = sim.create_link(f["id"], max_downloads=5)
    view_link(stranger, limited)
    expect(stranger.locator(".pub-preview-btn")).to_be_visible()
    assert stranger.evaluate(SMALL_CONTROLS) == []

    stranger.set_viewport_size(DESKTOP)
    card = stranger.locator(".pub-card").bounding_box()
    assert card["width"] == 560


def test_share_sheet_at_390_is_a_bottom_sheet_with_big_tap_targets(phone, sim):
    f = sim.upload("p.txt", b"p")
    open_file(phone, f["id"])
    sheet = open_share(phone)
    box = sheet.bounding_box()
    assert box["x"] == 0 and abs(box["width"] - PHONE["width"]) < 1
    assert abs(box["y"] + box["height"] - PHONE["height"]) < 1
    assert phone.evaluate(SMALL_CONTROLS, sheet.element_handle()) == []
    sheet.get_by_role("button", name="Create link").click()
    expect(sheet.locator("#link-url")).to_be_visible()
    assert phone.evaluate(SMALL_CONTROLS, sheet.element_handle()) == []
    width = phone.evaluate("Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)")
    assert width <= PHONE["width"]
    sheet.get_by_role("button", name="Done").click()
    card = phone.locator("#detail section.links-card")
    expect(card.locator(".link-row")).to_have_count(1)
    assert phone.evaluate(SMALL_CONTROLS, card.element_handle()) == []


def test_pasting_the_link_again_in_the_same_tab_opens_it_afresh(stranger, sim):
    f = sim.upload("again.txt", b"again")
    url, _ = sim.create_link(f["id"])
    base, key = url.split("#")
    page = view_link(stranger, f"{base}#{key[:-3]}")
    expect(page.locator(".pub-state-title")).to_have_text("This link is incomplete or damaged")
    stranger.goto(url)  # same path, only the fragment differs
    ready(stranger)
    expect(stranger.locator("#public pre.preview-pre")).to_have_text("again")
    assert "#" not in stranger.evaluate("location.href")
