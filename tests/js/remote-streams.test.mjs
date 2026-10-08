// Streams through the Remote transport (R10 streams end to end): frames in order, keepalives are not frames, LAST ends, the
// device's `cancel` goes out when the frame lets go, a silent stream says the computer is lost, a reconnect waits its
// turn (10 s, doubling to 60 s; here scaled down), a consumer that does not read gets the stream dropped, a stream that was
// ended for good (revoked) is not opened again. Also the viewer's decisions (what is shown, what is capped, never HTML).
// REMOTE_JS_DIR points the mutation script at a mutated copy of the modules.
import { test } from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const JS = process.env.REMOTE_JS_DIR ? pathToFileURL(process.env.REMOTE_JS_DIR + "/") : new URL("../../fileshare/static/js/", import.meta.url);
const T = await import(new URL("remote-transport.js", JS));
const M = await import(new URL("remote-model.js", JS));
const { hexToBytes } = await import(new URL("crypto.js", JS));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = (u8) => new TextDecoder().decode(u8);

// A scripted session and mailbox. `script` answers session.receive in order; feed() hands the transport one chunk.
function rig(script) {
  let n = 0;
  const requests = [], posted = [], cancelled = [], listeners = [];
  const session = {
    pending: new Map(),
    async request(args) { const id = (++n).toString(16).padStart(32, "0"); requests.push({ id, ...args }); this.pending.set(id, {}); return { id, envelope: new Uint8Array([n]) }; },
    retry: () => new Uint8Array([9]),
    async receive(env, mb) { return script.shift()(env, mb); },
  };
  const mailbox = {
    async post(id, env, stream, listener) { posted.push({ id, stream, at: Date.now() }); listeners.push(listener); },
    async cancel(id) { cancelled.push(id); },
    close() {},
  };
  const feed = (k = 1, l = listeners.at(-1)) => { for (let i = 0; i < k; i++) l.chunk({ env: new Uint8Array(1), mailbox: { id: "x", idx: i, last: false, stream: true } }); };
  return { session, mailbox, requests, posted, cancelled, feed, listeners };
}
const accept = (meta, data = "", last = false) => () => ({ result: "accept", rid: "x", last, refusal: false, meta, data: new TextEncoder().encode(data) });
const HEAD = accept({ status: 200, headers: { "Content-Type": "text/event-stream" } });
const frame = (s) => accept({}, s);
const KEEP = accept({ keepalive: true });
const LAST = accept({}, "", true);
const refuse = (code) => () => ({ result: "accept", rid: "x", last: true, refusal: true, meta: { refusal: code }, data: new Uint8Array(0) });
const REQ = (extra = {}) => ({ method: "GET", path: "/events", headers: {}, body: null, stream: true, signal: new AbortController().signal, ...extra });
const FAST = { reconnectMs: 40, reconnectMax: 160, healthyMs: 40, quietMs: 60, floorMs: 0 };

async function until(fn, ms = 1000) { const t = Date.now(); while (!fn()) { if (Date.now() - t > ms) throw new Error("timeout"); await sleep(2); } }

test("a stream is opened with the STREAM flag; frames come in the order they were sent, keepalives are not frames, LAST ends it", async () => {
  const r = rig([HEAD, frame("data: 1\n\n"), KEEP, frame("data: 2\n\n"), KEEP, LAST]);
  const it = T.bridgeTransport({ ...r, ...FAST, quietMs: 1000 }).request(REQ());
  const got = [];
  const run = (async () => { for await (const e of it) got.push(e.type === "chunk" ? text(e.data) : e.type); })();
  await until(() => r.listeners.length === 1);
  r.feed(6);
  await run;
  assert.deepEqual(got, ["head", "data: 1\n\n", "data: 2\n\n", "end"]);
  assert.equal(r.requests[0].flags, 2, "the request carries STREAM");
  assert.equal(r.posted[0].stream, true);
});

test("frames coalesced into one chunk, or one frame split over two, reach the frame whole and in order", async () => {
  const r = rig([HEAD, frame("data: 1\n\ndata: 2\n\n"), frame("data: 3"), frame("\n\n"), LAST]);
  const got = [];
  const run = (async () => { for await (const e of T.bridgeTransport({ ...r, ...FAST, quietMs: 1000 }).request(REQ())) if (e.type === "chunk") got.push(text(e.data)); })();
  await until(() => r.listeners.length === 1);
  r.feed(5);
  await run;
  assert.equal(got.join(""), "data: 1\n\ndata: 2\n\ndata: 3\n\n");
});

