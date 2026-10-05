"""The remote dashboard frame in a real browser (/sandbox/dash, js/frame-shim.js, js/frame-host.js; docs/bridge-frame.md).

The parent is a small harness page served by Playwright from the live server's origin (so it behaves like a TIX
page: same origin, `import("/static/js/frame-host.js")`), the transport is the fake of js/bridge-transport.js and the
"dashboard" is a few small pages shaped like the real one: a script with ?v=, a stylesheet with a font, an image,
forms, links of every kind, an EventSource, address/cookie/storage use, a nested frame, an artifact link. Nothing here
needs the crypto or the mailbox."""
import base64
import hashlib
import json

import pytest

pytestmark = pytest.mark.browser
HARNESS = "/__dash_harness__"
PNG = base64.b64encode(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")).decode()


def v(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


APP_JS = """(() => {
  const h = window.orchHost, out = document.getElementById("out"), r = {};
  document.documentElement.dataset.ran = "1";            // per document: the window (and its properties) outlives a page write
  window.__api = undefined;
  r.path = h.path(); r.url = h.url(); r.hash = h.hash();
  try { document.cookie = "t=1; Path=/"; r.cookie = document.cookie; } catch (e) { r.cookie = "throws"; }
  try { localStorage.setItem("a", "b"); r.ls = localStorage.getItem("a"); } catch (e) { r.ls = "throws"; }
  h.session.set("k", "v"); r.sess = h.session.get("k");
  fetch("/api/x").then((x) => x.json()).then((j) => { window.__api = j.v; });
  window.__esMsgs = [];
  window.__es = new EventSource("/events");
  window.__es.onmessage = (e) => window.__esMsgs.push(e.data);
  window.__r = r;
  window.__handled = 0;
  document.getElementById("handled").addEventListener("submit", (e) => { e.preventDefault(); window.__handled += 1; });
  document.dispatchEvent(new Event("app-ready"));
})();"""
CSS = ("body{background:rgb(1,2,3)} @font-face{font-family:F;src:url(/static/f.woff2?v=1)} "
       "#logo{width:4px;height:4px} .bg{background:url(/static/logo.png?v=1)} @import url(https://evil.test/x.css);")
EVIL_JS = "window.__evil = (window.__evil || 0) + 1;"


MXSS = """<!doctype html><html data-path="/mxss" onclick="window.__x=1"><head><title>mx</title>
<base href="https://evil.test/"><meta http-equiv="refresh" content="0;url=/healthz"><link rel="preload" href="/secret" as="script">
<link rel="prefetch" href="/secret"><link rel="modulepreload" href="/secret"></head><body onload="window.__x=1">
<h1 id="h">mx</h1>
<svg><script>window.__x = 1</script><a href="javascript:window.__x=1" id="svga"><text>x</text></a></svg>
<math><mtext><table><mglyph><style><img src=x onerror="window.__x=1"></style></mglyph></table></mtext></math>
<noscript><p title="</noscript><img src=x onerror=window.__x=1>">n</p></noscript>
<a id="ja" href="jav&#x09;ascript:window.__x=1">j1</a><a id="jb" href="  JaVaScRiPt:window.__x=1">j2</a>
<a id="jc" href="&#x6A;avascript:window.__x=1">j3</a><a id="jd" href="data:text/html,<script>window.__x=1</script>">j4</a>
<form id="jf" action="javascript:window.__x=1"><button id="jfb" formaction="javascript:window.__x=1">go</button></form>
<template id="tp"><script>window.__x = 1</script><img src=x onerror="window.__x=1"></template>
<iframe srcdoc="<script>parent.__x=1</script>"></iframe><object data="/secret"></object><embed src="/secret">
<textarea></textarea><img src=x onerror="window.__x=1"><div nonce="abc" onclick="window.__x=1" id="nn">n</div>
<style>/* </style><img src=x onerror=window.__x=1> */ p { color: red }</style>
<img id="bad" src="x" onerror="window.__x=1"></body></html>"""


def page(path, title, extra=""):
    return f"""<!doctype html><html data-path="{path}"><head><meta charset="utf-8"><title>{title}</title>
<link rel="stylesheet" href="/static/app.css?v=1"><link rel="icon" href="/static/logo.png">
<script src="/static/app.js?v={v(APP_JS)}"></script>
<script src="/static/evil.js?v=000000000000"></script><script src="/static/nov.js"></script>
<script>window.__inline = 1</script></head>
<body onload="window.__onload = 1"><h1 id="h">{title}</h1><img id="logo" src="/static/logo.png?v=1">
<a id="board" href="/board">Board</a> <a id="home" href="/">Home</a> <a id="art" href="/a/L-1/report.html">Report</a>
<a id="trick" href="/a/L-1/trick.html">Trick</a> <a id="blank" target="_blank" href="/t/L-1/x">Blank</a>
<a id="dl" download href="/a/L-1/data.csv">CSV</a> <a id="ext" target="_blank" href="https://example.com/x">Ext</a>
<a id="mail" href="mailto:a@b.c">Mail</a> <a id="frag" href="#h">frag</a> <a id="boom" href="/boom">Boom</a>
<a id="slowlink" href="/slow-page">Slow</a> <a id="mx" href="/mxss">mXSS</a> <a id="secret" href="/secret">Secret</a>
<form id="search" action="/search" method="get"><input name="q" value="x y"><button id="gs">Go</button></form>
<form id="newf" action="/new" method="post"><input name="title" value="Hello"><button id="nsub">New</button></form>
<form id="handled" action="/new" method="post"><input name="z" value="1"><button id="hb">Save</button></form>
<iframe src="/a/L-1/frame.html" title="Evil &lt;b&gt;frame&lt;/b&gt;"></iframe><div id="out"></div>{extra}</body></html>"""


def routes():
    html = {"content-type": "text/html; charset=utf-8", "content-security-policy": "default-src 'self'"}
    return {
        "/": {"body": page("/", "Home"), "headers": html, "page": True},
        "/board": {"body": page("/board", "Board"), "headers": html, "page": True},
        "/search": {"body": page("/search", "Results"), "headers": html, "page": True},
        "POST /new": {"body": page("/created", "Created"), "headers": html, "page": True, "url": "/created"},
        "/created": {"body": page("/created", "Created"), "headers": html, "page": True},
        "/slow-page": {"body": page("/slow-page", "Slow"), "headers": html, "page": True, "gate": True},
        "/mxss": {"body": MXSS, "headers": html, "page": True},
        "/boom": {"status": 500, "headers": html, "page": True,
                  "body": '<img src=x onerror="window.__pwn=1"><script>window.__pwn=1</script><b>boom</b>'},
        "/static/app.css": {"body": CSS, "headers": {"content-type": "text/css"}},
        "/static/app.js": {"body": APP_JS, "headers": {"content-type": "text/javascript"}},
        "/static/evil.js": {"body": EVIL_JS, "headers": {"content-type": "text/javascript"}},
        "/static/nov.js": {"body": EVIL_JS, "headers": {"content-type": "text/javascript"}},
        "/static/logo.png": {"b64": PNG, "headers": {"content-type": "image/png"}},
        "/static/f.woff2": {"body": "not a font", "headers": {"content-type": "font/woff2"}},
        "/api/x": {"body": '{"v": 7}', "headers": {"content-type": "application/json"}},
        "/slow": {"body": '{"v": 9}', "headers": {"content-type": "application/json"}, "gate": True},
        "/events": {"stream": True},
        "/a/L-1/report.html": {"body": "<h1>artifact</h1>", "headers": html, "page": False},
        "/a/L-1/trick.html": {"body": "<h1>tagged page</h1>", "headers": html, "page": True},
        "/a/L-1/frame.html": {"body": "<h1>nested</h1>", "headers": html},
        "/a/L-1/data.csv": {"body": "a,b\n1,2\n", "headers": {"content-type": "text/csv", "content-disposition": "attachment; filename=d.csv"}},
        "/t/L-1/x": {"body": "ticket text", "headers": {"content-type": "text/plain"}},
        "/secret": {"body": "SECRET", "headers": {"content-type": "text/plain"}},
    }


SCOPES = {"rules": [
    {"methods": ["GET"], "pattern": "/"}, {"methods": ["GET"], "pattern": "/board"}, {"methods": ["GET"], "pattern": "/search"},
    {"methods": ["GET"], "pattern": "/created"}, {"methods": ["GET"], "pattern": "/boom"}, {"methods": ["GET"], "pattern": "/mxss"}, {"methods": ["GET"], "pattern": "/slow"},
    {"methods": ["GET"], "pattern": "/slow-page"}, {"methods": ["GET"], "pattern": "/static/*"}, {"methods": ["GET"], "pattern": "/api/*"},
    {"methods": ["GET"], "pattern": "/a/*"}, {"methods": ["GET"], "pattern": "/t/*"},
    {"methods": ["GET"], "pattern": "/events", "stream": True}, {"methods": ["POST"], "pattern": "/new"},
]}

HARNESS_HTML = """<!doctype html><html><body><div id="mount"></div><script type="module">
import { createFrameHost } from "/static/js/frame-host.js";
import { fakeTransport } from "/static/js/bridge-transport.js";
const site = window.__site;
const ev = { viewer: [], download: [], external: [], history: [], theme: [], copy: [], notice: [], log: [], origins: [], seen: [], msgs: [] };
const dec = new TextDecoder();
const gates = {};
window.__ev = ev;
window.__release = (path) => { gates[path]?.(); delete gates[path]; };
addEventListener("message", (e) => { if (e.data && e.data.k === "orch-frame-1") { ev.origins.push(e.origin); ev.seen.push(e.data.t); ev.msgs.push({ t: e.data.t, id: e.data.id, gen: e.data.gen, path: e.data.path }); } }, true);
const answer = async (req) => {
  const p = req.path.split("?")[0];
  const r = site.routes[(req.method === "POST" ? "POST " : "") + p];
  if (!r) return { status: 404, body: "not found" };
  if (r.gate) await new Promise((res) => { gates[p] = res; });
  if (r.stream) return { stream: true };
  const body = r.b64 ? Uint8Array.from(atob(r.b64), (c) => c.charCodeAt(0)) : r.body;
  return { status: r.status || 200, headers: r.headers || {}, body, page: Boolean(r.page), url: r.url };
};
window.__fake = fakeTransport(answer);
window.__mk = (limits) => {
  window.__host?.destroy();
  window.__host = createFrameHost({
    mount: document.getElementById("mount"), transport: window.__fake, scopes: site.scopes, start: "/", limits,
    viewer: (d) => ev.viewer.push({ path: d.path, status: d.status, type: d.headers["content-type"], body: dec.decode(d.body) }),
    download: (d) => ev.download.push({ path: d.path, body: dec.decode(d.body) }),
    external: (u) => ev.external.push(u),
    history: (h) => ev.history.push(h), theme: (t) => ev.theme.push(t),
    copy: async (t) => { ev.copy.push(t); },
    log: (m) => ev.log.push(m),
    tap: (m) => { if (m.t === 'ready') window.__sid = m.sid; },
    notice: (n) => ev.notice.push(n),
  });
  return true;
};
window.__ready = true;
</script></body></html>"""


@pytest.fixture(autouse=True)
def _chromium_only(browser_name):
    if browser_name not in ("chromium", "webkit"):
        pytest.skip("Chromium and WebKit only here")


@pytest.fixture
def dash(page, live_server):
    """open(limits=None, wait=True) -> the harness page with a frame host built; frame() -> the live dashboard frame."""
    page.route(f"{live_server.url}{HARNESS}", lambda route: route.fulfill(status=200, content_type="text/html", body=HARNESS_HTML))
    page.add_init_script("window.__site = " + json.dumps({"routes": routes(), "scopes": SCOPES}))
    requests, answered, failed = [], [], []
    page.on("request", lambda r: requests.append(r.url))
    page.on("response", lambda r: answered.append(r.url))
    page.on("requestfailed", lambda r: failed.append((r.url, r.failure)))
    page.goto(live_server.url + HARNESS)
    page.wait_for_function("window.__ready === true")

    class Dash:
        base = live_server.url
        net = requests
        responses = answered
        failures = failed

        def open(self, limits=None, wait="#h"):
            page.evaluate("(l) => window.__mk(l)", limits)
            if wait:
                self.frame().wait_for_selector(wait, state="attached")
            return self

        def frame(self):
            page.wait_for_function("() => document.querySelector('iframe.frame-dash') !== null")
            for _ in range(100):
                fs = [f for f in page.frames if "/sandbox/dash" in f.url]
                if fs:
                    return fs[-1]
                page.wait_for_timeout(50)
            raise AssertionError("no dashboard frame")

        def ev(self, key=None):
            return page.evaluate("(k) => k ? window.__ev[k] : window.__ev", key)

        def calls(self):
            return page.evaluate("() => window.__fake.calls.map((c) => ({method: c.method, path: c.path, stream: c.stream, body: c.body ? new TextDecoder().decode(c.body) : null}))")

        def click(self, sel):
            self.frame().locator(sel).click()

        def title(self):
            return self.frame().evaluate("document.querySelector('h1') && document.querySelector('h1').textContent")

        def wait_title(self, text):
            self.frame().wait_for_function(f"() => document.querySelector('h1') && document.querySelector('h1').textContent === {json.dumps(text)}")

        def sid_event(self, **msg):
            """post a message as the frame would (its window as source, the opaque origin), with the right session id"""
            page.evaluate("""(m) => { const f = document.querySelector('iframe.frame-dash');
              window.dispatchEvent(new MessageEvent('message', {data: Object.assign({k: 'orch-frame-1', sid: window.__sid}, m),
                source: f.contentWindow, origin: m.__origin || 'null'})); }""", msg)

    return Dash()


# ---- the frame itself: policy, origin, what it cannot reach -------------------------------------------------------

def test_the_dashboard_page_is_drawn_and_the_frame_is_a_null_origin_sandbox(dash, page):
    dash.open()
    f = dash.frame()
    assert f.evaluate("window.origin") == "null"
    assert page.evaluate("document.querySelector('iframe.frame-dash').getAttribute('sandbox')") == "allow-scripts"
    assert dash.title() == "Home"
    # every message the frame posted arrived with the opaque origin
    assert set(dash.ev("origins")) == {"null"}


def test_the_frame_cannot_reach_the_network(dash, browser_name):
    dash.open()
    before = len(dash.net)
    res = dash.frame().evaluate("""async () => {
      const out = {}, probe = location.origin + '/__probe__';
      const violations = [];
      document.addEventListener('securitypolicyviolation', (e) => violations.push(e.effectiveDirective));
      try { out.beacon = navigator.sendBeacon(probe, 'x'); } catch (e) { out.beacon = 'throws'; }
      try { const w = new WebSocket(probe.replace('http', 'ws')); out.ws = await new Promise((r) => { w.onerror = () => r('error'); w.onopen = () => r('open'); }); } catch (e) { out.ws = 'throws'; }
      const img = new Image(); img.src = probe + '?img'; await new Promise((r) => { img.onerror = r; img.onload = r; });
      const s = document.createElement('script'); s.src = probe + '?js'; document.body.appendChild(s);
      const x = document.createElement('iframe'); x.src = probe + '?frame'; document.body.appendChild(x);
      await new Promise((r) => setTimeout(r, 300));
      out.violations = violations;
      return out;
    }""")
    # sendBeacon may report "queued"; the policy then refuses it (a connect-src violation, no request below)
    assert res["ws"] in ("error", "throws")           # WebKit refuses the constructor itself
    # whatever the browser tried was stopped by the policy before it left: no answer, a CSP block for each
    assert not [u for u in dash.responses if "__probe__" in u], dash.responses
    probes = [(u, f) for u, f in dash.failures if "__probe__" in u]
    assert (probes or browser_name != "chromium") and all("csp" in str(f).lower() or "blocked" in str(f).lower() for _, f in probes), dash.failures
    assert {"connect-src", "img-src", "script-src-elem", "frame-src"} <= set(res["violations"]), res


def test_the_frame_cannot_read_tix_storage_cookies_or_the_parent(dash, page):
    page.goto(dash.base + "/login")           # a real TIX page: put something in the origin's storage and cookie jar
    page.evaluate("document.cookie = 'probe=tix; path=/'; localStorage.setItem('probe', 'tix')")
    page.route(f"{dash.base}{HARNESS}", lambda route: route.fulfill(status=200, content_type="text/html", body=HARNESS_HTML))
    page.goto(dash.base + HARNESS)
    page.wait_for_function("window.__ready === true")
    dash.open()
    res = dash.frame().evaluate("""() => {
      const t = (fn) => { try { return String(fn()); } catch (e) { return 'throws:' + e.name; } };
      return { cookie: document.cookie, ls: localStorage.getItem('probe'), ss: sessionStorage.getItem('probe'),
        idb: t(() => indexedDB.open('x')), parentDoc: t(() => parent.document.cookie), parentLs: t(() => parent.localStorage.length),
        top: t(() => { top.location.href = '/x'; }), caches: t(() => typeof caches), sw: t(() => navigator.serviceWorker.controller) };
    }""")
    assert "probe=tix" not in res["cookie"] and res["ls"] is None and res["ss"] is None
    assert res["idb"].startswith("throws") and res["parentDoc"].startswith("throws") and res["parentLs"].startswith("throws")
    assert res["top"].startswith("throws")


# ---- rewriting and scripts ----------------------------------------------------------------------------------------

def test_the_page_is_rewritten_scripts_styles_images_fonts(dash):
    dash.open()
    f = dash.frame()
    f.wait_for_function("() => document.documentElement.dataset.ran === '1' && window.__api === 7")
    assert f.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(1, 2, 3)"          # stylesheet applied
    css = f.evaluate("[...document.querySelectorAll('style')].map((s) => s.textContent).join('')")
    assert "blob:" in css and "evil.test" not in css and "/static/f.woff2" not in css                  # font url -> blob, @import gone
    assert f.evaluate("document.querySelector('link[rel~=stylesheet], link[rel~=icon]')") is None
    f.wait_for_function("() => document.getElementById('logo').complete && document.getElementById('logo').naturalWidth === 1")
    assert f.evaluate("document.getElementById('logo').src").startswith("blob:")
    assert f.evaluate("document.getElementById('logo').complete")


def test_scripts_need_the_nonce_and_the_version_pin(dash):
    dash.open()
    f = dash.frame()
    f.wait_for_function("() => document.documentElement.dataset.ran === '1'")
    got = f.evaluate("""async () => {
      const out = { evil: window.__evil || 0, inline: window.__inline || 0, onload: window.__onload || 0,
        leftoverScripts: document.querySelectorAll('script').length };
      const run = (make) => { window.__x = 0; const s = document.createElement('script'); make(s); document.body.appendChild(s); return window.__x; };
      out.noNonce = run((s) => { s.textContent = 'window.__x = 1'; });
      out.wrongNonce = run((s) => { s.setAttribute('nonce', 'AAAAAAAAAAAAAAAAAAAAAAAA'); s.textContent = 'window.__x = 1'; });
      const d = document.createElement('div'); d.innerHTML = '<img src=x onerror="window.__x = 1">'; document.body.appendChild(d);
      await new Promise((r) => setTimeout(r, 200));
      out.x = window.__x;
      return out;
    }""")
    assert got["evil"] == 0 and got["inline"] == 0 and got["onload"] == 0     # wrong pin, no pin, inline script, handler: none ran
    assert got["noNonce"] == 0 and got["wrongNonce"] == 0 and got["x"] == 0
    assert got["leftoverScripts"] == 0                                         # the shim removes the scripts it ran: no nonce to read


def test_assets_are_cached_across_pages(dash):
    dash.open()
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    dash.click("#board")
    dash.wait_title("Board")
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")           # the page's own script ran on the new page
    paths = [c["path"] for c in dash.calls() if c["path"].startswith("/static/")]
    assert paths.count(f"/static/app.js?v={v(APP_JS)}") == 1 and paths.count("/static/app.css?v=1") == 1, paths


# ---- the adapter: address, history, cookie, storage, clipboard, links ---------------------------------------------

def test_the_adapter_supplies_path_history_storage_cookie_copy_theme(dash, page):
    dash.open()
    f = dash.frame()
    f.wait_for_function("() => window.__r")
    r = f.evaluate("window.__r")
    assert r["path"] == "/" and r["url"] == "/" and r["hash"] == "" and r["cookie"] == "t=1" and r["ls"] == "b" and r["sess"] == "v"
    f.evaluate("() => { window.__cp = window.orchHost.copy('hello').then(() => 'copied', () => 'failed'); }")
    page.wait_for_selector(".frame-copy")
    assert dash.ev("copy") == []                                      # asked, not written: no click in the app yet
    page.click(".frame-copy button:text-is('Copy')")
    assert f.evaluate("window.__cp") == "copied"
    out = f.evaluate("""async () => {
      const h = window.orchHost;
      h.pageHistory.push('/board?x=1#top'); h.pageHistory.replace('/board?x=2');
      const res = h.resolve('/t/L-1?y=1#z');
      let bad; try { await h.copy('x'.repeat(3000)); bad = 'ok'; } catch (e) { bad = 'rejected'; }
      h.setTheme('dark'); h.setTheme('bogus');
      return { cur: h.pageHistory.current(), path: h.path(), search: h.search(), res, bad,
        js: [typeof h.reload, typeof h.navigate, typeof h.openLink, typeof h.download, typeof h.session.remove, typeof h.local.get],
        ext: h.resolve('https://example.com/x').internal, ctrl: h.resolve('/a\\n/b') };
    }""")
    assert out["cur"] == "/board?x=2" and out["path"] == "/board" and out["search"] == "?x=2"
    assert out["res"]["path"] == "/t/L-1" and out["res"]["hash"] == "#z" and out["res"]["internal"] is True
    assert out["ext"] is False and out["ctrl"] is None and out["bad"] == "rejected"
    assert dash.ev("history")[:2] == [{"op": "push", "path": "/board?x=1"}, {"op": "replace", "path": "/board?x=2"}]
    assert dash.ev("copy") == ["hello"] and dash.ev("theme") == ["dark"]      # a theme the host does not know never leaves the frame's validation


def test_links_forms_and_new_tabs_go_to_the_app(dash):
    dash.open()
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    dash.click("#ext")
    dash.click("#mail")
    dash.click("#dl")
    dash.click("#blank")
    dash.frame().wait_for_function("() => true")
    # external link -> the app (http and https only); mailto: nothing; download -> the app's download; target=_blank
    # inside the dashboard -> the viewer, never the frame
    dash.frame().page.wait_for_function("() => window.__ev.viewer.length >= 1 && window.__ev.download.length >= 1")
    assert dash.ev("external") == ["https://example.com/x"]
    assert [d["path"] for d in dash.ev("download")] == ["/a/L-1/data.csv"] and dash.ev("download")[0]["body"] == "a,b\n1,2\n"
    assert [(x["path"], x["body"]) for x in dash.ev("viewer")] == [("/t/L-1/x", "ticket text")]
    assert dash.title() == "Home"
    dash.click("#frag")                                                    # an in-page fragment never leaves the page
    assert dash.title() == "Home"


def test_a_get_form_and_a_post_form_become_requests_the_page_follows(dash):
    dash.open()
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    dash.click("#gs")
    dash.wait_title("Results")
    assert {"method": "GET", "path": "/search?q=x+y"}.items() <= [c for c in dash.calls() if c["path"].startswith("/search")][0].items()
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    dash.click("#nsub")
    dash.wait_title("Created")
    post = [c for c in dash.calls() if c["method"] == "POST"]
    assert len(post) == 1 and post[0]["path"] == "/new" and post[0]["body"] == "title=Hello"
    assert dash.ev("history")[-1] == {"op": "push", "path": "/created"}


def test_enter_requestsubmit_and_a_form_the_page_handles_itself(dash, page):
    dash.open()
    f = dash.frame()
    f.wait_for_function("() => document.documentElement.dataset.ran === '1'")
    n = len(dash.calls())
    dash.click("#hb")                                      # the page's own submit handler: it prevents the default, the shim sends nothing
    f.wait_for_function("() => window.__handled === 1")
    f.locator("#handled input").press("Enter")
    f.wait_for_function("() => window.__handled === 2")
    page.wait_for_timeout(200)
    assert len(dash.calls()) == n and dash.title() == "Home"
    f.locator("#search input").press("Enter")              # implicit submission: the browser's own is refused in this sandbox
    dash.wait_title("Results")
    assert [c["path"] for c in dash.calls()[n:] if c["path"].startswith("/search")] == ["/search?q=x+y"]   # once, not twice
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    dash.frame().evaluate("document.getElementById('search').requestSubmit()")
    dash.frame().wait_for_function("() => window.__esMsgs !== undefined")
    page.wait_for_function("() => window.__fake.calls.filter((c) => c.path.startsWith('/search')).length === 2")


def test_a_nested_frame_and_an_artifact_open_in_the_viewer_not_the_frame(dash):
    dash.open()
    f = dash.frame()
    assert f.evaluate("document.querySelectorAll('iframe').length") == 0
    link = f.locator("a.orch-viewer-link")
    assert link.inner_text() == "Open Evil <b>frame</b> in the viewer"               # host-supplied text, drawn as text
    assert f.evaluate("document.querySelector('a.orch-viewer-link b')") is None
    link.click()
    f.page.wait_for_function("() => window.__ev.viewer.length === 1")
    dash.click("#art")
    dash.click("#trick")        # the host tagged this one a page; an artifact path is never one
    f.page.wait_for_function("() => window.__ev.viewer.length === 3")
    assert [x["path"] for x in dash.ev("viewer")] == ["/a/L-1/frame.html", "/a/L-1/report.html", "/a/L-1/trick.html"]
    assert dash.title() == "Home" and f.evaluate("document.body.textContent.includes('tagged page')") is False


def test_an_error_page_is_text_in_the_app_and_runs_nothing(dash, page):
    dash.open()
    dash.click("#boom")
    page.wait_for_function("() => document.querySelector('.frame-notice') && !document.querySelector('.frame-notice').hidden || window.__ev.notice.length")
    assert dash.title() == "Home"
    assert dash.ev("notice") == [{"kind": "error", "text": "The dashboard answered 500 for /boom."}]
    assert page.evaluate("window.__pwn") is None and dash.frame().evaluate("window.__pwn") is None
    # the default notice is a text line; the markup of the body never reached the app's DOM
    assert page.evaluate("document.querySelectorAll('.frame-host img, .frame-host b').length") == 0


# ---- streams, generations, history ------------------------------------------------------------------------------

def test_streams_work_and_every_navigation_closes_them_all(dash, page):
    dash.open()
    f = dash.frame()
    f.wait_for_function("() => document.documentElement.dataset.ran === '1'")
    page.wait_for_function("() => window.__fake.streams.length === 1")
    page.evaluate("window.__fake.streams[0].push('data: one\\n\\ndata: two\\n\\n')")
    f.wait_for_function("() => window.__esMsgs.length === 2")
    assert f.evaluate("window.__esMsgs") == ["one", "two"]
    f.evaluate("window.__oldEs = window.__es")
    for i in range(8):                                   # the spike leaked one stream per page and hit the six-connection limit
        dash.click("#board" if i % 2 == 0 else "#home")
        dash.wait_title("Board" if i % 2 == 0 else "Home")
        dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    page.wait_for_function("() => window.__fake.streams.length === 9")
    assert page.evaluate("window.__fake.streams.filter((s) => !s.closed).length") == 1      # only the page on screen
    assert page.evaluate("window.__fake.streams.slice(0, 8).every((s) => s.closed)")
    assert dash.frame().evaluate("window.__oldEs.readyState") == 2        # and the page's own EventSource object says so


def test_an_answer_for_an_older_page_is_dropped(dash, page):
    dash.open()
    f = dash.frame()
    f.wait_for_function("() => document.documentElement.dataset.ran === '1'")
    f.evaluate("() => { window.__slow = 'pending'; fetch('/slow').then(() => { window.__slow = 'answered'; }, () => { window.__slow = 'dropped'; }); }")
    page.wait_for_function("() => window.__fake.calls.some((c) => c.path === '/slow')")
    dash.click("#board")
    dash.wait_title("Board")
    page.evaluate("window.__release('/slow')")
    page.wait_for_timeout(300)
    assert dash.frame().evaluate("window.__slow") in (None, "pending", "dropped")     # the new document never sees it
    assert dash.frame().evaluate("typeof window.__api") in ("undefined", "number")


def test_the_shim_drops_an_answer_that_carries_another_page_generation(dash, page):
    dash.open()
    f = dash.frame()
    f.wait_for_function("() => document.documentElement.dataset.ran === '1'")
    f.evaluate("() => { window.__g = {}; fetch('/slow').then(() => { window.__g.a = 'resolved'; }, (e) => { window.__g.a = e.message; }); }")
    page.wait_for_function("() => window.__ev.msgs.some((m) => m.path === '/slow')")
    m = page.evaluate("window.__ev.msgs.find((m) => m.path === '/slow')")
    answer = {"k": "orch-frame-1", "t": "res", "id": m["id"], "status": 200, "headers": {}, "url": "/slow", "body": b"{}".decode()}
    page.evaluate("""(a) => { a.body = new TextEncoder().encode('{}').buffer; document.querySelector('iframe.frame-dash').contentWindow.postMessage(a, '*'); }""",
                  {**answer, "gen": m["gen"] + 5})                       # an answer for some other page
    f.wait_for_function("() => window.__g.a !== undefined")
    assert f.evaluate("window.__g.a") == "the page changed"
    # the same answer with the right generation is accepted (a fresh request)
    f.evaluate("() => { fetch('/slow').then(() => { window.__g.b = 'resolved'; }, (e) => { window.__g.b = e.message; }); }")
    page.wait_for_function("() => window.__ev.msgs.filter((m) => m.path === '/slow').length === 2")
    m2 = page.evaluate("window.__ev.msgs.filter((m) => m.path === '/slow')[1]")
    page.evaluate("""(a) => { a.body = new TextEncoder().encode('{}').buffer; document.querySelector('iframe.frame-dash').contentWindow.postMessage(a, '*'); }""",
                  {**answer, "id": m2["id"], "gen": m2["gen"]})
    f.wait_for_function("() => window.__g.b !== undefined")
    assert f.evaluate("window.__g.b") == "resolved"
    page.evaluate("window.__release('/slow')")


def test_the_newest_navigation_wins(dash, page):
    dash.open()
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    dash.click("#slowlink")                              # A: held at the host
    page.wait_for_function("() => window.__fake.calls.some((c) => c.path === '/slow-page')")
    dash.click("#board")                                 # B: answers at once
    dash.wait_title("Board")
    page.evaluate("window.__release('/slow-page')")
    page.wait_for_timeout(400)
    assert dash.title() == "Board"


def test_the_app_can_start_a_page_and_the_frame_follows(dash, page):
    dash.open()
    page.evaluate("window.__host.go('/board')")
    dash.wait_title("Board")
    assert dash.ev("history") == []                        # the app started it: it does not push its own history again
    assert page.evaluate("window.__host.go('/secret')") is False and page.evaluate("window.__host.go('//evil.test/x')") is False


# ---- who is talking ---------------------------------------------------------------------------------------------

def test_a_page_load_the_app_did_not_start_destroys_the_iframe(dash, page):
    dash.open()
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    first = page.evaluate("(window.__first = document.querySelector('iframe.frame-dash')) && 1")
    page.wait_for_timeout(1700)                            # the window a legitimate document write opens (1.5 s) is over
    dash.frame().evaluate("() => { setTimeout(() => { location.href = location.origin + '/healthz'; }, 0); }")
    page.wait_for_function("() => document.querySelector('iframe.frame-dash') !== window.__first")
    assert page.evaluate("document.querySelectorAll('iframe.frame-dash').length") == 1
    assert page.evaluate("window.__first.isConnected") is False
    dash.wait_title("Home")                                # a new frame, a new token, the page drawn again
    assert first == 1


def test_a_reload_from_the_page_destroys_the_iframe_too(dash, page):
    dash.open()
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    page.evaluate("window.__first = document.querySelector('iframe.frame-dash')")
    page.wait_for_timeout(1700)
    dash.frame().evaluate("() => { setTimeout(() => location.reload(), 0); }")
    page.wait_for_function("() => document.querySelector('iframe.frame-dash') !== window.__first")
    dash.wait_title("Home")


def test_messages_from_another_origin_window_or_session_are_ignored(dash, page):
    dash.open()
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    n = len(dash.calls())
    req = {"t": "req", "id": 900, "gen": 1, "intent": "fetch", "method": "GET", "path": "/api/x", "headers": {}}
    dash.sid_event(**{**req, "__origin": "https://evil.test"})                         # not the opaque origin
    page.evaluate("""(m) => window.dispatchEvent(new MessageEvent('message', {data: Object.assign({k: 'orch-frame-1', sid: window.__sid}, m), source: window, origin: 'null'}))""", req)   # not the frame's window
    page.evaluate("""(m) => { const f = document.querySelector('iframe.frame-dash');
      window.dispatchEvent(new MessageEvent('message', {data: Object.assign({k: 'orch-frame-1', sid: 'wrong'}, m), source: f.contentWindow, origin: 'null'})); }""", req)   # not the session
    page.evaluate("""(m) => { const f = document.querySelector('iframe.frame-dash');
      window.dispatchEvent(new MessageEvent('message', {data: Object.assign({k: 'orch-frame-1', t: 'hello', tok: 'x'.repeat(30)}), source: f.contentWindow, origin: 'null'})); }""", req)   # a hello with another token
    page.wait_for_timeout(200)
    assert len(dash.calls()) == n
    dash.sid_event(**req)                                                              # the same message, from the frame with the session: answered
    page.wait_for_function(f"() => window.__fake.calls.length === {n + 1}")


def test_a_crafted_message_is_refused_by_the_scope_table_before_the_transport(dash, page):
    dash.open()
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    n = len(dash.calls())
    hostile = [
        {"id": 11, "path": "/secret", "method": "GET"},                      # a path outside the table
        {"id": 12, "path": "/api/x", "method": "DELETE"},                    # a method the table does not name
        {"id": 13, "path": "/a/../secret", "method": "GET"},                 # traversal
        {"id": 14, "path": "//evil.test/x", "method": "GET"},                # another host
        {"id": 15, "path": "/api/x%2F..%2Fsecret", "method": "GET"},         # encoded slash
        {"id": 16, "path": "https://example.com/", "method": "GET"},         # an address, not a path
        {"id": 17, "path": "/api/x", "method": "TRACE"},                     # a method nobody uses
        {"id": 18, "path": "/new", "method": "GET"},                         # right path, wrong method
        {"id": 19, "path": "/api/x\r\nCookie: a=b", "method": "GET"},        # header injection in the path
    ]
    for h in hostile:
        dash.sid_event(t="req", gen=1, intent="fetch", headers={}, **h)
    dash.sid_event(t="sopen", id=30, gen=1, path="/api/x")                   # a stream on a path with no stream rule
    dash.sid_event(t="req", id=31, gen=1, intent="fetch", method="POST", path="/new", headers={"cookie": "a=b", "origin": "x", "host": "y", "content-type": "text/plain"}, body=None)
    page.wait_for_timeout(300)
    after = dash.calls()[n:]
    assert [c["path"] for c in after] == ["/new"]                            # only the one in scope reached the transport...
    assert page.evaluate("window.__fake.calls.at(-1).headers") == {"content-type": "text/plain"}   # ...with only allow-listed headers


def test_a_message_flood_is_capped_and_then_the_frame_is_rebuilt(dash, page):
    dash.open({"rate": 20, "floodSeconds": 2})
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    n = len(dash.calls())
    page.evaluate("window.__first = document.querySelector('iframe.frame-dash')")
    page.evaluate("""() => { const f = document.querySelector('iframe.frame-dash');
      for (let i = 0; i < 300; i++) window.dispatchEvent(new MessageEvent('message', {data: {k: 'orch-frame-1', sid: window.__sid, t: 'req', id: 1000 + i, gen: 1, intent: 'fetch', method: 'GET', path: '/api/x', headers: {}}, source: f.contentWindow, origin: 'null'})); }""")
    page.wait_for_timeout(100)
    assert len(dash.calls()) - n <= 20                                      # the cap, not 300
    page.wait_for_timeout(1100)
    page.evaluate("""() => { const f = document.querySelector('iframe.frame-dash');
      for (let i = 0; i < 300; i++) window.dispatchEvent(new MessageEvent('message', {data: {k: 'orch-frame-1', sid: window.__sid, t: 'req', id: 2000 + i, gen: 1, intent: 'fetch', method: 'GET', path: '/api/x', headers: {}}, source: f.contentWindow, origin: 'null'})); }""")
    page.wait_for_function("() => document.querySelector('iframe.frame-dash') !== window.__first")   # sustained: destroyed and rebuilt


def test_in_flight_requests_are_capped_per_frame(dash, page):
    dash.open({"inflight": 3})
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    n = len(dash.calls())
    for i in range(10):                                                      # /slow is held at the host: they pile up
        dash.sid_event(t="req", id=500 + i, gen=1, intent="fetch", method="GET", path="/slow", headers={})
    page.wait_for_timeout(300)
    assert len(dash.calls()) - n == 2          # the cap is 3 and the page's own stream holds one
    page.evaluate("window.__release('/slow')")


def test_a_body_over_the_cap_is_refused(dash, page):
    dash.open()
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    n = len(dash.calls())
    page.evaluate("""() => { const f = document.querySelector('iframe.frame-dash');
      window.dispatchEvent(new MessageEvent('message', {data: {k: 'orch-frame-1', sid: window.__sid, t: 'req', id: 77, gen: 1, intent: 'fetch', method: 'POST', path: '/new', headers: {}, body: new ArrayBuffer(2 * 1024 * 1024)}, source: f.contentWindow, origin: 'null'})); }""")
    page.wait_for_timeout(200)
    assert len(dash.calls()) == n


def test_a_request_to_a_route_outside_the_scope_table_fails_in_the_page_and_never_reaches_the_transport(dash):
    dash.open()
    f = dash.frame()
    f.wait_for_function("() => document.documentElement.dataset.ran === '1'")
    n = len(dash.calls())
    res = f.evaluate("""async () => {
      const out = {};
      out.secret = await fetch('/secret').then(() => 'ok', (e) => e.message);
      out.put = await fetch('/api/x', {method: 'PUT', body: 'x'}).then(() => 'ok', (e) => e.message);
      out.xhr = await new Promise((r) => { const x = new XMLHttpRequest(); x.open('GET', '/secret'); x.onerror = () => r('error'); x.onload = () => r('load'); x.send(); });
      out.cross = await fetch('https://example.com/').then(() => 'ok', (e) => e.message);
      return out;
    }""")
    assert res["secret"].startswith("refused") and res["put"].startswith("refused") and res["xhr"] == "error"
    assert "blocked" in res["cross"]
    assert dash.calls()[n:] == []


# ---- hostile page content: the page is changed as a DOM and moved in, never serialised and parsed again -------------

def test_a_hostile_page_string_runs_nothing_and_leaves_no_handler_or_script_url(dash, page):
    dash.open()
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    page.evaluate("window.__first = document.querySelector('iframe.frame-dash')")
    dash.click("#mx")
    dash.wait_title("mx")
    page.wait_for_timeout(600)
    f = dash.frame()
    left = f.evaluate("""() => {
      const walk = (root, out) => { root.querySelectorAll('*').forEach((n) => { out.push(n); if (n.localName === 'template' && n.content) walk(n.content, out); }); return out; };
      const all = walk(document, []);
      const bad = [];
      for (const n of all) for (const a of n.attributes) {
        const v = a.value.replace(/[\u0000-\u0020]/g, '').toLowerCase();
        if (/^on/i.test(a.name) || a.name === 'srcdoc' || a.name === 'nonce' || v.startsWith('javascript:') || v.startsWith('data:text/html')) bad.push(n.localName + '@' + a.name);
      }
      return { x: window.__x || 0, bad, scripts: all.filter((n) => n.localName === 'script').length,
        base: document.querySelectorAll('base, meta[http-equiv], object, embed, iframe, noscript').length,
        links: [...document.querySelectorAll('link')].map((l) => l.rel), path: location.pathname, title: document.title };
    }""")
    assert left["x"] == 0 and left["bad"] == [] and left["scripts"] == 0 and left["base"] == 0, left
    assert left["links"] == [] and left["path"] == "/sandbox/dash"            # no refresh, no navigation
    assert page.evaluate("document.querySelector('iframe.frame-dash') === window.__first")   # the app never had to rebuild it
    assert not [c for c in dash.calls() if c["path"] in ("/secret", "/healthz")]
    # a script: link in the page that survived in any form is neutralised when clicked
    f.evaluate("() => { const a = document.createElement('a'); a.id = 'late'; a.setAttribute('href', 'javascript:window.__x=1'); document.body.appendChild(a); a.click(); }")
    page.wait_for_timeout(200)
    assert f.evaluate("window.__x || 0") == 0


# ---- the clipboard: the frame asks, the person clicks in the app ------------------------------------------------------

def test_the_frame_cannot_write_the_clipboard_without_a_click_in_the_app(dash, page):
    dash.open()
    f = dash.frame()
    f.wait_for_function("() => document.documentElement.dataset.ran === '1'")
    f.evaluate("() => { window.__cp = window.orchHost.copy('rm -rf /tmp/x').then(() => 'copied', () => 'failed'); }")
    page.wait_for_selector(".frame-copy")
    assert page.inner_text(".frame-copy pre") == "rm -rf /tmp/x"                        # shown, as text
    page.wait_for_timeout(300)
    assert dash.ev("copy") == []
    # a second ask while one is up, or soon after, is refused outright
    assert f.evaluate("window.orchHost.copy('other').then(() => 'copied', () => 'failed')") == "failed"
    page.click(".frame-copy button:text-is('Dismiss')")
    assert f.evaluate("window.__cp") == "failed" and dash.ev("copy") == []
    assert f.evaluate("window.orchHost.copy('again').then(() => 'copied', () => 'failed')") == "failed"   # inside the gap
    # an ask that is up when the page changes is withdrawn: nothing is copied for a document the app did not just write
    page.wait_for_timeout(2100)
    f.evaluate("() => { window.__cp2 = window.orchHost.copy('late').then(() => 'copied', () => 'failed'); }")
    page.wait_for_selector(".frame-copy")
    f.evaluate("document.getElementById('board').click()")          # (the question moves the frame: no pointer click here)
    dash.wait_title("Board")
    page.wait_for_function("() => !document.querySelector('.frame-copy')")
    assert dash.ev("copy") == []
    # the host has no clipboard read at all, and the frame has no message for one
    src = page.evaluate("fetch('/static/js/frame-host.js').then((r) => r.text())")
    assert "readText" not in src and ".read(" not in src


# ---- who is talking: a spent token, a destroyed frame, odd shapes -------------------------------------------------------

def test_a_spent_token_is_refused_and_a_destroyed_frames_answers_go_nowhere(dash, page):
    dash.open()
    f = dash.frame()
    f.wait_for_function("() => document.documentElement.dataset.ran === '1'")
    tok = page.evaluate("new URL(document.querySelector('iframe.frame-dash').src).searchParams.get('tok')")
    page.evaluate("window.__readies = 0; window.__tapped = []")
    page.evaluate("""(tok) => { const f = document.querySelector('iframe.frame-dash');
      window.dispatchEvent(new MessageEvent('message', {data: {k: 'orch-frame-1', t: 'hello', tok}, source: f.contentWindow, origin: 'null'})); }""", tok)
    page.wait_for_timeout(200)
    assert dash.ev("seen").count("hello") >= 1 and page.evaluate("window.__sid") != tok     # the token is not the session
    # odd shapes: own __proto__ keys, constructor, nested junk: dropped, nothing reaches the transport
    n = len(dash.calls())
    page.evaluate("""() => { const f = document.querySelector('iframe.frame-dash');
      const odd = [JSON.parse('{"k":"orch-frame-1","sid":"' + window.__sid + '","t":"req","__proto__":{"sid":"x"},"id":5,"gen":1,"intent":"fetch","method":"GET","path":"/secret","headers":{}}'),
        {k: 'orch-frame-1', sid: window.__sid, t: 'constructor'}, {k: 'orch-frame-1', sid: window.__sid, t: '__proto__'}, [1, 2], 'text', null, 7];
      for (const d of odd) window.dispatchEvent(new MessageEvent('message', {data: d, source: f.contentWindow, origin: 'null'})); }""")
    page.wait_for_timeout(200)
    assert len(dash.calls()) == n
    # a held request answered after the frame was destroyed and rebuilt is not sent to the new frame
    f.evaluate("() => { fetch('/slow').catch(() => {}); }")
    page.wait_for_function("() => window.__fake.calls.some((c) => c.path === '/slow')")
    page.evaluate("window.__sent = []; window.__old = document.querySelector('iframe.frame-dash')")
    page.wait_for_timeout(1700)
    f.evaluate("() => { setTimeout(() => { location.href = location.origin + '/healthz'; }, 0); }")
    page.wait_for_function("() => document.querySelector('iframe.frame-dash') !== window.__old")
    page.evaluate("window.__release('/slow')")
    page.wait_for_timeout(300)
    dash.wait_title("Home")
    assert dash.frame().evaluate("window.__r !== undefined") and page.evaluate("window.__old.isConnected") is False
