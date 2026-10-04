"""Tags in the web UI (spec §19, Task 34): pills on rows, the multi-select tag filter kept in the
address, Edit tags in the file view, tags in the upload sheet and the Note composer (offline too),
and tags rendered as text only. Every load waits for networkidle."""
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from .conftest import login_ui

pytestmark = pytest.mark.browser

PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1024, "height": 720}
SPY_UPLOADS = """() => {
  window.__metas = [];
  const orig = window.fetch;
  window.fetch = (input, init) => {
    if (String(input).endsWith('/api/files') && init && init.body instanceof FormData) {
      window.__metas.push(JSON.parse(init.body.get('meta')));
    }
    return orig(input, init);
  };
}"""
# Every visible control under 44 px tall (as in test_redesign.py). The row pills are part of the row
# link, not controls of their own; the sr-only file input is opened by its button.
SMALL_CONTROLS = """() => [...document.querySelectorAll('button, a[href], input, select, textarea, summary, [role="menuitem"], [role="option"]')]
  .filter((n) => n.checkVisibility() && !n.closest('[inert]') && !(n.type === 'file' && n.classList.contains('sr-only')))
  .map((n) => [n.outerHTML.slice(0, 90), n.getBoundingClientRect().height])
  .filter(([, h]) => h < 44)"""
NO_SIDE_SCROLL = "Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) <= window.innerWidth"


def ready(page):
    page.wait_for_load_state("networkidle")


def _ctx(browser, live_server, sim, viewport):
    # No service worker here (test_offline.py covers it), so page.route sees every API request.
    ctx = browser.new_context(viewport=viewport, service_workers="block")
    page = ctx.new_page()
    login_ui(page, live_server.url, sim.passphrase)
    ready(page)
    return ctx, page


@pytest.fixture
def desk(browser, live_server, sim):
    ctx, page = _ctx(browser, live_server, sim, DESKTOP)
    yield page
    ctx.close()


@pytest.fixture
def phone(browser, live_server, sim):
    ctx, page = _ctx(browser, live_server, sim, PHONE)
    yield page
    ctx.close()


def row(page, file_id):
    return page.locator(f'#file-list .frow[data-id="{file_id}"]')


def ids(page):
    return page.locator("#file-list .frow").evaluate_all("rs => rs.map((r) => r.dataset.id)")


def url_tags(page):
    return parse_qs(urlparse(page.url).query).get("tag", [])


def server_tags(sim, ref):
    return sim.get_file(ref)["tags"]


# networkidle only helps after a load: once reached, waiting for it again returns at once. After a
# click, wait for what it does instead.
def wait_ids(page, want, ordered=True):
    """Wait until the rows are exactly `want` (in order, or as a set)."""
    want = list(want) if ordered else sorted(want)
    try:
        page.wait_for_function("""([want, ordered]) => {
          const got = [...document.querySelectorAll('#file-list .frow')].map((r) => r.dataset.id);
          if (!ordered) got.sort();
          return JSON.stringify(got) === JSON.stringify(want);
        }""", arg=[want, ordered], timeout=10_000)
    except Exception:
        raise AssertionError(f"rows {ids(page)} != {want}") from None


def wait_query(page, query):
    """Wait until location.search is `?query` ("" for none)."""
    want = f"?{query}" if query else ""
    try:
        page.wait_for_function("(q) => location.search === q", arg=want, timeout=10_000)
    except Exception:
        raise AssertionError(f"{urlparse(page.url).query!r} != {query!r}") from None


# ---- the upload sheet


def test_upload_with_tags_shows_the_pills(desk, sim, tmp_path):
    path = tmp_path / "report.txt"
    path.write_text("numbers")
    desk.evaluate(SPY_UPLOADS)
    desk.get_by_role("button", name="Upload").first.click()
    sheet = desk.locator("dialog.upload-sheet")
    sheet.locator("#upload-input").set_input_files(str(path))
    box = sheet.get_by_label("Tags")
    box.press_sequentially("Weekly Report")
    expect(box).to_have_value("weekly-report")  # normalised as it is typed
    box.press("Enter")
    box.press_sequentially("Notes,")
    expect(box).to_have_value("")
    chips = sheet.locator(".tag-chip-edit .tag-chip-text")
    expect(chips).to_have_text(["notes", "weekly-report"])
    sheet.get_by_role("button", name="Remove tag notes").click()
    expect(chips).to_have_text(["weekly-report"])
    box.press_sequentially("debug_log")  # still typed when sharing: it is added, not lost
    with desk.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/files")) as resp:
        sheet.get_by_role("button", name="Encrypt and share").click()
    assert resp.value.status == 201
    assert desk.evaluate("window.__metas")[0]["tags"] == ["debug-log", "weekly-report"]
    f = resp.value.json()
    assert server_tags(sim, f["id"]) == ["debug-log", "weekly-report"]
    expect(row(desk, f["id"]).locator(".tag-pill")).to_have_text(["debug-log", "weekly-report"])
    # The filter row now knows the new tags, with their counts.
    ready(desk)
    expect(desk.locator("#tag-chips .tag-chip[data-tag='weekly-report']")).to_contain_text("1")


