import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as c from "../../fileshare/static/js/crypto.js";

const VEC = JSON.parse(readFileSync(new URL("../vectors/shr1.json", import.meta.url), "utf8"));
const hex = c.hexToBytes;
const ptOf = (n) => { const b = new Uint8Array(n); for (let i = 0; i < n; i++) b[i] = i % 251; return b; };
const sha256Hex = async (b) => c.bytesToHex(new Uint8Array(await crypto.subtle.digest("SHA-256", b)));
const KEY = Uint8Array.from({ length: 32 }, (_, i) => i);
const UUID = Uint8Array.from({ length: 16 }, (_, i) => i);
const OTHER = Uint8Array.from({ length: 16 }, (_, i) => i + 1);

test("constants", () => {
  assert.equal(c.HEADER_LEN, 34);
  assert.equal(c.CHUNK, 1048576);
  assert.equal(new TextDecoder().decode(c.AAD_MK), "sharing/mk/v1");
  assert.equal(new TextDecoder().decode(c.aadDek(new Uint8Array(0))), "sharing/dek/v1|");
  assert.equal(new TextDecoder().decode(c.aadMeta(new Uint8Array(0))), "sharing/meta/v1|");
  assert.equal(new TextDecoder().decode(c.aadApprove("dev_x")), "sharing/approve/v1|dev_x");
  assert.equal(c.aadOnboard, undefined);   // v2 onboarding secret is gone
});

test("b64u roundtrip and strictness", () => {
  for (let n = 0; n < 40; n++) {
    const b = crypto.getRandomValues(new Uint8Array(n));
    const s = c.b64u(b);
    assert.doesNotMatch(s, /[=+/]/);
    assert.deepEqual(c.unb64u(s), b);
  }
  for (const bad of ["a", "ab=", "a+b/", "abc$", "AB"]) assert.throws(() => c.unb64u(bad));
});

for (const v of VEC.envelope) {
  test(`vector envelope ${v.name}`, async () => {
    const env = await c.seal(hex(v.key), hex(v.pt), hex(v.aad), hex(v.nonce));
    assert.equal(c.bytesToHex(env), v.env);
    assert.equal(c.bytesToHex(await c.open(hex(v.key), env, hex(v.aad))), v.pt);
    const k = await c.importAesKey(hex(v.key));
    assert.equal(c.bytesToHex(await c.open(k, env, hex(v.aad))), v.pt);
  });
}

for (const v of VEC.kdf) {
  test(`vector kdf ${v.name}`, async () => {
    const { authKey, kek } = await c.deriveRaw(v.passphrase, hex(v.salt), v.iterations);
    assert.equal(c.bytesToHex(authKey), v.auth_key);
    assert.equal(c.bytesToHex(kek), v.kek);
  });
}

for (const v of VEC.blob) {
  test(`vector blob ${v.name}`, async () => {
    const blob = await c.encryptBlob(hex(v.dek), hex(v.uuid), v.key_version, ptOf(v.pt_len),
      { chunkSize: v.chunk_size, noncePrefix: hex(v.nonce_prefix) });
    assert.equal(blob.length, v.blob_len);
    assert.equal(await sha256Hex(blob), v.blob_sha256);
    if (v.blob) assert.equal(c.bytesToHex(blob), v.blob);
    assert.deepEqual(await c.decryptBlob(hex(v.dek), hex(v.uuid), blob), ptOf(v.pt_len));
  });
}

for (const v of VEC.file_meta) {
  test(`vector file_meta ${v.name}`, async () => {
    const stream = hex(v.rng); let pos = 0;
    const rng = (n) => { const out = stream.slice(pos, pos + n); pos += n; return out; };
    const fc = await c.newFileCrypto(hex(v.mk), v.meta, { rng });
    assert.equal(fc.uuidHex, v.uuid);
    assert.equal(fc.wrappedDek, v.wrapped_dek);
    assert.equal(fc.encMeta, v.enc_meta);
    const { meta, dek } = await c.openFileMeta(hex(v.mk), { uuid: v.uuid, wrapped_dek: v.wrapped_dek, enc_meta: v.enc_meta });
    assert.deepEqual(meta, { ...v.meta, transcript: null });
    assert.equal(c.bytesToHex(dek), v.dek);
  });
}

// ------------------------------------------------------------ device approval (§4.6)

const ECDH = { name: "ECDH", namedCurve: "P-256" };

