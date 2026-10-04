// The shared ticket-card encoding on the phone (ticket-card.js): the move chip, the progress strip, the chapters
// of the ticket view and the one-tap answer of a Needs you card. Uses the addon's real docs (addon-docs.json).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  acceptance, cardAction, costText, cardLine, chapters, currentChapter, dayMonth, mainPr, moreSections, moveChip, planSteps,
  progressStrip, quickAnswer, taskItems, tasksDone,
} from "../../fileshare/static/js/ticket-card.js";

const LEVELS = JSON.parse(readFileSync(new URL("../vectors/addon-docs.json", import.meta.url), "utf8")).levels;
const FULL = LEVELS.full, TITLE = LEVELS.title, KEY = LEVELS["key-only"];
const H = (c) => "sha256:" + c.repeat(64);
// the local rules, as for a document from before schema 1.4 (no `move`)
const LOCAL = (d) => ({ ...d, move: undefined });

test("the move chip is pink only when it is your move", () => {
  assert.deepEqual(moveChip({ needs: "question", open_questions: 1 }, LOCAL(TITLE)), { role: "you", icon: "dot", text: "Your move: answer" });
  assert.equal(moveChip({ needs: "question", open_questions: 3 }, LOCAL(TITLE)).text, "Your move: answer 3 questions");
  const plan = { ...FULL, gates: { ...FULL.gates, plan: { state: "pending", hash: H("c") } }, needs: [{ kind: "approve-plan" }] };
  assert.equal(moveChip({ needs: "approval" }, LOCAL(plan)).text, "Your move: approve the plan");
  assert.equal(moveChip({ needs: "verdict" }, LOCAL(TITLE)).role, "you");
  for (const status of ["backlog", "open", "in-progress", "waiting", "testing", "done", "weird"]) {
    assert.notEqual(moveChip({ needs: null }, LOCAL({ ...TITLE, status })).role, "you", status);
  }
});

test("in progress names the agent and the task it is on", () => {
  const doc = { ...FULL, status: "in-progress" };
  assert.equal(moveChip({ needs: null }, LOCAL(doc)).text, "claude-code is working · T2 of 3");
  assert.equal(moveChip({ needs: null }, LOCAL({ ...TITLE, status: "in-progress" })).text, "claude-code is working · 1 of 3 done");
  assert.equal(moveChip({ needs: null }, { status: "in-progress" }).text, "An agent is working");
});

test("the strip: gates, tasks, criteria and the main PR, from what the doc carries", () => {
  const strip = (d) => progressStrip(d).map((s) => s.text);
  assert.deepEqual(strip(FULL), ["R ✓", "P ✓", "T 1/3", "AC 0/1"]);
  assert.deepEqual(strip(TITLE), ["R ✓", "P ✓", "T 1/3"]);
  assert.deepEqual(strip(KEY), ["R ✓", "P ✓"]);
  const waiting = { ...TITLE, gates: { requirements: { state: "approved" }, plan: { state: "pending", hash: H("d") } },
    needs: [{ kind: "approve-plan" }] };
  const p = progressStrip(waiting).find((s) => s.key === "P");
  assert.deepEqual([p.text, p.role], ["P ●", "you"]);
  assert.ok(progressStrip(FULL).every((s) => s.label));            // every chip has words, not only a glyph
  const pr = { ...FULL, prs: [{ repo: "acme", url: "https://github.com/a/b/pull/31", state: "draft" },
    { repo: "acme", url: "https://github.com/a/b/pull/32", state: "merged" }] };
  assert.deepEqual(mainPr(pr), { text: "PR #32 merged", role: "ok" });
  assert.deepEqual(mainPr({ prs: [{ url: "https://github.com/a/b/pull/7", state: "draft" }] }), { text: "PR #7", role: "neu" });
  assert.equal(mainPr({ prs: [] }), null);
});

