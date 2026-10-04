// The public drop page at /u/<token>#<link pub> (upload-links spec §18, Task 5). Entry module of
// drop.html: no session, no service worker, no login link — a stranger with only this URL sends one
// file, several files, or a folder, which the browser zips (stored, uncompressed) and encrypts to
// the link's public key before it ever reaches the server. Everything the server knows about the
// link (`GET /api/public/u/<token>`) is untrusted only in the sense that it can be wrong or missing;
// it never carries key material, so nothing here needs the integrity checks public.js needs for a
// downloaded blob.
import { buildZip, zipPlainSize } from "./zip.js";
import { encryptBlob, newDropFileCrypto, unb64u } from "./crypto.js";
import { el, hydrateIcons, icon } from "./ui.js";
import { cipherSize, humanSize } from "./format.js";

const TOKEN_RE = /^\/u\/([A-Za-z0-9_-]{43})$/;

export function readDropToken(pathname) {
  const m = TOKEN_RE.exec(String(pathname ?? ""));
  return m ? m[1] : null;
}

// The link's public key from location.hash ("#…" or without the "#"): exactly 65 bytes of canonical
// base64url, an uncompressed P-256 point ([0] === 0x04). A stripped or mangled fragment (chat apps
// sometimes cut a message at "#") must never look like a usable key (Review Focus 4).
export function readDropKey(hash) {
  if (typeof hash !== "string") return null;
  const frag = hash.startsWith("#") ? hash.slice(1) : hash;
  if (!frag) return null;
  let bytes;
  try {
    bytes = unb64u(frag);
  } catch {
    return null;
  }
  if (bytes.length !== 65 || bytes[0] !== 4) return null;
  return bytes;
}

const pad2 = (n) => String(n).padStart(2, "0");

export function zipName(now = new Date()) {
  return `upload-${now.getFullYear()}${pad2(now.getMonth() + 1)}${pad2(now.getDate())}-`
    + `${pad2(now.getHours())}${pad2(now.getMinutes())}.zip`;
}

// files: [{ path, size }]. One file with no folder part uploads as itself; more than one file, or
// any path with a "/" in it (a folder pick), is zipped; nothing selected is refused, not zipped
// empty (Review Focus 5).
export function planUpload(files) {
  if (!files.length) return { kind: "empty", name: null };
  if (files.length === 1 && !files[0].path.includes("/")) return { kind: "single", name: files[0].path };
  return { kind: "zip", name: zipName() };
}

export const MSG = {
  incomplete: "This link is incomplete — ask the sender to copy the whole link, including the part after #.",
  gone: "This link has expired, was revoked or was already used.",
  busy: "Too many requests — try again in a minute.",
  offline: "Couldn't reach the server — try again.",
  empty: "Choose at least one file.",
  tooLarge: "These files are too large for this link.",
  badName: "One of the file names can't be sent — rename it and try again.",
  failed: "Upload failed — the link is still valid, try again.",
  sent: "Sent. You can close this tab.",
};

class LinkGone extends Error {}
// Exported for tests: failureMessage's mapping is otherwise only reachable through a live fetch/XHR
// or an actual bad zip path.
export class HttpError extends Error {
  constructor(status) {
    super(`HTTP ${status}`);
    this.status = status;
  }
}
// buildZip (or the path check it runs) rejected one of the selected names.
export class PayloadError extends Error {}

// The error-to-message mapping for a failed build/encrypt/POST (Review Focus, final pass):
// a 404 means the link is gone, 429 means the server is busy, 413 means the payload is over the
// link's limit, a bad path means one file's name can't be zipped, and everything else (a network
// error, status 0, or a 5xx) is the generic "try again" message — nothing here has claimed the
// link, so it stays exactly as usable as before (Review Focus 2).
export function failureMessage(err) {
  if (err instanceof PayloadError) return MSG.badName;
  if (err instanceof HttpError) {
    if (err.status === 404) return MSG.gone;
    if (err.status === 429) return MSG.busy;
    if (err.status === 413) return MSG.tooLarge;
  }
  return MSG.failed;
}

