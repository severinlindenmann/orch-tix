// fileshare/static/js/bridge-crypto.js: the device half of the bridge protocol (docs/bridge-protocol.md), WebCrypto
// only. Bytes and checks; no network, no storage (bridge-store.js), no UI. tests/bridge_vectors.json is the contract.
import { b64u, base32, bytesToHex, canonicalJson, hexToBytes } from "./crypto.js";

const subtle = () => globalThis.crypto.subtle;
const te = new TextEncoder();
const td = new TextDecoder("utf-8", { fatal: true });
const label = (s) => te.encode(`sharing/bridge/${s}`);
const L_WS = "sharing/bridge/ws/v1|", L_MSG = label("msg/v1"), L_SIG = label("sig/v1|"), L_DEVICE = label("device/v1|");
const L_FP = label("fp/v1|"), L_HOST = label("host/v1|"), L_PAIR = label("pair/v1|"), L_PHONE = label("phone-link/v1|");
const L_ASSERT = label("assert/v1|"), L_REG = label("webauthn-reg/v1|");

export const HEADER_LEN = 104, TAG_LEN = 16, SIG_LEN = 64, OVERHEAD = 184;
export const MAX_REQUEST = 1 << 20, MAX_CHUNK = 256 * 1024, MAX_META = 65536;
export const WINDOW_MS = 300_000, MAX_OFFSET_MS = 86_400_000;   // §5.1
export const TO_HOST = 1, TO_DEVICE = 2, F_LAST = 1, F_STREAM = 2, F_REFUSAL = 4;
export const MAX_SEQ = Number.MAX_SAFE_INTEGER;                 // §5.2: seq stays below 2^53
const P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551n;
const ECDSA = { name: "ECDSA", namedCurve: "P-256" }, SIG = { name: "ECDSA", hash: "SHA-256" };
const HEX32 = /^[0-9a-f]{32}$/;
export const ZERO_ID = new Uint8Array(16);

export function cat(...parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let i = 0;
  for (const p of parts) { out.set(p, i); i += p.length; }
  return out;
}
const sha256 = async (b) => new Uint8Array(await subtle().digest("SHA-256", b));
const random = (n) => globalThis.crypto.getRandomValues(new Uint8Array(n));
const u64 = (v) => { const b = new Uint8Array(8); new DataView(b.buffer).setBigUint64(0, BigInt(v), false); return b; };
function len(name, v, n) {
  if (!(v instanceof Uint8Array) || v.length !== n) throw new TypeError(`${name} must be ${n} bytes`);
  return v;
}
function safeInt(name, v, min) {
  if (!Number.isSafeInteger(v) || v < min) throw new RangeError(`${name} must be a safe integer >= ${min}`);
  return v;
}
const bytesEqual = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);

// §2.2 / §10.1. mkRaw is the raw MK re-opened as approve.js does (bridge-store.js openWorkspaceKey). It is zeroed here,
// whatever happens; K_ws comes back as a non-extractable HKDF key that can only derive the per-envelope keys.
export async function workspaceKey(mkRaw, workspaceHex) {
  try {
    len("master key", mkRaw, 32);
    if (!HEX32.test(workspaceHex)) throw new TypeError("workspace must be 32 lower-case hex");
    const mk = await subtle().importKey("raw", mkRaw, "HKDF", false, ["deriveBits"]);
    const raw = new Uint8Array(await subtle().deriveBits(
      { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(0), info: te.encode(L_WS + workspaceHex) }, mk, 256));
    try {
      return await subtle().importKey("raw", raw, "HKDF", false, ["deriveKey"]);
    } finally {
      raw.fill(0);
    }
  } finally {
    if (mkRaw instanceof Uint8Array) mkRaw.fill(0);
  }
}

// §2.3: K_msg from the header's salt, one envelope only.
export const messageKey = (kWs, salt, usage) => subtle().deriveKey(
  { name: "HKDF", hash: "SHA-256", salt: len("salt", salt, 16), info: L_MSG }, kWs, { name: "AES-GCM", length: 256 }, false, [usage]);
const gcm = (header) => ({ name: "AES-GCM", iv: new Uint8Array(12), additionalData: header, tagLength: 128 });

