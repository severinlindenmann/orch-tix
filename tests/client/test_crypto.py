import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SHARING = REPO / "skill" / "sharing" / "sharing.py"

KEY = bytes(range(32))
UUID = bytes(range(16))
OTHER_UUID = bytes(range(1, 17))


def pt_of(n):
    return bytes(i % 251 for i in range(n))


def enc(s, pt, chunk_size=16, dek=KEY, uuid=UUID, key_version=1):
    out = io.BytesIO()
    n = s.encrypt_stream(dek, uuid, key_version, io.BytesIO(pt), out, chunk_size=chunk_size)
    blob = out.getvalue()
    assert n == len(blob)
    return blob


def dec(s, blob, dek=KEY, uuid=UUID):
    out = io.BytesIO()
    n = s.decrypt_stream(dek, uuid, io.BytesIO(blob), out)
    assert n == len(out.getvalue())
    return out.getvalue()


# ---------------------------------------------------------------- base64url

def test_b64u_roundtrip_unpadded(sharing):
    for n in range(0, 40):
        data = os.urandom(n)
        text = sharing.b64u(data)
        assert "=" not in text and "+" not in text and "/" not in text
        assert sharing.unb64u(text) == data


@pytest.mark.parametrize("bad", ["a", "ab=", "a+b/", "abc$", "AB"])
def test_unb64u_is_strict(sharing, bad):
    # "AB" is non-canonical: the low bits of the last char must be zero ("AA" is the canonical form of b"\x00").
    with pytest.raises(ValueError):
        sharing.unb64u(bad)


# ---------------------------------------------------------------- envelope

def test_seal_open_roundtrip(sharing):
    env = sharing.seal(KEY, b"hello", b"aad")
    assert env[0] == 1 and len(env) == 1 + 12 + 5 + 16
    assert sharing.open_(KEY, env, b"aad") == b"hello"


def test_seal_is_randomized(sharing):
    assert sharing.seal(KEY, b"x", b"") != sharing.seal(KEY, b"x", b"")


def test_seal_with_fixed_nonce_is_deterministic(sharing):
    nonce = bytes(12)
    assert sharing.seal(KEY, b"x", b"", nonce=nonce) == sharing.seal(KEY, b"x", b"", nonce=nonce)


def test_open_rejects_wrong_key(sharing):
    env = sharing.seal(KEY, b"hello", b"aad")
    with pytest.raises(sharing.IntegrityError):
        sharing.open_(bytes(32), env, b"aad")


def test_open_rejects_wrong_aad(sharing):
    env = sharing.seal(KEY, b"hello", b"aad")
    with pytest.raises(sharing.IntegrityError):
        sharing.open_(KEY, env, b"other")


def test_open_rejects_bad_version(sharing):
    env = bytearray(sharing.seal(KEY, b"hello", b"aad"))
    env[0] = 2
    with pytest.raises(sharing.IntegrityError):
        sharing.open_(KEY, bytes(env), b"aad")


def test_open_rejects_short_envelope(sharing):
    with pytest.raises(sharing.IntegrityError):
        sharing.open_(KEY, b"\x01" + bytes(20), b"")


# ---------------------------------------------------------------- kdf

def test_derive_splits_auth_and_kek(sharing):
    auth, kek = sharing.derive("correct horse battery staple", bytes(16), 1000)
    assert len(auth) == 32 and len(kek) == 32 and auth != kek
    assert sharing.derive("correct horse battery staple", bytes(16), 1000) == (auth, kek)


def test_derive_normalizes_nfc(sharing):
    nfc = "p\u00e4ssw\u00f6rt"
    nfd = "pa\u0308sswo\u0308rt"  # NFD, written as escapes so editors cannot re-normalize it
    assert sharing.derive(nfc, bytes(16), 1000) == sharing.derive(nfd, bytes(16), 1000)


def test_derive_depends_on_salt(sharing):
    assert sharing.derive("pw", bytes(16), 1000) != sharing.derive("pw", b"\x01" * 16, 1000)


# ---------------------------------------------------------------- blobs

@pytest.mark.parametrize("n", [0, 1, 15, 16, 17, 32, 40])
def test_blob_roundtrip(sharing, n):
    blob = enc(sharing, pt_of(n))
    assert dec(sharing, blob) == pt_of(n)


