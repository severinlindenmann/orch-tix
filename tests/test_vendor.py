import hashlib
from pathlib import Path

VENDOR = Path(__file__).resolve().parents[1] / "fileshare" / "static" / "vendor"


def test_vendor_files_match_sha256sums():
    lines = [l for l in (VENDOR / "SHA256SUMS").read_text().splitlines() if l.strip()]
    names = {line.split()[1] for line in lines}
    assert names == {"markdown-it.min.js", "purify.min.js"}
    for line in lines:
        digest, name = line.split()
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name


def test_vendor_versions_are_pinned():
    assert b"14.1.0" in (VENDOR / "markdown-it.min.js").read_bytes()[:300]
    assert b"3.1.7" in (VENDOR / "purify.min.js").read_bytes()[:300]


def test_vendor_licenses_present():
    assert (VENDOR / "LICENSE-markdown-it").stat().st_size > 0
    assert (VENDOR / "LICENSE-dompurify").stat().st_size > 0
