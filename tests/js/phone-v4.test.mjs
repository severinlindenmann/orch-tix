// Phone v4 (orch-core M-report "TIX"): the journey, the blocking headline, the agents' task bar, the proof list and
// the pinned images a ticket may show, all from the sealed document. Pure model (ticket-card.js); node tests it.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  acItems, agreedNote, blockingCount, doneNote, signerText, gateImage, isBlocking, journey, pinnedImages, proofImage, taskBar, taskChip,
} from "../../fileshare/static/js/ticket-card.js";

const H = (c) => c.repeat(64);
const approved = { state: "approved", hash: "sha256:" + H("a") };
const pending = { state: "pending", hash: "sha256:" + H("b") };

test("the journey: five stages from status and gates, Asked always done", () => {
  const j = (status, gates = { requirements: approved, plan: approved }) => journey({ status, gates }).map((s) => s.state);
  assert.deepEqual(j("backlog", {}), ["done", "now", "todo", "todo", "todo"]);
  assert.deepEqual(j("open", { requirements: pending, plan: pending }), ["done", "now", "todo", "todo", "todo"]);
  assert.deepEqual(j("in-progress"), ["done", "done", "now", "todo", "todo"]);
  assert.deepEqual(j("waiting"), ["done", "done", "now", "todo", "todo"]);
  assert.deepEqual(j("testing"), ["done", "done", "done", "now", "todo"]);
  assert.deepEqual(j("done"), ["done", "done", "done", "done", "done"]);
  // a plan skipped by size: the plan gate stays pending, the work has started, so Agreed is done
  assert.deepEqual(j("in-progress", { requirements: approved, plan: pending }), ["done", "done", "now", "todo", "todo"]);
  assert.deepEqual(j("testing", { requirements: approved, plan: pending }), ["done", "done", "done", "now", "todo"]);
  assert.deepEqual(j("open", { requirements: approved, plan: pending }), ["done", "now", "todo", "todo", "todo"]);
  assert.deepEqual(j("in-progress", { requirements: pending, plan: pending }), ["done", "now", "todo", "todo", "todo"]);
  // a plan waiting for approval is not agreed, whatever the status
  assert.deepEqual(journey({ status: "waiting", gates: { requirements: approved, plan: pending }, needs: [{ kind: "approve-plan" }] })
    .map((x) => x.state), ["done", "now", "todo", "todo", "todo"]);
  assert.deepEqual(journey({ status: "done" }).map((s) => s.name), ["Asked", "Agreed", "Doing", "Proven", "Done"]);
});

test("who agreed is never named: the mirror carries no signed ledger, so core's 'not signed here' wording", () => {
  assert.equal(agreedNote({ gates: { requirements: approved, plan: approved } }), "req + plan, approval not signed here");
  assert.equal(agreedNote({ gates: { requirements: approved, plan: pending } }), "req, approval not signed here");
  assert.equal(agreedNote({ gates: { requirements: pending, plan: pending } }), "waits for your approval");
  assert.equal(doneNote({ status: "done" }), "closed, not signed here");
  assert.equal(doneNote({ status: "testing" }), "your verdict");
});

test("the headline counts blocking decisions only", () => {
  const q = (blocking) => ({ id: "Q1", answer: null, blocking });
  const rows = [
    { needs: "approval", doc: {} }, { needs: "verdict", doc: {} },
    { needs: "question", doc: { questions: [q(true)] } }, { needs: "question", doc: { questions: [q(false)] } },
    { needs: null, doc: {} },
  ];
  assert.deepEqual(rows.map(isBlocking), [true, true, true, false, false]);
  assert.equal(blockingCount(rows), 3);
});

test("the agents' 5-segment task bar and chip", () => {
  const doc = (done, total, doing) => ({ tasks: { summary: { done, total }, doing } });
  assert.deepEqual(taskBar(doc(3, 5, "T4")), ["done", "done", "done", "doing", "todo"]);
  assert.deepEqual(taskBar(doc(1, 10, "T2")), ["todo", "doing", "todo", "todo", "todo"].map((s, i) => (i === 0 ? "doing" : "todo")));
  assert.deepEqual(taskBar(doc(5, 5, null)), ["done", "done", "done", "done", "done"]);
  assert.deepEqual(taskBar({}), ["todo", "todo", "todo", "todo", "todo"]);
  assert.equal(taskChip(doc(3, 5, "T4")), "T4/5");
  assert.equal(taskChip(doc(3, 5, null)), "3/5");
  assert.equal(taskChip({}), "");
});

test("acceptance criteria as proof lines", () => {
  const doc = { sections: { "Acceptance criteria": "- [x] every cluster has the tag\n- [ ] backfill\n* [ ] policy\nprose" } };
  assert.deepEqual(acItems(doc), [{ n: 1, text: "every cluster has the tag", done: true },
    { n: 2, text: "backfill", done: false }, { n: 3, text: "policy", done: false }]);
});