def test_an_invalid_tag_shows_an_inline_error_and_nothing_is_sent(desk, tmp_path):
    path = tmp_path / "x.txt"
    path.write_text("x")
    desk.evaluate(SPY_UPLOADS)
    desk.get_by_role("button", name="Upload").first.click()
    sheet = desk.locator("dialog.upload-sheet")
    sheet.locator("#upload-input").set_input_files(str(path))
    box = sheet.get_by_label("Tags")
    box.press_sequentially("a/b")
    error = sheet.locator(".tag-error")
    expect(error).to_be_visible()
    expect(error).to_contain_text("isn't a valid tag")
    expect(box).to_have_attribute("aria-invalid", "true")
    box.press("Enter")
    expect(sheet.locator(".tag-chip-edit")).to_have_count(0)
    sheet.get_by_role("button", name="Encrypt and share").click()
    expect(sheet).to_be_visible()  # refused: the sheet stays open with the error
    assert desk.evaluate("window.__metas") == []
    box.fill("")
    box.press_sequentially("-")
    expect(error).to_be_visible()
    box.press("Backspace")
    expect(error).to_be_hidden()


def test_suggestions_come_from_the_tags_in_use_and_work_by_keyboard(desk, sim, tmp_path):
    sim.upload("old.txt", b"o", tags=["weekly-report", "notes"])
    sim.upload("old2.txt", b"o", tags=["weekly-report"])
    desk.reload()
    ready(desk)
    path = tmp_path / "new.txt"
    path.write_text("n")
    desk.get_by_role("button", name="Upload").first.click()
    sheet = desk.locator("dialog.upload-sheet")
    sheet.locator("#upload-input").set_input_files(str(path))
    box = sheet.get_by_label("Tags")
    box.press_sequentially("wee")
    options = sheet.get_by_role("option")
    expect(options).to_have_count(1)
    expect(options.first).to_contain_text("weekly-report")
    expect(box).to_have_attribute("aria-expanded", "true")
    box.press("ArrowDown")
    expect(options.first).to_have_attribute("aria-selected", "true")
    box.press("Enter")
    expect(sheet.locator(".tag-chip-edit .tag-chip-text")).to_have_text(["weekly-report"])
    box.press("ArrowDown")  # an empty box suggests the rest, by count
    expect(options).to_have_text([re.compile("^notes")])
    box.press("Escape")  # closes the list, not the sheet
    expect(sheet.get_by_role("listbox")).to_be_hidden()
    expect(sheet).to_be_visible()


# ---- the filter


def _seed_ab(sim):
    both = [sim.upload(f"both{i}.txt", b"x", tags=["a", "b"]) for i in range(5)]
    only_a = [sim.upload(f"a{i}.txt", b"x", tags=["a"]) for i in range(2)]
    only_b = sim.upload("b.txt", b"x", tags=["b"])
    none = sim.upload("plain.txt", b"x")
    return both, only_a, only_b, none


