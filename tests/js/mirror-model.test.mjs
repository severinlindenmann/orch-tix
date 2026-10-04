// Task 9: the pure logic of the phone's Needs you list and decision cards (mirror-model.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  NEEDS_LABEL, VERDICT_VALUE, VOICE_TTL, archivedLine, archivedOnly, artifactList, highWater, pendingGate, buildDecision, decisionValue, history, notSentText, splitQueued, taskProgress,
  verificationSummary, canSendAnswer, groupBySpace, isRollback, needsYou, newDecisionId, outcomeText, targetFor,
} from "../../fileshare/static/js/mirror-model.js";

const Q = { id: "Q1", text: "Which timestamp format?", type: "single", hash: "sha256:" + "a".repeat(64),
  options: [{ key: "A", label: "ISO 8601", cost: null }, { key: "B", label: "Local time", cost: null }] };
const DOC = { schema_version: "1.0.0", id: "DEMO-0038", title: "Export", status: "waiting", questions: [Q],
  gates: { plan: { state: "pending", hash: "sha256:" + "b".repeat(64) } }, needs: [{ kind: "answer" }] };

test("answers need a valid option or text", () => {
  assert.equal(canSendAnswer(Q, "A"), true);
  assert.equal(canSendAnswer(Q, "Z"), false);
  assert.equal(canSendAnswer({ ...Q, type: "multi" }, []), false);
  assert.equal(canSendAnswer({ ...Q, type: "text" }, "  "), false);
});

test("multi needs at least one known key; text needs non-blank text; confirm needs one option", () => {
  const multi = { ...Q, type: "multi" };
  assert.equal(canSendAnswer(multi, ["A"]), true);
  assert.equal(canSendAnswer(multi, ["A", "Z"]), false);
  assert.equal(canSendAnswer(multi, "A"), false);
  assert.equal(canSendAnswer({ ...Q, type: "text" }, " ok "), true);
  assert.equal(canSendAnswer({ ...Q, type: "confirm" }, "B"), true);
  assert.equal(canSendAnswer({ ...Q, type: "confirm" }, ["B"]), false);
  assert.equal(canSendAnswer(null, "A"), false);
});

test("targets carry the hash the phone saw", () => {
  assert.deepEqual(targetFor(DOC, "answer", { qid: "Q1" }), { qid: "Q1", hash: Q.hash });
  assert.deepEqual(targetFor(DOC, "approve", { gate: "plan" }), { gate: "plan", hash: DOC.gates.plan.hash });
  assert.deepEqual(targetFor(DOC, "request_changes", { gate: "plan" }), { gate: "plan", hash: DOC.gates.plan.hash });
  assert.throws(() => targetFor(DOC, "verdict", {}), /verdict hash/);      // schema 1.3: no hash, no verdict
});

test("a verdict target carries the document's verdict hash and its testing round (schema 1.3)", () => {
  const H = "sha256:" + "c".repeat(64);
  const doc = { ...DOC, status: "testing", needs: [{ kind: "task", detail: "T3" }, { kind: "verdict", round: 41 }],
    verdict: { hash: H, round: 41 } };
  assert.deepEqual(targetFor(doc, "verdict"), { status: "testing", round: 41, hash: H });
  assert.deepEqual(targetFor({ ...doc, verdict: { hash: H, round: null } }, "verdict"), { status: "testing", round: 41, hash: H });
  assert.deepEqual(targetFor({ ...doc, needs: [], verdict: { hash: H, round: "41" } }, "verdict"), { status: "testing", hash: H });
  assert.throws(() => targetFor({ ...doc, verdict: null }, "verdict"), /verdict hash/);
  assert.throws(() => targetFor({ ...doc, verdict: { hash: "md5:x", round: 41 } }, "verdict"), /verdict hash/);
});