const ITEMS = [
  { source: "file", kind: "screenshot", label: "cluster list", name: "clusters.png", sha256: H("c"), ac: 1 },
  { source: "file", kind: "screenshot", label: "export dialog", name: "dialog.png", sha256: H("d") },
  { source: "file", kind: "report", label: "report", name: "report.md", sha256: H("e") },
  { source: "file", kind: "screenshot", label: "no hash", name: "nohash.png" },
  { source: "file", kind: "screenshot", label: "not shared", name: "notshared.png", sha256: H("f") },
  { source: "link", kind: "link", label: "PR #31" },
];
const CONTEXT = [{ name: "clusters.png", file: "FILE7" }, { name: "dialog.png", file: "FILE8" }, { name: "report.md", file: "FILE9" },
  { name: "nohash.png", file: "FILE10" }];

test("pinned images: an image item with a sha256 that reached the phone as a FILE, nothing else", () => {
  const imgs = pinnedImages({ artifact_items: ITEMS, context_artifacts: CONTEXT });
  assert.deepEqual(imgs.map((i) => [i.name, i.file, i.sha256]), [["clusters.png", "FILE7", H("c")], ["dialog.png", "FILE8", H("d")]]);
  assert.deepEqual(pinnedImages({ artifact_items: [{ ...ITEMS[0], sha256: "sha256:" + H("c") }], context_artifacts: CONTEXT }), []);
  assert.deepEqual(pinnedImages({ artifact_items: [{ ...ITEMS[0], name: "../x.png" }], context_artifacts: [{ name: "../x.png", file: "FILE1" }] }), []);
});

test("the gate image: the first ![…](artifact:name) in the gated text that is pinned; the proof image proves an AC", () => {
  const doc = { artifact_items: ITEMS, context_artifacts: CONTEXT,
    sections: { Requirements: "See ![dialog](artifact:dialog.png) and ![x](https://evil.example/x.png)", Plan: "1. Go" },
    gates: { requirements: { covers: ["Requirements"] }, plan: { covers: ["Plan"] } } };
  assert.equal(gateImage(doc, ["requirements", "plan"]).name, "dialog.png");
  assert.equal(gateImage({ ...doc, sections: { Requirements: "![x](https://evil.example/x.png)" } }, ["requirements"]), null);
  assert.equal(proofImage(doc).name, "clusters.png");
});

test("the board's model work for 300 tickets stays well under a frame budget", () => {
  const docs = Array.from({ length: 300 }, (_, i) => ({ id: `DEMO-${i}`, status: i % 3 ? "in-progress" : "testing",
    gates: { requirements: approved, plan: approved }, tasks: { summary: { done: i % 5, total: 5 }, doing: "T2" },
    sections: { "Acceptance criteria": "- [x] a\n- [ ] b" }, artifact_items: ITEMS, context_artifacts: CONTEXT }));
  const t0 = performance.now();
  for (const d of docs) { journey(d); taskBar(d); taskChip(d); acItems(d); pinnedImages(d); }
  assert.ok(performance.now() - t0 < 100, `${performance.now() - t0} ms`);
});

test("who signed: wording per value, only for an approved gate, nothing assumed without a signed block", () => {
  const ok = { state: "approved" }, inv = { state: "invalidated" };
  const d = (signed, gates) => ({ gates, signed });
  assert.equal(signerText({ signed: true, by: "you" }), "by you");
  assert.equal(signerText({ signed: true, by: "from your phone" }), "from your phone");
  assert.equal(signerText({ signed: true, by: "by your epic charter" }), "by your epic charter");
  assert.equal(signerText({ signed: false, by: "by delegation" }), "by delegation, not signed here");
  assert.equal(signerText({ signed: false, by: null }), "not signed here");
  assert.equal(signerText({ by: "you" }), "");
  assert.equal(signerText(null), "");
  const s = { requirements: { signed: true, by: "you" }, plan: { signed: true, by: "from your phone" } };
  assert.equal(agreedNote(d(s, { requirements: ok, plan: ok })), "req by you, plan from your phone");
  assert.equal(agreedNote(d({ ...s, plan: s.requirements }, { requirements: ok, plan: ok })), "req + plan by you");
  // an invalidated gate shows no signer, even with a matching ledger entry for its old hash
  assert.equal(agreedNote(d(s, { requirements: ok, plan: inv })), "req by you");
  assert.equal(doneNote({ status: "done", signed: { verdict: { signed: true, by: "accepted" } } }), "accepted");
  assert.equal(doneNote({ status: "done", signed: { verdict: { signed: false, by: null } } }), "not signed here");
  assert.equal(doneNote({ status: "testing", signed: { verdict: { signed: true, by: "accepted" } } }), "your verdict");
});