test("closing the stream sends `cancel` naming the stream, as a request of its own", async () => {
  const r = rig([HEAD, frame("data: 1\n\n")]);
  const ac = new AbortController();
  const run = (async () => { for await (const e of T.bridgeTransport({ ...r, ...FAST, quietMs: 1000 }).request(REQ({ signal: ac.signal }))) if (e.type === "chunk") ac.abort(); })();
  await until(() => r.listeners.length === 1);
  r.feed(2);
  await run;
  await until(() => r.requests.some((q) => q.meta.op === "cancel"));
  const c = r.requests.find((q) => q.meta.op === "cancel");
  assert.deepEqual([...c.stream], [...hexToBytes(r.requests[0].id)], "the cancel names the stream's request id");
  assert.equal(c.flags ?? 0, 0, "the cancel itself is not a stream");
  assert.ok(r.cancelled.includes(r.requests[0].id), "and the mailbox route is given up");
});

test("a stream that goes quiet for the silence limit says the computer is lost; the next answer says it is back", async () => {
  const seen = [];
  const r = rig([HEAD, frame("data: 1\n\n"), HEAD]);
  const t = T.bridgeTransport({ ...r, ...FAST, onHost: (w) => seen.push(w) });
  const t0 = Date.now();
  const run = (async () => { for await (const e of t.request(REQ())) { void e; } })();
  await until(() => r.listeners.length === 1);
  r.feed(2);
  await assert.rejects(run, (e) => e.message === M.HOST_SILENT);
  assert.ok(Date.now() - t0 >= FAST.quietMs - 5, "not before the limit");
  assert.deepEqual(seen, ["lost"]);
  await until(() => r.requests.some((q) => q.meta.op === "cancel"));
  // an answer to anything makes it ok again
  const run2 = (async () => { for await (const e of t.request(REQ({ stream: false, path: "/" }))) { void e; } })();
  await until(() => r.listeners.length >= 3);
  r.feed(1);
  r.listeners.at(-1).chunk({ env: new Uint8Array(1), mailbox: { id: "x", idx: 0, last: true, stream: false } });
  await run2.catch(() => {});
  assert.deepEqual(seen, ["lost", "ok"]);
});

test("a reconnect for the same path waits 1x, 2x, 4x the base and stops growing at the cap; a healthy stream starts it over", async () => {
  const r = rig([...Array(8)].flatMap(() => [HEAD, LAST]));
  const waits = [];
  const t = T.bridgeTransport({ ...r, ...FAST, onHost: (w, ms) => w === "waiting" && waits.push(ms) });
  const gaps = [];
  for (let i = 0; i < 5; i++) {
    const before = r.posted.length;
    const run = (async () => { for await (const e of t.request(REQ())) { void e; } })();
    await until(() => r.posted.length === before + 1, 2000);
    gaps.push(r.posted.at(-1).at);
    r.feed(2);
    await run;
  }
  const d = gaps.slice(1).map((g, i) => g - gaps[i]);
  assert.ok(d[0] >= 35, `first reconnect after the base: ${d}`);
  assert.ok(d[1] >= 75 && d[1] < 150, `then twice that: ${d}`);
  assert.ok(d[2] >= 155, `then four times, which is the cap: ${d}`);
  assert.ok(d[3] >= 155 && d[3] < 230, `and no more than the cap after that: ${d}`);
  assert.equal(waits.length, 4, "every wait is announced");
  // a stream that lived long enough was healthy: the next one is not held back
  const before = r.posted.length;
  await sleep(10);
  const run = (async () => { for await (const e of t.request(REQ())) { void e; } })();
  await until(() => r.posted.length === before + 1, 2000);
  const start = r.posted.at(-1).at;
  await sleep(FAST.healthyMs + 15);
  r.feed(2);
  await run;
  const t1 = Date.now();
  const run2 = (async () => { for await (const e of t.request(REQ())) { void e; } })();
  await until(() => r.posted.length === before + 2, 2000);
  assert.ok(r.posted.at(-1).at - t1 < 30, `after a healthy stream there is no wait (${r.posted.at(-1).at - t1}); started ${start}`);
  r.feed(2);
  await run2;
});

