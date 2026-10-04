// Inbound one-time upload links (spec §18, Task 4): the owner's "Upload links" dialog (create, list,
// revoke) and, on the files page, auto-adopting any file a stranger dropped through one of them.
//
// The link's private key (LPRIV) only ever exists unwrapped in memory here, for as long as it takes
// to recover a pending upload's DEK; it is never sent anywhere. The server holds LPRIV only sealed
// under the owner's master key, and never sees a dropped file's DEK in the clear (§18's ECDH seal).
import { api, ApiError } from "./api.js";
import {
  adoptWrappedDek, b64u, IntegrityError, newUploadLink, openSealedDek, openUploadLabel, openUploadLinkKey,
} from "./crypto.js";
import { confirmDialog, el, icon, toast } from "./ui.js";
import { copyText } from "./clipboard.js";
import { relTime } from "./format.js";
import { loadKeys } from "./keystore.js";

// [ttl, label]; the API takes exactly these ttl values (global-constraints.md).
export const DROP_TTLS = [["1h", "1 hour"], ["1d", "1 day"], ["7d", "7 days"]];
export const DEFAULT_TTL = "1d";

export function dropUrl(origin, token, pub) {
  return `${origin}/u/${token}#${b64u(pub)}`;
}

// The owner-facing wording for each state the server's GET /api/upload-links reports.
export function stateText(link) {
  switch (link.state) {
    case "waiting": return "Waiting";
    case "pending": return "Received, finishing…";
    case "received": return `Received as ${link.file}`;
    case "expired": return "Expired";
    case "revoked": return "Revoked";
    default: return "";
  }
}

// A fresh upload link: a P-256 keypair sealed under mkKey, POSTed, and its drop URL (the pub key
// travels only in the fragment, which the server and this request never see).
export async function createDropLink(apiCall, mkKey, keyVersion, { label, ttl }) {
  const link = await newUploadLink(mkKey, label);
  const made = await apiCall("POST", "/api/upload-links", { json: {
    uuid: link.uuidHex, key_version: keyVersion, wrapped_lpriv: link.wrappedLpriv, enc_label: link.encLabel, ttl,
  } });
  return { id: made.id, url: dropUrl(location.origin, made.token, link.pub), expiresAt: made.expires_at };
}

const isAlreadyDone = (err) => err instanceof ApiError && (err.code === "already_adopted" || err.code === "not_pending");

// Adopts every link whose server-reported state is "pending": recovers the DEK a drop-page upload
// sealed to the link, re-wraps it under mkKey, and claims the file. Never throws: any link that fails
// lands in `failed` or `undecryptable` instead, and the rest still get a chance. A 409 (someone else
// already adopted it, or it was revoked out from under us) counts as done, not a failure.
//
// `undecryptable` and `failed` are kept apart (final review): an IntegrityError from
// openUploadLinkKey/openSealedDek means the upload is tampered or was sealed to a key this account
// doesn't hold — that will never succeed on retry, so it's shown to the owner as "cannot decrypt".
// A network error or a 5xx from the adopt POST is transient: it just retries next time, silently.
export async function adoptPending(apiCall, mkKey, links) {
  let adopted = 0;
  const failed = [];
  const undecryptable = [];
  for (const link of links) {
    if (link.state !== "pending") continue;
    let dek;
    try {
      const linkKey = await openUploadLinkKey(mkKey, link.uuid, link.wrapped_lpriv);
      dek = await openSealedDek(linkKey, link.uuid, link.pending.file_uuid, link.pending.sealed_dek);
    } catch (err) {
      if (err instanceof IntegrityError) undecryptable.push(link.id);
      else failed.push(link.id);
      continue;
    }
    try {
      const wrappedDek = await adoptWrappedDek(mkKey, dek, link.pending.file_uuid);
      await apiCall("POST", `/api/upload-links/${encodeURIComponent(link.id)}/adopt`, { json: { wrapped_dek: wrappedDek } });
      adopted += 1;
    } catch (err) {
      if (isAlreadyDone(err)) { adopted += 1; continue; }
      failed.push(link.id);
    }
  }
  return { adopted, failed, undecryptable };
}

// Whether a "pending" link's upload can never be decrypted under mkKey (a tampered sealed_dek, or one
// sealed to a link key this account doesn't hold) — used by the dialog to show "Can't decrypt —
// discard" instead of "Received, finishing…" without waiting for an actual adopt attempt.
export async function isPendingUndecryptable(mkKey, link) {
  if (!link.pending) return false;
  try {
    const linkKey = await openUploadLinkKey(mkKey, link.uuid, link.wrapped_lpriv);
    await openSealedDek(linkKey, link.uuid, link.pending.file_uuid, link.pending.sealed_dek);
    return false;
  } catch (err) {
    return err instanceof IntegrityError;
  }
}

// ---- the dialog

let dlg = null;

function closeDialog() {
  if (!dlg) return;
  const d = dlg;
  dlg = null;
  d.close();
  d.remove();
}

async function labelOf(mkKey, link) {
  try {
    return await openUploadLabel(mkKey, link.uuid, link.enc_label);
  } catch {
    return "(unreadable label)";
  }
}

