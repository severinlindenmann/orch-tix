// The "seen" rollback mark is keyed by space|doc.id (bound to the row by boundToRow), not by the server's TIX
// number, which a reset server hands out again to another ticket.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// A tiny IndexedDB: one Map per store, just the calls db.js makes.
const stores = {};
const tick = (fn) => queueMicrotask(fn);
globalThis.indexedDB = { open() {
  const req = { result: { objectStoreNames: { contains: () => true }, close() {},
    transaction(name) {
      const map = (stores[name] ||= new Map());
      const tx = { objectStore: () => ({
        get: (k) => { const r = { result: map.get(k) }; tick(() => tx.oncomplete()); return r; },
        put: (v, k) => { map.set(k, v); const r = { result: k }; tick(() => tx.oncomplete()); return r; } }) };
      return tx;
    } } };
  tick(() => req.onsuccess());
  return req;
} };

const { openRow } = await import("../../fileshare/static/js/mirrors-data.js");
const { seenKey } = await import("../../fileshare/static/js/mirror-model.js");

const VEC = JSON.parse(readFileSync(new URL("../vectors/mirror1.json", import.meta.url), "utf8"));
const m = VEC.cases.find((x) => x.name === "mirror");
const mk = await globalThis.crypto.subtle.importKey("raw", Buffer.from(VEC.mk, "hex"), "AES-GCM", false, ["encrypt", "decrypt"]);
const row = { id: "TIX-42", n: 42, uuid: m.ticket_uuid, space: VEC.space_id, wrapped_dek: VEC.wrapped_dek.env, enc_content: m.env };
const higher = { gen: m.obj.gen, mirror_rev: m.obj.mirror_rev + 5 };

test("a mark an old ticket left under the same TIX number does not hide a new ticket", async () => {
  stores.seen = new Map([["TIX-42", { gen: m.obj.gen + 3, mirror_rev: 99 }]]);
  const r = await openRow(mk, row);
  assert.ok(r.doc && !r.rollback);
  assert.deepEqual(stores.seen.get(seenKey(VEC.space_id, m.obj)), { gen: m.obj.gen, mirror_rev: m.obj.mirror_rev });
});

test("a real rollback of the same ticket is still flagged", async () => {
  stores.seen = new Map([[seenKey(VEC.space_id, m.obj), higher]]);
  const r = await openRow(mk, row);
  assert.equal(r.doc, null);
  assert.equal(r.rollback, true);
  assert.deepEqual(stores.seen.get(seenKey(VEC.space_id, m.obj)), higher);
});

test("a row whose space is not the sealed doc's is refused before any mark is read", async () => {
  stores.seen = new Map();
  const r = await openRow(mk, { ...row, space: "x" + VEC.space_id.slice(1) });
  assert.equal(r.doc, null);
  assert.equal(r.error, "binding");
});