@pytest.mark.parametrize("n,chunks", [(0, 1), (1, 1), (16, 1), (17, 2), (32, 2), (40, 3)])
def test_blob_layout(sharing, n, chunks):
    blob = enc(sharing, pt_of(n))
    assert len(blob) == sharing.HEADER_LEN + n + 16 * chunks
    assert blob[:4] == b"SHR1" and blob[4] == 1 and blob[5] == 1
    assert blob[6:22] == UUID
    assert int.from_bytes(blob[22:26], "big") == 16


def test_blob_default_chunk_size_is_1mib(sharing):
    out = io.BytesIO()
    sharing.encrypt_stream(KEY, UUID, 1, io.BytesIO(b"abc"), out)
    assert int.from_bytes(out.getvalue()[22:26], "big") == 1 << 20 == sharing.CHUNK


def test_truncation_at_chunk_boundary_fails(sharing):
    blob = enc(sharing, pt_of(40))            # 3 chunks of ct: 32, 32, 24 bytes
    cut = blob[: sharing.HEADER_LEN + 32 * 2]  # drop the last chunk exactly
    with pytest.raises(sharing.IntegrityError):
        dec(sharing, cut)


def test_truncation_mid_chunk_fails(sharing):
    blob = enc(sharing, pt_of(40))
    with pytest.raises(sharing.IntegrityError):
        dec(sharing, blob[:-5])


def test_tail_shorter_than_tag_fails(sharing):
    blob = enc(sharing, pt_of(32))            # 2 full chunks
    with pytest.raises(sharing.IntegrityError):
        dec(sharing, blob + b"\x00" * 7)


def test_reordered_chunks_fail(sharing):
    blob = enc(sharing, pt_of(40))
    h = sharing.HEADER_LEN
    c0, c1, rest = blob[h:h + 32], blob[h + 32:h + 64], blob[h + 64:]
    with pytest.raises(sharing.IntegrityError):
        dec(sharing, blob[:h] + c1 + c0 + rest)


def test_extra_trailing_chunk_fails(sharing):
    blob = enc(sharing, pt_of(32))
    h = sharing.HEADER_LEN
    with pytest.raises(sharing.IntegrityError):
        dec(sharing, blob + blob[h:h + 32])


def test_header_uuid_mismatch_fails(sharing):
    blob = enc(sharing, pt_of(10))
    with pytest.raises(sharing.IntegrityError):
        dec(sharing, blob, uuid=OTHER_UUID)


def test_header_uuid_rewritten_fails(sharing):
    blob = bytearray(enc(sharing, pt_of(10)))
    blob[6:22] = OTHER_UUID                    # attacker relabels the blob as another file
    with pytest.raises(sharing.IntegrityError):
        dec(sharing, bytes(blob), uuid=OTHER_UUID)


def test_header_key_version_tamper_fails(sharing):
    blob = bytearray(enc(sharing, pt_of(10)))
    blob[5] = 2
    with pytest.raises(sharing.IntegrityError):
        dec(sharing, bytes(blob))


def test_bad_magic_fails(sharing):
    blob = bytearray(enc(sharing, pt_of(10)))
    blob[0:4] = b"XXXX"
    with pytest.raises(sharing.IntegrityError):
        dec(sharing, bytes(blob))


def test_wrong_dek_fails(sharing):
    blob = enc(sharing, pt_of(10))
    with pytest.raises(sharing.IntegrityError):
        dec(sharing, blob, dek=bytes(32))


def test_empty_input_fails(sharing):
    with pytest.raises(sharing.IntegrityError):
        dec(sharing, b"")


def test_zero_chunk_size_header_rejected(sharing):
    blob = bytearray(enc(sharing, pt_of(10)))
    blob[22:26] = (0).to_bytes(4, "big")
    with pytest.raises(sharing.IntegrityError):
        dec(sharing, bytes(blob))


def test_encrypt_rejects_bad_params(sharing):
    with pytest.raises(ValueError):
        sharing.encrypt_stream(bytes(31), UUID, 1, io.BytesIO(b""), io.BytesIO())
    with pytest.raises(ValueError):
        sharing.encrypt_stream(KEY, bytes(15), 1, io.BytesIO(b""), io.BytesIO())
    with pytest.raises(ValueError):
        sharing.encrypt_stream(KEY, UUID, 256, io.BytesIO(b""), io.BytesIO())
    with pytest.raises(ValueError):
        sharing.encrypt_stream(KEY, UUID, 1, io.BytesIO(b""), io.BytesIO(), chunk_size=0)


