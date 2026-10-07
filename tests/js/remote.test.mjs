// The glue of the Remote pages (R10 wiring): the pairing link is taken out of the address at once, every answer to a
// `pair` request is opened with the §8.1 step 4 check (the vectors `pending_answers` run through it), the transport
// renders nothing it dropped and says a refusal in fixed words, sign-out deletes the bridge database.
// tests/js/remote-mutations (a script, see the PR) points REMOTE_JS_DIR at a mutated copy of the modules.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const JS = process.env.REMOTE_JS_DIR ? pathToFileURL(process.env.REMOTE_JS_DIR + "/") : new URL("../../fileshare/static/js/", import.meta.url);
const B = await import(new URL("bridge-crypto.js", JS));
const C = await import(new URL("crypto.js", JS));
const { DeviceSession } = await import(new URL("bridge-session.js", JS));
const P = await import(new URL("remote-pair.js", JS));
const T = await import(new URL("remote-transport.js", JS));
const M = await import(new URL("remote-model.js", JS));
const { wipeBridge } = await import(new URL("bridge-wipe.js", JS));
const { clearLocalData } = await import(new URL("outbox-ui.js", JS));

const VEC = JSON.parse(readFileSync(new URL("../bridge_vectors.json", import.meta.url), "utf8"));
const { hexToBytes: hex } = C;
const WS = VEC.keys.workspace;
const kWs = await B.workspaceKey(hex(VEC.keys.mk), WS);
const counter = { next: async () => 1, atLeast: async () => {} };

// ---- the address -------------------------------------------------------------------------------------------------

function fakeWin(hash) {
  const w = { location: { hash, pathname: "/remote/pair", search: "" }, history: { calls: [], replaceState(...a) { this.calls.push(a); w.location.hash = ""; } } };
  return w;
}
const LINK = VEC.pairing.link_fragment;

