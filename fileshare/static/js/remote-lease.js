// fileshare/static/js/remote-lease.js: typing into a terminal from this browser (docs/bridge-frame.md, "Typing").
// The computer lets a device type only under a 15-minute lease (bridge-protocol.md §9.4), given after a Face ID style
// confirmation and only to input that names a stream THIS device opened. This module sits between the frame host and the
// transport (remote-transport.js) and does the device's half of that:
//   - it remembers the streams the frame has open (their rid comes on the head event) and, for a request to a lease route,
//     names one of them in the request header; with none open the request is refused here, in words, and nothing is sent;
//   - it wraps the unlock sheet (unlock.js askAssertion): a refused or abandoned sheet is not asked again for COOL_MS, and
//     a lease route fails at once in the meantime, so the page keeps its keys (it resends a failed post unchanged, same `n`)
//     without a new sheet and without a bridged request every second;
//   - it says when typing is unlocked (until a time on this device's clock, the lease being 15 minutes from the confirmation)
//     and clears that on the next lease_required, on a revoke or when the time passes.
// The sheet itself is awaited by the transport with no timer running; it ends by the challenge's own expiry (unlock.js).
// The key post that was refused is NOT resent by the page: after a confirmation the computer runs that very request once
// (§9.4), so `n` is used once. If the sheet is cancelled, the computer never ran it, so the resent post (same `n`) is new to it.
export const LEASE_MS = 15 * 60_000;
export const COOL_MS = 30_000;
const DEAD = new Set(["revoked", "not_paired", "stopped", "scope_changed"]);   // after these nothing more is sent
const ROUTES = [/^\/terminals\/new$/, /^\/terminals\/([^/]+)\/(?:keys|size|end)$/, /^\/t\/[^/]+\/agent\/start$/, /^\/quick\/[^/]+\/agent\/start$/];

export const LEASE_TEXT = Object.freeze({
  no_stream: "Typing needs the live screen. Wait until it shows, then try again.",
  locked: "Typing is locked because it was not confirmed. Your keys are kept; you will be asked again in a moment.",
});

const route = (req) => {
  if (req.method !== "POST" || req.stream) return null;
  const path = String(req.path).split("?")[0];
  for (const re of ROUTES) { const m = re.exec(path); if (m) return { name: m[1] || null }; }
  return null;
};

// opts: ask (unlock.js askAssertion), say(text) for a problem the person should read, onLease(untilMs | null),
// now, coolMs, leaseMs (tests). -> {unlock, wrap(transport)}
export function leaseGlue({ ask, say = () => {}, onLease = () => {}, now = Date.now, coolMs = COOL_MS, leaseMs = LEASE_MS }) {
  const streams = new Map();        // stream rid -> its path
  let coolUntil = 0, dead = null, unlockedAt = 0, until = 0, timer = null;

  const setLease = (t) => {
    clearTimeout(timer);
    until = t;
    onLease(t || null);
    if (t) timer = setTimeout(() => { until = 0; onLease(null); }, Math.max(0, t - now()));
  };

  async function unlock(session, refusal) {
    if (refusal.code !== "lease_required") return ask(session, refusal);
    setLease(0);
    const r = await ask(session, refusal);
    if (r.ok) { unlockedAt = now(); coolUntil = 0; }
    else if (r.reason !== "busy") coolUntil = now() + coolMs;
    return r;
  }

  const pick = (name) => {
    const want = name === null ? null : `/terminals/${name}/stream`;
    let found = null;
    for (const [rid, path] of streams) if (want === null || path === want) found = rid;   // the newest that fits
    return found;
  };

  function wrap(inner) {
    async function* followStream(req) {
      let mine = null;
      try {
        for await (const ev of inner.request(req)) {
          if (ev.type === "head" && ev.rid) { if (mine) streams.delete(mine); mine = ev.rid; streams.set(mine, String(req.path).split("?")[0]); }
          yield ev;
        }
      } catch (e) {
        gone(e);
        throw e;
      } finally { if (mine) streams.delete(mine); }
    }
    const gone = (e) => { if (DEAD.has(e?.code)) { dead = e.message; setLease(0); } };
    async function* typing(req, r) {
      const refuse = (text) => { say(text); throw new Error(text); };
      if (dead) refuse(dead);
      if (now() < coolUntil) refuse(LEASE_TEXT.locked);
      const rid = pick(r.name);
      if (!rid) refuse(LEASE_TEXT.no_stream);
      try {
        for await (const ev of inner.request({ ...req, streamRid: rid })) {
          if (ev.type === "head" && unlockedAt) { setLease(unlockedAt + leaseMs); unlockedAt = 0; }   // answered after a confirmation: the lease is open
          yield ev;
        }
      } catch (e) {
        unlockedAt = 0;
        gone(e);
        throw e;
      }
    }
    return {
      request(req) {
        if (req.stream) return followStream(req);
        const r = route(req);
        return r ? typing(req, r) : inner.request(req);
      },
    };
  }
  return { unlock, wrap, stop: () => { clearTimeout(timer); } };
}
