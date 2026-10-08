"""Typing into a terminal from the phone, end to end (R6 phone side): the dashboard's terminal page runs in the sandboxed
frame, watches its terminal over a stream, batches its key posts (orchHost.remote), and the computer (a fake host with
the real host's lease rules, tests/support/fake_terminal_host.py) lets them type only after the unlock sheet.

The page is a stand-in for orch-core's terminal.js: it has that file's key batcher verbatim (between its batcher:begin
and batcher:end markers) and the same posts ({seq, n, page}, one in flight, same body and n on a failed attempt, any
other answer ends the post), plus the stream. The real file needs the real orch-core and a real phone (the PR says what).
WebAuthn needs a real host name as its RP id, so the browser uses localhost, like test_unlock.py."""
import time

import pytest
from playwright.sync_api import expect

from tests.support.fake_terminal_host import TerminalHost

from .conftest import make_desktop
from .test_remote import HTML, dash, frame_of, wait_h1
from .test_dash_frame import v
# fixtures and helpers of the unlock tests: the platform authenticator, the localhost server, the signed-in page, pairing
from .test_unlock import _chromium_only, authenticator, base, confirm, live_server, open_dash, pair, sheet, signed_in  # noqa: F401

pytestmark = pytest.mark.browser

BATCHER = """
  const MAX_POST_ITEMS = 64;
  const MAX_POST_CHARS = 2048;
  const takePost = (queue) => {  // up to one post's worth from the front of the queue, splitting a long text item
    const items = [];
    let chars = 0;
    while (queue.length && items.length < MAX_POST_ITEMS) {
      const it = queue[0];
      if (!it.text) { items.push(queue.shift()); continue; }
      const room = MAX_POST_CHARS - chars;
      if (room <= 0) break;
      if (it.text.length <= room) { items.push(queue.shift()); chars += it.text.length; continue; }
      let cut = room;
      const c = it.text.charCodeAt(cut - 1);
      if (c >= 0xD800 && c <= 0xDBFF) cut -= 1;  // never cut an emoji (a surrogate pair) in half
      if (cut <= 0) break;
      items.push({ text: it.text.slice(0, cut) });
      queue[0] = { text: it.text.slice(cut) };
      break;
    }
    return items;
  };
  const makeBatcher = ({ send, ms, now = Date.now, timer = setTimeout }) => {
    const queue = [];
    let pending = null;  // the post being sent (or sent again): its items and number never change
    let busy = false;
    let armed = false;
    let n = 0;
    let retry = false;
    const arm = () => { if (!armed) { armed = true; timer(flush, ms); } };
    const flush = async () => {
      armed = false;
      if (busy) return;
      if (!pending) {
        if (!queue.length) return;
        n = Math.max(n + 1, now());
        pending = { items: takePost(queue), n };
        retry = false;
      }
      busy = true;
      let status = 0;
      try { status = await send(pending.items, pending.n, retry); } catch (_) { status = 0; }
      busy = false;
      if (status === 0 || status === 429) retry = true;
      else pending = null;
      if (pending || queue.length) arm();
    };
    const push = (item) => {
      const last = queue[queue.length - 1];
      if (item.text && last && last.text) last.text += item.text;
      else queue.push(item);
      arm();
    };
    return { push, flush, active: () => Boolean(pending || queue.length) };
  };
"""
TERMINAL_JS = """(() => {
  const host = window.orchHost, $ = (id) => document.getElementById(id);
  window.__posts = [];       // [n, status] of every answered post
  window.__failed = 0;       // posts that did not arrive (the page sends them again, unchanged)
  const es = new EventSource("/terminals/work/stream");
  es.addEventListener("screen", (e) => { $("screen").textContent = JSON.parse(e.data); });
  window.closeStream = () => es.close();
  window.__size = [];        // the page sizes its view also while only watching (the real page does on every resize)
  window.sizeIt = () => fetch("/terminals/work/size", { method: "POST", credentials: "same-origin", headers: { "content-type": "application/json" }, body: JSON.stringify({ cols: 80, rows: 24 }) })
    .then((r) => { window.__size.push(r.status); return r.status; }, () => { window.__size.push(0); return 0; });
  window.sizeIt();
""" + BATCHER + """
  const page = "pg" + Math.random().toString(36).slice(2, 10);
  const post = (body) => fetch("/terminals/work/keys", { method: "POST", credentials: "same-origin", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  const send = (items, n) => post({ seq: items, n, page }).then(async (r) => {
    window.__posts.push([n, r.status]);
    $("status").textContent = r.status === 204 ? "ok" : r.status + " " + await r.text();
    return r.status;
  }, (e) => { window.__failed++; throw e; });
  const batcher = host.remote ? makeBatcher({ send, ms: 150 }) : null;
  window.batching = Boolean(batcher);
  window.typeKeys = (t) => batcher.push({ text: t });
  window.waiting = () => batcher.active();
  window.startNew = () => fetch("/terminals/new", { method: "POST", credentials: "same-origin" }).then((r) => { $("status").textContent = "new " + r.status; return r.status; }, () => { $("status").textContent = "new failed"; return 0; });
  window.replay = (n) => post({ seq: [{ text: "REPLAY" }], n, page }).then(async (r) => { $("status").textContent = r.status + " " + await r.text(); return r.status; });
})();"""
TERMINAL_PAGE = {"status": 200, "headers": HTML, "page": True, "body": f"""<!doctype html><html><head><meta charset="utf-8"><title>Terminal work</title>
<script src="/static/terminal.js?v={v(TERMINAL_JS)}"></script></head>
<body><h1 id="h">Terminal work</h1><pre id="screen">(waiting for the screen)</pre><p id="status"></p></body></html>"""}
HOME = {"status": 200, "headers": HTML, "page": True, "body": '<!doctype html><html><head><meta charset="utf-8"><title>Home</title></head><body><h1 id="h">Home</h1><a id="go" href="/terminals/work">work</a></body></html>'}
PAGES = {"/": HOME, "/terminals/work": TERMINAL_PAGE, "/static/terminal.js": {"status": 200, "headers": {"content-type": "text/javascript"}, "body": TERMINAL_JS}}


