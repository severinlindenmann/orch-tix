// tests/js/sw.test.mjs — the service worker's routing and caching rules (spec §16), run in a vm
// with fake caches, fetch and clients.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SRC = readFileSync(new URL("../../fileshare/static/sw.js", import.meta.url), "utf8");
const ORIGIN = "https://tix.test";

// The build the pages are stamped with (X-Build): by default the build of the worker load() made last.
let currentBuild = "abc";

function res(body = "", { status = 200, type = "basic", redirected = false, cc = "no-cache", build = currentBuild } = {}) {
  const headers = { "cache-control": cc, "x-build": build };
  return {
    body, status, type, redirected, ok: status >= 200 && status < 300,
    headers: { get: (h) => headers[h.toLowerCase()] ?? null },
    clone() { return this; },
    async json() { return JSON.parse(body); },
  };
}

function fakeCaches() {
  const stores = new Map();
  const keyOf = (k, ignoreSearch) => {
    const u = new URL(typeof k === "string" ? k : k.url, ORIGIN);
    return ignoreSearch ? u.pathname : u.pathname + u.search;
  };
  const open = async (name) => {
    if (!stores.has(name)) stores.set(name, new Map());
    const m = stores.get(name);
    return {
      async put(k, r) { m.set(keyOf(k, false), r); },
      async match(k, opts = {}) {
        if (opts.ignoreSearch) {
          for (const [key, r] of m) if (keyOf(key, true) === keyOf(k, true)) return r;
          return undefined;
        }
        return m.get(keyOf(k, false));
      },
    };
  };
  return {
    stores,
    api: {
      open,
      async keys() { return [...stores.keys()]; },
      async delete(n) { return stores.delete(n); },
    },
  };
}

// Loads sw.js as the worker at /sw.js?v=<build>. `net(url, init)` answers fetch().
// `windows` (optional) are the open window clients matchAll returns; each records focus/navigate.
// A tiny IndexedDB: open() -> db.transaction(store).objectStore(store).get(key), from `idb`
// ({store: {key: value}}). `reads` records every (store, key) the worker asked for.
function fakeIdb(idb, reads) {
  const later = (req, fn) => setImmediate(() => { fn(); });
  return {
    open() {
      const req = {};
      later(req, () => {
        req.result = {
          objectStoreNames: { contains: (n) => n in idb },
          close() {},
          transaction(store) {
            return { objectStore: () => ({ get(key) {
              reads.push([store, key]);
              const g = {};
              later(g, () => { g.result = idb[store]?.[key]; g.onsuccess?.(); });
              return g;
            } }) };
          },
        };
        req.onsuccess?.();
      });
      return req;
    },
  };
}

function load({ build = "abc", net = async () => res("net"), windows = null, idb = {}, crypto: cryptoImpl } = {}) {
  currentBuild = build;
  const reads = [];
  const notifications = [];
  const listeners = {};
  const caches = fakeCaches();
  const fetched = [];
  const posted = [];
  const shown = [];
  const opened = [];
  const self = {
    location: new URL(`${ORIGIN}/sw.js?v=${build}`),
    addEventListener: (t, fn) => { listeners[t] = fn; },
    skipWaiting: async () => { self.skipped = true; },
    indexedDB: fakeIdb(idb, reads),
    crypto: cryptoImpl ?? globalThis.crypto,
    registration: {
      showNotification: async (title, options) => {
        shown.push({ title, options });
        for (let i = notifications.length - 1; i >= 0; i -= 1) if (notifications[i].tag === options.tag) notifications.splice(i, 1);
        const n = { title, tag: options.tag, data: options.data, close() { const j = notifications.indexOf(n); if (j >= 0) notifications.splice(j, 1); } };
        notifications.push(n);
      },
      getNotifications: async (filter = {}) => notifications.filter((n) => !filter.tag || n.tag === filter.tag),
    },
    clients: {
      claim: async () => { self.claimed = true; },
      matchAll: async () => windows ?? [{ postMessage: (m) => posted.push(m) }],
      openWindow: async (url) => { opened.push(url); return null; },
    },
  };
  const fetch = async (req, init) => {
    const url = typeof req === "string" ? req : req.url;
    fetched.push(url);
    return net(url, init);
  };
  vm.runInNewContext(SRC, { self, caches: caches.api, fetch, URL, Promise, setTimeout, clearTimeout, console,
    atob, TextEncoder, TextDecoder, Uint8Array });
  return { listeners, caches, fetched, posted, shown, opened, self, reads, notifications };
}

function fetchEvent(url, { method = "GET", mode = "cors" } = {}) {
  const ev = { request: { url: new URL(url, ORIGIN).href, method, mode }, responded: null, waits: [] };
  ev.respondWith = (p) => { ev.responded = Promise.resolve(p); };
  ev.waitUntil = (p) => { ev.waits.push(p); };
  return ev;
}

const PRECACHE = JSON.stringify({ pages: ["/", "/settings"], assets: ["/static/js/a.js", "/static/css/app.css"] });

test("install precaches pages and assets into shell-<BUILD>, then skips waiting", async () => {
  const w = load({
    build: "b42",
    net: async (url, init) => {
      if (url.startsWith("/static/precache.json")) return res(PRECACHE);
      if (url === "/settings") {
        assert.equal(init.redirect, "manual");
        return res("", { status: 0, type: "opaqueredirect" }); // signed out: skipped
      }
      return res(`body of ${url}`);
    },
  });
  const ev = fetchEvent("/");
  w.listeners.install(ev);
  await Promise.all(ev.waits);
  assert.ok(w.fetched.includes("/static/precache.json?v=b42"));
  assert.deepEqual([...w.caches.stores.keys()], ["shell-b42"]);
  assert.deepEqual([...w.caches.stores.get("shell-b42").keys()].sort(), ["/", "/static/css/app.css", "/static/js/a.js", "/static/precache.json"]);
  assert.equal(w.self.skipped, true);
});

test("install fails when an asset can't be cached, so the old worker stays", async () => {
  const w = load({ net: async (url) => (url.startsWith("/static/precache.json") ? res(PRECACHE)
    : url === "/static/js/a.js" ? res("", { status: 404 }) : res("ok")) });
  const ev = fetchEvent("/");
  w.listeners.install(ev);
  await assert.rejects(Promise.all(ev.waits));
  assert.notEqual(w.self.skipped, true);
});

