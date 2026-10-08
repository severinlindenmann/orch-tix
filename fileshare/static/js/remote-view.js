// fileshare/static/js/remote-view.js: the viewer and download callbacks of the dashboard frame (frame-host.js). The host
// has already required a user gesture and (for a download) a click on its question. A file from the dashboard opens in
// the app's own preview renderers (render.js, the same as the file view): text through textContent, an image or audio
// file from a blob URL, Markdown in a sandbox="" iframe. HTML and SVG are never shown (viewPlan says "none"; the person
// can save them). Nothing here runs a script of the file's.
import { el, toast } from "./ui.js";
import { renderKind, saveBytes } from "./render.js";
import { DOWNLOAD_MAX, fileName, viewPlan } from "./remote-model.js";

export function createViewer(mount) {
  let url = null;
  const close = () => { if (url) URL.revokeObjectURL(url); url = null; mount.replaceChildren(); };

  function open(view) {
    close();
    const plan = viewPlan(view, { markdown: Boolean(window.markdownit && window.DOMPurify) });
    const save = el("button", { class: "btn", type: "button", onclick: () => download(view) }, "Download");
    const body = plan.kind ? renderKind(plan.kind, view.body, (u) => { url = u; })
      : el("p", { class: "preview-msg" }, plan.reason === "large" ? "This file is too large to show here." : "This file cannot be shown here.");
    const done = el("button", { class: "btn", type: "button", onclick: close }, "Close");
    mount.append(el("section", { class: "card remote-viewer", role: "group", "aria-label": "File from the dashboard" },
      el("h2", {}, plan.name), body, el("div", { class: "row" }, save, done)));
    done.focus();
  }

  function download(view) {
    if (view.body.length > DOWNLOAD_MAX) return toast("This file is too large to save here.", "error");
    saveBytes(fileName(view), view.body);
  }

  return { open, download, close };
}
