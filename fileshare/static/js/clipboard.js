// Copy to clipboard (spec §14 D). Not an entry module. Both functions must be called synchronously
// from the click handler: they start the clipboard write before their first await, so the user
// gesture still counts. Pass already-decrypted bytes where there are some; copyImage also takes a
// Promise of bytes (a fresh decrypt) and hands it to ClipboardItem as a promise, which is the form
// Safari needs to keep the gesture.
import { toast } from "./ui.js";

export const UNAVAILABLE = "Copy isn't available in this browser";
const REFUSED = "Couldn't copy — the browser refused";

function clipboard() {
  return globalThis.navigator?.clipboard ?? null;
}

function settle(p) {
  return Promise.resolve(p).then(
    () => { toast("Copied"); return true; },
    () => { toast(REFUSED, "error"); return false; },
  );
}

export function copyText(text) {
  const cb = clipboard();
  if (!cb || typeof cb.writeText !== "function") {
    toast(UNAVAILABLE, "error");
    return Promise.resolve(false);
  }
  let p;
  try {
    p = cb.writeText(String(text));
  } catch (err) {
    p = Promise.reject(err);
  }
  return settle(p);
}

// PNG passes through; any other image type is drawn onto a detached <canvas> and re-encoded.
export async function toPngBlob(bytes, mime) {
  if (mime === "image/png") return new Blob([bytes], { type: "image/png" });
  const bitmap = await createImageBitmap(new Blob([bytes], { type: mime }));
  try {
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    canvas.getContext("2d").drawImage(bitmap, 0, 0);
    return await new Promise((resolve, reject) => {
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("canvas.toBlob gave nothing"))), "image/png");
    });
  } finally {
    bitmap.close?.();
  }
}

export function copyImage(bytes, mime) {
  const cb = clipboard();
  if (!cb || typeof cb.write !== "function" || typeof globalThis.ClipboardItem !== "function") {
    toast(UNAVAILABLE, "error");
    return Promise.resolve(false);
  }
  const png = Promise.resolve(bytes).then((b) => toPngBlob(b, mime));
  png.catch(() => {}); // the write below reports the failure; don't leave this one unhandled
  let p;
  try {
    p = cb.write([new globalThis.ClipboardItem({ "image/png": png })]);
  } catch (err) {
    p = Promise.reject(err);
  }
  return settle(p);
}