test("decision values are strings orch-core takes: multi keys joined by commas, send back is follow-up", () => {
  const multi = { ...Q, type: "multi" };
  assert.equal(decisionValue("answer", multi, ["A", "B"]), "A,B");
  assert.equal(decisionValue("answer", Q, "A"), "A");
  assert.equal(decisionValue("answer", { ...Q, type: "text" }, "free text"), "free text");
  assert.equal(VERDICT_VALUE.done, "done");
  assert.equal(VERDICT_VALUE.send_back, "follow-up");
});

test("a target for an unknown question or gate is refused", () => {
  assert.throws(() => targetFor(DOC, "answer", { qid: "Q9" }));
  assert.throws(() => targetFor(DOC, "approve", { gate: "requirements" }));
  assert.throws(() => targetFor(DOC, "comment", {}));
});

test("a decision has the v1 shape and keeps its id on retry", () => {
  const d = buildDecision({ space: "s".repeat(32), doc: DOC, kind: "answer", target: targetFor(DOC, "answer", { qid: "Q1" }),
    value: "A", note: "", device: "iPhone · Safari", at: "2026-10-02T09:41:07Z", decisionId: "dec_" + "c".repeat(32) });
  assert.deepEqual(Object.keys(d).sort(), ["at", "decision_id", "device", "kind", "note", "space", "target", "ticket", "v", "value"]);
  assert.equal(d.ticket, "DEMO-0038");
  assert.equal(d.v, 1);
  assert.ok(!("mac" in d));
  assert.equal(d.decision_id, "dec_" + "c".repeat(32));
});

test("a voice note goes in only when there is one, and a bad id is refused", () => {
  const base = { space: "s".repeat(32), doc: DOC, kind: "verdict", target: { status: "testing" }, value: "done",
    note: "looks good", device: "d", at: "2026-10-02T09:41:07Z", decisionId: "dec_" + "c".repeat(32) };
  const d = buildDecision({ ...base, voice: { file: "FILE94", transcript: "ship it" } });
  assert.deepEqual(d.voice, { file: "FILE94", transcript: "ship it" });
  assert.throws(() => buildDecision({ ...base, decisionId: "dec_xyz" }));
});

test("new decision ids are dec_ + 32 hex and differ", () => {
  const a = newDecisionId(), b = newDecisionId();
  assert.match(a, /^dec_[0-9a-f]{32}$/);
  assert.notEqual(a, b);
});

test("needs you lists only mirrors that need the human, newest first", () => {
  const rows = [{ space: "a", needs: null, doc: DOC, updated_at: "2026-10-02T09:00:00Z" },
    { space: "a", needs: "question", doc: DOC, updated_at: "2026-10-02T09:10:00Z" },
    { space: "b", needs: "verdict", doc: DOC, updated_at: "2026-10-02T09:20:00Z" }];
  assert.deepEqual(needsYou(rows).map((r) => r.needs), ["verdict", "question"]);
});

test("groupBySpace keeps the newest space first and labels unknown spaces plainly", () => {
  const rows = [{ space: "b", needs: "verdict", updated_at: "2026-10-02T09:20:00Z" },
    { space: "a", needs: "question", updated_at: "2026-10-02T09:10:00Z" },
    { space: "b", needs: "approval", updated_at: "2026-10-02T09:05:00Z" }];
  const g = groupBySpace(rows, new Map([["b", "Acme Energy"]]));
  assert.deepEqual(g.map((x) => [x.space, x.label, x.items.length]), [["b", "Acme Energy", 2], ["a", "A workspace", 1]]);
});

test("the needs labels are the spec's words", () => {
  assert.deepEqual(NEEDS_LABEL, { question: "Agent needs input", approval: "Plan ready for review",
    verdict: "Ready for your verdict", message: "Message from agent" });
});

