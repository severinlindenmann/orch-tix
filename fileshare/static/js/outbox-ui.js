// fileshare/static/js/outbox-ui.js — the outbox in the pages (spec §16). Imported by shell.js, so
// every signed-in page flushes; the Files page also shows the queue in #outbox-slot:
// "Waiting to upload · N" with Retry now, then one pending row per item (failed ones get Retry and
// Discard). Flush triggers: page start, `online`, the tab becoming visible, the backoff timer, and
// the service worker's Background Sync message. One tab flushes at a time (outbox.js's lock).
//
// Tickets (spec T10): answers, verdicts, comments and moves made offline are queued here too, as
// kind "ticket-event" holding the exact, already sealed POST body. An event whose voice note was
// queued with it (after_uuid) goes out after that file: its sealed body is opened with the ticket
// DEK, given the file's FILE id, and sealed again under the same event uuid.
//
// TIX on orch-core (Task 9): phone decisions are queued as kind "decision" (the sealed enc_body and
// its cleartext routing) and sent as POST /api/decisions; "fs:decisions-changed" tells the ticket
// page when one went out.
//
// Display names stay in memory. After a reload a row's name is decrypted from its enc_meta with
// the MK this browser already holds, else it reads "Encrypted item".
import { api, ApiError } from "./api.js";
import { IntegrityError, openFileMeta, openTicket, openTicketEvent, sealTicketEvent } from "./crypto.js";
import { clearKeys, loadKeys } from "./keystore.js";
import { wipeBridge } from "./bridge-wipe.js";
import { clearLists } from "./db.js";
import { confirmDialog, el, icon, toast } from "./ui.js";
import { humanSize, plainSize, shortAge } from "./format.js";
import { browserLock, createOutbox, decisionRequest, idbStore, OutboxFullError } from "./outbox.js";
import { transcribeAfterOutbox } from "./autotranscribe.js"; // ---- transcription (Task 35)
import { queuedLabel, withVoiceFile } from "./ticketreply-model.js"; // ---- tickets (spec T10)

export { OutboxFullError };
export const SYNC_TAG = "outbox-flush";
const CHANNEL = "fileshare-outbox";

const names = new Map(); // uuid -> display name, never persisted
// Real browsers only: under Node (the unit tests) a live BroadcastChannel keeps the event loop
// running forever on versions where it has no unref() (e.g. Node 22), so it's never opened outside
// a document.
const channel = typeof document !== "undefined" && typeof BroadcastChannel !== "undefined"
  ? new BroadcastChannel(CHANNEL) : null;
channel?.unref?.();
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// The "send" phase of an upload: exactly the request the online path makes. Throws ApiError.
export function sendPrepared(p) {
  const form = new FormData();
  const meta = { uuid: p.uuid, key_version: p.key_version, wrapped_dek: p.wrapped_dek, enc_meta: p.enc_meta, ttl: p.ttl };
  if (Array.isArray(p.tags) && p.tags.length) meta.tags = p.tags; // ---- tags (spec §19)
  form.append("meta", JSON.stringify(meta));
  form.append("blob", new Blob([p.blob], { type: "application/octet-stream" }), "blob.shr");
  return api("POST", "/api/files", { form });
}

// ---- tickets (spec T10): the ticket-event kind

export function sendTicketEvent(req, ref) {
  return api("POST", `/api/tickets/${encodeURIComponent(ref)}/events`, { json: req });
}

// The FILE id of a voice note that went up from the outbox (maybe from another tab, maybe answered
// 409 duplicate_uuid, so its id never reached this page), or null when it's gone.
async function voiceFileId(uuid) {
  const res = await api("GET", "/api/files?tag=ticket&acked=1&limit=200");
  const f = (res.files ?? []).find((x) => x.uuid === uuid && !x.deleted_at);
  return f ? f.id : null;
}

// A dependent event whose voice note is neither queued nor on the server (discarded from the Files
// page's queue, or expired): the event fails as voice_missing rather than silently losing the voice.
export class VoiceMissingError extends Error {
  constructor() { super("the voice note is missing"); this.name = "VoiceMissingError"; }
}

// The POST body to send for a queued ticket event: as stored, or re-sealed with its voice note's
// FILE id once that file is up. The id comes from the file item's own 201 (after_file, recorded by
// outbox.js); the list lookup is only the fallback for a 409 duplicate_uuid or another tab's flush.
export async function resolveTicketEvent(item) {
  if (!item.after_uuid) return item.body;
  const keys = await loadKeys();
  if (!keys) throw new ApiError(401, "unauthenticated", "sign in again");
  const fileId = item.after_file || (await voiceFileId(item.after_uuid));
  if (!fileId) throw new VoiceMissingError();
  const t = await api("GET", `/api/tickets/${encodeURIComponent(item.ref)}`);
  const { dek } = await openTicket(keys.mk, t);
  const { uuid, enc_body: enc, files: _files, ...rest } = item.body;
  const plain = await openTicketEvent(dek, t.uuid, uuid, enc);
  const body = withVoiceFile(plain, fileId);
  const sealed = await sealTicketEvent(dek, t.uuid, uuid, body);
  return { uuid, ...rest, enc_body: sealed, ...(body.voice ? { files: [fileId] } : {}) };
}