test("a stream the frame closed while it waited for its turn never posts anything", async () => {
  const r = rig([HEAD, LAST]);
  const t = T.bridgeTransport({ ...r, ...FAST, reconnectMs: 300 });
  const run = (async () => { for await (const e of t.request(REQ())) { void e; } })();
  await until(() => r.listeners.length === 1);
  r.feed(2);
  await run;
  const ac = new AbortController();
  const second = (async () => { for await (const e of t.request(REQ({ signal: ac.signal }))) { void e; } })();
  await sleep(30);
  ac.abort();
  await second;
  assert.equal(r.posted.length, 1);
});

test("plain pages are never held back, whatever the streams did", async () => {
  const r = rig([accept({ status: 200, headers: {} }, "a", true), accept({ status: 200, headers: {} }, "b", true)]);
  const t = T.bridgeTransport({ ...r, ...FAST });
  for (let i = 0; i < 2; i++) {
    const run = (async () => { for await (const e of t.request(REQ({ stream: false, path: "/" }))) { void e; } })();
    await until(() => r.listeners.length === i + 1);
    r.listeners[i].chunk({ env: new Uint8Array(1), mailbox: { id: "x", idx: 0, last: true, stream: false } });
    await run;
  }
  assert.equal(r.posted.length, 2);
  // even after a stream ended young for the same path, a page is not held back
  const r2 = rig([HEAD, LAST, accept({ status: 200, headers: {} }, "c", true)]);
  const t2 = T.bridgeTransport({ ...r2, ...FAST, reconnectMs: 300 });
  const s = (async () => { for await (const e of t2.request(REQ())) { void e; } })();
  await until(() => r2.listeners.length === 1);
  r2.feed(2);
  await s;
  const t0 = Date.now();
  const p = (async () => { for await (const e of t2.request(REQ({ stream: false }))) { void e; } })();
  await until(() => r2.listeners.length === 2);
  r2.listeners[1].chunk({ env: new Uint8Array(1), mailbox: { id: "x", idx: 0, last: true, stream: false } });
  await p;
  assert.ok(Date.now() - t0 < 100, "a page for the path of a stream that just ended waits for nothing");
});

test("a consumer that does not read gets the stream dropped and the computer told", async () => {
  const r = rig([HEAD, ...Array(400).fill(frame("data: x\n\n"))]);
  const it = T.bridgeTransport({ ...r, ...FAST, quietMs: 1000 }).request(REQ())[Symbol.asyncIterator]();
  const first = it.next();
  await until(() => r.listeners.length === 1);
  r.feed(1);
  assert.equal((await first).value.type, "head");
  const second = it.next();                      // parked in the generator; the next chunks queue up faster than it reads
  r.feed(T.MAX_QUEUED + 50);
  await assert.rejects(second, /could not keep up/);
  await until(() => r.requests.some((q) => q.meta.op === "cancel"));
});

test("a refusal that ends a stream for good is said once and the stream is not opened again; other refusals are not final", async () => {
  const seen = [];
  const r = rig([HEAD, refuse("revoked")]);
  const t = T.bridgeTransport({ ...r, ...FAST, onRefusal: (c) => seen.push(c) });
  const run = (async () => { for await (const e of t.request(REQ())) { void e; } })();
  await until(() => r.listeners.length === 1);
  r.feed(2);
  await assert.rejects(run, (e) => e.code === "revoked" && e.message === M.REFUSAL_TEXT.revoked);
  const posts = r.posted.length;
  await assert.rejects((async () => { for await (const e of t.request(REQ({ path: "/other" }))) { void e; } })(), (e) => e.code === "revoked");
  assert.equal(r.posted.length, posts, "nothing was sent to the computer");
  assert.deepEqual(seen, ["revoked", "revoked"]);
  // busy is not final
  const b = rig([HEAD, refuse("busy"), HEAD, LAST]);
  const tb = T.bridgeTransport({ ...b, ...FAST, reconnectMs: 5, reconnectMax: 10 });
  const rb = (async () => { for await (const e of tb.request(REQ())) { void e; } })();
  await until(() => b.listeners.length === 1);
  b.feed(2);
  await assert.rejects(rb, (e) => e.code === "busy");
  const rb2 = (async () => { for await (const e of tb.request(REQ())) { void e; } })();
  await until(() => b.listeners.length === 2);
  b.feed(2);
  await rb2;
});

