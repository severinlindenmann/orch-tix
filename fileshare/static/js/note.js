// fileshare/static/js/note.js — "New note" (spec §16): a composer for text with an optional name
// (default note-YYYY-MM-DD-HHMM.md, local time) and the expiry segment. Saving hands a text/markdown
// File to upload.js's uploadFiles, the same prepare -> send-or-queue path as every upload, so a
// note written offline lands in the encrypted outbox. Not an entry module: upload.js passes
// uploadFiles and its ttlSegment in, so node can test the pure helpers.
import { el, icon } from "./ui.js";

const pad = (n) => String(n).padStart(2, "0");

export function noteName(d = new Date()) {
  return `note-${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}.md`;
}

export function noteFile(text, name, fallback) {
  if (!String(text).trim()) return null;
  return new File([text], String(name).trim() || fallback, { type: "text/markdown" });
}

let sheet = null;

function closeSheet() {
  if (!sheet) return;
  sheet.close();
  sheet.remove();
  sheet = null;
}

// upload(files, note, ttl, opts) -> Promise; ttlSegment(value, ids) -> {group, value()};
// tagInput(opts) -> {el, value()} (tags-ui.js, spec §19), optional.
export function openNoteSheet({ upload, ttlSegment, tagInput = null }) {
  closeSheet();
  const fallback = noteName();
  const name = el("input", { id: "note-name", class: "input", type: "text", maxlength: "255", placeholder: fallback,
    autocomplete: "off", spellcheck: "false" });
  const text = el("textarea", { id: "note-text", class: "input note-text", rows: "8", placeholder: "Write something…" });
  const ttl = ttlSegment(undefined, { id: "note-ttl", labelId: "note-ttl-label" });
  const tagsIn = tagInput?.({ id: "note-tags" }); // ---- tags (spec §19)
  const save = el("button", { class: "btn btn-accent btn-block btn-big", type: "button", disabled: "" }, "Save");
  const close = el("button", { class: "close-btn", type: "button", "aria-label": "Close" }, icon("x"));
  sheet = el(
    "dialog",
    { class: "sheet note-sheet", "aria-labelledby": "note-title" },
    el("div", { class: "sheet-grip", "aria-hidden": "true" }),
    el("div", { class: "sheet-head" }, el("h2", { id: "note-title" }, "New note"), close),
    el("label", { class: "field" },
      el("span", { class: "label" }, "Name ", el("span", { class: "label-opt" }, "optional")), name),
    el("label", { class: "field" }, el("span", { class: "label" }, "Text"), text),
    tagsIn?.el,
    el("fieldset", { class: "field seg-field" },
      el("legend", { class: "label", id: "note-ttl-label" }, "Expires"), ttl.group),
    save,
    el("p", { class: "sheet-lock" }, icon("lock"), "Encrypted on this device before it leaves"),
  );
  text.addEventListener("input", () => { save.disabled = !text.value.trim(); });
  close.addEventListener("click", closeSheet);
  sheet.addEventListener("cancel", (e) => {
    e.preventDefault();
    closeSheet();
  });
  save.addEventListener("click", async () => {
    const file = noteFile(text.value, name.value, fallback);
    if (!file) return;
    const tags = tagsIn ? tagsIn.value() : []; // ---- tags: an invalid typed tag keeps the sheet open
    if (tags === null) return;
    const chosenTtl = ttl.value();
    closeSheet();
    await upload([file], "", chosenTtl, { kind: "note", tags });
  });
  document.body.append(sheet);
  sheet.showModal();
  text.focus();
}