// The device side of §4.6, written out in the test so the browser module stays send-only.
async function openBundle(devPrivKey, devicePub, deviceId, bundle) {
  const ephPub = bundle.subarray(0, 65);
  const ephKey = await crypto.subtle.importKey("raw", ephPub, ECDH, false, []);
  const z = new Uint8Array(await crypto.subtle.deriveBits({ name: "ECDH", public: ephKey }, devPrivKey, 256));
  const wk = await c.approvalKey(z, ephPub, devicePub, deviceId);
  return c.open(wk, bundle.subarray(65), c.aadApprove(deviceId));
}

async function newDevice() {
  const kp = await crypto.subtle.generateKey(ECDH, true, ["deriveBits"]);
  return { priv: kp.privateKey, pub: new Uint8Array(await crypto.subtle.exportKey("raw", kp.publicKey)) };
}

for (const v of VEC.approve) {
  test(`vector approve ${v.name}: fingerprint, wk and bundle are byte-exact with Python`, async () => {
    assert.equal(await c.fingerprint(hex(v.device_pub)), v.fingerprint);
    assert.equal(c.bytesToHex(await c.approvalKey(hex(v.z), hex(v.eph_pub), hex(v.device_pub), v.device_id)), v.wk);
    const bundle = await c.sealToDevice(hex(v.mk), hex(v.device_pub), v.device_id,
      { ephPrivPkcs8: c.unb64u(v.eph_priv_pkcs8), nonce: hex(v.nonce) });
    // Identical bytes to the vector, which tests/client/test_vectors.py opens with Python
    // open_device_bundle -> the Python device can open what the browser produces.
    assert.equal(c.bytesToHex(bundle), v.bundle);
    const devPriv = await crypto.subtle.importKey("pkcs8", c.unb64u(v.device_priv_pkcs8), ECDH, false, ["deriveBits"]);
    assert.equal(c.bytesToHex(await openBundle(devPriv, hex(v.device_pub), v.device_id, bundle)), v.mk);
  });
}

test("approval roundtrip with random keys; bundle is 126 bytes", async () => {
  const d = await newDevice();
  const bundle = await c.sealToDevice(KEY, d.pub, "dev_0123456789ab");
  assert.equal(bundle.length, 126);
  assert.deepEqual(await openBundle(d.priv, d.pub, "dev_0123456789ab", bundle), KEY);
});

test("approval bundle is bound to the device id and the device key", async () => {
  const d = await newDevice();
  const other = await newDevice();
  const bundle = await c.sealToDevice(KEY, d.pub, "dev_aaaaaaaaaaaa");
  await assert.rejects(openBundle(d.priv, d.pub, "dev_bbbbbbbbbbbb", bundle), c.IntegrityError);
  await assert.rejects(openBundle(other.priv, other.pub, "dev_aaaaaaaaaaaa", bundle), c.IntegrityError);
});

test("tampered ephemeral key fails", async () => {
  const d = await newDevice();
  const bundle = await c.sealToDevice(KEY, d.pub, "dev_x");
  const swapped = bundle.slice(); swapped.set((await newDevice()).pub, 0);   // valid point, wrong key
  await assert.rejects(openBundle(d.priv, d.pub, "dev_x", swapped), c.IntegrityError);
});

test("invalid device points are rejected", async () => {
  for (const bad of [new Uint8Array(0), new Uint8Array(65), Uint8Array.from([4, ...new Array(64).fill(1)]),
                     Uint8Array.from([2, ...new Array(32).fill(1)])]) {
    await assert.rejects(c.sealToDevice(KEY, bad, "dev_x"));
  }
  await assert.rejects(c.sealToDevice(new Uint8Array(31), (await newDevice()).pub, "dev_x"), /32 bytes/);
});

test("fingerprint format", async () => {
  const fp = await c.fingerprint((await newDevice()).pub);
  assert.match(fp, /^[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}$/);
});

// ------------------------------------------------------------ tamper tests

const enc = (n, opts = {}) => c.encryptBlob(KEY, UUID, 1, ptOf(n), { chunkSize: 16, ...opts });

