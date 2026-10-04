// tests/js/signout.test.mjs — sign-out's local wipe: keys first, and one failure never skips the other.
import { test } from "node:test";
import assert from "node:assert/strict";
import { clearLocalData } from "../../fileshare/static/js/outbox-ui.js";

const quiet = (t) => t.mock.method(console, "error", () => {});

const lists = async () => {};

test("keys are cleared first, then the outbox, then the cached lists", async () => {
  const order = [];
  const done = await clearLocalData({ keys: async () => order.push("keys"), queue: async () => order.push("outbox"),
    lists: async () => order.push("lists") });
  assert.deepEqual(order, ["keys", "outbox", "lists"]);
  assert.deepEqual(done, { keys: true, outbox: true, lists: true });
});

test("a failing key or outbox clear still clears the cached lists", async (t) => {
  quiet(t);
  let cleared = false;
  const fail = async () => { throw new Error("idb"); };
  const done = await clearLocalData({ keys: fail, queue: fail, lists: async () => { cleared = true; } });
  assert.equal(cleared, true);
  assert.deepEqual(done, { keys: false, outbox: false, lists: true });
});

test("a failing lists clear is reported, keys and outbox still cleared", async (t) => {
  quiet(t);
  const done = await clearLocalData({ keys: async () => {}, queue: async () => {}, lists: async () => { throw new Error("idb"); } });
  assert.deepEqual(done, { keys: true, outbox: true, lists: false });
});

test("a failing key clear still clears the outbox", async (t) => {
  quiet(t);
  let outbox = false;
  const done = await clearLocalData({ keys: async () => { throw new Error("idb"); }, queue: async () => { outbox = true; }, lists });
  assert.equal(outbox, true);
  assert.deepEqual(done, { keys: false, outbox: true, lists: true });
});

test("a failing outbox clear still reports the keys cleared", async (t) => {
  quiet(t);
  let keys = false;
  const done = await clearLocalData({ keys: async () => { keys = true; }, queue: async () => { throw new Error("idb"); }, lists });
  assert.equal(keys, true);
  assert.deepEqual(done, { keys: true, outbox: false, lists: true });
});