test("activate deletes older shell caches only, then claims the clients", async () => {
  const w = load({ build: "new" });
  for (const n of ["shell-old", "shell-new", "other"]) await w.caches.api.open(n);
  const ev = fetchEvent("/");
  w.listeners.activate(ev);
  await Promise.all(ev.waits);
  assert.deepEqual([...w.caches.stores.keys()].sort(), ["other", "shell-new"]);
  assert.equal(w.self.claimed, true);
});

test("bypassed: non-GET, cross-origin, /api/, /p/, /u/, /skill/, /onboarding* and /sw.js", () => {
  const w = load();
  const cases = [
    ["/api/files", {}], ["/api/files", { method: "POST" }], ["/", { method: "POST", mode: "navigate" }],
    ["/p/AAAA", { mode: "navigate" }], ["/u/AAAA", { mode: "navigate" }], ["/skill/sharing.py", {}],
    ["/onboarding", { mode: "navigate" }],
    ["/onboarding/abc", {}], ["/sw.js?v=1", {}], ["https://elsewhere.test/static/x.js", {}],
    ["/static/js/a.js", { method: "HEAD" }],
  ];
  for (const [url, opts] of cases) {
    const ev = fetchEvent(url, opts);
    w.listeners.fetch(ev);
    assert.equal(ev.responded, null, `${opts.method || "GET"} ${url} must not be handled`);
  }
});

test("assets are cache first, ignoring ?v=", async () => {
  const w = load();
  (await w.caches.api.open("shell-abc")).put("/static/js/a.js", res("cached"));
  const ev = fetchEvent("/static/js/a.js?v=abc");
  w.listeners.fetch(ev);
  assert.equal((await ev.responded).body, "cached");
  assert.deepEqual(w.fetched, []);
});

test("an asset miss goes to the network and is cached, unless no-store, opaque or an error", async () => {
  const answers = {
    "/static/js/new.js": res("fresh"),
    "/static/js/nostore.js": res("x", { cc: "private, no-store" }),
    "/static/js/opaque.js": res("x", { type: "opaque", status: 0 }),
    "/static/js/missing.js": res("x", { status: 404 }),
  };
  const w = load({ net: async (url) => answers[new URL(url).pathname] });
  for (const path of Object.keys(answers)) {
    const ev = fetchEvent(path);
    w.listeners.fetch(ev);
    await ev.responded;
  }
  assert.deepEqual([...(w.caches.stores.get("shell-abc")?.keys() ?? [])], ["/static/js/new.js"]);
});

test("a navigation without a cached page is network first and caches the answer", async () => {
  const w = load({ net: async () => res("online page") });
  const ev = fetchEvent("/devices?x=1", { mode: "navigate" });
  w.listeners.fetch(ev);
  assert.equal((await ev.responded).body, "online page");
  await Promise.all(ev.waits);
  assert.equal((await (await w.caches.api.open("shell-abc")).match("/devices")).body, "online page");
});

test("a navigation with a cached page answers from the cache at once and refreshes it in the background", async () => {
  let answer;
  const w = load({ net: () => new Promise((r) => { answer = r; }) }); // the server is slow
  const c = await w.caches.api.open("shell-abc");
  await c.put("/", res("cached root"));
  await c.put("/settings", res("cached settings"));
  // The second "/" open gets what the first one's refresh stored.
  for (const [path, want] of [["/", "cached root"], ["/?view=tickets", "fresh /"], ["/settings", "cached settings"]]) {
    const ev = fetchEvent(path, { mode: "navigate" });
    w.listeners.fetch(ev);
    assert.equal((await ev.responded).body, want, path); // no timer: the cache answered before the network
    assert.equal(ev.waits.length, 1, "the refresh is kept alive with waitUntil");
    answer(res(`fresh ${path}`));
    await Promise.all(ev.waits);
  }
  assert.equal((await c.match("/")).body, "fresh /?view=tickets");
  assert.equal((await c.match("/settings")).body, "fresh /settings");
});

test("a /t/<n> navigation answers with the cached /t shell and refreshes /t, not a per-ticket entry", async () => {
  let answer;
  const w = load({ net: () => new Promise((r) => { answer = r; }) });
  const c = await w.caches.api.open("shell-abc");
  await c.put("/", res("cached root"));
  await c.put("/t", res("cached ticket"));
  const ev = fetchEvent("/t/42#answer", { mode: "navigate" });
  w.listeners.fetch(ev);
  assert.equal((await ev.responded).body, "cached ticket");
  answer(res("fresh ticket"));
  await Promise.all(ev.waits);
  assert.equal((await c.match("/t")).body, "fresh ticket");
  assert.equal(await c.match("/t/42"), undefined);
});

test("a redirect (signed out) or an error never replaces a cached page", async () => {
  const answers = [res("", { status: 0, type: "opaqueredirect" }), res("", { status: 302, redirected: true }), res("x", { status: 500 }),
    res("x", { cc: "no-store" })];
  const w = load({ net: async () => answers.shift() });
  const c = await w.caches.api.open("shell-abc");
  await c.put("/", res("cached root"));
  for (let i = 0; i < 4; i += 1) {
    const ev = fetchEvent("/", { mode: "navigate" });
    w.listeners.fetch(ev);
    assert.equal((await ev.responded).body, "cached root");
    await Promise.all(ev.waits);
  }
  assert.equal((await c.match("/")).body, "cached root");
});

test("a newer build's page is never cached by an older worker, and the open pages are told to update", async () => {
  let answer;
  const posted = [];
  const w = load({ build: "OLD", net: () => new Promise((r) => { answer = r; }), windows: [{ postMessage: (m) => posted.push(m) }] });
  const c = await w.caches.api.open("shell-OLD");
  await c.put("/", res("old root"));
  const ev = fetchEvent("/", { mode: "navigate" });
  w.listeners.fetch(ev);
  assert.equal((await ev.responded).body, "old root");
  answer(res("new root", { build: "NEW" }));
  await Promise.all(ev.waits);
  assert.equal((await c.match("/")).body, "old root", "the NEW page (with ?v=NEW assets) stays out of shell-OLD");
  assert.deepEqual(json(posted), [{ type: "new-build", build: "NEW" }]);
  // Uncached path: the NEW page is shown (network first) but still not stored; a page without X-Build neither.
  for (const [path, r] of [["/files", res("new files", { build: "NEW" })], ["/settings", res("unstamped", { build: null })]]) {
    const w2 = load({ build: "OLD", net: async () => r, windows: [] });
    const e2 = fetchEvent(path, { mode: "navigate" });
    w2.listeners.fetch(e2);
    assert.equal((await e2.responded).body, r.body);
    await Promise.all(e2.waits);
    assert.equal(w2.caches.stores.get("shell-OLD")?.size ?? 0, 0, path);
  }
});

