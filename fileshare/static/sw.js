// The service worker (spec §16): it makes the app shell open offline. Served raw from /sw.js with
// scope "/"; the page registers it as /sw.js?v=<BUILD>, so a deploy is a new worker with a new cache.
//
// - install: precache every page (as a navigation) and every shell asset from /static/precache.json
//   into "shell-<BUILD>", then skipWaiting. activate: delete older shell-* caches, clients.claim.
// - navigations: stale-while-revalidate. The cached page for that path (/t/<n>: the "/t" ticket shell)
//   answers at once and the network refreshes that entry in the background (event.waitUntil); a
//   redirect (signed out) never replaces it. No cached page (or a legacy /?f= / /?tag= link): network
//   first (4 s), else the cached page, else the cached "/" (never for /sandbox/html: that is the
//   network's answer or its own cached copy only). /pair stays network first: its own shell only.
//   A page is cached only when its X-Build is this worker's build; a newer build's page is not, and the
//   open pages get "new-build" so they register that build's worker at once (swreg.js).
// - static assets: cache first; a ?v= of another build goes to the network and is not cached.
// - bypassed entirely (no respondWith): non-GET, cross-origin, /api/, /p/, /u/, /skill/, /onboarding*
//   and /sw.js. They never touch the cache.
// - never cached: a response with Cache-Control no-store, an opaque or cross-origin response, a
//   redirect, or an error status.
// - message "cache-pages" (sent by signed-in pages): cache the pages a signed-out install skipped.
// - Background Sync "outbox-flush" only tells the open pages to flush; the worker uploads nothing.
// - push (TIX on orch-core, spec §7): the cleartext payload v2 {v, s, t, k, n, c} (k "batch": a burst the server
//   folded, with ts = its TIX ids) becomes one
//   notification per workspace (tag "tix:<space>", "tix:all" past 3 workspaces); the space label
//   comes from the IndexedDB "labels" cache the pages keep. k "clear" replaces it silently. With the
//   "Show ticket titles in notifications" pref on (IndexedDB "prefs", off by default) the worker
//   opens the mirror with the MK it can reach in IndexedDB and shows the title. notificationclick
//   focuses an open window and navigates it to /t/<n>#answer, else opens one. Neither touches the cache.
"use strict";

const BUILD = new URL(self.location.href).searchParams.get("v") || "dev";
const CACHE = `shell-${BUILD}`;
const NAV_TIMEOUT_MS = 4000;
const SYNC_TAG = "outbox-flush";
const BYPASS = /^\/(?:api|p|u|skill)\/|^\/onboarding|^\/sw\.js$/;

function bypassed(request) {
  if (request.method !== "GET") return true;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return true;
  return BYPASS.test(url.pathname);
}

function cacheable(response) {
  if (!response || !response.ok || response.type !== "basic" || response.redirected) return false;
  return !/(^|,)\s*no-store\s*(,|$)/i.test(response.headers.get("Cache-Control") || "");
}

// A page is this build's when the server says so (X-Build, pages.render_page). Another build's page
// stamps ?v=<its build> on its assets, which this build's cache doesn't hold: it is never cached here.
const STAMP = /^[A-Za-z0-9._-]{1,40}$/;
const pageBuild = (response) => response.headers.get("X-Build") || "";
const cacheablePage = (response) => cacheable(response) && pageBuild(response) === BUILD;

// A newer build answered a navigation: the open pages register its worker now (swreg.js), so it installs
// on this very open instead of the next one.
async function announceBuild(build) {
  if (!STAMP.test(build) || build === BUILD) return;
  for (const c of await self.clients.matchAll({ type: "window", includeUncontrolled: true })) {
    c.postMessage({ type: "new-build", build });
  }
}

const LIST = "/static/precache.json";

