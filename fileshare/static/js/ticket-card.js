// fileshare/static/js/ticket-card.js — the shared ticket-card encoding of the design system (spec §3 "Ticket card at
// three sizes"), for the phone: the move chip (whose move it is; pink only when it is yours), the progress strip
// (R · P · T · AC · PR, the same glyphs everywhere), the chapters of the ticket view, and the one-tap answer of a
// Needs you card. Pure: no DOM, no fetch, no crypto, so node tests it. Everything here reads the SEALED doc (and
// the row's needs, which mirrors-data.js already took from that doc), never the server's cleartext routing.
import { approvalGate, verificationSummary } from "./mirror-model.js";
import { anyHidden } from "./textsafe.js";

const isObj = (x) => Boolean(x) && typeof x === "object" && !Array.isArray(x);
const text = (x) => (typeof x === "string" && x.trim() ? x : null);
const count = (x) => (Number.isInteger(x) && x >= 0 ? x : null);
const openQ = (q) => q && (q.answer === undefined || q.answer === null || q.answer === "");

export const GLYPH = Object.freeze({ done: "✓", doing: "◐", you: "●", todo: "○", warn: "▲" });
const ROLE = { done: "ok", doing: "info", you: "you", todo: "neu", warn: "warn" };

const GATE_NAME = { requirements: "requirements", plan: "plan" };

// ---- the move chip

// {role, icon, text}: whose move it is. You: a question, an approval or a verdict (pink). Otherwise the status;
// in progress names the agent and the task it is on ("claude-code is working · T2 of 3").
const MOVE_ROLE = { you: ["you", "dot"], agent: ["info", "half"], nobody: ["neu", "ring"] };

export function moveChip(row, doc) {
  // schema 1.4: the document's own move (the dashboard's rules), when it carries a usable one
  const m = doc?.move;
  if (m && MOVE_ROLE[m.who] && typeof m.label === "string" && m.label.trim() && !anyHidden(m.label)) {
    const [role, icon] = MOVE_ROLE[m.who];
    return { role, icon, text: m.label.trim().slice(0, 80) };
  }
  const needs = row?.needs;
  if (needs === "approval") return { role: "you", icon: "dot", text: `Your move: approve the ${GATE_NAME[approvalGate(doc)] || approvalGate(doc) || "plan"}` };
  if (needs === "question") {
    const n = count(row?.open_questions) || 0;
    return { role: "you", icon: "dot", text: n > 1 ? `Your move: answer ${n} questions` : "Your move: answer" };
  }
  if (needs === "verdict") return { role: "you", icon: "dot", text: "Your move: your verdict" };
  const status = doc?.status || row?.status;
  switch (status) {
    case "in-progress": {
      const agent = text(doc?.claim?.harness) || "An agent";
      const p = tasksDone(doc);
      const on = text(doc?.tasks?.doing);
      const where = on && p ? ` · ${on} of ${p.total}` : p ? ` · ${p.done} of ${p.total} done` : "";
      return { role: "info", icon: "half", text: `${agent} is working${where}` };
    }
    case "testing": return { role: "info", icon: "half", text: "Testing" };
    case "waiting": return { role: "warn", icon: "clock", text: "Waiting" };
    case "done": return { role: "ok", icon: "check", text: "Done" };
    case "open": return { role: "neu", icon: "ring", text: "Ready" };
    case "backlog": return { role: "neu", icon: "ring", text: "Backlog" };
    default: return { role: "neu", icon: "ring", text: status ? String(status) : "Unknown" };
  }
}

// ---- the progress strip

// {done, total} of the tasks (full: tasks.summary, title: tasks.progress), or null.
export function tasksDone(doc) {
  const t = isObj(doc?.tasks) ? doc.tasks : null;
  const p = t?.progress || t?.summary;
  const done = count(p?.done), total = count(p?.total);
  return done !== null && total ? { done: Math.min(done, total), total } : null;
}

// {done, total} of the acceptance criteria ("- [x]" lines of sections["Acceptance criteria"], full only), or null.
export function acceptance(doc) {
  const s = text(doc?.sections?.["Acceptance criteria"]);
  if (!s) return null;
  let done = 0, total = 0;
  for (const line of s.split("\n")) {
    const m = /^\s*[-*]\s+\[([ xX/])\]/.exec(line);
    if (!m) continue;
    total += 1;
    if (m[1] === "x" || m[1] === "X") done += 1;
  }
  return total ? { done, total } : null;
}

const PR_NUMBER = /\/(?:pull|merge_requests|pullrequest)\/(\d+)/;