// ---- the viewer's decisions ----------------------------------------------------------------------------------------

const view = (path, headers, n = 10) => ({ path, headers, body: new Uint8Array(n) });

test("HTML and SVG are never shown, whatever the name or type says; text, JSON and images are", () => {
  for (const v of [view("/f/a.html", { "content-type": "text/plain" }), view("/f/a.txt", { "content-type": "text/html" }), view("/f/x", { "content-type": "application/xhtml+xml" }),
    view("/f/a.svg", { "content-type": "image/png" }), view("/f/a", { "content-type": "image/svg+xml" })]) {
    assert.equal(M.viewPlan(v).reason, "none", v.path);
  }
  assert.equal(M.viewPlan(view("/f/a.txt", { "content-type": "text/plain" })).kind.kind, "text");
  assert.equal(M.viewPlan(view("/f/a.json", {})).kind.kind, "json");
  assert.equal(M.viewPlan(view("/f/a.png", { "content-type": "image/png" })).kind.kind, "image");
  assert.equal(M.viewPlan(view("/f/a.md", {})).kind.kind, "text", "no Markdown renderer here: shown as text");
  assert.equal(M.viewPlan(view("/f/a.md", {}), { markdown: true }).kind.kind, "markdown");
});

test("a file over its cap is not shown: text at 2 MiB, an image at 8 MiB", () => {
  assert.equal(M.viewPlan(view("/f/a.txt", {}, M.VIEW_MAX)).kind.kind, "text");
  assert.equal(M.viewPlan(view("/f/a.txt", {}, M.VIEW_MAX + 1)).reason, "large");
  assert.equal(M.viewPlan(view("/f/a.png", {}, M.VIEW_MAX + 1)).kind.kind, "image");
  assert.equal(M.viewPlan(view("/f/a.png", {}, M.MEDIA_MAX + 1)).reason, "large");
  assert.equal(M.DOWNLOAD_MAX, 8 * 1024 * 1024);
});

test("a file name comes from the header or the path and is made safe", () => {
  assert.equal(M.fileName(view("/f/report.txt?x=1", {})), "report.txt");
  assert.equal(M.fileName(view("/f/x", { "content-disposition": 'attachment; filename="../../etc/pass wd.txt"' })), "pass wd.txt");
  assert.equal(M.fileName(view("/f/x", { "content-disposition": "attachment; filename*=UTF-8''a%20b.txt" })), "a b.txt");
  assert.equal(M.fileName(view("/f/x", { "content-disposition": "attachment; filename=%E0%A4%A.txt" })), "%E0%A4%A.txt", "a broken escape does not throw");
});

// ---- review follow-ups: keys, floor, cap, pending, wrong type, not-a-failure, the default silence limit ------------------------

const cycle = async (t, r, path, extra = {}) => {      // one short stream: open, head, LAST
  const n = r.listeners.length;
  const run = (async () => { for await (const e of t.request(REQ({ path, ...extra }))) { void e; } })();
  await until(() => r.listeners.length === n + 1, 3000);
  const at = r.posted.at(-1).at;
  r.feed(2);
  await run;
  return at;
};

test("the back-off is keyed on the path without its query; another path waits for the floor after a failure", async () => {
  const r = rig([...Array(8)].flatMap(() => [HEAD, LAST]));
  const t = T.bridgeTransport({ ...r, ...FAST, reconnectMs: 60, floorMs: 80 });
  const a = await cycle(t, r, "/events?1");
  const b = await cycle(t, r, "/events?2");
  assert.ok(b - a >= 55, `a new query is the same path: ${b - a}`);
  const c = await cycle(t, r, "/other");
  assert.ok(c - b >= 75, `a different path still waits for the floor: ${c - b}`);
});

