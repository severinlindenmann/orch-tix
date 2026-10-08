// fileshare/static/js/unlock.js: the unlock sheet and the platform credential (docs/bridge-protocol.md §9, issue #26).
// Face ID, Touch ID, Windows Hello or the device PIN answer a challenge the computer issued; NEVER the passphrase, and
// nothing here is stored. Drawn in the TIX page, outside the dashboard frame (no WebAuthn there, no reach into this DOM).
//   askAssertion(session, {code, meta, rid}, {signal}) -> {ok: true, meta: <the op "assert" request>} | {ok: false, reason, text?}
//   registerCredential({session, label, send, click, signal}) -> {ok: true, credentialId, synced} | {ok: false, reason}
//   unlockText(reason) -> the fixed sentence for a failure
import { hexToBytes, unb64u } from "./crypto.js";
import { assert, assertionChallenge, assertionOptions, register, registrationChallenge, registrationOptions, subjectOf } from "./bridge-crypto.js";
import { el, shown } from "./ui.js";

export const SHEET_DELAY_MS = 500;      // a click on the button counts only this long after the sheet appeared
const SCOPES = new Set(["look", "decide", "operate", "type"]);
const FOR_CODE = { assertion_required: "fresh", lease_required: "lease" };
const HEX64 = /^[0-9a-f]{64}$/, HEX32 = /^[0-9a-f]{32}$/;

export const UNLOCK_TEXT = Object.freeze({
  cancelled: "Not confirmed, so nothing was done.",
  expired: "The confirmation ran out of time, so nothing was done. Try again.",
  failed: "The confirmation did not work, so nothing was done.",
  busy: "Another confirmation is open. Finish or cancel it first.",
  no_credential: "This browser has no Face ID or device unlock registered for that computer, so it cannot do that. Pair it again on a device that has one.",
  too_long: "The computer sent a request that is too long to check on this phone.",
  odd_text: "The computer sent text with invisible or look-alike spacing characters that this phone cannot check.",
  timeout: "Registering took too long, so this browser has no device unlock. It can pair at Look, Decide and Operate but cannot get Type. Pair again to retry.",
  bad_request: "The computer asked for a confirmation this app cannot read, so nothing was done.",
  no_platform: "This browser has no Face ID, Touch ID, Windows Hello or device PIN it can use. It can pair at Look, Decide and Operate but cannot get Type.",
  refused: "The computer did not take the unlock. This browser can pair at Look, Decide and Operate but cannot get Type.",
});
export const unlockText = (reason) => UNLOCK_TEXT[reason] || UNLOCK_TEXT.failed;

const no = (reason) => ({ ok: false, reason });
const fail = (reason) => Object.assign(new Error(reason), { reason });
let busy = false;     // at most one sheet at a time
export const sheetOpen = () => busy;

// ---- what the person is shown: the host's text, made hard to pad ----------------------------------------------------
// The host cleans the text but sets no limit, and keeps spaces of every kind. So the device: refuses any space-like or
// invisible character except ASCII space and line feed, and more than 2 combining marks in a row; draws a run of 3 or
// more spaces and a run of 2 or more empty lines as ONE styled marker element (never as text the host could type);
// states lines and characters; shows the END apart; refuses what is still over 40 lines or 2000 characters.
export const MAX_CHARS = 2000, MAX_LINES = 40, TAIL_CHARS = 80, BLANKS = "[… blank lines …]";
const ODD = /[\p{Zs}\p{Zl}\p{Zp}\p{Cc}\p{Cf}\p{Co}\p{Cn}\p{Default_Ignorable_Code_Point}\u2800]/u;
const atomText = (a) => (a.t === "c" ? a.s : a.t === "s" ? `[${a.n} spaces]` : BLANKS);
const weight = (a) => (a.t === "c" ? Array.from(a.s).length : a.t === "s" ? a.n : 1);
export function inspectText(text) {
  const s = String(text);
  for (const c of s) if (c !== " " && c !== "\n" && ODD.test(c)) return { ok: false, why: "odd" };
  if (/\p{Mn}{3,}/u.test(s)) return { ok: false, why: "odd" };
  const rows = [];
  let blank = 0;
  const flush = () => { if (blank > 1) rows.push([{ t: "b" }]); else if (blank === 1) rows.push([]); blank = 0; };
  for (const l of s.split("\n")) {
    if (/^ *$/.test(l)) { blank++; continue; }
    flush();
    rows.push(l.split(/( {3,})/).map((p, i) => (i % 2 ? { t: "s", n: p.length } : { t: "c", s: p })).filter((a) => a.t === "s" || a.s));
  }
  flush();
  const atoms = rows.flatMap((r, i) => (i ? [{ t: "c", s: "\n" }, ...r] : r));
  const chars = atoms.reduce((n, a) => n + weight(a), 0);
  const tailAtoms = [];
  for (let i = atoms.length - 1, left = TAIL_CHARS; i >= 0 && left > 0; i--) {
    const a = atoms[i];
    const part = a.t === "c" ? { t: "c", s: Array.from(a.s).slice(-left).join("") } : a;
    tailAtoms.unshift(part);
    left -= weight(part);
  }
  return { ok: rows.length <= MAX_LINES && chars <= MAX_CHARS, why: "long", atoms, tailAtoms, text: atoms.map(atomText).join(""), tail: tailAtoms.map(atomText).join(""), lines: rows.length, chars };
}

