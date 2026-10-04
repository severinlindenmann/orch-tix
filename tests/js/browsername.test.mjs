import { test } from "node:test";
import assert from "node:assert/strict";
import { deriveBrowserName } from "../../fileshare/static/js/browsername.js";

const UA = {
  iphoneSafari: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
  iphoneChrome: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/126.0.6478.54 Mobile/15E148 Safari/604.1",
  iphoneFirefox: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/127.0 Mobile/15E148 Safari/605.1.15",
  iphoneEdge: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 EdgiOS/126.2592.56 Mobile/15E148 Safari/605.1.15",
  ipadOld: "Mozilla/5.0 (iPad; CPU OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
  // iPadOS 13+ asks for the desktop site by default: a Mac UA, told apart only by touch points.
  ipadDesktop: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
  androidChrome: "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
  androidFirefox: "Mozilla/5.0 (Android 14; Mobile; rv:127.0) Gecko/127.0 Firefox/127.0",
  androidEdge: "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36 EdgA/126.0.2592.61",
  androidSamsung: "Mozilla/5.0 (Linux; Android 14; SM-S911B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36",
  macSafari: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
  macChrome: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
  macFirefox: "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:127.0) Gecko/20100101 Firefox/127.0",
  winEdge: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.2592.61",
  winChrome: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
  winFirefox: "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
  winOpera: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 OPR/111.0.0.0",
  linuxChrome: "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
  linuxFirefox: "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
  headless: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/126.0.0.0 Safari/537.36",
  cros: "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
  curl: "curl/8.4.0",
};

const nav = (userAgent, extra = {}) => ({ userAgent, maxTouchPoints: 0, ...extra });

test("deriveBrowserName: UA string table", () => {
  const table = [
    [nav(UA.iphoneSafari, { maxTouchPoints: 5 }), "iPhone · Safari"],
    [nav(UA.iphoneChrome, { maxTouchPoints: 5 }), "iPhone · Chrome"],
    [nav(UA.iphoneFirefox, { maxTouchPoints: 5 }), "iPhone · Firefox"],
    [nav(UA.iphoneEdge, { maxTouchPoints: 5 }), "iPhone · Edge"],
    [nav(UA.ipadOld, { maxTouchPoints: 5 }), "iPad · Safari"],
    [nav(UA.ipadDesktop, { maxTouchPoints: 5 }), "iPad · Safari"],
    [nav(UA.androidChrome), "Android phone · Chrome"],
    [nav(UA.androidFirefox), "Android phone · Firefox"],
    [nav(UA.androidEdge), "Android phone · Edge"],
    [nav(UA.androidSamsung), "Android phone · Browser"],
    [nav(UA.macSafari), "Mac · Safari"],
    [nav(UA.macChrome), "Mac · Chrome"],
    [nav(UA.macFirefox), "Mac · Firefox"],
    [nav(UA.winEdge), "Windows PC · Edge"],
    [nav(UA.winChrome), "Windows PC · Chrome"],
    [nav(UA.winFirefox), "Windows PC · Firefox"],
    [nav(UA.winOpera), "Windows PC · Browser"],
    [nav(UA.linuxChrome), "Linux PC · Chrome"],
    [nav(UA.linuxFirefox), "Linux PC · Firefox"],
    [nav(UA.headless), "Mac · Chrome"],
    [nav(UA.cros), "Browser · Chrome"],
    [nav(UA.curl), "Browser · Browser"],
    [nav(""), "Browser · Browser"],
  ];
  for (const [n, want] of table) assert.equal(deriveBrowserName(n), want, n.userAgent);
});

test("deriveBrowserName prefers userAgentData when present", () => {
  const brands = (...names) => names.map((brand) => ({ brand, version: "126" }));
  const uad = (platform, list, mobile = false) => ({ platform, mobile, brands: brands(...list) });
  // The UA string says Linux; the client hints win.
  assert.equal(deriveBrowserName(nav(UA.linuxChrome, { userAgentData: uad("macOS", ["Not/A)Brand", "Google Chrome", "Chromium"]) })), "Mac · Chrome");
  assert.equal(deriveBrowserName(nav(UA.winEdge, { userAgentData: uad("Windows", ["Microsoft Edge", "Chromium", "Not.A/Brand"]) })), "Windows PC · Edge");
  assert.equal(deriveBrowserName(nav(UA.linuxChrome, { userAgentData: uad("Linux", ["Chromium", "Not_A Brand"]) })), "Linux PC · Chrome");
  assert.equal(deriveBrowserName(nav(UA.androidChrome, { userAgentData: uad("Android", ["Google Chrome", "Chromium"], true) })), "Android phone · Chrome");
  assert.equal(deriveBrowserName(nav(UA.winOpera, { userAgentData: uad("Windows", ["Opera", "Chromium"]) })), "Windows PC · Browser");
  assert.equal(deriveBrowserName(nav(UA.headless, { userAgentData: uad("macOS", ["HeadlessChrome", "Chromium"]) })), "Mac · Chrome");
  // Empty hints fall back to the UA string.
  assert.equal(deriveBrowserName(nav(UA.winChrome, { userAgentData: { platform: "", mobile: false, brands: [] } })), "Windows PC · Chrome");
});

test("deriveBrowserName always fits the server's 1-40 printable-character rule", () => {
  for (const ua of Object.values(UA)) {
    const name = deriveBrowserName(nav(ua));
    assert.ok(name.length >= 1 && name.length <= 40, name);
    assert.match(name, /^[^\u0000-\u001f\u007f]+$/);
  }
  assert.equal(deriveBrowserName(undefined), "Browser · Browser");
  assert.equal(deriveBrowserName({}), "Browser · Browser");
  assert.equal(deriveBrowserName({ userAgent: 42, userAgentData: { platform: 7, brands: "x" } }), "Browser · Browser");
});
