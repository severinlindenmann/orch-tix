import pytest

pytestmark = pytest.mark.browser


def test_chromium_parses_the_manifest_and_sees_no_installability_errors(page, live_server, browser_name):
    if browser_name != "chromium":
        pytest.skip("CDP is Chromium-only")
    page.goto(live_server.url + "/setup")
    cdp = page.context.new_cdp_session(page)
    m = cdp.send("Page.getAppManifest")
    assert m["url"].endswith("/manifest.webmanifest")
    assert m["errors"] == []
    errors = cdp.send("Page.getInstallabilityErrors")["installabilityErrors"]
    assert errors == [], errors