const mark = (label, kind) => {
  const m = el("span", { class: "unlock-mark", "data-mark": kind }, label);
  Object.assign(m.style, { background: "var(--text)", color: "var(--surface)", borderRadius: "3px", padding: "0 3px", fontFamily: "sans-serif", fontSize: "11px" });
  return m;
};
const nodesOf = (atoms) => atoms.map((a) => (a.t === "c" ? shown(a.s) : mark(atomText(a), a.t === "s" ? "spaces" : "blanks")));

// ---- the sheet ----------------------------------------------------------------------------------------------------
// spec {title, text | atoms, tail | tailAtoms, facts, delayMs, onConfirm(), onCancel()}. onConfirm runs INSIDE the click, so
// the browser sees a user gesture; it runs for a trusted click only, not before delayMs, only with the text scrolled to its
// end, once. Cancel is always allowed.
export function drawSheet({ title, text = "", atoms = null, tail = "", tailAtoms = null, facts, delayMs, onConfirm, onCancel }, doc = document) {
  const shownAt = Date.now();
  let used = false, timeOk = false;
  const body = el("pre", { id: "unlock-text", dir: "auto", tabindex: "0" }, nodesOf(atoms || [{ t: "c", s: text }]));
  const atEnd = () => body.scrollHeight - body.scrollTop - body.clientHeight <= 1;
  const go = el("button", { type: "button", class: "btn btn-accent", id: "unlock-go", disabled: true, onclick: (e) => {
    if (used || !e.isTrusted || Date.now() - shownAt < delayMs || !atEnd()) return;
    used = true;
    go.disabled = true;
    onConfirm();
  } }, "Confirm");
  body.style.unicodeBidi = "isolate";
  const more = el("p", { class: "hint", id: "unlock-more", hidden: true }, "More text below. Scroll to the end to confirm.");
  const endAtoms = tailAtoms || (tail ? [{ t: "c", s: tail }] : null);
  const end = endAtoms ? [el("p", { class: "hint" }, "The text ends with:"), el("pre", { id: "unlock-tail", dir: "auto" }, nodesOf(endAtoms))] : [];
  end[1]?.style.setProperty("unicode-bidi", "isolate");
  const node = el("div", { class: "frame-prompt", id: "unlock-sheet", role: "dialog", "aria-modal": "true", "aria-label": title },
    el("p", {}, title), body, more, end, facts.map((f) => el("p", { class: "hint" }, f)), go,
    el("button", { type: "button", class: "btn", id: "unlock-cancel", onclick: () => onCancel() }, "Cancel"));
  node.style.zIndex = "100";                          // above any question of the frame
  doc.body.append(node);
  const refresh = () => { more.hidden = atEnd(); go.disabled = used || !timeOk || !atEnd(); };
  body.addEventListener("scroll", refresh);
  more.hidden = atEnd();
  const enable = setTimeout(() => { timeOk = true; refresh(); }, delayMs);
  return { close() { clearTimeout(enable); node.remove(); } };
}

// ---- an assertion for a refusal -----------------------------------------------------------------------------------
// refusal: {code: "assertion_required" | "lease_required", meta: the refusal's signed fields, rid: the refused request (hex)}.
// session: DeviceSession plus .credentialId (b64u, from the pairing).
async function prepare(session, { code, meta, rid }, now) {
  const purpose = FOR_CODE[code];
  if (!purpose || meta?.purpose !== purpose || !SCOPES.has(meta.scope) || !HEX64.test(meta.nonce || "") || !HEX32.test(rid || "")
      || !Number.isSafeInteger(meta.expires_ms) || meta.expires_ms < 0) throw fail("bad_request");
  if (!session.credentialId) throw fail("no_credential");
  let subject, credentialId;
  try { subject = subjectOf(meta.subject); credentialId = unb64u(session.credentialId); } catch { throw fail("bad_request"); }
  const expiresAt = meta.expires_ms - (session.offsetMs || 0);       // the host's clock on this device's clock
  if (expiresAt <= now()) throw fail("expired");
  const view = inspectText(subject.shown);
  if (!view.ok) throw Object.assign(fail("bad_request"), { text: view.why === "odd" ? UNLOCK_TEXT.odd_text : UNLOCK_TEXT.too_long });
  const challenge = await assertionChallenge({ workspace: hexToBytes(session.workspace), deviceId: session.deviceIdBytes, rid: hexToBytes(rid),
    purpose, scope: meta.scope, expiresMs: meta.expires_ms, nonce: hexToBytes(meta.nonce), subject });
  return { purpose, scope: meta.scope, subject, view, expiresAt, challenge, options: assertionOptions({ challenge, credentialId }) };
}