def test_filter_by_two_tags_is_and_server_side_with_load_more(desk, live_server, sim):
    both, only_a, only_b, none = _seed_ab(sim)
    lists = []

    def small_pages(route):  # pages of 2, so "Load more" is needed
        lists.append(route.request.url)
        route.continue_(url=route.request.url.replace("limit=50", "limit=2"))

    desk.route(re.compile(r"/api/files\?"), small_pages)
    desk.reload()
    ready(desk)
    chips = desk.locator("#tag-chips")
    expect(chips.locator(".tag-chip")).to_have_count(2)
    chips.locator(".tag-chip[data-tag='a']").click()
    wait_query(desk, "tag=a")
    chips.locator(".tag-chip[data-tag='b']").click()
    wait_query(desk, "tag=a&tag=b")
    expect(chips.locator(".tag-chip[aria-pressed='true']")).to_have_count(2)
    expect(chips.locator(".tag-chip[aria-pressed='true'] svg")).to_have_count(2)  # filled, with ×
    want = [f["id"] for f in reversed(both)]
    wait_ids(desk, want[:2])
    desk.locator("#load-more").click()
    wait_ids(desk, want[:4])
    desk.locator("#load-more").click()
    wait_ids(desk, want)
    expect(desk.locator("#load-more")).to_be_hidden()
    tagged = [u for u in lists if "tag=" in u]
    assert tagged and all(parse_qs(urlparse(u).query)["tag"] == ["a", "b"] for u in tagged[-3:])
    # It combines with search (client-side, over what is listed).
    desk.locator("#search").fill("both3")
    expect(desk.locator("#file-list .frow")).to_have_count(1)
    desk.locator("#search").fill("")
    # A pressed chip unpresses: only "a" is left.
    chips.locator(".tag-chip[data-tag='b']").click()
    wait_query(desk, "tag=a")
    want_a = [f["id"] for f in reversed(both + only_a)]
    for n in (2, 4, 6):
        wait_ids(desk, want_a[:n])
        desk.locator("#load-more").click()
    wait_ids(desk, want_a)


def test_the_filter_combines_with_done(desk, sim):
    a1 = sim.upload("a1.txt", b"x", tags=["a"])
    a2 = sim.upload("a2.txt", b"x", tags=["a"])
    sim.request("POST", f"/api/files/{a1['id']}/ack")
    desk.goto(desk.url.split("?")[0] + "?tag=a")
    ready(desk)
    wait_ids(desk, [a2["id"]])
    desk.locator("#acked-chip").click()
    wait_ids(desk, [a2["id"], a1["id"]])
    assert url_tags(desk) == ["a"]


def test_no_match_under_a_tag_filter_is_not_the_empty_state(desk, sim):
    sim.upload("x.txt", b"x", tags=["a"])
    desk.goto(desk.url.split("?")[0] + "?tag=zzz")
    ready(desk)
    expect(desk.locator("#no-match")).to_be_visible()
    expect(desk.locator("#empty")).to_be_hidden()
    expect(desk.locator("#tag-chips .tag-chip[aria-pressed='true']")).to_have_text(["zzz"])


def test_the_url_state_survives_a_reload(desk, live_server, sim):
    both, only_a, _, _ = _seed_ab(sim)
    desk.goto(f"{live_server.url}/?tag=B&tag=%3Cimg%3E&tag=a&tag=a")
    ready(desk)
    assert url_tags(desk) == ["a", "b"]  # normalised; the invalid one is dropped
    assert set(ids(desk)) == {f["id"] for f in both}
    desk.reload()
    ready(desk)
    assert url_tags(desk) == ["a", "b"]
    expect(desk.locator("#tag-chips .tag-chip[aria-pressed='true']")).to_have_count(2)
    assert set(ids(desk)) == {f["id"] for f in both}
    # Opening a file keeps the filter in the address, through a reload too.
    row(desk, both[0]["id"]).click()
    q = parse_qs(urlparse(desk.url).query)
    assert q["f"] == [both[0]["id"]] and q["tag"] == ["a", "b"]
    desk.reload()
    ready(desk)
    expect(desk.locator("#detail .detail-name")).to_have_text("both0.txt")
    assert set(ids(desk)) == {f["id"] for f in both}
    # Closing the file keeps it too.
    desk.keyboard.press("Escape")
    assert urlparse(desk.url).query == "tag=a&tag=b"


def test_back_and_forward_keep_the_filter_on_a_phone(phone, live_server, sim):
    both, only_a, _, _ = _seed_ab(sim)
    phone.reload()
    ready(phone)
    phone.locator("#tag-chips .tag-chip[data-tag='b']").click()
    wait_query(phone, "tag=b")
    phone.locator("#tag-chips .tag-chip[data-tag='a']").click()
    wait_query(phone, "tag=a&tag=b")
    wait_ids(phone, [f["id"] for f in both], ordered=False)
    row(phone, both[1]["id"]).click()
    expect(phone.locator("#detail.is-open")).to_be_visible()
    assert url_tags(phone) == ["a", "b"]
    phone.go_back()
    expect(phone.locator("#detail.is-open")).to_have_count(0)
    assert urlparse(phone.url).query == "tag=a&tag=b"
    expect(phone.locator("#tag-chips .tag-chip[aria-pressed='true']")).to_have_count(2)
    wait_ids(phone, [f["id"] for f in both], ordered=False)
    phone.go_forward()
    expect(phone.locator("#detail.is-open .detail-name")).to_have_text("both1.txt")