def until(cond, seconds=30, what="condition"):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out: {what}")


@pytest.fixture
def make_host(live_server, sim, base):
    hosts = []

    def make(label="Acme Energy"):
        space, headers = make_desktop(live_server, sim, label=label)
        host = TerminalHost(live_server.url, space, headers, sim.mk, PAGES).start()
        host.origin = base
        hosts.append(host)
        return host
    yield make
    for h in hosts:
        h.stop()


@pytest.fixture
def term(signed_in, base, make_host, authenticator):
    """Paired at Type with a platform credential, the terminal page open and its stream open."""
    page = signed_in
    host = make_host()
    dev = pair(page, base, host, scope="type")
    open_dash(page, base, host)
    frame_of(page).locator("#go").click()
    wait_h1(page, "Terminal work")
    until(lambda: host.open, what="the terminal stream opened")
    host.push_screen("work", "$ ")
    expect(frame_of(page).locator("#screen")).to_have_text("$ ")
    return page, host, dev


def typed_in_frame(page, text):
    frame_of(page).evaluate("(t) => window.typeKeys(t)", text)


def note(page):
    return page.locator("#lease-note")


def test_the_frame_says_it_is_remote_and_the_page_cannot_change_that(term):
    page, host, _ = term
    f = frame_of(page)
    assert f.evaluate("window.orchHost.remote") is True and f.evaluate("window.batching") is True
    assert f.evaluate("(() => { try { window.orchHost.remote = false; } catch (e) {} return window.orchHost.remote; })()") is True
    assert f.evaluate("Object.isFrozen(window.orchHost)") is True


