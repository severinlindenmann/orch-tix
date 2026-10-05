// The file view (spec §18): a full-screen page on a phone and the detail pane on a desktop, in the
// same #detail element. It shows the meta, the expiry pill, the preview or player (preview.js), the
// transcript card and the actions. Not an entry module: files.js opens it and owns the history.
// Decrypted strings only ever reach the DOM as text (el()), exactly as in the list.
import { api, ApiError } from "./api.js";
import { confirmDialog, el, icon, toast, wirePopover } from "./ui.js";
import { previewKind } from "./previewkind.js";
import { deviceLabel, expiryPhrase, humanSize, isExpiringSoon, isNew, plainSize, relTime, safeDownloadName } from "./format.js";
import { copyImage, copyText } from "./clipboard.js";
import { changeExpiry, setAck } from "./fileactions.js";
import {
  AUDIO_PREVIEW_MAX, DeletedError, PREVIEW_MAX, copyLabel, errorMessage, fetchPlaintext, logUnexpected, previewError, renderKind,
  resolveRow, saveBytes, transcriptSection,
} from "./preview.js";
import { fileTagsSection } from "./tags-ui.js"; // ---- tags (spec §19)
import { attachTranscribe, detachTranscribe } from "./autotranscribe.js"; // ---- transcription (Task 35)

// Hooks for the tasks that build on this view. Task 32 sets `shareLink = (file) => …`, which shows
// the "Share link" menu item and runs when it is chosen.
export const fileViewHooks = { shareLink: null };
// Task 32 (public links, linkshare.js): `linksSection(file, live)` returns the "Public links" node.
fileViewHooks.linksSection = null;

let view = null;
let placeholder = null;

const detailRoot = () => document.getElementById("detail");
const PHONE = "(max-width: 899px)";
// While the phone's full-screen view is open, everything behind it is inert and the view is a modal.
const BEHIND = [".list-col", "#dock", ".sidebar", ".m-head"];
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select, textarea, iframe, [tabindex]:not([tabindex="-1"])';

function setModal(root, on) {
  if (!root) return;
  if (on) {
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
  } else {
    root.removeAttribute("role");
    root.removeAttribute("aria-modal");
  }
  for (const sel of BEHIND) for (const n of document.querySelectorAll(sel)) n.inert = on;
}

// Applies or lifts the modal state for the current viewport (called on open, close and resize).
function syncModal() {
  setModal(detailRoot(), Boolean(view) && window.matchMedia(PHONE).matches);
}

// Keeps Tab inside the phone view: past the last control it wraps to the first, and back.
function trapTab(e) {
  if (e.key !== "Tab" || !view || view.root.getAttribute("aria-modal") !== "true") return;
  const items = [...view.root.querySelectorAll(FOCUSABLE)].filter((n) => n.checkVisibility?.() ?? n.offsetParent !== null);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  const inside = view.root.contains(document.activeElement);
  if (e.shiftKey && (document.activeElement === first || !inside)) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && (document.activeElement === last || !inside)) {
    e.preventDefault();
    first.focus();
  }
}

export function currentFile() {
  return view ? view.file : null;
}

export function closeFileView() {
  if (!view) return;
  const v = view;
  view = null;
  detachTranscribe(); // ---- transcription (Task 35): drop its hold on this view's plaintext
  if (v.url) URL.revokeObjectURL(v.url);
  v.menu.dispose();
  setModal(v.root, false);
  v.root.classList.remove("is-open");
  document.documentElement.classList.remove("fv-open");
  v.root.replaceChildren(...(placeholder ?? []));
  if (v.opener && v.opener.isConnected && v.restoreFocus) v.opener.focus({ preventScroll: true });
}

function btn(cls, iconName, label, attrs = {}) {
  return el("button", { type: "button", class: cls, ...attrs }, icon(iconName), el("span", { class: "btn-text" }, label));
}

function menuItem(label, iconName, attrs = {}) {
  return el("button", { type: "button", role: "menuitem", class: "menu-item", ...attrs }, icon(iconName), label);
}

function metaLine(f) {
  const size = f.size != null ? humanSize(plainSize(f.size)) : "";
  return [deviceLabel(f), f.project, relTime(f.created_at), size].filter(Boolean).join(" · ");
}

async function copyId(id) {
  try {
    await navigator.clipboard.writeText(id);
    toast(`Copied ${id}`);
  } catch {
    toast(`Couldn't copy. The ID is ${id}`, "error");
  }
}

