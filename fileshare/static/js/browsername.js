// Derives this browser's default session label, "<device> · <browser>" (spec §14 B), e.g.
// "iPhone · Safari". Pure: node tests pass in a fake navigator. The label is only a hint the user
// can rename; it is never used for anything security-relevant.
const SEP = " · ";

function deviceFromPlatform(platform) {
  switch (platform) {
    case "macOS": return "Mac";
    case "Windows": return "Windows PC";
    case "Linux": return "Linux PC";
    case "Android": return "Android phone";
    case "iOS": return "iPhone";
    default: return null;
  }
}

function browserFromBrands(brands) {
  const names = brands.map((b) => (b && typeof b.brand === "string" ? b.brand : ""));
  if (names.includes("Microsoft Edge")) return "Edge";
  if (names.includes("Opera") || names.includes("Opera GX") || names.includes("Brave")) return "Browser";
  if (names.some((n) => n === "Google Chrome" || n === "Chromium" || n === "HeadlessChrome")) return "Chrome";
  return null;
}

function deviceFromUa(ua, touchPoints) {
  if (/iPhone|iPod/.test(ua)) return "iPhone";
  if (/iPad/.test(ua)) return "iPad";
  // iPadOS 13+ sends a desktop Mac UA; only a touch screen gives it away.
  if (/Macintosh/.test(ua) && touchPoints > 1) return "iPad";
  if (/Android/.test(ua)) return "Android phone";
  if (/Macintosh|Mac OS X/.test(ua)) return "Mac";
  if (/Windows/.test(ua)) return "Windows PC";
  if (/CrOS/.test(ua)) return null;
  if (/Linux|X11/.test(ua)) return "Linux PC";
  return null;
}

function browserFromUa(ua) {
  if (/\bEdg(?:e|A|iOS)?\//.test(ua)) return "Edge";
  if (/\b(?:OPR|Opera|SamsungBrowser|YaBrowser|Vivaldi|UCBrowser)\//.test(ua)) return "Browser";
  if (/\b(?:Firefox|FxiOS)\//.test(ua)) return "Firefox";
  if (/\b(?:HeadlessChrome|Chrome|Chromium|CriOS)\//.test(ua)) return "Chrome";
  if (/\bSafari\//.test(ua) && /\bVersion\//.test(ua)) return "Safari";
  return null;
}

export function deriveBrowserName(nav) {
  const ua = typeof nav?.userAgent === "string" ? nav.userAgent : "";
  const touch = Number.isFinite(nav?.maxTouchPoints) ? nav.maxTouchPoints : 0;
  const uad = nav?.userAgentData;
  let device = null;
  let browser = null;
  if (uad && typeof uad === "object") {
    if (typeof uad.platform === "string") device = deviceFromPlatform(uad.platform);
    if (Array.isArray(uad.brands)) browser = browserFromBrands(uad.brands);
  }
  device = device ?? deviceFromUa(ua, touch) ?? "Browser";
  browser = browser ?? browserFromUa(ua) ?? "Browser";
  return `${device}${SEP}${browser}`;
}
