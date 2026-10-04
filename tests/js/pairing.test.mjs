// Task 10: pairing the phone with a desktop (orch-core P0 remote humans, spec §6.4). The check code
// and the MAC are pinned to vectors computed once with orch-core (orch.remote.store.check_code,
// orch.remote.verify.mac_of), so the JS and Python bytes cannot drift apart:
//   uv run python -c "from orch.remote.store import check_code; from orch.remote.verify import mac_of; ..."
import { test } from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import {
  checkCode, forgetPairing, pairingFor, pairings, parsePairFragment, parsePairLink, signDecision, storePairing,
} from "../../fileshare/static/js/pairing.js";

const KEY = new Uint8Array(32);           // all zero: matches the Python test vector below
const FRAG = "#" + "a".repeat(32) + ".ph_0123456789ab." + "A".repeat(43);
// check_code(bytes(32)); mac_of(bytes(32), {"v": 1, "decision_id": "dec_" + "a"*32, "value": "A", "pair": "ph_0123456789ab"})
const CHECK_VECTOR = "778075";
const MAC_VECTOR = "DfL0cb0JO_7X6YPWXGBmEh-s6oX36bfeYoaS1PoHr7A";
// key = bytes(range(32)); a verdict with target.round and non-ASCII text, signed by mac_of
const KEY2 = Uint8Array.from({ length: 32 }, (_, i) => i);
const KEY2_B64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
const CHECK_VECTOR2 = "916718";
const VERDICT = { v: 1, decision_id: "dec_" + "b".repeat(32), kind: "verdict", ticket: "DEMO-0042",
  target: { status: "testing", round: 17 }, value: "done", note: "Grüße ✓", at: "2026-10-02T10:00:00Z" };
const MAC_VECTOR2 = "fk1M2BLngUbhBjAVVwUoe8ysqZ1kKFjIpwc-5rdNwGU";

const hmacKey = (raw) => webcrypto.subtle.importKey("raw", raw, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);

test("the fragment parses strictly", () => {
  const p = parsePairFragment(FRAG);
  assert.equal(p.space, "a".repeat(32));
  assert.equal(p.phoneId, "ph_0123456789ab");
  assert.deepEqual(p.rawKey, KEY);
  assert.equal(parsePairFragment("#x.y.z"), null);
  assert.equal(parsePairFragment(FRAG + "A"), null);
});

test("the fragment refuses every malformed part", () => {
  const sp = "a".repeat(32), ph = "ph_0123456789ab", k = "A".repeat(43);
  assert.ok(parsePairFragment(`${sp}.${ph}.${k}`), "the # is optional");
  for (const bad of [
    `#${"A".repeat(32)}.${ph}.${k}`,          // upper-case hex
    `#${"a".repeat(31)}.${ph}.${k}`,          // short space
    `#${sp}.ph_0123456789aB.${k}`,            // upper-case phone id
    `#${sp}.ph_0123456789a.${k}`,             // short phone id
    `#${sp}.xx_0123456789ab.${k}`,            // wrong prefix
    `#${sp}.${ph}.${"A".repeat(42)}`,         // short key
    `#${sp}.${ph}.${"A".repeat(42)}B`,        // non-canonical base64url (stray low bits)
    `#${sp}.${ph}.${"A".repeat(42)}=`,        // padding
    `#${sp}.${ph}.${"A".repeat(42)}+`,        // standard base64
    `#${sp}.${ph}.${k}.extra`,
    "", null, undefined, 42,
  ]) assert.equal(parsePairFragment(bad), null, String(bad));
  assert.deepEqual(parsePairFragment(`#${sp}.${ph}.${KEY2_B64}`).rawKey, KEY2);
});

test("a pasted pairing link parses only for this server's /pair", () => {
  const origin = "https://tix.example";
  const p = parsePairLink(`  ${origin}/pair${FRAG}\n`, origin);
  assert.equal(p.space, "a".repeat(32));
  assert.equal(parsePairLink(`https://evil.example/pair${FRAG}`, origin), null);
  assert.equal(parsePairLink(`${origin}/p/x${FRAG}`, origin), null);
  assert.equal(parsePairLink(`${origin}/pair`, origin), null);
  assert.equal(parsePairLink("not a link", origin), null);
});