// §3.2 / §10.4 (the document's function, with its inputs checked).
export function encodeHeader({ direction, flags, keyVersion, workspace, deviceId, rid, stream, seq, tsMs, salt }) {
  for (const [n, v] of [["direction", direction], ["flags", flags], ["keyVersion", keyVersion]]) {
    if (!Number.isInteger(v) || v < 0 || v > 255) throw new RangeError(`${n} is one byte`);
  }
  for (const [n, v] of [["workspace", workspace], ["deviceId", deviceId], ["rid", rid], ["stream", stream], ["salt", salt]]) len(n, v, 16);
  safeInt("seq", seq, 0);
  safeInt("tsMs", tsMs, 0);
  const h = new Uint8Array(104), dv = new DataView(h.buffer);
  h.set(te.encode("SHRB"), 0); h[4] = 1; h[5] = direction; h[6] = flags; h[7] = keyVersion;
  h.set(workspace, 8); h.set(deviceId, 24); h.set(rid, 40); h.set(stream, 56);
  dv.setBigUint64(72, BigInt(seq), false); dv.setBigUint64(80, BigInt(tsMs), false); h.set(salt, 88);
  return h;
}

// seq and tsMs stay BigInt: a host may write any 64-bit value, and nothing here rounds it.
export function decodeHeader(h) {
  len("header", h, HEADER_LEN);
  const dv = new DataView(h.buffer, h.byteOffset, HEADER_LEN);
  return { magic: h[0] === 0x53 && h[1] === 0x48 && h[2] === 0x52 && h[3] === 0x42,   // "SHRB"
    version: h[4], direction: h[5], flags: h[6], keyVersion: h[7],
    workspace: h.subarray(8, 24), device: h.subarray(24, 40), rid: h.subarray(40, 56), stream: h.subarray(56, 72),
    seq: dv.getBigUint64(72, false), tsMs: dv.getBigUint64(80, false), salt: h.subarray(88, 104) };
}

export function splitEnvelope(env) {
  if (!(env instanceof Uint8Array) || env.length < OVERHEAD) throw new TypeError("not an envelope");
  return { header: env.subarray(0, HEADER_LEN), body: env.subarray(HEADER_LEN, env.length - SIG_LEN), sig: env.subarray(env.length - SIG_LEN) };
}

// §3.5
export function frame(meta, data = new Uint8Array(0)) {
  if (!meta || typeof meta !== "object" || Array.isArray(meta)) throw new TypeError("meta must be an object");
  if (!(data instanceof Uint8Array)) throw new TypeError("data must be bytes");
  const m = te.encode(canonicalJson(meta));
  if (m.length > MAX_META) throw new RangeError("meta is longer than 64 KiB");
  const out = new Uint8Array(4 + m.length + data.length);
  new DataView(out.buffer).setUint32(0, m.length, false);
  out.set(m, 4); out.set(data, 4 + m.length);
  return out;
}

export function unframe(pt) {
  if (pt.length < 4) throw new TypeError("short plaintext");
  const n = new DataView(pt.buffer, pt.byteOffset, 4).getUint32(0, false);
  if (n > MAX_META || 4 + n > pt.length) throw new TypeError("bad meta length");
  const meta = JSON.parse(td.decode(pt.subarray(4, 4 + n)));
  if (!meta || typeof meta !== "object" || Array.isArray(meta)) throw new TypeError("meta is not an object");
  return { meta, data: pt.subarray(4 + n) };
}

// §3.3: ciphertext || tag, the whole header as AAD.
export async function sealBody(kWs, header, plaintext) {
  return new Uint8Array(await subtle().encrypt(gcm(header), await messageKey(kWs, header.subarray(88, 104), "encrypt"), plaintext));
}
export async function openBody(kWs, header, body) {   // throws on a bad tag
  return new Uint8Array(await subtle().decrypt(gcm(header), await messageKey(kWs, header.subarray(88, 104), "decrypt"), body));
}

