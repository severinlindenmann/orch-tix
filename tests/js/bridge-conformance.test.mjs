// The production device module (fileshare/static/js/bridge-*.js) against every vector of tests/bridge_vectors.json, and the
// device-side rules the vectors do not reach: storage, the sequence counter across tabs, retries, the clock offset,
// stream silence, the host-key pin and the WebAuthn calls (docs/bridge-protocol.md §4, §5, §7, §8, §9, §10).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
// The production modules; tests/js/bridge-mutations.test.mjs points BRIDGE_JS_DIR at a mutated copy of them.
const JS = process.env.BRIDGE_JS_DIR ? pathToFileURL(process.env.BRIDGE_JS_DIR + "/") : new URL("../../fileshare/static/js/", import.meta.url);
const B = await import(new URL("bridge-crypto.js", JS));
const C = await import(new URL("crypto.js", JS));
const { DeviceSession, PIN_FAILURES, STREAM_SILENCE_MS } = await import(new URL("bridge-session.js", JS));
const S = await import(new URL("bridge-store.js", JS));
import { conformance, fakeWindow, ownChecks } from "./support/bridge-conformance.mjs";
import { fakeIndexedDB } from "./support/fake-idb.mjs";

const VEC = JSON.parse(readFileSync(new URL("../bridge_vectors.json", import.meta.url), "utf8"));
const { hexToBytes: hex, bytesToHex: toHex, b64u } = C;
const subtle = globalThis.crypto.subtle;
const WS = VEC.keys.workspace, ws = hex(WS), DEV_A = VEC.ids.device_a.device_id;
const NOW = 1_790_000_000_000;

test("every vector, through the production module", async () => {
  const n = await conformance(B, C, VEC);
  assert.deepEqual(n, { hkdf: VEC.hkdf.length, seal: VEC.seal.length, sign: VEC.sign.length, sig_scalars: VEC.sig_scalars.length,
    ids: 3, pairing: 1, host_cases: VEC.host_cases.length,
    host_steps: VEC.host_cases.reduce((k, c) => k + (c.steps?.length ?? 0), 0), device_cases: VEC.device_cases.length,
    pin_runs: VEC.pin_runs.length, pending_answers: VEC.pending_answers.length, labels: VEC.labels.length,
    links: VEC.links.length, challenge_parts: VEC.challenge_parts.length,
    shown: VEC.shown.length, assertion_cases: VEC.assertion.cases.length, assertion_challenges: 3 });
});

test("the §7 and link rules the vectors have no device case for", async () => {
  assert.equal(await ownChecks(B, C, VEC), 22);
});

// ---- helpers: the host side, from the FAKE vector keys -------------------------------------------------------------

const kWs = () => B.workspaceKey(hex(VEC.keys.mk), WS);
async function privateFrom(name, usages = ["sign"]) {
  const pub = hex(VEC.keys[name].pub);
  return subtle.importKey("jwk", { kty: "EC", crv: "P-256", d: b64u(hex(VEC.keys[name].d)), x: b64u(pub.subarray(1, 33)),
    y: b64u(pub.subarray(33)), ext: false }, { name: "ECDSA", namedCurve: "P-256" }, false, usages);
}
// A response chunk as the host would send it (§3, §4). `tamper` flips a tag bit before signing.
async function chunk({ rid, seq = 0, flags = B.F_LAST, meta = { status: 200, headers: {} }, data = new Uint8Array(0), ts = NOW,
  signer = "host", device = DEV_A, tamper = false, key }) {
  const header = B.encodeHeader({ direction: B.TO_DEVICE, flags, keyVersion: 1, workspace: ws, deviceId: hex(device), rid,
    stream: B.ZERO_ID, seq, tsMs: ts, salt: crypto.getRandomValues(new Uint8Array(16)) });
  const body = await B.sealBody(key || await kWs(), header, B.frame(meta, data));
  if (tamper) body[body.length - 1] ^= 1;
  const sig = new Uint8Array(await subtle.sign({ name: "ECDSA", hash: "SHA-256" }, await privateFrom(signer), B.signedBytes(header, body)));
  return B.cat(header, body, sig);
}
const mbox = (rid, seq = 0, last = true, stream = false) => ({ id: toHex(rid), idx: seq, last, stream });

