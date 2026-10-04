// Public links, the owner's side (spec §17): the "Share link" dialog (a bottom sheet on a phone) and
// the file view's "Public links" section. Not an entry module: files.js imports it once, and it wires
// itself into fileview.js through fileViewHooks.
//
// The link key LK lives only in this module's memory, for as long as the dialog shows the URL: it is
// generated here, used to wrap the file's DEK, and put into the URL's fragment, which browsers never
// send. The server gets the wrapped DEK and hands back a token. Closing the dialog drops both.
// Every string from the server (who created a link) reaches the DOM as text.
import { api, ApiError } from "./api.js";
import { LINK_KEY_LEN, b64u, hexToBytes, newLinkKey, wrapDekForLink } from "./crypto.js";
import { confirmDialog, el, icon, toast } from "./ui.js";
import { expiryPhrase, relTime } from "./format.js";
import { copyText } from "./clipboard.js";
import { fileViewHooks } from "./fileview.js";
import { previewError, resolveRow } from "./preview.js";

const H = 3600_000;
const D = 24 * H;
// [ttl, label, duration]; the API takes exactly these ttl values.
export const LINK_TTLS = [["1h", "1 hour", H], ["1d", "1 day", D], ["7d", "7 days", 7 * D], ["30d", "30 days", 30 * D]];
const DEFAULT_MAX = 7 * D;
export const MAX_DOWNLOADS = 1000;
const TOKEN_RE = /^[A-Za-z0-9_-]{43}$/;

export function linkUrl(origin, token, lk) {
  if (!TOKEN_RE.test(String(token))) throw new Error("malformed link token");
  if (!(lk instanceof Uint8Array) || lk.length !== LINK_KEY_LEN) throw new Error("malformed link key");
  return `${origin}/p/${token}#${b64u(lk)}`;
}

// The expiry options for a file that expires at `fileExpiresAt` (null: never). An option is allowed
// when it fits the file's remaining life, or when it is the smallest option past it: the server
// clamps a link at the file's expiry (§17), so a 7-day file uploaded 2 h ago still offers 7 d. The
// default is the longest allowed option up to 7 days. `left` is the file's remaining life in ms.
export function linkTtlChoices(fileExpiresAt, now = Date.now()) {
  const end = fileExpiresAt == null ? NaN : Date.parse(fileExpiresAt);
  const left = Number.isFinite(end) ? end - now : Infinity;
  const past = LINK_TTLS.findIndex(([, , ms]) => ms > left);
  const options = LINK_TTLS.map(([ttl, label, ms], i) => ({ ttl, label, disabled: ms > left && i !== past }));
  const fits = LINK_TTLS.filter(([, , ms], i) => !options[i].disabled && ms <= DEFAULT_MAX);
  return { options, def: fits[fits.length - 1][0], left };
}

// The "never outlives its file" hint: only when the chosen option is cut short by more than an hour.
export function cutByMoreThanAnHour(ttl, left) {
  const opt = LINK_TTLS.find(([t]) => t === ttl);
  return Boolean(opt) && opt[2] - left > H;
}

// "Limit downloads": null when off, else a whole number from 1 to 1000.
export function maxDownloadsValue(checked, raw) {
  if (!checked) return { value: null };
  const s = String(raw ?? "").trim();
  const n = /^\d{1,4}$/.test(s) ? Number(s) : NaN;
  if (!(n >= 1 && n <= MAX_DOWNLOADS)) return { error: `Enter a number from 1 to ${MAX_DOWNLOADS}.` };
  return { value: n };
}

// ---- the dialog

let sheet = null;

function closeSheet() {
  if (!sheet) return;
  const s = sheet;
  sheet = null;
  s.close();
  s.remove(); // the URL (and with it LK) goes with the dialog's DOM
}

function segment(choices, onChange) {
  let current = choices.def;
  const buttons = choices.options.map((o) => el("button", {
    type: "button", class: "seg-btn", "aria-pressed": String(o.ttl === current), dataset: { ttl: o.ttl }, disabled: o.disabled,
  }, o.label));
  for (const b of buttons) {
    b.addEventListener("click", () => {
      current = b.dataset.ttl;
      for (const x of buttons) x.setAttribute("aria-pressed", String(x === b));
      onChange(current);
    });
  }
  return { group: el("div", { class: "segmented", role: "group", "aria-labelledby": "link-ttl-label", id: "link-ttl" }, buttons),
    value: () => current };
}

