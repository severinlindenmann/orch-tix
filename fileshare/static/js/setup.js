import { api, ApiError } from "./api.js";
import { AAD_MK, b64u, deriveRaw, importAesKey, seal } from "./crypto.js";
import { decodeRecovery, encodeRecovery } from "./recovery.js";
import { saveKeys } from "./keystore.js";
import { clearLists } from "./db.js";
import { el, toast } from "./ui.js";
import { deriveBrowserName } from "./browsername.js";

export const ITERATIONS = 600000;
export const MIN_LEN = 12;
const $ = (id) => document.getElementById(id);
// Once the share is set up, only a restore (same MK, new passphrase) is allowed; a "new" setup
// would mint a fresh MK and orphan every existing file. The server refuses it too.
let initialized = false;

export function validatePassphrase(pass, confirm) {
  if (pass.length < MIN_LEN) return `Use at least ${MIN_LEN} characters.`;
  if (pass !== confirm) return "Passphrases don't match.";
  return null;
}

export function strengthLabel(pass) {
  if (pass.length < MIN_LEN) return `${MIN_LEN - pass.length} more character${MIN_LEN - pass.length === 1 ? "" : "s"} needed.`;
  const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter((r) => r.test(pass)).length;
  if (pass.length >= 20 || (pass.length >= 14 && classes >= 3)) return "Strength: strong.";
  return "Strength: okay. Longer is better.";
}

function setBusy(btn, busy, label) {
  btn.disabled = busy;
  btn.querySelector(".spinner").hidden = !busy;
  btn.querySelector(".btn-label").textContent = label;
}

function showError(id, msg) {
  $(id).textContent = msg ?? "";
  $(id).hidden = !msg;
}

function messageFor(err) {
  if (err instanceof ApiError) {
    if (err.status === 403) return "That setup code is wrong, expired or already used. Print a new one on the server.";
    if (err.status === 429) return "Too many attempts. Wait 5 minutes and try again.";
    if (err.status === 0) return err.detail;
  }
  return `Setup failed: ${err.message}`;
}

// Wraps mkRaw under a fresh passphrase-derived KEK, registers it, and stores non-extractable keys locally.
async function enroll(setupCode, passphrase, mkRaw, mode) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const { authKey, kek } = await deriveRaw(passphrase, salt, ITERATIONS);
  const kekKey = await importAesKey(kek, false);
  kek.fill(0);
  const wrapped = await seal(kekKey, mkRaw, AAD_MK);
  try {
    await api("POST", "/api/setup", {
      json: {
        setup_code: setupCode,
        kdf_salt: b64u(salt),
        kdf_iterations: ITERATIONS,
        auth_key: b64u(authKey),
        wrapped_mk: b64u(wrapped),
        mode,
        name: deriveBrowserName(navigator),
      },
    });
  } finally {
    authKey.fill(0);
  }
  const blob = await api("GET", "/api/keyblob");
  const mkKey = await importAesKey(mkRaw, false);
  await clearLists().catch(() => {});        // last-known lists sealed for another key never outlive it
  await saveKeys({ kek: kekKey, mk: mkKey, keyVersion: blob.key_version });
}

function selectMode(mode) {
  const restore = mode === "restore" || initialized;
  $("tab-new").setAttribute("aria-selected", String(!restore));
  $("tab-restore").setAttribute("aria-selected", String(restore));
  $("setup-form").hidden = restore;
  $("restore-form").hidden = !restore;
}

function showRecovery(recoveryKey) {
  $("mode-tabs").hidden = true;
  $("already-note").hidden = true;
  $("setup-form").hidden = true;
  $("restore-form").hidden = true;
  $("recovery-key").textContent = recoveryKey;
  $("recovery-screen").hidden = false;
  const guard = (e) => {
    if (!$("stored").checked) {
      e.preventDefault();
      e.returnValue = "";
    }
  };
  window.addEventListener("beforeunload", guard);
  $("stored").addEventListener("change", () => { $("continue").disabled = !$("stored").checked; });
  $("copy-recovery").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(recoveryKey);
      toast("Recovery key copied");
    } catch {
      toast("Couldn't copy. Select the key and copy it by hand.", "error");
    }
  });
  $("download-recovery").addEventListener("click", () => {
    const text = [
      "tix.severin.io/share recovery key",
      `Created: ${new Date().toISOString()}`,
      "",
      recoveryKey,
      "",
      "Keep this offline. Anyone with this key and a copy of the encrypted files can read them.",
      "",
    ].join("\n");
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const a = el("a", { href: url, download: "fileshare-recovery-key.txt", class: "sr-only" });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  });
  $("continue").addEventListener("click", () => {
    window.removeEventListener("beforeunload", guard);
    location.replace("/");
  });
}

$("tab-new").addEventListener("click", () => selectMode("new"));
$("tab-restore").addEventListener("click", () => selectMode("restore"));
$("mode-tabs").addEventListener("keydown", (e) => {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  if (initialized) return;
  const next = $("tab-new").getAttribute("aria-selected") === "true" ? "restore" : "new";
  selectMode(next);
  $(next === "restore" ? "tab-restore" : "tab-new").focus();
});
$("new-pass").addEventListener("input", () => { $("strength").textContent = strengthLabel($("new-pass").value); });

$("setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (initialized) return;
  showError("setup-error", null);
  const code = $("setup-code").value.trim();
  const pass = $("new-pass").value;
  const problem = !code ? "Enter the setup code." : validatePassphrase(pass, $("new-pass2").value);
  if (problem) {
    showError("setup-error", problem);
    return;
  }
  const btn = $("setup-submit");
  setBusy(btn, true, "Deriving key…");
  const mkRaw = crypto.getRandomValues(new Uint8Array(32));
  try {
    await enroll(code, pass, mkRaw, "new");
    const recoveryKey = await encodeRecovery(mkRaw);
    showRecovery(recoveryKey);
  } catch (err) {
    showError("setup-error", messageFor(err));
    setBusy(btn, false, "Create key and finish setup");
  } finally {
    mkRaw.fill(0);
  }
});

$("restore-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  showError("restore-error", null);
  const code = $("restore-code").value.trim();
  const pass = $("restore-pass").value;
  const problem = !code ? "Enter the setup code." : validatePassphrase(pass, $("restore-pass2").value);
  if (problem) {
    showError("restore-error", problem);
    return;
  }
  let mkRaw;
  try {
    mkRaw = await decodeRecovery($("recovery").value.trim());
  } catch {
    showError("restore-error", "That recovery key doesn't check out. Look for a typo.");
    return;
  }
  const btn = $("restore-submit");
  setBusy(btn, true, "Deriving key…");
  try {
    await enroll(code, pass, mkRaw, "restore");
    location.replace("/");
  } catch (err) {
    showError("restore-error", messageFor(err));
    setBusy(btn, false, "Restore access");
  } finally {
    mkRaw.fill(0);
  }
});

api("GET", "/api/setup/status")
  .then((status) => {
    if (!status.initialized) return;
    initialized = true;
    $("tab-new").hidden = true;
    $("tab-new").disabled = true;
    $("already-note").hidden = false;
    selectMode("restore");
  })
  .catch(() => {});
