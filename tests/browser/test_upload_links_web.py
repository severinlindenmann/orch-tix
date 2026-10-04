"""Inbound one-time upload links, owner web UI (spec §18, Task 4): the "Upload links" dialog
(create/list/revoke) and the files page auto-adopting a pending upload a stranger dropped, following
tests/browser/test_links_web.py's fixtures. The drop itself uses the real crypto through
tests/helpers/uploadlinks.py's `drop`, exactly as Task 5's drop page and Task 6's CLI will."""
import json
import re

import httpx
import pytest
from playwright.sync_api import expect

from tests.helpers.uploadlinks import drop
from tests.helpers.sharing_mod import load

from .conftest import login_ui

pytestmark = pytest.mark.browser

DESKTOP = {"width": 1024, "height": 720}


def ready(page):
    page.wait_for_load_state("networkidle")


@pytest.fixture
def desk(browser, live_server, sim):
    ctx = browser.new_context(viewport=DESKTOP)
    page = ctx.new_page()
    login_ui(page, live_server.url, sim.passphrase)
    yield page
    ctx.close()


def open_dialog(page):
    page.get_by_role("button", name="Upload links").click()
    dlg = page.locator("dialog.droplink-sheet")
    expect(dlg).to_be_visible()
    return dlg


def drop_a_file(live_server, token, pub, data, name, mime, note=""):
    """A stranger with only the link, no session, no cookies: a fresh HTTP client straight against
    the public endpoints, driving tests/helpers/uploadlinks.drop's injected callables."""
    with httpx.Client(base_url=live_server.url) as client:
        def get_json(path):
            r = client.get(path)
            assert r.status_code == 200, r.text
            return r.json()

        def post_multipart(path, meta, blob):
            r = client.post(path, data={"meta": json.dumps(meta)},
                            files={"blob": ("blob", blob, "application/octet-stream")})
            assert r.status_code == 201, r.text
            return r.json()

        return drop(post_multipart, get_json, token, pub, data, name, mime, note)


def test_create_a_link_drop_a_file_and_see_it_adopted(desk, live_server, sim):
    s = load()
    dlg = open_dialog(desk)
    expect(dlg.locator(".links-list .empty-inline")).to_have_text("No upload links yet.")

    dlg.locator("#droplink-ttl").select_option("1d")
    dlg.locator("#droplink-label").fill("from test")
    dlg.get_by_role("button", name="Create link").click()

    field = dlg.locator(".link-url")
    expect(field).to_have_value(re.compile(rf"^{re.escape(live_server.url)}/u/[A-Za-z0-9_-]{{43}}#[A-Za-z0-9_-]{{43,}}$"))
    url = field.input_value()
    base, frag = url.split("#", 1)
    token = base.split("/u/")[1]
    pub = s.unb64u(frag)

    expect(dlg.locator(".link-row-head")).to_have_text("from test · Waiting")

    made = drop_a_file(live_server, token, pub, b"hello from a stranger", "note.txt", "text/plain")
    assert made["ok"] is True

    dlg.locator(".close-btn").click()
    expect(desk.locator("dialog.droplink-sheet")).to_have_count(0)

    desk.reload()
    ready(desk)
    row = desk.locator("#file-list .frow", has_text="note.txt")
    expect(row).to_have_count(1)
    expect(row.locator(".device")).to_have_text("upload link")

    dlg2 = open_dialog(desk)
    expect(dlg2.locator(".link-row-head")).to_have_text(re.compile(r"^from test · Received as FILE\d+$"))


def test_revoke_asks_first_and_removes_a_waiting_link(desk, sim):
    dlg = open_dialog(desk)
    dlg.locator("#droplink-label").fill("to revoke")
    dlg.get_by_role("button", name="Create link").click()
    expect(dlg.locator(".link-row-head")).to_have_text("to revoke · Waiting")

    dlg.get_by_role("button", name="Revoke").click()
    confirm = desk.locator("dialog.confirm")
    expect(confirm).to_contain_text("Revoke this upload link?")
    confirm.get_by_role("button", name="Cancel").click()
    expect(dlg.locator(".link-row-head")).to_have_text("to revoke · Waiting")

    dlg.get_by_role("button", name="Revoke").click()
    desk.locator("dialog.confirm").get_by_role("button", name="Revoke").click()
    expect(dlg.locator(".link-row-head")).to_have_text("to revoke · Revoked")
    assert sim.request("GET", "/api/upload-links").json()["links"][0]["state"] == "revoked"
