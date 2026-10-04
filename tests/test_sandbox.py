"""/sandbox/html: the isolated frame for ticket HTML attachments (spec T15)."""
import html.parser

from fileshare import db
from fileshare.headers import SANDBOX_CSP, csp
from fileshare.routes import pages

# Copied verbatim from spec T15 / the plan's "Global Constraints", not imported from
# fileshare.headers.SANDBOX_CSP: a typo in the constant that happened to match itself would pass a
# self-comparison, so this test compares against an independent literal instead.
SPEC_SANDBOX_CSP = (
    "sandbox allow-scripts; default-src 'none'; "
    "script-src 'unsafe-inline' 'unsafe-eval' 'self' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
    "style-src 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com data:; "
    "img-src data: blob:; media-src data: blob:; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'"
)


def _mark_initialized(settings):
    conn = db.connect(settings.db_path)
    try:
        db.migrate(conn)
        db.set_meta(conn, "auth_hash", "scrypt.x.y")
    finally:
        conn.close()


def test_sandbox_html_has_exact_csp(client):
    r = client.get("/sandbox/html")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == SPEC_SANDBOX_CSP
    assert SANDBOX_CSP == SPEC_SANDBOX_CSP  # keeps the constant honest, without relying on it above
    assert SPEC_SANDBOX_CSP != csp()
    # Exactly one CSP header: a duplicate `headers[...] =` call, or an extra middleware also setting
    # it, would show up here as two values rather than the assertion above simply picking one.
    assert r.headers.get_list("content-security-policy") == [SPEC_SANDBOX_CSP]


def test_sandbox_html_needs_no_session_before_and_after_setup(client, settings):
    # Before setup...
    r = client.get("/sandbox/html")
    assert r.status_code == 200
    # ...and after setup, still with no session or login.
    _mark_initialized(settings)
    r = client.get("/sandbox/html")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == SPEC_SANDBOX_CSP


def test_sandbox_html_is_not_cached(client):
    r = client.get("/sandbox/html")
    assert r.headers["cache-control"] == "no-cache"


def test_other_paths_keep_the_global_csp(client, settings):
    _mark_initialized(settings)
    for path in ("/", "/tickets", "/healthz", "/static/js/api.js"):
        r = client.get(path, follow_redirects=False)
        assert r.headers["content-security-policy"] == csp(), path
        assert r.headers["content-security-policy"] != SPEC_SANDBOX_CSP, path


def test_path_variants_that_are_not_the_route_keep_the_global_csp(client):
    # None of these hit the `/sandbox/html` route: a trailing slash, an extra suffix, and a doubled
    # slash are all different paths, so the exact-match per-path override in SecurityHeadersMiddleware
    # must not fire for them (whatever status code routing gives them).
    for path in ("/sandbox/html/", "/sandbox/htmlx", "/sandbox//html"):
        r = client.get(path, follow_redirects=False)
        assert r.headers["content-security-policy"] == csp(), path
        assert r.headers["content-security-policy"] != SPEC_SANDBOX_CSP, path


def test_query_string_and_wrong_method_still_get_the_sandbox_csp(client):
    # A query string doesn't change scope["path"], and a method the route doesn't support (405)
    # still goes through the same middleware on the same path: the header is chosen by path alone.
    r = client.get("/sandbox/html?v=1")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == SPEC_SANDBOX_CSP

    r = client.post("/sandbox/html")
    assert r.status_code == 405
    assert r.headers["content-security-policy"] == SPEC_SANDBOX_CSP


def test_sandbox_html_is_stamped_and_has_no_inline_script(client, settings, monkeypatch):
    monkeypatch.setenv("FS_BUILD", "abc123")
    pages.build_stamp.cache_clear()
    r = client.get("/sandbox/html")
    assert r.status_code == 200
    assert "/static/js/sandbox-frame.js?v=abc123" in r.text
    assert "{{BUILD}}" not in r.text
    pages.build_stamp.cache_clear()
    _assert_every_script_is_a_src_only_tag(r.text)


class _ScriptAudit(html.parser.HTMLParser):
    """Every <script> must be `src`-only with an empty body: no inline code, and no code smuggled
    into a tag that also has a src (e.g. `<script src="x.js">evil()</script>`)."""

    def __init__(self):
        super().__init__()
        self.in_script = False
        self.problems = []

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return
        a = dict(attrs)
        self.in_script = True
        if not a.get("src"):
            self.problems.append("a <script> with no src")

    def handle_data(self, data):
        if self.in_script and data.strip():
            self.problems.append(f"non-empty <script> body: {data.strip()!r}")

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False


def _assert_every_script_is_a_src_only_tag(text):
    audit = _ScriptAudit()
    audit.feed(text)
    assert audit.problems == []


def test_sandbox_html_markup_on_disk_has_no_inline_script():
    text = (pages.STATIC_DIR / "sandbox.html").read_text(encoding="utf-8")
    assert 'src="/static/js/sandbox-frame.js?v={{BUILD}}"' in text
    _assert_every_script_is_a_src_only_tag(text)
