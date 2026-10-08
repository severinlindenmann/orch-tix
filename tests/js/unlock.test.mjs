// The unlock sheet's logic (R11, docs/bridge-protocol.md section 9): the challenge the device builds, the sheet's states, one
// sheet at a time, a refused request retried exactly once and only after the person confirmed. The DOM parts (text only,
// delayed trusted click) are in tests/browser/test_unlock.py. tests/js/unlock-mutations.sh points UNLOCK_JS_DIR at a
// mutated copy of fileshare/static/js and every test here must still be able to fail.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const JS = process.env.UNLOCK_JS_DIR ? pathToFileURL(process.env.UNLOCK_JS_DIR + "/") : new URL("../../fileshare/static/js/", import.meta.url);
const C = await import(new URL("crypto.js", JS));
const U = await import(new URL("unlock.js", JS));
const T = await import(new URL("remote-transport.js", JS));

const VEC = JSON.parse(readFileSync(new URL("../bridge_vectors.json", import.meta.url), "utf8"));
const I = VEC.assertion.challenge_inputs;
const { hexToBytes: hex, bytesToHex, b64u, unb64u } = C;
const CRED = Uint8Array.from({ length: 16 }, (_, i) => i + 1);
const te = new TextEncoder();

function session(extra = {}) {
  return { workspace: I.workspace, deviceIdBytes: hex(I.device), offsetMs: 0, credentialId: b64u(CRED), ...extra };
}
const refusal = (extra = {}, code = "assertion_required") => ({ code, rid: I.rid, meta: { purpose: code === "lease_required" ? "lease" : "fresh", scope: I.scope,
  expires_ms: I.expires_ms, nonce: I.nonce, subject: { ...I.subject }, ...extra } });
const NOW = I.expires_ms - 60_000;

// A window whose authenticator signs nothing real but answers like one: clientDataJSON carries the challenge it was given.
function win({ fail = null, credential = CRED, echo = null, type = "webauthn.get" } = {}) {
  const w = { gets: [] };
  w.top = w.self = w;
  w.navigator = { credentials: { async get(o) {
    w.gets.push(o);
    if (fail) throw Object.assign(new Error("x"), { name: fail });
    const challenge = echo ?? o.publicKey.challenge;
    return { rawId: credential.buffer.slice(0), response: { authenticatorData: new Uint8Array([1, 2, 3]),
      clientDataJSON: te.encode(JSON.stringify({ type, challenge: b64u(new Uint8Array(challenge)), origin: "https://x" })), signature: new Uint8Array([4, 5]) } };
  } } };
  return w;
}

// A sheet that only records: tests click by calling the spec's own handlers.
function sheets() {
  const log = { drawn: [], closed: 0 };
  log.draw = (spec) => { log.drawn.push(spec); return { close() { log.closed++; } }; };
  return log;
}
const ask = (s, r, w, log, extra = {}) => U.askAssertion(s, r, { win: w, now: () => NOW, draw: log.draw, delayMs: 0, ...extra });
const tick = () => new Promise((r) => setTimeout(r, 5));
const drawn = async (log, n = 1) => { for (let i = 0; i < 200 && log.drawn.length < n; i++) await tick(); };

test("the challenge is rebuilt from the host's parts, equals the vector, and is asked for with user verification required", async () => {
  const w = win(), log = sheets();
  const p = ask(session(), refusal(), w, log);
  await drawn(log);
  assert.equal(log.drawn.length, 1);
  assert.equal(w.gets.length, 0);                                   // nothing is asked of the authenticator before the click
  log.drawn[0].onConfirm();
  const r = await p;
  const o = w.gets[0].publicKey;
  assert.equal(bytesToHex(new Uint8Array(o.challenge)), VEC.assertion.challenge);
  assert.equal(o.userVerification, "required");
  assert.deepEqual(o.allowCredentials.map((c) => [...new Uint8Array(c.id)]), [[...CRED]]);
  assert.equal(r.ok, true);
  assert.equal(r.meta.op, "assert");
  assert.equal(r.meta.for, I.rid);
  assert.equal(r.meta.credential_id, b64u(CRED));
  assert.deepEqual(Object.keys(r.meta).sort(), ["authenticator_data", "client_data_json", "credential_id", "for", "op", "signature"]);
  assert.equal(log.closed, 1);
});

