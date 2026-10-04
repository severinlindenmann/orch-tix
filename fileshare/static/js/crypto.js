// SHR1 (spec §4) in WebCrypto. Must stay byte-identical with skill/sharing/sharing.py;
// tests/vectors/shr1.json is the arbiter.
const subtle = globalThis.crypto.subtle;
const te = new TextEncoder();
const td = new TextDecoder("utf-8", { fatal: true });

export const HEADER_LEN = 34;
export const CHUNK = 1 << 20;
const MAX_CHUNK = 64 << 20;
const TAG_LEN = 16;
const MAGIC = te.encode("SHR1");

export class IntegrityError extends Error {
  constructor(message) { super(message); this.name = "IntegrityError"; }
}

function concat(...parts) {
  let n = 0;
  for (const p of parts) n += p.length;
  const out = new Uint8Array(n);
  let off = 0;
  for (const p of parts) { out.set(p, off); off += p.length; }
  return out;
}

function u32(i) {
  const b = new Uint8Array(4);
  new DataView(b.buffer).setUint32(0, i, false);
  return b;
}

function randomBytes(n) { return globalThis.crypto.getRandomValues(new Uint8Array(n)); }

function checkLen(name, v, n) {
  if (!(v instanceof Uint8Array) || v.length !== n) throw new TypeError(`${name} must be ${n} bytes`);
}

export const AAD_MK = te.encode("sharing/mk/v1");
export const aadDek = (uuid) => concat(te.encode("sharing/dek/v1|"), uuid);
export const aadMeta = (uuid) => concat(te.encode("sharing/meta/v1|"), uuid);
const TDEK_PREFIX = te.encode("sharing/tdek/v1|");
const TICKET_PREFIX = te.encode("sharing/ticket/v1|");
const TEVENT_PREFIX = te.encode("sharing/tevent/v1|");
export const aadTdek = (uuid) => concat(TDEK_PREFIX, uuid);
export const aadTicket = (uuid) => concat(TICKET_PREFIX, uuid);
export const aadTevent = (ticketUuid, eventUuid) => concat(TEVENT_PREFIX, ticketUuid, eventUuid);
const APPROVE_PREFIX = te.encode("sharing/approve/v1|");
const FP_PREFIX = te.encode("sharing/fp/v1|");
export const aadApprove = (deviceId) => concat(APPROVE_PREFIX, te.encode(deviceId));

export function b64u(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function unb64u(str) {
  if (typeof str !== "string" || !/^[A-Za-z0-9_-]*$/.test(str) || str.length % 4 === 1) {
    throw new TypeError("invalid base64url");
  }
  const bin = atob(str.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (str.length % 4)) % 4));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  if (b64u(out) !== str) throw new TypeError("non-canonical base64url");
  return out;
}

export function hexToBytes(hex) {
  if (typeof hex !== "string" || !/^([0-9a-fA-F]{2})*$/.test(hex)) throw new TypeError("invalid hex");
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(2 * i, 2 * i + 2), 16);
  return out;
}

export function bytesToHex(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += bytes[i].toString(16).padStart(2, "0");
  return s;
}

export async function importAesKey(raw, extractable = false) {
  checkLen("key", raw, 32);
  return subtle.importKey("raw", raw, { name: "AES-GCM" }, extractable, ["encrypt", "decrypt"]);
}

async function asKey(k) {
  return k instanceof Uint8Array ? importAesKey(k, false) : k;
}

export async function seal(key, pt, aad, nonce) {
  const iv = nonce ?? randomBytes(12);
  checkLen("nonce", iv, 12);
  const ct = new Uint8Array(await subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: aad, tagLength: 128 }, await asKey(key), pt));
  return concat(new Uint8Array([1]), iv, ct);
}

export async function open(key, env, aad) {
  if (!(env instanceof Uint8Array) || env.length < 1 + 12 + TAG_LEN || env[0] !== 1) {
    throw new IntegrityError("malformed envelope");
  }
  const k = await asKey(key);
  try {
    return new Uint8Array(await subtle.decrypt(
      { name: "AES-GCM", iv: env.subarray(1, 13), additionalData: aad, tagLength: 128 }, k, env.subarray(13)));
  } catch {
    throw new IntegrityError("envelope failed authentication");
  }
}

