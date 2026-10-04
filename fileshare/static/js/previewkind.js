// Picks the preview renderer for decrypted metadata. Active formats (HTML, SVG) are never previewed.
const IMAGE_BY_EXT = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", webp: "image/webp", gif: "image/gif" };
const IMAGE_MIMES = new Set(Object.values(IMAGE_BY_EXT));
// These containers can also hold video: an extension counts as audio unless the mime says video/*,
// and .webm (most often video) only when the mime is audio/* or missing.
const AUDIO_BY_EXT = {
  m4a: "audio/mp4", mp3: "audio/mpeg", ogg: "audio/ogg", oga: "audio/ogg", opus: "audio/ogg",
  wav: "audio/wav", webm: "audio/webm", aac: "audio/aac",
};
const ACTIVE_EXT = new Set(["html", "htm", "xhtml", "svg", "svgz"]);
const ACTIVE_MIME = new Set(["text/html", "application/xhtml+xml", "image/svg+xml"]);
const TEXT_EXT = new Set([
  "txt", "log", "py", "sh", "bash", "zsh", "fish", "yaml", "yml", "toml", "ini", "cfg", "conf",
  "csv", "tsv", "ts", "tsx", "js", "mjs", "cjs", "jsx", "css", "scss", "go", "rs", "java", "kt",
  "c", "h", "cpp", "hpp", "cs", "rb", "php", "sql", "xml", "diff", "patch", "tf", "hcl", "lua",
  "swift", "r", "gradle", "properties", "env",
]);

function extOf(name) {
  const m = /\.([A-Za-z0-9]+)$/.exec(name || "");
  return m ? m[1].toLowerCase() : "";
}

export function previewKind(meta) {
  const ext = extOf(meta.name);
  const mime = (meta.mime || "").toLowerCase().split(";")[0].trim();
  if (ACTIVE_EXT.has(ext) || ACTIVE_MIME.has(mime)) return { kind: "none" };
  if (ext === "md" || ext === "markdown" || mime === "text/markdown") return { kind: "markdown" };
  if (ext === "json" || mime === "application/json") return { kind: "json" };
  if (IMAGE_BY_EXT[ext]) return { kind: "image", type: IMAGE_BY_EXT[ext] };
  if (IMAGE_MIMES.has(mime)) return { kind: "image", type: mime };
  if (mime.startsWith("audio/")) return { kind: "audio", type: mime };
  if (AUDIO_BY_EXT[ext] && !mime.startsWith("video/") && (ext !== "webm" || !mime)) return { kind: "audio", type: AUDIO_BY_EXT[ext] };
  if (TEXT_EXT.has(ext) || mime.startsWith("text/")) return { kind: "text" };
  return { kind: "none" };
}