test("outcomes read plainly", () => {
  assert.equal(outcomeText("applied"), "Applied on desktop");
  assert.equal(outcomeText("ignored"), "Ignored on desktop");
  // feedback round A: a refused decision says why (the phone knows when the text changed since)
  assert.equal(outcomeText("stale"), "Not applied · the ticket moved on, see the desktop");
  assert.equal(outcomeText("stale", { changed: true }), "Not applied · the text changed since, review again");
  assert.equal(outcomeText("answered-locally"), "Already answered on the desktop");
  assert.equal(outcomeText("superseded"), "Already answered on the desktop");
  assert.equal(outcomeText("unlinked"), "No longer on the phone");
  assert.equal(outcomeText(null), "Sent · waiting for the desktop");
  assert.equal(outcomeText(null, { paired: true }), "Sent · applying on your desktop");     // paired: no second step
});

test("an older snapshot than the one shown is a rollback; a doc without the fields never is", () => {
  const seen = { gen: 2, mirror_rev: 7 };
  assert.equal(isRollback(seen, { gen: 2, mirror_rev: 6 }), true);
  assert.equal(isRollback(seen, { gen: 1, mirror_rev: 99 }), true);
  assert.equal(isRollback(seen, { gen: 2, mirror_rev: 7 }), false);
  assert.equal(isRollback(seen, { gen: 3, mirror_rev: 1 }), false);
  assert.equal(isRollback(seen, { id: "DEMO-0038" }), false);
  assert.equal(isRollback(null, { gen: 1, mirror_rev: 1 }), false);
});

test("a decision the outbox marked failed is not 'queued': it never blocks the bar", () => {
  const items = [{ seq: 1, state: "pending" }, { seq: 2, state: "failed", error: "forbidden" }, { seq: 3 }];
  const { pending, failed } = splitQueued(items);
  assert.deepEqual(pending.map((x) => x.seq), [1, 3]);
  assert.deepEqual(failed.map((x) => x.seq), [2]);
  assert.deepEqual(splitQueued(null), { pending: [], failed: [] });
});

test("a failed decision says why in plain words", () => {
  assert.equal(notSentText({ error: "not_found" }), "Not sent: no longer on the phone");
  assert.equal(notSentText({ error: "gone" }), "Not sent: no longer on the phone");
  assert.equal(notSentText({ error: "bad_ref" }), "Not sent: no longer on the phone");
  assert.equal(notSentText({ error: "forbidden" }), "Not sent: not allowed");
  assert.equal(notSentText({ error: "too_large" }), "Not sent: too large");
  assert.equal(notSentText({ error: "http_400" }), "Not sent: refused by the server");
  assert.equal(notSentText({}), "Not sent: refused by the server");
});

// The docs the orch-tix addon seals, one per redaction level (tests/vectors/make_addon_docs.py runs the
// addon's mapping.redact on `orch schema example`).
const ADDON = JSON.parse(readFileSync(new URL("../vectors/addon-docs.json", import.meta.url), "utf8")).levels;

test("task progress comes from tasks.progress (title) or tasks.summary (full), never at key-only", () => {
  assert.equal(taskProgress(ADDON.title), "1 of 3");
  assert.equal(taskProgress(ADDON.full), "1 of 3");
  assert.equal(taskProgress(ADDON["key-only"]), "");
  assert.equal(taskProgress({ tasks: { progress: { done: 0, total: 0 } } }), "");
  assert.equal(taskProgress({ tasks: [{ state: "done" }] }), "", "an array is not the addon's shape");
});

test("the verification summary is verification_summary (title) or the Verification section's first line (full)", () => {
  assert.equal(verificationSummary(ADDON.title), "All 14 jobs export; Excel opens every file.");
  assert.equal(verificationSummary(ADDON.full), "All 14 jobs export; Excel opens every file.");
  assert.equal(verificationSummary(ADDON["key-only"]), "");
});

