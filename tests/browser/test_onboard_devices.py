import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

from .cli import make_repo, onboard_approved, run_cli, start_cli

pytestmark = pytest.mark.browser
CODE = re.compile(r"'(shr1\.[A-Za-z0-9_-]{22})'")


@pytest.fixture
def spawn():
    procs = []

    def _spawn(*a):
        p = start_cli(*a)
        procs.append(p)
        return p

    yield _spawn
    for p in procs:
        p.kill()


def generate(page):
    page.locator("#onboard-btn").click()
    dlg = page.locator("dialog.onboard")
    expect(dlg).to_be_visible()
    dlg.get_by_role("button", name="Generate").click()
    cmd = dlg.locator("code.onboard-cmd").first
    expect(cmd).to_contain_text("bash -s -- 'shr1.")
    return dlg, CODE.search(cmd.text_content()).group(1)


def config(repo):
    return repo / ".claude" / "skills" / "sharing" / "config.json"


def test_onboard_approve_then_revoke(ui_page, live_server, tmp_path, spawn):
    dlg, code = generate(ui_page)
    expect(dlg.locator(".onboard-countdown")).to_have_text(re.compile(r"^1[45]:\d\d$"))
    repo = make_repo(tmp_path / "r1")
    proc = spawn(repo, live_server.url, "_handshake", "--code", code, "--device", "pw-device", "--project", "pw-proj")
    fp = proc.wait_for_fingerprint()

    expect(dlg.locator(".onboard-fp")).to_have_text(fp, timeout=10_000)
    expect(dlg.locator(".onboard-device")).to_contain_text("pw-device · pw-proj")
    assert dlg.locator("code.onboard-cmd").count() == 0
    dlg.get_by_role("button", name="Approve").click()
    confirm = ui_page.locator("dialog.confirm[open]")
    expect(confirm).to_contain_text(f"Does the terminal show {fp}?")
    in_flight = dlg.locator(".onboard-pending .sheet-actions button").all()  # Reject and Approve
    assert len(in_flight) == 2
    for b in in_flight:
        expect(b).to_be_disabled()
    confirm.get_by_role("button", name="Approve").click()
    expect(dlg.locator(".onboard-status")).to_have_text("Approved: pw-device · pw-proj ✓", timeout=10_000)
    assert proc.wait(60) == 0, proc.output
    assert run_cli(repo, live_server.url, "whoami").returncode == 0

    dlg.get_by_role("button", name="Close").click()
    ui_page.goto(live_server.url + "/settings")
    row = ui_page.locator(".device-row", has_text="pw-device")
    expect(row).to_contain_text("pw-proj")
    expect(row).to_contain_text("active")
    row.get_by_role("button", name="Revoke").click()
    confirm = ui_page.locator("dialog.confirm[open]")
    expect(confirm).to_contain_text("Revoke pw-device?")
    confirm.get_by_role("button", name="Revoke").click()
    expect(ui_page.locator(".devices-revoked")).to_contain_text("pw-device")
    expect(ui_page.locator(".device-row", has_text="pw-device").get_by_role("button", name="Revoke")).to_have_count(0)
    assert run_cli(repo, live_server.url, "whoami").returncode == 3


def test_reject_from_modal(ui_page, live_server, tmp_path, spawn):
    dlg, code = generate(ui_page)
    repo = make_repo(tmp_path / "r2")
    proc = spawn(repo, live_server.url, "_handshake", "--code", code, "--device", "stranger", "--project", "p")
    fp = proc.wait_for_fingerprint()
    expect(dlg.locator(".onboard-fp")).to_have_text(fp, timeout=10_000)
    dlg.get_by_role("button", name="Reject").click()
    ui_page.locator("dialog.confirm[open]").get_by_role("button", name="Reject").click()
    expect(dlg.locator(".onboard-status")).to_contain_text("Rejected: stranger", timeout=10_000)
    assert proc.wait(60) == 3, proc.output
    assert not config(repo).exists()


