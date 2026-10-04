// tests/js/outbox.test.mjs — the outbox state machine (spec §16) against a fake fetch and store.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  createOutbox, memoryStore, classify, backoffDelay, checkLimits, OutboxFullError,
  MAX_ITEMS, MAX_BYTES, BACKOFF_MIN_MS, BACKOFF_MAX_MS, decisionRequest,
} from "../../fileshare/static/js/outbox.js";
import { readFileSync } from "node:fs";
import { upgrade, KEYS, OUTBOX, LABELS, PREFS, PAIRS, SEEN, LISTS, DB_VERSION } from "../../fileshare/static/js/db.js";

const MiB = 1024 * 1024;

function item(uuid, size = 100) {
  return { uuid, key_version: 1, wrapped_dek: "w", enc_meta: "m", ttl: "7d", blob: new ArrayBuffer(size), size,
    created_at: "2026-09-25T10:00:00Z", kind: "note" };
}

// A fake timer: records every scheduled delay; fire() runs the pending callback.
function fakeTimers() {
  const t = { delays: [], pending: null, cleared: 0 };
  t.set = (fn, ms) => { t.delays.push(ms); t.pending = fn; return t.delays.length; };
  t.clear = () => { t.cleared += 1; t.pending = null; };
  t.fire = async () => { const fn = t.pending; t.pending = null; await fn?.(); };
  return t;
}

// send() answers from a script of responses, one per call; "net" throws like a failed fetch.
function fakeSend(script) {
  const calls = [];
  const send = async (it) => {
    calls.push(it.uuid);
    const next = script.length ? script.shift() : { status: 201 };
    if (next === "net") throw new TypeError("Failed to fetch");
    return next;
  };
  return { send, calls };
}

function setup(script, extra = {}) {
  const store = memoryStore();
  const timers = fakeTimers();
  const { send, calls } = fakeSend(script);
  const drained = [];
  const ob = createOutbox({ store, send, timers, onDrained: (n) => drained.push(n), ...extra });
  return { store, timers, calls, drained, ob };
}

test("classify maps answers to done, pause, failed or retry", () => {
  assert.equal(classify({ status: 201 }), "done");
  assert.equal(classify({ status: 409, code: "duplicate_uuid" }), "done");
  assert.equal(classify({ status: 409, code: "conflict" }), "failed");
  assert.equal(classify({ status: 401 }), "pause");
  assert.equal(classify({ status: 400, code: "bad_request" }), "failed");
  assert.equal(classify({ status: 413 }), "failed");
  assert.equal(classify({ status: 403 }), "failed");
  assert.equal(classify({ status: 500 }), "retry");
  assert.equal(classify({ status: 503 }), "retry");
  assert.equal(classify({ status: 0 }), "retry");
});

test("backoff doubles from 5 s and stops at 5 min", () => {
  assert.equal(BACKOFF_MIN_MS, 5000);
  assert.equal(BACKOFF_MAX_MS, 300000);
  const steps = [0, 1, 2, 3, 4, 5, 6, 7, 20].map(backoffDelay);
  assert.deepEqual(steps, [5000, 10000, 20000, 40000, 80000, 160000, 300000, 300000, 300000]);
});

test("limits: 20 items or 300 MiB, whichever comes first", () => {
  assert.equal(MAX_ITEMS, 20);
  assert.equal(MAX_BYTES, 300 * MiB);
  const nineteen = Array.from({ length: 19 }, (_, i) => ({ size: 1 }));
  checkLimits(nineteen, 1); // the 20th fits
  assert.throws(() => checkLimits([...nineteen, { size: 1 }], 1), OutboxFullError);
  checkLimits([{ size: 200 * MiB }], 100 * MiB); // exactly 300 MiB fits
  assert.throws(() => checkLimits([{ size: 200 * MiB }], 100 * MiB + 1), OutboxFullError);
});

test("add refuses past the limits and keeps the queue as it was", async () => {
  const { ob, store } = setup([]);
  for (let i = 0; i < 20; i += 1) await ob.add(item(`u${i}`));
  await assert.rejects(ob.add(item("u20")), OutboxFullError);
  assert.equal((await store.all()).length, 20);
  const big = setup([]);
  await big.ob.add(item("a", 250 * MiB));
  await assert.rejects(big.ob.add(item("b", 51 * MiB)), OutboxFullError);
  assert.equal((await big.store.all()).length, 1);
});

