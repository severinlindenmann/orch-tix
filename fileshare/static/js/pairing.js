// fileshare/static/js/pairing.js — this phone paired with a desktop workspace (orch-core remote humans,
// spec §6.4). Not an entry module.
//
// The desktop (orch-core) makes a pairing link https://<server>/pair#<space>.<phone_id>.<key>, the key
// being 32 random bytes in base64url. The key only ever lives in that URL fragment (which no browser
// sends to a server) and then, after the human compared the check code, in IndexedDB as a
// non-extractable HMAC-SHA256 CryptoKey that can only sign. It is never sent, never logged, never
// shown. A paired phone signs each decision: `pair` = the phone id, `mac` = base64url HMAC-SHA256 of
// the canonical JSON of the decision without `mac` (orch-core orch.remote.verify.mac_of). Core checks
// it and applies the decision as the human; without a pairing core waits for an Apply on the desktop.
import { b64u, canonicalJson } from "./crypto.js";
import { PAIRS, withStore } from "./db.js";

const te = new TextEncoder();
const subtle = () => globalThis.crypto.subtle;
const SPACE = /^[0-9a-f]{32}$/;
const PHONE_ID = /^ph_[0-9a-f]{12}$/;
const KEY_B64 = /^[A-Za-z0-9_-]{43}$/;
const FRAGMENT = /^#?([0-9a-f]{32})\.(ph_[0-9a-f]{12})\.([A-Za-z0-9_-]{43})$/;
const CHECK_PREFIX = te.encode("orch/pair/v1|");

// base64url without padding -> bytes, only for the canonical spelling (re-encodes to the same text).
function keyBytes(text) {
  if (!KEY_B64.test(text)) return null;
  let bin;
  try {
    bin = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "=");
  } catch {
    return null;
  }
  const out = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  if (out.length !== 32 || b64u(out) !== text) {
    out.fill(0);
    return null;
  }
  return out;
}

// "#<space>.<phone_id>.<key>" (the "#" optional) -> {space, phoneId, rawKey} or null. Strict: 32 lower
// hex, "ph_" + 12 lower hex, 43 canonical base64url characters (32 bytes).
export function parsePairFragment(hash) {
  if (typeof hash !== "string") return null;
  const m = FRAGMENT.exec(hash);
  if (!m) return null;
  const rawKey = keyBytes(m[3]);
  return rawKey ? { space: m[1], phoneId: m[2], rawKey } : null;
}

// A pasted pairing link: this server's /pair with the fragment, or null.
export function parsePairLink(text, origin = globalThis.location?.origin) {
  let url;
  try {
    url = new URL(String(text ?? "").trim());
  } catch {
    return null;
  }
  if (url.origin !== origin || url.pathname !== "/pair" || url.search) return null;
  return parsePairFragment(url.hash);
}

// The 6-digit code both screens show: sha256("orch/pair/v1|" + key), first 4 bytes big-endian, mod 1e6
// (orch-core orch.remote.store.check_code).
export async function checkCode(rawKey) {
  const msg = new Uint8Array(CHECK_PREFIX.length + rawKey.length);
  msg.set(CHECK_PREFIX);
  msg.set(rawKey, CHECK_PREFIX.length);
  const digest = new DataView(await subtle().digest("SHA-256", msg));
  msg.fill(0);
  return String(digest.getUint32(0, false) % 1_000_000).padStart(6, "0");
}

// The IndexedDB store "pairs" (db.js), keyed by space. Tests pass an object with the same four methods.
export const pairsDb = Object.freeze({
  put: (rec) => withStore(PAIRS, "readwrite", (s) => s.put(rec)),
  get: async (space) => (await withStore(PAIRS, "readonly", (s) => s.get(space))) ?? null,
  delete: (space) => withStore(PAIRS, "readwrite", (s) => s.delete(space)),
  all: async () => (await withStore(PAIRS, "readonly", (s) => s.getAll())) ?? [],
});

const isoNow = () => new Date().toISOString().replace(/\.\d{3}Z$/, "Z");

// Imports the key as a non-extractable HMAC-SHA256 key that can only sign, zeroes the raw bytes and
// stores {space, phoneId, key, label, paired_at}. A second pairing for the same space replaces the first.
export async function storePairing(db, { space, phoneId, rawKey, label }) {
  try {
    if (!SPACE.test(String(space)) || !PHONE_ID.test(String(phoneId))) throw new TypeError("not a pairing");
    if (!(rawKey instanceof Uint8Array) || rawKey.length !== 32) throw new TypeError("a pairing key is 32 bytes");
    const key = await subtle().importKey("raw", rawKey, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    if (key.extractable) throw new Error("refusing to store an extractable key");
    await (db || pairsDb).put({ space, phoneId, key, label: String(label ?? "").slice(0, 80), paired_at: isoNow() });
  } finally {
    if (rawKey instanceof Uint8Array) rawKey.fill(0);
  }
}

export async function pairingFor(db, space) {
  const rec = await (db || pairsDb).get(space);
  return rec && PHONE_ID.test(String(rec.phoneId)) && rec.key ? { phoneId: rec.phoneId, key: rec.key } : null;
}

// Every pairing on this phone, for Settings: {space, phoneId, label, paired_at}. Never the key.
export async function pairings(db) {
  const all = await (db || pairsDb).all();
  return all.map(({ space, phoneId, label, paired_at }) => ({ space, phoneId, label, paired_at }));
}

export async function forgetPairing(db, space) {
  await (db || pairsDb).delete(space);
}

// A copy of the decision with `pair` set and `mac` = HMAC over the canonical JSON of the rest.
export async function signDecision(pairing, decision) {
  const copy = { ...decision, pair: pairing.phoneId };
  delete copy.mac;
  const sig = await subtle().sign("HMAC", pairing.key, te.encode(canonicalJson(copy)));
  return { ...copy, mac: b64u(new Uint8Array(sig)) };
}