def test_a_pill_filters_by_its_tag_from_a_row_and_from_the_file_view(phone, sim):
    f = sim.upload("r.txt", b"x", tags=["alpha", "beta"])
    other = sim.upload("o.txt", b"x", tags=["beta"])
    phone.reload()
    ready(phone)
    row(phone, f["id"]).locator(".tag-pill[data-tag='alpha']").click()
    wait_query(phone, "tag=alpha")
    expect(phone.locator("#detail.is-open")).to_have_count(0)  # the pill filtered; it didn't open the file
    wait_ids(phone, [f["id"]])
    row(phone, f["id"]).click()
    view = phone.locator("#detail.is-open")
    view.locator(".detail-tags").get_by_role("button", name="beta", exact=True).click()
    wait_query(phone, "tag=beta")  # back to the list (no ?f=), filtered by beta
    expect(phone.locator("#detail.is-open")).to_have_count(0)
    wait_ids(phone, [other["id"], f["id"]])


def test_more_opens_a_searchable_list_of_every_tag(desk, sim):
    sim.upload("ten.txt", b"x", tags=[f"t{i:02d}" for i in range(10)])
    five = sim.upload("five.txt", b"x", tags=[f"u{i}" for i in range(4)] + ["t00"])
    desk.reload()
    ready(desk)
    chips = desk.locator("#tag-chips")
    expect(chips.locator(".tag-chip")).to_have_count(12)
    expect(chips.locator(".tag-chip").first).to_have_attribute("data-tag", "t00")  # by count first
    chips.get_by_role("button", name="More…").click()
    sheet = desk.locator("dialog.tag-sheet")
    expect(sheet.locator(".tag-chip")).to_have_count(14)
    sheet.get_by_label("Search tags").fill("u")
    expect(sheet.locator(".tag-chip")).to_have_text([re.compile(f"^u{i}") for i in range(4)])
    sheet.locator(".tag-chip[data-tag='u3']").click()
    expect(sheet.locator(".tag-chip[data-tag='u3']")).to_have_attribute("aria-pressed", "true")
    sheet.get_by_role("button", name="Done").click()
    expect(sheet).to_have_count(0)
    wait_query(desk, "tag=u3")
    expect(chips.locator(".tag-chip[aria-pressed='true']")).to_have_text(["u3"])
    wait_ids(desk, [five["id"]])


# ---- the file view


def test_edit_tags_in_the_file_view_updates_the_row_and_the_counts(desk, sim):
    f = sim.upload("doc.txt", b"x", tags=["draft", "keep"])
    desk.reload()
    ready(desk)
    row(desk, f["id"]).click()
    pane = desk.locator("#detail")
    tags = pane.locator(".detail-tags")
    expect(tags.locator(".tag-chip-name")).to_have_text(["draft", "keep"])
    tags.get_by_role("button", name="Edit tags").click()
    box = tags.get_by_label(f"Tags of {f['id']}")
    expect(box).to_be_focused()
    box.press_sequentially("Final")
    box.press("Enter")
    tags.get_by_role("button", name="Remove tag draft").click()
    with desk.expect_response(lambda r: r.request.method == "PUT" and r.url.endswith(f"/api/files/{f['id']}/tags")) as resp:
        tags.get_by_role("button", name="Save tags").click()
    assert resp.value.status == 200
    assert json.loads(resp.value.request.post_data) == {"tags": ["final", "keep"]}
    expect(tags.locator(".tag-chip-name")).to_have_text(["final", "keep"])
    expect(row(desk, f["id"]).locator(".tag-pill")).to_have_text(["final", "keep"])
    assert server_tags(sim, f["id"]) == ["final", "keep"]
    chips = desk.locator("#tag-chips")
    expect(chips.locator(".tag-chip[data-tag='final']")).to_be_visible()
    expect(chips.locator(".tag-chip[data-tag='draft']")).to_have_count(0)
    # × on a chip in the view removes that tag at once.
    with desk.expect_response(lambda r: r.request.method == "PUT"):
        tags.get_by_role("button", name="Remove tag keep").click()
    expect(tags.locator(".tag-chip-name")).to_have_text(["final"])
    expect(row(desk, f["id"]).locator(".tag-pill")).to_have_text(["final"])
    assert server_tags(sim, f["id"]) == ["final"]


