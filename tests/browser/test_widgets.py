"""Ticket widgets on the phone (js/widgets.js, /sandbox/widget): the card shows the text alternative, Show draws
the document in the sandboxed frame (a core document inline, the decision-matrix template from a shared FILE with
orch-core's real kit), Stop and a kit that never says ready bring the text back. Local server only."""
import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from .conftest import ADDON_LEVELS

pytestmark = pytest.mark.browser
SHOTS = os.environ.get("FS_SHOTS")
TEMPLATE = (Path(__file__).parents[1] / "vectors" / "widget-template.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _chromium_only(browser_name):
    if browser_name != "chromium":
        pytest.skip("Chromium only here")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extra(index: int, html: str, *, layer="html", title="Extra", sha=None, raw="{}") -> dict:
    """An agent-HTML entry for the fence `raw`, its pin the digest of `html` unless `sha` says otherwise."""
    return {"section": "Findings", "index": index, "key": str(index), "layer": layer, "name": "artifacts/x.html",
            "title": title, "text": "The text instead", "doc": html, "sha256": sha or _sha(html), "raw_sha256": _sha(raw)}


def _push(mirror, sim, *, extra=None, template=TEMPLATE) -> dict:
    doc = copy.deepcopy(ADDON_LEVELS["full+widgets"])
    up = sim.upload("DEMO-0038-widget-1.html", template.encode("utf-8"), mime="text/html", tags=["context"])
    doc["widgets"][1]["file"] = up["id"]
    if extra:
        doc["sections"]["Findings"] += "\n\n```orch\n{}\n```"
        doc["widgets"].append(extra)
    mirror.push(doc, rev=2)
    return doc


def _open_findings(page, mirror):
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(f"{mirror.base}/t/{mirror.n}")
    page.locator("summary", has_text="More ·").click()      # Findings sits in no chapter: the More sections
    return errors


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_core_and_template_widgets_draw_in_the_frame(phone_page, mirror_with_question, sim, scheme):
    _push(mirror_with_question, sim)
    page = phone_page(scheme)
    errors = _open_findings(page, mirror_with_question)
    core, template = page.locator(".wcard").all()
    assert core.locator(".wcard-chip").inner_text() == "core"
    assert "files: 14" in core.locator(".wcard-text").inner_text()
    assert core.locator(".wcard-source").inner_text() == "Source: agent-measured"
    assert template.locator(".wcard-chip").inner_text() == "agent HTML · decision-matrix@1"
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=Path(SHOTS) / f"widgets-text-{scheme}.png", full_page=True)

    core.get_by_role("button", name="Show").click()
    core.get_by_role("button", name="Stop").wait_for()
    assert core.locator(".wcard-text").is_hidden()
    frame = core.frame_locator("iframe.wcard-iframe")
    frame.get_by_text("largest", exact=True).wait_for()
    assert core.locator("iframe").get_attribute("sandbox") == "allow-scripts"
    assert int(core.locator("iframe").evaluate("f => f.getBoundingClientRect().height")) > 60

    template.get_by_role("button", name="Show").click()
    template.get_by_role("button", name="Stop").wait_for(timeout=10_000)
    template.frame_locator("iframe.wcard-iframe").get_by_text("SQLite file").first.wait_for()
    dark = template.frame_locator("iframe.wcard-iframe").locator("html").get_attribute("data-theme")
    assert dark == scheme
    page.wait_for_timeout(300)
    if SHOTS:
        core.scroll_into_view_if_needed()
        page.screenshot(path=Path(SHOTS) / f"widgets-shown-{scheme}.png", full_page=True)

    # The widget HTML never entered the page: every script on it is the app's own.
    srcs = page.evaluate("() => [...document.scripts].map(s => s.getAttribute('src') || 'INLINE')")
    assert all(s.startswith("/static/") for s in srcs), srcs
    assert not [e for e in errors if "Content Security Policy" in e or "Refused" in e], errors

    template.get_by_role("button", name="Stop").click()
    assert template.locator("iframe").count() == 0 and template.locator(".wcard-text").is_visible()


