// fileshare/static/js/upload.js
import "./banner.js";
import { newFileCrypto, encryptBlob } from "./crypto.js";
import { ApiError } from "./api.js";
import { loadKeys } from "./keystore.js";
import { el, icon, toast } from "./ui.js";
import { cipherSize, humanSize } from "./format.js";
import { installGlobalPasteAndDrop, filesFromClipboardItems, fileFromText } from "./paste.js";
import { openRecorder } from "./recorder.js";
import { DEFAULT_TTL, TTL_OPTIONS } from "./fileactions.js";
import { enqueue, OutboxFullError, sendPrepared } from "./outbox-ui.js";
import { openNoteSheet } from "./note.js";
import { tagInput } from "./tags-ui.js"; // ---- tags (spec §19)
import { transcribeAfterUpload } from "./autotranscribe.js"; // ---- transcription (Task 35)

const MB = 1024 * 1024;
const SNIPPET_BYTES = 2000;
const SNIPPET_CHARS = 300;
let sheet = null;
let objectUrls = [];

export function maxUpload() {
  return Number(document.body.dataset.maxUpload) || 209715200;
}

// The "prepare" phase of an upload: all the crypto, nothing sent. The result is exactly the upload
// request the server takes (and all the outbox ever stores): no plaintext, no name.
export async function prepareUpload(file, keys, { note = "", ttl = DEFAULT_TTL, kind = "file", tags = [] } = {}) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const mime = file.type || "application/octet-stream";
  const fc = await newFileCrypto(keys.mk, { name: file.name, mime, note });
  const ct = await encryptBlob(fc.dek, fc.uuid, keys.keyVersion, bytes);
  bytes.fill(0);
  const blob = ct.byteOffset === 0 && ct.byteLength === ct.buffer.byteLength
    ? ct.buffer : ct.buffer.slice(ct.byteOffset, ct.byteOffset + ct.byteLength);
  return {
    uuid: fc.uuidHex, key_version: keys.keyVersion, wrapped_dek: fc.wrappedDek, enc_meta: fc.encMeta, ttl,
    blob, size: ct.byteLength, created_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"), kind,
    ...(tags.length ? { tags: [...tags] } : {}), // ---- tags (spec §19): cleartext, sent in the POST meta
  };
}

// Keeps a prepared upload in the encrypted outbox (spec §16). Resolves true when it was queued.
async function queue(prepared, name) {
  try {
    await enqueue(prepared, name);
    toast(`Saved ${name} — it uploads when you're back online`);
    return true;
  } catch (e) {
    if (e instanceof OutboxFullError) toast(`Couldn't save ${name}: ${e.message}`, "error");
    else {
      console.error("queueing an upload", e);
      toast(`Couldn't save ${name} for later`, "error");
    }
    return false;
  }
}

// `ttl` is "1d" | "7d" | "30d" | "never"; the recorder and other callers get the 7-day default.
// `kind` ("file", "paste", "recording", "note") is kept with a queued item, and so are `tags`.
// Every file is prepared (encrypted) first, then sent; offline, or when the send fails with a
// network error, the prepared request goes to the outbox instead. Resolves to the uploaded
// FileOuts plus {queued: true, uuid} for each queued one.
export async function uploadFiles(files, note = "", ttl = DEFAULT_TTL, { kind = "file", tags = [] } = {}) {
  const keys = await loadKeys();
  if (!keys) {
    window.dispatchEvent(new CustomEvent("fs:unauthenticated"));
    return [];
  }
  const done = [];
  let uploaded = 0;
  for (const file of files) {
    if (cipherSize(file.size) > maxUpload()) {
      toast(`${file.name} is ${(file.size / MB).toFixed(1)} MB — the limit is ${Math.floor(maxUpload() / MB)} MB`, "error");
      continue;
    }
    let prepared;
    try {
      toast(`Encrypting ${file.name}…`);
      prepared = await prepareUpload(file, keys, { note, ttl, kind, tags });
    } catch (e) {
      console.error("encrypting an upload", e);
      toast(`Couldn't encrypt ${file.name}`, "error");
      continue;
    }
    if (navigator.onLine === false) {
      if (await queue(prepared, file.name)) done.push({ queued: true, uuid: prepared.uuid });
      continue;
    }
    try {
      toast(`Uploading ${file.name}…`);
      const out = await sendPrepared(prepared);
      toast(`Uploaded ${out.id} · ${file.name}`, "ok");
      done.push(out);
      uploaded += 1;
      transcribeAfterUpload(out, file); // ---- transcription (Task 35): audio only, in the background
    } catch (e) {
      if (e instanceof ApiError && e.status === 0) {
        if (await queue(prepared, file.name)) done.push({ queued: true, uuid: prepared.uuid });
        continue;
      }
      if (e instanceof ApiError && e.status === 401) break;
      toast(e instanceof ApiError && e.status === 413 ? `${file.name} is too large for the server` : `Couldn't upload ${file.name}`, "error");
    }
  }
  if (uploaded) window.dispatchEvent(new CustomEvent("fs:files-changed"));
  return done;
}

