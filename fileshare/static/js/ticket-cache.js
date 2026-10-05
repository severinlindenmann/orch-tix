// The sealed local cache of this phone. tickets (db.js "tickets"): each opened ticket's mirror row exactly as the server
// sent it (sealed, never opened); read back through openRow(..., {cached: true}), so an older copy than the "seen" mark is
// refused. Bounded (LRU), cleared on sign-out with the lists. keymap/needsmemo (db.js "lists"): sealed under MK. Nothing here
// is plaintext beyond the server's own cleartext routing; nothing leaves the device.
import { LISTS, TICKETS, allValues, deleteValue, getValue, putValue } from "./db.js";
import { b64u, canonicalJson, open, seal, unb64u } from "./crypto.js";

export const MAX_TICKETS = 40;
export const MAX_BYTES = 6 * 1024 * 1024;       // of sealed row text, all cached tickets
const MAX_ROW_BYTES = 2 * 1024 * 1024;          // one row bigger than this is not kept

const te = new TextEncoder();
const td = new TextDecoder();
const AAD_KEYMAP = te.encode("sharing/keymap/v1");

const sizeOf = (row) => JSON.stringify(row).length;
const shaped = (row, n) => row && typeof row === "object" && row.n === n && typeof row.uuid === "string"
  && typeof row.space === "string" && typeof row.enc_content === "string" && typeof row.wrapped_dek === "string";

// "Offline · last updated 14:05" (this browser's time; with the date when it was not today).
export function offlineText(at, now = Date.now()) {
  const d = new Date(at), t = new Date(now);
  const two = (x) => String(x).padStart(2, "0");
  const clock = `${two(d.getHours())}:${two(d.getMinutes())}`;
  const today = d.getFullYear() === t.getFullYear() && d.getMonth() === t.getMonth() && d.getDate() === t.getDate();
  return `Offline · last updated ${today ? clock : `${two(d.getDate())}.${two(d.getMonth() + 1)} ${clock}`}`;
}

// Which entries to drop so at most maxTickets and maxBytes remain: oldest `used` first, never `keep`.
export function evictions(entries, { maxTickets = MAX_TICKETS, maxBytes = MAX_BYTES, keep = null } = {}) {
  const order = [...entries].sort((a, b) => (a.used || 0) - (b.used || 0));
  let count = order.length;
  let bytes = order.reduce((t, e) => t + (e.size || 0), 0);
  const out = [];
  for (const e of order) {
    if (count <= maxTickets && bytes <= maxBytes) break;
    if (e.n === keep) continue;
    out.push(e.n);
    count -= 1;
    bytes -= e.size || 0;
  }
  return out;
}

// Keeps an opened ticket's row (only one that passed openRow). Best effort.
export async function rememberTicket(n, row) {
  try {
    if (!shaped(row, n)) return;
    const size = sizeOf(row);
    if (size > MAX_ROW_BYTES) {
      await deleteValue(TICKETS, String(n));
      return;
    }
    const now = Date.now();
    await putValue(TICKETS, String(n), { n, row, at: now, used: now, size });
    const entries = (await allValues(TICKETS)).filter((e) => Number.isInteger(e?.n));
    for (const gone of evictions(entries, { keep: n })) await deleteValue(TICKETS, String(gone));
  } catch {
    /* no IndexedDB (a private window): no offline ticket */
  }
}

// {row, at} of ticket n, or null; marks it used.
export async function cachedTicket(n) {
  try {
    const e = await getValue(TICKETS, String(n));
    if (!e || e.n !== n || !Number.isFinite(e.at) || !shaped(e.row, n)) return null;
    putValue(TICKETS, String(n), { ...e, used: Date.now() }).catch(() => {});
    return { row: e.row, at: e.at };
  } catch {
    return null;
  }
}

// Forgets one ticket (the server says it is gone).
export async function forgetTicket(n) {
  try {
    await deleteValue(TICKETS, String(n));
  } catch {
    /* nothing to forget */
  }
}

// ---- the key map

// Map "DEMO-0042" -> n from opened rows ({doc, n}); the keys are upper-cased like the page looks them up.
export function keyMapOf(rows) {
  const map = new Map();
  for (const r of rows) {
    if (typeof r?.doc?.id === "string" && Number.isInteger(r.n)) map.set(r.doc.id.toUpperCase(), r.n);
  }
  return map;
}

export async function saveKeyMap(mk, map) {
  try {
    const pt = te.encode(canonicalJson(Object.fromEntries(map)));
    await putValue(LISTS, "keymap", { enc: b64u(await seal(mk, pt, AAD_KEYMAP)), at: Date.now() });
  } catch {
    /* best effort: no map, no key links until the list is read again */
  }
}

// The Map saved last, or null when there is none or it does not open with this MK.
export async function loadKeyMap(mk) {
  try {
    const e = await getValue(LISTS, "keymap");
    if (!e || typeof e.enc !== "string") return null;
    const obj = JSON.parse(td.decode(await open(mk, unb64u(e.enc), AAD_KEYMAP)));
    const map = new Map();
    for (const [k, n] of Object.entries(obj || {})) if (Number.isInteger(n) && n > 0) map.set(k, n);
    return map;
  } catch {
    return null;
  }
}

// ---- the badge memo: what a row's sealed doc said about "needs you", per row version

const AAD_NEEDS = te.encode("sharing/needs-memo/v1");

async function digest(text) {
  const d = new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", te.encode(text)));
  return Array.from(d.subarray(0, 12), (b) => b.toString(16).padStart(2, "0")).join("");
}

// uuid -> {h, need}: h digests the sealed content, so a changed row is opened again; sealed under MK.
export async function loadNeedsMemo(mk) {
  try {
    const e = await getValue(LISTS, "needsmemo");
    if (!e || typeof e.enc !== "string") return new Map();
    const obj = JSON.parse(td.decode(await open(mk, unb64u(e.enc), AAD_NEEDS)));
    return new Map(Object.entries(obj || {}));
  } catch {
    return new Map();
  }
}

export async function saveNeedsMemo(mk, memo) {
  try {
    const pt = te.encode(canonicalJson(Object.fromEntries(memo)));
    await putValue(LISTS, "needsmemo", { enc: b64u(await seal(mk, pt, AAD_NEEDS)), at: Date.now() });
  } catch {
    /* best effort */
  }
}

export const contentDigest = (row) => digest(`${row.uuid}|${row.wrapped_dek}|${row.enc_content}`);

// Rows for the badge ({doc, needs}): a memo hit stands in for the opened row, anything else is opened.
export async function rowsWithMemo(mk, mirrors, openRow) {
  const memo = await loadNeedsMemo(mk);
  const next = new Map();
  let changed = mirrors.length !== memo.size;
  const rows = await Promise.all(mirrors.map(async (m) => {
    const h = await contentDigest(m);
    const hit = memo.get(m.uuid);
    if (hit && hit.h === h) { next.set(m.uuid, hit); return { uuid: m.uuid, doc: {}, needs: hit.need }; }
    const r = await openRow(mk, m);
    changed = true;
    if (r?.doc) next.set(m.uuid, { h, need: r.needs ?? null });
    return r;
  }));
  if (changed) await saveNeedsMemo(mk, next);
  return rows;
}
