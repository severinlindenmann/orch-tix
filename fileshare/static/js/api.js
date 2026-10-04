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

export async function api(method, path, { json, form, raw } = {}) {
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