test("open rejects wrong key, wrong aad, bad version, short", async () => {
  const env = await c.seal(KEY, new Uint8Array([1, 2, 3]), new Uint8Array([9]));
  await assert.rejects(c.open(new Uint8Array(32), env, new Uint8Array([9])), c.IntegrityError);
  await assert.rejects(c.open(KEY, env, new Uint8Array([8])), c.IntegrityError);
  const bad = env.slice(); bad[0] = 2;
  await assert.rejects(c.open(KEY, bad, new Uint8Array([9])), c.IntegrityError);
  await assert.rejects(c.open(KEY, new Uint8Array(20), new Uint8Array(0)), c.IntegrityError);
});

test("blob roundtrip across sizes", async () => {
  for (const n of [0, 1, 15, 16, 17, 32, 40]) {
    assert.deepEqual(await c.decryptBlob(KEY, UUID, await enc(n)), ptOf(n));
  }
});

test("truncation at chunk boundary fails", async () => {
  const blob = await enc(40);
  await assert.rejects(c.decryptBlob(KEY, UUID, blob.slice(0, 34 + 64)), c.IntegrityError);
});

test("truncation mid-chunk and short tail fail", async () => {
  const blob = await enc(40);
  await assert.rejects(c.decryptBlob(KEY, UUID, blob.slice(0, blob.length - 5)), c.IntegrityError);
  const two = await enc(32);
  const tail = new Uint8Array(two.length + 7); tail.set(two);
  await assert.rejects(c.decryptBlob(KEY, UUID, tail), c.IntegrityError);
});

test("reordered chunks fail", async () => {
  const blob = await enc(40);
  const sw = blob.slice();
  sw.set(blob.slice(34 + 32, 34 + 64), 34);
  sw.set(blob.slice(34, 34 + 32), 34 + 32);
  await assert.rejects(c.decryptBlob(KEY, UUID, sw), c.IntegrityError);
});

test("extra trailing chunk fails", async () => {
  const blob = await enc(32);
  const ext = new Uint8Array(blob.length + 32);
  ext.set(blob); ext.set(blob.slice(34, 66), blob.length);
  await assert.rejects(c.decryptBlob(KEY, UUID, ext), c.IntegrityError);
});

test("uuid mismatch, relabel, key_version tamper, bad magic, wrong dek, zero chunk size", async () => {
  const blob = await enc(10);
  await assert.rejects(c.decryptBlob(KEY, OTHER, blob), c.IntegrityError);
  const relabel = blob.slice(); relabel.set(OTHER, 6);
  await assert.rejects(c.decryptBlob(KEY, OTHER, relabel), c.IntegrityError);
  const kv = blob.slice(); kv[5] = 2;
  await assert.rejects(c.decryptBlob(KEY, UUID, kv), c.IntegrityError);
  const magic = blob.slice(); magic[0] = 0x58;
  await assert.rejects(c.decryptBlob(KEY, UUID, magic), c.IntegrityError);
  await assert.rejects(c.decryptBlob(new Uint8Array(32), UUID, blob), c.IntegrityError);
  const zero = blob.slice(); zero.set([0, 0, 0, 0], 22);
  await assert.rejects(c.decryptBlob(KEY, UUID, zero), c.IntegrityError);
  await assert.rejects(c.decryptBlob(KEY, UUID, new Uint8Array(0)), c.IntegrityError);
});

test("file meta swap between files fails; deleted file throws", async () => {
  const mk = crypto.getRandomValues(new Uint8Array(32));
  const a = await c.newFileCrypto(mk, { name: "a", mime: "text/plain", note: "" });
  const b = await c.newFileCrypto(mk, { name: "b", mime: "text/plain", note: "" });
  await assert.rejects(c.openFileMeta(mk, { uuid: a.uuidHex, wrapped_dek: b.wrappedDek, enc_meta: b.encMeta }), c.IntegrityError);
  await assert.rejects(c.openFileMeta(mk, { uuid: a.uuidHex, wrapped_dek: null, enc_meta: null }), /deleted/);
});

test("non-extractable key works for seal/open", async () => {
  const k = await c.importAesKey(KEY, false);
  assert.equal(k.extractable, false);
  const env = await c.seal(k, new Uint8Array([7]), c.AAD_MK);
  assert.deepEqual(await c.open(KEY, env, c.AAD_MK), new Uint8Array([7]));
});

// ------------------------------------------------------------ transcripts in enc_meta (§14 G)

const T_OK = { text: "hello\nworld", language: "en", model: "nova-3", created_at: "2026-09-24T12:00:00Z", by: "laptop" };

