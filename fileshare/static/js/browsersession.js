// This browser's session name (spec §14 A/B). Not an entry module: files.js wires the header
// button, devices.js the "Browsers" section. The name is stored in the clear on the server and
// is untrusted like any server string: it only ever reaches the DOM through textContent.
import { api, ApiError } from "./api.js";
import { el, formDialog, toast } from "./ui.js";

export const NAME_MAX = 40; // code points, as the server counts them
// maxlength counts UTF-16 units, so 40 emoji need 80; the submit check enforces the real limit.
const INPUT_MAXLENGTH = 2 * NAME_MAX;
const UNNAMED = "unnamed";

export async function loadSessionName() {
  try {
    const { name } = await api("GET", "/api/sessions/self");
    return typeof name === "string" ? name : "";
  } catch {
    return null; // 401 is handled by the banner; the caller just shows nothing
  }
}

// Opens the rename dialog; resolves to the new name, or null when cancelled.
export async function renameBrowser(current) {
  const input = el("input", { id: "browser-name-input", class: "input", type: "text", maxlength: String(INPUT_MAXLENGTH),
    autocomplete: "off", spellcheck: "false", value: current ?? "" });
  let saved = null;
  const ok = await formDialog({
    title: "Rename this browser",
    className: "rename-dialog",
    fields: [
      el("label", { class: "label", for: "browser-name-input" }, "Name"),
      input,
      el("p", { class: "hint" }, "Shown next to what you upload from here, and on the Devices page."),
    ],
    submit: async () => {
      const name = input.value.trim();
      if (!name || [...name].length > NAME_MAX) return `Use 1–${NAME_MAX} characters.`;
      try {
        await api("PATCH", "/api/sessions/self", { json: { name } });
      } catch (err) {
        if (err instanceof ApiError && err.status === 400) return `Use 1–${NAME_MAX} printable characters.`;
        if (err instanceof ApiError && err.status === 401) return false;
        return `Couldn't rename: ${err.message}`;
      }
      saved = name;
      return true;
    },
  });
  if (ok && saved !== null) toast(`This browser is now “${saved}”`);
  return ok ? saved : null;
}

// The "This browser <name> ✎" controls (the sidebar, and the Settings card on a phone). Every
// button shows the same name; renaming from any of them updates them all.
export async function wireBrowserNameButton(...buttons) {
  const btns = buttons.flat().filter(Boolean);
  if (!btns.length) return;
  let current = "";
  const show = (name) => {
    current = name;
    for (const btn of btns) {
      btn.querySelector(".browser-name-value").textContent = name || UNNAMED;
      btn.title = `This browser: ${name || UNNAMED} — rename`;
    }
  };
  for (const btn of btns) {
    btn.addEventListener("click", async () => {
      const name = await renameBrowser(current);
      if (name !== null) show(name);
    });
  }
  const name = await loadSessionName();
  if (name === null) return;
  show(name);
  for (const btn of btns) btn.hidden = false;
}
