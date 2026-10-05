// fileshare/static/js/frame-host.js — the TIX app's side of the dashboard frame (docs/bridge-frame.md).
// Builds the sandboxed iframe (/sandbox/dash, sandbox="allow-scripts", opaque origin), talks to it over postMessage
// and hands every request to the abstract transport (bridge-transport.js). Loaded by nothing yet: the unlock and
// wiring ticket imports it.
//
// Who is talking. A sandboxed frame's origin is the string "null" for every document it ever holds, so the origin
// proves nothing. A message counts only when (1) event.source is this iframe's window, (2) event.origin is "null",
// (3) it is within the size and rate caps, and (4) after the shim proved it holds the one-time token written into its
// document (hello), it carries the session id this host answered with. The host replies only to the frame after that,
// and never while a load it did not expect is unproven. Any load the app did not start (a link, a form, location=,
// a reload) destroys the iframe and builds a new one.
import { el } from "./ui.js";
import { PROTOCOL, BODY_MAX, checkRequest, compileScopes, validPath } from "./frame-scope.js";
import { classify } from "./frame-render.js";
import { assertTransport } from "./bridge-transport.js";

export const LIMITS = {
  inflight: 16,          // requests answered at once per frame
  streams: 4,            // open streams per frame
  rate: 200,             // messages per second per frame; the excess is dropped
  floodSeconds: 3,       // seconds over the rate before the frame is destroyed
  response: 8 * 1024 * 1024,   // one buffered answer
  page: 4 * 1024 * 1024,       // one dashboard page
  copy: 2000,            // characters the dashboard may ask to copy (and the person sees all of them)
  copyMs: 30000,         // how long the question stays up
  copyGapMs: 2000,       // between two questions
  rebuilds: 5,           // rebuilds per minute before the frame is left stopped
  pongMs: 1500,
  writeLoads: 2,         // loads one document write of the shim may cause (Chromium and WebKit fire one more)
  writeMs: 1500,
  graceMs: 150,          // an unannounced load waits this long for the shim's announcement before it is treated as a navigation
};
const REPLY_HEADERS = ["content-type", "etag", "last-modified", "cache-control", "content-language"];
const THEMES = ["light", "dark", "system"];
const text = new TextDecoder();

const randomToken = () => Array.from(crypto.getRandomValues(new Uint8Array(24)), (b) => b.toString(16).padStart(2, "0")).join("");