// The main PR (the last one linked): {text, role}, or null. Never a link: the URL stays on the desktop.
export function mainPr(doc) {
  const prs = Array.isArray(doc?.prs) ? doc.prs.filter(isObj) : [];
  const pr = prs[prs.length - 1];
  if (!pr) return null;
  const n = PR_NUMBER.exec(String(pr.url ?? ""))?.[1];
  const state = typeof pr.state === "string" && pr.state !== "draft" ? pr.state : "";
  const role = state === "merged" ? "ok" : "neu";
  return { text: `PR${n ? ` #${n}` : ""}${state ? ` ${state}` : ""}`, role };
}

function gateStep(doc, gate, letter, label) {
  const g = isObj(doc?.gates?.[gate]) ? doc.gates[gate] : null;
  if (!g) return null;
  if (g.state === "approved") return { key: letter, role: "ok", text: `${letter} ${GLYPH.done}`, label: `${label} approved` };
  const yours = approvalGate(doc) === gate && (doc?.needs || []).some((n) => /^(approve-|re-approve)/.test(String(n?.kind)));
  return yours ? { key: letter, role: "you", text: `${letter} ${GLYPH.you}`, label: `${label} waits for your approval` }
    : { key: letter, role: "neu", text: `${letter} ${GLYPH.todo}`, label: `${label} not approved yet` };
}

// [{key, role, text, label}]: R and P (the gates), T done/total, AC done/total, the main PR. Only what the doc
// carries: a key-only doc shows the gate states and nothing else.
export function progressStrip(doc) {
  const out = [gateStep(doc, "requirements", "R", "Requirements"), gateStep(doc, "plan", "P", "Plan")];
  const t = tasksDone(doc);
  if (t) out.push({ key: "T", role: t.done === t.total ? "ok" : t.done ? "info" : "neu", text: `T ${t.done}/${t.total}`,
    label: `${t.done} of ${t.total} tasks done` });
  const ac = acceptance(doc);
  if (ac) out.push({ key: "AC", role: ac.done === ac.total ? "ok" : "neu", text: `AC ${ac.done}/${ac.total}`,
    label: `${ac.done} of ${ac.total} acceptance criteria proven` });
  const pr = mainPr(doc);
  if (pr) out.push({ key: "PR", role: pr.role, text: pr.text, label: pr.text });
  return out.filter(Boolean);
}

// ---- the chapters of the ticket view (Asked · Agreed · Doing · Proven · Left)

export const CHAPTER_SECTIONS = Object.freeze({
  1: ["Ask", "Context"],
  2: ["Summary", "Requirements", "Acceptance criteria", "Out of scope", "Plan"],
  3: ["Current state"],
  4: ["Verification"],
});
const IN_CHAPTERS = new Set(Object.values(CHAPTER_SECTIONS).flat());

// The chapter the ticket is in now: 2 while a gate waits, 3 in progress or waiting, 4 testing, 0 done.
export function currentChapter(row, doc) {
  const status = doc?.status || row?.status;
  if (status === "done") return 0;
  if (row?.needs === "approval") return 2;
  if (status === "testing" || row?.needs === "verdict") return 4;
  if (status === "in-progress" || status === "waiting") return 3;
  const g = doc?.gates || {};
  if (g.requirements?.state === "approved" && g.plan?.state === "approved") return 3;
  return status === "open" ? 2 : 1;
}

// "02.10" for an ISO time, or "".
export function dayMonth(iso) {
  const m = /^\d{4}-(\d{2})-(\d{2})/.exec(String(iso ?? ""));
  return m ? `${m[2]}.${m[1]}` : "";
}

// The tasks of a full doc: [{id, state, text}] (state done, doing, todo, blocked, skipped).
export function taskItems(doc) {
  const list = Array.isArray(doc?.tasks?.tasks) ? doc.tasks.tasks : [];
  return list.filter((t) => isObj(t) && text(t.id))
    .map((t) => ({ id: String(t.id), state: String(t.state || "todo"), text: String(t.text ?? "") }));
}

const sectionsOf = (doc, names, skip) => names.filter((n) => !skip.has(n) && text(doc?.sections?.[n]))
  .map((name) => ({ name, text: doc.sections[name] }));

// What is left: "2 tasks, 2 criteria to prove, then your verdict." or "".
export function leftText(doc) {
  const t = tasksDone(doc), ac = acceptance(doc);
  const parts = [];
  if (t && t.total > t.done) parts.push(`${t.total - t.done} task${t.total - t.done === 1 ? "" : "s"}`);
  if (ac && ac.total > ac.done) parts.push(`${ac.total - ac.done} criteri${ac.total - ac.done === 1 ? "on" : "a"} to prove`);
  return parts.length ? `${parts.join(", ")}, then your verdict.` : "";
}

// [{n, name, state, meta, line, sections: [{name, text}], tasks}] — chapters with nothing to show are left out;
// `skip`: section names the decision card already shows (the gate text being approved). `open` marks the
// current chapter. `summaryShown`: the decision card already shows the one-line verification summary (verdict).
export function chapters(row, doc, { skip = [], summaryShown = false } = {}) {
  const skipped = new Set(skip);
  const cur = currentChapter(row, doc);
  const stateOf = (n) => (cur === 0 || n < cur ? "done" : n === cur ? (row?.needs && n !== 3 ? "you" : "doing") : "todo");
  const g = doc?.gates || {};
  const t = tasksDone(doc), ac = acceptance(doc);
  const tasks = taskItems(doc);
  const out = [];
  const add = (n, name, extra) => {
    const c = { n, name, state: stateOf(n), meta: "", line: "", sections: [], tasks: [], open: n === cur, ...extra };
    if (c.sections.length || c.tasks.length || c.line) out.push(c);
  };
  add(1, "Asked", { sections: sectionsOf(doc, CHAPTER_SECTIONS[1], skipped) });
  const agreed = [g.requirements?.state === "approved" ? `Requirements ${GLYPH.done} approved ${dayMonth(g.requirements.approved)}`.trim() : "",
    g.plan?.state === "approved" ? `Plan ${GLYPH.done} approved ${dayMonth(g.plan.approved)}`.trim() : ""].filter(Boolean).join(" · ");
  add(2, "Agreed", { sections: sectionsOf(doc, CHAPTER_SECTIONS[2], skipped), line: agreed,
    meta: g.plan?.state === "approved" ? dayMonth(g.plan.approved) : "" });
  const doingSections = sectionsOf(doc, CHAPTER_SECTIONS[3], skipped);
  if (!tasks.length) doingSections.push(...sectionsOf(doc, ["Tasks"], skipped));
  add(3, "Doing", { tasks, sections: doingSections, meta: t ? `T ${t.done}/${t.total}` : "",
    line: t ? `Tasks: ${t.done} of ${t.total}` : "" });
  const summary = verificationSummary(doc);
  const proven = sectionsOf(doc, CHAPTER_SECTIONS[4], skipped);
  add(4, "Proven", { sections: proven, meta: ac ? `AC ${ac.done}/${ac.total}` : "",
    line: !proven.length && summary && !summaryShown ? summary : "" });
  if (cur !== 0) {
    const left = leftText(doc);
    if (left) out.push({ n: 5, name: "Left", state: "todo", meta: "", line: left, sections: [], tasks: [], open: false });
  }
  return out;
}

// Every non-empty section that no chapter holds and the decision card does not show (the Log is never shown).
export function moreSections(doc, { skip = [] } = {}) {
  const skipped = new Set([...skip, "Log", "Tasks"]);
  const sections = isObj(doc?.sections) ? doc.sections : {};
  return Object.keys(sections).filter((n) => !skipped.has(n) && !IN_CHAPTERS.has(n) && text(sections[n]))
    .map((name) => ({ name, text: sections[name] }));
}

// An option's cost, or null when it says nothing ("none", "-", "n/a", "0", blank).
export function costText(cost) {
  const t = typeof cost === "string" ? cost.trim() : "";
  return t && !/^(none|null|n\/?a|-+|–|0)$/i.test(t) ? t : null;
}

// ---- the Needs you card

// The one question a card answers in place: exactly one open question, single or confirm, with its hash.
// {question, options: [{n, key, label, rec, cost}]} or null (then the card links to the ticket).
export function quickAnswer(doc) {
  const open = (Array.isArray(doc?.questions) ? doc.questions : []).filter(openQ);
  if (open.length !== 1) return null;
  const q = open[0];
  if (!["single", "confirm"].includes(q.type) || typeof q.hash !== "string") return null;
  if (anyHidden(q.text, q.why, q.options)) return null;      // the ticket page shows the badges and refuses
  const raw = Array.isArray(q.options) && q.options.length ? q.options
    : q.type === "confirm" ? [{ key: "yes", label: "Yes" }, { key: "no", label: "No" }] : [];
  if (!raw.length || raw.length > 9) return null;
  const rec = (key) => (Array.isArray(q.recommended) ? q.recommended.map(String).includes(key) : String(q.recommended ?? "") === key);
  const options = raw.map((o, i) => {
    const key = String(isObj(o) ? o.key ?? i : o);
    return { n: i + 1, key, label: String(isObj(o) ? o.label ?? key : o), rec: rec(key), cost: isObj(o) ? costText(o.cost) : null };
  });
  return { question: q, options };
}

// The number of steps of a plan ("1. …" lines, else non-empty lines), or 0.
export function planSteps(doc) {
  const plan = text(doc?.sections?.Plan);
  if (!plan) return 0;
  const lines = plan.split("\n").map((l) => l.trim()).filter(Boolean);
  const numbered = lines.filter((l) => /^\d+[.)]\s/.test(l)).length;
  return numbered || lines.length;
}

