"""The AI Factory over the bridge, TIX side (R13, #27) in a real browser against the fake host.

The Factory pages are the dashboard's own, drawn in the frame; their buttons post urlencoded forms. The rules below
reproduce the STRUCTURE of the subject texts in orch-core's dashboard/factory_remote.py (the words there decide; the
limits text of a Start is a stand-in). Deny, Revoke and Pause are Decide only: no sheet. Needs a real phone and not
covered here: the Face ID prompt itself, and the real dashboard's pages."""
import hashlib
import json

import pytest
from playwright.sync_api import expect

from .test_remote import HTML, dash, frame_of, wait_h1
from .test_unlock import (_chromium_only, authenticator, base, confirm, live_server, make_host, pair, open_dash,  # noqa: F401
                          sheet, signed_in)

pytestmark = pytest.mark.browser

SEEN = "c" * 64
SHA = "d" * 64
START_BODY = f"gate=requirements&seen={SEEN}&factory=on"
EPIC = "B-0007"
PR = "/per" + "mits"          # the dashboard's request routes


def digest(**fields):
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def form(action, button, **fields):
    inputs = "".join(f'<input type="hidden" name="{k}" value="{v}">' for k, v in fields.items())
    return f'<form method="post" action="{action}">{inputs}<button id="{button}">{button}</button></form>'


def home(*forms):
    return {"status": 200, "headers": HTML, "page": True,
            "body": '<!doctype html><html><head><meta charset="utf-8"><title>Home</title></head><body><h1>Home</h1>' + "".join(forms) + "</body></html>"}


def rule(kind, shown, **bound):
    return {"kind": kind, "shown": shown, "digest": digest(kind=kind, **bound), "scope": "type"}


START = rule("charter", f"Start the AI Factory on epic {EPIC} Billing cleanup with 3 open children. Limits: factory True", seen=SEEN)
ONCE = rule("permission", f"Allow once request PR-3 on epic {EPIC}: git push origin factory/b-0007", scope="once", sha=SHA)
FOR_EPIC = rule("permission", f"Allow for the whole epic request PR-4 on epic {EPIC}: git push origin factory/b-0007", scope="epic", sha=SHA)
VERDICT = rule("verdict", f"Accept epic {EPIC} Billing cleanup: mark done B-0008, B-0009", seen=SEEN)

PAGES = {
    "/": home(form(f"/t/{EPIC}/approve", "start", gate="requirements", seen=SEEN, factory="on"),
              form(f"{PR}/PR-3/grant", "once", sha=SHA, scope="once"),
              form(f"{PR}/PR-4/grant", "epic", sha=SHA, scope="epic"),
              form(f"{PR}/PR-3/deny", "deny", sha=SHA),
              form(f"{PR}/grants/G-1/revoke", "revoke"),
              form(f"/t/{EPIC}/epic/pause", "pause"),
              form(f"/t/{EPIC}/verdict", "verdict", seen=SEEN, verdict="done"),
              form(f"{PR}/PR-9/grant", "stale", sha="0" * 64, scope="once")),
    "/other": dash("Other"),
}
for p in (f"/t/{EPIC}/approve", f"{PR}/PR-3/grant", f"{PR}/PR-4/grant", f"{PR}/PR-3/deny", f"{PR}/grants/G-1/revoke",
          f"/t/{EPIC}/epic/pause", f"/t/{EPIC}/verdict", f"{PR}/PR-9/grant"):
    PAGES[p] = dash("Done")


def requires():
    return {f"/t/{EPIC}/approve": lambda m, b: dict(START),
            f"{PR}/PR-3/grant": lambda m, b: dict(ONCE),
            f"{PR}/PR-4/grant": lambda m, b: dict(FOR_EPIC),
            f"/t/{EPIC}/verdict": lambda m, b: dict(VERDICT),
            f"{PR}/PR-9/grant": lambda m, b: {"refuse": "forbidden_scope"}}


@pytest.fixture
def setup(signed_in, base, make_host, authenticator):
    host = make_host(PAGES, requires())
    pair(signed_in, base, host)
    open_dash(signed_in, base, host)
    return signed_in, host


def ran(host, path):
    return [b for b in host.bodies if b[1] == path]


@pytest.mark.parametrize("button,path,want,body", [
    ("start", f"/t/{EPIC}/approve", START, START_BODY),
    ("once", f"{PR}/PR-3/grant", ONCE, f"sha={SHA}&scope=once"),
    ("epic", f"{PR}/PR-4/grant", FOR_EPIC, f"sha={SHA}&scope=epic"),
    ("verdict", f"/t/{EPIC}/verdict", VERDICT, f"seen={SEEN}&verdict=done"),
])
def test_a_factory_approval_shows_the_hosts_exact_text_and_runs_the_same_form_once(setup, button, path, want, body):
    page, host = setup
    frame_of(page).locator(f"#{button}").click()
    expect(sheet(page)).to_be_visible(timeout=30000)
    expect(page.locator("#unlock-text")).to_have_text(want["shown"])
    expect(sheet(page)).to_contain_text(want["digest"][:8])
    assert ran(host, path) == []                                  # nothing ran before the person confirmed
    confirm(page)
    wait_h1(page, "Done")
    assert ran(host, path) == [("POST", path, body.encode())]    # the very same urlencoded body, once
    assert [a["ok"] for a in host.audit] == [True]
    expect(sheet(page)).to_have_count(0)


