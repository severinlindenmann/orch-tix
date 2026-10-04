import os

import pytest

KEY = bytes(range(32))
FILE_UUID = bytes(range(1, 17))


def test_roundtrip_key_and_label(sharing):
    c = sharing.new_upload_link_crypto(KEY, "logs from Jonas")
    priv, pub = sharing.open_upload_link_key(KEY, c["uuid"], c["wrapped_lpriv"])
    assert pub == c["pub"]
    assert sharing.open_upload_label(KEY, c["uuid"], c["enc_label"]) == "logs from Jonas"


def test_empty_label_is_none(sharing):
    c = sharing.new_upload_link_crypto(KEY, "")
    assert c["enc_label"] is None
    assert sharing.open_upload_label(KEY, c["uuid"], c["enc_label"]) == ""


def test_wrapped_lpriv_length(sharing):
    c = sharing.new_upload_link_crypto(KEY, "x")
    assert len(sharing.unb64u(c["wrapped_lpriv"])) == 126


def test_seal_dek_to_link_roundtrip(sharing):
    c = sharing.new_upload_link_crypto(KEY, "x")
    priv, pub = sharing.open_upload_link_key(KEY, c["uuid"], c["wrapped_lpriv"])
    dek = os.urandom(32)
    s = sharing.seal_dek_to_link(dek, c["pub"], c["uuid"], FILE_UUID)
    raw = sharing.unb64u(s)
    assert len(raw) == 126
    assert raw[0] == 4
    assert sharing.open_sealed_dek(priv, c["pub"], c["uuid"], FILE_UUID, s) == dek


def test_sealed_dek_binding(sharing):
    c = sharing.new_upload_link_crypto(KEY, "x")
    priv, pub = sharing.open_upload_link_key(KEY, c["uuid"], c["wrapped_lpriv"])
    dek = os.urandom(32)
    s = sharing.seal_dek_to_link(dek, c["pub"], c["uuid"], FILE_UUID)

    other_file = bytes(range(2, 18))
    with pytest.raises(sharing.IntegrityError):
        sharing.open_sealed_dek(priv, c["pub"], c["uuid"], other_file, s)

    other_link_uuid = bytes(range(3, 19))
    with pytest.raises(sharing.IntegrityError):
        sharing.open_sealed_dek(priv, c["pub"], other_link_uuid, FILE_UUID, s)

    tampered = bytearray(sharing.unb64u(s))
    tampered[-1] ^= 0xFF
    with pytest.raises(sharing.IntegrityError):
        sharing.open_sealed_dek(priv, c["pub"], c["uuid"], FILE_UUID, sharing.b64u(bytes(tampered)))


def test_open_upload_link_key_wrong_mk(sharing):
    c = sharing.new_upload_link_crypto(KEY, "x")
    with pytest.raises(sharing.IntegrityError):
        sharing.open_upload_link_key(bytes(32), c["uuid"], c["wrapped_lpriv"])


def test_open_upload_link_key_tamper(sharing):
    c = sharing.new_upload_link_crypto(KEY, "x")
    tampered = bytearray(sharing.unb64u(c["wrapped_lpriv"]))
    tampered[-1] ^= 0xFF
    with pytest.raises(sharing.IntegrityError):
        sharing.open_upload_link_key(KEY, c["uuid"], sharing.b64u(bytes(tampered)))


def test_open_upload_link_key_rejects_mismatched_pub(sharing):
    c = sharing.new_upload_link_crypto(KEY, "x")
    priv, pub = sharing.open_upload_link_key(KEY, c["uuid"], c["wrapped_lpriv"])
    d_bytes = priv.private_numbers().private_value.to_bytes(32, "big")
    other = sharing.new_upload_link_crypto(KEY, "x")
    forged_plain = d_bytes + other["pub"]   # a d that doesn't match this pub
    forged = sharing.b64u(sharing.seal(KEY, forged_plain, sharing.aad_ulink(c["uuid"])))
    with pytest.raises(sharing.IntegrityError):
        sharing.open_upload_link_key(KEY, c["uuid"], forged)


def test_wrap_dek_under_mk_opens_with_file_meta_path(sharing):
    dek = os.urandom(32)
    wrapped_dek = sharing.wrap_dek_under_mk(KEY, dek, FILE_UUID)
    enc_meta = sharing.seal_file_meta(dek, FILE_UUID, {"name": "a", "mime": "b", "note": "c"})
    file_out = {"uuid": FILE_UUID.hex(), "wrapped_dek": wrapped_dek, "enc_meta": enc_meta}
    meta, opened_dek = sharing.open_file_meta(KEY, file_out)
    assert opened_dek == dek
    assert meta == {"name": "a", "mime": "b", "note": "c"}
