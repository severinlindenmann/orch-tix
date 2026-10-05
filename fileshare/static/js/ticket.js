// fileshare/static/js/ticket.js — one mirrored ticket on the phone, /t/<n> (TIX on orch-core, spec
// §4.2, §10). The decision card comes first (id="decision", focused on #answer): answer the open
// questions, approve the plan or request changes with the gate hash the phone saw, or give the
// verdict; approve and done ask once more in a bottom confirm sheet that names the hash. The header
// carries the shared ticket-card encoding (move chip, progress strip); below the card: the decisions
// sent with their outcome, the ticket as collapsible chapters (Asked · Agreed · Doing · Proven ·
// Left), artifacts and (at redaction full with the log) the history. A decision is sealed under the ticket DEK and sent at once; offline it waits
// in the outbox (kind "decision") and keeps its id, so a second tap or a retry is the same decision.
// When this phone is paired with the space's desktop (pairing.js), the decision is signed first.
import { api } from "./api.js";
import { shortAge } from "./format.js";
import { confirmSheet, el, icon, shown, toast } from "./ui.js";
import { anyHidden } from "./textsafe.js";
import { outbox, OutboxFullError, queuedDecisions } from "./outbox-ui.js";
import { openRecorder } from "./recorder.js";
import { maxUpload, uploadFiles } from "./upload.js";
import { sendDecisions } from "./decision-send.js";
import { pairingFor } from "./pairing.js";
import { cacheLabels, cachedRow, keysOrLogin, loadCachedSpaces, loadDecisions, loadSpaces, openRow, signInAgain, watchMirrors } from "./mirrors-data.js";
import {
  NEEDS_LABEL, QUEUED_TEXT, SENT_PAIRED_TEXT, SENT_TEXT, UNKNOWN_SPACE, VERDICT_VALUE, VOICE_TTL, approvalGate, canApproveOnPhone,
  approveTogether, gateCovers, normalizedGateText, history, verdictCanonical, verdictHash, verdictView, decisionValue, notSentText, splitQueued, canSendAnswer, cardTitle,
  NOT_YET_MS, ageOf, artifactList, isRollback, verificationSummary, outcomeRole, outcomeText, shortHash, targetChanged, targetFor,
} from "./mirror-model.js";
import {
  acItems, agreedNote, byLabel, chapters, chipFor, costText, doneNote, idleNote, journey, mainPr, moreSections, pinnedImages,
  keySplit, planSteps, proofImage, receipts, approveVerb, TITLE_APPROVE_WHY,
} from "./ticket-card.js";
import { pinnedFigure } from "./images.js";
import { moveChipEl, needsPill, pill, stripEl } from "./needs.js";
import { cachedTicket, forgetTicket, loadKeyMap, offlineText, rememberTicket } from "./ticket-cache.js";
import { sectionBody } from "./widgets.js";

const VOICE_TAG = "voice";
const KIND_TEXT = { answer: "Answer", approve: "Approved", request_changes: "Changes requested", verdict: "Verdict",
  comment: "Comment", ticket_request: "Ticket request" };

const open = (q) => q && (q.answer === undefined || q.answer === null || q.answer === "");

// The draft of the card: the values picked, the note, the voice note, and the decision ids, which
// stay the same until the decision has gone out (a double tap or a retry reuses them).
function newDraft() {
  return { values: {}, note: "", voice: null, ids: {}, busy: false, status: null };
}

function el2(tag, cls, ...kids) { return el(tag, { class: cls }, ...kids); }

function hiddenCallout(consequence) {
  return el("p", { class: "callout r-err hidden-refused", role: "alert" }, icon("alert"),
    el("span", {}, `This text holds hidden characters, shown as <U+…>. Ask the agent to remove them; ${consequence}.`));
}

function optionLabel(q, o, i) {
  const key = o && typeof o === "object" ? o.key : String(o);
  const label = o && typeof o === "object" ? o.label ?? key : String(o);
  const rec = Array.isArray(q.recommended) ? q.recommended.includes(key) : q.recommended === key;
  return { key: String(key ?? i), label: String(label), cost: costText(o?.cost), rec };
}

function questionBlock(q, draft, onChange) {
  const name = `q-${q.id}`;
  const head = el2("div", "dq-head", el("b", {}, `${q.id} · `, shown(q.text || "Question")),
    q.blocking ? pill("warn", "alert", "blocking") : null);
  const parts = [head];
  if (q.why) parts.push(el("p", { class: "dq-why" }, "Why: ", shown(q.why)));
  // A hidden character in the question or its options: shown as a badge, and nothing is sent from here.
  if (anyHidden(q.text, q.why, q.options)) parts.push(hiddenCallout("this question can't be answered from the phone"));
  if (q.type === "text") {
    const ta = el("textarea", { class: "input", rows: "3", "aria-label": q.text || q.id, name });
    ta.value = draft.values[q.id] ?? "";
    ta.addEventListener("input", () => { draft.values[q.id] = ta.value; onChange(); });
    parts.push(ta);
  } else {
    const multi = q.type === "multi";
    const opts = (Array.isArray(q.options) && q.options.length ? q.options : q.type === "confirm"
      ? [{ key: "yes", label: "Yes" }, { key: "no", label: "No" }] : []).map((o, i) => optionLabel(q, o, i));
    const group = el("div", { class: `dq-opts${q.type === "confirm" ? " is-confirm" : ""}`, role: multi ? "group" : "radiogroup", "aria-label": q.text || q.id });
    for (const o of opts) {
      const input = el("input", { type: multi ? "checkbox" : "radio", name, value: o.key });
      const v = draft.values[q.id];
      input.checked = multi ? Array.isArray(v) && v.includes(o.key) : v === o.key;
      input.addEventListener("change", () => {
        if (multi) {
          const cur = new Set(Array.isArray(draft.values[q.id]) ? draft.values[q.id] : []);
          if (input.checked) cur.add(o.key); else cur.delete(o.key);
          draft.values[q.id] = [...cur];
        } else {
          draft.values[q.id] = o.key;
        }
        onChange();
      });
      group.append(el("label", { class: "dq-opt" }, input,
        el("span", { class: "dq-opt-text" }, el("span", { class: "dq-key" }, shown(o.key)), " ", shown(o.label),
          o.rec ? el("span", { class: "dq-rec" }, icon("check"), "recommended") : null,
          o.cost ? el("span", { class: "dq-cost" }, String(o.cost)) : null)));
    }
    parts.push(group);
  }
  return el("fieldset", { class: "dq", dataset: { qid: q.id } }, el("legend", { class: "sr-only" }, q.text || q.id), parts);
}

