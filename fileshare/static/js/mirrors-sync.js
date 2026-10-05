// The mirror list without downloading it again on every open. /api/mirrors is ~0.5 MB of sealed rows and
// answers with a `cursor` (the newest event seq at the time of the read). The body is kept in IndexedDB
// ("lists", ciphertext only, exactly as the server sent it); the next open asks /api/mirrors/changes?after=<cursor>
// for the few rows that moved and merges them. A full list is fetched again only when there is nothing
// stored, the stored copy is more than a day old (a reset server or a missed deletion cannot linger), or the
// delta did not work. Offline and sign-out errors are not hidden by a full fetch.
import { api } from "./api.js";
import { LISTS, getValue, putValue } from "./db.js";

export const FULL_REFRESH_MS = 24 * 60 * 60 * 1000;
const MAX_DELTA_PAGES = 20;          // the server returns <= 200 rows a page

const isCursor = (n) => Number.isInteger(n) && n >= 0;

// Merges changed rows (tombstones carry deleted: true) into a list, newest update first like the server.
export function mergeMirrors(rows, changed) {
  const byUuid = new Map(rows.map((m) => [m.uuid, m]));
  for (const { deleted, ...row } of changed) {
    if (deleted) byUuid.delete(row.uuid);
    else byUuid.set(row.uuid, row);
  }
  return [...byUuid.values()].sort((a, b) => (a.updated_at < b.updated_at ? 1 : a.updated_at > b.updated_at ? -1 : 0));
}

async function readStored() {
  try {
    return await getValue(LISTS, "mirrors");
  } catch {
    return null;                                // no IndexedDB (a private window): always the full list
  }
}

async function store(body, fullAt) {
  try {
    await putValue(LISTS, "mirrors", { body, at: Date.now(), fullAt });
  } catch {
    /* no IndexedDB: no instant open, no delta next time */
  }
}

async function delta(body) {
  let { mirrors, cursor } = body;
  for (let page = 0; page < MAX_DELTA_PAGES; page++) {
    const r = await api("GET", `/api/mirrors/changes?after=${cursor}&wait=0`);
    if (!r.mirrors?.length || !(r.cursor > cursor)) return { mirrors, cursor };
    mirrors = mergeMirrors(mirrors, r.mirrors);
    cursor = r.cursor;
  }
  throw new Error("too many changes: take the full list");
}

let inflight = null;

// {mirrors, cursor}: the sealed rows as the server would list them now. Concurrent callers (the list, the
// tab-bar badge, the watcher) share one answer.
export function syncedMirrors() {
  if (!inflight) {
    inflight = sync().finally(() => { inflight = null; });
  }
  return inflight.then((body) => structuredClone(body));
}

async function sync() {
  const stored = await readStored();
  const body = stored?.body;
  const fullAt = Number.isFinite(stored?.fullAt) ? stored.fullAt : stored?.at;
  if (body && Array.isArray(body.mirrors) && isCursor(body.cursor) && Number.isFinite(fullAt)
      && Date.now() - fullAt < FULL_REFRESH_MS && fullAt <= Date.now()) {
    try {
      const next = await delta(body);
      await store(next, fullAt);
      return next;
    } catch (e) {
      if (e?.status === 0 || e?.status === 401) throw e;      // offline / signed out: say so, do not retry the world
    }
  }
  const full = await api("GET", "/api/mirrors");
  if (full && Array.isArray(full.mirrors)) await store(full, Date.now());
  return full;
}

// The cursor of the stored list: where the change watcher continues, instead of reading every row from 0.
export async function storedCursor() {
  const c = (await readStored())?.body?.cursor;
  return isCursor(c) ? c : 0;
}
