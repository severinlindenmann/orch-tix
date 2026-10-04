# tests/browser/test_transcribe.py — auto-transcription in the browser (spec §20), end to end in
# Chromium. Deepgram is never called: every test routes https://api.deepgram.com/** to a fake.
import json
import re
import struct
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from .conftest import login_ui

pytestmark = pytest.mark.browser

KEY = "dg-fake-key-0123456789abcdef"
DEEPGRAM = "https://api.deepgram.com/**"
TEXT = "Grüezi mitenand\nDas ist ein Test."


# The outbox test needs the real service worker; the rest don't mind it.
@pytest.fixture
def browser_context_args(browser_context_args):
    return {**browser_context_args, "service_workers": "allow"}


@pytest.fixture(autouse=True)
def _chromium_only(browser_name):
    if browser_name != "chromium":
        pytest.skip("the fake microphone and service worker offline emulation are Chromium-only here")


def _wav(seconds=0.2, rate=8000) -> bytes:
    n = int(seconds * rate)
    data = bytes(128 for _ in range(n))
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate, 1, 8)
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
            + b"data" + struct.pack("<I", len(data)) + data)


class FakeDeepgram:
    """Answers the browser's POST /v1/listen (and its CORS preflight) like Deepgram would."""

    def __init__(self, context, origin, status=200, transcript=TEXT):
        self.origin, self.status, self.transcript = origin, status, transcript
        self.calls = []
        context.route(DEEPGRAM, self._handle)

    def _cors(self):
        return {"Access-Control-Allow-Origin": self.origin, "Access-Control-Allow-Methods": "POST",
                "Access-Control-Allow-Headers": "authorization, content-type", "Vary": "Origin"}

    def _handle(self, route):
        req = route.request
        if req.method == "OPTIONS":
            route.fulfill(status=204, headers=self._cors())
            return
        self.calls.append({"url": req.url, "headers": req.headers, "body": req.post_data_buffer})
        if self.status != 200:
            route.fulfill(status=self.status, headers=self._cors(), content_type="application/json",
                          body=json.dumps({"err_code": "X", "err_msg": "fake failure"}))
            return
        body = {"metadata": {"request_id": "fake"}, "results": {"channels": [
            {"alternatives": [{"transcript": self.transcript, "confidence": 0.99}]}]}}
        route.fulfill(status=200, headers=self._cors(), content_type="application/json", body=json.dumps(body))

    def only_call(self):
        assert len(self.calls) == 1, self.calls
        return self.calls[0]


def meta_of(sim, ref):
    meta, _ = sim.s.open_file_meta(sim.mk, sim.get_file(ref))
    return meta


def file_ids(sim):
    return [f["id"] for f in sim.request("GET", "/api/files").json()["files"]]


def ready(page):
    page.wait_for_load_state("networkidle")


def row(page, file_id):
    return page.locator(f'#file-list .frow[data-id="{file_id}"]')


def open_view(page, file_id):
    row(page, file_id).click()
    panel = page.locator("#detail.is-open")
    expect(panel).to_be_visible()
    return panel


def toast(page, text):
    """The one toast with this text (never counts the others piling up)."""
    return page.locator(".toast", has_text=text)


def upload_audio(page, tmp_path, name="memo.wav", data=None):
    path = Path(tmp_path) / name
    path.write_bytes(data or _wav())
    page.locator("#upload-btn").click()
    sheet = page.locator("dialog.upload-sheet")
    expect(sheet).to_be_visible()
    sheet.locator("#upload-input").set_input_files(str(path))
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/files")) as resp:
        sheet.get_by_role("button", name="Encrypt and share").click()
    assert resp.value.status == 201
    return resp.value.json()


@pytest.fixture
def signed_in(page, live_server, sim):
    login_ui(page, live_server.url, sim.passphrase)
    ready(page)
    return page