function noteField(draft, label, onChange) {
  const ta = el("textarea", { class: "input", rows: "2", id: "decision-note", placeholder: label, "aria-label": label });
  ta.value = draft.note;
  ta.addEventListener("input", () => { draft.note = ta.value; onChange(); });
  const mic = el("button", { type: "button", class: "icon-btn icon-btn-line mic-btn", "aria-label": "Record a voice note" }, icon("mic"));
  const voiceLine = el("p", { class: "voice-line", role: "status" });
  const paintVoice = () => {
    voiceLine.textContent = draft.voice ? (draft.voice.file ? `Voice note ${draft.voice.file}` : "Voice note queued") : "";
    voiceLine.hidden = !draft.voice;
  };
  paintVoice();
  mic.addEventListener("click", async () => {
    let got = null;
    await openRecorder({
      upload: async (files) => {
        const out = await uploadFiles(files, "", VOICE_TTL, { kind: "recording", tags: [VOICE_TAG] });
        if (out && out.length) got = out[0];
        return out;
      },
      maxBytes: maxUpload,
      title: "Voice note for the agent",
    });
    if (!got) return;
    draft.voice = got.queued ? { file: null, transcript: "" } : { file: got.id, transcript: "" };
    paintVoice();
    onChange();
  });
  return el2("div", "note-field", el2("div", "note-row", ta, mic), voiceLine);
}

// ---- the page

const state = { n: null, keys: null, row: null, doc: null, space: null, decisions: [], queued: [], failed: [], draft: newDraft(),
  mode: null, focusOnRender: false, keyHrefs: new Map(),
  // offline: {at} while the page shows the stored copy of the ticket (the network is out); actions are off
  offline: null };

function modeOf(row, doc) {
  if (!doc) return "none";
  const qs = (Array.isArray(doc.questions) ? doc.questions : []).filter(open);
  if (row.needs === "question") return qs.length ? "answer" : "keyonly";
  if (row.needs === "approval") return "approval";
  if (row.needs === "verdict") return "verdict";
  return qs.length ? "answer" : "none";
}

function statusLine() {
  const s = state.draft.status;
  const latest = latestFor(state.mode);
  let text = "", role = "info";
  if (state.queued.length) { text = QUEUED_TEXT; role = "neu"; }
  else if (latest) {
    const ageMs = ageOf(latest.created_at);
    text = outcomeText(latest.ack, { changed: latest.ack === "stale" && targetChanged(state.doc, latest.body), paired: state.paired, ageMs });
    role = outcomeRole(latest.ack, { ageMs });
    // no ack yet: look again when the two minutes are up (then "Not applied yet · open the desktop")
    if (latest.ack == null && ageMs <= NOT_YET_MS && !state.notYetTimer) {
      state.notYetTimer = setTimeout(() => { state.notYetTimer = null; render(); }, NOT_YET_MS - ageMs + 500);
    }
  } else if (s) { text = s; }
  // Paired, a valid decision applies at once (feedback round A). Unpaired, the desktop shows it for an Apply: say so.
  const held = latest && (latest.ack == null || latest.ack === "waiting-unpaired");
  const waiting = !state.queued.length && !state.paired && (held || (!latest && s === SENT_TEXT));
  const late = !state.queued.length && state.paired && latest && latest.ack == null && ageOf(latest.created_at) > NOT_YET_MS;
  return el("div", { class: `decision-status${text ? "" : " is-empty"}`, role: "status", "aria-live": "polite" },
    text ? pill(role, role === "ok" ? "check" : role === "warn" ? "alert" : "clock", text) : "",
    waiting ? el("p", { class: "hint" }, "Confirm it in Mission Control (Today, or this ticket). ",
      el("a", { href: "/settings#pair" }, "Pair this phone so answers apply directly")) : null,
    late ? el("p", { class: "hint" }, "Nothing back from the desktop yet. Open Mission Control; if this phone was unpaired there, pair it again in Settings.") : null);
}

// The newest decision sent for what the card shows now (the same question or gate hash).
function latestFor(mode) {
  const list = state.decisions.filter((d) => d.body);
  for (let i = list.length - 1; i >= 0; i -= 1) {
    const b = list[i].body;
    if (mode === "answer" && b.kind === "answer") return list[i];
    if (mode === "approval" && (b.kind === "approve" || b.kind === "request_changes")) return list[i];
    if (mode === "verdict" && b.kind === "verdict") return list[i];
  }
  return null;
}

async function send(kind, items) {
  const { row, doc, draft } = state;
  if (draft.busy || state.offline) return false;
  draft.busy = true;
  paintBar();
  let ok = false;
  try {
    const { queued } = await sendDecisions({ row, doc, kind, items, note: draft.note, voice: draft.voice, ids: draft.ids });
    draft.status = queued ? QUEUED_TEXT : state.paired ? SENT_PAIRED_TEXT : SENT_TEXT;
    if (!queued) draft.ids = {};
    ok = true;
  } catch (e) {
    toast(e instanceof OutboxFullError ? e.message : `Couldn't send: ${e?.detail || e?.message || "error"}`, "error");
  } finally {
    draft.busy = false;
  }
  await refreshDecisions();
  render();
  return ok;
}

function answerItems() {
  const qs = (state.doc.questions || []).filter(open);
  return qs.filter((q) => canSendAnswer(q, normalized(q)))
    .map((q) => ({ target: targetFor(state.doc, "answer", { qid: q.id }), value: decisionValue("answer", q, normalized(q)) }));
}

function normalized(q) {
  const v = state.draft.values[q.id];
  return q.type === "text" && typeof v === "string" ? v.trim() : v;
}

