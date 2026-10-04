# tests/browser/test_recorder.py — "Record audio" end to end with Chromium's fake microphone.
import json
import re
import struct
from pathlib import Path

import pytest
from playwright.sync_api import expect

from .cli import make_repo, onboard_approved, run_cli
from .conftest import login_ui

pytestmark = pytest.mark.browser

EBML_MAGIC = bytes.fromhex("1A45DFA3")

# Keeps a handle on every stream the page opens, so the test can check the tracks were stopped.
TRACK_STREAMS = """(() => {
  const md = navigator.mediaDevices;
  const orig = md.getUserMedia.bind(md);
  window.__streams = [];
  md.getUserMedia = async (c) => { const s = await orig(c); window.__streams.push(s); return s; };
})();"""
DENY_MIC = """navigator.mediaDevices.getUserMedia = () =>
  Promise.reject(new DOMException("Permission denied", "NotAllowedError"));"""
ALL_TRACKS_ENDED = """() => window.__streams.length > 0 &&
  window.__streams.every((s) => s.getTracks().every((t) => t.readyState === "ended"))"""


@pytest.fixture(autouse=True)
def _chromium_only(browser_name):
    if browser_name != "chromium":
        pytest.skip("the fake media stream flags are Chromium-only")


# No service worker in these tests: they mock POST /api/files with page.route (abort, 500, 401), and Playwright
# does not intercept a request that goes through a service worker's fetch handler. The app's worker registers on
# the first page (/ after login) and claims the page once it has precached the shell; on a slow runner that claim
# lands inside a short (1.0 s) recording, and the upload then reached the real server (CI runs 37173506202 and
# 37190195888: FILE1 created, no queued row, no error). Nothing here is about the worker; test_offline.py and
# test_pwa*.py cover it with service workers allowed.
@pytest.fixture
def browser_context_args(browser_context_args):
    return {**browser_context_args, "service_workers": "block"}


# The fake microphone flags are set for the whole session in tests/browser/conftest.py: the
# session-scoped `browser` is launched once, so a per-module launch-args override would be ignored.
@pytest.fixture
def mic_page(page, live_server, sim):
    page.context.grant_permissions(["microphone"])
    page.add_init_script(TRACK_STREAMS)
    login_ui(page, live_server.url, sim.passphrase)
    return page


def file_ids(sim):
    return [f["id"] for f in sim.request("GET", "/api/files").json()["files"]]


def record(page, seconds=1.5):
    page.get_by_role("button", name="Record audio").click()
    dlg = page.locator("dialog.recorder")
    expect(dlg).to_be_visible()
    expect(dlg.locator(".recorder-status")).to_have_text("Recording…")
    page.wait_for_timeout(int(seconds * 1000))
    expect(dlg.locator(".recorder-timer")).not_to_have_text("00:00")
    return dlg


def test_record_stop_uploads_an_encrypted_webm(mic_page, live_server, sim, tmp_path):
    repo = make_repo(tmp_path / "rec-dev")
    onboard_approved(sim, live_server.url, repo, "rec-dev", "p")
    uploads = []
    mic_page.on("request", lambda r: uploads.append(r) if r.method == "POST" and r.url.endswith("/api/files") else None)

    dlg = record(mic_page)
    dlg.get_by_role("button", name="Stop & upload").click()

    toast = mic_page.get_by_text(re.compile(r"Uploaded FILE\d+"))
    expect(toast).to_be_visible(timeout=20_000)
    fid = re.search(r"FILE\d+", toast.text_content()).group(0)
    expect(mic_page.locator("dialog.recorder")).to_have_count(0)
    row = mic_page.locator(f'[data-id="{fid}"]').first
    expect(row).to_contain_text(re.compile(r"recording-\d{8}-\d{6}\.webm"))
    mic_page.wait_for_function(ALL_TRACKS_ENDED)
    assert len(uploads) == 1
    # What reached the server is ciphertext: the stored blob carries no WebM header.
    blobs = list((live_server.data_dir / "blobs").rglob("*.shr"))
    assert len(blobs) == 1 and EBML_MAGIC not in blobs[0].read_bytes()

    r = run_cli(repo, live_server.url, "get", fid, "--json")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert re.fullmatch(r"recording-\d{8}-\d{6}\.webm", Path(out["path"]).name)
    plain = Path(out["path"]).read_bytes()
    assert plain[:4] == EBML_MAGIC
    assert len(plain) > 1000
    assert out["mime"] == "audio/webm"