class Dribble(io.RawIOBase):
    """Returns at most 3 bytes per read, like a pipe."""
    def __init__(self, data):
        self._b = io.BytesIO(data)

    def readable(self):
        return True

    def readinto(self, buf):
        chunk = self._b.read(min(3, len(buf)))
        buf[: len(chunk)] = chunk
        return len(chunk)


def test_short_reads_are_handled(sharing):
    prefix = bytes(8)
    dribbled, whole = io.BytesIO(), io.BytesIO()
    sharing.encrypt_stream(KEY, UUID, 1, Dribble(pt_of(40)), dribbled, chunk_size=16, nonce_prefix=prefix)
    sharing.encrypt_stream(KEY, UUID, 1, io.BytesIO(pt_of(40)), whole, chunk_size=16, nonce_prefix=prefix)
    assert dribbled.getvalue() == whole.getvalue()
    back = io.BytesIO()
    sharing.decrypt_stream(KEY, UUID, Dribble(dribbled.getvalue()), back)
    assert back.getvalue() == pt_of(40)


# ---------------------------------------------------------------- file meta

def test_file_meta_roundtrip(sharing):
    mk = os.urandom(32)
    fc = sharing.new_file_crypto(mk, "deploy-notes.md", "text/markdown", "for FILE7 ü")
    assert len(fc["uuid"]) == 16 and fc["uuid_hex"] == fc["uuid"].hex()
    file_out = {"uuid": fc["uuid_hex"], "wrapped_dek": fc["wrapped_dek"], "enc_meta": fc["enc_meta"]}
    meta, dek = sharing.open_file_meta(mk, file_out)
    assert meta == {"name": "deploy-notes.md", "mime": "text/markdown", "note": "for FILE7 ü"}
    assert dek == fc["dek"]


def test_file_meta_swap_between_files_fails(sharing):
    mk = os.urandom(32)
    a = sharing.new_file_crypto(mk, "a.txt", "text/plain", "")
    b = sharing.new_file_crypto(mk, "b.txt", "text/plain", "")
    swapped = {"uuid": a["uuid_hex"], "wrapped_dek": b["wrapped_dek"], "enc_meta": b["enc_meta"]}
    with pytest.raises(sharing.IntegrityError):
        sharing.open_file_meta(mk, swapped)


def test_file_meta_wrong_mk_fails(sharing):
    fc = sharing.new_file_crypto(os.urandom(32), "a.txt", "text/plain", "")
    file_out = {"uuid": fc["uuid_hex"], "wrapped_dek": fc["wrapped_dek"], "enc_meta": fc["enc_meta"]}
    with pytest.raises(sharing.IntegrityError):
        sharing.open_file_meta(os.urandom(32), file_out)


def test_file_meta_injected_rng_order(sharing):
    stream = bytes((i * 7 + 3) % 256 for i in range(72))
    pos = [0]

    def rng(n):
        out = stream[pos[0]:pos[0] + n]
        pos[0] += n
        return out

    fc = sharing.new_file_crypto(KEY, "a", "b", "c", _rng=rng)
    assert fc["uuid"] == stream[0:16]
    assert fc["dek"] == stream[16:48]
    assert sharing.unb64u(fc["wrapped_dek"])[1:13] == stream[48:60]
    assert sharing.unb64u(fc["enc_meta"])[1:13] == stream[60:72]
    assert pos[0] == 72


# ---------------------------------------------------------------- onboarding code

def test_parse_code_roundtrip(sharing):
    lookup = os.urandom(16)
    assert sharing.parse_code(f"shr1.{sharing.b64u(lookup)}") == lookup


