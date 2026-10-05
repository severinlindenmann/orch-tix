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

// A new worker takes over an open page (skipWaiting + clients.claim, sw.js): the page keeps the old JS it already
// loaded, but any later dynamic import() is answered from the new build's cache, so old and new modules could mix.
// A page that was not controlled before (the very first install claims it) is not an update. When the page is in
// the background and holds nothing the user typed or is deciding on, it just reloads; otherwise it asks.
export function safeToReload(doc = globalThis.document) {
  if (!doc) return false;
  if (doc.querySelector("dialog[open]")) return false;
  for (const f of doc.querySelectorAll("textarea, input:not([type=checkbox]):not([type=radio]):not([type=search]):not([type=file]):not([type=button]):not([type=submit])")) {
    if (f.value) return false;
  }
  return true;
}

export function listenForUpdate(nav = globalThis.navigator, doc = globalThis.document, reload = () => globalThis.location.reload()) {
  const sw = nav?.serviceWorker;
  if (!sw || !doc) return;
  let had = Boolean(sw.controller);
  let asked = false;
  sw.addEventListener("controllerchange", () => {
    if (!had) { had = true; return; }
    if (asked) return;
    asked = true;
    if (doc.visibilityState === "hidden" && safeToReload(doc)) { reload(); return; }
    showUpdatePrompt(doc, reload);
  });
}

export function showUpdatePrompt(doc, reload) {
  if (doc.getElementById("update-prompt")) return;
  const bar = doc.createElement("div");
  bar.id = "update-prompt";
  bar.className = "update-prompt";
  bar.setAttribute("role", "status");
  const text = doc.createElement("span");
  text.textContent = "tix was updated.";
  const btn = doc.createElement("button");
  btn.type = "button";
  btn.className = "btn btn-primary";
  btn.textContent = "Reload";
  btn.addEventListener("click", () => reload());
  bar.append(text, btn);
  doc.body.append(bar);
}

if (typeof document !== "undefined") {
  registerServiceWorker();
  listenForNewBuild();
  listenForUpdate();
}
