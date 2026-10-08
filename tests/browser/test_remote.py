"""The Remote pages in a real browser (R10 wiring): pair a browser with a fake host over the real mailbox, open the
workspace's dashboard in the sandboxed frame, click through the bridge, switch workspaces, see a readable refusal when
the device was revoked, and find the bridge database gone after sign-out.

The host is tests/support/fake_bridge_host.py: an approved device that long-polls the real mailbox routes and answers
with the Python reference implementation of the protocol (docs/bridge-protocol.md)."""
import json
import re
import time

import pytest
from playwright.sync_api import expect

from tests.support import bridge_protocol_ref as ref
from tests.support.fake_bridge_host import FakeHost

from .conftest import PHONE, login_ui, make_desktop

pytestmark = pytest.mark.browser

HTML = {"content-type": "text/html; charset=utf-8"}


def dash(title, link=None):
    nav = f'<a id="go" href="{link}">Go</a>' if link else ""
    return {"status": 200, "headers": HTML, "page": True,
            "body": f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head>'
                    f'<body><h1 id="h">{title}</h1>{nav}</body></html>'}


@pytest.fixture
def make_host(live_server, sim):
    hosts = []

    def make(name="macbook-pro", label="Acme Energy", pages=None, beat=True):
        space, headers = make_desktop(live_server, sim, name=name, label=label)
        host = FakeHost(live_server.url, space, headers, sim.mk,
                        pages or {"/": dash("Home dashboard", "/board"), "/board": dash("Board page")}, beat=beat).start()
        hosts.append(host)
        return host
    yield make
    for h in hosts:
        h.stop()


def pair(page, live_server, host, approve=True):
    """Open the pairing link, press Pair, compare the shown fingerprint with the one the host computes, approve."""
    frag = host.offer()
    page.goto(f"{live_server.url}/remote")                # not just a new hash on the pairing page: that would not reload it
    page.goto(f"{live_server.url}/remote/pair#{frag}")
    page.locator("#pair-go").click()
    page.locator("#pair-fp").wait_for(state="visible", timeout=30000)
    dev = next(iter(host.waiting()))
    assert page.locator("#pair-fp").inner_text() == ref.device_fingerprint(bytes.fromhex(host.waiting()[dev]["pub"]))
    if approve:
        host.approve(dev)
        expect(page.locator("#remote-pair-main h1")).to_have_text("Paired", timeout=30000)
    return dev


def frame_of(page):
    for _ in range(100):
        fs = [f for f in page.frames if "/sandbox/dash" in f.url]
        if fs:
            return fs[-1]
        page.wait_for_timeout(100)
    raise AssertionError("no dashboard frame")


def wait_h1(page, text, seconds=30):
    for _ in range(seconds * 10):
        try:
            if frame_of(page).evaluate("document.querySelector('h1') && document.querySelector('h1').textContent") == text:
                return
        except Exception:
            pass
        page.wait_for_timeout(100)
    raise AssertionError(f"no page titled {text!r}")


@pytest.fixture
def signed_in(page, live_server, sim):
    login_ui(page, live_server.url, sim.passphrase, then="/remote")
    page.locator("#ws-loading").wait_for(state="hidden")
    return page


def test_the_secret_leaves_the_address_at_once(signed_in, live_server, make_host):
    host = make_host()
    frag = host.offer()
    secret = frag.split(".")[3]
    signed_in.goto(f"{live_server.url}/remote/pair#{frag}")
    expect(signed_in.locator("#pair-go")).to_be_visible()
    assert signed_in.evaluate("location.hash") == ""
    assert secret not in signed_in.url
    entries = signed_in.evaluate("history.length")
    signed_in.reload()                                    # a reload does not bring the link back
    expect(signed_in.locator("#remote-pair-main h1")).to_have_text("This is not a pairing link")
    assert secret not in signed_in.url and entries >= 1


