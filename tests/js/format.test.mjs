import { test } from "node:test";
import assert from "node:assert/strict";
import {
  humanSize, plainSize, relTime, extOf, typeOf, matches, mapLimit, safeDownloadName,
} from "../../fileshare/static/js/format.js";

const MiB = 1 << 20;

test("plainSize inverts the SHR1 v1 framing exactly", () => {
  assert.equal(plainSize(34 + 16), 0);                          // empty file: one empty last chunk
  assert.equal(plainSize(34 + 15 + 16), 15);
  assert.equal(plainSize(34 + MiB + 16), MiB);                   // exact multiple: no trailing empty chunk
  assert.equal(plainSize(34 + MiB + 16 + 1 + 16), MiB + 1);
  assert.equal(plainSize(34 + 3 * (MiB + 16) + 7 + 16), 3 * MiB + 7);
  assert.equal(plainSize(10), 0);                               // malformed: never negative
  assert.equal(plainSize(Number.NaN), 0);
});

test("humanSize uses 1024-based units like the mockups", () => {
  assert.equal(humanSize(0), "0 B");
  assert.equal(humanSize(512), "512 B");
  assert.equal(humanSize(4300), "4.2 KB");
  assert.equal(humanSize(348160), "340 KB");
  assert.equal(humanSize(19293798), "18.4 MB");
  assert.equal(humanSize(1288490189), "1.2 GB");
});

test("relTime covers the ranges shown in the table", () => {
  const now = Date.parse("2026-09-24T12:00:00Z");
  assert.equal(relTime("2026-09-24T11:59:30Z", now), "just now");
  assert.equal(relTime("2026-09-24T12:00:10Z", now), "just now");     // small clock skew
  assert.equal(relTime("2026-09-24T11:58:00Z", now), "2 min ago");
  assert.equal(relTime("2026-09-24T11:00:00Z", now), "1 hour ago");
  assert.equal(relTime("2026-09-24T09:00:00Z", now), "3 hours ago");
  assert.equal(relTime("2026-09-23T10:00:00Z", now), "yesterday");
  assert.equal(relTime("2026-09-20T12:00:00Z", now), "4 days ago");
  assert.equal(relTime("2026-08-01T12:00:00Z", now), "1 Aug 2026");
  assert.equal(relTime("garbage", now), "");
});

test("typeOf classifies by mime first, then extension, and never previews svg", () => {
  assert.deepEqual(typeOf("text/markdown", "a.txt"), { kind: "md", label: "MD" });
  assert.deepEqual(typeOf("", "notes.MD"), { kind: "md", label: "MD" });
  assert.deepEqual(typeOf("application/json", "x"), { kind: "json", label: "JSON" });
  assert.deepEqual(typeOf("image/png", "shot.png"), { kind: "img", label: "PNG" });
  assert.deepEqual(typeOf("", "photo.jpeg"), { kind: "img", label: "JPEG" });
  assert.deepEqual(typeOf("text/plain", "run.log"), { kind: "text", label: "LOG" });
  assert.deepEqual(typeOf("image/svg+xml", "logo.svg"), { kind: "other", label: "SVG" });
  assert.deepEqual(typeOf("", "backup.tar.gz"), { kind: "other", label: "GZ" });
  assert.deepEqual(typeOf("", "Makefile"), { kind: "other", label: "FILE" });
  assert.equal(extOf("a/b.c/readme"), "");
});

test("matches searches id, name and note, case-insensitively", () => {
  const f = { id: "FILE17", n: 17, ok: true, meta: { name: "Deploy.md", note: "For the Demo" } };
  assert.ok(matches(f, ""));
  assert.ok(matches(f, "file17"));
  assert.ok(matches(f, "17"));
  assert.ok(matches(f, "deploy"));
  assert.ok(matches(f, "demo"));
  assert.ok(!matches(f, "prod"));
  assert.ok(matches({ id: "FILE3", n: 3, ok: false, meta: null }, "file3"));
  assert.ok(!matches({ id: "FILE3", n: 3, ok: false, meta: null }, "secret"));
});

