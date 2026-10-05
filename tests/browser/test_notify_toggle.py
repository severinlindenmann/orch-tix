"""The phone's "Notify me about this ticket" switch: off by default, a tap reaches the server (and so the desktop's
notify-state) and survives a reload; turning it off again also reaches the server."""
import os

import httpx
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True)
def _chromium_only(browser_name):
    if browser_name != "chromium":
        pytest.skip("chromium only")


@pytest.fixture
def browser_context_args(browser_context_args):
    return {**browser_context_args, "service_workers": "allow"}


def _desktop_state(m):
    r = httpx.get(f"{m.base}/api/spaces/{m.space}/notify", headers=m.headers, timeout=30)
    assert r.status_code == 200, r.text
    return next(x for x in r.json()["mirrors"] if x["id"] == m.id)


def test_the_phone_switch_round_trip(phone_page, mirror_with_question):
    m = mirror_with_question
    page = phone_page("light")
    page.goto(f"{m.base}/t/{m.n}")
    toggle = page.locator("#notify-toggle")
    expect(toggle).not_to_be_checked()
    expect(page.locator("#notify-help")).to_contain_text("still shows in Needs you")
    assert _desktop_state(m)["notify"] is False
    toggle.click()
    expect(page.locator("#notify-help")).to_contain_text("buzzes")
    assert _desktop_state(m) == {**_desktop_state(m), "notify": True, "notify_rev": 1}
    page.reload()
    expect(page.locator("#notify-toggle")).to_be_checked()
    page.locator("#notify-toggle").click()
    expect(page.locator("#notify-help")).to_contain_text("still shows in Needs you")
    assert _desktop_state(m)["notify"] is False and _desktop_state(m)["notify_rev"] == 2


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_screenshots_for_the_pr(phone_page, mirror_with_question, scheme):
    """Only with NOTIFY_SHOTS=<dir>: the switch off and on, for the PR's visual check."""
    out = os.environ.get("NOTIFY_SHOTS")
    if not out:
        pytest.skip("set NOTIFY_SHOTS to write screenshots")
    m = mirror_with_question
    page = phone_page(scheme)
    page.goto(f"{m.base}/t/{m.n}")
    page.locator("#notify-toggle").wait_for()
    page.locator("#notify-toggle").scroll_into_view_if_needed()
    page.screenshot(path=f"{out}/4-phone-notify-off-{scheme}.png")
    page.locator("#notify-toggle").click()
    expect(page.locator("#notify-help")).to_contain_text("buzzes")
    page.screenshot(path=f"{out}/4-phone-notify-on-{scheme}.png")
