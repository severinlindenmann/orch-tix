// tests/js/tags.test.mjs — the web's tag rules (spec §19) mirror fileshare/tags.py, and the tag
// filter's URL state (?tag=a&tag=b) round-trips. GOOD and BAD are the Python tests' own sets
// (tests/test_tags_api.py GOOD_TAGS / BAD_TAGS); tests/test_tags_js_parity.py also runs the Python
// sets themselves through this module under node.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  BadTag, MAX_TAGS, liveTag, normalizeTag, normalizeTags, tagsFromSearch, tryTag, withTags,
} from "../../fileshare/static/js/tags.js";

const GOOD = [
  ["notes", "notes"],
  ["  Notes  ", "notes"],
  ["Weekly Report", "weekly-report"],
  ["weekly_report", "weekly-report"],
  ["A_b c", "a-b-c"],
  ["x", "x"],
  ["0day", "0day"],
  ["a".repeat(40), "a".repeat(40)],
  ["q3-2026", "q3-2026"],
  ["A_ B", "a--b"],
  ["debug_log", "debug-log"],
  ["a\x85", "a"],
  ["\x1ca", "a"],
];
const BAD = [
  "", " ", "   ", "-lead", "_lead", " _x", "a".repeat(41), "ümlaut", "a.b", "a/b", "a\nb", "a\tb",
  "emoji🙂", "İstanbul", "\ufeffa", null, 7, ["x"], { x: 1 }, true,
];

for (const [raw, want] of GOOD) {
  test(`normalises ${JSON.stringify(raw)} to ${want}`, () => {
    assert.equal(normalizeTag(raw), want);
    assert.equal(tryTag(raw), want);
  });
}

for (const raw of BAD) {
  test(`rejects ${JSON.stringify(raw)}`, () => {
    assert.throws(() => normalizeTag(raw), BadTag);
    assert.equal(tryTag(raw), null);
  });
}

test("a list is normalised, deduplicated and sorted", () => {
  assert.deepEqual(normalizeTags(["b", "A", "a", " b ", "Weekly Report", "weekly_report"]), ["a", "b", "weekly-report"]);
  assert.deepEqual(normalizeTags([]), []);
});

test("at most ten tags, counted after dedupe", () => {
  assert.equal(MAX_TAGS, 10);
  const ten = Array.from({ length: 10 }, (_, i) => `t${i}`);
  assert.deepEqual(normalizeTags([...ten, "T0", "t1 "]), [...ten].sort());
  assert.throws(() => normalizeTags([...ten, "t10"]), BadTag);
});

test("tags must be a list", () => {
  for (const raw of ["notes", null, 3, { a: 1 }]) assert.throws(() => normalizeTags(raw), BadTag);
});

test("live input keeps its trailing space until it is committed, and lowercases the rest", () => {
  assert.equal(liveTag("Weekly Report"), "weekly-report");
  assert.equal(liveTag("  Weekly_"), "weekly-");
  assert.equal(liveTag("notes "), "notes ");
  assert.equal(normalizeTag(liveTag("notes ")), "notes");
  assert.equal(liveTag(""), "");
});

test("the URL state round-trips: withTags writes ?tag=, tagsFromSearch reads it back", () => {
  const url = withTags("/", ["weekly-report", "notes"]);
  assert.equal(url, "/?tag=notes&tag=weekly-report");
  assert.deepEqual(tagsFromSearch(new URL(url, "https://x.test").search), ["notes", "weekly-report"]);
  // Other parameters (the open file) are kept, and the tags replace any old ones.
  const withFile = withTags("/?f=FILE7&tag=old", ["b", "a"]);
  assert.equal(withFile, "/?f=FILE7&tag=a&tag=b");
  assert.deepEqual(tagsFromSearch(new URL(withFile, "https://x.test").search), ["a", "b"]);
  assert.equal(withTags("/?tag=a", []), "/");
  assert.equal(withTags("/?f=FILE1&tag=a", []), "/?f=FILE1");
});

test("tags from the address are normalised, deduplicated, and invalid ones dropped", () => {
  assert.deepEqual(tagsFromSearch("?tag=Weekly%20Report&tag=weekly_report&tag=%3Cimg%3E&tag=&f=FILE1"), ["weekly-report"]);
  assert.deepEqual(tagsFromSearch(""), []);
  const many = Array.from({ length: 12 }, (_, i) => `tag=t${String(i).padStart(2, "0")}`).join("&");
  assert.equal(tagsFromSearch(`?${many}`).length, MAX_TAGS);
});

test("whitespace is stripped like Python's str.strip(): its isspace() set, not JS's trim()", () => {
  for (const ws of ["\t", "\n", "\v", "\f", "\r", "\x1c", "\x1d", "\x1e", "\x1f", " ", "\x85", "\xa0", " ", " ",
    " ", " ", " ", " ", " ", "　"]) {
    assert.equal(normalizeTag(`${ws}b${ws}`), "b", JSON.stringify(ws));
  }
  assert.equal(tryTag("﻿b"), null); // U+FEFF is not whitespace to Python
  assert.equal(tryTag("b﻿"), null);
});
