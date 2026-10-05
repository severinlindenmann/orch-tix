// fileshare/static/js/pair.js — pairing this phone with a desktop workspace (orch-core remote humans,
// spec §6.4): the /pair page the desktop's QR code or "Copy pairing link" points at, and the Settings
// card "Pair with a desktop" (paste the link). Key handling is in pairing.js.
//
// /pair reads the key from the URL fragment and strips it from the address bar at once. iOS opens a
// scanned link in Safari, not in the installed app, and the two have separate storage: outside the
// installed app this page stores nothing and offers "Copy pairing link" to paste in TIX → Settings →
// Pair with a desktop. In the installed app (Android may open the link there directly) and after a
// paste in Settings it shows the check code; only a Yes stores the pairing. The key is never sent,
// logged or put in the page.
import { LABELS, getValue } from "./db.js";
import { checkCode, forgetPairing, pairings, parsePairFragment, parsePairLink, storePairing } from "./pairing.js";
import { copyText } from "./clipboard.js";
import { shortAge } from "./format.js";
import { confirmDialog, el, icon, toast } from "./ui.js";

const PAIRED_TEXT = "Paired with this workspace";
const UNKNOWN = "A workspace";

export function isStandalone(win = globalThis) {
  try {
    if (win.matchMedia?.("(display-mode: standalone)")?.matches) return true;
  } catch {
    /* no matchMedia */
  }
  return win.navigator?.standalone === true;
}

async function spaceLabel(space) {
  try {
    const v = await getValue(LABELS, space);
    return (v && typeof v === "object" ? v.label : v) || null;
  } catch {
    return null;
  }
}

// The check-code step for a parsed pairing. Resolves true when the human said Yes and it was stored.
// `parsed.rawKey` is zeroed on every way out (stored, No, or an error).
export async function confirmPairing(container, parsed, { onDone } = {}) {
  let code;
  try {
    code = await checkCode(parsed.rawKey);
  } catch {
    parsed.rawKey.fill(0);
    container.replaceChildren(el("p", { class: "error-text", role: "alert" }, "This browser can't pair. Use the desktop's Apply instead."));
    return false;
  }
  const label = (await spaceLabel(parsed.space)) || UNKNOWN;
  return new Promise((resolve) => {
    const yes = el("button", { type: "button", class: "btn btn-primary btn-big" }, "Yes, pair");
    const no = el("button", { type: "button", class: "btn btn-big" }, "No");
    const status = el("p", { class: "pair-status", role: "status", "aria-live": "polite" });
    const finish = (ok) => { onDone?.(ok); resolve(ok); };
    yes.addEventListener("click", async () => {
      yes.disabled = no.disabled = true;
      try {
        await storePairing(null, { ...parsed, label });
      } catch {
        parsed.rawKey.fill(0);
        status.textContent = "Couldn't store the pairing in this browser.";
        finish(false);
        return;
      }
      container.replaceChildren(el("p", { class: "pair-done", role: "status" }, icon("check"), el("span", {}, PAIRED_TEXT)),
        el("p", { class: "hint" }, `${label}: what you send from this phone now applies on the desktop directly, as its phone settings allow.`),
        el("p", { class: "hint" }, "If you pasted the link, clear the clipboard now: copy any other text."));
      finish(true);
    });
    no.addEventListener("click", () => {
      parsed.rawKey.fill(0);
      container.replaceChildren(el("p", { class: "pair-status", role: "status" }, "Not paired. Nothing was stored."),
        el("p", { class: "hint" }, "Start again on the desktop: Mission Control → Workspace & addons → Phones → Pair a phone."));
      finish(false);
    });
    container.replaceChildren(
      el("p", { class: "muted" }, label),
      el("p", { class: "pair-code mono", "aria-label": `Check code ${code.split("").join(" ")}` }, `${code.slice(0, 3)} ${code.slice(3)}`),
      el("p", { class: "pair-question" }, "Does the desktop show the same code?"),
      el("div", { class: "pair-actions" }, yes, no),
      status);
    yes.focus();
  });
}

// ---- the /pair page

