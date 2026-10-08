// Typing from the phone (remote-lease.js between the frame host and remote-transport.js, bridge-protocol.md section 9.4):
// which stream a lease request names, what happens when there is none, how long the sheet may take, what a cancelled sheet
// does to the keys, when the "typing unlocked" note is set and cleared. The browser path, with the real sheet and a host
// that enforces the lease, is tests/browser/test_terminal.py. tests/js/lease-mutations.mjs points LEASE_JS_DIR at a mutated
// copy of fileshare/static/js and every test here must still be able to fail.
import { test, after } from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const JS = process.env.LEASE_JS_DIR ? pathToFileURL(process.env.LEASE_JS_DIR + "/") : new URL("../../fileshare/static/js/", import.meta.url);
const { bytesToHex } = await import(new URL("crypto.js", JS));
const L = await import(new URL("remote-lease.js", JS));
const T = await import(new URL("remote-transport.js", JS));
const te = new TextEncoder();
const tick = (ms = 5) => new Promise((r) => setTimeout(r, ms));

// A session and a mailbox that answer from a script, and remember what was sent (meta, body, the stream the request names).
function rig(script) {
  let n = 0;
  const sent = [], listeners = [];
  const session = {
    pending: new Map(),
    async request(args) {
      const id = (++n).toString(16).padStart(32, "0");
      sent.push({ id, meta: structuredClone(args.meta), body: args.data && new TextDecoder().decode(args.data), stream: args.stream ? bytesToHex(args.stream) : null, flags: args.flags });
      return { id, envelope: new Uint8Array([n]) };
    },
    retry: () => new Uint8Array([9]), expire() {},
    async receive() { return script.shift()(); },
  };
  const mailbox = {
    async post(id, env, stream, listener) { listeners.push({ id, listener }); queueMicrotask(() => listener.chunk({ env: new Uint8Array(1), mailbox: { id, idx: 0, last: !stream, stream: !!stream } })); },
    async cancel() {}, close() {},
  };
  const deliver = (i, idx = 1) => listeners[i].listener.chunk({ env: new Uint8Array(1), mailbox: { id: listeners[i].id, idx, last: true, stream: true } });
  return { session, mailbox, sent, deliver };
}
const lease = (code = "lease_required") => () => ({ result: "accept", rid: "x", last: true, refusal: true, meta: { refusal: code, purpose: "lease", scope: "type", expires_ms: 1, nonce: "0".repeat(64), subject: { kind: "lease", shown: "Type for 15 minutes", digest: "" } }, data: new Uint8Array(0) });
const refuse = (code) => () => ({ result: "accept", rid: "x", last: true, refusal: true, meta: { refusal: code }, data: new Uint8Array(0) });
const done = (status = 204) => () => ({ result: "accept", rid: "x", last: true, refusal: false, meta: { status, headers: {} }, data: new Uint8Array(0) });
const open = () => ({ result: "accept", rid: "x", last: false, refusal: false, meta: { status: 200, headers: { "content-type": "text/event-stream" } }, data: new Uint8Array(0) });
const STREAM = (path) => ({ method: "GET", path, headers: {}, body: null, stream: true, signal: new AbortController().signal });
const KEYS = (name = "work", body = '{"seq":[{"text":"ls"}],"n":1,"page":"abcd1234"}') => ({ method: "POST", path: `/terminals/${name}/keys`, headers: { "content-type": "application/json" }, body: te.encode(body), stream: false, signal: new AbortController().signal });
const glues = [];
after(() => glues.forEach((g) => g.stop()));      // the lease note's timer must not keep node alive
const collect = async (it) => { const out = []; for await (const e of it) out.push(e); return out; };

// glue + transport + a clock the test moves. `asks` records every sheet.
function setup(script, { ask, ...o } = {}) {
  const r = rig(script);
  const clock = { t: 1_000_000 };
  const log = { said: [], leases: [], asks: [] };
  const glue = L.leaseGlue({ ask: ask || (async (s, rf) => { log.asks.push(rf); return { ok: true, meta: { op: "assert", for: rf.rid } }; }),
    say: (t) => log.said.push(t), onLease: (u) => log.leases.push(u), now: () => clock.t, ...o });
  glues.push(glue);
  const transport = glue.wrap(T.bridgeTransport({ session: r.session, mailbox: r.mailbox, unlock: glue.unlock, firstChunkMs: o.firstChunkMs ?? 20_000 }));
  // open a terminal stream and keep it open; returns {rid, close}
  const watch = async (path = "/terminals/work/stream") => {
    r.script = null;
    const it = transport.request(STREAM(path))[Symbol.asyncIterator]();
    const head = await it.next();
    return { rid: head.value.rid, close: () => it.return(), next: () => it.next() };
  };
  return { ...r, clock, log, glue, transport, watch };
}