function memCounter(start = 1) {
  let next = start;
  return { next: async () => next++, atLeast: async (v) => { next = Math.max(next, v); }, peek: () => next };
}
async function session(opts = {}) {
  const clock = { t: opts.now ?? NOW };
  const s = new DeviceSession({ workspace: WS, kWs: await kWs(), keyVersion: 1, deviceId: hex(DEV_A), signKey: await privateFrom("device_a"),
    hostKey: await B.importPublicKey(hex(VEC.keys.host.pub)), counter: opts.counter || memCounter(), now: () => clock.t });
  return { s, clock };
}
const ridOf = (env) => B.decodeHeader(env.subarray(0, 104)).rid;

// ---- the wire: negative cases of our own ---------------------------------------------------------------------------

test("a chunk the host signed over a tampered tag is dropped at the tag, after the signature", async () => {
  const { s } = await session();
  const { envelope } = await s.request({ meta: { op: "http", method: "GET", path: "/" } });
  const rid = ridOf(envelope);
  const r = await s.receive(await chunk({ rid, tamper: true }), mbox(rid));
  assert.equal(r.why, "tag");
  assert.equal((await s.receive(await chunk({ rid }), mbox(rid))).result, "accept");
});

test("sealRequest: fresh rid and salt every time, only STREAM, seq 1 … 2^53-1, at most 1 MiB, opens to what was sealed", async () => {
  const signKey = await privateFrom("device_a"), key = await kWs();
  const base = { kWs: key, signKey, workspace: ws, deviceId: hex(DEV_A), keyVersion: 1, seq: 1, tsMs: NOW, meta: { op: "http", method: "GET", path: "/" } };
  const a = await B.sealRequest(base), b = await B.sealRequest(base);
  assert.notDeepEqual(a.rid, b.rid);
  assert.notDeepEqual(a.envelope.subarray(88, 104), b.envelope.subarray(88, 104));
  const { header, body, sig } = B.splitEnvelope(a.envelope);
  assert.ok(await B.verifySigned(await B.importPublicKey(hex(VEC.keys.device_a.pub)), sig, B.signedBytes(header, body)));
  assert.deepEqual(B.unframe(await B.openBody(key, header, body)).meta, base.meta);
  assert.deepEqual(B.decodeHeader(header).rid, a.rid);
  for (const bad of [{ flags: B.F_LAST }, { flags: B.F_REFUSAL }, { flags: 0x08 }, { seq: 0 }, { seq: 2 ** 53 }, { seq: 1.5 },
    { data: new Uint8Array(B.MAX_REQUEST) }, { meta: { pad: "x".repeat(B.MAX_META) } }, { meta: [] }]) {
    await assert.rejects(B.sealRequest({ ...base, ...bad }), JSON.stringify(Object.keys(bad)));
  }
  assert.ok(await B.sealRequest({ ...base, seq: Number.MAX_SAFE_INTEGER, flags: B.F_STREAM }));
});

test("K_ws: the raw MK is zeroed even when the derivation is refused", async () => {
  const mk = hex(VEC.keys.mk);
  await assert.rejects(B.workspaceKey(mk, WS.toUpperCase()));
  assert.ok(mk.every((x) => x === 0));
  await assert.rejects(B.workspaceKey(new Uint8Array(31), WS));
});

test("unframe refuses a meta length past the plaintext, over 64 KiB, or not an object", () => {
  const pt = B.frame({ op: "x" });
  new DataView(pt.buffer).setUint32(0, pt.length, false);
  assert.throws(() => B.unframe(pt));
  for (const meta of ["[]", "null", "1", "\"s\""]) {
    const m = new TextEncoder().encode(meta), p = new Uint8Array(4 + m.length);
    new DataView(p.buffer).setUint32(0, m.length, false); p.set(m, 4);
    assert.throws(() => B.unframe(p), meta);
  }
  assert.throws(() => B.unframe(new Uint8Array(3)));
});

test("a response for this device but from another workspace's key, or another key version, is dropped", async () => {
  const { s } = await session();
  const { envelope } = await s.request({ meta: { op: "http", method: "GET", path: "/" } });
  const rid = ridOf(envelope);
  const other = await B.workspaceKey(hex(VEC.keys.mk), "0".repeat(32));
  assert.equal((await s.receive(await chunk({ rid, key: other }), mbox(rid))).why, "tag");
  s.keyVersion = 2;
  assert.equal((await s.receive(await chunk({ rid }), mbox(rid))).why, "not_for_this_device");
});

