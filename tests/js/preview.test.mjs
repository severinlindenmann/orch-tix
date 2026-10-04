import { test } from "node:test";
import assert from "node:assert/strict";
import { previewKind } from "../../fileshare/static/js/previewkind.js";
import { cipherSize, plainSize } from "../../fileshare/static/js/format.js";
import { CHUNK, HEADER_LEN, IntegrityError } from "../../fileshare/static/js/crypto.js";
import { ApiError } from "../../fileshare/static/js/api.js";
import { DeletedError, previewError } from "../../fileshare/static/js/preview.js";

test("previewKind picks the renderer by extension, then mime", () => {
  const k = (name, mime = "") => previewKind({ name, mime });
  assert.equal(k("README.md").kind, "markdown");
  assert.equal(k("notes.markdown").kind, "markdown");
  assert.equal(k("x", "text/markdown").kind, "markdown");
  assert.equal(k("cfg.json").kind, "json");
  assert.equal(k("x", "application/json; charset=utf-8").kind, "json");
  assert.deepEqual(k("Photo.PNG"), { kind: "image", type: "image/png" });
  assert.deepEqual(k("a.jpeg"), { kind: "image", type: "image/jpeg" });
  assert.deepEqual(k("x", "image/webp"), { kind: "image", type: "image/webp" });
  assert.equal(k("build.log").kind, "text");
  assert.equal(k("script.py").kind, "text");
  assert.equal(k("x", "text/plain").kind, "text");
});

test("previewKind never previews active formats", () => {
  const k = (name, mime = "") => previewKind({ name, mime }).kind;
  assert.equal(k("logo.svg"), "none");
  assert.equal(k("logo.svg", "image/png"), "none");
  assert.equal(k("x", "image/svg+xml"), "none");
  assert.equal(k("page.html"), "none");
  assert.equal(k("page.htm", "text/html"), "none");
  assert.equal(k("evil.md", "text/html"), "none");
  assert.equal(k("archive.tar.gz"), "none");
  assert.equal(k("noext"), "none");
});

test("previewError gives every failure a clear line", () => {
  const msg = (err) => previewError(err, "FILE7");
  const decrypt = "Couldn't decrypt FILE7 — the file may be corrupt or was encrypted with another key.";
  assert.deepEqual(msg(new IntegrityError("chunk 0 failed authentication")), { text: decrypt, error: true });
  assert.deepEqual(msg(new DeletedError("FILE7")), { text: "FILE7 was deleted.", error: false });
  assert.deepEqual(msg(new ApiError(410, "gone", "deleted")), { text: "FILE7 was deleted.", error: false });
  assert.equal(msg(new ApiError(401, "unauthenticated")).text, "Session expired — log in again.");
  assert.equal(msg(new ApiError(0, "network", "offline")).text, "Couldn't reach the server — try again.");
  assert.equal(msg(new ApiError(500, "http_500", "boom")).text, "Couldn't load FILE7: boom");
  assert.deepEqual(msg(new TypeError("x")), { text: "Couldn't preview FILE7.", error: true });
  // the Download button's toast names the action that failed
  assert.equal(previewError(new TypeError("x"), "FILE7", "download").text, "Couldn't download FILE7.");
  assert.equal(previewError(new ApiError(0, "network"), "FILE7", "download").text, "Couldn't reach the server — try again.");
});

test("format.plainSize inverts format.cipherSize at chunk boundaries", () => {
  for (const n of [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 2 * CHUNK + 5]) {
    assert.equal(plainSize(cipherSize(n)), n, `n=${n}`);
  }
  assert.equal(cipherSize(0), HEADER_LEN + 16);
  assert.equal(cipherSize(CHUNK), HEADER_LEN + CHUNK + 16);
  assert.equal(plainSize(HEADER_LEN + 15), 0);
});