async function revokeLink(link, mkKey, list) {
  const ok = await confirmDialog({
    title: "Revoke this upload link?",
    body: "Anyone who still has it will be told it expired or was revoked. An upload it already carries is discarded.",
    confirmLabel: "Revoke",
    danger: true,
  });
  if (!ok) return;
  try {
    await api("DELETE", `/api/upload-links/${encodeURIComponent(link.id)}`);
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 401)) toast(`Couldn't revoke the link: ${err.message}`, "error");
  }
  await refreshList(list, mkKey);
}

async function refreshList(list, mkKey) {
  let links;
  try {
    links = (await api("GET", "/api/upload-links")).links;
  } catch {
    return; // 401 (the banner explains) or offline: keep what is shown
  }
  if (!dlg) return;
  const rows = await Promise.all(links.map(async (link) => {
    const label = await labelOf(mkKey, link);
    const revocable = link.state === "waiting" || link.state === "pending";
    const undecryptable = link.state === "pending" && await isPendingUndecryptable(mkKey, link);
    const state = undecryptable ? "Can't decrypt — discard" : stateText(link);
    const revoke = el("button", { type: "button", class: "btn btn-small link-revoke" }, "Revoke");
    revoke.addEventListener("click", () => revokeLink(link, mkKey, list));
    return el("li", { class: "link-row", dataset: { id: String(link.id) } },
      el("div", { class: "link-row-main" },
        el("span", { class: "link-row-head" }, `${label || "Unlabeled"} · ${state}`),
        el("span", { class: "link-row-sub" }, `Created ${relTime(link.created_at)}`)),
      revocable ? revoke : null);
  }));
  if (!dlg) return;
  list.replaceChildren(...rows);
  if (links.length === 0) list.append(el("li", { class: "empty-inline" }, "No upload links yet."));
}

function showResult(result, url) {
  const field = el("input", { type: "text", class: "input mono link-url", readonly: true, value: url,
    "aria-label": "Link", spellcheck: "false", autocomplete: "off" });
  const copy = el("button", { type: "button", class: "btn btn-accent", "aria-label": "Copy link" }, icon("copy"), "Copy");
  field.addEventListener("focus", () => field.select());
  copy.addEventListener("click", () => copyText(field.value));
  result.replaceChildren(
    el("span", { class: "label" }, "Link"),
    el("div", { class: "link-url-row" }, field, copy),
    el("p", { class: "notice link-warning", role: "note" }, "Shown once. Copy it now — the server never keeps the key."));
  result.hidden = false;
}

export function openDropDialog() {
  closeDialog();
  const ttl = el("select", { class: "input", id: "droplink-ttl", "aria-label": "Link expires" },
    ...DROP_TTLS.map(([value, text]) => el("option", { value, selected: value === DEFAULT_TTL ? "" : null }, text)));
  const label = el("input", { type: "text", id: "droplink-label", class: "input", maxlength: "200",
    placeholder: "Optional label", autocomplete: "off" });
  const error = el("p", { class: "error-text", role: "alert", hidden: true });
  const create = el("button", { class: "btn btn-accent btn-block btn-big", type: "button" }, "Create link");
  const form = el("div", { class: "link-form" },
    el("label", { class: "field", for: "droplink-ttl" }, el("span", { class: "label" }, "Link expires"), ttl),
    el("label", { class: "field", for: "droplink-label" }, el("span", { class: "label" }, "Label"), label),
    error, create);
  const result = el("div", { class: "link-result", hidden: true });
  const list = el("ul", { class: "links-list" });
  const close = el("button", { class: "close-btn", type: "button", "aria-label": "Close" }, icon("x"));
  dlg = el("dialog", { class: "sheet droplink-sheet", "aria-labelledby": "droplink-title" },
    el("div", { class: "sheet-grip", "aria-hidden": "true" }),
    el("div", { class: "sheet-head" }, el("h2", { id: "droplink-title" }, "Upload links"), close),
    form, result,
    el("section", { class: "card links-card" }, el("h3", { class: "links-title" }, "Existing links"), list));
  const mine = dlg;

  close.addEventListener("click", closeDialog);
  dlg.addEventListener("cancel", (e) => {
    if (e.target !== mine) return;
    e.preventDefault();
    closeDialog();
  });

  create.addEventListener("click", async () => {
    error.hidden = true;
    create.disabled = true;
    const keys = await loadKeys();
    if (!keys) {
      create.disabled = false;
      error.textContent = "Session expired — log in again.";
      error.hidden = false;
      return;
    }
    let made;
    try {
      made = await createDropLink(api, keys.mk, keys.keyVersion, { label: label.value.trim(), ttl: ttl.value });
    } catch (err) {
      if (dlg !== mine) return;
      create.disabled = false;
      error.textContent = err instanceof ApiError ? `Couldn't create the link: ${err.message}` : "Something went wrong.";
      error.hidden = false;
      return;
    }
    if (dlg !== mine) return;
    create.disabled = false;
    label.value = "";
    showResult(result, made.url);
    await refreshList(list, keys.mk);
  });

  document.body.append(dlg);
  dlg.showModal();
  ttl.focus();
  (async () => {
    const keys = await loadKeys();
    if (keys && dlg === mine) await refreshList(list, keys.mk);
  })();
}