test("tasks and acceptance counts", () => {
  assert.deepEqual(tasksDone(FULL), { done: 1, total: 3 });
  assert.deepEqual(tasksDone(TITLE), { done: 1, total: 3 });
  assert.equal(tasksDone(KEY), null);
  assert.deepEqual(acceptance({ sections: { "Acceptance criteria": "- [x] a\n- [ ] b\n* [X] c\nnot a box" } }), { done: 2, total: 3 });
  assert.equal(acceptance(TITLE), null);
  assert.deepEqual(taskItems(FULL).map((t) => [t.id, t.state]), [["T1", "done"], ["T2", "doing"], ["T3", "todo"]]);
});

test("the current chapter follows the gate, the status and the verdict", () => {
  assert.equal(currentChapter({ needs: "approval" }, FULL), 2);
  assert.equal(currentChapter({ needs: null }, { ...FULL, status: "in-progress" }), 3);
  assert.equal(currentChapter({ needs: "verdict" }, { ...FULL, status: "testing" }), 4);
  assert.equal(currentChapter({ needs: null }, { ...FULL, status: "done" }), 0);
  assert.equal(currentChapter({ needs: null }, { status: "backlog", gates: {} }), 1);
});

test("chapters at full: Asked, Agreed, Doing, Proven, Left, each with its sections; empty ones are left out", () => {
  const doc = { ...FULL, status: "in-progress" };
  const cs = chapters({ needs: null }, doc);
  assert.deepEqual(cs.map((c) => [c.n, c.name, c.state]), [[1, "Asked", "done"], [2, "Agreed", "done"], [3, "Doing", "doing"],
    [4, "Proven", "todo"], [5, "Left", "todo"]]);
  assert.deepEqual(cs[0].sections.map((s) => s.name), ["Ask"]);
  assert.deepEqual(cs[1].sections.map((s) => s.name), ["Requirements", "Acceptance criteria", "Plan"]);
  assert.equal(cs[1].meta, "02.10");
  assert.equal(cs[2].meta, "T 1/3");
  assert.equal(cs[2].tasks.length, 3);
  assert.ok(cs[2].open && !cs[0].open);
  assert.deepEqual(cs[3].sections.map((s) => s.name), ["Verification"]);
  assert.equal(cs[4].line, "2 tasks, 1 criterion to prove, then your verdict.");
});

test("the gate text the decision card shows is not repeated in a chapter", () => {
  const doc = { ...FULL, gates: { ...FULL.gates, plan: { state: "pending", hash: H("e") } }, needs: [{ kind: "approve-plan" }] };
  const agreed = chapters({ needs: "approval" }, doc, { skip: ["Plan"] }).find((c) => c.n === 2);
  assert.deepEqual(agreed.sections.map((s) => s.name), ["Requirements", "Acceptance criteria"]);
  assert.equal(agreed.state, "you");
});

test("at title the chapters hold only what title sends: the task line and the verification summary", () => {
  const cs = chapters({ needs: "verdict" }, { ...TITLE, status: "testing" });
  assert.deepEqual(cs.map((c) => c.name), ["Agreed", "Doing", "Proven", "Left"]);
  assert.equal(cs.find((c) => c.name === "Doing").line, "Tasks: 1 of 3");
  assert.equal(cs.find((c) => c.name === "Proven").line, "All 14 jobs export; Excel opens every file.");
  assert.deepEqual(chapters({ needs: null }, KEY).map((c) => c.name), ["Agreed"]);
  // the verdict card shows the summary already: the chapter does not repeat it
  assert.equal(chapters({ needs: "verdict" }, { ...TITLE, status: "testing" }, { summaryShown: true }).find((c) => c.n === 4), undefined);
});

test("more sections: what no chapter holds, never the log", () => {
  const doc = { sections: { Ask: "a", Decisions: "d", Findings: "", Log: "x", Proposal: "p", Tasks: "- [ ] T1" } };
  assert.deepEqual(moreSections(doc).map((s) => s.name), ["Decisions", "Proposal"]);
});

