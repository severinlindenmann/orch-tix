// tests/js/ticketreply-model.test.mjs — the reply, testing and comment boxes' pure logic (spec T10,
// T5): required-answer gating, the answer and verdict bodies, the recommended mark, the POST body.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  buildAnswer, buildComment, buildVerdict, canGiveVerdict, canSend, eventRequest, isAnswered, isRecommended,
  latestOfKind, withVoiceFile,
} from "../../fileshare/static/js/ticketreply-model.js";

const QS = [
  { id: "source", text: "Where from?", type: "single", options: ["Most used", "Recent"], recommended: "Most used" },
  { id: "extras", text: "Which extras?", type: "multi", options: ["a", "b", "c"], recommended: ["a", "c"] },
  { id: "preselect", text: "Pre-select?", type: "confirm", recommended: false },
  { id: "why", text: "Anything else?", type: "text", required: false },
];

test("Send stays disabled until every required single, multi and confirm question is answered", () => {
  assert.equal(canSend(QS, {}), false);
  assert.equal(canSend(QS, { source: "Most used" }), false);
  assert.equal(canSend(QS, { source: "Most used", extras: ["b"] }), false);
  assert.equal(canSend(QS, { source: "Most used", extras: [], preselect: true }), false, "an empty multi is no answer");
  // the optional text question doesn't block
  assert.equal(canSend(QS, { source: "Most used", extras: ["b"], preselect: false }), true);
  // a confirm answered No is an answer
  assert.equal(isAnswered(QS[2], false), true);
  assert.equal(isAnswered(QS[2], undefined), false);
  // a single answer must be one of the options
  assert.equal(canSend(QS, { source: "Something else", extras: ["b"], preselect: true }), false);
});

test("a required text question blocks until it has text; required defaults to true", () => {
  const qs = [{ id: "name", text: "Name it", type: "text" }];
  assert.equal(canSend(qs, {}), false);
  assert.equal(canSend(qs, { name: "   " }), false);
  assert.equal(canSend(qs, { name: "tix" }), true);
  const optional = [{ id: "s", text: "Pick", type: "single", options: ["x", "y"], required: false }];
  assert.equal(canSend(optional, {}), true);
});

test("the answer body matches T5 exactly", () => {
  const body = buildAnswer({
    questionEvent: "ab".repeat(16), questions: QS,
    answers: { source: "Recent", extras: ["c", "a", "zzz"], preselect: false, why: "  because  ", ghost: "x" },
    text: "  Go ahead.  ",
  });
  assert.deepEqual(body, {
    question_event: "ab".repeat(16),
    answers: { source: "Recent", extras: ["a", "c"], preselect: false, why: "because" },
    text: "Go ahead.",
  });
  assert.deepEqual(Object.keys(body), ["question_event", "answers", "text"]);
});

test("unanswered optional questions and empty text are left out; voice rides along", () => {
  const body = buildAnswer({
    questionEvent: "cd".repeat(16), questions: QS, answers: { source: "Most used", extras: ["b"], preselect: true, why: " " },
    text: "", voice: { file: "FILE94", transcript: "hello there" },
  });
  assert.deepEqual(body, {
    question_event: "cd".repeat(16),
    answers: { source: "Most used", extras: ["b"], preselect: true },
    voice: { file: "FILE94", transcript: "hello there" },
  });
  const failed = buildAnswer({ questionEvent: "cd".repeat(16), questions: [], answers: {}, voice: { file: "FILE9", transcript: null } });
  assert.deepEqual(failed.voice, { file: "FILE9", transcript: null });
});

test("the recommended mark: single, multi and confirm", () => {
  assert.equal(isRecommended(QS[0], "Most used"), true);
  assert.equal(isRecommended(QS[0], "Recent"), false);
  assert.equal(isRecommended(QS[1], "a"), true);
  assert.equal(isRecommended(QS[1], "b"), false);
  assert.equal(isRecommended(QS[2], false), true);
  assert.equal(isRecommended(QS[2], true), false);
  assert.equal(isRecommended({ type: "confirm" }, false), false, "no recommendation, no mark");
});

