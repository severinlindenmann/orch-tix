import { splitHidden } from "./textsafe.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const ICONS = {
  eye: ["M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z", "M9 12a3 3 0 1 0 6 0a3 3 0 1 0-6 0"],
  download: ["M12 4v12M7 11l5 5 5-5M5 20h14"],
  upload: ["M12 16V4M7 9l5-5 5 5M5 20h14"],
  trash: ["M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"],
  plus: ["M12 5v14M5 12h14"],
  x: ["M6 6l12 12M18 6 6 18"],
  copy: ["M10 8h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-8a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2z", "M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"],
  search: ["M4 11a7 7 0 1 0 14 0a7 7 0 1 0-14 0", "M20 20l-3.5-3.5"],
  devices: ["M4 5h10a1.5 1.5 0 0 1 1.5 1.5v6a1.5 1.5 0 0 1-1.5 1.5H4a1.5 1.5 0 0 1-1.5-1.5v-6A1.5 1.5 0 0 1 4 5z", "M1 18h16",
    "M19.2 8h2.6a1.2 1.2 0 0 1 1.2 1.2v9.6a1.2 1.2 0 0 1-1.2 1.2h-2.6a1.2 1.2 0 0 1-1.2-1.2V9.2A1.2 1.2 0 0 1 19.2 8z"],
  logout: ["M15 4h4v16h-4", "M10 17l5-5-5-5M15 12H3"],
  lock: ["M7 11h10a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2z", "M8 11V8a4 4 0 0 1 8 0v3"],
  mic: ["M12 3a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3z", "M5 11a7 7 0 0 0 14 0M12 18v3"],
  check: ["M5 12.5l4.5 4.5L19 7.5"],
  undo: ["M9 14 4 9l5-5", "M4 9h11a5 5 0 0 1 0 10h-3"],
  tickets: ["M5 4h3.5a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z",
    "M10.25 4h3.5a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1h-3.5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z",
    "M15.5 4H19a1 1 0 0 1 1 1v11.5a1 1 0 0 1-1 1h-3.5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z"],
  files: ["M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z", "M14 3v5h5"],
  doc: ["M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z", "M14 3v5h5M9 13h6M9 17h4"],
  image: ["M5.5 4.5h13a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2v-11a2 2 0 0 1 2-2z",
    "M7.4 10a1.6 1.6 0 1 0 3.2 0a1.6 1.6 0 1 0-3.2 0", "M20.5 16l-5-5-9 8.5"],
  audio: ["M4 10v4M8 7v10M12 4v16M16 8v8M20 11v2"],
  alert: ["M12 3.5 2.5 20h19z", "M12 10v4", "M12 17h.01"],
  settings: ["M4 7h10M18 7h2M4 17h4M12 17h8", "M14 7a2 2 0 1 0 4 0a2 2 0 1 0-4 0", "M8 17a2 2 0 1 0 4 0a2 2 0 1 0-4 0"],
  paste: ["M9 3h6a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z",
    "M8 5H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"],
  note: ["M6 3h12a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z", "M9 8h6M9 12h6M9 16h3"],
  pen: ["M4 20h4L19 9l-4-4L4 16z"],
  back: ["M15 5l-7 7 7 7"],
  more: ["M5 12h.01", "M12 12h.01", "M19 12h.01"],
  clock: ["M3.5 12a8.5 8.5 0 1 0 17 0a8.5 8.5 0 1 0-17 0", "M12 7.5V12l3 2"],
  filter: ["M4 6h16M7 12h10M10 18h4"],
  cloudOff: ["M3 3l18 18", "M8.5 6.2A6 6 0 0 1 17.7 10H18a4 4 0 0 1 2.9 6.8", "M17 18H7a4.5 4.5 0 0 1-1.3-8.8"],
  link: ["M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1", "M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1"],
  // ---- TIX on orch-core: the four tabs and the status pills' role icons
  bell: ["M6 16v-5a6 6 0 0 1 12 0v5l2 2H4z", "M10 20h4"],
  list: ["M8 6h12M8 12h12M8 18h12", "M4 6h.01M4 12h.01M4 18h.01"],
  gear: ["M9 12a3 3 0 1 0 6 0a3 3 0 1 0-6 0",
    "M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"],
  dot: ["M8 12a4 4 0 1 0 8 0a4 4 0 1 0-8 0"],
  ring: ["M5 12a7 7 0 1 0 14 0a7 7 0 1 0-14 0"],
  half: ["M5 12a7 7 0 1 0 14 0a7 7 0 1 0-14 0", "M12 5v14"],
};

