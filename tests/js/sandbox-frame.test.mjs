// tests/js/sandbox-frame.test.mjs — the /sandbox/html frame protocol (spec T15): the parent posts
// {type: "tix-render", html} after load; the frame renders only the first valid message.
//
// sandbox-frame.js is a classic script (no import/export — see its own header comment for why), so
// it's loaded here with `vm` rather than `import`, into a context with no `window` global (so the
// page-wiring block doesn't run). Without a `window`, the script exposes `__tixSandbox`
// {makeAcceptor, renderInto}; each test calls makeAcceptor fresh, so cases are independent.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SRC = readFileSync(new URL("../../fileshare/static/js/sandbox-frame.js", import.meta.url), "utf8");

function freshAcceptor() {
  const context = {};
  vm.createContext(context);
  vm.runInContext(SRC, context);
  assert.equal(typeof context.__tixSandbox?.makeAcceptor, "function", "sandbox-frame.js must expose __tixSandbox.makeAcceptor");
  return context.__tixSandbox.makeAcceptor();
}

test("in a frame (a window exists), nothing lands on window: no makeAcceptor, renderInto or __tixSandbox", () => {
  const listeners = [];
  const context = { addEventListener: (type, fn) => listeners.push([type, fn]) };
  context.window = context;       // like a browser: window is the global object
  vm.createContext(context);
  vm.runInContext(SRC, context);
  for (const name of ["makeAcceptor", "renderInto", "acceptRender", "__tixSandbox", "tixSandboxFrame"]) {
    assert.equal(name in context, false, `${name} leaked onto window`);
  }
  assert.deepEqual(listeners.map(([type]) => type), ["message"]);
});

const PARENT = { name: "parent" };
const OTHER = { name: "other" };

test("accepts a well-formed message from window.parent", () => {
  const acceptRender = freshAcceptor();
  assert.equal(acceptRender({ source: PARENT, data: { type: "tix-render", html: "<p>hi</p>" } }, PARENT), "<p>hi</p>");
});

test("rejects a message whose source isn't window.parent", () => {
  const acceptRender = freshAcceptor();
  assert.equal(acceptRender({ source: OTHER, data: { type: "tix-render", html: "<p>hi</p>" } }, PARENT), null);
});

test("rejects a message with the wrong type", () => {
  const acceptRender = freshAcceptor();
  assert.equal(acceptRender({ source: PARENT, data: { type: "other", html: "<p>hi</p>" } }, PARENT), null);
});

test("rejects malformed data: no data, missing html, non-string html", () => {
  const acceptRender = freshAcceptor();
  assert.equal(acceptRender({ source: PARENT, data: null }, PARENT), null);
  assert.equal(acceptRender({ source: PARENT, data: { type: "tix-render" } }, PARENT), null);
  assert.equal(acceptRender({ source: PARENT, data: { type: "tix-render", html: 42 } }, PARENT), null);
});

test("rejects a second message once one was already accepted, even a well-formed one", () => {
  const acceptRender = freshAcceptor();
  const html = "<p>first</p>";
  assert.equal(acceptRender({ source: PARENT, data: { type: "tix-render", html } }, PARENT), html);
  assert.equal(acceptRender({ source: PARENT, data: { type: "tix-render", html: "<p>second</p>" } }, PARENT), null);
  assert.equal(acceptRender({ source: OTHER, data: { type: "tix-render", html: "<p>third</p>" } }, PARENT), null);
});

test("each acceptor from makeAcceptor has its own independent state", () => {
  const a = freshAcceptor();
  const b = freshAcceptor();
  assert.equal(a({ source: PARENT, data: { type: "tix-render", html: "<p>a</p>" } }, PARENT), "<p>a</p>");
  // b hasn't rendered yet, even though a already has.
  assert.equal(b({ source: PARENT, data: { type: "tix-render", html: "<p>b</p>" } }, PARENT), "<p>b</p>");
});
