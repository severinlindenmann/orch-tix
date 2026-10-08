// fileshare/static/js/workspaces-model.js — the status page's words and grouping (Remote, R9). Pure: node tests it.
// A value the server did not send is "unknown", never zero: every helper skips what is not an integer.
import { UNKNOWN_SPACE } from "./mirror-model.js";

// state -> [pill role, icon, text]. The text always says the state, so colour is never the only signal.
export const STATE = {
  online: ["ok", "dot", "Online"],
  not_answering: ["warn", "clock", "Not answering"],
  lost: ["err", "alert", "Lost"],
  stopped: ["neu", "ring", "Stopped"],
  never_started: ["neu", "ring", "Never started"],
};
export const UNKNOWN_STATE = ["neu", "ring", "Unknown"];
export const FACTORY = {
  none: "No Factory run", running: "Factory running", paused: "Factory paused", waiting: "Factory waiting on a permission",
  ready: "Factory ready", stopped: "Factory stopped", done: "Factory done",
};
// Lost = no heartbeat reached the server for 5 min; the Factory itself never looks at the relay, so claim nothing about it.
export const FACTORY_LOST = "Factory: host lost, nothing heard for 5 min. Nothing can be approved from here until it is back; what runs on the computer is unknown.";
export const FACTORY_WAITING_HINT = "TIX reports the Factory is waiting on a permission from you.";
const LIVE_FACTORY = new Set(["running", "paused", "waiting", "ready"]);
export const UNKNOWN_MACHINE = "A machine";

const int = (v) => Number.isInteger(v) && v >= 0;
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

export const stateOf = (s) => STATE[s?.state] || UNKNOWN_STATE;

// "2 min ago" from the server's own clock (now and last_seen are both its seconds), so a wrong phone clock
// never shows a host as seen in the future. "" when it never reported.
export function seenText(s, now) {
  if (!Number.isFinite(s?.last_seen) || !Number.isFinite(now)) return "";
  const sec = Math.max(0, Math.round(now - s.last_seen));
  if (sec < 45) return "seen just now";
  const m = Math.round(sec / 60);
  if (m < 60) return `seen ${m} min ago`;
  const h = Math.round(m / 60);
  return h < 48 ? `seen ${h} h ago` : `seen ${Math.round(h / 24)} d ago`;
}

// What is happening, as short phrases. Only an answering host's numbers are shown (a stopped or lost host's
// last counts are old); each number appears only when it is a real integer.
export function activity(s) {
  if (s?.state === "lost") return LIVE_FACTORY.has(s.factory) ? [FACTORY_LOST] : [];
  if (s?.state !== "online" && s?.state !== "not_answering") return [];
  const out = [];
  if (int(s.sessions) && s.sessions) out.push(plural(s.sessions, "session working", "sessions working"));
  if (int(s.in_progress) && s.in_progress) out.push(`${s.in_progress} in progress`);
  if (Object.hasOwn(FACTORY, s.factory) && s.factory !== "none") {
    let f = FACTORY[s.factory];
    if (int(s.children_done) && int(s.children_total)) f += ` · ${s.children_done} of ${s.children_total} done`;
    if (int(s.budget_pct)) f += ` · ${s.budget_pct}% of budget`;
    out.push(f);
  }
  return out;
}

// TIX's report (a heartbeat code) that the Factory waits on a permission.
export const factoryWaiting = (s) => (s?.state === "online" || s?.state === "not_answering") && s.factory === "waiting";

// The needs-you badge: the opened snapshot (works with every host off) when it loaded, else an answering
// host's own count, else null (unknown).
export function needsYou(s, rows) {
  if (Array.isArray(rows)) return rows.filter((r) => r.space === s.id && r.doc && r.needs).length;
  return s.state === "online" && int(s.needs_you) ? s.needs_you : null;
}

// Machines (the owner device's name) with their workspaces, both by name. labels: space id -> opened label.
export function groupByMachine(spaces, labels = new Map()) {
  const groups = new Map();
  for (const s of Array.isArray(spaces) ? spaces : []) {
    const machine = (typeof s.owner_name === "string" && s.owner_name.trim()) || UNKNOWN_MACHINE;
    if (!groups.has(machine)) groups.set(machine, { machine, spaces: [] });
    groups.get(machine).spaces.push({ ...s, label: labels.get(s.id) || UNKNOWN_SPACE });
  }
  const byName = (a, b) => a.localeCompare(b);
  return [...groups.values()].sort((a, b) => byName(a.machine, b.machine))
    .map((g) => ({ ...g, spaces: g.spaces.sort((a, b) => byName(a.label, b.label)) }));
}

// The row's action: Open (the live dashboard, /remote) while the host answers, Snapshot otherwise.
export const actionFor = (s) => (s?.state === "online" ? "Open" : "Snapshot");
export const openHref = (id, online) => `/${online ? "remote?space" : "workspaces?open"}=${encodeURIComponent(id)}`;
// The dashboard home in the frame, where the Factory cards are.
export const factoryHref = (id) => `/remote?space=${encodeURIComponent(id)}`;
export const SPACE_ID = /^[0-9a-f]{32}$/;
