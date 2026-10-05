// A ticket the phone already has opens from the last-known list when there is no network (QA T08).
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

const { cachedRow, openRow } = await import("../../fileshare/static/js/mirrors-data.js");
const { seenKey } = await import("../../fileshare/static/js/mirror-model.js");

const VEC = JSON.parse(readFileSync(new URL("../vectors/mirror1.json", import.meta.url), "utf8"));
const m = VEC.cases.find((x) => x.name === "mirror");
const mk = await globalThis.crypto.subtle.importKey("raw", Buffer.from(VEC.mk, "hex"), "AES-GCM", false, ["encrypt", "decrypt"]);
const row = { id: "TIX-42", n: 42, uuid: m.ticket_uuid, space: VEC.space_id, wrapped_dek: VEC.wrapped_dek.env, enc_content: m.env };
const higher = { gen: m.obj.gen, mirror_rev: m.obj.mirror_rev + 5 };

test("cachedRow opens the ticket from the stored list and says when the copy is from", async () => {
  stores.seen = new Map();
  stores.lists = new Map([["mirrors", { body: { mirrors: [{ ...row, id: "TIX-7", n: 7 }, row] }, at: 1234 }]]);
  const c = await cachedRow(mk, 42);
  assert.equal(c.at, 1234);
  assert.ok(c.row.doc);
  assert.equal(c.row.n, 42);
  assert.equal(stores.seen.size, 0, "a cached copy never moves the rollback mark");
});

test("cachedRow is null without a stored list, without that ticket, or for a copy older than the newest opened", async () => {
  stores.seen = new Map();
  stores.lists = new Map();
  assert.equal(await cachedRow(mk, 42), null);
  stores.lists = new Map([["mirrors", { body: { mirrors: [row] }, at: 1 }]]);
  assert.equal(await cachedRow(mk, 99), null);
  stores.seen = new Map([[seenKey(VEC.space_id, m.obj), higher]]);
  assert.equal(await cachedRow(mk, 42), null);
});