function notInApp(main, link) {
  const copy = el("button", { type: "button", class: "btn btn-primary btn-big" }, icon("copy"), el("span", {}, "Copy pairing link"));
  copy.addEventListener("click", () => copyText(link));
  main.replaceChildren(
    el("h1", { class: "pair-title" }, "Open this in the TIX app"),
    el("p", { class: "muted" }, "This browser keeps its own storage, so the pairing has to go into the installed app."),
    copy,
    el("ol", { class: "pair-steps" },
      el("li", {}, "Open TIX from your home screen"),
      el("li", {}, "Settings → Pair with a desktop"),
      el("li", {}, "Paste, then compare the code with the desktop")),
    el("p", { class: "hint" }, "After pasting, clear the clipboard: copy any other text. The link holds the pairing key."),
    el("p", { class: "hint" }, "Nothing was stored in this browser."));
}

function badLink(main) {
  main.replaceChildren(el("h1", { class: "pair-title" }, "This pairing link doesn't work"),
    el("p", { class: "muted" }, "Copy it again from the desktop: Mission Control → Workspace & addons → Phones → Pair a phone → Copy pairing link."),
    el("a", { class: "btn btn-big", href: "/settings#pair" }, "Open Settings"));
}

export async function startPairPage() {
  // First thing: take the key out of the address bar (and so out of history and any later share).
  const hash = location.hash;
  history.replaceState(null, "", "/pair");
  const main = document.getElementById("pair-main");
  const parsed = parsePairFragment(hash);
  if (!parsed) {
    badLink(main);
    return;
  }
  if (!isStandalone()) {
    parsed.rawKey.fill(0);
    notInApp(main, `${location.origin}/pair${hash.startsWith("#") ? hash : `#${hash}`}`);
    return;
  }
  main.replaceChildren(el("h1", { class: "pair-title" }, "Pair with a desktop"));
  const box = el("div", { class: "pair-box" });
  main.append(box);
  await confirmPairing(box, parsed, {
    onDone: (ok) => box.append(el("a", { class: "btn btn-big", href: ok ? "/" : "/settings#pair" }, ok ? "Open Needs you" : "Open Settings")),
  });
}

// ---- the Settings card "Pair with a desktop"

async function renderPairList(list) {
  let rows;
  try {
    rows = await pairings(null);
  } catch {
    list.replaceChildren(el("p", { class: "muted" }, "This browser can't store pairings."));
    return;
  }
  if (!rows.length) {
    list.replaceChildren(el("p", { class: "muted" }, "Not paired with any desktop."));
    return;
  }
  const items = await Promise.all(rows.map(async (p) => {
    const label = (await spaceLabel(p.space)) || p.label || UNKNOWN;
    const forget = el("button", { type: "button", class: "btn" }, "Forget");
    forget.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: `Forget the pairing with ${label}?`,
        body: "Answers from this phone then wait for an Apply on the desktop. Revoke the phone on the desktop too (Workspace & addons).",
        confirmLabel: "Forget", danger: true,
      });
      if (!ok) return;
      try {
        await forgetPairing(null, p.space);
        toast("Pairing forgotten");
      } catch {
        toast("Couldn't forget the pairing", "error");
      }
      await renderPairList(list);
    });
    return el("div", { class: "join-row pair-row", dataset: { space: p.space } },
      el("div", { class: "join-main" },
        el("span", { class: "pill r-ok" }, icon("check"), el("span", {}, "Paired")),
        el("b", {}, label),
        el("span", { class: "muted" }, `paired ${shortAge(p.paired_at)}`)),
      forget);
  }));
  list.replaceChildren(...items);
}

export function mountPairCard(doc = document) {
  const form = doc.getElementById("pair-form");
  const input = doc.getElementById("pair-link");
  const step = doc.getElementById("pair-step");
  const list = doc.getElementById("pair-list");
  const err = doc.getElementById("pair-error");
  if (!form || !input || !step || !list) return;
  renderPairList(list);
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const parsed = parsePairLink(input.value, location.origin);
    input.value = "";                      // the key leaves the page's DOM either way
    if (!parsed) {
      err.textContent = "That isn't a pairing link for this TIX. Copy it again from the desktop.";
      err.hidden = false;
      return;
    }
    err.hidden = true;
    form.hidden = true;
    step.hidden = false;
    await confirmPairing(step, parsed, {
      onDone: () => {
        form.hidden = false;
        renderPairList(list);
      },
    });
  });
}

if (typeof document !== "undefined" && document.body?.classList.contains("page-pair")) startPairPage();
