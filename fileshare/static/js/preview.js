// Preview renderers (spec §9 screen 5), used by the file view (fileview.js). Decrypted text only ever
// reaches the DOM through textContent; the one HTML sink is iframe.srcdoc holding DOMPurify output
// inside a sandbox="" iframe.
import { IntegrityError, decryptBlob, hexToBytes, openFileMeta } from "./crypto.js";
import { api, ApiError } from "./api.js";
import { loadKeys } from "./keystore.js";
import { el, icon } from "./ui.js";
// The renderers moved to render.js (Task 32) so the public viewer can use them without this module's
// session imports; they are re-exported here unchanged for the file view.
import { saveBytes } from "./render.js";
export { AUDIO_PREVIEW_MAX, PREVIEW_MAX, renderKind, renderMarkdown, saveBytes } from "./render.js";
import { relTime, safeDownloadName } from "./format.js";
import { copyText } from "./clipboard.js";

// openFileMeta signals a tombstone with a plain Error; this names it so it isn't mistaken for a crash.
export class DeletedError extends Error {
  constructor(id) {
    super(`${id} was deleted`);
    this.name = "DeletedError";
  }
}

// What the file view's Copy does for a kind (spec §14 D), or null when the kind can't be copied.
export function copyLabel(kind) {
  return { markdown: "Copy markdown", json: "Copy text", text: "Copy text", image: "Copy image" }[kind.kind] ?? null;
}

function stem(name) {
  const base = String(name ?? "").split(/[\\/]/).pop();
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(0, dot) : base;
}

// The transcript card. The transcript lives in the audio file's encrypted metadata (spec §14 G) and
// is untrusted: every field reaches the DOM as text, and the text keeps its newlines (CSS pre-wrap).
export function transcriptSection(file) {
  const t = file.meta.transcript;
  const title = el("h3", { class: "transcript-title" }, "Transcript");
  if (!t) {
    return el("section", { class: "card transcript", "aria-label": "Transcript" },
      el("div", { class: "transcript-head" }, title),
      // Agents can't transcribe any more (spec §20); the file view adds its Transcribe button here.
      el("p", { class: "preview-msg transcript-none" }, "No transcript yet."));
  }
  const info = [t.model, t.language, `by ${t.by || "?"}`, relTime(t.created_at)].filter(Boolean).join(" · ");
  const md = `# ${file.meta.name}\n\n${t.text}`;
  return el("section", { class: "card transcript", "aria-label": "Transcript" },
    el("div", { class: "transcript-head" },
      el("div", { class: "transcript-titles" }, title, el("span", { class: "transcript-meta" }, info)),
      el("button", {
        class: "icon-btn icon-btn-line", type: "button", "aria-label": "Copy transcript", title: "Copy transcript",
        onClick: () => copyText(t.text),
      }, icon("copy"))),
    el("div", { class: "transcript-text" }, t.text),
    el("button", {
      class: "link-btn", type: "button",
      onClick: () => saveBytes(safeDownloadName(`${stem(file.meta.name)}-transcript.md`, file.id), new TextEncoder().encode(md)),
    }, "Download as .md"));
}

// Maps every failure the preview (or its Download button, verb "download") can hit to one
// user-facing line. `error` marks the red style.
export function previewError(err, id, verb = "preview") {
  if (err instanceof IntegrityError) {
    return { text: `Couldn't decrypt ${id} — the file may be corrupt or was encrypted with another key.`, error: true };
  }
  if (err instanceof DeletedError || (err instanceof ApiError && err.status === 410)) {
    return { text: `${id} was deleted.`, error: false };
  }
  if (err instanceof ApiError && err.status === 401) return { text: "Session expired — log in again.", error: false };
  if (err instanceof ApiError && err.status === 0) return { text: "Couldn't reach the server — try again.", error: true };
  if (err instanceof ApiError) return { text: `Couldn't load ${id}: ${err.message}`, error: true };
  return { text: `Couldn't ${verb} ${id}.`, error: true };
}

export function logUnexpected(err, what) {
  if (!(err instanceof IntegrityError) && !(err instanceof ApiError) && !(err instanceof DeletedError)) {
    console.error(what, err);
  }
}

export function errorMessage(err, id) {
  logUnexpected(err, `previewing ${id}`);
  const { text, error } = previewError(err, id);
  return el("p", { class: error ? "preview-msg error" : "preview-msg", role: "alert" }, text);
}

// A row from files.js is already decoded ({ok, meta, dek}); anything else is a raw FileOut to open here.
export async function resolveRow(row) {
  if (row.ok && row.meta && row.dek) return row;
  const keys = await loadKeys();
  if (!keys) throw new ApiError(401, "unauthenticated", "Session expired");
  try {
    const { meta, dek } = await openFileMeta(keys.mk, row);
    return { ...row, ok: true, meta, dek };
  } catch (err) {
    // IntegrityError: tampering or the wrong key. A plain Error on a keyless row: the tombstone.
    if (!(err instanceof IntegrityError) && (!row.wrapped_dek || !row.enc_meta)) throw new DeletedError(row.id);
    throw err;
  }
}

export async function fetchPlaintext(row) {
  const res = await api("GET", `/api/files/${encodeURIComponent(row.id)}/blob`, { raw: true });
  const blob = new Uint8Array(await res.arrayBuffer());
  // the ciphertext must be the size the file says it is (pinned images use images.js fetchCapped, which also caps it)
  if (Number.isInteger(row.size) && blob.byteLength !== row.size) throw new IntegrityError("size mismatch");
  return decryptBlob(row.dek, hexToBytes(row.uuid), blob);
}
