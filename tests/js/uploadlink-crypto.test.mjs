import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as c from "../../fileshare/static/js/crypto.js";

const VEC = JSON.parse(readFileSync(new URL("../vectors/ulink1.json", import.meta.url), "utf8"));
const hex = c.hexToBytes;
const KEY = Uint8Array.from({ length: 32 }, (_, i) => i);

test("constants and AAD prefixes", () => {
  assert.equal(c.ULINK_PRIV_LEN, 97);
  assert.equal(c.SEALED_DEK_LEN, 126);
  const uuid = new Uint8Array(16);
  const fileUuid = new Uint8Array(16).fill(1);
  assert.equal(new TextDecoder().decode(c.aadUlink(uuid)), "sharing/ulink/v1|" + "\0".repeat(16));
  assert.equal(new TextDecoder().decode(c.aadUlabel(uuid)), "sharing/ulabel/v1|" + "\0".repeat(16));
  assert.equal(c.bytesToHex(c.aadUldek(uuid, fileUuid)).startsWith(c.bytesToHex(new TextEncoder().encode("sharing/uldek/v1|"))), true);
});

for (const v of VEC.basic) {
  test(`vector ${v.name}: newUploadLink reproduces pub and wrappedLpriv byte-for-byte`, async () => {
    const linkPrivJwk = {
      d: v.link_priv_jwk_d,
      x: c.b64u(hex(v.pub).slice(1, 33)),
      y: c.b64u(hex(v.pub).slice(33, 65)),
    };
    const r = await c.newUploadLink(hex(v.mk), v.label, {
      linkPrivJwk, uuid: hex(v.link_uuid), nonce: hex(v.wrapped_nonce),
    });
    assert.equal(r.uuidHex, v.link_uuid);
    assert.equal(c.bytesToHex(r.pub), v.pub);
    assert.equal(r.wrappedLpriv, v.wrapped_lpriv);
  });

  test(`vector ${v.name}: JS opens the Python-made wrappedLpriv and enc_label`, async () => {
    const { priv, pub } = await c.openUploadLinkKey(hex(v.mk), v.link_uuid, v.wrapped_lpriv);
    assert.equal(c.bytesToHex(pub), v.pub);
    assert.equal(await c.openUploadLabel(hex(v.mk), v.link_uuid, v.enc_label), v.label);
  });

  test(`vector ${v.name}: sealDekToLink reproduces sealed_dek byte-for-byte, and opens with openSealedDek`, async () => {
    const linkKey = await c.openUploadLinkKey(hex(v.mk), v.link_uuid, v.wrapped_lpriv);
    const sealed = await c.sealDekToLink(hex(v.dek), hex(v.pub), v.link_uuid, v.file_uuid, {
      ephPrivPkcs8: c.unb64u(v.eph_priv_pkcs8), nonce: hex(v.sealed_nonce),
    });
    assert.equal(sealed, v.sealed_dek);
    const raw = c.unb64u(sealed);
    assert.equal(raw.length, 126);
    assert.equal(raw[0], 4);
    assert.deepEqual(await c.openSealedDek(linkKey, v.link_uuid, v.file_uuid, v.sealed_dek), hex(v.dek));
  });

  test(`vector ${v.name}: adoptWrappedDek matches Python's wrap_dek_under_mk`, async () => {
    const wrapped = await c.adoptWrappedDek(hex(v.mk), hex(v.dek), v.file_uuid);
    // wrapped_dek in the vector was sealed with a fixed nonce; adoptWrappedDek uses a fresh one, so
    // compare via round trip through openFileMeta's unwrap path instead of raw bytes.
    assert.notEqual(wrapped, undefined);
    const dek = await c.open(hex(v.mk), c.unb64u(wrapped), c.aadDek(hex(v.file_uuid)));
    assert.deepEqual(dek, hex(v.dek));
    // The vector's own wrapped_dek (fixed nonce) must also open to the same DEK.
    const dek2 = await c.open(hex(v.mk), c.unb64u(v.wrapped_dek), c.aadDek(hex(v.file_uuid)));
    assert.deepEqual(dek2, hex(v.dek));
  });
}

test("round trip with fresh random keys", async () => {
  const link = await c.newUploadLink(KEY, "team standup notes");
  const { priv, pub } = await c.openUploadLinkKey(KEY, link.uuidHex, link.wrappedLpriv);
  assert.deepEqual(pub, link.pub);
  assert.equal(await c.openUploadLabel(KEY, link.uuidHex, link.encLabel), "team standup notes");

  const fileUuidHex = "11".repeat(16);
  const dek = crypto.getRandomValues(new Uint8Array(32));
  const sealed = await c.sealDekToLink(dek, link.pub, link.uuidHex, fileUuidHex);
  const raw = c.unb64u(sealed);
  assert.equal(raw.length, 126);
  assert.equal(raw[0], 4);
  assert.deepEqual(await c.openSealedDek({ priv, pub }, link.uuidHex, fileUuidHex, sealed), dek);
});