// ---- the host-key pin (§7, §8.1, D6) -------------------------------------------------------------------------------

test("the pin: a host key whose pin differs is refused, never trusted on first use", async () => {
  const p = VEC.pairing;
  const wrong = hex(p.host_pin); wrong[0] ^= 1;
  await assert.rejects(B.hostKeyFromPin(hex(p.host_pub), wrong), B.HostKeyError);
  await assert.rejects(B.hostKeyFromPin(hex(VEC.keys.device_a.pub), hex(p.host_pin)), B.HostKeyError);
  await assert.rejects(B.hostKeyFromPin(hex(p.host_pub).subarray(0, 64), hex(p.host_pin)));
  assert.equal(new B.HostKeyError().message, "the host key changed: pair again");
});

test("chunks failing the pinned key: dropped; after several in a row the session drops everything and says pair again", async () => {
  const { s } = await session();
  const one = await s.request({ meta: { op: "http", method: "GET", path: "/a" } });
  const two = await s.request({ meta: { op: "http", method: "GET", path: "/b" } });
  const r1 = ridOf(one.envelope), r2 = ridOf(two.envelope);
  for (let i = 1; i < PIN_FAILURES; i++) assert.equal((await s.receive(await chunk({ rid: r1, signer: "intruder" }), mbox(r1))).why, "host_signature");
  assert.equal((await s.receive(await chunk({ rid: r2 }), mbox(r2))).result, "accept");   // a verified chunk resets the run
  for (let i = 1; i < PIN_FAILURES; i++) await s.receive(await chunk({ rid: r1, signer: "intruder" }), mbox(r1));
  assert.equal(s.hostKeyChanged, false);
  const r = await s.receive(await chunk({ rid: r1, signer: "device_a" }), mbox(r1));
  assert.equal(r.message, "the host key changed: pair again");
  assert.equal(s.hostKeyChanged, true);
  assert.equal(s.pending.size, 0);
  assert.equal((await s.receive(await chunk({ rid: r1 }), mbox(r1))).result, "drop");     // even a correctly signed one now
  await assert.rejects(s.request({ meta: { op: "http", method: "GET", path: "/" } }), B.HostKeyError);
});

test("a keyless server cannot raise the pin alarm: chunks from cleartext fields with random body and signature never count", async () => {
  // The forgery the review reproduced: every field checked before the host signature is cleartext.
  const { s } = await session();
  const { id } = await s.request({ meta: { op: "http", method: "GET", path: "/" } });
  for (let i = 0; i < PIN_FAILURES * 3; i++) {
    const h = B.encodeHeader({ direction: B.TO_DEVICE, flags: B.F_LAST, keyVersion: 1, workspace: ws, deviceId: hex(DEV_A), rid: hex(id),
      stream: B.ZERO_ID, seq: 0, tsMs: NOW, salt: crypto.getRandomValues(new Uint8Array(16)) });
    const env = B.cat(h, crypto.getRandomValues(new Uint8Array(40)), crypto.getRandomValues(new Uint8Array(64)));
    const r = await s.receive(env, { id, idx: 0, last: true, stream: false });
    assert.deepEqual([r.result, r.why, r.pinFailure, r.message], ["drop", "host_signature", false, undefined]);
  }
  assert.equal(s.hostKeyChanged, false);
  assert.equal(s.pending.size, 1);
  // forged chunks between keyed ones neither count nor reset: three keyed failures still raise it
  for (let i = 0; i < PIN_FAILURES; i++) {
    await s.receive(await chunk({ rid: hex(id), signer: "intruder" }), mbox(hex(id)));
    if (i < PIN_FAILURES - 1) {
      assert.equal(s.hostKeyChanged, false);
      const h = B.encodeHeader({ direction: B.TO_DEVICE, flags: B.F_LAST, keyVersion: 1, workspace: ws, deviceId: hex(DEV_A), rid: hex(id),
        stream: B.ZERO_ID, seq: 0, tsMs: NOW, salt: crypto.getRandomValues(new Uint8Array(16)) });
      await s.receive(B.cat(h, crypto.getRandomValues(new Uint8Array(104))), { id, idx: 0, last: true, stream: false });
    }
  }
  assert.equal(s.hostKeyChanged, true);
});

// ---- order, retries and streams (§4, §5.3) -------------------------------------------------------------------------

