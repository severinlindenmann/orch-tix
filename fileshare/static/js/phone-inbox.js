// fileshare/static/js/phone-inbox.js — what the phone sends and receives besides decisions (spec §5.5, §8):
//   - Messages: messages to the human (/api/messages), opened with MK and shown as data, never instructions:
//     plain text (textContent only, no links or markup from the text), FILE links, the ticket. Ack removes one;
//     it then leaves the push count `c`.
//   - Ticket request: a sealed ticket_request decision for a workspace (title, body, optional voice note),
//     scoped to the space under MK as the CLI does. The desktop's orch-tix addon offers "Create in backlog".
import { api, ApiError } from "./api.js";
import { deriveBrowserName } from "./browsername.js";
import { shortAge } from "./format.js";
import { el, icon, toast } from "./ui.js";
import { enqueueDecision, OutboxFullError } from "./outbox-ui.js";
import { openRecorder } from "./recorder.js";
import { maxUpload, uploadFiles } from "./upload.js";
import { openMessage, sealTicketRequest } from "./mirror-crypto.js";
import { pairingFor, signDecision } from "./pairing.js";
import { rememberMine } from "./decision-send.js";
import { QUEUED_TEXT, SENT_PAIRED_TEXT, SENT_TEXT, outcomeText, UNKNOWN_SPACE, VOICE_TTL, buildTicketRequest, messageView, newDecisionId } from "./mirror-model.js";

const isoNow = () => new Date().toISOString().replace(/\.\d{3}Z$/, "Z");

// ---- Messages

export async function loadMessages(mk) {
  const { messages = [] } = await api("GET", "/api/messages?after=0&wait=0");
  return Promise.all(messages.filter((m) => m.to_kind === "human").map(async (m) => {
    let sealed = null;
    try {
      sealed = await openMessage(mk, m);
    } catch {
      sealed = null;
    }
    return { row: m, view: messageView(sealed, m) };
  }));
}

function messageCard({ row, view }, onAck) {
  const ack = el("button", { type: "button", class: "btn", dataset: { fkey: `ack:${row.id}` }, "aria-label": "Mark as read" }, "Mark as read");
  ack.addEventListener("click", async () => {
    ack.disabled = true;
    try {
      await api("POST", `/api/messages/${row.id}/ack`);
      onAck();
    } catch {
      ack.disabled = false;
      toast("Couldn't ack the message", "error");
    }
  });
  const ticket = view.ticket ? el("a", { href: `/t/${view.ticket.slice(4)}` }, view.ticket) : null;
  return el("article", { class: "card msg-card", dataset: { id: row.id } },
    el("div", { class: "msg-head" }, el("b", {}, `From ${view.from}`), ticket ? [" · ", ticket] : "",
      el("span", { class: "muted" }, ` · ${shortAge(row.created_at)}`)),
    view.text === null ? el("p", { class: "banner banner-decrypt" }, "This message doesn't open with this browser's key.")
      : el("p", { class: "msg-text" }, view.text),
    view.files.length ? el("ul", { class: "art-list" }, view.files.map((f) => el("li", {},
      el("a", { href: `/files?f=${encodeURIComponent(f)}` }, icon("files"), f)))) : "",
    el("div", { class: "msg-actions" }, ack));
}

// The Messages section of Needs you; null when there are none.
export function messagesSection(items, onAck) {
  if (!items.length) return null;
  return el("section", { class: "ws msgs", "aria-labelledby": "msgs-title" },
    el("header", { class: "ws-head" }, el("h2", { class: "ws-title", id: "msgs-title" }, `Messages · ${items.length}`)),
    el("p", { class: "hint" }, "From your agents. Shown as text only; nothing here runs or changes anything."),
    items.map((it) => messageCard(it, onAck)));
}

// ---- Ticket request

