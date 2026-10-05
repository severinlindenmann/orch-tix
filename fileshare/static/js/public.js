// The public-link viewer at /p/<token>#<link key> (spec §17). Entry module of public.html: no
// session, no service worker, no IndexedDB, no login link. It imports only the crypto, the DOM
// helpers and the renderers (render.js), never the app's session modules (api.js, keystore.js).
// The key is read from the fragment and the fragment is removed from the address bar before
// anything else happens, so it is in no request, no Referer and no later copy of the URL; the key
// stays in this module's memory only.
//
// Everything decrypted from the metadata is untrusted: it reaches the DOM as text (el()), and
// Markdown goes through the app's own sanitised, sandboxed renderer (render.js).
import { decryptBlob, hexToBytes, openPublicFile, parseLinkKey } from "./crypto.js";
import { el, hydrateIcons, icon, toast } from "./ui.js";
import { expiryPhrase, humanSize, plainSize, relTime, safeDownloadName } from "./format.js";
import { previewKind } from "./previewkind.js";
import { AUDIO_PREVIEW_MAX, PREVIEW_MAX, renderKind, saveBytes } from "./render.js";
import { copyText } from "./clipboard.js";

const TOKEN_RE = /^\/p\/([A-Za-z0-9_-]{43})$/;
const FALLBACK_NAME = "shared-file";

export const MSG = {
  damaged: "This link is incomplete or damaged",
  gone: "This link has expired or was revoked",
  busy: "Too many requests — try again in a minute.",
  offline: "Couldn't reach the server — try again.",
};

export function readToken(pathname) {
  const m = TOKEN_RE.exec(String(pathname ?? ""));
  return m ? m[1] : null;
}

// The link key from location.hash ("#…" or without the "#"), or null unless it is exactly 32 bytes of
// canonical base64url.
export function readLinkKey(hash) {
  if (typeof hash !== "string") return null;
  const frag = hash.startsWith("#") ? hash.slice(1) : hash;
  if (!frag) return null;
  try {
    return parseLinkKey(frag);
  } catch {
    return null;
  }
}

// null for an unlimited link; otherwise how many downloads are left. Anything that isn't null counts
// as limited, so a malformed answer never makes the page spend a download on its own.
export function downloadsLeft(pub) {
  const n = pub?.downloads_left;
  if (n === null || n === undefined) return null;
  return Number.isInteger(n) && n >= 0 ? n : 0;
}

export function previewLabel(left) {
  return `Preview (uses 1 of ${left} ${left === 1 ? "download" : "downloads"} left)`;
}

class LinkGone extends Error {}
class HttpError extends Error {
  constructor(status) {
    super(`HTTP ${status}`);
    this.status = status;
  }
}