// Builds DOM without ever parsing HTML: strings become text nodes, handlers must be functions.
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs ?? {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "style" || key === "innerHTML" || key === "outerHTML") throw new Error(`el(): "${key}" is not allowed`);
    if (/^on/i.test(key)) {
      if (typeof value !== "function") throw new Error(`el(): ${key} must be a function`);
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "class") {
      node.className = value;
    } else if (key === "dataset") {
      Object.assign(node.dataset, value);
    } else {
      node.setAttribute(key, value === true ? "" : String(value));
    }
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// Ticket text as nodes: each hidden character (textsafe.js) becomes a visible <U+XXXX> badge, so nothing can
// reorder or hide what the human reads. Use it for every decrypted text a decision depends on.
export function shown(text) {
  return splitHidden(text).map((r) => (r.hidden ? el("span", { class: "hidden-char", title: "hidden character" }, r.hidden) : r.text));
}

export function icon(name) {
  const paths = ICONS[name];
  if (!paths) throw new Error(`unknown icon: ${name}`);
  const svg = document.createElementNS(SVG_NS, "svg");
  for (const [k, v] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.8",
    "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", class: `icon icon-${name}` })) svg.setAttribute(k, v);
  for (const d of paths) {
    const p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}

export function hydrateIcons(root = document) {
  for (const slot of root.querySelectorAll("[data-icon]")) {
    if (!slot.querySelector("svg")) slot.prepend(icon(slot.dataset.icon));
  }
}

let toastRoot = null;
export function toast(msg, kind = "info") {
  if (!toastRoot) {
    toastRoot = el("div", { class: "toasts", role: "status", "aria-live": "polite" });
    document.body.append(toastRoot);
  }
  const t = el("div", { class: kind === "error" ? "toast toast-error" : "toast" }, msg);
  toastRoot.append(t);
  setTimeout(() => t.remove(), kind === "error" ? 6000 : 2500);
  return t;
}

export function confirmDialog({ title, body, confirmLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    const titleId = `dlg-${crypto.getRandomValues(new Uint32Array(1))[0].toString(36)}`;
    const cancel = el("button", { type: "button", class: "btn" }, "Cancel");
    const ok = el("button", { type: "button", class: danger ? "btn btn-danger" : "btn btn-accent" }, confirmLabel);
    const dlg = el("dialog", { class: "modal confirm", "aria-labelledby": titleId },
      el("h2", { class: "modal-title", id: titleId }, title),
      el("p", { class: "modal-body" }, body),
      el("div", { class: "modal-actions" }, cancel, ok));
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      dlg.close();
      dlg.remove();
      resolve(value);
    };
    cancel.addEventListener("click", () => finish(false));
    ok.addEventListener("click", () => finish(true));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); finish(false); });
    dlg.addEventListener("click", (e) => {
      const r = dlg.getBoundingClientRect();
      const outside = e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
      if (e.target === dlg && outside) finish(false);
    });
    document.body.append(dlg);
    dlg.showModal();
    (danger ? cancel : ok).focus();
  });
}