test("cache-pages and install store only this build's pages", async () => {
  const w = load({ build: "OLD", net: async (url) => (url.startsWith("/static/precache.json") ? res(PRECACHE)
    : url === "/" ? res("new root", { build: "NEW" }) : res(`body of ${url}`)) });
  const ev = fetchEvent("/");
  w.listeners.install(ev);
  await Promise.all(ev.waits);
  const c = w.caches.stores.get("shell-OLD");
  assert.equal(c.has("/"), false);
  assert.equal(c.get("/settings").body, "body of /settings");
});

test("a legacy /?f= or /?tag= link asks the network (its redirect to Files), not the cached Needs you", async () => {
  const w = load({ net: async () => res("", { status: 0, type: "opaqueredirect" }) });
  await (await w.caches.api.open("shell-abc")).put("/", res("cached root"));
  for (const path of ["/?f=FILE7", "/?tag=x"]) {
    const ev = fetchEvent(path, { mode: "navigate" });
    w.listeners.fetch(ev);
    assert.equal((await ev.responded).type, "opaqueredirect", path);
  }
});

test("offline navigations fall back to the cached page, else the cached /", async () => {
  const w = load({ net: async () => { throw new TypeError("Failed to fetch"); } });
  const c = await w.caches.api.open("shell-abc");
  await c.put("/", res("cached root"));
  await c.put("/settings", res("cached settings"));
  for (const [path, want] of [["/settings", "cached settings"], ["/?f=FILE7", "cached root"], ["/devices", "cached root"]]) {
    const ev = fetchEvent(path, { mode: "navigate" });
    w.listeners.fetch(ev);
    assert.equal((await ev.responded).body, want, path);
  }
});

test("a no-store or redirected navigation is passed through but never cached", async () => {
  const w = load({ net: async (url) => (url.endsWith("/setup") ? res("setup", { cc: "no-store" })
    : res("", { status: 0, type: "opaqueredirect" })) });
  for (const path of ["/setup", "/settings"]) {
    const ev = fetchEvent(path, { mode: "navigate" });
    w.listeners.fetch(ev);
    await ev.responded;
    await Promise.all(ev.waits);
  }
  assert.equal(w.caches.stores.get("shell-abc")?.size ?? 0, 0);
});

test("a /pair navigation slower than 4 s never gets the / shell: the pair shell or the network", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const w = load({ net: () => new Promise(() => {}) }); // never answers
  const c = await w.caches.api.open("shell-abc");
  await c.put("/", res("cached root"));
  const ev = fetchEvent("/pair", { mode: "navigate" });
  w.listeners.fetch(ev);
  await new Promise((r) => setImmediate(r));
  t.mock.timers.tick(4000);
  let settled = false;
  ev.responded.then(() => { settled = true; });
  await new Promise((r) => setImmediate(r));
  assert.equal(settled, false, "no pair shell cached: it waits for the network, never answers with /");
  await c.put("/pair", res("cached pair"));
  const ev2 = fetchEvent("/pair", { mode: "navigate" });
  w.listeners.fetch(ev2);
  await new Promise((r) => setImmediate(r));
  t.mock.timers.tick(4000);
  assert.equal((await ev2.responded).body, "cached pair");
});

test("an offline /pair navigation gets the cached pair shell, never /", async () => {
  const w = load({ net: async () => { throw new TypeError("Failed to fetch"); } });
  const c = await w.caches.api.open("shell-abc");
  await c.put("/", res("cached root"));
  const ev = fetchEvent("/pair", { mode: "navigate" });
  w.listeners.fetch(ev);
  await assert.rejects(ev.responded, "nothing cached: the network's error, not the / shell");
  await c.put("/pair", res("cached pair"));
  const ev2 = fetchEvent("/pair", { mode: "navigate" });
  w.listeners.fetch(ev2);
  assert.equal((await ev2.responded).body, "cached pair");
});

test("a navigation without its own cached page, slower than 4 s, falls back to the cached /", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const w = load({ net: () => new Promise(() => {}) }); // never answers
  await (await w.caches.api.open("shell-abc")).put("/", res("cached root"));
  const ev = fetchEvent("/devices", { mode: "navigate" });
  w.listeners.fetch(ev);
  await new Promise((r) => setImmediate(r));
  t.mock.timers.tick(4000);
  assert.equal((await ev.responded).body, "cached root");
});

test("Background Sync only asks the open pages to flush", async () => {
  const w = load();
  const ev = { tag: "outbox-flush", waits: [], waitUntil(p) { this.waits.push(p); } };
  w.listeners.sync(ev);
  await Promise.all(ev.waits);
  assert.equal(JSON.stringify(w.posted), JSON.stringify([{ type: "outbox-flush" }]));
  assert.deepEqual(w.fetched, []);
});

test("cache-pages fills in the pages a signed-out install skipped, and only those", async () => {
  const w = load({ net: async (url) => res(`fresh ${url}`) });
  const c = await w.caches.api.open("shell-abc");
  await c.put("/static/precache.json", res(PRECACHE));
  await c.put("/", res("old root"));
  const ev = { data: { type: "cache-pages" }, waits: [], waitUntil(p) { this.waits.push(p); } };
  w.listeners.message(ev);
  await Promise.all(ev.waits);
  assert.deepEqual(w.fetched, ["/settings"]);
  assert.equal((await c.match("/settings")).body, "fresh /settings");
  assert.equal((await c.match("/")).body, "old root");
});

