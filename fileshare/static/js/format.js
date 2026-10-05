import { CHUNK, HEADER_LEN } from "./crypto.js";

const TAG = 16;
const MD = ["md", "markdown"];
const JSONX = ["json"];
const IMG = ["png", "jpg", "jpeg", "webp", "gif"];
const TEXT = ["txt", "log", "py", "sh", "yaml", "yml", "toml", "csv", "js", "ts", "ini", "cfg", "sql"];

export function plainSize(ct) {
  if (!Number.isFinite(ct) || ct < HEADER_LEN + TAG) return 0;
  const body = ct - HEADER_LEN;
  return body - TAG * Math.ceil(body / (CHUNK + TAG));
}

export function cipherSize(ptLen, chunkSize = CHUNK) {
  return HEADER_LEN + ptLen + TAG * Math.max(1, Math.ceil(ptLen / chunkSize));
}

export function humanSize(n) {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v < 10 || (i > 0 && v < 100) ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

export function relTime(iso, now = Date.now()) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const s = Math.round((now - t) / 1000);
  if (s < 45) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return h === 1 ? "1 hour ago" : `${h} hours ago`;
  const d = Math.round(h / 24);
  if (d === 1) return "yesterday";
  if (d < 7) return `${d} days ago`;
  return new Date(t).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

export function extOf(name) {
  const m = /\.([A-Za-z0-9]{1,8})$/.exec(String(name ?? "").split(/[\\/]/).pop());
  return m ? m[1].toLowerCase() : "";
}

export function typeOf(mime = "", name = "") {
  const ext = extOf(name);
  const mt = String(mime ?? "").toLowerCase();
  const tag = (fallback) => (ext || fallback).toUpperCase().slice(0, 4);
  if (mt === "text/markdown" || MD.includes(ext)) return { kind: "md", label: "MD" };
  if (mt === "application/json" || JSONX.includes(ext)) return { kind: "json", label: "JSON" };
  if (/^image\/(png|jpeg|webp|gif)$/.test(mt) || IMG.includes(ext)) return { kind: "img", label: tag(mt.split("/")[1]) };
  if (mt.startsWith("text/") || TEXT.includes(ext)) return { kind: "text", label: tag("txt") };
  return { kind: "other", label: ext ? tag("") : "FILE" };
}

export function matches(file, q) {
  const needle = String(q ?? "").trim().toLowerCase();
  if (!needle) return true;
  if (/^\d+$/.test(needle)) return String(file.n) === needle; // a bare number is a file number: FILE40, not every name with a 40 in it (QA TF-23)
  const hay = [file.id, file.ok ? file.meta.name : "", file.ok ? file.meta.note : ""].join("\n").toLowerCase();
  return hay.includes(needle);
}

export async function mapLimit(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  async function worker() {
    while (next < items.length) {
      const i = next;
      next += 1;
      out[i] = await fn(items[i], i);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

const MAX_NAME = 200; // code points
// C0 controls and DEL, plus bidi marks, embeddings/overrides and isolates (U+200E/F, U+202A–E, U+2066–9),
// which can make "invoice‮fdp.exe" display as "invoiceexe.pdf".
const UNSAFE_CHARS = /[\u0000-\u001f\u007f‎‏‪-‮⁦-⁩]/g;

export function safeDownloadName(name, id) {
  const base = String(name ?? "").split(/[\\/]/).pop().replace(UNSAFE_CHARS, "").trim();
  if (!base || base === "." || base === "..") return `${id}.bin`;
  const chars = [...base]; // by code point, so an astral character is never split
  if (chars.length <= MAX_NAME) return base;
  const ext = /\.[^.]{1,16}$/.exec(base);
  const extChars = ext && ext.index > 0 ? [...ext[0]] : [];
  return chars.slice(0, MAX_NAME - extChars.length).join("") + extChars.join("");
}

// ---- v2 (spec §14)

// FileOut.device is never null now; a browser upload has id null and the session's name.
// Browser uploads are keyed by name ("web:" can't collide with a "dev_…" id), so a filter works.
export function deviceLabel(f) {
  const name = f?.device?.name;
  return typeof name === "string" && name ? name : "browser";
}

export function deviceKey(f) {
  const id = f?.device?.id;
  return typeof id === "string" && id ? id : `web:${deviceLabel(f)}`;
}

const MIN_MS = 60_000;
const HOUR_MS = 60 * MIN_MS;
const DAY_MS = 24 * HOUR_MS;

// "expires in 6 d" / "5 h" / "12 min", "expired", or "never" (expires_at null). Each unit is
// rounded, so a fresh 1d upload (24 h minus a moment) reads "expires in 1 d".
export function expiryLabel(expiresAt, now = Date.now()) {
  if (expiresAt === null || expiresAt === undefined) return "never";
  const t = Date.parse(expiresAt);
  if (!Number.isFinite(t)) return "";
  const ms = t - now;
  if (ms <= 0) return "expired";
  const m = Math.round(ms / MIN_MS);
  if (m < 60) return `expires in ${Math.max(1, m)} min`;
  const h = Math.round(ms / HOUR_MS);
  if (h < 24) return `expires in ${h} h`;
  return `expires in ${Math.round(ms / DAY_MS)} d`;
}

export function isExpiringSoon(expiresAt, now = Date.now()) {
  if (expiresAt === null || expiresAt === undefined) return false;
  const t = Date.parse(expiresAt);
  return Number.isFinite(t) && t - now < DAY_MS;
}

// ---- redesign (spec §18)

function msLeft(expiresAt, now) {
  const t = Date.parse(expiresAt);
  return Number.isFinite(t) ? t - now : null;
}

// The compact label at the end of a row: "6d", "18h", "12m", "expired", "never" ("" if unparseable).
export function expiryShort(expiresAt, now = Date.now()) {
  if (expiresAt === null || expiresAt === undefined) return "never";
  const ms = msLeft(expiresAt, now);
  if (ms === null) return "";
  if (ms <= 0) return "expired";
  const m = Math.round(ms / MIN_MS);
  if (m < 60) return `${Math.max(1, m)}m`;
  const h = Math.round(ms / HOUR_MS);
  if (h < 24) return `${h}h`;
  return `${Math.round(ms / DAY_MS)}d`;
}

// The detail view's expiry pill: "Expires in 6 days" / "18 h" / "12 min", "Expired", "Never expires".
export function expiryPhrase(expiresAt, now = Date.now()) {
  if (expiresAt === null || expiresAt === undefined) return "Never expires";
  const ms = msLeft(expiresAt, now);
  if (ms === null) return "";
  if (ms <= 0) return "Expired";
  const m = Math.round(ms / MIN_MS);
  if (m < 60) return `Expires in ${Math.max(1, m)} min`;
  const h = Math.round(ms / HOUR_MS);
  if (h < 24) return `Expires in ${h} h`;
  const d = Math.round(ms / DAY_MS);
  return `Expires in ${d} ${d === 1 ? "day" : "days"}`;
}

// A row's age: "now", "2 min", "3 h", then the weekday within a week, then "12 Sep" ("12 Sep 2025").
export function shortAge(iso, now = Date.now()) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const s = Math.round((now - t) / 1000);
  if (s < 45) return "now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} h`;
  const d = new Date(t);
  if (now - t < 7 * DAY_MS) return d.toLocaleDateString("en-GB", { weekday: "short" });
  const opts = { day: "numeric", month: "short" };
  if (d.getFullYear() !== new Date(now).getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString("en-GB", opts);
}

export function isToday(iso, now = Date.now()) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return false;
  return new Date(t).toDateString() === new Date(now).toDateString();
}

export function fileGroup(f, now = Date.now()) {
  return isToday(f.created_at, now) ? "Today" : "Earlier";
}

// "New" (the accent dot, the sidebar count): not marked as done, and shared in the last 24 hours.
export function isNew(f, now = Date.now()) {
  if (f.acked_at) return false;
  const t = Date.parse(f.created_at);
  return Number.isFinite(t) && now - t < DAY_MS;
}