def test_a_widget_that_never_says_ready_falls_back_to_its_text(phone_page, mirror_with_question, sim):
    silent = _extra(2, '<!doctype html><html><head><meta name="orch-frame" content="silent0001"></head><body>x</body></html>',
                    title="Silent")
    _push(mirror_with_question, sim, extra=silent)
    page = phone_page("light")
    _open_findings(page, mirror_with_question)
    card = page.locator(".wcard").nth(2)
    card.get_by_role("button", name="Show").click()
    card.get_by_text("didn't start in time").wait_for(timeout=6000)
    assert card.locator("iframe").count() == 0 and card.get_by_text("The text instead").is_visible()


def test_a_widget_that_navigates_its_frame_is_torn_down(phone_page, mirror_with_question, sim):
    leaving = _extra(2, '<!doctype html><html><head><meta name="orch-frame" content="leave00001"></head><body>x'
                        '<script>setTimeout(function () { location.href = "about:blank"; }, 200)</script></body></html>',
                     title="Leaving")
    _push(mirror_with_question, sim, extra=leaving)
    page = phone_page("light")
    _open_findings(page, mirror_with_question)
    card = page.locator(".wcard").nth(2)
    card.get_by_role("button", name="Show").click()
    card.get_by_text("tried to leave the page").wait_for(timeout=2500)  # before the 3 s ready deadline
    assert card.locator("iframe").count() == 0 and card.get_by_text("The text instead").is_visible()


def _shows_text_only(card, why: str):
    card.get_by_role("button", name="Show").click()
    card.get_by_text(why).wait_for(timeout=6000)
    assert card.locator("iframe").count() == 0 and card.get_by_text("The text instead").is_visible()


def test_a_document_that_does_not_match_its_pin_is_never_drawn(phone_page, mirror_with_question, sim):
    """Inline: the pin is another document's. The card keeps its text and no frame is ever created."""
    inline = _extra(2, "<!doctype html><p>swapped</p>", sha=_sha("<!doctype html><p>what was sent</p>"))
    _push(mirror_with_question, sim, extra=inline)
    page = phone_page("light")
    _open_findings(page, mirror_with_question)
    page.evaluate("() => { window.__frames = 0; new MutationObserver((ms) => ms.forEach((m) => m.addedNodes.forEach("
                            "(n) => { if (n.nodeName === 'IFRAME' || (n.querySelector && n.querySelector('iframe'))) window.__frames++; })))"
                            ".observe(document.body, {childList: true, subtree: true}); }")
    _shows_text_only(page.locator(".wcard").nth(2), "changed since it was sent")
    assert page.evaluate("() => window.__frames") == 0


def test_a_shared_file_that_does_not_match_its_pin_is_never_drawn(phone_page, mirror_with_question, sim):
    """FILE: the file holds a swapped template; the pin is the original's digest."""
    doc = _push(mirror_with_question, sim, template=TEMPLATE + "<!-- swapped -->")
    assert doc["widgets"][1]["sha256"] == _sha(TEMPLATE)
    page = phone_page("light")
    _open_findings(page, mirror_with_question)
    card = page.locator(".wcard").nth(1)
    card.get_by_role("button", name="Show").click()
    card.get_by_text("changed since it was sent").wait_for(timeout=10_000)
    assert card.locator("iframe").count() == 0 and card.locator(".wcard-text").is_visible()


