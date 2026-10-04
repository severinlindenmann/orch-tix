// tests/js/precache.test.mjs — the service worker's precache list (spec §16) matches static/.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, extname, sep } from "node:path";
import { fileURLToPath } from "node:url";

const STATIC = fileURLToPath(new URL("../../fileshare/static/", import.meta.url));
const list = JSON.parse(readFileSync(join(STATIC, "precache.json"), "utf8"));
// What the pages load: styles, scripts (vendor included), images and fonts. Not sw.js (the worker
// itself), not the license and checksum text files, not precache.json.
const SHELL_EXT = new Set([".css", ".js", ".png", ".svg", ".woff2"]);

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

test("precache assets are exactly the shell files under static/", () => {
  const onDisk = walk(STATIC)
    .map((p) => relative(STATIC, p).split(sep).join("/"))
    .filter((r) => SHELL_EXT.has(extname(r)) && r !== "sw.js")
    .map((r) => `/static/${r}`)
    .sort();
  const listed = list.assets.filter((a) => a.startsWith("/static/"));
  assert.deepEqual(listed, [...listed].sort(), "keep precache.json sorted");
  assert.deepEqual(listed, onDisk, "regenerate fileshare/static/precache.json");
});

test("precache includes the manifest and every page as a navigation", () => {
  assert.ok(list.assets.includes("/manifest.webmanifest"));
  assert.deepEqual(list.pages, ["/", "/t", "/files", "/settings", "/login", "/sandbox/html", "/sandbox/widget", "/pair"]);
});

test("nothing precached is ever a bypassed path", () => {
  for (const u of [...list.pages, ...list.assets]) {
    assert.ok(!/^\/(api|p|skill)\/|^\/onboarding|^\/sw\.js/.test(u), u);
  }
});

test("every precached script is loaded by a page or imported by another module (no dead code)", () => {
  const html = readdirSync(STATIC).filter((n) => n.endsWith(".html")).map((n) => readFileSync(join(STATIC, n), "utf8"));
  const js = walk(join(STATIC, "js")).filter((p) => p.endsWith(".js"));
  const used = new Set();
  for (const page of [...html, readFileSync(join(STATIC, "sw.js"), "utf8")]) {
    for (const m of page.matchAll(/\/static\/js\/([a-z0-9-]+\.js)/g)) used.add(m[1]);
  }
  for (const p of js) {
    for (const m of readFileSync(p, "utf8").matchAll(/(?:from |import ?\(?)\s*"\.\/([a-z0-9-]+\.js)"/g)) used.add(m[1]);
  }
  const listed = list.assets.filter((a) => a.startsWith("/static/js/")).map((a) => a.slice("/static/js/".length));
  assert.deepEqual(listed.filter((n) => !used.has(n)), []);
});