async function precache() {
  const cache = await caches.open(CACHE);
  const res = await fetch(`${LIST}?v=${encodeURIComponent(BUILD)}`, { cache: "no-cache" });
  if (!res.ok) throw new Error(`precache list: ${res.status}`);
  const { pages, assets } = await res.clone().json();
  await cache.put(LIST, res);
  // Assets must all be there, or the install fails and the old worker stays.
  await Promise.all(assets.map(async (path) => {
    const r = await fetch(path, { cache: "no-cache" });
    if (!cacheable(r)) throw new Error(`precache ${path}: ${r.status}`);
    await cache.put(path, r);
  }));
  await cachePages(cache, pages);
}

// Pages are best effort: signed out (the login page registers the worker too), /settings redirects
// to /login. A signed-in page later sends "cache-pages", which fills in the missing ones.
async function cachePages(cache, pages, { onlyMissing = false } = {}) {
  await Promise.all(pages.map(async (path) => {
    try {
      if (onlyMissing && (await cache.match(path))) return;
      const r = await fetch(path, { cache: "no-cache", credentials: "same-origin", redirect: "manual" });
      if (cacheablePage(r)) await cache.put(path, r);
    } catch {
      /* offline: the page is cached on its next online navigation */
    }
  }));
}

self.addEventListener("install", (event) => {
  event.waitUntil(precache().then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name.startsWith("shell-") && name !== CACHE) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

async function fromCache(key, options) {
  const cache = await caches.open(CACHE);
  return cache.match(key, options);
}

// The cache key a navigation's page lives under: /t/<n> is the ticket shell, precached as /t (the same
// page for every number), everything else its own path.
const shellKey = (path) => (/^\/t\/[0-9]{1,12}$/.test(path) ? "/t" : path);

async function navigate(event) {
  const url = new URL(event.request.url);
  // /pair carries a pairing key in its fragment. It never gets another page's shell (that page's
  // scripts would see the key in location): the network, else its own precached shell.
  if (url.pathname === "/pair") return pairPage(event);
  const key = shellKey(url.pathname);
  const network = fetch(event.request).then(async (response) => {
    // cacheable() refuses a redirect (signed out: /login), so it never replaces a cached shell; another
    // build's page is never stored in this build's cache, and the pages are told to fetch its worker.
    if (cacheablePage(response)) {
      const cache = await caches.open(CACHE);
      await cache.put(key, response.clone());
    } else if (cacheable(response)) {
      await announceBuild(pageBuild(response));
    }
    return response;
  });
  event.waitUntil(network.catch(() => {}));
  // Stale-while-revalidate: this page's own cached shell answers at once and the network refreshes it
  // for the next open. The shells hold no data (their scripts load it and send a signed-out browser
  // to login). A pre-Task-9 link (/?f=FILE7, /?tag=…) is a server redirect to Files, so it asks the network.
  const legacy = url.pathname === "/" && (url.searchParams.has("f") || url.searchParams.has("tag"));
  const hit = legacy ? null : (await fromCache(url.pathname)) || (key !== url.pathname ? await fromCache(key) : null);
  if (hit) return hit;
  // No shell of its own: network first (4 s), then the fallbacks.
  let timer;
  const timeout = new Promise((resolve) => { timer = setTimeout(() => resolve(null), NAV_TIMEOUT_MS); });
  try {
    const first = await Promise.race([network, timeout]);
    if (first) return first;
  } catch {
    /* offline or the server is unreachable: fall back to the shell */
  } finally {
    clearTimeout(timer);
  }
  // The sandbox frame (spec T15) is its own document: the network's answer or its own precached
  // copy, never the Files shell inside an attachment preview.
  if (url.pathname === "/sandbox/html") return (await fromCache(url.pathname)) || network;
  // /t/42 is served by the ticket page, so offline it gets that shell (precached as /t), not Needs you.
  const cached = (await fromCache(url.pathname)) || (key !== url.pathname ? await fromCache(key) : null) || (await fromCache("/"));
  if (cached) return cached;
  return network; // nothing cached yet: whatever the network finally does
}

async function pairPage(event) {
  const network = fetch(event.request);
  event.waitUntil(network.catch(() => {}));
  let timer;
  const timeout = new Promise((resolve) => { timer = setTimeout(() => resolve(null), NAV_TIMEOUT_MS); });
  try {
    const first = await Promise.race([network, timeout]);
    if (first) return first;
  } catch {
    /* offline: the precached pair shell below */
  } finally {
    clearTimeout(timer);
  }
  return (await fromCache("/pair")) || network;
}

async function asset(request) {
  // A stamped URL from another build (a page newer than this worker, mid-update): the network
  // answers, and the response never lands in this build's cache.
  const v = new URL(request.url).searchParams.get("v");
  if (v !== null && v !== BUILD) return fetch(request);
  // Module imports have no ?v=, stamped tags carry this build's; the cache is per build, so the
  // query can be ignored.
  const hit = await fromCache(request, { ignoreSearch: true });
  if (hit) return hit;
  const response = await fetch(request);
  if (cacheable(response)) {
    const cache = await caches.open(CACHE);
    await cache.put(new URL(request.url).pathname, response.clone());
  }
  return response;
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (bypassed(request)) return;
  if (request.mode === "navigate") {
    event.respondWith(navigate(event));
    return;
  }
  event.respondWith(asset(request));
});

self.addEventListener("message", (event) => {
  if (!event.data || event.data.type !== "cache-pages") return;
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    const list = await cache.match(LIST);
    if (list) await cachePages(cache, (await list.json()).pages, { onlyMissing: true });
  })());
});

