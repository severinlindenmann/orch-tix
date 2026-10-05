// fileshare/static/js/mirror-model.js — the pure logic of the phone's Needs you list and the ticket
// decision cards (TIX on orch-core, spec §4.2, §7, §10). No DOM, no fetch, no crypto: node tests it.
//
import { anyHidden } from "./textsafe.js";

// A mirror row here is {space, needs, doc, updated_at, ...}: the server's cleartext columns plus the
// decrypted orch schema doc. A decision is the v1 body of spec §4.2, sealed later by mirror-crypto.js:
//   {v: 1, decision_id, space, ticket: <local key>, kind, target, value, note, device, at, voice?}
// There is no `mac` here: a paired phone adds `pair` and `mac` afterwards (pairing.js signDecision).
import { textOnly } from "./widget-model.js";

export const NEEDS_LABEL = Object.freeze({
  question: "Agent needs input",
  approval: "Plan ready for review",
  verdict: "Ready for your verdict",
  message: "Message from agent",
});

// Short pill text per needs kind (the card's pill, always next to its icon).
export const NEEDS_PILL = Object.freeze({
  question: "Question", approval: "Plan ready", verdict: "Verdict", message: "Message",
});

export const SENT_TEXT = "Sent · waiting for the desktop";
// A paired phone is the owner: core applies its signed decision at once, with no second step on the desktop.
export const SENT_PAIRED_TEXT = "Sent · applying on your desktop";
export const QUEUED_TEXT = "Queued · sends when you're online";
export const UNKNOWN_SPACE = "A workspace";

const DECISION_ID = /^dec_[0-9a-f]{32}$/;

const newestFirst = (a, b) => String(b?.updated_at ?? "").localeCompare(String(a?.updated_at ?? ""));

// The mirrors that need the human (cleartext `needs` set), newest first.
export function needsYou(mirrors) {
  return (Array.isArray(mirrors) ? mirrors : []).filter((m) => m && m.needs).sort(newestFirst);
}

// One group per space, in the order the spaces first appear (so the newest space leads when the
// rows are newest first). labels: Map space id -> label.
export function groupBySpace(mirrors, labels = new Map()) {
  const groups = new Map();
  for (const m of Array.isArray(mirrors) ? mirrors : []) {
    if (!groups.has(m.space)) groups.set(m.space, { space: m.space, label: labels.get(m.space) || UNKNOWN_SPACE, items: [] });
    groups.get(m.space).items.push(m);
  }
  return [...groups.values()];
}

const optionKeys = (q) => (Array.isArray(q?.options) ? q.options.map((o) => (o && typeof o === "object" ? o.key : o)).map(String) : []);

// Whether Send answer may be pressed: single/confirm need one option key, multi at least one (all
// known), text a non-blank string.
// A question whose text, reason or options hold a hidden character (textsafe.js) is never answered from the phone.
export function canSendAnswer(question, value) {
  if (!question || typeof question !== "object") return false;
  if (anyHidden(question.text, question.why, question.options)) return false;
  const keys = optionKeys(question);
  switch (question.type) {
    case "single":
    case "confirm":
      return typeof value === "string" && keys.includes(value);
    case "multi":
      return Array.isArray(value) && value.length > 0 && value.every((v) => keys.includes(String(v)));
    case "text":
      return typeof value === "string" && value.trim() !== "";
    default:
      return false;
  }
}

export function findQuestion(doc, qid) {
  return (Array.isArray(doc?.questions) ? doc.questions : []).find((q) => q && q.id === qid) ?? null;
}

