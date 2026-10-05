import { test } from "node:test";
import assert from "node:assert/strict";

globalThis.window = { dispatchEvent() {}, addEventListener() {} };
globalThis.document = globalThis.document ?? {};
const { closestTtl } = await import("../../fileshare/static/js/fileactions.js");

const NOW = Date.parse("2026-10-05T12:00:00Z");
const inDays = (d) => new Date(NOW + d * 86400000).toISOString();

test("Change expiry opens on the option closest to what the file has left", () => {
  assert.equal(closestTtl(inDays(29.9), NOW), "30d");
  assert.equal(closestTtl(inDays(30), NOW), "30d");
  assert.equal(closestTtl(inDays(6.2), NOW), "7d");
  assert.equal(closestTtl(inDays(1.2), NOW), "1d");
  assert.equal(closestTtl(inDays(0.2), NOW), "1d");
  assert.equal(closestTtl(inDays(-3), NOW), "1d");
  assert.equal(closestTtl(inDays(45), NOW), "30d");
});

test("no expiry is never; an unreadable one falls back to the default", () => {
  assert.equal(closestTtl(null, NOW), "never");
  assert.equal(closestTtl("not a date", NOW), "7d");
});
