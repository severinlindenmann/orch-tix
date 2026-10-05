// Per-ticket phone notifications: the pure bits of the phone's switch (mirror-model.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import { NOTIFY_LABEL, notifyHelp, notifyOn, notifyPath } from "../../fileshare/static/js/mirror-model.js";

test("a mirror notifies only when the server says so (default off)", () => {
  assert.equal(notifyOn({ id: "TIX-1" }), false);
  assert.equal(notifyOn({ notify: false }), false);
  assert.equal(notifyOn({ notify: "true" }), false);
  assert.equal(notifyOn({ notify: true }), true);
  assert.equal(notifyOn(null), false);
});

test("the help text says the ticket stays in Needs you while off", () => {
  assert.match(notifyHelp(false), /still shows in Needs you/);
  assert.match(notifyHelp(true), /buzzes/);
  assert.equal(NOTIFY_LABEL, "Notify me about this ticket");
});

test("the request path is the ticket's own, whole numbers only", () => {
  assert.equal(notifyPath(42), "/api/mirrors/TIX-42/notify");
  assert.equal(notifyPath("7"), "/api/mirrors/TIX-7/notify");
});
