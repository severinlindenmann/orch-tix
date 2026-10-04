// fileshare/static/js/widget-model.js — the pure side of ticket widgets on the phone (orch.widgets.v1, orch-core
// docs/widgets.md; the doc's `widgets` list comes from the orch-tix addon, ticket_widgets.py). No DOM: node tests it.
//
// A widget is a ```orch fence in a section's text. The addon sends, per block in ticket order, {section, index,
// key, layer, name, title, source?, text, doc? | file?}. The k-th orch fence of a section is the k-th widget of
// that section. The fence rule is orch-core's (orch/core/fences.py, CommonMark): up to 3 spaces, a run of 3+
// backticks or tildes; it closes on a run of the same character at least as long with nothing after it.

export const WIDGETS_FORMAT = "orch.widgets.v1";
export const READY_MS = 3000;              // the kit must say ready within 3 s, else the text is shown
export const MIN_HEIGHT = 60;
export const MAX_HEIGHT = 2400;
export const START_HEIGHT = 160;
export const MAX_INLINE_BYTES = 128 * 1024;        // an inline document (the addon's INLINE_MAX); more is not drawn
export const MAX_FILE_BYTES = 8 * 1024 * 1024;     // a shared document, below orch-core's 9 MiB frame-page ceiling
export const BLOB_SLACK = 1024 * 1024;             // the sealed blob's chunk tags on top of the plaintext cap

