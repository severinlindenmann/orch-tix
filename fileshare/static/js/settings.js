import "./banner.js";
// fileshare/static/js/settings.js: account-wide settings, end-to-end encrypted (spec §15).
//
// The decrypted settings object lives only in `state` below, for the lifetime of this page. It is
// never logged and never written to localStorage/sessionStorage; every decrypted value reaches the
// DOM through textContent only. The key input never shows the stored key.
import { api, ApiError } from "./api.js";
import { confirmDialog, hydrateIcons, toast } from "./ui.js";
import { relTime } from "./format.js";
import { loadKeys } from "./keystore.js";
import { signInAgain } from "./mirrors-data.js";
import { IntegrityError, openSettings, sealSettings } from "./crypto.js";
import { mountPushCard } from "./pushsettings.js"; // ---- tickets (spec T7, T10): Web Push on this browser
import { mountPairCard } from "./pair.js"; // ---- pairing (Task 10): Pair with a desktop
import { AUTO_KEY, LANGUAGE_KEY, LANGUAGES, SETTINGS_CHANNEL, transcriptionSettings } from "./transcribe-settings.js";

export const DEEPGRAM = "deepgram_api_key";
export const CONFLICT_MSG = "Settings changed elsewhere — reloaded, please retry";
// The same rule the CLI applies before putting the key in an HTTP header (sharing.py _SECRET_OK_RE):
// one line of 16–256 printable ASCII characters, no spaces.
const KEY_RE = /^[\x21-\x7e]{16,256}$/;

export function checkKey(raw) {
  const v = String(raw ?? "").trim();
  if (!v) return { error: "Enter a key. To clear the stored key, use Remove." };
  if (!KEY_RE.test(v)) return { error: "That doesn't look like an API key (16–256 characters, no spaces)." };
  return { value: v };
}

export const isConfigured = (obj) => typeof obj?.[DEEPGRAM] === "string" && obj[DEEPGRAM] !== "";

export function statusText(obj, updatedAt, now = Date.now()) {
  if (!isConfigured(obj)) return "Not set";
  const when = updatedAt ? relTime(updatedAt, now) : "";
  return when ? `Configured ✓ · updated ${when}` : "Configured ✓";
}

// Everything else in the object (keys this page doesn't know) is carried over untouched.
export const withKey = (obj, value) => ({ ...obj, [DEEPGRAM]: value });
export function withoutKey(obj) {
  const { [DEEPGRAM]: _removed, ...rest } = obj;
  return rest;
}

// ---- transcription (Task 35, spec §20): the language and the auto toggle, in the same object
export const transcriptionOf = (obj) => {
  const { language, auto } = transcriptionSettings(obj);
  return { language, auto };
};
export function withTranscription(obj, { language, auto }) {
  if (!LANGUAGES.includes(language) || typeof auto !== "boolean") throw new TypeError("bad transcription settings");
  return { ...obj, [LANGUAGE_KEY]: language, [AUTO_KEY]: auto };
}
// Tells this browser's other tabs (and their auto-transcription) to read the settings again.
function announceSaved() {
  try {
    const ch = new BroadcastChannel(SETTINGS_CHANNEL);
    ch.postMessage({ type: "changed" });
    ch.close();
  } catch {
    /* no BroadcastChannel: other tabs pick the change up on their next load */
  }
}

// unreadable: the stored settings failed to decrypt; only "Reset settings" may overwrite them.
const state = { mk: null, obj: null, rev: 0, updatedAt: null, busy: false, unreadable: false, loaded: false };
const $ = (id) => document.getElementById(id);

function message(text, kind = "info") {
  const m = $("settings-msg");
  m.textContent = text;
  m.className = kind === "error" ? "settings-msg error" : "settings-msg";
  m.hidden = !text;
}

function hideRevealed() {
  const out = $("deepgram-revealed");
  out.textContent = "";
  out.hidden = true;
  $("deepgram-reveal").textContent = "Reveal";
  $("deepgram-reveal").setAttribute("aria-expanded", "false");
}

function render() {
  const loaded = state.obj !== null;
  const configured = loaded && isConfigured(state.obj);
  const status = $("deepgram-status");
  status.textContent = loaded ? statusText(state.obj, state.updatedAt) : status.textContent;
  status.className = configured ? "settings-status ok" : "settings-status";
  $("deepgram-actions").hidden = !configured;
  $("deepgram-save").disabled = !loaded || state.busy;
  $("deepgram-remove").disabled = state.busy;
  $("settings-reset-row").hidden = !state.unreadable;
  $("settings-reset").disabled = state.busy;
  if (!configured) hideRevealed();
  renderTranscription(configured); // ---- transcription (Task 35)
}

// ---- transcription (Task 35): shown but disabled, with a hint, while no key is set
function transcriptionMessage(text, kind = "info") {
  const m = $("transcription-msg");
  m.textContent = text;
  m.className = kind === "error" ? "settings-msg error" : "settings-msg";
  m.hidden = !text;
}

function renderTranscription(configured) {
  const off = !configured || state.busy;
  $("transcription-card").classList.toggle("is-disabled", !configured);
  $("transcription-hint").hidden = configured;
  $("transcribe-language").disabled = off;
  $("auto-transcribe").disabled = off;
  if (state.obj !== null && !state.busy) {
    const { language, auto } = transcriptionOf(state.obj);
    $("transcribe-language").value = language;
    $("auto-transcribe").checked = auto;
  }
}

