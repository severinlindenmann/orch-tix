// The one IndexedDB database, "fileshare", shared by keystore.js ("keys") and outbox.js
// ("outbox"). Every module opens it through openDb() so the version and the upgrade live in one
// place and two modules never race on version numbers.
//   v1: keys   (the non-extractable KEK and MK, spec §9)
//   v2: outbox (queued encrypted upload requests, spec §16), keyPath "seq", auto-increment = FIFO
//   v3: labels (space id -> {label, titles}, decrypted here, read by the service worker for push v2)
//       and prefs (this browser's switches, e.g. "show_titles"). sw.js opens the same version with
//       the same stores (tests/js/outbox.test.mjs keeps the two in step).
//   v4: pairs  (space id -> {space, phoneId, key, label, paired_at}: this phone's pairing with a
//       desktop, the key a non-extractable HMAC CryptoKey; pairing.js), keyPath "space"
//   v5: seen   (space|doc id -> {gen, mirror_rev}: the newest snapshot this browser opened, so an older one
//       the server hands back later is noticed by the ticket page, Needs you and the service worker)
//   v6: lists  ("spaces" | "mirrors" -> {body, at}: the last GET /api/spaces and /api/mirrors bodies exactly
//       as the server sent them, sealed labels and docs, never opened; mirrors-data.js), so Needs you
//       opens at once and offline. Cleared on sign-out.
export const DB_NAME = "fileshare";
export const DB_VERSION = 6;
export const KEYS = "keys";
export const OUTBOX = "outbox";
export const LABELS = "labels";
export const PREFS = "prefs";
export const PAIRS = "pairs";
export const SEEN = "seen";
export const LISTS = "lists";

export function upgrade(db) {
  if (!db.objectStoreNames.contains(KEYS)) db.createObjectStore(KEYS);
  if (!db.objectStoreNames.contains(OUTBOX)) db.createObjectStore(OUTBOX, { keyPath: "seq", autoIncrement: true });
  if (!db.objectStoreNames.contains(LABELS)) db.createObjectStore(LABELS);
  if (!db.objectStoreNames.contains(PREFS)) db.createObjectStore(PREFS);
  if (!db.objectStoreNames.contains(PAIRS)) db.createObjectStore(PAIRS, { keyPath: "space" });
  if (!db.objectStoreNames.contains(SEEN)) db.createObjectStore(SEEN);
  if (!db.objectStoreNames.contains(LISTS)) db.createObjectStore(LISTS);
}

export const getValue = (store, key) => withStore(store, "readonly", (s) => s.get(key));
export const putValue = (store, key, value) => withStore(store, "readwrite", (s) => s.put(value, key));
// Sign-out and an expired session drop the last-known lists (mirrors-data.js) with the keys.
export const clearLists = () => withStore(LISTS, "readwrite", (s) => s.clear());

export function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => upgrade(req.result);
    req.onsuccess = () => {
      const db = req.result;
      // A newer tab wants to upgrade: let it, instead of blocking it.
      db.onversionchange = () => db.close();
      resolve(db);
    };
    req.onerror = () => reject(req.error);
    // onblocked: an older tab still holds v1 open. Its connections close after every operation
    // (and on versionchange), so the open simply completes a moment later.
  });
}

// Runs fn(objectStore) in one transaction and resolves with the last request's result.
export async function withStore(name, mode, fn) {
  const db = await openDb();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(name, mode);
      const req = fn(tx.objectStore(name));
      tx.oncomplete = () => resolve(req ? req.result : undefined);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}