// Opens `row` (a decoded row from files.js, or a raw FileOut) in #detail. `onClose` is how the view
// asks to be closed (the back button, Escape, after a delete); files.js passes one that also steps
// the history back. `focus` moves focus into the view (the phone's full-screen page).
export async function openFileView(row, { onClose = closeFileView, focus = true } = {}) {
  const opener = document.activeElement;
  closeFileView();
  const root = detailRoot();
  if (!root) return;
  if (!placeholder) placeholder = [...root.childNodes];
  const id = row.id;

  const back = el("button", { type: "button", class: "back-btn", "aria-label": "Back to files" },
    icon("back"), el("span", {}, "Files"));
  const more = el("button", {
    type: "button", class: "icon-btn more-btn", "aria-label": "More actions", "aria-haspopup": "menu",
    "aria-controls": "file-menu",
  }, icon("more"));
  const idBtn = el("button", { type: "button", class: "detail-id mono", "aria-label": `Copy ${id}`, title: "Copy ID" }, id);
  const name = el("h2", { class: "detail-name" }, row.ok && row.meta ? row.meta.name : "Encrypted file");
  const meta = el("div", { class: "detail-meta" }, metaLine(row));
  const expPill = el("button", { type: "button", class: "pill pill-exp exp-pill" }, icon("clock"), el("span", { class: "pill-text" }));
  const newPill = el("span", { class: "pill pill-new", hidden: true }, el("span", { class: "new-dot", "aria-hidden": "true" }), "New");
  const donePill = el("span", { class: "pill pill-done acked-line", hidden: true });
  const note = el("p", { class: "detail-note preview-note", hidden: true });
  const body = el("div", { class: "detail-preview preview-body" });
  const tagsBox = fileTagsSection(row); // ---- tags: chips (a tap filters), × and Edit tags

  const ack = btn("btn btn-accent act-ack", "check", "Mark as done", { dataset: { action: "ack" } });
  const copy = btn("btn act-copy d-only", "copy", "Copy", { hidden: true });
  const download = btn("btn act-download", "download", "Download", { disabled: true, "aria-label": "Download" });
  const del = btn("btn act-delete m-only", "trash", "Delete", { "aria-label": "Delete" });

  const mCopy = menuItem("Copy", "copy", { class: "menu-item m-only", hidden: true });
  const mCopyId = menuItem("Copy ID", "copy");
  const mExpiry = menuItem("Change expiry", "clock");
  const mShare = menuItem("Share link", "link", { hidden: !fileViewHooks.shareLink, dataset: { action: "share-link" } });
  const mDelete = menuItem("Delete", "trash", { class: "menu-item danger d-only" });
  const menuEl = el("div", { class: "menu", id: "file-menu", role: "menu", "aria-label": "More actions", hidden: true },
    mCopy, mCopyId, mExpiry, mShare, mDelete);

  root.replaceChildren(
    el("header", { class: "detail-bar" }, back, el("div", { class: "menu-wrap" }, more, menuEl)),
    el("div", { class: "detail-scroll" },
      el("div", { class: "detail-head" }, idBtn, name, meta),
      el("div", { class: "detail-pills" }, expPill, newPill, donePill, note),
      tagsBox,
      body),
    el("footer", { class: "detail-actions" }, ack, copy, download, del));
  root.classList.add("is-open");
  document.documentElement.classList.add("fv-open");

  const v = {
    root, file: row, url: null, bytes: null, opener, restoreFocus: focus,
    menu: wirePopover(more, menuEl),
  };
  view = v;
  syncModal();
  const live = () => view === v;

  const paint = () => {
    const f = v.file;
    const phrase = expiryPhrase(f.expires_at);
    expPill.hidden = !phrase;
    expPill.querySelector(".pill-text").textContent = phrase;
    expPill.classList.toggle("exp-soon", isExpiringSoon(f.expires_at));
    expPill.setAttribute("aria-label", `${phrase} — change expiry`);
    newPill.hidden = !isNew(f);
    donePill.hidden = !f.acked_at;
    donePill.textContent = f.acked_at ? `Done by ${f.acked_by || "someone"} · ${relTime(f.acked_at)}` : "";
    meta.textContent = metaLine(f);
    const acked = Boolean(f.acked_at);
    ack.querySelector(".btn-text").textContent = acked ? "Mark as not done" : "Mark as done";
    ack.className = acked ? "btn act-ack" : "btn btn-accent act-ack";
    ack.dataset.action = acked ? "unack" : "ack";
  };
  v.paint = paint;
  paint();

  back.addEventListener("click", () => onClose());
  idBtn.addEventListener("click", () => copyId(id));
  mCopyId.addEventListener("click", () => copyId(id));
  const expiry = () => changeExpiry(id, v.file.ok ? v.file.meta.name : "", v.file.expires_at);
  expPill.addEventListener("click", expiry);
  mExpiry.addEventListener("click", expiry);
  mShare.addEventListener("click", () => fileViewHooks.shareLink?.(v.file));
  ack.addEventListener("click", async () => {
    ack.disabled = true;
    try {
      const out = await setAck(id, !v.file.acked_at);
      if (out && live()) {
        v.file = { ...v.file, acked_at: out.acked_at, acked_by: out.acked_by };
        paint();
      }
    } finally {
      ack.disabled = false;
    }
  });
  const remove = () => deleteFile(v, onClose);
  del.addEventListener("click", remove);
  mDelete.addEventListener("click", remove);
  if (focus) back.focus({ preventScroll: true });

  if (row.deleted_at) {
    // A tombstone (e.g. from a ?f= link): nothing to decrypt and nothing to act on.
    name.textContent = "Deleted file";
    meta.textContent = "";
    for (const n of [expPill, newPill, donePill, root.querySelector(".detail-actions"), more, tagsBox]) n.hidden = true;
    body.replaceChildren(errorMessage(new DeletedError(id), id));
    return;
  }
  // ---- Task 32: the file's public links, below the preview (hidden while there are none).
  const links = fileViewHooks.linksSection?.(v.file, live);
  if (links) body.after(links);

  let file;
  try {
    file = await resolveRow(row);
  } catch (err) {
    if (live()) body.replaceChildren(errorMessage(err, id));
    return;
  }
  if (!live()) return;
  v.file = { ...v.file, ...file };
  name.textContent = file.meta.name;
  note.textContent = file.meta.note;
  note.hidden = !file.meta.note;
  download.disabled = false;
  download.addEventListener("click", async () => {
    download.disabled = true;
    try {
      v.bytes = v.bytes || (await fetchPlaintext(file));
      saveBytes(safeDownloadName(file.meta.name, id), v.bytes);
    } catch (err) {
      // A failed download leaves whatever the view shows (the rendered preview, a message) intact.
      logUnexpected(err, `downloading ${id}`);
      toast(previewError(err, id, "download").text, "error"); // also when the view was closed meanwhile
    } finally {
      download.disabled = false;
    }
  });

  const kind = previewKind(file.meta);
  if (kind.kind === "none") {
    body.replaceChildren(el("p", { class: "preview-msg" }, "No preview for this file type — Download it instead."));
    return;
  }
  const audio = kind.kind === "audio";
  // The transcript is metadata, so it shows even when the audio itself is too large to play here.
  const extra = audio ? transcriptSection(file) : null;
  // ---- transcription (Task 35): the Transcribe button, or a transcript too long to store
  if (extra) attachTranscribe(extra, { file, bytes: () => (live() ? v.bytes : null) });
  if (plainSize(file.size) > (audio ? AUDIO_PREVIEW_MAX : PREVIEW_MAX)) {
    body.replaceChildren(el("p", { class: "preview-msg" }, `Too large to preview (> ${audio ? 50 : 2} MiB) — Download`), extra ?? "");
    return;
  }
  const decrypting = el("p", { class: "preview-msg" }, "Decrypting…");
  body.replaceChildren(decrypting, extra ?? "");
  let bytes;
  try {
    bytes = await fetchPlaintext(file);
  } catch (err) {
    if (live()) decrypting.replaceWith(errorMessage(err, id));
    return;
  }
  if (!live()) return;
  v.bytes = bytes;
  let shown;
  try {
    shown = renderKind(kind, bytes, (url) => { v.url = url; });
  } catch (err) {
    body.replaceChildren(errorMessage(err, id)); // e.g. a vendor global (markdownit, DOMPurify) failed to load
    return;
  }
  body.replaceChildren(shown, extra ?? "");
  const label = copyLabel(kind);
  if (label) {
    // Built once the bytes are decrypted, so the click copies them synchronously inside the gesture.
    const doCopy = kind.kind === "image" ? () => copyImage(bytes, kind.type) : () => copyText(new TextDecoder("utf-8").decode(bytes));
    for (const b of [copy, mCopy]) {
      b.hidden = false;
      b.title = label;
      b.addEventListener("click", doCopy);
    }
  }
}

