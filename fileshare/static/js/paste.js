// fileshare/static/js/paste.js — no imports: node tests it, upload.js wires it to the page
const IMAGE_EXT = {
  "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
  "image/bmp": "bmp", "image/avif": "avif", "image/heic": "heic", "image/tiff": "tiff",
};
const TEXT_TYPE = { json: "application/json", md: "text/markdown", txt: "text/plain" };
// Browsers name clipboard bitmaps "image.png" (Chrome/Edge) or leave the name empty (Safari).
const GENERIC_IMAGE_NAME = /^image\.(png|jpe?g|gif|webp|bmp|avif|heic|tiff?)$/i;
const pad = (n) => String(n).padStart(2, "0");

export function stamp(d) {
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
}

export function guessTextExt(text) {
  const t = String(text).trim();
  if (t.startsWith("{") || t.startsWith("[")) {
    try {
      const v = JSON.parse(t);
      if (v !== null && typeof v === "object") return "json";
    } catch {
      /* not JSON */
    }
  }
  if (t.startsWith("#") || /^\s*```/m.test(t)) return "md";
  return "txt";
}

export function fileFromText(text, now = new Date()) {
  if (!text || !String(text).trim()) return null;
  const ext = guessTextExt(text);
  return new File([text], `pasted-${stamp(now)}.${ext}`, { type: TEXT_TYPE[ext], lastModified: now.getTime() });
}

function normalise(files, now) {
  let images = 0;
  return files.map((f) => {
    if (!f.type.startsWith("image/") || (f.name && !GENERIC_IMAGE_NAME.test(f.name))) return f;
    const ext = IMAGE_EXT[f.type] || "png";
    const name = `screenshot-${stamp(now)}${images ? `-${images + 1}` : ""}.${ext}`;
    images += 1;
    return new File([f], name, { type: f.type, lastModified: now.getTime() });
  });
}

export function filesFromClipboard(dataTransfer, now = new Date()) {
  if (!dataTransfer) return [];
  const fromItems = [...(dataTransfer.items || [])]
    .filter((i) => i.kind === "file")
    .map((i) => i.getAsFile())
    .filter(Boolean);
  const raw = fromItems.length ? fromItems : [...(dataTransfer.files || [])];
  if (raw.length) return normalise(raw, now);
  const text = typeof dataTransfer.getData === "function" ? dataTransfer.getData("text/plain") : "";
  const f = fileFromText(text, now);
  return f ? [f] : [];
}

export async function filesFromClipboardItems(items, now = new Date()) {
  const images = [];
  let text = "";
  for (const item of items || []) {
    const type = item.types.find((t) => t.startsWith("image/"));
    if (type) {
      images.push(new File([await item.getType(type)], "", { type }));
    } else if (!text && item.types.includes("text/plain")) {
      text = await (await item.getType("text/plain")).text();
    }
  }
  if (images.length) return normalise(images, now);
  const f = fileFromText(text, now);
  return f ? [f] : [];
}

export function isEditable(node) {
  if (!node) return false;
  return node.tagName === "INPUT" || node.tagName === "TEXTAREA" || node.tagName === "SELECT" || node.isContentEditable === true;
}

const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");

export function installGlobalPasteAndDrop(onFiles, doc = document, win = window) {
  doc.addEventListener("paste", (e) => {
    // A paste into a field (the upload note, the search box) stays a normal text paste, and a
    // paste while any dialog is open (upload sheet, onboarding, confirm) never starts an upload.
    if (isEditable(doc.activeElement) || isEditable(e.target) || doc.querySelector("dialog[open]")) return;
    const files = filesFromClipboard(e.clipboardData);
    if (!files.length) return;
    e.preventDefault();
    onFiles(files, "paste");
  });

  const overlay = doc.createElement("div");
  overlay.className = "drop-overlay";
  overlay.hidden = true;
  const inner = doc.createElement("div");
  inner.className = "drop-overlay-inner";
  inner.textContent = "Drop to encrypt & share";
  overlay.append(inner);
  doc.body.append(overlay);

  // dragenter/dragleave fire for every child element crossed; a counter tells "left the window" apart.
  let depth = 0;
  win.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth += 1;
    overlay.hidden = false;
  });
  win.addEventListener("dragover", (e) => {
    if (hasFiles(e)) e.preventDefault();
  });
  win.addEventListener("dragleave", (e) => {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) overlay.hidden = true;
  });
  win.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    overlay.hidden = true;
    const files = [...e.dataTransfer.files];
    if (files.length) onFiles(files, "file");
  });
  return overlay;
}
