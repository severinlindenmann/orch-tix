// tests/js/wakelock.test.mjs — keepAwake(): the screen stays on while recording, with a fake
// navigator.wakeLock (the Screen Wake Lock API) and a fake document for visibility changes.
import { test } from "node:test";
import assert from "node:assert/strict";
import { keepAwake } from "../../fileshare/static/js/wakelock.js";

const tick = () => new Promise((r) => setTimeout(r, 0));

function fakeDoc(state = "visible") {
  const doc = new EventTarget();
  doc.visibilityState = state;
  doc.show = () => { doc.visibilityState = "visible"; doc.dispatchEvent(new Event("visibilitychange")); };
  doc.hide = () => { doc.visibilityState = "hidden"; doc.dispatchEvent(new Event("visibilitychange")); };
  return doc;
}

// Like the browser: request("screen") resolves to a sentinel; hiding the page releases it.
function fakeNav(doc, { refuse = false, hold = false } = {}) {
  const nav = { requests: [], sentinels: [] };
  let pending = [];
  nav.wakeLock = {
    request(type) {
      nav.requests.push(type);
      if (refuse) return Promise.reject(new DOMException("battery saver", "NotAllowedError"));
      const s = new EventTarget();
      s.released = false;
      s.release = async () => {
        if (s.released) return;
        s.released = true;
        s.dispatchEvent(new Event("release"));
      };
      nav.sentinels.push(s);
      doc.addEventListener("visibilitychange", () => { if (doc.visibilityState === "hidden") s.release(); });
      if (!hold) return Promise.resolve(s);
      return new Promise((resolve) => pending.push(() => resolve(s)));
    },
  };
  nav.resolvePending = () => { pending.forEach((f) => f()); pending = []; };
  return nav;
}

test("keepAwake asks for a screen wake lock and release() lets it go", async () => {
  const doc = fakeDoc();
  const nav = fakeNav(doc);
  const lock = keepAwake({ nav, doc });
  await tick();
  assert.deepEqual(nav.requests, ["screen"]);
  assert.equal(nav.sentinels[0].released, false);
  await lock.release();
  assert.equal(nav.sentinels[0].released, true);
});

test("without the Wake Lock API it does nothing and never throws", async () => {
  const lock = keepAwake({ nav: {}, doc: fakeDoc() });
  await tick();
  await lock.release();
  await lock.release();
  const noNav = keepAwake({ nav: undefined, doc: undefined });
  await noNav.release();
});

test("a refused request (battery saver, page not visible) is ignored", async () => {
  const doc = fakeDoc();
  const nav = fakeNav(doc, { refuse: true });
  const lock = keepAwake({ nav, doc });
  await tick();
  assert.deepEqual(nav.requests, ["screen"]);
  await lock.release();
});

test("returning to the app takes the lock again, since hiding it released the old one", async () => {
  const doc = fakeDoc();
  const nav = fakeNav(doc);
  const lock = keepAwake({ nav, doc });
  await tick();
  doc.hide();
  await tick();
  assert.equal(nav.sentinels[0].released, true);
  assert.equal(nav.requests.length, 1, "no request while hidden");
  doc.show();
  await tick();
  assert.equal(nav.requests.length, 2);
  assert.equal(nav.sentinels[1].released, false);
  await lock.release();
  assert.equal(nav.sentinels[1].released, true);
});

test("a visible page that already holds the lock does not ask twice", async () => {
  const doc = fakeDoc();
  const nav = fakeNav(doc);
  const lock = keepAwake({ nav, doc });
  await tick();
  doc.dispatchEvent(new Event("visibilitychange"));   // still visible: e.g. a focus change
  await tick();
  assert.equal(nav.requests.length, 1);
  await lock.release();
});

test("after release() it never asks again, even when the page comes back", async () => {
  const doc = fakeDoc();
  const nav = fakeNav(doc);
  const lock = keepAwake({ nav, doc });
  await tick();
  await lock.release();
  doc.hide();
  doc.show();
  await tick();
  assert.equal(nav.requests.length, 1);
});

test("release() while the request is still pending releases the lock once it arrives", async () => {
  const doc = fakeDoc();
  const nav = fakeNav(doc, { hold: true });
  const lock = keepAwake({ nav, doc });
  await tick();
  const done = lock.release();
  nav.resolvePending();
  await done;
  await tick();
  assert.equal(nav.sentinels[0].released, true);
});

test("a page that starts hidden asks only once it becomes visible", async () => {
  const doc = fakeDoc("hidden");
  const nav = fakeNav(doc);
  const lock = keepAwake({ nav, doc });
  await tick();
  assert.equal(nav.requests.length, 0);
  doc.show();
  await tick();
  assert.equal(nav.requests.length, 1);
  await lock.release();
});