// The card's one line under the title for a gate or a verdict.
export function cardLine(row, doc) {
  if (row?.needs === "approval") {
    const gate = approvalGate(doc) || "plan";
    const steps = gate === "plan" ? planSteps(doc) : 0;
    const agent = text(doc?.claim?.harness);
    const parts = [steps ? `${steps} step${steps === 1 ? "" : "s"}` : doc?.redaction === "full" ? "" : "Read it on the desktop",
      agent ? `${agent} waits` : ""].filter(Boolean);
    return parts.join(" · ");
  }
  if (row?.needs === "verdict") {
    const ac = acceptance(doc);
    return [verificationSummary(doc), ac ? `AC ${ac.done}/${ac.total} proven` : ""].filter(Boolean).join(" · ");
  }
  return "";
}

// The card's action label for a gate or a verdict (it opens the ticket's read-and-decide view).
export function cardAction(row, doc) {
  if (row?.needs === "approval") return "Review and approve";
  if (row?.needs === "verdict") return "Review and decide";
  return "";
}

export function chipFor(state) {
  return { role: ROLE[state] || "neu", glyph: GLYPH[state] || GLYPH.todo };
}

// ---- phone v4 (orch-core M-report "TIX"): journey, blocking headline, task bar, proof, pinned images

const approvedGate = (doc, g) => doc?.gates?.[g]?.state === "approved";