test("chunks are accepted only in order, and nothing after LAST", async () => {
  const { s } = await session();
  const { envelope } = await s.request({ meta: { op: "http", method: "GET", path: "/" } });
  const rid = ridOf(envelope);
  assert.equal((await s.receive(await chunk({ rid, seq: 1 }), mbox(rid, 1))).why, "out_of_order");
  assert.equal((await s.receive(await chunk({ rid, flags: 0 }), mbox(rid, 0, false))).result, "accept");
  const c1 = await chunk({ rid, seq: 1 });
  assert.equal((await s.receive(c1, mbox(rid, 1, false))).why, "mailbox_mismatch");
  assert.equal((await s.receive(c1, mbox(rid, 1))).last, true);
  assert.equal((await s.receive(c1, mbox(rid, 1))).why, "unknown_request");
});

test("a retry is the identical envelope; a new request is a new rid and the next seq", async () => {
  const counter = memCounter();
  const { s } = await session({ counter });
  const a = await s.request({ meta: { op: "http", method: "GET", path: "/" } });
  assert.deepEqual(s.retry(a.id), a.envelope);
  assert.equal(s.retry(a.id), a.envelope);
  const b = await s.request({ meta: { op: "http", method: "GET", path: "/" } });
  assert.notEqual(a.id, b.id);
  assert.deepEqual([B.decodeHeader(a.envelope.subarray(0, 104)).seq, B.decodeHeader(b.envelope.subarray(0, 104)).seq], [1n, 2n]);
  assert.equal(s.retry("00".repeat(16)), null);
});

test("a stream with no chunk for more than 60 s is closed; a keepalive keeps it open", async () => {
  const { s, clock } = await session();
  const { envelope } = await s.request({ meta: { op: "http", method: "GET", path: "/terminal/stream" }, flags: B.F_STREAM });
  const rid = ridOf(envelope), id = toHex(rid);
  const frame0 = await chunk({ rid, flags: B.F_STREAM, ts: clock.t });
  clock.t += STREAM_SILENCE_MS;
  assert.deepEqual(s.expire(), []);                                    // exactly 60 s: not yet
  assert.equal((await s.receive(frame0, mbox(rid, 0, false, true))).result, "accept");
  clock.t += STREAM_SILENCE_MS;
  const keep = await chunk({ rid, seq: 1, flags: B.F_STREAM, meta: { keepalive: true }, ts: clock.t });
  assert.deepEqual((await s.receive(keep, mbox(rid, 1, false, true))).meta, { keepalive: true });
  clock.t += STREAM_SILENCE_MS + 1;
  assert.deepEqual(s.expire(), [id]);
  assert.equal(s.pending.has(id), false);
});

// ---- the clock offset (§5.1) ---------------------------------------------------------------------------------------

test("the offset: adopted from a host-signed stale_timestamp for a pending rid, then used for ts_ms; the request goes again as new", async () => {
  const { s, clock } = await session({ now: NOW + 400_000 });           // this device runs 400 s fast
  const req = { meta: { op: "http", method: "GET", path: "/" } };
  const { envelope } = await s.request(req);
  const rid = ridOf(envelope);
  const refusal = await chunk({ rid, flags: B.F_LAST | B.F_REFUSAL, meta: { refusal: "stale_timestamp", host_ms: NOW } });
  const forged = await chunk({ rid, flags: B.F_LAST | B.F_REFUSAL, meta: { refusal: "stale_timestamp", host_ms: NOW + 10 ** 7 }, signer: "device_a" });
  assert.equal((await s.receive(forged, mbox(rid))).why, "host_signature");
  assert.equal(s.offsetMs, 0);
  const r = await s.receive(refusal, mbox(rid));
  assert.equal(r.offsetMs, -400_000);
  assert.equal(s.offsetMs, -400_000);
  assert.deepEqual(r.resend, { ...req, data: undefined, flags: 0, stream: B.ZERO_ID });
  const again = await s.request(r.resend);
  assert.equal(B.decodeHeader(again.envelope.subarray(0, 104)).tsMs, BigInt(clock.t - 400_000));
  assert.notEqual(again.id, toHex(rid));
});

