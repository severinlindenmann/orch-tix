import asyncio
import copy
import json
import os
from pathlib import Path
from dataclasses import dataclass

import httpx
import pytest

# Requests a service worker makes go through context.route as well (Playwright reads this when its driver
# starts, i.e. before the first browser test). Without it a route never sees the worker's own fetch of a
# navigation, and the slow-/pair test (test_pairing.py) could not delay it.
os.environ.setdefault("PW_EXPERIMENTAL_SERVICE_WORKER_NETWORK_EVENTS", "1")

LOGIN_TIMEOUT = 30_000
PHONE = {"width": 390, "height": 844}


# Playwright for this package only (pytest-playwright makes these session-scoped). Its sync API marks its own
# event loop as the thread's running loop for as long as it lives, so every later asyncio.run() in the session
# (tests/test_blobs.py) would fail with "cannot be called from a running event loop". Package scope stops it
# when tests/browser is done; nothing outside this package uses a browser.
@pytest.fixture(scope="package")
def playwright():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    yield pw
    pw.stop()
    asyncio.events._set_running_loop(None)        # stop() leaves no loop marked running; make sure of it


@pytest.fixture(scope="package")
def browser_context_args(pytestconfig, playwright, device, base_url, _pw_artifacts_folder):
    args = {}
    if device:
        args.update(playwright.devices[device])
    if base_url:
        args["base_url"] = base_url
    if pytestconfig.getoption("--video") in ["on", "retain-on-failure"]:
        args["record_video_dir"] = _pw_artifacts_folder.name
    return args


@pytest.fixture(scope="package")
def browser_type(playwright, browser_name):
    return getattr(playwright, browser_name)


@pytest.fixture(scope="package")
def launch_browser(browser_type_launch_args, browser_type, connect_options):
    def launch(**kwargs):
        options = {**browser_type_launch_args, **kwargs}
        if connect_options:
            return browser_type.connect(**{**connect_options, "headers": {
                "x-playwright-launch-options": json.dumps(options), **(connect_options.get("headers") or {})}})
        return browser_type.launch(**options)
    return launch


@pytest.fixture(scope="package")
def browser(launch_browser):
    b = launch_browser()
    yield b
    b.close()


# pytest-playwright's session-scoped `device` would be shadowed by tests/conftest.py's function-scoped one (ScopeMismatch).
@pytest.fixture(scope="session")
def device(pytestconfig):
    return pytestconfig.getoption("--device")


# Chromium's fake camera/microphone (a beep) and auto-accepted permission prompts, for the recorder
# tests. Session-wide because pytest-playwright launches one `browser` per session.
FAKE_MEDIA_ARGS = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args, browser_name):
    if browser_name not in (None, "chromium"):
        return browser_type_launch_args
    return {**browser_type_launch_args, "args": [*browser_type_launch_args.get("args", []), *FAKE_MEDIA_ARGS]}


def login_ui(page, base_url: str, passphrase: str, then: str = "/files") -> None:
    """Log in through the real /login form, wait for Needs you (/), then open `then` (the Files page
    by default: most browser tests are about files; None stays on Needs you)."""
    page.goto(base_url + "/login")
    page.locator("#passphrase").fill(passphrase)
    page.get_by_role("button", name="Log in").click()
    page.wait_for_url(base_url + "/", timeout=LOGIN_TIMEOUT)
    if then and then != "/":
        page.goto(base_url + then)


@pytest.fixture
def ui_page(page, live_server, sim):
    """A Playwright page logged in as the owner, on the Files page (/files)."""
    login_ui(page, live_server.url, sim.passphrase)
    return page


@pytest.fixture
def phone_page(page, live_server, sim):
    """phone_page(scheme): the owner signed in at 390 x 844 in the light or dark scheme, on Needs you,
    with the service worker controlling the page (so an offline reload opens from its cache)."""
    def make(scheme: str = "light"):
        page.set_viewport_size(PHONE)
        page.emulate_media(color_scheme=scheme)
        login_ui(page, live_server.url, sim.passphrase, then=None)
        # Not "networkidle": Needs you keeps a long-poll open on /api/mirrors/changes.
        page.evaluate("async () => { await navigator.serviceWorker.ready; }")
        page.reload()
        page.wait_for_function("() => navigator.serviceWorker.controller !== null")
        page.locator("#needs-loading").wait_for(state="hidden")
        return page
    return make


