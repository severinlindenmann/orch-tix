import { test } from "node:test";
import assert from "node:assert/strict";
import { UnsafeKdfError, attentionBadge, checkKdf, loginHref, needsCount, refreshAttention, safeNext } from "../../fileshare/static/js/nav.js";
import { b64u } from "../../fileshare/static/js/crypto.js";

const ORIGIN = "https://share.example";

const CASES = [
  ["/\t/evil.com", "/"],
  ["/\n/evil.com", "/"],
  ["/\r/evil.com", "/"],
  ["//evil.com", "/"],
  ["/\\evil.com", "/"],
  ["\\\\evil.com", "/"],
  ["/a/..//evil.com", "/"],
  ["https://evil.com", "/"],
  ["javascript:alert(1)", "/"],
  [" /x", "/x"],
  ["/files?x=1#y", "/files?x=1"],
  ["/pair#" + "a".repeat(32) + ".ph_0123456789ab." + "A".repeat(43), "/"],
  ["/devices", "/devices"],
  ["", "/"],
  [null, "/"],
  [undefined, "/"],
];

for (const [raw, want] of CASES) {
  test(`safeNext(${JSON.stringify(raw)}) -> ${want}`, () => {
    assert.equal(safeNext(raw, ORIGIN), want);
  });
}

test("safeNext result never leaves the origin when navigated to", () => {
  for (const [raw] of CASES) {
    assert.equal(new URL(safeNext(raw, ORIGIN), ORIGIN).origin, ORIGIN);
  }
});

const SALT16 = b64u(new Uint8Array(16).fill(7));

test("checkKdf accepts sane parameters and returns the salt bytes", () => {
  const salt = checkKdf({ kdf_salt: SALT16, kdf_iterations: 600000 });
  assert.equal(salt.length, 16);
  assert.equal(checkKdf({ kdf_salt: SALT16, kdf_iterations: 1000 }).length, 16);
  assert.equal(checkKdf({ kdf_salt: SALT16, kdf_iterations: 10_000_000 }).length, 16);
});

for (const [label, kdf] of [
  ["iterations below 1000", { kdf_salt: SALT16, kdf_iterations: 999 }],
  ["iterations of 1", { kdf_salt: SALT16, kdf_iterations: 1 }],
  ["iterations above 10M", { kdf_salt: SALT16, kdf_iterations: 10_000_001 }],
  ["non-integer iterations", { kdf_salt: SALT16, kdf_iterations: 600000.5 }],
  ["string iterations", { kdf_salt: SALT16, kdf_iterations: "600000" }],
  ["short salt", { kdf_salt: b64u(new Uint8Array(15)), kdf_iterations: 600000 }],
  ["empty salt", { kdf_salt: "", kdf_iterations: 600000 }],
  ["malformed salt", { kdf_salt: "!!!", kdf_iterations: 600000 }],
  ["missing body", null],
]) {
  test(`checkKdf refuses ${label}`, () => {
    assert.throws(() => checkKdf(kdf), UnsafeKdfError);
  });
}

// ---- the Needs you nav badge: the spaces' needs counts plus pending join requests

test("attentionBadge shows a bare count, hidden at zero", () => {
  assert.deepEqual(attentionBadge(3), { text: "3", title: "3 things need you", hidden: false });
  assert.deepEqual(attentionBadge(1), { text: "1", title: "1 thing needs you", hidden: false });
  assert.deepEqual(attentionBadge(0), { text: "", title: "", hidden: true });
  assert.deepEqual(attentionBadge("x"), { text: "", title: "", hidden: true });
  assert.equal(attentionBadge(250).text, "99+");
});

function fakeDoc() {
  const badge = { textContent: "", title: "", hidden: true };
  return { badge, getElementById: (id) => (id === "needs-badge" ? badge : null) };
}

// The badge counts what Needs you shows (polish wave): mirrors whose SEALED doc needs the human (the row
// opener sets `needs` from the doc, never the cleartext), messages to the human, and join requests.
const MIRRORS = [
  { id: "TIX-1", needs: "verdict" },   // cleartext says verdict, the sealed doc says question
  { id: "TIX-2", needs: "question" },  // cleartext says question, the sealed doc needs nothing
  { id: "TIX-3", needs: null },        // cleartext says nothing, the sealed doc needs an approval
  { id: "TIX-4", needs: "question" },  // does not open / routing mismatch: never counted
];
const SEALED = { "TIX-1": "question", "TIX-2": null, "TIX-3": "approval" };
const openRows = async (mirrors) => mirrors.map((m) => (m.id in SEALED
  ? { ...m, doc: { id: "D" }, needs: SEALED[m.id] } : { ...m, doc: null, error: "binding" }));