@pytest.mark.parametrize("bad", [
    "",
    "shr1.",
    "shr2." + "A" * 22,
    "shr1." + "A" * 21,
    "shr1." + "A" * 23,
    "shr1." + "A" * 21 + "$",
    " shr1." + "A" * 22,
    "shr1." + "A" * 22 + "." + "A" * 43,   # the old v2 format with an embedded secret is refused
    "shr1." + "A" * 21 + "B",             # non-canonical base64url (trailing bits set)
])
def test_parse_code_rejects(sharing, bad):
    with pytest.raises(sharing.Refused):
        sharing.parse_code(bad)


# ---------------------------------------------------------------- device approval (§4.6)

DEV_ID = "dev_0123456789ab"


def test_keypair_shapes(sharing):
    priv, pub = sharing.new_device_keypair()
    assert len(pub) == 65 and pub[0] == 4
    assert priv[:1] == b"\x30"                       # DER SEQUENCE (PKCS8)
    priv2, pub2 = sharing.new_device_keypair(_priv_int=12345)
    assert sharing.new_device_keypair(_priv_int=12345) == (priv2, pub2)   # deterministic


def test_fingerprint_format(sharing):
    _, pub = sharing.new_device_keypair(_priv_int=7)
    fp = sharing.fingerprint(pub)
    # 100 bits: 20 base32 chars grouped 4-4-4-4-4 -> 24 chars total (§4.6, security fix 2026-09-25).
    assert len(fp) == 24 and fp[4] == "-" and fp[9] == "-" and fp[14] == "-" and fp[19] == "-"
    assert all(ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for ch in fp.replace("-", ""))
    import base64, hashlib
    raw = base64.b32encode(hashlib.sha256(sharing.FP_PREFIX + pub).digest()).decode()[:20]
    assert fp == "-".join(raw[i:i + 4] for i in range(0, 20, 4))


def test_approval_roundtrip(sharing):
    priv, pub = sharing.new_device_keypair()
    bundle = sharing.seal_to_device(KEY, pub, DEV_ID)
    assert len(bundle) == 65 + 1 + 12 + 32 + 16
    assert sharing.open_device_bundle(priv, pub, DEV_ID, bundle) == KEY


def test_approval_wrong_device_key_fails(sharing):
    _, pub = sharing.new_device_keypair()
    other_priv, other_pub = sharing.new_device_keypair()
    bundle = sharing.seal_to_device(KEY, pub, DEV_ID)
    with pytest.raises(sharing.IntegrityError):
        # the other device's private key, but claiming the intended pub, is refused before ECDH
        sharing.open_device_bundle(other_priv, pub, DEV_ID, bundle)
    with pytest.raises(sharing.IntegrityError):
        sharing.open_device_bundle(other_priv, other_pub, DEV_ID, bundle)


def test_approval_bundle_bound_to_device_id(sharing):
    priv, pub = sharing.new_device_keypair()
    bundle = sharing.seal_to_device(KEY, pub, "dev_aaaaaaaaaaaa")
    with pytest.raises(sharing.IntegrityError):
        sharing.open_device_bundle(priv, pub, "dev_bbbbbbbbbbbb", bundle)


def test_approval_tampered_eph_pub_fails(sharing):
    priv, pub = sharing.new_device_keypair()
    bundle = bytearray(sharing.seal_to_device(KEY, pub, DEV_ID))
    bundle[40] ^= 0x01                                 # inside eph_pub's x coordinate
    with pytest.raises(sharing.IntegrityError):
        sharing.open_device_bundle(priv, pub, DEV_ID, bytes(bundle))
    # swap in a different, valid ephemeral key: point is on the curve, tag still fails
    _, fake_eph = sharing.new_device_keypair()
    swapped = fake_eph + bytes(bundle)[65:]
    with pytest.raises(sharing.IntegrityError):
        sharing.open_device_bundle(priv, pub, DEV_ID, swapped)


def test_approval_tampered_ciphertext_and_length(sharing):
    priv, pub = sharing.new_device_keypair()
    bundle = bytearray(sharing.seal_to_device(KEY, pub, DEV_ID))
    bundle[-1] ^= 0x80
    with pytest.raises(sharing.IntegrityError):
        sharing.open_device_bundle(priv, pub, DEV_ID, bytes(bundle))
    with pytest.raises(sharing.IntegrityError):
        sharing.open_device_bundle(priv, pub, DEV_ID, bytes(bundle[:-1]))