test("a follow-up verdict needs text or voice; Done needs nothing", () => {
  assert.equal(canGiveVerdict("done", {}), true);
  assert.equal(canGiveVerdict("follow-up", {}), false);
  assert.equal(canGiveVerdict("follow-up", { text: "  " }), false);
  assert.equal(canGiveVerdict("follow-up", { text: "The toast is cut off" }), true);
  assert.equal(canGiveVerdict("follow-up", { voice: { file: "FILE3", transcript: null } }), true);
  assert.equal(canGiveVerdict("maybe", { text: "x" }), false);
  assert.deepEqual(buildVerdict("done", {}), { verdict: "done" });
  assert.deepEqual(buildVerdict("follow-up", { text: " Fix the toast ", voice: { file: "FILE3", transcript: "fix it" } }),
    { verdict: "follow-up", text: "Fix the toast", voice: { file: "FILE3", transcript: "fix it" } });
  assert.throws(() => buildVerdict("follow-up", {}));
});

test("a comment is a plain update body", () => {
  assert.deepEqual(buildComment("  Looks good  "), { text: "Looks good" });
  assert.equal(buildComment("   "), null);
});

test("a voice comment carries text and voice; voice-only is allowed with empty text (spec T14)", () => {
  assert.deepEqual(buildComment(" Check the logs ", { file: "FILE7", transcript: "Check the logs" }),
    { text: "Check the logs", voice: { file: "FILE7", transcript: "Check the logs" } });
  assert.deepEqual(buildComment("  ", { file: "FILE7", transcript: null }),
    { text: "", voice: { file: "FILE7", transcript: null } });
  // queued offline: the file id isn't known yet (withVoiceFile fills it in later)
  assert.deepEqual(buildComment("", { file: null, transcript: null }), { text: "", voice: { file: null, transcript: null } });
  assert.equal(buildComment("", null), null);
});

test("eventRequest is exactly the POST body: files only when there are some, verdict only for verdicts", () => {
  const uuid = "ef".repeat(16);
  assert.deepEqual(eventRequest({ uuid, kind: "answer", encBody: "ENC" }), { uuid, kind: "answer", enc_body: "ENC" });
  assert.deepEqual(eventRequest({ uuid, kind: "answer", encBody: "ENC", files: ["FILE94"] }),
    { uuid, kind: "answer", enc_body: "ENC", files: ["FILE94"] });
  assert.deepEqual(eventRequest({ uuid, kind: "verdict", encBody: "ENC", verdict: "follow-up" }),
    { uuid, kind: "verdict", enc_body: "ENC", verdict: "follow-up" });
  assert.deepEqual(eventRequest({ uuid, kind: "update", encBody: "ENC", verdict: "done" }), { uuid, kind: "update", enc_body: "ENC" });
});

test("withVoiceFile fills in a queued voice note's FILE id, or drops the voice when it's gone", () => {
  const body = { question_event: "q", answers: {}, voice: { file: null, transcript: null } };
  assert.deepEqual(withVoiceFile(body, "FILE7"), { question_event: "q", answers: {}, voice: { file: "FILE7", transcript: null } });
  assert.deepEqual(withVoiceFile(body, null), { question_event: "q", answers: {} });
  assert.equal(body.voice.file, null, "the input is not changed");
});

test("the voice note's transcription outcome is surfaced in the file view's words", async () => {
  const { transcriptNote, STILL_SEND } = await import("../../fileshare/static/js/ticketreply-model.js");
  const { STATUS } = await import("../../fileshare/static/js/autotranscribe.js");
  assert.equal(transcriptNote({ outcome: "added", transcript: { text: "hi" } }, STATUS).ok, true);
  assert.equal(transcriptNote({ outcome: "exists", transcript: { text: "hi" } }, STATUS).ok, true);
  // no key: the Settings link ("Set a Deepgram key in Settings to transcribe."), then the reassurance
  assert.deepEqual(transcriptNote({ outcome: "skipped", reason: "no_key" }, STATUS), { ok: false, text: STILL_SEND, settings: true });
  assert.equal(transcriptNote({ outcome: "failed", reason: "the key was refused" }, STATUS).text,
    `Couldn't transcribe: the key was refused. ${STILL_SEND}`);
  assert.equal(transcriptNote({ outcome: "empty" }, STATUS).text, `No speech detected. ${STILL_SEND}`);
  assert.equal(transcriptNote({ outcome: "skipped", reason: "settings_unavailable" }, STATUS).text,
    `Couldn't load settings — try again. ${STILL_SEND}`);
});

test("latestOfKind finds the newest event of a kind", () => {
  const evs = [{ seq: 1, kind: "question", uuid: "a" }, { seq: 2, kind: "answer" }, { seq: 3, kind: "question", uuid: "b" }];
  assert.equal(latestOfKind(evs, "question").uuid, "b");
  assert.equal(latestOfKind(evs, "test"), null);
  assert.equal(latestOfKind(undefined, "test"), null);
});
