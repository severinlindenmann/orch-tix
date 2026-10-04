"""Web UI for the v2 features (spec §14 A–G, Task 27; redesigned in §18, Task 30): browser names,
sign out everywhere, "Mark as done", expiry, copy to clipboard, the detail pane (which replaced the
inline previews) and transcripts."""
import base64
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .cli import make_repo, onboard_approved, run_cli
from .conftest import login_ui

pytestmark = pytest.mark.browser

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
HOSTILE = "<img src=x onerror=window.__pwned=1>"
LOAD_KEYS = "async () => (await import('/static/js/keystore.js')).loadKeys()"
DERIVED = "async () => (await import('/static/js/browsername.js')).deriveBrowserName(navigator)"
TRANSCRIPT = {"text": "Hello there.\nSecond line, with <b>tags</b>.", "language": "en", "model": "nova-3",
              "created_at": "2026-09-24T12:00:00Z", "by": "laptop"}


def ready(page):
    """Let the startup requests (the list, its decrypts, the pending poll) settle."""
    page.wait_for_load_state("networkidle")


def row(page, file_id):
    return page.locator(f'#file-list .frow[data-id="{file_id}"]')


def rows(page):
    return page.locator("#file-list .frow")


def open_preview(page, file_id):
    """Selects the row; the desktop detail pane shows the file."""
    row(page, file_id).click()
    panel = page.locator("#detail.is-open")
    expect(panel).to_be_visible()
    return panel


def pick_filter(page, select_id, label):
    """The device and project filters live in the list header's filter menu (desktop)."""
    page.get_by_role("button", name="Filter by device or project").click()
    page.locator(select_id).select_option(label=label)
    page.keyboard.press("Escape")


def grant_clipboard(page, live_server):
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live_server.url)


# Records the `meta` part of each POST /api/files as the page's upload.js builds it (Playwright
# can't read a multipart body holding a Blob back from the request).
TTL_LABEL = {"1d": "1 day", "7d": "7 days", "30d": "30 days", "never": "Never"}
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


def upload_via_sheet(page, tmp_path, name, data, ttl=None):
    """Uploads through the real sheet; returns the upload meta upload.js sent."""
    path = Path(tmp_path) / name
    path.write_bytes(data)
    page.evaluate(SPY_UPLOADS)
    page.locator("#upload-btn").click()
    sheet = page.locator("dialog.upload-sheet")
    expect(sheet).to_be_visible()
    sheet.locator("#upload-input").set_input_files(str(path))
    if ttl is not None:
        sheet.get_by_role("button", name=TTL_LABEL[ttl], exact=True).click()
        expect(sheet.get_by_role("button", name=TTL_LABEL[ttl], exact=True)).to_have_attribute("aria-pressed", "true")
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/files")) as resp:
        sheet.get_by_role("button", name="Encrypt and share").click()
    assert resp.value.status == 201
    metas = page.evaluate("window.__metas")
    assert len(metas) == 1, metas
    return metas[0]


# ---- A / B: browser names, rename, sign out everywhere


def test_login_sends_the_derived_name_and_the_header_shows_it(ui_page, live_server):
    ready(ui_page)
    derived = ui_page.evaluate(DERIVED)
    assert re.fullmatch(r"(iPhone|iPad|Android phone|Mac|Windows PC|Linux PC|Browser) · "
                        r"(Safari|Chrome|Firefox|Edge|Browser)", derived), derived
    assert ui_page.request.get(live_server.url + "/api/sessions/self").json() == {"name": derived}
    btn = ui_page.locator("#browser-name")
    expect(btn).to_be_visible()
    expect(btn.locator(".browser-name-value")).to_have_text(derived)
    expect(btn).to_have_attribute("title", f"This browser: {derived} — rename")
    expect(ui_page.locator(".this-browser-label")).to_have_text("This browser")