test("a bare number matches the file number only, not every name or note containing it", () => {
  const f = { id: "FILE17", n: 17, ok: true, meta: { name: "report 2017.md", note: "item 17" } };
  const g = { id: "FILE40", n: 40, ok: true, meta: { name: "a.md", note: "" } };
  assert.ok(matches(g, "40") && !matches(f, "40") && matches(f, "17") && !matches(f, "20") && matches(f, "report"));
});

test("mapLimit keeps order and never exceeds the limit", async () => {
  let running = 0;
  let peak = 0;
  const out = await mapLimit([5, 1, 4, 2, 3], 2, async (x) => {
    running += 1;
    peak = Math.max(peak, running);
    await new Promise((r) => setTimeout(r, x));
    running -= 1;
    return x * 10;
  });
  assert.deepEqual(out, [50, 10, 40, 20, 30]);
  assert.equal(peak, 2);
  assert.deepEqual(await mapLimit([], 4, async (x) => x), []);
});

test("safeDownloadName strips paths and control characters", () => {
  assert.equal(safeDownloadName("../../.ssh/authorized_keys", "FILE7"), "authorized_keys");
  assert.equal(safeDownloadName("a\\b\\c.txt", "FILE7"), "c.txt");
  assert.equal(safeDownloadName("..", "FILE7"), "FILE7.bin");
  assert.equal(safeDownloadName("bad\u0000name\u001f.md", "FILE7"), "badname.md");
  assert.equal(safeDownloadName("", "FILE7"), "FILE7.bin");
});

test("safeDownloadName strips bidi and format controls", () => {
  // "invoice‮fdp.exe" would display as "invoiceexe.pdf".
  assert.equal(safeDownloadName("invoice‮fdp.exe", "FILE7"), "invoicefdp.exe");
  for (const cp of [0x200e, 0x200f, 0x202a, 0x202b, 0x202c, 0x202d, 0x202e, 0x2066, 0x2067, 0x2068, 0x2069]) {
    assert.equal(safeDownloadName(`a${String.fromCodePoint(cp)}b.txt`, "FILE7"), "ab.txt", cp.toString(16));
  }
  assert.equal(safeDownloadName("‮⁦", "FILE7"), "FILE7.bin");
});

test("safeDownloadName keeps the final extension when truncating to 200 characters", () => {
  const long = `${"a".repeat(300)}.tar.gz`;
  const out = safeDownloadName(long, "FILE7");
  assert.equal(out.length, 200);
  assert.ok(out.endsWith(".gz"), out.slice(-10));
  assert.equal(out, `${"a".repeat(197)}.gz`);
  assert.equal(safeDownloadName("b".repeat(250), "FILE7"), "b".repeat(200));
  assert.equal(safeDownloadName("c".repeat(199) + ".md", "FILE7").length, 200);
  assert.ok(safeDownloadName("c".repeat(199) + ".md", "FILE7").endsWith(".md"));
  // an "extension" too long to be one is just truncated
  assert.equal(safeDownloadName(`x.${"y".repeat(300)}`, "FILE7").length, 200);
  // astral characters are never split into lone surrogates
  const emoji = safeDownloadName(`${"\u{1F600}".repeat(250)}.png`, "FILE7");
  assert.ok(emoji.endsWith(".png"));
  assert.equal([...emoji].length, 200);
  assert.ok(!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/.test(emoji));
});

// ---- v2 (spec §14 B, E)
import { deviceKey, deviceLabel, expiryLabel, isExpiringSoon } from "../../fileshare/static/js/format.js";