test("the first add asks for persistent storage, later ones don't", async () => {
  let asked = 0;
  const { ob } = setup([], { persist: () => { asked += 1; } });
  await ob.add(item("a"));
  await ob.add(item("b"));
  assert.equal(asked, 1);
});

test("flush uploads oldest first, one at a time, and reports the drain once", async () => {
  const { ob, calls, drained, store } = setup([{ status: 201 }, { status: 201 }, { status: 201 }]);
  await ob.add(item("a"));
  await ob.add(item("b"));
  await ob.add(item("c"));
  await ob.flush();
  assert.deepEqual(calls, ["a", "b", "c"]);
  assert.deepEqual(await store.all(), []);
  assert.deepEqual(drained, [3]);
});

test("a 409 duplicate_uuid counts as done", async () => {
  const { ob, store, drained } = setup([{ status: 409, code: "duplicate_uuid" }]);
  await ob.add(item("a"));
  await ob.flush();
  assert.deepEqual(await store.all(), []);
  assert.deepEqual(drained, [1]);
});

test("a 401 pauses the outbox and keeps every item, until resume", async () => {
  const { ob, store, calls, timers } = setup([{ status: 401 }, { status: 201 }, { status: 201 }]);
  await ob.add(item("a"));
  await ob.add(item("b"));
  await ob.flush();
  assert.deepEqual(calls, ["a"]);
  assert.equal((await store.all()).length, 2);
  assert.equal(ob.status().paused, true);
  assert.equal(timers.pending, null, "a pause schedules no retry");
  await ob.flush(); // still paused: nothing is sent
  assert.deepEqual(calls, ["a"]);
  await ob.resume();
  assert.deepEqual(calls, ["a", "a", "b"]);
  assert.deepEqual(await store.all(), []);
});

test("another 4xx marks the item failed and the queue moves on", async () => {
  const { ob, store, calls, drained } = setup([{ status: 400, code: "bad_request" }, { status: 201 }]);
  await ob.add(item("a"));
  await ob.add(item("b"));
  await ob.flush();
  assert.deepEqual(calls, ["a", "b"]);
  const left = await store.all();
  assert.equal(left.length, 1);
  assert.equal(left[0].uuid, "a");
  assert.equal(left[0].state, "failed");
  assert.equal(left[0].error, "bad_request");
  assert.deepEqual(drained, [1]);
  await ob.flush(); // failed items wait for Retry
  assert.deepEqual(calls, ["a", "b"]);
});

test("Retry sends a failed item again; Discard drops it", async () => {
  const { ob, store, calls } = setup([{ status: 400 }, { status: 400 }, { status: 201 }]);
  const a = await ob.add(item("a"));
  const b = await ob.add(item("b"));
  await ob.flush();
  assert.equal((await store.all()).filter((x) => x.state === "failed").length, 2);
  await ob.discard(b);
  await ob.retry(a);
  assert.deepEqual(calls, ["a", "b", "a"]);
  assert.deepEqual(await store.all(), []);
});

test("a 5xx or a network error keeps the item and retries with backoff; FIFO holds", async () => {
  const { ob, store, calls, timers, drained } = setup([{ status: 500 }, "net", "net", { status: 201 }, { status: 201 }]);
  await ob.add(item("a"));
  await ob.add(item("b"));
  await ob.flush();
  assert.deepEqual(calls, ["a"], "b never jumps ahead of a");
  assert.deepEqual(timers.delays, [5000]);
  await timers.fire();
  assert.deepEqual(timers.delays, [5000, 10000]);
  await timers.fire();
  assert.deepEqual(timers.delays, [5000, 10000, 20000]);
  assert.equal((await store.all()).length, 2);
  await timers.fire();
  assert.deepEqual(calls, ["a", "a", "a", "a", "b"]);
  assert.deepEqual(await store.all(), []);
  assert.deepEqual(drained, [2]);
  assert.ok(ob.status().nextRetryAt === null);
});

test("a success resets the backoff", async () => {
  const { ob, timers } = setup(["net", { status: 201 }, "net"]);
  await ob.add(item("a"));
  await ob.add(item("b"));
  await ob.flush(); // a: net -> 5 s
  await timers.fire(); // a: 201, b: net -> 5 s again
  assert.deepEqual(timers.delays, [5000, 5000]);
});

