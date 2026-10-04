// tests/js/swreg.test.mjs — service worker registration (spec §16).
import { test } from "node:test";
import assert from "node:assert/strict";
import { buildStamp, listenForNewBuild, registerServiceWorker } from "../../fileshare/static/js/swreg.js";

const doc = (href) => ({ querySelector: () => (href === null ? null : { href }) });

test("the build comes from the stamped stylesheet link", () => {
  assert.equal(buildStamp(doc("/static/css/app.css?v=ab12cd3")), "ab12cd3");
  assert.equal(buildStamp(doc("https://tix.test/static/css/app.css?v=x.y-z")), "x.y-z");
  assert.equal(buildStamp(doc("/static/css/app.css")), "dev");
  assert.equal(buildStamp(doc(null)), "dev");
});

test("registers /sw.js?v=<build> with scope /", async () => {
  const calls = [];
  const nav = { serviceWorker: { register: async (url, opts) => { calls.push([url, opts]); return "reg"; } } };
  assert.equal(await registerServiceWorker(nav, doc("/static/css/app.css?v=b1")), "reg");
  assert.deepEqual(calls, [["/sw.js?v=b1", { scope: "/" }]]);
});

test("fails silently without service worker support or when registration is refused", async () => {
  assert.equal(await registerServiceWorker({}, doc("/static/css/app.css?v=b1")), null);
  assert.equal(await registerServiceWorker(undefined, doc("x")), null);
  const refused = { serviceWorker: { register: async () => { throw new DOMException("no", "SecurityError"); } } };
  assert.equal(await registerServiceWorker(refused, doc("/static/css/app.css?v=b1")), null);
  const throws = { serviceWorker: { register: () => { throw new Error("sync"); } } };
  assert.equal(await registerServiceWorker(throws, doc("/static/css/app.css?v=b1")), null);
});

test("a new-build message from the worker registers that build's worker; anything else is ignored", async () => {
  const calls = [];
  let onMessage;
  const nav = { serviceWorker: { addEventListener: (t, fn) => { if (t === "message") onMessage = fn; },
    register: async (url, opts) => { calls.push([url, opts]); return "reg"; } } };
  listenForNewBuild(nav);
  onMessage({ data: { type: "new-build", build: "NEW1" } });
  onMessage({ data: { type: "new-build", build: "../evil?x" } });
  onMessage({ data: { type: "outbox-flush" } });
  onMessage({ data: null });
  assert.deepEqual(calls, [["/sw.js?v=NEW1", { scope: "/" }]]);
  listenForNewBuild({});          // no service workers: nothing to listen to
});
