// fileshare/static/js/mirror-crypto.js — mirrors and decisions (TIX on orch-core). Same bytes as
// sharing.py: seal(key, canonicalJson(obj), AAD). Vectors: tests/vectors/mirror1.json.
//   space label: MK,  AAD "sharing/space/v1|" + space id (ASCII)
//   mirror doc:  DEK, AAD "sharing/mirror/v1|" + ticket uuid (16 bytes); DEK wrapped under MK (tdek AAD)
//   decision:    DEK (or MK for a space-scoped ticket_request), AAD "sharing/decision/v1|" + scope + "|" + uuid
import { aadTdek, b64u, canonicalJson, hexToBytes, IntegrityError, open, seal, unb64u } from "./crypto.js";

const te = new TextEncoder();
const td = new TextDecoder();
const cat = (...parts) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let i = 0;
  for (const p of parts) { out.set(p, i); i += p.length; }
  return out;
};

export const aadSpace = (spaceId) => cat(te.encode("sharing/space/v1|"), te.encode(spaceId));
export const aadMirror = (ticketUuid) => cat(te.encode("sharing/mirror/v1|"), ticketUuid);
export const aadDecision = (scope, decisionUuid) => cat(te.encode("sharing/decision/v1|"), scope, te.encode("|"), decisionUuid);

function json(bytes, what) {
  let obj;
  try {
    obj = JSON.parse(td.decode(bytes));
  } catch {
    throw new IntegrityError(`${what} is not valid JSON`);
  }
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) throw new IntegrityError(`${what} is not a JSON object`);
  return obj;
}

// {doc, dek}: dek is the raw 32-byte ticket key (seal/open import it as needed).
export async function openMirror(mkKey, m) {
  let tu, wrapped, enc;
  try {
    tu = hexToBytes(m.uuid);
    wrapped = unb64u(m.wrapped_dek);
    enc = unb64u(m.enc_content);
  } catch {
    throw new IntegrityError("malformed mirror record");
  }
  if (tu.length !== 16) throw new IntegrityError("malformed mirror uuid");
  const dek = await open(mkKey, wrapped, aadTdek(tu));
  const doc = json(await open(dek, enc, aadMirror(tu)), "mirror");
  return { doc, dek };
}

export async function openSpaceLabel(mkKey, space) {
  const obj = json(await open(mkKey, unb64u(space.enc_label), aadSpace(space.id)), "space label");
  if (typeof obj.label !== "string") throw new IntegrityError("space label has the wrong shape");
  return obj.label;
}

// The mirror uuid of a link: sha256("<space>|<local key>|<gen>")[:16], hex (sharing.mirror_uuid, orch-tix).
export async function mirrorUuid(space, key, gen) {
  const digest = new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", te.encode(`${space}|${key}|${gen}`)));
  return Array.from(digest.subarray(0, 16), (b) => b.toString(16).padStart(2, "0")).join("");
}

// Whether the server's cleartext routing (row.uuid, row.space) is the mirror the sealed doc names: the doc
// carries its local key and link generation, so a server that moves a mirror to another space, or hands one
// ticket's row with another's content, is noticed (final review I3). A doc without gen proves nothing.
export async function boundToRow(row, doc) {
  if (!doc || typeof doc.id !== "string" || !Number.isInteger(doc.gen) || doc.gen < 1) return false;
  if (typeof row?.space !== "string" || typeof row?.uuid !== "string") return false;
  return (await mirrorUuid(row.space, doc.id, doc.gen)) === row.uuid;
}

export const aadMsg = (msgUuid) => cat(te.encode("sharing/msg/v1|"), msgUuid);

// A message from /api/messages: sealed under MK with AAD "sharing/msg/v1|" + its uuid (sharing.seal_msg).
export async function openMessage(mkKey, msg) {
  let uuid, env;
  try {
    uuid = hexToBytes(msg.uuid);
    env = unb64u(msg.enc_body);
  } catch {
    throw new IntegrityError("malformed message record");
  }
  if (uuid.length !== 16) throw new IntegrityError("malformed message uuid");
  return json(await open(mkKey, env, aadMsg(uuid)), "message");
}

// A ticket request is scoped to its space and sealed under MK (no ticket, so no DEK), as the CLI does.
export async function sealTicketRequest(mkKey, space, decisionUuidHex, body, opts = {}) {
  return sealDecision(mkKey, te.encode(space), hexToBytes(decisionUuidHex), body, opts);
}

// scope: the 16-byte ticket uuid, or the ASCII space id for a ticket_request. Returns base64url.
export async function sealDecision(key, scope, decisionUuid, body, { nonce } = {}) {
  return b64u(await seal(key, te.encode(canonicalJson(body)), aadDecision(scope, decisionUuid), nonce));
}

export async function openDecision(key, scope, decisionUuid, enc) {
  return json(await open(key, unb64u(enc), aadDecision(scope, decisionUuid)), "decision");
}