export async function deriveRaw(passphrase, salt, iterations) {
  const pw = te.encode(passphrase.normalize("NFC"));
  const base = await subtle.importKey("raw", pw, "PBKDF2", false, ["deriveBits"]);
  const root = new Uint8Array(await subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt, iterations }, base, 256));
  const hk = await subtle.importKey("raw", root, "HKDF", false, ["deriveBits"]);
  const expand = async (info) => new Uint8Array(await subtle.deriveBits(
    { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(0), info: te.encode(info) }, hk, 256));
  root.fill(0);
  return { authKey: await expand("sharing/auth/v1"), kek: await expand("sharing/kek/v1") };
}

function blobHeader(keyVersion, fileUuid, chunkSize, noncePrefix) {
  const h = new Uint8Array(HEADER_LEN);
  h.set(MAGIC, 0);
  h[4] = 1;
  h[5] = keyVersion;
  h.set(fileUuid, 6);
  new DataView(h.buffer).setUint32(22, chunkSize, false);
  h.set(noncePrefix, 26);
  return h;
}

export async function encryptBlob(dek, fileUuid, keyVersion, plaintext, { chunkSize = CHUNK, noncePrefix } = {}) {
  checkLen("fileUuid", fileUuid, 16);
  if (!Number.isInteger(keyVersion) || keyVersion < 0 || keyVersion > 255) throw new TypeError("bad keyVersion");
  if (!Number.isInteger(chunkSize) || chunkSize <= 0 || chunkSize > MAX_CHUNK) throw new TypeError("bad chunkSize");
  const prefix = noncePrefix ?? randomBytes(8);
  checkLen("noncePrefix", prefix, 8);
  const key = await asKey(dek);
  const header = blobHeader(keyVersion, fileUuid, chunkSize, prefix);
  const n = Math.max(1, Math.ceil(plaintext.length / chunkSize));
  const parts = [header];
  for (let i = 0; i < n; i++) {
    const chunk = plaintext.subarray(i * chunkSize, Math.min((i + 1) * chunkSize, plaintext.length));
    const isLast = i === n - 1 ? 1 : 0;
    const ct = await subtle.encrypt(
      { name: "AES-GCM", iv: concat(prefix, u32(i)), additionalData: concat(header, u32(i), new Uint8Array([isLast])), tagLength: 128 },
      key, chunk);
    parts.push(new Uint8Array(ct));
  }
  return concat(...parts);
}

export async function decryptBlob(dek, expectedUuid, blob) {
  checkLen("expectedUuid", expectedUuid, 16);
  if (!(blob instanceof Uint8Array) || blob.length < HEADER_LEN + TAG_LEN) throw new IntegrityError("truncated blob");
  const header = blob.subarray(0, HEADER_LEN);
  for (let i = 0; i < 4; i++) if (header[i] !== MAGIC[i]) throw new IntegrityError("not an SHR1 blob");
  if (header[4] !== 1) throw new IntegrityError("not an SHR1 blob");
  for (let i = 0; i < 16; i++) if (header[6 + i] !== expectedUuid[i]) throw new IntegrityError("blob belongs to a different file");
  const chunkSize = new DataView(header.buffer, header.byteOffset, HEADER_LEN).getUint32(22, false);
  if (chunkSize === 0 || chunkSize > MAX_CHUNK) throw new IntegrityError("bad chunk size");
  const prefix = header.subarray(26, 34);
  const step = chunkSize + TAG_LEN;
  const body = blob.subarray(HEADER_LEN);
  const n = Math.ceil(body.length / step);
  if (body.length - (n - 1) * step < TAG_LEN) throw new IntegrityError("truncated blob");
  const key = await asKey(dek);
  const hdr = header.slice();
  const out = [];
  for (let i = 0; i < n; i++) {
    const ct = body.subarray(i * step, Math.min((i + 1) * step, body.length));
    const isLast = i === n - 1 ? 1 : 0;
    try {
      out.push(new Uint8Array(await subtle.decrypt(
        { name: "AES-GCM", iv: concat(prefix, u32(i)), additionalData: concat(hdr, u32(i), new Uint8Array([isLast])), tagLength: 128 },
        key, ct)));
    } catch {
      throw new IntegrityError(`chunk ${i} failed authentication`);
    }
  }
  return concat(...out);
}