def test_cancel_uploads_nothing_and_releases_the_mic(mic_page, sim):
    before = file_ids(sim)
    dlg = record(mic_page, 1.2)
    dlg.get_by_role("button", name="Cancel").click()
    expect(mic_page.locator("dialog.recorder")).to_have_count(0)
    mic_page.wait_for_function(ALL_TRACKS_ENDED)
    mic_page.wait_for_timeout(1500)  # a stray final chunk must not start an upload
    assert file_ids(sim) == before


def test_escape_discards_the_recording(mic_page, sim):
    before = file_ids(sim)
    record(mic_page, 1.0)
    mic_page.keyboard.press("Escape")
    expect(mic_page.locator("dialog.recorder")).to_have_count(0)
    mic_page.wait_for_function(ALL_TRACKS_ENDED)
    mic_page.wait_for_timeout(1500)
    assert file_ids(sim) == before


def test_denied_permission_shows_an_inline_error(page, live_server, sim):
    page.add_init_script(DENY_MIC)
    login_ui(page, live_server.url, sim.passphrase)
    page.get_by_role("button", name="Record audio").click()
    dlg = page.locator("dialog.recorder")
    expect(dlg.locator(".recorder-error")).to_have_text(
        "Microphone access was denied — allow it in the browser settings")
    expect(dlg.get_by_role("button", name="Stop & upload")).to_be_disabled()
    dlg.get_by_role("button", name="Close").click()
    expect(dlg).to_have_count(0)


def test_no_media_recorder_says_unsupported(page, live_server, sim):
    page.add_init_script("delete window.MediaRecorder;")
    login_ui(page, live_server.url, sim.passphrase)
    page.get_by_role("button", name="Record audio").click()
    expect(page.locator("dialog.recorder .recorder-error")).to_have_text("Recording isn't supported in this browser")


def test_phone_dock_opens_the_recorder(browser, live_server, sim):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, permissions=["microphone"])
    page = ctx.new_page()
    page.add_init_script(TRACK_STREAMS)
    login_ui(page, live_server.url, sim.passphrase)
    page.wait_for_load_state("networkidle")
    page.locator("#dock").get_by_role("button", name="Record audio").click()
    dlg = page.locator("dialog.recorder")
    expect(dlg.locator(".recorder-status")).to_have_text("Recording…")
    box = dlg.bounding_box()
    assert box["width"] <= 390 and box["y"] + box["height"] <= 844 + 0.5
    dlg.get_by_role("button", name="Cancel").click()
    page.wait_for_function(ALL_TRACKS_ENDED)
    ctx.close()


def _wav(seconds=0.2, rate=8000) -> bytes:
    n = int(seconds * rate)
    data = bytes(128 for _ in range(n))  # 8-bit unsigned silence
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate, 1, 8)
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
            + b"data" + struct.pack("<I", len(data)) + data)


def test_audio_file_previews_from_a_blob_url_and_revokes_it(ui_page, sim):
    f = sim.upload("memo.wav", _wav())
    ui_page.reload()
    violations = []
    ui_page.on("console", lambda m: violations.append(m.text) if "Content Security Policy" in m.text else None)
    ui_page.evaluate("""() => {
      window.__revoked = [];
      const orig = URL.revokeObjectURL.bind(URL);
      URL.revokeObjectURL = (u) => { window.__revoked.push(u); orig(u); };
    }""")
    ui_page.wait_for_load_state("networkidle")
    ui_page.locator(f'.frow[data-id="{f["id"]}"]').click()
    audio = ui_page.locator("#detail audio")
    expect(audio).to_have_count(1)
    src = audio.get_attribute("src")
    assert src.startswith("blob:")
    assert audio.get_attribute("controls") == ""
    assert audio.get_attribute("preload") == "metadata"
    ui_page.wait_for_function("() => document.querySelector('#detail audio').readyState >= 1")
    assert violations == []
    ui_page.keyboard.press("Escape")
    assert src in ui_page.evaluate("window.__revoked")