// Seals an arbitrary metadata object the way sharing.py's seal_file_meta does (sorted keys, compact).
async function fileWithMeta(mk, metaObj) {
  const fc = await c.newFileCrypto(mk, { name: "memo.mp3", mime: "audio/mpeg", note: "" });
  const sorted = (o) => (o && typeof o === "object" && !Array.isArray(o)
    ? Object.fromEntries(Object.keys(o).sort().map((k) => [k, sorted(o[k])])) : o);
  const env = await c.seal(fc.dek, new TextEncoder().encode(JSON.stringify(sorted(metaObj))), c.aadMeta(fc.uuid));
  return { uuid: fc.uuidHex, wrapped_dek: fc.wrappedDek, enc_meta: c.b64u(env) };
}

test("validTranscript accepts only the documented shape", () => {
  assert.deepEqual(c.validTranscript(T_OK), T_OK);
  assert.deepEqual(c.validTranscript({ ...T_OK, language: "" }), { ...T_OK, language: "" });
  // Unknown extra keys are dropped from what the UI sees.
  assert.deepEqual(c.validTranscript({ ...T_OK, extra: { evil: 1 } }), T_OK);
  assert.equal(c.validTranscript({ ...T_OK, text: "x".repeat(1_000_000) }).text.length, 1_000_000);
  for (const bad of [
    undefined, null, "text", 42, [], [T_OK],
    { ...T_OK, text: 1 }, { ...T_OK, text: null }, { ...T_OK, text: "x".repeat(1_000_001) },
    { ...T_OK, language: null }, { ...T_OK, model: 3 }, { ...T_OK, created_at: {} }, { ...T_OK, by: ["x"] },
    (({ by, ...rest }) => rest)(T_OK),
  ]) {
    assert.equal(c.validTranscript(bad), null, JSON.stringify(bad)?.slice(0, 80));
  }
});

test("openFileMeta returns a valid transcript, and null for a missing or malformed one", async () => {
  const mk = crypto.getRandomValues(new Uint8Array(32));
  const base = { name: "memo.mp3", mime: "audio/mpeg", note: "n" };
  let r = await c.openFileMeta(mk, await fileWithMeta(mk, { ...base, transcript: T_OK }));
  assert.deepEqual(r.meta, { ...base, transcript: T_OK });
  r = await c.openFileMeta(mk, await fileWithMeta(mk, base));
  assert.equal(r.meta.transcript, null);
  // A malformed transcript is ignored; the file itself still opens.
  r = await c.openFileMeta(mk, await fileWithMeta(mk, { ...base, transcript: { text: 5 } }));
  assert.deepEqual(r.meta, { ...base, transcript: null });
  r = await c.openFileMeta(mk, await fileWithMeta(mk, { ...base, transcript: "<img src=x onerror=alert(1)>" }));
  assert.equal(r.meta.transcript, null);
  // Hostile text is returned as data, untouched (the DOM layer uses textContent).
  const evil = { ...T_OK, text: "<img src=x onerror=window.__pwned=1>" };
  r = await c.openFileMeta(mk, await fileWithMeta(mk, { ...base, transcript: evil }));
  assert.equal(r.meta.transcript.text, evil.text);
});

// ------------------------------------------------------------ encrypted settings (§15)

test("AAD_SETTINGS constant", () => {
  assert.equal(new TextDecoder().decode(c.AAD_SETTINGS), "sharing/settings/v1");
});

for (const v of VEC.settings) {
  test(`vector settings ${v.name}: byte-exact with Python`, async () => {
    const settings = JSON.parse(v.input_json);             // keys in the input's (unsorted) order
    assert.notDeepEqual(Object.keys(settings), Object.keys(settings).sort(), "the vector must exercise sorting");
    assert.equal(c.canonicalJson(settings), v.json);
    assert.equal(await c.sealSettings(hex(v.mk), settings, { nonce: hex(v.nonce) }), v.enc_settings);
    assert.deepEqual(await c.openSettings(hex(v.mk), v.enc_settings), settings);
  });
}

for (const v of VEC.meta_reseal) {
  test(`vector meta_reseal ${v.name}: byte-exact with Python seal_file_meta`, async () => {
    const meta = JSON.parse(v.input_json);
    assert.notDeepEqual(Object.keys(meta), Object.keys(meta).sort(), "the vector must exercise sorting");
    assert.equal(await c.sealFileMeta(hex(v.dek), hex(v.uuid), meta, { nonce: hex(v.nonce) }), v.enc_meta);
    assert.deepEqual(await c.openMetaObject(hex(v.dek), hex(v.uuid), v.enc_meta), meta);
  });
}