// Python: json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).
// Keys are sorted by code point (Python's str order), not by UTF-16 unit; JSON.stringify
// keeps non-ASCII raw and escapes exactly what Python escapes for strings.
export function canonicalJson(v) {
  if (Array.isArray(v)) return `[${v.map(canonicalJson).join(",")}]`;
  if (v && typeof v === "object") {
    const keys = Object.keys(v).sort((a, b) => {
      const x = Array.from(a), y = Array.from(b);
      for (let i = 0; i < Math.min(x.length, y.length); i++) {
        const d = x[i].codePointAt(0) - y[i].codePointAt(0);
        if (d) return d;
      }
      return x.length - y.length;
    });
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(v[k])}`).join(",")}}`;
  }
  return JSON.stringify(v);
}

export const AAD_SETTINGS = te.encode("sharing/settings/v1");

const isPlainObject = (o) => !!o && typeof o === "object" && !Array.isArray(o);

// Account-wide settings (spec §15): seal(MK, canonical JSON, AAD_SETTINGS) as base64url.
export async function sealSettings(mkKey, obj, { nonce } = {}) {
  if (!isPlainObject(obj)) throw new TypeError("settings must be a JSON object");
  return b64u(await seal(mkKey, te.encode(canonicalJson(obj)), AAD_SETTINGS, nonce));
}

// The whole decrypted object, unknown keys included, so a re-seal preserves them.
export async function openSettings(mkKey, encSettings) {
  let env;
  try {
    env = unb64u(encSettings);
  } catch {
    throw new IntegrityError("malformed settings envelope");
  }
  let obj;
  try {
    obj = JSON.parse(td.decode(await open(mkKey, env, AAD_SETTINGS)));
  } catch (e) {
    if (e instanceof IntegrityError) throw e;
    throw new IntegrityError("settings are not valid JSON");
  }
  if (!isPlainObject(obj)) throw new IntegrityError("settings are not a JSON object");
  return obj;
}

export async function newFileCrypto(mkKey, { name, mime, note }, { rng } = {}) {
  const r = rng ?? randomBytes;
  const uuid = r(16);
  const dek = r(32);
  const wrapped = await seal(mkKey, dek, aadDek(uuid), r(12));
  // Python: json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False).
  // The literal is written in sorted key order, and JSON.stringify keeps non-ASCII raw.
  const metaJson = JSON.stringify({ mime, name, note });
  const encMeta = await seal(dek, te.encode(metaJson), aadMeta(uuid), r(12));
  return { uuid, uuidHex: bytesToHex(uuid), dek, wrappedDek: b64u(wrapped), encMeta: b64u(encMeta) };
}

export async function openFileMeta(mkKey, fileOut) {
  if (!fileOut.wrapped_dek || !fileOut.enc_meta) throw new Error("file has no key (deleted)");
  let uuid, wrapped, enc;
  try {
    uuid = hexToBytes(fileOut.uuid);
    wrapped = unb64u(fileOut.wrapped_dek);
    enc = unb64u(fileOut.enc_meta);
  } catch {
    throw new IntegrityError("malformed file record");
  }
  if (uuid.length !== 16) throw new IntegrityError("malformed file uuid");
  const dek = await open(mkKey, wrapped, aadDek(uuid));
  return { meta: await openMeta(dek, uuid, enc), dek };
}

