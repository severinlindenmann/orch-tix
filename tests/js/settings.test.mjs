import { test } from "node:test";
import assert from "node:assert/strict";
import * as s from "../../fileshare/static/js/settings.js";

test("checkKey trims, rejects empty and non-key values", () => {
  assert.deepEqual(s.checkKey("  abcdef0123456789  "), { value: "abcdef0123456789" });
  for (const bad of ["", "   ", null, undefined]) assert.match(s.checkKey(bad).error, /Remove/);
  for (const bad of ["a b".repeat(8), "tab\there-long-enough", "x".repeat(257), "x".repeat(15), "ünïcode-key-0123456", "line\nbreak-long-enough"]) {
    assert.ok(s.checkKey(bad).error, JSON.stringify(bad));
  }
  assert.deepEqual(s.checkKey("x".repeat(256)), { value: "x".repeat(256) });
});

test("statusText", () => {
  const now = Date.parse("2026-09-24T12:10:00Z");
  assert.equal(s.statusText({}, null, now), "Not set");
  assert.equal(s.statusText({ deepgram_api_key: "" }, "2026-09-24T12:00:00Z", now), "Not set");
  assert.equal(s.statusText({ other: 1 }, "2026-09-24T12:00:00Z", now), "Not set");
  assert.equal(s.statusText({ deepgram_api_key: "k" }, "2026-09-24T12:00:00Z", now), "Configured ✓ · updated 10 min ago");
  assert.equal(s.statusText({ deepgram_api_key: "k" }, null, now), "Configured ✓");
});

test("withKey and withoutKey keep unknown keys", () => {
  const obj = { future_setting: { a: 1 }, deepgram_api_key: "old" };
  assert.deepEqual(s.withKey(obj, "new"), { future_setting: { a: 1 }, deepgram_api_key: "new" });
  assert.deepEqual(s.withoutKey(obj), { future_setting: { a: 1 } });
  assert.deepEqual(obj, { future_setting: { a: 1 }, deepgram_api_key: "old" });   // not mutated
});

test("settings.js takes the transcription constants from transcribe-settings.js, not autotranscribe.js", async () => {
  const { readFileSync } = await import("node:fs");
  const src = readFileSync(new URL("../../fileshare/static/js/settings.js", import.meta.url), "utf8");
  assert.ok(!src.includes("autotranscribe.js"));
  assert.match(src, /from "\.\/transcribe-settings\.js"/);
});

test("transcription settings: read with defaults, written keeping every other key", () => {
  assert.deepEqual(s.transcriptionOf({}), { language: "de", auto: true });
  assert.deepEqual(s.transcriptionOf({ transcribe_language: "de-CH", auto_transcribe: false }), { language: "de-CH", auto: false });
  assert.deepEqual(s.transcriptionOf({ transcribe_language: "xx", auto_transcribe: 0 }), { language: "de", auto: false });
  const obj = { future_setting: [1], deepgram_api_key: "k" };
  assert.deepEqual(s.withTranscription(obj, { language: "en", auto: false }),
    { future_setting: [1], deepgram_api_key: "k", transcribe_language: "en", auto_transcribe: false });
  assert.deepEqual(obj, { future_setting: [1], deepgram_api_key: "k" });
  assert.throws(() => s.withTranscription(obj, { language: "fr", auto: true }), TypeError);
  assert.throws(() => s.withTranscription(obj, { language: "de", auto: "yes" }), TypeError);
});
