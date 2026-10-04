// The Needs you / Tickets status pills (needs.js). Pink (role "you") is only for what needs the human:
// the needs pill, never a ticket's status (design rules; Task 10 review minor).
import { test } from "node:test";
import assert from "node:assert/strict";
import { STATUS } from "../../fileshare/static/js/needs.js";

test("no status pill is pink: waiting is info, not needs-you", () => {
  for (const [status, [role]] of Object.entries(STATUS)) assert.notEqual(role, "you", status);
  assert.equal(STATUS.waiting[0], "warn");
});

test("every status pill has an icon and text", () => {
  for (const [status, [, ic, text]] of Object.entries(STATUS)) assert.ok(ic && text, status);
});

test("asOf is the local HH:MM a cached list arrived", async () => {
  const { asOf } = await import("../../fileshare/static/js/needs.js");
  const at = new Date(2026, 9, 3, 7, 5).getTime();
  assert.equal(asOf(at), "07:05");
  assert.equal(asOf(new Date(2026, 9, 3, 23, 59).getTime()), "23:59");
});
