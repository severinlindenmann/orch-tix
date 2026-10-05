// A warm open asks /api/mirrors/changes?after=<stored cursor> instead of downloading the whole list (T09).
import { test } from "node:test";
import assert from "node:assert/strict";

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
globalThis.window = { dispatchEvent() {} };
globalThis.CustomEvent = class { constructor(type) { this.type = type; } };

let calls = [];
let server = { mirrors: [], cursor: 0, changes: [], epoch: "e1" };
const headOf = () => Math.max(server.cursor, ...server.changes.map((c) => c.seq), 0);
globalThis.fetch = async (path) => {
  calls.push(path);
  const body = path.startsWith("/api/mirrors/changes")
    ? (() => { const after = Number(new URL(path, "https://x").searchParams.get("after"));
        const rows = server.changes.filter((c) => c.seq > after);
        return { mirrors: rows.map((c) => c.row), cursor: rows.length ? rows.at(-1).seq : after, epoch: server.epoch, head: server.head ?? headOf() }; })()
    : { mirrors: server.mirrors, cursor: server.cursor, epoch: server.epoch };
  return { ok: true, status: 200, json: async () => structuredClone(body) };
};
const { syncedMirrors, mergeMirrors, storedCursor } = await import("../../fileshare/static/js/mirrors-sync.js");
const row = (uuid, updated_at, extra = {}) => ({ uuid, n: 1, updated_at, enc_content: "x".repeat(10), ...extra });
const { api } = await import("../../fileshare/static/js/api.js");
const forgetShared = () => api("POST", "/api/x");   // api.js shares one /api/mirrors answer for 4 s
const reset = () => { calls = []; for (const k of Object.keys(stores)) delete stores[k]; };

test("the first open takes the full list and keeps it with its cursor", async () => {
  reset();
  server = { mirrors: [row("a", 5), row("b", 3)], cursor: 7, changes: [], epoch: "e1" };
  const r = await syncedMirrors();
  assert.deepEqual(calls, ["/api/mirrors"]);
  assert.equal(r.cursor, 7);
  assert.equal(await storedCursor(), 7);
});

test("the next open asks only for what changed and merges it", async () => {
  server.changes = [{ seq: 8, row: row("b", 9, { needs: "approval" }) }, { seq: 9, row: row("a", 5, { deleted: true }) },
    { seq: 10, row: row("c", 10) }];
  server.cursor = 10;
  calls = [];
  const r = await syncedMirrors();
  assert.ok(calls.every((c) => c.startsWith("/api/mirrors/changes?after=")), calls.join());
  assert.ok(!calls.includes("/api/mirrors"));
  assert.deepEqual(r.mirrors.map((m) => m.uuid), ["c", "b"]);          // newest first, a is gone
  assert.equal(r.mirrors[1].needs, "approval");
  assert.ok(!("deleted" in r.mirrors[0]));
  assert.equal(r.cursor, 10);
  assert.equal(await storedCursor(), 10);
});

test("concurrent callers share one request round", async () => {
  calls = [];
  await Promise.all([syncedMirrors(), syncedMirrors(), syncedMirrors()]);
  assert.equal(calls.length, 1);
});

test("a stored list older than a day is replaced by a full one", async () => {
  const e = stores.lists.get("mirrors");
  stores.lists.set("mirrors", { ...e, fullAt: Date.now() - 25 * 3600 * 1000 });
  await forgetShared();
  calls = [];
  await syncedMirrors();
  assert.deepEqual(calls, ["/api/mirrors"]);
});

test("offline is reported, not hidden by a second full fetch", async () => {
  const real = globalThis.fetch;
  globalThis.fetch = async () => { throw new TypeError("offline"); };
  await assert.rejects(syncedMirrors(), (e) => e.status === 0);
  globalThis.fetch = real;
});

test("a list without a cursor (an older cache) is fetched in full", async () => {
  stores.lists.set("mirrors", { body: { mirrors: [row("z", 1)] }, at: Date.now() });
  await forgetShared();
  calls = [];
  await syncedMirrors();
  assert.deepEqual(calls, ["/api/mirrors"]);
});

test("mergeMirrors replaces by uuid, drops tombstones and sorts newest first", () => {
  assert.deepEqual(mergeMirrors([row("a", 1), row("b", 2)], [row("a", 3), row("b", 0, { deleted: true })]).map((m) => m.uuid), ["a"]);
});

test("a server that started over (another epoch) is never answered with 'nothing changed'", async () => {
  reset();
  server = { mirrors: [row("a", 5), row("b", 3)], cursor: 500, changes: [], epoch: "old" };
  await syncedMirrors();                                                  // stored: cursor 500, epoch "old"
  await forgetShared();
  // the database was wiped: new epoch, events numbered from 1 again, and a different list
  server = { mirrors: [row("z", 9)], cursor: 4, changes: [], epoch: "new" };
  calls = [];
  const r = await syncedMirrors();
  assert.ok(calls.includes("/api/mirrors"), calls.join());                // the full list, not the delta
  assert.deepEqual(r.mirrors.map((m) => m.uuid), ["z"]);
  assert.equal(await storedCursor(), 4);
});

test("a restored database whose head is behind the stored cursor takes the full list too", async () => {
  reset();
  server = { mirrors: [row("a", 5)], cursor: 90, changes: [], epoch: "same" };
  await syncedMirrors();
  await forgetShared();
  server = { mirrors: [row("b", 6)], cursor: 40, changes: [], epoch: "same", head: 40 };
  calls = [];
  const r = await syncedMirrors();
  assert.ok(calls.includes("/api/mirrors"), calls.join());
  assert.deepEqual(r.mirrors.map((m) => m.uuid), ["b"]);
});

test("a stored list from before epochs existed is refreshed once, then deltas work again", async () => {
  reset();
  await forgetShared();
  stores.lists = new Map([["mirrors", { body: { mirrors: [row("a", 5)], cursor: 7 }, at: Date.now(), fullAt: Date.now() }]]);
  server = { mirrors: [row("a", 5)], cursor: 7, changes: [], epoch: "e1" };
  calls = [];
  await syncedMirrors();
  assert.ok(calls.includes("/api/mirrors"));
  await forgetShared();
  calls = [];
  await syncedMirrors();
  assert.ok(!calls.includes("/api/mirrors"), calls.join());
});
