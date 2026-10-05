#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["cryptography>=43"]
# ///
from __future__ import annotations

import argparse
import base64
import contextlib
import fnmatch
import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Iterator

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _CRYPTO_ERR: Exception | None = None
except Exception as exc:  # ImportError, or a broken native wheel
    AESGCM = ec = serialization = None
    InvalidTag = Exception
    _CRYPTO_ERR = exc

# =========================================================== crypto (SHR1, spec §4)

HEADER_LEN = 34
CHUNK = 1 << 20
MAX_CHUNK = 64 << 20
MAGIC = b"SHR1"
BLOB_VERSION = 1
ENV_VERSION = 1
TAG_LEN = 16

AAD_MK = b"sharing/mk/v1"
_AAD_DEK = b"sharing/dek/v1|"
_AAD_META = b"sharing/meta/v1|"
_AAD_TDEK = b"sharing/tdek/v1|"
_AAD_TICKET = b"sharing/ticket/v1|"
_AAD_TEVENT = b"sharing/tevent/v1|"
AAD_SETTINGS = b"sharing/settings/v1"
AAD_LINK_PREFIX = b"sharing/link/v1|"
LINK_KEY_LEN = 32
APPROVE_INFO_PREFIX = b"sharing/approve/v1|"   # both the HKDF info prefix and the AAD prefix (§4.6)
FP_PREFIX = b"sharing/fp/v1|"
P256_PUB_LEN = 65
BUNDLE_LEN = P256_PUB_LEN + 1 + 12 + 32 + TAG_LEN   # eph_pub || envelope(MK) = 126
_INFO_AUTH = b"sharing/auth/v1"
_INFO_KEK = b"sharing/kek/v1"

_B64U_RE = re.compile(r"^[A-Za-z0-9_-]*$")
_CODE_RE = re.compile(r"^shr1\.([A-Za-z0-9_-]{22})$")


class IntegrityError(Exception):
    """Ciphertext failed authentication or is malformed. Exit code 5."""


class Refused(Exception):
    """A local guard refused the operation. Exit code 6."""


def aad_dek(file_uuid: bytes) -> bytes:
    return _AAD_DEK + file_uuid


def aad_meta(file_uuid: bytes) -> bytes:
    return _AAD_META + file_uuid


def aad_tdek(ticket_uuid: bytes) -> bytes:
    """AAD_TDEK(uuid) of spec T3: binds a wrapped ticket DEK to one ticket."""
    return _AAD_TDEK + ticket_uuid


def aad_ticket(ticket_uuid: bytes) -> bytes:
    """AAD_TICKET(uuid) of spec T3: binds sealed ticket content to one ticket."""
    return _AAD_TICKET + ticket_uuid


def aad_tevent(ticket_uuid: bytes, event_uuid: bytes) -> bytes:
    """AAD_TEVENT(ticket_uuid, event_uuid) of spec T3: binds a sealed event body to one event."""
    return _AAD_TEVENT + ticket_uuid + event_uuid


def aad_link(file_uuid: bytes) -> bytes:
    """AAD_LINK(uuid) of spec §17: binds a link-wrapped DEK to one file."""
    return AAD_LINK_PREFIX + file_uuid


def aad_approve(device_id: str) -> bytes:
    return APPROVE_INFO_PREFIX + device_id.encode("utf-8")


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def unb64u(s: str) -> bytes:
    if not isinstance(s, str) or not _B64U_RE.match(s) or len(s) % 4 == 1:
        raise ValueError("invalid base64url")
    out = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    if b64u(out) != s:
        raise ValueError("non-canonical base64url")
    return out


def _check_len(name: str, value: bytes, n: int) -> None:
    if not isinstance(value, (bytes, bytearray)) or len(value) != n:
        raise ValueError(f"{name} must be {n} bytes")


def seal(key: bytes, pt: bytes, aad: bytes, nonce: bytes | None = None) -> bytes:
    _check_len("key", key, 32)
    if nonce is None:
        nonce = os.urandom(12)
    _check_len("nonce", nonce, 12)
    return bytes([ENV_VERSION]) + nonce + AESGCM(key).encrypt(nonce, pt, aad)


def open_(key: bytes, env: bytes, aad: bytes) -> bytes:
    _check_len("key", key, 32)
    if len(env) < 1 + 12 + TAG_LEN or env[0] != ENV_VERSION:
        raise IntegrityError("malformed envelope")
    try:
        return AESGCM(key).decrypt(env[1:13], env[13:], aad)
    except InvalidTag:
        raise IntegrityError("envelope failed authentication") from None


def _hkdf(ikm: bytes, info: bytes, length: int = 32, salt: bytes = b"") -> bytes:
    # RFC 5869; an empty salt equals HashLen zero bytes because HMAC zero-pads its key.
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm, block, counter = b"", b"", 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def derive(passphrase: str, salt: bytes, iterations: int) -> tuple[bytes, bytes]:
    pw = unicodedata.normalize("NFC", passphrase).encode("utf-8")
    root = hashlib.pbkdf2_hmac("sha256", pw, salt, iterations, 32)
    return _hkdf(root, _INFO_AUTH), _hkdf(root, _INFO_KEK)