let bar = null;
function paintBar() {
  if (!bar) return;
  const { mode, draft } = state;
  const busy = draft.busy || state.queued.length > 0 || Boolean(state.offline);
  const btn = (label, cls, onClick, disabled = false) => el("button", { type: "button", class: `btn btn-big ${cls}`, disabled: disabled || busy ? "" : null, onClick }, label);
  let kids = [];
  if (mode === "answer") {
    kids = [btn("Send answer", "btn-primary", () => send("answer", answerItems()), answerItems().length === 0)];
  } else if (mode === "approval") {
    const gate = approvalGate(state.doc);
    const together = approvalGates(state.doc).length === 2;
    const target = gate && state.doc?.gates?.[gate]?.hash ? targetFor(state.doc, "approve", { gate, together }) : null;
    // Approve only where the phone shows the gate text (full); at title the text stays on the desktop
    // (final review I2) and only Request changes is offered. Together: one decision for requirements and plan.
    const approve = approvalGates(state.doc).every((g) => canApproveOnPhone(state.doc, g))
      ? [btn(together ? `${approveVerb(state.doc)} requirements and plan` : `${approveVerb(state.doc)} ${gate || "plan"}`, "btn-primary", () => confirmApprove(gate, target),
        !target || !gateVerified(state.doc))] : [];
    kids = [el2(approve.length ? "div" : "div", approve.length ? "bar-grid" : "bar-one", ...approve,
      btn("Request changes", "", () => send("request_changes", [{ target: targetFor(state.doc, "request_changes", { gate }), value: draft.note.trim() }]),
        !target || !draft.note.trim()))];
  } else if (mode === "verdict") {
    // Epic verdicts are desktop-only. Without the verdict hash core refuses a phone verdict, so none is offered.
    // Done needs the criteria and evidence the hash covers on screen and checked; Send back only the hash.
    const v = verdictView(state.doc);
    const hash = verdictHash(state.doc);
    if (v.reason !== "epic" && hash) {
      const done = v.ok || v.reason === "hidden"
        ? [btn("Done", "btn-primary", () => confirmDone(), !verdictVerified(state.doc))] : [];
      kids = [el2("div", done.length ? "bar-grid" : "bar-one", ...done,
        btn("Send back", "", () => send("verdict", [{ target: targetFor(state.doc, "verdict"), value: VERDICT_VALUE.send_back }]), !draft.note.trim()))];
    }
  }
  const hint = state.offline ? "Offline: actions are off until you are back" : mode === "approval" ? "If the plan changes first, nothing is approved" : mode === "none" || mode === "keyonly" ? "" : "Applies when your desktop picks it up";
  bar.replaceChildren(...kids, hint ? el("p", { class: "bar-hint" }, hint) : "");
  bar.hidden = kids.length === 0;
}

// ---- the gate check: the phone hashes what it shows (the parts gates.<g>.covers names, normalized as orch-core
// does) and offers Approve only when that is the gate's hash. No covers (an older orch-core), a covered part the
// doc lacks, or a different hash: nothing to approve here, Request changes still works.
async function sha256Hex(text) {
  const d = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
  return Array.from(d, (b) => b.toString(16).padStart(2, "0")).join("");
}

// The check's cache key is the very text that was hashed (and the hash it must equal), never a revision number.
function gateKey(doc, gate) {
  const cov = gateCovers(doc, gate);
  return `${gate}\n${doc?.gates?.[gate]?.hash || ""}\n${cov.ok ? normalizedGateText(cov.parts) : `!${cov.reason}`}`;
}

// The gates one approval binds: requirements and plan together (schema 1.4 `together`), else the one gate.
function approvalGates(doc) {
  const gate = approvalGate(doc);
  return gate === "requirements" && approveTogether(doc) ? ["requirements", "plan"] : [gate];
}

function approvalKey(doc) {
  return approvalGates(doc).map((g) => gateKey(doc, g)).join("\n\n");
}

function checkGate() {
  const doc = state.doc, gates = approvalGates(doc);
  const key = approvalKey(doc);
  if (state.gateCheck?.key === key) return;
  state.gateCheck = { key, ok: null };
  if (!gates.every((g) => canApproveOnPhone(doc, g))) { state.gateCheck.ok = false; return; }
  Promise.all(gates.map((g) => sha256Hex(normalizedGateText(gateCovers(doc, g).parts))
    .then((hex) => `sha256:${hex}` === doc.gates[g].hash))).then((oks) => {
    if (state.gateCheck?.key !== key) return;
    state.gateCheck.ok = oks.every(Boolean);
    render();
  }, () => { if (state.gateCheck?.key === key) { state.gateCheck.ok = false; render(); } });
}

// Approve may be pressed: full doc, covers, and every hash it binds checked against what is shown.
function gateVerified(doc) {
  return approvalGates(doc).every((g) => canApproveOnPhone(doc, g)) && state.gateCheck?.key === approvalKey(doc)
    && state.gateCheck.ok === true;
}

// The gate's confirm sheet names what is approved: the step count and the hash the phone shows. A hash that
// changed while the sheet was open (the desktop pushed a new text) sends nothing: review again.
async function confirmApprove(gate, target) {
  if (!target || state.draft.busy || state.offline) return;
  if (!gateVerified(state.doc)) return;
  const together = Boolean(target.plan_hash);
  const what = together ? "the requirements and the plan" : `the ${gate}`;
  const steps = gate === "plan" || together ? planSteps(state.doc) : 0;
  const agent = state.doc?.claim?.harness || "The agent";
  const meta = gateCovers(state.doc, gate).parts.filter((p) => p.kind === "meta").map((p) => `${p.name} ${p.text || "not set"}`);
  const ok = await confirmSheet({
    title: together ? "Approve requirements and plan?" : `Approve the ${gate}?`,
    body: `Approve ${what}${steps ? ` (${steps} step${steps === 1 ? "" : "s"})` : ""} exactly as shown${meta.length ? `, with ${meta.join(" and ")}` : ""}. ${agent} can start right after; if the text changes first, nothing is approved.`,
    detail: together ? `requirements · ${shortHash(target.hash)} · plan · ${shortHash(target.plan_hash)}` : `${gate} · ${shortHash(target.hash)}`,
    confirmLabel: "Confirm approval",
  });
  if (!ok) return;
  const now = approvalGate(state.doc) === gate && state.mode === "approval"
    ? targetFor(state.doc, "approve", { gate, together: together && approveTogether(state.doc) }) : null;
  if (!now || JSON.stringify(now) !== JSON.stringify(target) || !gateVerified(state.doc)) {
    toast(`The ${together ? "requirements or plan" : gate} changed while you looked: review it again`, "error");
    render();
    return;
  }
  await send("approve", [{ target, value: null }]);
}

