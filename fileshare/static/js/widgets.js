// fileshare/static/js/widgets.js — ticket widgets in the ticket view (orch.widgets.v1; the pure side is
// widget-model.js). A ```orch fence in a section becomes a card the page owns: title, layer chip, source and the
// text alternative. Show draws the widget's document in a /sandbox/widget frame (sandbox="allow-scripts", opaque
// origin, its own CSP): the inline document, or the shared FILE fetched and decrypted like any file. The widget's
// HTML never enters this page; its messages are taken only from that frame's window with its nonce.
import { api } from "./api.js";
import { decryptBlob, hexToBytes } from "./crypto.js";
import { resolveRow } from "./preview.js";
import { el, shown } from "./ui.js";
import {
  BLOB_SLACK, MAX_FILE_BYTES, READY_MS, START_HEIGHT, openFrames, openKey, chipText, clampHeight, docSource, frameMessage, readCapped,
  sectionParts, verifyDoc, withNonce, withTheme,
} from "./widget-model.js";

const CHANGED = "changed since it was sent, not shown";
const dark = () => document.documentElement.dataset.theme === "dark"
  || (document.documentElement.dataset.theme !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);

function newNonce() {
  return Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("");
}

// The document's bytes, checked against the pin the desktop sent before anything is drawn: the size cap is
// enforced on the sealed blob before it is decrypted (the file row's size is only a claim), the digest on the
// decrypted bytes. A mismatch throws CHANGED and the card keeps its text.
async function loadDoc(src) {
  let bytes;
  if (src.inline) {
    bytes = new TextEncoder().encode(src.inline);
  } else {
    const file = await resolveRow(await api("GET", `/api/files/${encodeURIComponent(src.file)}`));
    if (Number(file.size) > MAX_FILE_BYTES) throw new Error("the widget document is too large");
    const res = await api("GET", `/api/files/${encodeURIComponent(file.id)}/blob`, { raw: true });
    const blob = await readCapped(res, MAX_FILE_BYTES + BLOB_SLACK);
    bytes = await decryptBlob(file.dek, hexToBytes(file.uuid), blob);
    if (bytes.length > MAX_FILE_BYTES) throw new Error("the widget document is too large");
  }
  if (!(await verifyDoc(bytes, src.sha256))) throw new Error(CHANGED);
  return new TextDecoder().decode(bytes);
}

function card(w) {
  const src = docSource(w);
  const text = el("pre", { class: "wcard-text" }, w.text || "No text alternative given");
  const box = el("div", { class: "wcard-frame", hidden: true });
  const status = el("span", { class: "wcard-status", role: "status", "aria-live": "polite" });
  const btn = src ? el("button", { type: "button", class: "btn wcard-btn" }, "Show") : null;
  let live = null;
  const key = openKey(w);

  const stop = (why = "") => {
    if (box.isConnected) openFrames.delete(key);   // a card already replaced by a re-render must not close its successor
    if (live) { live.cleanup(); live = null; }
    box.replaceChildren();
    box.hidden = true;
    box.classList.remove("is-loading");
    text.hidden = false;
    status.textContent = why;
    if (btn) { btn.textContent = "Show"; btn.disabled = false; }
  };

  const show = async () => {
    openFrames.add(key);
    btn.disabled = true;
    status.textContent = "Loading…";
    let html;
    try {
      html = await loadDoc(src);
    } catch (e) {
      stop(e?.message === CHANGED ? `This widget ${CHANGED}; showing the text`
        : `Couldn't load the widget: ${e?.detail || e?.message || "error"}`);
      return;
    }
    const nonce = newNonce();
    html = withNonce(html, nonce);
    const frame = el("iframe", { class: "wcard-iframe", sandbox: "allow-scripts", src: "/sandbox/widget",
      title: w.title || "Widget", referrerpolicy: "no-referrer", loading: "eager" });
    frame.style.height = `${START_HEIGHT}px`;
    const media = matchMedia("(prefers-color-scheme: dark)");
    const onTheme = () => frame.contentWindow?.postMessage({ orch: 1, kind: "theme", frame: nonce, dark: dark() }, "*");
    const onMessage = (event) => {
      if (!frame.isConnected) { live?.cleanup(); return; }
      if (event.source !== frame.contentWindow) return;
      const m = frameMessage(event.data, nonce);
      if (!m) return;
      if (m.kind === "ready") {
        clearTimeout(timer);
        box.classList.remove("is-loading");
        text.hidden = true;
        status.textContent = "";
        btn.textContent = "Stop";
        btn.disabled = false;
      } else if (m.kind === "resize") {
        frame.style.height = `${clampHeight(m.height)}px`;
      } else if (m.kind === "text" && typeof m.text === "string") {
        text.textContent = m.text.slice(0, 20000);
      } else if (m.kind === "error") {
        stop(`The widget failed: ${String(m.message || "error").slice(0, 200)}`);
      }
    };
    const timer = setTimeout(() => stop("The widget didn't start in time; showing the text"), READY_MS);
    live = { cleanup: () => { clearTimeout(timer); removeEventListener("message", onMessage); media.removeEventListener("change", onTheme); } };
    addEventListener("message", onMessage);
    media.addEventListener("change", onTheme);
    // /sandbox/widget loads, then writes the posted document into itself, which fires load once more (Chromium,
    // WebKit). A load after those two is the widget navigating its frame (a link, a form, location = …): that is not
    // the widget any more, so the frame goes. (ponytail: a browser that skips the second load lets one navigation
    // through unseen; the frame's CSP and sandbox still hold.)
    let loads = 0;
    frame.addEventListener("load", () => {
      loads += 1;
      if (loads > 2) { stop("The widget tried to leave the page; showing the text"); return; }
      if (loads > 1) return;
      frame.contentWindow.postMessage({ type: "tix-widget", html: withTheme(html, dark()), nonce }, "*");
    });
    box.hidden = false;                 // laid out (the frame's size is real), behind the text until ready
    box.classList.add("is-loading");
    box.replaceChildren(frame);
  };

  if (btn) btn.addEventListener("click", () => (live ? stop() : show()));
  if (btn && openFrames.has(key)) queueMicrotask(show);   // opened before this re-render: shown again, pin checked again
  return el("figure", { class: "wcard", id: `w-${w.key}`, dataset: { layer: w.layer || "" } },
    el("figcaption", { class: "wcard-head" }, w.title ? el("span", { class: "wcard-title" }, w.title) : null,
      el("span", { class: `wcard-chip${w.layer === "type" ? "" : " is-agent"}` }, chipText(w))),
    box, text,
    w.source ? el("p", { class: "wcard-source" }, `Source: ${w.source}`) : null,
    btn ? el("div", { class: "wcard-actions" }, btn, status) : null);
}

// A section's text with its widget blocks as cards (a fence nothing vouches for stays text). Returns at once with
// the text; the cards replace it when the fences have been matched to their entries by digest.
export function sectionBody(doc, name, text) {
  const box = el("div", { class: "section-text" }, shown(text));
  sectionParts(doc, name, text).then((parts) => {
    if (!parts.some((p) => p.widget)) return;
    box.className = "section-body";
    box.replaceChildren(...parts.map((p) => {
      if (p.kind === "text") return p.text.trim() ? el("div", { class: "section-text" }, shown(p.text.replace(/^\n+|\n+$/g, ""))) : null;
      return p.widget ? card(p.widget) : el("div", { class: "section-text" }, shown("```orch\n" + p.raw + "\n```"));
    }).filter(Boolean));
  }).catch(() => {});
  return box;
}