def _read_full(src: BinaryIO, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = src.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _u32(i: int) -> bytes:
    return struct.pack(">I", i)


def encrypt_stream(dek: bytes, file_uuid: bytes, key_version: int, src: BinaryIO, dst: BinaryIO,
                   chunk_size: int = CHUNK, nonce_prefix: bytes | None = None) -> int:
    _check_len("dek", dek, 32)
    _check_len("file_uuid", file_uuid, 16)
    if not 0 <= key_version <= 255:
        raise ValueError("key_version must fit in one byte")
    if not 0 < chunk_size <= MAX_CHUNK:
        raise ValueError("bad chunk_size")
    if nonce_prefix is None:
        nonce_prefix = os.urandom(8)
    _check_len("nonce_prefix", nonce_prefix, 8)

    header = MAGIC + bytes([BLOB_VERSION, key_version]) + file_uuid + _u32(chunk_size) + nonce_prefix
    assert len(header) == HEADER_LEN
    aes = AESGCM(dek)
    dst.write(header)
    total = len(header)

    cur = _read_full(src, chunk_size)
    i = 0
    while True:
        nxt = _read_full(src, chunk_size) if len(cur) == chunk_size else b""
        is_last = len(nxt) == 0
        if i > 0xFFFFFFFF:
            raise ValueError("file too large for SHR1")
        ct = aes.encrypt(nonce_prefix + _u32(i), cur, header + _u32(i) + bytes([1 if is_last else 0]))
        dst.write(ct)
        total += len(ct)
        if is_last:
            return total
        cur = nxt
        i += 1


def decrypt_stream(dek: bytes, expected_uuid: bytes, src: BinaryIO, dst: BinaryIO) -> int:
    """dst may hold partial plaintext if this raises; callers write to a temp file."""
    _check_len("dek", dek, 32)
    _check_len("expected_uuid", expected_uuid, 16)
    header = _read_full(src, HEADER_LEN)
    if len(header) != HEADER_LEN or header[:4] != MAGIC or header[4] != BLOB_VERSION:
        raise IntegrityError("not an SHR1 blob")
    if not hmac.compare_digest(header[6:22], expected_uuid):
        raise IntegrityError("blob belongs to a different file")
    chunk_size = struct.unpack(">I", header[22:26])[0]
    if not 0 < chunk_size <= MAX_CHUNK:
        raise IntegrityError("bad chunk size")
    prefix = header[26:34]
    aes = AESGCM(dek)
    step = chunk_size + TAG_LEN

    cur = _read_full(src, step)
    i, total = 0, 0
    while True:
        if len(cur) < TAG_LEN:
            raise IntegrityError("truncated blob")
        nxt = _read_full(src, step) if len(cur) == step else b""
        is_last = len(nxt) == 0
        try:
            pt = aes.decrypt(prefix + _u32(i), cur, header + _u32(i) + bytes([1 if is_last else 0]))
        except InvalidTag:
            raise IntegrityError(f"chunk {i} failed authentication") from None
        dst.write(pt)
        total += len(pt)
        if is_last:
            return total
        cur = nxt
        i += 1


def canonical_json(obj) -> bytes:
    """The one canonical serialiser (sorted keys, compact, UTF-8) for sealed JSON: file metadata and
    settings. crypto.js canonicalJson produces the same bytes."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def seal_file_meta(dek: bytes, file_uuid: bytes, meta: dict, _nonce: bytes | None = None) -> str:
    """Metadata sealed under the file's DEK and AAD with a fresh nonce. The browser parses these bytes."""
    return b64u(seal(dek, canonical_json(meta), aad_meta(file_uuid), nonce=_nonce))


def seal_settings(mk: bytes, settings: dict, _nonce: bytes | None = None) -> str:
    """Account-wide settings (§15): seal(MK, canonical JSON, AAD_SETTINGS), base64url."""
    if not isinstance(settings, dict):
        raise TypeError("settings must be a JSON object")
    return b64u(seal(mk, canonical_json(settings), AAD_SETTINGS, nonce=_nonce))


def open_settings(mk: bytes, enc_settings) -> dict:
    """The whole decrypted settings object (unknown keys included). IntegrityError on any failure."""
    try:
        env = unb64u(enc_settings)
    except (ValueError, TypeError):
        raise IntegrityError("malformed settings envelope") from None
    try:
        obj = json.loads(open_(mk, env, AAD_SETTINGS).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IntegrityError("settings are not valid JSON") from None
    if not isinstance(obj, dict):
        raise IntegrityError("settings are not a JSON object")
    return obj


def new_file_crypto(mk: bytes, name: str, mime: str, note: str,
                    _rng: Callable[[int], bytes] | None = None) -> dict:
    rng = _rng or os.urandom
    file_uuid = rng(16)
    dek = rng(32)
    wrapped_dek = wrap_dek_under_mk(mk, dek, file_uuid, _nonce=rng(12))
    enc_meta = seal_file_meta(dek, file_uuid, {"name": name, "mime": mime, "note": note}, _nonce=rng(12))
    return {"uuid": file_uuid, "uuid_hex": file_uuid.hex(), "dek": dek,
            "wrapped_dek": wrapped_dek, "enc_meta": enc_meta}


def open_file_meta(mk: bytes, file_out: dict) -> tuple[dict, bytes]:
    """(meta, dek). meta is the whole decrypted object, so a re-seal keeps keys this version doesn't know."""
    if not file_out.get("wrapped_dek") or not file_out.get("enc_meta"):
        raise IntegrityError("file has no key (deleted)")
    try:
        file_uuid = bytes.fromhex(file_out["uuid"])
        wrapped, enc = unb64u(file_out["wrapped_dek"]), unb64u(file_out["enc_meta"])
    except (KeyError, ValueError, TypeError):
        raise IntegrityError("malformed file record") from None
    if len(file_uuid) != 16:
        raise IntegrityError("malformed file uuid")
    dek = open_(mk, wrapped, aad_dek(file_uuid))
    return _open_meta(dek, file_uuid, enc), dek


def _open_meta(dek: bytes, file_uuid: bytes, enc: bytes) -> dict:
    try:
        meta = json.loads(open_(dek, enc, aad_meta(file_uuid)).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IntegrityError("metadata is not valid JSON") from None
    if not isinstance(meta, dict) or not all(isinstance(meta.get(k), str) for k in ("name", "mime", "note")):
        raise IntegrityError("metadata has the wrong shape")
    return meta


# ------------------------------------------------------------ tickets (spec T3, T5)

def new_ticket_crypto(mk: bytes, content: dict, _rng: Callable[[int], bytes] | None = None) -> dict:
    """A fresh ticket DEK, wrapped under MK, and content sealed under it (T3). content = {title, body, fm}."""
    rng = _rng or os.urandom
    ticket_uuid = rng(16)
    dek = rng(32)
    wrapped_dek = seal(mk, dek, aad_tdek(ticket_uuid), nonce=rng(12))
    enc_content = seal_ticket_content(dek, ticket_uuid, content, _nonce=rng(12))
    return {"uuid": ticket_uuid.hex(), "key_version": 1, "wrapped_dek": b64u(wrapped_dek),
            "enc_content": enc_content, "_dek": dek}


def open_ticket(mk: bytes, ticket_out: dict) -> tuple[dict, bytes]:
    """(content, dek). content is the whole decrypted object, so a re-seal keeps unknown fm keys."""
    if not ticket_out.get("wrapped_dek") or not ticket_out.get("enc_content"):
        raise IntegrityError("ticket has no key (deleted)")
    try:
        ticket_uuid = bytes.fromhex(ticket_out["uuid"])
        wrapped, enc = unb64u(ticket_out["wrapped_dek"]), unb64u(ticket_out["enc_content"])
    except (KeyError, ValueError, TypeError):
        raise IntegrityError("malformed ticket record") from None
    if len(ticket_uuid) != 16:
        raise IntegrityError("malformed ticket uuid")
    dek = open_(mk, wrapped, aad_tdek(ticket_uuid))
    return _open_ticket_content(dek, ticket_uuid, enc), dek


def _open_ticket_content(dek: bytes, ticket_uuid: bytes, enc: bytes) -> dict:
    try:
        content = json.loads(open_(dek, enc, aad_ticket(ticket_uuid)).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IntegrityError("ticket content is not valid JSON") from None
    if (not isinstance(content, dict) or not isinstance(content.get("title"), str)
            or not isinstance(content.get("body"), str) or not isinstance(content.get("fm"), dict)):
        raise IntegrityError("ticket content has the wrong shape")
    return content


def seal_ticket_content(dek: bytes, ticket_uuid: bytes, content: dict, _nonce: bytes | None = None) -> str:
    """enc_content = seal(DEK, canonical JSON, AAD_TICKET(uuid)), base64url. An edit re-seals with a fresh nonce."""
    return b64u(seal(dek, canonical_json(content), aad_ticket(ticket_uuid), nonce=_nonce))


def seal_ticket_event(dek: bytes, ticket_uuid: bytes, event_uuid: bytes, body: dict,
                      _nonce: bytes | None = None) -> str:
    """enc_body = seal(DEK, canonical JSON, AAD_TEVENT(ticket_uuid, event_uuid)), base64url (T5)."""
    return b64u(seal(dek, canonical_json(body), aad_tevent(ticket_uuid, event_uuid), nonce=_nonce))


def open_ticket_event(dek: bytes, ticket_uuid: bytes, event_uuid: bytes, enc: str) -> dict:
    try:
        env = unb64u(enc)
    except (ValueError, TypeError):
        raise IntegrityError("malformed event envelope") from None
    try:
        body = json.loads(open_(dek, env, aad_tevent(ticket_uuid, event_uuid)).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IntegrityError("event body is not valid JSON") from None
    if not isinstance(body, dict):
        raise IntegrityError("event body is not a JSON object")
    return body


# ------------------------------------------------------------ spaces, mirrors, decisions, messages (A4)
#
# A space is one orch workspace; its label is sealed under MK. A mirror is an orch ticket snapshot sealed
# under its own DEK (wrapped under MK with the ticket AAD); its uuid is derived from (space, key, link
# generation), so re-linking after an unlink gets a new uuid and TIX id. Decisions from the phone are sealed
# under the mirror's DEK (or MK for a space-scoped ticket_request); agent messages under MK.

AAD_SPACE_PREFIX = b"sharing/space/v1|"
AAD_MIRROR_PREFIX = b"sharing/mirror/v1|"
AAD_DECISION_PREFIX = b"sharing/decision/v1|"
AAD_MSG_PREFIX = b"sharing/msg/v1|"


def aad_space(space_id: str) -> bytes:
    return AAD_SPACE_PREFIX + space_id.encode("ascii")


def aad_mirror(ticket_uuid: bytes) -> bytes:
    _check_len("ticket uuid", ticket_uuid, 16)
    return AAD_MIRROR_PREFIX + ticket_uuid


def aad_decision(scope: bytes, decision_uuid: bytes) -> bytes:
    """scope = the 16-byte mirror uuid, or the ASCII space id for a space-scoped ticket_request."""
    _check_len("decision uuid", decision_uuid, 16)
    return AAD_DECISION_PREFIX + scope + b"|" + decision_uuid


def aad_msg(msg_uuid: bytes) -> bytes:
    _check_len("message uuid", msg_uuid, 16)
    return AAD_MSG_PREFIX + msg_uuid


def mirror_uuid(space_id: str, key: str, gen: int) -> str:
    return hashlib.sha256(f"{space_id}|{key}|{gen}".encode("utf-8")).digest()[:16].hex()


def mirror_event_uuid(space_id: str, key: str, gen: int, rev: int) -> str:
    return hashlib.sha256(f"{space_id}|{key}|{gen}|{rev}".encode("utf-8")).digest()[:16].hex()


def _open_json(key: bytes, enc: str, aad: bytes, what: str) -> dict:
    try:
        obj = json.loads(open_(key, unb64u(enc), aad).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        raise IntegrityError(f"{what} is not valid sealed JSON") from None
    if not isinstance(obj, dict):
        raise IntegrityError(f"{what} is not a JSON object")
    return obj


def seal_space_label(mk: bytes, space_id: str, label: str, _nonce: bytes | None = None) -> str:
    return b64u(seal(mk, canonical_json({"label": label}), aad_space(space_id), nonce=_nonce))


def open_space_label(mk: bytes, space_id: str, enc: str) -> str:
    obj = _open_json(mk, enc, aad_space(space_id), "space label")
    if not isinstance(obj.get("label"), str):
        raise IntegrityError("space label has the wrong shape")
    return obj["label"]


def mirror_doc(doc: dict, rev: int, gen: int) -> dict:
    """The doc as sealed: the snapshot plus its mirror_rev and link generation, so a reader can tell an
    older snapshot (a rollback by the server) from the doc itself (batch 3 review m4)."""
    return {**doc, "mirror_rev": rev, "gen": gen}


def seal_mirror(dek: bytes, ticket_uuid: bytes, doc: dict, _nonce: bytes | None = None) -> str:
    return b64u(seal(dek, canonical_json(doc), aad_mirror(ticket_uuid), nonce=_nonce))


def open_mirror(dek: bytes, ticket_uuid: bytes, enc: str) -> dict:
    return _open_json(dek, enc, aad_mirror(ticket_uuid), "mirror")


def seal_decision(key: bytes, scope: bytes, decision_uuid: bytes, body: dict, _nonce: bytes | None = None) -> str:
    return b64u(seal(key, canonical_json(body), aad_decision(scope, decision_uuid), nonce=_nonce))


def open_inbox_item(mk: bytes, item: dict) -> dict:
    """A decision from /api/inbox/changes: under the mirror's DEK when it names a ticket, else under MK."""
    du = bytes.fromhex(item["uuid"])
    if item.get("ticket_uuid"):
        tu = bytes.fromhex(item["ticket_uuid"])
        dek = open_(mk, unb64u(item["ticket_wrapped_dek"]), aad_tdek(tu))
        return _open_json(dek, item["enc_body"], aad_decision(tu, du), "decision")
    return _open_json(mk, item["enc_body"], aad_decision(item["space"].encode("ascii"), du), "decision")


def seal_msg(mk: bytes, msg_uuid: bytes, body: dict, _nonce: bytes | None = None) -> str:
    return b64u(seal(mk, canonical_json(body), aad_msg(msg_uuid), nonce=_nonce))


def open_msg(mk: bytes, msg_uuid: bytes, enc: str) -> dict:
    return _open_json(mk, enc, aad_msg(msg_uuid), "message")


# ------------------------------------------------------------ public links (§17)

def new_link_key() -> bytes:
    return os.urandom(LINK_KEY_LEN)


def wrap_dek_for_link(dek: bytes, file_uuid: bytes, lk: bytes, _nonce: bytes | None = None) -> str:
    """wrapped_dek_link = seal(LK, DEK, AAD_LINK(uuid)), base64url. LK travels only in the URL fragment."""
    _check_len("dek", dek, 32)
    _check_len("file_uuid", file_uuid, 16)
    _check_len("link key", lk, LINK_KEY_LEN)
    return b64u(seal(lk, dek, aad_link(file_uuid), nonce=_nonce))


def open_link_dek(lk: bytes, file_uuid: bytes, wrapped_dek_link) -> bytes:
    """The file's DEK. IntegrityError for a wrong key, a wrong file, or anything malformed."""
    try:
        env = unb64u(wrapped_dek_link)
        _check_len("link key", lk, LINK_KEY_LEN)
        _check_len("file_uuid", file_uuid, 16)
    except (ValueError, TypeError):
        raise IntegrityError("malformed link envelope") from None
    dek = open_(lk, env, aad_link(file_uuid))
    if len(dek) != 32:
        raise IntegrityError("link envelope does not hold a DEK")
    return dek


def open_link_file(lk: bytes, public_out: dict) -> tuple[dict, bytes]:
    """(meta, dek) from GET /api/public/{token}; the meta is the whole decrypted object."""
    try:
        file_uuid = bytes.fromhex(public_out["uuid"])
        enc = unb64u(public_out["enc_meta"])
    except (KeyError, ValueError, TypeError):
        raise IntegrityError("malformed public file record") from None
    if len(file_uuid) != 16:
        raise IntegrityError("malformed file uuid")
    dek = open_link_dek(lk, file_uuid, public_out.get("wrapped_dek_link"))
    return _open_meta(dek, file_uuid, enc), dek


def parse_code(code: str) -> bytes:
    m = _CODE_RE.fullmatch(code or "")
    if not m:
        raise Refused("that is not a valid onboarding code (expected shr1.<22 characters>)")
    try:
        lookup = unb64u(m.group(1))
    except ValueError:
        raise Refused("that is not a valid onboarding code") from None
    if len(lookup) != 16:
        raise Refused("that is not a valid onboarding code")
    return lookup

# ----------------------------------------------------------- device approval (§4.6)


def _load_p256_pub(pub: bytes):
    if not isinstance(pub, (bytes, bytearray)) or len(pub) != P256_PUB_LEN or pub[0] != 4:
        raise ValueError("device public key must be a 65-byte uncompressed P-256 point")
    # from_encoded_point checks that the point is on the curve (and that coordinates are < p)
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), bytes(pub))


def _pub_bytes(key) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _p256_private(priv_int: int | None):
    if priv_int is None:
        return ec.generate_private_key(ec.SECP256R1())
    return ec.derive_private_key(priv_int, ec.SECP256R1())


def new_device_keypair(_priv_int: int | None = None) -> tuple[bytes, bytes]:
    priv = _p256_private(_priv_int)
    der = priv.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    return der, _pub_bytes(priv.public_key())


def fingerprint(pub: bytes) -> str:
    # 100 bits: the first 20 base32 chars of the hash, grouped 4-4-4-4-4 (§4.6). Must stay
    # byte-identical with fileshare/security.py and static/js/crypto.js.
    raw = base64.b32encode(hashlib.sha256(FP_PREFIX + bytes(pub)).digest()).decode("ascii")[:20]
    return "-".join(raw[i:i + 4] for i in range(0, 20, 4))


def approval_key(z: bytes, eph_pub: bytes, device_pub: bytes, device_id: str) -> bytes:
    return _hkdf(z, aad_approve(device_id), 32, salt=bytes(eph_pub) + bytes(device_pub))


def seal_to_device(mk: bytes, device_pub: bytes, device_id: str,
                   _eph_priv_int: int | None = None, nonce: bytes | None = None) -> bytes:
    """What the browser does on Approve; Python has it for tests, vectors and BrowserSim."""
    _check_len("mk", mk, 32)
    dev = _load_p256_pub(device_pub)
    eph = _p256_private(_eph_priv_int)
    eph_pub = _pub_bytes(eph.public_key())
    z = eph.exchange(ec.ECDH(), dev)
    wk = approval_key(z, eph_pub, device_pub, device_id)
    return eph_pub + seal(wk, mk, aad_approve(device_id), nonce=nonce)


def open_device_bundle(priv_pkcs8: bytes, device_pub: bytes, device_id: str, bundle: bytes) -> bytes:
    if not isinstance(bundle, (bytes, bytearray)) or len(bundle) != BUNDLE_LEN:
        raise IntegrityError("device bundle has the wrong length")
    try:
        priv = serialization.load_der_private_key(bytes(priv_pkcs8), password=None)
    except (ValueError, TypeError):
        raise IntegrityError("pending private key is unreadable") from None
    if not isinstance(priv, ec.EllipticCurvePrivateKey) or priv.curve.name != "secp256r1":
        raise IntegrityError("pending private key is not P-256")
    if not hmac.compare_digest(_pub_bytes(priv.public_key()), bytes(device_pub)):
        raise IntegrityError("private key does not match this device's public key")
    eph_pub = bytes(bundle[:P256_PUB_LEN])
    try:
        eph = _load_p256_pub(eph_pub)
    except ValueError:
        raise IntegrityError("device bundle carries an invalid ephemeral key") from None
    z = priv.exchange(ec.ECDH(), eph)
    wk = approval_key(z, eph_pub, device_pub, device_id)
    mk = open_(wk, bytes(bundle[P256_PUB_LEN:]), aad_approve(device_id))
    if len(mk) != 32:
        raise IntegrityError("device bundle does not hold a 32-byte key")
    return mk

# ----------------------------------------------------------- upload links (§18)

ULINK_PREFIX = b"sharing/ulink/v1|"
ULABEL_PREFIX = b"sharing/ulabel/v1|"
ULDEK_PREFIX = b"sharing/uldek/v1|"

ULINK_PRIV_LEN = 32 + 65            # d ‖ uncompressed pub, the wrapped_lpriv plaintext
SEALED_DEK_LEN = 65 + 1 + 12 + 32 + 16   # eph_pub ‖ envelope(dek): 65 + (1+12+32+16)


def aad_ulink(link_uuid: bytes) -> bytes:
    """AAD_ULINK(uuid): binds an upload link's own private key, wrapped under MK, to one link."""
    return ULINK_PREFIX + link_uuid


def aad_ulabel(link_uuid: bytes) -> bytes:
    """AAD_ULABEL(uuid): binds an upload link's (owner-only) label, wrapped under MK, to one link."""
    return ULABEL_PREFIX + link_uuid


def aad_uldek(link_uuid: bytes, file_uuid: bytes) -> bytes:
    """AAD_ULDEK(link_uuid, file_uuid): binds a DEK sealed to an upload link's key to one file."""
    return ULDEK_PREFIX + link_uuid + file_uuid


def wrap_dek_under_mk(mk: bytes, dek: bytes, file_uuid: bytes, _nonce: bytes | None = None) -> str:
    """wrapped_dek = seal(MK, DEK, AAD_DEK(uuid)), base64url. Exactly what new_file_crypto does; both
    the initial upload and an adopted upload-link file share this one path."""
    return b64u(seal(mk, dek, aad_dek(file_uuid), nonce=_nonce))


def new_upload_link_crypto(mk: bytes, label: str, _priv_int: int | None = None,
                          _rng: Callable[[int], bytes] | None = None, _nonce: bytes | None = None) -> dict:
    """A fresh P-256 keypair for one upload link. Its private scalar (d ‖ pub) is sealed under MK so
    only the owner can ever use it; an optional label is sealed the same way, under its own AAD, so a
    label is never visible to anyone who only holds the link. label == "" means no label at all."""
    rng = _rng or os.urandom
    link_uuid = rng(16)
    priv = _p256_private(_priv_int)
    pub = _pub_bytes(priv.public_key())
    d_bytes = priv.private_numbers().private_value.to_bytes(32, "big")
    wrapped_lpriv = b64u(seal(mk, d_bytes + pub, aad_ulink(link_uuid), nonce=_nonce or rng(12)))
    enc_label = None
    if label != "":
        enc_label = b64u(seal(mk, label.encode("utf-8"), aad_ulabel(link_uuid), nonce=rng(12)))
    return {"uuid": link_uuid, "uuid_hex": link_uuid.hex(), "pub": pub,
            "wrapped_lpriv": wrapped_lpriv, "enc_label": enc_label}


def open_upload_link_key(mk: bytes, link_uuid: bytes, wrapped_lpriv: str):
    """(priv, pub) for one upload link. Verifies the stored pub actually matches d, in case a
    wrapped_lpriv was ever produced or edited by anything other than new_upload_link_crypto."""
    try:
        env = unb64u(wrapped_lpriv)
    except (ValueError, TypeError):
        raise IntegrityError("malformed upload-link envelope") from None
    plain = open_(mk, env, aad_ulink(link_uuid))
    if len(plain) != ULINK_PRIV_LEN:
        raise IntegrityError("upload-link key has the wrong length")
    d_bytes, pub = plain[:32], plain[32:]
    priv = ec.derive_private_key(int.from_bytes(d_bytes, "big"), ec.SECP256R1())
    if not hmac.compare_digest(_pub_bytes(priv.public_key()), pub):
        raise IntegrityError("upload-link private key does not match its public key")
    return priv, pub


def open_upload_label(mk: bytes, link_uuid: bytes, enc_label: str | None) -> str:
    """The link's label, or "" when it has none."""
    if enc_label is None:
        return ""
    try:
        env = unb64u(enc_label)
    except (ValueError, TypeError):
        raise IntegrityError("malformed upload-link label envelope") from None
    try:
        return open_(mk, env, aad_ulabel(link_uuid)).decode("utf-8")
    except UnicodeDecodeError:
        raise IntegrityError("upload-link label is not valid UTF-8") from None


def _uldek_key(z: bytes, eph_pub: bytes, link_pub: bytes, link_uuid: bytes, file_uuid: bytes) -> bytes:
    return _hkdf(z, aad_uldek(link_uuid, file_uuid), 32, salt=bytes(eph_pub) + bytes(link_pub))


def seal_dek_to_link(dek: bytes, link_pub: bytes, link_uuid: bytes, file_uuid: bytes,
                     _eph_priv_int: int | None = None, _nonce: bytes | None = None) -> str:
    """What the uploader (drop page) does with the link's public key: sealed_dek = eph_pub ‖
    seal(wk, DEK, AAD_ULDEK), wk = HKDF-SHA256(ECDH(eph, link_pub), salt=eph_pub‖link_pub, info=AAD_ULDEK)."""
    _check_len("dek", dek, 32)
    link = _load_p256_pub(link_pub)
    eph = _p256_private(_eph_priv_int)
    eph_pub = _pub_bytes(eph.public_key())
    z = eph.exchange(ec.ECDH(), link)
    wk = _uldek_key(z, eph_pub, link_pub, link_uuid, file_uuid)
    return b64u(eph_pub + seal(wk, dek, aad_uldek(link_uuid, file_uuid), nonce=_nonce))


def open_sealed_dek(priv, link_pub: bytes, link_uuid: bytes, file_uuid: bytes, sealed: str) -> bytes:
    """The link owner's side: recovers the 32-byte DEK a drop-page upload sealed to this link."""
    try:
        blob = unb64u(sealed)
    except (ValueError, TypeError):
        raise IntegrityError("malformed sealed DEK") from None
    if len(blob) != SEALED_DEK_LEN:
        raise IntegrityError("sealed DEK has the wrong length")
    eph_pub = bytes(blob[:P256_PUB_LEN])
    try:
        eph = _load_p256_pub(eph_pub)
    except ValueError:
        raise IntegrityError("sealed DEK carries an invalid ephemeral key") from None
    z = priv.exchange(ec.ECDH(), eph)
    wk = _uldek_key(z, eph_pub, link_pub, link_uuid, file_uuid)
    dek = open_(wk, bytes(blob[P256_PUB_LEN:]), aad_uldek(link_uuid, file_uuid))
    if len(dek) != 32:
        raise IntegrityError("sealed envelope does not hold a 32-byte DEK")
    return dek

# =========================================================== paths & guards (Task 15)

SECRET_PATTERNS = (".env*", "*.env", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", "*.p12", "*.pfx",
                   "*.jks", "*.kdbx", ".netrc", ".npmrc", ".pgpass", "credentials*")
MAX_UPLOAD = 209_715_200
_NAME_BAD_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")   # C0, C1, bidi controls


def safe_name(name, fallback: str) -> str:
    """The decrypted name is attacker-controlled: keep only a harmless basename."""
    if not isinstance(name, str):
        return fallback
    base = re.split(r"[/\\]", name)[-1]
    if (base in ("", ".", "..") or base.startswith("-") or _NAME_BAD_RE.search(base)
            or len(base.encode("utf-8")) > 255):
        return fallback
    return base


def choose_target(share_dir: Path, file_id: str, name: str, force: bool) -> Path:
    base = Path(share_dir).resolve()
    first, second = base / name, base / f"{file_id}-{name}"
    for cand in (first, second):
        if cand.parent != base:
            raise Refused(f"refusing to write outside {base}")
    if force:
        if first.is_dir() and not first.is_symlink():
            raise Refused(f"{first} is a directory; refusing to overwrite it")
        return first
    for cand in (first, second):
        if not os.path.lexists(cand):
            return cand
    raise Refused(f"{first.name} and {second.name} already exist in {base}; use --force to overwrite {first.name}")


def _in_skill_dir(path: Path, repo_root: Path) -> bool:
    """Is path the skill folder or inside it, as written or after following symlinks? Compared by
    (st_dev, st_ino) as well as by name, so a differently-cased path on a case-insensitive disk
    (.CLAUDE/Skills/...) is caught too. Fails closed if it can't tell."""
    try:
        skill = Path(os.path.abspath(Path(repo_root) / REPO_CONFIG)).parent
        cands = (Path(os.path.abspath(path)), Path(path).resolve())
        if any(p == s or s in p.parents for p in cands for s in (skill, skill.resolve())):
            return True
        try:
            skill_st = os.stat(skill)
        except FileNotFoundError:
            return False
        for p in cands:
            for d in (p, *p.parents):
                try:
                    if os.path.samestat(os.stat(d), skill_st):
                        return True
                except FileNotFoundError:
                    continue
        return False
    except (OSError, RuntimeError):   # RuntimeError: a symlink loop on Python < 3.13
        return True


def is_secret_path(path: Path, repo_root: Path) -> bool:
    name = Path(path).name.lower()
    if any(fnmatch.fnmatchcase(name, pat) for pat in SECRET_PATTERNS):
        return True
    return _in_skill_dir(path, repo_root)

# =========================================================== config (Task 13)

VERSION = "2.4.0"   # semver; bump on every skill change — the server's /skill/manifest.json reads it
REPO_CONFIG = Path(".claude") / "skills" / "sharing" / "config.json"
OLD_CONFIG = "config.json.old"   # the identity --force is replacing; lives beside config.json until the new one is approved
TICKETS_SKILL_SRC = "tickets-SKILL.md"   # served beside SKILL.md; installed where Claude Code finds skills
TICKETS_SKILL_DIR = Path(".claude") / "skills" / "sharing-tickets"
EXCLUDE_LINES = ("/.claude/skills/sharing/", "/.claude/skills/sharing-tickets/", "/share/")
SELF_IGNORE = "*\n"
IS_WINDOWS = os.name == "nt"
PLATFORM = "windows" if IS_WINDOWS else ("darwin" if sys.platform == "darwin" else "linux")


class ConfigError(Exception):
    """This repo/device is not (correctly or yet) onboarded. Exit code 3."""

    def __init__(self, message: str, code: str = "not_configured"):
        super().__init__(message)
        self.code = code


class UsageError(Exception):
    """Bad command line. Exit code 1."""


class CliExit(Exception):
    """A failure with its own exit code and error code (e.g. from a public link's server, not this one)."""

    def __init__(self, code: int, err: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.err = err


def _pubkey_of(priv_pkcs8: bytes) -> bytes:
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_der_private_key(priv_pkcs8, password=None)
    return key.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)


@dataclass
class Config:
    repo_root: Path
    config_path: Path
    device_id: str
    device_name: str
    project: str
    server_url: str
    device_token: str
    mk: bytes | None
    key_version: int | None
    pending_privkey: bytes | None

    @property
    def status(self) -> str:
        return "active" if self.mk is not None else "pending"

    def fingerprint(self) -> str:
        """Only knowable locally while pending (the private key is deleted once MK arrives)."""
        return fingerprint(_pubkey_of(self.pending_privkey)) if self.pending_privkey else ""


def find_repo_root(start: Path) -> Path | None:
    """Nearest ancestor holding our repo config, or a .git (the installer's view of 'the repo')."""
    here = Path(start).resolve()
    for d in (here, *here.parents):
        if (d / REPO_CONFIG).is_file() or (d / ".git").exists():
            return d
    return None


# ---- the §8.2 guards

def ensure_self_ignore(dir_: Path) -> None:
    """A .gitignore containing '*' ignores the folder AND itself, in any repo, whatever else is configured."""
    dir_ = Path(dir_)
    dir_.mkdir(parents=True, exist_ok=True)
    gi = dir_ / ".gitignore"
    want = SELF_IGNORE.encode()
    try:
        st = os.lstat(gi)   # never follows a symlink: a committed `.gitignore -> ~/.bashrc` must not be written
    except FileNotFoundError:
        st = None
    if st is not None:
        if stat.S_ISLNK(st.st_mode):
            _warn(f"{gi} was a symlink; replaced it with a regular .gitignore (its target is untouched)")
            gi.unlink()
        elif not stat.S_ISREG(st.st_mode):
            _warn(f"{gi} is not a regular file; leaving it alone, so git may not ignore {dir_}")
            return
        else:
            with open(gi, "rb") as fh:
                if fh.read(len(want) + 1) == want:
                    return
    fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".gitignore-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(want)
        if not IS_WINDOWS:
            os.chmod(tmp, 0o644)
        os.replace(tmp, gi)   # replaces the name itself; a symlink put back meanwhile is not followed
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _in_git_tree(path: Path) -> bool:
    """A .git (dir, or a worktree/submodule file) at path or any ancestor."""
    here = Path(path).resolve()
    return any((d / ".git").exists() for d in (here, *here.parents))


def _git_exclude_path(repo_root: Path) -> Path | None:
    """git's own answer; if git is missing or errors, <root>/.git/info/exclude when .git is a directory."""
    try:
        r = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--git-path", "info/exclude"],
                           capture_output=True, text=True)
        out = r.stdout.strip() if r.returncode == 0 else ""
    except OSError:
        out = ""
    if out:
        p = Path(out)
        return p if p.is_absolute() else Path(repo_root) / p
    git_dir = Path(repo_root) / ".git"
    return git_dir / "info" / "exclude" if git_dir.is_dir() else None


def ensure_git_exclude(repo_root: Path) -> Path | None:
    """Second layer after the self-ignoring .gitignore files. Idempotent; keeps LF or CRLF as found."""
    path = _git_exclude_path(repo_root)
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = path.read_bytes().decode("utf-8", "replace") if path.exists() else ""
    nl = "\r\n" if "\r\n" in raw else "\n"
    missing = [line for line in EXCLUDE_LINES if line not in raw.splitlines()]
    if missing:
        add = ("" if not raw or raw.endswith("\n") else nl) + "".join(line + nl for line in missing)
        with open(path, "a", encoding="utf-8", newline="") as fh:
            fh.write(add)
    return path


def remove_git_exclude(repo_root: Path, lines: Iterable[str] = EXCLUDE_LINES) -> None:
    path = _git_exclude_path(repo_root)
    if path is None or not path.exists():
        return
    raw = path.read_bytes().decode("utf-8", "replace")
    nl = "\r\n" if "\r\n" in raw else "\n"
    drop = set(lines)
    kept = [line for line in raw.splitlines() if line not in drop]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("".join(line + nl for line in kept))


def git_tracked(repo_root: Path, rel: str) -> bool:
    """Fail closed: only 'exit 1' or 'no repository at all' mean not tracked; anything else is Refused."""
    try:
        r = subprocess.run(["git", "-C", str(repo_root), "ls-files", "--error-unmatch", "--", rel],
                           capture_output=True)
    except OSError as e:
        if not _in_git_tree(repo_root):
            return False
        raise Refused(f"cannot check whether {rel} is tracked by git: git could not be run ({e}). "
                      "Install git (and make sure it is on PATH), then re-run.") from None
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    if not _in_git_tree(repo_root):
        return False   # e.g. "not a git repository": nothing can be tracked
    err = (r.stderr or b"").decode("utf-8", "replace").strip()
    raise Refused(f"cannot check whether {rel} is tracked by git: git exited {r.returncode}: {err or '(no output)'}. "
                  "Resolve the git error shown above, then re-run.")


def _oem_encoding() -> str:
    """Console output of whoami/icacls is in the OEM code page, not UTF-8 and not the ANSI page."""
    try:
        import ctypes
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except (ImportError, AttributeError, OSError):
        return "utf-8"


def _parse_whoami_sid(csv_line: str) -> str:
    """`whoami /user /fo csv /nh` -> '"DOMAIN\\user","S-1-5-…"'. Only the SID is used: names are localised."""
    sid = csv_line.strip().rsplit(",", 1)[-1].strip().strip('"')
    if not re.fullmatch(r"S-1-\d+(-\d+)+", sid):
        raise Refused(f"could not determine the current Windows user SID from whoami: {csv_line!r}")
    return sid


def _windows_sid() -> str:
    r = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True)
    return _parse_whoami_sid(r.stdout.decode(_oem_encoding(), "replace"))


def _parse_icacls_save(data: bytes) -> str:
    """`icacls <path> /save <file>` output (UTF-16LE, optional BOM): line 1 = name, line 2 = SDDL."""
    text = data.decode("utf-16-le", "replace").lstrip("\ufeff")
    lines = [ln for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
    if len(lines) < 2 or "D:" not in lines[1]:
        raise Refused("could not read the file's ACL (icacls /save gave no SDDL)")
    return lines[1].strip()


def _sddl_trustees(sddl: str) -> list[str]:
    """Trustee SIDs (or SDDL aliases such as SY/BA/BU) of every ACE in the DACL; the SACL is ignored."""
    dacl = sddl.split("D:", 1)[1] if "D:" in sddl else ""
    dacl = dacl.split("S:", 1)[0]
    trustees = []
    for ace in re.findall(r"\(([^)]*)\)", dacl):
        parts = ace.split(";")
        if len(parts) >= 6:
            trustees.append(parts[5])
    return trustees


def _foreign_trustees(trustees: list[str], sid: str) -> list[str]:
    return [t for t in trustees if t.upper() != sid.upper()]


def _check_sddl(sddl: str, sid: str, path: Path) -> None:
    """Fail closed: the DACL must exist, be non-NULL, name the current user, and name nobody else."""
    fix = f'fix it with: icacls "{path}" /inheritance:r /grant:r "*{sid}:(F)"'
    if "D:" not in sddl:
        raise Refused(f"could not read the ACL of {path} (no DACL in {sddl!r}); {fix}")
    dacl = sddl.split("D:", 1)[1].split("S:", 1)[0]
    if "NO_ACCESS_CONTROL" in dacl.upper():
        raise Refused(f"{path} has a NULL DACL, so everyone has full access; {fix}")
    trustees = _sddl_trustees(sddl)
    if sid.upper() not in (t.upper() for t in trustees):
        raise Refused(f"the ACL of {path} does not grant the current user ({sid}), so it was not set by "
                      f"this tool or could not be read ({sddl!r}); {fix}")
    foreign = _foreign_trustees(trustees, sid)
    if foreign:
        raise Refused(f"{path} is readable by {', '.join(foreign)}; {fix}")


def _windows_sddl(path: Path) -> str:
    fd, tmp = tempfile.mkstemp(prefix=".acl-", suffix=".txt")
    os.close(fd)
    try:
        r = subprocess.run(["icacls", str(path), "/save", tmp, "/q"], capture_output=True)
        if r.returncode != 0:
            raise Refused(f"icacls could not read the ACL of {path}: "
                          f"{r.stderr.decode(_oem_encoding(), 'replace').strip()}")
        return _parse_icacls_save(Path(tmp).read_bytes())
    finally:
        os.unlink(tmp)


def secure_file(path: Path) -> None:
    """Owner-only: 0600 on POSIX; on Windows an ACL with exactly one ACE, the current user's SID."""
    if IS_WINDOWS:
        sid = _windows_sid()
        r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"], capture_output=True)
        if r.returncode != 0:
            raise Refused(f"could not restrict {path} to the current user: "
                          f"{r.stdout.decode(_oem_encoding(), 'replace').strip()}")
    else:
        os.chmod(path, 0o600)


def check_permissions(path: Path) -> None:
    if IS_WINDOWS:
        _check_sddl(_windows_sddl(path), _windows_sid(), path)
        return
    mode = stat.S_IMODE(Path(path).stat().st_mode)
    if mode & 0o077:
        raise Refused(f"{path} is readable by other users (mode {mode:03o}); fix it with: chmod 600 {path}")


def write_config(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        secure_file(Path(tmp))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _read_json(path: Path, what: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read {what} {path}: {e}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{what} {path} is not a JSON object")
    return data


def _config_from(root: Path, path: Path, data: dict) -> Config:
    broken = f"{path} is incomplete or corrupt — re-run the onboarding command with --force"
    try:
        if int(data.get("v", 0)) != 1:
            raise ConfigError(f"{path} has an unsupported format (v={data.get('v')!r}) — run `sharing update`")
        mk = unb64u(data["mk"]) if data.get("mk") else None
        priv = unb64u(data["pending_privkey"]) if data.get("pending_privkey") else None
        if mk is not None and len(mk) != 32:
            raise ConfigError(broken)
        if mk is None and priv is None:
            raise ConfigError(broken)
        dg = data.get("deepgram_api_key")   # a pre-§20 leftover: never used, but still redacted (below)
        if isinstance(dg, str):
            _remember_secret(dg.strip())
        return Config(repo_root=root, config_path=path, device_id=str(data["device_id"]),
                      device_name=str(data["device_name"]), project=str(data["project"]),
                      server_url=str(data["server_url"]).rstrip("/"), device_token=str(data["device_token"]),
                      mk=mk, key_version=int(data["key_version"]) if mk is not None else None,
                      pending_privkey=priv if mk is None else None)
    except (KeyError, TypeError, ValueError):
        raise ConfigError(broken) from None


def _guard_secret(root: Path, rel: Path) -> None:
    """A file holding a device secret must be untracked by git and owner-only."""
    if git_tracked(root, rel.as_posix()):
        raise Refused(f"{rel.as_posix()} is tracked by git — the key would be committed. "
                      f"Run: git rm --cached {rel.as_posix()}  (then treat the key as exposed if it was pushed)")
    check_permissions(root / rel)


def _share_inside_repo(repo_root: Path) -> bool:
    """<repo>/share, after following symlinks, is still inside the (resolved) repo."""
    try:
        return (Path(repo_root) / "share").resolve().is_relative_to(Path(repo_root).resolve())
    except (OSError, RuntimeError):
        return False


def ensure_tickets_skill(repo_root: Path) -> None:
    """Spec T8: copy the downloaded tickets-SKILL.md to .claude/skills/sharing-tickets/SKILL.md when it is
    missing there or different, in a folder that ignores itself. Runs on every CLI start; best effort
    (a failure only warns), and a no-op for an install that has no tickets-SKILL.md yet."""
    root = Path(repo_root)
    src = root / REPO_CONFIG.parent / TICKETS_SKILL_SRC
    dst_dir = root / TICKETS_SKILL_DIR
    try:
        if not src.is_file():
            return
        # never write through a committed symlink that leads out of the repo (like share/)
        if dst_dir.is_symlink() or not dst_dir.resolve().is_relative_to(root.resolve()):
            _warn(f"{TICKETS_SKILL_DIR.as_posix()} is a symlink or leaves the repo; not installing the tickets skill")
            return
        want = src.read_bytes()
        dst = dst_dir / "SKILL.md"
        ensure_self_ignore(dst_dir)
        if dst.is_symlink() or not dst.is_file() or dst.read_bytes() != want:
            fd, tmp = tempfile.mkstemp(dir=dst_dir, prefix=".SKILL-", suffix=".md")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(want)
                if not IS_WINDOWS:
                    os.chmod(tmp, 0o644)
                os.replace(tmp, dst)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
                raise
    except (OSError, RuntimeError) as e:
        _warn(f"could not install the sharing-tickets skill ({e})")


def load_config(start: Path, require_active: bool = True) -> Config:
    root = find_repo_root(start)
    if root is None or not (root / REPO_CONFIG).is_file():
        raise ConfigError("this repo is not onboarded — run the onboarding command from the web UI "
                          "(Onboard device) in this repo")
    path = root / REPO_CONFIG
    _guard_secret(root, REPO_CONFIG)
    if (path.parent / OLD_CONFIG).exists():   # the identity a pending --force is replacing: same guards
        _guard_secret(root, REPO_CONFIG.parent / OLD_CONFIG)
    ensure_self_ignore(path.parent)
    if _share_inside_repo(root):   # never write a .gitignore through a share/ symlink that leaves the repo
        ensure_self_ignore(root / "share")   # §8.2: re-created if missing or changed, on every run
    ensure_git_exclude(root)
    ensure_tickets_skill(root)
    cfg = _config_from(root, path, _read_json(path, "sharing config"))
    if require_active and cfg.status == "pending":
        raise ConfigError(f"this device is pending approval — approve fingerprint {cfg.fingerprint()} in the "
                          "web UI (Devices), then run `sharing wait`", code="pending")
    return cfg



# =========================================================== http client (Task 13)

EXIT_CONFLICT = 7   # spec T8: a 409 from the tickets API (someone else moved first)
CONFLICT_CODES = ("bad_move", "claimed", "claim_lost", "conflict", "duplicate_uuid")


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail

    @property
    def exit_code(self) -> int:
        if self.status == 409 and self.code in CONFLICT_CODES:
            return EXIT_CONFLICT
        if self.status in (404, 410):
            return 2
        if self.code in ("forbidden", "not_owner"):   # another device's file or space: a refusal, not a broken device
            return 6
        if self.status in (401, 403):
            return 3
        if self.status == 0:
            return 4
        return 1


_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def check_server_url(url: str) -> str:
    """https://, or http:// only to loopback. Returns the URL without a trailing slash; Refused otherwise."""
    try:
        u = urllib.parse.urlsplit(str(url).strip())
        host = (u.hostname or "").lower()
        _ = u.port   # raises ValueError on a malformed port
    except ValueError:
        raise Refused(f"not a valid server URL: {_clean(str(url))!r}") from None
    if u.scheme == "https" and host:
        return urllib.parse.urlunsplit(u).rstrip("/")
    if u.scheme == "http" and host in _LOOPBACK_HOSTS:
        return urllib.parse.urlunsplit(u).rstrip("/")
    raise Refused(f"refusing server URL {_clean(str(url))!r}: it must use https:// "
                  "(plain http:// is allowed only for 127.0.0.1, localhost or ::1)")


def _build_opener() -> urllib.request.OpenerDirector:
    """Like urllib's default opener but WITHOUT HTTPRedirectHandler: a 3xx is an error, never followed,
    so the bearer token is never re-sent to wherever a redirect points."""
    o = urllib.request.OpenerDirector()
    for h in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(), urllib.request.HTTPHandler(),
              urllib.request.HTTPSHandler(), urllib.request.HTTPDefaultErrorHandler(),
              urllib.request.HTTPErrorProcessor()):
        o.add_handler(h)
    return o


@contextlib.contextmanager
def _net_errors() -> Iterator[None]:
    """Map failures while reading a response (reset, timeout, truncated body) to ApiError(0, 'network')."""
    try:
        yield
    except (http.client.HTTPException, OSError) as e:
        raise ApiError(0, "network", f"connection failed while reading the response: {e or type(e).__name__}") from None


class Api:
    TIMEOUT = 120

    def __init__(self, server_url: str, token: str | None):
        self.base = check_server_url(server_url)
        self.token = token
        self._opener = _build_opener()

    def _req(self, method: str, path: str, data=None, headers: dict | None = None):
        h = {"User-Agent": f"sharing/{VERSION}", "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            return self._opener.open(req, timeout=self.TIMEOUT)
        except urllib.error.HTTPError as e:
            raise self._http_error(e) from None
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            raise ApiError(0, "network", str(getattr(e, "reason", e))) from None

    @staticmethod
    def _http_error(e: urllib.error.HTTPError) -> ApiError:
        if 300 <= e.code < 400:
            return ApiError(e.code, "redirect", f"the server answered {e.code} (a redirect to "
                            f"{_clean(e.headers.get('Location') or '?')!r}); redirects are never followed — "
                            "check server_url in the config")
        try:
            body = json.loads(e.read() or b"{}")
        except (ValueError, http.client.HTTPException, OSError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        return ApiError(e.code, str(body.get("error") or f"http_{e.code}"), str(body.get("detail") or ""))

    @staticmethod
    def _parse(r) -> dict:
        """A 2xx body must be empty or a JSON object. Anything else (a captive portal, a proxy page, a
        truncated body) is ApiError(0, 'bad_response'): status 0, so exit 4, like other network trouble."""
        with _net_errors():
            raw = r.read()
        if not raw:
            return {}
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ApiError(0, "bad_response", "the server's response was not the expected JSON "
                           "(is server_url pointing at the sharing server?)")
        return body

    def get_json(self, path: str, headers: dict | None = None) -> dict:
        with self._req("GET", path, headers=headers) as r:
            return self._parse(r)

    def post_json(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        with self._req("POST", path, data=data, headers={"Content-Type": "application/json"}) as r:
            return self._parse(r)

    def delete(self, path: str, headers: dict | None = None) -> None:
        with self._req("DELETE", path, headers=headers) as r, _net_errors():
            r.read()

    def send(self, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
        """Any method, an optional JSON body; the (possibly empty, e.g. 204) response as a dict."""
        data = json.dumps(body).encode("utf-8") if body is not None else b""
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        with self._req(method, path, data=data, headers=h) as r:
            return self._parse(r)

    def _multipart(self, meta: dict, blob_path: Path) -> tuple[dict, Iterator[bytes], int]:
        boundary = "sharing-" + secrets.token_hex(16)
        head = (f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="meta"\r\n'
                "Content-Type: application/json\r\n\r\n"
                f"{json.dumps(meta)}\r\n"
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="blob"; filename="blob.shr"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        length = len(head) + blob_path.stat().st_size + len(tail)

        def body() -> Iterator[bytes]:
            yield head
            with open(blob_path, "rb") as fh:
                while chunk := fh.read(1 << 16):
                    yield chunk
            yield tail

        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(length)}
        return headers, body(), length

    def upload_to(self, path: str, meta: dict, blob_path: Path) -> dict:
        headers, body, _ = self._multipart(meta, blob_path)
        with self._req("POST", path, data=body, headers=headers) as r:
            return self._parse(r)

    def upload(self, meta: dict, blob_path: Path) -> dict:
        return self.upload_to("/api/files", meta, blob_path)

    def download(self, path: str, dst: BinaryIO) -> None:
        """Streams the (ciphertext) body into dst. Only read errors map to 'network'; a failing dst.write
        (disk full, …) propagates as itself. dst may be partial on error: callers use a temp file."""
        with self._req("GET", path, headers={"Accept": "application/octet-stream"}) as r:
            expected = r.headers.get("Content-Length")
            got = 0
            while True:
                with _net_errors():
                    chunk = r.read(1 << 16)
                if not chunk:
                    break
                dst.write(chunk)
                got += len(chunk)
            # read(amt) returns b"" on a connection closed early instead of raising IncompleteRead.
            if expected is not None and expected.isdigit() and got != int(expected):
                raise ApiError(0, "network", f"the connection closed after {got} of {expected} bytes")



# =========================================================== commands (Tasks 13–15)

_REF_RE = re.compile(r"^(?:file)?([1-9][0-9]{0,11})$", re.IGNORECASE)
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")   # C0, C1 and bidi controls
_UNSAFE_RE = re.compile(r"[\x80-\x9f\u202a-\u202e\u2066-\u2069]")   # what --json would pass through raw


def norm_ref(ref: str) -> str:
    m = _REF_RE.match((ref or "").strip())
    if not m:
        raise UsageError(f"not a file ID: {_clean(ref)!r} (expected e.g. FILE7 or 7)")
    return f"FILE{int(m.group(1))}"


def plaintext_size(ct_size: int, chunk_size: int = CHUNK) -> int:
    body = ct_size - HEADER_LEN
    if body < TAG_LEN:
        return 0
    chunks = -(-body // (chunk_size + TAG_LEN))
    return body - TAG_LEN * chunks


def describe_file(mk: bytes, file_out: dict) -> dict:
    d = {"id": file_out["id"], "name": None, "mime": None, "size": plaintext_size(int(file_out["size"])),
         "note": "", "device": (file_out.get("device") or {}).get("name") or "browser",
         "project": file_out.get("project", ""), "created_at": file_out.get("created_at"),
         "deleted_at": file_out.get("deleted_at"), "acked_at": file_out.get("acked_at"),
         "acked_by": file_out.get("acked_by"), "expires_at": file_out.get("expires_at"), "transcript": None,
         "tags": _file_tags(file_out)}
    if not d["deleted_at"]:
        try:
            meta, _ = open_file_meta(mk, file_out)
            t = transcript_summary(meta)
            if t is not None:
                t = {k: _strip_unsafe(v) for k, v in t.items()}
            d.update(name=_strip_unsafe(meta["name"]), mime=_strip_unsafe(meta["mime"]),
                     note=_strip_unsafe(meta["note"]), transcript=t)
        except IntegrityError:
            d["undecryptable"] = True
    return d


def _stored_transcript(meta: dict) -> dict | None:
    t = meta.get("transcript")
    return t if isinstance(t, dict) and isinstance(t.get("text"), str) else None


def transcript_summary(meta: dict) -> dict | None:
    """What list/info/get show about a transcript: never its text (untrusted, and possibly long)."""
    t = _stored_transcript(meta)
    if t is None:
        return None
    return {"language": t.get("language"), "model": t.get("model"), "words": len(t["text"].split()),
            "by": t.get("by"), "created_at": t.get("created_at")}


def _strip_unsafe(s):
    """Untrusted decrypted strings in --json output: drop C1 and bidi controls (JSON escapes only C0)."""
    return _UNSAFE_RE.sub("", s) if isinstance(s, str) else s


def _clean(s) -> str:
    """Server-supplied text must not drive the terminal: replace control characters."""
    return _CTRL_RE.sub("?", s) if isinstance(s, str) else ""


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# Tags (spec §19): cleartext labels. The same rules as the server's fileshare/tags.py, so a bad tag
# fails here (exit 1) before anything is encrypted or sent.
_TAG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}", re.ASCII)
MAX_TAGS = 10


def normalize_tag(raw) -> str:
    """Trim, lowercase, turn spaces and underscores into '-', then require ^[a-z0-9][a-z0-9-]{0,39}$."""
    tag = re.sub(r"[ _]", "-", raw.strip().lower()) if isinstance(raw, str) else ""
    if not _TAG_RE.fullmatch(tag):
        raise UsageError(f"bad tag {_clean(raw)!r}: use a-z, 0-9 and '-', starting with a letter or digit, "
                         "at most 40 characters")
    return tag


def normalize_tags(raw: Iterable[str]) -> list[str]:
    """Normalised, deduplicated and sorted; at most MAX_TAGS once duplicates are gone."""
    tags = sorted({normalize_tag(t) for t in raw})
    if len(tags) > MAX_TAGS:
        raise UsageError(f"a file can carry at most {MAX_TAGS} tags ({len(tags)} given)")
    return tags


def _file_tags(file_out: dict) -> list[str]:
    """The tags of a server file object: strings only, C1 and bidi controls dropped (as for --json)."""
    raw = file_out.get("tags")
    return [_strip_unsafe(t) for t in raw if isinstance(t, str)] if isinstance(raw, list) else []


def _tags_text(tags: list[str]) -> str:
    return ", ".join(_clean(t) for t in tags) or "—"


def _tag_query(tags: Iterable[str]) -> str:
    return "".join("&tag=" + urllib.parse.quote(t, safe="") for t in tags)


# ============================================================= frontmatter (spec T8)
#
# A restricted YAML subset, with no dependency beyond stdlib. static/js/frontmatter.js implements
# the same grammar; tests/vectors/frontmatter.json pins the shared cases in both languages.
#
# Grammar: `key: scalar`, scalars are plain/quoted strings, integers, true/false, null/~; inline
# lists `[a, "b, c"]`; block lists of scalars (`- x`) or of mappings (`- id: q1` then more-indented
# keys); nested mappings by consistent indentation; `#` comments at line start or after whitespace,
# outside quotes. As in YAML, a quote opens a quoted scalar only at the start of a value, a key, a
# list item or an inline-list item; `it's` or `the "save" button` mid-value are literal. Anything
# else is a UsageError naming the 1-based line.

_INT_RE = re.compile(r"^-?[0-9]+$")


_QUOTES = ('"', "'")


def _close_quote(line: str, i: int) -> int:
    """line[i] opens a quoted scalar: the index of its closing quote. ValueError if it never closes."""
    j = line.find(line[i], i + 1)
    if j < 0:
        raise ValueError("unclosed quote")
    return j


def _skip_spaces(line: str, i: int) -> int:
    while i < len(line) and line[i] in " \t":
        i += 1
    return i


def _comment_at(line: str, i: int) -> bool:
    return line[i] == "#" and (i == 0 or line[i - 1] in " \t")


def _scan_node(line: str, i: int, allow_key: bool) -> int:
    """From the start of a node (a key, a value or a list item) at i: the index where a comment
    starts, or len(line). As in YAML, a quote opens a quoted scalar only at the START of a node or
    of an inline-list item; anywhere else ' and " are literal characters of a plain scalar."""
    n = len(line)
    if i < n and line[i] in _QUOTES:
        # after a quoted scalar: only whitespace, then the end, a comment, or (for a key) ': '
        i = _skip_spaces(line, _close_quote(line, i) + 1)
        if i == n or _comment_at(line, i):
            return i
        if allow_key and line[i] == ":" and (i + 1 == n or line[i + 1] == " "):
            return _scan_node(line, _skip_spaces(line, i + 1), False)
        raise ValueError("unexpected text after a quoted value")
    elif i < n and line[i] == "[":
        i += 1
        item_start = True
        while i < n:
            ch = line[i]
            if ch == "]":
                i += 1
                break
            if _comment_at(line, i):
                return i
            if ch == ",":
                item_start = True
            elif ch in " \t":
                pass
            elif item_start and ch in _QUOTES:
                i = _close_quote(line, i)
                item_start = False
            else:
                item_start = False
            i += 1
    while i < n:
        if _comment_at(line, i):
            return i
        if allow_key and line[i] == ":" and (i + 1 == n or line[i + 1] == " "):
            return _scan_node(line, _skip_spaces(line, i + 1), False)
        i += 1
    return n


def _strip_comment(line: str) -> str:
    """The line up to a '#' that starts a comment (at line start or after whitespace, outside a
    quoted scalar). Raises ValueError on a quoted scalar that never closes."""
    i = _skip_spaces(line, 0)
    while line.startswith("- ", i):   # list item markers
        i = _skip_spaces(line, i + 2)
    return line[:_scan_node(line, i, True)]


def _logical_lines(text: str, _line_offset: int = 0) -> list[tuple[int, int, str]]:
    """(line_no, indent, content) for every non-blank, non-comment-only line. CRLF and lone CR are
    normalised to LF first, so line numbers count the same either way."""
    physical = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    for idx, raw in enumerate(physical):
        line_no = idx + 1 + _line_offset
        if raw.strip() == "":
            continue
        indent_str = re.match(r"[ \t]*", raw).group(0)
        if "\t" in indent_str:
            raise UsageError(f"frontmatter line {line_no}: tabs are not allowed in indentation")
        try:
            stripped = _strip_comment(raw)
        except ValueError as e:
            raise UsageError(f"frontmatter line {line_no}: {e}") from None
        content = stripped[len(indent_str):].rstrip()
        if content == "":
            continue
        out.append((line_no, len(indent_str), content))
    return out


def _find_key_colon(content: str) -> int | None:
    """The index of the ':' that separates a mapping key from its value (followed by a space or end
    of line, and outside a key that is quoted), or None if content doesn't look like `key: value`."""
    i = _skip_spaces(content, 0)
    if i < len(content) and content[i] in _QUOTES:
        j = content.find(content[i], i + 1)
        i = len(content) if j < 0 else j + 1
    for k in range(i, len(content)):
        if content[k] == ":" and (k + 1 == len(content) or content[k + 1] == " "):
            return k
    return None


def _split_key(content: str, line_no: int) -> tuple[str, str]:
    i = _find_key_colon(content)
    if i is None:
        raise UsageError(f"frontmatter line {line_no}: expected 'key: value'")
    return content[:i].strip(), content[i + 1:]


def _parse_scalar(s: str) -> str | int | bool | None:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    if s == "true":
        return True
    if s == "false":
        return False
    if s in ("null", "~"):
        return None
    if _INT_RE.match(s):
        return int(s)
    return s


def _split_inline_list(inner: str, line_no: int) -> list[str]:
    """The raw items of `[...]`, split at commas. A quote opens a quoted item only at the start of
    an item (after spaces); inside a plain item, ' and " are literal."""
    if inner.strip() == "":
        return []
    items, cur, i, n = [], "", 0, len(inner)
    while i < n:
        ch = inner[i]
        if ch in _QUOTES and cur.strip() == "":
            j = inner.find(ch, i + 1)
            if j < 0:
                raise UsageError(f"frontmatter line {line_no}: unclosed quote")
            cur += inner[i:j + 1]
            i = j + 1
            while i < n and inner[i] in " \t":
                i += 1
            if i < n and inner[i] != ",":
                raise UsageError(f"frontmatter line {line_no}: unexpected text after a quoted value")
            continue
        if ch == ",":
            items.append(cur)
            cur = ""
        else:
            cur += ch
        i += 1
    items.append(cur)
    return items


def _parse_value(s: str, line_no: int):
    if s.startswith("["):
        if not s.endswith("]"):
            raise UsageError(f"frontmatter line {line_no}: unclosed '['")
        return [_parse_scalar(item.strip()) for item in _split_inline_list(s[1:-1], line_no)]
    return _parse_scalar(s)


def _parse_mapping(lines: list[tuple[int, int, str]], i: int, indent: int) -> tuple[dict, int]:
    result: dict = {}
    while i < len(lines):
        line_no, cur_indent, content = lines[i]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise UsageError(f"frontmatter line {line_no}: inconsistent indentation")
        if content.startswith("- "):
            raise UsageError(f"frontmatter line {line_no}: expected a mapping key, found a list item")
        key, rest = _split_key(content, line_no)
        if key in result:
            raise UsageError(f"frontmatter line {line_no}: duplicate key {key!r}")
        i += 1
        rest = rest.strip()
        if rest == "":
            if i < len(lines) and lines[i][1] > indent:
                child_indent = lines[i][1]
                if lines[i][2].startswith("- "):
                    value, i = _parse_block_list(lines, i, child_indent)
                else:
                    value, i = _parse_mapping(lines, i, child_indent)
            else:
                value = None
        else:
            value = _parse_value(rest, line_no)
        result[key] = value
    return result, i


def _parse_block_list(lines: list[tuple[int, int, str]], i: int, indent: int) -> tuple[list, int]:
    items: list = []
    while i < len(lines):
        line_no, cur_indent, content = lines[i]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise UsageError(f"frontmatter line {line_no}: inconsistent indentation")
        if not content.startswith("- "):
            raise UsageError(f"frontmatter line {line_no}: expected a list item")
        item_content = content[2:]
        item_indent = indent + 2
        if _find_key_colon(item_content) is None:
            items.append(_parse_value(item_content.strip(), line_no))
            i += 1
        else:
            synth = list(lines)
            synth[i] = (line_no, item_indent, item_content)
            obj, i = _parse_mapping(synth, i, item_indent)
            items.append(obj)
    return items, i


def parse_yaml_subset(text: str, _line_offset: int = 0) -> dict:
    """The restricted subset's grammar (module docstring above). Root is always a mapping."""
    lines = _logical_lines(text, _line_offset)
    if not lines:
        return {}
    obj, i = _parse_mapping(lines, 0, lines[0][1])
    if i != len(lines):
        raise UsageError(f"frontmatter line {lines[i][0]}: inconsistent indentation")
    return obj


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """(fm, body). A leading '---' line starts a YAML block, closed by a line that is exactly '---';
    everything after belongs to body untouched. No leading '---' line: fm is {} and body is all of text."""
    physical = text.split("\n")
    if not physical or physical[0].rstrip("\r") != "---":
        return {}, text
    closing = next((i for i in range(1, len(physical)) if physical[i].rstrip("\r") == "---"), None)
    if closing is None:
        raise UsageError("frontmatter line 1: unterminated frontmatter block")
    fm = parse_yaml_subset("\n".join(physical[1:closing]), _line_offset=1)
    return fm, "\n".join(physical[closing + 1:])


def _needs_quote(s: str) -> bool:
    if s == "" or s != s.strip():
        return True
    if s in ("true", "false", "null", "~") or _INT_RE.match(s):
        return True
    if s[0] in ("-", '"', "'"):
        return True
    return any(tok in s for tok in (": ", " #", "[", "]", ","))


def _quote(s: str) -> str:
    if '"' not in s:
        return '"' + s + '"'
    if "'" not in s:
        return "'" + s + "'"
    return '"' + s.replace('"', "'") + '"'   # last resort; the round-trip vectors never need it


def _emit_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return _quote(v) if _needs_quote(v) else v
    raise TypeError(f"cannot emit scalar of type {type(v).__name__}")


def _emit_inline_list(items: list) -> str:
    return "[" + ", ".join(_emit_scalar(x) for x in items) + "]"


def _emit_mapping(obj: dict, level: int) -> list[str]:
    pad = "  " * level
    lines = []
    for key, value in obj.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.extend(_emit_mapping(value, level + 1))
        elif isinstance(value, list):
            if value and all(isinstance(x, dict) for x in value):
                lines.append(f"{pad}{key}:")
                lines.extend(_emit_block_list(value, level + 1))
            else:
                lines.append(f"{pad}{key}: {_emit_inline_list(value)}")
        else:
            lines.append(f"{pad}{key}: {_emit_scalar(value)}")
    return lines


def _emit_block_list(items: list[dict], level: int) -> list[str]:
    pad, prefix = "  " * level, "  " * (level + 1)
    lines = []
    for item in items:
        if not isinstance(item, dict) or not item:
            raise TypeError("a block list of mappings needs non-empty dict items")
        sub = _emit_mapping(item, level + 1)
        lines.append(pad + "- " + sub[0][len(prefix):])
        lines.extend(sub[1:])
    return lines


def emit_yaml_subset(obj: dict) -> str:
    """The inverse of parse_yaml_subset: 2-space indents, inline lists of scalars, block lists of
    mappings. Quotes whatever would otherwise parse as another type or is ambiguous unquoted."""
    if not isinstance(obj, dict):
        raise TypeError("emit_yaml_subset expects a dict")
    lines = _emit_mapping(obj, 0)
    return "\n".join(lines) + ("\n" if lines else "")


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} GB"


TTLS = ("1d", "7d", "30d", "never")
DEFAULT_TTL = "7d"


def _parse_ts(s) -> datetime | None:
    try:
        return datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def expiry_text(expires_at, now: datetime | None = None) -> str:
    """'expires in 6d' / '5h' / '12m', 'expired', or 'never' (expires_at None)."""
    if not expires_at:
        return "never"
    at = _parse_ts(expires_at)
    if at is None:
        return "expires ?"
    left = (at - (now or datetime.now(timezone.utc))).total_seconds()
    if left <= 0:
        return "expired"
    if left >= 86400:
        return f"expires in {int(left // 86400)}d"
    if left >= 3600:
        return f"expires in {int(left // 3600)}h"
    return f"expires in {max(1, -(-int(left) // 60))}m"


def _display_path(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(p)


_SECRETS: set[str] = set()   # exact secret values seen this run, redacted from every output


def _remember_secret(value: str | None) -> None:
    if value and len(value) >= 8:
        _SECRETS.add(value)


def _known_secrets() -> set[str]:
    found = set(_SECRETS)
    env = os.environ.get("DEEPGRAM_API_KEY", "").strip()
    if len(env) >= 8:
        found.add(env)
    try:   # best effort, for a failure before the config was loaded; never raises
        root = find_repo_root(Path.cwd())
        if root is not None:
            dg = json.loads((root / REPO_CONFIG).read_text(encoding="utf-8")).get("deepgram_api_key")
            if isinstance(dg, str) and len(dg) >= 8:
                found.add(dg)
    except Exception:
        pass
    return found


def _redact_exact(text: str) -> str:
    """Replace every known secret, raw and in its JSON-escaped forms (a key holding `"` or `\\`
    looks different once `--json` output has been serialised)."""
    forms = set()
    for secret in _known_secrets():
        forms |= {secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]}
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, "…")
    return text


def _out(args, obj, human: str) -> None:
    if getattr(args, "json", False):
        print(_redact_exact(json.dumps(obj, ensure_ascii=False)))
    else:
        print(_redact_exact(human))


def _iter_files(api: Api, include_deleted: bool, include_acked: bool = False,
                tags: Iterable[str] = ()) -> Iterator[dict]:
    """Newest first. The server hides acknowledged live files unless acked=1 (tombstones always come).
    `tags` filter on the server: a file must carry all of them."""
    before = None
    tag_q = _tag_query(tags)
    while True:
        page = api.get_json("/api/files?limit=50" + ("&acked=1" if include_acked else "") + tag_q
                            + (f"&before={before}" if before else ""))
        for f in page.get("files", []):
            if f.get("deleted_at") and not include_deleted:
                continue
            yield f
        before = page.get("next_before")
        if not before:
            return


def _missing(e: ApiError, ref: str) -> ApiError:
    """404/410 on a file route, reworded; anything else unchanged."""
    if e.status == 410:
        return ApiError(410, "deleted", f"{ref} was deleted or has expired (IDs are never reused)")
    if e.status == 404:
        return ApiError(404, "not_found", f"{ref} does not exist")
    return e


def _file_path(ref: str, suffix: str = "") -> str:
    return f"/api/files/{urllib.parse.quote(ref, safe='')}{suffix}"


def _fetch_file(api: Api, ref: str) -> dict:
    try:
        return api.get_json(_file_path(ref))
    except ApiError as e:
        raise _missing(e, ref) from None


def _remote_skill_version(server_url: str) -> str | None:
    """Best effort: the version /skill/manifest.json advertises, or None if it can't be read."""
    try:
        v = Api(server_url, None).get_json("/skill/manifest.json").get("version")
    except (ApiError, ValueError, AttributeError):
        return None
    return str(v) if v else None


def _positive_int(v: str) -> int:
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


# ---------------------------------------------------------------- whoami / list / info

def cmd_whoami(args) -> int:
    cfg = load_config(Path.cwd())
    info = {"device_id": cfg.device_id, "device": cfg.device_name, "project": cfg.project,
            "server": cfg.server_url, "repo": str(cfg.repo_root), "key_version": cfg.key_version,
            "fingerprint": None, "reachable": True, "skill_version": VERSION,
            "latest_skill_version": None, "update_available": False}
    code = 0
    try:
        me = Api(cfg.server_url, cfg.device_token).get_json("/api/whoami")
        info["fingerprint"] = (me.get("device") or {}).get("fingerprint")
    except ApiError as e:
        if e.status != 0:
            raise
        info["reachable"] = False
        code = 4
    if info["reachable"]:
        latest = _remote_skill_version(cfg.server_url)
        info["latest_skill_version"] = latest
        info["update_available"] = latest is not None and latest != VERSION
    skill_line = f"skill    {VERSION}"
    if info["update_available"]:
        skill_line += f"  (update available: {info['latest_skill_version']} — run `sharing update`)"
    human = "\n".join([
        f"device   {_clean(cfg.device_name)} ({cfg.device_id})"
        + (f"  fingerprint {info['fingerprint']}" if info["fingerprint"] else ""),
        f"project  {_clean(cfg.project)}",
        f"server   {cfg.server_url}  ({'reachable' if info['reachable'] else 'unreachable'})",
        f"repo     {cfg.repo_root}",
        f"key      version {cfg.key_version}",
        skill_line,
    ])
    _out(args, info, human)
    return code


def cmd_list(args) -> int:
    cfg = load_config(Path.cwd())
    api = Api(cfg.server_url, cfg.device_token)
    _try_adopt_pending(api, cfg)
    rows = []
    tags = normalize_tags(args.tag)
    for f in _iter_files(api, include_deleted=args.all, include_acked=args.all, tags=tags):
        d = describe_file(cfg.mk, f)
        if args.from_device and d["device"] != args.from_device:
            continue
        if args.project and d["project"] != args.project:
            continue
        rows.append(d)
        if len(rows) >= args.n:
            break
    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print("no files")
        return 0
    print(f"{'ID':<9} {'NAME':<32} {'SIZE':>9}  {'DEVICE':<16} {'PROJECT':<16} SHARED")
    for d in rows:
        if d["deleted_at"]:
            name = "(deleted)"
        elif d["name"] is None:
            name = "(cannot decrypt)"
        else:
            name = _clean(d["name"])
        acked = f"  acked by {_clean(d['acked_by'] or '?')}" if d["acked_at"] else ""
        print(f"{d['id']:<9} {_trunc(name, 32):<32} {_human_size(d['size']):>9}  "
              f"{_trunc(_clean(d['device']), 16):<16} {_trunc(_clean(d['project']), 16):<16} "
              f"{d['created_at']}  {expiry_text(d['expires_at']) if not d['deleted_at'] else ''}{acked}")
        if d["note"]:
            print(f"{'':<9} note: {_clean(d['note'])}")
        if d["tags"]:
            print(f"{'':<9} tags: {_tags_text(d['tags'])}")
    return 0


def cmd_info(args) -> int:
    cfg = load_config(Path.cwd())
    api = Api(cfg.server_url, cfg.device_token)
    _try_adopt_pending(api, cfg)
    f = _fetch_file(api, norm_ref(args.ref))
    d = describe_file(cfg.mk, f)
    if d.get("undecryptable"):
        raise IntegrityError(f"cannot decrypt the metadata of {d['id']} — corrupt, tampered with, or another key")
    human = "\n".join([
        f"{d['id']}  {_clean(d['name'])}",
        f"  type     {_clean(d['mime'])}",
        f"  size     {_human_size(d['size'])}",
        f"  from     {_clean(d['device'])} · {_clean(d['project'])}",
        f"  shared   {d['created_at']}",
        f"  expiry   {expiry_text(d['expires_at'])}" + (f" ({d['expires_at']})" if d["expires_at"] else ""),
        f"  acked    " + (f"by {_clean(d['acked_by'] or '?')} · {d['acked_at']}" if d["acked_at"] else "no"),
        f"  note     {_clean(d['note']) or '—'}",
        f"  tags     {_tags_text(d['tags'])}",
    ])
    t = d.get("transcript")
    if t:
        human += f"\n  transcript: yes ({_clean(str(t['language'] or '?'))}, {t['words']} words)"
    elif audio_type(d):
        human += "\n  transcript: no — transcribe it in the web app (press Transcribe in the file view)"
    _out(args, d, human)
    return 0


# ---------------------------------------------------------------- parser + dispatch

_REGISTRARS: list[Callable] = []


def command(fn: Callable) -> Callable:
    """Decorator: fn(sub, common) adds one subcommand. Later tasks add commands with it."""
    _REGISTRARS.append(fn)
    return fn


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(f"{message}\n{self.format_usage().strip()}")


@command
def _reg_whoami(sub, common):
    p = sub.add_parser("whoami", parents=[common], help="show this device's identity and check the server")
    p.set_defaults(func=cmd_whoami)


@command
def _reg_list(sub, common):
    p = sub.add_parser("list", parents=[common], help="list shared files, newest first")
    p.add_argument("-n", "--limit", dest="n", type=_positive_int, default=20, help="how many (default 20)")
    p.add_argument("--tag", action="append", default=[], metavar="TAG",
                   help="only files carrying this tag (repeatable: all must match)")
    p.add_argument("--from", dest="from_device", metavar="DEVICE", help="only files from this device name")
    p.add_argument("--project", help="only files from this project")
    p.add_argument("--all", action="store_true", help="include acknowledged and deleted files")
    p.set_defaults(func=cmd_list)


@command
def _reg_info(sub, common):
    p = sub.add_parser("info", parents=[common], help="show one file's metadata and note")
    p.add_argument("ref", metavar="ID")
    p.set_defaults(func=cmd_info)


def build_parser() -> argparse.ArgumentParser:
    common = _Parser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    p = _Parser(prog="sharing", description="Share files between your devices (end-to-end encrypted).")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    for reg in _REGISTRARS:
        reg(sub, common)
    return p


_TOKEN_RE = re.compile(r"shd_[A-Za-z0-9_-]+")
_CLAIM_TOKEN_RE = re.compile(r"clm_[A-Za-z0-9_-]+")   # ticket claim tokens (claims.json)
_DEEPGRAM_KEY_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{40}(?![0-9A-Fa-f])")   # a leftover key's old format
_TEXT_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")   # like _CTRL_RE, keeps \t and \n


def _redact(text) -> str:
    """Secrets never reach the terminal or a log: device tokens, and a leftover deepgram_api_key (the exact
    configured values, and anything shaped like one — old repos may still hold one in config.json)."""
    text = _redact_exact(_CLAIM_TOKEN_RE.sub("clm_…", _TOKEN_RE.sub("shd_…", str(text))))
    return _DEEPGRAM_KEY_RE.sub("…", text)


def _warn(msg: str) -> None:
    print(f"sharing: warning: {_redact(_clean(msg))}", file=sys.stderr)


def _fail(as_json: bool, code: int, err: str, detail: str) -> int:
    # Defence in depth: whatever reaches here, no control or bidi character drives the terminal.
    detail = _redact(_TEXT_CTRL_RE.sub("?", str(detail)))
    if as_json:
        print(json.dumps({"error": err, "detail": detail}, ensure_ascii=False))
    else:
        print(f"sharing: {detail}", file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    if _CRYPTO_ERR is not None:
        print(f"sharing: encryption is unavailable ({_CRYPTO_ERR}); refusing to run — nothing is ever sent "
              "unencrypted. Run through the 'sharing' wrapper (uv run --script) so 'cryptography' is installed.",
              file=sys.stderr)
        return 5
    try:
        args = build_parser().parse_args(argv)
    except UsageError as e:
        return _fail("--json" in (argv if argv is not None else sys.argv[1:]), 1, "usage", str(e))
    except SystemExit as e:  # --help
        return int(e.code or 0)
    as_json = getattr(args, "json", False)
    try:
        return args.func(args)
    except UsageError as e:
        return _fail(as_json, 1, "usage", str(e))
    except ConfigError as e:
        return _fail(as_json, 3, e.code, str(e))
    except Refused as e:
        return _fail(as_json, 6, "refused", str(e))
    except IntegrityError as e:
        return _fail(as_json, 5, "integrity",
                     f"{e} — the data is corrupt, was tampered with, or was encrypted with another key")
    except ApiError as e:
        if e.code == "bad_response":
            detail = e.detail
        elif e.status == 0:
            detail = f"cannot reach the server ({e.detail})"
        elif e.status == 401:
            detail = e.detail or "the server no longer accepts this device (revoked?) — re-onboard with --force"
            if "revoked" not in detail:
                detail += " (revoked or unknown device)"
        elif e.code == "pending":
            detail = "this device is still pending approval — approve it in the web UI, then run `sharing wait`"
        else:
            detail = e.detail or e.code
        return _fail(as_json, e.exit_code, e.code, detail)
    except CliExit as e:
        return _fail(as_json, e.code, e.err, str(e))
    except KeyboardInterrupt:
        return 130
    except Exception as e:   # last resort: --json callers still get JSON on stdout, never a traceback
        return _fail(as_json, 1, "internal", f"unexpected error: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- onboarding (_handshake / wait)

APPROVAL_TIMEOUT_S = 900
APPROVAL_POLL_S = 3.0
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$", re.ASCII)   # the server's rule for device and project names


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _banner(fp: str, device: str, project: str) -> None:
    line = "─" * 44
    for text in (line, "  Approve this device in the web UI", f"  fingerprint  {fp}",
                 f"  device       {_clean(device)} · {_clean(project)}", line,
                 "waiting for approval (up to 15 min; Ctrl-C is safe — resume with `sharing wait`)…"):
        print(f"sharing: {text}", file=sys.stderr, flush=True)


def _withdraw(data: dict, what: str) -> None:
    """Best effort DELETE /api/devices/self with the token in data; a failure only warns."""
    try:
        Api(str(data["server_url"]), str(data["device_token"])).delete("/api/devices/self")
    except (ValueError, KeyError, TypeError, Refused, ApiError) as e:
        print(f"sharing: warning: could not revoke {what} ({_clean(str(e))}); revoke it on the Devices page",
              file=sys.stderr)


def _transient(e: ApiError) -> bool:
    """Worth retrying later: no answer at all (status 0), rate limited, or a server-side error."""
    return e.status == 0 or e.status == 429 or e.status >= 500


def _retire_old(skill_dir: Path, final: bool = False) -> bool:
    """After the new identity is approved: revoke the replaced device, then drop config.json.old.

    config.json.old is deleted only once the revoke succeeded or the server says the device is already
    gone (401/404). On a transient failure it is kept, so the next `sharing wait` (or any retire) tries
    again — unless final (uninstall), where the folder is removed anyway. Returns True if it is gone."""
    old_path = skill_dir / OLD_CONFIG
    if not old_path.exists():
        return True
    try:
        old = json.loads(old_path.read_text(encoding="utf-8"))
        if not isinstance(old, dict):
            raise ValueError("not a JSON object")
        api = Api(str(old["server_url"]), str(old["device_token"]))
    except (OSError, ValueError, KeyError, TypeError, Refused) as e:
        # Can never succeed from this file: warn and drop it rather than retrying forever.
        print(f"sharing: warning: could not read the replaced device's config ({_clean(str(e))}); "
              "revoke it on the Devices page", file=sys.stderr)
        old_path.unlink(missing_ok=True)
        return True
    try:
        api.delete("/api/devices/self")
    except ApiError as e:
        if e.status in (401, 404):
            pass   # already revoked (e.g. in the web UI) or gone: nothing to warn about
        elif _transient(e) and not final:
            print(f"sharing: warning: could not revoke the replaced device yet ({_clean(str(e))}); it stays in "
                  f"{OLD_CONFIG} and is retried on the next `sharing wait` — or revoke it on the Devices page",
                  file=sys.stderr)
            return False
        else:
            print(f"sharing: warning: could not revoke the replaced device ({_clean(str(e))}); "
                  "revoke it on the Devices page", file=sys.stderr)
    old_path.unlink(missing_ok=True)
    return True


def _restore_old(skill_dir: Path) -> bool:
    old_path = skill_dir / OLD_CONFIG
    if old_path.exists():
        os.replace(old_path, skill_dir / "config.json")
        return True
    return False


def wait_for_approval(cfg: Config, timeout_s: float = 900, interval_s: float = 3.0) -> Config:
    """Poll GET /api/devices/self/bundle until approved (-> active config), rejected, or timed out."""
    skill_dir = cfg.config_path.parent
    api = Api(cfg.server_url, cfg.device_token)
    pub = _pubkey_of(cfg.pending_privkey)
    fp = fingerprint(pub)
    resume = f"still waiting for approval of fingerprint {fp} — approve it in the web UI, then run `sharing wait`"
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                body = api.get_json("/api/devices/self/bundle")
            except ApiError as e:
                if e.status == 401:
                    cfg.config_path.unlink(missing_ok=True)
                    restored = _restore_old(skill_dir)
                    raise ConfigError("this device was rejected in the web UI (or revoked); its config was removed"
                                      + (" and the previous identity restored" if restored else "")
                                      + " — generate a new link to onboard again", code="rejected") from None
                if not _transient(e):
                    raise
                body = {}   # network blip, 429 or 5xx: keep polling until the deadline
            if body.get("device_bundle"):
                try:
                    bundle, key_version = unb64u(str(body["device_bundle"])), int(body["key_version"])
                except (KeyError, TypeError, ValueError):
                    raise IntegrityError("the server sent a malformed device bundle") from None
                mk = open_device_bundle(cfg.pending_privkey, pub, cfg.device_id, bundle)
                data = _read_json(cfg.config_path, "sharing config")
                data.pop("pending_privkey", None)
                data.update(mk=b64u(mk), key_version=key_version)
                write_config(cfg.config_path, data)
                _retire_old(skill_dir)
                return _config_from(cfg.repo_root, cfg.config_path, data)
            if time.monotonic() >= deadline:
                raise ConfigError(resume, code="pending")
            _sleep(interval_s)
    except KeyboardInterrupt:
        raise ConfigError(resume, code="pending") from None


def _approved_summary(args, cfg: Config) -> int:
    exclude = ensure_git_exclude(cfg.repo_root)
    result = {"device_id": cfg.device_id, "device": cfg.device_name, "project": cfg.project,
              "server": cfg.server_url, "status": cfg.status, "config": str(cfg.config_path),
              "git_exclude": str(exclude) if exclude else None}
    human = "\n".join([
        f"approved: {_clean(cfg.device_name)} · {_clean(cfg.project)} ({cfg.device_id}) on {cfg.server_url}",
        f"  config       {cfg.config_path}  (owner-only)",
        "  git ignore   .claude/skills/sharing/.gitignore (*), share/.gitignore (*)"
        + (f", {exclude}" if exclude else ""),
    ])
    _out(args, result, human)
    return 0


def _set_aside_current(skill_dir: Path) -> None:
    """--force, after the new handshake succeeded: move the current identity to config.json.old.

    If config.json.old already exists, the current config.json is either an earlier --force replacement
    still pending (withdraw it; config.json.old stays the original identity) or an approved one whose
    predecessor was never retired (retire that first)."""
    cfg_path, old_path = skill_dir / "config.json", skill_dir / OLD_CONFIG
    if not cfg_path.exists():
        return
    if old_path.exists():
        try:
            cur = _read_json(cfg_path, "sharing config")
        except ConfigError:
            cur = {}
        if not cur.get("mk"):
            if cur:
                _withdraw(cur, "the earlier, still pending replacement")
            cfg_path.unlink()
            return
        _retire_old(skill_dir, final=True)   # .old is about to be overwritten: no later retry is possible
    os.replace(cfg_path, old_path)
    secure_file(old_path)


def cmd_handshake(args) -> int:
    server = check_server_url(args.server)
    raw = sys.stdin.read() if args.code == "-" else args.code
    lookup = parse_code(raw.strip())   # strips the CR/LF a PowerShell pipe appends
    for label, value in (("device", args.device), ("project", args.project)):
        if not _NAME_RE.fullmatch(value or ""):
            raise Refused(f"{label} name {_clean(value)!r} is not allowed: use 1–64 of A-Z a-z 0-9 . _ -")
    skill_dir = Path(args.skill_dir).resolve()
    repo_root = Path(args.repo).resolve()
    cfg_path = skill_dir / "config.json"

    if cfg_path.exists() and not args.force:
        raise Refused(f"this repo is already onboarded ({cfg_path}); re-run with --force to replace it")
    try:
        rel = skill_dir.relative_to(repo_root).as_posix()
    except ValueError:
        rel = None
    if rel and git_tracked(repo_root, rel):   # fails closed when git is missing but a .git exists
        raise Refused(f"files under {rel} are tracked by git — the device key would be committed. "
                      f"Run: git rm -r --cached {rel}")

    ensure_self_ignore(skill_dir)
    ensure_self_ignore(repo_root / "share")
    ensure_git_exclude(repo_root)

    priv, pub = new_device_keypair()
    local_fp = fingerprint(pub)
    body = {"lookup": b64u(lookup), "device_name": args.device, "project": args.project,
            "hostname": args.hostname, "platform": args.platform, "device_pub": b64u(pub)}
    try:
        resp = Api(server, None).post_json("/api/handshake", body)
    except ApiError as e:
        if e.status in (403, 410):
            raise ApiError(401, "refused", "the onboarding code was refused (expired, already used, or "
                                           "cancelled) — generate a new link in the web UI") from None
        raise
    try:
        device_id, token = str(resp["device_id"]), str(resp["device_token"])
    except KeyError:
        raise ApiError(0, "bad_response", "the server's handshake response is missing fields") from None
    if resp.get("fingerprint") != local_fp:
        with contextlib.suppress(ApiError, Refused):
            Api(server, token).delete("/api/devices/self")
        raise IntegrityError("the server reports a different fingerprint for this device's key")

    _set_aside_current(skill_dir)
    write_config(cfg_path, {"v": 1, "device_id": device_id, "device_name": args.device,
                            "project": args.project, "server_url": server, "device_token": token,
                            "pending_privkey": b64u(priv)})
    _banner(local_fp, args.device, args.project)
    cfg = _config_from(repo_root, cfg_path, _read_json(cfg_path, "sharing config"))
    if args.no_wait:
        _out(args, {"device_id": device_id, "status": "pending", "fingerprint": local_fp,
                    "config": str(cfg_path)},
             f"pending approval: fingerprint {local_fp} — approve it in the web UI, then run `sharing wait`")
        return 0
    return _approved_summary(args, wait_for_approval(cfg, APPROVAL_TIMEOUT_S, APPROVAL_POLL_S))


def cmd_wait(args) -> int:
    cfg = load_config(Path.cwd(), require_active=False)
    if cfg.status == "active":
        _retire_old(cfg.config_path.parent)   # a replaced identity left over from an interrupted run
        _out(args, {"device_id": cfg.device_id, "status": "active"}, "already approved")
        return 0
    _banner(cfg.fingerprint(), cfg.device_name, cfg.project)
    return _approved_summary(args, wait_for_approval(cfg, APPROVAL_TIMEOUT_S, APPROVAL_POLL_S))


@command
def _reg_handshake(sub, common):
    p = sub.add_parser("_handshake", parents=[common], help="(internal) used by the onboarding script")
    p.add_argument("--code", required=True, help="the shr1.… code, or - to read it from stdin")
    p.add_argument("--server", required=True)
    p.add_argument("--repo", default=".")
    p.add_argument("--skill-dir", default=str(Path(__file__).resolve().parent))
    p.add_argument("--device", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--hostname", default=socket.gethostname())
    p.add_argument("--platform", default=PLATFORM, choices=("darwin", "linux", "windows"))
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-wait", action="store_true", help="write the pending config and return immediately")
    p.set_defaults(func=cmd_handshake)


@command
def _reg_wait(sub, common):
    p = sub.add_parser("wait", parents=[common], help="finish onboarding once this device is approved")
    p.set_defaults(func=cmd_wait)


# ---------------------------------------------------------------- share / get / rm / uninstall / update

_MIME_FALLBACK = {".md": "text/markdown", ".markdown": "text/markdown", ".json": "application/json",
                  ".txt": "text/plain", ".log": "text/plain", ".yaml": "text/yaml", ".yml": "text/yaml",
                  ".csv": "text/csv", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf"}
UPDATABLE = ("SKILL.md", TICKETS_SKILL_SRC, "sharing", "sharing.cmd", "sharing.py")   # replacement order: sharing.py last
# A 1.5.x update never fetched tickets-SKILL.md, so the 1.6.0 it installed lacks it: `update` at the
# same version restores just these (verified like any other download).
RESTORABLE = (TICKETS_SKILL_SRC,)
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _guess_mime(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return _MIME_FALLBACK.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def _secret_msg(name: str) -> str:
    return (f"{_clean(name)!r} looks like a secret (.env, keys, credentials); refusing to share it. "
            "Pass --allow-secret only if the user explicitly asked for this exact file.")


def ciphertext_size(pt_size: int, chunk_size: int = CHUNK) -> int:
    """Exactly what encrypt_stream writes for pt_size bytes (an empty file is still one tagged chunk)."""
    return HEADER_LEN + pt_size + TAG_LEN * max(1, -(-pt_size // chunk_size))


def _too_large(what: str, limit: int | None = None) -> Refused:
    # `share`'s own wording is unchanged: no limit given means MAX_UPLOAD, worded as "200 MiB"
    # (its only real-world value). `put` passes the upload link's own, usually different, max_bytes
    # explicitly, and gets its actual human-readable size instead (final review).
    if limit is None:
        return Refused(f"{what} is too large to share: the limit is 200 MiB ({MAX_UPLOAD} bytes) encrypted")
    return Refused(f"{what} is too large to share: the limit is {_human_size(limit)} ({limit} bytes) encrypted")


class _LimitWriter:
    """Counts what encrypt_stream writes and stops it as soon as it passes MAX_UPLOAD (an endless stdin
    must not fill the disk)."""

    def __init__(self, dst: BinaryIO, what: str):
        self.dst, self.what, self.n = dst, what, 0

    def write(self, b: bytes) -> int:
        self.n += len(b)
        if self.n > MAX_UPLOAD:
            raise _too_large(self.what)
        return self.dst.write(b)


def _checked_share_path(path_arg: str, root: Path, name: str | None, yes: bool, allow_secret: bool) -> tuple[Path, str]:
    """The FS guards for sharing a local file: (resolved path, name to share it under), or Refused."""
    p = Path(path_arg)
    if _in_skill_dir(p, root):   # checked first, and --allow-secret / --yes never override it
        raise Refused("files in .claude/skills/sharing/ hold this device's key and are never shared, "
                      "not even with --allow-secret")
    if p.is_dir():
        raise Refused(f"{path_arg} is a directory — pack it first, e.g. tar czf {p.name}.tar.gz {path_arg}")
    if not p.is_file():
        raise UsageError(f"no such file: {path_arg}")
    rp = p.resolve()
    if not rp.is_relative_to(root) and not yes:
        raise Refused(f"{path_arg} is outside this repo ({root}); pass --yes if the user asked for it")
    name = name or p.name
    if not allow_secret and (is_secret_path(p, root) or is_secret_path(rp, root)
                             or is_secret_path(Path(name), root)):
        raise Refused(_secret_msg(name))
    return rp, name


def _encrypt_upload(cfg: Config, src: BinaryIO, clean: str, mime: str, note: str, ttl: str,
                    tags: list[str], size: int | None) -> dict:
    """Encrypt src into a temp file and upload it as a FILE; the server's FileOut. `size` (when
    known) is checked against the limit before a single byte is encrypted."""
    fc = new_file_crypto(cfg.mk, clean, mime, note)
    if size is not None and ciphertext_size(size) > MAX_UPLOAD:
        raise _too_large(clean)
    fd, ct_name = tempfile.mkstemp(prefix="sharing-", suffix=".shr")
    os.close(fd)
    ct_path = Path(ct_name)
    try:
        with open(ct_path, "wb") as dst:
            # the counting writer also covers a file that grows while it is read
            encrypt_stream(fc["dek"], fc["uuid"], cfg.key_version, src, _LimitWriter(dst, clean))
        return Api(cfg.server_url, cfg.device_token).upload(
            {"uuid": fc["uuid_hex"], "key_version": cfg.key_version,
             "wrapped_dek": fc["wrapped_dek"], "enc_meta": fc["enc_meta"], "ttl": ttl, "tags": tags},
            ct_path)
    finally:
        ct_path.unlink(missing_ok=True)


def _share_name(name: str) -> tuple[str, str]:
    clean = safe_name(name, "")
    if not clean:
        raise UsageError(f"unusable file name: {_clean(name)!r}")
    return clean, _guess_mime(clean)


def _share_path(cfg: Config, rp: Path, name: str, *, ttl: str, tags: list[str], note: str) -> dict:
    """Upload a file that already passed `_checked_share_path` (the secret and repo guards); the server's
    FileOut plus "name" and "mime". `share` and `msg send --attach` both go through here."""
    clean, mime = _share_name(name)
    with open(rp, "rb") as src:
        out = _encrypt_upload(cfg, src, clean, mime, note, ttl, tags, os.fstat(src.fileno()).st_size)
    return out | {"name": clean, "mime": mime}


def cmd_share(args) -> int:
    tags = normalize_tags(args.tag)   # a bad tag fails before anything is read or sent
    cfg = load_config(Path.cwd())
    root = cfg.repo_root.resolve()
    note = args.message or ""
    if args.path == "-":
        if not args.name:
            raise UsageError("--name is required when sharing from stdin")
        name = args.name
        if not args.allow_secret and is_secret_path(Path(name), root):
            raise Refused(_secret_msg(name))
        clean, mime = _share_name(name)
        out = _encrypt_upload(cfg, sys.stdin.buffer, clean, mime, note, args.ttl, tags, None)
    else:
        rp, name = _checked_share_path(args.path, root, args.name, args.yes, args.allow_secret)
        out = _share_path(cfg, rp, name, ttl=args.ttl, tags=tags, note=note)
        clean, mime = out["name"], out["mime"]
    d = {"id": out["id"], "name": clean, "mime": mime, "size": plaintext_size(int(out["size"])), "note": note,
         "device": cfg.device_name, "project": out.get("project", cfg.project),
         "created_at": out.get("created_at"), "deleted_at": None, "acked_at": None, "acked_by": None,
         "expires_at": out.get("expires_at"), "tags": _file_tags(out)}
    _out(args, d, out["id"])
    return 0


def _download_decrypted(api: Api, blob_path: str, dek: bytes, file_uuid: bytes, target: Path) -> int:
    """Download the ciphertext, decrypt it and move it into place atomically; the plaintext size.

    decrypt_stream writes each chunk as soon as it authenticates, so a later bad chunk leaves partial
    plaintext behind: write to a temp file beside the target and rename it only after full success."""
    fd, part = tempfile.mkstemp(dir=target.parent, prefix=".sharing-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as dst, tempfile.TemporaryFile() as ct:
            api.download(blob_path, ct)
            ct.seek(0)
            size = decrypt_stream(dek, file_uuid, ct, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.chmod(part, 0o644)
        os.replace(part, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(part)
        raise
    return size


def _sha256_of(path: Path) -> bytes:
    """Streamed: a 200 MiB file must not be read into memory twice to be compared."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.digest()


def _identical_existing(api: Api, f: dict, dek: bytes, out_dir: Path, name: str, ref: str) -> Path | None:
    """A copy of this very file already in out_dir (as NAME or REF-NAME): `get` again reuses it instead of
    piling up FILE7-name copies until it is refused (QA TF-19). Compared by content: the file is decrypted
    into a scratch folder beside it and hashed; only a regular, non-symlink file of the right size is considered."""
    try:
        size = plaintext_size(int(f["size"]))
    except (KeyError, TypeError, ValueError):
        return None
    for cand in (out_dir / name, out_dir / f"{ref}-{name}"):
        try:
            if cand.is_symlink() or not cand.is_file() or cand.stat().st_size != size:
                continue
        except OSError:
            continue
        scratch = Path(tempfile.mkdtemp(dir=out_dir, prefix=".sharing-cmp-"))
        try:
            probe = scratch / "probe"
            _download_decrypted(api, f"/api/files/{ref}/blob", dek, bytes.fromhex(f["uuid"]), probe)
            same = _sha256_of(probe) == _sha256_of(cand)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if same:
            return cand
    return None


def cmd_get(args) -> int:
    cfg = load_config(Path.cwd())
    api = Api(cfg.server_url, cfg.device_token)
    _try_adopt_pending(api, cfg)
    tags = normalize_tags(args.tag)
    if args.ref.strip().lower() == "latest":
        f = next(_iter_files(api, include_deleted=False, include_acked=args.all, tags=tags), None)
        if f is None:
            what = f" tagged {_tags_text(tags)}" if tags else ""
            raise ApiError(404, "not_found", f"no files{what} on the share" if args.all else
                           f"no unacknowledged files{what} (pass --all to include acknowledged ones)")
    elif tags:
        raise UsageError("--tag only applies to `get latest`")
    else:
        f = _fetch_file(api, norm_ref(args.ref))
    ref = f["id"]
    meta, dek = open_file_meta(cfg.mk, f)
    if args.output:
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = cfg.repo_root / "share"
        if not _share_inside_repo(cfg.repo_root):   # e.g. a committed `share -> ~/.ssh` symlink
            raise Refused(f"share/ points outside the repo (to {out_dir.resolve()}); refusing to write there. "
                          "Pass -o DIR to choose the output directory explicitly")
        ensure_self_ignore(out_dir)
    name = safe_name(meta["name"], f"{ref}.bin")
    target = None if args.force else _identical_existing(api, f, dek, out_dir, name, ref)
    if target is None:
        target = choose_target(out_dir, ref, name, args.force)
        _download_decrypted(api, f"/api/files/{ref}/blob", dek, bytes.fromhex(f["uuid"]), target)
    d = describe_file(cfg.mk, f)
    d["path"] = str(target)
    d["acked"] = bool(f.get("acked_at"))
    if not args.no_ack:   # only now: the file is written and renamed into place
        try:
            api.send("POST", _file_path(ref, "/ack"))
            d["acked"] = True
            if not d.get("acked_at"):      # fresh metadata: acked_at must agree with acked (QA TF-19)
                with contextlib.suppress(ApiError):
                    d = {**describe_file(cfg.mk, _fetch_file(api, ref)), "path": str(target), "acked": True}
        except ApiError as e:   # the file was delivered: warn, never fail the get
            d["acked"] = False
            _warn(f"{ref} was saved, but could not be acknowledged ({e.detail or e.code}); "
                  f"run `sharing ack {ref}` later")
    _out(args, d, _display_path(target))
    return 0


def _set_ack(args, on: bool) -> int:
    cfg = load_config(Path.cwd())
    ref = norm_ref(args.ref)
    try:
        Api(cfg.server_url, cfg.device_token).send("POST" if on else "DELETE", _file_path(ref, "/ack"))
    except ApiError as e:
        raise _missing(e, ref) from None
    _out(args, {"id": ref, "acked": on}, f"{'acknowledged' if on else 'unacknowledged'} {ref}")
    return 0


def cmd_ack(args) -> int:
    return _set_ack(args, True)


def cmd_unack(args) -> int:
    return _set_ack(args, False)


def cmd_ttl(args) -> int:
    cfg = load_config(Path.cwd())
    ref = norm_ref(args.ref)
    api = Api(cfg.server_url, cfg.device_token)
    try:
        api.send("PATCH", _file_path(ref), {"ttl": args.ttl})
    except ApiError as e:
        if e.status == 403 and e.code == "forbidden":   # pending/revoked keep their own code (exit 3)
            raise ApiError(403, "forbidden", f"only the device that shared {ref} (or the web UI) can change "
                                             "its expiry") from None
        raise _missing(e, ref) from None
    expires_at = _fetch_file(api, ref).get("expires_at")
    _out(args, {"id": ref, "ttl": args.ttl, "expires_at": expires_at}, f"{ref}: {expiry_text(expires_at)}")
    return 0


def cmd_rm(args) -> int:
    cfg = load_config(Path.cwd())
    ref = norm_ref(args.ref)
    try:
        Api(cfg.server_url, cfg.device_token).delete(f"/api/files/{ref}")
    except ApiError as e:
        if e.status == 403 and e.code == "forbidden":   # pending/revoked keep their own code (exit 3)
            raise ApiError(403, "forbidden", f"only the device that shared {ref} (or the web UI) can delete it") from None
        if e.status == 410:
            raise ApiError(410, "deleted", f"{ref} was already deleted") from None
        if e.status == 404:
            raise ApiError(404, "not_found", f"{ref} does not exist") from None
        raise
    _out(args, {"id": ref, "deleted": True}, f"deleted {ref}")
    return 0


def cmd_uninstall(args) -> int:
    cfg = load_config(Path.cwd(), require_active=False)
    try:
        Api(cfg.server_url, cfg.device_token).delete("/api/devices/self")
    except ApiError as e:
        if e.status in (401, 403):
            pass
        elif not args.force:
            raise ApiError(e.status, e.code, f"could not revoke this device on the server ({e.detail or e.code}); "
                                             "re-run with --force to remove it locally anyway, then revoke it "
                                             "on the Devices page") from None
    _retire_old(cfg.config_path.parent, final=True)   # a --force leftover (config.json.old) is revoked too
    shutil.rmtree(cfg.config_path.parent, ignore_errors=True)
    tickets_dir = cfg.repo_root / TICKETS_SKILL_DIR
    if tickets_dir.is_symlink():   # never follow it out of the repo: drop only the link
        tickets_dir.unlink()
    else:
        shutil.rmtree(tickets_dir, ignore_errors=True)
    share = cfg.repo_root / "share"
    share_has_files = share.is_dir() and any(p.name != ".gitignore" for p in share.iterdir())
    if share.is_dir() and not share_has_files:
        shutil.rmtree(share, ignore_errors=True)
    remove_git_exclude(cfg.repo_root, [line for line in EXCLUDE_LINES
                                       if not (line == "/share/" and share_has_files)])
    note = " (share/ still has files, so it stays excluded from git)" if share_has_files else ""
    _out(args, {"device_id": cfg.device_id, "uninstalled": True, "share_kept": share_has_files},
         f"uninstalled {cfg.device_name} ({cfg.device_id}) from {cfg.repo_root}{note}")
    return 0


def update_skill(cfg: Config, check_only: bool = False) -> tuple[str, str, bool]:
    """(old, new, changed). Downloads every listed file, verifies every sha256, and only then swaps them in."""
    skill_dir = cfg.config_path.parent
    api = Api(cfg.server_url, None)
    man = api.get_json("/skill/manifest.json")
    new = str(man.get("version", "")) if isinstance(man, dict) else ""
    files = man.get("files") if isinstance(man, dict) else None
    if (not _SEMVER_RE.match(new) or not isinstance(files, dict) or "sharing.py" not in files
            or not all(isinstance(files[n], str) and _SHA_RE.match(files[n]) for n in files if n in UPDATABLE)):
        raise IntegrityError("the server's skill manifest is malformed — nothing was replaced")
    if check_only:
        return VERSION, new, False
    if new == VERSION:
        names = [n for n in RESTORABLE if n in files and not (skill_dir / n).is_file()]
        if not names:
            return VERSION, new, False
    else:
        names = [n for n in UPDATABLE if n in files]   # UPDATABLE order; never config.json, .old or .gitignore
    tmp = Path(tempfile.mkdtemp(dir=skill_dir, prefix=".update-"))
    try:
        for name in names:
            with open(tmp / name, "wb") as fh:
                api.download(f"/skill/{name}", fh)
            if hashlib.sha256((tmp / name).read_bytes()).hexdigest() != files[name]:
                raise IntegrityError(f"{name} does not match the manifest's sha256 — nothing was replaced")
        for name in names:
            if not IS_WINDOWS:
                os.chmod(tmp / name, 0o755 if name == "sharing" else 0o644)
            os.replace(tmp / name, skill_dir / name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    ensure_self_ignore(skill_dir)
    load_config(cfg.repo_root, require_active=False)
    return VERSION, new, True


def cmd_update(args) -> int:
    cfg = load_config(Path.cwd(), require_active=False)
    old, new, changed = update_skill(cfg, check_only=args.check)
    available = new != old
    if changed and new == old:
        human = f"restored missing skill files ({new}). Start a new Claude Code session to load them."
    elif changed:
        human = f"updated {old} → {new}. Start a new Claude Code session to load the new SKILL.md."
    elif available:
        human = f"update available: {old} → {new} (run `sharing update`)"
    else:
        human = f"already up to date ({old})"
    _out(args, {"old": old, "new": new, "changed": changed, "update_available": available and not changed}, human)
    return 0


@command
def _reg_share(sub, common):
    p = sub.add_parser("share", aliases=["put"], parents=[common],
                       help="encrypt and upload a file; prints its ID (`put` is the same command)")
    p.add_argument("path", help="file to share, or - for stdin (needs --name)")
    p.add_argument("-m", "--message", help="one-line note shown with the file")
    p.add_argument("--name", help="name to share it under")
    p.add_argument("--yes", action="store_true", help="allow a file outside this repo")
    p.add_argument("--allow-secret", action="store_true", help="allow a name that looks like a secret")
    p.add_argument("--ttl", choices=TTLS, default=DEFAULT_TTL,
                   help="delete it automatically after 1d, 7d (default) or 30d, or never")
    p.add_argument("--tag", action="append", default=[], metavar="TAG",
                   help="tag the file (repeatable, at most 10), e.g. --tag weekly-report")
    p.set_defaults(func=cmd_share)


@command
def _reg_get(sub, common):
    p = sub.add_parser("get", parents=[common], help="download and decrypt a file into share/, then acknowledge it",
                       description="Download and decrypt a file into share/, then acknowledge it (so `list` and "
                                   "`latest` skip it). `latest` is the newest unacknowledged file unless --all.")
    p.add_argument("ref", metavar="ID", help="FILE7, 7, or latest (the newest unacknowledged file)")
    p.add_argument("-o", "--output", metavar="DIR", help="directory to write into (default: <repo>/share)")
    p.add_argument("--force", action="store_true", help="overwrite share/<name> if it exists")
    p.add_argument("--no-ack", action="store_true", help="don't acknowledge the file after writing it")
    p.add_argument("--all", action="store_true", help="let `latest` pick acknowledged files too")
    p.add_argument("--tag", action="append", default=[], metavar="TAG",
                   help="let `latest` pick only files carrying this tag (repeatable: all must match)")
    p.set_defaults(func=cmd_get)


@command
def _reg_ack(sub, common):
    p = sub.add_parser("ack", parents=[common], help="acknowledge a file (hides it from list and latest)")
    p.add_argument("ref", metavar="ID")
    p.set_defaults(func=cmd_ack)


@command
def _reg_ttl(sub, common):
    p = sub.add_parser("ttl", parents=[common], help="change when a file this device shared expires (from now)")
    p.add_argument("ref", metavar="ID")
    p.add_argument("ttl", choices=TTLS, metavar="{1d,7d,30d,never}")
    p.set_defaults(func=cmd_ttl)


@command
def _reg_unack(sub, common):
    p = sub.add_parser("unack", parents=[common], help="clear a file's acknowledgement")
    p.add_argument("ref", metavar="ID")
    p.set_defaults(func=cmd_unack)


@command
def _reg_rm(sub, common):
    p = sub.add_parser("rm", parents=[common], help="delete a file this device shared")
    p.add_argument("ref", metavar="ID")
    p.set_defaults(func=cmd_rm)


@command
def _reg_uninstall(sub, common):
    p = sub.add_parser("uninstall", parents=[common], help="revoke this device and remove the skill from this repo")
    p.add_argument("--force", action="store_true", help="remove locally even if the server can't be reached")
    p.set_defaults(func=cmd_uninstall)


@command
def _reg_update(sub, common):
    p = sub.add_parser("update", parents=[common], help="install the newest version of this skill from the server")
    p.add_argument("--check", action="store_true", help="only report whether an update is available")
    p.set_defaults(func=cmd_update)


# ---------------------------------------------------------------- tags (spec §19)

def _retag(args, add: bool) -> int:
    """`tag` / `untag`: read the file's current tags, then PUT the union or the difference.

    Read-then-replace is not atomic: the server's PUT replaces the whole set, so if another device or
    the web UI changes this file's tags between our GET and our PUT, that change is overwritten
    (last writer wins). Acceptable for hand-applied labels; the server has no add/remove endpoint."""
    ref = norm_ref(args.ref)
    given = normalize_tags(args.tags)
    if not given:
        raise UsageError("name at least one tag")
    cfg = load_config(Path.cwd())
    api = Api(cfg.server_url, cfg.device_token)
    current = _file_tags(_fetch_file(api, ref))
    wanted = normalize_tags(set(current) | set(given)) if add else sorted(set(current) - set(given))
    try:
        out = api.send("PUT", _file_path(ref, "/tags"), {"tags": wanted})
    except ApiError as e:
        raise _missing(e, ref) from None
    tags = _file_tags(out)
    _out(args, {"id": ref, "tags": tags}, f"{ref}: {_tags_text(tags)}")
    return 0


def cmd_tag(args) -> int:
    return _retag(args, add=True)


def cmd_untag(args) -> int:
    return _retag(args, add=False)


def cmd_tags(args) -> int:
    cfg = load_config(Path.cwd())
    raw = Api(cfg.server_url, cfg.device_token).get_json("/api/tags").get("tags")
    rows = [{"tag": _strip_unsafe(r["tag"]), "count": r.get("count"), "last_used": r.get("last_used")}
            for r in (raw if isinstance(raw, list) else [])
            if isinstance(r, dict) and isinstance(r.get("tag"), str)]
    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print("no tags")
        return 0
    print(f"{'TAG':<40}  {'COUNT':>5}  LAST USED")
    for r in rows:
        print(f"{_clean(r['tag']):<40}  {_clean(str(r['count'])):>5}  {_clean(str(r['last_used'] or ''))}")
    return 0


@command
def _reg_tag(sub, common):
    p = sub.add_parser("tag", parents=[common], help="add tags to a file (keeps its other tags)")
    p.add_argument("ref", metavar="ID")
    p.add_argument("tags", nargs="*", metavar="TAG")
    p.set_defaults(func=cmd_tag)


@command
def _reg_untag(sub, common):
    p = sub.add_parser("untag", parents=[common], help="remove tags from a file")
    p.add_argument("ref", metavar="ID")
    p.add_argument("tags", nargs="*", metavar="TAG")
    p.set_defaults(func=cmd_untag)


@command
def _reg_tags(sub, common):
    p = sub.add_parser("tags", parents=[common], help="list the tags in use, with how many live files carry each")
    p.set_defaults(func=cmd_tags)


# ---------------------------------------------------------------- audio detection (for the transcript hint)

# As previewkind.js: these containers can also hold video, so an extension counts as audio unless the mime
# says video/*, and .webm (most often video) only when the mime is missing.
_AUDIO_BY_EXT = {"m4a": "audio/mp4", "mp3": "audio/mpeg", "ogg": "audio/ogg", "oga": "audio/ogg",
                 "opus": "audio/ogg", "wav": "audio/wav", "webm": "audio/webm", "aac": "audio/aac"}
_EXT_RE = re.compile(r"\.([A-Za-z0-9]+)$")


def audio_type(meta: dict) -> str | None:
    """The audio Content-Type of a decrypted meta, or None if it isn't audio (the rule of previewkind.js).

    Used only to decide whether `info` hints at a transcript: transcription itself now happens in the
    browser (spec §20), never from this CLI."""
    m = _EXT_RE.search(str(meta.get("name") or ""))
    ext = m.group(1).lower() if m else ""
    mime = str(meta.get("mime") or "").lower().split(";")[0].strip()
    if mime.startswith("audio/"):
        return mime
    if ext in _AUDIO_BY_EXT and not mime.startswith("video/") and (ext != "webm" or not mime):
        return _AUDIO_BY_EXT[ext]
    return None


def cmd_transcribe_removed(args) -> int:
    raise UsageError("transcribe was removed: transcripts are now made in the web app")


@command
def _reg_transcribe(sub, common):
    p = sub.add_parser("transcribe", parents=[common],
                       help="removed: transcripts are now made automatically in the web app",
                       description="Removed (spec §20): the browser now transcribes audio you upload there, "
                                   "and offers a Transcribe button in the file view. This CLI no longer talks "
                                   "to Deepgram.")
    p.add_argument("ref", nargs="?", metavar="ID")
    p.set_defaults(func=cmd_transcribe_removed)


# ---------------------------------------------------------------- public links (§17)

LINK_TTLS = ("1h", "1d", "7d", "30d")
DEFAULT_LINK_TTL = "7d"
MAX_LINK_DOWNLOADS = 1000
_LINK_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_LINK_ID_RE = re.compile(r"^lnk_[0-9a-f]{12}$")
_LINK_FORM = "expected a link like https://<server>/p/<token>#<key>"


def _max_downloads(v: str) -> int:
    n = _positive_int(v)
    if n > MAX_LINK_DOWNLOADS:
        raise argparse.ArgumentTypeError(f"must be at most {MAX_LINK_DOWNLOADS}")
    return n


def cmd_link(args) -> int:
    """Wrap the file's DEK under a fresh link key; the key goes only into the URL fragment."""
    cfg = load_config(Path.cwd())
    api = Api(cfg.server_url, cfg.device_token)
    ref = norm_ref(args.ref)
    f = _fetch_file(api, ref)
    _, dek = open_file_meta(cfg.mk, f)
    lk = new_link_key()
    body = {"wrapped_dek_link": wrap_dek_for_link(dek, bytes.fromhex(f["uuid"]), lk), "ttl": args.ttl}
    if args.max is not None:
        body["max_downloads"] = args.max
    try:
        made = api.post_json(_file_path(ref, "/links"), body)
    except ApiError as e:
        raise _missing(e, ref) from None
    token = str(made.get("token", ""))
    if not _LINK_TOKEN_RE.match(token):
        raise ApiError(0, "bad_response", "the server returned no usable link token")
    url = f"{api.base}/p/{token}#{b64u(lk)}"
    # Printed once, on stdout only: the server keeps just the token's hash and never sees the key.
    _out(args, {"id": made.get("id"), "url": url, "expires_at": made.get("expires_at"),
                "max_downloads": made.get("max_downloads")}, url)
    return 0


def cmd_links(args) -> int:
    cfg = load_config(Path.cwd())
    ref = norm_ref(args.ref)
    try:
        links = Api(cfg.server_url, cfg.device_token).get_json(_file_path(ref, "/links")).get("links", [])
    except ApiError as e:
        raise _missing(e, ref) from None
    rows = []
    for x in links:
        m = x.get("max_downloads")
        by = _clean((x.get("created_by") or {}).get("name") or "?")
        rows.append(f"{_clean(x.get('id'))}  {expiry_text(x.get('expires_at'))}  "
                    f"downloads {x.get('downloads', 0)}/{m if m is not None else '∞'}  by {by}")
    _out(args, links, "\n".join(rows) if rows else f"no live links for {ref}")
    return 0


def cmd_unlink(args) -> int:
    link_id = (args.link_id or "").strip()
    if not _LINK_ID_RE.match(link_id):
        raise UsageError(f"not a link ID: {_clean(link_id)!r} (expected lnk_ and 12 hex digits; see `sharing links ID`)")
    cfg = load_config(Path.cwd())
    try:
        Api(cfg.server_url, cfg.device_token).delete(f"/api/links/{link_id}")
    except ApiError as e:
        if e.status == 404:
            raise ApiError(404, "not_found", f"no link {link_id}") from None
        raise
    _out(args, {"id": link_id, "revoked": True}, f"revoked {link_id}")
    return 0


def parse_link_url(url: str) -> tuple[str, str, bytes]:
    """(server base URL, token, link key). Never echoes the URL: it carries the key."""
    try:
        u = urllib.parse.urlsplit(str(url).strip())
    except ValueError:
        raise UsageError(f"not a sharing link: {_LINK_FORM}") from None
    if not u.scheme:
        raise UsageError(f"not a sharing link: {_LINK_FORM}")
    if u.scheme not in ("http", "https") or not u.netloc:
        raise Refused("refusing that link: it must use https:// (plain http:// only for 127.0.0.1, "
                      "localhost or ::1)")
    try:
        has_userinfo = u.username is not None or u.password is not None
    except ValueError:
        has_userinfo = True
    if has_userinfo:
        raise Refused("refusing that link: it carries a user name or password")
    base = check_server_url(f"{u.scheme}://{u.netloc}")
    m = re.fullmatch(r"/p/([^/]*)", u.path)
    if not m or not _LINK_TOKEN_RE.match(m.group(1)):
        raise UsageError(f"not a sharing link: {_LINK_FORM}")
    try:
        lk = unb64u(u.fragment)
    except ValueError:
        lk = b""
    if len(lk) != LINK_KEY_LEN:
        raise UsageError(f"this link is incomplete or damaged: the key after '#' is missing or malformed ({_LINK_FORM})")
    return base, m.group(1), lk


def _link_transcript(meta: dict) -> dict | None:
    """The full transcript for the link holder, only in the documented shape (all strings)."""
    t = meta.get("transcript")
    keys = ("text", "language", "model", "created_at", "by")
    if not isinstance(t, dict) or not all(isinstance(t.get(k), str) for k in keys):
        return None
    return {k: _strip_unsafe(t[k]) for k in keys}


def _link_http_error(e: ApiError) -> Exception:
    """The link's server is whoever the URL names: never print its text, only a fixed message by status."""
    if e.status == 0:
        return e                      # local: a connection failure or an unparseable body
    if e.status == 404:
        return CliExit(2, "not_found", "this link has expired or was revoked")
    if e.status == 429:
        return CliExit(1, "rate_limited", "too many requests; try again in a minute")
    if 500 <= e.status < 600:
        return CliExit(4, "server_error", "the link's server failed")
    if 300 <= e.status < 400:
        return CliExit(1, "redirect", f"the link's server answered with a redirect (HTTP {e.status}); "
                                      "redirects are never followed")
    return CliExit(1, "refused", f"the link's server refused the request (HTTP {e.status})")


def cmd_open_link(args) -> int:
    """Fetch, decrypt and write a file shared by public link. No config, no onboarding."""
    base, token, lk = parse_link_url(args.url)
    try:
        return _open_link(args, Api(base, None), token, lk)
    except ApiError as e:
        raise _link_http_error(e) from None


def _open_link(args, api: Api, token: str, lk: bytes) -> int:
    pub = api.get_json(f"/api/public/{token}")
    meta, dek = open_link_file(lk, pub)            # a wrong key fails here, before a download is spent
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        root = find_repo_root(Path.cwd()) or Path.cwd()
        out_dir = root / "share"
        if not _share_inside_repo(root):
            raise Refused(f"share/ points outside {root} (to {out_dir.resolve()}); refusing to write there. "
                          "Pass --out DIR to choose the output directory explicitly")
        ensure_self_ignore(out_dir)
    target = choose_target(out_dir, "link", safe_name(meta["name"], "link-file.bin"), False)
    size = _download_decrypted(api, f"/api/public/{token}/blob", dek, bytes.fromhex(pub["uuid"]), target)
    d = {"name": _strip_unsafe(meta["name"]), "mime": _strip_unsafe(meta["mime"]), "size": size,
         "path": str(target), "note": _strip_unsafe(meta["note"])}
    t = _link_transcript(meta)
    if t is not None:
        d["transcript"] = t
    _out(args, d, _display_path(target))
    return 0


@command
def _reg_link(sub, common):
    p = sub.add_parser("link", parents=[common], help="create a public link to one file; prints the URL once",
                       description="Create a public link: anyone with the URL can read this one file (name, "
                                   "note and transcript included). The key is only in the URL's #fragment; "
                                   "the URL is printed once.")
    p.add_argument("ref", metavar="ID")
    p.add_argument("--ttl", choices=LINK_TTLS, default=DEFAULT_LINK_TTL,
                   help="how long the link works: 1h, 1d, 7d (default) or 30d, never past the file's expiry")
    p.add_argument("--max", type=_max_downloads, metavar="N", help=f"stop after N downloads (1-{MAX_LINK_DOWNLOADS})")
    p.set_defaults(func=cmd_link)


@command
def _reg_links(sub, common):
    p = sub.add_parser("links", parents=[common], help="list a file's live public links (never their URLs)")
    p.add_argument("ref", metavar="ID")
    p.set_defaults(func=cmd_links)


@command
def _reg_unlink(sub, common):
    p = sub.add_parser("unlink", parents=[common], help="revoke a public link")
    p.add_argument("link_id", metavar="LINK_ID", help="lnk_… as shown by `sharing links ID`")
    p.set_defaults(func=cmd_unlink)


@command
def _reg_open_link(sub, common):
    p = sub.add_parser("open-link", parents=[common],
                       help="download and decrypt a file from a public link (needs no onboarding)")
    p.add_argument("url", metavar="URL", help="https://<server>/p/<token>#<key>")
    p.add_argument("--out", metavar="DIR", help="directory to write into (default: <repo or cwd>/share)")
    p.set_defaults(func=cmd_open_link)


# =========================================================== upload links (spec §18)
#
# `sharing upload-link …`: the owner side of an inbound one-time link (create/list/revoke), and `put`,
# which anyone holding a link's URL runs to send files -- no onboarding, like `open-link`.

_DROP_FORM = "expected a link like https://<server>/u/<token>#<public key>"
_UPLOAD_LINK_ID_RE = re.compile(r"^upl_[0-9a-f]{12}$")
UPLOAD_LINK_TTLS = ("1h", "1d", "7d")
DEFAULT_UPLOAD_LINK_TTL = "1d"


def parse_drop_url(url: str) -> tuple[str, str, bytes]:
    """(server base URL, token, link public key). Never echoes the URL: it carries the key."""
    try:
        u = urllib.parse.urlsplit(str(url).strip())
    except ValueError:
        raise UsageError(f"not an upload link: {_DROP_FORM}") from None
    if not u.scheme:
        raise UsageError(f"not an upload link: {_DROP_FORM}")
    if u.scheme not in ("http", "https") or not u.netloc:
        raise Refused("refusing that link: it must use https:// (plain http:// only for 127.0.0.1, "
                      "localhost or ::1)")
    try:
        has_userinfo = u.username is not None or u.password is not None
    except ValueError:
        has_userinfo = True
    if has_userinfo:
        raise Refused("refusing that link: it carries a user name or password")
    base = check_server_url(f"{u.scheme}://{u.netloc}")
    m = re.fullmatch(r"/u/([^/]*)", u.path)
    if not m or not _LINK_TOKEN_RE.match(m.group(1)):
        raise UsageError(f"not an upload link: {_DROP_FORM}")
    try:
        pub = unb64u(u.fragment)
    except ValueError:
        pub = b""
    if len(pub) != P256_PUB_LEN or pub[0] != 4:
        raise UsageError(f"this link is incomplete or damaged: the key after '#' is missing or "
                         f"malformed ({_DROP_FORM})")
    try:
        _load_p256_pub(pub)
    except ValueError:
        raise UsageError(f"this link is incomplete or damaged: the key after '#' is not a valid "
                         f"public key ({_DROP_FORM})") from None
    return base, m.group(1), pub


# ---------------------------------------------------------------- owner: create / list / revoke

def cmd_upload_link_create(args) -> int:
    """Wrap a fresh P-256 keypair under MK; the private half never leaves this device."""
    cfg = load_config(Path.cwd())
    link = new_upload_link_crypto(cfg.mk, args.label or "")
    body = {"uuid": link["uuid_hex"], "key_version": cfg.key_version, "wrapped_lpriv": link["wrapped_lpriv"],
            "enc_label": link["enc_label"], "ttl": args.ttl}
    api = Api(cfg.server_url, cfg.device_token)
    made = api.post_json("/api/upload-links", body)
    token = str(made.get("token", ""))
    if not _LINK_TOKEN_RE.match(token):
        raise ApiError(0, "bad_response", "the server returned no usable upload-link token")
    url = f"{api.base}/u/{token}#{b64u(link['pub'])}"
    # Printed once, on stdout only: the server keeps just the token's hash and never sees the key.
    _out(args, {"id": made.get("id"), "url": url, "expires_at": made.get("expires_at")}, url)
    return 0


def _adopt_pending(api: Api, cfg: Config) -> int:
    """Adopt every upload link this owner holds that has a pending (uploaded but unclaimed) file.

    A 409 means someone else (another device, or a previous run) already adopted it -- that counts as
    done, not a failure. Any other failure only warns, once per link, and never raises: called at the
    top of `list`/`get`/`info`, it must never break them (the caller still wraps the initial GET, since
    the server itself may be briefly unreachable)."""
    rows = api.get_json("/api/upload-links").get("links", [])
    n = 0
    for row in rows:
        pending = row.get("pending")
        if not pending:
            continue
        link_id = row.get("id", "?")
        try:
            link_uuid = bytes.fromhex(row["uuid"])
            file_uuid = bytes.fromhex(pending["file_uuid"])
            priv, pub = open_upload_link_key(cfg.mk, link_uuid, row["wrapped_lpriv"])
            dek = open_sealed_dek(priv, pub, link_uuid, file_uuid, pending["sealed_dek"])
            wrapped_dek = wrap_dek_under_mk(cfg.mk, dek, file_uuid)
        except (KeyError, ValueError, TypeError, IntegrityError) as e:
            _warn(f"could not adopt the upload waiting on {link_id} ({e})")
            continue
        try:
            api.post_json(f"/api/upload-links/{link_id}/adopt", {"wrapped_dek": wrapped_dek})
            n += 1
        except ApiError as e:
            if e.status == 409:
                n += 1   # already adopted (by another device, or a race we lost): counts as done
                continue
            _warn(f"could not adopt the upload waiting on {link_id} ({e.detail or e.code})")
    return n


def _try_adopt_pending(api: Api, cfg: Config) -> None:
    try:
        _adopt_pending(api, cfg)
    except ApiError as e:
        _warn(f"could not check for pending uploads ({e.detail or e.code})")


def _upload_link_row(cfg: Config, row: dict) -> dict:
    """The row with its label decrypted, and the raw key material stripped out."""
    try:
        label = open_upload_label(cfg.mk, bytes.fromhex(row["uuid"]), row.get("enc_label"))
    except (IntegrityError, ValueError, TypeError):
        label = None
    d = {k: v for k, v in row.items() if k != "wrapped_lpriv"}
    pending = d.get("pending")
    if pending:
        d["pending"] = {k: v for k, v in pending.items() if k not in ("sealed_dek", "enc_meta")}
    d["label"] = label
    return d


def cmd_upload_link_list(args) -> int:
    cfg = load_config(Path.cwd())
    api = Api(cfg.server_url, cfg.device_token)
    _try_adopt_pending(api, cfg)
    rows = [_upload_link_row(cfg, r) for r in api.get_json("/api/upload-links").get("links", [])]
    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print("no upload links")
        return 0
    lines = []
    for d in rows:
        label = _clean(d["label"]) if d["label"] else ""
        line = f"{d['id']}  {d['state']}  {expiry_text(d['expires_at'])}  {label}"
        if d.get("file"):
            line += f"  {d['file']}"
        lines.append(line)
    print("\n".join(lines))
    return 0


def cmd_upload_link_revoke(args) -> int:
    link_id = (args.link_id or "").strip()
    if not _UPLOAD_LINK_ID_RE.match(link_id):
        raise UsageError(f"not an upload-link ID: {_clean(link_id)!r} (expected upl_ and 12 hex digits; "
                         "see `sharing upload-link list`)")
    cfg = load_config(Path.cwd())
    api = Api(cfg.server_url, cfg.device_token)
    # Like list/get/info: adopt a pending upload first, so revoking a link that already carries an
    # upload keeps it as a file instead of silently discarding it (final review).
    _try_adopt_pending(api, cfg)
    rows = api.get_json("/api/upload-links").get("links", [])
    row = next((r for r in rows if r.get("id") == link_id), None)
    if row is not None and row.get("state") == "received":
        file_id = row.get("file")
        _out(args, {"id": link_id, "revoked": False, "file": file_id},
             f"{link_id} was already received as {file_id} — nothing to revoke")
        return 0
    try:
        api.delete(f"/api/upload-links/{link_id}")
    except ApiError as e:
        if e.status == 404:
            raise ApiError(404, "not_found", f"no upload link {link_id}") from None
        raise
    _out(args, {"id": link_id, "revoked": True}, f"revoked {link_id}")
    return 0


# ---------------------------------------------------------------- put (needs no config)

def _secret_hits(rel: str) -> bool:
    return any(fnmatch.fnmatchcase(part.lower(), pat) for part in Path(rel).parts for pat in SECRET_PATTERNS)


def _collect_upload_paths(paths: list[str]) -> list[tuple[Path, str]]:
    """[(real file, arcname)] for every regular file under the given paths, sorted, symlinks skipped
    (with a warning). Arcnames are relative to each given path's own parent, so a directory argument
    keeps its own name as the top folder (Review Focus 5: nested dirs and non-ASCII names survive)."""
    out: list[tuple[Path, str]] = []
    for path_arg in paths:
        p = Path(path_arg)
        if p.is_symlink():
            _warn(f"{path_arg}: a symlink, skipping it")
            continue
        if not p.exists():
            raise UsageError(f"no such file or directory: {path_arg}")
        if p.is_file():
            out.append((p, p.name))
            continue
        if not p.is_dir():
            raise UsageError(f"no such file or directory: {path_arg}")
        base = p.parent
        for root, dirs, files in os.walk(p, followlinks=False):
            root_path = Path(root)
            kept = []
            for d in sorted(dirs):
                if (root_path / d).is_symlink():
                    _warn(f"{(root_path / d).relative_to(base)}: a symlink, skipping it")
                else:
                    kept.append(d)
            dirs[:] = kept
            for name in sorted(files):
                fp = root_path / name
                if fp.is_symlink():
                    _warn(f"{fp.relative_to(base)}: a symlink, skipping it")
                    continue
                out.append((fp, fp.relative_to(base).as_posix()))
    _check_no_duplicate_arcnames(out)
    return out


def _check_no_duplicate_arcnames(files: list[tuple[Path, str]]) -> None:
    """`put a/x.txt b/x.txt` would otherwise write two zip entries both named "x.txt" (each bare-file
    argument's arcname is just its basename, spec Task 6): refuse before anything is zipped or
    encrypted, naming the collision, rather than silently keeping only one of them."""
    seen: set[str] = set()
    for _, rel in files:
        if rel in seen:
            raise UsageError(f"two files would both be named {rel} in the zip — pass their parent "
                             "folders instead")
        seen.add(rel)


def _make_upload_zip(files: list[tuple[Path, str]]) -> Path:
    fd, tmp = tempfile.mkstemp(prefix="sharing-upload-", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp, rel in files:
                zf.write(fp, rel)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return Path(tmp)


def cmd_upload_link_put(args) -> int:
    """Fetch a link's public key, encrypt one file (or a zip of several) under a fresh DEK, and seal
    that DEK to the link. No config, no onboarding -- like `open-link`."""
    base, token, pub = parse_drop_url(args.url)   # Review Focus 4: nothing is read or sent for a bad URL
    files = _collect_upload_paths(args.paths)
    if not files:
        raise UsageError("nothing to send")
    if not args.allow_secret:
        hits = sorted({rel for _, rel in files if _secret_hits(rel)})
        if hits:
            raise Refused("refusing to send what looks like a secret: " + ", ".join(hits) + ". Pass "
                          "--allow-secret only if the user explicitly asked for these exact files.")
    single = (len(args.paths) == 1 and len(files) == 1 and Path(args.paths[0]).is_file()
             and not Path(args.paths[0]).is_symlink())
    note = args.note or ""
    zip_path: Path | None = None
    try:
        if single:
            src_path, name = files[0]
            mime = _guess_mime(name)
        else:
            zip_path = _make_upload_zip(files)
            src_path = zip_path
            name = datetime.now(timezone.utc).strftime("upload-%Y%m%d-%H%M.zip")
            mime = "application/zip"
        api = Api(base, None)
        try:
            pub_meta = api.get_json(f"/api/public/u/{token}")
        except ApiError as e:
            raise _link_http_error(e) from None
        try:
            link_uuid = bytes.fromhex(pub_meta["uuid"])
            key_version = int(pub_meta["key_version"])
            max_bytes = int(pub_meta["max_bytes"])
        except (KeyError, ValueError, TypeError):
            raise ApiError(0, "bad_response", "the link's server sent a malformed response") from None
        pt_size = src_path.stat().st_size
        if ciphertext_size(pt_size) > max_bytes:
            raise _too_large(name, max_bytes)
        file_uuid = os.urandom(16)
        dek = os.urandom(32)
        fd, ct_name = tempfile.mkstemp(prefix="sharing-upload-", suffix=".shr")
        os.close(fd)
        ct_path = Path(ct_name)
        try:
            with open(src_path, "rb") as src, open(ct_path, "wb") as dst:
                encrypt_stream(dek, file_uuid, key_version, src, dst)
            enc_meta = seal_file_meta(dek, file_uuid, {"name": name, "mime": mime, "note": note})
            sealed_dek = seal_dek_to_link(dek, pub, link_uuid, file_uuid)
            meta = {"uuid": file_uuid.hex(), "key_version": key_version, "sealed_dek": sealed_dek,
                    "enc_meta": enc_meta}
            try:
                out = api.upload_to(f"/api/public/u/{token}", meta, ct_path)
            except ApiError as e:
                raise _link_http_error(e) from None
        finally:
            ct_path.unlink(missing_ok=True)
    finally:
        if zip_path is not None:
            zip_path.unlink(missing_ok=True)
    d = {"name": name, "size": pt_size, "sent": bool(out.get("ok"))}
    _out(args, d, f"sent {_clean(name)} ({_human_size(pt_size)})")
    return 0


@command
def _reg_upload_link(sub, common):
    p = sub.add_parser("upload-link", parents=[common],
                       help="an inbound one-time link: create/list/revoke, or `put` to send files")
    usub = p.add_subparsers(dest="upload_link_cmd", required=True, metavar="COMMAND")

    c = usub.add_parser("create", parents=[common],
                        help="create an upload link; prints the URL once (send it to whoever is sending files)")
    c.add_argument("--ttl", choices=UPLOAD_LINK_TTLS, default=DEFAULT_UPLOAD_LINK_TTL,
                   help="how long the link works: 1h, 1d (default) or 7d")
    c.add_argument("--label", help="a private note shown in `upload-link list` (never visible to the sender)")
    c.set_defaults(func=cmd_upload_link_create)

    ls = usub.add_parser("list", parents=[common], help="list this owner's upload links")
    ls.set_defaults(func=cmd_upload_link_list)

    rv = usub.add_parser("revoke", parents=[common], help="revoke an upload link")
    rv.add_argument("link_id", metavar="ID", help="upl_… as shown by `upload-link list`")
    rv.set_defaults(func=cmd_upload_link_revoke)

    put = usub.add_parser("put", parents=[common],
                          help="send files through someone's upload link (needs no onboarding)")
    put.add_argument("url", metavar="URL", help="https://<server>/u/<token>#<key>")
    put.add_argument("paths", nargs="+", metavar="PATH", help="files or directories to send")
    put.add_argument("--note", help="one-line note shown with the file")
    put.add_argument("--allow-secret", action="store_true", help="allow a name that looks like a secret")
    put.set_defaults(func=cmd_upload_link_put)


# =========================================================== spaces, mirrors and the inbox (A4)
#
# Used by the orch-tix addon of orch-core (through `ctx.run`, always with --json). space.json beside
# config.json names this workspace's space. Every command is idempotent: a repeat push is "duplicate",
# an older one "stale", a second unlink "absent".

SPACE_FILE = "space.json"
JOIN_TIMEOUT_S = 900
JOIN_POLL_S = 3.0
INBOX_MAX_WAIT_S = 30           # the server's largest ?wait=
MAX_LINK_GEN_BUMPS = 1000       # a key unlinked this often is a bug, not a workflow
DECISION_ACKS = ("applied", "ignored", "stale", "superseded", "answered-locally", "unlinked",   # = server ACKS
                 # non-final: why the desktop holds it (= server WAITING); a final ack replaces it later
                 "waiting-unpaired", "waiting-signature", "waiting-switched-off", "waiting-time", "waiting-check")
_SPACE_ID_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)
_DEC_ID_RE = re.compile(r"dec_[0-9a-f]{32}", re.ASCII)


def _space_path(cfg: Config) -> Path:
    return cfg.config_path.parent / SPACE_FILE


def _load_space(cfg: Config) -> dict:
    p = _space_path(cfg)
    if not p.is_file():
        raise ConfigError("no space for this workspace yet — run `sharing space create --label NAME`", code="no_space")
    data = _read_json(p, "space.json")
    if not isinstance(data.get("space_id"), str) or not _SPACE_ID_RE.fullmatch(data["space_id"]):
        raise ConfigError("space.json is damaged; remove it and run `sharing space join`", code="no_space")
    return data


def _space_arg(v: str) -> str:
    v = str(v).strip().lower()
    if not _SPACE_ID_RE.fullmatch(v):
        raise UsageError("a space id is 32 lowercase hex characters (see `sharing space show` on its owner)")
    return v


AGENT_ENV = ("CLAUDECODE", "ORCH_HARNESS", "ORCH_HOME", "CLAUDE_CODE_SESSION_ID", "ORCH_SESSION", "ORCH_MODEL")


def _agent_env() -> list[str]:
    """The agent markers that make a human-only command (`space join`, `tickets migrate --apply`) refuse:
    the same names orch-core treats as an agent harness."""
    return list(AGENT_ENV)


def _in_agent() -> bool:
    return any(os.environ.get(v) for v in _agent_env())


def _interactive() -> bool:
    """A human at a terminal (tests replace this)."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _find_space(api: Api, space_id: str) -> dict | None:
    return next((s for s in api.get_json("/api/spaces").get("spaces", []) if s.get("id") == space_id), None)


def cmd_space_create(args) -> int:
    cfg = load_config(Path.cwd())
    label = _clean(args.label).strip()[:80]
    if not label:
        raise UsageError("--label must not be empty")
    path = _space_path(cfg)
    # The id is chosen here and written before the POST (batch 3 review m3): a retry after a lost answer
    # finds it marked pending and sends the same id, so the server never gets a second space.
    space_id = None
    if path.exists():
        prev = _read_json(path, "space.json")
        if prev.get("pending") is not True or not _SPACE_ID_RE.fullmatch(str(prev.get("space_id", ""))):
            raise Refused("this workspace already has a space (`sharing space show`)")
        space_id = prev["space_id"]
    space_id = space_id or os.urandom(16).hex()
    write_config(path, {"space_id": space_id, "label": label, "pending": True})
    api = Api(cfg.server_url, cfg.device_token)
    try:
        api.post_json("/api/spaces", {"id": space_id, "key_version": cfg.key_version or 1,
                                      "enc_label": seal_space_label(cfg.mk, space_id, label)})
    except ApiError as e:
        # 409: the first try reached the server. It counts only if this device owns that space.
        row = _find_space(api, space_id) if e.status == 409 else None
        if row is None or row.get("owner_device") != cfg.device_id:
            raise
    write_config(path, {"space_id": space_id, "label": label})
    _out(args, {"space_id": space_id, "label": label}, f"space {space_id[:8]}… created: {label}")
    return 0


def cmd_space_show(args) -> int:
    cfg = load_config(Path.cwd())
    local = _load_space(cfg)
    row = _find_space(Api(cfg.server_url, cfg.device_token), local["space_id"])
    if row is None:
        raise ApiError(404, "no_space", "the server has no such space any more; remove space.json and create one")
    label = local.get("label") if isinstance(local.get("label"), str) else ""
    owner = row.get("owner_device") == cfg.device_id
    d = {"space_id": local["space_id"], "label": _strip_unsafe(label), "owner": owner,
         "last_seen_at": row.get("last_seen_at"), "server": cfg.server_url,
         "notify_messages": row.get("notify_messages") is True}
    who = "this device owns it" if owner else f"owned by {_clean(row.get('owner_name') or '?')}"
    _out(args, d, f"space {local['space_id']} ({_clean(label)}): {who}")
    return 0


def _wait_for_join(api: Api, space_id: str, req_id: str) -> str:
    """Poll the join request until the browser decides it: "approved" | "denied" | "expired" (7 days)."""
    path = f"/api/spaces/{space_id}/join-requests/{urllib.parse.quote(req_id, safe='')}"
    resume = ("the request is still pending — approve it in TIX in your browser, then run "
              "`sharing space join` again (it continues the same request)")
    deadline = time.monotonic() + JOIN_TIMEOUT_S
    try:
        while True:
            try:
                status = api.get_json(path).get("status")
            except ApiError as e:
                if not _transient(e):
                    raise
                status = None
            if status in ("approved", "denied", "expired"):
                return status
            if time.monotonic() >= deadline:
                raise ConfigError(resume, code="pending")
            _sleep(JOIN_POLL_S)
    except KeyboardInterrupt:
        raise ConfigError(resume, code="pending") from None


def cmd_space_join(args) -> int:
    if _in_agent() or not _interactive():
        raise Refused("taking over a space is for a human at a terminal")
    cfg = load_config(Path.cwd())
    space_id = _space_arg(args.space_id)
    if _space_path(cfg).exists():
        current = _load_space(cfg)["space_id"]
        if current != space_id:
            raise Refused(f"this workspace already syncs space {current}; remove space.json first if you mean it")
    print("Type the space id to take it over: ", end="", file=sys.stderr, flush=True)
    typed = sys.stdin.readline().strip().lower()
    if typed != space_id:
        raise Refused("the typed space id does not match; nothing changed")
    api = Api(cfg.server_url, cfg.device_token)
    r = api.post_json(f"/api/spaces/{space_id}/join", {})
    status = r.get("status")
    if status == "pending":
        req = (r.get("request") or {}).get("id")
        if not isinstance(req, str):
            raise ApiError(0, "bad_response", "the server did not return a join request")
        print("Waiting for approval in TIX (open it in your browser) …", file=sys.stderr, flush=True)
        status = _wait_for_join(api, space_id, req)
    elif status != "owner":
        raise ApiError(0, "bad_response", "unexpected answer to the join request")
    if status == "denied":
        raise Refused("your browser denied taking over this space; nothing changed")
    if status == "expired":
        raise Refused("the join request expired before it was approved; run `sharing space join` again")
    if args.label is not None:
        label = _clean(args.label).strip()[:80]
    else:
        row = _find_space(api, space_id) or {}
        label = open_space_label(cfg.mk, space_id, row["enc_label"]) if row.get("enc_label") else ""
    write_config(_space_path(cfg), {"space_id": space_id, "label": label})
    status = "approved" if status in ("approved", "owner") else status
    _out(args, {"space_id": space_id, "label": _strip_unsafe(label), "status": status},
         f"this device now syncs space {space_id[:8]}… ({_clean(label)})")
    return 0


def _mirror_body(raw: str) -> dict:
    try:
        body = json.loads(raw)
    except ValueError:
        raise UsageError("mirror file is not JSON") from None
    if not isinstance(body, dict):
        raise UsageError("mirror file must hold a JSON object")
    for k, t in (("key", str), ("gen", int), ("rev", int), ("status", str), ("priority", str),
                 ("open_questions", int), ("schema_version", str), ("doc", dict)):
        if not isinstance(body.get(k), t) or isinstance(body.get(k), bool):
            raise UsageError(f"mirror file: {k} must be a {t.__name__}")
    if body["gen"] < 1 or body["rev"] < 1 or not body["key"]:
        raise UsageError("mirror file: key must be non-empty, gen and rev at least 1")
    if body.get("needs") is not None and not isinstance(body["needs"], str):
        raise UsageError("mirror file: needs must be null or a string")
    if body.get("notify") is not None and not isinstance(body["notify"], bool):
        raise UsageError("mirror file: notify must be true or false")
    if body.get("notify_seen") is not None and (type(body["notify_seen"]) is not int or body["notify_seen"] < 0):
        raise UsageError("mirror file: notify_seen must be a whole number")
    if body.get("decided_via") not in (None, "phone"):
        raise UsageError("mirror file: decided_via must be absent or \"phone\"")
    return body


def _existing_mirror(api: Api, space: str, uuid: str) -> dict | None:
    """The stored mirror (200), None (404), or ApiError 410 gone for a retired uuid."""
    try:
        return api.get_json(f"/api/mirrors/u/{uuid}?space={space}")
    except ApiError as e:
        if e.status == 404:
            return None
        raise


def cmd_mirror_push(args) -> int:
    cfg = load_config(Path.cwd())
    space = _load_space(cfg)["space_id"]
    body = _mirror_body(_read_input_file(args.file, cfg.repo_root.resolve()))
    api = Api(cfg.server_url, cfg.device_token)
    key, gen = body["key"], body["gen"]

    def gone() -> int:
        # Unlinked meanwhile (a late or retried push): report it and recreate nothing. Linking again is the
        # addon's decision and uses --relink, which moves to the next generation (batch 3 review I1).
        _out(args, {"status": "gone", "gen": gen}, f"{_clean(key)}: unlinked; push with --relink to link it again")
        return 0

    for _ in range(MAX_LINK_GEN_BUMPS):
        uuid = mirror_uuid(space, key, gen)
        tu = bytes.fromhex(uuid)
        try:
            existing = _existing_mirror(api, space, uuid)
        except ApiError as e:
            if e.status == 410:          # unlinked: only --relink uses the next generation (new uuid, TIX id)
                if not args.relink:
                    return gone()
                gen += 1
                continue
            raise
        rev = body["rev"]
        server_rev = int((existing or {}).get("mirror_rev") or 0)
        if existing is not None and server_rev >= rev and existing.get("last_writer_device") != cfg.device_id:
            # The server says another device stored this rev (the previous owner, after a takeover): push above
            # it, so the phone never sees a rollback (final review I1). An older rev of this device's own stays
            # stale, whatever is on this disk.
            rev = server_rev + 1
        req = {"space": space, "mirror_rev": rev, "schema_version": body["schema_version"],
               "status": body["status"], "priority": body["priority"], "needs": body.get("needs"),
               "open_questions": body["open_questions"], "event_uuid": mirror_event_uuid(space, key, gen, rev)}
        if body.get("notify") is not None:       # cleartext: may this ticket notify the phone (the server default is off)
            req["notify"], req["notify_seen"] = body["notify"], body.get("notify_seen") or 0
        if body.get("decided_via"):      # Mission Control applied a phone answer: the "handled" push says so (QA #55)
            req["decided_via"] = body["decided_via"]
        if existing is not None:         # reuse the stored DEK and key version (else 409 dek_mismatch)
            dek = open_(cfg.mk, unb64u(existing["wrapped_dek"]), aad_tdek(tu))
            req["key_version"] = existing["key_version"]
        else:
            dek = os.urandom(32)
            req["key_version"] = cfg.key_version or 1
            req["wrapped_dek"] = b64u(seal(cfg.mk, dek, aad_tdek(tu)))
        req["enc_content"] = seal_mirror(dek, tu, mirror_doc(body["doc"], rev, gen))
        try:
            r = api.send("PUT", f"/api/mirrors/{uuid}", req)
            status, tix = "pushed", r.get("id")
        except ApiError as e:
            if e.status == 410:          # unlinked between our read and the write
                if not args.relink:
                    return gone()
                gen += 1
                continue
            if e.code not in ("stale_rev", "duplicate_uuid"):
                raise
            status, tix = ("stale" if e.code == "stale_rev" else "duplicate"), (existing or {}).get("id")
        _out(args, {"status": status, "id": tix, "uuid": uuid, "server_rev": (existing or {}).get("mirror_rev"),
                    "gen": gen, "rev": rev, **({"notify": r["notify"], "notify_rev": r["notify_rev"]}
                                               if status == "pushed" and "notify" in r else {})},
             f"{_clean(key)}: {status}" + (f" as {tix}" if tix else ""))
        return 0
    raise Refused(f"{_clean(key)} was unlinked more than {MAX_LINK_GEN_BUMPS} times; refusing to go on")


def cmd_mirror_notify_state(args) -> int:
    """Per mirror of this space: its TIX id, whether the phone may be notified, and how many times the phone changed
    that (no sealed data). The orch-tix addon merges the phone's changes from this."""
    cfg = load_config(Path.cwd())
    space = _load_space(cfg)["space_id"]
    r = Api(cfg.server_url, cfg.device_token).get_json(f"/api/spaces/{space}/notify")
    _out(args, {"messages": r.get("messages") is True, "mirrors": r.get("mirrors", [])},
         "\n".join(f"{m.get('id')}: {'on' if m.get('notify') else 'off'}" for m in r.get("mirrors", [])) or "no mirrors")
    return 0


def cmd_space_notify(args) -> int:
    """Phone notifications for agent messages that name no ticket (this space's owner only; default off)."""
    cfg = load_config(Path.cwd())
    space = _load_space(cfg)["space_id"]
    on = args.messages == "on"
    r = Api(cfg.server_url, cfg.device_token).send("PUT", f"/api/spaces/{space}/notify", {"messages": on})
    _out(args, {"messages": r.get("notify_messages") is True}, f"messages without a ticket: {args.messages}")
    return 0


def cmd_mirror_unlink(args) -> int:
    cfg = load_config(Path.cwd())
    space = _load_space(cfg)["space_id"]
    if args.gen < 1:
        raise UsageError("--gen must be at least 1")
    uuid = mirror_uuid(space, args.key, args.gen)
    try:
        Api(cfg.server_url, cfg.device_token).delete(f"/api/mirrors/{uuid}?space={space}")
        status = "unlinked"
    except ApiError as e:
        if e.status not in (404, 410):   # never linked, or already unlinked: the goal is reached
            raise
        status = "absent"
    _out(args, {"status": status}, f"{_clean(args.key)}: {status}")
    return 0


def _gen_of(space: str, key: str, uuid: str) -> int | None:
    return next((g for g in range(1, MAX_LINK_GEN_BUMPS + 1) if mirror_uuid(space, key, g) == uuid), None)


def cmd_mirror_status(args) -> int:
    cfg = load_config(Path.cwd())
    space = _load_space(cfg)["space_id"]
    rows = Api(cfg.server_url, cfg.device_token).get_json(f"/api/mirrors?space={space}").get("mirrors", [])
    out = []
    for m in rows:
        key = gen = None
        try:
            tu = bytes.fromhex(m["uuid"])
            doc = open_mirror(open_(cfg.mk, unb64u(m["wrapped_dek"]), aad_tdek(tu)), tu, m["enc_content"])
            if isinstance(doc.get("id"), str):
                key, gen = _strip_unsafe(doc["id"]), _gen_of(space, doc["id"], m["uuid"])
        except (IntegrityError, KeyError, TypeError, ValueError):
            pass                          # a row that does not open is listed with key null
        out.append({"id": m.get("id"), "key": key, "gen": gen, "rev": m.get("mirror_rev"), "needs": m.get("needs"),
                    "updated_at": m.get("updated_at")})
    lines = [f"{o['id']}  {_clean(o['key'] or '?')}  rev {o['rev']}" + (f"  needs {o['needs']}" if o["needs"] else "")
             for o in out]
    _out(args, {"mirrors": out}, "\n".join(lines) or "no mirrors")
    return 0


def _inbox(args, after: int, wait: int) -> int:
    cfg = load_config(Path.cwd())
    space = _load_space(cfg)["space_id"]
    wait = min(max(int(wait), 0), INBOX_MAX_WAIT_S)
    api = Api(cfg.server_url, cfg.device_token)
    r = api.get_json(f"/api/inbox/changes?space={space}&after={int(after)}&wait={wait}")
    out = []
    tix_of = None                               # mirror uuid -> TIX id, read once when an item needs it
    for item in r.get("decisions", []):
        gen = tix = None
        try:
            d = open_inbox_item(cfg.mk, item)
            gen = _bound_gen(_checked_decision(d, item), item, space)
            decision, err = _strip_deep(d), None
            if gen is not None:
                if tix_of is None:
                    rows = api.get_json(f"/api/mirrors?space={space}").get("mirrors", [])
                    tix_of = {m.get("uuid"): m.get("id") for m in rows if isinstance(m, dict)}
                tix = tix_of.get(item.get("ticket_uuid"))
        except (IntegrityError, KeyError, TypeError, ValueError):
            decision, err = None, "integrity"
        # kind at the top only from a body that opened and agrees with the routing (batch 3 review I2). The
        # TIX id is never the item's cleartext `ticket`: it is the id of the mirror at the bound ticket_uuid,
        # and null for an integrity item or a ticket request (Task 10 review I3).
        out.append({"id": item.get("id"), "seq": item.get("seq"), "ticket": tix,
                    "kind": decision["kind"] if decision else item.get("kind"), "created_at": item.get("created_at"),
                    "gen": gen, "decision": decision, "error": err})
    _out(args, {"decisions": out, "cursor": r.get("cursor", after)}, f"{len(out)} decision(s)")
    return 0


def _checked_decision(d: dict, item: dict) -> dict:
    """The sealed decision decides: its kind and id must match the cleartext routing the server shows, and
    only a ticket_request has no ticket. Otherwise the item is an integrity error and is never applied."""
    if d.get("kind") != item.get("kind") or d.get("decision_id") != item.get("id"):
        raise IntegrityError("the sealed decision does not match its routing")
    has_ticket = isinstance(d.get("ticket"), str) and d["ticket"] != ""
    if (d["kind"] == "ticket_request") == has_ticket or (item.get("ticket") is None) != (d["kind"] == "ticket_request"):
        raise IntegrityError("the sealed decision names another ticket than its routing")
    return d


def _bound_gen(d: dict, item: dict, space: str) -> int | None:
    """The sealed space must be this workspace's space (and the routing's), and a ticket decision's routing
    must be the mirror of the sealed ticket: item.ticket_uuid == mirror_uuid(space, sealed ticket, gen). So a
    server (or a session) that routes a decision to another live mirror cannot have it applied to the ticket
    it names inside, nor reported as the routed one (Task 9 review). Returns that link generation."""
    if d.get("space") != space or item.get("space") != space:
        raise IntegrityError("the sealed decision is for another space")
    if d["kind"] == "ticket_request":
        return None
    gen = _gen_of(space, d["ticket"], str(item.get("ticket_uuid") or ""))
    if gen is None:
        raise IntegrityError("the sealed decision names another ticket than its routing")
    return gen


def cmd_inbox_list(args) -> int:
    return _inbox(args, 0, 0)


def cmd_inbox_wait(args) -> int:
    if args.after < 0:
        raise UsageError("--after must be 0 or more")
    return _inbox(args, args.after, args.timeout)


def cmd_inbox_ack(args) -> int:
    if not _DEC_ID_RE.fullmatch(args.decision_id):
        raise UsageError("a decision id is dec_ and 32 lowercase hex characters")
    cfg = load_config(Path.cwd())
    Api(cfg.server_url, cfg.device_token).post_json(f"/api/decisions/{args.decision_id}/ack", {"ack": args.outcome})
    _out(args, {"id": args.decision_id, "ack": args.outcome}, f"{args.decision_id}: {args.outcome}")
    return 0


def cmd_devices(args) -> int:
    """The device list is browser-only (controller ruling): a device token gets 403. Say so plainly."""
    cfg = load_config(Path.cwd())
    try:
        r = Api(cfg.server_url, cfg.device_token).get_json("/api/devices")
    except ApiError as e:
        if e.status == 403:
            raise CliExit(6, "browser_only", "the device list is only shown in your browser (TIX → Devices); "
                                             "a device cannot read it. This device is fine.") from None
        raise
    out = [{"id": d.get("id"), "name": _strip_unsafe(d.get("name")), "project": _strip_unsafe(d.get("project")),
            "platform": d.get("platform"), "state": d.get("status"), "last_seen_at": d.get("last_seen_at")}
           for d in r.get("devices", [])]
    _out(args, {"devices": out}, "\n".join(f"{_clean(d['name'])}  {d['state']}" for d in out) or "no devices")
    return 0


def _int_arg(v: str) -> int:
    try:
        return int(v)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None


BRIDGE_WS_INFO = b"sharing/bridge/ws/v1|"   # docs/bridge-protocol.md §2.2


def bridge_key(mk: bytes, workspace: str) -> bytes:
    """K_ws = HKDF-SHA-256(IKM = MK, salt = empty, info = "sharing/bridge/ws/v1|" || workspace_hex, L = 32)."""
    return _hkdf(mk, BRIDGE_WS_INFO + workspace.encode("ascii"), 32, b"")


def _stdout_is_tty() -> bool:
    """Tests replace this."""
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def cmd_bridge_key(args) -> int:
    """Hand the bridge workspace key K_ws (never the master key) to the host. A guarded command: the orch
    command guard refuses agents, and that guard is the only barrier (docs/bridge-protocol.md §2.7)."""
    workspace = _space_arg(args.workspace)
    if _stdout_is_tty() and not args.allow_terminal:
        raise Refused("the key is a secret and stdout is a terminal, so it would stay in scrollback; "
                      "pipe it to the host, or add --allow-terminal if you really want it printed here")
    cfg = load_config(Path.cwd())   # pending (not approved) -> exit 3 `pending`
    mk = cfg.mk
    _remember_secret(b64u(mk))      # the master key is redacted from any message, in both spellings
    _remember_secret(mk.hex())
    row = _find_space(Api(cfg.server_url, cfg.device_token), workspace)   # a revoked device gets 401 here
    if row is None:
        raise ApiError(404, "no_space", "the server has no such space")
    if row.get("owner_device") != cfg.device_id:
        raise ApiError(403, "not_owner", "this device does not own that space, so it does not hand out its key "
                                         "(the host is the owner device)")
    key = bridge_key(mk, workspace).hex()
    # Printed raw on purpose: _out would run it through the redaction. It is not a registered secret.
    print(json.dumps({"key": key}) if args.json else key)
    return 0


@command
def _reg_space(sub, common):
    p = sub.add_parser("space", parents=[common], help="this workspace's space (used by the orch-tix addon)")
    ssub = p.add_subparsers(dest="space_cmd", required=True, metavar="COMMAND")
    c = ssub.add_parser("create", parents=[common], help="create a space for this workspace")
    c.add_argument("--label", required=True, help="a name for it, shown only in your browser (sealed)")
    c.set_defaults(func=cmd_space_create)
    s = ssub.add_parser("show", parents=[common], help="show this workspace's space and who owns it")
    s.set_defaults(func=cmd_space_show)
    j = ssub.add_parser("join", parents=[common],
                        help="ask to take a space over on this machine (a human, approved in the browser)")
    j.add_argument("space_id", metavar="SPACE_ID")
    j.add_argument("--label", help="the local name (default: the space's own)")
    j.set_defaults(func=cmd_space_join)
    n = ssub.add_parser("notify", parents=[common], help="phone notifications for messages that name no ticket (owner; default off)")
    n.add_argument("--messages", required=True, choices=("on", "off"))
    n.set_defaults(func=cmd_space_notify)


@command
def _reg_bridge_key(sub, common):
    p = sub.add_parser("bridge-key", parents=[common],
                       help="(for the orch host) print the bridge workspace key K_ws of a space this device owns")
    p.add_argument("--workspace", required=True, metavar="SPACE_ID", help="the space id (32 hex characters)")
    p.add_argument("--allow-terminal", action="store_true",
                   help="print even though stdout is a terminal (the key then stays in scrollback)")
    p.set_defaults(func=cmd_bridge_key)


# ---------------------------------------------------------------- bridge-host (R15b)

HOST_LEASE_S = 40            # the server's lease; only the ready event echoes it, every poll answer carries the real one
HOST_MAX_LINE = 1 << 19      # one command line, bytes (a respond body is at most ~350 KB of base64url)
HOST_MAX_INFLIGHT = 8        # commands being worked on at once; more are refused rate_limited
HOST_MAX_BODY_B64 = (256 * 1024 * 4 + 2) // 3   # the server's response-chunk cap (bridge.MAX_CHUNK_BYTES), as base64url text
HOST_MAX_WAIT_S = 25
HOST_CALL_TIMEOUT_S = 15     # per request, besides a poll (which gets its wait on top)
HOST_EOF_JOIN_S = 30         # on stdin EOF: how long to let a pending poll end before releasing the lease
_HOLDER_RE = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)
_RID_RE = re.compile(r"[0-9a-f]{32}", re.ASCII)
_B64U_RE = re.compile(r"[A-Za-z0-9_-]*", re.ASCII)
_HOST_BEAT_KEYS = ("sessions", "in_progress", "needs_you", "factory", "children_done", "children_total", "budget_pct")
_HOST_CODES = {   # stable code -> a fixed short text; the server's own text is never forwarded
    "host_taken": "another host serves this workspace; start with --take-over to replace it",
    "lease_lost": "this host no longer holds the workspace; poll again",
    "not_owner": "this device does not own the workspace",
    "unauthorized": "the server does not accept this device (revoked?)",
    "pending": "this device is still pending approval",
    "rate_limited": "the server asks for fewer calls; slow down",
    "too_large": "the body is over the server's size limit",
    "bad_request": "the server refused the request as malformed",
    "no_space": "the server has no such workspace",
    "network": "cannot reach the server (retryable)",
    "server": "the server failed (retryable)",
    "protocol": "malformed command",
}


class HostError(Exception):
    def __init__(self, code: str, message: str | None = None):
        super().__init__(code)
        self.code = code
        self.message = message or _HOST_CODES[code]


def _stdin_is_tty() -> bool:
    """Tests replace this."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def host_error_code(e: ApiError) -> str:
    """Map an HTTP failure to one of the stable bridge-host codes."""
    c = e.code
    if e.status == 0:
        return "network"
    if c in ("host_taken", "lease_lost", "not_owner", "pending", "rate_limited", "too_large", "bad_request", "no_space"):
        return c
    if c in ("revoked", "unauthenticated") or e.status == 401:
        return "unauthorized"
    if c == "forbidden":
        return "not_owner"
    if c == "mailbox_full" or e.status == 429:
        return "rate_limited"
    if e.status == 413:
        return "too_large"
    if e.status >= 500 or 300 <= e.status < 400:
        return "server"
    return "bad_request"


def _host_int(v, lo: int, hi: int, name: str) -> int:
    if type(v) is not int or not lo <= v <= hi:
        raise HostError("bad_request", f"{name} must be a whole number from {lo} to {hi}")
    return v


class BridgeHost:
    """The pipe proxy: JSON-lines commands in, answers out, mailbox calls made with the device token this
    object holds. Bodies are opaque base64url text and are never decoded or logged."""

    def __init__(self, cfg: Config, workspace: str, holder: str, take_over: bool, stdin, stdout):
        self.cfg, self.ws, self.holder = cfg, workspace, holder
        self._take_over = take_over            # cleared once a poll got an answer from the server
        self._lock = threading.Lock()          # guards _take_over, _inflight, _threads
        self._wlock = threading.Lock()         # serialises stdout
        self._inflight: set[str] = set()
        self._threads: set[threading.Thread] = set()
        self._stdin, self._out = stdin, stdout
        self.gone = threading.Event()          # stdout is closed: nobody listens any more

    # --- output ------------------------------------------------------------------------------------

    def emit(self, obj: dict) -> None:
        line = json.dumps(obj) + "\n"          # ASCII only: one line, no raw control or bidi characters
        with self._wlock:
            try:
                self._out.write(line)
                self._out.flush()
            except (OSError, ValueError):
                self.gone.set()

    def _fail(self, cid, code: str, message: str | None = None) -> None:
        self.emit({"id": cid, "ok": False, "code": code, "message": message or _HOST_CODES[code]})

    # --- mailbox calls -----------------------------------------------------------------------------

    def _api(self, timeout: float) -> Api:
        api = Api(self.cfg.server_url, self.cfg.device_token)
        api.TIMEOUT = timeout
        return api

    def _call(self, fn, timeout: float = HOST_CALL_TIMEOUT_S):
        try:
            return fn(self._api(timeout))
        except ApiError as e:
            raise HostError(host_error_code(e)) from None

    def op_poll(self, cmd: dict) -> dict:
        wait = _host_int(cmd.get("wait", HOST_MAX_WAIT_S), 0, HOST_MAX_WAIT_S, "wait")
        with self._lock:
            take = self._take_over
        q = f"holder={self.holder}&wait={wait}" + ("&take_over=1" if take else "")
        r = self._call(lambda a: a.get_json(f"/api/bridge/{self.ws}/requests?{q}"), wait + HOST_CALL_TIMEOUT_S)
        rows, lease = r.get("requests"), r.get("lease_s")
        if not isinstance(rows, list) or not all(isinstance(x, dict) and isinstance(x.get("id"), str)
                                                 and isinstance(x.get("body"), str) for x in rows):
            raise HostError("server", "the server's answer was not the expected shape")
        if take:
            with self._lock:
                self._take_over = False
        return {"ok": True, "requests": [{"rid": x["id"], "body": x["body"]} for x in rows],
                "lease_s": lease if type(lease) is int else HOST_LEASE_S}

    def op_respond(self, cmd: dict) -> dict:
        rid, body = cmd.get("rid"), cmd.get("body")
        if not isinstance(rid, str) or not _RID_RE.fullmatch(rid):
            raise HostError("bad_request", "rid must be 32 lowercase hex characters")
        idx, last = _host_int(cmd.get("idx"), 0, 2**31 - 1, "idx"), cmd.get("last")
        if type(last) is not bool:
            raise HostError("bad_request", "last must be true or false")
        if not isinstance(body, str) or not body or not _B64U_RE.fullmatch(body):
            raise HostError("bad_request", "body must be base64url text")
        if len(body) > HOST_MAX_BODY_B64:
            raise HostError("too_large")
        self._call(lambda a: a.post_json(f"/api/bridge/{self.ws}/responses/{rid}?holder={self.holder}",
                                         {"idx": idx, "last": last, "body": body}))
        return {"ok": True}

    def op_heartbeat(self, cmd: dict) -> dict:
        extra = set(cmd) - {"id", "op", *_HOST_BEAT_KEYS}
        if extra:
            raise HostError("bad_request", "unknown field in heartbeat")
        # Values are forwarded unvalidated on purpose: the server's strict schema checks them, and the line is
        # capped at HOST_MAX_LINE bytes. Only the key set is checked here.
        beat = {k: cmd[k] for k in _HOST_BEAT_KEYS if cmd.get(k) is not None}
        self._call(lambda a: a.post_json(f"/api/presence/{self.ws}/heartbeat", beat))
        return {"ok": True}

    def op_goodbye(self, cmd: dict) -> dict:
        self._call(lambda a: a.post_json(f"/api/presence/{self.ws}/goodbye", {}))
        return {"ok": True}

    def op_release(self, cmd: dict) -> dict:
        self._call(lambda a: a.delete(f"/api/bridge/{self.ws}/host?holder={self.holder}"))
        return {"ok": True}

    OPS = {"poll": "op_poll", "respond": "op_respond", "heartbeat": "op_heartbeat",
           "goodbye": "op_goodbye", "release": "op_release"}

    # --- the loop ----------------------------------------------------------------------------------

    def _run(self, cid: str, cmd: dict) -> None:
        try:
            try:
                out = getattr(self, self.OPS[cmd["op"]])(cmd)
            except HostError as e:
                self._fail(cid, e.code, e.message)
            except Exception as e:   # never crash, never forward text that could carry anything: the type only
                self._fail(cid, "server", f"internal error ({type(e).__name__})")
            else:
                self.emit({"id": cid, **out})
        finally:
            with self._lock:
                self._inflight.discard(cid)
                self._threads.discard(threading.current_thread())

    def _dispatch(self, raw: bytes) -> None:
        try:
            cmd = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            return self._fail(None, "protocol")
        if not isinstance(cmd, dict):
            return self._fail(None, "protocol")
        cid = cmd.get("id")
        if not isinstance(cid, str) or not 1 <= len(cid) <= 128:
            return self._fail(None, "protocol", "a command needs a string id of 1 to 128 characters")
        op = cmd.get("op")
        if not (isinstance(op, str) and op in self.OPS):   # a list or dict op is unhashable: type first
            return self._fail(cid, "protocol", "unknown op")
        with self._lock:
            if cid in self._inflight:
                return self._fail(cid, "bad_request", "this id is already in flight")
            if len(self._inflight) >= HOST_MAX_INFLIGHT:
                return self._fail(cid, "rate_limited", "too many commands in flight")
            self._inflight.add(cid)
            t = threading.Thread(target=self._run, args=(cid, cmd), daemon=True)
            self._threads.add(t)
        t.start()

    def _lines(self):
        """Yield command lines of at most HOST_MAX_LINE bytes; an over-long one is skipped and yields None."""
        while not self.gone.is_set():   # stdout closed: nobody listens, stop reading
            line = self._stdin.readline(HOST_MAX_LINE + 1)
            if not line:
                return
            if len(line) > HOST_MAX_LINE and not line.endswith(b"\n"):
                while line and not line.endswith(b"\n"):   # drop the rest of it without keeping it
                    line = self._stdin.readline(HOST_MAX_LINE)
                yield None
                continue
            if line.strip():
                yield line

    def serve(self) -> None:
        self.emit({"event": "ready", "holder": self.holder, "lease_s": HOST_LEASE_S})
        for line in self._lines():
            if self.gone.is_set():
                break
            if line is None:
                self._fail(None, "protocol", "line too long")
            else:
                try:
                    self._dispatch(line)
                except Exception:   # nothing a client sends may end the loop
                    self._fail(None, "protocol")
        # EOF (or stdout gone): let pending calls end, then release best effort. A poll re-takes the lease on
        # every server slice while it waits, so a release sent before it ends can be overtaken; a poll that
        # outlives this wait is abandoned and may re-lease for one slice (the lease then lapses by itself).
        with self._lock:
            pending = list(self._threads)
        deadline = time.monotonic() + HOST_EOF_JOIN_S
        for t in pending:
            t.join(max(0.0, deadline - time.monotonic()))
        try:
            self._api(5).delete(f"/api/bridge/{self.ws}/host?holder={self.holder}")
        except Exception:
            pass


def cmd_bridge_host(args) -> int:
    """A long-running JSON-lines proxy for the orch host: it holds the device token so the dashboard process
    never does. A guarded command like bridge-key: the orch command guard refuses agents, and that guard is
    the only barrier (docs/bridge-protocol.md §2.7)."""
    workspace = _space_arg(args.workspace)
    holder = args.holder if args.holder is not None else secrets.token_urlsafe(12)
    if not _HOLDER_RE.fullmatch(holder):
        raise UsageError("--holder is 1 to 64 characters of A-Z a-z 0-9 _ -")
    if _stdin_is_tty() or _stdout_is_tty():   # before any config, key or network access
        raise Refused("bridge-host speaks a JSON-lines pipe: stdin and stdout must both be pipes, not a terminal")
    cfg = load_config(Path.cwd())             # pending -> exit 3
    _remember_secret(b64u(cfg.mk))            # redacted from anything this command prints, in both spellings
    _remember_secret(cfg.mk.hex())
    _remember_secret(cfg.device_token)
    row = _find_space(Api(cfg.server_url, cfg.device_token), workspace)   # a revoked device gets 401 here
    if row is None:
        raise ApiError(404, "no_space", "the server has no such space")
    if row.get("owner_device") != cfg.device_id:
        raise ApiError(403, "not_owner", "this device does not own that space")
    BridgeHost(cfg, workspace, holder, args.take_over, getattr(sys.stdin, "buffer", sys.stdin), sys.stdout).serve()
    return 0


@command
def _reg_bridge_host(sub, common):
    # No `common`: --json would put an error object on the protocol's stdout.
    p = sub.add_parser("bridge-host", help="(for the orch host) relay mailbox operations over a JSON-lines pipe; "
                                           "holds the device token so the dashboard does not")
    p.add_argument("--workspace", required=True, metavar="SPACE_ID", help="the space id (32 hex characters)")
    p.add_argument("--take-over", action="store_true", help="replace another live host on the first poll")
    p.add_argument("--holder", help="this host process's id (default: random per process)")
    p.set_defaults(func=cmd_bridge_host)


@command
def _reg_mirror(sub, common):
    p = sub.add_parser("mirror", parents=[common], help="mirrored orch tickets (used by the orch-tix addon)")
    msub = p.add_subparsers(dest="mirror_cmd", required=True, metavar="COMMAND")
    pu = msub.add_parser("push", parents=[common], help="seal and upload one ticket snapshot")
    pu.add_argument("--file", required=True, metavar="PATH",
                    help="JSON {key, gen, rev, status, priority, needs, open_questions, schema_version, doc, "
                         "notify?, notify_seen?} (a human's choice, written by the orch-tix addon; agents do not set notify)")
    pu.add_argument("--relink", action="store_true",
                    help="if the ticket was unlinked, link it again under the next generation (a new TIX id)")
    pu.set_defaults(func=cmd_mirror_push)
    un = msub.add_parser("unlink", parents=[common], help="stop mirroring a ticket (the TIX id is retired)")
    un.add_argument("--key", required=True)
    un.add_argument("--gen", required=True, type=_int_arg)
    un.set_defaults(func=cmd_mirror_unlink)
    st = msub.add_parser("status", parents=[common], help="list this space's mirrors")
    st.set_defaults(func=cmd_mirror_status)
    ns = msub.add_parser("notify-state", parents=[common], help="per mirror: may it notify the phone (used by the orch-tix addon)")
    ns.set_defaults(func=cmd_mirror_notify_state)


@command
def _reg_inbox(sub, common):
    p = sub.add_parser("inbox", parents=[common], help="decisions from the phone (used by the orch-tix addon)")
    isub = p.add_subparsers(dest="inbox_cmd", required=True, metavar="COMMAND")
    ls = isub.add_parser("list", parents=[common], help="decisions not acked yet")
    ls.set_defaults(func=cmd_inbox_list)
    w = isub.add_parser("wait", parents=[common], help="wait for decisions after a cursor")
    w.add_argument("--after", type=_int_arg, default=0)
    w.add_argument("--timeout", type=_int_arg, default=INBOX_MAX_WAIT_S, metavar="S",
                   help=f"seconds, at most {INBOX_MAX_WAIT_S}")
    w.set_defaults(func=cmd_inbox_wait)
    a = isub.add_parser("ack", parents=[common], help="record what happened to a decision")
    a.add_argument("decision_id", metavar="DEC_ID")
    a.add_argument("outcome", choices=DECISION_ACKS, metavar="OUTCOME", help=", ".join(DECISION_ACKS))
    a.set_defaults(func=cmd_inbox_ack)


@command
def _reg_devices(sub, common):
    p = sub.add_parser("devices", parents=[common], help="list devices (only your browser can; explains why)")
    p.set_defaults(func=cmd_devices)



# ---- agent messages (spec §8): `sharing msg …`
#
# Routing (to whom, kind, size, FILE ids, ticket) is cleartext for the server; the body
# {"text", "files", "ticket", "from", "kind"} is sealed under MK with aad_msg(uuid). Attachments are ordinary
# FILEs uploaded through the `share` guards (never a secret, never outside the repo), ttl 7d, tag message.

MSG_KINDS = ("text", "question")
MSG_MAX_FILES = 10              # the server's limit
MSG_MAX_TEXT = 16000
_MSG_TO_RE = re.compile(r"(device|project|space):([A-Za-z0-9._-]{1,64})|human", re.ASCII)
_MSG_ID_RE = re.compile(r"msg_[0-9a-f]{32}", re.ASCII)


def _msg_to(v: str) -> tuple[str, str]:
    m = _MSG_TO_RE.fullmatch(v or "")
    if not m:
        raise UsageError(f"--to must be device:<id or name>, project:<name>, space:<id> or human, not {_clean(v)!r}")
    if v == "human":
        return "human", ""
    kind, ident = m.group(1), m.group(2)
    if kind == "space" and not _SPACE_ID_RE.fullmatch(ident):
        raise UsageError("a space id is 32 lowercase hex characters")
    return kind, ident


def cmd_msg_send(args) -> int:
    to_kind, to_id = _msg_to(args.to)
    text = args.message or ""
    if not text.strip():
        raise UsageError("-m must not be empty")
    if len(text) > MSG_MAX_TEXT:
        raise UsageError(f"-m is at most {MSG_MAX_TEXT} characters; attach a file for more")
    if len(args.attach) > MSG_MAX_FILES:
        raise UsageError(f"at most {MSG_MAX_FILES} attachments per message")
    cfg = load_config(Path.cwd())
    root = cfg.repo_root.resolve()
    ticket = space = None
    if args.ticket is not None:
        ticket = norm_ticket_ref(args.ticket)
        space = _load_space(cfg)["space_id"]          # a ticket tag names a mirror of this workspace's space
    paths = [_checked_share_path(a, root, None, False, False) for a in args.attach]   # every guard first
    files, size = [], 0
    for rp, name in paths:
        out = _share_path(cfg, rp, name, ttl=DEFAULT_TTL, tags=["message"], note="attached to a message")
        files.append(out["id"])
        size += plaintext_size(int(out["size"]))
    u = os.urandom(16)
    body = {"text": text, "files": files, "ticket": ticket, "from": cfg.device_name, "kind": args.kind}
    Api(cfg.server_url, cfg.device_token).post_json("/api/messages", {
        "uuid": u.hex(), "to_kind": to_kind, "to_id": to_id, "kind": args.kind, "key_version": cfg.key_version or 1,
        "enc_body": seal_msg(cfg.mk, u, body), "files": files, "space": space, "ticket": ticket, "size": size})
    mid = "msg_" + u.hex()
    _out(args, {"id": mid, "files": files}, f"{mid} sent" + (f" with {', '.join(files)}" if files else ""))
    return 0


def _checked_msg(body: dict, m: dict) -> dict:
    """The sealed body decides (batch 3 review I2): files and ticket must equal the cleartext routing, the
    kind too when the body names one, and a device's sealed `from` its server-attested name."""
    files = [f for f in m.get("files", []) if isinstance(f, str)]
    if body.get("files", []) != files or body.get("ticket") != m.get("ticket"):
        raise IntegrityError("the sealed message does not match its routing")
    if "kind" in body and body["kind"] != m.get("kind"):
        raise IntegrityError("the sealed message has another kind")
    if m.get("from_kind") == "device" and "from" in body and body["from"] != m.get("from_name"):
        raise IntegrityError("the sealed message names another sender")
    return body


def _msg_out(cfg: Config, m: dict) -> dict:
    sender = "human" if m.get("from_kind") == "human" else _clean(m.get("from_name"))
    to = "human" if m.get("to_kind") == "human" else f"{m.get('to_kind')}:{_clean(m.get('to_id'))}"
    out = {"id": m.get("id"), "seq": m.get("seq"), "from": sender, "to": to, "kind": m.get("kind"), "text": None,
           "files": [], "ticket": m.get("ticket"), "created_at": m.get("created_at"), "error": "integrity"}
    try:
        body = _checked_msg(open_msg(cfg.mk, bytes.fromhex(m["uuid"]), m["enc_body"]), m)
    except (IntegrityError, KeyError, TypeError, ValueError):
        return out                 # nothing sealed reaches the output, the server's files included
    # Text keeps its newlines and tabs; other control characters are replaced (batch 3 review m6).
    out.update(text=_text(body["text"]) if isinstance(body.get("text"), str) else "",
               files=[_clean(f) for f in body.get("files", []) if isinstance(f, str)],
               ticket=body.get("ticket"), kind=body.get("kind", m.get("kind")), error=None)
    if m.get("from_kind") == "device" and isinstance(body.get("from"), str):
        out["from"] = _clean(body["from"])
    return out


def _msg_poll(args, after: int, wait: int, follow: bool = False) -> int:
    cfg = load_config(Path.cwd())
    api = Api(cfg.server_url, cfg.device_token)
    wait = min(max(int(wait), 0), INBOX_MAX_WAIT_S)
    out, cursor = [], after
    while True:
        r = api.get_json(f"/api/messages?after={int(cursor)}&wait={wait}")
        page = r.get("messages", [])
        out += [_msg_out(cfg, m) for m in page]
        cursor = r.get("cursor", cursor)
        if not (follow and page):
            break
    lines = [f"{o['id']}  from {o['from']}: {o['text'] if o['text'] is not None else '(does not open)'}" for o in out]
    _out(args, {"messages": out, "cursor": cursor}, "\n".join(lines) or "no messages")
    return 0


def cmd_msg_list(args) -> int:
    return _msg_poll(args, 0, 0, follow=args.all)


def cmd_msg_wait(args) -> int:
    if args.after < 0:
        raise UsageError("--after must be 0 or more")
    return _msg_poll(args, args.after, args.timeout)


def cmd_msg_ack(args) -> int:
    if not _MSG_ID_RE.fullmatch(args.message_id):
        raise UsageError("a message id is msg_ and 32 lowercase hex characters")
    cfg = load_config(Path.cwd())
    Api(cfg.server_url, cfg.device_token).send("POST", f"/api/messages/{args.message_id}/ack")
    _out(args, {"id": args.message_id, "acked": True}, f"{args.message_id}: acked")
    return 0


@command
def _reg_msg(sub, common):
    p = sub.add_parser("msg", parents=[common], help="messages between environments and to the human")
    msub = p.add_subparsers(dest="msg_cmd", required=True, metavar="COMMAND")
    s = msub.add_parser("send", parents=[common], help="send a sealed message")
    s.add_argument("--to", required=True, metavar="TO", help="device:<id or name>, project:<name>, space:<id> or human")
    s.add_argument("-m", "--message", required=True, help="the text")
    s.add_argument("--attach", action="append", default=[], metavar="PATH",
                   help=f"attach a repo file as a FILE (repeatable, at most {MSG_MAX_FILES}; the share guards apply)")
    s.add_argument("--ticket", metavar="TIX-n", help="about this mirrored ticket of this workspace's space")
    s.add_argument("--kind", choices=MSG_KINDS, default="text")
    s.set_defaults(func=cmd_msg_send)
    ls = msub.add_parser("list", parents=[common], help="messages to this device not acked yet")
    ls.add_argument("--all", action="store_true", help="every page, not only the first 100")
    ls.set_defaults(func=cmd_msg_list)
    w = msub.add_parser("wait", parents=[common], help="wait for messages after a cursor")
    w.add_argument("--after", type=_int_arg, default=0)
    w.add_argument("--timeout", type=_int_arg, default=INBOX_MAX_WAIT_S, metavar="S",
                   help=f"seconds, at most {INBOX_MAX_WAIT_S}")
    w.set_defaults(func=cmd_msg_wait)
    a = msub.add_parser("ack", parents=[common], help="mark a message as read on this device")
    a.add_argument("message_id", metavar="ID")
    a.set_defaults(func=cmd_msg_ack)



# =========================================================== legacy tickets (spec T8; read-only since 2.0.0)
#
# `sharing tickets list|show|migrate`: the old TIX board. Tickets now live in orch-core workspaces; these
# commands read legacy tickets and move them over (`migrate`). Content and event bodies are sealed under the
# ticket's DEK (the ticket crypto above). claims.json beside config.json is read only to mark held claims.

TICKET_STATUSES = ("backlog", "open", "in-progress", "waiting", "testing", "done")
TICKET_TYPES = ("feature", "bug", "chore", "spike")
TICKET_PRIORITIES = ("low", "normal", "high", "urgent")
QUESTION_TYPES = ("single", "multi", "text", "confirm")
CLAIMS_FILE = "claims.json"
MAX_TITLE = 200
_TICKET_REF_RE = re.compile(r"^(?:tix-?)?([1-9][0-9]{0,11})$", re.IGNORECASE)


def norm_ticket_ref(ref) -> str:
    """TIX-42, tix-42, TIX42, tix42 and 42 all mean TIX-42 (spec D5)."""
    if isinstance(ref, int) and not isinstance(ref, bool):
        ref = str(ref)
    m = _TICKET_REF_RE.match(ref.strip()) if isinstance(ref, str) else None
    if not m:
        raise UsageError(f"not a ticket ID: {_clean(str(ref))!r} (expected e.g. TIX-42 or 42)")
    return f"TIX-{int(m.group(1))}"


def _ticket_path(ref: str, suffix: str = "") -> str:
    return f"/api/tickets/{urllib.parse.quote(ref, safe='')}{suffix}"


def _strip_deep(v):
    """_strip_unsafe on every string of a decrypted (untrusted) JSON value."""
    if isinstance(v, str):
        return _strip_unsafe(v)
    if isinstance(v, list):
        return [_strip_deep(x) for x in v]
    if isinstance(v, dict):
        return {_strip_unsafe(k) if isinstance(k, str) else k: _strip_deep(x) for k, x in v.items()}
    return v


def _text(s) -> str:
    """Multi-line untrusted text for the terminal: control characters replaced, \\t and \\n kept."""
    return _TEXT_CTRL_RE.sub("?", s) if isinstance(s, str) else ""


# ---- claims.json: {"TIX-42": {"token": "clm_…", "cursor": 57}}

class _Claims:
    """Read-only since 2.0.0: the claims this repo held on legacy tickets (claims.json), only to mark them as
    `held` in `show`. Still refused if git tracks it (it holds claim tokens)."""

    def __init__(self, cfg: Config):
        self.root = cfg.repo_root
        self.rel = REPO_CONFIG.parent / CLAIMS_FILE
        self.path = self.root / self.rel
        self.data: dict[str, dict] = {}
        if self.path.exists():
            _guard_secret(self.root, self.rel)
            for ref, c in _read_json(self.path, "claims file").items():
                if isinstance(c, dict) and isinstance(c.get("token"), str) and type(c.get("cursor")) is int:
                    self.data[ref] = {"token": c["token"], "cursor": c["cursor"]}
                    _remember_secret(c["token"])

    def get(self, ref: str) -> dict | None:
        return self.data.get(ref)


class _TicketCtx:
    """One legacy-tickets command (read-only): the config, the API and claims.json (for `held`)."""

    def __init__(self):
        self.cfg = load_config(Path.cwd())
        self.api = Api(self.cfg.server_url, self.cfg.device_token)
        self.claims = _Claims(self.cfg)

    def get(self, ref: str) -> dict:
        """GET one legacy ticket, with plain errors for a tombstone (410) or a missing ticket (404)."""
        try:
            return self.api.get_json(_ticket_path(ref))
        except ApiError as e:
            if e.status == 410:
                raise ApiError(410, "deleted", f"{ref} was deleted (ticket IDs are never reused)") from None
            if e.status == 404 and e.code == "not_found":
                raise ApiError(404, "not_found", f"{ref} does not exist") from None
            raise

    def load(self, ref: str) -> tuple[dict, dict, bytes]:
        """(TicketOut with events, decrypted content, DEK)."""
        t = self.get(ref)
        content, dek = open_ticket(self.cfg.mk, t)
        return t, content, dek


# ---- input files (also read by `mirror push --file`)

def _read_input_file(path_arg: str, root: Path) -> str:
    """A ticket, question or update file: text read into a sealed ticket, so the FS guards apply as for
    `share`: never a secret, and never outside this repo, symlinks included (the resolved path counts)."""
    p = Path(path_arg)
    if _in_skill_dir(p, root) or is_secret_path(p, root) or is_secret_path(p.resolve(), root):
        raise Refused(f"{_clean(path_arg)!r} looks like a secret (.env, keys, credentials, or this skill's folder); "
                      "refusing to put it into a ticket")
    if not p.is_file():
        raise UsageError(f"no such file: {path_arg}")
    rp = p.resolve()
    if not rp.is_relative_to(root):
        raise Refused(f"{_clean(path_arg)} is outside this repo ({root}); refusing to put it into a ticket "
                      "(copy it into the repo first if the user asked for it)")
    try:
        return rp.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise UsageError(f"{path_arg} is not UTF-8 text") from None


def _title(v) -> str:
    if isinstance(v, int) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str) or not 1 <= len(v.strip()) <= MAX_TITLE:
        raise UsageError(f"a ticket needs a title of 1–{MAX_TITLE} characters (`title:` or --title)")
    return v.strip()


_TICKET_FIELDS = ("id", "n", "status", "project", "type", "priority", "labels", "due", "parent", "blocked_by",
                  "created_by", "opened_by", "rev", "open_questions", "files", "claim", "created_at",
                  "updated_at", "last_seq", "archived_at")


def _event_view(dek: bytes | None, ticket_uuid: str, ev: dict) -> dict:
    """An EventOut with its body decrypted (None for bodiless kinds); untrusted strings stripped."""
    d = {k: ev.get(k) for k in ("seq", "kind", "actor", "status_from", "status_to", "created_at", "files")}
    d["body"] = None
    if ev.get("enc_body") and dek is not None:
        try:
            d["body"] = open_ticket_event(dek, bytes.fromhex(ticket_uuid), bytes.fromhex(ev["uuid"]), ev["enc_body"])
        except (IntegrityError, ValueError, KeyError, TypeError):
            d["undecryptable"] = True
    return _strip_deep(d)


def _event_line(e: dict) -> str:
    actor = e.get("actor") or {}
    move = f" {e['status_from']} → {e['status_to']}" if e.get("status_to") else ""
    body = e.get("body") if isinstance(e.get("body"), dict) else {}
    text = body.get("text") if isinstance(body.get("text"), str) else ""
    extra = ""
    if e.get("kind") == "answer":
        extra = " " + json.dumps(body.get("answers"), ensure_ascii=False)
    elif e.get("kind") == "verdict":
        extra = f" {body.get('verdict')}"
    elif e.get("kind") == "test":
        extra = f" passed={body.get('passed')} {body.get('summary') or ''}"
    line = (f"#{e.get('seq')} {e.get('created_at')} {_clean(actor.get('name') or '?')} ({actor.get('kind')}) "
            f"{e.get('kind')}{move}")
    return _text(line + extra) + (("\n    " + _text(text).replace("\n", "\n    ")) if text else "")


# ---------------------------------------------------------------- list / show / wait (legacy tickets are read-only)

LEGACY_READONLY = "legacy tickets are read-only; tickets now live in orch-core"


def cmd_tickets_wait(args) -> int:
    """Kept so an agent that still runs it learns why it returns at once (Task 12)."""
    raise CliExit(6, "legacy_readonly", f"{LEGACY_READONLY} — use `orch wait <ID> --json` there")


def _ticket_row(mk: bytes, t: dict) -> dict:
    try:
        title = open_ticket(mk, t)[0]["title"]
    except IntegrityError:
        title = None
    return _strip_deep({"id": t.get("id"), "title": title, "status": t.get("status"), "project": t.get("project"),
                        "type": t.get("type"), "priority": t.get("priority"), "labels": t.get("labels") or [],
                        "claim_state": (t.get("claim") or {}).get("state"),
                        "open_questions": t.get("open_questions"), "updated_at": t.get("updated_at")})


def cmd_tickets_list(args) -> int:
    ctx = _TicketCtx()
    statuses = args.status or ([] if args.all else [s for s in TICKET_STATUSES if s != "done"])
    q = [("status", s) for s in dict.fromkeys(statuses)]
    if args.project:
        q.append(("project", args.project))
    q += [("label", x) for x in normalize_tags(args.label)]
    raw = ctx.api.get_json("/api/tickets" + ("?" + urllib.parse.urlencode(q) if q else "")).get("tickets")
    rows = [_ticket_row(ctx.cfg.mk, t) for t in (raw if isinstance(raw, list) else []) if isinstance(t, dict)]
    if getattr(args, "json", False):
        print(_redact_exact(json.dumps(rows, ensure_ascii=False)))
        return 0
    if not rows:
        print("no tickets")
        return 0
    for r in rows:
        title = _clean(r["title"]) if r["title"] is not None else "(cannot decrypt)"
        live = f"  [{_clean(r['claim_state'])}]" if r["claim_state"] else ""
        qs = f"  {r['open_questions']} questions" if r["open_questions"] else ""
        print(_redact_exact(f"{_clean(r['id']):<9} {_clean(r['status']):<12} {_clean(r['priority']):<7} "
                            f"{_trunc(title, 60)}  ({_clean(r['project'])}){live}{qs}"))
    return 0


def cmd_tickets_show(args) -> int:
    ctx = _TicketCtx()
    ref = norm_ticket_ref(args.ref)
    t, content, dek = ctx.load(ref)
    d = _strip_deep({k: t.get(k) for k in _TICKET_FIELDS})
    d.update(_strip_deep({"title": content["title"], "body": content["body"], "fm": content["fm"]}))
    d["held"] = ctx.claims.get(ref) is not None
    d["events"] = [_event_view(dek, t["uuid"], e) for e in t.get("events") or [] if isinstance(e, dict)]
    claim = d.get("claim") or {}
    claim_text = "none"
    if claim:
        claim_text = (f"{_clean((claim.get('device') or {}).get('name') or '?')} ({_clean(str(claim.get('state')))})"
                      + (" — this repo" if d["held"] else ""))
    try:
        fm_text = _text(emit_yaml_subset(d["fm"])).rstrip()
    except TypeError:
        fm_text = _text(json.dumps(d["fm"], ensure_ascii=False))
    human = "\n".join([
        f"{ref}  {_clean(d['title'])}",
        f"  status   {_clean(d['status'])}   {_clean(d['type'])} · {_clean(d['priority'])} · {_clean(d['project'])}",
        f"  labels   {_tags_text(d.get('labels') or [])}",
        f"  claim    {claim_text}",
        "",
        _text(d["body"]).rstrip(),
        "",
        "frontmatter:",
        fm_text or "(none)",
        "",
        "events:",
        *("  " + _event_line(e) for e in d["events"]),
    ])
    _out(args, d, human)
    return 0


# ---------------------------------------------------------------- migrate (Task 12: tickets move to orch-core)

MIGRATE_WORD = "migrate"
ORCH_TIMEOUT_S = 60
DONE_REASON = "done: kept read-only on TIX for 90 days"
ARCHIVED_REASON = "already migrated (archived on TIX)"
_ORCH_TYPES = ("feature", "bug", "chore", "spike")


def _legacy_rows(ctx: "_TicketCtx") -> list[dict]:
    q = [("status", s) for s in TICKET_STATUSES] + [("limit", "500")]
    raw = ctx.api.get_json("/api/tickets?" + urllib.parse.urlencode(q)).get("tickets")
    return [t for t in (raw if isinstance(raw, list) else []) if isinstance(t, dict)]


def _migration_plan(ctx: "_TicketCtx") -> list[dict]:
    """Every legacy ticket by project, each with import true/false and why. Only this device's project is
    imported (a device migrates its own project's tickets); done and archived ones never are."""
    projects: dict[str, list[dict]] = {}
    for t in sorted(_legacy_rows(ctx), key=lambda r: int(r.get("n") or 0)):
        try:
            title = open_ticket(ctx.cfg.mk, t)[0]["title"]
        except IntegrityError:
            title = None
        project = str(t.get("project") or "")
        if t.get("archived_at"):
            imp, why = False, ARCHIVED_REASON
        elif t.get("status") == "done":
            imp, why = False, DONE_REASON
        elif project != ctx.cfg.project:
            imp, why = False, f"another project ({project}): run the migration on a device of that project"
        elif title is None:
            imp, why = False, "cannot be decrypted on this device"
        else:
            imp, why = True, ""
        projects.setdefault(project, []).append(_strip_deep({
            "id": t.get("id"), "title": title, "status": t.get("status"), "open_questions": t.get("open_questions"),
            "import": imp, "reason": why}))
    return [{"project": p, "device": p == ctx.cfg.project, "tickets": ts} for p, ts in sorted(projects.items())]


class _MigrateError(Exception):
    pass


_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")


def _neutral(text: str) -> str:
    """orch-core's `neutral_text` rule for legacy free text bound for a ticket section: a line that would open or
    close a fence (``` or ~~~ after at most 3 spaces) or start a heading (# after any indent) gets a backslash
    before it, so it reads the same but can never forge a section (## Plan, ## Log) or swallow the rest of the
    file in an unclosed fence."""
    out = []
    for line in str(text).splitlines():
        indent = len(line) - len(line.lstrip())
        rest = line[indent:]
        out.append(line[:indent] + "\\" + rest if _FENCE_RE.match(line) or rest.startswith("#") else line)
    return "\n".join(out)


def _run_orch(orch: str, workspace: Path, *args: str) -> dict:
    try:
        r = subprocess.run([orch, *args], cwd=workspace, check=False, capture_output=True, text=True,
                           timeout=ORCH_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise _MigrateError(f"orch {args[0]} failed: {type(e).__name__}") from None
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise _MigrateError(_redact(f"orch {args[0]} exited {r.returncode}: " + " / ".join(tail))[:500])
    try:
        return json.loads(r.stdout) if "--json" in args and r.stdout.strip() else {}
    except ValueError:
        raise _MigrateError(f"orch {args[0]} did not print JSON") from None


def _qkey(options: list, value) -> str | None:
    return chr(ord("A") + options.index(value)) if value in options else None


def _orch_question(q: dict) -> dict:
    """A legacy question (spec T5) as an `orch ask` entry: options keyed A, B, … by orch; recommended by key."""
    qtype = q.get("type") if q.get("type") in QUESTION_TYPES else "text"
    out = {"text": str(q.get("text") or ""), "type": qtype, "blocking": q.get("required", True) is not False}
    options = [str(o) for o in q.get("options") or []] if qtype in ("single", "multi") else []
    if options:
        out["options"] = options
    rec = q.get("recommended")
    if qtype == "single" and _qkey(options, rec):
        out["recommended"] = _qkey(options, rec)
    elif qtype == "multi" and isinstance(rec, list):
        keys = [k for k in (_qkey(options, r) for r in rec) if k]
        if keys:
            out["recommended"] = ",".join(keys)
    elif qtype == "confirm" and isinstance(rec, bool):
        out["recommended"] = "yes" if rec else "no"
    elif qtype == "text" and isinstance(rec, str) and rec:
        out["recommended"] = rec
    return out


def _questions(events: list[dict]) -> tuple[list[dict], list[str]]:
    """(open legacy questions, "question: answer" lines for the answered ones)."""
    asked = [e for e in events if e.get("kind") == "question" and isinstance(e.get("body"), dict)]
    answers = [e for e in events if e.get("kind") == "answer" and isinstance(e.get("body"), dict)]
    still_open, decided = [], []
    for qe in asked:
        reply = next((a for a in answers if a["body"].get("question_event") == qe.get("seq")), None)
        if reply is None:
            reply = next((a for a in answers if (a.get("seq") or 0) > (qe.get("seq") or 0)
                          and a["body"].get("question_event") is None), None)
        for q in qe["body"].get("questions") or []:
            if not isinstance(q, dict):
                continue
            if reply is None:
                still_open.append(q)
            else:
                got = (reply["body"].get("answers") or {}).get(q.get("id"))
                val = ", ".join(map(str, got)) if isinstance(got, list) else ("" if got is None else str(got))
                decided.append(f"- {_text(str(q.get('text') or q.get('id')))}: {_text(val) or '(no answer)'}")
    return still_open, decided


def _sections(t: dict, content: dict, events: list[dict]) -> dict[str, str]:
    fm = content.get("fm") if isinstance(content.get("fm"), dict) else {}
    out: dict[str, str] = {}
    acc = fm.get("acceptance")
    items = acc if isinstance(acc, list) else ([acc] if isinstance(acc, str) and acc.strip() else [])
    if items:
        out["Acceptance criteria"] = "".join(f"- [ ] {_text(str(i)).strip()}\n" for i in items)
    testing = fm.get("testing") if isinstance(fm.get("testing"), dict) else {}
    run = testing.get("run")
    run = run if isinstance(run, list) else ([run] if isinstance(run, str) else [])
    if run:
        out["Verification"] = ("The legacy ticket's test commands, kept as text (never run by the migration):\n\n"
                               + "".join(f"    {line}\n" for r in run for line in _text(str(r)).splitlines()))
    ctx_lines = [f"Was {t.get('status')} on TIX as {t.get('id')}."]
    if t.get("labels"):
        ctx_lines.append("Labels: " + ", ".join(map(str, t["labels"])))
    if t.get("due"):
        ctx_lines.append(f"Due: {t['due']}")
    if t.get("parent"):
        ctx_lines.append(f"Parent on TIX: {t['parent']}")
    if t.get("blocked_by"):
        ctx_lines.append("Blocked by on TIX: " + ", ".join(map(str, t["blocked_by"])))
    files = list(dict.fromkeys([*(t.get("files") or []), *(f for e in events for f in e.get("files") or [])]))
    if files:
        ctx_lines.append("Files on TIX: " + ", ".join(map(str, files)))
    rest = {k: v for k, v in fm.items() if k not in ("acceptance", "testing", "voice", "source")}
    if rest:
        ctx_lines.append("Other legacy fields: " + _text(json.dumps(rest, ensure_ascii=False, sort_keys=True)))
    out["Context"] = "\n".join(f"- {line}" if i else line for i, line in enumerate(ctx_lines)) + "\n"
    return out


def _workspace_name(ws: Path) -> str:
    try:
        name = json.loads((ws / "orchestrator" / "config.json").read_text(encoding="utf-8")).get("customer")
    except (OSError, ValueError, AttributeError):
        name = None
    return _clean(str(name or ws.name))[:80]


MIGRATE_STATE = "migrate.json"   # beside config.json: {"TIX-12": {"to", "workspace", "step"}}, 0600
_STEPS = ("created", "sections", "asked", "archived")


class _MigrateState:
    """What a migration already did per legacy ticket, written right after each step, so a re-run after a
    failure resumes the same orch ticket and never runs `orch new` twice."""

    def __init__(self, cfg: Config):
        self.path = cfg.config_path.parent / MIGRATE_STATE
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (OSError, ValueError):
            raise ConfigError(f"{self.path} is damaged; check it by hand before migrating again") from None
        self.data = data if isinstance(data, dict) else {}

    def get(self, ref: str) -> dict | None:
        e = self.data.get(ref)
        return e if isinstance(e, dict) and e.get("step") in _STEPS and isinstance(e.get("to"), str) else None

    def set(self, ref: str, to: str, workspace: Path, step: str) -> None:
        self.data[ref] = {"to": to, "workspace": str(workspace), "step": step}
        write_config(self.path, self.data)


def _migrate_one(ctx: "_TicketCtx", ref: str, orch: str, ws: Path, ws_name: str, tmp: Path,
                 state: _MigrateState) -> str:
    t, content, dek = ctx.load(ref)
    if t.get("archived_at"):
        raise _MigrateError(ARCHIVED_REASON)
    events = [_event_view(dek, t["uuid"], e) for e in t.get("events") or [] if isinstance(e, dict)]

    def write(name: str, text: str) -> str:
        p = tmp / f"{t['n']}-{name}"
        p.write_text(text, encoding="utf-8")
        return str(p)

    done = state.get(ref)
    if done and Path(done["workspace"]) != ws:
        raise _MigrateError(f"already migrated to {_clean(done['workspace'])} as {_clean(done['to'])}; finish it "
                            "there (or remove its entry from migrate.json)")
    if done:
        new_id, step = done["to"], done["step"]
    else:
        body = _neutral(_text(str(content.get("body") or "")).strip() or _text(str(content.get("title") or "")))
        ttype = t.get("type") if t.get("type") in _ORCH_TYPES else "feature"
        made = _run_orch(orch, ws, "new", "--title", _title(content.get("title")), "--type", ttype,
                         "--priority", str(t.get("priority") or "normal"), "--body-file",
                         write("ask.md", body + "\n"), "--json")
        new_id = made.get("id")
        if not isinstance(new_id, str) or not new_id:
            raise _MigrateError("orch new did not return the new ticket's id")
        step = "created"
        state.set(ref, new_id, ws, step)
    try:
        if step == "created":
            still_open, decided = _questions(events)
            sections = _sections(t, content, events)
            if decided:
                sections["Decisions"] = "Answered on TIX:\n" + "\n".join(decided) + "\n"
            for name, text in sections.items():     # replace: safe to run again after a failure
                _run_orch(orch, ws, "section", "set", new_id, name, "--file",
                          write(f"{name}.md", _neutral(text) + "\n"))
            step = "sections"
            state.set(ref, new_id, ws, step)
        if step == "sections":
            still_open, _ = _questions(events)
            if still_open:
                qs = {"questions": [_orch_question(q) for q in still_open]}
                _run_orch(orch, ws, "ask", new_id, "--file", write("q.yaml", json.dumps(qs, ensure_ascii=False)),
                          "--json")
            step = "asked"
            state.set(ref, new_id, ws, step)
    except _MigrateError as e:
        raise _MigrateError(f"created {new_id}, then {e}; run the migration again to finish it (TIX keeps "
                            f"{ref})") from None
    eu = os.urandom(16)
    note = {"text": f"Migrated to {ws_name} as {new_id}"}
    try:
        ctx.api.post_json(f"/api/tickets/{ref}/archive", {
            "uuid": eu.hex(), "enc_body": seal_ticket_event(dek, bytes.fromhex(t["uuid"]), eu, note)})
    except ApiError as e:
        raise _MigrateError(f"created {new_id}, but archiving {ref} on TIX failed ({e.detail or e.code}); run the "
                            "migration again to finish it") from None
    state.set(ref, new_id, ws, "archived")
    return new_id


def cmd_tickets_migrate(args) -> int:
    if not args.plan and not args.apply:
        raise UsageError("pass --plan (what would move) or --apply (move it)")
    ctx = _TicketCtx()
    plan = _migration_plan(ctx)
    if args.plan:
        lines = []
        for p in plan:
            lines.append(f"{_clean(p['project'])}{'  (this device)' if p['device'] else ''}")
            for t in p["tickets"]:
                mark = "import" if t["import"] else f"skip: {t['reason']}"
                lines.append(f"  {t['id']:<9} {_clean(t['status'] or ''):<12} {_trunc(t['title'] or '?', 50)}  {mark}")
        _out(args, {"projects": plan}, "\n".join(lines) or "no legacy tickets")
        return 0
    if _in_agent() or not _interactive():
        raise Refused("migrating tickets is for a human at a terminal; an agent may run --plan only")
    if not args.workspace:
        raise UsageError("--apply needs --workspace PATH (the orch-core workspace the tickets move into)")
    ws = Path(args.workspace).expanduser().resolve()
    if not ws.is_dir():
        raise UsageError(f"no such workspace folder: {ws}")
    orch = args.orch or shutil.which("orch")
    if not orch or not Path(orch).is_absolute() or not os.access(orch, os.X_OK):
        raise UsageError("orch was not found; pass --orch with its absolute path")
    todo = [t for p in plan if p["device"] for t in p["tickets"] if t["import"]]
    ws_name = _workspace_name(ws)
    print(f"This moves {len(todo)} ticket(s) of {_clean(ctx.cfg.project)} into {ws_name} ({ws}) as backlog "
          f"tickets and archives them on TIX (read-only, deleted after 90 days). Type {MIGRATE_WORD!r} to go on: ",
          end="", file=sys.stderr, flush=True)
    if sys.stdin.readline().strip() != MIGRATE_WORD:
        raise Refused("nothing migrated (the typed word did not match)")
    migrated, skipped = [], []
    state = _MigrateState(ctx.cfg)
    with tempfile.TemporaryDirectory(prefix="tix-migrate-") as tmp_name:
        for t in todo:
            try:
                new_id = _migrate_one(ctx, t["id"], orch, ws, ws_name, Path(tmp_name), state)
            except (_MigrateError, ApiError, IntegrityError, UsageError) as e:
                skipped.append({"id": t["id"], "title": t["title"], "reason": _redact(str(e))[:500]})
                continue
            migrated.append({"from": t["id"], "to": new_id, "title": t["title"]})
    human = "\n".join([*(f"{m['from']} -> {m['to']}  {_trunc(m['title'] or '', 50)}" for m in migrated),
                       *(f"{s['id']} skipped: {s['reason']}" for s in skipped)]) or "nothing to migrate"
    _out(args, {"migrated": migrated, "skipped": skipped}, human)
    return 0


# ---------------------------------------------------------------- parser: `sharing tickets …`

_TICKET_REGISTRARS: list[Callable] = []


def ticket_command(fn: Callable) -> Callable:
    """Decorator: fn(tsub, common) adds one `sharing tickets` subcommand."""
    _TICKET_REGISTRARS.append(fn)
    return fn


@command
def _reg_tickets(sub, common):
    p = sub.add_parser("tickets", parents=[common], help="legacy TIX tickets: list, show, migrate to orch-core",
                       description="Legacy tickets on TIX are read-only; tickets now live in orch-core. "
                                   "`migrate` moves them into an orch-core workspace.")
    tsub = p.add_subparsers(dest="tickets_cmd", required=True, metavar="COMMAND")
    for reg in _TICKET_REGISTRARS:
        reg(tsub, common)


@ticket_command
def _reg_tickets_list(tsub, common):
    p = tsub.add_parser("list", parents=[common], help="list tickets (default: everything but done)")
    p.add_argument("--status", action="append", default=[], choices=TICKET_STATUSES, help="repeatable")
    p.add_argument("--project")
    p.add_argument("--label", action="append", default=[], metavar="L", help="repeatable: all must match")
    p.add_argument("--all", action="store_true", help="include done tickets")
    p.set_defaults(func=cmd_tickets_list)


@ticket_command
def _reg_tickets_show(tsub, common):
    p = tsub.add_parser("show", parents=[common], help="show a ticket: fields, body, frontmatter and events")
    p.add_argument("ref", metavar="REF")
    p.set_defaults(func=cmd_tickets_show)


@ticket_command
def _reg_tickets_wait(tsub, common):
    p = tsub.add_parser("wait", parents=[common], help="legacy tickets are read-only: tells you to use `orch wait`")
    p.add_argument("ref", metavar="REF")
    p.set_defaults(func=cmd_tickets_wait)


@ticket_command
def _reg_tickets_migrate(tsub, common):
    p = tsub.add_parser("migrate", parents=[common], help="move legacy tickets into an orch-core workspace "
                                                          "(--plan shows what would move; --apply is for a human)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="list every legacy ticket and whether it would move")
    mode.add_argument("--apply", action="store_true", help="create them with orch new and archive them on TIX")
    p.add_argument("--workspace", metavar="PATH", help="the orch-core workspace (with --apply)")
    p.add_argument("--orch", metavar="PATH", help="the orch CLI (default: orch on PATH)")
    p.set_defaults(func=cmd_tickets_migrate)


if __name__ == "__main__":
    sys.exit(main())