def test_watch_then_type_asks_once_with_the_sheet_and_the_keys_arrive_in_order(term):
    page, host, dev = term
    typed_in_frame(page, "ls")
    expect(sheet(page)).to_be_visible(timeout=30000)
    expect(page.locator("#unlock-text")).to_have_text("Type for 15 minutes")
    assert host.typed == []                                   # nothing typed before the person confirmed
    typed_in_frame(page, " -la")                              # typed while the sheet is open: queued behind the first post
    typed_in_frame(page, "\r")
    confirm(page)
    until(lambda: "".join(host.typed) == "ls -la\r", what="all keys typed, in order")
    expect(sheet(page)).to_have_count(0)
    assert [a["purpose"] for a in host.audit] == ["lease"] and host.audit[0]["ok"]
    expect(note(page)).to_contain_text("Typing unlocked until")
    posts = [p for p in host.posts if p["path"].endswith("/keys") and p["status"] == 204]
    ns = [p["body"]["n"] for p in posts]
    assert ns == sorted(set(ns)) and len(ns) >= 1             # one number per post, rising
    assert [p["body"]["page"] for p in posts] == [posts[0]["body"]["page"]] * len(posts)
    typed_in_frame(page, "pwd")
    until(lambda: "".join(host.typed) == "ls -la\rpwd", what="the lease is open: no second sheet")
    assert sheet(page).count() == 0 and len(host.audit) == 1
    # the first refused post was run once by the computer after the proof, never sent a second time with its number
    assert [p["body"]["n"] for p in host.posts if p["status"] == 204].count(ns[0]) == 1
    assert frame_of(page).evaluate("window.__failed") == 0


def test_a_409_is_shown_not_hidden_and_types_nothing(term):
    page, host, _ = term
    typed_in_frame(page, "a")
    expect(sheet(page)).to_be_visible(timeout=30000)
    confirm(page)
    until(lambda: host.typed == ["a"])
    f = frame_of(page)
    assert f.evaluate("window.replay(1)") == 409              # a number already used
    expect(f.locator("#status")).to_have_text("409 post number already used")
    assert host.typed == ["a"] and host.posts[-1]["status"] == 409


def test_when_the_lease_runs_out_the_next_keys_ask_again_and_nothing_is_lost(term):
    page, host, dev = term
    typed_in_frame(page, "one")
    expect(sheet(page)).to_be_visible(timeout=30000)
    confirm(page)
    until(lambda: host.typed == ["one"])
    host.leases[dev] = host._now() - 1                         # the 15 minutes are over
    typed_in_frame(page, "two")
    expect(sheet(page)).to_be_visible(timeout=30000)
    expect(note(page)).to_be_hidden()                          # the note went with the lease
    typed_in_frame(page, "three")
    confirm(page)
    until(lambda: "".join(host.typed) == "onetwothree", what="the keys after the second sheet, in order")
    assert [a["purpose"] for a in host.audit] == ["lease", "lease"] and all(a["ok"] for a in host.audit)
    expect(note(page)).to_contain_text("Typing unlocked until")


def test_cancelling_the_sheet_keeps_the_keys_says_so_and_does_not_nag(term):
    page, host, dev = term
    typed_in_frame(page, "keep")
    expect(sheet(page)).to_be_visible(timeout=30000)
    page.locator("#unlock-cancel").click()
    expect(sheet(page)).to_have_count(0)
    expect(page.locator("#remote-notice")).to_contain_text("Not confirmed, so nothing was done")
    assert host.typed == []
    sent = len(host.refused)
    page.wait_for_timeout(2500)                                # the page resends every second: nothing goes out and no sheet opens
    assert sheet(page).count() == 0 and len(host.refused) == sent and host.typed == []
    expect(page.locator("#remote-notice")).to_contain_text("Your keys are kept")
    assert frame_of(page).evaluate("window.waiting()") is True and frame_of(page).evaluate("window.__failed") >= 1
    # the glue reads Date.now at every use, so this moves its clock (no real waits): 20 s in, still cooling; 32 s in, over
    page.evaluate("() => { const n = Date.now; window.__skew = 20000; Date.now = () => n() + window.__skew; }")
    page.wait_for_timeout(1800)
    assert sheet(page).count() == 0 and len(host.refused) == sent
    page.evaluate("() => { window.__skew = 32000; }")
    expect(sheet(page)).to_be_visible(timeout=3000)
    confirm(page)
    until(lambda: host.typed == ["keep"], what="the kept keys, once")
    bodies = [p["body"] for p in host.posts if p["status"] == 204]
    assert len(bodies) == 1


