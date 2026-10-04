"""/sandbox/html in a real browser (spec T15, plan Review Focus #1): the frame protocol renders an
agent's HTML, and the sandbox actually holds — no access to the parent, cookies, IndexedDB or the
API, and no top-level navigation.

Task 3 wires this into the real ticket timeline (an "Open preview" card). Here the parent is a tiny
harness page served, via Playwright request interception, from the same origin as the live server —
so this behaves exactly like the real preview overlay will: a same-origin parent embedding
`<iframe sandbox="allow-scripts" src="/sandbox/html?v=...">` and posting `tix-render` after load.
"""
import json
import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

HARNESS_PATH = "/__sandbox_harness__"

# The attachment's own script: runs once inside the frame, records a marker, then tries five escapes
# and writes what happened for each into the DOM, so the test can read them back through
# frame_locator without ever trusting a postMessage back from the (deliberately mute) frame.
PROBE_HTML = """<!doctype html>
<html><body>
<div id="ran">not-run</div>
<div id="origin">unknown</div>
<div id="probe-parent-doc">pending</div>
<div id="probe-cookie">pending</div>
<div id="probe-idb">pending</div>
<div id="probe-fetch">pending</div>
<div id="probe-nav">pending</div>
<script>
  function set(id, v) { document.getElementById(id).textContent = v; }
  set('ran', 'ran');
  set('origin', String(window.origin));

  try { void parent.document; set('probe-parent-doc', 'leaked'); }
  catch (e) { set('probe-parent-doc', 'blocked'); }

  try {
    var c = document.cookie;
    set('probe-cookie', c ? ('leaked:' + c) : 'readable-empty');
  } catch (e) { set('probe-cookie', 'blocked'); }

  try {
    if (typeof indexedDB === 'undefined' || indexedDB === null) throw new Error('unavailable');
    var req = indexedDB.open('probe');
    req.onerror = function () { set('probe-idb', 'blocked'); };
    req.onsuccess = function () { set('probe-idb', 'leaked'); };
  } catch (e) { set('probe-idb', 'blocked'); }

  fetch('/api/healthz').then(function () {
    set('probe-fetch', 'leaked');
  }).catch(function () {
    set('probe-fetch', 'blocked');
  });

  try {
    top.location = '/x';
    set('probe-nav', 'leaked');
  } catch (e) {
    set('probe-nav', 'blocked');
  }
</script>
</body></html>"""

SECOND_HTML = "<!doctype html><body><div id=\"ran\">second-message-should-not-render</div></body>"


def _js_string(s: str) -> str:
    # A literal "</script" inside a <script> element's text ends it early, in the HTML parser, no
    # matter that it's really inside a JS string: it never reaches the JS parser at all. Break up
    # every such sequence so the outer harness script stays intact.
    return json.dumps(s).replace("</", "<\\/")


def _harness_html(iframe_src: str, render_html: str) -> str:
    # A plain, unrestricted page (Playwright serves it, not the app, so no CSP applies to it) that
    # mirrors the real preview overlay's wiring: create the sandboxed iframe, and post the HTML once
    # its `load` event fires, with targetOrigin "*" (the frame's origin is opaque).
    return f"""<!doctype html>
<html><body>
<iframe id="f" sandbox="allow-scripts" referrerpolicy="no-referrer" src="{iframe_src}"></iframe>
<script>
  // A readable (non-HttpOnly) cookie on the tix origin: a frame that wrongly ran at this origin
  // would read it, so an empty string can't pass as "blocked" (the session cookie is HttpOnly).
  document.cookie = 'probe=1; path=/';
  window.__posted__ = false;
  const frame = document.getElementById('f');
  frame.addEventListener('load', () => {{
    if (window.__posted__) return; // only the harness's own first post; a second is sent explicitly
    window.__posted__ = true;
    frame.contentWindow.postMessage({{type: 'tix-render', html: {_js_string(render_html)}}}, '*');
  }});
  window.__postAgain__ = (html) => {{
    frame.contentWindow.postMessage({{type: 'tix-render', html}}, '*');
  }};
</script>
</body></html>"""


@pytest.fixture
def harness(page, live_server):
    """Loads the harness at the tix origin and returns the frame_locator for the sandboxed iframe."""
    def _load(render_html: str):
        html = _harness_html(f"{live_server.url}/sandbox/html?v=test", render_html)
        page.route(f"{live_server.url}{HARNESS_PATH}",
                   lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        page.goto(live_server.url + HARNESS_PATH)
        return page.frame_locator("#f")

    return _load


def test_the_attachment_script_runs_inside_the_sandbox(harness):
    frame = harness(PROBE_HTML)
    expect(frame.locator("#ran")).to_have_text("ran")


def test_the_frame_has_an_opaque_null_origin(harness):
    frame = harness(PROBE_HTML)
    expect(frame.locator("#origin")).to_have_text("null")


@pytest.mark.parametrize("probe_id", ["probe-parent-doc", "probe-cookie", "probe-idb", "probe-fetch", "probe-nav"])
def test_every_escape_probe_is_blocked(harness, probe_id):
    frame = harness(PROBE_HTML)
    locator = frame.locator(f"#{probe_id}")
    expect(locator).not_to_have_text("pending", timeout=5000)
    expect(locator).not_to_have_text(re.compile("^leaked"))


def test_the_cookie_probe_throws_in_the_opaque_origin(harness, page):
    frame = harness(PROBE_HTML)
    # A SecurityError in the opaque origin, not merely an empty (or HttpOnly-hidden) cookie string.
    expect(frame.locator("#probe-cookie")).to_have_text("blocked")
    assert "probe=1" in page.evaluate("document.cookie")      # the parent's cookie was there to read


def test_top_navigation_is_blocked_so_the_top_page_url_is_unchanged(harness, page, live_server):
    frame = harness(PROBE_HTML)
    expect(frame.locator("#probe-nav")).to_have_text("blocked")
    assert page.url == live_server.url + HARNESS_PATH


def test_a_second_message_is_ignored_only_the_first_ever_renders(harness, page):
    frame = harness(PROBE_HTML)
    expect(frame.locator("#ran")).to_have_text("ran")
    page.evaluate(f"window.__postAgain__({json.dumps(SECOND_HTML)})")
    # No re-render: the marker from the first message is still there, and the second's marker text
    # (which would appear as "ran" too, since it reuses the id) never overwrites it.
    page.wait_for_timeout(200)
    expect(frame.locator("#ran")).to_have_text("ran")
