import { test } from "node:test";
import assert from "node:assert/strict";

// api.js dispatches window events; give it a minimal window and a counting fetch.
globalThis.window = { dispatchEvent() {} };
globalThis.CustomEvent = class { constructor(type) { this.type = type; } };
let calls = [];
globalThis.fetch = async (path, init) => {
  calls.push([init.method, path]);
  return { ok: true, status: 200, json: async () => ({ mirrors: [{ uuid: String(calls.length) }] }) };
};
const { api } = await import("../../fileshare/static/js/api.js");

test("the full mirrors list is fetched once for callers within a few seconds", async () => {
  calls = [];
  const [a, b, c] = await Promise.all([api("GET", "/api/mirrors"), api("GET", "/api/mirrors"), api("GET", "/api/mirrors")]);
  assert.equal(calls.length, 1);
  assert.deepEqual(a, b);
  a.mirrors.push("changed by the first caller"); // every caller gets its own copy
  assert.deepEqual(c.mirrors.length, 1);
});

test("other paths, other methods and a write in between are never shared", async () => {
  calls = [];
  await api("GET", "/api/mirrors/changes?after=0&wait=0");
  await api("GET", "/api/mirrors/changes?after=0&wait=0");
  assert.equal(calls.length, 2);
  await api("POST", "/api/x"); // starts from an empty cache
  calls = [];
  await api("GET", "/api/mirrors");
  await api("POST", "/api/decisions", { json: {} });
  await api("GET", "/api/mirrors");
  assert.deepEqual(calls.map((c) => c[0]), ["GET", "POST", "GET"]);
});

test("a failed fetch is not remembered", async () => {
  calls = [];
  await api("POST", "/api/x"); // clears the cache
  const real = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error("offline"); };
  await assert.rejects(api("GET", "/api/mirrors"));
  globalThis.fetch = real;
  await api("GET", "/api/mirrors");
  assert.equal(calls.length, 2); // the POST and the retry
});
