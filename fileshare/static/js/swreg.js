// Registers the service worker (spec §16) as /sw.js?v=<BUILD>: /sw.js is served raw, so the query is
// how the worker learns the build it caches for. The build is read from the page's stamped
// stylesheet link. Imported by shell.js (every signed-in page) and login.js. Where the browser has
// no service workers (or refuses them: private mode, an insecure origin), this does nothing.
export function buildStamp(doc = document) {
  const link = doc.querySelector('link[rel="stylesheet"][href^="/static/css/app.css"]');
  try {
    return new URL(link.href, "https://x.invalid").searchParams.get("v") || "dev";
  } catch {
    return "dev";
  }
}

export function registerServiceWorker(nav = globalThis.navigator, doc = globalThis.document) {
  if (!nav || !("serviceWorker" in nav) || !doc) return Promise.resolve(null);
  try {
    return nav.serviceWorker
      .register(`/sw.js?v=${encodeURIComponent(buildStamp(doc))}`, { scope: "/" })
      .catch(() => null);
  } catch {
    return Promise.resolve(null);
  }
}

// Signed-in pages (shell.js) ask the worker to cache the pages a signed-out install could not
// (/settings redirects to /login without a session).
export async function warmShell(nav = globalThis.navigator) {
  try {
    const reg = await nav?.serviceWorker?.ready;
    reg?.active?.postMessage({ type: "cache-pages" });
  } catch {
    /* no worker: nothing to warm */
  }
}

// A message to the active worker (nav.js tells it what needs you, sw.js reconcileNeeds). Best effort.
export function tellWorker(msg, nav = globalThis.navigator) {
  try {
    nav?.serviceWorker?.controller?.postMessage(msg);
  } catch {
    /* no worker */
  }
}

// The worker saw a navigation answered by a newer build (sw.js "new-build"): register that build's worker
// now, so it installs and takes over on this open rather than one deploy late.
const STAMP = /^[A-Za-z0-9._-]{1,40}$/;
export function listenForNewBuild(nav = globalThis.navigator) {
  if (!nav || !("serviceWorker" in nav)) return;
  nav.serviceWorker.addEventListener("message", (e) => {
    const build = e.data?.type === "new-build" ? String(e.data.build || "") : "";
    if (!STAMP.test(build)) return;
    try {
      nav.serviceWorker.register(`/sw.js?v=${encodeURIComponent(build)}`, { scope: "/" }).catch(() => null);
    } catch {
      /* no worker: nothing to update */
    }
  });
}

if (typeof document !== "undefined") {
  registerServiceWorker();
  listenForNewBuild();
}