// §3.4: raw r || s; both scalars in 1 … n-1 before anything is verified.
export const signedBytes = (header, body) => cat(L_SIG, header, body);
export function scalarsInRange(sig) {
  if (!(sig instanceof Uint8Array) || sig.length !== SIG_LEN) return false;
  const r = BigInt("0x" + bytesToHex(sig.subarray(0, 32))), s = BigInt("0x" + bytesToHex(sig.subarray(32)));
  return r > 0n && r < P256_N && s > 0n && s < P256_N;
}
export async function verifySigned(pubKey, sig, msg) {
  if (!scalarsInRange(sig)) return false;
  try { return await subtle().verify(SIG, pubKey, sig, msg); } catch { return false; }
}
export async function importPublicKey(pub) {   // rejects anything but an uncompressed P-256 point on the curve
  if (len("public key", pub, 65)[0] !== 4) throw new TypeError("not an uncompressed P-256 point");
  return subtle().importKey("raw", pub, ECDSA, false, ["verify"]);
}

// §2.4 / §10.3: the private half can never be exported.
export async function newDeviceKey() {
  const kp = await subtle().generateKey(ECDSA, false, ["sign", "verify"]);
  if (kp.privateKey.extractable) throw new Error("refusing an extractable device key");
  return { privateKey: kp.privateKey, publicKey: kp.publicKey, pub: new Uint8Array(await subtle().exportKey("raw", kp.publicKey)) };
}
export const deviceId = async (workspace, pub) => (await sha256(cat(L_DEVICE, len("workspace", workspace, 16), len("pub", pub, 65)))).subarray(0, 16);
export async function deviceFingerprint(pub) {
  const raw = base32(await sha256(cat(L_FP, len("pub", pub, 65)))).slice(0, 20);
  return raw.match(/.{4}/g).join("-");
}
export const hostPin = (hostPub) => sha256(cat(L_HOST, len("host key", hostPub, 65)));

export class HostKeyError extends Error {
  constructor() { super(MESSAGES.hostKeyChanged); this.name = "HostKeyError"; }
}
// §8.1 step 4: the host key from the pending answer is used only if it matches the link's pin. Never on first use.
export async function hostKeyFromPin(hostPub, pin) {
  if (!bytesEqual(await hostPin(hostPub), len("pin", pin, 32))) throw new HostKeyError();
  return importPublicKey(hostPub);
}

// The texts the specification fixes (§5.1, §5.3, §7, §8.1).
export const MESSAGES = Object.freeze({
  usedElsewhere: "this link was used by someone else: reject it on your Mac",
  hostKeyChanged: "the host key changed: pair again",
  clockWrong: "this device's clock is wrong",
  outcomeUnknown: "outcome unknown",
});

// A request envelope (§3): fresh rid and fresh salt every call, so the same bytes are never re-sealed differently.
export async function sealRequest({ kWs, signKey, workspace, deviceId: dev, keyVersion, seq, tsMs, meta, data, flags = 0, stream = ZERO_ID }) {
  if (flags & ~F_STREAM) throw new RangeError("a request may set only STREAM");
  safeInt("seq", seq, 1);   // below 2^53 (§5.2)
  const rid = random(16);
  const header = encodeHeader({ direction: TO_HOST, flags, keyVersion, workspace, deviceId: dev, rid, stream, seq, tsMs, salt: random(16) });
  const body = await sealBody(kWs, header, frame(meta, data));
  const envelope = cat(header, body, new Uint8Array(await subtle().sign(SIG, signKey, signedBytes(header, body))));
  if (envelope.length > MAX_REQUEST) throw new RangeError("request is larger than 1 MiB");
  return { rid, envelope };
}

const drop = (why) => ({ result: "drop", why });