def test_an_uploaded_audio_file_is_transcribed_and_the_transcript_is_stored(page, live_server, sim, tmp_path):
    sim.put_settings({"deepgram_api_key": KEY, "transcribe_language": "de-CH", "future_setting": [1]})
    fake = FakeDeepgram(page.context, live_server.url)
    console = []
    page.on("console", lambda m: console.append(m.text))
    login_ui(page, live_server.url, sim.passphrase)
    ready(page)
    audio = _wav()
    out = upload_audio(page, tmp_path, data=audio)
    expect(toast(page, f"{out['id']} — Transcript added")).to_be_visible(timeout=20_000)

    call = fake.only_call()
    assert call["headers"]["authorization"] == f"Token {KEY}"
    assert call["headers"]["content-type"] == "audio/wav"
    parts = urlsplit(call["url"])
    assert parts.scheme == "https" and parts.netloc == "api.deepgram.com" and parts.path == "/v1/listen"
    assert parse_qs(parts.query) == {"model": ["nova-3"], "smart_format": ["true"], "language": ["de-CH"]}
    assert call["body"] == audio  # the raw plaintext audio, nothing else

    t = meta_of(sim, out["id"])["transcript"]
    assert t["text"] == TEXT and t["language"] == "de-CH" and t["model"] == "nova-3"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", t["created_at"])
    assert t["by"] and t["by"] != "browser"   # this browser's session name
    assert meta_of(sim, out["id"])["name"] == "memo.wav"

    # The row gets its badge at once; the view shows the transcript, and so it does after a reload.
    expect(row(page, out["id"]).locator(".pill-transcribed")).to_be_visible()
    panel = open_view(page, out["id"])
    expect(panel.locator(".transcript-text")).to_have_text(TEXT)
    page.reload()
    ready(page)
    panel = open_view(page, out["id"])
    expect(panel.locator(".transcript-text")).to_have_text(TEXT)
    expect(panel.locator(".transcribe-btn")).to_have_count(0)

    assert not any(KEY in m for m in console)
    stored = page.evaluate("() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage })")
    assert KEY not in stored
    assert sim.get_settings()[0]["future_setting"] == [1]


def test_auto_transcribe_off_sends_nothing_to_deepgram(page, live_server, sim, tmp_path):
    sim.put_settings({"deepgram_api_key": KEY, "auto_transcribe": False})
    fake = FakeDeepgram(page.context, live_server.url)
    deepgram_requests = []
    page.on("request", lambda r: deepgram_requests.append(r.url) if "api.deepgram.com" in r.url else None)
    login_ui(page, live_server.url, sim.passphrase)
    ready(page)
    out = upload_audio(page, tmp_path)
    ready(page)
    page.wait_for_timeout(500)
    assert fake.calls == [] and deepgram_requests == []
    assert "transcript" not in meta_of(sim, out["id"])
    # The button is still there for an explicit run.
    panel = open_view(page, out["id"])
    expect(panel.locator(".transcribe-btn")).to_be_enabled()


def test_non_audio_uploads_are_not_transcribed(page, live_server, sim, tmp_path):
    sim.put_settings({"deepgram_api_key": KEY})
    fake = FakeDeepgram(page.context, live_server.url)
    login_ui(page, live_server.url, sim.passphrase)
    ready(page)
    path = Path(tmp_path) / "notes.txt"
    path.write_text("hello")
    page.locator("#upload-btn").click()
    sheet = page.locator("dialog.upload-sheet")
    sheet.locator("#upload-input").set_input_files(str(path))
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/files")):
        sheet.get_by_role("button", name="Encrypt and share").click()
    ready(page)
    page.wait_for_timeout(300)
    assert fake.calls == []


def test_the_transcribe_button_on_an_agent_shared_audio_file(page, live_server, sim):
    f = sim.upload("agent-memo.wav", _wav())   # uploaded by a device: nothing transcribed it
    sim.put_settings({"deepgram_api_key": KEY, "transcribe_language": "en"})
    fake = FakeDeepgram(page.context, live_server.url, transcript="Hello from the agent")
    login_ui(page, live_server.url, sim.passphrase)
    ready(page)
    assert fake.calls == []  # a device upload is never auto-transcribed by the browser
    panel = open_view(page, f["id"])
    expect(panel.locator(".transcript-none")).to_have_text("No transcript yet.")
    expect(panel.locator(".transcribe-hint")).to_have_text("Sends the audio to Deepgram · English")
    expect(panel.locator("audio.preview-audio")).to_be_visible()
    panel.locator(".transcribe-btn").click()
    expect(panel.locator(".transcript-text")).to_have_text("Hello from the agent", timeout=20_000)
    expect(panel.locator(".transcribe-btn")).to_have_count(0)
    call = fake.only_call()
    assert parse_qs(urlsplit(call["url"]).query)["language"] == ["en"]
    assert call["body"] == _wav()   # the plaintext the player already held
    t = meta_of(sim, f["id"])["transcript"]
    assert t["text"] == "Hello from the agent" and t["language"] == "en"
    assert meta_of(sim, f["id"])["name"] == "agent-memo.wav"
    expect(row(page, f["id"]).locator(".pill-transcribed")).to_be_visible()


