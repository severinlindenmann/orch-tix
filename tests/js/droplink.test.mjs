// tests/js/droplink.test.mjs — upload links, owner web UI (spec §18, Task 4): the pure URL/state
// helpers and adoptPending's crypto against a fake api().
import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../../fileshare/static/js/api.js";
import * as c from "../../fileshare/static/js/crypto.js";
import { adoptPending, DROP_TTLS, dropUrl, isPendingUndecryptable, stateText } from "../../fileshare/static/js/droplink.js";

const KEY = crypto.getRandomValues(new Uint8Array(32));

test("DROP_TTLS holds exactly the three link ttls, default 1d", () => {
  assert.deepEqual(DROP_TTLS.map(([v]) => v), ["1h", "1d", "7d"]);
});

test("dropUrl is origin + /u/ + token + # + b64u(pub), and round trips", () => {
  const pub = crypto.getRandomValues(new Uint8Array(65));
  pub[0] = 4;
  const token = "A".repeat(20) + "b_-" + "9".repeat(20); // 43 chars of base64url
  const url = dropUrl("https://tix.severin.io", token, pub);
  assert.equal(url, `https://tix.severin.io/u/${token}#${c.b64u(pub)}`);
  const u = new URL(url);
  assert.equal(u.pathname, `/u/${token}`);
  assert.deepEqual(c.unb64u(u.hash.slice(1)), pub);
});

test("stateText covers every server state", () => {
  assert.equal(stateText({ state: "waiting" }), "Waiting");
  assert.equal(stateText({ state: "pending" }), "Received, finishing…");
  assert.equal(stateText({ state: "received", file: "FILE7" }), "Received as FILE7");
  assert.equal(stateText({ state: "expired" }), "Expired");
  assert.equal(stateText({ state: "revoked" }), "Revoked");
});

// A real link + a real dropped file, exactly as newUploadLink/newDropFileCrypto produce them, so
// adoptPending exercises the actual crypto path (openUploadLinkKey -> openSealedDek -> adoptWrappedDek).
async function makePendingLink({ tamperSealedDek = false } = {}) {
  const link = await c.newUploadLink(KEY, "team standup");
  const drop = await c.newDropFileCrypto(link.pub, link.uuidHex, { name: "a.txt", mime: "text/plain", note: "" });
  let sealedDek = drop.sealedDek;
  if (tamperSealedDek) {
    const raw = c.unb64u(sealedDek).slice();
    raw[raw.length - 1] ^= 0xff;
    sealedDek = c.b64u(raw);
  }
  return {
    id: "upl_000000000001",
    uuid: link.uuidHex,
    wrapped_lpriv: link.wrappedLpriv,
    enc_label: link.encLabel,
    state: "pending",
    pending: { file_uuid: drop.uuidHex, size: 5, sealed_dek: sealedDek, enc_meta: drop.encMeta },
    file: null,
  };
}

test("adoptPending posts a wrapped_dek that opens back to the same dek under mk", async () => {
  const link = await makePendingLink();
  const calls = [];
  const api = async (method, path, opts) => {
    calls.push([method, path, opts]);
    return { id: "FILE1" };
  };
  const { adopted, failed, undecryptable } = await adoptPending(api, KEY, [link]);
  assert.equal(adopted, 1);
  assert.deepEqual(failed, []);
  assert.deepEqual(undecryptable, []);
  assert.equal(calls.length, 1);
  const [method, path, opts] = calls[0];
  assert.equal(method, "POST");
  assert.equal(path, `/api/upload-links/${link.id}/adopt`);
  const wrappedDek = opts.json.wrapped_dek;
  // What the server would have received: seal(mk, dek, aad_dek(file_uuid)). Opening it back proves
  // it holds the same dek the drop-page sealed to the link (not the raw or a garbage value).
  const dek = await c.open(KEY, c.unb64u(wrappedDek), c.aadDek(c.hexToBytes(link.pending.file_uuid)));
  const linkKey = await c.openUploadLinkKey(KEY, link.uuid, link.wrapped_lpriv);
  const expected = await c.openSealedDek(linkKey, link.uuid, link.pending.file_uuid, link.pending.sealed_dek);
  assert.deepEqual(dek, expected);
});

