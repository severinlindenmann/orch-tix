// The sealed local cache: offline tickets (bounded, LRU, rollback-safe) and the sealed key -> number map (#14, #73).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// A tiny IndexedDB: one Map per store, with the calls db.js makes.
const stores = {};
const tick = (fn) => queueMicrotask(fn);
globalThis.indexedDB = { open() {
  const req = { result: { objectStoreNames: { contains: () => true }, close() {},
    transaction(name) {
      const map = (stores[name] ||= new Map());
      const op = (tx, result, mutate) => { const r = { result }; tick(() => { mutate?.(); r.result = typeof result === "function" ? result() : result; tx.oncomplete(); }); return r; };
      const tx = { objectStore: () => ({
        get: (k) => op(tx, map.get(k)),
        getAll: () => op(tx, () => [...map.values()]),
        put: (v, k) => op(tx, k, () => map.set(k, structuredClone(v))),
        delete: (k) => op(tx, undefined, () => map.delete(k)),
        clear: () => op(tx, undefined, () => map.clear()) }) };
      return tx;
    } } };
  tick(() => req.onsuccess());
  return req;
} };

const cache = await import("../../fileshare/static/js/ticket-cache.js");
const { openRow } = await import("../../fileshare/static/js/mirrors-data.js");
const { clearLists } = await import("../../fileshare/static/js/db.js");
const { seenKey } = await import("../../fileshare/static/js/mirror-model.js");

const VEC = JSON.parse(readFileSync(new URL("../vectors/mirror1.json", import.meta.url), "utf8"));
const m = VEC.cases.find((x) => x.name === "mirror");
const key = (hex) => globalThis.crypto.subtle.importKey("raw", Buffer.from(hex, "hex"), "AES-GCM", false, ["encrypt", "decrypt"]);
const mk = await key(VEC.mk);
const row = { id: "TIX-42", n: 42, uuid: m.ticket_uuid, space: VEC.space_id, wrapped_dek: VEC.wrapped_dek.env, enc_content: m.env, status: "waiting" };
const reset = () => { for (const k of Object.keys(stores)) delete stores[k]; };

test("a remembered ticket comes back exactly as the server sent it, sealed", async () => {
  reset();
  await cache.rememberTicket(42, row);
  const hit = await cache.cachedTicket(42);
  assert.deepEqual(hit.row, row);
  assert.ok(Math.abs(hit.at - Date.now()) < 5000);
  // nothing opened is stored: no title, no key, no DEK
  const text = JSON.stringify([...stores.tickets.values()]);
  assert.ok(!text.includes(m.obj.id) && !text.includes(m.obj.title ?? "\u0000"));
  assert.deepEqual(Object.keys(stores.tickets.get("42")).sort(), ["at", "n", "row", "size", "used"]);
});

test("a row that is not the ticket asked for, or malformed, is neither stored nor served", async () => {
  reset();
  await cache.rememberTicket(7, row);                       // row.n is 42
  await cache.rememberTicket(42, { ...row, enc_content: 5 });
  assert.equal(stores.tickets?.size ?? 0, 0);
  stores.tickets = new Map([["42", { n: 41, row, at: Date.now(), used: 1, size: 1 }]]);
  assert.equal(await cache.cachedTicket(42), null);
  assert.equal(await cache.cachedTicket(99), null);
});

test("an opened cached row goes through the same checks: opens, and never moves the seen mark", async () => {
  reset();
  await cache.rememberTicket(42, row);
  const hit = await cache.cachedTicket(42);
  const r = await openRow(mk, hit.row, { cached: true });
  assert.ok(r.doc && r.dek);
  assert.equal(stores.seen?.size ?? 0, 0);
});

test("a cached copy older than the seen mark is refused, not shown", async () => {
  reset();
  await cache.rememberTicket(42, row);
  stores.seen = new Map([[seenKey(VEC.space_id, m.obj), { gen: m.obj.gen, mirror_rev: m.obj.mirror_rev + 5 }]]);
  assert.equal(await openRow(mk, (await cache.cachedTicket(42)).row, { cached: true }), null);
});