test("the offset: never from a refusal for a rid that is not pending, more than 24 h is refused with the clock message", async () => {
  const { s } = await session({ now: NOW + B.MAX_OFFSET_MS + 1 });
  const { envelope } = await s.request({ meta: { op: "http", method: "GET", path: "/" } });
  const rid = ridOf(envelope), stranger = crypto.getRandomValues(new Uint8Array(16));
  const st = (r) => chunk({ rid: r, flags: B.F_LAST | B.F_REFUSAL, meta: { refusal: "stale_timestamp", host_ms: NOW } });
  assert.equal((await s.receive(await st(stranger), mbox(stranger))).why, "unknown_request");
  const r = await s.receive(await st(rid), mbox(rid));
  assert.equal(r.clockWrong, true);
  assert.equal(r.message, "this device's clock is wrong");
  assert.equal(r.resend, undefined);
  assert.equal(s.offsetMs, 0);
});

test("the offset: at most once per pending rid, even if the host's answer for it repeats", async () => {
  const pend = new Map([["ab".repeat(16), { next: 0, stream: false }]]);
  const rid = hex("ab".repeat(16));
  const ctx = { workspace: WS, kWs: await kWs(), keyVersion: 1, device: DEV_A, hostKey: await B.importPublicKey(hex(VEC.keys.host.pub)), pending: pend, offsetMs: 0 };
  const st = await chunk({ rid, flags: B.F_LAST | B.F_REFUSAL, meta: { refusal: "stale_timestamp", host_ms: NOW } });
  const r1 = await B.openResponse(ctx, st, mbox(rid), NOW + 400_000);
  assert.equal(r1.offsetMs, -400_000);
  pend.get("ab".repeat(16)).next = 0;                                   // the same answer delivered again
  const r2 = await B.openResponse(ctx, st, mbox(rid), NOW + 500_000);
  assert.equal(r2.offsetMs, undefined);
});

// ---- the sequence counter (§5.2) -----------------------------------------------------------------------------------

test("stale_sequence from the host (a restored phone): the counter jumps to high + 1 and the request goes again", async () => {
  const counter = memCounter(5);                                        // restored from an old backup: the host saw 40
  const { s } = await session({ counter });
  const { envelope } = await s.request({ meta: { op: "http", method: "GET", path: "/" } });
  const rid = ridOf(envelope);
  assert.equal(B.decodeHeader(envelope.subarray(0, 104)).seq, 5n);
  const r = await s.receive(await chunk({ rid, flags: B.F_LAST | B.F_REFUSAL, meta: { refusal: "stale_sequence", high: 40 } }), mbox(rid));
  assert.ok(r.resend);
  const again = await s.request(r.resend);
  assert.equal(B.decodeHeader(again.envelope.subarray(0, 104)).seq, 41n);
  for (const high of [-1, 1.5, "40", 2 ** 53]) {                       // a refusal without a usable high moves nothing
    const x = await s.request({ meta: { op: "http", method: "GET", path: "/" } });
    const xr = ridOf(x.envelope);
    const y = await s.receive(await chunk({ rid: xr, flags: B.F_LAST | B.F_REFUSAL, meta: { refusal: "stale_sequence", high } }), mbox(xr));
    assert.equal(y.resend, undefined, String(high));
  }
  assert.equal(counter.peek(), 46);
});

test("already_done is 'outcome unknown', never failed", async () => {
  const { s } = await session();
  const { envelope } = await s.request({ meta: { op: "http", method: "POST", path: "/x" } });
  const rid = ridOf(envelope);
  const r = await s.receive(await chunk({ rid, flags: B.F_LAST | B.F_REFUSAL, meta: { refusal: "already_done", status: "unknown" } }), mbox(rid));
  assert.equal(r.message, "outcome unknown");
  assert.equal(r.resend, undefined);
});

async function withIdb(fn, opts) {
  const saved = Object.getOwnPropertyDescriptor(globalThis, "indexedDB");
  globalThis.indexedDB = fakeIndexedDB(opts);
  try { return await fn(globalThis.indexedDB); } finally {
    if (saved) Object.defineProperty(globalThis, "indexedDB", saved); else delete globalThis.indexedDB;
  }
}