test("the history is the addon's events: who and what at title, with the text at full, none at key-only", () => {
  assert.equal(history(ADDON.full).length, 6);
  assert.deepEqual(history(ADDON.full)[2], { at: "2026-10-02T09:10Z", who: "human (desktop log)", what: "asked for changes on the plan",
    text: "split the exporter" });
  assert.equal(history(ADDON.title)[2].text, "");
  assert.deepEqual(history(ADDON["key-only"]), []);
  assert.ok(!history(ADDON["full+log"]).some((h) => h.what.includes("step 24")), "never the Log text");
  assert.deepEqual(history({ history: [{ seq: 1, who: 7, what: null }, "x"] }), [{ at: "", who: "", what: "", text: "" }]);
});

test("artifacts are named, and the ones the addon sent for context link to their FILE", () => {
  assert.deepEqual(artifactList(ADDON.title), [{ name: "report.md", file: "FILE7" }]);
  assert.deepEqual(artifactList(ADDON.full), [{ name: "report.md", file: "FILE7" }]);
  assert.deepEqual(artifactList(ADDON["key-only"]), []);
  assert.deepEqual(artifactList({ artifacts: ["a.txt"], context_artifacts: [{ name: "b.png", file: "FILE9" }] }),
    [{ name: "a.txt", file: null }, { name: "b.png", file: "FILE9" }]);
  assert.deepEqual(artifactList({ context_artifacts: [{ name: "x", file: "javascript:alert(1)" }] }), [{ name: "x", file: null }]);
});

test("the high-water mark of a ticket only moves forward", () => {
  assert.deepEqual(highWater(null, { gen: 1, mirror_rev: 3 }), { gen: 1, mirror_rev: 3 });
  assert.deepEqual(highWater({ gen: 1, mirror_rev: 3 }, { gen: 1, mirror_rev: 5 }), { gen: 1, mirror_rev: 5 });
  assert.deepEqual(highWater({ gen: 1, mirror_rev: 5 }, { gen: 1, mirror_rev: 4 }), { gen: 1, mirror_rev: 5 });
  assert.deepEqual(highWater({ gen: 1, mirror_rev: 9 }, { gen: 2, mirror_rev: 1 }), { gen: 2, mirror_rev: 1 });
  assert.deepEqual(highWater({ gen: 1, mirror_rev: 2 }, { title: "no fields" }), { gen: 1, mirror_rev: 2 });
  assert.equal(highWater(undefined, {}), null);
});

test("the gate to approve is requirements before plan, and only one with a hash", () => {
  const h = (c) => "sha256:" + c.repeat(64);
  assert.equal(pendingGate({ gates: { plan: { state: "pending", hash: h("b") }, requirements: { state: "pending", hash: h("a") } } }), "requirements");
  assert.equal(pendingGate({ gates: { plan: { state: "pending", hash: h("b") }, requirements: { state: "approved", hash: h("a") } } }), "plan");
  assert.equal(pendingGate({ gates: { plan: { state: "pending" }, verify: { verdict: null } } }), null);
  assert.equal(pendingGate({}), null);
});

test("a voice note for the agent is kept 30 days", () => {
  assert.equal(VOICE_TTL, "30d");
});

test("the Archive lists only archived legacy tickets, newest archive first, with the 90-day line", () => {
  const rows = [{ id: "TIX-1", archived_at: null }, { id: "TIX-2", archived_at: "2026-09-01T10:00:00Z" },
    { id: "TIX-3", archived_at: "2026-09-20T10:00:00Z" }, { id: "TIX-4", archived_at: "2026-09-25T10:00:00Z", deleted_at: "x" }];
  assert.deepEqual(archivedOnly(rows).map((r) => r.id), ["TIX-3", "TIX-2"]);
  assert.equal(archivedLine("2026-09-01T10:00:00Z"), "Archived 2026-09-01 · readable until 2026-11-30");
  assert.equal(archivedLine("nonsense"), "Archived");
});

// ---- final review I3: the needs kind comes from the sealed doc, never the cleartext row
import { approvalGate, phoneNeed, openQuestionCount } from "../../fileshare/static/js/mirror-model.js";

