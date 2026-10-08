// fileshare/static/js/remote-pair.js: the device's half of pairing with a workspace's computer (docs/bridge-protocol.md
// §8.1). The link is "/remote/pair#v1.<workspace>.<pairing id>.<S>.<host pin>"; the fragment is a secret, so it is read
// once and taken out of the address at once (readPairLink). Every answer to the `pair` request, the `pending` answer
// and every refusal, is opened with the §8.1 step 4 check before anything in it is used: tag, then the `host_pub` it
// carries against the link's pin, then the signature with that key, and only then §7 (openPairAnswer). The pin is stored
// only after a verified `pending` whose fingerprint is the one this device computed itself.
import { hexToBytes } from "./crypto.js";
import { DeviceSession } from "./bridge-session.js";
import {
  F_REFUSAL, MESSAGES, PAIR_ANSWER_MS, decodeHeader, deviceFingerprint, deviceId, hostKeyFromPin, importPublicKey, openBody,
  pairRequestMeta, parsePairFragment, signedBytes, splitEnvelope, unframe, verifySigned,
} from "./bridge-crypto.js";
import { deviceKey, forgetWorkspace, openWorkspaceKey, pinHost, saveCredential, workspaceRecord } from "./bridge-store.js";
import { registerCredential, unlockText } from "./unlock.js";
import { createMailbox } from "./remote-mailbox.js";
import { pairRefusalText } from "./remote-model.js";

export const STATUS_EVERY_MS = 2000;
export const OFFER_MS = 10 * 60_000;      // §8.1 step 1: the offer lives 10 minutes
const HEX130 = /^[0-9a-f]{130}$/;

// Takes the secret out of the address bar and the history entry BEFORE anything else runs, whether or not it parses.
export function readPairLink(win = window) {
  const hash = win.location.hash;
  if (hash) win.history.replaceState(null, "", win.location.pathname + win.location.search);
  return parsePairFragment(hash);
}

// §8.1 step 4. `hostPin` is the link's pin (bytes). Returns {result: "drop"} or the accepted answer from
// DeviceSession.receive plus {hostPub, fingerprint}. The session's host key is set only for the call, from the pin check.
export async function openPairAnswer(session, env, mailbox, hostPin) {
  const drop = { result: "drop" };
  let key;
  let meta, hostPub, refusal;
  try {
    const { header, body, sig } = splitEnvelope(env);
    refusal = !!(decodeHeader(header).flags & F_REFUSAL);
    ({ meta } = unframe(await openBody(session.kWs, header, body)));                 // tag first
    if (typeof meta.host_pub !== "string" || !HEX130.test(meta.host_pub)) return drop;
    if (refusal ? typeof meta.refusal !== "string" : meta.state !== "pending") return drop;
    hostPub = hexToBytes(meta.host_pub);
    key = await hostKeyFromPin(hostPub, hostPin);                                    // the pin, throws on a mismatch
    if (!await verifySigned(key, sig, signedBytes(header, body))) return drop;       // the signature, with that key
  } catch {
    return drop;
  }
  const before = session.hostKey;
  session.hostKey = key;
  try {
    const r = await session.receive(env, mailbox);                                   // then the rest of §7
    return r.result === "accept" ? { ...r, hostPub, fingerprint: refusal ? null : meta.fingerprint } : drop;
  } finally {
    session.hostKey = before;
  }
}

