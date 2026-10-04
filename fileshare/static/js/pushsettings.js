// fileshare/static/js/pushsettings.js — the "Notifications on this browser" card of Settings (spec
// T7, T10). The toggle turns Web Push on: Notification.requestPermission(), then
// pushManager.subscribe({userVisibleOnly: true, applicationServerKey: the VAPID key}), then POST
// /api/push/subscribe. Off unsubscribes and sends DELETE. The card shows the state: on, off, blocked
// by the browser, unsupported, or (iOS outside the Home Screen app) how to get notifications.
//
// Pure helpers first (node tests them), then the card. settings.js calls mountPushCard().
import { api, ApiError } from "./api.js";

export const TEXT = Object.freeze({
  on: "On",
  off: "Off",
  blocked: "Blocked by the browser",
  unsupported: "Not supported",
  ios: "Add tix to your Home Screen to get notifications",
  loading: "Checking…",
});
export const HINT = Object.freeze({
  on: "This browser gets a notification when a ticket needs your input or passed its tests.",
  off: "Get a notification when a ticket needs your input or passed its tests.",
  blocked: "Notifications are blocked for this site. Allow them in the browser's site settings, then reload.",
  unsupported: "This browser can't receive push notifications.",
  ios: "On iPhone and iPad, notifications work only in the app: tap Share, then Add to Home Screen, and open tix from there.",
  loading: "",
});

// iPhone, iPad (which says Macintosh but has touch), iPod.
export function isIos(nav = globalThis.navigator) {
  const ua = String(nav?.userAgent ?? "");
  return /iPad|iPhone|iPod/.test(ua) || (/Macintosh/.test(ua) && Number(nav?.maxTouchPoints) > 1);
}

export function isStandalone(win = globalThis.window, nav = globalThis.navigator) {
  try {
    if (win?.matchMedia?.("(display-mode: standalone)").matches) return true;
  } catch {
    /* no matchMedia */
  }
  return nav?.standalone === true;
}

export function pushSupported(win = globalThis.window, nav = globalThis.navigator) {
  return Boolean(nav && "serviceWorker" in nav && win && "PushManager" in win && "Notification" in win);
}

// -> "ios" | "unsupported" | "blocked" | "on" | "off"
export function pushState({ ios, standalone, supported, permission, subscribed }) {
  if (ios && !standalone) return "ios";
  if (!supported) return "unsupported";
  if (permission === "denied") return "blocked";
  return subscribed && permission === "granted" ? "on" : "off";
}

export function b64uToBytes(s) {
  const b64 = String(s).replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

// The POST /api/push/subscribe body for a PushSubscription.
export function subscribeBody(sub) {
  const j = typeof sub.toJSON === "function" ? sub.toJSON() : sub;
  return { endpoint: j.endpoint, keys: { p256dh: j.keys?.p256dh, auth: j.keys?.auth } };
}

// ---- the card

async function registration() {
  // The page registers the worker (swreg.js); wait for it, but not forever.
  return Promise.race([
    navigator.serviceWorker.ready,
    new Promise((_, reject) => setTimeout(() => reject(new Error("no service worker")), 10_000)),
  ]);
}

export function mountPushCard(doc = document) {
  const card = doc.getElementById("push-card");
  if (!card) return null;
  const toggle = doc.getElementById("push-toggle");
  const status = doc.getElementById("push-status");
  const hint = doc.getElementById("push-hint");
  const msg = doc.getElementById("push-msg");
  let current = "loading";
  let busy = false;

  const say = (text, error = false) => {
    msg.textContent = text || "";
    msg.className = error ? "settings-msg error" : "settings-msg";
    msg.hidden = !text;
  };
  const show = (state) => {
    current = state;
    card.dataset.state = state;
    status.textContent = TEXT[state];
    hint.textContent = HINT[state];
    hint.hidden = !HINT[state];
    toggle.checked = state === "on";
    toggle.disabled = busy || !(state === "on" || state === "off");
    card.classList.toggle("is-disabled", toggle.disabled && !busy);
  };

  async function detect() {
    const ios = isIos();
    const standalone = isStandalone();
    const supported = pushSupported();
    let subscribed = false;
    const permission = supported ? Notification.permission : "default";
    if (supported && !(ios && !standalone) && permission !== "denied") {
      try {
        subscribed = Boolean(await (await registration()).pushManager.getSubscription());
      } catch {
        subscribed = false;
      }
    }
    return pushState({ ios, standalone, supported, permission, subscribed });
  }

  async function turnOn() {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      show(permission === "denied" ? "blocked" : "off");
      if (permission !== "denied") say("Notifications weren't allowed.");
      return;
    }
    const reg = await registration();
    const { public_key: key } = await api("GET", "/api/push/vapid");
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64uToBytes(key) });
    try {
      await api("POST", "/api/push/subscribe", { json: subscribeBody(sub) });
    } catch (err) {
      await sub.unsubscribe().catch(() => {}); // the server doesn't know it: don't leave a dead subscription
      throw err;
    }
    show("on");
    say("Notifications are on for this browser.");
  }

  async function turnOff() {
    const reg = await registration();
    const sub = await reg.pushManager.getSubscription();
    if (sub) {
      const { endpoint } = sub;
      await sub.unsubscribe().catch(() => false);
      try {
        await api("DELETE", "/api/push/subscribe", { json: { endpoint } });
      } catch (err) {
        // The browser no longer has the subscription, so nothing can arrive here any more; the
        // server just still lists it (its next push to it is answered 410 and pruned).
        show("off");
        const why = err instanceof ApiError ? err.detail || err.code : "the server can't be reached";
        say(`Notifications are off on this browser, but the server wasn't told (${why}). It forgets the subscription on its next push.`, true);
        return;
      }
    }
    show("off");
    say("Notifications are off for this browser.");
  }

  toggle.addEventListener("change", async () => {
    if (busy) return;
    const want = toggle.checked;
    busy = true;
    say("");
    toggle.disabled = true;
    try {
      await (want ? turnOn() : turnOff());
    } catch (err) {
      const why = err instanceof ApiError ? err.detail || err.code : err?.message || "something went wrong";
      say(`Couldn't turn notifications ${want ? "on" : "off"}: ${why}`, true);
      show(await detect().catch(() => "off"));
    } finally {
      busy = false;
      show(current);
    }
  });

  show("loading");
  const ready = detect().then(show, () => show("unsupported"));
  return { ready, state: () => current };
}