def test_back_and_forward_across_a_pill_deep_link_restore_each_entrys_filter(phone, sim):
    # alpha-filtered list -> open f (its own entry, ?f=&tag=alpha) -> tap "beta" in the view: that
    # steps back and re-filters the list's entry to beta. Forward then lands on an entry whose tags
    # (alpha) differ from the page's (beta), which only syncTagsFromUrl can restore.
    f = sim.upload("r.txt", b"x", tags=["alpha", "beta"])
    other = sim.upload("o.txt", b"x", tags=["beta"])
    phone.reload()
    ready(phone)
    phone.locator("#tag-chips .tag-chip[data-tag='alpha']").click()
    wait_ids(phone, [f["id"]])
    row(phone, f["id"]).click()
    phone.locator("#detail.is-open .detail-tags").get_by_role("button", name="beta", exact=True).click()
    wait_query(phone, "tag=beta")
    wait_ids(phone, [other["id"], f["id"]])
    phone.go_forward()
    expect(phone.locator("#detail.is-open .detail-name")).to_have_text("r.txt")
    assert parse_qs(urlparse(phone.url).query) == {"f": [f["id"]], "tag": ["alpha"]}
    expect(phone.locator("#tag-chips .tag-chip[aria-pressed='true']")).to_have_text(["alpha"])
    wait_ids(phone, [f["id"]])
    phone.go_back()
    expect(phone.locator("#detail.is-open")).to_have_count(0)
    wait_query(phone, "tag=beta")
    expect(phone.locator("#tag-chips .tag-chip[aria-pressed='true']")).to_have_text(["beta"])
    wait_ids(phone, [other["id"], f["id"]])


# Holds the first PUT of tags back for a while, so a second tap lands while it is in flight.
SLOW_FIRST_PUT = """() => {
  const orig = window.fetch;
  let first = true;
  window.fetch = async (input, init) => {
    if (init && init.method === "PUT" && String(input).endsWith("/tags") && first) {
      first = false;
      await new Promise((r) => setTimeout(r, 800));
    }
    return orig(input, init);
  };
}"""


def test_two_quick_removals_both_stick(desk, sim):
    f = sim.upload("doc.txt", b"x", tags=["one", "two", "keep"])
    desk.reload()
    ready(desk)
    row(desk, f["id"]).click()
    tags = desk.locator("#detail .detail-tags")
    expect(tags.locator(".tag-chip-name")).to_have_text(["keep", "one", "two"])
    desk.evaluate(SLOW_FIRST_PUT)
    tags.get_by_role("button", name="Remove tag one").click()
    expect(tags.get_by_role("button", name="Remove tag two")).to_be_disabled()  # the first save is in flight
    tags.get_by_role("button", name="Remove tag two").click()  # taps as soon as it can
    expect(tags.locator(".tag-chip-name")).to_have_text(["keep"])
    expect(tags.get_by_role("button", name="Remove tag keep")).to_be_enabled()
    assert server_tags(sim, f["id"]) == ["keep"]
    desk.reload()
    ready(desk)
    expect(row(desk, f["id"]).locator(".tag-pill")).to_have_text(["keep"])


def test_saving_the_tags_of_a_file_that_is_gone_says_so(desk, sim):
    f = sim.upload("doc.txt", b"x", tags=["keep"])
    desk.reload()
    ready(desk)
    row(desk, f["id"]).click()
    tags = desk.locator("#detail .detail-tags")
    tags.get_by_role("button", name="Edit tags").click()
    tags.get_by_label(f"Tags of {f['id']}").press_sequentially("more")
    desk.route(re.compile(r"/api/files/[^/]+/tags$"), lambda route: route.fulfill(
        status=404, content_type="application/json", body=json.dumps({"error": "not_found", "detail": "gone"})))
    tags.get_by_role("button", name="Save tags").click()
    expect(desk.locator(".toast-error").last).to_have_text(f"{f['id']} no longer exists")
    expect(tags.get_by_role("button", name="Save tags")).to_be_enabled()  # the editor stays, usable
    expect(row(desk, f["id"]).locator(".tag-pill")).to_have_text(["keep"])
    assert server_tags(sim, f["id"]) == ["keep"]