@pytest.mark.parametrize("bad", [
    b"",
    b"\x04" + b"\x00" * 64,                          # (0,0) is not on the curve
    b"\x04" + b"\x01" * 64,                          # random coordinates, off-curve
    b"\x02" + b"\x01" * 32,                          # compressed form is not accepted
    b"\x04" + b"\xff" * 64,                          # coordinates >= p
])
def test_invalid_device_point_rejected(sharing, bad):
    with pytest.raises(ValueError):
        sharing.seal_to_device(KEY, bad, DEV_ID)


# ---------------------------------------------------------------- fail closed

def test_main_refuses_when_crypto_missing(sharing, monkeypatch, capsys):
    monkeypatch.setattr(sharing, "_CRYPTO_ERR", ImportError("no cryptography"))
    assert sharing.main(["whoami"]) == 5
    assert "refusing" in capsys.readouterr().err


def test_script_exits_5_without_cryptography():
    code = (
        "import sys, runpy\n"
        "sys.modules['cryptography'] = None\n"
        f"sys.argv = ['sharing', 'whoami']\n"
        f"runpy.run_path({str(SHARING)!r}, run_name='__main__')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 5, proc.stderr
    assert "refusing" in proc.stderr


# ---------------------------------------------------------------- encrypted settings (§15)

def test_settings_aad_constant(sharing):
    assert sharing.AAD_SETTINGS == b"sharing/settings/v1"


def test_settings_roundtrip_and_canonical_json(sharing):
    obj = {"zeta": "ü ✓", "deepgram_api_key": "dg-test", "alpha": {"b": 1, "a": [1, "x"]}}
    enc_ = sharing.seal_settings(KEY, obj)
    assert sharing.open_settings(KEY, enc_) == obj
    env = sharing.unb64u(enc_)
    pt = sharing.open_(KEY, env, sharing.AAD_SETTINGS)
    assert pt == '{"alpha":{"a":[1,"x"],"b":1},"deepgram_api_key":"dg-test","zeta":"ü ✓"}'.encode("utf-8")
    # a fresh nonce every time
    assert sharing.seal_settings(KEY, obj) != enc_


def test_settings_fixed_nonce_is_deterministic(sharing):
    a = sharing.seal_settings(KEY, {"k": "v"}, _nonce=bytes(12))
    assert a == sharing.seal_settings(KEY, {"k": "v"}, _nonce=bytes(12))


def test_settings_wrong_key_or_aad_fails(sharing):
    enc_ = sharing.seal_settings(KEY, {"deepgram_api_key": "x"})
    with pytest.raises(sharing.IntegrityError):
        sharing.open_settings(os.urandom(32), enc_)
    # a wrapped DEK (same MK, other AAD) must never open as settings
    wrapped = sharing.b64u(sharing.seal(KEY, b'{"deepgram_api_key":"x"}', sharing.aad_dek(UUID)))
    with pytest.raises(sharing.IntegrityError):
        sharing.open_settings(KEY, wrapped)
    # and settings never open as a wrapped DEK
    with pytest.raises(sharing.IntegrityError):
        sharing.open_(KEY, sharing.unb64u(enc_), sharing.aad_dek(UUID))


@pytest.mark.parametrize("pt", [b"[]", b'"x"', b"1", b"null", b"not json", b"\xff\xfe"])
def test_open_settings_rejects_non_object(sharing, pt):
    enc_ = sharing.b64u(sharing.seal(KEY, pt, sharing.AAD_SETTINGS))
    with pytest.raises(sharing.IntegrityError):
        sharing.open_settings(KEY, enc_)


@pytest.mark.parametrize("bad", ["", "!!!", "AQ", None, 5])
def test_open_settings_rejects_malformed_envelope(sharing, bad):
    with pytest.raises(sharing.IntegrityError):
        sharing.open_settings(KEY, bad)


def test_seal_settings_requires_object(sharing):
    for bad in ([], "x", None):
        with pytest.raises(TypeError):
            sharing.seal_settings(KEY, bad)


# ---------------------------------------------------------------- public links (§17)

LK = bytes(range(100, 132))


def test_link_aad(sharing):
    assert sharing.aad_link(UUID) == b"sharing/link/v1|" + UUID
    assert sharing.AAD_LINK_PREFIX == b"sharing/link/v1|"


def test_link_wrap_roundtrip_and_fresh_nonce(sharing):
    dek = os.urandom(32)
    w = sharing.wrap_dek_for_link(dek, UUID, LK)
    assert isinstance(w, str)
    assert sharing.open_link_dek(LK, UUID, w) == dek
    assert sharing.wrap_dek_for_link(dek, UUID, LK) != w
    assert len(sharing.unb64u(w)) == 1 + 12 + 32 + 16
    fixed = sharing.wrap_dek_for_link(dek, UUID, LK, _nonce=bytes(12))
    assert fixed == sharing.wrap_dek_for_link(dek, UUID, LK, _nonce=bytes(12))


def test_link_wrong_key_or_uuid_fails(sharing):
    w = sharing.wrap_dek_for_link(KEY, UUID, LK)
    with pytest.raises(sharing.IntegrityError):
        sharing.open_link_dek(bytes(32), UUID, w)
    with pytest.raises(sharing.IntegrityError):
        sharing.open_link_dek(LK, OTHER_UUID, w)


@pytest.mark.parametrize("bad", ["", "!!!", "AQ", None, 5, "A" * 10])
def test_link_malformed_envelope_is_integrity_error(sharing, bad):
    with pytest.raises(sharing.IntegrityError):
        sharing.open_link_dek(LK, UUID, bad)


def test_link_bad_key_length(sharing):
    with pytest.raises(ValueError):
        sharing.wrap_dek_for_link(KEY, UUID, b"short")
    with pytest.raises(ValueError):
        sharing.wrap_dek_for_link(b"short", UUID, LK)
    with pytest.raises(sharing.IntegrityError):
        sharing.open_link_dek(b"short", UUID, sharing.wrap_dek_for_link(KEY, UUID, LK))


def test_link_opened_dek_must_be_32_bytes(sharing):
    env = sharing.b64u(sharing.seal(LK, b"x" * 31, sharing.aad_link(UUID)))
    with pytest.raises(sharing.IntegrityError):
        sharing.open_link_dek(LK, UUID, env)


def test_dek_settings_and_link_envelopes_do_not_swap(sharing):
    """One key used in all three roles: no envelope opens in another role."""
    k, dek = KEY, bytes(range(50, 82))
    wrapped_dek = sharing.b64u(sharing.seal(k, dek, sharing.aad_dek(UUID)))
    link = sharing.wrap_dek_for_link(dek, UUID, k)
    settings = sharing.seal_settings(k, {"deepgram_api_key": "x" * 20})
    # a DEK envelope and a settings envelope are not link envelopes
    for env in (wrapped_dek, settings):
        with pytest.raises(sharing.IntegrityError):
            sharing.open_link_dek(k, UUID, env)
    # a link envelope is neither a wrapped DEK nor settings
    with pytest.raises(sharing.IntegrityError):
        sharing.open_(k, sharing.unb64u(link), sharing.aad_dek(UUID))
    with pytest.raises(sharing.IntegrityError):
        sharing.open_settings(k, link)
    meta = sharing.seal_file_meta(dek, UUID, {"name": "a", "mime": "b", "note": ""})
    with pytest.raises(sharing.IntegrityError):
        sharing.open_file_meta(k, {"uuid": UUID.hex(), "wrapped_dek": link, "enc_meta": meta})


def test_open_link_file_returns_meta_and_dek(sharing):
    mk = bytes(range(10, 42))
    fc = sharing.new_file_crypto(mk, "a.md", "text/markdown", "hi")
    w = sharing.wrap_dek_for_link(fc["dek"], fc["uuid"], LK)
    pub = {"uuid": fc["uuid_hex"], "wrapped_dek_link": w, "enc_meta": fc["enc_meta"]}
    meta, dek = sharing.open_link_file(LK, pub)
    assert meta == {"name": "a.md", "mime": "text/markdown", "note": "hi"} and dek == fc["dek"]
    with pytest.raises(sharing.IntegrityError):
        sharing.open_link_file(bytes(32), pub)
    for bad in ({**pub, "uuid": "zz"}, {**pub, "uuid": "00" * 15}, {**pub, "enc_meta": None},
                {**pub, "wrapped_dek_link": None}, {}):
        with pytest.raises(sharing.IntegrityError):
            sharing.open_link_file(LK, bad)