self.addEventListener("sync", (event) => {
  if (event.tag !== SYNC_TAG) return;
  event.waitUntil((async () => {
    const pages = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of pages) c.postMessage({ type: "outbox-flush" });
  })());
});

// ---- push notifications v2 (TIX on orch-core, spec §7). The payload holds only cleartext: the
// space id, the TIX id, the kind and counts. Names and text come from this browser's own data.
const NEEDS_LABEL = { question: "Agent needs input", approval: "Plan ready for review",
  verdict: "Ready for your verdict", message: "Message from agent" };
const TICKET_ID = /^TIX-([1-9][0-9]{0,11})$/;
const SPACE_ID = /^[0-9a-f]{32}$/;
const MAX_SPACE_TAGS = 3;              // more workspaces than this share one "tix:all" notification
const ICON = "/static/img/icon-192.png";

// The IndexedDB "fileshare" database the pages use (db.js). Same version and stores; the worker
// creates nothing the pages wouldn't.
const DB_NAME = "fileshare";
const DB_VERSION = 6;
const DB_STORES = [["keys", null], ["outbox", { keyPath: "seq", autoIncrement: true }], ["labels", null], ["prefs", null],
  ["pairs", { keyPath: "space" }], ["seen", null], ["lists", null]];

function idbGet(store, key) {
  return new Promise((resolve) => {
    let req;
    try {
      req = self.indexedDB.open(DB_NAME, DB_VERSION);
    } catch {
      resolve(undefined);
      return;
    }
    req.onupgradeneeded = () => {
      for (const [name, opts] of DB_STORES) {
        if (!req.result.objectStoreNames.contains(name)) req.result.createObjectStore(name, opts || undefined);
      }
    };
    req.onerror = () => resolve(undefined);
    req.onsuccess = () => {
      const db = req.result;
      try {
        const get = db.transaction(store, "readonly").objectStore(store).get(key);
        get.onsuccess = () => { db.close(); resolve(get.result); };
        get.onerror = () => { db.close(); resolve(undefined); };
      } catch {
        db.close();
        resolve(undefined);
      }
    };
  });
}

// {label, titles}: titles is whether the space's redaction allows a title on the lock screen.
async function spaceInfo(s) {
  const v = SPACE_ID.test(s) ? await idbGet("labels", s) : undefined;
  if (typeof v === "string") return { label: v.slice(0, 80), titles: true };
  if (v && typeof v === "object" && typeof v.label === "string") return { label: v.label.slice(0, 80), titles: v.titles !== false };
  return { label: "a workspace", titles: false };
}