async function get(path) {
  let res;
  try {
    res = await fetch(path, { credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer", redirect: "error" });
  } catch {
    throw new HttpError(0);
  }
  if (!res.ok) {
    await res.text().catch(() => ""); // finish the answer (its text is never shown)
    throw res.status === 404 ? new LinkGone() : new HttpError(res.status);
  }
  return res;
}

// The server's answers keep their own message. Every other failure (a wrong key, a bad uuid, JSON
// that doesn't parse, a tampered meta or blob) is the link being incomplete or damaged; nothing is
// logged, because the details would only describe the attacker's input.
function messageFor(err) {
  if (err instanceof LinkGone) return MSG.gone;
  if (err instanceof HttpError && err.status === 429) return MSG.busy;
  if (err instanceof HttpError) return MSG.offline;
  return MSG.damaged;
}

function stem(name) {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(0, dot) : name;
}

// The transcript card, as in the file view (spec §14 G), built from the untrusted meta as text.
function transcriptCard(meta) {
  const t = meta.transcript;
  const info = [t.model, t.language, `by ${t.by || "?"}`, relTime(t.created_at)].filter(Boolean).join(" · ");
  const md = `# ${meta.name}\n\n${t.text}`;
  return el("section", { class: "card transcript", "aria-label": "Transcript" },
    el("div", { class: "transcript-head" },
      el("div", { class: "transcript-titles" },
        el("h3", { class: "transcript-title" }, "Transcript"), el("span", { class: "transcript-meta" }, info)),
      el("button", {
        class: "icon-btn icon-btn-line", type: "button", "aria-label": "Copy transcript", title: "Copy transcript",
        onClick: () => copyText(t.text),
      }, icon("copy"))),
    el("div", { class: "transcript-text" }, t.text),
    el("button", {
      class: "link-btn", type: "button",
      onClick: () => saveBytes(safeDownloadName(`${stem(meta.name)}-transcript.md`, FALLBACK_NAME), new TextEncoder().encode(md)),
    }, "Download as .md"));
}

function boot() {
  // First: take the key out of the address bar (and so out of history, bookmarks and screenshots).
  const lk = readLinkKey(location.hash);
  history.replaceState(null, "", location.pathname);
  // Pasting the link again into this tab only changes the fragment, which reloads nothing: start over.
  window.addEventListener("hashchange", () => location.reload());
  const token = readToken(location.pathname);
  const body = document.getElementById("public-body");
  hydrateIcons();

  const fail = (text) => {
    body.replaceChildren(el("div", { class: "pub-state", role: "alert" },
      el("span", { class: "pub-state-icon", "aria-hidden": "true" }, icon("alert")),
      el("h1", { class: "pub-state-title" }, text)));
  };

  if (!token || !lk) {
    fail(MSG.damaged);
    return;
  }
  show(token, lk, body, fail).catch((err) => fail(messageFor(err)));
}

async function show(token, lk, body, fail) {
  const pub = await (await get(`/api/public/${token}`)).json();
  const { meta, dek } = await openPublicFile(lk, pub);
  const uuid = hexToBytes(pub.uuid);
  const left = downloadsLeft(pub);

  // Every blob fetch counts as a download (§17), so preview and Download share one fetch, made the
  // first time either needs it; the plaintext is kept in memory for this page only.
  let plain = null;
  const bytes = () => {
    plain ??= get(`/api/public/${token}/blob`)
      .then((r) => r.arrayBuffer())
      .then((buf) => decryptBlob(dek, uuid, new Uint8Array(buf)));
    plain.catch(() => { plain = null; }); // a failure can be retried
    return plain;
  };

  const size = plainSize(pub.size);
  const facts = [humanSize(size), expiryPhrase(pub.expires_at)].filter(Boolean).join(" · ");
  const preview = el("div", { class: "pub-preview" });
  const download = el("button", { type: "button", class: "btn btn-accent btn-block btn-big pub-download" }, icon("download"), "Download");
  body.replaceChildren(...[
    el("div", { class: "pub-head" },
      el("h1", { class: "pub-name" }, meta.name),
      el("p", { class: "pub-facts" }, facts)),
    meta.note ? el("p", { class: "pub-note" }, meta.note) : null,
    preview,
    meta.transcript ? transcriptCard(meta) : null,
    download].filter(Boolean));

  download.addEventListener("click", async () => {
    download.disabled = true;
    try {
      saveBytes(safeDownloadName(meta.name, FALLBACK_NAME), await bytes());
    } catch (err) {
      toast(messageFor(err), "error");
    } finally {
      download.disabled = false;
    }
  });

  const kind = previewKind(meta);
  if (kind.kind === "none") {
    preview.replaceChildren(el("p", { class: "preview-msg" }, "No preview for this file type — Download it instead."));
    return;
  }
  const audio = kind.kind === "audio";
  if (size > (audio ? AUDIO_PREVIEW_MAX : PREVIEW_MAX)) {
    preview.replaceChildren(el("p", { class: "preview-msg" }, `Too large to preview (> ${audio ? 50 : 2} MiB) — Download`));
    return;
  }

  const render = async () => {
    preview.replaceChildren(el("p", { class: "preview-msg" }, "Decrypting…"));
    let data;
    try {
      data = await bytes();
    } catch (err) {
      if (err instanceof HttpError) {
        preview.replaceChildren(el("p", { class: "preview-msg error", role: "alert" }, messageFor(err)));
      } else {
        fail(messageFor(err));
      }
      return;
    }
    try {
      // The image and audio blob URLs are never revoked: they live exactly as long as this page,
      // which shows one file and holds nothing else.
      preview.replaceChildren(renderKind(kind, data, () => {}));
    } catch {
      preview.replaceChildren(el("p", { class: "preview-msg" }, "No preview available — Download it instead."));
    }
  };

  if (left === null) {
    await render(); // unlimited: previewing costs nothing
    return;
  }
  // A limited link: the preview spends a download, so it waits to be asked for. Download reuses it.
  const ask = el("button", { type: "button", class: "btn btn-block pub-preview-btn" }, icon("eye"), previewLabel(left));
  ask.addEventListener("click", render, { once: true });
  preview.replaceChildren(ask);
}

if (typeof document !== "undefined" && document.getElementById("public-body")) boot();