// The target of a decision: the hash the phone saw, so a changed question or gate is not decided blind.
export function targetFor(doc, kind, extra = {}) {
  if (kind === "answer") {
    const q = findQuestion(doc, extra.qid);
    if (!q || typeof q.hash !== "string") throw new Error(`no question ${extra.qid} on this ticket`);
    return { qid: q.id, hash: q.hash };
  }
  if (kind === "approve" || kind === "request_changes") {
    const g = doc?.gates?.[extra.gate];
    if (!g || typeof g.hash !== "string") throw new Error(`no gate ${extra.gate} on this ticket`);
    // schema 1.4: requirements and plan approved as ONE decision, each bound to its own hash
    if (kind === "approve" && extra.together) {
      const plan = doc?.gates?.plan;
      if (extra.gate !== "requirements" || !plan || typeof plan.hash !== "string") throw new Error("no plan hash to approve with");
      return { gate: "requirements", hash: g.hash, plan_hash: plan.hash };
    }
    return { gate: extra.gate, hash: g.hash };
  }
  if (kind === "verdict") {
    // orch-core: the testing round (doc.verdict.round, else the needs item {kind: "verdict", round}) keeps a signed
    // verdict off a later round; schema 1.3's verdict.hash binds it to the criteria and evidence the phone showed.
    // Core refuses a phone verdict without the hash, so the phone never sends one.
    const vh = verdictHash(doc);
    if (!vh) throw new Error("this ticket has no verdict hash (update orch-core); give the verdict on the desktop");
    const need = (Array.isArray(doc?.needs) ? doc.needs : []).find((n) => n && n.kind === "verdict");
    const round = Number.isInteger(doc?.verdict?.round) ? doc.verdict.round : need?.round;
    return Number.isInteger(round) && round >= 0 ? { status: "testing", round, hash: vh } : { status: "testing", hash: vh };
  }
  throw new Error(`no target for a ${kind} decision`);
}

const HASH = /^sha256:[0-9a-f]{64}$/;

// The document's verdict hash (schema 1.3 verdict: {hash, round}), or null.
export function verdictHash(doc) {
  const h = doc?.verdict && typeof doc.verdict === "object" ? doc.verdict.hash : null;
  return typeof h === "string" && HASH.test(h) ? h : null;
}

const RAW = (doc, name) => (typeof doc?.sections?.[name] === "string" ? doc.sections[name] : "");

// orch.core.epics verdict_hash input for one ticket: canonical_json([{ac, id, status, verification}]) (sorted keys,
// no spaces, non-ASCII as is), ac and verification being the raw sections. sha256 of it is the verdict hash.
export function verdictCanonical(doc) {
  return JSON.stringify([{ ac: RAW(doc, "Acceptance criteria"), id: String(doc?.id ?? ""), status: String(doc?.status ?? ""),
    verification: RAW(doc, "Verification") }]);
}

// What a verdict on the phone shows and binds: {ok, reason, parts: [{name, text}]}. reason: "epic" (epic verdicts
// are desktop-only), "no-hash", "not-full" (the criteria and evidence did not reach the phone), "missing:<name>",
// "hidden" (the parts are still returned so the badges show).
export function verdictView(doc) {
  if (doc?.type === "epic") return { ok: false, reason: "epic", parts: [] };
  if (!verdictHash(doc)) return { ok: false, reason: "no-hash", parts: [] };
  if (doc?.redaction !== "full") return { ok: false, reason: "not-full", parts: [] };
  for (const name of ["Acceptance criteria", "Verification"]) {
    if (!has(doc.sections, name) || typeof doc.sections[name] !== "string") return { ok: false, reason: `missing:${name}`, parts: [] };
  }
  const parts = ["Acceptance criteria", "Verification"].map((name) => ({ name, text: doc.sections[name] }));
  if (anyHidden(parts.map((p) => p.text), doc.id, doc.status)) return { ok: false, reason: "hidden", parts };
  return { ok: true, reason: "", parts };
}

// ---- the Archive (Settings): legacy tickets migrated to orch-core (Task 12's archived_at), read-only
export const ARCHIVE_DAYS = 90;   // fileshare/expiry.py LEGACY_ARCHIVE_DAYS: then the server tombstones them

export function archivedOnly(tickets) {
  return (Array.isArray(tickets) ? tickets : []).filter((t) => t && t.archived_at && !t.deleted_at)
    .sort((a, b) => String(b.archived_at).localeCompare(String(a.archived_at)));
}