const sameBytes = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);

export async function askAssertion(session, refusal, d = {}) {
  const { win = globalThis, now = Date.now, draw = drawSheet, delayMs = SHEET_DELAY_MS, signal = null } = d;
  if (busy) return no("busy");
  busy = true;
  try {
    if (!win.navigator?.credentials) return no("failed");
    if (signal?.aborted) return no("cancelled");
    const p = await prepare(session, refusal, now);
    if (signal?.aborted) return no("cancelled");
    return await new Promise((resolve) => {
      const ac = new AbortController();                                // ends the OS prompt when the sheet ends first
      let done = false, sheet = null, timer = null;
      const onAbort = () => finish(no("cancelled"));
      const finish = (r) => { if (done) return; done = true; clearTimeout(timer); signal?.removeEventListener("abort", onAbort); ac.abort(); sheet?.close(); resolve(r); };
      signal?.addEventListener("abort", onAbort, { once: true });
      const v = p.view, more = v.lines > 1 || v.chars > TAIL_CHARS;
      const facts = [`${v.lines} line${v.lines === 1 ? "" : "s"}, ${v.chars} characters`, `Needs: ${p.scope}`, `Expires at ${new Date(p.expiresAt).toLocaleTimeString()}`,
        ...(p.subject.digest ? [`Check these 8 characters on the computer: ${p.subject.digest.slice(0, 8)}`] : []),
        "Confirm with Face ID, Touch ID, Windows Hello or your device PIN. Never your passphrase."];
      sheet = draw({ title: p.purpose === "lease" ? "Confirm to type for 15 minutes" : "Confirm this action", text: v.text, atoms: v.atoms,
        tail: more ? v.tail : "", tailAtoms: more ? v.tailAtoms : null, facts, delayMs,
        onCancel: () => finish(no("cancelled")),
        onConfirm: () => {
          assert(hexToBytes(refusal.rid), { ...p.options, signal: ac.signal }, win).then((fields) => {
            // the authenticator signed over exactly our challenge, for a get, or the host would refuse it anyway
            const cd = JSON.parse(new TextDecoder().decode(unb64u(fields.client_data_json)));
            if (cd.type !== "webauthn.get" || !sameBytes(unb64u(cd.challenge), p.challenge)) return finish(no("failed"));
            finish({ ok: true, meta: { op: "assert", ...fields } });
          }, (e) => finish(no(e?.name === "NotAllowedError" || e?.name === "AbortError" ? "cancelled" : "failed")));
        } });
      timer = setTimeout(() => finish(no("expired")), Math.max(0, p.expiresAt - now()));
    });
  } catch (e) {
    return e?.text ? { ok: false, reason: e.reason, text: e.text } : no(e?.reason || "failed");
  } finally {
    busy = false;
  }
}

// ---- the credential at pairing (§9.2) -----------------------------------------------------------------------------
// send(meta) -> the host's answer meta, or null. click(run, again) shows the register button and calls run() SYNCHRONOUSLY
// inside the person's click (so create() runs in the user activation, which Safari needs), resolving to its result; omitted
// in tests. credential_begin and the challenge come BEFORE the click; if the host's 120 s window has passed by click
// time, run() answers STALE and the whole thing is redone once with a second click ("Try again").
const STALE = Symbol("stale");
export async function registerCredential({ session, label, send, click, signal = null, win = globalThis, now = Date.now }) {
  const ac = new AbortController();
  const onAbort = () => ac.abort();
  signal?.addEventListener("abort", onAbort, { once: true });
  try {
    if (!await win.PublicKeyCredential?.isUserVerifyingPlatformAuthenticatorAvailable?.()) return no("no_platform");
    for (let again = false; ; again = true) {
      const begin = await send({ op: "credential_begin" });
      if (!begin || !HEX64.test(begin.nonce || "") || !Number.isSafeInteger(begin.expires_ms)) return no("refused");
      const challenge = await registrationChallenge({ workspace: hexToBytes(session.workspace), deviceId: session.deviceIdBytes,
        expiresMs: begin.expires_ms, nonce: hexToBytes(begin.nonce) });
      const options = { ...registrationOptions({ challenge, deviceId: session.deviceIdBytes, label }), signal: ac.signal };
      const run = () => (now() + (session.offsetMs || 0) >= begin.expires_ms - 1000 ? STALE : register(options, win));
      const fields = await (click ? click(run, again) : run());
      if (fields === STALE) { if (again) return no("timeout"); continue; }
      const fin = await send({ op: "credential_finish", ...fields });
      if (fin?.registered !== true) return no("refused");
      return { ok: true, credentialId: fields.credential_id, synced: fin.synced === true };
    }
  } catch (e) {
    return no(e?.name === "TimeoutError" ? "timeout" : e?.name === "NotAllowedError" || e?.name === "AbortError" ? "cancelled" : "failed");
  } finally {
    signal?.removeEventListener("abort", onAbort);
  }
}
