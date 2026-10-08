// Mutation check of the typing glue: node tests/js/lease-mutations.mjs
// Copies fileshare/static/js, applies ONE mutation at a time and runs tests/js/lease.test.mjs against the copy
// (LEASE_JS_DIR). Every mutation must make that run fail; a survivor is reported and the exit code is 1.
// Mutations only the browser can see (the shim's orchHost.remote, the sheet and the batcher) are run by hand against
// tests/browser/test_terminal.py; the PR lists the result.
import { cpSync, mkdtempSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const SRC = fileURLToPath(new URL("../../fileshare/static/js/", import.meta.url));
const TEST = fileURLToPath(new URL("./lease.test.mjs", import.meta.url));

export const MUTATIONS = [
  ["the note is not cleared at a new lease_required", "remote-lease.js", "setLease(0);\n    const r = await ask", "const r = await ask"],
  ["a cancelled sheet sets no cool-down", "remote-lease.js", 'else if (r.reason !== "busy") coolUntil = now() + coolMs;', ""],
  ["a busy sheet sets a cool-down", "remote-lease.js", 'r.reason !== "busy"', "true"],
  ["the cool-down is not honoured", "remote-lease.js", "if (now() < coolUntil) refuse(LEASE_TEXT.locked);", ""],
  ["no stream: the request is sent anyway", "remote-lease.js", "if (!rid) refuse(LEASE_TEXT.no_stream);", ""],
  ["a terminal's keys ride on any stream", "remote-lease.js", "want === null || path === want", "true"],
  ["the stream is not named in the request", "remote-lease.js", "{ ...req, streamRid: rid }", "{ ...req }"],
  ["a closed stream is not forgotten", "remote-lease.js", "finally { if (mine) streams.delete(mine); }", "finally { }"],
  ["the lease note is wrong by 15 minutes", "remote-lease.js", "setLease(unlockedAt + leaseMs);", "setLease(unlockedAt);"],
  ["a refused assertion leaves the confirmation pending", "remote-lease.js", "unlockedAt = 0;\n        gone(e);", "gone(e);"],
  ["a revoked device is still sent to", "remote-lease.js", "if (dead) refuse(dead);", ""],
  ["a revoke that ends the stream is not remembered", "remote-lease.js", "gone(e);\n        throw e;\n      } finally", "throw e;\n      } finally"],
  ["a fresh assertion is treated as a lease", "remote-lease.js", 'if (refusal.code !== "lease_required") return ask(session, refusal, opts);', ""],
  ["the abort signal is dropped on the lease path", "remote-lease.js", "const r = await ask(session, refusal, opts);", "const r = await ask(session, refusal);"],
  ["the abort signal is dropped on the other path", "remote-lease.js", "return ask(session, refusal, opts);", "return ask(session, refusal);"],
  ["a size post while watching is sent", "remote-lease.js", "if (r.size && !(until > now()))", "if (false)"],
  ["a size post is not recognised", "remote-lease.js", 'size: path.endsWith("/size")', "size: false"],
  ["a quiet size answer is a banner", "remote-lease.js", "{ yield* quiet(); return; }", "{ refuse(LEASE_TEXT.locked); }"],
  ["a second sheet for one request", "remote-transport.js", "asked = true;", ""],
  ["a GET of a lease path is a lease request", "remote-lease.js", 'if (req.method !== "POST" || req.stream) return null;', "if (req.stream) return null;"],
  ["Start agent is not a lease route", "remote-lease.js", "/^\\/t\\/[^/]+\\/agent\\/start$/,", ""],
  ["the terminal's own keys route is not a lease route", "remote-lease.js", "(?:keys|size|end)", "(?:size|end)"],
  ["a lease that ended is not cleared", "remote-lease.js", "timer = setTimeout(() => { until = 0; onLease(null); }, Math.max(0, t - now()));", ""],
  ["the transport does not name the stream", "remote-transport.js", "if (streamRid) args.stream = hexToBytes(streamRid);", ""],
  ["the assert request names the stream too", "remote-transport.js", "delete args.stream; ", ""],
  ["the head event carries no rid", "remote-transport.js", "page: m.page === true, rid: sent.id }", "page: m.page === true }"],
  ["a stream is not allowed by the scope table", "remote-model.js", '{ methods: ["GET"], pattern: "/terminals/:name/stream", stream: true }', "{ methods: [], pattern: \"/x\" }"],
  ["a cool-down of zero", "remote-lease.js", "export const COOL_MS = 30_000;", "export const COOL_MS = 0;"],
  ["a lease of a minute", "remote-lease.js", "export const LEASE_MS = 15 * 60_000;", "export const LEASE_MS = 60_000;"],
];

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  let survivors = 0, ran = 0;
  const only = process.argv[2];       // optional: run the mutations whose name contains this text
  for (const [name, file, from, to] of MUTATIONS.filter((m) => !only || m[0].includes(only))) {
    ran++;
    const dir = mkdtempSync(join(tmpdir(), "lease-mut-"));
    cpSync(SRC, dir, { recursive: true });
    const p = join(dir, file), s = readFileSync(p, "utf8");
    if (!s.includes(from)) { console.log(`SKIPPED (pattern not found): ${name}`); survivors++; rmSync(dir, { recursive: true }); continue; }
    writeFileSync(p, s.replace(from, to));
    const r = spawnSync(process.execPath, ["--test", "--test-force-exit", TEST], { env: { ...process.env, LEASE_JS_DIR: dir }, encoding: "utf8", timeout: 60_000 });
    const killed = r.status !== 0;
    if (!killed) survivors++;
    console.log(`${killed ? "killed  " : "SURVIVED"}  ${name}`);
    rmSync(dir, { recursive: true });
  }
  console.log(`${ran - survivors}/${ran} killed`);
  process.exit(survivors ? 1 : 0);
}