test("the phone's needs kind is the first orch need the human answers on the phone", () => {
  assert.equal(phoneNeed({ needs: [{ kind: "task" }, { kind: "answer", qids: ["Q1"] }] }), "question");
  assert.equal(phoneNeed({ needs: [{ kind: "approve-requirements" }] }), "approval");
  assert.equal(phoneNeed({ needs: [{ kind: "approve-plan" }] }), "approval");
  assert.equal(phoneNeed({ needs: [{ kind: "re-approve", gate: "plan" }] }), "approval");
  assert.equal(phoneNeed({ needs: [{ kind: "verdict", round: 3 }] }), "verdict");
  assert.equal(phoneNeed({ needs: [{ kind: "task" }, { kind: "broken" }] }), null);
  assert.equal(phoneNeed({}), null);
  assert.equal(openQuestionCount({ questions: [{ id: "Q1" }, { id: "Q2", answer: "A" }, { id: "Q3", answer: "" }] }), 2);
});

test("the gate an approval need names: requirements, plan, or a re-approve's gate", () => {
  assert.equal(approvalGate({ needs: [{ kind: "approve-requirements" }], gates: {} }), "requirements");
  assert.equal(approvalGate({ needs: [{ kind: "approve-plan" }], gates: {} }), "plan");
  assert.equal(approvalGate({ needs: [{ kind: "re-approve", gate: "requirements" }], gates: {} }), "requirements");
  const h = "sha256:" + "a".repeat(64);
  assert.equal(approvalGate({ needs: [{ kind: "re-approve" }], gates: { plan: { state: "pending", hash: h } } }), "plan");
});

// ---- the gate text the phone shows is exactly what the gate's hash covers (orch-core gates.<g>.covers, hash v2)
import { createHash } from "node:crypto";
import { canApproveOnPhone, gateCovers, normalizedGateText, otherSections } from "../../fileshare/static/js/mirror-model.js";

const sha = (t) => "sha256:" + createHash("sha256").update(t, "utf8").digest("hex");

test("the gate's parts come from covers: sections, then the frontmatter keys (v2: size and type)", () => {
  const req = gateCovers(ADDON.full, "requirements");
  assert.equal(req.ok, true);
  assert.deepEqual(req.parts.map((p) => [p.kind, p.name]), [["section", "Requirements"], ["section", "Acceptance criteria"],
    ["section", "Out of scope"], ["meta", "size"], ["meta", "type"]]);
  assert.deepEqual(req.parts.slice(3).map((p) => p.text), ["m", "feature"]);
  assert.deepEqual(gateCovers(ADDON.full, "plan").parts, [{ kind: "section", name: "Plan", text: "1. Inventory\n2. Exporter" }]);
});

test("the normalized text hashes to the gate hash orch-core computed (both gates of the real doc)", () => {
  for (const gate of ["requirements", "plan"]) {
    assert.equal(sha(normalizedGateText(gateCovers(ADDON.full, gate).parts)), ADDON.full.gates[gate].hash, gate);
  }
  // a Summary is bound first when it is covered; ticking a criterion does not change the hash
  const doc = { ...ADDON.full, sections: { ...ADDON.full.sections, "Acceptance criteria": "- [x] Opens in Excel" } };
  assert.equal(sha(normalizedGateText(gateCovers(doc, "requirements").parts)), ADDON.full.gates.requirements.hash);
  const edited = { ...ADDON.full, sections: { ...ADDON.full.sections, Requirements: "- Two files per day" } };
  assert.notEqual(sha(normalizedGateText(gateCovers(edited, "requirements").parts)), ADDON.full.gates.requirements.hash);
});

