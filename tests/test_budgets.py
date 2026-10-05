"""Page weight of the phone app (phone v4 keeps the budget): everything the service worker precaches for the
offline shell, and the two phone pages' own scripts."""
import json
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "fileshare" / "static"
SHELL_BUDGET = 1_020_000           # bytes, uncompressed: the whole offline shell (fonts, scripts, styles, icons); was 1_000_000,
                                   # raised by 20 kB for the phone UI round (#71: update prompt, offline cards, settings folds, labels)
PHONE_JS_BUDGET = 160_000          # bytes, uncompressed: Needs you, Board and ticket modules
# The bridge's device module (R10a, docs/bridge-protocol.md) has its own allowance on top of the shell's, so it can
# neither eat the shell's budget nor grow unnoticed. The owner chose this separate allowance (PR #81, for #78) over
# raising SHELL_BUDGET or keeping the bridge out of the offline shell.
BRIDGE_JS = ["bridge-crypto.js", "bridge-session.js", "bridge-store.js"]
BRIDGE_JS_BUDGET = 30_000          # bytes, uncompressed


def test_the_offline_shell_stays_within_its_budget():
    assets = json.loads((STATIC / "precache.json").read_text())["assets"]
    bridge = {f"/static/js/{n}" for n in BRIDGE_JS}
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
             "phone-inbox.js", "mirrors-data.js"]
    total = sum((STATIC / "js" / n).stat().st_size for n in names)
    assert total <= PHONE_JS_BUDGET, total