test("openFileMetaFull keeps unknown keys; a re-seal uses a fresh nonce and still opens", async () => {
  const mk = Uint8Array.from({ length: 32 }, (_, i) => i + 7);
  const fc = await c.newFileCrypto(mk, { name: "a.webm", mime: "audio/webm", note: "" });
  const rec = { uuid: fc.uuidHex, wrapped_dek: fc.wrappedDek, enc_meta: fc.encMeta };
  const first = await c.openFileMetaFull(mk, rec);
  assert.deepEqual(first.raw, { mime: "audio/webm", name: "a.webm", note: "" });
  assert.equal(c.bytesToHex(first.uuid), fc.uuidHex);
  const withFuture = { ...first.raw, x_future: { keep: [1, 2] }, transcript: T_OK };
  const a = await c.sealFileMeta(first.dek, first.uuid, withFuture);
  const b = await c.sealFileMeta(first.dek, first.uuid, withFuture);
  assert.notEqual(a, b, "fresh nonce each time");
  const again = await c.openFileMetaFull(mk, { ...rec, enc_meta: a });
  assert.deepEqual(again.raw, withFuture);
  assert.deepEqual(again.meta.transcript, T_OK);
  // the meta is bound to its file: another uuid's AAD refuses it
  await assert.rejects(c.openMetaObject(first.dek, OTHER, a), c.IntegrityError);
  await assert.rejects(c.sealFileMeta(first.dek, first.uuid, ["not", "an", "object"]), TypeError);
});

test("canonicalJson sorts keys recursively and stays compact", () => {
  assert.equal(c.canonicalJson({ b: 1, a: { d: [3, { z: 1, y: 2 }], c: "ü" } }),
    '{"a":{"c":"ü","d":[3,{"y":2,"z":1}]},"b":1}');
  // code-point order, like Python's sort_keys (UTF-16 order would put U+FB01 after U+1F600)
  assert.equal(c.canonicalJson({ "\u{1F600}": 1, "ﬁ": 2 }), '{"ﬁ":2,"\u{1F600}":1}');
});

test("settings roundtrip with a non-extractable key; fresh nonce each time", async () => {
  const mk = await c.importAesKey(crypto.getRandomValues(new Uint8Array(32)), false);
  const obj = { deepgram_api_key: "dg-test", other: { keep: true } };
  const a = await c.sealSettings(mk, obj);
  assert.notEqual(a, await c.sealSettings(mk, obj));
  assert.deepEqual(await c.openSettings(mk, a), obj);
});

test("openSettings rejects the wrong key, the wrong AAD and non-objects", async () => {
  const enc = await c.sealSettings(KEY, { deepgram_api_key: "x" });
  await assert.rejects(c.openSettings(new Uint8Array(32), enc), c.IntegrityError);
  // a DEK envelope under the same MK must not open as settings
  const wrapped = c.b64u(await c.seal(KEY, new TextEncoder().encode('{"deepgram_api_key":"x"}'), c.aadDek(UUID)));
  await assert.rejects(c.openSettings(KEY, wrapped), c.IntegrityError);
  for (const pt of ["[]", '"x"', "1", "null", "not json"]) {
    const env = c.b64u(await c.seal(KEY, new TextEncoder().encode(pt), c.AAD_SETTINGS));
    await assert.rejects(c.openSettings(KEY, env), c.IntegrityError, pt);
  }
  for (const bad of ["", "!!!", "AQ", null, 5]) {
    await assert.rejects(c.openSettings(KEY, bad), c.IntegrityError, String(bad));
  }
  await assert.rejects(c.sealSettings(KEY, []), TypeError);
  await assert.rejects(c.sealSettings(KEY, null), TypeError);
});

// ------------------------------------------------------------ tickets (spec T3, T5)

