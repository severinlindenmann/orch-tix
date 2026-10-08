"""Page weight of the phone app (phone v4 keeps the budget): everything the service worker precaches for the
offline shell, and the two phone pages' own scripts."""
import json
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "fileshare" / "static"
SHELL_BUDGET = 1_031_050           # bytes, uncompressed: the whole offline shell (fonts, scripts, styles, icons); was 1_000_000,
                                   # raised by 20 kB for the phone UI round (#71: update prompt, offline cards, settings folds, labels),
                                   # then 10 kB more for the sealed offline ticket cache (#73, ticket-cache.js); then 1_050 for the Factory words, the host-lost sentence and the waiting hint on the status page (R13, #27: +1_111 = workspaces-model.js +920 and workspaces.js +191; 1_031_037 measured)
PHONE_JS_BUDGET = 172_000          # bytes, uncompressed: Needs you, Board and ticket modules (+12 kB: the per-ticket notify row (#74) and the offline ticket cache, #73)
# The bridge's device module (R10a, docs/bridge-protocol.md) has its own allowance on top of the shell's, so it can
# neither eat the shell's budget nor grow unnoticed. The owner chose this separate allowance (PR #81, for #78) over
# raising SHELL_BUDGET or keeping the bridge out of the offline shell.
BRIDGE_JS = ["bridge-crypto.js", "bridge-session.js", "bridge-store.js"]
BRIDGE_JS_BUDGET = 30_600          # bytes, uncompressed; +600 for the stored platform credential id (R11, saveCredential)
# The dashboard frame (R10b, docs/bridge-frame.md) follows the same pattern: its own allowance, outside the shell's.
FRAME_JS = ["bridge-transport.js", "frame-host.js", "frame-render.js", "frame-scope.js", "frame-shim.js"]
FRAME_CSS = ["frame.css"]          # loaded by frame-host.js itself, so app.css (and the shell budget) stay as they were
FRAME_JS_BUDGET = 72_000           # bytes, uncompressed (the scripts)
# The Remote pages' glue (R10 wiring: the ceremony, the workspace list, the transport over the mailbox) is likewise its
# own allowance; the tiny sign-out wipe (bridge-wipe.js) is counted here too.
REMOTE_JS = ["bridge-wipe.js", "remote.js", "remote-lease.js", "remote-mailbox.js", "remote-model.js", "remote-pair.js", "remote-pair-ui.js", "remote-transport.js", "remote-view.js"]
REMOTE_JS_BUDGET = 53_500          # bytes, uncompressed; one allowance for all the Remote work, by increment: #96 streams and viewer (40_000 with main's earlier work), #97 the unlock hooks in remote*.js (+3_684: credential step at pairing, the unlock call and its once-per-exchange rule, declined-stream memory, the not-remote notice), #100 the Factory line and waiting hint on the card (+332), #99 typing from the phone (remote-lease.js, the lease note, the path memory for declined lease sheets: about +9_000); 52_805 measured on the merge
# The unlock sheet and the platform credential (R11, docs/bridge-protocol.md section 9): its own allowance, outside the shell's.
UNLOCK_JS = ["unlock.js"]
UNLOCK_JS_BUDGET = 14_500          # bytes, uncompressed: the sheet, its text checks (padding, markers, limits) and the registration click


def test_the_offline_shell_stays_within_its_budget():
    assets = json.loads((STATIC / "precache.json").read_text())["assets"]
    bridge = {f"/static/js/{n}" for n in BRIDGE_JS + FRAME_JS + REMOTE_JS + UNLOCK_JS} | {f"/static/css/{n}" for n in FRAME_CSS}
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


def test_the_unlock_sheet_stays_within_its_own_allowance():
    assets = json.loads((STATIC / "precache.json").read_text())["assets"]
    assert {f"/static/js/{n}" for n in UNLOCK_JS} <= set(assets)
    total = sum((STATIC / "js" / n).stat().st_size for n in UNLOCK_JS)
    assert total <= UNLOCK_JS_BUDGET, total
