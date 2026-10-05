// fileshare/static/js/onboard-cmd.js — pure: no DOM, no fetch (node imports it)
import { b64u } from "./crypto.js";

export const CODE_RE = /^shr1\.[A-Za-z0-9_-]{22}$/;
export const TERMINAL = new Set(["approved", "rejected", "expired"]);
export const TOKEN_TTL_MS = 15 * 60_000;

export function buildCode(lookup) {
  if (!(lookup instanceof Uint8Array) || lookup.length !== 16) throw new TypeError("lookup must be 16 bytes");
  return `shr1.${b64u(lookup)}`;
}

// Same rule as the installer's slug(): lower-case, anything but a-z 0-9 . _ - becomes "-", no leading
// "-" or ".", no trailing "-", at most 64 characters. "" means "let the installer use the hostname".
export function deviceSlug(name) {
  return String(name ?? "").toLowerCase().replace(/[^a-z0-9._-]/g, "-").replace(/^[-.]+/, "").replace(/-+$/, "").slice(0, 64);
}

export function buildCommands(origin, code, deviceName = "") {
  const dev = deviceSlug(deviceName);
  const sh = dev ? ` --device '${dev}'` : "";
  const ps = dev ? ` -Device '${dev}'` : "";
  const curl = `curl --proto '=https' --tlsv1.2 -fsSL ${origin}/onboarding.txt`;
  return {
    posix: `${curl} | bash -s -- '${code}'${sh}`,
    posixInspect: `${curl} -o onboarding.sh && less onboarding.sh && bash onboarding.sh '${code}'${sh}`,
    windows: `& ([scriptblock]::Create((irm ${origin}/onboarding.ps1))) '${code}'${ps}`,
    // -File under a process-scoped Bypass: the default Restricted policy blocks a plain `.\onboarding.ps1`.
    windowsInspect: `irm ${origin}/onboarding.ps1 -OutFile onboarding.ps1; notepad onboarding.ps1; powershell -NoProfile -ExecutionPolicy Bypass -File .\\onboarding.ps1 '${code}'${ps}`,
  };
}

export function defaultOs(userAgent) {
  return /Windows/i.test(userAgent || "") ? "windows" : "posix";
}

// state = {phase: "waiting"|"pending"|"approved"|"rejected"|"expired", expiresAt: ms, device: DeviceOut|null}
// tokenStatus: GET /api/onboarding-tokens/{id} body | null (404: the token is gone) | undefined (clock tick)
// expiresAt comes from the local clock at generate time (a skewed clock can't shorten the countdown);
// the server stays authoritative through polling: a 404, or a used token with no device, ends the wait.
export function nextState(state, tokenStatus, now = Date.now()) {
  if (TERMINAL.has(state.phase)) return state;
  if (tokenStatus === null) return { ...state, phase: "expired" };
  const device = tokenStatus?.device ?? state.device;
  if (device) {
    if (device.status === "active") return { ...state, phase: "approved", device };
    if (device.status === "revoked") return { ...state, phase: "rejected", device };
    return { ...state, phase: "pending", device };
  }
  if (tokenStatus?.used_at) return { ...state, phase: "expired" };
  if (now >= state.expiresAt) return { ...state, phase: "expired" };
  return { ...state, phase: "waiting" };
}
