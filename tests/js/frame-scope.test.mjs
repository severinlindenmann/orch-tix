// tests/js/frame-scope.test.mjs — what the app accepts from the dashboard frame (js/frame-scope.js, js/frame-render.js,
// js/bridge-transport.js). Pure modules; the frame itself is exercised in a browser (tests/browser/test_dash_frame.py).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { checkRequest, compileScopes, validPath, PATH_MAX, BODY_MAX } from "../../fileshare/static/js/frame-scope.js";
import { classify, isNeverPage, NEVER_PAGE } from "../../fileshare/static/js/frame-render.js";
import { fakeTransport, assertTransport } from "../../fileshare/static/js/bridge-transport.js";

const scopes = compileScopes({ rules: [
  { methods: ["GET"], pattern: "/" }, { methods: ["GET"], pattern: "/t/:ref" }, { methods: ["GET"], pattern: "/static/*" },
  { methods: ["POST"], pattern: "/t/:ref/answer" }, { methods: ["GET"], pattern: "/events", stream: true },
] });
const req = (over) => ({ t: "req", id: 1, gen: 0, intent: "fetch", method: "GET", path: "/", headers: {}, ...over });

test("validPath: one slash, printable ASCII, no dot segments, no encoded separators, no fragment", () => {
  for (const ok of ["/", "/t/L-1", "/t/L-1?x=1&y=%20", "/static/app.js?v=abc", "/a/b%20c"]) assert.equal(validPath(ok), ok, ok);
  for (const bad of ["", "t/x", "//evil.test/x", "/\\evil", "/a/../b", "/a/./b", "/a/%2e%2e/b", "/a/%2E/b", "/a%2fb", "/a%5cb", "/a%00",
    "/a b", "/a\tb", "/a\r\nb", "/a#frag", "/é", "/a\u0000", "https://x.test/", "javascript:alert(1)", "/%zz", "/" + "a".repeat(PATH_MAX), 7, null, undefined, {}, ["/"]]) {
    assert.equal(validPath(bad), null, JSON.stringify(bad));
  }
  assert.equal(validPath("/?a=../b"), "/?a=../b", "dots in a query are data, not a segment");
});

test("the scope table: method and path must both match; a stream needs a stream rule", () => {
  assert.equal(scopes.allows("GET", "/"), true);
  assert.equal(scopes.allows("GET", "/t/L-1?x=1"), true);
  assert.equal(scopes.allows("GET", "/t/L-1/extra"), false, ":ref is one segment");
  assert.equal(scopes.allows("GET", "/t/"), false);
  assert.equal(scopes.allows("POST", "/t/L-1"), false, "right path, wrong method");
  assert.equal(scopes.allows("POST", "/t/L-1/answer"), true);
  assert.equal(scopes.allows("DELETE", "/t/L-1/answer"), false);
  assert.equal(scopes.allows("GET", "/static/x/y.js"), true);
  assert.equal(scopes.allows("GET", "/static"), false, "a trailing * needs at least one segment");
  assert.equal(scopes.allows("GET", "/events", { stream: true }), true);
  assert.equal(scopes.allows("GET", "/t/L-1", { stream: true }), false);
  assert.equal(scopes.allows("TRACE", "/"), false);
  assert.equal(scopes.allows("GET", "/secret"), false);
  assert.equal(compileScopes(null).allows("GET", "/"), false, "no table, no access");
});

test("checkRequest: a crafted path or method is refused before anything else", () => {
  const code = (m) => { const r = checkRequest(m, scopes); return r.ok ? "ok" : r.code; };
  assert.equal(code(req({})), "ok");
  assert.equal(code(req({ path: "/secret" })), "scope");
  assert.equal(code(req({ path: "/a/../t/L-1" })), "path");
  assert.equal(code(req({ path: "//evil.test/" })), "path");
  assert.equal(code(req({ path: "https://x.test/" })), "path");
  assert.equal(code(req({ path: "/t/L-1\r\nCookie: a=b" })), "path");
  assert.equal(code(req({ method: "TRACE" })), "method");
  assert.equal(code(req({ method: "get" })), "method", "methods are upper case");
  assert.equal(code(req({ method: 7 })), "shape");
  assert.equal(code(req({ method: "DELETE", path: "/t/L-1/answer" })), "scope");
  assert.equal(code(req({ intent: "asset", method: "POST", path: "/t/L-1/answer" })), "method", "an asset is a GET");
  assert.equal(code(req({ intent: "open", method: "POST", path: "/t/L-1/answer" })), "method");
  assert.equal(code(req({ intent: "other" })), "shape");
  assert.equal(code(req({ id: 0 })), "shape");
  assert.equal(code(req({ id: "1" })), "shape");
  assert.equal(code(req({ gen: -1 })), "shape");
  assert.equal(code(null), "shape");
  assert.equal(code("req"), "shape");
  assert.equal(code({ t: "sopen", id: 2, gen: 0, path: "/events" }), "ok");
  assert.equal(code({ t: "sopen", id: 2, gen: 0, path: "/t/L-1" }), "scope");
});

test("checkRequest: bodies are bytes under the cap, and only on methods that carry one", () => {
  const post = { method: "POST", path: "/t/L-1/answer" };
  assert.equal(checkRequest(req({ ...post, body: new ArrayBuffer(10) }), scopes).ok, true);
  assert.equal(checkRequest(req({ ...post, body: new ArrayBuffer(BODY_MAX + 1) }), scopes).code, "size");
  assert.equal(checkRequest(req({ ...post, body: "text" }), scopes).code, "shape");
  assert.equal(checkRequest(req({ body: new ArrayBuffer(1) }), scopes).code, "shape", "a GET with a body");
  assert.equal(checkRequest(req({ ...post, body: new ArrayBuffer(100) }), scopes, { bodyMax: 50 }).code, "size");
});