def test_pairing_shows_the_fingerprint_and_waits_for_the_owner(signed_in, live_server, make_host):
    host = make_host()
    dev = pair(signed_in, live_server, host)
    rec = signed_in.evaluate("""async () => {
      const S = await import("/static/js/bridge-store.js"), C = await import("/static/js/crypto.js");
      const r = await S.workspaceRecord(%s);
      return {pub: C.bytesToHex(r.hostPub), extractable: r.kWs.extractable};
    }""" % json.dumps(host.space))
    assert rec == {"pub": host.host_pub.hex(), "extractable": False}      # pinned from the link's pin; K_ws never extractable
    assert dev in host.state["devices"]


def test_a_used_link_says_so_and_pins_nothing(signed_in, live_server, make_host):
    host = make_host()
    frag = host.offer()
    pid = frag.split(".")[2]
    host.state["offers"][pid]["used"] = True               # someone else got there first
    signed_in.goto(f"{live_server.url}/remote/pair#{frag}")
    signed_in.locator("#pair-go").click()
    expect(signed_in.locator("#pair-state")).to_contain_text("used by someone else", timeout=30000)
    assert signed_in.locator("#pair-fp").is_hidden()
    pinned = signed_in.evaluate("""async () => (await (await import("/static/js/bridge-store.js")).workspaceRecord(%s))?.hostPub ?? null""" % json.dumps(host.space))
    assert pinned is None


def test_a_fingerprint_that_is_not_this_browsers_stops_the_pairing(signed_in, live_server, make_host):
    host = make_host()
    host.lie_fingerprint = True                            # the computer saw some other device key
    signed_in.goto(f"{live_server.url}/remote/pair#{host.offer()}")
    signed_in.locator("#pair-go").click()
    expect(signed_in.locator("#pair-state")).to_contain_text("different device key", timeout=30000)
    assert signed_in.locator("#pair-fp").is_hidden()
    pinned = signed_in.evaluate("""async () => (await (await import("/static/js/bridge-store.js")).workspaceRecord(%s))?.hostPub ?? null""" % json.dumps(host.space))
    assert pinned is None


def test_open_a_workspace_click_through_the_bridge_and_see_the_host_answer(signed_in, live_server, make_host):
    host = make_host()
    pair(signed_in, live_server, host)
    signed_in.goto(f"{live_server.url}/remote")
    card = signed_in.locator(f'.wrow[data-space="{host.space}"]')
    expect(card).to_contain_text("Acme Energy")
    expect(card.locator(".pill").first).to_have_text("Online")
    card.get_by_role("button", name="Open Acme Energy").click()
    wait_h1(signed_in, "Home dashboard")
    frame_of(signed_in).locator("#go").click()
    wait_h1(signed_in, "Board page")
    assert [m["path"] for m in host.seen] == ["/", "/board"]
    assert all(m["op"] == "http" and m["method"] == "GET" for m in host.seen)
    assert signed_in.locator("iframe.frame-dash").count() == 1


def test_switching_between_workspaces_replaces_the_frame(signed_in, live_server, make_host):
    a = make_host()
    b = make_host(name="mac-mini", label="Second", pages={"/": dash("Second home")})
    pair(signed_in, live_server, a)
    pair(signed_in, live_server, b)
    signed_in.goto(f"{live_server.url}/remote")
    signed_in.get_by_role("button", name="Open Acme Energy").click()
    wait_h1(signed_in, "Home dashboard")
    signed_in.get_by_role("button", name="Open Second").click()
    wait_h1(signed_in, "Second home")
    assert signed_in.locator("iframe.frame-dash").count() == 1
    assert [m["path"] for m in b.seen] == ["/"]
    signed_in.get_by_role("button", name="Open Acme Energy").click()
    wait_h1(signed_in, "Home dashboard")
    assert signed_in.locator("iframe.frame-dash").count() == 1