test("two tabs drawing sequence numbers at once never share one, and the counter survives a re-derived K_ws", () => withIdb(async () => {
  await S.saveWorkspaceKey(WS, await kWs(), 1);
  const tabA = S.sequenceCounter(WS), tabB = S.sequenceCounter(WS);
  const got = await Promise.all(Array.from({ length: 60 }, (_, i) => (i % 2 ? tabA : tabB).next()));
  assert.deepEqual([...got].sort((x, y) => x - y), Array.from({ length: 60 }, (_, i) => i + 1));
  await S.saveWorkspaceKey(WS, await kWs(), 1);
  assert.equal(await tabA.next(), 61);
  await Promise.all([tabA.atLeast(100), tabB.next(), tabB.atLeast(90)]);
  const n = await tabB.next();
  assert.ok(n >= 100, String(n));
}));

test("the race is real: two transactions (get, then put) would hand out the same number", () => withIdb(async (idb) => {
  // The fake really interleaves separate transactions; this is what the single transaction in sequenceCounter avoids.
  await S.saveWorkspaceKey(WS, await kWs(), 1);
  const db = await new Promise((r) => { const q = idb.open("fileshare-bridge", 1); q.onsuccess = () => r(q.result); });
  const run = (mode, f) => new Promise((resolve) => {
    const t = db.transaction("hosts", mode), req = f(t.objectStore("hosts"));
    t.oncomplete = () => resolve(req.result);
  });
  const naive = async () => {
    const rec = await run("readonly", (s) => s.get(WS));
    await run("readwrite", (s) => s.put({ ...rec, next: rec.next + 1 }));
    return rec.next;
  };
  const got = await Promise.all([naive(), naive(), naive()]);
  assert.ok(new Set(got).size < got.length, `the naive way shares numbers: ${got}`);
}));

test("the counter refuses to pass 2^53 - 1, and a workspace without a key has no counter", () => withIdb(async () => {
  await assert.rejects(S.sequenceCounter(WS).next(), S.BridgeStorageError);
  await S.saveWorkspaceKey(WS, await kWs(), 1);
  const c = S.sequenceCounter(WS);
  await c.atLeast(Number.MAX_SAFE_INTEGER);
  await assert.rejects(c.next(), /pair again/);
  await assert.rejects(c.atLeast(2 ** 53 + 2), /pair again/);
}));

test("the counter's transactions ask for strict durability; an engine that ignores the option still counts", () => withIdb(async (idb) => {
  await S.saveWorkspaceKey(WS, await kWs(), 1);
  const c = S.sequenceCounter(WS);
  assert.deepEqual([await c.next(), await c.next()], [1, 2]);    // the fake records the option and otherwise ignores it
  await c.atLeast(10);
  const hosts = idb.dbs.get("fileshare-bridge").opened.filter((t) => t.name === "hosts" && t.mode === "readwrite");
  assert.deepEqual(hosts.map((t) => t.options), [undefined, { durability: "strict" }, { durability: "strict" }, { durability: "strict" }]);
}));

test("an extractable K_ws is never stored", () => withIdb(async () => {
  const aes = await subtle.importKey("raw", new Uint8Array(32), "AES-GCM", true, ["encrypt"]);
  for (const k of [aes, { extractable: true }, {}, null]) await assert.rejects(S.saveWorkspaceKey(WS, k, 1), /extractable|Cannot/);
  assert.equal(await S.workspaceRecord(WS), null);
}));

test("without IndexedDB, or when it refuses to open, every call fails clearly and nothing falls back", async () => {
  const saved = Object.getOwnPropertyDescriptor(globalThis, "indexedDB");
  delete globalThis.indexedDB;
  try {
    const key = await kWs();
    for (const call of [() => S.deviceKey(), () => S.sequenceCounter(WS).next(), () => S.saveWorkspaceKey(WS, key, 1), () => S.pinnedHostKey(WS)]) {
      await assert.rejects(call(),(e) => e instanceof S.BridgeStorageError && /IndexedDB is unavailable/.test(e.message));
    }
  } finally {
    if (saved) Object.defineProperty(globalThis, "indexedDB", saved);
  }
  await withIdb(async () => {
    await assert.rejects(S.deviceKey(), (e) => e instanceof S.BridgeStorageError && /refused to open/.test(e.message));
  }, { failOpen: true });
});

// ---- keys in storage (§2.4, §2.6) ----------------------------------------------------------------------------------