// The verdict's target (its testing round) is the one the sheet was opened for: a new round, or the ticket
// leaving testing, while the sheet is open sends nothing.
// ---- the verdict check: the phone hashes what it shows (id, status, Acceptance criteria, Verification, orch-core's
// canonical JSON) and offers Done only when that is the document's verdict hash.
function verdictKey(doc) {
  return `${verdictHash(doc) || ""}\n${verdictCanonical(doc)}`;
}

function checkVerdict() {
  const doc = state.doc;
  const key = verdictKey(doc);
  if (state.verdictCheck?.key === key) return;
  state.verdictCheck = { key, ok: null };
  if (!verdictView(doc).ok) { state.verdictCheck.ok = false; return; }
  sha256Hex(verdictCanonical(doc)).then((hex) => {
    if (state.verdictCheck?.key !== key) return;
    state.verdictCheck.ok = `sha256:${hex}` === verdictHash(doc);
    render();
  }, () => { if (state.verdictCheck?.key === key) { state.verdictCheck.ok = false; render(); } });
}

function verdictVerified(doc) {
  return verdictView(doc).ok && state.verdictCheck?.key === verdictKey(doc) && state.verdictCheck.ok === true;
}

async function confirmDone() {
  if (state.draft.busy || state.offline || state.mode !== "verdict" || !verdictVerified(state.doc)) return;
  const key = state.doc?.id || state.row?.id;
  const target = targetFor(state.doc, "verdict");
  const ok = await confirmSheet({
    title: `Accept ${key} as done?`,
    body: "The ticket moves to done when your desktop picks it up. Send back instead if something is missing.",
    confirmLabel: "Confirm done",
  });
  if (!ok) return;
  if (state.mode !== "verdict" || !verdictVerified(state.doc)
      || JSON.stringify(targetFor(state.doc, "verdict")) !== JSON.stringify(target)) {
    toast("The ticket changed while you looked: review it again", "error");
    render();
    return;
  }
  await send("verdict", [{ target, value: VERDICT_VALUE.done }]);
}

function decisionCard() {
  const { mode, doc, row, draft } = state;
  const onChange = () => paintBar();
  const kids = [];
  if (mode === "answer") {
    const qs = doc.questions.filter(open);
    kids.push(el2("div", "decision-head", needsPill(row), el("span", { class: "muted" }, NEEDS_LABEL.question)));
    for (const q of qs) kids.push(questionBlock(q, draft, onChange));
    kids.push(noteField(draft, "Note for the agent (optional)", onChange));
  } else if (mode === "keyonly") {
    kids.push(el2("div", "decision-head", needsPill(row)),
      el("p", {}, cardTitle({ id: doc.id }, doc.id)),
      el("p", { class: "hint" }, "This workspace shares only the key. Answer on the desktop."));
  } else if (mode === "approval") {
    const gate = approvalGate(doc);
    const gates = approvalGates(doc);
    kids.push(el2("div", "decision-head", pill("you", "dot", gates.length === 2 ? `${approveVerb(doc)} requirements and plan` : `${approveVerb(doc)} ${gate || "plan"}`),
      el("span", { class: "muted" }, NEEDS_LABEL.approval)));
    if (doc.redaction !== "full") kids.push(el("p", { class: "callout r-info approve-why", role: "status" }, icon("alert"), el("span", {}, TITLE_APPROVE_WHY)));
    for (const gname of doc.redaction === "full" ? gates : []) {
      const cov = gateCovers(doc, gname);
      if (gates.length === 2) kids.push(el("h3", { class: "sect gate-group" }, gname === "plan" ? "Plan" : "Requirements"));
      if (!cov.ok && cov.reason !== "hidden") {
        kids.push(el("p", { class: "callout r-warn gate-refused", role: "status" }, icon("alert"), el("span", {}, cov.reason === "no-covers"
          ? "This desktop doesn't say what the approval covers (update orch-core). Approve on the desktop."
          : "Part of what the approval covers didn't reach the phone. Approve on the desktop.")));
        continue;
      }
      if (cov.reason === "hidden") kids.push(hiddenCallout("nothing can be approved here until then"));
      // everything the hash binds, in hash order: the sections, then the frontmatter keys (size, type)
      for (const p of cov.parts.filter((x) => x.kind === "section")) {
        kids.push(el("section", { class: "gate-text" }, el("h3", { class: "gate-name" }, shown(p.name), " · full text"),
          p.text.trim() ? el("div", { class: "section-text" }, shown(p.text)) : el("p", { class: "muted" }, "(empty)")));
      }
      const meta = cov.parts.filter((x) => x.kind === "meta");
      if (meta.length) {
        kids.push(el("section", { class: "gate-text gate-meta" }, el("h3", { class: "gate-name" }, "Also bound by this approval"),
          el("ul", { class: "kv-list" }, meta.map((p) => el("li", {}, el("span", { class: "muted" }, `${p.name[0].toUpperCase()}${p.name.slice(1)}: `),
            el("b", {}, p.text ? shown(p.name === "size" ? p.text.toUpperCase() : p.text) : "not set"))))));
      }
    }
    if (doc.redaction === "full" && state.gateCheck?.key === approvalKey(doc) && state.gateCheck.ok === false
        && gates.every((g) => gateCovers(doc, g).ok)) {
      kids.push(el("p", { class: "callout r-err gate-refused", role: "alert" }, icon("alert"),
        el("span", {}, "The text on this phone doesn't match what the approval binds. Approve on the desktop.")));
    }
    for (const gname of gates) {
      const g = gname ? doc.gates?.[gname] : null;
      if (g) kids.push(el2("div", "hash", el("span", {}, el("b", {}, gname), " · seen now"), el("span", { class: "mono" }, shortHash(g.hash))));
    }
    kids.push(noteField(draft, "What should change? (for Request changes)", onChange));
  } else if (mode === "verdict") {
    kids.push(el2("div", "decision-head", pill("you", "dot", "Ready for your verdict")));
    const v = verdictView(doc);
    if (v.reason === "epic") {
      kids.push(el("p", { class: "hint" }, "An epic's verdict covers all its children: give it on the desktop."));
    } else if (v.reason === "no-hash") {
      kids.push(el("p", { class: "callout r-warn gate-refused", role: "status" }, icon("alert"),
        el("span", {}, "This desktop doesn't send the verdict hash yet (update orch-core). Give the verdict on the desktop.")));
      if (verificationSummary(doc)) kids.push(el("p", {}, shown(verificationSummary(doc))));
    } else if (v.parts.length) {
      if (v.reason === "hidden") kids.push(hiddenCallout("nothing can be accepted here until then"));
      // exactly what the verdict hash covers: this ticket's criteria and evidence (plus its id and status, above)
      for (const p of v.parts) {
        kids.push(el("section", { class: "gate-text" }, el("h3", { class: "gate-name" }, `${p.name} · full text`),
          p.text.trim() ? el("div", { class: "section-text" }, shown(p.text)) : el("p", { class: "muted" }, "(empty)")));
      }
      if (state.verdictCheck?.key === verdictKey(doc) && state.verdictCheck.ok === false && v.ok) {
        kids.push(el("p", { class: "callout r-err gate-refused", role: "alert" }, icon("alert"),
          el("span", {}, "The criteria and evidence on this phone don't match what the verdict binds. Decide on the desktop.")));
      }
      kids.push(el2("div", "hash", el("span", {}, el("b", {}, "verdict"), ` · ${doc.status}`), el("span", { class: "mono" }, shortHash(verdictHash(doc)))));
    } else {
      kids.push(el("p", {}, shown(verificationSummary(doc) || "Check the result on the desktop.")));
      kids.push(el("p", { class: "hint" }, "Accept on the desktop: the criteria and evidence stay there. Send back works here."));
    }
    if (v.reason !== "epic" && v.reason !== "no-hash") kids.push(noteField(draft, "What is missing? (for Send back)", onChange));
  } else {
    kids.push(el("p", { class: "muted" }, "Nothing needs you on this ticket."));
  }
  kids.push(statusLine(), ...state.failed.map(failedRow));
  return el("article", { class: `card decision${mode === "none" ? "" : " is-you"}`, id: "decision", tabindex: "-1", "aria-label": "Decision" }, kids);
}