def test_a_rejected_save_shows_the_error_and_keeps_the_editor(desk, sim):
    f = sim.upload("doc.txt", b"x", tags=["keep"])
    desk.reload()
    ready(desk)
    row(desk, f["id"]).click()
    tags = desk.locator("#detail .detail-tags")
    tags.get_by_role("button", name="Edit tags").click()
    desk.route(re.compile(r"/api/files/[^/]+/tags$"), lambda route: route.fulfill(
        status=400, content_type="application/json", body=json.dumps({"error": "bad_tag", "detail": "no, thanks"})))
    tags.get_by_role("button", name="Save tags").click()
    expect(tags.locator(".tag-error")).to_have_text("no, thanks")
    expect(tags.get_by_role("button", name="Save tags")).to_be_visible()
    assert server_tags(sim, f["id"]) == ["keep"]


def test_rows_show_two_pills_on_a_phone_and_three_on_a_desktop(browser, live_server, sim):
    f = sim.upload("many.txt", b"x", tags=["a1", "a2", "a3", "a4", "a5"])
    for viewport, shown in ((PHONE, ["a1", "a2", "+3"]), (DESKTOP, ["a1", "a2", "a3", "+2"])):
        ctx, page = _ctx(browser, live_server, sim, viewport)
        pills = row(page, f["id"]).locator(".tag-pill:visible")
        expect(pills).to_have_text(shown)
        ctx.close()


# ---- untrusted strings


def test_a_hostile_tag_cannot_exist_and_pills_render_as_text(desk, live_server, sim):
    hostile = '<img src=x onerror="window.__pwned=1">'
    f = sim.upload("x.txt", b"x", tags=["fine"])
    with pytest.raises(AssertionError, match='-> 400 .*"bad_tag"'):
        sim.upload("h.txt", b"x", tags=[hostile])
    r = sim.request("PUT", f"/api/files/{f['id']}/tags", json={"tags": [hostile]})
    assert r.status_code == 400 and r.json()["error"] == "bad_tag"
    assert server_tags(sim, f["id"]) == ["fine"]
    assert [t["tag"] for t in sim.request("GET", "/api/tags").json()["tags"]] == ["fine"]

    # Even if a hostile string reached the page, it would be text: rewrite the answers to carry one.
    def files(route):
        body = route.fetch().json()
        for x in body["files"]:
            x["tags"] = [hostile]
        route.fulfill(json=body)

    desk.route(re.compile(r"/api/files\?"), files)
    desk.route(re.compile(r"/api/tags$"), lambda route: route.fulfill(json={"tags": [{"tag": hostile, "count": 1}]}))
    desk.reload()
    ready(desk)
    pill = row(desk, f["id"]).locator(".tag-pill")
    expect(pill).to_have_text(hostile)
    expect(desk.locator("#tag-chips .tag-chip")).to_have_text([re.compile(re.escape(hostile))])
    row(desk, f["id"]).click()
    expect(desk.locator("#detail .tag-chip-name")).to_have_text(hostile)
    assert desk.locator("#file-list img, #tag-chips img, #detail .detail-tags img").count() == 0
    assert desk.evaluate("window.__pwned") is None


# ---- phones: no sideways scroll, 44 px targets