test("an explicit flush (online, visible, Retry now) cancels the timer and sends at once", async () => {
  const { ob, calls, timers } = setup(["net", { status: 201 }]);
  await ob.add(item("a"));
  await ob.flush();
  assert.ok(timers.pending);
  await ob.flush({ reset: true });
  assert.deepEqual(calls, ["a", "a"]);
  assert.equal(timers.pending, null);
});

test("schedule() arms the first 5 s retry without sending", async () => {
  const { ob, calls, timers } = setup([]);
  await ob.add(item("a"));
  ob.schedule();
  assert.deepEqual(calls, []);
  assert.deepEqual(timers.delays, [5000]);
});

test("concurrent flushes share one run (one flusher at a time)", async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const store = memoryStore();
  const calls = [];
  const ob = createOutbox({
    store, timers: fakeTimers(),
    send: async (it) => { calls.push(it.uuid); await gate; return { status: 201 }; },
  });
  await ob.add(item("a"));
  const p1 = ob.flush();
  const p2 = ob.flush();
  release();
  await Promise.all([p1, p2]);
  assert.deepEqual(calls, ["a"]);
});

test("the cross-tab lock: when another tab holds it, this tab sends nothing", async () => {
  const { ob, calls } = setup([], { lock: async () => undefined /* not granted */ });
  await ob.add(item("a"));
  await ob.flush();
  assert.deepEqual(calls, []);
});

test("items() lists oldest first and clear() empties the queue", async () => {
  const { ob } = setup([]);
  await ob.add(item("a"));
  await ob.add(item("b"));
  assert.deepEqual((await ob.items()).map((x) => x.uuid), ["a", "b"]);
  assert.equal(await ob.count(), 2);
  await ob.clear();
  assert.equal(await ob.count(), 0);
});

test("add stores exactly the upload request: no name, no plaintext fields", async () => {
  const { ob, store } = setup([]);
  await ob.add({ ...item("a"), name: "secret.md", text: "hello" });
  const [stored] = await store.all();
  assert.deepEqual(Object.keys(stored).sort(),
    ["blob", "created_at", "enc_meta", "key_version", "kind", "seq", "size", "state", "ttl", "uuid", "wrapped_dek"]);
});

test("onChange fires on add, send and failure", async () => {
  let changes = 0;
  const { ob } = setup([{ status: 400 }], { onChange: () => { changes += 1; } });
  await ob.add(item("a"));
  const afterAdd = changes;
  await ob.flush();
  assert.ok(afterAdd >= 1 && changes > afterAdd);
});

test("db upgrade to v6 adds the outbox, labels, prefs, pairs, seen and lists and keeps the keys store", () => {
  assert.equal(DB_VERSION, 6);
  const created = [];
  const fakeDb = (existing) => ({
    objectStoreNames: { contains: (n) => existing.includes(n) },
    createObjectStore: (n, opts) => created.push([n, opts]),
  });
  upgrade(fakeDb([KEYS])); // from v1
  assert.deepEqual(created, [[OUTBOX, { keyPath: "seq", autoIncrement: true }], [LABELS, undefined], [PREFS, undefined],
    [PAIRS, { keyPath: "space" }], [SEEN, undefined], [LISTS, undefined]]);
  created.length = 0;
  upgrade(fakeDb([KEYS, OUTBOX])); // from v2
  assert.deepEqual(created.map((c) => c[0]), [LABELS, PREFS, PAIRS, SEEN, LISTS]);
  created.length = 0;
  upgrade(fakeDb([KEYS, OUTBOX, LABELS, PREFS])); // from v3
  assert.deepEqual(created, [[PAIRS, { keyPath: "space" }], [SEEN, undefined], [LISTS, undefined]]);
  created.length = 0;
  upgrade(fakeDb([KEYS, OUTBOX, LABELS, PREFS, PAIRS])); // from v4
  assert.deepEqual(created, [[SEEN, undefined], [LISTS, undefined]]);
  created.length = 0;
  upgrade(fakeDb([KEYS, OUTBOX, LABELS, PREFS, PAIRS, SEEN])); // from v5
  assert.deepEqual(created, [[LISTS, undefined]]);
  created.length = 0;
  upgrade(fakeDb([])); // a fresh browser
  assert.deepEqual(created.map((c) => c[0]), [KEYS, OUTBOX, LABELS, PREFS, PAIRS, SEEN, LISTS]);
});

