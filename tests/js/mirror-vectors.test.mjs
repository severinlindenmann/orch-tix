// A4 Task 5: tests/vectors/mirror1.json, made by Python (tests/vectors/make_vectors.py). Here JS composes
// every AAD from the prefixes on its own, and crypto.js seal + canonicalJson must give the same bytes.
// Task 9's mirror-crypto.test.mjs adds the module-level helpers on top of this file.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import * as c from "../../fileshare/static/js/crypto.js";

const VEC = JSON.parse(readFileSync(new URL("../vectors/mirror1.json", import.meta.url), "utf8"));
const te = new TextEncoder();
const hex = c.hexToBytes;
const cat = (...parts) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let i = 0;
  for (const p of parts) { out.set(p, i); i += p.length; }
  return out;
};
const byName = Object.fromEntries(VEC.cases.map((x) => [x.name, x]));
const P = {
  space: te.encode("sharing/space/v1|"), mirror: te.encode("sharing/mirror/v1|"),
  decision: te.encode("sharing/decision/v1|"), msg: te.encode("sharing/msg/v1|"),
};

test("AAD prefixes match the exact bytes", () => {
  for (const [k, v] of Object.entries(P)) assert.equal(new TextDecoder().decode(v), VEC.aad_prefixes[k]);
});

test("JS composes the same AADs", () => {
  const s = byName["space-label"], m = byName.mirror, dt = byName["decision-ticket"],
    ds = byName["decision-space"], g = byName.msg;
  assert.equal(c.bytesToHex(cat(P.space, te.encode(s.space_id))), s.aad);
  assert.equal(c.bytesToHex(cat(P.mirror, hex(m.ticket_uuid))), m.aad);
  assert.equal(c.bytesToHex(cat(P.decision, hex(dt.scope), te.encode("|"), hex(dt.decision_uuid))), dt.aad);
  assert.equal(c.bytesToHex(cat(P.decision, te.encode(VEC.space_id), te.encode("|"), hex(ds.decision_uuid))), ds.aad);
  assert.equal(c.bytesToHex(cat(P.msg, hex(g.msg_uuid))), g.aad);
  assert.equal(VEC.wrapped_dek.aad, c.bytesToHex(cat(te.encode("sharing/tdek/v1|"), hex(m.ticket_uuid))));
});

test("mirror uuids are sha256(space|key|gen)[:16] and event uuids add |rev", () => {
  for (const u of VEC.uuids) {
    const h = (s) => createHash("sha256").update(s, "utf8").digest("hex").slice(0, 32);
    assert.equal(h(`${u.space_id}|${u.key}|${u.gen}`), u.mirror_uuid);
    assert.equal(h(`${u.space_id}|${u.key}|${u.gen}|${u.rev}`), u.mirror_event_uuid);
  }
});

for (const v of VEC.cases) {
  test(`vector ${v.name}: canonicalJson + seal reproduce the Python envelope, and open it`, async () => {
    assert.equal(c.bytesToHex(te.encode(c.canonicalJson(v.obj))), v.pt);
    const key = await c.importAesKey(hex(v.key));
    const env = await c.seal(key, hex(v.pt), hex(v.aad), hex(v.nonce));
    assert.equal(c.b64u(env), v.env);
    const pt = await c.open(key, c.unb64u(v.env), hex(v.aad));
    assert.deepEqual(JSON.parse(new TextDecoder().decode(pt)), v.obj);
  });
}

test("the wrapped DEK opens under MK with the ticket-DEK AAD", async () => {
  const mk = await c.importAesKey(hex(VEC.mk));
  const dek = await c.open(mk, c.unb64u(VEC.wrapped_dek.env), hex(VEC.wrapped_dek.aad));
  assert.equal(c.bytesToHex(dek), VEC.dek);
});
