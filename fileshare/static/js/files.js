import { api, ApiError } from "./api.js";
import { IntegrityError, openFileMeta } from "./crypto.js";
import { loadKeys } from "./keystore.js";
import { el, hydrateIcons, icon, wirePopover } from "./ui.js";
import {
  deviceKey, deviceLabel, expiryShort, fileGroup, isExpiringSoon, isNew, mapLimit, matches, shortAge, typeOf,
} from "./format.js";
import { previewKind } from "./previewkind.js";
import { tagsFromSearch, withTags } from "./tags.js";
import { closeFileView, currentFile, openFileView } from "./fileview.js";
// ---- tags (spec §19): the pills, the tag filter (?tag=) and its URL state live in tags-ui.js
import {
  addTagParams, hasAllTags, hasTagFilter, initTagFilter, pillTag, rowTagPills, setTagFilter, syncTagsFromUrl, tagUrl,
} from "./tags-ui.js";
import "./linkshare.js"; // Task 32: Share link and the Public links section (spec §17)
import { adoptPending, openDropDialog } from "./droplink.js"; // Task 4: inbound one-time upload links (spec §18)
import { showBanner } from "./banner.js";

const PAGE = 50;
const DECRYPT_CONCURRENCY = 6;
const TOMBSTONE_PAGES = 20;
// The type chips of the redesign (spec §18); "Text" covers Markdown, JSON and plain text, "Other" the rest (zip, pdf, bin).
const TYPES = [["all", "All"], ["image", "Images"], ["audio", "Audio"], ["text", "Text"], ["other", "Other"]];
const UNREADABLE = { kind: "other", label: "?" };
const TILE_ICON = { image: "image", audio: "audio", text: "doc", other: "files", done: "check", bad: "alert" };

const state = {
  keys: null,
  files: [],
  nextBefore: null,
  loaded: false,
  gen: 0, // bumped by reload(); a loadMore from an older generation discards its result
  loading: null, // the generation whose loadMore is in flight

  filter: { type: "all", device: "", project: "", q: "" },
  showAcked: false, // the "Done" chip; remembered per browser
  selected: null, // n of the highlighted row (desktop keyboard selection)
  pushed: false, // the open file view added a history entry (phone), so closing it steps back
  wanted: null, // ?f=ID from the URL, opened once the list knows it
  offline: false, // the first listing got no answer: the Offline state is shown
};
const $ = (id) => document.getElementById(id);
const SHOW_ACKED_KEY = "fileshare.showAcked";
const DESKTOP = "(min-width: 900px)";
const isDesktop = () => window.matchMedia(DESKTOP).matches;