const sentTickets = new Set(); // refs whose queued events went out in this flush

// ---- TIX on orch-core (Task 9): the decision kind
const DECISION_LABEL = { answer: "Answer", approve: "Approval", request_changes: "Changes requested", verdict: "Verdict",
  comment: "Comment", ticket_request: "Ticket request" };

async function sendQueuedDecision(item) {
  const req = decisionRequest(item);
  try {
    await api(req.method, req.path, { json: req.json });
    return { status: 201 };
  } catch (e) {
    return e instanceof ApiError ? { status: e.status, code: e.code } : { status: 0 };
  } finally {
    window.dispatchEvent(new CustomEvent("fs:decisions-changed", { detail: { ticket: item.ticket } }));
  }
}

// Queues a sealed decision. Throws OutboxFullError like enqueue(); the same uuid twice is one item.
export async function enqueueDecision(rec) {
  await outbox.add({ ...rec, kind: "decision", size: String(rec.body || "").length,
    created_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z") });
  registerSync();
  try {
    outbox.schedule();
  } catch (e) {
    console.error("arming the outbox retry", e);
  }
}

// The decisions still queued in this browser for one ticket ("TIX-n"), oldest first.
export async function queuedDecisions(ticket) {
  try {
    return (await outbox.items()).filter((x) => x.kind === "decision" && (ticket == null || x.ticket === ticket));
  } catch {
    return [];
  }
}

async function sendQueuedTicketEvent(item) {
  try {
    await sendTicketEvent(await resolveTicketEvent(item), item.ref);
    sentTickets.add(item.ref);
    return { status: 201 };
  } catch (e) {
    if (e instanceof ApiError) {
      if (e.status === 409 && e.code === "duplicate_uuid") sentTickets.add(item.ref);
      return { status: e.status, code: e.code };
    }
    if (e instanceof IntegrityError) return { status: 400, code: "undecryptable" };
    if (e instanceof VoiceMissingError) return { status: 400, code: "voice_missing" };
    return { status: 0 };
  }
}

async function sendQueued(item) {
  if (typeof navigator !== "undefined" && navigator.onLine === false) return { status: 0 };
  if (item.kind === "ticket-event") return sendQueuedTicketEvent(item);
  if (item.kind === "decision") return sendQueuedDecision(item);
  try {
    const out = await sendPrepared(item);
    transcribeAfterOutbox(out, item); // ---- transcription (Task 35): queued audio, once it is up
    return { status: 201, id: out?.id }; // the id reaches a ticket event waiting for this file
  } catch (e) {
    return e instanceof ApiError ? { status: e.status, code: e.code } : { status: 0 };
  }
}

function persist() {
  try {
    navigator.storage?.persist?.().catch(() => {});
  } catch {
    /* not offered: the queue still works, the browser may just evict it under pressure */
  }
}

export const outbox = createOutbox({
  store: idbStore(),
  send: sendQueued,
  lock: browserLock(),
  persist,
  onChange: () => {
    render();
    channel?.postMessage({ type: "changed" });
    window.dispatchEvent(new CustomEvent("fs:outbox-changed"));
  },
  onDrained: (n) => {
    const refs = [...sentTickets];
    sentTickets.clear();
    toast(`Uploaded ${plural(n, "queued item")}`, "ok");
    window.dispatchEvent(new CustomEvent("fs:files-changed"));
    for (const ref of refs) window.dispatchEvent(new CustomEvent("fs:ticket-changed", { detail: { ref } }));
    channel?.postMessage({ type: "drained" });
  },
});

async function registerSync() {
  try {
    const reg = await navigator.serviceWorker?.ready;
    await reg?.sync?.register(SYNC_TAG);
  } catch {
    /* no Background Sync (Safari, Firefox): the page's own triggers cover it */
  }
}

// Queues a prepared upload. Throws OutboxFullError past 20 items or 300 MiB.
export async function enqueue(prepared, name) {
  names.set(prepared.uuid, name);
  try {
    await outbox.add(prepared);
  } catch (e) {
    names.delete(prepared.uuid);
    throw e;
  }
  // Queued is queued: nothing after the add may report it as lost.
  registerSync();
  try {
    outbox.schedule();
  } catch (e) {
    console.error("arming the outbox retry", e);
  }
}

// ---- tickets (spec T10): queues a sealed event request. `afterUuid`: the outbox item of the voice
// note it needs, which goes first. Throws OutboxFullError like enqueue().
export async function enqueueTicketEvent(req, ref, { afterUuid = null } = {}) {
  const rec = {
    uuid: req.uuid, kind: "ticket-event", ref, body: req, size: JSON.stringify(req).length,
    created_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"), ...(afterUuid ? { after_uuid: afterUuid } : {}),
  };
  await outbox.add(rec);
  registerSync();
  try {
    outbox.schedule();
  } catch (e) {
    console.error("arming the outbox retry", e);
  }
}

// The queued events of one ticket (the ticket view shows "queued" instead of a second form).
// Each carries `fileItem` ({seq, state, error}) when the voice note it waits for is still queued.
export async function queuedTicketEvents(ref) {
  try {
    const all = await outbox.items();
    return all.filter((x) => x.kind === "ticket-event" && x.ref === ref).map((ev) => {
      const f = ev.after_uuid ? all.find((x) => x.uuid === ev.after_uuid) : null;
      return { ...ev, fileItem: f ? { seq: f.seq, state: f.state, error: f.error } : null };
    });
  } catch {
    return [];
  }
}

// "Send without the voice": re-seals the queued event with its voice dropped (the ticket DEK is
// the open view's), discards the voice note's file item, and sends the event now.
export async function sendWithoutVoice(ev, { dek, uuid: ticketUuid }) {
  const { uuid, enc_body: enc, files: _files, ...rest } = ev.body;
  const plain = await openTicketEvent(dek, ticketUuid, uuid, enc);
  const sealed = await sealTicketEvent(dek, ticketUuid, uuid, withVoiceFile(plain, null));
  const file = (await outbox.items()).find((x) => x.uuid === ev.after_uuid);
  if (file) await outbox.discard(file.seq);
  await outbox.update(ev.seq, { body: { uuid, ...rest, enc_body: sealed }, after_uuid: undefined, after_file: undefined });
  await flushNow();
}

// "Discard comment": drops a queued event only. The recording is kept: the voice note's file item
// it waited for stays in the outbox and uploads to Files like any other upload (tried again now if
// it had failed; if it still can't, it waits in the Files page's queue). Resolves whether there was
// a recording to keep.
export async function discardQueuedEvent(ev) {
  const file = ev.after_uuid ? (await outbox.items()).find((x) => x.uuid === ev.after_uuid) : null;
  await outbox.discard(ev.seq);
  if (file?.state === "failed") await outbox.retry(file.seq);
  return Boolean(file);
}

// Retry a failed voice note (its event follows it).
export async function retryQueued(seq) {
  await outbox.retry(seq);
}

// Flushes now; after a 401 pause, only once this browser holds keys again (a new sign-in).
export async function flushNow() {
  if (outbox.status().paused) {
    if (await loadKeys().catch(() => null)) await outbox.resume();
    return;
  }
  await outbox.flush({ reset: true });
}

// Sign out and Sign out everywhere: resolves true when there is nothing queued or the user agreed
// to discard it (the caller then clears the outbox along with the keys).
export async function confirmDiscardOutbox() {
  let n = 0;
  try {
    n = await outbox.count();
  } catch {
    return true; // no readable outbox: nothing to lose
  }
  if (!n) return true;
  return confirmDialog({
    title: n === 1 ? "Discard 1 item that hasn't uploaded?" : `Discard ${n} items that haven't uploaded?`,
    body: "They are waiting in this browser for a connection. Signing out deletes them.",
    confirmLabel: "Discard and sign out",
    danger: true,
  });
}

export async function clearOutbox() {
  names.clear();
  await outbox.clear();
}

// Sign-out's local wipe: the keys first (they matter most), then the bridge database (device key, workspace keys,
// pinned host keys, sequence counters), then the outbox, then the last-known lists (sealed bodies, but they still say
// which tickets exist). Each has its own try, so one failing never skips another. Resolves {keys, bridge, outbox,
// lists}: true for each store cleared.
export async function clearLocalData({ keys = clearKeys, bridge = wipeBridge, queue = clearOutbox, lists = clearLists } = {}) {
  const done = { keys: false, bridge: false, outbox: false, lists: false };
  try {
    await keys();
    done.keys = true;
  } catch (e) {
    console.error("clearing stored keys on sign-out", e);
  }
  try {
    await bridge();
    done.bridge = true;
  } catch (e) {
    console.error("clearing the bridge keys on sign-out", e);
  }
  try {
    await queue();
    done.outbox = true;
  } catch (e) {
    console.error("clearing the outbox on sign-out", e);
  }
  try {
    await lists();
    done.lists = true;
  } catch (e) {
    console.error("clearing the cached lists on sign-out", e);
  }
  return done;
}

export const CLEAR_FAILED = "Couldn't clear the keys, queued uploads or cached lists stored in this browser. "
  + "Close all its windows to be sure they're gone.";

// ---- rendering

async function displayName(it, keys) {
  if (it.kind === "ticket-event") return queuedLabel(it.body, it.ref); // cleartext: the kind and the ID
  if (it.kind === "decision") return `${DECISION_LABEL[it.decisionKind] || "Decision"} for ${it.ticket || "a workspace"}`;
  if (names.has(it.uuid)) return names.get(it.uuid);
  if (!keys) return null;
  try {
    const { meta } = await openFileMeta(keys.mk, it);
    names.set(it.uuid, meta.name);
    return meta.name;
  } catch {
    return null;
  }
}

function pendingRow(it, name) {
  const failed = it.state === "failed";
  const event = it.kind === "ticket-event" || it.kind === "decision";
  const meta = event
    ? (failed ? `Couldn't send (${it.error || "refused"})` : `Waiting to send · ${shortAge(it.created_at)}`)
    : failed
    ? `Couldn't upload (${it.error || "refused"}) · ${humanSize(plainSize(it.size))}`
    : `Waiting to upload · ${humanSize(plainSize(it.size))} · ${shortAge(it.created_at)}`;
  const actions = failed
    ? el("span", { class: "frow-side outbox-actions" },
      el("button", { type: "button", class: "btn btn-small", dataset: { outbox: "retry" },
        onClick: () => outbox.retry(it.seq) }, "Retry"),
      el("button", { type: "button", class: "btn btn-small btn-danger-ghost", dataset: { outbox: "discard" },
        onClick: () => outbox.discard(it.seq).then(() => names.delete(it.uuid)) }, "Discard"))
    : null;
  return el("div", { class: `frow frow-pending${failed ? " frow-failed" : ""}`, dataset: { seq: String(it.seq), uuid: it.uuid } },
    el("span", { class: `tile ${failed ? "t-bad" : "t-other"}`, "aria-hidden": "true" }, icon(failed ? "alert" : "cloudOff")),
    el("span", { class: "frow-main" },
      el("span", { class: "fname-line" }, el("span", { class: "fname" }, name || "Encrypted item")),
      el("span", { class: "frow-meta" }, name || event ? meta : `Encrypted item · ${humanSize(plainSize(it.size))} · ${shortAge(it.created_at)}`)),
    actions);
}

let renderGen = 0;
export async function render() {
  const slot = typeof document !== "undefined" ? document.getElementById("outbox-slot") : null;
  if (!slot) return;
  const gen = ++renderGen;
  let items = [];
  try {
    items = await outbox.items();
  } catch {
    items = [];
  }
  const keys = items.some((x) => x.kind !== "ticket-event" && x.kind !== "decision" && !names.has(x.uuid)) ? await loadKeys().catch(() => null) : null;
  const rows = [];
  for (const it of items) rows.push(pendingRow(it, await displayName(it, keys)));
  if (gen !== renderGen) return;
  if (!items.length) {
    slot.replaceChildren();
    slot.hidden = true;
    return;
  }
  const { paused } = outbox.status();
  const retry = el("button", { type: "button", class: "btn btn-small", id: "outbox-retry" }, "Retry now");
  retry.addEventListener("click", () => flushNow());
  slot.replaceChildren(
    el("div", { class: "outbox-head", role: "status" },
      el("span", { class: "outbox-icon", "aria-hidden": "true" }, icon("cloudOff")),
      el("span", { class: "outbox-text" }, `Waiting to upload · ${items.length}`,
        paused ? el("span", { class: "outbox-sub" }, "Sign in again to send them") : null),
      retry),
    el("div", { class: "outbox-rows" }, rows));
  slot.hidden = false;
}

if (typeof document !== "undefined") {
  const start = () => {
    render();
    flushNow();
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
  window.addEventListener("online", () => flushNow());
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") flushNow();
  });
  navigator.serviceWorker?.addEventListener("message", (e) => {
    if (e.data && e.data.type === SYNC_TAG) flushNow();
  });
  if (channel) {
    channel.onmessage = (e) => {
      const t = e.data && e.data.type;
      if (t === "changed") {
        render();
        window.dispatchEvent(new CustomEvent("fs:outbox-changed"));
      }
      if (t === "drained") window.dispatchEvent(new CustomEvent("fs:files-changed"));
    };
  }
  setInterval(() => { // keeps the rows' ages current
    if (document.getElementById("outbox-slot")?.hidden === false) render();
  }, 60_000);
}