async function get(path) {
  let res;
  try {
    res = await fetch(path, { credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer", redirect: "error" });
  } catch {
    throw new HttpError(0);
  }
  if (!res.ok) {
    await res.text().catch(() => "");
    throw res.status === 404 ? new LinkGone() : new HttpError(res.status);
  }
  return res;
}

function messageFor(err) {
  if (err instanceof LinkGone) return MSG.gone;
  if (err instanceof HttpError && err.status === 429) return MSG.busy;
  return MSG.offline;
}

// ---- selection: the file picker, the folder picker (webkitdirectory) and drag-and-drop -----------

function pathOf(file) {
  return file.webkitRelativePath || file.name;
}

function filesFromInput(input) {
  return [...input.files].map((file) => ({ file, path: pathOf(file) }));
}

function readDirectory(entry) {
  return new Promise((resolve, reject) => {
    const reader = entry.createReader();
    const out = [];
    const next = () => reader.readEntries((batch) => {
      if (!batch.length) { resolve(out); return; }
      out.push(...batch);
      next();
    }, reject);
    next();
  });
}

async function walkEntry(entry) {
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    return [{ file, path: entry.fullPath.replace(/^\/+/, "") }];
  }
  if (entry.isDirectory) {
    const children = await readDirectory(entry);
    const lists = await Promise.all(children.map(walkEntry));
    return lists.flat();
  }
  return [];
}

// Drag-and-drop of files and folders: DataTransferItem.webkitGetAsEntry() walks directories. Falls
// back to the flat file list on a browser without that API (no folders, but still every file).
async function filesFromDataTransfer(dt) {
  const items = [...(dt?.items || [])].filter((i) => i.kind === "file");
  const entries = items.map((i) => (typeof i.webkitGetAsEntry === "function" ? i.webkitGetAsEntry() : null));
  if (items.length && entries.every(Boolean)) {
    const lists = await Promise.all(entries.map(walkEntry));
    return lists.flat();
  }
  return [...(dt?.files || [])].map((file) => ({ file, path: pathOf(file) }));
}

// ---- building and sending the payload --------------------------------------------------------

async function buildPayload(plan, selected) {
  if (plan.kind === "single") return new Uint8Array(await selected[0].file.arrayBuffer());
  const entries = await Promise.all(
    selected.map(async (it) => ({ path: it.path, data: new Uint8Array(await it.file.arrayBuffer()) })));
  try {
    return buildZip(entries);
  } catch (e) {
    throw new PayloadError(e.message);
  }
}

function mimeFor(plan, selected) {
  return plan.kind === "single" ? (selected[0].file.type || "application/octet-stream") : "application/zip";
}

// The ciphertext size the upload will actually have: the zip's own framing (for a multi-file plan)
// plus SHR1's header and per-chunk tag overhead — never just the sum of the plaintext file sizes.
function payloadCipherSize(plan, selected) {
  const plainSize = plan.kind === "single"
    ? selected[0].file.size
    : zipPlainSize(selected.map((it) => ({ path: it.path, size: it.file.size })));
  return cipherSize(plainSize);
}

// A multipart POST via XMLHttpRequest (for upload progress), not fetch: the meta part is JSON, the
// blob part is the ciphertext.
function postDrop(token, fc, keyVersion, ct, onProgress) {
  return new Promise((resolve, reject) => {
    const meta = JSON.stringify({ uuid: fc.uuidHex, key_version: keyVersion, sealed_dek: fc.sealedDek, enc_meta: fc.encMeta });
    const form = new FormData();
    form.append("meta", meta);
    form.append("blob", new Blob([ct]), "blob");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/public/u/${token}`);
    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
    });
    xhr.addEventListener("load", () => {
      if (xhr.status === 201) resolve();
      else reject(new HttpError(xhr.status));
    });
    xhr.addEventListener("error", () => reject(new HttpError(0)));
    xhr.addEventListener("timeout", () => reject(new HttpError(0)));
    xhr.send(form);
  });
}

// ---- the page ----------------------------------------------------------------------------------

function renderForm(token, linkPub, pub, body) {
  let selected = [];

  const fileInput = el("input", {
    type: "file", multiple: "", class: "sr-only", tabindex: "-1", "aria-hidden": "true", id: "drop-file-input",
  });
  const folderInput = el("input", {
    type: "file", webkitdirectory: "", class: "sr-only", tabindex: "-1", "aria-hidden": "true", id: "drop-folder-input",
  });
  const pick = el("button", { type: "button", class: "upload-pick" },
    icon("upload"), el("span", { class: "upload-pick-text" }, "Choose files or drag them here"));
  const pickFolder = el("button", { type: "button", class: "btn" }, "Choose a folder");
  const list = el("ul", { class: "upload-list", id: "drop-list" });
  const hint = el("p", { class: "preview-msg", role: "status", id: "drop-hint" }, MSG.empty);
  const note = el("textarea", {
    class: "input", rows: "2", maxlength: "500", placeholder: "Note (optional)", "aria-label": "Note", id: "drop-note",
  });
  const send = el("button", { type: "button", class: "btn btn-accent btn-block btn-big", id: "drop-send" }, "Send");
  const progress = el("progress", { class: "drop-progress", max: "100", value: "0", id: "drop-progress" });
  progress.hidden = true;
  const status = el("p", { class: "preview-msg", role: "status", id: "drop-status" });

  send.disabled = true;

  const refresh = () => {
    list.replaceChildren(...selected.map((it) =>
      el("li", { class: "upload-item" },
        el("div", { class: "upload-card" },
          el("div", { class: "upload-card-main" },
            el("span", { class: "upload-card-name" }, it.path),
            el("span", { class: "upload-size" }, humanSize(it.file.size)))))));
    pick.classList.toggle("has-files", selected.length > 0);
    pick.querySelector(".upload-pick-text").textContent = selected.length ? "Choose other files" : "Choose files or drag them here";

    const plan = planUpload(selected.map((it) => ({ path: it.path, size: it.file.size })));
    if (plan.kind === "empty") {
      send.disabled = true;
      hint.textContent = MSG.empty;
      hint.hidden = false;
    } else if (payloadCipherSize(plan, selected) > pub.max_bytes) {
      send.disabled = true;
      hint.textContent = `Too large — the limit is ${humanSize(pub.max_bytes)}.`;
      hint.hidden = false;
    } else {
      send.disabled = false;
      hint.hidden = true;
    }
    return plan;
  };

  fileInput.addEventListener("change", () => { selected = filesFromInput(fileInput); refresh(); });
  folderInput.addEventListener("change", () => { selected = filesFromInput(folderInput); refresh(); });
  pick.addEventListener("click", () => fileInput.click());
  pickFolder.addEventListener("click", () => folderInput.click());
  pick.addEventListener("dragover", (e) => { e.preventDefault(); pick.classList.add("is-drag"); });
  pick.addEventListener("dragleave", () => pick.classList.remove("is-drag"));
  pick.addEventListener("drop", async (e) => {
    e.preventDefault();
    pick.classList.remove("is-drag");
    selected = await filesFromDataTransfer(e.dataTransfer);
    refresh();
  });

  send.addEventListener("click", async () => {
    const plan = refresh();
    if (plan.kind === "empty" || send.disabled) return;
    send.disabled = true;
    fileInput.disabled = true;
    folderInput.disabled = true;
    progress.hidden = false;
    progress.value = 0;
    status.textContent = "Preparing…";
    try {
      const bytes = await buildPayload(plan, selected);
      status.textContent = "Encrypting…";
      const fc = await newDropFileCrypto(linkPub, pub.uuid, { name: plan.name, mime: mimeFor(plan, selected), note: note.value.trim() });
      const ct = await encryptBlob(fc.dek, fc.uuid, pub.key_version, bytes);
      status.textContent = "Uploading…";
      await postDrop(token, fc, pub.key_version, ct, (pct) => { progress.value = pct; });
      body.replaceChildren(el("div", { class: "pub-state" },
        el("span", { class: "pub-state-icon", "aria-hidden": "true" }, icon("check")),
        el("h1", { class: "pub-state-title" }, MSG.sent)));
    } catch (err) {
      // Review Focus 2: nothing here has claimed the link — a failed upload leaves it exactly as
      // usable as before, so the sender just tries again.
      status.textContent = failureMessage(err);
      progress.hidden = true;
      send.disabled = false;
      fileInput.disabled = false;
      folderInput.disabled = false;
    }
  });

  body.replaceChildren(el("div", { class: "drop-form" },
    el("div", { class: "pub-head" }, el("h1", { class: "pub-name" }, "Send files")),
    fileInput, folderInput,
    el("div", { class: "upload-pick-row" }, pick, pickFolder),
    list,
    hint,
    el("label", { class: "field" },
      el("span", { class: "label" }, "Note ", el("span", { class: "label-opt" }, "optional")), note),
    send,
    progress,
    status,
    el("p", { class: "sheet-lock" }, icon("lock"), "Encrypted on this device before it leaves")));
  refresh();
}

async function show(token, linkPub, body) {
  const pub = await (await get(`/api/public/u/${token}`)).json();
  renderForm(token, linkPub, pub, body);
}

function boot() {
  const token = readDropToken(location.pathname);
  const linkPub = readDropKey(location.hash);
  const body = document.getElementById("drop-body");
  hydrateIcons();

  const fail = (text) => {
    body.replaceChildren(el("div", { class: "pub-state", role: "alert" },
      el("span", { class: "pub-state-icon", "aria-hidden": "true" }, icon("alert")),
      el("h1", { class: "pub-state-title" }, text)));
  };

  // A stripped or mangled fragment must be refused before any request that uses the link, and
  // before anything is encrypted or sent (Review Focus 4): no form is shown at all.
  if (!token || !linkPub) {
    fail(MSG.incomplete);
    return;
  }
  show(token, linkPub, body).catch((err) => fail(messageFor(err)));
}

if (typeof document !== "undefined" && document.getElementById("drop")) boot();