function readShowAcked() {
  try {
    return localStorage.getItem(SHOW_ACKED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeShowAcked(on) {
  try {
    localStorage.setItem(SHOW_ACKED_KEY, on ? "1" : "0");
  } catch {
    /* storage blocked (private mode, policy): the chip still works for this page */
  }
}

const isAuthError = (err) => err instanceof ApiError && err.status === 401;

// The tile colour and the type chips: image, audio, text (Markdown, JSON, code, plain) or other.
export function tileKind(f) {
  if (!f.ok) return "bad";
  const pk = previewKind(f.meta).kind;
  if (pk === "audio") return "audio";
  if (pk === "image") return "image";
  if (pk === "markdown" || pk === "json" || pk === "text") return "text";
  return "other";
}

async function decode(f) {
  try {
    const { meta, dek } = await openFileMeta(state.keys.mk, f);
    const out = { ...f, ok: true, meta, dek, kind: typeOf(meta.mime, meta.name) };
    return { ...out, tile: tileKind(out) };
  } catch (err) {
    if (!(err instanceof IntegrityError)) console.error(`decrypting metadata of ${f.id}`, err);
    window.dispatchEvent(new CustomEvent("fs:decrypt-failed", { detail: { id: f.id } }));
    return { ...f, ok: false, meta: null, dek: null, kind: UNREADABLE, tile: "bad" };
  }
}

// Fetches the next page. A page of nothing but tombstones would render blank, so keep going
// (up to TOMBSTONE_PAGES pages per click) until a live row turns up or the list ends.
async function loadMore() {
  const gen = state.gen;
  if (state.loading === gen) return;
  state.loading = gen;
  $("load-more").disabled = true;
  try {
    let added = 0;
    for (let pages = 0; added === 0 && pages < TOMBSTONE_PAGES; pages += 1) {
      const qs = new URLSearchParams({ limit: String(PAGE) });
      if (state.showAcked) qs.set("acked", "1");
      if (state.nextBefore != null) qs.set("before", String(state.nextBefore));
      addTagParams(qs); // ---- tags: every selected tag must match, server-side
      const res = await api("GET", `/api/files?${qs}`);
      const live = res.files.filter((f) => !f.deleted_at);
      const decoded = await mapLimit(live, DECRYPT_CONCURRENCY, decode);
      if (gen !== state.gen) return; // reload() started over while this page was in flight
      state.files.push(...decoded);
      state.nextBefore = res.next_before;
      state.loaded = true;
      added += decoded.length;
      if (state.nextBefore == null) break;
    }
    $("load-error").hidden = true;
    setOffline(false);
  } catch (err) {
    if (gen !== state.gen || isAuthError(err)) return;
    // No answer before anything was listed (the session check): the Offline state (spec §16). The
    // dock stays usable; what is created now goes to the outbox.
    if (err.status === 0 && !state.loaded) {
      setOffline(true);
      return;
    }
    $("load-error-text").textContent = err.status === 0 ? err.detail : `Couldn't load files: ${err.message}`;
    $("load-error").hidden = false;
  } finally {
    if (gen === state.gen) {
      state.loading = null;
      $("load-more").disabled = false;
      $("loading").hidden = true;
      render();
      openWanted();
    }
  }
}

// ---- upload links (spec §18, Task 4): adopt any pending drop before the list is first shown, and
// again whenever the tab regains focus (another owner client, or the drop page, may have just used
// the link). Never blocks the files list on a failure: a transient failure (offline, a 5xx) just
// retries on the next pass, silently; only an upload that can never be decrypted (tampered, or sealed
// to a key this account doesn't hold) gets a banner pointing at the Upload links dialog to discard it.
let adopting = false;
async function autoAdopt() {
  if (!state.keys || adopting) return;
  adopting = true;
  try {
    let links;
    try {
      links = (await api("GET", "/api/upload-links")).links;
    } catch {
      return; // offline or 401: the usual banners already cover this
    }
    // `failed` (a transient network/5xx error) is intentionally ignored here: nothing to show, it
    // just retries on the next pass.
    const { adopted, undecryptable } = await adoptPending(api, state.keys.mk, links);
    if (adopted > 0) reload();
    if (undecryptable.length) {
      showBanner("decrypt", "An upload can't be decrypted — open Upload links to discard it.");
    }
  } finally {
    adopting = false;
  }
}

function onVisible() {
  if (document.visibilityState === "visible") autoAdopt();
}

function setOffline(on) {
  state.offline = on;
  $("offline").hidden = !on;
  document.body.classList.toggle("is-offline", on);
}

// Back online after an offline start: list the files now.
function onReconnect() {
  if (state.offline && state.loading !== state.gen) reload();
}

function visibleFiles() {
  const { type, device, project, q } = state.filter;
  return state.files.filter((f) =>
    (state.showAcked || !f.acked_at)
    && (type === "all" || f.tile === type)
    && (!device || deviceKey(f) === device)
    && (!project || f.project === project)
    && hasAllTags(f) // ---- tags: a row whose tags were just edited away drops out
    && matches(f, q));
}

function renderSelect(select, pairs, allLabel, filterKey) {
  const current = state.filter[filterKey];
  select.replaceChildren(el("option", { value: "" }, allLabel), ...pairs.map(([k, v]) => el("option", { value: k }, v)));
  if (!pairs.some(([k]) => k === current)) state.filter[filterKey] = "";
  select.value = state.filter[filterKey];
  select.closest(".chip-select").hidden = pairs.length < 2;
  return pairs.length >= 2;
}

function distinct(pairs) {
  const seen = new Map();
  for (const [k, v] of pairs) if (!seen.has(k)) seen.set(k, v);
  return [...seen].sort((a, b) => a[1].localeCompare(b[1]));
}

function transcribedPill(f) {
  if (!f.ok || !f.meta.transcript || f.tile !== "audio") return null;
  return el("span", { class: "badge pill-transcribed", title: "Has a transcript — open the file to read it" }, "Transcribed");
}

function tile(f) {
  const kind = f.acked_at && f.ok ? "done" : f.tile;
  return el("span", { class: `tile t-${kind}`, "aria-hidden": "true" }, icon(TILE_ICON[kind]));
}

function ageEl(iso) {
  return el("time", { datetime: iso, title: iso, dataset: { age: "short" } }, shortAge(iso));
}

function metaLine(f) {
  const parts = [el("span", { class: "mono frow-id" }, f.id), " · "];
  if (f.acked_at) {
    parts.push(el("span", { class: "acked-line" }, `Done by ${f.acked_by || "someone"}`));
  } else {
    parts.push(el("span", { class: "device", title: deviceLabel(f) }, deviceLabel(f)), " · ", ageEl(f.created_at));
  }
  return el("span", { class: "frow-meta" }, parts);
}

function expiryEl(f) {
  const label = expiryShort(f.expires_at);
  if (!label) return null; // an unparseable expires_at: show nothing rather than an empty label
  return el("span", {
    class: isExpiringSoon(f.expires_at) ? "exp exp-soon" : "exp",
    title: f.expires_at ? `Expires ${f.expires_at}` : "Never expires",
    dataset: { expires: f.expires_at ?? "" },
  }, label);
}

function rowClass(f) {
  return ["frow", f.ok ? "" : "frow-bad", f.acked_at ? "acked" : "", state.selected === f.n ? "is-selected" : ""]
    .filter(Boolean).join(" ");
}

function renderRow(f) {
  const current = currentFile();
  const a = el("a", {
    class: rowClass(f), href: `/files?f=${encodeURIComponent(f.id)}`, dataset: { id: f.id, n: String(f.n) },
    "aria-current": current && current.n === f.n ? "true" : null,
    onClick: (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return; // let "open in new tab" work
      e.preventDefault();
      const tag = pillTag(e); // ---- tags: a tap on a pill filters by its tag instead
      if (tag) filterByTag(tag);
      else openRow(f);
    },
  },
  tile(f),
  el("span", { class: "frow-main" },
    el("span", { class: "fname-line" },
      el("span", { class: "fname", title: f.ok ? f.meta.name : null }, f.ok ? f.meta.name : "Encrypted file"),
      transcribedPill(f),
      f.ok ? null : el("span", { class: "badge badge-warn", title: "Encrypted with a different key, or corrupt" }, "Can't decrypt")),
    f.ok && f.meta.note ? el("span", { class: "fnote", title: f.meta.note }, f.meta.note) : null,
    metaLine(f),
    rowTagPills(f)), // ---- tags
  el("span", { class: "frow-side" },
    isNew(f) ? el("span", { class: "new-dot", title: "New" }, el("span", { class: "sr-only" }, "New")) : null,
    expiryEl(f)));
  return a;
}

function renderGroups(visible) {
  const groups = new Map();
  for (const f of visible) {
    const g = fileGroup(f);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(f);
  }
  return [...groups].map(([title, files]) => el("section", { class: "fgroup", "aria-label": title },
    el("h2", { class: "fgroup-title" }, title),
    el("div", { class: "fgroup-rows" }, files.map(renderRow))));
}

function countText(visibleCount) {
  const open = state.files.filter((f) => !f.acked_at).length;
  const done = state.files.length - open;
  let text = `${open} open`;
  // Only done files this page knows about: with the chip off the server doesn't list them.
  if (done) text += state.showAcked ? ` · ${done} done` : ` · ${done} done hidden`;
  const listed = state.showAcked ? state.files.length : open;
  if (visibleCount !== listed) text += ` · ${visibleCount} shown`;
  return text;
}

function render() {
  const visible = visibleFiles();
  const empty = state.loaded && state.files.length === 0 && state.nextBefore == null && !hasTagFilter(); // ---- tags
  if (state.selected != null && !visible.some((f) => f.n === state.selected)) state.selected = null;
  $("file-list").replaceChildren(...renderGroups(visible));
  $("empty").hidden = !empty;
  $("results").hidden = empty || !state.loaded;
  // Also shown when only tombstones were found so far but older pages remain behind "Load more".
  $("no-match").hidden = visible.length > 0 || (state.files.length === 0 && state.nextBefore == null && !hasTagFilter());
  $("load-more").hidden = state.nextBefore == null;
  $("count").textContent = !state.loaded || empty ? "" : countText(visible.length);
  const fresh = state.files.filter((f) => isNew(f)).length;
  const badge = $("files-new");
  if (badge) {
    badge.textContent = fresh ? `${fresh} new` : "";
    badge.hidden = !fresh;
  }
  const hasDevice = renderSelect($("filter-device"), distinct(state.files.map((f) => [deviceKey(f), deviceLabel(f)])), "All devices", "device");
  const hasProject = renderSelect($("filter-project"), distinct(state.files.map((f) => [f.project, f.project])), "All projects", "project");
  $("list-menu-wrap").hidden = !hasDevice && !hasProject;
}

// ---- the file view: opening, closing, history (spec §18)

function rowEl(n) {
  return $("file-list").querySelector(`.frow[data-n="${n}"]`);
}

function markOpen(n) {
  for (const r of $("file-list").querySelectorAll(".frow[aria-current]")) r.removeAttribute("aria-current");
  if (n != null) rowEl(n)?.setAttribute("aria-current", "true");
}

function urlFor(id) {
  return tagUrl(id ? `/files?f=${encodeURIComponent(id)}` : "/files"); // ---- tags: the filter stays in the address
}

function show(f, { focus }) {
  state.selected = f.n;
  for (const r of $("file-list").querySelectorAll(".frow.is-selected")) r.classList.remove("is-selected");
  rowEl(f.n)?.classList.add("is-selected");
  openFileView(f, { onClose: requestClose, focus });
  markOpen(f.n);
}

// A tap or click on a row, or Enter on the selection.
function openRow(f) {
  if (isDesktop()) {
    history.replaceState({ f: f.id }, "", urlFor(f.id));
    state.pushed = false;
    show(f, { focus: false });
  } else {
    // The phone's full-screen view is its own history entry, so the back button closes it.
    if (state.pushed) history.replaceState({ f: f.id }, "", urlFor(f.id));
    else history.pushState({ f: f.id }, "", urlFor(f.id));
    state.pushed = true;
    show(f, { focus: true });
  }
}

function closeView() {
  closeFileView();
  markOpen(null);
}

// The view's back button, Escape, or a delete.
function requestClose() {
  if (state.pushed) {
    history.back(); // popstate closes the view
    return;
  }
  history.replaceState(null, "", urlFor(null));
  closeView();
}

function onPopState() {
  const ref = new URLSearchParams(location.search).get("f");
  const id = ref && FILE_REF.test(ref) ? canonicalId(ref) : null;
  const f = id ? state.files.find((x) => x.id.toLowerCase() === id.toLowerCase()) : null;
  if (syncTagsFromUrl()) reload(); // ---- tags: back/forward restores the entry's tag filter
  if (!f) {
    state.pushed = false;
    closeView();
    return;
  }
  state.pushed = Boolean(history.state && history.state.f) && !isDesktop();
  show(f, { focus: !isDesktop() });
}

// ?f= takes a file ID ("FILE7", any case) or its bare number, nothing else.
const FILE_REF = /^(file)?\d+$/i;

function canonicalId(ref) {
  return `FILE${ref.replace(/^file/i, "")}`;
}

// A ?f=ID in the address (a reload, a shared link inside the app): open it once the list has it,
// or fetch that one file when it isn't on the first page.
async function openWanted() {
  const id = state.wanted;
  if (!id) return;
  state.wanted = null;
  let f = state.files.find((x) => x.id.toLowerCase() === id.toLowerCase());
  if (!f) {
    try {
      const raw = await api("GET", `/api/files/${encodeURIComponent(id)}`);
      // A tombstone has no keys: show it as deleted instead of failing to decrypt it.
      f = raw.deleted_at ? raw : await decode(raw);
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 410)) {
        history.replaceState(null, "", urlFor(null));
        return;
      }
      f = { id: canonicalId(id), n: null, deleted_at: true };
    }
  }
  state.pushed = false;
  show(f, { focus: !isDesktop() });
}

// ---- keyboard (desktop): "/" focuses search, ↑/↓ move the selection, Enter opens it

function isTyping(t) {
  return t instanceof HTMLElement && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
}

function moveSelection(step) {
  const rows = [...$("file-list").querySelectorAll(".frow")];
  if (!rows.length) return;
  let i = rows.findIndex((r) => Number(r.dataset.n) === state.selected);
  i = i < 0 ? (step > 0 ? 0 : rows.length - 1) : Math.min(rows.length - 1, Math.max(0, i + step));
  for (const r of rows) r.classList.remove("is-selected");
  rows[i].classList.add("is-selected");
  state.selected = Number(rows[i].dataset.n);
  rows[i].focus();
}

function onKey(e) {
  if (e.defaultPrevented || e.altKey || e.metaKey || e.ctrlKey) return;
  if (document.querySelector("dialog[open]")) return;
  const t = e.target;
  if (e.key === "Escape" && currentFile() && !isTyping(t)) {
    e.preventDefault();
    requestClose();
    return;
  }
  if (!isDesktop()) return;
  const inSearch = t === $("search");
  if (e.key === "/" && !isTyping(t)) {
    e.preventDefault();
    $("search").focus();
    $("search").select();
  } else if ((e.key === "ArrowDown" || e.key === "ArrowUp") && (!isTyping(t) || inSearch)) {
    if (t instanceof HTMLElement && t.closest(".detail, .popover, .menu")) return;
    e.preventDefault();
    moveSelection(e.key === "ArrowDown" ? 1 : -1);
  } else if (e.key === "Enter" && inSearch && state.selected != null) {
    const f = state.files.find((x) => x.n === state.selected);
    if (f) {
      e.preventDefault();
      openRow(f);
    }
  }
}

// ---- updates from elsewhere

// A single file changed on the server (ack, expiry): merge its clear metadata into the row in place.
// Done rows stay in the list (hidden unless the "Done" chip is on) so the count knows them.
function applyUpdate(file) {
  if (!file || typeof file.n !== "number") return;
  const i = state.files.findIndex((x) => x.n === file.n);
  if (i < 0) return;
  const { acked_at, acked_by, expires_at, device, project } = file;
  state.files[i] = { ...state.files[i], acked_at, acked_by, expires_at, device, project };
  render();
}

// ---- tags (spec §19): a pill was chosen (a row, the file view): filter by just that tag. An open
// phone view is its own history entry, so step back first and filter once the list is showing.
function filterByTag(tag) {
  if (currentFile() && state.pushed) {
    window.addEventListener("popstate", () => setTagFilter([tag]), { once: true });
    requestClose();
    return;
  }
  if (currentFile() && !isDesktop()) requestClose();
  setTagFilter([tag]);
}

function applyTags({ n, tags } = {}) {
  const i = state.files.findIndex((x) => x.n === n);
  if (i < 0 || !Array.isArray(tags)) return;
  state.files[i] = { ...state.files[i], tags };
  render();
}

function dropLocal(n) {
  state.files = state.files.filter((x) => x.n !== n);
  render();
}

function wireToolbar() {
  hydrateIcons();
  const chips = $("type-chips");
  const ackedChip = $("acked-chip");
  const pick = (key) => {
    state.filter.type = key;
    for (const c of chips.querySelectorAll("[data-type]")) c.setAttribute("aria-pressed", String(c.dataset.type === key));
    render();
  };
  ackedChip.before(...TYPES.map(([key, label]) => el("button", {
    type: "button", class: "chip", "aria-pressed": String(key === state.filter.type), dataset: { type: key },
    onClick: () => pick(key),
  }, label)));
  $("search").addEventListener("input", (e) => { state.filter.q = e.target.value; render(); });
  $("filter-device").addEventListener("change", (e) => { state.filter.device = e.target.value; render(); });
  $("filter-project").addEventListener("change", (e) => { state.filter.project = e.target.value; render(); });
  wirePopover($("list-menu-btn"), $("list-menu"));
  $("droplink-btn").addEventListener("click", () => openDropDialog());
  initTagFilter({ after: chips, onChange: reload }); // ---- tags: the tag chip row under the type chips
  ackedChip.setAttribute("aria-pressed", String(state.showAcked));
  ackedChip.addEventListener("click", () => {
    state.showAcked = !state.showAcked;
    ackedChip.setAttribute("aria-pressed", String(state.showAcked));
    writeShowAcked(state.showAcked);
    reload();
  });
  $("load-more").addEventListener("click", loadMore);
  $("retry").addEventListener("click", loadMore);
  document.addEventListener("keydown", onKey);
  window.addEventListener("popstate", onPopState);
  setInterval(() => {
    for (const t of $("file-list").querySelectorAll("time[data-age]")) t.textContent = shortAge(t.dateTime);
    for (const p of $("file-list").querySelectorAll(".exp[data-expires]")) {
      const at = p.dataset.expires || null;
      p.textContent = expiryShort(at);
      p.hidden = !p.textContent;
      p.classList.toggle("exp-soon", isExpiringSoon(at));
    }
  }, 60_000);
}

function reload() {
  state.gen += 1;
  state.files = [];
  state.nextBefore = null;
  state.loaded = false;
  return loadMore();
}

async function main() {
  // banner.js (imported by shell.js) owns fs:unauthenticated: it clears the keys and shows the session banner.
  window.addEventListener("fs:files-changed", reload);
  window.addEventListener("fs:file-updated", (e) => applyUpdate(e.detail?.file));
  window.addEventListener("fs:file-deleted", (e) => dropLocal(e.detail?.n));
  window.addEventListener("fs:file-tags", (e) => applyTags(e.detail)); // ---- tags
  window.addEventListener("fs:tag-filter", (e) => filterByTag(e.detail?.tag));
  // ---- transcription (Task 35): a transcript added from this tab shows the Transcribed badge at once
  window.addEventListener("fs:transcript-added", (e) => {
    const f = state.files.find((x) => x.n === e.detail?.n);
    if (!f?.ok) return;
    f.meta = { ...f.meta, transcript: e.detail.transcript };
    render();
  });
  window.addEventListener("online", onReconnect);
  window.addEventListener("fs:network-ok", onReconnect);
  document.addEventListener("visibilitychange", onVisible);
  state.showAcked = readShowAcked();
  const ref = new URLSearchParams(location.search).get("f");
  // ignored, and dropped from the address (the tag filter stays)
  if (ref !== null && !FILE_REF.test(ref)) history.replaceState(null, "", withTags("/files", tagsFromSearch(location.search)));
  state.wanted = ref !== null && FILE_REF.test(ref) ? canonicalId(ref) : null;
  state.keys = await loadKeys();
  if (!state.keys) {
    location.replace("/login");
    return;
  }
  wireToolbar();
  await autoAdopt();
  await loadMore();
}

main();