test("a cached row bound to another space or opened with another key is refused", async () => {
  reset();
  const bad = await openRow(mk, { ...row, space: "x" + VEC.space_id.slice(1) }, { cached: true });
  assert.equal(bad.doc, null);
  const other = await key("11".repeat(32));
  assert.equal((await openRow(other, row, { cached: true })).error, "integrity");
});

test("evictions drop the least recently used first, never the one just written, until under both bounds", () => {
  const e = (n, used, size = 10) => ({ n, used, size });
  assert.deepEqual(cache.evictions([e(1, 5), e(2, 1), e(3, 3)], { maxTickets: 2 }), [2]);
  assert.deepEqual(cache.evictions([e(1, 5), e(2, 1), e(3, 3)], { maxTickets: 2, keep: 2 }), [3]);
  assert.deepEqual(cache.evictions([e(1, 1, 60), e(2, 2, 60), e(3, 3, 60)], { maxBytes: 130 }), [1]);
  assert.deepEqual(cache.evictions([e(1, 1, 60), e(2, 2, 60), e(3, 3, 60)], { maxBytes: 70 }), [1, 2]);
  assert.deepEqual(cache.evictions([e(1, 1)], {}), []);
});

test("the store stays at MAX_TICKETS and a ticket opened offline survives longer", async () => {
  reset();
  const base = Date.now();
  for (let n = 1; n <= cache.MAX_TICKETS; n++) {
    stores.tickets ||= new Map();
    stores.tickets.set(String(n), { n, row: { ...row, n }, at: base, used: base + n, size: 100 });
  }
  const oldest = stores.tickets.get("1");
  stores.tickets.set("1", { ...oldest, used: base + 10_000 });        // opened again: now the newest
  await cache.rememberTicket(42, row);
  assert.equal(stores.tickets.size, cache.MAX_TICKETS);
  assert.ok(stores.tickets.has("1") && stores.tickets.has("42") && !stores.tickets.has("2"));
});

test("a ticket the server says is gone is forgotten", async () => {
  reset();
  await cache.rememberTicket(42, row);
  await cache.forgetTicket(42);
  assert.equal(await cache.cachedTicket(42), null);
});

test("sign-out (clearLists) empties the offline tickets and the sealed lists with them", async () => {
  reset();
  await cache.rememberTicket(42, row);
  await cache.saveKeyMap(mk, new Map([["DEMO-1", 1]]));
  stores.lists.set("mirrors", { body: { mirrors: [] }, at: 1 });
  await clearLists();
  assert.equal(stores.tickets.size, 0);
  assert.equal(stores.lists.size, 0);
});

test("the key map is sealed: no key in the store, opens only with the same MK", async () => {
  reset();
  const map = new Map([["DEMO-0042", 42], ["DEMO-0007", 7]]);
  await cache.saveKeyMap(mk, map);
  assert.ok(!JSON.stringify([...stores.lists.values()]).includes("DEMO-00"));
  assert.deepEqual(Object.fromEntries(await cache.loadKeyMap(mk)), Object.fromEntries(map));
  assert.equal(await cache.loadKeyMap(await key("22".repeat(32))), null);
  const e = stores.lists.get("keymap");
  stores.lists.set("keymap", { ...e, enc: e.enc.slice(0, -4) + (e.enc.endsWith("AAAA") ? "BBBB" : "AAAA") });
  assert.equal(await cache.loadKeyMap(mk), null);
  stores.lists.delete("keymap");
  assert.equal(await cache.loadKeyMap(mk), null);
});

test("keyMapOf maps upper-cased local keys to numbers and skips rows without a doc", () => {
  const map = cache.keyMapOf([{ n: 1, doc: { id: "demo-1" } }, { n: 2, doc: null }, { n: "3", doc: { id: "X-1" } }, { n: 4, doc: { id: "X-4" } }]);
  assert.deepEqual([...map], [["DEMO-1", 1], ["X-4", 4]]);
});

test("offlineText says when it was last updated, with the date when not today", () => {
  const at = new Date(2026, 9, 5, 14, 5).getTime();
  assert.equal(cache.offlineText(at, new Date(2026, 9, 5, 20, 0).getTime()), "Offline · last updated 14:05");
  assert.equal(cache.offlineText(at, new Date(2026, 9, 7, 9, 0).getTime()), "Offline · last updated 05.10 14:05");
});
