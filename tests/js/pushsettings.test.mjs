// tests/js/pushsettings.test.mjs — the Settings notification card's pure helpers (spec T7, T10).
import { test } from "node:test";
import assert from "node:assert/strict";
import { TEXT, b64uToBytes, isIos, isStandalone, pushState, pushSupported, subscribeBody } from "../../fileshare/static/js/pushsettings.js";

test("the card's state: iOS outside the Home Screen app, unsupported, blocked, on, off", () => {
  const base = { ios: false, standalone: false, supported: true, permission: "default", subscribed: false };
  assert.equal(pushState({ ...base, ios: true, supported: false }), "ios");
  assert.equal(pushState({ ...base, ios: true, standalone: true, permission: "granted", subscribed: true }), "on");
  assert.equal(pushState({ ...base, supported: false }), "unsupported");
  assert.equal(pushState({ ...base, permission: "denied" }), "blocked");
  assert.equal(pushState({ ...base, permission: "granted", subscribed: true }), "on");
  assert.equal(pushState({ ...base, permission: "granted" }), "off");
  assert.equal(pushState({ ...base, subscribed: true }), "off", "a subscription without permission isn't on");
  assert.equal(TEXT.ios, "Add tix to your Home Screen to get notifications");
});

test("iOS and standalone detection", () => {
  assert.equal(isIos({ userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)" }), true);
  assert.equal(isIos({ userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", maxTouchPoints: 5 }), true, "iPadOS");
  assert.equal(isIos({ userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", maxTouchPoints: 0 }), false);
  assert.equal(isStandalone({ matchMedia: () => ({ matches: true }) }, {}), true);
  assert.equal(isStandalone({ matchMedia: () => ({ matches: false }) }, { standalone: true }), true);
  assert.equal(isStandalone({ matchMedia: () => ({ matches: false }) }, {}), false);
  assert.equal(pushSupported({ PushManager: 1, Notification: 1 }, { serviceWorker: {} }), true);
  assert.equal(pushSupported({ Notification: 1 }, { serviceWorker: {} }), false);
});

test("the VAPID key decodes from base64url, and the subscribe body is {endpoint, keys}", () => {
  assert.deepEqual([...b64uToBytes("BAEC_w")], [4, 1, 2, 255]);
  const sub = { toJSON: () => ({ endpoint: "https://push.example/abc", expirationTime: null, keys: { p256dh: "P", auth: "A" } }) };
  assert.deepEqual(subscribeBody(sub), { endpoint: "https://push.example/abc", keys: { p256dh: "P", auth: "A" } });
});
