// fileshare/static/js/frame-host.js — the TIX app's side of the dashboard frame (docs/bridge-frame.md).
// Builds the sandboxed iframe (/sandbox/dash, sandbox="allow-scripts", opaque origin), talks to it and hands every
// request to the abstract transport (bridge-transport.js). Loaded by js/remote.js (the /remote page), with the transport of js/remote-transport.js.
//
// Who is talking. A sandboxed frame's origin is the string "null" for every document it ever holds, so the origin
// proves nothing, and the iframe's WindowProxy stays the same when a document replaces another. So identity is a
// channel, not a name: the one window message this host accepts is hello, from this iframe's window with the opaque
// origin, carrying the one-time token written into the document it asked for. The host answers with ONE MessagePort
// (ready) and everything else, both ways, travels on that port. A document that replaced the shim's (a navigation the
// shim did not start, a page that sets location) never holds the port: it cannot speak to the host and cannot listen.
// Window messages other than a valid hello are dropped. A heartbeat over the port rebuilds the iframe when it stops
// answering, whatever the iframe's load events did, and any load the app did not start rebuilds it at once.
import { el, shown } from "./ui.js";
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
  promptMs: 30000,       // how long a question to the person stays up
  promptGapMs: 2000,     // between two questions
  promptDelayMs: 500,    // a click on a question's button counts only this long after it appeared
  gestureGapMs: 1000,    // one action that needs a user gesture per this long
  rebuilds: 5,           // rebuilds per minute before the frame is left stopped
  helloMs: 6000,         // the shim must say hello within this
  pingMs: 1000,          // heartbeat: a ping over the port every second; the frame is rebuilt when two in a row go unanswered (about 2 s)
  writeLoads: 2,         // loads one document write of the shim may cause (Chromium and WebKit fire one more)
  writeMs: 1500,
  graceMs: 150,          // an unannounced load waits this long for the shim's announcement before it is treated as a navigation
  string: 4096,          // longest string in a frame message
  keys: 12,              // most own keys in a frame message
};
const REPLY_HEADERS = ["content-type", "etag", "last-modified", "cache-control", "content-language"];
const THEMES = ["light", "dark", "system"];
const text = new TextDecoder();

const randomToken = () => Array.from(crypto.getRandomValues(new Uint8Array(24)), (b) => b.toString(16).padStart(2, "0")).join("");

// The cheapest checks first, before anything reads a field: the browser has already cloned the message (a hostile frame
// can make that cost something; nothing here can stop it), so what is bounded is what the host does with it.
export function boundedShape(m, limits = LIMITS) {
  if (m === null || typeof m !== "object" || Array.isArray(m) || Object.getPrototypeOf(m) !== Object.prototype) return false;
  const keys = Object.keys(m);
  if (keys.length > limits.keys || typeof m.t !== "string" || m.t.length > 16) return false;
  for (const k of keys) if (typeof m[k] === "string" && m[k].length > limits.string) return false;
  return true;
}