const OPEN = /^ {0,3}(`{3,}|~{3,})(.*)$/;

function opening(line) {
  const m = OPEN.exec(line);
  if (!m || (m[1][0] === "`" && m[2].includes("`"))) return null;
  return { run: m[1], info: m[2].trim() };
}

function closes(line, run) {
  const m = OPEN.exec(line);
  return Boolean(m && m[1][0] === run[0] && m[1].length >= run.length && !m[2].trim());
}

// [{kind: "text", text}, {kind: "widget", n, raw}] in order; n counts the section's orch blocks from 0. An
// unclosed orch fence is still a block (orch-core counts it, as an error) and runs to the end.
export function splitText(text) {
  const parts = [];
  let buf = [], fence = null, body = null, n = 0;
  const flush = () => { if (buf.length) parts.push({ kind: "text", text: buf.join("\n") }); buf = []; };
  for (const line of String(text ?? "").split("\n")) {
    if (fence === null) {
      const o = opening(line);
      if (o && o.info === "orch") { flush(); fence = o.run; body = []; continue; }
      if (o) fence = o.run;
      buf.push(line);
    } else if (body !== null) {
      if (closes(line, fence)) { parts.push({ kind: "widget", n: n++, raw: body.join("\n") }); fence = null; body = null; }
      else body.push(line);
    } else {
      if (closes(line, fence)) fence = null;
      buf.push(line);
    }
  }
  if (body !== null) parts.push({ kind: "widget", n: n++, raw: body.join("\n") });
  flush();
  return parts;
}

// The widgets the addon sent (an older addon or a title/key-only doc sends none: the text stays as it is).
export function docWidgets(doc) {
  return doc?.widgets_format === WIDGETS_FORMAT && Array.isArray(doc.widgets) ? doc.widgets.filter((w) => w && typeof w === "object") : [];
}

export async function sha256Hex(data) {
  const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), (b) => b.toString(16).padStart(2, "0")).join("");
}

// A section's parts with each fence's widget entry attached (`widget`, or null: the fence then shows as the text
// it is). A fence belongs to the widget of its section whose raw_sha256 is the digest of the fence's own text;
// position (the entry's place among this section's entries; `index` is orch-core's ticket-wide one) only breaks the tie between identical fences. A fence nothing vouches for stays text. Each
// fence is hashed once per call; each entry serves one fence.
export async function sectionParts(doc, name, text) {
  const mine = docWidgets(doc).filter((w) => w.section === name);
  const parts = splitText(text);
  const used = new Set();
  for (const p of parts) {
    if (p.kind !== "widget") continue;
    const digest = await sha256Hex(p.raw);
    const same = mine.filter((w) => w.raw_sha256 === digest && !used.has(w));
    const w = same.find((x) => mine.indexOf(x) === p.n) ?? same[0] ?? null;
    if (w) used.add(w);
    p.widget = w;
  }
  return parts;
}

// The section text without its widget blocks (a one-line summary must not read "```orch").
export function textOnly(text) {
  return splitText(text).filter((p) => p.kind === "text").map((p) => p.text).join("\n");
}

export function chipText(w) {
  if (w?.layer === "type") return "core";
  if (w?.layer === "widget") return `agent HTML · ${w.name || "template"}`;
  if (w?.layer === "html") return "agent HTML · one-off";
  return "not shown";
}

const FILE_ID = /^FILE[1-9][0-9]{0,11}$/;

const LAYERS = new Set(["type", "widget", "html"]);
const PIN = /^[0-9a-f]{64}$/;

// What Show loads: the inline document (up to 128 KiB), else the shared FILE, else nothing (text only), and only
// for a known layer with a document pin: {inline | file, sha256}. The pin is checked on the bytes (verifyDoc).
export function docSource(w) {
  if (!LAYERS.has(w?.layer) || typeof w.sha256 !== "string" || !PIN.test(w.sha256)) return null;
  if (typeof w.doc === "string" && w.doc) {
    return new TextEncoder().encode(w.doc).length <= MAX_INLINE_BYTES ? { inline: w.doc, sha256: w.sha256 } : null;
  }
  if (typeof w.file === "string" && FILE_ID.test(w.file)) return { file: w.file, sha256: w.sha256 };
  return null;
}

// True when the document's exact bytes hash to the pin the desktop sent.
export async function verifyDoc(bytes, sha256) {
  return typeof sha256 === "string" && PIN.test(sha256) && (await sha256Hex(bytes)) === sha256;
}

// The document with its frame nonce replaced by this page's own fresh one, so a nonce the document carries
// (known to whoever wrote it) is never the one messages are trusted by. Only the orch-frame meta changes.
export function withNonce(html, nonce) {
  return String(html ?? "").replace(/(<meta\s+name="orch-frame"\s+content=")[A-Za-z0-9_-]*(")/g, `$1${nonce}$2`);
}

// A response body as bytes, refused before it is all read: by Content-Length first, then while streaming (the
// header can lie). Throws past `max`.
export async function readCapped(res, max) {
  const big = () => new Error("the widget document is too large");
  const len = Number(res.headers?.get?.("content-length"));
  if (Number.isFinite(len) && len > max) throw big();
  const reader = res.body?.getReader?.();
  if (!reader) {
    const buf = new Uint8Array(await res.arrayBuffer());
    if (buf.length > max) throw big();
    return buf;
  }
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > max) { await reader.cancel().catch(() => {}); throw big(); }
    chunks.push(value);
  }
  const out = new Uint8Array(total);
  let at = 0;
  for (const c of chunks) { out.set(c, at); at += c.length; }
  return out;
}

// The document with its first <html> tag's data-theme set, so it opens in the page's theme.
export function withTheme(html, dark) {
  const theme = dark ? "dark" : "light";
  return String(html ?? "").replace(/<html\b[^>]*>/i, (tag) => (/\sdata-theme="[^"]*"/.test(tag)
    ? tag.replace(/\sdata-theme="[^"]*"/, ` data-theme="${theme}"`)
    : tag.replace(/^<html/i, `<html data-theme="${theme}"`)));
}

const KINDS = new Set(["ready", "resize", "text", "error", "open"]);

// A frame's message, checked: {orch: 1, frame: <nonce>, kind} with a known kind; null otherwise. The caller has
// already checked that event.source is that frame's window.
export function frameMessage(data, nonce) {
  if (!data || typeof data !== "object" || data.orch !== 1 || data.frame !== nonce || !KINDS.has(data.kind)) return null;
  return data;
}

export function clampHeight(h) {
  const n = Number(h);
  return Number.isFinite(n) ? Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, Math.ceil(n))) : START_HEIGHT;
}