test("empty label means no label at all", async () => {
  const link = await c.newUploadLink(KEY, "");
  assert.equal(link.encLabel, null);
  assert.equal(await c.openUploadLabel(KEY, link.uuidHex, link.encLabel), "");
});

test("openSealedDek rejects a different file uuid or link uuid", async () => {
  const link = await c.newUploadLink(KEY, "x");
  const linkKey = await c.openUploadLinkKey(KEY, link.uuidHex, link.wrappedLpriv);
  const fileUuidHex = "22".repeat(16);
  const dek = crypto.getRandomValues(new Uint8Array(32));
  const sealed = await c.sealDekToLink(dek, link.pub, link.uuidHex, fileUuidHex);

  await assert.rejects(c.openSealedDek(linkKey, link.uuidHex, "33".repeat(16), sealed), c.IntegrityError);
  const otherLink = await c.newUploadLink(KEY, "x");
  await assert.rejects(c.openSealedDek(linkKey, otherLink.uuidHex, fileUuidHex, sealed), c.IntegrityError);

  const tampered = c.unb64u(sealed).slice();
  tampered[tampered.length - 1] ^= 0xff;
  await assert.rejects(c.openSealedDek(linkKey, link.uuidHex, fileUuidHex, c.b64u(tampered)), c.IntegrityError);
});

test("openUploadLinkKey rejects a wrong mk or tampered envelope", async () => {
  const link = await c.newUploadLink(KEY, "x");
  await assert.rejects(c.openUploadLinkKey(new Uint8Array(32), link.uuidHex, link.wrappedLpriv), c.IntegrityError);

  const tampered = c.unb64u(link.wrappedLpriv).slice();
  tampered[tampered.length - 1] ^= 0xff;
  await assert.rejects(c.openUploadLinkKey(KEY, link.uuidHex, c.b64u(tampered)), c.IntegrityError);
});

test("openUploadLinkKey rejects a d that doesn't match the stored pub, hand-built with seal", async () => {
  // Mirrors Python's test_open_upload_link_key_rejects_mismatched_pub: someone who holds mk (the
  // only thing that authenticates wrappedLpriv) seals a d from one keypair next to the pub of an
  // unrelated one. AEAD authenticates the joint bytes fine; only an explicit d-vs-pub check (here,
  // WebCrypto's own JWK-import consistency validation) catches the mismatch.
  const ECDH = { name: "ECDH", namedCurve: "P-256" };
  const kp1 = await crypto.subtle.generateKey(ECDH, true, ["deriveBits"]);
  const kp2 = await crypto.subtle.generateKey(ECDH, true, ["deriveBits"]);
  const d1 = c.unb64u((await crypto.subtle.exportKey("jwk", kp1.privateKey)).d);
  const pub2 = new Uint8Array(await crypto.subtle.exportKey("raw", kp2.publicKey));
  const linkUuid = crypto.getRandomValues(new Uint8Array(16));
  const uuidHex = c.bytesToHex(linkUuid);
  const forgedPlain = new Uint8Array(d1.length + pub2.length);
  forgedPlain.set(d1, 0);
  forgedPlain.set(pub2, d1.length);
  const forged = c.b64u(await c.seal(KEY, forgedPlain, c.aadUlink(linkUuid)));
  await assert.rejects(c.openUploadLinkKey(KEY, uuidHex, forged), c.IntegrityError);
});

test("newDropFileCrypto produces a usable file DEK sealed to the link, and metadata under it", async () => {
  const link = await c.newUploadLink(KEY, "drop me files");
  const linkKey = await c.openUploadLinkKey(KEY, link.uuidHex, link.wrappedLpriv);
  const drop = await c.newDropFileCrypto(link.pub, link.uuidHex, { name: "a.txt", mime: "text/plain", note: "" });
  assert.equal(drop.uuidHex, c.bytesToHex(drop.uuid));

  const dek = await c.openSealedDek(linkKey, link.uuidHex, drop.uuidHex, drop.sealedDek);
  const meta = await c.openMetaObject(dek, drop.uuid, drop.encMeta);
  assert.deepEqual(meta, { name: "a.txt", mime: "text/plain", note: "" });

  // the DEK CryptoKey newDropFileCrypto hands back for encryptBlob is the same key material
  const blob = await c.encryptBlob(drop.dek, drop.uuid, 1, new TextEncoder().encode("hello"));
  assert.deepEqual(await c.decryptBlob(dek, drop.uuid, blob), new TextEncoder().encode("hello"));

  const wrappedDek = await c.adoptWrappedDek(KEY, dek, drop.uuidHex);
  const { meta: adoptedMeta, dek: adoptedDek } = await c.openFileMeta(KEY, {
    uuid: drop.uuidHex, wrapped_dek: wrappedDek, enc_meta: drop.encMeta,
  });
  assert.deepEqual(adoptedDek, dek);
  assert.equal(adoptedMeta.name, "a.txt");
});
