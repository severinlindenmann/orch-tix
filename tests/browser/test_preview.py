import base64
import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

XSS_MD = """# Title

<script>window.top.__pwned = 1</script>

<img src=x onerror="window.top.__pwned = 2">

[click me](javascript:window.top.__pwned=3)

<b onmouseover="window.top.__pwned = 4">raw html</b>

<a href="https://example.com" target="_top">outbound</a>
"""

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

DECRYPT_MSG = "Couldn't decrypt {id} — the file may be corrupt or was encrypted with another key."

# fileview.js as the page's own module graph loads it (relative imports drop the ?v= stamp).
OPEN_RAW = "async (row) => { (await import('/static/js/fileview.js')).openFileView(row); }"
PANEL = "#detail.is-open"


def open_preview(page, file_id):
    """Opens the file in the desktop detail pane (spec §18), which replaced the preview panel."""
    page.reload()
    page.wait_for_load_state("networkidle")
    page.locator(f'.frow[data-id="{file_id}"]').click()
    panel = page.locator(PANEL)
    expect(panel).to_be_visible()
    return panel


def test_markdown_payloads_are_inert(ui_page, sim):
    dialogs = []
    ui_page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    f = sim.upload("<img src=x onerror=alert(1)>.md", XSS_MD.encode(), note="<script>alert(2)</script>")

    panel = open_preview(ui_page, f["id"])
    iframe = panel.locator("iframe.preview-md")
    assert iframe.get_attribute("sandbox") == ""
    assert iframe.get_attribute("referrerpolicy") == "no-referrer"

    frame = ui_page.frame_locator("#detail iframe.preview-md")
    expect(frame.locator("h1")).to_have_text("Title")
    assert frame.locator("script, img, svg, iframe, form, style[onload]").count() == 0
    frame.get_by_text("click me").first.click()
    frame.get_by_text("raw html").first.hover()
    frame.get_by_text("outbound").first.click()

    expect(panel.locator(".detail-name")).to_have_text("<img src=x onerror=alert(1)>.md")
    expect(panel.locator(".preview-note")).to_have_text("<script>alert(2)</script>")
    expect(ui_page.locator(f'[data-id="{f["id"]}"]').first).to_contain_text("<img src=x onerror=alert(1)>.md")
    assert re.search(r"/files\?f=FILE\d+$", ui_page.url), ui_page.url  # no navigation, only the view's own URL
    assert ui_page.evaluate("window.__pwned") is None
    assert dialogs == []


def test_preview_style_is_allowed_by_csp(ui_page, sim):
    f = sim.upload("styled.md", b"# Styled\n\n`code`")
    violations = []
    ui_page.on("console", lambda m: violations.append(m.text) if "Content Security Policy" in m.text else None)
    open_preview(ui_page, f["id"])
    frame = ui_page.frame_locator("#detail iframe.preview-md")
    expect(frame.locator("h1")).to_have_text("Styled")
    assert violations == []


def test_json_pretty_then_raw(ui_page, sim):
    f = sim.upload("cfg.json", b'{"b":1,"a":[1,2]}')
    panel = open_preview(ui_page, f["id"])
    pre = panel.locator("pre.preview-pre")
    expect(pre).to_contain_text('"b": 1')
    assert pre.text_content() == '{\n  "b": 1,\n  "a": [\n    1,\n    2\n  ]\n}'
    panel.get_by_role("button", name="Raw").click()
    assert pre.text_content() == '{"b":1,"a":[1,2]}'
    panel.get_by_role("button", name="Pretty").click()
    expect(pre).to_contain_text('"a": [')


def test_invalid_json_falls_back_to_raw(ui_page, sim):
    f = sim.upload("broken.json", b"{not json")
    panel = open_preview(ui_page, f["id"])
    expect(panel).to_contain_text("Not valid JSON — showing raw text.")
    assert panel.locator("pre.preview-pre").text_content() == "{not json"


def test_text_preview_is_textcontent(ui_page, sim):
    f = sim.upload("run.log", b"<b>not bold</b>\nline 2")
    panel = open_preview(ui_page, f["id"])
    pre = panel.locator("pre.preview-pre")
    expect(pre).to_contain_text("line 2")
    assert pre.locator("b").count() == 0


