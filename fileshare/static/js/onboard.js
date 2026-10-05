// fileshare/static/js/onboard.js
import { b64u } from "./crypto.js";
import { api, ApiError } from "./api.js";
import { el, toast } from "./ui.js";
import { buildCode, buildCommands, defaultOs, deviceSlug, nextState, TERMINAL, TOKEN_TTL_MS } from "./onboard-cmd.js";
import { approveDevice, rejectDevice, localFingerprint, whileBusy } from "./approve.js";
import { installPendingIndicator } from "./pending.js";

let state = null;
const NAME_HINT = "Leave empty to use the computer's hostname. Give each repo or machine its own name, or two devices end up with the same one.";

function stopTimers(s) {
  s.timers.forEach(clearInterval);
  s.timers = [];
}

function copyButton(text) {
  const b = el("button", { class: "btn btn-small", type: "button" }, "Copy");
  b.addEventListener("click", async () => {
    await navigator.clipboard.writeText(text);
    toast("Copied — run it inside the repo on the new device");
  });
  return b;
}

function panel(kind, main, inspect) {
  return el(
    "div",
    { class: `tab-panel tab-${kind}`, role: "tabpanel", id: `onboard-panel-${kind}` },
    el("div", { class: "codebox" }, el("code", { class: "onboard-cmd mono" }, main), copyButton(main)),
    el(
      "details",
      { class: "onboard-inspect" },
      el("summary", {}, "Inspect the script first"),
      el("div", { class: "codebox" }, el("code", { class: "onboard-cmd mono" }, inspect), copyButton(inspect)),
    ),
  );
}

function showCommand(s, code) {
  s.code = code;
  const c = buildCommands(location.origin, code, s.name.value);
  const panels = { posix: panel("posix", c.posix, c.posixInspect), windows: panel("windows", c.windows, c.windowsInspect) };
  const tabs = {
    posix: el("button", { type: "button", role: "tab", class: "tab", "aria-controls": "onboard-panel-posix" }, "macOS / Linux"),
    windows: el("button", { type: "button", role: "tab", class: "tab", "aria-controls": "onboard-panel-windows" }, "Windows"),
  };
  const select = (k) => {
    for (const key of Object.keys(tabs)) {
      tabs[key].setAttribute("aria-selected", String(key === k));
      panels[key].hidden = key !== k;
    }
  };
  tabs.posix.addEventListener("click", () => select("posix"));
  tabs.windows.addEventListener("click", () => select("windows"));
  s.result.replaceChildren(
    el("div", { class: "result-label" }, "Run this on the new device, inside the repo:"),
    el("div", { class: "tabs", role: "tablist" }, tabs.posix, tabs.windows),
    panels.posix,
    panels.windows,
    el("p", { class: "hint" }, "Expires in ", s.countdown, " · single use · the device then waits for your approval"),
  );
  select(s.os || defaultOs(navigator.userAgent));
  tabs.posix.addEventListener("click", () => { s.os = "posix"; });
  tabs.windows.addEventListener("click", () => { s.os = "windows"; });
  s.result.hidden = false;
  s.gen.hidden = true;
}

function setStatus(s, text, cls = "") {
  s.status.className = `onboard-status ${cls}`.trim();
  s.status.textContent = text;
}

async function renderPending(s, dev) {
  s.result.replaceChildren();
  s.result.hidden = true;
  let fp = null;
  try {
    fp = await localFingerprint(dev);
  } catch {
    fp = null;
  }
  if (state !== s || s.fsm.phase !== "pending") return;
  const match = fp !== null && fp === dev.fingerprint;
  const approve = el("button", { class: "btn btn-accent", type: "button" }, "Approve");
  const reject = el("button", { class: "btn btn-danger", type: "button" }, "Reject");
  const act = (fn, verb) => () =>
    whileBusy([approve, reject], () => fn(dev))
      .then((ok) => ok && poll(s))
      .catch((e) => {
        if (!(e instanceof ApiError)) console.error(`${verb} ${dev.id}`, e);
        toast(`Couldn't ${verb} — try again`, "error");
      });
  approve.addEventListener("click", act(approveDevice, "approve"));
  reject.addEventListener("click", act(rejectDevice, "reject"));
  s.pending.replaceChildren(
    el("p", { class: "onboard-pending-title" }, "Waiting for approval"),
    el("p", { class: "onboard-device" }, `${dev.name} · ${dev.project} · ${dev.hostname || "—"} · ${dev.platform || "?"}`),
    el("p", { class: "onboard-fp mono" }, fp || "—"),
    match
      ? el("p", { class: "hint" }, "Approve only if the terminal on the new device shows exactly this fingerprint.")
      : el("p", { class: "fp-mismatch" }, "Fingerprint mismatch — do not approve. Reject this device."),
    el("div", { class: "sheet-actions" }, ...(match ? [reject, approve] : [reject])),
  );
  s.pending.hidden = false;
  setStatus(s, "");
}

function render(s) {
  const { phase, device } = s.fsm;
  if (phase === s.rendered) return;
  s.rendered = phase;
  if (phase === "waiting") {
    setStatus(s, "Waiting for the device…");
    return;
  }
  if (phase === "pending") {
    renderPending(s, device);
    return;
  }
  stopTimers(s);
  s.result.replaceChildren();
  s.result.hidden = true;
  s.pending.replaceChildren();
  s.pending.hidden = true;
  if (phase === "approved") {
    setStatus(s, `Approved: ${device.name} · ${device.project} ✓`, "ok");
    window.dispatchEvent(new CustomEvent("fs:devices-changed"));
  } else if (phase === "rejected") {
    setStatus(s, `Rejected: ${device.name} — it never received the key`, "error");
    window.dispatchEvent(new CustomEvent("fs:devices-changed"));
  } else {
    setStatus(s, "Link expired", "expired");
    const again = el("button", { class: "btn btn-accent", type: "button" }, "Generate new link");
    again.addEventListener("click", () => {
      again.remove();
      generate(s).catch((e) => onError(s, e));
    });
    s.status.after(again);
  }
}