test("expiryLabel counts down in d / h / min and says never or expired", () => {
  const now = Date.parse("2026-09-24T12:00:00Z");
  const at = (ms) => new Date(now + ms).toISOString().replace(/\.\d{3}Z$/, "Z");
  const S = 1000, M = 60 * S, H = 60 * M, D = 24 * H;
  assert.equal(expiryLabel(null, now), "never");
  assert.equal(expiryLabel(undefined, now), "never");
  assert.equal(expiryLabel(at(7 * D - 2 * S), now), "expires in 7 d");  // a fresh 7d upload
  assert.equal(expiryLabel(at(6 * D + 3 * H), now), "expires in 6 d");
  assert.equal(expiryLabel(at(D - 2 * S), now), "expires in 1 d");      // a fresh 1d upload
  assert.equal(expiryLabel(at(30 * D), now), "expires in 30 d");
  assert.equal(expiryLabel(at(5 * H), now), "expires in 5 h");
  assert.equal(expiryLabel(at(5 * H + 20 * M), now), "expires in 5 h");
  assert.equal(expiryLabel(at(59 * M + 50 * S), now), "expires in 1 h");
  assert.equal(expiryLabel(at(12 * M), now), "expires in 12 min");
  assert.equal(expiryLabel(at(10 * S), now), "expires in 1 min");
  assert.equal(expiryLabel(at(0), now), "expired");
  assert.equal(expiryLabel(at(-H), now), "expired");
  assert.equal(expiryLabel("not a date", now), "");
});

test("isExpiringSoon is true under 24 h (and once expired), never for null", () => {
  const now = Date.parse("2026-09-24T12:00:00Z");
  const at = (ms) => new Date(now + ms).toISOString();
  const H = 3600 * 1000;
  assert.equal(isExpiringSoon(null, now), false);
  assert.equal(isExpiringSoon(at(24 * H), now), false);
  assert.equal(isExpiringSoon(at(24 * H - 1000), now), true);
  assert.equal(isExpiringSoon(at(H), now), true);
  assert.equal(isExpiringSoon(at(-H), now), true);
  assert.equal(isExpiringSoon("garbage", now), false);
});

test("deviceKey keys a device by id and a browser upload (id null) by its name", () => {
  const dev = { device: { id: "dev_abc", name: "laptop" } };
  const web = { device: { id: null, name: "iPhone · Safari" } };
  const web2 = { device: { id: null, name: "Mac · Chrome" } };
  assert.equal(deviceKey(dev), "dev_abc");
  assert.equal(deviceKey(web), "web:iPhone · Safari");
  assert.notEqual(deviceKey(web), deviceKey(web2));
  // A device named like a browser still has its own key (device ids are "dev_…").
  assert.notEqual(deviceKey({ device: { id: "dev_x", name: "iPhone · Safari" } }), deviceKey(web));
  assert.equal(deviceKey({ device: null }), "web:browser");
  assert.equal(deviceKey({}), "web:browser");
  assert.equal(deviceLabel(dev), "laptop");
  assert.equal(deviceLabel(web), "iPhone · Safari");
  assert.equal(deviceLabel({ device: { id: null, name: "" } }), "browser");
  assert.equal(deviceLabel({ device: null }), "browser");
});

// ---- redesign (spec §18): short row labels, the detail pill, Today / Earlier, the new dot
import { expiryPhrase, expiryShort, isNew, isToday, shortAge, fileGroup } from "../../fileshare/static/js/format.js";

test("expiryShort is the compact row label", () => {
  const now = Date.parse("2026-09-24T12:00:00Z");
  const at = (ms) => new Date(now + ms).toISOString();
  const S = 1000, M = 60 * S, H = 60 * M, D = 24 * H;
  assert.equal(expiryShort(null, now), "never");
  assert.equal(expiryShort(at(7 * D - 2 * S), now), "7d");
  assert.equal(expiryShort(at(18 * H), now), "18h");
  assert.equal(expiryShort(at(12 * M), now), "12m");
  assert.equal(expiryShort(at(10 * S), now), "1m");
  assert.equal(expiryShort(at(-H), now), "expired");
  assert.equal(expiryShort("garbage", now), "");
});

