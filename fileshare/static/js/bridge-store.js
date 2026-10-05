// fileshare/static/js/bridge-store.js: what the bridge keeps in this browser (docs/bridge-protocol.md §2.4, §2.6, §5.2).
// Its own IndexedDB database, so db.js and the service worker's stores are untouched:
//   device: "signing" -> {privateKey (non-extractable ECDSA), publicKey, pub (65 bytes)}, one per browser profile
//   hosts:  workspace hex -> {workspace, keyVersion, kWs (non-extractable HKDF), hostPub (65 bytes) | null, next (seq)}
// The device key and the counter live in the same database, so a browser that lost one lost both and pairs again.
// No fallback: without IndexedDB there is no device key and no sequence counter, so every call fails with
// BridgeStorageError rather than keeping keys in memory or in storage other scripts can read.
import { AAD_MK, open as openSealed, unb64u } from "./crypto.js";
import { api } from "./api.js";
import { loadKeys } from "./keystore.js";
import { MAX_SEQ, hostKeyFromPin, importPublicKey, newDeviceKey, workspaceKey } from "./bridge-crypto.js";

const DB = "fileshare-bridge", DEVICE = "device", HOSTS = "hosts", SLOT = "signing";
const HEX32 = /^[0-9a-f]{32}$/;

export class BridgeStorageError extends Error {
  constructor(message, cause) { super(message, { cause }); this.name = "BridgeStorageError"; }
}

function openDb() {
  const factory = globalThis.indexedDB;
  if (!factory) return Promise.reject(new BridgeStorageError("IndexedDB is unavailable: this browser cannot keep a bridge device key"));
  return new Promise((resolve, reject) => {
    let req;
    try { req = factory.open(DB, 1); } catch (e) { reject(new BridgeStorageError("IndexedDB refused to open", e)); return; }
    req.onupgradeneeded = () => {
      req.result.createObjectStore(DEVICE);
      req.result.createObjectStore(HOSTS, { keyPath: "workspace" });
    };
    req.onsuccess = () => { req.result.onversionchange = () => req.result.close(); resolve(req.result); };
    req.onerror = () => reject(new BridgeStorageError("IndexedDB refused to open", req.error));
  });
}

// One transaction. fn(store, done, fail) issues requests; the promise settles only after the transaction committed
// (or aborted), so a value is never used unless it is on disk. readwrite transactions on one store run one after the
// other, in every tab of this origin: that is what makes the counter atomic (§5.2).
async function tx(name, mode, fn, options) {
  const db = await openDb();
  try {
    return await new Promise((resolve, reject) => {
      let out, err;
      const t = db.transaction(name, mode, options);   // a browser without the options argument ignores it
      const fail = (e) => { err = e; try { t.abort(); } catch { /* already finished */ } };
      t.oncomplete = () => (err ? reject(err) : resolve(out));
      t.onabort = t.onerror = () => reject(err || new BridgeStorageError("IndexedDB transaction failed", t.error));
      try { fn(t.objectStore(name), (v) => { out = v; }, fail); } catch (e) { fail(e); }
    });
  } finally {
    db.close();
  }
}
const step = (req, fail, then) => { req.onsuccess = () => { try { then(req.result); } catch (e) { fail(e); } }; };

// This browser's device key: loaded, or created once. Two tabs creating at the same time keep the first one stored.
export async function deviceKey() {
  const have = await tx(DEVICE, "readonly", (s, done) => { const g = s.get(SLOT); g.onsuccess = () => done(g.result); });
  if (have) return have;
  const fresh = await newDeviceKey();
  return tx(DEVICE, "readwrite", (s, done, fail) => step(s.get(SLOT), fail, (cur) => {
    if (cur) return done(cur);
    s.put(fresh, SLOT);
    done(fresh);
  }));
}

function checkWs(ws) {
  if (!HEX32.test(ws)) throw new TypeError("workspace must be 32 lower-case hex");
}

// K_ws (§2.2, §10.1): MK re-opened exactly as approve.js does (the stored KEK, GET /api/keyblob, AAD "sharing/mk/v1"),
// HKDF to K_ws, raw bytes zeroed, K_ws kept non-extractable. No passphrase is asked for or stored.
export async function openWorkspaceKey(workspace, { keys = loadKeys, keyblob = () => api("GET", "/api/keyblob") } = {}) {
  checkWs(workspace);
  const k = await keys();
  if (!k?.kek) throw new BridgeStorageError("not signed in");
  const { wrapped_mk, key_version } = await keyblob();
  const kWs = await workspaceKey(await openSealed(k.kek, unb64u(wrapped_mk), AAD_MK), workspace);   // zeroes the raw MK
  await saveWorkspaceKey(workspace, kWs, key_version);
  return { kWs, keyVersion: key_version };
}

// Keeps the counter: re-deriving K_ws never resets a sequence number.
export async function saveWorkspaceKey(workspace, kWs, keyVersion) {
  checkWs(workspace);
  if (kWs?.extractable !== false) throw new Error("refusing to store an extractable key");
  return tx(HOSTS, "readwrite", (s, done, fail) => step(s.get(workspace), fail, (cur) => {
    s.put({ workspace, hostPub: null, next: 1, ...cur, kWs, keyVersion });
    done();
  }));
}

export const workspaceRecord = (workspace) => tx(HOSTS, "readonly", (s, done) => {
  const g = s.get(workspace);
  g.onsuccess = () => done(g.result ?? null);
});

// The pin (§8.1 step 4, D6): set only from a pairing link's host_pin, never trusted on first use.
export async function pinHost(workspace, hostPub, pin) {
  await hostKeyFromPin(hostPub, pin);   // throws HostKeyError on a mismatch
  return tx(HOSTS, "readwrite", (s, done, fail) => step(s.get(workspace), fail, (cur) => {
    if (!cur) throw new BridgeStorageError("no workspace key for this workspace");
    s.put({ ...cur, hostPub: Uint8Array.from(hostPub) });
    done();
  }));
}

export async function pinnedHostKey(workspace) {
  const rec = await workspaceRecord(workspace);
  return rec?.hostPub ? importPublicKey(rec.hostPub) : null;
}

// §5.2: per (browser, workspace) counter, from 1. next() hands out each value once, across tabs and reloads;
// atLeast(high + 1) is the resync after a host-signed stale_sequence refusal.
export function sequenceCounter(workspace) {
  const update = (f) => tx(HOSTS, "readwrite", (s, done, fail) => step(s.get(workspace), fail, (cur) => {
    if (!cur) throw new BridgeStorageError("no workspace key for this workspace");
    const { next, value } = f(cur.next);
    if (!Number.isSafeInteger(next) || next > MAX_SEQ) throw new BridgeStorageError("sequence exhausted: pair again");
    s.put({ ...cur, next });
    done(value);
  }), { durability: "strict" });             // a handed-out number is flushed to disk, not only to the OS cache
  return {
    next: () => update((n) => ({ next: n + 1, value: n })),
    atLeast: (v) => update((n) => ({ next: Math.max(n, v), value: undefined })),
  };
}

export const forgetWorkspace = (workspace) => tx(HOSTS, "readwrite", (s) => { s.delete(workspace); });
