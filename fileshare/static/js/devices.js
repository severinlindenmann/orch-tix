import "./banner.js";
// fileshare/static/js/devices.js
import { api, ApiError } from "./api.js";
import { el, toast, confirmDialog, hydrateIcons } from "./ui.js";
import { relTime } from "./format.js";
import { approveDevice, rejectDevice, whileBusy } from "./approve.js";
import { withLocalFingerprints } from "./pending.js";
import { CLEAR_FAILED, clearLocalData, confirmDiscardOutbox } from "./outbox-ui.js";
import { loadSessionName } from "./browsersession.js";

const MAX_ROWS = 50;
const MISMATCH = {
  pending: "Fingerprint mismatch — do not approve",
  active: "Fingerprint mismatch — the server's record of this key changed; revoke it if unsure",
  revoked: "Fingerprint mismatch",
};

function dates(d) {
  return `onboarded ${relTime(d.created_at)} · last seen ${d.last_seen_at ? relTime(d.last_seen_at) : "never"}`;
}

// `d` has been through withLocalFingerprints: d.fingerprint is computed here from d.pubkey (R12).
function baseRow(d) {
  const r = el(
    "div",
    { class: "device-row", "data-device": d.id },
    el("div", { class: "device-main" }, el("span", { class: "device mono" }, d.name), el("span", { class: "project" }, d.project)),
    el("div", { class: "device-host mono" }, `${d.hostname || "—"} · ${d.platform || "?"}`),
    el("div", { class: "device-fp mono" }, d.fingerprint),
    el("div", { class: "device-dates" }, dates(d)),
    el("span", { class: `badge ${d.status}` }, d.status),
  );
  if (d.mismatch) r.append(el("p", { class: "fp-mismatch" }, MISMATCH[d.status] ?? MISMATCH.revoked));
  return r;
}

function pendingRow(d) {
  const r = baseRow(d);
  const actions = el("div", { class: "device-actions" });
  const reject = el("button", { class: "btn btn-danger", type: "button" }, "Reject");
  const approve = el("button", { class: "btn btn-accent", type: "button" }, "Approve");
  const act = (fn, verb) => () =>
    whileBusy([approve, reject], () => fn(d.server)).catch((e) => {
      if (!(e instanceof ApiError)) console.error(`${verb} ${d.id}`, e);
      toast(`Couldn't ${verb} ${d.name}`, "error");
    });
  reject.addEventListener("click", act(rejectDevice, "reject"));
  approve.addEventListener("click", act(approveDevice, "approve"));
  actions.append(reject);
  if (!d.mismatch) actions.append(approve);
  r.append(actions);
  return r;
}

// An active device is one compact row: name, project and when it was last seen, the status and an outline Revoke
// (a big filled red button next to a status badge was an accident waiting to happen). Host, fingerprint and the
// dates sit under "Details"; a pending device keeps its fingerprint in plain view, that is what is compared.
function activeRow(d) {
  const b = el("button", { class: "btn btn-danger-line", type: "button" }, "Revoke");
  b.addEventListener("click", () => revoke(d));
  return el("div", { class: "device-row device-row-active", "data-device": d.id },
    el("div", { class: "device-main" }, el("span", { class: "device mono" }, d.name),
      el("span", { class: "project" }, `${d.project} · last seen ${d.last_seen_at ? relTime(d.last_seen_at) : "never"}`),
      el("span", { class: `badge ${d.status}` }, d.status)),
    b,
    el("details", { class: "device-more" }, el("summary", {}, "Details"),
      el("div", { class: "device-host mono" }, `${d.hostname || "—"} · ${d.platform || "?"}`),
      el("div", { class: "device-fp mono" }, d.fingerprint),
      el("div", { class: "device-dates" }, dates(d))),
    d.mismatch ? el("p", { class: "fp-mismatch" }, MISMATCH[d.status] ?? MISMATCH.revoked) : null);
}

