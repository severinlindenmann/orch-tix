// fileshare/static/js/tags-ui.js — tags in the pages (spec §19): the pills on rows, the tag filter
// row under the type chips (its state in the address, ?tag=a&tag=b), the tags section of the file
// view with Edit tags, and the tags input of the upload sheet and the Note composer.
//
// Tags come from any device and are untrusted strings: they only ever reach the DOM as text (el()).
// The server's pattern limits them anyway. Other modules call in through small hooks; the list,
// the view and this module talk through window events:
//   fs:tag-filter {tag}        a pill was chosen: filter the list by that one tag (files.js)
//   fs:file-tags {n, id, tags} a file's tags were saved: update its row and the open view
import { api, ApiError } from "./api.js";
import { el, icon, toast } from "./ui.js";
import { MAX_TAGS, TAG_RULE, liveTag, tagsFromSearch, tryTag, withTags } from "./tags.js";

const TOP = 12; // the filter row shows this many tags by count, then "More…"
const SUGGEST = 8; // suggestions under a tags input
const same = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
const sorted = (tags) => [...tags].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
const tagsOf = (f) => (Array.isArray(f?.tags) ? f.tags.filter((t) => typeof t === "string") : []);

// ---- the tags in use (GET /api/tags): suggestions and the filter row's counts

let known = []; // [{tag, count, last_used}], by count then name (the server's order)
let knownLoad = null;
const knownListeners = new Set();

export function refreshKnownTags() {
  knownLoad = api("GET", "/api/tags")
    .then((res) => {
      known = (Array.isArray(res?.tags) ? res.tags : [])
        .filter((x) => x && typeof x.tag === "string")
        .map((x) => ({ tag: x.tag, count: Number(x.count) || 0 }));
    })
    .catch(() => { /* offline or signed out: keep what we had; no suggestions is fine */ })
    .then(() => {
      for (const fn of knownListeners) fn();
      return known;
    });
  return knownLoad;
}

function ensureKnown() {
  return knownLoad ?? refreshKnownTags();
}

// ---- the filter (Files): selected tags, applied server-side with ?tag= (all must match)

const filter = { tags: [], onChange: null, row: null, sheet: null };

export const selectedTags = () => filter.tags;
export const hasTagFilter = () => filter.tags.length > 0;
export const hasAllTags = (f) => filter.tags.every((t) => tagsOf(f).includes(t));
export function addTagParams(qs) {
  for (const t of filter.tags) qs.append("tag", t);
}
// A URL files.js builds ("/files", "/files?f=FILE7") with the current tag filter kept in it.
export const tagUrl = (url) => withTags(url, filter.tags);

const here = () => location.pathname + location.search + location.hash;

export function setTagFilter(tags) {
  const next = sorted(new Set(tags.map(tryTag).filter(Boolean))).slice(0, MAX_TAGS);
  if (same(next, filter.tags)) return;
  filter.tags = next;
  history.replaceState(history.state, "", withTags(here(), next));
  renderFilter();
  filter.onChange?.();
}

function toggleTag(tag) {
  if (filter.tags.includes(tag)) {
    setTagFilter(filter.tags.filter((t) => t !== tag));
  } else if (filter.tags.length >= MAX_TAGS) {
    toast(`Filter by at most ${MAX_TAGS} tags`, "error");
  } else {
    setTagFilter([...filter.tags, tag]);
  }
}

// Back/forward landed on an entry: take its tags. True when they changed (the caller reloads).
export function syncTagsFromUrl() {
  const next = tagsFromSearch(location.search);
  if (same(next, filter.tags)) return false;
  filter.tags = next;
  renderFilter();
  return true;
}

