// fileshare/static/js/wakelock.js — keep the screen on while something long runs (a recording):
// a phone that dims and locks mid-recording suspends the page, and with it the microphone.
// The Screen Wake Lock API is best effort: without it (older browsers), or when the browser refuses
// (battery saver), the screen just sleeps as it always did. The browser drops the lock whenever the
// page is hidden, so it is taken again each time the page becomes visible, until release().
// `nav` and `doc` are injectable for node tests.

export function keepAwake({ nav = globalThis.navigator, doc = globalThis.document } = {}) {
  const api = nav && nav.wakeLock;
  let sentinel = null;
  let asking = null;          // the in-flight request, so a visibility change can't ask twice
  let released = false;

  const held = () => sentinel !== null && !sentinel.released;
  const visible = () => !doc || doc.visibilityState === "visible";

  const ask = () => {
    if (released || asking || held() || !visible()) return;
    asking = api.request("screen")
      .then((s) => {
        if (released) return s.release();   // release() came while the request was pending
        sentinel = s;
      })
      .catch(() => { /* refused (battery saver, not visible yet): the screen may sleep */ })
      .finally(() => { asking = null; });
  };
  const onVisibility = () => ask();

  if (api && typeof api.request === "function") {
    if (doc) doc.addEventListener("visibilitychange", onVisibility);
    ask();
  }

  return {
    async release() {
      if (released) return;
      released = true;
      if (doc) doc.removeEventListener("visibilitychange", onVisibility);
      if (asking) await asking;
      const s = sentinel;
      sentinel = null;
      if (s && !s.released) await s.release().catch(() => {});
    },
  };
}
