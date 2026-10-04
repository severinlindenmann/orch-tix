// The preview renderers (spec §9 screen 5), shared by the file view (through preview.js) and the
// public-link viewer (public.js). No session, no keys, no API: only DOM building from decrypted
// bytes. Decrypted text reaches the DOM through textContent; the one HTML sink is iframe.srcdoc
// holding DOMPurify output inside a sandbox="" iframe.
import { el, icon } from "./ui.js";

export const PREVIEW_MAX = 2 * 1024 * 1024;
// Audio is played, not parsed, and a voice memo is easily a few MiB, so it gets its own cap.
export const AUDIO_PREVIEW_MAX = 50 * 1024 * 1024;
const MD_FORBID_TAGS = ["style", "img", "svg", "math", "form", "iframe"];
const MD_FORBID_ATTR = ["style"];

function previewCss() {
  const tpl = document.getElementById("preview-css");
  return tpl ? tpl.content.textContent : "";
}

export function renderMarkdown(text) {
  const md = window.markdownit({ html: false, linkify: true });
  return window.DOMPurify.sanitize(md.render(text), { FORBID_TAGS: MD_FORBID_TAGS, FORBID_ATTR: MD_FORBID_ATTR });
}

function markdownFrame(clean) {
  const frame = document.createElement("iframe");
  frame.className = "preview-md";
  frame.setAttribute("sandbox", "");
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.title = "Markdown preview";
  frame.srcdoc = `<!doctype html><meta charset=utf-8><style>${previewCss()}</style><body>${clean}</body>`;
  return frame;
}

function jsonView(raw) {
  let pretty = null;
  try {
    pretty = JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    pretty = null;
  }
  const pre = el("pre", { class: "preview-pre mono" }, pretty ?? raw);
  if (pretty === null) {
    return el("div", {}, el("p", { class: "preview-msg" }, "Not valid JSON — showing raw text."), pre);
  }
  let showingRaw = false;
  const toggle = el("button", { class: "btn btn-small", type: "button" }, "Raw");
  toggle.addEventListener("click", () => {
    showingRaw = !showingRaw;
    pre.textContent = showingRaw ? raw : pretty;
    toggle.textContent = showingRaw ? "Pretty" : "Raw";
  });
  return el("div", {}, toggle, pre);
}

export function renderKind(kind, bytes, setUrl) {
  const text = () => new TextDecoder("utf-8").decode(bytes);
  switch (kind.kind) {
    case "markdown":
      return markdownFrame(renderMarkdown(text()));
    case "json":
      return jsonView(text());
    case "text":
      return el("pre", { class: "preview-pre mono" }, text());
    case "image": {
      const url = URL.createObjectURL(new Blob([bytes], { type: kind.type }));
      setUrl(url);
      return el("div", { class: "preview-stage" }, el("img", { class: "preview-img", src: url, alt: "" }));
    }
    case "audio": {
      const url = URL.createObjectURL(new Blob([bytes], { type: kind.type }));
      setUrl(url);
      return el("section", { class: "card audio-card", "aria-label": "Audio" },
        el("span", { class: "audio-icon", "aria-hidden": "true" }, icon("audio")),
        el("audio", { class: "preview-audio", src: url, controls: true, preload: "metadata" }));
    }
    default:
      return el("p", { class: "preview-msg" }, "No preview for this file type — Download it instead.");
  }
}

export function saveBytes(name, bytes) {
  const url = URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" }));
  const a = el("a", { href: url, download: name, class: "sr-only" });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30_000);
}