function createError(err, id) {
  if (err instanceof ApiError && (err.status === 404 || err.status === 410)) return `${id} has expired or was deleted.`;
  if (err instanceof ApiError && err.status === 401) return "Session expired — log in again.";
  if (err instanceof ApiError && err.status === 0) return "Couldn't reach the server — try again.";
  if (err instanceof ApiError) return `Couldn't create the link: ${err.message}`;
  return previewError(err, id).text;
}

export function openShareDialog(row) {
  closeSheet();
  const id = row.id;
  const choices = linkTtlChoices(row.expires_at);
  const cap = el("p", { class: "hint link-cap" }, `A link never outlives its file (${expiryPhrase(row.expires_at).toLowerCase()}).`);
  const showCap = (t) => { cap.hidden = !cutByMoreThanAnHour(t, choices.left); };
  const ttl = segment(choices, showCap);
  showCap(choices.def);
  const limitHint = el("p", { class: "hint link-limit-hint", hidden: true },
    "Each person who opens and previews or downloads uses one.");
  const limit = el("input", { type: "checkbox", id: "link-limit" });
  const max = el("input", {
    type: "number", id: "link-max", class: "input link-max", min: "1", max: String(MAX_DOWNLOADS), step: "1", value: "1",
    inputmode: "numeric", disabled: true, "aria-label": "Maximum downloads",
  });
  const error = el("p", { class: "error-text", role: "alert", hidden: true });
  const create = el("button", { class: "btn btn-accent btn-block btn-big", type: "button" }, "Create link");
  const close = el("button", { class: "close-btn", type: "button", "aria-label": "Close" }, icon("x"));
  const name = row.ok && row.meta ? row.meta.name : id;
  const form = el("div", { class: "link-form" },
    el("p", { class: "link-file" }, el("span", { class: "mono" }, id), " · ", name),
    el("fieldset", { class: "field seg-field" },
      el("legend", { class: "label", id: "link-ttl-label" }, "Link expires"), ttl.group, cap),
    el("div", { class: "field" },
      el("div", { class: "link-limit" },
        el("label", { class: "check", for: "link-limit" }, limit, "Limit downloads"), max),
      limitHint),
    error, create,
    el("p", { class: "sheet-lock" }, icon("lock"), "The key stays in the link. The server can't read the file."));
  sheet = el("dialog", { class: "sheet link-sheet", "aria-labelledby": "link-title" },
    el("div", { class: "sheet-grip", "aria-hidden": "true" }),
    el("div", { class: "sheet-head" }, el("h2", { id: "link-title" }, "Share link"), close),
    form);
  const mine = sheet;

  limit.addEventListener("change", () => {
    max.disabled = !limit.checked;
    limitHint.hidden = !limit.checked;
    if (limit.checked) {
      if (!max.value) max.value = "1";
      max.focus();
    }
  });
  close.addEventListener("click", closeSheet);
  sheet.addEventListener("cancel", (e) => {
    if (e.target !== mine) return;
    e.preventDefault();
    closeSheet();
  });
  create.addEventListener("click", async () => {
    const lim = maxDownloadsValue(limit.checked, max.value);
    error.hidden = true;
    if (lim.error) {
      error.textContent = lim.error;
      error.hidden = false;
      max.focus();
      return;
    }
    create.disabled = true;
    let url;
    try {
      const file = await resolveRow(row); // the DEK, unwrapped with the MK (openFileMeta)
      const lk = newLinkKey();
      const body = { wrapped_dek_link: await wrapDekForLink(file.dek, hexToBytes(file.uuid), lk), ttl: ttl.value() };
      if (lim.value !== null) body.max_downloads = lim.value;
      const made = await api("POST", `/api/files/${encodeURIComponent(id)}/links`, { json: body });
      url = linkUrl(location.origin, made.token, lk);
    } catch (err) {
      if (sheet !== mine) return;
      create.disabled = false;
      error.textContent = createError(err, id);
      error.hidden = false;
      return;
    }
    refreshLinks(row.n);
    if (sheet !== mine) return; // closed meanwhile: the URL is dropped unseen
    showResult(mine, form, url);
  });

  document.body.append(sheet);
  sheet.showModal();
  (sheet.querySelector('.seg-btn[aria-pressed="true"]') ?? create).focus();
}

