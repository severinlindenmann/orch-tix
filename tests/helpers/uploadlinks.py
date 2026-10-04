"""Upload links (spec §18): the transport (an authenticated TestClient/httpx session for `make_link`,
an unauthenticated one for `drop`, or the CLI's own Api) is injected as plain callables, so this module
knows nothing about sessions, cookies or base URLs -- only the crypto and the two HTTP calls. Tasks 5
(the drop page) and 6 (the CLI) reuse `drop`; Task 4's browser test and Task 6 both reuse `make_link`."""
from __future__ import annotations

import io
import os
from typing import Callable

from tests.helpers.sharing_mod import load


def make_link(post_json: Callable[[str, dict], dict], mk: bytes, key_version: int,
              label: str = "", ttl: str = "1d") -> dict:
    """Create an upload link as the owner does: post_json(path, body) -> the parsed JSON response
    (already status-checked by the caller). Returns {id, token, pub, uuid, url_path}; `url_path` is
    `/u/<token>` with no fragment -- the caller appends `#<b64u(pub)>` itself, since the key never
    crosses the wire."""
    s = load()
    link = s.new_upload_link_crypto(mk, label)
    body = {"uuid": link["uuid_hex"], "key_version": key_version,
            "wrapped_lpriv": link["wrapped_lpriv"], "enc_label": link["enc_label"], "ttl": ttl}
    made = post_json("/api/upload-links", body)
    return {"id": made["id"], "token": made["token"], "pub": link["pub"], "uuid": link["uuid"],
            "url_path": f"/u/{made['token']}"}


def drop(post_multipart: Callable[[str, dict, bytes], dict], get_json: Callable[[str], dict],
         token: str, pub: bytes, data: bytes, name: str, mime: str, note: str = "") -> dict:
    """What the drop page (or the CLI) does with a link's token and public key: GET the link's public
    meta, seal a fresh file DEK to `pub`, encrypt `data` under it, and POST the multipart upload.
    get_json(path) -> parsed JSON body; post_multipart(path, meta_dict, blob_bytes) -> parsed JSON
    body. Returns the server's {ok, size}."""
    s = load()
    meta = get_json(f"/api/public/u/{token}")
    key_version = meta["key_version"]
    link_uuid = bytes.fromhex(meta["uuid"])
    file_uuid = os.urandom(16)
    dek = os.urandom(32)
    sealed_dek = s.seal_dek_to_link(dek, pub, link_uuid, file_uuid)
    enc_meta = s.seal_file_meta(dek, file_uuid, {"name": name, "mime": mime, "note": note})
    blob = io.BytesIO()
    s.encrypt_stream(dek, file_uuid, key_version, io.BytesIO(data), blob)
    drop_meta = {"uuid": file_uuid.hex(), "key_version": key_version, "sealed_dek": sealed_dek, "enc_meta": enc_meta}
    return post_multipart(f"/api/public/u/{token}", drop_meta, blob.getvalue())
