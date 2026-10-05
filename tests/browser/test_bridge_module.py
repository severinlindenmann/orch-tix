"""The bridge's device module (R10a, docs/bridge-protocol.md) in a real browser, on a page of this app under its own
Content-Security-Policy: it loads with `script-src 'self'` and nothing else, every vector passes through it in
Chromium's WebCrypto, and its keys and sequence counter behave in real IndexedDB, two tabs at once included.

The shared conformance runner (tests/js/support/bridge-conformance.mjs) is test code, not part of the app, so it is
handed to the page by request interception at a same-origin path; the production modules come from the live server."""
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

ROOT = Path(__file__).resolve().parents[1]
RUNNER = (ROOT / "js" / "support" / "bridge-conformance.mjs").read_text(encoding="utf-8")
VECTORS = json.loads((ROOT / "bridge_vectors.json").read_text(encoding="utf-8"))
RUNNER_PATH = "/__bridge_test__/conformance.mjs"

LOAD = """async (vec) => {
  const violations = [];
  document.addEventListener("securitypolicyviolation", (e) => violations.push(e.violatedDirective + " " + e.blockedURI));
  const B = await import("/static/js/bridge-crypto.js"), C = await import("/static/js/crypto.js");
  const S = await import("/static/js/bridge-store.js"), Sess = await import("/static/js/bridge-session.js");
  const T = await import("%s");
  const counts = await T.conformance(B, C, vec), own = await T.ownChecks(B, C, vec);
  const k = await S.deviceKey();
  let exported = [];
  for (const f of ["pkcs8", "jwk", "raw"]) { try { await crypto.subtle.exportKey(f, k.privateKey); exported.push(f); } catch {} }
  const before = [...violations];
  let blocked = false;                              // control: the policy is really enforced on this page
  try { await import("data:text/javascript,export default 1"); } catch { blocked = true; }
  await new Promise((r) => setTimeout(r, 50));
  return { counts, own, extractable: k.privateKey.extractable, usages: k.privateKey.usages, exported,
           pub: C.bytesToHex(k.pub), session: typeof Sess.DeviceSession, violations: before, blocked,
           controlReported: violations.length > before.length };
}""" % RUNNER_PATH

SETUP = """async (vec) => {
  const B = await import("/static/js/bridge-crypto.js"), C = await import("/static/js/crypto.js");
  const S = await import("/static/js/bridge-store.js");
  await S.saveWorkspaceKey(vec.keys.workspace, await B.workspaceKey(C.hexToBytes(vec.keys.mk), vec.keys.workspace), 1);
}"""
START = """async (ws) => {
  const S = await import("/static/js/bridge-store.js");
  const c = S.sequenceCounter(ws);
  window.__seqs = Promise.all(Array.from({ length: 150 }, () => c.next()));
}"""
STATE = """async (ws) => {
  const S = await import("/static/js/bridge-store.js"), C = await import("/static/js/crypto.js");
  const rec = await S.workspaceRecord(ws), k = await S.deviceKey();
  return { next: rec.next, kWsExtractable: rec.kWs.extractable, pub: C.bytesToHex(k.pub) };
}"""


@pytest.fixture
def bridge_page(context, live_server):
    def serve_runner(route):
        route.fulfill(status=200, content_type="text/javascript", body=RUNNER)

    context.route("**" + RUNNER_PATH, serve_runner)

    def make():
        page = context.new_page()
        resp = page.goto(live_server.url + "/login")
        csp = resp.headers["content-security-policy"]
        assert "script-src 'self';" in csp and "unsafe" not in csp
        return page
    return make


def test_the_module_loads_under_the_app_policy_and_passes_every_vector(bridge_page):
    page = bridge_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    r = page.evaluate(LOAD, VECTORS)
    assert r["violations"] == []
    assert r["blocked"] and r["controlReported"]
    assert [e for e in errors if "data:text/javascript" not in e] == []
    host_steps = sum(len(c.get("steps", [])) for c in VECTORS["host_cases"])
    assert r["counts"] == {"hkdf": 3, "seal": 1, "sign": 8, "sig_scalars": 7, "ids": 3, "pairing": 1,
                           "host_cases": len(VECTORS["host_cases"]), "host_steps": host_steps,
                           "device_cases": len(VECTORS["device_cases"]), "shown": len(VECTORS["shown"]),
                           "assertion_cases": len(VECTORS["assertion"]["cases"]), "assertion_challenges": 3}
    assert r["own"] == 19
    assert r["extractable"] is False and r["exported"] == [] and sorted(r["usages"]) == ["sign"]
    assert r["session"] == "function"


def test_two_tabs_never_share_a_sequence_number_and_the_key_survives_a_reload(bridge_page):
    ws = VECTORS["keys"]["workspace"]
    a, b = bridge_page(), bridge_page()
    a.evaluate(SETUP, VECTORS)
    before = a.evaluate(STATE, ws)
    a.evaluate(START, ws)
    b.evaluate(START, ws)
    got = a.evaluate("() => window.__seqs") + b.evaluate("() => window.__seqs")
    assert sorted(got) == list(range(1, 301))
    a.reload()
    after = a.evaluate(STATE, ws)
    assert after == {"next": 301, "kWsExtractable": False, "pub": before["pub"]}
    a.evaluate(SETUP, VECTORS)                      # re-deriving K_ws never resets the counter
    assert a.evaluate(STATE, ws)["next"] == 301