// A decision the server refused (4xx): it never blocks the bar; Discard drops it from this phone.
function failedRow(item) {
  const discard = el("button", { type: "button", class: "btn" }, "Discard");
  discard.addEventListener("click", async () => {
    discard.disabled = true;
    try {
      await outbox.discard(item.seq);
    } catch {
      toast("Couldn't discard it in this browser", "error");
    }
    await refreshDecisions();
    render();
  });
  return el("div", { class: "not-sent", role: "alert", dataset: { seq: String(item.seq) } },
    pill("warn", "alert", notSentText(item)), discard);
}

function sentList() {
  // Shorter words than the card's status line, so each text is on the page once.
  const rows = [...state.queued.map((q) => ({ kind: q.decisionKind, text: "Queued", role: "neu", at: q.created_at })),
    ...state.decisions.slice().reverse().map((d) => ({ kind: d.kind,
      text: d.ack ? outcomeText(d.ack, { changed: d.ack === "stale" && targetChanged(state.doc, d.body) })
        : ageOf(d.created_at) > NOT_YET_MS ? "Not applied yet" : state.paired ? "Applying on your desktop" : "Waiting for the desktop",
      role: outcomeRole(d.ack, { ageMs: ageOf(d.created_at) }), at: d.created_at }))];
  if (!rows.length) return null;
  return el("section", { class: "tsection", "aria-labelledby": "sent-title" },
    el("h2", { class: "sect", id: "sent-title" }, "Sent from this phone"),
    el("ul", { class: "sent-list" }, rows.map((r) => el("li", {},
      el("span", {}, `${KIND_TEXT[r.kind] || r.kind} · ${shortAge(r.at)}`),
      pill(r.role, r.role === "ok" ? "check" : r.role === "warn" ? "alert" : "clock", r.text)))));
}

const STATE_WORD = { done: "done", doing: "in progress", you: "waits for you", todo: "not yet" };
const TASK_MARK = { done: ["ok", "✓"], doing: ["info", "◐"], blocked: ["warn", "▲"], skipped: ["neu", "–"], todo: ["neu", "○"] };

// "02.10 09:24" in this browser's time, or "".
function whenText(iso) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const d = new Date(t), two = (x) => String(x).padStart(2, "0");
  return `${two(d.getDate())}.${two(d.getMonth() + 1)} ${two(d.getHours())}:${two(d.getMinutes())}`;
}

// ---- v4: the journey as five segments with words; who agreed is named only from a signed ledger, which the mirror
// does not carry, so core's own "not signed here" wording
function journeyEl(doc) {
  const stages = journey(doc);
  const word = { done: "done", now: "now", todo: "not yet" };
  const agreed = stages[1].state === "done" ? agreedNote(doc) : "";
  return el("div", { class: "journey" },
    el("span", { class: "seg5 seg5-journey", role: "img", "aria-label": `Journey: ${stages.map((x) => `${x.name} ${word[x.state]}`).join(", ")}` },
      stages.map((x) => el("i", { class: `seg-${x.state === "now" ? "doing" : x.state}` }))),
    el("p", { class: "journey-words" }, stages.map((x) => `${x.name}${x.state === "done" ? " ✓" : ""}`).join(" · ")),
    agreed || doc.status === "done" ? el("p", { class: "t-meta journey-who" },
      [agreed ? `Agreed: ${agreed}` : "", doc.status === "done" ? `Done: ${doneNote(doc)}` : ""].filter(Boolean).join(" · ")) : null);
}