def test_at_390_nothing_scrolls_sideways_and_every_target_is_44px(phone, sim, tmp_path):
    long = "a" + "x" * 39  # sorts first among the tags used once, so it is in the top 12
    f = sim.upload("long-name-" + "y" * 60 + ".txt", b"x", tags=[long, "weekly-report", "notes", "more"])
    for i in range(13):
        sim.upload(f"f{i}.txt", b"x", tags=[f"tag-{i:02d}-" + "z" * 30])
    phone.reload()
    ready(phone)
    phone.locator(f"#tag-chips .tag-chip[data-tag='{long}']").click()
    wait_ids(phone, [f["id"]])
    assert phone.evaluate(NO_SIDE_SCROLL), "files, filtered"
    assert phone.evaluate(SMALL_CONTROLS) == [], "files, filtered"
    phone.locator(f"#tag-chips .tag-chip[data-tag='{long}']").click()
    wait_query(phone, "")
    phone.locator("#tag-chips").get_by_role("button", name="More…").click()
    assert phone.evaluate(NO_SIDE_SCROLL), "more sheet"
    assert phone.evaluate(SMALL_CONTROLS) == [], "more sheet"
    phone.locator("dialog.tag-sheet").get_by_role("button", name="Done").click()

    row(phone, f["id"]).click()
    view = phone.locator("#detail.is-open")
    expect(view.locator(".tag-chip-name")).to_have_count(4)
    assert phone.evaluate(NO_SIDE_SCROLL), "file view"
    assert phone.evaluate(SMALL_CONTROLS) == [], "file view"
    view.get_by_role("button", name="Edit tags").click()
    view.get_by_label(f"Tags of {f['id']}").press("ArrowDown")
    expect(view.get_by_role("option").first).to_be_visible()
    assert phone.evaluate(NO_SIDE_SCROLL), "file view, editing"
    assert phone.evaluate(SMALL_CONTROLS) == [], "file view, editing"
    for name in ("Remove tag notes", "Edit tags"):
        loc = view.get_by_role("button", name=name)
        if loc.count():
            assert loc.first.bounding_box()["width"] >= 44, name
    phone.keyboard.press("Escape")  # closes the suggestions (they lie over the buttons below)
    expect(view.get_by_role("listbox")).to_be_hidden()
    view.get_by_role("button", name="Cancel").click()
    phone.go_back()
    expect(phone.locator("#detail.is-open")).to_have_count(0)

    path = tmp_path / "x.txt"
    path.write_text("x")
    phone.locator("#dock").get_by_role("button", name="Upload").click()
    sheet = phone.locator("dialog.upload-sheet")
    sheet.locator("#upload-input").set_input_files(str(path))
    box = sheet.get_by_label("Tags")
    box.press_sequentially(long)
    box.press("Enter")
    box.press_sequentially("tag")
    expect(sheet.get_by_role("option").first).to_be_visible()
    assert phone.evaluate(NO_SIDE_SCROLL), "upload sheet"
    assert phone.evaluate(SMALL_CONTROLS) == [], "upload sheet"
    assert sheet.get_by_role("button", name=f"Remove tag {long}").bounding_box()["width"] >= 44
    phone.keyboard.press("Escape")
    phone.keyboard.press("Escape")
    phone.locator("#note-btn").click()
    note = phone.locator("dialog.note-sheet")
    note.get_by_label("Tags").press_sequentially("notes")
    assert phone.evaluate(NO_SIDE_SCROLL), "note sheet"
    assert phone.evaluate(SMALL_CONTROLS) == [], "note sheet"


# ---- the Note composer, offline: the queued request carries the tags


def test_a_note_with_tags_queued_offline_keeps_its_tags(browser, browser_name, live_server, sim):
    if browser_name != "chromium":
        pytest.skip("service worker offline emulation is Chromium-only here")
    from .test_offline import controlled, go_offline, outbox_records

    ctx = browser.new_context(service_workers="allow")
    try:
        page = ctx.new_page()
        controlled(page, live_server.url, sim)
        go_offline(page)
        page.locator("#note-btn").click()
        sheet = page.locator("dialog.note-sheet")
        sheet.locator("#note-name").fill("standup.md")
        sheet.locator("#note-text").fill("the offline standup")
        box = sheet.get_by_label("Tags")
        box.press_sequentially("Meeting Notes")
        box.press("Enter")
        box.press_sequentially("q3")  # still typed on Save: added
        sheet.get_by_role("button", name="Save").click()
        expect(sheet).to_have_count(0)
        expect(page.locator("#outbox-slot .frow-pending")).to_have_count(1)
        [rec] = outbox_records(page)
        assert set(rec) == {"seq", "state", "uuid", "key_version", "wrapped_dek", "enc_meta", "ttl", "blob", "size",
                            "created_at", "kind", "tags"}
        assert rec["tags"] == ["meeting-notes", "q3"] and rec["kind"] == "note"
        page.context.set_offline(False)
        expect(page.locator("#outbox-slot")).to_be_hidden(timeout=20_000)
        row_ = page.locator("#file-list .frow").filter(has_text="standup.md")
        expect(row_).to_have_count(1, timeout=20_000)
        expect(row_.locator(".tag-pill")).to_have_text(["meeting-notes", "q3"])
        [f] = [x for x in sim.request("GET", "/api/files").json()["files"] if x["tags"]]
        assert f["tags"] == ["meeting-notes", "q3"]
        page.wait_for_load_state("networkidle")
    finally:
        ctx.close()