async function deleteFile(v, onClose) {
  const f = v.file;
  const name = f.ok && f.meta ? f.meta.name : "This encrypted file";
  const ok = await confirmDialog({
    title: `Delete ${f.id}?`,
    body: `${name} will be deleted. This can't be undone. The ID stays retired.`,
    confirmLabel: "Delete",
    danger: true,
  });
  if (!ok) return;
  try {
    await api("DELETE", `/api/files/${encodeURIComponent(f.id)}`);
    toast(`Deleted ${f.id}`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return;
    if (!(err instanceof ApiError && err.status === 410)) {
      toast(`Couldn't delete ${f.id}: ${err.message}`, "error");
      return;
    }
  }
  window.dispatchEvent(new CustomEvent("fs:file-deleted", { detail: { n: f.n, id: f.id } }));
  if (view === v) onClose();
}

if (typeof window !== "undefined") {
  // Ack and expiry changes arrive as the server's FileOut (fileactions.js): repaint the open file.
  window.addEventListener("fs:file-updated", (e) => {
    const file = e.detail?.file;
    if (!view || !file || file.n !== view.file.n) return;
    const { acked_at, acked_by, expires_at } = file;
    view.file = { ...view.file, acked_at, acked_by, expires_at };
    view.paint();
  });
  // ---- tags (spec §19): saved tags (tags-ui.js) follow into the open file.
  window.addEventListener("fs:file-tags", (e) => {
    if (view && e.detail && e.detail.n === view.file.n) view.file = { ...view.file, tags: e.detail.tags };
  });
  // Session gone: drop the decrypted preview and its blob URL with the keys.
  window.addEventListener("fs:unauthenticated", () => closeFileView());
  window.matchMedia(PHONE).addEventListener("change", syncModal);
  document.addEventListener("keydown", trapTab);
}