const unb64u = (str) => {
  const b64 = String(str).replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4));
  return Uint8Array.from(bin, (ch) => ch.charCodeAt(0));
};
const hexBytes = (h) => Uint8Array.from(h.match(/../g), (x) => parseInt(x, 16));
const withPrefix = (prefix, tail) => {
  const head = new TextEncoder().encode(prefix);
  const out = new Uint8Array(head.length + tail.length);
  out.set(head);
  out.set(tail, head.length);
  return out;
};
async function openEnvelope(key, env, aad) {
  if (env[0] !== 1 || env.length < 29) throw new Error("malformed envelope");
  return new Uint8Array(await self.crypto.subtle.decrypt({ name: "AES-GCM", iv: env.subarray(1, 13), additionalData: aad, tagLength: 128 },
    key, env.subarray(13)));
}

// Whether the server's row is the mirror the sealed doc names, in the space the push named:
// uuid == sha256("<space>|<doc.id>|<doc.gen>")[:16] (mirror-crypto.boundToRow, core, the CLI). Anything
// that cannot be checked counts as unbound.
async function boundTitleRow(m, doc, space) {
  if (!SPACE_ID.test(String(m?.space)) || m.space !== space) return false;
  if (typeof doc?.id !== "string" || !Number.isInteger(doc.gen) || doc.gen < 1) return false;
  const digest = new Uint8Array(await self.crypto.subtle.digest("SHA-256", new TextEncoder().encode(`${m.space}|${doc.id}|${doc.gen}`)));
  return Array.from(digest.subarray(0, 16), (b) => b.toString(16).padStart(2, "0")).join("") === m.uuid;
}

// The ticket title, decrypted here with the non-extractable MK; null on any failure, and null unless the
// routing is bound to the sealed doc (then the notification keeps the space-only text).
async function ticketTitle(t, space) {
  try {
    const keys = await idbGet("keys", "current");
    if (!keys || !keys.mk) return null;
    const r = await fetch(`/api/mirrors/${t}`, { credentials: "same-origin", cache: "no-store" });
    if (!r.ok) return null;
    const m = await r.json();
    const tu = hexBytes(m.uuid);
    const dekRaw = await openEnvelope(keys.mk, unb64u(m.wrapped_dek), withPrefix("sharing/tdek/v1|", tu));
    const dek = await self.crypto.subtle.importKey("raw", dekRaw, "AES-GCM", false, ["decrypt"]);
    const doc = JSON.parse(new TextDecoder().decode(await openEnvelope(dek, unb64u(m.enc_content), withPrefix("sharing/mirror/v1|", tu))));
    if (doc.redaction && doc.redaction !== "full" && doc.redaction !== "title") return null;
    if (!(await boundTitleRow(m, doc, space))) return null;
    // An older copy than the newest this phone opened (db.js "seen"): no title from it.
    const seen = await idbGet("seen", t);
    const g0 = seen?.gen, r0 = seen?.mirror_rev, g1 = doc.gen, r1 = doc.mirror_rev;
    if ([g0, r0, g1, r1].every(Number.isInteger) && (g1 < g0 || (g1 === g0 && r1 < r0))) return null;
    return typeof doc.title === "string" && doc.title.trim() ? doc.title.trim().slice(0, 120) : null;
  } catch {
    return null;
  }
}