def test_the_transcribe_button_is_disabled_with_a_hint_without_a_key(signed_in, sim):
    f = sim.upload("memo.wav", _wav())
    signed_in.reload()
    ready(signed_in)
    panel = open_view(signed_in, f["id"])
    expect(panel.locator(".transcribe-btn")).to_be_disabled()
    expect(panel.locator(".transcribe-hint")).to_have_text("Set a Deepgram key in Settings to transcribe.")
    expect(panel.locator(".transcribe-hint a")).to_have_attribute("href", "/settings")


def test_deepgram_401_shows_the_reason_and_the_file_stays(page, live_server, sim, tmp_path):
    sim.put_settings({"deepgram_api_key": KEY})
    fake = FakeDeepgram(page.context, live_server.url, status=401)
    console = []
    page.on("console", lambda m: console.append(m.text))
    login_ui(page, live_server.url, sim.passphrase)
    ready(page)
    out = upload_audio(page, tmp_path)
    expect(toast(page, f"{out['id']} — Couldn't transcribe: Deepgram rejected the API key")).to_be_visible(timeout=20_000)
    assert len(fake.calls) == 1
    assert out["id"] in file_ids(sim)
    assert "transcript" not in meta_of(sim, out["id"])
    expect(row(page, out["id"])).to_be_visible()
    # The button retries; the reason shows inline as well.
    panel = open_view(page, out["id"])
    panel.locator(".transcribe-btn").click()
    expect(panel.locator(".transcribe-status")).to_have_text("Couldn't transcribe: Deepgram rejected the API key")
    expect(panel.locator(".transcribe-btn")).to_be_enabled()
    assert not any(KEY in m or "fake failure" in m for m in console)
    assert KEY not in page.content()


def test_an_empty_transcript_says_no_speech_and_stores_nothing(page, live_server, sim, tmp_path):
    sim.put_settings({"deepgram_api_key": KEY})
    FakeDeepgram(page.context, live_server.url, transcript="   ")
    login_ui(page, live_server.url, sim.passphrase)
    ready(page)
    out = upload_audio(page, tmp_path)
    expect(toast(page, f"{out['id']} — No speech detected")).to_be_visible(timeout=20_000)
    ready(page)
    assert "transcript" not in meta_of(sim, out["id"])
    panel = open_view(page, out["id"])
    panel.locator(".transcribe-btn").click()
    expect(panel.locator(".transcribe-status")).to_have_text("No speech detected")
    assert "transcript" not in meta_of(sim, out["id"])


def test_a_transcript_too_long_to_store_is_shown_once_unstored(page, live_server, sim):
    f = sim.upload("long.wav", _wav())
    sim.put_settings({"deepgram_api_key": KEY})
    long_text = "word " * 90_000   # ~450 KB: over the 524288-character enc_meta cap once sealed
    FakeDeepgram(page.context, live_server.url, transcript=long_text)
    login_ui(page, live_server.url, sim.passphrase)
    ready(page)
    panel = open_view(page, f["id"])
    panel.locator(".transcribe-btn").click()
    expect(panel.locator(".transcript-notice")).to_have_text(
        "This transcript is too long to store with the file. It's shown here once and isn't saved.", timeout=20_000)
    expect(panel.locator(".transcript-text")).to_have_text(long_text.strip())
    assert "transcript" not in meta_of(sim, f["id"])
    # Once: reopening the file shows the button again.
    page.reload()
    ready(page)
    panel = open_view(page, f["id"])
    expect(panel.locator(".transcript-notice")).to_have_count(0)
    expect(panel.locator(".transcribe-btn")).to_be_visible()