def test_image_preview_and_url_revoked_on_close(ui_page, sim):
    f = sim.upload("dot.png", PNG_1PX)
    panel = open_preview(ui_page, f["id"])
    img = panel.locator("img.preview-img")
    expect(img).to_be_visible()
    src = img.get_attribute("src")
    assert src.startswith("blob:")
    assert ui_page.evaluate("el => el.naturalWidth", img.element_handle()) == 1
    ui_page.keyboard.press("Escape")
    expect(ui_page.locator(PANEL)).to_have_count(0)
    assert ui_page.evaluate("u => fetch(u).then(() => 'ok', () => 'revoked')", src) == "revoked"


def test_svg_is_download_only(ui_page, sim):
    # R8: SVG is never rendered, only offered for download.
    f = sim.upload("x.svg", b'<svg xmlns="http://www.w3.org/2000/svg" onload="window.top.__pwned=5"/>')
    blob_requests = []
    ui_page.on("request", lambda r: blob_requests.append(r.url) if r.url.endswith("/blob") else None)
    panel = open_preview(ui_page, f["id"])
    expect(panel).to_contain_text("No preview for this file type — Download it instead.")
    expect(panel.get_by_role("button", name="Download", exact=True)).to_be_enabled()
    assert panel.locator("iframe, img, svg image, object, embed").count() == 0
    assert blob_requests == []
    assert ui_page.evaluate("window.__pwned") is None


def test_too_large_is_not_downloaded(ui_page, sim):
    f = sim.upload("big.txt", b"a" * (2 * 1024 * 1024 + 1))
    blob_requests = []
    ui_page.on("request", lambda r: blob_requests.append(r.url) if r.url.endswith("/blob") else None)
    panel = open_preview(ui_page, f["id"])
    expect(panel).to_contain_text("Too large to preview (> 2 MiB) — Download")
    assert blob_requests == []


def test_tampered_blob_shows_decrypt_message(ui_page, sim, live_server):
    f = sim.upload("t.md", b"# will be tampered\n" * 100)
    u = f["uuid"]
    path = live_server.data_dir / "blobs" / u[:2] / u[2:4] / f"{u}.shr"
    data = bytearray(path.read_bytes())
    data[-1] ^= 0x01
    path.write_bytes(bytes(data))
    panel = open_preview(ui_page, f["id"])
    expect(panel).to_contain_text(DECRYPT_MSG.format(id=f["id"]))
    assert panel.locator("iframe").count() == 0


def test_escape_closes(ui_page, sim):
    f = sim.upload("esc.md", b"# esc")
    open_preview(ui_page, f["id"])
    ui_page.keyboard.press("Escape")
    expect(ui_page.locator(PANEL)).to_have_count(0)


# ---- carry-in (T9 review): every way the preview's decryption can fail ends in a message, not a crash


def test_file_deleted_after_listing_says_deleted(ui_page, sim):
    f = sim.upload("gone.md", b"# gone")
    ui_page.reload()
    ui_page.wait_for_load_state("networkidle")
    sim.delete(f["id"])  # the row is still on screen; the blob fetch now gets 410
    ui_page.locator(f'.frow[data-id="{f["id"]}"]').click()
    panel = ui_page.locator(PANEL)
    expect(panel).to_contain_text(f"{f['id']} was deleted.")
    assert panel.locator("iframe").count() == 0


def test_tombstone_row_says_deleted(ui_page, sim):
    # openFileMeta throws a plain Error (not IntegrityError) for a tombstone.
    f = sim.upload("tomb.md", b"# tomb")
    sim.delete(f["id"])
    listing = sim.request("GET", "/api/files?limit=50").json()["files"]
    tomb = next(x for x in listing if x["id"] == f["id"])
    assert tomb["deleted_at"] and tomb["wrapped_dek"] is None
    errors = []
    ui_page.on("pageerror", lambda e: errors.append(str(e)))
    ui_page.evaluate(OPEN_RAW, tomb)
    panel = ui_page.locator(PANEL)
    expect(panel).to_contain_text(f"{f['id']} was deleted.")
    expect(panel.get_by_role("button", name="Download", exact=True)).to_be_hidden()  # a tombstone offers no actions
    assert errors == []