def test_a_revoked_browser_gets_the_refusal_text_and_types_nothing(term):
    page, host, dev = term
    typed_in_frame(page, "x")
    expect(sheet(page)).to_be_visible(timeout=30000)
    confirm(page)
    until(lambda: host.typed == ["x"])
    host.revoke(dev)
    typed_in_frame(page, "y")
    expect(page.locator("#remote-notice")).to_contain_text("removed from that computer", timeout=30000)
    assert host.typed == ["x"]
    expect(note(page)).to_be_hidden()
    n = len(host.posts)
    page.wait_for_timeout(2500)
    assert len(host.posts) == n                                # nothing more is sent to a computer that revoked us


def test_without_an_open_stream_typing_is_refused_in_words_and_nothing_is_sent(term):
    page, host, dev = term
    frame_of(page).evaluate("window.closeStream()")
    until(lambda: not host.open, what="the stream closed on the computer")
    typed_in_frame(page, "z")
    expect(page.locator("#remote-notice")).to_contain_text("Typing needs the live screen", timeout=30000)
    assert host.typed == [] and host.audit == [] and sheet(page).count() == 0


def test_a_new_session_is_a_fresh_assertion_with_its_own_text_and_names_no_stream(term):
    page, host, dev = term
    frame_of(page).evaluate("() => { window.startNew(); }")
    expect(sheet(page)).to_be_visible(timeout=30000)
    expect(page.locator("#unlock-text")).to_have_text("Start a session")       # not the lease text
    assert host.posts == []
    confirm(page)
    until(lambda: any(p["path"] == "/terminals/new" for p in host.posts), what="the start ran once")
    assert host.headers_seen[-1] == ("/terminals/new", "")                      # no stream header
    assert [a["purpose"] for a in host.audit] == ["fresh"] and host.leases.get(dev, 0) == 0
    expect(note(page)).to_be_hidden()


def test_only_watching_asks_for_nothing_and_shows_nothing(term):
    page, host, dev = term
    f = frame_of(page)
    until(lambda: f.evaluate("window.__size.length") >= 1, what="the page sized its view")
    f.evaluate("window.sizeIt()")
    until(lambda: f.evaluate("window.__size.length") >= 2)
    page.wait_for_timeout(1500)
    assert f.evaluate("window.__size") == [409, 409]          # the refusal the page ignores
    assert sheet(page).count() == 0 and host.audit == [] and host.refused == [] and host.posts == []
    expect(page.locator("#remote-notice")).to_be_hidden()


def test_the_stream_closing_while_the_sheet_is_open_is_a_readable_refusal_and_types_nothing(term):
    page, host, dev = term
    typed_in_frame(page, "q")
    expect(sheet(page)).to_be_visible(timeout=30000)
    host.end_streams()                                        # the live view goes away while the person decides
    confirm(page)
    expect(page.locator("#remote-notice")).to_contain_text("not allowed to do that", timeout=30000)
    assert host.typed == [] and host.posts == []              # the post number was never taken
    assert host.audit[-1]["why"] == "forbidden_scope"
    expect(note(page)).to_be_hidden()
    assert host.leases.get(dev, 0) == 0                       # and no lease was opened


def test_switching_workspace_clears_the_typing_unlocked_note(signed_in, base, make_host, authenticator):
    a = make_host("Acme Energy")
    b = make_host("Second")
    pair(signed_in, base, a, scope="type")
    pair(signed_in, base, b, scope="type")
    open_dash(signed_in, base, a)
    frame_of(signed_in).locator("#go").click()
    wait_h1(signed_in, "Terminal work")
    until(lambda: a.open)
    typed_in_frame(signed_in, "x")
    expect(sheet(signed_in)).to_be_visible(timeout=30000)
    confirm(signed_in)
    expect(note(signed_in)).to_contain_text("Typing unlocked until", timeout=30000)
    signed_in.get_by_role("button", name="Open Second").click()
    expect(note(signed_in)).to_be_hidden(timeout=30000)