// opts: mount (element), transport, scopes (table, see frame-scope.js), start (path),
//   viewer({path, status, headers, body}), download({path, status, headers, body}), external(url),
//   history({op: "push" | "replace" | "back" | "forward" | "go", path?, n?}), theme(value), copy(text) -> Promise,
//   notice({kind, text}) (default: a line of text above the frame), log(text), limits (overrides LIMITS),
//   tap(msg) (sees every message sent to the frame: tests and debugging).
export function createFrameHost(opts) {
  const transport = assertTransport(opts.transport);
  const scopes = compileScopes(opts.scopes);
  const limits = { ...LIMITS, ...(opts.limits || {}) };
  const noop = () => {};
  const cb = {
    viewer: opts.viewer || noop, download: opts.download || noop, history: opts.history || noop, theme: opts.theme || noop,
    log: opts.log || noop,
    external: opts.external || ((url) => { window.open(url, "_blank", "noopener,noreferrer"); }),
    copy: opts.copy || ((t) => navigator.clipboard.writeText(t)),
  };
  const line = el("p", { class: "frame-notice", role: "status", hidden: true });
  const notice = opts.notice || ((n) => { line.textContent = n.text; line.hidden = !n.text; });   // text only, never markup
  const wrap = el("div", { class: "frame-host" }, line);
  opts.mount.append(wrap);

  let frame = null;
  let tok = null;          // the one-time token of the document being built; null once used
  let sid = null;          // set when the shim proved the token; every later frame message carries it
  let suspect = true;      // a load happened that no pong has vouched for: nothing is sent to the frame meanwhile
  let held = [];
  let seq = 0;
  let pongTimer = null;
  let helloTimer = null;
  let writeLoads = 0;
  let writeUntil = 0;
  let loadsExpected = 0;   // the load of the document this host asked for
  let lastPing = 0;
  let graceTimer = null;
  let flooding = false;
  let current = opts.start || "/";
  let destroyed = false;
  let stopped = false;
  const inflight = new Map();        // request id -> AbortController
  const streams = new Set();         // ids of inflight entries that are streams
  const rebuilds = [];
  let windowStart = 0;
  let windowCount = 0;
  let overSeconds = 0;

  // The clipboard. The frame can only ASK; the text is shown in the TIX page and written only by a click there (a real
  // gesture), once, for the document the app just wrote. There is no way to read the clipboard.
  let copyBox = null;
  let lastCopy = 0;
  function clearCopy(ok) {
    if (!copyBox) return;
    clearTimeout(copyBox.timer);
    copyBox.node.remove();
    const { id } = copyBox;
    copyBox = null;
    send({ t: "copied", id, ok: ok === true });
  }
  function askCopy(id, text) {
    const now = Date.now();
    if (suspect || copyBox || now - lastCopy < limits.copyGapMs) return send({ t: "copied", id, ok: false });
    lastCopy = now;
    const write = async () => {
      let ok = false;
      try { await cb.copy(text); ok = true; } catch { /* the page is told it failed */ }
      clearCopy(ok);
    };
    const node = el("div", { class: "frame-copy", role: "group", "aria-label": "Copy from the dashboard" },
      el("p", {}, "The dashboard asks to copy this text:"), el("pre", {}, text),
      el("button", { type: "button", class: "btn", onclick: write }, "Copy"),
      el("button", { type: "button", class: "btn", onclick: () => clearCopy(false) }, "Dismiss"));
    wrap.insertBefore(node, frame);
    copyBox = { id, node, timer: setTimeout(() => clearCopy(false), limits.copyMs) };
  }

  const abortAll = () => { for (const ac of inflight.values()) ac.abort(); inflight.clear(); streams.clear(); };
  const clearTimers = () => { clearTimeout(pongTimer); clearTimeout(helloTimer); clearTimeout(graceTimer); pongTimer = helloTimer = graceTimer = null; };

  function send(msg, transfer) {
    if (destroyed || !frame || sid === null) return;
    if (suspect) { held.push([msg, transfer]); return; }
    frame.contentWindow?.postMessage({ k: PROTOCOL, ...msg }, "*", transfer || []);
    if (opts.tap) opts.tap(msg);
  }

  function build() {
    tok = randomToken();
    sid = null;
    suspect = true;
    held = [];
    writeLoads = 0;
    writeUntil = 0;
    loadsExpected = 1;
    windowStart = windowCount = overSeconds = 0;
    frame = el("iframe", { class: "frame-dash", sandbox: "allow-scripts", referrerpolicy: "no-referrer", loading: "eager",
      title: opts.title || "Dashboard", src: `/sandbox/dash?tok=${tok}` });
    frame.addEventListener("load", onLoad);
    wrap.append(frame);
    // no hello in time: a frame that never started its shim is not the one this host built
    helloTimer = setTimeout(() => rebuild("no hello"), limits.pongMs * 4);
  }

  function rebuild(reason) {
    if (destroyed) return;
    clearTimers();
    abortAll();
    clearCopy(false);
    frame?.remove();
    frame = null;
    const now = Date.now();
    while (rebuilds.length && now - rebuilds[0] > 60000) rebuilds.shift();
    rebuilds.push(now);
    if (rebuilds.length > limits.rebuilds) { stopped = true; notice({ kind: "error", text: "The dashboard frame was stopped." }); return; }
    cb.log(`frame rebuilt: ${reason}`);
    build();
  }

  function onLoad() {
    if (destroyed || !frame) return;
    if (loadsExpected > 0) { loadsExpected -= 1; return; }   // the document this host asked for: the hello vouches for it
    suspect = true;
    // A later load: the shim's own document write announces up to a few, anything else is the page leaving. The
    // announcement (a message) and the load are separate tasks and may arrive in either order, so an unannounced
    // load gets a short grace for the announcement; nothing is trusted meanwhile, and without it the frame is rebuilt.
    if (!takeWriteLoad()) {
      clearTimeout(graceTimer);
      graceTimer = setTimeout(() => { graceTimer = null; if (takeWriteLoad()) ping(); else rebuild("unexpected load"); }, limits.graceMs);
      return;
    }
    ping();
  }

  function takeWriteLoad() {
    if (writeLoads > 0 && Date.now() < writeUntil) { writeLoads -= 1; return true; }
    return false;
  }

  function ping() {
    const n = lastPing = ++seq;
    clearTimeout(pongTimer);
    // sent past `send` on purpose: it is what makes the frame trusted again
    frame.contentWindow?.postMessage({ k: PROTOCOL, t: "ping", n }, "*");
    pongTimer = setTimeout(() => rebuild("no pong"), limits.pongMs);
  }

  function trusted() {
    suspect = false;
    const queue = held;
    held = [];
    for (const [msg, transfer] of queue) send(msg, transfer);
  }

  function rateOk() {
    const now = Date.now();
    if (now - windowStart >= 1000) {
      if (windowCount > limits.rate) overSeconds += 1; else overSeconds = 0;
      windowStart = now;
      windowCount = 0;
    }
    windowCount += 1;
    if (windowCount > limits.rate) {
      if (overSeconds + 1 >= limits.floodSeconds && !flooding) { flooding = true; queueMicrotask(() => { flooding = false; rebuild("flood"); }); }
      return false;
    }
    return true;
  }

  // ---- what the frame may ask ------------------------------------------------------------------------------
  const refused = (r, code) => {
    send({ t: "err", id: r.id, gen: r.gen, code, message: `refused: ${code}` });
    if (r.intent === "page" || r.intent === "open") notice({ kind: "error", text: `The dashboard page was refused (${code}).` });
  };

  async function collect(req, ac, cap) {
    let head = null;
    const parts = [];
    let size = 0;
    for await (const ev of transport.request({ ...req, signal: ac.signal })) {
      if (ac.signal.aborted) break;
      if (ev.type === "head") head = ev;
      else if (ev.type === "chunk") {
        size += ev.data.length;
        if (size > cap) throw new Error("the answer is too large");
        parts.push(ev.data);
      } else if (ev.type === "end") break;
    }
    if (!head) throw new Error("no answer");
    const body = new Uint8Array(size);
    let at = 0;
    for (const p of parts) { body.set(p, at); at += p.length; }
    return { head, body };
  }

  const answered = (head, req) => {
    const final = validPath(head.url ?? req.path);
    return final && scopes.allows("GET", final) ? final : null;
  };

  async function handleRequest(req) {
    if (inflight.size >= limits.inflight) return refused(req, "busy");
    const ac = new AbortController();
    inflight.set(req.id, ac);
    try {
      const { head, body } = await collect({ method: req.method, path: req.path, headers: req.headers, body: req.body, stream: false }, ac,
        req.intent === "page" ? limits.page : limits.response);
      if (ac.signal.aborted) return;
      const path = answered(head, req);
      if (!path) return refused(req, "scope");
      const view = { path, status: head.status, headers: head.headers || {}, body };
      if (req.intent === "page" || req.intent === "open") {
        const kind = classify({ path, status: head.status, headers: view.headers, page: head.page }, { allowPage: req.intent === "page" });
        if (kind === "page") return send({ t: "page", id: req.id, gen: req.gen, path, html: text.decode(body) });
        if (kind === "viewer") cb.viewer(view);
        else if (kind === "download") cb.download(view);
        else notice({ kind: "error", text: `The dashboard answered ${Number(head.status) || "with an error"} for ${path.slice(0, 120)}.` });
        return send({ t: "handled", id: req.id, gen: req.gen, outcome: kind });
      }
      const headers = {};
      for (const name of REPLY_HEADERS) if (typeof view.headers[name] === "string") headers[name] = view.headers[name];
      send({ t: "res", id: req.id, gen: req.gen, status: head.status, headers, url: path, body: body.buffer }, [body.buffer]);
    } catch (e) {
      if (!ac.signal.aborted) send({ t: "err", id: req.id, gen: req.gen, code: "failed", message: String(e?.message || "request failed").slice(0, 200) });
    } finally {
      inflight.delete(req.id);
    }
  }

  async function handleStream(req) {
    if (inflight.size >= limits.inflight || streams.size >= limits.streams) return refused(req, "busy");
    const ac = new AbortController();
    inflight.set(req.id, ac);
    streams.add(req.id);
    const decoder = new TextDecoder();
    try {
      for await (const ev of transport.request({ method: "GET", path: req.path, headers: { ...req.headers, accept: "text/event-stream" }, body: null, stream: true, signal: ac.signal })) {
        if (ac.signal.aborted) break;
        if (ev.type === "head") {
          if (ev.status !== 200 || !String(ev.headers?.["content-type"] || "").startsWith("text/event-stream")) throw new Error("not a stream");
          send({ t: "sdata", id: req.id, gen: req.gen, chunk: "" });
        } else if (ev.type === "chunk") send({ t: "sdata", id: req.id, gen: req.gen, chunk: decoder.decode(ev.data, { stream: true }) });
        else if (ev.type === "end") break;
      }
    } catch { /* the stream ends below */ }
    if (!ac.signal.aborted) send({ t: "send", id: req.id, gen: req.gen });
    inflight.delete(req.id);
    streams.delete(req.id);
  }

  async function handleDownload(path) {
    const ac = new AbortController();
    const id = -(++seq);                // not a frame id: nothing for the frame to answer
    inflight.set(id, ac);
    try {
      const { head, body } = await collect({ method: "GET", path, headers: {}, body: null, stream: false }, ac, limits.response);
      if (ac.signal.aborted) return;
      const final = answered(head, { path });
      if (!final) return;
      if (head.status === 200) cb.download({ path: final, status: head.status, headers: head.headers || {}, body });
      else notice({ kind: "error", text: `The dashboard answered ${Number(head.status) || "with an error"} for ${final.slice(0, 120)}.` });
    } catch (e) {
      notice({ kind: "error", text: String(e?.message || "download failed").slice(0, 200) });
    } finally {
      inflight.delete(id);
    }
  }

  function openOutside(m) {
    if (typeof m.href !== "string" || m.href.length > 2048) return;
    let url;
    try { url = new URL(m.href); } catch { return; }
    if (url.protocol === "https:" || url.protocol === "http:") cb.external(url.href);
  }

  function onMessage(event) {
    if (destroyed) { removeEventListener("message", onMessage); return; }
    if (!frame || !frame.isConnected) return;
    if (!event.source || event.source !== frame.contentWindow) return;      // any other window (or none): not ours, not even counted
    if (event.origin !== "null") return;                    // a sandboxed document has the opaque origin
    if (!rateOk()) return;
    const m = event.data;
    if (m === null || typeof m !== "object" || Object.getPrototypeOf(m) !== Object.prototype || m.k !== PROTOCOL || typeof m.t !== "string") return;
    if (m.t === "hello") {
      if (tok === null || sid !== null || m.tok !== tok) return;   // one time: the token is spent by the first proof
      tok = null;
      clearTimeout(helloTimer);
      sid = randomToken();
      suspect = false;
      send({ t: "ready", sid });
      send({ t: "go", path: current });
      return;
    }
    if (sid === null || m.sid !== sid) return;
    switch (m.t) {
      case "pong": if (m.n === lastPing) { clearTimeout(pongTimer); trusted(); } return;
      case "write":
        abortAll();
        clearCopy(false);
        writeLoads = limits.writeLoads;
        writeUntil = Date.now() + limits.writeMs;
        if (graceTimer) { clearTimeout(graceTimer); graceTimer = null; if (takeWriteLoad()) ping(); }   // its load came first
        return;
      case "req": case "sopen": {
        const r = checkRequest(m, scopes, { bodyMax: BODY_MAX });
        if (!r.ok) return refused({ id: Number.isSafeInteger(m.id) ? m.id : 0, gen: Number.isSafeInteger(m.gen) ? m.gen : 0, intent: m.intent }, r.code);
        return r.req.stream ? handleStream(r.req) : handleRequest(r.req);
      }
      case "sclose": case "abort": {
        const ac = Number.isSafeInteger(m.id) ? inflight.get(m.id) : null;
        if (ac) { ac.abort(); inflight.delete(m.id); streams.delete(m.id); }
        return;
      }
      case "rendered": {
        const path = validPath(m.path);
        if (!path) return;
        current = path;
        if (m.push === true) cb.history({ op: "push", path });
        return;
      }
      case "hist": {
        if (m.op === "push" || m.op === "replace") {
          const path = validPath(m.path);
          if (!path) return;
          current = path;
          cb.history({ op: m.op, path });
        } else if (m.op === "back" || m.op === "forward") cb.history({ op: m.op });
        else if (m.op === "go" && Number.isSafeInteger(m.n) && Math.abs(m.n) <= 50) cb.history({ op: "go", n: m.n });
        return;
      }
      case "theme": if (THEMES.includes(m.value)) cb.theme(m.value); return;
      case "copy": {
        if (!Number.isSafeInteger(m.id)) return;
        if (typeof m.text !== "string" || m.text.length < 1 || m.text.length > limits.copy) return send({ t: "copied", id: m.id, ok: false });
        askCopy(m.id, m.text);
        return;
      }
      case "open": {
        if (m.path !== undefined) {
          const path = validPath(m.path);
          if (path && scopes.allows("GET", path)) handleRequest({ id: -(++seq), gen: 0, intent: "open", method: "GET", path, headers: {}, body: null });
        } else openOutside(m);
        return;
      }
      case "download": {
        const path = validPath(m.path);
        if (path && scopes.allows("GET", path)) handleDownload(path);
        return;
      }
      case "log": if (typeof m.m === "string") cb.log(m.m.slice(0, 200)); return;
      default:
    }
  }

  addEventListener("message", onMessage);
  build();

  return {
    get frame() { return frame; },
    get stopped() { return stopped; },
    get current() { return current; },
    // The app starts a page (the first one, Back, Forward, a menu entry). Not a push: the app owns its own history.
    go(path) {
      const p = validPath(path);
      if (!p || !scopes.allows("GET", p)) return false;
      current = p;
      send({ t: "go", path: p });
      return true;
    },
    send,
    destroy() {
      destroyed = true;
      clearTimers();
      abortAll();
      removeEventListener("message", onMessage);
      clearCopy(false);
      frame?.remove();
      frame = null;
      wrap.remove();
    },
  };
}