test("a key post names the stream this device opened for that terminal; the assert request names none", async () => {
  const s = setup([open, lease(), done()]);
  const w = await s.watch();
  await collect(s.transport.request(KEYS()));
  const [stream, keys, asserted] = s.sent;
  assert.equal(keys.stream, w.rid);
  assert.equal(keys.meta.path, "/terminals/work/keys");
  assert.equal(s.log.asks.length, 1);
  assert.equal(s.log.asks[0].code, "lease_required");
  assert.equal(asserted.meta.op, "assert");
  assert.equal(asserted.stream, null);
  await w.close();
});

test("a plain page request names no stream", async () => {
  const s = setup([open, done(200)]);
  await s.watch();
  await collect(s.transport.request({ method: "GET", path: "/terminals/work/snapshot", headers: {}, body: null, stream: false }));
  assert.equal(s.sent[1].stream, null);
});

test("with no stream open a lease route is refused in words and nothing is sent", async () => {
  const s = setup([]);
  await assert.rejects(collect(s.transport.request(KEYS())), (e) => e.message === L.LEASE_TEXT.no_stream);
  assert.equal(s.sent.length, 0);
  assert.deepEqual(s.log.said, [L.LEASE_TEXT.no_stream]);
});

test("a terminal's keys never ride on another terminal's stream; new and agent start take any stream", async () => {
  const s = setup([open, done(), done(), done()]);
  const w = await s.watch("/terminals/other/stream");
  await assert.rejects(collect(s.transport.request(KEYS("work"))), (e) => e.message === L.LEASE_TEXT.no_stream);
  assert.equal(s.sent.length, 1);
  await collect(s.transport.request({ ...KEYS(), path: "/terminals/new", body: null }));
  await collect(s.transport.request({ ...KEYS(), path: "/t/TIX-4/agent/start", body: null }));
  assert.deepEqual(s.sent.slice(1).map((x) => x.stream), [w.rid, w.rid]);
  await w.close();
});

test("a closed stream is forgotten: the next key post is refused locally", async () => {
  const s = setup([open]);
  const w = await s.watch();
  await w.close();
  await tick();
  await assert.rejects(collect(s.transport.request(KEYS())), (e) => e.message === L.LEASE_TEXT.no_stream);
});

test("only POSTs to the lease routes are lease requests (a GET of the same path, other POSTs, are not)", async () => {
  const s = setup([done(200), done(200), done(200)]);
  for (const [method, path] of [["GET", "/terminals/work/keys"], ["POST", "/schedules/1/run"], ["POST", "/terminals/work/keys/x"]])
    await collect(s.transport.request({ method, path, headers: {}, body: null, stream: false }));
  assert.equal(s.sent.length, 3);
  assert.deepEqual(s.sent.map((x) => x.stream), [null, null, null]);
});

test("the sheet may take longer than the transport's own first-chunk limit: nothing times out while it is open", async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const s = setup([open, lease(), done()], { firstChunkMs: 30, ask: async (sess, rf) => { await gate; return { ok: true, meta: { op: "assert", for: rf.rid } }; } });
  const w = await s.watch();
  const p = collect(s.transport.request(KEYS()));
  await tick(200);                                                 // 6x the limit
  assert.equal(s.sent.length, 2);                                  // the stream and the refused post: nothing resent, nothing cancelled
  release();
  const ev = await p;
  assert.equal(ev[0].status, 204);
  assert.equal(s.sent.length, 3);
  await w.close();
});

test("the refused post is sent once and run once by the computer: the assert request carries the proof, the keys are not resent", async () => {
  const s = setup([open, lease(), done()]);
  const w = await s.watch();
  await collect(s.transport.request(KEYS()));
  assert.equal(s.sent.filter((x) => x.meta.path === "/terminals/work/keys").length, 1);
  assert.equal(s.sent.filter((x) => x.meta.op === "assert").length, 1);
  await w.close();
});

test("typing unlocked is set 15 minutes after the confirmation once the post is answered, and cleared when that passes", async () => {
  const s = setup([open, lease(), done()], { leaseMs: 120 });
  const w = await s.watch();
  assert.deepEqual(s.log.leases, []);
  await collect(s.transport.request(KEYS()));
  assert.deepEqual(s.log.leases, [null, s.clock.t + 120]);          // cleared at the refusal, set at the answer
  await tick(250);
  assert.deepEqual(s.log.leases.at(-1), null);
  await w.close();
});

test("the default lease is 15 minutes and the cool-down 30 seconds", () => {
  assert.equal(L.LEASE_MS, 15 * 60_000);
  assert.equal(L.COOL_MS, 30_000);
});