function showResult(dlg, form, url) {
  const field = el("input", { type: "text", id: "link-url", class: "input mono link-url", readonly: true, value: url,
    "aria-label": "Link", spellcheck: "false", autocomplete: "off" });
  const copy = el("button", { type: "button", class: "btn btn-accent", "aria-label": "Copy link" }, icon("copy"), "Copy");
  const done = el("button", { type: "button", class: "btn btn-block btn-big" }, "Done");
  // Synchronous inside the click, so the gesture still counts for the clipboard.
  copy.addEventListener("click", () => copyText(field.value));
  field.addEventListener("focus", () => field.select());
  done.addEventListener("click", closeSheet);
  form.replaceWith(el("div", { class: "link-result" },
    el("label", { class: "field", for: "link-url" }, el("span", { class: "label" }, "Link")),
    el("div", { class: "link-url-row" }, field, copy),
    el("p", { class: "notice link-warning", role: "note" },
      "Shown once. Anyone with this link can read this file, including its note and transcript."),
    done));
  dlg.querySelector("#link-title").textContent = "Link created";
  copy.focus();
}

// ---- the "Public links" section of the file view

let section = null; // { n, root, load } of the open file view

function refreshLinks(n) {
  if (section && section.n === n && section.root.isConnected) section.load();
}

function linkLine(link) {
  const dl = link.max_downloads == null ? `${link.downloads} ${link.downloads === 1 ? "download" : "downloads"}`
    : `${link.downloads}/${link.max_downloads} downloads`;
  const by = link.created_by?.name ? String(link.created_by.name) : "unknown";
  return { head: `${expiryPhrase(link.expires_at)} · ${dl}`, sub: `Created ${relTime(link.created_at)} by ${by}` };
}

async function revoke(link, n) {
  const ok = await confirmDialog({
    title: "Revoke this link?",
    body: "Anyone who opens it will be told it expired or was revoked. A copy someone already downloaded stays with them.",
    confirmLabel: "Revoke",
    danger: true,
  });
  if (!ok) return;
  try {
    await api("DELETE", `/api/links/${encodeURIComponent(link.id)}`);
    toast("Link revoked");
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 401)) toast(`Couldn't revoke the link: ${err.message}`, "error");
  }
  refreshLinks(n);
}

export function linksSection(row, live = () => true) {
  const list = el("ul", { class: "links-list" });
  const root = el("section", { class: "card links-card", "aria-labelledby": "links-title", hidden: true },
    el("h3", { class: "links-title", id: "links-title" }, "Public links"), list);
  const me = { n: row.n, root, load: null };
  let gen = 0;
  me.load = async () => {
    const mine = ++gen;
    let links;
    try {
      links = (await api("GET", `/api/files/${encodeURIComponent(row.id)}/links`)).links;
    } catch {
      links = null; // 401 (the banner explains), 410, offline: keep what is shown
    }
    if (mine !== gen || !live() || !Array.isArray(links)) return;
    list.replaceChildren(...links.map((link) => {
      const { head, sub } = linkLine(link);
      const btn = el("button", { type: "button", class: "btn btn-small link-revoke", "aria-label": "Revoke link" }, "Revoke");
      btn.addEventListener("click", () => revoke(link, row.n));
      return el("li", { class: "link-row", dataset: { id: String(link.id) } },
        el("div", { class: "link-row-main" }, el("span", { class: "link-row-head" }, head), el("span", { class: "link-row-sub" }, sub)),
        btn);
    }));
    root.hidden = links.length === 0;
  };
  section = me;
  me.load();
  return root;
}

fileViewHooks.shareLink = (row) => openShareDialog(row);
fileViewHooks.linksSection = (row, live) => linksSection(row, live);
