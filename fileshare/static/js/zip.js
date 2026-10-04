// A stored (uncompressed) ZIP writer for the public drop page (upload-links spec §18, Task 5): no
// vendored compression library, just local headers, a central directory and an EOCD, general-purpose
// flag bit 11 (UTF-8 names) set on every entry. The CLI (Task 6) uses stdlib `zipfile` instead;
// tests/test_zip_js_parity.py is the arbiter that the two agree byte-for-byte.

const te = new TextEncoder();

let CRC_TABLE = null;
function crcTable() {
  if (CRC_TABLE) return CRC_TABLE;
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? (0xedb88320 ^ (c >>> 1)) : c >>> 1;
    t[n] = c >>> 0;
  }
  CRC_TABLE = t;
  return t;
}

export function crc32(bytes) {
  const t = crcTable();
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) c = t[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function dosDateTime(d) {
  const time = ((d.getHours() & 0x1f) << 11) | ((d.getMinutes() & 0x3f) << 5) | ((d.getSeconds() >> 1) & 0x1f);
  const date = (((d.getFullYear() - 1980) & 0x7f) << 9) | (((d.getMonth() + 1) & 0xf) << 5) | (d.getDate() & 0x1f);
  return { time, date };
}

// Only a path *segment* that is exactly "..", "." or "" is unsafe (traversal, or an empty/repeated
// slash); a name that merely contains ".." somewhere, like "notes..final.txt" or "v1..2/x", is a
// perfectly ordinary file name and must not be rejected.
function checkPath(path) {
  if (typeof path !== "string" || path === "") throw new TypeError("bad zip entry path");
  if (path.startsWith("/") || path.includes("\\")) {
    throw new TypeError(`unsafe zip entry path: ${path}`);
  }
  for (const seg of path.split("/")) {
    if (seg === "" || seg === "." || seg === "..") throw new TypeError(`unsafe zip entry path: ${path}`);
  }
}

// The size buildZip's output would have for these entries, without building it: a local header,
// name and data plus a central-directory record per entry, and the EOCD (Task 5's drop-page
// pre-check needs this before it reads any file into memory).
export function zipPlainSize(entries) {
  let total = 22; // EOCD
  for (const e of entries) {
    const nameLen = te.encode(e.path).length;
    total += 30 + nameLen + e.size + 46 + nameLen;
  }
  return total;
}

const LOCAL_SIG = 0x04034b50;
const CENTRAL_SIG = 0x02014b50;
const EOCD_SIG = 0x06054b50;
const FLAG_UTF8 = 0x0800;
const VERSION = 20;

// entries: [{ path, data }], path relative with forward slashes, data a Uint8Array. Method 0
// (stored): no compression, so compressed size === uncompressed size. Throws on >65535 entries, on
// total output over 0xFFFFFFFF (no zip64 support) or on an unsafe path.
export function buildZip(entries, { now = new Date() } = {}) {
  if (!Array.isArray(entries)) throw new TypeError("entries must be an array");
  if (entries.length > 65535) throw new RangeError("too many entries for a zip without zip64");
  const { time, date } = dosDateTime(now);

  const names = entries.map((e) => {
    checkPath(e.path);
    return te.encode(e.path);
  });

  let localSize = 0;
  let centralSize = 0;
  for (let i = 0; i < entries.length; i++) {
    localSize += 30 + names[i].length + entries[i].data.length;
    centralSize += 46 + names[i].length;
  }
  const total = localSize + centralSize + 22;
  if (total > 0xffffffff) throw new RangeError("zip too large without zip64");

  const out = new Uint8Array(total);
  const view = new DataView(out.buffer);
  const offsets = new Uint32Array(entries.length);
  let off = 0;

  for (let i = 0; i < entries.length; i++) {
    const { data } = entries[i];
    const name = names[i];
    const crc = crc32(data);
    offsets[i] = off;

    view.setUint32(off, LOCAL_SIG, true);
    view.setUint16(off + 4, VERSION, true);
    view.setUint16(off + 6, FLAG_UTF8, true);
    view.setUint16(off + 8, 0, true);            // method: stored
    view.setUint16(off + 10, time, true);
    view.setUint16(off + 12, date, true);
    view.setUint32(off + 14, crc, true);
    view.setUint32(off + 18, data.length, true); // compressed size
    view.setUint32(off + 22, data.length, true); // uncompressed size
    view.setUint16(off + 26, name.length, true);
    view.setUint16(off + 28, 0, true);           // extra field length
    out.set(name, off + 30);
    out.set(data, off + 30 + name.length);
    off += 30 + name.length + data.length;
  }

  const centralStart = off;
  for (let i = 0; i < entries.length; i++) {
    const { data } = entries[i];
    const name = names[i];
    const crc = crc32(data);

    view.setUint32(off, CENTRAL_SIG, true);
    view.setUint16(off + 4, VERSION, true);      // version made by
    view.setUint16(off + 6, VERSION, true);      // version needed
    view.setUint16(off + 8, FLAG_UTF8, true);
    view.setUint16(off + 10, 0, true);           // method: stored
    view.setUint16(off + 12, time, true);
    view.setUint16(off + 14, date, true);
    view.setUint32(off + 16, crc, true);
    view.setUint32(off + 20, data.length, true);
    view.setUint32(off + 24, data.length, true);
    view.setUint16(off + 28, name.length, true);
    view.setUint16(off + 30, 0, true);           // extra field length
    view.setUint16(off + 32, 0, true);           // comment length
    view.setUint16(off + 34, 0, true);           // disk number start
    view.setUint16(off + 36, 0, true);           // internal attributes
    view.setUint32(off + 38, 0, true);           // external attributes
    view.setUint32(off + 42, offsets[i], true);
    out.set(name, off + 46);
    off += 46 + name.length;
  }
  const centralDirSize = off - centralStart;

  view.setUint32(off, EOCD_SIG, true);
  view.setUint16(off + 4, 0, true);              // disk number
  view.setUint16(off + 6, 0, true);              // disk with central dir
  view.setUint16(off + 8, entries.length, true); // records on this disk
  view.setUint16(off + 10, entries.length, true); // total records
  view.setUint32(off + 12, centralDirSize, true);
  view.setUint32(off + 16, centralStart, true);
  view.setUint16(off + 20, 0, true);             // comment length

  return out;
}