test("a second lease_required (the lease ran out) clears the note and asks again", async () => {
  const s = setup([open, lease(), done(), lease(), done()]);
  const w = await s.watch();
  await collect(s.transport.request(KEYS()));
  s.clock.t += 16 * 60_000;
  await collect(s.transport.request(KEYS()));
  assert.equal(s.log.asks.length, 2);
  assert.equal(s.log.leases.filter((x) => x === null).length, 2);
  assert.equal(s.log.leases.at(-1), s.clock.t + L.LEASE_MS);
  await w.close();
});

test("an open lease needs no sheet: the post is answered and the note is left as it was", async () => {
  const s = setup([open, done()]);
  const w = await s.watch();
  await collect(s.transport.request(KEYS()));
  assert.equal(s.log.asks.length, 0);
  assert.deepEqual(s.log.leases, []);
  await w.close();
});

test("a cancelled sheet: the post fails (the page keeps its keys), and for the cool-down nothing is sent and no sheet opens", async () => {
  const s = setup([open, lease()], { ask: async (sess, rf) => { s.log.asks.push(rf); return { ok: false, reason: "cancelled" }; } });
  const w = await s.watch();
  await assert.rejects(collect(s.transport.request(KEYS())), (e) => e.message === "Not confirmed, so nothing was done.");
  const before = s.sent.length;
  for (let i = 0; i < 3; i++) await assert.rejects(collect(s.transport.request(KEYS())), (e) => e.message === L.LEASE_TEXT.locked);
  assert.equal(s.sent.length, before);
  assert.equal(s.log.asks.length, 1);
  assert.equal(s.log.said.at(-1), L.LEASE_TEXT.locked);
  await w.close();
});

test("after the cool-down the same post is sent again unchanged and a new sheet opens", async () => {
  const s = setup([open, lease(), lease(), done()], { ask: async (sess, rf) => { s.log.asks.push(rf); return s.log.asks.length === 1 ? { ok: false, reason: "cancelled" } : { ok: true, meta: { op: "assert" } }; } });
  const w = await s.watch();
  await assert.rejects(collect(s.transport.request(KEYS())));
  s.clock.t += L.COOL_MS;
  await collect(s.transport.request(KEYS()));
  const keys = s.sent.filter((x) => x.meta.path === "/terminals/work/keys");
  assert.equal(keys.length, 2);
  assert.equal(keys[0].body, keys[1].body);                         // the same bytes, the same n
  assert.notEqual(keys[0].id, keys[1].id);                          // a new request id
  assert.equal(s.log.asks.length, 2);
  await w.close();
});

test("a sheet that is already open (busy) is not a cool-down", async () => {
  const s = setup([open, lease(), lease(), done()], { ask: async (sess, rf) => { s.log.asks.push(rf); return s.log.asks.length === 1 ? { ok: false, reason: "busy" } : { ok: true, meta: { op: "assert" } }; } });
  const w = await s.watch();
  await assert.rejects(collect(s.transport.request(KEYS())));
  await collect(s.transport.request(KEYS()));
  assert.equal(s.log.asks.length, 2);
  await w.close();
});

test("a refused or expired sheet is a cool-down too", async () => {
  for (const reason of ["expired", "failed", "no_credential"]) {
    const s = setup([open, lease()], { ask: async () => ({ ok: false, reason }) });
    const w = await s.watch();
    await assert.rejects(collect(s.transport.request(KEYS())));
    await assert.rejects(collect(s.transport.request(KEYS())), (e) => e.message === L.LEASE_TEXT.locked);
    await w.close();
  }
});

test("a revoked device sends nothing more, says why, and the note is gone", async () => {
  const s = setup([open, lease(), refuse("revoked")]);
  const w = await s.watch();
  await assert.rejects(collect(s.transport.request(KEYS())), (e) => e.code === "revoked");
  const n = s.sent.length;
  await assert.rejects(collect(s.transport.request(KEYS())), (e) => /removed from that computer/.test(e.message));
  assert.equal(s.sent.length, n);
  assert.equal(s.log.leases.at(-1), null);
  await w.close();
});

test("an assertion the computer refused does not leave a pending note for a later answer", async () => {
  const s = setup([open, lease(), refuse("assertion_failed"), done()], { ask: async (sess, rf) => { s.log.asks.push(rf); return { ok: true, meta: { op: "assert" } }; } });
  const w = await s.watch();
  await assert.rejects(collect(s.transport.request(KEYS())), (e) => e.code === "assertion_failed");
  await collect(s.transport.request(KEYS()));                       // answered without a sheet (some other lease), no confirmation behind it
  assert.deepEqual(s.log.leases, [null]);
  await w.close();
});