// opts: mount (element), transport, scopes (table, see frame-scope.js), start (path),
//   viewer({path, status, headers, body}), download({path, status, headers, body}), external(url),
//   history({op: "push" | "replace" | "back" | "forward" | "go", path?, n?}), theme(value), copy(text) -> Promise,
//   blocked() (true while the unlock sheet is open: the frame's questions wait),
//   notice({kind, text}) (default: a line of text above the frame), log(text), limits (overrides LIMITS),
//   tap(msg) (sees every message sent to the frame), onPort(port) (the host's end of the channel) and isActive() (replaces
//   navigator.userActivation.isActive, which a test driver's own scripts keep switching on): tests and debugging.
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
  if (!document.querySelector('link[href^="/static/css/frame.css"]')) document.head.append(el("link", { rel: "stylesheet", href: "/static/css/frame.css" }));
  const line = el("p", { class: "frame-notice", role: "status", hidden: true });
  const notice = opts.notice || ((n) => { line.textContent = n.text; line.hidden = !n.text; });   // text only, never markup
  const wrap = el("div", { class: "frame-host" }, line);
  opts.mount.append(wrap);

  let frame = null;
  let tok = null;          // the one-time token of the document being built; null once used
  let port = null;         // the host's end of the channel; null until the shim proved the token
  let seq = 0;
  let pingTimer = null;
  let missed = 0;          // pings in a row the shim has not answered
  let awaiting = false;    // a ping is out that the shim has not answered
  let lastPing = 0;
  let helloTimer = null;
  let graceTimer = null;
  let writeLoads = 0;
  let writeUntil = 0;
  let loadsExpected = 0;   // the load of the document this host asked for
  let flooding = false;
  let current = opts.start || "/";
  let destroyed = false;
  let stopped = false;
  let histBack = 0;        // history entries the frame pushed, and how many of them a Back has stepped over
  let histFwd = 0;
  let lastGesture = 0;
  const inflight = new Map();        // request id -> AbortController
  const streams = new Set();         // ids of inflight entries that are streams
  const rebuilds = [];
  let windowStart = 0;
  let windowCount = 0;
  let overSeconds = 0;

  // A user gesture is a real one in the TIX page or in a frame inside it (user activation reaches the parent), at most
  // one action per gestureGapMs. Without the API (an old browser) nothing that needs a gesture happens.
  function gesture() {
    const now = Date.now();
    const active = opts.isActive ? opts.isActive() : Boolean(navigator.userActivation && navigator.userActivation.isActive);
    if (!active || now - lastGesture < limits.gestureGapMs) return false;
    lastGesture = now;
    return true;
  }

  // ---- questions to the person ---------------------------------------------------------------------------
  // The frame can only ASK. A question shows the exact text (hidden and bidirectional characters made visible) in a
  // fixed box that does not move the frame, and its button works only for a real click on it, not at once, once.
  // There is no way to read the clipboard.
  let prompt = null;
  let lastPrompt = 0;
  function closePrompt(ok) {
    if (!prompt) return;
    clearTimeout(prompt.timer);
    clearTimeout(prompt.enable);
    prompt.node.remove();
    const { onClose } = prompt;
    prompt = null;
    if (onClose) onClose(ok === true);
  }
  // spec: {title, text, label, run: async () => void, onClose(ok)?}; false when another question is up or one came too soon
  function ask(spec) {
    const now = Date.now();
    if (prompt || now - lastPrompt < limits.promptGapMs || !frame || opts.blocked?.()) return false;    // blocked(): the unlock sheet is open, nothing stacks on it
    lastPrompt = now;
    const act = el("button", { type: "button", class: "btn", disabled: true, onclick: async (e) => {
      if (!e.isTrusted || Date.now() - prompt.shownAt < limits.promptDelayMs) return;
      let ok = false;
      try { await spec.run(); ok = true; } catch { /* reported by the callback's own failure */ }
      closePrompt(ok);
    } }, spec.label);
    const node = el("div", { class: "frame-prompt", role: "group", "aria-label": spec.title },
      el("p", {}, spec.title), el("pre", {}, shown(spec.text)),
      act, el("button", { type: "button", class: "btn", onclick: () => closePrompt(false) }, "Dismiss"));
    wrap.append(node);                                   // after the frame in the document, fixed on screen: the frame never moves
    prompt = { node, shownAt: now, onClose: spec.onClose,
      timer: setTimeout(() => closePrompt(false), limits.promptMs),
      enable: setTimeout(() => { act.disabled = false; }, limits.promptDelayMs) };
    return true;
  }

  const abortAll = () => { for (const ac of inflight.values()) ac.abort(); inflight.clear(); streams.clear(); };
  const clearTimers = () => {
    clearInterval(pingTimer); clearTimeout(helloTimer); clearTimeout(graceTimer);
    pingTimer = helloTimer = graceTimer = null;
  };

  function send(msg, transfer) {
    if (destroyed || !port) return;
    port.postMessage(msg, transfer || []);
    if (opts.tap) opts.tap(msg);
  }

  function build() {
    tok = randomToken();
    port = null;
    awaiting = false;
    missed = 0;
    writeLoads = 0;
    writeUntil = 0;
    loadsExpected = 1;
    histBack = histFwd = 0;
    windowStart = windowCount = overSeconds = 0;
    frame = el("iframe", { class: "frame-dash", sandbox: "allow-scripts", referrerpolicy: "no-referrer", loading: "eager",
      title: opts.title || "Dashboard", src: `/sandbox/dash?tok=${tok}` });
    frame.addEventListener("load", onLoad);
    wrap.insertBefore(frame, line.nextSibling);
    // no hello in time: a frame that never started its shim is not the one this host built
    helloTimer = setTimeout(() => rebuild("no hello"), limits.helloMs);
  }

  function rebuild(reason) {
    if (destroyed) return;
    clearTimers();
    abortAll();
    closePrompt(false);
    if (port) { port.onmessage = null; port.close(); port = null; }
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
    // A later load: the shim's own document write announces up to a few, anything else is the page leaving. The
    // announcement (a message) and the load are separate tasks and may arrive in either order, so an unannounced
    // load gets a short grace for the announcement, and without it the frame is rebuilt. (A foreign document can hold
    // its own load back; the heartbeat covers that.)
    if (takeWriteLoad()) return;
    clearTimeout(graceTimer);
    graceTimer = setTimeout(() => { graceTimer = null; if (!takeWriteLoad()) rebuild("unexpected load"); }, limits.graceMs);
  }

  function takeWriteLoad() {
    if (writeLoads > 0 && Date.now() < writeUntil) { writeLoads -= 1; return true; }
    return false;
  }

  function heartbeat() {
    if (awaiting && ++missed >= 2) { rebuild("no pong"); return; }
    awaiting = true;
    lastPing = ++seq;
    send({ t: "ping", n: lastPing });
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

  const nameOf = (view) => {
    const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(String(view.headers["content-disposition"] || ""));
    const fromPath = view.path.split("?")[0].split("/").pop();
    return (m ? decodeURIComponent(m[1]) : fromPath || "file").slice(0, 120);
  };
  // A download is offered to the person (name and size shown); the bytes are handed over on a click in the app.
  function offerDownload(view) {
    const ok = ask({ title: "The dashboard offers a download:", text: `${nameOf(view)} (${view.body.length} bytes)`, label: "Download",
      run: async () => { cb.download(view); } });
    if (!ok) notice({ kind: "error", text: "Another question is waiting; try the download again." });
  }

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
        if (kind === "error") notice({ kind: "error", text: `The dashboard answered ${Number(head.status) || "with an error"} for ${path.slice(0, 120)}.` });
        else if (req.intent === "page" && !gesture()) notice({ kind: "error", text: "Click the link to open that." });   // opening anything outside the frame needs a click
        else if (kind === "viewer") cb.viewer(view);
        else offerDownload(view);
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
      if (head.status === 200) offerDownload({ path: final, status: head.status, headers: head.headers || {}, body });
      else notice({ kind: "error", text: `The dashboard answered ${Number(head.status) || "with an error"} for ${final.slice(0, 120)}.` });
    } catch (e) {
      notice({ kind: "error", text: String(e?.message || "download failed").slice(0, 200) });
    } finally {
      inflight.delete(id);
    }
  }

  // An address outside the dashboard: shown to the person (as text) with a button; opened with noopener and noreferrer.
  function openOutside(m) {
    if (typeof m.href !== "string" || m.href.length > 2048) return;
    let url;
    try { url = new URL(m.href); } catch { return; }
    if (url.protocol !== "https:" && url.protocol !== "http:") return;
    if (!ask({ title: "The dashboard wants to open this address in a new tab:", text: url.href, label: "Open", run: async () => { cb.external(url.href); } })) {
      notice({ kind: "error", text: "Another question is waiting; try the link again." });
    }
  }

  // History the frame asks for stays inside the entries the frame itself pushed (the app keeps its own history).
  function history(op, path, n) {
    if (op === "replace" || (op === "push" && !gesture())) { current = path; cb.history({ op: "replace", path }); return; }   // no gesture: no new entry
    if (op === "push") { histBack += 1; histFwd = 0; current = path; cb.history({ op: "push", path }); return; }
    const k = op === "back" ? -1 : op === "forward" ? 1 : n;
    if (!Number.isSafeInteger(k) || k === 0 || (k < 0 ? -k > histBack : k > histFwd) || !gesture()) return;
    histBack += k; histFwd -= k;
    cb.history(op === "go" ? { op, n: k } : { op });
  }

  // ---- the one window message: hello ---------------------------------------------------------------------
  function onWindowMessage(event) {
    if (destroyed) { removeEventListener("message", onWindowMessage); return; }
    if (!frame || !frame.isConnected) return;
    if (!event.source || event.source !== frame.contentWindow) return;      // any other window (or none): not ours, not even counted
    if (event.origin !== "null") return;                    // a sandboxed document has the opaque origin
    if (!rateOk()) return;
    const m = event.data;
    if (!boundedShape(m, limits) || m.k !== PROTOCOL || m.t !== "hello") return;
    if (tok === null || port !== null || m.tok !== tok) return;             // one time: the token is spent by the first proof
    tok = null;
    clearTimeout(helloTimer);
    const channel = new MessageChannel();
    port = channel.port1;
    port.onmessage = onPortMessage;
    if (opts.onPort) opts.onPort(port);
    frame.contentWindow.postMessage({ k: PROTOCOL, t: "ready" }, "*", [channel.port2]);   // the only message that names no secret
    pingTimer = setInterval(heartbeat, limits.pingMs);
    send({ t: "go", path: current });
  }

  // ---- everything else: on the port ----------------------------------------------------------------------
  function onPortMessage(event) {
    if (destroyed || !port) return;
    if (!rateOk()) return;
    const m = event.data;
    if (!boundedShape(m, limits)) return;
    switch (m.t) {
      case "pong": if (m.n === lastPing) { awaiting = false; missed = 0; } return;
      case "write":
        abortAll();
        closePrompt(false);
        writeLoads = limits.writeLoads;
        writeUntil = Date.now() + limits.writeMs;
        if (graceTimer) { clearTimeout(graceTimer); graceTimer = null; takeWriteLoad(); }   // its load came first
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
        if (m.push === true) history("push", path); else current = path;
        return;
      }
      case "hist": {
        if (m.op === "push" || m.op === "replace") {
          const path = validPath(m.path);
          if (path) history(m.op, path);
        } else if (m.op === "back" || m.op === "forward" || m.op === "go") history(m.op, null, m.n);
        return;
      }
      case "theme": if (THEMES.includes(m.value)) cb.theme(m.value); return;
      case "copy": {
        if (!Number.isSafeInteger(m.id)) return;
        const id = m.id;
        if (typeof m.text !== "string" || m.text.length < 1 || m.text.length > limits.copy) return send({ t: "copied", id, ok: false });
        const asked = ask({ title: "The dashboard asks to copy this text:", text: m.text, label: "Copy",
          run: () => cb.copy(m.text), onClose: (ok) => send({ t: "copied", id, ok }) });
        if (!asked) send({ t: "copied", id, ok: false });
        return;
      }
      case "open": {
        if (!gesture()) return;
        if (m.path !== undefined) {
          const path = validPath(m.path);
          if (path && scopes.allows("GET", path)) handleRequest({ id: -(++seq), gen: 0, intent: "open", method: "GET", path, headers: {}, body: null });
        } else openOutside(m);
        return;
      }
      case "download": {
        if (!gesture()) return;
        const path = validPath(m.path);
        if (path && scopes.allows("GET", path)) handleDownload(path);
        return;
      }
      case "log": if (typeof m.m === "string") cb.log(m.m.slice(0, 200)); return;
      default:
    }
  }

  addEventListener("message", onWindowMessage);
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
      removeEventListener("message", onWindowMessage);
      closePrompt(false);
      if (port) { port.onmessage = null; port.close(); port = null; }
      frame?.remove();
      frame = null;
      wrap.remove();
    },
  };
}