test("the service worker opens the same database version with the same stores", () => {
  const sw = readFileSync(new URL("../../fileshare/static/sw.js", import.meta.url), "utf8");
  assert.match(sw, new RegExp(`const DB_VERSION = ${DB_VERSION};`));
  assert.match(sw, /\["keys", null\], \["outbox", \{ keyPath: "seq", autoIncrement: true \}\], \["labels", null\], \["prefs", null\],\n  \["pairs", \{ keyPath: "space" \}\], \["seen", null\], \["lists", null\]\]/);
});

test("add runs the limit check and the insert as one step (addChecked)", async () => {
  const store = memoryStore();
  const seen = [];
  const spy = { ...store, all: store.all.bind(store), add: store.add.bind(store),
    addChecked: async (rec, check) => { seen.push("addChecked"); return store.addChecked(rec, check); } };
  spy.add = async () => { throw new Error("add must go through addChecked"); };
  const ob = createOutbox({ store: spy, send: async () => ({ status: 201 }), timers: fakeTimers() });
  await ob.add(item("a"));
  assert.deepEqual(seen, ["addChecked"]);
  assert.equal((await store.all()).length, 1);
});

test("two adds racing at the limit: exactly one fits", async () => {
  const { ob, store } = setup([]);
  for (let i = 0; i < 19; i += 1) await ob.add(item(`u${i}`));
  const results = await Promise.allSettled([ob.add(item("x")), ob.add(item("y"))]);
  assert.deepEqual(results.map((r) => r.status).sort(), ["fulfilled", "rejected"]);
  assert.equal((await store.all()).length, 20);
});

// ---- tickets (spec T10): the ticket-event kind

function ticketEvent(uuid, extra = {}) {
  const body = { uuid, kind: "answer", enc_body: "ENC" };
  return { uuid, kind: "ticket-event", ref: "TIX-42", body, size: JSON.stringify(body).length,
    created_at: "2026-09-25T10:00:00Z", ...extra };
}

test("a ticket-event keeps exactly {kind, ref, body} (plus after_uuid), and flushes", async () => {
  const { ob, store, calls, drained } = setup([{ status: 201 }]);
  await ob.add({ ...ticketEvent("e1"), text: "plaintext never stored" });
  const [stored] = await store.all();
  assert.deepEqual(Object.keys(stored).filter((k) => stored[k] !== undefined).sort(),
    ["body", "created_at", "kind", "ref", "seq", "size", "state", "uuid"]);
  assert.deepEqual(stored.body, { uuid: "e1", kind: "answer", enc_body: "ENC" });
  await ob.flush();
  assert.deepEqual(calls, ["e1"]);
  assert.deepEqual(await store.all(), []);
  assert.deepEqual(drained, [1]);
});

test("a ticket-event answered 409 duplicate_uuid counts as done (a double tap or a retry)", async () => {
  const { ob, store } = setup([{ status: 409, code: "duplicate_uuid" }]);
  await ob.add(ticketEvent("e1"));
  await ob.flush();
  assert.deepEqual(await store.all(), []);
});

test("a ticket-event refused with 409 bad_move is kept as failed", async () => {
  const { ob, store } = setup([{ status: 409, code: "bad_move" }]);
  await ob.add(ticketEvent("e1"));
  await ob.flush();
  const [left] = await store.all();
  assert.equal(left.state, "failed");
  assert.equal(left.error, "bad_move");
});

test("a voice-dependent event waits for its file item; others go ahead", async () => {
  // The voice note failed (a 4xx), so the answer that needs it must not go out; a comment queued
  // after it is independent and still flushes.
  const { ob, store, calls } = setup([{ status: 400, code: "bad_request" }, { status: 201 }, { status: 201 }, { status: 201 }]);
  const voice = await ob.add(item("voice"));
  await ob.add(ticketEvent("answer", { after_uuid: "voice" }));
  await ob.add(ticketEvent("comment"));
  await ob.flush();
  assert.deepEqual(calls, ["voice", "comment"], "the answer waits while its voice note is still queued");
  assert.deepEqual((await store.all()).map((x) => x.uuid), ["voice", "answer"]);
  const [kept] = (await store.all()).filter((x) => x.uuid === "answer");
  assert.equal(kept.after_uuid, "voice");
  await ob.retry(voice);
  assert.deepEqual(calls, ["voice", "comment", "voice", "answer"], "the file first, then its event");
  assert.deepEqual(await store.all(), []);
});

