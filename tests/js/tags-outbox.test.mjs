// tests/js/tags-outbox.test.mjs — queued uploads carry their tags (spec §19): the outbox record
// keeps a prepared request's tags (cleartext by design, like the server's), and the send phase
// puts them in the POST meta. Without tags, both stay exactly as before (spec §16).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createOutbox, memoryStore } from "../../fileshare/static/js/outbox.js";
import { sendPrepared } from "../../fileshare/static/js/outbox-ui.js";

function item(uuid, extra = {}) {
  return { uuid, key_version: 1, wrapped_dek: "w", enc_meta: "m", ttl: "7d", blob: new ArrayBuffer(8), size: 8,
    created_at: "2026-09-25T10:00:00Z", kind: "note", ...extra };
}

test("an outbox record keeps the request's tags", async () => {
  const store = memoryStore();
  const ob = createOutbox({ store, send: async () => ({ status: 201 }) });
  const tags = ["notes", "weekly-report"];
  await ob.add(item("a", { tags }));
  await ob.add(item("b"));
  await ob.add(item("c", { tags: [] }));
  const [a, b, c] = await store.all();
  assert.deepEqual(a.tags, ["notes", "weekly-report"]);
  assert.notEqual(a.tags, tags, "stored as a copy");
  assert.ok(!("tags" in b));
  assert.ok(!("tags" in c));
});

function captureFetch(t) {
  const metas = [];
  t.mock.method(globalThis, "fetch", async (_path, init) => {
    metas.push(JSON.parse(init.body.get("meta")));
    return new Response(JSON.stringify({ id: "FILE1" }), { status: 201, headers: { "Content-Type": "application/json" } });
  });
  globalThis.window ??= new EventTarget();
  return metas;
}

test("the send phase puts the tags in the POST meta", async (t) => {
  const metas = captureFetch(t);
  await sendPrepared(item("a", { tags: ["notes"] }));
  await sendPrepared(item("b"));
  assert.deepEqual(metas[0].tags, ["notes"]);
  assert.ok(!("tags" in metas[1]));
  assert.deepEqual(Object.keys(metas[1]).sort(), ["enc_meta", "key_version", "ttl", "uuid", "wrapped_dek"]);
});