test("checkRequest: only the allow-listed headers survive, and a bad value refuses the request", () => {
  const ok = checkRequest(req({ headers: { Accept: "application/json", Cookie: "a=b", Origin: "x", Host: "y", "X-Evil": "1", "content-type": "text/plain" } }), scopes);
  assert.deepEqual(ok.req.headers, { accept: "application/json", "content-type": "text/plain" });
  assert.equal(checkRequest(req({ headers: { accept: "a\r\nb" } }), scopes).code, "shape");
  assert.equal(checkRequest(req({ headers: { accept: "x".repeat(600) } }), scopes).code, "shape");
  assert.equal(checkRequest(req({ headers: Object.fromEntries(Array.from({ length: 20 }, (_, i) => [`h${i}`, "1"])) }), scopes).code, "shape");
  assert.equal(checkRequest(req({ headers: "x" }), scopes).ok, true, "no headers object: none");
});

test("render rules: only a host-tagged 200 text/html answer outside the policy routes is a page", () => {
  const ok = { path: "/board", status: 200, headers: { "content-type": "text/html; charset=utf-8" }, page: true };
  assert.equal(classify(ok), "page");
  // the dashboard's own CSP is not the test: a page carries one
  assert.equal(classify({ ...ok, headers: { ...ok.headers, "content-security-policy": "default-src 'self'" } }), "page");
  assert.equal(classify({ ...ok, page: false }), "viewer", "not tagged by the host");
  assert.equal(classify({ ...ok, page: undefined }), "viewer");
  assert.equal(classify({ ...ok, page: "yes" }), "viewer", "only true counts");
  assert.equal(classify({ ...ok, status: 404 }), "error");
  assert.equal(classify({ ...ok, status: 302 }), "error");
  assert.equal(classify({ ...ok, headers: { "content-type": "text/plain" } }), "viewer");
  assert.equal(classify({ ...ok, headers: { "content-type": "application/json" } }), "viewer");
  assert.equal(classify({ ...ok, headers: { "content-type": "text/html", "content-disposition": "attachment; filename=a.html" } }), "page", "a page's disposition is the host's business, the tag decides");
  assert.equal(classify({ ...ok, headers: { "content-type": "application/octet-stream" } }), "download");
  assert.equal(classify({ ...ok, headers: { "content-type": "text/csv", "content-disposition": "attachment" }, page: false }), "download");
  assert.equal(classify({ ...ok, headers: {} }), "download");
  assert.equal(classify(ok, { allowPage: false }), "viewer", "a link that opens elsewhere never writes the frame");
});

test("render rules: artifacts, widgets, addon files and raw ticket files are never a page, whatever the host tagged", () => {
  const tagged = (path) => classify({ path, status: 200, headers: { "content-type": "text/html" }, page: true });
  for (const p of ["/a/L-1/report.html", "/a/L-1/x?v=abc", "/w/L-1/Findings/abcdef", "/w/preview/L-1", "/wp/wiki/abcd", "/wpf/wiki/abcd/f.js",
    "/addons/design/files/tok123", "/t/L-1/raw", "/t/L-1/raw?x=1"]) {
    assert.equal(isNeverPage(p), true, p);
    assert.equal(tagged(p), "viewer", p);
  }
  for (const p of ["/", "/board", "/t/L-1", "/addons/design/", "/workspace", "/activity"]) assert.equal(tagged(p), "page", p);
  assert.ok(NEVER_PAGE.length >= 6);
});

test("the fake transport follows the interface: head first, chunks, end; streams close on abort", async () => {
  const fake = fakeTransport((r) => (r.path === "/s" ? { stream: true } : { status: 200, headers: { "Content-Type": "text/html" }, body: "hi", page: true }));
  assertTransport(fake);
  const events = [];
  for await (const e of fake.request({ method: "GET", path: "/p", headers: {}, body: null, stream: false, signal: new AbortController().signal })) events.push(e);
  assert.deepEqual(events.map((e) => e.type), ["head", "chunk", "end"]);
  assert.equal(events[0].headers["content-type"], "text/html");
  assert.equal(events[0].page, true);
  const ac = new AbortController();
  const it = fake.request({ method: "GET", path: "/s", headers: {}, body: null, stream: true, signal: ac.signal })[Symbol.asyncIterator]();
  assert.equal((await it.next()).value.type, "head");
  fake.streams[0].push("data: x\n\n");
  assert.equal((await it.next()).value.type, "chunk");
  ac.abort();
  assert.equal((await it.next()).value.type, "end");
  assert.equal(fake.streams[0].closed, true);
  assert.throws(() => assertTransport({}), TypeError);
});

test("host-supplied strings are drawn as text: no innerHTML, outerHTML, insertAdjacentHTML or document.write in the host or the app side", () => {
  for (const name of ["frame-host.js", "frame-scope.js", "frame-render.js", "bridge-transport.js"]) {
    const src = readFileSync(new URL(`../../fileshare/static/js/${name}`, import.meta.url), "utf8");
    assert.doesNotMatch(src, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|DOMParser|eval\(|new Function/, name);
  }
});

test("the shim: no eval, no closing script tag, document writes only for the page it was handed", () => {
  const src = readFileSync(new URL("../../fileshare/static/js/frame-shim.js", import.meta.url), "utf8");
  assert.doesNotMatch(src, /<\/script|<!--|\beval\(|new Function|innerHTML\s*=|insertAdjacentHTML/);
  assert.equal(src.match(/document\.write\(/g).length, 1);
  assert.doesNotMatch(src, /^\s*(import|export)\s/m, "a classic script: no import or export statements");
});
