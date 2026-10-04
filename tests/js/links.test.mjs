// tests/js/links.test.mjs — public links in the web UI (spec §17): the owner dialog's pure helpers
// (URL building, the expiry options capped at the file's life, the download limit) and the
// viewer's parsing of the token and the key from the URL.
import { test } from "node:test";
import assert from "node:assert/strict";
import { b64u, unb64u } from "../../fileshare/static/js/crypto.js";
import { cutByMoreThanAnHour, linkUrl, linkTtlChoices, maxDownloadsValue } from "../../fileshare/static/js/linkshare.js";
import { readLinkKey, readToken } from "../../fileshare/static/js/public.js";

const H = 3600_000;
const D = 24 * H;
const NOW = Date.parse("2026-09-25T12:00:00Z");
const iso = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
const TOKEN = "A".repeat(20) + "b_-" + "9".repeat(20); // 43 chars of base64url

test("the link URL is origin + /p/ + token + # + b64u(LK)", () => {
  const lk = new Uint8Array(32).map((_, i) => i * 7 + 250);
  const url = linkUrl("https://tix.severin.io", TOKEN, lk);
  assert.equal(url, `https://tix.severin.io/p/${TOKEN}#${b64u(lk)}`);
  const u = new URL(url);
  assert.equal(u.pathname, `/p/${TOKEN}`);
  assert.deepEqual(unb64u(u.hash.slice(1)), lk);
  assert.throws(() => linkUrl("https://x", TOKEN, new Uint8Array(31)));
  assert.throws(() => linkUrl("https://x", "not a token", lk));
});

test("every expiry option is open for a file that never expires; 7 days is the default", () => {
  const c = linkTtlChoices(null, NOW);
  assert.deepEqual(c.options.map((o) => [o.ttl, o.label, o.disabled]), [
    ["1h", "1 hour", false], ["1d", "1 day", false], ["7d", "7 days", false], ["30d", "30 days", false],
  ]);
  assert.equal(c.def, "7d");
  assert.equal(linkTtlChoices(iso(NOW + 60 * D), NOW).def, "7d");
});

test("an option fits the file's remaining life, or is the smallest one past it (the server clamps it)", () => {
  const pick = (left) => {
    const c = linkTtlChoices(iso(NOW + left), NOW);
    return [c.options.filter((o) => !o.disabled).map((o) => o.ttl), c.def];
  };
  assert.deepEqual(pick(40 * D), [["1h", "1d", "7d", "30d"], "7d"]);
  assert.deepEqual(pick(30 * D), [["1h", "1d", "7d", "30d"], "7d"]);
  assert.deepEqual(pick(10 * D), [["1h", "1d", "7d", "30d"], "7d"]);   // 30 d is clamped to 10
  assert.deepEqual(pick(7 * D), [["1h", "1d", "7d", "30d"], "7d"]);
  assert.deepEqual(pick(7 * D - 2 * H), [["1h", "1d", "7d"], "7d"]);   // a 7-day file uploaded 2 h ago
  assert.deepEqual(pick(3 * D), [["1h", "1d", "7d"], "7d"]);
  assert.deepEqual(pick(D), [["1h", "1d", "7d"], "7d"]);
  assert.deepEqual(pick(D - 1000), [["1h", "1d"], "1d"]);
  assert.deepEqual(pick(5 * H), [["1h", "1d"], "1d"]);
  assert.deepEqual(pick(H), [["1h", "1d"], "1d"]);
  assert.deepEqual(pick(10 * 60_000), [["1h"], "1h"]);
});

test("the default is the longest allowed option up to 7 days", () => {
  assert.equal(linkTtlChoices(iso(NOW + 100 * D), NOW).def, "7d");
  assert.equal(linkTtlChoices(iso(NOW + 20 * H), NOW).def, "1d");
  assert.equal(linkTtlChoices(iso(NOW + 30 * 60_000), NOW).def, "1h");
});

test("the hint shows only when the chosen option is cut by more than an hour", () => {
  const left = (ms) => linkTtlChoices(iso(NOW + ms), NOW).left;
  assert.equal(cutByMoreThanAnHour("7d", left(7 * D - 60_000)), false);  // uploaded a minute ago
  assert.equal(cutByMoreThanAnHour("7d", left(7 * D - 2 * H)), true);
  assert.equal(cutByMoreThanAnHour("1d", left(D - 59 * 60_000)), false);
  assert.equal(cutByMoreThanAnHour("30d", left(10 * D)), true);
  assert.equal(cutByMoreThanAnHour("1h", left(10 * 60_000)), false);
  assert.equal(cutByMoreThanAnHour("30d", linkTtlChoices(null, NOW).left), false);
});

