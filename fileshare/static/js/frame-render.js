// fileshare/static/js/frame-render.js — the render rules of the dashboard frame (docs/bridge-frame.md).
// Pure (no DOM). Only a response the HOST tagged as a dashboard page (head.page === true, set by the transport from
// the host's own answer; never read from the body or from a header the page controls) is ever written into the
// dashboard frame, and only as a 200 text/html answer whose final path is not one of the routes that carry their own
// policy. Everything else goes to the existing sandboxed viewer or to a download. The dashboard sends its own
// Content-Security-Policy on every page, so "no policy of its own" is not the test.
import { compileScopes, pathnameOf } from "./frame-scope.js";

// Routes that are never a dashboard page, whatever the host tagged: a ticket's artifact files, the widget frames and
// their files, an addon's downloads, and a ticket's raw file (orch-core docs/dashboard-frame.md, "Routes that carry
// their own Content-Security-Policy"; /t/{ref}/raw is added here on purpose: raw ticket text is never drawn as a page).
export const NEVER_PAGE = [
  "/a/*", "/w/*", "/wp/*", "/wpf/*", "/addons/:name/files/*", "/t/:ref/raw",
].map((pattern) => compileScopes({ rules: [{ methods: ["GET", "POST"], pattern }] }));

export const isNeverPage = (path) => NEVER_PAGE.some((s) => s.allows("GET", pathnameOf(path)));

const type = (headers) => String((headers || {})["content-type"] || "").split(";")[0].trim().toLowerCase();

// -> "page" | "viewer" | "download" | "error". `allowPage: false` is a link that opens elsewhere (target=_blank).
export function classify({ path, status, headers, page }, { allowPage = true } = {}) {
  const ct = type(headers);
  if (allowPage && page === true && status === 200 && ct === "text/html" && !isNeverPage(path)) return "page";
  if (!Number.isInteger(status) || status < 200 || status >= 300) return "error";
  const disposition = String((headers || {})["content-disposition"] || "").toLowerCase();
  if (disposition.startsWith("attachment") || ct === "application/octet-stream" || ct === "") return "download";
  return "viewer";
}