def test_a_workspace_that_is_not_online_says_why_in_words(signed_in, live_server, make_host):
    host = make_host()
    pair(signed_in, live_server, host)
    never = make_host(name="mac-mini", label="Idle", beat=False)
    pair(signed_in, live_server, never)
    host.goodbye()
    signed_in.goto(f"{live_server.url}/remote")
    stopped = signed_in.locator(f'.wrow[data-space="{host.space}"]')
    expect(stopped).to_contain_text("Stopped on the computer")
    expect(stopped.get_by_role("button", name="Open Acme Energy")).to_be_disabled()
    idle = signed_in.locator(f'.wrow[data-space="{never.space}"]')
    expect(idle).to_contain_text("never started")
    expect(idle.get_by_role("button", name="Open Idle")).to_be_disabled()
    assert signed_in.locator("iframe.frame-dash").count() == 0


@pytest.mark.parametrize("state,words", [("not_answering", "Not answering"), ("lost", "Lost")])
def test_a_host_that_stops_answering_while_open_is_said_in_words(signed_in, live_server, make_host, state, words):
    host = make_host()
    pair(signed_in, live_server, host)
    mode = {"state": "online"}

    def presence(route):
        r = route.fetch()
        body = r.json()
        for s in body["spaces"]:
            s["state"] = mode["state"]
        route.fulfill(response=r, json=body)
    signed_in.route("**/api/presence", presence)
    signed_in.goto(f"{live_server.url}/remote?space={host.space}")
    wait_h1(signed_in, "Home dashboard")
    mode["state"] = state                                     # the next refresh (10 s) sees the host gone quiet
    signed_in.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(signed_in.locator("#remote-notice")).to_contain_text(words, timeout=30000)
    expect(signed_in.get_by_role("button", name="Open Acme Energy")).to_be_disabled()
    signed_in.unroute("**/api/presence")                     # no handler may outlive the server


def test_a_revoked_browser_sees_a_readable_refusal_not_a_blank_page(signed_in, live_server, make_host):
    host = make_host()
    dev = pair(signed_in, live_server, host)
    host.revoke(dev)
    signed_in.goto(f"{live_server.url}/remote")
    signed_in.get_by_role("button", name="Open Acme Energy").click()
    expect(signed_in.locator("#remote-notice")).to_contain_text("removed from that computer", timeout=30000)
    assert host.seen == []                                    # nothing ran


def test_sign_out_deletes_the_bridge_database_and_its_keys(signed_in, live_server, make_host):
    host = make_host()
    pair(signed_in, live_server, host)
    signed_in.goto(f"{live_server.url}/remote")
    probe = """() => new Promise((resolve) => {
      const r = indexedDB.open("fileshare-bridge");
      let fresh = false;
      r.onupgradeneeded = () => { fresh = true; };
      r.onsuccess = () => { const names = [...r.result.objectStoreNames]; r.result.close(); if (fresh) indexedDB.deleteDatabase("fileshare-bridge"); resolve({fresh, names}); };
    })"""
    # the database exists with its keys before sign-out ...
    assert signed_in.evaluate(probe)["fresh"] is False
    signed_in.locator("#logout").first.click()
    signed_in.wait_for_url(re.compile(r".*/login"))
    # ... and is gone after it (opening it again creates it from nothing)
    assert signed_in.evaluate(probe)["fresh"] is True


def test_the_page_fits_a_phone(signed_in, live_server, make_host):
    host = make_host()
    pair(signed_in, live_server, host)
    signed_in.set_viewport_size(PHONE)
    signed_in.goto(f"{live_server.url}/remote?space={host.space}")
    wait_h1(signed_in, "Home dashboard")
    assert signed_in.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


# ---- review round -------------------------------------------------------------------------------------------------

PROBE = """() => new Promise((resolve) => {
  const r = indexedDB.open("fileshare-bridge");
  let fresh = false;
  r.onupgradeneeded = () => { fresh = true; };
  r.onsuccess = () => { r.result.close(); if (fresh) indexedDB.deleteDatabase("fileshare-bridge"); resolve(fresh); };
})"""
LINK = "v1." + "a" * 32 + "." + "b" * 32 + "." + "A" * 43 + "." + "A" * 43