@pytest.mark.parametrize("button,path", [("deny", f"{PR}/PR-3/deny"), ("revoke", f"{PR}/grants/G-1/revoke"), ("pause", f"/t/{EPIC}/epic/pause")])
def test_deny_revoke_and_pause_need_no_sheet(setup, button, path):
    page, host = setup
    frame_of(page).locator(f"#{button}").click()
    wait_h1(page, "Done")
    assert len(ran(host, path)) == 1 and host.audit == [] and sheet(page).count() == 0


def test_cancelling_the_start_sheet_starts_nothing_and_says_so(setup):
    page, host = setup
    frame_of(page).locator("#start").click()
    expect(sheet(page)).to_be_visible(timeout=30000)
    page.locator("#unlock-cancel").click()
    expect(page.locator("#remote-notice")).to_contain_text("Not confirmed, so nothing was done")
    assert ran(host, f"/t/{EPIC}/approve") == [] and host.audit == []


def test_a_refusal_without_a_sheet_is_readable_text(setup):
    page, host = setup
    frame_of(page).locator("#stale").click()
    expect(page.locator("#remote-notice")).to_have_text("This browser is not allowed to do that on that computer.", timeout=30000)
    assert sheet(page).count() == 0 and ran(host, f"{PR}/PR-9/grant") == [] and host.audit == []


def test_a_start_text_that_looks_like_markup_is_drawn_as_text(signed_in, base, make_host, authenticator):
    hostile = {**START, "shown": 'Start the AI Factory on epic B-0007 <img src=x onerror="window.__x=1"> with 3 open children.'}
    host = make_host(PAGES, {**requires(), f"/t/{EPIC}/approve": lambda m, b: dict(hostile)})
    pair(signed_in, base, host)
    open_dash(signed_in, base, host)
    frame_of(signed_in).locator("#start").click()
    expect(sheet(signed_in)).to_be_visible(timeout=30000)
    expect(signed_in.locator("#unlock-text")).to_have_text(hostile["shown"])
    assert signed_in.locator("#unlock-sheet img").count() == 0 and signed_in.evaluate("window.__x") is None


# ---- the status side ------------------------------------------------------------------------------------------------

def beat(host, factory, **extra):
    body = {"sessions": 1, "in_progress": 1, "needs_you": 1, "factory": factory, **extra}
    host._http.post(f"/api/presence/{host.space}/heartbeat", json=body).raise_for_status()


FACTORY_TEXT = {
    "running": "Factory running · 4 of 9 done · 37% of budget",
    "paused": "Factory paused · 4 of 9 done · 37% of budget",
    "waiting": "Factory waiting on a permission · 4 of 9 done · 37% of budget",
    "ready": "Factory ready · 4 of 9 done · 37% of budget",
    "stopped": "Factory stopped · 4 of 9 done · 37% of budget",
    "done": "Factory done · 4 of 9 done · 37% of budget",
}


@pytest.mark.parametrize("code", sorted(FACTORY_TEXT) + ["none"])
@pytest.mark.parametrize("where", ["/workspaces", "/remote"])
def test_the_workspace_card_says_the_factory_state(signed_in, base, make_host, where, code):
    host = make_host(PAGES, requires())
    host._beat = False
    beat(host, code, children_done=4, children_total=9, budget_pct=37)
    signed_in.goto(f"{base}{where}")
    card = signed_in.locator(f'.wrow[data-space="{host.space}"]')
    expect(card).to_be_visible(timeout=30000)
    if code == "none":
        expect(card).not_to_contain_text("Factory")
    else:
        expect(card).to_contain_text(FACTORY_TEXT[code])
    expect(card.locator(".wrow-factory")).to_have_count(1 if code == "waiting" and where == "/workspaces" else 0)


def test_a_waiting_factory_links_to_its_page_in_the_frame(signed_in, base, make_host):
    host = make_host(PAGES, requires())
    host._beat = False
    pair(signed_in, base, host)
    beat(host, "waiting")
    signed_in.goto(f"{base}/workspaces")
    link = signed_in.locator(f'.wrow[data-space="{host.space}"] a.wrow-factory')
    expect(link).to_contain_text("waiting on a permission from you", timeout=30000)
    link.click()
    wait_h1(signed_in, "Home")
    assert "/sandbox/dash" in frame_of(signed_in).url


def test_remote_opens_the_frame_at_a_valid_path_and_ignores_an_invalid_one(signed_in, base, make_host):
    host = make_host(PAGES, requires())
    pair(signed_in, base, host)
    signed_in.goto(f"{base}/remote?space={host.space}&path=%2Fother")
    wait_h1(signed_in, "Other")
    signed_in.goto(f"{base}/remote?space={host.space}&path=%2F..%2Fx")
    wait_h1(signed_in, "Home")


@pytest.mark.parametrize("where", ["/workspaces", "/remote"])
def test_a_lost_host_says_what_stops_and_claims_nothing_about_running_sessions(signed_in, base, make_host, where):
    host = make_host(PAGES, requires())
    host._beat = False
    beat(host, "waiting", sessions=3, children_done=1, children_total=4, budget_pct=10)

    def lose(route):
        r = route.fetch()
        body = r.json()
        for s in body["spaces"]:
            s["state"] = "lost"
        route.fulfill(response=r, json=body)
    signed_in.route("**/api/presence", lose)
    signed_in.goto(f"{base}{where}")
    card = signed_in.locator(f'.wrow[data-space="{host.space}"]')
    expect(card).to_contain_text("Factory: host lost: nothing new starts and no parked child wakes until it is back", timeout=30000)
    text = card.inner_text().lower()
    for word in ("session", "running", "working", "in progress", "budget", "done", "waiting"):
        assert word not in text, word
    assert card.locator(".wrow-factory").count() == 0