// The whole decrypted meta object, keys this UI doesn't know included. `enc` is bytes or base64url.
export async function openMetaObject(dek, uuid, enc) {
  let meta;
  try {
    const env = typeof enc === "string" ? unb64u(enc) : enc;
    meta = JSON.parse(td.decode(await open(dek, env, aadMeta(uuid))));
  } catch (e) {
    if (e instanceof IntegrityError) throw e;
    throw new IntegrityError("metadata is not valid JSON");
  }
  if (!isPlainObject(meta) || !["name", "mime", "note"].every((k) => typeof meta[k] === "string")) {
    throw new IntegrityError("metadata has the wrong shape");
  }
  return meta;
}

// Only the fields this UI knows. A re-seal (the transcript, §20) starts from the full object
// instead (openFileMetaFull), so keys this UI doesn't know survive.
const metaView = (meta) => ({ name: meta.name, mime: meta.mime, note: meta.note, transcript: validTranscript(meta.transcript) });

async function openMeta(dek, uuid, enc) {
  return metaView(await openMetaObject(dek, uuid, enc));
}

// ---- transcription (Task 35): re-sealing a file's meta (spec §14 G, §20)

// {raw, meta, dek, uuid}: raw is the whole decrypted object, meta the validated view openFileMeta gives.
export async function openFileMetaFull(mkKey, fileOut) {
  const { dek } = await openFileMeta(mkKey, fileOut);  // validates the record and unwraps the DEK
  const uuid = hexToBytes(fileOut.uuid);
  const raw = await openMetaObject(dek, uuid, fileOut.enc_meta);
  return { raw, meta: metaView(raw), dek, uuid };
}

// seal(DEK, canonical JSON, AAD_META(uuid)) under a fresh nonce, base64url: the same bytes Python's
// seal_file_meta produces (tests/vectors/shr1.json "meta_reseal").
export async function sealFileMeta(dek, uuid, meta, { nonce } = {}) {
  if (!isPlainObject(meta)) throw new TypeError("meta must be a JSON object");
  return b64u(await seal(dek, te.encode(canonicalJson(meta)), aadMeta(uuid), nonce));
}

// ------------------------------------------------------------ tickets (spec T3, T5)

// A fresh ticket DEK, wrapped under MK, and content sealed under it (T3). content = {title, body, fm}.
// dek travels as raw bytes, exactly like a file's DEK (fc.dek): seal()/open() import it as a
// non-extractable AES-GCM CryptoKey via asKey() whenever it's used.
export async function newTicketCrypto(mkKey, content, { rng } = {}) {
  const r = rng ?? randomBytes;
  const uuid = r(16);
  const dek = r(32);
  const uuidHex = bytesToHex(uuid);
  const wrapped = await seal(mkKey, dek, aadTdek(uuid), r(12));
  const encContent = await sealTicketContent(dek, uuidHex, content, { nonce: r(12) });
  return { uuid: uuidHex, key_version: 1, wrapped_dek: b64u(wrapped), enc_content: encContent, dek };
}

// {content, dek}. content is the whole decrypted object, so a re-seal keeps unknown fm keys.
export async function openTicket(mkKey, t) {
  if (!t.wrapped_dek || !t.enc_content) throw new Error("ticket has no key (deleted)");
  let uuid, wrapped, enc;
  try {
    uuid = hexToBytes(t.uuid);
    wrapped = unb64u(t.wrapped_dek);
    enc = unb64u(t.enc_content);
  } catch {
    throw new IntegrityError("malformed ticket record");
  }
  if (uuid.length !== 16) throw new IntegrityError("malformed ticket uuid");
  const dek = await open(mkKey, wrapped, aadTdek(uuid));
  let content;
  try {
    content = JSON.parse(td.decode(await open(dek, enc, aadTicket(uuid))));
  } catch (e) {
    if (e instanceof IntegrityError) throw e;
    throw new IntegrityError("ticket content is not valid JSON");
  }
  if (!isPlainObject(content) || typeof content.title !== "string" || typeof content.body !== "string"
      || !isPlainObject(content.fm)) {
    throw new IntegrityError("ticket content has the wrong shape");
  }
  return { content, dek };
}

