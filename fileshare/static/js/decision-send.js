// fileshare/static/js/decision-send.js — sends decisions on one mirrored ticket (TIX on orch-core, spec §4.2):
// shared by the ticket page (ticket.js) and the one-tap answer on a Needs you card (needs.js). Each decision is
// built with the hash the phone saw (targetFor), signed first when this phone is paired with the space's desktop
// (pairing.js), sealed under the ticket DEK and posted; offline (or a 5xx) it waits in the outbox (kind
// "decision") and keeps its id, so a second tap or a retry is the same decision. Not an entry module.
import { api, ApiError } from "./api.js";
import { hexToBytes } from "./crypto.js";
import { deriveBrowserName } from "./browsername.js";
import { enqueueDecision } from "./outbox-ui.js";
import { sealDecision } from "./mirror-crypto.js";
import { pairingFor, signDecision } from "./pairing.js";
import { buildDecision, newDecisionId } from "./mirror-model.js";

const isoNow = () => new Date().toISOString().replace(/\.\d{3}Z$/, "Z");

// items: [{target, value}]. ids: slot -> decision id, kept by the caller until the decisions have gone out
// (filled in here). Resolves {queued, sent}; throws what the server refused (a 4xx) or an OutboxFullError.
export async function sendDecisions({ row, doc, kind, items, note = "", voice = null, ids = {} }) {
  const device = deriveBrowserName(navigator) || "phone";
  const pairing = await pairingFor(null, row.space).catch(() => null);
  let queued = false, sent = 0;
  for (const it of items) {
    const slot = `${kind}:${it.target.qid || it.target.gate || "verdict"}`;
    const decisionId = ids[slot] ||= newDecisionId();
    let body = buildDecision({ space: row.space, doc, kind, target: it.target, value: it.value, note,
      device, at: isoNow(), voice: voice?.file ? voice : null, decisionId });
    // A paired phone signs the decision before it is sealed (orch-core applies it directly);
    // without a pairing it goes unsigned and the desktop shows it for an Apply.
    if (pairing) body = await signDecision(pairing, body);
    const uuid = decisionId.slice(4);
    const enc = await sealDecision(row.dek, hexToBytes(row.uuid), hexToBytes(uuid), body);
    const req = { uuid, space: row.space, ticket: row.id, decisionKind: kind, key_version: row.key_version, body: enc };
    let offline = navigator.onLine === false;
    if (!offline) {
      try {
        await api("POST", "/api/decisions", { json: { uuid, space: row.space, ticket: row.id, kind, key_version: row.key_version, enc_body: enc } });
        sent += 1;
      } catch (e) {
        if (e instanceof ApiError && e.status === 409 && e.code === "duplicate_uuid") sent += 1;
        else if (e instanceof ApiError && (e.status === 0 || e.status >= 500)) offline = true;
        else throw e;
      }
    }
    if (offline) {
      await enqueueDecision(req);
      queued = true;
    }
  }
  return { queued, sent };
}