// The card keeps its inputs for its whole life; card.setSpaces(spaces) swaps the workspace options in place (a
// gone workspace is no longer selectable, the pick stays when it still exists), so a list refresh never drops
// what is being typed.
export function ticketRequestCard({ mk, keyVersion, spaces }) {
  const optionsOf = (map) => [...map.values()].map((s) => el("option", { value: s.id }, s.label || UNKNOWN_SPACE));
  const space = el("select", { class: "input", id: "req-space", "aria-label": "Workspace" }, optionsOf(spaces));
  const title = el("input", { class: "input", id: "req-title", type: "text", maxlength: "200", autocomplete: "off",
    "aria-label": "Title", placeholder: "What should the agent do?" });
  const body = el("textarea", { class: "input", id: "req-body", rows: "3", "aria-label": "Details", placeholder: "Details (optional)" });
  const voiceLine = el("p", { class: "voice-line", role: "status", hidden: true });
  const status = el("p", { class: "decision-status", role: "status", "aria-live": "polite" });
  const send = el("button", { type: "button", class: "btn btn-primary btn-big" }, "Send request");
  const mic = el("button", { type: "button", class: "icon-btn icon-btn-line mic-btn", "aria-label": "Record a voice note" }, icon("mic"));
  let voice = null, decisionId = null, busy = false, pairing = null;
  const paint = () => {
    send.disabled = busy || !title.value.trim() || !space.value;
    voiceLine.hidden = !voice;
    voiceLine.textContent = voice ? (voice.file ? `Voice note ${voice.file}` : "Voice note queued") : "";
  };
  title.addEventListener("input", paint);
  mic.addEventListener("click", async () => {
    let got = null;
    await openRecorder({
      upload: async (files) => {
        const out = await uploadFiles(files, "", VOICE_TTL, { kind: "recording", tags: ["voice"] });
        if (out && out.length) got = out[0];
        return out;
      },
      maxBytes: maxUpload,
      title: "Voice note for the ticket request",
    });
    if (got) voice = got.queued ? { file: null, transcript: "" } : { file: got.id, transcript: "" };
    paint();
  });
  send.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    paint();
    decisionId ||= newDecisionId();               // a double tap or a retry is the same request
    rememberMine(decisionId);
    try {
      const req = buildTicketRequest({ space: space.value, title: title.value, body: body.value, at: isoNow(), decisionId,
        voice: voice?.file ? voice : null });
      req.device = deriveBrowserName(navigator) || "phone";
      // A paired phone signs the request like any decision (pair + mac): core then creates the backlog ticket
      // directly; unsigned, the desktop shows it for a Create in backlog.
      pairing = await pairingFor(null, req.space).catch(() => null);
      const signed = pairing ? await signDecision(pairing, req) : req;
      const uuid = decisionId.slice(4);
      const enc = await sealTicketRequest(mk, req.space, uuid, signed);
      const item = { uuid, space: req.space, ticket: null, decisionKind: "ticket_request", key_version: keyVersion || 1, body: enc };
      let queued = navigator.onLine === false;
      if (!queued) {
        try {
          await api("POST", "/api/decisions", { json: { uuid, space: req.space, ticket: null, kind: "ticket_request",
            key_version: item.key_version, enc_body: enc } });
        } catch (e) {
          if (e instanceof ApiError && e.status === 409 && e.code === "duplicate_uuid") { /* already sent */ }
          else if (e instanceof ApiError && (e.status === 0 || e.status >= 500)) queued = true;
          else throw e;
        }
      }
      if (queued) await enqueueDecision(item);
      status.textContent = queued ? QUEUED_TEXT : pairing ? SENT_PAIRED_TEXT : SENT_TEXT;
      if (!queued) followRequest(req.space, `dec_${uuid}`, Boolean(pairing), Date.now());
      title.value = "";
      body.value = "";
      voice = null;
      decisionId = null;
    } catch (e) {
      toast(e instanceof OutboxFullError ? e.message : `Couldn't send: ${e?.detail || e?.message || "error"}`, "error");
    } finally {
      busy = false;
      paint();
    }
  });
  // The request's outcome (feedback fix round F1): its ack, read every 5 s for up to 10 minutes. Created, held with
  // the desktop's reason, or "Not applied yet" after two minutes without one.
  let follow = 0;
  const followRequest = (spaceId, id, paired, at) => {
    const mine = ++follow;
    const look = async () => {
      if (mine !== follow) return;                       // a newer request took over the line
      let ack = null;
      try {
        const { decisions = [] } = await api("GET", `/api/decisions?space=${encodeURIComponent(spaceId)}`);
        ack = decisions.find((d) => d.id === id)?.ack ?? null;
      } catch {
        ack = null;
      }
      const ageMs = Date.now() - at;
      status.textContent = outcomeText(ack, { paired, ageMs, request: true });
      const final = ack && !String(ack).startsWith("waiting-");
      if (!final && ageMs < 600000) setTimeout(look, 5000);
    };
    setTimeout(look, 3000);
  };
  paint();
  const card = el("section", { class: "card req-card", "aria-labelledby": "req-title-h" },
    el("h2", { class: "settings-title", id: "req-title-h" }, "Ticket request"),
    el("p", { class: "hint" }, "Ask for a new ticket. The desktop creates it in the backlog when you apply it there."),
    el("label", { class: "label", for: "req-space" }, "Workspace"), space,
    el("label", { class: "label", for: "req-title" }, "Title"), title,
    el("label", { class: "label", for: "req-body" }, "Details"), el("div", { class: "note-row" }, body, mic),
    voiceLine, send, status);
  card.setSpaces = (next) => {
    const was = space.value;
    space.replaceChildren(...optionsOf(next));
    if (was && next.has(was)) space.value = was;
    paint();
  };
  return card;
}