export function archivedLine(iso) {
  const t = Date.parse(iso ?? "");
  if (!Number.isFinite(t)) return "Archived";
  const day = (ms) => new Date(ms).toISOString().slice(0, 10);
  return `Archived ${day(t)} · readable until ${day(t + ARCHIVE_DAYS * 86400000)}`;
}

// A voice note recorded for the agent on a decision card is kept 30 days (Task 9 review).
export const VOICE_TTL = "30d";

// The gate a plan-approval card decides: requirements first, then plan, then any other gate; only a
// pending one with the hash the phone shows (a key-only doc has no hash, so there is nothing to decide).
export function pendingGate(doc) {
  const gates = doc?.gates && typeof doc.gates === "object" ? doc.gates : {};
  const order = [...new Set(["requirements", "plan", ...Object.keys(gates)])];
  return order.find((g) => gates[g] && gates[g].state !== "approved" && typeof gates[g].hash === "string") || null;
}

// The phone's needs kind from the SEALED doc (orch needs, the addon's mapping.needs_of): never from the
// server's cleartext `needs`, which only routes pushes (final review I3).
const PHONE_NEED = { answer: "question", "approve-requirements": "approval", "approve-plan": "approval",
  "re-approve": "approval", verdict: "verdict" };

export function phoneNeed(doc) {
  for (const n of Array.isArray(doc?.needs) ? doc.needs : []) {
    const kind = PHONE_NEED[n?.kind];
    if (kind) return kind;
  }
  return null;
}

export function openQuestionCount(doc) {
  return (Array.isArray(doc?.questions) ? doc.questions : [])
    .filter((q) => q && (q.answer === undefined || q.answer === null || q.answer === "")).length;
}

// Schema 1.4: the requirements need says `together: true` when the plan is drafted too, so both are approved as one.
export function approveTogether(doc) {
  const n = (Array.isArray(doc?.needs) ? doc.needs : []).find((x) => x?.kind === "approve-requirements");
  return n?.together === true && typeof doc?.gates?.requirements?.hash === "string" && typeof doc?.gates?.plan?.hash === "string";
}

// The gate the doc's approval need names; a re-approve without its gate (title) falls back to pendingGate.
export function approvalGate(doc) {
  const n = (Array.isArray(doc?.needs) ? doc.needs : []).find((x) => PHONE_NEED[x?.kind] === "approval");
  if (n?.kind === "approve-requirements") return "requirements";
  if (n?.kind === "approve-plan") return "plan";
  if (typeof n?.gate === "string" && n.gate) return n.gate;
  return pendingGate(doc);
}

// ---- gate text: exactly what the gate's hash covers. orch-core sends gates.<g>.covers (hash v2): the section names
// in hash order (a non-empty Summary first for requirements), then the frontmatter keys (size, type). There is no
// list here: the phone shows what the doc says the hash binds, checks the hash itself (ticket.js) and approves
// nothing without covers. The addon sends sections only at full; at title and key-only the text stays on the desktop.
// The frontmatter keys a v2 hash binds after the sections (orch.core.gates _V2_META).
const V2_META = new Set(["size", "type"]);
function has(o, k) { return Boolean(o) && typeof o === "object" && Object.prototype.hasOwnProperty.call(o, k); }

