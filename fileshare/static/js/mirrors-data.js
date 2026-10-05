// fileshare/static/js/mirrors-data.js — what Needs you, Tickets and a ticket page load (TIX on
// orch-core): the spaces and their sealed labels, the mirrors opened with MK, the decisions sent,
// and a long-poll on /api/mirrors/changes (it also wakes when the desktop acks a decision).
// The labels are cached in IndexedDB ("labels") so the service worker can name a workspace on the
// lock screen; nothing else decrypted is stored. The last GET /api/spaces and /api/mirrors bodies are
// kept as the server sent them ("lists": ciphertext only, never a doc, DEK or MK), so Needs you opens at
// once and offline; a cached row is opened on every load through the same checks as a fresh one.
import { api, ApiError } from "./api.js";
import { LABELS, LISTS, SEEN, clearLists, getValue, putValue } from "./db.js";
import { highWater, isRollback, openQuestionCount, phoneNeed, seenKey } from "./mirror-model.js";
import { mapLimit } from "./format.js";
import { clearKeys, loadKeys } from "./keystore.js";
import { boundToRow, openDecision, openMirror, openSpaceLabel } from "./mirror-crypto.js";
import { hexToBytes } from "./crypto.js";
import { loginHref } from "./nav.js";

const POLL_WAIT_S = 25;
const POLL_BACKOFF_MS = 5000;

// The MK of this browser, or a redirect to sign in.
export async function keysOrLogin() {
  const keys = await loadKeys().catch(() => null);
  if (!keys) {
    await clearLists().catch(() => {});        // whatever another key left behind
    location.replace(loginHref(location));
    return null;
  }
  return keys;
}

// A 401 although this browser still holds keys: the session is gone. The keys and the last-known lists
// are dropped (banner.js does too; this waits for it), then the page goes to sign in.
export async function signInAgain() {
  await Promise.all([clearKeys().catch(() => {}), clearLists().catch(() => {})]);
  location.replace(loginHref(location));
}

// Map space id -> {id, label, owner_name, last_seen_at, needs, key_version}. A label that does
// not open stays null (the page says "A workspace").
export async function loadSpaces(mk) {
  const body = await api("GET", "/api/spaces");
  await storeList("spaces", body);
  return openSpaces(mk, body?.spaces || []);
}

async function openSpaces(mk, spaces = []) {
  const out = new Map();
  await mapLimit(spaces, 4, async (s) => {
    let label = null;
    try {
      label = await openSpaceLabel(mk, s);
    } catch {
      label = null;
    }
    out.set(s.id, { ...s, label });
  });
  return out;
}

// Every live mirror, opened. A row that does not open keeps doc null and error "integrity".
export async function loadMirrors(mk, space = null) {
  const q = space ? `?space=${encodeURIComponent(space)}` : "";
  const body = await api("GET", `/api/mirrors${q}`);
  if (!space) await storeList("mirrors", body);
  return mapLimit(body?.mirrors || [], 4, (m) => openRow(mk, m));
}

// ---- the last-known lists (db.js "lists")

// Keeps a server body exactly as it came (sealed labels and docs) with the time it arrived. Best effort.
async function storeList(key, body) {
  if (!body || typeof body !== "object") return;
  try {
    await putValue(LISTS, key, { body, at: Date.now() });
  } catch {
    /* no IndexedDB (a private window): no instant open next time */
  }
}

const listOf = (entry, field) => (entry && Number.isFinite(entry.at) && Array.isArray(entry.body?.[field]) ? entry.body[field] : null);

// The last-known spaces and mirrors, opened with MK now: {spaces, rows, skipped, at} (at: when the older of
// the two bodies arrived), or null without both. Rows go through openRow like a fresh answer, as a cached
// copy: one older than the "seen" mark is left out (skipped counts them, never shown as a rollback) and
// the mark is never written.
export async function loadCachedLists(mk) {
  let spaces, mirrors;
  try {
    [spaces, mirrors] = await Promise.all([getValue(LISTS, "spaces"), getValue(LISTS, "mirrors")]);
  } catch {
    return null;
  }
  const s = listOf(spaces, "spaces"), m = listOf(mirrors, "mirrors");
  if (!s || !m) return null;
  const opened = await mapLimit(m, 4, (row) => openRow(mk, row, { cached: true }));
  const rows = opened.filter(Boolean);
  return { spaces: await openSpaces(mk, s), rows, skipped: opened.length - rows.length, at: Math.min(spaces.at, mirrors.at) };
}