def test_audio_over_the_2_mib_text_cap_still_previews(ui_page, sim):
    f = sim.upload("long-memo.wav", _wav(seconds=400))  # ~3.2 MB, under AUDIO_PREVIEW_MAX
    ui_page.reload()
    ui_page.wait_for_load_state("networkidle")
    ui_page.locator(f'.frow[data-id="{f["id"]}"]').click()
    expect(ui_page.locator("#detail audio")).to_have_count(1, timeout=15_000)
    expect(ui_page.locator("#detail.is-open")).not_to_contain_text("Too large")


def test_recording_auto_stops_and_uploads_near_the_size_limit(mic_page, sim):
    limit = 16_000  # a few seconds of opus
    mic_page.evaluate(f"document.body.dataset.maxUpload = '{limit}'")
    mic_page.get_by_role("button", name="Record audio").click()
    expect(mic_page.get_by_text("The recording reached the upload limit — uploading it now")).to_be_visible(timeout=30_000)
    toast = mic_page.get_by_text(re.compile(r"Uploaded FILE\d+"))
    expect(toast).to_be_visible(timeout=20_000)
    expect(mic_page.locator("dialog.recorder")).to_have_count(0)
    mic_page.wait_for_function(ALL_TRACKS_ENDED)
    fid = re.search(r"FILE\d+", toast.text_content()).group(0)
    assert 0 < sim.get_file(fid)["size"] <= limit


UPLOAD_FAILED = "Upload failed — your recording is kept. Retry when you're back online."


# A server error keeps the recording in the recorder with Retry. (A network error queues it in the
# encrypted outbox instead: test_a_network_error_queues_the_recording, and test_offline.py.)
def _fail_uploads(page):
    def handle(route):
        if route.request.method != "POST":
            route.continue_()
            return
        route.fulfill(status=500, content_type="application/json",
                      body=json.dumps({"error": "http_500", "detail": "boom"}))
    page.route("**/api/files", handle)


def test_a_network_error_queues_the_recording(mic_page, sim):
    before = file_ids(sim)
    mic_page.route("**/api/files", lambda route: route.abort() if route.request.method == "POST" else route.continue_())
    dlg = record(mic_page, 1.0)
    dlg.get_by_role("button", name="Stop & upload").click()
    expect(mic_page.locator("dialog.recorder")).to_have_count(0, timeout=20_000)
    row = mic_page.locator("#outbox-slot .frow-pending")
    expect(row).to_contain_text(re.compile(r"recording-\d{8}-\d{6}\.webm"))
    assert file_ids(sim) == before
    mic_page.unroute("**/api/files")
    mic_page.locator("#outbox-retry").click()
    expect(mic_page.locator("#outbox-slot")).to_be_hidden(timeout=20_000)
    assert len(file_ids(sim)) == len(before) + 1


def test_failed_upload_keeps_the_recording_and_retry_uploads_it(mic_page, sim):
    before = file_ids(sim)
    _fail_uploads(mic_page)
    dlg = record(mic_page)
    dlg.get_by_role("button", name="Stop & upload").click()
    expect(dlg.locator(".recorder-error")).to_have_text(UPLOAD_FAILED, timeout=20_000)
    expect(dlg).to_be_visible()
    expect(dlg.get_by_role("button", name="Discard")).to_be_enabled()
    mic_page.wait_for_function(ALL_TRACKS_ENDED)  # retrying never records again
    mic_page.keyboard.press("Escape")  # a stray Escape must not drop the kept recording
    expect(dlg).to_be_visible()
    assert file_ids(sim) == before
    expect(mic_page.locator("#file-list .frow").filter(has_text="recording-")).to_have_count(0)

    mic_page.unroute("**/api/files")
    dlg.get_by_role("button", name="Retry").click()
    toast = mic_page.get_by_text(re.compile(r"Uploaded FILE\d+"))
    expect(toast).to_be_visible(timeout=20_000)
    expect(mic_page.locator("dialog.recorder")).to_have_count(0)
    fid = re.search(r"FILE\d+", toast.text_content()).group(0)
    expect(mic_page.locator(f'[data-id="{fid}"]').first).to_contain_text(re.compile(r"recording-\d{8}-\d{6}\.webm"))
    assert set(file_ids(sim)) == {fid, *before}