def test_approve_from_devices_page_after_closing_modal(ui_page, live_server, tmp_path, spawn):
    dlg, code = generate(ui_page)
    repo = make_repo(tmp_path / "r3")
    proc = spawn(repo, live_server.url, "_handshake", "--code", code, "--device", "later", "--project", "p")
    fp = proc.wait_for_fingerprint()
    expect(dlg.locator(".onboard-fp")).to_have_text(fp, timeout=10_000)
    dlg.get_by_role("button", name="Close").click()
    expect(ui_page.locator("dialog.onboard")).to_have_count(0)

    ui_page.goto(live_server.url + "/settings")
    pending = ui_page.locator("section.devices-pending .device-row", has_text="later")
    expect(pending.locator(".device-fp")).to_have_text(fp)
    pending.get_by_role("button", name="Approve").click()
    ui_page.locator("dialog.confirm[open]").get_by_role("button", name="Approve").click()
    assert proc.wait(60) == 0, proc.output
    expect(ui_page.locator("section.devices-pending")).to_have_count(0)
    expect(ui_page.locator(".device-row", has_text="later")).to_contain_text("active")
    assert run_cli(repo, live_server.url, "whoami").returncode == 0


def test_cancel_revoke_keeps_device(ui_page, live_server, sim, tmp_path):
    repo = make_repo(tmp_path / "r4")
    onboard_approved(sim, live_server.url, repo, "keep-me", "p")
    ui_page.goto(live_server.url + "/settings")
    ui_page.locator(".device-row", has_text="keep-me").get_by_role("button", name="Revoke").click()
    ui_page.locator("dialog.confirm[open]").get_by_role("button", name="Cancel").click()
    assert run_cli(repo, live_server.url, "whoami").returncode == 0


def test_closing_waiting_modal_burns_the_code(ui_page, live_server, tmp_path):
    dlg, code = generate(ui_page)
    with ui_page.expect_response(lambda r: "/api/onboarding-tokens/" in r.url and r.request.method == "DELETE"):
        dlg.get_by_role("button", name="Close").click()
    expect(ui_page.locator("dialog.onboard")).to_have_count(0)
    repo = make_repo(tmp_path / "r5")
    r = run_cli(repo, live_server.url, "_handshake", "--code", code, "--device", "late", "--project", "p", "--no-wait")
    assert r.returncode != 0
    assert not config(repo).exists()


def test_os_tabs_show_both_commands(ui_page):
    dlg, code = generate(ui_page)
    dlg.get_by_role("tab", name="Windows").click()
    visible = dlg.locator("code.onboard-cmd:visible").first
    expect(visible).to_have_text(f"& ([scriptblock]::Create((irm {ui_page.url.split('/files')[0]}/onboarding.ps1))) '{code}'")
    expect(dlg.get_by_role("tab", name="Windows")).to_have_attribute("aria-selected", "true")
    dlg.get_by_role("tab", name="macOS / Linux").click()
    expect(dlg.locator("code.onboard-cmd:visible").first).to_contain_text(f"bash -s -- '{code}'")


def test_link_expires_after_15_minutes(ui_page, live_server):
    ui_page.clock.install()
    ui_page.goto(live_server.url + "/files")
    dlg, _ = generate(ui_page)
    ui_page.clock.fast_forward("15:01")
    expect(dlg.locator(".onboard-status")).to_have_text("Link expired")
    assert dlg.locator("code.onboard-cmd").count() == 0
    dlg.get_by_role("button", name="Generate new link").click()
    expect(dlg.locator("code.onboard-cmd").first).to_contain_text("bash -s -- 'shr1.")


def test_server_fingerprint_mismatch_blocks_approval(ui_page, live_server, sim, tmp_path):
    code, _ = sim.onboarding_code()
    repo = make_repo(tmp_path / "r6")
    assert run_cli(repo, live_server.url, "_handshake", "--code", code, "--device", "mitm",
                   "--project", "p", "--no-wait").returncode == 0
    db = sqlite3.connect(live_server.data_dir / "fileshare.db")
    with db:
        db.execute("UPDATE devices SET fingerprint = 'AAAA-AAAA' WHERE name = 'mitm'")
    db.close()
    ui_page.goto(live_server.url + "/settings")
    row = ui_page.locator("section.devices-pending .device-row", has_text="mitm")
    expect(row.locator(".fp-mismatch")).to_contain_text("Fingerprint mismatch")
    expect(row.get_by_role("button", name="Approve")).to_have_count(0)
    expect(row.get_by_role("button", name="Reject")).to_have_count(1)