def test_a_label_that_looks_like_markup_is_drawn_as_text(signed_in, live_server, make_host):
    label = '<img src=x onerror="window.__x=1">'
    host = make_host(label=label)
    signed_in.goto(f"{live_server.url}/remote")
    card = signed_in.locator(f'.wrow[data-space="{host.space}"]')
    expect(card.locator(".wrow-name")).to_have_text(label)
    assert card.locator("img").count() == 0
    assert signed_in.evaluate("window.__x") is None


def test_a_rejected_browser_is_not_shown_as_paired(signed_in, live_server, make_host):
    host = make_host()
    dev = pair(signed_in, live_server, host, approve=False)
    host.reject(dev)
    expect(signed_in.locator("#pair-state")).to_contain_text("rejected", timeout=30000)
    signed_in.goto(f"{live_server.url}/remote")
    card = signed_in.locator(f'.wrow[data-space="{host.space}"]')
    expect(card).to_contain_text("not paired")
    expect(card.get_by_role("button", name="Open Acme Energy")).to_be_disabled()


def test_a_session_that_ends_deletes_the_bridge_database(signed_in, live_server, make_host):
    host = make_host()
    pair(signed_in, live_server, host)
    signed_in.goto(f"{live_server.url}/remote")
    assert signed_in.evaluate(PROBE) is False
    signed_in.evaluate("window.dispatchEvent(new CustomEvent('fs:unauthenticated'))")
    signed_in.wait_for_function("""() => new Promise((resolve) => {
      const r = indexedDB.open("fileshare-bridge"); let fresh = false;
      r.onupgradeneeded = () => { fresh = true; };
      r.onsuccess = () => { r.result.close(); if (fresh) indexedDB.deleteDatabase("fileshare-bridge"); resolve(fresh); };
    })""", timeout=15000)


def test_an_offline_remote_pair_page_opens_its_own_shell_and_clears_the_hash(phone_page, context):
    page = phone_page("light")
    base = page.url.split("/", 3)[0] + "//" + page.url.split("/", 3)[2]
    page.wait_for_function("async () => !!(await caches.match('/remote/pair'))")   # precached by the worker
    context.set_offline(True)
    page.goto(f"{base}/remote/pair#{LINK}")
    expect(page.locator("#remote-pair-main h1")).to_have_text("Pair this browser")
    assert page.evaluate("location.hash") == "" and "AAAA" not in page.url


def test_a_slow_remote_pair_page_never_falls_back_to_the_app_shell(phone_page, context):
    page = phone_page("light")
    base = page.url.split("/", 3)[0] + "//" + page.url.split("/", 3)[2]
    page.wait_for_function("async () => !!(await caches.match('/remote/pair'))")

    def slow(route):
        import time
        time.sleep(5)                                     # past the worker's 4 s navigation timeout
        route.continue_()
    context.route("**/remote/pair", slow)
    page.goto(f"{base}/remote/pair#{LINK}", wait_until="commit", timeout=60_000)
    expect(page.locator("#remote-pair-main h1")).to_have_text("Pair this browser", timeout=60_000)
    assert page.evaluate("location.hash") == "" and "AAAA" not in page.url


# ---- streams end to end (R10 streams): the page's EventSource over the bridge ----------------------------------------

OPEN_ES = """() => { window.__m = []; window.__es = new EventSource('/events'); window.__es.onmessage = (e) => window.__m.push(e.data); }"""


def open_dashboard(page, live_server, host):
    pair(page, live_server, host)
    page.goto(f"{live_server.url}/remote?space={host.space}")
    wait_h1(page, "Home dashboard")


def wait_stream(host, n=1):
    for _ in range(300):
        if len(host.open) >= n:
            return
        time.sleep(0.1)
    raise AssertionError("the host never saw the stream")


def messages(page):
    return frame_of(page).evaluate("window.__m")