test("the device key: non-extractable, created once even when two tabs ask at the same time", () => withIdb(async () => {
  const [a, b] = await Promise.all([S.deviceKey(), S.deviceKey()]);
  assert.equal(a.privateKey, b.privateKey);
  assert.equal(a.privateKey.extractable, false);
  await assert.rejects(subtle.exportKey("pkcs8", a.privateKey));
  await assert.rejects(subtle.exportKey("jwk", a.privateKey));
  assert.equal(a.pub.length, 65);
  assert.equal((await S.deviceKey()).privateKey, a.privateKey);
}));

test("K_ws re-opened as approve.js does: stored KEK, /api/keyblob, AAD sharing/mk/v1; kept non-extractable", () => withIdb(async () => {
  const kek = await C.importAesKey(hex("11".repeat(32)));
  const wrapped = C.b64u(await C.seal(kek, hex(VEC.keys.mk), C.AAD_MK));
  const { kWs: k, keyVersion } = await S.openWorkspaceKey(WS, { keys: async () => ({ kek }), keyblob: async () => ({ wrapped_mk: wrapped, key_version: 1 }) });
  assert.equal(keyVersion, 1);
  assert.equal(k.extractable, false);
  const c = VEC.seal[0];
  assert.equal(toHex(await B.sealBody((await S.workspaceRecord(WS)).kWs, hex(c.header), hex(c.plaintext))), c.sealed);
  await assert.rejects(S.openWorkspaceKey(WS, { keys: async () => null, keyblob: async () => ({}) }), /not signed in/);
  await assert.rejects(S.openWorkspaceKey(WS, { keys: async () => ({ kek: await C.importAesKey(hex("22".repeat(32))) }),
    keyblob: async () => ({ wrapped_mk: wrapped, key_version: 1 }) }));
}));

test("the pinned host key is stored only through a matching pin", () => withIdb(async () => {
  const p = VEC.pairing;
  await assert.rejects(S.pinHost(WS, hex(p.host_pub), hex(p.host_pin)), S.BridgeStorageError);   // no workspace yet
  await S.saveWorkspaceKey(WS, await kWs(), 1);
  assert.equal(await S.pinnedHostKey(WS), null);
  const wrong = hex(p.host_pin); wrong[31] ^= 1;
  await assert.rejects(S.pinHost(WS, hex(p.host_pub), wrong), B.HostKeyError);
  assert.equal(await S.pinnedHostKey(WS), null);
  await S.pinHost(WS, hex(p.host_pub), hex(p.host_pin));
  const key = await S.pinnedHostKey(WS);
  assert.equal(await B.verifySigned(key, hex(VEC.sign[0].sig), hex(VEC.sign[0].msg)), false);   // a device's signature is not the host's
  const ok = VEC.device_cases.find((c) => c.name === "full_response_chunk");
  const { header, body, sig } = B.splitEnvelope(hex(ok.envelope));
  assert.equal(await B.verifySigned(key, sig, B.signedBytes(header, body)), true);
}));

// ---- pairing (§8.1) ------------------------------------------------------------------------------------------------

test("the pairing link is parsed strictly", () => {
  const f = VEC.pairing.link_fragment, origin = "https://tix.example";
  assert.ok(B.parsePairLink(`${origin}/remote/pair#${f}`, origin));
  for (const bad of [f.replace("v1.", "v2."), f.toUpperCase(), f + ".x", f.slice(0, -1), f + "=", f.replace(/\.[^.]+$/, ".EOgNH5gsxH4odjMXzq0eP4D1Nj4iU8sC2axgJOADtrN"),
    f.replace(VEC.pairing.workspace, VEC.pairing.workspace.slice(2)), `${f} `, null, 7]) {
    assert.equal(B.parsePairFragment(bad), null, String(bad));
  }
  for (const bad of [`https://other.example/remote/pair#${f}`, `${origin}/pair#${f}`, `${origin}/remote/pair?x=1#${f}`, "not a url"]) {
    assert.equal(B.parsePairLink(bad, origin), null, bad);
  }
});

test("the pair request meta: label at most 80 characters, as given; the secret is zeroed", async () => {
  const pub = hex(VEC.pairing.device_pub);
  const link = () => B.parsePairFragment(VEC.pairing.link_fragment);
  const m = await B.pairRequestMeta({ link: link(), pub, label: "‮é" + "x".repeat(78) });
  assert.equal(m.label, "‮é" + "x".repeat(78));
  const l = link();
  await assert.rejects(B.pairRequestMeta({ link: l, pub, label: "x".repeat(81) }));
  assert.ok(l.secret.every((x) => x === 0));
  await assert.rejects(B.pairRequestMeta({ link: link(), pub, label: "a\uD800" }));
});