def test_markup_in_a_device_name_is_refused_by_the_server(sim, tmp_path, live_server):
    code, _ = sim.onboarding_code()
    repo = make_repo(tmp_path / "r7")
    r = run_cli(repo, live_server.url, "_handshake", "--code", code,
                "--device", "<img src=x onerror=alert(1)>", "--project", "p", "--no-wait")
    assert r.returncode != 0  # NAME_RE allows only [A-Za-z0-9._-]
    assert not config(repo).exists()


def test_devices_page_escapes_names_even_if_validation_is_bypassed(ui_page, live_server):
    evil = "<img src=x onerror=window.__pwned=1>"
    pub = "B" + "A" * 86  # base64url of 0x04 followed by 64 zero bytes: display only, never approved
    db = sqlite3.connect(live_server.data_dir / "fileshare.db")
    with db:
        for dev_id, approved in (("dev_0000000000ee", "2026-09-24T12:00:00Z"), ("dev_0000000000ef", None)):
            db.execute(
                "INSERT INTO devices (id, name, project, hostname, platform, token_hash, pubkey, fingerprint,"
                " created_at, approved_at) VALUES (?, ?, ?, ?, 'linux', ?, ?, 'ABCD-EFGH', '2026-09-24T12:00:00Z', ?)",
                (dev_id, evil, evil, evil, "x" + dev_id, pub, approved),
            )
    db.close()
    ui_page.goto(live_server.url + "/settings")
    expect(ui_page.locator(".device-row", has_text=evil)).to_have_count(2)
    assert ui_page.evaluate("window.__pwned") is None


def test_waiting_device_shows_banner_and_badge_on_files_page(ui_page, live_server, sim, tmp_path):
    code, _ = sim.onboarding_code()
    repo = make_repo(tmp_path / "r-banner")
    r = run_cli(repo, live_server.url, "_handshake", "--code", code, "--device", "banner-dev",
                "--project", "p", "--no-wait")
    assert r.returncode == 0, r.stdout + r.stderr
    fp = re.search(r"[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}", r.stdout + r.stderr).group(0)
    ui_page.goto(live_server.url + "/files")
    banner = ui_page.locator("#pending-banner")
    expect(banner).to_be_visible(timeout=12_000)
    expect(banner).to_contain_text(f"banner-dev · p is waiting for approval — fingerprint {fp}")
    badge = ui_page.locator("#devices-badge")
    expect(badge).to_have_text("1")  # the tab bar / sidebar show a bare count
    expect(badge).to_have_attribute("title", "1 pending")
    banner.get_by_role("link", name="Review").click()
    expect(ui_page).to_have_url(re.compile(r"/settings#devices$"))


def test_countdown_uses_the_local_clock_even_when_it_is_skewed(ui_page, live_server):
    # R9: a browser clock an hour ahead of the server must not expire the link on the spot.
    ui_page.clock.install(time=datetime.now(timezone.utc) + timedelta(hours=1))
    ui_page.goto(live_server.url + "/files")
    dlg, _ = generate(ui_page)
    expect(dlg.locator(".onboard-countdown")).to_have_text(re.compile(r"^1[45]:\d\d$"))
    expect(dlg.locator(".onboard-status")).to_have_text("Waiting for the device…")


def _tamper_fingerprint(live_server, name):
    db = sqlite3.connect(live_server.data_dir / "fileshare.db")
    with db:
        db.execute("UPDATE devices SET fingerprint = 'AAAA-AAAA' WHERE name = ?", (name,))
    db.close()