function revokeUrls() {
  objectUrls.forEach((u) => URL.revokeObjectURL(u));
  objectUrls = [];
}

function closeSheet() {
  if (!sheet) return;
  revokeUrls();
  sheet.close();
  sheet.remove();
  sheet = null;
}

const isTextish = (f) => f.type.startsWith("text/") || f.type === "application/json";
const isImage = (f) => f.type.startsWith("image/") && f.type !== "image/svg+xml";

function previewFor(file) {
  // SVG can carry script; it gets the generic label like any other non-previewable type.
  if (isImage(file)) {
    const url = URL.createObjectURL(file);
    objectUrls.push(url);
    return el("img", { class: "upload-thumb", src: url, alt: "" });
  }
  const pre = el("pre", { class: "upload-snippet" }, isTextish(file) ? "" : file.type || "file");
  if (isTextish(file)) {
    file.slice(0, SNIPPET_BYTES).text()
      .then((t) => { pre.textContent = t.slice(0, SNIPPET_CHARS); })
      .catch(() => { pre.textContent = file.type; });
  }
  return pre;
}

function renamed(file, value) {
  const name = value.trim();
  if (!name || name === file.name) return file;
  return new File([file], name, { type: file.type, lastModified: file.lastModified });
}

// The expiry segment of the sheet (spec §18): 1 day / 7 days / 30 days / Never, 7 days pressed.
export function ttlSegment(value = DEFAULT_TTL, { id = "upload-ttl", labelId = "upload-ttl-label" } = {}) {
  let current = value;
  const buttons = TTL_OPTIONS.map(([v, label]) => el("button", {
    type: "button", class: "seg-btn", "aria-pressed": String(v === current), dataset: { ttl: v },
  }, label));
  const group = el("div", { class: "segmented", role: "group", "aria-labelledby": labelId, id }, buttons);
  for (const b of buttons) {
    b.addEventListener("click", () => {
      current = b.dataset.ttl;
      for (const x of buttons) x.setAttribute("aria-pressed", String(x === b));
    });
  }
  return { group, value: () => current };
}

// The upload bottom sheet (spec §18 "Share"): the picked files with an editable name each, a note,
// the expiry segment, then "Encrypt and share". Nothing is encrypted or sent before that click.
export function openUploadSheet(initialFiles = [], { kind = "file" } = {}) {
  closeSheet();
  let files = [...initialFiles];
  let chosenKind = kind;
  let names = [];
  const input = el("input", { type: "file", id: "upload-input", class: "sr-only", multiple: "", tabindex: "-1" });
  // A real button, so the picker is reachable by keyboard; the file input itself stays out of the tab order.
  const pick = el("button", { type: "button", class: "upload-pick", "aria-controls": "upload-input" },
    icon("upload"), el("span", { class: "upload-pick-text" }, "Choose files"));
  pick.addEventListener("click", () => input.click());
  const note = el("textarea", { id: "upload-note", class: "input", rows: "2", maxlength: "500", placeholder: "What should the agent do with it?" });
  const list = el("ul", { class: "upload-list" });
  const ttl = ttlSegment();
  const tagsIn = tagInput({ id: "upload-tags" }); // ---- tags (spec §19)
  const go = el("button", { class: "btn btn-accent btn-block btn-big", type: "button" }, "Encrypt and share");
  const close = el("button", { class: "close-btn", type: "button", "aria-label": "Close" }, icon("x"));
  const render = () => {
    revokeUrls();
    names = files.map((f, i) =>
      el("input", { class: "input upload-name", id: `upload-name-${i}`, type: "text", value: f.name, maxlength: "255", "aria-label": `Name of file ${i + 1}` }),
    );
    list.replaceChildren(
      ...files.map((f, i) =>
        el("li", { class: "upload-item" },
          el("div", { class: "upload-card" },
            el("span", { class: "upload-thumb-wrap" }, previewFor(f)),
            el("div", { class: "upload-card-main" },
              el("span", { class: "upload-card-name" }, f.name),
              el("span", { class: "upload-size" }, `${humanSize(f.size)}${f.type ? ` · ${f.type}` : ""}`))),
          el("label", { class: "field" }, el("span", { class: "label" }, files.length > 1 ? `Name ${i + 1}` : "Name"), names[i])),
      ),
    );
    pick.querySelector(".upload-pick-text").textContent = files.length ? "Choose other files" : "Choose files";
    pick.classList.toggle("has-files", files.length > 0);
    go.disabled = files.length === 0;
  };
  input.addEventListener("change", () => {
    files = [...input.files];
    chosenKind = "file";
    render();
  });
  sheet = el(
    "dialog",
    { class: "sheet upload-sheet", "aria-labelledby": "upload-title" },
    el("div", { class: "sheet-grip", "aria-hidden": "true" }),
    el("div", { class: "sheet-head" }, el("h2", { id: "upload-title" }, "Share"), close),
    list,
    input,
    pick,
    el("label", { class: "field" },
      el("span", { class: "label" }, "Note ", el("span", { class: "label-opt" }, "optional")), note),
    tagsIn.el,
    el("fieldset", { class: "field seg-field" },
      el("legend", { class: "label", id: "upload-ttl-label" }, "Expires"), ttl.group),
    go,
    el("p", { class: "sheet-lock" }, icon("lock"), "Encrypted on this device before it leaves"),
  );
  close.addEventListener("click", closeSheet);
  sheet.addEventListener("cancel", (e) => {
    // The file input's own "cancel" (the picker was dismissed) bubbles up to here: that must
    // leave the sheet open. Only the dialog's cancel (Escape) closes it.
    if (e.target !== sheet) return;
    e.preventDefault();
    closeSheet();
  });
  // A tap on the dimmed backdrop (outside the sheet's box) closes it, like the Close button.
  sheet.addEventListener("click", (e) => {
    if (e.target !== sheet) return;
    const r = sheet.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) closeSheet();
  });
  go.addEventListener("click", async () => {
    const tags = tagsIn.value(); // ---- tags: an invalid typed tag keeps the sheet open, with its error
    if (tags === null) return;
    const chosen = files.map((f, i) => renamed(f, names[i].value));
    const text = note.value.trim();
    const chosenTtl = ttl.value();
    closeSheet();
    await uploadFiles(chosen, text, chosenTtl, { kind: chosenKind, tags });
  });
  document.body.append(sheet);
  render();
  sheet.showModal();
  (files.length ? names[0] : pick).focus();
}

