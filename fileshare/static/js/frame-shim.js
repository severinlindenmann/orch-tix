// fileshare/static/js/frame-shim.js — the shim inside the /sandbox/dash frame (docs/bridge-frame.md).
// A CLASSIC script that the server puts INLINE into the frame document with that load's nonce (routes/pages.py
// sandbox_dash): the frame's policy has no 'self' and no 'unsafe-inline' for scripts, so this is the only code that
// runs until it re-creates the dashboard page's own scripts with the same nonce. Nothing here may contain a closing
// script tag. The frame has an opaque origin and no network: everything the dashboard asks of the network (fetch,
// XMLHttpRequest, EventSource, link clicks, form submits) becomes a message to the TIX page (js/frame-host.js), which
// validates it and hands it to the transport. The shim is the polite path only; the host trusts nothing it says.
//
// Channel. The one message that goes through window.postMessage is hello {k, t: "hello", tok}; the host answers
// ready with ONE MessagePort and everything else, both ways, travels on that port: a document that replaces this one
// (a navigation the shim did not see) never holds the port, so it can neither speak to the host nor listen. The port
// and every built-in that touches it are captured below, before any page script exists, and used through the captured
// Reflect.apply, so a page that poisons prototypes (Function.prototype.call, MessageEvent.prototype.data, Object.assign,
// Array and Promise methods) never sees the port or a secret. Messages are built as object literals.
// Messages on the port: {t, ...}. To the host: pong {n}, req, sopen, sclose, abort, write, rendered, hist, theme, copy,
// open, download, log. From the host: ping {n}, go {path}, res, page, handled, err, sdata, send, copied.
// docs/bridge-frame.md has the fields.
(() => {
  "use strict";
  const K = "orch-frame-1";
  const NONCE = document.currentScript ? document.currentScript.nonce : "";
  const root = document.documentElement;
  const TOK = root.getAttribute("data-tok") || "";
  root.removeAttribute("data-tok");                  // the page that runs later must not find it
  const ORIGIN = location.origin;                    // the TIX origin the frame's URL is on; the document's own origin is opaque
  const parentWin = window.parent;
  const apply = Reflect.apply;
  const winPost = parentWin.postMessage;
  const portPost = MessagePort.prototype.postMessage;
  const portDesc = (proto, name) => Object.getOwnPropertyDescriptor(proto, name);
  const evData = portDesc(MessageEvent.prototype, "data").get;
  const evPorts = portDesc(MessageEvent.prototype, "ports").get;
  const evSource = portDesc(MessageEvent.prototype, "source").get;
  const evOrigin = portDesc(MessageEvent.prototype, "origin").get;
  const portSetOnMessage = portDesc(MessagePort.prototype, "onmessage").set;
  let port = null;                                   // the one MessagePort; never put on any object the page can reach
  const NativeRequest = Request, NativeResponse = Response, NativeURL = URL, NativeFormData = FormData;
  const CACHE_ENTRIES = 128, CACHE_BYTES = 24 * 1024 * 1024;

  let gen = 0;                                       // the page generation: bumped by every document write
  let seq = 0;
  let navSeq = 0;                                    // the newest navigation; an older answer is dropped
  let current = "/";                                 // the page shown, "/path?query#hash" (the address the dashboard sees)
  const pending = new Map();                         // request id -> {resolve, reject, gen}
  const streams = new Map();                         // stream id -> EventSource
  const copies = new Map();

  const post = (m, transfer) => { if (port !== null) apply(portPost, port, [m, transfer || []]); };
  const log = (m) => { try { post({ t: "log", m: String(m).slice(0, 200) }); } catch (e) { /* nothing to tell */ } };
  const stale = () => new TypeError("the page changed");
  const aborted = () => new DOMException("aborted", "AbortError");

  // ---- addresses ---------------------------------------------------------------------------------------------
  const base = () => ORIGIN + current.split("#")[0];
  const resolveUrl = (href) => {
    if (/[\u0000-\u001f\u007f]/.test(String(href))) return null;
    try { return new NativeURL(String(href), base()); } catch (e) { return null; }
  };
  // "/path?query" of an address on the dashboard, or null (another origin, another scheme)
  const toPath = (href, from) => {
    let u;
    try { u = new NativeURL(String(href), from || base()); } catch (e) { return null; }
    return u.origin === ORIGIN ? u.pathname + u.search : null;
  };

  // ---- requests to the TIX page ------------------------------------------------------------------------------
  function call(intent, method, path, headers, body) {
    let id = 0;
    const p = new Promise((resolve, reject) => {
      if (port === null) { reject(new TypeError("the frame is not connected")); return; }
      id = ++seq;
      pending.set(id, { resolve, reject, gen });
      if (body) post({ t: "req", id, gen, intent, method, path, headers: headers || {}, body }, [body]);
      else post({ t: "req", id, gen, intent, method, path, headers: headers || {} });
    });
    p.id = id;
    return p;
  }
  function cancel(id) {
    const e = pending.get(id);
    if (!e) return;
    pending.delete(id);
    post({ t: "abort", id });
    e.reject(aborted());
  }

  // ---- fetch, XMLHttpRequest, EventSource --------------------------------------------------------------------
  const NULL_BODY = [101, 204, 205, 304];
  const toResponse = (r) => {
    const res = new NativeResponse(NULL_BODY.includes(r.status) ? null : r.body, { status: r.status, headers: r.headers });
    Object.defineProperty(res, "url", { value: ORIGIN + r.url });
    return res;
  };
  window.fetch = async function fetch(input, init) {
    const req = new NativeRequest(typeof input === "string" || input instanceof NativeURL ? new NativeURL(String(input), base()).href : input, init);
    const path = toPath(req.url);
    if (path === null) throw new TypeError("blocked: only the dashboard can be reached");
    if (req.signal.aborted) throw aborted();
    const body = req.method === "GET" || req.method === "HEAD" ? null : await req.arrayBuffer();
    const headers = {};
    req.headers.forEach((v, k) => { headers[k] = v; });
    const p = call("fetch", req.method, path, headers, body);
    req.signal.addEventListener("abort", () => cancel(p.id), { once: true });
    const r = await p;
    const res = toResponse(r);
    Object.defineProperty(res, "redirected", { value: r.url !== path });
    return res;
  };

  class XHR extends EventTarget {
    constructor() { super(); this.readyState = 0; this.status = 0; this.statusText = ""; this.responseType = ""; this.response = null; this.responseText = ""; this.responseURL = ""; this.timeout = 0; this.withCredentials = false; this._h = {}; this._res = null; }
    open(method, url) { this._m = String(method).toUpperCase(); this._u = String(url); this._set(1); }
    setRequestHeader(k, v) { this._h[String(k).toLowerCase()] = String(v); }
    overrideMimeType() {}
    getResponseHeader(k) { return this._res ? (this._res.headers[String(k).toLowerCase()] ?? null) : null; }
    getAllResponseHeaders() { return this._res ? Object.entries(this._res.headers).map(([k, v]) => `${k}: ${v}\r\n`).join("") : ""; }
    abort() { if (this._id) cancel(this._id); this._id = 0; this._fire("abort"); }
    send(body) {
      const path = toPath(this._u);
      if (path === null) { queueMicrotask(() => { this._fire("error"); }); return; }
      const data = body == null ? null : new NativeRequest(ORIGIN + "/", { method: "POST", body }).arrayBuffer();
      Promise.resolve(data).then((buf) => {
        const p = call("fetch", this._m, path, this._h, this._m === "GET" || this._m === "HEAD" ? null : buf);
        this._id = p.id;
        return p;
      }).then((r) => {
        this._res = r; this.status = r.status; this.responseURL = ORIGIN + r.url; this._set(2); this._set(3);
        if (this.responseType === "arraybuffer") this.response = r.body;
        else {
          this.responseText = new TextDecoder().decode(r.body);
          this.response = this.responseType === "json" ? (() => { try { return JSON.parse(this.responseText); } catch (e) { return null; } })() : this.responseText;
        }
        this._set(4); this._fire("load"); this._fire("loadend");
      }, () => { this._fire("error"); this._fire("loadend"); });
    }
    _set(n) { this.readyState = n; this._fire("readystatechange"); }
    _fire(type) { const e = new Event(type); const h = this["on" + type]; if (typeof h === "function") h.call(this, e); this.dispatchEvent(e); }
  }
  window.XMLHttpRequest = XHR;

  class DashEventSource extends EventTarget {
    constructor(url) {
      super();
      const path = toPath(url);
      this.url = path === null ? String(url) : ORIGIN + path;
      this.withCredentials = false;
      this.readyState = 0;
      this._buf = ""; this._type = ""; this._data = [];
      if (path === null || port === null) { this.readyState = 2; queueMicrotask(() => this._fire("error")); return; }
      this._id = ++seq;
      streams.set(this._id, this);
      post({ t: "sopen", id: this._id, gen, path });
    }
    close() { if (this.readyState !== 2) { this.readyState = 2; if (streams.delete(this._id)) post({ t: "sclose", id: this._id }); } }
    _feed(chunk) {
      this._buf += chunk;
      let i;
      while ((i = this._buf.search(/\r\n|\n|\r/)) >= 0) {
        const line = this._buf.slice(0, i);
        this._buf = this._buf.slice(i + (this._buf.startsWith("\r\n", i) ? 2 : 1));
        if (line === "") {
          if (this._data.length) this._fire(this._type || "message", new MessageEvent(this._type || "message", { data: this._data.join("\n"), origin: ORIGIN }));
          this._type = ""; this._data = [];
        } else if (line.startsWith("event:")) this._type = line.slice(6).trim();
        else if (line.startsWith("data:")) this._data.push(line.slice(5).replace(/^ /, ""));
      }
    }
    _fire(type, event) {
      const e = event || new Event(type);
      const h = this["on" + type];
      if (typeof h === "function") h.call(this, e);
      this.dispatchEvent(e);
    }
  }
  Object.assign(DashEventSource, { CONNECTING: 0, OPEN: 1, CLOSED: 2 });
  window.EventSource = DashEventSource;
  const closeStreams = () => { for (const s of [...streams.values()]) s.close(); streams.clear(); };

  window.open = (href) => { post({ t: "open", href: String(href) }); return null; };

  // ---- cookie and storage: an opaque origin throws on all of them; the page gets an in-memory stand-in ----------
  const memStore = () => {
    const m = new Map();
    return { getItem: (k) => (m.has(String(k)) ? m.get(String(k)) : null), setItem: (k, v) => { m.set(String(k), String(v)); },
      removeItem: (k) => { m.delete(String(k)); }, clear: () => m.clear(), key: (i) => [...m.keys()][i] ?? null, get length() { return m.size; } };
  };
  for (const name of ["localStorage", "sessionStorage"]) {
    try { const s = memStore(); Object.defineProperty(window, name, { configurable: true, get: () => s }); } catch (e) { log(`${name}: ${e.name}`); }
  }
  try {
    const jar = new Map();
    Object.defineProperty(Document.prototype, "cookie", { configurable: true,
      get: () => [...jar].map(([k, v]) => `${k}=${v}`).join("; "),
      set: (v) => { const [pair] = String(v).split(";"); const i = pair.indexOf("="); if (i > 0) jar.set(pair.slice(0, i).trim(), pair.slice(i + 1).trim()); } });
  } catch (e) { log(`cookie: ${e.name}`); }

  // ---- window.orchHost: the adapter the dashboard asks (orch-core docs/dashboard-frame.md) --------------------
  const part = (re) => { const m = re.exec(current); return m ? m[0] : ""; };
  const setCurrent = (url) => {
    const u = resolveUrl(url);
    if (u && u.origin === ORIGIN) current = u.pathname + u.search + u.hash;
  };
  const storeOf = () => {
    const m = new Map();
    return { get: (k) => (m.has(k) ? m.get(k) : null), set: (k, v) => { m.set(k, String(v)); }, remove: (k) => { m.delete(k); } };
  };
  const XLINK = "http://www.w3.org/1999/xlink";
  // the address of an anchor in any namespace: <a href>, SVG <a xlink:href>, or an animated SVG href
  const linkHref = (a) => {
    const v = a.getAttribute("href") ?? a.getAttributeNS(XLINK, "href") ?? (a.href && typeof a.href === "object" ? a.href.animVal : null);
    return typeof v === "string" ? v : "";
  };
  function linkAction(a, out) {
    const abs = resolveUrl(linkHref(a));
    if (!abs) return;
    if (abs.origin !== ORIGIN) { if (abs.protocol === "https:" || abs.protocol === "http:") post({ t: "open", href: abs.href }); return; }
    const path = abs.pathname + abs.search;
    if (a.hasAttribute("data-orch-viewer") || (out && !a.hasAttribute("download"))) post({ t: "open", path });
    else if (a.hasAttribute("download")) post({ t: "download", path });
    else if (path === current.split("#")[0] && abs.hash) { const t = document.getElementById(decodeURIComponent(abs.hash.slice(1))); if (t) t.scrollIntoView(); setCurrent(path + abs.hash); }
    else navigate("GET", path + abs.hash, null, null, true);
  }
  // (an SVG anchor's .target is an object, so the attribute is read)
  const outClick = (e, a) => { const t = a.getAttribute("target"); return Boolean((t && t !== "_self") || e.ctrlKey || e.metaKey || e.shiftKey); };
  window.orchHost = {
    path: () => part(/^[^?#]*/),
    search: () => part(/\?[^#]*/),
    url: () => current.split("#")[0],
    hash: () => part(/#.*$/),
    resolve(href) {
      const u = resolveUrl(href);
      return u ? { href: u.href, path: u.pathname, search: u.search, hash: u.hash, internal: u.origin === ORIGIN } : null;
    },
    navigate(href) {
      const h = String(href);
      if (/[\u0000-\u001f\u007f]/.test(h) || /^[/\\]{2}/.test(h) || /^\/[\\]/.test(h)) return;
      const u = resolveUrl(h);
      if (u && u.origin === ORIGIN) navigate("GET", u.pathname + u.search + u.hash, null, null, true);
    },
    reload() { navigate("GET", current, null, null, false); },
    pageHistory: {
      canPush: () => true,
      push(url) { setCurrent(url); post({ t: "hist", op: "push", path: current.split("#")[0] }); },
      replace(url) { setCurrent(url); post({ t: "hist", op: "replace", path: current.split("#")[0] }); },
      current: () => current,
    },
    setTheme(value) { post({ t: "theme", value: String(value) }); },
    session: Object.freeze(storeOf()),
    local: Object.freeze(storeOf()),
    copy(text) {
      return new Promise((resolve, reject) => {
        const id = ++seq;
        const timer = setTimeout(() => { copies.delete(id); reject(new Error("no answer")); }, 40000);   // the TIX page asks the person first (a real click there)
        copies.set(id, (ok) => { clearTimeout(timer); (ok ? resolve : reject)(ok ? undefined : new Error("not copied")); });
        post({ t: "copy", id, text: String(text) });
      });
    },
    openLink(a) { if (!a || !a.getAttribute || !linkHref(a)) return false; linkAction(a, true); return true; },
    download(a) { if (!a || !a.getAttribute || !linkHref(a)) return false; linkAction(a, true); return true; },
  };
  Object.freeze(window.orchHost.pageHistory);
  Object.freeze(window.orchHost);                    // the dashboard copies the methods it needs; the page cannot swap them

  // ---- subresources ------------------------------------------------------------------------------------------
  const cache = new Map();                           // path -> Promise<{status, headers, body}>; assets only, never pages
  const blobs = new Map();                           // path -> Promise<blob: url>
  let cacheBytes = 0;
  const load = (path) => {
    if (!cache.has(path)) {
      const p = call("asset", "GET", path).then((r) => {
        if (r.status !== 200) throw new Error(`${path} ${r.status}`);
        cacheBytes += r.body.byteLength;
        return r;
      });
      cache.set(path, p);
      p.catch(() => cache.delete(path));
      while (cache.size > CACHE_ENTRIES || cacheBytes > CACHE_BYTES) {
        const [oldest] = cache.keys();
        if (oldest === path) break;
        cache.get(oldest).then((r) => { cacheBytes -= r.body.byteLength; }, () => {});
        cache.delete(oldest);
        blobs.get(oldest)?.then((u) => URL.revokeObjectURL(u), () => {});
        blobs.delete(oldest);
      }
    }
    return cache.get(path);
  };
  const blobUrl = (path) => {
    if (!blobs.has(path)) blobs.set(path, load(path).then((r) => URL.createObjectURL(new Blob([r.body], { type: r.headers["content-type"] || "" }))));
    return blobs.get(path);
  };
  const decode = (r) => new TextDecoder().decode(r.body);
  const hex = (buf) => Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, "0")).join("");
  // A script runs only when (1) its path is under the dashboard's own static files (a constant here, never taken from the
  // page), (2) the answer is a JavaScript type (the shim fetched the bytes, so it does the nosniff check), and (3) the
  // file's SHA-256 starts with the ?v= of its URL. (3) is cache busting and a check against an accidental mismatch only:
  // the page writes both the URL and the stamp, so against hostile markup it adds nothing; (1) and (2) are what keep
  // an artifact or any other file from running as a script.
  const SCRIPT_PREFIXES = ["/static/"];
  const JS_TYPE = /^(text|application)\/(x-)?(java|ecma)script$/;
  const scriptAllowed = (path, r) => {
    const pathname = path.split("?")[0];
    const type = String((r && r.headers && r.headers["content-type"]) || "").split(";")[0].trim().toLowerCase();
    return (!r || JS_TYPE.test(type)) && SCRIPT_PREFIXES.some((x) => pathname.startsWith(x)) && !pathname.includes("..");
  };
  async function pinned(path, r) {
    const v = new NativeURL(path, ORIGIN).searchParams.get("v") || "";
    if (!/^[0-9a-f]{12,64}$/.test(v) || !crypto.subtle) return false;
    return hex(await crypto.subtle.digest("SHA-256", r.body)).startsWith(v);
  }
  async function cssWithBlobs(css, pagePath) {
    css = css.replace(/@import[^;]*;/gi, "");
    const re = /url\(\s*(['"]?)([^'")]+)\1\s*\)/g;
    const map = new Map();
    await Promise.all([...new Set([...css.matchAll(re)].map((m) => m[2]).filter((u) => !/^(data|blob):/i.test(u)))].map(async (u) => {
      const p = toPath(u, ORIGIN + pagePath);
      if (p !== null) { try { map.set(u, await blobUrl(p)); } catch (e) { /* the reference stays and is blocked */ } }
    }));
    return css.replace(re, (m, q, u) => (map.has(u) ? `url("${map.get(u)}")` : m));
  }

  // ---- writing a page ----------------------------------------------------------------------------------------
  // The page is changed as a DOM and moved into the live document with importNode: it is never serialised and parsed a
  // second time, so the parser that cleaned it and the parser that renders it cannot disagree (mutation XSS). Every
  // element, in any namespace (svg and math scripts too) and inside templates, loses scripts, handlers, javascript: and
  // similar URLs, srcdoc and any nonce attribute.
  const URL_ATTRS = /^(href|src|action|formaction|xlink:href|data|poster|background|ping|srcset|srcdoc|nonce)$/i;
  const BAD_URL = /^(javascript|vbscript|data:text\/html|data:application\/xhtml)/i;
  const HTML_NS = "http://www.w3.org/1999/xhtml";
  function sanitize(root) {
    // scripts, and the SMIL elements that could set an address later (animate href to another origin)
    root.querySelectorAll("script,noscript,animate,set,animateMotion,animateTransform,animateColor,discard").forEach((n) => n.remove());
    root.querySelectorAll("*").forEach((n) => {
      for (const a of [...n.attributes]) {
        const squashed = a.value.replace(/[\u0000-\u0020\u007f-\u009f]/g, "");
        if (/^on/i.test(a.name) || /^(srcdoc|ping|nonce)$/i.test(a.name) || (URL_ATTRS.test(a.name) && BAD_URL.test(squashed))) { n.removeAttribute(a.name); continue; }
        // an SVG or MathML element (a link, a use, an image, an anchor in a foreignObject) points only into this page or this
        // origin: any other address is dropped. HTML links keep theirs; the shim opens those through the TIX page.
        if (n.namespaceURI !== HTML_NS && /^(xlink:)?href$/i.test(a.name) && !a.value.startsWith("#")) {
          let same = false;
          try { same = new NativeURL(a.value, ORIGIN + "/").origin === ORIGIN && !/^\s*\/\//.test(a.value); } catch (e) { /* not an address */ }
          if (!same || n.localName === "use") n.removeAttribute(a.name);
        }
      }
      if (n.localName === "template" && n.content) sanitize(n.content);
    });
  }

  async function render(html, pagePath, hash, push, mine) {
    const pageBase = ORIGIN + pagePath;
    const doc = new DOMParser().parseFromString(html, "text/html");      // inert: nothing runs, nothing loads
    const jobs = [];
    const scripts = [];
    doc.querySelectorAll("base,object,embed,applet,frame,frameset,meta[http-equiv]").forEach((n) => n.remove());
    // links: an allow-list. Only a stylesheet stays (it becomes a style below); dns-prefetch, preconnect, prerender, next and
    // the rest would reach out.
    doc.querySelectorAll("link").forEach((l) => { if (!l.relList.contains("stylesheet")) l.remove(); });
    // a nested frame (an artifact, a widget) cannot run here: a link that opens it in the TIX viewer takes its place
    doc.querySelectorAll("iframe").forEach((f) => {
      const p = toPath(f.getAttribute("src") || "", pageBase);
      if (p === null) { f.remove(); return; }
      const a = doc.createElement("a");
      a.setAttribute("href", p);
      a.setAttribute("data-orch-viewer", "");
      a.className = "orch-viewer-link";
      a.textContent = `Open ${(f.getAttribute("title") || "this file").slice(0, 120)} in the viewer`;
      f.replaceWith(a);
    });
    doc.querySelectorAll("script").forEach((s) => {
      const p = s.hasAttribute("src") ? toPath(s.getAttribute("src"), pageBase) : null;
      const type = (s.getAttribute("type") || "").toLowerCase();
      s.remove();
      if (p === null || (type && type !== "text/javascript" && type !== "application/javascript")) return;   // other scripts, inline ones, modules: gone
      if (!scriptAllowed(p, null)) { log(`script not run (path): ${p}`); return; }
      scripts.push(load(p).then(async (r) => {
        if (!scriptAllowed(p, r)) return (log(`script not run (type): ${p}`), null);
        return (await pinned(p, r)) ? decode(r) : (log(`script not run (version): ${p}`), null);
      }).catch((e) => (log(`script: ${e.message}`), null)));
    });
    doc.querySelectorAll("link[rel~=stylesheet]").forEach((l) => {
      const p = toPath(l.getAttribute("href") || "", pageBase);
      const st = doc.createElement("style");
      l.replaceWith(st);
      if (p === null) { st.remove(); return; }
      jobs.push(load(p).then(async (r) => { st.textContent = await cssWithBlobs(decode(r), p); }, () => st.remove()));
    });
    doc.querySelectorAll("style").forEach((st) => { if (st.textContent) jobs.push(cssWithBlobs(st.textContent, pagePath).then((t) => { st.textContent = t; })); });
    doc.querySelectorAll("img,source,video,audio").forEach((n) => {
      n.removeAttribute("srcset");
      for (const attr of ["src", "poster"]) {
        if (!n.hasAttribute(attr)) continue;
        const v = n.getAttribute(attr);
        if (/^(data|blob):/i.test(v)) continue;
        const p = toPath(v, pageBase);
        if (p === null) n.removeAttribute(attr);
        else jobs.push(blobUrl(p).then((u) => n.setAttribute(attr, u), () => n.removeAttribute(attr)));
      }
    });
    sanitize(doc);   // after the scripts were collected (they are re-created below), before anything is awaited
    await Promise.all(jobs);
    const code = await Promise.all(scripts);
    if (mine !== navSeq) return;                                           // a newer navigation took over meanwhile
    post({ t: "write" });                                                  // the host cancels everything still running
    closeStreams();                                                        // a document write keeps the window: its streams must not survive
    for (const [, e] of pending) e.reject(stale());
    pending.clear();
    gen += 1;
    current = pagePath + hash;
    document.open();
    arm();
    document.write("<!doctype html><html><head></head><body></body></html>");   // a fixed skeleton; the page is moved in as nodes
    document.replaceChild(document.importNode(doc.documentElement, true), document.documentElement);
    for (const text of code) {                                             // after the page exists, before it is closed, in order
      if (text === null || !document.body) continue;
      const s = document.createElement("script");
      s.setAttribute("nonce", NONCE);
      s.textContent = text;
      document.body.appendChild(s);                                        // an inline script runs as it is inserted
      s.remove();                                                          // and leaves nothing behind that carries the nonce
    }
    document.close();
    watch();
    post({ t: "rendered", path: current.split("#")[0], push, title: String(document.title).slice(0, 200) });
  }

  async function navigate(method, target, body, headers, push) {
    const mine = ++navSeq;
    const hash = target.includes("#") ? target.slice(target.indexOf("#")) : "";
    const path = target.split("#")[0];
    let r;
    try { r = await call("page", method, path, headers, body); } catch (e) { return; }
    if (mine !== navSeq || r.t !== "page") return;
    try {
      await render(r.html, r.path, hash, push, mine);
    } catch (e) { log(`render: ${e.message}`); }
  }

  // ---- what the page does, re-armed after every document write ---------------------------------------------
  function onClick(e) {
    if (e.defaultPrevented) return;
    const b = e.target && e.target.closest ? e.target.closest("button, input") : null;
    if (b && b.form && !b.disabled && (b.type === "submit" || b.type === "image")) ensureSubmit(b.form, b);
    // any anchor on the way up, whatever its namespace or attributes (HTML a, area, SVG a with href or xlink:href)
    let a = null;
    for (const n of e.composedPath()) { if (n.localName === "a" || n.localName === "area") { a = n; break; } }
    if (!a) return;
    const href = linkHref(a);
    if (href.startsWith("#")) return;
    e.preventDefault();                                                    // the frame never navigates itself
    if (href) linkAction(a, outClick(e, a));
  }
  function submitForm(form, submitter) {
    let fd;
    try { fd = new NativeFormData(form, submitter || undefined); } catch (e) { fd = new NativeFormData(form); }
    const method = String((submitter && submitter.formMethod) || form.method || "get").toUpperCase();
    const action = (submitter && submitter.getAttribute("formaction")) || form.getAttribute("action") || current.split("#")[0];
    const u = resolveUrl(action);
    if (!u || u.origin !== ORIGIN) return;
    if (method === "GET") {
      u.search = "";
      for (const [k, v] of fd) if (typeof v === "string") u.searchParams.append(k, v);
      navigate("GET", u.pathname + u.search, null, null, true);
      return;
    }
    const enctype = String((submitter && submitter.formEnctype) || form.enctype || "");
    const probe = new NativeRequest(ORIGIN + "/", { method: "POST", body: enctype === "multipart/form-data" ? fd : new URLSearchParams([...fd].filter(([, v]) => typeof v === "string")) });
    probe.arrayBuffer().then((buf) => navigate("POST", u.pathname + u.search, buf, { "content-type": probe.headers.get("content-type") }, true));
  }
  // The frame's policy has no allow-forms, so the browser refuses a form submission before it fires any submit event.
  // The shim fires it itself for a submit button's click and for Enter in a field, unless the browser did (a browser
  // that fires it anyway must not submit twice): the page's own submit handlers then see it as they always did.
  let submits = 0;
  const claimed = new WeakSet();
  function fireSubmit(form, submitter) {
    if (!form.isConnected) return;
    const skip = submitter && submitter.hasAttribute("formnovalidate");
    if (!skip && !form.checkValidity()) { form.reportValidity(); return; }
    form.dispatchEvent(new SubmitEvent("submit", { bubbles: true, cancelable: true, submitter: submitter || null }));
  }
  function ensureSubmit(form, submitter) {
    if (claimed.has(form)) return;
    claimed.add(form);
    const before = submits;
    setTimeout(() => { claimed.delete(form); if (submits === before) fireSubmit(form, submitter); }, 0);
  }
  const TEXTLIKE = /^(text|search|email|url|tel|password|number|date|datetime-local|month|week|time)$/;
  function onKeydown(e) {
    const t = e.target;
    if (e.key !== "Enter" || e.defaultPrevented || e.isComposing || !t || t.tagName !== "INPUT" || !t.form || !TEXTLIKE.test(t.type)) return;
    if (!t.form.querySelector("button:not([type]), button[type=submit], input[type=submit], input[type=image]")) ensureSubmit(t.form, null);
  }
  HTMLFormElement.prototype.requestSubmit = function requestSubmit(submitter) { fireSubmit(this, submitter || null); };
  function onSubmit(e) {
    if (e.defaultPrevented) return;
    e.preventDefault();
    submitForm(e.target, e.submitter);
  }
  HTMLFormElement.prototype.submit = function submit() { submitForm(this, null); };
  const onError = (e) => log(`${e.message} (${String(e.filename || "").slice(-30)}:${e.lineno})`);
  const imgs = new MutationObserver((records) => records.forEach((r) => r.addedNodes.forEach((n) => {
    if (!n.querySelectorAll) return;
    (n.matches && n.matches("img[src]") ? [n] : []).concat([...n.querySelectorAll("img[src]")]).forEach((im) => {
      const v = im.getAttribute("src");
      const p = /^(data|blob):/i.test(v) ? null : toPath(v);
      if (p !== null) blobUrl(p).then((u) => im.setAttribute("src", u), () => im.removeAttribute("src"));
    });
  })));
  function arm() {
    window.addEventListener("click", onClick, false);
    window.addEventListener("auxclick", onClick, false);
    window.addEventListener("submit", onSubmit, false);
    window.addEventListener("submit", () => { submits += 1; }, true);
    window.addEventListener("keydown", onKeydown, false);
    window.addEventListener("error", onError, true);
  }
  function watch() { imgs.disconnect(); if (document.documentElement) imgs.observe(document.documentElement, { childList: true, subtree: true }); }

  // ---- from the TIX page -----------------------------------------------------------------------------------
  // Before the port exists: one window message, the host's ready, from the parent, carrying the port.
  function onReady(e) {
    if (port !== null || apply(evSource, e, []) !== parentWin || apply(evOrigin, e, []) !== ORIGIN) return;
    const m = apply(evData, e, []);
    const ports = apply(evPorts, e, []);
    if (m === null || typeof m !== "object" || m.k !== K || m.t !== "ready" || ports.length !== 1) return;
    port = ports[0];
    apply(portSetOnMessage, port, [onPort]);
    window.removeEventListener("message", onReady, false);
  }
  function onPort(e) {
    const m = apply(evData, e, []);
    if (m === null || typeof m !== "object") return;
    if (m.t === "ping") { post({ t: "pong", n: m.n }); return; }
    if (m.t === "go") { if (typeof m.path === "string") navigate("GET", m.path, null, null, false); return; }
    if (m.t === "copied") { const f = copies.get(m.id); copies.delete(m.id); if (f) f(m.ok === true); return; }
    if (m.t === "sdata" || m.t === "send") {
      const s = streams.get(m.id);
      if (!s || m.gen !== gen) return;
      if (m.t === "send") { s.readyState = 2; streams.delete(m.id); s._fire("error"); return; }
      if (s.readyState === 0) { s.readyState = 1; s._fire("open"); }
      if (m.chunk) s._feed(String(m.chunk));
      return;
    }
    const p = pending.get(m.id);
    if (!p) return;
    pending.delete(m.id);
    if (m.gen !== p.gen || p.gen !== gen) { p.reject(stale()); return; }  // an answer for an older page
    if (m.t === "err") p.reject(new TypeError(String(m.message || "request failed")));
    else p.resolve(m);
  }

  arm();
  window.addEventListener("message", onReady, false);
  apply(winPost, parentWin, [{ k: K, t: "hello", tok: TOK }, ORIGIN]);
})();