// Five equal stages: Asked always done; Agreed done when requirements (and plan) are approved; Doing while in
// progress or waiting; Proven while testing; Done when done. [{name, state: done|now|todo}] (core has no journey key).
export function journey(doc) {
  const s = doc?.status;
  // Agreed: the requirements are approved and so is the plan, or the work has started without one (core skips the plan
  // gate for small sizes, `plan_skip_sizes`; schema 1.5 has no skip flag, the plan gate simply stays pending)
  const working = s === "in-progress" || s === "waiting" || s === "testing" || s === "done";
  const awaiting = (Array.isArray(doc?.needs) ? doc.needs : []).some((n) => /^(approve-|re-approve)/.test(String(n?.kind)));
  const agreed = approvedGate(doc, "requirements") && (approvedGate(doc, "plan") || (working && !awaiting));
  const stage = s === "done" ? 5 : s === "testing" ? 3 : agreed ? 2 : 1;
  return ["Asked", "Agreed", "Doing", "Proven", "Done"].map((name, i) => ({
    name, state: s === "done" || i < stage ? "done" : i === stage ? "now" : "todo" }));
}

// Who signed, from the document's `signed` block (orch-core schema 1.6): `{signed, by}` per approved gate and for a done
// verdict. A plain string for the phone to draw with textContent. Empty when there is nothing to say (no entry).
export function signerText(entry) {
  if (!isObj(entry) || typeof entry.signed !== "boolean") return "";
  const by = typeof entry.by === "string" ? entry.by : "";
  if (!entry.signed) return by === "by delegation" ? "by delegation, not signed here" : "not signed here";
  return by === "you" ? "by you" : by || "signed";
}

const hasSigned = (doc) => isObj(doc?.signed);

// Who agreed. With a `signed` block the wording is per gate, only for a gate that is approved (an invalidated gate
// shows no signer). Without one (orch-core before 1.6) the mirror cannot say, so core's own "not signed here" wording.
export function agreedNote(doc) {
  const approved = ["requirements", "plan"].filter((g) => approvedGate(doc, g));
  // the plan skipped by size: only the requirements were agreed
  if (!approved.length) return "waits for your approval";
  const name = (g) => (g === "requirements" ? "req" : "plan");
  if (!hasSigned(doc)) return `${approved.map(name).join(" + ")}, approval not signed here`;
  const who = approved.map((g) => signerText(doc.signed[g]) || "not signed here");
  if (who.every((w) => w === who[0])) return `${approved.map(name).join(" + ")} ${who[0]}`.trim();
  return approved.map((g, i) => `${name(g)} ${who[i]}`.trim()).join(", ");
}

