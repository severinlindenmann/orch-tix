// fileshare/static/js/banner.js
import { el } from "./ui.js";
import { clearKeys } from "./keystore.js";
import { clearLists } from "./db.js";
import { loginHref } from "./nav.js";

const shown = new Map();

function slot() {
  let s = document.getElementById("banner");
  if (!s) {
    s = el("div", { id: "banner" });
    document.body.prepend(s);
  }
  return s;
}

export function hideBanner(kind) {
  shown.get(kind)?.remove();
  shown.delete(kind);
}

export function showBanner(kind, text, link = null) {
  hideBanner(kind);
  const b = el("div", { class: `banner banner-${kind}`, role: kind === "session" ? "alert" : "status" }, el("span", {}, text));
  if (link) b.append(el("a", { href: link.href, class: "banner-link" }, link.label));
  const dismiss = el("button", { class: "icon-btn", type: "button", "aria-label": "Dismiss" }, "×");
  dismiss.addEventListener("click", () => hideBanner(kind));
  b.append(dismiss);
  slot().append(b);
  shown.set(kind, b);
}

if (typeof window !== "undefined") {
  window.addEventListener("fs:unauthenticated", () => {
    clearKeys().catch(() => {});
    clearLists().catch(() => {});
    hideBanner("decrypt");
    showBanner("session", "Session expired — log in again", { href: loginHref(location), label: "Log in" });
  });
  window.addEventListener("fs:decrypt-failed", (e) => {
    const id = e.detail?.id || "a file";
    showBanner("decrypt", `Couldn't decrypt ${id} — the file may be corrupt or was encrypted with another key`);
  });
  window.addEventListener("fs:network-error", () => {
    showBanner("network", "Can't reach tix.severin.io — check your connection");
  });
  window.addEventListener("online", () => hideBanner("network"));
  // A server outage never fires `online`; the next request that gets any answer clears the banner.
  window.addEventListener("fs:network-ok", () => hideBanner("network"));
}
