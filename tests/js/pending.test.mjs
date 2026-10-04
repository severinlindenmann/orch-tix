import { test } from "node:test";
import assert from "node:assert/strict";
import { pendingSummary, withLocalFingerprints } from "../../fileshare/static/js/pending.js";
import { b64u, fingerprint, unb64u } from "../../fileshare/static/js/crypto.js";

const dev = (id, status, name = id, project = "p", fingerprint = "ABCD-EFGH") =>
  ({ id, status, name, project, fingerprint });

test("no pending devices -> hidden", () => {
  assert.deepEqual(pendingSummary([dev("a", "active"), dev("b", "revoked")]), { count: 0, badge: "", text: "" });
});

test("one pending device names it and its fingerprint", () => {
  const s = pendingSummary([dev("a", "active"), dev("b", "pending", "thinkpad-t14", "acme-etl", "7KQ2-M9XD")]);
  assert.deepEqual(s, { count: 1, badge: "1 pending",
    text: "thinkpad-t14 · acme-etl is waiting for approval — fingerprint 7KQ2-M9XD" });
});

test("several pending devices are counted", () => {
  const s = pendingSummary([dev("a", "pending"), dev("b", "pending"), dev("c", "pending")]);
  assert.equal(s.count, 3);
  assert.equal(s.badge, "3 pending");
  assert.equal(s.text, "3 devices are waiting for approval");
});

// R12: the banner shows the fingerprint computed here from pubkey, never the server's field alone.
async function newPub() {
  const kp = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  return b64u(new Uint8Array(await crypto.subtle.exportKey("raw", kp.publicKey)));
}

test("withLocalFingerprints recomputes each pending fingerprint and flags a server mismatch", async () => {
  const pub = await newPub();
  const local = await fingerprint(unb64u(pub));
  const [ok, bad] = await withLocalFingerprints([
    { id: "a", status: "pending", name: "a", project: "p", pubkey: pub, fingerprint: local },
    { id: "b", status: "pending", name: "b", project: "p", pubkey: pub, fingerprint: "AAAA-AAAA" },
  ]);
  assert.equal(ok.fingerprint, local);
  assert.equal(ok.mismatch, false);
  assert.equal(bad.fingerprint, local);
  assert.equal(bad.mismatch, true);
  // the untouched DeviceOut rides along, so approveDevice can still compare against the server's field
  assert.equal(bad.server.fingerprint, "AAAA-AAAA");
});

test("withLocalFingerprints marks an unparseable pubkey as a mismatch", async () => {
  const [d] = await withLocalFingerprints([
    { id: "a", status: "pending", name: "a", project: "p", pubkey: "!!", fingerprint: "ABCD-EFGH" },
  ]);
  assert.equal(d.fingerprint, "—");
  assert.equal(d.mismatch, true);
});

test("a single mismatching device is not shown with a fingerprint to compare", () => {
  const s = pendingSummary([{ ...dev("b", "pending", "evil", "p", "7KQ2-M9XD"), mismatch: true }]);
  assert.equal(s.text, "evil · p is waiting for approval — fingerprint mismatch, do not approve");
});