test("an asset stamped with another build skips the cache, and the answer isn't cached", async () => {
  const w = load({ build: "OLD", net: async () => res("new css") });
  const c = await w.caches.api.open("shell-OLD");
  await c.put("/static/css/app.css", res("old css"));
  const ev = fetchEvent("/static/css/app.css?v=NEW");
  w.listeners.fetch(ev);
  assert.equal((await ev.responded).body, "new css");
  assert.equal((await c.match("/static/css/app.css")).body, "old css");
  // This build's own stamp and an unstamped module import still come from the cache.
  for (const url of ["/static/css/app.css?v=OLD", "/static/css/app.css"]) {
    const e2 = fetchEvent(url);
    w.listeners.fetch(e2);
    assert.equal((await e2.responded).body, "old css", url);
  }
  assert.equal(w.fetched.length, 1);
});

// ---- tickets (spec T7, T10): push notifications, clicks, and offline deep links

const json = (x) => JSON.parse(JSON.stringify(x)); // objects made inside the vm have another realm's prototypes

function pushEvent(payload) {
  const ev = { waits: [], waitUntil(p) { this.waits.push(p); } };
  ev.data = payload === undefined ? null : { json: () => (typeof payload === "string" ? JSON.parse(payload) : payload) };
  return ev;
}

const S1 = "1".repeat(32), S2 = "2".repeat(32), S3 = "3".repeat(32), S4 = "4".repeat(32);
const LABELS = { labels: { [S1]: { label: "Acme Energy", titles: true }, [S2]: "Orchestrator" } };

async function push(w, payload) {
  const ev = pushEvent(payload);
  w.listeners.push(ev);
  await Promise.all(ev.waits);
  return json(w.shown.at(-1));
}

test("push v2: a question names the workspace from the label cache, tags it per workspace, opens #answer", async () => {
  const w = load({ idb: LABELS });
  const got = await push(w, { v: 2, s: S1, t: "TIX-42", k: "question", n: 2, c: 2 });
  assert.equal(got.title, "Agent needs input");
  assert.equal(got.options.body, "Acme Energy · 2 questions");
  assert.equal(got.options.tag, `tix:${S1}`);
  assert.equal(got.options.renotify, true);
  assert.equal(got.options.data.url, "/t/42#answer");
  assert.equal(got.options.icon, "/static/img/icon-192.png");
});

test("push v2: the wording of each kind, and an unknown space reads 'a workspace'", async () => {
  const w = load({ idb: LABELS });
  const cases = [
    [{ v: 2, s: S2, t: "TIX-7", k: "approval", n: 0, c: 1 }, "Plan ready for review", "Orchestrator · TIX-7"],
    [{ v: 2, s: S2, t: "TIX-8", k: "verdict", n: 0, c: 1 }, "Ready for your verdict", "Orchestrator · TIX-8"],
    [{ v: 2, s: S3, t: "TIX-9", k: "question", n: 1, c: 1 }, "Agent needs input", "a workspace · 1 question"],
    [{ v: 2, s: "", t: "", k: "message", n: 3, c: 3 }, "Message from agent", "3 messages"],
  ];
  for (const [payload, title, body] of cases) {
    const got = await push(w, payload);
    assert.equal(got.title, title);
    assert.equal(got.options.body, body);
  }
});

test("push v2: other items needing you show as '+N more'", async () => {
  const w = load({ idb: LABELS });
  const got = await push(w, { v: 2, s: S1, t: "TIX-42", k: "question", n: 2, c: 5 });
  assert.equal(got.options.body, "Acme Energy · 2 questions\n+3 more");
});

test("push v2: past three workspaces one 'tix:all' notification takes over", async () => {
  const w = load({ idb: LABELS });
  for (const s of [S1, S2, S3]) await push(w, { v: 2, s, t: "TIX-1", k: "question", n: 1, c: 1 });
  assert.equal(w.notifications.length, 3);
  const got = await push(w, { v: 2, s: S4, t: "TIX-4", k: "question", n: 1, c: 4 });
  assert.equal(got.options.tag, "tix:all");
  assert.equal(got.options.body, "4 workspaces need you");
  assert.deepEqual(w.notifications.map((n) => n.tag), ["tix:all"]);
});

test("push v2: clear replaces the workspace's notification silently", async () => {
  const w = load({ idb: LABELS });
  await push(w, { v: 2, s: S1, t: "TIX-42", k: "question", n: 2, c: 2 });
  const got = await push(w, { v: 2, s: S1, t: "TIX-42", k: "clear", n: 0, c: 0 });
  assert.equal(got.options.tag, `tix:${S1}`);
  assert.equal(got.options.silent, true);
  assert.equal(got.options.renotify, false);
  assert.equal(got.options.body, "Handled on desktop · Acme Energy");
  assert.equal(w.notifications.length, 1);
});

test("push v2: clear keeps the workspace's notification while another ticket there still needs you", async () => {
  const w = load({ idb: LABELS });
  await push(w, { v: 2, s: S1, t: "TIX-42", k: "question", n: 1, c: 1 });
  await push(w, { v: 2, s: S1, t: "TIX-43", k: "approval", n: 0, c: 2 });
  const kept = await push(w, { v: 2, s: S1, t: "TIX-42", k: "clear", n: 0, c: 1 });
  assert.equal(kept.options.tag, `tix:${S1}`);
  assert.equal(kept.options.silent, true);
  assert.notEqual(kept.options.body, "Handled on desktop · Acme Energy");
  assert.deepEqual(kept.options.data.ts, ["TIX-43"]);
  const gone = await push(w, { v: 2, s: S1, t: "TIX-43", k: "clear", n: 0, c: 0 });
  assert.equal(gone.options.body, "Handled on desktop · Acme Energy");
  assert.equal(w.notifications.length, 1);
});

