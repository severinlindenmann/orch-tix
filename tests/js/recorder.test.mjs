// tests/js/recorder.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  pickAudioType, extForAudioType, recordingName, formatElapsed, AUDIO_TYPES, levelOf, litBars,
} from "../../fileshare/static/js/recorder.js";
import { previewKind } from "../../fileshare/static/js/previewkind.js";

const supports = (...types) => (t) => types.includes(t);

test("pickAudioType prefers webm/opus like Chrome and Firefox offer it", () => {
  const chrome = supports("audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus");
  assert.equal(pickAudioType(chrome), "audio/webm;codecs=opus");
  assert.equal(pickAudioType(supports("audio/webm")), "audio/webm");
});

test("pickAudioType falls back to mp4 on Safari", () => {
  assert.equal(pickAudioType(supports("audio/mp4")), "audio/mp4");
  assert.equal(pickAudioType(supports("audio/ogg;codecs=opus")), "audio/ogg;codecs=opus");
});

test("pickAudioType returns '' (browser default) when nothing matches or the probe is missing or throws", () => {
  assert.equal(pickAudioType(() => false), "");
  assert.equal(pickAudioType(undefined), "");
  assert.equal(pickAudioType(() => { throw new Error("nope"); }), "");
});

test("pickAudioType asks in the documented order", () => {
  const asked = [];
  pickAudioType((t) => { asked.push(t); return false; });
  assert.deepEqual(asked, ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]);
  assert.deepEqual(AUDIO_TYPES, asked);
});

test("extForAudioType maps the container to a file extension", () => {
  assert.equal(extForAudioType("audio/webm;codecs=opus"), ".webm");
  assert.equal(extForAudioType("audio/webm"), ".webm");
  assert.equal(extForAudioType("audio/mp4"), ".m4a");
  assert.equal(extForAudioType("audio/mp4;codecs=mp4a.40.2"), ".m4a");
  assert.equal(extForAudioType("audio/ogg;codecs=opus"), ".ogg");
  assert.equal(extForAudioType(""), ".webm");
  assert.equal(extForAudioType(undefined), ".webm");
});

test("recordingName is recording-YYYYMMDD-HHMMSS<ext> in local time, zero-padded", () => {
  assert.equal(recordingName(new Date(2026, 0, 2, 3, 4, 5), ".webm"), "recording-20260102-030405.webm");
  assert.equal(recordingName(new Date(2026, 11, 31, 23, 59, 59), ".m4a"), "recording-20261231-235959.m4a");
});

test("formatElapsed is mm:ss, and minutes keep counting past 59", () => {
  assert.equal(formatElapsed(0), "00:00");
  assert.equal(formatElapsed(1499), "00:01");
  assert.equal(formatElapsed(65_000), "01:05");
  assert.equal(formatElapsed(60 * 60_000), "60:00");
});

test("previewKind: audio by extension", () => {
  for (const [name, type] of [
    ["a.m4a", "audio/mp4"], ["a.mp3", "audio/mpeg"], ["a.ogg", "audio/ogg"], ["a.oga", "audio/ogg"],
    ["a.opus", "audio/ogg"], ["a.wav", "audio/wav"], ["a.aac", "audio/aac"], ["REC.WEBM", "audio/webm"],
  ]) {
    assert.deepEqual(previewKind({ name, mime: "" }), { kind: "audio", type }, name);
  }
});

test("previewKind: audio by mime, which wins over the extension's default", () => {
  assert.deepEqual(previewKind({ name: "x", mime: "audio/flac" }), { kind: "audio", type: "audio/flac" });
  assert.deepEqual(previewKind({ name: "rec.webm", mime: "audio/webm;codecs=opus" }), { kind: "audio", type: "audio/webm" });
  assert.deepEqual(previewKind({ name: "voice.m4a", mime: "audio/x-m4a" }), { kind: "audio", type: "audio/x-m4a" });
});

test("previewKind: .webm with a video mime is not audio", () => {
  assert.equal(previewKind({ name: "clip.webm", mime: "video/webm" }).kind, "none");
  assert.equal(previewKind({ name: "clip.webm", mime: "application/octet-stream" }).kind, "none");
});

test("previewKind: images, text and active formats are unchanged", () => {
  assert.equal(previewKind({ name: "a.png", mime: "" }).kind, "image");
  assert.equal(previewKind({ name: "a.txt", mime: "" }).kind, "text");
  assert.equal(previewKind({ name: "a.svg", mime: "audio/mpeg" }).kind, "none");
});

test("audio has its own 50 MiB preview cap; everything else keeps 2 MiB", async () => {
  const { AUDIO_PREVIEW_MAX, PREVIEW_MAX } = await import("../../fileshare/static/js/preview.js");
  assert.equal(AUDIO_PREVIEW_MAX, 50 * 1024 * 1024);
  assert.equal(PREVIEW_MAX, 2 * 1024 * 1024);
});

test("previewKind: a video/* mime is never audio, whatever the extension", () => {
  for (const name of ["a.m4a", "a.mp3", "a.ogg", "a.oga", "a.opus", "a.wav", "a.aac", "a.webm"]) {
    assert.equal(previewKind({ name, mime: "video/mp4" }).kind, "none", name);
  }
  assert.equal(previewKind({ name: "a.ogg", mime: "video/ogg" }).kind, "none");
  assert.deepEqual(previewKind({ name: "a.ogg", mime: "application/octet-stream" }), { kind: "audio", type: "audio/ogg" });
});

// ---- the level meter (speak a ticket, spec T13)

test("levelOf is the RMS of 8-bit time-domain samples around 128, in [0, 1]", () => {
  assert.equal(levelOf(new Uint8Array([128, 128, 128, 128])), 0);
  assert.equal(levelOf(new Uint8Array([0, 0, 0, 0])), 1);
  const half = levelOf(new Uint8Array([192, 64, 192, 64]));
  assert.ok(Math.abs(half - 0.5) < 1e-9, String(half));
  assert.equal(levelOf(new Uint8Array([])), 0);
});

test("litBars lights more bars for a louder signal, never more than there are", () => {
  assert.equal(litBars(0, 12), 0);
  assert.equal(litBars(1, 12), 12);
  assert.equal(litBars(5, 12), 12);
  assert.ok(litBars(0.05, 12) >= 1, "speech-level input shows");
  assert.ok(litBars(0.1, 12) < litBars(0.3, 12));
});