test("a fresh assertion (not a lease) goes straight to the sheet and does not touch the lease", async () => {
  const seen = [];
  const g = L.leaseGlue({ ask: async (sess, rf) => { seen.push(rf.code); return { ok: false, reason: "cancelled" }; }, onLease: (u) => seen.push(["lease", u]) });
  await g.unlock({}, { code: "assertion_required" });
  assert.deepEqual(seen, ["assertion_required"]);
  assert.equal((await g.unlock({}, { code: "assertion_required" })).ok, false);
});

test("a stream request is passed through unchanged", async () => {
  const s = setup([open]);
  const w = await s.watch("/events");
  assert.equal(s.sent[0].meta.path, "/events");
  assert.equal(s.sent[0].stream, null);
  await w.close();
});

test("the computer ending the stream with a revoke stops everything: nothing more is sent, the note is gone", async () => {
  const s = setup([open, refuse("revoked")]);
  const w = await s.watch();
  s.glue.stop();
  s.deliver(0);                                                       // the stream's last chunk is the revoke
  await assert.rejects(w.next(), (e) => e.code === "revoked");
  const n = s.sent.length;
  await assert.rejects(collect(s.transport.request(KEYS())), (e) => /removed from that computer/.test(e.message));
  assert.equal(s.sent.length, n);
  assert.equal(s.log.leases.at(-1), null);
});

test("the scope table lets the frame open the terminal streams, and no other path as a stream", async () => {
  const { compileScopes } = await import(new URL("frame-scope.js", JS));
  const { SCOPES } = await import(new URL("remote-model.js", JS));
  const sc = compileScopes(SCOPES);
  for (const p of ["/terminals/stream", "/terminals/work/stream"]) assert.equal(sc.allows("GET", p, { stream: true }), true, p);
  for (const p of ["/terminals/a/b/stream", "/terminals", "/t/DEMO-1/agent/start"]) assert.equal(sc.allows("GET", p, { stream: true }), false, p);
  assert.equal(sc.allows("POST", "/terminals/work/keys"), true);
});

const SIZE = () => ({ ...KEYS(), path: "/terminals/work/size", body: te.encode('{"cols":80,"rows":24}') });

test("sizing the view while only watching sends nothing, opens no sheet and shows no banner: the page gets a 409 it ignores", async () => {
  const s = setup([open]);
  const w = await s.watch();
  const ev = await collect(s.transport.request(SIZE()));
  assert.equal(ev[0].status, 409);
  assert.equal(s.sent.length, 1);                                  // only the stream
  assert.deepEqual(s.log.asks, []);
  assert.deepEqual(s.log.said, []);
  await w.close();
});

test("with an open lease a size post is sent, naming the terminal's stream", async () => {
  const s = setup([open, lease(), done(), done()]);
  const w = await s.watch();
  await collect(s.transport.request(KEYS()));
  await collect(s.transport.request(SIZE()));
  assert.equal(s.sent.at(-1).meta.path, "/terminals/work/size");
  assert.equal(s.sent.at(-1).stream, w.rid);
  await w.close();
});

test("a size post after the lease ended on this device is quiet again", async () => {
  const s = setup([open, lease(), done()]);
  const w = await s.watch();
  await collect(s.transport.request(KEYS()));
  s.clock.t += 16 * 60_000;
  await tick(1);
  s.glue.stop();
  const n = s.sent.length;
  const ev = await collect(s.transport.request(SIZE()));
  assert.equal(ev[0].status, 409);
  assert.equal(s.sent.length, n);
  await w.close();
});

test("the transport's abort signal reaches the sheet, for a lease and for any other assertion", async () => {
  const seen = [];
  const g = L.leaseGlue({ ask: async (sess, rf, opts) => { seen.push(opts); return { ok: false, reason: "cancelled" }; } });
  const opts = { signal: new AbortController().signal };
  await g.unlock({}, { code: "lease_required" }, opts);
  await g.unlock({}, { code: "assertion_required" }, opts);
  assert.deepEqual(seen, [opts, opts]);
  g.stop();
});

test("one sheet per request: a second lease_required after the confirmation is said, not asked again", async () => {
  const s = setup([open, lease(), lease()]);
  const w = await s.watch();
  await assert.rejects(collect(s.transport.request(KEYS())), (e) => e.code === "lease_required");
  assert.equal(s.log.asks.length, 1);
  await w.close();
});

test("the stream closed while the sheet was open: forbidden_scope is said, nothing is left pending, no note is set", async () => {
  const s = setup([open, lease(), refuse("forbidden_scope"), open, done()]);
  const w = await s.watch();
  await assert.rejects(collect(s.transport.request(KEYS())), (e) => e.code === "forbidden_scope" && /not allowed/.test(e.message));
  await w.close();
  const w2 = await s.watch();
  await collect(s.transport.request(KEYS()));                       // answered with no confirmation behind it
  assert.deepEqual(s.log.leases, [null]);
  await w2.close();
});
