import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { encodeRecovery, decodeRecovery } from "../../fileshare/static/js/recovery.js";
import { hexToBytes, bytesToHex } from "../../fileshare/static/js/crypto.js";

const VEC = JSON.parse(readFileSync(new URL("../vectors/shr1.json", import.meta.url), "utf8"));

for (const v of VEC.recovery) {
  test(`recovery vector ${v.mk.slice(0, 8)}`, async () => {
    const s = await encodeRecovery(hexToBytes(v.mk));
    assert.equal(s, v.recovery);
    assert.match(s, /^shrk(-[A-Z2-7]{5}){11}$/);
    assert.equal(bytesToHex(await decodeRecovery(s)), v.mk);
  });
}

test("decode is case-insensitive and ignores spaces and dashes", async () => {
  const v = VEC.recovery[0];
  const messy = "  " + v.recovery.toLowerCase().replace(/-/g, " - ") + "\n";
  assert.equal(bytesToHex(await decodeRecovery(messy)), v.mk);
  const noPrefix = v.recovery.slice(5).replace(/-/g, "");
  assert.equal(bytesToHex(await decodeRecovery(noPrefix)), v.mk);
});

test("decode rejects a typo (checksum)", async () => {
  const v = VEC.recovery[0].recovery;
  const i = v.length - 3;
  const typo = v.slice(0, i) + (v[i] === "A" ? "B" : "A") + v.slice(i + 1);
  await assert.rejects(decodeRecovery(typo), /checksum|invalid/);
});

test("decode rejects wrong length and bad alphabet", async () => {
  await assert.rejects(decodeRecovery("shrk-AAAAA"), /invalid/);
  await assert.rejects(decodeRecovery("shrk-" + "1".repeat(55)), /invalid/);
});

test("encode rejects non-32-byte input", async () => {
  await assert.rejects(encodeRecovery(new Uint8Array(31)), /32 bytes/);
});