def test_tampered_metadata_row_shows_decrypt_message(ui_page, sim):
    # openFileMeta throws IntegrityError when enc_meta fails authentication.
    f = sim.upload("meta.md", b"# meta")
    row = sim.get_file(f["id"])
    enc = bytearray(base64.urlsafe_b64decode(row["enc_meta"] + "=" * (-len(row["enc_meta"]) % 4)))
    enc[-1] ^= 0x01
    row["enc_meta"] = base64.urlsafe_b64encode(bytes(enc)).rstrip(b"=").decode()
    errors = []
    ui_page.on("pageerror", lambda e: errors.append(str(e)))
    ui_page.evaluate(OPEN_RAW, row)
    panel = ui_page.locator(PANEL)
    expect(panel).to_contain_text(DECRYPT_MSG.format(id=f["id"]))
    assert panel.locator("iframe").count() == 0
    assert errors == []


# ---- carry-ins (T19 review)


def test_render_exception_shows_an_error_instead_of_decrypting(ui_page, sim):
    # (a) a missing vendor global makes renderKind throw; the panel must not stay on "Decrypting…".
    f = sim.upload("novendor.md", b"# no vendor")
    ui_page.reload()
    ui_page.wait_for_load_state("networkidle")
    ui_page.evaluate("window.markdownit = undefined")
    ui_page.locator(f'.frow[data-id="{f["id"]}"]').click()
    panel = ui_page.locator(PANEL)
    expect(panel.locator(".preview-msg.error")).to_have_text(f"Couldn't preview {f['id']}.")
    expect(panel).not_to_contain_text("Decrypting")


# Every attribute value of every element renderMarkdown produces (the browser parses it as the iframe will).
RENDERED_ATTRS = """async (src) => {
  const { renderMarkdown } = await import('/static/js/preview.js');
  const doc = new DOMParser().parseFromString(renderMarkdown(src), 'text/html');
  return [...doc.body.querySelectorAll('*')].flatMap((n) => [...n.attributes].map((a) => a.value));
}"""


@pytest.mark.parametrize("src", [
    "[x](javascript:alert(1))",
    '<a href="javascript:alert(1)">x</a>',
    "[x](JaVaScRiPt:alert(1))",
    "[x](java&#x09;script:alert(1))",
    "<javascript:alert(1)>",
])
def test_rendered_markdown_has_no_javascript_urls(ui_page, src):
    # (b) the sanitizer level, evaluated in the page where markdown-it and DOMPurify exist.
    values = ui_page.evaluate(RENDERED_ATTRS, src)
    assert not [v for v in values if "javascript:" in "".join(v.lower().split())], values


def test_rendered_markdown_keeps_safe_links(ui_page):
    # Guards the test above against passing because nothing renders at all.
    assert ui_page.evaluate(RENDERED_ATTRS, "[x](https://example.com/)") == ["https://example.com/"]


def test_failed_download_toasts_and_keeps_the_preview(ui_page, sim):
    # (c) cached bytes: the save itself fails after the preview rendered.
    f = sim.upload("keep.txt", b"still here")
    panel = open_preview(ui_page, f["id"])
    pre = panel.locator("pre.preview-pre")
    expect(pre).to_have_text("still here")
    ui_page.evaluate("() => { URL.createObjectURL = () => { throw new Error('boom'); }; }")
    panel.get_by_role("button", name="Download", exact=True).click()
    expect(ui_page.locator(".toast-error")).to_have_text(f"Couldn't download {f['id']}.")
    expect(pre).to_have_text("still here")
    expect(panel.get_by_role("button", name="Download", exact=True)).to_be_enabled()


def test_failed_download_of_an_unpreviewable_file_keeps_its_message(ui_page, sim):
    # (c) no cached bytes: the blob fetch fails; the "too large" message stays and a toast explains.
    f = sim.upload("big2.txt", b"a" * (2 * 1024 * 1024 + 1))
    panel = open_preview(ui_page, f["id"])
    expect(panel).to_contain_text("Too large to preview (> 2 MiB) — Download")
    ui_page.route("**/blob", lambda route: route.abort())
    panel.get_by_role("button", name="Download", exact=True).click()
    expect(ui_page.locator(".toast-error")).to_have_text("Couldn't reach the server — try again.")
    expect(panel).to_contain_text("Too large to preview (> 2 MiB) — Download")