def test_discard_after_a_failed_upload_uploads_nothing(mic_page, sim):
    before = file_ids(sim)
    _fail_uploads(mic_page)
    dlg = record(mic_page, 1.0)
    dlg.get_by_role("button", name="Stop & upload").click()
    expect(dlg.locator(".recorder-error")).to_have_text(UPLOAD_FAILED, timeout=20_000)
    mic_page.unroute("**/api/files")
    dlg.get_by_role("button", name="Discard").click()
    expect(mic_page.locator("dialog.recorder")).to_have_count(0)
    mic_page.wait_for_timeout(1000)
    assert file_ids(sim) == before


def test_pagehide_releases_the_mic(mic_page):
    record(mic_page, 0.5)
    mic_page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide'))")
    mic_page.wait_for_function(ALL_TRACKS_ENDED)
    expect(mic_page.locator("dialog.recorder")).to_have_count(0)


SESSION_EXPIRED = "Your session expired — the recording is kept here. Log in in a new tab, then press Retry."
# Counts the page's own window beforeunload listeners (added minus removed).
TRACK_BEFOREUNLOAD = """(() => {
  window.__beforeunload = 0;
  const add = window.addEventListener, rm = window.removeEventListener;
  window.addEventListener = function (t, ...a) { if (t === "beforeunload") window.__beforeunload++; return add.call(this, t, ...a); };
  window.removeEventListener = function (t, ...a) { if (t === "beforeunload") window.__beforeunload--; return rm.call(this, t, ...a); };
})();"""


def _expire_session_once(page):
    used = []

    def handle(route):
        if route.request.method != "POST" or used:
            route.continue_()
            return
        used.append(1)
        route.fulfill(status=401, content_type="application/json",
                      body=json.dumps({"error": "unauthenticated", "detail": "session expired"}))
    page.route("**/api/files", handle)


def _stop_into_session_expired(page):
    page.add_init_script(TRACK_BEFOREUNLOAD)
    page.reload()
    _expire_session_once(page)
    dlg = record(page)
    dlg.get_by_role("button", name="Stop & upload").click()
    expect(dlg.locator(".recorder-error")).to_have_text(SESSION_EXPIRED, timeout=20_000)
    return dlg


def test_session_expiry_keeps_the_recording_and_guards_leaving(mic_page, sim):
    before = file_ids(sim)
    dlg = _stop_into_session_expired(mic_page)
    link = dlg.get_by_role("link", name="Open login in a new tab")
    expect(link).to_be_visible()
    assert link.get_attribute("href") == "/login"
    assert link.get_attribute("target") == "_blank"
    assert link.get_attribute("rel") == "noopener"
    expect(dlg.get_by_role("button", name="Retry")).to_be_enabled()
    assert mic_page.evaluate("window.__beforeunload") == 1
    assert file_ids(sim) == before
    # Leaving now really asks first (the banner's "Log in" link would otherwise drop the recording).
    dialogs = []
    mic_page.on("dialog", lambda d: (dialogs.append(d.type), d.dismiss()))
    mic_page.close(run_before_unload=True)
    mic_page.wait_for_timeout(500)
    assert dialogs == ["beforeunload"]