test("no covers, or a covered part the doc does not carry: nothing to approve on the phone", () => {
  const { covers, ...bare } = ADDON.full.gates.plan;
  const noCovers = { ...ADDON.full, gates: { ...ADDON.full.gates, plan: bare } };
  assert.deepEqual(gateCovers(noCovers, "plan"), { ok: false, reason: "no-covers", parts: [] });
  assert.equal(canApproveOnPhone(noCovers, "plan"), false);
  const summary = { ...ADDON.full, gates: { ...ADDON.full.gates, requirements: { ...ADDON.full.gates.requirements,
    covers: ["Summary", "Requirements", "size", "type"] } }, sections: { ...ADDON.full.sections } };
  delete summary.sections.Summary;
  assert.equal(gateCovers(summary, "requirements").reason, "missing:Summary");
  assert.equal(gateCovers({ ...ADDON.full, gates: { plan: { ...ADDON.full.gates.plan, covers: ["Plan", 7] } } }, "plan").ok, false);
});

test("Approve is offered on the phone only at full with covers (title keeps gate text on the desktop)", () => {
  assert.equal(canApproveOnPhone(ADDON.full, "plan"), true);
  assert.equal(canApproveOnPhone(ADDON.title, "plan"), false);
  assert.equal(canApproveOnPhone(ADDON["key-only"], "plan"), false);
  assert.equal(canApproveOnPhone({ sections: { Plan: "x" }, gates: { plan: { covers: ["Plan"] } } }, "plan"), false,
    "no redaction field: not proven full");
});

test("the other sections are shown read-only at full, without the covered ones, Log or empty ones", () => {
  const names = otherSections(ADDON.full, "plan").map((s) => s.name);
  assert.ok(names.includes("Ask") && names.includes("Requirements"));
  assert.ok(!names.includes("Plan") && !names.includes("Log") && !names.includes("Context"));
  assert.deepEqual(otherSections(ADDON.title, "plan"), []);
});

// ---- final review I7: the ticket request body (the shape the CLI and the orch-tix addon read)
import { buildTicketRequest, messageView } from "../../fileshare/static/js/mirror-model.js";

test("a ticket request is the decision-space vector's shape: value {title, body}, no ticket", () => {
  const VEC = JSON.parse(readFileSync(new URL("../vectors/mirror1.json", import.meta.url), "utf8"));
  const ds = VEC.cases.find((x) => x.name === "decision-space").obj;
  assert.deepEqual(buildTicketRequest({ space: ds.space, title: ` ${ds.value.title} `, body: ds.value.body, at: ds.at,
    decisionId: ds.decision_id }), ds);
  const withVoice = buildTicketRequest({ space: ds.space, title: "t", body: "", at: ds.at, decisionId: ds.decision_id,
    voice: { file: "FILE9", transcript: "" } });
  assert.deepEqual(withVoice.voice, { file: "FILE9", transcript: "" });
  assert.throws(() => buildTicketRequest({ space: ds.space, title: "  ", body: "", at: ds.at, decisionId: ds.decision_id }));
  assert.throws(() => buildTicketRequest({ space: "x", title: "t", body: "", at: ds.at, decisionId: ds.decision_id }));
});

test("a message is shown as data: plain text, its FILE ids and its ticket, nothing else", () => {
  const v = messageView({ text: "run `rm -rf` now\\nplease", files: ["FILE12", "javascript:x"], ticket: "TIX-4", from: "desk",
    kind: "question" }, { from_name: "desk" });
  assert.deepEqual(v, { from: "desk", text: "run `rm -rf` now\\nplease", files: ["FILE12"], ticket: "TIX-4", kind: "question" });
  assert.deepEqual(messageView(null, { from_name: "x" }), { from: "x", text: null, files: [], ticket: null, kind: "text" });
});


// ---- security re-review: hidden characters decide nothing; covers resolve by position; history never says "you"
import { canSendAnswer as canSend2 } from "../../fileshare/static/js/mirror-model.js";