def test_a_recording_made_offline_is_transcribed_after_it_uploads(page, live_server, sim):
    sim.put_settings({"deepgram_api_key": KEY})
    fake = FakeDeepgram(page.context, live_server.url, transcript="offline memo")
    page.context.grant_permissions(["microphone"])
    login_ui(page, live_server.url, sim.passphrase)
    ready(page)
    page.evaluate("async () => { await navigator.serviceWorker.ready; }")
    page.reload()
    ready(page)
    page.wait_for_function("() => navigator.serviceWorker.controller !== null")
    page.context.set_offline(True)
    page.reload()
    expect(page.locator("#offline")).to_be_visible()

    page.get_by_role("button", name="Record audio").click()
    dlg = page.locator("dialog.recorder")
    expect(dlg.locator(".recorder-status")).to_have_text("Recording…")
    page.wait_for_timeout(1200)
    dlg.get_by_role("button", name="Stop & upload").click()
    expect(dlg).to_have_count(0, timeout=20_000)
    expect(page.locator("#outbox-slot .frow-pending")).to_be_visible()
    assert fake.calls == []   # queued, nothing sent anywhere yet

    page.context.set_offline(False)
    expect(page.locator("#outbox-slot")).to_be_hidden(timeout=20_000)
    (fid,) = file_ids(sim)
    expect(toast(page, f"{fid} — Transcript added")).to_be_visible(timeout=20_000)
    call = fake.only_call()
    assert call["headers"]["content-type"].startswith("audio/webm")
    assert call["body"][:4] == b"\x1a\x45\xdf\xa3"   # decrypted locally: a WebM (EBML) header
    meta = meta_of(sim, fid)
    assert meta["transcript"]["text"] == "offline memo"
    assert re.fullmatch(r"recording-\d{8}-\d{6}\.webm", meta["name"])
    # The plaintext never reached IndexedDB: the outbox is empty and holds no other store.
    stores = page.evaluate("""() => new Promise((res) => {
      const q = indexedDB.open("fileshare");
      q.onsuccess = () => { const names = [...q.result.objectStoreNames]; q.result.close(); res(names); };
    })""")
    assert sorted(stores) == ["keys", "labels", "lists", "outbox", "pairs", "prefs", "seen"]


def test_settings_language_and_toggle_survive_a_reload_and_keep_unknown_keys(page, live_server, sim):
    sim.put_settings({"deepgram_api_key": KEY, "future_setting": {"a": 1}})
    login_ui(page, live_server.url, sim.passphrase)
    page.goto(live_server.url + "/settings")
    ready(page)
    card = page.locator("#transcription-card")
    expect(card.locator(".notice")).to_have_text(
        "Audio you upload is sent to Deepgram for transcription (unencrypted, over HTTPS).")
    select, toggle = page.locator("#transcribe-language"), page.locator("#auto-transcribe")
    expect(select).to_be_enabled()
    expect(select).to_have_value("de")      # the default
    expect(toggle).to_be_checked()          # the default
    assert [o.strip() for o in select.locator("option").all_inner_texts()] == ["German", "Swiss German", "English"]

    rev = sim.get_settings()[1]
    with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/api/settings")) as put:
        select.select_option("de-CH")
    assert put.value.status == 200
    ready(page)
    expect(select).to_be_enabled()
    assert sim.get_settings()[1] == rev + 1
    with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/api/settings")) as put:
        toggle.uncheck()
    assert put.value.status == 200
    ready(page)
    expect(toggle).to_be_enabled()
    obj, rev2, _ = sim.get_settings()
    assert rev2 == rev + 2
    assert obj == {"deepgram_api_key": KEY, "future_setting": {"a": 1},
                   "transcribe_language": "de-CH", "auto_transcribe": False}

    page.reload()
    ready(page)
    expect(select).to_have_value("de-CH")
    expect(toggle).not_to_be_checked()


def test_settings_transcription_card_is_disabled_without_a_key(page, live_server, sim):
    login_ui(page, live_server.url, sim.passphrase)
    page.goto(live_server.url + "/settings")
    ready(page)
    expect(page.locator("#transcription-hint")).to_be_visible()
    expect(page.locator("#transcribe-language")).to_be_disabled()
    expect(page.locator("#auto-transcribe")).to_be_disabled()
    # Saving a key turns it on, and the Deepgram key card keeps working as before.
    page.locator("#deepgram-key").fill(KEY)
    page.locator("#deepgram-save").click()
    expect(page.locator("#deepgram-status")).to_contain_text("Configured")
    expect(page.locator("#transcribe-language")).to_be_enabled()
    expect(page.locator("#transcription-hint")).to_be_hidden()
    assert sim.get_settings()[0] == {"deepgram_api_key": KEY}


