// The vectors must catch a broken device module, not only pass a correct one: each mutation below changes one rule of
// fileshare/static/js/bridge-crypto.js, and the conformance run over the vectors, or the checks of our own for the rules
// the vectors have no device case for (both in tests/js/support/bridge-conformance.mjs), must fail on it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { copyFileSync, cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import * as C from "../../fileshare/static/js/crypto.js";
import { conformance, ownChecks } from "./support/bridge-conformance.mjs";

const JS = new URL("../../fileshare/static/js/", import.meta.url);
const SOURCE = readFileSync(new URL("bridge-crypto.js", JS), "utf8");
const VEC = JSON.parse(readFileSync(new URL("../bridge_vectors.json", import.meta.url), "utf8"));

// [what it breaks, text in bridge-crypto.js, replacement]
const MUTATIONS = [
  ["REFUSAL without LAST accepted", "if (h.flags & F_REFUSAL && !(h.flags & F_LAST))", "if (false)"],
  ["an offset of exactly 24 h refused", "Math.abs(off) <= MAX_OFFSET_MS", "Math.abs(off) < MAX_OFFSET_MS"],
  ["the offset adopted more than once per rid", "if (!pend.offsetAdopted) {", "if (true) {"],
  ["any refusal code exempt from the window", 'meta.refusal === "stale_timestamp" && Number', "Number"],
  ["the host signature not checked", "if (!await verifySigned(ctx.hostKey, sig, signedBytes(header, body)))", "if (false)"],
  ["chunk order not checked", 'if (h.seq !== BigInt(pend.next)) return drop("out_of_order");', ""],
  ["the mailbox's last not compared", "mailbox.last !== last", "false"],
  ["a response for another device accepted", "|| bytesToHex(h.device) !== ctx.device", ""],
  ["a response for a rid not pending accepted", "pend = ctx.pending.get(rid);", "pend = ctx.pending.get(rid) || { next: 0, stream: false };"],
  ["the response window not checked", 'if (d > BigInt(WINDOW_MS) || d < -BigInt(WINDOW_MS)) return drop("stale_timestamp");', ""],
  ["the offset ignored in the window", "BigInt(nowMs + (ctx.offsetMs || 0))", "BigInt(nowMs)"],
  ["r = 0 accepted", "return r > 0n &&", "return r >= 0n &&"],
  ["s = n accepted", "&& s < P256_N", "&& s <= P256_N"],
  ["r = n accepted", "&& r < P256_N", "&& r <= P256_N"],
  ["a short signature length accepted", "sig.length !== SIG_LEN) return false", "sig.length < 63) return false"],
  ["device id without the workspace", "cat(L_DEVICE, len(\"workspace\", workspace, 16), ", "cat(L_DEVICE, "],
  ["device id label", 'label("device/v1|")', 'label("device/v1")'],
  ["fingerprint length", ".slice(0, 20);", ".slice(0, 16);"],
  ["fingerprint label", 'label("fp/v1|")', 'label("fp/v2|")'],
  ["host pin label", 'label("host/v1|")', 'label("host/v1")'],
  ["K_ws label", '"sharing/bridge/ws/v1|"', '"sharing/bridge/ws/v1"'],
  ["K_ws salt", "salt: new Uint8Array(0), info: te.encode(L_WS", "salt: new Uint8Array(32).fill(1), info: te.encode(L_WS"],
  ["K_msg label", 'label("msg/v1")', 'label("msg/v1|")'],
  ["AAD left out", "additionalData: header, tagLength", "additionalData: new Uint8Array(0), tagLength"],
  ["signature label", 'label("sig/v1|")', 'label("sig/v1")'],
  ["header: seq written little-endian", "dv.setBigUint64(72, BigInt(seq), false)", "dv.setBigUint64(72, BigInt(seq), true)"],
  ["meta over 64 KiB opened", "if (n > MAX_META || 4 + n > pt.length)", "if (4 + n > pt.length)"],
  ["pairing MAC label", 'label("pair/v1|")', 'label("pair/v1")'],
  ["pairing MAC without the pairing id", 'cat(L_PAIR, ws, len("pairing id", link.pairingId, 16), pub)', "cat(L_PAIR, ws, pub)"],
  ["phone-link proof label", 'label("phone-link/v1|")', 'label("phone-link/v1")'],
  ["pairing link accepts another version", "/^#?v1\\.", "/^#?v\\d\\."],
  ["assertion challenge: purpose and scope swapped", "[PURPOSES[purpose], SCOPES[scope]]", "[SCOPES[scope], PURPOSES[purpose]]"],
  ["assertion label", 'label("assert/v1|")', 'label("assert/v1")'],
  ["registration label", 'label("webauthn-reg/v1|")', 'label("webauthn-reg/v1")'],
  ["a lone surrogate accepted", '|| !s.isWellFormed()', ""],
  // pinned by the amended vectors (#82) and, a second time, by ownChecks
  ["a chunk sent to the host accepted", "|| h.direction !== TO_DEVICE ", ""],
  ["another version accepted", "|| h.version !== 1 ", ""],
  ["an unknown flag accepted", "|| h.flags & ~(F_LAST | F_STREAM | F_REFUSAL)", ""],
  ["a chunk forged without K_ws counts as a pin failure", "let pinFailure = false;", "let pinFailure = true;"],
  ["a K_ws-sealed chunk with a bad signature is not a pin failure", "pinFailure = true; } catch", "} catch"],
  ["upper-case workspace hex accepted in the link", "/^#?v1\\.([0-9a-f]{32})", "/^#?v1\\.([0-9a-fA-F]{32})"],
  ["upper-case pairing id hex accepted in the link", "\\.([0-9a-f]{32})\\.([A-Za-z0-9_-]{43})", "\\.([0-9a-fA-F]{32})\\.([A-Za-z0-9_-]{43})"],
  ["the label counted in UTF-16 units", "[...name].length > 80", "name.length > 80"],
  ["a label of 81 code points accepted", "[...name].length > 80", "[...name].length > 81"],
  ["a chunk over 256 KiB accepted", "|| env.length > MAX_CHUNK", ""],
  ["another key version accepted", "h.keyVersion !== ctx.keyVersion || ", ""],
  ["another workspace accepted", "bytesToHex(h.workspace) !== ctx.workspace || ", ""],
  ["exactly 300 s refused", "d > BigInt(WINDOW_MS)", "d >= BigInt(WINDOW_MS)"],
  ["the mailbox's stream not compared", "|| mailbox.stream !== stream ", ""],
  ["a stream's rid answered without STREAM", "|| pend.stream !== stream", ""],
  ["the mailbox's idx not compared", "|| !Number.isSafeInteger(mailbox.idx) || BigInt(mailbox.idx) !== h.seq", ""],
  ["the mailbox's id not compared", "mailbox?.id !== rid || ", "!mailbox || "],
  ["a host_ms that is not an integer exempt", "&& Number.isSafeInteger(meta.host_ms)", ""],
  ["the shown text re-cleaned", "  return s;\n}", '  return s.replace(/[\\p{Cf}]/gu, "");\n}'],
];

