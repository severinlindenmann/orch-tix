// Non-extractable CryptoKeys survive structured clone into IndexedDB with extractable=false intact.
import { KEYS, withStore } from "./db.js";

const SLOT = "current";

export async function saveKeys({ kek, mk, keyVersion }) {
  if (kek.extractable || mk.extractable) throw new Error("refusing to store extractable keys");
  await withStore(KEYS, "readwrite", (s) => s.put({ kek, mk, keyVersion }, SLOT));
}

export async function loadKeys() {
  return (await withStore(KEYS, "readonly", (s) => s.get(SLOT))) ?? null;
}

export async function clearKeys() {
  await withStore(KEYS, "readwrite", (s) => s.delete(SLOT));
}