// enc_content = seal(DEK, canonical JSON, AAD_TICKET(uuid)), base64url. An edit re-seals with a fresh nonce.
export async function sealTicketContent(dek, uuidHex, content, { nonce } = {}) {
  if (!isPlainObject(content)) throw new TypeError("content must be a JSON object");
  return b64u(await seal(dek, te.encode(canonicalJson(content)), aadTicket(hexToBytes(uuidHex)), nonce));
}

// enc_body = seal(DEK, canonical JSON, AAD_TEVENT(ticket_uuid, event_uuid)), base64url (T5).
export async function sealTicketEvent(dek, ticketUuidHex, eventUuidHex, body, { nonce } = {}) {
  if (!isPlainObject(body)) throw new TypeError("event body must be a JSON object");
  return b64u(await seal(dek, te.encode(canonicalJson(body)),
    aadTevent(hexToBytes(ticketUuidHex), hexToBytes(eventUuidHex)), nonce));
}

export async function openTicketEvent(dek, ticketUuidHex, eventUuidHex, enc) {
  let env;
  try {
    env = unb64u(enc);
  } catch {
    throw new IntegrityError("malformed event envelope");
  }
  let body;
  try {
    body = JSON.parse(td.decode(await open(dek, env, aadTevent(hexToBytes(ticketUuidHex), hexToBytes(eventUuidHex)))));
  } catch (e) {
    if (e instanceof IntegrityError) throw e;
    throw new IntegrityError("event body is not valid JSON");
  }
  if (!isPlainObject(body)) throw new IntegrityError("event body is not a JSON object");
  return body;
}

// ------------------------------------------------------------ public links (§17)

export const LINK_KEY_LEN = 32;
const LINK_PREFIX = te.encode("sharing/link/v1|");
export const aadLink = (uuid) => concat(LINK_PREFIX, uuid);

export function newLinkKey() { return randomBytes(LINK_KEY_LEN); }

// The link key from a URL fragment (without the "#"): exactly 32 bytes of canonical base64url.
export function parseLinkKey(fragment) {
  let lk;
  try {
    lk = unb64u(fragment);
  } catch {
    throw new IntegrityError("malformed link key");
  }
  if (lk.length !== LINK_KEY_LEN) throw new IntegrityError("malformed link key");
  return lk;
}

// wrapped_dek_link = seal(LK, DEK, AAD_LINK(uuid)), base64url. dek and lk are raw bytes.
export async function wrapDekForLink(dek, uuid, lk, { nonce } = {}) {
  checkLen("dek", dek, 32);
  checkLen("uuid", uuid, 16);
  checkLen("lk", lk, LINK_KEY_LEN);
  return b64u(await seal(lk, dek, aadLink(uuid), nonce));
}

export async function openLinkDek(lk, uuid, wrappedDekLink) {
  let env;
  try {
    env = unb64u(wrappedDekLink);
    checkLen("lk", lk, LINK_KEY_LEN);
    checkLen("uuid", uuid, 16);
  } catch {
    throw new IntegrityError("malformed link envelope");
  }
  const dek = await open(lk, env, aadLink(uuid));
  if (dek.length !== 32) throw new IntegrityError("link envelope does not hold a DEK");
  return dek;
}

// {meta, dek} from GET /api/public/{token}; the meta has the same shape as openFileMeta's.
export async function openPublicFile(lk, pub) {
  let uuid, enc;
  try {
    uuid = hexToBytes(pub.uuid);
    enc = unb64u(pub.enc_meta);
  } catch {
    throw new IntegrityError("malformed public file record");
  }
  if (uuid.length !== 16) throw new IntegrityError("malformed file uuid");
  const dek = await openLinkDek(lk, uuid, pub.wrapped_dek_link);
  return { meta: await openMeta(dek, uuid, enc), dek };
}

export const TRANSCRIPT_MAX_CHARS = 1_000_000;
const TRANSCRIPT_FIELDS = ["language", "model", "created_at", "by"];