test("every mutation of the device module is caught by the vectors", async () => {
  const dir = mkdtempSync(join(tmpdir(), "bridge-mutants-"));
  try {
    copyFileSync(new URL("crypto.js", JS), join(dir, "crypto.js"));
    const survivors = [];
    for (const [i, [why, from, to]] of MUTATIONS.entries()) {
      assert.ok(SOURCE.includes(from), `mutation "${why}" no longer matches bridge-crypto.js`);
      const file = join(dir, `bridge-crypto-${i}.js`);
      writeFileSync(file, SOURCE.replace(from, to));
      const mutant = await import(pathToFileURL(file));
      try {
        await conformance(mutant, C, VEC);
        await ownChecks(mutant, C, VEC);
        survivors.push(why);
      } catch { /* caught */ }
    }
    assert.deepEqual(survivors, []);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// The session's and the store's rules: [file, what it breaks, text, replacement]. Each mutant runs
// tests/js/bridge-conformance.test.mjs against a mutated copy of fileshare/static/js, which must fail.
const RULE_MUTATIONS = [
  ["bridge-session.js", "any host-signature failure counts (a keyless server raises the alarm)", "if (r.pinFailure &&", 'if (r.why === "host_signature" &&'],
  ["bridge-session.js", "a verified chunk does not reset the pin-failure run", "    this.pinFailures = 0;\n    const pend", "    const pend"],
  ["bridge-session.js", "the adopted offset not used", "if (r.offsetMs !== undefined) this.offsetMs = r.offsetMs;", ""],
  ["bridge-session.js", "resync to high instead of high + 1", "atLeast(high + 1)", "atLeast(high)"],
  ["bridge-session.js", "a stream closed at exactly 60 s", "t - p.lastAt > STREAM_SILENCE_MS", "t - p.lastAt >= STREAM_SILENCE_MS"],
  ["bridge-store.js", "the counter hands out a number twice", "next: n + 1, value: n", "next: n, value: n"],
  ["bridge-store.js", "the counter's durability not strict", '{ durability: "strict" }', "undefined"],
  ["bridge-store.js", "an extractable K_ws stored", 'if (kWs?.extractable !== false) throw new Error("refusing to store an extractable key");', ""],
];

test("every mutation of the session's and the store's rules is caught", () => {
  const dir = mkdtempSync(join(tmpdir(), "bridge-rule-mutants-"));
  const testFile = fileURLToPath(new URL("bridge-conformance.test.mjs", import.meta.url));
  const env = { ...process.env, BRIDGE_JS_DIR: dir };
  delete env.NODE_TEST_CONTEXT;                    // a child of the test runner would otherwise report to it, not exit
  const passes = () => spawnSync(process.execPath, ["--test", "--test-timeout=60000", testFile], { env, encoding: "utf8" }).status === 0;
  try {
    cpSync(fileURLToPath(JS), dir, { recursive: true });
    assert.ok(passes(), "the unmutated copy must pass");
    const survivors = [];
    for (const [file, why, from, to] of RULE_MUTATIONS) {
      cpSync(fileURLToPath(JS), dir, { recursive: true });
      const source = readFileSync(join(dir, file), "utf8");
      assert.ok(source.includes(from), `mutation "${why}" no longer matches ${file}`);
      writeFileSync(join(dir, file), source.replace(from, to));
      if (passes()) survivors.push(why);
    }
    assert.deepEqual(survivors, []);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