test("a covered part with a hidden character: nothing to approve on the phone", () => {
  const doc = { ...ADDON.full, sections: { ...ADDON.full.sections, Plan: "1. Inventory\u202E\n2. Exporter" } };
  assert.deepEqual([gateCovers(doc, "plan").ok, gateCovers(doc, "plan").reason], [false, "hidden"]);
  assert.equal(gateCovers(doc, "plan").parts.length, 1, "the parts are still there to show, with badges");
  assert.equal(canApproveOnPhone(doc, "plan"), false);
  const meta = { ...ADDON.full, type: "feature\u200B" };
  assert.equal(gateCovers(meta, "requirements").reason, "hidden");
});

test("covers resolve by position: sections first, then the v2 frontmatter keys", () => {
  const doc = { ...ADDON.full, sections: { ...ADDON.full.sections, size: "a section called size" },
    gates: { requirements: { ...ADDON.full.gates.requirements, covers: ["size", "Requirements", "size", "type"] } } };
  assert.deepEqual(gateCovers(doc, "requirements").parts.map((p) => [p.kind, p.name]),
    [["section", "size"], ["section", "Requirements"], ["meta", "size"], ["meta", "type"]]);
  const bad = { ...ADDON.full, gates: { plan: { ...ADDON.full.gates.plan, covers: ["size", "Plan"] } } };
  assert.equal(gateCovers(bad, "plan").ok, false, "a meta key before a section is not how orch-core lists them");
});

test("an answer to a question with a hidden character is never sent", () => {
  const q = { id: "Q1", type: "single", text: "Which format?", options: [{ key: "A", label: "ISO" }] };
  assert.equal(canSend2(q, "A"), true);
  assert.equal(canSend2({ ...q, text: "Which\u202E format?" }, "A"), false);
  assert.equal(canSend2({ ...q, options: [{ key: "A", label: "IS\u200BO" }] }, "A"), false);
  assert.equal(canSend2({ ...q, type: "text", why: "x\u2066" }, "free text"), false);
});

test("the history never says you: a human event is from the desktop's log, unverified", () => {
  assert.equal(history({ history: [{ who: "you", what: "approved the plan" }] })[0].who, "human (desktop log)");
  assert.equal(history({ history: [{ who: "human (desktop log)", what: "x" }] })[0].who, "human (desktop log)");
  assert.equal(history({ history: [{ who: "claude-code", what: "x" }] })[0].who, "claude-code");
});


// ---- schema 1.3: the verdict hash covers id, status, Acceptance criteria and Verification, as the phone shows them
import { verdictCanonical, verdictView } from "../../fileshare/static/js/mirror-model.js";

const VERDICT_DOC = { ...ADDON.full, status: "testing", sections: { ...ADDON.full.sections, Verification: "- AC1: opened 3 files in Excel" },
  verdict: { hash: "sha256:061c01e67c0692b2686d641ffd7d580c2c6310928dd8818b5a7ff5f68eb62a3b", round: 3 } };

test("the verdict hash is orch-core's test vector over what the phone shows", () => {
  assert.equal(verdictCanonical(VERDICT_DOC),
    '[{"ac":"- [ ] Opens in Excel","id":"DEMO-0038","status":"testing","verification":"- AC1: opened 3 files in Excel"}]');
  assert.equal(sha(verdictCanonical(VERDICT_DOC)), VERDICT_DOC.verdict.hash);
  const v = verdictView(VERDICT_DOC);
  assert.equal(v.ok, true);
  assert.deepEqual(v.parts.map((p) => p.name), ["Acceptance criteria", "Verification"]);
});

test("a verdict decides nothing on the phone for an epic, without a hash, below full, or with hidden characters", () => {
  assert.equal(verdictView({ ...VERDICT_DOC, type: "epic" }).reason, "epic");
  assert.equal(verdictView({ ...VERDICT_DOC, verdict: null }).reason, "no-hash");
  assert.equal(verdictView({ ...ADDON.title, status: "testing", verdict: VERDICT_DOC.verdict }).reason, "not-full");
  const hidden = { ...VERDICT_DOC, sections: { ...VERDICT_DOC.sections, Verification: "- AC1: opened\u202E 3 files" } };
  assert.equal(verdictView(hidden).reason, "hidden");
  assert.equal(verdictView(hidden).parts.length, 2);
});