const answers = ({ mirrors = MIRRORS, messages = [], requests = [] } = {}) => async (path) => {
  if (path === "/api/mirrors") return { mirrors };
  if (path.startsWith("/api/messages")) return { messages };
  if (path === "/api/join-requests") return { requests };
  throw new Error(path);
};

test("the badge counts sealed needs, messages to the human and join requests, never cleartext needs", async () => {
  const doc = fakeDoc();
  const paths = [];
  const get = answers({ messages: [{ to_kind: "human" }, { to_kind: "project" }], requests: [{ id: "jr_x" }] });
  await refreshAttention(doc, async (path) => { paths.push(path); return get(path); }, openRows);
  assert.deepEqual(paths.sort(), ["/api/join-requests", "/api/messages?after=0&wait=0", "/api/mirrors"]);
  assert.deepEqual(doc.badge, { textContent: "4", title: "4 things need you", hidden: false });   // 2 + 1 + 1
  await refreshAttention(doc, answers({ mirrors: [] }), openRows);
  assert.equal(doc.badge.hidden, true);
});

test("refreshAttention keeps the badge as it was when the mirrors request fails", async () => {
  const doc = fakeDoc();
  await refreshAttention(doc, answers(), openRows);
  assert.equal(doc.badge.textContent, "2");
  await refreshAttention(doc, async () => { throw new Error("offline"); }, openRows);
  assert.equal(doc.badge.textContent, "2");
  await refreshAttention({ getElementById: () => null }, async () => { throw new Error("never called"); }, openRows);
});

test("needsCount still counts the sealed needs when messages and join requests fail", async () => {
  assert.equal(await needsCount(async (path) => { if (path === "/api/mirrors") return { mirrors: MIRRORS }; throw new Error("403"); },
    openRows), 2);
});

test("the login redirect never carries the URL fragment (a pairing key lives there)", () => {
  const key = "a".repeat(32) + ".ph_0123456789ab." + "A".repeat(43);
  assert.equal(loginHref({ pathname: "/pair", search: "", hash: "#" + key }), "/login?next=%2F");
  assert.equal(loginHref({ pathname: "/t/42", search: "", hash: "#answer" }), "/login?next=%2Ft%2F42");
  assert.equal(loginHref({ pathname: "/files", search: "?f=FILE7", hash: "" }), "/login?next=%2Ffiles%3Ff%3DFILE7");
});

test("no page script builds a next= from location.hash", async () => {
  const { readdirSync, readFileSync } = await import("node:fs");
  const dir = new URL("../../fileshare/static/js/", import.meta.url);
  for (const f of readdirSync(dir).filter((n) => n.endsWith(".js"))) {
    const src = readFileSync(new URL(f, dir), "utf8");
    for (const line of src.split("\n").filter((l) => l.includes("next="))) {
      assert.doesNotMatch(line, /location\.hash/, `${f}: ${line.trim()}`);
    }
  }
});

test("tellWorkerNeeds sends the workspaces and tickets that need you, setAppBadge follows the count", async () => {
  const { tellWorkerNeeds, setAppBadge } = await import("../../fileshare/static/js/nav.js");
  const posted = [];
  tellWorkerNeeds([{ space: "a", id: "TIX-1", needs: "question" }, { space: "a", id: "TIX-2", needs: null }, { space: "b", id: "TIX-3", needs: "approval" }],
    2, { serviceWorker: { controller: { postMessage: (m) => posted.push(m) } } });
  assert.deepEqual(posted, [{ type: "needs-spaces", spaces: ["a", "b"], tickets: ["a|TIX-1", "b|TIX-3"], messages: 2 }]);
  const calls = [];
  const nav = { setAppBadge: async (n) => calls.push(n), clearAppBadge: async () => calls.push(0) };
  setAppBadge(3, nav);
  setAppBadge(0, nav);
  assert.deepEqual(calls, [3, 0]);
  tellWorkerNeeds([], 0, {});                                  // no worker: no throw
});