test("push v2: clear updates the 'tix:all' group: a workspace leaves it, the last one closes it", async () => {
  const w = load({ idb: LABELS });
  for (const [s, t] of [[S1, "TIX-1"], [S2, "TIX-2"], [S3, "TIX-3"], [S4, "TIX-4"]]) {
    await push(w, { v: 2, s, t, k: "question", n: 1, c: 4 });
  }
  assert.deepEqual(w.notifications.map((n) => n.tag), ["tix:all"]);
  const three = await push(w, { v: 2, s: S4, t: "TIX-4", k: "clear", n: 0, c: 3 });
  assert.equal(three.options.tag, "tix:all");
  assert.equal(three.options.silent, true);
  assert.equal(three.options.body, "3 workspaces need you");
  assert.deepEqual([...three.options.data.spaces].sort(), [S1, S2, S3]);
  for (const [s, t] of [[S1, "TIX-1"], [S2, "TIX-2"]]) await push(w, { v: 2, s, t, k: "clear", n: 0, c: 1 });
  const one = json(w.shown.at(-1));
  assert.equal(one.options.body, "1 workspace needs you");
  const last = await push(w, { v: 2, s: S3, t: "TIX-3", k: "clear", n: 0, c: 0 });
  assert.equal(last.options.tag, "tix:all");
  assert.equal(last.options.body, "Handled on desktop");
  assert.equal(w.notifications.length, 1);
});

test("push v2: a join request names the device from the session's list; its clear has an empty t", async () => {
  const w = load({ idb: LABELS, net: async (url) => (url === "/api/join-requests"
    ? res(JSON.stringify({ requests: [{ space: S1, device_name: "mac-mini" }] })) : res("", { status: 404 })) });
  const got = await push(w, { v: 2, s: S1, t: "", k: "join", n: 1, c: 1 });
  assert.equal(got.title, "Device wants to sync");
  assert.equal(got.options.body, "mac-mini wants to sync Acme Energy");
  assert.equal(got.options.tag, `tix:join:${S1}`);
  assert.equal(got.options.data.url, "/settings#join");
  const cleared = await push(w, { v: 2, s: S1, t: "", k: "clear", n: 0, c: 0 });
  assert.equal(cleared.options.tag, `tix:join:${S1}`);
  assert.equal(cleared.options.silent, true);
});

test("push v2: with the titles switch off, no mirror is fetched", async () => {
  const w = load({ idb: LABELS });
  await push(w, { v: 2, s: S1, t: "TIX-42", k: "question", n: 2, c: 2 });
  assert.deepEqual(w.fetched, []);
  assert.ok(!w.reads.some(([store]) => store === "keys"));
});

test("push v2: with the titles switch on, a failure to open the mirror falls back to the label text", async () => {
  const w = load({ idb: { ...LABELS, prefs: { show_titles: true }, keys: { current: { mk: "not a key" } } },
    net: async () => res("", { status: 500 }) });
  const got = await push(w, { v: 2, s: S1, t: "TIX-42", k: "question", n: 2, c: 2 });
  assert.deepEqual(w.fetched, ["/api/mirrors/TIX-42"]);
  assert.equal(got.options.body, "Acme Energy · 2 questions");
});

test("push v2: with the titles switch on, the worker opens the mirror with the stored MK and shows the title", async () => {
  const VEC = JSON.parse(readFileSync(new URL("../vectors/mirror1.json", import.meta.url), "utf8"));
  const m = VEC.cases.find((x) => x.name === "mirror");
  const mk = await globalThis.crypto.subtle.importKey("raw", Buffer.from(VEC.mk, "hex"), "AES-GCM", false, ["encrypt", "decrypt"]);
  // The row as the server sends it: its space and uuid are bound to the doc (sha256(space|id|gen)[:16]).
  const row = { uuid: m.ticket_uuid, space: VEC.space_id, wrapped_dek: VEC.wrapped_dek.env, enc_content: m.env };
  const labels = { labels: { [VEC.space_id]: { label: "Acme Energy", titles: true } } };
  const w = load({ idb: { ...labels, prefs: { show_titles: true }, keys: { current: { mk } } },
    net: async () => res(JSON.stringify(row)) });
  const got = await push(w, { v: 2, s: VEC.space_id, t: "TIX-42", k: "question", n: 1, c: 1 });
  assert.equal(got.options.body, `Acme Energy · ${m.obj.title}`);
});

test("push v2: with the switch on, a copy older than this phone's high-water mark shows no title", async () => {
  const VEC = JSON.parse(readFileSync(new URL("../vectors/mirror1.json", import.meta.url), "utf8"));
  const m = VEC.cases.find((x) => x.name === "mirror");
  assert.ok(Number.isInteger(m.obj.mirror_rev) && Number.isInteger(m.obj.gen), "the vector doc carries gen and mirror_rev");
  const mk = await globalThis.crypto.subtle.importKey("raw", Buffer.from(VEC.mk, "hex"), "AES-GCM", false, ["encrypt", "decrypt"]);
  const row = { uuid: m.ticket_uuid, space: VEC.space_id, wrapped_dek: VEC.wrapped_dek.env, enc_content: m.env };
  const seen = { [`${VEC.space_id}|${m.obj.id}`]: { gen: m.obj.gen, mirror_rev: m.obj.mirror_rev + 1 } };
  const w = load({ idb: { labels: { [VEC.space_id]: { label: "Acme Energy", titles: true } }, prefs: { show_titles: true },
    keys: { current: { mk } }, seen },
    net: async () => res(JSON.stringify(row)) });
  const got = await push(w, { v: 2, s: VEC.space_id, t: "TIX-42", k: "question", n: 1, c: 1 });
  assert.equal(got.options.body, "Acme Energy · 1 question");
  assert.ok(w.reads.some(([store, key]) => store === "seen" && key === `${VEC.space_id}|${m.obj.id}`));
});

test("push v2: a title only after the routing binding check; else the space-only text", async () => {
  const VEC = JSON.parse(readFileSync(new URL("../vectors/mirror1.json", import.meta.url), "utf8"));
  const m = VEC.cases.find((x) => x.name === "mirror");
  const mk = await globalThis.crypto.subtle.importKey("raw", Buffer.from(VEC.mk, "hex"), "AES-GCM", false, ["encrypt", "decrypt"]);
  const B = "b".repeat(32);
  const labels = { labels: { [VEC.space_id]: { label: "Acme Energy", titles: true }, [B]: { label: "Other", titles: true } } };
  const base = { uuid: m.ticket_uuid, space: VEC.space_id, wrapped_dek: VEC.wrapped_dek.env, enc_content: m.env };
  for (const [row, s, why] of [
    [{ ...base, space: B }, B, "the server moved the mirror to another space"],
    [base, B, "the push names another space than the mirror"],
    [{ ...base, space: undefined }, VEC.space_id, "no space to check"],
  ]) {
    const w = load({ idb: { ...labels, prefs: { show_titles: true }, keys: { current: { mk } } },
      net: async () => res(JSON.stringify(row)) });
    const got = await push(w, { v: 2, s, t: "TIX-42", k: "question", n: 1, c: 1 });
    assert.ok(!got.options.body.includes(m.obj.title), why);
    assert.match(got.options.body, / · 1 question$/, why);
  }
});