test("the pairing secret is removed from the address as soon as it is read, and the link parses", () => {
  const w = fakeWin("#" + LINK.replace(/^#/, ""));
  const link = P.readPairLink(w);
  assert.equal(link.workspace, WS);
  assert.equal(w.location.hash, "");
  assert.deepEqual(w.history.calls, [[null, "", "/remote/pair"]]);
});

test("a fragment that is not a pairing link is removed too, and gives no link", () => {
  const w = fakeWin("#v1.not.a.link");
  assert.equal(P.readPairLink(w), null);
  assert.equal(w.location.hash, "");
  const none = fakeWin("");
  assert.equal(P.readPairLink(none), null);
  assert.equal(none.history.calls.length, 0);
});

// ---- every answer to a pair request (the vectors) ----------------------------------------------------------------

test("pending answers and pair refusals: every vector, through openPairAnswer", async () => {
  let n = 0;
  for (const c of VEC.pending_answers) {
    const pending = new Map(Object.entries(c.pending).map(([k, v]) => [k, { ...v, offsetAdopted: v.offset_adopted }]));
    const s = new DeviceSession({ workspace: WS, kWs, keyVersion: 1, deviceId: hex(VEC.ids.device_a.device_id), signKey: null, hostKey: null,
      counter, now: () => c.now_ms });
    s.pending = pending;
    const r = await P.openPairAnswer(s, hex(c.envelope), c.mailbox, hex(c.host_pin));
    const got = r.result !== "accept" ? { result: "drop" } : { result: "accept", host_pub: C.bytesToHex(r.hostPub), fingerprint: r.fingerprint,
      refusal: r.refusal ? r.meta.refusal : null, offset_ms: r.offsetMs ?? null, clock_wrong: r.clockWrong ?? null };
    assert.deepEqual(got, c.expect, c.name);
    assert.equal(s.hostKey, null, `${c.name}: the pin-checked key is not kept on the session`);
    assert.equal(s.pinFailures, 0, `${c.name}: an answer that fails the pin or the signature is no evidence of a changed host key`);
    n++;
  }
  assert.equal(n, VEC.pending_answers.length);
});

// ---- the transport -----------------------------------------------------------------------------------------------

// A scripted session and mailbox: the session says what each chunk is; the mailbox hands them over when a request is posted.
function rig(script, { silent = 0 } = {}) {
  let n = 0;
  const posted = [], cancelled = [];
  const session = {
    pending: new Map(),
    async request(args) { const id = (++n).toString(16).padStart(32, "0"); this.last = args; return { id, envelope: new Uint8Array([n]) }; },
    retry: (id) => new Uint8Array([9]),
    expire() {},
    async receive(env, mb) { return script.shift()(env, mb); },
  };
  let posts = 0;
  const mailbox = {
    async post(id, env, stream, listener) {
      posted.push({ id, env: [...env], stream });
      if (posts++ < silent) return;
      queueMicrotask(() => listener.chunk({ env: new Uint8Array(1), mailbox: { id, idx: 0, last: true, stream: false } }));
    },
    async cancel(id) { cancelled.push(id); },
    close() {},
  };
  return { session, mailbox, posted, cancelled };
}
const accept = (meta, data = "", last = true) => () => ({ result: "accept", rid: "x", last, refusal: false, meta, data: new TextEncoder().encode(data) });
const collect = async (it) => { const out = []; for await (const e of it) out.push(e); return out; };
const REQ = { method: "GET", path: "/", headers: {}, body: null, stream: false, signal: new AbortController().signal };

test("a reply becomes head, chunk and end; `page` is true only when the host's signed meta says so", async () => {
  const r = rig([accept({ status: 200, headers: { "Content-Type": "text/html" }, page: true }, "<p>hi</p>")]);
  const ev = await collect(T.bridgeTransport(r).request(REQ));
  assert.deepEqual(ev.map((e) => e.type), ["head", "chunk", "end"]);
  assert.equal(ev[0].page, true);
  assert.equal(ev[0].headers["content-type"], "text/html");
  const r2 = rig([accept({ status: 200, headers: {}, page: "yes" }, "x")]);
  assert.equal((await collect(T.bridgeTransport(r2).request(REQ)))[0].page, false);
});

test("a dropped chunk is never rendered; the next real one is", async () => {
  const r = rig([() => ({ result: "drop", why: "host_signature" }), accept({ status: 200, headers: {} }, "ok")]);
  const t = T.bridgeTransport(r);
  // the mailbox answers a post once, so give the drop its own delivery
  const post = r.mailbox.post;
  r.mailbox.post = async (id, env, s, l) => { await post(id, env, s, l); queueMicrotask(() => l.chunk({ env: new Uint8Array(1), mailbox: { id, idx: 0, last: true, stream: false } })); };
  const ev = await collect(t.request(REQ));
  assert.deepEqual(ev.map((e) => e.type), ["head", "chunk", "end"]);
});

test("a refusal throws the fixed text for its code, tells the page, and never echoes the host's words", async () => {
  const seen = [];
  const r = rig([() => ({ result: "accept", rid: "x", last: true, refusal: true, meta: { refusal: "revoked", text: "<b>pwn</b>" }, data: new Uint8Array(0) })]);
  await assert.rejects(collect(T.bridgeTransport({ ...r, onRefusal: (c, t) => seen.push([c, t]) }).request(REQ)),
    (e) => e.code === "revoked" && e.message === M.REFUSAL_TEXT.revoked && !e.message.includes("pwn"));
  assert.deepEqual(seen, [["revoked", M.REFUSAL_TEXT.revoked]]);
  const unknown = rig([() => ({ result: "accept", rid: "x", last: true, refusal: true, meta: { refusal: "<script>" }, data: new Uint8Array(0) })]);
  await assert.rejects(collect(T.bridgeTransport(unknown).request(REQ)), (e) => e.message === M.refusalText("nope") && !e.message.includes("script"));
});

test("a stale_sequence refusal that the session turns into a resend goes out again as a new request", async () => {
  const r = rig([() => ({ result: "accept", rid: "x", last: true, refusal: true, meta: { refusal: "stale_sequence", high: 5 }, resend: { meta: { op: "http", method: "GET", path: "/" } }, data: new Uint8Array(0) }),
    accept({ status: 200, headers: {} }, "again")]);
  const ev = await collect(T.bridgeTransport(r).request(REQ));
  assert.equal(r.posted.length, 2);
  assert.notEqual(r.posted[0].id, r.posted[1].id);
  assert.equal(new TextDecoder().decode(ev[1].data), "again");
});

test("no first chunk: the same bytes are sent once more, then the host is said to be silent", async () => {
  const t = T.bridgeTransport({ ...rig([accept({ status: 200, headers: {} }, "late")], { silent: 1 }), firstChunkMs: 10 });
  const r = rig([accept({ status: 200, headers: {} }, "late")], { silent: 1 });
  const ev = await collect(T.bridgeTransport({ ...r, firstChunkMs: 10 }).request(REQ));
  assert.deepEqual(r.posted.map((p) => p.env), [[1], [9]], "the retry is session.retry's bytes");
  assert.equal(r.posted[0].id, r.posted[1].id);
  assert.equal(ev.length, 3);
  const dead = rig([], { silent: 5 });
  await assert.rejects(collect(T.bridgeTransport({ ...dead, firstChunkMs: 10 }).request(REQ)), (e) => e.message === M.HOST_SILENT);
  assert.ok(t);
});

test("a redirect is followed to a valid path and the frame sees the final one; an outside address is refused", async () => {
  const r = rig([accept({ status: 302, headers: { Location: "/board" } }), accept({ status: 200, headers: {}, page: true }, "board")]);
  const ev = await collect(T.bridgeTransport(r).request({ ...REQ, method: "POST", body: new Uint8Array([1]) }));
  assert.equal(ev[0].url, "/board");
  assert.equal(r.session.last.meta.method, "GET");
  assert.equal(r.session.last.meta.path, "/board");
  const out = rig([accept({ status: 302, headers: { Location: "https://evil.test/x" } })]);
  await assert.rejects(collect(T.bridgeTransport(out).request(REQ)), /will not follow/);
});

// ---- sign-out ------------------------------------------------------------------------------------------------------

test("wipeBridge deletes the fileshare-bridge database", async () => {
  const names = [];
  await wipeBridge({ deleteDatabase: (n) => { names.push(n); const req = {}; queueMicrotask(() => req.onsuccess()); return req; } });
  assert.deepEqual(names, ["fileshare-bridge"]);
  await assert.rejects(wipeBridge({ deleteDatabase: () => { const req = { error: new Error("blocked") }; queueMicrotask(() => req.onerror()); return req; } }));
});

test("sign-out clears the bridge database, and a failure there never skips the outbox or the lists", async (t) => {
  t.mock.method(console, "error", () => {});
  const order = [];
  const ok = await clearLocalData({ keys: async () => order.push("keys"), bridge: async () => order.push("bridge"), queue: async () => order.push("outbox"), lists: async () => order.push("lists") });
  assert.deepEqual(order, ["keys", "bridge", "outbox", "lists"]);
  assert.deepEqual(ok, { keys: true, bridge: true, outbox: true, lists: true });
  const done = await clearLocalData({ keys: async () => {}, bridge: async () => { throw new Error("idb"); }, queue: async () => {}, lists: async () => {} });
  assert.deepEqual(done, { keys: true, bridge: false, outbox: true, lists: true });
});

// ---- words ----------------------------------------------------------------------------------------------------------

test("a host that does not answer is said in words, and only an online, paired workspace opens", () => {
  for (const s of ["not_answering", "lost", "stopped", "never_started", "zzz"]) assert.ok(M.hostMessage(s).length > 20, s);
  assert.equal(M.hostMessage("online"), "");
  assert.match(M.hostMessage("lost"), /Lost/);
  assert.equal(M.canOpen({ state: "online" }, true), true);
  assert.equal(M.canOpen({ state: "online" }, false), false);
  assert.equal(M.canOpen({ state: "not_answering" }, true), false);
  assert.equal(M.pairRefusalText("pairing_closed"), B.MESSAGES.usedElsewhere);
});

// ---- review round: redirect limit, an empty poll answer, session expiry --------------------------------------------

test("at most five redirects are followed; the sixth is refused", async () => {
  const hop = (n) => accept({ status: 302, headers: { Location: `/h${n}` } });
  const five = rig([...[1, 2, 3, 4, 5].map(hop), accept({ status: 200, headers: {} }, "end")]);
  const ev = await collect(T.bridgeTransport(five).request(REQ));
  assert.equal(ev[0].url, "/h5");
  const six = rig([...[1, 2, 3, 4, 5, 6].map(hop), accept({ status: 200, headers: {} }, "end")]);
  await assert.rejects(collect(T.bridgeTransport(six).request(REQ)), /will not follow/);
});

test("a poll answered with no body (204) is an empty poll, not a crash", async () => {
  const { createMailbox } = await import(new URL("remote-mailbox.js", JS));
  let polls = 0;
  const got = [];
  const fetchFn = async (url, init) => {
    if (init.method === "POST") return { ok: true, status: 201, json: async () => ({}) };
    polls++;
    if (polls === 1) return { ok: true, status: 204, json: async () => { throw new Error("no body"); } };
    return { ok: true, status: 200, json: async () => ({ chunks: [{ id: "a".repeat(32), idx: 0, last: true, stream: false, body: "AQID" }] }) };
  };
  const mb = createMailbox("b".repeat(32), { fetchFn });
  await mb.post("a".repeat(32), new Uint8Array([1]), false, { chunk: (c) => { got.push(c); mb.close(); }, fail: (e) => got.push(e) });
  for (let i = 0; i < 50 && !got.length; i++) await new Promise((r) => setTimeout(r, 10));
  assert.equal(got.length, 1);
  assert.deepEqual([...got[0].env], [1, 2, 3]);
});

test("a session that ends deletes the bridge database too (signInAgain)", async () => {
  const names = [];
  globalThis.indexedDB = { open() { throw new Error("no idb"); }, deleteDatabase: (n) => { names.push(n); const r = {}; queueMicrotask(() => r.onsuccess()); return r; } };
  globalThis.location = { pathname: "/remote", search: "", replace() {} };
  const { signInAgain } = await import(new URL("mirrors-data.js", JS));
  await signInAgain();
  assert.deepEqual(names, ["fileshare-bridge"]);
});