test("a card answers in place only one open single or confirm question with its hash", () => {
  const qa = quickAnswer(TITLE);
  assert.equal(qa.question.id, "Q1");
  assert.deepEqual(qa.options.map((o) => [o.n, o.key, o.label, o.rec]), [[1, "A", "ISO 8601", true], [2, "B", "Local time", false]]);
  const q = TITLE.questions[0];
  assert.equal(quickAnswer({ questions: [q, { ...q, id: "Q2" }] }), null);                    // two open: on the ticket
  assert.equal(quickAnswer({ questions: [{ ...q, type: "multi" }] }), null);
  assert.equal(quickAnswer({ questions: [{ ...q, type: "text", options: [] }] }), null);
  assert.equal(quickAnswer({ questions: [{ ...q, hash: undefined }] }), null);
  assert.equal(quickAnswer({ questions: [{ ...q, answer: "A" }] }), null);
  assert.deepEqual(quickAnswer({ questions: [{ ...q, type: "confirm", options: [] }] }).options.map((o) => o.key), ["yes", "no"]);
  assert.equal(quickAnswer(KEY), null);
});

test("a gate card says how long the plan is and what opens", () => {
  const doc = { ...FULL, gates: { ...FULL.gates, plan: { state: "pending", hash: H("f") } }, needs: [{ kind: "approve-plan" }] };
  assert.equal(planSteps(doc), 2);
  assert.equal(cardLine({ needs: "approval" }, doc), "2 steps · claude-code waits");
  assert.equal(cardAction({ needs: "approval" }, doc), "Read plan and approve");
  assert.equal(cardLine({ needs: "approval" }, { ...TITLE, needs: [{ kind: "approve-plan" }] }), "Read it on the desktop · claude-code waits");
  assert.equal(cardAction({ needs: "verdict" }, TITLE), "Review and decide");
  assert.equal(cardLine({ needs: "verdict" }, TITLE), "All 14 jobs export; Excel opens every file.");
});

test("dayMonth", () => {
  assert.equal(dayMonth("2026-10-02T09:00Z"), "02.10");
  assert.equal(dayMonth(null), "");
});

test("a cost that says nothing is hidden", () => {
  for (const c of ["none", "None", "-", "n/a", "0", "", null, undefined]) assert.equal(costText(c), null, String(c));
  assert.equal(costText("2 h"), "2 h");
  assert.equal(quickAnswer(TITLE).options[0].cost, null);          // the example's "none"
});

test("a question with a hidden character is not answered in place", () => {
  const q = TITLE.questions[0];
  assert.equal(quickAnswer({ questions: [{ ...q, text: "Which\u202E format?" }] }), null);
  assert.equal(quickAnswer({ questions: [{ ...q, options: [{ key: "A", label: "ISO\u200B", cost: null }] }] }), null);
});

test("the move chip is the document's move (schema 1.4) when it carries one", () => {
  const you = { ...TITLE, move: { who: "you", kind: "approve-requirements", label: "Approve requirements and plan", ref: "requirements" } };
  assert.deepEqual(moveChip({ needs: "approval" }, you), { role: "you", icon: "dot", text: "Approve requirements and plan" });
  const agent = { ...TITLE, move: { who: "agent", kind: "work", label: "claude-code is working · T2", ref: "T2" } };
  assert.deepEqual(moveChip({ needs: null }, agent), { role: "info", icon: "half", text: "claude-code is working · T2" });
  assert.equal(moveChip({ needs: null }, { ...TITLE, move: { who: "nobody", kind: "done", label: "Done" } }).role, "neu");
  // a move without a usable label falls back to the local rules; hidden characters never reach the chip
  assert.equal(moveChip({ needs: "question", open_questions: 1 }, { ...TITLE, move: { who: "you", label: "" } }).text, "Your move: answer");
  assert.equal(moveChip({ needs: "question", open_questions: 1 }, { ...TITLE, move: { who: "you", label: "Ans\u202Ewer" } }).text,
    "Your move: answer");
});
