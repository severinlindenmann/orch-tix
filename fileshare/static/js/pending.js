// The files-page "waiting for approval" banner and Devices badge (not an entry module).
import { api } from "./api.js";
import { localFingerprint } from "./approve.js";

const POLL_MS = 10_000;

// R12: returns display copies whose `fingerprint` is the one computed here from `pubkey`, with
// `mismatch` set when the server-reported fingerprint differs (or the key can't be parsed).
// `server` keeps the untouched DeviceOut: approveDevice needs the server's field to compare against.
export async function withLocalFingerprints(devices) {
  return Promise.all(devices.map(async (d) => {
    let fp = null;
    try {
      fp = await localFingerprint(d);
    } catch {
      fp = null;
    }
    return { ...d, fingerprint: fp ?? "—", mismatch: fp === null || fp !== d.fingerprint, server: d };
  }));
}

// Expects devices already passed through withLocalFingerprints, so `fingerprint` is the local one.
export function pendingSummary(devices) {
  const pending = devices.filter((d) => d.status === "pending");
  if (pending.length === 0) return { count: 0, badge: "", text: "" };
  const [d] = pending;
  const text = pending.length === 1
    ? `${d.name} · ${d.project} is waiting for approval — ${d.mismatch ? "fingerprint mismatch, do not approve" : `fingerprint ${d.fingerprint}`}`
    : `${pending.length} devices are waiting for approval`;
  return { count: pending.length, badge: `${pending.length} pending`, text };
}

export async function refreshPending(doc = document) {
  const banner = doc.getElementById("pending-banner");
  const badge = doc.getElementById("devices-badge");
  if (!banner && !badge) return;
  let summary;
  try {
    const { devices } = await api("GET", "/api/devices");
    summary = pendingSummary(await withLocalFingerprints(devices.filter((d) => d.status === "pending")));
  } catch {
    return; // the session/network banners report failures; this indicator just stays as it was
  }
  if (badge) {
    // The tab bar and sidebar show a bare count (spec §18); the full "N pending" is its tooltip.
    badge.textContent = summary.count ? String(summary.count) : "";
    badge.title = summary.badge;
    badge.hidden = summary.count === 0;
  }
  if (banner) {
    banner.querySelector(".pending-text").textContent = summary.text;
    banner.hidden = summary.count === 0;
  }
}

export function installPendingIndicator(doc = document, win = window) {
  refreshPending(doc);
  win.addEventListener("fs:devices-changed", () => refreshPending(doc));
  win.setInterval(() => {
    if (doc.visibilityState === "visible") refreshPending(doc);
  }, POLL_MS);
}