// A transcript (spec §14 G) is written by any device and is untrusted: accept exactly
// {text, language, model, created_at, by}, all strings, text at most 1,000,000 characters.
// Anything else is ignored (null), never an error: the audio itself is still fine.
export function validTranscript(t) {
  if (!t || typeof t !== "object" || Array.isArray(t)) return null;
  if (typeof t.text !== "string" || t.text.length > TRANSCRIPT_MAX_CHARS) return null;
  if (!TRANSCRIPT_FIELDS.every((k) => typeof t[k] === "string")) return null;
  return { text: t.text, language: t.language, model: t.model, created_at: t.created_at, by: t.by };
}

// ------------------------------------------------------------ device approval (§4.6)

const ECDH = { name: "ECDH", namedCurve: "P-256" };
const B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

function base32(bytes) {                        // RFC 4648, upper case, no padding
  let bits = 0, value = 0, out = "";
  for (const b of bytes) {
    value = (value << 8) | b; bits += 8;
    while (bits >= 5) { out += B32[(value >>> (bits - 5)) & 31]; bits -= 5; }
    value &= (1 << bits) - 1;
  }
  if (bits > 0) out += B32[(value << (5 - bits)) & 31];
  return out;
}

function checkPoint(pub) {
  if (!(pub instanceof Uint8Array) || pub.length !== 65 || pub[0] !== 4) {
    throw new TypeError("device public key must be a 65-byte uncompressed P-256 point");
  }
}

export async function fingerprint(devicePub) {
  // 100 bits: the first 20 base32 chars of the hash, grouped 4-4-4-4-4 (§4.6). Must stay
  // byte-identical with skill/sharing/sharing.py and fileshare/security.py.
  const h = new Uint8Array(await subtle.digest("SHA-256", concat(FP_PREFIX, devicePub)));
  const raw = base32(h).slice(0, 20);
  return [raw.slice(0, 4), raw.slice(4, 8), raw.slice(8, 12), raw.slice(12, 16), raw.slice(16, 20)].join("-");
}

export async function approvalKey(z, ephPub, devicePub, deviceId) {
  const k = await subtle.importKey("raw", z, "HKDF", false, ["deriveBits"]);
  return new Uint8Array(await subtle.deriveBits(
    { name: "HKDF", hash: "SHA-256", salt: concat(ephPub, devicePub), info: aadApprove(deviceId) }, k, 256));
}

async function ephemeral(ephPrivPkcs8) {
  if (!ephPrivPkcs8) {
    const kp = await subtle.generateKey(ECDH, true, ["deriveBits"]);
    return { priv: kp.privateKey, pub: new Uint8Array(await subtle.exportKey("raw", kp.publicKey)) };
  }
  // Fixed key (vector tests only): WebCrypto cannot derive a public key from a private one,
  // so import extractable and rebuild the point from the JWK's x and y.
  const priv = await subtle.importKey("pkcs8", ephPrivPkcs8, ECDH, true, ["deriveBits"]);
  const jwk = await subtle.exportKey("jwk", priv);
  return { priv, pub: concat(new Uint8Array([4]), unb64u(jwk.x), unb64u(jwk.y)) };
}

export async function sealToDevice(mkRaw, devicePub, deviceId, { ephPrivPkcs8, nonce } = {}) {
  checkLen("mk", mkRaw, 32);
  checkPoint(devicePub);
  // importKey validates that the point is on the curve (throws otherwise)
  const devKey = await subtle.importKey("raw", devicePub, ECDH, false, []);
  const eph = await ephemeral(ephPrivPkcs8);
  const z = new Uint8Array(await subtle.deriveBits({ name: "ECDH", public: devKey }, eph.priv, 256));
  const wk = await approvalKey(z, eph.pub, devicePub, deviceId);
  z.fill(0);
  const env = await seal(wk, mkRaw, aadApprove(deviceId), nonce);
  wk.fill(0);
  return concat(eph.pub, env);
}

// ------------------------------------------------------------ upload links (spec §18)

