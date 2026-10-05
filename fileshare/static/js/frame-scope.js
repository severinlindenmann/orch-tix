// fileshare/static/js/frame-scope.js — what the TIX app accepts from the dashboard frame (docs/bridge-frame.md).
// Pure (no DOM): every message the frame posts is checked here before it reaches the transport. The frame is
// not trusted: its shim is only the polite path, any code in that window can post anything, so shape, size,
// path and method are validated here, and the injected scope table (advisory: the host decides again from its own
// router) says which method and path a paired device may even ask for.

export const PROTOCOL = "orch-frame-1";
export const PATH_MAX = 2048;
export const BODY_MAX = 1024 * 1024;
export const METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"];
export const INTENTS = ["page", "fetch", "asset", "open"];
// Request headers that may leave the frame (the bridge never carries Cookie, Origin or Host; the host sets its own).
export const REQUEST_HEADERS = ["accept", "content-type", "if-none-match"];
const HEADER_VALUE_MAX = 512;
const HEADER_COUNT_MAX = 16;

// A path as the bridge sends it: one slash, then printable ASCII only (everything else percent-encoded), no
// backslash, no "#", no dot segments (also encoded), no encoded slash, backslash or NUL. Returns the path or null.
export function validPath(value) {
  if (typeof value !== "string" || value.length > PATH_MAX || value.length < 1) return null;
  if (!/^\/(?![/\\])[\x21-\x7e]*$/.test(value) || /[\\#]/.test(value)) return null;
  const q = value.indexOf("?");
  const pathPart = q < 0 ? value : value.slice(0, q);
  // one canonical spelling: no empty segment, and no percent-escape of a character that needs none (/%61/x is /a/x)
  if (pathPart.includes("//") || /%(3\d|4[1-9A-F]|5[0-9A]|6[1-9A-F]|7[0-9A]|2[DE]|5F|7E)/i.test(pathPart)) return null;
  for (const raw of pathPart.split("/").slice(1)) {
    let seg;
    try { seg = decodeURIComponent(raw); } catch { return null; }
    if (seg === "." || seg === ".." || /[/\\\u0000-\u001f\u007f]/.test(seg)) return null;
  }
  return value;
}

// Pattern syntax: literal segments, ":name" for exactly one segment, a trailing "*" for the rest (one or more).
function compilePattern(pattern) {
  const segs = pattern.split("/").slice(1);
  const rest = segs[segs.length - 1] === "*";
  const fixed = rest ? segs.slice(0, -1) : segs;
  return (pathname) => {
    const parts = pathname.split("/").slice(1);
    if (rest ? parts.length <= fixed.length : parts.length !== fixed.length) return false;
    return fixed.every((s, i) => (s[0] === ":" ? parts[i] !== "" : s === parts[i]));
  };
}

export const pathnameOf = (path) => { const q = path.indexOf("?"); return q < 0 ? path : path.slice(0, q); };

// table: {rules: [{methods: ["GET"], pattern: "/t/:ref", stream?: true}, ...]}. A request is in scope when some rule
// names its method and matches its path; a stream (EventSource) needs a rule with stream: true.
export function compileScopes(table) {
  const rules = (table && Array.isArray(table.rules) ? table.rules : []).map((r) => ({
    methods: new Set((r.methods || []).map((m) => String(m).toUpperCase())),
    match: compilePattern(String(r.pattern)),
    stream: r.stream === true,
  }));
  return {
    allows(method, path, { stream = false } = {}) {
      const p = validPath(path);
      if (!p || !METHODS.includes(method)) return false;
      const name = pathnameOf(p);
      return rules.some((r) => r.methods.has(method) && (!stream || r.stream) && r.match(name));
    },
  };
}

const isPlain = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const isId = (v) => Number.isSafeInteger(v) && v > 0;

function cleanHeaders(headers) {
  const out = {};
  if (!isPlain(headers)) return out;
  const names = Object.keys(headers);
  if (names.length > HEADER_COUNT_MAX) return null;
  for (const name of names) {
    const n = name.toLowerCase();
    const v = headers[name];
    if (!REQUEST_HEADERS.includes(n)) continue;
    if (typeof v !== "string" || v.length > HEADER_VALUE_MAX || /[\u0000-\u001f\u007f]/.test(v)) return null;
    out[n] = v;
  }
  return out;
}

// A request message {t: "req", id, gen, intent, method, path, headers, body} or a stream {t: "sopen", id, gen, path}.
// Returns {ok: true, req: {...}} or {ok: false, code}. Codes: "shape", "path", "method", "scope", "size".
export function checkRequest(msg, scopes, { bodyMax = BODY_MAX } = {}) {
  if (!isPlain(msg) || !isId(msg.id) || !Number.isSafeInteger(msg.gen) || msg.gen < 0) return { ok: false, code: "shape" };
  const stream = msg.t === "sopen";
  const intent = stream ? "fetch" : msg.intent;
  const method = stream ? "GET" : msg.method;
  if (!INTENTS.includes(intent) || typeof method !== "string") return { ok: false, code: "shape" };
  if (!METHODS.includes(method)) return { ok: false, code: "method" };
  const path = validPath(msg.path);
  if (!path) return { ok: false, code: "path" };
  if ((intent === "asset" || intent === "open") && method !== "GET") return { ok: false, code: "method" };
  if (intent === "page" && method !== "GET" && method !== "POST") return { ok: false, code: "method" };
  let body = null;
  if (msg.body !== undefined && msg.body !== null && !stream) {
    if (!(msg.body instanceof ArrayBuffer)) return { ok: false, code: "shape" };
    if (msg.body.byteLength > bodyMax) return { ok: false, code: "size" };
    if (method === "GET" || method === "HEAD") return { ok: false, code: "shape" };
    body = new Uint8Array(msg.body);
  }
  const headers = cleanHeaders(msg.headers);
  if (headers === null) return { ok: false, code: "shape" };
  if (!scopes.allows(method, path, { stream })) return { ok: false, code: "scope" };
  return { ok: true, req: { id: msg.id, gen: msg.gen, intent, method, path, headers, body, stream } };
}
