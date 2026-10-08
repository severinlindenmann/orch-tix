// fileshare/static/js/remote-model.js: the words and fixed tables of the Remote pages. Pure: node tests it.
import { MESSAGES } from "./bridge-crypto.js";
import { previewKind } from "./previewkind.js";
import { safeDownloadName } from "./format.js";

// What the viewer shows and what a download hands over, from a host answer {path, headers, body} (frame-host.js). Text
// and structured kinds are capped small (they are parsed); an image or audio file is only played, so it gets the
// frame host's whole answer cap. HTML and SVG are never shown ("none"): previewKind refuses active formats.
export const VIEW_MAX = 2 * 1024 * 1024;
export const MEDIA_MAX = 8 * 1024 * 1024;
export const DOWNLOAD_MAX = 8 * 1024 * 1024;

export function fileName(view) {
  const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(String((view.headers || {})["content-disposition"] || ""));
  let name = view.path.split("?")[0].split("/").pop();
  if (m) { try { name = decodeURIComponent(m[1]); } catch { name = m[1]; } }
  return safeDownloadName(name, "file");
}

// -> {name, kind} (kind as previewKind) to show, or {name, reason: "none" | "large"} to say why not.
export function viewPlan(view, { markdown = false } = {}) {
  const name = fileName(view);
  let kind = previewKind({ name, mime: (view.headers || {})["content-type"] });
  if (kind.kind === "markdown" && !markdown) kind = { kind: "text" };   // the markdown renderer is the files page's library
  if (kind.kind === "audio" && !/^audio\/[a-z0-9.+-]{1,30}$/.test(kind.type)) kind = { kind: "none" };   // a sender's type only as a plain token
  if (kind.kind === "none") return { name, reason: "none" };
  if (view.body.length > (kind.kind === "image" || kind.kind === "audio" ? MEDIA_MAX : VIEW_MAX)) return { name, reason: "large" };
  return { name, kind };
}

// What the host may be asked, as the frame host's advisory scope table (frame-scope.js). The host decides again from
// its own router, so this is not the access control; it only keeps a hostile frame from asking for anything at all.
// ponytail: one broad GET/POST table; narrow it when the real dashboard's route list is fixed.
export const SCOPES = { rules: [
  { methods: ["GET"], pattern: "/" }, { methods: ["GET", "POST"], pattern: "/*" },
  { methods: ["GET"], pattern: "/events", stream: true }, { methods: ["GET"], pattern: "/api/events", stream: true },
] };

// A refusal is shown as the device's own fixed text for its code; nothing the host or the request said is echoed (§6.2).
export const REFUSAL_TEXT = Object.freeze({
  revoked: "This browser was removed from that computer. Pair it again from the computer's Remote tab.",
  not_paired: "That computer does not know this browser. Pair it again from the computer's Remote tab.",
  stopped: "The workspace was stopped on the computer.",
  scope_changed: "This browser's access changed on the computer. Open the workspace again.",
  forbidden_scope: "This browser is not allowed to do that on that computer.",
  busy: "The computer is busy. Try again in a moment.",
  assertion_required: "That action needs your phone or computer to confirm it, which this version cannot do yet.",
  lease_required: "That action needs your phone or computer to confirm it, which this version cannot do yet.",
  assertion_failed: "The confirmation was refused.",
  already_done: MESSAGES.outcomeUnknown,
  stale_timestamp: MESSAGES.clockWrong,
});
export const refusalText = (code) => REFUSAL_TEXT[code] || "The computer refused the request.";
export const HOST_SILENT = "The computer did not answer. Is it awake and running the workspace?";
export const SIGNED_OUT = "You are signed out. Sign in again to open a workspace.";

// Why a pairing did not work, for the refusal codes an answer to a `pair` request can carry (§8.1, §6.2).
export function pairRefusalText(code) {
  if (code === "pairing_closed") return MESSAGES.usedElsewhere;
  if (code === "stale_timestamp") return MESSAGES.clockWrong;
  return "The computer refused the pairing link. Make a new one on the computer.";
}

// The state line under a workspace that is not online, in words (presence.py states).
export function hostMessage(state) {
  switch (state) {
    case "online": return "";
    case "not_answering": return "Not answering. The computer missed its last check-ins; it may be asleep or offline. Opening is paused until it answers.";
    case "lost": return "Lost. The computer has not been seen for over five minutes. Start the workspace there again.";
    case "stopped": return "Stopped on the computer.";
    case "never_started": return "This workspace never started on a computer.";
    default: return "The state of this workspace is unknown.";
  }
}

// Opening needs a paired browser and a host that answers now.
export const canOpen = (space, paired) => space?.state === "online" && paired === true;
