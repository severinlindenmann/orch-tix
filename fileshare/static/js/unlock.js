// fileshare/static/js/unlock.js: the unlock sheet and the platform credential (docs/bridge-protocol.md §9, issue #26).
// Face ID, Touch ID, Windows Hello or the device PIN answer a challenge the computer issued; NEVER the passphrase, and
// nothing here is stored. Drawn in the TIX page, outside the dashboard frame (which has no WebAuthn and cannot reach
// this DOM). What the person reads is exactly what the challenge commits to: the host's `shown` text, as text.
//   askAssertion(session, {code, meta, rid}) -> {ok: true, meta: <the op "assert" request to send>} | {ok: false, reason}
//   registerCredential({session, label, send, gate}) -> {ok: true, credentialId, synced} | {ok: false, reason}
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

// The host's text may hold line feeds and no cap (the host cleans, it does not limit), so a long run of empty lines
// could push the dangerous tail out of the visible box. The device therefore collapses runs of empty lines into ONE
// visible marker, states the size, always shows the END of the text apart, and refuses what is still too long.
export const MAX_CHARS = 2000, MAX_LINES = 40, TAIL_CHARS = 80, BLANKS = "[\u2026 blank lines \u2026]";
export function inspectText(text) {
  const out = [];
  let blank = 0;
  const flush = () => { if (blank > 1) out.push(BLANKS); else if (blank === 1) out.push(""); blank = 0; };
  for (const l of String(text).split("\n")) {
    if (l.trim() === "") { blank++; continue; }
    flush();
    out.push(l);
  }
  flush();
  const t = out.join("\n"), chars = Array.from(t);
  return { text: t, lines: out.length, chars: chars.length, tail: chars.slice(-TAIL_CHARS).join(""), ok: out.length <= MAX_LINES && chars.length <= MAX_CHARS };
}

// ---- the sheet ----------------------------------------------------------------------------------------------------
// spec {title, text, facts: [string], delayMs, onConfirm(), onCancel()}. onConfirm runs INSIDE the click, so the browser
// sees a user gesture; it runs for a trusted click only, not before delayMs, once. Dismissing is always allowed.
export function drawSheet({ title, text, tail = "", facts, delayMs, onConfirm, onCancel }, doc = document) {
  const shownAt = Date.now();
  let used = false;
  const go = el("button", { type: "button", class: "btn btn-accent", id: "unlock-go", disabled: true, onclick: (e) => {
    if (used || !e.isTrusted || Date.now() - shownAt < delayMs) return;
    used = true;
    go.disabled = true;
    onConfirm();
  } }, "Confirm");
  const body = el("pre", { id: "unlock-text", dir: "auto" }, shown(text));
  body.style.unicodeBidi = "isolate";
  const more = el("p", { class: "hint", id: "unlock-more", hidden: true }, "More text below. Scroll the box above.");
  const end = tail ? [el("p", { class: "hint" }, "The text ends with:"), el("pre", { id: "unlock-tail", dir: "auto" }, shown(tail))] : [];
  end[1]?.style.setProperty("unicode-bidi", "isolate");
  const node = el("div", { class: "frame-prompt", id: "unlock-sheet", role: "dialog", "aria-modal": "true", "aria-label": title },
    el("p", {}, title), body, more, end, facts.map((f) => el("p", { class: "hint" }, f)), go,
    el("button", { type: "button", class: "btn", id: "unlock-cancel", onclick: () => onCancel() }, "Cancel"));
  node.style.zIndex = "100";                          // above any question of the frame
  doc.body.append(node);
  if (body.scrollHeight > body.clientHeight + 1) more.hidden = false;
  const enable = setTimeout(() => { if (!used) go.disabled = false; }, delayMs);
  return { close() { clearTimeout(enable); node.remove(); } };
}

// ---- an assertion for a refusal -----------------------------------------------------------------------------------
// refusal: {code: "assertion_required" | "lease_required", meta: the refusal's signed fields, rid: the refused request (hex)}.
// session: DeviceSession (workspace, deviceIdBytes, offsetMs, now) plus .credentialId (b64u, from the pairing).
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
  if (!view.ok) throw Object.assign(fail("bad_request"), { text: UNLOCK_TEXT.too_long });
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
      let done = false, sheet = null, timer = null;
      const onAbort = () => finish(no("cancelled"));
      const finish = (r) => { if (done) return; done = true; clearTimeout(timer); signal?.removeEventListener("abort", onAbort); sheet?.close(); resolve(r); };
      signal?.addEventListener("abort", onAbort, { once: true });
      const facts = [`${p.view.lines} line${p.view.lines === 1 ? "" : "s"}, ${p.view.chars} characters`, `Needs: ${p.scope}`, `Expires at ${new Date(p.expiresAt).toLocaleTimeString()}`,
        ...(p.subject.digest ? [`Check these 8 characters on the computer: ${p.subject.digest.slice(0, 8)}`] : []),
        "Confirm with Face ID, Touch ID, Windows Hello or your device PIN. Never your passphrase."];
      sheet = draw({ title: p.purpose === "lease" ? "Confirm to type for 15 minutes" : "Confirm this action", text: p.view.text, tail: p.view.lines > 1 || p.view.chars > TAIL_CHARS ? p.view.tail : "", facts, delayMs,
        onCancel: () => finish(no("cancelled")),
        onConfirm: () => {
          assert(hexToBytes(refusal.rid), p.options, win).then((fields) => {
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
// send(meta) -> the host's answer meta, or null (no answer, a refusal). gate(): resolves on the person's click (Safari
// wants the ceremony to follow a gesture); omitted in tests. Needs a platform authenticator; without one the browser
// still pairs, but the sentence for `no_platform` must be shown.
export async function registerCredential({ session, label, send, gate, win = globalThis }) {
  try {
    if (!await win.PublicKeyCredential?.isUserVerifyingPlatformAuthenticatorAvailable?.()) return no("no_platform");
    await gate?.();       // the person's click first: the host's 120 s registration clock starts after it, and create() follows at once
    const begin = await send({ op: "credential_begin" });
    if (!begin || !HEX64.test(begin.nonce || "") || !Number.isSafeInteger(begin.expires_ms)) return no("refused");
    const challenge = await registrationChallenge({ workspace: hexToBytes(session.workspace), deviceId: session.deviceIdBytes,
      expiresMs: begin.expires_ms, nonce: hexToBytes(begin.nonce) });
    const fields = await register(registrationOptions({ challenge, deviceId: session.deviceIdBytes, label }), win);
    const fin = await send({ op: "credential_finish", ...fields });
    if (fin?.registered !== true) return no("refused");
    return { ok: true, credentialId: fields.credential_id, synced: fin.synced === true };
  } catch (e) {
    return no(e?.name === "TimeoutError" ? "timeout" : e?.name === "NotAllowedError" || e?.name === "AbortError" ? "cancelled" : "failed");
  }
}
