import base64
import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.helpers.blobs import make_fake_blob

from .conftest import login_ui

pytestmark = pytest.mark.browser
PASS = "correct horse battery"  # BrowserSim's default passphrase (Task 13)
BODY = b"# Deploy notes\n"
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


@pytest.fixture
def sim(sim):
    """The shared owner sim from tests/conftest.py (Task 13), seeded with one Markdown file."""
    sim.upload("deploy-notes.md", BODY, note="for the demo")
    return sim


def login(page, live_server):
    login_ui(page, live_server.url, PASS)


def rows(page):
    return page.locator("#file-list .frow")


def open_file(page, file_id):
    """Click the row; on a desktop the detail pane shows it."""
    page.locator(f'.frow[data-id="{file_id}"]').click()
    detail = page.locator("#detail.is-open")
    expect(detail).to_be_visible()
    return detail


def test_login_shows_decrypted_name_note_size_and_device(page, live_server, sim):
    login(page, live_server)
    row = rows(page).filter(has_text="deploy-notes.md")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("for the demo")
    expect(row).to_contain_text("FILE1")
    expect(row).to_contain_text("browser")
    expect(row.locator(".tile")).to_have_class(re.compile(r"\bt-text\b"))  # Markdown gets the text tile
    expect(page.locator("#file-list time").first).to_have_attribute("datetime", re.compile(r"^\d{4}-\d\d-\d\dT"))
    detail = open_file(page, "FILE1")
    expect(detail.locator(".detail-meta")).to_contain_text("15 B")
    expect(detail.locator(".detail-name")).to_have_text("deploy-notes.md")


def test_wrong_passphrase_stays_on_login(page, live_server, sim):
    page.goto(live_server.url + "/login")
    page.locator("#passphrase").fill("not the passphrase")
    page.get_by_role("button", name="Log in").click()
    expect(page.locator("#login-error")).to_have_text("Wrong passphrase.")
    assert page.url.startswith(live_server.url + "/login")


def test_copy_id(page, live_server, sim):
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live_server.url)
    login(page, live_server)
    open_file(page, "FILE1").get_by_role("button", name="Copy FILE1").click()
    expect(page.locator(".toast")).to_contain_text("Copied FILE1")
    assert page.evaluate("navigator.clipboard.readText()") == "FILE1"


def test_download_decrypts_in_the_browser(page, live_server, sim):
    login(page, live_server)
    detail = open_file(page, "FILE1")
    download = detail.get_by_role("button", name="Download", exact=True)
    expect(download).to_be_enabled()
    with page.expect_download() as info:
        download.click()
    download = info.value
    assert download.suggested_filename == "deploy-notes.md"
    assert Path(download.path()).read_bytes() == BODY


def delete_from_menu(page, detail):
    # Desktop: Delete sits in the file view's "More actions" menu.
    detail.get_by_role("button", name="More actions").click()
    detail.get_by_role("menuitem", name="Delete").click()


def test_delete_needs_confirmation_and_leaves_a_tombstone(page, live_server, sim):
    login(page, live_server)
    detail = open_file(page, "FILE1")
    delete_from_menu(page, detail)
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("Delete FILE1?")
    expect(dialog).to_contain_text("deploy-notes.md")
    dialog.get_by_role("button", name="Cancel").click()
    expect(rows(page)).to_have_count(1)

    delete_from_menu(page, detail)
    page.get_by_role("dialog").get_by_role("button", name="Delete").click()
    expect(rows(page)).to_have_count(0)
    expect(page.locator("#empty")).to_be_visible()
    expect(page.locator("#detail.is-open")).to_have_count(0)
    assert page.request.get(live_server.url + "/api/files/FILE1").status == 410
    page.reload()
    expect(page.locator("#empty")).to_be_visible()


def test_search_and_type_filter(page, live_server, sim):
    sim.upload("config.json", b'{"a": 1}', note="prod config")
    login(page, live_server)
    expect(rows(page)).to_have_count(2)
    page.locator("#search").fill("prod")
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text("config.json")
    page.locator("#search").fill("")
    sim.upload("dot.png", base64.b64decode(PNG_B64))
    page.reload()
    expect(rows(page)).to_have_count(3)
    page.get_by_role("button", name="Images", exact=True).click()
    expect(page.get_by_role("button", name="Images", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text("dot.png")
    page.get_by_role("button", name="Text", exact=True).click()  # Markdown and JSON both count as text
    expect(rows(page)).to_have_count(2)
    page.get_by_role("button", name="All", exact=True).click()
    expect(rows(page)).to_have_count(3)


def test_load_more_pages_in_fifty(page, live_server, sim):
    for i in range(50):
        sim.upload(f"f{i}.txt", b"x")
    login(page, live_server)
    expect(rows(page)).to_have_count(50)
    page.get_by_role("button", name="Load more").click()
    expect(rows(page)).to_have_count(51)
    expect(page.get_by_role("button", name="Load more")).to_be_hidden()


def test_undecryptable_file_renders_a_badge_not_a_crash(page, live_server, sim, sharing):
    # A file wrapped under a different MK, uploaded through the owner's own session.
    wrong = sharing.new_file_crypto(b"\x01" * 32, "secret.md", "text/markdown", "")
    meta = {"uuid": wrong["uuid_hex"], "key_version": 1, "wrapped_dek": wrong["wrapped_dek"], "enc_meta": wrong["enc_meta"]}
    up = sim.http.post("/api/files", data={"meta": json.dumps(meta)},
                       files={"blob": ("blob", make_fake_blob(wrong["uuid_hex"]), "application/octet-stream")})
    assert up.status_code == 201, up.text
    login(page, live_server)
    expect(rows(page).filter(has_text="FILE2")).to_contain_text("Can't decrypt")
    expect(rows(page).filter(has_text="deploy-notes.md")).to_have_count(1)
    detail = open_file(page, "FILE2")
    expect(detail).to_contain_text("Couldn't decrypt FILE2")
    expect(detail.get_by_role("button", name="Download", exact=True)).to_be_disabled()


def test_phone_width_shows_rows_and_a_file_view_with_big_targets(page, live_server, sim):
    page.set_viewport_size({"width": 390, "height": 844})
    login(page, live_server)
    row = rows(page)
    expect(row).to_have_count(1)
    expect(row.first).to_contain_text("deploy-notes.md")
    expect(row.first).to_contain_text("for the demo")
    assert row.first.bounding_box()["height"] >= 44
    page.wait_for_load_state("networkidle")
    row.first.click()
    detail = page.locator("#detail.is-open")
    for name in ("Delete", "Download", "Copy FILE1", "Mark as done", "More actions", "Back to files"):
        box = detail.get_by_role("button", name=name, exact=True).bounding_box()
        assert box["height"] >= 44, name
