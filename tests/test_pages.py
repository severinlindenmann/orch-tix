import html.parser
import json
import re
import struct

import pytest

from fileshare import db
from fileshare.routes import pages

HTML_FILES = sorted(pages.STATIC_DIR.glob("*.html"))


@pytest.fixture(autouse=True)
def _fresh_stamp():
    pages.build_stamp.cache_clear()
    yield
    pages.build_stamp.cache_clear()


def _mark_initialized(settings):
    conn = db.connect(settings.db_path)
    try:
        db.migrate(conn)
        db.set_meta(conn, "auth_hash", "scrypt.x.y")
    finally:
        conn.close()


def test_root_redirects_to_setup_when_not_initialized(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/setup"


def test_login_redirects_to_setup_when_not_initialized(client):
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/setup"


def test_setup_page_is_stamped_and_not_cached(client, monkeypatch):
    monkeypatch.setenv("FS_BUILD", "abc123")
    r = client.get("/setup")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "/static/js/setup.js?v=abc123" in r.text
    assert "/static/css/app.css?v=abc123" in r.text
    assert "{{BUILD}}" not in r.text
    assert r.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_pages_say_their_build_for_the_service_worker(client, settings, monkeypatch):
    """X-Build: the worker caches a page only for its own build (sw.js cacheablePage)."""
    monkeypatch.setenv("FS_BUILD", "b77")
    _mark_initialized(settings)
    for path in ("/setup", "/login", "/files", "/pair", "/sandbox/html"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["x-build"] == "b77", path


def test_setup_code_hint_points_at_the_runbook_command():
    text = (pages.STATIC_DIR / "setup.html").read_text()
    hint = re.search(r'<p class="hint">Print one on the server.*?</p>', text, re.S).group(0)
    assert "sudo -u fileshare" in hint and "RUNBOOK" in hint
    assert "<code>uv run python -m fileshare.admin setup-code</code>" not in text


def test_login_served_once_initialized(client, settings):
    _mark_initialized(settings)
    r = client.get("/login")
    assert r.status_code == 200
    assert 'id="login-form"' in r.text


def test_hostile_build_env_is_ignored(monkeypatch):
    monkeypatch.setenv("FS_BUILD", 'x" onload="alert(1)')
    stamp = pages.build_stamp()
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,40}", stamp)
    assert "onload" not in stamp


def test_static_js_revalidates(client):
    r = client.get("/static/js/api.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-cache"


class _InlineAudit(html.parser.HTMLParser):
    def __init__(self, stamp_suffix="?v={{BUILD}}"):
        super().__init__()
        self.stamp_suffix = stamp_suffix
        self.problems = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "style":
            self.problems.append("<style> block")
        if "style" in a:
            self.problems.append(f"style= on <{tag}>")
        self.problems += [f"{k}= on <{tag}>" for k in a if k.startswith("on")]
        if tag == "script":
            if "src" not in a:
                self.problems.append("inline <script>")
            elif not a["src"].endswith(self.stamp_suffix):
                self.problems.append(f"unstamped script {a['src']}")
            # Classic scripts only for the vendored libraries (Task 19), which expose globals, and
            # sandbox-frame.js (spec T15): from the sandbox's opaque origin, a `type="module"` fetch
            # is cross-origin and gets blocked, so that one script must stay classic.
            src = a.get("src", "")
            classic_ok = src.startswith("/static/vendor/") or src.startswith("/static/js/sandbox-frame.js")
            if a.get("type") != "module" and not classic_ok:
                self.problems.append("classic <script> (must be type=module)")
        if tag == "link" and a.get("rel") == "stylesheet" and not a.get("href", "").endswith(self.stamp_suffix):
            self.problems.append(f"unstamped stylesheet {a.get('href')}")


@pytest.mark.parametrize("page", HTML_FILES, ids=lambda p: p.name)
def test_html_has_no_inline_script_or_style(page):
    audit = _InlineAudit()
    audit.feed(page.read_text(encoding="utf-8"))
    assert audit.problems == []


@pytest.mark.parametrize("route", ["/setup", "/login", "/files", "/p/" + "A" * 43, "/u/" + "A" * 43, "/pair"])
def test_rendered_html_has_no_inline_script_or_style(client, settings, monkeypatch, route):
    # Audits what the browser actually receives, after placeholder substitution.
    monkeypatch.setenv("FS_BUILD", "abc123")
    _mark_initialized(settings)
    r = client.get(route, follow_redirects=False)
    assert r.status_code == 200
    audit = _InlineAudit(stamp_suffix="?v=abc123")
    audit.feed(r.text)
    assert audit.problems == []
    assert "{{" not in r.text


def test_files_served_once_initialized(client, settings):
    _mark_initialized(settings)
    r = client.get("/files")
    assert r.status_code == 200
    for hook in ('id="file-list"', 'id="detail"', 'id="dock"', 'id="outbox-slot"'):
        assert hook in r.text, hook
    # The Note button (spec §16) is live, and the Offline state has its fixed text.
    note = re.search(r'<button[^>]*id="note-btn"[^>]*>', r.text).group(0)
    assert "hidden" not in note and 'data-action="note"' in note
    assert ("You're offline. New notes, recordings and uploads will be sent when you're back online."
            in r.text)


def test_settings_carries_the_devices_card(owner):
    r = owner.client.get("/settings")
    assert r.status_code == 200
    assert "/static/js/devices.js?v=" in r.text and "{{BUILD}}" not in r.text
    for hook in ('id="devices-list"', 'id="devices"', 'id="join-list"', 'id="join"', 'id="pair-card"',
                 'id="titles-toggle"', 'id="archive-card"', 'id="push-card"', 'id="transcription-card"'):
        assert hook in r.text, hook
    titles = re.search(r'<input[^>]*id="titles-toggle"[^>]*>', r.text).group(0)
    assert "checked" not in titles       # off by default (spec §7)


ICON_LINKS = (
    '<link rel="icon" href="/static/img/icon.svg?v={{BUILD}}" type="image/svg+xml">',
    '<link rel="icon" href="/static/img/icon-32.png?v={{BUILD}}" sizes="32x32" type="image/png">',
    '<link rel="apple-touch-icon" href="/static/img/apple-touch-icon.png?v={{BUILD}}">',
)


# The sandbox frame (spec T15) is never navigated to directly or bookmarked: it's loaded invisibly
# inside a sandboxed iframe to render an attachment, so it carries no icons, manifest or PWA tags.
ICON_HTML_FILES = [p for p in HTML_FILES if p.name != "sandbox.html"]


@pytest.mark.parametrize("page", ICON_HTML_FILES, ids=lambda p: p.name)
def test_every_page_links_the_app_icon(page):
    text = page.read_text()
    for link in ICON_LINKS:
        assert link in text, f"{page.name} is missing {link}"


@pytest.mark.parametrize("path,ctype", [
    ("/static/img/icon.svg", "image/svg+xml"),
    ("/static/img/icon-32.png", "image/png"),
    ("/static/img/apple-touch-icon.png", "image/png"),
])
def test_icon_files_are_served_as_images(client, path, ctype):
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(ctype)
    assert len(r.content) > 100


# ---- PWA (Task 24) ----
MANIFEST = {
    "name": "tix share",
    "short_name": "tix",
    "description": "End-to-end encrypted file share between your devices",
    "id": "/",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait-primary",
    "theme_color": "#15171a",
    "background_color": "#15171a",
    "icons": [
        {"src": "/static/img/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/img/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/static/img/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        {"src": "/static/img/icon.svg", "sizes": "any", "type": "image/svg+xml"},
    ],
}

PWA_HEAD = (
    '<link rel="manifest" href="/manifest.webmanifest">',
    '<meta name="apple-mobile-web-app-capable" content="yes">',
    '<meta name="mobile-web-app-capable" content="yes">',
    '<meta name="apple-mobile-web-app-status-bar-style" content="default">',
    '<meta name="apple-mobile-web-app-title" content="tix">',
    '<meta name="theme-color" content="#15171a">',
)


def test_manifest_is_served_before_setup_without_redirect(client):
    r = client.get("/manifest.webmanifest", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].split(";")[0] == "application/manifest+json"
    assert r.headers["cache-control"] == "no-cache"
    assert r.json() == MANIFEST
    assert "prefer_related_applications" not in r.json()


def test_manifest_is_served_once_initialized(client, settings):
    _mark_initialized(settings)
    r = client.get("/manifest.webmanifest", follow_redirects=False)
    assert r.status_code == 200 and r.json() == MANIFEST


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    length, ctype = struct.unpack(">I4s", data[8:16])
    assert ctype == b"IHDR" and length == 13
    return struct.unpack(">II", data[16:24])


@pytest.mark.parametrize("icon", MANIFEST["icons"], ids=lambda i: i["src"])
def test_every_manifest_icon_is_served_at_its_declared_size(client, icon):
    r = client.get(icon["src"])
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(icon["type"])
    if icon["type"] == "image/png":
        w, h = _png_size(r.content)
        assert f"{w}x{h}" == icon["sizes"]
    else:
        assert icon["sizes"] == "any" and b"<svg" in r.content


def test_manifest_file_is_the_served_body(client):
    raw = (pages.STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8")
    assert json.loads(raw) == MANIFEST


# The public-link viewer (§17) and the upload-link drop page (upload-links spec) are not the app:
# someone without an account can't install them. The sandbox frame (spec T15) is likewise never
# installed (see ICON_HTML_FILES above).
APP_HTML_FILES = [p for p in HTML_FILES if p.name not in ("public.html", "drop.html", "sandbox.html")]


@pytest.mark.parametrize("page", APP_HTML_FILES, ids=lambda p: p.name)
def test_every_page_is_installable(page):
    text = page.read_text()
    for tag in PWA_HEAD:
        assert tag in text, f"{page.name} is missing {tag}"


# ---- The offline PWA (spec §16) ----
# Only these modules may touch navigator.serviceWorker: swreg.js registers /sw.js?v=<BUILD>, and
# outbox-ui.js asks for Background Sync and listens for the worker's flush message.
# pushsettings.js (spec T7, T10) only waits for serviceWorker.ready to reach its pushManager.
SW_MODULES = {"swreg.js", "outbox-ui.js", "pushsettings.js"}


def test_only_swreg_and_the_outbox_touch_the_service_worker():
    users = {f.name for f in pages.STATIC_DIR.rglob("*")
             if f.is_file() and f.suffix in {".js", ".html"} and "vendor" not in f.parts
             and f.name != "sw.js" and "serviceWorker" in f.read_text(encoding="utf-8")}
    assert users == SW_MODULES
    reg = (pages.STATIC_DIR / "js" / "swreg.js").read_text(encoding="utf-8")
    assert ".register(`/sw.js?v=${encodeURIComponent(buildStamp(doc))}`, { scope: \"/\" })" in reg


@pytest.mark.parametrize("module", ["shell.js", "login.js"])
def test_signed_in_pages_and_login_register_the_worker(module):
    text = (pages.STATIC_DIR / "js" / module).read_text(encoding="utf-8")
    assert re.search(r'^import (\{ warmShell \} from )?"\./swreg\.js";$', text, re.M)


@pytest.mark.parametrize("route", ["/", "/t", "/t/42", "/files", "/settings", "/login", "/pair"])
def test_shell_pages_revalidate_instead_of_no_store(owner, route):
    """The worker never caches a no-store response, so the shell pages it precaches are no-cache."""
    r = owner.client.get(route, follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"


def test_setup_and_public_pages_stay_no_store(client):
    assert client.get("/setup").headers["cache-control"] == "no-store"
    assert client.get("/p/" + "A" * 43).headers["cache-control"] == "no-store"


def test_precache_list_is_served(client):
    r = client.get("/static/precache.json")
    assert r.status_code == 200
    body = r.json()
    assert body["pages"] == ["/", "/t", "/files", "/settings", "/login", "/sandbox/html", "/pair"]
    for path in body["assets"]:
        if path.startswith("/static/"):
            assert (pages.STATIC_DIR / path.removeprefix("/static/")).is_file(), path


def test_service_worker_reads_the_build_and_bypasses_the_api():
    sw = (pages.STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    assert 'new URL(self.location.href).searchParams.get("v")' in sw
    assert "const CACHE = `shell-${BUILD}`;" in sw
    assert r"const BYPASS = /^\/(?:api|p|u|skill)\/|^\/onboarding|^\/sw\.js$/;" in sw


def test_bottom_anchored_ui_clears_the_home_bar():
    """Standalone on a notched phone: nothing fixed to the bottom may sit under the home indicator."""
    css = (pages.STATIC_DIR / "css" / "app.css").read_text(encoding="utf-8")
    for selector in (".toasts { position: fixed", "  .sidebar { position: fixed",
                     "  .dock { position: fixed", "  .detail-actions { padding",
                     "  dialog.sheet, dialog.modal, dialog.recorder, dialog.onboard { width: 100vw",
                     ".auth-wrap {"):
        start = css.index(selector)
        rule = css[start:css.index("}", start)]
        assert "var(--safe-b)" in rule or "env(safe-area-inset-bottom, 0px)" in rule, selector
    assert "--safe-b: env(safe-area-inset-bottom, 0px);" in css


def _css_rule(css: str, selector: str) -> str:
    start = css.index(selector + " {")
    return css[start:css.index("}", start)]


def test_attachment_viewers_clear_the_side_notch_and_images_have_no_callout():
    """Spec T15 (final review M5): a landscape phone's notch never covers the viewer bar, image or
    frame; a long press on an image doesn't open iOS's callout (the viewer has Download)."""
    css = (pages.STATIC_DIR / "css" / "app.css").read_text(encoding="utf-8")
    for sel in (".att-viewer-bar", ".att-viewer-stage", ".att-frame-wrap"):
        rule = _css_rule(css, sel)
        assert "env(safe-area-inset-left" in rule and "env(safe-area-inset-right" in rule, sel
    for sel in (".att-img", ".att-viewer-img"):
        assert "-webkit-touch-callout: none" in _css_rule(css, sel), sel


# ---- the four tabs (TIX spec §10): Needs you · Tickets · Files · Settings ----
NAV_PAGES = {"index.html", "ticket.html", "files.html", "settings.html"}
TABS = [("/", "Needs you"), ("/?view=tickets", "Tickets"), ("/files", "Files"), ("/settings", "Settings")]


def test_every_page_with_a_nav_has_the_four_tabs():
    with_nav = {p.name for p in HTML_FILES if "<nav" in p.read_text(encoding="utf-8")}
    assert with_nav == NAV_PAGES
    for name in NAV_PAGES:
        text = (pages.STATIC_DIR / name).read_text(encoding="utf-8")
        nav = text[text.index("<nav"):text.index("</nav>")]
        links = re.findall(r'<a class="nav-item" href="([^"]+)"[^>]*>.*?<span class="nav-label">([^<]+)</span>', nav)
        assert links == TABS, name
        assert '<span id="needs-badge" class="count-badge" hidden></span>' in nav, name
        assert "/tickets" not in text.replace("/?view=tickets", "") and 'href="/devices"' not in text, name
        assert 'class="brand" href="/"' in text, name       # the brand is the way back to Needs you
    current = {n: re.search(r'href="([^"]+)" data-tab="\w+" aria-current="page"', (pages.STATIC_DIR / n).read_text()).group(1)
               for n in NAV_PAGES}
    assert current == {"index.html": "/", "ticket.html": "/", "files.html": "/files", "settings.html": "/settings"}


def test_no_page_asks_google_for_fonts():
    for page in HTML_FILES:
        text = page.read_text(encoding="utf-8")
        assert "fonts.googleapis" not in text and "fonts.gstatic" not in text, page.name


def test_settings_redirects_to_setup_when_not_initialized(client):
    r = client.get("/settings", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/setup"


def test_settings_without_session_redirects_to_login(client, settings):
    _mark_initialized(settings)
    r = client.get("/settings", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/login?next=/settings"


def test_settings_page_served_with_session(owner, monkeypatch):
    monkeypatch.setenv("FS_BUILD", "abc123")
    r = owner.client.get("/settings", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"   # precached by the service worker (§16)
    assert "default-src 'self'" in r.headers["content-security-policy"]
    audit = _InlineAudit(stamp_suffix="?v=abc123")
    audit.feed(r.text)
    assert audit.problems == []
    assert "{{" not in r.text
    assert "/static/js/settings.js?v=abc123" in r.text
    key = re.search(r"<input[^>]*id=\"deepgram-key\"[^>]*>", r.text).group(0)
    assert 'type="password"' in key and 'autocomplete="off"' in key
    assert ("Stored end-to-end encrypted. Every approved device can read it; "
            "after revoking a device, rotate the key at Deepgram.") in r.text


# ---- Public links (spec §17) and the service worker route (§16) ----
@pytest.mark.parametrize("route", ["/setup", "/login", "/", "/files", "/t/1", "/devices", "/tickets", "/manifest.webmanifest",
                                   "/p/" + "A" * 43, "/p/bad", "/sw.js", "/api/public/" + "A" * 43,
                                   "/u/" + "A" * 43, "/api/public/u/" + "A" * 43, "/pair"])
def test_every_page_and_public_route_has_the_exact_csp(client, settings, route):
    from fileshare.headers import csp
    _mark_initialized(settings)
    r = client.get(route, follow_redirects=False)
    assert r.headers["content-security-policy"] == csp()
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_public_page_has_no_nav_and_no_login_link():
    text = (pages.STATIC_DIR / "public.html").read_text(encoding="utf-8")
    assert "/login" not in text and "<nav" not in text
    assert '<script type="module" src="/static/js/public.js?v={{BUILD}}"></script>' in text


def test_public_page_head_is_the_viewer_not_the_app():
    """No manifest or install meta, no service worker; the same referrer rule, stylesheet and the
    Markdown renderer's vendored libraries and preview CSS template."""
    text = (pages.STATIC_DIR / "public.html").read_text(encoding="utf-8")
    assert 'rel="manifest"' not in text and "apple-mobile-web-app" not in text and "mobile-web-app-capable" not in text
    assert '<meta name="referrer" content="no-referrer">' in text
    assert '<link rel="stylesheet" href="/static/css/app.css?v={{BUILD}}">' in text
    assert '<template id="preview-css">{{PREVIEW_CSS}}</template>' in text
    for lib in ("markdown-it.min.js", "purify.min.js"):
        assert f'<script src="/static/vendor/{lib}?v={{{{BUILD}}}}"></script>' in text
    assert "Shared end-to-end encrypted via tix" in text
    js = (pages.STATIC_DIR / "js" / "public.js").read_text(encoding="utf-8")
    assert '"./swreg.js"' not in js and "navigator.serviceWorker" not in js and '"./keystore.js"' not in js
    assert '"./api.js"' not in js and '"./preview.js"' not in js and '"./render.js"' in js


def test_public_page_is_rendered_with_the_preview_css(client):
    from fileshare.headers import PREVIEW_CSS
    r = client.get("/p/" + "A" * 43)
    assert r.status_code == 200
    assert f'<template id="preview-css">{PREVIEW_CSS}</template>' in r.text


# ---- TIX on orch-core: Needs you and /t/<n> are gated like /settings; the old board redirects
@pytest.mark.parametrize("route, nxt", [("/", "/"), ("/t/42", "/t/42"), ("/t", "/")])
def test_needs_and_ticket_without_session_redirect_to_login(client, settings, route, nxt):
    _mark_initialized(settings)
    r = client.get(route, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == f"/login?next={nxt}"


def test_needs_and_ticket_redirect_to_setup_when_not_initialized(client):
    for route in ("/", "/t/1", "/files", "/settings"):
        r = client.get(route, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/setup"


@pytest.mark.parametrize("route", ["/t/x", "/t/1234567890123", "/t/-1"])
def test_a_ticket_route_takes_only_a_number(owner, route):
    assert owner.client.get(route, follow_redirects=False).status_code == 404


@pytest.mark.parametrize("route, location", [
    ("/tickets", "/"), ("/tickets/TIX-42", "/t/42"), ("/tickets/42", "/t/42"), ("/tickets/tix-7", "/t/7"),
    ("/tickets/%3Cx%3E", "/"), ("/tickets/TIX-0", "/"), ("/devices", "/settings#devices")])
def test_the_old_board_and_devices_routes_redirect(client, settings, route, location):
    _mark_initialized(settings)
    r = client.get(route, follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == location


@pytest.mark.parametrize("route, hooks", [
    ("/", ("/static/js/needs.js?v=abc123", 'id="needs-list"', 'id="needs-count"', 'class="app page-needs phone"')),
    ("/t/42", ("/static/js/ticket.js?v=abc123", 'id="ticket"', 'id="decision-bar"', 'class="app page-ticket phone"')),
])
def test_needs_and_ticket_pages_served_with_session(owner, monkeypatch, route, hooks):
    monkeypatch.setenv("FS_BUILD", "abc123")
    r = owner.client.get(route, follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"   # precached by the service worker (§16)
    assert r.headers["content-security-policy"] == __import__("fileshare.headers", fromlist=["csp"]).csp()
    audit = _InlineAudit(stamp_suffix="?v=abc123")
    audit.feed(r.text)
    assert audit.problems == []
    assert "{{" not in r.text
    for hook in hooks:
        assert hook in r.text, hook


def test_the_app_icon_is_the_signal_tile():
    """TIX spec §10: a dark rounded square, mint arcs, a pink blip and a white ticket glyph."""
    svg = (pages.STATIC_DIR / "img" / "icon.svg").read_text(encoding="utf-8")
    assert 'fill="#15171a"' in svg and svg.count('stroke="#00CC99"') == 3
    assert 'fill="#F84A9B"' in svg and 'fill="#F2F3F5"' in svg


@pytest.mark.parametrize("query", ["f=FILE7", "f=FILE7&tag=ticket", "tag=notes"])
def test_old_file_links_on_root_open_the_files_page(client, settings, query):
    _mark_initialized(settings)
    r = client.get("/?" + query, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/files?" + query


# ---- Pairing with a desktop (Task 10, orch-core remote humans) ----
def test_pair_page_is_served_without_session_or_setup_as_a_precachable_shell(client, settings):
    """The desktop's QR code opens /pair#<space>.<phone>.<key>: no session gate (iOS opens it in Safari,
    which is not signed in). The page holds no data, so it is no-cache and the service worker precaches it
    as its own shell (review fix round 1). The key is in the fragment, which never reaches the server."""
    r = client.get("/pair", follow_redirects=False)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    _mark_initialized(settings)
    r = client.get("/pair", follow_redirects=False)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    assert "/static/js/pair.js?v=" in r.text and 'class="page-pair"' in r.text
    assert "/login" not in r.text and "<nav" not in r.text


def test_settings_carries_the_pair_card():
    text = (pages.STATIC_DIR / "settings.html").read_text(encoding="utf-8")
    assert '<label class="label" for="pair-link">Pairing link</label>' in text
    assert 'id="pair-form"' in text and 'id="pair-list"' in text and "It comes in the next update" not in text


def test_the_archive_card_says_when_it_is_empty():
    text = (pages.STATIC_DIR / "settings.html").read_text(encoding="utf-8")
    assert '<div id="archive-list" class="archive-list"><p class="muted">No archived tickets.</p></div>' in text
