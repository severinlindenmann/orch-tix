// The public drop page's pure helpers (upload-links spec, Task 5). drop.js's main() only runs when
// document has #drop, so importing it here in node never touches the DOM or the network.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  readDropToken, readDropKey, zipName, planUpload, failureMessage, MSG, HttpError, PayloadError,
} from "../../fileshare/static/js/drop.js";

const TOKEN = "a".repeat(43);
const PUB = "A".repeat(43) + "AAA"; // placeholder, replaced per-test with real b64u values below

function b64u(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

test("readDropToken accepts exactly /u/<43 chars>", () => {
  assert.equal(readDropToken(`/u/${TOKEN}`), TOKEN);
  assert.equal(readDropToken(`/u/${TOKEN}x`), null);
  assert.equal(readDropToken(`/u/${TOKEN.slice(1)}`), null);
  assert.equal(readDropToken("/p/" + TOKEN), null);
  assert.equal(readDropToken("/u/"), null);
});

test("readDropKey accepts a 65-byte key starting with 0x04, else null", () => {
  const good = new Uint8Array(65);
  good[0] = 4;
  assert.deepEqual(readDropKey(`#${b64u(good)}`), good);

  assert.equal(readDropKey(""), null);
  assert.equal(readDropKey("#"), null);

  const wrongLen = new Uint8Array(64);
  wrongLen[0] = 4;
  assert.equal(readDropKey(`#${b64u(wrongLen)}`), null);

  const wrongPrefix = new Uint8Array(65);
  wrongPrefix[0] = 2;
  assert.equal(readDropKey(`#${b64u(wrongPrefix)}`), null);

  assert.equal(readDropKey("#not*base64url!"), null);
});

test("zipName formats local date/time as upload-YYYYMMDD-HHMM.zip", () => {
  assert.equal(zipName(new Date(2026, 0, 2, 3, 4, 5)), "upload-20260102-0304.zip");
  assert.equal(zipName(new Date(2026, 8, 29, 23, 59, 0)), "upload-20260929-2359.zip");
});

test("planUpload picks single, zip or empty", () => {
  assert.deepEqual(planUpload([]), { kind: "empty", name: null });
  assert.deepEqual(planUpload([{ path: "a.png", size: 10 }]), { kind: "single", name: "a.png" });
  assert.equal(planUpload([{ path: "a.png", size: 1 }, { path: "b.png", size: 1 }]).kind, "zip");
  assert.equal(planUpload([{ path: "dir/a.png", size: 1 }]).kind, "zip");
});

test("failureMessage maps a failed build/encrypt/POST to the right on-page text", () => {
  assert.equal(failureMessage(new HttpError(404)), MSG.gone);
  assert.equal(failureMessage(new HttpError(429)), MSG.busy);
  assert.equal(failureMessage(new HttpError(413)), MSG.tooLarge);
  assert.equal(failureMessage(new PayloadError("unsafe zip entry path: ../x")), MSG.badName);
  // Network errors (status 0) and 5xx all fall back to the generic "try again" message: nothing
  // has claimed the link, so it stays exactly as usable as before.
  assert.equal(failureMessage(new HttpError(0)), MSG.failed);
  assert.equal(failureMessage(new HttpError(500)), MSG.failed);
  assert.equal(failureMessage(new HttpError(503)), MSG.failed);
  assert.equal(failureMessage(new Error("boom")), MSG.failed);
});
