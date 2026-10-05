export class ApiError extends Error {
  constructor(status, code, detail = "") {
    super(detail || code);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

// A 401 from /api/login is a refusal, not an expired session, so it must not trigger the global redirect.
const NOT_A_SESSION_401 = new Set(["/api/login"]);

// One open of the app asks for the full /api/mirrors list from several places at once (the Needs list,
// the tab-bar badge, the change watcher): ~0.5 MB each. Share one answer for a few seconds.
const SHARED_TTL_MS = 4000;
const shared = new Map(); // path -> { at, promise }
export function api(method, path, opts = {}) {
  if (method !== "GET") shared.clear(); // a write may change the list: the next read is a fresh one
  if (method === "GET" && !opts.raw && path.startsWith("/api/mirrors/changes")) {
    // the change feed saying something changed (another device, a desktop ack): the list we hold is stale now
    return request(method, path, opts).then((body) => { if (body?.mirrors?.length) shared.clear(); return body; });
  }
  if (method !== "GET" || opts.raw || path !== "/api/mirrors") return request(method, path, opts);
  let hit = shared.get(path);
  if (!hit || Date.now() - hit.at > SHARED_TTL_MS) {
    hit = { at: Date.now(), promise: request(method, path, opts) };
    hit.promise.catch(() => shared.delete(path));
    shared.set(path, hit);
  }
  return hit.promise.then((body) => structuredClone(body));
}

async function request(method, path, { json, form, raw } = {}) {
  const init = { method, credentials: "same-origin", headers: {} };
  if (json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  } else if (form !== undefined) {
    init.body = form;
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    window.dispatchEvent(new CustomEvent("fs:network-error"));
    throw new ApiError(0, "network", "Couldn't reach the server. Check your connection.");
  }
  // Any HTTP answer, error statuses included, proves the server is reachable again.
  window.dispatchEvent(new CustomEvent("fs:network-ok"));
  if (res.status === 401 && !NOT_A_SESSION_401.has(path)) {
    window.dispatchEvent(new CustomEvent("fs:unauthenticated"));
  }
  if (!res.ok) {
    let body = {};
    try { body = await res.json(); } catch { body = {}; }
    throw new ApiError(res.status, body.error || `http_${res.status}`, body.detail || "");
  }
  if (raw) return res;
  if (res.status === 204) return null;
  return res.json();
}