// §7, in its order. ctx: {workspace (hex), kWs, keyVersion, device (hex), hostKey, pending: Map(rid hex -> {next, stream,
// offsetAdopted?}), offsetMs}. mailbox: the cleartext {id, idx, last, stream}. Mutates the pending entry (next, offsetAdopted).
export async function openResponse(ctx, env, mailbox, nowMs) {
  if (!(env instanceof Uint8Array) || env.length < OVERHEAD || env.length > MAX_CHUNK) return drop("size");
  const { header, body, sig } = splitEnvelope(env);
  const h = decodeHeader(header);
  if (!h.magic || h.version !== 1 || h.direction !== TO_DEVICE || h.flags & ~(F_LAST | F_STREAM | F_REFUSAL)) return drop("version_direction_or_flags");
  if (h.flags & F_REFUSAL && !(h.flags & F_LAST)) return drop("flags");
  if (h.keyVersion !== ctx.keyVersion || bytesToHex(h.workspace) !== ctx.workspace || bytesToHex(h.device) !== ctx.device) return drop("not_for_this_device");
  const rid = bytesToHex(h.rid), pend = ctx.pending.get(rid);
  if (!pend) return drop("unknown_request");
  const last = !!(h.flags & F_LAST), stream = !!(h.flags & F_STREAM);
  if (mailbox?.id !== rid || !Number.isSafeInteger(mailbox.idx) || BigInt(mailbox.idx) !== h.seq || mailbox.last !== last
      || mailbox.stream !== stream || pend.stream !== stream) return drop("mailbox_mismatch");
  if (!await verifySigned(ctx.hostKey, sig, signedBytes(header, body))) {
    // Every field checked so far is cleartext, so anyone can get this far. Only a K_ws holder can make the tag verify:
    // only such a chunk is evidence that the host key changed (pinFailure). Dropped either way.
    let pinFailure = false;
    try { await openBody(ctx.kWs, header, body); pinFailure = true; } catch { /* forged without K_ws */ }
    return { ...drop("host_signature"), pinFailure };
  }
  let meta, data;
  try { ({ meta, data } = unframe(await openBody(ctx.kWs, header, body))); } catch { return drop("tag"); }
  if (h.seq !== BigInt(pend.next)) return drop("out_of_order");
  const res = { result: "accept", rid, last, refusal: !!(h.flags & F_REFUSAL), meta, data };
  if (res.refusal && meta.refusal === "stale_timestamp" && Number.isSafeInteger(meta.host_ms)) {
    // the host says this clock is off: never dropped for that clock; adopted once per pending rid, within 24 h (§5.1)
    if (!pend.offsetAdopted) {
      pend.offsetAdopted = true;
      const off = meta.host_ms - nowMs;
      if (Math.abs(off) <= MAX_OFFSET_MS) res.offsetMs = off;
      else res.clockWrong = true;
    }
  } else {
    const d = BigInt(nowMs + (ctx.offsetMs || 0)) - h.tsMs;
    if (d > BigInt(WINDOW_MS) || d < -BigInt(WINDOW_MS)) return drop("stale_timestamp");
  }
  pend.next += 1;
  return res;
}

// §8.1: the link fragment "#v1.<workspace hex>.<pairing_id hex>.<b64u S>.<b64u host_pin>", strictly.
const PAIR_FRAGMENT = /^#?v1\.([0-9a-f]{32})\.([0-9a-f]{32})\.([A-Za-z0-9_-]{43})\.([A-Za-z0-9_-]{43})$/;
function b64u32(text) {
  const bin = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "=");
  const out = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  if (out.length !== 32 || b64u(out) !== text) throw new TypeError("non-canonical");
  return out;
}
export function parsePairFragment(hash) {
  const m = typeof hash === "string" ? PAIR_FRAGMENT.exec(hash) : null;
  if (!m) return null;
  try {
    return { workspace: m[1], pairingId: hexToBytes(m[2]), secret: b64u32(m[3]), hostPin: b64u32(m[4]) };
  } catch {
    return null;
  }
}
export function parsePairLink(text, origin = globalThis.location?.origin) {
  let url;
  try { url = new URL(String(text ?? "").trim()); } catch { return null; }
  if (url.origin !== origin || url.pathname !== "/remote/pair" || url.search) return null;
  return parsePairFragment(url.hash);
}

// A pairing label or a `shown` text is used exactly as given (the host cleans; the device never re-cleans, §9.3).
export function acceptShown(s) {
  if (typeof s !== "string" || !s.isWellFormed()) throw new TypeError("not Unicode scalar values");
  return s;
}
const hmac = async (key, msg) => new Uint8Array(await subtle().sign("HMAC", key, msg));

