// fileshare/static/js/remote.js: /remote, the page that opens a paired workspace's dashboard (Remote R10). The list is
// the status page's (presence from the server, names opened here with MK); the dashboard of the chosen workspace is
// drawn by the sandboxed frame host (frame-host.js) over a transport that seals every request to that computer
// (remote-transport.js). Switching workspaces destroys the frame and its mailbox and builds new ones. A host that is
// not answering is said in words; a refusal (a revoked browser, say) is shown as the fixed text for its code.
import { api } from "./api.js";
import { el, icon, shown, toast } from "./ui.js";
import { keysOrLogin, signInAgain } from "./mirrors-data.js";
import { openSpaceLabel } from "./mirror-crypto.js";
import { mapLimit } from "./format.js";
import { UNKNOWN_SPACE } from "./mirror-model.js";
import { SPACE_ID, stateOf } from "./workspaces-model.js";
import { createFrameHost } from "./frame-host.js";
import { DeviceSession } from "./bridge-session.js";
import { deviceKey, forgetWorkspace, pinnedHostKey, workspaceRecord } from "./bridge-store.js";
import { deviceId } from "./bridge-crypto.js";
import { hexToBytes } from "./crypto.js";
import { createMailbox } from "./remote-mailbox.js";
import { bridgeTransport } from "./remote-transport.js";
import { askAssertion, sheetOpen } from "./unlock.js";
import { isNeverPage } from "./frame-render.js";
import { SCOPES, canOpen, hostMessage } from "./remote-model.js";

const REFRESH_MS = 10_000;
const NOT_REMOTE = "Agent widgets and artifacts are not available remotely. Open them on the computer.";
const $ = (id) => document.getElementById(id);

export async function openSession(workspace) {
  const rec = await workspaceRecord(workspace);
  const hostKey = await pinnedHostKey(workspace);
  if (!rec?.hostPub || !hostKey) return null;            // not paired in this browser
  const dk = await deviceKey();
  const session = new DeviceSession({ workspace, kWs: rec.kWs, keyVersion: rec.keyVersion, deviceId: await deviceId(hexToBytes(workspace), dk.pub),
    signKey: dk.privateKey, hostKey });
  session.credentialId = rec.credentialId || null;       // the platform credential registered at pairing (unlock.js)
  return session;
}

function notice(text) {
  const n = $("remote-notice");
  n.textContent = text || "";
  n.hidden = !text;
}

const pill = (role, ic, text) => el("span", { class: `pill r-${role}` }, icon(ic), el("span", {}, text));

function render(state) {
  $("ws-loading").hidden = true;
  const root = $("ws-list");
  if (!state.spaces.length) {
    root.replaceChildren(el("section", { class: "empty" }, el("h2", {}, "No workspaces yet."),
      el("p", { class: "hint" }, "Start a workspace on a computer and it shows here.")));
    return;
  }
  root.replaceChildren(...state.spaces.map((s) => {
    const label = state.labels.get(s.id) || UNKNOWN_SPACE;
    const [role, ic, text] = stateOf(s);
    const paired = state.paired.get(s.id) === true;
    const open = canOpen(s, paired);
    const why = !paired ? "This browser is not paired with this workspace. Open the pairing link from the computer here."
      : hostMessage(s.state);
    return el("article", { class: "card wrow", dataset: { space: s.id, state: s.state || "unknown", paired: String(paired) },
      "aria-current": state.selected === s.id ? "true" : null },
      el("h3", { class: "wrow-name" }, shown(label)),
      el("div", { class: "wrow-state" }, pill(role, ic, text)),
      why ? el("p", { class: "hint wrow-why" }, why) : null,
      el("button", { class: open ? "btn btn-accent wrow-act" : "btn wrow-act", type: "button", disabled: !open,
        "aria-label": `Open ${label}`, onclick: () => select(state, s.id) }, "Open"));
  }));
}

function closeCurrent(state) {
  state.host?.destroy();
  state.mailbox?.close();
  state.host = state.mailbox = null;
}

async function select(state, id) {
  closeCurrent(state);
  state.selected = id;
  state.refusal = null;
  notice("");
  history.replaceState(null, "", `/remote?space=${id}`);
  const s = state.spaces.find((x) => x.id === id);
  $("ws-title").textContent = state.labels.get(id) || UNKNOWN_SPACE;
  const session = await openSession(id).catch(() => null);
  if (!session || !canOpen(s, true)) { render(state); notice(session ? hostMessage(s?.state) : "This browser is not paired with this workspace."); return; }
  const mailbox = createMailbox(id);
  const transport = bridgeTransport({ session, mailbox, unlock: askAssertion, onRefusal: (code, text) => {
    state.refusal = text; notice(text);
    if (code === "not_paired") forgetWorkspace(id).then(() => { state.paired.set(id, false); render(state); }).catch(() => {});   // the host does not know us
  } });
  state.mailbox = mailbox;
  state.host = createFrameHost({ blocked: sheetOpen, mount: $("remote-frame"), transport, scopes: SCOPES, start: "/", title: `Dashboard of ${state.labels.get(id) || "a workspace"}`,
    viewer: (v) => { if (isNeverPage(v?.path || "")) { state.refusal = NOT_REMOTE; notice(NOT_REMOTE); } else toast("Opening files from the dashboard comes later.", "info"); },
    download: () => toast("Downloads from the dashboard come later.", "info"),
    notice: (n) => { if (n.text) notice(n.text); } });
  render(state);
}

async function openLabels(mk, spaces, labels) {
  await mapLimit(spaces.filter((s) => !labels.has(s.id)), 4, async (s) => {
    try { labels.set(s.id, await openSpaceLabel(mk, s)); } catch { labels.set(s.id, null); }
  });
}

async function refresh(state) {
  try {
    const pres = await api("GET", "/api/presence");
    await openLabels(state.keys.mk, pres.spaces, state.labels);
    await Promise.all(pres.spaces.map(async (s) => state.paired.set(s.id, (await workspaceRecord(s.id).catch(() => null))?.hostPub != null)));
    state.spaces = pres.spaces;
    const sel = state.spaces.find((s) => s.id === state.selected);
    if (sel && sel.state !== "online") notice(hostMessage(sel.state));
    else if (sel && !state.refusal) notice("");
    render(state);
  } catch (e) {
    if (e?.status === 401) return signInAgain();
    $("ws-loading").hidden = true;
    if (!state.spaces.length) $("ws-list").replaceChildren(el("p", { class: "hint" }, "Couldn't load your workspaces."));
  }
}

async function start() {
  const keys = await keysOrLogin();
  if (!keys) return;
  const state = { keys, spaces: [], labels: new Map(), paired: new Map(), selected: null, host: null, mailbox: null, refusal: null };
  await refresh(state);
  const q = new URLSearchParams(location.search).get("space");
  if (q && SPACE_ID.test(q)) await select(state, q);
  setInterval(() => { if (!document.hidden) refresh(state); }, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(state); });
}

if (typeof document !== "undefined" && document.body?.classList.contains("page-remote")) start();
