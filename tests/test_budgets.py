"""Page weight of the phone app (phone v4 keeps the budget): everything the service worker precaches for the
offline shell, and the two phone pages' own scripts."""
import json
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "fileshare" / "static"
SHELL_BUDGET = 1_030_000           # bytes, uncompressed: the whole offline shell (fonts, scripts, styles, icons); was 1_000_000,
                                   # raised by 20 kB for the phone UI round (#71: update prompt, offline cards, settings folds, labels),
                                   # then 10 kB more for the sealed offline ticket cache (#73, ticket-cache.js)
PHONE_JS_BUDGET = 172_000          # bytes, uncompressed: Needs you, Board and ticket modules (+12 kB: the per-ticket notify row (#74) and the offline ticket cache, #73)
# The bridge's device module (R10a, docs/bridge-protocol.md) has its own allowance on top of the shell's, so it can
# neither eat the shell's budget nor grow unnoticed. The owner chose this separate allowance (PR #81, for #78) over
# raising SHELL_BUDGET or keeping the bridge out of the offline shell.
BRIDGE_JS = ["bridge-crypto.js", "bridge-session.js", "bridge-store.js"]
BRIDGE_JS_BUDGET = 30_000          # bytes, uncompressed
# The dashboard frame (R10b, docs/bridge-frame.md) follows the same pattern: its own allowance, outside the shell's.
FRAME_JS = ["bridge-transport.js", "frame-host.js", "frame-render.js", "frame-scope.js", "frame-shim.js"]
FRAME_CSS = ["frame.css"]          # loaded by frame-host.js itself, so app.css (and the shell budget) stay as they were
FRAME_JS_BUDGET = 72_000           # bytes, uncompressed (the scripts)
# The Remote pages' glue (R10 wiring: the ceremony, the workspace list, the transport over the mailbox) is likewise its
# own allowance; only the tiny sign-out wipe (bridge-wipe.js) is loaded by every page and stays in the shell's.
REMOTE_JS = ["remote.js", "remote-mailbox.js", "remote-model.js", "remote-pair.js", "remote-pair-ui.js", "remote-transport.js"]
REMOTE_JS_BUDGET = 32_000          # bytes, uncompressed


def test_the_offline_shell_stays_within_its_budget():
    assets = json.loads((STATIC / "precache.json").read_text())["assets"]
    bridge = {f"/static/js/{n}" for n in BRIDGE_JS + FRAME_JS + REMOTE_JS} | {f"/static/css/{n}" for n in FRAME_CSS}
    total = sum((STATIC / a.removeprefix("/static/")).stat().st_size for a in assets
                if a.startswith("/static/") and a not in bridge)
    assert total <= SHELL_BUDGET, total


def test_the_bridge_module_stays_within_its_own_allowance():
    assets = json.loads((STATIC / "precache.json").read_text())["assets"]
    assert {f"/static/js/{n}" for n in BRIDGE_JS} <= set(assets)
    total = sum((STATIC / "js" / n).stat().st_size for n in BRIDGE_JS)
    assert total <= BRIDGE_JS_BUDGET, total


def test_the_phone_screens_scripts_stay_within_their_budget():
    names = ["needs.js", "ticket.js", "ticket-card.js", "mirror-model.js", "images.js", "textsafe.js", "decision-send.js",
             "phone-inbox.js", "mirrors-data.js", "ticket-cache.js"]
    total = sum((STATIC / "js" / n).stat().st_size for n in names)
    assert total <= PHONE_JS_BUDGET, total


def test_the_dashboard_frame_stays_within_its_own_allowance():
    assets = json.loads((STATIC / "precache.json").read_text())["assets"]
    assert {f"/static/js/{n}" for n in FRAME_JS} <= set(assets)
    total = sum((STATIC / "js" / n).stat().st_size for n in FRAME_JS)
    assert total <= FRAME_JS_BUDGET, total
    assert sum((STATIC / "css" / n).stat().st_size for n in FRAME_CSS) <= 2_000


def test_the_remote_pages_glue_stays_within_its_own_allowance():
    assets = json.loads((STATIC / "precache.json").read_text())["assets"]
    assert {f"/static/js/{n}" for n in REMOTE_JS} <= set(assets)
    total = sum((STATIC / "js" / n).stat().st_size for n in REMOTE_JS)
    assert total <= REMOTE_JS_BUDGET, total