// A row whose doc is older than the newest this browser opened for that ticket (the "seen" high-water
// mark) keeps doc null and rollback true; otherwise the mark moves forward. Best effort without IndexedDB.
// cached: the row comes from the last-known list (loadCachedLists), not the server just now. Such a row
// older than the mark is no rollback (the server has since sent the newer one): openRow returns null and
// the page leaves it out until the network answers. A cached row never moves the mark.
export async function openRow(mk, m, { cached = false } = {}) {
  let opened;
  try {
    opened = await openMirror(mk, m);
  } catch {
    return { ...m, doc: null, dek: null, error: "integrity" };
  }
  const { doc, dek } = opened;
  // The cleartext routing must be the mirror the sealed doc names (final review I3); otherwise nothing from
  // this row is shown or sent. The needs kind, question count and status then come from the sealed doc.
  if (!(await boundToRow(m, doc))) return { ...m, doc: null, dek: null, error: "binding" };
  const sealed = { needs: phoneNeed(doc), open_questions: openQuestionCount(doc),
    status: typeof doc.status === "string" ? doc.status : m.status };
  // Keyed by what boundToRow just proved, not by m.id: the server reuses TIX numbers after a reset.
  const mark = seenKey(m.space, doc);
  let seen = null;
  try {
    seen = await getValue(SEEN, mark);
  } catch {
    seen = null;
  }
  if (isRollback(seen, doc)) return cached ? null : { ...m, ...sealed, doc: null, dek, rollback: true };
  const next = highWater(seen, doc);
  if (!cached && next && (!seen || next.gen !== seen.gen || next.mirror_rev !== seen.mirror_rev)) {
    try {
      await putValue(SEEN, mark, next);
    } catch {
      /* no IndexedDB: the page still compares within its own lifetime */
    }
  }
  return { ...m, ...sealed, doc, dek };
}

// Whether a doc's redaction lets a title reach the lock screen (full or title, never key-only).
export function allowsTitles(doc) {
  if (!doc) return false;
  if (doc.redaction) return doc.redaction === "full" || doc.redaction === "title";
  return typeof doc.title === "string" && doc.title.trim() !== "";
}

// Writes the labels cache the service worker reads. Best effort.
export async function cacheLabels(spaces, mirrors = []) {
  for (const s of spaces.values()) {
    if (!s.label) continue;
    const docs = mirrors.filter((m) => m.space === s.id && m.doc);
    const titles = docs.length ? docs.every((m) => allowsTitles(m.doc)) : true;
    try {
      await putValue(LABELS, s.id, { label: s.label, titles });
    } catch {
      /* no IndexedDB (a private window): the lock screen says "a workspace" */
    }
  }
}

// The decisions sent for one ticket, opened with its DEK (newest last). A body that does not open
// keeps body null.
export async function loadDecisions(row) {
  const { decisions = [] } = await api("GET", `/api/decisions?space=${row.space}&ticket=${row.id}`);
  const scope = hexToBytes(row.uuid);
  return mapLimit(decisions, 4, async (d) => {
    let body = null;
    try {
      body = await openDecision(row.dek, scope, hexToBytes(d.uuid), d.enc_body);
    } catch {
      body = null;
    }
    return { ...d, body };
  });
}

// Long-polls /api/mirrors/changes and calls onChange(rows) for every batch. First it reads up to
// the current cursor without waiting, so only later changes count. Returns stop().
export function watchMirrors(onChange) {
  let stopped = false;
  let cursor = 0;
  (async () => {
    try {
      for (;;) {
        const r = await api("GET", `/api/mirrors/changes?after=${cursor}&wait=0`);
        if (!r.mirrors?.length || r.cursor <= cursor) break;
        cursor = r.cursor;
      }
    } catch {
      /* the loop below retries */
    }
    while (!stopped) {
      try {
        const r = await api("GET", `/api/mirrors/changes?after=${cursor}&wait=${POLL_WAIT_S}`);
        if (stopped) return;
        if (r.mirrors?.length) {
          cursor = r.cursor;
          await onChange(r.mirrors);
        }
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return;
        await new Promise((res) => setTimeout(res, POLL_BACKOFF_MS));
      }
    }
  })();
  return () => { stopped = true; };
}