test("push v2: with the switch on, a space without titles (key-only) is never fetched", async () => {
  const w = load({ idb: { labels: { [S1]: { label: "Acme Energy", titles: false } }, prefs: { show_titles: true } } });
  await push(w, { v: 2, s: S1, t: "TIX-42", k: "question", n: 2, c: 2 });
  assert.deepEqual(w.fetched, []);
});

test("push: a malformed or v1 payload still shows a generic notification, never a foreign URL", async () => {
  const w = load();
  for (const payload of [undefined, "not json", { t: "https://evil.example/", s: "waiting", p: "x", n: 1 },
    { v: 2, s: S1, t: "https://evil.example/", k: "question", n: 1, c: 1 }, { v: 2, s: S1, t: "TIX-1", k: "bogus" }]) {
    const ev = pushEvent(payload);
    if (payload === "not json") ev.data = { json: () => { throw new SyntaxError("bad"); } };
    w.listeners.push(ev);
    await Promise.all(ev.waits);
    const got = json(w.shown.pop());
    assert.ok(got.title);
    assert.equal(got.options.data.url, "/");
  }
});

function clickEvent(url) {
  const ev = { waits: [], waitUntil(p) { this.waits.push(p); }, closed: false };
  ev.notification = { data: url === undefined ? null : { url }, close() { ev.closed = true; } };
  return ev;
}

test("notificationclick focuses an open window and navigates it to the ticket's decision card (#answer)", async () => {
  const log = [];
  const win = { url: `${ORIGIN}/`, focus: async () => { log.push("focus"); return win; },
    navigate: async (u) => { log.push(`navigate ${u}`); return win; } };
  const w = load({ windows: [win] });
  const ev = clickEvent("/t/42#answer");
  w.listeners.notificationclick(ev);
  await Promise.all(ev.waits);
  assert.equal(ev.closed, true);
  assert.deepEqual(log, ["focus", `navigate ${ORIGIN}/t/42#answer`]);
  assert.deepEqual(w.opened, []);
});

test("notificationclick opens a window when none is open, and only ever for this origin", async () => {
  const w = load({ windows: [] });
  const ev = clickEvent("/t/42#answer");
  w.listeners.notificationclick(ev);
  await Promise.all(ev.waits);
  assert.deepEqual(w.opened, [`${ORIGIN}/t/42#answer`]);
  const evil = clickEvent("https://evil.example/phish");
  w.listeners.notificationclick(evil);
  await Promise.all(evil.waits);
  assert.deepEqual(w.opened, [`${ORIGIN}/t/42#answer`, `${ORIGIN}/`]);
  // an older notification already carrying #answer keeps it once; another fragment is refused
  for (const [raw, want] of [["/t/9", `${ORIGIN}/t/9#answer`], ["/t/9#x", `${ORIGIN}/`],
    ["/settings#join", `${ORIGIN}/settings#join`], ["/tickets/TIX-9", `${ORIGIN}/`]]) {
    const e = clickEvent(raw);
    w.listeners.notificationclick(e);
    await Promise.all(e.waits);
    assert.equal(w.opened.at(-1), want);
  }
});

test("notificationclick opens a window when the open one can't be navigated", async () => {
  const win = { url: `${ORIGIN}/`, focus: async () => win, navigate: async () => { throw new TypeError("not controlled"); } };
  const w = load({ windows: [win] });
  const ev = clickEvent("/t/3#answer");
  w.listeners.notificationclick(ev);
  await Promise.all(ev.waits);
  assert.deepEqual(w.opened, [`${ORIGIN}/t/3#answer`]);
});

test("an offline /t/<n> navigation gets the cached /t ticket shell, not Needs you", async () => {
  const w = load({ net: async () => { throw new TypeError("Failed to fetch"); } });
  const c = await w.caches.api.open("shell-abc");
  await c.put("/", res("cached root"));
  await c.put("/t", res("cached ticket"));
  for (const [path, want] of [["/t/42", "cached ticket"], ["/t/x", "cached root"], ["/tickets", "cached root"]]) {
    const ev = fetchEvent(path, { mode: "navigate" });
    w.listeners.fetch(ev);
    assert.equal((await ev.responded).body, want, path);
  }
});

test("an offline /sandbox/html navigation never gets the / shell (spec T15)", async () => {
  const w = load({ net: async () => { throw new TypeError("Failed to fetch"); } });
  const c = await w.caches.api.open("shell-abc");
  await c.put("/", res("cached root"));
  const miss = fetchEvent("/sandbox/html", { mode: "navigate" });
  w.listeners.fetch(miss);
  await assert.rejects(miss.responded, TypeError); // the network's own failure, not Files
  await c.put("/sandbox/html", res("cached sandbox"));
  const hit = fetchEvent("/sandbox/html", { mode: "navigate" });
  w.listeners.fetch(hit);
  assert.equal((await hit.responded).body, "cached sandbox");
});

test("an offline /sandbox/widget navigation gets its own cached copy, never the / shell", async () => {
  const w = load({ net: async () => { throw new TypeError("Failed to fetch"); } });
  const c = await w.caches.api.open("shell-abc");
  await c.put("/", res("cached root"));
  const miss = fetchEvent("/sandbox/widget", { mode: "navigate" });
  w.listeners.fetch(miss);
  await assert.rejects(miss.responded, TypeError);
  await c.put("/sandbox/widget", res("cached widget frame"));
  const hit = fetchEvent("/sandbox/widget", { mode: "navigate" });
  w.listeners.fetch(hit);
  assert.equal((await hit.responded).body, "cached widget frame");
});

test("a cached /sandbox/html answers with its own copy at once, never another page's", async () => {
  const w = load({ net: () => new Promise(() => {}) });
  const c = await w.caches.api.open("shell-abc");
  await c.put("/", res("cached root"));
  await c.put("/sandbox/html", res("cached sandbox"));
  const ev = fetchEvent("/sandbox/html", { mode: "navigate" });
  w.listeners.fetch(ev);
  assert.equal((await ev.responded).body, "cached sandbox");
});

