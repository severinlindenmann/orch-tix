// tests/js/widget-model.test.mjs — ticket widgets on the phone: the fence split (orch-core's CommonMark rule),
// fences matched to the addon's widgets (tests/vectors/addon-docs.json "full+widgets"), and the frame helpers.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import {
  MAX_INLINE_BYTES, chipText, clampHeight, docSource, docWidgets, frameMessage, readCapped, sectionParts, sha256Hex,
  openFrames, openKey, splitText, textOnly, verifyDoc, withNonce, withTheme,
} from "../../fileshare/static/js/widget-model.js";
import { verificationSummary } from "../../fileshare/static/js/mirror-model.js";

const ADDON = JSON.parse(readFileSync(new URL("../vectors/addon-docs.json", import.meta.url), "utf8"));
const kinds = (parts) => parts.map((p) => (p.kind === "widget" ? `w${p.n}` : "t"));

test("an orch fence splits the text; the text around it stays", () => {
  const parts = splitText('Before.\n\n```orch\n{"type": "text", "text": "x"}\n```\n\nAfter.');
  assert.deepEqual(kinds(parts), ["t", "w0", "t"]);
  assert.equal(parts[0].text, "Before.\n");
  assert.equal(parts[1].raw, '{"type": "text", "text": "x"}');
  assert.equal(parts[2].text, "\nAfter.");
});

test("fences follow CommonMark: same character, at least as long, nothing after", () => {
  assert.deepEqual(kinds(splitText("````orch\n{}\n```\nstill inside\n````\nout")), ["w0", "t"]);
  assert.equal(splitText("````orch\n{}\n```\nstill inside\n````")[0].raw, "{}\n```\nstill inside");
  assert.deepEqual(kinds(splitText("~~~orch\n{}\n~~~")), ["w0"]);
  assert.deepEqual(kinds(splitText("```orch\n{}\n``` x\n```")), ["w0"]);           // "``` x" does not close
  assert.deepEqual(kinds(splitText("    ```orch\n{}\n```")), ["t"]);                 // 4 spaces: code, no fence
  assert.deepEqual(kinds(splitText("```orchx\n{}\n```")), ["t"]);                    // another info string
});

test("an orch fence inside another fence is code, not a widget", () => {
  assert.deepEqual(kinds(splitText("````md\n```orch\n{}\n```\n````\n```orch\n{}\n```")), ["t", "w0"]);
});

test("an unclosed orch fence is still a block (orch-core counts it), to the end", () => {
  const parts = splitText("a\n```orch\n{\n");
  assert.deepEqual(kinds(parts), ["t", "w0"]);
  assert.equal(parts[1].raw, "{\n");
});

test("a fence is matched to its widget by section and the digest of its own text", async () => {
  const doc = ADDON.levels["full+widgets"];
  const parts = await sectionParts(doc, "Findings", doc.sections.Findings);
  assert.deepEqual(kinds(parts), ["t", "w0", "t", "w1", "t"]);
  assert.equal(parts[1].widget.layer, "type");
  assert.equal(parts[1].widget.name, "stats");
  assert.match(parts[1].widget.text, /files: 14/);
  assert.equal(parts[3].widget.name, "decision-matrix@1");
  assert.deepEqual(docSource(parts[1].widget), { inline: parts[1].widget.doc, sha256: parts[1].widget.sha256 });
  assert.deepEqual(docSource(parts[3].widget), { file: "FILE8", sha256: parts[3].widget.sha256 });
});

test("reordered fences still find their own widgets; position only breaks ties between identical fences", async () => {
  const doc = ADDON.levels["full+widgets"];
  const [a, b] = doc.sections.Findings.match(/```orch\n[\s\S]*?\n```/g);
  const swapped = await sectionParts(doc, "Findings", `${b}\n\n${a}`);
  assert.deepEqual(swapped.filter((p) => p.widget).map((p) => p.widget.name), ["decision-matrix@1", "stats"]);
  const other = await sectionParts(doc, "Other", `${a}`);
  assert.equal(other[0].widget, null, "another section's text never takes this section's widget");
  const w = doc.widgets[0];
  const twin = { ...doc, widgets: [{ ...w, index: 0, key: "0" }, { ...w, index: 1, key: "1" }] };
  const two = await sectionParts(twin, "Findings", `${a}\n\n${a}`);
  assert.deepEqual(two.filter((p) => p.widget).map((p) => p.widget.key), ["0", "1"]);
  const three = await sectionParts(twin, "Findings", `${a}\n\n${a}\n\n${a}`);
  assert.deepEqual(three.filter((p) => p.kind === "widget").map((p) => p.widget?.key ?? null), ["0", "1", null], "an entry serves one fence");
});

