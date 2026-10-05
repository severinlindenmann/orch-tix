// Pure login guards and the Needs you nav badge, importable without a DOM (node tests import this module directly).
import { unb64u } from "./crypto.js";
import { api } from "./api.js";
import { tellWorker } from "./swreg.js";

export const MIN_ITERATIONS = 1000;
export const MAX_ITERATIONS = 10_000_000;
export const MIN_SALT_LEN = 16;

// Resolve like the browser will, so tab/newline stripping and backslashes can't smuggle in another host.
export function safeNext(raw, origin = globalThis.location?.origin) {
  if (typeof raw !== "string" || !origin) return "/";
  try {
    const u = new URL(raw, origin);
    // A "//host" pathname (e.g. from "/a/..//evil.com") would become protocol-relative when navigated to.
    if (u.origin !== origin || u.pathname.startsWith("//")) return "/";
    // Never a fragment: it can hold a key (a /pair link), and a fragment never needs to survive a login.
    return u.pathname === "/pair" ? "/" : u.pathname + u.search;
  } catch {
    return "/";
  }
}

// The login link for this page: path and query only, never the fragment (review fix round 1).
export function loginHref(loc = globalThis.location) {
  const path = loc.pathname === "/pair" ? "/" : loc.pathname + (loc.search || "");
  // Same-origin relative paths only (one leading "/"), else "/"; the login page's safeNext() checks again.
  return `/login?next=${encodeURIComponent(path.startsWith("/") && !path.startsWith("//") ? path : "/")}`;
}

export class UnsafeKdfError extends Error {
  constructor() {
    super("server returned unsafe key parameters");
    this.name = "UnsafeKdfError";
  }
}

// Returns the decoded salt, or throws UnsafeKdfError before any key is derived.
export function checkKdf(kdf) {
  const it = kdf?.kdf_iterations;
  if (!Number.isInteger(it) || it < MIN_ITERATIONS || it > MAX_ITERATIONS) throw new UnsafeKdfError();
  let salt;
  try {
    salt = unb64u(kdf.kdf_salt);
  } catch {
    throw new UnsafeKdfError();
  }
  if (!(salt instanceof Uint8Array) || salt.length < MIN_SALT_LEN) throw new UnsafeKdfError();
  return salt;
}

// ---- the Needs you badge (TIX on orch-core): what Needs you shows. Mirrors whose SEALED doc needs the human
// (openRow binds the routing and takes `needs` from the doc; the cleartext never counts), messages to the
// human, and join requests waiting in this browser.

export function attentionBadge(count) {
  const n = Number.isInteger(count) && count > 0 ? count : 0;
  if (!n) return { text: "", title: "", hidden: true };
  return { text: n > 99 ? "99+" : String(n), title: n === 1 ? "1 thing needs you" : `${n} things need you`, hidden: false };
}

// Opens the mirrors with this browser's MK (mirrors-data.openRow). Loaded on demand: mirrors-data imports
// this module too.
async function openRowsHere(mirrors) {
  const [{ loadKeys }, { openRow }] = await Promise.all([import("./keystore.js"), import("./mirrors-data.js")]);
  const keys = await loadKeys();
  if (!keys) return [];
  return Promise.all(mirrors.map((m) => openRow(keys.mk, m)));
}

const optional = async (p, pick, fallback = 0) => {
  try {
    return pick(await p);
  } catch {
    return fallback;    // a device token, offline: only what did load
  }
};

// Tells the service worker what needs you right now, so it closes the notifications of things that are handled
// (sw.js reconcileNeeds). iOS never replaces a notification by tag, so the server sends it no "clear" push at all
// (push.py); this is how its lock screen catches up when the app is opened. `messages`: unread messages to you.
// `messages` is null when the unread count could not be read (offline, a device token): the worker then leaves message
// notifications alone. `at` is when the lists were requested: the worker never closes a notification shown after it
// (a push that arrived while the answer was on its way is newer than the list).
export function tellWorkerNeeds(mirrors, messages, nav = globalThis.navigator, at = undefined, joins = null) {
  try {
    const live = (mirrors || []).filter((m) => m && m.needs);
    tellWorker({ type: "needs-spaces", spaces: [...new Set(live.map((m) => m.space))],
      tickets: live.map((m) => `${m.space}|${m.id}`), messages, joins, ...(at === undefined ? {} : { at }) }, nav);
  } catch {
    /* no worker */
  }
}

// The app icon badge follows what needs you (iOS 16.4+ installed web apps): the same count as the tab badge.
export function setAppBadge(n, nav = globalThis.navigator) {
  try {
    if (n > 0) nav?.setAppBadge?.(n)?.catch?.(() => {});
    else nav?.clearAppBadge?.()?.catch?.(() => {});
  } catch {
    /* no Badging API */
  }
}

export async function needsCount(get = (path) => api("GET", path), openRows = openRowsHere) {
  const at = Date.now();
  const { mirrors = [] } = await get("/api/mirrors");
  let joinSpaces = null;
  const [rows, messages, joins] = await Promise.all([
    openRows(mirrors),
    optional(get("/api/messages?after=0&wait=0"), (r) => (r.messages || []).filter((m) => m.to_kind === "human").length, null),
    optional(get("/api/join-requests"), (r) => { joinSpaces = (r.requests || []).map((x) => x.space); return joinSpaces.length; }),
  ]);
  const total = rows.filter((r) => r && r.doc && r.needs).length + (messages ?? 0) + joins;
  tellWorkerNeeds(mirrors, messages, undefined, at, joinSpaces);
  return total;
}

// Fills #needs-badge (the sidebar and the tab bar share it). Failures keep the badge as it was:
// the session and network banners report those.
export async function refreshAttention(doc = document, get = (path) => api("GET", path), openRows = openRowsHere) {
  const badge = doc.getElementById("needs-badge");
  if (!badge) return;
  let n;
  try {
    n = await needsCount(get, openRows);
  } catch {
    return;
  }
  setAppBadge(n);
  const b = attentionBadge(n);
  badge.textContent = b.text;
  badge.title = b.title;
  badge.hidden = b.hidden;
}

// The Tickets tab is the Needs you page with ?view=tickets: mark the right tab as current.
export function markCurrentTab(doc = document, loc = globalThis.location) {
  if (!loc || loc.pathname !== "/") return;
  const tickets = new URLSearchParams(loc.search).get("view") === "tickets";
  for (const a of doc.querySelectorAll(".nav-item[data-tab]")) {
    const cur = a.dataset.tab === (tickets ? "tickets" : "needs");
    if (cur) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  if (tickets) {
    const t = doc.getElementById("needs-title");
    if (t) t.textContent = "Tickets";
    doc.title = "Tickets · tix";
  }
}