function tick(s) {
  if (state !== s || TERMINAL.has(s.fsm.phase)) return;
  const left = Math.max(0, s.fsm.expiresAt - Date.now());
  s.countdown.textContent = `${Math.floor(left / 60000)}:${String(Math.floor((left % 60000) / 1000)).padStart(2, "0")}`;
  s.fsm = nextState(s.fsm, undefined, Date.now());
  render(s);
}

async function poll(s) {
  if (state !== s || !s.tokenId || TERMINAL.has(s.fsm.phase)) return;
  let tok;
  try {
    tok = await api("GET", `/api/onboarding-tokens/${s.tokenId}`);
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 404)) return;
    tok = null;
  }
  if (state !== s) return;
  s.fsm = nextState(s.fsm, tok, Date.now());
  render(s);
}

function onError(s, e) {
  if (state !== s) return;
  s.gen.hidden = false;
  s.gen.disabled = false;
  setStatus(s, e instanceof ApiError && e.status === 401 ? "Session expired — log in again." : "Couldn't create a link — try again.", "error");
}

async function generate(s) {
  s.gen.disabled = true;
  setStatus(s, "Generating…");
  const lookup = crypto.getRandomValues(new Uint8Array(16));
  const tok = await api("POST", "/api/onboarding-tokens", { json: { lookup: b64u(lookup) } });
  if (state !== s) {
    api("DELETE", `/api/onboarding-tokens/${tok.id}`).catch(() => {});
    return;
  }
  s.tokenId = tok.id;
  // R9: count down from the local clock (a skewed clock can't expire the link early); polling keeps
  // the server authoritative, since a 404 or a used token moves the state on.
  s.fsm = { phase: "waiting", expiresAt: Date.now() + TOKEN_TTL_MS, device: null };
  s.rendered = null;
  showCommand(s, buildCode(lookup));
  render(s);
  s.timers.push(setInterval(() => tick(s), 1000), setInterval(() => poll(s), 2000));
  tick(s);
}

export function closeOnboardModal() {
  const s = state;
  if (!s) return;
  state = null;
  stopTimers(s);
  if (s.tokenId && s.fsm.phase === "waiting") api("DELETE", `/api/onboarding-tokens/${s.tokenId}`).catch(() => {});
  if (s.fsm.phase === "pending") {
    toast(`${s.fsm.device.name} is still waiting — approve it on the Devices page`);
    window.dispatchEvent(new CustomEvent("fs:devices-changed"));
  }
  s.dlg.close();
  s.dlg.remove();
}

export function openOnboardModal() {
  if (state) return;
  const status = el("p", { class: "onboard-status", role: "status" }, "");
  const countdown = el("span", { class: "onboard-countdown mono" }, "");
  const result = el("div", { class: "onboard-result", hidden: "" });
  const pending = el("div", { class: "onboard-pending", hidden: "", "aria-live": "polite" });
  const gen = el("button", { class: "btn btn-accent", type: "button" }, "Generate");
  const name = el("input", {
    class: "input", type: "text", id: "onboard-device-name", maxlength: "64", autocomplete: "off",
    autocapitalize: "off", spellcheck: "false", placeholder: "Device name (optional)",
  });
  const nameNote = el("p", { class: "hint", id: "onboard-name-note" }, NAME_HINT);
  const close = el("button", { class: "icon-btn", type: "button", "aria-label": "Close" }, "×");
  const dlg = el(
    "dialog",
    { class: "onboard", "aria-labelledby": "onboard-title" },
    el("div", { class: "modal-head" }, el("h2", { id: "onboard-title" }, "Onboard a new device"), close),
    el("p", { class: "hint" }, "Creates a one-time link, valid for 15 minutes. The device then shows a fingerprint; it gets the key only after you approve that fingerprint here."),
    el("label", { class: "field" }, el("span", { class: "label" }, "Device name ", el("span", { class: "label-opt" }, "optional")), name),
    nameNote,
    gen,
    result,
    pending,
    status,
  );
  const s = {
    dlg, result, pending, status, countdown, gen, name, nameNote, code: null, os: null,
    tokenId: null, timers: [], rendered: null,
    fsm: { phase: "idle", expiresAt: 0, device: null },
  };
  state = s;
  name.addEventListener("input", () => {
    const slug = deviceSlug(name.value);
    nameNote.textContent = name.value && slug !== name.value ? `The device will be called "${slug}".` : NAME_HINT;
    if (s.code && s.fsm.phase === "waiting") showCommand(s, s.code);   // same one-time link, new --device
  });
  gen.addEventListener("click", () => generate(s).catch((e) => onError(s, e)));
  close.addEventListener("click", closeOnboardModal);
  dlg.addEventListener("cancel", (e) => {
    e.preventDefault();
    closeOnboardModal();
  });
  document.body.append(dlg);
  dlg.showModal();
}

if (typeof document !== "undefined") {
  // #onboard-btn in the top bar and the empty state's button both carry data-action="onboard" (Task 18).
  for (const b of document.querySelectorAll('[data-action="onboard"]')) b.addEventListener("click", openOnboardModal);
  installPendingIndicator();
}
