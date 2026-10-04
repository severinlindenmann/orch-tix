// Hidden characters (textsafe.js), the set orch-core's orch/textsafe.py uses.
import { test } from "node:test";
import assert from "node:assert/strict";
import { anyHidden, badge, hasHidden, isHidden, splitHidden } from "../../fileshare/static/js/textsafe.js";

test("controls, format, separators, private use and the fillers are hidden; tab and newline are not", () => {
  for (const ch of ["‮", "​", "­", "⁦", "\u001B", "\r", "\u007F", " ", " ", "",
    "͏", "ᅟ", "ᅠ", "ㅤ", "ﾠ", "\u{E0041}"]) assert.ok(isHidden(ch), badge(ch));
  for (const ch of ["\t", "\n", "a", "ä", " ", "€", "→", "😀"]) assert.ok(!isHidden(ch), JSON.stringify(ch));
});

test("hasHidden and the visible runs", () => {
  assert.equal(hasHidden("plain text\nwith\ttab"), false);
  assert.equal(hasHidden("approve‮evil"), true);
  assert.deepEqual(splitHidden("a‮b​c"), [{ text: "a" }, { hidden: "<U+202E>" }, { text: "b" }, { hidden: "<U+200B>" },
    { text: "c" }]);
  assert.deepEqual(splitHidden("x\u001B"), [{ text: "x" }, { hidden: "<U+001B>" }]);
  assert.deepEqual(splitHidden(""), []);
  assert.equal(badge("\u{E0041}"), "<U+E0041>");
});

test("anyHidden walks strings, arrays and objects", () => {
  assert.equal(anyHidden("a", ["b", { label: "c" }]), false);
  assert.equal(anyHidden("a", [{ label: "c⁦" }]), true);
});