function filterChip(tag, count) {
  const on = filter.tags.includes(tag);
  return el("button", {
    type: "button", class: on ? "chip tag-chip is-on" : "chip tag-chip", "aria-pressed": String(on), dataset: { tag },
    title: on ? `Stop filtering by ${tag}` : `Show only files tagged ${tag}`, onClick: () => toggleTag(tag),
  },
  el("span", { class: "tag-chip-text" }, tag),
  on ? icon("x") : count ? el("span", { class: "tag-count", "aria-hidden": "true" }, String(count)) : null);
}

function renderFilter() {
  const row = filter.row;
  if (!row) return;
  const counts = new Map(known.map((x) => [x.tag, x.count]));
  const selected = filter.tags.map((t) => filterChip(t, counts.get(t)));
  const top = known.filter((x) => !filter.tags.includes(x.tag)).slice(0, TOP).map((x) => filterChip(x.tag, x.count));
  const more = known.length > TOP
    ? el("button", { type: "button", class: "chip tag-more-btn", "aria-haspopup": "dialog", onClick: openTagSheet }, "More…")
    : null;
  row.replaceChildren(...selected, ...top, more ?? "");
  row.hidden = !selected.length && !top.length;
  filter.sheet?.render();
}

// "More…": every tag in use, searchable; a tap toggles it in the filter and keeps the list open.
function openTagSheet() {
  filter.sheet?.close();
  const search = el("input", { id: "tag-search", class: "input", type: "search", placeholder: "Search tags", autocomplete: "off",
    spellcheck: "false", autocapitalize: "none" });
  const list = el("div", { class: "tag-sheet-list", role: "group", "aria-label": "Tags" });
  const done = el("button", { type: "button", class: "btn btn-accent btn-block" }, "Done");
  const close = el("button", { class: "close-btn", type: "button", "aria-label": "Close" }, icon("x"));
  const dlg = el("dialog", { class: "sheet tag-sheet", "aria-labelledby": "tag-sheet-title" },
    el("div", { class: "sheet-grip", "aria-hidden": "true" }),
    el("div", { class: "sheet-head" }, el("h2", { id: "tag-sheet-title" }, "Filter by tag"), close),
    el("label", { class: "field" }, el("span", { class: "label" }, "Search tags"), search),
    list, done);
  const render = () => {
    const q = liveTag(search.value).trim();
    const hits = known.filter((x) => x.tag.includes(q));
    list.replaceChildren(...(hits.length ? hits.map((x) => filterChip(x.tag, x.count)) : [el("p", { class: "muted" }, "No tags match.")]));
  };
  const finish = () => {
    if (filter.sheet !== sheet) return;
    filter.sheet = null;
    dlg.close();
    dlg.remove();
  };
  const sheet = { render, close: finish };
  filter.sheet = sheet;
  search.addEventListener("input", render);
  done.addEventListener("click", finish);
  close.addEventListener("click", finish);
  dlg.addEventListener("cancel", (e) => {
    if (e.target !== dlg) return;
    e.preventDefault();
    finish();
  });
  document.body.append(dlg);
  render();
  dlg.showModal();
  search.focus();
}

// Builds the tag chip row after `after` (the type chips) and reads ?tag= from the address.
// `onChange` runs when the selection changes (files.js reloads the list with the new ?tag=).
export function initTagFilter({ after, onChange }) {
  filter.onChange = onChange;
  filter.tags = tagsFromSearch(location.search);
  const canonical = withTags(here(), filter.tags);
  if (canonical !== here()) history.replaceState(history.state, "", canonical);
  filter.row = el("div", { class: "chips tag-chips", id: "tag-chips", role: "group", "aria-label": "Filter by tag", hidden: true });
  after.after(filter.row);
  knownListeners.add(renderFilter);
  ensureKnown();
  renderFilter();
}

// ---- pills on a row: at most 2 then +N on a phone, 3 on a desktop. A tap on one filters by it.

