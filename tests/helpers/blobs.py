"""Header-valid SHR1 blobs without any crypto. The server never decrypts, so this is all it can check."""
import os

HEADER_LEN = 34


def make_fake_blob(uuid_hex: str, key_version: int = 1, body_len: int = 32) -> bytes:
    header = (b"SHR1" + bytes([1, key_version]) + bytes.fromhex(uuid_hex)
              + (1 << 20).to_bytes(4, "big") + b"\x00" * 8)
    assert len(header) == HEADER_LEN
    return header + os.urandom(body_len)