function openPasteFallback() {
  const ta = el("textarea", { id: "paste-text", class: "input", rows: "6", placeholder: "Long-press here and choose Paste" });
  const use = el("button", { class: "btn btn-accent", type: "button" }, "Use this text");
  const cancel = el("button", { class: "btn", type: "button" }, "Cancel");
  const dlg = el(
    "dialog",
    { class: "sheet paste-fallback", "aria-labelledby": "paste-title" },
    el("h2", { id: "paste-title" }, "Paste"),
    el("label", { for: "paste-text" }, "Text to share"),
    ta,
    el("div", { class: "sheet-actions" }, cancel, use),
  );
  const done = () => {
    dlg.close();
    dlg.remove();
  };
  cancel.addEventListener("click", done);
  dlg.addEventListener("cancel", (e) => {
    e.preventDefault();
    done();
  });
  use.addEventListener("click", () => {
    const f = fileFromText(ta.value);
    done();
    if (f) openUploadSheet([f], { kind: "paste" });
    else toast("Nothing to share — the text is empty");
  });
  document.body.append(dlg);
  dlg.showModal();
  ta.focus();
}

export async function pasteFromMenu() {
  if (navigator.clipboard && typeof navigator.clipboard.read === "function") {
    try {
      const files = await filesFromClipboardItems(await navigator.clipboard.read());
      if (files.length) {
        openUploadSheet(files, { kind: "paste" });
        return;
      }
    } catch {
      /* permission denied or unsupported type: fall back to the textarea */
    }
  }
  openPasteFallback();
}

// Records from the microphone and uploads straight away (no sheet): auto-upload is the point.
// Offline, or on a network error, the recording is queued like any upload and the recorder closes.
export function recordAudio() {
  return openRecorder({
    upload: (files, note) => uploadFiles(files, note, DEFAULT_TTL, { kind: "recording" }),
    maxBytes: maxUpload,
  });
}

export function newNote() {
  openNoteSheet({ upload: uploadFiles, ttlSegment, tagInput });
}

const ACTIONS = {
  upload: () => openUploadSheet(),
  paste: () => pasteFromMenu(),
  record: () => recordAudio(),
  note: () => newNote(),
};

// The Files page wires the dock, paste and drop. Other pages (Tickets' voice notes, spec T10) import
// uploadFiles only, and must not get the global paste and drop.
if (typeof document !== "undefined" && document.body?.classList.contains("page-files")) {
  // The action dock (spec §18): Upload, Paste, Record, Note.
  for (const b of document.querySelectorAll("[data-action]")) {
    const run = ACTIONS[b.dataset.action];
    if (run) b.addEventListener("click", run);
  }
  installGlobalPasteAndDrop((files, kind) => openUploadSheet(files, { kind }));
}