test("a slow /sandbox/html navigation never gets the / shell either", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let answer;
  const w = load({ net: () => new Promise((r) => { answer = r; }) });
  await (await w.caches.api.open("shell-abc")).put("/", res("cached root"));
  const ev = fetchEvent("/sandbox/html", { mode: "navigate" });
  w.listeners.fetch(ev);
  await new Promise((r) => setImmediate(r));
  t.mock.timers.tick(4000);
  await new Promise((r) => setImmediate(r));
  answer(res("net sandbox"));
  assert.equal((await ev.responded).body, "net sandbox");
});

// ---- feedback round B: the server folds a burst into one "batch" push per workspace
test("push v2: a batch reads 'N new things need you in <workspace>' and remembers its tickets for later clears", async () => {
  const w = load({ idb: LABELS });
  const got = await push(w, { v: 2, s: S1, t: "", k: "batch", n: 9, c: 10, ts: ["TIX-2", "TIX-3", "bad", "TIX-4"] });
  assert.equal(got.title, "Needs you");
  assert.equal(got.options.body, "9 new things need you in Acme Energy");
  assert.equal(got.options.tag, `tix:${S1}`);
  assert.equal(got.options.data.url, "/");
  assert.deepEqual(got.options.data.ts, ["TIX-2", "TIX-3", "TIX-4"]);
  const kept = await push(w, { v: 2, s: S1, t: "TIX-3", k: "clear", n: 0, c: 2 });
  assert.equal(kept.options.silent, true);
  assert.deepEqual(kept.options.data.ts, ["TIX-2", "TIX-4"]);                 // the others still need you
});


test("push v2: a batch joins the workspace grouping: past three workspaces it goes into 'tix:all'", async () => {
  const w = load({ idb: LABELS });
  for (const s of [S1, S2]) await push(w, { v: 2, s, t: "TIX-1", k: "question", n: 1, c: 1 });
  const S4 = "4".repeat(32);
  await push(w, { v: 2, s: S3, t: "", k: "batch", n: 2, c: 4, ts: ["TIX-5", "TIX-6"] });
  assert.equal(w.notifications.length, 3);
  const got = await push(w, { v: 2, s: S4, t: "", k: "batch", n: 2, c: 6, ts: ["TIX-7", "TIX-8"] });
  assert.equal(got.options.tag, "tix:all");                                    // the cap holds
  assert.equal(w.notifications.length, 1);
  assert.deepEqual(got.options.data.ts[S4], ["TIX-7", "TIX-8"]);
  const after = await push(w, { v: 2, s: S4, t: "TIX-7", k: "clear", n: 0, c: 5 });
  assert.deepEqual(after.options.data.ts[S4], ["TIX-8"]);                      // the clear updates the group
});

test("push v2: a message has its own tag, so it never replaces a needs notification, and its clear never touches one", async () => {
  const w = load({ idb: LABELS });
  await push(w, { v: 2, s: S1, t: "TIX-42", k: "question", n: 1, c: 1, cs: 1 });
  const msg = await push(w, { v: 2, s: S1, t: "", k: "message", n: 2, c: 3 });
  assert.equal(msg.options.tag, `tix:msg:${S1}`);
  assert.deepEqual(w.notifications.map((n) => n.tag).sort(), [`tix:${S1}`, `tix:msg:${S1}`].sort());     // both visible
  assert.equal(w.notifications.find((n) => n.tag === `tix:${S1}`).title, "Agent needs input");
  const got = await push(w, { v: 2, s: S1, t: "", k: "clear", w: "message", n: 0, c: 1 });
  assert.equal(got.options.tag, `tix:msg:${S1}`);
  assert.equal(got.title, "Read on desktop");
  assert.equal(got.options.body, "Read on desktop · Acme Energy");
  assert.equal(w.notifications.find((n) => n.tag === `tix:${S1}`).title, "Agent needs input");           // untouched
  const none = await push(w, { v: 2, s: "", t: "", k: "message", n: 1, c: 1 });
  assert.equal(none.options.tag, "tix:msg");
});

test("push v2: a message does not count as a ticket of its workspace for the 'tix:all' grouping", async () => {
  const w = load({ idb: LABELS });
  for (const s of [S1, S2, S3]) await push(w, { v: 2, s, t: "", k: "message", n: 1, c: 3 });
  assert.equal(w.notifications.length, 3);
  const q = await push(w, { v: 2, s: S4, t: "TIX-1", k: "question", n: 1, c: 4, cs: 1 });
  assert.equal(q.options.tag, `tix:${S4}`);                                                                // not folded into tix:all
});

test("push v2: the app badge follows c, and a clear to 0 removes it", async () => {
  const w = load({ idb: LABELS });
  const badge = [];
  w.self.navigator = { setAppBadge: async (n) => badge.push(n), clearAppBadge: async () => badge.push(0) };
  await push(w, { v: 2, s: S1, t: "TIX-42", k: "question", n: 1, c: 3 });
  await push(w, { v: 2, s: S1, t: "TIX-42", k: "clear", n: 0, c: 0 });
  assert.deepEqual(badge, [3, 0]);
});

test("pushsubscriptionchange subscribes again and replaces the server row", async () => {
  const calls = [];
  const w = load({ net: async (url, init) => {
    calls.push([init?.method || "GET", url]);
    return url === "/api/push/vapid" ? res(JSON.stringify({ public_key: "BAAA" })) : res("");
  } });
  const sub = { toJSON: () => ({ endpoint: "https://push/new", keys: { p256dh: "p", auth: "a" } }) };
  const ev = { newSubscription: sub, oldSubscription: { endpoint: "https://push/old" }, waits: [], waitUntil(p) { this.waits.push(p); } };
  w.listeners.pushsubscriptionchange(ev);
  await Promise.all(ev.waits);
  assert.deepEqual(calls.map((c) => c.join(" ")), ["GET /api/push/vapid", "POST /api/push/subscribe", "DELETE /api/push/subscribe"]);
});

// ---- the clear decides per workspace (`cs`), not from the global count or a remembered ticket list

