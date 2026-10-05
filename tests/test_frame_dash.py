"""/sandbox/dash: the frame a remote dashboard page is drawn in (docs/bridge-frame.md). Its policy has a nonce per
load; the shim is inline; the token the TIX page chose is written into the document once."""
import re

from fileshare.routes import pages
from fileshare.headers import DASH_CSP, DASH_PATH, FRAME_CSP, SANDBOX_CSP, WIDGET_CSP, csp

TOK = "A" * 24
# An independent literal (not the constant): the policy as the spec of this frame states it, with a nonce slot.
EXPECTED = re.compile(
    r"^sandbox allow-scripts; default-src 'none'; script-src 'nonce-([A-Za-z0-9_-]{16,})'; style-src 'unsafe-inline'; "
    r"img-src blob: data:; font-src blob: data:; media-src blob: data:; connect-src 'none'; frame-src 'none'; "
    r"form-action 'none'; base-uri 'none'; frame-ancestors 'self'$"
)


def _load(client, tok=TOK):
    r = client.get(f"/sandbox/dash?tok={tok}")
    assert r.status_code == 200, r.text
    return r


def test_the_policy_is_exactly_the_stated_one_with_a_nonce(client):
    r = _load(client)
    policies = r.headers.get_list("content-security-policy")
    assert len(policies) == 1
    m = EXPECTED.match(policies[0])
    assert m, policies[0]
    assert DASH_PATH == "/sandbox/dash" and "{nonce}" in DASH_CSP and EXPECTED.match(DASH_CSP.replace("{nonce}", "x" * 20))


def test_the_policy_has_no_same_origin_no_script_unsafe_and_no_network(client):
    p = _load(client).headers["content-security-policy"]
    directives = {d.split(" ", 1)[0]: d.split(" ", 1)[1] if " " in d else "" for d in (x.strip() for x in p.split(";"))}
    assert "allow-same-origin" not in directives["sandbox"] and directives["sandbox"] == "allow-scripts"
    assert "unsafe" not in directives["script-src"] and "'self'" not in directives["script-src"]
    for name in ("connect-src", "frame-src", "form-action", "base-uri", "default-src"):
        assert directives[name] == "'none'", name


def test_the_nonce_on_the_shim_is_the_one_in_the_header_and_new_every_load(client):
    seen = set()
    for _ in range(3):
        r = _load(client)
        header_nonce = EXPECTED.match(r.headers["content-security-policy"]).group(1)
        assert re.search(rf'<script nonce="{re.escape(header_nonce)}">', r.text)
        assert r.text.count("<script") == 1                      # the shim alone: no other script in the document
        seen.add(header_nonce)
    assert len(seen) == 3


def test_the_token_is_written_into_the_document_and_the_shim_is_inline(client):
    r = _load(client, "tok_" + "x" * 20)
    assert 'data-tok="tok_xxxxxxxxxxxxxxxxxxxx"' in r.text
    assert "{{" not in r.text
    assert "hello" in r.text and "orch-frame-1" in r.text         # the shim's own source
    assert "src=" not in r.text.split("<script", 1)[1].split(">", 1)[0]


def test_a_missing_or_malformed_token_is_refused(client):
    assert client.get("/sandbox/dash").status_code == 400
    for tok in ("short", "A" * 65, 'A"B' + "c" * 22, "A B" + "c" * 22, "<script>" + "c" * 20):
        assert client.get("/sandbox/dash", params={"tok": tok}).status_code == 400, tok


def test_never_cached_and_no_session_needed(client):
    r = _load(client)
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_other_frames_and_pages_keep_their_policies(client):
    assert client.get("/sandbox/html").headers["content-security-policy"] == SANDBOX_CSP
    assert client.get("/sandbox/widget").headers["content-security-policy"] == WIDGET_CSP
    assert client.get("/healthz").headers["content-security-policy"] == csp()
    assert client.get("/sandbox/dash/?tok=" + TOK, follow_redirects=False).headers["content-security-policy"] == csp()


def test_every_sandbox_route_has_a_frame_policy():
    """Derived from the app's own route table, not a list: a /sandbox/ page without a FRAME_CSP entry would be
    served under the app's global policy, which lets 'self' scripts run in the frame."""
    paths = {r.path for r in pages.router.routes if getattr(r, "path", "").startswith("/sandbox/")}
    assert paths and paths == set(FRAME_CSP)


def test_the_shim_is_the_file_and_cannot_close_its_own_script(client):
    from pathlib import Path
    shim = (Path(__file__).parents[1] / "fileshare" / "static" / "js" / "frame-shim.js").read_text(encoding="utf-8")
    assert "</script" not in shim.lower() and "<!--" not in shim
    assert shim in _load(client).text
