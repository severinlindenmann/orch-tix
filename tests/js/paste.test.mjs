// tests/js/paste.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  stamp, guessTextExt, fileFromText, filesFromClipboard, filesFromClipboardItems, isEditable,
} from "../../fileshare/static/js/paste.js";

const NOW = new Date(2026, 8, 24, 12, 3, 4); // local time, as the user sees it

const fileItem = (file) => ({ kind: "file", type: file.type, getAsFile: () => file });
const stringItem = (type) => ({ kind: "string", type, getAsFile: () => null });
const dt = ({ items = [], files = [], text = "" } = {}) => ({
  items, files, getData: (t) => (t === "text/plain" ? text : ""),
});

test("stamp is YYYYMMDD-HHMMSS in local time", () => {
  assert.equal(stamp(NOW), "20260924-120304");
});

test("guessTextExt: objects/arrays are json, # or fenced code is md, the rest txt", () => {
  assert.equal(guessTextExt('{"a": 1}'), "json");
  assert.equal(guessTextExt("  [1, 2]\n"), "json");
  assert.equal(guessTextExt("42"), "txt");
  assert.equal(guessTextExt('"just a string"'), "txt");
  assert.equal(guessTextExt("{broken"), "txt");
  assert.equal(guessTextExt("# Title\n\nbody"), "md");
  assert.equal(guessTextExt("see this:\n```js\nx()\n```"), "md");
  assert.equal(guessTextExt("hello world"), "txt");
});

test("fileFromText names and types pasted text; blank text gives no file", () => {
  assert.equal(fileFromText("   \n", NOW), null);
  assert.equal(fileFromText("", NOW), null);
  const f = fileFromText('{"a":1}', NOW);
  assert.equal(f.name, "pasted-20260924-120304.json");
  assert.equal(f.type, "application/json");
  assert.equal(fileFromText("# x", NOW).type, "text/markdown");
  assert.equal(fileFromText("plain", NOW).name, "pasted-20260924-120304.txt");
});

test("filesFromClipboard: screenshots get timestamped names, real files keep theirs, text is the fallback", async () => {
  const shot = new File([new Uint8Array([1, 2, 3])], "image.png", { type: "image/png" });
  const shot2 = new File([new Uint8Array([4])], "", { type: "image/jpeg" });
  const pdf = new File([new Uint8Array([9])], "report.pdf", { type: "application/pdf" });

  const out = filesFromClipboard(dt({ items: [fileItem(shot), fileItem(shot2), fileItem(pdf), stringItem("text/plain")], text: "ignored" }), NOW);
  assert.deepEqual(out.map((f) => f.name), ["screenshot-20260924-120304.png", "screenshot-20260924-120304-2.jpg", "report.pdf"]);
  assert.deepEqual([...new Uint8Array(await out[0].arrayBuffer())], [1, 2, 3]);
  assert.equal(out[0].type, "image/png");

  const named = new File([new Uint8Array([7])], "holiday.png", { type: "image/png" });
  assert.deepEqual(filesFromClipboard(dt({ items: [fileItem(named)] }), NOW).map((f) => f.name), ["holiday.png"]);
  assert.deepEqual(filesFromClipboard(dt({ files: [pdf] }), NOW).map((f) => f.name), ["report.pdf"]);

  const text = filesFromClipboard(dt({ items: [stringItem("text/plain")], text: "[1,2]" }), NOW);
  assert.deepEqual(text.map((f) => f.name), ["pasted-20260924-120304.json"]);
  assert.deepEqual(filesFromClipboard(dt({ text: "  " }), NOW), []);
  assert.deepEqual(filesFromClipboard(null, NOW), []);
});

test("filesFromClipboardItems handles navigator.clipboard.read() results", async () => {
  const item = (types, data) => ({ types, getType: async (t) => new Blob([data[t]], { type: t }) });
  const imgs = await filesFromClipboardItems([item(["image/png"], { "image/png": new Uint8Array([5, 6]) })], NOW);
  assert.deepEqual(imgs.map((f) => [f.name, f.type, f.size]), [["screenshot-20260924-120304.png", "image/png", 2]]);
  const txt = await filesFromClipboardItems([item(["text/html", "text/plain"], { "text/plain": "# notes" })], NOW);
  assert.deepEqual(txt.map((f) => f.name), ["pasted-20260924-120304.md"]);
  assert.deepEqual(await filesFromClipboardItems([], NOW), []);
});

test("isEditable: inputs, textareas, selects and contenteditable swallow the paste", () => {
  assert.equal(isEditable({ tagName: "TEXTAREA" }), true);
  assert.equal(isEditable({ tagName: "INPUT" }), true);
  assert.equal(isEditable({ tagName: "SELECT" }), true);
  assert.equal(isEditable({ tagName: "DIV", isContentEditable: true }), true);
  assert.equal(isEditable({ tagName: "BODY", isContentEditable: false }), false);
  assert.equal(isEditable(null), false);
});
