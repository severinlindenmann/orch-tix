# tests/browser/test_upload_mobile.py
import base64
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .cli import make_repo, onboard_approved, run_cli
from .conftest import login_ui

pytestmark = pytest.mark.browser

# a valid 1x1 PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

PASTE_FILE_JS = """([bytes, name, type]) => {
  document.activeElement && document.activeElement.blur && document.activeElement.blur();
  const dt = new DataTransfer();
  dt.items.add(new File([new Uint8Array(bytes)], name, {type}));
  document.body.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
}"""

PASTE_TEXT_JS = """([text, targetSelector]) => {
  const target = targetSelector ? document.querySelector(targetSelector) : document.body;
  if (!targetSelector && document.activeElement && document.activeElement.blur) document.activeElement.blur();
  if (targetSelector) target.focus();
  const dt = new DataTransfer();
  dt.setData('text/plain', text);
  target.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
}"""


def cli_device(sim, live_server, tmp_path, name="cli-dev"):
    repo = make_repo(tmp_path / name)
    onboard_approved(sim, live_server.url, repo, name, "p")
    return repo


def upload_and_get_id(page, sheet):
    sheet.get_by_role("button", name="Encrypt and share").click()
    toast = page.get_by_text(re.compile(r"Uploaded FILE\d+ · "))
    expect(toast).to_be_visible(timeout=20_000)
    return re.search(r"FILE\d+", toast.text_content()).group(0)


