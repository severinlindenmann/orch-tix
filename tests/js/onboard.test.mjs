import { test } from "node:test";
import assert from "node:assert/strict";
import { buildCode, buildCommands, CODE_RE, defaultOs, nextState } from "../../fileshare/static/js/onboard-cmd.js";

test("buildCode is shr1.<22-char lookup> and matches the installer's regex", () => {
  assert.equal(buildCode(new Uint8Array(16)), "shr1." + "A".repeat(22));
  assert.ok(CODE_RE.test(buildCode(crypto.getRandomValues(new Uint8Array(16)))));
  assert.throws(() => buildCode(new Uint8Array(32)), TypeError);
  assert.equal(CODE_RE.test("shr1." + "A".repeat(22) + "." + "B".repeat(43)), false);
});

test("buildCommands produces the exact POSIX and Windows commands", () => {
  const c = buildCommands("https://tix.severin.io", "shr1.L");
  assert.equal(c.posix, "curl --proto '=https' --tlsv1.2 -fsSL https://tix.severin.io/onboarding.txt | bash -s -- 'shr1.L'");
  assert.equal(c.posixInspect, "curl --proto '=https' --tlsv1.2 -fsSL https://tix.severin.io/onboarding.txt -o onboarding.sh && less onboarding.sh && bash onboarding.sh 'shr1.L'");
  assert.equal(c.windows, "& ([scriptblock]::Create((irm https://tix.severin.io/onboarding.ps1))) 'shr1.L'");
  // The default Restricted execution policy blocks `.\onboarding.ps1`; -File with a process-scoped Bypass doesn't.
  assert.equal(c.windowsInspect, "irm https://tix.severin.io/onboarding.ps1 -OutFile onboarding.ps1; notepad onboarding.ps1; powershell -NoProfile -ExecutionPolicy Bypass -File .\\onboarding.ps1 'shr1.L'");
});

test("defaultOs picks the Windows tab only for Windows user agents", () => {
  assert.equal(defaultOs("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"), "windows");
  assert.equal(defaultOs("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5)"), "posix");
  assert.equal(defaultOs("Mozilla/5.0 (X11; Linux x86_64)"), "posix");
  assert.equal(defaultOs(""), "posix");
});

const T0 = Date.parse("2026-09-24T12:00:00Z");
const waiting = { phase: "waiting", expiresAt: T0 + 15 * 60_000, device: null };
const dev = (status) => ({ id: "dev_1", name: "pc", project: "p", status });

test("nextState: waiting stays waiting until expiry, then expires on a tick or a 404", () => {
  assert.equal(nextState(waiting, undefined, T0 + 60_000).phase, "waiting");
  assert.equal(nextState(waiting, { used_at: null, device: null }, T0 + 60_000).phase, "waiting");
  assert.equal(nextState(waiting, undefined, T0 + 15 * 60_000).phase, "expired");
  assert.equal(nextState(waiting, null, T0).phase, "expired");
});

test("nextState: a device moves the modal to pending, approved or rejected", () => {
  const p = nextState(waiting, { used_at: "x", device: dev("pending") }, T0);
  assert.equal(p.phase, "pending");
  assert.equal(p.device.id, "dev_1");
  // a pending device does not expire with the (already used) code
  assert.equal(nextState(p, undefined, T0 + 60 * 60_000).phase, "pending");
  assert.equal(nextState(p, { used_at: "x", device: dev("active") }, T0).phase, "approved");
  assert.equal(nextState(p, { used_at: "x", device: dev("revoked") }, T0).phase, "rejected");
});

test("nextState: a used token without a device (the server dropped it) ends the wait", () => {
  assert.equal(nextState(waiting, { used_at: "x", device: null }, T0).phase, "expired");
});

test("nextState: terminal phases are sticky", () => {
  for (const phase of ["approved", "rejected", "expired"]) {
    const s = { ...waiting, phase };
    assert.equal(nextState(s, { used_at: "x", device: dev("pending") }, T0), s);
    assert.equal(nextState(s, null, T0), s);
  }
});