async function joinBody(s, label) {
  try {
    const r = await fetch("/api/join-requests", { credentials: "same-origin", cache: "no-store" });
    if (r.ok) {
      const req = ((await r.json()).requests || []).find((x) => x.space === s);
      if (req && req.device_name) return `${String(req.device_name).slice(0, 60)} wants to sync ${label}`;
    }
  } catch {
    /* offline or signed out: the generic line */
  }
  return `A device wants to sync ${label}`;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const count = (x) => (Number.isInteger(x) && x > 0 ? x : 0);

async function openSpaceTags() {
  try {
    const shown = await self.registration.getNotifications();
    return shown.filter((x) => /^tix:[0-9a-f]{32}$/.test(x.tag || ""));
  } catch {
    return [];
  }
}

const GENERIC = { title: "TIX", options: { body: "Open TIX to see what needs you", tag: "tix", icon: ICON, data: { url: "/" } } };

// -> {title, options, close?: notifications to close first}
async function notificationFor(data) {
  const d = data && typeof data === "object" ? data : {};
  if (d.v !== 2 || typeof d.k !== "string" || typeof d.s !== "string") return GENERIC;
  const s = SPACE_ID.test(d.s) ? d.s : "";
  const m = typeof d.t === "string" ? TICKET_ID.exec(d.t) : null;
  const url = m ? `/t/${m[1]}#answer` : "/";
  const n = count(d.n);
  if (d.k === "clear") return clearFor(d, s, m);
  if (d.k === "join") {
    if (!s) return GENERIC;
    const { label } = await spaceInfo(s);
    return { title: "Device wants to sync", options: { body: await joinBody(s, label), tag: `tix:join:${s}`, renotify: true,
      icon: ICON, data: { url: "/settings#join", s } } };
  }
  let title, body, newIds, url2 = url;
  if (d.k === "batch") {
    // A burst the server folded into one push (feedback round B): "9 new things need you in Acme", with the TIX
    // ids so a later "clear" knows what still needs you. Same workspace grouping and cap as a single push.
    if (!s) return GENERIC;
    const { label } = await spaceInfo(s);
    newIds = (Array.isArray(d.ts) ? d.ts : []).filter((t) => typeof t === "string" && TICKET_ID.test(t)).slice(0, 20);
    const k = n || newIds.length || 1;
    title = "Needs you";
    body = `${k} new thing${k === 1 ? "" : "s"} need${k === 1 ? "s" : ""} you in ${label}`;
    url2 = "/";
  } else {
    title = NEEDS_LABEL[d.k];
    if (!title) return GENERIC;
    const { label, titles } = await spaceInfo(s);
    body = d.k === "message" ? (s ? `${label} · ${plural(n || 1, "message")}` : plural(n || 1, "message"))
      : d.k === "question" ? `${label} · ${plural(n || 1, "question")}` : `${label} · ${m ? d.t : "a ticket"}`;
    if (m && titles && (await idbGet("prefs", "show_titles")) === true) {
      const t = await ticketTitle(d.t, s);
      if (t) body = `${label} · ${t}`;
    }
    const more = count(d.c) - (n || 1);
    if (more > 0) body += `\n+${more} more`;
    newIds = m ? [d.t] : [];
  }
  const tags = await openSpaceTags();
  const mine = tags.find((x) => x.tag === `tix:${s}`);
  const others = tags.filter((x) => x !== mine);
  // The tickets that need you per workspace, so a later "clear" knows whether others remain.
  const byspace = {};
  for (const x of others) byspace[x.tag.slice(4)] = ticketsOf(x);
  const group = await groupNote();
  for (const [sp, ts] of Object.entries(group?.data?.ts || {})) byspace[sp] = [...new Set([...(byspace[sp] || []), ...ts])];
  for (const sp of group?.data?.spaces || []) byspace[sp] ||= [];
  if (s) byspace[s] = [...new Set([...(byspace[s] || []), ...ticketsOf(mine), ...newIds])];
  const spaces = Object.keys(byspace);
  if (spaces.length > MAX_SPACE_TAGS) {
    return { title, close: [...others, ...(mine ? [mine] : [])], options: { body: workspacesText(spaces.length), tag: "tix:all",
      renotify: true, icon: ICON, data: { url: "/", spaces, ts: byspace, title, body: workspacesText(spaces.length) } } };
  }
  return { title, options: { body, tag: s ? `tix:${s}` : "tix", renotify: true, icon: ICON,
    data: { url: url2, s, ts: s ? byspace[s] : [], title, body } } };
}

const ticketsOf = (n) => (Array.isArray(n?.data?.ts) ? n.data.ts.filter((t) => typeof t === "string") : []);
const workspacesText = (k) => `${k} workspace${k === 1 ? " needs" : "s need"} you`;

async function groupNote() {
  try {
    return (await self.registration.getNotifications({ tag: "tix:all" }))[0] || null;
  } catch {
    return null;
  }
}

// "clear" for ticket d.t in space s: the workspace's notification (or the "tix:all" group) is replaced
// with "Handled on desktop" only when nothing else there still needs you; otherwise it is shown again
// silently without that ticket. A join request's clear (empty t) replaces its own notification.
async function clearFor(d, s, m) {
  const { label } = await spaceInfo(s);
  const handled = (tag, body) => ({ title: "Handled on desktop", options: { body, tag, silent: true, renotify: false,
    icon: ICON, data: { url: "/", s } } });
  if (!m) return handled(s ? `tix:join:${s}` : "tix", `Handled on desktop · ${label}`);
  const quiet = (n, data) => ({ title: n.data?.title || n.title || "TIX", options: { body: n.data?.body || n.body || "",
    tag: n.tag, silent: true, renotify: false, icon: ICON, data } });
  const mine = (await openSpaceTags()).find((x) => x.tag === `tix:${s}`);
  if (mine) {
    const rest = ticketsOf(mine).filter((t) => t !== d.t);
    return rest.length && count(d.c) > 0 ? quiet(mine, { ...mine.data, ts: rest }) : handled(`tix:${s}`, `Handled on desktop · ${label}`);
  }
  const group = await groupNote();
  if (group && (group.data?.spaces || []).includes(s)) {
    const ts = { ...(group.data.ts || {}) };
    ts[s] = (ts[s] || []).filter((t) => t !== d.t);
    if (!ts[s].length) delete ts[s];
    const spaces = (group.data.spaces || []).filter((x) => x !== s || ts[x]);
    if (!spaces.length || count(d.c) === 0) return handled("tix:all", "Handled on desktop");
    const body = workspacesText(spaces.length);
    return { title: group.data?.title || group.title || "TIX", options: { body, tag: "tix:all", silent: true, renotify: false,
      icon: ICON, data: { ...group.data, spaces, ts, body } } };
  }
  return handled(s ? `tix:${s}` : "tix", `Handled on desktop · ${label}`);
}

self.addEventListener("push", (event) => {
  let data = null;
  try {
    data = event.data ? event.data.json() : null;
  } catch {
    data = null; /* not JSON: a generic notification (a push must always show one) */
  }
  event.waitUntil((async () => {
    let note;
    try {
      note = await notificationFor(data);
    } catch {
      note = GENERIC;
    }
    for (const x of note.close || []) x.close();
    await self.registration.showNotification(note.title, note.options);
  })());
});

// Only a path on this origin is ever opened: a ticket on its decision card (/t/<n>#answer), Needs
// you, or the join request in Settings.
function clickTarget(data) {
  const raw = data && typeof data.url === "string" ? data.url : "";
  const m = /^\/t\/([1-9][0-9]{0,11})(#answer)?$/.exec(raw);
  const path = m ? `/t/${m[1]}#answer` : raw === "/settings#join" ? raw : "/";
  return new URL(path, self.location.origin).href;
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = clickTarget(event.notification.data);
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const win = wins.find((c) => { try { return new URL(c.url).origin === self.location.origin; } catch { return false; } });
    if (win) {
      try {
        await win.focus();
        await win.navigate(url);
        return;
      } catch {
        /* an uncontrolled window can't be navigated from here: open a new one */
      }
    }
    await self.clients.openWindow(url);
  })());
});