def test_oversize_documents_are_refused_before_they_are_drawn(phone_page, mirror_with_question, sim):
    """A FILE far over the cap whose row claims a tiny size (the claim is not trusted: the blob is capped as read),
    and an inline document over 128 KiB (no Show button at all)."""
    big = "<!doctype html><p>" + "a" * (9 * 1024 * 1024 + 4096) + "</p>"
    inline = _extra(2, "<p>" + "b" * (128 * 1024 + 1) + "</p>")
    doc = _push(mirror_with_question, sim, extra=inline, template=big)
    fid = doc["widgets"][1]["file"]
    doc["widgets"][1]["sha256"] = _sha(big)
    mirror_with_question.push(doc, rev=3)

    def lie(route):
        r = route.fetch()
        body = r.json()
        body["size"] = 10
        route.fulfill(response=r, json=body)
    page = phone_page("light")
    page.route(f"**/api/files/{fid}", lie)
    _open_findings(page, mirror_with_question)
    card = page.locator(".wcard").nth(1)
    card.get_by_role("button", name="Show").click()
    card.get_by_text("too large").wait_for(timeout=30_000)
    assert card.locator("iframe").count() == 0
    assert page.locator(".wcard").nth(2).get_by_role("button", name="Show").count() == 0


def test_an_unknown_layer_has_no_show_button(phone_page, mirror_with_question, sim):
    odd = _extra(2, "<p>x</p>", layer="invalid")
    _push(mirror_with_question, sim, extra=odd)
    page = phone_page("light")
    _open_findings(page, mirror_with_question)
    card = page.locator(".wcard").nth(2)
    assert card.get_by_role("button").count() == 0 and card.get_by_text("The text instead").is_visible()


HOSTILE = """<!doctype html><html><body><p id="r">running</p><script>
var out = {};
function t(name, fn) { try { out[name] = String(fn()); } catch (e) { out[name] = "blocked: " + e.name; } }
t("parent.document", function () { parent.document.title = "pwned"; return parent.document.title; });
t("parent.localStorage", function () { parent.localStorage.setItem("pwned", "1"); return "set"; });
t("own.localStorage", function () { localStorage.setItem("pwned", "1"); return "set"; });
t("top.location", function () { top.location.href = "about:blank"; return "navigated"; });
t("cookie", function () { return document.cookie || "empty"; });
fetch("/api/files").then(function (r) { out.fetch = "status " + r.status; }, function () { out.fetch = "blocked"; })
  .then(function () { document.getElementById("r").textContent = JSON.stringify(out); });
</script></body></html>"""


def test_a_hostile_widget_reaches_neither_the_page_nor_the_api(phone_page, mirror_with_question, sim):
    _push(mirror_with_question, sim, extra=_extra(2, HOSTILE, title="Hostile"))
    page = phone_page("light")
    _open_findings(page, mirror_with_question)
    page.evaluate("() => { document.title = 'ticket'; localStorage.removeItem('pwned'); }")
    before = page.evaluate("() => [document.title, location.href, JSON.stringify(Object.entries(localStorage).sort()), document.body.innerHTML.length]")
    api_from_frame = []
    page.on("request", lambda r: api_from_frame.append(r.url) if r.frame != page.main_frame and "/api/" in r.url else None)
    card = page.locator(".wcard").nth(2)
    card.get_by_role("button", name="Show").click()
    report = card.frame_locator("iframe").locator("#r")
    report.filter(has_text="parent.document").wait_for(timeout=10_000)
    got = json.loads(report.inner_text())
    assert got["parent.document"].startswith("blocked") and got["parent.localStorage"].startswith("blocked")
    assert got["top.location"].startswith("blocked") and got["fetch"] == "blocked"
    assert got["own.localStorage"].startswith("blocked")
    assert got["cookie"].startswith("blocked") or got["cookie"] == "empty"
    page.wait_for_timeout(500)
    after = page.evaluate("() => [document.title, location.href, JSON.stringify(Object.entries(localStorage).sort()), document.body.innerHTML.length]")
    assert before[:3] == after[:3], (before, after)
    assert api_from_frame == []