// "Proof so far · AC p/t": the first pinned evidence image (shown once its sha256 is verified), then the criteria.
function proofCard(doc) {
  const ac = acItems(doc);
  const img = proofImage(doc);
  const runs = receipts(doc);
  if (!ac.length && !img && !runs.length) return null;
  const done = ac.filter((a) => a.done);
  const open = ac.filter((a) => !a.done);
  return el("section", { class: "card proof", "aria-labelledby": "proof-title" },
    el("h2", { class: "card-h", id: "proof-title" }, `Proof so far${ac.length ? ` · AC ${done.length}/${ac.length}` : ""}`),
    img ? pinnedFigure(img, "pin-proof") : null,
    done.length ? el("ul", { class: "proof-list" }, done.map((a) => el("li", {}, el("span", { class: "t-ok", "aria-hidden": "true" }, "✓ "),
      `AC${a.n} `, shown(a.text)))) : null,
    open.length ? el("p", { class: "muted proof-open" }, open.map((a, i) => [i ? " · " : "", `○ AC${a.n} `, shown(a.text)])) : null,
    // receipts of `orch task done --run`: what orch itself ran, step by step (the agent's check, not your verdict)
    runs.length ? el("ul", { class: "proof-list proof-runs", "aria-label": "Checks orch ran" }, runs.map((r) => el("li", {},
      el("span", { class: r.ok ? "t-ok" : "t-err", "aria-hidden": "true" }, r.ok ? "✓ " : "✕ "),
      el("span", { class: "sr-only" }, r.ok ? "passed: " : "failed: "), shown(r.text),
      r.steps.length > 1 ? el("span", { class: "muted proof-steps" },
        ` — ${r.steps.map((s) => `${s.name} ${s.status === "pass" ? "✓" : s.status === "fail" ? "✕" : "–"}`).join(" · ")}`) : null))) : null);
}

// "Artifacts · N": pinned images as a 3-column grid of verified thumbnails, then the other items by name and kind
// (never a URL: the document carries none), the files sent for context (links to Files) and the main PR.
function artifactsCard(doc) {
  const items = Array.isArray(doc.artifact_items) ? doc.artifact_items.filter((x) => x && typeof x === "object") : [];
  const imgs = pinnedImages(doc);
  const shownNames = new Set(imgs.map((i) => i.name));
  const files = artifactList(doc).filter((a) => !shownNames.has(a.name));     // each image once: its thumbnail
  const others = items.filter((x) => !(x.source === "file" && shownNames.has(x.name)));
  const pr = mainPr(doc);
  const count = items.length || files.length;
  if (!count && !pr) return null;
  return el("section", { class: "card arts", "aria-labelledby": "art-title" },
    el("h2", { class: "card-h", id: "art-title" }, `Artifacts · ${count}`),
    imgs.length ? el("div", { class: "art-grid" }, imgs.map((i) => pinnedFigure(i, "pin-thumb"))) : null,
    others.length ? el("ul", { class: "art-items" }, others.map((x) => el("li", {},
      shown(typeof x.label === "string" && x.label ? x.label : x.name || x.kind),
      el("span", { class: "muted" }, ` · ${x.kind}${byLabel(x) ? ` · by ${byLabel(x)}` : ""}`)))) : null,
    files.length ? el("ul", { class: "art-list" }, files.map((a) => el("li", {}, a.file
      ? el("a", { href: `/files?f=${encodeURIComponent(a.file)}` }, icon("files"), a.name)
      : el("span", {}, icon("files"), a.name)))) : null,
    pr ? el("p", { class: "art-pr" }, pr.text) : null);
}

// Ticket text with the keys of tickets this phone mirrors as links to them (keySplit); everything else as `shown`.
function linkedText(text) {
  const self = String(state.doc?.id || "").toUpperCase();
  const hrefOf = (k) => (k.toUpperCase() === self ? null : state.keyHrefs.get(k.toUpperCase()) ?? null);
  return keySplit(text, hrefOf).flatMap((p) => (p.key ? [el("a", { class: "key-link", href: p.href }, p.key)] : shown(p.text)));
}

// Which of the mirrored tickets has which phone page, from the sealed key map saved with the last-known list: one
// small decrypt, not one per mirrored ticket, and no request; best effort.
async function loadKeyLinks() {
  const keys = await loadKeyMap(state.keys?.mk);
  const map = new Map();
  for (const [k, n] of keys || []) map.set(k, `/t/${n}`);
  if (!map.size) return;
  state.keyHrefs = map;
  // Draw again only when this ticket's text names another mirrored ticket, and never under a focused control (a
  // deep-linked decision card, a draft being typed): the next render picks the links up anyway.
  const self = String(state.doc?.id || "").toUpperCase();
  const text = Object.values(state.doc?.sections || {}).filter((s) => typeof s === "string").join("\n");
  const names = keySplit(text, (k) => (k.toUpperCase() !== self && map.has(k.toUpperCase()) ? "/t/1" : null))
    .some((p) => p.key);
  const busy = document.activeElement && document.activeElement !== document.body;
  if (names && !busy) render();
}

// A section's text, its widget blocks as cards (widgets.js).
function sectionRow(doc, sec) {
  return el("section", { class: "section-row" }, el("h3", { class: "section-name" }, shown(sec.name)), sectionBody(doc, sec.name, sec.text, linkedText));
}

function chapterEl(doc, c) {
  const chip = chipFor(c.state);
  return el("details", { class: "chap", open: c.open ? "" : null, dataset: { chapter: String(c.n) } },
    el("summary", {},
      el("span", { class: `chap-mark r-${chip.role}`, "aria-hidden": "true" }, chip.glyph),
      el("span", { class: "chap-name" }, `${c.n} · ${c.name}`),
      el("span", { class: "sr-only" }, ` (${STATE_WORD[c.state] || c.state})`),
      c.meta ? el("span", { class: "chap-meta" }, c.meta) : null),
    el("div", { class: "chap-body" },
      c.line ? el("p", { class: "chap-line" }, shown(c.line)) : null,
      c.tasks.length ? el("ul", { class: "task-list" }, c.tasks.map((t) => {
        const [role, glyph] = TASK_MARK[t.state] || TASK_MARK.todo;
        return el("li", { class: `task is-${t.state}` }, el("span", { class: `task-mark t-${role}`, "aria-hidden": "true" }, glyph),
          el("span", {}, `${t.id} `, shown(t.text)), el("span", { class: "sr-only" }, ` (${t.state})`));
      })) : null,
      c.sections.map((sec) => sectionRow(doc, sec))));
}

