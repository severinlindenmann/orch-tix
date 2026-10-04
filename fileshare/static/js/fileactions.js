// "Mark as done" (the API's ack) and expiry actions of the file view (spec §14 C, E; wording §18).
// Not an entry module. After a change the file is re-read from the server and announced as
// `fs:file-updated` {detail: {file: FileOut}}, so every view updates in place with the server's
// own acked_by and expires_at.
import { api, ApiError } from "./api.js";
import { el, formDialog, toast } from "./ui.js";

export const TTL_OPTIONS = [["1d", "1 day"], ["7d", "7 days"], ["30d", "30 days"], ["never", "Never"]];
export const DEFAULT_TTL = "7d";

const path = (id, rest = "") => `/api/files/${encodeURIComponent(id)}${rest}`;
const isAuth = (err) => err instanceof ApiError && err.status === 401;

export function ttlSelect(id, value = DEFAULT_TTL) {
  const select = el("select", { id, class: "input ttl-select" },
    TTL_OPTIONS.map(([v, label]) => el("option", { value: v }, label)));
  select.value = value;
  return select;
}

function gone(id) {
  toast(`${id} was deleted`, "error");
  window.dispatchEvent(new CustomEvent("fs:files-changed"));
}

async function refresh(id) {
  try {
    const file = await api("GET", path(id));
    window.dispatchEvent(new CustomEvent("fs:file-updated", { detail: { file } }));
    return file;
  } catch (err) {
    if (isAuth(err)) return null;
    if (err instanceof ApiError && err.status === 410) gone(id);
    else window.dispatchEvent(new CustomEvent("fs:files-changed"));
    return null;
  }
}

// Returns the updated FileOut, or null after its own toast (or silently on a 401: the banner explains).
export async function setAck(id, on) {
  try {
    await api(on ? "POST" : "DELETE", path(id, "/ack"));
  } catch (err) {
    if (isAuth(err)) return null;
    if (err instanceof ApiError && err.status === 410) gone(id);
    else toast(`Couldn't mark ${id} as ${on ? "done" : "not done"}: ${err.message}`, "error");
    return null;
  }
  toast(on ? `Marked ${id} as done` : `Marked ${id} as not done`);
  return refresh(id);
}

// Opens the "Change expiry" dialog; the new expiry counts from now. Returns the FileOut or null.
export async function changeExpiry(id, name = "") {
  const select = ttlSelect("expiry-ttl");
  let updated = null;
  const saved = await formDialog({
    title: `Change expiry of ${id}`,
    confirmLabel: "Save",
    fields: [
      name ? el("p", { class: "modal-body" }, name) : null,
      el("label", { class: "label", for: "expiry-ttl" }, "Expires"),
      select,
      el("p", { class: "hint" }, "Counted from now. When it expires, the file is deleted and its ID stays retired."),
    ],
    submit: async () => {
      try {
        await api("PATCH", path(id), { json: { ttl: select.value } });
        return true;
      } catch (err) {
        if (isAuth(err)) return false;
        if (err instanceof ApiError && err.status === 410) {
          gone(id);
          return false;
        }
        if (err instanceof ApiError && err.status === 403) return "Only the device that shared this file can change its expiry.";
        return `Couldn't change the expiry: ${err.message}`;
      }
    },
  });
  if (saved) {
    toast(`Expiry of ${id} changed`);
    updated = await refresh(id);
  }
  return updated;
}
