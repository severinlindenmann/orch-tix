// A pinned image is refused before any byte of it is downloaded when the FILE is not the named one or is not a raster
// type (an SVG under a raster name). Fast-suite twin of the browser test in tests/browser/test_phone_v4.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import { verifiedImage } from "../../fileshare/static/js/images.js";

function stubFetch(row) {
  const urls = [];
  globalThis.window = { dispatchEvent() {} };
  globalThis.fetch = async (url) => {
    urls.push(String(url));
    return { ok: true, status: 200, json: async () => row };
  };
  return urls;
}

const row = (name, mime) => ({ id: "f1", ok: true, meta: { name, mime }, dek: new Uint8Array(32), uuid: "00".repeat(16), size: 100 });

for (const [label, pinned, file] of [
  ["another name", "chart.png", row("other.png", "image/png")],
  ["an svg under a raster name", "logo.png", row("logo.png", "image/svg+xml")],
]) {
  test(`a pinned image is refused for ${label}, without downloading it`, async () => {
    const urls = stubFetch(file);
    const url = await verifiedImage({ name: pinned, sha256: "a".repeat(64), file: `f-${label}` });
    assert.equal(url, null);
    assert.ok(urls.length > 0 && urls.every((u) => !u.endsWith("/blob")), "the blob was never requested");
  });
}