test("a fence whose text changed after its digest was made shows as text", async () => {
  const doc = ADDON.levels["full+widgets"];
  const edited = doc.sections.Findings.replace('"files"', '"fil3s"');
  const parts = await sectionParts(doc, "Findings", edited);
  assert.equal(parts[1].widget, null);
  assert.equal(parts[3].widget.name, "decision-matrix@1");
});

test("a doc without widgets (an older addon, title) leaves the fence as text", async () => {
  const full = ADDON.levels.full;
  assert.deepEqual(docWidgets(full), []);
  const parts = await sectionParts(full, "Findings", full.sections.Findings);
  assert.deepEqual(parts.filter((p) => p.kind === "widget").map((p) => p.widget), [null, null]);
  assert.deepEqual(docWidgets({ widgets: [{}] }), [], "no format, no widgets");
});

test("textOnly drops the blocks; the verification summary never reads a fence", () => {
  assert.equal(textOnly("```orch\n{}\n```\nAll green."), "All green.");
  assert.equal(verificationSummary({ sections: { Verification: '```orch\n{"type": "checks"}\n```\n\nAC1 met.' } }), "AC1 met.");
});

test("chips name the layer; agent HTML says so", () => {
  assert.equal(chipText({ layer: "type" }), "core");
  assert.equal(chipText({ layer: "widget", name: "mermaid@1" }), "agent HTML · mermaid@1");
  assert.equal(chipText({ layer: "html" }), "agent HTML · one-off");
  assert.equal(chipText({ layer: "invalid" }), "not shown");
});

const PIN = "a".repeat(64);

test("docSource needs a known layer and a 64-hex pin, then takes the inline document, else a FILE id", () => {
  assert.equal(docSource({ layer: "type", sha256: PIN, file: "../etc" }), null);
  assert.equal(docSource({ layer: "type", sha256: PIN, text: "t" }), null);
  assert.deepEqual(docSource({ layer: "html", sha256: PIN, doc: "<p>", file: "FILE3" }), { inline: "<p>", sha256: PIN });
  assert.deepEqual(docSource({ layer: "widget", sha256: PIN, file: "FILE3" }), { file: "FILE3", sha256: PIN });
  for (const layer of ["invalid", undefined, "", "TYPE", "script"]) assert.equal(docSource({ layer, sha256: PIN, doc: "<p>" }), null, String(layer));
  for (const sha256 of [undefined, "", "A".repeat(64), "a".repeat(63), "g".repeat(64), 5]) assert.equal(docSource({ layer: "type", sha256, doc: "<p>" }), null, String(sha256));
});

test("an inline document over 128 KiB is not drawn; one at the limit is", () => {
  assert.equal(docSource({ layer: "type", sha256: PIN, doc: "x".repeat(MAX_INLINE_BYTES + 1) }), null);
  assert.notEqual(docSource({ layer: "type", sha256: PIN, doc: "x".repeat(MAX_INLINE_BYTES) }), null);
  assert.equal(docSource({ layer: "type", sha256: PIN, doc: "\u00e9".repeat(MAX_INLINE_BYTES / 2 + 1) }), null, "bytes, not characters");
});

test("verifyDoc compares the sha-256 of the exact bytes", async () => {
  const bytes = new TextEncoder().encode("<p>hi</p>");
  const digest = await sha256Hex(bytes);
  assert.equal(await verifyDoc(bytes, digest), true);
  assert.equal(await verifyDoc(new TextEncoder().encode("<p>hi</p> "), digest), false);
  assert.equal(await verifyDoc(bytes, undefined), false);
  assert.equal(await verifyDoc(bytes, digest.toUpperCase()), false);
});

test("readCapped refuses by Content-Length, and while streaming when the header lies", async () => {
  const stream = (chunks) => new ReadableStream({ start(c) { chunks.forEach((x) => c.enqueue(x)); c.close(); } });
  const res = (chunks, headers = {}) => new Response(stream(chunks), { headers });
  assert.deepEqual([...await readCapped(res([new Uint8Array([1, 2]), new Uint8Array([3])]), 3)], [1, 2, 3]);
  await assert.rejects(readCapped({ headers: new Headers({ "content-length": "10" }), body: stream([new Uint8Array(10)]) }, 5), /too large/);
  let pulled = 0;
  const lying = { headers: new Headers({ "content-length": "1" }),
    body: new ReadableStream({ pull(c) { pulled += 1; c.enqueue(new Uint8Array(4)); if (pulled > 100) c.close(); } }) };
  await assert.rejects(readCapped(lying, 10), /too large/);
  assert.ok(pulled < 10, "the read stopped at the cap");
});

