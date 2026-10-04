// Recovery key: "shrk-" + 11 groups of 5 RFC 4648 base32 chars of MK || sha256(MK)[0:2] (spec §4.2).
const ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

async function checksum(mk) {
  return new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", mk)).subarray(0, 2);
}

function base32(bytes) {
  let bits = 0, value = 0, out = "";
  for (const b of bytes) {
    value = (value << 8) | b;
    bits += 8;
    while (bits >= 5) {
      out += ALPHABET[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
    value &= (1 << bits) - 1;
  }
  if (bits > 0) out += ALPHABET[(value << (5 - bits)) & 31];
  return out;
}

function unbase32(str, nBytes) {
  let bits = 0, value = 0;
  const out = new Uint8Array(nBytes);
  let o = 0;
  for (const ch of str) {
    const v = ALPHABET.indexOf(ch);
    if (v < 0) throw new Error("invalid recovery key");
    value = (value << 5) | v;
    bits += 5;
    if (bits >= 8) {
      if (o >= nBytes) throw new Error("invalid recovery key");
      out[o++] = (value >>> (bits - 8)) & 0xff;
      bits -= 8;
    }
    value &= (1 << bits) - 1;
  }
  if (o !== nBytes || value !== 0) throw new Error("invalid recovery key");
  return out;
}

export async function encodeRecovery(mkRaw) {
  if (!(mkRaw instanceof Uint8Array) || mkRaw.length !== 32) throw new TypeError("master key must be 32 bytes");
  const raw = new Uint8Array(34);
  raw.set(mkRaw, 0);
  raw.set(await checksum(mkRaw), 32);
  const s = base32(raw);
  const groups = [];
  for (let i = 0; i < s.length; i += 5) groups.push(s.slice(i, i + 5));
  return "shrk-" + groups.join("-");
}

export async function decodeRecovery(str) {
  let s = String(str).toUpperCase().replace(/[\s-]/g, "");
  if (s.startsWith("SHRK")) s = s.slice(4);
  if (s.length !== 55) throw new Error("invalid recovery key");
  const raw = unbase32(s, 34);
  const mk = raw.slice(0, 32);
  const want = await checksum(mk);
  if (raw[32] !== want[0] || raw[33] !== want[1]) throw new Error("recovery key checksum mismatch — check for a typo");
  return mk;
}