test("a lease challenge is purpose 2", async () => {
  const w = win(), log = sheets();
  const lease = { kind: "lease", shown: "Type for 15 minutes", digest: "" };
  const p = ask(session(), refusal({ subject: lease }, "lease_required"), w, log);
  await drawn(log);
  log.drawn[0].onConfirm();
  assert.equal((await p).ok, true);
  assert.equal(bytesToHex(new Uint8Array(w.gets[0].publicKey.challenge)), VEC.assertion.lease_challenge);
  assert.match(log.drawn[0].title, /15 minutes/);
});

test("the sheet shows exactly the host's text, the scope, the expiry and the digest to compare", async () => {
  const log = sheets();
  const p = ask(session(), refusal(), win(), log);
  await drawn(log);
  const s = log.drawn[0];
  assert.equal(s.text, I.subject.shown);
  assert.ok(s.facts.some((f) => f.includes("type")) && s.facts.some((f) => f.includes("Expires")));
  assert.ok(s.facts.some((f) => f.includes(I.subject.digest.slice(0, 8))));
  assert.ok(s.facts.some((f) => /never your passphrase/i.test(f)));
  s.onCancel();
  await p;
});

test("a hostile subject is passed on as the very same string (the DOM draws it as text), and is what the challenge covers", async () => {
  const hostile = '<img src=x onerror="alert(1)">';
  const w = win(), log = sheets();
  const p = ask(session(), refusal({ subject: { kind: "action", shown: hostile, digest: "" } }), w, log);
  await drawn(log);
  assert.equal(log.drawn[0].text, hostile);
  log.drawn[0].onConfirm();
  await p;
  const B = await import(new URL("bridge-crypto.js", JS));
  const want = await B.assertionChallenge({ workspace: hex(I.workspace), deviceId: hex(I.device), rid: hex(I.rid), purpose: "fresh", scope: I.scope,
    expiresMs: I.expires_ms, nonce: hex(I.nonce), subject: { kind: "action", shown: hostile, digest: "" } });
  assert.deepEqual(new Uint8Array(w.gets[0].publicKey.challenge), want);
});

test("cancel resolves cancelled, closes the sheet and never touches the authenticator", async () => {
  const w = win(), log = sheets();
  const p = ask(session(), refusal(), w, log);
  await drawn(log);
  log.drawn[0].onCancel();
  assert.deepEqual(await p, { ok: false, reason: "cancelled" });
  assert.equal(w.gets.length, 0);
  assert.equal(log.closed, 1);
});

test("an authenticator that refuses (no user verification, dismissed) is cancelled; anything else failed; the sheet closes", async () => {
  for (const [name, reason] of [["NotAllowedError", "cancelled"], ["AbortError", "cancelled"], ["SecurityError", "failed"]]) {
    const log = sheets();
    const p = ask(session(), refusal(), win({ fail: name }), log);
    await drawn(log);
    log.drawn[0].onConfirm();
    assert.deepEqual(await p, { ok: false, reason });
    assert.equal(log.closed, 1);
  }
});

test("only one sheet at a time: a second ask while one is open is busy, and a later one works again", async () => {
  const log = sheets();
  const first = ask(session(), refusal(), win(), log);
  await drawn(log);
  assert.deepEqual(await ask(session(), refusal(), win(), log), { ok: false, reason: "busy" });
  assert.equal(log.drawn.length, 1);
  log.drawn[0].onCancel();
  await first;
  const third = ask(session(), refusal(), win(), log);
  await drawn(log, 2);
  assert.equal(log.drawn.length, 2);
  log.drawn[1].onCancel();
  await third;
});

test("the sheet expires with the challenge: nothing is asked after it, and an already expired one is not even drawn", async () => {
  const log = sheets(), w = win();
  const r = await U.askAssertion(session(), refusal(), { win: w, now: () => I.expires_ms - 20, draw: log.draw, delayMs: 0 });
  assert.deepEqual(r, { ok: false, reason: "expired" });
  assert.equal(log.drawn.length, 1);
  assert.equal(log.closed, 1);
  const late = await U.askAssertion(session(), refusal(), { win: w, now: () => I.expires_ms + 1, draw: log.draw, delayMs: 0 });
  assert.deepEqual(late, { ok: false, reason: "expired" });
  assert.equal(log.drawn.length, 1);
  assert.equal(w.gets.length, 0);
});

