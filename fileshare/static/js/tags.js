// fileshare/static/js/tags.js — the tag rules of spec §19, mirroring fileshare/tags.py exactly:
// trim, lowercase, spaces and "_" become "-", then ^[a-z0-9][a-z0-9-]{0,39}$ (ASCII); at most 10
// per file, deduplicated and sorted. Plus the tag filter's URL state (?tag=a&tag=b). Pure: no DOM.

export const MAX_TAGS = 10;
const TAG_RE = /^[a-z0-9][a-z0-9-]{0,39}$/;
export const TAG_RULE = "Use a–z, 0–9 and -, starting with a letter or digit, at most 40 characters.";

// Python's str.isspace() set, which str.strip() removes. Unlike JS's trim() it includes
// \x1c-\x1f and \x85, and it does not include U+FEFF (the BOM).
const WS = "\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const LEAD_WS = new RegExp(`^[${WS}]+`);
const TRAIL_WS = new RegExp(`[${WS}]+$`);
export const pyStrip = (s) => s.replace(LEAD_WS, "").replace(TRAIL_WS, "");

export class BadTag extends Error {
  constructor(message) {
    super(message);
    this.name = "BadTag";
  }
}

export function normalizeTag(raw) {
  if (typeof raw !== "string") throw new BadTag("a tag must be a string");
  const tag = pyStrip(raw).toLowerCase().replace(/[ _]/g, "-");
  if (!TAG_RE.test(tag)) throw new BadTag(TAG_RULE);
  return tag;
}

export function tryTag(raw) {
  try {
    return normalizeTag(raw);
  } catch {
    return null;
  }
}

// Sorts like Python's sorted() does for these ASCII strings (by code unit).
const byCode = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

export function normalizeTags(raw) {
  if (!Array.isArray(raw)) throw new BadTag("tags must be a list of strings");
  const tags = [...new Set(raw.map(normalizeTag))].sort(byCode);
  if (tags.length > MAX_TAGS) throw new BadTag(`At most ${MAX_TAGS} tags per file.`);
  return tags;
}

// What the tags input shows while typing: leading space dropped, lowercase, spaces and "_" as "-".
// Trailing spaces stay until the tag is committed (normalizeTag trims them), so "notes " + comma
// is "notes", as on the server, not "notes-".
export function liveTag(value) {
  const s = String(value).replace(LEAD_WS, "");
  const core = s.replace(TRAIL_WS, "");
  return core.toLowerCase().replace(/[ _]/g, "-") + s.slice(core.length);
}

// The selected filter tags in a location.search: valid ones only, normalised, distinct, sorted,
// at most MAX_TAGS. Anything else in the address is ignored.
export function tagsFromSearch(search) {
  const out = new Set();
  for (const raw of new URLSearchParams(search).getAll("tag")) {
    const t = tryTag(raw);
    if (t) out.add(t);
  }
  return [...out].sort(byCode).slice(0, MAX_TAGS);
}

// `url` (a same-origin path such as "/" or "/?f=FILE7") with its tag parameters replaced by `tags`.
export function withTags(url, tags) {
  const u = new URL(url, "https://x.invalid");
  u.searchParams.delete("tag");
  for (const t of [...tags].sort(byCode)) u.searchParams.append("tag", t);
  return u.pathname + u.search + u.hash;
}
