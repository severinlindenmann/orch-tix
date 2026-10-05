// docs/bridge-protocol.md §10: the WebCrypto calls the document gives for the browser, run against
// tests/bridge_vectors.json (made by Python, tests/support/bridge_protocol_ref.py). Only crypto.subtle
// and the existing crypto.js helpers are used, so a mismatch is a mismatch in the specification.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { b64u, bytesToHex, canonicalJson, hexToBytes as hex } from "../../fileshare/static/js/crypto.js";

const VEC = JSON.parse(readFileSync(new URL("../bridge_vectors.json", import.meta.url), "utf8"));
const subtle = globalThis.crypto.subtle;
const te = new TextEncoder();
const HEADER_LEN = 104, SIG_LEN = 64;
const L = Object.fromEntries(Object.entries(VEC.constants.labels).map(([k, v]) => [k, te.encode(v)]));
const cat = (...parts) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let i = 0;
  for (const p of parts) { out.set(p, i); i += p.length; }
  return out;
};
const sha256 = async (b) => new Uint8Array(await subtle.digest("SHA-256", b));
const ECDSA = { name: "ECDSA", namedCurve: "P-256" };
const SIG = { name: "ECDSA", hash: "SHA-256" };

// §10.1: K_ws from the raw MK (re-opened with the KEK, as approve.js does), kept as a non-extractable HKDF key.
async function workspaceKey(mkRaw, workspaceHex) {
  const mk = await subtle.importKey("raw", mkRaw, "HKDF", false, ["deriveBits"]);
  const raw = new Uint8Array(await subtle.deriveBits(
    { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(0), info: cat(L.ws, te.encode(workspaceHex)) }, mk, 256));
  try {
    return { raw: bytesToHex(raw), key: await subtle.importKey("raw", raw, "HKDF", false, ["deriveKey", "deriveBits"]) };
  } finally {
    raw.fill(0);
  }
}

// §10.2: one AES-GCM key per envelope, from the header's salt (bytes 88..104).
const messageKey = (kWs, header, usage) => subtle.deriveKey(
  { name: "HKDF", hash: "SHA-256", salt: header.subarray(88, 104), info: L.msg }, kWs,
  { name: "AES-GCM", length: 256 }, false, [usage]);
const gcm = (header) => ({ name: "AES-GCM", iv: new Uint8Array(12), additionalData: header, tagLength: 128 });

// §10.4, verbatim: the header from its fields.
function encodeHeader({ direction, flags, keyVersion, workspace, deviceId, rid, stream, seq, tsMs, salt }) {
  const h = new Uint8Array(104), dv = new DataView(h.buffer);
  h.set(te.encode("SHRB"), 0); h[4] = 1; h[5] = direction; h[6] = flags; h[7] = keyVersion;
  h.set(workspace, 8); h.set(deviceId, 24); h.set(rid, 40); h.set(stream, 56);
  dv.setBigUint64(72, BigInt(seq), false); dv.setBigUint64(80, BigInt(tsMs), false); h.set(salt, 88);
  return h;
}

const importPub = (pub) => subtle.importKey("raw", pub, ECDSA, false, ["verify"]);

async function kWs() {
  return (await workspaceKey(hex(VEC.keys.mk), VEC.keys.workspace)).key;
}

test("HKDF: the workspace key and a message key", async () => {
  const ws = await workspaceKey(hex(VEC.keys.mk), VEC.keys.workspace);
  assert.equal(ws.raw, VEC.hkdf[0].okm);
  assert.equal(ws.key.extractable, false);
  const mkc = VEC.hkdf[2];
  const bits = await subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt: hex(mkc.salt), info: hex(mkc.info) }, ws.key, 256);
  assert.equal(bytesToHex(new Uint8Array(bits)), mkc.okm);
});

test("§10.4 builds the vector's header byte for byte", () => {
  const c = VEC.seal[0], f = c.header_fields;
  const h = encodeHeader({ direction: f.direction, flags: f.flags, keyVersion: f.key_version, workspace: hex(f.workspace),
    deviceId: hex(f.device), rid: hex(f.rid), stream: hex(f.stream), seq: f.seq, tsMs: f.ts_ms, salt: hex(f.salt) });
  assert.equal(bytesToHex(h), c.header);
});

test("seal and open: AES-256-GCM, zero nonce, header as AAD", async () => {
  const c = VEC.seal[0];
  const header = hex(c.header), key = await kWs();
  const ct = new Uint8Array(await subtle.encrypt(gcm(header), await messageKey(key, header, "encrypt"), hex(c.plaintext)));
  assert.equal(bytesToHex(ct), c.sealed);
  const pt = new Uint8Array(await subtle.decrypt(gcm(header), await messageKey(key, header, "decrypt"), ct));
  assert.equal(bytesToHex(pt), c.plaintext);
});