// {ok, reason, parts: [{kind: "section" | "meta", name, text}]}. Covers resolve by position, as orch-core lists them:
// section names first, then the trailing v2 frontmatter keys. reason: "no-covers", "bad-covers", "missing:<name>",
// or "hidden" (a part holds a hidden character; the parts are still returned so the phone can show the badges).
export function gateCovers(doc, gate) {
  const covers = doc?.gates?.[gate]?.covers;
  if (!Array.isArray(covers) || !covers.length) return { ok: false, reason: "no-covers", parts: [] };
  if (!covers.every((n) => typeof n === "string" && n)) return { ok: false, reason: "bad-covers", parts: [] };
  let split = covers.length;
  while (split > 0 && V2_META.has(covers[split - 1])) split -= 1;
  const parts = [];
  for (const [i, name] of covers.entries()) {
    if (i < split) {
      if (!(has(doc.sections, name) && typeof doc.sections[name] === "string")) return { ok: false, reason: `missing:${name}`, parts: [] };
      parts.push({ kind: "section", name, text: doc.sections[name] });
    } else {
      if (!(has(doc, name) && (doc[name] === null || ["string", "number"].includes(typeof doc[name])))) {
        return { ok: false, reason: `missing:${name}`, parts: [] };
      }
      parts.push({ kind: "meta", name, text: doc[name] === null ? "" : String(doc[name]) });
    }
  }
  if (!parts.some((p) => p.kind === "section")) return { ok: false, reason: "bad-covers", parts: [] };
  if (anyHidden(parts.map((p) => [p.name, p.text]))) return { ok: false, reason: "hidden", parts };
  return { ok: true, reason: "", parts };
}

// orch.core.gates normalized_text: each covered section as "## name\n\n<text>" (checkboxes unticked, line ends
// trimmed, at most one blank line, trimmed), joined by a blank line; then "key: value" lines of the covered keys.
const normalize = (t) => String(t).replace(/^(\s*[-*+]\s+)\[[ xX]\]/gm, "$1[ ]").split("\n").map((l) => l.trimEnd()).join("\n")
  .replace(/\n{3,}/g, "\n\n").trim();

export function normalizedGateText(parts) {
  const secs = parts.filter((p) => p.kind === "section").map((p) => `## ${p.name}\n\n${normalize(p.text)}`).join("\n\n");
  const meta = parts.filter((p) => p.kind === "meta").map((p) => `${p.name}: ${p.text}`).join("\n");
  return meta ? `${secs}\n\n${meta}` : secs;
}

// The phone approves only what it can show: the full doc, with everything the gate's hash covers.
export function canApproveOnPhone(doc, gate) {
  return doc?.redaction === "full" && gateCovers(doc, gate).ok;
}

const SECTION_TEXT = (doc, name) => {
  const t = doc?.sections?.[name];
  return typeof t === "string" && t.trim() ? t : null;
};

// Every other non-empty section, read-only (the covered ones are in the decision card; Log is never shown).
export function otherSections(doc, gate) {
  const covered = gateCovers(doc, gate).parts.filter((p) => p.kind === "section").map((p) => p.name);
  const skip = new Set([...covered, "Log"]);
  const sections = doc?.sections && typeof doc.sections === "object" ? doc.sections : {};
  return Object.keys(sections).filter((name) => !skip.has(name) && SECTION_TEXT(doc, name))
    .map((name) => ({ name, text: sections[name] }));
}

// ---- spec §5.5: a ticket request from the phone (decision kind ticket_request, no ticket). The exact body the
// CLI and the orch-tix addon read (tests/vectors/mirror1.json "decision-space"): value {title, body}.
const SPACE_ID = /^[0-9a-f]{32}$/;
export const REQUEST_TITLE_MAX = 200;
export const REQUEST_BODY_MAX = 4000;

export function buildTicketRequest({ space, title, body = "", at, decisionId, voice = null }) {
  if (!SPACE_ID.test(String(space))) throw new Error("pick a workspace");
  if (!DECISION_ID.test(String(decisionId))) throw new Error("decision id must be dec_ and 32 hex characters");
  const t = String(title ?? "").replace(/\s+/g, " ").trim();
  if (!t) throw new Error("a ticket request needs a title");
  const out = { v: 1, decision_id: decisionId, kind: "ticket_request", space, at,
    // cut by code points, as core counts them (a split surrogate pair would be a broken character)
    value: { title: Array.from(t).slice(0, REQUEST_TITLE_MAX).join(""),
      body: Array.from(String(body ?? "").trim()).slice(0, REQUEST_BODY_MAX).join("") } };
  if (voice && typeof voice === "object" && voice.file) {
    out.voice = { file: String(voice.file), transcript: typeof voice.transcript === "string" ? voice.transcript : "" };
  }
  return out;
}

