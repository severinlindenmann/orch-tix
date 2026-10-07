// fileshare/static/js/remote-mailbox.js: this browser's side of the bridge mailbox (routes/bridge.py): post a sealed
// request, one multiplexed long poll for every answer, give up on a request. Bodies are base64url text; nothing here
// reads them. Chunks are handed to the listener registered for their request id.
import { b64u, unb64u } from "./crypto.js";

export class MailboxError extends Error {
  constructor(status, code) { super(code); this.name = "MailboxError"; this.status = status; this.code = code; }
}
const RETRIES = 3, WAIT_S = 25;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function createMailbox(space, { fetchFn = (...a) => fetch(...a), tab = b64u(crypto.getRandomValues(new Uint8Array(12))), retryMs = 1000 } = {}) {
  const listeners = new Map();      // request id -> {chunk(c), fail(err)}
  let polling = false, closed = false, abort = null;

  async function call(method, path, json, signal) {
    let res;
    try {
      res = await fetchFn(`/api/bridge/${space}${path}`, { method, credentials: "same-origin", signal,
        headers: json ? { "Content-Type": "application/json" } : {}, body: json ? JSON.stringify(json) : undefined });
    } catch (e) {
      if (e?.name === "AbortError") throw e;
      throw new MailboxError(0, "network");
    }
    if (!res.ok) {
      let code = `http_${res.status}`;
      try { code = (await res.json()).error || code; } catch { /* no body */ }
      throw new MailboxError(res.status, code);
    }
    return res.status === 204 ? null : res.json();
  }

  async function loop() {
    polling = true;
    let failures = 0;
    try {
      while (listeners.size && !closed) {
        abort = new AbortController();
        let out;
        try {
          out = await call("GET", `/responses?wait=${WAIT_S}&tab=${tab}`, null, abort.signal);
          failures = 0;
        } catch (e) {
          if (e?.name === "AbortError") continue;
          if (e.status === 401 || e.status === 403 || ++failures >= RETRIES) {
            for (const l of [...listeners.values()]) l.fail(e);
            listeners.clear();
            return;
          }
          await sleep(retryMs);
          continue;
        }
        for (const c of out?.chunks || []) {
          const l = listeners.get(c.id);
          if (!l) continue;
          let env;
          try { env = unb64u(c.body); } catch { continue; }      // not base64url: dropped
          l.chunk({ env, mailbox: { id: c.id, idx: c.idx, last: c.last, stream: c.stream } });
        }
      }
    } finally {
      polling = false;
    }
  }

  return {
    tab,
    // Listen for the answer to `id`, then post the sealed envelope. The listener comes first so an instant answer is not missed.
    async post(id, envelope, stream, listener) {
      listeners.set(id, listener);
      if (!polling) loop();
      try {
        await call("POST", "/requests", { id, body: b64u(envelope), tab, stream: !!stream });
      } catch (e) {
        listeners.delete(id);
        throw e;
      }
    },
    // Give up on a request (frees the quota). A retry posts exactly the same bytes again (§5.3).
    async cancel(id) {
      listeners.delete(id);
      try { await call("DELETE", `/requests/${id}`); } catch { /* already gone */ }
    },
    close() { closed = true; listeners.clear(); abort?.abort(); },
  };
}