test("check code matches orch-core", async () => {
  // orch.remote.store.check_code(b"\0" * 32)
  assert.equal(await checkCode(KEY), CHECK_VECTOR);
  assert.equal(await checkCode(KEY2), CHECK_VECTOR2);
});

test("MAC matches orch-core mac_of", async () => {
  const key = await hmacKey(KEY);
  const d = await signDecision({ phoneId: "ph_0123456789ab", key }, { v: 1, decision_id: "dec_" + "a".repeat(32), value: "A" });
  assert.equal(d.pair, "ph_0123456789ab");
  assert.equal(d.mac, MAC_VECTOR);
});

test("a signed verdict carries target.round and matches orch-core over non-ASCII text", async () => {
  const d = await signDecision({ phoneId: "ph_0123456789ab", key: await hmacKey(KEY2) }, VERDICT);
  assert.equal(d.target.round, 17);
  assert.equal(d.mac, MAC_VECTOR2);
  assert.equal(d.mac.length, 43);
  assert.ok(!("mac" in VERDICT) && !("pair" in VERDICT), "the input is not changed");
});

test("signing again replaces an old mac instead of signing over it", async () => {
  const key = await hmacKey(KEY);
  const d = await signDecision({ phoneId: "ph_0123456789ab", key },
    { v: 1, decision_id: "dec_" + "a".repeat(32), value: "A", mac: "old", pair: "ph_ffffffffffff" });
  assert.equal(d.mac, MAC_VECTOR);
});

function memoryPairs() {
  const m = new Map();
  return {
    m,
    put: async (rec) => { m.set(rec.space, rec); },
    get: async (space) => m.get(space) ?? null,
    delete: async (space) => { m.delete(space); },
    all: async () => [...m.values()],
  };
}

test("a stored pairing keeps a non-extractable sign-only key and zeroes the raw bytes", async () => {
  const db = memoryPairs();
  const raw = KEY2.slice();
  await storePairing(db, { space: "a".repeat(32), phoneId: "ph_0123456789ab", rawKey: raw, label: "Acme Energy" });
  assert.ok(raw.every((b) => b === 0), "rawKey is zeroed after the import");
  const rec = db.m.get("a".repeat(32));
  assert.deepEqual(Object.keys(rec).sort(), ["key", "label", "paired_at", "phoneId", "space"]);
  assert.equal(rec.key.extractable, false);
  assert.deepEqual(rec.key.usages, ["sign"]);
  assert.equal(rec.key.algorithm.name, "HMAC");
  assert.match(rec.paired_at, /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/);
  await assert.rejects(webcrypto.subtle.exportKey("raw", rec.key));
  const p = await pairingFor(db, "a".repeat(32));
  assert.equal(p.phoneId, "ph_0123456789ab");
  const signed = await signDecision(p, VERDICT);
  assert.equal(signed.mac, MAC_VECTOR2, "the stored key signs like the raw one");
  assert.equal(await pairingFor(db, "b".repeat(32)), null);
  assert.equal((await pairings(db)).length, 1);
  await forgetPairing(db, "a".repeat(32));
  assert.equal(await pairingFor(db, "a".repeat(32)), null);
});

test("storePairing refuses a malformed pairing and stores nothing", async () => {
  const db = memoryPairs();
  await assert.rejects(storePairing(db, { space: "x", phoneId: "ph_0123456789ab", rawKey: new Uint8Array(32), label: "" }));
  await assert.rejects(storePairing(db, { space: "a".repeat(32), phoneId: "ph_0123456789ab", rawKey: new Uint8Array(16), label: "" }));
  assert.equal(db.m.size, 0);
});

test("pairing.js never logs and never sends anything", async () => {
  const { readFileSync } = await import("node:fs");
  for (const f of ["pairing.js", "pair.js"]) {
    const src = readFileSync(new URL(`../../fileshare/static/js/${f}`, import.meta.url), "utf8");
    assert.doesNotMatch(src, /console\./, f);
    assert.doesNotMatch(src, /fetch\(|XMLHttpRequest|sendBeacon|from "\.\/api\.js"/, f);
    assert.doesNotMatch(src, /localStorage|sessionStorage/, f);
  }
});