def test_widgets_are_found_by_section_and_digest_not_by_position(phone_page, mirror_with_question, sim):
    """The doc lists its widgets in the other order, and a third fence has no entry (its text was edited after the
    digest was made): the cards still follow their own fences and the edited fence stays text."""
    doc = _push(mirror_with_question, sim)
    doc["widgets"].reverse()
    doc["sections"]["Findings"] += "\n\n```orch\n{\"type\": \"text\", \"text\": \"edited\"}\n```"
    mirror_with_question.push(doc, rev=3)
    page = phone_page("light")
    _open_findings(page, mirror_with_question)
    cards = page.locator(".wcard").all()
    assert [c.locator(".wcard-chip").inner_text() for c in cards] == ["core", "agent HTML · decision-matrix@1"]
    assert page.get_by_text('"text": "edited"').count() >= 1


E2E = os.environ.get("TIX_WIDGET_DOCS")


@pytest.mark.skipif(not E2E, reason="needs TIX_WIDGET_DOCS from addons/orch-tix/tests/test_ticket_widgets.py::"
                                    "test_end_to_end_documents_are_real_frames_with_kit_and_data")
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_end_to_end_documents_draw_in_the_pwa(phone_page, mirror_with_question, sim, scheme):
    """What the addon made against the real orch-core (a chrome-less core `checks`, decision-matrix@1 and a one-off
    html, both frames with kit, data and fonts) drawn by the PWA at 390 px without a CSP complaint."""
    src = Path(E2E)
    widgets = json.loads((src / "widgets.json").read_text(encoding="utf-8"))
    findings = (src / "findings.txt").read_text(encoding="utf-8")
    for w in widgets:
        if "file" in w:
            body = (src / f"{w['layer']}.html").read_bytes()
            w["file"] = sim.upload(f"DEMO-0038-widget-{w['key']}.html", body, mime="text/html", tags=["context"])["id"]
    doc = copy.deepcopy(ADDON_LEVELS["full+widgets"])
    doc["sections"]["Findings"] = findings
    doc["widgets"] = widgets
    mirror_with_question.push(doc, rev=2)
    page = phone_page(scheme)
    errors = _open_findings(page, mirror_with_question)
    checks, matrix, one_off = page.locator(".wcard").all()
    for card in (checks, matrix, one_off):
        card.get_by_role("button", name="Show").click()
        card.get_by_role("button", name="Stop").wait_for(timeout=10_000)
    one_off.frame_locator("iframe").get_by_text("One-off: 3 runs, all green").wait_for()
    matrix.frame_locator("iframe").get_by_text("SQLite").first.wait_for()
    frame = checks.frame_locator("iframe").locator("html")
    frame.get_by_text("needs a phone to try").wait_for()
    # Chrome once: the card's title, not again inside the frame.
    assert frame.locator("figcaption, details, .w-source").count() == 0
    # The checks table stacks at 390 px: nothing wider than the frame, each row a grid.
    assert frame.evaluate("e => e.scrollWidth <= e.clientWidth"), "checks overflow the frame"
    assert frame.evaluate("e => getComputedStyle(e.querySelector('.w-checks tr')).display") == "grid"
    for card in (checks, matrix):
        fonts = card.frame_locator("iframe").locator("html").evaluate(
            "async () => { await document.fonts.ready; return [...document.fonts].filter(f => f.status === 'loaded')"
            ".map(f => f.family.replace(/\"/g, '')); }")
        assert "Manrope" in fonts, fonts
    assert matrix.frame_locator("iframe").locator("html").get_attribute("data-theme") == scheme
    page.wait_for_timeout(400)
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        for name, card in (("checks", checks), ("matrix", matrix), ("html", one_off)):
            card.scroll_into_view_if_needed()  # a frame off screen comes out blank in a full-page shot
            card.screenshot(path=Path(SHOTS) / f"widgets-e2e-{name}-{scheme}.png")
        page.screenshot(path=Path(SHOTS) / f"widgets-e2e-{scheme}.png", full_page=True)
    assert not [e for e in errors if "Content Security Policy" in e or "Refused" in e], errors