def test_retry_after_logging_in_in_another_tab_uploads_the_recording(mic_page, live_server, sim):
    before = file_ids(sim)
    dlg = _stop_into_session_expired(mic_page)
    # The banner cleared the keys: a Retry before logging in keeps the recording and says so again.
    mic_page.wait_for_function("""() => new Promise((ok) => {
      const r = indexedDB.open("fileshare"); r.onsuccess = () => {
        const g = r.result.transaction("keys").objectStore("keys").get("current");
        g.onsuccess = () => ok(g.result === undefined); };
    })""")
    dlg.get_by_role("button", name="Retry").click()
    expect(dlg.locator(".recorder-error")).to_have_text(SESSION_EXPIRED)
    assert file_ids(sim) == before

    other = mic_page.context.new_page()
    login_ui(other, live_server.url, sim.passphrase)
    other.close()
    dlg.get_by_role("button", name="Retry").click()
    toast = mic_page.get_by_text(re.compile(r"Uploaded FILE\d+"))
    expect(toast).to_be_visible(timeout=20_000)
    expect(mic_page.locator("dialog.recorder")).to_have_count(0)
    fid = re.search(r"FILE\d+", toast.text_content()).group(0)
    assert set(file_ids(sim)) == {fid, *before}
    assert mic_page.evaluate("window.__beforeunload") == 0


# The Screen Wake Lock API, faked so the test sees every lock the page takes and whether it let go.
FAKE_WAKE_LOCK = """(() => {
  window.__locks = [];
  Object.defineProperty(navigator, "wakeLock", { configurable: true, value: {
    request: async (type) => {
      const s = new EventTarget();
      s.type = type;
      s.released = false;
      s.release = async () => { s.released = true; };
      window.__locks.push(s);
      return s;
    },
  } });
})();"""
LOCKS = "() => window.__locks.map((s) => ({ type: s.type, released: s.released }))"


@pytest.fixture
def awake_page(mic_page):
    mic_page.add_init_script(FAKE_WAKE_LOCK)
    mic_page.reload()
    return mic_page


def test_the_screen_stays_on_while_recording_and_uploading(awake_page, sim):
    assert awake_page.evaluate(LOCKS) == []            # nothing held before recording starts
    dlg = record(awake_page)
    assert awake_page.evaluate(LOCKS) == [{"type": "screen", "released": False}]
    dlg.get_by_role("button", name="Stop & upload").click()
    expect(awake_page.get_by_text(re.compile(r"Uploaded FILE\d+"))).to_be_visible(timeout=20_000)
    awake_page.wait_for_function("() => window.__locks.length === 1 && window.__locks[0].released")


def test_cancel_lets_the_screen_sleep_again(awake_page):
    dlg = record(awake_page, 1.0)
    dlg.get_by_role("button", name="Cancel").click()
    expect(awake_page.locator("dialog.recorder")).to_have_count(0)
    awake_page.wait_for_function("() => window.__locks.length === 1 && window.__locks[0].released")


def test_a_failed_upload_lets_the_screen_sleep_and_retry_keeps_it_on(awake_page, sim):
    _fail_uploads(awake_page)
    dlg = record(awake_page, 1.0)
    dlg.get_by_role("button", name="Stop & upload").click()
    expect(dlg.locator(".recorder-error")).to_have_text(UPLOAD_FAILED, timeout=20_000)
    awake_page.wait_for_function("() => window.__locks.every((s) => s.released)")
    awake_page.unroute("**/api/files")
    dlg.get_by_role("button", name="Retry").click()
    expect(awake_page.get_by_text(re.compile(r"Uploaded FILE\d+"))).to_be_visible(timeout=20_000)
    assert len(awake_page.evaluate(LOCKS)) == 2           # retry took the lock again for its upload
    awake_page.wait_for_function("() => window.__locks.every((s) => s.released)")


def test_a_denied_microphone_never_takes_the_lock(page, live_server, sim):
    page.add_init_script(FAKE_WAKE_LOCK)
    page.add_init_script(DENY_MIC)
    login_ui(page, live_server.url, sim.passphrase)
    page.get_by_role("button", name="Record audio").click()
    expect(page.locator("dialog.recorder .recorder-error")).to_be_visible()
    assert page.evaluate(LOCKS) == []
