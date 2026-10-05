// The vectors must catch a broken device module, not only pass a correct one: each mutation below changes one rule of
// fileshare/static/js/bridge-crypto.js, and the conformance run over the vectors, or the checks of our own for the rules
// the vectors have no device case for (both in tests/js/support/bridge-conformance.mjs), must fail on it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { copyFileSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
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
  // pinned by ownChecks only: the vectors have no device case for these
  ["a chunk sent to the host accepted", "|| h.direction !== TO_DEVICE ", ""],
  ["another version accepted", "|| h.version !== 1 ", ""],
  ["an unknown flag accepted", "|| h.flags & ~(F_LAST | F_STREAM | F_REFUSAL)", ""],
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