test("withNonce replaces the document's own nonce with this page's", () => {
  const html = '<head><meta name="orch-frame" content="abcDEF12_-"></head><p>abcDEF12_-</p>';
  assert.equal(withNonce(html, "fresh0000000"), '<head><meta name="orch-frame" content="fresh0000000"></head><p>abcDEF12_-</p>');
  assert.equal(withNonce("<p>no kit</p>", "x"), "<p>no kit</p>");
});

test("withTheme sets the page's theme on the first <html> tag", () => {
  assert.equal(withTheme('<!doctype html>\n<html lang="en" data-theme="system"><body>', true),
    '<!doctype html>\n<html lang="en" data-theme="dark"><body>');
  assert.equal(withTheme('<html lang="en">x<html data-theme="y">', false), '<html data-theme="light" lang="en">x<html data-theme="y">');
  const core = ADDON.levels["full+widgets"].widgets[0].doc;
  assert.match(withTheme(core, true), /<html[^>]*data-theme="dark"/);
});

test("frame messages need orch 1, the nonce and a known kind", () => {
  assert.deepEqual(frameMessage({ orch: 1, frame: "n0nce123", kind: "ready" }, "n0nce123"), { orch: 1, frame: "n0nce123", kind: "ready" });
  assert.equal(frameMessage({ orch: 1, frame: "other", kind: "ready" }, "n0nce123"), null);
  assert.equal(frameMessage({ orch: 2, frame: "n0nce123", kind: "ready" }, "n0nce123"), null);
  assert.equal(frameMessage({ orch: 1, frame: "n0nce123", kind: "navigate" }, "n0nce123"), null);
  assert.equal(frameMessage("ready", "n0nce123"), null);
});

test("heights are clamped", () => {
  assert.equal(clampHeight(10), 60);
  assert.equal(clampHeight(99999), 2400);
  assert.equal(clampHeight(212.2), 213);
  assert.equal(clampHeight("x"), 160);
});

// The frame's script is the one inline classic script of sandbox-widget.html: loaded with vm, without a window.
const FRAME_HTML = readFileSync(new URL("../../fileshare/static/sandbox-widget.html", import.meta.url), "utf8");
const FRAME = /<script>([\s\S]*?)<\/script>/.exec(FRAME_HTML)[1];
const PARENT = { name: "parent" };

function acceptor() {
  const context = {};
  vm.createContext(context);
  vm.runInContext(FRAME, context);
  return context.__tixWidgetFrame.makeAcceptor();
}

test("the frame takes one tix-widget message, from its parent, with a valid nonce", () => {
  const ok = { source: PARENT, data: { type: "tix-widget", html: "<p>x</p>", nonce: "abcd1234" } };
  const accept = acceptor();
  assert.deepEqual({ ...accept(ok, PARENT) }, { html: "<p>x</p>", nonce: "abcd1234" });
  assert.equal(accept(ok, PARENT), null, "only once");
  assert.equal(acceptor()({ ...ok, source: { name: "other" } }, PARENT), null);
  assert.equal(acceptor()({ source: PARENT, data: { type: "tix-render", html: "x", nonce: "abcd1234" } }, PARENT), null);
  assert.equal(acceptor()({ source: PARENT, data: { type: "tix-widget", html: "x", nonce: "short" } }, PARENT), null);
  assert.equal(acceptor()({ source: PARENT, data: { type: "tix-widget", html: 1, nonce: "abcd1234" } }, PARENT), null);
});

test("in a frame nothing of the frame script lands on window", () => {
  const listeners = [];
  const context = { addEventListener: (type, fn) => listeners.push(type) };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(FRAME, context);
  assert.equal("__tixWidgetFrame" in context, false);
  assert.deepEqual(listeners, ["message"]);
});

test("an open frame is kept by section, fence digest and document pin; any change is another key", () => {
  const w = { section: "Findings", raw_sha256: "a".repeat(64), sha256: "b".repeat(64) };
  const key = openKey("T-1", w);
  openFrames.add(key);
  assert.ok(openFrames.has(openKey("T-1", { ...w })), "same widget after a re-render");
  assert.ok(!openFrames.has(openKey("T-1", { ...w, sha256: "c".repeat(64) })), "changed document pin");
  assert.ok(!openFrames.has(openKey("T-1", { ...w, raw_sha256: "d".repeat(64) })), "changed fence");
  assert.ok(!openFrames.has(openKey("T-1", { ...w, section: "Other" })), "another section");
  assert.ok(!openFrames.has(openKey("T-2", w)), "another ticket");
  openFrames.delete(key);
});