// The confirm sheet of a gate (approve, accept as done; design system "Dialog", phone: a bottom sheet). A native
// <dialog> with showModal: focus starts on Cancel, the safe option is on top, Esc cancels, a backdrop tap does
// nothing (the confirm is the checkpoint). `detail` is a short line under the body (the hash being approved).
// Focus returns to the opener. Resolves true on confirm, false otherwise. Never window.confirm().
export function confirmSheet({ title, body, detail = "", confirmLabel = "Confirm", cancelLabel = "Cancel" }) {
  return new Promise((resolve) => {
    const opener = document.activeElement;
    const titleId = `dlg-${crypto.getRandomValues(new Uint32Array(1))[0].toString(36)}`;
    const cancel = el("button", { type: "button", class: "btn btn-big" }, cancelLabel);
    const ok = el("button", { type: "button", class: "btn btn-big btn-primary" }, confirmLabel);
    const dlg = el("dialog", { class: "modal sheet-confirm", "aria-labelledby": titleId },
      el("h2", { class: "modal-title", id: titleId }, title),
      el("p", { class: "sheet-confirm-body" }, body),
      detail ? el("p", { class: "sheet-confirm-detail mono" }, detail) : null,
      el("div", { class: "sheet-confirm-actions" }, cancel, ok));
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      dlg.close();
      dlg.remove();
      if (opener && opener.isConnected && typeof opener.focus === "function") opener.focus();
      resolve(value);
    };
    cancel.addEventListener("click", () => finish(false));
    ok.addEventListener("click", () => finish(true));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); finish(false); });
    document.body.append(dlg);
    dlg.showModal();
    cancel.focus();
  });
}

// A small modal form (rename, change expiry). `submit` runs on Save and returns true to close,
// or an error string to show inline and keep the dialog open (anything else closes it unsaved).
// Resolves true once saved, false otherwise.
export function formDialog({ title, fields, confirmLabel = "Save", submit, className = "" }) {
  return new Promise((resolve) => {
    const titleId = `dlg-${crypto.getRandomValues(new Uint32Array(1))[0].toString(36)}`;
    const error = el("p", { class: "error-text", role: "alert", hidden: true });
    const cancel = el("button", { type: "button", class: "btn" }, "Cancel");
    const ok = el("button", { type: "submit", class: "btn btn-accent" }, confirmLabel);
    const form = el("form", { class: "form-dialog-body", novalidate: true },
      fields, error, el("div", { class: "modal-actions" }, cancel, ok));
    const dlg = el("dialog", { class: `modal form-dialog ${className}`.trim(), "aria-labelledby": titleId },
      el("h2", { class: "modal-title", id: titleId }, title), form);
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      dlg.close();
      dlg.remove();
      resolve(value);
    };
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      ok.disabled = true;
      error.hidden = true;
      let result;
      try {
        result = await submit();
      } catch (err) {
        console.error(`${title}: saving`, err);
        result = "Something went wrong — try again.";
      }
      ok.disabled = false;
      if (typeof result === "string") {
        error.textContent = result;
        error.hidden = false;
      } else {
        finish(result === true);
      }
    });
    cancel.addEventListener("click", () => finish(false));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); finish(false); });
    document.body.append(dlg);
    dlg.showModal();
    (form.querySelector("input, select") ?? ok).focus();
  });
}

// A popover behind a toggle button: the "More actions" menu of the file view, the list's filter menu.
// Closes on a click outside, on Escape (which then goes no further, so it doesn't also close the
// file view) and after any menu item is chosen. Returns { close }.
export function wirePopover(button, pop) {
  const set = (open) => {
    pop.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
  };
  const onDocDown = (e) => {
    if (!pop.hidden && !pop.contains(e.target) && !button.contains(e.target)) set(false);
  };
  button.setAttribute("aria-expanded", "false");
  button.addEventListener("click", () => {
    set(pop.hidden);
    if (!pop.hidden) pop.querySelector('[role="menuitem"]:not([hidden]), select, button:not([hidden])')?.focus();
  });
  pop.addEventListener("click", (e) => {
    if (e.target.closest('[role="menuitem"]')) set(false);
  });
  const onKey = (e) => {
    if (e.key !== "Escape" || pop.hidden) return;
    e.stopPropagation();
    set(false);
    button.focus();
  };
  pop.addEventListener("keydown", onKey);
  button.addEventListener("keydown", onKey);
  document.addEventListener("pointerdown", onDocDown);
  return {
    close: () => set(false),
    dispose: () => document.removeEventListener("pointerdown", onDocDown),
  };
}