test("push v2: a clear with cs 0 is 'Handled' even though the global count is above 0 and the remembered list is stale", async () => {
  const w = load({ idb: LABELS });
  // earlier pushes left TIX-1 and TIX-2 on the notification; their clears were missed
  await push(w, { v: 2, s: S1, t: "TIX-1", k: "question", n: 1, c: 1, cs: 1 });
  await push(w, { v: 2, s: S1, t: "TIX-2", k: "question", n: 1, c: 2, cs: 2 });
  const got = await push(w, { v: 2, s: S1, t: "TIX-3", k: "clear", n: 0, c: 3, cs: 0 });   // 3 needs elsewhere
  assert.equal(got.title, "Handled on desktop");
  assert.equal(got.options.body, "Handled on desktop · Acme Energy");
  assert.equal(w.notifications.length, 1);
});

test("push v2: a clear with cs 2 re-shows the notification quietly with the honest remaining count", async () => {
  const w = load({ idb: LABELS });
  await push(w, { v: 2, s: S1, t: "TIX-1", k: "question", n: 1, c: 1, cs: 1 });
  const got = await push(w, { v: 2, s: S1, t: "TIX-1", k: "clear", n: 0, c: 2, cs: 2 });
  assert.equal(got.options.silent, true);
  assert.equal(got.options.body, "2 still need you · Acme Energy");
  const one = await push(w, { v: 2, s: S1, t: "TIX-1", k: "clear", n: 0, c: 1, cs: 1 });
  assert.equal(one.options.body, "1 still needs you · Acme Energy");
});

test("push v2: the tix:all group drops a workspace whose cs is 0, whatever the global count says", async () => {
  const w = load({ idb: LABELS });
  for (const [s, t] of [[S1, "TIX-1"], [S2, "TIX-2"], [S3, "TIX-3"], [S4, "TIX-4"]]) {
    await push(w, { v: 2, s, t, k: "question", n: 1, c: 4, cs: 1 });
  }
  const three = await push(w, { v: 2, s: S4, t: "TIX-4", k: "clear", n: 0, c: 3, cs: 0 });
  assert.equal(three.options.body, "3 workspaces need you");
  const stay = await push(w, { v: 2, s: S3, t: "TIX-3", k: "clear", n: 0, c: 3, cs: 1 });   // still needs you
  assert.equal(stay.options.body, "3 workspaces need you");
  for (const s of [S1, S2, S3]) await push(w, { v: 2, s, t: "TIX-9", k: "clear", n: 0, c: 3, cs: 0 });
  assert.equal(json(w.shown.at(-1)).options.body, "Handled on desktop");     // the last one, with c still 3
});

test("needs-spaces from the page closes the notifications of workspaces with nothing left", async () => {
  const w = load({ idb: LABELS });
  await push(w, { v: 2, s: S1, t: "TIX-1", k: "question", n: 1, c: 2, cs: 1 });
  await push(w, { v: 2, s: S2, t: "TIX-2", k: "approval", n: 0, c: 2, cs: 1 });
  assert.equal(w.notifications.length, 2);
  const ev = { data: { type: "needs-spaces", spaces: [S2] }, waits: [], waitUntil(p) { this.waits.push(p); } };
  w.listeners.message(ev);
  await Promise.all(ev.waits);
  assert.deepEqual(w.notifications.map((n) => n.tag), [`tix:${S2}`]);
});

test("push v2: reading a message on the desktop is titled 'Read on desktop', not 'Handled on desktop'", async () => {
  const w = load({ idb: LABELS });
  await push(w, { v: 2, s: S1, t: "", k: "message", n: 1, c: 1 });
  const got = await push(w, { v: 2, s: S1, t: "", k: "clear", w: "message", n: 0, c: 0, cs: 0 });
  assert.equal(got.title, "Read on desktop");
  assert.equal(got.options.body, "Read on desktop · Acme Energy");
});

test("needs-spaces: handled tickets, read messages and old 'Handled' notes leave the lock screen when the app opens", async () => {
  const w = load({ idb: LABELS });
  await push(w, { v: 2, s: S1, t: "TIX-1", k: "question", n: 1, c: 3, cs: 1 });
  await push(w, { v: 2, s: S2, t: "", k: "message", n: 1, c: 3 });
  await push(w, { v: 2, s: S3, t: "TIX-9", k: "approval", n: 0, c: 3, cs: 1 });
  await push(w, { v: 2, s: S3, t: "TIX-9", k: "clear", n: 0, c: 2, cs: 0 });          // an old "Handled" note
  const ev = { data: { type: "needs-spaces", spaces: [S1], tickets: [`${S1}|TIX-1`], messages: 0 }, waits: [], waitUntil(p) { this.waits.push(p); } };
  w.listeners.message(ev);
  await Promise.all(ev.waits);
  assert.deepEqual(w.notifications.map((n) => n.tag), [`tix:${S1}`]);                 // the question is still open
  const ev2 = { data: { type: "needs-spaces", spaces: [S1], tickets: [`${S1}|TIX-7`], messages: 0 }, waits: [], waitUntil(p) { this.waits.push(p); } };
  w.listeners.message(ev2);
  await Promise.all(ev2.waits);
  assert.equal(w.notifications.length, 0);                                           // TIX-1 was handled meanwhile
});

test("needs-spaces never closes what the list cannot know yet: a notification newer than the request, or messages it could not read", async () => {
  const w = load({ idb: LABELS });
  await push(w, { v: 2, s: S1, t: "TIX-1", k: "question", n: 1, c: 2, cs: 1 });
  await push(w, { v: 2, s: S2, t: "", k: "message", n: 1, c: 2 });
  w.notifications.find((n) => n.tag === `tix:${S1}`).timestamp = 2000;          // shown after the page asked (at 1000)
  const ask = async (data) => { const ev = { data: { type: "needs-spaces", ...data }, waits: [], waitUntil(p) { this.waits.push(p); } };
    w.listeners.message(ev); await Promise.all(ev.waits); };
  await ask({ spaces: [], tickets: [], messages: null, at: 1000 });              // unread count unreadable, S1 is newer
  assert.deepEqual(w.notifications.map((n) => n.tag).sort(), [`tix:${S1}`, `tix:${S2}`].sort());
  await ask({ spaces: [], tickets: [], messages: 0, at: 3000 });                 // now both are older than the request
  assert.equal(w.notifications.length, 0);
});