def test_banner_never_shows_a_server_fingerprint_that_does_not_match(ui_page, live_server, sim, tmp_path):
    # R12: every fingerprint shown is recomputed from pubkey; a mismatch is called out, not displayed.
    code, _ = sim.onboarding_code()
    repo = make_repo(tmp_path / "r-mm")
    assert run_cli(repo, live_server.url, "_handshake", "--code", code, "--device", "mm-dev",
                   "--project", "p", "--no-wait").returncode == 0
    _tamper_fingerprint(live_server, "mm-dev")
    ui_page.goto(live_server.url + "/files")
    banner = ui_page.locator("#pending-banner")
    expect(banner).to_contain_text("mm-dev · p is waiting for approval — fingerprint mismatch, do not approve",
                                   timeout=12_000)
    expect(banner).not_to_contain_text("AAAA-AAAA")


def test_modal_blocks_approval_when_the_server_fingerprint_differs(ui_page, live_server, tmp_path, spawn):
    dlg, code = generate(ui_page)
    # Hold the modal's polls (they fail as network errors and are retried) until the row is tampered,
    # so the first device it sees carries the tampered fingerprint.
    token_poll = re.compile(r".*/api/onboarding-tokens/[^/]+$")
    ui_page.route(token_poll, lambda route: route.abort())
    repo = make_repo(tmp_path / "r-mm2")
    proc = spawn(repo, live_server.url, "_handshake", "--code", code, "--device", "mm2", "--project", "p")
    fp = proc.wait_for_fingerprint()
    _tamper_fingerprint(live_server, "mm2")
    ui_page.unroute(token_poll)
    expect(dlg.locator(".fp-mismatch")).to_contain_text("Fingerprint mismatch", timeout=10_000)
    expect(dlg.get_by_role("button", name="Approve")).to_have_count(0)
    expect(dlg.locator(".onboard-fp")).not_to_have_text("AAAA-AAAA")
    assert fp != "AAAA-AAAA"


def test_active_rows_show_the_locally_computed_fingerprint(ui_page, live_server, sim, tmp_path):
    repo = make_repo(tmp_path / "r-act")
    onboard_approved(sim, live_server.url, repo, "act-dev", "p")
    _tamper_fingerprint(live_server, "act-dev")
    ui_page.goto(live_server.url + "/settings")
    row = ui_page.locator(".device-row", has_text="act-dev")
    expect(row.locator(".device-fp")).not_to_have_text("AAAA-AAAA")
    expect(row.locator(".fp-mismatch")).to_contain_text("Fingerprint mismatch")


def pending_device(sim, live_server, tmp_path, name):
    code, _ = sim.onboarding_code()
    repo = make_repo(tmp_path / name)
    assert run_cli(repo, live_server.url, "_handshake", "--code", code, "--device", name,
                   "--project", "p", "--no-wait").returncode == 0
    return repo


def test_double_click_approve_sends_one_approve_post(ui_page, live_server, sim, tmp_path):
    pending_device(sim, live_server, tmp_path, "twice")
    posts = []
    ui_page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.endswith("/approve") else None)
    ui_page.goto(live_server.url + "/settings")
    row = ui_page.locator("section.devices-pending .device-row", has_text="twice")
    # Two clicks before the first confirm opens (a real double-click can also land on its backdrop).
    row.get_by_role("button", name="Approve").evaluate("(b) => { b.click(); b.click(); }")
    confirm = ui_page.locator("dialog.confirm[open]")
    expect(confirm.first).to_be_visible()
    ui_page.wait_for_timeout(300)  # a second approveDevice would have opened its own confirm by now
    while confirm.count():
        confirm.last.get_by_role("button", name="Approve").click()  # the topmost modal
    expect(ui_page.locator(".device-row", has_text="twice")).to_contain_text("active")
    ui_page.wait_for_timeout(300)
    assert len(posts) == 1, posts


def test_cancelled_confirm_re_enables_approve_and_reject(ui_page, live_server, sim, tmp_path):
    pending_device(sim, live_server, tmp_path, "undecided")
    ui_page.goto(live_server.url + "/settings")
    row = ui_page.locator("section.devices-pending .device-row", has_text="undecided")
    approve = row.get_by_role("button", name="Approve")
    reject = row.get_by_role("button", name="Reject")
    approve.click()
    expect(approve).to_be_disabled()
    expect(reject).to_be_disabled()
    ui_page.locator("dialog.confirm[open]").get_by_role("button", name="Cancel").click()
    expect(approve).to_be_enabled()
    expect(reject).to_be_enabled()