def test_stream_frames_arrive_in_order_and_coalesced_frames_are_split(signed_in, live_server, make_host):
    host = make_host()
    host.sse["/events"] = [b"data: one\n\n", b"data: two\n\n"]
    open_dashboard(signed_in, live_server, host)
    frame_of(signed_in).evaluate(OPEN_ES)
    wait_stream(host)
    host.keepalive("/events")                                     # a keepalive is not a frame
    host.push("/events", b"data: three\n\ndata: four\n\n", coalesce=True)
    host.push("/events", b"data: fi", b"ve\n\n")                   # one frame in two chunks
    for _ in range(100):
        if len(messages(signed_in)) >= 5:
            break
        signed_in.wait_for_timeout(100)
    assert messages(signed_in) == ["one", "two", "three", "four", "five"]
    assert frame_of(signed_in).evaluate("window.__es.readyState") == 1
    host.end("/events")                                           # LAST: the page's EventSource is closed
    signed_in.wait_for_function("() => true")
    for _ in range(100):
        if frame_of(signed_in).evaluate("window.__es.readyState") == 2:
            break
        signed_in.wait_for_timeout(100)
    assert frame_of(signed_in).evaluate("window.__es.readyState") == 2
    assert host.open == {}


def test_navigating_away_sends_cancel_for_the_stream(signed_in, live_server, make_host):
    host = make_host()
    open_dashboard(signed_in, live_server, host)
    frame_of(signed_in).evaluate(OPEN_ES)
    wait_stream(host)
    rid = next(iter(host.open))
    frame_of(signed_in).locator("#go").click()
    wait_h1(signed_in, "Board page")
    for _ in range(100):
        if rid in host.cancelled:
            break
        time.sleep(0.1)
    assert host.cancelled == [rid] and host.open == {}


def test_switching_workspace_sends_cancel_for_the_stream(signed_in, live_server, make_host):
    a = make_host()
    b = make_host(name="mac-mini", label="Second", pages={"/": dash("Second home")})
    pair(signed_in, live_server, b)
    open_dashboard(signed_in, live_server, a)
    frame_of(signed_in).evaluate(OPEN_ES)
    wait_stream(a)
    rid = next(iter(a.open))
    signed_in.get_by_role("button", name="Open Second").click()
    wait_h1(signed_in, "Second home")
    for _ in range(100):
        if rid in a.cancelled:
            break
        time.sleep(0.1)
    assert a.cancelled == [rid]


def test_a_revoked_device_sees_the_stored_refusal_and_its_stream_ends(signed_in, live_server, make_host):
    host = make_host()
    host.sse["/events"] = [b"data: one\n\n"]
    dev = pair(signed_in, live_server, host)
    signed_in.goto(f"{live_server.url}/remote?space={host.space}")
    wait_h1(signed_in, "Home dashboard")
    frame_of(signed_in).evaluate(OPEN_ES)
    wait_stream(host)
    host.revoke(dev)                                              # the host ends the device's streams with the stored refusal
    expect(signed_in.locator("#remote-notice")).to_contain_text("removed from that computer", timeout=30000)
    for _ in range(100):
        if frame_of(signed_in).evaluate("window.__es.readyState") == 2:
            break
        signed_in.wait_for_timeout(100)
    assert frame_of(signed_in).evaluate("window.__es.readyState") == 2
    opens = len(host.stream_opens)
    frame_of(signed_in).evaluate("() => { new EventSource('/events'); }")      # the page tries again: nothing goes out
    signed_in.wait_for_timeout(1500)
    assert len(host.stream_opens) == opens


def test_a_page_that_reconnects_at_once_is_held_to_one_reconnect_in_ten_seconds(signed_in, live_server, make_host):
    host = make_host()
    open_dashboard(signed_in, live_server, host)
    frame_of(signed_in).evaluate("""() => {
      const go = () => { const es = new EventSource('/events'); es.onerror = () => { es.close(); window.__tries = (window.__tries || 0) + 1; go(); }; };
      go();
    }""")
    wait_stream(host)
    host.end("/events")                                           # the host closes it; the page asks again straight away
    expect(signed_in.locator("#remote-notice")).to_contain_text("Reconnecting to the computer in", timeout=15000)
    assert len(host.stream_opens) == 1                            # nothing has gone out yet
    wait_stream(host, 1)
    assert len(host.stream_opens) == 2
    gap = host.stream_opens[1][1] - host.stream_opens[0][1]
    assert 9.5 <= gap < 14, gap
    expect(signed_in.locator("#remote-notice")).to_be_hidden(timeout=15000)      # the host answered again: the note is gone