async function revoke(d) {
  const ok = await confirmDialog({
    title: `Revoke ${d.name}?`,
    body: `${d.name} (${d.project}) loses access immediately. Files it already downloaded stay on that machine.`,
    confirmLabel: "Revoke",
    danger: true,
  });
  if (!ok) return;
  try {
    await api("DELETE", `/api/devices/${d.id}`);
    toast(`${d.name} revoked`);
  } catch {
    toast(`Couldn't revoke ${d.name}`, "error");
  }
  await load();
}

export async function load() {
  const list = document.getElementById("devices-list");
  let devices;
  try {
    ({ devices } = await api("GET", "/api/devices"));
    devices = await withLocalFingerprints(devices);
  } catch (e) {
    const msg = e instanceof ApiError && e.status === 401 ? "Session expired — log in again." : "Couldn't load devices.";
    list.replaceChildren(el("p", { class: "empty" }, msg));
    return;
  }
  const recent = (a, b) => (b.last_seen_at || b.created_at).localeCompare(a.last_seen_at || a.created_at);
  const pending = devices.filter((d) => d.status === "pending").sort(recent);
  const active = devices.filter((d) => d.status === "active").sort(recent);
  const revoked = devices.filter((d) => d.status === "revoked");

  const children = [];
  if (pending.length) {
    children.push(
      el(
        "section",
        { class: "devices-pending", "aria-labelledby": "pending-title" },
        el("h2", { id: "pending-title" }, `Pending approval (${pending.length})`),
        el("p", { class: "hint" }, "Compare each fingerprint with the terminal on that device before approving."),
        ...pending.slice(0, MAX_ROWS).map(pendingRow),
      ),
    );
  }
  if (!active.length) children.push(el("p", { class: "empty" }, "No active devices — onboard one from the Files page."));
  active.slice(0, MAX_ROWS).forEach((d) => children.push(activeRow(d)));
  if (active.length > MAX_ROWS) children.push(el("p", { class: "hint" }, `and ${active.length - MAX_ROWS} more`));
  if (revoked.length) {
    children.push(
      el(
        "details",
        { class: "devices-revoked" },
        el("summary", {}, `Revoked (${revoked.length})`),
        ...revoked.slice(0, MAX_ROWS).map(baseRow),
      ),
    );
  }
  list.replaceChildren(...children);
}

// Spec §14 A: there is no endpoint listing other sessions, so only this browser is shown.
async function signOutEverywhere(btn) {
  const ok = await confirmDialog({
    title: "Sign out everywhere?",
    body: "Every browser and phone signed in to this share is signed out, this one included. "
      + "You'll need your passphrase to sign in again. Onboarded devices keep their access.",
    confirmLabel: "Sign out everywhere",
    danger: true,
  });
  if (!ok) return;
  if (!(await confirmDiscardOutbox())) return;
  btn.disabled = true;
  try {
    await api("POST", "/api/sessions/revoke-all");
  } catch (e) {
    // A 401 means this session is already gone: finish signing out locally.
    if (!(e instanceof ApiError && e.status === 401)) {
      toast(`Couldn't sign out everywhere: ${e.message}`, "error");
      btn.disabled = false;
      return;
    }
  }
  const done = await clearLocalData();
  const cleared = done.keys && done.outbox && done.lists;
  if (!cleared) toast(CLEAR_FAILED, "error");
  setTimeout(() => location.replace("/login"), cleared ? 0 : 2500);
}

export async function loadBrowsers() {
  const nameEl = document.getElementById("this-browser");
  if (!nameEl) return;
  const name = await loadSessionName();
  nameEl.textContent = name === null ? "—" : name || "unnamed";
}

if (typeof document !== "undefined") {
  hydrateIcons();
  const signOut = document.getElementById("signout-all");
  signOut?.addEventListener("click", () => signOutEverywhere(signOut));
  loadBrowsers();
  load();
  window.addEventListener("fs:devices-changed", load);
  // fs:unauthenticated (a 401, or approveDevice finding no keys) is handled by banner.js:
  // it clears the keys and shows "Session expired — log in again", as on the files page.
}
