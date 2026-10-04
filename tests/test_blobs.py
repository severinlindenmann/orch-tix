import asyncio
import os
import time

import pytest

from fileshare.blobs import HEADER_LEN, BlobError, BlobStore, TooLarge
from tests.helpers.blobs import make_fake_blob

U1 = "0123456789abcdef0123456789abcdef"
U2 = "fedcba9876543210fedcba9876543210"


async def _agen(parts):
    for p in parts:
        yield p


def receive(store, parts, max_bytes=10_000):
    return asyncio.run(store.receive(_agen(parts), max_bytes))


@pytest.fixture
def store(tmp_path):
    return BlobStore(tmp_path)


def test_dirs_created(tmp_path):
    BlobStore(tmp_path)
    assert (tmp_path / "blobs").is_dir() and (tmp_path / "tmp").is_dir()


def test_receive_streams_to_tmp(store, tmp_path):
    tmp, size = receive(store, [b"abc", b"", b"defg"])
    assert size == 7
    assert tmp.parent == tmp_path / "tmp"
    assert tmp.read_bytes() == b"abcdefg"


def test_receive_too_large_leaves_nothing(store, tmp_path):
    with pytest.raises(TooLarge):
        receive(store, [b"x" * 6, b"x" * 6], max_bytes=10)
    assert list((tmp_path / "tmp").iterdir()) == []


def test_receive_exactly_at_limit_ok(store):
    _, size = receive(store, [b"x" * 10], max_bytes=10)
    assert size == 10


def test_validate_header_returns_key_version(store):
    tmp, _ = receive(store, [make_fake_blob(U1, key_version=3)])
    assert store.validate_header(tmp, U1) == 3


@pytest.mark.parametrize("mutate,uuid", [
    (lambda b: b"XXXX" + b[4:], U1),                  # bad magic
    (lambda b: b[:4] + b"\x02" + b[5:], U1),          # unsupported version
    (lambda b: b, U2),                                # header uuid != metadata uuid
    (lambda b: b[:HEADER_LEN + 15], U1),              # shorter than header + one GCM tag
])
def test_validate_header_rejects(store, mutate, uuid):
    tmp, _ = receive(store, [mutate(make_fake_blob(U1))])
    with pytest.raises(BlobError):
        store.validate_header(tmp, uuid)


def test_validate_header_minimum_length_accepted(store):
    tmp, _ = receive(store, [make_fake_blob(U1, body_len=16)])
    assert store.validate_header(tmp, U1) == 1


@pytest.mark.parametrize("bad", ["../../etc/passwd", "ABCDEF0123456789ABCDEF0123456789", "abc", U1 + "0", ""])
def test_uuid_must_be_32_lower_hex(store, bad):
    with pytest.raises(BlobError):
        store.path_for(bad)


def test_path_layout_and_commit(store, tmp_path):
    tmp, _ = receive(store, [make_fake_blob(U1)])
    dest = store.commit(tmp, U1)
    assert dest == tmp_path / "blobs" / "01" / "23" / f"{U1}.shr"
    assert dest == store.path_for(U1)
    assert dest.exists() and not tmp.exists()


def test_discard_and_delete_are_idempotent(store):
    tmp, _ = receive(store, [b"x"])
    store.discard(tmp)
    store.discard(tmp)
    store.delete(U1)  # never existed
    tmp, _ = receive(store, [make_fake_blob(U1)])
    store.commit(tmp, U1)
    store.delete(U1)
    assert not store.path_for(U1).exists()


def test_sweep_removes_stale_tmp_and_orphans(store, tmp_path):
    old_tmp, _ = receive(store, [b"old"])
    fresh_tmp, _ = receive(store, [b"fresh"])
    past = time.time() - 7200
    os.utime(old_tmp, (past, past))
    for u in (U1, U2):
        t, _ = receive(store, [make_fake_blob(u)])
        store.commit(t, u)
    store.sweep(live_uuids={U1}, tmp_max_age_s=3600)
    assert not old_tmp.exists()
    assert fresh_tmp.exists()
    assert store.path_for(U1).exists()
    assert not store.path_for(U2).exists()