test("the host's clock offset is applied to the expiry", async () => {
  const log = sheets();
  const r = await U.askAssertion(session({ offsetMs: 500 }), refusal(), { win: win(), now: () => I.expires_ms - 400, draw: log.draw, delayMs: 0 });
  assert.deepEqual(r, { ok: false, reason: "expired" });       // 500 ms ahead: already over on the device's clock
  assert.equal(log.drawn.length, 0);
});

test("a refusal this app cannot read is not asked about; no credential means no sheet", async () => {
  const bad = [
    refusal({ purpose: "lease" }),                                     // code and purpose disagree
    refusal({ scope: "root" }), refusal({ nonce: "00" }), refusal({ expires_ms: "soon" }),
    refusal({ subject: { kind: "nope", shown: "x", digest: "" } }), refusal({ subject: { kind: "action", shown: "x", digest: "XYZ" } }),
    { ...refusal(), rid: "zz" }, { ...refusal(), code: "revoked" },
  ];
  for (const r of bad) {
    const log = sheets();
    assert.deepEqual(await ask(session(), r, win(), log), { ok: false, reason: "bad_request" });
    assert.equal(log.drawn.length, 0);
  }
  const log = sheets();
  assert.deepEqual(await ask(session({ credentialId: null }), refusal(), win(), log), { ok: false, reason: "no_credential" });
  assert.equal(log.drawn.length, 0);
});

test("an answer for another challenge, or from another credential, or without WebAuthn, is a failure", async () => {
  for (const w of [win({ echo: new Uint8Array(32) }), win({ type: "webauthn.create" }), win({ credential: Uint8Array.from({ length: 16 }, () => 9) })]) {
    const log = sheets();
    const p = ask(session(), refusal(), w, log);
    await drawn(log);
    log.drawn[0].onConfirm();
    assert.deepEqual(await p, { ok: false, reason: "failed" });
  }
  assert.deepEqual(await ask(session(), refusal(), { navigator: {} }, sheets()), { ok: false, reason: "failed" });
});

test("every failure has a fixed sentence and none of them is the passphrase", () => {
  for (const r of ["cancelled", "expired", "failed", "busy", "no_credential", "bad_request", "no_platform", "refused"]) {
    assert.ok(U.unlockText(r).length > 10);
    assert.doesNotMatch(U.unlockText(r), /enter your passphrase/i);
  }
  assert.equal(U.unlockText("whatever"), U.UNLOCK_TEXT.failed);
  assert.match(U.unlockText("no_platform"), /cannot get Type/);
});

// ---- registration at pairing ---------------------------------------------------------------------------------------

test("registration: begin, then the person's click, then the credential, then finish; no platform authenticator means no begin", async () => {
  const sent = [], order = [];
  const w = { top: null, PublicKeyCredential: { isUserVerifyingPlatformAuthenticatorAvailable: async () => true } };
  w.top = w.self = w;
  w.navigator = { credentials: { async create(o) {
    order.push("create");
    assert.equal(o.publicKey.authenticatorSelection.userVerification, "required");
    assert.equal(o.publicKey.authenticatorSelection.authenticatorAttachment, "platform");
    assert.equal(o.publicKey.attestation, "none");
    assert.deepEqual(o.publicKey.pubKeyCredParams, [{ type: "public-key", alg: -7 }]);
    assert.equal(bytesToHex(new Uint8Array(o.publicKey.challenge)), VEC.assertion.registration_challenge.challenge);
    return { rawId: CRED.buffer.slice(0), response: { attestationObject: new Uint8Array([7]).buffer, clientDataJSON: new Uint8Array([8]).buffer } };
  } } };
  const rc = VEC.assertion.registration_challenge;
  const send = async (m) => { sent.push(m.op); return m.op === "credential_begin" ? { nonce: rc.nonce, expires_ms: rc.expires_ms } : { registered: true, synced: true }; };
  const r = await U.registerCredential({ session: session(), label: "Phone", send, gate: async () => { order.push("click"); }, win: w });
  assert.deepEqual(r, { ok: true, credentialId: b64u(CRED), synced: true });
  assert.deepEqual(sent, ["credential_begin", "credential_finish"]);
  assert.deepEqual(order, ["click", "create"]);
  const none = await U.registerCredential({ session: session(), label: "P", send: async () => assert.fail("asked the host"), win: { PublicKeyCredential: { isUserVerifyingPlatformAuthenticatorAvailable: async () => false } } });
  assert.deepEqual(none, { ok: false, reason: "no_platform" });
  const refused = await U.registerCredential({ session: session(), label: "P", send: async () => null, win: w });
  assert.deepEqual(refused, { ok: false, reason: "refused" });
});