function extras() {
  const { doc, row } = state;
  const out = [];
  // The gate text being approved is in the decision card; the chapters hold the rest (only the full doc carries
  // sections; title carries the task count and the verification summary).
  const skip = state.mode === "approval"
    ? approvalGates(doc).flatMap((g) => gateCovers(doc, g).parts.filter((p) => p.kind === "section").map((p) => p.name))
    : state.mode === "verdict" ? verdictView(doc).parts.map((p) => p.name) : [];
  const cs = chapters(row, doc, { skip, summaryShown: state.mode === "verdict" });
  if (cs.length) {
    out.push(el("section", { class: "tsection chapters", "aria-labelledby": "chapters-title" },
      el("h2", { class: "sect", id: "chapters-title" }, "Ticket"), cs.map((c) => chapterEl(doc, c))));
  }
  const more = moreSections(doc, { skip });
  if (more.length) {
    out.push(el("details", { class: "chap chap-quiet" }, el("summary", {}, el("span", { class: "chap-name" }, `More · ${more.length} section${more.length === 1 ? "" : "s"}`)),
      el("div", { class: "chap-body" }, more.map((sec) => sectionRow(doc, sec)))));
  }
  const proof = proofCard(doc);
  if (proof) out.unshift(proof);
  const arts = artifactsCard(doc);
  if (arts) out.splice(proof ? 1 : 0, 0, arts);
  const log = history(doc);
  if (log.length) {
    out.push(el("details", { class: "chap chap-quiet" },
      el("summary", {}, el("span", { class: "chap-name", id: "hist-title" }, `History · ${log.length} entries`)),
      el("div", { class: "chap-body" },
        // who is what the desktop's event log claims (agents can write it); "you" is only this phone's sent list
        el("p", { class: "hint hist-caption" }, "From the desktop's event log — not verified."),
        el("ul", { class: "hist" }, log.slice().reverse().map((h) => el("li", {},
          el("span", { class: "hist-when" }, whenText(h.at)), " ", el("b", {}, shown(h.who || "orch")), " ", shown(h.what),
          h.text ? el("span", { class: "hist-text" }, " — ", shown(h.text)) : null))))));
  }
  return out;
}

// Before the ticket has loaded: only what this phone's outbox holds for it.
function renderOutboxOnly(main) {
  const rows = [...state.queued.map(() => pill("neu", "clock", QUEUED_TEXT)), ...state.failed.map(failedRow)];
  if (!rows.length) return;
  main.replaceChildren(el("section", { class: "decision-status", role: "status", "aria-live": "polite" }, rows));
}

function render() {
  const main = document.getElementById("ticket");
  const { row, doc } = state;
  if (!row) {
    renderOutboxOnly(main);
    return;
  }
  state.mode = modeOf(row, doc);
  if (state.mode === "approval") checkGate();
  if (state.mode === "verdict") checkVerdict();
  const label = state.space?.label || UNKNOWN_SPACE;
  const key = doc?.id || row.id;
  main.replaceChildren(
    el("header", { class: "t-head" },
      el2("div", "t-ids", el("b", {}, key), el("span", { class: "muted" },
        [typeof doc?.size === "string" ? doc.size.toUpperCase() : "", typeof doc?.parent === "string" && doc.parent ? `epic ${doc.parent}` : "",
          row.id, label].filter(Boolean).join(" · "))),
      el("h1", { class: "t-title" }, doc ? shown(cardTitle(doc, key)) : state.rollback ? key
        : row.error === "binding" ? "Ticket held back" : "Couldn't decrypt this ticket"),
      el2("div", "t-pills", moveChipEl(row, doc)),
      doc ? journeyEl(doc) : null,
      doc && doc.redaction !== "full" ? stripEl(doc) : null,
      el("p", { class: "t-meta" }, [row.updated_at ? `Updated ${shortAge(row.updated_at)}` : "",
        row.needs ? "" : "nothing waits on you"].filter(Boolean).join(" · ")),
      doc && idleNote(doc) ? el("p", { class: "t-meta t-idle" }, idleNote(doc)) : null),
    state.offline ? el("p", { class: "banner banner-network offline-note", role: "status" }, icon("clock"),
      el("span", {}, offlineText(state.offline.at), el("br"), "Actions are off until you are back online.")) : "",
    state.rollback ? el("p", { class: "banner banner-decrypt", role: "alert" }, doc
      ? "The server sent an older copy of this ticket. Showing the newer one."
      : "The server sent an older copy of this ticket than this phone already saw. Open it again later.") : "",
    doc ? decisionCard() : state.rollback ? "" : el("p", { class: "banner banner-decrypt", role: "alert" }, row.error === "binding"
      ? "The server's routing for this ticket doesn't match its sealed content. Nothing can be sent from here; check it on the desktop."
      : "This ticket doesn't open with this browser's key."),
    ...[sentList(), ...(doc ? extras() : [])].filter(Boolean));
  if (state.offline) for (const c of main.querySelectorAll("#decision input, #decision textarea, #decision button")) c.disabled = true;
  paintBar();
  if (state.focusOnRender) {
    state.focusOnRender = false;
    const card = document.getElementById("decision");
    const first = card?.querySelector("input, textarea, button");
    (first || card)?.focus();
    card?.scrollIntoView({ block: "start" });
  }
}

async function refreshDecisions() {
  if (state.row?.dek) {
    try {
      state.decisions = await loadDecisions(state.row);
    } catch {
      /* keep the last list; the status line still shows what is queued */
    }
  }
  // The outbox status is known without the ticket (review I2): offline, the page still says what waits.
  const { pending, failed } = splitQueued(await queuedDecisions(state.row?.id || `TIX-${state.n}`));
  state.queued = pending;
  state.failed = failed;
  if (!pending.length) {
    if (state.draft.status === QUEUED_TEXT) state.draft.status = null;
    state.draft.ids = {};              // nothing waits: a new tap is a new decision (a refused id stays refused)
  }
}

