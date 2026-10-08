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
  ["subject text not shown", "unlock.js", 'text: p.subject.shown, facts', 'text: "", facts'],
  ["retried twice (the ask is not remembered)", "remote-transport.js", "asked = true;", "asked = false;"],
  ["retried although the sheet failed", "remote-transport.js", "if (u.ok) { args.meta = u.meta;", "if (true) { args.meta = u.meta;"],
  ["user verification only preferred", "bridge-crypto.js", 'allowCredentials: [{ type: "public-key", id: credentialId }], userVerification: "required"', 'allowCredentials: [{ type: "public-key", id: credentialId }], userVerification: "preferred"'],
  ["challenge not from the host (random)", "unlock.js", "const challenge = await assertionChallenge({", "const challenge = crypto.getRandomValues(new Uint8Array(32)); void ({"],
  ["challenge without the host's nonce", "unlock.js", "nonce: hexToBytes(meta.nonce), subject });", "nonce: new Uint8Array(32), subject });"],
  ["double sheet allowed", "unlock.js", "if (busy) return no(\"busy\");", "if (false) return no(\"busy\");"],
  ["assertion for another request id", "unlock.js", "assert(hexToBytes(refusal.rid), p.options, win)", "assert(new Uint8Array(16), p.options, win)"],
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
  ["credential made before the person's click", "unlock.js", "await gate?.();\n", ""],
  ["credential made without a platform authenticator check", "unlock.js", "if (!await win.PublicKeyCredential?.isUserVerifyingPlatformAuthenticatorAvailable?.()) return no(\"no_platform\");", ""],
  ["cancel does not close the sheet", "unlock.js", "const finish = (r) => { if (done) return; done = true; clearTimeout(timer); sheet?.close(); resolve(r); };", "const finish = (r) => { if (done) return; done = true; clearTimeout(timer); resolve(r); };"],
  ["a refusal is not shown the fixed sentence", "remote-transport.js", "onRefusal(code, unlockText(u.reason));", "onRefusal(code, \"\");"],
];

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  let survivors = 0, ran = 0;
  const only = process.argv[2];       // optional: run the mutations whose name contains this text
  for (const [name, file, from, to] of MUTATIONS.filter((m) => !only || m[0].includes(only))) {
    ran++;
    const dir = mkdtempSync(join(tmpdir(), "unlock-mut-"));
    cpSync(SRC, dir, { recursive: true });
    const p = join(dir, file), s = readFileSync(p, "utf8");
    if (!s.includes(from)) { console.log(`SKIPPED (pattern not found): ${name}`); survivors++; rmSync(dir, { recursive: true }); continue; }
    writeFileSync(p, s.replace(from, to));
    const r = spawnSync(process.execPath, ["--test", "--test-force-exit", TEST], { env: { ...process.env, UNLOCK_JS_DIR: dir }, encoding: "utf8", timeout: 60_000 });
    const killed = r.status !== 0;
    if (!killed) survivors++;
    console.log(`${killed ? "killed  " : "SURVIVED"}  ${name}`);
    rmSync(dir, { recursive: true });
  }
  console.log(`${ran - survivors}/${ran} killed`);
  process.exit(survivors ? 1 : 0);
}