// ---- spec §8: a message to the human is data, never instructions: its text (shown as plain text), its FILE
// ids (links to Files) and its ticket. The sealed body decides; a body that does not open has text null.
const FILE_ID_RE = /^FILE[1-9][0-9]{0,11}$/;
const TIX_RE = /^TIX-[1-9][0-9]{0,11}$/;

export function messageView(sealed, row = {}) {
  const b = sealed && typeof sealed === "object" ? sealed : null;
  return {
    from: String(b?.from || row.from_name || "a device"),
    text: b && typeof b.text === "string" ? b.text : null,
    files: (Array.isArray(b?.files) ? b.files : []).filter((f) => typeof f === "string" && FILE_ID_RE.test(f)),
    ticket: typeof b?.ticket === "string" && TIX_RE.test(b.ticket) ? b.ticket : null,
    kind: typeof b?.kind === "string" ? b.kind : "text",
  };
}

// The verdict values orch-core's verdict takes (orch.core.ops: "done" or "follow-up").
export const VERDICT_VALUE = Object.freeze({ done: "done", send_back: "follow-up" });

// A decision's value as orch-core reads it: always a string (a multi answer is its keys joined by
// commas, as `orch answer` takes them), so a signed decision verifies and applies without a rewrite.
export function decisionValue(kind, question, value) {
  if (kind === "answer" && question?.type === "multi" && Array.isArray(value)) return value.map(String).join(",");
  return value;
}

export function newDecisionId(rng = (n) => globalThis.crypto.getRandomValues(new Uint8Array(n))) {
  return "dec_" + Array.from(rng(16), (b) => b.toString(16).padStart(2, "0")).join("");
}

export function buildDecision({ space, doc, kind, target, value, note = "", device, at, voice = null, decisionId }) {
  if (!DECISION_ID.test(String(decisionId))) throw new Error("decision id must be dec_ and 32 hex characters");
  const out = {
    v: 1, decision_id: decisionId, space, ticket: doc?.id ?? null, kind, target, value,
    note: typeof note === "string" ? note.trim() : "", device: String(device ?? ""), at,
  };
  if (voice && typeof voice === "object" && voice.file) {
    out.voice = { file: String(voice.file), transcript: typeof voice.transcript === "string" ? voice.transcript : "" };
  }
  return out;
}

// The outbox's decision items for one ticket, split: pending ones wait to be sent (and hold the bar);
// failed ones were refused (a 4xx) and never block it, the page offers Discard instead.
export function splitQueued(items) {
  const all = Array.isArray(items) ? items : [];
  return { pending: all.filter((x) => x && x.state !== "failed"), failed: all.filter((x) => x && x.state === "failed") };
}

const NOT_SENT_REASON = { bad_ref: "no longer on the phone", not_found: "no longer on the phone", gone: "no longer on the phone",
  no_space: "no longer on the phone", forbidden: "not allowed", too_large: "too large" };

// "Not sent: <reason>" for a decision the server refused.
export function notSentText(item) {
  return `Not sent: ${NOT_SENT_REASON[item?.error] || "refused by the server"}`;
}

// What the phone shows for a sent decision. ack null = not acked yet. A stale ack whose target hash
// no longer matches the doc (the phone checks) reads "Changed · review again".
// Why the desktop held a decision (its non-final "waiting-*" ack, a fixed code: the server never sees text).
export const WAITING_REASON = Object.freeze({
  "waiting-unpaired": "this phone isn't paired with that desktop",
  "waiting-signature": "the desktop couldn't verify this phone",
  "waiting-switched-off": "decisions of this kind from the phone are switched off",
  "waiting-time": "this phone's clock looks wrong",
  "waiting-check": "it needs a look on the desktop",
});
// No ack this long after sending: say so instead of "applying" forever (an older desktop never sends a waiting ack).
export const NOT_YET_MS = 120000;
export const UNPAIRED_TEXT = "Sent · waiting for you to confirm in Mission Control";
const NOT_YET_TEXT = "Not applied yet · open the desktop";