for (const v of VEC.tickets) {
  test(`vector tickets ${v.name}: byte-exact with Python`, async () => {
    const stream = hex(v.rng); let pos = 0;
    const rng = (n) => { const out = stream.slice(pos, pos + n); pos += n; return out; };
    const tc = await c.newTicketCrypto(hex(v.mk), v.content, { rng });
    assert.equal(tc.uuid, v.uuid);
    assert.equal(tc.key_version, v.key_version);
    assert.equal(tc.wrapped_dek, v.wrapped_dek);
    assert.equal(tc.enc_content, v.enc_content);
    assert.equal(c.bytesToHex(tc.dek), v.dek);

    const { content, dek } = await c.openTicket(hex(v.mk),
      { uuid: v.uuid, wrapped_dek: v.wrapped_dek, enc_content: v.enc_content });
    assert.deepEqual(content, v.content);
    assert.equal(c.bytesToHex(dek), v.dek);

    const encBody = await c.sealTicketEvent(dek, v.uuid, v.event_uuid, v.body, { nonce: hex(v.event_nonce) });
    assert.equal(encBody, v.enc_body);
    assert.deepEqual(await c.openTicketEvent(dek, v.uuid, v.event_uuid, encBody), v.body);
  });
}

test("openTicket rejects a wrong key, a wrong ticket and a deleted ticket", async () => {
  const mk = crypto.getRandomValues(new Uint8Array(32));
  const other = crypto.getRandomValues(new Uint8Array(32));
  const t = await c.newTicketCrypto(mk, { title: "A", body: "", fm: {} });
  await assert.rejects(c.openTicket(other, t), c.IntegrityError);
  await assert.rejects(c.openTicket(mk, { ...t, wrapped_dek: null, enc_content: null }), /deleted/);
  const t2 = await c.newTicketCrypto(mk, { title: "B", body: "", fm: {} });
  await assert.rejects(
    c.openTicket(mk, { uuid: t.uuid, wrapped_dek: t.wrapped_dek, enc_content: t2.enc_content }),
    c.IntegrityError);
});

test("event bodies are bound to both the ticket and the event uuid", async () => {
  const dek = crypto.getRandomValues(new Uint8Array(32));
  const ticketA = crypto.getRandomValues(new Uint8Array(16));
  const ticketB = crypto.getRandomValues(new Uint8Array(16));
  const eventA = crypto.getRandomValues(new Uint8Array(16));
  const eventB = crypto.getRandomValues(new Uint8Array(16));
  const uuidHexA = c.bytesToHex(ticketA), uuidHexB = c.bytesToHex(ticketB);
  const evHexA = c.bytesToHex(eventA), evHexB = c.bytesToHex(eventB);
  const enc = await c.sealTicketEvent(dek, uuidHexA, evHexA, { text: "hi" });
  assert.deepEqual(await c.openTicketEvent(dek, uuidHexA, evHexA, enc), { text: "hi" });
  await assert.rejects(c.openTicketEvent(dek, uuidHexB, evHexA, enc), c.IntegrityError);
  await assert.rejects(c.openTicketEvent(dek, uuidHexA, evHexB, enc), c.IntegrityError);
});

// ------------------------------------------------------------ public links (§17)

const LK = Uint8Array.from({ length: 32 }, (_, i) => i + 100);

test("aadLink constant", () => {
  assert.equal(c.bytesToHex(c.aadLink(UUID)), c.bytesToHex(new TextEncoder().encode("sharing/link/v1|")) + c.bytesToHex(UUID));
});

for (const v of VEC.link) {
  test(`vector link ${v.name}: byte-exact with Python`, async () => {
    const lk = hex(v.lk), dek = hex(v.dek), uuid = hex(v.uuid);
    assert.equal(c.b64u(lk), v.lk_b64u);
    assert.deepEqual(c.parseLinkKey(v.lk_b64u), lk);
    assert.equal(c.bytesToHex(c.aadLink(uuid)), v.aad);
    assert.equal(await c.wrapDekForLink(dek, uuid, lk, { nonce: hex(v.nonce) }), v.wrapped_dek_link);
    assert.deepEqual(await c.openLinkDek(lk, uuid, v.wrapped_dek_link), dek);
    const r = await c.openPublicFile(lk, { uuid: v.uuid, wrapped_dek_link: v.wrapped_dek_link, enc_meta: v.enc_meta });
    assert.deepEqual(r.dek, dek);
    assert.deepEqual({ name: r.meta.name, mime: r.meta.mime, note: r.meta.note }, v.meta);
    assert.equal(r.meta.transcript, null);
  });
}