test("no answer to a pairing within 60 s, or pairing_closed, is the 'used by someone else' warning", () => {
  assert.equal(B.pairingUsedElsewhere("pairing_closed", 0), true);
  assert.equal(B.pairingUsedElsewhere(null, 59_999), false);
  assert.equal(B.pairingUsedElsewhere(null, 60_000), true);
  assert.equal(B.pairingUsedElsewhere("stale_timestamp", 70_000), false);
  assert.equal(B.MESSAGES.usedElsewhere, "this link was used by someone else: reject it on your Mac");
});

// ---- WebAuthn (§9, §10.5) ------------------------------------------------------------------------------------------

test("the WebAuthn calls: the document's parameters, only in the TIX app's own window", async () => {
  const ch = new Uint8Array(32).fill(7), dev = hex(DEV_A), cred = hex("a2".repeat(16));
  assert.deepEqual(B.registrationOptions({ challenge: ch, deviceId: dev, label: "Phone", rpId: "tix.example" }), { publicKey: {
    challenge: ch, rp: { id: "tix.example", name: "TIX" }, user: { id: dev, name: "Phone", displayName: "Phone" },
    pubKeyCredParams: [{ type: "public-key", alg: -7 }],
    authenticatorSelection: { authenticatorAttachment: "platform", userVerification: "required", residentKey: "discouraged" },
    attestation: "none", timeout: 60000 } });
  const opts = B.assertionOptions({ challenge: ch, credentialId: cred, rpId: "tix.example" });
  assert.deepEqual(opts, { publicKey: { challenge: ch, rpId: "tix.example", allowCredentials: [{ type: "public-key", id: cred }],
    userVerification: "required", timeout: 60000 } });
  const a = VEC.assertion.cases[0].assertion;
  const framed = fakeWindow(cred, a, hex);
  framed.top = {};
  await assert.rejects(B.assert(hex(VEC.assertion.challenge_inputs.rid), opts, framed), /own window/);
  await assert.rejects(B.register(B.registrationOptions({ challenge: ch, deviceId: dev, label: "Phone", rpId: "x" }), framed), /own window/);
  await assert.rejects(B.assert(hex(VEC.assertion.challenge_inputs.rid), opts, fakeWindow(hex("b3".repeat(16)), a, hex)), /another credential/);
  const reg = { navigator: { credentials: { create: async (o) => ({ rawId: cred.buffer.slice(0),
    response: { attestationObject: new Uint8Array([1, 2]).buffer, clientDataJSON: new Uint8Array([3]).buffer }, o }) } } };
  reg.top = reg.self = reg;
  assert.deepEqual(await B.register(B.registrationOptions({ challenge: ch, deviceId: dev, label: "Phone", rpId: "x" }), reg),
    { credential_id: b64u(cred), attestation_object: "AQI", client_data_json: "Aw" });
});

test("a subject is taken as received: no re-cleaning, only its three fields, and nothing that is not scalar values", async () => {
  const i = VEC.assertion.challenge_inputs;
  const parts = { workspace: hex(i.workspace), deviceId: hex(i.device), rid: hex(i.rid), purpose: i.purpose, scope: i.scope,
    expiresMs: i.expires_ms, nonce: hex(i.nonce) };
  assert.equal(toHex(await B.assertionChallenge({ ...parts, subject: { ...i.subject, extra: "ignored" } })), VEC.assertion.challenge);
  const dirty = { ...i.subject, shown: i.subject.shown + "​" };
  assert.notEqual(toHex(await B.assertionChallenge({ ...parts, subject: dirty })), VEC.assertion.challenge);
  for (const bad of [{ kind: "other" }, { digest: "AB".repeat(32) }, { digest: "ab" }, { shown: "\uDC00" }, { shown: 1 }]) {
    await assert.rejects(B.assertionChallenge({ ...parts, subject: { ...i.subject, ...bad } }), JSON.stringify(bad));
  }
  for (const bad of [{ purpose: "other" }, { scope: "admin" }, { nonce: new Uint8Array(31) }, { expiresMs: -1 }, { rid: new Uint8Array(15) }]) {
    await assert.rejects(B.assertionChallenge({ ...parts, subject: i.subject, ...bad }), JSON.stringify(Object.keys(bad)));
  }
});