test("a voice note's 201 hands its FILE id to the event waiting for it, before the event is sent", async () => {
  const seen = [];
  const store = memoryStore();
  const ob = createOutbox({
    store, timers: fakeTimers(),
    send: async (it) => { seen.push({ uuid: it.uuid, after_file: it.after_file }); return it.uuid === "voice" ? { status: 201, id: "FILE94" } : { status: 201 }; },
  });
  await ob.add(item("voice"));
  await ob.add(ticketEvent("answer", { after_uuid: "voice" }));
  await ob.add(ticketEvent("other"));
  await ob.flush();
  assert.deepEqual(seen, [
    { uuid: "voice", after_file: undefined }, { uuid: "answer", after_file: "FILE94" }, { uuid: "other", after_file: undefined },
  ]);
});

test("update() replaces an item's fields and makes it pending again", async () => {
  const { ob, store, calls } = setup([{ status: 400, code: "voice_missing" }, { status: 201 }]);
  const seq = await ob.add(ticketEvent("answer", { after_uuid: "gone" }));
  await ob.flush();
  assert.equal((await store.all())[0].state, "failed");
  await ob.update(seq, { body: { uuid: "answer", kind: "answer", enc_body: "RESEALED" }, after_uuid: undefined });
  const [it] = await store.all();
  assert.equal(it.state, "pending");
  assert.equal(it.error, undefined);
  assert.equal(it.body.enc_body, "RESEALED");
  await ob.flush();
  assert.deepEqual(calls, ["answer", "answer"]);
  assert.deepEqual(await store.all(), []);
});

test("a voice-dependent event retries behind its file while the network is down", async () => {
  const { ob, calls, timers } = setup(["net", { status: 201 }, { status: 201 }]);
  await ob.add(item("voice"));
  await ob.add(ticketEvent("answer", { after_uuid: "voice" }));
  await ob.flush();
  assert.deepEqual(calls, ["voice"]);
  await timers.fire();
  assert.deepEqual(calls, ["voice", "voice", "answer"]);
});

// ---- TIX on orch-core (Task 9): the decision kind

function decisionItem(uuid, extra = {}) {
  return { kind: "decision", uuid, space: "a".repeat(32), ticket: "TIX-42", decisionKind: "answer", key_version: 1,
    body: "ENCBODY", size: 7, created_at: "2026-10-02T09:41:07Z", ...extra };
}

test("a decision keeps exactly its sealed request fields, never plaintext", async () => {
  const { ob, store } = setup([]);
  await ob.add({ ...decisionItem("d".repeat(32)), value: "A", note: "plaintext never stored" });
  const [stored] = await store.all();
  assert.deepEqual(Object.keys(stored).filter((k) => stored[k] !== undefined).sort(),
    ["body", "created_at", "decisionKind", "key_version", "kind", "seq", "size", "space", "state", "ticket", "uuid"]);
});

test("a decision item posts to /api/decisions with its sealed body", () => {
  const req = decisionRequest(decisionItem("d".repeat(32)));
  assert.deepEqual(req, { method: "POST", path: "/api/decisions", json: {
    uuid: "d".repeat(32), space: "a".repeat(32), ticket: "TIX-42", kind: "answer", key_version: 1, enc_body: "ENCBODY" } });
});

test("a decision answered 409 duplicate_uuid is removed (it was sent)", async () => {
  const posted = [];
  const store = memoryStore();
  const send = async (it) => {
    const req = decisionRequest(it);
    posted.push(req.path);
    return { status: 409, code: "duplicate_uuid" };
  };
  const ob = createOutbox({ store, send, timers: fakeTimers() });
  await ob.add(decisionItem("d".repeat(32)));
  await ob.flush();
  assert.deepEqual(posted, ["/api/decisions"]);
  assert.deepEqual(await store.all(), []);
});

test("queueing the same decision twice (a double tap) keeps one item", async () => {
  const { ob, store, calls } = setup([{ status: 201 }]);
  const a = await ob.add(decisionItem("d".repeat(32)));
  const b = await ob.add(decisionItem("d".repeat(32)));
  assert.equal(a, b);
  assert.equal((await store.all()).length, 1);
  await ob.flush();
  assert.deepEqual(calls, ["d".repeat(32)]);
});