test("link wrap roundtrip, fresh nonce, 61-byte envelope, fresh random link key", async () => {
  const dek = crypto.getRandomValues(new Uint8Array(32));
  const w = await c.wrapDekForLink(dek, UUID, LK);
  assert.notEqual(w, await c.wrapDekForLink(dek, UUID, LK));
  assert.equal(c.unb64u(w).length, 61);
  assert.deepEqual(await c.openLinkDek(LK, UUID, w), dek);
  const a = c.newLinkKey(), b = c.newLinkKey();
  assert.equal(a.length, 32);
  assert.notDeepEqual(a, b);
});

test("openLinkDek rejects the wrong key, the wrong uuid and malformed input", async () => {
  const w = await c.wrapDekForLink(KEY, UUID, LK);
  await assert.rejects(c.openLinkDek(new Uint8Array(32), UUID, w), c.IntegrityError);
  await assert.rejects(c.openLinkDek(LK, OTHER, w), c.IntegrityError);
  for (const bad of ["", "!!!", "AQ", null, 5, "A".repeat(10)]) {
    await assert.rejects(c.openLinkDek(LK, UUID, bad), c.IntegrityError, String(bad));
  }
  await assert.rejects(c.openLinkDek(new Uint8Array(5), UUID, w), c.IntegrityError);
  const short = c.b64u(await c.seal(LK, new Uint8Array(31), c.aadLink(UUID)));
  await assert.rejects(c.openLinkDek(LK, UUID, short), c.IntegrityError);
  await assert.rejects(c.wrapDekForLink(KEY, UUID, new Uint8Array(5)), TypeError);
});

test("parseLinkKey accepts exactly 32 bytes of canonical base64url", () => {
  assert.equal(c.parseLinkKey(c.b64u(LK)).length, 32);
  for (const bad of ["", "abc", c.b64u(new Uint8Array(31)), c.b64u(new Uint8Array(33)), c.b64u(LK) + "=", null, "#" + c.b64u(LK)]) {
    assert.throws(() => c.parseLinkKey(bad), c.IntegrityError, String(bad));
  }
});

test("DEK, settings and link envelopes do not swap (one key in every role)", async () => {
  const dek = Uint8Array.from({ length: 32 }, (_, i) => i + 50);
  const wrappedDek = c.b64u(await c.seal(KEY, dek, c.aadDek(UUID)));
  const link = await c.wrapDekForLink(dek, UUID, KEY);
  const settings = await c.sealSettings(KEY, { deepgram_api_key: "x".repeat(20) });
  for (const env of [wrappedDek, settings]) await assert.rejects(c.openLinkDek(KEY, UUID, env), c.IntegrityError);
  await assert.rejects(c.open(KEY, c.unb64u(link), c.aadDek(UUID)), c.IntegrityError);
  await assert.rejects(c.openSettings(KEY, link), c.IntegrityError);
  const fc = await c.newFileCrypto(KEY, { name: "a", mime: "text/plain", note: "" });
  const linkForFc = await c.wrapDekForLink(fc.dek, fc.uuid, KEY);
  await assert.rejects(c.openFileMeta(KEY, { uuid: fc.uuidHex, wrapped_dek: linkForFc, enc_meta: fc.encMeta }), c.IntegrityError);
  await assert.rejects(c.openPublicFile(KEY, { uuid: fc.uuidHex, wrapped_dek_link: fc.wrappedDek, enc_meta: fc.encMeta }), c.IntegrityError);
});

test("openPublicFile returns meta with a transcript and rejects malformed records", async () => {
  const mk = crypto.getRandomValues(new Uint8Array(32));
  const f = await fileWithMeta(mk, { name: "memo.mp3", mime: "audio/mpeg", note: "n", transcript: T_OK });
  const { dek } = await c.openFileMeta(mk, f);
  const pub = { uuid: f.uuid, wrapped_dek_link: await c.wrapDekForLink(dek, hex(f.uuid), LK), enc_meta: f.enc_meta };
  const r = await c.openPublicFile(LK, pub);
  assert.equal(r.meta.name, "memo.mp3");
  assert.deepEqual(r.meta.transcript, T_OK);
  for (const bad of [{ ...pub, uuid: "zz" }, { ...pub, uuid: "00".repeat(15) }, { ...pub, enc_meta: null }, { ...pub, wrapped_dek_link: null }, {}]) {
    await assert.rejects(c.openPublicFile(LK, bad), c.IntegrityError);
  }
});
