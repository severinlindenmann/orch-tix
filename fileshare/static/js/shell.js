// The chrome every signed-in page shares (spec §18): the sidebar on a desktop, the bottom tab bar on
// a phone, "This browser ✎" and Sign out. Importing onboard.js wires every [data-action="onboard"]
// button and the pending-devices badge in the Devices tab; swreg.js registers the service worker,
// and outbox-ui.js flushes the encrypted outbox from every signed-in page (spec §16). The Tickets
// badge (spec T10) is filled on load and again on every "fs:tickets-changed" (the board sends it).
import "./banner.js";
import "./onboard.js";
import { warmShell } from "./swreg.js";
import { api } from "./api.js";
import { CLEAR_FAILED, clearLocalData, confirmDiscardOutbox } from "./outbox-ui.js";
import { hydrateIcons, toast } from "./ui.js";
import { wireBrowserNameButton } from "./browsersession.js";
import { markCurrentTab, refreshAttention } from "./nav.js";

const LOGOUT_WARN_MS = 2500;

export async function logout() {
  // Queued uploads (spec §16) would be lost with the keys: ask first.
  if (!(await confirmDiscardOutbox())) return;
  try {
    await api("POST", "/api/logout");
  } catch {
    // The session may already be gone; local data is cleared either way.
  }
  const done = await clearLocalData();
  const cleared = done.keys && done.bridge && done.outbox && done.lists;
  if (!cleared) toast(CLEAR_FAILED, "error");
  // Leave the toast readable for a moment when clearing failed; redirect either way.
  setTimeout(() => location.replace("/login"), cleared ? 0 : LOGOUT_WARN_MS);
}

if (typeof document !== "undefined") {
  hydrateIcons();
  warmShell();
  for (const b of document.querySelectorAll('[data-action="logout"]')) b.addEventListener("click", logout);
  wireBrowserNameButton([...document.querySelectorAll("button.browser-name")]);
  markCurrentTab();
  refreshAttention();
  window.addEventListener("fs:needs-changed", () => refreshAttention());
  // Back in the app (an installed iPhone app resumes without a reload): catch up with what was handled meanwhile.
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refreshAttention(); });
}