// ---- feedback round A (orch-core schema 1.4): requirements and plan approved as one decision
import { approveTogether } from "../../fileshare/static/js/mirror-model.js";

test("together: one approve with the requirements hash and the plan hash", () => {
  const H1 = "sha256:" + "1".repeat(64), H2 = "sha256:" + "2".repeat(64);
  const doc = { gates: { requirements: { state: "pending", hash: H1, covers: ["Requirements"] },
    plan: { state: "pending", hash: H2, covers: ["Plan"] } },
  needs: [{ kind: "approve-requirements", together: true }] };
  assert.equal(approveTogether(doc), true);
  assert.deepEqual(targetFor(doc, "approve", { gate: "requirements", together: true }),
    { gate: "requirements", hash: H1, plan_hash: H2 });
  assert.deepEqual(targetFor(doc, "approve", { gate: "requirements" }), { gate: "requirements", hash: H1 });
  assert.equal(approveTogether({ ...doc, needs: [{ kind: "approve-requirements" }] }), false);
  assert.equal(approveTogether({ ...doc, gates: { requirements: doc.gates.requirements } }), false, "no plan hash: not together");
  assert.throws(() => targetFor({ ...doc, gates: { requirements: doc.gates.requirements } }, "approve",
    { gate: "requirements", together: true }), /plan/);
});


// ---- feedback fix round F1: a held decision says why; no ack for two minutes says so too
import { outcomeRole as role2, outcomeText as text2, NOT_YET_MS } from "../../fileshare/static/js/mirror-model.js";

test("a waiting ack reads 'Not applied · <reason> · open the desktop'", () => {
  assert.equal(text2("waiting-unpaired"), "Not applied · this phone isn't paired with that desktop · open the desktop");
  assert.equal(text2("waiting-switched-off"), "Not applied · decisions of this kind from the phone are switched off · open the desktop");
  assert.equal(text2("waiting-signature"), "Not applied · the desktop couldn't verify this phone · open the desktop");
  assert.equal(text2("waiting-time"), "Not applied · this phone's clock looks wrong · open the desktop");
  assert.equal(text2("waiting-check"), "Not applied · it needs a look on the desktop · open the desktop");
  assert.equal(role2("waiting-check"), "warn");
});

test("no ack after two minutes: 'Not applied yet · open the desktop', paired or not", () => {
  assert.equal(NOT_YET_MS, 120000);
  assert.equal(text2(null, { paired: true, ageMs: 119000 }), "Sent · applying on your desktop");
  assert.equal(text2(null, { paired: true, ageMs: 121000 }), "Not applied yet · open the desktop");
  assert.equal(text2(null, { ageMs: 121000 }), "Not applied yet · open the desktop");
  assert.equal(role2(null, { ageMs: 121000 }), "warn");
});

test("a ticket request's outcome", () => {
  assert.equal(text2("applied", { request: true }), "Created in your backlog");
  assert.equal(text2("waiting-unpaired", { request: true }), "Not applied · this phone isn't paired with that desktop · open the desktop");
});


test("a ticket request title is cut by code points, never inside a character", () => {
  const { buildTicketRequest: btr } = { buildTicketRequest };
  const t = "😀".repeat(250);
  const r = btr({ space: "a".repeat(32), title: t, at: "2026-10-04T00:00:00Z", decisionId: "dec_" + "1".repeat(32) });
  assert.equal(Array.from(r.value.title).length, 200);
  assert.ok(r.value.title.endsWith("😀") && !/[\uD800-\uDBFF]$/.test(r.value.title));
});
