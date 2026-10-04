import "./swreg.js";
import { api, ApiError } from "./api.js";
import { AAD_MK, IntegrityError, b64u, deriveRaw, importAesKey, open, unb64u } from "./crypto.js";
import { clearKeys, saveKeys } from "./keystore.js";
import { clearLists } from "./db.js";
import { UnsafeKdfError, checkKdf, safeNext } from "./nav.js";
import { deriveBrowserName } from "./browsername.js";

const $ = (id) => document.getElementById(id);

function setBusy(busy, label) {
  const btn = $("login-submit");
  btn.disabled = busy;
  btn.querySelector(".spinner").hidden = !busy;
  btn.querySelector(".btn-label").textContent = label;
}

function showError(msg) {
  $("login-error").textContent = msg;
  $("login-error").hidden = !msg;
}

function messageFor(err) {
  if (err instanceof UnsafeKdfError) return "Can't sign in: the server returned unsafe key parameters.";
  if (err instanceof IntegrityError) {
    return "Signed in, but your key couldn't be unlocked. If you restored access recently, use the new passphrase.";
  }
  if (err instanceof ApiError) {
    if (err.status === 401) return "Wrong passphrase.";
    if (err.status === 429) return "Too many attempts. Wait 5 minutes and try again.";
    if (err.status === 0) return err.detail;
  }
  return `Login failed: ${err.message}`;
}

async function signIn(passphrase) {
  const kdf = await api("GET", "/api/kdf");
  const salt = checkKdf(kdf); // throws before deriving or sending anything
  setBusy(true, "Deriving key…");
  const { authKey, kek } = await deriveRaw(passphrase, salt, kdf.kdf_iterations);
  try {
    setBusy(true, "Signing in…");
    try {
      await api("POST", "/api/login", { json: { auth_key: b64u(authKey), name: deriveBrowserName(navigator) } });
    } finally {
      authKey.fill(0);
    }
    const blob = await api("GET", "/api/keyblob");
    const kekKey = await importAesKey(kek, false);
    kek.fill(0);
    const mkRaw = await open(kekKey, unb64u(blob.wrapped_mk), AAD_MK);
    try {
      const mkKey = await importAesKey(mkRaw, false);
      await clearLists().catch(() => {});      // last-known lists sealed for another key never outlive it
      await saveKeys({ kek: kekKey, mk: mkKey, keyVersion: blob.key_version });
    } finally {
      mkRaw.fill(0);
    }
  } finally {
    kek.fill(0);
  }
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  showError("");
  const passphrase = $("passphrase").value;
  if (!passphrase) {
    showError("Enter your passphrase.");
    return;
  }
  setBusy(true, "Checking…");
  try {
    await signIn(passphrase);
    location.replace(safeNext(new URLSearchParams(location.search).get("next")));
  } catch (err) {
    if (err instanceof ApiError && err.code === "not_initialized") {
      location.replace("/setup");
      return;
    }
    await clearKeys().catch(() => {});
    showError(messageFor(err));
    setBusy(false, "Log in");
    $("passphrase").select();
  }
});