export function rowTagPills(f) {
  const tags = tagsOf(f);
  if (!tags.length) return null;
  const pills = tags.slice(0, 3).map((t, i) => el("span", {
    class: i === 2 ? "tag-pill d-only" : "tag-pill", dataset: { tag: t }, title: `Show files tagged ${t}`,
  }, t));
  return el("span", { class: "frow-tags" }, pills,
    tags.length > 2 ? el("span", { class: "tag-pill tag-extra m-only", title: tags.slice(2).join(", ") }, `+${tags.length - 2}`) : null,
    tags.length > 3 ? el("span", { class: "tag-pill tag-extra d-only", title: tags.slice(3).join(", ") }, `+${tags.length - 3}`) : null);
}

// The tag of the pill a row click landed on, or null (then the click opens the file as before).
export function pillTag(e) {
  const p = e.target instanceof Element ? e.target.closest(".frow-tags .tag-pill[data-tag]") : null;
  return p ? p.dataset.tag : null;
}

const askFilter = (tag) => window.dispatchEvent(new CustomEvent("fs:tag-filter", { detail: { tag } }));

// ---- the tags input: chips with ×, an input normalised as you type, suggestions by keyboard

let inputSeq = 0;

// Returns {el, value(), focus()}. value() adds any tag still typed in the box and returns the sorted
// tags, or null when that typed text isn't a valid tag (the inline error then says why).
// `item` and `noun` name the tags and what they are on ("tag", "file" -> "10 tags per file");
// `suggestions` ([{tag, count}], e.g. the labels
// seen on loaded tickets) replaces the /api/tags suggestions of files.
export function tagInput({
  initial = [], id = `tags-${++inputSeq}`, label = "Tags", optional = true, item = "tag", noun = "file", suggestions = null,
} = {}) {
  const own = Array.isArray(suggestions)
    ? suggestions.filter((x) => x && typeof x.tag === "string").map((x) => ({ tag: x.tag, count: Number(x.count) || 0 }))
    : null;
  const pool = () => own ?? known;
  let tags = sorted(new Set(initial));
  let active = -1;
  let options = [];
  const listId = `${id}-list`;
  const errId = `${id}-error`;
  const input = el("input", {
    id, class: "tag-input", type: "text", role: "combobox", "aria-autocomplete": "list", "aria-expanded": "false",
    "aria-controls": listId, "aria-describedby": errId, autocomplete: "off", autocapitalize: "none", spellcheck: "false",
    enterkeyhint: "done", maxlength: "60", placeholder: `Add a ${item}`,
  });
  const chips = el("span", { class: "tag-set" });
  const list = el("ul", { class: "tag-suggest", id: listId, role: "listbox", "aria-label": "Suggestions", hidden: true });
  const error = el("p", { class: "error-text tag-error", id: errId, hidden: true });
  const box = el("div", { class: "tag-box" }, chips, input);
  const wrap = el("div", { class: "field tag-field" },
    el("label", { class: "label", for: id }, label, optional ? " " : null, optional ? el("span", { class: "label-opt" }, "optional") : null),
    el("div", { class: "tag-anchor" }, box, list), error);
  box.addEventListener("click", (e) => { if (e.target === box) input.focus(); });

  const showError = (msg) => {
    error.textContent = msg || "";
    error.hidden = !msg;
    input.setAttribute("aria-invalid", String(Boolean(msg)));
  };
  const check = () => {
    const raw = input.value.trim();
    showError(raw && !tryTag(raw) ? `“${raw}” isn't a valid tag. ${TAG_RULE}` : "");
  };

  const renderChips = () => {
    chips.replaceChildren(...tags.map((t) => el("span", { class: "tag-chip-edit" },
      el("span", { class: "tag-chip-text" }, t),
      el("button", { type: "button", class: "tag-x", "aria-label": `Remove ${item} ${t}`, onClick: () => remove(t) }, icon("x")))));
  };
  const setActive = (i) => {
    active = i;
    options.forEach((o, j) => o.setAttribute("aria-selected", String(j === i)));
    if (i >= 0 && options[i]) {
      input.setAttribute("aria-activedescendant", options[i].id);
      options[i].scrollIntoView?.({ block: "nearest" });
    } else input.removeAttribute("aria-activedescendant");
  };
  const closeList = () => {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    setActive(-1);
  };
  // The list opens on typing and on ↓ only (not on focus), so it never covers the buttons below
  // unasked. `open` false re-renders it closed; by default it stays as it is.
  const renderList = (open = !list.hidden) => {
    const q = input.value.trim();
    const hits = pool().filter((x) => !tags.includes(x.tag) && x.tag.includes(q)).slice(0, SUGGEST);
    options = hits.map((x, i) => {
      const o = el("li", { id: `${listId}-${i}`, role: "option", class: "tag-option", "aria-selected": "false", dataset: { tag: x.tag } },
        el("span", {}, x.tag), el("span", { class: "tag-count", "aria-hidden": "true" }, String(x.count)));
      o.addEventListener("pointerdown", (e) => e.preventDefault()); // keep the focus in the input
      o.addEventListener("click", () => { add(x.tag); input.focus(); });
      return o;
    });
    list.replaceChildren(...options);
    const shown = open && options.length > 0 && document.activeElement === input;
    list.hidden = !shown;
    input.setAttribute("aria-expanded", String(shown));
    setActive(-1);
  };

  function add(raw) {
    const t = tryTag(raw);
    if (!t) {
      showError(`“${String(raw).trim()}” isn't a valid tag. ${TAG_RULE}`);
      return false;
    }
    if (!tags.includes(t)) {
      if (tags.length >= MAX_TAGS) {
        showError(`At most ${MAX_TAGS} ${item}s per ${noun}.`);
        return false;
      }
      tags = sorted([...tags, t]);
      renderChips();
    }
    input.value = "";
    showError("");
    renderList(false);
    return true;
  }
  function remove(t) {
    tags = tags.filter((x) => x !== t);
    renderChips();
    renderList();
    input.focus();
  }

  input.addEventListener("input", () => {
    let v = input.value;
    if (v.includes(",")) { // a comma (typed or pasted) commits what is before it
      const parts = v.split(",");
      v = parts.pop();
      for (const p of parts) if (p.trim()) add(p);
    }
    const live = liveTag(v);
    if (live !== input.value) input.value = live;
    check();
    renderList(true);
  });
  // Fresh suggestions matter only while the list can show, so the input listens only while focused:
  // nothing stays registered once its sheet or editor is gone.
  const onKnown = () => { if (!list.hidden) renderList(); };
  input.addEventListener("focus", () => {
    if (own) return; // its own suggestions: no /api/tags
    knownListeners.add(onKnown);
    ensureKnown();
  });
  input.addEventListener("blur", () => {
    knownListeners.delete(onKnown);
    closeList();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (list.hidden) renderList(true);
      if (!options.length) return;
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive(active < 0 ? (step > 0 ? 0 : options.length - 1) : (active + step + options.length) % options.length);
    } else if (e.key === "Enter") {
      if (active >= 0 && options[active]) {
        e.preventDefault();
        add(options[active].dataset.tag);
      } else if (input.value.trim()) {
        e.preventDefault();
        add(input.value);
      }
    } else if (e.key === "Escape" && !list.hidden) {
      e.preventDefault(); // closes the suggestions only, not the sheet or the file view
      e.stopPropagation();
      closeList();
    } else if (e.key === "Backspace" && !input.value && tags.length) {
      e.preventDefault();
      remove(tags[tags.length - 1]);
    }
  });
  renderChips();

  return {
    el: wrap,
    focus: () => input.focus(),
    value() {
      if (input.value.trim() && !add(input.value)) {
        input.focus();
        return null;
      }
      return [...tags];
    },
    showError,
  };
}