test("a path whose gate was dropped (more than MAX_GATES paths) is not held back", async () => {
  const n = T.MAX_GATES + 2;
  const r = rig([...Array(n + 1)].flatMap(() => [HEAD, LAST]));
  const t = T.bridgeTransport({ ...r, ...FAST, reconnectMs: 400 });
  await cycle(t, r, "/p0");
  for (let i = 1; i < n; i++) await cycle(t, r, `/p${i}`);
  const t0 = Date.now();
  const at = await cycle(t, r, "/p0");
  assert.ok(at - t0 < 100, `forgotten: ${at - t0}`);
});

test("the slow-consumer path forgets the pending request; a stream with another type is cancelled at the computer", async () => {
  const r = rig([HEAD, ...Array(400).fill(frame("data: x\n\n"))]);
  const it = T.bridgeTransport({ ...r, ...FAST, quietMs: 1000 }).request(REQ())[Symbol.asyncIterator]();
  const first = it.next();
  await until(() => r.listeners.length === 1);
  r.feed(1);
  await first;
  const second = it.next();
  r.feed(T.MAX_QUEUED + 50);
  await assert.rejects(second, /could not keep up/);
  assert.equal(r.session.pending.has(r.requests[0].id), false);
  const w = rig([accept({ status: 200, headers: { "Content-Type": "text/html" } })]);
  const run = (async () => { for await (const e of T.bridgeTransport({ ...w, ...FAST }).request(REQ())) { void e; } })();
  await until(() => w.listeners.length === 1);
  w.feed(1);
  await assert.rejects(run, /did not answer with a stream/);
  await until(() => w.requests.some((q) => q.meta.op === "cancel"));
});

test("a mailbox error is not a final refusal: the next stream opens", async () => {
  const r = rig([HEAD, HEAD, LAST]);
  const t = T.bridgeTransport({ ...r, ...FAST, reconnectMs: 5, reconnectMax: 10 });
  const run = (async () => { for await (const e of t.request(REQ())) { void e; } })();
  await until(() => r.listeners.length === 1);
  r.feed(1);
  r.listeners[0].fail(Object.assign(new Error("net"), { status: 500 }));
  await assert.rejects(run, (e) => e.message === M.HOST_SILENT);
  await cycle(t, r, "/events");
});

test("a stream the page closed itself is not a failure: early closes in a row wait only the base", async () => {
  const r = rig([...Array(6)].flatMap(() => [HEAD]));
  const t = T.bridgeTransport({ ...r, ...FAST, reconnectMs: 40, reconnectMax: 400 });
  const starts = [];
  for (let i = 0; i < 4; i++) {
    const ac = new AbortController();
    const run = (async () => { for await (const e of t.request(REQ({ signal: ac.signal }))) { void e; } })();
    await until(() => r.listeners.length === i + 1, 3000);
    starts.push(Date.now());
    r.feed(1);
    await sleep(5);
    ac.abort();
    await run;
  }
  assert.ok(starts[3] - starts[2] < 90, `the fourth opens after the base wait, not a grown one: ${starts[3] - starts[2]}`);
});

test("the silence limit is 40 s, not 60: the default, with the clock mocked", async (ctx) => {
  assert.equal(T.QUIET_MS, 40_000);
  ctx.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const r = rig([HEAD, frame("data: 1\n\n")]);
  const run = (async () => { for await (const e of T.bridgeTransport({ session: r.session, mailbox: r.mailbox }).request(REQ())) { void e; } })();
  const out = run.then(() => "done", (e) => e.message);
  for (let i = 0; i < 20 && !r.listeners.length; i++) await Promise.resolve();
  r.feed(2);
  for (let i = 0; i < 50; i++) await Promise.resolve();
  ctx.mock.timers.tick(39_000);
  for (let i = 0; i < 50; i++) await Promise.resolve();
  assert.equal(await Promise.race([out, Promise.resolve("pending")]), "pending", "not yet at 39 s");
  ctx.mock.timers.tick(1_500);
  assert.equal(await out, M.HOST_SILENT);
});

test("an audio type that is not a plain token is not played; the plain ones are", () => {
  assert.equal(M.viewPlan(view("/f/a.mp3", { "content-type": "audio/é" })).reason, "none");
  assert.equal(M.viewPlan(view("/f/a.mp3", { "content-type": "audio/mpeg" })).kind.type, "audio/mpeg");
});