test("an unparseable file expiry is treated as no cap", () => {
  assert.equal(linkTtlChoices("garbage", NOW).def, "7d");
  assert.equal(linkTtlChoices("garbage", NOW).left, Infinity);
  assert.ok(linkTtlChoices("garbage", NOW).options.every((o) => !o.disabled));
});

test("the download limit is null when off, else an integer from 1 to 1000", () => {
  assert.deepEqual(maxDownloadsValue(false, "abc"), { value: null });
  assert.deepEqual(maxDownloadsValue(true, "1"), { value: 1 });
  assert.deepEqual(maxDownloadsValue(true, " 1000 "), { value: 1000 });
  for (const bad of ["0", "1001", "-3", "2.5", "", "1e2", "abc", "12x"]) {
    assert.ok(maxDownloadsValue(true, bad).error, bad);
  }
});

test("the viewer reads the token from /p/<43 b64u chars> only", () => {
  assert.equal(readToken(`/p/${TOKEN}`), TOKEN);
  assert.equal(readToken(`/p/${TOKEN}/`), null);
  assert.equal(readToken(`/p/${TOKEN.slice(1)}`), null);
  assert.equal(readToken(`/p/${TOKEN}A`), null);
  assert.equal(readToken(`/p/${TOKEN.slice(1)}+`), null);
  assert.equal(readToken("/"), null);
  assert.equal(readToken(`/x/${TOKEN}`), null);
});

test("the viewer accepts a key only as canonical base64url of exactly 32 bytes", () => {
  const lk = new Uint8Array(32).map((_, i) => 255 - i);
  const frag = b64u(lk);
  assert.deepEqual(readLinkKey(`#${frag}`), lk);
  assert.deepEqual(readLinkKey(frag), lk);
  assert.equal(readLinkKey(""), null);
  assert.equal(readLinkKey("#"), null);
  assert.equal(readLinkKey(`#${frag.slice(0, -1)}`), null);           // truncated
  assert.equal(readLinkKey(`#${frag}AAAA`), null);                     // too long
  assert.equal(readLinkKey(`#${b64u(new Uint8Array(31))}`), null);
  assert.equal(readLinkKey(`#${frag.slice(0, -1)}+`), null);           // not base64url
  assert.equal(readLinkKey(`#${frag}=`), null);                        // padded
  const ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  const loose = ALPHA[ALPHA.indexOf(frag.at(-1)) | 1];                  // sets a padding bit
  assert.equal(readLinkKey(`#${frag.slice(0, -1)}${loose}`), null);    // non-canonical last char
  assert.equal(readLinkKey(null), null);
});

test("downloads_left: null is unlimited; anything else counts as limited", async () => {
  const { downloadsLeft, previewLabel } = await import("../../fileshare/static/js/public.js");
  assert.equal(downloadsLeft({ downloads_left: null }), null);
  assert.equal(downloadsLeft({}), null);
  assert.equal(downloadsLeft({ downloads_left: 3 }), 3);
  for (const bad of ["3", 2.5, -1, true, {}]) assert.equal(downloadsLeft({ downloads_left: bad }), 0, String(bad));
  assert.equal(previewLabel(3), "Preview (uses 1 of 3 downloads left)");
  assert.equal(previewLabel(1), "Preview (uses 1 of 1 download left)");
});

test("the viewer's import graph holds no session, key store or IndexedDB module", async () => {
  const { readFileSync } = await import("node:fs");
  const dir = new URL("../../fileshare/static/js/", import.meta.url);
  const seen = new Set();
  const walk = (name) => {
    if (seen.has(name)) return;
    seen.add(name);
    const src = readFileSync(new URL(name, dir), "utf8");
    for (const m of src.matchAll(/^\s*(?:import|export)\s[^;]*?from\s+"\.\/([\w.-]+)"/gm)) walk(m[1]);
    for (const m of src.matchAll(/^\s*import\s+"\.\/([\w.-]+)"/gm)) walk(m[1]);
  };
  walk("public.js");
  for (const banned of ["api.js", "keystore.js", "db.js", "preview.js", "swreg.js", "outbox.js"]) {
    assert.ok(!seen.has(banned), `public.js pulls in ${banned}: ${[...seen].join(", ")}`);
  }
  assert.ok(seen.has("render.js") && seen.has("crypto.js"));
  for (const name of seen) {
    assert.ok(!/indexedDB|serviceWorker|localStorage/.test(readFileSync(new URL(name, dir), "utf8")), name);
  }
});
