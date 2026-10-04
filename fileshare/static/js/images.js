// fileshare/static/js/images.js — the pinned images of a ticket on the phone (orch-core artifacts pinned by sha256).
// A ticket's sealed mirror names each image with the sha256 core pinned and, when the desktop shared it, the FILE it
// travels in (ticket-card.js pinnedImages). The phone downloads that FILE, decrypts it like any file, and shows it only
// when the bytes hash to the pinned sha256, the file is the named one and its type is a raster image. Anything else
// (a mismatch, a missing or deleted file, offline) shows the alt text: never an unverified image, never a URL from the
// ticket text. Verified images are kept as blob URLs for the page's life; the server's size is never trusted (a
// missing or too large size is refused, the download is cut at the cap, the plaintext length must fit exactly).
import { api } from "./api.js";
import { el } from "./ui.js";
import { decryptBlob, hexToBytes } from "./crypto.js";
import { cipherSize } from "./format.js";
import { resolveRow } from "./preview.js";

const RASTER = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
const MAX_BYTES = 8 * 1024 * 1024;
const MAX_CIPHER = cipherSize(MAX_BYTES);
const cache = new Map();          // `${file}|${name}|${sha256}` -> Promise<string> (a verified blob URL); failures are not kept

async function sha256Hex(bytes) {
  const d = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return Array.from(d, (b) => b.toString(16).padStart(2, "0")).join("");
}

// The ciphertext of a FILE, read as a stream and abandoned as soon as it passes `cap` bytes (the server's size is not
// trusted: a missing, non-numeric or too large size is refused before any byte is read).
export async function fetchCapped(row, cap = MAX_CIPHER) {
  const size = typeof row.size === "number" ? row.size : NaN;
  if (!Number.isInteger(size) || size <= 0 || size > cap) throw new Error("size refused");
  const res = await api("GET", `/api/files/${encodeURIComponent(row.id)}/blob`, { raw: true });
  const reader = res.body.getReader();
  const parts = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    got += value.byteLength;
    if (got > cap || got > size) {
      await reader.cancel().catch(() => {});
      throw new Error("too large");
    }
    parts.push(value);
  }
  if (got !== size) throw new Error("size mismatch");
  const out = new Uint8Array(got);
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.byteLength; }
  return out;
}

// The verified blob URL of a pinned image, or null. `img`: {name, sha256, file} from pinnedImages. A failure (a
// mismatch, offline, gone) is not remembered: the next render tries again.
export function verifiedImage(img) {
  const key = `${img.file}|${img.name}|${img.sha256}`;
  if (!cache.has(key)) {
    const p = (async () => {
      const row = await resolveRow(await api("GET", `/api/files/${encodeURIComponent(img.file)}`));
      if (row.meta?.name !== img.name || !RASTER.has(row.meta?.mime)) throw new Error("not this image");
      const blob = await fetchCapped(row);
      const bytes = await decryptBlob(row.dek, hexToBytes(row.uuid), blob);
      // the plaintext must be exactly what this ciphertext length holds, and within the cap
      if (bytes.byteLength > MAX_BYTES || cipherSize(bytes.byteLength) !== blob.byteLength) throw new Error("size mismatch");
      if ((await sha256Hex(bytes)) !== img.sha256) throw new Error("hash mismatch");
      return URL.createObjectURL(new Blob([bytes], { type: row.meta.mime }));
    })();
    cache.set(key, p);
    p.catch(() => cache.delete(key));
  }
  return cache.get(key).catch(() => null);
}

// A figure that shows the alt text at once and swaps in the image once it is verified. `cls` sizes it.
export function pinnedFigure(img, cls = "pin-img") {
  const alt = img.label || img.name;
  const box = el("figure", { class: `pin ${cls}`, dataset: { name: img.name, state: "pending" } },
    el("span", { class: "pin-alt" }, alt));
  verifiedImage(img).then((url) => {
    if (!url) { box.dataset.state = "unverified"; return; }
    box.dataset.state = "verified";
    box.replaceChildren(el("img", { src: url, alt, loading: "lazy", decoding: "async" }));
  });
  return box;
}

if (typeof window !== "undefined") {
  window.addEventListener("pagehide", () => {
    for (const p of cache.values()) p.then((u) => URL.revokeObjectURL(u), () => {});
    cache.clear();
  });
}