// Runs the whole ceremony up to "waiting for the owner". Dependencies are injectable for tests.
// events: onFingerprint(text) once the host's pending answer is verified, onState(text) for progress.
// Resolves {approved: true, scope} | {approved: false, why} .
export async function runPairing({ link, label, signal, now = Date.now, onFingerprint, onState, gate, onCredential,
  deps = {} }) {
  const { openKey = openWorkspaceKey, device = deviceKey, pin = pinHost, record = workspaceRecord, forget = forgetWorkspace,
    mailbox = createMailbox(link.workspace), sleep = (ms) => new Promise((r) => setTimeout(r, ms)), pollMs = STATUS_EVERY_MS,
    answerMs = PAIR_ANSWER_MS, offerMs = OFFER_MS } = deps;
  const hostPin = link.hostPin;
  const earlier = (await record(link.workspace))?.hostPub != null;      // paired before this link?
  const { kWs, keyVersion } = await openKey(link.workspace);
  const dk = await device();
  const dev = await deviceId(hexToBytes(link.workspace), dk.pub);
  const session = new DeviceSession({ workspace: link.workspace, kWs, keyVersion, deviceId: dev, signKey: dk.privateKey, hostKey: null });
  const myFingerprint = await deviceFingerprint(dk.pub);
  const meta = await pairRequestMeta({ link, pub: dk.pub, label });          // zeroes the secret

  // One exchange: send, wait for one answer through `open`, resend what the session asks to resend.
  async function ask(args, open, waitMs) {
    for (let round = 0; round < 3; round++) {
      let sent = await session.request(args);
      const got = new Promise((resolve) => {
        const listener = {
          chunk: async (c) => { const r = await open(c); if (r.result === "accept") resolve(r); },
          fail: () => resolve(null),
        };
        mailbox.post(sent.id, sent.envelope, false, listener).catch(() => resolve(null));
      });
      const timeout = new Promise((r) => setTimeout(() => r("timeout"), waitMs));
      const r = await Promise.race([got, timeout]);
      await mailbox.cancel(sent.id);
      if (r === "timeout" || r === null) return { silent: true };
      if (r.refusal && r.resend) { args = r.resend; continue; }
      return r;
    }
    return { silent: true };
  }

  // Pinned but never approved: forget the pin, so /remote does not show a workspace this browser cannot open.
  const undo = async (why) => {
    if (!earlier) await forget(link.workspace).catch(() => {});
    return { approved: false, why };
  };

  try {
    onState?.("Asking the computer…");
    const sentAt = now();
    const pending = await ask({ meta }, (c) => openPairAnswer(session, c.env, c.mailbox, hostPin), answerMs);
    if (pending.silent) return { approved: false, why: MESSAGES.usedElsewhere };      // §8.1: no answer within 60 s
    if (pending.refusal) return { approved: false, why: pairRefusalText(pending.meta.refusal) };
    if (pending.fingerprint !== myFingerprint) {
      return { approved: false, why: "The computer saw a different device key than this browser's. Do not approve it. Make a new link." };
    }
    await pin(link.workspace, pending.hostPub, hostPin);                          // the pin; throws on a mismatch
    session.hostKey = await importPublicKey(pending.hostPub);
    onFingerprint?.(myFingerprint);
    // §9.2: the platform credential is made now, before the owner approves, so the owner sees whether there is one.
    // gate() is the person's click (the ceremony follows a gesture). No credential: still paired, but no Type.
    const send = async (m) => { const a = await ask({ meta: m }, (c) => session.receive(c.env, c.mailbox), 15_000); return a.silent || a.refusal ? null : a.meta; };
    const cred = await (deps.register || registerCredential)({ session, label, send, gate });
    onCredential?.(cred.ok ? "" : unlockText(cred.reason === "no_platform" || cred.reason === "timeout" ? cred.reason : "refused"));
    onState?.("Waiting for you to approve this browser on the computer.");

    while (now() - sentAt < offerMs && !signal?.aborted) {
      await sleep(pollMs);
      const r = await ask({ meta: { op: "pair_status", pairing_id: meta.pairing_id } }, (c) => session.receive(c.env, c.mailbox), 15_000);
      if (r.silent || r.refusal) continue;
      const st = r.meta?.state;
      if (st === "approved") {
        if (cred.ok) await saveCredential(link.workspace, cred.credentialId);         // only once approved: a rejected pairing leaves nothing
        return { approved: true, scope: typeof r.meta.scope === "string" ? r.meta.scope : "", credential: cred.ok };
      }
      if (st === "rejected") return await undo("The computer rejected this browser.");
    }
    return await undo("The link expired. Make a new one on the computer.");
  } finally {
    mailbox.close();
  }
}
