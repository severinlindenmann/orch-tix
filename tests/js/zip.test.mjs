// The public drop page's stored ZIP writer (upload-links spec, Task 5).
import { test } from "node:test";
import assert from "node:assert/strict";
import { crc32, buildZip, zipPlainSize } from "../../fileshare/static/js/zip.js";

const te = new TextEncoder();

test("crc32 matches the standard test vector", () => {
  assert.equal(crc32(te.encode("123456789")), 0xcbf43926);
  assert.equal(crc32(new Uint8Array(0)), 0);
});

test("buildZip writes a local header, the data and an EOCD with the right count", () => {
  const data = te.encode("hello");
  const zip = buildZip([{ path: "a.txt", data }]);
  assert.equal(String.fromCharCode(...zip.subarray(0, 4)), "PK\x03\x04");

  // The general-purpose flag (bytes 6-7 of the local header) has bit 11 (UTF-8 names) set.
  const flag = zip[6] | (zip[7] << 8);
  assert.equal(flag & 0x0800, 0x0800);

  // EOCD signature is the last 22 bytes (no comment, one entry, one disk).
  const eocd = zip.subarray(zip.length - 22);
  assert.equal(String.fromCharCode(...eocd.subarray(0, 4)), "PK\x05\x06");
  const view = new DataView(eocd.buffer, eocd.byteOffset, eocd.length);
  assert.equal(view.getUint16(8, true), 1);   // records on this disk
  assert.equal(view.getUint16(10, true), 1);  // total records
});

test("buildZip rejects unsafe paths", () => {
  const data = te.encode("x");
  for (const path of ["../x", "a/../b", "a//b", "/x", "a\\b"]) {
    assert.throws(() => buildZip([{ path, data }]), `should reject ${path}`);
  }
});

test("buildZip only rejects a traversal segment, not '..' inside an ordinary name", () => {
  const data = te.encode("x");
  for (const path of ["a/notes..final.txt", "Report... final.pdf", "v1..2/x"]) {
    assert.doesNotThrow(() => buildZip([{ path, data }]), `should accept ${path}`);
  }
});

test("zipPlainSize matches buildZip's actual output size", () => {
  const entries = [
    { path: "dir/sub/ä.txt", data: te.encode("hello") },
    { path: "b.bin", data: Uint8Array.from({ length: 256 }, (_, i) => i) },
  ];
  const zip = buildZip(entries);
  const size = zipPlainSize(entries.map((e) => ({ path: e.path, size: e.data.length })));
  assert.equal(size, zip.length);
});

test("buildZip round-trips through a naive stored-zip reader", () => {
  const entries = [
    { path: "dir/sub/ä.txt", data: te.encode("hello") },
    { path: "b.bin", data: Uint8Array.from({ length: 256 }, (_, i) => i) },
  ];
  const zip = buildZip(entries, { now: new Date(2026, 0, 2, 3, 4, 5) });
  const view = new DataView(zip.buffer, zip.byteOffset, zip.length);
  let off = 0;
  const read = [];
  while (off < zip.length) {
    const sig = view.getUint32(off, true);
    if (sig === 0x04034b50) {
      const nameLen = view.getUint16(off + 26, true);
      const extraLen = view.getUint16(off + 28, true);
      const size = view.getUint32(off + 18, true);
      const name = new TextDecoder().decode(zip.subarray(off + 30, off + 30 + nameLen));
      const start = off + 30 + nameLen + extraLen;
      read.push({ path: name, data: zip.subarray(start, start + size) });
      off = start + size;
    } else {
      break;
    }
  }
  assert.equal(read.length, 2);
  assert.equal(read[0].path, "dir/sub/ä.txt");
  assert.deepEqual([...read[0].data], [...te.encode("hello")]);
  assert.equal(read[1].path, "b.bin");
  assert.deepEqual([...read[1].data], [...Uint8Array.from({ length: 256 }, (_, i) => i)]);
});

test("buildZip throws over the entry-count limit", () => {
  const entries = Array.from({ length: 65536 }, (_, i) => ({ path: `f${i}`, data: new Uint8Array(0) }));
  assert.throws(() => buildZip(entries));
});
