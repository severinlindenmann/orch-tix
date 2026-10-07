// fileshare/static/js/remote-transport.js: the frame host's transport (bridge-transport.js) over the real bridge: every
// request is sealed and signed by a DeviceSession (bridge-session.js), posted to the mailbox (remote-mailbox.js) and
// its answer chunks are opened by the session (host signature against the pinned key, tag, order, window; §7).
// A drop is never rendered. A refusal throws a fixed text (remote-model.js), never an echo. `page` comes only from the
// host's signed reply meta. Redirects are followed here (a 3xx never reaches the frame) and only to a valid path.
import { F_STREAM } from "./bridge-crypto.js";
import { hexToBytes } from "./crypto.js";
import { validPath } from "./frame-scope.js";
import { HOST_SILENT, SIGNED_OUT, refusalText } from "./remote-model.js";
import { STREAM_SILENCE_MS } from "./bridge-session.js";

export const FIRST_CHUNK_MS = 20_000;     // no first chunk: cancel and send the same bytes once more (§5.3), then give up
export const CHUNK_MS = 60_000;           // between chunks of one page
const MAX_REDIRECTS = 5;
const EMPTY = new Uint8Array(0);

export class RefusalError extends Error {
  constructor(code) { super(refusalText(code)); this.name = "RefusalError"; this.code = code; }
}

// A small async queue with a timeout and an abort.
function queue() {
  const items = [];
  let wake = null;
  return {
    push: (v) => { items.push(v); wake?.(); },
    async next(ms, signal) {
      if (!items.length) {
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

// opts: session (DeviceSession), mailbox, onRefusal(code, text), onEvent(name) for tests.
export function bridgeTransport({ session, mailbox, onRefusal = () => {}, firstChunkMs = FIRST_CHUNK_MS, chunkMs = CHUNK_MS }) {
  async function* exchange({ method, path, headers, body, stream, signal }) {
    const args = { meta: { op: "http", method, path, headers }, data: body || EMPTY, flags: stream ? F_STREAM : 0 };
    for (let round = 0; round < 3; round++) {            // a second and third round only for a resend the host asked for
      const q = queue();
      const listener = { chunk: (c) => q.push(c), fail: (e) => q.push({ error: e }) };
      let sent = await session.request(args), finished = false, retried = false, resend = false, first = true;
      const abort = () => {
        mailbox.cancel(sent.id);
        if (stream) cancelStream(sent.id);
      };
      signal?.addEventListener("abort", abort, { once: true });
      try {
        try { await mailbox.post(sent.id, sent.envelope, stream, listener); } catch (e) { throw mailboxProblem(e); }
        for (;;) {
          const c = await q.next(first ? firstChunkMs : stream ? STREAM_SILENCE_MS : chunkMs, signal);
          if (signal?.aborted) return;
          if (c === null) {
            if (stream && !first) { session.expire(); throw new Error(HOST_SILENT); }
            if (!first || retried) throw new Error(HOST_SILENT);
            retried = true;                                // the same bytes, after giving the first post up
            await mailbox.cancel(sent.id);
            await mailbox.post(sent.id, session.retry(sent.id), stream, listener).catch((e) => { throw mailboxProblem(e); });
            continue;
          }
          if (c.error) throw mailboxProblem(c.error);
          const r = await session.receive(c.env, c.mailbox);
          if (r.result === "drop") {
            if (r.message) throw new Error(r.message);
            continue;                                      // never rendered, and no answer either
          }
          if (r.refusal) {
            if (r.resend) { args.meta = r.resend.meta; resend = true; break; }
            const code = String(r.meta.refusal);
            onRefusal(code, r.message || refusalText(code));
            throw new RefusalError(code);
          }
          if (first) {
            first = false;
            const m = r.meta;
            if (!Number.isInteger(m.status) || m.status < 100 || m.status > 599) throw new Error("The computer sent an answer this app cannot read.");
            const h = {};
            for (const [k, v] of Object.entries(m.headers && typeof m.headers === "object" ? m.headers : {})) if (typeof v === "string") h[k.toLowerCase()] = v;
            yield { type: "head", status: m.status, headers: h, page: m.page === true };
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

  return {
    async *request(req) {
      let { method, path, body } = req;
      for (let hops = 0; ; hops++) {
        const it = exchange({ method, path, headers: req.headers, body, stream: req.stream, signal: req.signal });
        let redirect = null;
        for await (const ev of it) {
          if (ev.type === "head" && !req.stream && [301, 302, 303, 307, 308].includes(ev.status)) {
            const loc = validPath(ev.headers.location);
            if (!loc || hops >= MAX_REDIRECTS) throw new Error("The computer sent the page somewhere this app will not follow.");
            redirect = loc;
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
    },
  };
}

function mailboxProblem(e) {
  if (e?.status === 401 || e?.status === 403) return new Error(SIGNED_OUT);
  return new Error(HOST_SILENT);
}
