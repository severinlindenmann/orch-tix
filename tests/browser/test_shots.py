"""Visual check (spec §18): screenshots of every page at 390×844 and 1024×720 for comparing against
the artboards by eye. Skipped unless FS_SHOTS names an output directory, so the suite stays fast."""
import base64
import os
from pathlib import Path

import pytest

from .cli import make_repo, onboard_approved
from .conftest import login_ui

pytestmark = pytest.mark.browser
SHOTS = os.environ.get("FS_SHOTS")
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
TRANSCRIPT = {"text": "Quick update on the deploy. The settings page is live and the backup timer is gone. Next up is "
              "the Windows install test, then a pass over the new file list on the phone.",
              "language": "en", "model": "nova-3", "created_at": "2026-09-24T12:00:00Z", "by": "MacBook"}


def _wav(seconds=2.0, rate=8000) -> bytes:
    import struct
    n = int(seconds * rate)
    data = bytes(128 for _ in range(n))
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate, 1, 8)
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
            + b"data" + struct.pack("<I", len(data)) + data)


@pytest.mark.skipif(not SHOTS, reason="set FS_SHOTS=<dir> to take the screenshots")
@pytest.mark.parametrize("size", [(390, 844), (1024, 720)], ids=["phone", "desktop"])
def test_screenshots(browser, live_server, sim, tmp_path, size):
    out = Path(SHOTS)
    out.mkdir(parents=True, exist_ok=True)
    tag = f"{size[0]}x{size[1]}"
    repo = make_repo(tmp_path / "repo")
    onboard_approved(sim, live_server.url, repo, "work-laptop", "orchestrator")
    sim.upload("invoice-september.pdf", b"%PDF-1.4 fake", ttl="30d")
    done = sim.upload("error-log.txt", b"Traceback: boom\n")
    sim.request("POST", f"/api/files/{done['id']}/ack")
    sim.upload("deploy-plan.md", b"# Deploy plan\n\n1. Ship the redesign\n2. Test Windows\n", note="Read before Friday")
    sim.upload("screenshot-settings-page.png", PNG_1PX, note="does the Settings layout look right?", ttl="1d")
    memo = sim.upload("standup-notes.wav", _wav())
    sim.set_transcript(memo["id"], TRANSCRIPT)

    ctx = browser.new_context(viewport={"width": size[0], "height": size[1]}, device_scale_factor=2)
    page = ctx.new_page()
    login_ui(page, live_server.url, sim.passphrase)
    page.wait_for_load_state("networkidle")
    page.screenshot(path=out / f"files-{tag}.png")

    page.locator(f'.frow[data-id="{memo["id"]}"]').click()
    page.wait_for_load_state("networkidle")
    page.screenshot(path=out / f"fileview-audio-{tag}.png")
    page.locator(f'.frow[data-id="{memo["id"]}"]').evaluate("() => 0")  # keep the page alive
    if size[0] < 900:
        page.go_back()
    png = page.locator('.frow', has_text="screenshot-settings-page.png")
    png.click()
    page.wait_for_load_state("networkidle")
    page.screenshot(path=out / f"fileview-image-{tag}.png")
    if size[0] < 900:
        page.go_back()

    path = tmp_path / "IMG_4821.jpg"
    path.write_bytes(PNG_1PX)
    page.locator("#upload-btn").click()
    page.locator("#upload-input").set_input_files(str(path))
    page.wait_for_timeout(300)
    page.screenshot(path=out / f"sheet-{tag}.png")
    page.keyboard.press("Escape")

    for name in ("devices", "settings"):
        page.goto(live_server.url + "/" + name)
        page.wait_for_load_state("networkidle")
        page.screenshot(path=out / f"{name}-{tag}.png", full_page=True)
    page.goto(live_server.url + "/login")
    page.wait_for_load_state("networkidle")
    page.screenshot(path=out / f"login-{tag}.png")
    ctx.close()
