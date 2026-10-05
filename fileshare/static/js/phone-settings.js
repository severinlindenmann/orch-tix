// fileshare/static/js/phone-settings.js — the TIX parts of Settings (TIX on orch-core): workspace
// join requests (a desktop asks to take over a space; only this browser session approves or denies,
// ruling TIX-J1) and the "Show ticket titles in notifications" switch (IndexedDB "prefs", off by
// default; the service worker reads it). The anchors #devices, #join and #pair scroll into view.
import { api, ApiError } from "./api.js";
import { PREFS, getValue, putValue } from "./db.js";
import { shortAge } from "./format.js";
import { loadKeys } from "./keystore.js";
import { openSpaceLabel } from "./mirror-crypto.js";
import { openTicket } from "./crypto.js";
import { mapLimit } from "./format.js";
import { archivedLine, archivedOnly } from "./mirror-model.js";
import { confirmDialog, el, icon, toast } from "./ui.js";

const $ = (id) => document.getElementById(id);

async function spaceLabels() {
  const out = new Map();
  try {
    const keys = await loadKeys();
    const { spaces = [] } = await api("GET", "/api/spaces");
    for (const s of spaces) {
      try {
        out.set(s.id, { label: await openSpaceLabel(keys.mk, s), owner: s.owner_name });
      } catch {
        out.set(s.id, { label: null, owner: s.owner_name });
      }
    }
  } catch {
    /* no labels: rows say "a workspace" */
  }
  return out;
}

async function decide(req, label, decision, buttons) {
  if (decision === "approve") {
    const ok = await confirmDialog({
      title: `Let ${req.device_name || "this device"} sync ${label}?`,
      body: `${req.device_name || "The device"} becomes the desktop for ${label}. The current desktop stops syncing it.`,
      confirmLabel: "Approve",
    });
    if (!ok) return;
  }
  for (const b of buttons) b.disabled = true;
  try {
    await api("POST", `/api/spaces/${req.space}/join-requests/${req.id}/${decision}`);
    toast(decision === "approve" ? `${req.device_name || "The device"} now syncs ${label}` : "Request denied", "ok");
  } catch (e) {
    toast(e instanceof ApiError && e.code === "expired" ? "This request expired. Ask again on the desktop."
      : `Couldn't ${decision} the request`, "error");
  }
  await loadJoins();
}

export async function loadJoins() {
  const list = $("join-list");
  if (!list) return;
  let requests;
  try {
    ({ requests = [] } = await api("GET", "/api/join-requests"));
  } catch {
    list.replaceChildren(el("p", { class: "muted" }, "Couldn't load requests."));
    if ($("join-card")) $("join-card").hidden = false;
    return;
  }
  const status = $("join-status");
  if (status) status.textContent = requests.length ? `${requests.length} waiting` : "";
  // an empty block is not shown on every visit; a request (or a load error above) is
  const card = $("join-card");
  if (card) card.hidden = !requests.length;
  if (!requests.length) {
    list.replaceChildren(el("p", { class: "muted" }, "No requests."));
    return;
  }
  const labels = await spaceLabels();
  list.replaceChildren(...requests.map((r) => {
    const info = labels.get(r.space);
    const label = info?.label || "a workspace";
    const approve = el("button", { type: "button", class: "btn btn-primary" }, "Approve");
    const deny = el("button", { type: "button", class: "btn" }, "Deny");
    approve.addEventListener("click", () => decide(r, label, "approve", [approve, deny]));
    deny.addEventListener("click", () => decide(r, label, "deny", [approve, deny]));
    return el("div", { class: "join-row", dataset: { request: r.id } },
      el("div", { class: "join-main" },
        el("span", { class: "pill r-you" }, icon("dot"), el("span", {}, "Waiting for you")),
        el("b", {}, `${r.device_name || "A device"} wants to sync ${label}`),
        el("span", { class: "muted" }, [info?.owner ? `now synced by ${info.owner}` : "", `asked ${shortAge(r.created_at)}`].filter(Boolean).join(" · "))),
      el("div", { class: "join-actions" }, approve, deny));
  }));
}

// The Archive: legacy tickets migrated to orch-core, decrypted here, read-only (no action of any kind).
export async function loadArchive() {
  const list = $("archive-list");
  if (!list) return;
  let rows;
  try {
    const keys = await loadKeys();
    const { tickets = [] } = await api("GET", "/api/tickets");
    rows = await mapLimit(archivedOnly(tickets), 4, async (t) => {
      try {
        const { content: c } = await openTicket(keys.mk, t);
        return { t, title: c.title, body: c.body };
      } catch {
        return { t, title: null, body: "" };
      }
    });
  } catch {
    list.replaceChildren(el("p", { class: "muted" }, "Couldn't load the archive."));
    return;
  }
  if (!rows.length) {
    list.replaceChildren(el("p", { class: "muted" }, "No archived tickets."));
    return;
  }
  if ($("archive-card")) $("archive-card").hidden = false;
  list.replaceChildren(...rows.map(({ t, title, body }) => el("details", { class: "archive-row", dataset: { n: String(t.n) } },
    el("summary", {}, el("b", {}, t.id), " ", title ?? "Couldn't decrypt this ticket",
      el("span", { class: "muted archive-when" }, archivedLine(t.archived_at))),
    body ? el("p", { class: "archive-body" }, body) : null)));
}

export async function mountTitlesSwitch() {
  const toggle = $("titles-toggle");
  if (!toggle) return;
  try {
    toggle.checked = (await getValue(PREFS, "show_titles")) === true;
  } catch {
    toggle.checked = false;
    toggle.disabled = true;
    return;
  }
  toggle.addEventListener("change", async () => {
    try {
      await putValue(PREFS, "show_titles", toggle.checked);
    } catch {
      toggle.checked = !toggle.checked;
      toast("Couldn't save this switch in this browser", "error");
    }
  });
}

function scrollToAnchor() {
  const id = location.hash.slice(1);
  if (!/^(devices|join|pair|notifications|archive)$/.test(id)) return;
  const adv = $(id)?.closest("details.adv");
  if (adv) adv.open = true;                      // pairing and the archive live in the Advanced fold
  $(id)?.closest("section")?.scrollIntoView({ block: "start" });
}

if (typeof document !== "undefined" && document.body?.classList.contains("page-settings")) {
  // Advanced is folded on a phone; a desktop has the room.
  if (globalThis.matchMedia?.("(min-width: 900px)").matches && $("advanced")) $("advanced").open = true;
  loadJoins();
  loadArchive();
  mountTitlesSwitch();
  scrollToAnchor();
  window.addEventListener("hashchange", scrollToAnchor);
}