export function outcomeText(ack, { changed = false, paired = false, ageMs = 0, request = false } = {}) {
  // Not an error: an unpaired phone's answer is safe on the desktop and waits there for Apply (QA #56).
  if (ack === "waiting-unpaired") return UNPAIRED_TEXT;
  if (typeof ack === "string" && WAITING_REASON[ack]) return `Not applied · ${WAITING_REASON[ack]} · open the desktop`;
  switch (ack) {
    case null:
    case undefined:
      if (ageMs > NOT_YET_MS) return NOT_YET_TEXT;
      return paired ? SENT_PAIRED_TEXT : SENT_TEXT;
    case "applied":
      return request ? "Created in your backlog" : "Applied on desktop";
    case "ignored":
      return "Ignored on desktop";
    case "stale":
      return changed ? "Not applied · the text changed since, review again" : "Not applied · the ticket moved on, see the desktop";
    case "answered-locally":
    case "superseded":
      return "Already answered on the desktop";
    case "unlinked":
      return "No longer on the phone";
    default:
      return "Handled on desktop";
  }
}

// The role (colour) and icon of an outcome, for the status pill.
export function outcomeRole(ack, { ageMs = 0 } = {}) {
  if (ack === "applied") return "ok";
  if (ack === null || ack === undefined) return ageMs > NOT_YET_MS ? "warn" : "info";
  if (ack === "waiting-unpaired") return "info";
  if (ack === "stale" || (typeof ack === "string" && WAITING_REASON[ack])) return "warn";
  return "neu";
}

// How long ago an ISO time was, in ms (0 when unknown).
export function ageOf(iso, now = Date.now()) {
  const t = Date.parse(iso ?? "");
  return Number.isFinite(t) ? Math.max(0, now - t) : 0;
}

// Whether a stale decision's target no longer matches the doc the phone has now.
export function targetChanged(doc, decision) {
  const t = decision?.target;
  if (!t || typeof t !== "object") return false;
  if (t.qid) return findQuestion(doc, t.qid)?.hash !== t.hash;
  if (t.gate) return doc?.gates?.[t.gate]?.hash !== t.hash;
  return false;
}

// "sha256:ab12…c21e" for a gate hash.
export function shortHash(h) {
  const m = /^sha256:([0-9a-f]{64})$/.exec(String(h ?? ""));
  return m ? `sha256:${m[1].slice(0, 4)}…${m[1].slice(-4)}` : "";
}

// The doc the orch-tix addon seals follows its redaction table (addons/orch-tix/orch_tix/mapping.py):
// title sends tasks.progress {done, total} and verification_summary; full is the orch doc itself
// (tasks.summary, sections.Verification, sections.Log only with sync_log); key-only sends none of these.
const count = (x) => (Number.isInteger(x) && x >= 0 ? x : null);

// Task progress "4 of 7", or "".
export function taskProgress(doc) {
  const t = doc?.tasks && typeof doc.tasks === "object" && !Array.isArray(doc.tasks) ? doc.tasks : null;
  const p = t?.progress || t?.summary;
  const done = count(p?.done), total = count(p?.total);
  return done !== null && total ? `${Math.min(done, total)} of ${total}` : "";
}

const firstLine = (text) => String(text ?? "").split("\n").map((l) => l.trim()).find(Boolean) || "";

// The verification summary: one line.
export function verificationSummary(doc) {
  if (typeof doc?.verification_summary === "string" && doc.verification_summary.trim()) return firstLine(doc.verification_summary);
  return firstLine(textOnly(doc?.sections?.Verification));
}

