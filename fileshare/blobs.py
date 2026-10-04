"""Ciphertext on disk. The server checks the SHR1 header (§4.4) and nothing else — it has no key."""
import os
import re
import secrets
import time
from collections.abc import AsyncIterator
from pathlib import Path

HEADER_LEN = 34
MAGIC = b"SHR1"
TAG_LEN = 16
_UUID = re.compile(r"[0-9a-f]{32}")


class BlobError(Exception):
    pass


class TooLarge(BlobError):
    pass


def _check_uuid(uuid_hex: str) -> None:
    if not isinstance(uuid_hex, str) or not _UUID.fullmatch(uuid_hex):
        raise BlobError("uuid must be 32 lower-case hex characters")


class BlobStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.blobs_dir = self.root / "blobs"
        self.tmp_dir = self.root / "tmp"
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    async def receive(self, chunks: AsyncIterator[bytes], max_bytes: int) -> tuple[Path, int]:
        tmp = self.tmp_dir / f"{secrets.token_hex(16)}.part"
        size = 0
        f = open(tmp, "xb")
        try:
            async for chunk in chunks:
                size += len(chunk)
                if size > max_bytes:
                    f.close()
                    tmp.unlink(missing_ok=True)
                    raise TooLarge(f"upload exceeds {max_bytes} bytes")
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            if not f.closed:
                f.close()
            tmp.unlink(missing_ok=True)
            raise
        f.close()
        return tmp, size

    def validate_header(self, tmp: Path, uuid_hex: str) -> int:
        _check_uuid(uuid_hex)
        if Path(tmp).stat().st_size < HEADER_LEN + TAG_LEN:
            raise BlobError("blob shorter than header plus one tag")
        with open(tmp, "rb") as f:
            h = f.read(HEADER_LEN)
        if h[0:4] != MAGIC:
            raise BlobError("bad magic")
        if h[4] != 1:
            raise BlobError("unsupported blob version")
        if h[6:22].hex() != uuid_hex:
            raise BlobError("header uuid does not match metadata")
        return h[5]

    def path_for(self, uuid_hex: str) -> Path:
        _check_uuid(uuid_hex)
        return self.blobs_dir / uuid_hex[0:2] / uuid_hex[2:4] / f"{uuid_hex}.shr"

    def commit(self, tmp: Path, uuid_hex: str) -> Path:
        dest = self.path_for(uuid_hex)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, dest)
        return dest

    def discard(self, tmp: Path) -> None:
        Path(tmp).unlink(missing_ok=True)

    def delete(self, uuid_hex: str) -> None:
        self.path_for(uuid_hex).unlink(missing_ok=True)

    def sweep(self, live_uuids: set[str], tmp_max_age_s: int = 3600) -> None:
        cutoff = time.time() - tmp_max_age_s
        for p in self.tmp_dir.iterdir():
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        for p in self.blobs_dir.glob("*/*/*.shr"):
            if p.stem not in live_uuids:
                p.unlink(missing_ok=True)
