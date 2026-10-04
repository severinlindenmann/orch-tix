"""The public drop page at /u/{token}#{pub} (upload-links spec §18, Task 5): a stranger with only the
link zips a folder (or several files) and encrypts it in the browser, no session involved. The owner
then adopts it exactly as tests/browser/test_upload_links_web.py's flow does, but driven directly
through the crypto helpers (open_upload_link_key -> open_sealed_dek -> wrap_dek_under_mk -> POST
adopt) instead of the dialog, so this test also proves that path end to end."""
import json

import pytest
from playwright.sync_api import expect

from tests.helpers.sharing_mod import load
from tests.helpers.uploadlinks import make_link

pytestmark = pytest.mark.browser

PHONE = {"width": 390, "height": 844}


def ready(page):
    page.wait_for_load_state("networkidle")


@pytest.fixture
def stranger(browser):
    """A fresh context with no session and no cookies: someone who only has the link."""
    ctx = browser.new_context(viewport=PHONE)
    page = ctx.new_page()
    yield page
    ctx.close()


def create_link(sim, ttl="1d", label=""):
    def post_json(path, body):
        r = sim.request("POST", path, json=body)
        assert r.status_code == 201, r.text
        return r.json()

    return make_link(post_json, sim.mk, sim.key_version, label=label, ttl=ttl)


def drop_url(live_server, link):
    s = load()
    return f"{live_server.url}{link['url_path']}#{s.b64u(link['pub'])}"


def adopt(sim, link_id, link_uuid_hex, sealed_dek, file_uuid_hex):
    """The owner's side of adoption, done directly with the crypto (no dialog): recover the DEK the
    drop page sealed to the link, re-wrap it under MK, and claim the file."""
    s = load()
    priv, pub = s.open_upload_link_key(sim.mk, bytes.fromhex(link_uuid_hex),
                                       _wrapped_lpriv(sim, link_id))
    dek = s.open_sealed_dek(priv, pub, bytes.fromhex(link_uuid_hex), bytes.fromhex(file_uuid_hex), sealed_dek)
    wrapped_dek = s.wrap_dek_under_mk(sim.mk, dek, bytes.fromhex(file_uuid_hex))
    r = sim.request("POST", f"/api/upload-links/{link_id}/adopt", json={"wrapped_dek": wrapped_dek})
    assert r.status_code == 201, r.text
    return r.json()


def _wrapped_lpriv(sim, link_id):
    r = sim.request("GET", "/api/upload-links")
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["links"] if x["id"] == link_id)
    return row["wrapped_lpriv"]


def pending_of(sim, link_id):
    r = sim.request("GET", "/api/upload-links")
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["links"] if x["id"] == link_id)
    return row["pending"]


def test_drop_a_folder_of_files_and_a_note_then_owner_adopts_it(stranger, live_server, sim, tmp_path):
    link = create_link(sim, label="from a stranger")
    url = drop_url(live_server, link)

    folder = tmp_path / "vacation"
    folder.mkdir()
    (folder / "a.txt").write_bytes(b"first file")
    sub = folder / "sub"
    sub.mkdir()
    (sub / "ä.txt").write_text("second file, umlaut folder name intact", encoding="utf-8")

    stranger.goto(url)
    ready(stranger)

    stranger.locator("#drop-folder-input").set_input_files(str(folder))
    expect(stranger.locator("#drop-send")).to_be_enabled()
    stranger.locator("#drop-note").fill("a note from the drop page")
    stranger.get_by_role("button", name="Send").click()
    expect(stranger.locator(".pub-state-title")).to_have_text("Sent. You can close this tab.", timeout=15_000)

    pending = pending_of(sim, link["id"])
    assert pending is not None
    file_out = adopt(sim, link["id"], link["uuid"].hex(), pending["sealed_dek"], pending["file_uuid"])

    s = load()
    meta, dek = s.open_file_meta(sim.mk, file_out)
    assert meta["mime"] == "application/zip"
    assert meta["note"] == "a note from the drop page"

    r = sim.request("GET", f"/api/files/{file_out['id']}/blob")
    assert r.status_code == 200, r.text
    import io
    out = io.BytesIO()
    s.decrypt_stream(dek, bytes.fromhex(file_out["uuid"]), io.BytesIO(r.content), out)
    zip_bytes = out.getvalue()

    import zipfile
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    assert zf.testzip() is None
    names = set(zf.namelist())
    assert "vacation/a.txt" in names
    assert "vacation/sub/ä.txt" in names
    assert zf.read("vacation/a.txt") == b"first file"
    assert zf.read("vacation/sub/ä.txt").decode("utf-8") == "second file, umlaut folder name intact"


def test_a_stripped_fragment_shows_the_incomplete_message_and_no_form(stranger, live_server, sim):
    link = create_link(sim)
    base = f"{live_server.url}{link['url_path']}"  # the fragment (the pub key) never made it through

    stranger.goto(base)
    ready(stranger)

    expect(stranger.locator("#drop-body")).to_have_text(
        "This link is incomplete — ask the sender to copy the whole link, including the part after #.")
    expect(stranger.locator("#drop-send")).to_have_count(0)
    expect(stranger.locator("#drop-file-input")).to_have_count(0)