export const ULINK_PRIV_LEN = 32 + 65;                    // d ‖ uncompressed pub
export const SEALED_DEK_LEN = 65 + 1 + 12 + 32 + 16;       // eph_pub ‖ envelope(dek)
const ULINK_PREFIX = te.encode("sharing/ulink/v1|");
const ULABEL_PREFIX = te.encode("sharing/ulabel/v1|");
const ULDEK_PREFIX = te.encode("sharing/uldek/v1|");
export const aadUlink = (linkUuid) => concat(ULINK_PREFIX, linkUuid);
export const aadUlabel = (linkUuid) => concat(ULABEL_PREFIX, linkUuid);
export const aadUldek = (linkUuid, fileUuid) => concat(ULDEK_PREFIX, linkUuid, fileUuid);

// {d, pub}: either the fixed key a vector test supplies (a JWK-shaped {d, x, y}, all base64url — x
// and y come from the vector's own recorded pub, exactly as openUploadLinkKey reconstructs one from
// a stored d ‖ pub) or a freshly generated P-256 keypair, exported to get its raw d and pub bytes.
async function ulinkKeyPair(linkPrivJwk) {
  if (linkPrivJwk) {
    return { d: unb64u(linkPrivJwk.d), pub: concat(new Uint8Array([4]), unb64u(linkPrivJwk.x), unb64u(linkPrivJwk.y)) };
  }
  const kp = await subtle.generateKey(ECDH, true, ["deriveBits"]);
  const jwk = await subtle.exportKey("jwk", kp.privateKey);
  return { d: unb64u(jwk.d), pub: concat(new Uint8Array([4]), unb64u(jwk.x), unb64u(jwk.y)) };
}

// A fresh P-256 keypair for one upload link: wrappedLpriv = seal(mkKey, d ‖ pub, aadUlink(uuid)), so
// only the owner (who holds mkKey) can ever recover it. encLabel is null when label === "".
export async function newUploadLink(mkKey, label, { linkPrivJwk, uuid, nonce } = {}) {
  const linkUuid = uuid ?? randomBytes(16);
  const { d, pub } = await ulinkKeyPair(linkPrivJwk);
  const wrappedLpriv = b64u(await seal(mkKey, concat(d, pub), aadUlink(linkUuid), nonce));
  let encLabel = null;
  if (label !== "") {
    encLabel = b64u(await seal(mkKey, te.encode(label), aadUlabel(linkUuid)));
  }
  return { uuidHex: bytesToHex(linkUuid), pub, wrappedLpriv, encLabel };
}

// {priv, pub}: unwraps wrappedLpriv under mkKey and imports d ‖ pub as an ECDH private key via JWK
// (x = pub[1..33], y = pub[33..65]). A d that doesn't match the stored pub is an inconsistent JWK,
// which WebCrypto's own key-import validation rejects.
export async function openUploadLinkKey(mkKey, uuidHex, wrappedLpriv) {
  const linkUuid = hexToBytes(uuidHex);
  let env;
  try {
    env = unb64u(wrappedLpriv);
  } catch {
    throw new IntegrityError("malformed upload-link envelope");
  }
  const plain = await open(mkKey, env, aadUlink(linkUuid));
  if (plain.length !== ULINK_PRIV_LEN) throw new IntegrityError("upload-link key has the wrong length");
  const d = plain.subarray(0, 32), pub = plain.subarray(32);
  const jwk = { kty: "EC", crv: "P-256", d: b64u(d), x: b64u(pub.subarray(1, 33)), y: b64u(pub.subarray(33, 65)) };
  let priv;
  try {
    priv = await subtle.importKey("jwk", jwk, ECDH, false, ["deriveBits"]);
  } catch {
    throw new IntegrityError("upload-link private key does not match its public key");
  }
  return { priv, pub };
}

// The link's label, or "" when it has none (encLabel === null).
export async function openUploadLabel(mkKey, uuidHex, encLabel) {
  if (encLabel == null) return "";
  const linkUuid = hexToBytes(uuidHex);
  let env;
  try {
    env = unb64u(encLabel);
  } catch {
    throw new IntegrityError("malformed upload-link label envelope");
  }
  try {
    return td.decode(await open(mkKey, env, aadUlabel(linkUuid)));
  } catch (e) {
    if (e instanceof IntegrityError) throw e;
    throw new IntegrityError("upload-link label is not valid UTF-8");
  }
}

