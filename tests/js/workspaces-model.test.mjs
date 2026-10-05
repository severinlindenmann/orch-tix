// The status page's pure helpers (Remote R9): words, grouping, and "unknown is never zero".
import { test } from "node:test";
import assert from "node:assert/strict";
import { STATE, actionFor, activity, groupByMachine, needsYou, openHref, seenText, stateOf } from "../../fileshare/static/js/workspaces-model.js";

const online = { id: "a".repeat(32), state: "online", sessions: 2, in_progress: 1, needs_you: 3, factory: "running",
  children_done: 4, children_total: 9, budget_pct: 37, last_seen: 1000 };

test("every server state has a pill whose text names it", () => {
  for (const k of ["online", "not_answering", "lost", "stopped", "never_started"]) assert.ok(STATE[k][2].length > 3);
  assert.equal(stateOf({ state: "weird" })[2], "Unknown");
  assert.equal(stateOf(undefined)[2], "Unknown");
});

test("activity lists real numbers for an answering host", () => {
  assert.deepEqual(activity(online), ["2 sessions working", "1 in progress", "Factory running · 4 of 9 done · 37% of budget"]);
  assert.deepEqual(activity({ ...online, sessions: 1 })[0], "1 session working");
});

test("an absent or zero value is not shown as a number, and a silent host shows nothing", () => {
  assert.deepEqual(activity({ state: "online", sessions: 0, in_progress: 0, factory: "none", children_done: null, children_total: null, budget_pct: null }), []);
  assert.deepEqual(activity({ state: "online", sessions: null, factory: "paused", budget_pct: undefined }), ["Factory paused"]);
  for (const state of ["lost", "stopped", "never_started"]) assert.deepEqual(activity({ ...online, state }), []);
});

test("needs-you prefers the opened snapshot and falls back to an online host, else unknown", () => {
  const rows = [{ space: online.id, doc: {}, needs: "question" }, { space: online.id, doc: null, needs: "question" }, { space: "x", doc: {}, needs: "approval" }];
  assert.equal(needsYou(online, rows), 1);
  assert.equal(needsYou(online, []), 0);
  assert.equal(needsYou(online, null), 3);
  assert.equal(needsYou({ ...online, state: "lost" }, null), null);
  assert.equal(needsYou({ state: "never_started", needs_you: null }, undefined), null);
});

test("seen text uses the server's clock", () => {
  assert.equal(seenText({ last_seen: 1000 }, 1010), "seen just now");
  assert.equal(seenText({ last_seen: 1000 }, 1000 + 300), "seen 5 min ago");
  assert.equal(seenText({ last_seen: 1000 }, 1000 + 7200), "seen 2 h ago");
  assert.equal(seenText({ last_seen: 1000 }, 900), "seen just now");           // never in the future
  assert.equal(seenText({ last_seen: null }, 1000), "");
});

test("workspaces group by machine, both sorted, with a fallback for an unnamed one", () => {
  const spaces = [{ id: "1", owner_name: "mini" }, { id: "2", owner_name: "air" }, { id: "3", owner_name: "mini" }, { id: "4", owner_name: " " }];
  const labels = new Map([["1", "Zed"], ["3", "Alpha"], ["2", "Beta"]]);
  const g = groupByMachine(spaces, labels);
  assert.deepEqual(g.map((x) => x.machine), ["A machine", "air", "mini"]);
  assert.deepEqual(g[2].spaces.map((s) => s.label), ["Alpha", "Zed"]);
  assert.equal(g[0].spaces[0].label, "A workspace");
  assert.deepEqual(groupByMachine(null), []);
});

test("Open while the host answers, Snapshot otherwise; the link carries only the id", () => {
  assert.equal(actionFor(online), "Open");
  assert.equal(actionFor({ state: "lost" }), "Snapshot");
  assert.equal(openHref("a b"), "/workspaces?open=a%20b");
});