def test_browser_upload_reaches_cli_byte_identical(ui_page, live_server, sim, tmp_path):
    repo = cli_device(sim, live_server, tmp_path)
    data = os.urandom(1024 * 1024 + 17)
    ui_page.locator("#upload-btn").click()
    sheet = ui_page.locator("dialog.upload-sheet")
    expect(sheet).to_be_visible()
    ui_page.set_input_files("#upload-input", files=[{"name": "blob.bin", "mimeType": "application/octet-stream", "buffer": data}])
    ui_page.fill("#upload-note", "from the phone")
    fid = upload_and_get_id(ui_page, sheet)
    expect(ui_page.locator(f'[data-id="{fid}"]').first).to_contain_text("blob.bin")

    r = run_cli(repo, live_server.url, "get", fid, "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert Path(out["path"]).read_bytes() == data
    assert out["note"] == "from the phone"
    assert out["project"] == "web"


def test_paste_screenshot_shows_thumbnail_and_uploads_with_edited_name(ui_page, live_server, sim, tmp_path):
    repo = cli_device(sim, live_server, tmp_path, "shot-dev")
    ui_page.evaluate(PASTE_FILE_JS, [list(PNG), "image.png", "image/png"])
    sheet = ui_page.locator("dialog.upload-sheet")
    expect(sheet).to_be_visible()
    expect(sheet.locator("img.upload-thumb")).to_be_visible()
    assert ui_page.evaluate("document.querySelector('img.upload-thumb').src").startswith("blob:")
    name = sheet.locator("#upload-name-0")
    expect(name).to_have_value(re.compile(r"^screenshot-\d{8}-\d{6}\.png$"))
    name.fill("login-bug.png")
    fid = upload_and_get_id(ui_page, sheet)

    r = run_cli(repo, live_server.url, "get", fid, "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["name"] == "login-bug.png" and out["mime"] == "image/png"
    assert Path(out["path"]).read_bytes() == PNG


def test_paste_json_text_becomes_a_json_file(ui_page):
    ui_page.evaluate(PASTE_TEXT_JS, ['{"a": 1, "b": [2]}', None])
    sheet = ui_page.locator("dialog.upload-sheet")
    expect(sheet.locator("#upload-name-0")).to_have_value(re.compile(r"^pasted-\d{8}-\d{6}\.json$"))
    expect(sheet.locator("pre.upload-snippet")).to_contain_text('{"a": 1, "b": [2]}')


def test_paste_in_the_note_field_stays_a_text_paste(ui_page):
    ui_page.locator("#upload-btn").click()
    sheet = ui_page.locator("dialog.upload-sheet")
    expect(sheet).to_be_visible()
    ui_page.evaluate(PASTE_TEXT_JS, ["just a note", "#upload-note"])
    expect(ui_page.locator("dialog.upload-sheet")).to_have_count(1)
    expect(sheet.locator(".upload-name")).to_have_count(0)


def test_paste_in_the_search_box_does_not_upload(ui_page):
    ui_page.evaluate(PASTE_TEXT_JS, ["FILE7", "#search"])
    expect(ui_page.locator("dialog.upload-sheet")).to_have_count(0)


def test_drop_anywhere_shows_overlay_then_sheet(ui_page):
    ui_page.evaluate(
        """() => {
          window.__dt = new DataTransfer();
          window.__dt.items.add(new File(['hello'], 'drop.txt', {type: 'text/plain'}));
          document.body.dispatchEvent(new DragEvent('dragenter', {dataTransfer: window.__dt, bubbles: true, cancelable: true}));
        }"""
    )
    expect(ui_page.locator(".drop-overlay")).to_be_visible()
    expect(ui_page.locator(".drop-overlay")).to_contain_text("Drop to encrypt & share")
    ui_page.evaluate(
        """() => {
          for (const t of ['dragover', 'drop'])
            document.querySelector('.list-head').dispatchEvent(new DragEvent(t, {dataTransfer: window.__dt, bubbles: true, cancelable: true}));
        }"""
    )
    expect(ui_page.locator(".drop-overlay")).to_be_hidden()
    sheet = ui_page.locator("dialog.upload-sheet")
    expect(sheet.locator("#upload-name-0")).to_have_value("drop.txt")
    expect(sheet.locator("pre.upload-snippet")).to_contain_text("hello")
    expect(sheet.get_by_role("button", name="Encrypt and share")).to_be_enabled()


def test_session_expiry_shows_banner_and_clears_keys(ui_page):
    # A startup request still in flight could land a renewed session cookie after the clear.
    ui_page.wait_for_load_state("networkidle")
    ui_page.context.clear_cookies()
    ui_page.reload()
    banner = ui_page.locator(".banner-session")
    expect(banner).to_contain_text("Session expired — log in again")
    expect(banner.get_by_role("link", name="Log in")).to_have_attribute("href", "/login?next=%2Ffiles")   # back to this page after signing in
    expect(ui_page.locator(".banner-decrypt")).to_have_count(0)
    count_js = """() => new Promise((resolve) => {
      const q = indexedDB.open('fileshare');
      q.onsuccess = () => {
        const db = q.result;
        if (!db.objectStoreNames.contains('keys')) { resolve(0); return; }
        const c = db.transaction('keys', 'readonly').objectStore('keys').count();
        c.onsuccess = () => resolve(c.result);
      };
    })"""
    for _ in range(30):
        if ui_page.evaluate(count_js) == 0:
            break
        time.sleep(0.1)
    assert ui_page.evaluate(count_js) == 0


def test_undecryptable_row_shows_decrypt_banner_not_session(ui_page, live_server, sim):
    a = sim.upload("a.txt", b"a")
    b = sim.upload("b.txt", b"b")
    db = sqlite3.connect(live_server.data_dir / "fileshare.db")
    with db:
        db.execute("UPDATE files SET enc_meta = (SELECT enc_meta FROM files WHERE n = ?) WHERE n = ?", (a["n"], b["n"]))
    db.close()
    ui_page.reload()
    banner = ui_page.locator(".banner-decrypt")
    expect(banner).to_contain_text(f"Couldn't decrypt {b['id']}")
    expect(ui_page.locator(".banner-session")).to_have_count(0)
    expect(ui_page.locator(f'[data-id="{a["id"]}"]').first).to_contain_text("a.txt")


# Both outage tests first wait for the page's own startup requests (the files list, the pending-
# devices check) to finish: set_offline doesn't abort a request already in flight, and one landing
# a 200 after the outage began dispatches fs:network-ok, which rightly hides the banner again.
def test_network_error_banner(ui_page):
    ui_page.wait_for_load_state("networkidle")
    ui_page.context.set_offline(True)
    ui_page.evaluate("() => window.dispatchEvent(new CustomEvent('fs:files-changed'))")
    expect(ui_page.locator(".banner-network")).to_contain_text("Can't reach tix.severin.io")
    expect(ui_page.locator(".banner-session")).to_have_count(0)
    ui_page.context.set_offline(False)


def test_network_banner_clears_once_the_server_answers_again(ui_page):
    # The server is down while the phone stays online, so no `online` event will ever fire:
    # the next successful request must clear the banner on its own.
    ui_page.wait_for_load_state("networkidle")
    # All API traffic, so the 10 s pending-devices poll can't land a success mid-outage either.
    ui_page.route("**/api/**", lambda route: route.abort())
    ui_page.evaluate("() => window.dispatchEvent(new CustomEvent('fs:files-changed'))")
    expect(ui_page.locator(".banner-network")).to_contain_text("Can't reach tix.severin.io")
    ui_page.unroute("**/api/**")
    ui_page.evaluate("() => window.dispatchEvent(new CustomEvent('fs:files-changed'))")
    expect(ui_page.locator(".banner-network")).to_have_count(0)


def test_mobile_layout_and_paste_fallback(browser, live_server, sim):
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    login_ui(page, live_server.url, sim.passphrase)
    f = sim.upload("m.md", b"# Mobile\n\nhello")
    page.reload()
    page.wait_for_load_state("networkidle")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    # The action dock sits above the tab bar: Upload (primary), Paste, Record, Note.
    dock = page.locator("#dock")
    for name in ("Upload", "Paste from clipboard", "Record audio", "New note"):
        expect(dock.get_by_role("button", name=name)).to_be_visible()
    dock_box = dock.bounding_box()
    nav_box = page.get_by_role("navigation", name="Main").bounding_box()
    assert dock_box["y"] + dock_box["height"] <= nav_box["y"] + 0.5
    assert nav_box["y"] + nav_box["height"] <= 844 + 0.5

    # A tap opens the full-screen file view.
    page.locator(f'.frow[data-id="{f["id"]}"]').click()
    view = page.locator("#detail.is-open")
    box = view.bounding_box()
    assert box["x"] == 0 and abs(box["width"] - 390) < 1 and abs(box["height"] - 844) < 1
    # §1.3: the phone actually renders the decrypted Markdown, not just an empty full-width panel.
    expect(page.frame_locator("#detail iframe.preview-md").locator("h1")).to_have_text("Mobile")
    view.get_by_role("button", name="Back to files").click()
    expect(page.locator("#detail.is-open")).to_have_count(0)

    # No async clipboard API (or permission denied) -> the textarea fallback.
    page.evaluate("() => Object.defineProperty(navigator, 'clipboard', {value: {}, configurable: true})")
    dock.get_by_role("button", name="Paste from clipboard").click()
    fallback = page.locator("dialog.paste-fallback")
    expect(fallback).to_be_visible()
    page.fill("#paste-text", "hello from the phone")
    fallback.get_by_role("button", name="Use this text").click()
    sheet = page.locator("dialog.upload-sheet")
    expect(sheet.locator("#upload-name-0")).to_have_value(re.compile(r"^pasted-\d{8}-\d{6}\.txt$"))
    sheet_box = sheet.bounding_box()
    assert sheet_box["width"] <= 390
    assert abs(sheet_box["y"] + sheet_box["height"] - 844) < 1  # a bottom sheet
    ctx.close()


def test_dock_sits_in_the_list_header_on_desktop(ui_page):
    ui_page.wait_for_load_state("networkidle")
    head = ui_page.locator(".list-head").bounding_box()
    for sel in ("#upload-btn", "#paste-btn", "#record-btn"):
        b = ui_page.locator(sel)
        expect(b).to_be_visible()
        box = b.bounding_box()
        assert head["y"] <= box["y"] and box["y"] + box["height"] <= head["y"] + head["height"], sel
    expect(ui_page.locator("#dock .dock-label")).to_have_text("Upload")
