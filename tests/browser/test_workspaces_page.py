"""The status page (Remote R9): workspaces grouped by machine with state, activity, needs-you, paired devices and an
Open or Snapshot action; names are opened in the browser and drawn as text; an absent number says unknown."""
import httpx
import pytest
from playwright.sync_api import expect

from .conftest import EXAMPLE_DOC, PHONE, login_ui, make_desktop

pytestmark = pytest.mark.browser

DESKTOP = {"width": 1280, "height": 800}
BEAT = {"sessions": 2, "in_progress": 3, "needs_you": 1, "factory": "running",
        "children_done": 4, "children_total": 9, "budget_pct": 37}


def heartbeat(base, space, headers, **over):
    r = httpx.post(f"{base}/api/presence/{space}/heartbeat", json={**BEAT, **over}, headers=headers, timeout=30)
    assert r.status_code == 204, r.text


@pytest.fixture
def two_workspaces(live_server, sim, mirror_with_question):
    """One workspace whose host is online (with a synced ticket that needs the human) and one that never started,
    on another machine, with a label that tries to be markup."""
    heartbeat(live_server.url, mirror_with_question.space, mirror_with_question.headers)
    other, _ = make_desktop(live_server, sim, name="mac-mini", label="<b>Second</b> workspace")
    return mirror_with_question, other


def open_page(page, live_server, sim, size):
    page.set_viewport_size(size)
    login_ui(page, live_server.url, sim.passphrase, then="/workspaces")
    page.locator("#ws-loading").wait_for(state="hidden")


@pytest.mark.parametrize("size", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_rows_show_state_activity_needs_and_actions(page, live_server, sim, two_workspaces, size):
    first, other = two_workspaces
    open_page(page, live_server, sim, size)
    online = page.locator(f'.wrow[data-space="{first.space}"]')
    expect(online).to_contain_text("Acme Energy")
    expect(online.locator(".pill").first).to_have_text("Online")
    expect(online).to_contain_text("2 sessions working")
    expect(online).to_contain_text("3 in progress")
    expect(online).to_contain_text("Factory running · 4 of 9 done · 37% of budget")
    expect(online).to_contain_text("1 needs you")             # from the synced snapshot, not the host's own count
    expect(online).to_contain_text("paired device")
    expect(online.get_by_role("link", name="Open Acme Energy")).to_be_visible()
    never = page.locator(f'.wrow[data-space="{other}"]')
    expect(never.locator(".pill").first).to_have_text("Never started")
    expect(never).to_contain_text("No live data")
    expect(never).to_contain_text("Nothing needs you")
    expect(never.get_by_role("link", name="Snapshot")).to_be_visible()
    # grouped by machine, each group named
    names = [t.strip().lower() for t in page.locator(".wgroup > h2").all_inner_texts()]
    assert sorted(names) == ["mac-mini", "macbook-pro"]
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "no sideways scroll"


def test_names_are_drawn_as_text_never_as_markup(page, live_server, sim, two_workspaces):
    _, other = two_workspaces
    open_page(page, live_server, sim, DESKTOP)
    row = page.locator(f'.wrow[data-space="{other}"]')
    expect(row.locator(".wrow-name")).to_have_text("<b>Second</b> workspace")
    assert row.locator(".wrow-name b").count() == 0


def test_a_stopped_host_shows_stopped_and_no_stale_counts(page, live_server, sim, two_workspaces):
    first, _ = two_workspaces
    r = httpx.post(f"{live_server.url}/api/presence/{first.space}/goodbye", json={}, headers=first.headers, timeout=30)
    assert r.status_code == 204
    open_page(page, live_server, sim, PHONE)
    row = page.locator(f'.wrow[data-space="{first.space}"]')
    expect(row.locator(".pill").first).to_have_text("Stopped")
    expect(row).not_to_contain_text("sessions working")
    expect(row).to_contain_text("1 needs you")                  # the snapshot still works with the host off
    expect(row.get_by_role("link", name="Snapshot Acme Energy")).to_be_visible()


def test_an_absent_number_is_unknown_not_zero(page, live_server, sim, mirror_with_question):
    httpx.post(f"{live_server.url}/api/presence/{mirror_with_question.space}/heartbeat", headers=mirror_with_question.headers,
               json={"sessions": 0, "in_progress": 0, "needs_you": 0, "factory": "none"}, timeout=30).raise_for_status()
    open_page(page, live_server, sim, DESKTOP)
    row = page.locator(f'.wrow[data-space="{mirror_with_question.space}"]')
    expect(row).to_contain_text("Nothing reported running")
    expect(row).not_to_contain_text("budget")


def test_open_shows_the_synced_ticket_snapshot(page, live_server, sim, two_workspaces):
    first, _ = two_workspaces
    open_page(page, live_server, sim, PHONE)
    page.locator(f'.wrow[data-space="{first.space}"]').get_by_role("link", name="Open Acme Energy").click()
    expect(page.locator("#ws-title")).to_have_text("Acme Energy")
    expect(page.locator("#ws-list")).to_contain_text(EXAMPLE_DOC["id"])
    expect(page.locator("#ws-list")).to_contain_text("later update")
    page.get_by_role("link", name="Workspaces").first.click()
    expect(page.locator(".wrow").first).to_be_visible()


def test_the_nav_has_a_current_workspaces_tab(page, live_server, sim, two_workspaces):
    open_page(page, live_server, sim, PHONE)
    expect(page.locator('.nav-item[aria-current="page"]')).to_have_text("Workspaces")