export function doneNote(doc) {
  if (doc?.status !== "done") return "your verdict";
  if (!hasSigned(doc)) return "closed, not signed here";
  return signerText(doc.signed.verdict) || "closed, not signed here";
}

// A decision that blocks an agent: an approval, a verdict, or an open blocking question (the headline's count).
export function isBlocking(row) {
  if (row?.needs === "approval" || row?.needs === "verdict") return true;
  if (row?.needs !== "question") return false;
  return (Array.isArray(row.doc?.questions) ? row.doc.questions : []).some((q) => openQ(q) && q.blocking !== false);
}

export function blockingCount(rows) {
  return (Array.isArray(rows) ? rows : []).filter(isBlocking).length;
}

// Five segments for an agent's card: done tasks scaled to five, then the one in progress.
export function taskBar(doc, segments = 5) {
  const t = tasksDone(doc);
  const out = Array(segments).fill("todo");
  if (!t) return out;
  const done = Math.min(segments, Math.floor((t.done / t.total) * segments));
  for (let i = 0; i < done; i += 1) out[i] = "done";
  if (done < segments && text(doc?.tasks?.doing)) out[done] = "doing";
  return out;
}

// "T4/5" (the task in progress of the total), or "3/5" done, or "".
export function taskChip(doc) {
  const t = tasksDone(doc);
  if (!t) return "";
  const on = text(doc?.tasks?.doing);
  return on && /^T[1-9][0-9]*$/.test(on) ? `${on}/${t.total}` : `${t.done}/${t.total}`;
}

// The acceptance criteria as proof lines: [{n, text, done}] ("- [x] …" boxes, full only).
export function acItems(doc) {
  const s = text(doc?.sections?.["Acceptance criteria"]);
  if (!s) return [];
  const out = [];
  for (const line of s.split("\n")) {
    const m = /^\s*[-*]\s+\[([ xX/])\]\s*(.*)$/.exec(line);
    if (m) out.push({ n: out.length + 1, text: m[2].trim(), done: m[1] === "x" || m[1] === "X" });
  }
  return out;
}

const IMAGE_KINDS = new Set(["screenshot", "diagram"]);
const IMAGE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(png|jpe?g|gif|webp)$/i;
const SHA_HEX = /^[0-9a-f]{64}$/;
const FILE_REF = /^FILE[1-9][0-9]{0,11}$/;

// The images a ticket may show: artifact items of an image kind with a well-formed sha256 (pinned by core) whose
// file reached the phone as a FILE (the addon's context artifacts, by name). [{name, sha256, file, label, ac, task}].
// The phone fetches the FILE and shows it only when its bytes hash to this sha256 (images.js).
export function pinnedImages(doc) {
  const files = new Map((Array.isArray(doc?.context_artifacts) ? doc.context_artifacts : [])
    .filter((a) => isObj(a) && typeof a.name === "string" && FILE_REF.test(String(a.file))).map((a) => [a.name, a.file]));
  return (Array.isArray(doc?.artifact_items) ? doc.artifact_items : []).filter((it) => isObj(it) && it.source === "file"
    && IMAGE_KINDS.has(it.kind) && typeof it.name === "string" && IMAGE_NAME.test(it.name) && SHA_HEX.test(String(it.sha256))
    && files.has(it.name))
    .map((it) => ({ name: it.name, sha256: it.sha256, file: files.get(it.name), label: typeof it.label === "string" ? it.label : it.name,
      ac: Number.isInteger(it.ac) ? it.ac : null, task: typeof it.task === "string" ? it.task : null }));
}

const ARTIFACT_REF = /!\[[^\]]*\]\(artifact:([^)\s]+)\)/g;

// The first pinned image the gated text of `gates` references as ![…](artifact:<name>); never a URL from the text.
export function gateImage(doc, gates) {
  const pinned = new Map(pinnedImages(doc).map((i) => [i.name, i]));
  for (const g of gates || []) {
    for (const name of Array.isArray(doc?.gates?.[g]?.covers) ? doc.gates[g].covers : []) {
      const t = doc?.sections?.[name];
      if (typeof t !== "string") continue;
      for (const m of t.matchAll(ARTIFACT_REF)) if (pinned.has(m[1])) return pinned.get(m[1]);
    }
  }
  return null;
}

// The first pinned image that proves an acceptance criterion.
export function proofImage(doc) {
  return pinnedImages(doc).find((i) => i.ac !== null) || null;
}
