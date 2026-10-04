// fileshare/static/js/approve.js — shared by onboard.js and devices.js (not an entry module)
import { open, sealToDevice, fingerprint, b64u, unb64u, AAD_MK } from "./crypto.js";
import { api } from "./api.js";
import { loadKeys } from "./keystore.js";
import { confirmDialog, toast } from "./ui.js";

// Computed here from the public key the device will open the bundle with. The server's
// `fingerprint` field is only compared against it, never shown on its own.
export async function localFingerprint(device) {
  return fingerprint(unb64u(device.pubkey));
}

export async function approveDevice(device) {
  const fp = await localFingerprint(device);
  if (fp !== device.fingerprint) {
    toast(`Fingerprint mismatch for ${device.name} — not approved`, "error");
    return false;
  }
  const ok = await confirmDialog({
    title: `Approve ${device.name}?`,
    body: `Does the terminal show ${fp}? Approve only if it matches exactly — ${device.name} (${device.project}) then gets the key to every file.`,
    confirmLabel: "Approve",
    danger: false,
  });
  if (!ok) return false;
  const keys = await loadKeys();
  if (!keys) {
    window.dispatchEvent(new CustomEvent("fs:unauthenticated"));
    return false;
  }
  const { wrapped_mk } = await api("GET", "/api/keyblob");
  const mkRaw = await open(keys.kek, unb64u(wrapped_mk), AAD_MK);
  let bundle;
  try {
    bundle = await sealToDevice(mkRaw, unb64u(device.pubkey), device.id);
  } finally {
    mkRaw.fill(0);
  }
  await api("POST", `/api/devices/${device.id}/approve`, { json: { device_bundle: b64u(bundle) } });
  toast(`${device.name} approved`, "ok");
  window.dispatchEvent(new CustomEvent("fs:devices-changed"));
  return true;
}

// Runs an approve/reject `action` with all of `buttons` disabled, so a double-click can't start it
// twice. The buttons come back on cancel (the action resolves false) or on error; on success the
// caller re-renders them away. Returns the action's result, or false if one is already in flight.
export async function whileBusy(buttons, action) {
  if (buttons.some((b) => b.disabled)) return false;
  for (const b of buttons) b.disabled = true;
  let ok = false;
  try {
    ok = await action();
    return ok;
  } finally {
    if (!ok) for (const b of buttons) b.disabled = false;
  }
}

export async function rejectDevice(device) {
  const ok = await confirmDialog({
    title: `Reject ${device.name}?`,
    body: `${device.name} (${device.project}) is revoked and never receives the key. Generate a new link if this was a mistake.`,
    confirmLabel: "Reject",
    danger: true,
  });
  if (!ok) return false;
  await api("DELETE", `/api/devices/${device.id}`);
  toast(`${device.name} rejected`);
  window.dispatchEvent(new CustomEvent("fs:devices-changed"));
  return true;
}
