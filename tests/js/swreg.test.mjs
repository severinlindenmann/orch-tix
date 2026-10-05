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

import { listenForUpdate, safeToReload } from "../../fileshare/static/js/swreg.js";

function swFake(controller) {
  let fire;
  return { sw: { controller, addEventListener: (t, fn) => { if (t === "controllerchange") fire = fn; } }, fire: () => fire() };
}
const fakeDoc = (visibility, extra = {}) => { const made = []; return { visibilityState: visibility, made, querySelector: () => extra.dialog || null,
  querySelectorAll: () => extra.fields || [], getElementById: (id) => made.find((x) => x.id === id) || null,
  createElement: () => { const n = { children: [], listeners: {}, setAttribute() {}, append(...c) { this.children.push(...c); }, addEventListener(t, f) { this.listeners[t] = f; } }; return n; },
  body: { append(n) { made.push(n); } } }; };

test("the first claim of an uncontrolled page is not an update", () => {
  const { sw, fire } = swFake(null);
  const d = fakeDoc("visible");
  listenForUpdate({ serviceWorker: sw }, d, () => assert.fail("no reload"));
  fire();
  assert.equal(d.made.length, 0);
});

test("a visible controlled page is asked, once; Reload reloads", () => {
  const { sw, fire } = swFake({});
  const d = fakeDoc("visible"); let reloads = 0;
  listenForUpdate({ serviceWorker: sw }, d, () => reloads++);
  fire(); fire();
  assert.equal(d.made.length, 1);
  d.made[0].children[1].listeners.click();
  assert.equal(reloads, 1);
});

test("a hidden page with nothing typed reloads quietly; with a draft it asks", () => {
  let a = swFake({}), reloads = 0, d = fakeDoc("hidden");
  listenForUpdate({ serviceWorker: a.sw }, d, () => reloads++);
  a.fire();
  assert.equal(reloads, 1); assert.equal(d.made.length, 0);
  a = swFake({}); d = fakeDoc("hidden", { fields: [{ value: "half a request" }] }); reloads = 0;
  listenForUpdate({ serviceWorker: a.sw }, d, () => reloads++);
  a.fire();
  assert.equal(reloads, 0); assert.equal(d.made.length, 1);
  assert.equal(safeToReload(fakeDoc("hidden", { dialog: {} })), false);
});
