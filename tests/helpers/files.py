import json
import os

from fileshare.security import b64u_encode
from tests.helpers.blobs import make_fake_blob


def fake_meta(uuid_hex: str, key_version: int = 1) -> dict:
    return {
        "uuid": uuid_hex,
        "key_version": key_version,
        "wrapped_dek": b64u_encode(b"\x01" + os.urandom(60)),   # 61 bytes = sealed 32-byte DEK
        "enc_meta": b64u_encode(b"\x01" + os.urandom(40)),
    }


def upload(client, headers=None, uuid_hex=None, blob=None, meta=None):
    uuid_hex = uuid_hex or os.urandom(16).hex()
    blob = make_fake_blob(uuid_hex) if blob is None else blob
    meta = fake_meta(uuid_hex) if meta is None else meta
    return client.post(
        "/api/files",
        data={"meta": meta if isinstance(meta, str) else json.dumps(meta)},
        files={"blob": ("blob", blob, "application/octet-stream")},
        headers=headers or {},
    )


def fake_drop_meta(uuid_hex: str, key_version: int = 1) -> dict:
    return {
        "uuid": uuid_hex,
        "key_version": key_version,
        # eph_pub(65, [0]==0x04) ‖ seal(...)(61, [0]==0x01) = 126 bytes; see global-constraints.md
        "sealed_dek": b64u_encode(b"\x04" + os.urandom(64) + b"\x01" + os.urandom(60)),
        "enc_meta": b64u_encode(b"\x01" + os.urandom(40)),
    }


def drop(client, token, uuid_hex=None, blob=None, meta=None):
    uuid_hex = uuid_hex or os.urandom(16).hex()
    blob = make_fake_blob(uuid_hex) if blob is None else blob
    meta = fake_drop_meta(uuid_hex) if meta is None else meta
    return client.post(
        f"/api/public/u/{token}",
        data={"meta": meta if isinstance(meta, str) else json.dumps(meta)},
        files={"blob": ("blob", blob, "application/octet-stream")},
    )