test("ECDSA P-256 verify takes the raw 64-byte r||s the vectors hold", async () => {
  for (const c of VEC.sign) {
    const ok = await subtle.verify(SIG, await importPub(hex(c.pub)), hex(c.sig), hex(c.msg));
    assert.equal(ok, c.valid, c.name);
  }
});

test("a non-extractable device key signs, and its public half gives the device id", async () => {
  const kp = await subtle.generateKey(ECDSA, false, ["sign", "verify"]);
  assert.equal(kp.privateKey.extractable, false);
  const pub = new Uint8Array(await subtle.exportKey("raw", kp.publicKey));
  assert.equal(pub.length, 65);
  const sig = new Uint8Array(await subtle.sign(SIG, kp.privateKey, te.encode("x")));
  assert.equal(sig.length, SIG_LEN);
  const a = VEC.ids.device_a;
  assert.equal(bytesToHex((await sha256(cat(L.device, hex(VEC.keys.workspace), hex(a.pub)))).subarray(0, 16)), a.device_id);
  assert.notEqual(a.device_id, a.device_id_other_workspace);
});

test("the device opens the full response chunk: host signature, then tag", async () => {
  const c = VEC.device_cases.find((x) => x.name === "full_response_chunk");
  const env = hex(c.envelope), header = env.subarray(0, HEADER_LEN);
  const body = env.subarray(HEADER_LEN, env.length - SIG_LEN), sig = env.subarray(env.length - SIG_LEN);
  const host = await importPub(hex(VEC.keys.host.pub));
  assert.ok(await subtle.verify(SIG, host, sig, cat(L.sig, header, body)));
  const pt = new Uint8Array(await subtle.decrypt(gcm(header), await messageKey(await kWs(), header, "decrypt"), body));
  const metaLen = new DataView(pt.buffer).getUint32(0, false);
  const meta = JSON.parse(new TextDecoder().decode(pt.subarray(4, 4 + metaLen)));
  assert.deepEqual(meta, c.expect.meta);
  assert.equal(bytesToHex(pt.subarray(4 + metaLen)), c.expect.data);
  const dv = new DataView(header.buffer, header.byteOffset, HEADER_LEN);
  assert.equal(Number(dv.getBigUint64(72, false)), c.mailbox.idx);
});

test("a response signed by a device key or with a flipped tag bit fails", async () => {
  const host = await importPub(hex(VEC.keys.host.pub));
  for (const name of ["response_signed_by_a_device", "response_tampered_tag"]) {
    const env = hex(VEC.device_cases.find((x) => x.name === name).envelope);
    const header = env.subarray(0, HEADER_LEN), body = env.subarray(HEADER_LEN, env.length - SIG_LEN);
    assert.equal(await subtle.verify(SIG, host, env.subarray(env.length - SIG_LEN), cat(L.sig, header, body)), false, name);
  }
});

test("the assertion challenge, recomputed from what the device shows", async () => {
  const i = VEC.assertion.challenge_inputs;
  assert.equal(canonicalJson(i.subject), i.subject_json);
  const subjectHash = await sha256(te.encode(canonicalJson(i.subject)));
  assert.equal(bytesToHex(subjectHash), i.subject_hash);
  const exp = new Uint8Array(8);
  new DataView(exp.buffer).setBigUint64(0, BigInt(i.expires_ms), false);
  const ch = await sha256(cat(L.assert, hex(i.workspace), hex(i.device), hex(i.rid), new Uint8Array([1, 4]), exp,
    hex(i.nonce), subjectHash));
  assert.equal(bytesToHex(ch), VEC.assertion.challenge);
});

test("the pairing link carries the host pin, and the MAC is HMAC-SHA256", async () => {
  const p = VEC.pairing;
  const [, ws, pid, secret, pin] = p.link_fragment.split(".");
  assert.equal(pin, b64u(await sha256(cat(L.host, hex(p.host_pub)))));
  const k = await subtle.importKey("raw", hex(p.secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  assert.equal(b64u(hex(p.secret)), secret);
  const mac = new Uint8Array(await subtle.sign("HMAC", k, cat(L.pair, hex(ws), hex(pid), hex(p.device_pub))));
  assert.equal(bytesToHex(mac), p.mac);
  // the phone-link proof is signed with the phone's existing sign-only HMAC pairing key (pairing.js)
  const pk = await subtle.importKey("raw", hex(p.phone_key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const proof = new Uint8Array(await subtle.sign("HMAC", pk, cat(L.phone_link, hex(p.device_id))));
  assert.equal(bytesToHex(proof), p.phone_proof);
});

test("signature scalars outside 1..n-1 never verify in WebCrypto", async () => {
  const c = VEC.sign[0], pub = await importPub(hex(c.pub));
  for (const s of VEC.sig_scalars.filter((x) => !x.in_range && x.name !== "short")) {
    let ok = false;
    try { ok = await subtle.verify(SIG, pub, hex(s.sig), hex(c.msg)); } catch { ok = false; }
    assert.equal(ok, false, s.name);
  }
});