// The network is out and no ticket is on screen: show the stored row of this ticket, opened like a fresh one but as a
// cached copy (an older one than this browser already saw is refused). Actions stay off until the network is back.
async function showOffline() {
  if (state.row && state.doc) return false;
  // This ticket's own sealed copy first; else the row in the last-known list (a ticket never opened here, #86).
  const own = await cachedTicket(state.n);
  const fromOwn = own ? await openRow(state.keys.mk, own.row, { cached: true }) : null;
  const hit = fromOwn?.doc ? { at: own.at } : await cachedRow(state.keys.mk, state.n);
  const row = fromOwn?.doc ? fromOwn : hit?.row;
  if (!row?.doc) return false;
  state.row = row;
  state.doc = row.doc;
  state.rollback = false;
  state.offline = { at: hit.at };
  state.paired = Boolean(await pairingFor(null, row.space).catch(() => null));
  state.space = (await loadCachedSpaces(state.keys.mk))?.get(row.space) || null;
  await refreshDecisions();
  return true;
}

async function load() {
  let r;
  try {
    r = await api("GET", `/api/mirrors/TIX-${state.n}`);
  } catch (e) {
    if (e?.status === 404) await forgetTicket(state.n);
    if (e?.status === 0 && await showOffline()) return;
    throw e;
  }
  state.offline = null;
  const row = await openRow(state.keys.mk, r);
  const prevHash = JSON.stringify(state.doc?.questions?.map((q) => q.hash) ?? null) + JSON.stringify(state.doc?.gates ?? null);
  // An older snapshot than the one on screen, or than the newest this browser opened (openRow's
  // high-water mark): keep the newer one on screen, or show none.
  state.rollback = Boolean(row.rollback || (state.doc && row.doc && isRollback(state.doc, row.doc)));
  if (state.rollback && state.doc) return;
  if (state.rollback) { state.row = row; state.doc = null; return; }
  state.row = row;
  state.doc = state.row.doc;
  if (state.doc) {
    await rememberTicket(state.n, r);       // only a row that just opened and is no rollback
  }
  state.paired = Boolean(await pairingFor(null, row.space).catch(() => null));
  const nextHash = JSON.stringify(state.doc?.questions?.map((q) => q.hash) ?? null) + JSON.stringify(state.doc?.gates ?? null);
  if (prevHash !== nextHash) state.draft = { ...newDraft(), values: state.draft.values, note: state.draft.note };
  if (!state.space) {
    try {
      const spaces = await loadSpaces(state.keys.mk);
      state.space = spaces.get(state.row.space) || null;
      cacheLabels(spaces, [state.row]);
    } catch {
      state.space = null;
    }
  }
  await refreshDecisions();
}

export async function start() {
  const m = /^\/t\/([1-9][0-9]{0,11})$/.exec(location.pathname);
  const main = document.getElementById("ticket");
  bar = document.getElementById("decision-bar");
  if (!m) {
    main.replaceChildren(el("section", { class: "empty" }, el("h2", {}, "No such ticket."), el("a", { class: "btn", href: "/" }, "Back to Needs you")));
    return;
  }
  state.n = Number(m[1]);
  state.focusOnRender = location.hash === "#answer";
  // Every listener is in place before the first await (review I2): an `online` that fires while the first
  // load is still pending must not be lost. Until the keys are there, a reload only marks itself as wanted.
  let loading = null, again = false, retries = 0, retryTimer = null;
  const reload = async () => {
    if (!state.keys) { again = true; return; }
    if (loading) { again = true; return loading; }
    loading = (async () => {
      do {
        again = false;
        try {
          await load();
          document.getElementById("ticket-error").hidden = true;
          render();
          if (!state.offline) retries = 0;
          // The stored copy is shown although the browser says it is online (a request that started offline and
          // failed late): look again shortly, a few times, instead of waiting for an `online` that already fired.
          else if (navigator.onLine !== false && retries < RETRY_MAX && !retryTimer) {
            retries += 1;
            retryTimer = setTimeout(() => { retryTimer = null; reload(); }, RETRY_MS * retries);
          }
        } catch (e) {
          // The service worker answers with the cached shell, so a gone session shows here: sign in again.
          // Only before anything was shown, never over a draft.
          if (e?.status === 401 && !state.row) {
            await signInAgain();
            return;
          }
          const err = document.getElementById("ticket-error");
          err.querySelector("span").textContent = e?.status === 404 ? "This ticket is no longer on the phone."
            : e?.status === 0 ? "You're offline. The ticket loads when you're back." : "Couldn't load this ticket.";
          err.hidden = false;
          await refreshDecisions().catch(() => {});
          render();
          // No answer although the browser says it is online (a request that started offline and failed
          // late): try again shortly, a few times, instead of waiting for an `online` that already fired.
          if (e?.status === 0 && navigator.onLine !== false && retries < RETRY_MAX && !retryTimer) {
            retries += 1;
            retryTimer = setTimeout(() => { retryTimer = null; reload(); }, RETRY_MS * retries);
          }
        }
      } while (again);
    })();
    try { await loading; } finally { loading = null; }
  };
  window.addEventListener("online", reload);
  window.addEventListener("fs:decisions-changed", async () => { await refreshDecisions(); render(); });
  window.addEventListener("fs:outbox-changed", async () => { await refreshDecisions(); render(); });
  window.addEventListener("hashchange", () => { if (location.hash === "#answer") { state.focusOnRender = true; render(); } });
  document.getElementById("ticket-retry")?.addEventListener("click", reload);
  state.keys = await keysOrLogin();
  if (!state.keys) return;
  await reload();
  loadKeyLinks();  // not awaited: the page is shown first, the links follow
  watchMirrors(async (rows) => { if (rows.some((x) => x.n === state.n)) await reload(); });
}

const RETRY_MS = 1000;
const RETRY_MAX = 5;

if (typeof document !== "undefined" && document.body?.classList.contains("page-ticket")) start();
