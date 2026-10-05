// fileshare/static/js/workspaces.js — the status page (/workspaces, Remote R9): every workspace this instance has,
// grouped by machine, with its state, what is happening, the needs-you count, paired devices and an Open or
// Snapshot action. The server sends clear status only; names are sealed and opened here with MK, and every
// string is drawn as text (el()/shown(), never innerHTML). It renders with every host off: state comes from
// the server's clock and the counts from the synced ticket snapshot. ?open=<space> is the placeholder the
// Open action goes to: the ticket snapshot for that workspace (the live view is a later ticket).
import { api } from "./api.js";
import { el, icon, shown } from "./ui.js";
import { keysOrLogin, loadMirrors, signInAgain } from "./mirrors-data.js";
import { openSpaceLabel } from "./mirror-crypto.js";
import { mapLimit } from "./format.js";
import { UNKNOWN_SPACE, cardTitle } from "./mirror-model.js";
import { SPACE_ID, actionFor, activity, groupByMachine, needsYou, openHref, seenText, stateOf } from "./workspaces-model.js";

const REFRESH_MS = 10_000;
const $ = (id) => document.getElementById(id);

const pill = (role, ic, text) => el("span", { class: `pill r-${role}` }, icon(ic), el("span", {}, text));

function needsEl(n) {
  if (n === null) return el("span", { class: "pill r-neu" }, "Needs you: unknown");
  return n ? pill("you", "dot", `${n} need${n === 1 ? "s" : ""} you`) : el("span", { class: "wrow-quiet" }, "Nothing needs you");
}

function devicesText(n) {
  return n === null ? "Paired devices: unknown" : `${n} paired device${n === 1 ? "" : "s"}`;
}

function rowEl(s, ctx) {
  const [role, ic, text] = stateOf(s);
  const what = activity(s);
  const seen = seenText(s, ctx.now);
  const nameId = `w-${s.id}`;
  return el("article", { class: "card wrow", "aria-labelledby": nameId, dataset: { space: s.id, state: s.state || "unknown" } },
    el("h3", { class: "wrow-name", id: nameId }, shown(s.label)),
    el("div", { class: "wrow-state" }, pill(role, ic, text), seen && s.state !== "online" ? el("span", { class: "hint" }, seen) : null),
    el("p", { class: "wrow-what" }, what.length ? what.join(" · ")
      : s.state === "online" || s.state === "not_answering" ? "Nothing reported running" : "No live data"),
    el("div", { class: "wrow-needs" }, needsEl(needsYou(s, ctx.rows))),
    el("span", { class: "wrow-devs hint" }, devicesText(ctx.devices)),
    el("a", { class: s.state === "online" ? "btn btn-accent wrow-act" : "btn wrow-act", href: openHref(s.id),
      "aria-label": `${actionFor(s)} ${s.label}` }, actionFor(s)));
}

function render(state) {
  const root = $("ws-list");
  const groups = groupByMachine(state.spaces, state.labels);
  $("ws-loading").hidden = true;
  $("ws-sub").textContent = state.spaces.length ? `${state.spaces.length} workspace${state.spaces.length === 1 ? "" : "s"}` : "";
  if (!groups.length) {
    root.replaceChildren(el("section", { class: "empty" }, el("h2", {}, "No workspaces yet."),
      el("p", { class: "hint" }, "Run the orch-tix addon in a workspace and it shows here.")));
    return;
  }
  root.replaceChildren(...groups.map((g, i) => el("section", { class: "wgroup", "aria-labelledby": `m-${i}` },
    el("h2", { class: "lab", id: `m-${i}` }, shown(g.machine)),
    el("div", { class: "wgroup-rows" }, g.spaces.map((s) => rowEl(s, state))))));
}

async function openLabels(mk, spaces, labels) {
  await mapLimit(spaces.filter((s) => !labels.has(s.id)), 4, async (s) => {
    try { labels.set(s.id, await openSpaceLabel(mk, s)); } catch { labels.set(s.id, null); }
  });
}

async function refresh(state) {
  try {
    const [pres, rows, devices] = await Promise.all([
      api("GET", "/api/presence"),
      loadMirrors(state.keys.mk).catch(() => null),
      api("GET", "/api/devices").then((b) => (b.devices || []).filter((d) => d.status === "active").length).catch(() => null),
    ]);
    await openLabels(state.keys.mk, pres.spaces, state.labels);
    Object.assign(state, { spaces: pres.spaces, now: pres.now, rows: rows && rows.filter(Boolean), devices });
    $("ws-error").hidden = true;
    if (state.open) renderOpen(state); else render(state);
  } catch (e) {
    if (e?.status === 401) return signInAgain();
    if (!state.spaces.length) {
      $("ws-loading").hidden = true;
      $("ws-error").querySelector("span").textContent = e?.status === 0 ? "You're offline. This list updates when you're back." : "Couldn't load your workspaces.";
      $("ws-error").hidden = false;
    }
  }
}

// ---- the placeholder behind Open: the synced ticket snapshot of one workspace
function renderOpen(state) {
  const s = state.spaces.find((x) => x.id === state.open);
  const root = $("ws-list");
  $("ws-loading").hidden = true;
  $("ws-sub").textContent = "";
  const label = s ? state.labels.get(s.id) || UNKNOWN_SPACE : "Workspace";
  $("ws-title").textContent = label;
  document.title = `${label} · tix`;
  const back = el("a", { class: "back-link", href: "/workspaces" }, icon("back"), "Workspaces");
  if (!s) {
    root.replaceChildren(back, el("section", { class: "empty" }, el("h2", {}, "No such workspace."),
      el("p", { class: "hint" }, "It may have been removed.")));
    return;
  }
  const [role, ic, text] = stateOf(s);
  const rows = (state.rows || []).filter((r) => r.space === s.id && r.doc);
  const seen = seenText(s, state.now);
  root.replaceChildren(back,
    el("div", { class: "wrow-state" }, pill(role, ic, text), seen ? el("span", { class: "hint" }, seen) : null),
    el("p", { class: "hint" }, "This is the last synced snapshot of the tickets. The live view arrives in a later update."),
    state.rows === null ? el("p", { class: "banner banner-error", role: "alert" }, "Couldn't load the tickets.")
      : rows.length ? el("div", { class: "wgroup-rows" }, rows.map((r) => el("a", { class: "card ncard-link", href: `/t/${r.n}` },
        el("span", { class: "ncard-row" }, el("b", { class: "ncard-key" }, r.doc.id || r.id),
          r.needs ? pill("you", "dot", "Needs you") : null),
        el("span", { class: "ncard-title" }, shown(cardTitle(r.doc, r.doc.id || r.id))),
        el("span", { class: "ncard-meta" }, typeof r.doc.status === "string" ? r.doc.status : "status unknown"))))
        : el("section", { class: "empty" }, el("h2", {}, "No tickets synced for this workspace yet.")));
}

async function start() {
  const keys = await keysOrLogin();
  if (!keys) return;
  const q = new URLSearchParams(location.search).get("open");
  const state = { keys, spaces: [], labels: new Map(), rows: undefined, devices: null, now: NaN, open: q && SPACE_ID.test(q) ? q : null };
  if (state.open) $("ws-title").textContent = "Workspace";
  $("ws-retry")?.addEventListener("click", () => refresh(state));
  await refresh(state);
  setInterval(() => { if (!document.hidden) refresh(state); }, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(state); });
}

if (typeof document !== "undefined" && document.body?.classList.contains("page-workspaces")) start();