def test_rename_then_a_browser_upload_shows_that_name_and_filters_by_it(ui_page, live_server, sim, tmp_path):
    sim.upload("from-sim.txt", b"sim")  # the sim's session has no name: From says "browser"
    ready(ui_page)
    ui_page.locator("#browser-name").click()
    dlg = ui_page.locator("dialog.rename-dialog")
    expect(dlg).to_be_visible()
    # Client-side check: empty is refused inline.
    dlg.locator("#browser-name-input").fill("   ")
    dlg.get_by_role("button", name="Save").click()
    expect(dlg.locator(".error-text")).to_have_text("Use 1–40 characters.")
    # Server-side refusal (400) is shown inline too, and the dialog stays open.
    ui_page.route("**/api/sessions/self", lambda route: route.fulfill(
        status=400, content_type="application/json", body='{"error":"bad_request","detail":"name"}')
        if route.request.method == "PATCH" else route.continue_())
    dlg.locator("#browser-name-input").fill("Refused name")
    dlg.get_by_role("button", name="Save").click()
    expect(dlg.locator(".error-text")).to_have_text("Use 1–40 printable characters.")
    ui_page.unroute("**/api/sessions/self")

    dlg.locator("#browser-name-input").fill(HOSTILE[:40])
    dlg.get_by_role("button", name="Save").click()
    expect(dlg).to_have_count(0)
    expect(ui_page.locator("#browser-name .browser-name-value")).to_have_text(HOSTILE[:40])
    assert ui_page.request.get(live_server.url + "/api/sessions/self").json() == {"name": HOSTILE[:40]}

    ui_page.locator("#browser-name").click()
    ui_page.locator("#browser-name-input").fill("Work laptop")
    ui_page.locator("dialog.rename-dialog").get_by_role("button", name="Save").click()
    expect(ui_page.locator("#browser-name .browser-name-value")).to_have_text("Work laptop")

    upload_via_sheet(ui_page, tmp_path, "from-ui.txt", b"ui")
    mine = rows(ui_page).filter(has_text="from-ui.txt")
    expect(mine.locator(".device")).to_have_text("Work laptop")
    expect(rows(ui_page).filter(has_text="from-sim.txt").locator(".device")).to_have_text("browser")
    ready(ui_page)

    pick_filter(ui_page, "#filter-device", "Work laptop")
    expect(rows(ui_page)).to_have_count(1)
    expect(rows(ui_page).first).to_contain_text("from-ui.txt")
    pick_filter(ui_page, "#filter-device", "browser")
    expect(rows(ui_page)).to_have_count(1)
    expect(rows(ui_page).first).to_contain_text("from-sim.txt")
    assert ui_page.locator("img").count() == 0
    assert ui_page.evaluate("window.__pwned") is None


def test_sign_out_everywhere_logs_out_and_clears_the_keys(ui_page, live_server, sim):
    ready(ui_page)
    ui_page.goto(live_server.url + "/settings")
    ready(ui_page)
    expect(ui_page.locator("#this-browser")).to_have_text(ui_page.evaluate(DERIVED))
    assert ui_page.evaluate(LOAD_KEYS) is not None
    ui_page.get_by_role("button", name="Sign out everywhere").click()
    dlg = ui_page.get_by_role("dialog")
    expect(dlg).to_contain_text("Sign out everywhere?")
    dlg.get_by_role("button", name="Cancel").click()
    assert ui_page.url == live_server.url + "/settings"

    ui_page.get_by_role("button", name="Sign out everywhere").click()
    ui_page.get_by_role("dialog").get_by_role("button", name="Sign out everywhere").click()
    ui_page.wait_for_url(live_server.url + "/login")
    assert ui_page.evaluate(LOAD_KEYS) is None
    assert ui_page.request.get(live_server.url + "/api/files").status == 401
    # Every session is gone, the sim's (another browser) included.
    assert sim.request("GET", "/api/files").status_code == 401


# ---- C: "Mark as done" (the API's ack; wording spec §18)