async function saveTranscription() {
  if (state.obj === null || state.busy || !isConfigured(state.obj)) return;
  transcriptionMessage("");
  const next = withTranscription(state.obj, {
    language: $("transcribe-language").value, auto: $("auto-transcribe").checked,
  });
  await write(next, "Transcription settings saved", transcriptionMessage);
}

async function load() {
  const res = await api("GET", "/api/settings");
  state.loaded = true;               // the server answered once: a later 401 is the banner's, not a redirect
  state.rev = res.rev;               // known even if decryption fails, so a reset can target it
  state.updatedAt = res.updated_at;
  try {
    state.obj = res.enc_settings ? await openSettings(state.mk, res.enc_settings) : {};
    state.unreadable = false;
  } catch (e) {
    state.unreadable = e instanceof IntegrityError;
    throw e;
  }
  message("");
  hideRevealed();
  render();
}

async function loadOrExplain() {
  try {
    await load();
  } catch (e) {
    state.obj = null;
    render();
    if (e instanceof IntegrityError) {
      $("deepgram-status").textContent = "Couldn't decrypt";
      message("Couldn't decrypt the settings — they may be corrupt or were encrypted with another key.", "error");
    } else if (e instanceof ApiError && e.status === 401 && !state.loaded) {
      // The service worker opened the cached page, so a gone session shows here first: sign in again.
      await signInAgain();
    } else if (e instanceof ApiError && e.status === 401) {
      $("deepgram-status").textContent = "Session expired";   // banner.js shows "log in again"
    } else {
      $("deepgram-status").textContent = "Couldn't load";
      message("Couldn't load the settings. Reload the page to try again.", "error");
    }
  }
}

// Seal `next` under MK and PUT it with the rev we loaded. Returns true when it was stored.
async function write(next, done, say = message) {
  state.busy = true;
  render();
  try {
    const enc = await sealSettings(state.mk, next);
    const { rev } = await api("PUT", "/api/settings", { json: { enc_settings: enc, rev: state.rev } });
    // Stored. Show it at once; the reload only refreshes updated_at (and may itself fail).
    Object.assign(state, { obj: next, rev, updatedAt: new Date().toISOString(), unreadable: false });
    hideRevealed();
    toast(done);
    announceSaved(); // ---- transcription (Task 35): other tabs read the settings again
    await loadOrExplain();
    return true;
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      await loadOrExplain();
      say(CONFLICT_MSG, "error");
    } else if (!(e instanceof ApiError && e.status === 401)) {
      say(`Couldn't save the settings${e instanceof ApiError ? `: ${e.message}` : ""}.`, "error");
    }
    return false;
  } finally {
    state.busy = false;
    render();
  }
}

async function save(ev) {
  ev.preventDefault();
  if (state.obj === null || state.busy) return;
  message("");
  const input = $("deepgram-key");
  const { value, error } = checkKey(input.value);
  if (error) {
    message(error, "error");
    input.focus();
    return;
  }
  // On a conflict the typed key stays in the field, so "retry" is one more click on Save.
  if (await write(withKey(state.obj, value), "Deepgram key saved")) input.value = "";
}

function toggleReveal() {
  const out = $("deepgram-revealed");
  if (!out.hidden) {
    hideRevealed();
    return;
  }
  if (!isConfigured(state.obj)) return;
  out.textContent = state.obj[DEEPGRAM];
  out.hidden = false;
  $("deepgram-reveal").textContent = "Hide";
  $("deepgram-reveal").setAttribute("aria-expanded", "true");
}

async function remove() {
  if (state.obj === null || state.busy) return;
  message("");
  const ok = await confirmDialog({
    title: "Remove the Deepgram key?",
    body: "Audio isn't transcribed until you set a key again. The key itself stays valid at Deepgram — revoke it there if it may have leaked.",
    confirmLabel: "Remove",
    danger: true,
  });
  if (!ok) return;
  await write(withoutKey(state.obj), "Deepgram key removed");
}

// The way out when the settings can't be decrypted: replace them with an empty object at the
// current rev (a concurrent writer still gets a 409 like any other save).
async function reset() {
  if (!state.unreadable || state.busy) return;
  const ok = await confirmDialog({
    title: "Reset settings?",
    body: "This replaces the unreadable settings with empty ones; you'll need to re-enter your Deepgram key.",
    confirmLabel: "Reset settings",
    danger: true,
  });
  if (!ok) return;
  if (await write({}, "Settings reset")) message("");
}

async function main() {
  hydrateIcons();
  const keys = await loadKeys().catch(() => null);
  if (!keys) {
    location.replace("/login?next=/settings");
    return;
  }
  state.mk = keys.mk;
  $("deepgram-form").addEventListener("submit", save);
  $("deepgram-reveal").addEventListener("click", toggleReveal);
  $("deepgram-remove").addEventListener("click", remove);
  $("settings-reset").addEventListener("click", reset);
  $("transcribe-language").addEventListener("change", saveTranscription); // ---- transcription (Task 35)
  $("auto-transcribe").addEventListener("change", saveTranscription);
  mountPushCard();
  mountPairCard();
  await loadOrExplain();
}

if (typeof document !== "undefined") main();