async function uldekKey(z, ephPub, linkPub, linkUuid, fileUuid) {
  const k = await subtle.importKey("raw", z, "HKDF", false, ["deriveBits"]);
  return new Uint8Array(await subtle.deriveBits(
    { name: "HKDF", hash: "SHA-256", salt: concat(ephPub, linkPub), info: aadUldek(linkUuid, fileUuid) }, k, 256));
}

// What the uploader (drop page) does with the link's public key: sealedDek = ephPub ‖
// seal(wk, dek, aadUldek), wk = HKDF-SHA256(ECDH(eph, linkPub), salt=ephPub‖linkPub, info=aadUldek).
export async function sealDekToLink(dekRaw, linkPub, linkUuidHex, fileUuidHex, { ephPrivPkcs8, nonce } = {}) {
  checkLen("dek", dekRaw, 32);
  checkPoint(linkPub);
  const linkKey = await subtle.importKey("raw", linkPub, ECDH, false, []);
  const eph = await ephemeral(ephPrivPkcs8);
  const linkUuid = hexToBytes(linkUuidHex), fileUuid = hexToBytes(fileUuidHex);
  const z = new Uint8Array(await subtle.deriveBits({ name: "ECDH", public: linkKey }, eph.priv, 256));
  const wk = await uldekKey(z, eph.pub, linkPub, linkUuid, fileUuid);
  const env = await seal(wk, dekRaw, aadUldek(linkUuid, fileUuid), nonce);
  return b64u(concat(eph.pub, env));
}

// The link owner's side: recovers the 32-byte DEK a drop-page upload sealed to this link. linkKey is
// the {priv, pub} openUploadLinkKey returned.
export async function openSealedDek(linkKey, linkUuidHex, fileUuidHex, sealedB64) {
  const linkUuid = hexToBytes(linkUuidHex), fileUuid = hexToBytes(fileUuidHex);
  let blob;
  try {
    blob = unb64u(sealedB64);
  } catch {
    throw new IntegrityError("malformed sealed DEK");
  }
  if (blob.length !== SEALED_DEK_LEN) throw new IntegrityError("sealed DEK has the wrong length");
  const ephPub = blob.subarray(0, 65);
  let ephKey;
  try {
    checkPoint(ephPub);
    ephKey = await subtle.importKey("raw", ephPub, ECDH, false, []);
  } catch {
    throw new IntegrityError("sealed DEK carries an invalid ephemeral key");
  }
  const z = new Uint8Array(await subtle.deriveBits({ name: "ECDH", public: ephKey }, linkKey.priv, 256));
  const wk = await uldekKey(z, ephPub, linkKey.pub, linkUuid, fileUuid);
  const dek = await open(wk, blob.subarray(65), aadUldek(linkUuid, fileUuid));
  if (dek.length !== 32) throw new IntegrityError("sealed envelope does not hold a 32-byte DEK");
  return dek;
}

// The uploader (drop page) side: a fresh file uuid and DEK, the DEK sealed to the link's public key
// (never to an mk the uploader doesn't have), and metadata sealed under the DEK as usual.
export async function newDropFileCrypto(linkPub, linkUuidHex, { name, mime, note }) {
  const uuid = randomBytes(16);
  const uuidHex = bytesToHex(uuid);
  const dekRaw = randomBytes(32);
  const sealedDek = await sealDekToLink(dekRaw, linkPub, linkUuidHex, uuidHex);
  const encMeta = await sealFileMeta(dekRaw, uuid, { name, mime, note });
  const dek = await importAesKey(dekRaw, false);
  return { uuid, uuidHex, dek, sealedDek, encMeta };
}

// Adoption: wraps a DEK the owner recovered from openSealedDek under mkKey, exactly like
// newFileCrypto's wrappedDek, so an adopted upload-link file is indistinguishable from any other.
export async function adoptWrappedDek(mkKey, dekRaw, fileUuidHex) {
  return b64u(await seal(mkKey, dekRaw, aadDek(hexToBytes(fileUuidHex))));
}