# ---- a desktop workspace with one mirrored ticket that needs an answer (TIX on orch-core) ----
# The real document shape (final review I2): what the orch-tix addon seals, from tests/vectors/addon-docs.json
# (its mapping.redact of `orch schema example`). EXAMPLE_DOC is the default `title` level: one open question
# Q1 ("ISO 8601" / "Local time"), both gates approved, no sections. FULL_DOC is the `full` level (sections,
# gate text). Both carry the needs the phone reads.
ADDON_LEVELS = json.loads((Path(__file__).parents[1] / "vectors" / "addon-docs.json").read_text(encoding="utf-8"))["levels"]
EXAMPLE_DOC = copy.deepcopy(ADDON_LEVELS["title"])
FULL_DOC = copy.deepcopy(ADDON_LEVELS["full"])


def sealed_doc(doc: dict, rev: int, gen: int = 1) -> dict:
    """What the CLI seals (sharing.mirror_doc): the doc plus mirror_rev and gen. A test that sets its own
    mirror_rev or gen (a rollback) keeps them."""
    return {"mirror_rev": rev, "gen": gen, **doc}


@dataclass
class MirrorFixture:
    base: str
    sim: object
    space: str
    n: int
    id: str
    uuid: str
    headers: dict
    dek: bytes

    def decisions(self) -> list:
        r = self.sim.request("GET", f"/api/decisions?space={self.space}&ticket={self.id}")
        assert r.status_code == 200, r.text
        return r.json()["decisions"]

    def inbox_items(self) -> list:
        """The space's decisions as the owner desktop's inbox long-poll returns them (sealed)."""
        r = httpx.get(f"{self.base}/api/inbox/changes", params={"space": self.space, "after": 0, "wait": 0},
                      headers=self.headers, timeout=30)
        assert r.status_code == 200, r.text
        return r.json()["decisions"]

    def push(self, doc: dict, *, rev: int, needs="question", open_questions=1, status="waiting") -> dict:
        s = self.sim.s
        tu = bytes.fromhex(self.uuid)
        body = {"space": self.space, "mirror_rev": rev, "schema_version": "1.0.0", "status": status, "priority": "high",
                "needs": needs, "open_questions": open_questions, "key_version": 1,
                "enc_content": s.seal_mirror(self.dek, tu, sealed_doc(doc, rev)),
                "event_uuid": s.mirror_event_uuid(self.space, doc["id"], 1, rev)}
        r = httpx.put(f"{self.base}/api/mirrors/{self.uuid}", json=body, headers=self.headers, timeout=30)
        assert r.status_code == 200, r.text
        return r.json()


def make_desktop(live_server, sim, *, name="macbook-pro", label="Acme Energy") -> tuple[str, dict]:
    """An approved device that created a space: (space id, its bearer headers)."""
    s = sim.s
    code, _ = sim.onboarding_code()
    priv, pub = s.new_device_keypair()
    r = httpx.post(live_server.url + "/api/handshake", json={
        "lookup": code.split(".", 1)[1], "device_name": name, "project": "acme", "hostname": "desk",
        "platform": "darwin", "device_pub": s.b64u(pub)}, timeout=30)
    assert r.status_code == 201, r.text
    dev = r.json()
    sim.approve(dev["device_id"])
    headers = {"Authorization": f"Bearer {dev['device_token']}"}
    space = os.urandom(16).hex()
    r = httpx.post(live_server.url + "/api/spaces", json={"id": space, "key_version": 1,
                                                          "enc_label": s.seal_space_label(sim.mk, space, label)},
                   headers=headers, timeout=30)
    assert r.status_code == 201, r.text
    return space, headers


@pytest.fixture
def mirror_with_question(live_server, sim):
    s = sim.s
    space, headers = make_desktop(live_server, sim)
    uuid = s.mirror_uuid(space, EXAMPLE_DOC["id"], 1)
    dek = os.urandom(32)
    tu = bytes.fromhex(uuid)
    body = {"space": space, "mirror_rev": 1, "schema_version": "1.0.0", "status": "waiting", "priority": "high",
            "needs": "question", "open_questions": 1, "key_version": 1,
            "wrapped_dek": s.b64u(s.seal(sim.mk, dek, s.aad_tdek(tu))),
            "enc_content": s.seal_mirror(dek, tu, sealed_doc(EXAMPLE_DOC, 1)),
            "event_uuid": s.mirror_event_uuid(space, EXAMPLE_DOC["id"], 1, 1)}
    r = httpx.put(f"{live_server.url}/api/mirrors/{uuid}", json=body, headers=headers, timeout=30)
    assert r.status_code == 200, r.text
    out = r.json()
    return MirrorFixture(live_server.url, sim, space, out["n"], out["id"], uuid, headers, dek)