// ---- the file view's tags: chips (a tap filters by the tag, × removes it) and Edit tags

// Returns the section for `file` (a decoded row or a raw FileOut). Saving calls PUT /api/files/{ref}/tags.
export function fileTagsSection(file) {
  let current = tagsOf(file);
  let editor = null;
  let busy = false; // a PUT is in flight: every × and Save waits, so two quick × can't undo each other
  const chips = el("div", { class: "tag-set" });
  const none = el("span", { class: "muted tag-none" }, "No tags");
  const edit = el("button", { type: "button", class: "btn btn-small tag-edit-btn" }, icon("pen"), "Edit tags");
  const editWrap = el("div", { class: "tag-editor", hidden: true });
  const readRow = el("div", { class: "detail-tags-row" }, chips, none, edit);
  const section = el("section", { class: "detail-tags", "aria-label": "Tags" }, readRow, editWrap);

  const paint = () => {
    chips.replaceChildren(...current.map((t) => el("span", { class: "tag-chip-edit" },
      el("button", { type: "button", class: "tag-chip-name", title: `Show files tagged ${t}`, dataset: { tag: t }, onClick: () => askFilter(t) }, t),
      el("button", { type: "button", class: "tag-x", "aria-label": `Remove tag ${t}`, disabled: busy,
        onClick: () => save(current.filter((x) => x !== t)) }, icon("x")))));
    none.hidden = current.length > 0;
  };

  const setBusy = (on) => {
    busy = on;
    for (const b of section.querySelectorAll(".tag-x, .tag-edit-btn, .tag-save-btn")) b.disabled = on;
  };

  async function save(tags, input = null) {
    if (busy) return false;
    setBusy(true);
    try {
      const out = await api("PUT", `/api/files/${encodeURIComponent(file.id)}/tags`, { json: { tags } });
      current = tagsOf(out);
      paint();
      window.dispatchEvent(new CustomEvent("fs:file-tags", { detail: { n: file.n, id: file.id, tags: current } }));
      refreshKnownTags();
      toast(current.length ? `Tags of ${file.id}: ${current.join(", ")}` : `Removed the tags of ${file.id}`, "ok");
      return true;
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return false;
      if (err instanceof ApiError && err.code === "bad_tag") {
        if (input) input.showError(err.detail || TAG_RULE);
        else toast(`Couldn't save the tags: ${err.detail || TAG_RULE}`, "error");
      } else if (err instanceof ApiError && err.status === 404) {
        toast(`${file.id} no longer exists`, "error");
      } else {
        toast(`Couldn't save the tags of ${file.id}: ${err.message}`, "error");
      }
      return false;
    } finally {
      setBusy(false);
    }
  }

  const closeEditor = () => {
    editor = null;
    editWrap.replaceChildren();
    editWrap.hidden = true;
    readRow.hidden = false;
    edit.focus();
  };
  edit.addEventListener("click", () => {
    editor = tagInput({ initial: current, id: "file-tags", label: `Tags of ${file.id}`, optional: false });
    const saveBtn = el("button", { type: "button", class: "btn btn-accent btn-small tag-save-btn" }, "Save tags");
    const cancel = el("button", { type: "button", class: "btn btn-small" }, "Cancel");
    const ed = editor;
    saveBtn.addEventListener("click", async () => {
      const tags = ed.value();
      if (tags === null) return;
      const ok = await save(tags, ed);
      if (ok && editor === ed) closeEditor();
    });
    cancel.addEventListener("click", closeEditor);
    editWrap.replaceChildren(ed.el, el("div", { class: "tag-editor-actions" }, cancel, saveBtn));
    editWrap.hidden = false;
    readRow.hidden = true; // the editor holds the chips while it is open
    ed.focus();
  });

  paint();
  return section;
}

if (typeof window !== "undefined") {
  // The counts follow uploads, deletes and drained outbox items.
  for (const ev of ["fs:files-changed", "fs:file-deleted"]) {
    window.addEventListener(ev, () => { if (knownLoad) refreshKnownTags(); });
  }
}