# ---- the viewer and the download ----------------------------------------------------------------------------------

FILES = {
    "/": {"status": 200, "headers": HTML, "page": True,
          "body": '<!doctype html><html><head><title>Files</title></head><body><h1>Home dashboard</h1>'
                  '<a id="txt" href="/f/notes.txt">t</a> <a id="html" href="/f/evil.html">h</a> <a id="bin" href="/f/data.bin">b</a> '
                  '<a id="png" href="/f/pic.png">p</a></body></html>'},
    "/f/notes.txt": {"status": 200, "headers": {"content-type": "text/plain"}, "body": "hello <b>not bold</b> from the computer"},
    "/f/evil.html": {"status": 200, "headers": {"content-type": "text/html"}, "body": "<script>window.parent.__pwn = 1</script><h1>evil</h1>"},
    "/f/data.bin": {"status": 200, "headers": {"content-type": "application/octet-stream", "content-disposition": 'attachment; filename="data.bin"'}, "body": b"\x00\x01\x02"},
    "/f/pic.png": {"status": 200, "headers": {"content-type": "image/png"},
                   "body": bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8cfc0f01f0005000201a7c2c8780000000049454e44ae426082")},
}


def test_a_text_file_opens_in_the_viewer_as_text_and_html_never_does(signed_in, live_server, make_host):
    host = make_host(pages=FILES)
    pair(signed_in, live_server, host)
    signed_in.goto(f"{live_server.url}/remote?space={host.space}")
    wait_h1(signed_in, "Home dashboard")
    frame_of(signed_in).locator("#txt").click()
    viewer = signed_in.locator(".remote-viewer")
    expect(viewer).to_contain_text("hello <b>not bold</b> from the computer", timeout=30000)
    assert viewer.locator("b, script, iframe").count() == 0
    assert frame_of(signed_in).evaluate("document.querySelector('h1').textContent") == "Home dashboard"      # the frame did not navigate
    signed_in.wait_for_timeout(1100)                                                                           # one gated action a second
    frame_of(signed_in).locator("#html").click()
    expect(viewer).to_contain_text("cannot be shown here", timeout=30000)
    assert viewer.locator("script, iframe, h1").count() == 0
    assert signed_in.evaluate("window.__pwn") is None
    assert frame_of(signed_in).evaluate("document.querySelector('h1').textContent") == "Home dashboard"
    signed_in.wait_for_timeout(1100)
    frame_of(signed_in).locator("#png").click()
    expect(viewer.locator("img")).to_be_visible(timeout=30000)


def test_a_download_asks_first_and_saves_on_the_click(signed_in, live_server, make_host):
    host = make_host(pages=FILES)
    pair(signed_in, live_server, host)
    signed_in.goto(f"{live_server.url}/remote?space={host.space}")
    wait_h1(signed_in, "Home dashboard")
    frame_of(signed_in).locator("#bin").click()
    prompt = signed_in.locator(".frame-prompt")
    expect(prompt).to_contain_text("data.bin (3 bytes)", timeout=30000)
    with signed_in.expect_download() as dl:
        prompt.get_by_role("button", name="Download").click()
    assert dl.value.suggested_filename == "data.bin"
    with open(dl.value.path(), "rb") as f:
        assert f.read() == b"\x00\x01\x02"


def test_the_status_page_opens_an_online_workspace_in_the_live_view(signed_in, live_server, make_host):
    host = make_host()
    pair(signed_in, live_server, host)
    signed_in.goto(f"{live_server.url}/workspaces")
    signed_in.get_by_role("link", name="Open Acme Energy").click()
    wait_h1(signed_in, "Home dashboard")
    assert signed_in.url == f"{live_server.url}/remote?space={host.space}"