test("a 409 already_adopted or not_pending counts as done, not failed", async () => {
  for (const code of ["already_adopted", "not_pending"]) {
    const link = await makePendingLink();
    const api = async () => { throw new ApiError(409, code, "already done"); };
    const { adopted, failed, undecryptable } = await adoptPending(api, KEY, [link]);
    assert.equal(adopted, 1);
    assert.deepEqual(failed, []);
    assert.deepEqual(undecryptable, []);
  }
});

// A tampered sealed_dek can never be decrypted, on this device or any other: it lands in
// `undecryptable` (final review), not the transient `failed`, and adoptPending never throws.
test("a tampered sealed_dek lands in undecryptable and adoptPending never throws", async () => {
  const link = await makePendingLink({ tamperSealedDek: true });
  const api = async () => { throw new Error("should never be called: crypto fails before any POST"); };
  const { adopted, failed, undecryptable } = await adoptPending(api, KEY, [link]);
  assert.equal(adopted, 0);
  assert.deepEqual(failed, []);
  assert.deepEqual(undecryptable, [link.id]);
});

test("a wrong mk also lands in undecryptable without throwing, and does not touch other links", async () => {
  const good = await makePendingLink();
  const bad = await makePendingLink();
  bad.wrapped_lpriv = (await c.newUploadLink(crypto.getRandomValues(new Uint8Array(32)), "x")).wrappedLpriv;
  const calls = [];
  const api = async (method, path, opts) => { calls.push(path); return { id: "FILE1" }; };
  const { adopted, failed, undecryptable } = await adoptPending(api, KEY, [bad, good]);
  assert.equal(adopted, 1);
  assert.deepEqual(failed, []);
  assert.deepEqual(undecryptable, [bad.id]);
  assert.deepEqual(calls, [`/api/upload-links/${good.id}/adopt`]);
});

test("non-pending links are skipped: no crypto, no POST", async () => {
  const link = await makePendingLink();
  for (const state of ["waiting", "received", "expired", "revoked"]) {
    const other = { ...link, id: `not-${state}`, state };
    const api = async () => { throw new Error("must not be called"); };
    const { adopted, failed, undecryptable } = await adoptPending(api, KEY, [other]);
    assert.equal(adopted, 0);
    assert.deepEqual(failed, []);
    assert.deepEqual(undecryptable, []);
  }
});

// A network error or a 5xx from the adopt POST itself (crypto already succeeded) is transient: it
// lands in `failed`, not `undecryptable`, so the owner sees no banner and it just retries.
test("an unrelated ApiError from the adopt POST lands in failed, not undecryptable, not thrown", async () => {
  const link = await makePendingLink();
  const api = async () => { throw new ApiError(500, "server_error", "boom"); };
  const { adopted, failed, undecryptable } = await adoptPending(api, KEY, [link]);
  assert.equal(adopted, 0);
  assert.deepEqual(failed, [link.id]);
  assert.deepEqual(undecryptable, []);
});

// ---- isPendingUndecryptable: the dialog uses this to show "Can't decrypt — discard" without a
// network round-trip (the whole check is pure crypto against what GET /api/upload-links returned).

test("isPendingUndecryptable is false for a link with no pending upload", async () => {
  const link = await makePendingLink();
  assert.equal(await isPendingUndecryptable(KEY, { ...link, pending: null }), false);
});

test("isPendingUndecryptable is false for a good pending upload", async () => {
  const link = await makePendingLink();
  assert.equal(await isPendingUndecryptable(KEY, link), false);
});

test("isPendingUndecryptable is true for a tampered sealed_dek or the wrong mk", async () => {
  const tampered = await makePendingLink({ tamperSealedDek: true });
  assert.equal(await isPendingUndecryptable(KEY, tampered), true);

  const wrongMk = await makePendingLink();
  wrongMk.wrapped_lpriv = (await c.newUploadLink(crypto.getRandomValues(new Uint8Array(32)), "x")).wrappedLpriv;
  assert.equal(await isPendingUndecryptable(KEY, wrongMk), true);
});