def test_mark_as_done_hides_the_row_the_done_chip_dims_it_and_not_done_restores(ui_page, sim):
    a = sim.upload("a.md", b"# a")
    b = sim.upload("b.txt", b"b")
    ui_page.reload()
    ready(ui_page)
    expect(ui_page.locator("#count")).to_have_text("2 open")
    open_preview(ui_page, a["id"]).get_by_role("button", name="Mark as done", exact=True).click()
    expect(row(ui_page, a["id"])).to_have_count(0)
    expect(row(ui_page, b["id"])).to_have_count(1)
    expect(ui_page.locator("#count")).to_have_text("1 open · 1 done hidden")
    assert sim.get_file(a["id"])["acked_at"] is not None

    chip = ui_page.get_by_role("button", name="Done", exact=True)
    expect(chip).to_have_attribute("aria-pressed", "false")
    with ui_page.expect_response(lambda r: "/api/files?" in r.url and "acked=1" in r.url):
        chip.click()
    expect(chip).to_have_attribute("aria-pressed", "true")
    r = row(ui_page, a["id"])
    expect(r).to_have_class(re.compile(r"\backed\b"))
    derived = ui_page.evaluate(DERIVED)
    expect(r.locator(".acked-line")).to_have_text(f"Done by {derived}")
    expect(r.locator(".tile")).to_have_class(re.compile(r"\bt-done\b"))
    expect(row(ui_page, b["id"])).not_to_have_class(re.compile(r"\backed\b"))

    # The chip is remembered in this browser.
    ui_page.reload()
    ready(ui_page)
    expect(ui_page.get_by_role("button", name="Done", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(row(ui_page, a["id"])).to_have_class(re.compile(r"\backed\b"))

    panel = open_preview(ui_page, a["id"])
    expect(panel.locator(".pill-done")).to_contain_text(f"Done by {derived} · ")
    panel.get_by_role("button", name="Mark as not done", exact=True).click()
    expect(row(ui_page, a["id"])).not_to_have_class(re.compile(r"\backed\b"))
    expect(row(ui_page, a["id"]).locator(".acked-line")).to_have_count(0)
    expect(panel.locator(".pill-done")).to_be_hidden()
    assert sim.get_file(a["id"])["acked_at"] is None

    ui_page.get_by_role("button", name="Done", exact=True).click()
    expect(row(ui_page, a["id"])).to_have_count(1)
    expect(ui_page.locator("#count")).to_have_text("2 open")


def test_mark_as_done_from_the_file_view_toggles(ui_page, sim):
    f = sim.upload("p.md", b"# p")
    ui_page.reload()
    ready(ui_page)
    panel = open_preview(ui_page, f["id"])
    panel.get_by_role("button", name="Mark as done", exact=True).click()
    expect(panel.get_by_role("button", name="Mark as not done", exact=True)).to_be_visible()
    expect(ui_page.locator(".toast").last).to_have_text(f"Marked {f['id']} as done")
    assert sim.get_file(f["id"])["acked_at"] is not None
    panel.get_by_role("button", name="Mark as not done", exact=True).click()
    expect(panel.get_by_role("button", name="Mark as done", exact=True)).to_be_visible()
    assert sim.get_file(f["id"])["acked_at"] is None


def test_a_cli_get_acknowledges_and_the_row_disappears_after_reload(ui_page, live_server, sim, tmp_path):
    f = sim.upload("for-agent.md", b"# for the agent")
    repo = make_repo(tmp_path / "repo")
    onboard_approved(sim, live_server.url, repo, "laptop", "proj")
    ui_page.reload()
    ready(ui_page)
    expect(row(ui_page, f["id"])).to_have_count(1)
    r = run_cli(repo, live_server.url, "get", f["id"])
    assert r.returncode == 0, r.stdout + r.stderr
    ui_page.reload()
    ready(ui_page)
    expect(row(ui_page, f["id"])).to_have_count(0)
    ui_page.get_by_role("button", name="Done", exact=True).click()
    expect(row(ui_page, f["id"]).locator(".acked-line")).to_have_text("Done by laptop")


# ---- E: expiry


def test_upload_sheet_sends_the_chosen_ttl(ui_page, sim, tmp_path):
    ready(ui_page)
    meta = upload_via_sheet(ui_page, tmp_path, "short.txt", b"short-lived", ttl="1d")
    assert meta["ttl"] == "1d"
    r = rows(ui_page).filter(has_text="short.txt")
    expect(r.locator(".exp")).to_have_text("1d")
    expect(r.locator(".exp")).to_have_class(re.compile(r"\bexp-soon\b"))
    file_id = r.get_attribute("data-id")
    pill = open_preview(ui_page, file_id).locator(".pill-exp")
    expect(pill).to_have_text("Expires in 1 day")
    expect(pill).to_have_class(re.compile(r"\bexp-soon\b"))
    assert sim.get_file(file_id)["expires_at"] is not None


def test_upload_sheet_defaults_to_seven_days(ui_page, tmp_path):
    ready(ui_page)
    ui_page.locator("#upload-btn").click()
    seg = ui_page.locator("#upload-ttl")
    expect(seg.locator('[aria-pressed="true"]')).to_have_text("7 days")
    expect(seg.get_by_role("button")).to_have_text(["1 day", "7 days", "30 days", "Never"])
    ui_page.locator("dialog.upload-sheet").get_by_role("button", name="Close").click()
    expect(ui_page.locator("dialog.upload-sheet")).to_have_count(0)
    meta = upload_via_sheet(ui_page, tmp_path, "week.txt", b"a week")
    assert set(meta) == {"uuid", "key_version", "wrapped_dek", "enc_meta", "ttl"}
    assert meta["ttl"] == "7d"
    pill = rows(ui_page).filter(has_text="week.txt").locator(".exp")
    expect(pill).to_have_text("7d")
    expect(pill).not_to_have_class(re.compile(r"\bexp-soon\b"))


def test_change_expiry_to_never(ui_page, sim):
    f = sim.upload("keep.md", b"# keep")
    ui_page.reload()
    ready(ui_page)
    expect(row(ui_page, f["id"]).locator(".exp")).to_have_text("7d")
    pill = open_preview(ui_page, f["id"]).locator(".pill-exp")
    expect(pill).to_have_text("Expires in 7 days")
    pill.click()
    dlg = ui_page.locator("dialog.form-dialog")
    expect(dlg).to_contain_text(f"Change expiry of {f['id']}")
    dlg.locator("#expiry-ttl").select_option("never")
    dlg.get_by_role("button", name="Save").click()
    expect(dlg).to_have_count(0)
    expect(pill).to_have_text("Never expires")
    expect(row(ui_page, f["id"]).locator(".exp")).to_have_text("never")
    assert sim.get_file(f["id"])["expires_at"] is None


def test_less_than_a_day_left_gets_the_red_pill(ui_page, sim):
    soon = sim.upload("soon.txt", b"x", ttl="1d")
    later = sim.upload("later.txt", b"x", ttl="30d")
    forever = sim.upload("forever.txt", b"x", ttl="never")
    ui_page.reload()
    ready(ui_page)
    red = re.compile(r"\bexp-soon\b")
    expect(row(ui_page, soon["id"]).locator(".exp")).to_have_class(red)
    expect(row(ui_page, later["id"]).locator(".exp")).to_have_text("30d")
    expect(row(ui_page, later["id"]).locator(".exp")).not_to_have_class(red)
    expect(row(ui_page, forever["id"]).locator(".exp")).to_have_text("never")
    ui_page.set_viewport_size({"width": 390, "height": 844})
    soon_exp = row(ui_page, soon["id"]).locator(".exp")
    expect(soon_exp).to_have_class(red)
    assert ui_page.evaluate("el => getComputedStyle(el).color", soon_exp.element_handle()) == "rgb(180, 35, 24)"


# ---- D: copy to clipboard


def test_copy_markdown_from_the_preview(ui_page, live_server, sim):
    grant_clipboard(ui_page, live_server)
    body = "# Notes\n\n- one\n- two\n"
    f = sim.upload("notes.md", body.encode())
    ui_page.reload()
    ready(ui_page)
    panel = open_preview(ui_page, f["id"])
    panel.get_by_role("button", name="Copy", exact=True).click()
    expect(ui_page.locator(".toast").last).to_have_text("Copied")
    assert ui_page.evaluate("navigator.clipboard.readText()") == body


def test_copy_text_from_a_json_preview(ui_page, live_server, sim):
    grant_clipboard(ui_page, live_server)
    f = sim.upload("cfg.json", b'{"a":1}')
    ui_page.reload()
    ready(ui_page)
    open_preview(ui_page, f["id"]).get_by_role("button", name="Copy", exact=True).click()
    expect(ui_page.locator(".toast").last).to_have_text("Copied")
    assert ui_page.evaluate("navigator.clipboard.readText()") == '{"a":1}'


CLIPBOARD_TYPES = "async () => (await navigator.clipboard.read()).flatMap((i) => i.types)"
CLIPBOARD_PNG_SIZE = """async () => {
  const [item] = await navigator.clipboard.read();
  const bmp = await createImageBitmap(await item.getType('image/png'));
  return [bmp.width, bmp.height];
}"""
MAKE_JPEG = """async () => {
  const c = document.createElement('canvas'); c.width = 3; c.height = 2;
  const ctx = c.getContext('2d'); ctx.fillStyle = '#c2540c'; ctx.fillRect(0, 0, 3, 2);
  const b = await new Promise((r) => c.toBlob(r, 'image/jpeg'));
  return [...new Uint8Array(await b.arrayBuffer())];
}"""


def test_copy_image_from_the_preview_puts_a_png_on_the_clipboard(ui_page, live_server, sim):
    grant_clipboard(ui_page, live_server)
    f = sim.upload("dot.png", PNG_1PX)
    ui_page.reload()
    ready(ui_page)
    panel = open_preview(ui_page, f["id"])
    expect(panel.locator("img.preview-img")).to_be_visible()
    panel.get_by_role("button", name="Copy", exact=True).click()
    expect(ui_page.locator(".toast").last).to_have_text("Copied")
    assert "image/png" in ui_page.evaluate(CLIPBOARD_TYPES)


def test_copy_a_jpeg_converts_it_to_png(ui_page, live_server, sim):
    grant_clipboard(ui_page, live_server)
    jpeg = bytes(ui_page.evaluate(MAKE_JPEG))
    assert jpeg[:2] == b"\xff\xd8"
    f = sim.upload("photo.jpg", jpeg)
    ui_page.reload()
    ready(ui_page)
    panel = open_preview(ui_page, f["id"])
    expect(panel.locator("img.preview-img")).to_be_visible()
    panel.get_by_role("button", name="Copy", exact=True).click()
    expect(ui_page.locator(".toast").last).to_have_text("Copied")
    assert "image/png" in ui_page.evaluate(CLIPBOARD_TYPES)
    assert ui_page.evaluate(CLIPBOARD_PNG_SIZE) == [3, 2]


def test_copy_without_a_clipboard_api_says_so(ui_page, sim):
    f = sim.upload("n.md", b"# n")
    ui_page.reload()
    ready(ui_page)
    ui_page.evaluate("() => Object.defineProperty(Navigator.prototype, 'clipboard', { get: () => undefined })")
    open_preview(ui_page, f["id"]).get_by_role("button", name="Copy", exact=True).click()
    expect(ui_page.locator(".toast-error")).to_have_text("Copy isn't available in this browser")


def test_copy_all_of_a_long_text_from_the_detail_pane(ui_page, live_server, sim):
    grant_clipboard(ui_page, live_server)
    body = "line one\nline two\n" + "\n".join(f"line {i}" for i in range(3, 20))
    f = sim.upload("long.txt", body.encode())
    ui_page.reload()
    ready(ui_page)
    copy = open_preview(ui_page, f["id"]).get_by_role("button", name="Copy", exact=True)
    expect(copy).to_have_attribute("title", "Copy text")
    copy.click()
    expect(ui_page.locator(".toast").last).to_have_text("Copied")
    assert ui_page.evaluate("navigator.clipboard.readText()") == body


# ---- F, replaced (spec §18): the detail pane shows one file; the list decrypts no content


def test_the_list_fetches_no_content_and_the_pane_shows_the_selection(ui_page, sim, live_server):
    ui_page.set_viewport_size({"width": 1280, "height": 900})
    png = sim.upload("dot.png", PNG_1PX)
    md = sim.upload("readme.md", HOSTILE.encode())
    t = sim.upload("t.md", b"# will be tampered\n")
    u = t["uuid"]
    path = live_server.data_dir / "blobs" / u[:2] / u[2:4] / f"{u}.shr"
    data = bytearray(path.read_bytes())
    data[-1] ^= 0x01
    path.write_bytes(bytes(data))
    blob_requests = []
    ui_page.on("request", lambda r: blob_requests.append(r.url) if r.url.endswith("/blob") else None)
    ui_page.reload()
    ready(ui_page)
    expect(rows(ui_page)).to_have_count(3)
    assert blob_requests == []
    assert ui_page.locator(".inline-preview, img.inline-thumb, pre.inline-text").count() == 0
    assert ui_page.locator(".banner").filter(has_text="Couldn't decrypt").count() == 0
    expect(ui_page.locator("#detail .detail-empty")).to_be_visible()

    panel = open_preview(ui_page, png["id"])
    img = panel.locator("img.preview-img")
    expect(img).to_be_visible()
    src = img.get_attribute("src")
    assert src.startswith("blob:")
    expect(row(ui_page, png["id"])).to_have_attribute("aria-current", "true")
    assert re.search(rf"\?f={png['id']}$", ui_page.url)

    # Selecting another row replaces the pane and revokes the old blob URL.
    open_preview(ui_page, md["id"])
    expect(ui_page.frame_locator("#detail iframe.preview-md").locator("body")).to_contain_text("<img src=x")
    assert ui_page.evaluate("u => fetch(u).then(() => 'ok', () => 'revoked')", src) == "revoked"
    expect(row(ui_page, png["id"])).not_to_have_attribute("aria-current", "true")
    assert ui_page.evaluate("window.__pwned") is None


def test_no_content_is_fetched_on_a_phone_until_a_row_is_opened(ui_page, sim):
    ui_page.set_viewport_size({"width": 390, "height": 844})
    sim.upload("dot.png", PNG_1PX)
    sim.upload("readme.md", b"# First line\n")
    blob_requests = []
    ui_page.on("request", lambda r: blob_requests.append(r.url) if r.url.endswith("/blob") else None)
    ui_page.reload()
    ready(ui_page)
    expect(rows(ui_page)).to_have_count(2)
    expect(ui_page.locator("#detail")).to_be_hidden()
    assert blob_requests == []


# ---- G: transcripts


def test_transcript_in_the_audio_preview(ui_page, live_server, sim):
    grant_clipboard(ui_page, live_server)
    f = sim.upload("memo.mp3", b"\xff\xfb" + b"\0" * 400)
    plain = sim.upload("other.mp3", b"\xff\xfb" + b"\0" * 400)
    sim.set_transcript(f["id"], TRANSCRIPT)
    ui_page.reload()
    ready(ui_page)
    expect(row(ui_page, f["id"]).locator(".pill-transcribed")).to_have_text("Transcribed")
    assert row(ui_page, plain["id"]).locator(".pill-transcribed").count() == 0

    panel = open_preview(ui_page, f["id"])
    expect(panel.locator("audio.preview-audio")).to_have_count(1)
    section = panel.locator("section.transcript")
    expect(section.locator(".transcript-text")).to_have_text(TRANSCRIPT["text"])
    assert section.locator(".transcript-text").text_content() == TRANSCRIPT["text"]  # newline kept
    assert section.locator("b").count() == 0
    expect(section.locator(".transcript-meta")).to_contain_text("nova-3 · en · by laptop · ")
    assert ui_page.evaluate("el => getComputedStyle(el).whiteSpace",
                            section.locator(".transcript-text").element_handle()) == "pre-wrap"

    section.get_by_role("button", name="Copy transcript").click()
    expect(ui_page.locator(".toast").last).to_have_text("Copied")
    assert ui_page.evaluate("navigator.clipboard.readText()") == TRANSCRIPT["text"]

    with ui_page.expect_download() as info:
        section.get_by_role("button", name="Download as .md").click()
    dl = info.value
    assert dl.suggested_filename == "memo-transcript.md"
    assert Path(dl.path()).read_text("utf-8") == f"# memo.mp3\n\n{TRANSCRIPT['text']}"


def test_audio_without_a_transcript_says_how_to_get_one(ui_page, sim):
    # §20: agents can't transcribe any more; the view offers Transcribe (disabled without a key).
    f = sim.upload("memo.mp3", b"\xff\xfb" + b"\0" * 400)
    ui_page.reload()
    ready(ui_page)
    panel = open_preview(ui_page, f["id"])
    expect(panel.locator(".transcript-none")).to_have_text("No transcript yet.")
    expect(panel.locator(".transcribe-btn")).to_be_disabled()
    expect(panel.locator(".transcribe-hint")).to_have_text("Set a Deepgram key in Settings to transcribe.")
    assert panel.locator("code.cmd").count() == 0


def test_hostile_transcript_renders_as_text(ui_page, sim):
    dialogs = []
    ui_page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    f = sim.upload("evil.mp3", b"\xff\xfb" + b"\0" * 400)
    sim.set_transcript(f["id"], {**TRANSCRIPT, "text": HOSTILE, "by": HOSTILE, "model": "<script>x</script>"})
    ui_page.reload()
    ready(ui_page)
    panel = open_preview(ui_page, f["id"])
    section = panel.locator("section.transcript")
    expect(section.locator(".transcript-text")).to_have_text(HOSTILE)
    expect(section.locator(".transcript-meta")).to_contain_text(f"<script>x</script> · en · by {HOSTILE}")
    assert section.locator("img, script").count() == 0
    ui_page.wait_for_timeout(300)
    assert ui_page.evaluate("window.__pwned") is None
    assert dialogs == []


@pytest.mark.parametrize("bad", [
    {"text": 5},
    "<img src=x onerror=window.__pwned=1>",
    {**TRANSCRIPT, "language": None},
    [TRANSCRIPT],
])
def test_malformed_transcript_is_ignored(ui_page, sim, bad):
    f = sim.upload("odd.mp3", b"\xff\xfb" + b"\0" * 400)
    sim.set_transcript(f["id"], bad)
    ui_page.reload()
    ready(ui_page)
    expect(row(ui_page, f["id"])).to_contain_text("odd.mp3")  # the file itself still decrypts
    assert row(ui_page, f["id"]).locator(".pill-transcribed").count() == 0
    panel = open_preview(ui_page, f["id"])
    expect(panel.locator(".transcript-none")).to_be_visible()
    assert ui_page.evaluate("window.__pwned") is None


# ---- fix round 1


def test_unparseable_expiry_shows_no_pill(ui_page, sim):
    f = sim.upload("odd-expiry.txt", b"x")
    keep = sim.upload("normal.txt", b"x")

    def garble(route):
        resp = route.fetch()
        body = resp.json()
        for x in body["files"]:
            if x["id"] == f["id"]:
                x["expires_at"] = "not a date"
        route.fulfill(response=resp, json=body)

    ui_page.route(re.compile(r"/api/files\?"), garble)
    ui_page.reload()
    ready(ui_page)
    expect(row(ui_page, f["id"])).to_contain_text("odd-expiry.txt")
    assert row(ui_page, f["id"]).locator(".exp").count() == 0
    expect(row(ui_page, keep["id"]).locator(".exp")).to_have_text("7d")
    ui_page.unroute(re.compile(r"/api/files\?"))


def test_session_expiry_closes_the_file_view_and_revokes_its_blob(ui_page, sim):
    png = sim.upload("dot.png", PNG_1PX)
    ui_page.reload()
    ready(ui_page)
    img = open_preview(ui_page, png["id"]).locator("img.preview-img")
    expect(img).to_be_visible()
    src = img.get_attribute("src")
    ui_page.evaluate("() => window.dispatchEvent(new CustomEvent('fs:unauthenticated'))")
    expect(ui_page.locator("#detail.is-open")).to_have_count(0)
    assert ui_page.evaluate("u => fetch(u).then(() => 'ok', () => 'revoked')", src) == "revoked"


def test_rename_accepts_forty_emoji(ui_page, live_server):
    ready(ui_page)
    name = "😀" * 40  # 40 code points, 80 UTF-16 units
    ui_page.locator("#browser-name").click()
    ui_page.locator("#browser-name-input").fill(name)
    ui_page.locator("dialog.rename-dialog").get_by_role("button", name="Save").click()
    expect(ui_page.locator("dialog.rename-dialog")).to_have_count(0)
    assert ui_page.request.get(live_server.url + "/api/sessions/self").json() == {"name": name}
    ui_page.locator("#browser-name").click()
    ui_page.locator("#browser-name-input").fill("😀" * 41)  # maxlength (80 units) stops at 40 emoji
    assert ui_page.locator("#browser-name-input").input_value() == name
    # A 41-code-point BMP name fits maxlength, so the code-point check refuses it inline.
    ui_page.locator("#browser-name-input").fill("é" * 41)
    ui_page.locator("dialog.rename-dialog").get_by_role("button", name="Save").click()
    expect(ui_page.locator("dialog.rename-dialog .error-text")).to_have_text("Use 1–40 characters.")
