// fileshare/static/js/remote-transport.js: the frame host's transport (bridge-transport.js) over the real bridge: every
// request is sealed and signed by a DeviceSession (bridge-session.js), posted to the mailbox (remote-mailbox.js) and
// its answer chunks are opened by the session (host signature against the pinned key, tag, order, window; §7).
// A drop is never rendered. A refusal throws a fixed text (remote-model.js), never an echo. `page` comes only from the
// host's signed reply meta. Redirects are followed here (a 3xx never reaches the frame) and only to a valid path.
// Streams (docs/bridge-frame.md, "What the frame host guarantees"): chunk 0 is the head, then frames in order,
// keepalives are not frames, LAST ends, `cancel` goes to the computer when the frame lets go, and a new stream for a
// path waits its turn (every reconnect is a bridged request).
import { F_STREAM } from "./bridge-crypto.js";
import { hexToBytes } from "./crypto.js";
import { validPath } from "./frame-scope.js";
import { CHANGED_TEXT, HOST_SILENT, SIGNED_OUT, refusalText } from "./remote-model.js";
import { unlockText } from "./unlock.js";

export const FIRST_CHUNK_MS = 20_000;     // no first chunk: cancel and send the same bytes once more (§5.3), then give up
export const CHUNK_MS = 60_000;           // between chunks of one page
export const QUIET_MS = 40_000;           // a stream with no chunk, not even a keepalive (every 20 s), for this long: the computer is lost
export const RECONNECT_MS = 10_000;       // a new stream for the same path at most this often, growing to RECONNECT_MAX_MS
export const RECONNECT_MAX_MS = 60_000;   //   (every reconnect is a bridged request; the host's quota is about 1.1 a second)
export const FLOOR_MS = 2_000;           // once any stream has failed, no two stream opens (any path) closer than this
export const MAX_GATES = 64;              // paths remembered
export const HEALTHY_MS = 60_000;         // a stream that lived this long was healthy: the back-off starts over
export const DECLINED_MS = 300_000;       // a stream path whose unlock sheet was declined or failed is refused without a new sheet this long
export const LEASE_DECLINED_MS = 30_000;  // a request path whose typing-lease sheet was declined is refused without a new sheet this long (remote-lease.js has the same cool-down)
export const MAX_QUEUED = 256;            // chunks waiting for a consumer that does not read: beyond this the stream is dropped
const ENDS_STREAMS = new Set(["revoked", "not_paired", "stopped", "scope_changed"]);   // after these no stream is opened again
const MAX_REDIRECTS = 5;
const EMPTY = new Uint8Array(0);
// One spelling of a path for what is remembered about it: no query or fragment, no empty or "." segment, ".." resolved,
// and a percent-escape of an unreserved character written as the character (the computer treats them alike).
export function normPath(p) {
  const out = [];
  const plain = String(p).split(/[?#]/)[0].replace(/%([0-9a-f]{2})/gi, (m, h) => { const c = String.fromCharCode(parseInt(h, 16)); return /[A-Za-z0-9._~-]/.test(c) ? c : m.toUpperCase(); });
  for (const seg of plain.split("/")) {
    if (seg === "" || seg === ".") continue;
    if (seg === "..") out.pop(); else out.push(seg);
  }
  return "/" + out.join("/");
}
const SLOW = "The dashboard could not keep up with the computer. Reconnecting.";

export class RefusalError extends Error {
  constructor(code) { super(refusalText(code)); this.name = "RefusalError"; this.code = code; }
}

// A small async queue with a timeout and an abort.
function queue() {
  const items = [];
  let wake = null;
  return {
    push: (v) => { items.push(v); wake?.(); },
    size: () => items.length,
    reset: () => { items.length = 0; },
    async next(ms, signal) {
      if (!items.length && !signal?.aborted) {
        await new Promise((resolve) => {
          const t = setTimeout(resolve, ms), done = () => { clearTimeout(t); signal?.removeEventListener("abort", done); wake = null; resolve(); };
          wake = done;
          signal?.addEventListener("abort", done);
        });
      }
      return items.length ? items.shift() : null;
    },
  };
}

const sleep = (ms, signal) => new Promise((resolve) => {
  const done = () => { clearTimeout(t); signal?.removeEventListener("abort", done); resolve(); };
  const t = setTimeout(done, ms);
  signal?.addEventListener("abort", done);
});

const NEEDS_UNLOCK = new Set(["assertion_required", "lease_required"]);

// opts: session (DeviceSession), mailbox, onRefusal(code, text), unlock(session, {code, meta, rid}, {signal}) (unlock.js
// askAssertion; without it the refusal is just said) and onHost("lost") when the computer stopped answering,
// onHost("waiting", ms) while a reconnect waits its turn, onHost("ok") when it answers again. The rest are timings (tests).
// A request refused for an assertion is never retried silently: the sheet is shown, and only a confirmed assertion is sent,
// once, as the `assert` request whose answer is the refused request's own result (§9.4). Asked once per exchange; a stream
// whose sheet was declined is not asked about again for DECLINED_MS, so a reconnect loop cannot pile sheets up.
export function bridgeTransport({ session, mailbox, onRefusal = () => {}, onHost = () => {}, unlock = null, firstChunkMs = FIRST_CHUNK_MS, chunkMs = CHUNK_MS,
  quietMs = QUIET_MS, reconnectMs = RECONNECT_MS, reconnectMax = RECONNECT_MAX_MS, healthyMs = HEALTHY_MS, floorMs = FLOOR_MS, leaseDeclinedMs = LEASE_DECLINED_MS, now = () => Date.now() }) {
  let note = null, ended = null;          // note: what onHost last said, until the next answer
  const declinedPlain = new Map();     // normalised path -> {until, err}: a lease sheet the person declined
  const gates = new Map();             // stream path (no query) -> {start, delay, fails}
  let lastOpen = 0, failed = false;    // when any stream last opened; whether any has failed

  async function* exchange({ method, path, headers, body, stream, streamRid, signal }) {
    const args = { meta: { op: "http", method, path, headers }, data: body || EMPTY, flags: stream ? F_STREAM : 0 };
    if (streamRid) args.stream = hexToBytes(streamRid);    // a typing-lease request names the stream this device opened (§9.4, remote-lease.js)
    let asked = false, askedCode = null;
    for (let round = 0; round < 3; round++) {            // a second and third round only for a resend the host asked for
      const q = queue();
      const listener = { chunk: (c) => { if (q.size() >= MAX_QUEUED) { q.reset(); q.push({ slow: true }); } else q.push(c); },
        fail: (e) => q.push({ error: e }) };
      let sent = await session.request(args), finished = false, retried = false, resend = false, first = true;
      const abort = () => {
        mailbox.cancel(sent.id);
        if (stream) cancelStream(sent.id);
      };
      signal?.addEventListener("abort", abort, { once: true });
      try {
        try { await mailbox.post(sent.id, sent.envelope, stream, listener); } catch (e) { throw mailboxProblem(e); }
        for (;;) {
          const c = await q.next(first ? firstChunkMs : stream ? quietMs : chunkMs, signal);
          if (signal?.aborted) return;
          if (c === null) {
            if (stream && !first) { session.pending?.delete(sent.id); cancelStream(sent.id); throw new Error(HOST_SILENT); }
            if (!first || retried) throw new Error(HOST_SILENT);
            retried = true;                                // the same bytes, after giving the first post up
            await mailbox.cancel(sent.id);
            await mailbox.post(sent.id, session.retry(sent.id), stream, listener).catch((e) => { throw mailboxProblem(e); });
            continue;
          }
          if (c.slow) { session.pending?.delete(sent.id); cancelStream(sent.id); throw new Error(SLOW); }
          if (c.error) throw mailboxProblem(c.error);
          const r = await session.receive(c.env, c.mailbox);
          if (r.result === "drop") {
            if (r.message) throw new Error(r.message);
            continue;                                      // never rendered, and no answer either
          }
          if (r.refusal) {
            if (r.resend) { args.meta = r.resend.meta; resend = true; break; }
            const code = String(r.meta.refusal);
            if (unlock && !asked && NEEDS_UNLOCK.has(code)) {
              asked = true;
              askedCode = code;
              const u = await unlock(session, { code, meta: r.meta, rid: sent.id }, { signal });
              if (signal?.aborted) return;
              if (u.ok) { args.meta = u.meta; delete args.stream; resend = true; break; }
              const text = u.text || unlockText(u.reason);
              onRefusal(code, text);
              throw Object.assign(new RefusalError(code), { message: text, declined: u.reason !== "busy" });
            }
            // The computer refuses a confirmed start whose details changed meanwhile with the plain code (it adds no reason on the
            // wire): after our own confirmation of a fresh action that is the likeliest cause; the sentence says no more than that.
            if (code === "assertion_failed" && askedCode === "assertion_required") { onRefusal(code, CHANGED_TEXT); throw Object.assign(new RefusalError(code), { message: CHANGED_TEXT }); }
            onRefusal(code, r.message || refusalText(code));
            throw new RefusalError(code);
          }
          if (first) {
            first = false;
            const m = r.meta;
            if (!Number.isInteger(m.status) || m.status < 100 || m.status > 599) throw new Error("The computer sent an answer this app cannot read.");
            const h = {};
            for (const [k, v] of Object.entries(m.headers && typeof m.headers === "object" ? m.headers : {})) if (typeof v === "string") h[k.toLowerCase()] = v;
            if (stream && (m.status !== 200 || !(h["content-type"] || "").startsWith("text/event-stream"))) { cancelStream(sent.id); throw new Error("The computer did not answer with a stream."); }
            yield { type: "head", status: m.status, headers: h, page: m.page === true, rid: sent.id };
          }
          if (r.data?.length) yield { type: "chunk", data: r.data };
          if (r.last) { finished = true; yield { type: "end" }; return; }
        }
      } finally {
        signal?.removeEventListener("abort", abort);
        if (!finished && !resend) mailbox.cancel(sent.id);
      }
    }
    throw new Error(HOST_SILENT);
  }

  // The device-side `cancel` (§4): a request whose header names the stream. Best effort.
  async function cancelStream(rid) {
    try {
      const s = await session.request({ meta: { op: "cancel" }, data: EMPTY, stream: hexToBytes(rid) });
      await mailbox.post(s.id, s.envelope, false, { chunk: () => mailbox.cancel(s.id), fail: () => {} });
    } catch { /* the stream is closed on this side either way */ }
  }

  async function* redirecting(req) {
    let { method, path, body } = req;
    for (let hops = 0; ; hops++) {
      const it = exchange({ method, path, headers: req.headers, body, stream: req.stream, streamRid: hops ? null : req.streamRid, signal: req.signal });
      let redirect = null;
      for await (const ev of it) {
        if (ev.type === "head" && !req.stream && [301, 302, 303, 307, 308].includes(ev.status)) {
          const loc = validPath(ev.headers.location);
          if (!loc || hops >= MAX_REDIRECTS) throw new Error("The computer sent the page somewhere this app will not follow.");
          redirect = loc;                                // ponytail: once SCOPES is narrowed, the target must pass scopes.allows too
          continue;                                      // drain the redirect's own body
        }
        if (redirect) continue;
        yield hops && ev.type === "head" ? { ...ev, url: path } : ev;
      }
      if (!redirect) return;
      path = redirect;                                   // ponytail: every redirect becomes a GET, also a 307/308
      method = "GET";
      body = null;
    }
  }

  // Says "lost" once when the computer stops answering and "ok" when the next answer arrives.
  async function* plain(req) {
    try {
      for await (const ev of redirecting(req)) {
        if (ev.type === "head" && note) { note = null; onHost("ok"); }
        yield ev;
      }
    } catch (e) {
      if (e?.message === HOST_SILENT && note !== "lost") { note = "lost"; onHost("lost"); }
      throw e;
    }
  }

  // A stream is never opened again after a refusal that ended it for good. Otherwise one per path per `delay`: 10, 20,
  // 40, 60 s while the ones before died young, and from the start again after one that lived a minute. A stream the
  // frame closed itself changes nothing.
  async function* stream(req) {
    if (ended) { onRefusal(ended, refusalText(ended)); throw new RefusalError(ended); }
    const key = normPath(req.path);
    let g = gates.get(key);
    if (g?.declined && g.declined.until > now()) throw g.declined.err;      // the person said no a moment ago
    const until = Math.max(g ? g.start + g.delay : 0, failed ? lastOpen + floorMs : 0);
    if (until > now()) {
      note = "waiting";
      onHost("waiting", until - now());
      await sleep(until - now(), req.signal);
      if (req.signal?.aborted) return;
    }
    if (!g) {
      if (gates.size >= MAX_GATES) gates.delete(gates.keys().next().value);
      gates.set(key, g = { start: 0, delay: reconnectMs, fails: 0 });
    }
    g.start = lastOpen = now();
    try {
      yield* plain(req);
    } catch (e) {
      if (ENDS_STREAMS.has(e?.code)) ended = e.code;
      if (e?.declined) g.declined = { until: now() + DECLINED_MS, err: e };
      throw e;
    } finally {
      if (!req.signal?.aborted) {
        const healthy = now() - g.start >= healthyMs;
        if (!healthy) failed = true;
        g.fails = healthy ? 0 : g.fails + 1;
        g.delay = healthy ? reconnectMs : Math.min(reconnectMax, reconnectMs * 2 ** (g.fails - 1));
      }
    }
  }

  // A plain request whose lease sheet was declined is refused again without a sheet for leaseDeclinedMs, whatever the
  // spelling of its path: a page that posts again and again cannot raise a sheet each time.
  async function* checked(req) {
    const key = normPath(req.path), d = declinedPlain.get(key);
    if (d && d.until > now()) throw d.err;
    try {
      yield* plain(req);
    } catch (e) {
      if (e?.declined && e.code === "lease_required") {
        if (declinedPlain.size >= MAX_GATES) declinedPlain.delete(declinedPlain.keys().next().value);
        declinedPlain.set(key, { until: now() + leaseDeclinedMs, err: e });
      }
      throw e;
    }
  }

  return { request: (req) => (req.stream ? stream(req) : checked(req)) };
}

function mailboxProblem(e) {
  if (e?.status === 401 || e?.status === 403) return new Error(SIGNED_OUT);
  return new Error(HOST_SILENT);
}