test("expiryPhrase is the detail pill's sentence", () => {
  const now = Date.parse("2026-09-24T12:00:00Z");
  const at = (ms) => new Date(now + ms).toISOString();
  const M = 60 * 1000, H = 60 * M, D = 24 * H;
  assert.equal(expiryPhrase(null, now), "Never expires");
  assert.equal(expiryPhrase(at(6 * D + H), now), "Expires in 6 days");
  assert.equal(expiryPhrase(at(D - 1000), now), "Expires in 1 day");
  assert.equal(expiryPhrase(at(18 * H), now), "Expires in 18 h");
  assert.equal(expiryPhrase(at(12 * M), now), "Expires in 12 min");
  assert.equal(expiryPhrase(at(-M), now), "Expired");
  assert.equal(expiryPhrase("garbage", now), "");
});

test("shortAge reads like the row mockup: now, min, h, weekday, date", () => {
  const now = new Date(2026, 8, 24, 12, 0, 0).getTime(); // local time, like the page
  const ago = (ms) => new Date(now - ms).toISOString();
  const M = 60 * 1000, H = 60 * M, D = 24 * H;
  assert.equal(shortAge(ago(10 * 1000), now), "now");
  assert.equal(shortAge(ago(2 * M), now), "2 min");
  assert.equal(shortAge(ago(3 * H), now), "3 h");
  const tue = new Date(now - 2 * D);
  assert.equal(shortAge(tue.toISOString(), now), tue.toLocaleDateString("en-GB", { weekday: "short" }));
  assert.equal(shortAge(ago(20 * D), now), new Date(now - 20 * D).toLocaleDateString("en-GB", { day: "numeric", month: "short" }));
  assert.match(shortAge(ago(400 * D), now), /\d{4}$/);
  assert.equal(shortAge("garbage", now), "");
});

test("isToday and fileGroup split the list at local midnight", () => {
  const now = new Date(2026, 8, 24, 0, 30, 0).getTime();
  const earlier = new Date(2026, 8, 23, 23, 50, 0).toISOString();
  const today = new Date(2026, 8, 24, 0, 10, 0).toISOString();
  assert.equal(isToday(today, now), true);
  assert.equal(isToday(earlier, now), false);
  assert.equal(isToday("garbage", now), false);
  assert.equal(fileGroup({ created_at: today }, now), "Today");
  assert.equal(fileGroup({ created_at: earlier }, now), "Earlier");
});

test("isNew means not done and shared in the last 24 hours", () => {
  const now = Date.parse("2026-09-24T12:00:00Z");
  const ago = (h) => new Date(now - h * 3600 * 1000).toISOString();
  assert.equal(isNew({ created_at: ago(1), acked_at: null }, now), true);
  assert.equal(isNew({ created_at: ago(23.9), acked_at: null }, now), true);
  assert.equal(isNew({ created_at: ago(25), acked_at: null }, now), false);
  assert.equal(isNew({ created_at: ago(1), acked_at: ago(0.5) }, now), false);
  assert.equal(isNew({ created_at: "garbage" }, now), false);
});

import { SORTS, sortFiles } from "../../fileshare/static/js/format.js";
test("sortFiles: newest, oldest, name and size; unreadable files go last by name and size", () => {
  const f = (n, name, plain) => ({ n, ok: name !== null, meta: name === null ? null : { name }, size: plain === null ? undefined : 34 + plain + 16 });
  const files = [f(3, "b.txt", 10), f(5, "A.png", 500), f(1, null, null), f(4, "c.zip", 500), f(2, "a2.md", 1)];
  const ns = (k) => sortFiles(files, k).map((x) => x.n);
  assert.deepEqual(ns("newest"), [5, 4, 3, 2, 1]);
  assert.deepEqual(ns("oldest"), [1, 2, 3, 4, 5]);
  assert.deepEqual(ns("name"), [5, 2, 3, 4, 1]);
  assert.deepEqual(ns("size"), [5, 4, 3, 2, 1]);
  assert.deepEqual(ns("bogus"), [5, 4, 3, 2, 1]);
  assert.deepEqual(files.map((x) => x.n), [3, 5, 1, 4, 2]);   // the input is not reordered
  assert.deepEqual(SORTS.map(([k]) => k), ["newest", "oldest", "name", "size"]);
});
