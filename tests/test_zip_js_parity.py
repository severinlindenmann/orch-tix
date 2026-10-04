"""The drop page's stored ZIP writer (fileshare/static/js/zip.js, upload-links spec Task 5) produces
a ZIP that Python's own zipfile module reads back byte-for-byte: nested folders, non-ASCII (UTF-8)
names and contents all survive (global-constraints.md Review Focus 5)."""
import base64
import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ZIP_JS = Path(__file__).resolve().parent.parent / "fileshare" / "static" / "js" / "zip.js"

ENTRIES = {"dir/sub/ä.txt": "hello", "b.bin": bytes(range(256))}

SCRIPT = """
import { buildZip } from %s;
const te = new TextEncoder();
const entries = [
  { path: "dir/sub/ä.txt", data: te.encode("hello") },
  { path: "b.bin", data: Uint8Array.from({ length: 256 }, (_, i) => i) },
];
const zip = buildZip(entries, { now: new Date(2026, 0, 2, 3, 4, 5) });
let bin = "";
for (const b of zip) bin += String.fromCharCode(b);
process.stdout.write(btoa(bin));
"""


@pytest.fixture(scope="module")
def zip_bytes():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "--input-type=module", "-e", SCRIPT % json.dumps(ZIP_JS.as_uri())],
                         capture_output=True, text=True, check=True, timeout=30)
    return base64.b64decode(out.stdout)


def test_python_zipfile_reads_the_js_zip_back(zip_bytes):
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    assert zf.testzip() is None
    assert set(zf.namelist()) == set(ENTRIES)
    for name, want in ENTRIES.items():
        data = zf.read(name)
        want_bytes = want.encode("utf-8") if isinstance(want, str) else want
        assert data == want_bytes
    # UTF-8 names intact: the flag bit (0x800) is set on every entry, so zipfile decodes as UTF-8
    # rather than cp437.
    assert "dir/sub/ä.txt" in zf.namelist()