def test_settings_conflict_on_the_transcription_card_reloads_and_asks_to_retry(page, live_server, sim):
    sim.put_settings({"deepgram_api_key": KEY})
    login_ui(page, live_server.url, sim.passphrase)
    page.goto(live_server.url + "/settings")
    ready(page)
    sim.put_settings({"deepgram_api_key": KEY, "other_writer": True})   # lands between load and save
    page.locator("#transcribe-language").select_option("en")
    expect(page.locator("#transcription-msg")).to_have_text("Settings changed elsewhere — reloaded, please retry")
    expect(page.locator("#transcribe-language")).to_have_value("de")
    with page.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/api/settings")) as put:
        page.locator("#transcribe-language").select_option("en")
    assert put.value.status == 200
    ready(page)
    expect(page.locator("#transcription-msg")).to_be_hidden()
    assert sim.get_settings()[0] == {"deepgram_api_key": KEY, "other_writer": True,
                                     "transcribe_language": "en", "auto_transcribe": True}


def _fail_settings(route):
    if route.request.method == "GET":
        route.fulfill(status=500, content_type="application/json", body='{"error":"boom","detail":"x"}')
    else:
        route.continue_()


def test_settings_that_fail_to_load_are_not_mistaken_for_no_key(page, live_server, sim, tmp_path):
    f = sim.upload("memo.wav", _wav())
    sim.put_settings({"deepgram_api_key": KEY})
    fake = FakeDeepgram(page.context, live_server.url, transcript="after the retry")
    login_ui(page, live_server.url, sim.passphrase)
    ready(page)
    page.route("**/api/settings", _fail_settings)
    # An automatic run skips silently: nothing goes to Deepgram and no "set a key" message appears.
    out = upload_audio(page, tmp_path, name="auto.wav")
    ready(page)
    page.wait_for_timeout(300)
    assert fake.calls == []
    assert "transcript" not in meta_of(sim, out["id"])
    expect(page.locator(".toast", has_text="Deepgram key")).to_have_count(0)
    # The file view says so and keeps the button usable; a press retries.
    panel = open_view(page, f["id"])
    expect(panel.locator(".transcribe-hint")).to_have_text("Couldn't load settings — try again")
    expect(panel.locator(".transcribe-btn")).to_be_enabled()
    panel.locator(".transcribe-btn").click()
    expect(panel.locator(".transcribe-status")).to_have_text("Couldn't load settings — try again")
    expect(panel.locator(".transcribe-btn")).to_be_enabled()
    assert fake.calls == []
    page.unroute("**/api/settings", _fail_settings)
    panel.locator(".transcribe-btn").click()
    expect(panel.locator(".transcript-text")).to_have_text("after the retry", timeout=20_000)
    assert len(fake.calls) == 1


def test_a_settings_save_in_one_tab_reaches_another_tab(page, live_server, sim, tmp_path):
    a1 = sim.upload("one.wav", _wav())
    a2 = sim.upload("two.wav", _wav())
    sim.put_settings({"deepgram_api_key": KEY})
    fake = FakeDeepgram(page.context, live_server.url)
    login_ui(page, live_server.url, sim.passphrase)
    ready(page)
    # Tab A reads the settings once (opening an audio file needs them).
    panel = open_view(page, a1["id"])
    expect(panel.locator(".transcribe-hint")).to_have_text("Sends the audio to Deepgram · German")

    # Tab B, same browser: pick English and turn auto-transcribe off.
    other = page.context.new_page()
    other.goto(live_server.url + "/settings")
    ready(other)
    with other.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/api/settings")):
        other.locator("#transcribe-language").select_option("en")
    ready(other)
    with other.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith("/api/settings")):
        other.locator("#auto-transcribe").uncheck()
    ready(other)

    # Tab A, without a reload: the next view shows English, and a new upload isn't sent anywhere.
    page.bring_to_front()
    panel = open_view(page, a2["id"])
    expect(panel.locator(".transcribe-hint")).to_have_text("Sends the audio to Deepgram · English")
    out = upload_audio(page, tmp_path)
    ready(page)
    page.wait_for_timeout(300)
    assert fake.calls == []
    assert "transcript" not in meta_of(sim, out["id"])
    other.close()


def test_the_csp_lets_the_page_reach_deepgram_and_nothing_else_new(page, live_server, sim):
    login_ui(page, live_server.url, sim.passphrase)
    resp = page.goto(live_server.url + "/files")
    csp = resp.headers["content-security-policy"]
    directives = dict(d.strip().split(" ", 1) for d in csp.split(";"))
    assert directives["connect-src"] == "'self' https://api.deepgram.com"
    assert csp.count("deepgram") == 1
