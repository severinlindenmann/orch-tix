// Mutation check of the unlock glue: node tests/js/unlock-mutations.mjs
// Copies fileshare/static/js, applies ONE mutation at a time and runs tests/js/unlock.test.mjs against the copy
// (UNLOCK_JS_DIR). Every mutation must make that run fail; a survivor is reported and the exit code is 1.
// Mutations only the browser can see (sheet drawn with innerHTML, the enable delay, the trusted-click check) are run by
// hand against tests/browser/test_unlock.py; the PR lists the result.
import { cpSync, mkdtempSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const SRC = fileURLToPath(new URL("../../fileshare/static/js/", import.meta.url));
const TEST = fileURLToPath(new URL("./unlock.test.mjs", import.meta.url));

export const MUTATIONS = [
  ["subject text not shown", "unlock.js", 'text: v.text, atoms', 'text: "", atoms'],
  ["retried twice (the ask is not remembered)", "remote-transport.js", "asked = true;", "asked = false;"],
  ["retried although the sheet failed", "remote-transport.js", "if (u.ok) { args.meta = u.meta;", "if (true) { args.meta = u.meta;"],
  ["user verification only preferred", "bridge-crypto.js", 'allowCredentials: [{ type: "public-key", id: credentialId }], userVerification: "required"', 'allowCredentials: [{ type: "public-key", id: credentialId }], userVerification: "preferred"'],
  ["challenge not from the host (random)", "unlock.js", "const challenge = await assertionChallenge({", "const challenge = crypto.getRandomValues(new Uint8Array(32)); void ({"],
  ["challenge without the host's nonce", "unlock.js", "nonce: hexToBytes(meta.nonce), subject });", "nonce: new Uint8Array(32), subject });"],
  ["double sheet allowed", "unlock.js", "if (busy) return no(\"busy\");", "if (false) return no(\"busy\");"],
  ["assertion for another request id", "unlock.js", "assert(hexToBytes(refusal.rid), {", "assert(new Uint8Array(16), {"],
  ["challenge for another request id", "unlock.js", "rid: hexToBytes(rid),\n    purpose", "rid: new Uint8Array(16),\n    purpose"],
  ["passphrase prompt instead of no credential", "unlock.js", 'if (!session.credentialId) throw fail("no_credential");', 'if (!session.credentialId) { win.prompt("Passphrase"); }'],
  ["answer's challenge not compared", "unlock.js", "!sameBytes(unb64u(cd.challenge), p.challenge)", "false"],
  ["answer's type not compared", "unlock.js", 'cd.type !== "webauthn.get" ||', ""],
  ["another credential may answer", "bridge-crypto.js", 'if (!bytesEqual(new Uint8Array(c.rawId), options.publicKey.allowCredentials[0].id)) throw new Error("another credential answered");', ""],
  ["expiry ignored", "unlock.js", "if (expiresAt <= now()) throw fail(\"expired\");", ""],
  ["host clock offset ignored", "unlock.js", "meta.expires_ms - (session.offsetMs || 0)", "meta.expires_ms"],
  ["sheet never times out", "unlock.js", "timer = setTimeout(() => finish(no(\"expired\")), Math.max(0, p.expiresAt - now()));", ""],
  ["lease treated as a fresh action", "unlock.js", 'lease_required: "lease"', 'lease_required: "fresh"'],
  ["any refusal code is accepted", "unlock.js", "if (!purpose || meta?.purpose !== purpose ||", "if (!purpose ||"],
  ["unknown subject kind accepted", "bridge-crypto.js", "!KINDS.has(s.kind) || ", ""],
  ["scope fixed to type in the challenge", "unlock.js", "purpose, scope: meta.scope, expiresMs", "purpose, scope: \"type\", expiresMs"],
  ["cancel does not close the sheet", "unlock.js", "sheet?.close(); resolve(r); };", "resolve(r); };"],
  ["a refusal is not shown the fixed sentence", "remote-transport.js", "onRefusal(code, text);\n              throw", "onRefusal(code, \"\");\n              throw"],
  ["abort does not close the sheet", "unlock.js", 'signal?.addEventListener("abort", onAbort, { once: true });\n      const v', 'const v'],
  ["transport does not hand over its abort", "remote-transport.js", "rid: sent.id }, { signal });", "rid: sent.id }, {});"],
  ["the OS prompt is not withdrawn (get)", "unlock.js", "{ ...p.options, signal: ac.signal }", "{ ...p.options }"],
  ["the OS prompt is not withdrawn (create)", "unlock.js", "signal: ac.signal };\n      const run", "};\n      const run"],
  ["the sheet's end does not abort the OS prompt", "unlock.js", "ac.abort(); sheet?.close()", "sheet?.close()"],
  // the person's click and create()
  ["registration without the person's click", "unlock.js", "await (click ? click(run, again) : run())", "await run()"],
  ["create() after an await, outside the activation", "unlock.js", "STALE : register(options, win))", "STALE : Promise.resolve().then(() => register(options, win)))"],
  ["a stale registration window is not noticed", "unlock.js", "(now() + (session.offsetMs || 0) >= begin.expires_ms - 1000 ? STALE : register(options, win))", "register(options, win)"],
  ["begin not redone after a stale click", "unlock.js", "if (fields === STALE) { if (again) return no(\"timeout\"); continue; }", "if (fields === STALE) return no(\"timeout\");"],
  ["credential made without a platform authenticator check", "unlock.js", "if (!await win.PublicKeyCredential?.isUserVerifyingPlatformAuthenticatorAvailable?.()) return no(\"no_platform\");", ""],
  // the text
  ["empty lines not collapsed", "unlock.js", 'if (l.trim() === "") { blank++; continue; }', "if (false) { blank++; continue; }"],
  ["a run of exactly two empty lines not collapsed", "unlock.js", "if (blank > 1) rows.push", "if (blank > 2) rows.push"],
  ["a single empty line dropped", "unlock.js", "else if (blank === 1) rows.push([]);", ""],
  ["end of the text not shown apart", "unlock.js", "tail: more ? v.tail : \"\", tailAtoms: more ? v.tailAtoms : null", "tail: \"\", tailAtoms: null"],
  ["size not stated", "unlock.js", "const facts = [`${v.lines} line${v.lines === 1 ? \"\" : \"s\"}, ${v.chars} characters`, ", "const facts = ["],
  ["too long text still opens", "unlock.js", "if (!view.ok) throw", "if (false) throw"],
  ["line limit is exclusive", "unlock.js", "rows.length <= MAX_LINES", "rows.length < MAX_LINES"],
  ["character limit is exclusive", "unlock.js", "chars <= MAX_CHARS", "chars < MAX_CHARS"],
  ["line limit raised", "unlock.js", "MAX_LINES = 40", "MAX_LINES = 4000"],
  ["character limit raised", "unlock.js", "MAX_CHARS = 2000", "MAX_CHARS = 2000000"],
  ["spaces do not count as characters", "unlock.js", 'a.t === "s" ? a.n : 1', 'a.t === "s" ? 0 : 1'],
  ["any odd character allowed", "unlock.js", 'if (c !== " " && c !== "\\n" && ODD.test(c)) return', "if (false) return"],
  ["no-break and other Zs spaces allowed", "unlock.js", "[\\p{Zs}\\p{Zl}", "[\\p{Zl}"],
  ["braille blank allowed", "unlock.js", "}\\u2800]/u", "}]/u"],
  ["format characters allowed", "unlock.js", "\\p{Cf}", ""],
  ["control characters allowed", "unlock.js", "\\p{Cc}", ""],
  ["line separators allowed", "unlock.js", "\\p{Zl}\\p{Zp}", ""],
  ["private use allowed", "unlock.js", "\\p{Co}", ""],
  ["combining marks not capped", "unlock.js", "/\\p{Mn}{3,}/u", "/\\p{Mn}{30,}/u"],
  ["runs of spaces not marked", "unlock.js", "/( {3,})/", "/( {3000,})/"],
  ["a run of exactly three spaces not marked", "unlock.js", "/( {3,})/", "/( {4,})/"],
  ["confirm works before the text is scrolled to its end (click)", "unlock.js", "|| !atEnd()) return;", ") return;"],
  ["confirm enabled before the text is scrolled to its end", "unlock.js", "go.disabled = used || !timeOk || !atEnd();", "go.disabled = used || !timeOk;"],
];

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  let survivors = 0, ran = 0;
  const only = process.argv[2];       // optional: run the mutations whose name contains this text
  for (const [name, file, from, to] of MUTATIONS.filter((m) => !only || m[0].includes(only))) {
    ran++;
    const dir = mkdtempSync(join(tmpdir(), "unlock-mut-"));
    cpSync(SRC, dir, { recursive: true });
    const p = join(dir, file), s = readFileSync(p, "utf8");
    const pairs = Array.isArray(from) ? from.map((f, i) => [f, to[i]]) : [[from, to]];
    if (!pairs.every(([f]) => s.includes(f))) { console.log(`SKIPPED (pattern not found): ${name}`); survivors++; rmSync(dir, { recursive: true }); continue; }
    writeFileSync(p, pairs.reduce((acc, [f, t]) => acc.replace(f, t), s));
    const r = spawnSync(process.execPath, ["--test", "--test-force-exit", TEST], { env: { ...process.env, UNLOCK_JS_DIR: dir }, encoding: "utf8", timeout: 60_000 });
    const killed = r.status !== 0;
    if (!killed) survivors++;
    console.log(`${killed ? "killed  " : "SURVIVED"}  ${name}`);
    rmSync(dir, { recursive: true });
  }
  console.log(`${ran - survivors}/${ran} killed`);
  process.exit(survivors ? 1 : 0);
}
