// fileshare/static/js/bridge-transport.js — the transport the frame host talks to (docs/bridge-frame.md).
// The frame host (frame-host.js) knows nothing about keys, envelopes or the mailbox. js/remote-transport.js implements this
// interface with the real crypto (bridge-session.js) and the mailbox; the tests use fakeTransport below.
//
//   transport.request(req) -> async iterable of events
//
//   req    {method, path, headers, body: Uint8Array | null, stream: boolean, signal: AbortSignal}
//          path is already validated and in scope; headers hold only the allow-listed names (frame-scope.js).
//          The implementation ends the request when `signal` aborts (and when the iterator's return() is called).
//   events {type: "head", status, headers, url?, page?}   exactly once, first
//            headers: lower-case names. url: the FINAL path after any redirect the host followed (default: the
//            request path); a 3xx never reaches the frame. page: true only when the HOST marked the answer as a
//            dashboard page (a signed field of its reply); a transport must never infer it from the body.
//          {type: "chunk", data: Uint8Array}              zero or more
//          {type: "end"}                                   last. A refusal or a transport failure is a thrown
//            Error (message shown as text only); a refused request is `status` 403 with a head, not an exception.

export function assertTransport(t) {
  if (!t || typeof t.request !== "function") throw new TypeError("transport.request(req) is required");
  return t;
}

const bytes = (v) => (typeof v === "string" ? new TextEncoder().encode(v) : v instanceof Uint8Array ? v : new Uint8Array(v || 0));

// A transport for tests: answer(req) returns {status = 200, headers = {}, body = "", page = false, url} or, for a
// stream, {stream: true} (events are then pushed with fake.streams[i].push(text) and ended with .end()). Every request
// is recorded in fake.calls; fake.streams lists the streams opened, each with `closed` (true once aborted or ended).
export function fakeTransport(answer) {
  const fake = {
    calls: [],
    streams: [],
    request(req) {
      fake.calls.push({ method: req.method, path: req.path, headers: req.headers, body: req.body, stream: req.stream });
      return run(req);
    },
  };
  async function* run(req) {
    const r = (await answer(req)) || { status: 404, body: "not found" };
    if (r.error) throw new Error(r.error);
    const headers = Object.fromEntries(Object.entries(r.headers || {}).map(([k, v]) => [k.toLowerCase(), v]));
    if (r.stream) {
      const queue = [];
      let wake = null;
      const s = { path: req.path, closed: false,
        push(text) { queue.push(bytes(text)); wake?.(); },
        end() { s.closed = true; wake?.(); } };
      fake.streams.push(s);
      req.signal.addEventListener("abort", () => { s.closed = true; wake?.(); });
      yield { type: "head", status: r.status ?? 200, headers: { "content-type": "text/event-stream", ...headers } };
      while (!s.closed || queue.length) {
        if (queue.length) { yield { type: "chunk", data: queue.shift() }; continue; }
        await new Promise((resolve) => { wake = resolve; });
      }
      yield { type: "end" };
      return;
    }
    yield { type: "head", status: r.status ?? 200, headers, url: r.url, page: r.page === true };
    const body = bytes(r.body);
    if (body.length) yield { type: "chunk", data: body };
    yield { type: "end" };
  }
  return fake;
}