// ---- the transport: ask once, send the proof once ---------------------------------------------------------------------

function rig(script) {
  let n = 0;
  const posted = [];
  const sess = {
    pending: new Map(), requests: [],
    async request(args) { const id = (++n).toString(16).padStart(32, "0"); this.requests.push(structuredClone({ meta: args.meta, flags: args.flags })); return { id, envelope: new Uint8Array([n]) }; },
    retry: () => new Uint8Array([9]), expire() {},
    async receive() { return script.shift()(); },
  };
  const mailbox = {
    async post(id, env, stream, listener) { posted.push(id); queueMicrotask(() => listener.chunk({ env: new Uint8Array(1), mailbox: { id, idx: 0, last: true, stream: false } })); },
    async cancel() {}, close() {},
  };
  return { session: sess, mailbox, posted };
}
const need = (code = "assertion_required") => () => ({ result: "accept", rid: "x", last: true, refusal: true, meta: { refusal: code, purpose: "fresh", scope: "type", expires_ms: 1, nonce: "0".repeat(64), subject: { kind: "action", shown: "s", digest: "" } }, data: new Uint8Array(0) });
const ok = () => ({ result: "accept", rid: "x", last: true, refusal: false, meta: { status: 200, headers: {}, page: true }, data: new TextEncoder().encode("done") });
const REQ = { method: "POST", path: "/type", headers: {}, body: te.encode("ls"), stream: false, signal: new AbortController().signal };
const collect = async (it) => { const out = []; for await (const e of it) out.push(e); return out; };

test("a refusal for an assertion asks once; the confirmed proof is sent once as the assert request, and its answer is the result", async () => {
  const r = rig([need(), ok]);
  const asked = [];
  const unlock = async (s, rf) => { asked.push(rf); return { ok: true, meta: { op: "assert", for: rf.rid } }; };
  const ev = await collect(T.bridgeTransport({ ...r, unlock }).request(REQ));
  assert.deepEqual(ev.map((e) => e.type), ["head", "chunk", "end"]);
  assert.equal(asked.length, 1);
  assert.equal(asked[0].code, "assertion_required");
  assert.equal(asked[0].rid, r.posted[0]);                           // the proof is for the request that was refused
  assert.equal(r.session.requests.length, 2);
  assert.equal(r.session.requests[1].meta.op, "assert");
  assert.notEqual(r.posted[0], r.posted[1]);                         // a new request id
});

test("a second refusal for an assertion is not asked about again: the fixed text, no third request", async () => {
  const r = rig([need(), need("lease_required")]);
  let asked = 0;
  const seen = [];
  await assert.rejects(collect(T.bridgeTransport({ ...r, unlock: async () => { asked++; return { ok: true, meta: { op: "assert" } }; }, onRefusal: (c, t) => seen.push(c) }).request(REQ)),
    (e) => e.code === "lease_required");
  assert.equal(asked, 1);
  assert.equal(r.session.requests.length, 2);
  assert.deepEqual(seen, ["lease_required"]);
});

test("a cancelled or failed sheet sends nothing more and says its fixed sentence", async () => {
  for (const reason of ["cancelled", "failed", "expired", "busy"]) {
    const r = rig([need()]);
    const seen = [];
    await assert.rejects(collect(T.bridgeTransport({ ...r, unlock: async () => ({ ok: false, reason }), onRefusal: (c, t) => seen.push(t) }).request(REQ)),
      (e) => e.message === U.unlockText(reason));
    assert.equal(r.session.requests.length, 1);
    assert.deepEqual(seen, [U.unlockText(reason)]);
  }
});

test("without an unlock the refusal is only said, and nothing is sent again", async () => {
  const r = rig([need()]);
  await assert.rejects(collect(T.bridgeTransport(r).request(REQ)), (e) => e.code === "assertion_required");
  assert.equal(r.session.requests.length, 1);
});

test("the proof for a request is never sent unless the sheet resolved ok (no silent retry)", async () => {
  const r = rig([need(), ok]);
  let release;
  const gate = new Promise((res) => { release = res; });
  const p = collect(T.bridgeTransport({ ...r, unlock: async () => { await gate; return { ok: true, meta: { op: "assert" } }; } }).request(REQ));
  await tick(); await tick();
  assert.equal(r.session.requests.length, 1);                        // waiting for the person
  release();
  await p;
  assert.equal(r.session.requests.length, 2);
});