// The ticket's history from orch events, as the orch-tix addon sends it (doc.history: {seq, at, who, what, text?};
// text only at full). Never the Log text. Plain strings only; shown as text.
// Who is as the desktop's event log says, which an agent can write: a human event is never shown as "you" (an
// older addon sent "you"); only the decisions this phone sent itself (its sent list) are yours.
export const HUMAN_IN_LOG = "human (desktop log)";

export function history(doc) {
  const str = (x) => (typeof x === "string" ? x : "");
  return (Array.isArray(doc?.history) ? doc.history : []).filter((h) => h && typeof h === "object")
    .map((h) => ({ at: str(h.at), who: h.who === "you" ? HUMAN_IN_LOG : str(h.who), what: str(h.what), text: str(h.text) }));
}

const FILE_ID = /^FILE[1-9][0-9]{0,11}$/;

// Artifacts by name; those the addon sent for context carry their FILE id (a link to Files).
export function artifactList(doc) {
  const out = [];
  const seen = new Map();
  const add = (name, file) => {
    const n = String(name ?? "").trim();
    if (!n) return;
    const f = typeof file === "string" && FILE_ID.test(file) ? file : null;
    if (seen.has(n)) { if (f && !seen.get(n).file) seen.get(n).file = f; return; }
    const item = { name: n, file: f };
    seen.set(n, item);
    out.push(item);
  };
  for (const a of Array.isArray(doc?.artifacts) ? doc.artifacts : []) add(a && typeof a === "object" ? a.name : a, null);
  for (const a of Array.isArray(doc?.context_artifacts) ? doc.context_artifacts : []) add(a?.name, a?.file);
  return out;
}

// "Desktop last seen 2 h ago · answers apply when it's back", or "" while the desktop is live.
export function lastSeenText(iso, now = Date.now()) {
  const t = Date.parse(iso ?? "");
  if (!Number.isFinite(t)) return "Desktop not seen yet · answers apply when it's back";
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 120) return "";
  const ago = s < 3600 ? `${Math.round(s / 60)} min` : s < 172800 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} d`;
  return `Desktop last seen ${ago} ago · answers apply when it's back`;
}

// The question count line of a card.
export function questionCount(n) {
  const k = Number.isInteger(n) && n > 0 ? n : 0;
  return k ? `${k} question${k === 1 ? "" : "s"}` : "";
}

// A key-only doc carries no title: the card says where to answer.
export function cardTitle(doc, key = doc?.id) {
  if (typeof doc?.title === "string" && doc.title.trim()) return doc.title;
  return `Question on ${key || "a ticket"} · open on desktop`;
}

// Whether `doc` is older than a snapshot this page already showed (`seen` = {gen, mirror_rev} of that
// one): a lower link generation, or the same generation with a lower mirror_rev. The desktop seals
// both into the doc (batch 3 review m4), so a server that hands back an old copy is noticed. A doc
// without the fields (an older CLI) is never called a rollback.
// The newest {gen, mirror_rev} seen for a ticket after opening `doc` (null when neither has the fields).
export function highWater(prev, doc) {
  const mark = (x) => (x && Number.isInteger(x.gen) && Number.isInteger(x.mirror_rev) ? { gen: x.gen, mirror_rev: x.mirror_rev } : null);
  const a = mark(prev), b = mark(doc);
  if (!b) return a;
  if (!a) return b;
  return b.gen > a.gen || (b.gen === a.gen && b.mirror_rev > a.mirror_rev) ? b : a;
}

// The key of a ticket's "seen" mark: the space and the doc's own key, the two things boundToRow proves the
// row's uuid is made from. Never the server's TIX number, which a reset server hands out again to another ticket.
export const seenKey = (space, doc) => `${space}|${doc.id}`;

export function isRollback(seen, doc) {
  const g0 = seen?.gen, r0 = seen?.mirror_rev, g1 = doc?.gen, r1 = doc?.mirror_rev;
  if (![g0, r0, g1, r1].every(Number.isInteger)) return false;
  return g1 < g0 || (g1 === g0 && r1 < r0);
}