// The `op = "pair"` meta (§8.1 step 2). S is single use: it is zeroed here. `phone`, if this browser holds the existing
// phone pairing for the workspace (pairing.js pairingFor), adds the link and its proof (§8.2).
export async function pairRequestMeta({ link, pub, label: name, phone }) {
  try {
    acceptShown(name);
    if ([...name].length > 80) throw new RangeError("a label is at most 80 characters");
    const ws = hexToBytes(link.workspace), id = await deviceId(ws, pub);
    const s = await subtle().importKey("raw", len("pairing secret", link.secret, 32), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const meta = { op: "pair", pairing_id: bytesToHex(link.pairingId), pub: bytesToHex(pub), label: name,
      mac: bytesToHex(await hmac(s, cat(L_PAIR, ws, len("pairing id", link.pairingId, 16), pub))) };
    if (phone) {
      meta.phone_id = phone.phoneId;
      meta.phone_proof = bytesToHex(await hmac(phone.key, cat(L_PHONE, id)));
    }
    return meta;
  } finally {
    link?.secret?.fill?.(0);
  }
}

// §8.1: "pairing_closed" for a link just opened, or no answer within 60 s, means someone else may hold the link.
export const PAIR_ANSWER_MS = 60_000;
export const pairingUsedElsewhere = (refusalCode, msSinceSent) => refusalCode === "pairing_closed" || (refusalCode == null && msSinceSent >= PAIR_ANSWER_MS);

// §9: challenges, recomputed on the device from the host's parts.
const PURPOSES = { fresh: 1, lease: 2 }, SCOPES = { look: 1, decide: 2, operate: 3, type: 4 };
const KINDS = new Set(["action", "command", "charter", "verdict", "permission", "lease"]);
export function subjectOf(s) {   // exactly the three fields, the text as received
  if (!s || !KINDS.has(s.kind) || !(s.digest === "" || /^[0-9a-f]{64}$/.test(s.digest))) throw new TypeError("not a subject");
  return { kind: s.kind, shown: acceptShown(s.shown), digest: s.digest };
}
export async function assertionChallenge({ workspace, deviceId: dev, rid, purpose, scope, expiresMs, nonce, subject }) {
  if (!PURPOSES[purpose] || !SCOPES[scope]) throw new TypeError("unknown purpose or scope");
  const sh = await sha256(te.encode(canonicalJson(subjectOf(subject))));
  return sha256(cat(L_ASSERT, len("workspace", workspace, 16), len("device", dev, 16), len("rid", rid, 16),
    new Uint8Array([PURPOSES[purpose], SCOPES[scope]]), u64(safeInt("expiresMs", expiresMs, 0)), len("nonce", nonce, 32), sh));
}
export const registrationChallenge = ({ workspace, deviceId: dev, expiresMs, nonce }) => sha256(cat(L_REG,
  len("workspace", workspace, 16), len("device", dev, 16), u64(safeInt("expiresMs", expiresMs, 0)), len("nonce", nonce, 32)));

// §10.5. The ceremonies run in the TIX app's own window, never in a frame.
function topWindow(win) {
  if (!win?.navigator?.credentials || win.top !== win.self) throw new Error("WebAuthn runs only in the TIX app's own window");
  return win;
}
export function registrationOptions({ challenge, deviceId: dev, label: name, rpId = globalThis.location?.hostname }) {
  return { publicKey: { challenge: len("challenge", challenge, 32), rp: { id: rpId, name: "TIX" },
    user: { id: len("device", dev, 16), name: acceptShown(name), displayName: name },
    pubKeyCredParams: [{ type: "public-key", alg: -7 }],
    authenticatorSelection: { authenticatorAttachment: "platform", userVerification: "required", residentKey: "discouraged" },
    attestation: "none", timeout: 60000 } };
}
export function assertionOptions({ challenge, credentialId, rpId = globalThis.location?.hostname }) {
  return { publicKey: { challenge: len("challenge", challenge, 32), rpId,
    allowCredentials: [{ type: "public-key", id: credentialId }], userVerification: "required", timeout: 60000 } };
}
const b = (buf) => b64u(new Uint8Array(buf));
export async function register(options, win = globalThis) {   // -> the credential_finish fields (§9.2)
  const c = await topWindow(win).navigator.credentials.create(options);
  return { credential_id: b(c.rawId), attestation_object: b(c.response.attestationObject), client_data_json: b(c.response.clientDataJSON) };
}
export async function assert(forRid, options, win = globalThis) {   // -> the op = "assert" fields (§9.4)
  const c = await topWindow(win).navigator.credentials.get(options);
  if (!bytesEqual(new Uint8Array(c.rawId), options.publicKey.allowCredentials[0].id)) throw new Error("another credential answered");
  return { for: bytesToHex(len("rid", forRid, 16)), credential_id: b(c.rawId), authenticator_data: b(c.response.authenticatorData),
    client_data_json: b(c.response.clientDataJSON), signature: b(c.response.signature) };
}
